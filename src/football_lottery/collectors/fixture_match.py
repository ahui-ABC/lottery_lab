"""期次对阵 → matches 匹配（设计 §3.4 / 实现 T6）。

规则：
- 候选 = `|match_time - matches.match_date| <= 2 天` 且双方 `resolve(中文名)` 命中的英文名一致
- 多条候选取时间差最小；无候选 → match_id = NULL
- 返回 `{period_no: {"matched": n, "unmatched": [seq...]}}`
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from football_lottery.collectors import team_alias
from football_lottery.db import store


def _candidate_matches(
    conn: sqlite3.Connection,
    home_en: str,
    away_en: str,
    match_date: str | None,
) -> list[dict]:
    """按英文名对 + 日期 ±2 天查历史 matches。"""
    if not match_date:
        return []
    sql = """
        SELECT m.id, m.match_date, m.home_team_id, m.away_team_id,
               ht.name_en AS home, at.name_en AS away
        FROM matches m
        JOIN teams ht ON ht.id = m.home_team_id
        JOIN teams at ON at.id = m.away_team_id
        WHERE ht.name_en = ? AND at.name_en = ?
          AND ABS(JULIANDAY(m.match_date) - JULIANDAY(?)) <= 2
        ORDER BY ABS(JULIANDAY(m.match_date) - JULIANDAY(?)) ASC
    """
    return list(conn.execute(sql, (home_en, away_en, match_date, match_date)))


def match_period(
    conn: sqlite3.Connection,
    period_no: str,
    seed: dict[str, str] | None = None,
) -> dict:
    """对单期所有 period_matches 跑匹配；返回统计与未匹配清单。"""
    seed = seed or team_alias.SEED
    period = conn.execute(
        "SELECT id, period_no FROM periods WHERE period_no=?", (period_no,)
    ).fetchone()
    if not period:
        return {"matched": 0, "unmatched": []}
    pms = list(conn.execute(
        "SELECT id, seq, home_name_cn, away_name_cn, match_time, match_id, odds_json "
        "FROM period_matches WHERE period_id=? ORDER BY seq",
        (period["id"],),
    ))
    matched = 0
    unmatched: list[int] = []
    for pm in pms:
        # resolve 中文 → 英文
        home_en = team_alias.resolve(conn, pm["home_name_cn"], seed=seed)
        away_en = team_alias.resolve(conn, pm["away_name_cn"], seed=seed)
        if not home_en or not away_en:
            unmatched.append(pm["seq"])
            store.upsert(conn, "period_matches", {
                "period_id": period["id"],
                "seq": pm["seq"],
                "home_name_cn": pm["home_name_cn"],
                "away_name_cn": pm["away_name_cn"],
                "match_time": pm["match_time"],
                "match_id": None,
                "odds_json": pm["odds_json"],
            }, ["period_id", "seq"])
            continue
        cands = _candidate_matches(conn, home_en, away_en, pm["match_time"])
        if not cands:
            unmatched.append(pm["seq"])
            store.upsert(conn, "period_matches", {
                "period_id": period["id"],
                "seq": pm["seq"],
                "home_name_cn": pm["home_name_cn"],
                "away_name_cn": pm["away_name_cn"],
                "match_time": pm["match_time"],
                "match_id": None,
                "odds_json": pm["odds_json"],
            }, ["period_id", "seq"])
            continue
        chosen = cands[0]
        store.upsert(conn, "period_matches", {
            "period_id": period["id"],
            "seq": pm["seq"],
            "home_name_cn": pm["home_name_cn"],
            "away_name_cn": pm["away_name_cn"],
            "match_time": pm["match_time"],
            "match_id": chosen["id"],
            "odds_json": pm["odds_json"],
        }, ["period_id", "seq"])
        matched += 1
    conn.commit()
    return {"matched": matched, "unmatched": unmatched}


def match_all_periods(
    conn: sqlite3.Connection,
    seed: dict[str, str] | None = None,
) -> dict[str, dict]:
    """对数据库内所有未映射完的期次跑匹配。"""
    seeds = seed or team_alias.SEED
    out: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT id, period_no FROM periods WHERE status IN ('historical','current')"
    ).fetchall()
    for r in rows:
        # 跳过得失：直接 match_period
        out[r["period_no"]] = match_period(conn, r["period_no"], seed=seeds)
    return out
