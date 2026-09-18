"""data_health 测试。"""
import json
from football_lottery.db import store
from football_lottery.cli import data_health


def test_data_health_returns_dict(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    out = data_health(conn)
    assert isinstance(out, dict)
    assert "matches_total" in out
    assert "odds_missing_rate" in out
    assert "unmapped_fixtures" in out
    assert "unconfirmed_aliases" in out
    # 空库下数字都该是 0
    assert out["matches_total"] == 0
    assert out["odds_missing_rate"] == 0.0
    assert out["unmapped_fixtures"] == 0
