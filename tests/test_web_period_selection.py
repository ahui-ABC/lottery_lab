import json

import pytest

from football_lottery.db import store
from football_lottery.web import app as web_app


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


def _add_period(conn, period_no, status, sale_end, draw_date):
    conn.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES(?, ?, ?, ?)""",
        (period_no, draw_date, sale_end, status),
    )
    pid = conn.execute(
        "SELECT id FROM periods WHERE period_no=?", (period_no,)
    ).fetchone()["id"]
    conn.executemany(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, league_cn)
           VALUES(?, ?, '主队', '客队', '英超')""",
        [(pid, i) for i in range(1, 15)],
    )
    conn.commit()
    return pid


def test_prefers_onsale_current_period(conn, monkeypatch):
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["period_no"] == "26131"
    assert got["is_current"] is True
    assert got["period_status"] == "current"
    assert got["sale_end"] == "2099-01-01 20:30:00"


def test_expired_current_is_not_treated_as_current(conn, monkeypatch):
    """核心回归：销售已截止的 current 行不得再被当作当期。"""
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26129", "current", "2020-01-01 20:00:00", "2020-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["is_current"] is False
    assert got["period_no"] == "26080"  # 退到最新 historical


def test_falls_back_to_latest_historical_when_no_current(conn, monkeypatch):
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26129", "historical", "2026-09-18 22:00:00", "2026-09-19")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["period_no"] == "26129"
    assert got["is_current"] is False
    assert got["period_status"] == "historical"


def test_period_payload_exposes_league_cn(conn, monkeypatch):
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["matches"][0]["league_cn"] == "英超"


def test_no_periods_returns_warning(conn, monkeypatch):
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    resp = web_app.api_period_current()
    # 无期次时该接口返回 JSONResponse（status_code=200）而非普通 dict
    payload = json.loads(resp.body)

    assert payload["period_no"] is None
    assert "warning" in payload


def test_predict_suppresses_fake_uniform_distribution(conn, monkeypatch):
    """无映射、无赔率时不得回传均匀 fused（否则页面显示假的 33.3%）。"""
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_predict()

    first = got["matches"][0]
    assert first["mapped"] is False
    assert first["fused"] is None
    assert first["has_prediction"] is False
