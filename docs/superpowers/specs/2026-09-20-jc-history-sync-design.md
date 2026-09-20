# 竞彩历史数据同步 — 设计文档（子项目 1/3）

日期：2026-09-20
状态：待评审
所属拆分：「竞彩混合过关推荐」的子项目 1（共 3 个）

## 0. 拆分背景

用户需求是「竞彩混合过关购买推荐：同步 2021 年至今 5 种玩法的历史赔率，每种玩法给出推荐」。
该需求实为三个子系统，已商定拆开做：

| # | 子项目 | 交付物 | 依赖 |
|---|---|---|---|
| **1** | **历史数据同步**（本文档） | 3 万场 × 5 玩法的赔率 + 变化序列 + 开奖结果入库 | 无 |
| 2 | 信号验证与回测 | 用批数据回测：模型 vs 市场谁准、赔率趋势有无增量 | 1 |
| 3 | 推荐引擎 + 页面 | 价值榜（逐场逐玩法）+ N 串 1 组合 | 2 的结论 |

**为什么拆**：子项目 2 的结论决定子项目 3 怎么写。用户已明确选择「先验证再决定」，
所以推荐算法不应提前设计。子项目 1 可独立交付价值（拿到数据即可自查赔率变化）。

## 1. 已完成的接口调研（重要，避免重复调研）

### 1.1 可用的两个接口

| 用途 | 接口 | 备注 |
|---|---|---|
| 按日期列比赛 | `getUniformMatchResultV1.qry?matchBeginDate=&matchEndDate=&leagueId=&pageSize=&pageNo=&isFix=0&matchPage=1&pcOrWap=1` | 由用户提供，实测可用 |
| 单场 5 玩法赔率 | `getFixedBonusV1.qry?clientCode=3001&matchId=` | 实测可用 |

Base：`https://webapi.sporttery.cn/gateway/uniform/football/`，**无需认证**。

### 1.2 单场接口返回内容（实测 matchId=1001144，2020 年荷甲）

`value.oddsHistory` 含 5 种玩法，**每个玩法的列表长度就是赔率变化次数**：

| 字段 | 玩法 | 实测条数 |
|---|---|---|
| `hadList` | 胜平负 | 7 |
| `hhadList` | 让球胜平负 | 7 |
| `crsList` | 比分 | 2 |
| `ttgList` | 总进球 | 1 |
| `hafuList` | 半全场 | 3 |

另有 `value.matchResultList`（各玩法开奖结果）。

**每次变化含完整快照 + 涨跌标志**，例如 `hadList`：

```json
{"h":"1.91","d":"3.30","a":"3.30","hf":"0","df":"-1","af":"0",
 "updateDate":"2026-09-19","updateTime":"09:46:22","goalLine":""}
```

### 1.3 各玩法的选项字段

| 玩法 | 选项字段 | 数量 |
|---|---|---|
| `had` | `h/d/a` | 3 |
| `hhad` | `h/d/a` + `goalLine` | 3 |
| `ttg` | `s0`~`s7`（总进球 0-7+） | 8 |
| `hafu` | `hh/hd/ha/dh/dd/da/ah/ad/aa` | 9 |
| `crs` | `sXXsYY`（如 `s02s01`）+ `s-1s{h,d,a}`（其他比分） | 32 |

每个选项都有对应的 `*f` 涨跌标志字段（如 `hhf`）。

### 1.4 关键结论

**同一场比赛的每个玩法只有一个盘口**（实测 3 场，`goalLine` 在所有变化中恒定）。
列表长度代表**赔率变化次数**，不是多个盘口。因此数据模型无需按盘口做复合键。

### 1.5 回溯验证（按年抽样）

| 月份 | 场次 |
|---|---|
| 2021-01 | 539 |
| 2022-06 | 387 |
| 2023-03 | 377 |
| 2024-05 | 380 |
| 2025-08 | 417 |

约 400-540 场/月，**2021-01 至今约 30,000 场**。

### 1.6 已排除的路径（不要再试）

