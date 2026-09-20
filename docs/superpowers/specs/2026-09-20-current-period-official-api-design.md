# 当期对阵与历史开奖接入官方接口 — 设计文档

日期：2026-09-20
状态：待评审

## 1. 问题背景

### 1.1 现象

Web 预测页长期显示 **26080 期**（2026-05-25 开奖），而真实当期是 **26131 期**（2026-09-20 20:30 截止销售，2026-09-21 开奖）。

### 1.2 根因（已实测确认）

| # | 根因 | 证据 |
|---|---|---|
| 1 | `web/app.py:97-100` 把"当期"定义为"DB 里 `status='current'` 优先、否则 `id` 最大的一条 periods"，**无任何日期校验** | 实测 `/api/period/current` 返回 `period_no=26080` |
| 2 | `periods` 表里只有 26080 一条 —— 9/18 为验收手工导入的样本 | `SELECT * FROM periods` 仅 1 行 |
| 3 | `cli.py:92-102` 的 `cmd_collect_period` 无 `--file` 时只探测，**即使成功也只 print 不解析不入库** | 实测 `collect-period` 打印"当前期接口不可用"、`exit=2` |
| 4 | 原设计要求的 `parse_period_fixtures` / `parse_draw_results` **从未实现** | `sporttery.py` 中无这两个函数 |
| 5 | 原设计唯一的 live 接口名 `getMatchListV1.qry?param=90,0` **根本不存在** | 实测返回 `E0001`；另有 7 个相近命名同样 `E0001` |

### 1.3 关键结论

**不是"官方数据源不可用"，而是"接口名猜错了 + 入库链路从未接通"。**

补充澄清：
- `E0001` 是"接口不存在/参数不合法"的通用码，**不是反爬**。
- `isVerify=1` 从来不是问题。
- `data/probe/probe_0.json` 里的 WAF 页面是 9/18 抓取的偶发产物；同一 URL 现在正常返回 JSON。
- 原设计"500 彩票有反爬"的记录只对了一半：`odds.500.com` 确有 WAF，但 `live.500.com` 赛程页可访问（不过页面不含赔率数据）。

## 2. 目标与非目标

### 目标

1. 当期 14 场对阵可从官方接口获取并入库。
2. 近 4 个赛季的历史期次（对阵 + 赛果 + 奖金）可入库。
3. Web 页面显示真实当期，并明确标注期次状态与截止时间。
4. 赔率沿用既有映射路径（football-data），缺失时显式标注而非静默为空。
5. 修复 26080 这类"把样本期次当成当期"的问题，使其不可能再发生。

### 非目标

- 逆向 500.com WAF 获取亚赔/凯利/多公司赔率（**单独一轮**）。
- 修改模型、融合、方案求解逻辑。
- 全量拉取 3328 期历史。
- 自动化定时抓取（本轮为手动执行 CLI）。

## 3. 实测确认的官方接口

所有接口均**无需认证/签名**，只需合理的 `User-Agent` 与 `Referer`。

| 用途 | 接口 | 关键返回 |
|---|---|---|
| 当期在售期号 | `getLottoSaleInfoV1.qry?param=90,0` | `value[0].lotteryDrawNum` / `lotterySaleEndtime` / `lotteryDrawTime` |
| **按期号取对阵** | `getFootBallDrawInfoByDrawNumV2.qry?isVerify=1&lotteryGameNum=90&lotteryDrawNum={期号}` | `value.matchList`（14 条） |
| 最新期 + 期次列表 | `getFootBallDrawInfoV2.qry?isVerify=1&param=90,0` | `value.sfcDetail` / `value.sfclist` |
| 历史开奖 | `getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=100&isVerify=1&pageNo={n}` | `value.list`（每期含对阵+赛果+奖金） |

Base URL：`https://webapi.sporttery.cn/gateway/lottery/`

### 关键约束

1. **`lotteryGameNum` 与 `lotteryDrawNum` 两个参数缺一不可**。只传前者返回 `P0001`（参数不合法）。
2. `gameNo` / `param` 中 **90 = 胜负彩**。（注：85 = 超级大乐透，35 = 排列3，与足彩无关；原设计把这些搞混过。）
3. **接口不提供赔率** —— `matchList[].h/d/a` 恒为空字符串。
4. 历史接口分页 `pageSize` 最大 **100**，`total=3328`。
5. 按期号查询返回的 `value` 直接是期次详情，**没有 `sfcDetail` 外层**；而 `getFootBallDrawInfoV2` 的详情在 `value.sfcDetail` 下。两者结构不同，解析时不可混用。

