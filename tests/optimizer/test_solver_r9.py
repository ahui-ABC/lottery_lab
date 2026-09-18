"""任九 DP 测试（实现计划 T22）。"""
import itertools
import pytest

from football_lottery.optimizer import solver
from football_lottery.optimizer.probabilities import p_first


def test_r9_dp_matches_bruteforce():
    """8 场中选 4，预算 8 元：DP 与 (子集 × size向量) 暴力对拍。"""
    probs = [
        [.6, .25, .15], [.4, .35, .25], [.5, .3, .2], [.45, .3, .25],
        [.8, .12, .08], [.34, .33, .33], [.7, .2, .1], [.5, .25, .25],
    ]
    best = solver.solve_r9(probs, need=4, budget=8)
    bf = 0.0
    for sub in itertools.combinations(range(8), 4):
        for sizes in solver.enumerate_size_vectors(4, 4):
            legs, cs, p1, _ = solver.evaluate([probs[i] for i in sub], sizes)
            bf = max(bf, p1)
    assert best["p_win"] == pytest.approx(bf, abs=1e-9)


def test_r9_picks_exactly_nine_when_n_equals_14():
    """n = 14、need = 9 时应选恰好 9 场（其余空），且注数 ≤32。"""
    probs = [[.6, .25, .15]] * 14
    best = solver.solve_r9(probs, need=9, budget=64)
    picked = sum(1 for leg in best["legs"] if leg)
    assert picked == 9
    assert best["notes_count"] <= 32


def test_r9_returns_valid_legs():
    probs = [[.6, .25, .15]] * 14
    best = solver.solve_r9(probs, need=9)
    for leg in best["legs"]:
        if leg:  # 非空 leg 应是从 {"3","1","0"} 选 1-3 个
            assert all(s in {"0", "1", "3"} for s in leg)
            assert 1 <= len(leg) <= 3
