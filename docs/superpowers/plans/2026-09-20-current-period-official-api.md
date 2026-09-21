# 当期对阵与历史开奖接入官方接口 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Web 页面显示真实的当期胜负彩 14 场对阵（而非 2026-05 的样本期次 26080），并把近 4 年历史开奖（对阵 + 赛果 + 奖金）入库。

**Architecture:** 新增 `sporttery.py` 的三层结构——抓取（`fetch_*`）、解析（`parse_*`）、入库（`upsert_*`）——由 CLI 的 `collect-period` / `collect-draws` 驱动。Web 层的期次选取从"取 DB 里最新一条"改为"取真正在售的 current 期次，否则退到最新 historical 并明确标注"。赔率沿用既有 `map-fixtures` 映射到 football-data。

**Tech Stack:** Python 3.13、httpx、SQLite（标准库 sqlite3）、FastAPI + 原生 JS、pytest。运行环境：`.venv\Scripts\python.exe`，src 布局，`PYTHONPATH=src` 或已编辑安装。

**设计文档：** `docs/superpowers/specs/2026-09-20-current-period-official-api-design.md`

---

## 重要前提与已知边界（实现前必读）

1. **官方接口不提供赔率**，`matchList[].h/d/a` 恒为空字符串。赔率只来自 football-data。
2. **当期 14 场将拿不到 DC/GBDT 预测**：`matches` 表 15868 场全部有赛果、无未赛比赛，赛季止于 `2025/2026`（最新 `2026-05-24`）。当期对阵经映射后 `match_id` 必为 `NULL`。这是本轮的**已知交付边界**，不是 bug。
3. **`Fusion.predict` 在三个输入全为 `None` 时返回均匀 `[1/3,1/3,1/3]`**，页面若直接渲染会产生"假 33.3%"。必须有抑制逻辑。
4. **队名必须取全名字段** `masterTeamAllName` / `guestTeamAllName`（实测命中 20/28，简称仅 16/28）。
5. 两个 `odds_json` 位置不同：`period_matches.odds_json`（预测读）vs `matches.odds_json`（页面显示回退读）。
6. 官方接口需要 `User-Agent` + `Referer`，但**无需认证/签名**。
7. `E0001` 是"接口不存在/参数不合法"的通用码，**不是反爬**。按期号查对阵必须同时给 `lotteryGameNum` 与 `lotteryDrawNum`，缺一返回 `P0001`。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/lottery_lab/db/store.py` | 加幂等轻量迁移（补 `league_cn` 列） | 修改 |
| `src/lottery_lab/collectors/sporttery.py` | 官方接口抓取 / 解析 / 入库三层 | 修改（追加） |
| `src/lottery_lab/cli.py` | `collect-period` / `collect-draws` 接通；`data_health` 补当期信息 | 修改 |
| `src/lottery_lab/web/app.py` | 期次选取逻辑修复 + 透传期次状态与 `league_cn` | 修改 |
| `src/lottery_lab/web/templates/predict.html` | 展示期次状态/截止时间；抑制假 33.3% | 修改 |
| `config.yaml` | `seasons` 增加 `2627` | 修改 |
| `tests/fixtures/sporttery_*.json` | 离线测试用的真实响应 | 新建 |
| `tests/collectors/test_sporttery_official.py` | 解析 / 入库 / 异常测试 | 新建 |
| `tests/collectors/test_fixture_match_odds.py` | `odds_json` 不被清空的回归测试 | 新建 |
| `tests/test_web_period_selection.py` | 期次选取与状态透传测试 | 新建 |

**不修改**：`models/`、`optimizer/`、`backtest/`、`features/` 下的任何模块。

---

## Task 0: 准备测试 fixture（离线测试的前提）

**Files:**
- Create: `tests/fixtures/sporttery_saleinfo_90.json`
- Create: `tests/fixtures/sporttery_bydraw_26131.json`
- Create: `tests/fixtures/sporttery_history_90.json`

这些文件是**真实响应**的副本，已抓取并保存在仓库的 `.tmp/probe/` 下。不要伪造数据——测试的价值就在于用真实结构。

- [ ] **Step 1: 复制已抓取的真实响应到 fixtures**

```bash
cd D:/project/lottery-lab
mkdir -p tests/fixtures
cp .tmp/probe/sale_90.json        tests/fixtures/sporttery_saleinfo_90.json
cp .tmp/probe/bydraw_26131.json   tests/fixtures/sporttery_bydraw_26131.json
cp .tmp/probe/hist_90_30.json     tests/fixtures/sporttery_history_90.json
```

若 `.tmp/probe/` 已被清理，用以下命令重新抓取：

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import httpx, json, pathlib
H={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36','Referer':'https://www.sporttery.cn/ctzc/kjgg/','Origin':'https://www.sporttery.cn','Accept':'application/json, text/plain, */*'}
out=pathlib.Path('tests/fixtures'); out.mkdir(parents=True, exist_ok=True)
B='https://webapi.sporttery.cn/gateway/lottery/'
jobs=[('sporttery_saleinfo_90.json', B+'getLottoSaleInfoV1.qry?param=90,0'),
      ('sporttery_bydraw_26131.json', B+'getFootBallDrawInfoByDrawNumV2.qry?isVerify=1&lotteryGameNum=90&lotteryDrawNum=26131'),
      ('sporttery_history_90.json', B+'getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=30&isVerify=1&pageNo=1')]
for name,url in jobs:
    r=httpx.get(url,timeout=25,headers=H)
    out.joinpath(name).write_text(json.dumps(r.json(),ensure_ascii=False,indent=2),encoding='utf-8')
    print('saved',name)
"
```

- [ ] **Step 2: 校验 fixture 内容符合预期**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import json
s=json.load(open('tests/fixtures/sporttery_saleinfo_90.json',encoding='utf-8'))
d=json.load(open('tests/fixtures/sporttery_bydraw_26131.json',encoding='utf-8'))
h=json.load(open('tests/fixtures/sporttery_history_90.json',encoding='utf-8'))
print('sale period_no =', s['value'][0]['lotteryDrawNum'])
print('detail period_no =', d['value']['lotteryDrawNum'], 'matches =', len(d['value']['matchList']))
print('first fixture teams =', d['value']['matchList'][0]['masterTeamAllName'], 'vs', d['value']['matchList'][0]['guestTeamAllName'])
print('history total =', h['value']['total'], 'page items =', len(h['value']['list']))
print('history[0] period =', h['value']['list'][0]['lotteryDrawNum'], 'result =', h['value']['list'][0]['lotteryDrawResult'][:12])
"
```

Expected 输出（数字须一致；期号可能因抓取时间不同而变，但结构必须一致）：

```
sale period_no = 26131
detail period_no = 26131 matches = 14
first fixture teams = 伯恩茅斯 vs 利物浦
history total = 3328 page items = 30
history[0] period = 26129 result = 3 3 3 1 3 3 0 3 0 3
```

- [ ] **Step 3: 提交 fixtures**

```bash
cd D:/project/lottery-lab
git add tests/fixtures/sporttery_saleinfo_90.json tests/fixtures/sporttery_bydraw_26131.json tests/fixtures/sporttery_history_90.json
git commit -m "test(fixtures): 官方 webapi 真实响应样本（当期/按期号/历史）"
```

---

## Task 1: `league_cn` 列的幂等迁移

**Files:**
- Modify: `src/lottery_lab/db/store.py`
- Test: `tests/db/test_store_migration.py`（新建）

`schema.sql` 用 `CREATE TABLE IF NOT EXISTS`，对已存在的表不会加列。需要一个幂等迁移函数，否则老库（当前 `data/football.db`）跑起来会报 "no such column: league_cn"。

- [ ] **Step 1: 写失败的测试**

Create `tests/db/test_store_migration.py`：

```python
from lottery_lab.db import store


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


