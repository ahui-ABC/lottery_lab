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


def test_build_plan_uses_however_many_are_available():
    """场次不够不该整天不出方案 —— n_legs 是上限不是必须达到的门槛。

    用户反馈的场景：某天只有 3 场在售，三个玩法全部因「可用场次不足 5/4」
    被跳过，页面上一条方案都没有。只要有 2 场（能组 2 串 1）就该出。
    """
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 6.0, 0.3), (2, "s02s00", 5.0, 0.25), (3, "s03s00", 8.0, 0.2),
    ])

    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 4)

    assert plan is not None, "3 场 < 上限 4 场，但仍应出方案"
    assert len(plan["legs"]) == 3
    assert plan["n_legs"] == 3          # 实际场次
    assert plan["n_legs_max"] == 4      # 原定上限，保留以便追溯
    # C(3,2) + C(3,3) = 3 + 1 = 4 注
    assert len(plan["bets"]) == 4


def test_build_plan_needs_at_least_two_legs():
    """1 场组不成 2 串 1 —— 这是组合结构的硬下限，不是策略选择。"""
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [(1, "s01s00", 6.0, 0.5)])
    assert jc_parlay.build_plan(conn, "2026-09-20", "crs", 4) is None

    _seed(conn, "2026-09-20", "crs", [(2, "s02s00", 5.0, 0.4)])
    assert jc_parlay.build_plan(conn, "2026-09-20", "crs", 4) is not None


def test_min_prob_filters_out_less_confident_legs():
    """「有把握」的实现点：低于门槛的候选直接不选。"""
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.70),      # 达标
        (2, "h", 1.8, 0.60),      # 达标
        (3, "h", 2.5, 0.40),      # 不达标
    ])

    legs = jc_parlay.pick_legs(conn, "2026-09-20", "hhad", 5, min_prob=0.55)

    assert [x["match_id"] for x in legs] == [1, 2]


def test_threshold_frees_the_slot_for_a_more_confident_leg():
    """门槛要在 SQL 里过滤，不能取回来再筛 —— 否则名额会被刷掉的场次占掉。"""
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.2, 0.85),
        (2, "h", 2.5, 0.40),      # 概率低于第 3 场之外，但排在第 3 位
        (3, "h", 2.0, 0.30),
    ])

    # 上限 2 场、门槛 0.55：只有 1 场达标，就该只出 1 场，而不是拿 0.40 的凑数
    legs = jc_parlay.pick_legs(conn, "2026-09-20", "hhad", 2, min_prob=0.55)
    assert [x["match_id"] for x in legs] == [1]


def test_hhad_uses_default_threshold_but_crs_does_not():
    """门槛按玩法而异，且是样本外验证的结果，不是一刀切。

    让球加门槛有效（样本外 83.7% vs 75.5%）；比分加门槛反而更差
    （0.11 → 63.1% vs 75.0%），所以比分的默认门槛必须是 0。
    """
    assert jc_parlay.MIN_PROB["hhad"] > 0
    assert jc_parlay.MIN_PROB["crs"] == 0.0
    assert jc_parlay.MIN_PROB["hafu"] == 0.0

    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [(1, "h", 2.5, 0.40), (2, "h", 2.6, 0.38)])
    assert jc_parlay.build_plan(conn, "2026-09-20", "hhad", 5) is None, "都不到 0.55"

    _seed(conn, "2026-09-20", "crs", [(101, "s01s00", 6.0, 0.13),
                                      (102, "s02s00", 5.0, 0.12)])
    assert jc_parlay.build_plan(conn, "2026-09-20", "crs", 4) is not None, "比分无门槛"


def test_skip_reason_distinguishes_no_match_from_no_confidence():
    """「没比赛」和「有比赛但都没把握」对用户意义完全不同，必须能区分。"""
    conn = _db()

    empty = jc_parlay.skip_reason(conn, "2026-09-20", "hhad", 5)
    assert empty["candidates"] == 0

    _seed(conn, "2026-09-20", "hhad", [(1, "h", 2.5, 0.40), (2, "h", 2.6, 0.38)])
    weak = jc_parlay.skip_reason(conn, "2026-09-20", "hhad", 5)
    assert weak["candidates"] == 2 and weak["qualified"] == 0
    assert weak["min_prob"] == jc_parlay.MIN_PROB["hhad"]


