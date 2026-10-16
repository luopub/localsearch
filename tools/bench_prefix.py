r"""量 `F5-01` 的三件事，读数落进 `docs/PROGRESS.md` §3。

    uv run python tools/bench_prefix.py            # 两个后端都量
    uv run python tools/bench_prefix.py --backend onnx

量的是**前缀**这件事本身，不是吞吐（吞吐是 `tools/bench_embed.py`）：

1. **前缀有没有用**——同一个查询与"相关/无关"两组文档的余弦差。差距够大，说明前缀
   把模型带进了检索任务；差距很小，说明它没起作用（而**搜索照样出结果**）。
2. **写错会怎样**——把查询与文档的前缀**互换**、以及**完全不加**前缀，各量一遍。
   `M3` 的原话是"写错不会报错，只会静默降低召回"，这一栏就是那句话的实证：
   同一个语料、同一个模型，只因前缀不同，相关文档的得分可以掉到与无关文档同一档。
3. **两个后端一致到什么程度**——这是 `embed_ver` 里**放不放 `backend`** 的判据
   （`embed/base.py` 第五节）。差异在 float32 抖动量级上就不放，否则要放。

语料是脚本里写死的 5 条中文，确定性、不依赖盘上任何文件。用的是**产品代码**
（`localsearch.embed`）而不是另写一份推理——量出来的数才是这个工具的数。
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from localsearch.core.config import EmbedCfg
from localsearch.core.console import use_safe_output
from localsearch.embed.base import create_embedder, postprocess_vectors

#: 一个查询 + 2 条相关 + 4 条无关（其中两条与查询**同一个大主题**——网络与电脑），
#: 写死是为了可复跑。
#:
#: 第一版只有 4 条、主题各不相干，量出来的差是 **+0.010**（正确前缀 0.2970 对不加前缀
#: 0.2870）：难题太简单，加不加前缀都能分开。所以加了同主题的干扰项。
QUERY = "怎么修改无线路由器的密码"
DOCUMENTS = [
    "登录路由器管理页面，在无线设置里可以修改 Wi-Fi 密码。",
    "忘记无线密码时，长按路由器背后的重置键可以恢复出厂设置。",
    "公司网络交换机出现环路，导致整个办公区断网。",
    "如何给笔记本加装固态硬盘，并把系统迁移过去。",
    "红烧肉的做法：五花肉切块，冷水下锅焯水，加冰糖炒糖色。",
    "本季度财务报表显示营业收入同比增长 12.4%。",
]
RELEVANT = 2


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b)


def _vectors(embedder, texts: list[str]) -> np.ndarray:
    """直接走 `_forward`（**不加**前缀）。"前缀互换""不加前缀"这两栏只能这么量：
    它们拼出来的东西过不了 `TaskPrefixes` 的四条约束，**那正是约束存在的意义**——
    这个工具要用它量出"过不了约束的那种拼法到底差多少"。"""
    raw = embedder._forward(texts)
    return postprocess_vectors(raw, embedder.dim, source="bench_prefix 裸文本")


def score(embedder, *, query_prefix: str, document_prefix: str) -> np.ndarray:
    """这个查询对每条文档的余弦，**按 `DOCUMENTS` 的顺序**。

    **走产品代码**（`encode_queries` / `encode_documents`）时前缀由 `TaskPrefixes`
    决定。要构造"互换"与"不加"这两种错法，就不能走那一条路——它们的拼法过不了
    `TaskPrefixes` 的四条约束，而那正是约束存在的意义：这个工具要用它量出
    "过不了约束的那种拼法到底差多少"。所以那一栏改成手工拼 + `_forward`。

    ⚠ 手工拼的那两栏**不替换标题槽**（与 `encode_documents(…, titles=None)` 同形），
    所以三条之间只差前缀，比出来的才是前缀的效应。
    """
    product = (
        embedder.prefixes.query == query_prefix
        and embedder.prefixes.document == document_prefix
    )
    if product:
        query = embedder.encode_queries([QUERY])[0]
        docs = embedder.encode_documents(DOCUMENTS)
    else:
        vectors = _vectors(
            embedder,
            [query_prefix + QUERY] + [document_prefix + d for d in DOCUMENTS],
        )
        query, docs = vectors[0], vectors[1:]
    return np.array([cos(query, d) for d in docs])


def ranks(scores: np.ndarray) -> list[int]:
    """每条相关文档在**全部 6 条**里的名次（1 = 排第一）。名次是真正要紧的量：
    余弦的绝对值会随前缀整体平移（实测正确前缀反而让相关文档的余弦**更低**），
    只有**名次**与"用户能不能找到"直接挂钩。"""
    order = np.argsort(-scores)
    position = {int(index): place + 1 for place, index in enumerate(order)}
    return [position[index] for index in range(RELEVANT)]


def measure(name: str, cfg: EmbedCfg) -> dict[str, np.ndarray]:
    if cfg.backend == "sentence_transformers":
        # 不加这一句的话，加载权重时 transformers 会刷 1376 步的进度条，把读数冲掉。
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()
        hf_logging.set_verbosity_error()
    start = time.perf_counter()
    embedder = create_embedder(cfg)
    load_seconds = time.perf_counter() - start
    try:
        prefixes = embedder.prefixes
        variants = [
            ("正确前缀", prefixes.query, prefixes.document),
            ("前缀互换", prefixes.document, prefixes.query),
            ("不加前缀", "", ""),
        ]
        print(f"[{name}] 加载 {load_seconds:.2f} s")
        for label, query_prefix, document_prefix in variants:
            scores = score(
                embedder, query_prefix=query_prefix, document_prefix=document_prefix
            )
            relevant, irrelevant = scores[:RELEVANT], scores[RELEVANT:]
            print(
                f"[{name}] {label}：相关 {relevant.mean():.4f}  无关 {irrelevant.mean():.4f}"
                f"  差 {relevant.mean() - irrelevant.mean():+.4f}"
                f"  名次 {ranks(scores)}"
            )
            # 逐条也印出来：`tests/unit/test_embed_onnx.py` 那条"语义合理性"断言用的是
            # 其中第 1 条（相关）与第 5 条（红烧肉），要能在这里核对它的余量够不够。
            print(
                f"[{name}] {label} 逐条："
                + "  ".join(f"{value:.4f}" for value in scores)
            )
        vectors = {
            "query": embedder.encode_queries([QUERY])[0],
            "docs": embedder.encode_documents(DOCUMENTS),
        }
    finally:
        embedder.close()
    return vectors


def main() -> None:
    # 本机控制台是 GBK（E16）：不戴这个帽子，输出里任何一个 GBK 编不出的字符（`⚠` 是
    # 项目写作习惯）在重定向到文件时会让整屏输出连同退出码一起丢掉。
    use_safe_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="both", choices=["both", "onnx", "st"])
    args = parser.parse_args()

    wanted = ["onnx", "sentence_transformers"] if args.backend == "both" else [args.backend]
    wanted = ["onnx" if b == "onnx" else "sentence_transformers" for b in wanted]

    got: dict[str, dict[str, np.ndarray]] = {}
    for backend in wanted:
        got[backend] = measure(backend, EmbedCfg(backend=backend))

    if len(got) == 2:
        first, second = got["onnx"], got["sentence_transformers"]
        pairs = [("查询", cos(first["query"], second["query"]))]
        pairs.append(
            (
                "文档（逐条）",
                min(cos(a, b) for a, b in zip(first["docs"], second["docs"])),
            )
        )
        print("两个后端的余弦（同一模型、同一前缀、同一维度）")
        for label, value in pairs:
            print(f"    {label}：{value:.6f}")
        all_pairs = [cos(a, b) for a, b in zip(first["docs"], second["docs"])]
        all_pairs.append(cos(first["query"], second["query"]))
        print(f"    最低 {min(all_pairs):.6f}  平均 {np.mean(all_pairs):.6f}")


if __name__ == "__main__":
    main()
