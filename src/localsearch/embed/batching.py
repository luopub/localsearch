r"""多进程调度与 OOM 退避：把"一批批文本"分给几个嵌入进程（`F5-02`，验收项 `A2`）。

`F5-01` 管的是"一个进程里怎么把一个向量算出来"（前缀、分批、后处置）。这一层管的是
**几个进程**——以及一件比"多开几个"重要得多的事：**这台机器放得下几个**。

------------------------------------------------------------------------------
一、为什么是进程，不是线程
------------------------------------------------------------------------------

嵌入这一段是**纯 CPU 的 Python + 两个都自带线程池的库**（ONNX Runtime / torch），
换线程不会变快。而真正决定"要不要拆进程"的是一条实测到的反直觉行为：**ONNX Runtime
与 torch 的内存池按"可用内存"生长**（`PROGRESS.md` §4 P0「与计划的偏差」第 11 条）——
同一份代码，起始可用 7.84 GiB 的那一轮峰值 8.06 GiB 并**段错误退出**，起始可用
6.23 GiB 的那一轮峰值 6.89 GiB 通过。也就是说"先把别的程序关掉再跑"对自己无效：
腾出来的空间会被它一并吃掉，墙只是往后挪。

同一份 P0 读数还给出另一条：**三份模型同时在手**（ST fp32 + ST bf16 + ONNX int8）时
峰值顶到 7.49 GiB，onnxruntime 直接 `EXIT=139`、**没有 traceback**。修法是段落边界
`release_models()`——即"一段算完就把模型放掉"。本模块面对的是同一个问题的另一种形态，
而解法也一样：**不要在一个进程里堆几份模型**。

所以这里的并行单位是**进程**：一个 worker 一个模型、一份内存池，互不相干；哪一份被
内存压力打掉，另外几份还在。

------------------------------------------------------------------------------
二、W 是算出来的：内存账，不是核数
------------------------------------------------------------------------------

`A2` 的原口径是"整机并行 ≥ 单进程 8 倍"，**已作废**——它在本机算术上不可达：
单 worker 峰值 RSS **2.01 GiB**（`E8`）× 8 = **16.09 GiB**，而整机只有 **15.37 GiB**
（`E7`，105%）。用户 2026-10-14 把它重定为：

    W = ⌊(可用内存 − 保留额) ÷ 单 worker 实测 RSS⌋

且"整机有效吞吐 ≥ 0.9 × W × 同配置单进程吞吐、**W ≥ 2** 才算验过"。

**`W` 不按核数取**，这是重定口径时明确的：按物理核算会得 4（`E6`：8 物理 / 16 逻辑），
而按"放得下"算，本机当前余量下是 **1**。核数不是这里的约束——`A2` 的硬顶是内存
（§3.2 设计强制项第 2 条）。所以：

* `[perf] workers = 0`（**模板缺省**）= 由这台机器自己算；
* 写一个正数 = **上限**，实际取 `min(你写的, 内存算出来的)`；
* 内存把它夹住时**要说出来**（`WorkerPlan.notes` + 日志），不静默降级。

⚠ 夹住（clamp）而不是像 `--tier 2` 那样直接拒绝，是一个**判断**，理由：`--tier 2`
被拒是因为"少做一级"会让用户拿到一个**形状不对**的索引（他要的是"意思相近也能搜到"，
给他一个空的 Tier2 却报告"跑完了"）；而这里少开几个 worker 只改变**快慢**，不改变
任何结果——代价是时间，不是正确性。这与 `cli/commands/index.py` 那里"配置里写着
`semantic = true` 就压低一级并说出来"是同一条判据。

**本机现在算出来是多少**：见 `PER_WORKER_RSS_BYTES` 与 `RESERVE_BYTES` 的注释，以及
`tools/measure_worker_rss.py` 的读数。⚠ **`A2` 的性能那一半（W ≥ 2 时到底能加速几倍）
本机测不出来**，归 `F7-04`——这一轮交的是**机制**，不是那个倍数。别把本模块读成
"A2 已验"。

------------------------------------------------------------------------------
三、先验值是先验值：观测值会被回报，退避是兜底
------------------------------------------------------------------------------

`PER_WORKER_RSS_BYTES` 是**先验**（一个实测数，但它量于某一台机器、某一个配置、某一种
长度的文本）。它可能偏小，而偏小的后果是进程被内存压力打掉。所以有两层防护：

1. **每个 worker 自报峰值常驻内存**（`BatchResult.peak_bytes`）——这是**同一口径**下的
   观测值（`E8` 量的是同一样东西：进程的 `PeakWorkingSetSize`），父进程取最大值放进
   `observed_peak_bytes`。它比先验值大得多时说明那个先验该改了，而不是"这次运气好"。
2. **退避**：一个 worker 抛出 `MemoryError`、或者整个进程池被打死
   （`BrokenProcessPool`）时，**不是把这一批丢掉**，而是把 worker 数**减半**、重建进程池、
   **重投没算完的那些批**，并记一条 note。退到 `MIN_WORKERS` 还不行才抛出——那种情况下
   人得知道的是"这台机器现在连一个 worker 都放不下"。

⚠ **"不会重复交付"是退避的硬约束**：结果只在父进程**从 future 里取出来之后**才
`yield`，退避时重投的只是"还没交出去的那些"（含那一刻挂在池里但还没取回的全部）。
所以每个 `BatchRequest.index` **恰好**出现一次——测试里有一条专门钉这件事。

------------------------------------------------------------------------------
四、`W == 1` 就地跑，不开子进程
------------------------------------------------------------------------------

只放得下一个 worker 时（**本机当前的情形**），再开一个子进程买不到任何并行，却要多付：
一份重复的模型常驻（约等于 `PER_WORKER_RSS_BYTES`，而放不下的原因正是内存）、
一次冷启动（`D9`：ONNX 0.50 秒，而 `F5-01` 特意把后端类做成**函数内导入**就是为了
一次性命令的冷启动），以及每批向量的一次 pickle 往返。

所以 `W == 1` 走**就地路径**：同一个进程里直接建嵌入器、直接算。这不是"退化成没实现
并行"——它正是 W 的算术结果该有的行为，而且它让"本机当前 W = 1"这句话是真的在跑，
不是一句注释。

------------------------------------------------------------------------------
五、这一层不做的事
------------------------------------------------------------------------------

* **不做"一个请求拆成几批"**：`EmbedPool` 的工作单位就是一个 `BatchRequest`，它内部
  由嵌入器的 `iter_batches()` 按 `[embed] batch_size` 切。所以**调用方要把请求开得
  接近 `batch_size`**（比如从库里读一行批就是一个请求）——一个请求里塞一万条文本
  不会算错，只是全部落进同一个 worker，并行度白给。这条契约写给 `F5-04` 的索引那一趟。
* **不碰数据库、不碰盘**：它只吃文本、只吐向量。落库、提交、续跑是 `index/build.py`
  的事（`F5-03` / `F5-04`）。
* **不替调用方决定 `--workers`**：这里只算 `W`；把 `--workers` 接到命令面上是 `F5-04`
  的事（`cli/commands/index.py` 的文档已经写明它为什么还不存在）。
"""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import BatchEmbedder, EmbedError, create_embedder, iter_batches

