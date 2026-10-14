"""`F2-04` 的内容去重与引用计数：`ingest/dedup.py`。

关联验收项 **A9**（同一份内容出现在多个路径时，只提取与嵌入一次）。

本文件里绝大多数测试在钉**静默错误**：计数偏大（内容永远清不掉）、计数偏小（内容在
还有人引用时被删）、以及"旧引用丢在半路"（只涨不跌的库）。这三种都不会抛异常、
不会有日志，只会在几个月后表现为"搜不到"或"磁盘不降"——所以每一条都有一个具体的
失败场景，而不是"调用一下看返回值"。
"""

from __future__ import annotations

import pytest

from localsearch.ingest.dedup import BlobRefs, Put, Removal

#: 三个内容哈希。用带前缀的规范形（`fingerprint.content_hash()` 的产物形状）——
#: 本模块不解释它们，只当不透明字符串用，但用真形状能顺带防止"前缀可有可无"扩散。
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
H3 = "sha256:" + "3" * 64


# ==============================================================================
# 1. A9：同一份内容只处理一次
# ==============================================================================


def test_the_first_path_of_a_content_says_go():
    refs = BlobRefs()
    put = refs.put("d:/a.txt", H1)
    assert isinstance(put, Put)
    assert put.content_is_new is True, "第一份内容要提取、要嵌入"
    assert put.replaced is None
    assert put.changed is True


def test_a_second_path_with_the_same_content_says_stop():
    """**这一条就是 `A9`。** 同一份内容出现在两个路径上，第二个不该再提取一次。"""
    refs = BlobRefs()
    refs.put("d:/a.txt", H1)
    put = refs.put("d:/b.txt", H1)
    assert put.content_is_new is False, "同一份内容，第二次不该再处理"
    assert put.replaced is None
    assert put.changed is True, "但对 b.txt 这条路径来说它是新记录"
    assert refs.refs_of(H1) == 2
    assert refs.duplicate_groups()[H1] == ["d:/a.txt", "d:/b.txt"]


def test_the_three_fields_of_Put_answer_three_different_questions():
    """⚠ **钉住三个字段不可互相推**——被压掉的那一维只在"副本"这一格显形。

    这一格是 `put()` 的全部四种可能里的第三种：**新路径 + 老内容**（第二个副本）。
    第一版 `Put` 只有 `replaced` 与 `content_is_new`，它在这一格上两个都为假，
    于是"账变了吗"被算成假——**而账确实变了**（多了一条引用）。

    代价具体长什么样：库那一层若拿 `content_is_new` 当"要不要写一条 files 行"用，
    第二个副本**根本不会被记下来**——文件在盘上、内容早就有了、索引里却没有它。
    """
    refs = BlobRefs()

    # 第一种：新路径 + 新内容
    first = refs.put("d:/a.txt", H1)
    assert (first.is_new_key, first.replaced, first.content_is_new) == (True, None, True)
    assert first.changed is True

    # 第二种：老路径 + 老内容（重扫）
    same = refs.put("d:/a.txt", H1)
    assert (same.is_new_key, same.replaced, same.content_is_new) == (False, None, False)
    assert same.changed is False

    # 第三种：**新路径 + 老内容**（第二个副本）——第一版判错的就是这一格
    copy = refs.put("d:/b.txt", H1)
    assert (copy.is_new_key, copy.replaced, copy.content_is_new) == (True, None, False)
    assert copy.changed is True, "账变了（多了一条引用），虽然内容一点都不新"

    # 第四种：老路径 + 新内容（`is_new_key` 是假——这个键早就在账上了）
    edited = refs.put("d:/a.txt", H2)
    assert (edited.is_new_key, edited.replaced is not None, edited.content_is_new) == (False, True, True)
    assert edited.replaced.content_hash == H1
    assert edited.changed is True


def test_a_second_copy_is_recorded_even_though_the_content_is_not_new():
    """上一条的后果版：两个副本都要在账上，`duplicate_groups` 才找得到它们。"""
    refs = BlobRefs()
    assert refs.put("d:/a.txt", H1).content_is_new is True
    assert refs.put("d:/b.txt", H1).content_is_new is False
    assert refs.keys == 2, "第二个副本也要记账，否则索引里就少了这个文件"
    assert refs.duplicate_groups()[H1] == ["d:/a.txt", "d:/b.txt"]


def test_three_copies_of_one_content_are_all_found():
    """`D12` 白赚的那条能力：按内容分组即"找出所有重复文件"。"""
    refs = BlobRefs()
    for key in ("d:/x/1.txt", "d:/y/2.txt", "d:/z/3.txt"):
        refs.put(key, H1)
    refs.put("d:/only.txt", H2)
    groups = refs.duplicate_groups()
    assert list(groups) == [H1]
    assert len(groups[H1]) == 3
    assert refs.refs_of(H2) == 1


