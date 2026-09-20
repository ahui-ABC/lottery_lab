# 竞彩赔率接入 — 设计文档

日期：2026-09-20
状态：待评审

## 1. 问题背景

### 1.1 当期预测缺赔率

前一轮已打通官方 webapi 拿到当期胜负彩 14 场对阵，但**官方接口不提供赔率**（`matchList[].h/d/a` 恒为空）。而 `pipeline.period_predictions` 对未映射场次只走 `market.devig(period_matches.odds_json)`，该列一直为空，导致当期 14 场的市场概率全为 `None`，页面只能显示"暂无预测"。

### 1.2 原赔率源覆盖不足

原设计指望 football-data 提供赔率，但实测存在两重不匹配：

| 问题 | 实测 |
|---|---|
| 数据滞后 | `2627` 赛季最新只到 `2026-09-17`，而 26131 的比赛在 `09-20/21`；手工把 14 场英文名全部填对后候选数仍为 **0/14** |
| 赛事不覆盖 | football-data 只有 9 个联赛（E0/E1/D1/I1/SP1/F1/N1/P1/SC0），而胜负彩混编亚运会、欧冠、欧罗巴、各国杯赛、二级联赛、国家队赛事。近 40 期映射率仅 **12.3%** |

### 1.3 解法：体彩官方竞彩赔率

`getMatchCalculatorV1.qry?poolCode=had&channel=c`（与已打通的接口同源，**无需认证**）返回当天在售的竞彩比赛，每场含实时胜平负赔率。实测：

- 与 26131 期 **14/14 全部命中**
- 队名与胜负彩**同一套中文名**（如"弗洛西诺""沙尔克04""埃沃斯堡"），**不需要别名映射**
- 赔率实时（`had.updateTime`）
- 赛事覆盖含亚运、日职、韩职、五大联赛、英冠、瑞超、挪超、巴甲等
- `market.shin` 对竞彩赔率 **29/29 全部成功**去水（overround 稳定 1.129，竞彩固定抽水约 11.4%）

## 2. 目标与非目标

### 目标

1. 当期 14 场能拿到实时市场赔率并入库。
2. 捕捉赔率的**变化规律**（初盘 → 即时）：10 分钟轮询，有变化才追加，绝不覆盖。
3. 预测链路能直接消费竞彩赔率，产出市场概率与融合概率。
4. 赔率采集与对阵采集**解耦**，互不覆盖。

### 非目标

- 50 家公司赔率、亚赔、凯利指数、欧赔初盘/即时（即 500.com 路线）——**用户明确推迟到后续单独评估**
- 历史赔率回溯（该接口只给当天在售，无历史查询）
- 修改模型/融合/优化器逻辑

## 3. 数据来源

| 项 | 值 |
|---|---|
| 接口 | `getMatchCalculatorV1.qry?poolCode=had&channel=c` |
| Base | `https://webapi.sporttery.cn/gateway/uniform/football/` |
| 认证 | 无 |
| 关键字段 | `value.matchInfoList[].subMatchList[]` |

单场比赛的关键字段：

```
homeTeamAllName / homeTeamAbbName     主队中文名
awayTeamAllName / awayTeamAbbName     客队中文名
leagueAbbName                         联赛简称（如"英超"）
businessDate                          销售日（如 2026-09-20）
matchTime                             开赛时间（HH:MM:SS）
matchDate                             比赛日期
had: { h, d, a, updateDate, updateTime }   胜平负赔率（字符串形式）
```

**重要语义**：`businessDate` 是**销售日**而非比赛日。26131 期的尤文图斯 vs 亚特兰大 `matchDate` 为 `2026-09-21`、`matchTime` 为 `00:00:00`，仍归入 `businessDate=2026-09-20` 的销售日。因此匹配时**不能**按比赛日期过滤，应以队名为准。

**队名字段必须用 `*TeamAllName`**：实测用 `homeTeamAllName`/`awayTeamAllName` 匹配为 14/14，而用 `homeTeamAbbName`/`awayTeamAbbName` 只有 6/14。§6 的匹配逻辑以 AllName 为准。

## 4. 数据模型变更

新增一张表（`db/schema.sql` + `store.ensure_columns` 的迁移机制不适用于建表，需在 schema.sql 中新增并依赖 `CREATE TABLE IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_match_id INTEGER NOT NULL REFERENCES period_matches(id),
  source TEXT NOT NULL,              -- 'jc'
  captured_at TEXT NOT NULL,         -- 本地抓取时刻，微秒精度 ISO
  update_time TEXT,                  -- 官方 updateDate+updateTime
  h REAL, d REAL, a REAL,
  UNIQUE(period_match_id, source, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_odds_snapshots_pm ON odds_snapshots(period_match_id);
```

