r"""`F4-02` 的正文那一层：`blobs` / `chunks` / `chunks_fts` 的写入与检索。

关联验收项 **A8**（`D10` 的三级就绪：正文落地之后 Tier1 这一层可用），以及 **A6**
的后半句——"原因计入 `files.error_msg`"（那一条要 `files` 表才谈得上，本文件最后
一组用的是**真提取器 + 真样本**跑出来的 `Outcome`）。

本文件钉的还是**静默错误**——它们不抛异常、不写日志，只在很久以后表现为"某些内容
永远搜不到""搜出来重复的两条""某个文件的正文明明在库里、却翻不到"：

* 同一份内容写了两遍 → `blobs` 里两行、切片两套（去重白做了）；
* `parser_ver` 变了却复用旧切片 → 换了提取器之后，新格式的正文**永远不进库**；
* 换版本时不删旧的 FTS 行 → 孤儿行搜得出来、却 JOIN 不到任何切片（"搜到一条、
  显示不出来"）；
* `chunks_fts` 的 `rowid` 与 `chunks.id` 不是同一个 → 删除时按 `chunk_id` 全扫一遍
  FTS 表，或者删错行；
* `text_tok` 里存的是原文而不是分词后的文本 → 中文**一个词也搜不到**，而
  `SELECT` 出来看还挺正常；
* `error_msg` 与 `status` 对不上（`skipped` 带原因、`error` 不带原因）→ 失败报表
  要么全是噪音、要么什么都查不出来。

`E38` 那条"2-gram 只给名/路径、**不给正文**"的切法在这里也有一条测试钉住：它是
**故意的**（正文 2-gram 值 2.76× 的落盘量而收益没测过），将来有人"顺手统一一下"
就会把落盘量翻倍，而这件事不报错。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from localsearch.chunk.sliding import split
from localsearch.core.config import ChunkCfg
from localsearch.core.models import Chunk, ExtractedDoc
from localsearch.extract import (
    ExtractConfig,
    build_registry,
    extract_many,
)
from localsearch.index.sqlite_store import (
    BLOB_CREATED,
    BLOB_REPLACED,
    BLOB_REUSED,
    FileRow,
    SqliteStore,
    StoreError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: 写库时用的假版本号。取值由 `F4-04` 组装（要同时覆盖提取器实现与分块参数，
#: 见 `put_blob()` 的文档）；这一层只管"它变了"这条语义。
PARSER_VER = "p4-test:1"

HASH_A = "a" * 64
HASH_B = "b" * 64


def row(path: str, *, status: str = "metadata", tier: int = 0, size: int = 100) -> FileRow:
    """一条最小的 `files` 行。默认是 Tier0 跑完的样子（与 `F4-01` 的测试同形）。"""
    name = path.rsplit("/", 1)[-1]
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    return FileRow(
        path=path, size_bytes=size, mtime_ns=1_700_000_000_000_000_000, status=status,
        tier=tier, ext=ext,
    )


def chunk(ord_: int, text: str, start: int = 0, end: int = 0, n: int = 10) -> Chunk:
    return Chunk(ord=ord_, text=text, char_start=start, char_end=end, n_tokens=n)


@pytest.fixture()
def store() -> SqliteStore:
    s = SqliteStore.in_memory()
    yield s
    s.close()


def fts_rows(store: SqliteStore) -> list[tuple]:
    """白盒看一眼 `chunks_fts`：`(rowid, chunk_id, text_tok, title)` 按 rowid 排。

    直接查库而不是走 API，是因为要断言的东西（rowid 与 id 是不是同一个、分词那一列
    长什么样）**没有对外接口**——它们只在库里。库里没有接口的列不该为测试长出接口。
    """
    return store._conn.execute(
        "SELECT rowid, chunk_id, text_tok, title FROM chunks_fts ORDER BY rowid"
    ).fetchall()


def write_doc(
    store: SqliteStore,
    path: str,
    doc: ExtractedDoc,
    *,
    content_hash: str = HASH_A,
    parser_ver: str = PARSER_VER,
    now: int = 1000,
) -> int:
    """把一个提取结果写进库的那三步，返回 blob_id。

    ⚠ **这不是 `F4-04` 的循环**：真的那一趟要批量、要按文件数提交、要能续跑。
    这里只把"三步 + 一个事务"的最小形态写出来，好让本文件能整条路地验一遍
    （提取 → 切片 → 落库 → 搜出来）。真正的编排在 `index/build.py`。
    """
    chunks = split(doc.text, **asdict(ChunkCfg())) if doc.text.strip() else []
    written = store.put_blob(
        content_hash=content_hash,
        size_bytes=len(doc.text.encode("utf-8")),
        extractor=doc.source or "unknown",
        parser_ver=parser_ver,
        now=now,
    )
    if written.needs_chunks:
        store.put_chunks(
            written.blob_id, chunks, title=doc.title, modality=doc.modality
        )
    store.set_file_extracted(
        path,
        status="fulltext",
        tier=1,
        content_hash=content_hash,
        blob_id=written.blob_id,
        now=now,
    )
    return written.blob_id


# ==============================================================================
# 1. blob 的身份就是内容
# ==============================================================================


def test_a_new_content_creates_a_blob(store: SqliteStore):
    written = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER, now=7
    )
    assert written.action == BLOB_CREATED
    assert written.needs_chunks
    assert written.blob_id > 0


def test_the_same_content_comes_back_as_reused(store: SqliteStore):
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    second = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert second.action == BLOB_REUSED
    assert second.blob_id == first.blob_id


def test_a_reused_blob_does_not_ask_for_chunks_again(store: SqliteStore):
    store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    again = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert not again.needs_chunks


def test_a_different_content_gets_its_own_blob(store: SqliteStore):
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    second = store.put_blob(
        content_hash=HASH_B, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert second.blob_id != first.blob_id
    assert second.action == BLOB_CREATED


def test_an_empty_content_hash_is_refused(store: SqliteStore):
    with pytest.raises(ValueError, match="content_hash"):
        store.put_blob(content_hash="", size_bytes=1, extractor="x", parser_ver=PARSER_VER)


def test_an_empty_parser_version_is_refused(store: SqliteStore):
    with pytest.raises(ValueError, match="parser_ver"):
        store.put_blob(content_hash=HASH_A, size_bytes=1, extractor="x", parser_ver="")


def test_a_new_parser_version_replaces_the_old_chunks(store: SqliteStore):
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(first.blob_id, [chunk(0, "旧解析器的正文", 0, 7)])

    second = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver="p4-test:2"
    )

    assert second.action == BLOB_REPLACED
    assert second.blob_id == first.blob_id
    assert store.count_chunks(blob_id=first.blob_id) == 0


def test_a_replaced_blob_asks_for_chunks_again(store: SqliteStore):
    store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    again = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver="p4-test:2"
    )
    assert again.needs_chunks


def test_replacing_leaves_no_orphan_fts_row(store: SqliteStore):
    """换版本时 `chunks_fts` 里的旧行也要走——FTS5 是独立表，`chunks` 的级联管不着它。

    留着孤儿的症状：搜得出来那一条，但 JOIN 不到切片，于是"搜到一条、显示不出来"。
    """
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(first.blob_id, [chunk(0, "旧字", 0, 2), chunk(1, "旧词", 2, 4)])
    assert len(fts_rows(store)) == 2

    store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver="p4-test:2"
    )

    assert fts_rows(store) == []


def test_replacing_updates_the_extractor_and_the_version(store: SqliteStore):
    store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_blob(
        content_hash=HASH_A, size_bytes=11, extractor="plaintext", parser_ver="p4-test:2"
    )
    assert store._conn.execute(
        "SELECT extractor, parser_ver, size_bytes, chunk_count FROM blobs"
    ).fetchone() == ("plaintext", "p4-test:2", 11, 0)


def test_replacing_keeps_the_row_identity(store: SqliteStore):
    """`created_at` 不动：变的是"按哪个版本切过"，不是"这份内容什么时候第一次见到"。"""
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER, now=100
    )
    store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver="p4-test:2", now=200
    )
    assert store._conn.execute("SELECT created_at FROM blobs").fetchone()[0] == 100
    assert store._conn.execute("SELECT id FROM blobs").fetchone()[0] == first.blob_id


def test_created_at_uses_the_given_now(store: SqliteStore):
    store.put_blob(
        content_hash=HASH_A, size_bytes=1, extractor="x", parser_ver=PARSER_VER, now=424242
    )
    assert store._conn.execute("SELECT created_at FROM blobs").fetchone()[0] == 424242


def test_an_unknown_action_is_refused():
    """`BlobWrite` 自己也把关：写错一个字母的症状是调用方**永远不写切片**。"""
    from localsearch.index.sqlite_store import BlobWrite

    with pytest.raises(ValueError, match="action"):
        BlobWrite(1, "reuse")


# ==============================================================================
# 2. 切片的写入：chunks + chunks_fts
# ==============================================================================


def test_put_chunks_writes_every_column(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "第一段正文", 0, 5, n=4)], modality="image")
    assert store._conn.execute(
        "SELECT blob_id, ord, char_start, char_end, text, n_tokens, modality FROM chunks"
    ).fetchone() == (blob.blob_id, 0, 0, 5, "第一段正文", 4, "image")


def test_put_chunks_keeps_the_ord_and_the_offsets(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(
        blob.blob_id,
        [chunk(0, "甲", 0, 1), chunk(1, "乙", 1, 2), chunk(2, "丙", 2, 3)],
    )
    assert [
        r[0] for r in store._conn.execute("SELECT ord FROM chunks ORDER BY ord")
    ] == [0, 1, 2]


def test_put_chunks_returns_the_number_written(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1), chunk(1, "乙", 1, 2)]) == 2


def test_chunk_count_is_updated_on_the_blob(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1), chunk(1, "乙", 1, 2)])
    assert store._conn.execute("SELECT chunk_count FROM blobs").fetchone()[0] == 2


def test_an_empty_document_writes_zero_chunks_and_still_records_it(store: SqliteStore):
    """0 字节文件是**合法输入**（`base.Extractor` 的约定）：有 blob、没有切片。

    `chunk_count` 要落成 0——它是"这一份内容切出几片"的事实，留着一个旧数字更坏。
    """
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=0, extractor="plaintext", parser_ver=PARSER_VER
    )
    assert store.put_chunks(blob.blob_id, []) == 0
    assert store.count_chunks(blob_id=blob.blob_id) == 0
    assert store._conn.execute("SELECT chunk_count FROM blobs").fetchone()[0] == 0


def test_the_fts_rowid_is_the_chunk_id(store: SqliteStore):
    """`chunks_fts.rowid == chunks.id`。

    这不是巧合而是本模块维持的不变式：`DELETE FROM chunks_fts WHERE rowid = ?` 走
    rowid 索引；而 `chunk_id` 那一列在 `schema.sql` 里是 `UNINDEXED`——拿它当删除
    条件，每删一片就全扫一遍 FTS 表。
    """
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1), chunk(1, "乙", 1, 2)])
    ids = [r[0] for r in store._conn.execute("SELECT id FROM chunks ORDER BY ord")]
    assert [r[0] for r in fts_rows(store)] == ids


def test_the_fts_chunk_id_column_matches_the_rowid(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1)])
    rowid, chunk_id, _text_tok, _title = fts_rows(store)[0]
    assert rowid == chunk_id


def test_the_stored_text_is_the_original_not_the_tokenized_form(store: SqliteStore):
    """`chunks.text` 是原文：`snippet` / 高亮 / 显示都从那取（计划 §4.4.1 的义务）。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "年度报告：全文", 0, 8)])
    assert (
        store._conn.execute("SELECT text FROM chunks").fetchone()[0] == "年度报告：全文"
    )


