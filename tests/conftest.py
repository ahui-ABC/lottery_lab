"""共享 fixture：合成按时间序的 4 队循环对阵。"""
import pytest
from datetime import date, timedelta


@pytest.fixture
def all_matches():
    """60 场合成：4 队循环对阵 + 升班马队。"""
    teams = ["A", "B", "C", "D"]
    matches = []
    base = date(2024, 1, 1)
    idx = 0
    for w in range(15):  # 15 周，每周 4 场
        for i in range(0, 4, 2):
            h, a = teams[i], teams[i + 1]
            hg, ag = (w % 3), ((w + 1) % 3)
            matches.append({
                "id": idx + 1,
                "date": base + timedelta(days=w * 7 + i),
                "league_code": "E0",
                "season": "2024/2025",
                "home_id": teams.index(h) + 1,
                "away_id": teams.index(a) + 1,
                "home": h, "away": a,
                "home_goals": hg, "away_goals": ag,
                "result": "H" if hg > ag else ("D" if hg == ag else "A"),
            })
            idx += 1
    return matches
