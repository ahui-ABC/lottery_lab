"""数字彩实际战绩跟踪。

`lottery_predict` 负责生成推荐，`lottery_backtest` 负责证伪（历史走查），
本模块负责第三件事：**跟踪真实发生的推荐与开奖，算实际盈亏**。

和回测的区别很重要：回测是事后在历史上重放策略，本模块只看
`lottery_prediction` 里**真的产生过**的推荐（当时不知道开奖结果）。
所以它没有回测的样本量，但也没有任何事后诸葛的余地。

口径：
- 只统计**已对奖**（`prize IS NOT NULL`）的期次；未开奖的单独计为 pending。
- 投入 = **实际发出的注数** × 2 元。`predict_bets` 去重后可能少于请求注数，
  按请求注数算会虚增投入 —— 回测里已经吃过这个亏。
"""
from __future__ import annotations

import json

from lottery_lab.collectors.lottery_history import LOTTERY_NAMES
from lottery_lab.models.lottery_predict import BET_PRICE, STRATEGY_LABELS


def _bets_count(raw: str | None) -> int:
    try:
        return len(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return 0


def summary(conn, lottery: str) -> dict:
    """某彩种按策略汇总已对奖的推荐。

    只算已开奖的期次；pending 单独报，不掺进盈亏 —— 把未开奖的算成 0 返还
    会让盈亏随着「还有几期没开」上下跳，那是假数字。
    """
    rows = _rows(conn, lottery, scored=True)
    by_strategy: dict[str, dict] = {}
    for row in rows:
        acc = by_strategy.setdefault(row["strategy"], {
            "strategy": row["strategy"],
            "label": STRATEGY_LABELS.get(row["strategy"], row["strategy"]),
            "issues": 0, "bets": 0, "invested": 0.0, "returned": 0.0, "win_issues": 0,
        })
        acc["issues"] += 1
        acc["bets"] += row["bets"]
        acc["invested"] += row["invested"]
        acc["returned"] += row["returned"]
        if row["returned"] > 0:
            acc["win_issues"] += 1

    strategies = []
    for code in sorted(by_strategy):
        acc = by_strategy[code]
        invested = acc["invested"]
        acc["profit"] = acc["returned"] - invested
        acc["return_rate"] = (acc["returned"] / invested) if invested else None
        acc["win_rate"] = (acc["win_issues"] / acc["issues"]) if acc["issues"] else None
        strategies.append(acc)

    return {"lottery": lottery, "name": LOTTERY_NAMES[lottery],
            "strategies": strategies,
            "pending": sum(1 for r in _rows(conn, lottery, scored=False))}


def _rows(conn, lottery: str, scored: bool) -> list[dict]:
    """把原始行展开成「每条预测的投入/返还」。

    `bets` 是 JSON，注数得解出来 —— 所以过滤放不到 SQL 里，只能取回来算。
    单彩种预测行数在千级别，代价可以忽略。
    """
    condition = "IS NOT NULL" if scored else "IS NULL"
    raw = conn.execute(
        f"""SELECT strategy, bets, prize FROM lottery_prediction
            WHERE lottery=? AND prize {condition} ORDER BY target_issue""",
        (lottery,)).fetchall()
    out = []
    for r in raw:
        bets = _bets_count(r["bets"])
        out.append({"strategy": r["strategy"], "bets": bets,
                    "invested": bets * BET_PRICE,
                    "returned": float(r["prize"] or 0)})
    return out


def overview(conn) -> list[dict]:
    """五个彩种的摘要，供总览页。"""
    from lottery_lab.collectors.lottery_history import LOTTERIES

    out = []
    for code in LOTTERIES:
        last = conn.execute(
            """SELECT issue, draw_date, numbers FROM lottery_draw
               WHERE lottery=? ORDER BY issue DESC LIMIT 1""", (code,)).fetchone()
        summary_ = summary(conn, code)
        out.append({
            "lottery": code,
            "name": LOTTERY_NAMES[code],
            "latest": {"issue": last["issue"], "draw_date": last["draw_date"],
                       "numbers": json.loads(last["numbers"])} if last else None,
            "scored_issues": max((s["issues"] for s in summary_["strategies"]),
                                 default=0),
            "pending": summary_["pending"],
            "strategies": [{"label": s["label"], "profit": s["profit"],
                            "return_rate": s["return_rate"]}
                           for s in summary_["strategies"]],
        })
    return out


def timeline(conn, lottery: str, strategy: str) -> list[dict]:
    """某彩种某策略的逐期累计盈亏，供曲线图。

    只含已对奖期次 —— 曲线画的是**已实现**的盈亏。
    """
    raw = conn.execute(
        """SELECT p.target_issue, p.bets, p.prize, d.draw_date
           FROM lottery_prediction p
           LEFT JOIN lottery_draw d
             ON d.lottery = p.lottery AND d.issue = p.target_issue
           WHERE p.lottery=? AND p.strategy=? AND p.prize IS NOT NULL
           ORDER BY p.target_issue""",
        (lottery, strategy)).fetchall()
    out = []
    cumulative = 0.0
    for r in raw:
        profit = float(r["prize"] or 0) - _bets_count(r["bets"]) * BET_PRICE
        cumulative += profit
        out.append({"issue": r["target_issue"], "date": r["draw_date"] or r["target_issue"],
                    "profit": profit, "cumulative": cumulative})
    return out


def recent(conn, lottery: str, limit: int = 15) -> list[dict]:
    """最近若干期的各策略明细，供页面表格。含未开奖的（标 pending）。"""
    issues = [r["target_issue"] for r in conn.execute(
        """SELECT DISTINCT target_issue FROM lottery_prediction
           WHERE lottery=? ORDER BY target_issue DESC LIMIT ?""",
        (lottery, limit))]
    if not issues:
        return []

    marks = ",".join("?" * len(issues))
    raw = conn.execute(
        f"""SELECT p.target_issue, p.strategy, p.bets, p.prize,
                   d.draw_date, d.numbers
            FROM lottery_prediction p
            LEFT JOIN lottery_draw d
              ON d.lottery = p.lottery AND d.issue = p.target_issue
            WHERE p.lottery=? AND p.target_issue IN ({marks})
            ORDER BY p.target_issue DESC, p.strategy""",
        (lottery, *issues)).fetchall()

    grouped: dict[str, dict] = {}
    for r in raw:
        entry = grouped.setdefault(r["target_issue"], {
            "issue": r["target_issue"], "draw_date": r["draw_date"], "strategies": []})
        if r["numbers"]:
            entry["numbers"] = json.loads(r["numbers"])
        bets = _bets_count(r["bets"])
        entry["strategies"].append({
            "strategy": r["strategy"],
            "label": STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
            "pending": r["prize"] is None,
            "invested": bets * BET_PRICE,
            "returned": float(r["prize"]) if r["prize"] is not None else None,
            "profit": (float(r["prize"]) - bets * BET_PRICE)
            if r["prize"] is not None else None,
        })
    return [grouped[i] for i in issues]
