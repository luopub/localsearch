r"""扫描后端的公共形态（P2 / `F2-01`）。测试落点 `tests/unit/test_scanner_scandir.py`。

------------------------------------------------------------------------------
为什么要有这一层
------------------------------------------------------------------------------

`D11` 要的是**可替换扫描后端**：`os.scandir` 默认，MFT 可选加速。可替换的前提是
两边产出**同一种东西**——否则 `F2-02` 那句"核对 MFT 与 scandir 结果一致"根本没有
可比的接口。所以扫描结果必须是一种不依赖 `os.scandir` 的数据形态，`MftScanner`
往同一个 `on_entry` 里塞同样的 `ScanEntry`。

---- 流式，不攒列表 -----------------------------------------------------------

C: 盘实测 **347 万条目**（`PROGRESS.md` §3 的 E11）。攒成一个列表再返回，等于
要求 300 多万个对象同时活着——峰值内存与全盘规模成正比，而这正是后端要避免的。
所以形态是**回调**：`on_entry(entry)` 每判定一条就调一次，调用方自己决定写库、
攒样本还是只计数。

**回调里抛异常不捕获**：那是调用方的 bug（比如写库失败），扫描器假装看不见只会
让它更难查。**IO 层的错**（权限、文件在扫描途中被删、reparse 环）反过来一律不抛，
计数 + 留样本后继续——`A1` 要的是"全量索引无未捕获异常"，单条路径的问题不能中断
整轮。

---- 两件事只在这里判 ---------------------------------------------------------

1. **`data_dir` 硬性剔除**（计划 §4.5.1 的第 2 重机制）。索引器在往数据目录里持续
   写文件，靠规则文件匹配去排除它**存在竞态**：刚遍历过的目录里又冒出新文件。所以
   这一条不走规则、走前缀判断，写在扫描器里。
2. **`max_file_mb` 上限**（计划 §8 的 e 条）。判的不是"要不要"，而是"正文提不提"——
   超限文件仍然**只记名字**（`NAME_ONLY`），因为在"文件名叫什么"这件事上，64 MiB
   的日志和 1 KiB 的笔记没有区别。

---- 不猜"这卷是不是本地盘" ---------------------------------------------------

`E10` 实测：本机 **E: 报 `DRIVE_FIXED(3)`**（`GetDriveType` 与 C:/D: 读数完全一样），
而用户明确说它不是本地盘。所以"根目录从哪来"只有一个可靠来源：**配置里显式写的
`paths.roots`**。`all_drives = true`（枚举所有固定盘）因此**在 v1 里直接报错**而不是
猜着跑——把会掉线的卷当固定盘建全量索引，会得到一份永远追不平、且随时可能被误判
成"文件都删了"的索引（`R14` 的 `--prune` 同理）。总线类型（`BusType`）是本机唯一
测过的替代判据，但它**没有在非本地卷上验证过**（见 `PROGRESS.md` §3 的 E13）。

---- 目录级标记 `.lsearch-noindex`（`F2-05`） ----------------------------------

判定落在本模块的目录循环里，**在 `rules.decide()` 之前**，两份逻辑都在这里：
`has_noindex_marker()` 问文件系统，`marker_reason()` 给跳过的那条一个不参与规则
归因的理由。语义（为什么不可被 `!` 救回、为什么必须在**下潜之前**判）写在
`filters.py` 的第九条，本模块只负责把它接到 `scandir` 的顺序上。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from localsearch.ingest.filters import (
    NOINDEX_MARKER,
    Action,
    Decision,
    IgnoreRules,
    normalize,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不需要 core
    from localsearch.core.config import Config

#: `FILE_ATTRIBUTE_REPARSE_POINT`。P0 的 `R4` 实测用的就是这个属性位
#: （`tools/probe_mft.py` 的 scandir 基线），MFT 侧口径一致。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

#: 错误样本最多留这么多条，其余只计数。全盘扫描能刷出成百上千条同类错误，全留着
#: 会把内存和报表一起淹掉；但**计数必须是准的**——所以样本与总数分开存。
MAX_SAMPLES = 20


class ScanError(Exception):
    """扫描**开不起来**时的错误（根目录为空、后端没落地、规则的 roots 对不上）。

    单条路径的问题不走这里——那些进 `ScanStats`，不中断整轮。
    """


# ==============================================================================
# 扫描到的一条
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """盘上的一条，以及它被判成了什么。

    `path` 与 `key` 是两个不同的东西，**不要混用**：

    - `path` 是给人看的实际路径（正斜杠、**保留大小写**），入库后显示、报错、打开
      文件都用它；
    - `key` 是规范形（小写、正斜杠、无尾斜杠），**排除规则的匹配与"是不是同一个
      文件"的判定用**它。NTFS 大小写不敏感，用带大小写的路径当唯一键，会在
      `C:/Windows` 与 `c:/windows` 上得到两行。
    """

    path: str
    name: str
    is_dir: bool
    size: int
    mtime_ns: int
    action: Action
    decision: Decision
    root: str
    reason: str
    error: str | None = None
    reparse_tag: int = 0
    #: NTFS 的文件引用号（FRN），**不知道就是 0**。
    #:
    #: `F2-02` 起两个后端填的值不一样，这是刻意的：MFT 后端从枚举里**白拿**
    #: （`FSCTL_ENUM_USN_DATA` 每条记录都带），而 `scandir` 后端要拿它就得对每条
    #: 条目 `os.stat` 一次（实测 36 µs/条，C: 上 347 万条 = 125 s），所以那边一律
    #: 留 0，**不要**把它读成"这个文件没有 FRN"。
    #:
    #: 两个用处：`F2-02` 的核对按它折叠硬链接（`ingest/parity.py`），`F6-01` 的
    #: USN 增量要靠它把日志里的变化对回库里的行。
    frn: int = 0

    @property
    def key(self) -> str:
        return normalize(self.path)


# ==============================================================================
# 一轮扫描的账目
# ==============================================================================


@dataclass
class MftStats:
    """MFT 后端（`F2-02`）独有的账。`scandir` 后端留 `None`。

    **为什么单独一块，而不是散进 `ScanStats` 那几栏**：这些数的**读法完全不同**。
    `records` 是整个卷的记录数，含扫描范围之外的一大片；把它与 `entries_seen`
    相比会立刻得出"读错了"的结论，而它其实是对的。分开放，名字就能说明白。

    `outside_roots` / `system` / `hidden` 三项回答的是同一个问题——"MFT 看得见、
    但 `scandir` 从来不会看到"的那部分去哪了。**`scandir` 没有对应的数**，因为
    它根本看不见它们。
    """

    #: 枚举到的记录总数（整卷，含不在扫描根下的）
    records: int = 0
    #: 不在任何扫描根下
    outside_roots: int = 0
    #: 落在 NTFS 保留记录（记录号 0–15 的 `$Extend` 等）的子树里——Win32 的目录
    #: 枚举从来不返回它们，所以 `scandir` 那边也不存在
    system: int = 0
    #: 在扫描根下，但祖先目录被剪枝（规则 / 标记 / 嵌套根），因而 `scandir` 从没
    #: 走到过。**不是错误**，只是"看见了但没被算数"
    hidden: int = 0
    #: 父记录的序号对不上（父槽位已被别的文件复用）的条数。正常卷上应当是 0——
    #: 不是 0 就说明"只比记录号"会认错文件
    broken_links: int = 0
    #: 补 `size`/`mtime` 打了几次 `GetFileAttributesExW`（`ntfs.py` 里那笔单价账）
    attributes_calls: int = 0
    bad_records: int = 0
    other_versions: int = 0
    volumes: int = 0
    enumerate_seconds: float = 0.0


@dataclass
class ScanStats:
    """一轮扫描的计数。

    **每一项都是"见到的"还是"留下的"要分清**：`files_seen` 是见到的全部（含被跳过的），
    `files_index` 是真正要提正文的。混起来会让"排除规则到底排掉了多少"变得无法回答，
    而那正是 `F2-06` 的 `--dry-run` 要给人看的。

    目录单独计（`dirs_*`）：一个被剪掉的目录会带走**整棵子树**，它的代价与剪掉一个
    文件不是一个量级——`--dry-run` 的报告里这两栏必须分开，否则"排掉 3 条"和
    "排掉 1 个含 30 万文件的目录"长得一样。

    ⚠ **`dirs_entered` 比 `dirs_seen` 多**，差额是**扫描根本身**：根是用户声明的范围，
    不是"盘上被发现的东西"，所以它不产生 `ScanEntry`；但它的确被 `os.scandir` 打开过。
    D:/work 那次实测：`dirs_seen` 47,754、`dirs_entered` 46,167、`dirs_pruned` 1,588——
    46,167 = (47,754 - 1,588) + 1。**不要把这个 +1 当 bug 顺手抹掉。**

    ⚠ **MFT 后端（`F2-02`）永远是 0，这是对的，不是漏了**：它从不打开目录（枚举的是
    卷上的记录，不递归目录），所以"进过几个目录"这件事在那一侧根本不存在。要去比
    "MFT 是不是少走了 4.7 万个目录"之前先看这一行。同理，MFT 侧的 `dirs_seen` 与
    `scandir` 侧同义，`dirs_entered` 不同义。

    （这里的减号是 ASCII 的：U+2212 在 GBK 控制台上编不出来，`print` 出来会抛
    `UnicodeEncodeError`。本行只是文档，但它旁边就是会被打印的那些行——顺手保持
    一致，省得有人抄走。）
    """

    roots_total: int = 0
    roots_scanned: int = 0
    #: **没被扫的根**：不可达（盘没插、路径不存在），或被目录级标记排除。两种都不
    #: 报错——它们都是运行时事实，不是配置错误；但都必须出现在报表里，否则"根写了
    #: 却没索引到东西"会没有任何痕迹。
    roots_unreachable: list[tuple[str, str]] = field(default_factory=list)

    dirs_seen: int = 0
    dirs_entered: int = 0
    dirs_pruned: int = 0
    dirs_reparse: int = 0

    files_seen: int = 0
    files_index: int = 0
    files_name_only: int = 0
    files_skipped: int = 0
    files_over_limit: int = 0

    bytes_index: int = 0
    errors_total: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    #: reparse tag → 条数。为什么按 tag 分而不是只给一个总数：`0xA0000003`（挂载点）、
    #: `0x9000xxx`（云占位文件）与 `0x8000001A`（WSL）的**成因与对策完全不同**，
    #: 一个总数只能让人知道"有东西被跳过了"，分不开就得回头再跑一遍。
    reparse_tags: dict[int, int] = field(default_factory=dict)

    seconds: float = 0.0

    #: MFT 后端才有（`F2-02`）——见 `MftStats`。`scandir` 后端留 `None`，
    #: 报表据此决定要不要印那几行。
    mft: MftStats | None = None

    def add_error(self, path: str, why: str) -> None:
        self.errors_total += 1
        if len(self.errors) < MAX_SAMPLES:
            self.errors.append((path, why))

    def add_reparse(self, tag: int) -> None:
        self.reparse_tags[tag] = self.reparse_tags.get(tag, 0) + 1

    @property
    def entries_seen(self) -> int:
        return self.dirs_seen + self.files_seen

    @property
    def entries_kept(self) -> int:
        """要进索引的条数（正文 + 仅名字）。"""
        return self.dirs_kept + self.files_index + self.files_name_only

    @property
    def dirs_kept(self) -> int:
        return self.dirs_seen - self.dirs_pruned - self.dirs_reparse


# ==============================================================================
# 扫描器的输入
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """扫描真正需要的那几项，从 `core.config.Config` 摘出来。

    摘出来而不是直接吃 `Config`：扫描器要能在**不碰 TOML、不碰配置文件**的前提下
    建起来，否则 `test_scanner_scandir.py` 里每一条断言都得先铺一份配置目录。真正的
    接线只有 `from_config` 一处。

    `max_file_mb = 0` 的语义是**字面的**：所有非空文件都只记名字。想"不设上限"就把
    它写大，别写 0——这类"0 表示无限"的默契是静默错误的温床。
    """

    roots: tuple[str, ...] = ()
    data_dir: str = ""
    max_file_mb: int = 64
    backend: str = "auto"
    follow_symlinks: bool = False
    all_drives: bool = False

    @classmethod
    def from_config(cls, config: "Config") -> "ScanConfig":
        return cls(
            roots=tuple(config.paths.roots),
            data_dir=config.paths.data_dir,
            max_file_mb=config.scan.max_file_mb,
            backend=config.scan.backend,
            follow_symlinks=config.scan.follow_symlinks,
            all_drives=config.paths.all_drives,
        )

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


EntryCallback = Callable[[ScanEntry], None]


@runtime_checkable
class Scanner(Protocol):
    """可替换后端的接口（`D11`）。

    `on_entry` 收**留着**的条目（`INDEX` / `NAME_ONLY`），`on_skip` 收**被排除或跳过**
    的条目（规则判 `SKIP`、reparse point、读不到属性）。分成两个回调是刻意的：
    索引只需要前者，而 `--dry-run` 要数"每条规则排掉了多少"——只有后者能回答。
    合成一个回调再让调用方自己按 `action` 过滤，会得到"忘了过滤就把排除掉的文件
    也索引了"这种安静而昂贵的错。
    """

    name: str

    def scan(
        self,
        *,
        rules: IgnoreRules,
        on_entry: EntryCallback,
        on_skip: EntryCallback | None = None,
    ) -> ScanStats: ...


def make_scanner(config: ScanConfig) -> Scanner:
    """按 `[scan] backend` 造后端。

    两条约定（`D11` / `R15`）：

    - **`auto` = 先探测、失败静默回退 `scandir`**。回退**不报错**：默认配置在没有
      管理员权限的机器上必须能跑（计划 §4.8 的 `LeastPrivilege`），"没提权"是常态
      而不是故障。为什么回退原因不能让 `auto` 自己咽下去：`doctor` 要把"当前实际
      走的是哪条路"印出来，否则"为什么这次慢了"就没有答案——所以原因由
      `scanner_mft.probe()` 单独给出，`doctor` 调它。
    - **显式的 `mft` 用不了就报错**，不偷偷换成 `scandir`：用户点名了后端，静默换掉
      等于让他以为"MFT 跑过了"。这一条与 `index.py` 里 `--dry-run` 的态度一致
      （"偷偷忽略比报错坏得多"）。
    """
    if config.all_drives:
        raise ScanError(
            "all_drives = true 在 v1 里不支持：本机实测 GetDriveType 会把非本地的卷"
            "（E:）报成固定盘（PROGRESS §3 的 E10），枚举「所有固定盘」就会把会掉线的盘"
            "当成一直在的盘建全量索引。请在 [paths] roots 里显式写明要索引的目录。"
        )
    if config.backend not in ("auto", "scandir", "mft"):
        raise ScanError(f"[scan] backend 只认 auto / scandir / mft，实际是 {config.backend!r}")

    if config.backend in ("auto", "mft"):
        from localsearch.ingest.scanner_mft import MftScanner, probe

        result = probe(config)
        if result.ok:
            return MftScanner(config)
        if config.backend == "mft":
            raise ScanError(
                f"backend = 「mft」在本机用不了：{result.reason}。"
                "MFT 需要管理员权限、且只对本地 NTFS 卷有效；改用 「scandir」 或 "
                "「auto」（auto 会自动回退，不会报错），或到管理员终端里重跑。"
            )
        # auto：静默回退。原因不丢——`probe()` 能再给一次，`doctor` 就是那么取的。

    from localsearch.ingest.scanner_scandir import ScandirScanner

    return ScandirScanner(config)


# ==============================================================================
# 路径上的公共判断
# ==============================================================================


def inside(norm: str, prefix: str) -> bool:
    """`norm` 是不是 `prefix` 本身或它的后代（两者都已是规范形）。

    在路径边界收口：`c:/dir2` **不是** `c:/dir` 的后代。少了这一条，把 `data_dir`
    配成 `D:/data` 就会顺手剔掉 `D:/database`——静默少索引，且没人会发现。
    """
    if not prefix or not norm:
        return False
    return norm == prefix or norm.startswith(prefix + "/")


def check_rules_roots(rules: IgnoreRules, roots: tuple[str, ...]) -> None:
    """核对规则的 `roots` 与本次要扫的 `roots` 一致。

    **为什么值得为它抛异常**：含 `/rel/path` 的相对路径规则必须靠 roots 才能解释，
    两者不一致时它们**不会报错，只是永远不命中**——"规则写了但没生效"最典型的形态，
    与 `filters.py` 里 `_literal_ends` 取错后缀是同一个失败模式。规则集与扫描范围
    本来都来自同一份配置，这里对不上就是接线错了。
    """
    rule_roots = {normalize(r) for r in rules.roots}
    scan_roots = {normalize(r) for r in roots}
    if rule_roots != scan_roots:
        raise ScanError(
            "排除规则的 roots 与本次扫描的 roots 不一致："
            f"规则 {sorted(rule_roots) or '（空）'} / 扫描 {sorted(scan_roots) or '（空）'}。"
            "含 `/rel` 的相对路径规则要靠 roots 才能解释，不一致时它们会静默不命中，"
            "所以这里直接报错——用同一个 roots 构造 IgnoreRules 与 ScanConfig。"
        )


def drive_of(norm: str) -> str:
    """规范形路径的卷（`c:` / `d:`）；不是盘符路径时返回空串。

    MFT 后端（`F2-02`）按卷枚举，`--prune`（`F6-03`）按卷判断"本次哪些卷真的可达"，
    两处都要这个。**卷根本身**（`"c:"`）与它下面的路径回报同一个值——这一点比
    "只认带路径的写法"重要：`roots` 里最常出现的恰恰是 `D:/work` 这种，而 `all_drives`
    之类的将来形态会拿盘符根来问。UNC（`//server/share`）不是卷，返回空串。
    """
    head, sep, _ = norm.partition(":")
    if not sep or len(head) != 1 or not head.isalpha():
        return ""
    return head + ":"


def reparse_tag_of(stat_result: os.stat_result) -> int:
    """`os.stat_result` 上的 reparse tag；非 Windows / 不是 reparse 时返回 0。

    `st_reparse_tag` 在 3.8+ 的 Windows 上才有，所以用 `getattr` 兜一层——
    本模块的三条测试有两条要在非 reparse 的普通文件上跑。
    """
    if not (getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        return 0
    return int(getattr(stat_result, "st_reparse_tag", 0) or 0)


def path_of(entry: os.DirEntry[str]) -> str:
    """`DirEntry` → 保留大小写的正斜杠路径（给 `ScanEntry.path` 用）。"""
    return str(entry.path).replace("\\", "/")


def has_noindex_marker(directory: str | os.PathLike[str]) -> bool:
    """目录里有没有 `.lsearch-noindex`（语义见 `filters.py` 第九条）。

    用 `os.path.lexists` 而**不是** `exists`：标记是个空文件，但用户完全可能把它
    做成指向别处的链接（甚至一个断链）。`exists` 在那种情况下返回 False，于是标记
    **静默失效**——而"我明明放了标记却没生效"是最难查的一类，因为它连个错误都没有。
    这里要问的是"那个名字在不在"，不是"它指向的东西在不在"。

    成本是每个目录一次 `lexists`（一次系统调用；具体走哪个 Win32 API 没核实）。
    **这个成本是量过的，不是估的**（2026-10-15 01:34:31）：在真实目录集上逐个探一次，
    3,000 次 **15.71 µs/次**（同一批热跑 30,000 次 **14.57 µs/次**）；另一组在
    `C:/Windows/System32` 的 182 个目录上重复 100 遍是 **20.22 µs/次**——同一个量级，
    与目录大小无关。按这个单价外推：**D:/work 的 47,754 个目录 ≈ 0.75–0.97 s；
    C: 盘的 655,515 个 ≈ 10–13 s**（前者占那轮 23–40 s 的 2–4%）。

    所以它**不是免费的**，但也没为它加开关：想省掉这个 syscall，只能"先把目录列一遍、
    看里面有没有标记、再决定要不要它的子项"——那正是第九条 b 明确不要的顺序依赖，
    代价（结果随枚举顺序而变）比这点时间大得多。将来真嫌它慢，能改的是记账方式
    （比如把"标记目录"整批延后到下一轮），不是把它挪到下潜之后。
    """
    return os.path.lexists(os.path.join(os.fspath(directory), NOINDEX_MARKER))


def marker_reason() -> str:
    """被标记剪掉的那种条目，`reason` 里写的那句话。

    写成函数而不是散在扫描器里的字面量：`F2-06` 的 `--dry-run` 要把这类归到
    「规则之外」那一栏（它不来自任何一行规则，硬塞进某条规则会让报表说谎），
    两边必须给出一模一样的字符串。扫描根也用同一句——根本来就是一个目录。
    """
    return f"目录里有 {NOINDEX_MARKER}（目录级标记，整棵子树不索引）"


