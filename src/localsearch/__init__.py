"""LocalSearch —— 本地离线全盘全文搜索。

这个包的形态（src 布局、可编辑安装、`lsearch` 入口点）由 P1 的 `F1-01` 落地，
测试落点在 `tests/unit/test_package.py`。

`__version__` 是**版本号的唯一真相来源**：`pyproject.toml` 用
`[tool.hatch.version] path = "src/localsearch/__init__.py"` 从这里读，不另写一份。
两处各写一个版本号是必然漂移的写法，测试 `test_version_single_source` 盯着这一点。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
