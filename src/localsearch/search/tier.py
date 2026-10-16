r"""分级就绪的**标注**：这一趟能做什么、少搜了什么（`D10`，验收项 `A8`）。测试落点
`tests/unit/test_tier_state.py`。

与 `index/tier.py` 的分工：那个模块管**状态**（词表、选择条件、台账——都要读库或
即将被库读），这个模块管**话**（拿一份台账与用户要的模式，决定能不能做、要说什么）。
所以这里是**纯函数**：不碰数据库、不碰文件系统、不打印，出入都是值。

------------------------------------------------------------------------------
"就绪"不是"好"或"坏"，是"我刚才说的话算不算数"
------------------------------------------------------------------------------

`A8` 的原话是"Tier2 未完成时必须降级并**显式标注**「语义未就绪」，不允许静默返回
不完整结果"。这句话里最要紧的是**静默**那两个字：一个不完整的检索结果本身没有错
（索引还没跑完是常态），错的是**它看起来和完整的结果一模一样**。用户看到 3 条命中，
无从知道这 3 条是从全盘里搜出来的、还是从已经索引的那 3% 里搜出来的。

所以 `resolve()` 产出一串 `Note`，每一条说明**这一趟在哪一件事上打了折**。它**不**
决定怎么显示（那是 `presenter/`）、也**不**决定退出码（那是 `F4-04`）——它只保证
"打了折"这件事有一处**算得对**的来源。

------------------------------------------------------------------------------
降级与拒绝：两种"做不到"，处理方式相反
------------------------------------------------------------------------------

* **模式没实现**（`P4` 只有词法）。这里分两种情况，判据是 `explicit`：

  - `explicit=True`——**这句话是一个人说的**（`--mode semantic`）。**当场抛
    `ModeUnavailable`**，不许悄悄换成词法。理由：一个人明确要了语义检索，说明他
    要的是"意思相近"，而词法检索会给出一堆**看起来合理**的关键词命中，他没有任何
    办法察觉自己拿到的是另一种东西。宁可失败，不可错答。
  - `explicit=False`——**这句话是一个默认值说的**（配置文件里的 `mode = "hybrid"`，
    或者没写、用了内置默认）。降级到已实现的模式，**并记一条 note**。理由：默认值
    表达的是一份**偏好**（"能混合就混合"），而 P4 的配置模板里 `mode` 就是 `hybrid`
    ——不降级的话 `lsearch search 关键词` 开箱即坏，一个连默认配置都跑不起来的工具
    等于没有。⚠ 降级**必须**带 note：这正是 `D10` 说的"显式标注"。

* **层级没做完**（`Tier1` 还在跑、或者 `Tier2` 根本还没开始）。不抛错——那是常态，
  索引就是分几趟跑的。记 note，把**差多少**写进去（`3 / 100` 比"未就绪"有用得多：
  前者告诉你再等一会儿，后者只告诉你"不对劲"）。

⚠ 判断层级缺口时**只看 `target_tier(tiers)` 以内的级别**：用户把 `fulltext = false`
关掉之后，Tier1 永远不会做，此时还一直警告"正文未就绪"就是一句**永远为真的废话**
——而永远为真的警告会被学会忽略，连带着真警告一起被忽略。
"""

from __future__ import annotations

from dataclasses import dataclass

from localsearch.core.config import TiersCfg
from localsearch.index.tier import (
    TIER_FULLTEXT,
    TIER_METADATA,
    TIER_SEMANTIC,
    TierLedger,
    target_tier,
)

__all__ = [
    "IMPLEMENTED_MODES",
    "MODES",
    "NOTE_EMPTY_INDEX",
    "NOTE_MODE_DOWNGRADED",
    "NOTE_TIER_INCOMPLETE",
    "ModeUnavailable",
    "Note",
    "Readiness",
    "resolve",
]

#: 检索模式（计划 §4.6 的 `--mode`，与 `SearchCfg.mode` 同一套取值）。
MODES = ("lexical", "semantic", "hybrid")

#: **已经实现**的模式。`P4` 只有词法；`P5` 把 `semantic` / `hybrid` 加进来时，
#: 改的是这一个 frozenset——降级逻辑（`_FALLBACK`）与拒绝逻辑都读它，所以
#: "实现了一个新模式"与"允许用户选它"是同一步，不会漂。
IMPLEMENTED_MODES = frozenset({"lexical"})

