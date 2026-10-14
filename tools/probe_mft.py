"""P0 MFT 枚举可行性与权限验证：把"能不能用 MFT 走全盘枚举、能快多少"变成已知事实。

对应功能点：**F0-04**。关联验收项 **A1**（全量索引无未捕获异常——枚举前端不崩是它的前提）。
产出指标 **M8**（计划 §3.1：MFT + USN Journal 枚举可行性，目标 **~200 万文件/分钟**）
与决策 **D11**（扫描后端：`os.scandir` 默认，MFT + USN 可选加速）的**可行性部分**。

---- 为什么这件事必须在提权终端里做 --------------------------------------------

`FSCTL_ENUM_USN_DATA` 要的是**卷设备句柄**（`\\\\.\\C:`）。它不是一个普通文件：
`CreateFile` 打开它需要管理员权限，不提权时返回 `ERROR_ACCESS_DENIED(5)`。
所以本脚本一进来就先报**自己是不是提权的**，并把两种情况的行为都量出来——
"没提权会怎样"本身就是 **R15** 要的证据（必须能无损回退 `scandir`，且不崩）。

---- 本脚本只读，不碰系统状态 --------------------------------------------------

它**不创建、不修改、不删除 USN Journal**（`fsutil usn createjournal` 会改卷上的
元数据，那属于系统修改，不该由一个探针代劳）。查不到 journal 时它只报告这件事
和那条命令，**不替你执行**。

---- 为什么用纯 ctypes 而不是 pywin32 / mftparser --------------------------------

计划 §7.4 列过 `mftparser`，但 P0 实测发现**本机 `.venv` 里没有它，`pyproject.toml`
的依赖表里也没有**（`pywin32` 同理）。所以这一轮**不引入新依赖**，直接 ctypes 调
Win32——这也顺便回答了更根本的问题：**这件事需不需要一个第三方库**。答案是
不需要：`DeviceIoControl` 两个控制码就够了。要不要在 P2 换用 `mftparser` 是
实现选择，不是可行性问题。

---- 用法（仓库根执行）-----------------------------------------------------

    # 常规：所有卷都试一遍。MFT 段需要提权终端，否则会如实报 ERROR_ACCESS_DENIED
    PYTHONUTF8=1 ./.venv/Scripts/python.exe -u tools/probe_mft.py --log <日志路径>

    # 加 scandir 基线做同卷对照（scandir 不需要提权，慢，默认不开）
    ... tools/probe_mft.py --volumes C,E --scandir --log <日志路径>

    # 不提权也能跑的那部分：只量 scandir 基线
    ... tools/probe_mft.py --backend scandir --volumes C

原始输出落盘用外部重定向（`> docs/reports/xxx.md`）或 `--log`，两者等价；
与 `probe_deps.py` / `bench_embed.py` 同一个理由：**脚本不参与排版**，也就不可能
把失败修饰成好看的样子。

---- 口径（先看这里，否则数字没法比）---------------------------------------

  * **"记录数"不是"文件数"。** MFT 里每个文件*和*每个目录各占一条记录，还包括
    `$MFT`/`$Bitmap` 等 NTFS 元数据文件。所以本脚本把 总记录 / 目录 / 文件
    **分开报**——拿"记录/分钟"去对"文件/分钟"的目标是虚高。
  * **两轮计时不是重复劳动。** 第 1 轮的 MFT 可能还在磁盘上（真冷），第 2 轮已经
    在 Windows 的文件缓存里。**两个数都要，别只留快的那个**——首次装机的体验是
    第 1 轮，不是第 2 轮。报告里逐轮列出。
  * **`scandir` 的数极度依赖缓存与当时的机器负载**，而且权限被拒的目录会**整棵
    跳过**，是系统性地**少算**。所以它只能与 MFT 看**量级**差别；逐条一致性是
    `F2-02` 的活，本脚本不声称一致。
  * **`scandir` 侧必须跳过 reparse point，否则数出来的东西根本不对**：本机的
    junction（`%LOCALAPPDATA%\\Application Data` 等 11 个）指回自己所在的目录，
    下潜会把同一棵树数很多遍——实测时间 **2.23×**、文件数虚高 1.2 万。
    MFT 侧不受影响（它枚举的是记录，不递归目录）。
  * **MFT 记录数通常多于 `scandir` 数**：MFT 含目录、元数据文件，以及每卷各自的
    内部记录。数量不等是**预期**，不是 bug。

退出码：
  0  计划内的检查都完成（**含"未提权、MFT 段失败"这种已如实报告的情形**）
  2  这次要跑 MFT 但没提权 → **本次不能作为 F0-04 的证据**，请到提权终端重跑
  1  出现未预期异常
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# ===========================================================================
# 0. 常量与 Win32 绑定
# ===========================================================================

# ---- 控制码。CTL_CODE(DeviceType, Function, Method, Access)
# = (DeviceType << 16) | (Access << 14) | (Function << 2) | Method
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_ENUM_USN_DATA = 0x000900B3

# ---- CreateFile
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

# ---- 错误码（只列我们真要分支的）
ERROR_ACCESS_DENIED = 5
ERROR_HANDLE_EOF = 38
ERROR_MORE_DATA = 234
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_SUPPORTED = 50

# ---- 卷类型。DRIVE_FIXED 才是本地 NTFS 盘的常态
DRIVE_TYPES = {0: "未知", 1: "无根目录", 2: "可移动", 3: "本地固定",
               4: "网络", 5: "光驱", 6: "RAM 盘"}

# ---- 文件属性位
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

# ---- 提权查询
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
TOKEN_INTEGRITY_LEVEL = 25

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                 ctypes.c_void_p]
_k32.DeviceIoControl.restype = wt.BOOL
_k32.GetCurrentProcess.restype = wt.HANDLE
_k32.GetLogicalDrives.restype = wt.DWORD
_k32.GetDriveTypeW.argtypes = [wt.LPCWSTR]
_k32.GetDriveTypeW.restype = wt.UINT
_k32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                                       ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD]
_k32.GetVolumeInformationW.restype = wt.BOOL
_k32.GetDiskFreeSpaceExW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                     ctypes.POINTER(ctypes.c_ulonglong),
                                     ctypes.POINTER(ctypes.c_ulonglong)]
_k32.GetDiskFreeSpaceExW.restype = wt.BOOL
_advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
_advapi32.OpenProcessToken.restype = wt.BOOL
_advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wt.DWORD, ctypes.POINTER(wt.DWORD)]
_advapi32.GetTokenInformation.restype = wt.BOOL


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


# ===========================================================================
# 1. 输出管道：stdout 与控制台代码页
# ===========================================================================

class Tee:
    """把 stdout 同时写到控制台与日志文件。

    本机控制台代码页是 936(GBK)，中文在**提权终端**里也可能编不出来；直接 print
    会抛 UnicodeEncodeError 把整轮探针打断。所以先把 stdout reconfigure 成
    utf-8 + errors=replace——日志文件要的是**原始输出**，不能为了迁就控制台
    把内容改掉。

    write() 里**每次都 flush**：不加这一条，日志文件在进程退出前一直是 0 字节
    （实测过）。`-u` 只对被我替换掉的那个原始 sys.stdout 生效，管不到包装层。
    这个脚本整轮的用途就是把原始输出留证，一旦中途崩掉却什么都没留下，
    留证这件事本身就没做到。
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001 —— 控制台写不进去不该影响日志
                pass
        self.flush()
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass


def setup_output(log_path: str | None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:  # noqa: BLE001
        pass
    if log_path:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n"：落盘就是 LF，省得报告阶段再做一次行尾归一。
        # buffering=1：文本模式下行缓冲，与 Tee 里的 flush 一起做双保险。
        fh = open(p, "w", encoding="utf-8", newline="\n", buffering=1)
        sys.stdout = Tee(sys.stdout, fh)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def human_bytes(n: int | float) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(x) < 1024 or unit == "TiB":
            return f"{int(x)} B" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} TiB"


# ===========================================================================
# 2. 机器事实 / 版本 / 内存
# ===========================================================================

def machine_facts() -> dict:
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    _k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"total_ram": m.ullTotalPhys, "avail_ram": m.ullAvailPhys,
            "load_pct": m.dwMemoryLoad, "logical": os.cpu_count() or 1}


def peak_rss() -> int:
    # 注意：GetProcessMemoryInfo 是 **psapi.dll** 导出的，不在 kernel32 里
    # （kernel32 只有 K32GetProcessMemoryInfo 这个别名）。写错模块名要到调用时
    # 才炸，而那时输出已经打了一大半——与 bench_embed.py 保持一致，用 psapi。
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
    fn.restype = wt.BOOL
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if not fn(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return -1
    return int(c.PeakWorkingSetSize)


def git_version() -> str:
    def run(args: list[str]) -> str:
        return subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()

    try:
        head = run(["git", "rev-parse", "--short", "HEAD"])
        dirty = run(["git", "status", "--porcelain"])
        # 工作区脏必须标出来：在未提交的代码上跑出的绿，别人 clone 不到同一份。
        return f"{head} + 工作区脏（{len(dirty.splitlines())} 条改动）" if dirty else f"{head} 干净"
    except Exception as exc:  # noqa: BLE001 —— 取不到版本不该让整轮失败，但要明说
        return f"（取不到：{type(exc).__name__}）"


def print_header(facts: dict) -> None:
    section("P0 MFT 枚举探针 — 原始输出")
    print(f"生成时间      : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"解释器        : {sys.version.split()[0]}  ({sys.executable})")
    wv = sys.getwindowsversion()
    print(f"平台          : Windows {wv.major}.{wv.minor}.{wv.build}")
    print(f"代码版本      : {git_version()}")
    print(f"内存          : 总 {human_bytes(facts['total_ram'])}，"
          f"空闲 {human_bytes(facts['avail_ram'])}，占用 {facts['load_pct']}%")
    print("卷枚举是流式计数、不累积记录，所以本轮的峰值 RSS 主要反映解释器本身；")
    print("它**不能**与嵌入实测的 2 GiB 同台比较。")


# ===========================================================================
# 3. 提权状态
# ===========================================================================

def elevation_state() -> dict:
    """返回是否提权 + 完整性级别。用 token 查而不是只看 IsUserAnAdmin()：

    后者在被"以不同用户身份运行"启动时会给出误导答案，而我们要的正是
    "这个进程现在有没有打开卷设备句柄的权限"。
    """
    out: dict = {"elevated": None, "integrity": "?", "error": None, "is_admin": None}
    h = wt.HANDLE()
    if not _advapi32.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h)):
        out["error"] = f"OpenProcessToken 失败，WinError {ctypes.get_last_error()}"
        return out
    try:
        elev = ctypes.c_ulong(0)
        ret = ctypes.c_ulong(0)
        if _advapi32.GetTokenInformation(h, TOKEN_ELEVATION, ctypes.byref(elev),
                                         ctypes.sizeof(elev), ctypes.byref(ret)):
            out["elevated"] = bool(elev.value)

        # TOKEN_INTEGRITY_LEVEL 是变长结构：先问长度，再读 SID 的最后一级
        need = ctypes.c_ulong(0)
        _advapi32.GetTokenInformation(h, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(need))
        if need.value:
            buf = ctypes.create_string_buffer(need.value)
            if _advapi32.GetTokenInformation(h, TOKEN_INTEGRITY_LEVEL, buf, need.value,
                                             ctypes.byref(ret)):
                # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attr; } }
                # 64 位下 PSID 占前 8 字节，SID 本体在它指向的地方。
                sid = ctypes.c_void_p.from_buffer(buf).value
                # SID 布局：Revision(1) SubAuthorityCount(1) IdentifierAuthority(6)
                # SubAuthority[SubAuthorityCount]。完整性级别是**最后一级**。
                sub_auth = ctypes.cast(sid + 1, ctypes.POINTER(ctypes.c_ubyte))[0]
                last = ctypes.cast(sid + 8 + 4 * (sub_auth - 1),
                                   ctypes.POINTER(ctypes.c_uint))[0]
                out["integrity"] = {0x0000: "Untrusted", 0x1000: "Low",
                                    0x2000: "Medium", 0x2100: "Medium Plus",
                                    0x3000: "High", 0x4000: "System"}.get(last, hex(last))
    finally:
        _k32.CloseHandle(h)

    try:
        out["is_admin"] = bool(_shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass
    return out


def print_elevation(e: dict) -> None:
    section("1. 权限状态（决定 MFT 段能不能跑）")
    if e["elevated"] is None:
        print(f"提权状态      : 取不到（{e['error']}）")
    else:
        print(f"提权状态      : {'是（已完成提权）' if e['elevated'] else '否（普通权限）'}")
    print(f"完整性级别    : {e['integrity']}")
    print(f"IsUserAnAdmin : {e['is_admin']}   ← 交叉验证，与上一行不一致时以上一行为准")
    if not e["elevated"]:
        print()
        print("  ⚠ 本进程**没有**提权。MFT 段的 CreateFile(\\\\.\\X:) 预期返回")
        print("    ERROR_ACCESS_DENIED(5)——这一次的运行**不能**作为 F0-04 的证据，")
        print("    但它的失败方式正是 R15 要的记录：必须能回退 scandir，且不能崩。")


# ===========================================================================
# 4. 卷清单
# ===========================================================================

def list_volumes(wanted: set[str] | None) -> list[dict]:
    mask = _k32.GetLogicalDrives()
    vols = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        letter = chr(ord("A") + i)
        if wanted and letter.upper() not in wanted:
            continue
        root = f"{letter}:\\"
        d = {"letter": letter, "root": root, "device": f"\\\\.\\{letter}:"}
        d["drive_type"] = _k32.GetDriveTypeW(root)

        fs = ctypes.create_unicode_buffer(261)
        vol = ctypes.create_unicode_buffer(261)
        serial, maxcomp, flags = wt.DWORD(), wt.DWORD(), wt.DWORD()
        d["ok_volinfo"] = bool(_k32.GetVolumeInformationW(
            root, vol, 261, ctypes.byref(serial), ctypes.byref(maxcomp),
            ctypes.byref(flags), fs, 261))
        d["fs"] = fs.value if d["ok_volinfo"] else None
        d["label"] = vol.value if d["ok_volinfo"] else None

        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        if _k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)):
            d["total"], d["free"] = total.value, free.value
        else:
            d["total"] = d["free"] = None
        vols.append(d)
    return vols


