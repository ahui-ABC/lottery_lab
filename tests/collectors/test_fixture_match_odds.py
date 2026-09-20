from football_lottery.collectors import fixture_match
from football_lottery.db import store


def test_match_period_preserves_existing_odds_json():
    """锁定：重跑映射不得用 NULL 覆写 period_matches.odds_json。"""
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO periods(period_no, draw_date, status) VALUES('26080','2026-05-25','historical')"
    )
    pid = conn.execute("SELECT id FROM periods").fetchone()["id"]
    conn.execute(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn, odds_json)
           VALUES(?, 1, '伯恩利', '狼队', '{"avg":{"h":2.36,"d":3.5,"a":2.77}}')""",
        (pid,),
    )
    conn.commit()

    fixture_match.match_period(conn, "26080")

    row = conn.execute(
        "SELECT match_id, odds_json FROM period_matches WHERE period_id=? AND seq=1", (pid,)
    ).fetchone()
    assert row["match_id"] is None          # 库里没有对应比赛，映射必然失败
    assert row["odds_json"] == '{"avg":{"h":2.36,"d":3.5,"a":2.77}}'   # 但赔率必须保留
