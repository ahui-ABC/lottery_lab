"""FastAPI 应用与路由（实现 T25-T28 / 设计 §7）。

四个页面：
- /           重定向到 /predict
- /predict    本期预测：取最近一个 historical period，对每场分别跑 market/devig → 三概率
- /plan       推荐方案：HTML 表单 + 调用 optimizer 求解
- /history    历史战绩：从 DB 汇总（仅在有 fixture + winnings 时有真实数据）
- /backtest   回测报告：读 data/reports/*.json + calibration.csv

API：
- GET  /api/predict           概率明细
- POST /api/plan              求解方案 {game_type, objective, budget, probs:[14 × 3]}
- GET  /api/history           战绩
- GET  /api/backtest-report   回测报告
- GET  /api/health            体检
- GET  /api/period/current    当期信息（若存在）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from football_lottery.db import store
from football_lottery.models import pipeline
from football_lottery.collectors import fixture_match, team_alias
from football_lottery.optimizer import solver
from football_lottery.optimizer import expand as exp


app = FastAPI(title="足彩与数字彩分析", docs_url="/docs")

# 胜负彩 3/1/0 是主队视角：3=主胜、1=平、0=主负（客胜）
OUTCOME_LABELS = {"3": "胜", "1": "平", "0": "负"}

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _pct(value, digits: int = 1) -> str:
    """模板里的比率格式化：0.753 → '75.3%'，None → '—'。"""
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _money(value, digits: int = 0) -> str:
    """金额：负号写在 ¥ 外面（-¥2，不是 ¥-2），None → '—'。

    默认不带小数 —— 用它的地方都是累计额，两位小数纯属噪音。
    """
    if value is None:
        return "—"
    try:
        amount = abs(float(value))
    except (TypeError, ValueError):
        return "—"
    return ("-¥" if float(value) < 0 else "¥") + f"{amount:.{digits}f}"


templates.env.filters["pct"] = _pct
templates.env.filters["money"] = _money


def _get_conn():
    """每次请求 new conn（SQLite 本地适合）。"""
    import yaml
    config_path = Path("config.yaml")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    cfg = cfg or {}
    db_path = cfg.get("db_path", "data/football.db")
    conn = store.connect(db_path)
    store.init_db(conn)
    return conn


# ---------------- 页面 ----------------
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """概览首页：各模块摘要 + 快捷入口。

    此前 `/` 直接 302 到 `/predict`，导致导航左侧的品牌与「本期预测」指向
    同一页、看着像重复。改为真正的概览页。
    """
    from football_lottery import daemon_ctl
    from football_lottery.models import jc_parlay, lottery_track

    conn = _get_conn()
    period = _period_with_matches(conn)

    today = date.today().isoformat()
    jc = {
        "today": today,
        "predictions": conn.execute(
            "SELECT COUNT(DISTINCT match_id) FROM jc_predictions WHERE predicted_on=?",
            (today,)).fetchone()[0],
        "plans_pending": conn.execute(
            "SELECT COUNT(*) FROM jc_parlay_plans WHERE scored_at IS NULL").fetchone()[0],
        "plans_scored": conn.execute(
            "SELECT COUNT(*) FROM jc_parlay_plans WHERE scored_at IS NOT NULL").fetchone()[0],
        "plans_today": conn.execute(
            "SELECT COUNT(*) FROM jc_parlay_plans WHERE plan_date=?", (today,)).fetchone()[0],
    }
    running, _ = daemon_ctl.is_running()
    last = conn.execute(
        "SELECT MAX(captured_at) FROM odds_snapshots").fetchone()[0]

    return templates.TemplateResponse(request, "overview.html", {
        "period": period,
        "jc": jc,
        "summary": jc_parlay.summary(conn),
        "timeline": jc_parlay.timeline(conn, limit=60),
        "lottery": lottery_track.overview(conn),
        "daemon_running": running,
        "last_snapshot": last,
    })


@app.get("/predict", response_class=HTMLResponse)
def page_predict(request: Request):
    return templates.TemplateResponse(request, "predict.html", {})


@app.get("/plan", response_class=HTMLResponse)
def page_plan(request: Request):
    return templates.TemplateResponse(request, "plan.html", {})


@app.get("/history", response_class=HTMLResponse)
def page_history(request: Request):
    return templates.TemplateResponse(request, "history.html", {})


@app.get("/backtest", response_class=HTMLResponse)
def page_backtest(request: Request):
    return templates.TemplateResponse(request, "backtest.html", {})


@app.get("/collect", response_class=HTMLResponse)
def page_collect(request: Request):
    """采集页。任务清单在服务端按分组渲染好，JS 只负责填状态 —— 不在前端再抄一份。"""
    from football_lottery import jobs

    return templates.TemplateResponse(request, "collect.html",
                                      {"job_groups": jobs.grouped()})


@app.get("/jc", response_class=HTMLResponse)
def page_jc(request: Request):
    return templates.TemplateResponse(request, "jc.html", {})


def _frequency_groups(history: list[dict], spec: dict, window: int = 100) -> list[dict]:
    """近 window 期的出现次数与遗漏期数，供页面画热力格。"""
    from collections import Counter

    recent = history[-window:]

    def _cells(universe, values_per_draw):
        counts = Counter(v for values in values_per_draw for v in values)
        gap, seen = {}, set()
        for age, values in enumerate(reversed(values_per_draw)):
            for v in values:
                if v not in seen:
                    gap[v] = age
                    seen.add(v)
        return [{"value": v, "count": counts.get(v, 0),
                 "gap": gap.get(v, len(recent))} for v in universe]

    if spec["kind"] == "two_zone":
        return [
            {"zone": "前区", "cells": _cells(
                [f"{i:02d}" for i in range(1, spec["front_max"] + 1)],
                [d["numbers"]["front"] for d in recent])},
            {"zone": "后区", "cells": _cells(
                [f"{i:02d}" for i in range(1, spec["back_max"] + 1)],
                [d["numbers"]["back"] for d in recent])},
        ]
    return [
        {"zone": f"第{pos + 1}位", "cells": _cells(
            [str(i) for i in range(10)],
            [[d["numbers"]["digits"][pos]] for d in recent])}
        for pos in range(spec["digits"])
    ]


def _lottery_index(conn) -> dict:
    """数字彩总览：五类彩种各一张卡。"""
    from football_lottery.models import lottery_track as ltk

    return {"lotteries": ltk.overview(conn)}


def _lottery_detail(conn, code: str) -> dict:
    """单个彩种的全部页面数据。"""
    from football_lottery.collectors.lottery_history import LOTTERIES
    from football_lottery.models import lottery_predict as lp
    from football_lottery.models import lottery_track as ltk

    spec = LOTTERIES[code]
    detail = {"code": code, "name": spec["name"], "spec": spec, "latest": None,
              "target": None, "predictions": [], "freq": [], "backtest": [],
              "track": {"strategies": [], "pending": 0}, "timelines": {},
              "recent": []}
    last = store.fetchone(conn, """
        SELECT issue, draw_date, numbers FROM lottery_draw
        WHERE lottery=? ORDER BY issue DESC LIMIT 1""", (code,))
    if last is None:
        return detail

    history = lp.load_history(conn, code)
    target = lp.next_issue(last["issue"])
    preds = store.fetchall(conn, """
        SELECT strategy, bets FROM lottery_prediction
        WHERE lottery=? AND target_issue=? ORDER BY strategy""", (code, target))
    tests = store.fetchall(conn, """
        SELECT strategy, metrics, paired FROM lottery_backtest
        WHERE lottery=? ORDER BY strategy""", (code,))
    track = ltk.summary(conn, code)
    detail.update({
        "latest": {"issue": last["issue"], "draw_date": last["draw_date"],
                   "numbers": json.loads(last["numbers"])},
        "target": target,
        "predictions": [{"strategy": r["strategy"],
                         "label": lp.STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
                         "bets": json.loads(r["bets"])} for r in preds],
        "freq": _frequency_groups(history, spec),
        "backtest": [{"strategy": r["strategy"],
                      "label": lp.STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
                      "metrics": json.loads(r["metrics"]) if r["metrics"] else {},
                      "paired": json.loads(r["paired"]) if r["paired"] else None}
                     for r in tests],
        "track": track,
        # 五条策略各一条曲线，前端切换；数据量很小，一次给全免得来回请求
        "timelines": {s["strategy"]: ltk.timeline(conn, code, s["strategy"])
                      for s in track["strategies"]},
        "recent": ltk.recent(conn, code, limit=12),
    })
    return detail


@app.get("/lottery", response_class=HTMLResponse)
def page_lottery(request: Request):
    conn = _get_conn()
    return templates.TemplateResponse(request, "lottery_index.html",
                                      _lottery_index(conn))


@app.get("/lottery/{code}", response_class=HTMLResponse)
def page_lottery_detail(request: Request, code: str):
    from football_lottery.collectors.lottery_history import LOTTERIES

    # 未知彩种要明确 404，不能静默回落到第一个 —— 那会让人以为在看大乐透
    if code not in LOTTERIES:
        raise HTTPException(status_code=404, detail=f"未知彩种：{code}")
    conn = _get_conn()
    return templates.TemplateResponse(request, "lottery.html",
                                      _lottery_detail(conn, code))


@app.get("/api/jc/plans")
def api_jc_plans(limit: int = 12):
    """最近的串关方案（含选场明细与组合数）。"""
    from football_lottery.models import jc_parlay

    conn = _get_conn()
    return {"plans": jc_parlay.recent_plans(conn, limit=limit)}


@app.get("/api/jc/summary")
def api_jc_summary():
    """串关方案的历史盈亏汇总 + 逐期盈亏序列（供曲线图）。"""
    from football_lottery.models import jc_parlay

    conn = _get_conn()
    out = jc_parlay.summary(conn)
    out["timeline"] = jc_parlay.timeline(conn)
    return out


@app.get("/api/daemon/status")
def api_daemon_status():
    """采集守护进程状态 + 快照概况。"""
    from football_lottery import daemon_ctl

    state = daemon_ctl.status()
    state["snapshots"] = daemon_ctl.snapshot_summary(_get_conn())
    return state


@app.post("/api/daemon/start")
def api_daemon_start():
    """启动采集守护进程（已在运行时直接返回现状）。"""
    from football_lottery import daemon_ctl

    return daemon_ctl.start()


@app.post("/api/daemon/stop")
def api_daemon_stop():
    """停止采集守护进程。"""
    from football_lottery import daemon_ctl

    return daemon_ctl.stop()


# ---------------- 一次性采集任务 ----------------
# 与上面的守护进程不同：那些是跑完就结束的动作，一次只允许跑一个
# （见 jobs 模块顶部：并发抓取曾经把整个 IP 被 WAF 封过）。

@app.get("/api/jobs")
def api_jobs():
    """全部任务的状态。"""
    from football_lottery import jobs

    return jobs.status()


@app.post("/api/jobs/{key}/start")
def api_job_start(key: str):
    """启动一个任务。失败原因（未知 key / 已有任务在跑）放在 message 里，HTTP 仍 200。"""
    from football_lottery import jobs

    return jobs.start(key)


@app.post("/api/jobs/{key}/stop")
def api_job_stop(key: str):
    """停止正在跑的任务。"""
    from football_lottery import jobs

    return jobs.stop(key)


# ---------------- API ----------------
@app.get("/api/health")
def api_health():
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    return {"matches": n, "ok": True}


def _is_on_sale(sale_end: str | None, now: datetime | None = None) -> bool:
    """sale_end 为 'YYYY-MM-DD HH:MM:SS'；空值或解析失败视为不在售（保守）。"""
    if not sale_end:
        return False
    now = now or datetime.now()
    try:
        end = datetime.fromisoformat(sale_end.replace("/", "-").strip())
    except ValueError:
        return False
    return end >= now


def _pick_period(conn):
    """挑期次：优先真正在售的 current；否则最新的 historical。

    返回 (row, is_current)。此前只按 status 取"最新一条"，2026-05 的样本
    期次 26080 因此被长期当作当期展示 —— 根因就是缺这个时间判定。
    """
    rows = list(conn.execute(
        """SELECT id, period_no, status, sale_end, draw_date FROM periods
           ORDER BY COALESCE(draw_date, '') DESC, id DESC"""
    ))
    for row in rows:
        if row["status"] == "current" and _is_on_sale(row["sale_end"]):
            return row, True
    for row in rows:
        if row["status"] != "current":
            return row, False
    return None, False


def _period_with_matches(conn) -> dict | None:
    """当期（或退化的最新历史期）+ 14 场对阵，并透传期次身份。"""
    p, is_current = _pick_period(conn)
    if p is None:
        return None

    pm = list(conn.execute(
        """SELECT id, seq, home_name_cn, away_name_cn, match_id, match_time,
                  odds_json, league_cn
           FROM period_matches WHERE period_id=?
           ORDER BY seq""",
        (p["id"],),
    ))
    if len(pm) < 14:
        return None

    enriched = []
    for row in pm[:14]:
        d = {"id": row["id"], "seq": row["seq"],
             "home_cn": row["home_name_cn"], "away_cn": row["away_name_cn"],
             "match_time": row["match_time"], "match_id": row["match_id"],
             "league_cn": row["league_cn"]}
        try:
            d["odds"] = json.loads(row["odds_json"] or "{}")
        except Exception:
            d["odds"] = {}
        if row["match_id"] and not d["odds"]:
            m = conn.execute(
                """SELECT odds_json FROM matches WHERE id=?""", (row["match_id"],)
            ).fetchone()
            if m:
                try:
                    d["odds"] = json.loads(m["odds_json"] or "{}")
                except Exception:
                    d["odds"] = {}
        d["mapped"] = bool(row["match_id"])
        enriched.append(d)

    unmapped = [
        {"seq": m["seq"], "side": side, "name_cn": m[f"{side}_cn"]}
        for m in enriched if not m["mapped"]
        for side in ("home", "away")
    ]
    warnings = []
    if not is_current:
        warnings.append({
            "type": "stale_period",
            "message": f"当前显示的是历史期次 {p['period_no']}，非在售期次",
        })
    if unmapped:
        warnings.append({
            "type": "unmapped_fixture",
            "count": len(unmapped),
            "message": "部分场次尚未匹配到历史比赛，请确认球队别名后重试",
        })
    return {
        "period_id": p["id"], "period_no": p["period_no"],
        "period_status": p["status"], "sale_end": p["sale_end"],
        "is_current": bool(is_current),
        "matches": enriched,
        "unmapped": unmapped, "warnings": warnings,
    }


@app.get("/api/period/current")
def api_period_current():
    conn = _get_conn()
    p = _period_with_matches(conn)
    if not p:
        return JSONResponse(
            {"warning": "数据库内尚无对齐的期次；请先 import-fixtures + map-fixtures",
             "period_no": None, "matches": []},
            status_code=200,
        )
    return p


@app.get("/api/predict")
def api_predict():
    """对当前期给出市场、DC、GBDT（若已训练）与融合概率。"""
    conn = _get_conn()
    period = _period_with_matches(conn)
    if not period:
        return {"warning": "无当期数据", "matches": []}
    import yaml
    cfg_path = Path("config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg = cfg or {}
    result = pipeline.period_predictions(
        conn, period["period_id"], models_dir=cfg.get("models_dir", "data/models"),
        fusion_mode=(cfg.get("fusion") or {}).get("mode", "full"),
    )
    by_seq = {row["seq"]: row for row in result["matches"]}
    matches = []
    for m in period["matches"]:
        row = by_seq.get(m["seq"], {})
        market, dc, gbdt, fused = (row.get("market"), row.get("dc"),
                                   row.get("gbdt"), row.get("fused"))
        # 无映射且三个组件全空时，Fusion 会回退成均匀分布 [1/3,1/3,1/3]，
        # 直接展示会变成"假 33.3%"。此处抑制，交由前端显示"暂无预测"。
        has_prediction = bool(m["mapped"] or market or dc or gbdt)
        if not has_prediction:
            fused = None
        matches.append({
            "seq": m["seq"],
            "home": m["home_cn"], "away": m["away_cn"],
            "league_cn": m["league_cn"],
            "match_time": m["match_time"],
            "match_id": m["match_id"], "mapped": m["mapped"],
            "has_prediction": has_prediction,
            "market": market, "dc": dc, "gbdt": gbdt, "fused": fused,
        })
    return {"period_id": period["period_id"], "period_no": period["period_no"],
            "model_version": result["model_version"],
            "period_status": period["period_status"],
            "sale_end": period["sale_end"],
            "is_current": period["is_current"],
            "warnings": period["warnings"],
            "unmapped": period["unmapped"],
            "matches": matches}


class PlanReq(BaseModel):
    game_type: str            # "sfc14" | "r9"
    objective: str = "first_second"   # "first" | "first_second"
    budget: int = 64
    probs: list[list[float]]  # 14 / 9 × [ph, pd, pa]
    period_id: int | None = None
    period_no: str | None = None


class AliasReq(BaseModel):
    name_cn: str
    name_en: str


@app.post("/api/alias/confirm")
def api_alias_confirm(req: AliasReq):
    conn = _get_conn()
    name_cn = req.name_cn.strip()
    name_en = req.name_en.strip()
    if not name_cn or not name_en:
        raise HTTPException(400, "name_cn/name_en 不能为空")
    team_alias.confirm(conn, name_cn, name_en)
    rematch = fixture_match.match_all_periods(conn, seed=team_alias.SEED)
    return {"ok": True, "name_cn": name_cn, "name_en": name_en, "rematch": rematch}


@app.get("/api/teams")
def api_teams(q: str | None = None):
    """Return team candidates for the alias confirmation workflow."""
    conn = _get_conn()
    needle = (q or "").strip()
    if needle:
        pattern = f"%{needle}%"
        rows = conn.execute(
            """SELECT name_en, COALESCE(name_cn, '') AS name_cn
               FROM teams
               WHERE name_en LIKE ? OR COALESCE(name_cn, '') LIKE ?
               ORDER BY name_en LIMIT 100""",
            (pattern, pattern),
        )
    else:
        rows = conn.execute(
            """SELECT name_en, COALESCE(name_cn, '') AS name_cn
               FROM teams ORDER BY name_en LIMIT 100"""
        )
    return {"teams": [dict(row) for row in rows]}


def _legs_detail(conn, period_id, legs) -> list[dict]:
    """每场一行：队名 + 中文胜平负（双选/三选并列在同一行，不展开）。

    兼容两种 legs 形态：
      - sfc14: [[...], ...] 按序号 1..14 对应
      - r9:    {"selected": [0-based idx], "legs": [[...]]} 只展示选中的场次
    """
    if period_id is None:
        return []

    name_by_seq = {}
    for row in conn.execute(
        """SELECT seq, home_name_cn, away_name_cn FROM period_matches
           WHERE period_id=? ORDER BY seq""",
        (period_id,),
    ):
        name_by_seq[row["seq"]] = (row["home_name_cn"], row["away_name_cn"])

    if isinstance(legs, dict):
        pairs = [(int(i) + 1, leg)
                 for i, leg in zip(legs.get("selected") or [], legs.get("legs") or [])]
    else:
        pairs = list(enumerate(legs or [], start=1))

    out = []
    for seq, leg in pairs:
        selection = [str(x) for x in (leg or [])]
        if not selection:
            continue
        home, away = name_by_seq.get(seq, (None, None))
        out.append({
            "seq": seq,
            "home": home,
            "away": away,
            "selection": selection,
            "labels": [OUTCOME_LABELS.get(x, x) for x in selection],
        })
    return out


@app.get("/api/plans")
def api_plans(period_id: int, game_type: str):
    """某期某玩法下的全部已存方案（含每场选择明细），供历史战绩弹框使用。"""
    conn = _get_conn()
    rows = list(conn.execute(
        """SELECT pl.id, pl.objective, pl.budget, pl.notes_count,
                  pl.p_first, pl.p_second, pl.legs_json, pl.created_at,
                  p.period_no
           FROM plans pl JOIN periods p ON p.id = pl.period_id
           WHERE pl.period_id=? AND pl.game_type=?
           ORDER BY pl.id DESC""",
        (period_id, game_type),
    ))
    if not rows:
        raise HTTPException(404, "该期次/玩法下没有方案")
    plans = []
    for row in rows:
        try:
            legs = json.loads(row["legs_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            legs = []
        plans.append({
            "plan_id": row["id"],
            "objective": row["objective"],
            "budget": row["budget"],
            "notes_count": row["notes_count"],
            "amount": int(row["notes_count"] or 0) * 2,
            "p_first": row["p_first"],
            "p_second": row["p_second"],
            "created_at": row["created_at"],
            "legs_detail": _legs_detail(conn, period_id, legs),
        })
    return {"period_no": rows[0]["period_no"], "game_type": game_type, "plans": plans}


@app.post("/api/plan")
def api_plan(req: PlanReq):
    try:
        if req.game_type == "sfc14":
            if len(req.probs) != 14:
                raise HTTPException(400, "sfc14 需要 14 场概率")
            best = solver.solve_14(req.probs, budget=req.budget, objective=req.objective)
            legs = best["legs"]
            rows = exp.expand(legs)
            stored_legs = legs
        elif req.game_type == "r9":
            if len(req.probs) not in (9, 14):
                raise HTTPException(400, "任九需要 9 或 14 场概率")
            best = solver.solve_r9(req.probs, need=9, budget=req.budget)
            legs = [leg or [] for leg in best["legs"]]
            rows, selected = exp.expand_selected(best["legs"])
            stored_legs = {"selected": selected,
                           "legs": [best["legs"][i] for i in selected]}
        else:
            raise HTTPException(400, f"未知玩法 {req.game_type}")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    conn = _get_conn()
    period_id = req.period_id
    if period_id is None and req.period_no:
        row = conn.execute("SELECT id FROM periods WHERE period_no=?", (req.period_no,)).fetchone()
        period_id = row["id"] if row else None
    if period_id is None:
        latest = _period_with_matches(conn)
        period_id = latest["period_id"] if latest else None
    plan_id = None
    if period_id is not None:
        cur = conn.execute(
            """INSERT INTO plans(period_id, game_type, objective, budget, notes_count,
                                  p_first, p_second, p_win, legs_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (period_id, req.game_type, req.objective, req.budget, best["notes_count"],
             best.get("p_first"), best.get("p_second"), best.get("p_win"),
             json.dumps(stored_legs, ensure_ascii=False)),
        )
        conn.commit()
        plan_id = cur.lastrowid

    legs_detail = _legs_detail(conn, period_id, legs)

    return {
        "plan_id": plan_id,
        "p_first": best.get("p_first"),
        "p_second": best.get("p_second"),
        "p_win": best.get("p_win"),
        "notes_count": best["notes_count"],
        "amount": best["notes_count"] * 2,
        "legs": legs,
        "legs_detail": legs_detail,
        "rows": rows,
    }


