r"""Tier0 / Tier1 / Tier2 那一趟的编排：批量、提交、断点续跑（`F4-04` / `F5-04`）。
测试落点 `tests/unit/test_index_resume.py`（前两级）与 `tests/unit/test_vectors.py`（第三级）。

三趟，正好对着 `D10` 的三级：

* **Tier0**（`run_tier0`）——扫一遍盘，把**留着的**条目写进 `files` +
  `files_fts`。不读任何正文，分钟级。被规则排除的条目**一条都不写**（它们不进索引，
  这正是"排除规则是唯一最有效的杠杆"那句话的落点）。
* **Tier1**（`run_tier1`）——从库里挑"还没做正文"的行，逐个提取、切片、写
  `blobs` / `chunks` / `chunks_fts`，并把结局写回 `files`。
* **Tier2**（`run_tier2`）——从库里挑"还没做向量"的行，把它们（已经切好的）切片
  嵌成向量、加进 HNSW、写 `chunks.vec_label`，并把结局写回 `files`。**天级**。

编排里没有一行提取、切片、嵌入或写库的实现：那些分别在 `extract/`、`chunk/`、
`embed/`、`index/sqlite_store.py`、`index/vectors_faiss.py`。这个模块只回答
**顺序、批量与中断**三件事。

------------------------------------------------------------------------------
一批 = 一个事务
------------------------------------------------------------------------------

`SqliteStore` 的每一个 `put_*` / `set_*` 都不提交（那是刻意的），提交点因此在这里：
每 `PerfCfg.commit_every_files` 个文件一次（缺省 500）。这一个数字同时决定了三件事，
它们其实是同一件事的三种说法：

* **中断会丢多少**——最多丢当前这一批。一个文件的三处写入（`blobs` / `chunks` /
  `files` 那一行的结局）落在同一个事务里，所以**丢就一起丢**，库里不会留下"正文写了
  但结局没写"或者反过来的半截状态（`sqlite_store` 的模块文档把这条写成了续跑的依据）。
* **`--resume` 接得上**——重新跑一遍就是接续，不需要额外的游标或进度文件。见下面。
* **一次 fsync 摊销多少**——500 这个数的理由是 IO 次数，不是内存。

------------------------------------------------------------------------------
"重跑一遍"就是"续跑"，所以 `--resume` 说的不是那件事
------------------------------------------------------------------------------

`A1` 的字面要求是"中断后 `--resume` 能接续"。落地时这里要分清两件事，否则会做出一个
**什么都不做**的开关：

* **接续本身不需要开关。** Tier0 的 `put_file()` 是 `INSERT OR IGNORE`（已有的行一个
  字节都不动），Tier1 的选择条件（`tier.work_condition()`）挑的是"还没做到这一级的
  行"。所以**普通的一条 `lsearch index` 本来就是接着上次跑的**——做完的行退出选择集，
  没做完的还在。"中断后接得上"是这个设计的结果，不是某个参数的功劳。
* **`--resume` 说的是另一件事：这一趟不重新扫盘。** 它跳过 Tier0，直接从库里挑还没
  做完 Tier1 的行。适用场合是"上一个全量跑到第三天在 Tier1 阶段被杀掉，我只想把它
  跑完"——那时重新走一遍 300 多万个目录项（`E11`：C: 盘 347 万条目）是纯浪费，而
  盘上这三天里新出现的文件**归下一轮的普通 `lsearch index`**。

所以 `--resume` 的文档措辞是"接着上一轮跑完，不重新扫盘"，**不是**"能接续的前提"。
把它说成后者会让人以为不加它就会从头再来一遍——而事实相反。

⚠ `--resume` 配上一个空的库是"没有可接续的东西"（`P4` 的正常路径不看 `pending`，
见 `tier.py`），它**不报错**，只是什么都不做。这一条必须由调用方说出来：一条打印
"完成"却什么都没做的命令，与"索引建好了"在终端上没有区别。

------------------------------------------------------------------------------
超限的文件（`NAME_ONLY`）记成 `skipped`
------------------------------------------------------------------------------

扫描器判定的"超过 `max_file_mb`"的文件**仍然进 `files` 与 `files_fts`**（"文件名叫
什么"这件事上，64 MiB 的日志与 1 KiB 的笔记没有区别），但它的正文这一轮不做。它记成
`skipped`——`tier.TERMINAL_STATUSES` 之一，于是：

* 它**不会**被 Tier1 那趟挑中（选择条件排除终态）；
* 它**不算**在 Tier1 的分母里（`expected_at(1) = total - terminal`）。这一条是必须的：
  把全盘上的压缩包与镜像算进分母，"正文索引就绪"就永远为假。

⚠ 代价是两个说不清的地方，都记在 `PROGRESS.md` §4 P4「遗留」里：

1. **`skipped` 一个词现在有两个成因**（`batch.STATUS_SKIPPED` 的"这一类 v1 不做"与
   这里的"这一轮不做正文"）。`lsearch explain`（P6）要能分开说，现在分不开。
2. **把 `max_file_mb` 调大之后，这些行不会自己回来。** `put_file()` 只插入不更新，
   所以第二轮扫描碰到同一个路径时它是"已有的行"，一个字都不改——症状是"我明明把上限
   调到 512 了，那些大文件还是搜不到正文"，且不报错。要让它们回来得有人把 `status`
   从 `skipped` 改回去，那是**变更检测**（`F6-02`）的活，不是这一层的。

------------------------------------------------------------------------------
目录不留行：Tier0 只写文件
------------------------------------------------------------------------------

`on_entry` 收到的条目里**有目录**（`ingest.base.ScanEntry.is_dir`；扫描器的账目把它们
记在 `dirs_kept` 里，`ScanStats.entries_kept` 那句话的意思是"没有被规则剪掉"）。
Tier0 **不给目录写行**，三条理由：

1. `files` 里**没有"这一行是目录"这一列**——`schema.sql` 的 `files` 只有
   `size_bytes` / `mtime_ns` / `ext` / `mime` / `content_hash` / `blob_id` 这些
   **只有文件才有意义**的列。写进去的一行目录，与一个 0 字节的文件在库里
   **完全分不开**。
2. 于是 Tier1 会去提取它：`content_hash()` 在目录上抛 `IsADirectoryError`，被
   `_one_file` 当成"这个文件读不出来"记一笔失败。全盘上目录是百万级的，那就是
   百万条**全部是假的失败**（实测：一棵 4 个文件的小树上就冒出了 1 条）。
3. 目录名**照样搜得到**：`files_fts.path_tok` 存的是**完整路径**，所以
   `D:/work/报表/2026/x.md` 里的那一段 `报表` 会被搜到。真正丢掉的只有**空目录**
   （里面一个文件都没有的那种），而"一个不含任何文件的目录"本来也不是要搜的东西。

⚠ 代价与出路：`lsearch status`（`P6`）报的"文件总数"因此是**文件数**、不含目录；
真要连目录一起搜，得给 `files` 加一列 `is_dir`——那是 `schema.sql` 的改动，
`SCHEMA_VERSION` 要跟着动（"旧库不认识新列"由 `_require_schema()` 兜住）。

⚠ 同一件事还有一处**说法**上的偏差：`ScanStats.entries_kept`（`ingest/base.py`）的
注释是"要进索引的条数（正文 + 仅名字）"，它含 `dirs_kept`；按上面这一条，Tier0 实际
写进去的比那个数**少**。那句注释是扫描器的账（"没被规则剪掉"），而 `ingest/base.py`
是 `F2` 已通过的模块——改它的注释会让那一批测试按「绿灯不自动续期」回退成待复测，
所以这一趟**不动它**，改成把差额**显式记出来**（`Tier0Stats.dirs`，报表里印
"目录（不写行）"），让两个数在终端上对得上。两处一起记在 `PROGRESS.md` §4 P4「偏差」。

------------------------------------------------------------------------------
`parser_ver`：谁组装、包里装什么
------------------------------------------------------------------------------

`put_blob()` 的 `parser_ver` **没有默认值**是刻意的（它的文档里写着理由），取值由这里
组装：`parse:<配方号>|chunk:<切法>`。两段都有必要，因为"该重切一遍"有两个来源：

* **提取逻辑变了**（换了库、改了某个格式的解析、`P0` 那类前缀策略的调整）——靠
  `PARSER_RECIPE`。它是个**手改**的整数：改提取逻辑的人要顺手把它加一。
  ⚠ 忘了加的症状是**旧切片永远留在库里**：新格式的正文搜不到，而旧的那一份还在，
  于是"这个文件有内容、就是搜不出来"，不报错。
* **切法变了**（`target_tokens` / `overlap_ratio` / `max_chunks_per_doc`）——靠第二段。
  它们进 `parser_ver` 而不是只进配置，是因为切片**已经按旧参数写进库里了**：
  参数一变，那些切片与"现在的配置"就对不上，而没有任何一趟会回头重切它们
  （`fulltext` 的行不在 Tier1 的选择集里）。

于是 `lsearch index --max-chunks-per-doc 10` 这一趟写下的 blob 会带一个不同的版本，
下一次不带这个参数的运行走到同一个内容时会走 `put_blob()` 的 `replaced` 分支
（删旧切片、按新参数重切）。这是**对的**：那一份切片确实是按别的参数切的。

------------------------------------------------------------------------------
`A9`：先问"这份内容见过没有"，再决定要不要读盘提取
------------------------------------------------------------------------------

`A9` 的字面要求是"同一份内容只**提取**与嵌入一次"。只靠 `put_blob()` 的 `reused` 做
不到前半个：走到 `reused` 的时候文件已经读进来、正文已经解析出来了，第二次提取白做。
所以顺序是：

1. 算这份内容的 sha256（`ingest/fingerprint.content_hash`，读一遍字节）；
2. `store.find_blob(hash, parser_ver)`——**只读地问一句**"这份内容、这个版本，库里
   有现成的切片吗"；
3. 有（`reused`）→ **跳过提取**，直接把这一行记成 `fulltext` 并指向那个 blob。
   它的切片已经在库里（同一个 blob 共享），所以这一行**确实**可搜——写 `fulltext`
   不是在说谎；
4. 没有 → 提取、切片、`put_blob()` + `put_chunks()`。

⚠ 步骤 1 是**每个被提取的文件都要多读一遍字节**。计划 §4.4.1 原本要用"只对 size 有
兄弟的文件算哈希"来省掉这一遍，那一层**没有落地**，理由（为什么它不能按原样落地）
写在 `PROGRESS.md` §4 P4「偏差」里。今天付的是 `E19` 那个读数：小文件 7 MiB/s、
2.87 ms/个，与解析一遍同量级——**这是 `A9` 的价格，不是白花的**。

⚠ `doc.truncated`（提取器到了 `max_bytes` 上限）与 `doc.warnings` 在 `files` 那张表里
**没有落点**（没有对应的列），所以只在这一趟的报表里计数。要能查出来得等 `P6`。

------------------------------------------------------------------------------
Tier2：先存索引、后提交 SQL（`F5-04`）
------------------------------------------------------------------------------

`run_tier2` 每一批的最后三句是 `index.save()` → `set_meta()` → `store.commit()`，
顺序**不能动**。两个落点（HNSW 文件 / SQLite）分别落盘，中间那个窗口各自留一种
残留，而两种的后果不对称（`VectorIndex.save()` 的文档算了这笔账）：

* **先存索引**：崩在中间 ⇒ 索引里多了这批向量、库里没有记录。它们成了**孤儿**——
  浪费一点磁盘，`next_label_hint()` 保证它们的号不被重发。**能接着跑。**
* **先提交 SQL**：崩在中间 ⇒ `chunks.vec_label` 指着一个索引里不存在的位置，而
  `F5-03` 的缓存语义是"命中了就不再嵌"——那批切片**永远**拿不到向量，每次都少几条
  结果，且不报错。唯一能发现它的是下一次打开时的 `check_consistency()`（它当场抛）。

同一个理由决定了 `--rebuild` 的写法：**删索引文件**与**清库里的缓存/label/状态**
必须成对，前者由 `VectorIndex.open(fresh=True)` 做、后者由
`reset_vector_state()` 在一个事务里做。少做任何一半，"重建"都只对了一半。

⚠ `save()` 只在真的加了向量时才做。它写的是**整个文件**，而 `faiss.write_index` 是
O(索引总大小)——全命中缓存的一批（一个字节都没往索引里加）也存一遍，在 `A1` 那套
两万个文件的规模上就是"每 500 个文件重写一遍几 GB"。

------------------------------------------------------------------------------
Tier2：`title=None`——切片属于内容，不属于路径
------------------------------------------------------------------------------

`document_key()` 的第二个参数（标题）在 Tier2 这一趟**一律是 `None`**，而 Tier1 是
拿 `doc.title` 切片、`D12` 又把同一份内容的切片挂在**一个 blob** 上。理由是同一条：
同一个 blob 可以被好几个路径指着（`paths_for_blob()`），而标题是**路径**的属性
（文件名、文档属性）。按路径各算一次键，`D12` 那套去重的收益就整个消失——同一份
内容有几个副本就嵌几遍。代价是切片级的文档前缀里没有标题（查询侧仍然有它自己的
查询前缀，两边本来就非对称）。记在 `PROGRESS.md` §5 P5「偏差」里。

------------------------------------------------------------------------------
这一层**不做**的两件事
------------------------------------------------------------------------------

* `--prune`（剔除失效条目）要"按路径删行"，而 `files_fts` 的清理还没有主人
  （`P4` 遗留第 8 条），外加 `R14` 那套"只在本次真正可达的卷内删"的判据——那是
  `F6-03` 的活。
* **Tier1 的 `--workers`**：`--workers` 说的是**嵌入**的并行（`A2`），而 Tier1 这一段
  根本不嵌东西，它是单进程顺序跑（读盘、解析、切片）。所以那个开关在 `F5-04` 之后
  只对 `--tier 2` 有意义，而它由 `run_tier2()` 转手给 `EmbedPool`。

两件事都记在 `PROGRESS.md` §4 P4「偏差」里，不在代码里留半个开关。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from localsearch.chunk.sliding import split
from localsearch.core.config import ChunkCfg, Config
from localsearch.embed.base import TaskPrefixes, embed_ver, resolve_prefixes
from localsearch.embed.batching import EmbedPool, iter_requests
from localsearch.extract import (
    ExtractConfig,
    Extractor,
    build_registry,
    extract_many,
    ext_of,
    mime_of,
)
from localsearch.extract.batch import (
    STATUS_ERROR,
    STATUS_FULLTEXT,
    STATUS_SKIPPED,
    Tally,
    one_line,
)
from localsearch.ingest.base import ScanEntry, ScanStats, Scanner
from localsearch.ingest.filters import Action, IgnoreRules
from localsearch.ingest.fingerprint import content_hash
from localsearch.index.sqlite_store import (
    BLOB_CREATED,
    ChunkText,
    FileRow,
    SqliteStore,
)
from localsearch.index.tier import TIER_FULLTEXT, TIER_METADATA, TIER_SEMANTIC
from localsearch.index.vector_cache import VectorCache, document_key
from localsearch.index.vectors_faiss import (
    INDEX_FILENAME,
    META_EMBED_VER,
    META_NEXT_LABEL,
    VectorIndex,
    VectorSpec,
    bind_embed_ver,
    check_consistency,
    next_label,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不需要 core.registry
    from localsearch.core.registry import Registry

__all__ = [
    "PARSER_RECIPE",
    "STATUS_METADATA",
    "BuildError",
    "BuildResult",
    "Tier0Stats",
    "Tier1Stats",
    "Tier2Stats",
    "parser_ver",
    "run",
    "run_tier0",
    "run_tier1",
    "run_tier2",
    "tier0_status",
]

#: 提取逻辑的**配方号**。改了任何影响"正文长什么样"的实现之后，把它加一。
#:
#: ⚠ **没有测试能替你记得这件事**——这是有意的取舍（另一条路是拿包版本号当版本，
#: 而那会让每一次与提取无关的提交都触发全库重切）。忘了加的症状写在模块文档里：
#: 旧切片留着、新正文搜不到，**且不报错**。
PARSER_RECIPE = 1

#: Tier0 做完、等着提正文的那些文件记成什么 `files.status`。
#:
#: 字面量而不是从 `tier.py` 导入的常量：`tier.py` 的 `STATUSES` 是**词表**（取值域），
#: 它没有为每个取值定义一个名字。写错一个字不会静默——`FileRow.__post_init__` 会
#: 当场拿词表校验（这正是那个校验存在的理由）。
STATUS_METADATA = "metadata"

#: 这一趟的进度回调：每提交一批调一次，参数是**这一趟到目前为止的账**
#: （`Tier0Stats` 或 `Tier1Stats`）。CLI 用它打进度行。
#:
#: 回调是"批"而不是"文件"粒度：全盘 100 万行、每行回一次调，就等于把打印的代价
#: 加在写库的循环里。而"还剩多少"这个问题也不需要精确到个位。
Progress = Callable[[object], None]


class BuildError(Exception):
    """这一趟**开不起来**或**走下去会出错**。

    与 `extract.ParseFailed` 一类**单文件**的错误分工明确：那些是"这个文件坏了"，
    一个文件一个结局、进 `files.error_msg`，**不打断整轮**（`A6`）；而这一层抛出来的
    都是"再跑下去每一步都会是错的"——库的 schema 不认识、`--resume` 接不上、
    一个批次的写入一行都没生效。宁可停在半路（已经提交的批次是完好的），也不要
    让它继续跑出一个**看起来正常**的库。
    """


# ==============================================================================
# 账
# ==============================================================================


@dataclass(slots=True)
class Tier0Stats:
    """Tier0 那一趟的账。`scan` 是扫描器自己的计数（见 `ingest.base.ScanStats`）。"""

    scan: ScanStats = field(default_factory=ScanStats)
    #: 这一趟**新写进** `files` 的行数（`put_file()` 返回真）。
    new_rows: int = 0
    #: 库里已经有、这一趟**一个字都没改**的行数。全量索引跑第二遍时它等于全部。
    known_rows: int = 0
    #: 其中记成 `skipped` 的（超 `max_file_mb`、只记名字的那些）。
    name_only: int = 0
    #: 这一趟**没有写行**的目录数（理由见模块文档那一节）。它不参与 `kept`：
    #: `kept` 是"写进索引的条目数"，而目录没进去。
    dirs: int = 0
    commits: int = 0
    seconds: float = 0.0

    @property
    def kept(self) -> int:
        """这一趟写进索引的条目数（含只记名字的）。"""
        return self.new_rows + self.known_rows


@dataclass(slots=True)
class Tier1Stats:
    """Tier1 那一趟的账。"""

    #: 这一趟从库里挑出来的行数（= 参与这一趟的文件数）。
    work: int = 0
    #: 提取的账：`total` 是**真正跑过提取**的文件数，`fulltext` / `skipped` /
    #: `error` / `unexpected` 的语义与分工见 `extract/batch.py` 的模块文档。
    tally: Tally = field(default_factory=Tally)
    #: `A9` 的那一格：内容已经在库里（同一个 sha256、同一个 `parser_ver`），
    #: **这一趟没有读盘提取**。
    reused: int = 0
    #: `blobs` 的三条下落（`put_blob()` 的 `action`）。
    blobs_created: int = 0
    blobs_replaced: int = 0
    #: 写进 `chunks` 的切片数（复用的那些不在内——它们的切片早就写好了）。
    chunks_written: int = 0
    #: 正文被提取器截断（到了 `max_bytes`）的文件数。**只计数，不落库**——见模块文档。
    truncated: int = 0
    #: 连读都读不出来（算 sha256 时 `OSError`）的文件数。单独一栏而不是混进
    #: `tally.error`：`tally` 数的是**提取**的结局，而这些文件连提取都没开始。
    hash_errors: int = 0
    commits: int = 0
    seconds: float = 0.0

    @property
    def error(self) -> int:
        """这一趟失败的文件数（提取失败 + 读不出来）。"""
        return self.tally.error + self.hash_errors

    @property
    def has_failures(self) -> bool:
        """「跑完了，但有文件没成」——`P6` 的启动器靠它把退出码归一（计划 §4.8）。

        ⚠ 它**不是**"这一趟失败了"：`A6` 要的就是"单个文件出事不中断整轮"，所以
        这一类轮次算**成功**（退出码 1，不是 ≥2），否则 `<RestartOnFailure>` 会
        把"语料里有几个坏文件"当成崩溃反复重启。
        """
        return self.error > 0


@dataclass(slots=True)
class Tier2Stats:
    r"""Tier2 那一趟的账（`F5-04`）。

    ⚠ **这一趟没有"失败的文件"这一栏**，与 Tier0 / Tier1 都不同：Tier1 的失败是
    "这个文件的正文读不出来"（一个文件一个结局，`A6` 要求不中断整轮），而 Tier2
    手上只有**已经在库里的切片**——读盘、解析、切片全都做完了。嵌不出来是
    **整机级**的事（模型不在、内存不够），它该当场抛、由调用方报错退出，
    而不是记成某个文件的失败。所以《失败》那一栏在这一趟的报表里不该出现。

    四组数，**两种口径不要混**（同 `CachePlan` 的文档）：

    * 按**文件**：`work`、`files_indexed`、`no_blob`；
    * 按**切片**：`chunks`（这批参与的片数，含重复文本）、`embedded`（真正嵌的）、
      `cached`（靠缓存与批内去重省下的）；⚠ `cached` **不等于** `chunks - embedded`
      之外的任何一种拆法，它是 `CachePlan.saved` 的和，已经含了批内去重。
    """

    #: 这一趟从库里挑出来的文件行数。
    work: int = 0
    #: 参与这一趟的切片数（**按片记**，同一段文本重复出现会重复计数）。
    chunks: int = 0
    #: 真正送进嵌入器的**唯一文本**条数（去重 + 命中缓存之后剩下的）。
    embedded: int = 0
    #: 靠缓存命中与批内去重**少嵌**的条数（`A9` 落在这里，见 `CachePlan.saved`）。
    cached: int = 0
    #: 记成 `indexed` 的文件数（Tier2 唯一的成功结局）。
    files_indexed: int = 0
    #: 没有任何切片的文件数（0 字节文件、或者提取出的正文为空）。它们照样算做完：
    #: 没有文本就没有向量，这一级对它们是**空真**。单列一栏是因为"这批文件一条
    #: 向量都没产出"有两个成因，混在一起会让人以为嵌入器漏了东西。
    empty: int = 0
    #: `blob_id` 是 NULL 的行数——它们连切片都挂不上（不正常，但库是可以手改的）。
    #: 单独记，因为把它们静默当成"做完了"就等于把"这行坏了"藏起来。
    no_blob: int = 0
    #: 打开索引时读到的孤儿向量数（索引里有、库里没有哪一行指着）。
    #: 它由"先存索引、后提交 SQL"这个**刻意**的顺序造出来，无害，但要说出来。
    orphans: int = 0
    #: 开这一趟之前 / 之后索引里的向量条数。
    ntotal_before: int = 0
    ntotal_after: int = 0
    #: `--rebuild` 清掉了多少（`VectorReset` 的三个数）。没重建时三个都是 0。
    reset_cached: int = 0
    reset_chunks: int = 0
    reset_files: int = 0
    #: 实际开了几个嵌入 worker，以及内存算出来的那一份说明（`WorkerPlan.notes`）。
    workers: int = 0
    worker_notes: tuple[str, ...] = ()
    #: 某个 worker 自报的峰值常驻内存（`E8` 同口径的观测值）。
    observed_peak_bytes: int | None = None
    commits: int = 0
    seconds: float = 0.0

    @property
    def added(self) -> int:
        """这一趟往索引里加了几条向量。"""
        return self.ntotal_after - self.ntotal_before


@dataclass(slots=True)
class BuildResult:
    """一趟 `lsearch index` 的完整账。`tier0` 为 `None` 表示这一趟没扫盘（`--resume`）。"""

    target_tier: int
    tier0: Tier0Stats | None = None
    tier1: Tier1Stats | None = None
    tier2: Tier2Stats | None = None
    seconds: float = 0.0

    @property
    def has_failures(self) -> bool:
        """这一趟有没有"没成"的文件（扫描读不到 + 提取失败）。"""
        scan_errors = self.tier0.scan.errors_total if self.tier0 is not None else 0
        return scan_errors > 0 or (self.tier1 is not None and self.tier1.has_failures)


# ==============================================================================
# parser_ver
# ==============================================================================


def parser_ver(chunk: ChunkCfg) -> str:
    """这一趟写下的 blob 该带哪个 `parser_ver`（组装理由见模块文档）。

    形如 `parse:1|chunk:512:0.15:200`。⚠ **参数顺序与 `ChunkCfg` 的字段顺序一致**，
    因为它是拼出来的字符串：换顺序会造出一个新的值，于是全库的 blob 都会走一遍
    `replaced`（重切 + 重嵌）。那不是危险，是浪费——但浪费得没有任何提示。
    """
    return (
        f"parse:{PARSER_RECIPE}"
        f"|chunk:{chunk.target_tokens}:{chunk.overlap_ratio:g}:{chunk.max_chunks_per_doc}"
    )


def tier0_status(action: Action) -> str:
    """扫描器留下的条目在 `files` 里记成什么 `status`（两种情形的理由见模块文档）。"""
    if action is Action.INDEX:
        return STATUS_METADATA
    if action is Action.NAME_ONLY:
        return STATUS_SKIPPED
    # 扫描器的 on_entry 只收留着的条目（ingest/base.py 的 Scanner 协议写明了这一条）。
    # 走到这里说明那条约定被改了——而它的失败形态是"被规则排除的文件也进了索引"，
    # 那是这一层能犯的最贵的错（全盘上被排除的往往正是最不该读的那部分）。
    raise BuildError(
        f"扫描器把 action={action!r} 的条目交给了 on_entry；它只该交 "
        f"{Action.INDEX!r} 与 {Action.NAME_ONLY!r}"
    )


# ==============================================================================
# Tier0
# ==============================================================================


def _stamp(now: int | None) -> int:
    """这一趟的时间戳（秒）。`now` 可传是为了测试能把它钉死。

    ⚠ **秒**：`files.indexed_at` 与 `mtime_ns` 回答的是两件事，后者的纳秒是为了
    比"变没变"，前者是"这一趟什么时候跑的"。
    """
    return int(time.time()) if now is None else int(now)


def run_tier0(
    store: SqliteStore,
    *,
    scanner: Scanner,
    rules: IgnoreRules,
    every: int,
    now: int | None = None,
    on_progress: Progress | None = None,
) -> Tier0Stats:
    r"""扫一遍盘，把留着的条目写进 `files` + `files_fts`。**碰到 `every` 条提交一次。**

    `scanner` / `rules` 由调用方造好（`make_scanner()` 与 `IgnoreRules`）：这一层不
    知道 `--root` / `--exclude` 这些命令行参数，也不该知道——"哪些根、哪些规则"是
    命令面的事，而"按什么顺序写库、什么时候提交"是这里的事。

    `now` 是**整趟共用的一个时刻**（缺省取当前时间）：`files.indexed_at` 回答的是
    "这一趟是什么时候跑的"，一个扫了三小时的盘不需要 100 万个各不相同的时间戳。
    一次 `time.time()` 也比 100 万次便宜。

    ⚠ **`put_file()` 的返回值分两种，账也要分两栏记**（`new_rows` / `known_rows`）：
    全量索引跑第二遍时 `known_rows` 等于全部、`new_rows` 是 0，那是**正常**的
    （Tier0 是幂等的，`INSERT OR IGNORE` 一个字都不改）。把它读成"什么都没做"、
    然后去"修"它，就会得到一个每跑一遍都要改写全库的实现。
    """
    stats = Tier0Stats()
    stamp = _stamp(now)
    started = time.monotonic()
    since_commit = 0

    def on_entry(entry: ScanEntry) -> None:
        nonlocal since_commit
        if entry.is_dir:
            # 目录不留行（理由见模块文档那一节）。**先算进账再返回**：这个数要与
            # 报表里"保留的目录"对得上，不然那两行数字会差出一大截而没人解释。
            stats.dirs += 1
            return
        status = tier0_status(entry.action)
        row = FileRow(
            path=entry.path,
            size_bytes=entry.size,
            mtime_ns=entry.mtime_ns,
            status=status,
            tier=TIER_METADATA,
            ext=ext_of(entry.path),
            # 猜不出来的给 NULL 而不是空串：`files.mime` 的"没猜出来"与"猜出来是空"
            # 在报表里要能分开（空串会印成一个空单元格，与 NULL 看不出区别）。
            mime=mime_of(entry.path) or None,
            # ⚠ `quick_sig` 一律留 NULL：填它要给 `ScanEntry` 加 `ctime_ns`，
            # 归 F6-02（见 schema.sql 里那一行注释）。
            quick_sig=None,
            # MFT 后端才有文件记录号；scandir 侧是 0，落 NULL 而不是 0
            # （0 是一个真实的记录号：$MFT 自己）。
            frn=entry.frn or None,
            indexed_at=stamp,
        )
        if store.put_file(row):
            stats.new_rows += 1
        else:
            stats.known_rows += 1
        if status == STATUS_SKIPPED:
            stats.name_only += 1
        since_commit += 1
        if since_commit >= every:
            store.commit()
            stats.commits += 1
            since_commit = 0
            if on_progress is not None:
                on_progress(stats)

    stats.scan = scanner.scan(rules=rules, on_entry=on_entry)
    # 收尾提交：最后不足一批的那些**必须**提交，否则它们要等 `close()` 的兜底——
    # 而兜底只在正常退出时才发生，"跑完了但最后几百个文件没进库"就是从这里来的。
    store.commit()
    stats.commits += 1
    stats.seconds = time.monotonic() - started
    return stats


# ==============================================================================
# Tier1
# ==============================================================================


def run_tier1(
    store: SqliteStore,
    *,
    registry: Registry[Extractor],
    extract_config: ExtractConfig,
    chunk: ChunkCfg,
    every: int,
    on_progress: Progress | None = None,
) -> Tier1Stats:
    r"""把库里还没做正文的行逐个做完。**每 `every` 个提交一次。**

    循环的形态是"取一批 → 做完 → 提交"，而不是"一个生成器从头到尾边遍历边写"。后者
    看起来更省事，代价是一条**不报错的错**：`iter_tier_work()` 的选择条件压在
    `idx_files_tier` 上，而这一趟要改的正是 `status` / `tier` 那两列——**在扫描一个
    索引的同时改它的键**，SQLite 的文档把结果定义为"未指定"。所以每一批先把行取完、
    把游标关掉（见 `_take_batch`），再动库。

    每一批都**重新问一次**库（而不是从一个长游标里往下读）：做过的行已经退出选择集，
    于是"下一批"自然就是"还没做的那些里 id 最小的那一批"。这也正是 `--resume` 与
    普通重跑共用的那条性质——**同一个循环，从库里挑活**，没有任何进度文件。
    """
    stats = Tier1Stats()
    ver = parser_ver(chunk)
    chunk_kwargs = asdict(chunk)
    started = time.monotonic()
    previous_first: int | None = None

    while True:
        batch = _take_batch(store, TIER_FULLTEXT, every)
        if not batch:
            break
        first = batch[0].id
        if first is not None and first == previous_first:
            # 上一批的第一行还在最前面：它没被写掉。再转一圈还是这一批，这个循环
            # 就永远不会结束——而死循环不抛异常，只会把机器占住。
            raise BuildError(
                f"这一批没有推进：id={first} 上一批就在最前面。它的结局没有写进库"
                f"（`set_file_extracted()` 改了 0 行？），继续跑会是一个死循环。"
            )
        previous_first = first

        for row in batch:
            stats.work += 1
            _one_file(
                store,
                row,
                registry=registry,
                extract_config=extract_config,
                ver=ver,
                chunk_kwargs=chunk_kwargs,
                stats=stats,
            )
        store.commit()
        stats.commits += 1
        if on_progress is not None:
            on_progress(stats)

    stats.seconds = time.monotonic() - started
    return stats


def _take_batch(store: SqliteStore, tier: int, size: int) -> list[FileRow]:
    """取一批要做的行，**取完就把游标关掉**（理由见 `run_tier1` 的文档）。

    `size` 由 `perf.commit_every_files` 给：批次大小与提交粒度是同一个数，多一个
    旋钮只会多一种"提交比批次小"的错法（那种错法不报错，只是白提交）。
    """
    rows = store.iter_tier_work(tier)
    try:
        return list(islice(rows, size))
    finally:
        rows.close()


def _one_file(
    store: SqliteStore,
    row: FileRow,
    *,
    registry: Registry[Extractor],
    extract_config: ExtractConfig,
    ver: str,
    chunk_kwargs: dict[str, object],
    stats: Tier1Stats,
) -> None:
    r"""一个文件走完 Tier1。**失败也一定写回一行结局**（`A6` 的后半句）。

    顺序（理由见模块文档）：算哈希 → 问库里有没有现成的 → 有就跳过提取 → 没有就提取
    切片写库 → 把结局写回 `files`。四步全在同一个事务里（提交归 `run_tier1`）。
    """
    path = row.path
    try:
        digest = content_hash(path)
    except OSError as exc:
        # 读不出来（权限、文件在扫描与提取之间被删、磁盘掉线）。**不当成崩溃**：
        # 它是"这一个文件没成"，与提取失败同一个档，写原因、接着跑下一个。
        store.set_file_extracted(
            path, status=STATUS_ERROR, tier=TIER_METADATA, error_msg=one_line(_reason(exc))
        )
        stats.hash_errors += 1
        return

    known = store.find_blob(digest, parser_ver=ver)
    if known is not None:
        # `A9`：这份内容（同一个 sha256、同一个 parser_ver）的切片已经在库里了，
        # 它属于同一个 blob 的行——**这一行的正文确实可搜**，所以记 fulltext 不是谎话。
        store.set_file_extracted(
            path, status=STATUS_FULLTEXT, tier=TIER_FULLTEXT, content_hash=digest, blob_id=known
        )
        stats.reused += 1
        return

    outcome = next(extract_many((path,), registry=registry, config=extract_config))
    stats.tally.add(outcome)
    if not outcome.ok:
        # `skipped`（这一类不做）与 `error`（这个文件坏了）走同一条路：都没有正文、
        # 都没有 blob，都要把结局写回去——不写的话下一趟还会再挑它一次。
        store.set_file_extracted(
            path,
            status=outcome.status,
            tier=TIER_METADATA,
            # 空串归一成 NULL 由 `set_file_extracted()` 做（它拿非空的 error_msg
            # 当"全库唯一的失败判据"，见那里的文档）。
            error_msg=outcome.error_msg or None,
        )
        return

    doc = outcome.doc
    assert doc is not None  # outcome.ok 就是 doc is not None
    if doc.truncated:
        stats.truncated += 1
    chunks = split(doc.text, **chunk_kwargs)  # type: ignore[arg-type]
    write = store.put_blob(
        content_hash=digest,
        # 用**扫描时**记下的字节数。文件在扫描与提取之间被改过的话这个数会偏，
        # 而偏的是"这一份内容有多大"这一个信息项，不影响键与切片——
        # 那种情形归 F6-02 的变更检测，不在这里猜。
        size_bytes=row.size_bytes,
        extractor=doc.source,
        parser_ver=ver,
    )
    if not write.needs_chunks:
        # 刚才 `find_blob()` 说没有、`put_blob()` 说复用：两个判据漂了（它们本该是
        # 同一个 WHERE，见 `find_blob()` 的文档）。这时候跳过 `put_chunks()` 会在库里
        # 留下一行**声称有正文、其实一片都没有**的记录——搜不到，且不报错。
        raise BuildError(
            f"find_blob() 与 put_blob() 的判断不一致：{path} 的内容 {digest} 在 "
            f"parser_ver={ver!r} 下先是「没有」、又是「复用」。这两个方法的 WHERE "
            f"必须一致（find_blob 的文档里写着这条耦合）。"
        )
    stats.chunks_written += store.put_chunks(
        write.blob_id, chunks, title=doc.title, modality=doc.modality
    )
    if write.action == BLOB_CREATED:
        stats.blobs_created += 1
    else:
        stats.blobs_replaced += 1
    store.set_file_extracted(
        path,
        status=STATUS_FULLTEXT,
        tier=TIER_FULLTEXT,
        content_hash=digest,
        blob_id=write.blob_id,
    )


def _reason(exc: BaseException) -> str:
    """`files.error_msg` 的原始形态 `"<异常类名>: <消息>"`（折行与截断由 `one_line` 做）。

    与 `extract/batch.py` 的 `Outcome.error_msg` 同一个形状，理由也同一个：报表要能按
    类分堆。这一条是**这一层唯一**自己造错误消息的地方（提取那一路的消息由 batch 造）。
    """
    return f"{type(exc).__name__}: {exc}"


# ==============================================================================
# Tier2
# ==============================================================================


def run_tier2(
    store: SqliteStore,
    *,
    config: Config,
    every: int,
    prefixes: TaskPrefixes | None = None,
    pool: EmbedPool | None = None,
    workers: int = 0,
    rebuild: bool = False,
    index_path: str | Path | None = None,
    on_progress: Progress | None = None,
) -> Tier2Stats:
    r"""把库里还没做向量的切片嵌完，写进 HNSW（`F5-04`）。

    循环形态与 `run_tier1` **完全一样**（取一批 → 做完 → 提交），理由也一样：这一趟
    改的正是 `tier` / `status` 那两列，而 `iter_tier_work()` 的选择条件压在
    `idx_files_tier` 上——**在扫一个索引的同时改它的键**是 SQLite 明说"结果未指定"
    的那种事。所以每一批都重新问一次库。

    一批之内的顺序是固定的，每一处都对应一个**不报错**的错法：

    1. `store.chunks_for_blobs()`——一批**一条 `SELECT`**（不是一个文件一条）。
    2. `document_key(prefixes, text)`——缓存键按**喂给模型的那串字符**算。`title=None`：
       切片属于 **blob**（`D12` 的去重让好几个路径共用一份切片），而标题是**路径**的
       属性——按路径各算一次键会让去重的收益整个消失，见「与计划的偏差」。
    3. `cache.plan(keys)`——**先算完"哪些要嵌"再动手**（`F5-03`）。
    4. 只把 `plan.todo` 喂给 `EmbedPool`，按 `BatchResult.index` 归位。
    5. `index.add(vectors, labels)`；label 由 `meta` 的计数器发（`next_label()`，
       并与索引文件里的最大号取大——见那里的文档）。
    6. `cache.record(plan, labels)` → `plan.labels_in_order()` → 写 `chunks.vec_label`。
    7. `set_file_indexed()` 把这一批的文件记成 `indexed`。
    8. **`index.save()` → `set_meta()` → `store.commit()`**：先存索引、后提交。
       反过来的那个窗口留下的是"库里记着 label、索引里没有"，而 `F5-03` 的缓存语义是
       "命中了就不再嵌"——那批切片会**永远**拿不到向量，每次都少几条结果，不报错
       （`VectorIndex.save()` 的文档把两个窗口的账算过）。

    `prefixes` / `pool` / `index_path` 可传是为了测试（不给就按配置现场解析/装配）。
    ⚠ 两者都是**懒的**：手上没活时既不加载模型、也不去解析快照——一个没什么可做的
    `--tier 2` 不该先花十几秒把 1.81 GiB 的权重读进来。

    `rebuild=True` 是"丢掉已有那一层重来"：它先把库里的缓存 / label / 状态清干净
    （`reset_vector_state()`，三件事一个事务），再把索引文件**删掉重建**。⚠ 这两个
    动作必须**成对**（`VectorCache.clear()` 的文档里写着为什么：缓存说有、索引里没有，
    会让那批切片永远不再被嵌）。所以 `--rebuild` 是本模块**唯一**有权 `fresh=True`
    的地方。
    """
    stats = Tier2Stats()
    started = time.monotonic()
    cfg = config.embed
    ver = embed_ver(cfg)
    spec = VectorSpec.from_config(config)
    path = (
        Path(index_path)
        if index_path is not None
        else config.index_dir_path() / INDEX_FILENAME
    )

    if rebuild:
        reset = store.reset_vector_state(embed_ver=ver)
        stats.reset_cached = reset.cached
        stats.reset_chunks = reset.chunks
        stats.reset_files = reset.files
    index = VectorIndex.open(path, spec, fresh=rebuild)
    if rebuild:
        # 索引是空的、库也清干净了，这一版就是它。**有意不走 `bind_embed_ver()`**：
        # 它的第三种情形（库里记的是另一版模型）正是"换了模型"，而重建的**全部意义**
        # 就是允许换——旧向量一条不剩，没有可混的东西。走它的话，想换模型的用户会
        # 被一句"要换模型就得重建"挡住，而他现在做的恰恰就是重建。
        store.set_meta(META_EMBED_VER, ver)
    else:
        bind_embed_ver(store, index, embed_ver=ver)
    cache = VectorCache(store, ver)
    stats.orphans = check_consistency(index, cache)
    stats.ntotal_before = index.ntotal
    label = next_label(store, hint=index.next_label_hint())
    previous_first: int | None = None
    own_pool = pool is None

    try:
        while True:
            batch = _take_batch(store, TIER_SEMANTIC, every)
            if not batch:
                break
            first = batch[0].id
            if first is not None and first == previous_first:
                # 与 `run_tier1` 同一条守卫：上一批的第一行没被写掉，再转一圈还是它。
                # 死循环不抛异常，只会把机器占住。
                raise BuildError(
                    f"这一批没有推进：id={first} 上一批就在最前面。它的结局没有写进库"
                    f"（`set_file_indexed()` 改了 0 行？），继续跑会是一个死循环。"
                )
            previous_first = first

            dirty = False
            pieces: list[ChunkText] = []
            for row in batch:
                stats.work += 1
                if row.blob_id is None:
                    # 一个 `fulltext` 的行本该总有 blob 的。走到这里说明那一行坏了
                    # （手改过库）。**记一笔再让它离开选择集**：不写结局的话它每一趟
                    # 都会被挑中，而"每趟都做、每趟都没做"看起来与"做完了"一样。
                    store.set_file_indexed(row.path)
                    stats.no_blob += 1
                    stats.files_indexed += 1
                    continue
                blob_chunks = store.chunks_for_blobs((row.blob_id,)).get(row.blob_id, ())
                if blob_chunks:
                    pieces.extend(blob_chunks)
                else:
                    # 0 字节文件 / 正文为空：没有文本就没有向量，这一级对它是**空真**。
                    stats.empty += 1
                store.set_file_indexed(row.path)
                stats.files_indexed += 1

            stats.chunks += len(pieces)
            if pieces:
                if prefixes is None:
                    prefixes = resolve_prefixes(cfg)
                keys = [document_key(prefixes, piece.text) for piece in pieces]
                plan = cache.plan(keys)
                stats.cached += plan.saved
                stats.embedded += len(plan.todo)
                labels: list[int] = []
                if plan.todo:
                    dirty = True
                    if pool is None:
                        pool = EmbedPool(cfg, workers=workers)
                        stats.workers = pool.workers
                        stats.worker_notes = tuple(pool.notes)
                    vectors = _embed_todo(
                        pool,
                        plan.todo,
                        pieces,
                        keys,
                        dim=cfg.dim,
                        batch_size=cfg.batch_size,
                    )
                    labels = [label + i for i in range(len(plan.todo))]
                    index.add(vectors, labels)
                    label += len(labels)
                # 新嵌的那批进 `embed_cache`（`plan.record()` 顺带把"键 → label"记在
                # `plan` 上）。`todo` 为空时 `record([])` 是**合法**的，不必先判一次
                # （`CachePlan.recorded` 的文档写着为什么）。
                cache.record(plan, labels)
                ordered = plan.labels_in_order()
                store.set_chunk_vectors(
                    [
                        (piece.chunk_id, lbl)
                        for piece, lbl in zip(pieces, ordered, strict=True)
                    ],
                    embed_ver=ver,
                )

            # 顺序固定：先索引落盘，再提交 SQL（`VectorIndex.save()` 的文档里算了两个
            # 窗口的账）。`set_meta()` 与 commit 挨着：计数器与"向量进了索引"必须用
            # 同一条顺序推进。
            #
            # ⚠ `save()` **只在真的加了向量时**才做：它写的是**整个文件**，而
            # `faiss.write_index` 是 O(索引总大小)——全命中缓存的一批（一个字节都没
            # 往索引里加）也存一遍，在 `A1` 那套两万个文件的规模上就是"每 500 个文件
            # 重写一遍几 GB"。而这一趟的瓶颈本该只在嵌入器上。
            if dirty:
                index.save()
                store.set_meta(META_NEXT_LABEL, str(label))
            store.commit()
            stats.commits += 1
            if on_progress is not None:
                on_progress(stats)
    except BaseException:
        # 嵌入或写库中途抛错：这一批还没提交的结局（`set_file_indexed` 等）整体撤回，
        # 否则下面 `finally` 的 `close()` 会把它们当成"做完了"落盘（见上面的说明）。
        store.rollback()
        raise
    finally:
        if pool is not None:
            stats.workers = pool.workers
            stats.worker_notes = tuple(pool.notes)
            stats.observed_peak_bytes = pool.observed_peak_bytes
            if own_pool:
                pool.close()
        stats.ntotal_after = index.ntotal
        index.close()
        stats.seconds = time.monotonic() - started
    return stats


def _embed_todo(
    pool: EmbedPool,
    todo: Sequence[str],
    pieces: Sequence[ChunkText],
    keys: Sequence[str],
    *,
    dim: int,
    batch_size: int,
) -> np.ndarray:
    r"""把 `plan.todo` 里的唯一文本嵌完，返回 `(len(todo), dim)` 的矩阵。

    ⚠ **`todo` 是键、`pieces` 是切片**，两者不是一回事（一批里同一段文本出现三次时
    `todo` 只有一条）。所以这里必须先按**首次出现的那个键**把文本捡出来，
    `text_of[key]` 那一步不能省：直接把 `pieces[:len(todo)]` 当作输入，等于把"去重
    之后的第 i 条"错认成"原始的第 i 条"，而它的后果是**每一条切片都挂上别人的向量**
    ——搜索结果看着还挺像那么回事（`CachePlan.record()` 的文档串点了同一件事）。

    结果按 `BatchResult.index` 归位（并行走完回来是乱序的，见 `EmbedPool`）。
    """
    text_of: dict[str, str] = {}
    for key, piece in zip(keys, pieces, strict=True):
        text_of.setdefault(key, piece.text)
    vectors = np.zeros((len(todo), dim), dtype=np.float32)
    for result in pool.embed_documents(iter_requests(list(todo), size=batch_size)):
        start = result.index * batch_size
        vectors[start : start + len(result.vectors)] = result.vectors
    return vectors


# ==============================================================================
# 一趟
# ==============================================================================


def run(
    store: SqliteStore,
    *,
    scanner: Scanner,
    rules: IgnoreRules,
    config: Config,
    target_tier: int,
    resume: bool = False,
    registry: Registry[Extractor] | None = None,
    extract_config: ExtractConfig | None = None,
    now: int | None = None,
    on_progress: Progress | None = None,
    prefixes: TaskPrefixes | None = None,
    pool: EmbedPool | None = None,
    workers: int = 0,
    rebuild: bool = False,
    index_path: str | Path | None = None,
) -> BuildResult:
    r"""一趟索引：按需跑 Tier0 与 Tier1，交出两级的账。

    `target_tier` 是**这一趟跑到哪一级为止**（`tier.target_tier()` 给出配置允许的上限，
    命令行的 `--tier` 可以在它之内再往下压）。`< TIER_METADATA` 表示配置把三级全关了
    ——那不是"跑到 0 级"，是"没有活干"，`P4` 由调用方拒绝（见 `cli/commands/index.py`）。

    `resume=True` 跳过 Tier0（不扫盘），直接从库里挑 Tier1 的活。⚠ 它在空库上是
    **没有可接续的东西**，不报错——调用方要把这句话说出来。

    `config` 是**已经生效的那一份**：命令行的覆盖（`--max-chunks-per-doc`）由调用方
    事先 `dataclasses.replace()` 进去。这一层不解析参数，也不该解析——`parser_ver()`
    与 `split()` 必须拿到同一个 `ChunkCfg`，两份会让"写下的版本"与"实际的切法"打架。

    `registry` / `extract_config` 不给就按配置现场装配（`ExtractConfig.from_config()` +
    `build_registry()`）。可传是为了测试能塞一个受控的提取器。
    """
    started = time.monotonic()
    result = BuildResult(target_tier=target_tier)
    every = config.perf.commit_every_files

    if not resume and target_tier >= TIER_METADATA:
        result.tier0 = run_tier0(
            store,
            scanner=scanner,
            rules=rules,
            every=every,
            now=now,
            on_progress=on_progress,
        )
    if target_tier >= TIER_FULLTEXT:
        if extract_config is None:
            # `ExtractConfig` 与 `ScanConfig` 一样，是"从 Config 摘出来让提取器不碰
            # TOML"的那一层；`max_bytes` 来自 `[scan] max_file_mb`（两个数只有一个
            # 真相来源，见 extract/base.py 的文档）。
            extract_config = ExtractConfig.from_config(config)
        result.tier1 = run_tier1(
            store,
            registry=registry or build_registry(extract_config),
            extract_config=extract_config,
            chunk=config.chunk,
            every=every,
            on_progress=on_progress,
        )
    if target_tier >= TIER_SEMANTIC:
        result.tier2 = run_tier2(
            store,
            config=config,
            every=every,
            prefixes=prefixes,
            pool=pool,
            workers=workers,
            rebuild=rebuild,
            index_path=index_path,
            on_progress=on_progress,
        )
    result.seconds = time.monotonic() - started
    return result
