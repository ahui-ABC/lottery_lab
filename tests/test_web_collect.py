"""采集页渲染：三组任务按钮 + 守护进程面板。"""
import re

import pytest
from fastapi.testclient import TestClient

from football_lottery import jobs
from football_lottery.db import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.close()
    (tmp_path / "config.yaml").write_text(f"db_path: {db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # 日志目录也挪走，别往真实项目里写
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "GLOBAL_LOCK", tmp_path / "jobs" / "running.lock")
    from football_lottery.web import app as web_app
    return TestClient(web_app.app)


def test_collect_page_renders_three_groups(client):
    body = client.get("/collect").text
    for group in jobs.GROUP_ORDER:
        assert f"<h3>{group}</h3>" in body


def test_every_job_has_a_row_with_buttons(client):
    body = client.get("/collect").text
    for spec in jobs.JOBS:
        assert f'data-key="{spec.key}"' in body
        assert spec.label in body
    assert body.count('class="job-row"') == len(jobs.JOBS)
    assert body.count('class="job-start"') == len(jobs.JOBS)
    assert body.count('class="job-stop"') == len(jobs.JOBS)


def test_jokes_render_in_sidebar_order(client):
    """组内顺序要与 jobs.grouped() 一致，否则页面和注册表会对不上。"""
    body = client.get("/collect").text
    positions = [body.index(f'data-key="{s.key}"')
                 for _, specs in jobs.grouped() for s in specs]
    assert positions == sorted(positions)


def test_daemon_panel_is_still_there(client):
    body = client.get("/collect").text
    for element_id in ("daemon-state", "daemon-start", "daemon-stop", "daemon-log"):
        assert f'id="{element_id}"' in body
    assert "自动轮询" in body


def test_collect_page_mentions_the_serial_constraint(client):
    """串行限制要写在页面上 —— 用户点了第二个按钮没反应时得知道为什么。"""
    body = client.get("/collect").text
    assert "只允许跑一个任务" in body


def test_jobs_api_lists_every_job(client):
    data = client.get("/api/jobs").json()
    assert {j["key"] for j in data["jobs"]} == {s.key for s in jobs.JOBS}
    assert data["running_key"] is None


def test_start_unknown_job_reports_in_message(client):
    resp = client.post("/api/jobs/nope/start")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "未知任务" in resp.json()["message"]


def test_stop_when_idle_reports_in_message(client):
    resp = client.post("/api/jobs/daily-jc/stop")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
