# 竞彩当期预测与对奖闭环 — 设计文档

日期：2026-09-20
状态：已确认（对话中确认，见下）

## 0. 背景与动机

竞彩历史数据（2021 至今，子项目 1）正在回填。但**历史回测只能回答"过去有效吗"**，
无法回答"现在还有效吗"——市场环境会变。

因此并行建立第二条验证路径：**每天对当期在售比赛做预测 → 存档 → 次日对奖 → 看命中率**。
两条路径互补：历史有纵深无当下，实时有当下无纵深。

用户明确要求「赔率变化也要考虑进去」，并同意「几种综合起来算」。

## 1. 核心设计原则：可拆解，不做黑盒

**问题**：把多个信号拍脑袋加权，很可能比单用市场概率更差；而且一旦做成黑盒，
就无法归因到底哪个信号在起作用。

**做法**：并行记录**每一条独立路径**的推荐，让数据自己淘汰。
`jc_predictions` 按 `method` 分行，每条路径各有命中率。

等积累足够天数后，用这些数据**拟合真实权重**，那时的综合才是有依据的。

## 2. 关于"资金"信号的澄清

竞彩**没有公开的成交量数据**。所谓"资金流入"在赔率数据里就表现为**赔率下降**
（买的人多，庄家压赔率）。因此「资金」与「趋势」不是两个独立信号，是同一现象的
两种表述。本设计不单列"资金"路径，其信息由 `trend` 承载。

## 3. 五条路径

| method | 逻辑 | 先验参数 |
|---|---|---|
| `market` | 去水后概率最高的选项（基线） | — |
| `trend` | 市场概率 × 漂移加成 | α=0.5 |
| `stable` | 同 `market`，但**波动过大的场次不推荐**（`pick` 为空） | 阈值 0.15 |
| `cross` | 市场概率 + **跨玩法印证**（仅 `had`，见 §3.3） | 加成 0.2 |
| `blend` | 市场 × 趋势加成 × 波动惩罚 | α_trend=0.5, α_vol=0.3 |

### 3.1 信号定义

设某玩法赔率序列为 `[o_0, o_1, ..., o_n]`（n+1 次变化，`o_0` 为初盘，`o_n` 为最新），
下标 `i` 表示**选项**（如 had 的 h/d/a）。**除 `change_count` 外，所有信号都按选项计算。**

- **市场概率**：`prob_i = devig(o_n)_i`（3 选项用 `market.shin`，多选项用比例法）
- **漂移**：`drift_i = o_n[i] - o_0[i]`（**负值 = 赔率下降 = 资金流入**）
- **归一化漂移**：`nd_i = -drift_i / o_0[i]`（正值表示被看好）
- **波动性**：`vol_i = std(o_k[i] for k in 0..n) / mean(o_k[i] for k in 0..n)`

> **`vol_i` 必须按选项计算，不能取"主选项"的单一标量。** 早期设计用标量，
> 导致 `blend` 中的波动项对所有选项是同乘因子，argmax 不变 —— `blend` 会退化成
> `trend` 的等比例缩放，等同于 `trend`。

### 3.2 各路径的评分

**评分必须按选项区分，否则 argmax 不变、路径退化。**

```python
# market —— 基线
score_i = prob_i
pick    = argmax(score)

# trend —— 漂移加成
score_i = prob_i * (1 + 0.5 * nd_i)

# stable —— 波动过滤（不改变排序，只决定是否出手）
if max(vol_i) > 0.15: pick = None      # 任一选项波动过大 → 本轮不出手
else:                 pick = argmax(prob_i)

# blend —— 趋势加成 × 波动惩罚（两项都按选项）
score_i = prob_i * (1 + 0.5 * nd_i) * (1 - 0.3 * min(vol_i, 1.0))

# cross —— 跨玩法印证（仅 had，见下）
score_i = prob_i * (1 + 0.2 * support_i)
```

### 3.3 `cross` 的推导（仅 `had`）

**为什么只做 `had`**：`hhad` 的选项是**让球后**的主/平/客，而其他玩法推导出的是
**未让球**的全场方向。拿两者比对在语义上是错的（让球 -1 时，"主胜"与"让球后主胜"
是两回事）。除非额外做让球调整，否则 `hhad` 不做 `cross`。

**推导来源**（都要计入「其他比分」桶，否则概率被低估）：

