"""数字彩回测：逐期走查 + 与随机选号的配对显著性。

设计见 `docs/superpowers/specs/2026-09-21-digital-lottery-design.md` §6。
这是这个子项目最重要的部分 —— 它负责证伪，而不是证实。

奖金一律取**当期抓到的实际奖级表**：
- 大乐透的奖级设置改过（2026-02-02 第 26014 期起，9 个奖级 → 7 个，三等奖从
  「5+0」并入「5+0；4+2」），所以「命中数→奖级」要按当期适用哪套规则推导，
  不能把任何一套硬编码到底。详见 `dlt_tiers`。
- 双色球奖级规则长期稳定（一～六等奖），硬编码规则 + 用当期金额算浮动奖。
- 排列三/福彩3D/排列五只按**直选**计奖：一注 2 元只买一种玩法。若同时算上组选，
  单注期望奖金会超过票价（>100% 返还率），那是模型错误而不是发现。
"""
from __future__ import annotations

import json

from football_lottery.collectors.lottery_history import LOTTERY_NAMES
from football_lottery.models import lottery_predict

BET_PRICE = lottery_predict.BET_PRICE       # 元/注，五类彩种统一 2 元
MIN_HISTORY = 30        # 走查起点的最小历史长度
DEFAULT_SEED = 20260921

# 大乐透改过奖级设置（2026-02-02，第 26014 期起）：
#   旧：9 个奖级，三等奖 5+0、四等奖 4+2、五等奖 4+1 …
#   新：7 个奖级，三等奖并入 4+2、四等奖变 4+1、五等奖并入 3+2 …
# 两套规则的命中条件不同，所以必须按当期适用哪套来判。判据是奖级表里**有没有
# 「九等奖」**（新规则取消了九等奖），而不是按日期硬编码切换点 —— 数据自己说话。
DLT_TIERS_OLD = {
    (5, 2): "一等奖", (5, 1): "二等奖", (5, 0): "三等奖",
    (4, 2): "四等奖", (4, 1): "五等奖", (3, 2): "六等奖",
    (4, 0): "七等奖", (3, 1): "八等奖", (2, 2): "八等奖",
    (3, 0): "九等奖", (2, 1): "九等奖", (1, 2): "九等奖", (0, 2): "九等奖",
}
DLT_TIERS_NEW = {
    (5, 2): "一等奖", (5, 1): "二等奖", (5, 0): "三等奖", (4, 2): "三等奖",
    (4, 1): "四等奖", (4, 0): "五等奖", (3, 2): "五等奖", (3, 1): "六等奖",
    (2, 2): "六等奖", (3, 0): "七等奖", (2, 1): "七等奖",
    (1, 2): "七等奖", (0, 2): "七等奖",
}

# 双色球：奖级规则长期稳定，硬编码
SSQ_TIERS = {
    (6, True): "一等奖", (6, False): "二等奖", (5, True): "三等奖",
    (5, False): "四等奖", (4, True): "四等奖",
    (4, False): "五等奖", (3, True): "五等奖",
    (2, True): "六等奖", (1, True): "六等奖", (0, True): "六等奖",
}
# 双色球浮动奖（一、二等奖）无兜底值，页面缺失时计 0
SSQ_FIXED = {"三等奖": 3000, "四等奖": 200, "五等奖": 10, "六等奖": 5}
# 排列类：直选固定奖金（页面金额优先，这是兜底）
DIRECT_PRIZE = {"p3": 1040, "3d": 1040, "p5": 100000}
DIRECT_TIER = {"p3": ("直选",), "3d": ("直选",), "p5": ("一等奖",)}


def dlt_tiers(prizes) -> dict[tuple[int, int], str]:
    """判断当期适用哪套大乐透奖级规则，返回 (前区命中, 后区命中) → 奖级名。

    奖级表里还有「一等奖(追加)」这类行 —— 同条件、不同奖金，我们买的是普通票，
    映射表里不含「追加」二字，命中后按奖级名取金额时自然不会误取到追加行。
    """
    names = {row.get("tier") or "" for row in prizes or []}
    has_nine = any("九等奖" in name for name in names)
    return DLT_TIERS_OLD if has_nine else DLT_TIERS_NEW


def _amount_of(prizes, tier: str) -> int | None:
    for row in prizes or []:
        if row.get("tier") == tier and row.get("amount"):
            return int(row["amount"])
    return None


