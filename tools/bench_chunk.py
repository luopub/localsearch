r"""`chunk/sliding.py` 的开销读数（`F3-05`）。

------------------------------------------------------------------------------
为什么要有这个
------------------------------------------------------------------------------

`sliding.py` 的模块文档里有几句关于**代价**的话（"先倍增再二分，所以每次切片的
计数器调用次数是 `O(log 片长)`"、"两趟都是 C 层扫描"），而它们是写代码时的推理，
不是读数。P4 要拿这些数决定"要不要先优化分块再跑全盘"，P5 换真 tokenizer 之后
还要再问一遍同样的问题（那时代价从"扫字符"变成"每次调用有固定开销"）。

------------------------------------------------------------------------------
它量三件事
------------------------------------------------------------------------------

1. **用时**：几种规模的正文切完要多久（含 `D8` 的上限生效的那些——⚠ 上限只限
   **产出**，正文还是要**整篇走完**，因为统计摘要要说"本来一共多少片"）。
2. **计数器被调用多少次**：这个数是**确定性的**，跟机器负载无关，所以它比耗时更
   适合横向比较，也正好是 P5 那种"每次调用都有固定开销"的计数器的成本模型。
3. **`D8` 在多大的正文上开始触发**（拿 `ChunkCfg` 的真数字 512 / 0.15 实测一遍，
   而不是纸上算）。

⚠ 正文是**现造**的，不读盘、不碰任何用户文件；跑完什么也不留下。

用法：

    PYTHONIOENCODING=utf-8 uv run python tools/bench_chunk.py
"""

from __future__ import annotations

import time
from dataclasses import asdict

from localsearch.chunk import estimate_tokens, split
from localsearch.core.config import ChunkCfg
from localsearch.core.console import use_safe_output

#: 一篇正文的全部规模与内容都写在这里，一列对齐着看。
SAMPLES: tuple[tuple[str, str, int], ...] = (
    ("中文", "本地离线全盘全文搜索索引测试文档。", 500),
    ("中文", "本地离线全盘全文搜索索引测试文档。", 5_000),
    ("中文", "本地离线全盘全文搜索索引测试文档。", 136_000),
    ("中文", "本地离线全盘全文搜索索引测试文档。", 1_062_500),
    ("ASCII", "the quick brown fox jumps over the lazy dog 1234567890 ", 1_100_000),
)


def section(number: int, title: str) -> None:
    print(f"\n{'=' * 78}\n{number}. {title}\n{'=' * 78}")


def repeat(text: str, size: int) -> str:
    """把一句样本重复到**至少** `size` 个字符（整句地重复，不切掉半句）。"""
    unit = max(1, -(-size // len(text)))
    return text * unit


def timed(text: str, **keywords) -> tuple[list, float, int]:
    """切一遍，返回（片、秒数、计数器调用次数）。"""
    calls = [0]

    def counting(part: str) -> int:
        calls[0] += 1
        return estimate_tokens(part)

    start = time.perf_counter()
    chunks = split(text, count_tokens=counting, **keywords)
    return chunks, time.perf_counter() - start, calls[0]


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    config = asdict(ChunkCfg())
    print(f"ChunkCfg：{config}")

    # ---------------------------------------------------------------- 1 + 2
    section(1, "用时与计数器调用次数（上限按配置的 200 片）")
    print(f"{'内容':7s}{'字符数':>11s}{'片数':>6s}{'截断':>5s}{'毫秒':>9s}"
          f"{'us/字符':>9s}{'计数器调用':>11s}{'不截断的片数':>13s}{'每次窗口搜索':>13s}")
    for label, unit, size in SAMPLES:
        text = repeat(unit, size)
        chunks, seconds, calls = timed(text, **config)
        # 第二遍不设上限：**上限只限产出，正文照样整篇走完**，所以"每次窗口搜索
        # 调用了几次计数器"要用不截断的片数当分母，不能用保留下来的 199 片。
        full, _, _ = timed(text, **{**config, "max_chunks_per_doc": 100_000})
        print(f"{label:7s}{len(text):>11,d}{len(chunks):>6d}"
              f"{'是' if chunks and chunks[-1].truncated else '否':>5s}"
              f"{seconds * 1000:>9.1f}{seconds / len(text) * 1e6:>9.2f}"
              f"{calls:>11,d}{len(full):>13d}{calls / len(full):>13.1f}")
    print("\n⚠ 「每次窗口搜索」就是二分那一段的调用次数。它是 `O(log 片长)` 的"
          "**大致两倍**：先倍增探上界（2、4、8……）到超出一格，再在 [上界, 超出点] "
          "里二分。片长越大这个数的绝对值越大，但**每一步都只扫一个窗口那么长**"
          "（这正是当初不用「在 [起点, 正文末尾] 上直接二分」的原因——那样每探一次"
          "都要扫半个正文）。")

    # ---------------------------------------------------------------- 3
    section(2, "上限生效时，正文整篇仍然要走完（对比：不设上限）")
    print(f"{'字符数':>11s}{'不截断的片数':>14s}{'不截断毫秒':>12s}"
          f"{'200 片上限的毫秒':>18s}{'截断后保留':>12s}")
    for size in (50_000, 100_000, 500_000, 1_062_500):
        text = repeat("本地离线全盘全文搜索索引测试文档。", size)
        full, full_seconds, _ = timed(
            text, **{**config, "max_chunks_per_doc": 100_000}
        )
        capped, capped_seconds, _ = timed(text, **config)
        kept = sum(1 for one in capped if not one.truncated)
        print(f"{len(text):>11,d}{len(full):>14d}{full_seconds * 1000:>12.1f}"
              f"{capped_seconds * 1000:>18.1f}{kept:>12d}")

    # ---------------------------------------------------------------- 4
    section(3, "D8 在多大的中文正文上开始触发（实测）")
    print(f"{'字符数':>11s}{'片数':>7s}{'有统计摘要':>12s}")
    for size in (80_000, 86_000, 87_000, 88_000, 90_000):
        text = repeat("本地离线全盘全文搜索索引测试文档。", size)
        chunks = split(text, **config)
        print(f"{len(text):>11,d}{len(chunks):>7d}"
              f"{'是' if chunks[-1].truncated else '否':>12s}")
    edge = 199 * (config["target_tokens"]
                  - int(config["overlap_ratio"] * config["target_tokens"])) \
        + config["target_tokens"]
    print(f"\n纸上算的临界点（每片净增 "
          f"{config['target_tokens'] - int(config['overlap_ratio'] * config['target_tokens'])}"
          f" 个 token = 中文同样多的字符）：199 × 那个数 + 一片 = 约 {edge:,d} 字符。"
          "上面那几行是**实测**，它应该落在这个数两边。")

    print("\n⚠ 耗时随机器负载浮动，别拿单次读数当基准；第 2、3 段里的调用次数是"
          "确定性的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
