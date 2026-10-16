r"""`F4-03` 的 tier 状态推进：词表、选择条件、就绪台账、`status`↔`tier` 的约束，
以及 `D10` 那句"未就绪要**显式标注**"的标注本身（验收项 **A8**）。

本文件钉的是**静默错误**——它们不抛异常、不写日志，只在很久以后表现为"某一级永远
不就绪""某个文件永远不再被处理""语义未就绪却被当成正常结果"：

* Tier0 的选择条件写成 `tier < 0` → **一趟挑不到任何文件**，且不报错（`tier` 的默认
  值就是 0，那个比较恒假）；
* 就绪的分母用总数 → 全盘上任何一个 `.exe`（`skipped`）都让正文那一级**永远不算
  就绪**，于是那个标志从此没有信息量；
* 空库报"就绪" → 把"根本没索引过"说成"一切正常"，这是这个模块最坏的一种错法；
* `status='metadata', tier=1` 这种自相矛盾的行 → 三趟都不接它，而台账还把它记成
  "第 1 级做到了"（**报就绪、其实没有正文**）；
* 做完的文件仍被下一趟挑中 → `F4-04` 的续跑永远不收敛（每趟重做一遍全部文件）；
* 有人明确要语义检索、却**悄悄**给了关键词结果 → 他拿到的是一种看起来合理的东西，
  没有任何办法察觉（`A8` 里"不允许静默"那半句）。

⚠ 一条**故意的**不对称也在这里钉着：`status` 声称的级别**低于** `tier` 记的（如
`fulltext` + `tier=0`）是**允许**的，因为它会被下一趟重做一遍、自己修好；反过来的那
一格（声称"什么都没做"却记着到过第 1 级）才必须当场拒绝。
"""

from __future__ import annotations

import re
from dataclasses import fields
from importlib.resources import files

import pytest

from localsearch.core.config import TiersCfg
from localsearch.index import tier as t
from localsearch.index.sqlite_store import FileRow, SqliteStore
from localsearch.search import tier as st

# ------------------------------------------------------------------ 小工具


def row(path: str, *, status: str = "metadata", tier: int = 0, size: int = 100) -> FileRow:
    """一条最小的 `files` 行。默认是 Tier0 跑完的样子（与 `F4-01` / `F4-02` 的测试同形）。"""
    name = path.rsplit("/", 1)[-1]
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    return FileRow(
        path=path,
        size_bytes=size,
        mtime_ns=1_700_000_000_000_000_000,
        status=status,
        tier=tier,
        ext=ext,
    )


def ledger(**counts: int) -> t.TierLedger:
    """`ledger(metadata_0=3, fulltext_1=2)` → 台账。名字里的下划线是 `{status}_{tier}`。

    ⚠ `status` 与 `tier` 之间用下划线拼，是因为两组取值里都没有下划线——**这个约定
    由 `test_the_ledger_helper_can_name_every_status` 兜着**：新加一个带下划线的
    `status` 时它会红，而不是让某个测试悄悄地喂错键。
    """
    return t.ledger_from(
        {
            (name.rsplit("_", 1)[0], int(name.rsplit("_", 1)[1])): n
            for name, n in counts.items()
        }
    )


@pytest.fixture()
def store() -> SqliteStore:
    s = SqliteStore.in_memory()
    yield s
    s.close()


def put(store: SqliteStore, path: str, status: str, tier: int) -> None:
    assert store.put_file(row(path, status=status, tier=tier)) is True


# ==============================================================================
# 一、词表：三处的取值必须对得上
# ==============================================================================


def test_the_ledger_helper_can_name_every_status() -> None:
    """上面那个 `ledger()` 小工具靠"取值里没有下划线"来拆键——这一条守住它。"""
    assert not any("_" in status for status in t.STATUSES)
    assert all(name.isalpha() for name in t.TIER_NAMES.values())


def test_the_three_tier_names_are_the_config_field_names() -> None:
    """`TIER_NAMES` 与 `TiersCfg` 的字段名**逐字相同**。

    配置里写 `[tiers] fulltext = false`、日志里说"停在 fulltext"、报表的列名是
    `fulltext`——三处是同一个词。配置那边改了名而这里没改，症状是"开关看起来关不掉"
    （改的是 `semantic`，读的是 `semantic`……只是名字对不上时读的是错的那个）。
    """
    assert set(t.TIER_NAMES.values()) == {f.name for f in fields(TiersCfg)}


def test_max_tier_is_the_top_one() -> None:
    assert t.MAX_TIER == t.TIER_SEMANTIC == 2
    assert sorted(t.TIER_NAMES) == [t.TIER_METADATA, t.TIER_FULLTEXT, t.TIER_SEMANTIC]


def test_statuses_match_the_schema_comment() -> None:
    """`STATUSES` 与 `schema.sql` 里那行注释是同一个集合。

    那一行注释是**给建库的人看的取值域**，`STATUSES` 是给代码看的。两者漂开之后，
    往库里写一个代码不认识的 `status` 不会报错（`files.status` 上只有 `NOT NULL`），
    只会让那个文件**哪一趟都不被挑中**。
    """
    sql = files("localsearch.index").joinpath("schema.sql").read_text(encoding="utf-8")
    match = re.search(r"status\s+TEXT NOT NULL,\s*--\s*([a-z|]+)", sql)
    assert match is not None, "schema.sql 里那行 status 的取值域注释不见了"
    assert set(match.group(1).split("|")) == set(t.STATUSES)


def test_every_status_is_either_terminal_or_picked_by_some_pass() -> None:
    """每一个 `status` 要么是终态，要么会被**某一趟**挑走。

    这是"没有 status 掉在缝里"的那条不变量：一个既不是终态、又不在任何一趟的选择
    条件里的取值，会让那个文件**永远不再被处理**——它不会报错，只是那一级永远不就绪，
    而没人能看出是哪几个文件拖着的。
    """
    picked = set().union(*t.WORK_STATUSES.values())
    assert picked | set(t.TERMINAL_STATUSES) == set(t.STATUSES)
    assert not (picked & set(t.TERMINAL_STATUSES))


