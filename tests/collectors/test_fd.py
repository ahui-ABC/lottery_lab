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


def test_fetch_and_collect_skips_past_seasons(tmp_path, monkeypatch):
    """增量：过去的赛季已经踢完、CSV 不会再变，已入库的直接跳过。

    否则每点一次「抓联赛历史数据」都要重下 赛季数 × 联赛数 个文件。
    """
    from football_lottery.db import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)

    downloaded = []

    def fake_download(season, div, timeout=30):
        downloaded.append((season, div))
        return FIX.read_text(encoding="utf-8-sig")

    monkeypatch.setattr(fd, "download", fake_download)
    monkeypatch.setattr(fd, "_SEASON_FLOOR", 3)     # 夹具只有 3 场

    # 第一次：三个赛季都下
    fd.fetch_and_collect(conn, ["2324", "2425", "2526"], ["E0"], silent=True)
    assert downloaded == [("2324", "E0"), ("2425", "E0"), ("2526", "E0")]

    # 第二次：前两个过去赛季已入库 → 只重下最后一个（当前赛季）
    downloaded.clear()
    fd.fetch_and_collect(conn, ["2324", "2425", "2526"], ["E0"], silent=True)
    assert downloaded == [("2526", "E0")]

    # force=True：全部重下
    downloaded.clear()
    fd.fetch_and_collect(conn, ["2324", "2425", "2526"], ["E0"], silent=True, force=True)
    assert len(downloaded) == 3


def test_season_floor_protects_against_partial_data(tmp_path):
    """半截数据不能把赛季冻住 —— 场次数不到下限时仍要重下。"""
    from football_lottery.db import store
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    assert fd._season_loaded(conn, "2324", "E0") is False     # 空库
