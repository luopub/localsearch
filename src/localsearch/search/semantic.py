r"""HNSW 查询那一格：查询 → 向量 → 近似最近邻 → 切片 → 路径（`F5-04`）。测试落点
`tests/unit/test_vectors.py`。

这一层回答一个问题：**"意思相近"的那些片段在哪儿。** 它只有一条路，四步：

1. `embedder.encode_queries([query])`——加**查询前缀**（与文档前缀非对称，`M3`）；
2. `index.search(vector, k)`——HNSW 近似最近邻，返回 `(label, 内积)`；
3. `store.chunks_by_labels(labels)`——label 是 `chunks.vec_label`，取回那一片原文；
4. `store.paths_for_blob(blob_id)`——这一步才回答"是哪些文件"。

第 3 与第 4 步是**两个问题**，不能合并：`D12` 的去重让同一个 blob 被好几个路径
指着，所以"命中哪一片"（库里一行切片）与"命中哪些文件"（可能好几行 `files`）
本来就不同。少了第 4 步，用户会以为"只搜到一个文件"，而那些副本在库里是明确
记着的（`paths_for_blob()` 的文档写了同一件事）。

------------------------------------------------------------------------------
分数是内积，**不是** BM25 那种东西，而且 int8 下会略大于 1
------------------------------------------------------------------------------

`VectorHit.score` 是内积；库里的向量都是单位向量，所以它同时就是余弦相似度。
⚠ 两个不要：**不要与 BM25 的 `rank` 比大小**（量纲无关，融合是 `F5-05` 的 RRF），
**不要断言 `score <= 1`**（`quantize = "int8"` 下拿库里的向量查它自己返回的是
1.0039，`E43`——量化再反量化的溢出，不是错误）。

------------------------------------------------------------------------------
孤儿向量：少几条命中要**报出来**，不是悄悄少
------------------------------------------------------------------------------

`index.search()` 返回的 label 有两种在库里查不到：跨过程崩溃留下的**孤儿**（索引里
有、库里没有——`VectorIndex.save()` 那个刻意的顺序），以及被 `--prune` 删掉的切片。
两种都不是错误，处理方式也一样：**丢掉这一条**（`chunks_by_labels()` 的文档写着
"调用方要做的正是丢掉，而不是把整个查询判成失败"）。但"丢掉"要**计数**：
`SemanticSearch.dropped` 就是它。不计数的话，一次本该给 20 条结果的查询给了 17 条，
而终端上没有任何东西解释那 3 条去哪了。

所以这里**多要一点**（`_OVERFETCH`）：孤儿正常时一条都没有，而"上一批加进去的向量
全成孤儿"就是最坏的情形——那种量级下 ×2 足够把结果补满，且 HNSW 的额外代价在
`efSearch` 面前可以忽略。

------------------------------------------------------------------------------
空查询：**一次都不嵌**
------------------------------------------------------------------------------

`query.strip()` 为空时直接返回空结果，不去嵌入。理由不是省一次嵌入，而是**空串
在模型里是一个完全合法的输入**——它会得到一个向量，然后 HNSW 会老老实实返回
最接近它的 20 条。于是一个"什么都没敲"的查询看起来像一次**成功**的检索：20 条结果、
分数正常、路径正常。这与词法那一路"没有可搜的 token 就不查"（`tokens.and_expr()`
返回 `None`）是同一条判据，只是原因不同：那边是 `MATCH ''` 会语法错，这边是
空串有个**看起来合理**的向量。
"""

from __future__ import annotations

from dataclasses import dataclass

from localsearch.embed.base import Embedder
from localsearch.index.sqlite_store import ChunkText, SqliteStore
from localsearch.index.vectors_faiss import VectorIndex

__all__ = [
    "SemanticHit",
    "SemanticSearch",
    "search",
]

#: 向 HNSW 多要几条（理由见模块文档最后一段）。⚠ 它是个**工程判断**，不是读数：
#: 正常时孤儿为零（那就是 ×1 也够），而"上一批全成孤儿"的量级是
#: `perf.commit_every_files`（缺省 500）——×2 补不满那种情形，但那种情形下
#: `dropped` 会把话说出来，而结果**少几条**远好过让查询去走一遍大半个图。
_OVERFETCH = 2


