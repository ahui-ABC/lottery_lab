"""fixture_match 测试。"""
import json
from datetime import date
from lottery_lab.db import store
from lottery_lab.collectors import fd
from lottery_lab.collectors import fixture_match
from lottery_lab.collectors import team_alias


def _seed_db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _put_history(conn):
    """放入两场测试历史：曼城 vs 阿森纳、利物浦 vs 曼联。"""
    # league
    store.upsert(conn, "leagues",
                 {"code": "E0", "name_cn": "英超", "name_en": "Premier League"},
                 ["code"])
    # 直接构造 matches (避免联网)
    store.upsert(conn, "teams", {"name_en": "Man City", "name_cn": "曼城"}, ["name_en"])
    store.upsert(conn, "teams", {"name_en": "Arsenal", "name_cn": "阿森纳"}, ["name_en"])
    store.upsert(conn, "teams", {"name_en": "Liverpool", "name_cn": "利物浦"}, ["name_en"])
    store.upsert(conn, "teams", {"name_en": "Man United", "name_cn": "曼联"}, ["name_en"])
    store.upsert(conn, "teams", {"name_en": "Chelsea", "name_cn": "切尔西"}, ["name_en"])
    store.upsert(conn, "teams", {"name_en": "Tottenham", "name_cn": "热刺"}, ["name_en"])
    # 2026-09-19: 曼城 v 阿森纳 (seq=1)
    store.upsert(conn, "matches", {
        "league_code": "E0", "season": "2026/2027",
        "match_date": "2026-09-19",
        "home_team_id": 1, "away_team_id": 2,
        "home_goals": 2, "away_goals": 1, "result": "H",
        "odds_json": "{}", "shots_json": "{}",
    }, ["league_code", "season", "match_date", "home_team_id", "away_team_id"])
    # 2026-09-19: 利物浦 v 曼联 (seq=2)
    store.upsert(conn, "matches", {
        "league_code": "E0", "season": "2026/2027",
        "match_date": "2026-09-19",
        "home_team_id": 3, "away_team_id": 4,
        "home_goals": 0, "away_goals": 0, "result": "D",
        "odds_json": "{}", "shots_json": "{}",
    }, ["league_code", "season", "match_date", "home_team_id", "away_team_id"])


def test_match_by_date_and_names(tmp_path):
    conn = _seed_db(tmp_path)
    _put_history(conn)
    # 写入 period: 3 场对阵，前 2 场应匹配
    from lottery_lab.collectors import sporttery
    csv = (
        'period_no,seq,home_cn,away_cn,match_time,result,draw_14,first_prize,second_prize,r9_prize,draw_date\n'
        '26001,1,曼城,阿森纳,2026-09-19T20:00,3,"3,1,0,3,1,0,3,1,0,3,1,0,3,1",,\n'
        '26001,2,利物浦,曼联,2026-09-19T20:00,1,,\n'
        '26001,3,切尔西,热刺,2026-09-19T20:00,0,,\n'
    )
    (tmp_path / "fx.csv").write_text(csv, encoding="utf-8-sig")
    sporttery.import_fixtures_csv(conn, tmp_path / "fx.csv")
    out = fixture_match.match_period(conn, "26001", seed=team_alias.SEED)
    assert out["matched"] == 2
    # 切尔西 vs 热刺 没有历史数据（我们没放入），会未匹配
    assert 3 in out["unmatched"]
    # 校验 match_id 是否真的写入了
    rows = list(conn.execute(
        "SELECT seq, match_id FROM period_matches WHERE period_id=(SELECT id FROM periods WHERE period_no='26001') ORDER BY seq"
    ))
    assert rows[0]["match_id"] is not None
    assert rows[1]["match_id"] is not None
    assert rows[2]["match_id"] is None
