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

## 2. 数据源调研结论（已实测，勿重复调研）

来源：彩宝贝 `https://kaijiang.78500.cn/`

| 项 | 结论 |
|---|---|
| 列表页 `/dlt/` `/ssq/` `/p3/` `/p5/` `/3d/` | 只返回最近 100 期，**无分页参数**（`?page=2` 被忽略；`/index_2.html`、`/2025/` 均 404） |
| 单期页 `/{lottery}/{issue}/` | ✅ 可用，2020 年至今全部可取；不存在返回 **404** |
| 编码 | **gb18030** |
| 反爬 | **必须带浏览器 User-Agent**，否则阿里云 WAF 返回 403 |
| 手机版 | `m.78500.cn/kaijiang/{lottery}/{issue}.html`（不使用，结构未验证） |

### 2.1 五种彩种模板完全一致

```
#kjCode ul.kjh li          → 开奖号码；class 含 rb_kj = 前区/红球，b_kj = 后区/蓝球
#kjCode .kjh_order_nums    → 出球顺序
#endTime                   → 开奖日期，形如 "2026年09月19日 星期六"
#sale                      → 本期投注金额，形如 "313,508,185元"
#bonusBalance              → 滚入下期奖金（排列三/五/3D 无此项，该行被注释）
#winList tr                → 奖级表：奖级 / 中奖条件 / 中奖注数 / 单注奖金
```

### 2.2 两个必须处理的坑

1. **排列三/排列五/福彩3D 的号码是单位数**：页面里是 `<li class="rb_kj">0</li>`，
   issue 2020100 的号码是 `0 6 4` 而非 `064`。**必须按位数左侧补零**（p3/3d 补到 3 位，p5 补到 5 位），
   否则 `064` 会变成 `64`，命中判定全错。
2. **期号不连续**：2026-06-13 才到 2026154 期（春节停售），不能按日期推算期号。
   回填策略是**枚举 + 404 跳过**：每个彩种有已知的年最大期号上界
   （`per_year`，大乐透/双色球 160，排列类 370），枚举全部并在 404 时跳过。
   多做约十几次无效请求，换来的是不必猜「连续几个 404 才算到头」这种脆弱启发式。

### 2.3 回填量估算

| 彩种 | 频率 | 6.7 年约需请求 |
|---|---|---|
| dlt | 周一三六 | ~1050 |
| ssq | 周二四日 | ~1030 |
| p3 / p5 / 3d | 每日 | ~2450 × 3 |
| **合计** | | **~8000** |

6 线程 + httpx 连接池，约 5–8 分钟。失败页要记入断点，支持重跑续传。

## 3. 数据模型

新增两张表（`db/schema.sql`）：

```sql
CREATE TABLE IF NOT EXISTS lottery_draw (
  lottery      TEXT NOT NULL,   -- dlt/ssq/p3/p5/3d
  issue        TEXT NOT NULL,   -- '2026107'
  draw_date    TEXT NOT NULL,   -- 'YYYY-MM-DD'
  numbers      TEXT NOT NULL,   -- JSON {"front":[2,5,7,14,22],"back":[4,10]}
                                --   或 {"digits":[0,6,4]}（已补零的字符串数组）
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
    "dlt": {"name": "大乐透", "kind": "two_zone", "front": 5, "front_max": 35,
            "back": 2, "back_max": 12, "per_year": 160},
    "ssq": {"name": "双色球", "kind": "two_zone", "front": 6, "front_max": 33,
            "back": 1, "back_max": 16, "per_year": 160},
    "p3":  {"name": "排列三", "kind": "digits", "digits": 3, "per_year": 370},
    "p5":  {"name": "排列五", "kind": "digits", "digits": 5, "per_year": 370},
    "3d":  {"name": "福彩3D", "kind": "digits", "digits": 3, "per_year": 370},
}
```

- `parse_draw(html, lottery)` — 纯函数，输入 HTML 字符串返回 dict。测试用本地 fixture。
- `fetch_draw(lottery, issue)` — 返回 dict 或 `None`（404）。
- `sync_range(conn, lottery, year_from, year_to, workers=6, on_progress=None)`
  - 线程池并发抓取，**主线程单线程写库**（沿用 `jc_history.sync_day` 的模式）
  - 断点续传：先查 `lottery_draw` 已有的 issue 集合，只抓缺的
  - 连续 20 个 404 判定该年结束，跳到下一年
  - 自建 httpx 连接池（不复用 `sporttery.get_client()`：它带的 `Referer`/`Origin`
    指向体彩官网，发给彩宝贝既不对也可能触发风控）

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
| `dlt` | 前区命中数 + 后区命中数 → 奖级（5+2…0+2） | 当期实际奖级表（浮动奖）+ 固定奖级规则 |
| `ssq` | 红球命中数 + 蓝球是否命中 → 奖级 | 同上 |
| `p3`/`3d` | 三位全中（**直选**） | 固定 1040 元 |
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
| `collect-lottery --lottery all --from 2020-01-01 [--workers 6]` | 回填历史（断点续传） |
| `predict-lottery --lottery dlt [--strategy hot,weighted] [--notes 5]` | 对下一期生成推荐并存 `lottery_prediction` |
| `score-lottery` | 给已开奖的预测回填 hits/prize |
| `backtest-lottery --lottery all --window 100 [--notes 5]` | 逐期走查 + 显著性表 |

## 8. Web 页面 `/lottery`

- 五个彩种切换（卡片行）
- 最新一期开奖号码（球体样式，区分前区/后区色）
- 下一期推荐号码：各策略一行
- 近 100 期号码频次 + 遗漏表
- 回测结果表：策略 × 指标（含 `beats_random` 标记与 p 值）

导航栏新增 `数字彩` tab。

## 9. 测试策略

| 文件 | 覆盖 |
|---|---|
| `tests/collectors/test_lottery_parse.py` | 五种彩种 fixture HTML 解析；**补零**；`*`/缺失字段容错；404 判定 |
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
