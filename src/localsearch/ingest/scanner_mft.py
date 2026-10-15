r"""可选扫描后端：直接枚举 NTFS 的 MFT（P2 / `F2-02`）。测试落点 `tests/unit/test_scanner_mft.py`。

底层访问（卷句柄、`FSCTL_ENUM_USN_DATA`、属性）在 `ingest/ntfs.py`，本模块只管
**扫描语义**：把记录流还原成路径、按与 `ScandirScanner` **同一套**判定产出 `ScanEntry`。

------------------------------------------------------------------------------
它凭什么存在（`D11`，理由已经改过一次）
------------------------------------------------------------------------------

计划里原本的理由是「全盘枚举 5 万文件/分钟 → 200 万/分钟」，`F0-04` 实测后改成
**5.68×（冷）/ 14.06×（暖）**，并把主理由改成 **USN 增量**（`F6-01`：只返回变化，
代价从"走遍全盘"降到"读一条日志"）。P2 落地的这一层是那个主理由的**地基**——
没有"按 FRN 认记录"的能力，增量就无从谈起。

⚠ **本模块不承诺 MFT 的冷扫比 scandir 快。** 原因是一个实测出来的硬账：USN 记录里
**没有文件大小**，要补就得逐条再问一次文件系统。这里原本引的 `F0-04` 对拍数
（`os.stat` 36 µs/条、`GetFileAttributesExW` 14 µs/条）**在真机核对那一轮没有复现**：
同一批路径上两者几乎相等（**5.3 µs 对 5.7 µs**，比值 0.93×，2026-10-15）。读数原样
放在 `docs/reports/F2-02-MFT与scandir核对.md` 第四节，**这里不重抄**——两份必然漂移。

补属性改用 `GetFileAttributesExW` 的决定**不变，理由换了**：不是因为它快（今天量不出
差别），而是因为它**不开句柄**（不触发杀软的开句柄回调）。整轮读数同样在那份报告里：
**205.89 s 对 65.52 s，慢 3.14×**——"全盘枚举更快、所以 MFT 当快路径"这个直觉
在那份报告里被推翻了；MFT 的赢面要等 `F6-01` 的 USN 增量才谈得上，`F2-02` 只是地基。

------------------------------------------------------------------------------
形态：先把整卷收进紧凑结构，再按深度序判
------------------------------------------------------------------------------

与 `scandir` 的后端有一个根本差别：**MFT 只能按卷枚举，不能按目录枚举**。

1. 枚举给出的是**整个卷**的记录，子记录完全可能排在父记录**前面**（MFT 按记录号
   顺序返回），而路径还原必须从 `ParentFRN` 逐级上溯。所以这一层**必须**先把记录
   收下来——`base.py` 那句"流式，不攒列表"在这里做不到，能改的只是**攒什么**：
   不攒 `Record` 对象（含 `str`，2.7M 个就是几百 MB），改成按记录号索引的**定长
   数组 + 一个名字池**（`_VolumeIndex`，约 18 字节/槽 + 名字原文）。
2. 收完以后按**深度**（到扫描根的距离）分桶、由浅到深判。深度序不是为了好看：
   目录级标记、`data_dir`、嵌套根的**剪枝要能带走整棵子树**，而"父级判过了"
   是这件事成立的前提。`scandir` 靠栈天然满足，这里靠深度序。

------------------------------------------------------------------------------
账目口径：只数「scandir 也会看到的那些」
------------------------------------------------------------------------------

这是本模块最容易被自己骗到的地方。MFT 看得见**整个卷**：

- 不在扫描根下的一大片（C: 上十几万个目录），`scandir` 根本不会走到；
- NTFS 保留记录（记录号 0–15：`$MFT`、`$Extend` 等）及其子树，Win32 的目录枚举
  **从来不返回**（这也是为什么不能只按"名字以 `$` 开头"过滤：`$Recycle.Bin` 是
  普通目录，`scandir` 看得见）；
- 落在被剪枝目录里的那些：它看得见，但从没被"发现"过。

所以 `dirs_seen` / `files_seen` 这些数**只统计可见的那一批**（靠深度序把父级的
"可见/已剪枝"状态往下传），其余分列进 `ScanStats.mft` 的 `outside_roots` /
`system` / `hidden`。混在一起会让"这轮到底扫了多少"变成一个无法回答的数——
而它正是 `--dry-run` 要给人看的。

⚠ **硬链接让两边的总数不可能相等**（`F0-04` §2.3：C: 上差 758,931）。MFT 一个文件
一条记录（一个名字），`scandir` 一个名字一条记录。所以核对判据是**按 FRN 折叠
硬链接后的集合相等**，不是数字相等；见 `ingest/parity.py`。

------------------------------------------------------------------------------
不装的那一半
------------------------------------------------------------------------------

**USN 增量（`F6-01`）不在本模块**。`USN_RECORD_V2` 里的 `Usn` 与 `TimeStamp` 这一层
刻意没解析（`ntfs.Record` 的注释里记着偏移），因为"拿日志算变化"要处理日志回绕、
id 变化、跨卷对齐（`R17`），那是 P6 的事，顺手做一半只会留下一份看起来能用、
实际上错的增量。
"""

