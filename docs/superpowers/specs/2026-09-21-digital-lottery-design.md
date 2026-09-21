# 数字彩（大乐透/双色球/排列三/排列五/福彩3D）— 设计文档

日期：2026-09-21
状态：待评审

## 1. 背景与目标

用户要求：抓取近 6 年（2020-01-01 起）五类数字彩的开奖数据，做分析预测。
用户已被告知数字彩是摇奖机、历史号码对下一期没有信息量，仍选择「按原计划做预测」。

因此本项目的交付物是**两件事**，缺一不可：

1. **预测**：给出每类彩种下一期的推荐号码（多条策略路径，不藏私）。
2. **回测**：用 6 年历史逐期走查，量化「每条策略 vs 随机选号」的差距，并做显著性检验。

第 2 条是硬约束。没有它，第 1 条就是自欺欺人。

## 2. 数据源（已实测，勿重复调研）

**用官方接口。** 最初选的是第三方站点彩宝贝（`kaijiang.78500.cn`），
但它的单期页面是 1 请求/期（6 年约 8000 次），实测 6 并发无间隔约 1000 次请求
就被阿里云 WAF 封了整个 IP。官方接口每页 100 期，6 年只要约 85 个请求 —— 差 94 倍，
而且是权威源、JSON、无 WAF、不必处理编码与 HTML 结构漂移。78500 相关代码已删除。

| 彩种 | 来源 | 标识 | 每页 | 全历史 |
|---|---|---|---|---|
| 大乐透 | `webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry` | `gameNo=85` | 100 | 2925 期（2007 起） |
| 排列三 | 同上 | `gameNo=35` | 100 | 7728 期 |
| 排列五 | 同上 | `gameNo=350133` | 100 | 7728 期 |
| 双色球 | `www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice` | `name=ssq` | 100 | 2067 期 |
| 福彩3D | 同上 | `name=3d` | 100 | 4758 期 |

体彩网关与足彩用的是同一个，无需认证；福彩需要 `Referer: https://www.cwl.gov.cn/ygkj/wqkjgg/`。

### 2.1 字段映射

**体彩**：`lotteryDrawNum`（5 位期号，如 `26107` → 补成 `2026107`）、
`lotteryDrawResult`（空格分隔，如 `02 05 07 14 22 04 10`）、`lotteryUnsortDrawresult`（出球顺序）、
`lotteryDrawTime`、`totalSaleAmount`、`poolBalanceAfterdraw`、
`prizeLevelList[]`（`prizeLevel` 奖级名 / `stakeAmountFormat` 单注奖金 / `stakeCount` 注数）。

**福彩**：`code`（已是 7 位）、`red` / `blue`（逗号分隔）、`date`（形如 `2026-09-20(日)`）、
`sales`、`poolmoney`、`prizegrades[]`（`type` 编号 → 奖级名 / `typemoney` / `typenum`）。

### 2.2 五个必须处理的坑（都是实测撞出来的）

1. **体彩用 `-1` 表示「该项不适用」**（例如追加奖没开出）。按「去掉非数字字符」解析
   会把它变成正的 `1`，静默把哨兵值当成了奖金。要在清洗前先判负号。
2. **福彩两个彩种的 `typemoney` 口径不同**：双色球是**单注**奖金（三等奖 3000、四等奖 200
   都对得上法定值），3D 是**总奖金**（2020001 期「直选」= 17,598,680 ÷ 16,931 注 ≈ 1039.4）。
   混用会把 3D 直选当成 1759 万。3D 奖金是法定固定值，回测直接算固定值，不取这份数据。
3. **排列类的号码是空格/逗号分隔的单字符**（`2 0 2`），必须保持单个字符，
   不能拼成 `202` 再当数字处理。
4. **大乐透的奖级设置改过**：2026-02-02（第 26014 期）起由 9 个奖级改为 7 个，
   三等奖从「5+0」并入「5+0；4+2」，四等奖从「4+2」变成「4+1」。
   6 年数据横跨两套规则，判据用**当期奖级表里有没有「九等奖」**，不按日期硬编码。
5. **福彩3D 的 `prizegrades` 长期为空**（2026 年各期均为空表），3D 的奖金走固定值。

### 2.3 请求量

全量回填（`--from 2020`）实测约 85 个请求、几十秒。单线程按页抓即可，
没有并发必要 —— 也就不会重演把站点打爆那一幕。仍保留全局限速令牌桶
（默认 5 次/秒）与撞 403/429 即中止的退避逻辑，作为对任何对端的基本礼貌
与自我保护：被拦时**不换 UA、不换代理**（那是绕过反爬），只降速，降不下来就停手。

## 3. 数据模型

新增两张表（`db/schema.sql`）：

