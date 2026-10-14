"""核心层：配置、日志、跨层共用的数据模型与注册表。

这一层不依赖 `ingest` / `extract` / `embed` / `search` 中的任何一个——
依赖方向单向朝内，否则将来抽 `SearchService` 给 GUI 用时会牵出一串循环导入。
"""
