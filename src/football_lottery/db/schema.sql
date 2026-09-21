-- 足彩预测工具 SQLite schema（设计文档 §3.3）
-- IF NOT EXISTS 保证可重复执行

CREATE TABLE IF NOT EXISTS leagues (
  code TEXT PRIMARY KEY, name_cn TEXT, name_en TEXT);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name_en TEXT NOT NULL UNIQUE, name_cn TEXT);

CREATE TABLE IF NOT EXISTS team_alias (
  name_cn TEXT PRIMARY KEY,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  confirmed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT);

CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  league_code TEXT NOT NULL REFERENCES leagues(code),
  season TEXT NOT NULL,
  match_date TEXT NOT NULL,
  home_team_id INTEGER NOT NULL REFERENCES teams(id),
  away_team_id INTEGER NOT NULL REFERENCES teams(id),
  home_goals INTEGER, away_goals INTEGER, result TEXT,
  odds_json TEXT,
  shots_json TEXT,
  UNIQUE(league_code, season, match_date, home_team_id, away_team_id));
CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(match_date);
CREATE INDEX IF NOT EXISTS idx_matches_league_season ON matches(league_code, season);

CREATE TABLE IF NOT EXISTS periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_no TEXT NOT NULL UNIQUE,
  draw_date TEXT, sale_end TEXT, status TEXT);

CREATE TABLE IF NOT EXISTS period_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  seq INTEGER NOT NULL,
  home_name_cn TEXT NOT NULL, away_name_cn TEXT NOT NULL,
  match_time TEXT,
  match_id INTEGER REFERENCES matches(id),
  odds_json TEXT,
  league_cn TEXT,
  UNIQUE(period_id, seq));

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_match_id INTEGER NOT NULL REFERENCES period_matches(id),
  source TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  update_time TEXT,
  h REAL, d REAL, a REAL,
  UNIQUE(period_match_id, source, captured_at));
CREATE INDEX IF NOT EXISTS idx_odds_snapshots_pm ON odds_snapshots(period_match_id);

CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_match_id INTEGER NOT NULL REFERENCES period_matches(id),
  model_version TEXT NOT NULL,
  market_json TEXT, dc_json TEXT, gbdt_json TEXT, fused_json TEXT,
  created_at TEXT,
  UNIQUE(period_match_id, model_version));

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  game_type TEXT NOT NULL,
  objective TEXT NOT NULL,
  budget INTEGER NOT NULL, notes_count INTEGER NOT NULL,
  p_first REAL, p_second REAL, p_win REAL,
  legs_json TEXT NOT NULL, created_at TEXT);

CREATE TABLE IF NOT EXISTS draw_results (
  period_id INTEGER PRIMARY KEY REFERENCES periods(id),
  results_json TEXT NOT NULL,
  prizes_json TEXT);

CREATE TABLE IF NOT EXISTS winnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id INTEGER NOT NULL REFERENCES periods(id),
  plan_id INTEGER NOT NULL REFERENCES plans(id),
  tier TEXT NOT NULL,
  hit_notes INTEGER NOT NULL,
  single_prize REAL,
  amount REAL,
  UNIQUE(plan_id, tier));

-- 竞彩历史数据同步（子项目 1/3，见 docs/superpowers/specs/2026-09-20-jc-history-sync-design.md）
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
  captured_at TEXT);
CREATE INDEX IF NOT EXISTS idx_jc_matches_date ON jc_matches(match_date);

-- 每次赔率变化一行；各玩法的完整选项存 JSON（crs 单条即 65 字段，不宜宽表）
CREATE TABLE IF NOT EXISTS jc_odds_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  pool TEXT NOT NULL,
  seq INTEGER NOT NULL,
  update_date TEXT, update_time TEXT,
  goal_line TEXT,
  odds_json TEXT NOT NULL,
  UNIQUE(match_id, pool, update_date, update_time));
CREATE INDEX IF NOT EXISTS idx_jc_odds_match ON jc_odds_history(match_id, pool, seq);

-- 断点续传：记录已完成的日期
CREATE TABLE IF NOT EXISTS jc_sync_log (
  sync_date TEXT PRIMARY KEY,
  matches INTEGER NOT NULL DEFAULT 0,
  odds_rows INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  failed_match_ids TEXT,
  finished_at TEXT);

