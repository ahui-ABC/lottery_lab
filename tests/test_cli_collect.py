import json
import os
from pathlib import Path

import pytest

from lottery_lab import cli
from lottery_lab.collectors import sporttery
from lottery_lab.db import store

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
    from lottery_lab import daemon_ctl

    lock = tmp_path / "daemon.lock"
    lock.write_text("999999", encoding="utf-8")   # 几乎必然不存在的 PID
    monkeypatch.setattr(daemon_ctl, "pid_alive", lambda pid: False)

    assert daemon_ctl.acquire_singleton(lock) is True, "持有者已死，应接管陈旧锁"
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_singleton_lock_rejects_when_holder_alive(tmp_path, monkeypatch):
    from lottery_lab import daemon_ctl

    lock = tmp_path / "daemon.lock"
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(daemon_ctl, "pid_alive", lambda pid: pid == 4242)

    assert daemon_ctl.acquire_singleton(lock) is False


def test_is_running_ignores_dead_pid(tmp_path, monkeypatch):
    from lottery_lab import daemon_ctl

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


def test_predict_jc_records_live_snapshot(monkeypatch, conn, patched, capsys):
    """在售列表要落库：重算方案靠它区分「还能买」与「已停售」。"""
    from datetime import date

    from lottery_lab.models import jc_predict

    payload = {"value": {"matchInfoList": [
        {"subMatchList": [{"matchId": "2041001", "matchDate": "2026-09-20 20:00:00"},
                          {"matchId": "2041002", "matchDate": "2026-09-20 22:00:00"}]}]}}
    monkeypatch.setattr(sporttery, "fetch_jc_odds", lambda: payload)
    monkeypatch.setattr(sporttery, "fetch_fixed_bonus", lambda mid: {"oddsHistory": []})

    cli.cmd_predict_jc(_args(), {})

    assert jc_predict.live_match_ids(conn, date.today().isoformat()) == {2041001, 2041002}


def test_predict_jc_records_empty_snapshot_on_rest_day(monkeypatch, conn, patched):
    """休赛日也要写 '[]'：空快照与「没跑过」是两回事，前者应过滤掉全部候选。"""
    from datetime import date

    from lottery_lab.models import jc_predict

    monkeypatch.setattr(sporttery, "fetch_jc_odds", lambda: {"value": {}})

    cli.cmd_predict_jc(_args(), {})

    assert jc_predict.live_match_ids(conn, date.today().isoformat()) == set()


def test_plan_jc_reports_counts_when_nothing_qualified(monkeypatch, conn, patched,
                                                       capsys):
    """跳过原因要带上计数，页面/CLI 才能说出「买不到」还是「没把握」。"""
    conn.execute("INSERT INTO jc_matches(match_id, match_date, home_team, away_team)"
                 " VALUES(1, '2026-09-20', 'H', 'A')")
    conn.execute("""INSERT INTO jc_predictions(match_id, predicted_on, pool, method,
                                               pick, odds, prob)
                    VALUES(1, '2026-09-20', 'hhad', 'market', 'h', 2.5, 0.40)""")
    conn.commit()

    rc = cli.cmd_plan_jc(_args(date="2026-09-20", pool="hhad"), {})
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    skipped = out["plans"][0]
    assert skipped["candidates"] == 1 and skipped["off_sale"] == 0
    assert "把握门槛" in skipped["skipped"]


def _args(**kw):
    """predict-jc / plan-jc 的无参调用。date=None 走实时分支（重放走 --date）。"""
    from types import SimpleNamespace
    base = dict(date=None, workers=2, delay=0, pool=None,
                unit=None, min_combo=None)
    base.update(kw)
    return SimpleNamespace(**base)


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


# ---- 胜负彩开奖自动补齐 ---------------------------------------------------

def _football_env(tmp_path, monkeypatch, periods):
    """periods: [(period_no, draw_date, 是否已有开奖记录)]"""
    from lottery_lab.db import store
    from datetime import date, timedelta

    cfg = {"db_path": str(tmp_path / "t.db")}
    conn = store.connect(cfg["db_path"])
    store.init_db(conn)
    for pn, draw_date, has_draw in periods:
        conn.execute("INSERT INTO periods(period_no, status, draw_date, sale_end) "
                     "VALUES(?,?,?,?)", (pn, "historical", draw_date, draw_date))
        pid = conn.execute("SELECT id FROM periods WHERE period_no=?", (pn,)).fetchone()["id"]
        if has_draw:
            conn.execute("INSERT INTO draw_results(period_id, results_json, prizes_json) "
                         "VALUES(?,?,?)", (pid, "3,3,3,3,3,3,3,3,3,3,3,3,3,3", "{}"))
    conn.commit()
    conn.close()
    return cfg


