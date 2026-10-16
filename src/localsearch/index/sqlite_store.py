r"""索引库的读写面（P4 / `F4-01` 与 `F4-02`）。测试落点 `tests/unit/test_store_files_fts.py`
（Tier0 那一层）与 `tests/unit/test_store_body_fts.py`（正文那一层）。

四句话概括这一层：

1. **建库/开库都走 `SqliteStore.open()`**，它会配 PRAGMA、按 `schema.sql` 建表、
   核对 `user_version`。调用方不需要知道建表语句长什么样。
2. **表结构只有一处真相来源**：同目录的 `schema.sql`（它自己就是计划 §4.4 的落地形态，
   两处刻意的差异写在那个文件的注释里）。本文件里**不抄任何 DDL**。
3. **切法只有一处真相来源**：`tokens.py`。本文件只决定"哪张表要不要 2-gram"，
   而那个决定是一个带读数的常量（见 `FILES_BIGRAMS` / `CHUNK_BIGRAMS`）。
4. **一次写入不提交**（`put_file()` / `put_blob()` / `put_chunks()` /
   `set_file_extracted()`）。事务边界属于管线，见下面那一段。
5. **`status` 与 `tier` 的词义不在这里**（`F4-03`）：取值域、哪一趟挑哪些行、
   "就绪"怎么算，全在 `index/tier.py`。本模块从那里导入词表，只负责**执行**
   （`set_file_extracted()` 的校验、`iter_tier_work()` 的选择条件、`tier_counts()`
   的盘点）。见下面「状态机在 `tier.py`」那一段。

------------------------------------------------------------------------------
写入**不提交**——提交是调用方的事
------------------------------------------------------------------------------

`F4-04` 要求"可中断续跑"，所以提交的粒度属于**管线**（`build.py` 每
`PerfCfg.commit_every_files` 个文件提交一次），不属于单行写入。所以：

* 想让写入生效，调用方得自己 `commit()`——**那个门现在就开着**（`F4-04` 加的公开
  方法，不再是"去够 `_conn`"）；
* 想在同一个事务里攒一批，连着调几个 `put_*` 就是——Python 的 `sqlite3` 默认在第一条
  DML 上开事务、`commit()` 才结束，所以两次写入之间不提交就是同一个事务；
* `close()` **会**兜底提交（见它的文档串），但不要依赖它：那只是防"最后一个批次
  悄悄丢了"。

⚠ 这一条也是**崩溃后不留半截状态**的全部依据：一个文件的三处写入（`files` 那一行的
更新、`blobs`、`chunks` + `chunks_fts`）落在同一个事务里，崩在中间就是一起回滚，
续跑时会重新做一遍。

------------------------------------------------------------------------------
正文那一层（`F4-02`）：blob 按内容去重，切片挂在 blob 上
------------------------------------------------------------------------------

`put_blob()` / `put_chunks()` 是 `D12` 的落点：**一份内容只提取、分块、嵌入一次**，
多个路径共用同一行 `blobs`，切片挂在 blob 上而不是文件上（`chunks.blob_id`）。
`files.blob_id` 指回它，于是"同一份内容出现在几个路径上"在库里是**看得到**的
（`paths_for_blob()`）——同一个 blob 命中一次正文，就能说出它全部的路径。

`put_blob()` 的三种结局调用方要能分辨（`BlobWrite.action`），尤其是 `replaced`：
内容一样但 `parser_ver` 变了，意味着**旧解析结果必须被丢掉**。计划 §4.4.1 那条
"缓存键必须带上版本"说的就是它，而静默复用旧切片的症状是"改过提取器之后，某些文件的
正文永远搜不到"——不报错，只是少。

**引用计数不存列**：`blobs` 上没有 `refcount`，"一个 blob 被几条 `files` 指着"由
`SELECT count(*) FROM files WHERE blob_id = ?` 现算（计划 §4.4.1 的原话，`idx_files_blob`
就是为它建的索引）。理由与 `ingest/dedup.py` 里那三条"会静默出错的格子"同源：**一个
只会涨的计数器比没有计数器更坏**——它不报错，只会让内容永远清不掉。现算的代价是一次
索引查找，而它没有第二条真相。

⚠ `ingest/dedup.py` 的模块文档里曾有一句"计划 §4.3 的 `blobs` 表要存 `refcount`"，
那是 `F2-04` 落地时照着计划 §4.3 写的，而 §4.4 与 `schema.sql` 用的都是现算——
`F4-02` 把那句话改正了（同一件事不能有两处真相）。

------------------------------------------------------------------------------
状态机在 `tier.py`，本模块只执行它
------------------------------------------------------------------------------

`F4-01` / `F4-02` 那两轮里，`STATUSES` 与 `MAX_TIER` 一度定义在本文件——那时它们只是
`set_file_extracted()` 的入参校验。`F4-03` 把它们搬去 `index/tier.py`，因为同一个词表
同时要说四件事：取值域、**哪一趟会挑到哪一行**（`work_condition()`）、**"就绪"怎么算**
（`TierLedger`）、以及 `status` 与 `tier` 之间的那条单向约束。四件事分在两地，症状是
"校验通过、但那一趟永远挑不到它"——不报错，只是那一级永远不就绪。

本模块因此多了三个方法，它们都只是**一条 SQL + 一次委托**：

* `tier_counts()`——`GROUP BY status, tier` 点一次数，喂给 `ledger_from()` 算台账；
* `iter_tier_work(tier)`——按 `work_condition()` 挑出这一趟要处理的文件行；
* `set_file_extracted()` 里那条新的校验——`status` 声明的级别不许超过 `tier` 记的
  （`STATUS_MIN_TIER` / `STATUS_MAX_TIER`）。

⚠ `STATUSES` / `MAX_TIER` 仍然是 `sqlite_store` 命名空间里的名字（导入进来的），
但它们**不在它的 `__all__` 里**：让它们从这一层的公开面上消失，是为了不在下一个
功能点里被"顺手"重新定义一遍。

------------------------------------------------------------------------------
`files.path` 的形态由本模块定，且只有一个函数说了算
------------------------------------------------------------------------------

见 `store_path()`：**反斜杠换正斜杠，大小写原样留着**。NTFS 的路径比较不分大小写，
所以 `files.path` 上挂的是 `COLLATE NOCASE`（`schema.sql` 里那处刻意的差异）——
两条只差大小写的路径会撞成一行，而**存下来的那一行是先到的那个写法**。

------------------------------------------------------------------------------
嵌入缓存那一层（`F5-03`）：本模块只供四条 SQL，**键怎么算不在这里**
------------------------------------------------------------------------------

`embed_cache` 那四条（`find_vectors()` / `put_vectors()` / `count_vectors()` /
`delete_vectors()`）与 `F4-03` 的切法同源：**策略在 `index/vector_cache.py`，"执行"
在这里**。所以本模块只管 `(text_hash, embed_ver) → vec_label` 这三列的读写，
**不碰"text_hash 是怎么算出来的"**——那是 `vector_cache.text_hash()` 与
`BatchEmbedder.document_input()` 的事，一处实现、一处理由。

⚠ 这四条的 `embed_ver` **全是必填/无默认**的（`count_vectors()` 那个 `None` 只用于
"总数"这一个报表用途）。理由是该表的主键是**两列**：只按 `text_hash` 查或删，
"换了模型之后复用旧向量"与"清缓存顺手把别的版本也清了"就都成了**不报错**的操作。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from localsearch.core.models import Chunk
from localsearch.index.tier import (
    MAX_TIER,
    STATUS_INDEXED,
    STATUS_MAX_TIER,
    STATUS_MIN_TIER,
    STATUSES,
    TIER_SEMANTIC,
    work_condition,
)
from localsearch.index.tokens import and_expr, query_tokens, tokenize
from localsearch.ingest.base import drive_of

__all__ = [
    "BLOB_CREATED",
    "BLOB_REPLACED",
    "BLOB_REUSED",
    "CHUNK_BIGRAMS",
    "FILES_BIGRAMS",
    "NAME_WEIGHT",
    "PATH_WEIGHT",
    "SCHEMA_VERSION",
    "BlobWrite",
    "ChunkHit",
    "ChunkSearch",
    "ChunkText",
    "VectorReset",
    "FileRow",
    "PathHit",
    "PathSearch",
    "SchemaVersionError",
    "SqliteStore",
    "StoreError",
    "store_path",
]

#: `PRAGMA user_version` 的值。改 `schema.sql` 里任何一张表的形状都要 +1——
#: 这一条不是形式主义：库里躺着的旧索引与代码里的新 schema 对不上时，症状通常是
#: "某几列一直是 NULL"或者"某条查询永远返回 0 条"，两者都不会报错。
SCHEMA_VERSION = 1

#: 建表语句的来源。用 `importlib.resources` 而不是 `Path(__file__).with_name()`：
#: 后者在 zipimport 下拿不到文件，而这是**包数据**（`schema.sql` 必须打进 wheel，
#: `F7-03` 的打包测试要盯住它）。
SCHEMA_SQL = files("localsearch.index").joinpath("schema.sql").read_text(encoding="utf-8")

# ==============================================================================
# 两张 FTS 表各自的切法（`E38` 的读数决定，不是默认值）
# ==============================================================================

#: `files_fts`（名字 + 路径）**要** 2-gram。代价 1.99× token，买到的是文件名碎片查询
#: 的微召回 75.3% → 98.8%。名字短、总量小，而 `A8` 要的正是"按文件名搜到"。
FILES_BIGRAMS = True

#: `chunks_fts`（正文）**不要** 2-gram。正文的 token 会涨到 2.76×，而收益**没实测过**
#: （`E38` 量的是文件名碎片，正文检索打的是词，`cut_for_search` 已经够）。
#: 等 P5 有了真实的正文查询再回头量一遍。
CHUNK_BIGRAMS = False

#: bm25 的列权重：命中**文件名**比命中它所在的**目录**更有意义（搜 `财务报表` 时，
#: 一个叫"财务报表"的文件应该排在"财务报表/附件/1.txt"前面）。
#: ⚠ 这两个数是**暂定**的：真正的排序策略归 P5 的 `search/`，这里只保证"名字优先"
#: 这件事成立。它们是 bm25 的光滑倍数，换掉不影响召回。
NAME_WEIGHT = 4.0
PATH_WEIGHT = 1.0

#: `files.status` / `files.tier` 的词义与取值域**定义在 `index/tier.py`**（`F4-03`），
#: 本模块从那里导入 `STATUSES` / `MAX_TIER` / `STATUS_MIN_TIER` / `STATUS_MAX_TIER`。
#: 这里只**执行**那条约束、不定义它：`status` 与 `tier` 互相矛盾的行会让某一级
#: "永远不算就绪"而没有任何一趟会挑到它——**写错一个字母不会报错**，所以
#: `set_file_extracted()` 的校验与选择条件必须是同一份词表。

#: `put_blob()` 的三种结局（`BlobWrite.action`）。
#:
#: `replaced` 必须能被调用方看见：它是"**这一份内容的旧解析结果已经被丢掉**"，
#: 在一轮全盘索引里应该是个位数（除非刚换过提取器），而它悄悄发生就等于旧切片
#: 在没人知道的情况下消失了。
BLOB_CREATED = "created"
BLOB_REUSED = "reused"
BLOB_REPLACED = "replaced"

_BLOB_ACTIONS = frozenset({BLOB_CREATED, BLOB_REUSED, BLOB_REPLACED})

#: `files.status` 里**唯一**允许（并且必须）带 `error_msg` 的那个。`skipped`
#: （这一类 v1 不做）故意不算：全盘上 `.db` / `.dat` / `.exe` 多得是，把"这一类不做"
#: 也填上原因，失败报表就淹了（`extract/batch.py` 的模块文档定了这条分工）。
_STATUS_WITH_ERROR = "error"

#: 一条 `SELECT` 里最多塞多少个 `?`（`F5-03` 的 `find_vectors()` 用）。
#:
#: 上限是编译期的 `SQLITE_MAX_VARIABLE_NUMBER`，超了直接抛 `OperationalError`——
#: 而"抛在哪一批"取决于批次大小，也就是"取决于内存"。**这个值是随 SQLite 的编译
#: 选项变的**：老版本默认 **999**，本机 sqlite 3.50.4 实测是 **32766**（`E42`：
#: 32766 个 `?` 通过、32767 抛 `too many SQL variables`）。所以这里不能拿"本机的
#: 读数"当常量用，也不能贴着 999 写——取 900，两个数都安全，代价只是每 900 条多一条
#: 查询。（本方法每一条 `SELECT` 还各多带一个 `embed_ver` 参数，留余量也是为它。）
_SQL_PARAM_LIMIT = 900


def _chunked(items: Sequence[object], size: int) -> Iterator[Sequence[object]]:
    """把一段序列切成每段最多 `size` 个。`IN (…)` 那几处都靠它。

    ⚠ 收的是 `Sequence[object]`（而不是 `Sequence[str]`）：`F5-04` 起 `IN (…)` 的
    参数不只有文本哈希，还有 `blob_id` / `vec_label` 这类整数。
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