def test_the_body_column_is_tokenized_and_lowercased(store: SqliteStore):
    """分词那一列长什么样是**量出来的**（`E38` 的探针跑出来的形态），所以写成字面量。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "Annual Report 年度报告", 0, 20)])
    assert fts_rows(store)[0][2] == "annual report 年度 报告 年度报告"


def test_the_body_layer_has_no_bigrams(store: SqliteStore):
    """`chunks_fts` **不加**相邻汉字 2-gram（`CHUNK_BIGRAMS = False`，`E38` 的决定）。

    加的那一层能把文件名碎片查询的微召回从 75.3% 推到 98.8%，**代价是正文的落盘量
    涨到 2.76 倍**。正文检索打的是词不是碎片，所以这一层不买——而它一旦被"顺手
    统一"，代价翻倍且不报错。`度报` 是 `年度报告` 里的一个跨词缝，它就是那条分界。
    """
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "年度报告", 0, 4)])
    # 三个 token 就是 `cut_for_search` 的全部产出（1 字词 + 整词），一个不多。
    assert fts_rows(store)[0][2].split() == ["年度", "报告", "年度报告"]
    # 行为面才是那条分界：`度报` 是一个跨词缝，正文这一层不该有它的落点。
    # ⚠ 别拿"token 串里含不含这两个字"当判据——整词 token `年度报告` 自己就含。
    assert store.search_chunks("度报").hits == ()


def test_the_title_is_tokenized_too(store: SqliteStore):
    """标题也是要被 MATCH 的列，所以走同一套切法（不然中文标题一个词也搜不到）。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "正文在这里", 0, 5)], title="2024 年度报告")
    assert fts_rows(store)[0][3] == "2024 年度 报告 年度报告"


