r"""`F5-05` 的 RRF 融合（计划 §4.2 `FusionStrategy` 的 v1 实现）。

输入是若干条**各自已经排好序**的结果表（词法一条、语义一条，将来还可能多一条），输出是
一条合并后的排序。公式：`score(d) = Σ 1 / (k + rank_i(d))`，`rank` 从 1 开始，`k = 60`。

为什么是 RRF、而不是把两路分数加权：

* 词法的 bm25（越小越好）与语义的内积（越大越好）**不是一个量纲**，直接相加没有意义，
  加权又要靠一个对语料敏感的系数；
* RRF 只看**名次**，两路的分数尺度完全不进来，所以不需要任何标定。

本模块钉的是**静默错误**——它们不抛异常，排序看起来合理，只是不对：

* 同一份结果在**一路内**出现两次 → 它的分数被那一路加了两遍，压过了别人。只计它第一次出现
  的名次；
* 一个键在两路里都出现时**没有累加** → 两路都命中的结果反而排不到只命中一路的前面，融合
  就失去了意义；
* 同分时顺序**不确定** → 同一个查询两次结果的次序不同，没法复现、没法评测（A4）。同分按
  键第一次出现的位置（先词法后语义）定序；
* 空的一路**把别的路吃掉** → 语义那一路没东西（索引空、查询空）时，融合结果必须与另一路
  逐条相同，而不是变成空。

这个模块**只管名次**，不知道什么是切片、什么是路径。键是调用方给的任意可哈希值（这里用
`chunk_id`），返回的是 `(键, 融合分)` 的元组，按分从高到低。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import TypeVar

__all__ = [
    "RRF_K",
    "rrf_merge",
]

K = TypeVar("K", bound=Hashable)

#: RRF 的平滑常数。计划 §4.2 与 D2 写死 60（Cormack 等人 2009 年的原始取值）。它是**文献
#: 取值**，不是本项目调出来的：改它要先回到 A4 的 20 条评测上重测，不能拍脑袋。
RRF_K = 60


def rrf_merge(result_lists: Sequence[Sequence[K]], *, k: int = RRF_K) -> list[tuple[K, float]]:
    """把若干条已排序的结果表融合成一条。

    参数 `result_lists` 的每一条都是**从好到坏**排好的键序列（第 0 个是第一名）。空表允许，
    表示那一路没有结果。

    返回 `[(键, 融合分), ...]`，按融合分从高到低；同分按键**首次出现**的位置（列表序、再
    表内序）定先后，所以同一输入两次调用结果逐项相同。

    ⚠ `k` 必须是正整数：`k = 0` 时平滑项消失，第一名独占 `1`，名次靠前的优势被放大到
    失真；`k < 0` 时分母可能是 0 或负数，排序就乱了。这里当场抛，而不是让那种输出看起来
    像一次正常的融合。
    """
    if k <= 0:
        raise ValueError(f"RRF 的 k 必须是正整数，拿到 {k}")
    scores: dict[K, float] = {}
    for results in result_lists:
        seen: set[K] = set()
        for position, key in enumerate(results):
            if key in seen:
                # 同一路内重复出现：只算第一次的名次（见模块文档）。
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position + 1)
    # `dict` 保持插入顺序，所以 `sorted` 的稳定性把"首次出现"的先后留在了同分的位置上。
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