def test_tier_name_round_trips() -> None:
    for tier, name in t.TIER_NAMES.items():
        assert t.tier_name(tier) == name


def test_tier_name_refuses_an_unknown_number() -> None:
    with pytest.raises(ValueError, match="没有"):
        t.tier_name(3)
    with pytest.raises(ValueError, match="没有"):
        t.tier_name(-1)


# ==============================================================================
# 二、选择条件：Tier0 那条**没有** tier 子句
# ==============================================================================


def test_tier0_has_no_tier_clause() -> None:
    """⚠ 这一条是 `F4-03` 最容易写错、也最不容易发现的一处。

    `files.tier` 的默认值是 0，所以"已达 Tier0"与"什么都没做"在那一列上长得一模一样，
    而 `tier < 0` **恒假**——写成 SQL 就是这一趟永远挑不到任何文件，且不报错。
    Tier0 的完成因此由 `status` 说（`pending` = 名字还没进 `files_fts`）。
    """
    where, params = t.work_condition(t.TIER_METADATA)
    assert "tier" not in where
    assert where == "status IN (?)"
    assert params == ("pending",)
    assert 0 not in params  # 一个数字都不该出现：没有比较，就没有参数


def test_the_higher_tiers_ask_for_tier_less_than() -> None:
    where, params = t.work_condition(t.TIER_FULLTEXT)
    assert where == "tier < ? AND status IN (?, ?, ?, ?)"
    assert params[0] == 1
    assert isinstance(params[0], int), "整数列不该被拿字符串去比"
    assert set(params[1:]) == {"metadata", "fulltext", "indexed", "stale"}


def test_the_body_pass_asks_for_the_rows_that_still_need_a_body() -> None:
    """Tier1 要挑的是"还没到 1 且还该做"的那些——**用"不是什么"写的**。

    写成"是什么"（`status IN ('metadata', 'stale')`）的话，一个 `status` 与 `tier`
    互相矛盾的行会被两边同时漏掉：它既不算做完（`tier` 没到）、也不会被挑中
    （`status` 不在名单里），于是它就那样躺在库里。用"不是什么"写，矛盾的行会被
    重做一遍，而重做一遍正好把它修好。
    """
    where, params = t.work_condition(t.TIER_FULLTEXT)
    assert params[0] == 1
    assert set(params[1:]) == set(t.STATUSES) - set(t.TERMINAL_STATUSES) - {"pending"}


def test_both_higher_passes_skip_the_terminal_statuses() -> None:
    for tier in (t.TIER_FULLTEXT, t.TIER_SEMANTIC):
        _, params = t.work_condition(tier)
        assert not (set(params[1:]) & set(t.TERMINAL_STATUSES))


def test_a_pass_never_picks_a_file_that_already_reached_it() -> None:
    """条件本身（而不是数据）保证"已达该级的行不会被再挑一次"。

    `F4-04` 的续跑靠这一条收敛：做完一个文件就把它推到 `tier = 该级`，下一趟它就不在
    选择条件里了。漏掉 `tier < ?` 的话，每趟会把全部文件重做一遍——**能跑完，只是
    永远跑不完**。
    """
    for tier in (t.TIER_FULLTEXT, t.TIER_SEMANTIC):
        where, params = t.work_condition(tier)
        assert "tier < ?" in where
        assert params[0] == tier


def test_the_status_list_is_sorted_so_the_condition_is_stable() -> None:
    """同一组取值永远生成同一个字符串：条件片段会被日志与测试当字符串用。"""
    first = t.work_condition(t.TIER_FULLTEXT)
    second = t.work_condition(t.TIER_FULLTEXT)
    assert first == second
    assert list(first[1][1:]) == sorted(first[1][1:])


@pytest.mark.parametrize("tier", [-1, 3, 99])
def test_an_unknown_tier_is_refused(tier: int) -> None:
    with pytest.raises(ValueError, match="没有第"):
        t.work_condition(tier)


def test_the_selection_condition_shapes_the_sql_not_the_result(store: SqliteStore) -> None:
    """条件片段喂进 `files` 之后挑出来的确实是"该做的那几个"（行为面，不是字符串面）。"""
    put(store, "C:/a.txt", "metadata", 0)  # 该做正文
    put(store, "C:/b.txt", "fulltext", 1)  # 做完了
    put(store, "C:/c.exe", "skipped", 0)  # 这一类不做
    put(store, "C:/d.txt", "error", 0)  # 这个文件坏了
    assert [r.path for r in store.iter_tier_work(t.TIER_FULLTEXT)] == ["C:/a.txt"]


# ==============================================================================
# 三、`target_tier`：配置说的是"意图"，不是"事实"
# ==============================================================================


def test_the_switches_are_cumulative() -> None:
    """`semantic` 开着就意味着下面两级也要做——向量挂在切片上，切片要正文那一趟才有。"""
    assert t.target_tier(TiersCfg()) == t.TIER_SEMANTIC
    assert t.target_tier(TiersCfg(semantic=False)) == t.TIER_FULLTEXT
    assert t.target_tier(TiersCfg(semantic=False, fulltext=False)) == t.TIER_METADATA
    assert t.target_tier(TiersCfg(metadata=False, fulltext=False, semantic=True)) == (
        t.TIER_SEMANTIC
    )


def test_all_switches_off_means_no_level_at_all() -> None:
    """三个全关**不是**"跑到 Tier0"——`-1` 不是一级，调用方要读成"这一趟没有活干"。"""
    assert t.target_tier(TiersCfg(metadata=False, fulltext=False, semantic=False)) == -1


