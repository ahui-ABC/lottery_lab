"""市场去水（设计文档 §4.1 / 实现计划 T8）。

Shin 方法主，proportional 作对照。
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def proportional(odds: list[float]) -> list[float]:
    """简单归一化 p_i / Σp。"""
    p = 1.0 / np.asarray(odds, dtype=float)
    return (p / p.sum()).tolist()


def shin(odds: list[float]) -> list[float]:
    """Shin 去水：解 Σπ(z) = 1 的 z。

    π_i(z) = (sqrt(z² + 4(1−z)p_i²/B) − z) / (2(1−z))，其中 p_i = 1/o_i, B = Σp_i。
    """
    p = 1.0 / np.asarray(odds, dtype=float)
    B = float(p.sum())
    if B < 1.0:
        raise ValueError(f"非法赔率（隐含概率和 {B:.4f} < 1，异常盘口）")

    def pi(z: float) -> np.ndarray:
        return (np.sqrt(z * z + 4.0 * (1.0 - z) * p * p / B) - z) / (2.0 * (1.0 - z))

    f = lambda z: float(pi(z).sum() - 1.0)
    z = brentq(f, 1e-9, 0.5)
    out = pi(z)
    return (out / out.sum()).tolist()


# 优先级：avg → max → b365；任一可用则用之
_ODDS_PREFERENCE = ("avg", "max", "b365")


def devig(odds_json: dict | None, method: str = "shin") -> list[float] | None:
    """从 matches.odds_json 取一组赔率并去水。

    返回 [ph, pd, pa]，均未取到赔率时返回 None。
    """
    if not odds_json:
        return None
    for key in _ODDS_PREFERENCE:
        o = odds_json.get(key)
        if o and {"h", "d", "a"} <= set(o):
            try:
                odds = [float(o["h"]), float(o["d"]), float(o["a"])]
                if method == "shin":
                    return shin(odds)
                return proportional(odds)
            except (ValueError, TypeError):
                continue
    return None
