"""数字彩开奖采集（数据源：彩宝贝 kaijiang.78500.cn）。

设计见 `docs/superpowers/specs/2026-09-21-digital-lottery-design.md` §2/§4。

三个必须知道的坑：
1. 站点编码是 gb18030。
2. 不带浏览器 UA 会被阿里云 WAF 拦成 403。
3. 排列三/排列五/福彩3D 的号码在 HTML 里是**单位数** `<li class="rb_kj">0</li>`，
   解析后必须保持单个字符，不能两两拼接（否则 064 会变成 64）。

页面模板五种彩种一致，已实测：
  #kjCode ul.kjh li     → 号码，class 含 rb_kj = 前区/红球，b_kj = 后区/蓝球
  .kjh_order_nums       → 出球顺序（排列类无）
  #endTime              → 开奖日期
  #sale                 → 本期投注金额
  #bonusBalance         → 滚入下期奖金（排列类该行被注释掉，解析结果自然是 None）
  #winList tr           → 奖级表。大乐透每行 4 列（奖级/中奖条件/中奖注数/单注奖金），
                          其余彩种 3 列（没有「中奖条件」列）。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

BASE_URL = "https://kaijiang.78500.cn"
DEFAULT_WORKERS = 6             # 对第三方站点保持克制，不加大并发

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# kind: two_zone = 前后双区选号；digits = 按位选数字
LOTTERIES = {
    "dlt": {"name": "大乐透", "kind": "two_zone", "front": 5, "front_max": 35,
            "back": 2, "back_max": 12, "per_year": 160},
    "ssq": {"name": "双色球", "kind": "two_zone", "front": 6, "front_max": 33,
            "back": 1, "back_max": 16, "per_year": 160},
    "p3": {"name": "排列三", "kind": "digits", "digits": 3, "per_year": 370},
    "p5": {"name": "排列五", "kind": "digits", "digits": 5, "per_year": 370},
    "3d": {"name": "福彩3D", "kind": "digits", "digits": 3, "per_year": 370},
}
LOTTERY_NAMES = {k: v["name"] for k, v in LOTTERIES.items()}

_KJ_LI = re.compile(r'<li class="(rb_kj|b_kj)">\s*([0-9]+)\s*</li>')
_DRAW_DATE = re.compile(r'id="endTime">\s*(\d{4})年(\d{2})月(\d{2})日')
_SALE = re.compile(r'id="sale">([\d,]+)')
_JACKPOT = re.compile(r'id="bonusBalance">([\d,]+)')
_ORDER = re.compile(r'class="kjh_order_nums">\s*([\d\s]+?)\s*</span>')
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _int(text: str | None) -> int | None:
    """'313,508,185元' → 313508185；空/无数字 → None（不是 0）。"""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _text(cell: str) -> str:
    return _TAG.sub("", cell).strip()


def _parse_prizes(html: str) -> list[dict]:
    """解析奖级表。大乐透 4 列、其余彩种 3 列，按列数分别处理。"""
    start = html.find('id="winList"')
    if start < 0:
        return []
    end = html.find("</tbody>", start)
    block = html[start:end if end > 0 else len(html)]
    rows = []
    for tr in _TR.findall(block):
        cells = [_text(c) for c in _TD.findall(tr)]
        if len(cells) == 4:
            tier, cond, winners, amount = cells
        elif len(cells) == 3:
            tier, winners, amount = cells
            cond = ""
        else:
            continue
        if not tier or tier == "奖级分配":
            continue
        rows.append({"tier": tier, "cond": cond.strip("（）()"),
                     "winners": _int(winners), "amount": _int(amount)})
    return rows


def parse_draw(html: str, lottery: str, issue: str) -> dict:
    """解析单期开奖页。号码个数/位数与彩种规格不符时抛 ValueError。"""
    spec = LOTTERIES[lottery]
    start = html.find('id="kjCode"')
    if start < 0:
        raise ValueError(f"{lottery} {issue} 页面缺少 kjCode 区块")
    end = html.find("</ul>", start)
    block = html[start:end if end > 0 else len(html)]
    items = _KJ_LI.findall(block)

    if spec["kind"] == "two_zone":
        front = sorted(v.zfill(2) for cls, v in items if cls == "rb_kj")
        back = sorted(v.zfill(2) for cls, v in items if cls == "b_kj")
        if len(front) != spec["front"] or len(back) != spec["back"]:
            raise ValueError(
                f"{lottery} {issue} 号码个数不符：front={len(front)}/{spec['front']} "
                f"back={len(back)}/{spec['back']}")
        numbers = {"front": front, "back": back}
    else:
        digits = [v for _, v in items]        # 单个字符，原样保留
        if len(digits) != spec["digits"]:
            raise ValueError(
                f"{lottery} {issue} 位数不符：{len(digits)}/{spec['digits']}")
        numbers = {"digits": digits}

    date_match = _DRAW_DATE.search(html)
    if not date_match:
        raise ValueError(f"{lottery} {issue} 页面缺少开奖日期")
    year, month, day = date_match.groups()

    sale = _SALE.search(html)
    jackpot = _JACKPOT.search(html)
    order = _ORDER.findall(html)
    return {
        "lottery": lottery,
        "issue": issue,
        "draw_date": f"{year}-{month}-{day}",
        "numbers": numbers,
        "draw_order": json.dumps(order, ensure_ascii=False) if order else None,
        "sales": _int(sale.group(1)) if sale else None,
        "jackpot": _int(jackpot.group(1)) if jackpot else None,
        "prizes": _parse_prizes(html),
    }


_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client():
    """复用 httpx.Client 连接池（与 sporttery 同理：省掉每请求一次 TCP+TLS 握手）。

    不复用 sporttery 的 client —— 它带着体彩官网的 Referer/Origin，
    发给彩宝贝既不对也可能触发风控。
    """
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            import httpx

            _CLIENT = httpx.Client(
                headers=HEADERS, timeout=20, follow_redirects=True,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))
    return _CLIENT


def close_client() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


def _get_html(path: str) -> str | None:
    resp = get_client().get(f"{BASE_URL}{path}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content.decode("gb18030", errors="replace")


def fetch_draw(lottery: str, issue: str) -> dict | None:
    """抓单期。返回 None 表示该期号不存在（404），不是错误。"""
    html = _get_html(f"/{lottery}/{issue}/")
    return None if html is None else parse_draw(html, lottery, issue)


def latest_issues(lottery: str, limit: int = 1) -> list[str]:
    """列表页给出最近 100 期，用于把尾巴补到最新。"""
    html = _get_html(f"/{lottery}/")
    if html is None:
        return []
    found = sorted(set(re.findall(rf'href="/{lottery}/(\d{{7}})/"', html)),
                   reverse=True)
    return found[:limit]


def issue_candidates(year: int, lottery: str) -> list[str]:
    return [f"{year}{n:03d}" for n in range(1, LOTTERIES[lottery]["per_year"] + 1)]


def existing_issues(conn, lottery: str) -> set[str]:
    return {r["issue"] for r in conn.execute(
        "SELECT issue FROM lottery_draw WHERE lottery=?", (lottery,))}


def upsert_draw(conn, row: dict) -> None:
    """开奖结果是既成事实 —— 这里用 upsert 覆盖，而不是赔率快照那套 append-only。
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