# ==============================================================================
# 四、台账：三个数各自怎么来的
# ==============================================================================


def test_the_total_is_the_sum() -> None:
    led = ledger(metadata_0=5, fulltext_1=2, skipped_0=3)
    assert led.total == 10
    assert led.terminal == 3
    assert led.pending == 0


def test_tier0_done_is_total_minus_pending() -> None:
    """字面地数 `tier >= 0` 等于说"每一行都做完了 Tier0"——0 是默认值，那个判断恒真。

    Tier0 唯一的实质内容是"这一行的名字进过 `files_fts`"，它由 `pending` 说。
    """
    led = ledger(metadata_0=5, pending_0=2)
    assert led.done_at(t.TIER_METADATA) == 5
    assert led.expected_at(t.TIER_METADATA) == 7


def test_a_pending_row_is_the_only_thing_that_holds_tier0_back() -> None:
    """`error` / `skipped` 的行**名字也进过索引**，所以它们不算 Tier0 的缺口。"""
    led = ledger(metadata_0=5, error_0=2, skipped_0=3)
    assert led.ready(t.TIER_METADATA) is True


def test_the_upper_tiers_are_counted_cumulatively() -> None:
    led = ledger(metadata_0=4, fulltext_1=3, indexed_2=2, skipped_0=1)
    assert led.done_at(t.TIER_METADATA) == 10  # 只有 pending 挡住 Tier0
    assert led.done_at(t.TIER_FULLTEXT) == 5
    assert led.done_at(t.TIER_SEMANTIC) == 2


def test_the_denominator_excludes_the_terminal_statuses() -> None:
    """⚠ 分母用总数的话，全盘上任何一个 `.exe` 都让正文那一级**永远不算就绪**。

    这一条是 `F4-03` 里最容易"看起来对"的一处：跑一次全盘索引，`skipped` 少说几千个，
    于是"正文索引就绪"这个标志永远是假的——一个永远为假的标志等于没有标志。
    """
    led = ledger(fulltext_1=7, skipped_0=5, error_0=2)
    assert led.total == 14
    assert led.expected_at(t.TIER_FULLTEXT) == 7  # 14 - 5 - 2
    assert led.done_at(t.TIER_FULLTEXT) == 7
    assert led.ready(t.TIER_FULLTEXT) is True


def test_the_denominator_of_tier0_is_every_row() -> None:
    """Tier0 的分母**是**总数：每一个文件都该有一个进过索引的名字，没有例外。"""
    led = ledger(metadata_0=4, skipped_0=5)
    assert led.expected_at(t.TIER_METADATA) == 9


def test_a_stale_row_does_not_count_as_done_for_the_tier_it_reached() -> None:
    """`stale` 的行内容已经变了，库里那一份是旧的——**不算做到**它到过的任何级别。

    少算一行的后果是多一句"未就绪"，多算一行的后果是"报就绪而内容是旧的"。
    两个方向都不好，但只有后一种是"看起来正常"的那种坏。
    """
    fresh = ledger(fulltext_1=3)
    stale = ledger(fulltext_1=2, stale_1=1)
    assert fresh.ready(t.TIER_FULLTEXT) is True
    assert stale.ready(t.TIER_FULLTEXT) is False
    assert stale.done_at(t.TIER_FULLTEXT) == 2
    assert stale.expected_at(t.TIER_FULLTEXT) == 3  # 分母不动：那一行还该做
    assert stale.stale == 1


def test_a_stale_row_still_counts_for_tier0() -> None:
    """改内容不改名字：Tier0 那一列（名字已经进了 `files_fts`）不受 `stale` 影响。"""
    led = ledger(metadata_0=3, stale_0=1)
    assert led.done_at(t.TIER_METADATA) == 4
    assert led.ready(t.TIER_METADATA) is True


def test_a_stale_row_below_a_level_was_never_counted_for_it() -> None:
    """`stale` + `tier = 0` 的那一行本来就没算进第 1 级，不能再减一次。

    减两次的症状是"那一级明明做完了却报未就绪"，而它会一直挂着——因为没有任何一趟
    会去动那一行（它要重做的是 Tier0 那一趟的事）。
    """
    assert ledger(fulltext_1=2).done_at(t.TIER_FULLTEXT) == 2
    assert ledger(fulltext_1=2, stale_0=1).done_at(t.TIER_FULLTEXT) == 2
    assert ledger(fulltext_1=2, stale_1=1).done_at(t.TIER_FULLTEXT) == 2


@pytest.mark.parametrize("tier", [0, 1, 2])
def test_an_empty_index_is_never_ready(tier: int) -> None:
    """⚠ 空库**不算就绪**。`0 >= 0` 成立，照 `>=` 数会把三级全报成"就绪"。

    库里一行都没有与"跑完了但没搜到"在数据上无法区分，而这两件事的正确回答完全
    不同：前者要你先把索引跑起来，后者要你换个词。把前者报成后者，是这个模块最坏的
    一种错法。
    """
    empty = t.ledger_from({})
    assert empty.total == 0
    assert empty.is_empty is True
    assert empty.ready(tier) is False
    assert empty.can_reach(tier) is False
    assert empty.progress(tier) == (0, 0)