__all__ = [
    "MIN_WORKERS",
    "PER_WORKER_RSS_BYTES",
    "RESERVE_BYTES",
    "BatchRequest",
    "BatchResult",
    "EmbedPool",
    "WorkerPlan",
    "available_bytes",
    "iter_requests",
    "peak_rss_bytes",
    "resolve_workers",
    "total_bytes",
    "worker_budget",
]

log = logging.getLogger(__name__)

#: 单 worker 峰值常驻内存的**先验值**（字节）。
#:
#: 2026-10-16 实测（`tools/measure_worker_rss.py`，原始输出在
#: `docs/reports/F5-02-单worker常驻内存.md`，读数在 `PROGRESS.md` §3 的 `E41`）：
#: 当前配置（ONNX int8、`dim = 768`、`batch_size = 8`、`max_seq = 2048`）下，一个嵌入
#: 进程的峰值常驻内存 = **7.17 GiB**。
#:
#: 口径是**长文本那一档**：一个批 8 条、每条 1622 个 token（`--tokens 2048` 跑出来的
#: 实际值——语料是按字数拼的近似，凑不到满 2048，见 `E41` 的注），一次前向 12976 个
#: token。短切片那一档（416 token/条、同样 8 条一批）是 **1.81 GiB**，与 `E8` 的
#: 2.01 GiB 对得上，而且两次独立运行**都是 1.81 GiB**（1_938_706_432 字节，逐字节相同）。
#:
#: ⚠ 长文本那一档**两次读数不一样**：7.17 GiB（那次起手可用 3.54 GiB）与 6.89 GiB
#: （起手可用 4.67 GiB，精确值 7_392_919_552 字节）。取大的那个 7.17 GiB——7.17 那一趟
#: 只印了两位小数，所以按 7.7e9 字节落（≈7.171 GiB，比读数略大）。这两行一起说明它
#: 不是一台机器的常数：**起手有多少可用内存会影响它长到多大**（模块文档第一节那条）。
#:
#: **取 7.17 而不是 1.81 是一个判断，不是算术必然**：本机可用内存只有 3–5 GiB（`E7`），
#: 减掉 2 GiB 保留额之后**两档都放不下第二个**，所以本机 `W` 恒为 1。差别在别的机器上
#: 会显出来——可用 8 GiB 时，按 1.81 GiB 算得 3 个 worker（而本机只有 8 个物理核，核数
#: 从来不是这里的约束），代价是长文本那一档下 3 × 7.17 = 21.5 GiB，整机只有 15.37 GiB；
#: `R1` 记着硬跑会把机器拖到**无响应**（实测两次）。宁可少开。
#:
#: ⚠ 这个数对"一条占满 `max_seq` 的切片"来说是**下界**：量到的批是 1622 token/条，
#: 不是 2048。偏小的方向正是危险的那个方向（先验偏小 → W 偏大 → 退避兜底），所以这条
#: 记在 `PROGRESS.md` §4 P5 的「遗留」里，等 `F5-04` 定下切片长度之后连 `max_seq`
#: 一起重量（现在改它等于同时动切分与并行两件事）。
#:
#: ⚠ 顺带量到的：`max_seq = 2048` 的代价（峰值 1.81 → 7.17 GiB、暖吞吐 562 → 272 tok/s）
#: 也是这一轮第一次有数——原来 2048 是拍的（`F5-01` 的遗留）。**但这一轮不改它**：
#: 切片长度归 `F5-04` 的切分与接线。
#:
#: ⚠ 它是**先验**：换机器、换 `batch_size`、换 `max_seq`、换后端都会让真值变。所以
#: 每个 worker 会自报实测峰值（`BatchResult.peak_bytes`），偏大时应当回来改这个数；
#: 中间那一段（先验偏小而还没到退避）由 `MemoryError` / `BrokenProcessPool` 兜底。
#:
#: ⚠ **这个数不是"这个配置至少要多少"，而是"它在这台机器上长到了多少"**：那次读数起手
#: 时可用内存 3.5 GiB，峰值却到了 7.17 GiB——它超过了当时可用的量，正是 P0 那条
#: "内存池按可用内存生长"的同一个形态。所以拿它当预算是**保守**方向上的取法。
PER_WORKER_RSS_BYTES = 7_700_000_000

