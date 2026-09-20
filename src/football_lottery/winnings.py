"""对奖计算（设计 §附录 / 实现 T24 / T30）。

对奖公式（适应任意 n 场方案，比赛级回测用 n=1~14）：
  14 场一/二等奖（合并口径）：covered[k] = result[k] in legs[k]
    - 全 14 场 covered → 一等 1 注，二等 = Σ(|S_i| - 1)
    - 13 场 covered（差 1 场）→ 一等 0，二等 = |S_miss|
    - ≤12 场 covered → 都 0
  任九：全 9 场 covered → 1 注，否则 0

通配场次（`*`）：比赛推迟或中断、且自开赛起 48 小时内未补赛时，官方开奖
结果以 `*` 占位，该场按 3/1/0 全选计算 —— 即对**所有**投注都算猜中。
故 `*` 一律视为 covered，不参与"差一场"的失配判定。
"""
from __future__ import annotations

WILDCARD = "*"


def _covered(result_i: str, leg: list[str]) -> bool:
    """该场是否命中；`*`（推迟未补赛）对任意选择都算命中。"""
    return result_i == WILDCARD or result_i in leg


def hit_counts(legs: list[list[str]], result: list[str]) -> tuple[int, int]:
    """(一等奖注数, 二等奖注数)。

    默认按 14 场口径计算（合并口径：全对时同时持有二等注）。
    非 14 场也按同公式退化计算。
    """
    n = len(legs)
    if n != len(result):
        return 0, 0
    covered = [_covered(result[i], legs[i]) for i in range(n)]
    k = sum(covered)
    if k == n:
        return 1, sum(len(legs[i]) - 1 for i in range(n))
    if n >= 2 and k == n - 1:
        miss = covered.index(False)
        return 0, len(legs[miss])
    return 0, 0


def r9_hit(legs: list[list[str]], result: list[str]) -> int:
    """任九只有全对一档：9 场全覆盖才 1 注。"""
    if len(legs) != 9 or len(result) != 9:
        return 0
    return 1 if all(_covered(result[i], legs[i]) for i in range(9)) else 0


def amount(hit_notes: int, single_prize: float | None) -> float:
    return hit_notes * (single_prize or 0.0)
