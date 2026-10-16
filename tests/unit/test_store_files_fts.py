r"""`F4-01` 的 `files` / `files_fts` 建库与检索：`index/sqlite_store.py` + `schema.sql`。

关联验收项 **A8**（Tier0 完成后立刻能按文件名搜到；分级未就绪时显式标注）。

本文件钉的主要是**静默错误**——它们全都不抛异常、不写日志，只在一个月后表现为
"同一个文件搜出来好几条""盘点数字少了一半""某个词永远搜不到"：

* `put_file` 被重复调用时 `files_fts` 里多出一行（搜出来是重复的）；
* 路径大小写不同的两条记录各占一行（计数偏大、`--prune` 删不干净）；
* `volume` 的大小写跟着输入走（`count_files(volume="d:")` 漏掉一半）；
* 用户查询里带了 FTS5 的语法字符（`*` `"` `AND`）时查询抛异常，或者更糟：
  不抛异常但按**语法**解释、静默返回一个别的结果集；
* 查询串里没有可搜内容时与"没搜到"给出同一个输出。

`E38` 那两条靠读数定下来的切法（2-gram 只给名字、查询侧丢单字汉字）也各有一条
测试钉住：它们是**故意的**，将来有人"顺手统一一下"就会掉召回，而掉召回不报错。
"""

from __future__ import annotations

import sqlite3

import pytest

from localsearch.index.sqlite_store import (
    SCHEMA_VERSION,
    FileRow,
    SchemaVersionError,
    SqliteStore,
    StoreError,
    store_path,
)


def row(path: str, *, status: str = "metadata", tier: int = 0, size: int = 100) -> FileRow:
    """一条最小的 `files` 行。默认是 Tier0 跑完的样子。"""
    name = path.rsplit("/", 1)[-1]
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    return FileRow(
        path=path, size_bytes=size, mtime_ns=1_700_000_000_000_000_000, status=status,
        tier=tier, ext=ext,
    )


@pytest.fixture()
def store() -> SqliteStore:
    s = SqliteStore.in_memory()
    yield s
    s.close()


# ==============================================================================
# 1. 路径的入库形态（一个函数说了算）
# ==============================================================================


def test_store_path_turns_backslashes_forward():
    assert store_path(r"D:\work\项目\财务报表.xlsx") == "D:/work/项目/财务报表.xlsx"


def test_store_path_keeps_the_case():
    """**大小写原样**：`NOCASE` 只管"唯不唯一"，而这一列还要拿去打开文件、显示给用户。

    收成小写不会报错，只会让 `lsearch search` 的输出把用户盘上的
    `README.md` 印成 `readme.md`——看着像另一份文件。
    """
    assert store_path(r"D:\Work\README.MD") == "D:/Work/README.MD"


def test_store_path_does_not_resolve_dots():
    r"""**不许** `normpath`：库里的路径必须还是文件真实的路径。

    解掉 `..` 之后，库里躺的是一条**猜出来的**路径。它看起来更"干净"，但那种路径
    打不开原文件，而"打不开"要到用户双击时才暴露。
    """
    assert store_path(r"D:\work\a\..\b.txt") == "D:/work/a/../b.txt"


def test_file_row_derives_name_and_volume():
    r = row(r"D:\work\项目\财务报表.xlsx")
    assert r.name == "财务报表.xlsx"
    assert r.volume == "d:"
    assert r.path == "D:/work/项目/财务报表.xlsx", "path 也要过 store_path"


def test_volume_is_lowercased_whatever_the_scanner_said():
    """**同一个卷只有一个写法。** scandir 从 `D:/work` 出发给 `D:`，MFT 按卷枚举给 `d:`。

    不统一的话 `count_files(volume="d:")` 会漏掉一整批，而那个数字**看起来只是偏小**。
    """
    upper = FileRow(path="D:/a.txt", size_bytes=1, mtime_ns=1, status="metadata")
    lower = FileRow(path="d:/b.txt", size_bytes=1, mtime_ns=1, status="metadata")
    assert upper.volume == lower.volume == "d:"


# ==============================================================================
# 2. FileRow 自己挡住写错的取值
# ==============================================================================


def test_a_typo_in_status_is_refused():
    """写错一个字母不会报错，只会让 Tier1 的选择条件永远挑不到那个文件。"""
    with pytest.raises(ValueError, match="status"):
        FileRow(path="d:/a.txt", size_bytes=1, mtime_ns=1, status="matadata")