#: 一个模式降级时**按顺序**试的替补，从最接近它意思的那个往下排。
#:
#: 排序表达的是"谁是谁的子集"：混合检索 = 词法 + 语义，所以它降级先丢语义；
#: 语义检索没有更接近的替补，只能落到词法（那是唯一能跑的东西，而 note 必须
#: 说清它**不是**语义检索）。`P5` 落地向量之后，`hybrid` 会先落到 `semantic`。
_FALLBACK: dict[str, tuple[str, ...]] = {
    "lexical": ("lexical",),
    "semantic": ("semantic", "lexical"),
    "hybrid": ("hybrid", "semantic", "lexical"),
}

#: 每个模式**需要**哪几级索引。`P4` 只有词法那一条会被真的用到，另外两条留着是因为
#: 它们正是"为什么词法模式不该抱怨 Tier2"的答案——没有这张表，那个判断只能写成
#: 一句 `if tier == TIER_SEMANTIC: continue`，而那种写法看不出意图。
_MODE_TIERS: dict[str, tuple[int, ...]] = {
    "lexical": (TIER_METADATA, TIER_FULLTEXT),
    "semantic": (TIER_SEMANTIC,),
    "hybrid": (TIER_METADATA, TIER_FULLTEXT, TIER_SEMANTIC),
}

_MODE_LABEL: dict[str, str] = {
    "lexical": "词法（关键词）",
    "semantic": "语义（向量）",
}

#: 每个模式**因此少了什么**。降级之后必须说这句话，见 `_DOWNGRADE_TEMPLATE`。
_MODE_LIMIT: dict[str, str] = {
    "lexical": "只按关键词命中，换个说法搜不到",
    "semantic": "只按意思相近找，关键词对不上的搜不到",
}

#: 降级时**给用户看的那句话**，按"要的是什么"分写。`{label}` / `{limit}` 由**实际
#: 生效的模式**填——⚠ 不能把"词法"写死在这里：`P5` 之后 `hybrid` 会先落到
#: `semantic`（见 `_FALLBACK`），那时写死的那句话就成了一处**看起来正常**的错话。
#:
#: ⚠ 两条都**逐字**含「语义未就绪」——那是计划与 `CLAUDE.md` 里点名的那句话，测试
#: 钉的就是这个子串（措辞可以改，改掉它不行：它是 `A8` 的可观察形态）。反过来说，
#: 也不许**只有**这句话：用户要知道的是"我拿到的是另一种东西"，所以每条都接着说
#: "这一趟实际按什么检索、因此而少了什么"。
_DOWNGRADE_TEMPLATE: dict[str, str] = {
    "semantic": (
        "语义未就绪：P5 之前没有向量。这一趟按{label}检索代替——{limit}。"
    ),
    "hybrid": (
        "语义未就绪：混合检索里的语义那一半要等 P5 的向量。这一趟按{label}检索"
        "——{limit}。"
    ),
}

#: note 的种类。**测试断言的是 code 而不是消息文本**：消息是给人看的、会改，
#: 而"这一趟因为什么降级了"是一个稳定的判断（CLI 可能按它决定要不要标红）。
NOTE_EMPTY_INDEX = "empty-index"
NOTE_MODE_DOWNGRADED = "mode-downgraded"
NOTE_TIER_INCOMPLETE = "tier-incomplete"

#: 每条 note 怎么说话。⚠ 三句都**点名后果**而不是只报进度：用户要知道的是
#: "什么东西现在搜不到"，`3 / 100` 只有配上那句话才有用。
_TIER_MESSAGE: dict[int, str] = {
    TIER_METADATA: (
        "文件名索引还没做完：{done} / {expected}。没做完的那些文件现在**搜不到**，"
        "连按文件名也搜不到。"
    ),
    TIER_FULLTEXT: (
        "正文未就绪：{done} / {expected}。现在的命中只覆盖文件名与路径，"
        "正文里的词一律搜不到。"
    ),
    TIER_SEMANTIC: (
        "语义未就绪：{done} / {expected}。向量还没建完，这一级现在给不出结果；"
        "换个说法、只给意思的查询搜不到。"
    ),
}


