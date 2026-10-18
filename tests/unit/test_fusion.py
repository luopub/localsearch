r"""`F5-05` 的 RRF 融合（`search/fusion.py`）。

本文件钉的是**静默错误**：融合函数从不抛异常，排序看起来合理，只是不对。

* 同一路内重复出现的键被**多算了一次** → 它压过了别人；
* 两路都命中的键**没有累加** → 两路都说好的结果反而排不到只说了一路的后面；
* 同分的次序**不稳定** → 同一个查询两次次序不同，没法复现、没法评测（A4）；
* 空的一路**吃掉了另一路** → 语义那一路没有结果时，融合必须与词法逐条相同。
"""

from __future__ import annotations

import pytest

from localsearch.search.fusion import RRF_K, rrf_merge


def test_the_constant_is_the_published_sixty() -> None:
    """`k = 60` 是计划 §4.2 与 D2 写死的文献取值。改它必须先回 A4 重测，所以它被钉住。"""
    assert RRF_K == 60


def test_a_single_list_keeps_its_order_and_scores_by_rank() -> None:
    """只有一路：融合结果就是那一路本身，分数是 `1 / (k + 名次)`，名次从 1 开始。"""
    merged = rrf_merge([["a", "b", "c"]])
    assert [key for key, _ in merged] == ["a", "b", "c"]
    assert merged[0][1] == pytest.approx(1 / 61)
    assert merged[1][1] == pytest.approx(1 / 62)
    assert merged[2][1] == pytest.approx(1 / 63)


def test_a_key_in_both_lists_accumulates_its_two_contributions() -> None:
    """两路都命中的键，分数是两份贡献之和。这是融合存在的全部理由。"""
    merged = dict(rrf_merge([["a", "b"], ["b", "c"]]))
    # b 在第一路第 2 名、第二路第 1 名：1/62 + 1/61
    assert merged["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert merged["a"] == pytest.approx(1 / 61)
    assert merged["c"] == pytest.approx(1 / 62)


def test_a_key_found_by_both_lists_beats_one_found_by_only_one() -> None:
    """两路都说好的，排在只说了一路的前面——即便后者在它那一路里名次更靠前。"""
    merged = rrf_merge([["only-lexical", "both"], ["both", "only-semantic"]])
    assert merged[0][0] == "both"


def test_a_duplicate_inside_one_list_counts_only_its_first_rank() -> None:
    """同一路内重复出现：只算第一次的名次。否则它的分数被那一路加了两遍，压过别人。

    `x` 在第一路出现两次（第 1 名、第 3 名），只该得 `1/61`；`y` 只在第一路第 2 名，得 `1/62`。
    """
    merged = dict(rrf_merge([["x", "y", "x"]]))
    assert merged["x"] == pytest.approx(1 / 61)
    assert merged["y"] == pytest.approx(1 / 62)


def test_a_duplicate_is_not_double_counted_across_lists_either() -> None:
    """同一个键在两路各出现一次是正常累加；在一路里出现两次只算一次。两者要分得开。"""
    merged = dict(rrf_merge([["x", "x"], ["x"]]))
    assert merged["x"] == pytest.approx(1 / 61 + 1 / 61)


def test_ties_keep_first_occurrence_order_so_runs_are_reproducible() -> None:
    """同分按键**首次出现**的位置定先后（先列表序、再表内序）。

    `a` 与 `b` 得分完全相同（各在一路第一名、另一路第二名），`a` 先出现，于是 `a` 在前。
    同一输入两次调用的结果逐项相同——没有这一条，A4 的评测就没法复跑。
    """
    first = rrf_merge([["a", "b"], ["b", "a"]])
    second = rrf_merge([["a", "b"], ["b", "a"]])
    assert first == second
    assert [key for key, _ in first] == ["a", "b"]


def test_an_empty_list_passes_the_other_through_unchanged() -> None:
    """语义那一路没有结果（索引空、查询空）：融合结果必须与词法那一路**逐条相同**，而不是变空。"""
    lexical = ["a", "b", "c"]
    merged = rrf_merge([lexical, []])
    assert [key for key, _ in merged] == lexical
    assert merged == rrf_merge([lexical])


def test_all_lists_empty_gives_nothing() -> None:
    assert rrf_merge([[], []]) == []
    assert rrf_merge([]) == []


def test_the_result_is_sorted_by_score_descending() -> None:
    merged = rrf_merge([["a", "b", "c", "d"], ["d", "c", "b", "a"]])
    scores = [score for _, score in merged]
    assert scores == sorted(scores, reverse=True)


def test_a_non_positive_k_is_refused_loudly() -> None:
    """`k <= 0` 当场抛：`k < 0` 时分母会是 0 或负数，排序就是错的，却还像一次正常的融合。"""
    with pytest.raises(ValueError, match="正整数"):
        rrf_merge([["a"]], k=0)
    with pytest.raises(ValueError, match="正整数"):
        rrf_merge([["a"]], k=-5)


def test_k_changes_the_weight_of_the_top_rank() -> None:
    """`k` 越小，第一名相对第二名的优势越大（`k=1` 时 `1/2` 对 `1/3`）。
    这一条钉的是 `k` 确实进了公式，而不是一个被忽略的参数。"""
    small = dict(rrf_merge([["a", "b"]], k=1))
    large = dict(rrf_merge([["a", "b"]], k=60))
    assert small["a"] / small["b"] > large["a"] / large["b"]
