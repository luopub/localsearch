r"""扫描结果的账目：谁被谁排掉了（P2 / `F2-06`）。测试落点 `tests/unit/test_cli_index_dryrun.py`。

本模块**只算数、不排版**：`lsearch index --dry-run` 用 rich 画表，`tools/scan_report.py`
打纯文本，两边读同一份 `Report`。排版各写一份不影响"数字只有一个来源"——真正不能
有两个来源的是**分桶规则**，那正是本模块存在的理由。

------------------------------------------------------------------------------
三栏，而不是两栏
------------------------------------------------------------------------------

`F2-06` 之前只有「规则」与「规则之外」两栏（`tools/scan_report.py` 的第一版），
`.lsearch-noindex` 剪掉的目录混在后一栏里，与 reparse point、`data_dir` 硬剔除、
嵌套根、`max_file_mb` 超限**共用一格**（`F2-05` 的遗留第 8 条）。混着有两个具体的坏处：

1. 那几类的**成因与对策完全不同**——reparse 要决定"云占位文件算不算搜得到"，
   `data_dir` 是刻意的自我排除，超限是阈值问题，一个合计数字分不开它们；
2. 标记是**用户亲手放在那个目录里的**，量级是"一个标记剪掉一整棵子树"。而它
   恰恰是用户在 `--dry-run` 里最想核对的一项（"我放的标记到底省了多少"），
   与"系统自己跳过的东西"混在一起就核对不了。

所以标记单独成栏。「规则之外」剩下的那些理由按**前 28 个字符**归并：错误消息里
带路径，原样分组会得到几百个各含 1 条的分组，等于没分组（这条是 `F2-01` 踩出来的）。

------------------------------------------------------------------------------
为什么"一条都没命中的规则"也要报
------------------------------------------------------------------------------

一份 284 条规则的入库文件里，有多少条其实**从未生效**（被后面的规则完全盖掉，
或者压根没匹配上任何东西）？只列"命中 > 0"的规则回答不了这个问题——**没命中的
那些恰恰是答案**。这是 `F2-03` 遗留第 1 条（`Thumbs.db` 被 `*.db` 盖掉，最终生效的是
后者）的推广：当时只发现了那一条，因为其他被盖掉的规则在报表上是**不出现**的。

------------------------------------------------------------------------------
两条与"可复现"有关的约定
------------------------------------------------------------------------------

- **同数并列时按 key 排序**，不靠字典的插入顺序。`F2-04` 的微基准因为抽样不确定，
  两次运行给出了不可比的两组数（PROGRESS §4 P2「偏差」第 9 条）。同样的坑在这里的
  表现是"同一次报表两遍跑出来行序不同"，看的人会以为是数据变了。
- **目录与文件分开计**，且**字节只算文件**。剪掉一个目录会带走整棵子树，与剪掉一个
  文件不是一个量级；目录的 `st_size` 在 NTFS 上是 0 或目录项大小，把它加进字节数会
  得到一个既不是 0 也不是真值的数。
"""

from __future__ import annotations

import collections
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from localsearch.ingest.base import (
    ScanEntry,
    Scanner,
    ScanStats,
    marker_reason,
)
from localsearch.ingest.filters import PROJECT_ROOT, Action, IgnoreRules, Rule

#: 三栏的名字。它们同时是 `tools/scan_report.py` 的 JSON 键的一部分，
#: 改字面量等于改那份报表的键——两处要一起改。
KIND_RULE = "规则"
KIND_MARKER = "目录标记"
KIND_OTHER = "规则之外"
KINDS = (KIND_RULE, KIND_MARKER, KIND_OTHER)

#: 「规则之外」的理由按前多少个字符归并（理由里带路径，见模块文档）。
#:
#: ⚠ **这是一条对理由生产者的约束**：会逐条变化的部分必须排在第 `OTHER_PREFIX` 个字符
#: **之后**（路径、文件大小、错误消息都在此列）。违反它不会报错，只会让同一条原因裂成
#: 很多行——`max_file_mb` 超限那条第一版把文件大小写进了前 28 个字符，一次真实全盘
#: dry-run 里裂成 8 行、每行 1~8 个文件（2026-10-15 实测，见 `docs/reports/F2-06-*`）。
#: 放宽这个数字治不了本：理由是给人读的消息，拿它当归并键本身就脆（`F2-06` 的遗留，
#: 正解是让扫描器给一个机器可读的成因，见 `PROGRESS.md` §5.2）。
OTHER_PREFIX = 28

#: 非规则的分组没有行号可写。用长破折号而不是 `-`：它要印在「归属」那一列里，
#: 与真的行号（`skip 第 2 行`）区分开。GBK（本机代码页 936）认得这个字符。
WHERE_NONE = "—"