| 来源 | 主胜 | 平 | 客胜 |
|---|---|---|---|
| `hafu`（半全场） | `hh + dh + ah` | `hd + dd + ad` | `ha + da + aa` |
| `crs`（比分） | `ΣP(主>客)` **+ `s-1sh`** | `ΣP(相等)` **+ `s-1sd`** | `ΣP(客>主)` **+ `s-1sa`** |

> `crs` 实测为 **31** 个选项（28 个具体比分 + 3 个"其他比分"桶 `s-1sh/s-1sd/s-1sa`）。
> 漏掉这三个桶会系统性低估对应方向的概率。

**`support_i` 按选项计算**（这是修正早期设计的关键 —— 标量的"是否一致"会让整条
路径退化成 `market`）：

```python
derived_i = 由 hafu 与 crs 推导出的概率在 i 上的**平均**（各自先归一化到和为 1）
support_i = derived_i / max(derived)
score_i   = prob_i * (1 + 0.2 * support_i)
```

这样"被其他玩法独立印证"的选项得到加成，argmax **可能因此改变**。

**`hafu` 与 `crs` 推导不一致时**：各占一半权重（等权平均），不做主观取舍。

### 3.4 落库的特征必须能支持离线重新拟合

**只能存选项向量，不能存单个标量。** 早期设计存 `first_odds` / `latest_odds` /
`drift` / `volatility` 四个标量，问题有二：

1. 重新拟合时无法为**每个选项**单独调参（而这正是需要的）；
2. `pick = NULL`（`stable` 过滤）时这些标量无意义。

因此改为存 **JSON 向量**：

```sql
  -- 按选项对齐的向量，键为选项名（h/d/a、s0..s7、hh..aa、s02s00..）
  latest_odds_json TEXT,   -- 最新一次赔率快照
  first_odds_json  TEXT,   -- 初盘赔率快照
  drift_json       TEXT,   -- latest - first，按选项
  vol_json         TEXT,   -- 按选项的变异系数
  prob_json        TEXT,   -- 去水后的市场概率，按选项
  change_count     INTEGER -- 全局标量：该玩法的变化次数
```

外加 `provenance_json` 存 `cross` 的推导中间量（`derived` 向量），便于事后归因。

## 4. 数据模型

```sql
CREATE TABLE IF NOT EXISTS jc_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  predicted_on TEXT NOT NULL,        -- 预测日期
  pool TEXT NOT NULL,
  method TEXT NOT NULL,              -- market/trend/stable/cross/blend
  pick TEXT,                         -- 推荐选项；stable 过滤时为 NULL
  odds REAL,                         -- 推荐选项在最新赔率里的赔率
  prob REAL,                         -- 该选项的最终得分
  -- 按选项对齐的向量（JSON），支持离线重新拟合
  prob_json TEXT, first_odds_json TEXT, latest_odds_json TEXT,
  drift_json TEXT, vol_json TEXT,
  change_count INTEGER,
  provenance_json TEXT,              -- cross 的推导中间量，便于归因
  -- 对奖
  result TEXT, hit INTEGER, scored_at TEXT,
  UNIQUE(match_id, pool, predicted_on, method)
);
CREATE INDEX IF NOT EXISTS idx_jc_pred_date ON jc_predictions(predicted_on);
CREATE INDEX IF NOT EXISTS idx_jc_pred_pending ON jc_predictions(scored_at);
```

**`pick = NULL` 的行（`stable` 过滤）也必须写 `scored_at`** —— 它们没有推荐，
但对奖集合是按 `scored_at IS NULL` 捞取的；不写就会**每次 `score-jc` 都被重新捞出来**，
导致重复拉取、永远无法收敛。这类行的语义是「已处理，无需对奖」，用 `hit = NULL`、
`scored_at = 处理时刻` 表示。

## 5. 命令

```bash
predict-jc                        # 对当期在售比赛预测并存档
predict-jc --workers 8
predict-jc --date 2026-09-19      # 重放：用已存档的赔率数据补跑，不联网
score-jc                          # 对未对奖的预测对奖
score-jc --date 2026-09-19        # 只对指定预测日的
```

### 5.1 预测流程

**两种数据来源，按是否给 `--date` 分流**：

