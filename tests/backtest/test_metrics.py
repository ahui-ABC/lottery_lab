"""回测指标测试。"""
import math
from lottery_lab.backtest import metrics


def test_logloss_hand_computed():
    """两场，一对一错，第二场 50/50。期望 = (0 + ln2) / 2。"""
    probs = [[1.0, 0.0, 0.0], [0.5, 0.5, 0.0]]
    outcomes = ["H", "D"]
    expected = math.log(2) / 2
    assert abs(metrics.logloss(probs, outcomes) - expected) < 1e-9


def test_brier_zero_on_perfect():
    probs = [[1.0, 0.0, 0.0]]
    assert metrics.brier(probs, ["H"]) == 0.0


def test_brier_one_class_off():
    """预测 [0,1,0] 而实际 H → (1-0)² + (0-1)² + (0-0)² = 2。"""
    probs = [[0.0, 1.0, 0.0]]
    assert metrics.brier(probs, ["H"]) == 2.0


def test_brier_three_classes():
    """预测 [0.6,0.2,0.2] 实际 D：差 (0.6)² + (0.8)² + (0.2)² ≈ 1.04。"""
    probs = [[0.6, 0.2, 0.2]]
    expected = (0.6 ** 2) + (0.8 ** 2) + (0.2 ** 2)
    assert abs(metrics.brier(probs, ["D"]) - expected) < 1e-9
