r"""索引库的读写面（P4 / `F4-01`）。测试落点 `tests/unit/test_store_files_fts.py`。

三句话概括这一层：

1. **建库/开库都走 `SqliteStore.open()`**，它会配 PRAGMA、按 `schema.sql` 建表、
   核对 `user_version`。调用方不需要知道建表语句长什么样。
2. **表结构只有一处真相来源**：同目录的 `schema.sql`（它自己就是计划 §4.4 的落地形态，
   两处刻意的差异写在那个文件的注释里）。本文件里**不抄任何 DDL**。
3. **切法只有一处真相来源**：`tokens.py`。本文件只决定"哪张表要不要 2-gram"，
   而那个决定是一个带读数的常量（见 `FILES_BIGRAMS` / `CHUNK_BIGRAMS`）。

------------------------------------------------------------------------------
`put_file()` **不提交**——提交是调用方的事
------------------------------------------------------------------------------

`F4-04` 要求"可中断续跑"，所以提交的粒度属于**管线**（`build.py` 每
`PerfCfg.commit_every_files` 个文件提交一次），不属于单行写入。`put_file()` 因此只在
当前事务里落两行（`files` + `files_fts`）就返回：

* 想让写入生效，调用方得自己 `commit()`；
* 想在同一个事务里攒一批，连着调它就是——Python 的 `sqlite3` 默认在第一条 DML 上
  开事务、`commit()` 才结束，所以两次 `put_file()` 之间不提交就是同一个事务；
* `close()` **会**兜底提交（见它的文档串），但不要依赖它：那只是防"最后一个批次
  悄悄丢了"。

------------------------------------------------------------------------------
`files.path` 的形态由本模块定，且只有一个函数说了算
------------------------------------------------------------------------------

见 `store_path()`：**反斜杠换正斜杠，大小写原样留着**。NTFS 的路径比较不分大小写，
所以 `files.path` 上挂的是 `COLLATE NOCASE`（`schema.sql` 里那处刻意的差异）——
两条只差大小写的路径会撞成一行，而**存下来的那一行是先到的那个写法**。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from localsearch.index.tokens import and_expr, query_tokens, tokenize
from localsearch.ingest.base import drive_of

__all__ = [
    "CHUNK_BIGRAMS",
    "FILES_BIGRAMS",
    "NAME_WEIGHT",
    "PATH_WEIGHT",
    "SCHEMA_VERSION",
    "STATUSES",
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

#: `files.status` 的取值（`schema.sql` 那行注释是全集）。**写错一个字母不会报错**，
#: 只会让 Tier1 的选择条件永远挑不到那个文件，所以这里挡一道。
STATUSES = frozenset(
    {"pending", "metadata", "fulltext", "indexed", "skipped", "error", "stale"}
)

#: `D10` 的三级。`files.tier` 只会是这三个值。
MAX_TIER = 2


class StoreError(Exception):
    """索引库这一层能说清楚的失败。"""


class SchemaVersionError(StoreError):
    """库里的 `user_version` 与 `SCHEMA_VERSION` 对不上。"""


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
