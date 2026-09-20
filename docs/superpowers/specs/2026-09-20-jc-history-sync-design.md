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

新增**三张表**（`db/schema.sql`；`init_db` 重跑 schema 会自动建表）：

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
  failed_match_ids TEXT,
  finished_at TEXT
);
```

**为什么快照存 JSON 而非宽表**：`crs` 单条记录就有 65 个字段（32 个比分 + 涨跌标志），
宽表列数爆炸；且各玩法的选项集合可能随官方调整变化，JSON 更耐受。
常用字段（胜平负赔率、开奖结果）抽到 `jc_matches` 供 SQL 聚合。

### 3.1 `jc_matches.had_h/d/a` 的唯一写入者

**只由本接口的列表数据写入**（`getUniformMatchResultV1` 返回的 `h/d/a`），
不由 `oddsHistory` 的最后一条覆盖 —— 避免两个写入者互相打架、也避免"最新变化"
与"赛前挂牌价"语义混淆（`hadList` 最后一条通常是赛前最终价，列表的 `h/d/a` 是挂牌价，
两者不一定相同）。需要赔率序列时查 `jc_odds_history`。

### 3.2 空值处理

列表接口在比赛**未开售**时可能返回 `h/d/a` 为空字符串 `""`（实测存在）。
写入 REAL 列前必须把 `""` 映射为 SQL NULL，否则 SQLite 会存成 0.0 或被拒。
所有数值字段统一走一个 `_num()` 辅助函数（空串/非数字 → None）。

### 3.3 `result_*` 字段的编码（子项目 2 会依赖）

`matchResultList` 每项的 `code` 取值为 `HAD`/`HHAD`/`CRS`/`TTG`/`HAFU`（实测确认），
但 **`combination` 的编码与赔率字段词汇不同**，必须记录映射关系，否则子项目 2 无法
把"开奖结果"与"下注选项"对应起来：

| pool | `combination` 示例 | 对应的赔率字段 | 说明 |
|---|---|---|---|
| `HAD` | `H` / `D` / `A` | `h` / `d` / `a` | 直接对应 |
| `HHAD` | `H` / `D` / `A` | `h` / `d` / `a` | **另需结合该项自己的 `goalLine`** |
| `TTG` | `"2"` | `s2` | 数字 → `s{n}`；7 球及以上为 `s7` |
| `HAFU` | `"H:H"` | `hh` | 半场:全场，拼接小写（`H:D` → `hd`） |
| `CRS` | `"2:0"` | `s02s01` 形式 | 比分 → `s{主:02d}s{客:02d}`；"其他比分"归 `s-1s{h,d,a}` |

**写入时只存原始 `combination`**（不做归一化），归一化逻辑留给子项目 2 —— 因为
归一化规则可能随玩法调整，且原始值可无损还原。

## 4. 采集流程

### 4.1 并发模型

采集是 **IO 密集型**（3 万次 HTTP 请求），单线程 + sleep 会浪费绝大部分时间。
采用**「多线程抓取 + 单线程写库」**的生产者-消费者模型：

```
主线程                                    worker 线程池 (N 个)
  │                                             │
  ├─ 逐场提交 fetch_fixed_bonus(match_id) ──────►│ 发 HTTP → 解析 → 返回结果
  │                                             │
  ├─◄── as_completed 按完成顺序取回结果 ────────┘
  │
  └─ 主线程统一写库（SQLite 单写入者）
```

**为什么写库必须单线程**：SQLite 默认 journal 模式下写入互斥，多线程写会互相阻塞
甚至抛 `database is locked`。让线程池只做 HTTP，结果的入库串行化，既避免锁竞争，
也让 `jc_sync_log` 的计数天然准确。

**但单线程写库必须批处理，不能沿用 `store.upsert` 的逐行提交**：
`store.upsert()` 内部每次都 `conn.commit()`（`db/store.py`），一场比赛约 16 行
（5 玩法 × 平均 3.25 次变化），全场 30k 场就是约 **50 万次 commit / fsync**，
单写线程会被磁盘同步拖成瓶颈，20 QPS 的抓取速度根本喂不满。

因此 `sync_day()` 采用 **「每场一个事务」**：解析出该场的所有行后在**同一个事务内**
批量写 `jc_odds_history` + `jc_matches`，最后 `commit()` 一次。
这样降到约 3 万次提交（每场一次），与抓取节奏匹配。
`jc_sync_log` 的写入（每天一次）单独提交。

### 4.2 限速与自我保护

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workers` | `4` | 并发线程数 |
| `--delay` | `0.2` | **每个 worker 自身**两次请求之间的间隔（秒） |

