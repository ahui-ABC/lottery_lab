"""数字彩选号策略。

设计见 `docs/superpowers/specs/2026-09-21-digital-lottery-design.md` §5。
五条路径的差别**只在权重函数**，采样过程共用一套「加权不放回抽样」（Gumbel top-k）。

硬约束：第 t 期只能用 < t 期的数据。本模块的 `predict_one` 只读调用方给的
history，不自行按日期筛选 —— 时间边界由调用方（回测游标 / 线上预测）负责。
测试用「同一份历史给同一结果、换一份历史结果必变」锁死「无隐藏状态」这一点。
"""
from __future__ import annotations

import json
import math
import random
from collections import Counter
from datetime import date, datetime

from lottery_lab.collectors.lottery_history import LOTTERIES

STRATEGIES = ("random", "hot", "cold", "overdue", "weighted")
STRATEGY_LABELS = {
    "random": "随机",
    "hot": "热号",
    "cold": "冷号",
    "overdue": "遗漏",
    "weighted": "贝叶斯",
}
DEFAULT_WINDOW = 100
DEFAULT_BETS = 5
BET_PRICE = 2           # 元/注，与 lottery_backtest.BET_PRICE 一致
DIRICHLET_ALPHA = 1.0


def _gumbel(rng: random.Random) -> float:
    """标准 Gumbel 噪声 -log(-log(U))。"""
    return -math.log(-math.log(rng.random()))


def weighted_sample(weights: list[float], k: int, rng: random.Random) -> list[int]:
    """加权不放回抽样，返回选中下标（升序）。

    Gumbel top-k：给 log(w) 加 Gumbel 噪声后取前 k 大，等价于按 w 不放回抽样，
    O(n log n)、无需累积分布，权重为 0 也安全。
    """
    keys = [(math.log(max(w, 1e-12)) + _gumbel(rng), i) for i, w in enumerate(weights)]
    keys.sort(key=lambda pair: (-pair[0], pair[1]))
    return sorted(i for _, i in keys[:k])


def _last_seen_gap(series: list[list[str]], universe: list[str]) -> dict[str, int]:
    """每个号码距最近一次出现过了几期（0 = 最近一期刚出）。

    窗口内从未出现的号码取上界 len(series) —— 对排序而言与真实遗漏等价。
    """
    gap = {x: len(series) for x in universe}
    seen: set[str] = set()
    for age, values in enumerate(reversed(series)):
        for value in values:
            if value in gap and value not in seen:
                gap[value] = age
                seen.add(value)
    return gap


def _weights(strategy: str, series: list[list[str]], universe: list[str],
             rng: random.Random) -> list[float]:
    counts = Counter(v for values in series for v in values)
    if strategy == "random":
        return [1.0] * len(universe)
    if strategy == "hot":
        return [counts.get(x, 0) + 1.0 for x in universe]
    if strategy == "cold":
        top = max([counts.get(x, 0) for x in universe] + [0])
        return [(top - counts.get(x, 0)) + 1.0 for x in universe]
    if strategy == "overdue":
        gap = _last_seen_gap(series, universe)
        return [gap.get(x, 0) + 1.0 for x in universe]
    if strategy == "weighted":
        # Dirichlet(α + 频次) 后验：Gamma 采样即可，归一化对排序无影响
        return [rng.gammavariate(DIRICHLET_ALPHA + counts.get(x, 0), 1.0)
                for x in universe]
    raise ValueError(f"未知策略：{strategy}")


def _series(history: list[dict], field: str) -> list[list[str]]:
    """把历史开奖按 field 抽成一个序列。field: 'front'/'back'/'d0'/'d1'..."""
    out = []
    for draw in history:
        numbers = draw["numbers"]
        if field == "front":
            out.append(list(numbers["front"]))
        elif field == "back":
            out.append(list(numbers["back"]))
        else:
            out.append([numbers["digits"][int(field[1:])]])
    return out


def _pick(strategy: str, series: list[list[str]], universe: list[str], k: int,
          rng: random.Random) -> list[str]:
    idx = weighted_sample(_weights(strategy, series, universe, rng), k, rng)
    return [universe[i] for i in idx]


def predict_one(lottery: str, history: list[dict], strategy: str,
                rng: random.Random, window: int = DEFAULT_WINDOW) -> dict:
    """生成一注号码。history 必须**只含目标期之前**的开奖，时序升序。"""
    spec = LOTTERIES[lottery]
    recent = history[-window:] if window > 0 else history
    if spec["kind"] == "two_zone":
        front_universe = [f"{i:02d}" for i in range(1, spec["front_max"] + 1)]
        back_universe = [f"{i:02d}" for i in range(1, spec["back_max"] + 1)]
        return {
            "front": _pick(strategy, _series(recent, "front"), front_universe,
                           spec["front"], rng),
            "back": _pick(strategy, _series(recent, "back"), back_universe,
                          spec["back"], rng),
        }
    universe = [str(i) for i in range(10)]
    return {"digits": [_pick(strategy, _series(recent, f"d{pos}"), universe, 1, rng)[0]
                       for pos in range(spec["digits"])]}


def predict_bets(lottery: str, history: list[dict], strategy: str, n_bets: int,
                 seed, window: int = DEFAULT_WINDOW) -> list[dict]:
    """生成一批注单。

    随机流按 attempt 递增，**同一期内不重复**；历史极度退化（例如某个位置只有
    一个数字曾有非零权重）导致凑不满 n_bets 时，如实返回能凑出的注数 ——
    调用方按实际注数算投入，不要假设一定等于 n_bets。
    """
    bets: list[dict] = []
    seen: set[str] = set()
    for attempt in range(max(n_bets, 1) * 20):
        if len(bets) >= n_bets:
            break
        rng = random.Random(f"{seed}-{strategy}-{attempt}")
        picked = predict_one(lottery, history, strategy, rng, window)
        key = json.dumps(picked, sort_keys=True)
        if key not in seen:
            seen.add(key)
            bets.append(picked)
    return bets


def next_issue(last_issue: str, today: date | None = None) -> str:
    """下一期期号：同年 +1；跨年归 001。"""
    today = today or date.today()
    year, number = int(last_issue[:4]), int(last_issue[4:])
    if today.year > year:
        return f"{year + 1}001"
    return f"{year}{number + 1:03d}"


def load_history(conn, lottery: str, before_issue: str | None = None) -> list[dict]:
    """取某彩种历史开奖，时序升序。before_issue 给定时只取严格早于它的期数。"""
    sql = "SELECT lottery, issue, numbers FROM lottery_draw WHERE lottery=?"
    params: list = [lottery]
    if before_issue is not None:
        sql += " AND issue < ?"
        params.append(before_issue)
    sql += " ORDER BY issue"
    return [{"lottery": r["lottery"], "issue": r["issue"],
             "numbers": json.loads(r["numbers"])}
            for r in conn.execute(sql, params)]


def save_predictions(conn, lottery: str, target_issue: str, strategies,
                     n_bets: int, window: int, seed) -> int:
    """对下一期生成各策略推荐并入库。返回写入条数。"""
    history = load_history(conn, lottery, target_issue)
    if not history:
        return 0
    written = 0
    for strategy in strategies:
        bets = predict_bets(lottery, history, strategy, n_bets, seed, window)
        conn.execute(
            """INSERT INTO lottery_prediction(
                   lottery, target_issue, strategy, bets, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(lottery, target_issue, strategy) DO UPDATE SET
                 bets=excluded.bets, created_at=excluded.created_at""",
            (lottery, target_issue, strategy, json.dumps(bets, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        written += 1
    conn.commit()
    return written
