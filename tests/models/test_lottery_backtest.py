import random

import pytest

from football_lottery.models import lottery_backtest as lb

# 2020 年的大乐透奖级表（9 级）：三等奖 5+0 @10000、八等奖 3+1/2+2 @15 …
DLT_PRIZES_2020 = [
    {"tier": "一等奖", "cond": "5+2", "winners": 1, "amount": 8000000},
    {"tier": "二等奖", "cond": "5+1", "winners": 10, "amount": 150000},
    {"tier": "三等奖", "cond": "5+0", "winners": 100, "amount": 10000},
    {"tier": "四等奖", "cond": "4+2", "winners": 500, "amount": 3000},
    {"tier": "五等奖", "cond": "4+1", "winners": 5000, "amount": 300},
    {"tier": "六等奖", "cond": "3+2", "winners": 9000, "amount": 200},
    {"tier": "七等奖", "cond": "4+0", "winners": 20000, "amount": 100},
    {"tier": "八等奖", "cond": "3+1；2+2", "winners": 500000, "amount": 15},
    {"tier": "九等奖", "cond": "3+0；2+1；1+2；0+2", "winners": 9000000, "amount": 5},
]
# 2026 年的奖级表（7 级）：三等奖并入了 4+2、金额也变了
DLT_PRIZES_2026 = [
    {"tier": "一等奖", "cond": "5+2", "winners": 3, "amount": 10000000},
    {"tier": "三等奖", "cond": "5+0；4+2", "winners": 995, "amount": 6666},
    {"tier": "四等奖", "cond": "4+1", "winners": 16929, "amount": 380},
    {"tier": "五等奖", "cond": "4+0；3+2", "winners": 66439, "amount": 200},
    {"tier": "六等奖", "cond": "3+1；2+2", "winners": 765764, "amount": 18},
    {"tier": "七等奖", "cond": "3+0；2+1；1+2；0+2", "winners": 8096524, "amount": 7},
]
DLT_DRAWN = {"front": ["01", "02", "03", "04", "05"], "back": ["01", "02"]}


def _dlt(front, back, drawn=DLT_DRAWN, prizes=DLT_PRIZES_2020):
    return lb.prize_for("dlt", {"front": front, "back": back}, drawn, prizes)


def _draws(n, lottery="p3", numbers=None, start=1):
    return [{"lottery": lottery, "issue": f"2020{start + i:03d}",
             "draw_date": "2020-01-01", "prizes": None,
             "numbers": numbers or {"digits": ["1", "2", "3"]}}
            for i in range(n)]


def test_dlt_tier_map_skips_appended_rows():
    prizes = DLT_PRIZES_2020 + [{"tier": "一等奖(追加)", "cond": "5+2",
                                 "winners": 0, "amount": 0}]
    mapping = lb.dlt_tier_map(prizes)
    assert mapping[(5, 2)]["tier"] == "一等奖"
    assert mapping[(5, 2)]["amount"] == 8000000


def test_dlt_prizes_follow_the_page_not_a_hardcoded_table():
    """同一次命中在不同年份的奖级表下必须给出不同的奖金 ——
    大乐透改过奖级设置（9 级 → 7 级），硬编码的表必然算错一段历史。"""
    five_zero = ["01", "02", "03", "04", "05"], ["03", "04"]     # 5+0
    four_two = ["01", "02", "03", "04", "06"], ["01", "02"]      # 4+2
    assert _dlt(*five_zero, prizes=DLT_PRIZES_2020) == 10000     # 三等奖
    assert _dlt(*five_zero, prizes=DLT_PRIZES_2026) == 6666      # 三等奖（并入 4+2）
    assert _dlt(*four_two, prizes=DLT_PRIZES_2020) == 3000       # 四等奖
    assert _dlt(*four_two, prizes=DLT_PRIZES_2026) == 6666       # 现在是三等奖


def test_dlt_tier_boundaries():
    assert _dlt(["01", "02", "03", "04", "06"], ["01", "02"]) == 3000      # 4+2
    assert _dlt(["01", "02", "03", "06", "07"], ["01", "02"]) == 200       # 3+2 六等奖
    assert _dlt(["01", "02", "06", "07", "08"], ["01", "02"]) == 15        # 2+2 八等奖
    assert _dlt(["01", "02", "03", "06", "07"], ["01", "03"]) == 15        # 3+1 八等奖
    assert _dlt(["01", "02", "06", "07", "08"], ["01", "03"]) == 5         # 2+1 九等奖
    assert _dlt(["01", "02", "06", "07", "08"], ["03", "04"]) == 0         # 2+0 未中奖


