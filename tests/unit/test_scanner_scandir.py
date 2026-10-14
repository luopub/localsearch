"""`F2-01`（`os.scandir` 扫描后端）的自动化测试。被测模块：`ingest/base.py`、`ingest/scanner_scandir.py`。

------------------------------------------------------------------------------
这一组测试为什么这样切
------------------------------------------------------------------------------

扫描后端最容易出的错**不是崩溃，而是数错**——`R4` 实测过：下潜 junction 会让时间
涨 2.23×、文件数虚高 12,101，而进程中没有任何异常。所以这里的断言绝大部分是
**"流里出现了什么、没出现什么"**，而不是"没报错"：

- **剪枝**要断言**子树里的文件根本不在流里**（连 `on_skip` 都不在）。"父目录被排除、
  子项又被 `!` 放开"这种写法在 walker 里是救不回来的，测试把这条语义写死。
- **reparse point** 用**真的 junction**（`mklink /J` 不需要管理员，本机实测可建）。
  只有一种情况造不出来：符号链接**文件**要权限（本机建不了），那一条用替身
  （monkeypatch）测，并在测试里写明白它测的是哪一半。
- **错误隔离**（`A1`）用替身造 `PermissionError` / `stat` 失败：真造"权限被拒的目录"
  要动 ACL，改坏了比测出来的多。

`tmp_path` 落在 `C:` 下的 pytest 临时目录里——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from localsearch.core.config import ChunkCfg, Config, PathsCfg, ScanCfg
from localsearch.ingest import base, scanner_scandir
from localsearch.ingest.base import (
    MAX_SAMPLES,
    Action,
    ScanConfig,
    ScanEntry,
    ScanError,
    ScanStats,
    check_rules_roots,
    drive_of,
    inside,
    make_scanner,
    reparse_tag_of,
)
from localsearch.ingest.filters import IgnoreRules, normalize
from localsearch.ingest.scanner_scandir import DATA_DIR_REASON, NESTED_ROOT_REASON

# ==============================================================================
# 工具
# ==============================================================================


def tree(root: Path, spec: dict[str, str | None]) -> Path:
    """按 `{相对路径: 内容}` 造一棵树；值是 `None` 表示造目录。"""
    for rel, content in spec.items():
        target = root / rel
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return root


def scan(root: Path, *, ignore: str = "", **cfg_kwargs):
    """跑一轮，把流分成 (留下的, 跳过的) 两个列表 + stats。"""
    roots = (str(root),)
    rules = IgnoreRules.parse(ignore, roots=roots)
    cfg = ScanConfig(roots=roots, data_dir=cfg_kwargs.pop("data_dir", ""), **cfg_kwargs)
    kept: list[ScanEntry] = []
    skipped: list[ScanEntry] = []
    stats = scanner_scandir.ScandirScanner(cfg).scan(
        rules=rules, on_entry=kept.append, on_skip=skipped.append
    )
    return kept, skipped, stats


def names(entries: list[ScanEntry]) -> set[str]:
    return {e.name for e in entries}


def entry_of(entries: list[ScanEntry], name: str) -> ScanEntry:
    hits = [e for e in entries if e.name == name]
    assert len(hits) == 1, f"期望恰好一条 {name!r}，实际 {len(hits)} 条"
    return hits[0]


def make_junction(link: Path, target: Path) -> bool:
    """建一个 junction（不需要管理员）。建不成就返回 False，让调用方 skip。"""
    if sys.platform != "win32":
        return False
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return done.returncode == 0 and link.exists()


class FakeEntry:
    """`os.DirEntry` 的替身：只提供扫描器用得到的那几个成员。

    用来造**造不出来的**两种情形——符号链接文件要权限（本机建不了）、目录读到一半
    被删（没法稳定复现）。它只替身 `os.scandir` 的产出，扫描逻辑本身一行不改。
    """

    def __init__(self, path: Path, *, is_dir: bool = False, size: int = 1,
                 tag: int = 0, stat_error: OSError | None = None) -> None:
        self.path = str(path)
        self.name = path.name
        self._is_dir = is_dir
        self._size = size
        self._tag = tag
        self._stat_error = stat_error

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def stat(self, *, follow_symlinks: bool = True):
        if self._stat_error is not None:
            raise self._stat_error
        attrs = base.FILE_ATTRIBUTE_REPARSE_POINT if self._tag else 0
        return _FakeStat(self._size, attrs, self._tag)


class _FakeStat:
    """够用的 stat 替身：只有 `st_size` / `st_mtime_ns` / `st_file_attributes` /
    `st_reparse_tag` 被读过。用得着真 `os.stat_result` 的地方（`reparse_tag_of`）
    另有专门的测试。"""

    def __init__(self, size: int, attrs: int, tag: int) -> None:
        self.st_size = size
        self.st_mtime_ns = 0
        self.st_file_attributes = attrs
        self.st_reparse_tag = tag


def fake_scandir(entries: list[FakeEntry]):
    """替掉 `os.scandir`：只对扫描根返回给定条目，其余目录返回空。"""

    class Ctx:
        def __init__(self, path) -> None:
            self._path = str(path)

        def __enter__(self):
            return iter(entries)

        def __exit__(self, *exc):
            return False

    return lambda path: Ctx(path)


# ==============================================================================
# A. 扫描器的输入与抉择
# ==============================================================================


def test_scan_config_is_taken_from_the_real_config():
    """接线只有一处，但接错了整轮扫的范围就错了——所以对着真 `Config` 核一遍。"""
    config = Config(
        paths=PathsCfg(data_dir="D:/data", roots=("D:/work", "C:/docs"), all_drives=False),
        scan=ScanCfg(max_file_mb=8, backend="scandir", follow_symlinks=False),
    )
    cfg = ScanConfig.from_config(config)
    assert cfg.roots == ("D:/work", "C:/docs")
    assert cfg.data_dir == "D:/data"
    assert cfg.max_file_mb == 8
    assert cfg.backend == "scandir"


def test_all_drives_is_refused_with_the_e10_reason(tmp_path: Path):
    """`E10` 的落地：判不出哪些卷"会一直在"，就不猜着枚举。"""
    with pytest.raises(ScanError) as err:
        make_scanner(ScanConfig(roots=(str(tmp_path),), all_drives=True))
    assert "E10" in str(err.value)
    assert "roots" in str(err.value)


def test_mft_backend_says_it_is_not_landed_yet(tmp_path: Path):
    """`F2-02` 之前，`backend="mft"` 必须**报错**而不是悄悄退回 scandir。"""
    with pytest.raises(ScanError) as err:
        make_scanner(ScanConfig(roots=(str(tmp_path),), backend="mft"))
    assert "F2-02" in str(err.value)


@pytest.mark.parametrize("backend", ["auto", "scandir"])
def test_auto_and_scandir_both_give_the_scandir_backend(tmp_path: Path, backend: str):
    scanner = make_scanner(ScanConfig(roots=(str(tmp_path),), backend=backend))
    assert scanner.name == "scandir"


def test_unknown_backend_is_refused(tmp_path: Path):
    with pytest.raises(ScanError):
        make_scanner(ScanConfig(roots=(str(tmp_path),), backend="everything"))


def test_empty_roots_are_refused():
    """没有要扫的目录——报错，不猜。"""
    with pytest.raises(ScanError) as err:
        scanner_scandir.ScandirScanner(ScanConfig(roots=()))
    assert "roots" in str(err.value)


def test_follow_symlinks_is_refused_with_the_r4_reason(tmp_path: Path):
    with pytest.raises(ScanError) as err:
        scanner_scandir.ScandirScanner(
            ScanConfig(roots=(str(tmp_path),), follow_symlinks=True)
        )
    assert "R4" in str(err.value)


def test_max_file_bytes_is_a_literal_product():
    """上限是**字面**的：0 就是 0 字节，不是"无限"。"""
    assert ScanConfig(max_file_mb=64).max_file_bytes == 64 * 1024 * 1024
    assert ScanConfig(max_file_mb=0).max_file_bytes == 0


# ==============================================================================
# B. 路径判断
# ==============================================================================


@pytest.mark.parametrize(
    "norm, prefix, expected",
    [
        ("c:/data", "c:/data", True),
        ("c:/data/x", "c:/data", True),
        ("c:/database", "c:/data", False),  # 边界收口：这就是它存在的理由
        ("c:/data2", "c:/data", False),
        ("c:/", "c:", True),
        ("d:/data", "c:/data", False),
        ("", "c:/data", False),
        ("c:/data", "", False),  # 空前缀不是"匹配一切"
    ],
)
def test_inside_closes_on_path_boundaries(norm: str, prefix: str, expected: bool):
    assert inside(norm, prefix) is expected


@pytest.mark.parametrize(
    "norm, expected",
    [
        ("c:", "c:"),
        ("c:/work", "c:"),
        ("d:/", "d:"),
        ("//server/share/x", ""),  # UNC 不是卷
        ("ab:/x", ""),  # 不是盘符
        ("", ""),
    ],
)
def test_drive_of(norm: str, expected: str):
    assert drive_of(norm) == expected


# ==============================================================================
# C. 规则 roots 的核对
# ==============================================================================


def test_rules_roots_must_match_the_scan_roots(tmp_path: Path):
    """不一致时**报错**，因为相对路径规则会静默不命中。

    这是 `PROGRESS.md` §4 P2「遗留」第 2 条从另一头堵上：规则的相对路径靠 roots
    才能解释，两个 roots 对不上就是接线错了，而错的表现是"规则写了但没生效"。
    """
    rules = IgnoreRules.parse("/doc/build/", roots=("D:/work",))
    with pytest.raises(ScanError) as err:
        check_rules_roots(rules, ("C:/other",))
    assert "roots" in str(err.value)


@pytest.mark.parametrize(
    "rule_roots, scan_roots",
    [
        (("D:/work",), ("D:/work",)),
        (("D:/work", "C:/docs"), ("c:/DOCS", "d:/WORK")),  # 顺序与大小写都不该管
        ((), ()),
    ],
)
def test_matching_roots_pass(rule_roots, scan_roots):
    check_rules_roots(IgnoreRules.parse("", roots=rule_roots), scan_roots)


def test_scan_refuses_when_rules_roots_disagree(tmp_path: Path):
    """同一件事在 `scan()` 的入口也要挡一次——调用方可能绕开 helper 直接扫。"""
    tree(tmp_path, {"a.txt": "x"})
    scanner = scanner_scandir.ScandirScanner(ScanConfig(roots=(str(tmp_path),)))
    with pytest.raises(ScanError):
        scanner.scan(
            rules=IgnoreRules.parse("*.txt", roots=("D:/elsewhere",)),
            on_entry=lambda e: None,
        )


# ==============================================================================
# D. reparse tag 的读法
# ==============================================================================


def test_reparse_tag_of_a_real_junction(tmp_path: Path):
    """真 junction 的 tag 是 `0xA0000003`（挂载点）。本机实测可建、不需要管理员。"""
    target = tree(tmp_path / "t", {"f.txt": "x"})
    link = tmp_path / "link"
    if not make_junction(link, target):
        pytest.skip("本机建不出 junction（mklink /J 失败）")
    stat = os.stat(link, follow_symlinks=False)
    assert reparse_tag_of(stat) == 0xA0000003


def test_ordinary_file_has_no_reparse_tag(tmp_path: Path):
    tree(tmp_path, {"f.txt": "x"})
    assert reparse_tag_of(os.stat(tmp_path / "f.txt", follow_symlinks=False)) == 0


def test_missing_reparse_attributes_read_as_zero():
    """非 Windows 的 `stat_result` 没有 `st_file_attributes`——不能因此炸。"""

    class Bare:
        st_size = 1

    assert reparse_tag_of(Bare()) == 0


# ==============================================================================
# E. 遍历：送出什么、剩下什么
# ==============================================================================


def test_every_file_and_directory_comes_out(tmp_path: Path):
    """根目录本身**不算一条**（它是用户声明的范围，不是盘上被发现的东西）。"""
    tree(tmp_path, {"a.txt": "x", "sub": None, "sub/b.md": "y"})
    kept, skipped, stats = scan(tmp_path)
    assert names(kept) == {"a.txt", "sub", "b.md"}
    assert skipped == []
    assert (stats.files_seen, stats.dirs_seen) == (2, 1)
    assert (stats.files_index, stats.dirs_kept) == (2, 1)
    assert stats.errors_total == 0


def test_entry_key_is_the_normalized_form(tmp_path: Path):
    """`path` 给人看（保留大小写），`key` 用来判定"是不是同一个"（全小写）。"""
    tree(tmp_path, {"Mixed.TXT": "x"})
    kept, _, _ = scan(tmp_path)
    entry = entry_of(kept, "Mixed.TXT")
    assert entry.path.endswith("Mixed.TXT")
    assert entry.key.endswith("mixed.txt")
    assert entry.key == normalize(entry.path)


def test_dirs_entered_counts_the_roots_too(tmp_path: Path):
    """`dirs_entered` 比 `dirs_kept` 多**根的个数**：根不产生条目，但确实被打开过。

    这个 +1 是真机上先看出来的（D:/work：47,754 见 / 46,167 进 / 1,588 剪）。写进
    测试是为了防止以后有人"顺手修正"它。
    """
    tree(tmp_path, {"a/x.txt": "1", "b/y.txt": "2"})
    roots = (str(tmp_path), str(tmp_path / "a"))
    rules = IgnoreRules.parse("", roots=roots)
    stats = scanner_scandir.ScandirScanner(ScanConfig(roots=roots)).scan(
        rules=rules, on_entry=lambda e: None
    )
    # 见到的目录条目只有 2 个：外层那一轮看到 a（嵌套根，剪掉）与 b；a 自己那一轮里
    # a 是根、不算条目
    assert stats.dirs_seen == 2
    assert stats.dirs_pruned == 1
    assert stats.dirs_entered == stats.dirs_kept + len(roots) == 3


def test_stats_entries_kept_matches_the_stream(tmp_path: Path):
    """账目与流必须对得上——对不上说明有一边在撒谎。"""
    tree(tmp_path, {"a.txt": "x", "b.log": "y", "sub/c.txt": "z", "sub/d.bin": "w"})
    kept, _, stats = scan(tmp_path, ignore="*.log\n")
    assert stats.entries_kept == len(kept)


def test_size_and_mtime_are_carried(tmp_path: Path):
    tree(tmp_path, {"a.txt": "hello"})
    kept, _, _ = scan(tmp_path)
    assert entry_of(kept, "a.txt").size == 5
    assert entry_of(kept, "a.txt").mtime_ns > 0


def test_directories_have_zero_size(tmp_path: Path):
    tree(tmp_path, {"sub": None, "sub/a.txt": "x"})
    kept, _, _ = scan(tmp_path)
    assert entry_of(kept, "sub").size == 0


def test_skip_rule_prunes_the_whole_subtree(tmp_path: Path):
    """被剪掉的子树**永远不会被问到**——连 `on_skip` 里都不该有它的子项。

    这条语义是 `decide()`/`explain()` 分成两个入口的原因（见 `filters.py` 语义第 7 条），
    也是"排掉 1 个目录"与"排掉 1 个文件"代价完全不同的地方。
    """
    tree(tmp_path, {"node_modules/deep/junk.js": "j", "keep/a.js": "k"})
    kept, skipped, stats = scan(tmp_path, ignore="node_modules/\n")
    assert names(kept) == {"keep", "a.js"}
    assert names(skipped) == {"node_modules"}
    assert "junk.js" not in names(skipped)  # 子项根本没被走到
    assert "deep" not in names(skipped)
    assert stats.dirs_pruned == 1


def test_skipped_files_go_to_on_skip_not_on_entry(tmp_path: Path):
    """排除掉的文件**不能**出现在索引流里。合成一个回调再让调用方过滤，
    就会得到"忘了过滤，把排除掉的文件也索引了"这种安静而昂贵的错。"""
    tree(tmp_path, {"a.txt": "x", "b.log": "y"})
    kept, skipped, _ = scan(tmp_path, ignore="*.log\n")
    assert names(kept) == {"a.txt"}
    assert names(skipped) == {"b.log"}


def test_skip_reason_names_the_rule_and_line(tmp_path: Path):
    tree(tmp_path, {"b.log": "y"})
    _, skipped, _ = scan(tmp_path, ignore="# 说明\n\n*.log\n")
    assert "第 3 行" in entry_of(skipped, "b.log").reason


def test_name_only_section_keeps_the_file(tmp_path: Path):
    tree(tmp_path, {"big.iso": "x"})
    kept, skipped, stats = scan(tmp_path, ignore="#@section name-only\n*.iso\n")
    assert entry_of(kept, "big.iso").action is Action.NAME_ONLY
    assert skipped == []
    assert stats.files_name_only == 1


def test_name_only_directory_is_inherited_by_its_subtree(tmp_path: Path):
    """`NAME_ONLY` 的目录要把处置传给整棵子树，且**子项的 reason 要说清是继承的**。"""
    tree(tmp_path, {"media/a.bin": "x", "media/deep/b.bin": "y"})
    kept, _, stats = scan(tmp_path, ignore="#@section name-only\nmedia/\n")
    assert entry_of(kept, "media").action is Action.NAME_ONLY
    assert entry_of(kept, "a.bin").action is Action.NAME_ONLY
    assert entry_of(kept, "b.bin").action is Action.NAME_ONLY
    assert "继承上级" in entry_of(kept, "a.bin").reason
    assert stats.files_name_only == 2


def test_negation_rescues_a_file_after_a_blanket_rule(tmp_path: Path):
    tree(tmp_path, {"a.log": "x", "keep.log": "y"})
    kept, _, _ = scan(tmp_path, ignore="*.log\n!keep.log\n")
    assert entry_of(kept, "keep.log").action is Action.INDEX
    assert "白名单例外" in entry_of(kept, "keep.log").reason


def test_no_rules_at_all_means_everything_is_indexed(tmp_path: Path):
    tree(tmp_path, {"a.txt": "x"})
    kept, skipped, _ = scan(tmp_path)
    assert entry_of(kept, "a.txt").decision.matched is False
    assert skipped == []


# ==============================================================================
# F. data_dir 硬性剔除（计划 §4.5.1 的第 2 重机制）
# ==============================================================================


def test_data_dir_is_pruned_even_with_no_rule_for_it(tmp_path: Path):
    """规则文件里**没有**关于 data_dir 的规则，它照样不能进流——因为索引器正在
    往里写，规则匹配对它存在竞态。"""
    tree(tmp_path, {"data/index.sqlite": "db", "docs/a.txt": "x"})
    kept, skipped, stats = scan(tmp_path, data_dir=str(tmp_path / "data"))
    assert "index.sqlite" not in names(kept)
    assert "index.sqlite" not in names(skipped)  # 目录都没进，别提子项
    assert entry_of(skipped, "data").reason == DATA_DIR_REASON
    assert stats.dirs_pruned == 1


def test_a_root_inside_data_dir_is_not_scanned(tmp_path: Path):
    tree(tmp_path, {"data/a.txt": "x"})
    roots = (str(tmp_path / "data"),)
    rules = IgnoreRules.parse("", roots=roots)
    cfg = ScanConfig(roots=roots, data_dir=str(tmp_path / "data"))
    stats = scanner_scandir.ScandirScanner(cfg).scan(rules=rules, on_entry=lambda e: None)
    assert stats.roots_scanned == 0
    assert stats.roots_unreachable == [(roots[0], "在 data_dir 里面，按硬性剔除跳过")]


def test_sibling_directory_with_a_shared_prefix_is_kept(tmp_path: Path):
    """`data_dir = …/data` 不能顺手剔掉 `…/database`（边界收口）。"""
    tree(tmp_path, {"data/x": "1", "database/y.txt": "2"})
    kept, _, _ = scan(tmp_path, data_dir=str(tmp_path / "data"))
    assert "database" in names(kept)


# ==============================================================================
# G. max_file_mb 上限（计划 §8 的 e 条）
# ==============================================================================


def test_file_over_the_limit_becomes_name_only(tmp_path: Path):
    tree(tmp_path, {"big.bin": "x" * 4096, "small.txt": "y"})
    kept, _, stats = scan(tmp_path, max_file_mb=0)  # 0 = 一个字节都不给正文
    assert entry_of(kept, "big.bin").action is Action.NAME_ONLY
    assert "max_file_mb" in entry_of(kept, "big.bin").reason
    assert entry_of(kept, "small.txt").action is Action.NAME_ONLY
    assert stats.files_over_limit == 2
    assert stats.bytes_index == 0  # 正文一个字节都不收


def test_empty_file_still_gets_indexed_at_limit_zero(tmp_path: Path):
    """`0 > 0` 为假：上限是**严格大于**才触发。空文件没有正文可提，但也不该被判成
    "太大"——否则 `max_file_mb = 0` 会把空文件也标成超限，看着像 bug。"""
    tree(tmp_path, {"empty.txt": ""})
    kept, _, stats = scan(tmp_path, max_file_mb=0)
    assert entry_of(kept, "empty.txt").action is Action.INDEX
    assert stats.files_over_limit == 0


def test_the_limit_is_strictly_greater_than(tmp_path: Path):
    tree(tmp_path, {"exact.bin": "x" * 1024, "over.bin": "y" * 1025})
    kept, _, _ = scan(tmp_path, max_file_mb=0)  # 上限 0 字节：两个都超
    assert entry_of(kept, "exact.bin").action is Action.NAME_ONLY
    assert entry_of(kept, "over.bin").action is Action.NAME_ONLY


def test_bytes_index_counts_only_full_text_files(tmp_path: Path):
    tree(tmp_path, {"a.txt": "12345", "b.iso": "x" * 100})
    _, _, stats = scan(tmp_path, ignore="#@section name-only\n*.iso\n")
    assert stats.bytes_index == 5
    assert stats.files_index == 1
    assert stats.files_name_only == 1


# ==============================================================================
# H. reparse point：真 junction + 替身的符号链接文件
# ==============================================================================


def test_a_real_junction_is_not_walked_through(tmp_path: Path):
    """`R4` 的核心断言：**不下潜**。

    不下潜才数得对——本机实测下潜会让文件数虚高 12,101、时间涨 2.23×（因为
    junction 指回自己所在的目录）。这里用一个指向自身所在目录的 junction 复现那个
    结构：若下潜就是无限循环，不循环本身就是证据。
    """
    tree(tmp_path, {"real/a.txt": "x"})
    link = tmp_path / "loop"
    if not make_junction(link, tmp_path):
        pytest.skip("本机建不出 junction（mklink /J 失败）")
    kept, skipped, stats = scan(tmp_path)
    assert names(kept) == {"real", "a.txt"}  # 只有真目录那一支
    entry = entry_of(skipped, "loop")
    assert entry.action is Action.SKIP
    assert "reparse point" in entry.reason
    assert stats.dirs_reparse == 1
    assert stats.reparse_tags == {0xA0000003: 1}
    assert stats.files_seen == 1  # a.txt 只数了一次


def test_a_reparse_file_is_kept_by_name_only(tmp_path: Path, monkeypatch):
    """符号链接**文件**给 `NAME_ONLY`：名字能搜到，正文不读（读它等于跟随链接，
    云占位文件还会触发下载）。

    ⚠ **这一条用的是替身**：建符号链接文件要权限，本机（未提权）建不出来。
    替身替换的是 `os.scandir` 的产出，扫描逻辑本身一行没改——所以它证明的是
    "扫描器拿到一个带 tag 的文件条目时会怎么处置"，不是"真符号链接长什么样"。
    """
    target = tmp_path / "real.txt"
    target.write_text("x", encoding="utf-8")
    fake = FakeEntry(tmp_path / "sym.txt", is_dir=False, size=1, tag=0xA000000C)
    monkeypatch.setattr(os, "scandir", fake_scandir([fake]))
    kept, skipped, stats = scan(tmp_path)
    assert [e.name for e in kept] == ["sym.txt"]
    assert kept[0].action is Action.NAME_ONLY
    assert "0xA000000C" in kept[0].reason
    assert skipped == []
    assert stats.files_name_only == 1
    assert stats.reparse_tags == {0xA000000C: 1}


def test_reparse_tag_histogram_separates_different_causes(tmp_path: Path, monkeypatch):
    """按 tag 分而不是只给一个总数：挂载点、云占位、WSL 的成因与对策完全不同。"""
    entries = [
        FakeEntry(tmp_path / "a.txt", size=1, tag=0xA0000003),
        FakeEntry(tmp_path / "b.txt", size=1, tag=0x9000001A),
        FakeEntry(tmp_path / "c.txt", size=1, tag=0x9000001A),
    ]
    monkeypatch.setattr(os, "scandir", fake_scandir(entries))
    _, _, stats = scan(tmp_path)
    assert stats.reparse_tags == {0xA0000003: 1, 0x9000001A: 2}


# ==============================================================================
# I. 错误隔离（A1：全量索引无未捕获异常）
# ==============================================================================


def test_unreadable_directory_does_not_break_the_round(tmp_path: Path, monkeypatch):
    """权限被拒的目录：计数 + 留样本 + 继续（C: 上实测有 354 个这样的目录）。"""
    tree(tmp_path, {"good/a.txt": "x", "locked/b.txt": "y"})
    real_scandir = os.scandir

    def guarded(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "拒绝访问")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded)
    kept, _, stats = scan(tmp_path)
    assert "a.txt" in names(kept)  # 别处的文件照常
    assert stats.errors_total == 1
    assert stats.errors[0][1].startswith("无法列出目录")
    assert "拒绝访问" in stats.errors[0][1]


def test_entry_without_readable_attributes_is_reported_not_crashing(tmp_path: Path, monkeypatch):
    """连是文件还是目录都判不出来的条目：送进 `on_skip`，账目上记一笔。"""
    boom = OSError(22, "句柄无效")
    fake = FakeEntry(tmp_path / "ghost.txt", stat_error=boom)
    monkeypatch.setattr(os, "scandir", fake_scandir([fake]))
    kept, skipped, stats = scan(tmp_path)
    assert kept == []
    assert [e.name for e in skipped] == ["ghost.txt"]
    assert skipped[0].error is not None
    assert skipped[0].size == -1 and skipped[0].mtime_ns == -1
    assert stats.errors_total == 1
    # 判不清的条目既不进 dirs_seen 也不进 files_seen——那两个数只收判得清的
    assert (stats.files_seen, stats.dirs_seen) == (0, 0)


def test_error_samples_are_bounded_but_the_count_is_not(tmp_path: Path, monkeypatch):
    """样本有上限、计数不设上限：全盘刷出上千条同类错误时，报表不能被淹掉，
    但"到底有多少条"必须准确。"""
    total = MAX_SAMPLES + 5
    entries = [
        FakeEntry(tmp_path / f"bad{i}.txt", stat_error=OSError(22, "句柄无效"))
        for i in range(total)
    ]
    monkeypatch.setattr(os, "scandir", fake_scandir(entries))
    _, _, stats = scan(tmp_path)
    assert stats.errors_total == total
    assert len(stats.errors) == MAX_SAMPLES


def test_a_broken_round_still_reports_the_time_it_spent(tmp_path: Path):
    """调用方中途抛异常时也要留下时间——否则"为什么这次只有一半"无从查起。"""
    tree(tmp_path, {"a.txt": "x"})

    def explode(entry: ScanEntry) -> None:
        raise RuntimeError("调用方的 bug")

    roots = (str(tmp_path),)
    scanner = scanner_scandir.ScandirScanner(ScanConfig(roots=roots))
    with pytest.raises(RuntimeError):
        scanner.scan(rules=IgnoreRules.parse("", roots=roots), on_entry=explode)


def test_on_skip_may_be_omitted(tmp_path: Path):
    """索引不需要"被排除了什么"，所以第二个回调是可选的。"""
    tree(tmp_path, {"a.txt": "x", "b.log": "y"})
    roots = (str(tmp_path),)
    scanner = scanner_scandir.ScandirScanner(ScanConfig(roots=roots))
    kept: list[ScanEntry] = []
    stats = scanner.scan(rules=IgnoreRules.parse("*.log", roots=roots), on_entry=kept.append)
    assert names(kept) == {"a.txt"}
    assert stats.files_skipped == 1


# ==============================================================================
# J. 扫描根
# ==============================================================================


def test_missing_root_is_reported_not_raised(tmp_path: Path):
    """盘没插、网络盘没挂载都是**运行时事实**，不是配置错误——记下来，继续扫别的根。"""
    tree(tmp_path / "real", {"a.txt": "x"})
    roots = (str(tmp_path / "not-here"), str(tmp_path / "real"))
    rules = IgnoreRules.parse("", roots=roots)
    kept: list[ScanEntry] = []
    stats = scanner_scandir.ScandirScanner(ScanConfig(roots=roots)).scan(
        rules=rules, on_entry=kept.append
    )
    assert stats.roots_scanned == 1
    assert stats.roots_unreachable == [(roots[0], "不存在或未挂载")]
    assert names(kept) == {"a.txt"}


def test_a_root_that_is_a_file_is_reported(tmp_path: Path):
    tree(tmp_path, {"a.txt": "x"})
    root = str(tmp_path / "a.txt")
    rules = IgnoreRules.parse("", roots=(root,))
    stats = scanner_scandir.ScandirScanner(ScanConfig(roots=(root,))).scan(
        rules=rules, on_entry=lambda e: None
    )
    assert stats.roots_unreachable == [(root, "不是一个目录")]


def test_nested_root_is_scanned_once_by_its_own_round(tmp_path: Path):
    """`D:/work` 与 `D:/work/test` 同时配着时：外层那一轮不重复扫内层，
    否则每个文件都会被数两遍（而两遍看起来"也说得通"，最难查）。"""
    tree(tmp_path, {"inner/a.txt": "x", "outer.txt": "y"})
    roots = (str(tmp_path), str(tmp_path / "inner"))
    rules = IgnoreRules.parse("", roots=roots)
    kept: list[ScanEntry] = []
    skipped: list[ScanEntry] = []
    stats = scanner_scandir.ScandirScanner(ScanConfig(roots=roots)).scan(
        rules=rules, on_entry=kept.append, on_skip=skipped.append
    )
    hits = [e for e in kept + skipped if e.name == "a.txt"]
    assert len(hits) == 1  # 只有内层那一轮扫它
    assert entry_of(skipped, "inner").reason == NESTED_ROOT_REASON
    assert entry_of(kept, "a.txt").root == normalize(roots[1])
    assert entry_of(kept, "outer.txt").root == normalize(roots[0])
    assert stats.files_seen == 2


def test_entry_root_is_the_walk_that_produced_it(tmp_path: Path):
    tree(tmp_path, {"sub/a.txt": "x"})
    kept, _, _ = scan(tmp_path)
    assert entry_of(kept, "a.txt").root == normalize(str(tmp_path))
