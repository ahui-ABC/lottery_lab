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

from lottery_lab.models import jc_predict

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


def run_with_returns(conn: sqlite3.Connection, batch_size: int = 2000,
                     progress=None) -> dict:
    """同 `run()`，但额外保留**每注的收益**，供统计显著性检验使用。

    返回 {(pool, method): [(match_id, profit), ...]}。
    单注收益：命中 = `odds - 1`，否则 = `-1`。
    """
    returns: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)

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
                profit = (float(row["odds"]) - 1.0) if normalized == pick else -1.0
                returns[(row["pool"], row["method"])].append((item["match_id"], profit))

        done += len(batch)
        if progress:
            progress(done)
    return dict(returns)


def significance(returns: dict, baseline_roi: float = BASELINE_RETURN_RATE - 1.0,
                 alpha: float = 0.05) -> list[dict]:
    """对每条路径做「ROI 是否显著区别于随机投注」的单样本 t 检验。

    零假设 H0: ROI = baseline_roi（竞彩抽水决定的随机期望）。
    返回含标准误、95% 置信区间与 p 值的明细，按 p 值升序。
    """
    import numpy as np
    from scipy import stats

    rows = []
    for (pool, method), pairs in returns.items():
        profits = np.asarray([p for _, p in pairs], dtype=float)
        n = profits.size
        if n < 30:
            continue
        mean = float(profits.mean())
        se = float(profits.std(ddof=1) / np.sqrt(n))
        if se == 0:
            continue
        t_stat = (mean - baseline_roi) / se
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df=n - 1)))
        crit = float(stats.t.ppf(1 - alpha / 2, df=n - 1))
        rows.append({
            "pool": pool, "method": method, "n": n,
            "roi": mean,
            "se": se,
            "ci_low": mean - crit * se,
            "ci_high": mean + crit * se,
            "t": t_stat,
            "p": p_value,
            # 显著优于随机（单侧）
            "beats_random": bool(p_value < alpha and mean > baseline_roi),
        })
    rows.sort(key=lambda r: r["p"])
    return rows


def paired_compare(returns: dict, pool: str, method_a: str, method_b: str) -> dict | None:
    """同场配对比较两条路径（同一场比赛的收益差），比各自独立比较更可靠。

    返回 {n, mean_diff, se, t, p, a_better}；样本不足或无法配对返回 None。
    """
    import numpy as np
    from scipy import stats

    a = dict(returns.get((pool, method_a)) or [])
    b = dict(returns.get((pool, method_b)) or [])
    common = sorted(set(a) & set(b))
    if len(common) < 30:
        return None

    diff = np.asarray([a[m] - b[m] for m in common], dtype=float)
    n = diff.size
    mean = float(diff.mean())
    se = float(diff.std(ddof=1) / np.sqrt(n))
    if se == 0:
        return None
    t_stat = mean / se
    p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df=n - 1)))
    return {
        "pool": pool, "a": method_a, "b": method_b, "n": n,
        "mean_diff": mean, "se": se, "t": t_stat, "p": p_value,
        # 差异是否显著；显著时看谁更好
        "significant": bool(p_value < 0.05),
        "a_better": bool(p_value < 0.05 and mean > 0),
    }


def _combinations(items: list, k: int) -> list[tuple]:
    from itertools import combinations

    return list(combinations(items, k))


