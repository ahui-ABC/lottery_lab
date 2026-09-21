"""数字彩开奖采集（官方数据源）。

- 体彩（大乐透 / 排列三 / 排列五）：`webapi.sporttery.cn`，与足彩同一个网关，无需认证。
  `getHistoryPageListV1.qry?gameNo=...` 每页 100 期。
- 福彩（双色球 / 福彩3D）：`www.cwl.gov.cn` 的 `findDrawNotice`，每页 100 期。

**为什么不用第三方站点**（最初试过彩宝贝 kaijiang.78500.cn，已弃用）：
1. 第三方站点单期页面 1 请求/期，6 年约 8000 次；官方接口 100 期/请求，全历史约 255 次。
2. 实测第三方站点 6 并发无间隔约 1000 次请求就被阿里云 WAF 封了整个 IP。
3. 官方是权威源，且返回 JSON —— 不必处理 gb18030 编码、HTML 结构漂移，
   也没有「排列类号码是单位数需要补零」这类陷阱。

期号统一成 7 位：体彩用 5 位（'26107' = 年 26 + 序号 107），补成 '2026107'；
福彩本来就是 7 位。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

BASE_SPORTTERY = "https://webapi.sporttery.cn/gateway/lottery"
BASE_CWL = "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx"
PAGE_SIZE = 100
DEFAULT_RATE = 5.0              # 全站共享的请求速率上限（次/秒）；官方接口可以稳一点快
BLOCK_BACKOFF = (30, 60, 120)   # 撞 403/429 后的退避秒数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SPORTTERY_HEADERS = {**HEADERS, "Referer": "https://www.sporttery.cn/"}
CWL_HEADERS = {**HEADERS, "Referer": "https://www.cwl.gov.cn/ygkj/wqkjgg/"}

LOTTERIES = {
    "dlt": {"name": "大乐透", "source": "sporttery", "game_no": "85",
            "kind": "two_zone", "front": 5, "front_max": 35, "back": 2, "back_max": 12},
    "ssq": {"name": "双色球", "source": "cwl", "game_no": "ssq",
            "kind": "two_zone", "front": 6, "front_max": 33, "back": 1, "back_max": 16},
    "p3": {"name": "排列三", "source": "sporttery", "game_no": "35",
           "kind": "digits", "digits": 3},
    "p5": {"name": "排列五", "source": "sporttery", "game_no": "350133",
           "kind": "digits", "digits": 5},
    "3d": {"name": "福彩3D", "source": "cwl", "game_no": "3d",
           "kind": "digits", "digits": 3},
}
LOTTERY_NAMES = {k: v["name"] for k, v in LOTTERIES.items()}

# 福彩奖级的 type 编号 → 名称。3D 的奖级接口长期为空，但它的奖金是固定值，
# 不依赖奖级表也能算（见 lottery_backtest）。
CWL_TIER_NAMES = {
    "ssq": {1: "一等奖", 2: "二等奖", 3: "三等奖",
            4: "四等奖", 5: "五等奖", 6: "六等奖"},
    "3d": {1: "直选", 2: "组选3", 3: "组选6"},
}


class CollectorError(RuntimeError):
    """接口返回业务错误或结构不符。"""


class RateLimited(RuntimeError):
    """站点返回 403/429 —— 被 WAF 拦了。不要硬撞，停下来。"""


class _RateLimiter:
    """全局令牌桶：所有 worker 共享一个速率上限。

    每个请求各自 sleep 一次是不够的 —— N 个 worker 会把总速率放大 N 倍。
    这里用一把锁推进「下一个可发时刻」，实际速率与并发数无关。
    """

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now)
            delay = self._next - now
            self._next += self._interval
        if delay > 0:
            time.sleep(delay)


_limiter = _RateLimiter(DEFAULT_RATE)
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client():
    """复用 httpx.Client 连接池（省掉每请求一次 TCP+TLS 握手）。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            import httpx

            _CLIENT = httpx.Client(timeout=25, follow_redirects=True,
                                   limits=httpx.Limits(max_connections=8,
                                                       max_keepalive_connections=8))
    return _CLIENT


def close_client() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


# ---- 数值 / 期号 -----------------------------------------------------------

def norm_issue(raw: str) -> str:
    """体彩 5 位期号（'26107'）补成 7 位（'2026107'）；福彩本来就是 7 位。"""
    raw = (raw or "").strip()
    return f"20{raw}" if len(raw) == 5 else raw