def test_duplicate_groups_can_be_asked_for_more_than_two():
    refs = BlobRefs()
    refs.put("a", H1)
    refs.put("b", H1)
    refs.put("c", H2)
    refs.put("d", H2)
    refs.put("e", H2)
    assert set(refs.duplicate_groups(min_refs=3)) == {H2}


# ==============================================================================
# 2. 幂等：重扫一遍不能把计数翻倍
# ==============================================================================


def test_putting_the_same_pair_again_changes_nothing():
    """⚠ **静默错误第 1 格**：计数一翻倍，`remove()` 就永远减不到零。

    失败场景：P6 的增量扫描每轮把没变过的文件也 `put()` 一遍。如果这里记账了，
    每一轮每一条引用 +1，`refs` 只涨不跌——**内容永远不会被清理**，而 `--prune`
    跑起来看着是成功的。本库会在跑几个月后才有人注意到磁盘没降。
    """
    refs = BlobRefs()
    first = refs.put("d:/a.txt", H1)
    for _ in range(5):
        again = refs.put("d:/a.txt", H1)
        assert again.replaced is None
        assert again.content_is_new is False
        assert again.changed is False
    assert refs.refs_of(H1) == 1, "重扫 5 遍，引用数还是 1"
    assert refs.keys == 1
    assert refs.total_refs == 1
    assert first.content_is_new is True


def test_reputting_a_shared_content_does_not_double_count_either():
    refs = BlobRefs()
    refs.put("a", H1)
    refs.put("b", H1)
    for _ in range(3):
        refs.put("a", H1)
        refs.put("b", H1)
    assert refs.refs_of(H1) == 2
    assert refs.total_refs == refs.keys == 2


# ==============================================================================
# 3. 内容变了：旧的那一笔不能丢
# ==============================================================================


def test_a_content_change_hands_back_the_old_reference():
    """⚠ **静默错误第 2 格**：只加不减 → 旧内容永远减不到零 → 泄漏。

    失败场景：文件被改过。旧内容的引用如果没被减掉，它的切片、FTS 行、向量就留
    一辈子；`refs_of(旧)` 永远 ≥ 1，于是"没人要了"这个判断永远为假。
    """
    refs = BlobRefs()
    refs.put("d:/a.txt", H1)
    put = refs.put("d:/a.txt", H2)
    assert put.replaced is not None, "旧引用必须交还，别丢"
    assert put.replaced.content_hash == H1
    assert put.replaced.last_reference is True, "它那一份内容没人要了 → 可以去清"
    assert put.replaced.refs_left == 0
    assert put.content_is_new is True
    assert refs.refs_of(H1) == 0, "旧内容必须从账上消失"
    assert refs.refs_of(H2) == 1
    assert refs.hash_of("d:/a.txt") == H2


def test_a_content_change_that_leaves_the_old_content_shared_does_not_free_it():
    """旧内容还有别人指着时，`last_reference` 必须是假——否则会删掉别人在用的东西。"""
    refs = BlobRefs()
    refs.put("a", H1)
    refs.put("b", H1)                 # 两份路径共用 H1
    put = refs.put("a", H2)           # a 改了内容
    assert put.replaced is not None
    assert put.replaced.content_hash == H1
    assert put.replaced.last_reference is False, "b 还指着 H1，不许清"
    assert put.replaced.refs_left == 1
    assert refs.refs_of(H1) == 1
    assert refs.hash_of("b") == H1


def test_changing_back_and_forth_keeps_the_books_straight():
    """来回改几轮之后，账目仍然与"当前盘上是什么"一致（幂等 + 搬移的组合）。"""
    refs = BlobRefs()
    for h in (H1, H2, H3, H1, H2):
        refs.put("a", h)
    assert refs.hash_of("a") == H2
    assert refs.keys == 1
    assert refs.blobs == 1
    assert refs.total_refs == 1
    assert [refs.refs_of(h) for h in (H1, H2, H3)] == [0, 1, 0]


# ==============================================================================
# 4. 删：不认识的不许当成功
# ==============================================================================


def test_removing_an_unknown_key_says_so():
    """⚠ **静默错误第 3 格**：把"没删到"当成功会掩盖计数偏小的 bug。

    失败场景：一条引用被删了两次（或者键拼错了）。返回"成功"的话，计数再减一次就会
    让**还有人在用的内容被判定为没人要**——于是内容被删掉，而某个文件还指着它。
    """
    refs = BlobRefs()
    assert refs.remove("d:/never-seen.txt") is None


