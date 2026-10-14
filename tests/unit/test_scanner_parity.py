"""`F2-02`（MFT 后端与 scandir 后端的折叠核对）的自动化测试。被测模块：`ingest/parity.py`。

------------------------------------------------------------------------------
两层，各测各的
------------------------------------------------------------------------------

1. **`ParityCheck` 本身**用**人造条目**测：判据是"哪几类差异必须让 `ok` 变假"、
   "硬链接为什么不算差异"、"FRN 什么时候才去问"。这几件事与盘无关，用人造条目能
   逐条钉死——真盘上凑齐"一条无法解释的差异"恰恰是最难的。
2. **两个真后端在同一棵真树上跑一遍**（差分）：这一条是 `F2-02` 的判据本身。
   硬链接用真 `os.link` 造——**它的存在正是判据从"计数相等"改成"按 FRN 折叠后集合
   相等"的原因**（`F0-04`：C: 上两侧计数差 758,931），所以它必须在测试里真的出现，
   否则整个模块测的是一件盘上不存在的事。

⚠ 这一层**不做 I/O**，FRN 由调用方注入（`frn_of`）——所以单测不需要提权。
真卷上的那一轮在 `tools/verify_mft_parity.py`（要管理员终端）。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from localsearch.ingest import ntfs, scanner_mft, scanner_scandir
from localsearch.ingest.base import Action, ScanConfig, ScanEntry
from localsearch.ingest.filters import IgnoreRules
from localsearch.ingest.parity import MAX_SAMPLES, ParityCheck

# ==============================================================================
# 人造条目
# ==============================================================================

_RULES = IgnoreRules.parse("", roots=())


def entry(path: str, *, action: Action = Action.INDEX, is_dir: bool = False,
          frn: int = 0, size: int = 1) -> ScanEntry:
    """一条最小可用的 `ScanEntry`。`decision` 走真规则对象（`0` 条规则 → 无规则命中），
    因为 `ParityCheck` 比的是 `action`，而 `action` 与 `decision.action` 在真流里是
    分开的（`max_file_mb` 那条会把 `INDEX` 改写成 `NAME_ONLY`）。"""
    return ScanEntry(
        path=path, name=path.rsplit("/", 1)[-1], is_dir=is_dir, size=size,
        mtime_ns=0, action=action, decision=_RULES.decide(path, is_dir=is_dir),
        root="", reason="无规则命中", frn=frn,
    )


def check(mft: list[ScanEntry], scandir: list[ScanEntry], frn_of=None) -> tuple:
    """按真实用法喂一遍：**先 MFT 那一侧整轮，再 scandir 那一侧**。"""
    pc = ParityCheck()
    for e in mft:
        pc.add_mft(e)
    for e in scandir:
        pc.add_scandir(e, frn_of)
    return pc.result(), pc


# ==============================================================================
# A. 判定
# ==============================================================================


def test_identical_streams_check_out():
    same = [entry("C:/work/a.txt"), entry("C:/work/sub", is_dir=True),
            entry("C:/work/sub/b.txt")]
    parity, _ = check(list(same), list(same))
    assert (parity.mft_entries, parity.scandir_entries, parity.matched) == (3, 3, 3)
    assert parity.ok is True
    assert "3" in parity.summary()
    # 干净的一轮里不该有任何样本
    assert not (parity.mft_only or parity.unexplained
                or parity.action_mismatch or parity.is_dir_mismatch)


def test_the_key_is_case_insensitive():
    """折叠与匹配走的是**规范形键**，不是显示用的 `path`：NTFS 大小写不敏感，
    用带大小写的路径当键，`C:/Windows` 与 `c:/windows` 会算成两条。"""
    parity, _ = check([entry("C:/Work/A.TXT")], [entry("c:/work/a.txt")])
    assert parity.matched == 1 and parity.ok is True


def test_a_path_only_the_mft_side_sees_fails_the_check():
    """**`mft_only` 必须为空**：MFT 报出一条 scandir 看不见的路径，要么是它把不该发的
    东西发出去了（保留记录、被剪枝的子树），要么是 scandir 少走了——两种都要查。
    """
    parity, _ = check([entry("C:/work/a.txt"), entry("C:/work/幽灵.txt")],
                      [entry("C:/work/a.txt")])
    assert parity.mft_only_total == 1
    assert parity.mft_only == ["C:/work/幽灵.txt"]
    assert parity.ok is False


def test_a_second_name_folding_onto_a_known_frn_is_not_a_difference():
    """硬链接：MFT 一条记录（一个名字）、scandir 两条目录项。多出来的那个名字**不是
    差异**——它的 FRN 落在 MFT 的 FRN 集合里，说明是同一个文件。
    """
    parity, _ = check(
        [entry("C:/win/kernel32.dll", frn=42)],
        [entry("C:/win/kernel32.dll"), entry("C:/win/KERNEL32_OLD.dll")],
        frn_of=lambda path: 42,
    )
    assert (parity.scandir_only_total, parity.hard_link_total) == (1, 1)
    assert parity.unexplained_total == 0
    assert parity.ok is True, "硬链接不该让核对失败"


def test_a_second_name_whose_frn_is_unknown_is_a_difference():
    """FRN 落在 MFT 集合**之外**：那不是一个额外名字，是一处真差异。"""
    parity, _ = check(
        [entry("C:/work/a.txt", frn=42)],
        [entry("C:/work/a.txt"), entry("C:/work/多出来的.txt")],
        frn_of=lambda path: 99,
    )
    assert parity.unexplained_total == 1
    assert parity.unexplained == ["C:/work/多出来的.txt"]
    assert parity.ok is False


def test_a_second_name_without_any_frn_is_a_difference():
    """`frn` 为 0 是"不知道"，不是"没有"（`base.ScanEntry.frn`）。不知道就没法折叠，
    只能记成无法解释——**不能默认放过**，那会把真差异静默吞掉。
    """
    parity, _ = check([entry("C:/work/a.txt", frn=42)],
                      [entry("C:/work/a.txt"), entry("C:/work/多出来的.txt")])
    assert parity.unexplained_total == 1
    assert parity.ok is False


def test_the_entry_frn_is_used_when_no_lookup_is_given():
    """调用方已经知道 FRN（MFT 那条流是白拿的）时不必再注入 `frn_of`。"""
    parity, _ = check([entry("C:/work/a.txt", frn=42)],
                      [entry("C:/work/a.txt"), entry("C:/work/别名.txt", frn=42)])
    assert (parity.hard_link_total, parity.frn_calls) == (1, 0)
    assert parity.ok is True


def test_frn_is_only_asked_for_entries_that_do_not_match():
    """**代价与差异成正比，不与条目数成正比**——这是整套设计的省钱之处：C: 上如果
    两侧每条都问一次 FRN，那是 3.47M 次 `os.stat`（125 s）；按这个写法只问 758,931 次
    （27 s）。所以这里直接数调用次数：100 条对得上的 + 1 条多出来的 = **1 次**。
    """
    calls: list[str] = []

    def frn_of(path: str) -> int:
        calls.append(path)
        return 42

    matched = [entry(f"C:/work/f{n}.txt", frn=n) for n in range(100)]
    parity, _ = check(matched, list(matched) + [entry("C:/work/别名.txt")],
                      frn_of=frn_of)
    assert calls == ["C:/work/别名.txt"], "对得上的那些不该去问 FRN"
    assert parity.frn_calls == 1
    assert parity.hard_link_total == 1


def test_action_and_is_dir_mismatches_are_reported_with_samples():
    """同一个路径在两边的**处置**不一致（规则分叉、剪枝分叉、`max_file_mb` 分叉）
    与"文件还是目录判断不一致"都要单独成栏——它们是不同的病因，合成一个数只会让人
    回头再跑一遍。
    """
    parity, _ = check(
        [entry("C:/work/a.txt", action=Action.INDEX),
         entry("C:/work/b.txt", action=Action.INDEX),
         entry("C:/work/是个目录", is_dir=True)],
        [entry("C:/work/a.txt", action=Action.NAME_ONLY),
         entry("C:/work/b.txt", action=Action.NAME_ONLY),
         entry("C:/work/是个目录", is_dir=False)],
    )
    assert parity.matched == 3
    assert parity.action_mismatch_total == 2
    assert parity.action_mismatch[0] == ("C:/work/a.txt", "index", "name-only")
    assert parity.is_dir_mismatch_total == 1
    assert parity.is_dir_mismatch[0] == ("C:/work/是个目录", True, False)
    assert parity.ok is False


def test_a_duplicate_path_on_the_mft_side_is_a_failure():
    """同一个规范路径在 MFT 那一侧出现两次 = **折叠没做干净**。它看起来只是"多了一条"，
    所以借 `mft_only` 那一栏把它说出来，而不是塞进"文件/目录判断不一致"。
    """
    parity, _ = check([entry("C:/work/a.txt"), entry("C:/work/a.txt")],
                      [entry("C:/work/a.txt")])
    assert parity.mft_entries == 2 and parity.matched == 1
    assert parity.mft_only_total == 1
    assert parity.ok is False


def test_samples_are_capped_but_totals_are_not():
    """样本上限 `MAX_SAMPLES`，**计数不设上限**。几百万条差异里没人会看第二条样本，
    但"N 是多少"必须准——报告的口径就是这个 N。
    """
    extra = MAX_SAMPLES + 7
    mft = [entry(f"C:/work/只有 mft 有{i}.txt") for i in range(extra)]
    parity, _ = check(mft, [])
    assert parity.mft_only_total == extra
    assert len(parity.mft_only) == MAX_SAMPLES


# ==============================================================================
# B. 两个真后端在同一棵真树上
# ==============================================================================


def records_of(root: Path) -> list[ntfs.Record]:
    """一棵真目录树 → 一份 MFT 记录流（**按 FRN 去重**：一个文件一条记录，这正是
    硬链接的第二个名字不在流里的原因）。

    这里刻意不复用 `test_scanner_mft.py` 里那份（那份要控制顺序、要塞人造记录）；
    这一条只需要"如实"。

    ⚠ 每条条目的 FRN 靠 `os.stat` 现问，**不能用 `DirEntry.stat()`**——后者在
    Windows 上返回的 `st_ino` 是 0（`F0-04` 实测），而 FRN 正是这份流的一切。
    """
    records: list[ntfs.Record] = []
    seen: set[int] = set()
    todo = [root]
    while todo:
        directory = todo.pop()
        directory_stat = os.stat(directory, follow_symlinks=False)
        frn = int(directory_stat.st_ino)
        if frn not in seen:
            seen.add(frn)
            records.append(ntfs.Record(
                frn=frn,
                parent=int(os.stat(directory.parent, follow_symlinks=False).st_ino),
                name=directory.name, attrs=int(directory_stat.st_file_attributes)))
        try:
            with os.scandir(directory) as it:
                children = [e.path for e in it]
        except OSError:
            continue
        for child_path in children:
            child = Path(child_path)
            try:
                child_stat = os.stat(child, follow_symlinks=False)
            except OSError:
                continue
            attrs = int(child_stat.st_file_attributes)
            child_frn = int(child_stat.st_ino)
            if child_frn not in seen:
                seen.add(child_frn)
                records.append(ntfs.Record(frn=child_frn, parent=frn,
                                           name=child.name, attrs=attrs))
            if (attrs & ntfs.FILE_ATTRIBUTE_DIRECTORY) and not (
                attrs & ntfs.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                todo.append(child)      # reparse 的目录**不下潜**，与真机同义
    return records


def real_frn(path: str) -> int:
    """`os.stat` 的 `st_ino` —— 在 Windows 上**就是** FRN（`F0-04` 实测）。"""
    return int(os.stat(path, follow_symlinks=False).st_ino)


def test_two_real_backends_agree_on_a_real_tree(tmp_path: Path):
    """**`F2-02` 的判据**：同一棵树、两个真后端、按 FRN 折叠硬链接后集合相等。

    树里必须有一条**真硬链接**（`os.link`）：它在 MFT 里是一条记录、在 scandir 里是
    两条目录项——判据从"计数相等"改成"按 FRN 折叠后集合相等"就是为了它。少了它，
    这条测试对"折叠到底有没有生效"一句话都没说。
    """
    root = tmp_path / "树"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("BB", encoding="utf-8")
    (root / "空目录").mkdir()
    os.link(root / "a.txt", root / "另一个名字.txt")

    roots = (str(root),)
    rules = IgnoreRules.parse("", roots=roots)
    config = ScanConfig(roots=roots)

    records = records_of(root)

    @contextmanager
    def source(device: str, counters: ntfs.RecordCounters):
        counters.records = len(records)
        yield iter(records)

    pc = ParityCheck()
    mft_stats = scanner_mft.MftScanner(config, source=source).scan(
        rules=rules, on_entry=pc.add_mft)
    dir_stats = scanner_scandir.ScandirScanner(config).scan(
        rules=rules, on_entry=lambda e: pc.add_scandir(e, real_frn))
    parity = pc.result()

    assert parity.mft_entries == mft_stats.entries_seen, "MFT 那侧只有目录里的条目"
    assert parity.scandir_entries == dir_stats.entries_seen
    assert parity.mft_only_total == 0, parity.mft_only
    assert parity.unexplained_total == 0, parity.unexplained
    assert parity.action_mismatch_total == 0
    assert parity.is_dir_mismatch_total == 0
    assert parity.hard_link_total == 1, "那条硬链接的第二个名字"
    assert parity.ok is True
    # 两侧条目数**本来就不相等**（差的就是那条硬链接），判据不是"计数相等"
    assert parity.scandir_entries == parity.mft_entries + 1
    # FRN 只问了那一条多出来的名字
    assert parity.frn_calls == 1


def test_the_check_fails_when_scandir_misses_a_file(tmp_path: Path):
    """**负面对照**：故意让 scandir 那一侧少喂一条，`mft_only` 必须亮。

    没有这一条，上面那条"通过"可能是判据本身太松（比如一条都没比上）。用有真文件的
    树而不是人造条目：这一条要顺着与上面**同一条**喂入路径走，区别只在少了一位。
    """
    root = tmp_path / "树"
    root.mkdir()
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "b.txt").write_text("B", encoding="utf-8")

    roots = (str(root),)
    rules = IgnoreRules.parse("", roots=roots)
    seen: list[ScanEntry] = []
    scanner_scandir.ScandirScanner(ScanConfig(roots=roots)).scan(
        rules=rules, on_entry=seen.append)

    pc = ParityCheck()
    for e in seen:                       # "MFT 那一侧" = 完整的一轮
        pc.add_mft(e)
    for e in seen[:-1]:                  # scandir 那一侧少一条（文件名的顺序不保证）
        pc.add_scandir(e, real_frn)
    parity = pc.result()
    assert parity.mft_only_total == 1
    assert parity.is_dir_mismatch_total == 0 and parity.action_mismatch_total == 0
    assert parity.ok is False
