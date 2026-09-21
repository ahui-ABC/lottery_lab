"""竞彩串关方案测试。"""
import pytest

from lottery_lab.db import store
from lottery_lab.models import jc_parlay


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _seed(conn, day, pool, entries, results=None):
    """写入预测行（选场来源）与比赛。

    entries: [(match_id, pick, odds, prob)]
    """
    for i, (mid, pick, odds, prob) in enumerate(entries):
        res = (results or {}).get(mid)
        conn.execute(
            f"""INSERT INTO jc_matches(match_id, match_date, home_team, away_team,
                                       result_{pool})
               VALUES(?, ?, ?, ?, ?)""",
            (mid, day, f"H{mid}", f"A{mid}", res))
        conn.execute(
            """INSERT INTO jc_predictions(match_id, predicted_on, pool, method,
                                          pick, odds, prob)
               VALUES(?,?,?, 'market', ?,?,?)""",
            (mid, day, pool, pick, odds, prob))
        conn.execute(
            """INSERT INTO jc_predictions(match_id, predicted_on, pool, method,
                                          pick, odds, prob)
               VALUES(?,?,?, 'trend', ?,?,?)""",
            (mid, day, pool, pick, odds, prob - 0.01))
    conn.commit()


def test_pick_legs_orders_by_prob_desc():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 6.0, 0.10),
        (2, "s02s00", 5.0, 0.30),   # 概率最高
        (3, "s03s00", 8.0, 0.20),
        (4, "s01s01", 7.0, 0.25),
    ])

    legs = jc_parlay.pick_legs(conn, "2026-09-20", "crs", 2)

    assert [x["match_id"] for x in legs] == [2, 4], "应按市场概率降序取前 N 场"
    assert legs[0]["odds"] == 5.0


def test_build_bets_creates_all_combinations():
    legs = [{"match_id": i} for i in range(1, 5)]        # 4 场

    bets = jc_parlay.build_bets(legs, unit=2.0, min_combo=2)

    # C(4,2) + C(4,3) + C(4,4) = 6 + 4 + 1 = 11
    assert len(bets) == 11
    assert sum(1 for b in bets if b["size"] == 2) == 6
    assert sum(1 for b in bets if b["size"] == 3) == 4
    assert sum(1 for b in bets if b["size"] == 4) == 1
    assert all(b["amount"] == 2.0 for b in bets)


def test_build_plan_requires_enough_legs():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [(1, "s01s00", 6.0, 0.1), (2, "s02s00", 5.0, 0.2)])

    assert jc_parlay.build_plan(conn, "2026-09-20", "crs", 4) is None
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 2)
    assert plan is not None and len(plan["legs"]) == 2


def test_score_plans_pays_only_full_hits():
    conn = _db()
    # 4 场比分：两场押 s01s00、两场押 s02s00，赔率各 5.0
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 5.0, 0.4), (2, "s01s00", 5.0, 0.35),
        (3, "s02s00", 5.0, 0.3), (4, "s02s00", 5.0, 0.25),
    ], results={1: "1:0", 2: "1:0", 3: "9:9", 4: "9:9"})   # 只有 1、2 中
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 4)
    jc_parlay.save_plan(conn, plan)

    out = jc_parlay.score_plans(conn)

    assert out["scored"] == 1
    row = conn.execute("SELECT * FROM jc_parlay_plans").fetchone()
    assert row["winning_bets"] == 1          # 只有 (1,2) 那注 2串1 中
    assert row["invested"] == 22.0           # 11 注 × 2 元
    assert row["returned"] == 5.0 * 5.0 * 2  # 组合奖金 = 25 × 2


def test_score_plans_skips_unfinished():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 5.0, 0.4), (2, "s01s00", 5.0, 0.35),
    ])                                        # 无赛果
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 2)
    jc_parlay.save_plan(conn, plan)

    out = jc_parlay.score_plans(conn)

    assert out["scored"] == 0 and out["skipped"] == 1
    # 未对奖的行仍可被下次捞出
    assert conn.execute(
        "SELECT COUNT(*) c FROM jc_parlay_plans WHERE scored_at IS NULL"
    ).fetchone()["c"] == 1


def test_score_plans_is_idempotent():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 5.0, 0.4), (2, "s01s00", 5.0, 0.35),
    ], results={1: "1:0", 2: "1:0"})
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 2)
    jc_parlay.save_plan(conn, plan)
    jc_parlay.score_plans(conn)

    again = jc_parlay.score_plans(conn)

    assert again["scored"] == 0, "第二次不应再处理"


def test_save_plan_overwrites_same_day_and_pool():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [(1, "s01s00", 5.0, 0.4), (2, "s02s00", 6.0, 0.3)])
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 2)
    jc_parlay.save_plan(conn, plan)
    jc_parlay.save_plan(conn, plan)

    assert conn.execute("SELECT COUNT(*) c FROM jc_parlay_plans").fetchone()["c"] == 1


def test_summary_aggregates_by_pool():
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [(1, "s01s00", 5.0, 0.4), (2, "s01s00", 5.0, 0.3)],
          results={1: "1:0", 2: "1:0"})
    jc_parlay.save_plan(conn, jc_parlay.build_plan(conn, "2026-09-20", "crs", 2))
    jc_parlay.score_plans(conn)

    got = jc_parlay.summary(conn)

    assert "crs" in got["by_pool"]
    assert got["by_pool"]["crs"]["invested"] == 2.0      # 1 注 × 2 元
    assert got["by_pool"]["crs"]["returned"] == 50.0     # 5×5×2
    assert got["total"]["profit"] == 48.0