| 路径 | 结果 |
|---|---|
| `getMatchListV1.qry` | HTTP **567**（反爬拦截），返回 HTML |
| `getMatchResultV1.qry` | 接口存在（ec=0）但各种参数组合均返回空 |
| `getLeagueListByMatchStatusV1.qry` | 返回空联赛列表 |
| `/jczx/jczq/` 赛程页 | **403**；`/jczx/jczq/` 下各子路径 404 |
| `zqdz/index.html` 页面本身 | 纯单场详情 SPA，只有 3 个接口，**无列表接口** |
| `getInfoListByChannelAndTagV2.inf?isHasMatchId=1` | 可用且能回溯到 2020-09，但它是**资讯**数据（10000 条上限、覆盖有偏），仅作兜底 |
| matchId 范围遍历 | **不可行**：matchId 是全局比赛 ID（每天约增长 1000），竞彩只是子集 |

## 2. 目标与非目标

### 目标

1. 采集 2021-01-01 至今的竞彩比赛列表与 5 种玩法赔率。
2. **保留赔率变化序列**（每次变化一行），供后续趋势分析。
3. 保留各玩法开奖结果，供后续回测计算命中与收益。
4. 支持断点续传与幂等重跑。

### 非目标

- 不做任何推荐、打分、模型训练（属子项目 2/3）。
- 不做 Web 页面（属子项目 3）。
- 不采集篮彩（`basketball` 路径下的同名接口存在，但本次不做）。

## 3. 数据模型

新增两张表（`db/schema.sql`；`init_db` 重跑 schema 会自动建表）：

```sql
CREATE TABLE IF NOT EXISTS jc_matches (
  match_id INTEGER PRIMARY KEY,
  match_date TEXT NOT NULL,
  match_num TEXT,
  league_id INTEGER,
  league_name TEXT,
  home_team TEXT, away_team TEXT,
  home_team_id INTEGER, away_team_id INTEGER,
  had_h REAL, had_d REAL, had_a REAL,
  goal_line TEXT,
  result_had TEXT, result_hhad TEXT, result_crs TEXT,
  result_ttg TEXT, result_hafu TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jc_matches_date ON jc_matches(match_date);

CREATE TABLE IF NOT EXISTS jc_odds_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  pool TEXT NOT NULL,
  seq INTEGER NOT NULL,
  update_date TEXT, update_time TEXT,
  goal_line TEXT,
  odds_json TEXT NOT NULL,
  UNIQUE(match_id, pool, update_date, update_time)
);
CREATE INDEX IF NOT EXISTS idx_jc_odds_match ON jc_odds_history(match_id, pool, seq);

-- 断点续传：记录已完成的日期
CREATE TABLE IF NOT EXISTS jc_sync_log (
  sync_date TEXT PRIMARY KEY,
  matches INTEGER NOT NULL DEFAULT 0,
  odds_rows INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  finished_at TEXT
);
```

**为什么快照存 JSON 而非宽表**：`crs` 单条记录就有 65 个字段（32 个比分 + 涨跌标志），
宽表列数爆炸；且各玩法的选项集合可能随官方调整变化，JSON 更耐受。
常用字段（胜平负赔率、开奖结果）抽到 `jc_matches` 供 SQL 聚合。

**`jc_matches` 的 `result_*` 字段**：来自 `matchResultList`，按 `code` 分派
（`HAD`/`HHAD`/`CRS`/`TTG`/`HAFU`），取 `combination`。

## 4. 采集流程

```
for 每个日期 d (from → to):
    若 jc_sync_log 中 d 已完成且 failed = 0 → 跳过

    # 1) 该日比赛列表（分页直到取完）
    for pageNo in 1..pages:
        getUniformMatchResultV1(matchBeginDate=d, matchEndDate=d, pageSize=100, pageNo)
        → upsert jc_matches

    # 2) 逐场拉 5 玩法赔率
    for 该日每场比赛:
        getFixedBonusV1(matchId)
        → 展开 5 个玩法 × N 次变化 → upsert jc_odds_history
        → 回填 jc_matches 的 result_* 与当前赔率
        限速 sleep(delay)
        单场失败 → 计入 failed，继续下一场

    写入 jc_sync_log(d, matches, odds_rows, failed)
```

### 命令

```bash
collect-jc-history                          # 全量：2021-01-01 → 今天
collect-jc-history --from 2021-01-01 --to 2021-01-31
collect-jc-history --delay 0.5              # 限速（秒/请求），默认 0.3
collect-jc-history --retry-failed           # 只重跑 jc_sync_log 中 failed > 0 的日期
```