#: 留给机器本身的保留额（`A2` 公式里那个"− 保留额"）。
#:
#: 2 GiB 的来由不是"算出来的最优"，而是两条实测：`E7` 记的可用内存波动（两轮测量期间
#: 4–8 GiB，占用率 48%–63%，本机同时跑着浏览器与编辑器），以及上面那段"内存池按可用
#: 内存生长"——把"当时可用"**全部**算进预算，等于把墙算到刚好贴着，而墙会往后挪。
#: 所以宁可少开一个 worker，也不要精确吃满：这一条在 `R1` 的缓解措施里写着
#: "硬跑会把机器拖到无响应（实测两次）"。
RESERVE_BYTES = 2 * 1024**3

#: 最少一个 worker。退避退到这里就到底了——再往下就是"什么都不算"。
MIN_WORKERS = 1

#: 每两个在飞的请求配一个 worker：够喂饱，又不至于为了并行把一整趟的行都读进内存。
_WINDOW_PER_WORKER = 2


# ==============================================================================
# 机器事实：内存
# ==============================================================================


class _MemoryStatus(ctypes.Structure):
    """`MEMORYSTATUSEX`。字段顺序与 `windows.h` 一致，**一个都不能挪**。"""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status() -> tuple[int, int]:
    """`(总物理内存, 可用物理内存)`，单位字节。

    Windows 走 `GlobalMemoryStatusEx`；别处退回 `os.sysconf`（`SC_PHYS_PAGES` /
    `SC_AVPHYS_PAGES`）。⚠ **本机实测过的只有 Windows 那一支**，`sysconf` 那一支是
    为了"这个模块不至于只能在 Windows 上导入"——它在别的平台上的正确性**没有实测过**，
    所以真要在别的平台上用它，先自己量一遍。

    **必须显式写 `argtypes` / `restype` 并检查返回值。** `ctypes.windll` 的函数默认按
    `c_int` 收尾、返回值不检查，而这里的结构体里放的是 64 位整数：写错就是**静默**拿到
    0，然后 `W` 的整除从"这台机器放得下几个"变成"零除"或者"负数"（P0 在
    `peak_rss()` 上踩过同一个坑，见 `tools/bench_embed.py`）。
    """
    if os.name == "nt":
        fn = ctypes.windll.kernel32.GlobalMemoryStatusEx
        fn.argtypes = [ctypes.POINTER(_MemoryStatus)]
        fn.restype = ctypes.c_int
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not fn(ctypes.byref(status)):
            raise OSError(
                "GlobalMemoryStatusEx 调用失败，取不到物理内存。"
                "取不到就不要猜——W 是拿这个数算出来的，猜出来的 W 会拿机器去试错。"
            )
        return int(status.ullTotalPhys), int(status.ullAvailPhys)

    page = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
    return (
        page * os.sysconf("SC_PHYS_PAGES"),  # type: ignore[attr-defined]
        page * os.sysconf("SC_AVPHYS_PAGES"),  # type: ignore[attr-defined]
    )


