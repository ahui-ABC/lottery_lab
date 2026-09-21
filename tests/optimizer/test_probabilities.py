"""覆盖概率与奖级概率测试（实现计划 T20）。"""
import pytest

from lottery_lab.optimizer import probabilities as P


def test_coverage_sum():
    probs = [0.5, 0.3, 0.2]
    assert P.coverage(probs, ["3", "1"]) == pytest.approx(0.8)


def test_p1_p2_hand_computed():
    cs = [0.8, 0.7, 0.6]
    p1 = P.p_first(cs)
    assert p1 == pytest.approx(0.336)
    p2 = P.p_second(cs)
    # P2 = P1 * Σ(1-c)/c ；公式与 c 顺序无关
    expected_p2 = 0.336 * (0.25 + 0.4285714286 + 0.6666666667)
    assert p2 == pytest.approx(expected_p2, rel=1e-9)


def test_p2_matches_direct_formula():
    cs = [0.999, 0.999, 0.5]
    expected = (
        (1 - 0.999) * 0.999 * 0.5
        + (1 - 0.999) * 0.999 * 0.5
        + (1 - 0.5) * 0.999 * 0.999
    )
    assert P.p_second(cs) == pytest.approx(expected, rel=1e-9)


def test_p_first_degenerate_zero_coverage():
    # 某场 c=0 → P1=0, P2=0
    cs = [0.0, 0.5]
    assert P.p_first(cs) == 0.0
    assert P.p_second(cs) == 0.0