def test_a_tier_out_of_range_is_refused():
    with pytest.raises(ValueError, match="tier"):
        FileRow(path="d:/a.txt", size_bytes=1, mtime_ns=1, status="metadata", tier=3)
    with pytest.raises(ValueError, match="tier"):
        FileRow(path="d:/a.txt", size_bytes=1, mtime_ns=1, status="metadata", tier=-1)


def test_negative_size_is_refused():
    with pytest.raises(ValueError):
        FileRow(path="d:/a.txt", size_bytes=-1, mtime_ns=1, status="metadata")


# ==============================================================================
# 3. 建库：schema、版本、PRAGMA
# ==============================================================================


def test_building_the_schema_creates_every_table():
    """计划 §4.4 的七张表。少一张的症状是"某个功能点永远查不到东西"。"""
    with SqliteStore.in_memory() as s:
        names = {
            r[0]
            for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    for expected in ("meta", "blobs", "files", "chunks", "embed_cache"):
        assert expected in names
    for fts in ("files_fts", "chunks_fts"):
        assert fts in names, f"{fts} 是虚拟表，也在 sqlite_master 里"


def test_the_schema_version_is_recorded():
    with SqliteStore.in_memory() as s:
        assert s.schema_version == SCHEMA_VERSION


def test_opening_a_brand_new_file_uses_wal(tmp_path):
    """WAL 是"索引跑着的时候还能搜"的前提，而它是**库级**的、会留在文件头上。"""
    db = tmp_path / "index" / "lsearch.db"
    with SqliteStore.open(db) as s:
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.is_file(), "父目录也要建出来（数据目录第一次索引之前不存在）"


def test_foreign_keys_are_on():
    """不打开它，删 blob 之后留下的是一堆孤儿切片行——**不报错**。"""
    with SqliteStore.in_memory() as s:
        assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopening_an_existing_library_does_not_rebuild(tmp_path):
    """第二次打开不许再跑一遍 DDL（`table meta already exists`），也不许丢数据。"""
    db = tmp_path / "lsearch.db"
    first = SqliteStore.open(db)
    first.put_file(row("d:/a.txt"))
    first.close()
    with SqliteStore.open(db) as second:
        assert second.count_files() == 1
        assert second.schema_version == SCHEMA_VERSION


def test_a_library_from_the_future_is_refused(tmp_path):
    """版本对不上要**立刻停**。

    带着一份不认识的 schema 继续跑，症状是"某几列一直是 NULL"或者"某条查询永远
    返回 0 条"——两者都不报错，只是结果不对。
    """
    db = tmp_path / "lsearch.db"
    with SqliteStore.open(db):
        pass
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError, match="版本"):
        SqliteStore.open(db)


def test_an_empty_file_with_a_version_is_refused(tmp_path):
    """有版本号却没有表——不是本工具建的库。这种库接着写下去会把两套 schema 混在一起。"""
    db = tmp_path / "lsearch.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError):
        SqliteStore.open(db)


def test_fts5_is_probed_before_building(tmp_path):
    """探针表不许留在库里：它若留下，`sqlite_master` 里就多一张 `__fts5_probe`。"""
    with SqliteStore.in_memory() as s:
        leftover = s._conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '__fts5%'"
        ).fetchall()
    assert leftover == []


# ==============================================================================
# 4. 写行：重复、去重、提交
# ==============================================================================


def test_put_file_says_true_the_first_time(store: SqliteStore):
    assert store.put_file(row("d:/work/财务报表.xlsx")) is True
    assert store.count_files() == 1


def test_put_file_says_false_for_the_same_path(store: SqliteStore):
    assert store.put_file(row("d:/work/财务报表.xlsx")) is True
    assert store.put_file(row("d:/work/财务报表.xlsx", size=999)) is False
    assert store.count_files() == 1
    assert store.get_file("d:/work/财务报表.xlsx").size_bytes == 100, "已有的那行不许被改写"


