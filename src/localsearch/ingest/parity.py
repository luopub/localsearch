r"""两个扫描后端的核对：**按 FRN 折叠硬链接后的集合相等**（P2 / `F2-02`）。
测试落点 `tests/unit/test_scanner_parity.py`，真机入口 `tools/verify_mft_parity.py`。

------------------------------------------------------------------------------
判据为什么不是"数字相等"
------------------------------------------------------------------------------

`F0-04` 实测（2026-10-14，C: 同一块盘）：

| | 文件 | 目录 |
|---|---|---|
| MFT（记录） | 2,055,857 | 673,288 |
| scandir（目录项） | 2,814,788 | 655,515 |
| 差 | **+758,931** | **−17,773** |

**MFT 数的是记录**（一个文件一条，不管它有几个名字），**scandir 数的是目录项**
（一个名字一条）。NTFS 的硬链接让 `kernel32.dll` 有两个名字、`notepad.exe` 有三个，
多出来的那 758,931 条全在 scandir 那一侧。目录方向相反（scandir 少 17,773），
与"跳过 422 个 reparse point 后其子树没有被下潜"一致。

所以计划原本写的「与 scandir 的**逐条一致性**」是个**永远满足不了的判据**——
照着做只有两条路：静默放宽，或者把正常实现当成 bug。`F0-04` 把判据改成了
**按 FRN 折叠硬链接后的集合相等**，本模块就是它的实现。

------------------------------------------------------------------------------
怎么折叠：**不用给每一侧都打 stat**
------------------------------------------------------------------------------

朴素做法是两边都把每条条目的 FRN 问出来（`os.stat` 36 µs/条，C: 上 3.47M 条
= 125 s），再比两个集合。**不需要**：

- MFT 那一侧的 FRN 是枚举白送的（`ScanEntry.frn`），集合直接就有；
- scandir 那一侧只需给**键对不上 MFT 的那些**问 FRN——它们要么是硬链接的额外
  名字（FRN 落在 MFT 的集合里），要么是真正的差异。C: 上这一批是 758,931 条
  （27 s），而不是 347 万条。

键对得上的那些**根本不用问**：路径相同就意味着是同一个目录项，折叠与否都相等。

------------------------------------------------------------------------------
`mft_only` 才是要命的那个方向
------------------------------------------------------------------------------

- `scandir_only` 通常是一大片**预期之内**的硬链接——它不构成失败，只说明"折叠"
  这件事确实发生了；
- **`mft_only` 必须为空**：MFT 报出一条 scandir 看不见的路径，只有两种可能——
  它把不该发的东西发出去了（保留记录、被剪枝的子树），或者 scandir 少走了。
  两种都是**要查的**，所以它单独计数、单独留样本。

⚠ 本模块只做判定，**不做 I/O**。谁去跑那两个后端、谁去 `os.stat` 补 FRN，由调用方
注入（`frn_of`）——单测于是不需要提权、也不需要真卷。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from localsearch.ingest.base import Action, ScanEntry

#: 每一类差异最多留几条样本。全留会让报告变成第二个文件列表，而且真出事的时候
#: 几百万条样本里也没人看第二条——**计数必须是准的**，样本只是"长什么样"。
MAX_SAMPLES = 20


@dataclass
class Parity:
    """一次核对的结果。字段名里的 `_total` 与留样本的列表是两件事。"""

    mft_entries: int = 0
    scandir_entries: int = 0
    matched: int = 0

    #: MFT 报了、scandir 没有的路径。**应当为空**（见模块文档）。
    mft_only: list[str] = field(default_factory=list)
    mft_only_total: int = 0

    #: scandir 报了、MFT 没有的路径：预期全部是硬链接的额外名字。
    scandir_only_total: int = 0
    hard_link_total: int = 0
    unexplained: list[str] = field(default_factory=list)
    unexplained_total: int = 0

    #: 同一个路径在两边的处置不一致（规则 / 剪枝 / `max_file_mb` 的分叉）。
    action_mismatch: list[tuple[str, str, str]] = field(default_factory=list)
    action_mismatch_total: int = 0
    is_dir_mismatch: list[tuple[str, bool, bool]] = field(default_factory=list)
    is_dir_mismatch_total: int = 0

    #: 为了折叠硬链接问了几次 FRN（调用方自己实现 `frn_of` 时就知道代价了）
    frn_calls: int = 0

    @property
    def ok(self) -> bool:
        """**判据**：这四个都必须干净。硬链接那一片（`hard_link_total`）不算差异。"""
        return not (self.mft_only_total or self.unexplained_total
                    or self.action_mismatch_total or self.is_dir_mismatch_total)

    def summary(self) -> str:
        return (f"MFT {self.mft_entries:,} 条 / scandir {self.scandir_entries:,} 条，"
                f"路径相同的 {self.matched:,} 条；"
                f"仅有 MFT {self.mft_only_total:,}、仅有 scandir {self.scandir_only_total:,}"
                f"（其中按 FRN 判为硬链接 {self.hard_link_total:,}、无法解释 {self.unexplained_total:,}）；"
                f"处置不一致 {self.action_mismatch_total:,}、文件/目录判断不一致 "
                f"{self.is_dir_mismatch_total:,}")


class ParityCheck:
    """边喂边核。**两侧都流式喂进来**，只有 MFT 那一侧按路径建一份索引。

    为什么不把两边各攒成一个列表再比：C: 上两侧加起来 486 万条，每条一个 `str`
    就已经是几百 MB，再乘两份集合就是 GB 级——而这件事只需要"路径 → 是第几条"这
    一张表再加一个 `bytearray` 的"见过"标记（486 万条 = 4.6 MB）。

    用法：先 `add_mft()` 喂完 MFT 那一轮（它的记录是整卷的，必须先建立索引），
    再 `add_scandir()` 喂 scandir 那一轮，最后 `result()`。
    """

    def __init__(self) -> None:
        self._parity = Parity()
        #: 规范路径 → MFT 那一侧在 `_mft` 里的下标
        self._at: dict[str, int] = {}
        self._mft: list[ScanEntry] = []
        #: 见过的 MFT 下标。用 `bytearray` 而不是 `set`：下标是稠密的。
        self._seen: bytearray = bytearray()
        #: MFT 报出来的全部 FRN（枚举白送）
        self._frns: set[int] = set()

    # ---------------------------------------------------------------- 收 MFT

    def add_mft(self, entry: ScanEntry) -> None:
        key = entry.key
        idx = self._at.get(key)
        if idx is None:
            self._at[key] = len(self._mft)
            self._mft.append(entry)
            self._seen.append(0)
        else:
            # 同一个规范路径在 MFT 那一侧出现两次：**折叠没做干净**。
            # 记进 is_dir 不一致那一栏会看不出所以然，所以借 `mft_only` 的话说清楚。
            self._parity.mft_only_total += 1
            if len(self._parity.mft_only) < MAX_SAMPLES:
                self._parity.mft_only.append(key)
        self._parity.mft_entries += 1
        if entry.frn:
            self._frns.add(entry.frn)

    # ------------------------------------------------------------ 收 scandir

    def add_scandir(self, entry: ScanEntry,
                    frn_of: Callable[[str], int] | None = None) -> None:
        """喂一条 scandir 的条目。

        `frn_of(entry.path)` **只在键对不上 MFT 时才被调用**（见模块文档）——
        调用方的实现代价因此与"差异有多少"成正比，而不是与"条目有多少"。
        """
        self._parity.scandir_entries += 1
        key = entry.key
        idx = self._at.get(key)
        if idx is None:
            self._parity.scandir_only_total += 1
            frn = 0
            if frn_of is not None:
                frn = frn_of(entry.path)
                self._parity.frn_calls += 1
            elif entry.frn:
                frn = entry.frn
            if frn and frn in self._frns:
                self._parity.hard_link_total += 1
            else:
                self._parity.unexplained_total += 1
                if len(self._parity.unexplained) < MAX_SAMPLES:
                    self._parity.unexplained.append(entry.path)
            return

        self._parity.matched += 1
        self._seen[idx] = 1
        other = self._mft[idx]
        if other.is_dir != entry.is_dir:
            self._parity.is_dir_mismatch_total += 1
            if len(self._parity.is_dir_mismatch) < MAX_SAMPLES:
                self._parity.is_dir_mismatch.append((entry.path, other.is_dir, entry.is_dir))
        if other.action is not entry.action:
            self._parity.action_mismatch_total += 1
            if len(self._parity.action_mismatch) < MAX_SAMPLES:
                self._parity.action_mismatch.append(
                    (entry.path, other.action.value, entry.action.value)
                )

    # ----------------------------------------------------------------- 结果

    def result(self) -> Parity:
        """收尾：MFT 那侧**从没被 scandir 见过**的那些，就是 `mft_only`。"""
        for idx, entry in enumerate(self._mft):
            if not self._seen[idx]:
                self._parity.mft_only_total += 1
                if len(self._parity.mft_only) < MAX_SAMPLES:
                    self._parity.mft_only.append(entry.path)
        return self._parity
