# 竞彩方案「下注前手动重算」 — 设计文档

日期：2026-09-21
状态：已确认（对话中确认）

## 0. 背景与动机

方案目前每天由采集守护进程在 14:00 之后自动生成一次（`cli._maybe_run_daily_jc`：
当天已有方案就跳过）。赔率快照每 10 分钟采一次，但**方案不跟着变**。

用户是在停售前才决定下注的，需要一次按**最新赔率**的重算。直接重算有两个问题：

1. **覆盖即丢失**：`jc_parlay_plans` 的唯一键是 `(plan_date, pool)`，重算会覆盖
   "上一版推的是什么"，赔率怎么变、场次怎么换都无据可查。
2. **买不到的场次**：`pick_legs` 只看当天的预测，而预测是当天早些时候写的。
   那时还在售、现在已停售（甚至已开赛）的场次仍会入选，用户拿着方案去买会发现买不了。

本设计：加一个按钮，重算时只从**还能买的**场次里选，并把旧版本留档可对比。

## 1. 交互与触发：复用现有任务机制

按钮触发**已有的** `daily-jc` 后台任务（`predict-jc` → `plan-jc` → `score-jc`）。

理由：`jobs.py` 的单任务闸门、日志文件、运行状态、失败可见全部现成。

范围要说清：**单任务闸门只约束手动任务彼此**（避免用户连点几个按钮同时抓站点，
`jobs.py` 顶部记录过并发抓取把整个 IP 封掉的教训）。采集守护进程
（`collect-odds --watch`）仍每 10 分钟独立抓一次赔率 —— 这一点本次没有改变，
也不在本次改动范围内。

不做同步接口（一个 HTTP 请求里跑完约 1 分钟）：页面假死、失败只剩报错没有日志、
且绕过单任务锁 —— 正是要避免的那类做法。

`daily-jc` 的最后一步对奖是幂等的，多跑一次无副作用。

## 2. 重算只从「还能买的」场次里选

### 2.1 在售快照落库

`cmd_predict_jc` 的**实时分支**（无 `--date`）本来就通过 `getMatchCalculatorV1`
拿到当轮在售 matchId 列表 —— 这是权威的在售集合。拿到后立刻写快照：

```sql
CREATE TABLE IF NOT EXISTS jc_live_snapshot (
  predicted_on TEXT PRIMARY KEY,   -- 快照日期
  seen_at TEXT NOT NULL,           -- 采集时刻
  match_ids_json TEXT NOT NULL);   -- 在售 matchId 列表；'[]' 表示「确实没有在售」
```

语义是**「最近一次实时运行时的在售集合」**，不是历史事实：每轮实时运行先删当天行
再写入。`--date` 重放分支既不写也不读。

### 2.2 三态：未知 / 空 / 有

`live_match_ids(conn, day)` 返回三态，**必须区分**：

| 返回 | 含义 | 行为 |
|---|---|---|
| `None` | 当天没有快照（历史重放、老数据、没跑过实时） | **不过滤**，保持旧行为 |
| `set()` | 快照存在但为空（此刻确实没有在售） | 过滤掉全部 → 无方案 |
| `{...}` | 快照存在 | 只保留其中的 match_id |

空集与"无快照"必须分开：若把空集当"未知"，20:00 全场停售后重算会产出一份
**全是买不了场次**的方案，正是本设计要避免的。

### 2.3 过滤位置

`pick_legs` 的应用层 SQL 追加：

```python
live = live_match_ids(conn, day)          # None | set[int]
if live is not None:
    if not live:
        return []
    sql += f" AND p.match_id IN ({','.join('?' * len(live))})"
    params += sorted(live)                 # 只拼占位符，值仍走参数绑定
```

必须是**占位符 + 参数绑定**，不把 id 拼进 SQL 字符串。

过滤放在 SQL 内（同 `min_prob` 的理由）：`LIMIT n_legs` 的名额不能被停售场次占掉。
参数顺序写死为 `(day, pool, min_prob, *sorted(live), n_legs)`，IN 子句插在 `WHERE` 内、
`ORDER BY ... LIMIT` 之前。

`skip_reason` / `daily_status` 走同一过滤，字段语义**明确定义**（含糊会让页面文案自相矛盾）：

| 字段 | 含义 |
|---|---|
| `candidates` | 当天该玩法的候选**预测条数（过滤前）** —— 回答"当天有多少场" |
| `off_sale` | 其中已停售（不在在售快照内）的场次数 |
| `bettable` | `candidates - off_sale`，当前还能买的场次数 |
| `qualified` | 在 `bettable` 中达到把握门槛（`MIN_PROB`）的场次数 |
| `live_known` | 当天是否有在售快照（即过滤是否生效） |

**文案按新语义改**（否则会出现自相矛盾的话 —— 全部候选因停售被剔除时，
用户会看到"0 场达到把握门槛 55%"，而真实原因是买不到）：

