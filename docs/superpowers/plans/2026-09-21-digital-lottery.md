# 数字彩（大乐透/双色球/排列三/排列五/福彩3D）实施计划

> **⚠ 实施过程中的重大变更（2026-09-21，已执行）**
>
> 本计划 Task 2 写的是抓第三方站点彩宝贝的 HTML 单期页。实施时该方案被放弃：
> 1 请求/期、6 年约 8000 次，实测 6 并发无间隔约 1000 次请求就把整个 IP 被 WAF 封了。
> 已改为**官方接口**（体彩 webapi + 福彩 cwl.gov.cn），每页 100 期、6 年约 85 个请求。
> 因此下面 Task 2 的 HTML 解析、枚举回填、`sync_range`/`sync_tail`、`notes` 命名
> **均已被取代**，实际以 `docs/superpowers/specs/2026-09-21-digital-lottery-design.md`
> 与代码为准（`parse_sporttery`/`parse_cwl`/`sync_history`/`refresh_latest`/`bets`）。
> 其余任务（表结构、策略、回测、CLI、页面）按计划执行。
> 实际结论见 `docs/数字彩回测结论.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抓取 2020 年至今五类数字彩开奖数据入库，实现五条选号策略的预测，并用逐期走查回测量化每条策略相对随机选号的真实表现。

**Architecture:** 采集器用「多线程抓 HTML + 单线程写库」（沿用 `jc_history.sync_day` 的模型）；预测把所有策略统一成「权重函数 + 加权不放回抽样」；回测按开奖时序走查，第 t 期只允许看 t 之前的数据，用逐期配对 t 检验判断策略是否显著优于随机。

**Tech Stack:** Python 3.14 / httpx（连接池）/ SQLite / 正则解析（项目无 bs4/lxml，站点结构简单且已验证）/ numpy + scipy（显著性）/ FastAPI + Jinja2 + 原生 JS

**设计文档:** `docs/superpowers/specs/2026-09-21-digital-lottery-design.md`

**项目约定（已核对源码，照抄即可）:**
- 解释器：`.venv/Scripts/python.exe`；测试：`.venv/Scripts/python.exe -m pytest`
- `cli.build_parser()` 用 `sub.add_subparsers(dest="cmd")`，**`cli.main()` 用 `if args.cmd == "xxx":` 硬分发** —— 只加 `set_defaults(func=...)` 不会生效，必须同时改 `main()`
- `web/app.py` 连库用 `_get_conn()`，渲染用 `templates.TemplateResponse(request, "x.html", {...})`
- `db/store.py`：`fetchone(conn, sql, params)`、`fetchall(conn, sql, params)`、`upsert(conn, table, row, keys=[...])`
- 术语统一用 **`bets`（注单）**，与 `jc_parlay_plans.bets_json` 保持一致
- 所有测试**不联网**，HTML 解析用本地 fixture

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/football_lottery/collectors/lottery_history.py` | 新建。抓取 + 解析 + 回填同步。唯一与 78500.cn 打交道的地方 |
| `src/football_lottery/models/lottery_predict.py` | 新建。权重函数、Gumbel 采样、生成推荐号码 |
| `src/football_lottery/models/lottery_backtest.py` | 新建。奖级判定、逐期走查、配对显著性 |
| `src/football_lottery/db/schema.sql` | 修改。追加 3 张表 |
| `src/football_lottery/cli.py` | 修改。追加 4 个子命令（含 `main()` 分发分支） |
| `src/football_lottery/web/app.py` | 修改。追加 `/lottery` 路由与 `_lottery_context` |
| `src/football_lottery/web/templates/lottery.html` | 新建。数字彩页面 |
| `src/football_lottery/web/templates/base.html` | 修改。导航加 tab |
| `src/football_lottery/web/static/styles.css` | 修改。号码球样式 |
| `tests/fixtures/lottery/{dlt,ssq,p3,p5,3d}.html` | 新建。五种彩种真实页面裁剪 |
| `tests/collectors/test_lottery_parse.py` | 新建 |
| `tests/models/test_lottery_predict.py` | 新建 |
| `tests/models/test_lottery_backtest.py` | 新建 |
| `tests/test_cli_lottery.py` | 新建 |

---

### Task 1: 数据表

**Files:**
- Modify: `src/football_lottery/db/schema.sql`（追加到文件末尾）
- Test: `tests/test_cli_lottery.py`

- [ ] **Step 1: 追加建表 SQL**

在 `src/football_lottery/db/schema.sql` 末尾追加：

```sql
-- 数字彩开奖（大乐透/双色球/排列三/排列五/福彩3D）
--
-- 与 jc_odds_history 的 append-only 不同：开奖号码是**既成事实**，重抓同一期
-- 应当得到完全相同的值。若不同，只可能是数据源修正或我方解析 bug —— 所以这里
-- 用 upsert 覆盖写入，而不是追加快照。
CREATE TABLE IF NOT EXISTS lottery_draw (
  lottery     TEXT NOT NULL,     -- dlt/ssq/p3/p5/3d
  issue       TEXT NOT NULL,     -- '2026107'
  draw_date   TEXT NOT NULL,     -- 'YYYY-MM-DD'
  numbers     TEXT NOT NULL,     -- {"front":["02",...],"back":["04","10"]} 或 {"digits":["0","6","4"]}
  draw_order  TEXT,              -- JSON 出球顺序；排列类无此字段
  sales       INTEGER,           -- 本期投注金额（元）
  jackpot     INTEGER,           -- 滚入下期奖金（元）
  prizes      TEXT,              -- [{"tier":"一等奖","cond":"5+2","winners":3,"amount":10000000}]
  fetched_at  TEXT,
  PRIMARY KEY (lottery, issue));
CREATE INDEX IF NOT EXISTS idx_lottery_draw_date ON lottery_draw(lottery, draw_date);

-- 每期每策略的推荐注单；开奖后回填 hits/prize
CREATE TABLE IF NOT EXISTS lottery_prediction (
  lottery      TEXT NOT NULL,
  target_issue TEXT NOT NULL,
  strategy     TEXT NOT NULL,
  bets         TEXT NOT NULL,    -- JSON 注单列表，一注一个元素
  created_at   TEXT NOT NULL,
  hits         TEXT,             -- JSON 每注命中明细
  prize        INTEGER,          -- 该策略该期总奖金（元）
  PRIMARY KEY (lottery, target_issue, strategy));

-- 回测结果落库，供页面读取（页面实时跑回测太慢）
CREATE TABLE IF NOT EXISTS lottery_backtest (
  lottery  TEXT NOT NULL,
  strategy TEXT NOT NULL,
  params   TEXT,                 -- JSON {window, bets, start, end, draws}
  metrics  TEXT,                 -- JSON {roi, invested, returned, avg_hits, win_rate}
  paired   TEXT,                 -- JSON {se, ci_low, ci_high, p, beats_random}
  ran_at   TEXT,
  PRIMARY KEY (lottery, strategy));
```

- [ ] **Step 2: 写建表测试**

`tests/test_cli_lottery.py`：

```python
import json
import sqlite3

from football_lottery.db import store


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def test_lottery_tables_created():
    conn = _conn()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lottery_draw", "lottery_prediction", "lottery_backtest"} <= names
```

- [ ] **Step 3: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_lottery.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/football_lottery/db/schema.sql tests/test_cli_lottery.py
git commit -m "feat(lottery): 数字彩开奖/预测/回测三张表"
```

---

### Task 2: 页面解析器

**Files:**
- Create: `src/football_lottery/collectors/lottery_history.py`
- Create: `tests/fixtures/lottery/{dlt,ssq,p3,p5,3d}.html`
- Test: `tests/collectors/test_lottery_parse.py`

- [ ] **Step 1: 保存 fixture**

用浏览器 UA 抓五种彩种各一期真实页面，裁剪到开奖区块（`<table class="openBox"` 起、
`id="winList"` 之后第一个 `</tbody>` 止），存成 utf-8。
命令（项目根目录执行，脚本写在临时目录，不落进仓库）：

```bash
cat > "$TEMP/grab_lottery_fixtures.py" <<'PY'
import httpx, pathlib
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAIRS = [("dlt", "2026107"), ("ssq", "2020100"), ("p3", "2020100"),
         ("p5", "2020100"), ("3d", "2020100")]
OUT = pathlib.Path("tests/fixtures/lottery")
OUT.mkdir(parents=True, exist_ok=True)
with httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as c:
    for lot, issue in PAIRS:
        r = c.get(f"https://kaijiang.78500.cn/{lot}/{issue}/")
        r.raise_for_status()
        html = r.content.decode("gb18030", errors="replace")
        start = html.find('<table class="openBox"')
        win = html.find('id="winList"')
        end = html.find("</tbody>", win) + len("</tbody>")
        assert start > 0 and win > start and end > win, (lot, start, win, end)
        (OUT / f"{lot}.html").write_text(html[start:end], encoding="utf-8")
        print(f"{lot}/{issue}: {end - start} bytes")