OLD_SCHEMA_NO_LEAGUE = """
CREATE TABLE periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_no TEXT NOT NULL UNIQUE,
  draw_date TEXT, sale_end TEXT, status TEXT);
CREATE TABLE period_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  seq INTEGER NOT NULL,
  home_name_cn TEXT NOT NULL, away_name_cn TEXT NOT NULL,
  match_time TEXT,
  match_id INTEGER,
  odds_json TEXT,
  UNIQUE(period_id, seq));
"""


def test_ensure_columns_adds_league_cn_idempotently():
    # 手工构造"迁移前"的旧库：init_db 现在会连带迁移，
    # 所以不能用 init_db 来验证迁移本身。
    conn = store.connect(":memory:")
    conn.executescript(OLD_SCHEMA_NO_LEAGUE)
    assert "league_cn" not in _columns(conn, "period_matches")

    store.ensure_columns(conn)
    assert "league_cn" in _columns(conn, "period_matches")

    # 再跑一次不得报错（幂等）
    store.ensure_columns(conn)
    assert "league_cn" in _columns(conn, "period_matches")


def test_ensure_columns_preserves_existing_rows():
    conn = store.connect(":memory:")
    conn.executescript(OLD_SCHEMA_NO_LEAGUE)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26080','2026-05-25','historical')"
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn)
           VALUES(?, 1, '伯恩利', '狼队')""",
        (pid,),
    )
    conn.commit()

    store.ensure_columns(conn)

    row = conn.execute(
        "SELECT home_name_cn, league_cn FROM period_matches WHERE period_id=?", (pid,)
    ).fetchone()
    assert row["home_name_cn"] == "伯恩利"
    assert row["league_cn"] is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/db/test_store_migration.py -v
```
Expected: FAIL —— `AttributeError: module 'lottery_lab.db.store' has no attribute 'ensure_columns'`

- [ ] **Step 3: 实现 `ensure_columns`**

在 `src/lottery_lab/db/store.py` 末尾追加（保留现有 `connect` / `init_db` / `upsert` / `fetchone` / `fetchall` 不动）：

```python
# schema.sql 用 CREATE TABLE IF NOT EXISTS，无法给已存在的表补列；
# 这里集中做幂等的轻量迁移。
_COLUMN_MIGRATIONS = {
    "period_matches": [("league_cn", "TEXT")],
}


def ensure_columns(conn: sqlite3.Connection) -> None:
    """补齐 schema 演进新增的列（幂等，可重复调用）。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
```

同时把新列补进 `schema.sql` 的 `period_matches` 定义，让全新库一次建好（老库仍靠 `ensure_columns`）：

`src/lottery_lab/db/schema.sql`，把

```sql
CREATE TABLE IF NOT EXISTS period_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  seq INTEGER NOT NULL,
  home_name_cn TEXT NOT NULL, away_name_cn TEXT NOT NULL,
  match_time TEXT,
  match_id INTEGER REFERENCES matches(id),
  odds_json TEXT,
  UNIQUE(period_id, seq));
```

改为（在 `odds_json` 后加一行）：

```sql
CREATE TABLE IF NOT EXISTS period_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  seq INTEGER NOT NULL,
  home_name_cn TEXT NOT NULL, away_name_cn TEXT NOT NULL,
  match_time TEXT,
  match_id INTEGER REFERENCES matches(id),
  odds_json TEXT,
  league_cn TEXT,
  UNIQUE(period_id, seq));