from __future__ import annotations

import os
import time
from array import array
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from localsearch.core.paths import extended_path
from localsearch.ingest import ntfs
from localsearch.ingest.base import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    Action,
    EntryCallback,
    MftStats,
    ScanConfig,
    ScanEntry,
    ScanError,
    ScanStats,
    check_rules_roots,
    drive_of,
    has_noindex_marker,
    inside,
    marker_reason,
    normalize,
    reparse_tag_of,
)
from localsearch.ingest.filters import IgnoreRules
from localsearch.ingest.scanner_scandir import (
    DATA_DIR_REASON,
    NESTED_ROOT_REASON,
    over_limit_reason,
    reparse_dir_reason,
    reparse_file_reason,
)

#: FRN 的低 48 位是 MFT 记录号，高 16 位是序号。
_RECORD_MASK = 0xFFFFFFFFFFFF

#: 卷根目录（`$Root`）的记录号。它的 `ParentFRN` 指向自己——`F0-04` 的样本里
#: `System Volume Information` 的父是 5，而记录 5 的父还是 5。
VOLUME_ROOT_RECORD = 5

#: 0–15 是 NTFS 保留记录。这些**及其子树**在 Win32 的目录枚举里不存在。
FIRST_USER_RECORD = 16

#: 槽位数的护栏。C: 实测约 450 万槽；坏记录里的记录号可能是天文数字，照着分配
#: 会直接 `MemoryError`。超了就按坏记录计数并丢掉，不试图解释它。
_MAX_SLOTS = 1 << 25

#: 名字池里单个名字的最大字节数（UTF-16 上限 255 个码元 → 510 字节）。
_MAX_NAME_BYTES = 1024

#: 一次枚举往哪个卷要数据。可替换，测试用假记录流替掉它。
VolumeSource = Callable[[str, ntfs.RecordCounters], ContextManager[Iterator[ntfs.Record]]]


@contextmanager
def volume_records(device: str, counters: ntfs.RecordCounters):
    """真机上的一条：打开卷 → 吐记录 → 关句柄。

    **句柄一定要关**：它是内核对象，扫一轮开一堆会漏。用 `contextmanager` 而不是
    让调用方自己 `try/finally`，是因为"忘了关"在这里只表现为句柄泄漏，不会有任何
    报错——又是那种"看起来成功"的失败。
    """
    handle = ntfs.open_volume(device)
    try:
        yield ntfs.iter_records(handle, counters)
    finally:
        ntfs.close_volume(handle)


# ==============================================================================
# 整卷的记录，紧凑存放
# ==============================================================================