def backtest_parlay_box(
    conn: sqlite3.Connection,
    pool: str,
    n_legs: int = 4,
    unit: float = 2.0,
    strategy: str = "top_prob",
    min_legs_in_combo: int = 2,
    max_days: int | None = None,
    bet_pick: str = "market",
) -> dict:
    """回测「选 N 场 + 多重串关组合」策略（用户实际玩法）。

    每期从当天该玩法的比赛里按 `strategy` 选 `n_legs` 场，每场押 `bet_pick`
    路径给出的选项，然后下注**所有** k 串 1 组合（k = min_legs_in_combo..n_legs）。
    例如 4 场比分 → 6 注 2串1 + 4 注 3串1 + 1 注 4串1 = 11 注。

    只要组合内全部命中即中奖，奖金 = Π(各场赔率) × unit。

    strategy: top_prob（市场概率最高）| top_odds（赔率最高，博冷）| bottom_prob
    """
    result_col = RESULT_COLUMN[pool]
    days = [r["match_date"] for r in conn.execute(
        f"""SELECT DISTINCT m.match_date FROM jc_matches m
            WHERE m.{result_col} IS NOT NULL
              AND EXISTS(SELECT 1 FROM jc_odds_history h
                         WHERE h.match_id=m.match_id AND h.pool=?)
            ORDER BY m.match_date""", (pool,))]
    if max_days:
        days = days[-max_days:]

    totals = {"days": 0, "invested": 0.0, "returned": 0.0, "winning_days": 0,
              "best_day": None, "hits_by_size": defaultdict(int)}
    for day in days:
        rows = list(conn.execute(
            f"""SELECT match_id, {result_col} AS res FROM jc_matches
                WHERE match_date = ? AND {result_col} IS NOT NULL""", (day,)))
        if len(rows) < n_legs:
            continue

        legs = []
        for row in rows:
            series = list(conn.execute(
                """SELECT odds_json, update_date, update_time, goal_line
                   FROM jc_odds_history WHERE match_id=? AND pool=?
                   ORDER BY seq""", (row["match_id"], pool)))
            if not series:
                continue
            entries = []
            for s in series:
                try:
                    payload = json.loads(s["odds_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                e = dict(payload)
                e.update({"updateDate": s["update_date"], "updateTime": s["update_time"],
                          "goalLine": s["goal_line"]})
                entries.append(e)
            sig = jc_predict._signals(entries)
            if not sig:
                continue
            recs = jc_predict.analyze_match({pool: sig})
            chosen = next((r for r in recs if r["method"] == bet_pick), None)
            if not chosen or not chosen["pick"]:
                continue
            normalized = jc_predict.normalize_combination(pool, row["res"])
            if normalized is None:
                continue
            legs.append({
                "match_id": row["match_id"],
                "pick": chosen["pick"],
                "odds": float(chosen["odds"]),
                "prob": chosen["prob"] or 0.0,
                "hit": normalized == chosen["pick"],
            })
        if len(legs) < n_legs:
            continue

        if strategy == "top_prob":
            legs.sort(key=lambda x: -x["prob"])
            picked = legs[:n_legs]
        elif strategy == "bottom_prob":
            legs.sort(key=lambda x: x["prob"])
            picked = legs[:n_legs]
        elif strategy == "top_odds":
            legs.sort(key=lambda x: -x["odds"])
            picked = legs[:n_legs]
        else:
            raise ValueError(f"未知 strategy: {strategy}")

        day_return = 0.0
        day_bets = 0
        for k in range(min_legs_in_combo, n_legs + 1):
            for combo in _combinations(picked, k):
                day_bets += 1
                if all(x["hit"] for x in combo):
                    product = 1.0
                    for x in combo:
                        product *= x["odds"]
                    day_return += product * unit
                    totals["hits_by_size"][k] += 1

        totals["days"] += 1
        totals["invested"] += day_bets * unit
        totals["returned"] += day_return
        if day_return > day_bets * unit:
            totals["winning_days"] += 1
        if totals["best_day"] is None or day_return > totals["best_day"]:
            totals["best_day"] = day_return

    n = totals["days"]
    invested = totals["invested"]
    returned = totals["returned"]
    return {
        "pool": pool, "strategy": strategy, "n_legs": n_legs,
        "days": n,
        "invested": round(invested, 2),
        "returned": round(returned, 2),
        "roi": ((returned - invested) / invested) if invested else None,
        "return_rate": (returned / invested) if invested else None,
        "winning_days": totals["winning_days"],
        "win_day_rate": (totals["winning_days"] / n) if n else None,
        "avg_bet_per_day": round(invested / n, 2) if n else 0,
        "hits_by_size": dict(totals["hits_by_size"]),
        "best_day": totals["best_day"],
    }


def day_legs(conn: sqlite3.Connection, day: str, pool: str,
             bet_pick: str = "market") -> list[dict]:
    """该日该玩法的候选场次（按市场概率降序），含 pick / odds / prob / hit。

    行情与赛果都取自 `jc_odds_history` + `jc_matches`（**不是** `jc_predictions`
    —— 后者只有当期数据，历史回测用不了）。
    """
    column = RESULT_COLUMN.get(pool)
    if not column:
        return []
    legs = []
    for row in conn.execute(
        f"""SELECT match_id, {column} AS res FROM jc_matches
            WHERE match_date = ? AND {column} IS NOT NULL""", (day,)):
        entries = []
        for s in conn.execute(
            """SELECT odds_json, update_date, update_time, goal_line
               FROM jc_odds_history WHERE match_id=? AND pool=? ORDER BY seq""",
            (row["match_id"], pool)):
            try:
                payload = json.loads(s["odds_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            e = dict(payload)
            e.update({"updateDate": s["update_date"], "updateTime": s["update_time"],
                      "goalLine": s["goal_line"]})
            entries.append(e)
        sig = jc_predict._signals(entries)
        if not sig:
            continue
        chosen = next((r for r in jc_predict.analyze_match({pool: sig})
                       if r["method"] == bet_pick), None)
        if not chosen or not chosen["pick"]:
            continue
        normalized = jc_predict.normalize_combination(pool, row["res"])
        if normalized is None:
            continue
        legs.append({
            "pool": pool,
            "match_id": row["match_id"],
            "pick": chosen["pick"],
            "odds": float(chosen["odds"]),
            "prob": chosen["prob"] or 0.0,
            "hit": normalized == chosen["pick"],
        })
    legs.sort(key=lambda x: -x["prob"])
    return legs


def backtest_mixed_box(
    conn: sqlite3.Connection,
    picks_per_pool: dict[str, int],
    unit: float = 2.0,
    min_combo: int = 2,
    max_days: int | None = None,
) -> dict:
    """回测**混合过关**：从多个玩法各选若干场，混在同一组合池里串。

    与 `backtest_parlay_box` 的区别：后者每个玩法**独立**串（让球只和让球串）。
    本函数把不同玩法的场次混在一起。

    动机：低赔率玩法（让球 @1.44~1.52）独立串时，2 串 1 的赔率积仅 2.1，
    远不足以覆盖 4~5 场组合的成本（保本需中 4 场）。混入高赔率玩法
    （比分 @5~6）可抬高短串赔率，降低保本门槛。

    选场仍按各玩法「市场概率最高」（该规则已由 `backtest_parlay_box` 验证）。
    """
    picks_per_pool = {p: n for p, n in picks_per_pool.items() if p in RESULT_COLUMN}
    if not picks_per_pool:
        return {}

    days = [r["match_date"] for r in conn.execute(
        """SELECT DISTINCT match_date FROM jc_matches
           WHERE match_date IS NOT NULL ORDER BY match_date""")]
    if max_days:
        days = days[-max_days:]

    totals = {"days": 0, "invested": 0.0, "returned": 0.0, "winning_days": 0,
              "hits_by_size": defaultdict(int), "best_day": None}
    for day in days:
        legs = []
        for pool, want in picks_per_pool.items():
            legs.extend(day_legs(conn, day, pool)[:want])
        if len(legs) < min_combo:
            continue

        day_return = 0.0
        day_bets = 0
        for k in range(min_combo, len(legs) + 1):
            for combo in _combinations(legs, k):
                day_bets += 1
                if all(x["hit"] for x in combo):
                    product = 1.0
                    for x in combo:
                        product *= x["odds"]
                    day_return += product * unit
                    totals["hits_by_size"][k] += 1

        totals["days"] += 1
        totals["invested"] += day_bets * unit
        totals["returned"] += day_return
        if day_return > day_bets * unit:
            totals["winning_days"] += 1
        if totals["best_day"] is None or day_return > totals["best_day"]:
            totals["best_day"] = day_return

    n = totals["days"]
    invested = totals["invested"]
    returned = totals["returned"]
    return {
        "picks_per_pool": picks_per_pool,
        "n_legs": sum(picks_per_pool.values()),
        "days": n,
        "invested": round(invested, 2),
        "returned": round(returned, 2),
        "return_rate": (returned / invested) if invested else None,
        "roi": ((returned - invested) / invested) if invested else None,
        "winning_days": totals["winning_days"],
        "win_day_rate": (totals["winning_days"] / n) if n else None,
        "avg_bet_per_day": round(invested / n, 2) if n else 0,
        "hits_by_size": dict(totals["hits_by_size"]),
        "best_day": totals["best_day"],
    }


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