def test_daily_status_reports_every_pool():
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.70), (2, "h", 1.4, 0.68),
    ])

    status = jc_parlay.daily_status(conn, "2026-09-20")

    assert {s["pool"] for s in status} == set(jc_parlay.DEFAULT_POOL_CONFIG)
    hhad = next(s for s in status if s["pool"] == "hhad")
    assert hhad["candidates"] == 2 and hhad["qualified"] == 2
    assert hhad["planned"] is False
    assert next(s for s in status if s["pool"] == "crs")["candidates"] == 0


def _live(conn, day, ids):
    from lottery_lab.models import jc_predict
    jc_predict.save_live_snapshot(conn, day, ids)


def test_pick_legs_only_keeps_on_sale_matches():
    """重算的目的是「现在还能买什么」——已停售的场次不能出现在方案里。"""
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.70), (2, "h", 1.6, 0.65), (3, "h", 1.7, 0.60),
    ])
    _live(conn, "2026-09-20", [1, 3])

    legs = jc_parlay.pick_legs(conn, "2026-09-20", "hhad", 5)

    assert [x["match_id"] for x in legs] == [1, 3]


def test_off_sale_match_does_not_take_a_slot():
    """停售的场次不能占名额 —— 否则等于把名额浪费在买不到的场上。

    1 号概率最高（0.9）但已停售，名额要给 2、3 号。
    """
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.90), (2, "h", 1.6, 0.60), (3, "h", 1.7, 0.55),
    ])
    _live(conn, "2026-09-20", [2, 3])

    legs = jc_parlay.pick_legs(conn, "2026-09-20", "hhad", 2)

    assert [x["match_id"] for x in legs] == [2, 3]


def test_empty_live_snapshot_means_nothing_bettable():
    """快照明确为空（全场停售）与「没跑过」不同：前者应当一场都不选。"""
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 6.0, 0.30), (2, "s02s00", 5.0, 0.25),
    ])
    _live(conn, "2026-09-20", [])

    assert jc_parlay.pick_legs(conn, "2026-09-20", "crs", 4) == []
    assert jc_parlay.build_plan(conn, "2026-09-20", "crs", 4) is None


def test_no_snapshot_keeps_old_behavior():
    """没有快照（历史重放、老数据）时不过滤 —— 否则会凭空改变历史结论。"""
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 6.0, 0.30), (2, "s02s00", 5.0, 0.25),
    ])

    assert len(jc_parlay.pick_legs(conn, "2026-09-20", "crs", 4)) == 2
    assert jc_parlay.skip_reason(conn, "2026-09-20", "crs", 4)["live_known"] is False


def test_skip_reason_separates_off_sale_from_no_confidence():
    """「买不到」和「没把握」必须说成不同的话。

    说成"0 场达到把握门槛"会让用户去等赔率变好，而真实原因是这几场
    已经不能下注了。
    """
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.70), (2, "h", 1.6, 0.65),
    ])

    _live(conn, "2026-09-20", [])
    why = jc_parlay.skip_reason(conn, "2026-09-20", "hhad", 5)
    assert (why["candidates"], why["off_sale"], why["bettable"]) == (2, 2, 0)
    assert why["live_known"] is True
    assert "停售" in jc_parlay.skip_text(why)

    _live(conn, "2026-09-20", [1, 2])
    why = jc_parlay.skip_reason(conn, "2026-09-20", "hhad", 5)
    assert (why["off_sale"], why["bettable"], why["qualified"]) == (0, 2, 2)

    # 在售但都不达标：这句才是真的"没把握"
    conn.execute("UPDATE jc_predictions SET prob=0.40 WHERE pool='hhad'")
    conn.commit()
    why = jc_parlay.skip_reason(conn, "2026-09-20", "hhad", 5)
    assert why["qualified"] == 0 and why["bettable"] == 2
    assert "把握门槛" in jc_parlay.skip_text(why)