def test_can_reach_distinguishes_two_kinds_of_zero() -> None:
    """分母为 0 有两种含义，台账只说"没有活可干"，怎么解释是调用方的事。

    ⚠ 两者的 `ready` 相反，而这是**对的**：空库那一格被 `is_empty` 挡住（`False`），
    而"能做到的全是终态文件"那一格是**真的做完了**（`0 >= 0`）。把后者也报成
    未就绪，症状是一个永远挂着"未就绪"的全盘索引——没有 `.txt` 的盘上它永远不消失。
    """
    all_terminal = ledger(skipped_0=3, error_0=2)
    assert all_terminal.can_reach(t.TIER_FULLTEXT) is False
    assert all_terminal.ready(t.TIER_FULLTEXT) is True
    assert all_terminal.can_reach(t.TIER_METADATA) is True  # Tier0 的分母是总数
    assert all_terminal.ready(t.TIER_METADATA) is True
    assert t.ledger_from({}).can_reach(t.TIER_FULLTEXT) is False
    assert t.ledger_from({}).ready(t.TIER_FULLTEXT) is False


def test_progress_reports_the_two_numbers() -> None:
    led = ledger(metadata_0=7, fulltext_1=3, skipped_0=2)
    assert led.progress(t.TIER_FULLTEXT) == (3, 10)  # 12 - 2


@pytest.mark.parametrize("tier", [-1, 3])
def test_the_ledger_refuses_an_unknown_level(tier: int) -> None:
    led = ledger(metadata_0=1)
    with pytest.raises(ValueError, match="没有第"):
        led.done_at(tier)
    with pytest.raises(ValueError, match="没有第"):
        led.expected_at(tier)
    with pytest.raises(ValueError, match="没有第"):
        led.progress(tier)


def test_the_ledger_refuses_a_status_it_does_not_know() -> None:
    """手改过库、或者 `schema.sql` 与代码漂了——两种情况下"照单全收"都会给出一个
    看起来正常的数（多出来的行算进 `total` 却落在三级之外）。"""
    with pytest.raises(ValueError, match="不认识的 status"):
        t.ledger_from({("done", 0): 1})


def test_the_ledger_refuses_an_unknown_level_key() -> None:
    with pytest.raises(ValueError, match="没有第"):
        t.ledger_from({("metadata", 7): 1})


def test_the_ledger_refuses_a_negative_count() -> None:
    with pytest.raises(ValueError, match="负"):
        t.ledger_from({("metadata", 0): -1})


# ==============================================================================
# 五、`status` 与 `tier` 不许互相矛盾（`set_file_extracted()` 执行）
# ==============================================================================


@pytest.mark.parametrize(
    ("status", "tier"),
    [("metadata", 0), ("fulltext", 1), ("fulltext", 2), ("indexed", 2), ("skipped", 0)],
)
def test_the_legal_combinations_go_through(store: SqliteStore, status: str, tier: int) -> None:
    put(store, "C:/a.txt", "metadata", 0)
    assert store.set_file_extracted("C:/a.txt", status=status, tier=tier) is True
    got = store.get_file("C:/a.txt")
    assert got is not None and (got.status, got.tier) == (status, tier)


@pytest.mark.parametrize(
    ("status", "tier"),
    [("fulltext", 0), ("indexed", 0), ("indexed", 1)],
)
def test_a_status_that_claims_more_than_the_tier_is_refused(
    store: SqliteStore, status: str, tier: int
) -> None:
    """`status` 说做到了第 N 级，`tier` 必须至少是 N——否则台账那一侧会把它记成
    "第 N 级做到了"，而它其实没有。"""
    put(store, "C:/a.txt", "metadata", 0)
    with pytest.raises(ValueError, match="两列在说两件事"):
        store.set_file_extracted("C:/a.txt", status=status, tier=tier)
    got = store.get_file("C:/a.txt")
    assert got is not None and (got.status, got.tier) == ("metadata", 0)  # 一个字没改


@pytest.mark.parametrize("status", ["metadata", "pending"])
def test_a_status_that_claims_nothing_cannot_have_a_tier(
    store: SqliteStore, status: str
) -> None:
    """⚠ 这一格就是那条不对称的由来。

    `status='metadata', tier=1` 的行**没有任何一趟会再挑到它**：Tier0 那趟只挑
    `pending`、Tier1 那趟要求 `tier < 1`、Tier2 那趟的 `status` 名单里没有
    `metadata`。而台账还把它记成"第 1 级做到了"——**报就绪、其实没有正文**。
    """
    put(store, "C:/a.txt", "metadata", 0)
    with pytest.raises(ValueError, match="没有任何一趟会再挑到它"):
        store.set_file_extracted("C:/a.txt", status=status, tier=1)


def test_the_opposite_direction_is_allowed_because_the_next_pass_heals_it(
    store: SqliteStore,
) -> None:
    """`status='fulltext', tier=0` 是允许的：它下一趟会被重做一遍，而重做正好修好它。

    两个方向不对称，是因为**后果不对称**——这一格会自己好，上面那一格不会。
    """
    put(store, "C:/a.txt", "fulltext", 0)  # put_file 只校验取值域，允许这一行存在
    assert [r.path for r in store.iter_tier_work(t.TIER_FULLTEXT)] == ["C:/a.txt"]


@pytest.mark.parametrize("tier", [0, 1, 2])
def test_the_terminal_statuses_are_not_constrained_by_the_tier(
    store: SqliteStore, tier: int
) -> None:
    """`skipped` / `error` 可能是"做到过第 1 级、后来重做失败了"——那一条的正文
    **还在库里**，把它的 `tier` 压成 0 是一句谎话。"""
    put(store, "C:/a.txt", "metadata", 0)
    assert store.set_file_extracted("C:/a.txt", status="error", tier=tier, error_msg="x")
    assert store.set_file_extracted("C:/a.txt", status="skipped", tier=tier)
    assert store.set_file_extracted("C:/a.txt", status="stale", tier=tier)


def test_the_written_row_agrees_with_the_vocabulary(store: SqliteStore) -> None:
    """写进去的 `status` 一定在 `STATUSES` 里，而 `STATUSES` 与 `schema.sql` 一致
    （第一条测试）——两跳合起来说明"库里的行不会被台账拒掉"。"""
    put(store, "C:/a.txt", "metadata", 0)
    store.set_file_extracted("C:/a.txt", status="fulltext", tier=1)
    got = store.get_file("C:/a.txt")
    assert got is not None
    assert t.ledger_from(store.tier_counts()).total == 1


