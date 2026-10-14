"""`F2-02` 的真机验收：同一批 roots 跑两个后端，**按 FRN 折叠硬链接后核对集合相等**。

对应功能点 **F2-02**，关联验收项 **A1**（全量索引无未捕获异常）与 **A9**（去重）。
判定本身在 `ingest/parity.py`（有单测，`tests/unit/test_scanner_parity.py`），本脚本
只做三件事：**跑那两个后端、喂给 `ParityCheck`、把账目打出来**。判定逻辑一行都不
在这儿——两处各有一份的话，CLI 与脚本迟早给出不同的结论。

---- 为什么判据不是"数字相等" -------------------------------------------------

`F0-04` 实测（2026-10-14，C: 同一块盘）：MFT 侧 2,055,857 条文件、scandir 侧
2,814,788 条，**差 758,931**——多出来的全是硬链接的第二个、第三个名字（MFT 数的是
**记录**，scandir 数的是**目录项**）。所以判据是"折叠后集合相等"，`ParityCheck`
的 `ok` 就是它。完整的推导在 `parity.py` 的模块文档里。

---- 为什么这件事必须在提权终端里做 -------------------------------------------

`MftScanner` 走 `FSCTL_ENUM_USN_DATA`，要的是卷设备句柄（`\\\\.\\C:`），不提权时
`CreateFile` 返回 `ERROR_ACCESS_DENIED(5)`。所以本脚本一进来就探一次，探不过就
**明确报"这一轮不是证据"并退出 2**——与 `probe_mft.py` 同一个约定，理由也一样：
一次没提权的运行如果退出 0，它看起来和一次真正的验收没有区别。

本脚本**只读**：不创建/删除 USN Journal，不写卷上的任何东西。读的是文件属性
（`GetFileAttributesExW`）与 MFT 记录本身。

---- scandir 读不到的目录会凭空造出 `mft_only` ---------------------------------

`mft_only`（MFT 报了、scandir 没报）**必须为空**，否则不是通过。但它有一种与
实现无关的成因：scandir 那一侧某个目录**权限被拒**，整棵子树就没被走下去，于是
MFT 报的那些路径全都"只有 MFT 有"。所以报告里把 scandir 的「读不到的」单独列出来，
并且结论不通过时提醒一句——**先去那里看一眼，再怀疑后端**。

---- 口径（先看这里，否则数字没法比）-------------------------------------------

  * 两侧**条目数本来就该不等**（差的那一大片是硬链接）。相等反而要查。
  * `MftStats.records` 是**整卷**的记录数，含不在扫描根下的、`$Extend` 那一片保留
    记录、以及被剪枝的子树——与 `scandir` 的条目数不是一个东西，别相减。它们的
    归属在报告里分栏列出（`outside_roots` / `system` / `hidden`）。
  * FRN 只对**键对不上 MFT 的那些**条目问一次 `os.stat`。C: 上那是 758,931 次
    （约 27 s）而不是 347 万次（约 125 s）——`frn_calls` 就是这件事的账。
    问不到（`OSError`）时记 `frn_failures` 并**按"无法解释"计**，不默认放过。
  * **内存**：MFT 那一侧整轮留在内存里（`ParityCheck` 要它来判 `mft_only`），
    实测 **573 字节/条**（2026-10-15，`tracemalloc`，30 万条外推）——C: 全盘一轮
    峰值约 **1.2 GB**，另有 FRN 集合约 0.1 GB。跑整卷前先看一眼可用内存。分多个
    根跑不省内存（一张表按根共享），要省就**一个根一轮**。

---- 用法（仓库根执行）-----------------------------------------------------

    # 真机验收：必须在管理员终端里。不给 roots 就用 config/config.toml 的 [paths] roots
    PYTHONUTF8=1 ./.venv/Scripts/python.exe -u tools/verify_mft_parity.py --log 报告.md
    ... tools/verify_mft_parity.py D:/work C:/Windows --json > 报告.json
    ... tools/verify_mft_parity.py D:/work --progress 200000     # 长跑时的进度（stderr）

    # 自动化测试与演示用：MFT 侧换成**从一棵真目录树合成**的记录流，不需要提权。
    # ⚠ 它走的判据与真机完全一样，但记录流是合成的，**不能替代管理员终端里的那一轮**。
    ... tools/verify_mft_parity.py D:/work --mft-from-tree D:/work

原始输出落盘用 `--log <路径>`（等价于 `> 路径`）——与 `probe_mft.py` 同一个
理由：**脚本不参与排版**，也就不可能把失败修饰成好看的样子。⚠ 进度与警告走
**stderr**，所以重定向时用 `>`（只收 stdout），用 `&>` 会把它们也收进报告。

**不要在打印路径上引入 GBK 编不了的字符**：U+2212（减号）踩过一次
（2026-10-15 01:24:49，见 `PROGRESS.md` §4 P2 的 F2-05 那条），整轮实测的输出一条
都没留下。`tests/unit/test_verify_mft_parity.py` 在 gbk 与 utf-8 两种编码下各跑一遍
本脚本，专门防这件事复发。⚠ 这只保证了**本脚本自己写下的字**：样本里要打的路径
来自文件系统，不受我们控制（真有个文件名带 U+2212 就还是编不出来）。所以整卷长跑
用 **`--log`**（写文件固定 UTF-8），别用 `>`（那用的是控制台编码）。

退出码：
  0  核对**通过**（判据：`ParityCheck.ok`）
  1  核对**不通过**（差异列在上面）
  2  这一轮**不能作为证据**：没提权 / 根扫不成 / 两侧都是空的 / 配置不对
  3  未预期异常（栈回溯在 stderr）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from localsearch.core.config import ConfigError, load_config
from localsearch.ingest import ntfs
from localsearch.ingest.base import (
    MftStats,
    ScanConfig,
    ScanEntry,
    ScanError,
    ScanStats,
    check_rules_roots,
)
from localsearch.ingest.filters import PROJECT_ROOT, IgnoreRules
from localsearch.ingest.parity import ParityCheck
from localsearch.ingest.scanner_mft import MftScanner, VolumeSource, probe
from localsearch.ingest.scanner_scandir import ScandirScanner

#: 每打印一次进度（stderr；0 关掉）。默认 50 万条——小树上完全安静，整卷才出声。
PROGRESS_EVERY = 500_000

#: 每一类差异默认打几条样本。计数是准的，样本只是"长什么样"（`parity.MAX_SAMPLES`）。
SAMPLES = 10


class CannotRun(Exception):
    """这一轮**跑不起来**，因此不构成证据。与"核对了但不通过"是两回事。

    分开的理由很实际：两者都会让人去查东西，但查的是不同的地方——跑不起来查权限与
    配置，不通过查后端与差异。合成一个非零退出码，报告就给不出这个区别。
    """


# ==============================================================================
# 喂给 ParityCheck 的三个零件：记录流、进度、FRN
# ==============================================================================


def records_of_tree(root: Path) -> list[ntfs.Record]:
    """一棵真目录树 → 一份 MFT 记录流（**按 FRN 去重**：一个文件一条记录，这正是
    硬链接的第二个名字不在流里的原因）。

    只给 `--mft-from-tree` 用：它的意义是"不用提权也能把整条链路跑通一遍"，所以
    它必须与真卷上那份记录流**在结构上同义**（一个文件一条、目录也是记录、reparse
    的目录不下潜）。

    ⚠ 每条条目的 FRN 靠 `os.stat` 现问，**不能用 `DirEntry.stat()`**——后者在
    Windows 上返回的 `st_ino` 是 0（`F0-04` 实测），而 FRN 正是这份流的一切。

    ⚠ `tests/unit/test_scanner_parity.py` 里有一份**刻意重复**的同义实现（差分测试
    不该依赖被测侧的测试脚手架）。改这里时看一眼那边是不是也该改。
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
                name=directory.name,
                attrs=int(directory_stat.st_file_attributes),
            ))
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