class StoreError(Exception):
    """索引库这一层能说清楚的失败。"""


class SchemaVersionError(StoreError):
    """库里的 `user_version` 与 `SCHEMA_VERSION` 对不上。"""


def _now(now: int | None) -> int:
    """这一笔写入的时刻（Unix 秒）。`now` 传了就照用它。

    让它成为一个**参数**而不是直接调 `time.time()`，是为了测试能钉住写入的
    `created_at` / `indexed_at`——"这一列有没有被写上"与"写的是不是现在"
    是两件事，前者是这一层该保证的，后者是机器时钟的事。
    """
    return int(time.time()) if now is None else int(now)


def store_path(path: str | Path) -> str:
    r"""一条路径 → **存进 `files.path` 的形态**：反斜杠换正斜杠，大小写原样。

    换正斜杠是因为它要能被直接读、被 SQL 里的字符串比较、被 `LIKE` 用；而
    `D:\work\x` 里的反斜杠在 SQL 字面量与 Python 字面量里都要转义，是一层白给的
    麻烦。**大小写原样**是因为它还要被拿去打开文件、显示给用户（`NOCASE` 只管
    "唯不唯一"，不改存下来的样子）。

    ⚠ **不做** `posixpath.normpath`：那会顺手解掉 `..` 段并收掉重复斜杠，
    而"一条路径被规范化之后再入库"意味着库里的路径**可能不是文件真实的路径**。
    扫描器给的路径本来就是拼出来的（父 + 名字），不存在 `..` 与重复斜杠；
    真出现了一条奇怪的路径，我宁可它原样躺在库里，也不要它是被猜出来的。
    """
    return str(path).replace("\\", "/")


@dataclass(frozen=True, slots=True)
class FileRow:
    r"""`files` 表的一行。

    只放"扫描/提取阶段真的知道"的东西；`content_hash` / `blob_id` / `frn` / `usn`
    属于后面的功能点（F4-02 起），现在一律 `None`。

    `volume` 由 `path` **推出来**而不是让调用方填：两个字段能各说各话，就是两个
    真相来源（`ingest.base.drive_of` 的口径已经固定，这里跟着它）。
    """

    path: str
    size_bytes: int
    mtime_ns: int
    status: str
    tier: int = 0
    ext: str = ""  # 不带点的小写扩展名；无扩展名是空串，不是 NULL
    mime: str | None = None
    quick_sig: str | None = None  # ⚠ P4 一律 None，理由见 schema.sql
    content_hash: str | None = None
    blob_id: int | None = None
    frn: int | None = None
    usn: int | None = None
    indexed_at: int | None = None
    error_msg: str | None = None
    id: int | None = None
    #: 从 `path` 推出来的，不接受调用方传值（`init=False`）
    volume: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(
                f"status 只能是 {sorted(STATUSES)} 之一，收到 {self.status!r}"
            )
        if not 0 <= self.tier <= MAX_TIER:
            raise ValueError(f"tier 只能是 0..{MAX_TIER}，收到 {self.tier}")
        if self.size_bytes < 0 or self.mtime_ns < 0:
            raise ValueError("size_bytes / mtime_ns 不接受负数")
        object.__setattr__(self, "path", store_path(self.path))
        # ⚠ 小写：`ingest.base.drive_of()` **保留原样大小写**（`D:` 进就 `D:` 出），
        # 而两个扫描后端给的写法不一样——scandir 从 `roots` 里的 `D:/work` 出发是
        # `D:`，MFT 按卷枚举出来的是 `d:`。不统一的话 `count_files(volume="d:")`
        # 会静默漏掉另一半，而"盘点少了一半"看上去只是一个偏小的数字。
        object.__setattr__(self, "volume", drive_of(self.path).lower())

    @property
    def name(self) -> str:
        """文件名（含扩展名）。`files` 表不存它——它永远是 `path` 的最后一段。"""
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class PathHit:
    """`search_paths()` 的一条命中。`rank` 是 bm25 的原始值（**越小越相关**）。"""

    path: str
    status: str
    tier: int
    size_bytes: int
    mtime_ns: int
    ext: str
    error_msg: str | None
    rank: float

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class PathSearch:
    """`search_paths()` 的完整结果。

    这里**必须**把 `tokens` 带出来：查询串里没有可搜内容（用户敲了一串标点）与
    "搜到了 0 条"是两件事，前者的正确输出是"这个词没有可搜的内容"，后者是"没找到"。
    只返回 `[]` 的话，调用方分不出这两种，只能猜。
    """

    query: str
    tokens: tuple[str, ...]
    expr: str | None
    hits: tuple[PathHit, ...]

    @property
    def has_query(self) -> bool:
        """查询串里有没有可搜的 token。没有就**根本没查**，`hits` 必然是空的。"""
        return self.expr is not None


