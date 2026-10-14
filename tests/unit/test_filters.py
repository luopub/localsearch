r"""F2-03：`.lsearchignore` 解析与两层分区。关联验收项：无（A8/A9 与它无关）。

测试分四组，各盯一件不同的事：

1. **解析**——两个分区、行号归属、写坏的规则必须**报错**（不静默忽略）；
2. **匹配**——各类写法各自的语义，以及"后写优先""白名单例外""大小写不敏感"；
3. **两条判定路径互相对齐**——`decide()`（walker 逐层问）与 `explain()`（对着一个
   已拿到的路径补祖先）对同一语料必须给同一个答案；快路径（反向索引）与逐条全扫
   （`_decide_reference`）必须逐条一致。这两条是**语义第 7、8 条**的护栏：它们保证
   的是"优化与解释没有偷偷改掉行为"，而这类改动平时看不出来；
4. **入库的 `.lsearchignore` 本身**——它是规格，所以把它当**用户交来的文件**读一遍：
   关键条目落在正确的分区、行号对得上、没有一行被解析器悄悄丢掉、managed 区块与
   工具生成的完全一致。

第 4 组是这份文件里最值钱的几条：前三组测的是"我写的代码自洽"，只有第 4 组测
"用户真正会碰到的那份规则文件被解释成了什么"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from localsearch.core.config import PathsCfg
from localsearch.ingest import filters
from localsearch.ingest.filters import (
    Action,
    Decision,
    IgnoreError,
    IgnoreRules,
    managed_entries,
    normalize,
    replace_managed_block,
    update_managed_block,
)

#: 入库的规则文件：**规格本身**，不是测试夹具。
IGNORE_FILE = filters.PROJECT_ROOT / ".lsearchignore"

#: 本机默认的 `paths.data_dir`。从代码默认值取而不是抄一遍（计划 §4.5 的模板与
#: 代码默认值必须逐字段相等，那边有测试盯着；这里只是不制造第二份数字）。
DEFAULT_DATA_DIR = PathsCfg().data_dir


def decide(rules: IgnoreRules, path: str, *, is_dir: bool = False) -> Action:
    return rules.decide(path, is_dir=is_dir).action


def walk(rules: IgnoreRules, path: str, *, is_dir: bool = False) -> Decision:
    """模拟 walker：从盘符开始逐层 `decide()`，把上级的处置往下传。

    这是 `explain()` 必须与之对齐的那条路径——真实索引就长这样。
    """
    parts = normalize(path).split("/")
    inherited: Action | None = None
    decision = Decision(Action.INDEX)
    for i in range(1, len(parts) + 1):
        is_leaf = i == len(parts)
        cur = "/".join(parts[:i])
        decision = rules.decide(
            cur, is_dir=(is_dir if is_leaf else True), inherited=inherited
        )
        if decision.action is Action.SKIP:
            return decision
        inherited = decision.action
    return decision


# ==============================================================================
# 1. 解析
# ==============================================================================


def test_rules_before_the_first_marker_default_to_skip() -> None:
    rules = IgnoreRules.parse("*.log\n#@section name-only\n*.zip\n")
    assert decide(rules, "D:/x/a.log") is Action.SKIP
    assert decide(rules, "D:/x/a.zip") is Action.NAME_ONLY


def test_a_later_marker_switches_the_section_back() -> None:
    """入库文件就是这样写的：最后一段"可选规则"回到 `skip`。

    这个测试是给"解析器把后一个标记也认了"留的——只认第一个标记的实现会让
    `*.log`（那一节里被注释掉的例子）落在错误的分区。
    """
    rules = IgnoreRules.parse(
        "#@section skip\n*.a\n#@section name-only\n*.b\n#@section skip\n*.c\n"
    )
    assert decide(rules, "D:/x/f.a") is Action.SKIP
    assert decide(rules, "D:/x/f.b") is Action.NAME_ONLY
    assert decide(rules, "D:/x/f.c") is Action.SKIP


def test_unknown_section_is_an_error() -> None:
    with pytest.raises(IgnoreError, match="第 1 行"):
        IgnoreRules.parse("#@section skip-only\n")


@pytest.mark.parametrize("bad", ["/", "!", "!/", "   /   "])
def test_rule_without_a_path_is_an_error(bad: str) -> None:
    with pytest.raises(IgnoreError):
        IgnoreRules.parse(bad + "\n")


def test_comments_and_blank_lines_are_ignored_but_still_count_for_line_numbers() -> None:
    rules = IgnoreRules.parse("# 注释\n\n   \n*.log\n")
    assert len(rules) == 1
    assert rules.rules[0].line == 4  # 行号是文件里的真实行号，不是规则序号


def test_a_trailing_note_stays_part_of_the_pattern() -> None:
    """`*.log # 注` **不是**"规则 + 行末注释"——规则体就是整行，注释必须独占一行。

    写下来的理由：哪天有人"顺手支持行末注释"，带 `#` 的规则就会静默失效
    （`.~lock.*#` 正是这个仓库里真实存在的一条）。宁可这里保持笨。
    """
    rules = IgnoreRules.parse("*.log # 注\n")
    assert rules.rules[0].pattern == "*.log # 注"


def test_missing_rule_file_reports_instead_of_behaving_like_empty(tmp_path: Path) -> None:
    missing = tmp_path / ".lsearchignore"
    with pytest.raises(IgnoreError, match="找不到规则文件"):
        IgnoreRules.from_file(missing)
    assert len(IgnoreRules.from_file(missing, missing_ok=True)) == 0


# ==============================================================================
# 2. 匹配
# ==============================================================================


def test_directory_rule_matches_a_directory_but_not_a_file_of_the_same_name() -> None:
    """语义第 4 条：`node_modules/` 不该误伤一个叫 `node_modules` 的文件。"""
    rules = IgnoreRules.parse("#@section skip\nnode_modules/\n")
    assert decide(rules, "D:/work/p/node_modules", is_dir=True) is Action.SKIP
    assert decide(rules, "D:/work/p/node_modules", is_dir=False) is Action.INDEX


def test_name_rule_matches_files_and_directories() -> None:
    rules = IgnoreRules.parse("#@section skip\npagefile.sys\n")
    assert decide(rules, "C:/pagefile.sys") is Action.SKIP
    assert decide(rules, "D:/x/pagefile.sys", is_dir=True) is Action.SKIP


def test_extension_rule_matches_at_any_level_and_ignores_case() -> None:
    rules = IgnoreRules.parse("#@section name-only\n*.jpg\n")
    assert decide(rules, "D:/a/b/c/Photo.JPG") is Action.NAME_ONLY
    assert decide(rules, "D:/a/b/c/photo.jpg.bak") is Action.INDEX


def test_absolute_prefix_stops_at_a_path_boundary() -> None:
    """`^C:/Windows` 不该匹配 `C:/WindowsOld`——否则"排除系统目录"会误伤一整棵
    名字碰巧以它开头的目录树，而这种误伤是查不出来的（用户只会觉得"文件不见了"）。"""
    rules = IgnoreRules.parse("#@section skip\n^C:/Windows\n")
    assert decide(rules, "C:/Windows/System32/a.dll") is Action.SKIP
    assert decide(rules, "C:/WindowsOld/a.txt") is Action.INDEX
    assert decide(rules, "C:/Windows", is_dir=True) is Action.SKIP


def test_drive_absolute_without_the_caret_is_still_absolute() -> None:
    rules = IgnoreRules.parse("#@section skip\nD:/work/tmp\n")
    assert decide(rules, "d:/work/tmp/x.txt") is Action.SKIP
    assert decide(rules, "d:/work/other/x.txt") is Action.INDEX


def test_absolute_rule_with_a_glob() -> None:
    rules = IgnoreRules.parse("#@section skip\n^C:/Users/*/AppData/Local/Temp\n")
    assert decide(rules, "C:/Users/luopu/AppData/Local/Temp/a.txt") is Action.SKIP
    assert decide(rules, "C:/Users/luopu/AppData/Local/Other/a.txt") is Action.INDEX


def test_relative_path_rule_needs_a_scan_root() -> None:
    text = "#@section skip\n/doc/build/\n"
    with_root = IgnoreRules.parse(text, roots=("D:/proj",))
    assert decide(with_root, "D:/proj/doc/build/out.pdf") is Action.SKIP
    assert decide(with_root, "D:/other/doc/build/out.pdf") is Action.INDEX
    without_root = IgnoreRules.parse(text)
    assert decide(without_root, "D:/proj/doc/build/out.pdf") is Action.INDEX


def test_a_relative_rule_never_excludes_the_scan_root_itself() -> None:
    """`/build/` 不该把根目录自己排除掉：那等于"什么都不索引"，而用户看不出来。

    真实形状是"某条相对路径规则的第一段恰好与硬盘上的目录同名"（比如根下没有
    build、根自己叫 build 的怪情况）。这里只保证边界写死：相对路径规则对
    `rel == ""`（就是根本身）一律不命中。
    """
    rules = IgnoreRules.parse("#@section skip\n/build/\n", roots=("D:/proj",))
    assert decide(rules, "D:/proj", is_dir=True) is Action.INDEX
    assert decide(rules, "D:/proj/build", is_dir=True) is Action.SKIP


def test_relative_path_rule_with_double_star() -> None:
    rules = IgnoreRules.parse("#@section skip\n/a/**/build/\n", roots=("D:/proj",))
    assert decide(rules, "D:/proj/a/build/x.txt") is Action.SKIP
    assert decide(rules, "D:/proj/a/b/c/build/x.txt") is Action.SKIP
    assert decide(rules, "D:/proj/a/b/c/building/x.txt") is Action.INDEX


def test_question_mark_matches_exactly_one_character() -> None:
    rules = IgnoreRules.parse("#@section skip\nfile?.txt\n")
    assert decide(rules, "D:/x/file1.txt") is Action.SKIP
    assert decide(rules, "D:/x/file10.txt") is Action.INDEX


def test_character_class_and_its_literal_suffix() -> None:
    """`[abc]x.txt` 的后缀是 `x.txt` 而不是 `abc]x.txt`。

    这是快路径里**取错就会静默失效**的一处：规则看着写了，却永远不命中。
    """
    rules = IgnoreRules.parse("#@section skip\n[a-c]over.txt\n")
    assert decide(rules, "D:/x/aover.txt") is Action.SKIP
    assert decide(rules, "D:/x/dover.txt") is Action.INDEX


def test_negation_restores_indexing_when_written_later() -> None:
    rules = IgnoreRules.parse("#@section skip\n*.log\n!keep.log\n")
    decision = rules.decide("D:/x/keep.log", is_dir=False)
    assert decision.action is Action.INDEX
    assert decision.rule is not None and decision.rule.negated
    assert decide(rules, "D:/x/other.log") is Action.SKIP


def test_a_negation_written_earlier_loses_to_a_later_rule() -> None:
    """语义第 3 条：last match wins。反过来写就得反过来的结果。"""
    rules = IgnoreRules.parse("#@section skip\n!keep.log\n*.log\n")
    assert decide(rules, "D:/x/keep.log") is Action.SKIP


def test_a_directory_excluded_before_a_negation_cannot_be_rescued() -> None:
    """文件头写着"父目录被排除后子项无法用 `!` 单独救回"。

    这不是规则的语义，是 **walk 层剪枝**的结果——到了 `!` 该起作用的地方，walker
    早就没往下走了。所以 `decide` 与 `walk` 给出 SKIP，而 `explain`（单独看一条
    已拿到的路径）会给出"白名单例外"。**两者都对，说的是不同的事**：
    一个说"实际会被索引吗"，一个说"按规则文本它被哪条命中"。这条测试把这个区别
    钉住，免得将来有人"统一"它们。
    """
    text = "#@section skip\n^D:/work/secret\n!/D:/work/secret/公开.txt\n"
    rules = IgnoreRules.parse(text, roots=("D:/work",))
    assert walk(rules, "D:/work/secret/公开.txt").action is Action.SKIP
    assert rules.explain("D:/work/secret/公开.txt").rule.negated is True


def test_name_only_on_a_directory_is_inherited_by_its_children() -> None:
    rules = IgnoreRules.parse("#@section name-only\nbigdata/\n")
    assert decide(rules, "D:/x/bigdata", is_dir=True) is Action.NAME_ONLY
    child = rules.decide("D:/x/bigdata/part1.bin", is_dir=False,
                         inherited=Action.NAME_ONLY)
    assert child.action is Action.NAME_ONLY
    assert child.inherited is Action.NAME_ONLY
    assert child.reason.startswith("无规则命中")


def test_inherited_action_can_be_overridden_by_a_rule_on_the_file() -> None:
    """继承是一种**默认**，不是一堵墙：文件自己被一条规则点名时，以那条规则为准。"""
    rules = IgnoreRules.parse("#@section name-only\nbigdata/\n#@section skip\n*.key\n")
    assert rules.decide("D:/x/bigdata/secret.key", is_dir=False,
                        inherited=Action.NAME_ONLY).action is Action.SKIP


def test_oversized_or_unknown_paths_do_not_crash() -> None:
    """R4：超长路径、设备路径前缀、UNC——都不能让判定抛异常。

    遍历全盘时这里抛一次异常，整轮索引就断了（A6 要的是"不中断"）。
    """
    rules = IgnoreRules.parse("#@section skip\n^C:/Windows\n*.log\n")
    for weird in (
        "\\\\?\\C:\\Windows\\System32\\very" + "long" * 40 + "\\a.txt",
        "\\\\server\\share\\x.log",
        "C:",
        "D:/",
        "D:/x/名字里带空格 和中文.log",
    ):
        assert isinstance(rules.decide(weird, is_dir=False).action, Action)


def test_normalize_unifies_the_comparison_form() -> None:
    assert normalize(r"C:\Users\luopu\a.TXT") == "c:/users/luopu/a.txt"
    assert normalize(r"\\?\C:\Users\x") == "c:/users/x"
    assert normalize("D:/a/b/../c/") == "d:/a/c"


def test_cli_overrides_are_appended_and_win_without_touching_the_file() -> None:
    base = IgnoreRules.parse("#@section skip\n*.log\n", roots=("D:/r",))
    rules = base.with_extra(excludes=["*.tmp"], includes=["*.log"])
    assert decide(rules, "D:/r/a.tmp") is Action.SKIP
    assert decide(rules, "D:/r/a.log") is Action.INDEX
    assert decide(rules, "D:/r/a.txt") is Action.INDEX
    # 原对象一字未动：覆盖是临时的（计划 §4.6）
    assert len(base) == 1
    assert decide(base, "D:/r/a.log") is Action.SKIP
    assert "--exclude ×1" in rules.describe_source()


def test_origin_reports_a_line_number_for_file_rules() -> None:
    rules = IgnoreRules.parse("\n\n#@section skip\n*.log\n")
    rule = rules.rules[0]
    assert rule.origin == "第 4 行 *.log"
    assert rule.line == 4


# ==============================================================================
# 3. 两条判定路径互相对齐
# ==============================================================================

CORPUS: tuple[tuple[str, bool], ...] = tuple(
    (p, d)
    for p, d in [
        ("D:/work/proj/a.txt", False),
        ("D:/work/proj/a.log", False),
        ("D:/work/proj/node_modules/x.js", False),
        ("D:/work/proj/node_modules", True),
        ("D:/work/proj/.venv/Lib/site.py", False),
        ("D:/work/proj/doc/build/out.pdf", False),
        ("D:/work/proj/build/main.o", False),
        ("D:/work/proj/photo.JPG", False),
        ("D:/work/proj/iso/x.iso", False),
        ("D:/work/proj/logs/keep.log", False),
        ("D:/work/proj/logs/drop.log", False),
        ("C:/Windows/System32/kernel32.dll", False),
        ("C:/WindowsOld/a.txt", False),
        ("D:/localsearch-data/index.db", False),
        ("D:/localsearch-data", True),
        ("d:/WORK/PROJ/A.TXT", False),
        ("D:/work/proj/Temp/x.txt", False),
        ("D:/work/proj/desktop.ini", False),
        ("D:/work/proj/老资料/年报.docx", False),
    ]
)

REFERENCE_TEXT = (
    "#@section skip\n"
    "^D:/work/proj/doc/build\n"
    "*.log\n"
    "!keep.log\n"
    "#@section name-only\n"
    "*.jpg\n"
    "*.iso\n"
    "#@section skip\n"
    "node_modules/\n"
    ".venv/\n"
    "Temp/\n"
    "*.o\n"
    "desktop.ini\n"
)


@pytest.mark.parametrize(("path", "is_dir"), CORPUS)
def test_decide_and_explain_agree_along_a_walk(path: str, is_dir: bool) -> None:
    """语义第 7 条：walk 的结果 == `explain` 的结果。

    唯一的例外是"父目录被剪枝后子项不可救回"，那时两者故意不同，由
    `test_a_directory_excluded_before_a_negation_cannot_be_rescued` 单独讲清楚。
    这里给出的语料不含那种情形。
    """
    for roots in ((), ("D:/work",)):
        rules = IgnoreRules.parse(REFERENCE_TEXT, roots=roots)
        assert walk(rules, path, is_dir=is_dir).action is rules.explain(
            path, is_dir=is_dir
        ).action


@pytest.mark.parametrize(("path", "is_dir"), CORPUS)
def test_fast_path_matches_the_full_scan(path: str, is_dir: bool) -> None:
    """语义第 8 条：反向索引与逐条全扫必须给出**同一条规则**，不只是同一个动作。"""
    rules = IgnoreRules.parse(REFERENCE_TEXT, roots=("D:/work",))
    got = rules.decide(path, is_dir=is_dir)
    want = filters._decide_reference(rules, path, is_dir=is_dir)
    assert got.action is want.action
    assert got.rule is want.rule


@pytest.mark.parametrize(("path", "is_dir"), CORPUS)
def test_fast_path_matches_the_full_scan_on_the_shipped_file(
    path: str, is_dir: bool
) -> None:
    """同一条护栏，但跑在**用户真正的那份**规则文件上（350 条）。"""
    rules = IgnoreRules.from_file(IGNORE_FILE, roots=("D:/work",))
    got = rules.decide(path, is_dir=is_dir)
    want = filters._decide_reference(rules, path, is_dir=is_dir)
    assert got.action is want.action
    assert got.rule is want.rule


# ==============================================================================
# 4. 入库的 `.lsearchignore` 本身
# ==============================================================================


@pytest.fixture(scope="module")
def shipped_text() -> str:
    return IGNORE_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shipped(shipped_text: str) -> IgnoreRules:
    return IgnoreRules.from_file(IGNORE_FILE, roots=("D:/work",))


def test_shipped_file_parses_one_rule_per_rule_line(
    shipped: IgnoreRules, shipped_text: str
) -> None:
    """没有一行被解析器悄悄丢掉。规则数是"用户以为写了多少条"的直接体现。"""
    lines = [
        ln.strip()
        for ln in shipped_text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert [r.raw for r in shipped] == lines


def test_shipped_file_line_numbers_point_at_those_lines(
    shipped: IgnoreRules, shipped_text: str
) -> None:
    """行号是 1-based，且**指向文件里的真实行**——`--dry-run` 要靠它报命中数。"""
    lines = shipped_text.splitlines()
    for rule in shipped:
        assert lines[rule.line - 1].strip() == rule.raw


def test_shipped_file_declares_both_sections(shipped: IgnoreRules) -> None:
    sections = {r.section for r in shipped}
    assert sections == {filters.SECTION_SKIP, filters.SECTION_NAME_ONLY}


@pytest.mark.parametrize(
    ("path", "is_dir", "expected", "why"),
    [
        ("D:/work/proj/node_modules/x.js", False, Action.SKIP, "依赖目录"),
        ("D:/work/proj/.venv/Lib/site.py", False, Action.SKIP, "虚拟环境"),
        ("D:/work/proj/__pycache__/m.pyc", False, Action.SKIP, "缓存目录"),
        ("D:/work/proj/a.pyc", False, Action.SKIP, "编译产物"),
        ("D:/work/proj/Temp/x.txt", False, Action.SKIP, "临时目录"),
        # ⚠ 这一条**不是** skip：`Thumbs.db`（第 175 行，skip）被后面第 343 行的
        # `*.db`（name-only）覆盖了。后写优先的规则是对的，覆盖也是真的——
        # 入库文件里 `*.db` 那一节在线号上晚于"系统杂项"，所以 Thumbs.db 最终是
        # name-only。这类覆盖正是 `--dry-run` 要让人看见的东西，故如实钉在这里。
        ("D:/work/proj/Thumbs.db", False, Action.NAME_ONLY, "被后面的 *.db 覆盖"),
        ("D:/work/proj/dump.etl", False, Action.SKIP, "事件日志"),
        ("D:/work/proj/notes.log", False, Action.INDEX, "日志**默认要索引**"),
        ("D:/work/proj/photo.jpg", False, Action.NAME_ONLY, "媒体：只留名字"),
        ("D:/work/proj/backup.iso", False, Action.NAME_ONLY, "镜像：只留名字"),
        ("D:/work/proj/old.doc", False, Action.NAME_ONLY, "旧版 Office：无正文"),
        ("D:/work/proj/a.exe", False, Action.NAME_ONLY, "可执行文件"),
        ("D:/work/proj/pic.svg", False, Action.INDEX, "SVG 是文本，**故意**没排"),
        ("D:/work/proj/年报.docx", False, Action.INDEX, "要索引的目标"),
        ("D:/work/proj/code/main.py", False, Action.INDEX, "要索引的目标"),
        ("C:/Windows/System32/kernel32.dll", False, Action.SKIP, "系统目录"),
        ("C:/WindowsOld/a.txt", False, Action.INDEX, "前缀边界"),
        ("D:/localsearch-data/index.db", False, Action.SKIP, "managed 区块"),
        ("D:/localsearch-data", True, Action.SKIP, "managed 区块（数据目录本身）"),
        ("D:/work/test/localsearch/.venv/x.py", False, Action.SKIP, "managed 区块"),
    ],
)
def test_shipped_file_puts_real_paths_in_the_right_bucket(
    shipped: IgnoreRules, path: str, is_dir: bool, expected: Action, why: str
) -> None:
    assert walk(shipped, path, is_dir=is_dir).action is expected, why


def test_shipped_file_does_not_exclude_a_file_named_like_an_excluded_dir(
    shipped: IgnoreRules,
) -> None:
    assert decide(shipped, "D:/work/proj/node_modules", is_dir=False) is Action.INDEX


def test_managed_block_matches_what_the_tool_would_generate(
    shipped_text: str,
) -> None:
    """入库文件的 managed 区块与工具生成的**逐字节一致**。

    它保证两件事：默认 `data_dir` 没被改过（改了这里就会红），以及
    `update_managed_block` 不会在第一次真实运行时把这一块重写一遍——那会让"这个
    文件什么时候被动过"每次都指向今天。
    """
    entries = managed_entries(DEFAULT_DATA_DIR)
    assert replace_managed_block(shipped_text, entries) == shipped_text


def test_replace_managed_block_keeps_everything_outside(
    shipped_text: str, tmp_path: Path
) -> None:
    copy = tmp_path / ".lsearchignore"
    shutil.copyfile(IGNORE_FILE, copy)
    before = copy.read_text(encoding="utf-8")
    other = "D:/elsewhere/data"
    assert update_managed_block(copy, managed_entries(other)) is True
    after = copy.read_text(encoding="utf-8")
    assert f"^{other}" in after
    # 区块外的每一行都还在，且顺序未变
    outside_before = before.split(filters.MANAGED_BEGIN)[0]
    outside_after = after.split(filters.MANAGED_BEGIN)[0]
    assert outside_before == outside_after
    assert before.split(filters.MANAGED_END)[1] == after.split(filters.MANAGED_END)[1]


def test_update_managed_block_is_idempotent(shipped_text: str, tmp_path: Path) -> None:
    copy = tmp_path / ".lsearchignore"
    shutil.copyfile(IGNORE_FILE, copy)
    entries = managed_entries(DEFAULT_DATA_DIR)
    assert update_managed_block(copy, entries) is False
    assert copy.read_text(encoding="utf-8") == shipped_text


def test_managed_block_without_markers_is_an_error() -> None:
    with pytest.raises(IgnoreError, match="没有 .* 标记"):
        replace_managed_block("*.log\n", ["^D:/x"])
    with pytest.raises(IgnoreError, match="找不到"):
        replace_managed_block(f"{filters.MANAGED_BEGIN}\n^D:/x\n", ["^D:/x"])


def test_managed_entries_are_absolute_prefix_rules_that_actually_match() -> None:
    """生成的每条规则都得是**能命中**的规则——写成别的东西（比如少个 `^`）会安静地
    什么都不排，而症状是"索引库把自己索引进去了"。"""
    rules = IgnoreRules.parse(
        "\n".join(managed_entries(DEFAULT_DATA_DIR)) + "\n"
    )
    assert decide(rules, f"{DEFAULT_DATA_DIR}/index.db") is Action.SKIP
    assert decide(rules, f"{filters.PROJECT_ROOT}/.venv/Lib/site.py") is Action.SKIP
