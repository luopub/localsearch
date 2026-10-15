"""提取层：一个文件 → 正文（计划 §4.3 的 `extract/`）。

依赖方向：本层可以用 `core/` 与 `ingest/`，**不反向依赖** `chunk` / `embed` / `search`。
切片不在这一层——提取器只负责把正文端出来，怎么切是 `chunk/` 的事。

------------------------------------------------------------------------------
两个入口，不是两个层次
------------------------------------------------------------------------------

- **单个文件**：`extract_file()`（`base.py`）——派发 + 提取，出事就抛。
- **一批文件**：`extract_many()`（`batch.py`）——同一个派发，外加"一个文件出事不停下
  整轮"，每个文件换回一条 `Outcome`（`A6`，`F3-07`）。

`batch` 不是"更高一层"：它是同一件事的**批处理形态**，除了接异常与计数什么都没做
（不写库、不切片）。`P4` 的索引循环直接用它。

------------------------------------------------------------------------------
装配只在这个文件里
------------------------------------------------------------------------------

`build_registry()` 是**唯一**决定"有哪几个提取器、按什么顺序问"的地方。顺序即优先级
（`core/registry.py` 那三条约定），所以它必须一眼看得完：注册调用按顺序排下来，
中间不夹条件分支的嵌套（每个模块的 `build()` 自己回答"我这次要不要上"）。

各提取器模块统一暴露 `build(config) -> Extractor | None`，返回 `None` 表示这次不注册，
**不是**"注册了但什么都不做"——理由写在 `plaintext.build()` 的文档里。
"""

from __future__ import annotations

from localsearch.core.registry import Registry

from .batch import ERROR_MSG_MAX, Outcome, Tally, extract_many
from .base import (
    BinaryContent,
    Decoded,
    ExtractConfig,
    ExtractedDoc,  # noqa: F401 - 从这一层转发出去，调用方不必知道它在 core/models.py
    Extractor,
    ExtractError,
    FileUnreadable,
    ParseFailed,
    UnsupportedFormat,
    decode_bytes,
    ext_of,
    extract_file,
    find_extractor,
    looks_binary,
    mime_of,
    read_bytes,
    read_text,
)

__all__ = [
    "ERROR_MSG_MAX",
    "BinaryContent",
    "Decoded",
    "ExtractConfig",
    "ExtractError",
    "ExtractedDoc",
    "Extractor",
    "FileUnreadable",
    "Outcome",
    "ParseFailed",
    "Tally",
    "UnsupportedFormat",
    "build_registry",
    "decode_bytes",
    "ext_of",
    "extract_file",
    "extract_many",
    "find_extractor",
    "looks_binary",
    "mime_of",
    "read_bytes",
    "read_text",
]


def build_registry(config: ExtractConfig) -> Registry[Extractor]:
    """按 `[extract]` 的四个开关装配提取器，**注册顺序即匹配优先级**。

    顺序有讲究，两条：

    - `plaintext` 放最前。它的名单是**穷举**的（`.txt` `.py` `.json`……）加上"没有
      扩展名"，前者不会与别人重叠，后者（`README` / `Makefile`）本来也不该被别人抢。
      放在前面意味着"认领"这件事在绝大多数文件上一步就问完了。
    - 其余三个按**格式的确定性**排：`office`（文件头就是硬证据）→ `web_mail`
      （`.eml` / `.mht` 是容器，`.html` 才是内容）→ `media_meta`（只出元数据，
      是被别人挑剩下的那一类）。这个顺序今天不会产生任何冲突（四份名单互不相交），
      写下来是为了**将来加 OCR / 多模态提取器时**有个明确的插入位置：新来的
      **必须**排在它想覆盖的那些后面，或者把名单改窄。
    """
    from . import media_meta, office, plaintext, web_mail

    registry: Registry[Extractor] = Registry("extractor")
    # P3 的四个都在这里了（`F3-01` 到 `F3-04`）；`F3-05` 起是分块、失败隔离与
    # fixtures，不再往这个元组里加东西。顺序就是上面文档里那条：格式的确定性从高到低。
    for module in (plaintext, office, web_mail, media_meta):
        extractor = module.build(config)
        if extractor is not None:
            registry.register(extractor.name, extractor)
    return registry