#: `--include` / `--exclude` 追加的规则在「归属」那一列的位置。
#: **不能拼成 `skip 第 0 行`**：`line == 0` 是"这条不在文件里"的哨兵值
#: （`filters.Rule.origin` 就是按它特判的），印成"第 0 行"等于把人引去看一个
#: 不存在的行号，而用户真去 `sed -n 0p` 只会看到空。
WHERE_COMMAND_LINE = "命令行"

#: `(栏, 归属, 原文)`。元组而不是 dataclass：它要能直接当 `dict` 的键，
#: 也要能拼成 `tools/scan_report.py` 的 JSON 键（`栏|归属|原文`）。
Key = tuple[str, str, str]


# ==============================================================================
# 分桶
# ==============================================================================


def is_marker_reason(reason: str) -> bool:
    """`reason` 是不是"这个目录里有 `.lsearch-noindex`"。

    判据是**逐字相等**，不是 `in`、不是 `startswith`。`base.marker_reason()` 的文档
    写着两边必须给出一模一样的字符串，就是为这里留的；用包含判断会让任何**引用**了
    这句话的消息也被算成标记（错误消息、注释里的引文都长得一样）。
    """
    return reason == marker_reason()


def rule_key(rule: Rule) -> Key:
    """一条规则在报表里的分组键。

    `line` 是必须的：用户唯一能对上号的标识是"第 39 行 `^D:/localsearch-data`"。
    两条原文相同但行号不同的规则（入库文件里很常见）必须是两行，否则"哪一行在生效"
    这个问题会被合计掉。

    `line == 0` 是 `--include` / `--exclude` 追加进来的临时规则（`filters.with_extra`），
    它们不在文件里，所以归属栏写「命令行」而不是"第 0 行"。
    """
    if rule.line == 0:
        return (KIND_RULE, WHERE_COMMAND_LINE, rule.raw)
    return (KIND_RULE, f"{rule.section} 第 {rule.line} 行", rule.raw)


def bucket(entry: ScanEntry) -> Key:
    """这条该记在谁的账上。

    「有规则命中」的判据是 `reason` 与 `decision.reason` **相同**：reparse point、
    `data_dir` 硬剔除、嵌套根、`max_file_mb` 超限、目录级标记都会改写 `reason`，但它们的
    `decision` 仍然是规则怎么说。把后者算到规则头上，会让报表里出现"规则 X 排掉了 3 万条"
    这种与规则无关的数字——这条判据从 `F2-01` 的 `tools/scan_report.py` 起就在用，
    搬到这里是为了让 CLI 与那个脚本**不可能**给出两种分桶结果。
    """
    rule = entry.decision.rule
    if rule is not None and entry.reason == entry.decision.reason:
        return rule_key(rule)
    if is_marker_reason(entry.reason):
        return (KIND_MARKER, WHERE_NONE, entry.reason)
    return (KIND_OTHER, WHERE_NONE, entry.reason[:OTHER_PREFIX])


# ==============================================================================
# 计数
# ==============================================================================


@dataclass
class Hits:
    """一个分组的命中数。目录、文件、字节三者分开，理由见模块文档。"""

    dirs: int = 0
    files: int = 0
    #: **只累计文件**的 `st_size`
    bytes: int = 0
    #: 第一条命中的路径。给报表"这条规则到底命中了什么"一个具体的样子；
    #: 全量列出等于把报表做成第二个 `files` 表。
    example: str = ""

    @property
    def total(self) -> int:
        return self.dirs + self.files

    def add(self, entry: ScanEntry) -> None:
        if entry.is_dir:
            self.dirs += 1
        else:
            self.files += 1
            # `max(-1, 0)`：读不到属性的那条 size 是 -1（`scanner_scandir` 用它表示
            # "连多大都不知道"），把它加进字节数会把合计做小，且小得看不出来。
            self.bytes += max(entry.size, 0)
        if not self.example:
            self.example = entry.path

    def merge(self, other: Hits) -> None:
        self.dirs += other.dirs
        self.files += other.files
        self.bytes += other.bytes
        if not self.example:
            self.example = other.example


#: 两张表：被排掉的、只记名字的。用字符串而不是常量对象，`rows()` 要按名字取表。
TABLE_SKIPPED = "skipped"
TABLE_NAME_ONLY = "name_only"


