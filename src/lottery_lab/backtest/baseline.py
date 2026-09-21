"""市场基线回测（设计 §4.1 / 实现计划 T10）。

读取 SQLite 中所有带赔率的 `matches` → 按 walk-forward 跑 `market.devig`
→ 计算 logloss / Brier → 输出 JSON 报告到 `data/reports/`。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from lottery_lab.models import market
from lottery_lab.backtest.metrics import logloss, brier, accuracy


def load_matches(conn: sqlite3.Connection) -> list[dict]:
    """从 DB 读历史比赛（含 odds），按日期升序。"""
    rows = list(conn.execute(
        """SELECT id, league_code, season, match_date, result, odds_json
           FROM matches
           WHERE odds_json IS NOT NULL AND result IS NOT NULL
           ORDER BY match_date, id"""
    ))
    out: list[dict] = []
    for r in rows:
        try:
            odds = json.loads(r["odds_json"] or "{}")
        except Exception:
            odds = {}
        d = date.fromisoformat(r["match_date"])
        out.append({
            "id": r["id"],
            "date": d,
            "league_code": r["league_code"],
            "season": r["season"],
            "result": r["result"],
            "odds": odds,
        })
    return out


def market_predict(matches: list[dict], odds_source: str = "avg") -> list[dict]:
    """对每场比赛预测：直接用 odds_json 的 [odds_source]。无/坏赔率则跳过。"""
    out: list[dict] = []
    for m in matches:
        if m["date"] >= date.today():
            continue  # type: ignore
        odds = m["odds"]
        if odds_source not in odds:
            continue
        try:
            probs = market.shin([
                float(odds[odds_source]["h"]),
                float(odds[odds_source]["d"]),
                float(odds[odds_source]["a"]),
            ])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({
            "id": m["id"],
            "date": m["date"],
            "probs": probs,
            "outcome": m["result"],
        })
    return out


def summarize(records: list[dict]) -> dict:
    if not records:
        return {"matches": 0}
    probs = [r["probs"] for r in records]
    outs = [r["outcome"] for r in records]
    return {
        "matches": len(records),
        "logloss": logloss(probs, outs),
        "brier": brier(probs, outs),
        "accuracy": accuracy(probs, outs),
    }


def run(
    db_path: str,
    start: str,
    odds_source: str = "avg",
    out_path: str = "data/reports/baseline_market.json",
) -> dict:
    from lottery_lab.db import store
    conn = store.connect(db_path)
    store.init_db(conn)
    matches = load_matches(conn)
    records = market_predict(matches, odds_source=odds_source)
    start_d = date.fromisoformat(start)
    records = [r for r in records if r["date"] >= start_d]
    summary = summarize(records)
    summary.update({
        "db_path": db_path,
        "start": start,
        "odds_source": odds_source,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/football.db")
    ap.add_argument("--start", required=True, help="回测起始日 YYYY-MM-DD")
    ap.add_argument("--odds", default="avg", choices=["avg", "max", "b365"])
    ap.add_argument("--out", default="data/reports/baseline_market.json")
    args = ap.parse_args()
    s = run(args.db, args.start, odds_source=args.odds, out_path=args.out)
    print(json.dumps(s, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