**双写策略**：

1. **追加**一条快照 —— 仅当该 `period_match` 在 `source='jc'` 下的赔率与**最新一条已有快照**不同。三个赔率值完全一致则不写（这就是"有变化才保存"）。

   **必须用普通 `INSERT`，绝不能用 `store.upsert`。** `store.upsert` 生成的是 `ON CONFLICT DO UPDATE`，在 `(period_match_id, source, captured_at)` 冲突时会**静默覆盖**已有快照 —— 这与"绝不覆盖"直接矛盾。用普通 INSERT 时，若真撞上唯一键会抛错，是安全的失败模式。

   为此 `captured_at` 用**微秒精度**（`datetime.now().isoformat(timespec="microseconds")`），确保同一秒内的两次采集不会碰撞。测试中如需确定性，允许注入 `captured_at`。

2. **更新** `period_matches.odds_json` 为最新值（供预测直接读），**保留其他既有键**。

> **`store.upsert` 会整列替换 `odds_json`**，不能用它做合并。此处用 read-modify-write：读出既有 dict → 写入 `jc` 键 → 用直接的 `UPDATE period_matches SET odds_json=? WHERE id=?` 写回。这比重新提供所有 NOT NULL 列更干净。

## 5. 数据流

```
collect-period（已有，不动）        collect-odds（新增）
  官方接口取当期期号 + 14 场对阵       GET getMatchCalculatorV1
  → upsert periods / period_matches  → value.matchInfoList[].subMatchList
                                       ↓ 按（主队名, 客队名）匹配
                                       ↓ 与最新快照比对
                                       ↓ 有变化 → 追加 odds_snapshots
                                       ↓         + 刷新 period_matches.odds_json

map-fixtures（已有，不动）—— 与竞彩赔率无耦合
```

**耦合点说明**：`collect-odds` **只写赔率与快照，不写对阵**。`upsert_period`（上一轮实现）已保证重跑 `collect-period` 时保留既有 `match_id` 与 `odds_json`，所以先抓赔率再重跑对阵不会丢数据。

## 6. 匹配逻辑

对每个竞彩场次，用（`homeTeamAllName`, `awayTeamAllName`）在当期 `period_matches` 中做**精确**匹配：

```
对当期每场 period_match：
    在竞彩列表里找 (home_name_cn, away_name_cn) 完全相等的项
    命中 → 取 had 赔率
    未命中 → 跳过并计数
```

因为两边队名同源（都是体彩官方中文名），实测 26131 为 14/14，**不需要经过 `team_alias` 解析**。

匹配输出需包含：命中数、未命中场次的 seq 与队名（供人工排查）。

## 7. 关键改动点：`market._ODDS_PREFERENCE`

现为 `("avg", "max", "b365")`，需加入 `jc` 并置于**最前**：

```python
_ODDS_PREFERENCE = ("jc", "avg", "max", "b365")
```

理由：竞彩赔率是当期**唯一**可用的实时赔率；football-data 的 `avg/max/b365` 对当期往往是空的。对历史期次，`jc` 不存在，自动回落到 `avg`，行为不变。

`devig` 的既有实现已支持按优先级逐个尝试，只需扩展该元组。

### 7.1 同时必须更新 `data_health` 的判定键

`cli.data_health` **硬编码**了赔率键列表：当前对 `matches` 与当期 `period_matches` 都以「是否含 `avg`/`b365`/`max`」判定赔率缺失（`cli.py` 中两处 `any(k in j for k in ("avg", "b365", "max"))`）。

**只加 `_ODDS_PREFERENCE` 是不够的** —— 只写 `jc` 的 `period_matches` 仍会被判为"缺赔率"，验收 §11.1 的「14 → 0」无法达成。因此这两处判定必须把 `jc` 纳入。

（不影响既有测试：`tests/test_cli_health.py` 构造的是 `avg` 赔率，`avg` 仍在列表内。）

### 7.2 第二个 Base URL

现有 `sporttery._get_json` 的前缀是 `BASE_URL = ".../gateway/lottery"`，而竞彩端点在 **`/gateway/uniform/football/`** 下。因此需要让 `_get_json` 接受可覆盖的 base（或新增一个接收完整路径参数的变体），不要硬拼现有 BASE_URL。

## 8. 命令设计

