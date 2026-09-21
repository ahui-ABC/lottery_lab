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

理由：`jobs.py` 的单任务闸门、日志文件、运行状态、失败可见全部现成，且
**不引入并发抓取**（`jobs.py` 顶部记录过：并发抓第三方站点曾把整个 IP 封掉）。

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

`skip_reason` / `daily_status` 走同一过滤，并新增两个字段：

- `off_sale`：当天有候选预测、但不在在售快照里的场次数；
- `live_known`：当天是否有在售快照（即过滤是否生效）。

`plan-jc` 的 JSON 输出加 `off_sale` 提示，让 CLI 用户也知道剔除了几场。

### 2.4 已知取舍

个别场次 `fetch_fixed_bonus` 失败时，它在权威在售列表里、但本轮没有新预测 ——
沿用当天早先的预测（仍按在售处理）。可以接受：赔率只是没刷新，不是买不到。

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

## 4. 页面

竞彩页「最近方案」上方新增卡片：

```
[按最新赔率重算方案]  正在按最新赔率重算…（已跑 12 秒）   ▸ 任务日志
```

- 点击 → `POST /api/jobs/daily-jc/start`；已有任务在跑时按钮置灰（复用单任务闸门）。
- 状态与日志来自现成的 `GET /api/jobs`：运行中 2 秒轮询，空闲 10 秒。
- 跑完自动刷新方案列表与历史盈亏（`loadPlans()` + `loadSummary()`）。

方案卡片新增：

- 标题显示「第 N 版」（N = 1 + 历史版本数）；
- 若有历史版本，加 `<details>`「查看历史版本（M 版）」，每版列出时间与
  `# / 对阵 / 选择 / 赔率 / 概率` 表格；与当前版**不同的行**标注（新增 / 已移除 / 赔率变化）。
  对比只做 `match_id` 集合运算 + 逐场比对，不做文本 diff。

## 5. 错误处理

| 场景 | 处理 |
|---|---|
| 当天没有在售快照（重放/老数据） | 不过滤，行为与现在完全一致 |
| 快照为空（全场停售） | 过滤掉全部 → 不出新方案；已有方案不被清掉（`build_plan` 返回 None 时不写库） |
| 已有其他任务在跑 | 按钮置灰，`jobs.start` 也会拒绝并给出"已有任务在跑（X）" |
| 重算时某场抓取失败 | 跳过该场的新预测，沿用旧预测（仍按在售） |
| 重算清空了当日预测的对奖状态 | `_write` 的 upsert 会 `result=NULL, hit=NULL`；`daily-jc` 的最后一步对奖会立刻补回（同一任务内） |

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
   `off_sale` / `live_known`（`TestClient`）。

## 7. 验收标准

1. 竞彩页点按钮 → 任务状态可见、日志可看 → 完成后方案列表刷新，方案卡片显示
   「第 2 版」，展开能看到第 1 版的场次与赔率。
2. 重算后的方案**不含**当天已停售的场次，且页面注明剔除了几场。
3. 重算 3 次后，历史盈亏的总投入仍只按当前版计一次。
4. 未跑过实时预测的日期（历史重放）行为不变：现有测试全绿。
5. 全部测试离线通过。

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
  - `skip_reason` / `daily_status` 增加 `off_sale` / `live_known`
  - `recent_plans` 返回 `revision` 与 `history`
- `web/templates/jc.html`：重算卡片 + 方案版本与历史对比
- `tests/models/test_jc_parlay.py`、`tests/test_web_api.py`、新增 predict-jc 快照测试