def _candidates_between(lottery: str, after: str, upto: str) -> list[str]:
    """期号在 (after, upto] 之间的候选，用于补中间缺口。"""
    out = []
    for year in range(int(after[:4]), int(upto[:4]) + 1):
        out.extend(c for c in issue_candidates(year, lottery) if after < c <= upto)
    return out


def sync_tail(conn, lottery: str) -> int:
    """把库里最大期号补齐到列表页的最新期号（含中间缺口）。返回新增条数。"""
    latest = latest_issues(lottery)
    have = existing_issues(conn, lottery)
    if not latest or not have:
        return 0        # 空库交给 sync_range 全量回填
    added = 0
    for issue in _candidates_between(lottery, max(have), latest[0]):
        row = fetch_draw(lottery, issue)
        if row is not None:
            upsert_draw(conn, row)
            added += 1
    return added


def sync_range(conn, lottery: str, year_from: int, year_to: int,
               workers: int = DEFAULT_WORKERS, on_progress=None) -> dict:
    """枚举期号回填，已入库的跳过（断点续传）。

    并发只用于 HTTP，写库在调用线程串行执行 —— SQLite 写互斥，多线程写会
    `database is locked`，串行入库同时也让计数天然准确（沿用 jc_history 的做法）。
    """
    have = existing_issues(conn, lottery)
    targets = [i for year in range(year_from, year_to + 1)
               for i in issue_candidates(year, lottery) if i not in have]
    stats = {"lottery": lottery, "targets": len(targets), "saved": 0,
             "missing": 0, "failed": 0, "failed_issues": []}
    if not targets:
        return stats

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_draw, lottery, i): i for i in targets}
        for done, future in enumerate(as_completed(futures), 1):
            issue = futures[future]
            try:
                row = future.result()
            except Exception:
                stats["failed"] += 1
                stats["failed_issues"].append(issue)
            else:
                if row is None:
                    stats["missing"] += 1        # 该期不存在（停售/年份尾部），正常
                else:
                    upsert_draw(conn, row)
                    stats["saved"] += 1
            if on_progress and done % 200 == 0:
                on_progress(done, len(targets), stats)
    return stats