class _VolumeIndex:
    """按**记录号**索引的定长数组 + 一个名字池。

    为什么不是 `dict[int, Record]`：C: 上 273 万条，每个 `Record` 是一个含 `str`
    的 Python 对象（一个 20 字符的 `str` 就要 70 字节上下），光对象头就把内存推高
    到 GB 级。这里每条记录摊下来的成本是：

    | 项 | 字节/槽 |
    |---|---|
    | `frns`（`Q`，完整 FRN，0 = 空槽） | 8 |
    | `parents`（`Q`，父 FRN，0 = 无） | 8 |
    | `attrs`（`I`） | 4 |
    | `name_off`（`I`） / `name_len`（`H`） | 6 |
    | 名字池（UTF-16 原文，平均约 20 码元） | ~40 |
    | 合计 | **约 66** |

    273 万个槽 ≈ **180 MB**，扫完就随这一轮结束释放（不是常驻）。

    ⚠ **记录号必须稠密这个前提**：NTFS 分配记录号是**递增且近乎连续**的（删除只留
    空洞），所以"按记录号数组索引"是合理的；`_MAX_SLOTS` 是给坏记录留的护栏，
    不是给正常卷留的余量。
    """

    __slots__ = ("frns", "parents", "attrs", "offs", "lens", "pool", "slots", "overflow")

    def __init__(self) -> None:
        self.frns = array("Q")
        self.parents = array("Q")
        self.attrs = array("I")
        self.offs = array("I")
        self.lens = array("H")
        self.pool = bytearray()
        #: 有记录的槽，按记录号升序。用 `array` 存而不是 list：list 里每个 int
        #: 都是一个对象（273 万个 ≈ 100 MB），`array("I")` 是 4 字节/个。
        self.slots = array("I")
        #: 记录号超出 `_MAX_SLOTS` 的条数（坏记录）。
        self.overflow = 0

    def __len__(self) -> int:
        return len(self.frns)

    def add(self, rec: ntfs.Record) -> None:
        slot = rec.record_number
        if slot >= _MAX_SLOTS:
            self.overflow += 1
            return
        raw = rec.name.encode("utf-16-le", errors="surrogatepass")[:_MAX_NAME_BYTES]
        if len(self.frns) <= slot:
            grow = slot + 1 - len(self.frns)
            self.frns.extend(array("Q", [0]) * grow)
            self.parents.extend(array("Q", [0]) * grow)
            self.attrs.extend(array("I", [0]) * grow)
            self.offs.extend(array("I", [0]) * grow)
            self.lens.extend(array("H", [0]) * grow)
        # 空槽才登记。**同一个槽位第二次出现时不重复登记**——登记两次会让它进两次
        # 深度桶、被发出去两条，而表现只是"数字偏大"。真机上一条记录只出现一次，
        # 但坏卷与测试里的假记录流都可能给重复，而这条护栏只值一次比较。
        fresh = self.frns[slot] == 0
        self.frns[slot] = rec.frn
        self.parents[slot] = rec.parent
        self.attrs[slot] = rec.attrs
        self.offs[slot] = len(self.pool)
        self.lens[slot] = len(raw)
        self.pool += raw
        if fresh:
            self.slots.append(slot)

    def name(self, slot: int) -> str:
        off = self.offs[slot]
        return bytes(self.pool[off:off + self.lens[slot]]).decode("utf-16-le", errors="replace")

    def is_dir(self, slot: int) -> bool:
        return bool(self.attrs[slot] & ntfs.FILE_ATTRIBUTE_DIRECTORY)


# ==============================================================================
# 探针：能不能用 MFT
# ==============================================================================


@dataclass(frozen=True, slots=True)
class MftProbe:
    """`backend = "auto"` 的探测结果。`reason` **两种情况都有内容**：

    - 能用时说明"打算走哪些卷"（doctor 要报当前实际走的是哪条路，`R15` 的对策）；
    - 不能用时说明原因（`auto` 回退后 doctor 也要把它印出来，否则"为什么变慢了"
      就没有答案）。
    """

    ok: bool
    reason: str
    volumes: tuple[str, ...] = ()


def probe(config: ScanConfig) -> MftProbe:
    """按卷探测 MFT 可用性。**不做任何枚举**（每个卷只发一次 `DeviceIoControl`）。

    判据只有两条，都必须是**看得见的事实**：

    1. 文件系统名是 NTFS、驱动类型是本地固定盘（`E10` 实测这一条**判不出本地性**，
       所以它只是筛子——真正的判据是第 2 条）；
    2. **打开卷设备成功**——这一条同时证明"提权了"和"这个卷支持这个控制码"。
       `F0-04` 的负面对照实测：不提权时是明确的 `ERROR_ACCESS_DENIED(5)`。
    """
    if config.follow_symlinks:
        return MftProbe(False, "follow_symlinks = true 在 v1 里不支持（与 scandir 后端同一个理由）")
    if not config.roots:
        return MftProbe(False, "[paths] roots 是空的：没有要扫的目录")

    volumes = sorted({drive_of(normalize(r)) for r in config.roots})
    if "" in volumes:
        return MftProbe(False, "有扫描根没有卷（UNC 或相对路径），MFT 只能按卷枚举")

    for volume in volumes:
        facts = ntfs.volume_facts(volume)
        if not facts["ok"]:
            return MftProbe(False, f"{volume} 问不到文件系统信息")
        if facts["fs"].upper() != "NTFS":
            return MftProbe(False, f"{volume} 的文件系统是 {facts['fs'] or '未知'}，不是 NTFS")
        if int(facts["drive_type"]) != ntfs.DRIVE_FIXED:
            return MftProbe(False, f"{volume} 的驱动类型是 {facts['drive_type_name']}，不是本地固定盘")
        counters = ntfs.RecordCounters()
        try:
            with volume_records(ntfs.device_of(volume), counters) as records:
                next(iter(records), None)      # 只发一次 DeviceIoControl
        except ntfs.VolumeError as exc:
            return MftProbe(False, str(exc))

    return MftProbe(True, "可用：" + "、".join(volumes), tuple(volumes))


