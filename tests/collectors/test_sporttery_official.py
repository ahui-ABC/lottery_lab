import json
from pathlib import Path

import pytest

from football_lottery.collectors import sporttery

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _memory_db():
    from football_lottery.db import store

    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


# ---- Task 2: 抓取层 ---------------------------------------------------------------
def test_collector_error_carries_error_code():
    err = sporttery.CollectorError("参数不合法", error_code="P0001")
    assert err.error_code == "P0001"
    assert "参数不合法" in str(err)


def test_get_json_raises_on_business_error(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"errorCode": "P0001", "errorMessage": "参数不合法", "success": False}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {"lotteryGameNum": "90"})
    assert excinfo.value.error_code == "P0001"


def test_get_json_raises_on_non_200(monkeypatch):
    class _Resp:
        status_code = 502

        def json(self):
            return {}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {})
    assert "502" in str(excinfo.value)


def test_get_json_raises_on_network_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(sporttery, "_http_get", _boom)

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {})
    assert "connection reset" in str(excinfo.value)


def test_get_json_passes_through_on_success(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"errorCode": "0", "success": True, "value": [1, 2, 3]}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    body = sporttery._get_json("ok.qry", {})
    assert body["value"] == [1, 2, 3]


def test_fetch_current_period_parses_onsale(monkeypatch):
    monkeypatch.setattr(
        sporttery, "_get_json",
        lambda path, params, **k: _fixture("sporttery_saleinfo_90.json"),
    )
    got = sporttery.fetch_current_period()
    assert got["period_no"] == "26131"
    assert got["sale_end"] == "2026-09-20 20:30:00"
    assert got["draw_time"].startswith("2026-09-21")


def test_fetch_current_period_returns_none_when_no_onsale(monkeypatch):
    monkeypatch.setattr(
        sporttery, "_get_json",
        lambda path, params, **k: {"errorCode": "0", "value": []},
    )
    assert sporttery.fetch_current_period() is None


# ---- Task 3: 解析层 ---------------------------------------------------------------
def test_parse_period_uses_full_team_names_and_normalizes_spaces():
    detail = _fixture("sporttery_bydraw_26131.json")["value"]
    parsed = sporttery.parse_period(detail, status="current")

    assert parsed["period"]["period_no"] == "26131"
    assert parsed["period"]["status"] == "current"
    assert parsed["period"]["sale_end"] == "2026-09-20 20:30:00"
    assert parsed["period"]["draw_date"] == "2026-09-21"
    assert len(parsed["fixtures"]) == 14

    by_seq = {f["seq"]: f for f in parsed["fixtures"]}
    assert by_seq[1]["home_name_cn"] == "伯恩茅斯"
    assert by_seq[1]["away_name_cn"] == "利物浦"
    assert by_seq[1]["league_cn"] == "英超"
    # 官方简称含空格填充（如 "利  兹"），全名不得残留连续或全角空格
    for fx in parsed["fixtures"]:
        assert fx["home_name_cn"] == " ".join(fx["home_name_cn"].split())
        assert fx["away_name_cn"] == " ".join(fx["away_name_cn"].split())
        assert fx["home_name_cn"] and fx["away_name_cn"]


def test_parse_period_rejects_non_14_fixtures():
    detail = dict(_fixture("sporttery_bydraw_26131.json")["value"])
    detail["matchList"] = detail["matchList"][:13]

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery.parse_period(detail, status="current")
    assert "14" in str(excinfo.value)


def test_parse_period_rejects_bad_seq():
    detail = dict(_fixture("sporttery_bydraw_26131.json")["value"])
    broken = [dict(m) for m in detail["matchList"]]
    broken[3]["matchNum"] = 99
    detail["matchList"] = broken

    with pytest.raises(sporttery.CollectorError):
        sporttery.parse_period(detail, status="current")


def test_parse_draw_results_converts_result_format_and_prizes():
    item = _fixture("sporttery_history_90.json")["value"]["list"][0]
    got = sporttery.parse_draw_results(item)

    assert got is not None
    parts = got["results_json"].split(",")
    assert len(parts) == 14
    assert all(p in {"0", "1", "3"} for p in parts)
    assert " " not in got["results_json"]

    prizes = json.loads(got["prizes_json"])
    assert isinstance(prizes["first"], float)
    assert isinstance(prizes["second"], float)
    assert prizes["second"] != "26,526"


def test_parse_draw_results_returns_none_when_undrawn():
    item = _fixture("sporttery_bydraw_26131.json")["value"]
    assert sporttery.parse_draw_results(item) is None


