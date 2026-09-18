"""足彩预测工具 CLI 入口。

子命令按阶段陆续添加：`collect-history` / `collect-period` / `collect-draws` /
`import-fixtures` / `map-fixtures` / `data-health` / `train` / `backtest` /
`backtest-plans` / `check-draw` / `serve`。
"""
import argparse
import io
import sys
from pathlib import Path

# 让 print 在 cp936 Windows 控制台也能输出中文（不影响文件 IO）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json  # noqa: E402
import yaml  # noqa: E402  (放在 stdout reconfigure 之后,顺序敏感)


def _load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _connect(cfg: dict):
    from football_lottery.db import store
    db_path = cfg.get("db_path", "data/football.db")
    conn = store.connect(db_path)
    store.init_db(conn)
    return conn


def data_health(conn) -> dict:
    """汇总数据库关键计数：比赛总数、赔率缺失率、未映射期次、未确认别名。"""
    import json as _json
    total = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()[0]
    with_odds = 0
    for r in conn.execute("SELECT odds_json FROM matches"):
        if not r[0]:
            continue
        try:
            j = _json.loads(r[0])
            if "avg" in j or "b365" in j or "max" in j:
                with_odds += 1
        except Exception:
            pass
    odds_missing_rate = round(1.0 - (with_odds / total), 4) if total else 0.0
    periods_unmapped = conn.execute(
        """SELECT COUNT(*) FROM period_matches WHERE match_id IS NULL"""
    ).fetchone()[0]
    unconfirmed = conn.execute(
        """SELECT COUNT(*) FROM team_alias WHERE confirmed=0"""
    ).fetchone()[0]
    by_league = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT league_code, COUNT(*) FROM matches GROUP BY league_code ORDER BY 1"
        )
    }
    last_match = conn.execute(
        "SELECT MAX(match_date) FROM matches"
    ).fetchone()[0]
    out = {
        "matches_total": total,
        "matches_with_odds": with_odds,
        "odds_missing_rate": odds_missing_rate,
        "by_league": by_league,
        "periods_total": conn.execute("SELECT COUNT(*) FROM periods").fetchone()[0],
        "unmapped_fixtures": periods_unmapped,
        "unconfirmed_aliases": unconfirmed,
        "last_match_date": last_match,
    }
    return out


def cmd_collect_history(args, cfg: dict) -> int:
    from football_lottery.collectors import fd
    seasons = args.seasons or cfg.get("seasons", ["2122"])
    divisions = args.divisions or cfg.get("divisions", ["E0"])
    conn = _connect(cfg)
    n = fd.fetch_and_collect(conn, seasons, divisions, silent=False)
    print(f"合计入库 {n} 条（分赛季联赛见上方输出）")
    return 0


def cmd_data_health(args, cfg: dict) -> int:
    conn = _connect(cfg)
    out = data_health(conn)
    import json as _json
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_import_fixtures(args, cfg: dict) -> int:
    from football_lottery.collectors import sporttery
    conn = _connect(cfg)
    out = sporttery.import_fixtures_csv(conn, args.file)
    import json as _json
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_map_fixtures(args, cfg: dict) -> int:
    from football_lottery.collectors import fixture_match
    conn = _connect(cfg)
    from football_lottery.collectors import team_alias
    if args.period:
        out = {args.period: fixture_match.match_period(conn, args.period, seed=team_alias.SEED)}
    else:
        out = fixture_match.match_all_periods(conn, seed=team_alias.SEED)
    import json as _json
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_check_draw(args, cfg: dict) -> int:
    """对指定期（或全部）跑对奖：从 draw_results 取开奖，对每个 plans 计 hit。"""
    from football_lottery import winnings as W
    from football_lottery.db import store
    import json as _json

    conn = _connect(cfg)
    periods = []
    if getattr(args, "period", None):
        pn = args.period
        row = conn.execute(
            "SELECT id, period_no FROM periods WHERE period_no=?", (pn,)
        ).fetchone()
        if not row:
            return 1
        periods = [(row["id"], row["period_no"])]
    else:
        periods = list(conn.execute("SELECT id, period_no FROM periods"))

    total_winnings = 0
    for pid, pn in periods:
        dr = conn.execute(
            "SELECT results_json, prizes_json FROM draw_results WHERE period_id=?",
            (pid,),
        ).fetchone()
        if not dr:
            continue
        results = [r.strip() for r in dr["results_json"].split(",")]
        if len(results) != 14:
            continue
        prizes = {}
        if dr["prizes_json"]:
            try: prizes = _json.loads(dr["prizes_json"])
            except Exception: prizes = {}
        # 对每个 plan 计奖
        plans = list(conn.execute(
            "SELECT id, game_type, legs_json FROM plans WHERE period_id=?",
            (pid,),
        ))
        for plan in plans:
            legs = _json.loads(plan["legs_json"])
            if plan["game_type"] == "sfc14" and len(legs) == 14:
                first, second = W.hit_counts(legs, results)
                tier_list = [("first", first, prizes.get("first")),
                             ("second", second, prizes.get("second"))]
            elif plan["game_type"] == "r9" and len(legs) == 9:
                hit = W.r9_hit(legs, results)
                tier_list = [("r9", hit, prizes.get("r9"))]
            else:
                continue
            for tier, n, prize in tier_list:
                if n <= 0:
                    continue
                amt = W.amount(n, prize)
                store.upsert(conn, "winnings", {
                    "period_id": pid, "plan_id": plan["id"],
                    "tier": tier, "hit_notes": n,
                    "single_prize": prize, "amount": amt,
                }, ["plan_id", "tier"])
                total_winnings += 1
    print(f"新增/更新 {total_winnings} 条对奖记录")
    return 0