```

并在 `store.init_db` 末尾调用 `ensure_columns`，确保任何 `_connect` 路径都自动迁移：

```python
def init_db(conn: sqlite3.Connection) -> None:
    """执行 schema.sql 建表（幂等）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_columns(conn)
    conn.commit()
```

（注意：`ensure_columns` 定义在文件下方，但 Python 在调用时才解析名字，运行时已定义，无需调整顺序。）

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/db/test_store_migration.py -v
```
Expected: 2 passed

- [ ] **Step 5: 对现有真实库跑一次迁移并验证**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from lottery_lab.db import store
conn = store.connect('data/football.db')
store.init_db(conn)
cols = [r['name'] for r in conn.execute('PRAGMA table_info(period_matches)')]
print('period_matches columns:', cols)
assert 'league_cn' in cols
print('OK')
"
```
Expected: 打印的列里含 `league_cn`，且 `OK`。

- [ ] **Step 6: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/db/store.py src/lottery_lab/db/schema.sql tests/db/test_store_migration.py
git commit -m "feat(db): period_matches 增加 league_cn 列与幂等迁移"
```

---

## Task 2: 抓取层 —— `CollectorError` 与 `fetch_*`

**Files:**
- Modify: `src/lottery_lab/collectors/sporttery.py`
- Test: `tests/collectors/test_sporttery_official.py`（新建）

先做抓取层的**错误处理**，因为它是其余部分的基础。注意：这里只测试 `CollectorError` 的构造与 `_get_json` 的错误分支（用 monkeypatch 打桩 httpx），**不发真实网络请求**。

- [ ] **Step 1: 写失败的测试**

Create `tests/collectors/test_sporttery_official.py`：

```python
import json
from pathlib import Path

import pytest

from lottery_lab.collectors import sporttery

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_collector_error_carries_error_code():
    err = sporttery.CollectorError("参数不合法", error_code="P0001")
    assert err.error_code == "P0001"
    assert "参数不合法" in str(err)


def test_get_json_raises_on_business_error(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"errorCode": "P0001", "errorMessage": "参数不合法", "success": False}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {"lotteryGameNum": "90"})
    assert excinfo.value.error_code == "P0001"


def test_get_json_raises_on_non_200(monkeypatch):
    class _Resp:
        status_code = 502

        def json(self):
            return {}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {})
    assert "502" in str(excinfo.value)


def test_get_json_raises_on_network_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(sporttery, "_http_get", _boom)

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery._get_json("someEndpoint.qry", {})
    assert "connection reset" in str(excinfo.value)


def test_get_json_passes_through_on_success(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"errorCode": "0", "success": True, "value": [1, 2, 3]}

    monkeypatch.setattr(sporttery, "_http_get", lambda *a, **k: _Resp())

    body = sporttery._get_json("ok.qry", {})
    assert body["value"] == [1, 2, 3]


def test_fetch_current_period_parses_onsale(monkeypatch):
    monkeypatch.setattr(
        sporttery, "_get_json",
        lambda path, params, **k: _fixture("sporttery_saleinfo_90.json"),
    )
    got = sporttery.fetch_current_period()
    assert got["period_no"] == "26131"
    assert got["sale_end"] == "2026-09-20 20:30:00"
    assert got["draw_time"].startswith("2026-09-21")


def test_fetch_current_period_returns_none_when_no_onsale(monkeypatch):
    monkeypatch.setattr(
        sporttery, "_get_json",
        lambda path, params, **k: {"errorCode": "0", "value": []},
    )
    assert sporttery.fetch_current_period() is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v
```
Expected: FAIL —— `AttributeError: module 'lottery_lab.collectors.sporttery' has no attribute 'CollectorError'`

- [ ] **Step 3: 实现抓取层**

在 `src/lottery_lab/collectors/sporttery.py` 中，把现有的 `LIVE_URLS` 常量与 `fetch_live` 函数**整体替换**为下面内容（原 `import_fixtures_csv` 保留不动）。

替换范围是文件开头的 `# ---- live API（best-effort） ----` 到 `fetch_live` 函数结束。

```python
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


def _http_get(url: str, params: dict | None, timeout: int = 20):
    """薄封装，便于测试打桩。"""
    import httpx

    return httpx.get(
        url, params=params, timeout=timeout, headers=_HEADERS, follow_redirects=True
    )


def _get_json(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """请求官方接口并返回 JSON；任何失败都抛 CollectorError。"""
    url = f"{BASE_URL}/{path}"
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v
```
Expected: 7 passed

- [ ] **Step 5: 真实验证一次抓取（联网）**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from lottery_lab.collectors import sporttery
cur = sporttery.fetch_current_period()
print('current:', cur)
detail = sporttery.fetch_period_detail(cur['period_no']) if cur else {}
print('fixtures:', len(detail.get('matchList') or []))
"
```
Expected: 打印当期期号与 14。若当前无在售期次（`cur` 为 None），属正常，跳过 detail 检查。

- [ ] **Step 6: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/collectors/sporttery.py tests/collectors/test_sporttery_official.py
git commit -m "feat(collectors): 官方 webapi 抓取层（真实接口 + CollectorError）"
```

---

## Task 3: 解析层 —— `parse_period` 与 `parse_draw_results`

**Files:**
- Modify: `src/lottery_lab/collectors/sporttery.py`
- Test: `tests/collectors/test_sporttery_official.py`（追加）

解析层把官方响应转成入库结构，是纯函数，最容易测。

- [ ] **Step 1: 写失败的测试**

在 `tests/collectors/test_sporttery_official.py` 末尾追加：

```python
def test_parse_period_uses_full_team_names_and_normalizes_spaces():
    detail = _fixture("sporttery_bydraw_26131.json")["value"]
    parsed = sporttery.parse_period(detail, status="current")

    assert parsed["period"]["period_no"] == "26131"
    assert parsed["period"]["status"] == "current"
    assert parsed["period"]["sale_end"] == "2026-09-20 20:30:00"
    assert parsed["period"]["draw_date"] == "2026-09-21"
    assert len(parsed["fixtures"]) == 14

    by_seq = {f["seq"]: f for f in parsed["fixtures"]}
    assert by_seq[1]["home_name_cn"] == "伯恩茅斯"
    assert by_seq[1]["away_name_cn"] == "利物浦"
    assert by_seq[1]["league_cn"] == "英超"
    # 官方简称含空格填充（如 "利  兹"），全名不得残留连续或全角空格
    for fx in parsed["fixtures"]:
        assert fx["home_name_cn"] == " ".join(fx["home_name_cn"].split())
        assert fx["away_name_cn"] == " ".join(fx["away_name_cn"].split())
        assert fx["home_name_cn"] and fx["away_name_cn"]


def test_parse_period_rejects_non_14_fixtures():
    detail = _fixture("sporttery_bydraw_26131.json")["value"]
    detail = dict(detail)
    detail["matchList"] = detail["matchList"][:13]

    with pytest.raises(sporttery.CollectorError) as excinfo:
        sporttery.parse_period(detail, status="current")
    assert "14" in str(excinfo.value)


def test_parse_period_rejects_bad_seq():
    detail = _fixture("sporttery_bydraw_26131.json")["value"]
    detail = dict(detail)
    broken = [dict(m) for m in detail["matchList"]]
    broken[3]["matchNum"] = 99
    detail["matchList"] = broken

    with pytest.raises(sporttery.CollectorError):
        sporttery.parse_period(detail, status="current")


def test_parse_draw_results_converts_result_format_and_prizes():
    item = _fixture("sporttery_history_90.json")["value"]["list"][0]
    got = sporttery.parse_draw_results(item)

    assert got is not None
    parts = got["results_json"].split(",")
    assert len(parts) == 14
    assert all(p in {"0", "1", "3"} for p in parts)
    assert " " not in got["results_json"]

    prizes = json.loads(got["prizes_json"])
    assert isinstance(prizes["first"], float)
    assert isinstance(prizes["second"], float)
    assert prizes["second"] != "26,526"


def test_parse_draw_results_returns_none_when_undrawn():
    item = _fixture("sporttery_bydraw_26131.json")["value"]
    assert sporttery.parse_draw_results(item) is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v -k parse
```
Expected: FAIL —— `AttributeError: ... has no attribute 'parse_period'`

- [ ] **Step 3: 实现解析层**

在 `src/lottery_lab/collectors/sporttery.py` 中，紧跟 `fetch_history_page` 之后追加：

```python
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
    results_json 为逗号分隔的 14 个 0/1/3；prizes_json 形如
    {"first": …, "second": …, "r9": …}（缺失的键不写入）。
    """
    raw = (detail.get("lotteryDrawResult") or "").strip()
    results = [x for x in raw.split() if x]
    if len(results) != 14 or any(x not in {"0", "1", "3"} for x in results):
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v
```
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/collectors/sporttery.py tests/collectors/test_sporttery_official.py
git commit -m "feat(collectors): 官方响应解析层（全名队名/14 场校验/奖金转换）"
```

---

## Task 4: 入库层 —— `upsert_period` 与 `collect_history`

**Files:**
- Modify: `src/lottery_lab/collectors/sporttery.py`
- Test: `tests/collectors/test_sporttery_official.py`（追加）

入库层有两个必须保证的性质：**幂等**（重复跑不产生重复行）和**不破坏已有映射**（重跑 `collect-period` 不能把 `match_id` / `odds_json` 清空）。

- [ ] **Step 1: 写失败的测试**

在 `tests/collectors/test_sporttery_official.py` 末尾追加：

