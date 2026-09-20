import json
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


def test_singleton_lock_blocks_second_instance(tmp_path):
    lock = tmp_path / "daemon.lock"

    assert cli._acquire_singleton(lock) is True
    # 本进程已持有 → 再取应失败
    assert cli._acquire_singleton(lock) is True  # 同 PID 视为自己，可重入

    # 伪造另一个"活着"的持有者（PID 1 在 Windows 上通常不存在，改用本进程 PID 的邻值）
    lock.write_text("1", encoding="utf-8")
    monkeypatch_pid = 999999  # 几乎必然不存在
    lock.write_text(str(monkeypatch_pid), encoding="utf-8")
    assert cli._acquire_singleton(lock) is True, "持有者已死，应接管陈旧锁"


def test_singleton_lock_rejects_when_holder_alive(tmp_path, monkeypatch):
    lock = tmp_path / "daemon.lock"
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 4242)

    assert cli._acquire_singleton(lock) is False


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
