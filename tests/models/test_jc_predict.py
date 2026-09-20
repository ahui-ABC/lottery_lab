"""竞彩预测与对奖测试（全离线）。"""
import json
from pathlib import Path

import pytest

from football_lottery.db import store
from football_lottery.models import jc_predict

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _db():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _snapshot(h, d, a, date="2026-09-20", time="10:00:00"):
    return {"h": str(h), "d": str(d), "a": str(a),
            "hf": "0", "df": "0", "af": "0",
            "goalLine": "", "updateDate": date, "updateTime": time}


# ---- 1. 去水 ---------------------------------------------------------------------
def test_devig_n_handles_various_option_counts():
    for n in (3, 8, 9, 31):
        probs = jc_predict.devig_n([2.0] * n)
        assert probs is not None
        assert len(probs) == n
        assert abs(sum(probs) - 1.0) < 1e-6


def test_devig_n_rejects_invalid_input():
    assert jc_predict.devig_n([]) is None
    assert jc_predict.devig_n([2.0]) is None            # 单选项无法比较
    assert jc_predict.devig_n([2.0, 0]) is None         # 非正
    assert jc_predict.devig_n([2.0, "abc", 3.0]) is None


# ---- 2. 关键回归：路径不能退化成彼此 ------------------------------------------------
def _degen_series():
    """构造一组赔率，使 market / trend / blend 各自选出不同选项。

    o_0 = 2.20 / 3.00 / 7.00 ; o_n = 2.40 / 3.30 / 3.00
      market → h（概率最高）
      trend  → a（赔率从 7.00 掉到 3.00，资金大幅流入）
      blend  → h（a 的波动太大被惩罚）
    """
    return [_snapshot(2.20, 3.00, 7.00, time="09:00:00"),
            _snapshot(2.40, 3.30, 3.00, time="12:00:00")]


def test_paths_do_not_degenerate():
    """核心回归：各路径必须能给出**不同的决策**，否则记录再多天也分不出高下。

    2026-09-20 实测发现 α=0.5 时 5 条路径 pick 完全一致（分歧 0/26），
    路径在决策层面退化 —— 这类测试正是为了拦住这种情况。
    """
    signals = {"had": jc_predict._signals(_degen_series())}
    rows = {r["method"]: r for r in jc_predict.analyze_match(signals)}

    assert rows["market"]["pick"] == "h"
    assert rows["trend"]["pick"] == "a", "trend 必须能改变 argmax，否则等同于 market"
    assert len({rows["market"]["pick"], rows["trend"]["pick"]}) > 1


def test_blend_incorporates_cross_signal():
    """blend 是唯一的「全信号综合」路径，必须比 trend 多用了 cross 的印证。"""
    had_only = {"had": jc_predict._signals(_degen_series())}
    with_cross = {
        "had": jc_predict._signals(_degen_series()),
        "hafu": jc_predict._signals(_hafu_series()),
        "crs": jc_predict._signals(_crs_series()),
    }

    # 只看 had —— analyze_match 会返回所有玩法的行，按 method 建字典时
    # 后面的 pool 会覆盖前面的，必须按 pool 过滤
    a = {r["method"]: r for r in jc_predict.analyze_match(had_only) if r["pool"] == "had"}
    b = {r["method"]: r for r in jc_predict.analyze_match(with_cross) if r["pool"] == "had"}

    # 波动惩罚始终生效：即使没有 cross，blend 也不等于 trend
    assert a["blend"]["prob"] != a["trend"]["prob"]

    # 无 hafu/crs → 无从推导，blend 不记 provenance
    assert a["blend"]["provenance_json"] is None
    # 有 hafu/crs → cross 印证被 blend 消费，并留下可归因的中间量
    assert b["blend"]["provenance_json"], "blend 应把 cross 印证计入并记录推导中间量"
    assert "derived" in json.loads(b["blend"]["provenance_json"])


