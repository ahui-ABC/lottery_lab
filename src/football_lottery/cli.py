"""足彩预测工具 CLI 入口。

子命令按阶段陆续添加：`collect-history` / `collect-period` / `collect-draws` /
`import-fixtures` / `map-fixtures` / `data-health` / `train` / `backtest` /
`backtest-plans` / `check-draw` / `serve`。
"""
import argparse
import io
import os
import sys
import time
from datetime import date, datetime
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
            if any(k in j for k in ("jc", "avg", "b365", "max")):
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
    current_row = conn.execute(
        "SELECT id, period_no FROM periods WHERE status='current' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    current_period = current_row["period_no"] if current_row else None
    current_missing_odds = 0
    if current_row:
        for r in conn.execute(
            "SELECT odds_json FROM period_matches WHERE period_id=?", (current_row["id"],)
        ):
            odds = r["odds_json"]
            if not odds:
                current_missing_odds += 1
                continue
            try:
                j = _json.loads(odds)
            except Exception:
                j = {}
            if not any(k in j for k in ("jc", "avg", "b365", "max")):
                current_missing_odds += 1
    out = {
        "matches_total": total,
        "matches_with_odds": with_odds,
        "odds_missing_rate": odds_missing_rate,
        "by_league": by_league,
        "periods_total": conn.execute("SELECT COUNT(*) FROM periods").fetchone()[0],
        "unmapped_fixtures": periods_unmapped,
        "unconfirmed_aliases": unconfirmed,
        "last_match_date": last_match,
        "current_period": current_period,
        "current_period_missing_odds": current_missing_odds,
    }
    return out


def cmd_collect_history(args, cfg: dict) -> int:
    from football_lottery.collectors import fd
    seasons = args.seasons or cfg.get("seasons", ["2122"])
    divisions = args.divisions or cfg.get("divisions", ["E0"])
    conn = _connect(cfg)
    n = fd.fetch_and_collect(conn, seasons, divisions, silent=False,
                             force=args.force)
    print(f"合计入库 {n} 条（分赛季联赛见上方输出）")
    return 0


def cmd_collect_period(args, cfg: dict) -> int:
    """采集当期对阵：官方接口取期号 + 14 场对阵并入库。"""
    if args.file:
        return cmd_import_fixtures(args, cfg)
    from football_lottery.collectors import sporttery
    conn = _connect(cfg)
    try:
        current = sporttery.fetch_current_period()
        if not current or not current.get("period_no"):
            print("当前无在售胜负彩期次；可用 --file 导入 CSV。", file=sys.stderr)
            return 2
        detail = sporttery.fetch_period_detail(current["period_no"])
        parsed = sporttery.parse_period(detail, status="current")
        if not parsed["period"].get("sale_end"):
            parsed["period"]["sale_end"] = current.get("sale_end")
        period_id = sporttery.upsert_period(conn, parsed, demote_others=True)
    except sporttery.CollectorError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "period_no": parsed["period"]["period_no"],
        "period_id": period_id,
        "fixtures": len(parsed["fixtures"]),
        "sale_end": parsed["period"].get("sale_end"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_collect_draws(args, cfg: dict) -> int:
    """采集近 N 年历史开奖（对阵 + 赛果 + 奖金）并入库。"""
    if args.file:
        return cmd_import_fixtures(args, cfg)
    from football_lottery.collectors import sporttery
    conn = _connect(cfg)
    years = getattr(args, "years", None) or 4
    try:
        out = sporttery.collect_history(conn, years=years)
    except sporttery.CollectorError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "years": years,
        "periods_saved": out["periods_saved"],
        "skipped": out["skipped"],
    }, ensure_ascii=False, indent=2))
    return 0


def _collect_odds_once(conn, sporttery) -> int:
    """拉一次竞彩赔率并写快照。返回进程退出码（watch 模式下忽略）。"""
    # 先确保当期对阵已入库：新期次开卖时自动跟上，无需人工跑 collect-period
    ensured = sporttery.ensure_current_period(conn)
    if ensured is None:
        print("当前无在售胜负彩期次，跳过本轮", file=sys.stderr, flush=True)
        return 2
    period_no = ensured["period_no"]
    if ensured["created"]:
        print(f"[{period_no}] 检测到新期次，已抓取 {ensured['fixtures']} 场对阵",
              flush=True)

    jc_odds = sporttery.parse_jc_odds(sporttery.fetch_jc_odds())
    if not jc_odds:
        # flush：watch 模式常被重定向到日志文件，不 flush 会一直看不到输出
        print(f"[{period_no}] 当日无竞彩在售比赛（休赛日？），跳过本轮", flush=True)
        return 0

    matched = sporttery.match_to_period(conn, period_no, jc_odds)
    saved = sporttery.save_odds_snapshots(conn, period_no, jc_odds)
    print(json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "period_no": period_no,
        "jc_matches": len(jc_odds),
        "matched": matched["matched"],
        "unmatched_seq": matched["unmatched"],
        "snapshots_added": saved["changed"],
        "unchanged": saved["skipped"],
    }, ensure_ascii=False), flush=True)
    return 0


