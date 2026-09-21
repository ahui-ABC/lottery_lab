import json
import sqlite3

from football_lottery.db import store


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def test_lottery_tables_created():
    conn = _conn()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lottery_draw", "lottery_prediction", "lottery_backtest"} <= names
