"""Shared local prediction pipeline used by CLI and Web.

The project deliberately keeps this layer file/SQLite based: no external
database, cache service, or model server is required for a local run.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from lottery_lab.db import store
from lottery_lab.features.build import FEATURE_COLUMNS, FeatureBuilder
from lottery_lab.models import dixon_coles, market
from lottery_lab.models.fusion import Fusion
from lottery_lab.models.gbdt import GBDTModel

OUTCOME_INDEX = {"H": 0, "D": 1, "A": 2}


def _parse_odds(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def load_matches(conn, completed_only: bool = False) -> list[dict]:
    where = "WHERE m.result IS NOT NULL AND m.home_goals IS NOT NULL" if completed_only else ""
    rows = conn.execute(
        f"""SELECT m.id, m.league_code, m.season, m.match_date, m.result,
                   m.home_goals, m.away_goals, m.odds_json,
                   ht.name_en AS home, at.name_en AS away
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            {where}
            ORDER BY m.match_date, m.id"""
    )
    out: list[dict] = []
    for row in rows:
        out.append({
            "id": row["id"],
            "league_code": row["league_code"],
            "season": row["season"],
            "date": date.fromisoformat(row["match_date"]),
            "result": row["result"],
            "home_goals": row["home_goals"],
            "away_goals": row["away_goals"],
            "odds": _parse_odds(row["odds_json"]),
            "home": row["home"],
            "away": row["away"],
        })
    return out


def feature_map(matches: list[dict]) -> dict[int, dict]:
    return FeatureBuilder().build(sorted(matches, key=lambda m: (m["date"], m["id"])))


def feature_matrix(features: dict[int, dict], matches: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X: list[list[float]] = []
    y: list[int] = []
    for match in matches:
        row = features.get(match["id"])
        label = OUTCOME_INDEX.get(match.get("result"))
        if row is None or label is None:
            continue
        X.append([float(row.get(column, 0.0)) for column in FEATURE_COLUMNS])
        y.append(label)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int)


def choose_backend(use_lightgbm: bool = True) -> str:
    if use_lightgbm:
        try:
            import lightgbm  # noqa: F401
            return "lightgbm"
        except ImportError:
            pass
    return "sklearn"


def train_gbdt(
    conn,
    models_dir: str = "data/models",
    use_lightgbm: bool = True,
    n_estimators: int = 200,
    learning_rate: float = 0.05,
) -> dict:
    matches = load_matches(conn, completed_only=True)
    features = feature_map(matches)
    X, y = feature_matrix(features, matches)
    if len(X) < 3 or len(np.unique(y)) < 2:
        raise ValueError("not enough completed matches/classes to train GBDT")
    backend = choose_backend(use_lightgbm)
    model = GBDTModel(
        backend=backend,
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        random_state=0,
    ).fit(X, y)
    models_path = Path(models_dir)
    models_path.mkdir(parents=True, exist_ok=True)
    version = datetime.now().strftime("gbdt-%Y%m%d%H%M%S")
    artifact = models_path / f"{version}.joblib"
    model.save(str(artifact))
    metrics = {"samples": int(len(X)), "classes": sorted(int(v) for v in np.unique(y))}
    store.upsert(conn, "model_versions", {
        "version": version,
        "model_type": "gbdt",
        "params_json": json.dumps({"backend": backend, "features": FEATURE_COLUMNS}),
        "metrics_json": json.dumps(metrics),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
    }, ["version"])
    return {
        "version": version,
        "path": str(artifact),
        "backend": backend,
        **metrics,
    }


def load_latest_gbdt(models_dir: str = "data/models") -> tuple[GBDTModel | None, str | None]:
    paths = sorted(Path(models_dir).glob("gbdt-*.joblib"))
    if not paths:
        return None, None
    try:
        return GBDTModel().load(str(paths[-1])), paths[-1].stem
    except Exception:
        return None, None


def _fit_dc(history: list[dict], target: dict, cache: dict) -> list[float] | None:
    key = (target["league_code"], target["date"])
    if key not in cache:
        rows = [m for m in history if m["league_code"] == target["league_code"]
                and m["date"] < target["date"] and m.get("result")]
        if len(rows) < 8:
            cache[key] = None
        else:
            teams = sorted({m["home"] for m in rows} | {m["away"] for m in rows})
            dc_rows = [(m["date"], m["home"], m["away"], m["home_goals"], m["away_goals"])
                       for m in rows[-1500:]]
            try:
                cache[key] = dixon_coles.DixonColes(half_life_days=365).fit(dc_rows, teams)
            except Exception:
                cache[key] = None
    model = cache[key]
    if model is None:
        return None
    try:
        return model.predict(target["home"], target["away"])
    except Exception:
        return None


def predict_components(
    target: dict,
    history: list[dict],
    gbdt_model: GBDTModel | None = None,
    features: dict[int, dict] | None = None,
    dc_cache: dict | None = None,
    fusion_mode: str = "full",
) -> dict[str, list[float] | None]:
    market_probs = market.devig(target.get("odds"))
    dc_probs = _fit_dc(history, target, dc_cache if dc_cache is not None else {})
    gbdt_probs = None
    if gbdt_model is not None and features is not None and target.get("id") in features:
        row = features[target["id"]]
        X = np.asarray([[float(row.get(c, 0.0)) for c in FEATURE_COLUMNS]], dtype=float)
        try:
            gbdt_probs = gbdt_model.predict_proba(X)[0].tolist()
        except Exception:
            gbdt_probs = None
    fusion = Fusion(mode=fusion_mode).predict(
        {"market": market_probs, "dc": dc_probs, "gbdt": gbdt_probs}
    )
    return {"market": market_probs, "dc": dc_probs, "gbdt": gbdt_probs, "fused": fusion}


def period_predictions(
    conn,
    period_id: int,
    models_dir: str = "data/models",
    model_version: str | None = None,
    fusion_mode: str = "full",
) -> dict:
    """Predict and persist one period. Missing history only disables model legs."""
    period = conn.execute("SELECT id, period_no FROM periods WHERE id=?", (period_id,)).fetchone()
    if not period:
        raise ValueError("period not found")
    history = load_matches(conn, completed_only=True)
    features = feature_map(history)
    gbdt_model, artifact_version = load_latest_gbdt(models_dir)
    version = model_version or artifact_version or "market-primary"
    rows = list(conn.execute(
        """SELECT id, seq, home_name_cn, away_name_cn, match_time, match_id, odds_json
           FROM period_matches WHERE period_id=? ORDER BY seq""", (period_id,)
    ))
    linked = {m["id"]: m for m in history}
    dc_cache: dict = {}
    output = []
    for pm in rows:
        target = linked.get(pm["match_id"])
        odds = _parse_odds(pm["odds_json"])
        if target is None:
            component = {"market": market.devig(odds), "dc": None, "gbdt": None}
            component["fused"] = Fusion(mode=fusion_mode).predict(component)
        else:
            target = {**target, "odds": odds or target.get("odds", {})}
            component = predict_components(
                target, history, gbdt_model, features, dc_cache, fusion_mode=fusion_mode
            )
        store.upsert(conn, "predictions", {
            "period_match_id": pm["id"],
            "model_version": version,
            "market_json": json.dumps(component["market"]),
            "dc_json": json.dumps(component["dc"]),
            "gbdt_json": json.dumps(component["gbdt"]),
            "fused_json": json.dumps(component["fused"]),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }, ["period_match_id", "model_version"])
        output.append({"seq": pm["seq"], **component})
    conn.commit()
    return {"period_no": period["period_no"], "model_version": version, "matches": output}