def tree_source(root: Path) -> VolumeSource:
    """把一棵真树包成 `MftScanner` 认的 `VolumeSource`（`--mft-from-tree`）。

    `counters.records` 必须自己填：真机上它是"枚举到多少条记录"的账，而
    `MftStats.records` 就取自它——不填会让报告里的整卷记录数印成 0，看起来像
    "一条记录都没读到但一切正常"。
    """
    records = records_of_tree(root)

    @contextmanager
    def source(device: str, counters: ntfs.RecordCounters) -> Iterator[ntfs.Record]:
        counters.records = len(records)
        yield iter(records)

    return source


def counting(callback: Callable[[ScanEntry], None], label: str, *, every: int
             ) -> Callable[[ScanEntry], None]:
    """给一个回调加进度打印（**stderr**，stdout 留给报告）。

    长跑（C: 全盘 scandir 要几十分钟）没有输出会让人怀疑它挂了。`every <= 0` 时
    原样返回——`ParityCheck` 的回调本身是热的，别给它套一层无用的闭包。
    """
    if every <= 0:
        return callback
    started = time.perf_counter()
    state = {"n": 0}

    def wrapped(entry: ScanEntry) -> None:
        state["n"] += 1
        if state["n"] % every == 0:
            print(f"[{label}] {state['n']:,} 条，已用 {time.perf_counter() - started:.1f} s",
                  file=sys.stderr, flush=True)
        callback(entry)

    return wrapped