class Distribution:
    """按 `(栏, 归属, 原文)` 分组的两张账。

    `rules` 是**整份规则文件**（不是只有命中的那些）：`rules_with_no_hits()` 要拿它
    减去命中的集合，"一条都没生效的规则有几条"这个问题只有拿着全集才回答得了。
    """

    def __init__(self, rules: Iterable[Rule] = ()) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.skipped: dict[Key, Hits] = collections.defaultdict(Hits)
        self.name_only: dict[Key, Hits] = collections.defaultdict(Hits)

    # ---- 收 ----------------------------------------------------------------

    def record_skip(self, entry: ScanEntry) -> None:
        """`on_skip` 收到的那条：被排除或跳过的。"""
        self.skipped[bucket(entry)].add(entry)

    def record_entry(self, entry: ScanEntry) -> None:
        """`on_entry` 收到的那条：**只记「留着但没提正文」的**。

        把正常索引的文件也记进来，只会把这张表做长一千倍，而它要回答的问题是
        "哪些规则把文件挡在正文之外"（`.ts`、`*.jpg` 这类）——`NAME_ONLY` 就是答案。
        """
        if entry.action is Action.NAME_ONLY:
            self.name_only[bucket(entry)].add(entry)

    # ---- 出 ----------------------------------------------------------------

    def table(self, which: str) -> dict[Key, Hits]:
        if which not in (TABLE_SKIPPED, TABLE_NAME_ONLY):
            raise ValueError(f"只认 {TABLE_SKIPPED} / {TABLE_NAME_ONLY}，实际是 {which!r}")
        return getattr(self, which)

    def rows(self, which: str = TABLE_SKIPPED) -> list[tuple[Key, Hits]]:
        """命中数降序；**并列时按 key 升序**（见模块文档：不靠字典顺序）。"""
        return sorted(self.table(which).items(), key=lambda kv: (-kv[1].total, kv[0]))

    def totals(self, which: str = TABLE_SKIPPED) -> Hits:
        out = Hits()
        for hits in self.table(which).values():
            out.merge(hits)
        return out

    def kind_totals(self, which: str = TABLE_SKIPPED) -> dict[str, Hits]:
        """按栏汇总。三栏各自的量级是这张报表最该先被看到的东西。"""
        out: dict[str, Hits] = {kind: Hits() for kind in KINDS}
        for (kind, _where, _raw), hits in self.table(which).items():
            out.setdefault(kind, Hits()).merge(hits)
        return out

    def rules_with_no_hits(self) -> list[Rule]:
        """一条都没管到的规则（两张表都没出现）。

        两种成因**分不开**，这一点必须写在报表上：**被后面的规则完全盖掉**
        （`F2-03` 的 `Thumbs.db`）与**从来就没匹配上任何东西**（`*.ts` 之外那些）。
        想分清要逐条重放候选集，是另一件事（`F2-03` 遗留第 1 条）。
        """
        seen = set(self.skipped) | set(self.name_only)
        return [rule for rule in self.rules if rule_key(rule) not in seen]


# ==============================================================================
# 跑一轮
# ==============================================================================


@dataclass
class Report:
    stats: ScanStats
    dist: Distribution
    #: 扫描本身花掉的时间（含两个回调，不含后面的打印）
    seconds: float


def collect(scanner: Scanner, rules: IgnoreRules) -> Report:
    """跑一轮扫描并把账记好。

    **扫描与记账绑在一起**，不留给调用方自己接两个回调：`on_skip` 是可选的
    （`Scanner.scan` 的签名里 `on_skip=None` 是合法的），忘了传的症状是"被排除的
    一条都看不见"——一个**空报表**，而不是一个错。CLI 与 `tools/scan_report.py`
    都从这里拿结果，两个入口就不可能出现"一个有 on_skip、一个没有"。
    """
    dist = Distribution(rules.rules)
    t0 = time.perf_counter()
    stats = scanner.scan(
        rules=rules,
        on_entry=dist.record_entry,
        on_skip=dist.record_skip,
    )
    return Report(stats=stats, dist=dist, seconds=time.perf_counter() - t0)


def resolve_ignore_file(configured: str | Path) -> Path:
    """`[scan] ignore_file` 的配置值 → 真正要读的那个文件。

    相对名字按**仓库根**解释：`.lsearchignore` 跟着代码走、不进数据目录（计划 §4.5.1），
    用户在哪个工作目录敲命令都得读到同一份。绝对路径原样用——测试靠它把规则文件
    放进 `tmp_path`，不想为了换一份规则去动仓库。

    这是 `PROJECT_ROOT` 的唯一消费者之一（另一个是 `filters.py` 自己），
    放在这里而不是 `filters.py`：那个模块是 `F2-03` 的被测模块，往它里面加东西
    会连带把它已经通过的绿灯打回复测，而这件事与规则解析无关。
    """
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path