def test_a_missing_title_is_an_empty_string(store: SqliteStore):
    """没有标题就是空串（不是 `NULL`）：两种"空"会让读的人要写两种判断。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "正文", 0, 2)])
    assert fts_rows(store)[0][3] == ""


def test_writing_chunks_twice_is_refused(store: SqliteStore):
    """重复写会在同一个 blob 下留两份一样的文本——搜出来是重复的，而且不报错。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1)])
    with pytest.raises(StoreError, match="已经有"):
        store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1)])


def test_writing_chunks_after_a_replace_is_fine(store: SqliteStore):
    """换版本之后当然要能再写——拦的是"同一批写两遍"，不是"换一批"。"""
    first = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(first.blob_id, [chunk(0, "旧的", 0, 2)])
    second = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver="p4-test:2"
    )
    assert store.put_chunks(second.blob_id, [chunk(0, "新的正文", 0, 4)]) == 1


def test_a_write_without_commit_can_be_rolled_back(store: SqliteStore):
    """写入**不提交**是这一层的约定（提交粒度属于 `F4-04` 的管线）。

    这里验的是相反的一面：没有 `commit()`，`rollback()` 之后干干净净——"崩在半个
    文件中间"不留半截状态的全部依据就是这个（`files` / `blobs` / `chunks` 三处
    写入落在同一个事务里）。
    """
    store.put_file(row(r"D:\work\a.docx"))
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "甲", 0, 1)])
    store._conn.rollback()
    assert store.count_chunks() == 0
    assert store._conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