class FrnLookup:
    """`ParityCheck` 的 `frn_of`：`os.stat` 的 `st_ino` **就是** FRN（`F0-04` 实测）。

    只在键对不上 MFT 时才被调用（`parity.py` 的省钱之处），所以这里的 `calls` 是
    "差异有多少"的账，不是"条目有多少"。问不到时返回 0（= 不知道）：`ParityCheck`
    会把它记成"无法解释"，**不能**在这里猜一个能对上的数——那会把真差异静默吞掉。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0

    def __call__(self, path: str) -> int:
        self.calls += 1
        try:
            return int(os.stat(path, follow_symlinks=False).st_ino)
        except OSError:
            self.failures += 1
            return 0


# ==============================================================================
# 跑一轮
# ==============================================================================


def run(
    roots: tuple[str, ...],
    ignore_file: Path,
    *,
    data_dir: str,
    follow: bool = False,
    mft_source: VolumeSource | None = None,
    progress_every: int = PROGRESS_EVERY,
) -> dict:
    """两个后端各跑一轮，边跑边喂 `ParityCheck`。

    **MFT 那一轮必须先跑完**：`ParityCheck` 要按路径建一份索引才知道"scandir 多出来
    的名字是不是硬链接"（`parity.py` 的用法约定）。反过来喂会让每一条都落进
    `scandir_only`。

    `mft_source` 给了就**不探测**（合成的记录流不需要提权，也没有卷可探）——这是
    `--mft-from-tree` 与自动化测试的入口。
    """
    config = ScanConfig(roots=roots, data_dir=data_dir, follow_symlinks=follow)
    rules = IgnoreRules.from_file(ignore_file, roots=roots)
    check_rules_roots(rules, roots)         # 两边 roots 不一致会当场报错，不静默不命中

    probe_reason = ""
    if mft_source is None:
        result = probe(config)
        if not result.ok:
            raise CannotRun(f"MFT 后端在本机用不了：{result.reason}")
        probe_reason = result.reason

    check = ParityCheck()
    lookup = FrnLookup()

    started = time.perf_counter()
    mft_stats = MftScanner(config, source=mft_source).scan(
        rules=rules,
        on_entry=counting(check.add_mft, "mft", every=progress_every),
    )
    mft_seconds = time.perf_counter() - started

    def scandir_push(entry: ScanEntry) -> None:
        check.add_scandir(entry, lookup)

    started = time.perf_counter()
    scandir_stats = ScandirScanner(config).scan(
        rules=rules,
        on_entry=counting(scandir_push, "scandir", every=progress_every),
    )
    scandir_seconds = time.perf_counter() - started

    return {
        "roots": list(roots),
        "ignore_file": str(ignore_file),
        "rules_total": len(rules.rules),
        "mft_side": "synthesized" if mft_source is not None else "real",
        "probe_reason": probe_reason,
        "parity": check.result(),
        "mft_stats": mft_stats,
        "scandir_stats": scandir_stats,
        "frn_calls": lookup.calls,
        "frn_failures": lookup.failures,
        "mft_seconds": mft_seconds,
        "scandir_seconds": scandir_seconds,
    }


# ==============================================================================
# 判定：这一轮算不算证据
# ==============================================================================


def evidence_problem(out: dict) -> str:
    """不构成证据的原因；是证据则返回 `""`。

    两条都要挡：
    - **MFT 那侧一个根都没扫成**（根在记录流里对不上号、卷读不了）——这时
      `ParityCheck` 眼里 MFT 侧是空的，scandir 侧每一条都会变成"无法解释"，
      `ok` 是 `False` 偶然正确，但原因在配置而不在差异。
    - **两侧都没比到任何条目**——那是一次"两边都空所以相等"的假通过：`ok` 是
      `True`，而它证明不了任何事。**这正是"看起来成功了"的那种失败**。
    """
    mft_stats = out["mft_stats"]
    parity = out["parity"]
    if mft_stats.roots_scanned == 0:
        reasons = "、".join(f"{raw}：{why}" for raw, why in mft_stats.roots_unreachable)
        return f"MFT 那一侧一个根都没扫成（{reasons or '原因见上面的账目'}）"
    if parity.mft_entries == 0 and parity.scandir_entries == 0:
        return "两侧都没比到任何条目：roots 是不是指到了空目录？"
    return ""


# ==============================================================================
# 呈现
# ==============================================================================


def print_stats(title: str, stats: ScanStats, mft: MftStats | None = None) -> None:
    """一个后端的账目。`mft` 给了就多印 MFT 独有的那几栏。

    ⚠ 减号一律用 ASCII 的 `-`：U+2212 是 GBK 编不了的字符，本机默认控制台（代码页
    936）会抛 `UnicodeEncodeError`，把整轮输出全吃掉（2026-10-15 01:24:49 实测过一次）。
    """
    print(f"\n{title}的账目")
    print(f"    根目录        {stats.roots_scanned} / {stats.roots_total} 可达")
    for raw, why in stats.roots_unreachable:
        print(f"      没扫        {raw}：{why}")
    print(f"    条目          {stats.entries_seen:,}"
          f"（目录 {stats.dirs_seen:,} + 文件 {stats.files_seen:,}）")
    if mft is None:
        print(f"    进入的目录    {stats.dirs_entered:,}"
              f"（剪掉 {stats.dirs_pruned:,} / reparse {stats.dirs_reparse:,}）")
    else:
        # MFT 从不打开目录，`dirs_entered` 恒为 0（`base.ScanStats` 里写了理由）。
        # 印出来会让人以为"少走了这么多目录"——不印，改印剪枝那一栏。
        print(f"    剪掉的目录    {stats.dirs_pruned:,} / reparse {stats.dirs_reparse:,}"
              "（MFT 不打开目录，「进入的目录」在它这一侧不存在）")
    print(f"    文件：正文    {stats.files_index:,}（{stats.bytes_index / 1024**3:.2f} GiB）"
          f" / 仅名字 {stats.files_name_only:,} / 排除 {stats.files_skipped:,}")
    if stats.reparse_tags:
        tags = "  ".join(f"0x{t:X}x{n}" for t, n in sorted(stats.reparse_tags.items()))
        print(f"    reparse tag   {tags}")
    print(f"    耗时          {stats.seconds:.2f} s")
    # 「读不到的」要显眼：scandir 少走一棵子树，会凭空造出一片 mft_only（见模块文档）
    print(f"    读不到的      {stats.errors_total:,} 条（样本最多 {len(stats.errors)}）")
    for path, why in stats.errors:
        print(f"      {path}：{why}")

    if mft is not None:
        print(f"    整卷记录      {mft.records:,}（枚举 {mft.volumes} 个卷，"
              f"耗时 {mft.enumerate_seconds:.2f} s）")
        print(f"      不在根下    {mft.outside_roots:,}")
        print(f"      系统保留    {mft.system:,}（记录 0-15 及其子树，Win32 枚举看不见它们）")
        print(f"      被剪枝      {mft.hidden:,}（在根下但祖先被剪掉）")
        print(f"    补属性        {mft.attributes_calls:,} 次 GetFileAttributesExW")
        print(f"    父记录断链    {mft.broken_links:,}（正常卷上应当是 0）")
        print(f"    坏记录        {mft.bad_records:,} / 非 V2 {mft.other_versions:,}")


def print_samples(parity, *, limit: int) -> None:
    """四类差异各打几条样本。计数已经印过了，这里只回答"长什么样"。"""
    groups = [
        ("仅有 MFT（应当为空）", parity.mft_only_total, parity.mft_only,
         lambda s: f"{s}"),
        ("无法解释（仅有 scandir 且 FRN 对不上）", parity.unexplained_total,
         parity.unexplained, lambda s: f"{s}"),
        ("处置不一致（MFT 侧 / scandir 侧）", parity.action_mismatch_total,
         parity.action_mismatch, lambda s: f"{s[0]}：{s[1]} vs {s[2]}"),
        ("文件 / 目录判断不一致（MFT 侧 / scandir 侧）", parity.is_dir_mismatch_total,
         parity.is_dir_mismatch, lambda s: f"{s[0]}：{s[1]} vs {s[2]}"),
    ]
    for title, total, samples, fmt in groups:
        if not total:
            continue
        print(f"\n  {title}：{total:,} 条，样本 {len(samples)} 条")
        for item in samples[:limit]:
            print(f"      {fmt(item)}")


def print_report(out: dict, *, samples: int) -> None:
    parity = out["parity"]
    print(f"扫描根        {'、'.join(out['roots'])}")
    print(f"排除规则      {out['ignore_file']}（{out['rules_total']} 条）")
    print(f"机器时间      {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if out["mft_side"] == "synthesized":
        print("MFT 侧        合成记录流（--mft-from-tree，不碰卷、不需要提权）")
        print("              **这一轮不是 F2-02 的证据**：真机那一轮必须在管理员终端里跑")
    else:
        print(f"MFT 后端探测  {out['probe_reason']}")
    print_stats("MFT 后端", out["mft_stats"], out["mft_stats"].mft)
    print_stats("scandir 后端", out["scandir_stats"])

    print("\n核对（判据：按 FRN 折叠硬链接后集合相等）")
    print(f"    {parity.summary()}")
    print(f"    问 FRN         {out['frn_calls']:,} 次（只问键对不上的那些）"
          f"，问不到 {out['frn_failures']:,} 次")
    print(f"    两轮耗时      MFT {out['mft_seconds']:.2f} s / scandir {out['scandir_seconds']:.2f} s")
    print_samples(parity, limit=samples)

    problem = evidence_problem(out)
    if problem:
        print(f"\n    结论          **不能作为证据**：{problem}")
    else:
        print(f"\n    结论          {'通过' if parity.ok else '不通过'}")
        # 不通过时先指向最可能的"与实现无关"的成因，别让人一上来就怀疑后端
        if not parity.ok and out["scandir_stats"].errors_total:
            print(f"      ⚠ scandir 这一轮有 {out['scandir_stats'].errors_total:,} 条读不到的路径："
                  "「仅有 MFT」里有多少是它们造成的，先看一眼上面的账目")


# ==============================================================================
# 命令行
# ==============================================================================


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", help="要核对的目录（默认为配置里的 [paths] roots）")
    ap.add_argument("--ignore-file", default=str(PROJECT_ROOT / ".lsearchignore"),
                    help="排除规则文件。⚠ 两个后端必须拿到同一份规则，否则比的是规则不是后端")
    ap.add_argument("--data-dir", default=None, help="默认为配置里的 [paths] data_dir")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="跟随 reparse point。⚠ v1 两个后端都会拒绝：见 R4")
    ap.add_argument("--mft-from-tree", default=None, metavar="目录",
                    help="**仅供自动化测试与演示**：MFT 侧改用从这棵真目录树合成的记录流，"
                         "不需要提权，因此这一轮不能当作 F2-02 的证据")
    ap.add_argument("--progress", type=int, default=PROGRESS_EVERY, metavar="N",
                    help=f"每 N 条往 stderr 打一次进度（默认 {PROGRESS_EVERY}，0 关掉）")
    ap.add_argument("--samples", type=int, default=SAMPLES,
                    help=f"每一类差异最多打几条样本（默认 {SAMPLES}）")
    ap.add_argument("--log", default=None, metavar="路径",
                    help="把报告同时写一份到这个文件（与 `> 路径` 等价）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给记录用）")
    return ap


def json_payload(out: dict) -> dict:
    """JSON 出口。**判据与结论都在里面**，所以报告可以只引用它。

    `is_evidence` 单独给一个布尔：拿到 JSON 的人（包括将来的 CI）不该靠"有没有
    `--mft-from-tree`"去推断这一轮算不算数。
    """
    parity = out["parity"]
    return {
        "roots": out["roots"],
        "ignore_file": out["ignore_file"],
        "rules_total": out["rules_total"],
        "mft_side": out["mft_side"],
        "is_evidence": out["mft_side"] == "real" and not evidence_problem(out),
        "probe_reason": out["probe_reason"],
        "ok": parity.ok,
        "problem": evidence_problem(out),
        "parity": dict(vars(parity)),
        "frn_calls": out["frn_calls"],
        "frn_failures": out["frn_failures"],
        "mft_seconds": out["mft_seconds"],
        "scandir_seconds": out["scandir_seconds"],
        "mft_stats": _stats_json(out["mft_stats"]),
        "scandir_stats": _stats_json(out["scandir_stats"]),
    }


def _stats_json(stats: ScanStats | MftStats) -> dict:
    """账目转 JSON。`reparse_tags` 的键是 int，JSON 的键只能是字符串，按十六进制写。"""
    payload = dict(vars(stats))
    mft = payload.get("mft")
    if mft is not None:                     # ScanStats.mft 是嵌套的 MftStats
        payload["mft"] = dict(vars(mft))
    tags = payload.get("reparse_tags")
    if tags is not None:
        payload["reparse_tags"] = {f"0x{t:X}": n for t, n in tags.items()}
    return payload


def main(argv: list[str] | None = None, *,
         mft_source: VolumeSource | None = None) -> int:
    """`mft_source` 只给自动化测试用（传进来就不用提权，也不会碰卷）。

    命令行上的 `--mft-from-tree` 会自动填它；真正的提权验收两个都不给。
    """
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        # 配置读不了，`roots` 与 `data_dir` 就都没得问：只认命令行上显式给的那两个，
        # 缺一个就当场说清楚。**不猜**一个默认值往下跑——那会让人以为"用的是配置"。
        if args.roots and args.data_dir:
            config = None
        else:
            print(f"配置读不了（{exc}）：这一轮得同时用命令行给出 roots 与 --data-dir。",
                  file=sys.stderr)
            return 2

    roots = tuple(args.roots) if args.roots else (tuple(config.paths.roots) if config else ())
    if not roots:
        print("没有 roots：要么在命令行给，要么在 config/config.toml 的 [paths] 里写。",
              file=sys.stderr)
        return 2
    data_dir = args.data_dir or (config.paths.data_dir if config else "")
    if not data_dir:
        print("没有 data_dir：要么在配置里写，要么用 --data-dir 给。", file=sys.stderr)
        return 2
    ignore_file = Path(args.ignore_file)
    if not ignore_file.exists():
        print(f"排除规则文件不存在：{ignore_file}", file=sys.stderr)
        return 2

    source = mft_source
    if source is None and args.mft_from_tree:
        source = tree_source(Path(args.mft_from_tree))

    try:
        out = run(roots, ignore_file, data_dir=data_dir,
                  follow=args.follow_symlinks, mft_source=source,
                  progress_every=args.progress)
    except (CannotRun, ScanError) as exc:
        # 这两类都是"这一轮不能作为证据"，不是"核对了但不通过"（见模块文档的退出码）
        print(f"这一轮跑不起来：{exc}", file=sys.stderr)
        return 2
    except Exception:                       # noqa: BLE001 - 兜底，输出栈回溯而不是吞掉
        traceback.print_exc(file=sys.stderr)
        return 3

    # 报告只有一份排版代码（`print_report`）：写文件时抓的就是它打出来的那一段，
    # 所以文件与终端**永远长得一样**，不会出现"文件里少了一栏"这种事。
    report = (_capture(print_report, out, samples=args.samples)
              if args.log or not args.json else None)
    if args.log:
        Path(args.log).write_text(report, encoding="utf-8")
    if report is not None and not args.json:
        print(report, end="")
    if args.json:
        print(json.dumps(json_payload(out), ensure_ascii=False, indent=2))

    problem = evidence_problem(out)
    if problem:
        print(f"⚠ 这一轮不能作为 F2-02 的证据：{problem}", file=sys.stderr)
        return 2
    return 0 if out["parity"].ok else 1


def _capture(fn: Callable[..., None], *args, **kwargs) -> str:
    """把一次 `print_*` 的输出抓成字符串（`--log` 用）。

    用 `redirect_stdout` 而不是让 print 自己带 `file=`：**报告只有一份排版代码**
    （`print_report`），终端与文件因此永远长得一样。
    """
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        fn(*args, **kwargs)
    return buffer.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
