"""Historical period/plan backtest over the local SQLite data."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from football_lottery import winnings
from football_lottery.models import market
from football_lottery.optimizer import solver


def _json(value, fallback=None):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _period_probs(conn, period_id: int, use_fused: bool) -> list[list[float]] | None:
    rows = list(conn.execute(
        """SELECT pm.seq, pm.odds_json, m.odds_json AS linked_odds,
                  p.fused_json, p.market_json
           FROM period_matches pm
           LEFT JOIN matches m ON m.id=pm.match_id
           LEFT JOIN predictions p ON p.id=(
               SELECT p2.id FROM predictions p2
               WHERE p2.period_match_id=pm.id
               ORDER BY p2.created_at DESC, p2.id DESC LIMIT 1
           )
           WHERE pm.period_id=? ORDER BY pm.seq""", (period_id,)
    ))
    if len(rows) < 14:
        return None
    out = []
    for row in rows[:14]:
        value = _json(row["fused_json"] if use_fused else row["market_json"])
        if not value:
            odds = _json(row["odds_json"], {}) or _json(row["linked_odds"], {})
            value = market.devig(odds)
        if not value:
            return None
        out.append(value)
    return out


def _r9_legs(best: dict) -> tuple[list[list[str]], list[int]]:
    selected = [(i, leg) for i, leg in enumerate(best["legs"]) if leg]
    return [leg for _, leg in selected], [i for i, _ in selected]


def _evaluate(game_type: str, best: dict, results: list[str], prizes: dict) -> dict:
    if game_type == "sfc14":
        first, second = winnings.hit_counts(best["legs"], results)
        first_prize = winnings.amount(first, prizes.get("first"))
        second_prize = winnings.amount(second, prizes.get("second"))
        return {
            "first_notes": first, "second_notes": second,
            "first_prize": first_prize, "second_prize": second_prize,
            "prize": first_prize + second_prize,
        }
    legs, selected = _r9_legs(best)
    selected_results = [results[i] for i in selected]
    hit = winnings.r9_hit(legs, selected_results)
    return {"r9_notes": hit, "prize": winnings.amount(hit, prizes.get("r9"))}


def _empty_summary() -> dict:
    return {
        "periods": 0,
        "winning_periods": 0,
        "invested": 0,
        "prize": 0.0,
        "sfc14": {
            "invested": 0, "prize": 0.0, "winning_periods": 0,
            "first_notes": 0, "second_notes": 0,
        },
        "r9": {
            "invested": 0, "prize": 0.0, "winning_periods": 0,
            "r9_notes": 0,
        },
    }


def _record(summary: dict, sfc: dict, r9: dict, sfc_invested: int, r9_invested: int) -> None:
    total_prize = sfc["prize"] + r9["prize"]
    summary["periods"] += 1
    summary["winning_periods"] += int(total_prize > 0)
    summary["invested"] += sfc_invested + r9_invested
    summary["prize"] += total_prize

    sfc_summary = summary["sfc14"]
    sfc_summary["invested"] += sfc_invested
    sfc_summary["prize"] += sfc["prize"]
    sfc_summary["winning_periods"] += int(sfc["first_notes"] + sfc["second_notes"] > 0)
    sfc_summary["first_notes"] += sfc["first_notes"]
    sfc_summary["second_notes"] += sfc["second_notes"]

    r9_summary = summary["r9"]
    r9_summary["invested"] += r9_invested
    r9_summary["prize"] += r9["prize"]
    r9_summary["winning_periods"] += int(r9["r9_notes"] > 0)
    r9_summary["r9_notes"] += r9["r9_notes"]


def _finish_summary(summary: dict) -> None:
    summary["roi"] = summary["prize"] / summary["invested"] if summary["invested"] else 0.0
    for game_type in ("sfc14", "r9"):
        game = summary[game_type]
        game["roi"] = game["prize"] / game["invested"] if game["invested"] else 0.0


def run(
    db_path: str = "data/football.db",
    start: str | None = None,
    budget: int = 64,
    out_path: str = "data/reports/plan_backtest.json",
) -> dict:
    from football_lottery.db import store
    conn = store.connect(db_path)
    store.init_db(conn)
    params = []
    where = ""
    if start:
        where = "AND (p.draw_date IS NULL OR p.draw_date >= ?)"
        params.append(start)
    periods = list(conn.execute(
        f"""SELECT p.id, p.period_no, d.results_json, d.prizes_json
            FROM periods p JOIN draw_results d ON d.period_id=p.id
            WHERE 1=1 {where} ORDER BY COALESCE(p.draw_date, p.period_no), p.id""", params
    ))
    rows = []
    aggregate = {"fused": _empty_summary(), "market": _empty_summary()}
    for period in periods:
        results = [x.strip() for x in (period["results_json"] or "").split(",")]
        if len(results) != 14 or any(x not in {"0", "1", "3"} for x in results):
            continue
        prizes = _json(period["prizes_json"], {}) or {}
        item = {"period_no": period["period_no"]}
        for mode, use_fused in (("fused", True), ("market", False)):
            probs = _period_probs(conn, period["id"], use_fused)
            if probs is None:
                item[mode] = {"status": "skipped", "reason": "missing_probabilities"}
                continue
            sfc = solver.solve_14(probs, budget=budget, objective="first_second")
            r9 = solver.solve_r9(probs, need=9, budget=budget)
            sfc_eval = _evaluate("sfc14", sfc, results, prizes)
            r9_eval = _evaluate("r9", r9, results, prizes)
            sfc_invested = sfc["notes_count"] * 2
            r9_invested = r9["notes_count"] * 2
            invested = sfc_invested + r9_invested
            _record(aggregate[mode], sfc_eval, r9_eval, sfc_invested, r9_invested)
            item[mode] = {
                "invested": invested,
                "prize": sfc_eval["prize"] + r9_eval["prize"],
                "sfc14": {"invested": sfc_invested, **sfc_eval},
                "r9": {"invested": r9_invested, **r9_eval},
            }
        rows.append(item)
    for summary in aggregate.values():
        _finish_summary(summary)
    report = {
        "status": "ok" if rows else "no_period_data",
        "db_path": db_path,
        "start": start,
        "budget_per_game": budget,
        "periods": rows,
        "summary": aggregate,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
