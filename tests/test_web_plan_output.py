"""推荐方案输出：每场一行 + 队名 + 中文胜平负。"""
import pytest

from football_lottery.db import store
from football_lottery.web import app as web_app

FIXTURES = [
    ("伯恩茅斯", "利物浦"), ("利兹联", "水晶宫"), ("曼城", "桑德兰"),
    ("富勒姆", "曼联"), ("勒沃库森", "莱比锡红牛"), ("沙尔克04", "埃沃斯堡"),
    ("弗洛西诺内", "科莫"), ("帕尔马", "热那亚"), ("尤文图斯", "亚特兰大"),
    ("AC米兰", "莱切"), ("马德里竞技", "皇家马德里"), ("拉科鲁尼亚", "皇家贝蒂斯"),
    ("巴伦西亚", "皇家社会"), ("马赛", "巴黎圣日尔曼"),
]


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    c.execute(
        """INSERT INTO periods(period_no, draw_date, sale_end, status)
           VALUES('26131', '2026-09-21', '2099-01-01 20:30:00', 'current')"""
    )
    pid = c.execute("SELECT id FROM periods").fetchone()["id"]
    c.executemany(
        """INSERT INTO period_matches(period_id, seq, home_name_cn, away_name_cn)
           VALUES(?, ?, ?, ?)""",
        [(pid, i, h, a) for i, (h, a) in enumerate(FIXTURES, start=1)],
    )
    c.commit()
    return c


def _probs():
    # 14 行，每行 [主胜, 平, 客胜]，和为 1
    return [[0.45, 0.30, 0.25]] * 14


def test_plan_outputs_one_row_per_match_with_team_names(conn, monkeypatch):
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_plan(web_app.PlanReq(
        game_type="sfc14", objective="first_second", budget=64, probs=_probs(),
        period_no="26131",
    ))

    detail = got["legs_detail"]
    assert len(detail) == 14

    first = detail[0]
    assert first["seq"] == 1
    assert first["home"] == "伯恩茅斯"
    assert first["away"] == "利物浦"
    # 单选一个结果 → labels 恰好一个中文名
    assert set(first["labels"]) <= {"胜", "平", "负"}
    assert len(first["labels"]) == len(first["selection"])


def test_plan_labels_map_310_to_chinese(conn, monkeypatch):
    """3=胜（主胜）、1=平、0=负（主负=客胜）。"""
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_plan(web_app.PlanReq(
        game_type="sfc14", objective="first_second", budget=64, probs=_probs(),
        period_no="26131",
    ))

    mapping = {"3": "胜", "1": "平", "0": "负"}
    for row in got["legs_detail"]:
        for code, label in zip(row["selection"], row["labels"]):
            assert label == mapping[code]


def test_plan_reports_multi_selection_labels_in_one_row(conn, monkeypatch):
    """双选/三选的中文标签合并在一行，不展开成多行。"""
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    # 让概率极端分散，迫使求解器在多场做多选
    probs = [[0.34, 0.33, 0.33]] * 14
    got = web_app.api_plan(web_app.PlanReq(
        game_type="sfc14", objective="first_second", budget=64, probs=probs,
        period_no="26131",
    ))

    multi = [r for r in got["legs_detail"] if len(r["selection"]) > 1]
    assert multi, "预算有限且概率分散时应当出现多选场次"
    for row in multi:
        assert len(row["labels"]) == len(row["selection"])


def test_plan_still_returns_rows_for_callers(conn, monkeypatch):
    """rows（展开明细）仍保留在响应里，只是前端不再渲染。"""
    monkeypatch.setattr(web_app, "_get_conn", lambda: conn)

    got = web_app.api_plan(web_app.PlanReq(
        game_type="sfc14", objective="first_second", budget=64, probs=_probs(),
        period_no="26131",
    ))

    assert got["rows"]
    assert all(len(r) == 14 for r in got["rows"])
