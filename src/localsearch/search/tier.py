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
降级与拒绝：三种"做不到"，处理方式凭"是不是人说的"
------------------------------------------------------------------------------

判据始终是 `explicit`（这句话是一个**人**说的，还是一个**默认值**说的）：

* **`explicit=True`**（`--mode X`）→ **拒绝**，不许悄悄换成别的。理由：一个人明确要了
  它，说明他要的是那件事，而另一种检索会给出一堆**看起来合理**的命中，他没有任何
  办法察觉自己拿到的是另一种东西。宁可失败，不可错答。⚠ 拒绝分**两种异常**，因为
  用户的下一步动作不同：

  - `ModeUnavailable`——**缺代码**（`--mode hybrid` 要等 `F5-05` 的 RRF）。换一版
    才有用。
  - `ModeNotReady`——**缺数据**（`--mode semantic` 而向量这一级一条都没建）。跑一趟
    `lsearch index --tier 2` 就有了。这两件事在日志里长得一样是最没用的那种混淆。

* **`explicit=False`**（配置里的 `mode = "hybrid"`，或者没写、用了内置默认）→
  **降级到可用的模式，并记一条 note**。理由：默认值表达的是一份**偏好**（"能混合
  就混合"），而配置模板里 `mode` 就是 `hybrid`——不降级的话 `lsearch search 关键词`
  开箱即坏，一个连默认配置都跑不起来的工具等于没有。⚠ 降级**必须**带 note：
  这正是 `D10` 说的"显式标注"。

* **层级没做完**（`Tier1` 还在跑、或者 `Tier2` 建了一半）。不抛错——那是常态，
  索引就是分几趟跑的。记 note，把**差多少**写进去（`3 / 100` 比"未就绪"有用得多：
  前者告诉你再等一会儿，后者只告诉你"不对劲"）。

⚠ 后两条的分界要看清，`F5-04` 之前它们是同一件事（都没有向量）。现在不是了：
**"一条都没有"会让模式降级**（那一级没得搜），而**"建了一半"只是标注**（搜索结果
不完整，但给得出）。把后者当成降级，就是白扔掉本来能拿到的那一半命中。

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
    "ModeNotReady",
    "ModeUnavailable",
    "Note",
    "Readiness",
    "resolve",
]

#: 检索模式（计划 §4.6 的 `--mode`，与 `SearchCfg.mode` 同一套取值）。
MODES = ("lexical", "semantic", "hybrid")

#: **已经实现**的模式。`F5-04` 把 `semantic` 加了进来；`hybrid` 要等 `F5-05` 的
#: RRF 融合——它**不是**"把两段结果拼在一起"，那件事 `F4-04` 就能做（它正是刻意
#: 不做的）。降级逻辑（`_FALLBACK`）与拒绝逻辑都读这一个 frozenset。
IMPLEMENTED_MODES = frozenset({"lexical", "semantic"})

#: 一个模式降级时**按顺序**试的替补，从最接近它意思的那个往下排。
#:
#: 排序表达的是"谁是谁的子集"：混合检索 = 词法 + 语义，所以它降级先丢语义；
#: 语义检索没有更接近的替补，只能落到词法（那是唯一能跑的东西，而 note 必须
#: 说清它**不是**语义检索）。
#:
#: ⚠ 候选是否可用有**两个**判据：实没实现（`IMPLEMENTED_MODES`）与**那一级有没有
#: 东西可查**（`_blocked()`）。`F5-04` 之前只有第一条——那时"没实现"与"没数据"
#: 是同一个事实（都没有向量）。现在不是了：一个装好了向量索引的库上 `hybrid` 落到
#: `semantic`，而一个还没建向量的库上它会一路落到 `lexical`，两种情况说的话
#: **不一样**。
_FALLBACK: dict[str, tuple[str, ...]] = {
    "lexical": ("lexical",),
    "semantic": ("semantic", "lexical"),
    "hybrid": ("hybrid", "semantic", "lexical"),
}

