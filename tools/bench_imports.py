r"""微基准：按需导入的那几个库，**冷导入**各要花多少。

对应功能点：**F3-02**（office 提取器，产出 **E32**）与 **F3-03**（web_mail 提取器，
产出 **E33**）。两者同一张表，理由也一样：两个模块都把第三方库写在各自的
`_parse_*` **函数里**导入，不是模块顶层，而"省下了多少"必须有读数才算数。

---- 为什么需要它 --------------------------------------------------------------

`extract/office.py` 把四个库都写在各自的 `_parse_*` **函数里**导入，不是模块顶层，
理由是"别让每一次 `lsearch` 启动都付这笔钱"。**那笔钱原来只有一次性内联命令量过、
没入库**——按 `E12` 立下的规矩（缺命令与计数的读数无法复跑），这里把它补上。
`F3-03` 的 `web_mail.py` 照搬了同一条做法（`E33`），于是候选里多一组。

⚠ 这个脚本**不验语义**，只量时间；它没有 `bench_paths.py` 那样的"两份实现逐条比对"
护栏，因为没有第二份实现可比。**读它的时候只信"数量级与相对大小"**，别把某一轮的
毫秒数当成有三位有效数字的东西（`M4` / `E12` / `E17` 都教训过同一件事）。

---- 量的是什么 ----------------------------------------------------------------

**冷导入**：每一个候选项都起一个**新进程**，在进程内计时"从 import 到 import 完成"。
计时区间**不含解释器自身的启动**（计时器在进程起来之后才起表）——量的是"这一次启动
为了这些 import 要多付多少"，而不是"起一个 python 要多久"。

候选：
- `import localsearch.extract.plaintext`——**基线**。`build_registry()` 会 import
  `plaintext`，它是"今天每一次启动都在付的那一笔"。
- office 侧：四个库各一条、`lxml.etree` 一条（`_ooxml_errors()` 在函数里 import 它）。
- web_mail 侧：`bs4` / `extract_msg` 各一条，`email.policy` 一条（标准库那一个，
  量它是为了说明**它不用省**——省标准库只是把复杂度换回来）。
- **两组各一条"一起"**——顶层 import 的代价就是这两条：写了顶层 import，那一组
  几个库会**全部**被拉起来，而不是"用到哪个拉哪个"。

---- 为什么是"三轮取最小"而不是中位数 ------------------------------------------

导入耗时**只会被噪声抬高、不会被压低**（它不像吞吐那样会被抢占拉低），所以最小值
最接近"这台机器空着时的真实代价"。这是 `bench_embed` 那一轮的做法；中位数会把这台
机器当时的负载一起量进去。**但各轮读数全部打印**——`E17` 的教训是只报一个点值会让
人以为它很稳。

---- 用法（仓库根执行）-------------------------------------------------------

    PYTHONIOENCODING=utf-8 uv run python tools/bench_imports.py
    PYTHONIOENCODING=utf-8 uv run python tools/bench_imports.py --rounds 5

原始输出落盘用外部重定向（`> docs/reports/xxx.md`）；**脚本不参与排版**，与
`bench_embed.py` / `bench_paths.py` / `bench_encoding.py` 同一个理由。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

from localsearch.core.console import use_safe_output

#: 候选项：(标签, 在子进程里 import 的东西)。顺序无关紧要——每一项都换新进程。
CANDIDATES: list[tuple[str, str]] = [
    ("localsearch.extract.plaintext", "localsearch.extract.plaintext"),
    ("pymupdf", "pymupdf"),
    ("docx", "docx"),
    ("openpyxl", "openpyxl"),
    ("pptx", "pptx"),
    ("lxml.etree", "lxml.etree"),
    ("office 四个一起", "pymupdf, docx, openpyxl, pptx"),
    ("bs4", "bs4"),
    ("extract_msg", "extract_msg"),
    ("email.policy", "email.message, email.policy"),
    ("web_mail 三个一起", "bs4, extract_msg, email.message, email.policy"),
]

#: 汇总比值时用的两组：标签 → 分母（基线）与分子（顶层 import 的代价）。
COMBOS: list[tuple[str, str]] = [
    ("office 四个一起", "localsearch.extract.plaintext"),
    ("web_mail 三个一起", "localsearch.extract.plaintext"),
]

#: 子进程里跑的那段：**先起表、再 import**，最后只 echo 一个毫秒数。
#: `-X importtime` 那种做法会把标准库的一堆行也打出来，解析成本比它量的还高。
_PROBE = "import time; t0 = time.perf_counter(); import {mods}; print((time.perf_counter() - t0) * 1000)"


def machine_facts() -> dict:
    """当时的机器闲忙。**这不是装饰**：`M4` / `E17` 的教训是数字必须连负载一起记。"""

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"avail_mib": m.ullAvailPhys / 1024 / 1024, "load_pct": m.dwMemoryLoad}


def one_shot(mods: str) -> float:
    """起一个新进程，只做那一次 import，返回毫秒（进程启动本身不在计时区间里）。"""
    code = _PROBE.format(mods=mods)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if proc.returncode != 0:
        # 不要静默地当成 0 ms——"import 失败"与"import 很快"必须分得开
        raise SystemExit(f"子进程失败（import {mods or '（空）'}）：\n{proc.stderr}")
    try:
        return float(proc.stdout.strip().splitlines()[-1])
    except ValueError as exc:
        raise SystemExit(f"子进程没给出毫秒数（import {mods or '（空）'}）：{proc.stdout!r}") from exc


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    ap = argparse.ArgumentParser(description="按需导入的库的冷导入微基准（E32 / E33）")
    ap.add_argument("--rounds", type=int, default=3, help="每个候选项跑几轮，取最小值（默认 3）")
    args = ap.parse_args()

    facts = machine_facts()
    print(f"解释器              {sys.executable}")
    print(f"轮数                {args.rounds} 轮取最小值（各轮读数全部列出）")
    print(f"可用物理内存        {facts['avail_mib']:,.0f} MiB（内存占用率 {facts['load_pct']}%）")
    print(f"起始机器时间        {datetime.now():%Y-%m-%d %H:%M:%S}")
    print()

    measured: dict[str, list[float]] = {}
    for label, mods in CANDIDATES:
        measured[label] = [one_shot(mods) for _ in range(args.rounds)]

    width = max(len(label) for label, _ in CANDIDATES)
    print(f"{'候选项'.ljust(width)}  {'最小':>9}  {'中位':>9}   各轮")
    for label, _ in CANDIDATES:
        readings = measured[label]
        print(f"{label.ljust(width)}  {min(readings):9.1f}  {statistics.median(readings):9.1f}   "
              + " ".join(f"{v:.1f}" for v in readings))

    print()
    for combo, denominator in COMBOS:
        top = min(measured[combo])
        base = min(measured[denominator])
        print(f"{combo} / 基线".ljust(20) + f"{top / base:.1f}x（{top:.1f} ms 对 {base:.1f} ms）")
    print(f"机器时间            {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