# ==============================================================================
# 六、盘点与挑选：`tier_counts()` / `iter_tier_work()`
# ==============================================================================


def test_tier_counts_groups_by_status_and_tier(store: SqliteStore) -> None:
    put(store, "C:/a.txt", "metadata", 0)
    put(store, "C:/b.txt", "metadata", 0)
    put(store, "C:/c.md", "fulltext", 1)
    put(store, "C:/d.exe", "skipped", 0)
    put(store, "C:/e.exe", "skipped", 0)
    put(store, "C:/f.bin", "skipped", 0)
    assert store.tier_counts() == {
        ("metadata", 0): 2,
        ("fulltext", 1): 1,
        ("skipped", 0): 3,
    }


def test_tier_counts_omits_a_combination_that_does_not_exist(store: SqliteStore) -> None:
    """不补齐成 7 × 3 的全表：`ledger_from()` 按 key 累加，"是 0"与"不在字典里"同义。"""
    put(store, "C:/a.txt", "metadata", 0)
    assert set(store.tier_counts()) == {("metadata", 0)}


def test_tier_counts_of_an_empty_index_is_empty() -> None:
    with SqliteStore.in_memory() as s:
        assert s.tier_counts() == {}
        assert t.ledger_from(s.tier_counts()).is_empty is True


def test_tier_counts_feeds_the_ledger(store: SqliteStore) -> None:
    put(store, "C:/a.txt", "metadata", 0)
    put(store, "C:/b.txt", "metadata", 0)
    put(store, "C:/c.txt", "fulltext", 1)
    led = t.ledger_from(store.tier_counts())
    assert (led.total, led.done_at(0), led.done_at(1)) == (3, 3, 1)
    assert led.ready(t.TIER_METADATA) is True
    assert led.ready(t.TIER_FULLTEXT) is False


def test_iter_tier_work_picks_exactly_the_rows_the_condition_says(store: SqliteStore) -> None:
    put(store, "C:/new.txt", "metadata", 0)  # 要正文
    put(store, "C:/done.txt", "fulltext", 1)  # 做完了
    put(store, "C:/broken.txt", "error", 0)  # 坏了
    put(store, "C:/tool.exe", "skipped", 0)  # 这一类不做
    put(store, "C:/vector.txt", "fulltext", 1)  # 正文做完、向量没做
    assert [r.path for r in store.iter_tier_work(t.TIER_METADATA)] == []
    assert [r.path for r in store.iter_tier_work(t.TIER_FULLTEXT)] == ["C:/new.txt"]
    assert [r.path for r in store.iter_tier_work(t.TIER_SEMANTIC)] == [
        "C:/done.txt",
        "C:/vector.txt",
    ]


def test_iter_tier_work_hands_back_full_rows(store: SqliteStore) -> None:
    """拿回来的是 `FileRow`（`F4-04` 要用它打开文件、取 `mtime`），不是一个路径字符串。"""
    put(store, "C:/a.txt", "metadata", 0)
    (got,) = list(store.iter_tier_work(t.TIER_FULLTEXT))
    assert isinstance(got, FileRow)
    assert got.path == "C:/a.txt" and got.status == "metadata" and got.tier == 0
    assert got.id is not None and got.size_bytes == 100


def test_iter_tier_work_is_ordered_by_insertion(store: SqliteStore) -> None:
    """**按 `id` 排序**是给续跑用的：中途停下之后下一趟要能接着走，而"接着走"只在
    两次遍历的顺序一致时才有意义。没有 `ORDER BY` 时 SQLite 走 `idx_files_tier`，
    出来的顺序随索引形态变。"""
    for name in ("C:/z.txt", "C:/a.txt", "C:/m.txt", "C:/b.txt"):
        put(store, name, "metadata", 0)
    assert [r.path for r in store.iter_tier_work(t.TIER_FULLTEXT)] == [
        "C:/z.txt",
        "C:/a.txt",
        "C:/m.txt",
        "C:/b.txt",
    ]


def test_iter_tier_work_honours_the_limit(store: SqliteStore) -> None:
    for i in range(5):
        put(store, f"C:/f{i}.txt", "metadata", 0)
    assert len(list(store.iter_tier_work(t.TIER_FULLTEXT, limit=2))) == 2


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_refused(store: SqliteStore, limit: int) -> None:
    """⚠ `LIMIT 0` 是一个合法的 SQL、返回 0 行——"这一趟没活干"与"参数写错了"就会
    长得一模一样。"""
    with pytest.raises(ValueError, match="limit"):
        list(store.iter_tier_work(t.TIER_FULLTEXT, limit=limit))


def test_iter_tier_work_refuses_an_unknown_tier(store: SqliteStore) -> None:
    with pytest.raises(ValueError, match="没有第"):
        list(store.iter_tier_work(3))


def test_a_finished_file_drops_out_of_the_next_pass(store: SqliteStore) -> None:
    """⚠ `F4-04` 的续跑靠这一条**收敛**：做完一个文件就把它推到 `tier = 该级`，
    下一趟它就不在选择条件里了。少这一步的话每趟都重做全部文件——**能跑完，只是
    永远跑不完**，而这一点在单机小库上根本看不出来。

    这里不提交（提交粒度是管线的事，`F4-04`）：同一条连接上未提交的写入自己看得见，
    而"挑不挑得到"只跟 `WHERE` 有关。
    """
    put(store, "C:/a.txt", "metadata", 0)
    put(store, "C:/b.txt", "metadata", 0)
    (first,) = list(store.iter_tier_work(t.TIER_FULLTEXT, limit=1))
    store.set_file_extracted(first.path, status="fulltext", tier=1)
    assert [r.path for r in store.iter_tier_work(t.TIER_FULLTEXT)] == ["C:/b.txt"]
    (second,) = list(store.iter_tier_work(t.TIER_FULLTEXT))
    store.set_file_extracted(second.path, status="fulltext", tier=1)
    assert list(store.iter_tier_work(t.TIER_FULLTEXT)) == []
    assert t.ledger_from(store.tier_counts()).ready(t.TIER_FULLTEXT) is True