# ==============================================================================
# 扫描器
# ==============================================================================


@dataclass
class _RootTarget:
    """一个通过了可达性检查、准备按 MFT 扫的根。"""

    raw: str
    norm: str
    path: str          # 保留大小写的正斜杠形式，给 ScanEntry.path 当前缀
    slot: int
    frn: int


@dataclass
class _Pass:
    """一个根的一轮判定里共用的几张表。

    全部按**记录号**索引的定长数组：C: 上 450 万个槽 × 10 字节 ≈ 45 MB/轮，
    比"每个记录一个 dict 键"省一个数量级。一轮判完就释放。
    """

    root_of: array            # 0 未定 / 1 在根下 / 2 不在根下 / 3 保留记录子树
    depth: array
    dir_state: bytearray      # 0 未处理 / 1 可见 / 2 已剪枝
    inherited: bytearray      # 0 无 / 1 NAME_ONLY（父级传下来的）


class MftScanner:
    """`FSCTL_ENUM_USN_DATA` 式枚举。需要管理员权限，且只对 NTFS 有效（`R15`）。"""

    name = "mft"

    def __init__(self, config: ScanConfig, *, source: VolumeSource | None = None) -> None:
        if not config.roots:
            raise ScanError(
                "[paths] roots 是空的：没有要扫的目录。扫描范围必须显式给出——"
                "本机判不出哪些卷会一直在（PROGRESS §3 的 E10），所以不会替你猜一个。"
            )
        if config.follow_symlinks:
            raise ScanError(
                "follow_symlinks = true 在 v1 里不支持：本机实测 junction 指回自身所在的"
                "目录，跟随会让同一棵树被数很多遍（R4：2.23× 时间、文件数虚高 12,101）。"
            )
        self.config = config
        self._roots = tuple(normalize(r) for r in config.roots)
        self._root_set = frozenset(self._roots)
        self._data_dir = normalize(config.data_dir) if config.data_dir else ""
        self._source = source or volume_records

    # ------------------------------------------------------------------ 入口

    def scan(
        self,
        *,
        rules: IgnoreRules,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None = None,
    ) -> ScanStats:
        check_rules_roots(rules, self.config.roots)
        stats = ScanStats(roots_total=len(self.config.roots))
        stats.mft = MftStats()
        started = time.perf_counter()
        try:
            by_volume: dict[str, list[tuple[int, str, str]]] = {}
            for i, (raw, norm) in enumerate(zip(self.config.roots, self._roots)):
                volume = drive_of(norm)
                if not volume:
                    stats.roots_unreachable.append(
                        (raw, "MFT 按卷枚举，这个根没有卷（UNC 或相对路径）")
                    )
                    continue
                by_volume.setdefault(volume, []).append((i, raw, norm))
            for volume, items in by_volume.items():
                self._scan_volume(volume, items, rules, stats, on_entry, on_skip)
        finally:
            stats.seconds = time.perf_counter() - started
        return stats

    # ------------------------------------------------------------------ 一个卷

    def _scan_volume(
        self,
        volume: str,
        items: list[tuple[int, str, str]],
        rules: IgnoreRules,
        stats: ScanStats,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
    ) -> None:
        assert stats.mft is not None
        targets = [t for t in (self._prepare_root(raw, norm, stats) for _i, raw, norm in items)
                   if t is not None]
        if not targets:
            return

        device = ntfs.device_of(volume)
        counters = ntfs.RecordCounters()
        started = time.perf_counter()
        try:
            with self._source(device, counters) as records:
                index = _VolumeIndex()
                for rec in records:
                    index.add(rec)
        except ntfs.VolumeError as exc:
            # 走到这里说明"开工之后卷用不了"——`auto` 在造后端之前已经探过一遍，
            # 探针过了而现在不行，只可能是提权被收回或卷被卸载。**不半路换后端**：
            # 一份混着两种后端口径的报表比一次明确的失败更难查。
            for target in targets:
                stats.roots_unreachable.append((target.raw, str(exc)))
            return
        stats.mft.volumes += 1
        stats.mft.enumerate_seconds += time.perf_counter() - started
        stats.mft.records += counters.records
        stats.mft.bad_records += counters.bad_records
        stats.mft.other_versions += counters.other_versions

        for n, target in enumerate(targets):
            # `_run_root` 还会拒一次（根在记录流里对不上号）。**拒了就不算扫过**：
            # `roots_scanned` 与 `roots_unreachable` 是互补的两栏，都记会让人以为
            # "这个根扫了，但又不可达"。
            if self._run_root(target, index, rules, stats, on_entry, on_skip,
                              first=(n == 0)):
                stats.roots_scanned += 1

        del index

    def _prepare_root(self, raw: str, norm: str, stats: ScanStats) -> _RootTarget | None:
        """可达性检查。**与 `scandir` 后端逐条同义**——两边对"这个根能不能扫"给出
        不同答案，是两份后端最容易分叉的地方。"""
        if inside(norm, self._data_dir):
            stats.roots_unreachable.append((raw, "在 data_dir 里面，按硬性剔除跳过"))
            return None
        # 问真相的一律用扩展写法（`core.paths` 的规则）：名字是保留设备名的根
        # ——`\\?\` 建得出来，比如 `D:/x/nul`——原样 `Path.is_dir()` 会返回 False
        # （它走 `os.stat`，问到的是设备），于是这个根被一个**错误的理由**拒掉。
        # 报出去的一律是 `raw`。这一段与 `ScandirScanner._scan_root` 逐条同义
        # （那边多一条 `path`：`_walk` 要用它，而它必须是原样路径）。
        reachable = Path(extended_path(raw))
        if not reachable.exists():
            stats.roots_unreachable.append((raw, "不存在或未挂载"))
            return None
        if not reachable.is_dir():
            stats.roots_unreachable.append((raw, "不是一个目录"))
            return None
        if has_noindex_marker(reachable):
            stats.roots_unreachable.append((raw, marker_reason()))
            return None
        try:
            stat = os.stat(reachable, follow_symlinks=False)
        except OSError as exc:
            stats.roots_unreachable.append((raw, f"读不到属性：{exc}"))
            return None
        if reparse_tag_of(stat) != 0:
            # scandir 会**跟着根走**（根是用户指定的范围，不是一个被发现的条目），
            # MFT 走不了：它只认 MFT 记录里的父子关系，junction 指向的内容在 MFT 里
            # 挂在自己的真实路径下，与这个根的名字没有关系。两边口径不同就直说，
            # 不装作扫过了。
            stats.roots_unreachable.append(
                (raw, "根是一个 reparse point（junction/符号链接），MFT 看不见它指向的内容")
            )
            return None
        frn = int(stat.st_ino)
        if frn == 0:
            stats.roots_unreachable.append((raw, "拿不到文件索引号（FRN），无法在 MFT 里定位这个根"))
            return None
        # 保留大小写的正斜杠形式：`ScanEntry.path` 与 `scandir` 那边是同一个写法
        # （`raw` 去尾斜杠 + 分隔符统一），`key` 才走 `normalize`。
        return _RootTarget(raw=raw, norm=norm, path=raw.replace("\\", "/").rstrip("/"),
                           slot=frn & _RECORD_MASK, frn=frn)

    # ------------------------------------------------------------------ 一个根

    def _run_root(
        self,
        target: _RootTarget,
        index: _VolumeIndex,
        rules: IgnoreRules,
        stats: ScanStats,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
        *,
        first: bool,
    ) -> bool:
        """扫完这个根返回 True；根在记录流里对不上号时记一笔并返回 False。"""
        assert stats.mft is not None
        if target.slot >= len(index) or index.frns[target.slot] != target.frn:
            stats.roots_unreachable.append(
                (target.raw, "MFT 里找不到这个根的记录（序号对不上：两次读取之间被重建了？）")
            )
            return False

        n = len(index)
        pass_ = _Pass(
            root_of=array("i", bytes(4 * n)),
            depth=array("i", bytes(4 * n)),
            dir_state=bytearray(n),
            inherited=bytearray(n),
        )
        buckets: dict[int, array] = {}
        for slot in index.slots:
            if slot == target.slot:
                continue
            code, depth = self._locate(slot, index, target.frn, pass_, stats)
            if code == 1:
                buckets.setdefault(depth, array("I")).append(slot)
            elif first:
                # 只在第一个根那一轮数：这几个数是**按卷**的账（整卷只有一份），
                # 换个根再数一遍会把同一批记录算两次，而根数通常就是 1。
                if code == 3:
                    stats.mft.system += 1
                else:
                    stats.mft.outside_roots += 1

        pass_.dir_state[target.slot] = 1
        # 目录路径按深度分层留着，只保留相邻两层：一层目录的路径在它的**孙辈**
        # 判完之前不再有人用。C: 上 67 万个目录的全量路径字典是上百 MB，而同一时刻
        # 只有相邻两层是活的。
        paths: dict[int, dict[int, str]] = {0: {target.slot: target.path}}

        for depth in sorted(buckets):
            parent_paths = paths.get(depth - 1, {})
            this_paths: dict[int, str] = {}
            for slot in buckets[depth]:
                parent_slot = index.parents[slot] & _RECORD_MASK
                if pass_.dir_state[parent_slot] != 1:
                    # 祖先被剪枝（规则排除 / 标记 / 嵌套根）：`scandir` 从来没走到
                    # 这里，所以它连"跳过"都算不上，单独计数。
                    stats.mft.hidden += 1
                    if index.is_dir(slot):
                        pass_.dir_state[slot] = 2
                    continue
                path = parent_paths[parent_slot] + "/" + index.name(slot)
                inherited = pass_.inherited[parent_slot]
                action = self._handle(
                    slot, index, path, target, rules, stats, pass_,
                    inherited=Action.NAME_ONLY if inherited else None,
                    on_entry=on_entry, on_skip=on_skip,
                )
                if index.is_dir(slot):
                    if action is Action.SKIP:
                        pass_.dir_state[slot] = 2
                    else:
                        pass_.dir_state[slot] = 1
                        this_paths[slot] = path
                        # NAME_ONLY 的目录把处置传给整棵子树；INDEX 的目录把**父级**
                        # 的继承传下去（自己这条规则不该替子项做主）——与 scandir
                        # 压栈时那行完全同义。
                        pass_.inherited[slot] = (1 if action is Action.NAME_ONLY else inherited)
            paths[depth] = this_paths
            paths.pop(depth - 1, None)
        return True

    # ------------------------------------------------------------------ 上溯

    def _locate(
        self, slot: int, index: _VolumeIndex, root_frn: int, pass_: _Pass, stats: ScanStats
    ) -> tuple[int, int]:
        """从 `slot` 沿 `ParentFRN` 上溯，判它是不是在扫描根下，并给出**深度**。

        返回 `(code, depth)`：`1` 在根下、`2` 不在根下、`3` 落在 NTFS 保留记录的
        子树里（Win32 看不见）。深度以扫描根为 0。

        **必须记忆化**：轨迹是一条到根的链，同一棵树上的每个节点各走一遍就是
        O(N × 深度)；记忆化之后每个槽只算一次（连带把整条链填上），总计 O(N)。
        表是按记录号索引的定长数组，不是 dict——见 `_Pass` 的注释。
        """
        assert stats.mft is not None
        cached = pass_.root_of[slot]
        if cached:
            return cached, pass_.depth[slot]

        n = len(index)
        chain = [slot]
        cur = slot
        while True:
            if index.frns[cur] == root_frn:                 # 根自己
                code, depth = 1, 0
                break
            if (index.frns[cur] & _RECORD_MASK) < FIRST_USER_RECORD:
                # 卷根（记录 5）与其它保留记录：`del` 得到的是"上不去了"，
                # 而 $Extend 那一棵本来就不该出现在索引里。
                code, depth = 3, 0
                break
            parent = index.parents[cur]
            # 0 = 没有父（`parents` 的哨兵）。FRN 0 不可能是一条真记录：记录 0 是
            # `$MFT`，它的序号从 1 起算。
            parent_slot = parent & _RECORD_MASK
            if parent == 0 or parent_slot >= n or parent_slot == cur:
                code, depth = 2, 0                      # 断链 / 自环 / 父记录不在枚举里
                break
            parent_frn = index.frns[parent_slot]
            if parent_frn == 0:
                code, depth = 2, 0                      # 父槽位空着
                break
            if parent_frn != parent:
                # 父记录的**序号**对不上：那条记录被删过、槽位已经给了别人。
                # 只比记录号会把两个不同的目录认成一个（`ntfs.Record` 的注释）。
                stats.mft.broken_links += 1
                code, depth = 2, 0
                break
            if parent_frn == root_frn:
                code, depth = 1, 1
                break
            cached = pass_.root_of[parent_slot]
            if cached:
                code, depth = cached, pass_.depth[parent_slot] + 1
                break
            if len(chain) >= n:
                # **环**：A 的父是 B、B 的父是 A。健康的卷上不会有（目录树是
                # 无环的），坏卷上却会把这条上溯变成**死循环**——而"扫不完"比
                # "数错"难查得多，它连一行输出都没有。链里的槽不会重复，所以
                # 链长到不了记录总数；到了就一定是环。
                # 自环（父是自己）由上面 `parent_slot == cur` 挡掉，这里管更长的环。
                code, depth = 2, 0
                break
            cur = parent_slot
            chain.append(cur)

        # 回填：`chain` 是从叶到顶的顺序，`chain[-1]` 的父就是上面定下来的那个。
        for node in reversed(chain):
            pass_.root_of[node] = code
            pass_.depth[node] = depth
            if code == 1:
                depth += 1
        return code, pass_.depth[slot]

    # ------------------------------------------------------------------ 一条

    def _handle(
        self,
        slot: int,
        index: _VolumeIndex,
        path: str,
        target: _RootTarget,
        rules: IgnoreRules,
        stats: ScanStats,
        pass_: _Pass,
        *,
        inherited: Action | None,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
    ) -> Action:
        """一条记录的判定，返回它的 `Action`。**步骤与顺序逐条对应
        `ScandirScanner._handle`**：reparse → data_dir → 嵌套根 → 目录级标记 →
        规则 → `max_file_mb`。

        顺序是有依赖的（比如标记必须在规则之前、剪枝必须在下潜之前），两处不同步
        就会得到"同一个目录在两个后端里处置不同"这种最难查的差异。测试
        `tests/unit/test_scanner_parity.py` 就是拿同一棵树跑两边来钉这一点的。

        返回值**只对目录有意义**：父目录是 SKIP 时，它的子孙在 `scandir` 那边
        从来没被看到过，这里也得当成没看到（`_run_root` 用它决定剪枝传递）。
        """
        assert stats.mft is not None
        name = index.name(slot)
        norm = normalize(path)
        is_dir = index.is_dir(slot)
        attrs = index.attrs[slot]
        # 枚举白送的，`scandir` 那边要**额外问一次**才拿得到（`base.ScanEntry.frn`；
        # 那是它相对这里唯一的、真正多出来的开销。曾经引的 36 µs 对拍数在
        # `F2-02` 真机核对里没复现，见模块文档）
        frn = index.frns[slot]

        # 属性：MFT 记录里只有属性位，**没有大小、没有时间**（见 `ntfs.py`）。
        # reparse 条目走 `os.stat`（要 tag，`GetFileAttributesExW` 不给），
        # 其余走 `GetFileAttributesExW`（理由见 `ntfs.py` 的模块文档：快慢已经
        # 不是理由了，不开句柄才是）。
        # 两支都把路径交给 `extended_path`：保留设备名的条目原样问会问到设备上。
        tag = 0
        try:
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                stat = os.stat(extended_path(path), follow_symlinks=False)
                tag = reparse_tag_of(stat)
                size, mtime_ns = stat.st_size, stat.st_mtime_ns
            else:
                info = ntfs.attributes_of(path)
                stats.mft.attributes_calls += 1
                size, mtime_ns = info.size, info.mtime_ns
        except OSError as exc:
            # 与 scandir 同形：**仍把它送出去**（"有一条路径我们完全无能为力"本身
            # 就是信息），但它既不进 dirs_seen 也不进 files_seen——那两个数的口径
            # 是"判得清的条目"，混进判不清的会让它失去意义。
            stats.add_error(path, f"读不到属性：{exc}")
            self._send(
                ScanEntry(
                    path=path, name=name, is_dir=False, size=-1, mtime_ns=-1,
                    action=Action.SKIP,
                    decision=rules.decide(path, is_dir=False, inherited=inherited),
                    root=target.norm, reason=f"读不到属性：{exc}", error=str(exc), frn=frn,
                ),
                on_skip,
            )
            # 返回 SKIP 而不是 None：**目录读不到属性就不下潜**（scandir 是"不压栈"，
            # 同义）。返回 None 会让它的子孙照常被发出去，而它们的父目录从没被
            # 判定过——两个后端从这里分叉，且只在"某个目录读不到属性"时才显形。
            return Action.SKIP

        if is_dir:
            stats.dirs_seen += 1
            size = 0
        else:
            stats.files_seen += 1

        def entry(action: Action, reason: str, tag_: int = 0) -> ScanEntry:
            return ScanEntry(
                path=path, name=name, is_dir=is_dir, size=size, mtime_ns=mtime_ns,
                action=action, decision=rules.decide(path, is_dir=is_dir, inherited=inherited),
                root=target.norm, reason=reason, reparse_tag=tag_, frn=frn,
            )

        # 1) reparse point：目录不下潜、文件只记名字（理由见 `scanner_scandir` 的文档）
        if tag != 0:
            stats.add_reparse(tag)
            if is_dir:
                stats.dirs_reparse += 1
                self._send(entry(Action.SKIP, reparse_dir_reason(tag), tag), on_skip)
            else:
                stats.files_name_only += 1
                self._send(entry(Action.NAME_ONLY, reparse_file_reason(tag), tag), on_entry)
            return Action.SKIP if is_dir else Action.NAME_ONLY

        # 2) data_dir 硬性剔除（只对目录判：文件不可能在没进入的目录里）
        if is_dir and inside(norm, self._data_dir):
            stats.dirs_pruned += 1
            self._send(entry(Action.SKIP, DATA_DIR_REASON), on_skip)
            return Action.SKIP

        # 3) 另一个扫描根：不在这里扫，避免同一个目录被两轮各数一遍
        if is_dir and norm in self._root_set and norm != target.norm:
            stats.dirs_pruned += 1
            self._send(entry(Action.SKIP, NESTED_ROOT_REASON), on_skip)
            return Action.SKIP

        # 4) 目录级标记 `.lsearch-noindex`
        if is_dir and has_noindex_marker(path):
            stats.dirs_pruned += 1
            self._send(entry(Action.SKIP, marker_reason()), on_skip)
            return Action.SKIP

        # 5) 其余交给规则
        decision = rules.decide(path, is_dir=is_dir, inherited=inherited)
        action = decision.action

        if action is Action.SKIP:
            if is_dir:
                stats.dirs_pruned += 1
            else:
                stats.files_skipped += 1
            self._send(
                ScanEntry(
                    path=path, name=name, is_dir=is_dir, size=size, mtime_ns=mtime_ns,
                    action=action, decision=decision, root=target.norm, reason=decision.reason,
                    frn=frn,
                ),
                on_skip,
            )
            return action

        if is_dir:
            self._send(
                ScanEntry(
                    path=path, name=name, is_dir=True, size=0, mtime_ns=mtime_ns,
                    action=action, decision=decision, root=target.norm, reason=decision.reason,
                    frn=frn,
                ),
                on_entry,
            )
            return action

        reason = decision.reason
        if action is Action.INDEX and size > self.config.max_file_bytes:
            action = Action.NAME_ONLY
            reason = over_limit_reason(self.config.max_file_mb, size)
            stats.files_over_limit += 1
        if action is Action.INDEX:
            stats.files_index += 1
            stats.bytes_index += size
        else:
            stats.files_name_only += 1
        self._send(
            ScanEntry(
                path=path, name=name, is_dir=False, size=size, mtime_ns=mtime_ns,
                action=action, decision=decision, root=target.norm, reason=reason, frn=frn,
            ),
            on_entry,
        )
        return action

    @staticmethod
    def _send(entry: ScanEntry, callback: EntryCallback | None) -> None:
        if callback is not None:
            callback(entry)
