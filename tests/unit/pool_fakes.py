r"""`test_batching.py` 用的假嵌入器：**不加载模型**，但要能证明进程那一层真的在跑。

------------------------------------------------------------------------------
为什么不能拿真模型测这一层
------------------------------------------------------------------------------

`F5-02` 测的是**调度**：几个进程、谁算的、炸了怎么退避。真模型（`E8`：单 worker 峰值
2.01 GiB、`A2` 算出来的 W 在本机是 1）会让"两个 worker"这一条**根本跑不起来**——要么
被内存夹成 1 个，要么把机器拖到无响应（`R1`，实测两次）。所以这里要的是一个**假的**
嵌入器：形状、前缀、后处置都走真代码（`BatchEmbedder` 那一半），只有 `_forward()`
换成"按文本算一个确定的向量"。

------------------------------------------------------------------------------
它必须能被 pickle，而且要能在**子进程**里 import
------------------------------------------------------------------------------

进程池是 `spawn` 的（`batching.py` 的 `_new_executor`），所以：

* **工厂与类都写在模块级**，`pool_fakes.fake_factory` 是模块级的真函数——lambda、
  闭包、局部类都过不了 pickle，而失败表现出来是"池起不来"，看起来像环境问题；
* 子进程要 `import pool_fakes`，靠的是 `spawn` 会把父进程的 `sys.path` 一起传过去，
  而 pytest 的默认导入方式 `prepend` 会把 `tests/unit` 放进 `sys.path`（这个目录没有
  `__init__.py`）。**这条依赖写在明面上**：如果哪天给 `tests/` 加了 `__init__.py`、
  或者换成 `importlib` 导入方式，这个文件得跟着改成可导入的包内模块。

------------------------------------------------------------------------------
三个"触发标记"：靠环境变量传路径，靠文件内容保证**只触发一次**
------------------------------------------------------------------------------

退避会把没算完的批**重投**，而重投的那一批里，标记文本还在。如果标记每次都触发，
减半到 1（就地跑）之后就会在 **pytest 自己的进程**里再炸一次——那是"测试把测试跑挂
了"，不是"退避没生效"。所以触发是一次性的：**文件不存在或为空才算数，触发时先把自己的
pid 写进去**。

| 环境变量 | 文本里带什么 | 会发生什么 |
|---|---|---|
| `LSEARCH_FAKE_PROBE` | —— | 每建一个假嵌入器就往这个文件追一行 pid |
| `LSEARCH_FAKE_CLOSE_PROBE` | —— | 每 `close()` 一次追一行 pid |
| `LSEARCH_FAKE_RAISE` | `RAISE_MARKER` | 当场抛 `MemoryError`（走退避那一条） |
| `LSEARCH_FAKE_KILL` | `KILL_MARKER` | 写下 pid 然后 `os._exit(1)`（把进程打死 → `BrokenProcessPool`） |
| —— | `UNAVAILABLE_MARKER` | 抛 `EmbedUnavailable`（**非**退避错误，整趟应当中止） |

前四行是"进程里发生的事"，所以只能靠文件回报；最后一行是普通异常，直接断言就行。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import BatchEmbedder, EmbedUnavailable, TaskPrefixes

#: 假嵌入器的两条前缀。内容不重要，**非对称**与文档前缀里的标题槽是硬要求
#: （`TaskPrefixes.__post_init__` 会拦下别的写法）。
FAKE_PREFIXES = TaskPrefixes(
    query="task: search result | query: ",
    document="title: none | text: ",
)

PROBE_ENV = "LSEARCH_FAKE_PROBE"
CLOSE_PROBE_ENV = "LSEARCH_FAKE_CLOSE_PROBE"
RAISE_ENV = "LSEARCH_FAKE_RAISE"
KILL_ENV = "LSEARCH_FAKE_KILL"

#: 文本里带上它就触发对应的行为。用 `\x00` 开头是为了**不可能**与真实文本撞上。
RAISE_MARKER = "\x00fake-memoryerror"
KILL_MARKER = "\x00fake-kill"
UNAVAILABLE_MARKER = "\x00fake-unavailable"


def _append(path: str | None, line: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _fires(path: str | None) -> bool:
    """一次性触发：文件不存在或为空 → 这次算数（并把 pid 写进去）。"""
    if not path:
        return False
    probe = Path(path)
    if probe.exists() and probe.stat().st_size:
        return False
    _append(path, str(os.getpid()))
    return True


def vector_for(text: str, dim: int) -> np.ndarray:
    """`text` → 一个**确定的**向量（同一个文本在任何进程里都是同一个向量）。

    不用 `np.random.default_rng(seed)`：那要赌 PRNG 的流在 numpy 版本之间不变。这里
    直接把 blake2b 的摘要当字节用（每元素 2 字节、映到 `[-1, 1)`），跨进程、跨版本、
    跨平台都是同一个数。向量本身没有意义——**有意义的是"同一个文本恒等"**，
    测试靠它断言"向量与 `index` 的对应没错位"。
    """
    need = dim * 2
    chunks: list[bytes] = []
    counter = 0
    while sum(len(one) for one in chunks) < need:
        chunks.append(
            hashlib.blake2b(
                text.encode("utf-8") + counter.to_bytes(4, "big"), digest_size=64
            ).digest()
        )
        counter += 1
    raw = np.frombuffer(b"".join(chunks)[:need], dtype=np.uint16).astype(np.float32)
    return (raw - 32768.0) / 32768.0


class FakeEmbedder(BatchEmbedder):
    """一个假后端：`_forward()` 按文本查表算向量，别的都走 `BatchEmbedder` 的真代码。"""

    name = "fake"

    def __init__(
        self,
        cfg: EmbedCfg,
        prefixes: TaskPrefixes = FAKE_PREFIXES,
        *,
        record: bool = True,
    ) -> None:
        super().__init__(cfg, prefixes)
        # `record=False` 是给 `expected_documents()` 用的：测试要**在测试进程里**
        # 算一份期望值，那次构造不算"起了一个 worker"，不该污染探针文件。
        if record:
            _append(os.environ.get(PROBE_ENV), f"{os.getpid()} {cfg.dim} {cfg.batch_size}")

    def _forward(self, texts: Sequence[str]) -> np.ndarray:
        for text in texts:
            if KILL_MARKER in text and _fires(os.environ.get(KILL_ENV)):
                # 直接把进程打死：这条路的产物必须是 `BrokenProcessPool`，而不是
                # 一个能被 pickle 回去的异常——两者在父进程里走的是不同的分支。
                os._exit(1)
            if RAISE_MARKER in text and _fires(os.environ.get(RAISE_ENV)):
                raise MemoryError(f"假嵌入器按 {RAISE_ENV} 触发")
            if UNAVAILABLE_MARKER in text:
                raise EmbedUnavailable("假嵌入器：这一类是环境问题，整趟该停")
        return np.stack([vector_for(text, self.cfg.dim) for text in texts])

    def close(self) -> None:
        _append(os.environ.get(CLOSE_PROBE_ENV), str(os.getpid()))
        super().close()


def fake_factory(cfg: EmbedCfg) -> FakeEmbedder:
    """`EmbedPool(factory=...)` 要的工厂。**必须是模块级的**（见模块文档）。"""
    return FakeEmbedder(cfg)


def expected_documents(
    cfg: EmbedCfg,
    texts: Sequence[str],
    titles: Sequence[str | None] | None = None,
) -> np.ndarray:
    """在**调用它的这个进程**里算一份期望向量，不碰探针文件。

    断言"向量与 `index` 的对应没错位"靠它：那一批该是什么向量，由"它自己的那几条文本"
    决定，所以拿它去比就能同时抓住"结果串了行"与"标题没跟着切片走"（标题进前缀，
    换个标题向量就变）。
    """
    return FakeEmbedder(cfg, record=False).encode_documents(texts, titles)


def tiny_cfg(**overrides: object) -> EmbedCfg:
    """一个"小到随便跑"的配置：维数 32、批 2、线程 1。

    `dim` 与 `batch_size` 调小只是省时间——这一层不关心向量有多真。
    """
    values: dict[str, object] = {"backend": "fake", "dim": 32, "batch_size": 2, "threads": 1}
    values.update(overrides)
    return EmbedCfg(**values)  # type: ignore[arg-type]


def probe_lines(path: str | None) -> list[str]:
    """读回报文件（还没建过时返回空表——"一个 worker 都没起"就该是这个结果）。"""
    if not path:
        return []
    probe = Path(path)
    if not probe.exists():
        return []
    return [one for one in probe.read_text(encoding="utf-8").splitlines() if one]