def test_dlt_missing_prize_table_pays_zero_not_a_guess():
    """页面没给奖级表时计 0，绝不估算 —— 估错的奖金会污染整个 ROI。"""
    assert _dlt(["01", "02", "03", "04", "05"], ["01", "02"], prizes=None) == 0
    assert _dlt(["01", "02", "03", "04", "06"], ["01", "02"], prizes=[]) == 0


def test_ssq_tier_boundaries():
    drawn = {"front": ["01", "02", "03", "04", "05", "06"], "back": ["07"]}

    def amount(front, back, prizes=None):
        return lb.prize_for("ssq", {"front": front, "back": back}, drawn, prizes)

    assert amount(["01", "02", "03", "04", "05", "06"], ["07"]) == 0        # 6+1 浮动，无表
    assert amount(["01", "02", "03", "04", "05", "08"], ["07"]) == 3000     # 5+1
    assert amount(["01", "02", "03", "04", "05", "08"], ["08"]) == 200      # 5+0
    assert amount(["01", "02", "03", "09", "10", "11"], ["07"]) == 10       # 3+1
    assert amount(["01", "02", "03", "09", "10", "11"], ["08"]) == 0        # 3+0
    assert amount(["08", "09", "10", "11", "12", "13"], ["07"]) == 5        # 0+1


def test_ssq_uses_scraped_floating_amount():
    drawn = {"front": ["01", "02", "03", "04", "05", "06"], "back": ["07"]}
    prizes = [{"tier": "一等奖", "cond": "", "winners": 9, "amount": 6279386}]
    assert lb.prize_for("ssq", drawn, drawn, prizes) == 6279386


def test_p3_and_3d_only_pay_direct():
    """一注 2 元只买一种玩法：直选。顺序不同就是没中 —— 不能同时算上组选，
    否则单注期望奖金会超过 2 元（1040/1000 + 6×173/1000 = 2.078 元），
    返还率会算出 >100%，那是自证错误。"""
    drawn = {"digits": ["0", "6", "4"]}
    assert lb.prize_for("p3", drawn, drawn, None) == 1040
    assert lb.prize_for("p3", {"digits": ["4", "6", "0"]}, drawn, None) == 0
    assert lb.prize_for("3d", {"digits": ["4", "0", "6"]}, drawn, None) == 0


def test_direct_prize_prefers_page_amount():
    drawn = {"digits": ["0", "6", "4"]}
    prizes = [{"tier": "直选", "cond": "", "winners": 6942, "amount": 1040}]
    assert lb.prize_for("p3", drawn, drawn, prizes) == 1040


def test_p5_prize():
    drawn = {"digits": ["1", "2", "3", "4", "5"]}
    assert lb.prize_for("p5", drawn, drawn, None) == 100000
    assert lb.prize_for("p5", {"digits": ["1", "2", "3", "4", "6"]}, drawn, None) == 0


def test_hits_for_shapes():
    assert lb.hits_for("dlt", {"front": ["01", "02"], "back": ["03"]},
                       {"front": ["01", "05"], "back": ["03"]}) == {"front": 1, "back": 1}
    assert lb.hits_for("p3", {"digits": ["1", "2", "3"]},
                       {"digits": ["1", "9", "3"]}) == {"digits": 2}


# ---- 显著性 ---------------------------------------------------------------

def test_paired_significance_flags_a_real_difference():
    """差值必须带噪声：常数差值 sd=0 会被跳过，而真实彩票收益永远不是常数。"""
    rng = random.Random(1)
    per_draw = {
        "random": {f"2020{i:03d}": float(rng.randint(0, 10)) for i in range(1, 61)},
        "hot": {f"2020{i:03d}": float(rng.randint(0, 10)) + 100.0 for i in range(1, 61)},
    }
    rows = lb.paired_significance(per_draw, baseline="random")
    assert rows[0]["strategy"] == "hot"
    assert rows[0]["beats_random"] is True
    assert rows[0]["mean_diff"] > 0


def test_paired_significance_does_not_flag_identical_series():
    series = {f"2020{i:03d}": float(i % 3) for i in range(1, 61)}
    assert lb.paired_significance({"random": series, "hot": dict(series)},
                                  baseline="random") == []


