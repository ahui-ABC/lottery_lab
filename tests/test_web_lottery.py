import json

import pytest
from fastapi.testclient import TestClient

from football_lottery.collectors import lottery_history as lh
from football_lottery.db import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小可用的 /lottery 页面：空库 + 五类彩种各一期真实结构的数据。

    `_get_conn()` 每次请求都重读 config.yaml，所以在 chdir 之后再写它就够了。
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
    conn.commit()
    conn.close()

    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lh, "refresh_latest", lambda conn, lottery: 0)
    return TestClient(_app())


def _app():
    from football_lottery.web import app as web_app
    return web_app.app


def test_lottery_page_renders_all_games(client):
    resp = client.get("/lottery")
    assert resp.status_code == 200
    body = resp.text
    for name in ("大乐透", "双色球", "排列三", "排列五", "福彩3D"):
        assert name in body
    assert "摇奖机是独立同分布的" in body


def test_lottery_page_shows_empty_hint_for_games_without_data(client):
    """没有数据的彩种要显示提示，而不是报错或渲染成空表。"""
    body = client.get("/lottery").text
    assert "库内暂无数据" in body


def test_lottery_page_renders_draw_numbers_as_balls(client):
    body = client.get("/lottery").text
    assert 'class="ball digit">0<' in body
    assert 'class="ball digit">6<' in body
    assert 'class="ball digit">4<' in body


def test_lottery_page_shows_next_issue_hint(client):
    """库里最新是 2020001，页面要提示下一期 —— 期号跨年时返回 2021001。"""
    body = client.get("/lottery").text
    assert "2021001" in body


def test_lottery_page_survives_empty_database(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resp = TestClient(_app()).get("/lottery")
    assert resp.status_code == 200
    assert "库内暂无数据" in resp.text


def test_nav_has_lottery_tab(client):
    body = client.get("/lottery").text
    assert 'href="/lottery"' in body
    assert 'class="active"' in body