def print_volumes(vols: list[dict]) -> None:
    section("2. 卷清单")
    print(f"{'卷':<4}{'类型':<12}{'文件系统':<10}{'容量':>12}{'空闲':>12}  标签")
    for v in vols:
        t = DRIVE_TYPES.get(v["drive_type"], f"未知({v['drive_type']})")
        cap = human_bytes(v["total"]) if v["total"] else "—"
        free = human_bytes(v["free"]) if v["free"] else "—"
        print(f"{v['letter'] + ':':<4}{t:<12}{v['fs'] or '（取不到）':<10}"
              f"{cap:>12}{free:>12}  {v['label'] or ''}")
    print()
    print("只有 **本地固定盘(3) + NTFS** 才有 MFT 枚举这条路。其余情形按 R15 回退 scandir。")


# ===========================================================================
# 5. USN Journal
# ===========================================================================

class USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [("UsnJournalID", ctypes.c_ulonglong),
                ("FirstUsn", ctypes.c_longlong), ("NextUsn", ctypes.c_longlong),
                ("LowestValidUsn", ctypes.c_longlong), ("MaxUsn", ctypes.c_longlong),
                ("MaximumSize", ctypes.c_ulonglong),
                ("AllocationDelta", ctypes.c_ulonglong)]


def open_volume(device: str) -> tuple[int | None, int]:
    """打开卷设备句柄。返回 (handle, winerror)；成功时 winerror 为 0。

    只读 + 共享读写——**不表达任何写意图**，本探针绝不动卷上的数据。
    """
    h = _k32.CreateFileW(device, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                         None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE_VALUE:
        return None, ctypes.get_last_error()
    return h, 0


def query_journal(handle: int) -> dict:
    out = USN_JOURNAL_DATA_V0()
    ret = wt.DWORD(0)
    ok = _k32.DeviceIoControl(handle, FSCTL_QUERY_USN_JOURNAL, None, 0,
                              ctypes.byref(out), ctypes.sizeof(out),
                              ctypes.byref(ret), None)
    if not ok:
        return {"ok": False, "winerror": ctypes.get_last_error()}
    return {"ok": True, "id": out.UsnJournalID, "first": out.FirstUsn, "next": out.NextUsn,
            "lowest_valid": out.LowestValidUsn, "max": out.MaxUsn,
            "max_size": out.MaximumSize, "delta": out.AllocationDelta}


def describe_winerror(code: int) -> str:
    return {ERROR_ACCESS_DENIED: "ERROR_ACCESS_DENIED —— 没提权，或该卷不允许",
            ERROR_INVALID_FUNCTION: "ERROR_INVALID_FUNCTION —— 该卷不支持这个控制码",
            ERROR_NOT_SUPPORTED: "ERROR_NOT_SUPPORTED —— 该文件系统不支持",
            ERROR_JOURNAL_NOT_ACTIVE: "ERROR_JOURNAL_NOT_ACTIVE —— 卷上没有活动的 USN Journal",
            ERROR_HANDLE_EOF: "ERROR_HANDLE_EOF —— 枚举正常结束"}.get(code, "")


# ===========================================================================
# 6. MFT 枚举
# ===========================================================================

# MFT_ENUM_DATA_V0: StartFileReferenceNumber(8) LowUsn(8) HighUsn(8)
_MFT_ENUM_DATA_V0 = "<QQQ"

# USN_RECORD_V2 头部 60 字节；FileName 跟在 FileNameOffset 处（V2 通常 = 60）
_USN_RECORD_V2_HEADER = 60


def mft_enumerate(handle: int, high_usn: int, buffer_kb: int, sample_n: int) -> dict:
    """流式枚举 MFT 记录。**只计数，不累积**——这一条不能省：

    大卷上 MFT 可以有数百万条记录，全留进列表就是几百 MB 到数 GB 的常驻内存，
    与嵌入段踩过的内存坑是同一个形状。这里只保留前 sample_n 条，用途仅限
    "证明解析是对的"。
    """
    out_size = buffer_kb * 1024
    buf = ctypes.create_string_buffer(out_size)
    ret = wt.DWORD(0)

    start_frn = 0
    calls = total_bytes = 0
    records = dirs = files = reparse = 0
    other_versions = bad_records = 0
    meta: list[dict] = []
    versions: set[tuple[int, int]] = set()
    t0 = time.perf_counter()

    while True:
        in_bytes = struct.pack(_MFT_ENUM_DATA_V0, start_frn, 0, high_usn)
        in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes))
        ok = _k32.DeviceIoControl(handle, FSCTL_ENUM_USN_DATA,
                                  ctypes.cast(in_buf, ctypes.c_void_p), len(in_bytes),
                                  ctypes.cast(buf, ctypes.c_void_p), out_size,
                                  ctypes.byref(ret), None)
        calls += 1
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_HANDLE_EOF:
                break                       # 正常结束
            if err != ERROR_MORE_DATA:
                return {"ok": False, "winerror": err, "calls": calls,
                        "bytes": total_bytes, "seconds": time.perf_counter() - t0,
                        "records": records}
            # ERROR_MORE_DATA：缓冲区放不下，但 ret 里有已写好的部分，照常处理
        n = ret.value
        if n <= 8:                          # 只有 next_frn、没有记录 → 到底了
            break
        data = ctypes.string_at(buf, n)
        total_bytes += n
        start_frn = struct.unpack_from("<Q", data, 0)[0]

        off = 8
        while off + _USN_RECORD_V2_HEADER <= n:
            (rlen,) = struct.unpack_from("<I", data, off)
            if rlen < _USN_RECORD_V2_HEADER or off + rlen > n:
                bad_records += 1            # 截断/异常记录：计数，但不猜，也不死循环
                break
            major, minor = struct.unpack_from("<HH", data, off + 4)
            versions.add((major, minor))
            records += 1

            if major != 2:
                # V3/V4 的头部布局不同（FRN 是 128 位）。拿 V2 的偏移去解释 V3
                # 只会得到垃圾数字，所以**只计数、不解析**，单独报出来。
                # 用 MFT_ENUM_DATA_V0 作输入时返回的本该是 V2；走到这里说明有意外。
                other_versions += 1
                off += rlen
                continue

            frn, parent = struct.unpack_from("<QQ", data, off + 8)
            (usn,) = struct.unpack_from("<q", data, off + 24)
            (ts,) = struct.unpack_from("<q", data, off + 32)
            (attrs,) = struct.unpack_from("<I", data, off + 52)
            name_len, name_off = struct.unpack_from("<HH", data, off + 56)

            if attrs & FILE_ATTRIBUTE_DIRECTORY:
                dirs += 1
            else:
                files += 1
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                reparse += 1

            if len(meta) < sample_n and name_off + name_len <= rlen:
                meta.append({
                    "name": data[off + name_off:off + name_off + name_len].decode(
                        "utf-16-le", errors="replace"),
                    "frn": frn, "parent": parent, "usn": usn, "attrs": attrs, "ts": ts})
            off += rlen

    return {"ok": True, "winerror": 0, "calls": calls, "bytes": total_bytes,
            "seconds": time.perf_counter() - t0, "records": records,
            "dirs": dirs, "files": files, "reparse": reparse, "sample": meta,
            "bad_records": bad_records, "other_versions": other_versions,
            "versions": sorted(versions)}