### 数据样例（26131 期，已实测）

```
lotteryDrawNum=26131  lotteryDrawTime=2026-09-21  lotterySaleEndtime=2026-09-20 20:30:00
1 英超 伯恩茅 vs 利物浦    2 英超 利  兹 vs 水晶宫   ...  14 法甲 马  赛 vs 日尔曼
```

注意：官方队名是**简称**（"伯恩茅"、"利  兹"含全角空格），且多队名含空格填充，入库前需 `strip()` 与空格归一化。

## 4. 数据模型变更

`period_matches` 增加一列：

```sql
ALTER TABLE period_matches ADD COLUMN league_cn TEXT;
```

- 用途：页面展示（"英超 伯恩茅斯 vs 利物浦"）与映射失败时的诊断。
- 现有 `schema.sql` 使用 `CREATE TABLE IF NOT EXISTS`，对已存在的表不会生效，因此需要在 `store.py` 增加一个幂等的轻量迁移函数（检查 `PRAGMA table_info(period_matches)` 后按需 `ALTER TABLE`）。
- 其余表结构不变。

## 5. 数据流

```
collect-period
  getLottoSaleInfoV1(param=90,0)          → 当期期号 N
  getFootBallDrawInfoByDrawNumV2(N)       → 14 场对阵
  upsert periods        (period_no=N, status='current', sale_end, draw_date)
  upsert period_matches (14 行, seq=1..14, league_cn)

collect-draws --years 4
  getHistoryPageListV1(pageSize=100, pageNo=1..k)
  对每期:
    upsert periods        (period_no, status='historical', draw_date, sale_end)
    upsert period_matches (14 行, league_cn)
    upsert draw_results   (results_json, prizes_json)

map-fixtures --period N        （既有命令，复用）
  team_alias.resolve(中文名) → 英文名
  matches 表按 (英文名对, 日期±2天) 找候选
  只写回 period_matches.match_id（不回填赔率；两份 odds_json 的区别见 §7）
```

## 6. 字段映射

| 目标列 | 来源字段 | 转换 |
|---|---|---|
| `periods.period_no` | `lotteryDrawNum` | 原样 |
| `periods.draw_date` | `lotteryDrawTime` | 取日期部分 `YYYY-MM-DD` |
| `periods.sale_end` | `lotterySaleEndtime` | 原样 |
| `periods.status` | — | 在售期 `'current'`，历史接口 `'historical'` |
| `period_matches.seq` | `matchNum` | int |
| `period_matches.home_name_cn` | **`masterTeamAllName`** | 空格归一化后 `strip()` |
| `period_matches.away_name_cn` | **`guestTeamAllName`** | 同上 |
| `period_matches.match_time` | `startTime` | 原样 |
| `period_matches.league_cn` | `matchName` | 原样 |
| `draw_results.results_json` | `lotteryDrawResult` | 空格分隔 → **逗号分隔**（对齐现有格式） |
| `draw_results.prizes_json` | `prizeLevelList` / `prizeLevelListRj` | → `{"first":…, "second":…, "r9":…}` |

**队名必须取全名字段**（`*TeamAllName`，不是 `*TeamName`）。理由：官方简称命中别名表的能力明显更差。对 26131 期实测（`team_alias.resolve`，28 个队名）：

| 字段 | 命中 |
|---|---|
| `masterTeamName` / `guestTeamName`（简称） | 16 / 28 |
| `masterTeamAllName` / `guestTeamAllName`（全名） | **20 / 28** |

短名失败例："伯恩茅"（全名"伯恩茅斯"）、"利  兹"（含全角空格）；15 场比赛中 5 场因短名失配而整场丢失预测。这直接损害本次要修的页面，故取全名。

残留的 8 个未命中全名（如"弗洛西诺内""皇家贝蒂斯""巴黎圣日尔曼"）需在 `team_alias.SEED` 补别名；**这不属于本轮的阻塞项**，但需在验收时统计并记录（见 §11）。