def test_put_file_says_false_for_a_case_variant(store: SqliteStore):
    """NTFS 的路径比较不分大小写，所以 `COLLATE NOCASE` 要把这两条撞成一行。

    撞不成的后果：同一个文件两条记录 → 计数偏大、搜索结果里出现两遍、`--prune`
    删掉一条还剩一条。
    """
    assert store.put_file(row("D:/Work/财务报表.xlsx")) is True
    assert store.put_file(row("d:/work/财务报表.xlsx")) is False
    assert store.count_files() == 1


def test_a_duplicate_does_not_add_a_second_fts_row(store: SqliteStore):
    """⚠ **这一条钉的是 `cursor.rowcount` 那条约定。**

    `put_file` 靠"`INSERT OR IGNORE` 被忽略时 rowcount 是 0"来跳过 `files_fts` 的
    写入。SQLite 哪天改了这条行为，症状不是异常，而是 `files_fts` 里慢慢长出重复行——
    搜索时同一个文件出现好几遍，而 `files` 表看上去完全正常。
    """
    store.put_file(row("d:/work/财务报表.xlsx"))
    store.put_file(row("d:/work/财务报表.xlsx"))
    store.put_file(row("D:/WORK/财务报表.xlsx"))
    fts_rows = store._conn.execute("SELECT count(*) FROM files_fts").fetchone()[0]
    assert fts_rows == store.count_files() == 1


def test_close_commits_what_was_not_committed(tmp_path):
    """`sqlite3` 关闭**不会**替你提交（未提交的事务按回滚算）。

    不显式提交的话，"最后一个批次悄悄丢了"就是默认行为，而它看起来与"那批文件本来
    就没扫到"一模一样。
    """
    db = tmp_path / "lsearch.db"
    s = SqliteStore.open(db)
    s.put_file(row("d:/a.txt"))
    assert s._conn.in_transaction
    s.close()
    with SqliteStore.open(db) as again:
        assert again.count_files() == 1


# ==============================================================================
# 5. 取行
# ==============================================================================


def test_get_file_round_trips_every_column(store: SqliteStore):
    original = FileRow(
        path=r"D:\work\项目\报表.xlsx",
        size_bytes=1234,
        mtime_ns=1_700_000_000_123_456_789,
        status="skipped",
        tier=0,
        ext="xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        error_msg="不支持的后缀",
    )
    store.put_file(original)
    got = store.get_file(r"D:\work\项目\报表.xlsx")
    assert got is not None
    assert got.path == "D:/work/项目/报表.xlsx"
    assert got.name == "报表.xlsx"
    assert (got.size_bytes, got.mtime_ns) == (1234, 1_700_000_000_123_456_789)
    assert (got.status, got.tier, got.ext) == ("skipped", 0, "xlsx")
    assert got.error_msg == "不支持的后缀"
    assert got.id is not None
    assert got.quick_sig is None, "P4 一律不填 quick_sig（见 schema.sql 的注释）"


def test_get_file_accepts_a_path_that_was_not_normalized(store: SqliteStore):
    store.put_file(row("d:/work/a.txt"))
    assert store.get_file(r"d:\work\a.txt") is not None


def test_get_file_returns_none_for_a_missing_path(store: SqliteStore):
    assert store.get_file("d:/nope.txt") is None


def test_count_files_filters(store: SqliteStore):
    store.put_file(row("d:/a.txt", status="metadata", tier=0))
    store.put_file(row("d:/b.txt", status="skipped", tier=0))
    store.put_file(row("c:/c.txt", status="metadata", tier=1))
    assert store.count_files() == 3
    assert store.count_files(status="metadata") == 2
    assert store.count_files(tier=1) == 1
    assert store.count_files(volume="d:") == 2
    assert store.count_files(volume="c:", status="metadata") == 1


# ==============================================================================
# 6. meta
# ==============================================================================


def test_meta_round_trip_and_upsert(store: SqliteStore):
    assert store.get_meta("embed_ver") is None
    store.set_meta("embed_ver", "v1")
    assert store.get_meta("embed_ver") == "v1"
    store.set_meta("embed_ver", "v2")
    assert store.get_meta("embed_ver") == "v2"
    assert store.count_files() == 0, "meta 与 files 是两张表"


# ==============================================================================
# 7. 按名/路径检索（A8）
# ==============================================================================