def test_paired_significance_does_not_flag_pure_noise():
    rng = random.Random(0)
    base = {f"2020{i:03d}": float(rng.randint(0, 100)) for i in range(1, 201)}
    other = {k: float(rng.randint(0, 100)) for k in base}
    rows = lb.paired_significance({"random": base, "hot": other}, baseline="random")
    assert all(not r["beats_random"] for r in rows)


def test_paired_significance_requires_output_symmetry():
    """双侧检验：显著为负也要能报出来（只是不算 beats_random）。"""
    rng = random.Random(2)
    per_draw = {
        "random": {f"2020{i:03d}": float(rng.randint(50, 60)) for i in range(1, 61)},
        "hot": {f"2020{i:03d}": float(rng.randint(0, 10)) for i in range(1, 61)},
    }
    rows = lb.paired_significance(per_draw, baseline="random")
    assert rows[0]["p"] < 0.05
    assert rows[0]["beats_random"] is False


# ---- 走查 ------------------------------------------------------------------

def test_walk_forward_never_uses_future(monkeypatch):
    """核心防泄漏断言：第 t 期拿到的 history 必须**内容上逐条等于** draws[:t]。

    只断言长度不够 —— 内容被串改时长度照样对。
    """
    seen = []

    def fake_predict_bets(lottery, history, strategy, n_bets, seed, window=100):
        seen.append([d["issue"] for d in history])
        return [{"digits": ["1", "1", "1"]}]

    monkeypatch.setattr(lb.lottery_predict, "predict_bets", fake_predict_bets)
    draws = [{"lottery": "p3", "issue": f"2020{i:03d}", "draw_date": "2020-01-01",
              "prizes": None, "numbers": {"digits": [str(i % 10)] * 3}}
             for i in range(1, 41)]
    lb.run_backtest(draws, "p3", strategies=["hot"], window=10, n_bets=1, min_history=5)
    expected = [[f"2020{j:03d}" for j in range(1, i)] for i in range(6, 41)]
    # 每期调用两次：被强制加入的 random 基线 + hot，两条路径必须拿到同一份 history
    assert seen[0::2] == expected
    assert seen[1::2] == expected


def test_run_backtest_always_includes_random_baseline():
    """即使调用方只点了一条策略，也必须带上 random —— 否则没有对照，
    报告会把「无基线」说成「无差异」。"""
    result = lb.run_backtest(_draws(60), "p3", strategies=["hot"], n_bets=1,
                             min_history=30)
    assert "random" in result["per_draw"]
    assert result["paired"]


def test_run_backtest_invested_uses_actual_bets(monkeypatch):
    """投入按**实际发出的注数**算。这里让策略每期只发 1 注，请求 5 注 ——
    若按请求注数记投入，投入会虚增 5 倍。"""
    monkeypatch.setattr(lb.lottery_predict, "predict_bets",
                        lambda *a, **k: [{"digits": ["1", "1", "1"]}])
    result = lb.run_backtest(_draws(40), "p3", strategies=["hot"], n_bets=5,
                             min_history=30)
    metrics = result["metrics"]["hot"]
    assert metrics["bets"] == metrics["draws"] == 10
    assert metrics["invested"] == 10 * 1 * lb.BET_PRICE


def test_run_backtest_counts_prizes(monkeypatch):
    """命中直选时返还和盈亏要对得上。"""
    monkeypatch.setattr(lb.lottery_predict, "predict_bets",
                        lambda *a, **k: [{"digits": ["1", "2", "3"]}])
    result = lb.run_backtest(_draws(40), "p3", strategies=["random"], n_bets=1,
                             min_history=30)
    metrics = result["metrics"]["random"]
    assert metrics["returned"] == metrics["draws"] * 1040
    assert metrics["win_rate"] == 1.0


def test_summarize_mentions_multiple_comparison():
    text = lb.summarize(lb.run_backtest(_draws(40), "p3", strategies=["hot"],
                                        n_bets=1, min_history=30))
    assert "假显著" in text


def test_load_draws_is_chronological():
    import sqlite3

    from football_lottery.db import store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    for issue in ("2020010", "2020002", "2020005"):
        conn.execute(
            "INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes) "
            "VALUES('p3',?,?,?,?)",
            (issue, "2020-01-01", '{"digits":["1","2","3"]}', "[]"))
    conn.commit()
    issues = [d["issue"] for d in lb.load_draws(conn, "p3")]
    assert issues == ["2020002", "2020005", "2020010"]