`prizes_json` 取值规则（对齐 `sporttery.import_fixtures_csv` 的既有格式，避免下游 `check-draw` 分裂出两套解析）：
- `prizeLevelList` 中 `prizeLevel == "一等奖"` → `first`，`"二等奖"` → `second`，取 `stakeAmountFormat` 并 **`float()` 转换**（接口返回的是字符串）。
- `prizeLevelListRj` 中 `prizeLevel` 为 **"任选9场"** 的项 → `r9`。
- 缺失的键直接不写入（下游已按 `.get()` 处理）。

## 7. 赔率策略

官方接口不提供赔率，本轮沿用**既有映射路径**：

1. `config.yaml` 的 `seasons` 增加 `"2627"`，补下载 2627 赛季 CSV。
2. 当期对阵经 `map-fixtures` 映射到 `matches.id` 后，从 `matches.odds_json` 取赔率。
3. **已知限制**：football-data 当前赛季更新滞后 3–6 天（实测 `2627/E0.csv` 最新至 `2026-09-14`，而 26131 的英超场次在 `09-20`）。因此当期场次大概率取不到赔率。
4. 处理方式：**明确标注，不静默失败**。页面与 API 对无赔率的场次输出"赔率待更新"，`data-health` 或 `collect-period` 输出中报告无赔率场次数。
5. 后续 football-data 补齐后，重跑 `map-fixtures` 即可自动回填（`upsert` 幂等）。

**注意存在两份 `odds_json`，不要混淆**：

| 位置 | 谁写入 | 谁读取 |
|---|---|---|
| `period_matches.odds_json` | 本轮的官方接口入库（写空）、CSV 导入 | **`pipeline.period_predictions`**（决定 market 组件） |
| `matches.odds_json` | football-data 采集 | `web/app.py:_period_with_matches`（仅用于页面展示回退） |

重跑 `map-fixtures` 只让 `period_matches.match_id` 指向 `matches`，**并不会把 `matches.odds_json` 拷进 `period_matches.odds_json`**。因此映射成功后：

- 页面（走 `matches.odds_json` 回退）**能**显示赔率；
- 但 `/api/predict` 的 market 组件读的是 `period_matches.odds_json`，**仍为 `null`**。

若不希望出现"页面有赔率、预测却没有"的割裂，需要在 `map-fixtures` 里把 `matches.odds_json` 回填到 `period_matches.odds_json`（仅在后者为空时）。**此项列入 §12**；若本轮不做，则该割裂需作为已知现象记录。

> 注：`findings.md` 第 27 条记录的"`fixture_match.match_period` 用 `NULL` 覆写 `odds_json`"问题，在当前未提交的工作区中**已修复**（三处 `store.upsert` 均已改为透传 `pm["odds_json"]`）。本轮只需**加回归测试验证**，不要重做该项修复。

### 7.1 已知限制：当期暂时没有预测（本轮不解决）

必须明确记录，避免交付时误判：

- `matches` 表当前 **15868 场全部 `result IS NOT NULL`**（无未赛比赛），赛季止于 `2025/2026`，最新比赛 `2026-05-24`。
- 因此当期 26131 的 14 场经 `map-fixtures` 后**必然全部 `match_id IS NULL`**——日期窗口 ±2 天内没有候选比赛。
- 即使补下载 2627 赛季，football-data 实测只更新到 `2026-09-14`，仍覆盖不到 26131 的 `09-20/21`。
- 而 `pipeline.period_predictions` 对 `match_id IS NULL` 的场次走的是 `target is None` 分支：

```python
component = {"market": market.devik(odds), "dc": None, "gbdt": None}
component["fused"] = Fusion(mode=fusion_mode).predict(component)
```

**实测这条路径的真实输出**（不是"只有 market 为空"这么简单）：

| 字段 | 值 |
|---|---|
| `market` | `None`（`market.devig({})` → `None`） |
| `dc` / `gbdt` | `None` |
| `fused` | **`[0.333333, 0.333333, 0.333333]`** |

