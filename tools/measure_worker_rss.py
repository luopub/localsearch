r"""量 `F5-02` 要的那一个数：**一个嵌入进程的峰值常驻内存**（读数落 `PROGRESS.md` §3）。

    $env:PYTHONUTF8 = "1"
    uv run python tools/measure_worker_rss.py --tokens 512      # 典型切片那一档
    uv run python tools/measure_worker_rss.py --tokens 2048     # 最坏那一档（= max_seq）

量它的理由只有一个：`A2` 的 `W` 是**用这个数除出来的**（`W = ⌊(可用 − 保留) ÷ 单 worker⌋`），
而 `embed/batching.py` 里的 `PER_WORKER_RSS_BYTES` 就是它。偏小的后果不是"慢"，是进程被内存
压力打掉（`R1`：硬跑会把机器拖到无响应，实测两次）。

------------------------------------------------------------------------------
一次进程只量一档：峰值 RSS 只增不减
------------------------------------------------------------------------------

这是 `P0` 用两次崩掉的运行换来的教训（`PROGRESS.md` §4 P0「与计划的偏差」第 11 条）：
峰值常驻内存**只增不减**，所以同一个进程里连着量两个档位，第二档读到的是"两档之和"，
而它看起来完全正常。所以这个脚本**每次只跑一档**、**只建一个嵌入器**，要两档就开两个进程
分别跑（`E41` 那两行就是这么来的）。

同一段还给出另一条：**内存池按可用内存生长**——"先把别的程序关掉再跑"会让它抓得更多。
所以下面报的可用内存也只是**此刻**的，不是承诺。

------------------------------------------------------------------------------
口径：整个进程的峰值，不是"这一档单独占多少"
------------------------------------------------------------------------------

`GetProcessMemoryInfo` 给的是**这个进程从启动到此刻**的峰值常驻（解释器 + numpy + 模型
+ 一次前向的中间张量）。这与 `E8` 量的是同一个东西、同一个口径（那一轮是在 spawn 出来的
子进程里读的），所以两行数可以直接比。

⚠ 脚本用的是**产品代码**（`localsearch.embed`，走的正是 `EmbedPool`）而不是另写一份推理：
要量的必须就是这个工具将来真跑的那条路。也因此 `--workers 0`（自动）在本机算出来是 1，
走的是"就地跑"那条路——`W == 1` 不开子进程，见 `embed/batching.py` 第四节。

⚠ token 数是拿**嵌入器自己的 tokenizer** 数的（含任务前缀、含截断），不是估算：
`bench_embed.py` 的 `tok/s` 是同一个口径（`M4`：553.0 tok/s），两行要能并排看。
"""

from __future__ import annotations

import argparse
import time

from localsearch.core.config import EmbedCfg, load_config
from localsearch.core.console import use_safe_output
from localsearch.embed.batching import (
    PER_WORKER_RSS_BYTES,
    RESERVE_BYTES,
    EmbedPool,
    available_bytes,
    iter_requests,
    peak_rss_bytes,
    resolve_workers,
    total_bytes,
)

#: 合成语料的积木：一段中文，重复到接近目标 token 数。中文比英文"每字一个 token"
#: 更接近事实（EmbeddingGemma 的 tokenizer 对中文常常一个字一个 token），所以按
#: 字数凑长度是够用的近似——**真实 token 数下面会用 tokenizer 数出来再报**。
PARAGRAPH = (
    "本地离线全文搜索把磁盘上的文档先切成片段，再逐片算成一个向量存进索引；"
    "检索时把查询也算成向量，取余弦最近的那些片段。切片太短会丢掉上下文，"
    "太长会让一个向量混进好几页正文的平均值。"
)