def test_stable_filters_when_any_option_volatile():
    signals = {"had": jc_predict._signals(_degen_series())}
    rows = {r["method"]: r for r in jc_predict.analyze_match(signals)}
    # a 从 7.00 变到 3.00，变异系数约 0.4 > 0.15
    assert rows["stable"]["pick"] is None


def test_stable_equals_market_when_series_is_flat():
    flat = [_snapshot(2.0, 3.0, 4.0, time="09:00:00"),
            _snapshot(2.0, 3.0, 4.0, time="12:00:00")]
    signals = {"had": jc_predict._signals(flat)}
    rows = {r["method"]: r for r in jc_predict.analyze_match(signals)}
    assert rows["stable"]["pick"] == rows["market"]["pick"]


# ---- 3. cross 推导 ---------------------------------------------------------------
def _hafu_series(probs_intensity="h"):
    """半全场赔率，使全场主胜占优。"""
    base = {"hh": 3.0, "hd": 12.0, "ha": 30.0,
            "dh": 5.0, "dd": 9.0, "da": 15.0,
            "ah": 20.0, "ad": 25.0, "aa": 8.0}
    if probs_intensity == "a":
        base = {k: (1.0 / v) for k, v in base.items()}   # 反转让客胜占优
        base = {k: round(v, 2) for k, v in base.items()}
    entry = {**{k: str(v) for k, v in base.items()},
             "goalLine": "", "updateDate": "2026-09-20", "updateTime": "10:00:00"}
    return [entry, entry]


def _crs_series():
    """比分赔率，含「其他比分」桶。"""
    entry = {"s01s00": "5.0", "s02s00": "6.0", "s02s01": "8.0",
             "s00s00": "9.0", "s01s01": "7.0",
             "s00s01": "12.0", "s01s02": "15.0",
             "s-1sh": "20.0", "s-1sd": "25.0", "s-1sa": "18.0",
             "goalLine": "", "updateDate": "2026-09-20", "updateTime": "10:00:00"}
    return [entry, entry]


def test_derive_1x2_sums_to_one_and_includes_other_score_buckets():
    signals = {
        "hafu": jc_predict._signals(_hafu_series()),
        "crs": jc_predict._signals(_crs_series()),
    }
    derived = jc_predict._derive_1x2(signals)
    assert derived is not None
    assert abs(sum(derived.values()) - 1.0) < 1e-6


def test_crs_other_score_buckets_are_counted():
    """漏掉 s-1sh/s-1sd/s-1sa 会让推导概率和小于 1。"""
    only_crs = {"crs": jc_predict._signals(_crs_series())}
    derived = jc_predict._derive_1x2(only_crs)
    assert derived is not None
    assert abs(sum(derived.values()) - 1.0) < 1e-6


def test_cross_produces_row_only_for_had():
    signals = {
        "had": jc_predict._signals(_degen_series()),
        "hhad": jc_predict._signals(_degen_series()),
        "hafu": jc_predict._signals(_hafu_series()),
        "crs": jc_predict._signals(_crs_series()),
    }
    rows = jc_predict.analyze_match(signals)
    cross_pools = {r["pool"] for r in rows if r["method"] == "cross"}
    assert cross_pools == {"had"}, "cross 只应作用于 had"
    # hhad 仍有其余 4 条路径
    hhad_methods = {r["method"] for r in rows if r["pool"] == "hhad"}
    assert hhad_methods == {"market", "trend", "stable", "blend"}


