"""方案展开：legs → 单式清单 + 打印输出（实现 T23）。"""
from __future__ import annotations

from itertools import product


def expand(legs: list[list[str]]) -> list[str]:
    """将每场选择集展开为单式号码行。"""
    return ["".join(c) for c in product(*legs) if all(c)]


def to_text(rows: list[str], meta: dict | None = None) -> str:
    """生成可打印文本清单。"""
    header_lines = [
        "=== 足彩单式清单 ===",
    ]
    if meta:
        header_lines.append(
            f"期号 {meta.get('period_no','')}  玩法 {meta.get('game_type','')}  "
            f"目标 {meta.get('objective','')}  预算 ¥{meta.get('budget','')}"
        )
    n = len(rows)
    header_lines.append(f"合计 {n} 注  金额 ¥{n * 2}")
    header_lines.append("-" * 30)
    body = "\n".join(rows)
    return "\n".join(header_lines) + "\n" + body + "\n"
