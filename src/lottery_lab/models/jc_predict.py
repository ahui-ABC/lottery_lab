"""竞彩当期预测与对奖。

设计见 `docs/superpowers/specs/2026-09-20-jc-prediction-design.md`。

**核心原则：可拆解，不做黑盒。** 并行记录 5 条独立路径的推荐，让数据淘汰弱路径。
所有先验权重（α）都只是假设 —— 因此按选项的特征向量必须全部落库，
将来才能离线重新拟合而不必重跑采集。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from lottery_lab.models import market

# 玩法 → 该玩法的选项名（与官方赔率字段一致）
POOL_OPTIONS = {
    "had": ["h", "d", "a"],
    "hhad": ["h", "d", "a"],
    "ttg": [f"s{i}" for i in range(8)],                      # s0..s7（7 球及以上归 s7）
    "hafu": ["hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"],
    "crs": None,                                              # 动态：由数据决定
}

METHODS = ("market", "trend", "stable", "cross", "blend")

# 先验权重 —— **刻意偏激进，目的是让各路径产生分歧**，以便用真实战绩对比。
#
# 2026-09-20 用当期 26 场实测校准：
#   概率首二名差距 margin 中位 0.24（很大）
#   归一化漂移 nd        中位 -0.008（赔率只变了 0.8%）
#   波动 vol             中位 0.013，最大 0.074
#
# 当时 α=0.5 的结果是**5 条路径 pick 完全一致**（分歧 0/26）—— 路径在决策层面
# 退化，记录再多天也分不出高下。实测要翻转 argmax 需 α 约 1.4-4.9，故上调到 2.0。
#
# 注意：这**不是**说趋势信号更强了。当前赔率漂移本身极微弱（0.8%），把 α 调大
# 有放大噪音的风险，`trend` 大概率会输给 `market`。但这个对比正是我们要的数据 ——
# 原始信号已按选项落库，将来可离线重新拟合。
ALPHA_TREND = 2.0
ALPHA_VOL = 0.3
ALPHA_CROSS = 0.5
# 同理下调：实测 vol 最大仅 0.074，原阈值 0.15 让 stable 几乎从不触发（等于没有过滤）
VOL_THRESHOLD = 0.04


# ---- 去水 ------------------------------------------------------------------------
def devig_n(odds: list[float]) -> list[float] | None:
    """n 选项去水：3 个走 Shin（`market.shin`），其余走比例法。

    非法输入（空/非数字/非正/选项数 < 2）返回 None。
    """
    if not odds or len(odds) < 2:
        return None
    try:
        values = [float(x) for x in odds]
    except (TypeError, ValueError):
        return None
    if any((v is None or v <= 0) for v in values):
        return None
    if len(values) == 3:
        try:
            return market.shin(values)
        except Exception:
            return None
    total = sum(1.0 / v for v in values)
    if total <= 0:
        return None
    return [(1.0 / v) / total for v in values]


# ---- 选项展示名（赔率字段名 → 中文） ------------------------------------------------
_TTG_LABELS = {f"s{i}": f"{i} 球" for i in range(7)}
_TTG_LABELS["s7"] = "7 球及以上"

_HAFU_LABELS = {
    "hh": "胜胜", "hd": "胜平", "ha": "胜负",
    "dh": "平胜", "dd": "平平", "da": "平负",
    "ah": "负胜", "ad": "负平", "aa": "负负",
}


def option_label(pool: str, option: str | None) -> str:
    """把赔率字段名翻译成中文，供界面展示。

    - had/hhad：h/d/a → 主胜/平/客胜（hhad 为**让球后**的胜负）
    - crs：s01s00 → 1:0；s-1sh/sd/sa → 主胜/平/客胜(其他比分)
    - ttg：s0..s7 → 0 球..7 球及以上
    - hafu：hh → 胜胜（半场结果 + 全场结果）
    """
    if not option:
        return "—"
    text = str(option)

    if pool in ("had", "hhad"):
        return {"h": "主胜", "d": "平", "a": "客胜"}.get(text, text)

    if pool == "ttg":
        return _TTG_LABELS.get(text, text)

    if pool == "hafu":
        return _HAFU_LABELS.get(text, text)

    if pool == "crs":
        if text == "s-1sh":
            return "主胜(其他比分)"
        if text == "s-1sd":
            return "平(其他比分)"
        if text == "s-1sa":
            return "客胜(其他比分)"
        # s01s00 → 1:0
        if text.startswith("s") and "s" in text[1:]:
            body = text[1:]
            head, _, tail = body.partition("s")
            if head.isdigit() and tail.isdigit():
                return f"{int(head)}:{int(tail)}"
        return text

    return text


def pool_label(pool: str) -> str:
    """玩法名 → 中文。"""
    return {
        "had": "胜平负", "hhad": "让球胜平负", "crs": "比分",
        "ttg": "总进球", "hafu": "半全场",
    }.get(pool, pool)


# ---- 开奖结果归一化 ----------------------------------------------------------------
def normalize_combination(pool: str, combination: str) -> str | None:
    """把 matchResultList 的 combination 映射为赔率字段名；无法识别返回 None。

    两套编码不同：CRS 是 "2:0"（赔率字段 `s02s00`）、HAFU 是 "H:H"（`hh`）、
    TTG 是 "2"（`s2`）。无法识别时**返回 None 由调用方跳过，不猜测**。
    """
    if combination is None:
        return None
    text = str(combination).strip()
    if not text:
        return None

    if pool in ("had", "hhad"):
        code = text.upper()
        return {"H": "h", "D": "d", "A": "a"}.get(code)

    if pool == "ttg":
        if not text.isdigit():
            return None
        n = int(text)
        return f"s{min(n, 7)}"          # 7 球及以上归 s7

    if pool == "hafu":
        # "H:H" → hh
        parts = text.upper().split(":")
        if len(parts) != 2 or any(p not in ("H", "D", "A") for p in parts):
            return None
        return (parts[0] + parts[1]).lower()

    if pool == "crs":
        # 非具体比分的三个桶：官方用 -1 表示
        if text.startswith("-1"):
            suffix = text[-1].upper() if text else ""
            return {"H": "s-1sh", "D": "s-1sd", "A": "s-1sa"}.get(suffix)
        parts = text.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        home, away = int(parts[0]), int(parts[1])
        if home > 5 or away > 5:
            # 超出逐比分范围的归入「其他比分」桶
            return "s-1sh" if home > away else ("s-1sd" if home == away else "s-1sa")
        return f"s{home:02d}s{away:02d}"

    return None


# ---- 信号计算 --------------------------------------------------------------------
def _option_vector(entry: dict) -> dict[str, float]:
    """从一条赔率快照里取出「选项 → 赔率」的映射（排除元数据与涨跌标志）。

    涨跌标志字段以 `f` 结尾（hf/df/af/hhf...），需要排除；但选项名本身可能
    以 f 结尾的概率极低，且官方选项名固定，故用白名单更稳。
    """
    out: dict[str, float] = {}
    for key, value in entry.items():
        if key in ("updateDate", "updateTime", "goalLine"):
            continue
        if key.endswith("f"):
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _signals(series: list[dict]) -> dict:
    """赔率序列 → 按选项的信号向量。

    series 按时间升序；series[0] 为初盘，series[-1] 为最新。
    """
    if not series:
        return {}
    first = _option_vector(series[0])
    latest = _option_vector(series[-1])
    options = [k for k in latest if k in first and latest[k] > 0]
    if len(options) < 2:
        return {}

    prob = devig_n([latest[k] for k in options])
    if prob is None:
        return {}
    prob_by = dict(zip(options, prob))

    drift = {k: latest[k] - first[k] for k in options}
    nd = {k: -(drift[k]) / first[k] if first[k] else 0.0 for k in options}

    vol = {}
    for k in options:
        values = []
        for entry in series:
            vec = _option_vector(entry)
            if k in vec and vec[k] > 0:
                values.append(vec[k])
        if len(values) >= 2:
            mean = sum(values) / len(values)
            var = sum((v - mean) ** 2 for v in values) / len(values)
            vol[k] = (var ** 0.5) / mean if mean else 0.0
        else:
            vol[k] = 0.0

    return {"options": options, "first": first, "latest": latest,
            "prob": prob_by, "drift": drift, "nd": nd, "vol": vol,
            "change_count": len(series)}


# ---- 跨玩法推导（cross 用） ---------------------------------------------------------
def _derive_1x2(signals_by_pool: dict) -> dict[str, float] | None:
    """用 hafu 与 crs 独立推导「未让球」的全场胜平负概率（等权平均）。"""
    sources = []

    hafu = signals_by_pool.get("hafu")
    if hafu and hafu.get("prob"):
        p = hafu["prob"]
        try:
            sources.append({
                "h": p["hh"] + p["dh"] + p["ah"],
                "d": p["hd"] + p["dd"] + p["ad"],
                "a": p["ha"] + p["da"] + p["aa"],
            })
        except KeyError:
            pass

    crs = signals_by_pool.get("crs")
    if crs and crs.get("prob"):
        home = draw = away = 0.0
        for key, value in crs["prob"].items():
            if key == "s-1sh":
                home += value
            elif key == "s-1sd":
                draw += value
            elif key == "s-1sa":
                away += value
            elif key.startswith("s") and "s" in key[1:]:
                try:
                    h, a = key[1:].split("s")
                    h, a = int(h), int(a)
                except ValueError:
                    continue
                if h > a:
                    home += value
                elif h == a:
                    draw += value
                else:
                    away += value
        sources.append({"h": home, "d": draw, "a": away})

    if not sources:
        return None

    merged = {k: sum(s[k] for s in sources) / len(sources) for k in ("h", "d", "a")}
    total = sum(merged.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in merged.items()}


# ---- 五条路径 --------------------------------------------------------------------
def analyze_match(signals_by_pool: dict[str, dict]) -> list[dict]:
    """对一场比赛的各玩法产出各路径推荐。

    signals_by_pool: {pool: _signals(...) 的结果}
    返回 [{pool, method, pick, odds, prob, ...特征}]（pick 可能为 None）。
    """
    derived = _derive_1x2(signals_by_pool)
    out: list[dict] = []

    for pool, sig in signals_by_pool.items():
        if not sig:
            continue
        options = sig["options"]
        prob, latest, first = sig["prob"], sig["latest"], sig["first"]
        nd, vol = sig["nd"], sig["vol"]

        def _row(method: str, pick: str | None, score: float | None,
                 extra: dict | None = None) -> dict:
            return {
                "pool": pool, "method": method, "pick": pick,
                "odds": latest.get(pick) if pick else None,
                "prob": score,
                "prob_json": json.dumps(prob, ensure_ascii=False),
                "first_odds_json": json.dumps(first, ensure_ascii=False),
                "latest_odds_json": json.dumps(latest, ensure_ascii=False),
                "drift_json": json.dumps(sig["drift"], ensure_ascii=False),
                "vol_json": json.dumps(vol, ensure_ascii=False),
                "change_count": sig["change_count"],
                "provenance_json": json.dumps(extra, ensure_ascii=False) if extra else None,
            }

        # market —— 基线
        best = max(options, key=lambda k: prob[k])
        out.append(_row("market", best, prob[best]))

        # trend —— 漂移加成（按选项）
        trend_score = {k: prob[k] * (1 + ALPHA_TREND * nd[k]) for k in options}
        best_t = max(options, key=lambda k: trend_score[k])
        out.append(_row("trend", best_t, trend_score[best_t]))

        # stable —— 波动过滤（任一选项波动过大就不出手）
        if max(vol[k] for k in options) > VOL_THRESHOLD:
            out.append(_row("stable", None, None))
        else:
            out.append(_row("stable", best, prob[best]))

        # cross 的印证强度（仅 had 可从其它玩法独立推导）
        support = None
        derived_extra = None
        if pool == "had" and derived:
            top = max(derived.values())
            support = {k: derived.get(k, 0.0) / top for k in options}
            derived_extra = {"derived": derived}

        # blend —— 趋势 × 波动 × 印证，**三项都按选项**。
        # 注意：blend 必须包含 cross 项，否则趋势加成会淹没波动惩罚
        # （实测 α_trend 放大到 2.0 后，纯 trend×vol 的 blend 与 trend 完全等价）。
        # 这是刻意的：blend 是唯一的"全信号综合"路径，与单信号路径形成对照。
        blend_score = {}
        for k in options:
            s = prob[k] * (1 + ALPHA_TREND * nd[k]) * (1 - ALPHA_VOL * min(vol[k], 1.0))
            if support:
                s *= (1 + ALPHA_CROSS * support[k])
            blend_score[k] = s
        best_b = max(options, key=lambda k: blend_score[k])
        out.append(_row("blend", best_b, blend_score[best_b], extra=derived_extra))

        # cross —— 仅 had（hhad 是让球后的方向，与未让球的推导语义不匹配）
        if support:
            cross_score = {k: prob[k] * (1 + ALPHA_CROSS * support[k]) for k in options}
            best_c = max(options, key=lambda k: cross_score[k])
            out.append(_row("cross", best_c, cross_score[best_c],
                            extra=derived_extra))

    return out


# ---- 入库 ------------------------------------------------------------------------
def _write(conn: sqlite3.Connection, match_id: int, predicted_on: str,
           rows: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        conn.execute(
            """INSERT INTO jc_predictions(
                   match_id, predicted_on, pool, method, pick, odds, prob,
                   prob_json, first_odds_json, latest_odds_json,
                   drift_json, vol_json, change_count, provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(match_id, pool, predicted_on, method) DO UPDATE SET
                   pick=excluded.pick, odds=excluded.odds, prob=excluded.prob,
                   prob_json=excluded.prob_json, first_odds_json=excluded.first_odds_json,
                   latest_odds_json=excluded.latest_odds_json,
                   drift_json=excluded.drift_json, vol_json=excluded.vol_json,
                   change_count=excluded.change_count,
                   provenance_json=excluded.provenance_json,
                   result=NULL, hit=NULL, scored_at=NULL""",
            (match_id, predicted_on, row["pool"], row["method"], row["pick"],
             row["odds"], row["prob"], row["prob_json"], row["first_odds_json"],
             row["latest_odds_json"], row["drift_json"], row["vol_json"],
             row["change_count"], row["provenance_json"]),
        )
    conn.commit()
    return len(rows)


def predict_match(conn: sqlite3.Connection, match_id: int,
                  odds_by_pool: dict[str, list[dict]], predicted_on: str) -> int:
    """对一场比赛预测并存档。odds_by_pool 是各玩法的赔率变化序列。"""
    signals = {}
    for pool, series in odds_by_pool.items():
        sig = _signals(series)
        if sig:
            signals[pool] = sig
    if not signals:
        return 0
    return _write(conn, match_id, predicted_on, analyze_match(signals))


# ---- 赔率序列的来源 ----------------------------------------------------------------
def odds_by_pool_from_value(value: dict) -> dict[str, list[dict]]:
    """`getFixedBonusV1` 的 value → {pool: [赔率快照]}（按时间升序）。

    原始 entry 可直接交给 `_signals`（`_option_vector` 会过滤 updateDate 等元数据）。
    """
    from lottery_lab.collectors import jc_history

    history = value.get("oddsHistory") or {}
    out: dict[str, list[dict]] = {}
    for pool, key in jc_history.POOL_LIST_KEY.items():
        series = history.get(key) or []
        if series:
            out[pool] = list(series)
    return out


def load_odds_by_pool(conn: sqlite3.Connection, match_id: int) -> dict[str, list[dict]]:
    """从 `jc_odds_history` 读某场各玩法的赔率序列（重放模式，不联网）。"""
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        """SELECT pool, odds_json, update_date, update_time, goal_line
           FROM jc_odds_history WHERE match_id=? ORDER BY pool, seq""",
        (match_id,),
    ):
        try:
            options = json.loads(row["odds_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(options, dict):
            continue
        entry = dict(options)
        entry["updateDate"] = row["update_date"]
        entry["updateTime"] = row["update_time"]
        entry["goalLine"] = row["goal_line"]
        out.setdefault(row["pool"], []).append(entry)
    return out


def match_ids_on(conn: sqlite3.Connection, day: str) -> list[int]:
    """该日已有赔率数据的比赛（重放用）。"""
    return [r["match_id"] for r in conn.execute(
        """SELECT DISTINCT h.match_id FROM jc_odds_history h
           JOIN jc_matches m ON m.match_id = h.match_id
           WHERE m.match_date = ? ORDER BY h.match_id""",
        (day,),
    )]


# ---- 对奖 ------------------------------------------------------------------------
RESULT_COLUMN = {
    "had": "result_had", "hhad": "result_hhad",
    "crs": "result_crs", "ttg": "result_ttg", "hafu": "result_hafu",
}


def score_pending(conn: sqlite3.Connection, predicted_on: str | None = None,
                  pending: dict[int, dict] | None = None) -> dict:
    """对未对奖的预测打分。

    `pending` 预先提供 {match_id: {result_column: combination}}（用于离线测试或
    调用方自行拉取）；缺省则从 `jc_matches` 读已存档的赛果。
    """
    where = "p.scored_at IS NULL"
    params: list = []
    if predicted_on:
        where += " AND p.predicted_on=?"
        params.append(predicted_on)

    rows = list(conn.execute(
        f"""SELECT p.id, p.match_id, p.pool, p.pick, m.result_had, m.result_hhad,
                   m.result_crs, m.result_ttg, m.result_hafu
            FROM jc_predictions p
            LEFT JOIN jc_matches m ON m.match_id = p.match_id
            WHERE {where}""",
        params,
    ))

    now = datetime.now().isoformat(timespec="seconds")
    scored = skipped = 0
    for row in rows:
        # 无推荐（stable 过滤）也要收尾，否则每次都会被重新捞出来
        if not row["pick"]:
            conn.execute(
                "UPDATE jc_predictions SET scored_at=?, hit=NULL WHERE id=?",
                (now, row["id"]),
            )
            scored += 1
            continue

        column = RESULT_COLUMN.get(row["pool"])
        if not column:
            skipped += 1
            continue
        raw = (pending or {}).get(row["match_id"], {}).get(column) \
            if pending is not None else row[column]
        if not raw:
            skipped += 1              # 尚未出结果 —— 下次再对
            continue

        normalized = normalize_combination(row["pool"], raw)
        if normalized is None:
            skipped += 1              # 未知编码：不猜
            continue

        conn.execute(
            "UPDATE jc_predictions SET result=?, hit=?, scored_at=? WHERE id=?",
            (raw, 1 if normalized == row["pick"] else 0, now, row["id"]),
        )
        scored += 1

    conn.commit()
    return {"scored": scored, "skipped": skipped}


def summary_by_method(conn: sqlite3.Connection) -> list[dict]:
    """各路径的命中率 —— 这是整个设计的目的：用数据淘汰弱路径。"""
    rows = conn.execute(
        """SELECT method, COUNT(*) total,
                  SUM(CASE WHEN hit = 1 THEN 1 ELSE 0 END) hits,
                  AVG(odds) avg_odds
           FROM jc_predictions
           WHERE scored_at IS NOT NULL AND hit IS NOT NULL
           GROUP BY method ORDER BY method"""
    ).fetchall()
    out = []
    for row in rows:
        total = row["total"] or 0
        hits = row["hits"] or 0
        out.append({
            "method": row["method"],
            "picks": total,
            "hits": hits,
            "hit_rate": (hits / total) if total else None,
            "avg_odds": row["avg_odds"],
        })
    return out
