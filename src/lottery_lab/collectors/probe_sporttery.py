"""体彩 webapi 探测：候选接口 + 真实响应落盘（一次性使用）。

参考实现计划 T5 / 设计文档 §3.1。每个 URL 请求一次，状态码与响应片段打印，
完整 body 写到 `data/probe/probe_{idx}.json`。

实际可用的 URL 与参数组合需要看响应内容判断，然后把"能用的"提到
`sporttery.py` 顶部常量并注释含义。
"""
from __future__ import annotations

import json
import pathlib
import sys

import httpx


CANDIDATES = [
    # 足彩（胜负彩/任九）当期对阵
    "https://webapi.sporttery.cn/gateway/lottery/getMatchListV1.qry?param=90,0&isVerify=1",
    "https://webapi.sporttery.cn/gateway/lottery/getMatchListV1.qry?param=1,0&isVerify=1",
    # 历史开奖（胜负彩 / 任九）
    "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=30&isVerify=1&pageNo=1",
    "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=85&provinceId=0&pageSize=30&isVerify=1&pageNo=1",
    # 有些版本的胜负彩 gameNo 是 90 / 任九是 85
    "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=90&provinceId=0&pageSize=10&isVerify=1&pageNo=2",
]


def main() -> int:
    out = pathlib.Path("data/probe")
    out.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(CANDIDATES):
        try:
            r = httpx.get(
                url,
                timeout=20,
                headers={"Referer": "https://www.sporttery.cn/"},
            )
            snippet = r.text[:200].replace("\n", " ")
            path = out / f"probe_{i}.json"
            path.write_text(r.text, encoding="utf-8")
            print(f"[{i}] {r.status_code} ({len(r.text)}b) → {path.name}: {snippet}")
        except Exception as e:
            print(f"[{i}] ERROR {e.__class__.__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