def test_search_finds_a_file_by_a_two_character_piece_of_its_name(store: SqliteStore):
    """**这一条就是 `A8` 的"按文件名搜到"。**

    `财务报表.xlsx` 在 jieba 的精确切法下是**一个** token，所以"报表"要能命中，
    索引里必须已经有 `报表`（`cut_for_search` 的子串，或 2-gram 层）。`E38` 测的
    就是这个：没有这一层，微召回只有 45%。
    """
    store.put_file(row("d:/work/项目/财务报表.xlsx"))
    result = store.search_paths("报表")
    assert result.has_query is True
    assert [h.name for h in result.hits] == ["财务报表.xlsx"]


def test_search_reaches_across_a_word_seam(store: SqliteStore):
    r"""`务报` 落在 `财务` 与 `报表` 的缝上——**只有 2-gram 那层够得到**。

    词法索引（只有 `cut_for_search`）在这里必然落空：`财务报表` 切出的是
    `财务` / `报表` / `财务报表`，没有 `务报`。`E38` 实测这一层把微召回从 75.3%
    推到 98.8%，所以这一条测试是**故意钉住"不许顺手统一成一种切法"**。
    """
    store.put_file(row("d:/work/项目/财务报表.xlsx"))
    assert [h.name for h in store.search_paths("务报").hits] == ["财务报表.xlsx"]


def test_search_finds_files_under_a_directory_name(store: SqliteStore):
    """按路径检索：目录名也是可搜的（`files_fts.path_tok`）。"""
    store.put_file(row("d:/work/校园/北京大学研究生招生简章.md"))
    store.put_file(row("d:/work/校园/清华大学本科生培养方案.md"))
    hits = store.search_paths("校园").hits
    assert len(hits) == 2


def test_search_uses_and_between_the_words_of_a_long_query(store: SqliteStore):
    """多词是**且**。查询"北京大学的研究生"要命中，靠的是丢掉了单字 `的`（`E38`）。"""
    store.put_file(row("d:/work/校园/北京大学研究生招生简章.md"))
    store.put_file(row("d:/work/校园/清华大学本科生培养方案.md"))
    hits = store.search_paths("北京大学的研究生").hits
    assert [h.name for h in hits] == ["北京大学研究生招生简章.md"]


def test_a_hit_in_the_name_outranks_a_hit_only_in_the_directory(store: SqliteStore):
    """`NAME_WEIGHT` 的意义：搜 `财务报表` 时，**叫这个名字的文件**该排在
    "财务报表/附件/1.txt" 前面。

    ⚠ 另外塞进 5 个不相干的文件，不是凑数：FTS5 的 bm25 在"词出现在**每一个**
    文档里"时 idf 会被夹到 `1e-6`，两个文档的分数差别小到看不清，这条测试就会
    变成在赌行的顺序。
    """
    for i in range(5):
        store.put_file(row(f"d:/other/无关{i}.log"))
    store.put_file(row("d:/work/财务报表/附件/1.txt"))
    store.put_file(row("d:/work/项目/财务报表.xlsx"))
    hits = store.search_paths("财务报表").hits
    assert len(hits) == 2, "两个都该命中，问题只是先后"
    assert [h.name for h in hits][0] == "财务报表.xlsx"
    assert hits[0].rank < hits[1].rank, "bm25 越小越相关——顺序就是靠这个数的"


def test_one_character_queries_are_still_searched(store: SqliteStore):
    """查询侧丢单字汉字，但**全是单字时一个也不丢**——否则等于什么都没搜。"""
    store.put_file(row("d:/work/书.txt"))
    result = store.search_paths("书")
    assert result.tokens == ("书",)
    assert [h.name for h in result.hits] == ["书.txt"]


def test_limit_is_honoured(store: SqliteStore):
    for i in range(5):
        store.put_file(row(f"d:/work/报表{i}.xlsx"))
    assert len(store.search_paths("报表", limit=2).hits) == 2


def test_a_query_with_nothing_searchable_is_not_a_search(store: SqliteStore):
    """`!!!` 与"搜不到"**必须是两种可分辨的结果**。

    拼不出 token 时 `MATCH ''` 是**语法错**，所以不能拿空表达式去查。返回空列表
    顶替的话，用户看到的"没找到"会与"这个词没有可搜的内容"一模一样。
    """
    store.put_file(row("d:/work/报表.xlsx"))
    result = store.search_paths("!!!")
    assert result.has_query is False
    assert result.tokens == ()
    assert result.hits == ()
    assert store.search_paths("北京大学").has_query is True, "拼得出 token 但没命中 ≠ 没搜"