# ==============================================================================
# 3. 把结局写回 files（`A6` 的后半句）
# ==============================================================================


def test_a_fulltext_outcome_links_the_blob_and_lifts_the_tier(store: SqliteStore):
    store.put_file(row(r"D:\work\a.docx"))
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert store.set_file_extracted(
        r"D:\work\a.docx",
        status="fulltext",
        tier=1,
        content_hash=HASH_A,
        blob_id=blob.blob_id,
        now=999,
    )
    got = store.get_file(r"D:\work\a.docx")
    assert (got.status, got.tier, got.content_hash, got.blob_id) == (
        "fulltext",
        1,
        HASH_A,
        blob.blob_id,
    )
    assert got.indexed_at == 999


def test_a_blob_id_that_does_not_exist_is_refused(store: SqliteStore):
    """`blob_id` 是外键、`foreign_keys = ON` 已经打开：指一个不存在的 blob 当场报错。

    少了这一条，一个写错的 id 会静默落进 `files`——那个文件的正文再也取不回来，
    而它的 `status` 写着 `fulltext`。
    """
    store.put_file(row(r"D:\work\a.docx"))
    with pytest.raises(sqlite3.IntegrityError):
        store.set_file_extracted(
            r"D:\work\a.docx", status="fulltext", tier=1, blob_id=9999
        )