#: 每个模式**需要**哪几级索引。它同时回答两件事，都是"少哪一级"那个判断的输入：
#: 语义那一路没有 `TIER_SEMANTIC` 就**跑不起来**（索引文件都不在），而词法那一路
#: 少一级只是**结果不全**（降级标注由 `_TIER_MESSAGE` 那一路负责，不是模式降级）。
_MODE_TIERS: dict[str, tuple[int, ...]] = {
    "lexical": (TIER_METADATA, TIER_FULLTEXT),
    "semantic": (TIER_SEMANTIC,),
    "hybrid": (TIER_METADATA, TIER_FULLTEXT, TIER_SEMANTIC),
}

#: 每个模式**跑起来**至少要哪一级有东西。⚠ **没有条目的模式永远不会被"没数据"拦住**
#: ——`lexical` 就是它：正文一片都没做时它照样能按文件名搜（那是 `A8` 要求的降级
#: 形态，不是"跑不起来"），而库是空的时候它给 0 条命中加一句 `NOTE_EMPTY_INDEX`，
#: 那也是一次**说得清**的检索。而语义这一路一个向量都没有时连索引文件都不存在
#: （`VectorIndex.load()` 抛 `VectorIndexMissing`）——那不是"结果不全"，是"没得搜"。
_MODE_NEEDS_DATA: dict[str, int] = {
    "semantic": TIER_SEMANTIC,
    "hybrid": TIER_SEMANTIC,
}

_MODE_LABEL: dict[str, str] = {
    "lexical": "词法（关键词）",
    "semantic": "语义（向量）",
    "hybrid": "混合（词法 + 语义）",
}

#: 每个模式**因此少了什么**。降级之后必须说这句话，见 `_downgrade_message()`。
_MODE_LIMIT: dict[str, str] = {
    "lexical": "只按关键词命中，换个说法搜不到",
    "semantic": "只按意思相近找，关键词对不上的搜不到",
}

#: "这个模式还没实现"那句话，按模式分写。降级消息的**前半截**。
#:
#: ⚠ 它必须说清**缺的是哪一块**而不是"还没做"：`hybrid` 的两个半边各自都实现了
#: （关键词 + 语义），缺的只是把它们**融**起来（RRF，`F5-05`）——含糊地说一句
#: "混合检索还没实现"，用户会以为语义那一半也不能用。
#:
#: ⚠ 这一句里**不许出现任何一个模式的显示名**（`_MODE_LABEL` 的那些词）：它是要不要
#: 降级都成立的**原因**，而"这一趟实际按什么检索"由 `_downgrade_message()` 的后半截
#: 说。原因里顺手提一句"那两个各自都能跑"，读起来就像在说这一趟会跑哪两个。
_MISSING_IMPL: dict[str, str] = {
    "semantic": "语义检索还没实现。",
    "hybrid": "混合检索还没实现（缺的是把两路结果融起来的 RRF，归 F5-05："
    "那两路各自都能跑）。",
}

#: "要的那一级还没有东西可查"那句话。降级消息的**后半截**（可能与前一条同时出现：
#: `hybrid` 在还没建向量的库上就是两条都要说）。`{tier}` / `{done}` / `{expected}`
#: 由 `_downgrade_message()` 填。
#:
#: ⚠ 它**逐字**含「语义未就绪」——那是计划与 `CLAUDE.md` 里点名的那句话，测试钉的
#: 就是这个子串（措辞可以改，改掉它不行：它是 `A8` 的可观察形态）。
_MISSING_DATA = "语义未就绪：向量索引还没建（{done} / {expected}）。"

#: note 的种类。**测试断言的是 code 而不是消息文本**：消息是给人看的、会改，
#: 而"这一趟因为什么降级了"是一个稳定的判断（CLI 可能按它决定要不要标红）。
NOTE_EMPTY_INDEX = "empty-index"
NOTE_MODE_DOWNGRADED = "mode-downgraded"
NOTE_TIER_INCOMPLETE = "tier-incomplete"

