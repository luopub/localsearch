r"""`embed/gemma_st.py`：`sentence-transformers` + PyTorch CPU 后端（`F5-01`）。

------------------------------------------------------------------------------
这一组为什么这样切
------------------------------------------------------------------------------

1. **假 model**——只验"传进去的参数对不对"。三处核对（dtype、`max_seq_length`、
   `default_prompt_name`）都是**写错了不报错**的那一类，它们的失败形态是"向量变差"
   或"慢 6 倍"，所以核对本身必须真的执行过。假 model 让这一段不必等 `import torch`
   的 2.4 秒与 `sentence_transformers` 的 7.6 秒。

2. **真 model**——模块级 fixture 加载**一次**（实测约 20 秒、常驻约 3 GB），后面几条
   共用。这里验的是那些**只有在真模型上才存在**的东西：
   `max_seq_length` 到底被读成了什么（`F5-01` 实测默认是 **1e36**，见下）、权重实际
   加载成什么 dtype、以及向量有没有语义。

3. **两路对照**（`test_embed_backends_agree`）**刻意放在这个文件里**：它需要两个后端
   同时在场，而 ST 模型在这个模块里已经加载好了，ONNX 那一份只要再加 2.5 秒。
   放在别处就要么再加载一次 ST（+20 秒），要么写成两个进程各算一半（那就不是一条
   测试了）。它钉住的是 `embed_ver` 里**不放 `backend`** 那个决定（`base.py` 第五节）。
"""

from __future__ import annotations

import numpy as np
import pytest

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import EmbedError, EmbedUnavailable, TaskPrefixes
from localsearch.embed.gemma_onnx import ONNX_MODEL_FILE, ONNX_REPO
from localsearch.embed.gemma_st import GemmaSTEmbedder

QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "

_PREFIXES = TaskPrefixes(query=QUERY_PREFIX, document=DOCUMENT_PREFIX)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b)


@pytest.fixture
def prefixes() -> TaskPrefixes:
    return _PREFIXES


# ==============================================================================
# 假 model：只验传进去的参数
# ==============================================================================


class _FakeSTModel:
    """只需要 `encode()`。`parameters()` / `prompts` 是**真**加载路径才用的，
    这条路上不该被碰到——碰到了就说明"注入了 model"这个口子漏了。"""

    def __init__(self, width: int = 8):
        self.width = width
        self.seen: list[str] | None = None
        self.kwargs: dict | None = None

    def encode(self, texts, **kwargs):
        self.seen = list(texts)
        self.kwargs = kwargs
        rows = len(self.seen)
        return np.arange(1, rows * self.width + 1, dtype=np.float32).reshape(rows, self.width)

    def __getattr__(self, name):  # pragma: no cover - 只在路径漏了的时候响
        raise AssertionError(f"假 model 不该被问 {name!r}——这条路径只该调 encode()")


@pytest.fixture
def fake() -> _FakeSTModel:
    return _FakeSTModel()


def test_encode_is_called_with_the_prefixed_texts(fake: _FakeSTModel, prefixes: TaskPrefixes):
    embedder = GemmaSTEmbedder(EmbedCfg(dim=8, batch_size=4), prefixes=prefixes, model=fake)
    embedder.encode_documents(["正文一", "正文二"], ["标题一", None])
    assert fake.seen == ["title: 标题一 | text: 正文一", "title: none | text: 正文二"]
    assert fake.kwargs["batch_size"] == 4


def test_normalization_is_left_to_the_base_class(fake: _FakeSTModel, prefixes: TaskPrefixes):
    """`normalize_embeddings=False` 是**必须**的：归一化要发生在按 `dim` 截断**之后**
    （`D5` 的 MRL）。交给 `encode()` 先归一化，截断出来的向量长度就小于 1——而余弦
    照样算得出来，只是错的。
    """
    embedder = GemmaSTEmbedder(EmbedCfg(dim=8), prefixes=prefixes, model=fake)
    embedder.encode_queries(["甲"])
    assert fake.kwargs["normalize_embeddings"] is False
    assert fake.kwargs["convert_to_numpy"] is True
    assert fake.kwargs["show_progress_bar"] is False


def test_an_injected_model_needs_prefixes(fake: _FakeSTModel):
    """假 model 没有 `prompts` 可读——不许"半个真、半个假"。"""
    with pytest.raises(EmbedUnavailable, match="prefixes"):
        GemmaSTEmbedder(EmbedCfg(), model=fake)


def test_float16_is_refused_before_touching_torch():
    """`R6`：fp16 在 CPU 上产出 NaN。`core/config.py` 的枚举已经拦过一道，这里是第二道
    ——它必须在**导入 torch 之前**响，否则报错要等 2.4 秒。

    `EmbedCfg` 是直接构造的（绕开了配置校验），正是为了走这一道。
    """
    with pytest.raises(EmbedUnavailable, match="float16"):
        GemmaSTEmbedder(EmbedCfg(dtype="float16"))


# ==============================================================================
# 真 model（模块级加载一次）
# ==============================================================================


@pytest.fixture(scope="module")
def real() -> GemmaSTEmbedder:
    """真的加载一次。取不到权重就跳过（`local_files_only=True`，绝不联网）。"""
    try:
        embedder = GemmaSTEmbedder(EmbedCfg())
    except EmbedUnavailable as exc:  # pragma: no cover - 取决于本机缓存
        pytest.skip(f"本机没有 {EmbedCfg().model_id} 的权重：{exc}")
    yield embedder
    embedder.close()


def test_real_prefixes_come_from_the_model(real: GemmaSTEmbedder):
    """`sentence-transformers` 自己读进来的 `prompts` 就是 `M3` 的那一对。"""
    assert real.prefixes.query == QUERY_PREFIX
    assert real.prefixes.document == DOCUMENT_PREFIX


