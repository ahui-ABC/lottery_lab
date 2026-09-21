import json
from argparse import Namespace

from lottery_lab import cli
from lottery_lab.db import store


def test_check_draw_is_idempotent_for_r9(monkeypatch, capsys):
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('001', '2026-01-01', 'historical')"
    )
    period_id = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        "INSERT INTO draw_results(period_id, results_json, prizes_json) VALUES(?, ?, ?)",
        (period_id, ",".join(["3"] * 14), json.dumps({"r9": 5})),
    )
    conn.execute(
        """INSERT INTO plans(
               period_id, game_type, objective, budget, notes_count,
               p_first, p_second, p_win, legs_json, created_at
           ) VALUES(?, 'r9', 'r9', 64, 1, NULL, NULL, 1.0, ?, datetime('now'))""",
        (period_id, json.dumps({"selected": list(range(9)), "legs": [["3"]] * 9})),
    )
    conn.commit()
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    args = Namespace(period=None)

    assert cli.cmd_check_draw(args, {}) == 0
    assert conn.execute("SELECT COUNT(*) FROM winnings").fetchone()[0] == 1
    assert cli.cmd_check_draw(args, {}) == 0
    assert conn.execute("SELECT COUNT(*) FROM winnings").fetchone()[0] == 1
    capsys.readouterr()