PY
.venv/Scripts/python.exe "$TEMP/grab_lottery_fixtures.py"
```

抓完人工看一眼 `tests/fixtures/lottery/p3.html`：应当含 `<li class="rb_kj">0</li>`
（单位数、含 0）和奖级行。

- [ ] **Step 2: 写失败的解析测试**

`tests/collectors/test_lottery_parse.py`：

```python
import json
import re
import sqlite3
from pathlib import Path

import pytest

from football_lottery.collectors import lottery_history as lh
from football_lottery.collectors.lottery_history import parse_draw
from football_lottery.db import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lottery"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def _html(lottery: str) -> str:
    return (FIXTURES / f"{lottery}.html").read_text(encoding="utf-8")


def test_dlt_numbers_and_meta():
    row = parse_draw(_html("dlt"), "dlt", "2026107")
    assert row["numbers"] == {"front": ["02", "05", "07", "14", "22"],
                              "back": ["04", "10"]}
    assert row["draw_date"] == "2026-09-19"
    assert row["sales"] == 313508185
    assert row["jackpot"] == 831053727
    tiers = {p["tier"]: p for p in row["prizes"]}
    assert tiers["一等奖"]["winners"] == 3
    assert tiers["一等奖"]["amount"] == 10000000


def test_ssq_has_six_red_one_blue():
    row = parse_draw(_html("ssq"), "ssq", "2020100")
    assert len(row["numbers"]["front"]) == 6
    assert len(row["numbers"]["back"]) == 1
    assert row["draw_date"] == "2020-10-13"


@pytest.mark.parametrize("lottery,width", [("p3", 3), ("p5", 5), ("3d", 3)])
def test_digit_lotteries_keep_leading_zeros(lottery, width):
    """页面用单位数 <li>0</li> 表示号码，必须原样保留单个数字，不能拼错位。"""
    row = parse_draw(_html(lottery), lottery, "2020100")
    digits = row["numbers"]["digits"]
    assert len(digits) == width
    assert all(len(d) == 1 and d.isdigit() for d in digits)
    assert row["jackpot"] is None       # 排列类没有滚存奖金


def test_missing_kjcode_raises():
    with pytest.raises(ValueError):
        parse_draw("<html>没有号码</html>", "dlt", "2026107")


def test_number_count_mismatch_raises():
    """号码个数与彩种规格不符时必须报错，绝不静默返回残缺数据。"""
    html = re.sub(r'<li class="rb_kj">\s*\d+\s*</li>', "", _html("dlt"), count=1)
    with pytest.raises(ValueError):
        parse_draw(html, "dlt", "2026107")


def test_issue_candidates_cover_a_year():
    items = lh.issue_candidates(2026, "p3")
    assert items[0] == "2026001"
    assert items[-1] == "2026370"
    assert all(len(i) == 7 for i in items)


def test_upsert_draw_is_idempotent_and_corrects():
    """开奖结果是事实：重抓同一期必须覆盖为最新解析结果。"""
    conn = _conn()
    row = parse_draw(_html("p3"), "p3", "2020100")
    lh.upsert_draw(conn, row)
    row2 = dict(row)
    row2["numbers"] = {"digits": ["1", "1", "1"]}
    lh.upsert_draw(conn, row2)
    rows = conn.execute("SELECT numbers FROM lottery_draw WHERE lottery='p3'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["numbers"]) == {"digits": ["1", "1", "1"]}


def test_sync_range_skips_existing(monkeypatch):
    """已入库的期号不应重复请求 —— 断点续传的基础。"""
    conn = _conn()
    lh.upsert_draw(conn, parse_draw(_html("p3"), "p3", "2020001"))
    called = []

    def fake_fetch(lottery, issue):
        called.append(issue)
        return None

    monkeypatch.setattr(lh, "fetch_draw", fake_fetch)
    lh.sync_range(conn, "p3", 2020, 2020, workers=2)
    assert "2020001" not in called
    assert "2020002" in called
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/collectors/test_lottery_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: football_lottery.collectors.lottery_history`

- [ ] **Step 4: 实现**

创建 `src/football_lottery/collectors/lottery_history.py`：

```python
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
  #bonusBalance         → 滚入下期奖金（排列类该行被注释掉）
  #winList tr           → 奖级表
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

BASE_URL = "https://kaijiang.78500.cn"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DEFAULT_WORKERS = 6             # 对第三方站点保持克制，不加大并发

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
_WIN_ROW = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]*)</td>\s*"
    r"<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>", re.S)