| 命令 | 行为 |
|---|---|
| `collect-odds` | 拉取一次，匹配并写入有变化的快照 |
| `collect-odds --watch [--interval 600]` | 循环：拉取 → 匹配 → 比对 → 有变化才写 → sleep。Ctrl+C 停止 |

- `--interval` 默认 **600 秒（10 分钟）**
- 每轮输出：轮次、命中场次数、有变化数、跳过数
- 当日无在售比赛（空列表）时打印提示并继续下一轮，**不报错退出**
- 网络失败时打印错误并**继续下一轮**（watch 模式不应因单次失败退出）
- 一次性模式下网络失败则返回非 0

## 9. 错误处理

| 场景 | 处理 |
|---|---|
| HTTP 非 200 / 网络异常 | 复用 `CollectorError`；watch 模式下捕获后继续下一轮 |
| `errorCode != 0` | 抛 `CollectorError`，携带 `errorCode` |
| 当期无 `status='current'` 的期次 | 打印提示并退出（无从匹配） |
| 竞彩列表为空（非比赛日） | 打印提示，不写库，不报错 |
| 赔率字段缺失或非法（`h/d/a` 为空或非数字） | 跳过该场并计数，不写库 |
| 某场三次赔率与上次完全相同 | 跳过（不追加快照） |

## 10. 测试策略

全部**离线**，使用已抓取的真实响应 `tests/fixtures/sporttery_jc_had_20260920.json`（29 场，含真实 `had` 赔率）。

1. **解析**：`parse_jc_odds` 输出的场次数、队名、赔率值、`update_time` 正确。
2. **匹配**：构造含 26131 部分场次的 `period_matches`，断言命中数与未命中清单正确。
3. **无变化不写**：同一份响应连续处理两次，`odds_snapshots` 只增加一次。
4. **有变化追加**：把某场赔率改一个值再处理，断言追加一条新快照 **且**旧快照仍在（**不覆盖**）。
5. **`odds_json` 合并**：预置 `{"avg": {...}}` 的 `period_matches`，写入 `jc` 后断言两个键**共存**。
6. **`devig` 优先级**：断言 `devig({"jc": {...}, "avg": {...}})` 使用 `jc` 的值（可用只对 `jc` 有效的赔率构造反例）。
7. **异常**：`had` 缺失/赔率为空 → 该场跳过且不写库；`matchList` 之外的非法赔率不导致整体失败。

## 11. 验收标准

1. `collect-period` 后跑 `collect-odds`，`data-health` 的 `current_period_missing_odds` 从 14 降为 **0**。
2. Web `/api/predict` 的 14 场 `market` 字段非空，`has_prediction` 为真。
3. `/plan` 页"载入概率"填入的是**真实模型概率**而非 `0.5 0.3 0.2` 占位值。
4. `collect-odds --watch --interval 600` 能持续运行，赔率变化时 `odds_snapshots` 追加、不变时不追加。
5. 重复执行 `collect-odds` 不产生重复快照行。
6. 对历史期次（无 `jc` 赔率）的预测行为**不回归**——仍按 `avg/max/b365` 回落。
7. 全部测试离线通过。

## 12. 已知限制（不解决，仅记录）

1. **无历史赔率**：接口只返回当天在售比赛，历史赔率只能靠 watch 进程逐日积累。
2. **竞彩抽水高**：overround 约 1.129（11.4% 抽水），而欧洲赔率通常 5–7%。去水后与欧洲盘的概率存在系统性差异，需在回测中用 logloss 验证可用性。
3. **非比赛日空转**：watch 进程在无比赛时持续空转，仅打印提示。

## 13. 待实现清单（供后续 plan 展开）

- `db/schema.sql`：新增 `odds_snapshots` 表与索引（`init_db` 重跑 `schema.sql`，已存在的库会自动建表）
- `collectors/sporttery.py`：`_get_json` 支持覆盖 base；`fetch_jc_odds()`、`parse_jc_odds()`、`match_to_period()`、`save_odds_snapshots()`
- `models/market.py`：`_ODDS_PREFERENCE` 前置 `jc`
- `cli.py`：新增 `collect-odds` 子命令，支持 `--watch` / `--interval`
- `cli.py`（`data_health`）：两处赔率判定键列表纳入 `jc`（见 §7.1），否则验收 §11.1 不可达
- `tests/fixtures/sporttery_jc_had_20260920.json`：真实响应（从 `.tmp/probe/jc_had.json` 复制，29 场，全部含 `had`）
- `tests/collectors/test_jc_odds.py`：上述 7 类测试
