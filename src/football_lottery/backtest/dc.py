"""Dixon-Coles 回测（T12 / 设计 §4.2）。

实现要点（性能友好）：
- 每个联赛在"训练段"内做一次滚动预测：每 refit_days 重新拟合一次；
- 训练数据按"截止该日期前 N 天的全部 past"装入，限定 max_train_matches 以保速度；
- 跳过 result 缺失或某队未在训练中出现（返回 1/3,1/3,1/3）以避免训练态爆炸；
- 全跑结束统一输出 logloss / Brier 与每联赛分桶到 `data/reports/baseline_dc.json`。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from football_lottery.backtest.metrics import logloss, brier, accuracy
from football_lottery.models import dixon_coles


def load_matches_with_teams(conn: sqlite3.Connection) -> list[dict]:
    rows = list(conn.execute(
        """SELECT m.id, m.league_code, m.season, m.match_date, m.result,
                  m.home_goals, m.away_goals,
                  ht.name_en AS home, at.name_en AS away
           FROM matches m
           JOIN teams ht ON ht.id = m.home_team_id
           JOIN teams at ON at.id = m.away_team_id
           WHERE m.result IS NOT NULL AND m.home_goals IS NOT NULL
           ORDER BY m.match_date, m.id"""
    ))
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "league_code": r["league_code"],
            "season": r["season"],
            "date": date.fromisoformat(r["match_date"]),
            "home": r["home"], "away": r["away"],
            "home_goals": r["home_goals"],
            "away_goals": r["away_goals"],
            "result": r["result"],
        })
    return out


def _fit_league(past: list[dict], half_life_days: float, max_train_matches: int = 1500):
    """用最近 N 场历史拟一个 DC。past 需按时间升序。"""
    train = past[-max_train_matches:]
    if not train:
        return None
    teams = sorted({p["home"] for p in train} | {p["away"] for p in train})
    rows = [(p["date"], p["home"], p["away"], p["home_goals"], p["away_goals"])
            for p in train]
    try:
        return dixon_coles.DixonColes(half_life_days=half_life_days).fit(rows, teams)
    except Exception:
        return None


def run(
    db_path: str,
    start: str,
    half_life_days: float = 180.0,
    refit_days: int = 30,
    out_path: str = "data/reports/baseline_dc.json",
    max_train_matches: int = 1500,
) -> dict:
    from football_lottery.db import store
    conn = store.connect(db_path)
    store.init_db(conn)
    matches = load_matches_with_teams(conn)
    start_d = date.fromisoformat(start)

    by_league: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        by_league[m["league_code"]].append(m)

    records: list[dict] = []
    skipped = 0
    per_league_records: dict[str, list[dict]] = defaultdict(list)
    per_league_skipped: dict[str, int] = defaultdict(int)

    # 对每个联赛独立跑 walk-forward
    for lg, league_matches in by_league.items():
        model = None
        last_fit: date | None = None
        for i, m in enumerate(league_matches):
            if m["date"] < start_d:
                continue
            past = league_matches[:i]
            if not past:
                continue
            if model is None or (
                last_fit is not None and (m["date"] - last_fit) >= timedelta(days=refit_days)
            ):
                model = _fit_league(past, half_life_days, max_train_matches)
                last_fit = m["date"]
                if model is None:
                    continue
            try:
                probs = model.predict(m["home"], m["away"])
            except Exception:
                skipped += 1
                per_league_skipped[lg] += 1
                continue
            r = {"id": m["id"], "date": m["date"], "probs": probs, "outcome": m["result"]}
            records.append(r)
            per_league_records[lg].append(r)

    if not records:
        return {"matches": 0, "skipped": skipped}
    probs = [r["probs"] for r in records]
    outs = [r["outcome"] for r in records]
    per_league_summary = {}
    for lg, rs in per_league_records.items():
        if rs:
            p = [r["probs"] for r in rs]
            o = [r["outcome"] for r in rs]
            per_league_summary[lg] = {
                "matches": len(rs),
                "logloss": logloss(p, o),
                "brier": brier(p, o),
                "accuracy": accuracy(p, o),
                "skipped": per_league_skipped.get(lg, 0),
            }
    summary = {
        "matches": len(records),
        "logloss": logloss(probs, outs),
        "brier": brier(probs, outs),
        "accuracy": accuracy(probs, outs),
        "skipped": skipped,
        "half_life_days": half_life_days,
        "refit_days": refit_days,
        "max_train_matches": max_train_matches,
        "start": start,
        "db_path": db_path,
        "per_league": per_league_summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/football.db")
    ap.add_argument("--start", required=True)
    ap.add_argument("--half-life", type=float, default=180.0)
    ap.add_argument("--refit-days", type=int, default=30)
    ap.add_argument("--max-train", type=int, default=1500)
    ap.add_argument("--out", default="data/reports/baseline_dc.json")
    args = ap.parse_args()
    s = run(args.db, args.start, args.half_life, args.refit_days, args.out, args.max_train)
    print(json.dumps(s, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
