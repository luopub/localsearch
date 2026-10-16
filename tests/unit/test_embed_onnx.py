r"""`embed/gemma_onnx.py`：ONNX int8 后端（`D9` 的生产后端）。

------------------------------------------------------------------------------
这一组为什么这样切
------------------------------------------------------------------------------

分两段，**证明方式完全不同**：

1. **喂进去的到底是什么**——用假 session / 假 tokenizer。这一段的每一条断言都对应一个
   "不报错但算错"的失败形态：拼错 feed 的键名（ORT 会报错，还好）、多模态槽喂了非空
   张量（**不报错**，算出一堆看着正常的向量）、按注意力掩码池化时漏乘掩码（**不报错**，
   同一条文本因为邻居不同而得到不同向量，而 `F5-03` 的缓存正是假设它不变）。
   假 session 是一棵**脚本化的图元数据**：我们声明图有哪些输入、什么形状，然后断言
   喂进去的东西与声明**逐项对应**。"不写死多模态槽的形状与类型"这条纪律，就是靠
   换一份图元数据（`(0, 512)` 换成 `(0, 1024)`）来验的。

2. **真模型**——权重就在本机缓存里，冷启动 0.5 秒，比 `sentence_transformers` 那一支
   便宜 10 倍，所以这一支可以多跑几条：形状、单位长度、批大小无关性、按 `dim` 截断、
   标题槽真的进了前缀（有标题与没标题得到不同向量）、以及一条**语义合理性**检查
   （相关文档的余弦必须高于无关文档）——那一条是"前缀与后端都接对了"唯一能看到的证据，
   因为前缀写错也不会报错，只会让排序变差。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import EmbedError, EmbedUnavailable, TaskPrefixes
from localsearch.embed.gemma_onnx import (
    ONNX_MODEL_FILE,
    ONNX_REPO,
    OnnxEmbedder,
    empty_input_for,
)

QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "

#: 图里那份**真实**的多模态槽形状（P0 的图 I/O 实测：`(0, 512)` float）。
MULTIMODAL_SHAPE = [0, 512]


#: 两条前缀。这里按字面写，是因为**假** session 那一段不该依赖模型文件的读法（那是
#: `test_embed_base.py` 的活）；真模型那两条测试用的是从快照里读出来的那两条。
_PREFIXES = TaskPrefixes(query=QUERY_PREFIX, document=DOCUMENT_PREFIX)


@pytest.fixture
def prefixes() -> TaskPrefixes:
    return _PREFIXES


# ==============================================================================
# 假 session / 假 tokenizer：验"喂进去的到底是什么"
# ==============================================================================


class _NodeArg:
    """ORT 的 `NodeArg` 里我们真正读的两样：`name` 与 `type` / `shape`。"""

    def __init__(self, name: str, type: str = "tensor(float)", shape=None):
        self.name = name
        self.type = type
        self.shape = list(shape) if shape is not None else []


class _Encoding:
    def __init__(self, ids: list[int]):
        self.ids = ids
        #: 真 tokenizers 在 `enable_padding` 之后会给这个：padding 位是 0，正文位是 1。
        self.attention_mask = [0 if i == 0 else 1 for i in ids]


class _FakeTokenizer:
    """把每条文本编成固定的 3 个 id（按文本长度填充），并记下收到的文本。"""

    def __init__(self):
        self.seen: list[str] | None = None

    def encode_batch(self, texts):
        self.seen = list(texts)
        return [_Encoding([101, 200 + len(t) % 50, 102]) for t in texts]


class _FakeSession:
    """脚本化的 session：输入/输出元数据由测试给，`run` 只记下 feed 然后回一份向量。"""

    def __init__(self, inputs, outputs, reply: np.ndarray | None = None):
        self._inputs = inputs
        self._outputs = outputs
        self._reply = reply
        self.feed: dict[str, np.ndarray] | None = None
        self.asked: list[str] | None = None

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs

    def run(self, names, feed):
        self.asked = list(names)
        self.feed = dict(feed)
        rows = len(next(iter(feed.values()))) if feed else 1
        reply = self._reply if self._reply is not None else np.ones((rows, 768), dtype=np.float32)
        return [reply]


#: 与真图同形的输入清单（P0 实测：5 进）。
GRAPH_INPUTS = [
    _NodeArg("input_ids", "tensor(int64)", ["batch", "sequence"]),
    _NodeArg("attention_mask", "tensor(int64)", ["batch", "sequence"]),
    _NodeArg("image_features", "tensor(float)", MULTIMODAL_SHAPE),
    _NodeArg("video_features", "tensor(float)", MULTIMODAL_SHAPE),
    _NodeArg("audio_features", "tensor(float)", MULTIMODAL_SHAPE),
]

#: 与真图同形的输出清单（2 出，`sentence_embedding` 在前是因为我们要优先取它）。
GRAPH_OUTPUTS = [_NodeArg("last_hidden_state"), _NodeArg("sentence_embedding")]


def _make(session, prefixes, **cfg) -> tuple[OnnxEmbedder, _FakeTokenizer]:
    tokenizer = _FakeTokenizer()
    embedder = OnnxEmbedder(
        EmbedCfg(**cfg), prefixes=prefixes, session=session, tokenizer=tokenizer
    )
    return embedder, tokenizer


def test_feed_covers_exactly_the_inputs_the_graph_declares(prefixes: TaskPrefixes):
    """feed 的键**恰好**是图声明的那些：不多（ORT 会拒收未声明的键）也不少（必需的
    多模态槽少一个，ORT 直接报错）。"""
    session = _FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS)
    embedder, _ = _make(session, prefixes)
    embedder.encode_queries(["甲", "乙"])
    assert set(session.feed) == {node.name for node in GRAPH_INPUTS}


def test_the_multimodal_slots_are_fed_empty_tensors(prefixes: TaskPrefixes):
    """多模态槽喂 `(0, 512)` 的**空** float 张量。

    这是"纯文本"的表达方式。喂成别的（比如 `(1, 512)` 的零张量）**不会报错**——模型会
    当作"这条样本带一张全黑的图"算下去，得到的向量看着完全正常。
    """
    session = _FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS)
    embedder, _ = _make(session, prefixes)
    embedder.encode_queries(["甲"])
    for name in ("image_features", "video_features", "audio_features"):
        tensor = session.feed[name]
        assert tensor.shape == (0, 512)
        assert tensor.dtype == np.float32
        assert tensor.size == 0


def test_empty_shapes_come_from_the_graph_not_from_a_constant():
    """换一份图元数据（512 → 1024），空张量的形状跟着变。

    这条测的是"不写死"这条纪律本身——写死 512 的话，换一份导出就会喂错形状，
    而**喂错形状在多模态槽上不报错**（ORT 只在批维不匹配时报）。
    """
    leaf = _NodeArg("image_features", "tensor(float)", [0, 1024])
    assert empty_input_for(leaf).shape == (0, 1024)
    dynamic = _NodeArg("image_features", "tensor(float)", ["batch", 768])
    assert empty_input_for(dynamic).shape == (0, 768)
    # 形状元数据为空 → 零长度一维数组（而不是 `np.zeros(())` 那个 size 为 1 的标量 0）。
    unknown = _NodeArg("image_features", "tensor(float)", None)
    assert empty_input_for(unknown).shape == (0,)


def test_an_unknown_multimodal_dtype_is_refused():
    """解不出类型就**当场失败**，不许猜——猜错的形状喂进去不会出错，只会算错。"""
    with pytest.raises(EmbedUnavailable, match="tensor\\(string\\)"):
        empty_input_for(_NodeArg("image_features", "tensor(string)", MULTIMODAL_SHAPE))


def test_the_two_tokenizer_inputs_are_int64(prefixes: TaskPrefixes):
    session = _FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS)
    embedder, tokenizer = _make(session, prefixes)
    embedder.encode_queries(["甲", "乙"])
    assert session.feed["input_ids"].dtype == np.int64
    assert session.feed["attention_mask"].dtype == np.int64
    assert session.feed["input_ids"].shape == (2, 3)
    assert tokenizer.seen == [QUERY_PREFIX + "甲", QUERY_PREFIX + "乙"]


def test_token_type_ids_is_zero_filled_when_the_graph_wants_it(prefixes: TaskPrefixes):
    inputs = [
        _NodeArg("input_ids", "tensor(int64)", ["batch", "sequence"]),
        _NodeArg("attention_mask", "tensor(int64)", ["batch", "sequence"]),
        _NodeArg("token_type_ids", "tensor(int64)", ["batch", "sequence"]),
    ]
    session = _FakeSession(inputs, GRAPH_OUTPUTS)
    embedder, _ = _make(session, prefixes)
    embedder.encode_queries(["甲"])
    assert session.feed["token_type_ids"].tolist() == [[0, 0, 0]]


def test_a_graph_without_input_ids_is_refused(prefixes: TaskPrefixes):
    """名字对不上时报错，并把图声明的名字**一起**说出来——否则还得自己去翻导出配置。"""
    inputs = [_NodeArg("pixel_values", "tensor(float)", [1, 3, 224, 224])]
    with pytest.raises(EmbedUnavailable) as info:
        _make(_FakeSession(inputs, GRAPH_OUTPUTS), prefixes)
    assert "input_ids" in str(info.value)
    assert "pixel_values" in str(info.value)


def test_sentence_embedding_is_preferred(prefixes: TaskPrefixes):
    """图里有现成的池化结果就用它，不去自己 mean pool。"""
    session = _FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS)
    embedder, _ = _make(session, prefixes)
    embedder.encode_queries(["甲"])
    assert session.asked == ["sentence_embedding"]


def test_a_graph_with_neither_output_is_refused(prefixes: TaskPrefixes):
    outputs = [_NodeArg("logits")]
    with pytest.raises(EmbedUnavailable, match="logits"):
        _make(_FakeSession(GRAPH_INPUTS, outputs), prefixes)


def test_a_session_without_a_tokenizer_is_refused(prefixes: TaskPrefixes):
    with pytest.raises(EmbedUnavailable, match="要么都给"):
        OnnxEmbedder(EmbedCfg(), prefixes=prefixes, session=_FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS))


def test_an_injected_session_needs_prefixes():
    """注入了 session 就没有模型文件可读前缀——不许"半个真、半个假"。"""
    with pytest.raises(EmbedUnavailable, match="prefixes"):
        OnnxEmbedder(
            EmbedCfg(),
            session=_FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS),
            tokenizer=_FakeTokenizer(),
        )


# ==============================================================================
# 池化（只在图不给 `sentence_embedding` 时走）
# ==============================================================================


def test_mean_pool_ignores_padding():
    """padding 位的 hidden state **不是 0**（是 pad token 过完模型的结果），漏乘掩码就会
    把它算进平均——于是同一条文本因为**邻居**不同而得到不同向量。

    这条测试直接盯着那个失败形态：两条真实 token 一样、padding 长度不同，
    池化结果必须逐位相同。
    """
    from localsearch.embed.gemma_onnx import _mean_pool

    hidden = np.array([[[1.0, 1.0], [3.0, 3.0], [999.0, 999.0]]], dtype=np.float32)
    short = _mean_pool(hidden, np.array([[1, 1, 0]], dtype=np.int64))
    long_hidden = np.concatenate([hidden, np.full((1, 5, 2), 777.0, dtype=np.float32)], axis=1)
    long_mask = np.array([[1, 1] + [0] * 6], dtype=np.int64)
    long = _mean_pool(long_hidden, long_mask)
    np.testing.assert_allclose(short, [[2.0, 2.0]], atol=1e-6)
    np.testing.assert_allclose(short, long, atol=1e-6)


# ==============================================================================
# 真模型
# ==============================================================================


@pytest.fixture(scope="module")
def real() -> OnnxEmbedder:
    """真的加载一次（约 2 秒 / 350 MB）。取不到权重就跳过（`local_files_only=True`，
    绝不联网）。

    收尾时**真的关一次**：真 session 上的 `close()` 也是要跑的代码路径，放在 fixture
    的收尾里比单开一条测试省一次加载。
    """
    try:
        embedder = OnnxEmbedder(EmbedCfg())
    except EmbedUnavailable as exc:  # pragma: no cover - 取决于本机缓存
        pytest.skip(f"本机没有 {ONNX_REPO} 的 {ONNX_MODEL_FILE}：{exc}")
    yield embedder
    embedder.close()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b)


def test_real_model_returns_unit_vectors_of_the_configured_dim(real: OnnxEmbedder):
    vectors = real.encode_queries(["北京大学的研究生前来报到", "路由器密码怎么改"])
    assert vectors.shape == (2, 768)
    assert vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert real.embed_ver.startswith("embed:1|google/embeddinggemma-2|768|float32|2048|int8")


def test_real_model_truncates_at_max_seq(real: OnnxEmbedder):
    """`max_seq` 真的生效了。

    这一条盯着 `F5-01` 那个意外发现：模型的 `config.json` 把上限（262144）藏在
    `text_config` 里，顶层没有 `max_position_embeddings`，所以 `SentenceTransformer`
    读到的 `max_seq_length` 是 **1e36**——**它不会自己截断**。ONNX 这一支的截断是我们
    自己在 tokenizer 上开的，所以这里直接问 tokenizer 要答案。
    """
    long_text = "这是一段很长的正文。" * 500
    ids = real._tokenizer.encode(long_text).ids
    assert len(ids) == EmbedCfg().max_seq


def test_real_model_is_padding_invariant(real: OnnxEmbedder):
    """同一条文本，无论和谁拼成一批，向量都一样。

    `F5-03` 的 `embed_cache` 整个建立在"同一文本 → 同一向量"上。批内的 padding 是这条
    假设最容易破的地方：一条 15 个字与一条 600 个字的切片拼成一批，前者会被补上几百个
    pad——只要池化漏乘掩码，它的向量就会随**邻居**变化。这里让同一条正文单独跑一次
    （不补 pad）与和一条超长正文拼批跑一次（补很多 pad），两个向量必须逐位相同。
    """
    text = "季度财务报表与预算对比说明"
    alone = real.encode_documents([text])[0]
    crowded = real.encode_documents([text, "另一条完全不同的正文。" * 60, "还有一条"])[0]
    np.testing.assert_allclose(alone, crowded, atol=1e-6)


def test_real_model_semantic_order_is_sane(real: OnnxEmbedder):
    """相关文档的余弦必须**明显**高于无关文档。

    这是"前缀接对了、后端接对了"唯一看得见的证据：前缀写错不会报错，只会让排序变差。
    所以这里断言的不是"能算出向量"，而是**算出来的向量有语义**。

    余量取 0.15，实测是 **0.3085**（0.8639 对 0.5554，`tools/bench_prefix.py` 与这条
    用的是同一对文本，读数在 `PROGRESS.md` §3）。留一半的余量，是因为这条会**跨机器**
    跑：不同 CPU 上的 int8 量化内核可能给出第三位小数不同的结果，但 0.3 的语义差距
    不会因此消失。
    """
    query = real.encode_queries(["怎么修改无线路由器的密码"])[0]
    documents = real.encode_documents(
        [
            "登录路由器管理页面，在无线设置里可以修改 Wi-Fi 密码。",
            "红烧肉的做法：五花肉切块，冷水下锅焯水，加冰糖炒糖色。",
        ]
    )
    good = cosine(query, documents[0])
    bad = cosine(query, documents[1])
    assert good > bad + 0.15, f"相关 {good:.4f} 对 无关 {bad:.4f}"


def test_the_title_slot_actually_changes_the_vector(real: OnnxEmbedder):
    """标题进了前缀：同一条正文带标题与不带标题，向量**不同**。

    这条盯着"标题槽是个占位符"这个理解本身。如果哪天前缀改成不替换，
    这条会红——而那样索引仍然建得起来、搜索仍然出结果，只是标题里的词全部搜不到。
    """
    text = "本次会议确定了下一季度的预算分配方案。"
    with_title = real.encode_documents([text], ["2026 年第一季度财务会议纪要"])[0]
    without = real.encode_documents([text])[0]
    assert cosine(with_title, without) < 0.999


def test_real_model_masks_padding_when_it_pools_itself(real: OnnxEmbedder):
    """强制走 `last_hidden_state` + 我们自己的 mean pool，在**真数据**上验掩码数学。

    真图给了 `sentence_embedding`，所以池化是模型作者做的，我们那条 `_mean_pool` 平时
    走不到——只有这一条（以及 `test_mean_pool_ignores_padding` 那条纯数字的）盯着它。
    不测的话，将来图一换、池化落到我们头上，错的方式是"向量随邻居变化"：索引建得起来、
    搜索出结果，只是**同一个切片每次算出来的向量都不一样**。

    这里直接改 `real._output_name` 而不另开一个 session：省 350 MB 与 2.5 秒，
    并且 `try/finally` 会把它还回去。
    """
    original = real._output_name
    real._output_name = "last_hidden_state"
    try:
        alone = real.encode_documents(["季度财务报表与预算对比说明"])[0]
        crowded = real.encode_documents(
            ["季度财务报表与预算对比说明", "另一条完全不同的正文。" * 60, "还有一条"]
        )[0]
    finally:
        real._output_name = original
    np.testing.assert_allclose(alone, crowded, atol=1e-5)


def test_dim_truncation_keeps_unit_norm():
    """`D5` 的 MRL 截断：512 维，仍然是单位长度（截断后要重新归一化）。"""
    embedder = OnnxEmbedder(EmbedCfg(dim=512))
    try:
        vectors = embedder.encode_queries(["语义检索"])
    finally:
        embedder.close()
    assert vectors.shape == (1, 512)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_close_turns_the_embedder_off():
    """`close()` 之后再用**当场报错**，不是"算出一堆零"——那正是"静默失效"的形态。

    真 session 上的 `close()` 走的是 fixture 的收尾（见 `real`），这里用假 session
    把"关掉之后再用"这条支路走一遍，省一次 350 MB 的加载。
    """
    embedder, _ = _make(_FakeSession(GRAPH_INPUTS, GRAPH_OUTPUTS), _PREFIXES)
    embedder.close()
    embedder.close()
    with pytest.raises(EmbedError, match="close"):
        embedder.encode_queries(["甲"])
