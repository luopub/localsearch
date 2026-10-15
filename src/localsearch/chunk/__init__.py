"""分块层：`ExtractedDoc.text` → `list[Chunk]`（计划 §4.3 的 `chunk/`）。

依赖方向：本层用 `core/`（就为拿 `Chunk` 与 `ChunkCfg` 那两个类型），**不依赖**
`ingest` / `extract` / `embed` / `search`。这一点与 `extract/` 的说明是同一条边上
的两半：提取器不知道"怎么切"，分块器不知道"文件长什么样"——它拿到的只是一段
字符串，正因如此它才对四类提取器一视同仁，也就不需要知道一共有几类。

`sliding.py` 是 v1 唯一的分块策略。名字叫 sliding 是因为它**真的**是一个滑窗
（窗口装到 `target_tokens` 为止、下一片往回退 `overlap_ratio` 那么长），而不是
"按段落切"。换成按结构切（Markdown 标题、代码块）是将来加**第二个**策略的事，
不是把这个改掉——`Chunk` 的字段足够两种策略共用。
"""

from __future__ import annotations

from .sliding import estimate_tokens, split

__all__ = ["estimate_tokens", "split"]