def build_corpus(count: int, tokens: int) -> list[str]:
    """`count` 条、每条大约 `tokens` 个 token 的确定性语料。"""
    # 一段 70 个汉字左右 → 重复到目标长度，再多带一点（截断交给模型那一边）
    repeats = max(1, tokens // len(PARAGRAPH) + 1)
    return [f"文档 {i}：" + PARAGRAPH * repeats for i in range(count)]


def count_tokens(embedder, texts: list[str]) -> int | None:
    """真正喂进模型的 token 数（**含前缀、含截断之后**，不含 padding）。

    ⚠ 读的是嵌入器的 `_tokenizer`（私有属性）：这一层没有别的公开口子，而另起一个
    tokenizer 会把"数的"与"喂的"变成两件事——截断、pad、特殊 token 的加法都可能不一致。
    这是**工具的**用法，不是产品代码的用法；`gemma_st.py` 那一支没有这个属性，所以只有
    ONNX 后端能量这个数（量不了就如实报 `None`）。
    """
    tokenizer = getattr(embedder, "_tokenizer", None)
    if tokenizer is None:
        return None
    prefixed = [embedder.prefixes.document_prefix(None) + text for text in texts]
    return sum(len(one.ids) for one in tokenizer.encode_batch(prefixed))


def main() -> int:
    use_safe_output()
    parser = argparse.ArgumentParser(description="量一个嵌入进程的峰值常驻内存（F5-02）")
    parser.add_argument("--tokens", type=int, default=512,
                        help="每条文本的目标 token 数（典型切片 512、最坏一档就是 max_seq）")
    parser.add_argument("--count", type=int, default=24, help="条数（默认 24 = 3 个批）")
    parser.add_argument("--backend", default="onnx", help="onnx | sentence_transformers")
    parser.add_argument("--workers", type=int, default=0,
                        help="0 = 按内存自动（本机应得 1，走就地跑那条路）")
    parser.add_argument("--config", default=None, help="配置文件；缺省按 resolve_config_file 找")
    options = parser.parse_args()

    base = load_config(options.config).embed
    cfg = EmbedCfg(
        backend=options.backend,
        model_id=base.model_id,
        dim=base.dim,
        dtype=base.dtype,
        quantize=base.quantize,
        batch_size=base.batch_size,
        max_seq=base.max_seq,
        threads=base.threads,
    )

    print("机器事实")
    print(f"    总物理内存 {total_bytes() / 1024**3:.2f} GiB；"
          f"此刻可用 {available_bytes() / 1024**3:.2f} GiB（**只是此刻**）")
    print(f"    单 worker 先验值 PER_WORKER_RSS_BYTES = "
          f"{PER_WORKER_RSS_BYTES / 1024**3:.2f} GiB；"
          f"保留额 RESERVE_BYTES = {RESERVE_BYTES / 1024**3:.2f} GiB")
    plan = resolve_workers(options.workers)
    print(f"    W 的结算：requested={plan.requested} budget={plan.budget} workers={plan.workers}")
    for note in plan.notes:
        print(f"    · {note}")

    print("配置")
    print(f"    backend={cfg.backend} dim={cfg.dim} dtype={cfg.dtype} quantize={cfg.quantize} "
          f"batch_size={cfg.batch_size} max_seq={cfg.max_seq} threads={cfg.threads}")

    corpus = build_corpus(options.count, options.tokens)
    print(f"语料：{options.count} 条 × 约 {options.tokens} token（确定性合成，不读盘）")

    warm = None
    start = time.perf_counter()
    with EmbedPool(cfg, workers=options.workers) as pool:
        count = 0
        for result in pool.embed_documents(iter_requests(corpus, None, size=cfg.batch_size)):
            count += len(result.vectors)
        cold = time.perf_counter() - start
        # 第二趟：同一批文本、同一个池，用来把"纯嵌入"与"含加载"分开。`M4` 的 553.0
        # tok/s 是**暖**的那一档，拿冷的那一趟去比会把它读成"慢了 5 倍"。
        start = time.perf_counter()
        for _ in pool.embed_documents(iter_requests(corpus, None, size=cfg.batch_size)):
            pass
        warm = time.perf_counter() - start
        # ⚠ 下面几样都在 `with` **里面**读：`close()` 之后嵌入器就被放掉了（就地跑那支
        # 连 tokenizer 都置空），出了这个块再读就什么也读不到。
        peak = peak_rss_bytes()
        observed = pool.observed_peak_bytes
        workers_used = pool.workers
        tokens = count_tokens(getattr(pool, "_local", None), corpus) if count else None

    print("读数")
    print(f"    批大小 {cfg.batch_size}，{len(corpus)} 条文本；实体 worker 数 {workers_used}")
    print(f"    向量 {count} 条；冷（含加载）{cold:.2f} s，暖（第二趟）{warm:.2f} s")
    if tokens is None:
        print("    这一支数不出 token 数（`_tokenizer` 只有 ONNX 那支有），所以没有 tok/s")
    else:
        print(f"    实际喂进去的 token 数 {tokens}（含前缀与截断后）")
        per_text = tokens / len(corpus)
        if abs(per_text - options.tokens) > 0.1 * options.tokens:
            print(f"    ⚠ 目标是每条 {options.tokens} token，实际 {per_text:.0f} token/条"
                  f"（差 {abs(per_text - options.tokens) / options.tokens:.0%}）。"
                  "语料是按**字数**拼的近似（中文一字约 0.74 token，见 `build_corpus`），"
                  f"要凑准就把 --tokens 调到 {round(options.tokens * options.tokens / per_text)} "
                  "上下。**下面那个峰值对应的是实际 token 数，不是目标数**——"
                  "写进报告时两样都要写。")
        print(f"    暖的那一趟 → {tokens / warm:.1f} tok/s"
              "（与 M4 的 553.0 同口径：ONNX int8、线程 8、批 8）")
    if warm < cold:
        print(f"    加载那一份约 {cold - warm:.2f} s（冷 − 暖）。**这只是个估计**：它把"
              "两趟之间的任何差异都算成加载。")
    else:
        print(f"    ⚠ 暖的那一趟比冷的**还慢** {warm - cold:.2f} s——所以这份读数里"
              "**分不出**加载占多少。实测出现过（2048 档）：第一趟把内存池撑大之后，"
              "第二趟是在更紧张的内存上跑的。**这种情形下 tok/s 也一起存疑**，别把它"
              "当这台机器的上限。")
    if peak is None:
        print("    本进程峰值常驻内存：量不了（非 Windows）")
    else:
        print(f"    本进程峰值常驻内存 {peak / 1024**3:.2f} GiB = {peak} 字节"
              "（口径同 E8：整个进程的 PeakWorkingSetSize；"
              "⚠ 报字节数是因为 `PER_WORKER_RSS_BYTES` 要的是字节，"
              "而两位小数的 GiB 反推不回原值）")
    if observed is None:
        print("    各 worker 自报的峰值：量不了（非 Windows）")
    else:
        where = "子进程" if workers_used > 1 else "本进程（W == 1 就地跑，没有子进程）"
        print(f"    自报的峰值最大 {observed / 1024**3:.2f} GiB（{where}）")
    print("⚠ 峰值只增不减：一个进程只量一档。要另一档请**另起一次**这个脚本"
          "（E41 那两行就是这么来的），别在同一个进程里连跑两个档位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
