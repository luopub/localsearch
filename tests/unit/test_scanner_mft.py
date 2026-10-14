"""`F2-02`（MFT 扫描后端）的自动化测试。被测模块：`ingest/scanner_mft.py`。

------------------------------------------------------------------------------
为什么不用管理员权限也能把这个后端测干净
------------------------------------------------------------------------------

`MftScanner` 与真卷之间只隔一层：`source`（`VolumeSource`）——"给我这个卷的记录流"
的可调用对象，默认实现 `volume_records` 是"开卷设备 → `FSCTL_ENUM_USN_DATA` → 关卷"。
测试把这一层换成**从真目录树翻出来的记录流**（`FakeVolume`），于是：

- 真机才有的前提（提权、NTFS、卷设备）都不进测试——它们在 `F2-02` 的真机核对
  `tools/verify_mft_parity.py` 里验；
- 而这一层真正会错的东西全都还在：路径还原、深度序剪枝、账目口径、断链判断。

`FakeVolume` 如实造出真 MFT 与目录树的**三点差别**，这三点恰好都是最容易写错的地方：

1. 记录里**没有大小与时间**（大小由 `ntfs.attributes_of` 现问——它不提权，所以这一环
   在测试里也是真的）；
2. 一个文件**一条记录**：硬链接的第二个名字根本不在记录流里（`F0-04` 实测 C: 上差
   758,931 条，就是这一条造成的）；
3. 顺序**按记录号**，与目录树的形状无关——所以子记录可能排在父记录前面。假流的顺序
   刻意打乱，因为"顺序无关"正是这一层要保证的事。

**根**用的是真目录（`tmp_path` 下的真文件），因为根要靠 `st_ino`（就是 FRN）在记录流
里对上号——这一点必须是真的，不然测出来的路径全是造的，等于没测。
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from localsearch.ingest import ntfs, scanner_mft, scanner_scandir
from localsearch.ingest.base import Action, ScanConfig, ScanEntry, ScanError
from localsearch.ingest.filters import NOINDEX_MARKER, IgnoreRules
from localsearch.ingest.scanner_scandir import (
    DATA_DIR_REASON,
    NESTED_ROOT_REASON,
    over_limit_reason,
)

# ==============================================================================
# 工具
# ==============================================================================

_MASK = 0xFFFFFFFFFFFF
_SEQ_SHIFT = 48
_DIRECTORY = ntfs.FILE_ATTRIBUTE_DIRECTORY
_REPARSE = ntfs.FILE_ATTRIBUTE_REPARSE_POINT


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


def frn_of(path: Path) -> int:
    """真文件引用号。`st_ino` 在 Windows 上**就是** FRN（`F0-04` 实测）。"""
    return int(os.stat(path, follow_symlinks=False).st_ino)


def synthetic(record_number: int, parent_number: int, name: str, *,
              seq: int = 1, parent_seq: int = 1, attrs: int = 0) -> ntfs.Record:
    """造一条**没有对应真文件**的记录。只有"上溯判断"这一类测试才用它。"""
    return ntfs.Record(
        frn=(seq << _SEQ_SHIFT) | record_number,
        parent=(parent_seq << _SEQ_SHIFT) | parent_number,
        name=name,
        attrs=attrs,
    )


class FakeVolume:
    """真目录树 → 一份 MFT 记录流（+ 可以塞进几条人造记录）。

    `order` 决定流的顺序：真 MFT 按记录号返回，与目录形状无关，所以默认**打乱**
    （固定种子，失败可复现）。`reverse` 是更极端的一半（子记录几乎全在父前面）。
    """

    def __init__(self, roots, *, extra=(), order: str = "shuffle", seed: int = 20261015):
        self.records: list[ntfs.Record] = []
        self.seen: set[int] = set()
        for root in roots:
            self._walk(Path(root))
        self.records.extend(extra)
        if order == "shuffle":
            random.Random(seed).shuffle(self.records)
        elif order == "reverse":
            self.records.reverse()
        #: 真机上卷设备名是 `\\.\C:`，这里只记下来给断言看
        self.devices: list[str] = []

    def _walk(self, root: Path) -> None:
        """按目录树造记录。**不下潜 reparse point**——junction 指向的内容在 MFT 里
        挂在它自己的真实路径下，不在这个目录里（`scanner_mft` 的根处理那一节）。

        ⚠ 这里对每条条目打的是 **`os.stat(entry.path)`**，不是
        `entry.stat(follow_symlinks=False)`：后者在 Windows 上返回的
        **`st_ino` 是 0**（`F0-04` 实测，`DirEntry` 的 stat 缓存里没有文件号），
        而 FRN 正是这份记录流的全部。用错的话每条记录的记录号都是 0，整棵树会
        塌成一个槽位——**第一次写这份替身就是这么错的**，表现是"扫描结果只剩目录"。
        """
        todo = [root]
        while todo:
            directory = todo.pop()
            # 目录自己那条记录的父是它的**上一级**；而下面扫出来的每一条的父是
            # **这个目录自己**。两个 `stat` 写反过：表现是"所有子项都挂到了根上"，
            # 而它们在那条路径上并不存在，于是每一条都变成一个读属性的错误。
            directory_stat = os.stat(directory, follow_symlinks=False)
            self._add(directory, directory_stat,
                      os.stat(directory.parent, follow_symlinks=False))
            try:
                with os.scandir(directory) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    stat = os.stat(entry.path, follow_symlinks=False)
                except OSError:
                    continue
                self._add(Path(entry.path), stat, directory_stat)
                if (stat.st_file_attributes & _DIRECTORY) and not (
                    stat.st_file_attributes & _REPARSE
                ):
                    todo.append(Path(entry.path))

    def _add(self, path: Path, stat, parent_stat) -> None:
        frn = int(stat.st_ino)
        if frn in self.seen:
            # 一条记录只出现一次——两个根嵌套时会走到同一棵树两次（真机上是**整卷
            # 枚举一次**，不管有几个根），重复的记录会让同一个槽位被登记两遍。
            return
        self.seen.add(frn)
        self.records.append(ntfs.Record(frn=frn, parent=int(parent_stat.st_ino),
                                        name=path.name,
                                        attrs=int(stat.st_file_attributes)))

    def source(self):
        """`VolumeSource`：喂记录流，并如实更新计数（记录条数就是流里的条数）。"""

        @contextmanager
        def open_(device: str, counters: ntfs.RecordCounters):
            self.devices.append(device)
            counters.records = len(self.records)
            counters.calls = 1
            yield iter(self.records)

        return open_


def scan_mft(roots, volume: FakeVolume, *, ignore: str = "", **cfg_kwargs):
    """MFT 后端跑一轮：返回 (留下的, 跳过的, stats)。"""
    roots = tuple(str(r) for r in roots)
    rules = IgnoreRules.parse(ignore, roots=roots)
    cfg = ScanConfig(roots=roots, data_dir=cfg_kwargs.pop("data_dir", ""), **cfg_kwargs)
    kept: list[ScanEntry] = []
    skipped: list[ScanEntry] = []
    stats = scanner_mft.MftScanner(cfg, source=volume.source()).scan(
        rules=rules, on_entry=kept.append, on_skip=skipped.append
    )
    return kept, skipped, stats


def scan_scandir(roots, *, ignore: str = "", **cfg_kwargs):
    """scandir 后端跑一轮，参数与 `scan_mft` 逐字对应（差分就靠这一点成立）。"""
    roots = tuple(str(r) for r in roots)
    rules = IgnoreRules.parse(ignore, roots=roots)
    cfg = ScanConfig(roots=roots, data_dir=cfg_kwargs.pop("data_dir", ""), **cfg_kwargs)
    kept: list[ScanEntry] = []
    skipped: list[ScanEntry] = []
    stats = scanner_scandir.ScandirScanner(cfg).scan(
        rules=rules, on_entry=kept.append, on_skip=skipped.append
    )
    return kept, skipped, stats


def fingerprint(entry: ScanEntry) -> tuple:
    """拿两个后端的流做对比时**只比这些字段**。

    不比 `frn`：`scandir` 后端一律留 0，这是刻意的（要拿它得逐条 `os.stat`，36 µs，
    C: 上 125 s），见 `base.ScanEntry.frn`。
    出错的那几条也不进对比：两边的错误文本来自不同的系统调用（`os.stat` 对
    `GetFileAttributesExW`），它俩同形但不必逐字相同。
    """
    return (entry.path, entry.name, entry.is_dir, entry.size, entry.mtime_ns,
            entry.action.value, entry.decision.action.value, entry.reason,
            entry.root, entry.reparse_tag)


def stream(entries: list[ScanEntry]) -> list[tuple]:
    """给两个后端做对比用的流。**排过序**：跨后端的约定是"内容一致"，不是"顺序一致"
    ——同一个深度里 MFT 按记录号走、`scandir` 按目录里的枚举顺序走，两者本来就不保证
    相同。顺序在**单个后端内部**才是有约定的事（由浅到深，见那条专门的测试）。
    """
    return sorted(fingerprint(e) for e in entries if e.error is None)


def paths(entries: list[ScanEntry]) -> set[str]:
    return {e.path for e in entries}


def names(entries: list[ScanEntry]) -> set[str]:
    return {e.name for e in entries}


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


def volume_of(root: Path, tmp_path: Path) -> FakeVolume:
    return FakeVolume([root])


# ==============================================================================
# A. 记录流 → 路径
# ==============================================================================


def test_records_become_paths_under_the_root(tmp_path: Path):
    """最基本的一件事：`ParentFRN` 一层层接回去，得到与目录树一致的路径。"""
    tree(tmp_path, {
        "a.txt": "A",
        "sub/b.txt": "B",
        "sub/deep/c.txt": "C",
    })
    volume = FakeVolume([tmp_path], order="reverse")     # 子记录排在父前面
    kept, skipped, stats = scan_mft([tmp_path], volume)

    assert paths(kept) == {
        f"{tmp_path.as_posix()}/a.txt",
        f"{tmp_path.as_posix()}/sub",
        f"{tmp_path.as_posix()}/sub/b.txt",
        f"{tmp_path.as_posix()}/sub/deep",
        f"{tmp_path.as_posix()}/sub/deep/c.txt",
    }
    assert skipped == []
    assert (stats.files_seen, stats.dirs_seen) == (3, 2)
    assert stats.roots_scanned == 1
    # 卷设备名就是盘符的 `\\.\X:` 形态
    assert volume.devices == [ntfs.device_of("C:")]


def test_the_root_itself_is_not_an_entry(tmp_path: Path):
    """根是**用户声明的范围**，不是盘上被发现的条目——它不产生 `ScanEntry`
    （`ScanStats.dirs_entered` 比 `dirs_seen` 多 1 就是这个意思）。MFT 那边同样不产
    生，虽然它的记录流里**有**根这条记录。
    """
    tree(tmp_path, {"a.txt": "A"})
    kept, skipped, _ = scan_mft([tmp_path], FakeVolume([tmp_path]))
    assert tmp_path.as_posix() not in paths(kept) | paths(skipped)
    assert names(kept) == {"a.txt"}


def test_the_record_order_does_not_change_the_result(tmp_path: Path):
    """MFT 按记录号返回，与目录形状无关。**同一条记录的不同顺序必须给同一个结果**：
    依赖顺序的实现只在某些树上出错，而真机上的顺序由盘上历史决定，不是选出来的。
    """
    tree(tmp_path, {
        "a.txt": "A", "sub/b.txt": "B", "sub/deep/c.txt": "C",
        "other/d.txt": "D", "other/e/f.txt": "E",
    })
    shuffles = {order: scan_mft([tmp_path], FakeVolume([tmp_path], order=order))
                for order in ("shuffle", "reverse", "as-is")}
    reference = stream(shuffles["shuffle"][0]), stream(shuffles["shuffle"][1])
    for order, (kept, skipped, stats) in shuffles.items():
        assert (stream(kept), stream(skipped)) == reference, order
        assert stats.dirs_seen == shuffles["shuffle"][2].dirs_seen, order


def test_entries_come_out_shallow_first(tmp_path: Path):
    """由浅到深的发出顺序**是依赖的前提**：剪枝要能带走整棵子树，就得先判父级。
    下游（`F2-03` 的写库）也按这个顺序拿数据，不能只靠"看起来对"。
    """
    tree(tmp_path, {"a.txt": "A", "s/b.txt": "B", "s/d/c.txt": "C", "s/d/e/f.txt": "F"})
    order: list[ScanEntry] = []
    cfg = ScanConfig(roots=(str(tmp_path),))
    scanner_mft.MftScanner(cfg, source=FakeVolume([tmp_path]).source()).scan(
        rules=IgnoreRules.parse("", roots=(str(tmp_path),)),
        on_entry=order.append, on_skip=order.append,
    )
    depths = [e.path.count("/") for e in order]
    assert depths == sorted(depths)
    # 最浅的那一层就是根的直接子项（`根/名字` 比根多一层）
    assert min(depths) == tmp_path.as_posix().count("/") + 1
    assert max(depths) == min(depths) + 3        # s/d/e/f.txt 比根深四层


def test_a_duplicate_record_does_not_make_a_duplicate_entry(tmp_path: Path):
    """同一个槽位出现两次时**只发一条**。

    真机上一条记录只出现一次，所以这条护栏不是给真机写的，是给坏卷与**假记录流**
    写的——而它的失效形态是"同一条被数了两次"，看起来只是数字偏大。
    """
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path])
    volume.records.append(volume.records[0])          # 复制一条记录
    kept, _, _ = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}


def test_names_round_trip(tmp_path: Path):
    """中文、emoji、长名字。名字是 UTF-16 原文从记录里解出来的，与 `scandir` 的
    `DirEntry.name` 不是同一个来源——两侧都得给对。"""
    long_name = "l" * 100 + ".txt"
    tree(tmp_path, {"中文 名字.txt": "1", "emoji 🎉.txt": "2", long_name: "3"})
    kept, _, _ = scan_mft([tmp_path], FakeVolume([tmp_path]))
    assert names(kept) == {"中文 名字.txt", "emoji 🎉.txt", long_name}


def test_a_name_that_cannot_be_decoded_becomes_a_counted_error(tmp_path: Path):
    """记录里的名字解不出来时（不成对的代理项）**不炸整轮**：路径因而是错的，
    读属性会失败，于是它变成一个**计数在案的错误**。两种坏法里选看得见的那种。
    """
    target = tmp_path / "真文件.txt"
    target.write_text("x", encoding="utf-8")
    volume = FakeVolume([tmp_path])
    # 把这条记录的名字改成一个坏名字（路径随之不存在）
    volume.records = [
        ntfs.Record(frn=r.frn, parent=r.parent,
                    name="坏\ud800名字" if r.name == "真文件.txt" else r.name,
                    attrs=r.attrs)
        for r in volume.records
    ]
    kept, skipped, stats = scan_mft([tmp_path], volume)
    assert kept == []
    assert stats.errors_total == 1
    (path, why), = stats.errors
    assert "读不到属性" in why
    assert skipped[0].action is Action.SKIP and skipped[0].error is not None
    assert "坏" in path


# ==============================================================================
# B. 账目口径
# ==============================================================================


def test_only_what_scandir_would_see_goes_into_the_counts(tmp_path: Path):
    """MFT 看得见**整个卷**：不在根下的、NTFS 保留记录子树里的、被剪枝盖住的。
    这三类**都不进** `dirs_seen` / `files_seen`，而是分列进 `mft` 那三栏——
    混在一起会让"这轮到底扫了多少"变成无法回答的数。
    """
    tree(tmp_path, {"keep/a.txt": "A", "cut/b.txt": "B"})
    (tmp_path / "cut" / NOINDEX_MARKER).write_text("", encoding="utf-8")
    volume = FakeVolume(
        [tmp_path],
        extra=[
            # 不在任何扫描根下：父记录根本不在枚举里
            synthetic(90_001, 88_888, "别处.txt"),
            # NTFS 保留记录（记录 11 = $Extend）的子树：Win32 的枚举看不见
            synthetic(11, 5, "$Extend", attrs=_DIRECTORY),
            synthetic(90_002, 11, "$Quota", attrs=_DIRECTORY),
        ],
    )
    kept, skipped, stats = scan_mft([tmp_path], volume)

    assert paths(kept) == {f"{tmp_path.as_posix()}/keep",
                           f"{tmp_path.as_posix()}/keep/a.txt"}
    assert names(skipped) == {"cut"}
    assert stats.mft.outside_roots == 1          # 别处.txt
    assert stats.mft.system == 2                 # $Extend 与它下面的 $Quota
    # cut 底下有两条记录：b.txt 与标记文件本身（标记是一个**真文件**，也是一条记录）
    assert stats.mft.hidden == 2
    # 那一批不进可见条目的账：`cut` 自己**算**（它被看见了，只是被剪掉，
    # 所以它进 `dirs_seen` 也进 `dirs_pruned`），它底下的那些不算。
    assert (stats.files_seen, stats.dirs_seen) == (1, 2)
    assert stats.dirs_pruned == 1
    _, _, dir_stats = scan_scandir([tmp_path])
    assert (dir_stats.files_seen, dir_stats.dirs_seen) == (1, 2)
    assert stats.mft.records == len(volume.records)
    assert stats.mft.volumes == 1


def test_the_volume_level_counts_are_counted_once(tmp_path: Path):
    """`outside_roots` / `system` 是**按卷**的账。两个根落在同一个卷上时，两个根各跑
    一轮都会看到同一批"根外"的记录——只能数一遍，不然它会随根数翻倍。
    """
    tree(tmp_path, {"one/a.txt": "A", "two/b.txt": "B"})
    volume = FakeVolume([tmp_path], extra=[synthetic(90_001, 88_888, "别处.txt")])
    roots = [tmp_path / "one", tmp_path / "two"]
    kept, _, stats = scan_mft(roots, volume)
    # 同一个卷只用一个根跑一遍，作为对照：两个根时这一栏**必须与它相等**
    _, _, one_root = scan_mft([tmp_path / "one"], volume)

    assert stats.roots_scanned == 2
    assert one_root.mft.outside_roots == 4       # tmp_path、two、b.txt、别处.txt
    assert stats.mft.outside_roots == one_root.mft.outside_roots, "同一个卷上数了两遍"
    assert names(kept) == {"a.txt", "b.txt"}


def test_a_record_number_that_cannot_be_a_slot_is_dropped(tmp_path: Path):
    """记录号是坏记录时可能是天文数字，照着分配会直接 `MemoryError`。
    护栏是丢掉并计数，不试图解释它。
    """
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path], extra=[
        synthetic(scanner_mft._MAX_SLOTS + 5, 5, "不可能的记录"),
    ])
    kept, _, stats = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}
    assert stats.mft.outside_roots == 0 and stats.mft.system == 0

    index = scanner_mft._VolumeIndex()
    index.add(synthetic(scanner_mft._MAX_SLOTS + 5, 5, "不可能的记录"))
    assert index.overflow == 1
    assert len(index) == 0


def test_a_name_longer_than_the_pool_limit_is_truncated(tmp_path: Path):
    """名字池单条上限 `_MAX_NAME_BYTES`。NTFS 的名字最多 255 个码元（510 字节），
    所以这道护栏只对**坏记录**生效——而它必须生效，否则一条坏记录就能让池子涨到
    任意大。
    """
    index = scanner_mft._VolumeIndex()
    index.add(synthetic(16, 5, "x" * 4000))
    assert index.overflow == 0
    assert index.lens[16] == scanner_mft._MAX_NAME_BYTES
    assert len(index.name(16)) == scanner_mft._MAX_NAME_BYTES // 2


# ==============================================================================
# C. 与 scandir 逐条同形的判定
# ==============================================================================


def test_the_same_tree_gives_the_same_stream_from_both_backends(tmp_path: Path):
    """**差分**：同一棵树、同一份规则、两个后端，流要逐条一致（路径、大小、时间、
    处置、理由、reparse tag）。

    这一条是这一整个测试文件里最值钱的一条：两个后端各写一遍判定，"哪一步顺序不同"
    或"某个理由的措辞不同"这类差异只会在这里现形，而它们在真机上的表现是
    "同一个目录两次扫描结果不一样"。
    """
    tree(tmp_path, {
        "a.txt": "A", "b.log": "B", "sub/c.txt": "C", "sub/d.md": "D",
        "sub/deep/e.txt": "E", "空目录/.keepme": "",
    })
    ignore = "*.log\nsub/d.md\n"
    mft_kept, mft_skip, mft_stats = scan_mft([tmp_path], FakeVolume([tmp_path]),
                                             ignore=ignore)
    dir_kept, dir_skip, dir_stats = scan_scandir([tmp_path], ignore=ignore)

    assert stream(mft_kept) == stream(dir_kept)
    assert stream(mft_skip) == stream(dir_skip)
    # 可见条目的账也要一致（scandir 那边没有 `mft` 这一块）
    assert (mft_stats.files_seen, mft_stats.dirs_seen, mft_stats.files_index,
            mft_stats.files_skipped, mft_stats.dirs_pruned, mft_stats.files_name_only
            ) == (dir_stats.files_seen, dir_stats.dirs_seen, dir_stats.files_index,
                  dir_stats.files_skipped, dir_stats.dirs_pruned, dir_stats.files_name_only)
    assert dir_stats.mft is None, "scandir 后端不该有 mft 那一块账"


def test_a_noindex_marker_prunes_the_whole_subtree(tmp_path: Path):
    """目录级标记（`F2-05`）在 MFT 这边同样要**带走整棵子树**。

    与 scandir 的差别只是计数：MFT 看得见被剪枝盖住的那些记录，所以它会出现在
    `mft.hidden` 里——而 scandir 根本看不见它们（这正是为什么它没有对应的一栏）。
    """
    tree(tmp_path, {"keep/a.txt": "A", "cut/b.txt": "B", "cut/deep/c.txt": "C"})
    (tmp_path / "cut" / NOINDEX_MARKER).write_text("", encoding="utf-8")

    kept, skipped, stats = scan_mft([tmp_path], FakeVolume([tmp_path]))
    dir_kept, dir_skip, _ = scan_scandir([tmp_path])

    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    assert names(skipped) == {"cut"}
    # cut 底下四条记录：b.txt、deep、标记文件，以及 deep/c.txt
    assert stats.mft.hidden == 4
    assert "c.txt" not in names(kept) | names(skipped)


def test_a_rule_pruned_directory_hides_its_children(tmp_path: Path):
    """规则剪枝同理：被排掉的目录的下面的记录，连 `on_skip` 都不该出现——
    "父目录被排除、子项又被 `!` 放开"这种写法在两个后端里都必须救不回来。
    """
    tree(tmp_path, {"keep/a.txt": "A", "sub/b.txt": "B", "sub/deep/c.txt": "C"})
    kept, skipped, stats = scan_mft([tmp_path], FakeVolume([tmp_path]), ignore="sub/\n")
    dir_kept, dir_skip, _ = scan_scandir([tmp_path], ignore="sub/\n")

    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    assert names(skipped) == {"sub"}
    # sub 底下三条记录：b.txt、deep，以及 deep/c.txt
    assert stats.mft.hidden == 3


def test_a_nested_root_is_scanned_by_its_own_round(tmp_path: Path):
    """嵌套根：外层那一轮把它剪掉（理由与 scandir 同一句），内层那一轮把它当根扫。
    两轮都收到同一个目录的条目会把它数两遍。
    """
    tree(tmp_path, {"a.txt": "A", "sub/b.txt": "B", "sub/deep/c.txt": "C"})
    roots = [tmp_path, tmp_path / "sub"]
    kept, skipped, stats = scan_mft(roots, FakeVolume(roots))
    dir_kept, dir_skip, dir_stats = scan_scandir(roots)

    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    assert stats.roots_scanned == dir_stats.roots_scanned == 2
    assert paths(kept) == {f"{tmp_path.as_posix()}/a.txt",
                           f"{tmp_path.as_posix()}/sub/b.txt",
                           f"{tmp_path.as_posix()}/sub/deep",
                           f"{tmp_path.as_posix()}/sub/deep/c.txt"}
    pruned = [e for e in skipped if e.reason == NESTED_ROOT_REASON]
    assert [e.name for e in pruned] == ["sub"]


def test_data_dir_is_pruned_and_its_root_is_refused(tmp_path: Path):
    """`data_dir` 是**硬性剔除**（计划 §4.5.1 的第 2 重机制）：索引器正在往里写，
    规则匹配对它存在竞态。两个后端要给出同一句话。
    """
    tree(tmp_path, {"a.txt": "A", "data/b.txt": "B"})
    data_dir = str(tmp_path / "data")

    kept, skipped, stats = scan_mft(
        [tmp_path], FakeVolume([tmp_path]), data_dir=data_dir
    )
    dir_kept, dir_skip, _ = scan_scandir([tmp_path], data_dir=data_dir)
    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    assert [e.reason for e in skipped] == [DATA_DIR_REASON]
    assert stats.mft.hidden == 1

    # 根就落在 data_dir 里面：整个根不扫，且理由与 scandir 的**逐字相同**
    _, _, mft2 = scan_mft([tmp_path / "data"], FakeVolume([tmp_path / "data"]),
                          data_dir=data_dir)
    _, _, dir2 = scan_scandir([tmp_path / "data"], data_dir=data_dir)
    assert mft2.roots_unreachable == dir2.roots_unreachable == [
        (str(tmp_path / "data"), "在 data_dir 里面，按硬性剔除跳过")]
    assert mft2.roots_scanned == 0


def test_max_file_mb_rewrites_index_to_name_only(tmp_path: Path):
    """超过 `max_file_mb` 的文件：规则判 INDEX，实际只记名字，理由**逐字**相同
    （措辞只有一个产出点，`F2-06` 的报表按前 28 个字符分组）。
    """
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    tree(tmp_path, {"small.txt": "s"})

    kept, skipped, stats = scan_mft([tmp_path], FakeVolume([tmp_path]), max_file_mb=1)
    dir_kept, dir_skip, dir_stats = scan_scandir([tmp_path], max_file_mb=1)

    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    over = [e for e in kept if e.name == "big.bin"][0]
    assert over.action is Action.NAME_ONLY
    assert over.decision.action is Action.INDEX, "规则自己也判了 INDEX，账要如实分开"
    assert over.reason == over_limit_reason(1, 2 * 1024 * 1024)
    assert stats.files_over_limit == dir_stats.files_over_limit == 1
    assert stats.bytes_index == dir_stats.bytes_index == len("s")


def test_a_reparse_directory_is_not_descended(tmp_path: Path):
    """junction 目录：只记一条 SKIP，**不下潜**。MFT 这边多一层含义——它指向的内容
    在 MFT 里挂在别处，本来就不在这个目录下面（假流也不给它的子项）。
    """
    target = tmp_path / "真目录"
    target.mkdir()
    (target / "里面的文件.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "链接"
    if not make_junction(link, target):
        pytest.skip("这台机器建不了 junction")

    kept, skipped, stats = scan_mft([tmp_path], FakeVolume([tmp_path]))
    dir_kept, dir_skip, dir_stats = scan_scandir([tmp_path])

    assert stream(kept) == stream(dir_kept)
    assert stream(skipped) == stream(dir_skip)
    repro = [e for e in skipped if e.name == "链接"][0]
    assert repro.action is Action.SKIP and "reparse point" in repro.reason
    assert stats.reparse_tags == dir_stats.reparse_tags   # 按 tag 分的账也要一致
    # 链接底下**一条都没有**（它指向的内容不在这个目录里）
    assert not [e for e in kept + skipped if e.path.startswith(link.as_posix() + "/")]
    # 而同一批内容在**它自己的真实路径**下照样被扫到：不跟随 ≠ 把它藏起来
    assert names(kept) == {"真目录", "里面的文件.txt"}


def test_a_recycle_bin_directory_is_a_normal_directory(tmp_path: Path):
    """`$Recycle.Bin` **不是**保留记录：它是卷根下的一个普通目录，`scandir` 看得见它。
    按"名字以 `$` 开头"过滤会把用户真能看到的目录从索引里抹掉——这就是不能那样做的
    原因（真正要滤的是**记录号** 0–15）。
    """
    tree(tmp_path, {"$Recycle.Bin/S-1-5-21/desktop.ini": "x"})
    kept, _, stats = scan_mft([tmp_path], FakeVolume([tmp_path]))
    assert {"$Recycle.Bin", "S-1-5-21", "desktop.ini"} <= names(kept)
    assert stats.mft.system == 0


# ==============================================================================
# D. 断链与环
# ==============================================================================


def test_a_parent_sequence_mismatch_is_a_broken_link(tmp_path: Path):
    """父记录的**序号**对不上：那条记录被删过、槽位已经给了别人。只比记录号会把两个
    不同的目录认成一个（`ntfs.Record` 的注释），所以这里按断链处理并计数。
    """
    tree(tmp_path, {"a.txt": "A"})
    root_number = frn_of(tmp_path) & _MASK
    volume = FakeVolume([tmp_path], extra=[
        synthetic(90_001, root_number, "序号对不上的父", parent_seq=0x9999),
    ])
    kept, _, stats = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}
    assert stats.mft.broken_links == 1
    assert stats.mft.outside_roots == 1


def test_a_cycle_terminates_instead_of_hanging(tmp_path: Path):
    """A 的父是 B、B 的父是 A。健康卷上不会有（目录树无环），坏卷上会把上溯变成
    **死循环**——而"扫不完"比"数错"难查得多。所以它必须有护栏。

    这条测试如果失败，形态是**卡住**而不是断言失败：那正是它要证明的东西。
    """
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path], extra=[
        synthetic(90_001, 90_002, "环甲"),
        synthetic(90_002, 90_001, "环乙"),
    ])
    kept, _, stats = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}
    assert stats.mft.outside_roots == 2


def test_a_self_referencing_record_is_not_followed(tmp_path: Path):
    """父是自己：一步就到，直接判"不在根下"（另外真卷上记录 5 `$Root` 的父也是自己，
    它由保留记录那条规则兜住）。"""
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path], extra=[synthetic(90_001, 90_001, "自环")])
    kept, _, stats = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}
    assert stats.mft.outside_roots == 1


def test_a_parent_outside_the_enumeration_is_outside_the_roots(tmp_path: Path):
    """父记录号比整个索引还大（枚举里没有它）——不在根下，不是错。"""
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path], extra=[synthetic(90_001, 90_999, "孤儿")])
    kept, _, stats = scan_mft([tmp_path], volume)
    assert names(kept) == {"a.txt"}
    assert stats.mft.outside_roots == 1
    assert stats.mft.broken_links == 0


# ==============================================================================
# E. 根
# ==============================================================================


def test_a_root_the_volume_cannot_reach_is_reported_not_raised(tmp_path: Path):
    """不可达的根**只记不抛**，且理由与 scandir 逐字相同：盘没插、路径写错都是运行时
    事实，不是配置错误——但它们必须在报表里留下痕迹。
    """
    missing = tmp_path / "不存在"
    file_root = tmp_path / "不是目录.txt"
    file_root.write_text("x", encoding="utf-8")

    for root, why in ((missing, "不存在或未挂载"), (file_root, "不是一个目录")):
        volume = FakeVolume([tmp_path])
        _, _, stats = scan_mft([root], volume, )
        assert stats.roots_unreachable == [(str(root), why)]
        assert stats.roots_scanned == 0
        _, _, dir_stats = scan_scandir([root])
        assert dir_stats.roots_unreachable == stats.roots_unreachable


def test_a_root_without_a_volume_is_refused(tmp_path: Path):
    """UNC 根没有卷（`drive_of` 返回空串）——MFT 是按卷枚举的，没有卷就无从枚举。"""
    _, _, stats = scan_mft(["//server/share"], FakeVolume([tmp_path]))
    assert stats.roots_unreachable == [
        ("//server/share", "MFT 按卷枚举，这个根没有卷（UNC 或相对路径）")]
    assert stats.roots_scanned == 0
    assert stats.mft.records == 0, "一个根都没扫时不该去开卷"


def test_a_root_missing_from_the_mft_is_reported(tmp_path: Path):
    """根的文件号在记录流里对不上（两次读取之间被重建了？）——报出来，不静悄悄地
    扫出一个空结果。"""
    tree(tmp_path, {"a.txt": "A"})
    volume = FakeVolume([tmp_path])
    volume.records = [r for r in volume.records if r.frn != frn_of(tmp_path)]
    _, _, stats = scan_mft([tmp_path], volume)
    assert stats.roots_scanned == 0
    (raw, why), = stats.roots_unreachable
    assert raw == str(tmp_path) and "序号对不上" in why


def test_a_reparse_root_is_refused_with_the_difference_spelled_out(tmp_path: Path):
    """根是 junction 时，**两个后端会给出不同结果**，MFT 这边明确拒绝而不是装作
    扫过了：`scandir` 会跟着根走（根是用户指定的范围），而 MFT 只认记录里的父子
    关系——junction 指向的内容在 MFT 里挂在它自己的真实路径下。
    """
    target = tmp_path / "真目录"
    target.mkdir()
    (target / "a.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "链接"
    if not make_junction(link, target):
        pytest.skip("这台机器建不了 junction")

    _, _, mft_stats = scan_mft([link], FakeVolume([tmp_path]))
    assert mft_stats.roots_scanned == 0
    (_, why), = mft_stats.roots_unreachable
    assert "reparse point" in why and "MFT 看不见" in why

    dir_kept, _, dir_stats = scan_scandir([link])
    assert dir_stats.roots_scanned == 1, "scandir 是跟着根走的"
    assert names(dir_kept) == {"a.txt"}


# ==============================================================================
# F. 枚举失败与句柄
# ==============================================================================


def test_a_volume_that_fails_mid_enumeration_stops_that_volume(tmp_path: Path):
    """开工之后卷用不了（提权被收回、卷被卸载）：**不半路换后端**——一份混着两种
    后端口径的报表比一次明确的失败更难查。
    """
    tree(tmp_path, {"a.txt": "A"})

    @contextmanager
    def broken(device: str, counters):
        raise ntfs.VolumeError(device, "打开卷设备", ntfs.ERROR_ACCESS_DENIED)
        yield  # pragma: no cover

    cfg = ScanConfig(roots=(str(tmp_path),))
    stats = scanner_mft.MftScanner(cfg, source=broken).scan(
        rules=IgnoreRules.parse("", roots=(str(tmp_path),)),
        on_entry=lambda e: pytest.fail("卷都开不起来，不该发出条目"),
    )
    assert stats.roots_scanned == 0
    assert stats.mft.volumes == 0
    (raw, why), = stats.roots_unreachable
    assert raw == str(tmp_path) and "打开卷设备" in why


def test_the_volume_handle_is_always_closed(monkeypatch):
    """句柄是内核对象，扫一轮开一堆就会漏——而"忘了关"不会有任何报错，
    只表现为句柄泄漏。所以 `volume_records` 用 `finally` 关，这里用替身钉住它。
    """
    closed: list[int] = []
    monkeypatch.setattr(ntfs, "open_volume", lambda device: 4321)
    monkeypatch.setattr(ntfs, "close_volume", closed.append)

    def boom(handle, counters):
        yield ntfs.Record(1, 5, "x", 0)
        raise RuntimeError("读到一半炸了")

    monkeypatch.setattr(ntfs, "iter_records", boom)
    with pytest.raises(RuntimeError):
        with scanner_mft.volume_records("\\\\.\\C:", ntfs.RecordCounters()) as records:
            list(records)
    assert closed == [4321]


def test_rules_and_roots_have_to_match(tmp_path: Path):
    """规则集与扫描范围对不上时直接报错：含 `/rel` 的相对路径规则**不会报错、
    只会永远不命中**（`check_rules_roots`）。两个后端都要过这一关。"""
    tree(tmp_path, {"a.txt": "A"})
    cfg = ScanConfig(roots=(str(tmp_path),))
    rules = IgnoreRules.parse("", roots=("C:/别处",))
    with pytest.raises(ScanError):
        scanner_mft.MftScanner(cfg, source=FakeVolume([tmp_path]).source()).scan(
            rules=rules, on_entry=lambda e: None
        )
