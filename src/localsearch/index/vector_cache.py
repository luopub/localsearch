r"""嵌入输入级缓存（`F5-03`，`D12` / `A9`）。测试落点 `tests/unit/test_vector_cache.py`。

这一层回答一个问题：**这批要嵌的文本里，哪些其实不用再嵌一遍。**

------------------------------------------------------------------------------
一、键是"实际喂给嵌入器的那串字符"，不是"切片的文本"
------------------------------------------------------------------------------

键 = `(text_hash, embed_ver)`，而 `text_hash` 算的是
**`BatchEmbedder.document_input()` 的返回值**——文档前缀 + 正文，也就是模型真正看到
的那串字符。`embed_ver` 是 `chunks.embed_ver` 同一个串（模型、维度、dtype、`max_seq`、
量化，见 `embed.base.embed_ver()`）。

⚠ **这两个决定各自闭掉一条静默错**：

* 按**切片文本**（不带前缀）算键 → "标题不同、正文相同"的两条文档撞成一个键，第二条
  复用第一条的向量，**标题就悄悄没进向量**。它不报错，只是那条文件的语义检索结果里
  永远没有标题带来的那点信号。所以取键**只有一个出口**：`document_key()`，它转手调
  `document_input()`；**不要自己拼前缀**。
* 键里**不带版本** → 换了模型/维度/量化之后，同一串文本的旧向量被静默复用。计划
  §4.4.1 专门写了这一条（有实践者踩过），而它的症状是"换了模型之后，一部分切片是按
  旧模型的语义参与排序"——看起来完全正常。`embed_ver` 因此是 `embed_cache` 主键的
  一半，本模块四个库方法里它**没有默认值**。

------------------------------------------------------------------------------
二、为什么是 blake2b，以及为什么这个选择其实不重要
------------------------------------------------------------------------------

计划 §4.4 定的是 blake2b。这里照办，但把账算清楚：`ingest/fingerprint.py` 量过
blake2b 是 **452 MiB/s**（`E18`，stdlib 里那个没有 SIMD 的版本），而它比 sha256 慢
一倍——**在这个用例上无所谓**：一条切片 512 token 约 1–2 KiB，452 MiB/s 下约 3 微秒，
而嵌它一次是 **0.9 秒**（`M4` 的 553.0 tok/s，512 token）。差 **30 万倍**。
选 blake2b 是因为计划这么定、且换来的是"摘要可以短"（这里取 128 位）；**不是**因为
它更快。真要更快才该换 sha256。

⚠ 摘要取 128 位是够的：键还带 `embed_ver`，且撞了也只是**一次静默的错误复用**。
按 200 万条切片算生日界约 2^64，比"某个文件被改坏"的概率低得多。前缀 `blake2b-128:`
与 `content_hash` 的 `sha256:` 同一个作用：**让"拿错了指纹"在日志里一眼可见**。

------------------------------------------------------------------------------
三、`CachePlan`：一次批次的决策，**先算完再动手**
------------------------------------------------------------------------------

调用方（`F5-04` 的 Tier2 那一趟）拿到一批切片后，顺序是：

1. `keys = [document_key(embedder, chunk.text, title) for chunk in batch]`；
2. `plan = cache.plan(keys)`——**一次只读的查库**，得到 `plan.todo`（去重后、真正要嵌
   的那些键，按首次出现顺序）与 `plan.hits`；
3. 只把 `plan.todo` 对应的那些文本喂给嵌入器（`EmbedPool`），拿回一组 label；
4. `cache.record(plan, labels)`——把新键连 label 写进 `embed_cache`，同时 `plan`
   记下它们，于是 `plan.labels_in_order()` 给出**与输入等长且逐条对应**的 label 表。

第 2 步与第 4 步分开是刻意的：**"要不要嵌"这个判断必须在动手之前全部做完**。合在
一起写的话，同一批里重复出现的文本会在第一次嵌入之后才被发现——那时它已经嵌过了。

⚠ **同一次运行里的重复也要去重**（`plan.todo` 是去重后的）。这不只是省一次嵌入：
两次嵌入同一条文本会得到两个 label，而 `embed_cache` 拿后写的那个去盖先写的（或者
被 `put_vectors()` 的 `OR IGNORE` 留下先写的），于是同一条文本在 HNSW 里有两份向量、
指向它的切片各说各话。`plan` 因此在构造时就去重，`put_vectors()` 那边还有一道
"同一次调用里键不能重复"的兜底——**两道，因为这一处错了不报错**。

------------------------------------------------------------------------------
四、这一层不做的事
------------------------------------------------------------------------------

* **不分配 label**。`vec_label` 是 HNSW 里的 id（`F5-04` 的 `vectors_faiss.py` 说了
  算）。这一层只负责"把 label 记下来"与"把它读回来"。
* **不持有嵌入器、不读配置**。`embed_ver` 由调用方给（`BatchEmbedder.embed_ver`），
  前缀由 `document_key()` 转手问嵌入器。这一层因此是**纯的**：`text_hash()` 与
  `plan_lookup()` 连库都不要，能单测。
* **不管 `chunks.vec_label` 的写入**。切片那一列归 `F5-04` 的建库流程；本模块管的是
  `embed_cache` 那张表。
* **不清跨版本的账**。`clear()` 只清**这一版**。别的版本的行留着的代价是磁盘（每个
  条目几十字节），清错的代价是整库重嵌（`A1` 的两万文件基准集上数小时到一天）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # 只为标注，为了不让 `index` 这一层白白拖进 `embed` 的导入面
    from localsearch.embed.base import Embedder, TaskPrefixes
    from localsearch.index.sqlite_store import SqliteStore

__all__ = [
    "TEXT_HASH_ALGO",
    "CachePlan",
    "InputBuilder",
    "VectorCache",
    "document_key",
    "plan_lookup",
    "text_hash",
]

#: `text_hash()` 的算法与摘要长度。**改这个值等于换一套键**：库里旧的那些行不会报错，
#: 只会永远命不中（下一次索引重嵌一遍）。所以它进返回值的前缀，好让日志里看得出来。
TEXT_HASH_ALGO = "blake2b-128"

#: 摘要字节数。128 位——理由与账见模块文档第二节。
_DIGEST_BYTES = 16


class InputBuilder(Protocol):
    r"""**能给出"喂进模型的那串字符"的东西**——这一层对它的全部要求。

    它**不是** `Embedder`：`F5-04` 的 Tier2 那一趟在**父进程**里算缓存键，而父进程
    手上没有嵌入器（多进程那一条路上模型只在 worker 里加载）。它手上有的是一份
    `TaskPrefixes`，而"前缀 + 正文"这件事**本来就只是前缀的函数**
    （`embed/base.py` 的 `TaskPrefixes.document_input()`）。

    ⚠ 把它写成协议而不是"也收一个 `TaskPrefixes`"，是为了让**只有一个方法**这件事
    成为类型上可见的约束：`document_key()` 用不到嵌入器的任何别的东西，它也**不该**
    用——一旦它开始问 `dim` / `encode_*`，父进程那条路就装不出来了。
    """

    def document_input(self, text: str, title: str | None = None) -> str: ...  # pragma: no cover


def text_hash(text: str) -> str:
    r"""一段"喂给嵌入器的文本"的摘要，形如 `blake2b-128:1a2b3c…`。

    **入参必须是 `BatchEmbedder.document_input()` 的返回值**，不是切片的原始
    文本——理由见模块文档第一节。取键请走 `document_key()`。

    ⚠ **不做任何规范化**：不压空白、不统一换行、不做 Unicode NFC。这一条是有意的：
    键的全部意义是"模型看到的那串字符逐字节相同"，规范化之后它就不再是那串字符了，
    而"两条本该相同的文本没命中"只是**多嵌一次**（安全方向），"两条本质不同的文本
    被规范化成同一个键"才是错的那一边。所以宁可少命中。

    ⚠ **空串照常返回一个值**（blake2b 的空输入摘要），不特判：`document_input()` 在
    空正文上会返回"只有前缀"的那串字符，它也是合法输入。
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=_DIGEST_BYTES)
    return f"{TEXT_HASH_ALGO}:{digest.hexdigest()}"


