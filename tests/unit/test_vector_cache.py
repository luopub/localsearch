r"""`F5-03` 的嵌入输入级缓存（`index/vector_cache.py` + `sqlite_store` 的四条 SQL）。

关联验收项 **A9**（"同一份内容出现在多个路径时，只提取与嵌入一次"）与 **D12**。

本文件钉的是**静默错误**——它们不抛异常、不写日志，只在很久以后表现为"换了模型之后
一部分切片按旧语义参与排序""标题明明有、就是搜不出来""缓存看着有、其实一条也没省下"：

* 键按**切片正文**算（不带前缀）→ "标题不同、正文相同"的两条撞成一个键，第二条复用
  第一条的向量，**标题没进向量**；
* 键里**不带 `embed_ver`** → 换了模型/维度/量化之后旧向量被静默复用；
* 同一次运行里重复的文本**没去重** → 同一条文本在 HNSW 里有两份向量，指向它的切片
  各说各话，而 `embed_cache` 里只会留下一组；
* `labels` 与 `todo` **错位** → 每条切片挂上别人的向量，搜得到、顺序不对；
* `put_vectors()` **覆盖**已有键 → 先写的那个 label 从此不可达（只能靠 HNSW 自己引用）；
* `delete_vectors()` / `count_vectors()` **漏掉版本条件** → "清缓存"顺手把别的版本也
  清了，代价是整库重嵌（`A1` 的两万文件基准集上数小时到一天）；
* 一次 `IN (…)` 塞超过 SQLite 的参数上限 → `OperationalError` 把整趟索引打断在第
  一批上（不是"慢一点"，是**做不到**）。

假嵌入器复用 `F5-02` 的 `pool_fakes.FakeEmbedder`：它是真的 `BatchEmbedder`
（所以 `document_input()` 走真代码），只有 `_forward()` 换成"按文本算一个确定的向量"。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import TaskPrefixes
from localsearch.index.sqlite_store import (
    _SQL_PARAM_LIMIT,
    SqliteStore,
    StoreError,
    _chunked,
)
from localsearch.index.vector_cache import (
    TEXT_HASH_ALGO,
    CachePlan,
    VectorCache,
    document_key,
    plan_lookup,
    text_hash,
)
from pool_fakes import FakeEmbedder, tiny_cfg, vector_for

VER = "embed:1|google/embeddinggemma-2|32|float32|2048|int8"
OTHER_VER = "embed:1|google/embeddinggemma-2|32|float32|2048|none"


class CountingFake(FakeEmbedder):
    """记下**模型真正看过哪些文本**：`_forward()` 的入参就是喂进去的那串字符。

    这是 `A9` 唯一能落到纸上的证据形态——不是"缓存里有几行"，而是"这次跑，模型少看了
    几条"。它**不进进程池**（这些测试单进程跑），所以不需要可 pickle。
    """

    def __init__(self, cfg: EmbedCfg) -> None:
        # `record=False`：这些测试不该往 `F5-02` 的探针文件里写行（那几行是"起了一个
        # worker"的证据，这里是同一个进程里造出来的假嵌入器，不是 worker）。
        super().__init__(cfg, record=False)
        self.seen: list[str] = []

    def _forward(self, texts: Sequence[str]) -> np.ndarray:
        self.seen.extend(texts)
        return super()._forward(texts)


def embedder(cfg: EmbedCfg | None = None) -> CountingFake:
    return CountingFake(cfg or tiny_cfg())


def cache(ver: str = VER) -> tuple[SqliteStore, VectorCache]:
    store = SqliteStore.in_memory()
    return store, VectorCache(store=store, embed_ver=ver)


# ==============================================================================
# 一、text_hash：摘要本身
# ==============================================================================


def test_hash_is_stable_and_names_its_algorithm() -> None:
    """同一个输入恒等，而且返回值**自带算法名**（`content_hash` 的 `sha256:` 同源）。

    前缀不是装饰：换算法之后库里旧的那些行不会报错，只会**永远命不中**（下一次索引
    重嵌一遍）。带上算法名，日志里一眼能看出"这批键是哪个算法留下的"。
    """
    one = text_hash("title: none | text: 正文")
    assert one == text_hash("title: none | text: 正文")
    assert one.startswith(f"{TEXT_HASH_ALGO}:")
    assert len(one.split(":")[1]) == 32  # 128 位 = 32 个十六进制字符


def test_hash_distinguishes_texts() -> None:
    """只差一个字符就是另一个键——这里连"只差一个空格"也要分开。"""
    assert text_hash("甲") != text_hash("乙")
    assert text_hash("甲 乙") != text_hash("甲乙")
    assert text_hash("甲") != text_hash("甲 ")


def test_hash_does_not_normalize_line_endings_or_unicode() -> None:
    r"""**故意不做任何规范化**：CRLF 与 LF 是两个键，NFC 与 NFD 也是两个键。

    这一条容易被人"顺手统一一下"，而它是有方向的：不规范化导致的是**少命中**（多嵌
    一次，安全方向）；规范化之后键就不再等于"模型看到的那串字符"，而两条本质不同的
    文本被折叠成同一个键才是错的那一边。⚠ 注意这里断言的是**不相等**。
    """
    assert text_hash("甲\r\n乙") != text_hash("甲\n乙")
    # 下一行两个 "é" 看着一模一样，**字节不同**：前一个是 U+00E9（预组合），
    # 后一个是 U+0065 U+0301（e + 组合尖音符）。改这一行前先确认自己没把它们统一成
    # 同一个字面量——统一之后这条断言就变成"同一个值不等于自己"，必失败。
    assert text_hash("é") != text_hash("é")


def test_empty_text_still_hashes() -> None:
    """空串照常返回一个值，不特判：`document_input()` 在空正文上返回的也是合法输入。"""
    assert text_hash("") == text_hash("")
    assert text_hash("") != text_hash(" ")


# ==============================================================================
# 二、document_key：键必须是"喂给模型的那串字符"
# ==============================================================================


def test_key_goes_through_document_input() -> None:
    """`document_key()` 的返回值**逐字节等于** `text_hash(喂进去的那串字符)`。

    这一条是"取键只有一个出口"的机器可读版本：拿 `document_input()` 的结果再摘要，
    与 `document_key()` 必须一致。两者一旦分家，就是模块文档第一节那条静默错。
    """
    fake = embedder()
    fed = fake.document_input("正文", "标题")
    assert fed == "title: 标题 | text: 正文"
    assert document_key(fake, "正文", "标题") == text_hash(fed)


def test_title_is_inside_the_key() -> None:
    """**标题进了向量，所以标题必须进键。** 同一个正文、两个标题 → 两个键。

    这是本模块最要紧的一条：按正文算键的实现会让这两条撞成一个，于是第二条复用第一条
    的向量——标题没进向量，不报错，只是那条文件再也搜不到标题带来的那点信号。
    """
    fake = embedder()
    assert document_key(fake, "正文", "甲") != document_key(fake, "正文", "乙")


def test_missing_and_blank_titles_share_one_key() -> None:
    """`None`、`""`、只有空白的标题 → **同一个键**（它们喂给模型的东西一模一样）。

    这一条跟着 `TaskPrefixes.document_prefix()` 的定义走：那三种写法落到的都是
    `title: none` 那一个槽。把它们分开会让同一段文本多嵌几次（安全方向但白花钱）。
    """
    fake = embedder()
    keys = {
        document_key(fake, "正文", None),
        document_key(fake, "正文", ""),
        document_key(fake, "正文", "   "),
    }
    assert len(keys) == 1
    # 有标题的那一条**不**是同一个键（否则上面那条 `title_is_inside_the_key` 就是假的）
    assert document_key(fake, "正文", "甲") not in keys


def test_key_follows_the_embedder_prefixes() -> None:
    """换了前缀（同一段文本、同一个模型）就是另一个键。

    `embed_ver` 里**没有前缀**（它收的是模型、维度、dtype、`max_seq`、量化——见
    `embed.base.embed_ver()` 的文档串），所以"模型文件里的前缀词表换了"这件事**只能**
    靠键变来发现。键要是按正文算的，这一条就漏了：换了前缀之后靠旧前缀算出的向量会被
    静默复用，而 `embed_ver` 没变、谁也拦不住。
    """
    one = embedder()
    other = embedder()
    other.prefixes = TaskPrefixes(
        query="task: search result | query: ",
        document="title: none | passage: ",
    )
    assert document_key(one, "正文", "标题") != document_key(other, "正文", "标题")


# ==============================================================================
# 三、plan_lookup：纯函数那一半
# ==============================================================================


def test_plan_all_miss() -> None:
    plan = plan_lookup(["a", "b"], {})
    assert plan.todo == ("a", "b")
    assert plan.hits == {}
    assert plan.entries == 2
    assert plan.saved == 0
    assert plan.deduped == 0


def test_plan_all_hit() -> None:
    """全命中：`todo` 为空，**并且不用 `record()` 就能取到完整的 label 表**。"""
    plan = plan_lookup(["a", "b"], {"a": 1, "b": 2})
    assert plan.todo == ()
    assert plan.hits == {"a": 1, "b": 2}
    assert plan.saved == 2
    assert plan.cached_entries == 2
    assert plan.recorded  # 没有东西要录，构造出来就算录完了
    assert plan.labels_in_order() == [1, 2]


def test_plan_mixed_and_keeps_input_order() -> None:
    plan = plan_lookup(["b", "a", "c"], {"a": 7})
    assert plan.todo == ("b", "c")  # 按**首次出现**的顺序
    plan.record([10, 20])
    assert plan.labels_in_order() == [10, 7, 20]


def test_plan_dedupes_within_the_run() -> None:
    """同一批里重复的文本只进 `todo` 一次——**同一个键只有一个 label**。

    不去重的后果不只是多嵌一次：两次嵌入会拿到两个 label，而 `embed_cache` 里只会留下
    其中一组，于是"plan 里记的那一组"与"库里存的那一组"从此对不上，两边都不报错。
    """
    plan = plan_lookup(["a", "b", "a", "a", "c"], {})
    assert plan.todo == ("a", "b", "c")
    assert plan.entries == 5
    assert plan.deduped == 2
    assert plan.saved == 2
    plan.record([1, 2, 3])
    assert plan.labels_in_order() == [1, 2, 1, 1, 3]  # 重复的条目指向**同一个** label


def test_plan_dedupes_hits_too() -> None:
    """命中的键重复出现：`cached_entries` 按条目数、`len(hits)` 按唯一键数。"""
    plan = plan_lookup(["a", "a", "a"], {"a": 9})
    assert plan.cached_entries == 3  # 三次嵌入被省掉
    assert len(plan.hits) == 1  # 库里只有一行
    assert plan.deduped == 2
    assert plan.saved == 3
    # ⚠ 两个口径**不能相加**：3 + 2 = 5 > entries。要总数就用 saved。
    assert plan.saved != plan.cached_entries + plan.deduped


def test_plan_ignores_found_keys_that_were_not_asked_about() -> None:
    plan = plan_lookup(["a"], {"a": 1, "z": 99})
    assert plan.hits == {"a": 1}
    assert plan.label_for("z") is None


# ==============================================================================
# 四、CachePlan：录 label
# ==============================================================================


def test_record_rejects_length_mismatch() -> None:
    """错位的代价是"每条切片挂上别人的向量"，而结果看起来像那么回事。"""
    plan = plan_lookup(["a", "b", "c"], {})
    with pytest.raises(ValueError, match="不等长"):
        plan.record([1, 2])
    with pytest.raises(ValueError, match="不等长"):
        plan.record([1, 2, 3, 4])


def test_record_rejects_being_called_twice() -> None:
    """第二次录会把同一批键指到两组 label 上，而库里只留得下一组。"""
    plan = plan_lookup(["a"], {})
    plan.record([1])
    with pytest.raises(ValueError, match="已经 record"):
        plan.record([2])
    assert plan.labels_in_order() == [1]  # 第一组说了算，不会被第二次改掉


def test_labels_in_order_refuses_before_record() -> None:
    """没录完就抛，**不留一条"部分 None"的表**——那会一路写进 `chunks.vec_label`。"""
    plan = plan_lookup(["a", "b"], {})
    assert not plan.recorded
    with pytest.raises(ValueError, match="还没录完"):
        plan.labels_in_order()


def test_label_for_sees_hits_and_fresh() -> None:
    plan = plan_lookup(["a", "b"], {"a": 7})
    assert plan.label_for("a") == 7
    assert plan.label_for("b") is None
    plan.record([8])
    assert plan.label_for("b") == 8


def test_plan_does_not_mutate_the_caller_table() -> None:
    """`keys` 原样收下（只读），调用方的列表不被改动——`tuple(keys)` 是拷贝。"""
    keys = ["a", "b"]
    plan = plan_lookup(keys, {})
    plan.record([1, 2])
    assert keys == ["a", "b"]
    assert isinstance(plan, CachePlan)


# ==============================================================================
# 五、库里那四条 SQL
# ==============================================================================


def test_find_vectors_empty_input() -> None:
    """空入参直接返回空表：不要为此发一条 `IN ()` 的语法错 SQL。"""
    store = SqliteStore.in_memory()
    assert store.find_vectors([], embed_ver=VER) == {}


def test_find_vectors_reads_back_what_was_written() -> None:
    store = SqliteStore.in_memory()
    assert store.put_vectors([("h1", 3), ("h2", 4)], embed_ver=VER) == 2
    assert store.find_vectors(["h1", "h2"], embed_ver=VER) == {"h1": 3, "h2": 4}
    assert store.find_vectors(["h1", "h9"], embed_ver=VER) == {"h1": 3}


def test_find_vectors_is_scoped_to_the_version() -> None:
    """**键是两列。** 少了版本这一半，换模型之后旧向量会被静默复用。"""
    store = SqliteStore.in_memory()
    store.put_vectors([("h1", 3)], embed_ver=VER)
    assert store.find_vectors(["h1"], embed_ver=OTHER_VER) == {}
    assert store.find_vectors(["h1"], embed_ver=VER) == {"h1": 3}


def test_find_vectors_merges_every_chunk() -> None:
    r"""一次查**跨段**的键表（1000 条 > `_SQL_PARAM_LIMIT`）：每一段的结果都要合并回来。

    ⚠ 这一条**不是**在验"超限会炸"：本机 sqlite 3.50.4 的参数上限是 **32766**
    （实测，`E42`），1000 个参数离它很远，所以去掉分块这段测试照样绿。它钉的是
    **合并**——只取第一段（900 条）时，剩下 100 条每次查都报"没有"，于是那 100 条
    切片**每趟重嵌一遍**，而缓存看着是有的。
    """
    store = SqliteStore.in_memory()
    pairs = [(f"h{i:05d}", i) for i in range(1000)]
    assert store.put_vectors(pairs, embed_ver=VER) == 1000
    keys = [key for key, _ in pairs]
    assert store.find_vectors(keys, embed_ver=VER) == dict(pairs)
    # 只查后半段也要对（保证不是"碰巧整段都在一块里"）
    assert store.find_vectors(keys[900:], embed_ver=VER) == dict(pairs[900:])


def test_chunked_keeps_every_key_and_bounds_each_chunk() -> None:
    r"""`_chunked()` 自己的契约：**一段都不丢，每段不超过上限**。

    为什么要摸私有名字：`find_vectors()` 的分块要防的是**编译期**那个上限
    （`SQLITE_MAX_VARIABLE_NUMBER`，老版本 999、本机 32766），所以拿真查询去撞它
    得造 3 万个键；而"每段多大"这件事本身可以直接断言。没有这一条，把
    `_SQL_PARAM_LIMIT` 改成 10 万、或者让 `_chunked()` 整段返回，都没有测试会红。
    """
    items = [f"k{i}" for i in range(2500)]
    chunks = list(_chunked(items, _SQL_PARAM_LIMIT))
    assert [len(one) for one in chunks] == [
        _SQL_PARAM_LIMIT,
        _SQL_PARAM_LIMIT,
        2500 - 2 * _SQL_PARAM_LIMIT,
    ]
    assert max(len(one) for one in chunks) <= _SQL_PARAM_LIMIT
    assert [key for one in chunks for key in one] == items  # 顺序与内容一个不差
    assert list(_chunked([], _SQL_PARAM_LIMIT)) == []  # 空表不发一条空 SQL


def test_put_vectors_rejects_duplicate_keys_in_one_call() -> None:
    """同一次调用里键重复**不是竞态**，是调用方的去重没做——当场抛。"""
    store = SqliteStore.in_memory()
    with pytest.raises(StoreError, match="出现了两次"):
        store.put_vectors([("h1", 1), ("h1", 2)], embed_ver=VER)


def test_put_vectors_does_not_overwrite_an_existing_key() -> None:
    """库里已有这个键（另一个进程写过）：**忽略，不覆盖**，返回值如实少一个。

    `INSERT OR REPLACE` 会让先写的那一份不可达（连带它指向的 label 变成只能靠 HNSW
    自己引用的孤儿），而两边存的其实是同一条文本的两个副本——谁都不是错的。所以选
    `OR IGNORE`：先说的那一份说了算，结果**确定**。
    """
    store = SqliteStore.in_memory()
    assert store.put_vectors([("h1", 1)], embed_ver=VER) == 1
    assert store.put_vectors([("h1", 2), ("h2", 3)], embed_ver=VER) == 1
    assert store.find_vectors(["h1", "h2"], embed_ver=VER) == {"h1": 1, "h2": 3}


def test_put_vectors_needs_a_version() -> None:
    store = SqliteStore.in_memory()
    with pytest.raises(ValueError, match="embed_ver"):
        store.put_vectors([("h1", 1)], embed_ver="")


def test_count_vectors_by_version_and_total() -> None:
    store = SqliteStore.in_memory()
    store.put_vectors([("h1", 1), ("h2", 2)], embed_ver=VER)
    store.put_vectors([("h1", 3)], embed_ver=OTHER_VER)
    assert store.count_vectors(embed_ver=VER) == 2
    assert store.count_vectors(embed_ver=OTHER_VER) == 1
    assert store.count_vectors() == 3
    assert store.count_vectors(embed_ver="embed:9|别的") == 0


def test_delete_vectors_only_touches_one_version() -> None:
    """**清缓存清的是某一版。** 顺手把别的版本也清了，代价是整库重嵌。"""
    store = SqliteStore.in_memory()
    store.put_vectors([("h1", 1), ("h2", 2)], embed_ver=VER)
    store.put_vectors([("h1", 3)], embed_ver=OTHER_VER)
    assert store.delete_vectors(embed_ver=VER) == 2
    assert store.count_vectors(embed_ver=VER) == 0
    assert store.find_vectors(["h1"], embed_ver=OTHER_VER) == {"h1": 3}


def test_delete_vectors_needs_a_version() -> None:
    """`embed_ver` **没有默认值**：给个默认值就等于"顺手清个缓存"和"清掉所有版本"
    写成同一个调用。"""
    store = SqliteStore.in_memory()
    with pytest.raises(ValueError, match="embed_ver"):
        store.delete_vectors(embed_ver="")


# ==============================================================================
# 六、VectorCache：两样绑在一起，以及端到端那一趟
# ==============================================================================


def test_cache_refuses_an_empty_version() -> None:
    store = SqliteStore.in_memory()
    with pytest.raises(ValueError, match="embed_ver"):
        VectorCache(store=store, embed_ver="")


def test_cache_round_trip() -> None:
    store, c = cache()
    plan = c.plan(["a", "b"])
    assert plan.todo == ("a", "b")
    assert c.record(plan, [11, 12]) == 2
    assert c.count() == 2
    assert plan.labels_in_order() == [11, 12]
    # 第二趟：全命中，一条都不写
    again = c.plan(["a", "b"])
    assert again.todo == ()
    assert again.hits == {"a": 11, "b": 12}


def test_cache_record_with_nothing_to_do_writes_nothing() -> None:
    store, c = cache()
    c.record(c.plan(["a"]), [1])
    plan = c.plan(["a"])
    assert c.record(plan, []) == 0
    assert c.count() == 1


def test_cache_clear_is_version_scoped() -> None:
    store = SqliteStore.in_memory()
    one = VectorCache(store=store, embed_ver=VER)
    other = VectorCache(store=store, embed_ver=OTHER_VER)
    one.record(one.plan(["a", "b"]), [1, 2])
    other.record(other.plan(["a"]), [1])
    assert one.clear() == 2
    assert one.count() == 0
    assert other.plan(["a"]).hits == {"a": 1}  # 另一版**一条没少**


def test_only_the_misses_reach_the_model() -> None:
    r"""端到端：**这是 `A9` 的证据形态。**

    不是"缓存里有几行"，而是"这次跑，模型少看了几条"——所以断言的是
    `CountingFake.seen`（`_forward()` 的入参，也就是真喂进去的那串字符）。
    """
    store, c = cache()
    fake = embedder()
    texts = ["甲", "乙", "甲", "丙", "甲"]
    title = "文档"
    # 去重后**该嵌哪三条**：调用方（`F5-04`）从 `plan.todo` 拿到的就是这几条文本，
    # 这里自己写出来，好让断言不依赖"从键里把正文拆回来"这种事。
    unique = ["甲", "乙", "丙"]

    plan = c.plan([document_key(fake, t, title) for t in texts])
    assert plan.todo == tuple(document_key(fake, t, title) for t in unique)

    vectors = fake.encode_documents(unique, [title] * len(unique))
    assert vectors.shape == (3, 32)
    assert c.record(plan, [0, 1, 2]) == 3

    # 三条看过、两条靠批内去重省下
    assert fake.seen == [fake.document_input(t, title) for t in unique]
    assert plan.entries == 5
    assert plan.saved == 2

    # 同一批再来一次：模型**一条都不看**
    before = list(fake.seen)
    again = c.plan([document_key(fake, t, title) for t in texts])
    assert again.todo == ()
    assert again.labels_in_order() == [0, 1, 0, 2, 0]
    assert fake.seen == before


def test_a_different_title_re_embeds_even_for_identical_bodies() -> None:
    """标题变了就是另一次嵌入——**向量确实不一样**（标题在前缀里）。

    这一条是"按正文算键"那个实现的审判：它会在这里给出全命中，而全命中的结果是那几条
    切片的向量**没有标题**，却带着"已经嵌过"的状态躺进库里。
    """
    store, c = cache()
    fake = embedder()
    texts = ["甲", "乙"]
    c.record(c.plan([document_key(fake, t, "旧标题") for t in texts]), [0, 1])
    new_keys = [document_key(fake, t, "新标题") for t in texts]
    plan = c.plan(new_keys)
    assert plan.todo == tuple(new_keys)
    assert plan.saved == 0  # 一条都没复用
    # 而且这两次嵌入**确实不同**：标题进了喂进去的那串字符
    assert fake.document_input("甲", "新标题") != fake.document_input("甲", "旧标题")


def test_a_different_version_re_embeds() -> None:
    """`embed_ver` 变了：同一批文本全部重嵌，旧版本的行还在。"""
    store = SqliteStore.in_memory()
    one = VectorCache(store=store, embed_ver=VER)
    other = VectorCache(store=store, embed_ver=OTHER_VER)
    fake = embedder()
    keys = [document_key(fake, "甲", "文档")]
    one.record(one.plan(keys), [0])
    assert one.plan(keys).todo == ()
    assert other.plan(keys).todo == tuple(keys)  # 另一版一条都不复用
    other.record(other.plan(keys), [1])
    assert store.count_vectors() == 2  # 两版各一行
    assert store.find_vectors(keys, embed_ver=VER) == dict.fromkeys(keys, 0)
    assert store.find_vectors(keys, embed_ver=OTHER_VER) == dict.fromkeys(keys, 1)


def test_fake_vectors_are_deterministic() -> None:
    """假向量按文本确定（`pool_fakes.vector_for`），所以上面那些断言才有意义。

    顺带钉住"同一个文本 → 同一个向量、不同文本 → 不同向量"这件事：`F5-02` 的测试
    靠它验"向量没串行"，这里也靠它。
    """
    assert np.array_equal(vector_for("甲", 32), vector_for("甲", 32))
    assert not np.array_equal(vector_for("甲", 32), vector_for("乙", 32))
