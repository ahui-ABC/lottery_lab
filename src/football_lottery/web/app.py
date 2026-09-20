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


app = FastAPI(title="足彩预测工具", docs_url="/docs")

# 胜负彩 3/1/0 是主队视角：3=主胜、1=平、0=主负（客胜）
OUTCOME_LABELS = {"3": "胜", "1": "平", "0": "负"}

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
@app.get("/")
def root():
    return RedirectResponse(url="/predict")


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

    # 每场一行：队名 + 中文胜平负（双选/三选并列在同一行，不展开）
    name_by_seq = {}
    if period_id is not None:
        for row in conn.execute(
            """SELECT seq, home_name_cn, away_name_cn FROM period_matches
               WHERE period_id=? ORDER BY seq""",
            (period_id,),
        ):
            name_by_seq[row["seq"]] = (row["home_name_cn"], row["away_name_cn"])

    legs_detail = []
    for idx, leg in enumerate(legs, start=1):
        selection = [str(x) for x in (leg or [])]
        if not selection:
            continue
        home, away = name_by_seq.get(idx, (None, None))
        legs_detail.append({
            "seq": idx,
            "home": home,
            "away": away,
            "selection": selection,
            "labels": [OUTCOME_LABELS.get(x, x) for x in selection],
        })

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