class ModeUnavailable(Exception):
    """有人明确要了一个**还没实现**的检索模式。

    ⚠ 不是 `ValueError`：`ValueError` 在这个代码库里是"传进来的值本身写错了"（调用方
    的 bug），而这一格**调用方没错**——`--mode semantic` 是一个完全合理的请求，只是
    这一版还做不到。混淆两者会让"配置里有个错字"与"功能还没做"在日志里长得一样。
    """

    def __init__(self, requested: str, available: frozenset[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"--mode {requested} 还没实现（现在能做的是 "
            f"{'、'.join(sorted(available))}）——语义那一半要等 P5。"
            f"只想按关键词检索的话显式写 --mode lexical。"
        )


@dataclass(frozen=True, slots=True)
class Note:
    """一句"这一趟在哪件事上打了折"。

    `code` 是稳定的判据，`message` 是给人看的话。`tier` 只在层级缺口那一条上有值
    （层间缺口的 note 需要知道是哪一级，前两种与级别无关）。
    """

    code: str
    message: str
    tier: int | None = None


@dataclass(frozen=True, slots=True)
class Readiness:
    """这一趟检索"能做什么"的结论。

    `requested` 与 `mode` **要分开留着**：前者是问题的原文，后者是这一趟实际做的事。
    把两者合成一个字段，就等于删掉了"降级"这件事本身——而 `D10` 要标注的正是它。
    """

    #: 用户（或配置）要的模式，原样留着。
    requested: str
    #: 这一趟实际按哪个模式检索。`== requested` 当且仅当没有降级。
    mode: str
    notes: tuple[Note, ...] = ()

    @property
    def degraded(self) -> bool:
        """这一趟做的事与要的事不一样（模式降级过）。"""
        return self.mode != self.requested

    def has(self, code: str) -> bool:
        """有没有某一类 note（CLI 按 code 决定要不要换行、标红）。"""
        return any(note.code == code for note in self.notes)


def _effective_mode(requested: str) -> str | None:
    """`requested` 降级之后的模式；**它本身已经实现**时返回 `None`（= 没降级）。"""
    for candidate in _FALLBACK[requested]:
        if candidate in IMPLEMENTED_MODES:
            return candidate if candidate != requested else None
    # 一个都不可用：`lexical` 永远在 `IMPLEMENTED_MODES` 里（它是底座），所以走到这里
    # 说明那个 frozenset 被改空了——那种情况下没有任何模式可跑，当场报错好过猜一个。
    raise AssertionError(
        f"{requested!r} 没有任何可用的替补，而 lexical 本应是底座——"
        f"IMPLEMENTED_MODES 是不是被改空了？"
    )


def resolve(
    requested: str,
    ledger: TierLedger,
    *,
    tiers: TiersCfg,
    explicit: bool = False,
) -> Readiness:
    """这一趟按哪个模式检索、要标注什么。

    参数：

    * `requested`——要的模式（`MODES` 之一）。不认识的值抛 `ValueError`：那一格是
      **调用方的错**（`--mode` 有 Typer 的枚举把关，配置里的错字要在这里被抓住）。
    * `ledger`——台账（`index.tier.ledger_from(store.tier_counts())`）。
    * `tiers`——配置里的三个开关，用来判断"这一级**本来该不该**做"（见模块文档）。
    * `explicit`——这个模式是**一个人**说的（命令行参数）还是**一个默认值**说的
      （配置文件）。决定了"没实现"的两种相反处理，见模块文档。

    ⚠ 台账的**来源**由调用方保证是刚刚算出来的。这个方法不读库，所以它也不知道
    那份数是不是旧的——`F4-04` 要在每一趟检索**开始时**取一次并往下传，而不是在
    结果格式化那一层现取（那样的话"标注"说的就是另一时刻的库）。
    """
    if requested not in MODES:
        raise ValueError(f"mode 只能是 {list(MODES)} 之一，收到 {requested!r}")

    notes: list[Note] = []
    fallback = _effective_mode(requested)
    effective = requested if fallback is None else fallback
    if fallback is not None:
        if explicit:
            raise ModeUnavailable(requested, IMPLEMENTED_MODES)
        notes.append(
            Note(
                NOTE_MODE_DOWNGRADED,
                _DOWNGRADE_TEMPLATE[requested].format(
                    label=_MODE_LABEL[effective], limit=_MODE_LIMIT[effective]
                ),
            )
        )

    if ledger.is_empty:
        notes.append(
            Note(
                NOTE_EMPTY_INDEX,
                "索引是空的：库里一行文件都没有，现在搜什么都搜不到——先跑 "
                "`lsearch index`。",
            )
        )
        # 空库时下面的层级缺口一条都不发：`can_reach()` 对三级全是 False，
        # 而"文件名索引 0 / 0 没做完"是一句没有信息量的话（要说的就是上面那句）。
        return Readiness(requested=requested, mode=effective, notes=tuple(notes))

    target = target_tier(tiers)
    for tier in _MODE_TIERS[effective]:
        if tier > target or not ledger.can_reach(tier) or ledger.ready(tier):
            continue
        done, expected = ledger.progress(tier)
        notes.append(
            Note(
                NOTE_TIER_INCOMPLETE,
                _TIER_MESSAGE[tier].format(done=done, expected=expected),
                tier=tier,
            )
        )
    return Readiness(requested=requested, mode=effective, notes=tuple(notes))
