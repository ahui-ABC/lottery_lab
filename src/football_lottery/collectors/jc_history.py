"""竞彩历史数据同步（子项目 1/3）。

设计见 `docs/superpowers/specs/2026-09-20-jc-history-sync-design.md`。

采集模型：**多线程抓 HTTP + 单线程写库**。SQLite 写入互斥，多线程写会抛
`database is locked`；让线程池只做网络，入库串行化，既避免锁竞争，也让计数天然准确。

写库**不用 `store.upsert`** —— 它逐行 commit，一场约 16 行、全场 3 万场就是约
50 万次 fsync，单写线程会被拖死。这里改为**每场一个事务**。
"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from football_lottery.collectors import sporttery

# 玩法 → 单场接口里的列表字段名
POOL_LIST_KEY = {
    "had": "hadList",
    "hhad": "hhadList",
    "crs": "crsList",
    "ttg": "ttgList",
    "hafu": "hafuList",
}
POOLS = tuple(POOL_LIST_KEY)

# 开奖结果的 code → jc_matches 的列名后缀
RESULT_CODE = {"HAD": "had", "HHAD": "hhad", "CRS": "crs", "TTG": "ttg", "HAFU": "hafu"}

DEFAULT_FROM = "2021-01-01"

# 熔断：跨整次运行累计的失败率上限。单日只有约 17 场，按日统计永远触发不了。
FAILURE_WINDOW = 100
FAILURE_RATE_LIMIT = 0.20


class CircuitBreakerTripped(RuntimeError):
    """失败率在并发降无可降后仍超限 —— 继续跑只会白白制造失败记录。"""


def _num(value) -> float | None:
    """官方接口用空串表示"未开售"；写 REAL 列前必须映射为 NULL 而非 0.0。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _text(value) -> str | None:
    text = (value or "").strip()
    return text or None


# ---- 解析层（纯函数） --------------------------------------------------------------
def parse_match_result(value: dict) -> list[dict]:
    """`getUniformMatchResultV1` 的 value → 比赛行列表。"""
    out: list[dict] = []
    for item in value.get("matchResult") or []:
        match_id = item.get("matchId")
        if not match_id:
            continue
        out.append({
            "match_id": int(match_id),
            "match_date": _text(item.get("matchDate")),
            "match_num": _text(item.get("matchNumStr")) or _text(item.get("matchNum")),
            "league_id": item.get("leagueId"),
            "league_name": _text(item.get("leagueName")),
            "home_team": _text(item.get("allHomeTeam")) or _text(item.get("homeTeam")),
            "away_team": _text(item.get("allAwayTeam")) or _text(item.get("awayTeam")),
            "home_team_id": item.get("homeTeamId"),
            "away_team_id": item.get("awayTeamId"),
            "had_h": _num(item.get("h")),
            "had_d": _num(item.get("d")),
            "had_a": _num(item.get("a")),
            "goal_line": _text(item.get("goalLine")),
        })
    return out


def parse_fixed_bonus(value: dict) -> dict:
    """`getFixedBonusV1` 的 value → {results, odds}。

    - results: {列名: 开奖结果原值}，如 {"result_had": "H"}
    - odds:    [(pool, seq, update_date, update_time, goal_line, odds_json), ...]

    开奖结果**只存原始 combination**，不做归一化 —— 归一化规则可能随官方调整，
    且原始值可无损还原（映射表见设计文档 §3.3）。
    """
    results: dict[str, str] = {}
    for item in value.get("matchResultList") or []:
        suffix = RESULT_CODE.get((item.get("code") or "").strip().upper())
        if suffix and item.get("combination") is not None:
            results[f"result_{suffix}"] = str(item.get("combination"))

    odds: list[tuple] = []
    history = value.get("oddsHistory") or {}
    for pool, key in POOL_LIST_KEY.items():
        for seq, entry in enumerate(history.get(key) or []):
            payload = {
                k: v for k, v in entry.items()
                if k not in ("updateDate", "updateTime")
            }
            odds.append((
                pool,
                seq,
                _text(entry.get("updateDate")),
                _text(entry.get("updateTime")),
                _text(entry.get("goalLine")),
                json.dumps(payload, ensure_ascii=False),
            ))
    return {"results": results, "odds": odds}