def prize_for(lottery: str, picked: dict, drawn: dict, prizes) -> int:
    """一注号码在给定开奖下的奖金（元）。"""
    if lottery == "dlt":
        if not prizes:
            return 0        # 没有奖级表就判断不了适用哪套规则 —— 计 0，不猜
        front = len(set(picked["front"]) & set(drawn["front"]))
        back = len(set(picked["back"]) & set(drawn["back"]))
        tier = dlt_tiers(prizes).get((front, back))
        return (_amount_of(prizes, tier) or 0) if tier else 0
    if lottery == "ssq":
        red = len(set(picked["front"]) & set(drawn["front"]))
        blue = bool(set(picked["back"]) & set(drawn["back"]))
        tier = SSQ_TIERS.get((red, blue))
        if not tier:
            return 0
        return _amount_of(prizes, tier) or SSQ_FIXED.get(tier, 0)
    if picked["digits"] != drawn["digits"]:
        return 0
    return _amount_of(prizes, DIRECT_TIER[lottery][0]) or DIRECT_PRIZE[lottery]


def hits_for(lottery: str, picked: dict, drawn: dict) -> dict:
    """命中明细，用于报告里的 avg_hits。"""
    if lottery in ("dlt", "ssq"):
        return {"front": len(set(picked["front"]) & set(drawn["front"])),
                "back": len(set(picked["back"]) & set(drawn["back"]))}
    return {"digits": sum(1 for a, b in zip(picked["digits"], drawn["digits"]) if a == b)}


def load_draws(conn, lottery: str) -> list[dict]:
    """按时序（issue 升序）取出全部开奖，供走查使用。"""
    rows = conn.execute(
        """SELECT issue, draw_date, numbers, prizes FROM lottery_draw
           WHERE lottery=? ORDER BY issue""", (lottery,)).fetchall()
    return [{"lottery": lottery, "issue": r["issue"], "draw_date": r["draw_date"],
             "numbers": json.loads(r["numbers"]),
             "prizes": json.loads(r["prizes"]) if r["prizes"] else None}
            for r in rows]


def run_backtest(draws: list[dict], lottery: str, strategies=None,
                 window: int = lottery_predict.DEFAULT_WINDOW,
                 n_bets: int = lottery_predict.DEFAULT_BETS,
                 seed=DEFAULT_SEED, min_history: int = MIN_HISTORY) -> dict:
    """逐期走查。draws 必须按时序升序，且含 numbers/prizes 字段。

    第 t 期只用 draws[:t] —— 这是防泄漏的唯一实现点，测试有内容级断言。
    """
    # random 是基线，恒在；调用方只点别的策略时也必须带上，否则没有对照
    strategies = list(dict.fromkeys(["random", *(strategies or lottery_predict.STRATEGIES)]))
    per_draw: dict[str, dict[str, float]] = {s: {} for s in strategies}
    invested: dict[str, float] = {s: 0.0 for s in strategies}
    bets_total: dict[str, int] = {s: 0 for s in strategies}
    hits_sum: dict[str, dict[str, float]] = {s: {} for s in strategies}
    win_draws: dict[str, int] = {s: 0 for s in strategies}
    evaluated = 0

    for idx, drawn in enumerate(draws):
        history = draws[:idx]
        if len(history) < min_history:
            continue
        evaluated += 1
        issue = drawn["issue"]
        drawn_numbers = drawn["numbers"]
        prizes = drawn.get("prizes")
        for strategy in strategies:
            bets = lottery_predict.predict_bets(lottery, history, strategy, n_bets,
                                                f"{seed}-{issue}", window)
            total = 0.0
            for picked in bets:
                total += prize_for(lottery, picked, drawn_numbers, prizes)
                for key, value in hits_for(lottery, picked, drawn_numbers).items():
                    hits_sum[strategy][key] = hits_sum[strategy].get(key, 0.0) + value
            # 按**实际发出的注数**算投入：去重后可能少于请求注数，按请求数算会虚增投入
            cost = len(bets) * BET_PRICE
            invested[strategy] += cost
            bets_total[strategy] += len(bets)
            per_draw[strategy][issue] = total - cost
            if total > 0:
                win_draws[strategy] += 1

    metrics = {}
    for strategy in strategies:
        profits = per_draw[strategy]
        spent = invested[strategy]
        returned = sum(profits.values()) + spent
        total_bets = max(1, bets_total[strategy])
        metrics[strategy] = {
            "draws": len(profits),
            "bets": bets_total[strategy],
            "invested": spent,
            "returned": returned,
            "roi": (returned / spent - 1.0) if spent else None,
            "return_rate": (returned / spent) if spent else None,
            "win_draws": win_draws[strategy],
            "win_rate": (win_draws[strategy] / len(profits)) if profits else None,
            "avg_hits": {k: v / total_bets for k, v in hits_sum[strategy].items()},
        }
    return {"lottery": lottery, "evaluated": evaluated, "n_bets": n_bets,
            "window": window, "per_draw": per_draw, "metrics": metrics,
            "paired": paired_significance(per_draw)}


