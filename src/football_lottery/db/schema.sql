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

CREATE TABLE IF NOT EXISTS model_versions (
  version TEXT PRIMARY KEY, model_type TEXT NOT NULL,
  params_json TEXT, metrics_json TEXT, trained_at TEXT);