def test_real_weights_are_float32(real: GemmaSTEmbedder):
    """`M2`：本机是 Zen 2、没有 bf16 硬件，bfloat16 会慢 **6.21 倍且不报错**。

    这一条盯的是"加载参数被默默忽略"那个失败形态：`torch_dtype` / `dtype` 这个关键字
    的写法在 transformers 的大版本之间改过名，写错的名字会被当成未知关键字吃掉。
    """
    import torch

    assert next(real._model.parameters()).dtype == torch.float32


def test_real_max_seq_length_is_not_1e36(real: GemmaSTEmbedder):
    """**这一条是 `F5-01` 那个意外发现的回归测试。**

    `sentence-transformers` 从模型**顶层**的 `max_position_embeddings` 取
    `max_seq_length`，而这个模型的 `config.json` 顶层没有它（真值 262144 藏在
    `text_config` 里），于是读出来是 **1e36**——**它永远不会自己截断**。
    不显式设的话，一条超长切片会被完整喂进模型：慢、吃内存，而那一段的向量是把几十页
    正文混在一起的**平均**，语义上也没什么用。

    两个位置都要看：`encode()` 用的是 `SentenceTransformer.max_seq_length`，
    而真正切 token 的是底层 tokenizer 的 `model_max_length`。
    """
    assert real._model.max_seq_length == EmbedCfg().max_seq
    assert real._model.max_seq_length < 1e6, "又读回 1e36 了"
    assert real._model.tokenizer.model_max_length == EmbedCfg().max_seq


def test_real_vectors_are_unit_vectors_of_the_native_dim(real: GemmaSTEmbedder):
    vectors = real.encode_queries(["北京大学的研究生前来报到", "路由器密码怎么改"])
    assert vectors.shape == (2, 768)
    assert vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_real_model_semantic_order_is_sane(real: GemmaSTEmbedder):
    """相关文档明显高于无关文档。实测余量 0.3085（0.8639 对 0.5554），断言留 0.15。"""
    query = real.encode_queries(["怎么修改无线路由器的密码"])[0]
    documents = real.encode_documents(
        [
            "登录路由器管理页面，在无线设置里可以修改 Wi-Fi 密码。",
            "红烧肉的做法：五花肉切块，冷水下锅焯水，加冰糖炒糖色。",
        ]
    )
    good, bad = cosine(query, documents[0]), cosine(query, documents[1])
    assert good > bad + 0.15, f"相关 {good:.4f} 对 无关 {bad:.4f}"


def test_real_title_slot_changes_the_vector(real: GemmaSTEmbedder):
    """标题进了前缀：同一条正文带标题与不带标题，向量不同。"""
    text = "本次会议确定了下一季度的预算分配方案。"
    with_title = real.encode_documents([text], ["2026 年第一季度财务会议纪要"])[0]
    without = real.encode_documents([text])[0]
    assert cosine(with_title, without) < 0.999


def test_real_model_is_padding_invariant(real: GemmaSTEmbedder):
    """同一条文本与一条超长文本拼批（补很多 pad）后，向量必须逐位相同。

    `F5-03` 的 `embed_cache` 建立在"同一文本 → 同一向量"上，而批内 padding 是这条
    假设最容易被破坏的地方。
    """
    text = "季度财务报表与预算对比说明"
    alone = real.encode_documents([text])[0]
    crowded = real.encode_documents([text, "另一条完全不同的正文。" * 60, "还有一条"])[0]
    np.testing.assert_allclose(alone, crowded, atol=1e-6)


# ==============================================================================
# 两路对照：`embed_ver` 里不放 `backend` 的判据
# ==============================================================================


def test_embed_backends_agree(real: GemmaSTEmbedder):
    """同一个模型的两条实现路径，余弦必须 ≥ 0.999。

    **这条测试是在钉住一个决定**：`embed_ver` 里没有 `backend`（`base.py` 第五节），
    凭据就是这条读数。实测最低 **0.999935**、平均 **0.999955**（`tools/bench_prefix.py`
    的 6 条语料，读数在 `PROGRESS.md` §3）——差异在 float32 抖动与 int8 量化同一个
    量级上，所以换后端不该触发全库重嵌（按 `A1` 的两万文件基准集估算是数小时到一天）。

    阈值取 0.999：比实测低一点点，容得下不同机器的 int8 内核；但**远高于**"两条前缀
    写错"或"dtype 掉进 bfloat16"会造成的偏差——那些都在 1e-2 这一档。

    对应地，`test_embed_base.py::test_embed_ver_ignores_knobs_that_only_change_the_speed`
    钉住的是**指纹里没有 backend 这个事实**；这一条钉住的是**为什么可以没有**。
    """
    from localsearch.embed.gemma_onnx import OnnxEmbedder

    try:
        other = OnnxEmbedder(EmbedCfg())
    except EmbedUnavailable as exc:  # pragma: no cover - 取决于本机缓存
        pytest.skip(f"本机没有 {ONNX_REPO} 的 {ONNX_MODEL_FILE}：{exc}")

    query = "怎么修改无线路由器的密码"
    documents = [
        "登录路由器管理页面，在无线设置里可以修改 Wi-Fi 密码。",
        "红烧肉的做法：五花肉切块，冷水下锅焯水，加冰糖炒糖色。",
    ]
    try:
        pairs = [
            (real.encode_queries([query])[0], other.encode_queries([query])[0]),
            *zip(real.encode_documents(documents), other.encode_documents(documents)),
        ]
    finally:
        other.close()

    scores = [cosine(a, b) for a, b in pairs]
    assert min(scores) >= 0.999, f"两路余弦 {scores}"