def test_parse_draw_results_returns_none_when_result_has_placeholder():
    """官方对取消/延期的场次用 '*' 占位，此类赛果不得入库。"""
    page = _fixture("sporttery_history_90.json")["value"]["list"]
    starred = [it for it in page if "*" in (it.get("lotteryDrawResult") or "")]
    assert starred, "fixture 应至少含一条带 '*' 的期次"
    for item in starred:
        assert sporttery.parse_draw_results(item) is None


# ---- Task 4: 入库层 ---------------------------------------------------------------
def test_upsert_period_writes_period_and_14_fixtures():
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )

    pid = sporttery.upsert_period(conn, parsed)

    row = conn.execute("SELECT * FROM periods WHERE id=?", (pid,)).fetchone()
    assert row["period_no"] == "26131"
    assert row["status"] == "current"
    assert row["sale_end"] == "2026-09-20 20:30:00"

    fixtures = list(conn.execute(
        "SELECT seq, home_name_cn, league_cn FROM period_matches WHERE period_id=? ORDER BY seq",
        (pid,),
    ))
    assert len(fixtures) == 14
    assert fixtures[0]["league_cn"] == "英超"


def test_upsert_period_is_idempotent():
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )

    sporttery.upsert_period(conn, parsed)
    sporttery.upsert_period(conn, parsed)

    assert conn.execute("SELECT COUNT(*) c FROM periods").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"] == 14


def test_upsert_period_preserves_existing_match_mapping():
    """重跑 collect-period 不得清空已映射的 match_id / odds_json。"""
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )
    pid = sporttery.upsert_period(conn, parsed)

    # match_id 有外键约束，必须先有真实的 matches 行
    conn.execute("INSERT INTO leagues(code, name_cn) VALUES('E0','英超')")
    conn.execute("INSERT INTO teams(id, name_en) VALUES(1,'Team A')")
    conn.execute("INSERT INTO teams(id, name_en) VALUES(2,'Team B')")
    conn.execute(
        """INSERT INTO matches(id, league_code, season, match_date,
                               home_team_id, away_team_id)
           VALUES(999, 'E0', '2627', '2026-09-20', 1, 2)"""
    )
    conn.execute(
        """UPDATE period_matches SET match_id=999, odds_json='{"avg":{"h":2.0}}'
           WHERE period_id=? AND seq=1""",
        (pid,),
    )
    conn.commit()

    sporttery.upsert_period(conn, parsed)

    row = conn.execute(
        "SELECT match_id, odds_json FROM period_matches WHERE period_id=? AND seq=1", (pid,)
    ).fetchone()
    assert row["match_id"] == 999
    assert row["odds_json"] == '{"avg":{"h":2.0}}'


def test_upsert_period_demotes_previous_current():
    conn = _memory_db()
    first = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )
    sporttery.upsert_period(conn, first, demote_others=True)

    other = json.loads(json.dumps(first))
    other["period"]["period_no"] = "26132"
    sporttery.upsert_period(conn, other, demote_others=True)

    rows = {r["period_no"]: r["status"] for r in conn.execute("SELECT period_no, status FROM periods")}
    assert rows == {"26131": "historical", "26132": "current"}


def test_collect_history_writes_only_complete_periods(monkeypatch):
    """fixture 中 26127/26106 的赛果含 '*'（取消场次），整期跳过。

    数字 28/2 取自 fixtures/sporttery_history_90.json（共 30 期）。
    若重新抓取 fixture 导致期数变化，按实际值调整断言。
    """
    conn = _memory_db()
    page = _fixture("sporttery_history_90.json")["value"]
    total = len(page["list"])

    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: page if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    out = sporttery.collect_history(conn, years=4)

    assert out["periods_saved"] == 28
    assert out["skipped"] == 2
    assert out["periods_saved"] + out["skipped"] == total

    # 入库的期次必有开奖（对阵 / 期次 / 开奖 三者数量一致）
    assert conn.execute("SELECT COUNT(*) c FROM periods").fetchone()["c"] == 28
    assert conn.execute("SELECT COUNT(*) c FROM draw_results").fetchone()["c"] == 28
    assert conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"] == 28 * 14
    assert conn.execute("SELECT COUNT(*) c FROM periods WHERE status='current'").fetchone()["c"] == 0

    prizes = conn.execute("SELECT prizes_json FROM draw_results LIMIT 1").fetchone()["prizes_json"]
    assert prizes and "first" in json.loads(prizes)


def test_collect_history_counts_short_match_lists_as_skipped(monkeypatch):
    conn = _memory_db()
    page = _fixture("sporttery_history_90.json")["value"]
    broken = json.loads(json.dumps(page))
    broken["list"][0]["matchList"] = broken["list"][0]["matchList"][:13]

    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: broken if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    out = sporttery.collect_history(conn, years=4)

    # 1 条对阵残缺 + 2 条赛果含 '*' = 3
    assert out["skipped"] == 3
    assert out["periods_saved"] == len(page["list"]) - 3
