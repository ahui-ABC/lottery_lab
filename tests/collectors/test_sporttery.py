"""体彩适配器 / fixture 导入测试（不联网）。"""
import json

from lottery_lab.db import store
from lottery_lab.collectors import sporttery


CSV = (
    'period_no,seq,home_cn,away_cn,match_time,result,draw_14,first_prize,second_prize,r9_prize,draw_date\n'
    '26001,1,曼城,阿森纳,2026-09-19T20:00,3,"3,1,0,3,1,0,3,1,0,3,1,0,3,1",10000,2000,500,2026-09-19\n'
    '26001,2,利物浦,曼联,2026-09-19T20:00,1,,\n'
    '26001,3,切尔西,热刺,2026-09-19T20:00,0,,\n'
    '26002,1,拜仁,多特,2026-09-22T20:00,3,"3,3,1,1,0,3,0,1,0,3,3,3,1,0",50000,3000,1000,2026-09-22\n'
)


def test_import_fixtures_writes_db(tmp_path):
    csv_path = tmp_path / "fx.csv"
    csv_path.write_text(CSV, encoding="utf-8-sig")
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = sporttery.import_fixtures_csv(conn, csv_path)
    # 期数
    assert set(out.keys()) == {"26001", "26002"}
    assert out["26001"]["period"] == 1 and out["26002"]["period"] == 1
    # per-period match count
    assert out["26001"]["matches"] == 3 and out["26002"]["matches"] == 1
    # DB 校验
    pm_count = conn.execute(
        """SELECT COUNT(*) c FROM period_matches pm
           JOIN periods p ON p.id=pm.period_id
           WHERE p.period_no='26001'"""
    ).fetchone()["c"]
    assert pm_count == 3
    # draw_results 第一期写入 — 由 seq=1 行结果字段提供
    dr = conn.execute(
        """SELECT dr.results_json, dr.prizes_json FROM draw_results dr
           JOIN periods p ON p.id=dr.period_id
           WHERE p.period_no='26001'"""
    ).fetchone()
    assert dr is not None
    assert dr["results_json"].count(",") == 13   # 14 字段
    prizes = json.loads(dr["prizes_json"])
    assert prizes["first"] == 10000 and prizes["second"] == 2000 and prizes["r9"] == 500


def test_import_idempotent(tmp_path):
    csv_path = tmp_path / "fx.csv"
    csv_path.write_text(CSV, encoding="utf-8-sig")
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    sporttery.import_fixtures_csv(conn, csv_path)
    cnt1 = conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"]
    sporttery.import_fixtures_csv(conn, csv_path)
    cnt2 = conn.execute("SELECT COUNT(*) c FROM period_matches").fetchone()["c"]
    assert cnt1 == cnt2