def document_key(
    embedder: Embedder | TaskPrefixes | InputBuilder,
    text: str,
    title: str | None = None,
) -> str:
    r"""一条待嵌文档的缓存键。**取键只有这一个入口。**

    它做的事只有一件：把"实际喂给模型的字符"问出来（`document_input()`）再摘要。
    看起来薄得像没必要存在——**它存在的理由正是让"自己拼前缀"这条路要先绕开一个
    有名字的函数**。少了它，调用方会写 `text_hash(prefix + chunk.text)`，而
    `prefix` 从哪来、要不要带标题、标题里换行怎么办，就成了第二处实现；那一处的
    错法是静默的（模块文档第一节）。

    第一个参数收三种东西，都是"能给出那串字符"的（见 `InputBuilder`）：带嵌入器的
    调用方（查询侧）给嵌入器，`F5-04` 的 Tier2 那一趟给一份 `TaskPrefixes`
    （父进程没有嵌入器）。三种在运行时**没有区别**——都是调一个同名方法。

    `title` 的表达与 `encode_documents()` 一致：`None`、空串、只有空白的标题都得到
    `title: none` 那一个槽，因此**共享同一个键**——喂给模型的东西确实一模一样。
    """
    return text_hash(embedder.document_input(text, title))


@dataclass(slots=True)
class CachePlan:
    r"""一批文本的缓存决策。**构造之后就定了"哪些要嵌"**，`record()` 只负责收尾。

    四个数，**两种口径不要混**：

    * 按**条目**（= 切片数，含重复）：`entries`、`cached_entries`、`saved`；
    * 按**唯一键**：`len(hits)`、`len(todo)`、`deduped`。

    `saved = entries - len(todo)` 是唯一那个"这一趟少嵌了多少条"的数，也是 `A9`
    那个"只嵌入一次"唯一能落到纸面上的量。⚠ 它**不等于** `cached_entries + deduped`
    ——两个来源有重叠：同一个键在一批里命中三次，那三次既被 `cached_entries` 数到、
    也被 `deduped` 数到两次。要总数就用 `saved`，**不要相加**。
    """

    #: 每个待嵌条目的键，**与调用方的输入等长、逐条对应**（含重复）。
    keys: tuple[str, ...]
    #: 库里已经有的：键 → label。只含命中且出现在 `keys` 里的那些唯一键。
    hits: Mapping[str, int]
    #: 真正要嵌的键，**去重后**、按首次出现顺序。
    todo: tuple[str, ...]
    #: 本次输入里重复出现的个数（`entries - 唯一键数`），**不分命中与否**。
    deduped: int
    #: `record()` 之后填上：新嵌入的键 → label。
    fresh: dict[str, int] = field(default_factory=dict, repr=False)
    #: `record()` 被调用过就置真。存在的理由是 `todo` 为空时 `fresh` 也是空的，
    #: 光看 `fresh` 分不出"还没录"与"录了一批空的"。私有，不进 `__init__` 的语义。
    _done: bool = field(default=False, repr=False)

    @property
    def entries(self) -> int:
        return len(self.keys)

    @property
    def cached_entries(self) -> int:
        """有多少条**切片**是靠缓存省下的。按条目算，所以重复命中会重复计数。

        ⚠ 与 `len(hits)`（唯一键命中的**个数**）不是一回事：同一个键在一批里出现三
        次，那是三次嵌入被省掉、而库里只有一行。报表要的是前者，本属性给前者。
        """
        return sum(1 for key in self.keys if key in self.hits)

    @property
    def saved(self) -> int:
        """这一趟**少嵌**了多少条（缓存命中 + 批内去重，两者有重叠，见类文档串）。"""
        return self.entries - len(self.todo)

    @property
    def recorded(self) -> bool:
        """新嵌的那批录完了没有。没录完就调 `labels_in_order()` 会当场报错。

        ⚠ `todo` 为空时**构造出来就算"录完了"**：一批全命中时确实没有东西要录，
        那次调用 `labels_in_order()` 是合法的、不必先 `record([])`。
        """
        return self._done or not self.todo

    def record(self, labels: Sequence[int]) -> None:
        r"""收下"新嵌的那批"的 label。`labels` 与 `todo` **逐条对应**。

        ⚠ 长度必须相等，这不是形式主义：错位之后每一条切片都会挂上**别人**的向量，
        而搜索结果看着还挺像那么回事（搜得到、顺序不对，没人会发现）。同
        `encode_documents()` 对 `titles` 长度的要求。

        ⚠ 重复调用当场抛。第二次录会把同一批键指到两组不同的 label 上，而
        `embed_cache` 里只会留下其中一组——那之后 `labels_in_order()` 给的是**第二
        组**，库里存的是**第一组**，两边从此对不上，而且都不报错。
        """
        if len(labels) != len(self.todo):
            raise ValueError(
                f"labels 与 todo 不等长：{len(labels)} 对 {len(self.todo)}。"
                "两条是逐条对应的，长度不等说明调用方把某一列的切片搞错了——"
                "而错位之后每条切片都会挂上别人的向量，搜出来看着还挺像那么回事。"
            )
        if self._done:
            raise ValueError(
                "这个 CachePlan 已经 record() 过了。重复调用会把同一批键指到两组"
                "不同的 label 上，而 embed_cache 的表里只会留下其中一组。"
            )
        self._done = True
        self.fresh.update(zip(self.todo, (int(label) for label in labels)))

    def label_for(self, key: str) -> int | None:
        """这个键现在有没有向量（命中的 + 刚录的）。`record()` 之前只答命中的。"""
        if key in self.fresh:
            return self.fresh[key]
        return self.hits.get(key)

    def labels_in_order(self) -> list[int]:
        r"""**与调用方的输入等长、逐条对应**的 label 表：命中直接取，未命中取新录的。

        这是调用方真正要的东西——`chunks.vec_label` 那一列就是按这个顺序写下去的。
        没 `record()` 过就抛，不留一条"部分 None"的表：那会一路写进库里。
        """
        if not self.recorded:
            raise ValueError(
                f"还没录完新嵌的那批（已录 {len(self.fresh)} / 待录 {len(self.todo)}），"
                "此时给不出完整的 label 表。"
            )
        labels: list[int] = []
        for key in self.keys:
            label = self.label_for(key)
            if label is None:  # pragma: no cover - recorded 已经保证不会走到
                raise ValueError(f"键 {key!r} 既没命中也没有新 label")
            labels.append(label)
        return labels