def _int(text: str | None) -> int | None:
    """'313,508,185元' → 313508185；空/无数字 → None（不是 0）。"""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _parse_prizes(html: str) -> list[dict]:
    start = html.find('id="winList"')
    if start < 0:
        return []
    end = html.find("</tbody>", start)
    block = html[start:end if end > 0 else len(html)]
    rows = []
    for tier, cond, winners, amount in _WIN_ROW.findall(block):
        tier = tier.strip()
        if not tier or tier == "奖级分配":
            continue
        rows.append({"tier": tier, "cond": cond.strip().strip("（）()"),
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


DEFAULT_WORKERS = DEFAULT_WORKERS
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client():
    """复用 httpx.Client 连接池（与 sporttery 同理：省掉每请求一次 TCP+TLS 握手）。"""
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
    targets = _candidates_between(lottery, max(have), latest[0])
    added = 0
    for issue in targets:
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
```

> 注：`DEFAULT_WORKERS` 只在文件顶部定义一次，上面那行 `DEFAULT_WORKERS = DEFAULT_WORKERS`
> 是笔误，写代码时**不要**照抄 —— 直接删掉。

- [ ] **Step 5: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/collectors/test_lottery_parse.py -v`
Expected: 全部 PASS

若 `test_dlt_numbers_and_meta` 的日期或金额断言与实际抓到的页面不符，
**以抓下来的真实页面为准**修改断言，不要改解析器去迁就臆想的期望值。

- [ ] **Step 6: 提交**

```bash
git add src/football_lottery/collectors/lottery_history.py tests/fixtures/lottery tests/collectors/test_lottery_parse.py
git commit -m "feat(lottery): 开奖页解析器与断点续传回填"
```

---

### Task 3: 选号策略

**Files:**
- Create: `src/football_lottery/models/lottery_predict.py`
- Test: `tests/models/test_lottery_predict.py`

- [ ] **Step 1: 写失败的测试**

`tests/models/test_lottery_predict.py`：

```python
import random

from football_lottery.models import lottery_predict as lp


def _draw(digits=(), front=(), back=()):
    if front or back:
        return {"numbers": {"front": list(front), "back": list(back)}}
    return {"numbers": {"digits": list(digits)}}


def test_weighted_sample_respects_weights():
    rng = random.Random(0)
    assert lp.weighted_sample([1000.0, 1.0, 1.0, 1.0], 1, rng) == [0]


def test_weighted_sample_returns_sorted_unique_indices():
    rng = random.Random(1)
    picked = lp.weighted_sample([1.0] * 35, 5, rng)
    assert len(set(picked)) == 5
    assert picked == sorted(picked)


def test_uniform_weights_are_actually_uniform():
    """均匀权重下各号码被选中频率应接近 5/35（卡方粗判）。"""
    counts = {i: 0 for i in range(35)}
    rng = random.Random(7)
    for _ in range(4000):
        for i in lp.weighted_sample([1.0] * 35, 5, rng):
            counts[i] += 1
    expected = 4000 * 5 / 35
    for c in counts.values():
        assert 0.7 * expected < c < 1.3 * expected


def test_hot_prefers_frequent_numbers():
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    picked = lp.predict_one("dlt", history, "hot", random.Random(0), window=20)
    assert set(picked["front"]) == {"01", "02", "03", "04", "05"}


def test_cold_prefers_absent_numbers():
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    picked = lp.predict_one("dlt", history, "cold", random.Random(0), window=20)
    assert not set(picked["front"]) & {"01", "02", "03", "04", "05"}


def test_overdue_prefers_longest_absent():
    """所有数字都出现过，且 '1' 最久没出 —— 它必须排在权重第一。

    构造上要保证 0/3-9 都出现过，否则它们会拿到「窗口内从未出现」的上界遗漏值，
    反而压过 '1'（这正是第一版测试写错的地方）。
    """
    cycle = ["0", "2", "3", "4", "5", "6", "7", "8", "9"]
    history = [_draw(digits=["1", "1", "1"])]
    for i in range(20):
        history.append(_draw(digits=[cycle[i % len(cycle)]] * 3))
    picked = lp.predict_one("p3", history, "overdue", random.Random(0), window=21)
    assert picked["digits"] == ["1", "1", "1"]


def test_digit_strategies_are_per_position():
    """按位统计：百位全是 9、十位全是 0、个位全是 5 —— 各位置各取各的。"""
    history = [_draw(digits=["9", "0", "5"]) for _ in range(30)]
    picked = lp.predict_one("p3", history, "hot", random.Random(0), window=30)
    assert picked["digits"] == ["9", "0", "5"]


def test_predict_one_has_no_hidden_state():
    """同一份历史必须给出同一结果；换一份历史结果必须变化 ——
    证明它只读传入的 history，不依赖任何模块级缓存（缓存 = 泄漏的温床）。"""
    past = [_draw(digits=["1", "2", "3"])] * 30
    extended = past + [_draw(digits=["7", "8", "9"])] * 30
    a = lp.predict_one("p3", past, "hot", random.Random(3), window=30)
    b = lp.predict_one("p3", past, "hot", random.Random(3), window=30)
    assert a == b == {"digits": ["1", "2", "3"]}
    assert lp.predict_one("p3", extended, "hot", random.Random(3),
                          window=30) == {"digits": ["7", "8", "9"]}


def test_all_strategies_produce_valid_shape():
    history = [_draw(front=[f"{i:02d}" for i in range(1, 7)], back=["01"])
               for _ in range(5)]
    for strategy in lp.STRATEGIES:
        picked = lp.predict_one("ssq", history, strategy, random.Random(0), window=5)
        assert len(picked["front"]) == 6
        assert len(set(picked["front"])) == 6
        assert len(picked["back"]) == 1
        assert all(1 <= int(x) <= 33 for x in picked["front"])
        assert all(1 <= int(x) <= 16 for x in picked["back"])


def test_predict_bets_returns_requested_count_and_unique():
    history = [_draw(front=["01", "02", "03", "04", "05"], back=["01", "02"])
               for _ in range(20)]
    bets = lp.predict_bets("dlt", history, "hot", 5, seed=1, window=20)
    assert len(bets) == 5
    keys = {tuple(b["front"] + b["back"]) for b in bets}
    assert len(keys) == 5          # 不留重复注单


def test_predict_bets_survives_degenerate_history():
    """历史极短时也要给出足量注单，不能因为去重而少发。"""
    history = [_draw(digits=["1", "2", "3"])]
    bets = lp.predict_bets("p3", history, "hot", 5, seed=1, window=100)
    assert len(bets) == 5


def test_transient_history_does_not_crash():
    for strategy in lp.STRATEGIES:
        picked = lp.predict_one("p5", [_draw(digits=list("12345"))],
                                strategy, random.Random(0), window=100)
        assert len(picked["digits"]) == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/models/test_lottery_predict.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/football_lottery/models/lottery_predict.py`：

```python
"""数字彩选号策略。

设计见 spec §5。五条路径的差别**只在权重函数**，采样过程共用一套
「加权不放回抽样」（Gumbel top-k）。

硬约束：第 t 期只能用 < t 期的数据。本模块的 `predict_one` 只接收调用方给的
history，不自行按日期筛选 —— 时间边界由调用方（回测游标 / 线上预测）负责，
测试用「同一份历史给同一结果、换一份历史结果必变」锁死「无隐藏状态」这一点。
"""
from __future__ import annotations

import json
import math
import random
from collections import Counter
from datetime import date, datetime

from football_lottery.collectors.lottery_history import LOTTERIES

STRATEGIES = ("random", "hot", "cold", "overdue", "weighted")
STRATEGY_LABELS = {
    "random": "随机",
    "hot": "热号",
    "cold": "冷号",
    "overdue": "遗漏",
    "weighted": "贝叶斯",
}
DEFAULT_WINDOW = 100
DEFAULT_BETS = 5
BET_PRICE = 2           # 元/注，与 lottery_backtest.TICKET_COST 保持一致
DIRICHLET_ALPHA = 1.0


def _gumbel(rng: random.Random) -> float:
    """标准 Gumbel 噪声 -log(-log(U))。"""
    return -math.log(-math.log(rng.random()))


def weighted_sample(weights: list[float], k: int, rng: random.Random) -> list[int]:
    """加权不放回抽样，返回选中下标（升序）。

    Gumbel top-k：给 log(w) 加 Gumbel 噪声后取前 k 大，等价于按 w 不放回抽样，
    O(n log n)、无需累积分布，权重为 0 也安全。
    """
    keys = [(math.log(max(w, 1e-12)) + _gumbel(rng), i) for i, w in enumerate(weights)]
    keys.sort(key=lambda pair: (-pair[0], pair[1]))
    return sorted(i for _, i in keys[:k])


def _last_seen_gap(series: list[list[str]], universe: list[str]) -> dict[str, int]:
    """每个号码距最近一次出现过了几期（0 = 最近一期刚出）。

    窗口内从未出现的号码取上界 len(series) —— 对排序而言与真实遗漏等价。
    """
    gap = {x: len(series) for x in universe}
    seen: set[str] = set()
    for age, values in enumerate(reversed(series)):
        for value in values:
            if value in gap and value not in seen:
                gap[value] = age
                seen.add(value)
    return gap


def _weights(strategy: str, series: list[list[str]], universe: list[str],
             rng: random.Random) -> list[float]:
    counts = Counter(v for values in series for v in values)
    if strategy == "random":
        return [1.0] * len(universe)
    if strategy == "hot":
        return [counts.get(x, 0) + 1.0 for x in universe]
    if strategy == "cold":
        top = max([counts.get(x, 0) for x in universe] + [0])
        return [(top - counts.get(x, 0)) + 1.0 for x in universe]
    if strategy == "overdue":
        gap = _last_seen_gap(series, universe)
        return [gap.get(x, 0) + 1.0 for x in universe]
    if strategy == "weighted":
        # Dirichlet(α + 频次) 后验：Gamma 采样即可，归一化对排序无影响
        return [rng.gammavariate(DIRICHLET_ALPHA + counts.get(x, 0), 1.0)
                for x in universe]
    raise ValueError(f"未知策略：{strategy}")


def _series(history: list[dict], field: str) -> list[list[str]]:
    """把历史开奖按 field 抽成一个序列。field: 'front'/'back'/'d0'/'d1'..."""
    out = []
    for draw in history:
        numbers = draw["numbers"]
        if field == "front":
            out.append(list(numbers["front"]))
        elif field == "back":
            out.append(list(numbers["back"]))
        else:
            out.append([numbers["digits"][int(field[1:])]])
    return out


def _pick(strategy: str, series: list[list[str]], universe: list[str], k: int,
          rng: random.Random) -> list[str]:
    idx = weighted_sample(_weights(strategy, series, universe, rng), k, rng)
    return [universe[i] for i in idx]


def predict_one(lottery: str, history: list[dict], strategy: str,
                rng: random.Random, window: int = DEFAULT_WINDOW) -> dict:
    """生成一注号码。history 必须**只含目标期之前**的开奖，时序升序。"""
    spec = LOTTERIES[lottery]
    recent = history[-window:] if window > 0 else history
    if spec["kind"] == "two_zone":
        front_universe = [f"{i:02d}" for i in range(1, spec["front_max"] + 1)]
        back_universe = [f"{i:02d}" for i in range(1, spec["back_max"] + 1)]
        return {
            "front": _pick(strategy, _series(recent, "front"), front_universe,
                           spec["front"], rng),
            "back": _pick(strategy, _series(recent, "back"), back_universe,
                          spec["back"], rng),
        }
    universe = [str(i) for i in range(10)]
    return {"digits": [_pick(strategy, _series(recent, f"d{pos}"), universe, 1, rng)[0]
                       for pos in range(spec["digits"])]}


def predict_bets(lottery: str, history: list[dict], strategy: str, n_bets: int,
                 seed, window: int = DEFAULT_WINDOW) -> list[dict]:
    """生成 n_bets 注**互不重复**的号码。

    随机流按 attempt 递增 —— 同一策略在同一期上不会因为重试而拿到同一注。
    """
    bets: list[dict] = []
    seen: set[str] = set()
    for attempt in range(n_bets * 50):
        if len(bets) >= n_bets:
            break
        rng = random.Random(f"{seed}-{strategy}-{attempt}")
        picked = predict_one(lottery, history, strategy, rng, window)
        key = json.dumps(picked, sort_keys=True)
        if key not in seen:
            seen.add(key)
            bets.append(picked)
    return bets


def next_issue(last_issue: str, today: date | None = None) -> str:
    """下一期期号：同年 +1；跨年归 001。"""
    today = today or date.today()
    year, number = int(last_issue[:4]), int(last_issue[4:])
    if today.year > year:
        return f"{year + 1}001"
    return f"{year}{number + 1:03d}"


def load_history(conn, lottery: str, before_issue: str | None = None) -> list[dict]:
    """取某彩种历史开奖，时序升序。before_issue 给定时只取严格早于它的期数。"""
    sql = "SELECT lottery, issue, numbers FROM lottery_draw WHERE lottery=?"
    params: list = [lottery]
    if before_issue is not None:
        sql += " AND issue < ?"
        params.append(before_issue)
    sql += " ORDER BY issue"
    return [{"lottery": r["lottery"], "issue": r["issue"],
             "numbers": json.loads(r["numbers"])}
            for r in conn.execute(sql, params)]


def save_predictions(conn, lottery: str, target_issue: str, strategies,
                     n_bets: int, window: int, seed) -> int:
    """对下一期生成各策略推荐并入库。返回写入条数。"""
    history = load_history(conn, lottery, target_issue)
    if not history:
        return 0
    written = 0
    for strategy in strategies:
        picks = predict_bets(lottery, history, strategy, n_bets, seed, window)
        conn.execute(
            """INSERT INTO lottery_prediction(
                   lottery, target_issue, strategy, bets, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(lottery, target_issue, strategy) DO UPDATE SET
                 bets=excluded.bets, created_at=excluded.created_at""",
            (lottery, target_issue, strategy,
             json.dumps(picks, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        written += 1
    conn.commit()
    return written
```

- [ ] **Step 4: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/models/test_lottery_predict.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/football_lottery/models/lottery_predict.py tests/models/test_lottery_predict.py
git commit -m "feat(lottery): 五条选号策略（权重函数 + Gumbel 加权抽样）"
```

---

### Task 4: 奖级判定与回测

**Files:**
- Create: `src/football_lottery/models/lottery_backtest.py`
- Test: `tests/models/test_lottery_backtest.py`

- [ ] **Step 1: 写失败的测试**

`tests/models/test_lottery_backtest.py`：

```python
from football_lottery.models import lottery_backtest as lb


def _draws(n, lottery="p3", numbers=None, start=1):
    return [{"lottery": lottery, "issue": f"2020{start + i:03d}",
             "draw_date": "2020-01-01", "prizes": None,
             "numbers": numbers or {"digits": ["1", "2", "3"]}}
            for i in range(n)]


def test_dlt_tier_boundaries():
    drawn = {"front": ["01", "02", "03", "04", "05"], "back": ["01", "02"]}
    prizes = [{"tier": "四等奖", "cond": "4+2", "winners": 100, "amount": 3000}]

    def amount(front, back):
        return lb.prize_for("dlt", {"front": front, "back": back}, drawn, prizes)

    assert amount(["01", "02", "03", "04", "06"], ["01", "02"]) == 3000   # 4+2
    assert amount(["01", "02", "06", "07", "08"], ["01", "02"]) == 0      # 2+2 不在表里
    assert amount(["01", "02", "03", "04", "05"], ["01", "02"]) == 0      # 5+2 浮动，表里没有 → 0
    assert amount(["01", "02", "03", "04", "05"], ["03", "04"]) == 0      # 5+0 不在表里


def test_dlt_falls_back_to_fixed_amount_when_table_missing():
    drawn = {"front": ["01", "02", "03", "04", "05"], "back": ["01", "02"]}
    assert lb.prize_for("dlt", {"front": ["01", "02", "03", "04", "06"],
                                "back": ["01", "02"]}, drawn, None) == 3000   # 4+2
    assert lb.prize_for("dlt", {"front": ["01", "02", "03", "06", "07"],
                                "back": ["01", "02"]}, drawn, None) == 200    # 3+2
    assert lb.prize_for("dlt", {"front": ["01", "02", "03", "06", "07"],
                                "back": ["01", "03"]}, drawn, None) == 100    # 3+1


def test_ssq_tier_boundaries():
    drawn = {"front": ["01", "02", "03", "04", "05", "06"], "back": ["07"]}
    assert lb.prize_for("ssq", drawn, drawn, None) == 0                  # 6+1 浮动 → 0
    assert lb.prize_for("ssq", {"front": ["01", "02", "03", "04", "05", "08"],
                                "back": ["07"]}, drawn, None) == 3000     # 5+1
    assert lb.prize_for("ssq", {"front": ["01", "02", "03", "09", "10", "11"],
                                "back": ["07"]}, drawn, None) == 10       # 3+1
    assert lb.prize_for("ssq", {"front": ["08", "09", "10", "11", "12", "13"],
                                "back": ["07"]}, drawn, None) == 5        # 0+1
    assert lb.prize_for("ssq", {"front": ["01", "02", "03", "09", "10", "11"],
                                "back": ["08"]}, drawn, None) == 0        # 3+0


def test_p3_and_3d_only_pay_direct():
    """一注 2 元只买一种玩法：直选。顺序不同就是没中 —— 不能同时算上组选，
    否则单注期望奖金会超过 2 元（直选 1/1000×1040 + 组选 6/1000×173 = 2.078 元），
    返还率算出来 >100%，那是自证错误。"""
    drawn = {"digits": ["0", "6", "4"]}
    assert lb.prize_for("p3", drawn, drawn, None) == 1040
    assert lb.prize_for("p3", {"digits": ["4", "6", "0"]}, drawn, None) == 0
    assert lb.prize_for("3d", {"digits": ["4", "0", "6"]}, drawn, None) == 0


def test_p5_prize():
    drawn = {"digits": ["1", "2", "3", "4", "5"]}
    assert lb.prize_for("p5", drawn, drawn, None) == 100000
    assert lb.prize_for("p5", {"digits": ["1", "2", "3", "4", "6"]}, drawn, None) == 0


def test_dlt_prizes_use_scraped_floating_amounts():
    """一/二等奖是浮动奖，必须用当期抓到的实际金额，不能用固定值。"""
    drawn = {"front": ["01", "02", "03", "04", "05"], "back": ["01", "02"]}
    prizes = [{"tier": "一等奖", "cond": "5+2", "winners": 3, "amount": 8000000}]
    assert lb.prize_for("dlt", drawn, drawn, prizes) == 8000000


def test_paired_significance_flags_obvious_difference():
    """差值必须带噪声 —— 常数差值 sd=0 会被跳过（见下一个测试），
    真实彩票收益也永远不会是常数。"""
    per_draw = {}
    for name, base in (("random", 0.0), ("hot", 100.0)):
        per_draw[name] = {f"2020{i:03d}": base + (i % 7)
                          for i in range(1, 61)}
    rows = lb.paired_significance(per_draw, baseline="random")
    assert rows[0]["strategy"] == "hot"
    assert rows[0]["beats_random"] is True
    assert rows[0]["mean_diff"] > 0


def test_paired_significance_does_not_flag_identical_series():
    """完全相同的收益序列不能被说成「击败随机」。"""
    series = {f"2020{i:03d}": float(i % 3) for i in range(1, 61)}
    rows = lb.paired_significance({"random": series, "hot": dict(series)},
                                  baseline="random")
    assert rows == []


def test_paired_significance_does_not_flag_pure_noise():
    """均值为 0 的随机噪声不应被判为显著。"""
    import random as _r
    rng = _r.Random(0)
    base = {f"2020{i:03d}": float(rng.randint(0, 100)) for i in range(1, 201)}
    other = {k: float(rng.randint(0, 100)) for k in base}
    rows = lb.paired_significance({"random": base, "hot": other}, baseline="random")
    assert all(not r["beats_random"] for r in rows)


def test_walk_forward_never_uses_future(monkeypatch):
    """核心防泄漏断言：第 t 期拿到的 history 必须**内容上逐条等于** draws[:t]。

    只断言长度不够 —— 号码相同的历史即使长度对，内容也可能被串改。
    """
    seen = []

    def fake_predict_one(lottery, history, strategy, rng, window=100):
        seen.append([d["issue"] for d in history])
        return {"digits": ["1", "1", "1"]}

    monkeypatch.setattr(lb.lottery_predict, "predict_one", fake_predict_one)
    monkeypatch.setattr(lb.lottery_predict, "predict_bets",
                        lambda lottery, history, strategy, n, seed, window=100: [
                            fake_predict_one(lottery, history, strategy, None, window)])
    draws = [{"lottery": "p3", "issue": f"2020{i:03d}",
              "draw_date": "2020-01-01", "prizes": None,
              "numbers": {"digits": [str(i % 10)] * 3}} for i in range(1, 41)]
    lb.run_backtest(draws, "p3", strategies=["hot"], window=10, n_bets=1,
                    min_history=5)
    expected = [[f"2020{j:03d}" for j in range(1, i)] for i in range(6, 41)]
    assert seen == expected


def test_run_backtest_always_includes_random_baseline():
    """即使调用方只点了一条策略，也必须带上 random —— 否则没有对照，
    报告会把「无基线」说成「无差异」。"""
    draws = _draws(60)
    result = lb.run_backtest(draws, "p3", strategies=["hot"], n_bets=1,
                             min_history=30)
    assert "random" in result["per_draw"]
    assert "hot" in result["per_draw"]
    assert result["paired"]


def test_run_backtest_invested_matches_actual_bets():
    """投入按**实际发出的注数**算，不能按请求注数 —— 去重后可能少发。"""
    draws = _draws(60)
    result = lb.run_backtest(draws, "p3", strategies=["hot"], n_bets=3,
                             min_history=30)
    metrics = result["metrics"]["hot"]
    assert metrics["invested"] == metrics["draws"] * 3 * lb.BET_PRICE


def test_summarize_mentions_multiple_comparison():
    result = lb.run_backtest(_draws(40), "p3", strategies=["hot"], n_bets=1,
                             min_history=30)
    text = lb.summarize(result)
    assert "多重比较" in text or "假显著" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/models/test_lottery_backtest.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/football_lottery/models/lottery_backtest.py`：

```python
"""数字彩回测：逐期走查 + 与随机选号的配对显著性。

设计见 spec §6。这是这个子项目最重要的部分 —— 它负责证伪，而不是证实。

奖级判定用「命中数 → 奖级」的规则表，金额优先取当期抓到的实际奖级表
（大乐透/双色球的一、二等奖是浮动奖，只能这么算）；抓不到时用固定奖级兜底，
浮动奖无兜底值，计 0 并在报告里标出来，绝不估算。

排列三/福彩3D/排列五只按**直选**计奖：一注 2 元只买一种玩法。若同时算上组选，
单注期望奖金会超过票价（>100% 返还率），那是模型错误而不是发现。
"""
from __future__ import annotations

import json

from football_lottery.collectors.lottery_history import LOTTERY_NAMES
from football_lottery.models import lottery_predict

BET_PRICE = lottery_predict.BET_PRICE       # 元/注，五类彩种统一 2 元
MIN_HISTORY = 30        # 走查起点的最小历史长度
DEFAULT_SEED = 20260921

# 命中数 → 奖级
DLT_TIERS = {
    (5, 2): "一等奖", (5, 1): "二等奖", (5, 0): "三等奖",
    (4, 2): "四等奖", (4, 1): "五等奖", (3, 2): "六等奖",
    (4, 0): "七等奖", (3, 1): "八等奖", (2, 2): "八等奖",
    (3, 0): "九等奖", (2, 1): "九等奖", (1, 2): "九等奖", (0, 2): "九等奖",
}
SSQ_TIERS = {
    (6, True): "一等奖", (6, False): "二等奖", (5, True): "三等奖",
    (5, False): "四等奖", (4, True): "四等奖",
    (4, False): "五等奖", (3, True): "五等奖",
    (2, True): "六等奖", (1, True): "六等奖", (0, True): "六等奖",
}
# 抓不到奖级表时的兜底。浮动奖（大乐透/双色球一、二等奖）无兜底 → 计 0。
FIXED_PRIZES = {
    "dlt": {"三等奖": 10000, "四等奖": 3000, "五等奖": 300, "六等奖": 200,
            "七等奖": 100, "八等奖": 15, "九等奖": 5},
    "ssq": {"三等奖": 3000, "四等奖": 200, "五等奖": 10, "六等奖": 5},
    "p3": {"直选": 1040},
    "3d": {"直选": 1040},
    "p5": {"直选": 100000},
}


def _amount_for(lottery: str, tier: str, prizes) -> int:
    for row in prizes or []:
        if row.get("tier") == tier and row.get("amount"):
            return int(row["amount"])
    return FIXED_PRIZES.get(lottery, {}).get(tier, 0)


def prize_for(lottery: str, picked: dict, drawn: dict, prizes) -> int:
    """一注号码在给定开奖下的奖金（元）。"""
    if lottery == "dlt":
        front = len(set(picked["front"]) & set(drawn["front"]))
        back = len(set(picked["back"]) & set(drawn["back"]))
        tier = DLT_TIERS.get((front, back))
        return _amount_for(lottery, tier, prizes) if tier else 0
    if lottery == "ssq":
        red = len(set(picked["front"]) & set(drawn["front"]))
        blue = bool(set(picked["back"]) & set(drawn["back"]))
        tier = SSQ_TIERS.get((red, blue))
        return _amount_for(lottery, tier, prizes) if tier else 0
    if picked["digits"] == drawn["digits"]:
        return FIXED_PRIZES[lottery]["直选"]
    return 0


def hits_for(lottery: str, picked: dict, drawn: dict) -> dict:
    """命中明细，用于报告里的 avg_hits。"""
    if lottery in ("dlt", "ssq"):
        return {"front": len(set(picked["front"]) & set(drawn["front"])),
                "back": len(set(picked["back"]) & set(drawn["back"]))}
    return {"digits": sum(1 for a, b in zip(picked["digits"], drawn["digits"]) if a == b)}


def load_draws(conn, lottery: str) -> list[dict]:
    """按时序（issue 升序）取出全部开奖，供走查使用。"""
    rows = conn.execute(
        """SELECT issue, draw_date, numbers, prizes FROM lottery_draw
           WHERE lottery=? ORDER BY issue""", (lottery,)).fetchall()
    return [{"lottery": lottery, "issue": r["issue"], "draw_date": r["draw_date"],
             "numbers": json.loads(r["numbers"]),
             "prizes": json.loads(r["prizes"]) if r["prizes"] else None}
            for r in rows]


def run_backtest(draws: list[dict], lottery: str, strategies=None,
                 window: int = lottery_predict.DEFAULT_WINDOW,
                 n_bets: int = lottery_predict.DEFAULT_BETS,
                 seed=DEFAULT_SEED, min_history: int = MIN_HISTORY) -> dict:
    """逐期走查。draws 必须按时序升序，且含 numbers/prizes 字段。

    第 t 期只用 draws[:t] —— 这是防泄漏的唯一实现点，测试有专门的内容级断言。
    """
    # random 是基线，恒在；调用方只点别的策略时也必须带上
    strategies = list(dict.fromkeys(["random", *(strategies or lottery_predict.STRATEGIES)]))
    per_draw: dict[str, dict[str, float]] = {s: {} for s in strategies}
    invested: dict[str, float] = {s: 0.0 for s in strategies}
    hits_sum: dict[str, dict[str, float]] = {s: {} for s in strategies}
    win_draws: dict[str, int] = {s: 0 for s in strategies}
    evaluated = 0

    for idx, drawn in enumerate(draws):
        history = draws[:idx]
        if len(history) < min_history:
            continue
        evaluated += 1
        issue = drawn["issue"]
        drawn_numbers = drawn["numbers"]
        prizes = drawn.get("prizes")
        for strategy in strategies:
            bets = lottery_predict.predict_bets(lottery, history, strategy, n_bets,
                                                f"{seed}-{issue}", window)
            total = 0.0
            for picked in bets:
                total += prize_for(lottery, picked, drawn_numbers, prizes)
                for key, value in hits_for(lottery, picked, drawn_numbers).items():
                    hits_sum[strategy][key] = hits_sum[strategy].get(key, 0.0) + value
            cost = len(bets) * BET_PRICE
            invested[strategy] += cost
            per_draw[strategy][issue] = total - cost
            if total > 0:
                win_draws[strategy] += 1

    metrics = {}
    for strategy in strategies:
        profits = per_draw[strategy]
        spent = invested[strategy]
        returned = sum(p for p in profits.values()) + spent
        total_bets = max(1, len(profits) * n_bets)
        metrics[strategy] = {
            "draws": len(profits),
            "invested": spent,
            "returned": returned,
            "roi": (returned / spent - 1.0) if spent else None,
            "return_rate": (returned / spent) if spent else None,
            "win_draws": win_draws[strategy],
            "win_rate": (win_draws[strategy] / len(profits)) if profits else None,
            "avg_hits": {k: v / total_bets for k, v in hits_sum[strategy].items()},
        }
    return {"lottery": lottery, "evaluated": evaluated, "n_bets": n_bets,
            "window": window, "per_draw": per_draw, "metrics": metrics,
            "paired": paired_significance(per_draw)}


def paired_significance(per_draw: dict[str, dict[str, float]],
                        baseline: str = "random", alpha: float = 0.05) -> list[dict]:
    """逐期配对 t 检验：同一期上「策略收益 − 随机收益」的均值是否为 0。

    同一期的号码是同一个随机事件，配对能消掉绝大部分方差，比各自跟理论值比更有力。
    """
    import numpy as np
    from scipy import stats

    base = per_draw.get(baseline)
    if not base:
        return []
    rows = []
    for strategy, profits in per_draw.items():
        if strategy == baseline:
            continue
        diffs = np.asarray([profits[i] - base[i] for i in profits if i in base],
                           dtype=float)
        n = diffs.size
        if n < 30:
            continue
        sd = float(diffs.std(ddof=1))
        if sd == 0:
            continue        # 完全无差异 → 不构成「击败随机」
        mean = float(diffs.mean())
        se = sd / (n ** 0.5)
        t_stat = mean / se
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df=n - 1)))
        crit = float(stats.t.ppf(1 - alpha / 2, df=n - 1))
        rows.append({
            "strategy": strategy, "n": n, "mean_diff": mean, "se": se,
            "ci_low": mean - crit * se, "ci_high": mean + crit * se,
            "t": t_stat, "p": p_value,
            "beats_random": bool(p_value < alpha and mean > 0),
        })
    rows.sort(key=lambda r: r["p"])
    return rows


