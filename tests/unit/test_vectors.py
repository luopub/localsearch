r"""`F5-04` 的 HNSW 层（`index/vectors_faiss.py`）与语义检索的编排（`search/semantic.py`）。

关联验收项 **A3**（检索端到端 p95 < 1.5 s，5M 切片）——**本文件不测那个数字**：这里只钉
正确性。性能要在真索引上实测，原始输出落 `docs/reports/`。

本文件钉的是**静默错误**——它们不抛异常，输出看起来完全正常：

* 索引文件写到一半就被当成"没有" → 原来的向量无声无息地没了，重建后看起来一切正常；
* `open(fresh=True)` 没清干净 → 旧向量与新向量混在同一个 HNSW 里；
* `faiss` 用 `-1` 补位，`-1` 流到调用方 → 那条命中凭空消失，不报错；
* 同一批里 label 重复 → 一个 label 挂两个向量，取回来的是哪一条看 faiss 内部顺序；
* 换了模型却没拦 → 两版 768 维单位向量混在一个索引里，搜索照常返回结果；
* 计数器落后于索引 → 下一趟重发 label（`IndexIDMap2` 允许同一个 id 出现两次）；
* 语义检索查询空串 / 空索引时仍然去嵌入、去查 → 白花钱，或把"没建"说成"没搜到"；
* `asked` 没封顶在 `ntotal` → 请求比向量还多，faiss 的补位就漏进结果。

向量用 `pool_fakes.vector_for()` 生成（同一个文本跨进程恒等），再手动归一化——它本身不是
单位向量，而 `VectorIndex.add()` 会拒绝非单位行（`check_unit_norm`）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from localsearch.embed.base import EmbedError
from localsearch.index.sqlite_store import ChunkText
from localsearch.index.vectors_faiss import (
    INDEX_FILENAME,
    META_EMBED_VER,
    META_NEXT_LABEL,
    VectorIncompatible,
    VectorIndex,
    VectorIndexError,
    VectorIndexMissing,
    VectorSpec,
    bind_embed_ver,
    check_consistency,
    next_label,
)
from localsearch.search import semantic
from pool_fakes import vector_for

DIM = 32


def unit(text: str, dim: int = DIM) -> np.ndarray:
    """`text` → 一个确定的单位向量。"""
    vec = vector_for(text, dim)
    return (vec / np.linalg.norm(vec)).astype(np.float32)


def batch(prefix: str, n: int, dim: int = DIM) -> np.ndarray:
    return np.stack([unit(f"{prefix}-{i}", dim) for i in range(n)])


def spec(quantize: str = "int8", **overrides: object) -> VectorSpec:
    """一个小到随便建的索引：维数 32、M 8。`efConstruction` / `efSearch` 也压小。"""
    values: dict[str, object] = {"dim": DIM, "m": 8, "ef_construction": 40, "ef_search": 40, "quantize": quantize}
    values.update(overrides)
    return VectorSpec(**values)  # type: ignore[arg-type]


class MetaStore:
    """只有 `get_meta` / `set_meta` 的桩：`bind_embed_ver()` 与 `next_label()` 只要这两个。"""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


class CountCache:
    """只有 `count()` 的桩：`check_consistency()` 只要这一个。"""

    def __init__(self, n: int) -> None:
        self.n = n

    def count(self) -> int:
        return self.n


# ==============================================================================
# 1. 建与查：HNSW 找得回自己
# ==============================================================================


@pytest.mark.parametrize("quantize", ["int8", "none"])
def test_each_vector_finds_itself_first(tmp_path: Path, quantize: str) -> None:
    """建一个小索引，每条向量拿自己去查，**第一名必须是它自己的 label**。

    这是 HNSW 最起码的正确性：`IndexIDMap2` 的 label 映射、`add_with_ids` 与 `search` 的
    返回顺序任何一处错位，这条就会先红。int8 的自我分数可以略超 1（`E43` 在项目参数下
    最大自查分数是 1.00074，见 `docs/PROGRESS.md` 的 E43 行；早先写过的 1.0039 没有复现），
    所以这里只断言排名，不断言分数。
    """
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec(quantize))
    vectors = batch("doc", 50)
    labels = list(range(100, 150))
    assert index.add(vectors, labels) == 50
    for vec, label in zip(vectors, labels, strict=True):
        hits = index.search(vec, 5)
        assert hits and hits[0].label == label, (label, hits)


def test_unquantized_self_score_is_one(tmp_path: Path) -> None:
    """不量化时，自己查自己的分数就是内积 = 余弦 = 1（容差只为 float32 的舍入）。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    vectors = batch("doc", 10)
    index.add(vectors, list(range(10)))
    top = index.search(vectors[3], 1)[0]
    assert top.label == 3
    assert abs(top.score - 1.0) < 1e-4, top