def plan_lookup(keys: Sequence[str], found: Mapping[str, int]) -> CachePlan:
    r"""纯函数那一半：给定键表与"库里查到的"，算出这一批要嵌什么。

    `keys` 里**允许重复**（同一批里两片文本一样很常见），重复只会让 `todo` 少一项、
    让 `saved` 多一。顺序按**首次出现**——`todo` 的顺序就是嵌入器收到的顺序，也就是
    调用方拼文本时的顺序，三处一致才能让 `labels` 与 `todo` 对齐。

    `found` 里**多出来的键**（不在 `keys` 里的）直接忽略：调用方多半是把两次查询的
    结果合在了一起，因为那个去重不是错误。
    """
    todo: list[str] = []
    seen: set[str] = set()
    hits: dict[str, int] = {}
    for key in keys:
        if key in found:
            hits[key] = int(found[key])
            continue
        if key in seen:
            continue
        seen.add(key)
        todo.append(key)
    return CachePlan(
        keys=tuple(keys),
        hits=hits,
        todo=tuple(todo),
        deduped=len(keys) - len({*keys}),
    )


@dataclass(frozen=True, slots=True)
class VectorCache:
    r"""把**库**与**版本**绑在一起——这两样必须同时是对的。

    单独一个"用哪个 `embed_ver`"的参数散在各处，就会出现"查的时候按 A 版、写的
    时候按 B 版"：查 A 说没命中，写 B 记下 label，于是 A 版永远命不中（每次都重嵌），
    而库里攒着一条谁也用不上的行。**不报错，只是缓存从此不生效。**

    `F5-03` 只交付这一层；把它接进 `index --tier 2` 那一趟的是 `F5-04`。
    """

    store: SqliteStore
    embed_ver: str

    def __post_init__(self) -> None:
        if not self.embed_ver:
            raise ValueError(
                "embed_ver 不能为空——它是缓存键的一半，空值会让'换了模型'这件事"
                "无法察觉（旧向量被静默复用）"
            )

    def plan(self, keys: Sequence[str]) -> CachePlan:
        """一次只读查库 + 算决策。**不写任何东西。**"""
        return plan_lookup(keys, self.store.find_vectors(keys, embed_ver=self.embed_ver))

    def record(self, plan: CachePlan, labels: Sequence[int]) -> int:
        r"""把新嵌的那批记进库、并让 `plan` 带上它们。返回**新写进去的行数**。

        顺序是先在 `plan` 上 `record()`（校验长度、拦住重复调用），再写库——写库那
        一步会 `commit()` 之外的活都留给调用方（与这一层其它写入一致，事务边界属于
        管线）。

        ⚠ 返回值和 `len(plan.todo)` 可能不等：库里已经有同一个键（另一个进程写过）
        时 `OR IGNORE` 会跳过，返回的是**真正新增**的行数。这不是错误，见
        `sqlite_store.put_vectors()` 的文档串。
        """
        plan.record(labels)
        if not plan.todo:
            return 0
        return self.store.put_vectors(
            [(key, plan.fresh[key]) for key in plan.todo], embed_ver=self.embed_ver
        )

    def count(self) -> int:
        """这一版缓存里有多少条。"""
        return self.store.count_vectors(embed_ver=self.embed_ver)

    def clear(self) -> int:
        r"""清掉**这一版**的全部条目，返回清了几条。**不提交**。

        ⚠ 谁该调它：**重建向量索引的人**。`vec_label` 是 HNSW 里的位置，索引重建
        之后旧位置指向的不是旧的向量，而"缓存说有、索引里没有"会让那一批切片
        **永远不再被嵌**（每次查都命中、每次都指向一个不存在的 id）。所以这一句必须
        与建索引在同一个流程里，归 `F5-04` / `F6`。
        """
        return self.store.delete_vectors(embed_ver=self.embed_ver)