def summarize(result: dict) -> str:
    """人类可读的回测报告。"""
    lottery = result["lottery"]
    lines = [f"== {LOTTERY_NAMES[lottery]}（{lottery}） 走查 {result['evaluated']} 期"
             f" · 每期 {result['n_bets']} 注 · 窗口 {result['window']} 期 =="]
    lines.append(f"{'策略':<8}{'返还率':>10}{'盈亏':>14}{'中奖期占比':>12}   命中均值")
    for strategy, m in result["metrics"].items():
        rate = f"{m['return_rate'] * 100:.1f}%" if m["return_rate"] else "—"
        profit = m["returned"] - m["invested"]
        win = f"{m['win_rate'] * 100:.1f}%" if m["win_rate"] is not None else "—"
        hits = " ".join(f"{k}={v:.2f}" for k, v in m["avg_hits"].items())
        lines.append(f"{strategy:<8}{rate:>10}{profit:>14.2f}{win:>12}   {hits}")
    if result["paired"]:
        lines.append("")
        lines.append("与 random 的逐期配对检验（正差值 = 策略更赚）：")
        for row in result["paired"]:
            flag = "  ← 显著" if row["beats_random"] else ""
            lines.append(
                f"  {row['strategy']:<8} 差值均值 {row['mean_diff']:+.3f} "
                f"95%CI [{row['ci_low']:+.3f}, {row['ci_high']:+.3f}] "
                f"p={row['p']:.3f}{flag}")
    else:
        lines.append("")
        lines.append("与 random 的逐期配对检验：无可比对数据（策略与随机完全同收益）。")
    lines.append("")
    lines.append("注：摇奖机是独立同分布，历史号码对下一期没有信息量。")
    lines.append("    这里同时跑了多条策略，α=0.05 下**平均每 20 次检验就会有一次假显著**；")
    lines.append("    看到「显著」的第一反应应当是查泄漏或判定 bug，而不是庆祝。")
    return "\n".join(lines)


