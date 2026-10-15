r"""`core/paths.py`：路径的可达性（Windows 保留设备名的绕行）。

------------------------------------------------------------------------------
这一组测试为什么这样切
------------------------------------------------------------------------------

被测的只有一个函数，但它存在的理由和它接到的调用点是**同一件事的三面**：

- **纯字符串那一半不碰盘**：转换规则（哪些名字算、什么形态不转）是纯粹的字面判断，
  一次跑完几毫秒。
- **平台事实那一半要真建文件**：`nul` 这一族只有在 `\\?\` 下才建得出来，而"原样路径
  会问到设备上"**不是我们的代码决定的**——它是 Win32 的名字解析，测试把它记下来，
  顺带把"绕行确实指到真文件"钉死。哪天 Windows 改了这条行为，这几条会红，那正是
  该有人来看一眼的时候（Windows 变了，还是我们的绕行坏了？两种都值得知道）。
- **调用点那一半**测的是"绕行真的接上了"：`ntfs.attributes_of`（MFT 后端补属性——
  `F2-02` 真机核对抓出的 2 条差异就在这里）、`fingerprint.content_hash`（读到 0 字节
  的那一半）、`fingerprint.quick_sig_of`。每条只有一两行断言，防的是"将来有人把那行
  `extended_path` 去掉，而所有测试照样绿"。扫描器两头（名字是设备名的**根**）各一条，
  在 `test_scanner_scandir.py` 与 `test_scanner_mft.py`——那边有各自的替身与助手。

⚠ 造出来的 `nul` **必须自己用扩展写法删掉**：原样 `os.unlink` 报 winerror 5，
`shutil.rmtree` 因此也失败，而 pytest 清 `tmp_path` 用的正是 rmtree（2026-10-15 实测）。
不自己清就是把 pytest 的临时目录毒掉——症状是测试**通过**、收尾时报一串清理错误。

`tmp_path` 落在 `C:` 下的 pytest 临时目录里——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from localsearch.core.paths import DEVICE_NAMES, extended_path
from localsearch.ingest import fingerprint, ntfs

#: 设备名文件里装的正文。用 ASCII 写，好让"字节数 == 字符数"这句话不需要解释。
CONTENT = "nul is a real file"


# ==============================================================================
# 工具
# ==============================================================================


@pytest.fixture
def device_file(tmp_path):
    """真名为 `nul` 的文件，返回**原样路径**（字符串）。收尾自己删（见模块文档）。"""
    plain = str(tmp_path / "nul")
    with open(extended_path(plain), "w", encoding="utf-8") as fh:
        fh.write(CONTENT)
    try:
        yield plain
    finally:
        os.unlink(extended_path(plain))


@pytest.fixture
def device_dir(tmp_path):
    r"""真名为 `nul` 的**目录**（里面有一个文件），返回 (原样目录, 原样子文件)。

    目录这一格与文件不同：实测原样 `os.scandir(…\\nul)` **能列出来**，原样
    `Path.is_dir()` 却返回 False——两个答案来自两条不同的路。
    """
    dir_plain = str(tmp_path / "nul")
    os.mkdir(extended_path(dir_plain))
    inner = os.path.join(dir_plain, "inner.txt")
    with open(inner, "w", encoding="utf-8") as fh:      # 中间一层是设备名，实测不影响
        fh.write("in")
    try:
        yield dir_plain, inner
    finally:
        os.unlink(extended_path(inner))
        os.rmdir(extended_path(dir_plain))


# ==============================================================================
# 1. 转换规则：纯字符串，不碰盘
# ==============================================================================


def test_a_device_name_as_the_last_segment_gets_the_prefix():
    """最后一段是设备名就转，正斜杠换成反斜杠、前缀只加一层。"""
    assert extended_path("D:/x/nul") == "\\\\?\\D:\\x\\nul"
    assert extended_path("D:/x/NUL") == "\\\\?\\D:\\x\\NUL"      # 大小写不敏感
    assert extended_path("D:/x/nul.") == "\\\\?\\D:\\x\\nul."    # 尾部的点也中招
    assert extended_path("D:/x/nul ") == "\\\\?\\D:\\x\\nul "    # 尾部的空格同上
    assert extended_path("D:/x/nul/") == "\\\\?\\D:\\x\\nul"     # 尾部分隔符先摘掉


def test_the_whole_dos_device_set_is_covered():
    r"""`DEVICE_NAMES` 里每一个名字都要转。

    ⚠ 实测今天**只有 `nul` 一族真的被解析成设备**（`con`/`prn`/`aux`/`com1`…裸名与
    带扩展名的原样路径都指到真文件）。谓词仍然按全集写：`\\?\` 对每一个都实测指到
    真文件，所以写宽只是多绕一次，写窄则会在别的 Windows 版本上静默失手。
    理由与逐名字的读数在 `core/paths.py` 的模块文档里。
    """
    for name in DEVICE_NAMES:
        assert extended_path(f"D:/x/{name.lower()}") != f"D:/x/{name.lower()}"
    # 故意不在表里的两个：老规则没有 COM0 / LPT0，本机也没试过。
    assert "COM0" not in DEVICE_NAMES and "LPT0" not in DEVICE_NAMES
    assert extended_path("D:/x/com0") == "D:/x/com0"
    assert extended_path("D:/x/com10") == "D:/x/com10"          # 只有 1–9


def test_names_that_only_resemble_a_device_name_are_left_alone():
    """像但不是的，一律原样——多绕一次也要有理由，这几条就是边界。"""
    for path in ["D:/x/nulx", "D:/x/a.nul", "D:/x/xcon", "D:/x/normal.txt",
                 "D:/x/.nul", "D:/x/", "D:/", "", "nul"]:
        assert extended_path(path) == path


def test_a_device_name_in_the_middle_does_not_matter():
    r"""设备名在**中间**一层不影响（实测：`…\\nul\\inner.txt` 原样读得好好的）。

    这条是"谓词只看最后一段"的依据，也是热路径上不多解析几段的原因。
    """
    assert extended_path("D:/x/nul/inner.txt") == "D:/x/nul/inner.txt"
    assert extended_path("D:/x/con/a/b.txt") == "D:/x/con/a/b.txt"


def test_an_already_prefixed_path_is_never_prefixed_again():
    r"""`\\?\` 与 `\\.\` 都算"已经有了"——加第二层的后果是路径指向别处。"""
    for path in ["\\\\?\\D:\\x\\nul", "\\\\?\\UNC\\srv\\share\\nul", "\\\\.\\NUL",
                 "\\\\.\\C:"]:
        assert extended_path(path) == path


def test_unc_paths_use_the_unc_form():
    r"""UNC 的扩展写法是 `\\?\UNC\\server\\share\\…`，不是往前面硬拼。

    ⚠ **未实测**：本机没有可用的网络共享。这一条测的是字符串规则，不是可达性。
    """
    assert (extended_path("\\\\srv\\share\\nul")
            == "\\\\?\\UNC\\srv\\share\\nul")
    assert (extended_path("//srv/share/nul")
            == "\\\\?\\UNC\\srv\\share\\nul")


def test_shapes_it_will_not_guess_at_are_returned_unchanged():
    r"""说不清的形态**保持现状**，不猜：`\\?\` 只认绝对路径，而且会关掉归一化。

    "不转"不等于"更安全"——一条相对路径里的 `nul` 今天怎么错，不转之后还是怎么错。
    """
    for path in ["sub/nul", "C:nul", "D:/x/../nul", "D:/x/./nul",
                 "D:/x/nul/../y", "\\\\srv\\share\\..\\nul"]:
        assert extended_path(path) == path


def test_a_pathlib_path_comes_back_as_a_string():
    """传 `Path` 回来的是 `str`：调用点要把它交给 `os.*`，多一层包装没必要。"""
    got = extended_path(Path("D:/x/nul"))
    assert isinstance(got, str) and got == "\\\\?\\D:\\x\\nul"


def test_a_bytes_path_is_left_alone():
    """字节路径不属于这一层的事：不猜它的编码，原样还回去。"""
    raw = b"D:/x/nul"
    assert extended_path(raw) is raw


# ==============================================================================
# 2. 平台事实：这一族名字在系统里到底怎么了（真建文件）
# ==============================================================================


def test_the_plain_path_answers_with_the_device_instead(device_file):
    r"""**平台事实**（2026-10-15 实测，非推理）：原样路径问到的是 `\\.\\NUL` 设备。

    注意它是**静默**的：`os.stat` 不报错，`open` 也不报错，只是两个答案都不是那个
    文件的（`st_ino=0`、大小 0、读到 0 字节）。出错的形态不止一种——`attributes_of`
    用的 `GetFileAttributesExW` 在同一批路径上报的是 `winerror=87`，那一半是响的。
    """
    stat = os.stat(device_file)
    assert stat.st_ino == 0, "设备没有 FRN，`F2-02` 的核对把它算作「问不到」"
    assert stat.st_size == 0
    with open(device_file, "rb") as fh:
        assert fh.read() == b""


def test_the_extended_path_reaches_the_real_file(device_file):
    """绕行之后三样都对：有 FRN、大小是正文长度、读到的是正文。"""
    ext = extended_path(device_file)
    assert ext != device_file
    stat = os.stat(ext, follow_symlinks=False)
    assert stat.st_ino != 0
    assert stat.st_size == len(CONTENT.encode())
    with open(ext, "rb") as fh:
        assert fh.read() == CONTENT.encode()


def test_is_dir_lies_about_a_device_named_directory(device_dir):
    r"""真目录 `…\\nul`：原样 `is_dir()` 说 False，扩展写法说 True。

    这一条钉的是两个后端的根可达性检查——`_prepare_root` / `_scan_root` 都拿
    `Path.is_dir()` 当门槛，不转的话这个根会被一个**错误的理由**（"不是一个目录"）拒掉。
    顺带记下对照：同一个目录，原样 `os.scandir` 是**列得出来**的。
    """
    dir_plain, inner = device_dir
    assert Path(dir_plain).exists() is True          # 设备"存在"
    assert Path(dir_plain).is_dir() is False         # 但它不是目录 —— 谎话
    assert Path(extended_path(dir_plain)).is_dir() is True
    with os.scandir(dir_plain) as it:                # 原样下潜不受影响（实测）
        assert [e.name for e in it] == ["inner.txt"]


# ==============================================================================
# 3. 调用点：绕行真的接上了
# ==============================================================================


def test_attributes_of_answers_for_the_real_file(device_file):
    """`ntfs.attributes_of`——`F2-02` 真机核对那 2 条差异的直接来源。

    修之前这里抛 `winerror=87`，于是那两个文件在 MFT 后端的记录流里**整条消失**。
    """
    info = ntfs.attributes_of(device_file)
    assert info.size == len(CONTENT.encode())
    assert info.mtime_ns == os.stat(extended_path(device_file)).st_mtime_ns


def test_content_hash_hashes_the_real_bytes_not_the_device(device_file):
    """`fingerprint.content_hash`——静默索引成空的那一半。

    修之前它**不报错**，返回的是空输入的摘要：这个文件于是"合法地"变成一个空文件。
    两条断言必须都在：等于真正文的摘要，且**不等于**空串的摘要。
    """
    algo = fingerprint.CONTENT_HASH_ALGO
    assert (fingerprint.content_hash(device_file)
            == f"{algo}:{hashlib.new(algo, CONTENT.encode()).hexdigest()}")
    assert (fingerprint.content_hash(device_file)
            != f"{algo}:{hashlib.new(algo, b'').hexdigest()}")


def test_quick_sig_of_sees_the_real_file(device_file):
    """`fingerprint.quick_sig_of`：原样 `stat` 会成功但给设备的签名。"""
    assert (fingerprint.quick_sig_of(device_file)
            == fingerprint.quick_sig(os.stat(extended_path(device_file))))
    assert fingerprint.quick_sig_of(device_file) != fingerprint.quick_sig(
        os.stat(device_file)
    )
