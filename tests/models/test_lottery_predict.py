import random
from collections import Counter

from lottery_lab.models import lottery_predict as lp

DIGITS = [str(i) for i in range(10)]
FRONT35 = [f"{i:02d}" for i in range(1, 36)]


def _draw(digits=(), front=(), back=()):
    if front or back:
        return {"numbers": {"front": list(front), "back": list(back)}}
    return {"numbers": {"digits": list(digits)}}


def _weights(strategy, history, field, universe, window=None):
    """直接测权重函数。

    策略的定义全在权重上，抽样本身是随机的 —— 拿单次抽样结果去断言「选了哪个号」
    是假测试（会随机变红）。要验证「热号偏好高频号」，就该断言权重排序。
    """
    series = lp._series(history, field)
    if window is not None:
        series = series[-window:]
    return lp._weights(strategy, series, universe, random.Random(0))


def _top_by_weight(strategy, history, field, universe, k, window=None, reverse=True):
    weights = _weights(strategy, history, field, universe, window)
    order = sorted(zip(universe, weights), key=lambda p: -p[1] if reverse else p[1])
    return {value for value, _ in order[:k]}


# ---- 抽样器 ---------------------------------------------------------------

def test_weighted_sample_respects_dominant_weight():
    rng = random.Random(0)
    assert lp.weighted_sample([1000.0, 1.0, 1.0, 1.0], 1, rng) == [0]


def test_weighted_sample_returns_sorted_unique_indices():
    rng = random.Random(1)
    picked = lp.weighted_sample([1.0] * 35, 5, rng)
    assert len(set(picked)) == 5
    assert picked == sorted(picked)


def test_weighted_sample_is_uniform_for_uniform_weights():
    """均匀权重下各号码被选中频率应接近 5/35。"""
    counts = {i: 0 for i in range(35)}
    rng = random.Random(7)
    for _ in range(4000):
        for i in lp.weighted_sample([1.0] * 35, 5, rng):
            counts[i] += 1
    expected = 4000 * 5 / 35
    for c in counts.values():
        assert 0.7 * expected < c < 1.3 * expected


def test_zero_weight_never_selected():
    rng = random.Random(0)
    for _ in range(200):
        assert 0 not in lp.weighted_sample([0.0, 1.0, 1.0], 2, rng)


# ---- 权重函数（策略真正被定义的地方）--------------------------------------

def test_hot_weights_favor_frequent_numbers():
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    assert _top_by_weight("hot", history, "front", FRONT35, 5) == {
        "01", "02", "03", "04", "05"}


def test_cold_weights_favor_absent_numbers():
    """cold 给「出现少」的号**更高**权重 —— 所以缺席号码会浮到权重榜顶部。"""
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    top = _top_by_weight("cold", history, "front", FRONT35, 5)
    assert not top & {"01", "02", "03", "04", "05"}


def test_overdue_weights_favor_longest_absent():
    """所有数字都出现过、且 '1' 最久没出 —— 它的权重必须最大。

    构造上要保证 0/3-9 都出现过，否则它们会拿到「窗口内从未出现」的上界遗漏值，
    反而压过 '1'。
    """
    cycle = ["0", "2", "3", "4", "5", "6", "7", "8", "9"]
    history = [_draw(digits=["1", "1", "1"])]
    for i in range(20):
        history.append(_draw(digits=[cycle[i % len(cycle)]] * 3))
    weights = _weights("overdue", history, "d0", DIGITS)
    assert DIGITS[max(range(10), key=lambda i: weights[i])] == "1"


def test_weighted_weights_are_smoothed_toward_frequency():
    """贝叶斯路径是「平滑后的热号」：高频号权重的期望更高，但不会像 hot 那样悬殊。"""
    history = [_draw(digits=["1", "1", "1"])] * 50 + [_draw(digits=["2", "2", "2"])] * 5
    series = lp._series(history, "d0")
    hot = lp._weights("hot", series, DIGITS, random.Random(0))
    assert hot.index(max(hot)) == 1

    wins = Counter()
    for seed in range(200):
        w = lp._weights("weighted", series, DIGITS, random.Random(seed))
        wins[DIGITS[max(range(10), key=lambda i: w[i])]] += 1
    assert wins.most_common(1)[0][0] == "1"