# ---- 4. 归一化 -------------------------------------------------------------------
def test_normalize_combination_maps_all_pools():
    assert jc_predict.normalize_combination("had", "H") == "h"
    assert jc_predict.normalize_combination("hhad", "A") == "a"
    assert jc_predict.normalize_combination("ttg", "2") == "s2"
    assert jc_predict.normalize_combination("ttg", "7") == "s7"
    assert jc_predict.normalize_combination("ttg", "9") == "s7", "7 球及以上归 s7"
    assert jc_predict.normalize_combination("hafu", "H:H") == "hh"
    assert jc_predict.normalize_combination("hafu", "H:D") == "hd"
    assert jc_predict.normalize_combination("crs", "2:0") == "s02s00"
    assert jc_predict.normalize_combination("crs", "0:0") == "s00s00"
    assert jc_predict.normalize_combination("crs", "-1H") == "s-1sh"


def test_option_label_translates_all_pools():
    assert jc_predict.option_label("had", "h") == "主胜"
    assert jc_predict.option_label("had", "d") == "平"
    assert jc_predict.option_label("had", "a") == "客胜"
    assert jc_predict.option_label("hhad", "h") == "主胜"
    # 比分：s01s00 → 1:0
    assert jc_predict.option_label("crs", "s01s00") == "1:0"
    assert jc_predict.option_label("crs", "s03s02") == "3:2"
    assert jc_predict.option_label("crs", "s-1sh") == "主胜(其他比分)"
    assert jc_predict.option_label("crs", "s-1sa") == "客胜(其他比分)"
    # 半全场：半场 + 全场
    assert jc_predict.option_label("hafu", "hh") == "胜胜"
    assert jc_predict.option_label("hafu", "ad") == "负平"
    # 总进球
    assert jc_predict.option_label("ttg", "s0") == "0 球"
    assert jc_predict.option_label("ttg", "s7") == "7 球及以上"
    # 未知编码原样返回，不猜
    assert jc_predict.option_label("had", "zz") == "zz"
    assert jc_predict.option_label("had", None) == "—"


def test_pool_label():
    assert jc_predict.pool_label("hhad") == "让球胜平负"
    assert jc_predict.pool_label("crs") == "比分"
    assert jc_predict.pool_label("unknown") == "unknown"


def test_normalize_combination_returns_none_for_unknown():
    assert jc_predict.normalize_combination("had", "X") is None
    assert jc_predict.normalize_combination("ttg", "abc") is None
    assert jc_predict.normalize_combination("hafu", "H:X") is None
    assert jc_predict.normalize_combination("crs", "abc") is None
    assert jc_predict.normalize_combination("unknown", "H") is None
    assert jc_predict.normalize_combination("had", None) is None


# ---- 5. 入库幂等 -----------------------------------------------------------------
def test_predict_match_is_idempotent_and_overwrites():
    conn = _db()
    series = _degen_series()

    n1 = jc_predict.predict_match(conn, 1001, {"had": series}, "2026-09-20")
    assert n1 == 4                                   # 无 hafu/crs → 无 cross，had 剩 4 条
    before = conn.execute("SELECT COUNT(*) c FROM jc_predictions").fetchone()["c"]

    jc_predict.predict_match(conn, 1001, {"had": series}, "2026-09-20")
    assert conn.execute("SELECT COUNT(*) c FROM jc_predictions").fetchone()["c"] == before


def test_predict_match_stores_option_vectors():
    conn = _db()
    jc_predict.predict_match(conn, 1001, {"had": _degen_series()}, "2026-09-20")
    row = conn.execute(
        "SELECT prob_json, latest_odds_json, vol_json, change_count FROM jc_predictions LIMIT 1"
    ).fetchone()
    assert set(json.loads(row["prob_json"])) == {"h", "d", "a"}
    assert set(json.loads(row["vol_json"])) == {"h", "d", "a"}
    assert row["change_count"] == 2


# ---- 6. 对奖 ---------------------------------------------------------------------
def _seed_pending(conn, match_id=1001):
    """had + hafu + crs —— 后两者是 cross 推导的来源，缺了 had 就只有 4 条路径。

    行数：had 5（含 cross）+ hafu 4 + crs 4 = 13。
    """
    conn.execute(
        """INSERT INTO jc_matches(match_id, match_date, home_team, away_team)
           VALUES(?, '2026-09-20', 'A', 'B')""", (match_id,))
    jc_predict.predict_match(conn, match_id, {
        "had": _degen_series(),
        "hafu": _hafu_series(),
        "crs": _crs_series(),
    }, "2026-09-20")
    conn.commit()