def cmd_serve(args, cfg: dict) -> int:
    import uvicorn
    port = int(args.port or cfg.get("serve_port", 8765))
    uvicorn.run(
        "football_lottery.web.app:app",
        host="127.0.0.1",
        port=port,
        reload=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="football_lottery", description="足彩预测工具")
    sub = p.add_subparsers(dest="cmd")

    # collect-history
    s = sub.add_parser("collect-history", help="联网下载 football-data 历史（5 赛季 × 9 联赛）")
    s.add_argument("--seasons", nargs="+", help="如 2122 2223 ... 2526")
    s.add_argument("--divisions", nargs="+", help="如 E0 D1 I1 ...")

    # import-fixtures
    s = sub.add_parser("import-fixtures", help="导入历史期次对阵 CSV（兜底 T5）")
    s.add_argument("--file", required=True, help="fixture CSV 路径")

    # map-fixtures
    s = sub.add_parser("map-fixtures", help="把 period_matches 中文名映射到 matches.id")
    s.add_argument("--period", help="指定期号；不传则所有期次")

    # data-health
    s = sub.add_parser("data-health", help="数据库体检")

    # check-draw
    s = sub.add_parser("check-draw", help="对指定期跑对奖并写 winnings")
    s.add_argument("--period", help="指定期号；不传则所有期次")

    # backtest
    s = sub.add_parser("backtest", help="回测（market/dc/gbdt/fused）")
    s.add_argument("--model", default="market", choices=["market", "dc"])
    s.add_argument("--start", required=True)
    s.add_argument("--odds", default="avg", choices=["avg", "max", "b365"])

    # serve
    s = sub.add_parser("serve", help="启动本地 Web（FastAPI + 静态页）")
    s.add_argument("--port", type=int)

    return p


def main(argv: list[str] | None = None) -> int:
    cfg = _load_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "collect-history":
        return cmd_collect_history(args, cfg)
    if args.cmd == "import-fixtures":
        return cmd_import_fixtures(args, cfg)
    if args.cmd == "map-fixtures":
        return cmd_map_fixtures(args, cfg)
    if args.cmd == "data-health":
        return cmd_data_health(args, cfg)
    if args.cmd == "check-draw":
        return cmd_check_draw(args, cfg)
    if args.cmd == "backtest":
        # 重定向到 backend.backtest 模块的简单 runner
        if args.model == "market":
            from football_lottery.backtest import baseline
            s = baseline.run(args.db if hasattr(args, "db") else "data/football.db",
                              args.start, odds_source=args.odds,
                              out_path=f"data/reports/baseline_market_{args.odds}.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        if args.model == "dc":
            from football_lottery.backtest import dc as _dc
            s = _dc.run("data/football.db", args.start,
                        out_path=f"data/reports/baseline_dc.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        parser.print_help()
        return 1
    if args.cmd == "serve":
        return cmd_serve(args, cfg)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