def test_int8_keeps_most_of_the_true_top_ten(tmp_path: Path) -> None:
    """int8 量化之后，前十名与**暴力精确**的前十名要大部分重合。

    精确答案用 numpy 点积算（不经过 HNSW，不经过量化）。阈值 0.8 是**这个小集合上的合理
    下限**，不是从生产数据测出来的数字——真实的召回要在 `E43` 的报告里实测，这里只防"量化
    把索引整个搞坏了"这一类大错。
    """
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("int8"))
    vectors = batch("doc", 300)
    index.add(vectors, list(range(300)))
    overlap = 0
    queries = batch("query", 20)
    for query in queries:
        exact = set(np.argsort(-(vectors @ query))[:10].tolist())
        got = {hit.label for hit in index.search(query, 10)}
        overlap += len(exact & got)
    assert overlap / (10 * len(queries)) >= 0.8


def test_search_drops_faiss_padding_instead_of_leaking_minus_one(tmp_path: Path) -> None:
    """索引里只有 3 条、要 10 条：faiss 用 `-1` 补位，**那些补位必须滤掉**。

    `-1` 流到调用方会被当成一个 label 去库里查，`chunks` 里没有 `vec_label = -1` 的行，
    于是那条命中凭空消失——不报错，只是少了一条。
    """
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 3), [7, 8, 9])
    hits = index.search(unit("doc-0"), 10)
    assert len(hits) == 3
    assert all(hit.label >= 0 for hit in hits)
    assert {hit.label for hit in hits} == {7, 8, 9}


def test_search_rejects_a_query_of_the_wrong_dimension(tmp_path: Path) -> None:
    """查询向量维度不对：当场抛，不让 faiss 在 C++ 侧 assert。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 2), [0, 1])
    with pytest.raises(VectorIndexError, match="维"):
        index.search(np.ones(DIM + 1, dtype=np.float32), 1)


# ==============================================================================
# 2. 写入的四道闸
# ==============================================================================


def test_add_refuses_a_batch_whose_labels_do_not_line_up(tmp_path: Path) -> None:
    """条数不等 → 当场抛。错位之后每条切片都会挂上别人的向量。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    with pytest.raises(VectorIndexError, match="条数"):
        index.add(batch("doc", 3), [0, 1])
    assert index.ntotal == 0, "被拒绝的那一批一条都不能进去"


def test_add_refuses_duplicate_labels_inside_one_batch(tmp_path: Path) -> None:
    """同一批里 label 重复 → 当场抛。否则一个 label 挂两个向量，取回来看 faiss 内部顺序。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    with pytest.raises(VectorIndexError, match="重复"):
        index.add(batch("doc", 2), [5, 5])
    assert index.ntotal == 0


def test_add_refuses_the_wrong_dimension(tmp_path: Path) -> None:
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    too_long = np.stack([unit("a", DIM + 1)])
    with pytest.raises(VectorIndexError, match="维"):
        index.add(too_long, [0])


def test_add_refuses_a_non_unit_vector_with_the_embed_error(tmp_path: Path) -> None:
    """非单位向量：内积不再等于余弦，分数从此是另一个量纲。抛的是 `EmbedError`（判据在
    `embed/base.py`，这里不抄第二份）。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    not_unit = batch("doc", 1) * 2.0
    with pytest.raises(EmbedError, match="单位长度"):
        index.add(not_unit, [0])


