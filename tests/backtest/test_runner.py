"""walk-forward runner 测试。"""
from dataclasses import dataclass
from datetime import date, timedelta
from football_lottery.backtest.runner import walk_forward, PredRecord


def _mk_matches(n: int):
    base = date(2025, 1, 1)
    return [
        {"id": i + 1, "date": base + timedelta(days=i), "result": "H" if i % 3 else "D"}
        for i in range(n)
    ]


def test_walk_forward_only_uses_past():
    """每次预测，模型只能看到该 match 之前的数据。"""
    matches = _mk_matches(6)

    seen_lengths: list[int] = []

    def factory(past):
        seen_lengths.append(len(past))

        def predict(m):
            # 只允许参考 past 长度；避免模型在闭包里抓未来
            return [0.4, 0.3, 0.3]
        return predict

    out = walk_forward(
        matches=matches,
        model_factory=factory,
        start_date=date(2025, 1, 1),
        refit_days=1,
        source="t",
    )
    # 第 k 次调用时，past 长度就是 k+1（首场无 past 不计入）
    for k, _ in enumerate(out):
        assert seen_lengths[k] == k + 1
    # 全部 6 场里，至少会有 5 场有 past（首场没有）
    assert len(out) == 5
    # 输出统一字段
    for r in out:
        assert isinstance(r, PredRecord)
        assert r.probs and len(r.probs) == 3


def test_walk_forward_filter_by_start_date():
    matches = _mk_matches(4)
    factory_called = [0]

    def factory(past):
        factory_called[0] += 1
        return lambda m: [0.5, 0.3, 0.2]
    out = walk_forward(
        matches=matches,
        model_factory=factory,
        start_date=date(2025, 1, 3),
        refit_days=1,
    )
    # start_date == 2025-01-03, 则 1/1, 1/2 都被滤掉
    assert len(out) == 2
