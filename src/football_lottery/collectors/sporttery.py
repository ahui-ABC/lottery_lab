"""体彩 webapi 适配器 + 历史期次 fixture 导入。

- 体彩 webapi（设计 §3.1）当前对未签到会话返回 `E0001/P0001`；live 路径作 best-effort。
- 历史期次对阵（方案层回测 P1 验收的硬前置）走 `cli import-fixtures --file <csv>`，
  CSV 模板：`period_no, seq, home_cn, away_cn, match_time(可选), result(可选),
  first_prize(单注,可选), second_prize(可选), r9_prize(可选)`。
  第一行可选（只在第一条出现）填期号级别的奖金。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sqlite3 as _sql
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from football_lottery.db import store


# ---- live API（best-effort） -------------------------------------------------------
LIVE_URLS = {
    "current_sfc14": "https://webapi.sporttery.cn/gateway/lottery/getMatchListV1.qry?param=90,0&isVerify=1",
    "draws_sfc14": "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=30&isVerify=1&pageNo=1",
    "draws_r9": "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=85&provinceId=0&pageSize=30&isVerify=1&pageNo=1",
}


def fetch_live(url_key: str, timeout: int = 15) -> dict | None:
    """尝试请求 live URL；返回 JSON dict。遇到反爬 / 错误时返回 None。"""
    import httpx
    url = LIVE_URLS.get(url_key)
    if not url:
        return None
    try:
        r = httpx.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible)",
                "Referer": "https://www.sporttery.cn/",
            },
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception:
        return None
    # 若 API 返回业务级错误码（如 E0001 待确认 / P0001 参数非法），当作不可用
    if body.get("errorCode") not in (None, "0", "0000", ""):
        return None
    return body


# ---- fixture CSV 导入（兜底主路径） ------------------------------------------------
def import_fixtures_csv(
    conn: sqlite3.Connection,
    csv_path: str | Path,
) -> dict:
    """导入历史期次对阵；返回 {period_no: 汇总计数}。

    CSV 列（首行表头必须存在）：
      period_no, seq, home_cn, away_cn, match_time,
      result,           # 该场开奖结果（可选, 单字符 3/1/0）
      draw_14,          # 整期 14 场结果，逗号分隔（只在 seq=1 行写）
      first_prize,      # 单注一等奖奖金（只写一次）
      second_prize,     # 二等奖单注
      r9_prize,         # 任九单注
      draw_date         # 开奖日 (YYYY-MM-DD)

    期号级（开奖结果 + 奖金）只在 seq=1 行写，其它行留空。
    """
    out: dict[str, dict[str, int]] = {}
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        pn = (row.get("period_no") or "").strip()
        if not pn:
            continue
        seq_raw = (row.get("seq") or "").strip()
        if not seq_raw.isdigit():
            continue
        seq = int(seq_raw)
        if not (1 <= seq <= 14):
            continue
        home = (row.get("home_cn") or "").strip()
        away = (row.get("away_cn") or "").strip()
        if not home or not away:
            continue
        if pn not in out:
            out[pn] = {"period": 0, "matches": 0, "draw": 0}
        if out[pn]["period"] == 0:
            store.upsert(conn, "periods", {
                "period_no": pn,
                "draw_date": (row.get("draw_date") or "").strip() or None,
                "sale_end": None,
                "status": "historical",
            }, ["period_no"])
            out[pn]["period"] = 1
        period_id = conn.execute(
            "SELECT id FROM periods WHERE period_no=?", (pn,)
        ).fetchone()["id"]
        store.upsert(conn, "period_matches", {
            "period_id": period_id,
            "seq": seq,
            "home_name_cn": home,
            "away_name_cn": away,
            "match_time": (row.get("match_time") or "").strip() or None,
            "match_id": None,
            "odds_json": None,
        }, ["period_id", "seq"])
        out[pn]["matches"] += 1
        # 开奖结果（14 场总和）只在 seq=1 这一行携带，整期写一次 draw_results
        if seq == 1:
            d14 = (row.get("draw_14") or "").strip()
            results = [s.strip() for s in d14.split(",") if s.strip()]
            if len(results) == 14 and all(r in {"0", "1", "3"} for r in results):
                prizes: dict[str, float] = {}
                for k_in, k_out in (("first_prize", "first"),
                                    ("second_prize", "second"),
                                    ("r9_prize", "r9")):
                    v = (row.get(k_in) or "").strip()
                    if v:
                        prizes[k_out] = float(v)
                store.upsert(conn, "draw_results", {
                    "period_id": period_id,
                    "results_json": d14,
                    "prizes_json": json.dumps(prizes) if prizes else None,
                }, ["period_id"])
                out[pn]["draw"] = 1
    conn.commit()
    return out
