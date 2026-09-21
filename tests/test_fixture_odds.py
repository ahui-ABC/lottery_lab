import json

from football_lottery.collectors import fixture_match
from football_lottery.db import store


def test_match_period_preserves_period_odds():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO leagues(code, name_en) VALUES('E0', 'Premier League')")
    conn.execute("INSERT INTO teams(name_en, name_cn) VALUES('Home FC', '主队')")
    conn.execute("INSERT INTO teams(name_en, name_cn) VALUES('Away FC', '客队')")
    conn.execute(
        """INSERT INTO matches(
               league_code, season, match_date, home_team_id, away_team_id,
               home_goals, away_goals, result, odds_json
           ) VALUES('E0', '2026/2027', '2026-09-19', 1, 2, 1, 0, 'H', '{}')"""
    )
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26003', '2026-09-19', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]
    odds = json.dumps({"home": 1.8, "draw": 3.4, "away": 4.2})
    conn.execute(
        """INSERT INTO period_matches(
               period_id, seq, home_name_cn, away_name_cn, match_time, match_id, odds_json
           ) VALUES(?, 1, '主队', '客队', '2026-09-19T20:00', NULL, ?)""",
        (period_id, odds),
    )
    conn.commit()

    result = fixture_match.match_period(conn, "26003", seed={"主队": "Home FC", "客队": "Away FC"})

    assert result["matched"] == 1
    stored = conn.execute("SELECT odds_json, match_id FROM period_matches").fetchone()
    assert stored["match_id"] == 1
    assert json.loads(stored["odds_json"]) == json.loads(odds)