def save_result(conn, result: dict) -> None:
    """回测结果落库，供页面读取（页面实时跑走查太慢）。"""
    from datetime import datetime

    paired = {row["strategy"]: row for row in result["paired"]}
    params = json.dumps({"window": result["window"], "bets": result["n_bets"],
                         "draws": result["evaluated"]}, ensure_ascii=False)
    ran_at = datetime.now().isoformat(timespec="seconds")
    for strategy, metrics in result["metrics"].items():
        conn.execute(
            """INSERT INTO lottery_backtest(lottery, strategy, params, metrics,
                                            paired, ran_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(lottery, strategy) DO UPDATE SET
                 params=excluded.params, metrics=excluded.metrics,
                 paired=excluded.paired, ran_at=excluded.ran_at""",
            (result["lottery"], strategy, params,
             json.dumps(metrics, ensure_ascii=False),
             json.dumps(paired.get(strategy), ensure_ascii=False), ran_at))
    conn.commit()
```

- [ ] **Step 4: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/models/test_lottery_backtest.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/football_lottery/models/lottery_backtest.py tests/models/test_lottery_backtest.py
git commit -m "feat(lottery): 奖级判定 + 逐期走查回测 + 配对显著性"
```

---

### Task 5: CLI 子命令

**Files:**
- Modify: `src/football_lottery/cli.py`
- Test: `tests/test_cli_lottery.py`（追加）

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_cli_lottery.py`：

```python
from football_lottery import cli