| 条件 | 页面 / CLI 文案 |
|---|---|
| `bettable == 0` 且 `off_sale > 0` | 「今日候选 N 场，均已停售」 |
| `bettable == 0` | 「今日无该玩法在售场次」 |
| `qualified == 0` | 「当前可买 M 场，均未达到把握门槛 X%」 |
| `live_known == false` | 不提停售（没有快照就无从判断），沿用现有文案 |

`plan-jc` 的 JSON 输出加 `off_sale`，让 CLI 用户也知道剔除了几场。

### 2.4 已知取舍

个别场次 `fetch_fixed_bonus` 失败时，它在权威在售列表里、但本轮没有新预测 ——
沿用当天早先的预测（仍按在售处理）。可以接受：赔率只是没刷新，不是买不到。

**快照的新鲜度取决于"上一次实时运行"**：14:00 自动跑完后，页面上的在售情况会一直
是那时的，直到用户点重算（或下一次实时运行）。这正是按钮存在的理由 ——
不要指望页面自己实时，也不要为此返工。

## 3. 旧方案留档

```sql
CREATE TABLE IF NOT EXISTS jc_parlay_plan_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_date TEXT NOT NULL,
  pool TEXT NOT NULL,
  revision INTEGER NOT NULL,      -- 第几版（1 起）
  n_legs INTEGER NOT NULL,
  unit REAL NOT NULL,
  min_combo INTEGER NOT NULL,
  legs_json TEXT NOT NULL,
  bets_json TEXT NOT NULL,
  invested REAL, returned REAL, winning_bets INTEGER,
  scored_at TEXT,
  created_at TEXT,                -- 该版原始生成时刻
  archived_at TEXT,               -- 被覆盖的时刻
  UNIQUE(plan_date, pool, revision));
```

`save_plan` 改为：同 `(plan_date, pool)` 已存在 → 先整行拷进 history（`revision` =
该组合已有历史行数 + 1，`archived_at` 记此刻）→ 再 upsert 主表。

**版本号不落列**：当前版 = `1 + 历史行数`。省掉一次表结构迁移
（`schema.sql` 是 `CREATE TABLE IF NOT EXISTS`，加不了列；`store._COLUMN_MIGRATIONS`
留给确实需要补列时用）。

**主表仍是唯一的最新版**，因此：`summary()` / `score_plans()` / 历史盈亏一行都不用改，
也不会重复记账。已归档的旧版**不再参与对奖** —— 符合"盈亏只统计当前方案"。

**内容完全相同也照常归档**（赔率没变时用户可能连点两次）：留档是事实记录，
不做"内容相同就不归档"的聪明判断；页面标注「与上一版无差异」，避免用户误以为赔率变了。

## 4. 页面

竞彩页「最近方案」上方新增卡片：

```
[按最新赔率重算方案]  正在按最新赔率重算…（已跑 12 秒）   ▸ 任务日志
```

- 点击 → `POST /api/jobs/daily-jc/start`；已有任务在跑时按钮置灰（复用单任务闸门）。
- 状态与日志来自现成的 `GET /api/jobs`：运行中 2 秒轮询，空闲 10 秒。
- 跑完自动刷新方案列表与历史盈亏（`loadPlans()` + `loadSummary()`）。

「今天选场情况」卡片同步升级：在售快照生效时显示「当前可买 M 场（另 N 场已停售）」，
并在「已有方案但当前有腿已停售、且可买场次仍够出方案」时给一句
**「在售情况已变化，建议重算」** —— 否则用户点了按钮、任务成功、方案却没变，
会以为按钮坏了。

方案卡片新增：

- 标题显示「第 N 版」（N = 1 + 历史版本数）与生成时刻；
- 每条腿标注在售状态：不在当天快照内 → 「已停售」（无快照时不显示，不猜）；
- 若有历史版本，加 `<details>`「查看历史版本（M 版）」，每版列出时间与
  `# / 对阵 / 选择 / 赔率 / 概率` 表格。

**历史版本的腿必须和主表走同一套翻译**：`legs_json` 里存的是 `s01s00` / `hh` 这类
原始代码，前端拿不到映射表（`jc_parlay.py` 的模块注释已说明"必须后端翻译好再给"），
且 `hhad` 还要带上让球线（`goal_line_of`）。`recent_plans` 返回 history 时就翻译好。

对比类别（与当前版逐场比对，用 `match_id` 集合运算，不做文本 diff）：

| 类别 | 判定 |
|---|---|
| 新增 | 当前版有、该历史版没有 |
| 已移除 | 该历史版有、当前版没有 |
| **选择变化** | 同一 `match_id`，`pick` 不同（**最该看到的差异**：赔率刷新后很可能换选项） |
| 赔率变化 | 同一 `match_id` 且 `pick` 相同，赔率不同 |

与上一版整体一致时标注「与上一版无差异」。

## 5. 错误处理

