r"""嵌入层（P5 起，`F5-01` 与 `F5-02`）。

* `base.py`       公共那一半：任务前缀、批处理、向量后处置、`Embedder` 契约与工厂
* `gemma_st.py`   `sentence-transformers` + PyTorch CPU 后端
* `gemma_onnx.py` ONNX Runtime + int8 量化后端（**`D9` 定的生产后端**）
* `batching.py`   多进程调度与 OOM 退避：**开几个嵌入进程由内存算出来**（`A2`）

计划 §4.3 那棵树里本模块只有 `base.py` 与 `gemma_st.py` 两个文件（外加 `batching.py`），
ONNX 那一支是 `D9` 之后才定下来的，落地时补成 `gemma_onnx.py`。

两层的关系是"一个进程里"与"几个进程"：`base.py` 管前者（前缀、分批、后处置），
`batching.py` 管后者（分给谁、放不下怎么办）。`[perf] workers = 0`（缺省）时由后者
按内存算，本机当前算出 1、走"就地跑"那条路——所以 `EmbedPool` 在单进程情形下**仍是**
这一层的入口，调用方不必分两条路。

两个后端**算的是同一个函数**：同一条文本的两路向量余弦实测 0.9999（读数在 `PROGRESS.md`
§3 的 `F5-01` 那一组）。所以 `embed_ver` 里**不含后端名**——理由写在
`base.embed_ver()` 的文档里。
"""