`Fusion.predict` 在三个组件全为 `None` 时回退到均匀分布（实测 `market_primary` 与 `full` 两种模式均如此）。而 `predict.html` 的 `appendProbs` 会把 `fused` 直接渲染成 **"33.3% / 33.3% / 33.3%"**。

**这是一个必须修复的展示陷阱**：当期 14 场会显示 14 组毫无信息的 33.3%，比"空白"更具误导性——用户会以为模型给出了预测。

**所以本轮的交付边界是：当期对阵正确显示，且如实呈现"暂无预测"。** 模板/API 需要在 `fused` 属于"无真实输入"的情况下**抑制渲染**（例如：当该场 `mapped == false` 且三个组件均为 `None` 时不输出概率，改显示"暂无预测/赔率待更新"），而不是显示均匀分布。

要真正让当期出预测，需要引入**当期的比赛级数据源**（这正是用户提出的 500.com 路线要解决的问题），属于后续单独一轮。

## 8. Web 层修复

`_period_with_matches` 的选取逻辑改为：

1. 优先**真正在售**的期次：`status='current'` **且** `sale_end > now`。
2. 无在售期次时，退到 `draw_date` 最新的 `historical` 期次。
3. **两种情况都必须把期次身份透传给前端**：新增返回字段 `period_status`、`sale_end`、`is_current`。
4. 前端在非当期时显示明确提示（如"当前显示的是历史期次 26129，非在售期次"），当期则显示"本期截止 2026-09-20 20:30"。

**`is_current` 的定义必须包含时间判定**：

```
is_current = (periods.status == 'current') and (now < periods.sale_end)
```

不能只用 `status=='current'`。否则 `collect-period` 写入的一行在其销售截止后仍会被永久标记为"当期"——这恰好复现了本次 bug 的形态（一个过期的期次被当作当期展示），直接违背目标 5。`sale_end` 为空时视为不在售（保守）。

配套地，`collect-period` 每次写入新当期时，应把此前所有 `status='current'` 的行降级为 `'historical'`，保证同一时刻至多一条 current。

这是本次 bug 的**直接根因**所在，必须显式修复，而不是仅靠数据变新来"掩盖"——否则下次数据过期时会重演。

## 9. 错误处理

| 场景 | 处理 |
|---|---|
| HTTP 非 200 / 网络异常 | 抛 `CollectorError`，CLI 打印可读错误并返回非 0 |
| `errorCode != 0`（如 `P0001`） | 抛 `CollectorError`，携带 `errorCode` + `errorMessage` |
| 当期接口返回空 `value` | 视为"当前无在售期次"，打印提示，不写库 |
| 按期号查不到（期号不存在） | 同上，明确区分"无此期"与"网络失败" |
| `matchList` 长度 ≠ 14 | **拒绝入库并报错**（数据不完整时不污染 DB） |
| 历史翻页中途失败 | 保留已入库页（`upsert` 幂等），报告失败页号 |
| 请求频率 | 翻页间加固定间隔（默认 0.5s），避免触发风控 |

## 10. 测试策略

**全部离线**，不依赖网络：

1. 将本次实测响应存为 fixtures：
   - `tests/fixtures/sporttery_saleinfo_90.json`（当期在售）
   - `tests/fixtures/sporttery_bydraw_26131.json`（当期对阵）
   - `tests/fixtures/sporttery_history_90.json`（历史开奖，含奖金）
2. 解析测试：`parse_*` 函数对上述 fixture 输出的结构断言（期号、14 场、队名去空格、`league_cn`、赛果格式、奖金键）。
3. 入库测试：对内存 SQLite 跑完整入库，断言 `periods` / `period_matches` / `draw_results` 行数与字段。
4. 幂等测试：同一 fixture 跑两次，行数不变。
5. 异常测试：`matchList` 只有 13 场 → 断言抛错且**不写库**；`errorCode=P0001` → 断言抛 `CollectorError`。
6. Web 层测试：构造 `status='current'` 与纯 `historical` 两种 DB，断言 `is_current` / `period_status` 正确，且纯 historical 时**不会**被当作当期。

## 11. 验收标准

> **措辞约定**：以下凡涉及"当期"，均指**执行验收时官方接口返回的在售期次**，不硬编码期号。26131 的销售截止时间是 2026-09-20 20:30；该时点之后 26131 不再在售（`is_current` 转为 False，且 `collect-period` 依 §9 不写库），届时须以新的在售期次为准。

