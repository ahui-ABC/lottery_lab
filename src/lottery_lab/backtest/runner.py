"""Walk-forward 回测框架（实现计划 T9 / 设计 §5）。

要点：
- 始终只用过去的数据拟合与预测（含 refit 节奏下的"上次拟合到当下"）；
- 预测时把当前 match 的 date 当下时间，past 仅含 date < current。
- 跳过 result 为空的场次。
- 跳过 date < start_date 的早段。
- 返回 `PredRecord` 列表（统一字段：match_id, date, probs, outcome, source）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable


@dataclass
class PredRecord:
    match_id: int
    date: date
    probs: list[float]
    outcome: str
    source: str


def walk_forward(
    matches: list[dict],
    model_factory: Callable[[list[dict]], Callable[[dict], list[float]]],
    start_date: date,
    refit_days: int = 7,
    source: str = "model",
) -> list[PredRecord]:
    """`matches` 需按 date 升序，每场至少含 id/date/result。"""
    records: list[PredRecord] = []
    model = None
    last_fit_date: date | None = None
    for i, m in enumerate(matches):
        cur_date: date = m["date"]
        # Never train on another fixture from the same calendar day.
        past = [p for p in matches[:i]
                if p.get("date") < cur_date and p.get("result")]
        if not past:
            continue
        if model is None or (
            last_fit_date is not None
            and (cur_date - last_fit_date) >= timedelta(days=refit_days)
        ):
            model = model_factory(past)
            last_fit_date = cur_date
        if cur_date < start_date or not m.get("result"):
            continue
        probs = model(m)
        records.append(PredRecord(m["id"], cur_date, probs, m["result"], source))
    return records