#: 每条 note 怎么说话。⚠ 三句都**点名后果**而不是只报进度：用户要知道的是
#: "什么东西现在搜不到"，`3 / 100` 只有配上那句话才有用。
#:
#: ⚠ 三句都要对 `{done} == 0` 与 `0 < {done} < {expected}` **都成立**：`F5-04` 之后
#: `semantic` 是一个**真能跑**的模式，于是"一条向量都没有"不再意味着它被降级——
#: 词法那一路从 `P4` 起就一直是这个处境（正文一片没做时它照样按文件名搜）。
#: 所以 Tier2 那句不能再说"这一级现在给不出结果"：向量建了一半时它**给得出**，
#: 给的是一份只覆盖建好那部分的命中，而那是要标注的**不完整**，不是"没有"。
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
        "语义未就绪：{done} / {expected}。向量只建了一部分，命中只覆盖已经建好的"
        "那些切片——换个说法、只给意思的查询可能搜不到。"
    ),
}


class ModeUnavailable(Exception):
    """有人明确要了一个**还没实现**的检索模式。

    ⚠ 不是 `ValueError`：`ValueError` 在这个代码库里是"传进来的值本身写错了"（调用方
    的 bug），而这一格**调用方没错**——`--mode hybrid` 是一个完全合理的请求，只是
    这一版还做不到。混淆两者会让"配置里有个错字"与"功能还没做"在日志里长得一样。

    ⚠ 与 `ModeNotReady` 的分工是"**缺什么**"：这一格缺的是**代码**（换一版就有了），
    那一格缺的是**数据**（跑一趟 `lsearch index --tier 2` 就有了）。两者对用户的
    下一步动作完全不同，合成一个异常就等于把"去跑索引"和"等下一版"说成同一件事。
    """

    def __init__(self, requested: str, available: frozenset[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"--mode {requested} 还没实现（现在能做的是 "
            f"{'、'.join(sorted(available))}）——"
            f"{_MISSING_IMPL.get(requested, '它')}".rstrip("。")
            + "。只想按关键词检索的话显式写 --mode lexical。"
        )


class ModeNotReady(Exception):
    r"""有人明确要了一个**能跑、但那一级还没有东西可查**的模式（`F5-04`）。

    ⚠ 为什么 explicit 时是**拒绝**而不是降级加标注：一个人明确要了语义检索，词法会
    给他一堆看起来合理的**关键词**命中，他没有办法察觉自己拿到的是另一种东西
    （同 `ModeUnavailable` 的理由，见模块文档）。而"还没建"这件事他一定能修好——
    所以这句话的后半截永远是**怎么做**，不是一个"等下一版"。

    `tier` 是缺的那一级，`done` / `expected` 是它的进度。⚠ 两者都要给：`0 / 7`
    与 `0 / 0`（一条都不该做——库是空的、或者文件全是终态）的正确修法不同，
    后者跑完索引也没用。
    """

    def __init__(self, requested: str, *, tier: int, done: int, expected: int) -> None:
        self.requested = requested
        self.tier = tier
        self.done = done
        self.expected = expected
        label = _MODE_LABEL[requested]
        if expected == 0:
            how = (
                "库里现在没有哪一份文件本该做到这一级（文件数是 0，或者全都是"
                "不做正文的那一类），所以跑完索引也还是没有向量——先确认索引本身"
                "跑过了（`lsearch index`）。"
            )
        else:
            how = "先跑 `lsearch index --tier 2` 把向量那一级建起来。"
        super().__init__(
            f"{label}现在给不出结果：它要的第 {tier} 级一条都没有"
            f"（{done} / {expected}）。{how}"
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


def _blocked(mode: str, ledger: TierLedger, tiers: TiersCfg) -> tuple[int, int, int] | None:
    """这个模式**跑得起来吗**：跑不起来时返回缺的那一级与它的进度，跑得起来返回 `None`。

    两个成因合成一句话答（两者对用户是同一件事："这一级现在没东西"）：

    * 配置里那一级**从来不会做**（`TIER_SEMANTIC > target_tier(tiers)`）；
    * 或者台账说那一级**一条都没做到**（`done_at(...) == 0`）。

    ⚠ 它故意**只问"有没有"**，不问"做完没有"：向量建了一半时语义检索**给得出**结果，
    给的是一份只覆盖建好那部分的命中——那是**不完整**，由 `_TIER_MESSAGE` 那一条
    标注（`A8` 的原话就是"降级并显式标注"，而不是"别搜了"）。
    """
    tier = _MODE_NEEDS_DATA.get(mode)
    if tier is None:
        return None
    if tier > target_tier(tiers) or ledger.done_at(tier) == 0:
        done, expected = ledger.progress(tier)
        return tier, done, expected
    return None


def _effective_mode(
    requested: str, ledger: TierLedger, tiers: TiersCfg
) -> tuple[str | None, tuple[str, ...]]:
    """`requested` 降级之后的模式与**为什么**。没降级时返回 `(None, ())`。

    第二个返回值是零到两条"缺什么"的话（`hybrid` 在没建向量的库上就是两条），
    由 `_downgrade_message()` 拼成跑给用户看的那一句。
    """
    why: list[str] = []
    for candidate in _FALLBACK[requested]:
        if candidate not in IMPLEMENTED_MODES:
            why.append(_MISSING_IMPL.get(candidate, f"{candidate} 还没实现。"))
            continue
        gap = _blocked(candidate, ledger, tiers)
        if gap is not None:
            tier, done, expected = gap
            if tier == TIER_SEMANTIC:
                why.append(_MISSING_DATA.format(done=done, expected=expected))
            else:  # pragma: no cover - 今天只有 Tier2 会拦住一个模式
                why.append(_TIER_MESSAGE[tier].format(done=done, expected=expected))
            continue
        return (candidate if candidate != requested else None), tuple(why)
    # 一个都不可用：`lexical` 永远在 `IMPLEMENTED_MODES` 里、而且它只要 Tier0
    # ——而空库在上面就被 `NOTE_EMPTY_INDEX` 拦下了。走到这里说明那张表被改坏了，
    # 那种情况下没有任何模式可跑，当场报错好过猜一个。
    raise AssertionError(
        f"{requested!r} 没有任何可用的替补，而 lexical 本应是底座——"
        f"IMPLEMENTED_MODES 是不是被改空了？"
    )


def _downgrade_message(requested: str, effective: str, why: tuple[str, ...]) -> str:
    r"""降级时**给用户看的那句话**：先说**缺什么**（可能两条），再说**我实际做了什么**。

    两半都不许省：

    * 只说"缺什么"——用户不知道自己拿到的是另一种检索，那正是 `A8` 里"静默"那一半；
    * 只说"降级了"——用户不知道**为什么**，也就没法判断是"等一版"还是"去跑索引"。

    ⚠ "实际按什么检索、因此少了什么"这一整句是按**实际生效的模式**填的，不是写死的：
    同一个 `hybrid` 在一个建好向量的库上落到 `semantic`、在没建的库上落到 `lexical`，
    写死任何一边都会在另一边变成一处**看起来正常**的错话。
    """
    return (
        "".join(why)
        + f"这一趟按{_MODE_LABEL[effective]}检索——{_MODE_LIMIT[effective]}。"
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
    fallback, why = _effective_mode(requested, ledger, tiers)
    effective = requested if fallback is None else fallback
    if fallback is not None:
        if explicit:
            # 一个人明确要了它。**两种情况分开报**：缺代码（换一版）与缺数据
            # （去跑索引）——它们的下一步动作完全不同（见 `ModeNotReady` 的文档）。
            if requested not in IMPLEMENTED_MODES:
                raise ModeUnavailable(requested, IMPLEMENTED_MODES)
            tier, done, expected = _blocked(requested, ledger, tiers) or (
                _MODE_NEEDS_DATA[requested],
                0,
                0,
            )
            raise ModeNotReady(requested, tier=tier, done=done, expected=expected)
        notes.append(Note(NOTE_MODE_DOWNGRADED, _downgrade_message(requested, effective, why)))

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