@dataclass(frozen=True, slots=True)
class SemanticHit:
    """一条语义命中：哪一片、多像、属于哪些文件。

    `paths` 是**元组**而不是单个路径：`D12` 的同一个 blob 可以被好几个路径指着
    （`paths_for_blob()` 的文档），而"只印第一个路径"会把"这份内容有 4 个副本"
    这件事整个藏起来。⚠ 它**可以是空的**：blob 在库里、但没有任何 `files` 行指着它
    （那是 `--prune` 之后可能出现的一种残留）。空的时候要照常印出来（"这份内容
    现在没有对应的文件"是一条真话），而不是把这条命中悄悄丢掉。

    `score` 的口径见模块文档第二节。
    """

    label: int
    score: float
    chunk: ChunkText
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticSearch:
    """一次语义查询的结论。**与 `ChunkSearch` 同形**（`query` + `hits`），少两样、多两样。

    少的是 `tokens` / `expr`：那是词法那一路的账（查询串被切成什么、`MATCH` 表达式
    长什么样），语义这一路一次都不查 FTS。

    多的是两个**只有这一路才有**的数：

    * `blank`——查询串里一个非空白字符都没有，于是**一次都没嵌、一次都没查**。
      它与词法那一路的 `has_query=False` 是同一个位置上的东西（"根本没查"与
      "查了、没搜到"必须分得开），只是判据不同。
    * `dropped`——HNSW 给了 label、库里查不到那一片的条数（孤儿 / 被删的切片）。
      它**不为零就意味着 `hits` 比要的少**，而终端上不印这个数就没有任何东西能解释
      那几条去哪了。
    """

    query: str
    hits: tuple[SemanticHit, ...] = ()
    blank: bool = False
    dropped: int = 0
    #: `k` 实际要了几条（= `min(limit * _OVERFETCH, index.ntotal)`）。它是这一趟
    #: 那笔多要的账，测试拿它钉 ×2 这件事，报表不用它。
    asked: int = 0


def search(
    query: str,
    *,
    store: SqliteStore,
    index: VectorIndex,
    embedder: Embedder,
    limit: int = 20,
) -> SemanticSearch:
    r"""按**意思**检索：查询 → 向量 → HNSW → 切片 → 路径。

    四个参数都是**造好的**：这一层不读配置、不开库、不加载索引——"用哪个 embedding
    模型、索引在哪儿、要不要降级"全是调用方的事（`cli/commands/search.py`）。这样
    它的每一步都能在没有模型、没有索引文件的进程里被测（塞一个假嵌入器与一个真
    HNSW 小索引就够）。

    `limit` 是**要几条命中**（不是要几条向量）：多要的那部分由 `_OVERFETCH` 定，
    补齐之后截到 `limit`。`limit <= 0` 当场抛——一个"要 0 条"的查询只能是调用方
    算错了，而返回空列表会让它看起来像"没搜到"。
    """
    if limit <= 0:
        raise ValueError(f"limit 必须是正数，拿到 {limit}")
    raw = query or ""
    if not raw.strip():
        return SemanticSearch(query=raw, blank=True)
    vector = embedder.encode_queries([raw])
    asked = min(limit * _OVERFETCH, index.ntotal)
    if asked <= 0:
        # 索引是空的：**不查**。`search()` 在空索引上返回的是空列表，看起来与
        # "查了、没搜到"一样，而两者的正确处理完全不同（前者要 `lsearch index
        # --tier 2`，后者是"换个说法"）。与空查询同一条判据。
        return SemanticSearch(query=raw)
    # ⚠ **查一次**，两处都用这一份：`index.search()` 是这一趟最贵的一步（走 HNSW 图），
    # 而调两次不只是浪费——两次之间没有任何保证给出同一批 label。
    candidates = index.search(vector, asked)
    found = store.chunks_by_labels([candidate.label for candidate in candidates])
    hits: list[SemanticHit] = []
    dropped = 0
    for hit in candidates:
        if len(hits) >= limit:
            break
        chunk = found.get(hit.label)
        if chunk is None:
            dropped += 1
            continue
        hits.append(
            SemanticHit(
                label=hit.label,
                score=hit.score,
                chunk=chunk,
                paths=store.paths_for_blob(chunk.blob_id),
            )
        )
    return SemanticSearch(
        query=raw, hits=tuple(hits), dropped=dropped, asked=asked
    )