# ==============================================================================
# 七、标注（`search/tier.py`）：`D10` 那句"显式标注"的落点
# ==============================================================================


def ready_ledger() -> t.TierLedger:
    """一个"该做的都做完了"的台账：三个文件全到第 2 级。"""
    return t.ledger_from({("indexed", 2): 3})


def ledger_two_thirds_built() -> t.TierLedger:
    """7 个文件做到第 1 级，其中 3 个做到了第 2 级——**向量建了一半**。

    这一份台账是 `F5-04` 之后最要紧的一个中间形态：`semantic` 这一级**有东西可查**
    （所以模式不降级、语义检索真跑），但**没做完**（所以要有那条带数字的标注）。
    `P4` 时这两种情形分不开（都没有向量），`F5-04` 之后它们的处理**相反**。
    """
    return t.ledger_from({("fulltext", 1): 4, ("indexed", 2): 3})


def test_the_modes_that_work_today_are_lexical_and_semantic() -> None:
    """`F5-04` 把 `semantic` 加进 `IMPLEMENTED_MODES`，`hybrid` 仍然要等 `F5-05`。

    ⚠ `hybrid` **不是**"把两段结果拼在一起"：那是 `F4-04` 就能做的事（它正是刻意
    不做的），缺的是把两路**融**起来的那一步（RRF）。
    """
    assert st.IMPLEMENTED_MODES == frozenset({"lexical", "semantic"})
    assert set(st.MODES) == {"lexical", "semantic", "hybrid"}
    assert "hybrid" not in st.IMPLEMENTED_MODES


def test_a_finished_index_needs_no_note_at_all() -> None:
    r = st.resolve("lexical", ready_ledger(), tiers=TiersCfg())
    assert r.mode == "lexical" and r.requested == "lexical"
    assert r.degraded is False
    assert r.notes == ()


def test_a_named_semantic_mode_runs_when_the_vectors_are_there() -> None:
    """`F5-04` 之后 `--mode semantic` 是一个**能跑**的请求：向量都在，一次都不降级。"""
    r = st.resolve("semantic", ready_ledger(), tiers=TiersCfg(), explicit=True)
    assert (r.requested, r.mode, r.degraded) == ("semantic", "semantic", False)
    assert r.notes == ()


def test_a_named_semantic_mode_is_refused_when_no_vector_exists() -> None:
    r"""⚠ `A8` 里"不允许静默"那半句最要紧的一格，`F5-04` 之后**换了异常**。

    一个人明确要了语义检索，说明他要的是"意思相近"，而词法检索会给出一堆**看起来
    合理**的关键词命中——他没有任何办法察觉自己拿到的是另一种东西。宁可失败，
    不可错答。

    ⚠ 这里要的是 `ModeNotReady`（**缺数据**）而不是 `ModeUnavailable`（缺代码）：
    前者跑一趟 `lsearch index --tier 2` 就好了，后者只能等下一版。两者合成一个
    异常，就等于把"去跑索引"和"等下一版"说成同一件事（`ModeNotReady` 的文档）。
    """
    led = t.ledger_from({("fulltext", 1): 7})
    with pytest.raises(st.ModeNotReady) as caught:
        st.resolve("semantic", led, tiers=TiersCfg(), explicit=True)
    assert (caught.value.requested, caught.value.tier) == ("semantic", t.TIER_SEMANTIC)
    assert (caught.value.done, caught.value.expected) == (0, 7)
    text = str(caught.value)
    assert "0 / 7" in text and "lsearch index --tier 2" in text
    assert "--mode lexical" in text, "拒绝要给出路，不然用户只能猜"


def test_a_named_semantic_mode_on_an_empty_library_says_what_to_look_at() -> None:
    """空库上要语义：正确的下一句不是"去建向量"（建了也没有）而是"先建索引"。

    `0 / 7` 与 `0 / 0` 的修法不同——后者跑完 `--tier 2` 也还是没有向量，因为根本没有
    哪一份文件本该做到那一级。
    """
    with pytest.raises(st.ModeNotReady) as caught:
        st.resolve("semantic", t.ledger_from({}), tiers=TiersCfg(), explicit=True)
    assert (caught.value.done, caught.value.expected) == (0, 0)
    assert "lsearch index" in str(caught.value)
    assert "--tier 2" not in str(caught.value), "空库上让人去建向量是一句办不到的话"


def test_switching_the_semantic_tier_off_makes_a_named_semantic_mode_refuse_too() -> None:
    """配置里 `semantic = false` 时那一级**从来不会做**——与"还没做"是同一句拒绝。

    这一格的判据不能只看台账：`done_at(2)` 也是 0，但原因在配置里，用户要改的是配置。
    """
    led = t.ledger_from({("fulltext", 1): 7})
    tiers = TiersCfg(semantic=False)
    with pytest.raises(st.ModeNotReady) as caught:
        st.resolve("semantic", led, tiers=tiers, explicit=True)
    assert caught.value.tier == t.TIER_SEMANTIC