# ---- 入库层 ----------------------------------------------------------------------
def upsert_match_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """比赛列表写入（一个事务）。不清空 result_* 与已有赔率。

    公开给 `jc_predict` 复用：当期预测的比赛可能不在按日期回填的集合里
    （`getUniformMatchResultV1` 按比赛日、`getMatchCalculatorV1` 按销售日），
    预测时需要把比赛基础信息补进 `jc_matches`，否则对奖拿不到赛果。
    """
    if not rows:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO jc_matches(
               match_id, match_date, match_num, league_id, league_name,
               home_team, away_team, home_team_id, away_team_id,
               had_h, had_d, had_a, goal_line, captured_at)
           VALUES(:match_id, :match_date, :match_num, :league_id, :league_name,
                  :home_team, :away_team, :home_team_id, :away_team_id,
                  :had_h, :had_d, :had_a, :goal_line, :captured_at)
           ON CONFLICT(match_id) DO UPDATE SET
               match_date=excluded.match_date, match_num=excluded.match_num,
               league_id=excluded.league_id, league_name=excluded.league_name,
               home_team=excluded.home_team, away_team=excluded.away_team,
               home_team_id=excluded.home_team_id, away_team_id=excluded.away_team_id,
               had_h=excluded.had_h, had_d=excluded.had_d, had_a=excluded.had_a,
               goal_line=excluded.goal_line, captured_at=excluded.captured_at""",
        [{**row, "captured_at": now} for row in rows],
    )
    conn.commit()


def _write_one_match(conn: sqlite3.Connection, match_id: int, parsed: dict,
                     captured_at: str) -> int:
    """一场比赛的全部数据写入 —— **一个事务**（见模块 docstring 的性能说明）。"""
    rows = 0
    try:
        for pool, seq, up_date, up_time, goal_line, odds_json in parsed["odds"]:
            conn.execute(
                """INSERT INTO jc_odds_history(
                       match_id, pool, seq, update_date, update_time, goal_line, odds_json)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(match_id, pool, update_date, update_time) DO UPDATE SET
                       seq=excluded.seq, goal_line=excluded.goal_line,
                       odds_json=excluded.odds_json""",
                (match_id, pool, seq, up_date, up_time, goal_line, odds_json),
            )
            rows += 1

        results = parsed["results"]
        if results:
            columns = ", ".join(f"{k}=?" for k in results)
            conn.execute(
                f"UPDATE jc_matches SET {columns}, captured_at=? WHERE match_id=?",
                [*results.values(), captured_at, match_id],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rows


# ---- 同步编排 --------------------------------------------------------------------
def _day_done(conn, day: str, refresh_from: str | None) -> bool:
    """该日是否已完成（且不在刷新窗口内）。"""
    if refresh_from and day >= refresh_from:
        return False
    row = conn.execute(
        "SELECT 1 FROM jc_sync_log WHERE sync_date=?", (day,)
    ).fetchone()
    return row is not None


def sync_day(conn, day: str, workers: int = 4, delay: float = 0.2,
             page_size: int = 100, max_pages: int = 50) -> dict:
    """同步一天的比赛列表与逐场赔率。返回统计。

    并发只用于 HTTP；写库在主线程串行执行。
    """
    matches: list[dict] = []
    for page_no in range(1, max_pages + 1):
        value = sporttery.fetch_uniform_match_result(day, day, page_no, page_size)
        page_rows = parse_match_result(value)
        matches.extend(page_rows)
        pages = int(value.get("pages") or 1)
        if page_no >= pages or not page_rows:
            break

    if not matches:
        return {"date": day, "matches": 0, "odds_rows": 0, "failed": 0, "failed_ids": []}

    upsert_match_rows(conn, matches)

    captured_at = datetime.now().isoformat(timespec="seconds")
    odds_rows = 0
    failed_ids: list[int] = []

    def _fetch(match_id: int):
        if delay:
            time.sleep(delay)          # 每个 worker 自身的节流（收到响应后再 sleep）
        return match_id, sporttery.fetch_fixed_bonus(match_id)

    def _run(pool_size: int):
        nonlocal odds_rows
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = {pool.submit(_fetch, m["match_id"]): m["match_id"] for m in matches}
            for future in as_completed(futures):
                match_id = futures[future]
                try:
                    _, value = future.result()
                except Exception:
                    failed_ids.append(match_id)
                    continue
                if not value.get("oddsHistory"):
                    continue          # 比赛取消/未开售 —— 正常跳过，不计失败
                parsed = parse_fixed_bonus(value)
                odds_rows += _write_one_match(conn, match_id, parsed, captured_at)

    _run(max(1, workers))

    conn.execute(
        """INSERT INTO jc_sync_log(
               sync_date, matches, odds_rows, failed, failed_match_ids, finished_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(sync_date) DO UPDATE SET
               matches=excluded.matches, odds_rows=excluded.odds_rows,
               failed=excluded.failed, failed_match_ids=excluded.failed_match_ids,
               finished_at=excluded.finished_at""",
        (day, len(matches), odds_rows, len(failed_ids),
         json.dumps(failed_ids[:200]), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return {"date": day, "matches": len(matches), "odds_rows": odds_rows,
            "failed": len(failed_ids), "failed_ids": failed_ids}


def sync_range(
    conn,
    from_date: str = DEFAULT_FROM,
    to_date: str | None = None,
    workers: int = 4,
    delay: float = 0.2,
    page_size: int = 100,
    refresh_days: int = 1,
    force: bool = False,
    retry_failed: bool = False,
    progress=None,
) -> dict:
    """按日期区间同步。支持断点续传、尾日刷新、失败重跑。

    刷新窗口锚定 `to_date`（非今天），这样历史区间回填不会把今天算进来。
    """
    end = date.fromisoformat(to_date) if to_date else date.today()
    start = date.fromisoformat(from_date)
    if start > end:
        raise ValueError(f"起始日期 {from_date} 晚于结束日期 {end.isoformat()}")

    # 刷新窗口（--retry-failed 时不生效）
    refresh_from = None
    if refresh_days > 0 and not retry_failed:
        refresh_from = (end - timedelta(days=refresh_days - 1)).isoformat()

    day = start
    effective_workers = max(1, workers)
    summary = {"days": 0, "skipped": 0, "matches": 0, "odds_rows": 0, "failed": 0,
               "workers_start": effective_workers, "workers_end": effective_workers,
               "breaker_warnings": 0}
    while day <= end:
        key = day.isoformat()
        if retry_failed:
            row = conn.execute(
                "SELECT failed FROM jc_sync_log WHERE sync_date=?", (key,)
            ).fetchone()
            if not row or not row["failed"]:
                summary["skipped"] += 1
                day += timedelta(days=1)
                continue
        elif not force and _day_done(conn, key, refresh_from):
            summary["skipped"] += 1
            day += timedelta(days=1)
            continue

        result = sync_day(conn, key, workers=effective_workers, delay=delay,
                          page_size=page_size)
        summary["days"] += 1
        summary["matches"] += result["matches"]
        summary["odds_rows"] += result["odds_rows"]
        summary["failed"] += result["failed"]
        if progress:
            progress(result)

        # 熔断：整次运行累计（单日仅约 17 场，按日窗口触发不了）
        seen = summary["matches"]
        if seen >= FAILURE_WINDOW:
            rate = summary["failed"] / seen if seen else 0.0
            if rate > FAILURE_RATE_LIMIT:
                if effective_workers <= 1:
                    summary["workers_end"] = effective_workers
                    raise CircuitBreakerTripped(
                        f"失败率 {rate:.0%} 超过 {FAILURE_RATE_LIMIT:.0%}，"
                        f"且并发已降至 1；已中止（累计 {summary['failed']}/{seen} 场失败）"
                    )
                # ThreadPoolExecutor 无法动态改大小：降级从下一批次（下一日）生效
                effective_workers = max(1, effective_workers // 2)
                summary["breaker_warnings"] += 1
                summary["workers_end"] = effective_workers
                if progress:
                    progress({"date": key, "breaker": True,
                              "workers": effective_workers, "rate": rate})

        day += timedelta(days=1)

    summary["workers_end"] = effective_workers
    return summary