| 场景 | 处理 |
|---|---|
| 当天没有在售快照（重放/老数据） | 不过滤，行为与现在完全一致 |
| 快照为空（全场停售） | 过滤掉全部 → 不出新方案；已有方案不被清掉（`build_plan` 返回 None 时不写库） |
| 已有其他任务在跑 | 按钮置灰，`jobs.start` 也会拒绝并给出"已有任务在跑（X）" |
| 重算时某场抓取失败 | 跳过该场的新预测，沿用旧预测（仍按在售） |
| 重算清空了当日预测的对奖状态 | `_write` 的 upsert 会 `result=NULL, hit=NULL`；`daily-jc` 的最后一步对奖会立刻补回（同一任务内） |
| 重算未出新版（在售场次已不足 2 场） | **不发假信号**：任务成功但方案不变时，页面必须说明原因 —— 「本次重算未出新方案：当前可买 M 场不足 2 串 1（N 场已停售）」，并保留旧方案原样展示、逐腿标注已停售 |

## 6. 测试策略（全离线）

1. **留档**：`save_plan` 连跑两次 → 主表 1 行、history 1 行；`summary()` 的总投入
   只算 1 份（不重复记账）；第三次 → history 2 行、当前版号为 3。
2. **三态过滤**：
   - 无快照 → 与现有行为逐字节一致（现有回归测试不改也应全绿）；
   - 快照为空 `[]` → `pick_legs` 返回空、`build_plan` 返回 None；
   - 快照只含部分场次 → 只选其中的。
3. **名额不被占用**：概率最高的场次已停售时，名额让给下一个在售场次
   （同 `min_prob` 那条测试的思路）。
4. **快照三态的可判定性**：`live_match_ids` 对「无行 / `'[]'` / 有 id」返回
   `None / set() / {...}`。
5. **`predict-jc` 写快照**：实时分支用打桩的 `fetch_jc_odds` / `fetch_fixed_bonus`
   验证写入内容与「先删后插」；`--date` 重放分支不写。
6. **Web**：`/api/jc/plans` 返回 `revision` 与 `history`；`today_status` 带
   `off_sale` / `live_known` / `bettable`（`TestClient`）。
7. **文案分支**：`bettable == 0 且 off_sale > 0`、`bettable == 0`、`qualified == 0`
   三种情况各自说出正确原因 —— 不能把"买不到"说成"没把握"。
8. **历史版本翻译**：history 里的腿带 `pick_label` 与 `hhad` 的让球线；
   对比能识别「选择变化」（同场 pick 变了）。

## 7. 验收标准

1. 竞彩页点按钮 → 任务状态可见、日志可看 → 完成后方案列表刷新，方案卡片显示
   「第 2 版」，展开能看到第 1 版的场次与赔率。
2. 重算后的方案**不含**当天已停售的场次，且页面注明剔除了几场。
3. 重算 3 次后，历史盈亏的总投入仍只按当前版计一次。
4. 未跑过实时预测的日期（历史重放）行为不变：现有测试全绿。
5. 重算没出新版时（在售不足 2 场），页面说明"为什么没变"，而不是沉默或假装成功。
6. 停留的旧方案里每条已停售的腿被标注出来 —— 用户不会照着一份买不到的计划去下注。
7. 全部测试离线通过。

## 8. 不做的事

- 不做自动频繁重算（保持每天一次；需要更贴近停售就手动点）；
- 不做跨版本盈亏统计（盈亏只跟当前版走）；
- 不做版本数上限（每版几 KB；页面默认只展开最近 3 版）；
- 不新增专用 CLI 子命令 / 专用任务键 —— 直接复用 `daily-jc`。

## 9. 待实现清单

- `db/schema.sql`：新增 `jc_live_snapshot`、`jc_parlay_plan_history` 两张表
- `models/jc_predict.py`：新增 `save_live_snapshot(conn, day, match_ids)` 与
  `live_match_ids(conn, day) -> set[int] | None`
- `cli.py`：`cmd_predict_jc` 实时分支写快照（拿到 `parse_jc_match_ids` 结果后立即写）
- `models/jc_parlay.py`：
  - `pick_legs` 接入在售过滤（§2.3）
  - `save_plan` 归档旧版
  - `skip_reason` / `daily_status` 增加 `off_sale` / `bettable` / `live_known`
  - `recent_plans` 返回 `revision`、`history`（腿**翻译好**：`pick_label` + `hhad` 让球线）、
    以及每条腿的 `on_sale`
  - 对比函数：按 `match_id` 集合运算给出 新增 / 已移除 / 选择变化 / 赔率变化
- `web/templates/jc.html`：重算卡片 + 方案版本与历史对比 + 停售腿标注 + 「建议重算」提示
- `web/templates/jc.html` 文案与 `cli.cmd_plan_jc` 的 skip 文案按 §2.3 的表格改
- `tests/models/test_jc_parlay.py`、`tests/test_web_api.py`、新增 predict-jc 快照测试
