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
| `cross` | 市场概率 + **跨玩法印证**（仅 `had`/`hhad`） | 加成 0.2 |
| `blend` | 市场 × 趋势加成 × 波动惩罚 | α_trend=0.5, α_vol=0.3 |

### 3.1 信号定义

设某玩法赔率序列为 `[o_0, o_1, ..., o_n]`（n+1 次变化，`o_0` 为初盘，`o_n` 为最新）：

- **市场概率**：`prob_i = devig(o_n)_i`（3 选项用 `market.shin`，多选项用比例法）
- **漂移**：`drift_i = o_n[i] - o_0[i]`（**负值 = 赔率下降 = 资金流入**）
- **归一化漂移**：`nd_i = -drift_i / o_0[i]`（正值表示被看好）
- **波动性**：`vol = std(o_k[i]) / mean(o_k[i])` 对主选项取

### 3.2 各路径的评分

```python
# market
score = prob_i
pick  = argmax(score)

# trend
score = prob_i * (1 + 0.5 * nd_i)

# stable
if vol > 0.15: pick = None      # 不推荐
else:          pick = argmax(prob_i)

# blend
score = prob_i * (1 + 0.5 * nd_i) * (1 - 0.3 * min(vol, 1.0))

# cross（仅 had/hhad）
# 用其他玩法独立推导同一概念的概率，与本玩法是否指向同一选项比对
#   hafu → P(全场主胜) = P(hh)+P(dh)+P(ah)；P(全场平) = P(hd)+P(dd)+P(ad)；P(全场客胜)= 其余
#   crs  → P(主胜) = Σ P(主>客比分)；平 = Σ P(相等)；负 = 其余
# 若本玩法的 pick 与推导来源的 argmax 一致，则加成分 +0.2
score = prob_i * (1 + 0.2 * agrees)
```

**`cross` 只作用于 `had` 与 `hhad`** —— 只有这两个玩法的结果能被其他玩法独立推导。
其余玩法不做 `cross`（无意义）。

### 3.3 存储的先验特征

除了推荐本身，**原始特征必须落库**（`first_odds` / `latest_odds` / `drift` /
`change_count` / `volatility`）。这样即使将来发现先验权重全错，也能**离线重新拟合**
而不必重跑采集。

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
  -- 原始特征
  first_odds REAL, latest_odds REAL, drift REAL,
  change_count INTEGER, volatility REAL,
  -- 对奖（次日回填）
  result TEXT, hit INTEGER, scored_at TEXT,
  UNIQUE(match_id, pool, predicted_on, method)
);
CREATE INDEX IF NOT EXISTS idx_jc_pred_date ON jc_predictions(predicted_on);
CREATE INDEX IF NOT EXISTS idx_jc_pred_scored ON jc_predictions(scored_at);
```

## 5. 命令

```bash
predict-jc                        # 对当期在售比赛预测并存档（默认今天）
predict-jc --date 2026-09-20      # 指定日期（补跑）
predict-jc --workers 8 --delay 0.05
score-jc                          # 对未对奖的预测对奖
score-jc --date 2026-09-20        # 只对指定预测日的
```

### 5.1 预测流程

```
1. getMatchCalculatorV1  → 当期在售比赛的 matchId 列表
2. 并发 getFixedBonusV1  → 每场的 5 玩法赔率序列（复用连接池）
3. 对每场每玩法跑 5 条路径 → 产出推荐
4. 写入 jc_predictions（幂等：UNIQUE(match_id,pool,predicted_on,method)）
```

### 5.2 对奖流程

```
1. 找出 jc_predictions 中 scored_at IS NULL 的 match_id
2. 拉这些比赛的 getFixedBonusV1
3. 从 matchResultList 取各玩法的 combination
4. **按 §3.3 的映射规则**把 combination 归一化为赔率字段词汇
   （CRS "2:0"→s02s00、HAFU "H:H"→hh、TTG "2"→s2）
5. 与 pick 比对 → 写 result / hit / scored_at
```

**归一化规则**照搬子项目 1 设计文档 §3.3 的映射表。

## 6. 关键实现点

### 6.1 去水：3 选项用 Shin，多选项用比例法

现有 `market.shin()` 只接受 3 个赔率（`values.shape != (3,)` 抛错）。
`crs`(32) / `ttg`(8) / `hafu`(9) 选项数远超 3，需新增通用去水：

```python
def devig_n(odds: list[float]) -> list[float] | None:
    """n 选项去水：3 个走 Shin，其余走比例法。"""
```

### 6.2 复用连接池

必须复用 `sporttery.get_client()` —— 实测每次新建 Client 单请求 0.87s，
复用后 0.05-0.18s（差 5 倍）。`predict-jc` 每天约 28 场，差距不大，
但保持与 `collect-jc-history` 一致。

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

1. **去水**：`devig_n` 对 3/8/9/32 选项均返回和为 1 的概率；非法输入返回 None。
2. **各路径**：给定构造的赔率序列，`market`/`trend`/`stable`/`blend` 的 pick 符合预期
   （如漂移为负时 `trend` 会改选被看好的选项；波动超阈值时 `stable` 的 pick 为 None）。
3. **cross 推导**：构造 `hafu`/`crs` 数据使其推导出的全场方向与 `had` 的 pick 一致 /
   不一致，断言加成是否生效。
4. **入库幂等**：同一天跑两次，行数不变且内容被覆盖。
5. **对奖**：构造 `matchResultList`，断言 `combination` → 赔率字段的归一化正确、
   `hit` 计算正确（含 pick 为 None 的 `stable` 行应记 `hit = NULL`）。
6. **别名/边界**：未知 `combination` 不猜测、不崩溃。

## 9. 验收标准

1. `predict-jc` 跑完后，当期每场每玩法有 4-5 行（`cross` 仅 had/hhad）。
2. 次日 `score-jc` 后，已结束比赛的 `hit` 被正确回填。
3. `/history` 之外**新增一个查看入口**（可与后续子项目 3 的页面合并）；
   本轮至少保证数据可通过 CLI 与 SQL 查询。
4. 重复执行 `predict-jc` / `score-jc` 不产生重复行。
5. 全部测试离线通过。

## 10. 已知限制

1. **先验权重未经数据验证**（α=0.5 / 0.2 / 0.3 均为假设）——这正是本设计要解决的问题，
   记录原始特征就是为了将来重新拟合。
2. **没有真正的"成交量"信号** —— 竞彩不公开，只能用赔率变动推断。
3. **对奖依赖接口仍可用** —— 若官方下线 `getFixedBonusV1` 的历史查询，已存档的预测
   将无法对奖。
4. **`cross` 仅覆盖 had/hhad** —— 比分/总进球/半全场无法被其他玩法独立推导。

## 11. 待实现清单

- `db/schema.sql`：新增 `jc_predictions` 与索引
- 新建 `models/jc_predict.py`：
  - `devig_n()` 通用去水
  - `analyze_match()` 对一场比赛产出 5 条路径的推荐
  - `predict_day()` / `score_day()`
- `collectors/sporttery.py`：无需改动（复用 `fetch_jc_odds` / `fetch_fixed_bonus`）
- `cli.py`：新增 `predict-jc` / `score-jc`
- `tests/models/test_jc_predict.py` + `tests/fixtures/`
