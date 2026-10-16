r"""F2-06：`lsearch index --dry-run` 与它的账目模块。测试落点。

被测模块有两个，测试也在两半：

1. **`ingest/report.py`**（分桶与计数）——纯内存，不需要配置文件、不碰磁盘。
2. **`cli/commands/index.py`** 的 `--dry-run` 那一半——`F2-06` 时它有两件事同样重要：
   **该拒绝的时候拒绝**，以及 **dry-run 不留任何落点**（不写库、不写日志、连数据目录
   都不建）。`F4-04` 之后前一条换了个对象（拒绝的是配置错误，不再是"真索引还没落地"，
   见第 1 节那段注释）；**真索引**那一半归 `tests/unit/test_index_resume.py`，
   本文件不再管它。

------------------------------------------------------------------------------
这一半为什么必须自己有一份 sandbox
------------------------------------------------------------------------------

`test_cli.py` 里那份 sandbox 长得一样但不共用：`tests/` 下**没有 `__init__.py`**
（只有 `tests/unit/`），`pyproject.toml` 的 `testpaths = ["tests"]` 也没有 `addopts`，
所以测试模块之间只能靠 `sys.path` 碰巧同名导入——`from test_cli import sandbox`
在"单独跑这个文件"与"跑整个 tests/"两种情形下行为不同。**复制一份、不共用**是刻意的
取舍：多十几行，换掉一整类只在某种跑法下才出现的失败。（哪天真要共用，就在
`tests/conftest.py` 里建 fixture，而不是跨模块 import。）

与 `test_cli.py` 那份的一处**实质区别**：这里不 monkeypatch `config_mod.PROJECT_ROOT`，
而是把 `[scan] ignore_file` 写成**绝对路径**。理由是 `PROJECT_ROOT` 在
`ingest/filters.py` 与 `ingest/report.py` 里各有一份自己的引用，
patch `core.config` 那个名字**改不动它们**——那样写出来的测试会以为规则文件在
tmp_path，实际去读仓库根，症状是"在我机器上过、在 CI 上找不到 `.lsearchignore`"。
`resolve_ignore_file` 对绝对路径原样放行，正是为了这里。

------------------------------------------------------------------------------
`COLUMNS = 300`：不是排版偏好，是为了断言不飘
------------------------------------------------------------------------------

报表是 rich 画的带框表格，**单元格宽度不够时会折行**（路径尤其容易），一折行，
行内的子串就断了。这个文件里的断言要么认死一行的内容（`row_containing`），
要么认窄列里的短词，所以把控制台调宽到"这一行必然不折"。
`rich` 在非终端下读 `COLUMNS` 环境变量，所以 `monkeypatch.setenv` 就够。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from localsearch.cli.main import main
from localsearch.core.config import ENV_CONFIG_PATH
from localsearch.core.logging import reset_logging
from localsearch.ingest.base import ScanEntry, marker_reason
from localsearch.ingest.filters import NOINDEX_MARKER, Action, IgnoreRules
from localsearch.ingest.report import (
    KIND_MARKER,
    KIND_OTHER,
    KIND_RULE,
    OTHER_PREFIX,
    TABLE_NAME_ONLY,
    TABLE_SKIPPED,
    WHERE_COMMAND_LINE,
    WHERE_NONE,
    Distribution,
    Hits,
    bucket,
    is_marker_reason,
    rule_key,
)

#: 规则文件的内容。**行号是断言的一部分**（报表里印的是"skip 第 3 行"），
#: 所以不要往中间插行：改动这里就必须同时改把行号写进断言的那几条测试。
IGNORE_LINES = (
    "# 中文注释：这一行不进规则表",  # 1  注释不是规则
    "#@section skip",  # 2  分区标记也不是规则
    "*.log",  # 3  → 命中 note.log
    "*.never",  # 4  → 一条都不命中
    "build/",  # 5  → 命中目录 build（整棵子树）
    "#@section name-only",  # 6
    "*.ts",  # 7  → 命中 sheet.ts，只记名字
)

#: 真规则条数。**推导出来**，不写 4：加了注释行却忘了改常数，会让报表里那句
#: "（4 条）"继续印着一个假的数。
RULES_TOTAL = sum(1 for line in IGNORE_LINES if line.strip() and not line.startswith("#"))


@pytest.fixture(autouse=True)
def clean_logging():
    """每个测试前后把 CLI 装到根 logger 上的 handler 摘干净（同 `test_cli.py`）。"""
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """一棵小树 + 一份配置 + 一份规则文件，全部落在 tmp_path 里。

    树里的每一样东西都对应一条断言，不是随手铺的：

    - `keep.txt` 正常索引；`note.log` 被规则排除；`sheet.ts` 只记名字；
    - `big.bin` 1.5 MiB —— 默认上限 64 MiB 下它是**正文**，`--max-file-mb 1`
      时才超限，正好用来钉"命令行参数压过配置值"这条分支；
    - `build/` 被目录规则剪掉，里面还有一层（剪掉一棵子树 ≠ 剪掉一个文件）；
    - `marked/` 里有 `.lsearch-noindex`，标记剪掉的是**目录自己**（不参与规则归因）；
    - `hidden.txt` 在标记目录里，一条都不该出现在任何一栏。
    """
    tree = tmp_path / "tree" / "docs"
    (tree / "build" / "深").mkdir(parents=True)
    marked = tree / "marked"
    marked.mkdir()
    (tree / "keep.txt").write_text("k", encoding="utf-8")
    (tree / "note.log").write_text("l", encoding="utf-8")
    (tree / "sheet.ts").write_text("t", encoding="utf-8")
    (tree / "big.bin").write_bytes(b"\0" * 1_600_000)
    (tree / "build" / "inner.txt").write_text("i", encoding="utf-8")
    (tree / "build" / "深" / "o.txt").write_text("o", encoding="utf-8")
    (marked / "hidden.txt").write_text("h", encoding="utf-8")
    (marked / NOINDEX_MARKER).write_text("", encoding="utf-8")

    ignore = tmp_path / "ignore.txt"
    ignore.write_text("\n".join(IGNORE_LINES) + "\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'roots = ["{tree.as_posix()}"]\n'
        "[scan]\n"
        # 绝对路径：见文件头"为什么必须自己有一份 sandbox"
        f'ignore_file = "{ignore.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    # 见文件头：让每行都排得下，断言才不会被折行切断
    monkeypatch.setenv("COLUMNS", "300")
    return {"cfg": cfg, "data_dir": data_dir, "tree": tree, "ignore": ignore}


def run(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    """跑一条命令，返回 `(退出码, stdout, stderr)`（都是原文，未压平）。"""
    code = main(["--config", str(sandbox["cfg"]), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def flat(text: str) -> str:
    """压成一行再断言（同 `test_cli.py`：rich 会折行，原文比对会飘）。"""
    return " ".join(text.split())


def row_containing(out: str, label: str) -> str:
    """表格里含 `label` 的那一整行，压平后返回。

    逐列断言要跟空格数量绑定，太脆；整行比对则只依赖**列的顺序**（目录、文件、
    大小、归属、规则、例）——那个顺序是 `_print_rows` 写死的，改它本来就要改测试。
    """
    for line in out.splitlines():
        if label in line and "│" in line:
            return flat(line)
    raise AssertionError(f"报表里没有含 {label!r} 的行：\n{out}")


# ==============================================================================
# 1. 两条路都不许有落点：`--dry-run` 与"被拒绝"
# ==============================================================================
#
# `F2-06` 的版本在这里钉的是"没有 `--dry-run` 就拒绝、且拒绝不留落点"（那时真索引
# 还没落地，缺 P3/P4/P5 三块）。`F4-04` 把真索引做出来了，那两条随之作废：
# 缺的三块已经不缺、`index` 也不再靠"自己会拒绝"换来注册。**新的那一条**是同一个
# 主题的延续：拒绝仍然不该有落点，只是现在被拒绝的是**配置错误**而不是"没实现"。
# 真索引自己的账（建库、建日志、退出码）在 `test_index_resume.py` 里。


def test_a_refused_config_error_leaves_no_trace(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    r"""`index --all-drives` 被拒绝（退出码 2），且**连数据目录都不建**。

    这是同一个主题在 `F4-04` 之后的形态。它同时钉住一个**真实发生过的回归**：
    `F4-04` 把 `make_scanner()` 从 `_dry_run()` 里拉出来时漏了那道
    `except ScanError`，于是 `all_drives` 报错的那条路**甩一屏 traceback、退出码 1**。
    退出码 1 在计划 §4.8 里的意思是"跑完了但有文件没成"——启动器会把它当成"该看一眼"
    而不是"配置错了"，用户拿到的是一句"索引中止"的谎话。

    落点这一半同样重要：扫描器是在 `SqliteStore.open()` **之前**造的（`_open_scanner`
    的文档记着理由）。顺序反了的话，一次被拒绝的 `index` 会留下一个**空库**，
    而那个空库会让之后每一次 `lsearch search` 都以为"索引已经建好了"。
    """
    data_dir = sandbox["data_dir"]
    code, out, err = run(sandbox, capsys, "index", "--all-drives")
    text = flat(out + " " + err)
    assert code == 2, f"配置错误应当是退出码 2，不是崩溃也不是 1：{text}"
    assert "无法开始" in text and "E10" in text
    assert "Traceback" not in text
    assert not data_dir.exists(), "被拒绝的那一趟在数据目录里留下了东西"

    # `--dry-run` 是**同一条**路（同一个扫描器、同一句拒绝），只差要不要落库
    assert run(sandbox, capsys, "index", "--dry-run", "--all-drives")[0] == 2
    assert not data_dir.exists()


def test_dry_run_leaves_no_trace_either(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` 说的"不落库"是**字面的**：库、日志、数据目录，一样都没有。

    它对用户唯一的价值就是「先看看会怎样」；一旦留下落点，这份承诺就不成立了。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    assert code == 0
    assert "不写索引库" in flat(out)
    assert not sandbox["data_dir"].exists(), (
        "dry-run 在数据目录里留下了东西：它对用户唯一的价值就是「先看看会怎样」，"
        "一旦有落点，这份承诺就不成立了"
    )


# ==============================================================================
# 2. 逐规则命中：行号、目录/文件分开数
# ==============================================================================


def test_each_rule_is_reported_with_its_line_number(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """命中要落到**具体哪一行**：`第 3 行 *.log`。

    用户手上只有规则文件，"这条规则排掉了多少"里的"这条"只能按行号指认。
    同时钉住：注释与分区标记都不是规则（条数推导自 `RULES_TOTAL`）。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    text = flat(out)
    assert code == 0
    assert f"（{RULES_TOTAL} 条）" in text
    assert "中文注释" not in text
    # 行号与规则原文在不同的列里，中间隔着表格的竖线——`"skip 第 3 行 *.log"` 这种
    # 连写的断言在**真报表**上永远不成立（第一版就是这么写的）。
    assert "*.log" in row_containing(out, "skip 第 3 行")
    assert "*.ts" in row_containing(out, "name-only 第 7 行")


