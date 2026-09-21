"""Shin 去水 / 比例法对照测试（实现计划 T8）。"""
import math
import pytest

from lottery_lab.models import market


def test_probabilities_sum_to_one():
    p = market.shin([2.10, 3.40, 3.60])
    assert abs(sum(p) - 1.0) < 1e-9
    assert all(0 < x < 1 for x in p)


def test_monotonic_with_odds():
    """赔率越低隐含概率越高。"""
    p = market.shin([1.30, 5.00, 9.00])
    assert p[0] > p[1] > p[2]


def test_shin_close_to_proportional_when_balanced():
    p_shin = market.shin([2.60, 3.30, 2.90])
    p_prop = market.proportional([2.60, 3.30, 2.90])
    assert max(abs(a - b) for a, b in zip(p_shin, p_prop)) < 0.02


def test_bad_odds_raise():
    """总和远小于 1（异常盘口）应抛错。"""
    with pytest.raises(ValueError):
        market.shin([1.01, 1.01, 1.01])


def test_proportional_basic():
    p = market.proportional([2.0, 3.0, 6.0])
    assert abs(p[0] + p[1] + p[2] - 1.0) < 1e-9
    assert p[0] > p[1] > p[2]


def test_devig_picks_first_available_source():
    odds = {"avg": {"h": 2.1, "d": 3.4, "a": 3.6}}
    p = market.devig(odds)
    assert p is not None
    assert abs(sum(p) - 1.0) < 1e-9


def test_devig_falls_back_to_b365():
    odds = {"b365": {"h": 2.1, "d": 3.4, "a": 3.6}}
    p = market.devig(odds)
    assert p is not None


def test_devig_returns_none_when_no_odds():
    assert market.devig({}) is None
    assert market.devig(None) is None