def mft_rounds(handle: int, high_usn: int, buffer_kb: int, rounds: int,
               sample_n: int) -> list[dict]:
    """跑多轮并**逐轮保留**：第 1 轮可能真读盘，第 2 轮已在文件缓存里。"""
    out = []
    for i in range(rounds):
        r = mft_enumerate(handle, high_usn, buffer_kb, sample_n if i == 0 else 0)
        r["round"] = i + 1
        out.append(r)
        tag = "第 1 轮（冷，可能真读盘）" if i == 0 else f"第 {i + 1} 轮（MFT 已在缓存里）"
        if r["ok"]:
            rate = r["records"] / max(r["seconds"], 1e-9) * 60
            print(f"    {tag:<26} {r['seconds']:9.3f} s  {r['records']:>11,} 条"
                  f"  {rate:>15,.0f} 条/分钟")
        else:
            print(f"    {tag:<26} 失败：WinError {r['winerror']} "
                  f"{describe_winerror(r['winerror'])}")
    return out


# ===========================================================================
# 7. scandir 基线
# ===========================================================================

def scandir_count(root: str) -> dict:
    """栈式遍历，数文件/目录。`follow_symlinks=False` 走的是 scandir 已缓存的
    stat，不会为每个条目多打一次系统调用。

    **reparse point（junction / 符号链接 / 挂载点）只计数、不下潜**——这一条不是
    优化，是正确性。本机 `%LOCALAPPDATA%\\Application Data` 这类 junction 指回
    它自己所在的目录，下潜会把同一棵树数很多遍。实测在
    `C:\\Users\\<user>\\AppData\\Local` 上：不下潜 **61.99 s / 1,561,747 文件**，
    跳过后 **27.78 s / 1,549,646 文件 + 11 个 junction**——时间差 **2.23×**，
    文件数虚高 **12,101**。全盘跑就是"十几分钟也跑不完"。
    判断位与 MFT 侧用的是同一个 `FILE_ATTRIBUTE_REPARSE_POINT`，两边口径一致。
    """
    files = dirs = errs = skipped_reparse = 0
    stack = [root]
    t0 = time.perf_counter()
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs += 1
                            st = e.stat(follow_symlinks=False)
                            if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                                skipped_reparse += 1
                                continue
                            stack.append(e.path)
                        else:
                            files += 1
                    except OSError:
                        errs += 1
        except OSError:
            errs += 1
    return {"seconds": time.perf_counter() - t0, "files": files, "dirs": dirs,
            "errors": errs, "skipped_reparse": skipped_reparse}