# ==============================================================================
# 8. 用户输入里的 FTS5 语法字符
# ==============================================================================

#: 每一个都在 FTS5 的 MATCH 里有含义。`test_...` 下面的断言只有一条：
#: **要么按字面量匹配，要么就是 0 条——绝不许抛异常，也绝不许按语法解释。**
FTS_SYNTAX_INPUTS = ("*", '"', "(", ")", "AND", "OR", "NOT", "NEAR", "^", "-", "a*", 'x"y')


@pytest.mark.parametrize("query", FTS_SYNTAX_INPUTS)
def test_a_query_made_of_fts_syntax_does_not_blow_up(store: SqliteStore, query: str):
    store.put_file(row("d:/work/报表.xlsx"))
    store.search_paths(query)  # 不抛异常就算过；返回什么由下一组测试钉


def test_a_file_named_and_txt_is_searchable_by_its_own_name(store: SqliteStore):
    """`and.txt` 里的 `AND` 是 FTS5 的保留字。**必须是字面量匹配**。

    直接把它拼进 MATCH 表达式会抛 `fts5: syntax error near "AND"`——而更坏的一种
    写法是"只做大小写规避"，那样 `and.txt` 能搜、`near.txt` 就不能。这里靠**给每个
    token 加引号**，而不是维护保留字名单。
    """
    store.put_file(row("d:/work/and.txt"))
    assert [h.name for h in store.search_paths("and.txt").hits] == ["and.txt"]
    assert [h.name for h in store.search_paths("and").hits] == ["and.txt"]


def test_a_star_never_becomes_a_wildcard_operator(store: SqliteStore):
    """`*` 在 FTS5 里是**前缀通配符**（`报*` = 以"报"开头的 token）。它绝不能溜进
    我们的表达式——否则用户的查询会变成一次他没要求的模糊展开。

    **判据是"`报` 找不到 `报表.xlsx`"**：查询被包成 `"报"` 字面量，就只认恰好是
    `报` 的那个 token；而索引里 `财务报表.xlsx` 一类的文档并没有单字 token `报`。
    如果 `*` 溜进了表达式（`报*`），前缀匹配会**立刻**把这批文件找出来。

    顺带钉住一条**已知下限**（记在 P4「遗留」里）：单字查询够不到 2 字词的内部，
    所以搜"报"不会命中"报表.xlsx"。这不是 bug，是"按词切分"的必然代价；
    真要让单字也命中，索引里得再放一层单字，代价是落盘量。
    """
    store.put_file(row("d:/work/报表.xlsx"))
    store.put_file(row("d:/work/报价.xlsx"))
    assert store.search_paths("报").hits == (), "单字查询不许命中「以它开头」的词"
    assert store.search_paths("报*").hits == store.search_paths("报").hits
    assert store.search_paths("*表").hits == store.search_paths("表").hits
    assert len(store.search_paths("报表").hits) == 1, "两字的那个才是词"


def test_a_quote_in_the_query_does_not_break_the_expression(store: SqliteStore):
    """`"` 会把引号配对搞乱，所以拼表达式时**每个 token 各自加引号**，而 token 里的
    引号先翻倍。这里的 `"` 会被当成标点丢掉，于是查询等价于 `报 表`——**两个词都要有**。"""
    store.put_file(row("d:/work/报 表.txt"))
    result = store.search_paths('报"表')
    assert result.tokens == ("报", "表")
    assert result.expr == '"报" "表"'
    assert [h.name for h in result.hits] == ["报 表.txt"]


# ==============================================================================
# 9. 结果对象的两个方向
# ==============================================================================


def test_path_search_keeps_the_query_it_ran(store: SqliteStore):
    store.put_file(row("d:/work/报表.xlsx"))
    result = store.search_paths("  报表  ")
    assert result.query == "  报表  ", "原样的查询串要留着——日志与「没找到」的提示都要它"
    assert result.tokens == ("报表",)
    assert result.expr == '"报表"'


def test_iter_paths_lists_what_is_in_the_library(store: SqliteStore):
    store.put_file(row("d:/b.txt"))
    store.put_file(row("d:/a.txt"))
    assert list(store.iter_paths()) == ["d:/b.txt", "d:/a.txt"]
