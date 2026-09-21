"""一次性后台任务：从 Web 页面触发耗时的采集命令。

和 `daemon_ctl` 的区别：那个管的是**跑不完的常驻轮询**（竞彩赔率守护进程），
这个管的是**跑完就结束的一次性动作**（抓当期对阵、历史回填、每日一条龙…）。
进程模型照抄前者（pythonw 分离进程 + 锁文件 + 日志文件），新增的是
「可参数化跑任意 CLI 子命令」与「全局单任务闸门」。

**单任务闸门不是洁癖**：曾经用 6 并发无间隔抓第三方站点，约 1000 次请求后
把整个 IP 被 WAF 封了。手动任务必须串行，避免在用户点了三四个按钮时并发打爆站点。

进程模型上的两个坑（前者是 daemon_ctl 里已经踩过的）：

1. venv 的 `pythonw.exe` 是 launcher，真正的解释器是它派生的**子进程**。
   所以停止一律用 `taskkill /F /T`（`/T` 连子进程一起杀），不能只杀 Popen.pid。
2. stdout 重定向到文件时 Python 默认块缓冲，不加 `-u` 的话页面上的实时日志
   会一直空着，直到进程结束才一次性出现。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lottery_lab import daemon_ctl

JOBS_DIR = daemon_ctl.PROJECT_ROOT / "data" / "logs" / "jobs"
GLOBAL_LOCK = JOBS_DIR / "running.lock"
LOG_TAIL_LINES = 80
GROUP_ORDER = ("胜负彩", "竞彩", "数字彩")


@dataclass(frozen=True)
class JobSpec:
    key: str            # 同时用作日志文件名与 URL 片段
    group: str
    label: str
    cli: tuple[str, ...]  # 传给 `python -m lottery_lab.cli` 的参数
    hint: str           # 大致耗时，给用户的心理预期


JOBS: tuple[JobSpec, ...] = (
    JobSpec("collect-period", "胜负彩", "抓当期对阵",
            ("collect-period",), "秒级"),
    JobSpec("collect-draws", "胜负彩", "抓历史开奖与赛果",
            ("collect-draws",), "分钟"),
    JobSpec("collect-history", "胜负彩", "抓联赛历史数据",
            ("collect-history",), "分钟"),

    JobSpec("collect-odds", "竞彩", "抓当期赔率快照",
            ("collect-odds",), "秒级"),
    JobSpec("collect-jc-history", "竞彩", "历史回填 2021 至今",
            ("collect-jc-history", "--from", "2021-01-01"), "分钟～小时"),
    JobSpec("daily-jc", "竞彩", "一条龙：预测+方案+对奖",
            ("daily-jc",), "分钟"),

    JobSpec("collect-lottery", "数字彩", "同步开奖",
            ("collect-lottery",), "分钟"),
    JobSpec("daily-lottery", "数字彩", "一条龙：刷新+预测+对奖",
            ("daily-lottery",), "秒级"),
)

_BY_KEY = {spec.key: spec for spec in JOBS}

# 只在 web 服务进程内有效：用来立刻拿到退出码与精确耗时。
# 服务重启后这两个 dict 会清空，但锁文件还在，status() 仍能认出「还在跑」的任务。
_RUNNING: dict[str, subprocess.Popen] = {}
_RESULTS: dict[str, dict] = {}


def spec(key: str) -> JobSpec | None:
    return _BY_KEY.get(key)


def grouped() -> list[tuple[str, list[JobSpec]]]:
    """按 GROUP_ORDER 分组，供页面渲染。顺序与左侧栏一致。"""
    return [(group, [s for s in JOBS if s.group == group]) for group in GROUP_ORDER]


def log_path(key: str) -> Path:
    return JOBS_DIR / f"{key}.log"


def lock_path(key: str) -> Path:
    return JOBS_DIR / f"{key}.lock"


# ---- 锁 --------------------------------------------------------------------

def _read_lock() -> dict | None:
    """全局锁内容：{"key", "pid", "started_at"}。"""
    try:
        data = json.loads(GLOBAL_LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_lock(key: str, pid: int, started_at: str) -> None:
    GLOBAL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_LOCK.write_text(
        json.dumps({"key": key, "pid": pid, "started_at": started_at}),
        encoding="utf-8")


def _clear_lock() -> None:
    GLOBAL_LOCK.unlink(missing_ok=True)


def current_run() -> dict | None:
    """当前正在跑的任务（依据锁 + 进程存活）。已死的残留锁会被清掉。"""
    lock = _read_lock()
    if not lock:
        return None
    if daemon_ctl.pid_alive(int(lock.get("pid") or 0)):
        return lock
    _clear_lock()          # 进程没了但锁还在 → 残留，清掉
    return None


# ---- 日志 ------------------------------------------------------------------

def tail_log(key: str, lines: int = LOG_TAIL_LINES) -> list[str]:
    path = log_path(key)
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in content.splitlines() if ln.strip()][-lines:]


# ---- 状态 ------------------------------------------------------------------

def _elapsed_sec(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        return round((datetime.now() - datetime.fromisoformat(started_at)).total_seconds(), 1)
    except ValueError:
        return None


def _one_status(spec_: JobSpec, lock: dict | None) -> dict:
    running = bool(lock and lock.get("key") == spec_.key)
    result = _RESULTS.get(spec_.key) or {}
    proc = _RUNNING.get(spec_.key)
    ok = result.get("ok")
    if running and proc is not None:
        code = proc.poll()
        if code is not None:            # 实际已结束，只是锁还没清
            running = False
            ok = code == 0
    return {
        "key": spec_.key,
        "group": spec_.group,
        "label": spec_.label,
        "hint": spec_.hint,
        "running": running,
        "pid": lock.get("pid") if running else None,
        "started_at": lock.get("started_at") if running else result.get("started_at"),
        "elapsed_sec": _elapsed_sec(lock.get("started_at")) if running
        else result.get("elapsed_sec"),
        "ok": None if running else ok,
        "finished_at": None if running else result.get("finished_at"),
        # 日志**始终**给：任务失败时恰恰是最需要看日志的时候，
        # 只在运行时才返回等于把排查线索关在最需要它的那一刻
        "log_tail": tail_log(spec_.key),
    }


def status() -> dict:
    """全部任务的状态。running_key 是当前在跑的那个（一次只允许一个）。"""
    record_finished()
    lock = current_run()
    jobs = [_one_status(s, lock) for s in JOBS]
    running = next((j for j in jobs if j["running"]), None)
    return {
        "running_key": running["key"] if running else None,
        "running_label": running["label"] if running else None,
        "jobs": jobs,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---- 启停 ------------------------------------------------------------------

def start(key: str) -> dict:
    spec_ = spec(key)
    if spec_ is None:
        return {"ok": False, "message": f"未知任务：{key}"}

    lock = current_run()
    if lock:
        other = spec(lock.get("key", ""))
        label = other.label if other else lock.get("key")
        return {"ok": False, "key": key,
                "message": f"已有任务在跑（{label}），等它结束或先停止再试"}

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = log_path(key)
    # 截断重写：看的是「这一次跑得怎么样」，不是历次日志堆叠
    handle = open(path, "w", encoding="utf-8")

    creationflags = 0
    if sys.platform == "win32":
        # 脱离 web 服务：服务重启不该带走正在跑的回填
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)

    cmd = [daemon_ctl._pythonw(), "-u", "-m", "lottery_lab.cli", *spec_.cli]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(daemon_ctl.PROJECT_ROOT), stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=creationflags, close_fds=True)
    except OSError as exc:
        handle.close()
        return {"ok": False, "key": key, "message": f"启动失败：{exc}"}
    finally:
        handle.close()

    started_at = datetime.now().isoformat(timespec="seconds")
    _write_lock(key, proc.pid, started_at)
    _RUNNING[key] = proc
    _RESULTS.pop(key, None)
    return {"ok": True, "key": key, "pid": proc.pid,
            "message": f"已开始：{spec_.label}"}


def stop(key: str) -> dict:
    spec_ = spec(key)
    if spec_ is None:
        return {"ok": False, "message": f"未知任务：{key}"}

    lock = current_run()
    if not lock or lock.get("key") != key:
        return {"ok": False, "key": key, "message": "该任务没有在运行"}

    pid = int(lock.get("pid") or 0)
    started_at = lock.get("started_at")
    try:
        if sys.platform == "win32":
            # /T 是必须的：pythonw 只是 launcher，真正干活的是它派生的子进程
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            import os
            os.kill(pid, 15)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "key": key, "message": f"停止失败：{exc}"}

    _clear_lock()
    _RUNNING.pop(key, None)
    _RESULTS[key] = {"ok": None, "started_at": started_at,
                     "elapsed_sec": _elapsed_sec(started_at),
                     "finished_at": datetime.now().isoformat(timespec="seconds")}
    return {"ok": True, "key": key, "pid": pid,
            "message": f"已停止：{spec_.label}"}


def record_finished() -> None:
    """把已结束但还没记账的任务落进 _RESULTS，并清掉锁。

    由 status() 顺带调用 —— 轮询本来就是最自然的检查点，不必再起一个守护线程。
    """
    lock = _read_lock()
    if not lock:
        return
    key = lock.get("key", "")
    proc = _RUNNING.get(key)
    if proc is None or proc.poll() is None:
        return
    started_at = lock.get("started_at")
    _RESULTS[key] = {"ok": proc.returncode == 0, "started_at": started_at,
                     "elapsed_sec": _elapsed_sec(started_at),
                     "finished_at": datetime.now().isoformat(timespec="seconds")}
    _RUNNING.pop(key, None)
    _clear_lock()
