import json
import re

import pytest
from fastapi.testclient import TestClient

from football_lottery.collectors import lottery_history as lh
from football_lottery.db import store

CODES = ["dlt", "ssq", "p3", "p5", "3d"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小可用站点：p3 有一期开奖 + 一条已对奖的预测，其余彩种空库。

    `_get_conn()` 每次请求都重读 config.yaml，所以 chdir 之后再写它就够了。
    """
    db = tmp_path / "t.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.execute(
        "INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes) "
        "VALUES('p3','2020001','2020-01-01',?,?)",
        (json.dumps({"digits": ["0", "6", "4"]}),
         json.dumps([{"tier": "直选", "cond": "", "winners": 6942,
                      "amount": 1040}], ensure_ascii=False)))
    for strategy in ("random", "hot"):
        conn.execute(
            """INSERT INTO lottery_prediction(lottery, target_issue, strategy,
                                              bets, created_at, prize)
               VALUES('p3','2020001',?,'[{"digits":["0","6","4"]}]',
                      '2020-01-01T10:00:00', ?)""",
            (strategy, 1040 if strategy == "hot" else 0))
    conn.commit()
    conn.close()

    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lh, "refresh_latest", lambda conn, lottery: 0)
    from football_lottery.web import app as web_app
    return TestClient(web_app.app)


# ---- 总览页 ----------------------------------------------------------------

def test_index_lists_all_five_lotteries(client):
    body = client.get("/lottery").text
    for name in ("大乐透", "双色球", "排列三", "排列五", "福彩3D"):
        assert name in body
    assert "摇奖机是独立同分布的" in body


def test_index_links_to_each_detail_page(client):
    body = client.get("/lottery").text
    for code in CODES:
        assert f'href="/lottery/{code}"' in body


def test_index_shows_empty_hint_for_games_without_data(client):
    assert "库内暂无数据" in client.get("/lottery").text


def test_index_shows_profit_per_strategy(client):
    """hot 中了一注直选（返还 1040）而 random 没中 —— 两行都要如实反映。"""
    body = client.get("/lottery").text
    assert "已统计 <b>1</b> 期" in body
    assert "¥1038" in body          # 返还 1040 − 投入 1 注 × 2 元
    assert "-¥2" in body            # 负数写在 ¥ 外面，不是 "¥-2"


def test_index_survives_empty_database(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from football_lottery.web import app as web_app
    resp = TestClient(web_app.app).get("/lottery")
    assert resp.status_code == 200
    assert "库内暂无数据" in resp.text


# ---- 单彩种详情页 ----------------------------------------------------------

@pytest.mark.parametrize("code", CODES)
def test_every_lottery_has_a_detail_page(client, code):
    resp = client.get(f"/lottery/{code}")
    assert resp.status_code == 200
    assert "<h2>" in resp.text


def test_detail_shows_draw_numbers_as_balls(client):
    body = client.get("/lottery/p3").text
    assert 'class="ball digit">0<' in body
    assert 'class="ball digit">6<' in body
    assert 'class="ball digit">4<' in body


def test_detail_shows_next_issue_and_track_table(client):
    body = client.get("/lottery/p3").text
    assert "2021001" in body          # 库里最新是 2020001，下一期跨年
    assert "实际盈亏" in body
    assert "中奖期数" in body


def test_detail_shows_backtest_and_frequency_sections(client):
    body = client.get("/lottery/p3").text
    assert "历史回测" in body
    assert "近 100 期频次与遗漏" in body


def test_detail_without_any_data_still_renders(client):
    body = client.get("/lottery/dlt").text
    assert "库内暂无数据" in body


def test_unknown_lottery_returns_404(client):
    """未知彩种必须 404。静默回落到第一个会让人以为自己在看大乐透。"""
    assert client.get("/lottery/nope").status_code == 404
    assert client.get("/lottery/999").status_code == 404


# ---- 导航 ------------------------------------------------------------------

def test_sidebar_holds_the_menu(client):
    """菜单在左侧栏里，不是顶部横条。"""
    body = client.get("/lottery").text
    assert '<aside class="sidebar">' in body
    assert "<nav" not in body, "顶部导航已经换成左侧栏，不该再有 <nav>"


def test_sidebar_lists_both_sections(client):
    body = client.get("/lottery").text
    assert re.search(r'<div class="nav-section">竞彩</div>', body)
    assert re.search(r'<div class="nav-section">数字彩</div>', body)
    # 竞彩那一大块里再分「胜负彩」「竞彩」两个小标题
    assert re.search(r'<div class="nav-group">胜负彩</div>', body)
    assert re.search(r'<div class="nav-group">竞彩</div>', body)


@pytest.mark.parametrize("path", [
    "/predict", "/plan", "/history", "/backtest", "/jc",
    "/lottery", "/lottery/dlt", "/lottery/ssq", "/lottery/p3",
    "/lottery/p5", "/lottery/3d", "/collect",
])
def test_active_link_follows_current_page(client, path):
    """当前页的菜单项要高亮，且只高亮一项。"""
    body = client.get(path).text
    active = re.findall(r'<a class="nav-link active" href="([^"]+)"', body)
    assert active == [path]


def test_overview_is_reachable_from_the_brand(client):
    body = client.get("/lottery").text
    assert re.search(r'<a class="brand" href="/">足彩分析</a>', body)
    assert client.get("/").status_code == 200


def test_top_level_overview_and_collect_still_work(client):
    assert client.get("/").status_code == 200
    assert client.get("/collect").status_code == 200