def test_lottery_commands_dispatch_in_main():
    """main() 是 if args.cmd == 硬分发，只加 set_defaults(func=...) 不会生效 ——
    这里直接调 main()，确保四条命令真的被接上。"""
    for name in ("collect-lottery", "predict-lottery", "score-lottery",
                 "backtest-lottery"):
        parser = cli.build_parser()
        args = parser.parse_args([name])
        assert args.cmd == name
        assert hasattr(cli, f"cmd_{name.replace('-', '_')}")


def test_collect_lottery_rejects_unknown_lottery(tmp_path, monkeypatch, capsys):
    cfg = {"db_path": str(tmp_path / "t.db")}
    args = cli.build_parser().parse_args(["collect-lottery", "--lottery", "xxx"])
    assert cli.cmd_collect_lottery(args, cfg) == 2
    assert "未知彩种" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_lottery.py -v`
Expected: FAIL — `SystemExit`（子命令不存在）

- [ ] **Step 3: 实现命令**

在 `src/football_lottery/cli.py` 中 `cmd_score_jc` 之后插入四个函数，
**并在 `main()` 的 `if args.cmd == "backtest-plans":` 之前加上四个分发分支**：

```python
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
    try:
        for lottery in picks:
            tail = lh.sync_tail(conn, lottery)
            if tail:
                print(f"{lh.LOTTERY_NAMES[lottery]}：补齐最新 {tail} 期")

            def _progress(done, total, stats, _name=lh.LOTTERY_NAMES[lottery]):
                print(f"  {_name} {done}/{total} 入库 {stats['saved']} "
                      f"缺期 {stats['missing']} 失败 {stats['failed']}", flush=True)

            stats = lh.sync_range(conn, lottery, args.year_from, args.year_to,
                                  workers=args.workers, on_progress=_progress)
            total = conn.execute("SELECT COUNT(*) c FROM lottery_draw WHERE lottery=?",
                                 (lottery,)).fetchone()["c"]
            print(f"{lh.LOTTERY_NAMES[lottery]}：本次新增 {stats['saved']}，"
                  f"缺期 {stats['missing']}，失败 {stats['failed']}，库内共 {total} 期")
            if stats["failed_issues"]:
                print(f"  失败期号（重跑本命令自动续传）："
                      f"{','.join(stats['failed_issues'][:20])}")
    finally:
        lh.close_client()
    return 0


def cmd_predict_lottery(args, cfg: dict) -> int:
    from football_lottery.collectors import lottery_history as lh
    from football_lottery.models import lottery_predict as lp

    conn = _connect(cfg)
    picks = list(lh.LOTTERIES) if args.lottery == "all" else args.lottery.split(",")
    strategies = (lp.STRATEGIES if args.strategy == "all"
                  else tuple(args.strategy.split(",")))
    try:
        for lottery in picks:
            lh.sync_tail(conn, lottery)        # 先补齐尾巴，否则会预测到已开过的期号
            last = conn.execute(
                "SELECT issue FROM lottery_draw WHERE lottery=? ORDER BY issue DESC LIMIT 1",
                (lottery,)).fetchone()
            if not last:
                print(f"{lh.LOTTERY_NAMES[lottery]}：库内无数据，先跑 collect-lottery")
                continue
            target = lp.next_issue(last["issue"])
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
        # 只更新对奖列，不要走 store.upsert —— 那会把 created_at 一起覆盖
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
```

`main()` 中追加（放在 `if args.cmd == "backtest-plans":` 之前）：

```python
    if args.cmd == "collect-lottery":
        return cmd_collect_lottery(args, cfg)
    if args.cmd == "predict-lottery":
        return cmd_predict_lottery(args, cfg)
    if args.cmd == "score-lottery":
        return cmd_score_lottery(args, cfg)
    if args.cmd == "backtest-lottery":
        return cmd_backtest_lottery(args, cfg)