# ==============================================================================
# 3. 落盘：原子、可读回、损坏不当成"没有"
# ==============================================================================


def test_save_leaves_no_tmp_file_and_loads_back_the_same(tmp_path: Path) -> None:
    """`save()` 先写 `*.tmp` 再 `os.replace()`：成功之后不该留下 `.tmp`，读回的条数与标签不变。"""
    path = tmp_path / INDEX_FILENAME
    index = VectorIndex.open(path, spec("none"))
    index.add(batch("doc", 6), list(range(6)))
    index.save()
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists(), "原子落盘后不该残留临时文件"
    index.close()

    reloaded = VectorIndex.load(path, spec("none"))
    assert reloaded.ntotal == 6
    assert set(reloaded.ids().tolist()) == set(range(6))


def test_load_refuses_a_missing_index_and_creates_nothing(tmp_path: Path) -> None:
    """查询那一侧只用 `load()`：文件不在就抛 `VectorIndexMissing`，**不许**顺手建一个空的。

    建一个空的会把"还没建"变成"建了、里面什么都没有"（`D10`），下一次索引那一趟就以为
    索引已经在了。
    """
    path = tmp_path / INDEX_FILENAME
    with pytest.raises(VectorIndexMissing):
        VectorIndex.load(path, spec("none"))
    assert not path.exists()


def test_a_damaged_file_is_an_error_and_is_not_replaced(tmp_path: Path) -> None:
    """文件存在但读不出来（写到一半的截断）：**抛**，并且**原文件一个字节都不动**。

    当成"没有"就会在损坏的文件上重新开始，原来那几百万个向量无声无息地没了。
    """
    path = tmp_path / INDEX_FILENAME
    garbage = b"not a faiss index, truncated mid-write"
    path.write_bytes(garbage)
    with pytest.raises(VectorIndexError, match="读不出来"):
        VectorIndex.open(path, spec("none"))
    assert path.read_bytes() == garbage


def test_load_refuses_an_index_built_with_a_different_dim_or_quantize(tmp_path: Path) -> None:
    """文件的维度或量化与配置对不上 → `VectorIncompatible`。两边都是 float 向量，不抛就会
    按错的维度去切，结果是乱码而不是报错。"""
    path = tmp_path / INDEX_FILENAME
    index = VectorIndex.open(path, spec("none"))
    index.add(batch("doc", 2), [0, 1])
    index.save()
    index.close()
    with pytest.raises(VectorIncompatible):
        VectorIndex.load(path, spec("none", dim=DIM * 2))
    with pytest.raises(VectorIncompatible):
        VectorIndex.load(path, spec("int8"))


def test_fresh_open_drops_the_old_index_and_starts_empty(tmp_path: Path) -> None:
    """`open(fresh=True)`：旧文件删掉，从空开始。`--rebuild` 靠它；少了它旧向量会和新向量混在一起。"""
    path = tmp_path / INDEX_FILENAME
    index = VectorIndex.open(path, spec("none"))
    index.add(batch("doc", 4), list(range(4)))
    index.save()
    index.close()

    fresh = VectorIndex.open(path, spec("none"), fresh=True)
    assert fresh.ntotal == 0


# ==============================================================================
# 4. label 不重号：崩溃窗口
# ==============================================================================


def test_next_label_is_never_behind_what_the_index_already_holds(tmp_path: Path) -> None:
    """索引已经落盘到 label 9，而计数器（meta）还停在 3（提交前崩溃）：下一个号必须是 10。

    若只信计数器，下一趟会**重发** 3..9，`IndexIDMap2` 允许同一个 id 出现两次——一个 label
    从此对应两个向量，不报错。
    """
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 10), list(range(10)))
    store = MetaStore()
    store.set_meta(META_NEXT_LABEL, "3")
    assert index.next_label_hint() == 10
    assert next_label(store, hint=index.next_label_hint()) == 10