def test_an_error_outcome_needs_a_reason(store: SqliteStore):
    """一个没有原因的失败是最难查的记录——每一层都觉得自己没错。"""
    store.put_file(row(r"D:\work\a.docx"))
    with pytest.raises(ValueError, match="error_msg"):
        store.set_file_extracted(r"D:\work\a.docx", status="error", tier=0)


def test_a_skipped_outcome_must_not_carry_a_reason(store: SqliteStore):
    """`skipped`（这一类 v1 不做）填上原因，失败报表就淹在噪音里了。

    这条分工是 `extract/batch.py` 定的（`error_msg` 非空是全库唯一的失败判据），
    本方法只是不让它被绕过。
    """
    store.put_file(row(r"D:\work\a.exe"))
    with pytest.raises(ValueError, match="error_msg"):
        store.set_file_extracted(
            r"D:\work\a.exe", status="skipped", tier=0, error_msg="不支持的文件类型"
        )


def test_an_empty_error_message_becomes_null(store: SqliteStore):
    """`batch.Outcome.error_msg` 给的是空串，库里要的是 `NULL`——两种形态会让"这一格
    是不是空的"要写两种判断。"""
    store.put_file(row(r"D:\work\a.txt"))
    store.set_file_extracted(r"D:\work\a.txt", status="skipped", tier=0, error_msg="")
    assert store.get_file(r"D:\work\a.txt").error_msg is None


