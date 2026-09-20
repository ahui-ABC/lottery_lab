"""体彩 webapi 适配器 + 历史期次 fixture 导入。

- 体彩 webapi（设计 §3.1）当前对未签到会话返回 `E0001/P0001`；live 路径作 best-effort。
- 历史期次对阵（方案层回测 P1 验收的硬前置）走 `cli import-fixtures --file <csv>`，
  CSV 模板：`period_no, seq, home_cn, away_cn, match_time(可选), result(可选),
  first_prize(单注,可选), second_prize(可选), r9_prize(可选)`。
  第一行可选（只在第一条出现）填期号级别的奖金。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sqlite3 as _sql
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from football_lottery.db import store


# ---- live API（官方 webapi，无需认证） -------------------------------------------
#
# 接口清单（2026-09-20 实测确认，均无需签名/登录）：
#   getLottoSaleInfoV1.qry?param=90,0
#       → 当期在售期号与销售截止时间。param 为 "<gameNo>,<type>"，
#         90 = 胜负彩（85 = 超级大乐透，35 = 排列3，与足彩无关）。
#   getFootBallDrawInfoByDrawNumV2.qry?isVerify=1&lotteryGameNum=90&lotteryDrawNum=<期号>
#       → 指定期次详情，value.matchList 为 14 场对阵。
#         lotteryGameNum 与 lotteryDrawNum 缺一返回 P0001。
#   getFootBallDrawInfoV2.qry?isVerify=1&param=90,0
#       → 最新期详情在 value.sfcDetail，期次列表在 value.sfclist。
#   getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=100&isVerify=1&pageNo=<页>
#       → 历史开奖，value.list 每期含 matchList + lotteryDrawResult + 奖金。
#
# 注意：E0001 是"接口不存在/参数不合法"的通用码，不是反爬；
#       接口不提供赔率（matchList[].h/d/a 恒为空）。
BASE_URL = "https://webapi.sporttery.cn/gateway/lottery"
SFC_GAME_NO = "90"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.sporttery.cn/ctzc/kjgg/",
    "Origin": "https://www.sporttery.cn",
    "Accept": "application/json, text/plain, */*",
}


class CollectorError(RuntimeError):
    """官方接口网络失败或返回业务错误码。"""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


_CLIENT = None


def get_client():
    """复用的 httpx.Client（连接池）。

    每次 `httpx.get()` 都会新建 Client，也就意味着每个请求都要重新做
    TCP + TLS 握手 —— 实测单次 0.87s，而复用连接后降到 0.05-0.18s（约 5 倍）。
    并发抓取时这个差距会被放大到数倍的总耗时。

    httpx.Client 是线程安全的，可被多个 worker 共享。
    """
    global _CLIENT
    if _CLIENT is None:
        import httpx

        _CLIENT = httpx.Client(
            headers=_HEADERS, timeout=20, follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
        )
    return _CLIENT


def close_client() -> None:
    """释放连接池（长任务结束后调用；测试也用它清理）。"""
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        finally:
            _CLIENT = None


def _http_get(url: str, params: dict | None, timeout: int = 20):
    """薄封装，便于测试打桩。"""
    return get_client().get(url, params=params, timeout=timeout)


def _get_json(
    path: str,
    params: dict | None = None,
    timeout: int = 20,
    base: str | None = None,
) -> dict:
    """请求官方接口并返回 JSON；任何失败都抛 CollectorError。

    base 可覆盖默认前缀（竞彩足球的端点在 /gateway/uniform/football/ 下）。
    """
    url = f"{base or BASE_URL}/{path}"
    try:
        resp = _http_get(url, params, timeout=timeout)
    except Exception as exc:  # 网络层异常统一收敛
        raise CollectorError(f"请求失败 {path}: {exc}") from exc
    if resp.status_code != 200:
        raise CollectorError(f"HTTP {resp.status_code} {path}")
    try:
        body = resp.json()
    except Exception as exc:
        raise CollectorError(f"响应非 JSON {path}") from exc
    code = body.get("errorCode")
    if code not in (None, "0", 0, ""):
        raise CollectorError(
            f"接口错误 {code} {path}: {body.get('errorMessage')}", error_code=code
        )
    return body


def fetch_current_period() -> dict | None:
    """当期在售期次；无在售返回 None。"""
    body = _get_json("getLottoSaleInfoV1.qry", {"param": f"{SFC_GAME_NO},0"})
    items = body.get("value") or []
    if not items:
        return None
    item = items[0]
    return {
        "period_no": item.get("lotteryDrawNum"),
        "sale_end": item.get("lotterySaleEndtime"),
        "draw_time": item.get("lotteryDrawTime"),
    }


def fetch_period_detail(period_no: str) -> dict:
    """按期号取期次详情。返回的 value 直接是期次详情（无 sfcDetail 外层）。"""
    body = _get_json(
        "getFootBallDrawInfoByDrawNumV2.qry",
        {"isVerify": 1, "lotteryGameNum": SFC_GAME_NO, "lotteryDrawNum": period_no},
    )
    return body.get("value") or {}


def fetch_history_page(page_no: int, page_size: int = 100) -> dict:
    """取一页历史开奖。返回的 value 含 list / total / pages。"""
    body = _get_json(
        "getHistoryPageListV1.qry",
        {
            "gameNo": SFC_GAME_NO,
            "provinceId": 0,
            "pageSize": page_size,
            "isVerify": 1,
            "pageNo": page_no,
        },
    )
    return body.get("value") or {}


# 竞彩足球（胜平负赔率）在另一条网关路径下
JC_BASE_URL = "https://webapi.sporttery.cn/gateway/uniform/football"
# 官网前端 commonV1.js 里写死的客户端标识，getFixedBonusV1 需要它
JC_CLIENT_CODE = "3001"


def fetch_jc_odds() -> dict:
    """当日竞彩足球在售列表（含实时胜平负赔率）。

    注意 value.matchInfoList[].subMatchList 里的 businessDate 是**销售日**，
    不是比赛日：次日凌晨开赛的场次也归入当天的销售日。
    """
    return _get_json(
        "getMatchCalculatorV1.qry",
        {"poolCode": "had", "channel": "c"},
        base=JC_BASE_URL,
    )


def fetch_uniform_match_result(
    begin_date: str,
    end_date: str,
    page_no: int = 1,
    page_size: int = 100,
) -> dict:
    """按日期范围取竞彩比赛列表（含开奖状态）。返回 value（含 matchResult/pages/total）。

    用于历史回填：这是唯一能按日期系统罗列竞彩比赛的接口。
    """
    return _get_json(
        "getUniformMatchResultV1.qry",
        {
            "matchBeginDate": begin_date,
            "matchEndDate": end_date,
            "leagueId": "",
            "pageSize": page_size,
            "pageNo": page_no,
            "isFix": 0,
            "matchPage": 1,
            "pcOrWap": 1,
        },
        base=JC_BASE_URL,
    ).get("value") or {}


def fetch_fixed_bonus(match_id: int) -> dict:
    """取单场比赛的 5 种玩法赔率与变化序列 + 开奖结果。返回 value。

    实测每个玩法的列表长度 = 赔率变化次数（非多盘口）；同一场每玩法只有一个
    goalLine。接口路径见 docs/superpowers/specs/2026-09-20-jc-history-sync-design.md。
    """
    return _get_json(
        "getFixedBonusV1.qry",
        {"clientCode": JC_CLIENT_CODE, "matchId": match_id},
        base=JC_BASE_URL,
    ).get("value") or {}


def _to_float(value) -> float | None:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def parse_jc_odds(payload: dict) -> list[dict]:
    """竞彩响应 → [{home_cn, away_cn, league_cn, h, d, a, update_time}]。

    赔率缺失或非数字的场次直接跳过（不中断整批）。
    队名取全名字段：实测 AllName 命中 14/14，而 AbbName 只有 6/14。
    """
    out: list[dict] = []
    for group in (payload.get("value") or {}).get("matchInfoList") or []:
        for item in group.get("subMatchList") or []:
            had = item.get("had") or {}
            h, d, a = (_to_float(had.get("h")), _to_float(had.get("d")),
                       _to_float(had.get("a")))
            if h is None or d is None or a is None:
                continue
            home = _norm_team_name(item.get("homeTeamAllName"))
            away = _norm_team_name(item.get("awayTeamAllName"))
            if not home or not away:
                continue
            update = " ".join(
                x for x in ((had.get("updateDate") or "").strip(),
                            (had.get("updateTime") or "").strip()) if x
            )
            out.append({
                "home_cn": home,
                "away_cn": away,
                "league_cn": item.get("leagueAbbName") or None,
                "h": h, "d": d, "a": a,
                "update_time": update or None,
            })
    return out


def parse_jc_match_ids(payload: dict) -> list[int]:
    """从当期竞彩列表里取出 matchId，供 getFixedBonusV1 逐场取赔率。

    注意与 `parse_jc_odds` 的区别：后者返回队名与赔率（用于匹配胜负彩期次），
    **不保留 matchId**；这里专门取 ID。
    """
    out: list[int] = []
    for group in (payload.get("value") or {}).get("matchInfoList") or []:
        for item in group.get("subMatchList") or []:
            match_id = item.get("matchId")
            if match_id:
                out.append(int(match_id))
    return out


# ---- 解析层（纯函数） ------------------------------------------------------------
def _norm_team_name(value: str | None) -> str:
    """统一空白：折叠连续空格、去首尾，消除官方数据的全角/填充空格。"""
    return " ".join((value or "").split())


def parse_period(detail: dict, status: str) -> dict:
    """官方期次详情 → {period, fixtures} 入库结构。

    fixtures 长度必须为 14，否则拒绝（避免不完整数据污染 DB）。
    队名取全名字段：实测全名别名命中 20/28，简称仅 16/28。
    """
    period_no = (detail.get("lotteryDrawNum") or "").strip()
    matches = detail.get("matchList") or []
    if len(matches) != 14:
        raise CollectorError(
            f"期次 {period_no or '?'} 对阵数为 {len(matches)}，期望 14，拒绝入库"
        )

    fixtures: list[dict] = []
    for item in matches:
        seq = item.get("matchNum")
        if not isinstance(seq, int) or not (1 <= seq <= 14):
            raise CollectorError(f"期次 {period_no} 场次序号非法: {seq!r}")
        fixtures.append({
            "seq": seq,
            "home_name_cn": _norm_team_name(item.get("masterTeamAllName")),
            "away_name_cn": _norm_team_name(item.get("guestTeamAllName")),
            "match_time": item.get("startTime") or None,
            "league_cn": item.get("matchName") or None,
        })
    fixtures.sort(key=lambda f: f["seq"])

    return {
        "period": {
            "period_no": period_no,
            "draw_date": (detail.get("lotteryDrawTime") or "")[:10] or None,
            "sale_end": detail.get("lotterySaleEndtime") or None,
            "status": status,
        },
        "fixtures": fixtures,
    }


def _prize_amount(item: dict) -> float | None:
    raw = item.get("stakeAmountFormat") or item.get("stakeAmount")
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_draw_results(detail: dict) -> dict | None:
    """官方期次详情 → {results_json, prizes_json}；未开奖返回 None。

    产出格式对齐 import_fixtures_csv，避免下游 check-draw 分裂出两套解析：
    results_json 为逗号分隔的 14 个 0/1/3/*；prizes_json 形如
    {"first": …, "second": …, "r9": …}（缺失的键不写入）。

    官方对"推迟或中断、且自开赛起 48 小时内未补赛"的场次以 '*' 占位，
    该场按 3/1/0 全选计算，因此 '*' 是**合法赛果**，必须保留入库，
    由 winnings 层按通配处理；只有整期未开奖才返回 None。
    """
    raw = (detail.get("lotteryDrawResult") or "").strip()
    results = [x for x in raw.split() if x]
    if len(results) != 14 or any(x not in {"0", "1", "3", "*"} for x in results):
        return None

    prizes: dict[str, float] = {}
    for item in detail.get("prizeLevelList") or []:
        level = (item.get("prizeLevel") or "").strip()
        amount = _prize_amount(item)
        if amount is None:
            continue
        if level == "一等奖":
            prizes["first"] = amount
        elif level == "二等奖":
            prizes["second"] = amount
    for item in detail.get("prizeLevelListRj") or []:
        level = (item.get("prizeLevel") or "").strip()
        amount = _prize_amount(item)
        if amount is not None and level in {"任选9场", "任九"}:
            prizes["r9"] = amount

    return {
        "results_json": ",".join(results),
        "prizes_json": json.dumps(prizes) if prizes else None,
    }


# ---- 入库层 ----------------------------------------------------------------------
def upsert_period(conn, parsed: dict, demote_others: bool = False) -> int:
    """写入期次与 14 场对阵，返回 period_id。

    幂等：同一期重跑不产生重复行。且**保留**已存在的 match_id / odds_json，
    否则重跑 collect-period 会清空 map-fixtures 的成果。
    """
    period = parsed["period"]
    if demote_others:
        conn.execute(
            "UPDATE periods SET status='historical' WHERE status='current' AND period_no<>?",
            (period["period_no"],),
        )

    store.upsert(conn, "periods", {
        "period_no": period["period_no"],
        "draw_date": period.get("draw_date"),
        "sale_end": period.get("sale_end"),
        "status": period.get("status") or "historical",
    }, ["period_no"])

    period_id = conn.execute(
        "SELECT id FROM periods WHERE period_no=?", (period["period_no"],)
    ).fetchone()["id"]

    previous = {
        row["seq"]: row
        for row in conn.execute(
            "SELECT seq, match_id, odds_json FROM period_matches WHERE period_id=?",
            (period_id,),
        )
    }

    for fx in parsed["fixtures"]:
        prev = previous.get(fx["seq"])
        store.upsert(conn, "period_matches", {
            "period_id": period_id,
            "seq": fx["seq"],
            "home_name_cn": fx["home_name_cn"],
            "away_name_cn": fx["away_name_cn"],
            "match_time": fx.get("match_time"),
            "league_cn": fx.get("league_cn"),
            "match_id": prev["match_id"] if prev else None,
            "odds_json": prev["odds_json"] if prev else None,
        }, ["period_id", "seq"])
    return period_id


def ensure_current_period(conn) -> dict | None:
    """确保当期在售期次的 14 场对阵已入库；缺失或不全则抓取补齐。

    供常驻采集使用：新期次开卖时无需人工跑 collect-period，采集进程
    自己就能跟上。返回 {period_no, created, fixtures}；无在售期次返回 None。
    """
    current = fetch_current_period()
    if not current or not current.get("period_no"):
        return None
    period_no = current["period_no"]

    row = conn.execute(
        "SELECT id FROM periods WHERE period_no=?", (period_no,)
    ).fetchone()
    if row:
        count = conn.execute(
            "SELECT COUNT(*) FROM period_matches WHERE period_id=?", (row["id"],)
        ).fetchone()[0]
        if count >= 14:
            return {"period_no": period_no, "created": False, "fixtures": count}

    detail = fetch_period_detail(period_no)
    parsed = parse_period(detail, status="current")
    if not parsed["period"].get("sale_end"):
        parsed["period"]["sale_end"] = current.get("sale_end")
    upsert_period(conn, parsed, demote_others=True)
    return {"period_no": period_no, "created": True,
            "fixtures": len(parsed["fixtures"])}


def collect_history(
    conn,
    years: int = 4,
    page_size: int = 100,
    pause: float = 0.5,
) -> dict:
    """拉取近 N 年历史开奖入库。翻页遇到早于 cutoff 的期次即停止。"""
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=365 * years)
    saved = skipped = 0
    period_nos: list[str] = []
    page_no = 1

    while True:
        value = fetch_history_page(page_no, page_size=page_size)
        items = value.get("list") or []
        if not items:
            break

        reached_cutoff = False
        for item in items:
            draw_time = (item.get("lotteryDrawTime") or "")[:10]
            try:
                drawn_on = date.fromisoformat(draw_time)
            except ValueError:
                skipped += 1
                continue
            if drawn_on < cutoff:
                reached_cutoff = True
                break

            # 先解析开奖再决定是否入库：未开奖的期次不写库。
            # 含 '*' 的期次照常入库 —— 那些场次按全选计算（见 parse_draw_results）。
            draw = parse_draw_results(item)
            if not draw:
                skipped += 1
                continue
            try:
                parsed = parse_period(item, status="historical")
            except CollectorError:
                # 对阵异常（含 matchList != 14）的期次跳过并计数，不中断整批
                skipped += 1
                continue

            period_id = upsert_period(conn, parsed)
            store.upsert(conn, "draw_results", {"period_id": period_id, **draw}, ["period_id"])
            saved += 1
            period_nos.append(parsed["period"]["period_no"])

        if reached_cutoff:
            break
        total_pages = value.get("pages") or 0
        page_no += 1
        if total_pages and page_no > total_pages:
            break
        time.sleep(pause)

    return {"periods_saved": saved, "skipped": skipped, "period_nos": period_nos}


# ---- 竞彩赔率：匹配与快照 ----------------------------------------------------------
def _period_rows(conn, period_no: str) -> list:
    period = conn.execute(
        "SELECT id FROM periods WHERE period_no=?", (period_no,)
    ).fetchone()
    if not period:
        return []
    return list(conn.execute(
        """SELECT id, seq, home_name_cn, away_name_cn, odds_json
           FROM period_matches WHERE period_id=? ORDER BY seq""",
        (period["id"],),
    ))


def _index_jc(jc_odds: list[dict]) -> dict[tuple[str, str], dict]:
    """按（主队名, 客队名）建索引。"""
    return {(m["home_cn"], m["away_cn"]): m for m in jc_odds}


def match_to_period(conn, period_no: str, jc_odds: list[dict]) -> dict:
    """把竞彩场次按队名对匹配到当期对阵。返回 {matched, unmatched}。

    两边队名同源（都是体彩官方中文名），无需经过 team_alias。
    """
    index = _index_jc(jc_odds)
    matched = 0
    unmatched: list[int] = []
    for row in _period_rows(conn, period_no):
        key = (row["home_name_cn"], row["away_name_cn"])
        if key in index:
            matched += 1
        else:
            unmatched.append(row["seq"])
    return {"matched": matched, "unmatched": unmatched}


def save_odds_snapshots(conn, period_no: str, jc_odds: list[dict]) -> dict:
    """按赔率变化追加快照，并刷新 period_matches.odds_json 的 jc 键。

    - 与该场最新快照的 h/d/a 完全一致 → 不写（"有变化才保存"）
    - 有变化 → **普通 INSERT** 追加一行（绝不用 store.upsert：
      它的 ON CONFLICT DO UPDATE 会静默覆盖历史快照）
    - odds_json 做 read-modify-write，保留既有键（如 avg/max/b365）
    """
    from datetime import datetime

    index = _index_jc(jc_odds)
    changed = skipped = unmatched = 0
    for row in _period_rows(conn, period_no):
        jc = index.get((row["home_name_cn"], row["away_name_cn"]))
        if not jc:
            unmatched += 1
            continue

        latest = conn.execute(
            """SELECT h, d, a FROM odds_snapshots
               WHERE period_match_id=? AND source='jc'
               ORDER BY id DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        if latest and (latest["h"], latest["d"], latest["a"]) == (jc["h"], jc["d"], jc["a"]):
            skipped += 1
            continue

        conn.execute(
            """INSERT INTO odds_snapshots(
                   period_match_id, source, captured_at, update_time, h, d, a)
               VALUES(?, 'jc', ?, ?, ?, ?, ?)""",
            (row["id"], datetime.now().isoformat(timespec="microseconds"),
             jc.get("update_time"), jc["h"], jc["d"], jc["a"]),
        )

        try:
            merged = json.loads(row["odds_json"] or "{}")
            if not isinstance(merged, dict):
                merged = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            merged = {}
        merged["jc"] = {"h": jc["h"], "d": jc["d"], "a": jc["a"]}
        conn.execute(
            "UPDATE period_matches SET odds_json=? WHERE id=?",
            (json.dumps(merged), row["id"]),
        )
        changed += 1

    conn.commit()
    return {"changed": changed, "skipped": skipped, "unmatched": unmatched}


# ---- fixture CSV 导入（兜底主路径） ------------------------------------------------
def import_fixtures_csv(
    conn: sqlite3.Connection,
    csv_path: str | Path,
) -> dict:
    """导入历史期次对阵；返回 {period_no: 汇总计数}。

    CSV 列（首行表头必须存在）：
      period_no, seq, home_cn, away_cn, match_time,
      result,           # 该场开奖结果（可选, 单字符 3/1/0）
      draw_14,          # 整期 14 场结果，逗号分隔（只在 seq=1 行写）
      first_prize,      # 单注一等奖奖金（只写一次）
      second_prize,     # 二等奖单注
      r9_prize,         # 任九单注
      draw_date         # 开奖日 (YYYY-MM-DD)

    期号级（开奖结果 + 奖金）只在 seq=1 行写，其它行留空。
    """
    out: dict[str, dict[str, int]] = {}
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        pn = (row.get("period_no") or "").strip()
        if not pn:
            continue
        seq_raw = (row.get("seq") or "").strip()
        if not seq_raw.isdigit():
            continue
        seq = int(seq_raw)
        if not (1 <= seq <= 14):
            continue
        home = (row.get("home_cn") or "").strip()
        away = (row.get("away_cn") or "").strip()
        if not home or not away:
            continue
        if pn not in out:
            out[pn] = {"period": 0, "matches": 0, "draw": 0}
        if out[pn]["period"] == 0:
            store.upsert(conn, "periods", {
                "period_no": pn,
                "draw_date": (row.get("draw_date") or "").strip() or None,
                "sale_end": None,
                "status": "historical",
            }, ["period_no"])
            out[pn]["period"] = 1
        period_id = conn.execute(
            "SELECT id FROM periods WHERE period_no=?", (pn,)
        ).fetchone()["id"]
        store.upsert(conn, "period_matches", {
            "period_id": period_id,
            "seq": seq,
            "home_name_cn": home,
            "away_name_cn": away,
            "match_time": (row.get("match_time") or "").strip() or None,
            "match_id": None,
            "odds_json": None,
        }, ["period_id", "seq"])
        out[pn]["matches"] += 1
        # 开奖结果（14 场总和）只在 seq=1 这一行携带，整期写一次 draw_results
        if seq == 1:
            d14 = (row.get("draw_14") or "").strip()
            results = [s.strip() for s in d14.split(",") if s.strip()]
            if len(results) == 14 and all(r in {"0", "1", "3"} for r in results):
                prizes: dict[str, float] = {}
                for k_in, k_out in (("first_prize", "first"),
                                    ("second_prize", "second"),
                                    ("r9_prize", "r9")):
                    v = (row.get(k_in) or "").strip()
                    if v:
                        prizes[k_out] = float(v)
                store.upsert(conn, "draw_results", {
                    "period_id": period_id,
                    "results_json": d14,
                    "prizes_json": json.dumps(prizes) if prizes else None,
                }, ["period_id"])
                out[pn]["draw"] = 1
    conn.commit()
    return out
