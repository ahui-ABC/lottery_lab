"""竞彩赔率接入测试（全部离线，用真实响应 fixture）。"""
import json
from pathlib import Path

import pytest

from football_lottery.collectors import sporttery
from football_lottery.db import store
from football_lottery.models import market

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture():
    return json.loads((FIXTURES / "sporttery_jc_had_20260920.json").read_text(encoding="utf-8"))


def _bydraw():
    """按期号查对阵的真实响应（value 直接是期次详情）。"""
    payload = json.loads(
        (FIXTURES / "sporttery_bydraw_26131.json").read_text(encoding="utf-8"))
    return payload["value"]


def _memory_db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _seed_period(conn, fixtures):
    """写入一个当期与若干对阵；fixtures = [(seq, home_cn, away_cn), ...]"""
    conn.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES('26131', '2026-09-21', '2099-01-01 20:30:00', 'current')"""
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    for seq, home, away in fixtures:
        conn.execute(
            """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn)
               VALUES(?, ?, ?, ?)""",
            (pid, seq, home, away),
        )
    conn.commit()
    return pid


# ---- 1. 解析 ---------------------------------------------------------------------
def test_parse_jc_odds_extracts_matches():
    got = sporttery.parse_jc_odds(_fixture())
    assert len(got) == 29
    first = got[0]
    assert {"home_cn", "away_cn", "league_cn", "h", "d", "a", "update_time"} <= set(first)
    # 赔率应是 float
    assert all(isinstance(m["h"], float) for m in got)
    names = {(m["home_cn"], m["away_cn"]) for m in got}
    assert ("伯恩茅斯", "利物浦") in names
    assert ("尤文图斯", "亚特兰大") in names


def test_parse_jc_odds_skips_matches_without_odds():
    payload = json.loads(json.dumps(_fixture()))
    sub = payload["value"]["matchInfoList"][0]["subMatchList"]
    sub[0]["had"] = {}                                  # 缺赔率
    sub[1]["had"]["h"] = ""                             # 空值
    sub[2]["had"]["d"] = "abc"                          # 非数字
    got = sporttery.parse_jc_odds(payload)
    assert len(got) == 26


def test_parse_jc_odds_handles_empty_list():
    assert sporttery.parse_jc_odds({"value": {"matchInfoList": []}}) == []
    assert sporttery.parse_jc_odds({}) == []


# ---- 2. 匹配 ---------------------------------------------------------------------
def test_match_to_period_uses_full_team_names():
    conn = _memory_db()
    _seed_period(conn, [
        (1, "伯恩茅斯", "利物浦"),
        (2, "利兹联", "水晶宫"),
        (3, "不存在的队", "另一支不存在的队"),
    ])
    jc = sporttery.parse_jc_odds(_fixture())

    result = sporttery.match_to_period(conn, "26131", jc)

    assert result["matched"] == 2
    assert result["unmatched"] == [3]


def test_match_to_period_matches_sales_day_midnight_games():
    """businessDate 是销售日：9/21 凌晨开赛的场次也要匹配上。"""
    conn = _memory_db()
    _seed_period(conn, [(1, "尤文图斯", "亚特兰大"), (2, "马赛", "巴黎圣日尔曼")])
    jc = sporttery.parse_jc_odds(_fixture())

    result = sporttery.match_to_period(conn, "26131", jc)

    assert result["matched"] == 2


def test_all_14_fixtures_of_26131_match_exactly():
    """真实数据回归：当期 14 场全部精确命中（两边队名同源）。"""
    conn = _memory_db()
    _seed_period(conn, [
        (1, "伯恩茅斯", "利物浦"), (2, "利兹联", "水晶宫"),
        (3, "曼彻斯特城", "桑德兰"), (4, "富勒姆", "曼彻斯特联"),
        (5, "勒沃库森", "莱比锡红牛"), (6, "沙尔克04", "埃尔沃斯堡"),
        (7, "弗洛西诺内", "科莫"), (8, "帕尔马", "热那亚"),
        (9, "尤文图斯", "亚特兰大"), (10, "AC米兰", "莱切"),
        (11, "马德里竞技", "皇家马德里"), (12, "拉科鲁尼亚", "皇家贝蒂斯"),
        (13, "巴伦西亚", "皇家社会"), (14, "马赛", "巴黎圣日尔曼"),
    ])
    jc = sporttery.parse_jc_odds(_fixture())

    result = sporttery.match_to_period(conn, "26131", jc)

    assert result["matched"] == 14
    assert result["unmatched"] == []


# ---- 3 & 4. 快照：无变化不写 / 有变化追加且不覆盖 -----------------------------------
def test_save_odds_snapshots_appends_only_on_change():
    conn = _memory_db()
    _seed_period(conn, [(1, "伯恩茅斯", "利物浦")])
    jc = sporttery.parse_jc_odds(_fixture())

    first = sporttery.save_odds_snapshots(conn, "26131", jc)
    assert first["changed"] == 1
    rows_after_first = conn.execute("SELECT COUNT(*) c FROM odds_snapshots").fetchone()["c"]
    assert rows_after_first == 1

    # 同一份赔率再处理一次 —— 不得追加
    second = sporttery.save_odds_snapshots(conn, "26131", jc)
    assert second["changed"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM odds_snapshots").fetchone()["c"] == rows_after_first


def test_save_odds_snapshots_appends_on_change_without_overwriting():
    conn = _memory_db()
    _seed_period(conn, [(1, "伯恩茅斯", "利物浦")])
    jc = sporttery.parse_jc_odds(_fixture())
    sporttery.save_odds_snapshots(conn, "26131", jc)

    before = conn.execute(
        "SELECT h, d, a FROM odds_snapshots ORDER BY id"
    ).fetchall()
    assert len(before) == 1

    # 改一个赔率值
    changed = json.loads(json.dumps(jc))
    for m in changed:
        if (m["home_cn"], m["away_cn"]) == ("伯恩茅斯", "利物浦"):
            m["h"] = m["h"] + 0.5
    out = sporttery.save_odds_snapshots(conn, "26131", changed)
    assert out["changed"] == 1

    rows = conn.execute("SELECT h, d, a FROM odds_snapshots ORDER BY id").fetchall()
    assert len(rows) == 2, "变化必须追加新行，而不是覆盖旧行"
    assert rows[0]["h"] == before[0]["h"], "旧快照必须原样保留"


def test_snapshot_uses_plain_insert_not_upsert():
    """同一 captured_at 重复写入必须报错，而不是被 upsert 静默覆盖。"""
    conn = _memory_db()
    _seed_period(conn, [(1, "伯恩茅斯", "利物浦")])
    pm_id = conn.execute("SELECT id FROM period_matches").fetchone()["id"]
    conn.execute(
        """INSERT INTO odds_snapshots(period_match_id, source, captured_at, h, d, a)
           VALUES(?, 'jc', '2026-09-20T10:00:00.000000', 1.0, 2.0, 3.0)""",
        (pm_id,),
    )
    conn.commit()

    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO odds_snapshots(period_match_id, source, captured_at, h, d, a)
               VALUES(?, 'jc', '2026-09-20T10:00:00.000000', 9.0, 9.0, 9.0)""",
            (pm_id,),
        )


