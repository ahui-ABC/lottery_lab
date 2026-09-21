"""覆盖概率与奖级概率（设计 §6.1）。

约定：每个 probs = [ph, pd, pa]；selections 是 "3"/"1"/"0" 子集。
"""
from __future__ import annotations

from math import prod

IDX = {"3": 0, "1": 1, "0": 2}


def coverage(probs: list[float], selections: list[str]) -> float:
    """该场选择集的总覆盖概率。"""
    return sum(probs[IDX[s]] for s in selections)


def p_first(cs: list[float]) -> float:
    """一等奖：14 场（或 9 场）全对概率。"""
    return prod(cs)


def p_second(cs: list[float]) -> float:
    """二等奖（含一等同时中）：以"对 13 场"的角度，P2 = P1 · Σ(1−c)/c。"""
    p1 = p_first(cs)
    if p1 == 0:
        return 0.0
    margin = sum((1 - c) / c for c in cs if c > 0)
    return p1 * margin
