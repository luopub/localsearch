r"""默认扫描后端：`os.scandir`（P2 / `F2-01`）。测试落点 `tests/unit/test_scanner_scandir.py`。

------------------------------------------------------------------------------
形态：一条栈，一层一层下潜
------------------------------------------------------------------------------

用显式栈而不是递归：目录深度在真机上是几十层，递归不会炸，但**剪枝要能表达成
"跳过整棵子树"**——递归里靠提前 return，栈里只是"不 push"。后者更难写错。

`os.scandir` 的 `DirEntry` 自带 stat 缓存（Windows 上目录枚举本身就把这些字段带回来
了），所以判定大小与属性**不额外打系统调用**。代价是缓存只在迭代过程中可靠，因此
本模块**边迭代边判定**，只把路径压栈——不把 `DirEntry` 攒起来。

------------------------------------------------------------------------------
三条只在真机上才暴露出来的坑
------------------------------------------------------------------------------

**1. reparse point 只计数、不下潜（`R4`，P0 实测）。**
   本机 `%LOCALAPPDATA%\Application Data` 这类 junction 指回自己所在的目录，下潜会
   把同一棵树数很多遍。实测在 `C:\Users\<user>\AppData\Local` 上：不下潜 **61.99 s /
   1,561,747 文件**，跳过后 **27.78 s / 1,549,646 文件 + 11 个 junction**——时间差
   **2.23×**、文件数虚高 **12,101**。C: 全盘共跳过 **422 个**（`E11`）。
   这不是优化，是**正确性**：不跳的话，"扫到多少个文件"这个数本身就是错的。

**2. reparse point 的「文件」给 `NAME_ONLY`，不是跳过。**
   跳过省事，但会把**云占位文件**（OneDrive 的"仅在线"文件就是 reparse point）从
   名字检索里一并抹掉——用户搜不到自己明明看得见的文件名，是个说不通的失败。而
   **读**它们会触发下载（机制推断，未实测）。`NAME_ONLY` 正好落在两者之间：名字进
   索引、正文不碰。符号链接文件同理（读它等于跟随链接）。

**3. 单条路径的错一律不中断整轮（`A1`）。**
   权限被拒的目录（C: 上实测 **354 个**）、扫描途中被删的文件、属性读不出来的条目，
   全部计数 + 留样本后继续。样本上限 `MAX_SAMPLES`，**计数不设上限**。

------------------------------------------------------------------------------
与 `filters` 的分工
------------------------------------------------------------------------------

本模块**不解释规则**：每一条都问 `IgnoreRules.decide()`，包括"要不要下潜"和
"子项继承什么"。它只负责三件规则管不到的事：**reparse point**、**`data_dir` 硬性
剔除**（计划 §4.5.1 的第 2 重机制——索引器在往数据目录里持续写文件，规则匹配对它
存在竞态）、**`max_file_mb` 上限**（计划 §8 的 e 条）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from localsearch.ingest.base import (
    Action,
    EntryCallback,
    ScanConfig,
    ScanEntry,
    ScanError,
    ScanStats,
    check_rules_roots,
    inside,
    path_of,
    reparse_tag_of,
)
from localsearch.ingest.filters import IgnoreRules, normalize

#: 硬性剔除时给出的原因。与规则给的原因**要能一眼分开**：规则能改，这一条不能。
DATA_DIR_REASON = "硬性剔除 data_dir（索引器正在往里写，规则匹配存在竞态）"
NESTED_ROOT_REASON = "这是另一个扫描根，由它自己那一轮负责"


class ScandirScanner:
    """`os.scandir` 栈式遍历。无需权限、无 NTFS 限制，是 `auto` 的兜底。"""

    name = "scandir"

    def __init__(self, config: ScanConfig) -> None:
        if not config.roots:
            raise ScanError(
                "[paths] roots 是空的：没有要扫的目录。扫描范围必须显式给出——"
                "本机判不出哪些卷会一直在（PROGRESS §3 的 E10），所以不会替你猜一个。"
            )
        if config.follow_symlinks:
            raise ScanError(
                "follow_symlinks = true 在 v1 里不支持：本机实测 junction 指回自身所在的"
                "目录，跟随会让同一棵树被数很多遍（R4：2.23× 时间、文件数虚高 12,101）。"
                "要开这一项，得先做环检测（记 (卷, 文件号) 对），那是另一件事。"
            )
        self.config = config
        self._roots = tuple(normalize(r) for r in config.roots)
        self._root_set = frozenset(self._roots)
        self._data_dir = normalize(config.data_dir) if config.data_dir else ""

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
        started = time.perf_counter()
        try:
            for raw, norm in zip(self.config.roots, self._roots):
                self._scan_root(raw, norm, rules, stats, on_entry, on_skip)
        finally:
            # 中途被调用方打断（回调抛异常）时也要留下已经花掉的时间——
            # 不然"为什么这次只有一半"这个问句就没有任何数据可查。
            stats.seconds = time.perf_counter() - started
        return stats

    # ------------------------------------------------------------------ 一个根

    def _scan_root(
        self,
        raw: str,
        norm: str,
        rules: IgnoreRules,
        stats: ScanStats,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
    ) -> None:
        """一个扫描根。**不可达只记不抛**——盘没插、网络盘没挂载都是运行时事实
        （`doctor` 的「索引根目录」一栏也是这么判的），不是配置错误。"""
        if self._inside_data_dir(norm):
            stats.roots_unreachable.append((raw, "在 data_dir 里面，按硬性剔除跳过"))
            return
        path = Path(raw)
        if not path.exists():
            stats.roots_unreachable.append((raw, "不存在或未挂载"))
            return
        if not path.is_dir():
            stats.roots_unreachable.append((raw, "不是一个目录"))
            return
        stats.roots_scanned += 1
        self._walk(path, norm, rules, stats, on_entry, on_skip)

    def _walk(
        self,
        root: Path,
        root_norm: str,
        rules: IgnoreRules,
        stats: ScanStats,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
    ) -> None:
        # 栈里存 (真实路径, 规范形, 继承来的处置)。继承只可能是 NAME_ONLY 或 None：
        # SKIP 的目录根本不会被压栈（剪枝就是"不压栈"）。
        stack: list[tuple[Path, str, Action | None]] = [(root, root_norm, None)]
        while stack:
            directory, dir_norm, inherited = stack.pop()
            stats.dirs_entered += 1
            try:
                with os.scandir(directory) as it:
                    for raw_entry in it:
                        self._handle(
                            raw_entry,
                            root_norm,
                            inherited,
                            rules,
                            stats,
                            on_entry,
                            on_skip,
                            stack,
                        )
            except OSError as exc:
                # 权限被拒 / 目录在扫描途中消失。C: 上实测有 354 个这样的目录。
                stats.add_error(str(directory), f"无法列出目录：{exc}")

    # ------------------------------------------------------------------ 一条

    def _handle(
        self,
        entry: os.DirEntry[str],
        root_norm: str,
        inherited: Action | None,
        rules: IgnoreRules,
        stats: ScanStats,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None,
        stack: list[tuple[Path, str, Action | None]],
    ) -> None:
        path = path_of(entry)
        name = entry.name
        norm = normalize(path)

        try:
            stat = entry.stat(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=False)
            tag = reparse_tag_of(stat)
        except OSError as exc:
            # 属性读不出来：连它是文件还是目录都不知道。仍然**把它送出去**
            # （`on_skip`），因为"有一条路径我们完全无能为力"本身就是信息——
            # 但它既不进 dirs_seen 也不进 files_seen，那两个数只收判得清的。
            stats.add_error(path, f"读不到属性：{exc}")
            self._send(
                ScanEntry(
                    path=path,
                    name=name,
                    is_dir=False,
                    size=-1,
                    mtime_ns=-1,
                    action=Action.SKIP,
                    decision=rules.decide(path, is_dir=False, inherited=inherited),
                    root=root_norm,
                    reason=f"读不到属性：{exc}",
                    error=str(exc),
                ),
                on_skip,
            )
            return

        if is_dir:
            stats.dirs_seen += 1
        else:
            stats.files_seen += 1

        # 1) reparse point：目录不下潜、文件只记名字（见模块文档的第 1、2 条）
        if tag != 0:
            stats.add_reparse(tag)
            if is_dir:
                stats.dirs_reparse += 1
                self._send(
                    self._entry(
                        path, name, stat, Action.SKIP,
                        f"reparse point（0x{tag:X}）：目录不下潜", tag, is_dir,
                        rules, inherited, root_norm,
                    ),
                    on_skip,
                )
            else:
                stats.files_name_only += 1
                self._send(
                    self._entry(
                        path, name, stat, Action.NAME_ONLY,
                        f"reparse point（0x{tag:X}）：只记名字，不读正文", tag, is_dir,
                        rules, inherited, root_norm,
                    ),
                    on_entry,
                )
            return

        # 2) data_dir 硬性剔除（只对目录判：文件不可能在没进入的目录里）
        if is_dir and self._inside_data_dir(norm):
            stats.dirs_pruned += 1
            self._send(
                self._entry(
                    path, name, stat, Action.SKIP, DATA_DIR_REASON, 0, is_dir,
                    rules, inherited, root_norm,
                ),
                on_skip,
            )
            return

        # 3) 另一个扫描根：不在这里扫，避免同一个目录被两轮各数一遍
        if is_dir and norm in self._root_set and norm != root_norm:
            stats.dirs_pruned += 1
            self._send(
                self._entry(
                    path, name, stat, Action.SKIP, NESTED_ROOT_REASON, 0, is_dir,
                    rules, inherited, root_norm,
                ),
                on_skip,
            )
            return

        # 4) 其余交给规则
        decision = rules.decide(path, is_dir=is_dir, inherited=inherited)
        action = decision.action

        if action is Action.SKIP:
            if is_dir:
                stats.dirs_pruned += 1
            else:
                stats.files_skipped += 1
            self._send(
                ScanEntry(
                    path=path, name=name, is_dir=is_dir,
                    size=0 if is_dir else stat.st_size, mtime_ns=stat.st_mtime_ns,
                    action=action, decision=decision, root=root_norm,
                    reason=decision.reason,
                ),
                on_skip,
            )
            return

        if is_dir:
            self._send(
                ScanEntry(
                    path=path, name=name, is_dir=True, size=0,
                    mtime_ns=stat.st_mtime_ns, action=action, decision=decision,
                    root=root_norm, reason=decision.reason,
                ),
                on_entry,
            )
            # NAME_ONLY 的目录把处置传给整棵子树；INDEX 的目录把**父级**的继承传下去
            # （自己这条规则不该替子项做主）。
            stack.append(
                (Path(path), norm, action if action is Action.NAME_ONLY else inherited)
            )
            return

        reason = decision.reason
        if action is Action.INDEX and stat.st_size > self.config.max_file_bytes:
            action = Action.NAME_ONLY
            reason = (
                f"超过 [scan] max_file_mb = {self.config.max_file_mb}"
                f"（{stat.st_size / 1024 / 1024:.1f} MiB）：只记名字与元数据"
            )
            stats.files_over_limit += 1
        if action is Action.INDEX:
            stats.files_index += 1
            stats.bytes_index += stat.st_size
        else:
            stats.files_name_only += 1
        self._send(
            ScanEntry(
                path=path, name=name, is_dir=False, size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, action=action, decision=decision,
                root=root_norm, reason=reason,
            ),
            on_entry,
        )

    # ------------------------------------------------------------------ 组装与送出

    def _entry(
        self,
        path: str,
        name: str,
        stat: os.stat_result,
        action: Action,
        reason: str,
        tag: int,
        is_dir: bool,
        rules: IgnoreRules,
        inherited: Action | None,
        root_norm: str,
    ) -> ScanEntry:
        """给「规则之外的原因」（reparse / data_dir / 嵌套根）造一条。

        这几条**没有规则命中**，但 `decision` 仍要如实给出规则怎么说——`--dry-run`
        数的是规则命中，把「被 reparse 跳过」的账算到某条规则头上是错的。
        """
        return ScanEntry(
            path=path,
            name=name,
            is_dir=is_dir,
            size=0 if is_dir else stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            action=action,
            decision=rules.decide(path, is_dir=is_dir, inherited=inherited),
            root=root_norm,
            reason=reason,
            reparse_tag=tag,
        )

    @staticmethod
    def _send(entry: ScanEntry, callback: EntryCallback | None) -> None:
        if callback is not None:
            callback(entry)

    def _inside_data_dir(self, norm: str) -> bool:
        return inside(norm, self._data_dir)
