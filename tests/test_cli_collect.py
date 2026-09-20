import json
import os
from pathlib import Path

import pytest

from football_lottery import cli
from football_lottery.collectors import sporttery
from football_lottery.db import store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


@pytest.fixture
def patched(monkeypatch, conn):
    # 仓库惯例：patch cli._connect（见 tests/test_check_draw.py）。
    # 不能依赖 _load_config —— cli.main 会去读真实 config.yaml 并写真实库。
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    monkeypatch.setattr(
        sporttery, "fetch_current_period",
        lambda: {"period_no": "26131", "sale_end": "2026-09-20 20:30:00",
                 "draw_time": "2026-09-21 14:00:00"},
    )
    monkeypatch.setattr(
        sporttery, "fetch_period_detail",
        lambda period_no: _fixture("sporttery_bydraw_26131.json")["value"],
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)
    return conn


def test_collect_period_writes_current_period(patched, capsys):
    rc = cli.main(["collect-period"])
    assert rc == 0

    row = patched.execute("SELECT * FROM periods WHERE period_no='26131'").fetchone()
    assert row["status"] == "current"
    assert row["sale_end"] == "2026-09-20 20:30:00"
    assert patched.execute(
        "SELECT COUNT(*) c FROM period_matches WHERE period_id=?", (row["id"],)
    ).fetchone()["c"] == 14

    out = json.loads(capsys.readouterr().out)
    assert out["period_no"] == "26131"
    assert out["fixtures"] == 14


def test_collect_period_reports_when_no_onsale(monkeypatch, conn):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    monkeypatch.setattr(sporttery, "fetch_current_period", lambda: None)
    assert cli.main(["collect-period"]) == 2


def test_collect_period_reports_collector_error(monkeypatch, conn, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)

    def _boom():
        raise sporttery.CollectorError("接口错误 P0001: 参数不合法", error_code="P0001")

    monkeypatch.setattr(sporttery, "fetch_current_period", _boom)
    assert cli.main(["collect-period"]) == 2
    assert "P0001" in capsys.readouterr().err


def test_singleton_lock_takes_over_stale_lock(tmp_path, monkeypatch):
    from football_lottery import daemon_ctl

    lock = tmp_path / "daemon.lock"
    lock.write_text("999999", encoding="utf-8")   # 几乎必然不存在的 PID
    monkeypatch.setattr(daemon_ctl, "pid_alive", lambda pid: False)

    assert daemon_ctl.acquire_singleton(lock) is True, "持有者已死，应接管陈旧锁"
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_singleton_lock_rejects_when_holder_alive(tmp_path, monkeypatch):
    from football_lottery import daemon_ctl

    lock = tmp_path / "daemon.lock"
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(daemon_ctl, "pid_alive", lambda pid: pid == 4242)

    assert daemon_ctl.acquire_singleton(lock) is False


def test_is_running_ignores_dead_pid(tmp_path, monkeypatch):
    from football_lottery import daemon_ctl

    lock = tmp_path / "daemon.lock"
    lock.write_text("31337", encoding="utf-8")
    monkeypatch.setattr(daemon_ctl, "LOCK_PATH", lock)
    monkeypatch.setattr(daemon_ctl, "pid_alive", lambda pid: False)

    running, pid = daemon_ctl.is_running()

    assert running is False
    assert pid == 31337


class _FakeDatetime:
    """打桩 datetime，只控制 now()。"""

    def __init__(self, hour):
        self._hour = hour

    def now(self):
        from datetime import datetime as _dt
        return _dt(2026, 9, 20, self._hour, 0, 0)

    def __getattr__(self, name):        # fromisoformat 等仍走真实实现
        from datetime import datetime as _dt
        return getattr(_dt, name)


def test_daily_hook_skips_before_configured_hour(monkeypatch, conn):
    called = []
    monkeypatch.setattr(cli, "datetime", _FakeDatetime(cli.DAILY_JC_HOUR - 1))
    monkeypatch.setattr(cli, "cmd_daily_jc", lambda *a, **k: called.append(1))

    cli._maybe_run_daily_jc(conn, {})

    assert called == [], "早于 DAILY_JC_HOUR 不应触发"


def test_daily_hook_skips_when_plan_exists(monkeypatch, conn):
    called = []
    monkeypatch.setattr(cli, "datetime", _FakeDatetime(cli.DAILY_JC_HOUR + 1))
    monkeypatch.setattr(cli, "cmd_daily_jc", lambda *a, **k: called.append(1))
    conn.execute(
        """INSERT INTO jc_matches(match_id, match_date) VALUES(1, '2026-09-20')""")
    conn.execute(
        """INSERT INTO jc_parlay_plans(plan_date, pool, n_legs, unit, min_combo,
                                       legs_json, bets_json)
           VALUES('2026-09-20', 'crs', 2, 2, 2, '[]', '[]')""")
    conn.commit()

    cli._maybe_run_daily_jc(conn, {})

    assert called == [], "当天已有方案不应重复生成"


def test_daily_hook_runs_once_when_no_plan(monkeypatch, conn):
    called = []
    monkeypatch.setattr(cli, "datetime", _FakeDatetime(cli.DAILY_JC_HOUR + 1))
    monkeypatch.setattr(cli, "cmd_daily_jc", lambda *a, **k: called.append(1) or 0)

    cli._maybe_run_daily_jc(conn, {})

    assert called == [1], "当天无方案时应触发一次"


def test_daily_hook_does_not_raise_on_failure(monkeypatch, conn, capsys):
    def _boom(*a, **k):
        raise RuntimeError("接口挂了")

    monkeypatch.setattr(cli, "datetime", _FakeDatetime(cli.DAILY_JC_HOUR + 1))
    monkeypatch.setattr(cli, "cmd_daily_jc", _boom)

    cli._maybe_run_daily_jc(conn, {})       # 不应抛出

    assert "接口挂了" in capsys.readouterr().err


def test_collect_draws_writes_history(monkeypatch, conn, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    page = _fixture("sporttery_history_90.json")["value"]
    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: page if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    assert cli.main(["collect-draws", "--years", "4"]) == 0

    # fixture 共 30 期，含 '*' 的期次也入库（官方按全选计算）
    assert conn.execute("SELECT COUNT(*) c FROM draw_results").fetchone()["c"] == 30
    out = json.loads(capsys.readouterr().out)
    assert out["periods_saved"] == 30
    assert out["skipped"] == 0