def test_a_named_hybrid_mode_is_refused_too() -> None:
    """`hybrid` 缺的是**代码**（RRF 归 `F5-05`），所以这一格是 `ModeUnavailable`。

    ⚠ 那句拒绝要说清缺的是哪一块：两个半边各自都能跑，缺的只是"融起来"——
    含糊地说一句"混合检索还没实现"，用户会以为语义那一半也不能用。
    """
    with pytest.raises(st.ModeUnavailable, match="hybrid") as caught:
        st.resolve("hybrid", ready_ledger(), tiers=TiersCfg(), explicit=True)
    text = str(caught.value)
    assert "F5-05" in text and "RRF" in text
    assert caught.value.available == st.IMPLEMENTED_MODES


def test_a_configured_semantic_mode_downgrades_and_says_语义未就绪() -> None:
    """配置里的默认值表达的是一份**偏好**——不降级的话 `lsearch search 关键词`
    开箱即坏，一个连默认配置都跑不起来的工具等于没有。但降级**必须**带 note。

    ⚠ 那句话要说两件事：**出了什么事**（`语义未就绪` + 为什么）与**我拿到的是什么**
    （实际按哪个模式、因此少了什么）。只说"降级了"等于没说——用户要知道的是
    "我这份结果不是语义检索，换个说法就搜不到"。

    ⚠ `F5-04` 之后这一格只对**向量一条都没有**的库成立（`ready_ledger()` 不再是它的
    输入）：那一级有东西可查时 `semantic` 一次都不降级，见下一条。"""
    led = t.ledger_from({("fulltext", 1): 7})
    r = st.resolve("semantic", led, tiers=TiersCfg())
    assert (r.requested, r.mode, r.degraded) == ("semantic", "lexical", True)
    assert r.has(st.NOTE_MODE_DOWNGRADED)
    (note,) = r.notes
    assert "语义未就绪" in note.message
    assert "词法（关键词）" in note.message, "要说清实际按哪个模式"
    assert "换个说法搜不到" in note.message, "要说清用户**因此少了什么**"


def test_a_configured_semantic_mode_does_not_downgrade_on_a_half_built_index() -> None:
    """向量**建了一半**与"一条都没有"是相反的处理——这一条钉住它们分开了。

    `F5-04` 之前这两件事在台账里长得一样（`done_at(2)` 都是 0），于是它们只能是
    同一个回答。现在不是了：一半的向量**给得出**结果，只是覆盖不全——那是"不完整"
    （一条带数字的标注），不是"跑不起来"（换模式）。判据由 `_blocked()` 那一行
    "只问有没有、不问做完没有"划。
    """
    r = st.resolve("semantic", ledger_two_thirds_built(), tiers=TiersCfg())
    assert (r.requested, r.mode, r.degraded) == ("semantic", "semantic", False)
    assert not r.has(st.NOTE_MODE_DOWNGRADED), "有东西可查就不许降级"
    assert [n.tier for n in r.notes] == [t.TIER_SEMANTIC], "只要那一条进度标注"


def test_a_configured_hybrid_mode_downgrades_to_whichever_half_is_left() -> None:
    r"""同一个 `hybrid`，台账不同则落到**不同的半边**——而两句话都要说对。

    ⚠ 这一条同时钉住那句降级话**不是写死的**（`_downgrade_message()` 的文档）：
    它在建好向量的库上说"按语义（向量）检索"，在没建的库上说"按词法（关键词）检索"。
    写死任何一边，另一边就会变成一处**看起来正常**的错话。

    两种情形**共有**的是前半截原因：`hybrid` 缺的是 RRF（`F5-05`），两个半边各自
    都能跑——那句原因在两种情况里都成立，所以不许按台账改（它也不许出现任何模式的
    显示名，否则读起来像在说这一趟会跑哪两个）。
    """
    reason = "混合检索还没实现（缺的是把两路结果融起来的 RRF，归 F5-05：那两路各自都能跑）。"

    half = st.resolve("hybrid", ledger_two_thirds_built(), tiers=TiersCfg())
    assert (half.mode, half.degraded) == ("semantic", True)
    (downgrade,) = [n for n in half.notes if n.code == st.NOTE_MODE_DOWNGRADED]
    assert downgrade.message == reason + "这一趟按语义（向量）检索——只按意思相近找，关键词对不上的搜不到。"
    assert "语义未就绪" not in downgrade.message, "向量有，不能说没有——那正是这一格要分开的"

    none = st.resolve("hybrid", t.ledger_from({("fulltext", 1): 7}), tiers=TiersCfg())
    assert (none.mode, none.degraded) == ("lexical", True)
    (downgrade,) = [n for n in none.notes if n.code == st.NOTE_MODE_DOWNGRADED]
    assert downgrade.message.startswith(reason)
    assert "语义未就绪" in downgrade.message
    assert "按词法（关键词）检索" in downgrade.message


def test_a_mode_nobody_knows_is_the_callers_bug() -> None:
    """`ValueError` 与 `ModeUnavailable` 的分工：前者是"传进来的值本身写错了"，
    后者是"调用方没错，只是这一版还做不到"。混淆两者会让配置里的错字与"功能还没做"
    在日志里长得一样。"""
    with pytest.raises(ValueError, match="mode 只能是"):
        st.resolve("fuzzy", ready_ledger(), tiers=TiersCfg())


def test_the_empty_index_gets_its_own_note_and_nothing_else() -> None:
    """空库与"跑完了但没搜到"必须说不同的话——这正是 `ChunkSearch` 自己说不清的
    那一格（它的类文档里写了）。"""
    r = st.resolve("lexical", t.ledger_from({}), tiers=TiersCfg())
    assert r.has(st.NOTE_EMPTY_INDEX)
    assert len(r.notes) == 1, "空库时再说'文件名索引 0 / 0 没做完'是一句废话"
    assert "lsearch index" in r.notes[0].message