def test_score_pending_scores_hits_and_misses():
    conn = _db()
    _seed_pending(conn)
    # 开奖 H → pick 为 h 的命中，其余不中
    out = jc_predict.score_pending(conn, pending={1001: {"result_had": "H"}})
    # 只给了 had 的赛果 → had 的 4 个非空行打分 + 1 个 stable 空行收尾；
    # hafu/crs 因无赛果而 skip
    assert out["scored"] == 5
    assert out["skipped"] == 8
    # 只有 had 被对奖（其余玩法没给赛果）；stable 的空行 hit 应为 NULL
    rows = list(conn.execute(
        "SELECT method, pick, hit FROM jc_predictions WHERE pool='had' ORDER BY method"))
    assert rows, "had 应有预测行"
    for row in rows:
        if row["pick"] is None:
            assert row["hit"] is None
            assert row["method"] == "stable"
        else:
            assert row["hit"] == (1 if row["pick"] == "h" else 0)


def test_score_pending_skips_unfinished_matches():
    conn = _db()
    _seed_pending(conn)
    out = jc_predict.score_pending(conn, pending={1001: {}})   # 无赛果
    assert out["scored"] == 1                       # 只有 had 的 stable 空行被收尾
    assert out["skipped"] == 12
    # 未对奖的行仍可被下次捞出
    pending = conn.execute(
        "SELECT COUNT(*) c FROM jc_predictions WHERE scored_at IS NULL AND pick IS NOT NULL"
    ).fetchone()["c"]
    assert pending > 0


def test_stable_null_rows_are_finalized_not_repeated():
    """pick=NULL 的行也必须写 scored_at，否则每次 score-jc 都会重新捞出来。"""
    conn = _db()
    _seed_pending(conn)
    jc_predict.score_pending(conn, pending={1001: {"result_had": "H"}})

    again = jc_predict.score_pending(conn, pending={1001: {"result_had": "H"}})
    assert again["scored"] == 0, "第二次不应再处理任何行"


def test_score_pending_reads_from_jc_matches_when_no_pending():
    conn = _db()
    _seed_pending(conn)
    conn.execute("UPDATE jc_matches SET result_had='H' WHERE match_id=1001")
    conn.commit()

    out = jc_predict.score_pending(conn)

    assert out["scored"] == 5                       # 只有 result_had 有值
    assert conn.execute(
        "SELECT COUNT(*) c FROM jc_predictions WHERE hit = 1"
    ).fetchone()["c"] > 0


def test_score_pending_with_all_pools_settled():
    """三个玩法都有赛果时，所有非空推荐行都应被打分。"""
    conn = _db()
    _seed_pending(conn)
    pending = {1001: {"result_had": "H", "result_hafu": "H:H", "result_crs": "2:0"}}

    out = jc_predict.score_pending(conn, pending=pending)

    # had 4 + hafu 4 + crs 4 非空行 + 1 个 stable 空行 = 13
    assert out["scored"] == 13
    assert out["skipped"] == 0


def test_summary_by_method_aggregates_hit_rate():
    conn = _db()
    _seed_pending(conn)
    jc_predict.score_pending(conn, pending={1001: {"result_had": "H"}})

    summary = {row["method"]: row for row in jc_predict.summary_by_method(conn)}

    assert set(summary) == {"blend", "cross", "market", "trend"}
    assert summary["market"]["picks"] == 1
    assert summary["market"]["hit_rate"] == 1.0      # market 选 h，开奖 H
    assert summary["trend"]["hit_rate"] == 0.0       # trend 选 a，未中
