r"""分级就绪（`D10`）：每一级做到了什么程度、下一趟该挑哪些文件。测试落点
`tests/unit/test_tier_state.py`。

计划 §4.3 那棵树里**没有这个文件**（它把"分级就绪"一格放在 `search/tier.py`），落地时
分成两半：**本模块管状态**（`files.tier` / `files.status` 的词汇表、选择条件、就绪台账），
`search/tier.py` 管**标注**（拿了台账与用户要的模式，决定能不能做、话要怎么说）。
分开的理由：台账要读库、标注是**纯函数**，而纯的那一半不该为了被测试而先建一个库。
（`F4-01` 把 `index/tokens.py` 加进树里是同一类回改，见 `PROGRESS.md` §4 P4「偏差」。）

------------------------------------------------------------------------------
三级是什么，`tier` 那一列记的是什么
------------------------------------------------------------------------------

* `Tier0` 文件名/路径（分钟级）——`files` 那一行写进去 + `files_fts` 有它的名字。
* `Tier1` 正文 + FTS（小时级）——`blobs` / `chunks` / `chunks_fts`（`F4-02` 的那条路）。
* `Tier2` 向量语义（天级）——`chunks.vec_label` / `embed_ver`（`P5`）。

`files.tier` 记的是**达成的最高级**，不是"跑到哪一步"，也不是"配置允许到哪一级"。
两个方向的区别都很实在：

* 记成"跑到哪一步"的话，`fulltext` 与 `tier=1` 就是同一件事说两遍，而 `F3-07` 定下的
  `skipped` / `error` 两个**终态**没有容身之处（它们"跑到"的是 Tier1 那一趟，
  但什么都没做成）；
* 配置允许到哪一级由 `TiersCfg` 说（`metadata` / `fulltext` / `semantic` 三个开关），
  它是**意图**；`tier` 是**事实**。两者混在一起之后，"为什么这个文件没做正文"
  就永远分不清是"不该做"还是"还没做"。

------------------------------------------------------------------------------
Tier0 的"做完了"**不能**从 `tier` 读出来
------------------------------------------------------------------------------

`tier` 的默认值是 `0`，所以"已达 Tier0"与"什么都没做"在那一列上长得一模一样——
而 `tier < 0` 恒假，写成 SQL 就是**一趟挑不到任何文件**，且不报错。所以 Tier0 的
完成由 `status` 说：`pending` = 这一行在库里、名字还没进 `files_fts`（`put_file()`
写名字与写行在同一个事务里，所以 `pending` 是一个**中间态**；`P4` 的正常路径上一次
都不出现，它留着给"行先建、名字后索引"那类分步实现）。见 `work_condition()`。

------------------------------------------------------------------------------
就绪的分母是"做得到的"，不是总数
------------------------------------------------------------------------------

`skipped`（这一类 v1 不做）与 `error`（这个文件坏了）是**终态**：它们永远到不了
Tier1。所以 Tier1 的分母是 `总数 - 终态数`，**不是总数**——用总数的话，全盘上任何
一个 `.exe` 都会让"正文索引就绪"永远为假，而那句话从此不再有信息量（没人会去查一个
永远为假的标志）。

反过来说：**空库不算就绪**（`total == 0` 时每一级都不是 ready）。库里一行都没有与
"跑完了但没搜到"在数据上无法区分，而这两件事的正确回答完全不同——这一个 `False`
就是 `D10` 那句"显式标注"唯一的支点（`ChunkSearch` 的类文档里写过它说不清这件事）。

`stale`（内容变过、要重做）**不算做到**它到过的任何级别。那一条的正文还在库里，
所以它"看上去是有的"——正是这一格让"按 `tier` 那一列数一数"变成一个会骗人的判据。
`ledger_from()` 里那一条 `status != 'stale'` 就是它。

------------------------------------------------------------------------------
`status` 与 `tier` 不许互相矛盾
------------------------------------------------------------------------------

两个列回答同一个问题的两个方向：`status` 说"停在哪儿"，`tier` 说"做到过哪儿"。
于是有一条**单向**约束：**`status` 声称做到的那一级，`tier` 必须至少达到**——
`fulltext` ⇒ `tier >= 1`、`indexed` ⇒ `tier >= 2`。反过来的约束只有两条，
落在"什么都没做"那两个 status 上：`pending` 与 `metadata` ⇒ `tier == 0`
（一个 `tier = 1` 的 `metadata` 行，会让"这份文件的正文做了没有"有两个矛盾的答案，
而且**没有任何一趟会再挑到它**——`tier < 1` 那一趟要 `status = 'metadata'`，
`tier < 2` 那一趟要 `status IN ('fulltext', 'indexed', 'stale')`，
两边都不接它，于是它就那样躺着，谁也不知道）。`skipped` / `error` / `stale`
**故意不设约束**：`stale` 的 `tier` 是"曾经到过哪一级"（重做要接着它往上走），
而 `error` 可能是"做到过 Tier1、后来重做失败了"——那一条的正文**还在库里**，
把它的 `tier` 压成 0 是一句谎话。

约束由 `set_file_extracted()` 执行（`SqliteStore`），因为它**是唯一改这两列的地方**。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from localsearch.core.config import TiersCfg

__all__ = [
    "MAX_TIER",
    "STATUSES",
    "STATUS_MAX_TIER",
    "STATUS_MIN_TIER",
    "TERMINAL_STATUSES",
    "TIER_FULLTEXT",
    "TIER_METADATA",
    "TIER_NAMES",
    "TIER_SEMANTIC",
    "TierLedger",
    "WORK_STATUSES",
    "ledger_from",
    "target_tier",
    "tier_name",
    "work_condition",
]

# ==============================================================================
# 词汇表
# ==============================================================================

#: `D10` 的三级。**编号就是 `files.tier` 的取值**，不是随便编的序号。
TIER_METADATA = 0
TIER_FULLTEXT = 1
TIER_SEMANTIC = 2

#: `files.tier` 只会是 `0..MAX_TIER`。
MAX_TIER = TIER_SEMANTIC

#: 编号 → 名字。**名字与 `TiersCfg` 的字段名逐字相同**（`metadata` / `fulltext` /
#: `semantic`）——配置里写 `[tiers] fulltext = false`、日志里说"停在 fulltext"、
#: 报表的列名是 `fulltext`，三处是同一个词。改名时三处一起改（`test_tier_state.py`
#: 里有一条测试喂的正是 `TiersCfg` 的字段名，它会在改名时红）。
TIER_NAMES: dict[int, str] = {
    TIER_METADATA: "metadata",
    TIER_FULLTEXT: "fulltext",
    TIER_SEMANTIC: "semantic",
}

#: `files.status` 的取值（`schema.sql` 那行注释是全集）。**定义在本模块**而不是
#: `sqlite_store.py`：它不是一个"表上的约束"，而是状态机的词汇表——哪个取值是终态、
#: 它声称做到哪一级、哪一趟会挑到它，全在这一个文件里。
STATUSES = frozenset(
    {"pending", "metadata", "fulltext", "indexed", "skipped", "error", "stale"}
)

#: 终态：这两个 status 的**这一轮**到此为止，没有哪一趟会再挑它（`F3-07` 定的分工）。
#: ⚠ 它们**不是**"永远不再动"：`F6-02` 的变更检测会把改过的文件重新标成 `stale`
#: 或 `pending`，那是**另一条**路径（先有"文件变了"这个事实）。
TERMINAL_STATUSES = frozenset({"skipped", "error"})

#: 一趟要挑哪些 status 的行。**按 tier 分组**，见 `work_condition()`。
#:
#: ⚠ Tier0 那条**没有别的可能**：`tier = 0` 是"什么都没做"的默认值，所以"Tier0 还没做"
#: 只能由 `status = 'pending'` 说。
#:
#: ⚠ Tier1 / Tier2 那两条**故意用"不是什么"来写**（`NOT IN (终态, 等 Tier0 的)`），
#: 而不是"是什么"（`IN ('metadata', 'stale')`）：后者会让一个 `status` 与 `tier`
#: 互相矛盾的行**永远被两边同时漏掉**——它既不算做完（`tier` 没到），也不会被挑中
#: （`status` 不在名单里），于是它就那样躺在库里，而"就绪"这个标志永远为假。
#: 用"不是什么"写，矛盾的行会被**重做一遍**，而重做一遍正好把它修好。
WORK_STATUSES: dict[int, frozenset[str]] = {
    TIER_METADATA: frozenset({"pending"}),
    TIER_FULLTEXT: frozenset(STATUSES - TERMINAL_STATUSES - {"pending"}),
    TIER_SEMANTIC: frozenset(STATUSES - TERMINAL_STATUSES - {"pending", "metadata"}),
}

#: `status` 声称做到的那一级的下界（见模块文档最后一节）。没有列在这里的 status
#: **没有下界**：`skipped` / `error` / `stale` 的 `tier` 是"曾经到过哪儿"，可能比
#: 它们的字面意思高。
STATUS_MIN_TIER: dict[str, int] = {
    "pending": 0,
    "metadata": 0,
    "fulltext": TIER_FULLTEXT,
    "indexed": TIER_SEMANTIC,
}

#: `status` 声称**最多**做到哪一级。只有"什么都没做"的那两个 status 有上界，理由见
#: 模块文档（一个 `tier = 1` 的 `metadata` 行没有任何一趟会挑到它）。
STATUS_MAX_TIER: dict[str, int] = {
    "pending": TIER_METADATA,
    "metadata": TIER_METADATA,
}


def tier_name(tier: int) -> str:
    """`0` → `"metadata"`。不知道的编号当场报错（错的是调用方，不是这一层）。"""
    try:
        return TIER_NAMES[tier]
    except KeyError:
        raise ValueError(f"没有 {tier!r} 这一级，只有 {sorted(TIER_NAMES)}") from None


def target_tier(tiers: TiersCfg) -> int:
    """配置允许跑到哪一级（`D10` 的"意图"，与 `files.tier` 那个"事实"是两件事）。

    三个开关是**累进**的：`semantic = true` 而 `fulltext = false` 是没有意义的
    （向量挂在切片上，切片要正文那一趟才有），所以取的是**最大的那个打开的级别**——
    低的那两级会自动跟着做。三个全关等于"什么都不做"，返回 `-1`：
    那不是一级，调用方（`F4-04`）要把它读成"这一趟没有活干"。
    """
    if tiers.semantic:
        return TIER_SEMANTIC
    if tiers.fulltext:
        return TIER_FULLTEXT
    if tiers.metadata:
        return TIER_METADATA
    return -1


# ==============================================================================
# 选择条件
# ==============================================================================


def work_condition(tier: int) -> tuple[str, tuple[object, ...]]:
    """跑第 `tier` 级那一趟要挑哪些行：`(WHERE 之后的条件, 参数)`。

    返回的是**片段**而不是整条 SQL：拼在哪张表、按什么排、要不要 `LIMIT`，都是调用方
    的事（`SqliteStore.iter_tier_work()` 拼的是 `files`）。

    ⚠ Tier0 那一条**没有 `tier < ?` 子句**，而这不是省事：`tier` 的默认值是 0，
    `tier < 0` 恒假，写出来就是"这一趟永远挑不到任何文件"——一条不报错的死路。

    `status` 那串名单是**排序过**的（同一组取值永远生成同一个字符串）：条件片段会被
    日志与测试当字符串用，而 `frozenset` 的迭代顺序不保证跨进程一致。
    """
    statuses = WORK_STATUSES.get(tier)
    if statuses is None:
        raise ValueError(f"没有第 {tier!r} 级，只有 {sorted(WORK_STATUSES)}")
    names = tuple(sorted(statuses))
    marks = ", ".join("?" * len(names))
    if tier == TIER_METADATA:
        return f"status IN ({marks})", names
    # ⚠ 参数是 **int** 而不是 `str(tier)`：`tier` 那一列是 INTEGER，把 '1' 交给它
    # 虽然靠 SQLite 的列亲和性也能比出正确结果，但那是一处"语法上看不出对错"的比较。
    return f"tier < ? AND status IN ({marks})", (tier, *names)


# ==============================================================================
# 就绪台账
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TierLedger:
    """三级各自做到什么程度。**纯数据**，由 `ledger_from()` 从计数算出来。

    字段都是计数，没有一个是从别的字段推出来的（唯一的两处推导是 `done_at()` 与
    `expected_at()`，两条都有文档说明为什么）。装成对象而不是一串数字，是因为
    "就绪"这件事的问法有六种（每一级的进度、缺口、是不是空的），而它们在调用方
    那侧必须**算得一样**——`search/tier.py` 的标注与 `F4-04` 的日志要给出同一个答案。
    """

    #: 库里一共几行（= `files` 的行数）。Tier0 的"应当达到"就是它。
    total: int
    #: `status = 'pending'`：行在库里、名字还没进 `files_fts`（Tier0 没做完的那些）。
    pending: int
    #: 终态（`skipped` + `error`）：**永远到不了 Tier1** 的那些。它们是分母里的减项。
    terminal: int
    #: `status = 'stale'`：内容变过、要重做。`P4` 的正常路径上一次都不出现（`F6-02`）。
    stale: int
    #: `done[i]` = 已达成第 `i` 级的文件数，**累积**（`done[2] ⊆ done[1]`）。
    #: `done[0]` 是"行在库里且名字进过索引"（= `total - pending`），不是"行存在"；
    #: `done[1]` / `done[2]` **不含 `stale` 的行**（见 `ledger_from()`）。
    done: tuple[int, int, int]

    def done_at(self, tier: int) -> int:
        """已达第 `tier` 级的文件数。"""
        if tier not in TIER_NAMES:
            raise ValueError(f"没有第 {tier!r} 级，只有 {sorted(TIER_NAMES)}")
        return self.done[tier]

    def expected_at(self, tier: int) -> int:
        """**应当**达到第 `tier` 级的文件数（就绪的分母）。

        * Tier0：`total`——每一行都该有一个进过索引的名字。
        * Tier1 / Tier2：`total - terminal`——终态那些做不到，把它们算进分母的话，
          全盘上任何一个 `.exe` 都会让这一级**永远不算就绪**。
        """
        if tier not in TIER_NAMES:
            raise ValueError(f"没有第 {tier!r} 级，只有 {sorted(TIER_NAMES)}")
        if tier == TIER_METADATA:
            return self.total
        return self.total - self.terminal

    def progress(self, tier: int) -> tuple[int, int]:
        """`(已达, 应当达)`——报"3 / 10"那两个数。"""
        return self.done_at(tier), self.expected_at(tier)

    @property
    def is_empty(self) -> bool:
        """库里一行都没有。**它不是"就绪"**，见 `ready()`。"""
        return self.total == 0

    def ready(self, tier: int) -> bool:
        """第 `tier` 级是不是做完了。

        ⚠ 空库**不算就绪**：`total == 0` 时两个数都是 0，`>=` 成立，于是三级全部
        "就绪"——而那是这个类最坏的一种错法（它把"根本没索引过"报成"一切正常"）。
        """
        if self.is_empty:
            return False
        return self.done_at(tier) >= self.expected_at(tier)

    def can_reach(self, tier: int) -> bool:
        """这一级**有没有活可干**（分母不为 0）。空库与"全是终态文件"都是 `False`，
        但两者的含义完全不同：前者要你先把索引跑起来，后者是"本来就没有可做的"。"""
        return self.expected_at(tier) > 0


def ledger_from(counts: Mapping[tuple[str, int], int]) -> TierLedger:
    """从 `{(status, tier): 条数}` 算出台账（`SqliteStore.tier_counts()` 就是那个映射）。

    这个形状（而不是七个现成的数）是刻意的：**`files` 是一个交叉表**，而三个级别的
    进度是它的三种切法。让数据库那边只做"点一次数"、把"怎么算"全部留在这里，
    好处是这一层**不建库就能测**（喂一个字典进去），而库那一侧只剩一条 SQL。

    **两级各自的"达成"是怎么数的**（两条不一样，都是刻意的）：

    * Tier0：`total - pending`。字面地数 `tier >= 0` 等于每一行都算数（0 是默认值），
      那是一个**恒真**的判断；而"这一行的名字进过 `files_fts` 没有"是 Tier0 的唯一
      实质内容，它由 `pending` 说（见模块文档）。
    * Tier1 / Tier2：`tier >= 该级` **且 `status != 'stale'`**。`stale` 的行内容已经
      变了，库里那一份是旧的，**不算做到**它到过的任何级别——包括它 `tier` 上写着的
      那一级。少算一行的后果是多一句"未就绪"，多算一行的后果是**报就绪而内容是旧的**：
      两个方向都不好，但只有一个是"看起来正常"的那种坏。

    ⚠ 不认识的 `status` / `tier` 当场报错：那种行的来源只有两种——手改过库，或者
    `schema.sql` 与代码漂了——而两种情况下"照单全收"都会让台账给出一个**看起来
    正常**的数（增量式的：多出来的那行被算进 `total` 却落在三级之外，于是三级全
    就绪而文件总数对不上）。宁可当场炸。
    """
    total = pending = terminal = stale = 0
    reached = [0, 0, 0]
    for key, count in counts.items():
        status, tier = key
        if status not in STATUSES:
            raise ValueError(f"不认识的 status：{status!r}（{STATUSES}）")
        if tier not in TIER_NAMES:
            raise ValueError(f"没有第 {tier!r} 级，只有 {sorted(TIER_NAMES)}")
        if count < 0:
            raise ValueError(f"条数不可能是负的：{key} → {count}")
        total += count
        if status == "pending":
            pending += count
        if status in TERMINAL_STATUSES:
            terminal += count
        if status == "stale":
            stale += count
        if status != "stale":
            for level in (TIER_FULLTEXT, TIER_SEMANTIC):
                if tier >= level:
                    reached[level] += count
    reached[TIER_METADATA] = total - pending
    return TierLedger(
        total=total,
        pending=pending,
        terminal=terminal,
        stale=stale,
        done=(reached[0], reached[1], reached[2]),
    )
