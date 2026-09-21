"""逐场按时间序构建特征（实现计划 T14 / 设计 §4.3）。

任何特征只来自此前已喂入的比赛（防泄漏铁律）。
默认设计文档特征列 + Elo + 联赛均值 + 赛季进度。
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
from typing import Iterable

import numpy as np


FEATURE_COLUMNS = [
    "home_points_5", "home_points_10", "home_gf_5", "home_ga_5",
    "away_points_5", "away_points_10", "away_gf_5", "away_ga_5",
    "home_home_points_5", "away_away_points_5",
    "home_rest_days", "away_rest_days",
    "elo_home", "elo_away", "elo_diff",
    "league_avg_goals", "league_home_win_rate", "league_draw_rate",
    "season_progress",
]


class _Elo:
    K = 20.0
    HOME_ADV = 65.0

    def __init__(self) -> None:
        self.r: dict[str, float] = defaultdict(lambda: 1500.0)

    def exp_score(self, h: str, a: str) -> float:
        return 1.0 / (1.0 + 10 ** ((self.r[a] - self.r[h] - self.HOME_ADV) / 400.0))

    def update(self, h: str, a: str, sh: float) -> None:
        d = self.K * (sh - self.exp_score(h, a))
        self.r[h] += d
        self.r[a] -= d


class FeatureBuilder:
    WINDOW = 10
    DEFAULT_SEASON_TOTALS = {
        "E0": 380, "I1": 380, "SP1": 380, "F1": 380,
        "E1": 552, "D1": 306, "N1": 306, "P1": 306, "SC0": 228,
    }

    def __init__(self, season_totals: dict[str, int] | None = None) -> None:
        self.results: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.WINDOW))
        self.last_date: dict[str, date] = {}
        self.elo = _Elo()
        # [总进球, 比赛数, 主胜, 平, 客胜]
        self.league_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
        self.season_matches: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.season_totals = dict(self.DEFAULT_SEASON_TOTALS)
        if season_totals:
            self.season_totals.update(season_totals)

    def build(self, matches: Iterable[dict]) -> dict[int, dict]:
        """`matches` 必须按日期升序。返回 {match_id: features}，仅含已完赛。"""
        feats: dict[int, dict] = {}
        rows = list(matches)
        # Treat fixtures on the same calendar day as one pre-match batch.
        i = 0
        while i < len(rows):
            day = rows[i].get("date")
            j = i + 1
            while j < len(rows) and rows[j].get("date") == day:
                j += 1
            group = rows[i:j]
            for m in group:
                if m.get("result"):
                    feats[m["id"]] = self._row(m)
            for m in group:
                if m.get("result"):
                    self._update(m)
            i = j
        return feats

    def build_forward(self, history: Iterable[dict], upcoming: Iterable[dict]
                      ) -> dict[int, dict]:
        """用历史喂完一遍后再为未赛对阵生成特征。"""
        self.build(history)
        return {m["id"]: self._row(m) for m in upcoming}

    # ----- 行特征 -----
    def _agg(self, team: str, n: int, home_only: bool | None = None):
        items = [r for r in self.results[team]
                 if home_only is None or r[3] == home_only]
        items = list(items)[-n:]
        if not items:
            return 0.0, 0.0, 0.0
        pts = sum(r[0] for r in items) / len(items)
        gf = sum(r[1] for r in items) / len(items)
        ga = sum(r[2] for r in items) / len(items)
        return pts, gf, ga

    def _row(self, m: dict) -> dict:
        h, a = m["home"], m["away"]
        hp5, hgf5, hga5 = self._agg(h, 5)
        hp10, _, _ = self._agg(h, 10)
        ap5, agf5, aga5 = self._agg(a, 5)
        ap10, _, _ = self._agg(a, 10)
        hh5, _, _ = self._agg(h, 5, home_only=True)
        aa5, _, _ = self._agg(a, 5, home_only=False)
        ls = self.league_stats[m["league_code"]]
        avg_goals = (ls[0] / ls[1]) if ls[1] else 2.6
        hw_rate = (ls[2] / ls[1]) if ls[1] else 0.46
        draw_rate = (ls[3] / ls[1]) if ls[1] else 0.26
        season_total = self.season_totals.get(m["league_code"], 380)
        return {
            "home_points_5": hp5, "home_points_10": hp10,
            "home_gf_5": hgf5, "home_ga_5": hga5,
            "away_points_5": ap5, "away_points_10": ap10,
            "away_gf_5": agf5, "away_ga_5": aga5,
            "home_home_points_5": hh5, "away_away_points_5": aa5,
            "home_rest_days": self._rest(h, m["date"]),
            "away_rest_days": self._rest(a, m["date"]),
            "elo_home": self.elo.r[h], "elo_away": self.elo.r[a],
            "elo_diff": self.elo.r[h] - self.elo.r[a],
            "league_avg_goals": avg_goals,
            "league_home_win_rate": hw_rate,
            "league_draw_rate": draw_rate,
            "season_progress": min(
                1.0, self.season_matches[m["league_code"]][m["season"]] / season_total
            ),
        }

    def _rest(self, team: str, d: date) -> float:
        last = self.last_date.get(team)
        if last is None:
            return 7.0
        return float((np.datetime64(d) - np.datetime64(last)).astype(int))

    def _update(self, m: dict) -> None:
        sh = {"H": 1.0, "D": 0.5, "A": 0.0}[m["result"]]
        hg, ag = m["home_goals"], m["away_goals"]
        self.results[m["home"]].append((3 * sh, hg, ag, True))
        self.results[m["away"]].append((3 * (1 - sh), ag, hg, False))
        self.last_date[m["home"]] = m["date"]
        self.last_date[m["away"]] = m["date"]
        self.elo.update(m["home"], m["away"], sh)
        ls = self.league_stats[m["league_code"]]
        ls[0] += hg + ag
        ls[1] += 1
        ls[2] += 1 if m["result"] == "H" else 0
        ls[3] += 1 if m["result"] == "D" else 0
        ls[4] += 1 if m["result"] == "A" else 0
        self.season_matches[m["league_code"]][m["season"]] += 1