def _num(value) -> int | None:
    """'831,053,727.13' → 831053727；'---' / '' / '-1' → None（不是 0）。

    体彩用 -1 表示「该项不适用」（例如追加奖没开出）。若按「去掉非数字」处理，
    -1 会变成正的 1 —— 静默把哨兵值当成了奖金。
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("-"):
        return None
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _get_json(url: str, params: dict, headers: dict,
              retries: int = len(BLOCK_BACKOFF)) -> dict:
    """GET 并解析 JSON。403/429 退避重试，仍被拦则抛 RateLimited。

    被拦时**不换 UA、不换代理** —— 那是绕过反爬。我们只降速，降不下来就停手，
    下次跑从断点续传。
    """
    for attempt in range(retries + 1):
        _limiter.wait()
        resp = get_client().get(url, params=params, headers=headers)
        if resp.status_code in (403, 429):
            if attempt >= retries:
                raise RateLimited(f"{url} 持续 {resp.status_code}，站点已限流")
            time.sleep(BLOCK_BACKOFF[attempt])
            continue
        resp.raise_for_status()
        return resp.json()
    raise RateLimited(f"{url} 持续被限流")


# ---- 解析 ------------------------------------------------------------------

def parse_sporttery(item: dict, lottery: str) -> dict:
    """解析体彩一条开奖记录。"""
    spec = LOTTERIES[lottery]
    parts = (item.get("lotteryDrawResult") or "").split()
    issue = norm_issue(str(item.get("lotteryDrawNum") or ""))

    if spec["kind"] == "two_zone":
        want = spec["front"] + spec["back"]
        if len(parts) != want:
            raise ValueError(f"{lottery} {issue} 号码个数不符：{len(parts)}/{want}")
        numbers = {"front": sorted(parts[:spec["front"]]),
                   "back": sorted(parts[spec["front"]:])}
    else:
        if len(parts) != spec["digits"]:
            raise ValueError(f"{lottery} {issue} 位数不符：{len(parts)}/{spec['digits']}")
        numbers = {"digits": list(parts)}

    prizes = [
        {"tier": p.get("prizeLevel") or "", "cond": p.get("lotteryCondition") or "",
         "winners": _num(p.get("stakeCount")), "amount": _num(p.get("stakeAmountFormat"))}
        for p in item.get("prizeLevelList") or []
    ]
    order = (item.get("lotteryUnsortDrawresult") or "").split()
    return {
        "lottery": lottery,
        "issue": issue,
        "draw_date": (item.get("lotteryDrawTime") or "")[:10],
        "numbers": numbers,
        "draw_order": json.dumps(order, ensure_ascii=False) if order else None,
        "sales": _num(item.get("totalSaleAmount")),
        "jackpot": _num(item.get("poolBalanceAfterdraw")),
        "prizes": prizes,
    }


def parse_cwl(item: dict, lottery: str) -> dict:
    """解析福彩一条开奖记录。3D 的 prizegrades 长期为空，属正常。"""
    spec = LOTTERIES[lottery]
    issue = norm_issue(str(item.get("code") or ""))
    red = [x for x in (item.get("red") or "").split(",") if x]
    blue = [x for x in (item.get("blue") or "").split(",") if x]

    if spec["kind"] == "two_zone":
        if len(red) != spec["front"] or len(blue) != spec["back"]:
            raise ValueError(
                f"{lottery} {issue} 号码个数不符：red={len(red)}/{spec['front']} "
                f"blue={len(blue)}/{spec['back']}")
        numbers = {"front": sorted(red), "back": sorted(blue)}
    else:
        if len(red) != spec["digits"]:
            raise ValueError(f"{lottery} {issue} 位数不符：{len(red)}/{spec['digits']}")
        numbers = {"digits": red}

    # 福彩两个彩种的 typemoney 语义**不同**，别混用：
    #   双色球 = 单注奖金（三等奖 3000、四等奖 200 都对得上法定值）
    #   3D     = 该奖级的总奖金（2020001 期「直选」= 17,598,680 / 16,931 注 ≈ 1039.4，
    #            不是单注 1040；且受限赔规则影响会略低于法定值）
    # 3D 的奖金是法定固定值，回测直接用固定值即可，不引入这份口径不同的数据。
    prizes = []
    if lottery != "3d":
        names = CWL_TIER_NAMES[lottery]
        for p in item.get("prizegrades") or []:
            amount = _num(p.get("typemoney"))
            if amount is None:
                continue
            prizes.append({"tier": names.get(int(p.get("type") or 0), ""), "cond": "",
                           "winners": _num(p.get("typenum")), "amount": amount})

    return {
        "lottery": lottery,
        "issue": issue,
        "draw_date": (item.get("date") or "").split("(")[0].strip(),
        "numbers": numbers,
        "draw_order": None,          # 福彩接口不提供出球顺序
        "sales": _num(item.get("sales")),
        "jackpot": _num(item.get("poolmoney")),
        "prizes": prizes,
    }


def parse_item(item: dict, lottery: str) -> dict:
    if LOTTERIES[lottery]["source"] == "sporttery":
        return parse_sporttery(item, lottery)
    return parse_cwl(item, lottery)


# ---- 抓取 ------------------------------------------------------------------

def fetch_page(lottery: str, page_no: int, page_size: int = PAGE_SIZE) -> list[dict]:
    """抓一页原始记录。返回空列表表示没有更多。"""
    spec = LOTTERIES[lottery]
    if spec["source"] == "sporttery":
        payload = _get_json(
            f"{BASE_SPORTTERY}/getHistoryPageListV1.qry",
            {"gameNo": spec["game_no"], "provinceId": "0",
             "pageSize": str(page_size), "isVerify": "1", "pageNo": str(page_no)},
            SPORTTERY_HEADERS)
        if not payload.get("success"):
            raise CollectorError(payload.get("errorMessage") or "体彩接口返回失败")
        return (payload.get("value") or {}).get("list") or []
    payload = _get_json(
        f"{BASE_CWL}/findDrawNotice",
        {"name": spec["game_no"], "pageNo": str(page_no),
         "pageSize": str(page_size), "systemType": "PC"},
        CWL_HEADERS)
    if payload.get("state") not in (0, None):
        raise CollectorError(payload.get("message") or "福彩接口返回失败")
    return payload.get("result") or []


def fetch_draws(lottery: str, since_year: int | None = None,
                max_pages: int = 200) -> list[dict]:
    """按页抓取到 since_year 为止（含）。返回解析后的记录列表。

    接口是倒序的（最新在前），所以按年份滑过边界即可停止。
    """
    out: list[dict] = []
    for page_no in range(1, max_pages + 1):
        items = fetch_page(lottery, page_no)
        if not items:
            break
        reached_older = False
        for item in items:
            row = parse_item(item, lottery)
            if since_year is not None and int(row["issue"][:4]) < since_year:
                reached_older = True
                continue
            out.append(row)
        if reached_older:
            break
    return out


# ---- 入库 ------------------------------------------------------------------

def existing_issues(conn, lottery: str) -> set[str]:
    return {r["issue"] for r in conn.execute(
        "SELECT issue FROM lottery_draw WHERE lottery=?", (lottery,))}


def upsert_draw(conn, row: dict) -> None:
    """开奖结果是既成事实 —— 用 upsert 覆盖，而不是赔率快照那套 append-only。
    重抓同一期得到不同值，只可能是数据源修正或我方解析 bug，覆盖并留痕才对。"""
    conn.execute(
        """INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, draw_order,
                                   sales, jackpot, prizes, fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(lottery, issue) DO UPDATE SET
             draw_date=excluded.draw_date, numbers=excluded.numbers,
             draw_order=excluded.draw_order, sales=excluded.sales,
             jackpot=excluded.jackpot, prizes=excluded.prizes,
             fetched_at=excluded.fetched_at""",
        (row["lottery"], row["issue"], row["draw_date"],
         json.dumps(row["numbers"]), row.get("draw_order"),
         row.get("sales"), row.get("jackpot"),
         json.dumps(row.get("prizes") or [], ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def sync_history(conn, lottery: str, since_year: int | None = None,
                 rate: float | None = None, on_progress=None) -> dict:
    """把某彩种的历史开奖同步入库（幂等，重复跑只会刷新）。

    单线程按页抓 —— 每页 100 期，6 年只要几十个请求，没有并发必要，
    也就不需要担心并发把站点打爆。
    """
    global _limiter
    if rate is not None:
        _limiter = _RateLimiter(rate)

    have = existing_issues(conn, lottery)
    stats = {"lottery": lottery, "saved": 0, "updated": 0, "skipped": 0,
             "pages": 0, "oldest": None, "blocked": False}

    def _walk():
        for page_no in range(1, 200):
            try:
                items = fetch_page(lottery, page_no)
            except RateLimited:
                stats["blocked"] = True
                return
            if not items:
                return
            stats["pages"] += 1
            reached_older = False
            for item in items:
                row = parse_item(item, lottery)
                if since_year is not None and int(row["issue"][:4]) < since_year:
                    reached_older = True
                    continue
                if row["issue"] in have:
                    upsert_draw(conn, row)      # 仍然覆盖：数据源可能修正过
                    stats["skipped"] += 1
                else:
                    have.add(row["issue"])
                    upsert_draw(conn, row)
                    stats["saved"] += 1
                stats["oldest"] = row["issue"]
            if on_progress:
                on_progress(stats["pages"], stats)
            if reached_older:
                return

    _walk()
    return stats


def refresh_latest(conn, lottery: str) -> int:
    """只刷第一页（最新 100 期），给「预测下一期」用。返回更新的条数。"""
    items = fetch_page(lottery, 1)
    for item in items:
        upsert_draw(conn, parse_item(item, lottery))
    return len(items)


def latest_issue(conn, lottery: str) -> str | None:
    row = conn.execute(
        "SELECT issue FROM lottery_draw WHERE lottery=? ORDER BY issue DESC LIMIT 1",
        (lottery,)).fetchone()
    return row["issue"] if row else None