def test_removing_the_last_reference_frees_the_content():
    refs = BlobRefs()
    refs.put("a", H1)
    got = refs.remove("a")
    assert isinstance(got, Removal)
    assert got.content_hash == H1
    assert got.last_reference is True
    assert got.refs_left == 0
    assert refs.refs_of(H1) == 0
    assert refs.blobs == 0, "归零的 blob 要从账上删掉，不留 0"
    assert refs.keys == 0
    assert refs.duplicate_groups() == {}
    assert refs.remove("a") is None, "再删一次就是不认识了"


def test_removing_one_of_two_references_keeps_the_content():
    refs = BlobRefs()
    refs.put("a", H1)
    refs.put("b", H1)
    got = refs.remove("a")
    assert got.last_reference is False
    assert got.refs_left == 1
    assert refs.refs_of(H1) == 1, "内容还在，别清"
    assert refs.hash_of("b") == H1
    assert refs.duplicate_groups() == {}


def test_a_blob_that_was_freed_can_be_added_again_as_new():
    """删干净之后再出现同一份内容，必须当成**新的**（要重新提取、重新嵌入）。

    失败场景：`_refs` 里留了一个 `0`，于是"这份内容见过"为真 → 第二次真的要用它时
    没人去提取，索引里就缺了这一份正文。
    """
    refs = BlobRefs()
    refs.put("a", H1)
    refs.remove("a")
    assert refs.blobs == 0
    assert refs.put("c", H1).content_is_new is True


# ==============================================================================
# 5. 不变式与边界
# ==============================================================================


def test_total_refs_always_equals_keys():
    """两个数摆在一起看，任何"计数偏了"的 bug 都会让它们对不上。

    这是全文件唯一一条覆盖多种操作序列的不变式测试，所以序列排得杂一点：新增、重复、
    共享、改内容、删共享的一条、删最后一条。
    """
    refs = BlobRefs()
    steps = [
        ("a", H1), ("b", H1), ("c", H2), ("a", H1), ("a", H3),
        ("d", H2), ("b", H3), ("e", H1),
    ]
    for key, h in steps:
        refs.put(key, h)
        assert refs.total_refs == refs.keys, f"记账对不上，发生在 {key}->{h}"
    refs.remove("a")
    assert refs.total_refs == refs.keys == 4
    refs.remove("e")
    assert refs.total_refs == refs.keys == 3
    assert refs.blobs == 2, "H2 有 c/d，H3 有 b"


def test_the_registry_does_not_normalize_paths_itself():
    """本类**不做**路径规范化——那是 `filters.normalize()` 的活（唯一真相来源）。

    这条测试故意把"误用"的结果摆出来：两个只差大小写的键被记成**两条引用**。
    失败场景正是模块文档说的那个入口：NTFS 大小写不敏感，`C:/Windows` 与
    `c:/windows` 是同一个文件；用带大小写的路径当键，这份内容就"多"一个引用，
    **永远归不了零**。调用方必须传 `ScanEntry.key`（规范形）。

    本类选择"不顺手规范化"而不是"顺手规范化"：顺手做会让项目里出现**第二个**
    路径规范化的实现，而两者早晚会不一致——那时"同一个文件"的判定就有两个答案了。
    """
    refs = BlobRefs()
    refs.put("C:/Windows/a.txt", H1)
    refs.put("c:/windows/a.txt", H1)
    assert refs.keys == 2
    assert refs.refs_of(H1) == 2, "同一个文件被记成两条 → 这就是那个入口，别这么用"
    assert len(refs.duplicate_groups()[H1]) == 2


def test_an_empty_registry_answers_everything_consistently():
    refs = BlobRefs()
    assert refs.keys == 0
    assert refs.blobs == 0
    assert refs.total_refs == 0
    assert refs.refs_of(H1) == 0
    assert refs.hash_of("a") is None
    assert refs.duplicate_groups() == {}
    assert refs.remove("a") is None


def test_the_removal_dataclass_is_frozen():
    """`Removal` / `Put` 是不可变的：它们是交给调用方的**凭据**，改动它们没有意义。

    失败场景：调用方拿着 `last_reference` 判断"可以清内容了"，中途有人把它改成假
    （或者反过来），于是要么泄漏、要么误删。冻结让这种改动直接抛 `TypeError`。
    """
    refs = BlobRefs()
    refs.put("a", H1)
    got = refs.remove("a")
    with pytest.raises(Exception):
        got.last_reference = False       # type: ignore[misc]
    put = BlobRefs()
    with pytest.raises(Exception):
        put.put("a", H1).content_is_new = False   # type: ignore[misc]