-- 竞彩当期预测与对奖（见 docs/superpowers/specs/2026-09-20-jc-prediction-design.md）
-- 每条 method 一行：并行记录各路径，让数据淘汰弱路径，避免黑盒综合。
CREATE TABLE IF NOT EXISTS jc_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  predicted_on TEXT NOT NULL,
  pool TEXT NOT NULL,
  method TEXT NOT NULL,
  pick TEXT,
  odds REAL,
  prob REAL,
  -- 按选项对齐的向量（JSON）：先验权重都是假设，存全了才能离线重新拟合
  prob_json TEXT, first_odds_json TEXT, latest_odds_json TEXT,
  drift_json TEXT, vol_json TEXT,
  change_count INTEGER,
  provenance_json TEXT,
  result TEXT, hit INTEGER, scored_at TEXT,
  UNIQUE(match_id, pool, predicted_on, method));
CREATE INDEX IF NOT EXISTS idx_jc_pred_date ON jc_predictions(predicted_on);
CREATE INDEX IF NOT EXISTS idx_jc_pred_pending ON jc_predictions(scored_at);

-- 竞彩串关方案（回测得出的最优玩法：按市场概率选场 + 多重串关组合）
CREATE TABLE IF NOT EXISTS jc_parlay_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_date TEXT NOT NULL,
  pool TEXT NOT NULL,
  n_legs INTEGER NOT NULL,
  unit REAL NOT NULL,
  min_combo INTEGER NOT NULL,
  legs_json TEXT NOT NULL,        -- [{match_id, home, away, pick, odds, prob, hit}]
  bets_json TEXT NOT NULL,        -- [{legs:[match_id...], size, amount}]
  invested REAL,
  returned REAL,
  winning_bets INTEGER,
  scored_at TEXT,
  created_at TEXT,
  UNIQUE(plan_date, pool));
CREATE INDEX IF NOT EXISTS idx_jc_parlay_date ON jc_parlay_plans(plan_date);

CREATE TABLE IF NOT EXISTS model_versions (
  version TEXT PRIMARY KEY, model_type TEXT NOT NULL,
  params_json TEXT, metrics_json TEXT, trained_at TEXT);

-- 数字彩开奖（大乐透/双色球/排列三/排列五/福彩3D）
--
-- 与 jc_odds_history 的 append-only 不同：开奖号码是**既成事实**，重抓同一期
-- 应当得到完全相同的值。若不同，只可能是数据源修正或我方解析 bug —— 所以这里
-- 用 upsert 覆盖写入，而不是追加快照。
CREATE TABLE IF NOT EXISTS lottery_draw (
  lottery     TEXT NOT NULL,     -- dlt/ssq/p3/p5/3d
  issue       TEXT NOT NULL,     -- '2026107'
  draw_date   TEXT NOT NULL,     -- 'YYYY-MM-DD'
  numbers     TEXT NOT NULL,     -- {"front":["02",...],"back":["04","10"]} 或 {"digits":["0","6","4"]}
  draw_order  TEXT,              -- JSON 出球顺序；排列类无此字段
  sales       INTEGER,           -- 本期投注金额（元）
  jackpot     INTEGER,           -- 滚入下期奖金（元）
  prizes      TEXT,              -- [{"tier":"一等奖","cond":"5+2","winners":3,"amount":10000000}]
  fetched_at  TEXT,
  PRIMARY KEY (lottery, issue));
CREATE INDEX IF NOT EXISTS idx_lottery_draw_date ON lottery_draw(lottery, draw_date);

-- 每期每策略的推荐注单；开奖后回填 hits/prize。术语用 bets 与 jc_parlay_plans.bets_json 一致。
CREATE TABLE IF NOT EXISTS lottery_prediction (
  lottery      TEXT NOT NULL,
  target_issue TEXT NOT NULL,
  strategy     TEXT NOT NULL,
  bets         TEXT NOT NULL,    -- JSON 注单列表，一注一个元素
  created_at   TEXT NOT NULL,
  hits         TEXT,             -- JSON 每注命中明细
  prize        INTEGER,          -- 该策略该期总奖金（元）
  PRIMARY KEY (lottery, target_issue, strategy));

-- 回测结果落库，供页面读取（页面实时跑回测太慢）
CREATE TABLE IF NOT EXISTS lottery_backtest (
  lottery  TEXT NOT NULL,
  strategy TEXT NOT NULL,
  params   TEXT,                 -- JSON {window, bets, draws}
  metrics  TEXT,                 -- JSON {roi, invested, returned, avg_hits, win_rate}
  paired   TEXT,                 -- JSON {se, ci_low, ci_high, p, beats_random}
  ran_at   TEXT,
  PRIMARY KEY (lottery, strategy));
