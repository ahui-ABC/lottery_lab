"""竞彩历史同步测试（全部离线，用真实响应 fixture + 打桩）。"""
import json
from pathlib import Path

import pytest

from lottery_lab.collectors import jc_history, sporttery
from lottery_lab.db import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixed_bonus_value():
    payload = json.loads(
        (FIXTURES / "sporttery_fixed_bonus_sample.json").read_text(encoding="utf-8"))
    return payload["value"]


def _uniform_value():
    payload = json.loads(
        (FIXTURES / "sporttery_uniform_result_sample.json").read_text(encoding="utf-8"))
    return payload["value"]


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


# ---- 1. 解析 ---------------------------------------------------------------------
def test_parse_fixed_bonus_expands_all_pools_and_changes():
    parsed = jc_history.parse_fixed_bonus(_fixed_bonus_value())

    pools = {row[0] for row in parsed["odds"]}
    assert pools == {"had", "hhad", "crs", "ttg", "hafu"}

    by_pool = {}
    for pool, seq, *_ in parsed["odds"]:
        by_pool.setdefault(pool, []).append(seq)
    # seq 从 0 连续递增，长度 = 该玩法的变化次数
    for pool, seqs in by_pool.items():
        assert seqs == list(range(len(seqs))), pool

    # crs 的完整选项被保留（32 个比分 + 其他比分），不是被截断
    crs_json = next(r[5] for r in parsed["odds"] if r[0] == "crs")
    crs = json.loads(crs_json)
    assert len(crs) >= 30
    assert any(k.startswith("s0") for k in crs)


def test_parse_fixed_bonus_keeps_update_metadata():
    parsed = jc_history.parse_fixed_bonus(_fixed_bonus_value())
    had = next(r for r in parsed["odds"] if r[0] == "had")
    assert had[2]        # update_date
    assert had[3]        # update_time
    # 涨跌标志留在 JSON 里（hf/df/af）
    payload = json.loads(had[5])
    assert any(k.endswith("f") for k in payload)


def test_parse_fixed_bonus_dispatches_result_codes():
    parsed = jc_history.parse_fixed_bonus(_fixed_bonus_value())
    results = parsed["results"]
    # fixture 的 matchResultList 共 5 项，code 为 HAD/HHAD/CRS/TTG/HAFU
    assert set(results) == {"result_had", "result_hhad", "result_crs",
                            "result_ttg", "result_hafu"}
    assert all(isinstance(v, str) and v for v in results.values())


# ---- 2. 空值处理 ------------------------------------------------------------------
def test_num_maps_empty_string_to_none():
    assert jc_history._num("") is None
    assert jc_history._num("  ") is None
    assert jc_history._num(None) is None
    assert jc_history._num("abc") is None
    assert jc_history._num("1.85") == 1.85


def test_parse_match_result_writes_null_for_unpriced_matches():
    value = {"matchResult": [{
        "matchId": 1, "matchDate": "2021-01-01", "h": "", "d": "", "a": "",
        "leagueName": "X", "allHomeTeam": "A", "allAwayTeam": "B",
    }]}
    row = jc_history.parse_match_result(value)[0]
    assert row["had_h"] is None and row["had_d"] is None and row["had_a"] is None


# ---- 3-5. 入库 / 幂等 / 断点续传 ----------------------------------------------------
def _stub_fetches(monkeypatch, calls=None):
    """打桩两个接口：列表返回 2 场，单场返回同一份 fixture。"""
    value = _uniform_value()
    rows = [m for m in jc_history.parse_match_result(value)][:2]

    monkeypatch.setattr(jc_history.sporttery, "fetch_uniform_match_result",
                        lambda *a, **k: {"matchResult": [
                            {"matchId": r["match_id"], "matchDate": "2021-01-01",
                             "h": "1.9", "d": "3.3", "a": "3.5",
                             "leagueName": "L", "allHomeTeam": r["home_team"],
                             "allAwayTeam": r["away_team"]} for r in rows],
                            "pages": 1, "total": len(rows)})

    def _fake_bonus(match_id):
        if calls is not None:
            calls.append(match_id)
        return _fixed_bonus_value()

    monkeypatch.setattr(jc_history.sporttery, "fetch_fixed_bonus", _fake_bonus)
    return rows


def test_sync_day_writes_matches_and_odds(monkeypatch):
    conn = _db()
    _stub_fetches(monkeypatch)

    out = jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)

    assert out["matches"] == 2
    assert out["odds_rows"] > 0
    assert conn.execute("SELECT COUNT(*) c FROM jc_matches").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM jc_odds_history").fetchone()["c"] == out["odds_rows"]
    # 开奖结果回填
    row = conn.execute("SELECT result_had FROM jc_matches LIMIT 1").fetchone()
    assert row["result_had"]


def test_sync_day_is_idempotent(monkeypatch):
    conn = _db()
    _stub_fetches(monkeypatch)

    jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)
    first = conn.execute("SELECT COUNT(*) c FROM jc_odds_history").fetchone()["c"]
    jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)

    assert conn.execute("SELECT COUNT(*) c FROM jc_odds_history").fetchone()["c"] == first
    assert conn.execute("SELECT COUNT(*) c FROM jc_matches").fetchone()["c"] == 2