def test_daily_status_carries_off_sale_and_note():
    conn = _db()
    _seed(conn, "2026-09-20", "hhad", [
        (1, "h", 1.5, 0.70), (2, "h", 1.6, 0.65),
    ])
    _live(conn, "2026-09-20", [1])

    status = {s["pool"]: s for s in jc_parlay.daily_status(conn, "2026-09-20")}

    assert status["hhad"]["off_sale"] == 1 and status["hhad"]["bettable"] == 1
    assert "停售" not in status["hhad"]["note"]      # 还有 1 场能买，方案照出
    assert "停售" in status["crs"]["note"] or "无该玩法" in status["crs"]["note"]


def test_save_plan_archives_previous_version():
    """重算会覆盖主表 —— 覆盖前必须把旧版留档，否则「改了什么」无据可查。"""
    import json as _json

    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s01s00", 6.0, 0.30), (2, "s02s00", 5.0, 0.25),
    ])
    jc_parlay.save_plan(conn, jc_parlay.build_plan(conn, "2026-09-20", "crs", 4))

    # 赔率变了 → 重算第二版
    conn.execute("UPDATE jc_predictions SET odds=9.0, prob=0.45 "
                 "WHERE match_id=1 AND pool='crs' AND method='market'")
    conn.commit()
    jc_parlay.save_plan(conn, jc_parlay.build_plan(conn, "2026-09-20", "crs", 4))

    hist = conn.execute("SELECT * FROM jc_parlay_plan_history").fetchall()
    assert len(hist) == 1 and hist[0]["revision"] == 1
    assert _json.loads(hist[0]["legs_json"])[0]["odds"] == 6.0   # 旧赔率留住了

    plan = jc_parlay.recent_plans(conn)[0]
    assert plan["revision"] == 2 and len(plan["history"]) == 1
    assert plan["history"][0]["legs"][0]["pick_label"]           # 旧版也翻好标签
    assert plan["legs"][0]["odds"] == 9.0


def test_summary_does_not_double_count_archived_versions():
    """归档的旧版不能进盈亏 —— 否则重算几次投入就翻几倍。"""
    conn = _db()
    _seed(conn, "2026-09-20", "crs", [
        (1, "s02s01", 5.0, 0.40), (2, "s01s00", 5.0, 0.35),
    ], results={1: "2:1", 2: "1:0"})
    plan = jc_parlay.build_plan(conn, "2026-09-20", "crs", 2)
    jc_parlay.save_plan(conn, plan)
    jc_parlay.score_plans(conn)
    invested_once = jc_parlay.summary(conn)["total"]["invested"]

    jc_parlay.save_plan(conn, plan)          # 重算覆盖 → 旧版进归档
    jc_parlay.score_plans(conn)              # 覆盖会清空对奖状态，重新对

    total = jc_parlay.summary(conn)["total"]
    assert total["plans"] == 1
    assert total["invested"] == invested_once


def test_recent_plans_marks_on_sale_only_for_today():
    """在售标注只给当天方案：更早的方案早已结算，整卡「已停售」是噪音。

    时序要照实模拟：方案先生成（那时都在售），之后某场才停售 —— 所以标注
    针对的是"生成后再停售"的腿，新算的方案里本来就不会有停售腿。
    """
    from datetime import date, timedelta

    conn = _db()
    today = date.today().isoformat()
    _seed(conn, today, "crs", [
        (1, "s01s00", 6.0, 0.30), (2, "s02s00", 5.0, 0.25),
    ])
    jc_parlay.save_plan(conn, jc_parlay.build_plan(conn, today, "crs", 4))
    _live(conn, today, [1])                    # 方案出完之后，2 号停售了

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _seed(conn, yesterday, "crs", [(9, "s01s00", 6.0, 0.30),
                                   (10, "s02s00", 5.0, 0.25)])
    jc_parlay.save_plan(conn, jc_parlay.build_plan(conn, yesterday, "crs", 4))

    plans = {p["plan_date"]: p for p in jc_parlay.recent_plans(conn)}
    today_legs = {l["match_id"]: l for l in plans[today]["legs"]}
    assert today_legs[1]["on_sale"] is True
    assert today_legs[2]["on_sale"] is False, "已停售的腿要被标出来"
    assert "on_sale" not in plans[yesterday]["legs"][0], "旧方案不标"


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