1. `collect-period` 后，`data-health` 输出当期期号 = 官方在售期号，且该期 `status='current'`、`sale_end` 与官方一致。
2. Web 预测页显示当期真实 14 场对阵（含联赛名与开赛时间），并显示销售截止时间；且**不再显示 26080**。
3. 当期页面如实呈现"暂无预测/赔率待更新"（见 §7.1）：不得空白、不得报错，**也不得显示均匀的 33.3%**。
4. `collect-draws --years 4` 后，`periods` 中 historical 期次数量 ≥ 600，每期 14 场对阵 + `draw_results`（含 `results_json` 与 `prizes_json`）齐备。
5. 对奖链路：取一期**已生成 `plans` 且该计划确实命中**的历史期次跑 `check-draw`，能产生 `winnings` 记录。
   （注：`check-draw` 对 `hit_notes <= 0` 的档位直接跳过，所以仅有 `plans` 还不够——必须有一个命中至少一档的计划。`collect-draws` 本身不生成 `plans`；验收时需先为某历史期次构造一个必定命中的计划，例如按该期已知 `draw_results` 构造 one-hot 概率再走求解流程，否则本项不可达。）
6. 重复执行 `collect-period` / `collect-draws` 不产生重复行；数据库无 `matchList` 长度异常期次。
7. 统计并记录当期 14 场中映射成功/失败的场次数（预期映射全失败，见 §7.1）与残留未命中队名清单。
8. 全部测试离线通过。

## 12. 待实现清单（供后续 plan 展开）

- `db/store.py`：幂等轻量迁移（加 `league_cn`）
- `collectors/sporttery.py`：修正 `LIVE_URLS`；新增 `fetch_current_period` / `fetch_period_by_no` / `fetch_history_pages`、`parse_*`、`upsert_period` 系列；`CollectorError` 异常类型
- `collectors/fixture_match.py`：**仅加回归测试**验证 `odds_json` 不被清空（问题已在工作区修复，见 §7 注）
- `cli.py`：接通 `collect-period` / `collect-draws`；`collect-draws` 增加 `--years N`
  - `--years` 语义定义：从第 1 页起顺序翻页，遇到 `lotteryDrawTime < 今天 − N 年` 的期次即停止（而非按页数估算）
  - `collect-period` 写入新当期时，**同时把此前所有 `status='current'` 的行降级为 `'historical'`**（§8 要求，保证同一时刻至多一条 current）
- `cli.py`（`data_health`）：输出当前 `status='current'` 的期号，以及当期无 odds 的场次数——验收 #1 依赖此项
- `web/app.py`：`_period_with_matches` 选取逻辑（含 `is_current` 时间判定）+ 透传期次状态
- `web/templates/predict.html` + `web/app.py`（`_period_with_matches` 的 enriched 结构）：**把 `league_cn` 一并透传并在页面展示**（验收 #2 要求显示联赛名）
- `web/templates/predict.html`：展示期次状态、截止时间；**并在无真实输入时抑制 `fused` 渲染**——否则会显示均匀 33.3%（§7.1），验收 #3 依赖此项
- `collectors/fixture_match.py`（可选，见 §7 的"两份 odds_json"说明）：映射成功时，若 `period_matches.odds_json` 为空，则从 `matches.odds_json` 回填；不做则需记录"页面有赔率但预测无 market"的现象
- `config.yaml`：`seasons` 增加 `2627`
- `tests/`：fixtures（`sporttery_saleinfo_90.json` / `sporttery_bydraw_26131.json` / `sporttery_history_90.json`）+ 离线测试

### 历史入库的边界规则

- 跳过 `lotteryDrawResult` 为空的条目（未开奖，无法构成 `draw_results`）。
- **跳过 `matchList` 长度 ≠ 14 的条目**（并计数告警），否则验收 #4 的"每期 14 场齐备"会在罕见短条目上失败。
- 历史条目**也带 `lotterySaleEndtime`**，故 `sale_end` 可正常填充，不必留空。
- 官方队名统一做空格归一化（`' '.join(s.split())`），消除 `利  兹` 这类全角/连续空格。