```python
def _memory_db():
    from lottery_lab.db import store

    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_upsert_period_writes_period_and_14_fixtures():
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )

    pid = sporttery.upsert_period(conn, parsed)

    row = conn.execute("SELECT * FROM periods WHERE id=?", (pid,)).fetchone()
    assert row["period_no"] == "26131"
    assert row["status"] == "current"
    assert row["sale_end"] == "2026-09-20 20:30:00"

    fixtures = list(conn.execute(
        "SELECT seq, home_name_cn, league_cn FROM period_matches WHERE period_id=? ORDER BY seq", (pid,)
    ))
    assert len(fixtures) == 14
    assert fixtures[0]["league_cn"] == "英超"


def test_upsert_period_is_idempotent():
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )

    sporttery.upsert_period(conn, parsed)
    sporttery.upsert_period(conn, parsed)

    assert conn.execute("SELECT COUNT(*) c FROM periods").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"] == 14


def test_upsert_period_preserves_existing_match_mapping():
    """重跑 collect-period 不得清空已映射的 match_id / odds_json。"""
    conn = _memory_db()
    parsed = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )
    pid = sporttery.upsert_period(conn, parsed)

    conn.execute(
        """UPDATE period_matches SET match_id=12345, odds_json='{"avg":{"h":2.0}}'
           WHERE period_id=? AND seq=1""",
        (pid,),
    )
    conn.commit()

    sporttery.upsert_period(conn, parsed)

    row = conn.execute(
        "SELECT match_id, odds_json FROM period_matches WHERE period_id=? AND seq=1", (pid,)
    ).fetchone()
    assert row["match_id"] == 12345
    assert row["odds_json"] == '{"avg":{"h":2.0}}'


def test_upsert_period_demotes_previous_current():
    conn = _memory_db()
    first = sporttery.parse_period(
        _fixture("sporttery_bydraw_26131.json")["value"], status="current"
    )
    sporttery.upsert_period(conn, first, demote_others=True)

    other = json.loads(json.dumps(first))
    other["period"]["period_no"] = "26132"
    sporttery.upsert_period(conn, other, demote_others=True)

    rows = {r["period_no"]: r["status"] for r in conn.execute("SELECT period_no, status FROM periods")}
    assert rows == {"26131": "historical", "26132": "current"}


def test_collect_history_writes_only_complete_periods(monkeypatch):
    """fixture 中 26127/26106 的赛果含 '*'（取消场次），整期跳过。

    数字 28/2 取自 fixtures/sporttery_history_90.json（共 30 期）。
    若重新抓取 fixture 导致期数变化，按实际值调整断言。
    """
    conn = _memory_db()
    page = _fixture("sporttery_history_90.json")["value"]
    total = len(page["list"])

    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: page if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    out = sporttery.collect_history(conn, years=4)

    assert out["periods_saved"] == 28
    assert out["skipped"] == 2
    assert out["periods_saved"] + out["skipped"] == total

    # 入库的期次必有开奖（对阵 / 期次 / 开奖 三者数量一致）
    assert conn.execute("SELECT COUNT(*) c FROM periods").fetchone()["c"] == 28
    assert conn.execute("SELECT COUNT(*) c FROM draw_results").fetchone()["c"] == 28
    assert conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"] == 28 * 14
    assert conn.execute("SELECT COUNT(*) c FROM periods WHERE status='current'").fetchone()["c"] == 0

    prizes = conn.execute("SELECT prizes_json FROM draw_results LIMIT 1").fetchone()["prizes_json"]
    assert prizes and "first" in json.loads(prizes)


def test_collect_history_counts_short_match_lists_as_skipped(monkeypatch):
    conn = _memory_db()
    page = _fixture("sporttery_history_90.json")["value"]
    broken = json.loads(json.dumps(page))
    broken["list"][0]["matchList"] = broken["list"][0]["matchList"][:13]

    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: broken if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    out = sporttery.collect_history(conn, years=4)

    # 1 条对阵残缺 + 2 条赛果含 '*' = 3
    assert out["skipped"] == 3
    assert out["periods_saved"] == len(page["list"]) - 3
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v -k "upsert or history"
```
Expected: FAIL —— `AttributeError: ... has no attribute 'upsert_period'`

- [ ] **Step 3: 实现入库层**

先在 `src/lottery_lab/collectors/sporttery.py` 顶部 import 区补 `import time`（现有已有 `csv / io / json / sqlite3 / datetime / pathlib / typing`，加 `import time`）。

然后追加：

```python
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

            # 先解析开奖再决定是否入库：官方对取消/延期的场次用 '*' 占位
            # （实测 26127、26106 即如此），此类期次赛果不完整，整期跳过，
            # 保证"入库的期次必有 draw_results"。
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_sporttery_official.py -v
```
Expected: 18 passed

- [ ] **Step 5: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/collectors/sporttery.py tests/collectors/test_sporttery_official.py
git commit -m "feat(collectors): 入库层（幂等 + 保留既有映射 + 历史翻页）"
```

---

## Task 5: CLI 接通 `collect-period` / `collect-draws`

**Files:**
- Modify: `src/lottery_lab/cli.py:92-115`（`cmd_collect_period` / `cmd_collect_draws`）
- Modify: `src/lottery_lab/cli.py`（argparse 的 `collect-draws` 增加 `--years`）
- Test: `tests/test_cli_collect.py`（新建）

- [ ] **Step 1: 写失败的测试**

Create `tests/test_cli_collect.py`：

```python
import json
from pathlib import Path

import pytest

from lottery_lab import cli
from lottery_lab.collectors import sporttery
from lottery_lab.db import store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


@pytest.fixture
def cfg():
    return {}


@pytest.fixture
def patched(monkeypatch, conn):
    # 仓库惯例：patch cli._connect（见 tests/test_check_draw.py）。
    # 不能依赖 _load_config —— cli.main 会去读真实 config.yaml 并写真实库。
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    monkeypatch.setattr(
        sporttery, "fetch_current_period",
        lambda: {"period_no": "26131", "sale_end": "2026-09-20 20:30:00",
                 "draw_time": "2026-09-21 14:00:00"},
    )
    monkeypatch.setattr(
        sporttery, "fetch_period_detail",
        lambda period_no: _fixture("sporttery_bydraw_26131.json")["value"],
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)
    return conn


def test_collect_period_writes_current_period(patched, capsys):
    rc = cli.main(["collect-period"])
    assert rc == 0

    row = patched.execute("SELECT * FROM periods WHERE period_no='26131'").fetchone()
    assert row["status"] == "current"
    assert row["sale_end"] == "2026-09-20 20:30:00"
    assert patched.execute(
        "SELECT COUNT(*) c FROM period_matches WHERE period_id=?", (row["id"],)
    ).fetchone()["c"] == 14

    out = json.loads(capsys.readouterr().out)
    assert out["period_no"] == "26131"
    assert out["fixtures"] == 14


def test_collect_period_reports_when_no_onsale(monkeypatch, conn):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    monkeypatch.setattr(sporttery, "fetch_current_period", lambda: None)
    assert cli.main(["collect-period"]) == 2


def test_collect_period_reports_collector_error(monkeypatch, conn, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)

    def _boom():
        raise sporttery.CollectorError("接口错误 P0001: 参数不合法", error_code="P0001")

    monkeypatch.setattr(sporttery, "fetch_current_period", _boom)
    assert cli.main(["collect-period"]) == 2
    assert "P0001" in capsys.readouterr().err