| 模式 | 数据来源 | 用途 |
|---|---|---|
| 无 `--date`（默认） | `getMatchCalculatorV1` 拿当期 matchId → 并发 `getFixedBonusV1` | 当日实时预测 |
| 有 `--date X` | **只读 `jc_odds_history` 中该日已存档的赔率序列**，不联网 | 补跑/重放 |

> `getMatchCalculatorV1` **不接受日期参数**，只返回当期在售的比赛，所以"指定日期
> 拉实时数据"是不可实现的。补跑走已存档数据，这也更可靠（不受接口时效影响）。

流程：

```
1. 取该日比赛与其 5 玩法的赔率序列
2. 对每场每玩法跑 5 条路径 → 产出推荐
3. 写入 jc_predictions（幂等：UNIQUE(match_id,pool,predicted_on,method)）
```

**依赖**：`--date` 重放要求该日的赔率已在 `jc_odds_history` 里（由
`collect-jc-history` 或 `collect-odds` 写入）。缺失时打印提示并跳过。

### 5.2 对奖流程

```
1. 找出 jc_predictions 中 scored_at IS NULL 的 (match_id, pool)
2. 取赛果，来源按优先级：
   a. jc_matches.result_*（由 collect-jc-history 写入，无需联网）
   b. 若为空 → 重新 getFixedBonusV1 拉取，并**回写 jc_matches**（下次即可离线）
3. 把 combination 归一化为赔率字段词汇（见 5.3）
4. 与 pick 比对 → 写 result / hit / scored_at
5. pick IS NULL 的行直接写 scored_at（语义：已处理，无需对奖），hit 留空
```

### 5.3 `combination` → 赔率字段的归一化

`matchResultList` 的 `combination` 编码与赔率字段**不同**，必须映射后
才能与 `pick` 比对：

| pool | `combination` 示例 | 赔率字段 | 规则 |
|---|---|---|---|
| `HAD` | `H` / `D` / `A` | `h` / `d` / `a` | 小写直接对应 |
| `HHAD` | `H` / `D` / `A` | `h` / `d` / `a` | 同上 |
| `TTG` | `"2"` | `s2` | 数字 → `s{n}`；**7 球及以上归 `s7`** |
| `HAFU` | `"H:H"` | `hh` | 半场:全场 → 拼小写（`H:D` → `hd`） |
| `CRS` | `"2:0"` | `s02s00` | 主客比分各补零两位；**非具体比分归 `s-1s{h,d,a}`** |

**遇到无法识别的 `combination` 时跳过该条并告警，不猜测。**

> 映射表与子项目 1 设计文档 §3.3 一致（那里的示例 `s02s01` 是笔误，正确形式为
> `s02s00` 这类「主客各两位」的格式，以本表为准）。

## 6. 关键实现点

### 6.1 去水：3 选项用 Shin，多选项用比例法

现有 `market.shin()` 只接受 3 个赔率（`values.shape != (3,)` 抛错）。
`crs`(**31**，实测) / `ttg`(8) / `hafu`(9) 选项数远超 3，需新增通用去水：

```python
def devig_n(odds: list[float]) -> list[float] | None:
    """n 选项去水：3 个走 Shin，其余走比例法。非法输入返回 None。"""
```

### 6.2 复用连接池

**仅在实时模式（无 `--date`）联网**，需复用 `sporttery.get_client()` ——
实测每次新建 Client 单请求 0.87s，复用后 0.05-0.18s（差 5 倍）。
每天约 28 场，规模不大，但保持与 `collect-jc-history` 一致。
`--date` 重放模式读库，不走网络。

### 6.3 幂等

同一天重跑不得产生重复行（UNIQUE 约束 + upsert）。
重跑会**覆盖**当天的推荐（因为赔率可能已变），这是期望行为。

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 当期无在售比赛 | 打印提示，返回 0（休赛日正常） |
| 某场 `getFixedBonusV1` 失败 | 跳过该场并计数，不中断整批 |
| 某场无 `oddsHistory` | 跳过（比赛取消/未开售） |
| 某玩法赔率序列为空 | 跳过该玩法 |
| 某玩法选项数 < 2 | 跳过（无法比较） |
| `score-jc` 时比赛尚未结束 | `matchResultList` 为空 → 跳过，下次再对 |
| 归一化时遇到未知 `combination` | 跳过该条并告警（不猜） |

## 8. 测试策略（全离线）

