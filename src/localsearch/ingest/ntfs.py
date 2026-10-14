r"""NTFS 卷设备的底层访问（P2 / `F2-02`）。测试落点 `tests/unit/test_ntfs.py`。

这一层**不含任何扫描语义**——它只回答三件事，答完就完：

1. 按卷拿设备句柄（`\\.\X:`），并如实翻译 WinError；
2. 用 `FSCTL_ENUM_USN_DATA` **流式**枚举 MFT 记录，解析成 `Record`；
3. 问一条路径的大小与时间（`GetFileAttributesExW`）。

------------------------------------------------------------------------------
为什么不用 mftparser / pywin32
------------------------------------------------------------------------------

`F0-04` 实测（2026-10-14）确认 `ctypes` 直接调 `DeviceIoControl` 够用，且**零新增
依赖**（计划 §7.4 与 §6 的实测结论）。这不是"不想用库"，是"库要干的事只有两个
控制码"：`FSCTL_ENUM_USN_DATA` 拿记录、`FSCTL_QUERY_USN_JOURNAL` 拿日志水位
（后者 `F6-01` 才用）。解析出来的是固定偏移的定长结构，用 `struct` 读比引一层
依赖更可控——**尤其出错的时候**。

------------------------------------------------------------------------------
为什么补 size 不用 `os.stat`
------------------------------------------------------------------------------

2026-10-15 01:xx 实测（`D:/work` 上的 60,000 个真实文件，同一批路径各跑两轮）：

| 办法 | 冷 | 热 |
|---|---|---|
| `os.stat(p, follow_symlinks=False)` | 36.93 µs/次 | 35.85 µs/次 |
| `GetFileAttributesExW(p, ...)` | **14.15 µs/次** | **14.00 µs/次** |

抽样 200 条比对 `st_size` 与 `st_mtime_ns`：**0 条不一致**。
差的 2.6× 来自实现：CPython 的 `os.stat` 在 Windows 上是
`CreateFileW` + `GetFileInformationByHandle` + `CloseHandle` 三条调用（外加杀软的
开句柄回调），而 `GetFileAttributesExW` 一条就够、**不开句柄**。

⚠ **最早那次测量把冷写成 69.42 µs**（同一脚本、同一棵树），与这次的 36.93 µs 差
近一倍。两次之间没有可控变量，所以**只认"36 µs 量级"这个结论**，69 µs 那个读数
当作噪声（原始输出见 `PROGRESS.md` §3 的 E23）。

**为什么这件事值得先量**：USN 记录里**没有文件大小**，MFT 后端要补大小就得逐条
再问一次文件系统。按 `os.stat` 的单价，C: 全量 273 万条要多花 **97 s**——MFT 相对
scandir 的枚举优势会**整条赔进去**。换 `GetFileAttributesExW` 后是 38 s。

------------------------------------------------------------------------------
权限
------------------------------------------------------------------------------

打开卷设备要管理员，`F0-04` 的**负面对照**实测过：不提权时 `CreateFileW` 返回
`ERROR_ACCESS_DENIED(5)`（不是"打不开就算了"，是明确拒绝）。这一层如实抛出
`VolumeError`，**回退的决策在上层**（`scanner_mft` + `base.make_scanner`）。
`GetFileAttributesExW` **不需要**权限，所以补大小的那一步不提权也能跑。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
from dataclasses import dataclass, field
from typing import Iterator

# ==============================================================================
# 常量
# ==============================================================================

#: `CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 44, METHOD_NEITHER, FILE_ANY_ACCESS)`。
#: 注意它是 **METHOD_NEITHER**，所以输入输出缓冲区要自己保证可写/可读，参数
#: 传的是指针而不是 `byref`（`probe_mft.py` 从 P0 起就是这么调的，实测可用）。
FSCTL_ENUM_USN_DATA = 0x000900B3

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

# ---- 只列真要分支的错误码
ERROR_ACCESS_DENIED = 5
ERROR_HANDLE_EOF = 38
ERROR_MORE_DATA = 234
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_SUPPORTED = 50

# ---- 卷类型 / 文件系统
DRIVE_FIXED = 3
DRIVE_TYPES = {0: "未知", 1: "无根目录", 2: "可移动", 3: "本地固定",
               4: "网络", 5: "光驱", 6: "RAM 盘"}

# ---- 文件属性位（与 `base.FILE_ATTRIBUTE_REPARSE_POINT` 是同一个位）
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

# ---- MFT_ENUM_DATA_V0：StartFileReferenceNumber(8) LowUsn(8) HighUsn(8)
_MFT_ENUM_DATA_V0 = "<QQQ"

#: USN_RECORD_V2 头部 60 字节，名字跟在 `FileNameOffset`（V2 通常是 60）处。
_USN_RECORD_HEADER = 60

#: 枚举上界。用 `0x7FFF…` 而不是 USN 日志的 `NextUsn`：**D: 上根本没有日志**
#: （`F0-04` 实测 `ERROR_JOURNAL_NOT_ACTIVE`），而枚举本身不依赖日志。
#: `F0-04` 在 D: 上就是用这个上界跑通的，386 万条记录。
HIGH_USN = 0x7FFFFFFFFFFFFFFF

#: 一次 `DeviceIoControl` 的输出缓冲区。1 MiB 是 P0 探针的默认值，实测可用；
#: 调大能少几次调用，但每条记录的处理时间不变，收益很小。
BUFFER_BYTES = 1 << 20

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                 ctypes.c_void_p]
_k32.DeviceIoControl.restype = wt.BOOL
_k32.GetDriveTypeW.argtypes = [wt.LPCWSTR]
_k32.GetDriveTypeW.restype = wt.UINT
_k32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                                       ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD]
_k32.GetVolumeInformationW.restype = wt.BOOL
_k32.GetFileAttributesExW.argtypes = [wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p]
_k32.GetFileAttributesExW.restype = wt.BOOL


# ==============================================================================
# 错误
# ==============================================================================


def describe_winerror(code: int) -> str:
    """把要分支的那几个错误码翻成人话。**认不出来的返回空串**，让调用方自己决定
    是印 `WinError 1234` 还是去查 `FormatMessage`。"""
    return {
        ERROR_ACCESS_DENIED: "ERROR_ACCESS_DENIED —— 没提权，或该卷不允许（MFT 需要管理员）",
        ERROR_INVALID_FUNCTION: "ERROR_INVALID_FUNCTION —— 该卷不支持这个控制码（多半不是 NTFS）",
        ERROR_NOT_SUPPORTED: "ERROR_NOT_SUPPORTED —— 该文件系统不支持",
        ERROR_JOURNAL_NOT_ACTIVE: "ERROR_JOURNAL_NOT_ACTIVE —— 卷上没有活动的 USN Journal",
        ERROR_HANDLE_EOF: "ERROR_HANDLE_EOF —— 枚举正常结束",
    }.get(code, "")


class VolumeError(Exception):
    """这一层开不起来或读到一半坏掉。

    `winerror` 是决策依据：`ERROR_ACCESS_DENIED` → 该回退 scandir；
    `ERROR_INVALID_FUNCTION` → 这个卷不是 NTFS。上层按它分流，所以它必须是**原始
    数值**，不能只有一句人话。
    """

    def __init__(self, device: str, stage: str, winerror: int) -> None:
        self.device = device
        self.stage = stage
        self.winerror = int(winerror)
        hint = describe_winerror(self.winerror)
        super().__init__(
            f"{stage} 失败：{device} WinError {self.winerror}"
            + (f" {hint}" if hint else "")
        )


# ==============================================================================
# 卷
# ==============================================================================


def device_of(volume: str) -> str:
    """卷（`"c:"` / `"C"` / `"C:/"`）→ 设备路径 `\\.\\C:`。

    只认盘符形态；UNC（`//server/share`）与相对路径在这里就没有卷可言，调用方
    （`base.drive_of`）先判过，这里直接报错而不是猜一个。
    """
    letter = volume.strip().rstrip("/\\").rstrip(":").strip()
    if len(letter) != 1 or not letter.isalpha():
        raise ValueError(f"不是一个盘符：{volume!r}")
    return f"\\\\.\\{letter.upper()}:"


def volume_facts(volume: str) -> dict[str, object]:
    """盘符根上问一次驱动类型与文件系统名。**不需要提权。**

    只用来做"这个卷看起来像不像本地 NTFS"的**第一道筛子**，它证明不了任何事：
    `E10` 实测本机 `E:` 报 `DRIVE_FIXED(3)` + NTFS，与 C:/D: 读数完全一样，而它
    不是本地盘。真正的判据是**打开卷设备成不成功**（那才是"提权 + 支持这个控制码"）。
    """
    root = volume.strip().rstrip("/\\") + "\\"
    letter = root[:2]
    drive_type = int(_k32.GetDriveTypeW(root))
    fs_name_buf = ctypes.create_unicode_buffer(256)
    ok = _k32.GetVolumeInformationW(root, None, 0, None, None, None, fs_name_buf, 256)
    return {
        "letter": letter,
        "drive_type": drive_type,
        "drive_type_name": DRIVE_TYPES.get(drive_type, "?"),
        "fs": fs_name_buf.value if ok else "",
        "ok": bool(ok),
    }


def open_volume(device: str) -> int:
    """打开卷设备。**只读 + 共享读写**——不表达任何写意图，这一层永远不动卷上的数据。

    不提权时是明确的 `ERROR_ACCESS_DENIED(5)`；`F0-04` 的负面对照就是这么取到的。
    """
    handle = _k32.CreateFileW(device, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise VolumeError(device, "打开卷设备", ctypes.get_last_error())
    return int(handle)


def close_volume(handle: int) -> None:
    _k32.CloseHandle(handle)


# ==============================================================================
# MFT 记录
# ==============================================================================


@dataclass(frozen=True, slots=True)
class Record:
    """一条 `USN_RECORD_V2` 里我们**现在**用得上的四个字段。

    `frn` 与 `parent` 都是 64 位文件引用号：**低 48 位是 MFT 记录号，高 16 位是
    序号**。序号会在记录被复用时递增，所以"同一个文件"要么比完整的 64 位，要么
    比完整 64 位（见 `scanner_mft` 的断链判断）——**只比记录号会把复用的槽位认成
    同一个文件**。

    ⚠ 这里**故意不含 `Usn` 与 `TimeStamp`**：`F6-01`（USN 增量）要用它们，P2 不用。
    它们的偏移写在 `parse_records` 旁边，接进来的时候照抄即可。
    """

    frn: int
    parent: int
    name: str
    attrs: int

    @property
    def record_number(self) -> int:
        return self.frn & 0xFFFFFFFFFFFF

    @property
    def is_dir(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass
class RecordCounters:
    """枚举过程中的账。**解析出来的异常也是数据**，不能只在日志里出现一次。"""

    records: int = 0
    #: `RecordLength` 不合理（太小 / 超出缓冲区）：计数后**停止解析这一块**。
    #: 为什么不猜着跳过它：长度字段读错时，"下一条"的起点也是错的，继续解析
    #: 只会得到一堆垃圾记录——宁可少几条也不要错一片。
    bad_records: int = 0
    #: 不是 V2（头布局不同，FRN 是 128 位）。拿 V2 的偏移解释 V3 只会得到垃圾数字。
    other_versions: int = 0
    calls: int = 0
    bytes_read: int = 0
    versions: set[tuple[int, int]] = field(default_factory=set)


def parse_records(data: bytes, counters: RecordCounters) -> Iterator[Record]:
    """解析一块 `FSCTL_ENUM_USN_DATA` 的输出缓冲区。

    **输出缓冲区的头 8 字节是"下一次从哪个 FRN 继续"，不是记录**——这一点是
    `F0-04` 的探针里最容易写错的地方（把偏移当成 0 就会读到一段垃圾）。
    返回值里不含它，调用方要自己从 `data[:8]` 取。

    做成纯函数（不碰卷、不碰 ctypes）是为了能测：**线格式只有拿人造缓冲区才测得
    干净**——真机上第一条记录解析错了，看到的只是"数字不对"，看不出错在哪个偏移。
    """
    n = len(data)
    off = 8
    while off + _USN_RECORD_HEADER <= n:
        (rlen,) = struct.unpack_from("<I", data, off)
        if rlen < _USN_RECORD_HEADER or off + rlen > n:
            counters.bad_records += 1
            return
        major, minor = struct.unpack_from("<HH", data, off + 4)
        counters.versions.add((major, minor))
        if major != 2:
            counters.other_versions += 1
            counters.records += 1
            off += rlen
            continue
        frn, parent = struct.unpack_from("<QQ", data, off + 8)
        # off+24 是 Usn（<q），off+32 是 TimeStamp（<q，FILETIME）——F6-01 用。
        (attrs,) = struct.unpack_from("<I", data, off + 52)
        name_len, name_off = struct.unpack_from("<HH", data, off + 56)
        if name_off < _USN_RECORD_HEADER or name_off + name_len > rlen:
            counters.bad_records += 1
        else:
            raw = data[off + name_off:off + name_off + name_len]
            counters.records += 1
            yield Record(frn=frn, parent=parent,
                         # `errors="replace"`：NTFS 允许名字里出现不成对的代理项，
                         # 那种名字解出来是 U+FFFD —— 路径因而是**错的**，但它会
                         # 在下一步（读属性）变成一个**计数在案的错误**，而不是
                         # 在报表里炸掉整轮扫描（`E16` 那一类：编码报错会让一整轮
                         # 输出一条都不剩）。两种坏法里选看得见的那种。
                         name=raw.decode("utf-16-le", errors="replace"),
                         attrs=attrs)
        off += rlen


def iter_records(handle: int, counters: RecordCounters | None = None,
                 *, buffer_bytes: int = BUFFER_BYTES) -> Iterator[Record]:
    """流式枚举整个卷的 MFT 记录。**边读边吐，不攒列表。**

    大卷上记录是数百万条（C: 实测 273 万），攒起来就是几百 MB 到几 GB 的常驻内存。
    调用方自己决定留什么——`scanner_mft` 留的是**紧凑数组**，不是 `Record` 对象。
    """
    counters = counters if counters is not None else RecordCounters()
    buf = ctypes.create_string_buffer(buffer_bytes)
    ret = wt.DWORD(0)
    start_frn = 0
    while True:
        payload = struct.pack(_MFT_ENUM_DATA_V0, start_frn, 0, HIGH_USN)
        in_buf = ctypes.create_string_buffer(payload, len(payload))
        ok = _k32.DeviceIoControl(handle, FSCTL_ENUM_USN_DATA,
                                  ctypes.cast(in_buf, ctypes.c_void_p), len(payload),
                                  ctypes.cast(buf, ctypes.c_void_p), buffer_bytes,
                                  ctypes.byref(ret), None)
        counters.calls += 1
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_HANDLE_EOF:
                return                       # 正常结束
            if err != ERROR_MORE_DATA:
                raise VolumeError(f"handle {handle}", "枚举 MFT 记录", err)
            # ERROR_MORE_DATA：缓冲区放不下，但已写好的部分照常处理
        n = int(ret.value)
        if n <= 8:                       # 只有 next_frn、没有记录 → 到底了
            return
        counters.bytes_read += n
        data = ctypes.string_at(buf, n)
        start_frn = struct.unpack_from("<Q", data, 0)[0]
        yield from parse_records(data, counters)


# ==============================================================================
# 一条路径的属性
# ==============================================================================


class FileAttrs(ctypes.Structure):
    """`WIN32_FILE_ATTRIBUTE_DATA` 里我们要的两项，外加属性位本身。

    直接复用 Win32 结构体而不是另造一个 dataclass：字段偏移由 ctypes 保证，
    少一次转换也少一处写错的机会。
    """

    _fields_ = [("dwFileAttributes", wt.DWORD),
                ("ftCreationTime", wt.FILETIME),
                ("ftLastAccessTime", wt.FILETIME),
                ("ftLastWriteTime", wt.FILETIME),
                ("nFileSizeHigh", wt.DWORD),
                ("nFileSizeLow", wt.DWORD)]

    @property
    def size(self) -> int:
        return (self.nFileSizeHigh << 32) | self.nFileSizeLow

    @property
    def mtime_ns(self) -> int:
        """`FILETIME`（1601-01-01 起的 100 ns）→ Unix 纪元起的纳秒。

        `116444736000000000` 是 1601→1970 的 100 ns 数。这个换算与 `os.stat` 的
        `st_mtime_ns` 在 200 条抽样上**逐条相等**（2026-10-15 实测）。
        """
        ft = (self.ftLastWriteTime.dwHighDateTime << 32) | self.ftLastWriteTime.dwLowDateTime
        return (ft - 116444736000000000) * 100


def attributes_of(path: str) -> FileAttrs:
    """问一条路径的属性。**不开句柄**，因而不触发杀软的开句柄回调，也不提权。

    路径不存在 / 权限不够时抛 `OSError`（带 `winerror`），由调用方计入错误账——
    扫描途中文件被删是常态，不是异常。

    ⚠ **未实测**：对云占位文件（OneDrive「仅在线」，reparse point）这一条会不会
    触发下载。`GetFileAttributesExW` 按文档只读属性、不读数据，而 `scandir` 后端
    在同样这些条目上连问都不问（名字从枚举里带回来），所以这是 MFT 独有的一处
    行为差异。真机上遇到再记。
    """
    info = FileAttrs()
    if not _k32.GetFileAttributesExW(path, 0, ctypes.byref(info)):
        err = ctypes.get_last_error()
        # 抛出来的是**与 `os.stat` 同一种形状**的异常：`ctypes.WinError` 会把 Windows
        # 错误码映射成 `errno`、保留原始的 `winerror`、消息取自系统（本机是中文），
        # 再补上 `filename` 就与 CPython 自己抛的一模一样——
        # `PermissionError(13, '拒绝访问。')` + `winerror=5` + `filename=...`。
        #
        # ⚠ 不要图省事写 `OSError(err, os.strerror(err), path)`：`os.strerror` 认的是
        # **POSIX errno**，而对 Windows 错误码 5 它会给出 "Input/output error"——一句
        # 与"拒绝访问"毫不相干的话，还把 `winerror` 丢掉（`winerror` 只有传第 4 个
        # 位置参数才有）。两个后端在 `files.error_msg` 里留下的文本也因此长一个样。
        exc = ctypes.WinError(err)
        exc.filename = path
        raise exc
    return info
