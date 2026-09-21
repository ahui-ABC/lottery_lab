import json
import sqlite3

import pytest

from lottery_lab.db import store
from lottery_lab.models import lottery_track as lt
from lottery_lab.models.lottery_predict import BET_PRICE


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def _predict(conn, issue, strategy, bets=5, prize=None):
    """插一条预测。bets 用真实结构的注单列表，注数就是它的长度。"""
    conn.execute(
        """INSERT INTO lottery_prediction(lottery, target_issue, strategy, bets,
                                          created_at, prize)
           VALUES('p3',?,?,?,?,?)""",
        (issue, strategy,
         json.dumps([{"digits": ["1", "2", "3"]}] * bets),
         "2026-09-21T10:00:00", prize))
    conn.commit()


def _draw(conn, issue):
    conn.execute(
        "INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes) "
        "VALUES('p3',?,'2026-01-01',?,?)",
        (issue, json.dumps({"digits": ["1", "2", "3"]}), "[]"))
    conn.commit()


def test_summary_only_counts_scored_predictions():
    """未开奖的不能掺进盈亏 —— 算成 0 返还会让盈亏随「还有几期没开」乱跳。"""
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=1040)
    _predict(conn, "2026002", "hot", bets=5, prize=None)      # 待开奖

    out = lt.summary(conn, "p3")
    hot = next(s for s in out["strategies"] if s["strategy"] == "hot")
    assert hot["issues"] == 1
    assert hot["invested"] == 5 * BET_PRICE
    assert hot["returned"] == 1040
    assert hot["profit"] == 1040 - 10
    assert out["pending"] == 1


def test_summary_invested_uses_actual_bet_count():
    """投入按**实际发出的注数**算。去重后少发了还按请求注数算，投入会虚增。"""
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=2, prize=0)         # 只发出 2 注
    out = lt.summary(conn, "p3")
    hot = next(s for s in out["strategies"] if s["strategy"] == "hot")
    assert hot["bets"] == 2
    assert hot["invested"] == 2 * BET_PRICE


def test_summary_return_rate_and_win_issues():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=0)         # 未中
    _predict(conn, "2026002", "hot", bets=5, prize=1040)      # 中一注直选
    _predict(conn, "2026003", "hot", bets=5, prize=0)
    out = lt.summary(conn, "p3")
    hot = next(s for s in out["strategies"] if s["strategy"] == "hot")
    assert hot["issues"] == 3
    assert hot["win_issues"] == 1
    assert hot["win_rate"] == pytest.approx(1 / 3)
    assert hot["return_rate"] == pytest.approx(1040 / 30)


def test_summary_groups_by_strategy():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=1040)
    _predict(conn, "2026001", "cold", bets=5, prize=0)
    out = lt.summary(conn, "p3")
    labels = {s["strategy"]: s for s in out["strategies"]}
    assert set(labels) == {"hot", "cold"}
    assert labels["hot"]["profit"] == 1040 - 10
    assert labels["cold"]["profit"] == -10
    assert labels["cold"]["return_rate"] == 0.0


def test_summary_empty_returns_no_division_by_zero():
    out = lt.summary(_conn(), "p3")
    assert out["strategies"] == []
    assert out["pending"] == 0


def test_timeline_accumulates_in_issue_order():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=1040)      # +1030
    _predict(conn, "2026002", "hot", bets=5, prize=0)         # -10
    _predict(conn, "2026003", "hot", bets=5, prize=None)      # 未开奖，不入曲线
    for issue in ("2026001", "2026002", "2026003"):
        _draw(conn, issue)

    points = lt.timeline(conn, "p3", "hot")
    assert [p["issue"] for p in points] == ["2026001", "2026002"]
    assert points[0]["cumulative"] == 1030
    assert points[1]["cumulative"] == 1020
    assert points[0]["date"] == "2026-01-01"


def test_timeline_is_empty_without_scored_predictions():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=None)
    assert lt.timeline(conn, "p3", "hot") == []


def test_recent_marks_unscored_as_pending():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=1040)
    _predict(conn, "2026002", "hot", bets=5, prize=None)
    _draw(conn, "2026001")

    rows = lt.recent(conn, "p3")
    assert [r["issue"] for r in rows] == ["2026002", "2026001"]   # 新的在前
    pending = next(r for r in rows if r["issue"] == "2026002")
    assert pending["strategies"][0]["pending"] is True
    assert pending["strategies"][0]["profit"] is None
    scored = next(r for r in rows if r["issue"] == "2026001")
    assert scored["strategies"][0]["profit"] == 1040 - 10
    assert scored["numbers"] == {"digits": ["1", "2", "3"]}


def test_recent_respects_limit():
    conn = _conn()
    for i in range(1, 6):
        _predict(conn, f"2026{i:03d}", "hot", bets=1, prize=0)
    rows = lt.recent(conn, "p3", limit=2)
    assert [r["issue"] for r in rows] == ["2026005", "2026004"]


def test_overview_covers_all_five_lotteries():
    conn = _conn()
    _predict(conn, "2026001", "hot", bets=5, prize=0)
    _draw(conn, "2026001")

    out = lt.overview(conn)
    assert [o["lottery"] for o in out] == ["dlt", "ssq", "p3", "p5", "3d"]
    p3 = next(o for o in out if o["lottery"] == "p3")
    assert p3["latest"]["issue"] == "2026001"
    assert p3["scored_issues"] == 1
    assert p3["strategies"][0]["profit"] == -10
    # 没有数据的彩种不能炸
    dlt = next(o for o in out if o["lottery"] == "dlt")
    assert dlt["latest"] is None and dlt["strategies"] == []
