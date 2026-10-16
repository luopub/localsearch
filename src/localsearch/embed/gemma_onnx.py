r"""ONNX Runtime 后端（`F5-01`）。**`D9` 定的生产后端**。

选它的两个数都是 P0 实测的（`PROGRESS.md` §3）：稳态 553.0 对 482.0 tok/s（快 14.7%）、
冷启动 0.50 对 5.89 秒（快 11.8 倍）。冷启动那一项对 `lsearch search` 这种**一次性命令**
比对批处理更要紧——ST 那一支光是把 torch 与 sentence-transformers 导进来就要 10 秒。

------------------------------------------------------------------------------
一、这里**不加载 sentence-transformers**，所以前缀要自己去快照里读
------------------------------------------------------------------------------

`SentenceTransformer` 会顺手把 `prompts` 一并读进来，而 ONNX 这条路只有一个 session 与
一个 tokenizer——**没有那个对象可问**。所以前缀由 `base.prefixes_from_file()` 读快照里的
`config_sentence_transformers.json`。

那个文件在 ONNX 仓库自己的快照里也有（实测与 `google/embeddinggemma-2` 那份**逐字节
相同**），两个后端因此读的是同一份契约，不存在"ONNX 这一支用了别的前缀"这种事。

------------------------------------------------------------------------------
二、多模态那几个输入是**必需的**，要显式喂空张量
------------------------------------------------------------------------------

这个模型是多模态的，导出的图把 `image_features` / `video_features` / `audio_features`
也声明成了**必需输入**（P0 的图 I/O 实测：5 进 2 出）。只喂 `input_ids` + `attention_mask`
会被 ORT 拒掉。纯文本时它们的形状是 `(0, 512)`。

⚠ 那几个"空输入"的形状与类型**从 session 自己声明的元数据里推**，绝不写死：

* 类型从 `tensor(float)` 这类字符串解出来；
* 形状里**符号维（动态维）一律当 0**——纯文本时每一个非文本输入的 batch 维都该是 0，
  这正是"这条样本没有图/没有音"的表达方式。

万一将来某个输入的名字与语义都变了，而它又不是可空的多模态槽，ORT 会**点名**那个输入
报错——这是我们要的失败方式（出声），比猜一个形状喂进去、算出一堆看着正常的向量好。

------------------------------------------------------------------------------
三、`[embed] quantize` 与这个 onnx 图**不是**同一件事
------------------------------------------------------------------------------

两个 int8 很容易被看成同一个，它们不是：

* `[embed] quantize = "int8"` 说的是**向量落盘**的量化（`F5-03` / `F5-04`），
  所以它进 `embed_ver`（不同的落盘表示就是不同的缓存内容）；
* 本模块的 `ONNX_MODEL_FILE` 说的是**编码器这张图**本身是 int8 量化的，
  它是 `D9` 选型的产物，与落盘无关，也**不随 `[embed] quantize` 变**。

把它们接起来（`quantize = "none"` 就去换一张 fp32 的图）会得到两个反直觉的后果：换落盘
格式会连带换编码器，而本机缓存里**没有** fp32 的 ONNX 图（只有 `model_quantized.onnx`），
于是 `quantize = "none"` 会当场变成一个下载需求——而离线是本项目的硬约束。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import (
    PREFIX_CONFIG_FILE,
    BatchEmbedder,
    EmbedUnavailable,
    TaskPrefixes,
    prefixes_from_file,
)

__all__ = [
    "ALLOW_PATTERNS",
    "ONNX_MODEL_FILE",
    "ONNX_REPO",
    "OnnxEmbedder",
    "empty_input_for",
]

#: 这份 ONNX 导出所在的位置。与 `[embed] model_id`（= `google/embeddinggemma-2`）
#: **是两个仓库**：前者是权重与 tokenizer 的来源，后者是同一批权重的 ONNX 导出。
ONNX_REPO = "onnx-community/embeddinggemma-2-ONNX"

#: 编码器图。int8 量化那一份（`D9`）——理由见模块文档第三节。
ONNX_MODEL_FILE = "onnx/model_quantized.onnx"

#: 拉这份导出时要哪些文件。**必须收窄**：仓库的 `allow_patterns=None` 会把 fp32 的
#: `model.onnx` 等好几份图一起拖下来（数 GB），而离线运行只需要 int8 那一份。
#: `.onnx_data` 是外置权重文件（实测 313 MB），**漏了它图加载不起来**。
ALLOW_PATTERNS = (
    ONNX_MODEL_FILE,
    f"{ONNX_MODEL_FILE}_data",
    PREFIX_CONFIG_FILE,
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "preprocessor_config.json",
    "processor_config.json",
)

#: ORT 元数据里 `type` 字段 → numpy 类型。认不出的类型当场报错（见 `empty_input_for`）。
_DTYPE_BY_ONNX_TYPE: dict[str, Any] = {
    "tensor(float)": np.float32,
    "tensor(double)": np.float64,
    "tensor(float16)": np.float16,
    "tensor(int64)": np.int64,
    "tensor(int32)": np.int32,
    "tensor(bool)": np.bool_,
}

#: 这三个名字是 tokenizer 的产物，必须按名字喂进去。
_IDS_INPUT = "input_ids"
_MASK_INPUT = "attention_mask"
_TOKEN_TYPE_INPUT = "token_type_ids"

#: 输出优先取这个（图里已经有池化+归一化之前的池化结果）。
_EMBEDDING_OUTPUT = "sentence_embedding"
_HIDDEN_OUTPUT = "last_hidden_state"


def empty_input_for(meta: Any) -> np.ndarray:
    """按 session 声明的元数据造一个**空**输入张量（纯文本时喂给多模态槽）。

    符号维 / 未知维 → 0。理由见模块文档第二节。
    """
    type_str = str(getattr(meta, "type", ""))
    if type_str not in _DTYPE_BY_ONNX_TYPE:
        raise EmbedUnavailable(
            f"输入 {meta.name!r} 的类型 {type_str!r} 不在已知表里"
            f"（已知：{', '.join(sorted(_DTYPE_BY_ONNX_TYPE))}）。"
            "这一支只喂文本，别的一律喂空张量——类型解不出来就没法造空张量。"
        )
    shape: list[int] = []
    for dim in getattr(meta, "shape", None) or []:
        shape.append(dim if isinstance(dim, int) and dim >= 0 else 0)
    if not shape:
        # 没声明形状的输入给一个**零长度一维数组**，而不是标量：`np.zeros(())` 是一个
        # 值为 0 的标量（size 1），那是在说"这里有一个元素，它是 0"，与"这里没有数据"
        # 正好相反——而后者才是纯文本要表达的意思。
        shape = [0]
    return np.zeros(tuple(shape), dtype=_DTYPE_BY_ONNX_TYPE[type_str])


class OnnxEmbedder(BatchEmbedder):
    """ONNX Runtime + int8 图。`_forward` 之外的活全在 `BatchEmbedder` 里。

    `session` / `tokenizer` 两个口子是给测试的：给了就**完全不碰** huggingface_hub、
    onnxruntime 与 `tokenizers`，于是"喂进去的到底是什么"可以在没有模型、甚至没装
    onnxruntime 的进程里断言。`test_embed_onnx.py` 里的假 session 走的就是这条路。
    """

    name = "onnx"

    def __init__(
        self,
        cfg: EmbedCfg,
        *,
        prefixes: TaskPrefixes | None = None,
        model_dir: str | Path | None = None,
        session: Any | None = None,
        tokenizer: Any | None = None,
    ) -> None:
        if (session is None) != (tokenizer is None):
            raise EmbedUnavailable(
                "session 与 tokenizer 要么都给、要么都不给：只给一个的话，另一个的来源"
                "就成了「看你从哪儿调的」——测试里给假的那一半、真的那一半还去读模型文件，"
                "得到的东西没法解释。"
            )
        if session is not None:
            if prefixes is None:
                raise EmbedUnavailable("注入了 session 就必须同时给出 prefixes：没有模型文件可读")
            self._model_dir: Path | None = None
        else:
            self._model_dir = _resolve_snapshot(model_dir)
            if prefixes is None:
                prefixes = prefixes_from_file(self._model_dir / PREFIX_CONFIG_FILE)
        super().__init__(cfg, prefixes)
        self._session = session if session is not None else _load_session(self._model_dir, cfg)
        self._tokenizer = (
            tokenizer if tokenizer is not None else _load_tokenizer(self._model_dir, cfg)
        )
        self._inputs = list(self._session.get_inputs())
        names = {meta.name for meta in self._inputs}
        for required in (_IDS_INPUT, _MASK_INPUT):
            if required not in names:
                raise EmbedUnavailable(
                    f"这张 ONNX 图的输入里没有 {required!r}（图声明的是："
                    f"{', '.join(sorted(names))}）。这一支只做文本嵌入，"
                    "拿不到 tokenizer 的两个标准输入就没法喂。"
                )
        self._output_name = _pick_output(self._session)

    # ------------------------------------------------------------------ 后端

    def _forward(self, texts: list[str]) -> np.ndarray:
        tokenizers = self._tokenizer
        encodings = tokenizers.encode_batch([str(t) for t in texts])
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        outputs = self._session.run([self._output_name], self._build_feed(ids, mask))
        raw = np.asarray(outputs[0])
        if self._output_name == _HIDDEN_OUTPUT:
            raw = _mean_pool(raw, mask)
        return raw

    def _build_feed(self, ids: np.ndarray, mask: np.ndarray) -> dict[str, np.ndarray]:
        """按 session **自己声明的**输入清单拼 feed——一个名字都不多、一个都不少。"""
        feed: dict[str, np.ndarray] = {}
        for meta in self._inputs:
            if meta.name == _IDS_INPUT:
                feed[meta.name] = ids
            elif meta.name == _MASK_INPUT:
                feed[meta.name] = mask
            elif meta.name == _TOKEN_TYPE_INPUT:
                # 这个模型不用句对，但别的导出可能有；全 0 是标准答案。
                feed[meta.name] = np.zeros_like(ids)
            else:
                feed[meta.name] = empty_input_for(meta)
        return feed

    def close(self) -> None:
        """ORT 的 `InferenceSession` 没有显式的 close（对象被回收时释放）。这里只是
        把 `BatchEmbedder._closed` 立起来，让 close 之后的调用**报错而不是继续算**。"""
        self._session = None
        self._tokenizer = None
        super().close()


# ==============================================================================
# 加载：所有需要 onnxruntime / huggingface_hub / tokenizers 的活都在这里
# ==============================================================================


def _resolve_snapshot(model_dir: str | Path | None) -> Path:
    """找到 ONNX 快照的本地目录。**只在本地找**（`local_files_only=True`）。

    离线是硬约束：`HF_HUB_OFFLINE=1` 由 `bootstrap_env()` 立起来，但本模块**不假设**
    调用方调过它——直接把 `local_files_only` 写在参数上，这样无论环境变量如何，
    这里的解析都只查缓存。首次下载是 `tools/fetch_model.py` 的事（它显式关掉离线）。
    """
    if model_dir is not None:
        path = Path(model_dir)
        if not path.is_dir():
            raise EmbedUnavailable(f"model_dir 不是一个目录：{path}")
        return path
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise EmbedUnavailable(f"没装 huggingface_hub：{exc}") from exc
    try:
        return Path(
            snapshot_download(
                ONNX_REPO, allow_patterns=list(ALLOW_PATTERNS), local_files_only=True
            )
        )
    except Exception as exc:
        raise EmbedUnavailable(
            f"ONNX 权重不在本机缓存里（{ONNX_REPO}，只要 {ONNX_MODEL_FILE} 那几件）。"
            f"离线运行不会去下载。原始错误：{exc}。"
            "跑一次 `uv run python tools/fetch_model.py` 可以把它取下来。"
        ) from exc


def _load_session(model_dir: Path, cfg: EmbedCfg) -> Any:
    """造 ORT session。线程数口径与 P0 基准一致：`intra_op` = `[embed] threads`、
    `inter_op` = 1（这一层没有可并行的算子图，开多了只是多几个线程抢同一批核）。"""
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise EmbedUnavailable(
            f"没装 onnxruntime：{exc}。`[embed] backend = \"onnx\"` 需要它，"
            "或者把 backend 改成 \"sentence_transformers\"。"
        ) from exc
    path = model_dir / ONNX_MODEL_FILE
    if not path.is_file():
        raise EmbedUnavailable(
            f"快照里没有 {path}。这个仓库的图不止一份，我们只要 int8 那一份"
            "（ALLOW_PATTERNS）——缺件通常是缓存被清理过。"
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = cfg.threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    # 3 = 只报 error：ORT 默认会把每个算子的落点打在 stderr 上，几百行。
    options.log_severity_level = 3
    try:
        return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise EmbedUnavailable(f"加载 {path} 失败：{exc}") from exc


def _load_tokenizer(model_dir: Path, cfg: EmbedCfg) -> Any:
    """从快照的 `tokenizer.json` 造一个 tokenizer，**补齐 padding 与截断**。

    ⚠ `max_length` 必须显式设：模型的 `config.json` 把上限藏在 `text_config` 里
    （262144），顶层没有 `max_position_embeddings`，所以 `SentenceTransformer` 读到的
    `max_seq_length` 是 **1e36**——**它不会自己截断**。不设的话一条超长切片会被完整
    喂进模型，慢得没边，而且那一段的向量也没什么意义。

    pad token 从 tokenizer 自己身上取（`"<pad>"`），不写死 id：写死一个 id 在换
    tokenizer 时会**静默**把别的 token 当 padding，注意力掩码随之全错。
    """
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise EmbedUnavailable(f"没装 tokenizers（transformers 的依赖）：{exc}") from exc
    path = model_dir / "tokenizer.json"
    if not path.is_file():
        raise EmbedUnavailable(f"快照里没有 {path}")
    tokenizer = Tokenizer.from_file(str(path))
    pad_token = "<pad>"
    pad_id = tokenizer.token_to_id(pad_token)
    if pad_id is None:
        raise EmbedUnavailable(
            f"{path} 里没有 {pad_token!r} 这个词表项。padding 需要一个确定的 pad id——"
            "用 0 顶替会把它当成一个真 token 参与注意力。"
        )
    tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)
    tokenizer.enable_truncation(max_length=cfg.max_seq)
    return tokenizer


def _pick_output(session: Any) -> str:
    """取 `sentence_embedding`，没有就退回 `last_hidden_state`（自己池化）。

    优先前者：图里的池化是模型作者实现的那一份，我们自己 mean pool 只是"等价实现"，
    能不用就不用。
    """
    names = {meta.name for meta in session.get_outputs()}
    if _EMBEDDING_OUTPUT in names:
        return _EMBEDDING_OUTPUT
    if _HIDDEN_OUTPUT in names:
        return _HIDDEN_OUTPUT
    raise EmbedUnavailable(
        f"这张 ONNX 图的输出里既没有 {_EMBEDDING_OUTPUT!r} 也没有 {_HIDDEN_OUTPUT!r}"
        f"（图声明的是：{', '.join(sorted(names))}）"
    )


def _mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按注意力掩码做 mean pooling（只在图不给 `sentence_embedding` 时走这条）。

    ⚠ 一定要乘掩码：padding 那些位置的 hidden state 不是 0（是 pad token 过完模型的结果），
    直接 `mean(axis=1)` 会让向量随**批里最长的那条**变化——同一条文本和不同的邻居拼批，
    得到不同向量，而缓存（`F5-03`）恰恰假设"同一文本 → 同一向量"。
    """
    weights = mask[..., None].astype(np.float32)
    total = (hidden.astype(np.float32) * weights).sum(axis=1)
    count = np.maximum(weights.sum(axis=1), 1.0)
    return total / count
