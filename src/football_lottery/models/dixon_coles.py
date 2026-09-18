"""Dixon-Coles 双泊松模型（设计文档 §4.2 / 实现计划 T11）。

带低比分修正（ρ）与时间衰减（半衰期 days）；按联赛单独拟合。
未知队走联赛均值先验（α=β=0）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


class DixonColes:
    def __init__(self, half_life_days: float = 180.0, max_goals: int = 10):
        self.half_life_days = half_life_days
        self.xi = 0.5 ** (1.0 / half_life_days)
        self.max_goals = max_goals
        self.teams: list[str] = []
        self._idx: dict[str, int] = {}
        self.a: np.ndarray | None = None  # 进攻参数
        self.b: np.ndarray | None = None  # 防守参数
        self.gamma: float = 0.0  # 主场优势
        self.rho: float = 0.0  # 低比分修正

    # ----- 拟合 -----
    def fit(self, rows: Iterable, teams: list[str] | None = None) -> "DixonColes":
        """rows 元素： (date_str|date, home_name, away_name, home_goals, away_goals)."""
        rows = list(rows)
        self.teams = teams if teams is not None else sorted(
            {r[1] for r in rows} | {r[2] for r in rows}
        )
        self._idx = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)

        def to_dt(x):
            if isinstance(x, (date, datetime)):
                return x if isinstance(x, date) else x.date()
            if isinstance(x, str):
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        return datetime.strptime(x[:len(fmt) + 2], fmt).date()
                    except ValueError:
                        continue
            raise ValueError(f"无法解析日期 {x!r}")

        d = np.array([np.datetime64(to_dt(r[0])) for r in rows])
        hi = np.array([self._idx[r[1]] for r in rows])
        ai = np.array([self._idx[r[2]] for r in rows])
        hg = np.array([r[3] for r in rows], dtype=float)
        ag = np.array([r[4] for r in rows], dtype=float)
        dmax = d.max()
        w = (self.xi ** (dmax - d).astype(float))

        def nll(params: np.ndarray) -> float:
            a = params[:n]
            a = a - a.mean()
            b = params[n:2 * n]
            b = b - b.mean()
            gamma = params[2 * n]
            rho = params[2 * n + 1]
            lam = np.exp(a[hi] - b[ai] + gamma)
            mu = np.exp(a[ai] - b[hi])
            tau = np.ones_like(lam)
            m = (hg == 0) & (ag == 0)
            tau[m] = 1.0 - lam[m] * mu[m] * rho
            m = (hg == 1) & (ag == 0)
            tau[m] = 1.0 + mu[m] * rho
            m = (hg == 0) & (ag == 1)
            tau[m] = 1.0 + lam[m] * rho
            m = (hg == 1) & (ag == 1)
            tau[m] = 1.0 - rho
            ll = (hg * np.log(np.maximum(lam, 1e-9))
                  - lam
                  + ag * np.log(np.maximum(mu, 1e-9))
                  - mu
                  + np.log(np.maximum(tau, 1e-9)))
            return -float(np.sum(w * ll))

        x0 = np.zeros(2 * n + 2)
        x0[2 * n] = 0.25
        x0[2 * n + 1] = -0.05
        bounds = [(-3, 3)] * (2 * n) + [(0.0, 1.0), (-0.5, 0.5)]
        res = minimize(nll, x0, method="L-BFGS-B", bounds=bounds)
        a = res.x[:n]
        self.a = a - a.mean()
        b = res.x[n:2 * n]
        self.b = b - b.mean()
        self.gamma = float(res.x[2 * n])
        self.rho = float(res.x[2 * n + 1])
        return self

    # ----- 预测 -----
    def _lam_mu(self, home: str, away: str):
        if home in self._idx and away in self._idx:
            i, j = self._idx[home], self._idx[away]
            lam = float(np.exp(self.a[i] - self.b[j] + self.gamma))
            mu = float(np.exp(self.a[j] - self.b[i]))
            return lam, mu
        # 未知队：a=b=0 → λ = e^γ, μ = 1（即主场略占优的均匀先验）
        return float(np.exp(self.gamma)), 1.0

    def score_matrix(self, home: str, away: str) -> np.ndarray:
        lam, mu = self._lam_mu(home, away)
        g = np.arange(0, self.max_goals + 1)
        P = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
        P[0, 0] *= 1.0 - lam * mu * self.rho
        P[1, 0] *= 1.0 + mu * self.rho
        P[0, 1] *= 1.0 + lam * self.rho
        P[1, 1] *= 1.0 - self.rho
        s = P.sum()
        return P / s if s > 0 else P

    def predict(self, home: str, away: str) -> list[float]:
        P = self.score_matrix(home, away)
        return [
            float(np.tril(P, -1).sum()),  # 主胜
            float(np.trace(P)),           # 平
            float(np.triu(P, 1).sum()),   # 客胜
        ]