@dataclass(frozen=True, slots=True)
class BlobWrite:
    """`put_blob()` 的结果：这份内容落在哪一行，以及**这一趟要不要接着写切片**。

    `needs_chunks` 是调用方真正要问的问题，把它做成属性而不是让调用方去比字符串：
    `created` 与 `replaced` 都要写，只有 `reused` 不要——**"不要"这一格是全部**
    （复用时再写一遍切片，会在同一个 blob 下留两份一样的文本，搜出来是重复的，
    而 `put_chunks()` 会当场拦下来，见那里）。
    """

    blob_id: int
    action: str

    def __post_init__(self) -> None:
        if self.action not in _BLOB_ACTIONS:
            raise ValueError(
                f"action 只能是 {sorted(_BLOB_ACTIONS)} 之一，收到 {self.action!r}"
            )

    @property
    def needs_chunks(self) -> bool:
        """这一趟要不要调 `put_chunks()`。"""
        return self.action != BLOB_REUSED


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """`search_chunks()` 的一条命中。`rank` 是 bm25 的原始值（**越小越相关**）。

    `text` 是**原文**（`chunks.text`），不是 FTS 里那份分词后的文本——计划 §4.4.1
    把这条写成了义务："展示片段一律从 `chunks.text` 按 `chunk_id` 取回原文"。
    分词那份（`chunks_fts.text_tok`）只用来匹配，它是小写的、词之间带空格的。

    ⚠ **没有 `title`**：`title` 只存在于 `chunks_fts` 里（`chunks` 表没有这一列），
    而那一份是**分词后**的形态，拿去显示会是"年度报告 2024"这种小写、带空格的串。
    要显示文档标题得给它另找一个落点（见 P4「遗留」）。命中里能显示的是 `text`
    与路径（`paths_for_blob()`）。

    `blob_id` 是这一片挂在**哪份内容**上：同一份内容的多个路径共用它，所以
    "这个命中对应哪些文件"要问 `paths_for_blob()`。
    """

    chunk_id: int
    blob_id: int
    ord: int
    text: str
    char_start: int | None
    char_end: int | None
    n_tokens: int | None
    rank: float


@dataclass(frozen=True, slots=True)
class ChunkText:
    """一片切片的**原文**与它在库里的身份（`F5-04` 的两条路共用）。

    它比 `ChunkHit` 少两样、多一样：

    * **没有 `rank`**：`F5-04` 的两条路（建索引、语义查询）里，"相关度"要么还没算出来
      （建索引时它不存在），要么是 HNSW 给的内积（与 bm25 不是一个量纲，`VectorHit`
      的文档里写了）。塞进来只会逼出一句"这个 rank 是哪种"的判断。
    * **没有 `n_tokens` / `char_start`**：建索引那条路只用得上 `text`，"这段文本在原文
      的哪一段"是**高亮**（`F5-06`）的事——到那天再加列，比现在先猜一遍要留下的形状便宜。
    * **多了 `ord`**：同一份内容里切片的顺序。它是 `(blob_id, ord)` 那半个索引的含义，
      也是"这一批切片有没有漏掉中间一片"唯一能看出来的地方。
    """

    chunk_id: int
    blob_id: int
    ord: int
    text: str


@dataclass(frozen=True, slots=True)
class VectorReset:
    """`reset_vector_state()` 清掉了什么：缓存条数 / 切片行数 / 文件行数。

    三个数**分开报**是有用的：一次"重建"如果只清掉缓存（`chunks = 0`），说明切片上
    那些 label 还指着旧索引；只清掉切片（`cached = 0`），说明下一次会把整库重嵌一遍。
    两个数都该与"这一版索引里有多少条"同量级——差得远就是清错了范围。
    """

    cached: int
    chunks: int
    files: int


@dataclass(frozen=True, slots=True)
class ChunkSearch:
    """`search_chunks()` 的完整结果。**与 `PathSearch` 同形**，理由也一样：

    "查询串里没有可搜内容"（用户敲了一串标点）与"搜到了 0 条"是两件事，
    前者的正确输出是"这个词没有可搜的内容"。只回一个 `[]` 的话调用方分不出。

    ⚠ 还多一件事：**库里根本没有正文**（Tier1 没跑完）时，`has_query` 为真而
    `hits` 为空——那与"跑完了但没搜到"在数据上**无法区分**。分级就绪的标注
    （`D10`：`A8` 要的"显式标注语义未就绪"）不能靠这个结果对象自己说，得由
    调用方拿着 `count_files(tier=…)` 一类的账来判断（`F4-03` 的 tier 状态推进
    给的就是那本账）。
    """

    query: str
    tokens: tuple[str, ...]
    expr: str | None
    hits: tuple[ChunkHit, ...]

    @property
    def has_query(self) -> bool:
        """查询串里有没有可搜的 token。没有就**根本没查**，`hits` 必然是空的。"""
        return self.expr is not None