def test_collect_draws_writes_history(monkeypatch, conn, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: conn)
    page = _fixture("sporttery_history_90.json")["value"]
    monkeypatch.setattr(
        sporttery, "fetch_history_page",
        lambda n, page_size=100: page if n == 1 else {"list": [], "pages": 1},
    )
    monkeypatch.setattr(sporttery.time, "sleep", lambda *_: None)

    assert cli.main(["collect-draws", "--years", "4"]) == 0

    assert conn.execute("SELECT COUNT(*) c FROM draw_results").fetchone()["c"] == 28
    out = json.loads(capsys.readouterr().out)
    assert out["periods_saved"] == 28
    assert out["skipped"] == 2
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_cli_collect.py -v
```
Expected: FAIL —— `collect-period` 返回 2 或断言失败（当前实现只探测、不入库）

- [ ] **Step 3: 实现 CLI**

把 `src/lottery_lab/cli.py` 中的 `cmd_collect_period` 与 `cmd_collect_draws` 整体替换为：

```python
def cmd_collect_period(args, cfg: dict) -> int:
    """采集当期对阵：官方接口取期号 + 14 场对阵并入库。"""
    if args.file:
        return cmd_import_fixtures(args, cfg)
    from lottery_lab.collectors import sporttery
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
    from lottery_lab.collectors import sporttery
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
```

在 `build_parser()` 中，把 `collect-draws` 的注册改为带 `--years`：

```python
    s = sub.add_parser("collect-draws", help="获取历史开奖；无网络时用 CSV 兜底")
    s.add_argument("--file", help="本地开奖 CSV（与 import-fixtures 格式相同）")
    s.add_argument("--years", type=int, default=4, help="回溯年数；默认 4 年")
```

同时在文件顶部 docstring 的子命令列表里把 `collect-draws` 的描述更新为"官方接口取历史开奖"。

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_cli_collect.py -v
```
Expected: 4 passed

- [ ] **Step 5: 真实跑一次（联网）**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli collect-period
```
Expected: 输出 JSON，含 `"period_no": "26131"`（或当前在售期号）与 `"fixtures": 14`。

- [ ] **Step 6: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/cli.py tests/test_cli_collect.py
git commit -m "feat(cli): collect-period/collect-draws 接通官方接口（含 --years）"
```

---

## Task 6: `data_health` 输出当期信息

**Files:**
- Modify: `src/lottery_lab/cli.py`（`data_health` 函数）
- Test: `tests/test_cli_health.py`（追加）

验收标准 #1 依赖此项。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_cli_health.py` 末尾追加：

```python
def test_data_health_reports_current_period_and_missing_odds():
    from lottery_lab.cli import data_health
    from lottery_lab.db import store

    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES('26131', '2026-09-21', '2026-09-20 20:30:00', 'current')"""
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.executemany(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, odds_json)
           VALUES(?, ?, '主', '客', ?)""",
        [(pid, 1, None), (pid, 2, '{"avg":{"h":2.0,"d":3.0,"a":4.0}}')] + [(pid, i, None) for i in range(3, 15)],
    )
    conn.commit()

    report = data_health(conn)

    assert report["current_period"] == "26131"
    assert report["current_period_missing_odds"] == 13


def test_data_health_current_period_none_when_absent():
    from lottery_lab.cli import data_health
    from lottery_lab.db import store

    conn = store.connect(":memory:")
    store.init_db(conn)

    report = data_health(conn)

    assert report["current_period"] is None
    assert report["current_period_missing_odds"] == 0
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_cli_health.py -v -k current_period
```
Expected: FAIL —— `KeyError: 'current_period'`

- [ ] **Step 3: 实现**

在 `src/lottery_lab/cli.py` 的 `data_health` 函数中操作。该函数末尾是 `out = { ... }` 字面量（最后两个键是 `"last_match_date": last_match`）紧跟 `return out`。

在 `out = {` 这一行**之前**插入：

```python
    current_row = conn.execute(
        "SELECT id, period_no FROM periods WHERE status='current' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    current_period = current_row["period_no"] if current_row else None
    current_missing_odds = 0
    if current_row:
        for r in conn.execute(
            "SELECT odds_json FROM period_matches WHERE period_id=?", (current_row["id"],)
        ):
            if not r["odds_json"]:
                current_missing_odds += 1
            else:
                try:
                    j = _json.loads(r["odds_json"])
                except Exception:
                    j = {}
                if not any(k in j for k in ("avg", "b365", "max")):
                    current_missing_odds += 1
```

然后在 `out = { ... }` 字面量里追加两个键（既有键保持不动）：

```python
        "current_period": current_period,
        "current_period_missing_odds": current_missing_odds,
```

- [ ] **Step 4: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_cli_health.py -v
```
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/cli.py tests/test_cli_health.py
git commit -m "feat(cli): data-health 输出当期期号与缺赔率场次数"
```

---

## Task 7: Web 层期次选取修复

**Files:**
- Modify: `src/lottery_lab/web/app.py:95-159`
- Test: `tests/test_web_period_selection.py`（新建）

这是 26080 bug 的**直接根因**所在，也是本轮的核心修复。

- [ ] **Step 1: 写失败的测试**

Create `tests/test_web_period_selection.py`：

```python
import json

import pytest

from lottery_lab.db import store
from lottery_lab.web import app as web_app


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    return c


def _add_period(conn, period_no, status, sale_end, draw_date):
    conn.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES(?, ?, ?, ?)""",
        (period_no, draw_date, sale_end, status),
    )
    pid = conn.execute(
        "SELECT id FROM periods WHERE period_no=?", (period_no,)
    ).fetchone()["id"]
    conn.executemany(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, league_cn)
           VALUES(?, ?, '主队', '客队', '英超')""",
        [(pid, i) for i in range(1, 15)],
    )
    conn.commit()
    return pid


def test_prefers_onsale_current_period(conn, monkeypatch):
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["period_no"] == "26131"
    assert got["is_current"] is True
    assert got["period_status"] == "current"
    assert got["sale_end"] == "2099-01-01 20:30:00"


def test_expired_current_is_not_treated_as_current(conn, monkeypatch):
    """核心回归：销售已截止的 current 行不得再被当作当期。"""
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26129", "current", "2020-01-01 20:00:00", "2020-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["is_current"] is False
    assert got["period_no"] == "26080"  # 退到最新 historical


def test_falls_back_to_latest_historical_when_no_current(conn, monkeypatch):
    _add_period(conn, "26080", "historical", "2026-05-24 20:00:00", "2026-05-25")
    _add_period(conn, "26129", "historical", "2026-09-18 22:00:00", "2026-09-19")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["period_no"] == "26129"
    assert got["is_current"] is False
    assert got["period_status"] == "historical"


def test_period_payload_exposes_league_cn(conn, monkeypatch):
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["matches"][0]["league_cn"] == "英超"


def test_no_periods_returns_warning(conn, monkeypatch):
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_period_current()

    assert got["period_no"] is None
    assert "warning" in got


def test_predict_suppresses_fake_uniform_distribution(conn, monkeypatch):
    """无映射、无赔率时不得回传均匀 fused（否则页面显示假的 33.3%）。"""
    _add_period(conn, "26131", "current", "2099-01-01 20:30:00", "2099-01-02")
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    with open("config.yaml", encoding="utf-8") as handle:
        pass  # 仅确认工作目录；配置缺失时 api_predict 已有兜底

    got = web_app.api_predict()

    first = got["matches"][0]
    assert first["mapped"] is False
    assert first["fused"] is None
    assert first["has_prediction"] is False
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_web_period_selection.py -v
```
Expected: FAIL —— `KeyError: 'is_current'`