**`--delay` 的语义**：每次**收到响应之后**再 sleep `delay` 秒（而非固定节拍）。
因此单 worker 的实际速率是 `1 / (delay + latency)` 而不是 `1 / delay` ——
`latency` 通常 0.1-0.3s，所以真实吞吐约为估算值的 40-70%。

以默认参数计，实际约 **8-15 QPS**，3 万场预计 **35-60 分钟**。
（早先"20 QPS / 25-40 分钟"是按 `1/delay` 的理想上限算的，偏乐观。）
`--workers 1` 时约 2.5-4 小时。

**必须有的保护机制**：

1. **失败率熔断**：
   - **作用域是整次运行累计**，不是按日。单日只有 15-20 场（30k 场 ÷ 约 2089 天），
     用"每 100 场"的窗口按日统计会永远触发不了，等于死代码。
   - 每完成 **100 场（跨日期累计）** 统计一次失败率；若 > 20%，把后续并发降到
     `max(1, workers // 2)` 并在日志告警。
   - **`ThreadPoolExecutor` 无法动态改大小**：降级从**下一个提交批次**（§4.3 的下一日）
     生效，已提交的任务会跑完。实现上即"重建线程池"。
   - 若已降到 `workers = 1` 且失败率仍 > 20%，**中止本次运行**并返回退出码 `2`
     （区别于普通失败汇总的 `1`），因为继续跑只会白白制造失败记录。
2. **指数退避重试**：单场请求失败后重试 2 次，间隔 1s / 3s；仍失败才计入 `failed`。
3. **可关闭并发**：`--workers 1` 退化为串行模式（保守用户或已被限流时使用）。

### 4.3 主流程

```
for 每个日期 d (from → to):
    若 jc_sync_log 中 d 已完成 且 d 不在 refresh 窗口内 → 跳过

    # 1) 该日比赛列表（分页直到取完）
    for pageNo in 1..pages:
        getUniformMatchResultV1(matchBeginDate=d, matchEndDate=d, pageSize=100, pageNo)
        → upsert jc_matches

    # 2) 并发拉取该日每场比赛的 5 玩法赔率
    线程池提交所有 matchId
    for 每个完成的结果:
        → 展开 5 个玩法 × N 次变化 → upsert jc_odds_history
        → 回填 jc_matches 的 result_*
        失败 → 记入 failed 与 failed_match_ids

    写入 jc_sync_log(d, matches, odds_rows, failed, failed_match_ids)
```

### 4.4 尾日刷新（重要）

`--to` 默认今天，但**今天的比赛尚未全部结束、赔率也还在变**。若不处理，
首次同步把今天写成 `failed=0` 后，**永远不会再更新**，`result_*` 将永久为 NULL、
赔率序列停在首次同步那一刻。

因此引入**刷新窗口**：`--refresh-days N`（默认 `1`）指定"最近 N 天总是重跑"，
即使 `jc_sync_log` 标记为已完成。重跑是幂等的（upsert），代价仅为少量重复请求。

**窗口锚定在 `--to`（而非 `date.today()`）**：刷新范围 = `[to - (N-1) 天, to]`。
这样 `--to 2021-03-31` 这类历史区间回填不会意外把"今天"也算进来。
`--retry-failed` 时刷新窗口**不生效**（该模式下只关心失败日期）。

## 5. 命令

```bash
collect-jc-history                             # 全量：2021-01-01 → 今天
collect-jc-history --from 2021-01-01 --to 2021-01-31
collect-jc-history --workers 4 --delay 0.2     # 并发与限速
collect-jc-history --workers 1                 # 退化为串行（保守/被限流时）
collect-jc-history --refresh-days 3            # 最近 3 天强制重跑
collect-jc-history --force                     # 忽略 jc_sync_log，全部重跑
collect-jc-history --retry-failed              # 只重跑 failed > 0 的日期
```

