"""data_health 测试。"""
import json
from lottery_lab.db import store
from lottery_lab.cli import build_parser, data_health


def test_data_health_returns_dict(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = data_health(conn)
    assert isinstance(out, dict)
    assert "matches_total" in out
    assert "odds_missing_rate" in out
    assert "unmapped_fixtures" in out
    assert "unconfirmed_aliases" in out
    # 空库下数字都该是 0
    assert out["matches_total"] == 0
    assert out["odds_missing_rate"] == 0.0
    assert out["unmapped_fixtures"] == 0


def test_backtest_plans_accepts_database_override():
    args = build_parser().parse_args(["backtest-plans", "--db", "custom.db"])

    assert args.db == "custom.db"


def test_data_health_reports_current_period_and_missing_odds():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES('26131', '2026-09-21', '2026-09-20 20:30:00', 'current')"""
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.executemany(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, odds_json)
           VALUES(?, ?, '主', '客', ?)""",
        [(pid, 1, None), (pid, 2, '{"avg":{"h":2.0,"d":3.0,"a":4.0}}')]
        + [(pid, i, None) for i in range(3, 15)],
    )
    conn.commit()

    report = data_health(conn)

    assert report["current_period"] == "26131"
    assert report["current_period_missing_odds"] == 13


def test_data_health_current_period_none_when_absent():
    conn = store.connect(":memory:")
    store.init_db(conn)

    report = data_health(conn)

    assert report["current_period"] is None
    assert report["current_period_missing_odds"] == 0
