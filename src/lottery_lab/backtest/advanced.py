"""Walk-forward GBDT and fused probability backtests."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from lottery_lab.backtest.metrics import accuracy, brier, logloss
from lottery_lab.db import store
from lottery_lab.models import dixon_coles, market
from lottery_lab.models.fusion import Fusion, tier
from lottery_lab.models.gbdt import GBDTModel
from lottery_lab.models.pipeline import (
    feature_map,
    feature_matrix,
    load_matches,
    choose_backend,
)
from lottery_lab.features.build import FEATURE_COLUMNS


def _summary(records: list[dict]) -> dict:
    if not records:
        return {"matches": 0}
    probs = [r["probs"] for r in records]
    outcomes = [r["outcome"] for r in records]
    return {
        "matches": len(records),
        "logloss": logloss(probs, outcomes),
        "brier": brier(probs, outcomes),
        "accuracy": accuracy(probs, outcomes),
    }


def _fit_gbdt(past: list[dict], features: dict[int, dict], max_train: int, backend: str) -> GBDTModel | None:
    train = past[-max_train:]
    X, y = feature_matrix(features, train)
    if len(X) < 30 or len(np.unique(y)) < 2:
        return None
    try:
        return GBDTModel(backend=backend, n_estimators=150, learning_rate=0.05,
                         random_state=0).fit(X, y)
    except Exception:
        return None


def _fit_dc_models(past: list[dict], half_life: float, max_train: int) -> dict[str, dixon_coles.DixonColes]:
    models = {}
    by_league: dict[str, list[dict]] = defaultdict(list)
    for row in past:
        by_league[row["league_code"]].append(row)
    for league, rows in by_league.items():
        rows = rows[-max_train:]
        if len(rows) < 8:
            continue
        teams = sorted({r["home"] for r in rows} | {r["away"] for r in rows})
        dc_rows = [(r["date"], r["home"], r["away"], r["home_goals"], r["away_goals"])
                   for r in rows]
        try:
            models[league] = dixon_coles.DixonColes(half_life_days=half_life).fit(dc_rows, teams)
        except Exception:
            continue
    return models


def run_gbdt(
    db_path: str,
    start: str,
    refit_days: int = 30,
    max_train_matches: int = 3000,
    use_lightgbm: bool = True,
    out_path: str = "data/reports/baseline_gbdt.json",
) -> dict:
    conn = store.connect(db_path)
    store.init_db(conn)
    matches = load_matches(conn, completed_only=True)
    features = feature_map(matches)
    backend = choose_backend(use_lightgbm)
    start_date = date.fromisoformat(start)
    model = None
    last_fit: date | None = None
    records: list[dict] = []
    for i, current in enumerate(matches):
        past = [p for p in matches[:i] if p["date"] < current["date"]]
        if not past:
            continue
        if model is None or (last_fit is not None and current["date"] - last_fit >= timedelta(days=refit_days)):
            model = _fit_gbdt(past, features, max_train_matches, backend)
            last_fit = current["date"]
        if current["date"] < start_date or model is None:
            continue
        row = features.get(current["id"])
        if row is None:
            continue
        X = np.asarray([[float(row.get(c, 0.0)) for c in FEATURE_COLUMNS]])
        try:
            probs = model.predict_proba(X)[0].tolist()
        except Exception:
            continue
        records.append({"id": current["id"], "date": current["date"],
                        "probs": probs, "outcome": current["result"]})
    summary = _summary(records)
    summary.update({"db_path": db_path, "start": start, "refit_days": refit_days,
                    "max_train_matches": max_train_matches, "backend": backend,
                    "generated_at": datetime.now().isoformat(timespec="seconds")})
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_fused(
    db_path: str,
    start: str,
    refit_days: int = 30,
    max_train_matches: int = 1500,
    half_life_days: float = 365.0,
    use_lightgbm: bool = True,
    out_path: str = "data/reports/baseline_fused.json",
) -> dict:
    conn = store.connect(db_path)
    store.init_db(conn)
    matches = load_matches(conn, completed_only=True)
    features = feature_map(matches)
    backend = choose_backend(use_lightgbm)
    start_date = date.fromisoformat(start)
    gbdt_model = None
    dc_models: dict = {}
    last_fit: date | None = None
    fusion = Fusion()
    calibration: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    records: list[dict] = []
    component_records: dict[str, list[dict]] = defaultdict(list)
    candidate_by_component: dict[str, list[dict]] = defaultdict(list)
    for i, current in enumerate(matches):
        past = [p for p in matches[:i] if p["date"] < current["date"]]
        if not past:
            continue
        if last_fit is None or current["date"] - last_fit >= timedelta(days=refit_days):
            gbdt_model = _fit_gbdt(past, features, max_train_matches, backend)
            dc_models = _fit_dc_models(past, half_life_days, max_train_matches)
            last_fit = current["date"]
            for name, values in calibration.items():
                if len(values.get("y", [])) >= 30:
                    payload = {"y": np.asarray(values["y"])}
                    for key in ("market", "dc", "gbdt"):
                        if values.get(key):
                            payload[key] = np.asarray(values[key])
                    try:
                        fusion.fit(payload, tier=name)
                    except Exception:
                        pass
        if current["date"] < start_date:
            continue
        components = {"market": market.devig(current.get("odds")), "dc": None, "gbdt": None}
        dc = dc_models.get(current["league_code"])
        if dc is not None:
            try:
                components["dc"] = dc.predict(current["home"], current["away"])
            except Exception:
                pass
        row = features.get(current["id"])
        if gbdt_model is not None and row is not None:
            try:
                X = np.asarray([[float(row.get(c, 0.0)) for c in FEATURE_COLUMNS]])
                components["gbdt"] = gbdt_model.predict_proba(X)[0].tolist()
            except Exception:
                pass
        probs = fusion.predict(components)
        records.append({"id": current["id"], "date": current["date"],
                        "probs": probs, "outcome": current["result"],
                        "market_probs": components["market"]})
        for name, component_probs in components.items():
            if component_probs is None:
                continue
            component_record = {"id": current["id"], "date": current["date"],
                                "probs": component_probs, "outcome": current["result"]}
            component_records[name].append(component_record)
            candidate_by_component[name].append(
                {"id": current["id"], "date": current["date"],
                 "probs": probs, "outcome": current["result"]}
            )
        current_tier = tier(components)
        keys = Fusion._keys_for_tier(current_tier)
        if keys:
            calibration[current_tier]["y"].append({"H": 0, "D": 1, "A": 2}[current["result"]])
            for key in keys:
                calibration[current_tier][key].append(components[key])
    candidate_summary = _summary(records)
    component_comparison = {}
    for name, single_records in component_records.items():
        candidate_records = candidate_by_component[name]
        component_comparison[name] = {
            "single": _summary(single_records),
            "candidate": _summary(candidate_records),
        }
    required = [
        pair for name, pair in component_comparison.items()
        if name in {"market", "dc", "gbdt"} and pair["single"].get("matches", 0)
    ]
    accepted = bool(required) and all(
        pair["candidate"].get("logloss", float("inf")) <=
        pair["single"].get("logloss", float("inf")) + 1e-12
        for pair in required
    )
    deployed_records = []
    for record in records:
        if accepted or record.get("market_probs") is None:
            deployed_records.append(record)
        else:
            deployed_records.append({**record, "probs": record["market_probs"]})
    summary = _summary(deployed_records)
    summary.update({"db_path": db_path, "start": start, "refit_days": refit_days,
                    "max_train_matches": max_train_matches, "half_life_days": half_life_days,
                    "backend": backend, "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "deployment_mode": "fused" if accepted else "market_primary",
                    "accepted": accepted,
                    "candidate": candidate_summary,
                    "market_baseline": _summary(component_records.get("market", [])),
                    "component_comparison": component_comparison,
                    "fallback_reason": None if accepted else "candidate fusion did not beat every available single component",
                    })
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