- [ ] **Step 3: 实现期次选取**

在 `src/lottery_lab/web/app.py` 中，用下面的 `_is_on_sale` / `_pick_period` / `_period_with_matches` **整体替换**现有的 `_period_with_matches`（第 95-146 行）。

```python
def _is_on_sale(sale_end: str | None, now: datetime | None = None) -> bool:
    """sale_end 为 'YYYY-MM-DD HH:MM:SS'；空值或解析失败视为不在售（保守）。"""
    if not sale_end:
        return False
    now = now or datetime.now()
    try:
        end = datetime.fromisoformat(sale_end.replace("/", "-").strip())
    except ValueError:
        return False
    return end >= now


def _pick_period(conn) -> tuple[sqlite3.Row, bool] | tuple[None, bool]:
    """挑期次：优先真正在售的 current；否则最新的 historical。

    返回 (row, is_current)。26080 bug 的根因就是此前无此时间判定。
    """
    rows = list(conn.execute(
        """SELECT id, period_no, status, sale_end, draw_date FROM periods
           ORDER BY COALESCE(draw_date, '') DESC, id DESC"""
    ))
    for row in rows:
        if row["status"] == "current" and _is_on_sale(row["sale_end"]):
            return row, True
    for row in rows:
        if row["status"] != "current":
            return row, False
    return None, False


def _period_with_matches(conn) -> dict | None:
    """当期（或退化的最新历史期）+ 14 场对阵，并透传期次身份。"""
    p, is_current = _pick_period(conn)
    if p is None:
        return None

    pm = list(conn.execute(
        """SELECT id, seq, home_name_cn, away_name_cn, match_id, match_time,
                  odds_json, league_cn
           FROM period_matches WHERE period_id=?
           ORDER BY seq""",
        (p["id"],),
    ))
    if len(pm) < 14:
        return None

    enriched = []
    for row in pm[:14]:
        d = {"id": row["id"], "seq": row["seq"],
             "home_cn": row["home_name_cn"], "away_cn": row["away_name_cn"],
             "match_time": row["match_time"], "match_id": row["match_id"],
             "league_cn": row["league_cn"]}
        try:
            d["odds"] = json.loads(row["odds_json"] or "{}")
        except Exception:
            d["odds"] = {}
        if row["match_id"] and not d["odds"]:
            m = conn.execute(
                """SELECT odds_json FROM matches WHERE id=?""", (row["match_id"],)
            ).fetchone()
            if m:
                try:
                    d["odds"] = json.loads(m["odds_json"] or "{}")
                except Exception:
                    d["odds"] = {}
        d["mapped"] = bool(row["match_id"])
        enriched.append(d)

    unmapped = [
        {"seq": m["seq"], "side": side, "name_cn": m[f"{side}_cn"]}
        for m in enriched if not m["mapped"]
        for side in ("home", "away")
    ]
    warnings = []
    if not is_current:
        warnings.append({
            "type": "stale_period",
            "message": f"当前显示的是历史期次 {p['period_no']}，非在售期次",
        })
    if unmapped:
        warnings.append({
            "type": "unmapped_fixture",
            "count": len(unmapped),
            "message": "部分场次尚未匹配到历史比赛，请确认球队别名后重试",
        })

    return {
        "period_id": p["id"], "period_no": p["period_no"],
        "period_status": p["status"], "sale_end": p["sale_end"],
        "is_current": bool(is_current),
        "matches": enriched,
        "unmapped": unmapped, "warnings": warnings,
    }
```

在 `src/lottery_lab/web/app.py` 顶部的 import 区确认有 `from datetime import datetime` 与 `import sqlite3`；若没有则补上（该文件已 `import json`）。

- [ ] **Step 4: 实现 `api_predict` 的抑制逻辑**

在 `src/lottery_lab/web/app.py` 的 `api_predict` 中，替换构造 `matches` 的循环体（原第 179-188 行）：

```python
    matches = []
    for m in period["matches"]:
        row = by_seq.get(m["seq"], {})
        market, dc, gbdt, fused = (row.get("market"), row.get("dc"),
                                   row.get("gbdt"), row.get("fused"))
        # 无映射且三个组件全空时，Fusion 会回退成均匀分布 [1/3,1/3,1/3]，
        # 直接展示会变成"假 33.3%"。此处抑制，交由前端显示"暂无预测"。
        has_prediction = bool(m["mapped"] or market or dc or gbdt)
        if not has_prediction:
            fused = None
        matches.append({
            "seq": m["seq"],
            "home": m["home_cn"], "away": m["away_cn"],
            "league_cn": m["league_cn"],
            "match_time": m["match_time"],
            "match_id": m["match_id"], "mapped": m["mapped"],
            "has_prediction": has_prediction,
            "market": market, "dc": dc, "gbdt": gbdt, "fused": fused,
        })
```

并把该函数末尾的返回语句（现为 `return {"period_id": ..., "model_version": result["model_version"], ...}`）替换为：

```python
    return {"period_id": period["period_id"], "period_no": period["period_no"],
            "model_version": result["model_version"],
            "period_status": period["period_status"],
            "sale_end": period["sale_end"],
            "is_current": period["is_current"],
            "warnings": period["warnings"],
            "unmapped": period["unmapped"],
            "matches": matches}
```

（原有键 `period_id` / `period_no` / `model_version` / `warnings` / `unmapped` / `matches` **全部保留**，只新增三个期次身份字段，避免破坏既有调用方与测试。）

- [ ] **Step 5: 更新被新选取逻辑打破的既有测试**

`tests/test_web_api.py` 的 `test_predict_exposes_unmapped_matches_and_components`（第 91 行起）插入的是 `status='current'` 且 **`sale_end` 为 NULL** 的期次。新 `_pick_period` 会拒绝它（current 但不在售），且第二个循环只接受非 current → 返回 `None` → `api_predict` 变成 `{"warning": "无当期数据", "matches": []}`，断言全部失败。

该测试的意图是"暴露未映射场次与各组件"，与当期身份无关，因此把期次改为 historical：

```python
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26002', '2026-01-02', 'historical')"
    )
```

**这一改动还有连带影响**：期次不再是"在售当期"，新的 `_period_with_matches` 会**在 warnings 最前面插入一条 `stale_period`**，于是同一文件第 124 行的断言

```python
    assert result["warnings"][0]["type"] == "unmapped_fixture"
```