### 参数与默认值

| 参数 | 默认 | 说明 |
|---|---|---|
| `--from` | `2021-01-01` | 起始日期 |
| `--to` | 今天 | 结束日期 |
| `--delay` | `0.3` | 每次单场请求后的 sleep 秒数 |
| `--page-size` | `100` | 列表接口分页大小 |

## 5. 错误处理

| 场景 | 处理 |
|---|---|
| 列表接口失败（网络/非 0 错误码） | 该日标记 `failed`，继续下一日；`--retry-failed` 可重跑 |
| 单场赔率接口失败 | 计入该日 `failed`，跳过该场，整批不中断 |
| 单场返回无 `oddsHistory`（比赛取消等） | 视为正常跳过，不计入 failed |
| 列表分页中途失败 | 已写入的比赛保留（upsert 幂等），该日标 failed |
| 全部日期跑完仍有 failed | 命令末尾打印汇总并返回非 0 |

## 6. 测试策略

全部**离线**，用已抓取的真实响应做 fixture：

1. **解析**：`parse_fixed_bonus()` 正确展开 5 玩法 × N 次变化；`crs` 的 32 个比分选项解析正确；涨跌标志保留。
2. **开奖结果**：`matchResultList` 按 `code` 正确分派到 5 个 `result_*` 字段。
3. **入库幂等**：同一份响应入库两次，两表行数不变。
4. **断点续传**：`jc_sync_log` 标记完成后重跑该日期，不产生新行、不重复请求（用打桩计数验证）。
5. **容错**：某场抛错时该日 `failed` 计数正确，且该日其余场次仍入库。
6. **列表分页**：`pages > 1` 时能取完全部页。
7. **`--retry-failed`**：只重跑失败日期。

## 7. 验收标准

1. `collect-jc-history --from 2021-01-01 --to 2021-01-31` 跑完后，`jc_matches` 中该月约 539 场（对照实测值）。
2. `jc_odds_history` 中每场至少 5 行（每玩法至少一次），含变化多次的场次。
3. 抽查一场（如 `matchId=1001144`），其 `hadList` 的变化序列与官网页面显示一致。
4. 重复执行同一天不产生重复行（幂等）。
5. 中断后重跑，已完成的日期被跳过（断点续传）。
6. 全部测试离线通过。

## 8. 数据量预估与运行时间

| 项 | 估算 |
|---|---|
| 比赛 | 约 30,000 场 |
| `jc_odds_history` 行数 | 约 65-70 万条（5 玩法 × 平均 4-5 次变化 × 3 万场） |
| 磁盘 | 约 300-400MB |
| 请求数 | 列表约 350 次 + 单场 30,000 次 |
| 耗时 | `--delay 0.3` 时约 2.5-4 小时 |

**必须支持中断续跑** —— 单次运行可能跨越数小时。

## 9. 已知限制（记录，不解决）

1. **竞彩比赛覆盖不全**：`getUniformMatchResultV1` 返回的是竞彩在售/曾售的比赛，不含未入选竞彩的场次（这对本需求是正确的范围）。
2. **比分玩法数据量最大**：`crs` 单条 JSON 约 1.6KB，占总量约 2/3。
3. **不采集赔率变化的中间态**：接口只返回最终列表（即官方记录的历次调整点），更细粒度的变动不可得。
4. **半全场模型缺数据**：项目现有 football-data 只覆盖 9 个联赛，而竞彩覆盖更广；子项目 2 验证半全场玩法时需注意模型适用范围。

## 10. 待实现清单（供后续 plan 展开）

- `db/schema.sql`：新增 `jc_matches` / `jc_odds_history` / `jc_sync_log` 与索引
- `collectors/sporttery.py`：`fetch_uniform_match_result()`、`fetch_fixed_bonus()` 及 base 覆盖
- 新建 `collectors/jc_history.py`：`parse_fixed_bonus()`、`parse_match_result()`、`sync_day()`、`sync_range()`
- `cli.py`：新增 `collect-jc-history` 子命令（`--from/--to/--delay/--page-size/--retry-failed`）
- `tests/fixtures/`：`sporttery_uniform_result_sample.json`、`sporttery_fixed_bonus_1001144.json`
- `tests/collectors/test_jc_history.py`：上述 7 类测试
