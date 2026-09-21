"""三路融合与温度缩放校准（设计 §4.4 / 实现 T17-T18）。

简化设计：
- 每档独立拟合：仅 market → 透传；2 路 → log-prob 加权；3 路 → 3 路 log-prob 软融合
- 权重通过 OOF 三路预测对数概率上跑 scipy.optimize.minimize 最小化 NLL 得到
- 温度缩放：网格搜索 T ∈ [0.5, 3.0] 找全局 NLL 最小的 T
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize


def tier(probs: dict) -> str:
    """根据哪些路有概率给出 tier key（"all" 是 3 路齐全的特殊名）。"""
    m = probs.get("market") is not None
    d = probs.get("dc") is not None
    g = probs.get("gbdt") is not None
    if m and d and g:
        return "all"
    parts = []
    if m: parts.append("market")
    if d: parts.append("dc")
    if g: parts.append("gbdt")
    return "+".join(parts) if parts else "none"


def _softmax(x, axis=-1):
    e = np.exp(x - x.max(axis=axis, keepdims=True))
    return e / e.sum(axis=axis, keepdims=True)


def nll(predict_fn, market: np.ndarray, dc: np.ndarray, y: np.ndarray) -> float:
    """predict_fn(market, dc) → (n,3) 概率 (Fusion.predict 期望 dict 形式)。"""
    p = predict_fn({"market": market, "dc": dc, "gbdt": None})
    p = np.asarray(p)
    if p.ndim == 1:
        # 单条 → 重塑
        p = np.tile(p, (len(y), 1))
    p = np.maximum(p, 1e-12)
    return float(-np.log(p[np.arange(len(y)), y]).mean())


class Fusion:
    """按 tier 路由：market 单路透传；2/3 路用对数概率加权。"""

    def __init__(self, mode: str = "full"):
        if mode not in {"full", "market_primary"}:
            raise ValueError("fusion mode must be 'full' or 'market_primary'")
        self.mode = mode
        self.weights: dict[str, np.ndarray] = {}
        self.temperature: float = 1.0

    def fit(self, data: dict, tier: str | None = None) -> "Fusion":
        """`data` = {"y": (n,), "market": (n,3)|None, "dc": ..., "gbdt": ...}。

        tier 给定时只拟合该 tier；不给定时按"组合实际可用路"覆盖常见 tier。
        """
        y = np.asarray(data["y"])
        if tier is None:
            tier = self._infer_tier(data)
        keys = self._keys_for_tier(tier)
        if not keys:
            return self
        arrays = [np.asarray(data[k], dtype=float) for k in keys]
        if len(arrays) == 1:
            self.weights[tier] = np.array([1.0])
        else:
            self.weights[tier] = self._fit_logavg(
                y, *(np.log(np.maximum(arr, 1e-9)) for arr in arrays)
            )
        return self

    @staticmethod
    def _infer_tier(data: dict) -> str:
        m = "market" in data and data["market"] is not None
        d = "dc" in data and data["dc"] is not None
        g = "gbdt" in data and data["gbdt"] is not None
        parts = []
        if m: parts.append("market")
        if d: parts.append("dc")
        if g: parts.append("gbdt")
        return "+".join(parts)

    @staticmethod
    def _keys_for_tier(tier_name: str) -> tuple[str, ...]:
        return {
            "market": ("market",),
            "dc": ("dc",),
            "gbdt": ("gbdt",),
            "market+dc": ("market", "dc"),
            "market+gbdt": ("market", "gbdt"),
            "dc+gbdt": ("dc", "gbdt"),
            "all": ("market", "dc", "gbdt"),
        }.get(tier_name, ())

    @staticmethod
    def _fit_logavg(y: np.ndarray, *log_p_list: np.ndarray) -> np.ndarray:
        """最小化 NLL 找出 log-p 软融合的归一权重（softmax(weights)）。"""
        def loss(w):
            wn = np.exp(w - w.max())
            wn = wn / wn.sum()
            mix = sum(wn[i] * log_p_list[i] for i in range(len(log_p_list)))
            p = _softmax(mix, axis=-1)
            p = np.maximum(p, 1e-12)
            return float(-np.log(p[np.arange(len(y)), y]).mean())

        x0 = np.zeros(len(log_p_list))
        res = minimize(loss, x0, method="Nelder-Mead", options={"xatol": 1e-4})
        w = np.exp(res.x - res.x.max())
        return w / w.sum()

    def predict(self, probs: dict) -> list[float]:
        if self.mode == "market_primary" and probs.get("market") is not None:
            out = np.asarray(probs["market"], dtype=float)
            out = np.maximum(out, 0.0)
            total = out.sum()
            return list(out / total) if total > 0 else [1 / 3, 1 / 3, 1 / 3]
        t = tier(probs)
        keys = self._keys_for_tier(t)
        if not keys:
            return [1 / 3, 1 / 3, 1 / 3]
        if len(keys) == 1:
            out = np.asarray(probs[keys[0]], dtype=float)
        else:
            weights = self.weights.get(t, np.full(len(keys), 1 / len(keys)))
            mix = sum(weights[i] * np.log(np.maximum(probs[k], 1e-9))
                       for i, k in enumerate(keys))
            out = _softmax(mix)
        if self.temperature != 1.0:
            log_p = np.log(np.maximum(out, 1e-9))
            out = _softmax(log_p / self.temperature)
        out = out / out.sum()
        return list(out)


def fit_temperature(probs: np.ndarray, y: np.ndarray,
                    grid: np.ndarray | None = None) -> float:
    """网格搜索最小 NLL 的 T∈[0.5, 3.0]。"""
    if grid is None:
        grid = np.linspace(0.5, 3.0, 51)
    eps = 1e-12
    def nll_at(t: float) -> float:
        p = apply_temperature(probs, t)
        return float(-np.log(np.maximum(p[np.arange(len(y)), y], eps)).mean())
    losses = [nll_at(t) for t in grid]
    return float(grid[int(np.argmin(losses))])


def apply_temperature(probs: np.ndarray, T: float) -> np.ndarray:
    """p' = softmax(log(p) / T)。"""
    return _softmax(np.log(np.maximum(probs, 1e-9)) / T)
