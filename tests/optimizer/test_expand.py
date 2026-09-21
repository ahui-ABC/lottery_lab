"""方案展开为单式清单 + 对奖计算测试。"""
from lottery_lab.optimizer import expand
from lottery_lab import winnings


def test_expand_3_legs_bruteforce_count():
    legs = [["3", "1"], ["3"], ["1", "0"]]
    rows = expand.expand(legs)
    assert len(rows) == 4
    expected = {"331", "330", "131", "130"}
    assert set(rows) == expected


def test_expand_14_legs_default_64_budget():
    # 14 场都选 3 个 → 3^14 注, 远高于 32 注预算；expand 仅在调用方已通过预算后使用
    legs = [["3", "1", "0"]] * 14
    rows = expand.expand(legs)
    assert len(rows) == 3 ** 14


def test_to_text_includes_meta():
    rows = ["331", "330"]
    txt = expand.to_text(rows, meta={
        "period_no": "26001", "game_type": "sfc14",
        "objective": "first_second", "budget": 64,
        "notes_count": 2, "amount": 4,
    })
    assert "26001" in txt and "2 注" in txt


def test_first_second_counts_all_covered():
    legs = [["3", "1"], ["3"], ["3", "0"]]
    res = ["3", "3", "3"]
    first, second = winnings.hit_counts(legs, res)
    assert first == 1
    # 一等奖时二等注数 = Σ(|S_i| - 1) = (2-1)+(1-1)+(2-1) = 2
    assert second == 2


def test_second_when_one_uncovered():
    legs = [["3", "1"], ["3"], ["3", "0"]]
    res = ["3", "3", "1"]
    first, second = winnings.hit_counts(legs, res)
    assert first == 0
    # 缺场 k=2 → 二等 = |S_k| = 2
    assert second == 2


def test_no_prize_two_uncovered():
    legs = [["3"], ["3"], ["3", "0"]]
    res = ["1", "1", "3"]
    assert winnings.hit_counts(legs, res) == (0, 0)


def test_r9_hit_all_match():
    legs = [["3"], ["3", "1"], ["3"], ["3"], ["3"], ["3"], ["3"], ["3"], ["3"]]
    res = ["3", "1", "3", "3", "3", "3", "3", "3", "3"]
    assert winnings.r9_hit(legs, res) == 1


def test_r9_hit_miss_one():
    legs = [["3"]] * 9
    res = ["3"] * 8 + ["1"]
    assert winnings.r9_hit(legs, res) == 0


# ---- '*' 场次：比赛推迟/中断且 48 小时内未补赛，官方按 3/1/0 全选计算 ----
def test_hit_counts_treats_star_as_covered():
    legs = [["3"], ["1"], ["0"]]      # 第 2 场押 '1'，按字面 '*' 不在腿里
    res = ["3", "*", "0"]
    first, second = winnings.hit_counts(legs, res)
    assert first == 1
    assert second == 0                # Σ(|S_i| - 1) = 0+0+0


def test_hit_counts_multiple_stars():
    legs = [["3", "1"], ["3", "1"], ["3"]]
    res = ["*", "*", "3"]
    first, second = winnings.hit_counts(legs, res)
    assert first == 1
    assert second == 2                # (2-1)+(2-1)+(1-1)


def test_star_can_rescue_second_prize():
    legs = [["3"], ["3"], ["3"]]
    res = ["3", "*", "1"]             # 第 3 场押错；第 2 场 '*' 自动算中
    first, second = winnings.hit_counts(legs, res)
    assert first == 0
    assert second == 1                # 唯一未覆盖场是第 3 场，|S_3| = 1


def test_r9_hit_treats_star_as_covered():
    legs = [["3"]] * 9
    res = ["3"] * 8 + ["*"]
    assert winnings.r9_hit(legs, res) == 1
