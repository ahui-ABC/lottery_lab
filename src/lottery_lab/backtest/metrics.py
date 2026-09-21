"""回测指标：logloss / Brier（实现计划 T9 / 设计 §4.1）。"""
from __future__ import annotations

import math

IDX = {"H": 0, "D": 1, "A": 2}


def logloss(probs: list[list[float]], outcomes: list[str]) -> float:
    """平均负对数似然；probs 每行为 [ph, pd, pa]，outcomes 每项为 'H'/'D'/'A'。"""
    eps = 1e-15
    return -sum(math.log(max(p[IDX[o]], eps)) for p, o in zip(probs, outcomes)) / len(outcomes)


def brier(probs: list[list[float]], outcomes: list[str]) -> float:
    """多分类 Brier（每类方差求和平均）。"""
    total = 0.0
    for p, o in zip(probs, outcomes):
        y = [1.0 if i == IDX[o] else 0.0 for i in range(3)]
        total += sum((pi - yi) ** 2 for pi, yi in zip(p, y))
    return total / len(probs)


def accuracy(probs: list[list[float]], outcomes: list[str]) -> float:
    """argmax 命中率。"""
    if not probs:
        return 0.0
    correct = sum(1 for p, o in zip(probs, outcomes)
                  if p.index(max(p)) == IDX[o])
    return correct / len(probs)
