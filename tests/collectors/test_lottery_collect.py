import json
import sqlite3
from pathlib import Path

import pytest

from football_lottery.collectors import lottery_history as lh
from football_lottery.db import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lottery"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# ---- 期号与数值 ------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("26107", "2026107"),       # 体彩 5 位 → 补成 7 位
    ("07025", "2007025"),       # 大乐透 2007 年的期号也是 5 位
    ("2026109", "2026109"),     # 福彩本来就是 7 位
])
def test_norm_issue(raw, expected):
    assert lh.norm_issue(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("831,053,727.13", 831053727),
    ("313,508,185", 313508185),
    ("100000", 100000),
    ("0", 0),
    ("---", None),              # 追加奖没开出
    ("", None),
    (None, None),
    ("-1", None),               # 体彩用 -1 表示「不适用」—— 绝不能变成正 1
])
def test_num_never_silently_turns_sentinels_into_numbers(raw, expected):
    assert lh._num(raw) == expected


# ---- 体彩解析 --------------------------------------------------------------

def test_parse_sporttery_dlt_splits_front_and_back():
    row = lh.parse_sporttery(_fixture("sporttery_dlt_2026"), "dlt")
    assert row["issue"] == "2026107"
    assert row["draw_date"] == "2026-09-19"
    assert row["numbers"] == {"front": ["02", "05", "07", "14", "22"],
                              "back": ["04", "10"]}
    assert row["sales"] == 313508185
    assert row["jackpot"] == 831053727
    assert row["draw_order"] is not None


def test_parse_sporttery_digit_lotteries_keep_single_chars():
    """排列类的号码在接口里是空格分隔的单字符，不能拼成 '202'。"""
    p3 = lh.parse_sporttery(_fixture("sporttery_p3"), "p3")
    assert p3["numbers"]["digits"] == ["2", "0", "2"]
    p5 = lh.parse_sporttery(_fixture("sporttery_p5"), "p5")
    assert p5["numbers"]["digits"] == ["2", "0", "2", "2", "8"]


def test_dlt_prize_tables_cover_both_rule_eras():
    """2020 年的表是 9 个奖级、2026 年是 7 个 —— 夹具要真的覆盖到两套，
    否则奖级判定的分支测试等于没测。"""
    old = {p["tier"] for p in
           lh.parse_sporttery(_fixture("sporttery_dlt_2020"), "dlt")["prizes"]}
    new = {p["tier"] for p in
           lh.parse_sporttery(_fixture("sporttery_dlt_2026"), "dlt")["prizes"]}
    assert "九等奖" in old and "九等奖" not in new
    assert "七等奖" in new


def test_parse_sporttery_rejects_wrong_number_count():
    bad = dict(_fixture("sporttery_dlt_2026"), lotteryDrawResult="02 05 07 14 22 04")
    with pytest.raises(ValueError):
        lh.parse_sporttery(bad, "dlt")


# ---- 福彩解析 --------------------------------------------------------------

def test_parse_cwl_ssq():
    row = lh.parse_cwl(_fixture("cwl_ssq"), "ssq")
    assert row["issue"] == "2020001"
    assert len(row["numbers"]["front"]) == 6
    assert len(row["numbers"]["back"]) == 1
    assert row["draw_date"] == "2020-01-02"
    assert row["sales"] is not None


def test_parse_cwl_ssq_prize_types_map_to_names():
    row = lh.parse_cwl(_fixture("cwl_ssq"), "ssq")
    tiers = {p["tier"] for p in row["prizes"]}
    assert {"一等奖", "二等奖", "三等奖"} <= tiers
    first = next(p for p in row["prizes"] if p["tier"] == "一等奖")
    assert first["amount"] > 0 and first["winners"] > 0


