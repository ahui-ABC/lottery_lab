"""football-data 采集器测试（不联网，使用 fixture）。"""
from pathlib import Path
from football_lottery.collectors import fd

FIX = Path(__file__).parent.parent / "fixtures" / "fd_E0_2425_sample.csv"


def test_parse_csv_returns_rows():
    text = FIX.read_text(encoding="utf-8-sig")
    rows = fd.parse_csv(text)
    assert len(rows) >= 3, f"样本至少 3 场，实际 {len(rows)}"
    r = rows[0]
    assert r["home"] and r["away"]
    assert r["result"] in {"H", "D", "A"}
    assert set(r["odds"]["avg"]) == {"h", "d", "a"}
    assert r["odds"]["avg"]["h"] > 1.0
    # 射门数据应解析为整数
    assert isinstance(r["shots"]["hs"], int)


def test_parse_date_dd_mm_yyyy_and_short():
    assert fd.parse_date("17/09/2025") == "2025-09-17"
    assert fd.parse_date("17/09/25") == "2025-09-17"


def test_collect_writes_to_db(tmp_path):
    """从 fixture 入库：upsert league/teams/matches 三张表都不报错。"""
    from football_lottery.db import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    fd.collect(conn, [(FIX, "E0", "2425")], silent=True)
    cnt_matches = conn.execute("SELECT COUNT(*) AS c FROM matches").fetchone()["c"]
    cnt_leagues = conn.execute("SELECT COUNT(*) AS c FROM leagues").fetchone()["c"]
    cnt_teams = conn.execute("SELECT COUNT(*) AS c FROM teams").fetchone()["c"]
    assert cnt_matches >= 3
    assert cnt_leagues >= 1
    assert cnt_teams >= 4   # 至少 4 个队（一场两队，3 场可能 6 队）
    # 幂等
    fd.collect(conn, [(FIX, "E0", "2425")], silent=True)
    cnt_matches2 = conn.execute("SELECT COUNT(*) AS c FROM matches").fetchone()["c"]
    assert cnt_matches2 == cnt_matches