```sql
CREATE TABLE IF NOT EXISTS lottery_draw (
  lottery      TEXT NOT NULL,   -- dlt/ssq/p3/p5/3d
  issue        TEXT NOT NULL,   -- '2026107'
  draw_date    TEXT NOT NULL,   -- 'YYYY-MM-DD'
  numbers      TEXT NOT NULL,   -- JSON {"front":[2,5,7,14,22],"back":[4,10]}
                                --   或 {"digits":["0","6","4"]}（接口给的就是单字符，原样保留）
  draw_order   TEXT,            -- JSON 出球顺序，缺失为 NULL
  sales        INTEGER,         -- 本期投注金额（元）
  jackpot      INTEGER,         -- 滚入下期奖金（元）
  prizes       TEXT,            -- JSON [{"tier":"一等奖","cond":"5+2",
                                --        "winners":3,"amount":10000000}]
  PRIMARY KEY (lottery, issue)
);
CREATE INDEX IF NOT EXISTS idx_lottery_draw_date
  ON lottery_draw(lottery, draw_date);

CREATE TABLE IF NOT EXISTS lottery_prediction (
  lottery      TEXT NOT NULL,
  target_issue TEXT NOT NULL,
  strategy     TEXT NOT NULL,
  bets         TEXT NOT NULL,   -- JSON 注单列表，一注一个元素，形状同 lottery_draw.numbers
  created_at   TEXT NOT NULL,
  hits         TEXT,            -- 开奖后回填：每注命中明细的列表
  prize        INTEGER,         -- 开奖后回填：该策略该期总奖金（元）
  PRIMARY KEY (lottery, target_issue, strategy)
);

CREATE TABLE IF NOT EXISTS lottery_backtest (
  lottery  TEXT NOT NULL,
  strategy TEXT NOT NULL,
  params   TEXT,                -- JSON {window, bets, draws}
  metrics  TEXT,                -- JSON {roi, invested, returned, avg_hits, win_rate}
  paired   TEXT,                -- JSON {se, ci_low, ci_high, p, beats_random}
  ran_at   TEXT,
  PRIMARY KEY (lottery, strategy)
);
```

术语用 **`bets`（注单）**，与既有的 `jc_parlay_plans.bets_json` 保持一致。

**为什么开奖结果用 `upsert` 而不是 append-only**：开奖号码是**既成事实**，
重抓同一期应当得到完全相同的结果；若不同则是数据源修正或我方解析 bug，
覆盖写入并留日志才是正确行为。这与竞彩赔率快照（同一时刻的观测值，追加不可覆盖）
是两类数据，代码注释里要写清这个区别。

## 4. 采集器 `collectors/lottery_history.py`

```python
LOTTERIES = {
    "dlt": {"name": "大乐透", "source": "sporttery", "game_no": "85",
            "kind": "two_zone", "front": 5, "front_max": 35, "back": 2, "back_max": 12},
    "ssq": {"name": "双色球", "source": "cwl", "game_no": "ssq",
            "kind": "two_zone", "front": 6, "front_max": 33, "back": 1, "back_max": 16},
    "p3":  {"name": "排列三", "source": "sporttery", "game_no": "35",
            "kind": "digits", "digits": 3},
    "p5":  {"name": "排列五", "source": "sporttery", "game_no": "350133",
            "kind": "digits", "digits": 5},
    "3d":  {"name": "福彩3D", "source": "cwl", "game_no": "3d",
            "kind": "digits", "digits": 3},
}
```

- `parse_sporttery(item, lottery)` / `parse_cwl(item, lottery)` — 纯函数，输入接口返回的
  单条 JSON 记录返回 dict。号码个数/位数与彩种规格不符时抛 `ValueError`，绝不静默返回残缺数据。
- `fetch_page(lottery, page_no)` — 返回该页原始记录；空列表表示没有更多。
- `sync_history(conn, lottery, since_year=None, rate=None, on_progress=None)`
  - **单线程按页抓**（每页 100 期，6 年只要几十个请求），不并发 —— 也就不会把站点打爆
  - 幂等：已有期号也照样覆盖（数据源可能修正），只是计入 `skipped` 而不是 `saved`
  - 接口是倒序的，抓到比 `since_year` 更早的期号即停止翻页
  - 撞 403/429 立刻中止整轮，`stats["blocked"] = True`
- `refresh_latest(conn, lottery)` — 只刷第一页，给「预测下一期」用。
- 全局限速令牌桶 `_RateLimiter` 保护所有请求（默认 5 次/秒）。

## 5. 预测模型 `models/lottery_predict.py`

### 5.1 统一框架：权重 → 加权不放回抽样

五条路径的差别**只在权重函数**，采样过程共用：

```python
def sample_numbers(weights: dict[str, float], k: int, rng) -> list[str]:
    """Gumbel top-k：给 log(w) 加 Gumbel 噪声后取前 k 个，等价于加权不放回抽样。"""
```

| 策略 | 权重 `w_i` | 直觉 |
|---|---|---|
| `random` | 1 | 基线，均匀随机 |
| `hot` | 近 W 期出现次数 + 1 | 追热号 |
| `cold` | (W − 出现次数) + 1 | 追冷号 |
| `overdue` | 当前遗漏期数 + 1 | 追「该出了」的号 |
| `weighted` | Dirichlet(α + 频次) 后验采样出的概率 | 贝叶斯平滑版热号 |

