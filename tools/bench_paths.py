r"""F2-02 微基准：`core/paths.py` 的路径可达性判定有多贵。

对应功能点：**F2-02**（保留设备名的路径可达性修复）。产出指标 **E29**
（`docs/PROGRESS.md` §3）。

---- 为什么需要它 --------------------------------------------------------------

`core/paths.extended_path()` 接在**一次全盘索引按条目数各调一次**的热路径上
（`D:/work` 实测 1,028,697 次，见 `E30`）。它的文档里写着"谓词只做两个 C 层
调用、不做逐字符循环，换来 1.8 倍"——**那个 1.8 倍原来只有一次性内联脚本量过，
脚本没入库**。按 `E12` 立下的规矩（缺命令与计数的读数无法复跑），这里把它补上。

---- 量的是什么 ----------------------------------------------------------------

两件事，都是**纯内存、不碰盘**：

1. `extended_path()` 本身——真实调用点用的就是它；
2. `naive_extended_path()`——**同一套语义、换一种写法**：最后一段的分隔符用
   两圈 Python 层逐字符循环找，而不是 `str.rstrip` + `str.rfind` 两个 C 层调用。

第 2 项不是"另一个实现"，它就是模块文档里说的那个"朴素写法"的**可复跑版本**。
两份实现在整份语料上**必须给出逐条相同的答案**——否则这个速度对比就没有意义
（快的那份要是算错了，快多少都不算数）。这条护栏与 `bench_filters.py` 同形。

---- 顺序是一条未受控的变量（E26 的教训）-------------------------------------

`E26` 第一版微基准把 `attributes_of` 排在 `os.stat` 前面，先量的那个吃掉冷启动，
得到 62.5 µs 对 5.8 µs 的假读数。所以这里**A/B 交替、每轮内部换先后、7 轮取中位数**，
另有 1 轮预热不计。**不要把这个结构改成"先把 A 跑完再跑 B"。**

---- 语料为什么是合成的、以及怎么选 --------------------------------------------

因为要比的是**两份实现对同一批输入的处理速度**，而不是"真实盘上设备名文件有多少"。
合成语料按真实盘上的**形态分布**配：绝大多数路径的最后一段是普通名字（走"不转"
的快路），少数是设备名（走转换那一支）、已经是扩展写法的、以及说不清形态的
（相对路径 / 含 `..` / UNC）。**四类都要出现**，否则某一条分支根本没被走到，
量出来的平均单价就不算数。

---- 用法（仓库根执行）-------------------------------------------------------

    PYTHONIOENCODING=utf-8 uv run python tools/bench_paths.py
    PYTHONIOENCODING=utf-8 uv run python tools/bench_paths.py --paths 20000 --rounds 9

原始输出落盘用外部重定向（`> docs/reports/xxx.md`）；**脚本不参与排版**，
与 `bench_embed.py` / `bench_filters.py` / `bench_fingerprint.py` 同一个理由。
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from datetime import datetime

from localsearch.core.console import use_safe_output
from localsearch.core.paths import DEVICE_NAMES, _ALREADY, extended_path

#: 语料里各形态的占比。普通名字必须占绝大多数——真实盘上就是这样，
#: 而平均单价是**按真实占比**折算到全盘的，配错了这个数就没有意义。
ORDINARY_DIRS = [
    "work/test/localsearch/src/localsearch/ingest",
    "Users/luopu/Documents/报告",
    "Program Files/Common Files/microsoft shared",
    "Windows/System32/drivers/etc",
    "work/test/localsearch/tests/unit",
]
ORDINARY_NAMES = [
    "scanner_mft.py", "说明.md", "report-2026.docx", "预算表.xlsx", "笔记.txt",
    "PROGRESS.md", "uv.lock", "a-very-long-file-name-with-lots-of-words-in-it.log",
]
#: 会走"要转换"那一支的名字（全部来自 `DEVICE_NAMES`，另加尾部点/空格形态）。
DEVICE_FORMS = ["nul", "NUL", "nul.", "nul ", "con", "com1", "lpt9", "aux"]


def make_corpus(n: int, seed: int) -> list[str]:
    """按真实形态分布生成语料。**确定性**：同一个种子给出同一批路径。"""
    rng = random.Random(seed)
    corpus: list[str] = []
    for _ in range(n):
        roll = rng.random()
        drive = rng.choice(["C:", "D:"])
        if roll < 0.02:
            # 设备名：走转换那一支（含 `\\?\` 拼接）
            name = rng.choice(DEVICE_FORMS)
            corpus.append(f"{drive}/{rng.choice(ORDINARY_DIRS)}/{name}")
        elif roll < 0.03:
            # 已经是扩展写法：必须原样还回去（加第二层会指到别处）
            corpus.append(f"\\\\?\\{drive}\\{rng.choice(ORDINARY_DIRS)}\\nul")
        elif roll < 0.06:
            # 说不清的形态：相对路径 / 含 `..` / 盘符相对 / UNC
            corpus.append(rng.choice([
                "sub/nul",
                f"{drive}/x/../nul",
                "C:nul",
                "\\\\srv\\share\\nul",
            ]))
        else:
            corpus.append(f"{drive}/{rng.choice(ORDINARY_DIRS)}/{rng.choice(ORDINARY_NAMES)}")
    return corpus


def naive_extended_path(path: str) -> str:
    r"""`extended_path()` 的**朴素写法**：同一套语义，最后一段用两圈逐字符循环找。

    循环放在 Python 层，而不是 `str.rstrip` / `str.rfind` 那两个 C 层调用——
    这正是模块文档里 `_needs_extension` 的注释在做对比的那件事。
    """
    text = os.fspath(path)
    if not isinstance(text, str):
        return text
    if text.startswith(_ALREADY):
        return text

    end = len(text)
    while end > 0 and text[end - 1] in "\\/":          # 第一圈：剥尾部分隔符
        end -= 1
    cut = end
    while cut > 0 and text[cut - 1] not in "\\/":      # 第二圈：往前找最后一个分隔符
        cut -= 1
    part = text[cut:end]
    if part.rstrip(" .").partition(".")[0].upper() not in DEVICE_NAMES:
        return text

    native = text.replace("/", "\\").rstrip("\\")
    if len(native) < 3:
        return text
    if native.startswith("\\\\"):
        rest = native[2:]
    elif native[1] == ":":
        if native[2] != "\\":
            return text
        rest = native[3:]
    else:
        return text
    if any(p in (".", "..") for p in rest.split("\\")):
        return text
    if native.startswith("\\\\"):
        return "\\\\?\\UNC\\" + native[2:]
    return "\\\\?\\" + native


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    ap = argparse.ArgumentParser(description="F2-02 路径可达性判定微基准（纯内存，不碰盘）")
    ap.add_argument("--paths", type=int, default=2000, help="合成语料的路径条数")
    ap.add_argument("--rounds", type=int, default=7, help="取中位数的轮数（另有 1 轮预热不计）")
    ap.add_argument("--seed", type=int, default=20261015, help="语料种子（换了就不是同一批输入）")
    ap.add_argument("--scan-entries", type=int, default=1_028_697,
                    help="折算用的条目数，默认取 E30 那次复跑的 MFT 侧条目数")
    ap.add_argument("--mft-seconds", type=float, default=163.37,
                    help="同上，那次 MFT 整轮的秒数（E30）")
    ap.add_argument("--scandir-seconds", type=float, default=51.85,
                    help="同上，那次 scandir 整轮的秒数（E30）")
    args = ap.parse_args()

    corpus = make_corpus(args.paths, args.seed)

    # 护栏：两份实现必须逐条给出同一个答案。快的那份要是算错了，快多少都不算数。
    mismatch = sum(1 for p in corpus if extended_path(p) != naive_extended_path(p))

    # 预热：第一次调用会让 `_ALREADY` 之类的东西进缓存，不该算进去
    for p in corpus:
        extended_path(p)
        naive_extended_path(p)

    def sweep(fn) -> float:
        t0 = time.perf_counter()
        for p in corpus:
            fn(p)
        return (time.perf_counter() - t0) / len(corpus) * 1e6      # µs/次

    real: list[float] = []
    naive: list[float] = []
    for i in range(args.rounds):
        # A/B 交替 + 每轮换先后：避免"谁先跑谁吃亏"（E26 的教训）
        if i % 2 == 0:
            real.append(sweep(extended_path))
            naive.append(sweep(naive_extended_path))
        else:
            naive.append(sweep(naive_extended_path))
            real.append(sweep(extended_path))

    real_med = statistics.median(real)
    naive_med = statistics.median(naive)
    device_count = sum(1 for p in corpus if p.rstrip("\\/").rsplit("/", 1)[-1]
                       .rstrip(" .").partition(".")[0].upper() in DEVICE_NAMES)

    print(f"语料                {len(corpus)} 条路径（种子 {args.seed}；其中会转换的 {device_count} 条）")
    print(f"两份实现不一致      {mismatch} / {len(corpus)}")
    print(f"轮数                {args.rounds} 轮取中位数（另有 1 轮预热不计）")
    print(f"extended_path       {real_med:.2f} µs/次（各轮 "
          + " ".join(f"{v:.2f}" for v in real) + "）")
    print(f"朴素写法            {naive_med:.2f} µs/次（各轮 "
          + " ".join(f"{v:.2f}" for v in naive) + "）")
    print(f"比值                {naive_med / real_med:.2f}x")
    print(f"折算：一次全盘 {args.scan_entries:,} 条目")
    print(f"  extended_path     {args.scan_entries * real_med / 1e6:,.2f} s"
          f"（占 MFT 整轮 {args.mft_seconds:,.2f} s 的 "
          f"{args.scan_entries * real_med / 1e6 / args.mft_seconds * 100:.1f}%，"
          f"占 scandir 整轮 {args.scandir_seconds:,.2f} s 的 "
          f"{args.scan_entries * real_med / 1e6 / args.scandir_seconds * 100:.1f}%）")
    print(f"  朴素写法          {args.scan_entries * naive_med / 1e6:,.2f} s"
          f"（比它多花 {args.scan_entries * (naive_med - real_med) / 1e6:,.2f} s）")
    print(f"机器时间            {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