def test_an_unfinished_body_layer_is_announced_with_the_numbers() -> None:
    """`3 / 100` 比"未就绪"有用得多：前者告诉你再等一会儿。"""
    led = t.ledger_from({("metadata", 0): 97, ("fulltext", 1): 3})
    r = st.resolve("lexical", led, tiers=TiersCfg())
    assert [n.code for n in r.notes] == [st.NOTE_TIER_INCOMPLETE]
    (note,) = r.notes
    assert note.tier == t.TIER_FULLTEXT
    assert "3 / 100" in note.message
    assert "正文" in note.message


def test_an_unfinished_name_layer_is_announced_before_the_body_layer() -> None:
    """两级都没做完时两条都发，而且**按级别从小到大**排：说话算数的是最低那一级
    （名字都没进去的库，正文那一级的进度没有意义）。"""
    led = t.ledger_from({("pending", 0): 2, ("metadata", 0): 1})
    assert led.progress(t.TIER_METADATA) == (1, 3)  # 一个名字进去了、两个没有
    r = st.resolve("lexical", led, tiers=TiersCfg())
    assert [n.tier for n in r.notes] == [t.TIER_METADATA, t.TIER_FULLTEXT]
    assert "1 / 3" in r.notes[0].message and "文件名" in r.notes[0].message
    assert "0 / 3" in r.notes[1].message


def test_lexical_mode_says_nothing_about_the_semantic_tier() -> None:
    """词法检索用不上向量。对它报"语义未就绪"是句永远为真的废话——而永远为真的警告
    会被学会忽略，连带着真警告一起被忽略。"""
    led = t.ledger_from({("fulltext", 1): 5})  # Tier1 全做完，Tier2 一个没做
    r = st.resolve("lexical", led, tiers=TiersCfg())
    assert r.notes == ()
    assert all(n.tier != t.TIER_SEMANTIC for n in r.notes)


def test_switching_a_level_off_silences_its_note() -> None:
    """配置里 `fulltext = false` 意味着 Tier1 永远不做——此时还一直警告"正文未就绪"
    就是一句永远为真的废话。判断缺口时只看 `target_tier(tiers)` 以内的级别。"""
    led = t.ledger_from({("metadata", 0): 4})
    loud = st.resolve("lexical", led, tiers=TiersCfg())
    assert [n.tier for n in loud.notes] == [t.TIER_FULLTEXT]
    quiet = st.resolve("lexical", led, tiers=TiersCfg(fulltext=False, semantic=False))
    assert quiet.notes == ()
    assert t.target_tier(TiersCfg(fulltext=False, semantic=False)) == t.TIER_METADATA


def test_a_full_index_that_only_has_terminal_rows_is_still_ready() -> None:
    """真跑一趟全盘之后的常见形态：大部分文件 `skipped`，一条正文都没有——
    **这算就绪**（Tier0 与 Tier1 都是），因为终态那些本来就不该做。"""
    led = t.ledger_from({("skipped", 0): 9000, ("error", 0): 12})
    r = st.resolve("lexical", led, tiers=TiersCfg())
    assert led.ready(t.TIER_METADATA) is True
    assert led.ready(t.TIER_FULLTEXT) is True
    assert r.notes == ()


def test_the_tier2_note_speaks_up_on_a_half_built_semantic_index() -> None:
    """`F5-04` 之后 `semantic` 真能跑，所以这一条不再靠猴子补丁伪造"模式已实现"：
    台账上向量建了一半，`semantic` 照常执行（不降级、不拒绝），只多一条带数字的标注。
    那三级消息与降级表是为这一天写好的，这一条把它钉在真实状态上。"""
    r = st.resolve("semantic", ledger_two_thirds_built(), tiers=TiersCfg(), explicit=True)
    assert r.mode == "semantic" and r.degraded is False
    (note,) = r.notes
    assert note.tier == t.TIER_SEMANTIC
    assert "语义未就绪" in note.message
    assert "3 / 7" in note.message


def test_hybrid_falls_back_to_semantic_before_lexical_once_vectors_exist() -> None:
    """降级表里那个**顺序**表达的是"谁是谁的子集"，而顺序错了不会报错：`P5` 之后
    如果 `hybrid` 直接落到 `lexical`，用户会失去本来能拿到的那一半语义命中。

    ⚠ 这里同时钉住降级那句话**不是写死的**：它按实际生效的模式填，所以
    `hybrid → semantic` 时说的必须是"按语义（向量）检索"而不是"按词法"。
    写死的话，到 `P5` 那天这句话会变成一处**看起来正常**的错话。
    """
    r = st.resolve("hybrid", ledger_two_thirds_built(), tiers=TiersCfg())
    assert r.mode == "semantic"
    (downgrade,) = [n for n in r.notes if n.code == st.NOTE_MODE_DOWNGRADED]
    assert "语义（向量）" in downgrade.message
    assert "词法" not in downgrade.message


def test_lexical_is_the_floor_that_never_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个模式都没有实现时当场炸（`AssertionError`），好过猜一个——`lexical` 是底座。"""
    monkeypatch.setattr(st, "IMPLEMENTED_MODES", frozenset())
    with pytest.raises(AssertionError, match="底座"):
        st.resolve("hybrid", ready_ledger(), tiers=TiersCfg())


def test_the_readiness_pair_is_what_makes_degradation_visible() -> None:
    """`requested` 与 `mode` 分开留着，是"降级"这件事唯一能被看见的地方——
    合成一个字段就等于删掉了它。"""
    r = st.resolve("hybrid", ready_ledger(), tiers=TiersCfg())
    assert r.requested != r.mode
    assert r.has(st.NOTE_MODE_DOWNGRADED)


def test_the_readiness_refuses_an_unknown_mode_before_looking_at_the_ledger() -> None:
    """"值写错了"要在读台账之前就被抓住，否则一个空库会让它走到另一条错路上。"""
    with pytest.raises(ValueError, match="mode 只能是"):
        st.resolve("semantic-ish", t.ledger_from({}), tiers=TiersCfg())
