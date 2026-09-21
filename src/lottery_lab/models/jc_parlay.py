"""竞彩串关方案：按回测结论选场 + 多重串关组合。

## 设计依据（全部来自 `jc_backtest` 的实测）

1. **选场规则 = 市场概率最高**。三玩法一致优于"博冷"（选高赔率）：
   让球 75.3% vs 69.0%、比分 75.1% vs 67.6%、半全场 64.6% vs 60.6%。
2. **多重串关组合**（同时下 k=2..N 串1）远优于单押 N串1：
   比分 4 场从理论 22.1% 提升到实测 75.1% —— 把高抽水的长串拆成低抽水的短串。
3. **仍是负期望**，本质是抽水决定的。
   本模块产出的是"在被验证为最优的规则下该选哪几场"，不是盈利保证。

## 场次多少不是门槛，「有没有把握」才是

`n_legs` 是**上限**不是必须达到的场次：当天只有 3 场可选就押 3 场，
只有 2 场就押 2 场，**只要这 2 场够有把握**。凑不够 `n_legs` 而整天不出方案，
和为了凑数把没把握的场次也塞进去，两者都不对。

「有把握」的标准按玩法而异，见 `MIN_PROB` —— 它不是拍脑袋定的，是样本外
验证的结果，且**不同玩法结论不同**（比分加门槛反而更差）。

## 默认配置

比分上限 4 场 / 半全场上限 4 场 / 让球胜平负上限 5 场（用户实际玩法）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date
from itertools import combinations

from lottery_lab.models import jc_predict

RESULT_COLUMN = {
    "had": "result_had", "hhad": "result_hhad",
    "crs": "result_crs", "ttg": "result_ttg", "hafu": "result_hafu",
}

# 玩法 → 选场**上限**（回测验证过的用户实际玩法）
DEFAULT_POOL_CONFIG = {
    "hhad": 5,
    "crs": 4,
    "hafu": 4,
}
DEFAULT_UNIT = 2.0          # 每注金额（元）
DEFAULT_MIN_COMBO = 2       # 最小串关数

# 各玩法的「有把握」门槛：选项的市场概率低于它就不押。0 = 不设门槛。
#
# 依据：2021-01 起 1975 天数据的样本外验证（前 70% 天选门槛、后 30% 天验证）。
# 返还率 = 总奖金 / 总投入。
#
#   让球   门槛 0.55 → 样本外 83.7%（无门槛 75.5%），保留 468/593 天有方案。
#          0.58 / 0.60 也有效（87.9% / 86.2%）但天数掉到 403 / 349，
#          三个点在噪声量级内，取覆盖最多的 0.55。
#   比分   加门槛**反而更差**：0.11 → 63.1%（无门槛 75.0%），再高更差。
#          而且比分基线本身就极不稳定（训练集 64.2% / 验证集 99.5%，
#          由几笔大额命中主导）——没有证据支持加门槛，就不加。
#   半全场 只有 0.50 一个点（70.7%）高过基线 67.5%，且要牺牲 2/3 的天数，
#          曲线非单调（0.40/0.45/0.48 都低于基线）。证据不足，不加。
#
# 所以「有把握」在三个玩法上含义不同：让球是绝对门槛，比分和半全场是
# 「当天市场概率最高的那几场」——后者是 1731 期回测验证过的选场规则，
# 不是放任乱猜。给比分套 0.55 会把场次全部滤光（比分最高概率通常仅 12%~15%）。
MIN_PROB = {"hhad": 0.55, "crs": 0.0, "hafu": 0.0}


def _live_filter(conn: sqlite3.Connection, day: str):
    """在售过滤的 SQL 片段与参数：`(片段, 参数)`。

    片段为 `False` 表示「当天快照明确为空」—— 此刻一场都买不了，直接没有候选。
    当天没有快照时返回空片段：历史重放（`--date`）与老数据没有「在售」概念，
    必须保持不过滤的旧行为，否则会凭空改变历史结论。
    """
    live = jc_predict.live_match_ids(conn, day)
    if live is None:
        return "", []
    if not live:
        return False, []
    ids = sorted(live)
    return f"AND p.match_id IN ({','.join('?' * len(ids))})", ids


def pick_legs(conn: sqlite3.Connection, day: str, pool: str,
              n_legs: int, min_prob: float = 0.0) -> list[dict]:
    """按「市场概率最高」为该玩法选出最多 n_legs 场，且只保留有把握的。

    直接复用 `jc_predictions` 里 market 路径的结果 —— 选场规则已由回测确定，
    这里不需要重算信号。

    `min_prob` 过滤掉低于门槛的候选，是「有把握才推」的实现点。
    过滤在 SQL 里做（而不是取回来再筛）是为了让 `LIMIT n_legs` 作用于
    真正入选的那几场 —— 否则会把名额浪费在被门槛刷掉的场上。

    同理，已停售的场次也不能占名额（见 `_live_filter`）：重算的目的是
    「现在还能买什么」，混进买不了的场次等于误导下注。
    """
    where, params = _live_filter(conn, day)
    if where is False:              # 快照明确：此刻没有在售场次
        return []
    rows = list(conn.execute(
        f"""SELECT p.match_id, p.pick, p.odds, p.prob,
                   m.home_team, m.away_team, m.league_name
            FROM jc_predictions p
            JOIN jc_matches m ON m.match_id = p.match_id
            WHERE p.predicted_on = ? AND p.pool = ? AND p.method = 'market'
              AND p.pick IS NOT NULL AND p.odds IS NOT NULL
              AND COALESCE(p.prob, 0) >= ?
              {where}
            ORDER BY p.prob DESC
            LIMIT ?""",
        (day, pool, min_prob, *params, n_legs),
    ))
    return [{
        "match_id": r["match_id"],
        "home": r["home_team"],
        "away": r["away_team"],
        "league": r["league_name"],
        "pick": r["pick"],
        "odds": float(r["odds"]),
        "prob": r["prob"],
        "hit": None,
    } for r in rows]


def build_bets(legs: list[dict], unit: float, min_combo: int) -> list[dict]:
    """生成所有 k 串 1 组合（k = min_combo..len(legs)）。"""
    bets = []
    ids = [x["match_id"] for x in legs]
    for k in range(min_combo, len(ids) + 1):
        for combo in combinations(ids, k):
            bets.append({"legs": list(combo), "size": k, "amount": unit})
    return bets


def build_plan(conn: sqlite3.Connection, day: str, pool: str, n_legs: int,
               unit: float = DEFAULT_UNIT,
               min_combo: int = DEFAULT_MIN_COMBO,
               min_prob: float | None = None) -> dict | None:
    """生成一个玩法的方案；连最小串关数都凑不齐时返回 None。

    `n_legs` 是上限不是必须：当天有 3 场有把握的就押 3 场，不因为凑不满 5 场
    而整天放弃，也不为了凑数把没把握的塞进来。

    `min_prob=None` 时取该玩法的默认门槛（见 `MIN_PROB`）。
    """
    if min_prob is None:
        min_prob = MIN_PROB.get(pool, 0.0)
    legs = pick_legs(conn, day, pool, n_legs, min_prob=min_prob)
    # 2 串 1 至少要有 2 场，这是组合结构决定的硬下限，不是策略选择
    if len(legs) < max(2, min_combo):
        return None
    return {
        "plan_date": day,
        "pool": pool,
        "n_legs": len(legs),          # 实际场次，可能少于上限
        "n_legs_max": n_legs,
        "min_prob": min_prob,
        "unit": unit,
        "min_combo": min_combo,
        "legs": legs,
        "bets": build_bets(legs, unit, min_combo),
    }


def skip_reason(conn: sqlite3.Connection, day: str, pool: str, n_legs: int,
                min_prob: float | None = None) -> dict:
    """当天没方案时，说清是哪一种「没」。

    三种情况对用户意味着完全不同的事，不能混为一谈：
      - 当天根本没有该玩法的候选场次 → 没比赛
      - 有候选但都已经停售          → 有比赛，但买不到了
      - 有候选但没有一场达到门槛     → 有比赛也能买，但都没把握
    把「买不到」说成「没把握」是误导：用户会去等赔率变好，而不是换一天。

    字段口径：`candidates` 是过滤前的候选场次数（回答"当天有多少场"），
    `bettable` 是其中还能买的，`qualified` 是在能买的里面达标的。
    """
    if min_prob is None:
        min_prob = MIN_PROB.get(pool, 0.0)
    total = conn.execute(
        """SELECT COUNT(*) FROM jc_predictions
           WHERE predicted_on=? AND pool=? AND method='market'
             AND pick IS NOT NULL AND odds IS NOT NULL""",
        (day, pool)).fetchone()[0]

    live = jc_predict.live_match_ids(conn, day)
    off_sale = 0
    if live is not None:
        if not live:
            off_sale = total          # 快照为空：候选全在停售之列
        else:
            off_sale = conn.execute(
                """SELECT COUNT(*) FROM jc_predictions
                   WHERE predicted_on=? AND pool=? AND method='market'
                     AND pick IS NOT NULL AND odds IS NOT NULL
                     AND match_id NOT IN (%s)""" % ",".join("?" * len(live)),
                (day, pool, *sorted(live))).fetchone()[0]

    qualified = len(pick_legs(conn, day, pool, n_legs, min_prob=min_prob))
    return {"candidates": total, "off_sale": off_sale,
            "bettable": total - off_sale, "qualified": qualified,
            "min_prob": min_prob, "n_legs_max": n_legs,
            "live_known": live is not None}


def skip_text(why: dict) -> str:
    """把 `skip_reason` 的计数翻成一句人话。CLI 与页面共用，口径不会漂。

    顺序不能反：先判「买不到」，再判「没把握」。说反（"0 场达到门槛"）会让用户
    去等赔率变好，而真实原因是这几场已经不能下注了。
    """
    if not why["live_known"]:
        if why["candidates"] == 0:
            return "今日无该玩法在售场次"
        return (f"{why['candidates']} 场候选中 0 场达到把握门槛 "
                f"{why['min_prob']:.0%}")
    if why["bettable"] == 0:
        if why["off_sale"] > 0:
            return f"今日候选 {why['candidates']} 场，均已停售"
        return "今日无该玩法在售场次"
    if why["qualified"] == 0:
        return (f"当前可买 {why['bettable']} 场，均未达到把握门槛 "
                f"{why['min_prob']:.0%}")
    return f"当前可买 {why['bettable']} 场中仅 {why['qualified']} 场达标，不足 2 串 1"


def save_plan(conn: sqlite3.Connection, plan: dict) -> int:
    """入库。同一天同玩法重跑会覆盖（赔率可能已变），覆盖前把旧版归档。

    留档是为了让「重算改了什么」有据可查 —— 赔率刷新后换了哪几场、哪个选项，
    只看最新版是看不出来的。主表仍只有最新版，盈亏与对奖都只认它，
    所以归档不会造成重复记账。
    """
    now = datetime.now().isoformat(timespec="seconds")
    prev = conn.execute(
        "SELECT * FROM jc_parlay_plans WHERE plan_date=? AND pool=?",
        (plan["plan_date"], plan["pool"])).fetchone()
    if prev is not None:
        revision = conn.execute(
            """SELECT COUNT(*) FROM jc_parlay_plan_history
               WHERE plan_date=? AND pool=?""",
            (plan["plan_date"], plan["pool"])).fetchone()[0] + 1
        conn.execute(
            """INSERT INTO jc_parlay_plan_history(
                   plan_date, pool, revision, n_legs, unit, min_combo,
                   legs_json, bets_json, invested, returned, winning_bets,
                   scored_at, created_at, archived_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prev["plan_date"], prev["pool"], revision, prev["n_legs"],
             prev["unit"], prev["min_combo"], prev["legs_json"],
             prev["bets_json"], prev["invested"], prev["returned"],
             prev["winning_bets"], prev["scored_at"], prev["created_at"], now))
    conn.execute(
        """INSERT INTO jc_parlay_plans(
               plan_date, pool, n_legs, unit, min_combo,
               legs_json, bets_json, created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(plan_date, pool) DO UPDATE SET
               n_legs=excluded.n_legs, unit=excluded.unit,
               min_combo=excluded.min_combo, legs_json=excluded.legs_json,
               bets_json=excluded.bets_json, created_at=excluded.created_at,
               invested=NULL, returned=NULL, winning_bets=NULL, scored_at=NULL""",
        (plan["plan_date"], plan["pool"], plan["n_legs"], plan["unit"],
         plan["min_combo"], json.dumps(plan["legs"], ensure_ascii=False),
         json.dumps(plan["bets"], ensure_ascii=False), now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM jc_parlay_plans WHERE plan_date=? AND pool=?",
        (plan["plan_date"], plan["pool"]),
    ).fetchone()
    return row["id"]


def score_plans(conn: sqlite3.Connection, day: str | None = None) -> dict:
    """对未对奖的方案打分。赛果未出的方案会被跳过，下次再对。"""
    where = "scored_at IS NULL"
    params: list = []
    if day:
        where += " AND plan_date = ?"
        params.append(day)

    rows = list(conn.execute(
        f"SELECT id, plan_date, pool, unit, legs_json, bets_json "
        f"FROM jc_parlay_plans WHERE {where}", params))

    now = datetime.now().isoformat(timespec="seconds")
    scored = skipped = 0
    for plan in rows:
        legs = json.loads(plan["legs_json"])
        column = RESULT_COLUMN.get(plan["pool"])
        if not column:
            skipped += 1
            continue

        # 赛果齐全才打分，否则等下一次（比赛可能还没结束）
        pending = False
        for leg in legs:
            row = conn.execute(
                f"SELECT {column} AS res FROM jc_matches WHERE match_id=?",
                (leg["match_id"],)).fetchone()
            raw = row["res"] if row else None
            if not raw:
                pending = True
                break
            normalized = jc_predict.normalize_combination(plan["pool"], raw)
            if normalized is None:
                pending = True
                break
            leg["hit"] = (normalized == leg["pick"])
        if pending:
            skipped += 1
            continue

        odds_by_id = {x["match_id"]: x["odds"] for x in legs}
        hit_by_id = {x["match_id"]: x["hit"] for x in legs}
        bets = json.loads(plan["bets_json"])
        returned = 0.0
        winning = 0
        for bet in bets:
            if all(hit_by_id.get(m) for m in bet["legs"]):
                product = 1.0
                for m in bet["legs"]:
                    product *= odds_by_id[m]
                returned += product * plan["unit"]
                winning += 1

        invested = len(bets) * plan["unit"]
        conn.execute(
            """UPDATE jc_parlay_plans
               SET legs_json=?, invested=?, returned=?, winning_bets=?, scored_at=?
               WHERE id=?""",
            (json.dumps(legs, ensure_ascii=False), invested, round(returned, 2),
             winning, now, plan["id"]),
        )
        scored += 1

    conn.commit()
    return {"scored": scored, "skipped": skipped}


def summary(conn: sqlite3.Connection, pool: str | None = None) -> dict:
    """历史盈亏汇总（按玩法分组 + 总计）。"""
    where = "scored_at IS NOT NULL"
    params: list = []
    if pool:
        where += " AND pool = ?"
        params.append(pool)

    by_pool: dict[str, dict] = {}
    for row in conn.execute(
        f"""SELECT pool, COUNT(*) n, SUM(invested) inv, SUM(returned) ret,
                   SUM(winning_bets) wins
            FROM jc_parlay_plans WHERE {where} GROUP BY pool""", params):
        inv = float(row["inv"] or 0)
        ret = float(row["ret"] or 0)
        by_pool[row["pool"]] = {
            "plans": row["n"],
            "invested": round(inv, 2),
            "returned": round(ret, 2),
            "profit": round(ret - inv, 2),
            "return_rate": (ret / inv) if inv else None,
            "winning_bets": int(row["wins"] or 0),
        }

    inv = sum(v["invested"] for v in by_pool.values())
    ret = sum(v["returned"] for v in by_pool.values())
    return {
        "by_pool": by_pool,
        "total": {
            "plans": sum(v["plans"] for v in by_pool.values()),
            "invested": round(inv, 2),
            "returned": round(ret, 2),
            "profit": round(ret - inv, 2),
            "return_rate": (ret / inv) if inv else None,
        },
    }


def timeline(conn: sqlite3.Connection, limit: int = 90) -> list[dict]:
    """按日期聚合的盈亏序列，供首页曲线图使用（累计 + 单期）。"""
    rows = list(conn.execute(
        """SELECT plan_date,
                  SUM(invested) AS invested,
                  SUM(returned) AS returned,
                  COUNT(*) AS plans
           FROM jc_parlay_plans
           WHERE scored_at IS NOT NULL
           GROUP BY plan_date ORDER BY plan_date DESC LIMIT ?""", (limit,)))
    rows.reverse()                     # 曲线按时间正序

    out = []
    cumulative = 0.0
    for r in rows:
        invested = float(r["invested"] or 0)
        returned = float(r["returned"] or 0)
        profit = returned - invested
        cumulative += profit
        out.append({
            "date": r["plan_date"],
            "plans": r["plans"],
            "invested": round(invested, 2),
            "returned": round(returned, 2),
            "profit": round(profit, 2),
            "cumulative": round(cumulative, 2),
        })
    return out


def daily_status(conn: sqlite3.Connection, day: str,
                 pool_config: dict | None = None) -> list[dict]:
    """当天各玩法的选场情况，供页面解释「今天为什么没有方案」。

    没有这层信息的话，页面只会列出历史方案，用户无从判断「今天没方案」
    是故障、是没比赛、还是比赛都没把握 —— 三种情况含义完全不同。
    """
    config = pool_config or DEFAULT_POOL_CONFIG
    out = []
    for pool, n_legs in config.items():
        why = skip_reason(conn, day, pool, n_legs)
        row = conn.execute(
            "SELECT n_legs FROM jc_parlay_plans WHERE plan_date=? AND pool=?",
            (day, pool)).fetchone()
        out.append({
            "pool": pool,
            "pool_label": jc_predict.pool_label(pool),
            "candidates": why["candidates"],
            "off_sale": why["off_sale"],
            "bettable": why["bettable"],
            "qualified": why["qualified"],
            "min_prob": why["min_prob"],
            "n_legs_max": n_legs,
            "live_known": why["live_known"],
            "planned": row is not None,
            "planned_legs": row["n_legs"] if row else None,
            # 页面直接显示这句，不再各写一套判断（口径漂了就会自相矛盾）
            "note": (f"已出方案（{row['n_legs']} 场）" if row is not None
                     else skip_text(why)),
        })
    return out


def recent_plans(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """最近的方案（含明细），供页面展示。

    附带中文标签（`pool_label` / 每场的 `pick_label`）—— 映射表在 Python 里，
    前端拿不到，必须后端翻译好再给。
    """
    rows = list(conn.execute(
        """SELECT * FROM jc_parlay_plans
           ORDER BY plan_date DESC, pool ASC LIMIT ?""", (limit,)))
    from lottery_lab.collectors import jc_history

    today = date.today().isoformat()
    out = []
    for row in rows:
        pool = row["pool"]
        legs = _translate_legs(conn, pool, json.loads(row["legs_json"]))

        # 只在当天方案上标注在售状态：更早的方案当天已结算，「已停售」是必然的
        # 噪音，标了反而干扰阅读。
        today_live = jc_predict.live_match_ids(conn, row["plan_date"]) \
            if row["plan_date"] == today else None
        if today_live is not None:
            for leg in legs:
                leg["on_sale"] = leg["match_id"] in today_live

        history = []
        for h in conn.execute(
                """SELECT * FROM jc_parlay_plan_history
                   WHERE plan_date=? AND pool=? ORDER BY revision""",
                (row["plan_date"], pool)):
            history.append({
                "revision": h["revision"],
                "n_legs": h["n_legs"],
                "unit": h["unit"],
                "legs": _translate_legs(conn, pool, json.loads(h["legs_json"])),
                "invested": h["invested"],
                "returned": h["returned"],
                "created_at": h["created_at"],
                "archived_at": h["archived_at"],
            })

        out.append({
            "id": row["id"],
            "plan_date": row["plan_date"],
            "pool": pool,
            "pool_label": jc_predict.pool_label(pool),
            "n_legs": row["n_legs"],
            "unit": row["unit"],
            "legs": legs,
            "bets": json.loads(row["bets_json"]),
            "invested": row["invested"],
            "returned": row["returned"],
            "winning_bets": row["winning_bets"],
            "scored": row["scored_at"] is not None,
            "created_at": row["created_at"],
            # 版本号不落列：当前版 = 1 + 历史版本数，省掉一次表结构迁移
            "revision": len(history) + 1,
            "history": history,
        })
    return out


def _translate_legs(conn: sqlite3.Connection, pool: str, legs: list[dict]) -> list[dict]:
    """把腿里的选项代码翻成中文标签（历史版本与当前版走同一套）。

    映射表在 Python 里，前端拿不到，必须后端翻译好再给；让球还要带上让球线，
    不然"主胜"会和比分盘的"1:1"看着自相矛盾。
    """
    from lottery_lab.collectors import jc_history

    for leg in legs:
        line = (jc_history.goal_line_of(conn, leg["match_id"], pool)
                if pool in ("hhad", "crs") else None)
        leg["goal_line"] = line
        label = jc_predict.option_label(pool, leg.get("pick"))
        if line and pool == "hhad":
            label = f"{label}({line})"
        leg["pick_label"] = label
    return legs
