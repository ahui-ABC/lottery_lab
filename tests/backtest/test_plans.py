import json

from lottery_lab.backtest import plans
from lottery_lab.db import store


def test_evaluate_sfc_uses_each_prize_tier():
    legs = [["3", "1"], *([["3"]] * 13)]
    result = ["3"] * 14

    evaluated = plans._evaluate(
        "sfc14", {"legs": legs}, result, {"first": 100.0, "second": 10.0}
    )

    assert evaluated == {
        "first_notes": 1,
        "second_notes": 1,
        "first_prize": 100.0,
        "second_prize": 10.0,
        "prize": 110.0,
    }


def test_plan_summary_counts_both_game_investments(monkeypatch):
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('001', '2026-01-01', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        "INSERT INTO draw_results(period_id, results_json, prizes_json) VALUES(?, ?, ?)",
        (period_id, ",".join(["3"] * 14), json.dumps({"first": 100, "second": 10, "r9": 5})),
    )
    probabilities = json.dumps([0.7, 0.2, 0.1])
    for seq in range(1, 15):
        cursor = conn.execute(
            """INSERT INTO period_matches(
                   period_id, seq, home_name_cn, away_name_cn, odds_json
               ) VALUES(?, ?, ?, ?, NULL)""",
            (period_id, seq, f"主{seq}", f"客{seq}"),
        )
        conn.execute(
            """INSERT INTO predictions(
                   period_match_id, model_version, market_json, fused_json
               ) VALUES(?, 'test', ?, ?)""",
            (cursor.lastrowid, probabilities, probabilities),
        )
    conn.commit()

    monkeypatch.setattr(store, "connect", lambda _path: conn)
    monkeypatch.setattr(plans.Path, "write_text", lambda *args, **kwargs: 0)
    report = plans.run(db_path="ignored.db", budget=64, out_path="ignored.json")

    row = report["periods"][0]["market"]
    assert row["invested"] == row["sfc14"]["invested"] + row["r9"]["invested"]
    assert row["sfc14"]["prize"] == (
        row["sfc14"]["first_notes"] * 100 + row["sfc14"]["second_notes"] * 10
    )
    assert row["r9"]["prize"] == row["r9"]["r9_notes"] * 5
    assert report["summary"]["market"]["invested"] == row["invested"]
    assert report["summary"]["market"]["prize"] == row["prize"]
    assert report["budget_per_game"] == 64


def test_period_probs_uses_latest_prediction_version():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('001', '2026-01-01', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]
    old = json.dumps([0.2, 0.3, 0.5])
    new = json.dumps([0.7, 0.2, 0.1])
    for seq in range(1, 15):
        cursor = conn.execute(
            """INSERT INTO period_matches(
                   period_id, seq, home_name_cn, away_name_cn, odds_json
               ) VALUES(?, ?, ?, ?, NULL)""",
            (period_id, seq, f"主{seq}", f"客{seq}"),
        )
        pm_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO predictions(
                   period_match_id, model_version, market_json, fused_json, created_at
               ) VALUES(?, 'old', ?, ?, '2026-01-01T00:00:00')""",
            (pm_id, old, old),
        )
        conn.execute(
            """INSERT INTO predictions(
                   period_match_id, model_version, market_json, fused_json, created_at
               ) VALUES(?, 'new', ?, ?, '2026-01-02T00:00:00')""",
            (pm_id, new, new),
        )
    conn.commit()

    probs = plans._period_probs(conn, period_id, use_fused=True)

    assert probs == [json.loads(new)] * 14
