import json
import re
import sqlite3
from pathlib import Path

import pytest

from football_lottery.collectors import lottery_history as lh
from football_lottery.collectors.lottery_history import parse_draw
from football_lottery.db import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lottery"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def _html(lottery: str) -> str:
    return (FIXTURES / f"{lottery}.html").read_text(encoding="utf-8")


def test_dlt_numbers_and_meta():
    row = parse_draw(_html("dlt"), "dlt", "2026107")
    assert row["numbers"] == {"front": ["02", "05", "07", "14", "22"],
                              "back": ["04", "10"]}
    assert row["draw_date"] == "2026-09-19"
    assert row["sales"] == 313508185
    assert row["jackpot"] == 831053727
    assert row["draw_order"] is not None


def test_dlt_prizes_have_condition_column():
    """大乐透奖级行有 4 列，第 2 列是「中奖条件」——奖级判定要靠它，
    因为大乐透的奖级设置改过（2023 年前 9 个奖级，现在 7 个）。"""
    row = parse_draw(_html("dlt"), "dlt", "2026107")
    tiers = {p["tier"]: p for p in row["prizes"]}
    assert tiers["一等奖"]["cond"] == "5+2"
    assert tiers["一等奖"]["winners"] == 3
    assert tiers["一等奖"]["amount"] == 10000000
    assert tiers["三等奖"]["cond"] == "5+0；4+2"
    assert tiers["三等奖"]["amount"] == 6666


@pytest.mark.parametrize("lottery", ["ssq", "p3", "3d"])
def test_three_column_prize_tables_parse(lottery):
    """其余彩种的奖级行只有 3 列（没有中奖条件列），不能因为少一列就解析成空表。"""
    row = parse_draw(_html(lottery), lottery, f"2020{100:03d}")
    assert row["prizes"], f"{lottery} 奖级表解析为空"
    assert all(p["winners"] is not None or p["amount"] is not None
               for p in row["prizes"])


def test_ssq_has_six_red_one_blue():
    row = parse_draw(_html("ssq"), "ssq", "2020100")
    assert len(row["numbers"]["front"]) == 6
    assert len(row["numbers"]["back"]) == 1
    assert row["draw_date"] == "2020-10-13"


@pytest.mark.parametrize("lottery,expected", [
    ("p3", ["0", "6", "4"]),                # 首位是 0 —— 补零写错就是它先炸
    ("p5", ["0", "6", "4", "3", "8"]),
    ("3d", ["6", "6", "8"]),                # 3D 与排列三同期号号码不同，别混用
])
def test_digit_lotteries_keep_single_digit_cells(lottery, expected):
    """页面用单位数 <li>0</li> 表示号码，必须原样保留单个数字，不能拼错位。"""
    row = parse_draw(_html(lottery), lottery, "2020100")
    assert row["numbers"]["digits"] == expected
    assert row["jackpot"] is None       # 排列类没有滚存奖金


def test_missing_kjcode_raises():
    with pytest.raises(ValueError):
        parse_draw("<html>没有号码</html>", "dlt", "2026107")


def test_number_count_mismatch_raises():
    """号码个数与彩种规格不符时必须报错，绝不静默返回残缺数据。"""
    html = re.sub(r'<li class="rb_kj">\s*\d+\s*</li>', "", _html("dlt"), count=1)
    with pytest.raises(ValueError):
        parse_draw(html, "dlt", "2026107")


def test_issue_candidates_cover_a_year():
    items = lh.issue_candidates(2026, "p3")
    assert items[0] == "2026001"
    assert items[-1] == "2026370"
    assert all(len(i) == 7 for i in items)


def test_candidates_between_covers_year_rollover():
    found = lh._candidates_between("dlt", "2025158", "2026003")
    assert found[0] == "2025159"
    assert found[-1] == "2026003"
    assert "2026001" in found


def test_upsert_draw_is_idempotent_and_corrects():
    """开奖结果是事实：重抓同一期必须覆盖为最新解析结果。"""
    conn = _conn()
    row = parse_draw(_html("p3"), "p3", "2020100")
    lh.upsert_draw(conn, row)
    row2 = dict(row)
    row2["numbers"] = {"digits": ["1", "1", "1"]}
    lh.upsert_draw(conn, row2)
    rows = conn.execute("SELECT numbers FROM lottery_draw WHERE lottery='p3'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["numbers"]) == {"digits": ["1", "1", "1"]}


def test_sync_range_skips_existing(monkeypatch):
    """已入库的期号不应重复请求 —— 断点续传的基础。"""
    conn = _conn()
    lh.upsert_draw(conn, parse_draw(_html("p3"), "p3", "2020001"))
    called = []

    def fake_fetch(lottery, issue):
        called.append(issue)
        return None

    monkeypatch.setattr(lh, "fetch_draw", fake_fetch)
    lh.sync_range(conn, "p3", 2020, 2020, workers=2)
    assert "2020001" not in called
    assert "2020002" in called


def test_sync_range_counts_missing_and_failed(monkeypatch):
    conn = _conn()

    def fake_fetch(lottery, issue):
        if issue.endswith("001"):
            raise RuntimeError("网络炸了")
        return None

    monkeypatch.setattr(lh, "fetch_draw", fake_fetch)
    stats = lh.sync_range(conn, "3d", 2020, 2020, workers=2)
    assert stats["failed"] == 1
    assert stats["failed_issues"] == ["2020001"]
    assert stats["missing"] == 369
