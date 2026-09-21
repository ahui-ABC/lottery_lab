"""后台任务运行器（jobs.py）的测试。全程不打桩网络，也不真的起进程。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from football_lottery import cli, jobs


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    """把任务目录挪到 tmp，并把 Popen / pid_alive 换成假的。"""
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "GLOBAL_LOCK", tmp_path / "running.lock")
    monkeypatch.setattr(jobs, "_RUNNING", {})
    monkeypatch.setattr(jobs, "_RESULTS", {})
    monkeypatch.setattr(jobs.daemon_ctl, "PROJECT_ROOT", tmp_path)
    return tmp_path


class _FakeProc:
    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _fake_popen(monkeypatch, proc=None, recorder=None):
    proc = proc or _FakeProc()

    def _popen(cmd, **kwargs):
        if recorder is not None:
            recorder.append((cmd, kwargs))
        return proc

    monkeypatch.setattr(jobs.subprocess, "Popen", _popen)
    return proc


# ---- 注册表 -----------------------------------------------------------------

@pytest.mark.parametrize("spec", jobs.JOBS, ids=lambda s: s.key)
def test_every_job_cli_is_parseable(spec):
    """每个任务的命令行都必须能被 argparse 解析。

    写错子命令名或参数名时任务会一直失败，但页面上只看到「已结束」——
    这条断言把那种沉默的坏掉挡在前面。
    """
    args = cli.build_parser().parse_args(list(spec.cli))
    assert args.cmd == spec.cli[0]


def test_job_keys_are_unique_and_groups_known():
    keys = [s.key for s in jobs.JOBS]
    assert len(keys) == len(set(keys))
    assert {s.group for s in jobs.JOBS} <= set(jobs.GROUP_ORDER)


def test_grouped_follows_sidebar_order():
    groups = [g for g, _ in jobs.grouped()]
    assert groups == list(jobs.GROUP_ORDER)
    assert all(specs for _, specs in jobs.grouped())


# ---- 启动 -------------------------------------------------------------------

def test_start_writes_lock_and_spawns_unbuffered(job_env, monkeypatch):
    calls = []
    _fake_popen(monkeypatch, recorder=calls)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)

    out = jobs.start("collect-odds")
    assert out["ok"] is True

    lock = json.loads(jobs.GLOBAL_LOCK.read_text(encoding="utf-8"))
    assert lock["key"] == "collect-odds"
    assert lock["pid"] == 4242
    assert lock["started_at"]

    cmd, kwargs = calls[0]
    assert "-u" in cmd, "-u 不能少：否则重定向到文件的日志会被块缓冲住，页面看不到实时输出"
    assert cmd[-1] == "collect-odds"
    assert str(kwargs["cwd"]) == str(job_env)


def test_start_refuses_unknown_key(job_env):
    assert jobs.start("nope")["ok"] is False


def test_start_refuses_while_another_job_runs(job_env, monkeypatch):
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    assert jobs.start("collect-odds")["ok"] is True

    out = jobs.start("daily-lottery")
    assert out["ok"] is False
    assert "已有任务在跑" in out["message"]


def test_start_truncates_previous_log(job_env, monkeypatch):
    jobs.log_path("collect-odds").write_text("上一次运行的旧输出", encoding="utf-8")
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")
    # open(..., "w") 已经把它清空了；写进去的是本次运行的输出
    assert jobs.log_path("collect-odds").read_text(encoding="utf-8") == ""


# ---- 状态 -------------------------------------------------------------------

def test_stale_lock_is_not_running(job_env, monkeypatch):
    """锁在但进程已死 → 不算运行中，且残留锁会被清掉。"""
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")

    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: False)
    st = jobs.status()
    assert st["running_key"] is None
    assert not jobs.GLOBAL_LOCK.exists()


def test_finished_job_is_recorded_and_lock_cleared(job_env, monkeypatch):
    proc = _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")

    proc.returncode = 0                     # 自己跑完了
    st = jobs.status()
    assert st["running_key"] is None
    assert not jobs.GLOBAL_LOCK.exists()
    entry = next(j for j in st["jobs"] if j["key"] == "collect-odds")
    assert entry["ok"] is True
    assert entry["finished_at"]


def test_failed_job_records_not_ok(job_env, monkeypatch):
    proc = _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")
    proc.returncode = 2
    entry = next(j for j in jobs.status()["jobs"] if j["key"] == "collect-odds")
    assert entry["ok"] is False


def test_finished_job_still_returns_its_log(job_env, monkeypatch):
    """跑完之后仍要能拿到日志 —— 失败时恰恰是最需要它的时候。"""
    proc = _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")
    jobs.log_path("collect-odds").write_text("抓取失败：连接超时\n", encoding="utf-8")

    proc.returncode = 1
    entry = next(j for j in jobs.status()["jobs"] if j["key"] == "collect-odds")
    assert entry["ok"] is False
    assert entry["log_tail"] == ["抓取失败：连接超时"]


def test_running_job_reports_elapsed_and_log(job_env, monkeypatch):
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-lottery")
    jobs.log_path("collect-lottery").write_text("正在抓第 3 页\n", encoding="utf-8")

    entry = next(j for j in jobs.status()["jobs"] if j["key"] == "collect-lottery")
    assert entry["running"] is True
    assert entry["pid"] == 4242
    assert entry["elapsed_sec"] >= 0
    assert entry["log_tail"] == ["正在抓第 3 页"]
    assert entry["ok"] is None


def test_status_lists_all_jobs_even_before_any_run(job_env):
    st = jobs.status()
    assert {j["key"] for j in st["jobs"]} == {s.key for s in jobs.JOBS}
    assert st["running_key"] is None
    assert all(j["log_tail"] == [] for j in st["jobs"])


# ---- 停止 -------------------------------------------------------------------

def test_stop_uses_taskkill_with_tree_flag(job_env, monkeypatch):
    """必须带 /T：pythonw 只是 launcher，真正干活的是它派生的子进程。"""
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")

    calls = []
    monkeypatch.setattr(jobs.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(jobs.sys, "platform", "win32")

    out = jobs.stop("collect-odds")
    assert out["ok"] is True
    assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert not jobs.GLOBAL_LOCK.exists()
    assert jobs.status()["running_key"] is None


def test_stop_refuses_when_not_running(job_env):
    assert jobs.stop("collect-odds")["ok"] is False
    assert jobs.stop("nope")["ok"] is False


def test_stop_refuses_a_different_job(job_env, monkeypatch):
    _fake_popen(monkeypatch)
    monkeypatch.setattr(jobs.daemon_ctl, "pid_alive", lambda pid: True)
    jobs.start("collect-odds")
    out = jobs.stop("daily-jc")
    assert out["ok"] is False
    assert "没有在运行" in out["message"]


# ---- 日志尾部 ---------------------------------------------------------------

def test_tail_log_returns_last_lines_and_skips_blank(job_env):
    jobs.log_path("daily-jc").write_text(
        "\n".join(f"第 {i} 行" for i in range(1, 11) if i != 5) + "\n\n",
        encoding="utf-8")
    lines = jobs.tail_log("daily-jc", lines=3)
    assert lines == ["第 8 行", "第 9 行", "第 10 行"]


def test_tail_log_on_missing_file(job_env):
    assert jobs.tail_log("never-ran") == []


def test_elapsed_survives_a_corrupt_timestamp(job_env):
    assert jobs._elapsed_sec("坏掉的时间戳") is None
    assert jobs._elapsed_sec(None) is None
    old = (datetime.now() - timedelta(seconds=90)).isoformat(timespec="seconds")
    assert jobs._elapsed_sec(old) >= 89
