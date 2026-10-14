"""卷的总线类型探测：`GetDriveType` 判不出本地性之后，还有没有别的判据。

对应问题：**`PROGRESS.md` §3 的 E10**（`GetDriveTypeW` 把非本地的 E: 报成
`DRIVE_FIXED(3)`，与 C:/D: 读数完全一样），以及它牵动的 `D11`（`backend="auto"`）与
`R14`（`--prune` 只在"本次真正可达的卷"内删条目）。

---- 本脚本回答的是一句话的问题 ------------------------------------------------

**"不提权能不能读到总线类型（`BusType`）？"**

能读到，才谈得上"用它来判本地性"；读不到，这条路在计划任务里就不用考虑了
（计划任务默认以 `InteractiveToken` 运行，但用户也可能改成 `S4U`/`SYSTEM`）。

---- 它**不**回答的问题，以及为什么 --------------------------------------------

**"总线类型能不能区分本地盘与非本地盘"——本脚本给不出结论。**

判据要在**已知的负例**上验：E: 是已知的非本地盘，只有把它的 `BusType` 也读出来，
才能说"USB 硬盘盒/iSCSI/VHD 报的是这些值，可以据此排除"。但**用户明确指示只碰
C: 与 D:**（`E:` 虽然报 `DRIVE_FIXED`，但不是本地盘），所以本脚本**不去开 E: 的
句柄**，只量 C: 与 D: 的读数。

于是它只能证明**技术可行**，不能证明**判据有效**。这个区别是本项目最在意的那一类：
"能读到字段"和"这个字段能分辨对错"是两件事，前者很容易被误当成后者。

---- 只读，不改任何卷元数据 ----------------------------------------------------

打开卷设备（`\\\\.\\C:`）只要**不请求写权限**就是只读查询，不发任何控制码去改状态。
与 `fsutil usn createjournal` 那种"改卷元数据"的操作是两件事。

---- 用法（仓库根执行）-----------------------------------------------------

    PYTHONIOENCODING=utf-8 uv run python tools/probe_volume_bus.py
    PYTHONIOENCODING=utf-8 uv run python tools/probe_volume_bus.py --drives C --json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from ctypes import wintypes
from datetime import datetime

# ---- 常量 -------------------------------------------------------------------

#: `IOCTL_STORAGE_QUERY_PROPERTY` = CTL_CODE(IOCTL_STORAGE_BASE, 0x500, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
STORAGE_DEVICE_PROPERTY = 0  # StorageDeviceProperty
PROPERTY_STANDARD_QUERY = 0

DRIVE_TYPES = {
    0: "未知", 1: "无根目录", 2: "可移动", 3: "固定盘", 4: "网络", 5: "光驱", 6: "RAM 盘",
}

#: `STORAGE_BUS_TYPE`（Win11 SDK 顺序）。虚线与斜线部分**故意留的是"这一类"**，
#: 不写具体产品名：本脚本只报读数，不替谁下判断。
BUS_TYPES = {
    0x00: "unknown", 0x01: "scsi", 0x02: "atapi", 0x03: "ata", 0x04: "1394", 0x05: "ssa",
    0x06: "fibre", 0x07: "usb", 0x08: "raid", 0x09: "iscsi", 0x0A: "sas", 0x0B: "sata",
    0x0C: "sd", 0x0D: "mmc", 0x0E: "virtual", 0x0F: "filebackedvirtual", 0x10: "spaces",
    0x11: "nvme", 0x12: "scm", 0x13: "ufs",
}

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]
_k32.GetVolumeInformationW.restype = wintypes.BOOL


def _read_cstr(buf: bytes, offset: int) -> str:
    if not offset:
        return ""
    end = buf.find(b"\x00", offset)
    return buf[offset:end if end >= 0 else len(buf)].decode("utf-8", "replace")


def bus_type(drive: str) -> dict:
    """一条卷的 `BusType` 与其自报的型号字符串。`drive` 形如 `"C:"`。

    失败时把 `WinError` 原样带回来——失败**本身**就是本次要问的那个问题的一半。
    """
    handle = _k32.CreateFileW(
        f"\\\\.\\{drive}", 0, 0x01 | 0x02, None, 3, 0, None  # 0 访问权 = 只查询
    )  # noqa: E501 —— 设备路径必须带冒号：`\\\\.\\C` 会得到 WinError 2（实测）
    invalid = wintypes.HANDLE(-1).value
    if handle is None or handle == invalid:
        err = ctypes.get_last_error()
        return {"ok": False, "error": f"CreateFile 失败 WinError {err}"}
    try:
        query = (ctypes.c_ubyte * 12)()
        ctypes.memmove(query, ctypes.byref(ctypes.c_int(STORAGE_DEVICE_PROPERTY)), 4)
        ctypes.memmove(
            ctypes.byref(query, 4), ctypes.byref(ctypes.c_int(PROPERTY_STANDARD_QUERY)), 4
        )
        out = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD()
        ok = _k32.DeviceIoControl(
            handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 12, out, 1024,
            ctypes.byref(returned), None,
        )
        if not ok:
            err = ctypes.get_last_error()
            return {"ok": False, "error": f"DeviceIoControl 失败 WinError {err}"}
        buf = bytes(out[: returned.value])
        if len(buf) < 36:
            return {"ok": False, "error": f"描述符只有 {len(buf)} 字节，短于 36"}
        dword = lambda off: int.from_bytes(buf[off : off + 4], "little")  # noqa: E731
        raw_bus = dword(28)
        return {
            "ok": True,
            "version": dword(0),
            "size": dword(4),
            "device_type": buf[8],
            "removable_media": bool(buf[10]),
            "command_queueing": bool(buf[11]),
            "vendor": _read_cstr(buf, dword(12)),
            "product": _read_cstr(buf, dword(16)),
            "revision": _read_cstr(buf, dword(20)),
            "bus_type": raw_bus,
            "bus_type_name": BUS_TYPES.get(raw_bus, f"未收录(0x{raw_bus:02X})"),
        }
    finally:
        _k32.CloseHandle(handle)


def volume(drive: str) -> dict:
    """`drive` 形如 `"C:"`：卷根带反斜杠、设备路径直接用盘符。"""
    root = f"{drive}\\"
    fs = ctypes.create_unicode_buffer(64)
    serial, max_component, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    ok = _k32.GetVolumeInformationW(
        root, None, 0,
        ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags),
        fs, 64,
    )
    kind = _k32.GetDriveTypeW(root)
    return {
        "drive": drive,
        "device_type": kind,
        "device_type_name": DRIVE_TYPES.get(kind, "?"),
        "filesystem": fs.value if ok else "",
        "bus": bus_type(drive),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drives", default="CD",
                    help="要探测的盘符（默认 CD）。⚠ 项目规定只碰 C: 与 D:，"
                         "加别的盘符之前先想清楚：非本地卷不是本地盘。")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给记录/脚本用）")
    args = ap.parse_args()

    drives = [f"{c.upper()}:" for c in args.drives if c.isalpha()]
    result = [volume(d) for d in drives]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"是否提权        {'是' if ctypes.windll.shell32.IsUserAnAdmin() else '否'}")
        for v in result:
            b = v["bus"]
            print(f"\n{v['drive']}  GetDriveType={v['device_type_name']}({v['device_type']})"
                  f"  文件系统={v['filesystem'] or '（读不到）'}")
            if b["ok"]:
                print(f"     BusType={b['bus_type_name']}(0x{b['bus_type']:02X})"
                      f"  可移动介质={b['removable_media']}  命令排队={b['command_queueing']}")
                print(f"     自报型号={b['vendor']} {b['product']} {b['revision']}".rstrip())
            else:
                print(f"     读不到：{b['error']}")
    print(f"\n机器时间        {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0 if all(v["bus"]["ok"] for v in result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
