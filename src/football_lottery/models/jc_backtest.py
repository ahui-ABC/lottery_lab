"""竞彩历史回测：用 2021 年至今的真实赔率与赛果，评估各推荐路径。

**为什么是 ROI 而不是 logloss**：竞彩是固定赔率，押中即按赔率返还，
因此可以算**真实投入产出**。这比概率类指标更贴近"这套策略能不能赚钱"。

**关键基线**：竞彩的返还率约 88.6%（overround≈1.129，实测），所以
**随机投注的期望 ROI ≈ -11.4%**。任何路径若 ROI 高于这条线，才说明
它提供了真实价值；低于则不如闭着眼睛买。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from football_lottery.models import jc_predict

RESULT_COLUMN = {
    "had": "result_had", "hhad": "result_hhad",
    "crs": "result_crs", "ttg": "result_ttg", "hafu": "result_hafu",
}

# 实测竞彩返还率（overround 1.129 → 1/1.129）
BASELINE_RETURN_RATE = 0.886


def _load_batch(conn: sqlite3.Connection, after_id: int, limit: int) -> list[dict]:
    """按比赛分批读入赔率序列与赛果（整批读内存，避免逐场查询）。"""
    rows = list(conn.execute(
        """SELECT m.match_id, m.result_had, m.result_hhad, m.result_crs,
                  m.result_ttg, m.result_hafu
           FROM jc_matches m
           WHERE m.match_id > ?
             AND EXISTS(SELECT 1 FROM jc_odds_history h WHERE h.match_id = m.match_id)
           ORDER BY m.match_id LIMIT ?""",
        (after_id, limit),
    ))
    if not rows:
        return []

    ids = [r["match_id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    odds_rows = list(conn.execute(
        f"""SELECT match_id, pool, odds_json, update_date, update_time, goal_line
            FROM jc_odds_history WHERE match_id IN ({placeholders})
            ORDER BY match_id, pool, seq""",
        ids,
    ))

    by_match: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in odds_rows:
        try:
            options = json.loads(row["odds_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(options, dict):
            continue
        entry = dict(options)
        entry["updateDate"] = row["update_date"]
        entry["updateTime"] = row["update_time"]
        entry["goalLine"] = row["goal_line"]
        by_match[row["match_id"]][row["pool"]].append(entry)

    out = []
    for row in rows:
        out.append({
            "match_id": row["match_id"],
            "results": {p: row[c] for p, c in RESULT_COLUMN.items()},
            "odds": dict(by_match.get(row["match_id"], {})),
        })
    return out


def run(conn: sqlite3.Connection, batch_size: int = 2000,
        progress=None) -> dict:
    """回测全部比赛。返回 {pool: {method: 统计}}。

    每场只取**最终赔率**下注（模拟赛前投注），用该选项的赔率与实际赛果判定。
    """
    stats: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "hits": 0, "invested": 0.0, "returned": 0.0}))

    last_id = 0
    done = 0
    while True:
        batch = _load_batch(conn, last_id, batch_size)
        if not batch:
            break
        last_id = batch[-1]["match_id"]

        for item in batch:
            signals = {}
            for pool, series in item["odds"].items():
                sig = jc_predict._signals(series)
                if sig:
                    signals[pool] = sig
            if not signals:
                continue

            for row in jc_predict.analyze_match(signals):
                pick = row["pick"]
                if not pick or not row["odds"]:
                    continue
                raw_result = item["results"].get(row["pool"])
                if not raw_result:
                    continue
                normalized = jc_predict.normalize_combination(row["pool"], raw_result)
                if normalized is None:
                    continue

                s = stats[row["pool"]][row["method"]]
                s["n"] += 1
                s["invested"] += 1.0                  # 每注 1 单位
                if normalized == pick:
                    s["hits"] += 1
                    s["returned"] += float(row["odds"])

        done += len(batch)
        if progress:
            progress(done)

    # 汇总
    out: dict[str, dict] = {}
    for pool, methods in stats.items():
        out[pool] = {}
        for method, s in methods.items():
            n = s["n"]
            out[pool][method] = {
                "n": n,
                "hits": s["hits"],
                "hit_rate": (s["hits"] / n) if n else None,
                "invested": round(s["invested"], 2),
                "returned": round(s["returned"], 2),
                "roi": ((s["returned"] - s["invested"]) / s["invested"]) if n else None,
            }
    return out


def compare_to_baseline(result: dict, baseline: float = BASELINE_RETURN_RATE) -> list[dict]:
    """把 ROI 与「随机投注的期望」（= -抽水）对比，排出真正的增量。"""
    rows = []
    for pool, methods in result.items():
        for method, s in methods.items():
            roi = s.get("roi")
            if roi is None:
                continue
            rows.append({
                "pool": pool,
                "method": method,
                "n": s["n"],
                "hit_rate": s["hit_rate"],
                "roi": roi,
                # 相对随机投注的增量（正数才说明优于"闭眼买"）
                "edge_vs_random": roi - (baseline - 1.0),
            })
    rows.sort(key=lambda r: (r["pool"], -(r["roi"] or -9)))
    return rows