def _redirect_output_to(path: str, max_bytes: int = 5 * 1024 * 1024) -> None:
    """把 stdout/stderr 追加到日志文件；超过上限先轮转一份。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > max_bytes:
        rotated = Path(str(p) + ".1")
        if rotated.exists():
            rotated.unlink()
        p.rename(rotated)
    handle = open(p, "a", encoding="utf-8", buffering=1)
    sys.stdout = handle
    sys.stderr = handle


# 采集进程里每日自动跑 daily-jc 的起始小时（24 小时制）。
# 设在下半天是因为：早于此赔率多为初盘、定价不充分；且 predict-jc 只处理
# 当时仍在售的比赛，跑得太早会漏掉当天晚些上架的场次。
DAILY_JC_HOUR = 14


def _maybe_run_daily_jc(conn, cfg: dict) -> None:
    """watch 进程里每天跑一次 daily-jc（当天已有方案则跳过）。

    判定「已跑过」用 `jc_parlay_plans` 里是否已有当天的方案 —— 不额外建状态表。
    失败只告警不中断采集：采集是本进程的主职。
    """
    from types import SimpleNamespace

    now = datetime.now()
    if now.hour < DAILY_JC_HOUR:
        return
    today = now.date().isoformat()
    if conn.execute("SELECT 1 FROM jc_parlay_plans WHERE plan_date=?",
                    (today,)).fetchone():
        return

    print(f"[daily-jc] {today} 尚无方案，开始生成…", flush=True)
    try:
        cmd_daily_jc(SimpleNamespace(workers=8, delay=0.02, pool=None), cfg)
    except Exception as exc:                       # noqa: BLE001 - 不能让采集挂掉
        print(f"[daily-jc] 失败（下一轮重试）：{exc}", file=sys.stderr, flush=True)


# 数字彩的刷新间隔（小时）。不设固定钟点：机器在该跑的时候关机，
# 固定钟点会整整漏掉那一天，改成「距上次跑够久就跑」下次开机自动补上。
LOTTERY_REFRESH_HOURS = 12


def cmd_daily_lottery(args, cfg: dict) -> int:
    """数字彩一条龙：刷新最新开奖 → 预测下一期 → 给已开奖的预测对奖。

    三步都幂等，重复跑只会刷新而不会重复记账。
    """
    from types import SimpleNamespace

    from football_lottery.collectors import lottery_history as lh
    from football_lottery.models import lottery_predict as lp

    conn = _connect(cfg)
    picks = list(lh.LOTTERIES) if args.lottery == "all" else args.lottery.split(",")
    strategies = (lp.STRATEGIES if args.strategy == "all"
                  else tuple(args.strategy.split(",")))
    try:
        for lottery in picks:
            name = lh.LOTTERY_NAMES[lottery]
            try:
                lh.refresh_latest(conn, lottery)
                last = lh.latest_issue(conn, lottery)
            except lh.RateLimited as exc:
                print(f"{name}：接口限流，跳过（{exc}）", file=sys.stderr)
                continue
            if not last:
                print(f"{name}：库内无数据，先跑 collect-lottery")
                continue
            target = lp.next_issue(last)
            written = lp.save_predictions(conn, lottery, target, strategies,
                                          args.bets, args.window, args.seed)
            print(f"{name}：最新 {last}，已存第 {target} 期推荐（{written} 条策略）")
    finally:
        lh.close_client()

    cmd_score_lottery(SimpleNamespace(), cfg)
    return 0


def _maybe_run_daily_lottery(conn, cfg: dict) -> None:
    """watch 进程里定期跑 daily-lottery。

    判据是「上次预测距今是否超过 LOTTERY_REFRESH_HOURS」—— 用 created_at 当
    运行时间戳，不额外建状态表。失败只告警不中断采集：采集是本进程的主职。
    """
    from datetime import timedelta

    last = conn.execute(
        "SELECT MAX(created_at) m FROM lottery_prediction").fetchone()["m"]
    if last:
        try:
            if datetime.now() - datetime.fromisoformat(last) < \
                    timedelta(hours=LOTTERY_REFRESH_HOURS):
                return
        except ValueError:
            pass                       # 时间戳坏了就跑一次，别卡死在这里

    print(f"[daily-lottery] 距上次已超过 {LOTTERY_REFRESH_HOURS} 小时，开始…",
          flush=True)
    from types import SimpleNamespace

    try:
        cmd_daily_lottery(SimpleNamespace(lottery="all", strategy="all", bets=5,
                                          window=100, seed=20260921), cfg)
    except Exception as exc:                       # noqa: BLE001 - 不能让采集挂掉
        print(f"[daily-lottery] 失败（下一轮重试）：{exc}", file=sys.stderr,
              flush=True)


def cmd_collect_odds(args, cfg: dict) -> int:
    """采集竞彩胜平负赔率；--watch 时按固定间隔轮询并只在赔率变化时追加快照。"""
    from football_lottery.collectors import sporttery
    import time as _time

    log_path = getattr(args, "log", None)
    if log_path:
        _redirect_output_to(log_path)

    conn = _connect(cfg)
    interval = int(args.interval or 600)

    if not getattr(args, "watch", False):
        try:
            return _collect_odds_once(conn, sporttery)
        except sporttery.CollectorError as exc:
            print(f"采集失败：{exc}", file=sys.stderr)
            return 2

    # watch 是常驻进程：只允许一个实例，避免自启与手动启动叠加导致重复采集
    from football_lottery import daemon_ctl

    lock_path = cfg.get("odds_lock_path") or daemon_ctl.LOCK_PATH
    if not daemon_ctl.acquire_singleton(Path(lock_path)):
        print(f"已有采集进程在运行（锁 {lock_path}），本次退出。", file=sys.stderr, flush=True)
        return 0

    print(f"watch 模式：每 {interval} 秒采集一次，赔率有变化才追加快照。Ctrl+C 停止。"
          f"（pid={os.getpid()}）", flush=True)
    try:
        while True:
            try:
                _collect_odds_once(conn, sporttery)
            except sporttery.CollectorError as exc:
                # watch 模式不因单次失败退出
                print(f"采集失败（下轮重试）：{exc}", file=sys.stderr, flush=True)
            _maybe_run_daily_jc(conn, cfg)
            _maybe_run_daily_lottery(conn, cfg)
            _time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止 watch。", flush=True)
        return 0


def cmd_collect_jc_history(args, cfg: dict) -> int:
    """回填竞彩历史：比赛列表 + 5 玩法赔率变化 + 开奖结果。"""
    from football_lottery.collectors import jc_history, sporttery

    if args.force and args.retry_failed:
        print("--force 与 --retry-failed 互斥。", file=sys.stderr)
        return 2

    conn = _connect(cfg)
    if args.log:
        _redirect_output_to(args.log)

    done = {"n": 0}

    def _progress(result):
        if result.get("breaker"):
            print(f"[熔断] 失败率 {result['rate']:.0%}，并发降至 {result['workers']}", flush=True)
            return
        done["n"] += 1
        print(f"[{result['date']}] 比赛 {result['matches']:>3} 场，"
              f"赔率行 {result['odds_rows']:>5}，失败 {result['failed']}", flush=True)

    try:
        out = jc_history.sync_range(
            conn,
            from_date=args.from_date or jc_history.DEFAULT_FROM,
            to_date=args.to_date,
            workers=args.workers,
            delay=args.delay,
            page_size=args.page_size,
            refresh_days=args.refresh_days,
            force=args.force,
            retry_failed=args.retry_failed,
            progress=_progress,
        )
    except jc_history.CircuitBreakerTripped as exc:
        print(f"已中止：{exc}", file=sys.stderr)
        return 2
    except sporttery.CollectorError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["failed"] == 0 else 1


def _match_meta_from_calculator(payload: dict) -> list[dict]:
    """`getMatchCalculatorV1` 的 payload → jc_matches 行（只取基础信息，不含赛果）。"""
    rows = []
    for group in (payload.get("value") or {}).get("matchInfoList") or []:
        for item in group.get("subMatchList") or []:
            match_id = item.get("matchId")
            if not match_id:
                continue
            had = item.get("had") or {}
            rows.append({
                "match_id": int(match_id),
                "match_date": (item.get("matchDate") or "")[:10] or None,
                "match_num": item.get("matchNumStr") or item.get("matchNum"),
                "league_id": item.get("leagueId"),
                # getMatchCalculatorV1 用的是 leagueAllName/leagueAbbName，
                # 而 getUniformMatchResultV1 用的是 leagueName —— 字段名不同
                "league_name": (item.get("leagueAllName") or item.get("leagueAbbName")
                                or item.get("leagueName")),
                "home_team": item.get("homeTeamAllName") or item.get("homeTeamAbbName"),
                "away_team": item.get("awayTeamAllName") or item.get("awayTeamAbbName"),
                "home_team_id": item.get("homeTeamId"),
                "away_team_id": item.get("awayTeamId"),
                "had_h": _num_or_none(had.get("h")),
                "had_d": _num_or_none(had.get("d")),
                "had_a": _num_or_none(had.get("a")),
                "goal_line": item.get("goalLine") or None,
            })
    return rows


def _num_or_none(value):
    try:
        return float(str(value).strip()) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def cmd_predict_jc(args, cfg: dict) -> int:
    """对竞彩比赛预测并存档。默认拉当期实时数据；--date 走重放（读库不联网）。"""
    from football_lottery.collectors import jc_history, sporttery
    from football_lottery.models import jc_predict
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = _connect(cfg)
    day = args.date or date.today().isoformat()

    if args.date:
        # 重放：只用已存档的赔率，不受接口时效影响
        ids = jc_predict.match_ids_on(conn, day)
        if not ids:
            print(f"{day} 无已存档的赔率数据；先跑 collect-jc-history 或 collect-odds。",
                  file=sys.stderr)
            return 2
        total = 0
        for mid in ids:
            total += jc_predict.predict_match(
                conn, mid, jc_predict.load_odds_by_pool(conn, mid), day)
        print(json.dumps({"mode": "replay", "date": day, "matches": len(ids),
                          "rows": total}, ensure_ascii=False))
        return 0

    payload = sporttery.fetch_jc_odds()
    match_ids = sporttery.parse_jc_match_ids(payload)
    if not match_ids:
        print("当期无在售竞彩比赛（休赛日？）")
        return 0

    # 当期比赛的 matchId 未必在 jc_matches 里：回填按**比赛日**取数
    # （getUniformMatchResultV1），而这里是**销售日**（getMatchCalculatorV1），
    # 两者集合不同。缺了基础信息，score-jc 就查不到队名与赛果。
    jc_history.upsert_match_rows(conn, _match_meta_from_calculator(payload))

    rows = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(sporttery.fetch_fixed_bonus, mid): mid
                   for mid in match_ids}
        for future in as_completed(futures):
            mid = futures[future]
            try:
                value = future.result()
            except sporttery.CollectorError:
                failed += 1
                continue
            if not value.get("oddsHistory"):
                continue
            if args.delay:
                time.sleep(args.delay)
            # 同时落盘赔率序列：否则 hhad 的让球线等信息丢失，
            # 页面上无法说明"让了几球"（曾因此让用户误以为推荐自相矛盾）
            jc_history.save_odds_series(conn, mid, value)
            rows += jc_predict.predict_match(
                conn, mid, jc_predict.odds_by_pool_from_value(value), day)

    print(json.dumps({"mode": "live", "date": day, "matches": len(match_ids),
                      "rows": rows, "failed": failed}, ensure_ascii=False))
    return 0


def cmd_plan_jc(args, cfg: dict) -> int:
    """生成竞彩串关方案：按市场概率选场 + 多重串关组合。

    选场规则与场次数均来自回测结论（见 models/jc_parlay.py 的模块说明）。
    依赖同日的 jc_predictions（先跑 predict-jc）。
    """
    from football_lottery.models import jc_parlay

    conn = _connect(cfg)
    day = args.date or date.today().isoformat()

    pools = jc_parlay.DEFAULT_POOL_CONFIG
    if args.pool:
        if args.pool not in pools:
            print(f"未知玩法 {args.pool}；可选：{', '.join(pools)}", file=sys.stderr)
            return 2
        pools = {args.pool: pools[args.pool]}

    from football_lottery.models import jc_predict

    made = []
    for pool, n_legs in pools.items():
        plan = jc_parlay.build_plan(
            conn, day, pool, n_legs,
            unit=args.unit or jc_parlay.DEFAULT_UNIT,
            min_combo=args.min_combo or jc_parlay.DEFAULT_MIN_COMBO)
        if plan is None:
            made.append({"pool": jc_predict.pool_label(pool), "skipped": f"可用场次不足 {n_legs}"})
            continue
        plan_id = jc_parlay.save_plan(conn, plan)
        made.append({
            "pool": jc_predict.pool_label(pool), "plan_id": plan_id,
            "legs": len(plan["legs"]), "bets": len(plan["bets"]),
            "invested": len(plan["bets"]) * plan["unit"],
            "picks": [
                {"match": f"{x['home']} vs {x['away']}",
                 "pick": jc_predict.option_label(pool, x["pick"]),
                 "odds": x["odds"], "prob": round(x["prob"] or 0, 4)}
                for x in plan["legs"]
            ],
        })
    print(json.dumps({"date": day, "plans": made}, ensure_ascii=False, indent=2))
    return 0


def cmd_daily_jc(args, cfg: dict) -> int:
    """竞彩每日一条龙：预测 → 生成串关方案 → 对未开奖的方案对奖。

    供定时任务调用。「先跑几期看看」用它最省事 —— 只需每天跑一次。

    时序说明：`predict-jc` 只处理**当时仍在售**的比赛（接口只返回可投注的），
    因此不存在"用已结束比赛的结果反推"的前视偏差；越接近截止赔率越准，
    所以建议固定在每天下午跑一次。
    """
    from types import SimpleNamespace

    print("=" * 60, flush=True)
    print("[1/3] 预测当期", flush=True)
    rc1 = cmd_predict_jc(
        SimpleNamespace(date=None, workers=args.workers, delay=args.delay), cfg)

    print("[2/3] 生成串关方案", flush=True)
    rc2 = cmd_plan_jc(
        SimpleNamespace(date=None, pool=getattr(args, "pool", None),
                        unit=None, min_combo=None), cfg)

    print("[3/3] 对奖（含昨日方案）", flush=True)
    rc3 = cmd_score_jc(SimpleNamespace(date=None), cfg)

    from football_lottery.models import jc_parlay
    conn = _connect(cfg)
    print(json.dumps({"daily_jc": "done", "summary": jc_parlay.summary(conn)},
                     ensure_ascii=False, indent=2), flush=True)
    return 0 if (rc1 == 0 and rc2 == 0 and rc3 == 0) else 1


def cmd_jc_summary(args, cfg: dict) -> int:
    """竞彩串关方案的历史盈亏。"""
    from football_lottery.models import jc_parlay

    conn = _connect(cfg)
    out = jc_parlay.summary(conn, pool=args.pool)
    if args.recent:
        out["recent"] = jc_parlay.recent_plans(conn, limit=args.recent)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_score_jc(args, cfg: dict) -> int:
    """对未对奖的竞彩预测打分。优先读已存档赛果，缺失才联网补拉。"""
    from football_lottery.collectors import jc_history, sporttery
    from football_lottery.models import jc_predict

    conn = _connect(cfg)

    # 已有存档赛果的先对（不联网）
    out = jc_predict.score_pending(conn, predicted_on=args.date)

    # 仍未对奖、且 jc_matches 里也没有赛果的 → 联网补拉
    where = "p.scored_at IS NULL AND p.pick IS NOT NULL"
    params: list = []
    if args.date:
        where += " AND p.predicted_on=?"
        params.append(args.date)

    need = [r["match_id"] for r in conn.execute(
        f"""SELECT DISTINCT p.match_id FROM jc_predictions p
            LEFT JOIN jc_matches m ON m.match_id = p.match_id
            WHERE {where}
              AND m.result_had IS NULL AND m.result_hhad IS NULL
              AND m.result_crs IS NULL AND m.result_ttg IS NULL
              AND m.result_hafu IS NULL""", params)]

    fetched = {}
    for mid in need:
        try:
            value = sporttery.fetch_fixed_bonus(mid)
        except sporttery.CollectorError:
            continue
        parsed = jc_history.parse_fixed_bonus(value)
        if parsed["results"]:
            fetched[mid] = parsed["results"]
            columns = ", ".join(f"{k}=?" for k in parsed["results"])
            conn.execute(f"UPDATE jc_matches SET {columns} WHERE match_id=?",
                         [*parsed["results"].values(), mid])
    if fetched:
        conn.commit()
        out2 = jc_predict.score_pending(conn, predicted_on=args.date, pending=fetched)
        out = {"scored": out["scored"] + out2["scored"],
               "skipped": out2["skipped"]}

    # 串关方案一并对奖（赛果未出的会被跳过）
    from football_lottery.models import jc_parlay
    plans_out = jc_parlay.score_plans(conn, day=args.date)

    print(json.dumps({"date": args.date, **out, "parlay_plans": plans_out,
                      "summary": jc_predict.summary_by_method(conn)},
                     ensure_ascii=False, indent=2))
    return 0


def _format_pick(lottery: str, pick: dict) -> str:
    if "front" in pick:
        return " ".join(pick["front"]) + "  +  " + " ".join(pick["back"])
    return "".join(pick["digits"])


def cmd_collect_lottery(args, cfg: dict) -> int:
    from football_lottery.collectors import lottery_history as lh

    conn = _connect(cfg)
    picks = list(lh.LOTTERIES) if args.lottery == "all" else args.lottery.split(",")
    unknown = [p for p in picks if p not in lh.LOTTERIES]
    if unknown:
        print(f"未知彩种：{','.join(unknown)}；可选 {','.join(lh.LOTTERIES)}")
        return 2
    blocked = False
    try:
        for lottery in picks:
            name = lh.LOTTERY_NAMES[lottery]

            def _progress(pages, stats, _name=name):
                print(f"  {_name} 第 {pages} 页：入库 {stats['saved']} "
                      f"刷新 {stats['skipped']}", flush=True)

            stats = lh.sync_history(conn, lottery, since_year=args.year_from,
                                    rate=args.rate, full=args.full,
                                    on_progress=_progress)
            # 范围要从库里查，不能用 stats["oldest"] —— 增量模式下那只是
            # 「本次抓到的最早一期」，会被误读成「库里最早的一期」
            total, lo, hi = conn.execute(
                "SELECT COUNT(*), MIN(issue), MAX(issue) FROM lottery_draw "
                "WHERE lottery=?", (lottery,)).fetchone()
            tail = "；已是最新，提前停止（补历史缺口用 --full）" \
                if stats["stopped_early"] else ""
            print(f"{name}：新增 {stats['saved']}，刷新 {stats['skipped']}，"
                  f"本次翻 {stats['pages']} 页{tail}；库内 {total} 期（{lo} ~ {hi}）")
            if stats["blocked"]:
                blocked = True
                print("  ⚠ 接口返回 403/429，本轮已中止。"
                      "隔一会儿再跑本命令即可续传；必要时用 --rate 调低速率。")
                break
    finally:
        lh.close_client()
    return 1 if blocked else 0


def cmd_predict_lottery(args, cfg: dict) -> int:
    from football_lottery.collectors import lottery_history as lh
    from football_lottery.models import lottery_predict as lp

    conn = _connect(cfg)
    picks = list(lh.LOTTERIES) if args.lottery == "all" else args.lottery.split(",")
    strategies = (lp.STRATEGIES if args.strategy == "all"
                  else tuple(args.strategy.split(",")))
    try:
        for lottery in picks:
            lh.refresh_latest(conn, lottery)   # 先刷最新，否则会预测到已开过的期号
            last = lh.latest_issue(conn, lottery)
            if not last:
                print(f"{lh.LOTTERY_NAMES[lottery]}：库内无数据，先跑 collect-lottery")
                continue
            target = lp.next_issue(last)
            lp.save_predictions(conn, lottery, target, strategies,
                                args.bets, args.window, args.seed)
            print(f"\n== {lh.LOTTERY_NAMES[lottery]} 第 {target} 期推荐"
                  f"（每注 {lp.BET_PRICE} 元，已存库）==")
            rows = conn.execute(
                """SELECT strategy, bets FROM lottery_prediction
                   WHERE lottery=? AND target_issue=? ORDER BY strategy""",
                (lottery, target)).fetchall()
            for row in rows:
                label = lp.STRATEGY_LABELS.get(row["strategy"], row["strategy"])
                for i, bet in enumerate(json.loads(row["bets"]), 1):
                    print(f"  {label:<4} 第{i}注  {_format_pick(lottery, bet)}")
    finally:
        lh.close_client()
    return 0


def cmd_score_lottery(args, cfg: dict) -> int:
    from football_lottery.db import store
    from football_lottery.models import lottery_backtest as lb

    conn = _connect(cfg)
    pending = store.fetchall(conn, """
        SELECT p.lottery, p.target_issue, p.strategy, p.bets, d.numbers, d.prizes
        FROM lottery_prediction p JOIN lottery_draw d
          ON d.lottery = p.lottery AND d.issue = p.target_issue
        WHERE p.prize IS NULL""")
    for row in pending:
        bets = json.loads(row["bets"])
        drawn = json.loads(row["numbers"])
        prizes = json.loads(row["prizes"]) if row["prizes"] else None
        total = sum(lb.prize_for(row["lottery"], b, drawn, prizes) for b in bets)
        hits = [lb.hits_for(row["lottery"], b, drawn) for b in bets]
        # 只更新对奖列。不要走 store.upsert —— 那会把 created_at 一起覆盖掉
        conn.execute(
            """UPDATE lottery_prediction SET hits=?, prize=?
               WHERE lottery=? AND target_issue=? AND strategy=?""",
            (json.dumps(hits, ensure_ascii=False), total, row["lottery"],
             row["target_issue"], row["strategy"]))
    conn.commit()
    print(f"已对奖 {len(pending)} 条预测")
    return 0


def cmd_backtest_lottery(args, cfg: dict) -> int:
    from football_lottery.collectors import lottery_history as lh
    from football_lottery.models import lottery_backtest as lb
    from football_lottery.models import lottery_predict as lp

    conn = _connect(cfg)
    picks = list(lh.LOTTERIES) if args.lottery == "all" else args.lottery.split(",")
    strategies = (lp.STRATEGIES if args.strategy == "all"
                  else tuple(args.strategy.split(",")))
    for lottery in picks:
        draws = lb.load_draws(conn, lottery)
        if len(draws) < lb.MIN_HISTORY + 30:
            print(f"{lh.LOTTERY_NAMES[lottery]}：数据不足"
                  f"（{len(draws)} 期，至少需要 {lb.MIN_HISTORY + 30}）")
            continue
        result = lb.run_backtest(draws, lottery, strategies,
                                 window=args.window, n_bets=args.bets, seed=args.seed)
        print()
        print(lb.summarize(result))
        lb.save_result(conn, result)
    return 0


def cmd_train(args, cfg: dict) -> int:
    from football_lottery.models import pipeline
    conn = _connect(cfg)
    out = pipeline.train_gbdt(
        conn,
        models_dir=args.models_dir or cfg.get("models_dir", "data/models"),
        use_lightgbm=bool(cfg.get("use_lightgbm", True)),
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_backtest_plans(args, cfg: dict) -> int:
    from football_lottery.backtest import plans
    out = plans.run(
        db_path=args.db or cfg.get("db_path", "data/football.db"),
        start=args.start,
        budget=args.budget or int(cfg.get("budget_per_game", 64)),
        out_path=args.out,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
            elif plan["game_type"] == "r9":
                if isinstance(legs, dict):
                    selected = legs.get("selected", [])
                    selected_legs = legs.get("legs", [])
                    selected_results = [results[i] for i in selected
                                       if isinstance(i, int) and 0 <= i < len(results)]
                else:
                    selected_legs = legs
                    selected_results = results[:len(legs)]
                hit = W.r9_hit(selected_legs, selected_results)
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
    p = argparse.ArgumentParser(prog="football_lottery",
                                description="足彩与数字彩分析工具")
    sub = p.add_subparsers(dest="cmd")

    # collect-history
    s = sub.add_parser("collect-history", help="联网下载 football-data 历史（5 赛季 × 9 联赛）")
    s.add_argument("--seasons", nargs="+", help="如 2122 2223 ... 2526")
    s.add_argument("--divisions", nargs="+", help="如 E0 D1 I1 ...")
    s.add_argument("--force", action="store_true",
                   help="已入库的过去赛季也重下（默认跳过，只有当前赛季每次重下）")

    # collect-period / collect-draws
    s = sub.add_parser("collect-period", help="获取当期对阵；无网络时用 CSV 兜底")
    s.add_argument("--file", help="本地期次 CSV（与 import-fixtures 格式相同）")
    s = sub.add_parser("collect-draws", help="获取历史开奖；无网络时用 CSV 兜底")
    s.add_argument("--file", help="本地开奖 CSV（与 import-fixtures 格式相同）")
    s.add_argument("--years", type=int, default=4, help="回溯年数；默认 4 年")

    # predict-jc / score-jc
    s = sub.add_parser("predict-jc", help="对竞彩比赛预测并存档（各路径并行记录）")
    s.add_argument("--date", help="重放模式：用已存档赔率补跑该日，不联网")
    s.add_argument("--workers", type=int, default=8, help="并发线程数")
    s.add_argument("--delay", type=float, default=0.05, help="每个 worker 的请求间隔（秒）")

    s = sub.add_parser("score-jc", help="对未对奖的竞彩预测打分（含串关方案）")
    s.add_argument("--date", help="只对指定预测日的")

    # plan-jc / jc-summary
    s = sub.add_parser("plan-jc", help="生成竞彩串关方案（按市场概率选场 + 多重组合）")
    s.add_argument("--date", help="指定日期；默认今天")
    s.add_argument("--pool", help="只做某个玩法（默认全部：hhad/crs/hafu）")
    s.add_argument("--unit", type=float, help="每注金额（元）；默认 2")
    s.add_argument("--min-combo", type=int, dest="min_combo", help="最小串关数；默认 2")

    s = sub.add_parser("daily-jc", help="竞彩每日一条龙：预测 + 生成方案 + 对奖")
    s.add_argument("--workers", type=int, default=8, help="预测并发线程数")
    s.add_argument("--delay", type=float, default=0.02, help="每个 worker 的请求间隔（秒）")
    s.add_argument("--pool", help="只做某个玩法")

    s = sub.add_parser("jc-summary", help="竞彩串关方案的历史盈亏")
    s.add_argument("--pool", help="只统计某个玩法")
    s.add_argument("--recent", type=int, help="附带最近 N 条方案明细")

    # collect-jc-history
    s = sub.add_parser("collect-jc-history", help="回填竞彩历史（比赛列表 + 5 玩法赔率变化）")
    s.add_argument("--from", dest="from_date", help="起始日期 YYYY-MM-DD；默认 2021-01-01")
    s.add_argument("--to", dest="to_date", help="结束日期 YYYY-MM-DD；默认今天")
    s.add_argument("--workers", type=int, default=4, help="并发线程数；1 = 串行")
    s.add_argument("--delay", type=float, default=0.2, help="每个 worker 的请求间隔（秒）")
    s.add_argument("--page-size", type=int, default=100, help="列表接口分页大小")
    s.add_argument("--refresh-days", type=int, default=1,
                   help="最近 N 天总是重跑（锚定 --to）；默认 1")
    s.add_argument("--force", action="store_true", help="忽略完成标记，全部重跑")
    s.add_argument("--retry-failed", action="store_true", help="只重跑失败过的日期")
    s.add_argument("--log", help="把输出追加到该日志文件")

    # collect-odds
    s = sub.add_parser("collect-odds", help="采集竞彩胜平负赔率（盘口变化快照）")
    s.add_argument("--watch", action="store_true", help="按间隔循环采集，只在赔率变化时追加")
    s.add_argument("--interval", type=int, default=600, help="watch 模式间隔秒数；默认 600")
    s.add_argument("--log", help="把输出追加到该日志文件（超过 5MB 自动轮转一份）")

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
    s.add_argument("--model", default="market", choices=["market", "dc", "gbdt", "fused"])
    s.add_argument("--start", required=True)
    s.add_argument("--odds", default="avg", choices=["avg", "max", "b365"])
    s.add_argument("--db", default="data/football.db")
    s.add_argument("--refit-days", type=int, default=30)
    s.add_argument("--max-train", type=int, default=1500)
    s.add_argument("--out")

    # train
    s = sub.add_parser("train", help="训练并保存本地 GBDT 模型")
    s.add_argument("--models-dir", default="data/models")
    s.add_argument("--n-estimators", type=int, default=200)
    s.add_argument("--learning-rate", type=float, default=0.05)

    # plan backtest
    s = sub.add_parser("backtest-plans", help="按历史期次回测方案与赔率方案")
    s.add_argument("--db", help="SQLite 数据库路径；默认读取 config.yaml")
    s.add_argument("--start")
    s.add_argument("--budget", type=int)
    s.add_argument("--out", default="data/reports/plan_backtest.json")

    # collect-lottery
    s = sub.add_parser("collect-lottery",
                       help="同步数字彩开奖（大乐透/双色球/排列三/排列五/福彩3D）")
    s.add_argument("--lottery", default="all", help="all 或逗号分隔，如 dlt,ssq")
    s.add_argument("--from", dest="year_from", type=int, default=2020,
                   help="只取这一年起的；默认 2020")
    s.add_argument("--rate", type=float, default=None,
                   help="每秒请求上限；默认 5。被限流时调低它")
    s.add_argument("--full", action="store_true",
                   help="扫完全部年份而非碰到已入库就停（用于补历史缺口）")
    s.set_defaults(func=cmd_collect_lottery)

    # predict-lottery
    s = sub.add_parser("predict-lottery", help="对下一期生成各策略推荐号码")
    s.add_argument("--lottery", default="all")
    s.add_argument("--strategy", default="all")
    s.add_argument("--bets", type=int, default=5)
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--seed", type=int, default=20260921)
    s.set_defaults(func=cmd_predict_lottery)

    # score-lottery
    s = sub.add_parser("score-lottery", help="给已开奖的数字彩预测回填命中与奖金")
    s.set_defaults(func=cmd_score_lottery)

    # daily-lottery
    s = sub.add_parser("daily-lottery",
                       help="数字彩一条龙：刷新开奖 + 预测下一期 + 对奖")
    s.add_argument("--lottery", default="all")
    s.add_argument("--strategy", default="all")
    s.add_argument("--bets", type=int, default=5)
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--seed", type=int, default=20260921)
    s.set_defaults(func=cmd_daily_lottery)

    # backtest-lottery
    s = sub.add_parser("backtest-lottery", help="数字彩逐期走查回测与配对显著性")
    s.add_argument("--lottery", default="all")
    s.add_argument("--strategy", default="all")
    s.add_argument("--bets", type=int, default=5)
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--seed", type=int, default=20260921)
    s.set_defaults(func=cmd_backtest_lottery)

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
    if args.cmd == "collect-period":
        return cmd_collect_period(args, cfg)
    if args.cmd == "collect-draws":
        return cmd_collect_draws(args, cfg)
    if args.cmd == "collect-odds":
        return cmd_collect_odds(args, cfg)
    if args.cmd == "collect-jc-history":
        return cmd_collect_jc_history(args, cfg)
    if args.cmd == "predict-jc":
        return cmd_predict_jc(args, cfg)
    if args.cmd == "score-jc":
        return cmd_score_jc(args, cfg)
    if args.cmd == "plan-jc":
        return cmd_plan_jc(args, cfg)
    if args.cmd == "jc-summary":
        return cmd_jc_summary(args, cfg)
    if args.cmd == "daily-jc":
        return cmd_daily_jc(args, cfg)
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
            s = baseline.run(args.db, args.start, odds_source=args.odds,
                              out_path=args.out or f"data/reports/baseline_market_{args.odds}.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        if args.model == "dc":
            from football_lottery.backtest import dc as _dc
            s = _dc.run(args.db, args.start, refit_days=args.refit_days,
                        max_train_matches=args.max_train,
                        out_path=args.out or "data/reports/baseline_dc.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        if args.model == "gbdt":
            from football_lottery.backtest import advanced
            s = advanced.run_gbdt(args.db, args.start, refit_days=args.refit_days,
                                  max_train_matches=args.max_train,
                                  use_lightgbm=bool(cfg.get("use_lightgbm", True)),
                                  out_path=args.out or "data/reports/baseline_gbdt.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        if args.model == "fused":
            from football_lottery.backtest import advanced
            s = advanced.run_fused(args.db, args.start, refit_days=args.refit_days,
                                   max_train_matches=args.max_train,
                                   use_lightgbm=bool(cfg.get("use_lightgbm", True)),
                                   out_path=args.out or "data/reports/baseline_fused.json")
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
    if args.cmd == "train":
        return cmd_train(args, cfg)
    if args.cmd == "backtest-plans":
        return cmd_backtest_plans(args, cfg)
    if args.cmd == "collect-lottery":
        return cmd_collect_lottery(args, cfg)
    if args.cmd == "predict-lottery":
        return cmd_predict_lottery(args, cfg)
    if args.cmd == "score-lottery":
        return cmd_score_lottery(args, cfg)
    if args.cmd == "daily-lottery":
        return cmd_daily_lottery(args, cfg)
    if args.cmd == "backtest-lottery":
        return cmd_backtest_lottery(args, cfg)
    if args.cmd == "serve":
        return cmd_serve(args, cfg)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
