import json

from lottery_lab.db import store
from lottery_lab.web import app as web_app


def _conn():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_history_reports_plan_accounting_without_join_multiplication(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26001', '2026-01-01', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]

    def add_plan(game_type, notes_count):
        cursor = conn.execute(
            """INSERT INTO plans(
                   period_id, game_type, objective, budget, notes_count,
                   legs_json, created_at
               ) VALUES(?, ?, 'test', 64, ?, ?, datetime('now'))""",
            (period_id, game_type, notes_count, json.dumps([])),
        )
        return cursor.lastrowid

    sfc_plan = add_plan("sfc14", 2)
    r9_plan = add_plan("r9", 3)
    conn.executemany(
        """INSERT INTO winnings(
               period_id, plan_id, tier, hit_notes, single_prize, amount
           ) VALUES(?, ?, ?, ?, ?, ?)""",
        [
            (period_id, sfc_plan, "first", 1, 100, 100),
            (period_id, sfc_plan, "second", 2, 10, 20),
            (period_id, r9_plan, "r9", 1, 5, 5),
        ],
    )
    conn.commit()
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    result = web_app.api_history()
    by_game = {row["game_type"]: row for row in result["summary"]}

    assert by_game["sfc14"]["invested"] == 4.0
    assert by_game["sfc14"]["prize"] == 120.0
    assert by_game["sfc14"]["profit"] == 116.0
    assert by_game["sfc14"]["hit_notes"] == 3
    assert by_game["r9"]["invested"] == 6.0
    assert by_game["r9"]["prize"] == 5.0
    assert by_game["r9"]["cumulative_invested"] == 6.0
    assert by_game["sfc14"]["cumulative_invested"] == 10.0
    assert result["totals"] == {
        "periods": 1,
        "plans": 2,
        "invested": 10.0,
        "prize": 125.0,
        "profit": 115.0,
        "hit_notes": 4,
    }


def test_alias_confirmation_rematches_and_team_candidates(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)
    called = {}

    def fake_match_all_periods(got_conn, seed=None):
        called["conn"] = got_conn
        called["seed"] = seed
        return {"26001": {"matched": 1, "unmatched": []}}

    monkeypatch.setattr(web_app.fixture_match, "match_all_periods", fake_match_all_periods)

    result = web_app.api_alias_confirm(
        web_app.AliasReq(name_cn="测试球队", name_en="Test United")
    )

    assert result["ok"] is True
    assert result["rematch"]["26001"]["matched"] == 1
    assert called["conn"] is conn
    assert called["seed"] is web_app.team_alias.SEED
    assert web_app.api_teams(q="Test")["teams"] == [
        {"name_en": "Test United", "name_cn": ""}
    ]


def test_predict_exposes_unmapped_matches_and_components(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26002', '2026-01-02', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]
    for seq in range(1, 15):
        conn.execute(
            """INSERT INTO period_matches(
                   period_id, seq, home_name_cn, away_name_cn, match_time, match_id, odds_json
               ) VALUES(?, ?, ?, ?, ?, NULL, NULL)""",
            (period_id, seq, f"主队{seq}", f"客队{seq}", "2026-01-02 20:00"),
        )
    conn.commit()
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    def fake_predictions(*args, **kwargs):
        return {
            "model_version": "test",
            "matches": [
                {"seq": seq, "market": [0.2, 0.3, 0.5], "dc": None,
                 "gbdt": None, "fused": [0.2, 0.3, 0.5]}
                for seq in range(1, 15)
            ],
        }

    monkeypatch.setattr(web_app.pipeline, "period_predictions", fake_predictions)

    result = web_app.api_predict()

    assert len(result["matches"]) == 14
    assert all(not row["mapped"] and row["match_id"] is None for row in result["matches"])
    assert len(result["unmapped"]) == 28
    # 期次是 historical，故先出现 stale_period 提示，再是未映射提示
    assert [w["type"] for w in result["warnings"]] == ["stale_period", "unmapped_fixture"]
    assert result["matches"][0]["dc"] is None
