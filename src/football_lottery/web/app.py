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
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from football_lottery.models import market
from football_lottery.db import store
from football_lottery.optimizer import solver
from football_lottery.optimizer import expand as exp


app = FastAPI(title="足彩预测工具", docs_url="/docs")

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
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
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


def _period_with_matches(conn) -> dict | None:
    """从 DB 拿最近一期 + 14 场对阵（优先有 match_id 关联的）。"""
    p = conn.execute(
        "SELECT id, period_no FROM periods ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not p:
        return None
    pm = list(conn.execute(
        """SELECT seq, home_name_cn, away_name_cn, match_id, match_time
           FROM period_matches WHERE period_id=?
           ORDER BY seq""",
        (p["id"],),
    ))
    if len(pm) < 14:
        return None
    enriched = []
    for row in pm[:14]:
        d = {"seq": row["seq"],
             "home_cn": row["home_name_cn"], "away_cn": row["away_name_cn"],
             "match_time": row["match_time"], "match_id": row["match_id"]}
        if row["match_id"]:
            m = conn.execute(
                """SELECT odds_json FROM matches WHERE id=?""", (row["match_id"],)
            ).fetchone()
            try:
                d["odds"] = json.loads(m["odds_json"] or "{}")
            except Exception:
                d["odds"] = {}
        else:
            d["odds"] = {}
        enriched.append(d)
    return {"period_no": p["period_no"], "matches": enriched}


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
    """对当前期 14 场给三路概率（市场去水 / DC / 占位 1/3）。"""
    conn = _get_conn()
    period = _period_with_matches(conn)
    if not period:
        return {"warning": "无当期数据", "matches": []}
    matches = []
    for m in period["matches"]:
        market_probs = None
        try:
            market_probs = market.devig(m.get("odds"))
        except Exception:
            market_probs = None
        matches.append({
            "seq": m["seq"],
            "home": m["home_cn"], "away": m["away_cn"],
            "match_time": m["match_time"],
            "market": market_probs,
            "dc": None,             # 接 run_dc 时填充
            "gbdt": None,
            "fused": market_probs,  # 无三路时退化 = market
        })
    return {"period_no": period["period_no"], "matches": matches}


class PlanReq(BaseModel):
    game_type: str            # "sfc14" | "r9"
    objective: str = "first_second"   # "first" | "first_second"
    budget: int = 64
    probs: list[list[float]]  # 14 / 9 × [ph, pd, pa]


@app.post("/api/plan")
def api_plan(req: PlanReq):
    if req.game_type == "sfc14":
        if len(req.probs) != 14:
            raise HTTPException(400, "sfc14 需要 14 场概率")
        best = solver.solve_14(req.probs, budget=req.budget, objective=req.objective)
        legs = best["legs"]
    elif req.game_type == "r9":
        if len(req.probs) not in (9, 14):
            raise HTTPException(400, "任九至少传 9 场概率")
        best = solver.solve_r9(req.probs, need=9, budget=req.budget)
        # 任九 legs 含 None（未选），仅展有值场
        legs = [leg or [] for leg in best["legs"]]
    else:
        raise HTTPException(400, f"未知玩法 {req.game_type}")
    rows = exp.expand(legs) if all(legs) else []
    return {
        "p_first": best.get("p_first"),
        "p_second": best.get("p_second"),
        "p_win": best.get("p_win"),
        "notes_count": best["notes_count"],
        "amount": best["notes_count"] * 2,
        "legs": legs,
        "rows": rows,
    }


@app.get("/api/history")
def api_history():
    """汇总每个有 winnings 的 period 战绩（仅在跑过对奖时有数据）。"""
    conn = _get_conn()
    rows = list(conn.execute(
        """SELECT p.period_no, w.tier, SUM(w.amount) amt, SUM(w.hit_notes) n
           FROM winnings w JOIN periods p ON p.id=w.period_id
           WHERE w.amount IS NOT NULL
           GROUP BY p.period_no, w.tier
           ORDER BY p.period_no DESC"""
    ))
    return {"summary": [dict(r) for r in rows]}


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