```

在 `build_parser()` 的 `backtest-plans` 之后追加：

```python
    s = sub.add_parser("collect-lottery", help="回填数字彩开奖（大乐透/双色球/排列三/排列五/福彩3D）")
    s.add_argument("--lottery", default="all", help="all 或逗号分隔，如 dlt,ssq")
    s.add_argument("--from", dest="year_from", type=int, default=2020)
    s.add_argument("--to", dest="year_to", type=int, default=date.today().year)
    s.add_argument("--workers", type=int, default=6)
    s.set_defaults(func=cmd_collect_lottery)

    s = sub.add_parser("predict-lottery", help="对下一期生成各策略推荐号码")
    s.add_argument("--lottery", default="all")
    s.add_argument("--strategy", default="all")
    s.add_argument("--bets", type=int, default=5)
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--seed", type=int, default=20260921)
    s.set_defaults(func=cmd_predict_lottery)

    s = sub.add_parser("score-lottery", help="给已开奖的数字彩预测回填命中与奖金")
    s.set_defaults(func=cmd_score_lottery)

    s = sub.add_parser("backtest-lottery", help="数字彩逐期走查回测与配对显著性")
    s.add_argument("--lottery", default="all")
    s.add_argument("--strategy", default="all")
    s.add_argument("--bets", type=int, default=5)
    s.add_argument("--window", type=int, default=100)
    s.add_argument("--seed", type=int, default=20260921)
    s.set_defaults(func=cmd_backtest_lottery)
```

`cli.py` 需确认顶部已 `import json`、`from datetime import date`
和 `from football_lottery.db import store`；缺哪个补哪个（`date` 可能只导入了 `datetime`，
那就把 `date.today()` 写成 `datetime.now().date()`）。

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_lottery.py -v`
Expected: PASS

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通过（此前 197 项 + 新增，无回归）

- [ ] **Step 5: 提交**

```bash
git add src/football_lottery/cli.py tests/test_cli_lottery.py
git commit -m "feat(cli): 数字彩采集/预测/对奖/回测四条子命令"
```

---

### Task 6: Web 页面

**Files:**
- Modify: `src/football_lottery/web/app.py`
- Create: `src/football_lottery/web/templates/lottery.html`
- Modify: `src/football_lottery/web/templates/base.html`
- Modify: `src/football_lottery/web/static/styles.css`
- Test: `tests/test_web_lottery.py`

- [ ] **Step 1: 加样式**

追加到 `src/football_lottery/web/static/styles.css`：

```css
/* ---- 数字彩号码球 ------------------------------------------------------ */
.balls { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.ball {
  width: 32px; height: 32px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 14px; font-weight: 700;
  background: var(--bg-elev-2); border: 1px solid var(--border-2);
}
.ball.front { background: var(--bad-dim);  color: var(--bad);  border-color: var(--bad); }
.ball.back  { background: var(--info-dim); color: var(--info); border-color: var(--info); }
.ball.digit { background: var(--ok-dim);   color: var(--ok);   border-color: var(--ok); }
.ball.plus  { background: none; border: none; color: var(--text-dim); width: 14px; }

/* 频次热力格：次数越多越暖 */
.freq-cell { font-family: var(--mono); font-size: 12px; padding: 3px 6px; text-align: center; }
.freq-hot  { background: rgba(255, 107, 107, .20); color: var(--bad); }
.freq-warm { background: rgba(255, 181, 71, .16);  color: var(--warn); }
.freq-cool { background: rgba(76, 194, 255, .12);  color: var(--text-mid); }
.freq-cold { background: var(--bg-elev-2);         color: var(--text-dim); }
```

- [ ] **Step 2: 加路由**

在 `src/football_lottery/web/app.py` 的 `/jc` 路由之后追加
（**用文件里既有的 `_get_conn()` 与 `templates.TemplateResponse(request, ...)`，
不要自己发明名字**）：

```python
def _frequency_groups(history: list[dict], spec: dict, window: int = 100) -> list[dict]:
    """近 window 期的出现次数与遗漏期数，供页面画热力格。"""
    from collections import Counter

    recent = history[-window:]

    def _cells(universe, values_per_draw):
        counts = Counter(v for values in values_per_draw for v in values)
        gap, seen = {}, set()
        for age, values in enumerate(reversed(values_per_draw)):
            for v in values:
                if v not in seen:
                    gap[v] = age
                    seen.add(v)
        return [{"value": v, "count": counts.get(v, 0),
                 "gap": gap.get(v, len(recent))} for v in universe]

    if spec["kind"] == "two_zone":
        return [
            {"zone": "前区", "cells": _cells(
                [f"{i:02d}" for i in range(1, spec["front_max"] + 1)],
                [d["numbers"]["front"] for d in recent])},
            {"zone": "后区", "cells": _cells(
                [f"{i:02d}" for i in range(1, spec["back_max"] + 1)],
                [d["numbers"]["back"] for d in recent])},
        ]
    return [
        {"zone": f"第{pos + 1}位", "cells": _cells(
            [str(i) for i in range(10)],
            [[d["numbers"]["digits"][pos]] for d in recent])}
        for pos in range(spec["digits"])
    ]


def _lottery_context(conn) -> dict:
    from football_lottery.collectors.lottery_history import LOTTERIES
    from football_lottery.models import lottery_predict as lp

    lotteries = []
    for code, spec in LOTTERIES.items():
        last = store.fetchone(conn, """
            SELECT issue, draw_date, numbers, prizes FROM lottery_draw
            WHERE lottery=? ORDER BY issue DESC LIMIT 1""", (code,))
        if last is None:
            lotteries.append({"code": code, "name": spec["name"], "latest": None,
                              "predictions": [], "freq": [], "backtest": [],
                              "target": None})
            continue
        history = lp.load_history(conn, code)
        target = lp.next_issue(last["issue"])
        preds = store.fetchall(conn, """
            SELECT strategy, bets FROM lottery_prediction
            WHERE lottery=? AND target_issue=? ORDER BY strategy""", (code, target))
        tests = store.fetchall(conn, """
            SELECT strategy, metrics, paired FROM lottery_backtest
            WHERE lottery=? ORDER BY strategy""", (code,))
        lotteries.append({
            "code": code, "name": spec["name"],
            "latest": {"issue": last["issue"], "draw_date": last["draw_date"],
                       "numbers": json.loads(last["numbers"]),
                       "prizes": json.loads(last["prizes"]) if last["prizes"] else []},
            "target": target,
            "predictions": [{"strategy": r["strategy"],
                             "label": lp.STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
                             "bets": json.loads(r["bets"])} for r in preds],
            "freq": _frequency_groups(history, spec),
            "backtest": [{"strategy": r["strategy"],
                          "label": lp.STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
                          "metrics": json.loads(r["metrics"]) if r["metrics"] else {},
                          "paired": json.loads(r["paired"]) if r["paired"] else None}
                         for r in tests],
        })
    return {"lotteries": lotteries}


@app.get("/lottery", response_class=HTMLResponse)
def lottery_page(request: Request):
    conn = _get_conn()
    return templates.TemplateResponse(request, "lottery.html", _lottery_context(conn))
```

- [ ] **Step 3: 写模板**

创建 `src/football_lottery/web/templates/lottery.html`：

