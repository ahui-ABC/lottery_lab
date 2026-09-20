"""市场去水（设计文档 §4.1 / 实现计划 T8）。

Shin 方法主，proportional 作对照。
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def proportional(odds: list[float]) -> list[float]:
    """简单归一化 p_i / Σp。"""
    values = np.asarray(odds, dtype=float)
    if values.shape != (3,) or not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ValueError("odds must contain three finite positive values")
    p = 1.0 / values
    return (p / p.sum()).tolist()


def shin(odds: list[float]) -> list[float]:
    """Shin 去水：解 Σπ(z) = 1 的 z。

    π_i(z) = (sqrt(z² + 4(1−z)p_i²/B) − z) / (2(1−z))，其中 p_i = 1/o_i, B = Σp_i。
    """
    values = np.asarray(odds, dtype=float)
    if values.shape != (3,) or not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ValueError("odds must contain three finite positive values")
    p = 1.0 / values
    B = float(p.sum())
    if B < 1.0 - 1e-10:
        raise ValueError(f"非法赔率（隐含概率和 {B:.4f} < 1，异常盘口）")
    # For a no-vig book z=0 is the Shin solution.  Avoid asking brentq to
    # solve a degenerate bracket at that boundary.
    if abs(B - 1.0) <= 1e-10:
        return (p / B).tolist()

    def pi(z: float) -> np.ndarray:
        return (np.sqrt(z * z + 4.0 * (1.0 - z) * p * p / B) - z) / (2.0 * (1.0 - z))

    f = lambda z: float(pi(z).sum() - 1.0)
    try:
        z = brentq(f, 1e-9, 0.5)
    except ValueError as exc:
        raise ValueError("unable to solve Shin margin for supplied odds") from exc
    out = pi(z)
    return (out / out.sum()).tolist()


# 优先级：jc（竞彩实时）→ avg → max → b365；任一可用则用之。
# jc 置前是因为当期只有竞彩赔率可用（football-data 滞后且联赛覆盖不足）；
# 历史期次没有 jc，自动回落到 avg，行为不变。
_ODDS_PREFERENCE = ("jc", "avg", "max", "b365")


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
            except (ValueError, TypeError, ZeroDivisionError, FloatingPointError):
                continue
    return None