# ---- 常驻采集：自动跟进新期次 -------------------------------------------------------
def test_ensure_current_period_creates_when_absent(monkeypatch):
    conn = _memory_db()
    monkeypatch.setattr(
        sporttery, "fetch_current_period",
        lambda: {"period_no": "26131", "sale_end": "2026-09-20 20:30:00",
                 "draw_time": "2026-09-21 14:00:00"},
    )
    monkeypatch.setattr(sporttery, "fetch_period_detail", lambda period_no: _bydraw())

    got = sporttery.ensure_current_period(conn)

    assert got == {"period_no": "26131", "created": True, "fixtures": 14}
    row = conn.execute("SELECT status, sale_end FROM periods WHERE period_no='26131'").fetchone()
    assert row["status"] == "current"
    assert row["sale_end"] == "2026-09-20 20:30:00"


def test_ensure_current_period_skips_when_already_complete(monkeypatch):
    conn = _memory_db()
    _seed_period(conn, [(i, f"主{i}", f"客{i}") for i in range(1, 15)])
    monkeypatch.setattr(
        sporttery, "fetch_current_period",
        lambda: {"period_no": "26131", "sale_end": "2026-09-20 20:30:00",
                 "draw_time": None},
    )
    called = {"detail": 0}

    def _detail(period_no):
        called["detail"] += 1
        return {}

    monkeypatch.setattr(sporttery, "fetch_period_detail", _detail)

    got = sporttery.ensure_current_period(conn)

    assert got["created"] is False
    assert got["fixtures"] == 14
    assert called["detail"] == 0, "对阵已齐时不应再请求详情接口"


def test_ensure_current_period_returns_none_when_no_onsale(monkeypatch):
    conn = _memory_db()
    monkeypatch.setattr(sporttery, "fetch_current_period", lambda: None)
    assert sporttery.ensure_current_period(conn) is None


# ---- 5. odds_json 合并（保留既有键） ------------------------------------------------
def test_save_odds_snapshots_merges_odds_json_preserving_existing_keys():
    conn = _memory_db()
    _seed_period(conn, [(1, "伯恩茅斯", "利物浦")])
    conn.execute(
        """UPDATE period_matches SET odds_json='{"avg":{"h":9.9,"d":8.8,"a":7.7}}'"""
    )
    conn.commit()

    jc = sporttery.parse_jc_odds(_fixture())
    sporttery.save_odds_snapshots(conn, "26131", jc)

    raw = conn.execute("SELECT odds_json FROM period_matches").fetchone()["odds_json"]
    merged = json.loads(raw)
    assert "jc" in merged, "必须写入 jc 赔率"
    assert "avg" in merged, "既有键不得被整体替换"
    assert merged["avg"]["h"] == 9.9
    assert merged["jc"]["h"] > 0


# ---- 6. devig 优先使用 jc ----------------------------------------------------------
def test_devig_prefers_jc_over_avg():
    payload = {
        "jc": {"h": 2.00, "d": 3.10, "a": 3.26},
        "avg": {"h": 1.01, "d": 1.01, "a": 1.01},   # 若被选中会产出极端概率
    }
    probs = market.devig(payload)
    assert probs is not None
    # jc 去水后主胜约 0.45，而 avg 会给出接近均匀但更偏的值；用范围区分
    assert 0.40 < probs[0] < 0.50


def test_devig_falls_back_to_avg_for_historical_periods():
    payload = {"avg": {"h": 2.00, "d": 3.10, "a": 3.26}}
    probs = market.devig(payload)
    assert probs is not None
    assert 0.40 < probs[0] < 0.50


# ---- 7. 端到端：当期缺赔率数归零 ----------------------------------------------------
def test_current_period_missing_odds_drops_to_zero():
    from football_lottery.cli import data_health

    conn = _memory_db()
    _seed_period(conn, [
        (1, "伯恩茅斯", "利物浦"),
        (2, "利兹联", "水晶宫"),
    ])
    assert data_health(conn)["current_period_missing_odds"] == 2

    sporttery.save_odds_snapshots(conn, "26131", sporttery.parse_jc_odds(_fixture()))

    assert data_health(conn)["current_period_missing_odds"] == 0