def test_a_message_that_is_all_whitespace_becomes_null(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    store.set_file_extracted(r"D:\work\a.txt", status="skipped", tier=0, error_msg=" \n\t ")
    assert store.get_file(r"D:\work\a.txt").error_msg is None


def test_a_message_is_stripped(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    store.set_file_extracted(
        r"D:\work\a.txt", status="error", tier=0, error_msg="  ParseFailed: 坏了  "
    )
    assert store.get_file(r"D:\work\a.txt").error_msg == "ParseFailed: 坏了"


def test_the_path_is_normalized_before_matching(store: SqliteStore):
    """调用方给的是扫描器那种反斜杠路径，库里存的是正斜杠。"""
    store.put_file(row(r"D:\work\a.txt"))
    assert store.set_file_extracted(r"D:\work\a.txt", status="skipped", tier=0)


def test_an_unknown_path_returns_false_and_writes_nothing(store: SqliteStore):
    """少了这一句，全盘一轮里"结局落到空处"会被静默吞掉（计数对不上没人知道）。"""
    assert not store.set_file_extracted(r"D:\nope\x.txt", status="skipped", tier=0)
    assert store.count_files() == 0


def test_an_unknown_status_is_refused(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    with pytest.raises(ValueError, match="status"):
        store.set_file_extracted(r"D:\work\a.txt", status="full_text", tier=1)


def test_a_tier_out_of_range_is_refused(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    with pytest.raises(ValueError, match="tier"):
        store.set_file_extracted(r"D:\work\a.txt", status="fulltext", tier=3)


def test_indexed_at_defaults_to_now(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    store.set_file_extracted(r"D:\work\a.txt", status="skipped", tier=0)
    assert store.get_file(r"D:\work\a.txt").indexed_at > 0


def test_a_second_write_back_overwrites_the_first(store: SqliteStore):
    """续跑会再走一遍：一个文件先被记成失败、修好之后再跑一遍要能翻过来。"""
    store.put_file(row(r"D:\work\a.txt"))
    store.set_file_extracted(
        r"D:\work\a.txt", status="error", tier=0, error_msg="FileUnreadable: 一度读不了"
    )
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="plaintext", parser_ver=PARSER_VER
    )
    store.set_file_extracted(
        r"D:\work\a.txt", status="fulltext", tier=1, blob_id=blob.blob_id
    )
    got = store.get_file(r"D:\work\a.txt")
    assert (got.status, got.tier, got.error_msg) == ("fulltext", 1, None)


def test_writing_back_leaves_the_other_rows_alone(store: SqliteStore):
    store.put_file(row(r"D:\work\a.txt"))
    store.put_file(row(r"D:\work\b.txt"))
    store.set_file_extracted(r"D:\work\a.txt", status="skipped", tier=0)
    assert store.get_file(r"D:\work\b.txt").status == "metadata"


# ==============================================================================
# 4. `A6` 的后半句：真提取器 + 真样本 → 真 Outcome → 写进库
# ==============================================================================


@pytest.fixture()
def outcomes():
    """跑一遍真注册表，按名取 `Outcome`。样本来自 `tests/fixtures/`（`F3-06`）。"""
    paths = [
        str(FIXTURES / "good" / "minimal.txt"),
        str(FIXTURES / "broken" / "broken.pdf"),
    ]
    registry = build_registry(ExtractConfig())
    return {
        Path(o.path).name: o
        for o in extract_many(paths, registry=registry, config=ExtractConfig())
    }


def test_a_good_file_gets_its_body_into_the_library(store: SqliteStore, outcomes):
    """`A8` 的正文那一半：Tier0 之后跑一遍 Tier1，正文就能被搜到。"""
    doc = outcomes["minimal.txt"].doc
    assert doc is not None
    store.put_file(row("D:/work/minimal.txt"))
    write_doc(store, "D:/work/minimal.txt", doc)

    found = store.search_chunks("fixture")
    # ⚠ 比的是 `strip()` 过的原文：`sliding._trim` 会把每一片两端的空白裁掉
    # （规则 4），所以单片的文档里 `hits[0].text` 与 `doc.text` 只差首尾空白，
    # 而 `char_start` / `char_end` 指的就是裁过之后的那个区间。
    assert [h.text for h in found.hits] == [doc.text.strip()]


def test_a_chinese_word_from_the_body_is_searchable(store: SqliteStore, outcomes):
    """中文 2 字词（`M9` 定的那条能力）——它靠的是入库前那一次 jieba 预分词。"""
    doc = outcomes["minimal.txt"].doc
    store.put_file(row("D:/work/minimal.txt"))
    write_doc(store, "D:/work/minimal.txt", doc)
    assert store.search_chunks("你好").hits


def test_a_broken_file_lands_in_the_library_with_its_reason(store: SqliteStore, outcomes):
    """`A6` 的后半句：坏文件**在库里**，而且写着为什么。

    注意这一条不是"提取器抛了异常"（那是 `F3-07` 验的），而是"那个原因**落到了
    `files.error_msg` 里**"——没有这一半，失败报表无从谈起。
    """
    broken = outcomes["broken.pdf"]
    assert broken.status == "error"
    store.put_file(row("D:/work/broken.pdf"))
    assert store.set_file_extracted(
        "D:/work/broken.pdf",
        status=broken.status,
        tier=0,
        error_msg=broken.error_msg,
    )
    got = store.get_file("D:/work/broken.pdf")
    assert got.status == "error"
    assert got.error_msg.startswith("ParseFailed: ")
    assert "\n" not in got.error_msg


def test_an_unsupported_file_lands_without_a_reason(store: SqliteStore, tmp_path):
    """这一类 v1 不做（`.exe`）——留 Tier0、`error_msg` 空着、**不进失败报表**。"""
    path = tmp_path / "tool.exe"
    path.write_bytes(b"MZ\x90\x00")
    registry = build_registry(ExtractConfig())
    outcome = next(iter(extract_many([str(path)], registry=registry, config=ExtractConfig())))

    assert outcome.status == "skipped"
    assert outcome.error_msg == ""
    store.put_file(row(str(path), status="metadata"))
    store.set_file_extracted(
        str(path), status=outcome.status, tier=0, error_msg=outcome.error_msg
    )
    got = store.get_file(str(path))
    assert (got.status, got.error_msg, got.blob_id) == ("skipped", None, None)


# ==============================================================================
# 5. 去重：一份内容，多个路径
# ==============================================================================


def test_two_paths_with_the_same_content_share_one_blob(store: SqliteStore):
    store.put_file(row("D:/work/a.docx"))
    store.put_file(row("D:/work/backup/a.docx"))
    doc = ExtractedDoc(text="同一份内容的正文", source="office")

    first = write_doc(store, "D:/work/a.docx", doc, content_hash=HASH_A)
    second = write_doc(store, "D:/work/backup/a.docx", doc, content_hash=HASH_A)

    assert first == second
    assert store.count_chunks() == 1
    assert len(fts_rows(store)) == 1


def test_paths_for_blob_lists_every_copy(store: SqliteStore):
    store.put_file(row("D:/work/a.docx"))
    store.put_file(row("D:/work/backup/a.docx"))
    doc = ExtractedDoc(text="同一份内容的正文", source="office")
    blob_id = write_doc(store, "D:/work/a.docx", doc)
    write_doc(store, "D:/work/backup/a.docx", doc)
    assert store.paths_for_blob(blob_id) == (
        "D:/work/a.docx",
        "D:/work/backup/a.docx",
    )


def test_paths_for_blob_is_sorted(store: SqliteStore):
    store.put_file(row("D:/work/z.docx"))
    store.put_file(row("D:/work/a.docx"))
    doc = ExtractedDoc(text="内容", source="office")
    blob_id = write_doc(store, "D:/work/z.docx", doc)
    write_doc(store, "D:/work/a.docx", doc)
    assert store.paths_for_blob(blob_id) == ("D:/work/a.docx", "D:/work/z.docx")


def test_paths_for_blob_is_empty_before_the_files_are_linked(store: SqliteStore):
    """先写内容、后写回 `files` 是正常的顺序（同一个事务里），中间那一刻它是空的。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    assert store.paths_for_blob(blob.blob_id) == ()


def test_one_hit_can_name_every_path_of_that_content(store: SqliteStore):
    """去重在结果面上的样子：命中**一条**，但它对应两个文件。"""
    store.put_file(row("D:/work/a.txt"))
    store.put_file(row("D:/work/backup/a.txt"))
    doc = ExtractedDoc(text="重复的正文内容", source="plaintext")
    write_doc(store, "D:/work/a.txt", doc)
    write_doc(store, "D:/work/backup/a.txt", doc)

    hits = store.search_chunks("重复").hits
    assert len(hits) == 1
    assert store.paths_for_blob(hits[0].blob_id) == (
        "D:/work/a.txt",
        "D:/work/backup/a.txt",
    )


def test_dedup_is_by_content_not_by_path(store: SqliteStore):
    """两份内容只差一个字，就该是两个 blob。"""
    store.put_file(row("D:/work/a.txt"))
    store.put_file(row("D:/work/b.txt"))
    write_doc(store, "D:/work/a.txt", ExtractedDoc(text="正文甲", source="plaintext"), content_hash=HASH_A)
    write_doc(store, "D:/work/b.txt", ExtractedDoc(text="正文乙", source="plaintext"), content_hash=HASH_B)
    assert store.count_chunks() == 2
    assert store._conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2


def test_a_real_content_hash_is_what_goes_in(store: SqliteStore, outcomes):
    """`content_hash` 的形态（64 位十六进制）在这一层只是**一个字符串**——算它的是
    `ingest/fingerprint.py`。这条钉的是"库里那一格与传进来的一模一样"。"""
    digest = hashlib.sha256(b"x").hexdigest()
    store.put_file(row("D:/work/a.txt"))
    write_doc(
        store, "D:/work/a.txt", ExtractedDoc(text="x", source="plaintext"), content_hash=digest
    )
    assert store._conn.execute("SELECT content_hash FROM blobs").fetchone()[0] == digest
    assert store.get_file("D:/work/a.txt").content_hash == digest


# ==============================================================================
# 6. 正文检索
# ==============================================================================


def test_a_two_character_body_query_finds_it(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "本季度财务报表已经归档", 0, 11)])
    assert len(store.search_chunks("报表").hits) == 1


def test_a_long_query_uses_and_between_its_words(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "项目的预算表已经做完", 0, 10)])
    assert store.search_chunks("项目的预算表").hits
    assert not store.search_chunks("项目的预算表 不存在词").hits


def test_a_query_with_nothing_searchable_is_not_a_search(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "正文", 0, 2)])
    result = store.search_chunks("。。。")
    assert not result.has_query
    assert result.hits == ()


def test_searching_an_empty_library_says_nothing_found(store: SqliteStore):
    """⚠ **这一条钉的是那个说不清的格子**：库里一篇正文都没有，与"跑完了、但没搜到"
    返回的是同一个东西（`has_query` 真、`hits` 空）。分级就绪的标注因此不能由这个方法
    自己给，得看 tier 那本账（`ChunkSearch` 的类文档里写了，`F4-03` 给那本账）。
    """
    result = store.search_chunks("报表")
    assert result.has_query
    assert result.hits == ()


def test_limit_is_honoured(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(
        blob.blob_id, [chunk(i, f"报表第 {i} 段", i, i + 1) for i in range(5)]
    )
    assert len(store.search_chunks("报表", limit=2).hits) == 2


def test_a_title_only_hit_still_returns_the_chunk(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "正文里没有那两个字", 0, 9)], title="年度报告")
    hits = store.search_chunks("年度报告").hits
    assert [h.text for h in hits] == ["正文里没有那两个字"]


def test_the_hit_carries_the_position_and_the_size(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "报表在第二段", 12, 18, n=6)])
    hit = store.search_chunks("报表").hits[0]
    assert (hit.ord, hit.char_start, hit.char_end, hit.n_tokens) == (0, 12, 18, 6)
    assert hit.blob_id == blob.blob_id


def test_the_hit_text_is_the_original_not_the_tokenized_form(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "Annual Report 年度报告", 0, 20)])
    assert store.search_chunks("annual").hits[0].text == "Annual Report 年度报告"


def test_a_body_hit_has_no_title_field(store: SqliteStore):
    """`chunks_fts` 里那份标题是**分词后**的，拿去显示会是"年度 报告"这种串。

    要显示文档标题得给它找一个真落点（`chunks` 表没有那一列），见 P4「遗留」。
    这条测试钉的是"别顺手把分词后的标题塞进结果里"。
    """
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "报表", 0, 2)], title="年度报告")
    assert not hasattr(store.search_chunks("报表").hits[0], "title")


FTS_SYNTAX_INPUTS = ("*", '"', "(", ")", "AND", "OR", "NOT", "NEAR", "^", "a*", 'x"y')


@pytest.mark.parametrize("query", FTS_SYNTAX_INPUTS)
def test_a_query_made_of_fts_syntax_does_not_blow_up(store: SqliteStore, query: str):
    """用户敲进来的东西不保证是"词"。与 Tier0 同一条要求（`tokens._quote`）。"""
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "报表", 0, 2)])
    store.search_chunks(query)


def test_a_star_never_becomes_a_wildcard_operator(store: SqliteStore):
    blob = store.put_blob(
        content_hash=HASH_A, size_bytes=10, extractor="office", parser_ver=PARSER_VER
    )
    store.put_chunks(blob.blob_id, [chunk(0, "报表", 0, 2), chunk(1, "报a", 2, 4)])
    assert [h.text for h in store.search_chunks("报*").hits] == ["报a"]


def test_the_same_query_can_hit_paths_and_bodies(store: SqliteStore):
    """同一个词在两层各有一处落点，各搜各的（两层都不漏）。"""
    store.put_file(row("D:/work/财务报表.xlsx"))
    doc = ExtractedDoc(text="这一页讲的是成本核算", source="office")
    write_doc(store, "D:/work/财务报表.xlsx", doc, content_hash=HASH_B)
    assert len(store.search_paths("报表").hits) == 1
    assert len(store.search_chunks("成本核算").hits) == 1
