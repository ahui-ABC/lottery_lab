"""14 场 / 任九 方案求解（设计 §6.2 / 实现 T21-T22）。

关键引理：固定各场选择数 m_i 后，每场取概率最高的 m_i 个结果使 P1 与 P1+P2 同时最优；
故只需枚举 size 向量（m_1, …, m_n），m_i ∈ {1,2,3}，约束 Πm_i ≤ max_notes。
"""
from __future__ import annotations

import math
from math import prod

import numpy as np

from lottery_lab.optimizer import probabilities as P

OUTCOMES = ("3", "1", "0")
IDX = {"3": 0, "1": 1, "0": 2}


def _normalise_probs(probs_list: list[list[float]], expected: int | None = None) -> list[list[float]]:
    if expected is not None and len(probs_list) != expected:
        raise ValueError(f"expected {expected} probability rows")
    out: list[list[float]] = []
    for row in probs_list:
        values = np.asarray(row, dtype=float)
        if values.shape != (3,) or not np.all(np.isfinite(values)) or np.any(values < 0):
            raise ValueError("each probability row must contain three finite non-negative values")
        total = float(values.sum())
        if total <= 0:
            raise ValueError("each probability row must have a positive sum")
        out.append((values / total).tolist())
    return out


def top_m(probs: list[float], m: int) -> list[str]:
    """按概率降序取前 m 个，并按 (3,1,0) 自然序返回。"""
    order = sorted(range(3), key=lambda i: probs[i], reverse=True)[:m]
    return sorted((OUTCOMES[i] for i in order), key=OUTCOMES.index)


def enumerate_size_vectors(n: int, max_notes: int):
    """遍历 (m_1, …, m_n), m_i ∈ {1,2,3}, Πm_i ≤ max_notes。"""
    def rec(i: int, prod_v: int, cur: list[int]):
        if i == n:
            yield tuple(cur)
            return
        for m in (1, 2, 3):
            if prod_v * m <= max_notes:
                cur.append(m)
                yield from rec(i + 1, prod_v * m, cur)
                cur.pop()
    yield from rec(0, 1, [])


def evaluate(probs_list: list[list[float]], sizes: list[int]):
    """按各场选择数构建 legs/cs/P1/P2。"""
    legs = [top_m(p, m) for p, m in zip(probs_list, sizes)]
    cs = [P.coverage(p, leg) for p, leg in zip(probs_list, legs)]
    return legs, cs, P.p_first(cs), P.p_second(cs)


def solve_14(probs_list: list[list[float]],
             budget: int = 64,
             ticket_price: int = 2,
             objective: str = "first_second") -> dict:
    """14 场 size 向量枚举（毫秒级精确最优）。

    objective ∈ {"first", "first_second"}。
    """
    if objective not in {"first", "first_second"}:
        raise ValueError("objective must be 'first' or 'first_second'")
    if ticket_price <= 0 or budget < ticket_price:
        raise ValueError("budget must cover at least one ticket")
    # The core solver is also used by small synthetic tests and generic
    # optimizers; the API layer enforces the 14-match SFC contract.
    probs_list = _normalise_probs(probs_list)
    max_notes = budget // ticket_price
    best: dict | None = None
    for sizes in enumerate_size_vectors(len(probs_list), max_notes):
        legs, cs, p1, p2 = evaluate(probs_list, sizes)
        score = p1 if objective == "first" else p1 + p2
        if best is None or score > best["score"]:
            best = {
                "sizes": list(sizes),
                "legs": legs,
                "p_first": p1,
                "p_second": p2,
                "p_win": p1 + p2,
                "score": score,
                "notes_count": int(prod(sizes)),
            }
    if best is None:
        raise ValueError("no feasible sfc14 plan for the supplied budget")
    return best


def solve_r9(probs_list: list[list[float]],
             need: int = 9,
             budget: int = 64,
             ticket_price: int = 2) -> dict:
    """任九（n≥need 场中选 need 场，预算注数），分组背包 DP。

    每个 probs 一场；选 need 场且 Πm_i ≤ max_notes，最大化 Πc_i。
    """
    if need < 1 or need > len(probs_list):
        raise ValueError("need must be between 1 and the number of probability rows")
    if ticket_price <= 0 or budget < ticket_price:
        raise ValueError("budget must cover at least one ticket")
    probs_list = _normalise_probs(probs_list)
    n = len(probs_list)
    max_notes = budget // ticket_price
    if max_notes < 1:
        raise ValueError("预算不足 2 元")

    NEG = -1e18
    # dp[i][k][notes] = log 最大值
    dp = np.full((n + 1, need + 1, max_notes + 1), NEG)
    choice: dict[tuple[int, int, int], tuple[str, list[str] | None, int]] = {}
    dp[0, 0, 1] = 0.0

    for i in range(n):
        for k in range(need + 1):
            for notes in range(1, max_notes + 1):
                cur = dp[i, k, notes]
                if cur <= NEG / 2:
                    continue
                # 跳过
                if cur > dp[i + 1, k, notes]:
                    dp[i + 1, k, notes] = cur
                    choice[(i + 1, k, notes)] = ("skip", None, notes)
                # 取
                if k < need:
                    for m in (1, 2, 3):
                        nn = notes * m
                        if nn > max_notes:
                            continue
                        leg = top_m(probs_list[i], m)
                        c = P.coverage(probs_list[i], leg)
                        if c <= 0:
                            continue
                        v = cur + math.log(c)
                        if v > dp[i + 1, k + 1, nn]:
                            dp[i + 1, k + 1, nn] = v
                            choice[(i + 1, k + 1, nn)] = ("pick", leg, notes)

    best_notes = max(range(1, max_notes + 1), key=lambda t: dp[n, need, t])
    if dp[n, need, best_notes] <= NEG / 2:
        raise ValueError("no feasible r9 plan for the supplied budget")
    legs_chosen: list[list[str] | None] = [None] * n
    k, notes = need, best_notes
    for i in range(n, 0, -1):
        kind, val, prev_notes = choice[(i, k, notes)]
        legs_chosen[i - 1] = val if kind == "pick" else []
        if kind == "pick":
            k -= 1
        notes = prev_notes
    p_win = float(math.exp(dp[n, need, best_notes]))
    return {
        "legs": legs_chosen,
        "p_win": p_win,
        "p_first": p_win,
        "notes_count": int(best_notes),
        "picked_count": sum(1 for x in legs_chosen if x),
    }