1. **去水**：`devig_n` 对 3/8/9/31 选项均返回和为 1 的概率；空值/非数字/单个选项 → None。
2. **各路径的区分度（关键回归）**：构造一组赔率，使得
   - `trend` 的 pick **不同于** `market`（漂移方向与概率排序相反时改变 argmax）；
   - `blend` 的 pick **不同于** `trend`（某个选项波动极大时被惩罚而落选）。
   **这两条专门防"路径退化"** —— 早期设计里 `cross` 和 `blend` 因使用标量而
   与基线等价，正是靠这类测试才能发现。
3. **`stable` 过滤**：波动超阈值时 `pick` 为 None；否则等于 `market` 的 pick。
4. **`cross` 推导**：
   - `hafu` 推导（`hh+dh+ah` = 全场主胜）与 `crs` 推导（含 `s-1s*` 桶）
     各自概率和为 1；
   - 构造使 `crs`/`hafu` 一致支持某选项时，`cross` 的 pick 与 `market` 不同
     （证明 `support_i` 按选项生效而非标量）；
   - `hhad` 不产出 `cross` 行。
5. **`crs` 的"其他比分"桶**：断言 `s-1sh` 计入主胜、`s-1sd` 计入平、`s-1sa` 计入客胜
   （漏掉会导致推导概率和 < 1）。
6. **入库幂等**：同一天跑两次，行数不变且内容被覆盖。
7. **对奖**：`combination` → 赔率字段的归一化正确（含 `TTG "7"→s7` 的封顶）；
   `hit` 计算正确；`pick = NULL` 的行写 `scored_at` 且 `hit` 为 NULL。
8. **对奖不重复拉取**：`score-jc` 连跑两次，第二次不再捞取已 `scored_at` 的行
   （用打桩的 `fetch_fixed_bonus` 计数验证）。
9. **未知 `combination`**：跳过并告警，不猜测、不崩溃。

## 9. 验收标准

1. `predict-jc` 跑完后，当期每场有 5 行（`had`）+ 4 行（其余 4 个玩法）= **21 行/场**
   （`cross` 只作用于 `had`）。
2. 次日 `score-jc` 后，已结束比赛的 `hit` 被正确回填；`stable` 被过滤的行
   `scored_at` 非空且 `hit` 为 NULL。
3. 数据可查：CLI 能打印当日推荐与历史命中率；SQL 能按 `method` 聚合出各路径的命中率。
   **本轮不做页面**（页面留给子项目 3）。
4. 重复执行 `predict-jc` / `score-jc` 不产生重复行；`score-jc` 反复执行不会重复拉取
   已处理过的比赛。
5. 全部测试离线通过。

## 10. 已知限制

1. **先验权重未经数据验证**（α=0.5 / 0.2 / 0.3 均为假设）——这正是本设计要解决的问题，
   记录原始特征就是为了将来重新拟合。
2. **没有真正的"成交量"信号** —— 竞彩不公开，只能用赔率变动推断。
3. **对奖依赖接口仍可用** —— 若官方下线 `getFixedBonusV1` 的历史查询，已存档的预测
   将无法对奖。
4. **`cross` 仅覆盖 had** —— `hhad` 因让球语义不匹配被排除（见 §3.3）；
   比分/总进球/半全场无法被其他玩法独立推导。

## 11. 待实现清单

- `db/schema.sql`：新增 `jc_predictions` 与索引
- 新建 `models/jc_predict.py`：
  - `devig_n()` 通用去水（3 选项走 `market.shin`，其余比例法）
  - `normalize_combination(pool, combination)` → 赔率字段名（§5.3 的表）
  - `analyze_match()` 对一场比赛产出各路径推荐（含向量特征）
  - `predict_day()` / `score_day()`
- `collectors/sporttery.py`：**需要一个 matchId 提取器** ——
  `parse_jc_odds()` 当前只返回队名与赔率、**丢掉了 matchId**，
  而 `predict-jc` 需要它去调 `getFixedBonusV1`。新增
  `parse_jc_match_ids(payload)`（或扩展 `parse_jc_odds` 保留 `match_id`）。
- `cli.py`：新增 `predict-jc` / `score-jc`
- `tests/models/test_jc_predict.py` + `tests/fixtures/`（复用已抓的
  `sporttery_fixed_bonus_sample.json`）
