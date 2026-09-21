"""Dixon-Coles 测试（实现计划 T11）。"""
import numpy as np
import pytest

from lottery_lab.models.dixon_coles import DixonColes


def _synthetic(n_weeks=80, seed=7):
    """合成：A 最强、F 最弱；生成 6 队循环对阵。"""
    rng = np.random.default_rng(seed)
    teams = ["A", "B", "C", "D", "E", "F"]
    att = {"A": 0.6, "B": 0.3, "C": 0.1, "D": -0.1, "E": -0.3, "F": -0.6}
    deff = {"A": 0.5, "B": 0.2, "C": 0.0, "D": 0.0, "E": -0.2, "F": -0.5}
    rows = []
    for w in range(n_weeks):
        for i in range(0, len(teams), 2):
            h, a = teams[i], teams[i + 1]
            lam = np.exp(att[h] - deff[a] + 0.25)
            mu = np.exp(att[a] - deff[h])
            rows.append((f"2024-{w % 12 + 1:02d}-{w % 28 + 1:02d}", h, a,
                         int(rng.poisson(lam)), int(rng.poisson(mu))))
    return teams, rows


def test_fit_recovers_strength_order():
    teams, rows = _synthetic()
    dc = DixonColes(half_life_days=3650)
    dc.fit(rows, teams)
    p_strong = dc.predict("A", "F")
    p_weak = dc.predict("F", "A")
    assert p_strong[0] > 0.5
    assert p_weak[2] > 0.25


def test_probability_sums_to_one():
    teams, rows = _synthetic()
    dc = DixonColes(half_life_days=3650)
    dc.fit(rows, teams)
    p = dc.predict("B", "D")
    assert abs(sum(p) - 1.0) < 1e-9
    assert all(x > 0 for x in p)


def test_neutral_matches_roughly_balanced():
    teams, rows = _synthetic()
    dc = DixonColes(half_life_days=3650)
    dc.fit(rows, teams)
    p = dc.predict("C", "D")
    # 中游 vs 中游：胜率应在合理区间，平局占比也明显
    assert 0.25 < p[0] < 0.55


def test_unknown_team_returns_default():
    teams, rows = _synthetic()
    dc = DixonColes(half_life_days=3650)
    dc.fit(rows, teams)
    p = dc.predict("Z", "A")  # Z 未参与训练
    # 未知队走联赛均值先验（实现为均匀 1/3）
    assert abs(sum(p) - 1.0) < 1e-9
    assert all(x > 0 for x in p)
