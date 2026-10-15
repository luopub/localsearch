r"""`extract/plaintext.py`：认领哪些扩展名、正文怎么出来。

------------------------------------------------------------------------------
这一组盯的四件事
------------------------------------------------------------------------------

1. **认领的边界**——名单里的认领、无扩展名的认领、**名单外的绝不认领**。最后一条比
   前两条重要：`plaintext` 一旦认领了 `.html` 或 `.pdf`，那个文件会被当一坨文本读出来
   而**不报错**，症状是"HTML 里的标签成了正文"、出现在别的功能点的报表里。这里逐条
   把四类提取器**该归别人的**扩展名钉死。
2. **正文原样端出来**——不归一化换行、不清理行尾、不设 title。这几条都是"看起来更
   整齐"的改动，而代价是偏移错位与向量缓存失效（模块文档里写了理由），所以要有测试
   拦着。
3. **编码的痕迹要留下**——猜出来的编码必须在 `warnings` 里说一声。乱码是"看不出来
   哪里错了"的典型，没有这条痕迹就只能靠猜。
4. **`nul` 那族文件读到的是真正文**——`read_bytes()` 已经修了根，但这一条是**提取器
   这一层的**回归线：`extract()` 只要有人换成裸 `open()`，正文就会静默变成空字符串。

正文里的中文一律写成字面量（不是 `\uXXXX`）：这个项目的语料就是中文，测试里的中文
读起来是内容而不是转义，比什么都重要。

`tmp_path` 落在 `C:` 下——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from localsearch.core.models import ExtractedDoc
from localsearch.core.paths import extended_path
from localsearch.extract.base import (
    BinaryContent,
    ExtractConfig,
    FileUnreadable,
    ext_of,
)
from localsearch.extract.plaintext import PLAINTEXT_EXTS, PlaintextExtractor, build

DEVICE_CONTENT = "nul 是个真文件"

#: 该归别的提取器的扩展名。**这些必须一条都不认领**——理由见模块文档第 1 条。
OWNED_BY_OTHERS = {
    "html": "web_mail", "htm": "web_mail", "xhtml": "web_mail",
    "mht": "web_mail", "mhtml": "web_mail", "eml": "web_mail", "msg": "web_mail",
    "pdf": "office", "docx": "office", "xlsx": "office", "pptx": "office",
    "doc": "office", "xls": "office", "ppt": "office",
    "jpg": "media_meta", "jpeg": "media_meta", "png": "media_meta",
    "mp3": "media_meta", "mp4": "media_meta",
}


@pytest.fixture
def extractor():
    return PlaintextExtractor(ExtractConfig())


@pytest.fixture
def device_file(tmp_path):
    """真名为 `nul` 的文件（正文是中文），返回原样路径。收尾用扩展写法删。"""
    plain = str(tmp_path / "nul")
    with open(extended_path(plain), "w", encoding="utf-8") as handle:
        handle.write(DEVICE_CONTENT)
    try:
        yield plain
    finally:
        os.unlink(extended_path(plain))


def write(tmp_path, name: str, data: bytes) -> str:
    target = tmp_path / name
    target.write_bytes(data)
    return str(target)


# ==============================================================================
# 1. 认领的边界
# ==============================================================================


def test_every_name_on_the_list_is_claimed(extractor):
    """名单里的每一个都要认领——**逐条过一遍**，因为多一个少一个都不会报错。

    （写成一圈循环而不是 `parametrize`：那会把这条测试变成一百多条，让总账里的
    计数被这一个文件灌满。断言里把漏掉的名字全列出来，失败信息一样清楚。）
    """
    missing = sorted(ext for ext in PLAINTEXT_EXTS if not extractor.supports(ext, ""))
    assert missing == []


def test_the_other_three_extractors_keep_their_extensions(extractor):
    stolen = sorted(ext for ext in OWNED_BY_OTHERS if extractor.supports(ext, ""))
    assert stolen == []


def test_a_file_with_no_extension_is_claimed(extractor):
    """`README` / `Makefile` / `.gitignore`——真的没有扩展名的那一类。"""
    assert extractor.supports("", "") is True
    assert extractor.supports(ext_of("D:/x/README"), "") is True
    assert extractor.supports(ext_of("D:/x/.gitignore"), "") is True


def test_an_unknown_extension_is_not_claimed(extractor):
    """有扩展名但不在名单里的，**不去嗅内容再决定**（模块文档写了为什么）。"""
    for ext in ("bin", "dat", "db", "exe", "dll", "iso", "zzz"):
        assert extractor.supports(ext, "") is False


def test_the_mime_is_never_used(extractor):
    """Windows 上 `mimetypes` 读注册表——"本机装过什么软件"不能决定索引结果。"""
    assert extractor.supports("txt", "application/octet-stream") is True
    assert extractor.supports("zzz", "text/plain") is False


def test_build_follows_the_switch():
    assert build(ExtractConfig(text_code=True)) is not None
    assert build(ExtractConfig(text_code=False)) is None


# ==============================================================================
# 2. 正文原样端出来
# ==============================================================================


def test_a_utf8_text_file_comes_out_whole(tmp_path, extractor):
    text = "第一行\n第二行\n\n第四行\n"
    doc = extractor.extract(write(tmp_path, "a.txt", text.encode()))

    assert doc.text == text
    assert doc.source == "plaintext"
    assert doc.modality == "text"
    assert doc.title is None                 # 纯文本没有标题（模块文档第 2 条）
    assert doc.warnings == ()
    assert doc.truncated is False
    assert doc.meta["encoding"] == "utf-8"
    assert doc.meta["bytes"] == len(text.encode())
    assert doc.meta["chars"] == len(text)


def test_crlf_is_left_alone(tmp_path, extractor):
    r"""换行符**不归一化**：`\r\n` 原样留着。

    折叠成 `\n` 会让每一行的偏移往后少 1，而 `Chunk.char_start` / `char_end` 存在的
    唯一理由就是"跳到这一段的原文位置"（`core/models.py` 的约定）。这条测试是为了
    拦住那个"顺手清理一下"的改动。
    """
    doc = extractor.extract(write(tmp_path, "a.txt", b"one\r\ntwo\r\n"))

    assert doc.text == "one\r\ntwo\r\n"
    assert "\r\n" in doc.text


def test_trailing_spaces_and_blank_lines_are_left_alone(tmp_path, extractor):
    """不清理行尾空格、不折叠空行：切片一变，`A9` 的向量缓存全废。"""
    text = "a   \n\n\n\nb\t\n"
    assert extractor.extract(write(tmp_path, "a.txt", text.encode())).text == text


def test_a_bom_is_eaten_but_not_dropped_from_the_encoding_record(tmp_path, extractor):
    doc = extractor.extract(write(tmp_path, "a.txt", "中文".encode("utf-8-sig")))

    assert doc.text == "中文"                # BOM 不在正文里
    assert doc.meta["encoding"] == "utf-8-sig"
    assert doc.warnings == ()


def test_an_empty_file_is_a_valid_empty_doc(tmp_path, extractor):
    """0 字节文件是**成功**，不是失败——`A6` 说的失败是"解析失败"，不是"内容为空"。"""
    doc = extractor.extract(write(tmp_path, "empty.txt", b""))

    assert doc.text == ""
    assert doc.is_empty is True
    assert doc.warnings == ()


def test_a_lone_newline_is_not_empty(tmp_path, extractor):
    """`is_empty` 判的是"全空白"，与"真的 0 字节"是两件事，这里区分开。"""
    doc = extractor.extract(write(tmp_path, "nl.txt", b"\n"))

    assert doc.text == "\n"
    assert doc.is_empty is True


# ==============================================================================
# 3. 编码的痕迹要留下
# ==============================================================================


def test_a_guessed_encoding_leaves_a_warning(tmp_path, extractor):
    """GBK 中文能解对，但**要说出来这是猜的**——乱码时第一个要查的就是它。"""
    text = "中文测试"
    doc = extractor.extract(write(tmp_path, "a.txt", text.encode("gbk")))

    assert doc.text == text
    assert doc.meta["encoding"] == "gb18030"
    assert len(doc.warnings) == 1
    assert "猜" in doc.warnings[0]


def test_a_file_with_no_extension_is_sniffed_once(tmp_path, extractor):
    """`README` 这类无扩展名的文件走同一套读法，正文照旧。"""
    doc = extractor.extract(write(tmp_path, "README", "部署说明\n".encode()))

    assert doc.text == "部署说明\n"
    assert doc.meta["encoding"] == "utf-8"


# ==============================================================================
# 4. 三类失败：二进制、超限、设备名
# ==============================================================================


def test_a_binary_extensionless_file_is_refused(tmp_path, extractor):
    """无扩展名的兜底必须在嗅出 NUL 时**报失败**，不是"悄悄返回空正文"。

    后者与"这是个 0 字节文件"完全分不开——而它们一个是错误、一个是正常结果。
    """
    path = write(tmp_path, "blob", b"MZ\x90\x00\x03\x00\x00\x00data")

    with pytest.raises(BinaryContent) as excinfo:
        extractor.extract(path)

    assert "NUL" in str(excinfo.value)


def test_a_binary_file_with_a_listed_extension_is_also_refused(tmp_path, extractor):
    """扩展名是 `.txt` 但内容是二进制的：一样拒绝——名单是"尝试"，不是"保证"。"""
    path = write(tmp_path, "fake.txt", b"\x00\x01\x02binary")

    with pytest.raises(BinaryContent):
        extractor.extract(path)


def test_over_the_limit_refuses(tmp_path):
    path = write(tmp_path, "big.txt", b"a" * 5000)
    extractor = PlaintextExtractor(ExtractConfig(max_bytes=1000))

    with pytest.raises(FileUnreadable):
        extractor.extract(path)


def test_a_device_named_file_yields_its_real_text(device_file, extractor):
    """**`E27` 在提取器这一层的回归线。**

    `read_bytes()` 里那行 `extended_path()` 一旦被去掉，这里读到的就是 0 字节，
    而正文会**静默变成空字符串**——doc 照常返回、没有任何警告，只有这一条测试会红。
    """
    doc = extractor.extract(device_file)

    assert doc.text == DEVICE_CONTENT
    assert doc.text != ""                    # 原样 open() 会读到 0 字节，这里必须是空的**反**


def test_a_device_named_file_and_a_normal_one_agree(tmp_path, extractor):
    """同一份正文，一次放在普通文件里、一次放在 `nul` 里，结果必须一模一样。"""
    text = "同一份正文\n第二行\n"
    normal = write(tmp_path, "normal.txt", text.encode())
    device = str(tmp_path / "sub" / "nul")
    os.makedirs(str(tmp_path / "sub"), exist_ok=True)
    # `newline=""`：文本模式默认会把 `\n` 翻成 `\r\n`，那样两个文件的内容就不同了，
    # 这条测试比的正是"内容相同、结果必须相同"。
    with open(extended_path(device), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    try:
        assert extractor.extract(device).text == extractor.extract(normal).text == text
    finally:
        os.unlink(extended_path(device))


# ==============================================================================
# 5. 与 `core/models.py` 的形状对接
# ==============================================================================


def test_the_result_is_a_frozen_extracted_doc(tmp_path, extractor):
    doc = extractor.extract(write(tmp_path, "a.txt", b"x"))

    assert isinstance(doc, ExtractedDoc)
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.text = "别的"                      # frozen=True：谁都不许原地改它
