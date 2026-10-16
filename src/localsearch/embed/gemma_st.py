r"""`sentence-transformers` + PyTorch CPU 后端（`F5-01`）。**不是**生产后端（`D9` 选了 ONNX）。

留着它的理由有两个，都不是"备用"：`P0` 的两路对照基准是它给的（两路向量余弦 0.9999
那条读数），而且 `F5-01` 的测试里 ONNX 那一路需要一个**独立实现**来对照——自己对自己
的测试证不了什么。

------------------------------------------------------------------------------
一、dtype 必须显式写 float32，而且要**验**一遍
------------------------------------------------------------------------------

`M2` 实测（二手信息，P0 已在本机复核）：本机是 Zen 2、没有 bf16 硬件，模型默认的
bfloat16 会掉进一条慢 **6.21 倍**的路径（71.4 对 442.9 tok/s），**且不报错**。
所以：

* 加载时 `model_kwargs={"dtype": torch.float32}` 显式指定；
* 加载完**读回** `next(model.parameters()).dtype` 核对一遍。

第二条不是多余的：`torch_dtype` / `dtype` 这个关键字在 transformers 的几个大版本之间
改过名，写错的老名字会被**当成未知的关键字默默忽略**，于是模型按它 config 里的
bfloat16 加载，一切正常、只是慢 6 倍。核对一次就能把这类静默失效变成一次当场失败。

`float16` 在这里再拦一道（`core/config.py` 的枚举已经拦过）：`R6`——fp16 在 CPU 上溢出
产出 NaN，而 NaN 往下走会变成"这个切片永远搜不到"，一句话不说。

------------------------------------------------------------------------------
二、`max_seq_length` 必须显式设：默认值是 **1e36**
------------------------------------------------------------------------------

`sentence-transformers` 从模型的**顶层** `max_position_embeddings` 取这个数，而这个
模型的 `config.json` 顶层没有它（真正的值 262144 藏在 `text_config` 里）。于是它读出来
是 1e36——**它永远不会自己截断**。

P0 已在真模型上确认过这一点（`PROGRESS.md` §3）。所以这里显式设 `cfg.max_seq`
（默认 2048，模型自己的推荐上限）。不设的话，一条超长切片会被完整喂进模型：慢、
吃内存，而那一段的向量是把几十页正文混在一起的**平均**，语义上也没什么用。

------------------------------------------------------------------------------
三、`default_prompt_name` 必须是 None，否则前缀会被加两遍
------------------------------------------------------------------------------

本项目的做法是**自己拼前缀**（`BatchEmbedder.encode_queries` / `encode_documents`），
因为文档侧要按标题替换标题槽——那是 `SentenceTransformer` 的 `prompt=` 表达不了的。
如果模型的配置里又设了 `default_prompt_name`，`encode()` 还会再加一遍默认前缀，
拼出来是 `task: search result | query: task: search result | query: ...`。

它**不报错**，只是让向量变差。所以加载时核对一次：不是 `None` 就当场失败
（这个模型的 `config_sentence_transformers.json` 里 `default_prompt_name` 就是 null，
所以这条核对在正常路径上永远不响，响的时候一定是配置真的变了）。

------------------------------------------------------------------------------
四、`torch.set_num_threads()` 是**进程级**的
------------------------------------------------------------------------------

它改的是整个进程的线程池，不只是这个模型。v1 里 PyTorch 只有这一个用户，所以没关系；
将来真出现第二个用户（比如 cross-encoder 重排，计划里 v1 不做），要把它挪到
"谁用谁设"的地方去。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import (
    BatchEmbedder,
    EmbedUnavailable,
    TaskPrefixes,
    prefixes_from_prompts,
)

__all__ = ["GemmaSTEmbedder"]

#: `[embed] dtype` → torch 的 dtype 名。值是**字符串**而不是 `torch.float32`：
#: 这个模块的常量不许在导入时就把 torch 拖进来（见 `base.create_embedder` 的文档）。
_TORCH_DTYPE_BY_NAME = {
    "float32": "float32",
    "bfloat16": "bfloat16",
}


class GemmaSTEmbedder(BatchEmbedder):
    """PyTorch CPU 上的 `google/embeddinggemma-2`。

    `model` 这个口子是给测试的：给了就完全不碰 `sentence_transformers`（也不必等它
    7.6 秒的导入）。假 model 只需要有 `encode()` / `parameters()` / `prompts` /
    `max_seq_length` / `default_prompt_name` 这几样。
    """

    name = "sentence_transformers"

    def __init__(
        self,
        cfg: EmbedCfg,
        *,
        prefixes: TaskPrefixes | None = None,
        model_dir: str | Path | None = None,
        model: Any | None = None,
    ) -> None:
        if model is None:
            model, detected_prefixes = _load(cfg, model_dir)
            if prefixes is None:
                prefixes = detected_prefixes
        if prefixes is None:
            raise EmbedUnavailable(
                "注入了 model 就必须同时给出 prefixes：假 model 没有 prompts 可读。"
            )
        super().__init__(cfg, prefixes)
        self._model = model

    def _forward(self, texts: list[str]) -> np.ndarray:
        # `normalize_embeddings=False`：归一化由 `BatchEmbedder` 统一做，而且要**在**
        # 按 `dim` 截断之后（MRL）。这里先归一化的话，截断出来的向量长度就小于 1 了。
        # 前缀已经拼好了，所以**不加** prompt（模块文档第三节）。
        vectors = self._model.encode(
            list(texts),
            batch_size=self.cfg.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=False,
        )
        return np.asarray(vectors)

    def close(self) -> None:
        """把模型引用放掉。torch 占的内存不保证立刻还给操作系统（Python 的分配器与
        torch 自己的缓存都留着），所以这不是"释放内存"，是"不再持有"——真要看内存
        回落，得让**进程**退出（`F5-02` 的多进程调度正是这么做的）。"""
        self._model = None
        super().close()


def _load(cfg: EmbedCfg, model_dir: str | Path | None) -> tuple[Any, TaskPrefixes]:
    """加载模型，返回 `(模型, 前缀)`。所有与 torch / sentence-transformers 的接触都在这里。"""
    dtype_name = _TORCH_DTYPE_BY_NAME.get(cfg.dtype)
    if dtype_name is None:
        raise EmbedUnavailable(
            f"[embed] dtype = {cfg.dtype!r} 不是这个后端支持的值（只有 "
            f"{', '.join(sorted(_TORCH_DTYPE_BY_NAME))}）。float16 是硬性禁止的：R6，"
            "fp16 在 CPU 上产出 NaN。"
        )
    try:
        import torch  # noqa: PLC0415 - 见 base.create_embedder 的文档（延迟导入）
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbedUnavailable(
            f"没装 {exc.name}。`[embed] backend = \"sentence_transformers\"` 需要 torch 与 "
            "sentence-transformers，或者把 backend 改成 \"onnx\"（D9 的生产后端）。"
        ) from exc

    # 进程级设置，理由见模块文档第四节。**放在加载之前**：加载本身（safetensors 反序列化）
    # 也会用到线程池。
    torch.set_num_threads(cfg.threads)
    want_dtype = getattr(torch, dtype_name)
    target = str(model_dir) if model_dir is not None else cfg.model_id
    try:
        model = SentenceTransformer(
            target,
            device="cpu",
            model_kwargs={"dtype": want_dtype},
            # 只查本地缓存：`HF_HUB_OFFLINE` 由 `bootstrap_env()` 立起，但本模块不假设
            # 调用方调过它。不写这一句时，缓存缺件会去**联网**——实测过一次挂到超时
            # （600 秒后被杀，退出码 143）。
            local_files_only=True,
        )
    except Exception as exc:
        raise EmbedUnavailable(
            f"加载 {target} 失败。离线运行（local_files_only=True）只在本地缓存里找，"
            f"不会去下载。原始错误：{exc}。"
            "跑一次 `uv run python tools/fetch_model.py` 可以取权重。"
        ) from exc

    # --- 三处核对：写错了都不会报错，只会静默变差（模块文档第一、二、三节）---
    actual = next(model.parameters()).dtype
    if actual != want_dtype:
        raise EmbedUnavailable(
            f"模型参数的实际 dtype 是 {actual}，要的是 {want_dtype}。"
            "这个关键字的写法在 transformers 的大版本之间改过名，写错的老名字会被"
            "**默默忽略**——那样模型会按 config 里的 bfloat16 加载，本机（Zen 2，"
            "无 bf16 硬件）会慢 6.21 倍且不报错（M2）。"
        )
    if model.default_prompt_name is not None:
        raise EmbedUnavailable(
            f"模型配置里设了 default_prompt_name = {model.default_prompt_name!r}。"
            "本项目自己拼前缀（文档侧要按标题替换标题槽），默认前缀会**再拼一遍**，"
            "拼出来的东西不报错、只是更差。"
        )
    # ⚠ 必须在加载之后设：它是 `SentenceTransformer` 的属性，加载时按 config 算出来是 1e36
    # （模块文档第二节）。
    model.max_seq_length = cfg.max_seq

    return model, prefixes_from_prompts(model.prompts, source=f"{target} 的 prompts")
