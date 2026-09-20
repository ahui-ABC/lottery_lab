"""采集守护进程的控制与状态查询。

CLI（单实例锁）与 Web 控制台（启停按钮）共用这一份实现，避免两处逻辑漂移。

进程模型说明：venv 的 `pythonw.exe` 是 launcher，它会派生真正的解释器作为
子进程并等待其结束。**执行采集逻辑、持有锁的是子进程**；父进程只是壳。
杀掉子进程后父 launcher 会自行退出，所以停止只需针对锁里记录的 PID。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / "data" / "logs" / "odds-daemon.lock"
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "odds-daemon.log"
DEFAULT_INTERVAL = 600


def _pythonw() -> str:
    """优先用 venv 的 pythonw（无控制台窗口）。"""
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return str(candidate) if candidate.exists() else sys.executable


def pid_alive(pid: int) -> bool:
    """该 PID 是否仍在运行。"""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_lock_pid() -> int | None:
    try:
        return int(LOCK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire_singleton(lock_path: Path | None = None) -> bool:
    """单实例锁：写入当前 PID。已被活着的进程持有则返回 False。

    持有者已死（崩溃残留）时自动接管。
    """
    lock = Path(lock_path) if lock_path is not None else LOCK_PATH
    if lock.exists():
        try:
            holder = int(lock.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            holder = None
        if holder and holder != os.getpid() and pid_alive(holder):
            return False
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def is_running() -> tuple[bool, int | None]:
    """(是否在跑, 持有的 PID)。锁文件存在但进程已死则视为未运行。"""
    pid = read_lock_pid()
    if pid and pid_alive(pid):
        return True, pid
    return False, pid


def status() -> dict:
    """供 Web 控制台展示的完整状态。"""
    running, pid = is_running()
    return {
        "running": running,
        "pid": pid if running else None,
        "stale_pid": pid if (pid and not running) else None,
        "interval": DEFAULT_INTERVAL,
        "lock_path": str(LOCK_PATH),
        "log_path": str(LOG_PATH),
        "log_lines": tail_log(40),
    }


def tail_log(lines: int = 40) -> list[str]:
    """读取日志末尾若干行（文件不存在时返回空列表）。"""
    if not LOG_PATH.exists():
        return []
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in content.splitlines() if ln.strip()][-lines:]


def start(interval: int = DEFAULT_INTERVAL) -> dict:
    """启动采集守护进程（分离、无窗口）。已在运行则直接返回现状。"""
    running, pid = is_running()
    if running:
        return {"ok": True, "started": False, "running": True, "pid": pid,
                "message": f"采集已在运行（PID {pid}）"}

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_pythonw(), "-m", "football_lottery.cli", "collect-odds",
           "--watch", "--interval", str(interval), "--log", str(LOG_PATH)]

    creationflags = 0
    if sys.platform == "win32":
        # 脱离父进程：web 服务重启/退出不应带走采集进程
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)

    subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return {"ok": True, "started": True, "running": True, "pid": None,
            "message": "已启动采集进程"}


def stop() -> dict:
    """停止采集守护进程：结束锁里记录的 PID，并清理锁文件。"""
    running, pid = is_running()
    if not running:
        # 进程已死但锁残留 → 清掉，让下次启动顺畅
        if LOCK_PATH.exists():
            LOCK_PATH.unlink(missing_ok=True)
            return {"ok": True, "stopped": False, "running": False,
                    "message": "采集未在运行，已清理残留锁"}
        return {"ok": True, "stopped": False, "running": False,
                "message": "采集未在运行"}

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as exc:
        return {"ok": False, "stopped": False, "running": True,
                "message": f"停止失败：{exc}"}

    LOCK_PATH.unlink(missing_ok=True)
    return {"ok": True, "stopped": True, "running": False, "pid": pid,
            "message": f"已停止采集进程（PID {pid}）"}


def snapshot_summary(conn) -> dict:
    """快照概况：总数、最近采集时刻、有多次记录的场次数。"""
    total = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    last = conn.execute(
        "SELECT MAX(captured_at) FROM odds_snapshots"
    ).fetchone()[0]
    changed = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT period_match_id FROM odds_snapshots
               GROUP BY period_match_id HAVING COUNT(*) > 1)"""
    ).fetchone()[0]
    return {"total": total, "last_captured_at": last, "periods_with_changes": changed,
            "checked_at": datetime.now().isoformat(timespec="seconds")}