def test_next_label_takes_the_bigger_of_the_counter_and_the_hint() -> None:
    """计数器比 hint 大（发出去的号被删过）时取计数器；反之取 hint。两者取大，永不重号。"""
    store = MetaStore()
    assert next_label(store) == 0, "什么都没记过就是 0"
    store.set_meta(META_NEXT_LABEL, "50")
    assert next_label(store, hint=10) == 50
    assert next_label(store, hint=80) == 80


def test_next_label_hint_of_an_empty_index_is_zero(tmp_path: Path) -> None:
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    assert index.next_label_hint() == 0


# ==============================================================================
# 5. 一致性：孤儿可以，缺向量不行
# ==============================================================================


def test_extra_vectors_in_the_index_are_orphans_and_are_counted(tmp_path: Path) -> None:
    """索引比缓存多：多出来的是**孤儿**（先存索引、后提交 SQL 的刻意窗口）。无害，返回条数。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 5), list(range(5)))
    assert check_consistency(index, CountCache(3)) == 2


def test_a_cache_ahead_of_the_index_refuses_at_once(tmp_path: Path) -> None:
    """缓存比索引多：**当场抛**。那批切片会永远拿不到向量（"命中了就不再嵌"），而且每次查询都少几条，
    不报错——这是唯一要命的方向。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 2), [0, 1])
    with pytest.raises(VectorIndexError, match="缓存比向量索引还多"):
        check_consistency(index, CountCache(5))


# ==============================================================================
# 6. 模型绑定：换了模型就拦住
# ==============================================================================


def test_a_fresh_store_and_an_empty_index_bind_to_the_version(tmp_path: Path) -> None:
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    store = MetaStore()
    assert bind_embed_ver(store, index, embed_ver="v1") is None
    assert store.get_meta(META_EMBED_VER) == "v1"


def test_the_same_version_again_is_a_no_op(tmp_path: Path) -> None:
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    store = MetaStore()
    store.set_meta(META_EMBED_VER, "v1")
    assert bind_embed_ver(store, index, embed_ver="v1") is None


def test_a_different_version_is_refused_not_silently_mixed(tmp_path: Path) -> None:
    """库里记着 v1、现在要用 v2：**拒绝**。旧向量不会自己消失，不拦住就两版混在同一个 HNSW 里，
    搜索照常返回结果——只是排序说不清。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 2), [0, 1])
    store = MetaStore()
    store.set_meta(META_EMBED_VER, "v1")
    with pytest.raises(VectorIncompatible, match="v1"):
        bind_embed_ver(store, index, embed_ver="v2")


def test_a_populated_index_with_no_recorded_version_is_refused(tmp_path: Path) -> None:
    """有向量却说不清是哪一版模型建的：**不猜**，抛。猜错就是两套语义混着。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 2), [0, 1])
    with pytest.raises(VectorIncompatible, match="没有记"):
        bind_embed_ver(MetaStore(), index, embed_ver="v1")


def test_an_empty_version_string_is_refused_outright(tmp_path: Path) -> None:
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    with pytest.raises(ValueError):
        bind_embed_ver(MetaStore(), index, embed_ver="")


# ==============================================================================
# 7. 语义检索的编排（search/semantic.py）
# ==============================================================================


class FakeEmbedder:
    """`encode_queries` 才是语义检索这一层会碰的方法。记下调用次数，证明"空查询不嵌"。"""

    def __init__(self) -> None:
        self.calls = 0

    def encode_queries(self, texts):
        self.calls += 1
        return np.stack([unit(t) for t in texts])


class SpyIndex:
    """包一层 `VectorIndex`，数 `search()` 被调了几次（`semantic.search` 的契约是**只查一次**）。"""

    def __init__(self, inner: VectorIndex) -> None:
        self.inner = inner
        self.searches = 0

    @property
    def ntotal(self) -> int:
        return self.inner.ntotal

    def search(self, vector, k):
        self.searches += 1
        return self.inner.search(vector, k)


class FakeStore:
    """`chunks_by_labels` 与 `paths_for_blob` 两个方法；`known` 之外的 label 视为库里没有（孤儿）。"""

    def __init__(self, known: dict[int, ChunkText], paths: dict[int, list[str]]) -> None:
        self.known = known
        self.paths = paths

    def chunks_by_labels(self, labels):
        return {label: self.known[label] for label in labels if label in self.known}

    def paths_for_blob(self, blob_id):
        return list(self.paths.get(blob_id, []))


