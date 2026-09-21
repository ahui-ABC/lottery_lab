"""竞彩历史回测测试。"""
import json

from lottery_lab.db import store
from lottery_lab.models import jc_backtest


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _seed(conn, match_id, series, result_had):
    """写入一场比赛：赔率序列 + 赛果。"""
    conn.execute(
        """INSERT INTO jc_matches(match_id, match_date, home_team, away_team, result_had)
           VALUES(?, '2021-01-01', 'A', 'B', ?)""", (match_id, result_had))
    for seq, (h, d, a) in enumerate(series):
        payload = {"h": str(h), "d": str(d), "a": str(a)}
        conn.execute(
            """INSERT INTO jc_odds_history(match_id, pool, seq, update_date, update_time,
                                           goal_line, odds_json)
               VALUES(?, 'had', ?, '2021-01-01', ?, '', ?)""",
            (match_id, seq, f"1{seq}:00:00", json.dumps(payload)))
    conn.commit()


def test_backtest_computes_hit_rate_and_roi():
    conn = _db()
    # 两场：赔率稳定，主胜概率高，都开主胜
    _seed(conn, 1, [(1.5, 4.0, 6.0), (1.5, 4.0, 6.0)], "H")
    _seed(conn, 2, [(1.6, 4.0, 5.5), (1.6, 4.0, 5.5)], "H")

    result = jc_backtest.run(conn, batch_size=100)

    market = result["had"]["market"]
    assert market["n"] == 2
    assert market["hits"] == 2
    assert market["hit_rate"] == 1.0
    assert market["invested"] == 2.0
    assert market["returned"] == 1.5 + 1.6
    # ROI = (3.1 - 2.0) / 2.0
    assert abs(market["roi"] - 0.55) < 1e-9


def test_backtest_counts_misses():
    conn = _db()
    _seed(conn, 1, [(1.5, 4.0, 6.0), (1.5, 4.0, 6.0)], "A")   # 开客胜，押主胜不中

    result = jc_backtest.run(conn, batch_size=100)

    market = result["had"]["market"]
    assert market["hits"] == 0
    assert market["roi"] == -1.0            # 全亏


def test_backtest_skips_matches_without_result():
    conn = _db()
    _seed(conn, 1, [(1.5, 4.0, 6.0)], None)   # 无赛果

    result = jc_backtest.run(conn, batch_size=100)

    assert "had" not in result or result["had"].get("market", {}).get("n", 0) == 0


def test_compare_to_baseline_reports_edge():
    result = {"had": {
        "market": {"n": 10, "hits": 5, "hit_rate": 0.5, "invested": 10.0,
                   "returned": 9.0, "roi": -0.1},
        "trend": {"n": 10, "hits": 4, "hit_rate": 0.4, "invested": 10.0,
                  "returned": 11.0, "roi": 0.1},
    }}

    rows = {r["method"]: r for r in jc_backtest.compare_to_baseline(result)}

    # 基线是 -11.4%（随机投注的期望），roi 高于它才算有增量
    assert rows["market"]["edge_vs_random"] > 0        # -0.1 > -0.114
    assert rows["trend"]["edge_vs_random"] > rows["market"]["edge_vs_random"]
