"""采集控制台：状态查询与启停接口。"""
import pytest

from lottery_lab import daemon_ctl
from lottery_lab.db import store
from lottery_lab.web import app as web_app


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    # 造两条快照：一场只采过 1 次、一场采过 2 次（有变化）
    c.execute(
        """INSERT INTO periods(period_no, draw_date, status)
           VALUES('26131', '2026-09-21', 'current')"""
    )
    pid = c.execute("SELECT id FROM periods").fetchone()["id"]
    for seq in (1, 2):
        c.execute(
            """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn)
               VALUES(?, ?, '主', '客')""",
            (pid, seq),
        )
    pm1, pm2 = [r["id"] for r in c.execute(
        "SELECT id FROM period_matches ORDER BY seq")]
    c.executemany(
        """INSERT INTO odds_snapshots(period_match_id, source, captured_at, h, d, a)
           VALUES(?, 'jc', ?, 2.0, 3.0, 4.0)""",
        [(pm1, "2026-09-20T10:00:00.000000"),
         (pm2, "2026-09-20T10:00:00.000000"),
         (pm2, "2026-09-20T10:10:00.000000")],
    )
    c.commit()
    return c


def test_status_reports_running_state(conn, monkeypatch):
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)
    monkeypatch.setattr(daemon_ctl, "is_running", lambda: (True, 4242))
    monkeypatch.setattr(daemon_ctl, "tail_log", lambda n=40: ["line1", "line2"])

    got = web_app.api_daemon_status()

    assert got["running"] is True
    assert got["pid"] == 4242
    assert got["interval"] == 600
    assert got["log_lines"] == ["line1", "line2"]


def test_status_snapshot_summary_counts_changes(conn, monkeypatch):
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_daemon_status()
    summary = got["snapshots"]

    assert summary["total"] == 3
    assert summary["periods_with_changes"] == 1     # 只有第二场采过两次
    assert summary["last_captured_at"] == "2026-09-20T10:10:00.000000"


def test_start_is_noop_when_already_running(monkeypatch):
    monkeypatch.setattr(daemon_ctl, "is_running", lambda: (True, 777))

    got = daemon_ctl.start()

    assert got["ok"] is True
    assert got["started"] is False
    assert got["pid"] == 777
    assert "已在运行" in got["message"]


def test_stop_reports_noop_when_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon_ctl, "is_running", lambda: (False, None))
    monkeypatch.setattr(daemon_ctl, "LOCK_PATH", tmp_path / "no-such.lock")

    got = daemon_ctl.stop()

    assert got["ok"] is True
    assert got["stopped"] is False
    assert "未在运行" in got["message"]


def test_stop_clears_stale_lock(monkeypatch, tmp_path):
    lock = tmp_path / "daemon.lock"
    lock.write_text("31337", encoding="utf-8")
    monkeypatch.setattr(daemon_ctl, "is_running", lambda: (False, 31337))
    monkeypatch.setattr(daemon_ctl, "LOCK_PATH", lock)

    got = daemon_ctl.stop()

    assert got["ok"] is True
    assert got["stopped"] is False
    assert "残留锁" in got["message"]
    assert not lock.exists()