# ===========================================================================
# 8. 主流程
# ===========================================================================

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P0 MFT 枚举可行性与权限验证（F0-04 / D11）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["auto", "mft", "scandir", "both"], default="auto",
                   help="auto = 只跑 MFT（scandir 要另加 --scandir）；mft = 同上；"
                        "scandir = 只跑 scandir（不需要提权）；both = 两边都跑")
    p.add_argument("--volumes", default=None,
                   help="逗号分隔的盘符，如 C,E。默认所有已挂载卷")
    p.add_argument("--scandir", nargs="?", const="ALL", default=None, metavar="卷",
                   help="对 MFT 成功的卷再跑一遍 os.scandir 做同卷对照。"
                        "不写卷名 = 所有成功的卷；写 `--scandir C,E` = 只跑这两个。"
                        "很慢（D: 有 1.37TB），所以建议只挑一个卷做基线")
    p.add_argument("--rounds", type=int, default=2, help="MFT 每卷跑几轮（默认 2：冷 + 暖）")
    p.add_argument("--buffer-kb", type=int, default=1024, help="DeviceIoControl 缓冲区 KiB（默认 1024）")
    p.add_argument("--sample", type=int, default=8, help="每卷保留前 N 条记录作解析证明（默认 8）")
    p.add_argument("--high-usn", choices=["journal", "max"], default="journal",
                   help="枚举上界取 journal 的 NextUsn（默认）还是 0x7FFF… 全量")
    p.add_argument("--log", default=None, help="额外把原始输出写一份到这个路径（UTF-8 / LF）")
    return p.parse_args(argv)