def total_bytes() -> int:
    """总物理内存（字节）。"""
    return _memory_status()[0]


def available_bytes() -> int:
    """**此刻**可用的物理内存（字节）。

    ⚠ 它只描述这一瞬间：本机同时跑着别的程序（`E7`：两轮测量之间可用内存在 4–8 GiB
    之间波动）。所以它是 W 的**输入**，不是承诺——退避那一层存在就是因为这台机器上
    "刚才还放得下"不等于"接下来也放得下"。
    """
    return _memory_status()[1]


class _ProcessMemoryCounters(ctypes.Structure):
    """`PROCESS_MEMORY_COUNTERS`（`psapi.h`）。字段顺序同样不能挪。"""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_rss_bytes() -> int | None:
    """**本进程**的峰值常驻内存（字节）；量不了时返回 `None`。

    这是 `E8` 量单 worker 内存用的同一个数（`PeakWorkingSetSize`）与同一个口径——
    每个 worker 在算完一批之后自报它，父进程取最大值，用来核对先验值有没有偏小。

    ⚠ `GetProcessMemoryInfo` 在 **psapi.dll** 里（kernel32 只有 `K32GetProcessMemoryInfo`
    这个别名），而 `argtypes` / `restype` 同样必须显式写：首参是 64 位句柄，默认按
    `c_int` 收就会**静默返回 0**——函数"成功"了，数字是 0。
    """
    if os.name != "nt":
        return None
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong]
    fn.restype = ctypes.c_int
    get_current = ctypes.windll.kernel32.GetCurrentProcess
    get_current.argtypes = []
    get_current.restype = ctypes.c_void_p
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    if not fn(get_current(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PeakWorkingSetSize)


# ==============================================================================
# W：按内存算几个 worker
# ==============================================================================


def worker_budget(
    available: int, per_worker: int, reserve: int = RESERVE_BYTES
) -> int:
    """`A2` 的公式：`W = max(1, ⌊(可用内存 − 保留额) ÷ 单 worker 峰值 RSS⌋)`。

    三件要说清的事：

    * **至少 1。** 算出 0 或负数说明"一个都放不下"，而那不是一个可行的结论——`P4` 起
      这个工具已经能搜了，Tier2 是**增强**不是前提（`D10`：分级就绪）。所以返回 1，
      由调用方按 `W == 1` 就地跑（见模块文档第四节），并在内存真的不够时靠 `MemoryError`
      说话。**"算出 0 就什么都不跑"会把一次内存紧张变成一次静默的无所作为。**
    * **不按核数取**（`A2` 重定口径时的明确决定）。核数在这台机器上从来不是约束：
      物理核 8、内存只放得下 1。
    * `per_worker <= 0` **当场报错**：那会让这里的整除得到 ZeroDivisionError（或者
      一个荒谬的大数），而两种都不该被"兜底"成一个看起来正常的 W。
    """
    if per_worker <= 0:
        raise ValueError(f"单 worker 内存必须是正数，拿到 {per_worker}")
    usable = available - reserve
    if usable <= 0:
        return MIN_WORKERS
    return max(MIN_WORKERS, usable // per_worker)


@dataclass(frozen=True, slots=True)
class WorkerPlan:
    """一次 `W` 的结算：**请求了几个、放得下几个、实际用几个、以及要告诉人的话**。

    存在的理由是"夹住时不许静默"：`notes` 里的每一条都是**能改变某个人判断**的事实
    （要么是自动算出来的数，要么是"你要的和放得下不一样"），调用方拿它去写日志、
    去 `EmbedPool.notes`、将来去 `lsearch status`。
    """

    #: 配置里写的（`0` = 自动）。
    requested: int
    #: 按内存算出来的上限。
    budget: int
    #: 实际要开的进程数（`W == 1` 时由 `EmbedPool` 就地跑）。
    workers: int
    #: 算这个数用到的三个输入，原样留着以便日志/报告复现这个数。
    available: int
    per_worker: int
    reserve: int
    #: 要说出来的话（可能为空）。
    notes: tuple[str, ...] = ()


def _gib(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


def resolve_workers(
    requested: int,
    *,
    available: int | None = None,
    per_worker: int | None = None,
    reserve: int | None = None,
    threads: int = 1,
    logical_cores: int | None = None,
) -> WorkerPlan:
    """把"配置里写的"与"这台机器放得下的"结算成 `WorkerPlan`。

    五个输入都可以注入：测试要的是**确定的**数字，而默认值全都取自机器（可用内存会
    随着浏览器开开关关而变，拿它当测试输入就是拿一台随机机器当测试环境）。

    `requested < 0` 当场报错（配置层已经拦过一次，见 `core/config.py` 的 `_MINIMUMS`；
    这里再拦一次是因为这个函数的调用方不止配置那一条路）。
    """
    if requested < 0:
        raise ValueError(f"worker 数不能是负数，拿到 {requested}")
    available = available_bytes() if available is None else available
    per_worker = PER_WORKER_RSS_BYTES if per_worker is None else per_worker
    reserve = RESERVE_BYTES if reserve is None else reserve
    cores = (os.cpu_count() or 1) if logical_cores is None else logical_cores

    budget = worker_budget(available, per_worker, reserve)
    notes: list[str] = []
    if requested == 0:
        workers = budget
        notes.append(
            f"[perf] workers = 0（自动）：可用 {_gib(available)} − 保留 {_gib(reserve)}"
            f" ÷ 单 worker {_gib(per_worker)} = {budget} 个。"
            "这个数按内存算，不按核数——A2 重定时就是这么定的（见 embed/batching.py 头）。"
        )
    else:
        workers = min(requested, budget)
        if workers < requested:
            notes.append(
                f"配置里要 {requested} 个嵌入 worker，但内存只放得下 {budget} 个"
                f"（可用 {_gib(available)} − 保留 {_gib(reserve)} ÷ 单 worker "
                f"{_gib(per_worker)}），实际用 {workers} 个。**这是被夹住，不是静默降级**："
                "结果一个字都不会变，变的只是快慢（A2 的 W 由内存决定）。"
            )

    # `[embed] threads` 与 worker 数是**相乘**的（两个配置项的注释里都写着）。这里只提醒，
    # 不改：改线程数会动到"单 worker 有多快"那个已经量过的口径（M4 量于线程 = 8）。
    if threads > 1 and workers * threads > cores:
        notes.append(
            f"{workers} 个 worker × [embed] threads({threads}) = {workers * threads} 个"
            f"线程，而本机逻辑核只有 {cores}（物理核更少，见 PROGRESS.md §3 的 E6："
            "8 物理 / 16 逻辑）。总线程数超过核数之后，多出来的那部分大概率是**负收益**"
            "——真要开多个 worker 时，把 [embed] threads 一起降下来（约 物理核 ÷ workers）。"
            "⚠ 这一条只是提醒：`threads` 不改向量数值（它不在 `embed_ver` 里），"
            "但「多少个线程最快」是 M4 量出来的，本模块不替它改配置。"
        )
    return WorkerPlan(
        requested=requested,
        budget=budget,
        workers=workers,
        available=available,
        per_worker=per_worker,
        reserve=reserve,
        notes=tuple(notes),
    )


# ==============================================================================
# 工作单位
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """一批待嵌入的文本。**调用方开的大小应当接近 `[embed] batch_size`**（模块文档第五节）。

    `index` 是调用方给的序号，唯一的作用是让结果**归位**：并行算出来的东西是乱序回来的，
    靠它才能把向量放回正确的位置（`F5-04` 写库时按它对应回切片）。
    """

    index: int
    texts: tuple[str, ...]
    #: 与 `texts` 等长；不需要标题时给 `None`（`F5-01` 的标题槽）。
    titles: tuple[str | None, ...] | None = None

    def __post_init__(self) -> None:
        if self.titles is not None and len(self.titles) != len(self.texts):
            raise ValueError(
                f"titles 与 texts 不等长：{len(self.titles)} 对 {len(self.texts)}。"
                "错位的症状是每条文本带上**别人**的标题，而向量看起来完全正常——"
                "所以这里当场拒绝，不留到搜出来才发现。"
            )


@dataclass(frozen=True, slots=True)
class BatchResult:
    """一批文本的向量，外加"这一批是谁算的、算了多少内存"。"""

    index: int
    #: `(len(texts), dim)`，`float32`、已归一化（`F5-01` 的后处置保证）。
    vectors: np.ndarray
    #: 算这一批的进程号。`W == 1` 时就是父进程自己（就地跑）。
    worker_pid: int
    #: 那个进程的峰值常驻内存；量不了时 `None`（非 Windows）。
    peak_bytes: int | None = None


def iter_requests(
    texts: Sequence[str],
    titles: Sequence[str | None] | None = None,
    *,
    size: int,
    start: int = 0,
) -> Iterator[BatchRequest]:
    """把一串文本按 `size` 切成 `BatchRequest`（序号从 `start` 起）。

    就是 `base.iter_batches()` 外面套了一层"带序号、带标题"，好让索引那一趟不必自己写
    一遍切法与序号管理——`F5-01` 留的 `iter_batches()` 接在这里。
    """
    items = list(texts)
    title_list = None if titles is None else list(titles)
    if title_list is not None and len(title_list) != len(items):
        raise ValueError(f"titles 与 texts 不等长：{len(title_list)} 对 {len(items)}")
    for ordinal, batch in enumerate(iter_batches(items, size)):
        picked = (
            None
            if title_list is None
            else tuple(title_list[ordinal * size : ordinal * size + len(batch)])
        )
        yield BatchRequest(index=start + ordinal, texts=tuple(batch), titles=picked)


# ==============================================================================
# 子进程那一侧
# ==============================================================================

#: 每个 worker 进程的私货：**一个进程一个嵌入器**，第一次接到活时才建。
#:
#: 为什么要懒建：`ProcessPoolExecutor` 的 `initializer` 里建的话，建失败（模型不在本机、
#: 快照缺件）会表现为"这个进程死了"→ 父进程收到 `BrokenProcessPool`，而**原始异常
#: （`EmbedUnavailable`）连同它的理由一起丢了**。放进任务里建，异常会原样 pickle 回来，
#: 调用方拿到的是"权重不在 `HF_HOME` 里、去跑 tools/fetch_model.py"这句话，而不是
#: "进程池坏了"。
_WORKER_STATE: dict[str, Any] = {}

_BACKOFF_ERRORS = (MemoryError, BrokenProcessPool)


def _init_worker(cfg: EmbedCfg, factory: Any) -> None:
    """进程池的 initializer：只把配置与工厂放进全局，**不建模型**（理由见 `_WORKER_STATE`）。"""
    _WORKER_STATE.clear()
    _WORKER_STATE["cfg"] = cfg
    _WORKER_STATE["factory"] = factory
    _WORKER_STATE["embedder"] = None


def _embedder_here() -> BatchEmbedder:
    """本进程的嵌入器，**整个进程只建一次**。

    "只建一次"不是优化，是正确性与内存：`[embed] batch_size = 8` 时，一个批一个模型
    等于把模型加载重复几百次（本机 ONNX 冷启动实测 0.5–3.6 秒，`E40`），而每建一次就
    多一份模型常驻——恰恰是"放不下"的那份内存。
    """
    embedder = _WORKER_STATE.get("embedder")
    if embedder is None:
        embedder = _WORKER_STATE["factory"](_WORKER_STATE["cfg"])
        _WORKER_STATE["embedder"] = embedder
    return embedder


def _encode_request(request: BatchRequest) -> BatchResult:
    """子进程里的入口（`F5-02` 的 worker 函数）。**必须是模块级的**：进程池按名字 pickle 它。"""
    embedder = _embedder_here()
    vectors = embedder.encode_documents(request.texts, request.titles)
    return BatchResult(
        index=request.index,
        vectors=vectors,
        worker_pid=os.getpid(),
        peak_bytes=peak_rss_bytes(),
    )


# ==============================================================================
# 池
# ==============================================================================


class EmbedPool:
    """把一批批文本分给若干嵌入进程，流式取回向量（`F5-02`）。

    形状是**生成器**：`for result in pool.embed_documents(requests): ...`。这样调用方
    可以"边算边写库"（`F5-04` 的索引那一趟正是如此），而不是先把整趟的两万个文件的
    向量都攒在内存里——那会把 `A1` 的规模问题变成内存问题。

    结果按**完成的先后**产出（不是按 `index`），`BatchResult.index` 负责归位。
    """

    def __init__(
        self,
        cfg: EmbedCfg,
        *,
        workers: int = 0,
        factory: Callable[[EmbedCfg], BatchEmbedder] = create_embedder,
        available: int | None = None,
        per_worker_bytes: int | None = None,
        reserve_bytes: int | None = None,
        logical_cores: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.factory = factory
        self.plan = resolve_workers(
            workers,
            available=available,
            per_worker=per_worker_bytes,
            reserve=reserve_bytes,
            threads=cfg.threads,
            logical_cores=logical_cores,
        )
        self.workers = self.plan.workers
        self.notes = list(self.plan.notes)
        #: 各 worker 自报的峰值常驻内存里的最大值（`None` = 量不了 / 还没算过）。
        self.observed_peak_bytes: int | None = None
        self._window = max(2, self.workers * _WINDOW_PER_WORKER)
        self._executor: ProcessPoolExecutor | None = None
        self._local: BatchEmbedder | None = None
        self._closed = False
        for note in self.notes:
            log.info("嵌入并行：%s", note)
        if self.workers > 1:
            self._executor = self._new_executor()

    # ---------------------------------------------------------------- 生命周期

    def _new_executor(self) -> ProcessPoolExecutor:
        """建一个进程池。**`spawn`**（不是 `fork`）：父进程里已经有一份装好模型的
        Python 堆，`fork` 之后子进程会连父进程的内存池一起复制/共享，而这正是要分开的
        东西。`spawn` 也顺带保证了"每个进程从干净的模块状态开始"——`F5-01` 把后端类
        做成函数内导入，那一层的前提就是子进程会自己 import 一遍。
        """
        import multiprocessing

        return ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            initargs=(self.cfg, self.factory),
        )

    def close(self) -> None:
        """收摊。**幂等**，且在异常路径上也会被调到（生成器自己保证）。

        `cancel_futures=True`：退避/中止时"还没开跑的批"直接取消，别在缩编之后还让它们
        挂着；已经在跑的那一批不去打断它（`ProcessPoolExecutor` 自己会在解释器退出时
        兜住进程）。
        """
        if self._closed:
            return
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._local is not None:
            self._local.close()
            self._local = None

    def __enter__(self) -> EmbedPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 退避

    def _shrink(self, exc: BaseException) -> None:
        """退避：worker 数减半、重建池、记一条 note。退到 `MIN_WORKERS` 还出问题就抛出。

        抛出的消息要说清"现在该怎么办"——到这一步用户能做的只有三件：关掉别的程序腾内存、
        把 `[embed] batch_size` / `max_seq` 调小（单 worker 的内存主要由它俩决定）、
        或者换一台机器。所以消息里把这三条写出来，而不是只报一个 `MemoryError`。
        """
        reason = (
            "有 worker 抛了 MemoryError"
            if isinstance(exc, MemoryError)
            else "进程池被打死了（BrokenProcessPool）"
        )
        if self.workers <= MIN_WORKERS:
            raise EmbedError(
                f"{reason}，而 worker 数已经是 {MIN_WORKERS}——**这台机器现在连一个嵌入"
                f"进程都放不下**（可用 {_gib(available_bytes())}，单 worker 先验 "
                f"{_gib(self.plan.per_worker)}）。能做的三件事：关掉别的程序腾内存、把 "
                "[embed] batch_size 与 max_seq 调小（单 worker 的常驻内存主要由这两个决定）、"
                "或者换一台内存更大的机器。"
            ) from exc
        self.close_executor_only()
        self.workers = max(MIN_WORKERS, self.workers // 2)
        self._window = max(2, self.workers * _WINDOW_PER_WORKER)
        if self.workers > 1:
            self._executor = self._new_executor()
        note = (
            f"{reason} → worker 数减半到 {self.workers}（**这一批不会丢**：没算完的批会"
            "重投；每次退避都是减半，退到 1 还不行才抛出）"
        )
        self.notes.append(note)
        log.warning("嵌入并行退避：%s", note)

    def close_executor_only(self) -> None:
        """只收掉进程池、保留 `closed` 状态（退避时重建用）。"""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    # ---------------------------------------------------------------- 主循环

    def _submit(self, request: BatchRequest) -> Future[BatchResult]:
        if self._executor is not None:
            return self._executor.submit(_encode_request, request)
        # 就地跑：见模块文档第四节。`Future` 只是为了让主循环不必分两条路。
        future: Future[BatchResult] = Future()
        try:
            if self._local is None:
                self._local = self.factory(self.cfg)
            vectors = self._local.encode_documents(request.texts, request.titles)
            future.set_result(
                BatchResult(
                    index=request.index,
                    vectors=vectors,
                    worker_pid=os.getpid(),
                    peak_bytes=peak_rss_bytes(),
                )
            )
        except BaseException as exc:  # noqa: BLE001 —— 原样交给 future，由主循环处置
            future.set_exception(exc)
        return future

    def _remember_peak(self, result: BatchResult) -> None:
        if result.peak_bytes is None:
            return
        if self.observed_peak_bytes is None or result.peak_bytes > self.observed_peak_bytes:
            self.observed_peak_bytes = result.peak_bytes

    def embed_documents(self, requests: Iterable[BatchRequest]) -> Iterator[BatchResult]:
        """流式嵌入。**每个 `request.index` 恰好产出一次**（退避也不会重复，见模块文档）。

        任何**非退避**异常（`EmbedUnavailable`、`EmbedError`……）都会让这一趟**整体中止**
        并把原异常抛出去：`EmbedUnavailable` 的文档写着"这一类是环境问题，调用方应当立刻
        停下整趟索引"——两万个文件逐条跳过只是把一次失败摊成两万次。

        **出错或提前退出（调用方 `break`）时这一层会把池收掉**；正常跑完则留着，同一个池
        可以接着喂下一批请求（一趟索引里会有好几段）。用完请 `close()`（或者用 `with`）。
        """
        if self._closed:
            raise EmbedError("这个 EmbedPool 已经 close() 过了")
        iterator = iter(requests)
        exhausted = False
        pending: dict[Future[BatchResult], BatchRequest] = {}
        try:
            while True:
                while not exhausted and len(pending) < self._window:
                    try:
                        request = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    pending[self._submit(request)] = request
                if not pending:
                    return
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                retry: list[BatchRequest] = []
                for future in done:
                    request = pending.pop(future)
                    try:
                        result = future.result()
                    except _BACKOFF_ERRORS as exc:
                        retry.append(request)
                        self._shrink(exc)
                        # 那一刻挂在池里的批**一个都还没交出去**，连同这一批一起重投；
                        # 重投是"重算"，不是"补交"，所以不会产出两次。
                        retry.extend(pending.pop(one) for one in list(pending))
                        break
                    self._remember_peak(result)
                    yield result
                for one in retry:
                    pending[self._submit(one)] = one
        except BaseException:
            # 生成器被提前关掉（`GeneratorExit`）与任何异常都走这里：把进程池收掉，
            # 别留下一堆没人管的子进程（它们在 Windows 上会一直占着内存直到解释器退出）。
            self.close()
            raise
