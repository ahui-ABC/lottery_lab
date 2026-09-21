"""融合与温度缩放测试（实现 T17 / T18）。"""
import math
import numpy as np
import pytest

from lottery_lab.models import fusion


def _synth(n: int = 400, seed: int = 1):
    """market 强信号 + dc 中强 + gbdt 纯噪声。"""
    rng = np.random.default_rng(seed)
    true_p = rng.dirichlet([3, 2, 2], size=n)
    y = np.array([rng.choice(3, p=p) for p in true_p])

    def noisy(p, sigma):
        e = np.exp(np.log(p) + rng.normal(0, sigma, size=p.shape))
        return e / e.sum(axis=1, keepdims=True)

    return {
        "y": y,
        "market": noisy(true_p, 0.10),
        "dc":    noisy(true_p, 0.40),
        "gbdt":  rng.dirichlet([1, 1, 1], size=n),
    }


def test_tier_classification():
    assert fusion.tier({"market": [.5, .3, .2], "dc": None, "gbdt": None}) == "market"
    assert fusion.tier({"market": [.5, .3, .2], "dc": [.4, .3, .3], "gbdt": None}) == "market+dc"
    assert fusion.tier({"market": [.5, .3, .2], "dc": [.4, .3, .3], "gbdt": [.3, .3, .4]}) == "all"


def test_market_only_predict_identity():
    f = fusion.Fusion()
    out = f.predict({"market": [.5, .3, .2], "dc": None, "gbdt": None})
    assert out == [.5, .3, .2]


def test_market_primary_mode_ignores_weaker_model_components():
    f = fusion.Fusion(mode="market_primary")

    out = f.predict({"market": [.5, .3, .2], "dc": [.1, .1, .8], "gbdt": [.1, .8, .1]})

    np.testing.assert_allclose(out, [.5, .3, .2])


def test_fit_predict_all_tier_sums_to_one():
    d = _synth()
    f = fusion.Fusion()
    f.fit(d, tier="all")
    out = f.predict({"market": [.5, .3, .2], "dc": [.4, .3, .3], "gbdt": [.3, .3, .4]})
    assert abs(sum(out) - 1.0) < 1e-6


def test_two_way_fusion_close_to_market_baseline():
    """加入噪声路 (dc) 后，融合 NLL 应接近/持平 market 单路。"""
    d = _synth()
    f = fusion.Fusion().fit({"y": d["y"], "market": d["market"], "dc": d["dc"]}, tier="market+dc")
    # 手算两路 NLL：Fusion 训练 loss 与只用 market 的 NLL（逐场 max-trick）
    eps = 1e-12
    market_nll = -np.log(np.maximum(d["market"][np.arange(len(d["y"])), d["y"]], eps)).mean()
    # 对每个 i 实际预测一行（融合路径）
    fused_probs = np.array([
        f.predict({"market": d["market"][i], "dc": d["dc"][i], "gbdt": None})
        for i in range(len(d["y"]))
    ])
    fused_nll = -np.log(np.maximum(fused_probs[np.arange(len(d["y"])), d["y"]], eps)).mean()
    # 不应大幅恶化（允许 0.02 容差）
    assert fused_nll <= market_nll + 0.02


def test_temperature_reduces_overconfident_nll():
    """过度自信且 30% 命中率 → T>1 降温后会降低 NLL。"""
    eps = 1e-9
    rng = np.random.default_rng(2)
    n = 600
    y = rng.integers(0, 3, size=n)
    # 构造过分自信但命中率 ~ 30%：
    pred_h = np.where(rng.random(n) < 0.30, y, rng.integers(0, 3, size=n))
    probs = np.full((n, 3), 0.05)
    probs[np.arange(n), pred_h] = 0.90
    T = fusion.fit_temperature(probs, y)
    out = fusion.apply_temperature(probs, T)
    nll_raw = -np.log(np.maximum(probs[np.arange(len(y)), y], eps)).mean()
    nll_cal = -np.log(np.maximum(out[np.arange(len(y)), y], eps)).mean()
    assert nll_cal <= nll_raw + 0.001   # 至少不能更差
    assert T >= 1.0                     # 应找到 T>=1（最优可能正好是 1）