def test_sync_range_skips_completed_days(monkeypatch):
    conn = _db()
    calls = []
    _stub_fetches(monkeypatch, calls)

    jc_history.sync_range(conn, "2021-01-01", "2021-01-02", workers=1, delay=0,
                          refresh_days=0)
    first_calls = len(calls)

    out = jc_history.sync_range(conn, "2021-01-01", "2021-01-02", workers=1, delay=0,
                                refresh_days=0)

    assert out["skipped"] == 2 and out["days"] == 0
    assert len(calls) == first_calls, "已完成的日期不应再请求"


def test_refresh_window_forces_recent_days(monkeypatch):
    conn = _db()
    calls = []
    _stub_fetches(monkeypatch, calls)

    jc_history.sync_range(conn, "2021-01-01", "2021-01-03", workers=1, delay=0,
                          refresh_days=0)
    before = len(calls)
    # 刷新窗口锚定 --to：只重跑 2021-01-03
    out = jc_history.sync_range(conn, "2021-01-01", "2021-01-03", workers=1, delay=0,
                                refresh_days=1)

    assert out["days"] == 1
    assert out["skipped"] == 2
    assert len(calls) > before


def test_retry_failed_only_touches_failed_days(monkeypatch):
    conn = _db()
    _stub_fetches(monkeypatch)
    jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)
    # 人为标记某日失败
    conn.execute("UPDATE jc_sync_log SET failed=3 WHERE sync_date='2021-01-01'")
    conn.commit()
    out = jc_history.sync_range(conn, "2021-01-01", "2021-01-05", workers=1, delay=0,
                                retry_failed=True)
    assert out["days"] == 1, "只应重跑失败的那一天"


# ---- 6. 容错 ---------------------------------------------------------------------
def test_sync_day_records_failed_match_and_continues(monkeypatch):
    conn = _db()
    rows = _stub_fetches(monkeypatch)
    bad_id = rows[0]["match_id"]

    def _boom(match_id):
        if match_id == bad_id:
            raise RuntimeError("network down")
        return _fixed_bonus_value()

    monkeypatch.setattr(jc_history.sporttery, "fetch_fixed_bonus", _boom)

    out = jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)

    assert out["failed"] == 1
    assert out["failed_ids"] == [bad_id]
    # 另一场仍然入库
    assert conn.execute("SELECT COUNT(*) c FROM jc_matches").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM jc_odds_history").fetchone()["c"] > 0


def test_sync_day_skips_matches_without_odds(monkeypatch):
    """比赛取消/未开售（无 oddsHistory）视为正常跳过，不计入 failed。"""
    conn = _db()
    _stub_fetches(monkeypatch)
    monkeypatch.setattr(jc_history.sporttery, "fetch_fixed_bonus", lambda mid: {})

    out = jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)

    assert out["failed"] == 0
    assert out["odds_rows"] == 0


# ---- 7. 分页 ---------------------------------------------------------------------
def test_sync_day_fetches_all_pages(monkeypatch):
    conn = _db()
    seen_pages = []

    def _fake_list(begin, end, page_no, page_size):
        seen_pages.append(page_no)
        if page_no > 2:
            return {"matchResult": [], "pages": 2}
        return {"matchResult": [{
            "matchId": 100 + page_no, "matchDate": begin, "h": "2", "d": "3", "a": "4",
            "leagueName": "L", "allHomeTeam": "A", "allAwayTeam": "B"}],
            "pages": 2, "total": 2}

    monkeypatch.setattr(jc_history.sporttery, "fetch_uniform_match_result", _fake_list)
    monkeypatch.setattr(jc_history.sporttery, "fetch_fixed_bonus", lambda mid: {})

    out = jc_history.sync_day(conn, "2021-01-01", workers=1, delay=0)

    assert seen_pages == [1, 2]
    assert out["matches"] == 2


# ---- 8. 并发一致性 ----------------------------------------------------------------
def test_workers_do_not_change_stored_data(monkeypatch):
    """并发只影响速度，不影响内容。按业务键比较（自增 id / 时间戳必然不同）。"""
    def _snapshot(conn):
        odds = conn.execute(
            """SELECT match_id, pool, update_date, update_time, odds_json
               FROM jc_odds_history ORDER BY match_id, pool, seq""").fetchall()
        matches = conn.execute(
            """SELECT match_id, result_had, result_hhad, result_crs,
                      result_ttg, result_hafu
               FROM jc_matches ORDER BY match_id""").fetchall()
        return [tuple(r) for r in odds], [tuple(r) for r in matches]

    conn1 = _db()
    _stub_fetches(monkeypatch)
    jc_history.sync_day(conn1, "2021-01-01", workers=1, delay=0)
    serial = _snapshot(conn1)

    conn4 = _db()
    _stub_fetches(monkeypatch)
    jc_history.sync_day(conn4, "2021-01-01", workers=4, delay=0)
    concurrent = _snapshot(conn4)

    assert serial == concurrent


# ---- 9. 熔断 ---------------------------------------------------------------------
def test_circuit_breaker_reduces_workers_then_aborts(monkeypatch):
    conn = _db()
    _stub_fetches(monkeypatch)
    # 全部失败
    monkeypatch.setattr(jc_history.sporttery, "fetch_fixed_bonus",
                        lambda mid: (_ for _ in ()).throw(RuntimeError("blocked")))

    events = []
    with pytest.raises(jc_history.CircuitBreakerTripped):
        jc_history.sync_range(conn, "2021-01-01", "2021-06-30", workers=4, delay=0,
                              refresh_days=0,
                              progress=lambda r: events.append(r) if r.get("breaker") else None)

    assert events, "应至少触发一次降级"
    assert events[0]["workers"] == 2, "首次降级应为一半"