- `dlt`/`ssq`：前区、后区**各自独立**加权采样。
- `p3`/`p5`/`3d`：**按位独立**（百位/十位/个位各一套频次），不是所有位置混在一起数频次。
- 每期每策略生成 **M 注**（默认 5 注），每注独立抽样（不同随机种子）。

### 5.2 防泄漏（硬约束）

第 t 期只能用 `issue` 严格小于 t 的历史。走查时按 `(年, 期号)` 排序推进游标，
**不允许**先把全量频次算好再切窗口——这正是足球那边踩过的坑。
测试要专门断言：篡改第 t 期开奖号码，不得改变第 t 期的预测输出。

## 6. 回测 `models/lottery_backtest.py`

逐期走查：对每一期 t，用 `< t` 的数据生成 M 注 → 与该期实际开奖比对。

### 6.1 命中判定与奖金

| 彩种 | 命中判定 | 奖金来源 |
|---|---|---|
| `dlt` | 前区命中数 + 后区命中数 → 奖级（5+2…0+2） | 当期实际奖级表；**按表里有没有「九等奖」判适用哪套规则** |
| `ssq` | 红球命中数 + 蓝球是否命中 → 奖级 | 同上 |
| `p3`/`3d` | 三位全中（**直选**） | 固定 1040 元（3D 的奖级接口为空，只能用固定值） |
| `p5` | 五位全中 | 固定 100000 元 |

单注投入统一 2 元。

**排列三/3D 只按直选计奖**。我们的预测输出是一个有序三元组，对应的是直选票。
若同时把「数字相同但顺序不同」按组选计奖，等于一注 2 元买了两种玩法：
单注期望奖金 = 1040/1000 + 6×173/1000 = 2.078 元 > 2 元，返还率会算出 **103.9%**。
超过 100% 的返还率是模型错误的铁证，不是发现。

### 6.2 指标

- `avg_hits`：每注平均命中个数（前区/后区分别记）
- `win_rate`：至少中最低奖级的比例
- `roi` = 总奖金 / 总投入
- `vs_random`：与 `random` 策略的**逐期配对**差值（同一期、同一注数）

### 6.3 显著性

逐期配对 t 检验（复用 `jc_backtest.significance` 的写法）：
对每一期算 `profit(策略) − profit(random)`，检验均值是否为 0。
输出标准误、95% 置信区间、p 值。**只有 `p < 0.05 且差值为正` 才标 `beats_random`。**

预期结论：全部策略的差值与 0 不可区分。若出现「显著」，先按 `systematic-debugging`
查泄漏与判定 bug，而不是宣布找到规律——这一点写进回测命令的输出里。

## 7. CLI

| 命令 | 作用 |
|---|---|
| `collect-lottery --lottery all --from 2020 [--rate 5]` | 同步历史（幂等，可重复跑） |
| `predict-lottery --lottery dlt [--strategy hot,weighted] [--bets 5]` | 对下一期生成推荐并存 `lottery_prediction` |
| `score-lottery` | 给已开奖的预测回填 hits/prize |
| `backtest-lottery --lottery all [--window 100] [--bets 5]` | 逐期走查 + 显著性表 |

## 8. Web 页面 `/lottery`

- 五个彩种纵向排布（每类一张卡 + 推荐 + 频次遗漏 + 回测表）
- 最新一期开奖号码（球体样式，区分前区/后区色）
- 下一期推荐号码：各策略一行
- 近 100 期号码频次 + 遗漏表
- 回测结果表：策略 × 指标（含 `beats_random` 标记与 p 值）

导航栏新增 `数字彩` tab。

## 9. 测试策略

| 文件 | 覆盖 |
|---|---|
| `tests/collectors/test_lottery_collect.py` | 五种彩种 fixture JSON 解析；期号归一（5 位→7 位）；`-1`/`---` 哨兵值不当成数字；限速与 403 中止；幂等入库 |
| `tests/models/test_lottery_predict.py` | 权重函数；Gumbel 采样；**防泄漏断言**；按位统计正确 |
| `tests/models/test_lottery_backtest.py` | 奖级判定（含边界：5+1、4+2、3+0）；ROI 计算；配对检验 |
| `tests/test_cli_lottery.py` | 命令装配与健康检查 |

Fixture HTML 从真实页面裁剪保存到 `tests/fixtures/lottery/`，不联网跑测试。

## 10. 风险与诚实声明

1. **数字彩无可预测性**。本设计交付的是「一个能被证伪的预测 + 证伪它的回测」，
   不是「一个能找到规律的模型」。回测若全线打平，那就是正确结果，不是失败。
2. **数据源是第三方站点**（彩宝贝）。回填 8000 次请求需控制并发（≤6）并带正常 UA，
   不绕过任何反爬机制；若被限流则降速重试，不加速。
3. **奖金数据依赖抓取的奖级表**。大乐透/双色球的一、二等奖为浮动奖金，
   ROI 以当期实际金额计算；缺失时不估算，该期计 0 并在报告里标注。