class SqliteStore:
    """索引库。**不是**线程安全的：一个连接给一个线程用（`F4-04` 的 worker 各自开库）。"""

    def __init__(self, conn: sqlite3.Connection, path: Path | None) -> None:
        self._conn = conn
        self.path = path

    # ------------------------------------------------------------------ 开与关

    @classmethod
    def open(cls, db_path: str | Path, *, create_parents: bool = True) -> SqliteStore:
        """打开（必要时建）索引库。

        `create_parents=True` 会建出父目录：数据目录 `D:/localsearch-data/index/` 在
        第一次索引之前并不存在，让调用方先 `mkdir` 是把这个知识散到各处。
        """
        target = Path(db_path)
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        store = cls(sqlite3.connect(str(target)), target)
        store._setup()
        return store

    @classmethod
    def in_memory(cls) -> SqliteStore:
        """只在内存里的库。测试用；也用于"试跑一遍不想留东西"的场合。

        ⚠ 它仍然会走完整的建表 + PRAGMA 流程，所以"建表语句有错"在单测里就会暴露，
        不会等到跑真库时才发现。
        """
        store = cls(sqlite3.connect(":memory:"), None)
        store._setup()
        return store

    def commit(self) -> None:
        r"""结束当前事务，让这一批写入落盘（`F4-04` 的分阶段提交）。

        **公开一个门口是刻意的**：本模块每一个 `put_*` / `set_*` 都不提交，而
        "提交的粒度属于管线"这条分工只有在**管线拿得到一个明确的提交动作**时才成立。
        少了它，调用方要么去摸 `store._conn`（私有），要么依赖 `close()` 的兜底提交
        ——后者只在进程正常退出时成立，而 `F4-04` 要的恰恰是"中途中断也只丢最后一批"。

        没有开事务时它是**空操作**（`sqlite3` 的 `commit()` 本来就是），所以调用方
        不必先判断"这一批有没有写过东西"——那种判断漏一次，症状是"某一批悄悄没提交"。

        ⚠ 提交的是**整个连接上未提交的写入**，不是"上一次 `put_*` 那一笔"。所以
        "一批 = 一次提交"要靠调用方**按批调用**它：`F4-04` 每 `commit_every_files`
        个文件调一次，中间不提交——一个文件的三处写入因此落在同一个事务里。
        """
        self._conn.commit()

    def rollback(self) -> None:
        r"""丢掉当前事务里**还没提交**的写入（与 `commit()` 成对）。

        给 `run_tier2()` 用：一批里的 `set_file_indexed()` 是在嵌入之前写的，嵌入一旦
        失败（模型不在本机、子进程崩了），这一批的结局就只能撤回——否则 `close()` 的
        兜底提交会把它们当成"做完了"落盘，而它们一条向量都没有。

        没有开事务时是**空操作**（`sqlite3` 的 `rollback()` 本来就是）。
        """
        self._conn.rollback()

    def close(self) -> None:
        """提交未提交的事务并关连接。

        ⚠ **先提交再关**：`sqlite3` 的关闭**不会**替你提交（未提交的事务按回滚处理），
        所以"最后一个批次悄悄丢了"是这里的默认行为。显式提交一次，代价是零。
        """
        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 建库

    def _setup(self) -> None:
        self._configure()
        self._require_fts5()
        self._ensure_schema()

    def _configure(self) -> None:
        """连接级的 PRAGMA。**不放进 `schema.sql`**：它们是连接的属性、不是库的属性。

        `journal_mode = WAL` 是个例外（它落在库头上、重启后仍然是 WAL），但它和别的
        PRAGMA 放在一起更好读，而且"建库"与"打开已有库"都需要执行它一遍。
        """
        conn = self._conn
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL 而不是 FULL：索引库可以从盘上重来，而 FULL 会让每批提交都等一次
        # 磁盘同步——全盘索引要提交几万次。真正的保证来自"批次是幂等的"（F4-04）。
        conn.execute("PRAGMA synchronous = NORMAL")
        # 外键要显式打开：`chunks.blob_id` 挂着 ON DELETE CASCADE，而 SQLite 默认
        # **不执行**外键——不打开它，删 blob 时留下的是一堆孤儿切片行（不报错）。
        conn.execute("PRAGMA foreign_keys = ON")
        # 索引跑着的同时还能被搜（WAL 的用处）。读侧拿不到锁时等一会儿再报错，
        # 而不是立刻抛 "database is locked"。
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = None

    def _require_fts5(self) -> None:
        """没有 FTS5 的 SQLite 要**立刻**说清楚，而不是等建表时报一句语法错。

        `D10` 的检索全建在 FTS5 上（`files_fts` / `chunks_fts`）。官方 Windows 的
        Python 自带的 SQLite 是带 FTS5 的，但"自带"不是保证——装出来的 Python 千奇
        百怪，而症状会是 `CREATE VIRTUAL TABLE ... USING fts5` 抛
        `no such module: fts5`，看起来像我们建表语句写错了。
        """
        try:
            self._conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(a)")
        except sqlite3.OperationalError as exc:
            raise StoreError(
                "当前 SQLite 没有 FTS5 模块（`no such module: fts5`），"
                f"而全文索引全靠它：{exc}"
            ) from exc
        finally:
            self._conn.execute("DROP TABLE IF EXISTS temp.__fts5_probe")

    def _ensure_schema(self) -> None:
        """建表（只在空的库上）并核对版本。

        判据是 `meta` 表在不在，而不是"文件是不是空的"：一个已经建过表的库再去跑一遍
        `CREATE TABLE` 会抛 `table meta already exists`，而"文件非空"还可能是 WAL
        与 `-shm` 文件留下的残迹。
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        has_meta = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if has_meta:
            if version != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"索引库的 schema 版本是 {version}，代码要的是 {SCHEMA_VERSION}。"
                    f"改过 schema.sql 就要重新索引（`lsearch index --rebuild`）"
                )
            return
        if version != 0:
            raise SchemaVersionError(
                f"库是空的但 user_version = {version}——这不是本工具建的库，先确认路径"
            )
        self._conn.executescript(SCHEMA_SQL)
        # PRAGMA 不吃参数占位符，只能拼字符串。拼进去的是本模块的常量（int），
        # 不是外部输入。
        self._conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        self._conn.commit()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    # ------------------------------------------------------------------ 文件

    def put_file(self, row: FileRow) -> bool:
        """写一行 `files` 并给 `files_fts` 建索引。**不提交**（见模块文档）。

        返回 `True` 表示这一行是**新**写进去的，`False` 表示库里已经有同一个路径
        （按 `NOCASE` 判），此时 `files_fts` 一个字也不动。

        判定用 `INSERT OR IGNORE` 的 `cursor.rowcount`：被忽略时它是 0。**先 SELECT
        再 INSERT 是两趟查询**，而全盘索引要跑几百万次；`rowcount` 是同一趟里拿到的
        答案。⚠ 它靠的是"`IGNORE` 掉的插入计入 0 行"这一条 SQLite 的行为——
        `test_store_files_fts.py` 里有一条测试专门钉它，因为一旦它变了，症状会是
        `files_fts` 里出现重复行（**不报错，只是搜出多条一样的**）。
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO files "
            "(path, volume, ext, mime, size_bytes, mtime_ns, quick_sig, content_hash,"
            " blob_id, frn, usn, status, tier, indexed_at, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.path,
                row.volume,
                row.ext,
                row.mime,
                row.size_bytes,
                row.mtime_ns,
                row.quick_sig,
                row.content_hash,
                row.blob_id,
                row.frn,
                row.usn,
                row.status,
                row.tier,
                row.indexed_at,
                row.error_msg,
            ),
        )
        if cur.rowcount == 0:
            return False
        file_id = cur.lastrowid
        self._conn.execute(
            "INSERT INTO files_fts (name_tok, path_tok, file_id) VALUES (?, ?, ?)",
            (
                tokenize(row.name, bigrams=FILES_BIGRAMS),
                tokenize(row.path, bigrams=FILES_BIGRAMS),
                file_id,
            ),
        )
        return True

    def get_file(self, path: str) -> FileRow | None:
        """按路径取一行。路径会先过 `store_path()`，调用方不必自己规范化。"""
        row = self._conn.execute(
            "SELECT id, path, volume, ext, mime, size_bytes, mtime_ns, quick_sig,"
            " content_hash, blob_id, frn, usn, status, tier, indexed_at, error_msg"
            " FROM files WHERE path = ?",
            (store_path(path),),
        ).fetchone()
        return None if row is None else _file_row(row)

    def count_files(
        self,
        *,
        status: str | None = None,
        tier: int | None = None,
        volume: str | None = None,
    ) -> int:
        """按条件计数。三个条件都可省，全省就是"库里一共多少行"。"""
        where: list[str] = []
        params: list[object] = []
        for column, value in (("status", status), ("tier", tier), ("volume", volume)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT count(*) FROM files"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return int(self._conn.execute(sql, params).fetchone()[0])

    # -------------------------------------------------------------- 分级那一趟

    def tier_counts(self) -> dict[tuple[str, int], int]:
        r"""盘一次点：`{(status, tier): 条数}`（只列**存在**的组合，没有 0）。

        台账（`tier.TierLedger`，由 `tier.ledger_from()` 从这份计数算出来）与
        `lsearch status` 都走它。**一条 SQL、一个交叉表**：`status` 与 `tier` 的
        每种组合各有多少行，剩下的解释全在 `tier.py` 那一侧——那种"算"要能在
        **不建库**的情况下被测，所以这里只做数据库擅长的那一件事。

        ⚠ 不返回零行：`GROUP BY` 天然只给出现过的组合，而"某个组合是 0"与"某个组合
        根本不在字典里"在 `ledger_from()` 那边是同一件事（它按 key 累加）。补齐成
        全表（7 × 3 = 21 个 key）只会多出 21 个需要被维护的常量。
        """
        return {
            (str(status), int(tier)): int(n)
            for status, tier, n in self._conn.execute(
                "SELECT status, tier, count(*) FROM files GROUP BY status, tier"
            )
        }

    def iter_tier_work(self, tier: int, *, limit: int | None = None) -> Iterator[FileRow]:
        r"""跑第 `tier` 级那一趟要处理的文件行（`F4-04` 的输入）。**不提交**。

        选择条件来自 `tier.work_condition()`（那里写着"为什么是这个条件"，包括
        Tier0 那条为什么**没有** `tier < ?` 子句）；这里只把它拼进 `files`。

        **按 `id` 排序**，`id` 就是插入顺序、也就是扫描顺序。这一条是给 `F4-04` 的
        断点续跑用的：中途 Ctrl-C 之后下一趟要能接着走，而"接着走"只有在**两次
        遍历的顺序一致**时才有意义——`WHERE` 挑出来的行集合会变（做完的行退出），
        但顺序不会跳。没有 `ORDER BY` 的话 SQLite 走 `idx_files_tier`，出来的顺序
        随索引形态变，续跑就会变成"每趟都从头扫一遍的大部分"。

        `limit` 是"这一趟最多吃多少行"（`F4-04` 分批用）。⚠ `limit <= 0` **当场报错**：
        `LIMIT 0` 是一个合法的 SQL、返回 0 行，于是"这一趟没活干"与"参数写错了"
        长得一模一样——那正是最难查的一类。（要"什么都不做"，别调这个方法。）

        ⚠ 一次全读进内存是**有意的**：全盘 100 万行时 `Iterator` 会被逐行取，
        而 `sqlite3` 的游标本来就是惰性的；分页（`LIMIT/OFFSET`）在这里只会更慢，
        因为 `OFFSET` 要走一遍跳过的行。
        """
        if limit is not None and limit < 1:
            raise ValueError(f"limit 要么不传，要么 ≥ 1，收到 {limit}")
        where, params = work_condition(tier)
        sql = (
            "SELECT id, path, volume, ext, mime, size_bytes, mtime_ns, quick_sig,"
            " content_hash, blob_id, frn, usn, status, tier, indexed_at, error_msg"
            f" FROM files WHERE {where} ORDER BY id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        for row in self._conn.execute(sql, params):
            yield _file_row(row)

    # ------------------------------------------------------------------ 检索

    def search_paths(self, query: str, *, limit: int = 20) -> PathSearch:
        """按文件名/路径检索（Tier0）。**不依赖正文提取**，索引跑完就可用（`A8`）。

        查询串先过 `tokens.query_tokens()`（精确模式 + 丢单字汉字），再拼成隐式 AND
        的 MATCH 表达式。**不做 OR 兜底**——`E38` 的读数说它买不到召回，而它的失败
        形态是"静默给出一堆不相关结果"，见 `tokens.py` 的模块文档五。

        拼不出 token 时**一次也不查**：`MATCH ''` 是语法错，而"没有可搜的内容"
        与"没搜到"要给用户看不同的话（`PathSearch.has_query`）。
        """
        raw = query or ""
        tokens = tuple(query_tokens(raw))
        expr = and_expr(tokens)
        if expr is None:
            return PathSearch(query=raw, tokens=tokens, expr=None, hits=())
        rows = self._conn.execute(
            "SELECT f.path, f.status, f.tier, f.size_bytes, f.mtime_ns, f.ext,"
            " f.error_msg, bm25(files_fts, ?, ?, 0.0) AS rank"
            " FROM files_fts JOIN files AS f ON f.id = files_fts.file_id"
            " WHERE files_fts MATCH ? ORDER BY rank LIMIT ?",
            (NAME_WEIGHT, PATH_WEIGHT, expr, limit),
        ).fetchall()
        return PathSearch(
            query=raw,
            tokens=tokens,
            expr=expr,
            hits=tuple(
                PathHit(
                    path=row[0],
                    status=row[1],
                    tier=row[2],
                    size_bytes=row[3],
                    mtime_ns=row[4],
                    ext=row[5],
                    error_msg=row[6],
                    rank=row[7],
                )
                for row in rows
            ),
        )

    # ------------------------------------------------------------------ 正文
    #
    # 一个文件走完 Tier1 的样子（写顺序就是这个顺序，都在同一个事务里）：
    #
    #   1. put_blob()           → 这份内容落在哪一行、要不要接着写
    #   2. put_chunks()         → 只有 needs_chunks 为真时才调
    #   3. set_file_extracted() → 把结局写回 files（status / tier / blob_id / error_msg）
    #
    # 失败的那两条路（这一类不做 / 这个文件坏了）**没有第 1、2 步**：没有正文就没有
    # 内容可挂，`blob_id` 留 NULL 是对的。第 3 步两种都要走——`skipped` 与 `error`
    # 各自是一个终态，不写回的话下一趟还会再挑一次。

    def find_blob(self, content_hash: str, *, parser_ver: str) -> int | None:
        r"""这份内容、这个版本，库里已经有现成的切片了吗。**只读**，什么都不改。

        `F4-04` 的 Tier1 那一趟**先问这个、再决定要不要读盘提取**——因为 `A9` 的字面
        要求是"同一份内容只**提取**与嵌入一次"，而只靠 `put_blob()` 的 `reused` 做不到
        那件事：走到 `reused` 时文件已经读进来、正文已经解析出来了，第二次提取白做
        （`E19` 的读数：小文件 2.87 ms/个，解析只比它更贵）。

        与 `put_blob()` 的复用判断**判据必须一致**（同一个 `content_hash`、同一个
        `parser_ver`），否则会出现"先问说没有、再写说复用"这种自相矛盾的组合。两处
        因此都用同一个 WHERE——**这一条是这两个方法之间唯一的耦合**，改一个要改另一个。

        ⚠ `chunk_count` 不参与判断：0 片的 blob（空正文）是**合法**的，而"复用一个 0
        片的 blob"给出的是"这份内容有正文、只是没有内容可切"——正确。

        ⚠ 它不保证那份 blob 的切片**此刻**还在。`put_blob()` 的 `replaced` 会先删旧
        切片再把 `parser_ver` 改成新的，两件事在同一个事务里——所以"版本对得上"与
        "切片齐着"要么同时成立，要么一起回滚。
        """
        row = self._conn.execute(
            "SELECT id FROM blobs WHERE content_hash = ? AND parser_ver = ?",
            (content_hash, parser_ver),
        ).fetchone()
        return None if row is None else int(row[0])

    def put_blob(
        self,
        *,
        content_hash: str,
        size_bytes: int,
        extractor: str,
        parser_ver: str,
        now: int | None = None,
    ) -> BlobWrite:
        """把"这份内容"登记进 `blobs`，按内容哈希去重（`D12`）。**不提交**。

        三种结局（`BlobWrite.action`）：

        * `created`——库里没有这份内容，插一行；
        * `reused`——有，且 `parser_ver` **一样**：一个字节都不动。同一份内容在一轮里
          被第二个路径碰到、或第二天重扫碰到，走的都是这一格；
        * `replaced`——有，但 `parser_ver` **不一样**：**旧切片与旧的 FTS 行先删掉**，
          再把这一行改成新的提取器与版本。这是计划 §4.4.1 那条"缓存键必须带上版本"
          的落点：换了提取器之后，旧的切片**必须**被丢掉，否则它会永远躺在库里——
          不报错，只是搜不到新格式里的内容。

        ⚠ `parser_ver` **没有默认值**，这是刻意的：它的取值必须同时覆盖"提取器实现"
        与"分块参数"（计划 §4.4），而这两样分头变化时都得触发一次重切。给它一个
        默认值（比如 `__version__`）就等于让它默认做错——包版本号与提取逻辑的改动
        完全无关。取值由谁组装写在 `F4-04` 那一层。

        ⚠ `replaced` 这条路径**在 `P5` 落地向量之后还要多做一件事**：旧切片的
        `vec_label` 要标成 tombstone，否则 HNSW 里会留着指向已经不存在的内容的向量。
        `P4` 安全只因为那时还没有向量（`chunks.vec_label` 一律 NULL）——见 P4「遗留」。
        """
        if not content_hash:
            raise ValueError("content_hash 不能为空——blobs 是按内容去重的")
        if not parser_ver:
            raise ValueError("parser_ver 不能为空——空值会让'版本变了'这件事无法察觉")
        row = self._conn.execute(
            "SELECT id, parser_ver FROM blobs WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO blobs"
                " (content_hash, size_bytes, extractor, parser_ver, chunk_count, created_at)"
                " VALUES (?, ?, ?, ?, 0, ?)",
                (content_hash, size_bytes, extractor, parser_ver, _now(now)),
            )
            return BlobWrite(int(cur.lastrowid), BLOB_CREATED)
        blob_id = int(row[0])
        if row[1] == parser_ver:
            return BlobWrite(blob_id, BLOB_REUSED)
        self._delete_chunks(blob_id)
        # created_at **不动**：这一行的身份（"这份内容第一次见到是什么时候"）没变，
        # 变的是"它按哪个版本的解析器切过"。后者问 files.indexed_at。
        self._conn.execute(
            "UPDATE blobs SET size_bytes = ?, extractor = ?, parser_ver = ?,"
            " chunk_count = 0 WHERE id = ?",
            (size_bytes, extractor, parser_ver, blob_id),
        )
        return BlobWrite(blob_id, BLOB_REPLACED)

    def put_chunks(
        self,
        blob_id: int,
        chunks: Sequence[Chunk],
        *,
        title: str | None = None,
        modality: str = "text",
    ) -> int:
        r"""把切片写进 `chunks` + `chunks_fts`，并更新 `blobs.chunk_count`。**不提交**。

        返回写进去的片数（**空正文就是 0**——那是合法的：一个 0 字节文件有 blob、
        没有切片）。

        **只应在 `BlobWrite.needs_chunks` 为真时调用。** 重复调用会当场抛
        `StoreError`，而不是在同一个 blob 下留两份一样的文本——后者搜出来是重复的，
        不报错，只在有人盯着结果列表时才看得出来。

        `chunks_fts` 的那一行**用 `chunks.id` 当 `rowid`**（同时填 `chunk_id` 那一列，
        与 `schema.sql` 一致）。这样删除一片是 `WHERE rowid = ?` 一次索引查找，而
        `chunk_id` 是 `UNINDEXED` 的——拿它当删除条件等于每次全扫一遍 FTS 表。
        两条路径都指向同一个 id，`search_chunks()` 的 JOIN 也按 `rowid`。

        `title` 存的是**分词后**的形态（`chunks_fts.title` 是要被 MATCH 的列）。
        它因此不能拿去显示——`ChunkHit` 里没有 title 就是这个原因。
        """
        existing = self._conn.execute(
            "SELECT count(*) FROM chunks WHERE blob_id = ?", (blob_id,)
        ).fetchone()[0]
        if existing:
            raise StoreError(
                f"blob {blob_id} 已经有 {existing} 片切片了——put_chunks() 只应在"
                f" put_blob().needs_chunks 为真时调用；重复写会在库里留下两份一样的文本"
            )
        title_tok = tokenize(title or "", bigrams=CHUNK_BIGRAMS)
        written = 0
        for chunk in chunks:
            cur = self._conn.execute(
                "INSERT INTO chunks"
                " (blob_id, ord, char_start, char_end, text, n_tokens, modality)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    blob_id,
                    chunk.ord,
                    chunk.char_start,
                    chunk.char_end,
                    chunk.text,
                    chunk.n_tokens,
                    modality,
                ),
            )
            chunk_id = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO chunks_fts (rowid, text_tok, title, chunk_id)"
                " VALUES (?, ?, ?, ?)",
                (
                    chunk_id,
                    tokenize(chunk.text, bigrams=CHUNK_BIGRAMS),
                    title_tok,
                    chunk_id,
                ),
            )
            written += 1
        # 无条件更新（写 0 片也要落）：`replaced` 那条路刚把 chunk_count 清成 0，
        # 而"这一份内容切出 0 片"本身是事实，不该留着一个旧数字。
        self._conn.execute(
            "UPDATE blobs SET chunk_count = ? WHERE id = ?", (written, blob_id)
        )
        return written

    def set_file_extracted(
        self,
        path: str,
        *,
        status: str,
        tier: int,
        content_hash: str | None = None,
        blob_id: int | None = None,
        error_msg: str | None = None,
        indexed_at: int | None = None,
        now: int | None = None,
    ) -> bool:
        r"""把一个文件的提取结局写回它那一行。**不提交**。返回有没有这一行。

        这是 `A6` 后半句（"原因写入 `files.error_msg`"）的**唯一**落点，也是 Tier1
        那一趟唯一改动已有行的入口——`put_file()` 只插入、不更新（`F4-01` 刻意如此：
        Tier0 那一趟反复跑不该把好行改成坏的）。

        四道校验，都是"写错了不报错、只在很久以后表现为查不出来"的那一类：

        * `status` 必须在 `STATUSES` 里。写错一个字母不会报错，只会让 Tier1 的选择
          条件再也挑不到这个文件。
        * `tier` 必须在 `0..MAX_TIER`。⚠ `tier` 记的是**达成的最高级**，不是"跑到哪
          一步"：`fulltext` → `1`，而 `skipped` / `error` 是 `0`（这一类文件本来就
          没做，把它记成 1 等于说它进过正文索引）。"该不该再做"由 `status` 说
          （`F4-03` 的选择条件同时看这两列）。
        * **这两个列不许互相矛盾**（`F4-03` 加的，`tier.STATUS_MIN_TIER` /
          `STATUS_MAX_TIER`）：`status` 声称做到的那一级，`tier` 必须至少达到
          （`fulltext` ⇒ `tier >= 1`、`indexed` ⇒ `tier >= 2`）；反方向只有
          "什么都没做"的两个 status 有上界（`pending` / `metadata` ⇒ `tier == 0`）。
          ⚠ 两个方向不对称是因为**后果不对称**：`status='fulltext', tier=0` 那种行
          下一趟的 `tier < 1 AND status IN (…, 'fulltext', …)` 会**再挑到它一次**，
          重做一遍正好把它修好；而 `status='metadata', tier=1` 那边三趟都不接它
          （Tier0 那趟只挑 `pending`、Tier1 那趟要求 `tier < 1`、Tier2 那趟的
          `status` 名单里没有 `metadata`），它就那样躺在库里，而台账那一侧还把它
          记成"第 1 级做到了"——**报就绪、其实没有正文**。完整推导写在 `tier.py`
          的模块文档里。
        * `error_msg` 与 `status` 的对应：`error` **必须**带原因（一个没有原因的失败
          是最难查的记录——每一层都觉得自己没错，`extract/batch.py` 的 `Outcome`
          在那一头把关），其余 status **必须不带**（`error_msg` 非空是全库唯一的
          "失败"判据）。空串一律归一成 `NULL`——`batch.Outcome.error_msg` 给的就是
          空串，两边形态不同会让"这一格是不是空的"要写两种判断。

        `indexed_at` 不传就取当前时间：这个方法只在"这一个文件刚被处理过"时调用，
        让调用方每次都自己取时间是白给的一处不一致。
        """
        if status not in STATUSES:
            raise ValueError(f"status 只能是 {sorted(STATUSES)} 之一，收到 {status!r}")
        if not 0 <= tier <= MAX_TIER:
            raise ValueError(f"tier 只能是 0..{MAX_TIER}，收到 {tier}")
        floor = STATUS_MIN_TIER.get(status, 0)
        if tier < floor:
            raise ValueError(
                f"status={status!r} 说这个文件做到了第 {floor} 级，而 tier={tier}"
                f" 比它低——两列在说两件事，其中一件是假的"
            )
        ceiling = STATUS_MAX_TIER.get(status)
        if ceiling is not None and tier > ceiling:
            raise ValueError(
                f"status={status!r} 说这个文件什么都没做，而 tier={tier} 说它到过"
                f"第 {tier} 级：这样的行没有任何一趟会再挑到它（它既不算做完，"
                f"也不在选择条件里）"
            )
        msg = (error_msg or "").strip() or None
        if status == _STATUS_WITH_ERROR and msg is None:
            raise ValueError(f"status={status!r} 必须带 error_msg（没有原因的失败查不出来）")
        if status != _STATUS_WITH_ERROR and msg is not None:
            raise ValueError(
                f"status={status!r} 不该带 error_msg（非空的 error_msg 是全库唯一的"
                f"失败判据）：{msg!r}"
            )
        cur = self._conn.execute(
            "UPDATE files SET status = ?, tier = ?, content_hash = ?, blob_id = ?,"
            " error_msg = ?, indexed_at = ? WHERE path = ?",
            (
                status,
                tier,
                content_hash,
                blob_id,
                msg,
                _now(now) if indexed_at is None else int(indexed_at),
                store_path(path),
            ),
        )
        return cur.rowcount > 0

    def paths_for_blob(self, blob_id: int) -> tuple[str, ...]:
        """这份内容（blob）被哪些路径指着，按路径排序。

        去重（`D12`）在结果面上就是这一句话：**同一份内容命中一次，但它可能对应
        好几个文件**。少了这一步，用户会以为"只搜到一个文件"，而那些副本在库里
        是明确记着的。
        """
        return tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT path FROM files WHERE blob_id = ? ORDER BY path", (blob_id,)
            )
        )

    def count_chunks(self, *, blob_id: int | None = None) -> int:
        """切片总数，或某个 blob 的片数。报表与测试用。"""
        if blob_id is None:
            return int(self._conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
        return int(
            self._conn.execute(
                "SELECT count(*) FROM chunks WHERE blob_id = ?", (blob_id,)
            ).fetchone()[0]
        )

    def search_chunks(self, query: str, *, limit: int = 20) -> ChunkSearch:
        """按正文检索（Tier1）。切法与查询侧的处理**与 Tier0 同一套**（`tokens.py`）。

        ⚠ 只对**已经做过 Tier1**的文件有结果。库里一篇正文都没有时它返回空列表，
        而那与"跑完了、但这个词没出现"长得一模一样——分级就绪的标注（`D10`）因此
        不能由这个方法自己给，得看 tier 那本账（`ChunkSearch` 的类文档里写了）。

        **不做列加权**（`bm25(chunks_fts)` 用默认的 1.0）：`files_fts` 那两列敢加权
        是因为"名字优先于它所在的目录"是常识级的不变量，而"命中标题该不该排在命中
        正文前面"**没测过**——给它一个拍出来的权重，就是给排序加一个说不清的东西。
        要加权得先有 `A4` 那 20 条评测。
        """
        raw = query or ""
        tokens = tuple(query_tokens(raw))
        expr = and_expr(tokens)
        if expr is None:
            return ChunkSearch(query=raw, tokens=tokens, expr=None, hits=())
        rows = self._conn.execute(
            "SELECT c.id, c.blob_id, c.ord, c.text, c.char_start, c.char_end,"
            " c.n_tokens, bm25(chunks_fts) AS rank"
            " FROM chunks_fts JOIN chunks AS c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (expr, limit),
        ).fetchall()
        return ChunkSearch(
            query=raw,
            tokens=tokens,
            expr=expr,
            hits=tuple(
                ChunkHit(
                    chunk_id=row[0],
                    blob_id=row[1],
                    ord=row[2],
                    text=row[3],
                    char_start=row[4],
                    char_end=row[5],
                    n_tokens=row[6],
                    rank=row[7],
                )
                for row in rows
            ),
        )

    def _delete_chunks(self, blob_id: int) -> int:
        r"""删掉一个 blob 的全部切片与它们在 `chunks_fts` 里的行。返回删了几片。

        **两处都要删，而且要按这个顺序**：先按 `rowid` 删 FTS（`chunks_fts` 是独立的
        虚拟表，`chunks` 上的 `ON DELETE CASCADE` 管不着它），再删 `chunks`。
        反过来做的话，`chunks` 的 id 已经没了，FTS 那批行就成了孤儿——它们搜得出来，
        而 JOIN 不到任何切片，于是"搜到一条、显示不出来"。

        ⚠ 这是当前唯一会删 FTS 行的地方。`files_fts` 那边（`files` 的增删）还没有
        对应的清理路径，归 `F6` 的 `--prune`（P4「遗留」）。
        """
        cur = self._conn.execute(
            "DELETE FROM chunks_fts WHERE rowid IN"
            " (SELECT id FROM chunks WHERE blob_id = ?)",
            (blob_id,),
        )
        deleted = cur.rowcount
        self._conn.execute("DELETE FROM chunks WHERE blob_id = ?", (blob_id,))
        return int(deleted)

    # -------------------------------------------------- 嵌入缓存那一层（F5-03）

    def find_vectors(
        self, text_hashes: Sequence[str], *, embed_ver: str
    ) -> dict[str, int]:
        r"""这批文本里，哪些**已经有向量了**。只读，什么都不改。

        返回 `{text_hash: vec_label}`，只含命中的那些键。**`embed_ver` 是必填的**，
        理由与 `find_blob()` 那条一样：键是 `(text_hash, embed_ver)` 两列，少了版本
        这一半，"换了模型之后复用旧向量"就成了一条不报错的路径——而它的症状是"换过
        模型的文件里，有些切片按旧模型的语义参与排序"，看起来一切正常。

        ⚠ **分块查**：SQLite 的 `?` 参数上限是编译期的 `SQLITE_MAX_VARIABLE_NUMBER`
        （老版本 999、本机 3.50.4 实测 32766，见 `_SQL_PARAM_LIMIT` 上的那段），而这个
        方法的入参长度由调用方的批次决定。一次 `IN (…)` 塞两万个 `?` 不会报错得很难看
        ——它会**直接抛** `sqlite3.OperationalError`，把整趟索引打断在第一批上。
        所以这里按 `_SQL_PARAM_LIMIT` 切段，一段一条 `SELECT`，**每段的结果都要合并
        回来**（少了合并的症状是"一部分键每趟都报没有、于是每趟重嵌"）。

        ⚠ 命中的 label **不保证还有效**：`vec_label` 指的是 HNSW 里的 id，而 HNSW
        被重建、或者某一版的 HNSW 文件丢了之后，库里这些行还在。用之前要确认那一版
        的向量索引确实在（归 `F5-04` 的加载路径；本方法只如实回报"库里写了什么"）。
        """
        found: dict[str, int] = {}
        if not text_hashes:
            return found
        for chunk in _chunked(text_hashes, _SQL_PARAM_LIMIT):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT text_hash, vec_label FROM embed_cache"
                f" WHERE embed_ver = ? AND text_hash IN ({placeholders})",
                (embed_ver, *chunk),
            )
            for text_hash, vec_label in rows:
                found[str(text_hash)] = int(vec_label)
        return found

    def put_vectors(self, entries: Sequence[tuple[str, int]], *, embed_ver: str) -> int:
        r"""把"这段文本的向量在 HNSW 里是哪个 id"记进 `embed_cache`。**不提交**。

        返回**真正插进去的行数**（`INSERT OR IGNORE`）。

        两条边界，方向不同：

        * **同一次调用里出现重复的键：当场 `StoreError`。** 那不是竞态，是调用方的
          去重没做——而"同一段文本有两个 label"正是本表要防的那件事（`D12`：同一段
          文本只嵌一次）。`F5-03` 的 `CachePlan` 在算 todo 时就去重了，所以正常的
          那条路永远碰不到它。这与 `put_chunks()` 拒绝重复调用同源。
        * **库里已经有这个键（另一个进程/上一趟写下的）：忽略，不覆盖。** 两边存的
          是同一条文本的两个向量副本，谁都不是错的；`INSERT OR REPLACE` 会让"先写
          的"变成不可达（连带它指向的 label 变成只能靠 HNSW 自己引用的孤儿），而
          `OR IGNORE` 至少让结果**确定**：先说的那一份说了算。返回值因此有意义——
          返回值比入参少，就说明有别的写者。

        `vec_label` 不许是负数的约定不在这一层判（`F5-04` 的 label 分配器说了算），
        这里只要求它存在——`embed_cache.vec_label` 在 `schema.sql` 里是 `NOT NULL`。
        """
        if not embed_ver:
            raise ValueError("embed_ver 不能为空——它是缓存键的一半")
        seen: set[str] = set()
        for text_hash, _label in entries:
            if text_hash in seen:
                raise StoreError(
                    f"同一次 put_vectors() 里 text_hash {text_hash!r} 出现了两次。"
                    "这不是竞态、是调用方的去重没做——同一段文本有两个 label 正是"
                    "embed_cache 要防的那件事（D12）。先合并再去写。"
                )
            seen.add(text_hash)
        written = 0
        for text_hash, vec_label in entries:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO embed_cache (text_hash, embed_ver, vec_label)"
                " VALUES (?, ?, ?)",
                (text_hash, embed_ver, int(vec_label)),
            )
            written += cur.rowcount
        return written

    def count_vectors(self, *, embed_ver: str | None = None) -> int:
        """`embed_cache` 里有多少条。给定 `embed_ver` 就只数那一版。

        `embed_ver=None`（全数）是给报表用的——"库里到底攒了多少条缓存"要能回答，
        但它**不是一个可以拿来当"有效"用的数**：不同版本的行互相不可用。
        """
        if embed_ver is None:
            row = self._conn.execute("SELECT count(*) FROM embed_cache").fetchone()
        else:
            row = self._conn.execute(
                "SELECT count(*) FROM embed_cache WHERE embed_ver = ?", (embed_ver,)
            ).fetchone()
        return int(row[0])

    def delete_vectors(self, *, embed_ver: str) -> int:
        r"""清掉某一版的全部缓存条目并返回条数。**不提交**。

        `embed_ver` **没有默认值**是刻意的：这一版的 label 失效（换了模型、或者向量
        索引要重建），别的版本那一堆行**一点问题都没有**。给个默认值就等于把"顺手清
        个缓存"和"清掉所有版本"写成同一个调用——而后者的代价是下一次索引把整库
        重嵌一遍（`A1` 的两万文件基准集上是数小时到一天）。

        ⚠ 谁该调它：**重建向量索引的人**（`F5-04` / `F6` 的 `reindex`）。`vec_label`
        是 HNSW 里的位置，索引重建之后旧的位置指向的不是旧的向量——**这一段必须与
        建索引在同一个事务/同一个流程里**，否则中间崩一下就留下"缓存说有、索引里
        没有"的窗口。本方法只提供那个动作，配平归调用方。
        """
        if not embed_ver:
            raise ValueError("embed_ver 不能为空——清缓存清的是某一版，不是'所有'")
        cur = self._conn.execute(
            "DELETE FROM embed_cache WHERE embed_ver = ?", (embed_ver,)
        )
        return int(cur.rowcount)

    # -------------------------------------------- 切片那一侧（F5-04）

    def chunks_for_blobs(
        self, blob_ids: Sequence[int]
    ) -> dict[int, tuple[ChunkText, ...]]:
        r"""这几份内容的切片，`{blob_id: (切片…)}`，每份按 `ord` 排序。

        `F5-04` 建索引那一趟用它把"这一批文件的切片"一次读出来：**一批一条 `SELECT`**
        而不是一个文件一条（500 个文件一批就是 500 次往返，而这一趟的瓶颈本该只在
        嵌入器上）。

        ⚠ **没有切片的 blob 不在字典里**（0 字节文件是合法的：有 blob、没有切片）。
        调用方用 `.get(blob_id, ())` 而不是 `[blob_id]`——写成下标的话，一个空文件
        就能让整趟索引停在第一批上，而"这个文件是空的"根本不是错误。

        ⚠ 只返回 `text` 一份原文，不返回 `chunks_fts` 那一份分词结果：喂给嵌入器的
        是**原文**（`F5-01` 的键就是按"喂进模型的那串字符"算的），而分词那份是小写、
        带空格的，拿去嵌入等于换了一个模型输入。这与 `ChunkHit.text` 那条同一个理由。
        """
        out: dict[int, list[ChunkText]] = {}
        if not blob_ids:
            return {}
        for part in _chunked(blob_ids, _SQL_PARAM_LIMIT):
            placeholders = ",".join("?" * len(part))
            rows = self._conn.execute(
                "SELECT id, blob_id, ord, text FROM chunks"
                f" WHERE blob_id IN ({placeholders}) ORDER BY blob_id, ord",
                tuple(part),
            )
            for chunk_id, blob_id, ord_, text in rows:
                out.setdefault(int(blob_id), []).append(
                    ChunkText(
                        chunk_id=int(chunk_id),
                        blob_id=int(blob_id),
                        ord=int(ord_),
                        text=str(text),
                    )
                )
        return {blob_id: tuple(items) for blob_id, items in out.items()}

    def chunks_by_labels(self, labels: Sequence[int]) -> dict[int, ChunkText]:
        r"""`vec_label` → 那一片切片（语义检索那一侧的取回）。

        与 `paths_for_blob()` 的分工：这里回答"这个 label 是哪一片"，那一头回答
        "这一片属于哪些文件"。两条都必要——`D12` 的去重让一个 label 可能对应好几个
        路径，而"命中哪一片"与"命中哪个文件"不是一个问题。

        ⚠ **查不到的 label 不报错、只是不在结果里**。查不到有两种来源：索引里有孤儿
        向量（`check_consistency()` 报的 `orphans`，跨过程崩溃留下的），或者库里那一行
        被删了。两种情况下调用方要做的都是**丢掉这一条命中**（`.get()` + `continue`），
        而不是把整个查询判成失败——一条命中的账不对，不该把另外十九条一起废掉。
        """
        found: dict[int, ChunkText] = {}
        if not labels:
            return found
        for part in _chunked(labels, _SQL_PARAM_LIMIT):
            placeholders = ",".join("?" * len(part))
            rows = self._conn.execute(
                "SELECT vec_label, id, blob_id, ord, text FROM chunks"
                f" WHERE vec_label IN ({placeholders})",
                tuple(part),
            )
            for vec_label, chunk_id, blob_id, ord_, text in rows:
                found[int(vec_label)] = ChunkText(
                    chunk_id=int(chunk_id),
                    blob_id=int(blob_id),
                    ord=int(ord_),
                    text=str(text),
                )
        return found

    def set_chunk_vectors(
        self, entries: Sequence[tuple[int, int]], *, embed_ver: str
    ) -> int:
        r"""把 `(chunk_id, vec_label)` 写进 `chunks.vec_label` / `chunks.embed_ver`。

        **不提交**，返回真正改动的行数。这是"这一片去哪个向量里找它的意思"唯一的写入口。

        `embed_ver` 与 `vec_label` **必须一起写**：`chunks.embed_ver` 非空是"这一片
        进过向量索引"的判据（`tier.py` 的模块文档把 Tier2 定义在它上面），只写 label
        会留下一行"有位置、不知道是哪一版模型放的"的记录。

        ⚠ **同一次调用里出现重复的 chunk_id：当场 `StoreError`**（与 `put_vectors()`
        同源）。那不是竞态，是调用方没对齐——而"一片切片在一次调用里拿到两个 label"
        意味着后写的那个覆盖前一个，前一个 label 从此是一段谁也指不到的向量。
        同时 `chunk_id` 不存在时**静默改 0 行**：那不是错误（切片可能刚被 `--prune`
        删掉），返回值会让调用方看出少了。
        """
        if not embed_ver:
            raise ValueError("embed_ver 不能为空——它是缓存键的一半，也是这一列的判据")
        seen: set[int] = set()
        for chunk_id, _label in entries:
            if chunk_id in seen:
                raise StoreError(
                    f"同一次 set_chunk_vectors() 里 chunk_id {chunk_id} 出现了两次："
                    "后一个 label 会覆盖前一个，而前一个从此是一段没人指得到的向量。"
                    "先合并再去写。"
                )
            seen.add(int(chunk_id))
        changed = 0
        for chunk_id, vec_label in entries:
            cur = self._conn.execute(
                "UPDATE chunks SET vec_label = ?, embed_ver = ? WHERE id = ?",
                (int(vec_label), embed_ver, int(chunk_id)),
            )
            changed += cur.rowcount
        return changed

    def count_chunk_vectors(self, *, embed_ver: str | None = None) -> int:
        r"""有多少片切片进过向量索引（`chunks.vec_label` 非空）。

        它与 `count_vectors()`（`embed_cache` 的条数）**不等**，而且这个差本身就是读数：
        缓存按**唯一文本**记，切片按**片**记，所以有重复文本时前者一定更小。

        ⚠ 它**不**能当作"索引里有多少条向量"的判据——那个数在索引文件里
        （`VectorIndex.ntotal`）。两者要一起看：本数 < `ntotal` 说明索引里躺着
        谁也不指的孤儿向量（跨过程崩溃留下的），本数 > `ntotal` 才是坏事。
        """
        if embed_ver is None:
            row = self._conn.execute(
                "SELECT count(*) FROM chunks WHERE vec_label IS NOT NULL"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT count(*) FROM chunks WHERE vec_label IS NOT NULL"
                " AND embed_ver = ?",
                (embed_ver,),
            ).fetchone()
        return int(row[0])

    def set_file_indexed(self, path: str, *, now: int | None = None) -> bool:
        r"""把一个文件标成"做到了最后一级"（`status = 'indexed'`、`tier = 2`）。

        **不提交**，返回有没有这一行。

        ⚠ 它是 `set_file_extracted()` 的窄包装，存在的理由只有一条：那个方法把
        `content_hash` 与 `blob_id` **无条件写下去**，而它们的默认值是 `None`——
        Tier2 那一趟不重新提取，调用方手上没有这两个值，直接调它就等于把这一行
        **从它的 blob 上摘下来**。后果不是报错：`blob_id = NULL` 之后这一行还认得
        Tier0 的路径检索，而**正文检索再也找不到它**（`chunks` 是按 blob 挂的，
        `paths_for_blob()` 找不到这条路径了）。所以这里先把两列读出来、原样传回去。
        """
        row = self._conn.execute(
            "SELECT content_hash, blob_id FROM files WHERE path = ?",
            (store_path(path),),
        ).fetchone()
        if row is None:
            return False
        return self.set_file_extracted(
            path,
            status=STATUS_INDEXED,
            tier=TIER_SEMANTIC,
            content_hash=row[0],
            blob_id=row[1],
            now=now,
        )

    def reset_vector_state(self, *, embed_ver: str) -> VectorReset:
        r"""把向量那一层清回"还没做过"：缓存、切片上的 label、文件的状态。**不提交**。

        三件事必须在**同一个事务**里做完，顺序也固定：

        1. `embed_cache` 里这一版的行删掉（`delete_vectors()`）；
        2. `chunks.vec_label` / `chunks.embed_ver` 清成 `NULL`（**只清这一版**的）；
        3. `files` 里 `tier >= 2 AND status = 'indexed'` 的行退回
           `status = 'fulltext'`、`tier = 1`。

        少了第 1 步，下一趟全部命中缓存、**一条都不嵌**，而索引是空的——那一批切片
        永远拿不到向量（每次查都命中、每次都指向不存在的位置）。少了第 2 步，切片上
        留着一堆指向旧索引的 label。少了第 3 步，Tier2 那一趟**再也挑不到这些文件**
        （`status = 'indexed'` 与 `tier = 2` 都不在选择条件里），于是"重建"变成一个
        只对了前半截的动作。

        ⚠ 第 3 步只动 `indexed` 的行，`stale` 不动：`stale` 是"内容变过、要重做"，
        它本来就会被 Tier2 那一趟挑到（`WORK_STATUSES[TIER_SEMANTIC]` 里有它），
        在这里顺手把它的 `tier` 压回 1 反而抹掉了"它到过第 2 级"这个事实。
        归 `--rebuild`（`F5-04`）调。
        """
        cached = self.delete_vectors(embed_ver=embed_ver)
        cur = self._conn.execute(
            "UPDATE chunks SET vec_label = NULL, embed_ver = NULL WHERE embed_ver = ?",
            (embed_ver,),
        )
        chunks = int(cur.rowcount)
        cur = self._conn.execute(
            "UPDATE files SET status = ?, tier = ? WHERE tier >= ? AND status = ?",
            # ⚠ 这里那个 `'fulltext'` 是字面量而不是常量：同名常量住在
            # `extract/batch.py`（提取层对自己结局的叫法），而本模块不该反向依赖提取层。
            # 两处同名常量是 `F5-04` 之前就有的状况，见 P5「遗留」。
            ("fulltext", TIER_FULLTEXT, TIER_SEMANTIC, STATUS_INDEXED),
        )
        files = int(cur.rowcount)
        return VectorReset(cached=cached, chunks=chunks, files=files)

    # ------------------------------------------------------------------ 杂项

    def iter_paths(self) -> Iterator[str]:
        """库里所有路径。`--prune`（`F6-03`）与测试用。"""
        for row in self._conn.execute("SELECT path FROM files ORDER BY id"):
            yield row[0]

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])


def _file_row(row: tuple) -> FileRow:
    """`SELECT` 出来的一行 → `FileRow`。

    顺序必须与 `get_file()` 的列清单一致（`id` 在最后）。这里**故意**不复用
    `sqlite3.Row` 的按列名取值：那需要在 `_configure()` 里设 `row_factory`，
    而设了它之后**每一处** `fetchone()[0]` 都要跟着改——一处漏改就会静默取到
    `Row` 对象而不是标量。
    """
    (
        file_id,
        path,
        _volume,
        ext,
        mime,
        size_bytes,
        mtime_ns,
        quick_sig,
        content_hash,
        blob_id,
        frn,
        usn,
        status,
        tier,
        indexed_at,
        error_msg,
    ) = row
    return FileRow(
        id=file_id,
        path=path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        status=status,
        tier=tier,
        ext=ext,
        mime=mime,
        quick_sig=quick_sig,
        content_hash=content_hash,
        blob_id=blob_id,
        frn=frn,
        usn=usn,
        indexed_at=indexed_at,
        error_msg=error_msg,
    )