def paired_significance(per_draw: dict[str, dict[str, float]],
                        baseline: str = "random", alpha: float = 0.05) -> list[dict]:
    """逐期配对 t 检验：同一期上「策略收益 − 随机收益」的均值是否为 0。

    同一期的开奖是同一个随机事件，配对能消掉绝大部分方差，比各自跟理论值比更有力。
    """
    import numpy as np
    from scipy import stats

    base = per_draw.get(baseline)
    if not base:
        return []
    rows = []
    for strategy, profits in per_draw.items():
        if strategy == baseline:
            continue
        diffs = np.asarray([profits[i] - base[i] for i in profits if i in base],
                           dtype=float)
        n = diffs.size
        if n < 30:
            continue
        sd = float(diffs.std(ddof=1))
        if sd == 0:
            continue        # 完全无差异 → 不构成「击败随机」
        mean = float(diffs.mean())
        se = sd / (n ** 0.5)
        t_stat = mean / se
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df=n - 1)))
        crit = float(stats.t.ppf(1 - alpha / 2, df=n - 1))
        rows.append({
            "strategy": strategy, "n": n, "mean_diff": mean, "se": se,
            "ci_low": mean - crit * se, "ci_high": mean + crit * se,
            "t": t_stat, "p": p_value,
            "beats_random": bool(p_value < alpha and mean > 0),
        })
    rows.sort(key=lambda r: r["p"])
    return rows


def summarize(result: dict) -> str:
    """人类可读的回测报告。"""
    lottery = result["lottery"]
    lines = [f"== {LOTTERY_NAMES[lottery]}（{lottery}） 走查 {result['evaluated']} 期"
             f" · 每期 {result['n_bets']} 注 · 窗口 {result['window']} 期 =="]
    lines.append(f"{'策略':<8}{'返还率':>10}{'盈亏':>14}{'中奖期占比':>12}   命中均值")
    for strategy, m in result["metrics"].items():
        rate = f"{m['return_rate'] * 100:.1f}%" if m["return_rate"] else "—"
        profit = m["returned"] - m["invested"]
        win = f"{m['win_rate'] * 100:.1f}%" if m["win_rate"] is not None else "—"
        hits = " ".join(f"{k}={v:.2f}" for k, v in m["avg_hits"].items())
        lines.append(f"{strategy:<8}{rate:>10}{profit:>14.2f}{win:>12}   {hits}")
    if result["paired"]:
        lines.append("")
        lines.append("与 random 的逐期配对检验（正差值 = 策略更赚）：")
        for row in result["paired"]:
            flag = "  ← 显著" if row["beats_random"] else ""
            lines.append(
                f"  {row['strategy']:<8} 差值均值 {row['mean_diff']:+.3f} "
                f"95%CI [{row['ci_low']:+.3f}, {row['ci_high']:+.3f}] "
                f"p={row['p']:.3f}{flag}")
    else:
        lines.append("")
        lines.append("与 random 的逐期配对检验：无可比对数据（策略与随机完全同收益）。")
    lines.append("")
    lines.append("注：摇奖机是独立同分布，历史号码对下一期没有信息量。")
    lines.append("    这里同时跑了多条策略，α=0.05 下**平均每 20 次检验就会有一次假显著**；")
    lines.append("    看到「显著」的第一反应应当是查泄漏或判定 bug，而不是庆祝。")
    return "\n".join(lines)


def save_result(conn, result: dict) -> None:
    """回测结果落库，供页面读取（页面实时跑走查太慢）。"""
    from datetime import datetime

    paired = {row["strategy"]: row for row in result["paired"]}
    params = json.dumps({"window": result["window"], "bets": result["n_bets"],
                         "draws": result["evaluated"]}, ensure_ascii=False)
    ran_at = datetime.now().isoformat(timespec="seconds")
    for strategy, metrics in result["metrics"].items():
        conn.execute(
            """INSERT INTO lottery_backtest(lottery, strategy, params, metrics,
                                            paired, ran_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(lottery, strategy) DO UPDATE SET
                 params=excluded.params, metrics=excluded.metrics,
                 paired=excluded.paired, ran_at=excluded.ran_at""",
            (result["lottery"], strategy, params,
             json.dumps(metrics, ensure_ascii=False),
             json.dumps(paired.get(strategy), ensure_ascii=False), ran_at))
    conn.commit()
