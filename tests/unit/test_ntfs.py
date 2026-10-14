"""`F2-02` 的底层层（`ingest/ntfs.py`）的自动化测试。

------------------------------------------------------------------------------
为什么这一层要单独测、而且**大部分不用真卷**
------------------------------------------------------------------------------

这一层干的是"把一段字节解释成一条记录"。它错起来的样子是**数字不对**，而数字不对
在真机上表现为"扫描结果少了两万条"——从那个现象反推不回偏移，也没有第二个人能复现。
所以这里的切法是：

- **线格式用**人造缓冲区**测**（`parse_records` 是纯函数）。`F0-04` 的探针里最容易
  写错的一处就是"输出缓冲区的头 8 字节是下次的起点、不是记录"，它写错了只会读到
  一段垃圾，而测试可以直接把这个偏移钉死。
- **枚举循环用**替身 `DeviceIoControl`** 测**：真调用要管理员权限（`F0-04` 的负面
  对照实测是不提权就 `ERROR_ACCESS_DENIED`），而循环里真正需要验证的是**三种结束
  方式的区别**——正常 EOF、`ERROR_MORE_DATA`（缓冲区满了但记录有效）、真错误。
  这三条不提权也能验证，只要有人替内核回答。
- **`attributes_of` 用真文件测**：它不提权，也不需要卷。而且它有个正是靠真文件才能
  发现的性质——`st_mtime_ns` 与 `FILETIME` 的换算必须**逐位相等**，那是 `F2-02`
  的 MFT 后端补大小/时间的唯一来源，差一点点就是全库时间戳集体偏移。

不加 `skipif` 平台判断：`ntfs.py` 在非 Windows 上连 import 都过不去
（`ctypes.WinDLL`），这一层本来就是 Windows-only。加判断只是把"跑不了"伪装成
"跳过了"。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
from pathlib import Path

import pytest

from localsearch.ingest import ntfs

# ==============================================================================
# 工具：造反造 USN 记录与输出缓冲区
# ==============================================================================

_DIRECTORY = ntfs.FILE_ATTRIBUTE_DIRECTORY
_REPARSE = ntfs.FILE_ATTRIBUTE_REPARSE_POINT


def usn_v2(
    frn: int,
    parent: int,
    name: str,
    *,
    attrs: int = 0,
    major: int = 2,
    minor: int = 0,
) -> bytes:
    """造一条 `USN_RECORD_V2`。

    记录长度按真实约定**对齐到 8 字节**（内核就是这么写的），虽然解析器不依赖对齐。
    `Usn` 与 `TimeStamp` 填的是非零值：当前不读它们，但**填零会让"偏移读错了"这个
    错误变得看不出来**——非零值在 V3 计数那一条测试里正好当哨兵用。

    "名字越界"那一条测的是 `NameOffset` 本身出错，所以不在这里开参数：
    它得先造一条**长度合法**的记录，再把 `NameOffset` 改坏（见那条测试）。
    """
    raw = name.encode("utf-16-le")
    length = 60 + len(raw)
    length += (-length) % 8
    rec = bytearray(length)
    struct.pack_into("<I", rec, 0, length)
    struct.pack_into("<HH", rec, 4, major, minor)
    struct.pack_into("<QQ", rec, 8, frn, parent)
    struct.pack_into("<q", rec, 24, 0x0000_0001_0000_0000)   # Usn
    struct.pack_into("<q", rec, 32, 0x01DC_0000_0000_0000)   # TimeStamp（FILETIME）
    struct.pack_into("<I", rec, 40, 0)                       # Reason
    struct.pack_into("<I", rec, 48, 0)                       # SecurityId
    struct.pack_into("<I", rec, 52, attrs)
    struct.pack_into("<HH", rec, 56, len(raw), 60)
    rec[60:60 + len(raw)] = raw
    return bytes(rec)


def block(*records: bytes, next_frn: int = 0) -> bytes:
    """一块输出缓冲区：**头 8 字节是 next_frn**，后面才是记录。"""
    return struct.pack("<Q", next_frn) + b"".join(records)


def parse(data: bytes) -> tuple[list[ntfs.Record], ntfs.RecordCounters]:
    counters = ntfs.RecordCounters()
    return list(ntfs.parse_records(data, counters)), counters


class FakeVolume:
    """替掉 `ntfs._k32.DeviceIoControl` 的脚本化回答。

    脚本里每一项是 `(数据, 是否成功, last_error)`；数据为 `None` 表示"什么都没写、
    返回 0"（`ERROR_HANDLE_EOF` 就是这么来的）。同时把每次调用**输入的三件套**
    （start_frn / low_usn / high_usn）记下来，因为"第二次从哪继续"正是要验证的东西。
    """

    def __init__(self, script: list[tuple[bytes | None, bool, int]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[int, int, int]] = []
        self.handles: list[int] = []

    def __call__(self, handle, code, in_ptr, in_len, out_ptr, out_len, ret_ptr, overlap):
        assert code == ntfs.FSCTL_ENUM_USN_DATA
        self.handles.append(handle)
        self.calls.append(struct.unpack("<QQQ", ctypes.string_at(in_ptr, in_len)))
        data, ok, err = self.script.pop(0)
        written = 0 if data is None else len(data)
        if data:
            assert written <= out_len, "造的数据比输出缓冲区还大"
            ctypes.memmove(out_ptr, data, written)
        ctypes.cast(ret_ptr, ctypes.POINTER(wt.DWORD))[0] = written
        ctypes.set_last_error(err)
        return 1 if ok else 0


@pytest.fixture
def fake_volume(monkeypatch):
    """装上替身后返回它。`monkeypatch` 负责在测试结束把真函数放回去。"""

    def install(script):
        fake = FakeVolume(script)
        monkeypatch.setattr(ntfs._k32, "DeviceIoControl", fake)
        return fake

    return install


# ==============================================================================
# A. 线格式
# ==============================================================================


def test_the_first_eight_bytes_are_the_next_frn_not_a_record():
    """头 8 字节是"下次从哪继续"。把它当记录读就会读到一段垃圾，且**不会报错**。

    所以这里两头都钉：记录要正好是这几条，而且**一条坏记录都不许有**——如果解析
    从偏移 0 开始，`next_frn` 的值会被当成 `RecordLength`，`bad_records` 立刻非零。
    """
    data = block(
        usn_v2(0x1000_0000_0005, 0x1000_0000_0005, "."),
        usn_v2(0x2000_0000_0009, 0x1000_0000_0005, "readme.txt"),
        next_frn=0xDEAD_BEEF_0000_0007,      # 非零哨兵：当成记录长度就会立刻炸出坏记录
    )
    records, counters = parse(data)
    assert [r.name for r in records] == [".", "readme.txt"]
    assert counters.bad_records == 0
    assert counters.records == 2
    assert counters.other_versions == 0


def test_every_field_comes_from_its_own_offset():
    """一条记录里我们读四个字段：`frn` / `parent` / `attrs` / 名字。

    四个都取**互不相同、且不像长度**的值——写得像长度的时候，"读错了偏移"会碰巧
    还是合法值，只有 `bad_records` 会救你，而那不算测到。
    """
    frn = 0x0004_0000_0001_2345
    parent = 0x0009_0000_0000_0005
    data = block(usn_v2(frn, parent, "目录", attrs=_DIRECTORY | _REPARSE))
    (record,), counters = parse(data)
    assert record.frn == frn
    assert record.parent == parent
    assert record.name == "目录"
    assert record.attrs == _DIRECTORY | _REPARSE
    assert record.is_dir is True
    assert record.is_reparse is True
    # 记录号是低 48 位：序号（高 16 位）**不进**记录号。混淆两者会把复用的槽位
    # 认成同一个文件，那是 `scanner_mft` 断链判断的依据。
    assert record.record_number == 0x12345
    assert counters.records == 1


def test_a_name_that_is_not_valid_utf16_does_not_raise():
    """NTFS 允许名字里出现不成对的代理项。`errors="replace"` 是**故意的**：

    路径解出来是错的（带 U+FFFD），但它会在下一步读属性时变成一个**计数在案的
    错误**；而抛异常会让整轮扫描一条都不剩（`E16` 那一类）。两种坏法里选看得见的
    那种。
    """
    raw = b"\x41\xd8"                       # 单个高代理项，不成对
    record = bytearray(usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, ""))
    struct.pack_into("<HH", record, 56, len(raw), 60)
    struct.pack_into("<I", record, 0, 64)   # 长一点，放得下这个坏名字
    record[60:62] = raw
    records, counters = parse(block(bytes(record)))
    assert len(records) == 1
    assert "\ufffd" in records[0].name
    assert counters.bad_records == 0


def test_a_zero_length_name_is_a_record_not_an_error():
    """名字为空**不是**坏记录（长度 0 合法）。把"空名字"判成坏记录会让解析在第一条
    上就停住，而真机上确实有名字为空的记录（保留记录）。
    """
    record = bytearray(usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, ""))
    records, counters = parse(block(bytes(record)))
    assert [r.name for r in records] == [""]
    assert counters.bad_records == 0


def test_a_truncated_record_stops_the_block():
    """`RecordLength` 说还有 200 字节、缓冲区里只剩 100：**计数后停止解析这一块**。

    不猜着跳过它：长度字段读错时"下一条"的起点也是错的，继续解析只会得到一堆垃圾
    记录。所以后一条**合法**记录也不许吐出来——「宁可少几条，不要错一片」。
    """
    good = usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, "ok.txt")
    bad = bytearray(usn_v2(0x10_0000_0000_0002, 0x10_0000_0000_0005, "cut.txt"))
    struct.pack_into("<I", bad, 0, 200)
    after = usn_v2(0x10_0000_0000_0003, 0x10_0000_0000_0005, "never.txt")
    records, counters = parse(block(good, bytes(bad)[:100], after, next_frn=9))
    assert [r.name for r in records] == ["ok.txt"]
    assert counters.bad_records == 1
    assert counters.records == 1


def test_a_record_length_below_the_header_stops_the_block():
    """`RecordLength` 比头还短（这里是 8）：同上，停下来。"""
    bad = bytearray(usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, "x"))
    struct.pack_into("<I", bad, 0, 8)
    after = usn_v2(0x10_0000_0000_0002, 0x10_0000_0000_0005, "never.txt")
    records, counters = parse(block(bytes(bad), after))
    assert records == []
    assert counters.bad_records == 1


def test_a_name_outside_the_record_is_dropped_but_the_block_continues():
    """名字越界与"长度字段出错"是**两回事**：这一条的长度是可信的，所以还能走到
    下一条，只是这一条不吐出来（它不构成一条能用的记录）。
    """
    bad = bytearray(usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, "x"))
    struct.pack_into("<H", bad, 58, 8)      # NameOffset 指向记录头里面：名字越界
    good = usn_v2(0x10_0000_0000_0002, 0x10_0000_0000_0005, "after.txt")
    records, counters = parse(block(bad, good))
    assert [r.name for r in records] == ["after.txt"]
    assert counters.bad_records == 1
    assert counters.records == 1, "坏记录不许混进 records 计数"


def test_a_v3_record_is_counted_but_not_interpreted():
    """V3 的头布局不同（FRN 是 128 位），拿 V2 的偏移解释它只会得到垃圾数字。

    所以只计数、不吐出来，**并且要靠它验证偏移推进**：它后面那条 V2 必须照样吐出来
    ——推错了就会在那条上炸出坏记录。
    """
    v3 = usn_v2(0x10_0000_0000_0001, 0x10_0000_0000_0005, "v3.txt", major=3, minor=1)
    v2 = usn_v2(0x10_0000_0000_0002, 0x10_0000_0000_0005, "v2.txt")
    records, counters = parse(block(v3, v2))
    assert [r.name for r in records] == ["v2.txt"]
    assert counters.other_versions == 1
    assert counters.records == 2, "不能解释的记录也是记录，账要算上"
    assert counters.bad_records == 0
    assert counters.versions == {(3, 1), (2, 0)}


def test_a_payload_without_records_is_not_an_error():
    """空负载（只有 next_frn）与"末尾剩几个不够一条头的字节"都不是坏记录——
    `iter_records` 正是靠前者判断到底了。
    """
    records, counters = parse(block(next_frn=42))
    assert records == [] and counters.bad_records == 0

    records, counters = parse(block(next_frn=42) + b"\x00" * 59)
    assert records == [] and counters.bad_records == 0


# ==============================================================================
# B. 枚举循环（替身 DeviceIoControl）
# ==============================================================================


def test_enumeration_follows_the_returned_frn_and_ends_on_eof(fake_volume):
    """三件事一起验证：第一次从 0 开始、第二次从**上一块头 8 字节**给的 FRN 继续、
    `ERROR_HANDLE_EOF` 是正常结束（不抛）。
    """
    first = block(usn_v2(1, 5, "a.txt"), next_frn=0x2A)
    second = block(usn_v2(2, 5, "b.txt"), next_frn=0x99)
    fake = fake_volume([(first, True, 0),
                        (second, True, 0),
                        (None, False, ntfs.ERROR_HANDLE_EOF)])
    counters = ntfs.RecordCounters()
    records = list(ntfs.iter_records(7, counters))
    assert [r.name for r in records] == ["a.txt", "b.txt"]
    assert [c[0] for c in fake.calls] == [0, 0x2A, 0x99]
    # 上界用 HIGH_USN 而不是日志水位：D: 上根本没有 USN Journal（F0-04 实测），
    # 而枚举不依赖它。
    assert all(c == (c[0], 0, ntfs.HIGH_USN) for c in fake.calls)
    assert counters.calls == 3
    assert counters.bytes_read == len(first) + len(second)
    assert counters.records == 2


def test_more_data_keeps_the_records_already_written(fake_volume):
    """`ERROR_MORE_DATA` 表示"缓冲区放不下"，但**写好的那部分照常用**。

    把它当失败处理会在大卷上丢掉恰好一个缓冲区大小的记录——而在 1 MiB 缓冲上
    那是几万条，且只在特定盘上出现。
    """
    first = block(usn_v2(1, 5, "a.txt"), next_frn=0x2A)
    fake = fake_volume([(first, False, ntfs.ERROR_MORE_DATA),
                        (None, False, ntfs.ERROR_HANDLE_EOF)])
    records = list(ntfs.iter_records(7))
    assert [r.name for r in records] == ["a.txt"]
    assert fake.calls[1][0] == 0x2A, "MORE_DATA 之后仍然要按返回的 FRN 继续"


def test_an_unexpected_error_becomes_a_volume_error(fake_volume):
    """真错误如实抛 `VolumeError`，并把**原始数值**带上——上层的分流（回退
    scandir / 判定不是 NTFS）就是按它做的，一句人话不够用。
    """
    first = block(usn_v2(1, 5, "a.txt"), next_frn=0x2A)
    fake_volume([(first, True, 0), (None, False, ntfs.ERROR_ACCESS_DENIED)])
    seen: list[str] = []
    with pytest.raises(ntfs.VolumeError) as err:
        for record in ntfs.iter_records(7):
            seen.append(record.name)
    assert err.value.winerror == ntfs.ERROR_ACCESS_DENIED
    assert err.value.stage == "枚举 MFT 记录"
    # 流式：抛之前已经吐出去的那些**已经交给调用方了**，这里如实反映这一点。
    assert seen == ["a.txt"]


def test_a_buffer_without_records_ends_the_enumeration(fake_volume):
    """返回值就是 8（只有 next_frn）时直接收工——不然会拿它当一块空记录，
    在结尾多绕一轮。
    """
    fake = fake_volume([(block(next_frn=1234), True, 0)])
    records = list(ntfs.iter_records(7))
    assert records == []
    assert len(fake.calls) == 1


def test_the_handle_is_passed_through_unchanged(fake_volume):
    """句柄原样传给内核——这一层不缓存、不包装它。"""
    fake = fake_volume([(None, False, ntfs.ERROR_HANDLE_EOF)])
    list(ntfs.iter_records(4321))
    assert fake.handles == [4321]


# ==============================================================================
# C. 卷、错误与翻译
# ==============================================================================


@pytest.mark.parametrize("volume", ["c:", "C", "C:/", "C:\\", "  e:  "])
def test_device_of_normalizes_a_drive_letter(volume: str):
    letter = "E" if volume.strip().startswith(("e", "E")) else "C"
    assert ntfs.device_of(volume) == f"\\\\.\\{letter}:"


@pytest.mark.parametrize("volume", ["", "//server/share", "CD:", "1:", "\\\\?\\C:"])
def test_device_of_refuses_what_has_no_volume(volume: str):
    """UNC 与相对路径**没有卷**可言。这里报错而不是猜一个：猜错就会去枚举一个
    不相干的卷，把整盘路径算成另一个盘的后代。
    """
    with pytest.raises(ValueError):
        ntfs.device_of(volume)


def test_volume_facts_reads_the_real_c_drive():
    """对着真 C: 核一遍读数。`drive_type` 只是**筛子**——`E10` 实测 `E:` 也报
    本地固定盘，所以下面这条**证明不了**本地性，真正的判据是打开卷设备成不成功。
    """
    facts = ntfs.volume_facts("C:")
    assert facts["ok"] is True
    assert facts["fs"] == "NTFS"
    assert facts["drive_type"] == ntfs.DRIVE_FIXED
    assert facts["drive_type_name"] == "本地固定"
    assert facts["letter"] == "C:"


def test_describe_winerror_answers_only_what_it_knows():
    assert "ERROR_ACCESS_DENIED" in ntfs.describe_winerror(ntfs.ERROR_ACCESS_DENIED)
    assert "不允许" in ntfs.describe_winerror(ntfs.ERROR_ACCESS_DENIED)
    # 不认识的码返回空串，而不是"未知错误"：空串让调用方**有得选**（印 WinError
    # 数值、或去问 FormatMessage），一句含糊的话把它锁死了。
    assert ntfs.describe_winerror(12345) == ""


def test_volume_error_keeps_the_raw_number():
    err = ntfs.VolumeError("\\\\.\\C:", "打开卷设备", ntfs.ERROR_ACCESS_DENIED)
    assert (err.device, err.stage, err.winerror) == ("\\\\.\\C:", "打开卷设备", 5)
    text = str(err)
    assert "\\\\.\\C:" in text and "打开卷设备" in text and "5" in text
    assert "提权" in text

    unknown = ntfs.VolumeError("\\\\.\\C:", "枚举 MFT 记录", 12345)
    assert "None" not in str(unknown) and str(unknown).endswith("12345")


def test_opening_a_volume_that_cannot_exist_reports_the_device():
    """不存在的设备名 → 一样是 `VolumeError`，而且**设备名要进消息**：多卷时
    "哪个卷开的失败"是分流的第一句。
    """
    # 没有卷的盘符报的是 `DRIVE_NO_ROOT_DIR(1)`，**不是** `DRIVE_UNKNOWN(0)`——
    # 顺带说明 `volume_facts` 的读数是可信的（本机实测）。
    absent = next(
        (f"{letter}:" for letter in "ZYXWVUTSRQPONMLKJIHGF"
         if ntfs.volume_facts(f"{letter}:")["drive_type"] in (0, 1)),
        None,
    )
    if absent is None:                      # 不太可能，但别让测试变成"看机器"
        pytest.skip("这台机器上从 J 到 Z 每个盘符都有卷")
    with pytest.raises(ntfs.VolumeError) as err:
        ntfs.open_volume(ntfs.device_of(absent))
    assert err.value.winerror != 0
    assert err.value.stage == "打开卷设备"
    assert ntfs.device_of(absent) in str(err.value)


# ==============================================================================
# D. 一条路径的属性（真文件，不提权）
# ==============================================================================


def test_attributes_of_agrees_with_os_stat(tmp_path: Path):
    """`F2-02` 的 MFT 后端补大小/时间**只靠这一条**。所以它不是"看起来差不多"，
    是逐条相等：USN 记录里没有大小，问错了就是全库大小错。
    """
    target = tmp_path / "文件.txt"
    target.write_bytes(b"x" * 4321)
    os.utime(target, ns=(1_700_000_000_123_456_700, 1_700_000_000_987_654_300))

    stat = os.stat(target)
    info = ntfs.attributes_of(str(target))
    assert info.size == stat.st_size == 4321
    assert info.mtime_ns == stat.st_mtime_ns
    assert info.dwFileAttributes & _DIRECTORY == 0

    directory = tmp_path / "子目录"
    directory.mkdir()
    assert ntfs.attributes_of(str(directory)).dwFileAttributes & _DIRECTORY == _DIRECTORY


def test_filetime_conversion_round_trips_bit_for_bit():
    """`116444736000000000` 这个常量写错一位，结果是全库时间戳整体偏离若干年——
    而"看起来是合理的时间"让这件事很难被发现。所以直接对着换算做往返。
    """
    stat = os.stat(Path(__file__))
    expected = stat.st_mtime_ns
    filetime = expected // 100 + 116444736000000000
    info = ntfs.FileAttrs()
    info.ftLastWriteTime.dwLowDateTime = filetime & 0xFFFFFFFF
    info.ftLastWriteTime.dwHighDateTime = filetime >> 32
    assert info.mtime_ns == expected

    info.nFileSizeHigh, info.nFileSizeLow = 0x0000_0001, 0x0000_0000
    assert info.size == 1 << 32, "大小是高低 32 位拼的，别只读低位"


def test_attributes_of_a_missing_path_raises_the_same_shape_as_os_stat(tmp_path: Path):
    """扫描途中文件被删是常态，不是异常——所以这里抛的是**带 winerror 与文件名的
    `OSError`**，调用方按普通错误记进账。

    而且形状要与 `os.stat` 一致（三个字段逐个核）：scandir 后端那边读属性失败走的
    就是 `os.stat`，两个后端在 `files.error_msg` 里留下的文本因此长一个样——
    `winerror` 尤其重要，它是上层分流（权限问题 vs 文件没了）的依据。
    """
    gone = tmp_path / "不存在的文件"
    with pytest.raises(OSError) as err:
        ntfs.attributes_of(str(gone))
    ours = err.value
    try:
        os.stat(gone)
    except OSError as exc:
        theirs = exc
    assert (ours.errno, ours.winerror, ours.filename) == (
        theirs.errno, theirs.winerror, str(gone))
    assert ours.winerror == 2                     # ERROR_FILE_NOT_FOUND
