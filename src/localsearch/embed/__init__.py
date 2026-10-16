r"""嵌入层（P5 起，`F5-01`）。

* `base.py`       公共那一半：任务前缀、批处理、向量后处置、`Embedder` 契约与工厂
* `gemma_st.py`   `sentence-transformers` + PyTorch CPU 后端
* `gemma_onnx.py` ONNX Runtime + int8 量化后端（**`D9` 定的生产后端**）

计划 §4.3 那棵树里本模块只有 `base.py` 与 `gemma_st.py` 两个文件（外加 `batching.py`），
ONNX 那一支是 `D9` 之后才定下来的，落地时补成 `gemma_onnx.py`。`batching.py`（多进程
调度、OOM 退避）是 `F5-02` 的，**不在这一轮**——本模块现在只有"一个进程里怎么分批"。

两个后端**算的是同一个函数**：同一条文本的两路向量余弦实测 0.9999（读数在 `PROGRESS.md`
§3 的 `F5-01` 那一组）。所以 `embed_ver` 里**不含后端名**——理由写在
`base.embed_ver()` 的文档里。
"""
