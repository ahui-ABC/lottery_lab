"""football-data.co.uk 采集器（设计文档 §3.1 / 实现计划 T3）。

数据源：`https://www.football-data.co.uk/mmz4281/{赛季}/{联赛}.csv`
- 直连可达（已验证，HTTP 200，2024/25 英超 381 行）。
- CSV 头部为各家博彩公司欧赔（Bet365/Betway/Pinnacle/WilliamHill/Avg/Max ...）。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import httpx

BASE = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"

# 设计文档 §4.1：均价 / 最高价是接下来回测选优的两个口径（默认 avg）。
# 同时保留单一公司 Bet365 作降级参照。
ODDS_COLS = {
    "avg":  {"h": "AvgH",  "d": "AvgD",  "a": "AvgA"},
    "max":  {"h": "MaxH",  "d": "MaxD",  "a": "MaxA"},
    "b365": {"h": "B365H", "d": "B365D", "a": "B365A"},
}

LEAGUE_NAMES_CN = {
    "E0": "英超", "E1": "英冠",
    "D1": "德甲", "D2": "德乙",
    "I1": "意甲", "I2": "意乙",
    "SP1": "西甲", "SP2": "西乙",
    "F1": "法甲", "F2": "法乙",
    "N1": "荷甲",
    "P1": "葡超",
    "SC0": "苏超",
}


def parse_date(s: str) -> str:
    """football-data 日期格式 DD/MM/YYYY 或 DD/MM/YY → ISO。"""
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {s!r}")


def download(season: str, div: str, timeout: int = 30) -> str:
    """下载某 (赛季, 联赛) 的 CSV 文本。指数退避重试 3 次。"""
    url = BASE.format(season=season, div=div)
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return r.content.decode("utf-8-sig")
        except Exception as e:  # 网络抖动 / 解码错误等
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"下载失败 {url}") from last


def parse_csv(text: str) -> list[dict]:
    """解析 CSV 文本 → 标准化字段列表（不含 league_code / season）。"""
    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        if not (row.get("HomeTeam") or "").strip():
            continue
        odds: dict[str, dict[str, float]] = {}
        for name, cols in ODDS_COLS.items():
            try:
                odds[name] = {k: float(row[c]) for k, c in cols.items()}
            except (KeyError, TypeError, ValueError):
                pass
        shots: dict[str, int] = {}
        for key, col in (("hs", "HS"), ("as", "AS"),
                         ("hst", "HST"), ("ast", "AST")):
            try:
                shots[key] = int(row[col])
            except (KeyError, TypeError, ValueError):
                pass
        out.append({
            "date": parse_date(row["Date"]),
            "home": row["HomeTeam"].strip(),
            "away": row["AwayTeam"].strip(),
            "home_goals": int(row["FTHG"]) if row.get("FTHG") else None,
            "away_goals": int(row["FTAG"]) if row.get("FTAG") else None,
            "result": (row.get("FTR") or "").strip() or None,
            "odds": odds,
            "shots": shots,
        })
    return out


def _team_id(conn: sqlite3.Connection, name_en: str) -> int:
    """upsert by name_en，返回内部 id。"""
    row = conn.execute("SELECT id FROM teams WHERE name_en=?", (name_en,)).fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO teams(name_en, name_cn) VALUES(?, NULL)", (name_en,)
    )
    return cur.lastrowid


def _ensure_league(conn: sqlite3.Connection, div: str) -> None:
    from football_lottery.db import store
    store.upsert(
        conn,
        "leagues",
        {"code": div, "name_cn": LEAGUE_NAMES_CN.get(div, div),
         "name_en": div},
        ["code"],
    )


def _season_label(season: str) -> str:
    """'2425' → '2024/25' 用于 matches.season。"""
    return f"20{season[:2]}/20{season[2:]}"


def collect_from_csv(
    conn: sqlite3.Connection,
    csv_path: str | Path,
    div: str,
    season: str,
    silent: bool = False,
) -> int:
    """从本地 CSV 文件入库。返回新增比赛条数。"""
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    rows = parse_csv(text)
    _ensure_league(conn, div)
    season_label = _season_label(season)
    n = 0
    from football_lottery.db import store
    for r in rows:
        h_id = _team_id(conn, r["home"])
        a_id = _team_id(conn, r["away"])
        store.upsert(
            conn,
            "matches",
            {
                "league_code": div,
                "season": season_label,
                "match_date": r["date"],
                "home_team_id": h_id,
                "away_team_id": a_id,
                "home_goals": r["home_goals"],
                "away_goals": r["away_goals"],
                "result": r["result"],
                "odds_json": json.dumps(r["odds"], ensure_ascii=False),
                "shots_json": json.dumps(r["shots"], ensure_ascii=False),
            },
            ["league_code", "season", "match_date", "home_team_id", "away_team_id"],
        )
        n += 1
    if not silent:
        print(f"{div} {season}: {n} rows")
    return n


def collect(
    conn: sqlite3.Connection,
    sources: Iterable[tuple[Path | str, str, str]],
    silent: bool = False,
) -> int:
    """批量入库：sources = [(csv_path_or_url, div, season), ...]。"""
    total = 0
    for src, div, season in sources:
        if isinstance(src, (str, Path)) and not str(src).startswith("http"):
            total += collect_from_csv(conn, src, div, season, silent=silent)
        else:
            text = download(season, div)
            rows = parse_csv(text)
            # 写一个临时文件后走 collect_from_csv 路径，统一行为
            tmp = Path("data") / f"_fd_{div}_{season}.csv"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8-sig")
            total += collect_from_csv(conn, tmp, div, season, silent=silent)
    return total


# 判定「这个赛季已经下过了」的最少场次数。一个完整赛季最小的联赛也有近 200 场，
# 用 100 做下限既能挡住半截数据被永久冻住，又不会误判。
_SEASON_FLOOR = 100


def _season_loaded(conn: sqlite3.Connection, season: str, div: str) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE season=? AND league_code=?",
        (_season_label(season), div)).fetchone()[0]
    return n >= _SEASON_FLOOR


def fetch_and_collect(
    conn: sqlite3.Connection, seasons: list[str], divisions: list[str],
    silent: bool = False, force: bool = False,
) -> int:
    """按赛季×联赛组合联网下载并入库。返回总条数。

    **增量**：过去的赛季已经踢完，football-data 的 CSV 不会再变 —— 已经入库的
    直接跳过，只有最后一个赛季（当前赛季，还在进行中）每次都重下。
    `force=True` 全部重下（赛季配置变了、或怀疑数据有缺口时用）。

    为什么要这个：一次全量是 `赛季数 × 联赛数` 个请求（默认 6×9 = 54），
    每次点一下都从头来一遍没有意义。
    """
    import traceback
    total = 0
    current = seasons[-1] if seasons else None
    for season in seasons:
        for div in divisions:
            if not force and season != current and _season_loaded(conn, season, div):
                if not silent:
                    print(f"{div} {season}: 已入库，跳过")
                continue
            try:
                text = download(season, div)
                tmp = Path("data") / f"_fd_{div}_{season}.csv"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(text, encoding="utf-8-sig")
                total += collect_from_csv(conn, tmp, div, season, silent=silent)
            except Exception as e:
                if not silent:
                    print(f"{div} {season}: 跳过 → {e.__class__.__name__}: {e}")
                    if not isinstance(e, RuntimeError):
                        # 打印完整堆栈（仅非下载错误时；下载错误太常见会刷屏）
                        traceback.print_exc()
    return total