def test_maybe_run_daily_football_fires_when_a_period_is_missing_its_draw(
        tmp_path, monkeypatch, capsys):
    """核心回归：开奖日已过却没有开奖记录时，守护进程必须去补。

    这就是「胜负彩开奖一直不出来」的根因 —— 这一环以前只存在于手动按钮里。
    """
    from datetime import date, timedelta
    from lottery_lab import cli
    from lottery_lab.db import store

    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    cfg = _football_env(tmp_path, monkeypatch,
                        [("26131", yesterday, False), ("26130", yesterday, True)])

    called = []
    monkeypatch.setattr(cli, "cmd_collect_draws",
                        lambda args, cfg: called.append("draws") or 0)
    monkeypatch.setattr(cli, "cmd_check_draw",
                        lambda args, cfg: called.append("check") or 0)

    conn = store.connect(cfg["db_path"])
    cli._maybe_run_daily_football(conn, cfg)
    conn.close()

    assert called == ["draws", "check"]
    assert "已过开奖日" in capsys.readouterr().out


def test_maybe_run_daily_football_stays_quiet_once_everything_is_in(
        tmp_path, monkeypatch, capsys):
    """已开奖且没有方案时不该发网络请求。

    注意判据是「有没有发请求」而不是「有没有输出」—— 对奖是纯本地的，
    会在有新开奖时正常跑并打印，那是它该做的事。
    """
    from datetime import date, timedelta
    from lottery_lab import cli
    from lottery_lab.db import store

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cfg = _football_env(tmp_path, monkeypatch, [("26131", yesterday, True)])

    called = []
    monkeypatch.setattr(cli, "cmd_collect_draws",
                        lambda args, cfg: called.append("draws") or 0)

    conn = store.connect(cfg["db_path"])
    cli._maybe_run_daily_football(conn, cfg)
    conn.close()

    assert called == [], "开奖已在库里，不该再发网络请求"


def test_maybe_run_daily_football_ignores_ancient_gaps(
        tmp_path, monkeypatch, capsys):
    """超出回看窗口的历史缺口不再重试 —— 那多半是数据源缺档，
    否则每一轮（10 分钟）都要白跑一次全量采集。"""
    from datetime import date, timedelta
    from lottery_lab import cli
    from lottery_lab.db import store

    old = (date.today() - timedelta(days=cli.FOOTBALL_LOOKBACK_DAYS + 5)).isoformat()
    cfg = _football_env(tmp_path, monkeypatch, [("20001", old, False)])

    called = []
    monkeypatch.setattr(cli, "cmd_collect_draws",
                        lambda args, cfg: called.append("draws") or 0)

    conn = store.connect(cfg["db_path"])
    cli._maybe_run_daily_football(conn, cfg)
    conn.close()

    assert called == []


def test_maybe_run_daily_football_scores_after_the_draw_was_已_earlier(
        tmp_path, monkeypatch):
    """开奖记录已存在时仍要跑对奖。

    这是「中奖注数恒为 0」的根因：对奖原先只在采集开奖的同一分支里跑，
    而开奖若由别处（手动命令 / 采集页按钮）补齐，对奖就永远轮不到。
    没中奖和对过奖但没中在库里长得一样，所以判据必须是「近期开过奖」，
    不能是「有没有 winnings 记录」。
    """
    from datetime import date, timedelta
    from lottery_lab import cli
    from lottery_lab.db import store

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cfg = _football_env(tmp_path, monkeypatch, [("26131", yesterday, True)])
    conn = store.connect(cfg["db_path"])
    pid = conn.execute("SELECT id FROM periods WHERE period_no='26131'").fetchone()["id"]
    conn.execute("INSERT INTO plans(period_id, game_type, objective, budget, "
                 "notes_count, legs_json, created_at) VALUES(?,?,?,?,?,?,?)",
                 (pid, "r9", "first", 32, 16, "[]", "2026-09-20 08:00:00"))
    conn.commit()
    conn.close()

    called = []
    monkeypatch.setattr(cli, "cmd_collect_draws",
                        lambda args, cfg: called.append("draws") or 0)
    monkeypatch.setattr(cli, "cmd_check_draw",
                        lambda args, cfg: called.append("check") or 0)

    conn = store.connect(cfg["db_path"])
    cli._maybe_run_daily_football(conn, cfg)
    conn.close()

    assert called == ["check"], "开奖已在库里，就不该再发网络请求；但有方案就得对奖"
