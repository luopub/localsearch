r"""索引库这一层（P4 起）。

* `schema.sql`  表结构（唯一真相来源，计划 §4.4 的落地形态）
* `tokens.py`   FTS5 的预分词与 MATCH 表达式（中英文两条路共用）
* `sqlite_store.py`  `SqliteStore`：建库、写行、按名/路径检索

`vectors_*.py` / `vector_cache.py` / `maintenance.py` 是 P5 / P6 的（计划 §4.3）。
"""