会失败（实际为 `['stale_period', 'unmapped_fixture']`）。把它改为按顺序断言两者：

```python
    assert [w["type"] for w in result["warnings"]] == ["stale_period", "unmapped_fixture"]
```

> 注意：不要为了让这个测试通过而删掉 `stale_period` 警告 —— 那条警告正是本轮修复的核心产出（明确告知用户"当前显示的不是在售期次"），删掉等于把 bug 的可见性又抹掉了。

- [ ] **Step 6: 运行测试，确认通过**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/test_web_period_selection.py tests/test_web_api.py tests/test_cli_health.py -v
```
Expected: 全部通过（`test_web_api.py` / `test_cli_health.py` 是既有测试，必须不回归）

- [ ] **Step 7: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/web/app.py tests/test_web_period_selection.py
git commit -m "fix(web): 期次选取加入在售时间判定，修复样本期次被当作当期"
```

---

## Task 8: 前端展示修复

**Files:**
- Modify: `src/lottery_lab/web/templates/predict.html`

- [ ] **Step 1: 更新表头，加入联赛列**

把 `<thead>` 的首行改为（在"客队"后插入"联赛"）：

```html
<tr><th rowspan="2">#</th><th rowspan="2">主队</th><th rowspan="2">客队</th><th rowspan="2">联赛</th><th rowspan="2">时间</th>
  <th colspan="3">市场</th><th colspan="3">DC</th><th colspan="3">GBDT</th><th colspan="3">融合</th><th rowspan="2">状态</th></tr>
```

- [ ] **Step 2: 抑制假概率并显示期次状态**

替换 `load()` 函数中从 `const warningCount = ...` 到 `tbody.appendChild(tr);` 的整段：

```javascript
  const warningCount = (j.unmapped || []).length;
  const statusText = j.is_current ? "在售" : "历史期次";
  const saleEnd = j.sale_end ? `，本期截止 ${j.sale_end}` : "";
  info.textContent = `期号 ${j.period_no}（${statusText}${saleEnd}，${j.matches.length} 场）` +
    (warningCount ? `，${warningCount} 个球队待确认` : "");
  const tbody = document.querySelector("#predict-table tbody");
  tbody.replaceChildren();
  j.matches.forEach(m => {
    const tr = document.createElement("tr");
    appendCell(tr, m.seq);
    appendCell(tr, m.home); appendCell(tr, m.away);
    appendCell(tr, m.league_cn || "");
    appendCell(tr, m.match_time || "");
    if (m.has_prediction) {
      appendProbs(tr, m.market);
      appendProbs(tr, m.dc);
      appendProbs(tr, m.gbdt);
      appendProbs(tr, m.fused);
    } else {
      // 无映射且无赔率：不得渲染 Fusion 回退出的均匀分布（假 33.3%）
      for (let i = 0; i < 12; i++) appendCell(tr, "—");
    }
    appendCell(tr, m.mapped ? "已匹配" : (m.has_prediction ? "待确认" : "暂无预测（赔率待更新）"),
               m.mapped ? "mapped" : "warning");
    tbody.appendChild(tr);
  });
```

- [ ] **Step 3: 手工验证（启动服务）**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli serve --port 8765
```
浏览器打开 `http://127.0.0.1:8765/predict`，确认：
- 标题显示 `期号 26131（在售，本期截止 2026-09-20 20:30，14 场）`（期号以实际在售期为准）
- 表格有"联赛"列，显示"英超/德甲/意甲/西甲/法甲"
- 12 个概率格显示 `—`，状态列显示"暂无预测（赔率待更新）"
- **不出现 33.3%**

- [ ] **Step 4: 提交**

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/web/templates/predict.html
git commit -m "feat(web): 预测页显示期次状态/联赛，并抑制无数据时的假概率"
```

---

## Task 9: `fixture_match` 赔率保留的回归测试

**Files:**
- Test: `tests/collectors/test_fixture_match_odds.py`（新建）

`findings.md` 第 27 条记录的问题（`match_period` 用 `NULL` 覆写 `odds_json`）**已在当前工作区修复但尚未提交**。本任务只补回归测试锁定它，**不要重写该函数**。

> **注意**：若在全新的 git worktree 中执行本计划，工作区的这个修复不在里面，Step 2 会失败。届时需要把 `fixture_match.match_period` 内的三处 `store.upsert(... "odds_json": None ...)` 改为 `"odds_json": pm["odds_json"]`。在当前工作区直接执行则无需改动。

- [ ] **Step 1: 写测试**

Create `tests/collectors/test_fixture_match_odds.py`：

```python
from lottery_lab.collectors import fixture_match
from lottery_lab.db import store


