import json
import sqlite3
from datetime import date

import pytest

from football_lottery import cli
from football_lottery.db import store


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    return conn


def test_lottery_tables_created():
    conn = _conn()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lottery_draw", "lottery_prediction", "lottery_backtest"} <= names


def test_daily_lottery_is_throttled_by_last_run_time(tmp_path, monkeypatch, capsys):
    """守护进程每 10 分钟转一圈，daily-lottery 不能被带着一起跑。

    判据是「上次预测距今多久」，所以这里直接构造 created_at 来验证：
    刚刚跑过 → 跳过；跑过很久 → 执行。
    """
    from datetime import datetime, timedelta

    db = tmp_path / "t.db"
    cfg = {"db_path": str(db)}
    conn = store.connect(str(db))
    store.init_db(conn)

    ran = []
    monkeypatch.setattr(cli, "cmd_daily_lottery",
                        lambda args, cfg: ran.append(1) or 0)

    cli._maybe_run_daily_lottery(conn, cfg)          # 从没跑过 → 该跑
    assert ran == [1]

    conn.execute(
        "INSERT INTO lottery_prediction(lottery, target_issue, strategy, bets, "
        "created_at) VALUES('p3','2026001','hot','[]',?)",
        (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    cli._maybe_run_daily_lottery(conn, cfg)          # 刚跑过 → 跳过
    assert ran == [1]

    old = (datetime.now() - timedelta(hours=cli.LOTTERY_REFRESH_HOURS + 1))
    conn.execute("UPDATE lottery_prediction SET created_at=?",
                 (old.isoformat(timespec="seconds"),))
    conn.commit()
    cli._maybe_run_daily_lottery(conn, cfg)          # 超时 → 再跑
    assert ran == [1, 1]
    assert "daily-lottery" in capsys.readouterr().out


def test_daily_lottery_survives_corrupt_timestamp(tmp_path, monkeypatch):
    """created_at 坏了不能把整个采集进程卡死 —— 当作「该跑了」处理。"""
    db = tmp_path / "t.db"
    cfg = {"db_path": str(db)}
    conn = store.connect(str(db))
    store.init_db(conn)
    conn.execute(
        "INSERT INTO lottery_prediction(lottery, target_issue, strategy, bets, "
        "created_at) VALUES('p3','2026001','hot','[]','坏掉的时间戳')")
    conn.commit()

    ran = []
    monkeypatch.setattr(cli, "cmd_daily_lottery",
                        lambda args, cfg: ran.append(1) or 0)
    cli._maybe_run_daily_lottery(conn, cfg)
    assert ran == [1]


@pytest.mark.parametrize("name", ["collect-lottery", "predict-lottery",
                                  "score-lottery", "backtest-lottery",
                                  "daily-lottery"])
def test_lottery_commands_actually_dispatch(monkeypatch, name):
    """main() 是 `if args.cmd ==` 硬分发，只加 set_defaults(func=...) 不会生效。

    这里把处理函数换成哨兵再真的调 main()，才能发现「子命令注册了但分发漏了」
    这种光看 parser 看不出来的问题。
    """
    monkeypatch.setattr(cli, "_load_config", lambda *a, **k: {})
    monkeypatch.setattr(cli, f"cmd_{name.replace('-', '_')}",
                        lambda args, cfg: 42)
    assert cli.main([name]) == 42


def test_collect_lottery_rejects_unknown_lottery(tmp_path, capsys):
    cfg = {"db_path": str(tmp_path / "t.db")}
    args = cli.build_parser().parse_args(["collect-lottery", "--lottery", "xxx"])
    assert cli.cmd_collect_lottery(args, cfg) == 2
    assert "未知彩种" in capsys.readouterr().out


def test_backtest_lottery_skips_when_not_enough_data(tmp_path, capsys):
    cfg = {"db_path": str(tmp_path / "t.db")}
    args = cli.build_parser().parse_args(["backtest-lottery", "--lottery", "p3"])
    assert cli.cmd_backtest_lottery(args, cfg) == 0
    assert "数据不足" in capsys.readouterr().out


def test_predict_and_score_round_trip(tmp_path, capsys, monkeypatch):
    """预测落库 → 开奖入库 → 对奖回填，整条链路要能跑通。

    `refresh_latest` 会真的连网刷最新一期，测试里必须打桩 —— 否则库外的新期号
    会改变 next_issue 的结果，测试就成了「今天跑得过、明天跑不过」。
    """
    from football_lottery.collectors import lottery_history as lh

    monkeypatch.setattr(lh, "refresh_latest", lambda conn, lottery: 0)

    # 期号用**当前年份**：next_issue 在「库里最后一年 < 今年」时会跳到下一年 001，
    # 写死 2020 会让这个测试在明年变成另一个结果
    year = date.today().year
    target = f"{year}041"
    db = tmp_path / "t.db"
    cfg = {"db_path": str(db)}
    conn = store.connect(str(db))
    store.init_db(conn)
    for i in range(1, 41):                 # 001..040 已开奖，下一期就是 041
        conn.execute(
            "INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes) "
            "VALUES('p3',?,?,?,?)",
            (f"{year}{i:03d}", f"{year}-01-01",
             json.dumps({"digits": ["1", "2", "3"]}),
             json.dumps([{"tier": "直选", "cond": "", "winners": 1, "amount": 1040}])))
    conn.commit()
    conn.close()

    args = cli.build_parser().parse_args(
        ["predict-lottery", "--lottery", "p3", "--strategy", "hot"])
    cli.cmd_predict_lottery(args, cfg)
    assert f"第 {target} 期推荐" in capsys.readouterr().out

    conn = store.connect(str(db))
    targets = [r["target_issue"] for r in conn.execute(
        "SELECT target_issue FROM lottery_prediction")]
    # 开奖号码与预测的第 1 注相同，保证必定中一注直选（1040 元）
    first = json.loads(conn.execute(
        "SELECT bets FROM lottery_prediction LIMIT 1").fetchone()["bets"])[0]
    conn.execute(
        "INSERT INTO lottery_draw(lottery, issue, draw_date, numbers, prizes) "
        "VALUES('p3',?,?,?,?)",
        (target, f"{year}-02-01", json.dumps(first),
         json.dumps([{"tier": "直选", "cond": "", "winners": 1, "amount": 1040}])))
    conn.commit()
    conn.close()
    assert set(targets) == {target}

    args = cli.build_parser().parse_args(["score-lottery"])
    cli.cmd_score_lottery(args, cfg)
    assert "已对奖 1 条预测" in capsys.readouterr().out

    conn = store.connect(str(db))
    row = conn.execute(
        "SELECT prize, created_at FROM lottery_prediction LIMIT 1").fetchone()
    conn.close()
    assert row["prize"] == 1040
    assert row["created_at"]          # 对奖不能把 created_at 覆盖成空