def test_parse_cwl_3d_does_not_take_prize_amounts():
    """3D 的 prizegrades.typemoney 是**总奖金**（2020001 期直选 17,598,680），
    口径和双色球的单注奖金不同。3D 奖金是法定固定值，回测直接用固定值，
    所以这里必须刻意不取 —— 取了就会把 1759 万当成单注奖金。"""
    row = lh.parse_cwl(_fixture("cwl_3d"), "3d")
    assert len(row["numbers"]["digits"]) == 3
    assert row["prizes"] == []


def test_parse_cwl_rejects_wrong_number_count():
    bad = dict(_fixture("cwl_ssq"), red="09,12,15")
    with pytest.raises(ValueError):
        lh.parse_cwl(bad, "ssq")


def test_parse_item_dispatches_by_source():
    assert lh.parse_item(_fixture("sporttery_p3"), "p3")["lottery"] == "p3"
    assert lh.parse_item(_fixture("cwl_ssq"), "ssq")["lottery"] == "ssq"


# ---- 限流 ------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_rate_limiter_spaces_requests():
    import time as _time

    limiter = lh._RateLimiter(50.0)          # 50/s → 20ms 一个
    start = _time.monotonic()
    for _ in range(5):
        limiter.wait()
    assert _time.monotonic() - start >= 0.06


def test_get_json_403_backs_off_then_raises(monkeypatch):
    """被拦要退避重试，重试仍失败就抛 RateLimited —— 不换 UA、不换代理。"""
    monkeypatch.setattr(lh, "BLOCK_BACKOFF", (0, 0, 0))
    monkeypatch.setattr(lh, "_limiter", lh._RateLimiter(0))
    calls = []

    def _get(self, url, params=None, headers=None):
        calls.append(url)
        return _FakeResp(403)

    monkeypatch.setattr(lh, "get_client", lambda: type("C", (), {"get": _get})())
    with pytest.raises(lh.RateLimited):
        lh._get_json("https://example/x", {}, {})
    assert len(calls) == 4          # 首次 + 3 次退避重试


def test_get_json_returns_payload(monkeypatch):
    monkeypatch.setattr(lh, "_limiter", lh._RateLimiter(0))
    monkeypatch.setattr(lh, "get_client", lambda: type(
        "C", (), {"get": lambda self, url, params=None, headers=None:
                  _FakeResp(200, {"ok": 1})})())
    assert lh._get_json("https://example/x", {}, {}) == {"ok": 1}


def test_sync_history_stops_when_blocked(monkeypatch):
    """撞上限流必须停下整轮，而不是把剩下几十页挨个撞一遍。"""
    conn = _conn()
    calls = []

    def fake_fetch_page(lottery, page_no, page_size=100):
        calls.append(page_no)
        if page_no == 1:
            return [_fixture("sporttery_p3")]
        raise lh.RateLimited("blocked")

    monkeypatch.setattr(lh, "fetch_page", fake_fetch_page)
    stats = lh.sync_history(conn, "p3")
    assert stats["blocked"] is True
    assert stats["saved"] == 1
    assert calls == [1, 2]          # 第二页被拦后立刻收手，没有继续翻页


def test_sync_history_stops_at_since_year(monkeypatch):
    """只要 2026 年起的，碰到更早的期号就停止翻页。"""
    conn = _conn()
    pages = {1: [_fixture("sporttery_dlt_2026")], 2: [_fixture("sporttery_dlt_2020")]}

    monkeypatch.setattr(lh, "fetch_page",
                        lambda lottery, page_no, page_size=100: pages.get(page_no, []))
    stats = lh.sync_history(conn, "dlt", since_year=2026)
    assert stats["saved"] == 1
    assert stats["pages"] == 2      # 翻到第 2 页发现太旧就停
    assert conn.execute("SELECT COUNT(*) FROM lottery_draw").fetchone()[0] == 1


# ---- 入库 ------------------------------------------------------------------