def filetime(ts: int) -> str:
    """FILETIME（自 1601 年的 100ns 计数）→ 本机时间字符串。样本里用来看记录是不是真的。"""
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(ts / 10_000_000 - 11644473600))
    except Exception:  # noqa: BLE001
        return "—"


def main() -> int:
    args = parse_args(sys.argv[1:])
    setup_output(args.log)

    facts = machine_facts()
    print_header(facts)

    elev = elevation_state()
    print_elevation(elev)

    wanted = ({s.strip().upper().rstrip(":") for s in args.volumes.split(",")}
              if args.volumes else None)
    vols = list_volumes(wanted)
    if not vols:
        print("\n没有匹配到任何卷，检查 --volumes。")
        return 1
    print_volumes(vols)

    do_mft = args.backend != "scandir"
    sc_wanted = None
    if args.scandir and args.scandir.upper() != "ALL":
        sc_wanted = {s.strip().upper().rstrip(":") for s in args.scandir.split(",") if s.strip()}
    do_scandir = args.backend in ("scandir", "both") or args.scandir is not None

    section("3. MFT 枚举（每卷逐轮计时）")
    if not do_mft:
        print("按 --backend 跳过（本次只量 scandir 基线）。")
    elif not elev["elevated"]:
        print("未提权 —— 仍会尝试打开卷句柄，把失败**如实记下来**（R15 的回退证据），")
        print("但这一次不能算 F0-04 通过，请到提权终端重跑。")

    results = []
    for v in vols:
        print(f"\n  --- {v['letter']}:  {DRIVE_TYPES.get(v['drive_type'], '?')} / "
              f"{v['fs'] or '未知文件系统'} ---")
        r = {"vol": v, "mft": None, "journal": None, "scandir": None}

        if not do_mft:
            results.append(r)
            continue

        h, err = open_volume(v["device"])
        if h is None:
            r["mft"] = {"ok": False, "winerror": err}
            print(f"    打开 {v['device']} 失败：WinError {err} {describe_winerror(err)}")
            results.append(r)
            continue
        print(f"    打开 {v['device']} 成功（拿到句柄 = 提权生效）")
        try:
            j = query_journal(h)
            r["journal"] = j
            if j["ok"]:
                print(f"    USN Journal   : 有。NextUsn={j['next']:,}  FirstUsn={j['first']:,}"
                      f"  最大 {human_bytes(j['max_size'])}")
            else:
                print(f"    USN Journal   : 无（WinError {j['winerror']} "
                      f"{describe_winerror(j['winerror'])}）")
                print(f"                    → USN 增量在该卷上要先用 "
                      f"`fsutil usn createjournal m=<MB> a=<MB> {v['letter']}:` 建日志；")
                print("                      **本探针不替你建**（那会改卷上的元数据）。")

            high = j["next"] if (args.high_usn == "journal" and j.get("ok")) else 0x7FFFFFFFFFFFFFFF
            print(f"    枚举上界      : {high:,}")

            mft = mft_rounds(h, high, args.buffer_kb, args.rounds, args.sample)
            r["mft"] = {"ok": all(x["ok"] for x in mft), "rounds": mft}
            if mft and mft[0]["ok"]:
                r["mft"].update({k: mft[0][k] for k in
                                 ("records", "dirs", "files", "reparse", "bytes",
                                  "calls", "sample", "bad_records", "other_versions",
                                  "versions")})
                if mft[0]["other_versions"]:
                    print(f"    ⚠ 有 {mft[0]['other_versions']} 条记录不是 USN_RECORD_V2"
                          f"（版本 {mft[0]['versions']}），已只计数未解析。")
                if mft[0]["bad_records"]:
                    print(f"    ⚠ 有 {mft[0]['bad_records']} 条记录长度异常，已跳过。")
                if mft[0]["sample"]:
                    print(f"    样本（前 {len(mft[0]['sample'])} 条，证明解析正确）：")
                    for s in mft[0]["sample"]:
                        kind = "目录" if s["attrs"] & FILE_ATTRIBUTE_DIRECTORY else "文件"
                        print(f"      FRN={s['frn'] & 0xFFFFFFFFFFFF:<12} "
                              f"父={s['parent'] & 0xFFFFFFFFFFFF:<12} {kind}  "
                              f"USN={s['usn']:<10} {filetime(s['ts'])}  {s['name']}")
        finally:
            _k32.CloseHandle(h)
        results.append(r)

    if do_scandir:
        section("4. os.scandir 基线（同卷对照；慢，且权限被拒的目录会少算）")
        for r in results:
            v = r["vol"]
            if sc_wanted is not None and v["letter"].upper() not in sc_wanted:
                print(f"\n  --- {v['letter']}: 跳过（不在 --scandir 指定的卷里）")
                continue
            # 有 MFT 结果的卷才做同卷对照；只跑 scandir 时（--backend scandir）
            # 没有对照对象，也要照常遍历。
            if do_mft and not (r["mft"] and r["mft"].get("ok")):
                print(f"\n  --- {v['letter']}: 跳过（MFT 没成功，没有对照对象）")
                continue
            print(f"\n  --- {v['letter']}: 遍历中，可能要几分钟 ……")
            s = scandir_count(v["root"])
            r["scandir"] = s
            rate = (s["files"] + s["dirs"]) / max(s["seconds"], 1e-9) * 60
            print(f"    {s['seconds']:9.3f} s  文件 {s['files']:,}  目录 {s['dirs']:,}  "
                  f"拒绝 {s['errors']:,}  → {rate:,.0f} 条/分钟")
            print(f"    跳过的 reparse point（junction/符号链接/挂载点，只计数不下潜）："
                  f"{s['skipped_reparse']:,}")

    # ---------------- 汇总 ----------------
    section("5. 汇总：M8 与 D11 的输入")
    if do_mft:
        print(f"{'卷':<4}{'MFT':<8}{'记录数':>12}{'文件':>12}{'目录':>11}"
              f"{'第1轮秒':>10}{'条/分钟':>15}")
        for r in results:
            v = r["vol"]
            m = r["mft"] or {}
            if m.get("ok"):
                first = m["rounds"][0]
                rate = m["records"] / max(first["seconds"], 1e-9) * 60
                print(f"{v['letter'] + ':':<4}{'可用':<8}{m['records']:>12,}{m['files']:>12,}"
                      f"{m['dirs']:>11,}{first['seconds']:>10.3f}{rate:>15,.0f}")
            else:
                we = m.get("winerror", "?")
                note = describe_winerror(we) if isinstance(we, int) else ""
                print(f"{v['letter'] + ':':<4}{'不可用':<8}    WinError {we} {note}")
    else:
        print("本次按 --backend 跳过了 MFT 段。")

    ok_vols = [r for r in results if r["mft"] and r["mft"].get("ok")]
    if ok_vols:
        total = sum(r["mft"]["records"] for r in ok_vols)
        t1 = sum(r["mft"]["rounds"][0]["seconds"] for r in ok_vols)
        fastest = max(r["mft"]["records"] / max(r["mft"]["rounds"][0]["seconds"], 1e-9) * 60
                      for r in ok_vols)
        print()
        print(f"  成功卷          : {len(ok_vols)} / {len(results)}")
        print(f"  记录总数        : {total:,}（含目录与 NTFS 元数据文件，**不是文件数**）")
        print(f"  第 1 轮合计耗时  : {t1:.3f} s → {total / max(t1, 1e-9) * 60:,.0f} 条/分钟（汇总）")
        print(f"  单卷最快        : {fastest:,.0f} 条/分钟")
        print(f"  M8 目标         : 2,000,000 文件/分钟 → "
              f"{'达到' if fastest >= 2_000_000 else '未达到'}（比值 {fastest / 2_000_000:.2f}×）")
        print()
        print("  ⚠ 上面的比值是「记录/分钟」对「文件/分钟」，分母不同——MFT 记录含目录")
        print("    与元数据文件，所以它**偏乐观**，引用时必须带上这一条。")
    elif do_mft:
        print("\n  没有任何卷的 MFT 枚举成功。")

    # scandir 与 MFT 的对照（两边都有数才算得出来）
    pairs = [r for r in results if r["scandir"] and r["mft"] and r["mft"].get("ok")]
    if pairs:
        print()
        print("  同卷对照（MFT 第 1 轮 vs scandir）：")
        print(f"    {'卷':<4}{'MFT 记录':>12}{'MFT 条/分':>14}{'scandir 条/分':>16}{'倍数':>9}")
        for r in pairs:
            m, s = r["mft"], r["scandir"]
            mft_rate = m["records"] / max(m["rounds"][0]["seconds"], 1e-9) * 60
            sc_rate = (s["files"] + s["dirs"]) / max(s["seconds"], 1e-9) * 60
            print(f"    {r['vol']['letter'] + ':':<4}{m['records']:>12,}{mft_rate:>14,.0f}"
                  f"{sc_rate:>16,.0f}{mft_rate / max(sc_rate, 1e-9):>8.1f}×")
        print("    （数量不等是预期的：MFT 含目录与元数据文件，scandir 只见得到的；")
        print("      逐条一致性归 F2-02，这里只比量级。）")

    rss = peak_rss()
    print()
    print(f"  峰值 RSS        : {human_bytes(rss) if rss > 0 else '取不到'}")
    print(f"  结束时时间      : {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # ---------------- 退出码 ----------------
    print()
    if do_mft and not elev["elevated"] and not ok_vols:
        print("退出码 2：要跑 MFT 但没提权，本次不能作为 F0-04 的证据。")
        return 2
    print("退出码 0：计划内的检查都完成。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n被中断。")
        sys.exit(130)