### 5.1 参数与默认值

| 参数 | 默认 | 说明 |
|---|---|---|
| `--from` | `2021-01-01` | 起始日期 |
| `--to` | 今天 | 结束日期 |
| `--workers` | `4` | 并发线程数（1 = 串行） |
| `--delay` | `0.2` | 每个 worker 的请求间隔（秒） |
| `--page-size` | `100` | 列表接口分页大小 |
| `--refresh-days` | `1` | 最近 N 天总是重跑，覆盖今天的半成品数据 |
| `--force` | 关 | 忽略完成标记，全部重跑 |
| `--retry-failed` | 关 | 只跑 `jc_sync_log.failed > 0` 的日期 |

### 5.2 参数交互语义（避免歧义）

- `--retry-failed` **仍受 `--from`/`--to` 约束**（例如 `--retry-failed --from 2021-01-01 --to 2021-06-30` 只重试该范围内的失败日）。
- `--retry-failed` 是**按日整体重跑**（不按 `failed_match_ids` 精确重试），因为单日只有
  几十次请求，整体重跑成本可忽略且幂等。`failed_match_ids` 仅用于诊断展示。
- `--force` 与 `--retry-failed` 互斥（同时给出时报错）。
- 若某日期**每次运行都失败**，命令将始终返回非 0 并以该日汇总告警 —— 这是有意的，
  避免"静默跳过失败日期"。

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| 列表接口失败（网络/非 0 错误码） | 该日标记 `failed`，继续下一日；`--retry-failed` 可重跑 |
| 单场赔率接口失败 | 重试 2 次（1s/3s 退避），仍失败则计入该日 `failed` 与 `failed_match_ids`，整批不中断 |
| 单场返回无 `oddsHistory`（比赛取消/未开售） | 视为正常跳过，**不计入 failed** |
| 列表分页中途失败 | 已写入的比赛保留（upsert 幂等），该日标 failed |
| **并发失败率 > 20%**（每 100 场统计） | 自动把 `workers` 降为一半并告警；已降为 1 仍高则中止本次运行 |
| 全部日期跑完仍有 failed | 命令末尾打印汇总并返回非 0 |

**关键取舍**：单场失败重试 2 次是为了区分"偶发网络抖动"与"真被限流"。熔断阈值定在
20% 是保守估计 —— 官方接口对正常顺序请求历来稳定，失败率突增几乎必然是限流信号。

## 7. 测试策略

全部**离线**，用已抓取的真实响应做 fixture（`getFixedBonusV1` 的响应与
`getUniformMatchResultV1` 的响应各存一份）。

1. **解析**：`parse_fixed_bonus()` 正确展开 5 玩法 × N 次变化；`crs` 的 32 个比分选项解析正确；涨跌标志保留。
2. **空值**：`h/d/a` 为 `""` 时写入 NULL 而非 0.0。
3. **开奖结果**：`matchResultList` 按 `code` 正确分派到 5 个 `result_*` 字段。
4. **入库幂等**：同一份响应入库两次，两表行数不变。
5. **断点续传**：`jc_sync_log` 标记完成后重跑该日期，不产生新行、不重复请求（用打桩计数验证）。
6. **刷新窗口**：`--refresh-days 1` 时，最近一天即使已标记完成也会重跑；更早的日期被跳过。
7. **容错**：某场抛错时该日 `failed` 计数正确、`failed_match_ids` 记录该场，且该日其余场次仍入库。
8. **列表分页**：`pages > 1` 时能取完全部页。
9. **`--retry-failed`**：只重跑失败日期，且仍受 `--from/--to` 约束。
10. **并发正确性**：`--workers 4` 与 `--workers 1` 对同一批打桩数据产生**相同的业务数据**
    （并发只影响速度，不影响内容）—— 用打桩的 `fetch_fixed_bonus` 加随机延迟验证。
    **比较时必须按业务键**，不能直接 diff 全表：`jc_odds_history` 的 `id`（自增）、
    `jc_matches.captured_at`、`jc_sync_log.finished_at` 两次运行必然不同。
    比较键：`jc_odds_history(match_id, pool, update_date, update_time, odds_json)` 的有序集合，
    以及 `jc_matches(match_id, result_had, result_hhad, result_crs, result_ttg, result_hafu)`。