@app.get("/api/history")
def api_history():
    """Return plan accounting without multiplying rows across plans and winnings."""
    conn = _get_conn()
    rows = list(conn.execute(
        """WITH plan_totals AS (
                 SELECT p.id AS period_id, p.period_no, pl.game_type,
                        COUNT(pl.id) AS plans,
                        COALESCE(SUM(pl.notes_count * 2), 0) AS invested
                 FROM plans pl
                 JOIN periods p ON p.id = pl.period_id
                 GROUP BY p.id, p.period_no, pl.game_type
             ), winning_totals AS (
                 SELECT pl.period_id, pl.game_type,
                        COALESCE(SUM(w.amount), 0) AS prize,
                        COALESCE(SUM(w.hit_notes), 0) AS hit_notes,
                        GROUP_CONCAT(DISTINCT w.tier) AS tiers
                 FROM plans pl
                 LEFT JOIN winnings w ON w.plan_id = pl.id
                 GROUP BY pl.period_id, pl.game_type
             )
             SELECT pt.period_id, pt.period_no, pt.game_type, pt.plans,
                    pt.invested, wt.prize, wt.hit_notes, wt.tiers
             FROM plan_totals pt
             JOIN winning_totals wt
               ON wt.period_id = pt.period_id AND wt.game_type = pt.game_type
             ORDER BY pt.period_no ASC, pt.game_type ASC"""
    ))
    summary = []
    cumulative_invested = 0.0
    cumulative_prize = 0.0
    cumulative_profit = 0.0
    for row in rows:
        invested = float(row["invested"] or 0)
        prize = float(row["prize"] or 0)
        profit = prize - invested
        cumulative_invested += invested
        cumulative_prize += prize
        cumulative_profit += profit
        tiers = [tier for tier in (row["tiers"] or "").split(",") if tier]
        summary.append({
            "period_id": row["period_id"],
            "period_no": row["period_no"],
            "game_type": row["game_type"],
            "plans": int(row["plans"] or 0),
            "invested": invested,
            "prize": prize,
            "profit": profit,
            "hit_notes": int(row["hit_notes"] or 0),
            "tiers": tiers,
            "tier": ",".join(tiers),
            "amt": prize,
            "n": int(row["hit_notes"] or 0),
            "cumulative_invested": cumulative_invested,
            "cumulative_prize": cumulative_prize,
            "cumulative_profit": cumulative_profit,
        })
    summary.sort(key=lambda item: (item["period_no"], item["game_type"]), reverse=True)
    totals = {
        "periods": len({item["period_id"] for item in summary}),
        "plans": sum(item["plans"] for item in summary),
        "invested": sum(item["invested"] for item in summary),
        "prize": sum(item["prize"] for item in summary),
        "profit": sum(item["profit"] for item in summary),
        "hit_notes": sum(item["hit_notes"] for item in summary),
    }
    return {"summary": summary, "totals": totals}


@app.get("/api/backtest-report")
def api_backtest_report():
    """读取 data/reports/ 下所有 JSON 报告。"""
    out: dict = {}
    p = Path("data/reports")
    if not p.exists():
        return out
    for f in sorted(p.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out
