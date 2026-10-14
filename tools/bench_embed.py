"""P0 嵌入吞吐实测：把"本机到底多快"变成可复现的原始输出。

对应功能点：F0-01（嵌入吞吐实测工具）、F0-02（ST 与 ONNX int8 双跑对比）、
            F0-03（本机 tokens/s 实测数字）。关联验收项 A2。
它同时产出决策 **D9**（嵌入后端选谁）所需的数字，并替换计划 §3.2 里那张
推算表的"嵌入耗时"两行——那两行的依据是别人的机器上的二手数字。

用法（在仓库根执行）：

    # 第一次：把 ONNX int8 成品取到本地（约 350MB，只取文本模型，不取那 6GB 全家桶）
    PYTHONUTF8=1 uv run python -u tools/bench_embed.py --download-onnx

    # 之后：完全离线地跑完整轮，原始输出落盘
    PYTHONUTF8=1 uv run python -u tools/bench_embed.py > docs/reports/F0-01-F0-03-本机嵌入吞吐.md 2>&1

为什么用重定向、而不是让脚本自己写报告文件：与 probe_deps.py 同一个理由——
落盘的是**未经加工的原始输出**，脚本不参与排版，也就不可能把失败修饰成好看的样子。

---- 三个数字的口径（这是本脚本最容易被误读的地方，先看这里）----------------

别人的 tokens/s 数字之所以互相不可比，几乎总是因为"token 指的是哪个"没写清。
本脚本把三个数**分开算、分开报**：

  有效 tokens   实际喂进去的 token，含任务前缀，**不含 padding**。
                这是换算索引耗时的分母：语料总 token ÷ 有效吞吐 = 小时数。
  计算 tokens   硬件真正算过的 token = 每个 batch 的 (batch_size × 该批最长序列)。
                含 padding。这是"硬件有多快"的分母。
  padding 放大  计算 ÷ 有效。批量越大这个数越难看——因为同一批里长短不齐，
                短的全被补到最长那个的长度。**它是本脚本里唯一的"优化空间"指标**：
                它高不代表后端差，只代表批处理策略浪费。

所以：**拿本脚本的数字去和别人比时，先问对方报的是哪一个。** 计划 §3.2 引用的
anygrep 那 ~1100 tokens/s（Ryzen 7 7700, int8 ONNX, 8 线程）没有说明是哪一种，
也没有说明是否批量——这正是计划里"口径未必一致"那句的由来。

---- 语料 ----------------------------------------------------------------

默认用**确定性合成语料**，不依赖任何外部文件：固定种子 → 固定文本 → 任何人在
任何机器上 clone 下来跑，拿到的是同一批输入。这是"可重跑"的前提。
合成方式刻意模仿真实分布：按计划 §4.5 的 `[chunk] target_tokens = 512` 切片，
每个 chunk 约 512 token，每个文档的**最后一个 chunk 是短的**。所以批内天然长短
不齐，padding 放大是真实的，不是造出来的。

用 `--corpus-dir <目录>` 可以换成自己的真实文档（只读 .txt/.md，纯文本）。
两种语料的数字**不可直接混用比较**，报告里会标出来用的是哪种。

---- 线程与进程 ------------------------------------------------------------

A2 要求"整机并行效率 ≥ 单进程的 8 倍"。这需要两段测量：单进程基线，与多进程。
计划 §4.5 的 `[perf] workers = min(8, cores//2)` 在本机 = 8。本机是
Ryzen 7 4800H：**8 物理核 / 16 逻辑核，内存 15.4 GiB**。8 个 worker 各加载一份
1.4GB 的模型是 11GB 量级，**内存可能是比 CPU 更早撞到的墙**，所以并行段会先量
单 worker 的常驻内存，再据此判断每个并行档位放不放得下；放不下就**跳过并说明**，
不做"跑崩了再说"的事。要强行跑用 `--force-parallel`。

退出码：全部档位跑完为 0，任一档位异常为 1。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import gc
import os
import platform
import random
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 环境必须在 import huggingface_hub **之前**设好（同 tools/fetch_model.py 的道理：
# 那个库在导入时就把这些值读进配置对象）。所以这里先扫一遍 argv，而不是等
# argparse。
# ---------------------------------------------------------------------------
_DOWNLOAD_ONNX = "--download-onnx" in sys.argv

os.environ.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
# 运行时的默认立场是**离线**：权重已在本地，索引/检索不该联网（A5、计划 §7.1）。
# 只有显式传 --download-onnx 的那一次才联网，且那一次的联网理由只有一个。
os.environ["HF_HUB_OFFLINE"] = "0" if _DOWNLOAD_ONNX else "1"

DEFAULT_MODEL_ID = "google/embeddinggemma-2"
DEFAULT_ONNX_REPO = "onnx-community/embeddinggemma-2-ONNX"

# 只取文本模型。这个仓库全量 6.2GB（含音频/视觉编码器的 fp16/q4/int8 各一份），
# 但文本 int8 只要两个文件、约 350MB。allow_patterns 不是优化，是**必需的**：
# 不写就会下 6.2GB。
ONNX_ALLOW = [
    "onnx/model_quantized.onnx",
    "onnx/model_quantized.onnx_data",
    "*.json",
    "*.jinja",
    "*.model",
]


# ===========================================================================
# 输出小工具（与 probe_deps.py 同一套风格：脚本只打印，排版交给重定向）
# ===========================================================================

def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def sub(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 74 - len(title)))


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GiB"


FAILURES: list[str] = []


def quieten() -> None:
    """关掉 HF 与 transformers 的进度条。

    它们用 `\\r` 原地刷新，重定向到报告文件时会变成一堆互相覆盖的残帧——报告是
    要给人读、要能复核的，不为了好看，而是因为**原始输出要能当作证据**。进度条
    属于噪声，且会把一次 12 档的扫描淹掉。子进程里同样要关（每个 worker 都会打）。
    """
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        import transformers.utils.logging as tf_logging
        tf_logging.disable_progress_bar()
        tf_logging.set_verbosity_error()
    except Exception:  # noqa: BLE001 —— 关不掉也不该让测量失败
        pass


def fail(where: str, exc: BaseException) -> None:
    """记一条失败并立刻打印。失败不进异常栈就往上冒——那样后半程的数字全丢。"""
    msg = f"{where}: {type(exc).__name__}: {exc}"
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


# ===========================================================================
# 机器事实
# ===========================================================================

def machine_facts() -> dict:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {
        "total_ram": m.ullTotalPhys,
        "avail_ram": m.ullAvailPhys,
        "load_pct": m.dwMemoryLoad,
        "logical": os.cpu_count() or 1,
    }


def physical_cores(logical: int) -> int:
    """物理核数。用 GetLogicalProcessorInformationEx 数 RelationProcessorCore 记录。

    为什么非要这个数：SMT 下 8 物理核会被报成 16 逻辑核，而**嵌入算力大致跟物理核
    走**，拿逻辑核数算并行度会高估一倍。计划 §3.1 的"16 核"就是这么来的。

    只数记录条数、不看记录里的 flags：既然查的是 RelationProcessorCore，返回的每
    一条本来就是**一个物理核**，再按 flags 过滤只会把核数越数越少。
    """
    try:
        fn = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
        RELATION_PROCESSOR_CORE = 0
        size = wt.DWORD(0)
        # 第一次调用只为拿所需缓冲区大小，必然"失败"（ERROR_INSUFFICIENT_BUFFER）
        fn(RELATION_PROCESSOR_CORE, None, ctypes.byref(size))
        if size.value <= 0:
            return logical
        buf = ctypes.create_string_buffer(size.value)
        if not fn(RELATION_PROCESSOR_CORE, buf, ctypes.byref(size)):
            return logical
        # 每条记录的头 8 字节：Relationship(DWORD) + Size(DWORD)，之后才是联合体。
        # 用 buf.raw 而不是 buf[i]：后者在 Py3 里给的是长度 1 的 bytes，与整数做位
        # 运算会直接抛 TypeError。
        raw, off, end = buf.raw, 0, size.value
        cores = 0
        while off + 8 <= end:
            rec_size = int.from_bytes(raw[off + 4:off + 8], "little")
            if rec_size < 8 or off + rec_size > end:
                break
            cores += 1
            off += rec_size
        return cores or logical
    except Exception:  # noqa: BLE001 —— 数不出来就用逻辑核，报告里能看出差别
        return logical


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_rss() -> int:
    """本进程峰值常驻内存。并行段要用它判断放不放得下 N 个 worker。

    **必须显式写 argtypes/restype。** `ctypes.windll` 的函数默认按 c_int 收尾，
    而这里的句柄是 64 位、`cb` 是 DWORD：不声明就会静默返回 0——函数"成功"了，
    数字是 0，然后并行段的整除直接 ZeroDivisionError。这个坑不报错，只给错答案。
    """
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
    fn.restype = wt.BOOL
    get_current = ctypes.windll.kernel32.GetCurrentProcess
    get_current.argtypes = []
    get_current.restype = wt.HANDLE

    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if not fn(get_current(), ctypes.byref(c), c.cb):
        raise OSError("GetProcessMemoryInfo 失败，取不到峰值内存")
    return int(c.PeakWorkingSetSize)


def git_version() -> str:
    def run(args: list[str]) -> str:
        return subprocess.run(args, capture_output=True, text=True, timeout=10,
                              check=True).stdout.strip()

    try:
        head = run(["git", "rev-parse", "--short", "HEAD"])
        dirty = run(["git", "status", "--porcelain"])
        # 工作区脏必须标出来：在未提交的代码上跑出的结果，别人 clone 不到同一份。
        return f"{head} + 工作区脏（{len(dirty.splitlines())} 条改动）" if dirty else f"{head} 干净"
    except Exception as exc:  # noqa: BLE001 —— 取不到版本不该让整轮失败，但要明说
        return f"（取不到：{type(exc).__name__}）"


def print_header(facts: dict, cores: int) -> None:
    section("P0 嵌入吞吐实测 — 原始输出")
    print(f"生成时间      : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"解释器        : {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台          : {platform.platform()}")
    print(f"CPU           : {cores} 物理核 / {facts['logical']} 逻辑核（SMT 倍率 "
          f"{facts['logical'] / cores:.1f}×）")
    print(f"内存          : 总 {human_bytes(facts['total_ram'])}，"
          f"空闲 {human_bytes(facts['avail_ram'])}，占用 {facts['load_pct']}%")
    print(f"HF_HOME       : {os.environ['HF_HOME']}")
    print(f"HF_HUB_OFFLINE: {os.environ['HF_HUB_OFFLINE']}"
          + ("   ← 本轮要下载 ONNX 成品，是唯一该联网的一次" if _DOWNLOAD_ONNX
             else "   ← 离线运行，权重走本地缓存"))
    print(f"代码版本      : {git_version()}")
    if facts["load_pct"] > 50:
        print(f"  ⚠ 测量时内存已占用 {facts['load_pct']}%：本机还有别的东西在跑，")
        print("    并行段的数字会偏悲观。报告里要连这一行一起看。")


# ===========================================================================
# 语料
# ===========================================================================

# 造中文正文用的词表。刻意混入英文与数字（真实语料就是这样，而且它们的分词
# 粒度完全不同，只测纯中文会低估 token 数）。词表固定 → 语料固定。
_WORDS = (
    "检索 索引 向量 语义 分词 文档 目录 磁盘 文件 缓存 模型 推理 吞吐 批量 线程 "
    "语料 切片 重叠 标题 正文 摘要 表格 图片 音频 视频 编码 解码 归一 量化 精度 "
    "查询 排序 融合 权重 阈值 召回 精确 延迟 并发 进程 内存 磁盘 网络 离线 本地 "
    "配置 参数 默认 覆盖 回退 重试 超时 异常 日志 追踪 调试 构建 部署 版本 依赖 "
    "search index vector semantic token document directory disk file cache model "
    "inference throughput batch thread corpus chunk overlap title body summary "
    "query rank fusion weight threshold recall latency concurrency process memory "
    "配置项 排除规则 就绪 降级 标注 命中 定位 片段 结果 用户 输入 输出 状态 阶段 "
).split()

_PUNCT = "，。；：、？！"


def build_corpus(seed: int, budget_tokens: int, corpus_dir: str | None, tok) -> list[str]:
    """造出总长约 budget_tokens 的 chunk 列表（chunk 是嵌入口径的单位）。

    真实分布：大部分 chunk 是满的 512 token，每个文档最后一个 chunk 是短尾。
    这里就按这个模型造，而不是造一堆等长文本——等长文本的 padding 放大永远是 1.0，
    会把批量大小的代价完全藏起来，那样测出来的最优批量是假的。
    """
    if corpus_dir:
        texts = []
        for p in sorted(Path(corpus_dir).rglob("*")):
            if p.suffix.lower() in (".txt", ".md") and p.is_file():
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if body.strip():
                    texts.append(body)
        if not texts:
            raise SystemExit(f"[FAIL] {corpus_dir} 里没有可读的 .txt/.md")
        return texts

    rng = random.Random(seed)

    # 先造一大块确定性正文，再按 token 切——比"按句子拼到够长"快得多，也精确得多。
    blob_parts = []
    while len(blob_parts) < 4096:
        n = rng.randint(6, 18)
        blob_parts.append("".join(rng.choice(_WORDS) for _ in range(n))
                          + rng.choice(_PUNCT))
    blob = "".join(blob_parts)
    ids = tok(blob, add_special_tokens=False)["input_ids"]
    if len(ids) < budget_tokens:
        raise SystemExit("[FAIL] 正文池不够长，把预算调小或把池子改大")

    target = 512  # 计划 §4.5 `[chunk] target_tokens`
    chunks: list[str] = []
    used = 0
    pos = 0
    while used < budget_tokens:
        # 每个"文档"随机长 200–3000 token，切成若干 512 的满片 + 一个短尾
        doc_len = rng.randint(200, 3000)
        remaining = doc_len
        while remaining > 0 and used < budget_tokens:
            take = min(target, remaining)
            if pos + take > len(ids):
                pos = 0
            piece = tok.decode(ids[pos:pos + take])
            pos += take
            remaining -= take
            chunks.append(piece)
            # 真实 token 数要重新数：decode→encode 不一定严格往返，不能拿 take 当数
            used += len(tok(piece, add_special_tokens=False)["input_ids"])
    return chunks


def corpus_stats(texts: list[str], tok, prefix_len: int) -> dict:
    lens = [len(tok(t, add_special_tokens=False)["input_ids"]) for t in texts]
    lens.sort()
    n = len(lens)
    return {
        "n": n,
        "total": sum(lens),
        "min": lens[0], "max": lens[-1], "median": lens[n // 2],
        "prefix_len": prefix_len,
        # 含前缀的"有效 token"总数：这才是换算索引耗时的分母
        "total_with_prefix": sum(lens) + prefix_len * n,
    }


# ===========================================================================
# 一个"档位"跑完的结果
# ===========================================================================

class Run:
    def __init__(self, backend: str, threads: int, batch: int, dtype: str = "—"):
        self.backend, self.threads, self.batch = backend, threads, batch
        self.dtype = dtype
        self.load_s = 0.0
        self.seconds = 0.0
        self.useful = 0        # 含前缀、不含 padding
        self.computed = 0      # 含 padding，硬件真算的
        self.embeddings = None
        self.rss = 0           # 本档位结束时进程的峰值常驻内存，用于算"能并行几个"

    @property
    def useful_per_s(self) -> float:
        return self.useful / self.seconds if self.seconds else 0.0

    @property
    def computed_per_s(self) -> float:
        return self.computed / self.seconds if self.seconds else 0.0

    @property
    def padding_amp(self) -> float:
        return self.computed / self.useful if self.useful else 0.0

    def line(self) -> str:
        mem = f" {human_bytes(self.rss):>9s}" if self.rss else " " * 9 + " "
        return (f"  {self.backend:5s} {self.dtype:9s} 线程={self.threads:<3d} 批={self.batch:<3d} "
                f"| 有效 {self.useful_per_s:8.1f} tok/s | 计算 {self.computed_per_s:8.1f} tok/s "
                f"| padding×{self.padding_amp:4.2f} | {self.seconds:6.2f}s | RSS{mem}")


# ===========================================================================
# 后端一：sentence-transformers
# ===========================================================================

_ST_MODEL = None


def release_models(keep: str = "") -> None:
    """把已加载的模型放掉，`keep` 可留一个（"st" 或 "onnx"）。

    **为什么非要有这个**：一轮跑下来本工具要依次加载 **三份** 模型——ST fp32、
    ST bfloat16（dtype 探针）、ONNX int8——而它们默认全部同时留在进程里。本机
    15.37 GiB、可用约 4 GiB 时，峰值顶到 **7.49 GiB** 之后 onnxruntime 直接段错误
    退出（2026-10-14 实测 `EXIT=139`，RSS 轨迹 4.31 → 7.49 GiB）。

    三份模型**没有必要同时在手**：ST 的向量在扫描时就已取出来存进 `Run.embeddings`，
    后面第 5 步的跨后端比对用的是 numpy 数组，不是模型对象。所以在段落边界处放掉
    即可，代价是重跑同一段要重新加载。

    注意 `main()` 里的局部引用（`model` / `sess`）也按着不放，光清这两个全局变量
    不够；调用方需一并置 None。`gc.collect()` 是必须的——torch 的权重之间是循环
    引用的，靠引用计数回收不掉。
    """
    global _ST_MODEL, _ONNX_SESS
    before = peak_rss()
    if keep != "st":
        _ST_MODEL = None
    if keep != "onnx":
        _ONNX_SESS = None
    gc.collect()
    print(f"  [内存] 释放模型（保留={keep or '无'}）：释放前峰值 {human_bytes(before)}"
          f" → 之后 {human_bytes(peak_rss())}"
          f"（峰值只增不减，回收量看下一档位实际涨了多少）")


# 各档位末列的 RSS 是**进程峰值**（GetProcessMemoryInfo），不是"这一档单独占多少"。
# 同一进程里各档位共用一份权重，峰值只增不减，所以它是个上界；要读单档真实占用，
# 看每段第一行的值，或者只跑那一档。这个数直接决定"最多能并行几个 worker"，
# 所以宁可把口径说清楚，也不要让人拿它当精确值用。
RSS_NOTE = (
    "    注：末列 RSS 是**进程峰值**，同一进程内只增不减——它是个上界，不是这一档\n"
    "    的独占占用。要读单档真实占用，看本段第一行，或者单独只跑那一档。"
)

# 冷启动加载耗时按后端记一次。各档位复用同一个模型对象，所以后几次的 load_s
# 都是 0，直接对 runs 取 max 会把冷启动成本显示成 0——那个数是要写进报告的
# （CLI 每次起进程都要付），不能被复用掩盖掉。
COLD_LOAD: dict[str, float] = {}


def _st_load(model_id: str, cache: bool, dtype_name: str = "float32"):
    """加载模型一次并复用。

    缓存键里**故意不含线程数**：`torch.set_num_threads()` 是进程级设置，模型对象
    本身与线程数无关。把线程数放进键里会让每个档位重新加载一遍 1.4GB 权重
    （~6 秒 × 12 档），白白拉长整轮，还让"加载耗时"这个数被稀释成噪声。
    但**必须含 dtype**：不同 dtype 是不同的权重张量，是不同的对象。

    dtype 不是小事，本机的实测结论是反直觉的：
      模型 config 写的是 bfloat16，不传 dtype 就按 bf16 加载；而本机 CPU 是
      Zen 2（Ryzen 7 4800H），**没有 bf16 硬件支持**，torch 只能软模拟，
      实测比 float32 慢约 4.5 倍。而且 bf16 下的归一化只做到 3 位有效数字，
      出来的向量范数是 1.0017 而不是 1.0，跨后端比对会看到余弦 > 1。
    所以默认走 float32（计划 §2.1 的 M2 明确允许 bf16/fp32，禁的是 fp16）。
    """
    import torch
    import sentence_transformers as st

    global _ST_MODEL
    key = (model_id, dtype_name)
    if cache and _ST_MODEL is not None and _ST_MODEL[0] == key:
        return _ST_MODEL[1], 0.0
    kw = {} if dtype_name == "default" else {
        "model_kwargs": {"dtype": getattr(torch, dtype_name)}}
    t0 = time.perf_counter()
    model = st.SentenceTransformer(model_id, **kw)
    load_s = time.perf_counter() - t0
    COLD_LOAD.setdefault(f"ST/{dtype_name}", load_s)
    if cache:
        _ST_MODEL = (key, model)
    return model, load_s


def run_st(texts: list[str], model_id: str, threads: int, batch: int,
           warmup: int, cache: bool, prompt_name: str, max_seq: int,
           dtype_name: str = "float32") -> Run:
    """跑一轮 ST：预热在计时之外，token 数按 ST 的真实口径另算。

    口径依据 sentence_transformers/base/modality.py:716 —— ST 拼前缀就是
    `prompt + text` **直接相接**，不加空格（前缀字符串自己以空格结尾）。这里
    必须用同一条规则数 token，否则报出来的有效吞吐会跟前缀长度一起漂移。
    """
    import torch

    torch.set_num_threads(threads)  # 进程级；必须在推理前设
    model, load_s = _st_load(model_id, cache, dtype_name)
    # ST 对这个模型**不会**自己截断：tokenizer_config 的 model_max_length 是
    # 1e36，而 ST 的上限逻辑只读顶层 config.max_position_embeddings（本模型把它
    # 藏在 text_config 里），所以取不到就保持 1e36。必须由调用方设死。
    model.max_seq_length = max_seq
    tok = model.tokenizer
    prefix = model.prompts[prompt_name]
    run = Run("ST", threads, batch, dtype_name)
    run.load_s = load_s

    def encode(chunk: list[str]):
        return model.encode(chunk, batch_size=batch, prompt_name=prompt_name,
                            normalize_embeddings=True, show_progress_bar=False,
                            convert_to_numpy=True)

    for _ in range(warmup):
        # 预热用真实长度的样本：第一次调用会选算子、分配内存、拉热页缓存。放进来
        # 会让第一个档位凭空变慢、后面的档位白占便宜——档位之间就不可比了。
        encode(texts[: max(1, min(batch, len(texts)))])

    run.useful = sum(len(tok(prefix + t, add_special_tokens=False)["input_ids"]) for t in texts)
    # "硬件真算的 token" = 每批补到该批最长。这个数只能在这儿按语料算出来，
    # 事后从结果反推不出来。它与 batched encode 共用同一批切分，所以是一致的。
    # 用 len(chunk) 而不是 batch：**最后一批往往不满**，ST 建的是 len(chunk) 行的
    # 张量，不是 batch 行。写成 batch 会把不满的最后一批按满批算，凭空放大
    # padding 倍数——语料越小这个偏差越离谱。
    total = 0
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        lens = [len(x) for x in tok([prefix + t for t in chunk],
                                    add_special_tokens=False)["input_ids"]]
        total += len(chunk) * max(lens)
    run.computed = total

    t0 = time.perf_counter()
    emb = None
    for i in range(0, len(texts), batch):
        emb = encode(texts[i:i + batch])
    run.seconds = time.perf_counter() - t0
    run.rss = peak_rss()
    run.embeddings = emb
    return run


# ===========================================================================
# 后端二：ONNX int8（onnxruntime）
# ===========================================================================

def fetch_onnx(repo_id: str) -> str:
    from huggingface_hub import snapshot_download

    section("第 0 步  取 ONNX int8 成品")
    print(f"  仓库   : {repo_id}")
    print(f"  只取   : {', '.join(ONNX_ALLOW)}")
    print(f"  说明   : 该仓库全量约 6.2GB（含音频/视觉编码器的多种量化），")
    print(f"           文本 int8 只要约 350MB。allow_patterns 是必需的，不是优化。")
    t0 = time.perf_counter()
    path = snapshot_download(repo_id=repo_id, allow_patterns=ONNX_ALLOW)
    print(f"  落点   : {path}")
    print(f"  耗时   : {time.perf_counter() - t0:.1f} s")
    return path


def onnx_paths(repo_id: str) -> tuple[str, str]:
    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=repo_id, allow_patterns=ONNX_ALLOW,
                             local_files_only=not _DOWNLOAD_ONNX)
    model = os.path.join(path, "onnx", "model_quantized.onnx")
    if not os.path.exists(model):
        raise FileNotFoundError(
            f"没有 {model}。先跑一次带 --download-onnx 的（本轮 HF_HUB_OFFLINE="
            f"{os.environ['HF_HUB_OFFLINE']}）")
    return path, model


_ONNX_SESS = None


def _onnx_load(model_file: str, threads: int, cache: bool):
    import onnxruntime as ort

    global _ONNX_SESS
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads       # 算子内部的并行度 = 我们说的"线程数"
    so.inter_op_num_threads = 1             # 算子之间不并行：嵌入是单条串行链
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    if cache and _ONNX_SESS is not None and _ONNX_SESS[0] == (model_file, threads):
        return _ONNX_SESS[1], 0.0
    t0 = time.perf_counter()
    sess = ort.InferenceSession(model_file, sess_options=so, providers=["CPUExecutionProvider"])
    load_s = time.perf_counter() - t0
    COLD_LOAD.setdefault("ONNX/int8", load_s)  # 冷启动也要在这儿记，见 _st_load
    if cache:
        _ONNX_SESS = ((model_file, threads), sess)
    return sess, load_s


def onnx_io_names(sess) -> tuple[list[str], list[str]]:
    return ([i.name for i in sess.get_inputs()], [o.name for o in sess.get_outputs()])


def onnx_extra_inputs(sess) -> dict[str, tuple]:
    """图里除了文本输入之外的那些入口，以及我们打算喂给它们的形状。

    这张图的多模态入口（image/video/audio_features）是**必需输入**，不是可选的：
    只喂 input_ids 和 attention_mask 会直接报缺输入。我们只跑文本，所以给它们 0
    长度的空张量。**不写死名字**，按"除了文本那两个之外都是多模态"来判——图的
    输入名随导出方变化，写死了换个成品就会静默失效。
    """
    extra = {}
    for i in sess.get_inputs():
        if i.name in ("input_ids", "attention_mask"):
            continue
        extra[i.name] = tuple(d if isinstance(d, int) else 0 for d in i.shape), i.type
    return extra


def run_onnx(texts: list[str], repo_dir: str, model_file: str, threads: int, batch: int,
             warmup: int, cache: bool, prefix: str, dim: int, max_seq: int) -> Run:
    """ONNX 一轮。

    这个图输出什么**不靠猜**：onnx-community 的导出有的带 `sentence_embedding`
    （图里已经做了池化），有的只给 `last_hidden_state`（池化要自己补）。两者
    算的不是一回事，选错了速度对比就没有意义。所以先读图的实际输出名，按实际
    情况走，并把走了哪条路**打印出来**。
    """
    import numpy as np
    from tokenizers import Tokenizer

    sess, load_s = _onnx_load(model_file, threads, cache)
    inames, onames = onnx_io_names(sess)
    tk = Tokenizer.from_file(os.path.join(repo_dir, "tokenizer.json"))
    # pad id 从这个词表自身查，不写死 0：写死了哪天换了词表就会静默错位。
    # tokenizer_config.json 只给了 pad_token 的名字（`<pad>`），pad_token_id 是 null。
    pad_id = tk.token_to_id("<pad>")
    if not isinstance(pad_id, int):
        raise RuntimeError("词表里查不到 <pad>，没法安全地做批量 padding")
    tk.enable_padding(pad_id=pad_id, pad_token="<pad>")
    tk.enable_truncation(max_length=max_seq)

    run = Run("ONNX", threads, batch, "int8")
    run.load_s = load_s
    run.graph_out = ",".join(onames)
    run.pooling = "图内 sentence_embedding" if "sentence_embedding" in onames \
        else "脚本内 mean-pool(last_hidden_state)"

    # 多模态空入口：见 onnx_extra_inputs 的注释（它们是必需输入，不给会直接报错）
    extra_inputs = {
        name: np.zeros(shape, dtype=np.float32 if "float" in typ else np.int64)
        for name, (shape, typ) in onnx_extra_inputs(sess).items()
    }

    def encode(chunk: list[str]):
        enc = tk.encode_batch([prefix + t for t in chunk])
        feed = dict(extra_inputs)
        if "input_ids" in inames:
            feed["input_ids"] = np.array([e.ids for e in enc], dtype=np.int64)
        if "attention_mask" in inames:
            feed["attention_mask"] = np.array([e.attention_mask for e in enc], dtype=np.int64)
        out = sess.run(None, feed)
        named = dict(zip(onames, out))
        if "sentence_embedding" in named:
            v = named["sentence_embedding"]
        else:
            hidden = named.get("last_hidden_state")
            if hidden is None:
                hidden = out[0]
            mask = feed["attention_mask"].astype(np.float32)[..., None]
            v = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        v = np.asarray(v, dtype=np.float32)[:, :dim]
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / np.clip(n, 1e-12, None)

    # 把多模态入口这件事报出去：它是这张图的一个真实事实，报告里要留痕。
    run.extra_inputs = {k: list(v.shape) for k, v in extra_inputs.items()}

    for _ in range(warmup):
        encode(texts[: max(1, min(batch, len(texts)))])

    run.useful = sum(len(tk.encode(prefix + t).ids) for t in texts)
    total = 0
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        lens = [len(tk.encode(prefix + t).ids) for t in chunk]
        # 同 run_st：按 len(chunk) 而不是 batch，最后一批不满时不算成满批
        total += len(chunk) * max(lens)
    run.computed = total

    t0 = time.perf_counter()
    emb = None
    for i in range(0, len(texts), batch):
        emb = encode(texts[i:i + batch])
    run.seconds = time.perf_counter() - t0
    run.rss = peak_rss()
    run.embeddings = emb
    return run


# ===========================================================================
# 多进程并行（A2）
# ===========================================================================

def _parallel_worker(payload: dict) -> dict:
    """子进程入口。必须是模块级函数：Windows 的 spawn 要按名字重新导入它。"""
    backend, threads, batch, texts = (payload["backend"], payload["threads"],
                                      payload["batch"], payload["texts"])
    warmup = payload["warmup"]
    quieten()
    t_import = time.perf_counter()
    try:
        if backend == "ST":
            run = run_st(texts, payload["model_id"], threads, batch, warmup,
                         cache=False, prompt_name=payload["prompt_name"],
                         max_seq=payload["max_seq"], dtype_name=payload["st_dtype"])
        else:
            run = run_onnx(texts, payload["repo_dir"], payload["model_file"], threads,
                           batch, warmup, cache=False, prefix=payload["prefix"],
                           dim=payload["dim"], max_seq=payload["max_seq"])
    except Exception as exc:  # noqa: BLE001 —— 子进程的异常要变成数据回传，不能只崩
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "seconds": 0.0,
                "useful": 0, "load_s": 0.0, "rss": peak_rss(),
                "setup_s": time.perf_counter() - t_import}
    return {"ok": True, "seconds": run.seconds, "useful": run.useful,
            "computed": run.computed, "load_s": run.load_s,
            "rss": peak_rss(), "setup_s": time.perf_counter() - t_import}


def parallel_sweep(base: dict, workers_wanted: list[int], facts: dict, force: bool,
                   threads_override: int | None, cores: int,
                   single: dict[tuple[str, str, int, int], float]) -> list[dict]:
    """按档位跑多进程。返回每个档位的结果，含被跳过的（跳过要说明理由）。

    **每 worker 的线程数按 worker 数反推**（tpw = 物理核数 // workers），这是一处
    刻意的修正。第一版把每 worker 的线程数固定成扫描出来的"最快单进程档位"，于是
    8 个 worker 各开 8 条线程 = 64 条线程抢 8 个物理核，测出来的"加速比"只有 1.15×
    ——那不是并行没用，是**问题没问对**：8 核的机器上，8 个 worker 的正确配法本来
    就是每 worker 1 条线程。

    并行效率的分母是"**同线程数**的单进程"（tpw 条线程、1 个进程），不是"最快档位"。
    拿最快档位当分母会把线程级并行的功劳算进进程级并行里。

    `single` 是单进程扫描已经量到的 (backend,dtype,threads,batch) → tok/s，能查到
    就直接用，查不到才补测一次——免得再花一遍时间。
    """
    import multiprocessing as mp

    out = []
    texts = base["texts"]

    # 先量单个 worker 的真实内存，再据此判断放不放得下。计划里的 workers=8 是
    # 按 16 核算的，本机 8 物理核、内存又只剩几个 G —— 内存很可能先撞墙。
    probe_payload = dict(base, texts=texts[: min(len(texts), 8)], threads=1)
    with mp.get_context("spawn").Pool(1) as pool:
        probe = pool.map(_parallel_worker, [probe_payload])[0]
    if not probe["ok"]:
        print(f"  [FAIL] 单 worker 试跑失败：{probe['error']}")
        FAILURES.append(f"并行前置试跑失败：{probe['error']}")
        return out
    rss = probe["rss"]
    print(f"  单 worker 峰值常驻内存: {human_bytes(rss)}")
    # 这里的上限必须用**当下**的可用内存算。用整轮开始时的快照会给出一个比实际
    # 乐观得多的数字（本机实测：开头 4.95 GiB、跑完 ONNX 扫描后 6.90 GiB，
    # 同一行文字相差 2 GiB），读者据此判断"放得下几个"会被误导。
    avail_now = machine_facts()["avail_ram"]
    print(f"  可用内存 {human_bytes(avail_now)} ÷ 单 worker {human_bytes(rss)} "
          f"= 最多约 {int(avail_now / rss)} 个 worker 放得下")
    print(f"  总物理内存 {human_bytes(machine_facts()['total_ram'])}："
          f"计划 §3.1 设想的 workers=8 需要 {human_bytes(rss * 8)}，"
          f"占整机 {rss * 8 / machine_facts()['total_ram'] * 100:.0f}%")
    print(f"  ⚠ 并行段是本工具唯一会**同时按下多份模型**的地方。本机实测它两次把"
          f"系统拖到无响应（2026-10-14），故默认只在余量充足时跑；要在别的机器上"
          f"复现，先确认可用内存 > 单 worker × workers × 1.5。")

    ctx = mp.get_context("spawn")

    # 放行一条并行档位要同时满足两件事，缺一不可：
    #
    #   1. 算上放大系数之后放得下。试跑只嵌 8 条文本，而真 worker 要嵌整条语料，
    #      激活与临时缓冲都更多，所以按 1.3 倍估。
    #   2. **跑完之后系统还剩得下**。这一条是本机用两次死机换来的：2026-10-14 的
    #      实测里，workers=1 在"需要 2.01 GiB、可用 6.90 GiB"这种看上去很宽的
    #      余量下，仍然把整台机器拖到无响应。本机只有 15.37 GiB，且同时跑着
    #      浏览器/编辑器，可用内存是借来的。旧判据 `need > avail * 0.9` 在这里
    #      会放行，等于赌运气。
    #
    # 保留额取物理内存的 25%，至少 4 GiB：留给 OS、页缓存和别的应用。
    total = machine_facts()["total_ram"]
    reserve = max(4 * 1024 ** 3, int(total * 0.25))

    for w in workers_wanted:
        tpw = threads_override or max(1, cores // w)
        total_threads = w * tpw

        # 每个档位开跑前**重新读一次**可用内存：本机还有别的东西在跑，可用内存在
        # 几分钟里能从 5GB 掉到 1GB。拿整轮开始时的那一个快照做判断，会在内存已经
        # 被别人吃掉之后照样放行。
        avail = machine_facts()["avail_ram"]
        need = int(rss * w * 1.3)
        room = avail - reserve
        if need > room and not force:
            print(f"\n  [SKIP] workers={w}：需要约 {human_bytes(need)}"
                  f"（单 worker {human_bytes(rss)} × {w} × 1.3），"
                  f"而可用 {human_bytes(avail)} 扣掉保留额 {human_bytes(reserve)} "
                  f"只剩 {human_bytes(max(0, room))}。")
            print(f"         跳过而不是硬跑——本机还有别的东西在用内存，跑崩会牵连它们。")
            print(f"         要强行跑：--force-parallel（后果自负，本机实测会死机）")
            out.append({"workers": w, "skipped": True, "need": need,
                        "avail": avail, "reserve": reserve, "tpw": tpw})
            continue

        # 单进程基线：同 backend、同 dtype、同线程数。扫描里量过就直接取。
        key = (base["backend"], base.get("dtype", "—"), tpw, base["batch"])
        baseline = single.get(key)
        if baseline is None:
            try:
                baseline = _measure_one(base, tpw)
                single[key] = baseline
            except Exception as exc:  # noqa: BLE001
                fail(f"并行基线 线程={tpw}", exc)
                continue

        shelf = [texts[i::w] for i in range(w)]  # 交错切分：各 worker 负载更均衡
        payloads = [dict(base, texts=shelf[i], threads=tpw) for i in range(w)]
        sub(f"并行 workers={w} × 每 worker {tpw} 线程 = {total_threads} 线程"
            f"（物理核 {cores}）")
        if total_threads > cores:
            print(f"  ⚠ 线程数 {total_threads} 超过物理核 {cores}，属于超订（靠 SMT 兜底）")
        t0 = time.perf_counter()
        try:
            with ctx.Pool(w) as pool:
                res = pool.map(_parallel_worker, payloads)
        except Exception as exc:  # noqa: BLE001
            fail(f"workers={w}", exc)
            out.append({"workers": w, "error": f"{type(exc).__name__}: {exc}"})
            continue
        wall = time.perf_counter() - t0

        bad = [r for r in res if not r["ok"]]
        if bad:
            print(f"  [FAIL] {len(bad)}/{w} 个子进程失败，首个：{bad[0]['error']}")
            out.append({"workers": w, "error": bad[0]["error"]})
            continue

        # 各 worker 并发跑，所以吞吐按**最慢那个**算，不是按总和除以墙钟
        slow = max(r["seconds"] for r in res)
        useful = sum(r["useful"] for r in res)
        computed = sum(r["computed"] for r in res)
        speedup = (useful / slow) / baseline if baseline else 0.0
        out.append({
            "workers": w, "tpw": tpw, "total_threads": total_threads,
            "useful": useful, "computed": computed, "slowest_s": slow,
            "wall_s": wall, "rss": max(r["rss"] for r in res),
            "load_s": max(r["load_s"] for r in res),
            "baseline": baseline, "speedup": speedup,
        })
        print(f"  单进程基线（{tpw} 线程）: {baseline:.1f} tok/s")
        print(f"  最慢 worker 编码 {slow:.2f}s，墙钟 {wall:.2f}s（含各自加载模型）")
        print(f"  合计有效 {useful:,} token → {useful / slow:8.1f} tok/s"
              f"  加速 {speedup:.2f}×")
        print(f"  各 worker 峰值内存 最大 {human_bytes(max(r['rss'] for r in res))}"
              f"，合计约 {human_bytes(sum(r['rss'] for r in res))}")
    return out


def _measure_one(base: dict, tpw: int) -> float:
    """在子进程里补测一次单进程吞吐，只为拿并行段的分母。"""
    import multiprocessing as mp

    with mp.get_context("spawn").Pool(1) as pool:
        r = pool.map(_parallel_worker, [dict(base, threads=tpw)])[0]
    if not r["ok"]:
        raise RuntimeError(r["error"])
    return r["useful"] / r["seconds"]


# ===========================================================================
# main
# ===========================================================================

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P0 嵌入吞吐实测（F0-01/F0-02/F0-03，A2）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["st", "onnx", "both"], default="both")
    p.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    p.add_argument("--onnx-repo", default=DEFAULT_ONNX_REPO)
    p.add_argument("--download-onnx", action="store_true",
                   help="本轮联网取 ONNX 成品（只需第一次）")
    p.add_argument("--prompt-name", default="Retrieval-document",
                   help="文档侧任务前缀名，取自模型自带的 config_sentence_transformers.json")
    p.add_argument("--budget-tokens", type=int, default=16000,
                   help="每档位要嵌入的有效 token 总量（默认 16000，约 30–60 秒一档）")
    p.add_argument("--seed", type=int, default=20261014)
    p.add_argument("--corpus-dir", default=None,
                   help="改用真实文档目录（只读 .txt/.md）。与合成语料的数字不可直接比")
    p.add_argument("--batches", default="8,16,32")
    p.add_argument("--threads", default="1,4,8,16")
    p.add_argument("--warmup", type=int, default=1, help="计时前的预热批数")
    p.add_argument("--st-dtype", default="float32",
                   choices=["float32", "bfloat16", "float16", "default"],
                   help="ST 后端的权重精度。默认 float32——**不要改成 default**，"
                        "那会按模型 config 走 bfloat16，而本机 CPU 没有 bf16 硬件，"
                        "实测慢约 4.5 倍。float16 明确禁止（M2：激活值 NaN）")
    p.add_argument("--dtype-probe-batch", type=int, default=16,
                   help="第 3b 步 dtype 对比用的批量")
    p.add_argument("--max-seq", type=int, default=8192,
                   help="序列长度上限。必须显式给：ST 对本模型不会自己截断（见 run_st 注释）。"
                        "本次语料的 chunk 都在 512 以内，所以它只是安全网，不参与吞吐计算")
    p.add_argument("--parallel", action="store_true",
                   help="跑多进程并行段（A2 的 8 倍效率就在这里量）")
    p.add_argument("--parallel-workers", default="1,2,4,8")
    p.add_argument("--threads-per-worker", type=int, default=None,
                   help="默认不设：每 worker 的线程数按 物理核数//workers 反推，"
                        "使总线程数 ≈ 物理核数。手动指定会失去这个对齐，只在"
                        "排查超订/欠订时才用")
    p.add_argument("--force-parallel", action="store_true",
                   help="内存不够也照跑（可能把本机拖垮，只在确认过的时候用）")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    quieten()
    facts = machine_facts()
    cores = physical_cores(facts["logical"])
    print_header(facts, cores)

    import torch
    import torchvision  # noqa: F401 —— 只为让它被导入；见文件末尾"为什么依赖 torchvision"
    import sentence_transformers as st

    section("第 1 步  环境与版本")
    print(f"  torch               : {torch.__version__}")
    print(f"  sentence-transformers: {st.__version__}")
    try:
        import onnxruntime as ort
        print(f"  onnxruntime         : {ort.__version__}（providers: "
              f"{', '.join(ort.get_available_providers())}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  onnxruntime         : 不可用（{type(exc).__name__}）")
    print(f"  torch 默认线程数     : {torch.get_num_threads()}")

    onnx_dir = onnx_file = None
    if args.backend in ("both", "onnx"):
        try:
            if args.download_onnx:
                fetch_onnx(args.onnx_repo)
            onnx_dir, onnx_file = onnx_paths(args.onnx_repo)
            print(f"  ONNX 成品            : {onnx_file}")
        except Exception as exc:  # noqa: BLE001
            fail("取 ONNX 成品", exc)
            if args.backend == "onnx":
                return 1
            print("  改为只跑 ST 后端。")

    section("第 2 步  语料")
    # 只加载 tokenizer 来造语料与计数，不加载权重：造语料不该等 1.4GB 权重。
    from transformers import AutoTokenizer

    tk_fast = AutoTokenizer.from_pretrained(args.model_id)

    class _Tk:
        """把 HF tokenizer 收成本脚本要的两动作：编码、解码。

        统一 `add_special_tokens=False`：本脚本数的是**内容 token**，BOS/EOS 由
        后端各自加，不该混进"有效 token"里——否则两个后端的口径会差一两个 token，
        而"不比别人的数字"本来就是这份报告的要点。注意**不**在这里手写前缀，
        前缀从模型自带的 config 里读，见下面。
        """

        def __call__(self, xs, add_special_tokens: bool = False):
            return {"input_ids": tk_fast(xs, add_special_tokens=add_special_tokens)["input_ids"]}

        def decode(self, ids):
            return tk_fast.decode(ids, skip_special_tokens=True)

    tk = _Tk()
    texts = build_corpus(args.seed, args.budget_tokens, args.corpus_dir, tk)

    # 前缀从模型自带的 config 里读，不在这里手写。"写错不会报错，只会静默降低
    # 召回"——所以真相来源只能是模型自己公布的那一份。
    import json
    from huggingface_hub import hf_hub_download
    cfg_path = hf_hub_download(args.model_id, "config_sentence_transformers.json")
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    prompt = cfg["prompts"][args.prompt_name]
    prefix = prompt + " " if not prompt.endswith(" ") else prompt
    print(f"  语料来源      : {'真实目录 ' + args.corpus_dir if args.corpus_dir else '确定性合成（种子 ' + str(args.seed) + '）'}")
    print(f"  任务前缀      : {prefix!r}   ← 读自 config_sentence_transformers.json，未手写")
    stats = corpus_stats(texts, tk, len(tk_fast(prefix, add_special_tokens=False)["input_ids"]))
    print(f"  chunk 数      : {stats['n']:,}")
    print(f"  有效 token     : {stats['total_with_prefix']:,}（含每片前缀 {stats['prefix_len']} token）")
    print(f"  chunk 长度     : 最短 {stats['min']}，中位 {stats['median']}，最长 {stats['max']}")
    print(f"  注：所有档位嵌入的是**同一批**文本，工作量相同，因此可以直接比速度。")

    batches = [int(x) for x in args.batches.split(",")]
    threads = [int(x) for x in args.threads.split(",")]

    runs: list[Run] = []

    if args.backend in ("st", "both"):
        section("第 3 步  sentence-transformers 后端")
        model, load_s = _st_load(args.model_id, cache=True, dtype_name=args.st_dtype)
        print(f"  权重精度      : {args.st_dtype}（实际张量 dtype = {model[0].auto_model.dtype}）")
        print(f"  模型加载      : {load_s:.2f} s（冷启动，一次性成本；之后各档位复用）")
        print(f"  维度          : {model.get_embedding_dimension()}")
        print(f"  similarity    : {model.similarity_fn_name}")
        print(f"  批 / 线程扫描 :")
        print(RSS_NOTE)
        for b in batches:
            for t in threads:
                try:
                    r = run_st(texts, args.model_id, t, b, args.warmup,
                               cache=True, prompt_name=args.prompt_name,
                               max_seq=args.max_seq, dtype_name=args.st_dtype)
                    runs.append(r)
                    print(r.line())
                except Exception as exc:  # noqa: BLE001
                    fail(f"ST 批={b} 线程={t}", exc)

        # ---- 3b：dtype 的影响（本机上这个因素比批/线程都大）----------------
        sub("3b  权重精度对吞吐的影响（固定 线程=8, 批=" + str(args.dtype_probe_batch) + "）")
        print(f"  为什么单独量这一项：模型 config 声明的 dtype 是 bfloat16，而本机")
        print(f"  CPU 是 {cores} 核 Zen 2 —— **没有 bf16 硬件**，torch 只能软模拟。")
        print(f"  不传 dtype 直接构造 SentenceTransformer 就会掉进这个坑里，而且")
        print(f"  **不会报错**，只会慢。这是 P0 最该抓出来的那类问题。")
        for d in ("float32", "bfloat16"):
            # 两种 dtype 是两份独立权重。不先放掉前一份，探针期间就会同时按下两份
            # ST（本机实测峰值 6.54 GiB），而本机可用只有 4 GiB 出头——这一步紧挨着
            # 后面的 ONNX，是整轮内存最险的地方。放掉之后峰值由"一份 ST"封顶。
            release_models()
            try:
                r = run_st(texts, args.model_id, 8, args.dtype_probe_batch, args.warmup,
                           cache=False, prompt_name=args.prompt_name,
                           max_seq=args.max_seq, dtype_name=d)
                runs.append(r)
                print(r.line())
            except Exception as exc:  # noqa: BLE001
                fail(f"ST dtype={d}", exc)
        probe = [r for r in runs if r.backend == "ST" and r.threads == 8
                 and r.batch == args.dtype_probe_batch]
        f32 = next((r for r in probe if r.dtype == "float32"), None)
        b16 = next((r for r in probe if r.dtype == "bfloat16"), None)
        if f32 and b16 and b16.useful_per_s:
            print(f"  → float32 是 bfloat16 的 {f32.useful_per_s / b16.useful_per_s:.2f} 倍")
            print(f"  → 结论：本项目的 ST 后端**必须显式传 dtype=float32**（计划 §2.1 的")
            print(f"    M2 明确允许 bf16/fp32，禁的只是 fp16）。下面所有档位都走 float32。")

    dim = 768  # 计划 §4.5 `[embed] dim`；下面拿实际向量形状核对
    if args.backend in ("both", "onnx") and onnx_file:
        section("第 4 步  ONNX int8 后端")
        # 换后端之前先把 ST 放掉。ST fp32 常驻约 4.3 GiB，不清掉的话 ONNX 会在它
        # 头上再要一份，峰值冲到 7.5 GiB —— 本机就是这么段错误的（EXIT=139）。
        # 第 5 步的跨后端比对用的是已存下的 numpy 向量，不需要模型还在。
        model = None
        release_models()
        try:
            import onnxruntime as ort
            sess, load_s = _onnx_load(onnx_file, threads[-1], cache=True)
            inames, onames = onnx_io_names(sess)
            print(f"  成品文件      : {onnx_file}（{human_bytes(os.path.getsize(onnx_file))}）")
            print(f"  模型加载      : {load_s:.2f} s（冷启动，一次性成本）")
            print(f"  图输入        : {', '.join(inames)}")
            print(f"  图输出        : {', '.join(onames)}")
            if "sentence_embedding" in onames:
                print(f"  → 图里已含池化，直接取 sentence_embedding（与 ST 的口径一致）")
            else:
                print(f"  → 图只给 last_hidden_state，池化在脚本里按 attention_mask 做均值，")
                print(f"    再 L2 归一。第 5 步会拿它和 ST 的向量比，比得上才说明这条路径没写错。")
            extra = onnx_extra_inputs(sess)
            if extra:
                print(f"  多模态空入口  : {extra}")
                print(f"  → 这三个是图的**必需输入**（不是可选），只喂文本会直接报缺输入。")
                print(f"    我们只跑文本，所以喂 0 长度的空张量。这是本模型多模态设计的直接后果：")
                print(f"    文本塔与音视频塔共用一张图。")
            print(RSS_NOTE)
        except Exception as exc:  # noqa: BLE001
            fail("ONNX 加载", exc)
        else:
            for b in batches:
                for t in threads:
                    try:
                        r = run_onnx(texts, onnx_dir, onnx_file, t, b, args.warmup,
                                     cache=True, prefix=prefix, dim=dim,
                                     max_seq=args.max_seq)
                        runs.append(r)
                        print(r.line())
                    except Exception as exc:  # noqa: BLE001
                        fail(f"ONNX 批={b} 线程={t}", exc)

    # ---- 第 5 步：两个后端的向量对不对得上 -------------------------------
    st_runs = [r for r in runs if r.backend == "ST" and r.embeddings is not None]
    on_runs = [r for r in runs if r.backend == "ONNX" and r.embeddings is not None]
    if st_runs and on_runs:
        section("第 5 步  跨后端一致性核对（速度对比成立的前提）")
        import numpy as np
        a = np.asarray(st_runs[0].embeddings, dtype=np.float32)
        b_ = np.asarray(on_runs[0].embeddings, dtype=np.float32)
        if a.shape != b_.shape:
            print(f"  [FAIL] 形状不同：ST {a.shape} vs ONNX {b_.shape}——两者算的不是一回事，"
                  f"上面的速度对比不成立")
            FAILURES.append(f"跨后端形状不一致 {a.shape} vs {b_.shape}")
        else:
            cos = (a * b_).sum(axis=1)
            na, nb = np.linalg.norm(a, axis=1), np.linalg.norm(b_, axis=1)
            print(f"  比较对象      : ST({st_runs[0].dtype},线程={st_runs[0].threads},"
                  f"批={st_runs[0].batch}) vs ONNX(int8,线程={on_runs[0].threads},"
                  f"批={on_runs[0].batch})")
            print(f"  向量范数      : ST 最低 {na.min():.6f} 最高 {na.max():.6f} | "
                  f"ONNX 最低 {nb.min():.6f} 最高 {nb.max():.6f}")
            print(f"  逐条 cosine   : 最低 {cos.min():.4f}，平均 {cos.mean():.4f}，最高 {cos.max():.4f}")
            if cos.max() > 1.0001:
                print(f"  ⚠ 余弦超过 1：归一化后的向量之间不可能真的大于 1，出现这个只说明")
                print(f"    至少一侧的范数不是 1（看上面那行）。若 ST 侧范数 ≈ 1.0017，")
                print(f"    那就是 bf16 的归一化只有 3 位有效数字所致——把 ST 换成 float32 即消失。")
            ok = cos.min() > 0.95
            print(f"  判定          : {'一致 ✔' if ok else '不一致 ✘'}"
                  + ("（int8 量化会掉一点点，掉到 0.95 以下就不是量化误差了，是路径写错了）"
                     if ok else "（低于 0.95：前缀、池化或归一化至少有一处与 ST 不同）"))
            if not ok:
                FAILURES.append(f"跨后端 cosine 最低 {cos.min():.4f}")

    # ---- 第 6 步：并行 ---------------------------------------------------
    par = []
    if args.parallel and runs:
        section("第 6 步  多进程并行（A2：整机并行效率 ≥ 单进程的 8 倍）")

        # 测"还能放几个 worker"之前，必须先把父进程自己占的内存放掉。
        # 父进程在前面几步加载过 ST fp32（约 3GB）与 ONNX 会话；留着不放，它们就
        # 从"可用内存"里被扣掉，测出来的结论会变成"这台机器放不下 2 个 worker"，
        # 而其实是**测量者自己**占的。这与"量网络要用将来真正发请求的那个客户端量"
        # 是同一类错误：先把自己摆到正确的位置，再量。
        model = None      # main() 里的这两个局部名同样按着模型不放，一起断掉
        sess = None
        release_models()
        freed = machine_facts()
        print(f"  已释放父进程持有的模型；此刻可用内存 {human_bytes(freed['avail_ram'])}"
              f"（整轮开始时 {human_bytes(facts['avail_ram'])}）")
        best = max(runs, key=lambda r: r.useful_per_s)
        print(f"  单进程最快档位: {best.backend} {best.dtype} 批={best.batch} 线程={best.threads}"
              f" → {best.useful_per_s:.1f} tok/s")
        print(f"  并行段用它作为**后端与批量**，线程数按 worker 数反推（见 parallel_sweep 注释）")
        base = {"texts": texts,
                "backend": best.backend, "batch": best.batch, "dtype": best.dtype,
                "warmup": args.warmup, "model_id": args.model_id,
                "st_dtype": best.dtype if best.backend == "ST" else args.st_dtype,
                "repo_dir": onnx_dir, "model_file": onnx_file,
                "prompt_name": args.prompt_name, "prefix": prefix, "dim": dim,
                "max_seq": args.max_seq}
        # 单进程扫描已经量过的档位直接复用，别在并行段再花一遍时间
        single = {(r.backend, r.dtype, r.threads, r.batch): r.useful_per_s for r in runs}
        par = parallel_sweep(base, [int(x) for x in args.parallel_workers.split(",")],
                             facts, args.force_parallel,
                             args.threads_per_worker, cores, single)
        good = [p for p in par if "useful" in p]
        skipped = [p for p in par if p.get("skipped")]
        if good:
            print()
            print(f"  并行效率（分母 = **同线程数**的单进程，不是上面那个最快档位）：")
            for p in good:
                print(f"    workers={p['workers']:<3d} × {p['tpw']} 线程 → "
                      f"{p['useful'] / p['slowest_s']:8.1f} tok/s"
                      f"  加速 {p['speedup']:5.2f}×"
                      f"  {'✔ ≥8×' if p['speedup'] >= 8 else '✘ <8×'}")
            print(f"  注：8 物理核上要跑 8 个 worker，每 worker 只能分到 1 条线程；")
            print(f"      再往上加 worker 只会抢同一批核，加速比必然往下走。")
        if skipped:
            print(f"  被跳过的档位：{', '.join('workers=' + str(p['workers']) for p in skipped)}"
                  f"（内存不够，理由见上）")

    # ---- 结论 ------------------------------------------------------------
    section("结论")
    if runs:
        best = max(runs, key=lambda r: r.useful_per_s)
        print(f"  最快档位      : {best.backend} 线程={best.threads} 批={best.batch}"
              f" → 有效 {best.useful_per_s:.1f} tok/s，计算 {best.computed_per_s:.1f} tok/s，"
              f"padding×{best.padding_amp:.2f}")
        # 冷启动必须读 COLD_LOAD 而不是对 runs 取 max：各档位复用同一个模型对象，
        # 后几次的 load_s 全是 0，取 max 会把这个数显示成 0（第一次就是错的）。
        for k, v in COLD_LOAD.items():
            print(f"  冷启动 {k:16s}: {v:.2f} s（每次新起进程都要付，CLI 一次性命令要算进去）")
        print()
        print(f"  换算（用最快档位的**有效**吞吐，语料 15 亿 token，见计划 §3.2）：")
        per_h = best.useful_per_s * 3600
        print(f"    1.5e9 ÷ {per_h:,.0f} tok/h = {1.5e9 / per_h:,.0f} 小时 ≈ "
              f"{1.5e9 / per_h / 24:,.0f} 天（单进程）")
    if par:
        good = [p for p in par if "useful" in p]
        skipped = [p for p in par if p.get("skipped")]
        if good:
            top = max(good, key=lambda p: p["useful"] / p["slowest_s"])
            per_h = top["useful"] / top["slowest_s"] * 3600
            print(f"    并行 {top['workers']} worker 最优：{per_h:,.0f} tok/h → "
                  f"{1.5e9 / per_h:,.0f} 小时 ≈ {1.5e9 / per_h / 24:,.0f} 天")
        if skipped:
            print(f"  并行被跳过的档位: {', '.join(str(p['workers']) for p in skipped)}"
                  f"（内存不够，见第 6 步）")

    print()
    print("本脚本只回答'本机嵌入有多快'。它**不**回答：")
    print("  检索端到端 p95 延迟（A3，要建完索引才能测）")
    print("  MFT 枚举可行性与权限（F0-04，需管理员权限）")
    print("  首次全盘索引要多少小时（还要乘上语料真实 token 总量与去重率，本脚本只给吞吐）")
    print()
    print("为什么依赖 torchvision（不在计划 §7.4 的 18 包里）：")
    print("  transformers 5.19 的 EmbeddingGemma2Processor 导入链落到了")
    print("  gemma4.image_processing_gemma4 → torchvision.transforms.v2。缺它连文本模型")
    print("  都加载不了，且 AutoProcessor 是无条件加载的，没有'只要文本'的开关。")
    print(f"  torchvision {__import__('torchvision').__version__}")

    if FAILURES:
        section("失败项")
        for f in FAILURES:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
