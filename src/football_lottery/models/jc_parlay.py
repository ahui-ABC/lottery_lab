"""竞彩串关方案：按回测结论选场 + 多重串关组合。

## 设计依据（全部来自 `jc_backtest` 的 1731 期实测）

1. **选场规则 = 市场概率最高**。三玩法一致优于"博冷"（选高赔率）：
   让球 75.3% vs 69.0%、比分 75.1% vs 67.6%、半全场 64.6% vs 60.6%。
2. **多重串关组合**（同时下 k=2..N 串1）远优于单押 N串1：
   比分 4 场从理论 22.1% 提升到实测 75.1% —— 把高抽水的长串拆成低抽水的短串。
3. **仍是负期望**（返还 64.6%~75.3%），本质是抽水决定的。
   本模块产出的是"在被验证为最优的规则下该选哪几场"，不是盈利保证。

## 默认配置

比分 4 场 / 半全场 4 场 / 让球胜平负 5 场（用户实际玩法）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from itertools import combinations

from football_lottery.models import jc_predict

RESULT_COLUMN = {
    "had": "result_had", "hhad": "result_hhad",
    "crs": "result_crs", "ttg": "result_ttg", "hafu": "result_hafu",
}

# 玩法 → 选几场（回测验证过的用户实际玩法）
DEFAULT_POOL_CONFIG = {
    "hhad": 5,
    "crs": 4,
    "hafu": 4,
}
DEFAULT_UNIT = 2.0          # 每注金额（元）
DEFAULT_MIN_COMBO = 2       # 最小串关数


def pick_legs(conn: sqlite3.Connection, day: str, pool: str,
              n_legs: int) -> list[dict]:
    """按「市场概率最高」为该玩法选出 n_legs 场。

    直接复用 `jc_predictions` 里 market 路径的结果 —— 选场规则已由回测确定，
    这里不需要重算信号。
    """
    rows = list(conn.execute(
        """SELECT p.match_id, p.pick, p.odds, p.prob,
                  m.home_team, m.away_team, m.league_name
           FROM jc_predictions p
           JOIN jc_matches m ON m.match_id = p.match_id
           WHERE p.predicted_on = ? AND p.pool = ? AND p.method = 'market'
             AND p.pick IS NOT NULL AND p.odds IS NOT NULL
           ORDER BY p.prob DESC
           LIMIT ?""",
        (day, pool, n_legs),
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
               min_combo: int = DEFAULT_MIN_COMBO) -> dict | None:
    """生成一个玩法的方案；可用场次不足时返回 None。"""
    legs = pick_legs(conn, day, pool, n_legs)
    if len(legs) < n_legs:
        return None
    return {
        "plan_date": day,
        "pool": pool,
        "n_legs": n_legs,
        "unit": unit,
        "min_combo": min_combo,
        "legs": legs,
        "bets": build_bets(legs, unit, min_combo),
    }


def save_plan(conn: sqlite3.Connection, plan: dict) -> int:
    """入库（同一天同玩法重跑会覆盖，因为赔率可能已变）。"""
    now = datetime.now().isoformat(timespec="seconds")
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


def recent_plans(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """最近的方案（含明细），供页面展示。

    附带中文标签（`pool_label` / 每场的 `pick_label`）—— 映射表在 Python 里，
    前端拿不到，必须后端翻译好再给。
    """
    rows = list(conn.execute(
        """SELECT * FROM jc_parlay_plans
           ORDER BY plan_date DESC, pool ASC LIMIT ?""", (limit,)))
    from football_lottery.collectors import jc_history

    out = []
    for row in rows:
        pool = row["pool"]
        legs = json.loads(row["legs_json"])
        for leg in legs:
            # 让球玩法必须带让球线：不然"主胜"看着会和比分盘的"1:1"自相矛盾
            # （让球 +1 的"主胜"= 主队平或赢，平局本就在其中）
            line = jc_history.goal_line_of(conn, leg["match_id"], pool) if pool in ("hhad", "crs") else None
            leg["goal_line"] = line
            label = jc_predict.option_label(pool, leg.get("pick"))
            if line and pool == "hhad":
                label = f"{label}({line})"
            leg["pick_label"] = label
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
        })
    return out