def test_upsert_draw_is_idempotent_and_corrects():
    """开奖结果是事实：重抓同一期必须覆盖为最新解析结果。"""
    conn = _conn()
    row = lh.parse_sporttery(_fixture("sporttery_p3"), "p3")
    lh.upsert_draw(conn, row)
    lh.upsert_draw(conn, dict(row, numbers={"digits": ["1", "1", "1"]}))
    rows = conn.execute("SELECT numbers FROM lottery_draw").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["numbers"]) == {"digits": ["1", "1", "1"]}


def _page(issue: str) -> list[dict]:
    """造一页只含指定期号的假数据（接口是倒序的，调用方按新→旧排）。"""
    item = dict(_fixture("sporttery_p3"))
    item["lotteryDrawNum"] = issue
    return [item]


def _paged(monkeypatch, pages: dict[int, list[dict]], fetched: list[int] | None = None):
    def fake(lottery, page_no, page_size=100):
        if fetched is not None:
            fetched.append(page_no)
        return pages.get(page_no, [])
    monkeypatch.setattr(lh, "fetch_page", fake)


def test_sync_history_stops_early_once_pages_are_known(monkeypatch):
    """增量：整页都已入库就停，不再往回翻。

    首次要翻二十多页才能把 2020 年至今拉齐；之后再点一次应该只发两三个请求，
    否则每点一下就把几十页重翻一遍 —— 那正是「同步开奖」按钮本来的毛病。
    """
    conn = _conn()
    pages = {1: _page("2026003"), 2: _page("2026002"), 3: _page("2026001"), 4: []}

    _paged(monkeypatch, pages)
    first = lh.sync_history(conn, "p3")
    assert first["saved"] == 3
    assert first["pages"] == 3
    assert first["stopped_early"] is False

    fetched: list[int] = []
    _paged(monkeypatch, pages, fetched)
    second = lh.sync_history(conn, "p3")
    assert second["saved"] == 0
    assert second["stopped_early"] is True
    assert fetched == [1, 2], "第 1 页刷新 + 第 2 页确认整页已知，就该停"


def test_sync_history_full_ignores_early_stop(monkeypatch):
    """--full 一直翻到年份边界，用来补历史缺口。

    增量早停的前提是「库里已有的就是对的」，库里有洞时它看不见 ——
    所以必须留一条强制扫全量的路。
    """
    conn = _conn()
    pages = {1: _page("2026003"), 2: _page("2026002"), 3: _page("2026001"), 4: []}
    _paged(monkeypatch, pages)
    lh.sync_history(conn, "p3")

    fetched: list[int] = []
    _paged(monkeypatch, pages, fetched)
    stats = lh.sync_history(conn, "p3", full=True)
    assert stats["stopped_early"] is False
    assert fetched == [1, 2, 3, 4], "全量模式要一直翻到没有更多数据为止"


def test_sync_history_always_refreshes_first_page(monkeypatch):
    """最近的期次可能被数据源修正，所以第一页永远重拉，不参与早停判断。"""
    conn = _conn()
    pages = {1: _page("2026003"), 2: _page("2026002"), 3: []}
    _paged(monkeypatch, pages)
    lh.sync_history(conn, "p3")

    fetched: list[int] = []
    _paged(monkeypatch, pages, fetched)
    lh.sync_history(conn, "p3")
    assert fetched[0] == 1


def test_sync_history_second_run_updates_not_duplicates(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        lh, "fetch_page",
        lambda lottery, page_no, page_size=100: [_fixture("cwl_ssq")] if page_no == 1 else [])
    first = lh.sync_history(conn, "ssq")
    second = lh.sync_history(conn, "ssq")
    assert first["saved"] == 1
    assert second["saved"] == 0 and second["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM lottery_draw").fetchone()[0] == 1


def test_latest_issue():
    conn = _conn()
    assert lh.latest_issue(conn, "p3") is None
    row = lh.parse_sporttery(_fixture("sporttery_p3"), "p3")
    lh.upsert_draw(conn, row)
    assert lh.latest_issue(conn, "p3") == row["issue"]