def _chunk(label: int, blob_id: int) -> ChunkText:
    return ChunkText(chunk_id=label, blob_id=blob_id, ord=0, text=f"正文 {label}")


def test_a_blank_query_is_never_embedded_or_searched(tmp_path: Path) -> None:
    """空串、纯空白：`blank=True`，**不嵌、不查**。嵌一个空串是白花钱，还会得到一个没有意义的向量。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 3), [0, 1, 2])
    spy = SpyIndex(index)
    embedder = FakeEmbedder()
    result = semantic.search("   ", store=FakeStore({}, {}), index=spy, embedder=embedder)
    assert result.blank is True and result.hits == ()
    assert embedder.calls == 0 and spy.searches == 0


def test_an_empty_index_answers_nothing_and_does_not_query(tmp_path: Path) -> None:
    """空索引：`asked == 0`，**不查**。与"查了、没搜到"是两回事（前者要 `lsearch index --tier 2`）。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    spy = SpyIndex(index)
    result = semantic.search("任何词", store=FakeStore({}, {}), index=spy, embedder=FakeEmbedder())
    assert result.asked == 0 and result.hits == () and result.blank is False
    assert spy.searches == 0


def test_asked_is_twice_the_limit_capped_at_what_the_index_holds(tmp_path: Path) -> None:
    """`asked = min(limit * 2, ntotal)`：过取两倍补掉孤儿，但**不能超过索引里有的条数**。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    labels = list(range(30))
    index.add(batch("doc", 30), labels)
    store = FakeStore({lbl: _chunk(lbl, lbl) for lbl in labels}, {lbl: [f"p{lbl}.txt"] for lbl in labels})

    spy = SpyIndex(index)
    result = semantic.search("查", store=store, index=spy, embedder=FakeEmbedder(), limit=5)
    assert result.asked == 10 and spy.searches == 1

    spy = SpyIndex(index)
    result = semantic.search("查", store=store, index=spy, embedder=FakeEmbedder(), limit=20)
    assert result.asked == 30, "两倍是 40，但索引只有 30 条，不能问得比它多"
    assert len(result.hits) == 20


def test_orphan_labels_are_dropped_and_counted_not_silently_lost(tmp_path: Path) -> None:
    """命中的 label 在库里找不到（孤儿）：**丢掉并计数**。不说的话少掉的那几条去哪了没有任何解释。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    labels = list(range(6))
    index.add(batch("doc", 6), labels)
    known = {lbl: _chunk(lbl, lbl) for lbl in (0, 1, 2)}  # 3..5 在库里没有
    store = FakeStore(known, {lbl: [f"p{lbl}.txt"] for lbl in known})
    result = semantic.search("查", store=store, index=SpyIndex(index), embedder=FakeEmbedder(), limit=6)
    assert {hit.label for hit in result.hits} == {0, 1, 2}
    assert result.dropped == 3


def test_a_chunk_with_no_path_still_comes_back_with_empty_paths(tmp_path: Path) -> None:
    """库里有切片、却没有路径指着它：仍然返回这一条，`paths` 为空——由渲染层去说"没有路径"，
    这一层不替它补一个假路径。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    index.add(batch("doc", 1), [0])
    store = FakeStore({0: _chunk(0, 99)}, {})
    result = semantic.search("查", store=store, index=SpyIndex(index), embedder=FakeEmbedder(), limit=1)
    assert len(result.hits) == 1 and result.hits[0].paths == []


def test_a_non_positive_limit_is_refused_loudly(tmp_path: Path) -> None:
    """`limit <= 0` 当场抛：返回空列表会让它看起来像"没搜到"。"""
    index = VectorIndex.open(tmp_path / INDEX_FILENAME, spec("none"))
    with pytest.raises(ValueError):
        semantic.search("查", store=FakeStore({}, {}), index=SpyIndex(index), embedder=FakeEmbedder(), limit=0)