def test_match_period_preserves_existing_odds_json():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26080','2026-05-25','historical')"
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, odds_json)
           VALUES(?, 1, '伯恩利', '狼队', '{"avg":{"h":2.36,"d":3.5,"a":2.77}}')""",
        (pid,),
    )
    conn.commit()

    fixture_match.match_period(conn, "26080")

    row = conn.execute(
        "SELECT match_id, odds_json FROM period_matches WHERE period_id=? AND seq=1", (pid,)
    ).fetchone()
    assert row["match_id"] is None          # 库里没有对应比赛，映射必然失败
    assert row["odds_json"] == '{"avg":{"h":2.36,"d":3.5,"a":2.77}}'   # 但赔率必须保留
```

- [ ] **Step 2: 运行测试**

Run:
```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/collectors/test_fixture_match_odds.py -v
```
Expected: PASS（该修复已在工作区）。若 FAIL，说明回归确实存在，此时才需要修 `fixture_match.match_period` 中三处 `store.upsert` 的 `odds_json` 取值，改为透传 `pm["odds_json"]`。

- [ ] **Step 3: 提交**

```bash
cd D:/project/lottery-lab
git add tests/collectors/test_fixture_match_odds.py
git commit -m "test(collectors): 锁定重映射不清空 period_matches.odds_json"
```

---

## Task 10: 补下 2627 赛季并按需回填

**Files:**
- Modify: `config.yaml`

本轮不解决当期预测（见"已知边界"），但补下 2627 赛季能让**已结束的近期比赛**（至 `2026-09-14`）进入 `matches` 表，改善后续期次的映射基础。

- [ ] **Step 1: `config.yaml` 加入 2627**

把：

```yaml
seasons: ["2122", "2223", "2324", "2425", "2526"]
```

改为：

```yaml
seasons: ["2122", "2223", "2324", "2425", "2526", "2627"]
```

- [ ] **Step 2: 补下载**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli collect-history
```
Expected: 分赛季输出入库条数，其中 2627 各联赛条数较小（赛季刚开始）。重复执行总数不变（幂等）。

- [ ] **Step 3: 验证**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from lottery_lab.db import store
conn = store.connect('data/football.db')
print([tuple(r) for r in conn.execute('SELECT season, COUNT(*) FROM matches GROUP BY season ORDER BY season')])
print('max date:', conn.execute('SELECT MAX(match_date) FROM matches').fetchone()[0])
"
```
Expected: 出现 `('2026/2027', N)`，且 `max date` 推进到 2026-09 中旬。

- [ ] **Step 4: 提交**

```bash
cd D:/project/lottery-lab
git add config.yaml
git commit -m "chore(config): 补下 2627 赛季（football-data 当前赛季）"
```

---

## Task 11: 端到端验收

- [ ] **Step 1: 全量测试**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: 全部通过。若出现 `tmp_path` 相关的 Windows 权限错误，属既有环境问题（见 `task_plan.md` 记载），用 `--basetemp` 指向工作区内目录重跑。

- [ ] **Step 2: 采集当期 + 历史**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli collect-period
.venv/Scripts/python.exe -m lottery_lab.cli collect-draws --years 4
.venv/Scripts/python.exe -m lottery_lab.cli data-health
```
Expected:
- `collect-period` 输出当期期号与 `fixtures: 14`
- `collect-draws --years 4` 的 `periods_saved` ≥ 600
- `data-health` 的 `current_period` 等于当期期号，`current_period_missing_odds` 为 14（当期无赔率，符合已知边界）

- [ ] **Step 3: 验证幂等**

再跑一次 `collect-period` 与 `collect-draws --years 4`，然后：

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from lottery_lab.db import store
conn = store.connect('data/football.db')
print('periods:', conn.execute('SELECT COUNT(*) FROM periods').fetchone()[0])
print('period_matches:', conn.execute('SELECT COUNT(*) FROM period_matches').fetchone()[0])
print('draw_results:', conn.execute('SELECT COUNT(*) FROM draw_results').fetchone()[0])
print('bad fixtures:', conn.execute('''SELECT COUNT(*) FROM (SELECT period_id FROM period_matches GROUP BY period_id HAVING COUNT(*)<>14)''').fetchone()[0])
"
```
Expected: 行数与第一次一致；`bad fixtures` 为 0。

- [ ] **Step 4: 启动服务供人工验收**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli serve --port 8765
```

浏览器逐页确认：
- `/predict` —— 期号 = 当期在售期号（非 26080），14 场对阵与联赛名正确，显示截止时间，无假 33.3%
- `/history` —— 历史期次与奖金可查
- 若当期已截止销售，页面应显示"历史期次"而非"在售"

- [ ] **Step 5: 对奖链路验收（设计文档 §11 #5）**

`check-draw` 对 `hit_notes <= 0` 的档位直接跳过，所以必须是**必定命中**的计划。构造一个每场只押真实结果的 14 场单式：

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys, json; sys.path.insert(0,'src')
from lottery_lab.db import store
conn = store.connect('data/football.db')
row = conn.execute('''
    SELECT p.id AS pid, p.period_no, d.results_json
    FROM periods p JOIN draw_results d ON d.period_id = p.id
    ORDER BY p.draw_date DESC, p.id DESC LIMIT 1
''').fetchone()
results = [r.strip() for r in row['results_json'].split(',')]
assert len(results) == 14, results
conn.execute(
    '''INSERT INTO plans(period_id, game_type, objective, budget, notes_count,
                         legs_json, created_at)
       VALUES(?, 'sfc14', 'acceptance', 2, 1, ?, datetime('now'))''',
    (row['pid'], json.dumps([[r] for r in results])),
)
conn.commit()
print(row['period_no'])
"
```
记下输出的期号 N，然后：

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -m lottery_lab.cli check-draw --period N
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from lottery_lab.db import store
conn = store.connect('data/football.db')
print([tuple(r) for r in conn.execute('SELECT tier, hit_notes, amount FROM winnings')])
"
```
Expected: 打印非空列表，形如 `[('first', 1, 2125090.0)]`，证明对奖链路打通。

注意 `hit_notes` 是**中奖注数**而非命中场数：本次构造的是 14 场单式（每场只押真实结果），所以恰好 1 注全中一等奖。二等奖（命中 13 场）为 0 注，`check-draw` 会跳过该档位，因此列表里只有 `first` 一行。

- [ ] **Step 6: 记录未命中队名（设计文档 §11 #7 要求）**

```bash
cd D:/project/lottery-lab
.venv/Scripts/python.exe -c "
import sys, json; sys.path.insert(0,'src')
from lottery_lab.db import store
from lottery_lab.collectors import team_alias
conn = store.connect('data/football.db')
row = conn.execute(\"SELECT id, period_no FROM periods WHERE status='current' ORDER BY id DESC LIMIT 1\").fetchone()
if not row:
    raise SystemExit('no current period; run collect-period first')
misses = []
for pm in conn.execute('SELECT seq, home_name_cn, away_name_cn FROM period_matches WHERE period_id=? ORDER BY seq', (row['id'],)):
    for side, name in (('H', pm['home_name_cn']), ('A', pm['away_name_cn'])):
        if not team_alias.resolve(conn, name, seed=team_alias.SEED):
            misses.append(f\"seq{pm['seq']} {side} {name}\")
print('period', row['period_no'], 'unresolved:', len(misses))
print('\n'.join(misses))
"
```
Expected: 打印未命中清单（预期全 28 个或近全，因 2627 赛季数据缺失）。**只需记录，不需要本轮修复**；后续可把稳定出现的队名补进 `team_alias.SEED` 或 ALIASES。

- [ ] **Step 7: 最终提交**

**不要用 `git add -A`** —— 工作区存在与本任务无关的未提交改动（`docs/实现计划.md`、`docs/回测记录.md`、`src/lottery_lab/backtest/*`、`models/fusion.py`、`optimizer/*`、若干 `*.md` 等），会被一并卷入。只提交本任务触碰的文件：

```bash
cd D:/project/lottery-lab
git add src/lottery_lab/db/store.py src/lottery_lab/db/schema.sql \
        src/lottery_lab/collectors/sporttery.py \
        src/lottery_lab/cli.py \
        src/lottery_lab/web/app.py \
        src/lottery_lab/web/templates/predict.html \
        config.yaml \
        tests/
git status --short
git commit -m "chore: 官方接口接入验收（当期对阵 + 近 4 年历史开奖）

数据文件 data/football.db 与 data/*.csv 不入库（见 .gitignore）。
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
提交前先看 `git status --short`，确认没有把无关改动带进来。

---

## 完成标准（对照设计文档 §11）

| # | 标准 | 对应任务 |
|---|---|---|
| 1 | `data-health` 输出当期期号 = 官方在售期号 | Task 6 |
| 2 | 预测页显示真实当期 14 场（含联赛）与截止时间，不再显示 26080 | Task 5, 7, 8 |
| 3 | 无预测时如实显示，不出现假 33.3% | Task 7, 8 |
| 4 | `collect-draws --years 4` 后 historical ≥ 600 且每期 14 场 + draw_results 齐备 | Task 4, 5 |
| 5 | 对已有命中计划的期次跑 `check-draw` 能产生 `winnings` | 既有功能，Task 11 手工验证 |
| 6 | 重复执行不产生重复行 | Task 4, 11 |
| 7 | 记录当期映射失败场次与未命中队名 | Task 11 Step 5 |
| 8 | 全部测试离线通过 | Task 11 Step 1 |