11. **熔断**：打桩让失败率超过阈值，断言（a）日志出现降级告警，（b）返回/记录的
    **生效并发数被下调**，（c）降到 1 后仍高失败率时命令以退出码 `2` 中止。
    （不依赖 `ThreadPoolExecutor` 内部状态，只断言可观测的返回值与日志。）

> 并发相关测试（10、11）不需要真实网络：`fetch_fixed_bonus` 打桩后线程池照常工作。

## 8. 验收标准

1. `collect-jc-history --from 2021-01-01 --to 2021-01-31` 跑完后，`jc_matches` 中该月约 539 场（对照实测值）。
2. `jc_odds_history` 的行数 ≥ `5 × (有赔率的比赛数)`。**注意不能断言"每场至少 5 行"** ——
   实测存在**无任何赔率**的场次（45 场抽样中有 1 场 0 个玩法）与**只有 4 个玩法**的场次
   （1 场）。验收时应先排除"该场 `getFixedBonusV1` 返回空 `oddsHistory`"的比赛。
3. 抽查一场（如 `matchId=1001144`），其 `hadList` 的变化序列与官网页面显示一致。
4. 重复执行同一天不产生重复行（幂等）。
5. 中断后重跑，已完成的日期被跳过（断点续传）。
6. 全部测试离线通过。

## 9. 数据量预估与运行时间

| 项 | 估算 |
|---|---|
| 比赛 | 约 30,000 场（日均仅约 17 场：2021-01 全月 539 场 ÷ 31 天） |
| `jc_odds_history` 行数 | **约 47-50 万条**（实测 3.25 条/玩法 × 5 玩法 × 3 万场；早期的"4-5 次变化"偏乐观） |
| 磁盘 | 约 250-350MB |
| 请求数 | **列表约 2,100 次**（按天查，日均 17 场 < pageSize 100，多数日子 1 页）+ 单场约 30,000 次 |
| 耗时 | `--workers 4 --delay 0.2` 实际约 8-15 QPS，预计 **35-60 分钟**；`--workers 1` 约 2.5-4 小时 |

**仍必须支持中断续跑** —— 即使并发，仍可能因数十分钟到数小时的操作或中途取消而中断。

## 10. 已知限制（记录，不解决）

1. **竞彩比赛覆盖不全**：`getUniformMatchResultV1` 返回的是竞彩在售/曾售的比赛，不含未入选竞彩的场次（这对本需求是正确的范围）。
2. **比分玩法数据量最大**：`crs` 单条 JSON 约 1.6KB，占总量约 2/3。
3. **不采集赔率变化的中间态**：接口只返回最终列表（即官方记录的历次调整点），更细粒度的变动不可得。
4. **半全场模型缺数据**：项目现有 football-data 只覆盖 9 个联赛，而竞彩覆盖更广；子项目 2 验证半全场玩法时需注意模型适用范围。

## 11. 待实现清单（供后续 plan 展开）

- `db/schema.sql`：新增 `jc_matches` / `jc_odds_history` / `jc_sync_log` 与索引
- `collectors/sporttery.py`：`fetch_uniform_match_result()`、`fetch_fixed_bonus()`（复用 `_get_json` 的 base 覆盖）
- 新建 `collectors/jc_history.py`：
  - `parse_match_result()` / `parse_fixed_bonus()` / `_num()` 空值归一
  - `sync_day(conn, date, workers, delay)` —— 单日同步（线程池抓取 + 主线程写库）
  - `sync_range(conn, from, to, ...)` —— 日期循环 + 断点续传 + 刷新窗口 + 熔断
- `cli.py`：新增 `collect-jc-history` 子命令
  （`--from/--to/--workers/--delay/--page-size/--refresh-days/--force/--retry-failed`）
- `tests/fixtures/`：`sporttery_fixed_bonus_1001144.json`、
  `sporttery_uniform_result_202101.json`（从 `.tmp/probe/` 的实测响应复制）
- `tests/collectors/test_jc_history.py`：上述 11 类测试
