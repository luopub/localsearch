r"""索引库这一层（P4 起）。

* `schema.sql`  表结构（唯一真相来源，计划 §4.4 的落地形态）
* `tokens.py`   FTS5 的预分词与 MATCH 表达式（中英文两条路共用）
* `tier.py`     三级就绪的**状态**：`status`/`tier` 的词表、选择条件、就绪台账（`D10`）
* `sqlite_store.py`  `SqliteStore`：建库、写行、按名/路径检索、按级别挑活

分级就绪的**另一半**（拿台账决定这一趟能做什么、要标注什么）在 `search/tier.py`——
那一条边界写在 `tier.py` 的模块文档里。

`build.py`（Tier0/Tier1 那一趟的编排）归 `F4-04`；`vectors_*.py` /
`vector_cache.py` / `maintenance.py` 是 P5 / P6 的（计划 §4.3）。
"""