```html
{% extends "base.html" %}
{% block title %}数字彩{% endblock %}
{% block content %}
<h2>数字彩</h2>
<p class="hint">
  大乐透 / 双色球 / 排列三 / 排列五 / 福彩3D 的历史开奖与选号推荐。
  <strong>摇奖机是独立同分布的</strong>：下面每一条策略的命中率，长期看都与随机选号
  没有区别。页面底部的回测表就是用真实历史检验这句话的 —— 请以它为准，不要以推荐号码为准。
</p>

{% for lot in lotteries %}
<h3>{{ lot.name }}{% if lot.latest %}（最新 {{ lot.latest.issue }} 期 · {{ lot.latest.draw_date }}）{% endif %}</h3>

{% if not lot.latest %}
<p class="hint">库内暂无数据。先跑 <code>collect-lottery --lottery {{ lot.code }}</code>。</p>
{% else %}

<div class="card">
  <div class="balls">
    {% if lot.latest.numbers.front %}
      {% for n in lot.latest.numbers.front %}<span class="ball front">{{ n }}</span>{% endfor %}
      <span class="ball plus">+</span>
      {% for n in lot.latest.numbers.back %}<span class="ball back">{{ n }}</span>{% endfor %}
    {% else %}
      {% for n in lot.latest.numbers.digits %}<span class="ball digit">{{ n }}</span>{% endfor %}
    {% endif %}
  </div>
</div>

{% if lot.predictions %}
<p class="hint">第 <strong>{{ lot.target }}</strong> 期推荐（每注 2 元）：</p>
<table>
  <thead><tr><th>策略</th><th>推荐号码</th></tr></thead>
  <tbody>
  {% for p in lot.predictions %}
    <tr>
      <td>{{ p.label }}</td>
      <td>
        {% for bet in p.bets %}
          <div class="balls" style="margin:3px 0">
            {% if bet.front %}
              {% for n in bet.front %}<span class="ball front">{{ n }}</span>{% endfor %}
              <span class="ball plus">+</span>
              {% for n in bet.back %}<span class="ball back">{{ n }}</span>{% endfor %}
            {% else %}
              {% for n in bet.digits %}<span class="ball digit">{{ n }}</span>{% endfor %}
            {% endif %}
          </div>
        {% endfor %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="hint">还没有第 {{ lot.target }} 期的推荐。跑 <code>predict-lottery --lottery {{ lot.code }}</code>。</p>
{% endif %}

<details>
  <summary>近 100 期频次与遗漏</summary>
  {% for group in lot.freq %}
  <p class="hint">{{ group.zone }}</p>
  <div class="table-scroll">
  <table>
    <tbody>
      <tr><td class="hint">号码</td>
        {% for c in group.cells %}<td class="freq-cell">{{ c.value }}</td>{% endfor %}</tr>
      <tr><td class="hint">次数</td>
        {% for c in group.cells %}
          <td class="freq-cell {% if c.count >= 8 %}freq-hot{% elif c.count >= 5 %}freq-warm{% elif c.count >= 2 %}freq-cool{% else %}freq-cold{% endif %}">{{ c.count }}</td>
        {% endfor %}</tr>
      <tr><td class="hint">遗漏</td>
        {% for c in group.cells %}<td class="freq-cell">{{ c.gap }}</td>{% endfor %}</tr>
    </tbody>
  </table>
  </div>
  {% endfor %}
</details>

{% if lot.backtest %}
<details>
  <summary>回测结果（逐期走查，每注 2 元）</summary>
  <div class="table-scroll">
  <table>
    <thead><tr>
      <th>策略</th><th>期数</th><th>投入</th><th>返还</th><th>返还率</th>
      <th>中奖期占比</th><th>vs 随机 p 值</th>
    </tr></thead>
    <tbody>
    {% for row in lot.backtest %}
      <tr>
        <td>{{ row.label }}</td>
        <td>{{ row.metrics.draws }}</td>
        <td>¥{{ "%.0f" | format(row.metrics.invested or 0) }}</td>
        <td>¥{{ "%.0f" | format(row.metrics.returned or 0) }}</td>
        <td>{{ "%.1f%%" | format((row.metrics.return_rate or 0) * 100) }}</td>
        <td>{{ "%.1f%%" | format((row.metrics.win_rate or 0) * 100) }}</td>
        <td>
          {% if row.strategy == "random" %}<span class="hint">基线</span>
          {% elif row.paired %}
            p={{ "%.3f" | format(row.paired.p) }}
            {% if row.paired.beats_random %}<span class="ok">显著</span>
            {% else %}<span class="hint">无差异</span>{% endif %}
          {% else %}<span class="hint">—</span>{% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  <p class="hint">
    「vs 随机 p 值」是同一期上「该策略收益 − 随机收益」的配对 t 检验。
    p &gt; 0.05 表示两者不可区分 —— 对摇奖机而言这是预期结果。
    同时跑了多条策略，α=0.05 下每 20 次检验平均出现一次假显著。
    <br>若某策略显示「显著」，请先怀疑回测代码有泄漏，而不是号码真的能算出来。
  </p>
</details>
{% else %}
<p class="hint">还没有回测结果。跑 <code>backtest-lottery --lottery {{ lot.code }}</code>。</p>
{% endif %}

{% endif %}
{% endfor %}
{% endblock %}
```

- [ ] **Step 4: 导航加 tab**

`base.html` 的 `回测报告` 那一行之后追加：

```html
  <a href="/lottery"  {% if path == '/lottery'  %}class="active"{% endif %}>数字彩</a>
```

- [ ] **Step 5: 写页面测试**

创建 `tests/test_web_lottery.py`（用 FastAPI TestClient；项目里 `tests/test_web_api.py`
已有同款写法，照抄它的 client 构造方式）：

```python
import json
import sqlite3

import pytest

from football_lottery.db import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小可用的 /lottery 页面：空库 + 一期真实结构的数据。"""
    db = tmp_path / "t.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.execute(
        """INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes)
           VALUES('p3','2020001','2020-01-01',?,?)""",
        (json.dumps({"digits": ["0", "6", "4"]}),
         json.dumps([{"tier": "直选", "cond": "直选", "winners": 100,
                      "amount": 1040}], ensure_ascii=False)))
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    from football_lottery.web import app as web_app
    from fastapi.testclient import TestClient
    return TestClient(web_app.app)


def test_lottery_page_renders_empty_and_filled(client):
    resp = client.get("/lottery")
    assert resp.status_code == 200
    body = resp.text
    assert "数字彩" in body
    assert "大乐透" in body and "福彩3D" in body
    assert "摇奖机是独立同分布的" in body


def test_lottery_page_shows_next_issue_hint(client):
    body = client.get("/lottery").text
    assert "2020002" in body          # 库里只有 2020001，下一期就是它
```

- [ ] **Step 6: 跑测试 + 起服务手工验证**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web_lottery.py -v`
Expected: PASS

Run: `.venv/Scripts/python.exe -m football_lottery.cli serve`
浏览器打开 `http://127.0.0.1:8000/lottery`，确认：
1. 五个彩种都渲染出来，无数据时显示提示而不是报错
2. 导航栏「数字彩」高亮，且点击能跳转
3. 号码球颜色正确（前区红、后区蓝、数字绿）

- [ ] **Step 7: 提交**

```bash
git add src/football_lottery/web/ tests/test_web_lottery.py
git commit -m "feat(web): 数字彩页面（开奖/推荐/频次遗漏/回测表）"
```

---

### Task 7: 回填真实数据并出结论

**Files:** 无代码改动（除非发现 bug）

- [ ] **Step 1: 回填**

Run: `.venv/Scripts/python.exe -m football_lottery.cli collect-lottery --lottery all --from 2020`
Expected: 五类彩种各打印进度与最终期数。耗时 5–15 分钟。
**这一步会真的向第三方站点发起约 8000 次请求** —— 是设计内的一次性回填，不是测试。
并发固定 6，不加码。

- [ ] **Step 2: 体检**

```bash
.venv/Scripts/python.exe -c "
import sqlite3, json
conn = sqlite3.connect('data/football.db'); conn.row_factory = sqlite3.Row
for r in conn.execute('''SELECT lottery, COUNT(*) n, MIN(issue) a, MAX(issue) b
                         FROM lottery_draw GROUP BY lottery ORDER BY lottery'''):
    print(dict(r))
print('奖级表为空的行数：')
for r in conn.execute('''SELECT lottery, COUNT(*) n FROM lottery_draw
                         WHERE prizes IS NULL OR prizes IN ('[]','') GROUP BY lottery'''):
    print(dict(r))
"
```
Expected: 每彩种 1000–2500 期；`prizes == '[]'` 的行数应当为 0
（注意：`upsert_draw` 写的是 `json.dumps(prizes or [])`，所以空值是字符串 `'[]'`
而不是 SQL NULL —— 按 NULL 查会永远查不到东西）。

- [ ] **Step 3: 预测下一期**

Run: `.venv/Scripts/python.exe -m football_lottery.cli predict-lottery --lottery all`
Expected: 打印五类彩种下一期各策略推荐号码。

- [ ] **Step 4: 回测**

Run: `.venv/Scripts/python.exe -m football_lottery.cli backtest-lottery --lottery all`
Expected: 打印五张对比表。**预期结论是各策略与 `random` 无显著差异，返还率接近各自
理论返奖率**（大乐透约 51%、双色球约 51%、排列三/3D 直选 52%、排列五 50%）。
若某彩种返还率明显偏离理论值（比如 >60%），那不是运气，是 bug。

- [ ] **Step 5: 若出现「显著」**

按 `superpowers:systematic-debugging` 走：先查走查游标是否越界、`prizes` 是否串期、
奖金是否重复计入、`predict_bets` 是否拿到了未来的 history。
**不允许**在没查清的情况下把结论写成「找到了规律」。

- [ ] **Step 6: 把结论写进文档**

把实际数字（各彩种返奖率、p 值区间、与理论值的偏差）写进新建的
`docs/数字彩回测结论.md`，然后提交。

---

## 完成标准

- [ ] `pytest` 全绿（含既有 197 项，无回归）
- [ ] 五类彩种各 ≥1000 期入库，奖级表齐全
- [ ] `/lottery` 页面在浏览器中可用
- [ ] 回测跑通，且结论以数据呈现（而不是以「模型有多聪明」呈现）