def test_a_pruned_directory_is_counted_as_a_directory(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`build/` 那一行必须是 **1 个目录 / 0 个文件 / 0.0 MiB**。

    F2-06 记下的第二个必做项。剪掉一个目录会带走**整棵子树**，与剪掉一个文件不是
    一个量级；把两者合成一个"排掉 1 条"，用户就没法判断自己那条规则到底省了多少。
    字节只算文件（目录的 `st_size` 在 NTFS 上是 0 或目录项大小，加进去是个假数）。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    assert code == 0
    row = row_containing(out, "skip 第 5 行")
    assert re.match(r"^│\s*1\s*│\s*0\s*│\s*0\.0 MiB\s*│\s*skip 第 5 行\s*│\s*build/", row), row
    # 剪掉的是 build 与 marked 两个目录（后者见下一条测试）
    assert "剪掉 2 / reparse 0" in flat(out)
    # 子树里的文件一条都不该出现：目录被剪掉 = 根本没下潜
    assert "inner.txt" not in flat(out)


def test_the_marker_gets_its_own_section_not_the_system_column(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`.lsearch-noindex` 剪掉的东西按**标记自己一栏**报，不进「规则之外的系统判断」。

    F2-05 的遗留第 8 条，也是 F2-06 的必做项之一。混着有两个具体的坏处：
    那几类的成因与对策完全不同（reparse 要决定"云占位文件算不算搜得到"、
    `data_dir` 是刻意的自我排除、超限是阈值问题）；而标记是**用户亲手放的**，
    量级是"一个标记剪掉一整棵子树"，恰恰是他在 `--dry-run` 里最想核对的一项。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--max-file-mb", "1")
    text = flat(out)
    assert code == 0

    marker_at = text.index("被排除掉的（目录级标记 .lsearch-noindex）")
    name_only_at = text.index("只记名字的（留着，但不读正文，按规则）")
    assert "整棵子树不索引" in text[marker_at:name_only_at], (
        "标记的理由没印在标记自己那一段里"
    )
    # 剪掉的是目录自己（标记在目录里，判定发生在下潜之前）
    marker_row = row_containing(out, "整棵子树不索引")
    assert re.match(r"^│\s*1\s*│\s*0\s*│\s*0\.0 MiB\s*│", marker_row), marker_row
    assert f"│ {WHERE_NONE} │" in marker_row, (
        "标记没有行号可写，归属栏不该出现一个行号"
    )
    # 标记目录里的文件一条都不该出现
    assert "hidden.txt" not in text

    # 「规则之外」在**被排除**那张表里压根不出现——标记没有把它撑起来。
    # F2-06 之前它一定出现：标记剪掉的目录正是被记到那一栏里的，而在这棵树上
    # 它是那一栏**唯一**的成员（其余系统理由都落在"只记名字"那边）。
    assert "被排除掉的（规则之外的系统判断）" not in text, (
        "标记又被并回「规则之外」了：这正是 F2-05 遗留第 8 条要修掉的那件事"
    )
    # 超限那条走的是"只记名字"（`ingest/base.py` 第 2 条：超限只降级、不排除），
    # 所以它落在另一张表的「规则之外」里，而标记同样不在那里
    name_only_other = text.split("只记名字的（规则之外的系统判断）")[1]
    assert "max_file_mb" in name_only_other
    assert "整棵子树不索引" not in name_only_other


def test_rules_that_only_keep_the_name_have_their_own_table(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """只记名字的规则（`*.ts`）单独一张表，且带行号。

    它与"被排除掉的"是两件事：一个文件是"不读正文"还是"根本不进索引"，
    决定了 `A4`（Tier0 仅文件名可搜）能不能覆盖它。混在一张表里就分不出来了。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    text = flat(out)
    assert code == 0
    section = text.split("只记名字的（留着，但不读正文，按规则）")[1].split(
        "一条都没管到的规则"
    )[0]
    assert "name-only 第 7 行" in section
    assert "*.ts" in section
    assert "sheet.ts" in row_containing(out, "name-only 第 7 行")
    # 反向：被排除掉的那张表里不该有它（否则"只记名字"是假的）
    skipped = text.split("被排除掉的（按规则）")[1].split("被排除掉的（目录级标记")[0]
    assert "sheet.ts" not in skipped


def test_a_file_under_the_size_limit_is_full_text_not_name_only(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--max-file-mb` 不给就用配置里的 64：1.5 MiB 的 `big.bin` 是**正文**。

    钉的是 `max_file_mb is None → 用配置值` 这条分支。写反的症状很安静：
    默认把所有文件都降级成"只记名字"，搜索照样能跑，只是永远搜不到正文。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    text = flat(out)
    assert code == 0
    assert "正文 2（" in text  # keep.txt + big.bin
    assert "只记名字 1（超限 0 已计入）" in text


def test_the_max_file_mb_flag_overrides_the_config_value(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--max-file-mb 1` 之后 `big.bin` 变成"只记名字"，且理由进」规则之外「那一栏。

    超限**不是**规则判的（`ingest/base.py` 第 2 条），所以它不能记到任何一条规则头上；
    只记名字而不是直接排除，是因为"文件名叫什么"这件事上 64 MiB 的日志与 1 KiB
    的笔记没有区别。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--max-file-mb", "1")
    text = flat(out)
    assert code == 0
    assert "正文 1（" in text
    assert "只记名字 2（超限 1 已计入）" in text
    section = text.split("只记名字的（规则之外的系统判断）")[1]
    assert "max_file_mb" in section
    assert "big.bin" in section


def test_two_over_limit_files_land_in_a_single_row(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """同一条系统理由的 N 条记录必须落在**同一行**，且理由里的 `[scan]` 要印得出来。

    两个坑各自都会**安静**地坏掉：

    - `report.bucket` 拿理由的前 `OTHER_PREFIX`（28）个字符当归并键，所以理由里
      **逐条会变的部分必须排在那之后**（对这条理由就是文件大小）。第一版把大小写进了
      前缀，一次真实全盘 dry-run 里同一条理由裂成 8 行、每行 1~8 个文件
      （2026-10-15 实测，原始输出见 `docs/reports/F2-06-逐规则命中分布.md`）——
      看的人会以为那是 8 种不同的成因，而它其实是同一句话的 8 个数字；
    - 理由原文里的 `[scan]` 是配置节的名字，rich 把方括号当样式标记**连词一起吃掉**，
      终端上印成 `超过  max_file_mb = 64`（多一个空格、少一个词，同样不报错）。

    两个文件大小不同才测得出：相同大小的话，即使大小排在分组前缀里也照样只出一行。
    """
    tree = sandbox["tree"]
    (tree / "big2.bin").write_bytes(b"\0" * 2_500_000)  # 2.4 MiB，与 1.5 MiB 的 big.bin 不同

    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--max-file-mb", "1")
    assert code == 0
    rows = [line for line in out.splitlines() if "max_file_mb" in line and "│" in line]
    assert len(rows) == 1, (
        "同一条超限理由裂成了多行——理由里会变的部分（文件大小）跑到分组前缀里去了。"
        "要改的是 `scanner_scandir.py` 里那句话的**语序**，不是 `OTHER_PREFIX`：\n"
        + "\n".join(rows)
    )
    row = row_containing(out, "max_file_mb")
    assert re.match(r"^│\s*0\s*│\s*2\s*│", row), row  # 0 个目录 / 2 个文件，都在这一行
    assert "[scan]" in row, f"理由里的 [scan] 被 rich 吃掉了：{row}"
    # 行里印的是**分组前缀**，所以这句话的长度本身就是证据：前缀已经越过了阈值、
    # 进到下一个分句（"：只记名字与元数据"），说明大小排在它后面。
    assert "：只" in row, row


def test_brackets_in_paths_and_patterns_survive_rich(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """方括号不是样式标记的专利：真实磁盘路径里有（`node_modules/[foo]` 这种）。

    `rich` 对**认不出来的**方括号的处理是"连后面的文字一起吞掉"，不报错，所以
    `_print_rows` 里三处从数据来的文本（归属、规则原文、例）都过 `escape()`。
    这条同时钉两处：`规则` 那一列（glob 的字符组 `[.]` 是常见写法）与
    `例` 那一列（路径里有方括号的目录）。

    `*[.]xyz` 而不是 `*.xyz`：`fnmatch` 里 `[.]` 是"一个点"，两者匹配同一个东西，
    但前者带方括号，才能测到转义。
    """
    tree = sandbox["tree"]
    logs = tree / "logs[1]"
    logs.mkdir()
    (logs / "data.xyz").write_text("x", encoding="utf-8")

    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--exclude", "*[.]xyz")
    assert code == 0
    row = row_containing(out, "[.]xyz")
    assert "*[.]xyz" in row, f"规则原文里的方括号没印出来：{row}"
    assert "logs[1]/data.xyz" in row, f"路径里的方括号把后面的文字吃掉了：{row}"


# ==============================================================================
# 3. 一条都没管到的规则
# ==============================================================================


def test_zero_hit_rules_are_listed_and_both_readings_are_named(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`*.never` 一条都没命中，要出现在榜上；两种成因**必须并列说**。

    只列"命中 > 0"的规则回答不了"这份 284 条的文件里有多少条其实没生效"——
    **没命中的那些恰恰是答案**（F2-03 遗留第 1 条的推广）。而两种成因在这里
    分不开（被后面的规则完全盖掉 / 压根没匹配上），把这一栏读成前者会得出错误结论，
    所以文案里必须两种都写。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run")
    text = flat(out)
    assert code == 0
    assert f"一条都没管到的规则 1 / {RULES_TOTAL} 条" in text
    section = text.split("一条都没管到的规则")[1]
    assert "第 4 行 *.never" in section
    assert "第 3 行 *.log" not in section, "`*.log` 命中过 note.log，不该出现在这里"
    assert "被后面的规则完全盖掉" in section
    assert "压根没匹配上" in section


# ==============================================================================
# 4. 命令行临时规则（不写回文件）
# ==============================================================================


def test_exclude_appends_a_command_line_rule(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--exclude` 追加的规则排在最后（后写优先），归属栏写「命令行」。

    "命令行"而不是"第 0 行"：`line == 0` 是"这条不在文件里"的哨兵值
    （`filters.Rule.origin` 按它特判），印成行号等于把人引去看一个不存在的行，
    而用户真去 `sed -n 0p` 只会看到空。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--exclude", "*.ts")
    text = flat(out)
    assert code == 0
    assert "另有命令行临时规则：--exclude ×1" in text
    assert "第 0 行" not in text
    row = row_containing(out, "*.ts")
    assert f"│ {WHERE_COMMAND_LINE} │ *.ts" in row, row
    assert re.match(r"^│\s*0\s*│\s*1\s*│", row), row
    # 它把 `*.ts` 从"只记名字"提升成了"被排除"，所以那张表空了
    assert "只记名字 0（超限 0 已计入）" in text
    assert "只记名字的：（没有这样的条目）" in text


def test_include_rescues_a_file_and_the_bang_is_stripped(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--include '!*.log'` 把 `note.log` 救回来——**写不写 `!` 都要救得回来**。

    `with_extra` 收的是**模式本身**（`!` 在读 `.lsearchignore` 那一层就被剥掉了）。
    不剥的话模式会带着 `!` 去匹配，结果是**一条都不命中、且不报错**：正文数少一条，
    没有任何一行输出会提示这件事。所以这条测试的断言是"笔记数变没变"，
    而不是"输出里有没有 `!`"——后者在坏版本上也是绿的。
    """
    code, out, _ = run(sandbox, capsys, "index", "--dry-run", "--include", "!*.log")
    text = flat(out)
    assert code == 0
    assert "另有命令行临时规则：--include ×1" in text
    assert "正文 3（" in text, "`!*.log` 没生效：带了 `!` 的模式一条都匹配不上，且不报错"
    assert "排除 0" in text
    # 被 `!` 救回的文件进的是"正文"，所以文件里那条规则变成一条都没管到
    section = text.split("一条都没管到的规则")[1]
    assert "第 3 行 *.log" in section


# ==============================================================================
# 5. 开不起来的情形：报错，不是崩溃
# ==============================================================================


def test_a_missing_ignore_file_is_a_clear_error(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """规则文件不存在时报错退出，**不静默当成空规则**跑。

    静默当空的症状是"什么都没排除"，会把 `.venv` 和索引库自己一起扫进来——
    而报表上看起来一切正常。这里退出码是 2（用法/配置问题），不是崩溃。
    """
    sandbox["cfg"].write_text(
        "[paths]\n"
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        f'roots = ["{sandbox["tree"].as_posix()}"]\n'
        "[scan]\n"
        'ignore_file = "' + (tmp_path / "nope.txt").as_posix() + '"\n',
        encoding="utf-8",
    )
    code, out, err = run(sandbox, capsys, "index", "--dry-run")
    text = flat(out + " " + err)
    assert code == 2
    assert "找不到规则文件" in text
    assert "Traceback" not in text


def test_all_drives_is_refused_with_the_measured_reason(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--all-drives` 直接报错，理由带着实测编号（`E10`）。

    本机 `E:` 报 `DRIVE_FIXED`，与 C:/D: 的读数完全一样，所以"枚举所有固定盘"
    会把会掉线的卷当成一直在的卷。这条报错的理由必须留下证据编号，否则下一个人
    会把它当成"还没实现"顺手补上。
    """
    code, out, err = run(sandbox, capsys, "index", "--dry-run", "--all-drives")
    text = flat(out + " " + err)
    assert code == 2
    assert "GetDriveType" in text
    assert "E10" in text


def test_options_that_nothing_can_honour_are_not_declared(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--workers` / `--rebuild` / `--prune` **故意没声明**，报的是用法错误。

    计划 §4.6 给 `index` 列了它们，但这一版兑现不了：`--workers` 是**嵌入**的并行
    （Tier1 那一段是单进程顺序跑，`P5` 才有意义），`--rebuild` / `--prune` 要"按路径
    删行"，那条路要处理 `files_fts` 的孤儿行与 `R14` 的"只在真正可达的卷内删"
    （归 `F6-03`）。声明了却不用，用户读到的是一句谎话（`--workers 8` 给了、没反应）；
    **偷偷忽略**更糟——同类的先例是 `backend = "mft"` 直接报错而不是静默按 scandir 跑。

    ⚠ 这一条在 `F4-04` **缩过一次**：原先里面还有 `--tier` 与 `--resume`，它们随真索引
    一起落地了（行为在 `test_index_resume.py` 的 CLI 那一段里逐条钉住）。名单要跟着
    缩小——留着的话，这条测试会挡一个已经不存在的"没实现"。
    """
    for option in ("--workers", "--rebuild", "--prune"):
        code, out, err = run(sandbox, capsys, "index", option, "8")
        assert code == 2, f"{option} 被声明了，而这一版兑现不了它"
        assert "No such option" in flat(out + err)


def test_the_help_text_keeps_the_bracketed_section_name(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--help` 里那句 `[paths]` 必须原样出现。

    Typer 的帮助是 rich 画的，而**方括号是样式标记**：裸写 `[paths]` 会被吃掉，
    输出变成"缺省用配置里  roots"——一个少了主语的句子，看起来只是排版不好。
    第一版就是这样（2026-10-15 实测）。这与 `doctor.py` 里 `escape()` 要防的是
    同一件事，所以帮助文本改动后要重跑这一条。
    """
    code, out, _ = run(sandbox, capsys, "index", "--help")
    assert code == 0
    assert "[paths] roots" in flat(out)


# ==============================================================================
# 6. 本机默认控制台是 GBK
# ==============================================================================


def test_the_report_is_printable_on_a_gbk_console(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """整份报表必须能在代码页 936 下打印出来。

    这不是洁癖，是踩过的坑（F2-05，2026-10-15 01:24:49）：那行里的减号是 U+2212，
    GBK 编不出来，`print` 抛 `UnicodeEncodeError`——**整轮输出一条都没留下**，
    包括出错之前已经打印的几十行。报表是最需要被完整看见的东西，
    所以这里把整份输出按 gbk 编一遍。

    宽字符、`…`、`—`、`×`、`→` 都没问题，**唯独 `−`（U+2212）不行**：
    减号一律写 ASCII 的 `-`。
    """
    code, out, err = run(sandbox, capsys, "index", "--dry-run", "--max-file-mb", "1")
    assert code == 0
    assert out and err == ""
    out.encode("gbk")  # 编不出来就在这里炸，而不是在用户终端上炸


# ==============================================================================
# 7. `ingest/report.py`：分桶与计数（纯内存，不经命令行）
# ==============================================================================
#
# 上面那些走的是真命令行，能证明"接线接对了"；但分桶本身的几条判据在那种层次上
# 没法逐个构造（比如"规则命中了、但理由其实是目录标记"）。这里直接喂 `ScanEntry`。
#
# 两份实现的分工写在 `report.py` 的模块文档里：**只算数、不排版**。CLI 与
# `tools/scan_report.py` 读同一份 `Distribution`，所以分桶错一次是**两处一起错**，
# 这也是它必须有自己测试的原因。

#: 行号又是断言的一部分：`*.log` 在第 2 行、`build/` 在第 3 行、`*.ts` 在第 5 行。
RULES_TEXT = "#@section skip\n*.log\nbuild/\n#@section name-only\n*.ts\n"


def tiny_rules() -> IgnoreRules:
    return IgnoreRules.parse(RULES_TEXT, roots=("C:/t",))


def make_entry(
    rules: IgnoreRules,
    path: str,
    *,
    is_dir: bool,
    action: Action = Action.SKIP,
    reason: str | None = None,
    size: int = 0,
) -> ScanEntry:
    """造一条扫描结果。`reason` 默认跟着 `Decision` —— 扫描器就是这么做的；
    传了 `reason` 就是模拟"规则命中了、但真正生效的理由是别的"那些情形。"""
    decision = rules.decide(path, is_dir=is_dir)
    return ScanEntry(
        path=path,
        name=path.rsplit("/", 1)[-1],
        is_dir=is_dir,
        size=size,
        mtime_ns=0,
        action=action,
        decision=decision,
        root="C:/t",
        reason=decision.reason if reason is None else reason,
    )


def test_bucket_attributes_a_rule_hit_to_that_rule() -> None:
    rules = tiny_rules()
    assert bucket(make_entry(rules, "C:/t/a.log", is_dir=False)) == (
        KIND_RULE,
        "skip 第 2 行",
        "*.log",
    )


def test_bucket_keeps_a_system_reason_out_of_the_rules() -> None:
    """规则**命中了**、但理由不是它 → 不能记到规则头上。

    reparse、`data_dir` 硬剔除、嵌套根、超限都属于这一类：它们的 `decision` 仍然是
    规则怎么说（因为规则确实命中），但那个数字与规则无关。归错栏的症状是报表里出现
    "某条规则排掉了 3 万条"，而用户会照着这条规则去改一个根本没起作用的地方。
    """
    rules = tiny_rules()
    entry = make_entry(
        rules, "C:/t/a.log", is_dir=False, reason="在数据目录内（硬剔除）"
    )
    assert entry.decision.rule is not None, "前提：规则确实命中了这条"
    kind, where, raw = bucket(entry)
    assert kind == KIND_OTHER
    assert where == WHERE_NONE
    assert "数据目录" in raw


def test_bucket_prices_the_marker_as_its_own_column() -> None:
    """规则与目录标记**同时**成立时，记在标记头上。

    `build/` 这条规则确实命中了 `C:/t/build`，但如果那个目录里有 `.lsearch-noindex`，
    真正生效的是标记（`ingest/base.py`：判定发生在下潜之前）。这也是「目录标记」
    这一栏存在的理由——它是用户亲手放的开关，混进规则里就问不出"我放的标记省了多少"。
    """
    rules = tiny_rules()
    entry = make_entry(rules, "C:/t/build", is_dir=True, reason=marker_reason())
    assert entry.decision.rule is not None, "前提：规则确实命中了这个目录"
    assert entry.decision.reason != entry.reason, "前提：理由被标记改写过"
    assert bucket(entry)[0] == KIND_MARKER


def test_is_marker_reason_is_an_exact_match_not_a_substring() -> None:
    """判据是**逐字相等**：一旦用 `in`，任何引用了这句话的消息都会被算成标记。"""
    assert is_marker_reason(marker_reason())
    assert not is_marker_reason(marker_reason() + "（另有原因）")
    assert not is_marker_reason("原因见 " + marker_reason())
    assert not is_marker_reason("")


def test_command_line_rules_are_labelled_not_numbered() -> None:
    """`--include` / `--exclude` 的规则没有行号，归属栏写「命令行」。"""
    rules = tiny_rules().with_extra(excludes=["*.tmp"])
    rule = rules.rules[-1]
    assert rule.line == 0, "前提：命令行规则的 line 是 0（哨兵值）"
    assert rule_key(rule) == (KIND_RULE, WHERE_COMMAND_LINE, "*.tmp")


def test_hits_count_directories_separately_and_ignore_their_size() -> None:
    """字节**只算文件**：目录的 `st_size` 在 NTFS 上是 0 或目录项大小。"""
    rules = tiny_rules()
    hits = Hits()
    hits.add(make_entry(rules, "C:/t/build", is_dir=True, size=4096))
    hits.add(make_entry(rules, "C:/t/a.log", is_dir=False, size=100))
    assert (hits.dirs, hits.files, hits.total) == (1, 1, 2)
    assert hits.bytes == 100, "目录的 st_size 被算进字节数了"
    assert hits.example == "C:/t/build", "例应当是第一条，且要能被指认"


def test_hits_ignore_the_unknown_size_sentinel() -> None:
    """读不到大小的文件 `size = -1`：加进合计会把字节数做小，且小得看不出来。"""
    hits = Hits()
    hits.add(make_entry(tiny_rules(), "C:/t/gone.bin", is_dir=False, size=-1))
    assert hits.files == 1
    assert hits.bytes == 0


def test_rows_break_ties_by_key_not_by_insertion_order() -> None:
    """同数并列时按 key 排序，不靠字典顺序。

    `F2-04` 的微基准因为抽样不确定，两次跑出过不可比的两组数（PROGRESS §4 P2
    「偏差」第 9 条）。同样的坑在这里的表现是"同一次报表两遍跑出来行序不同"，
    看的人会以为数据变了。先按总数降序，再按 key 升序。
    """
    dist = Distribution()
    dist.skipped[(KIND_RULE, "skip 第 9 行", "b")] = Hits(files=2)
    dist.skipped[(KIND_RULE, "skip 第 2 行", "a")] = Hits(files=2)
    dist.skipped[(KIND_OTHER, WHERE_NONE, "z")] = Hits(files=5)
    assert [key[1] for key, _ in dist.rows()] == [WHERE_NONE, "skip 第 2 行", "skip 第 9 行"]


def test_other_reasons_are_folded_by_their_first_characters() -> None:
    """「规则之外」的理由里带路径，原样分组会得到几百个各含 1 条的分组。

    这条是 `F2-01` 踩出来的：不截断的话每个读不到的文件各成一组，等于没分组，
    而报表上看起来"信息很全"。
    """
    rules = tiny_rules()
    tail = "C:/very/long/path/xyz/"
    long_a = f"读不到属性：{tail}aaaaaaaa"
    long_b = f"读不到属性：{tail}bbbbbbbb"
    assert long_a[:OTHER_PREFIX] == long_b[:OTHER_PREFIX], "前提：两条前 28 字相同"
    assert long_a != long_b

    dist = Distribution()
    dist.record_skip(make_entry(rules, "C:/t/x", is_dir=False, reason=long_a))
    dist.record_skip(make_entry(rules, "C:/t/y", is_dir=False, reason=long_b))
    rows = dist.rows()
    assert len(rows) == 1, "同一种错误的不同路径被分成了两组"
    key, hits = rows[0]
    assert hits.files == 2
    assert key[2] == long_a[:OTHER_PREFIX]


def test_a_name_only_hit_counts_as_hitting_the_rule() -> None:
    """只记名字也是"命中"：`*.ts` 的规则不该出现在"一条都没管到"的榜上。"""
    rules = tiny_rules()
    dist = Distribution(rules.rules)
    dist.record_entry(
        make_entry(rules, "C:/t/s.ts", is_dir=False, action=Action.NAME_ONLY)
    )
    assert [rule.raw for rule in dist.rules_with_no_hits()] == ["*.log", "build/"]


def test_record_entry_ignores_normally_indexed_files() -> None:
    """正常索引的文件**不进**这张表。

    记进来的话这表会做长一千倍，而它要回答的问题只有一个：哪些规则把文件挡在了
    正文之外（`.ts`、`*.jpg` 这类）。
    """
    rules = tiny_rules()
    dist = Distribution(rules.rules)
    dist.record_entry(make_entry(rules, "C:/t/keep.txt", is_dir=False, action=Action.INDEX))
    assert dist.rows(TABLE_NAME_ONLY) == []
    assert len(dist.rules_with_no_hits()) == 3


def test_rules_with_no_hits_starts_from_the_whole_file() -> None:
    """榜子的分母是**整份规则文件**，不是命中的那些。

    "这份文件里有多少条规则其实从未生效"只有拿着全集才回答得了——**没命中的那些
    恰恰是答案**（`F2-03` 遗留第 1 条的推广）。
    """
    rules = tiny_rules()
    dist = Distribution(rules.rules)
    dist.record_skip(make_entry(rules, "C:/t/a.log", is_dir=False))
    assert [rule.raw for rule in dist.rules_with_no_hits()] == ["build/", "*.ts"]


def test_kind_totals_sums_each_column_separately() -> None:
    """三栏各自的量级是这张报表最该先被看到的东西。"""
    rules = tiny_rules()
    dist = Distribution()
    dist.record_skip(make_entry(rules, "C:/t/a.log", is_dir=False, size=10))
    dist.record_skip(make_entry(rules, "C:/t/b.log", is_dir=False, size=20))
    dist.record_skip(make_entry(rules, "C:/t/build", is_dir=True))
    dist.record_skip(
        make_entry(rules, "C:/t/marked", is_dir=True, reason=marker_reason())
    )
    totals = dist.kind_totals(TABLE_SKIPPED)
    assert (totals[KIND_RULE].dirs, totals[KIND_RULE].files) == (1, 2)
    assert totals[KIND_RULE].bytes == 30
    assert (totals[KIND_MARKER].dirs, totals[KIND_MARKER].files) == (1, 0)
    assert totals[KIND_OTHER].total == 0
    assert dist.totals(TABLE_SKIPPED).total == 4
