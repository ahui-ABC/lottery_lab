from football_lottery.db import store


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


OLD_SCHEMA_NO_LEAGUE = """
CREATE TABLE periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_no TEXT NOT NULL UNIQUE,
  draw_date TEXT, sale_end TEXT, status TEXT);
CREATE TABLE period_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  seq INTEGER NOT NULL,
  home_name_cn TEXT NOT NULL, away_name_cn TEXT NOT NULL,
  match_time TEXT,
  match_id INTEGER,
  odds_json TEXT,
  UNIQUE(period_id, seq));
"""


def test_ensure_columns_adds_league_cn_idempotently():
    # 手工构造"迁移前"的旧库：init_db 现在会连带迁移，
    # 所以不能用 init_db 来验证迁移本身。
    conn = store.connect(":memory:")
    conn.executescript(OLD_SCHEMA_NO_LEAGUE)
    assert "league_cn" not in _columns(conn, "period_matches")

    store.ensure_columns(conn)
    assert "league_cn" in _columns(conn, "period_matches")

    # 再跑一次不得报错（幂等）
    store.ensure_columns(conn)
    assert "league_cn" in _columns(conn, "period_matches")


def test_ensure_columns_preserves_existing_rows():
    conn = store.connect(":memory:")
    conn.executescript(OLD_SCHEMA_NO_LEAGUE)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26080','2026-05-25','historical')"
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn)
           VALUES(?, 1, '伯恩利', '狼队')""",
        (pid,),
    )
    conn.commit()

    store.ensure_columns(conn)

    row = conn.execute(
        "SELECT home_name_cn, league_cn FROM period_matches WHERE period_id=?", (pid,)
    ).fetchone()
    assert row["home_name_cn"] == "伯恩利"
    assert row["league_cn"] is None


def test_init_db_creates_league_cn_for_fresh_database():
    conn = store.connect(":memory:")
    store.init_db(conn)
    assert "league_cn" in _columns(conn, "period_matches")
