from lottery_lab.db import store


def test_init_and_roundtrip(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.upsert(conn, "leagues", {"code": "E0", "name_cn": "英超", "name_en": "Premier League"}, ["code"])
    store.upsert(conn, "teams", {"name_en": "Arsenal", "name_cn": "阿森纳"}, ["name_en"])
    row = conn.execute("SELECT name_cn FROM leagues WHERE code='E0'").fetchone()
    assert row["name_cn"] == "英超"


def test_upsert_idempotent_updates_fields(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    store.upsert(conn, "leagues", {"code": "E0", "name_cn": "英超", "name_en": "Premier League"}, ["code"])
    store.upsert(
        conn,
        "leagues",
        {"code": "E0", "name_cn": "英格兰超级联赛", "name_en": "Premier League"},
        ["code"],
    )
    row = conn.execute("SELECT name_cn FROM leagues WHERE code='E0'").fetchone()
    assert row["name_cn"] == "英格兰超级联赛"
    cnt = conn.execute("SELECT COUNT(*) AS c FROM leagues").fetchone()["c"]
    assert cnt == 1


def test_foreign_keys_enabled(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    # 无 teams 行的 FK 应当失败
    import sqlite3
    raised = False
    try:
        conn.execute(
            "INSERT INTO team_alias(name_cn,team_id) VALUES('foo', 9999)"
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_schema_has_all_tables(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {r["name"] for r in rows}
    for t in [
        "leagues", "teams", "team_alias",
        "matches", "periods", "period_matches",
        "predictions", "plans", "draw_results", "winnings",
        "model_versions",
    ]:
        assert t in tables, f"缺少表 {t}"