def test_digit_strategies_are_per_position():
    """按位统计：百位全是 9、十位全是 0、个位全是 5 —— 三个位置的权重各归各。"""
    history = [_draw(digits=["9", "0", "5"]) for _ in range(30)]
    argmaxes = []
    for pos in range(3):
        w = _weights("hot", history, f"d{pos}", DIGITS)
        argmaxes.append(DIGITS[max(range(10), key=lambda i: w[i])])
    assert argmaxes == ["9", "0", "5"]


# ---- predict_one ----------------------------------------------------------

def test_predict_one_is_deterministic_and_history_sensitive():
    """不依赖任何模块级缓存：同一份历史 + 同一随机流 → 同一结果；
    换一份历史 → 输出分布必须跟着变。"""
    past = [_draw(digits=["1", "2", "3"])] * 40
    extended = past + [_draw(digits=["7", "8", "9"])] * 40

    def mode_first_digit(history):
        seen = Counter()
        for seed in range(200):
            picked = lp.predict_one("p3", history, "hot", random.Random(seed), window=40)
            seen[picked["digits"][0]] += 1
        return seen.most_common(1)[0][0]

    assert mode_first_digit(past) == "1"
    assert mode_first_digit(extended) == "7"
    assert (lp.predict_one("p3", past, "hot", random.Random(3), window=40)
            == lp.predict_one("p3", past, "hot", random.Random(3), window=40))


def test_all_strategies_produce_valid_shape():
    history = [_draw(front=[f"{i:02d}" for i in range(1, 7)], back=["01"])
               for _ in range(5)]
    for strategy in lp.STRATEGIES:
        picked = lp.predict_one("ssq", history, strategy, random.Random(0), window=5)
        assert len(picked["front"]) == 6
        assert len(set(picked["front"])) == 6
        assert len(picked["back"]) == 1
        assert all(1 <= int(x) <= 33 for x in picked["front"])
        assert all(1 <= int(x) <= 16 for x in picked["back"])


def test_all_strategies_respect_digit_universe():
    history = [_draw(digits=list("12345")) for _ in range(10)]
    for strategy in lp.STRATEGIES:
        picked = lp.predict_one("p5", history, strategy, random.Random(0), window=10)
        assert len(picked["digits"]) == 5
        assert all(d.isdigit() and len(d) == 1 for d in picked["digits"])


def test_transient_history_does_not_crash():
    for strategy in lp.STRATEGIES:
        picked = lp.predict_one("p5", [_draw(digits=list("12345"))],
                                strategy, random.Random(0), window=100)
        assert len(picked["digits"]) == 5


def test_unknown_strategy_raises():
    import pytest
    with pytest.raises(ValueError):
        lp.predict_one("p3", [_draw(digits=["1", "2", "3"])], "nope",
                       random.Random(0), window=5)


# ---- predict_bets ---------------------------------------------------------

def test_predict_bets_returns_requested_count_and_unique():
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    bets = lp.predict_bets("dlt", history, "hot", 5, seed=1, window=20)
    assert len(bets) == 5
    keys = {tuple(b["front"] + b["back"]) for b in bets}
    assert len(keys) == 5          # 不留重复注单


def test_predict_bets_is_deterministic_for_same_seed():
    history = [_draw(digits=["1", "2", "3"]) for _ in range(10)]
    a = lp.predict_bets("p3", history, "hot", 5, seed="x", window=10)
    b = lp.predict_bets("p3", history, "hot", 5, seed="x", window=10)
    assert a == b


def test_predict_bets_never_exceeds_requested():
    history = [_draw(digits=["1", "2", "3"]) for _ in range(3)]
    bets = lp.predict_bets("p3", history, "hot", 5, seed=1, window=3)
    assert 0 < len(bets) <= 5


# ---- 期号 ------------------------------------------------------------------

def test_next_issue_same_year():
    from datetime import date
    assert lp.next_issue("2026107", date(2026, 9, 21)) == "2026108"


def test_next_issue_rolls_over_year():
    from datetime import date
    assert lp.next_issue("2026158", date(2027, 1, 2)) == "2027001"
