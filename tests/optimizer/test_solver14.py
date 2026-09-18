"""14 场求解器测试：枚举解法与暴力穷举对拍。"""
import itertools
import pytest

from football_lottery.optimizer import solver
from football_lottery.optimizer.probabilities import p_first, p_second


PROBS_6 = [
    [.6, .25, .15], [.4, .35, .25], [.5, .3, .2],
    [.45, .3, .25], [.8, .12, .08], [.34, .33, .33],
]

# 暴力遍历（含 0 选）每场 8 种选择集（含空集）
OPTS = [
    [(), ("3",), ("1",), ("0",),
     ("3", "1"), ("3", "0"), ("1", "0"),
     ("3", "1", "0")]
    for _ in range(6)
]


def _bruteforce(probs, objective="first_second", max_notes=4):
    best = 0.0
    for choice in itertools.product(*OPTS):
        notes = 1
        for c in choice:
            notes *= max(1, len(c))
        if notes > max_notes or any(len(c) == 0 for c in choice):
            continue
        cs = [sum(p[{"3": 0, "1": 1, "0": 2}[s]] for s in c)
              for p, c in zip(probs, choice)]
        score = p_first(cs) + (p_second(cs) if objective != "first" else 0.0)
        best = max(best, score)
    return best


def test_solver14_first_second_matches_bruteforce():
    best = solver.solve_14(PROBS_6, budget=8, objective="first_second")
    bf = _bruteforce(PROBS_6, objective="first_second")
    assert best["p_win"] == pytest.approx(bf, abs=1e-9)


def test_solver14_first_matches_bruteforce():
    best = solver.solve_14(PROBS_6, budget=8, objective="first")
    bf = _bruteforce(PROBS_6, objective="first")
    assert best["p_first"] == pytest.approx(bf, abs=1e-9)


def test_solver14_respects_budget():
    best = solver.solve_14([[.6, .25, .15]] * 14, budget=64)
    assert best["notes_count"] <= 32
    assert len(best["legs"]) == 14


def test_solver14_legs_cover_highest_probs():
    """每场选择数 m_i 决定时，应取概率最高的 m_i 个。"""
    p = [.6, .25, .15]
    best = solver.solve_14([p], budget=64)
    # 1 注单选 → 选最高概率 [0.6 = 3]
    assert best["legs"][0] == ["3"]
    assert best["notes_count"] == 1
