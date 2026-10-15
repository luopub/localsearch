r"""`extract/base.py`：派发、读字节、判编码。

------------------------------------------------------------------------------
这一组为什么这样切
------------------------------------------------------------------------------

`base.py` 里三件事的**证明方式完全不同**，所以测试也分三段：

1. **扩展名 / mime / 是不是二进制**——纯字符串与纯字节，不碰盘，快，断言写死。
2. **编码判定**——这里没有"正确"可言，只有**选了的那个赌注**。所以测试不去断言
   `charset_normalizer` 的内部结论（那是它的实现细节，它换个版本就会红），而是断言
   **还原得回来**（`text == 原文`）和**标志位对不对**。另有一条专门钉住那个短文本
   赌注，见 `test_short_gbk_is_decoded_as_gb18030_not_korean`。
3. **读字节**——要真文件。其中一条**必须**是真设备名文件，见下。

⚠ **`nul` 那一条不是凑数的**：`read_bytes()` 里那行 `extended_path()` 是整个提取层
唯一一处防"静默读成 0 字节"的地方（`E27`），而它被删掉之后**其余所有测试照样绿**——
因为只有真名为 `nul` 的文件会中招。造出来的文件**必须用扩展写法删**（原样 `unlink`
报 winerror 5，pytest 清 `tmp_path` 用的 `rmtree` 会因此失败，症状是测试通过、
收尾时刷一屏清理错误）。这一套照抄 `test_paths.py`。

`tmp_path` 落在 `C:` 下的 pytest 临时目录里——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import codecs
import os

import pytest

from localsearch.core.models import ExtractedDoc
from localsearch.core.paths import extended_path
from localsearch.core.registry import Registry
from localsearch.extract import base
from localsearch.extract.base import (
    SHORT_BYTES,
    ExtractConfig,
    Extractor,
    FileUnreadable,
    UnsupportedFormat,
    decode_bytes,
    ext_of,
    extract_file,
    find_extractor,
    looks_binary,
    meta_str,
    mime_of,
    read_bytes,
    read_text,
    with_warning,
)

#: 设备名文件里装的正文。ASCII 写，好让"字节数 == 字符数"不需要解释。
DEVICE_CONTENT = "nul is a real file"


@pytest.fixture
def config():
    return ExtractConfig()


@pytest.fixture
def device_file(tmp_path):
    """真名为 `nul` 的文件，返回**原样路径**（字符串）。收尾自己删（见模块文档）。"""
    plain = str(tmp_path / "nul")
    with open(extended_path(plain), "w", encoding="utf-8") as handle:
        handle.write(DEVICE_CONTENT)
    try:
        yield plain
    finally:
        os.unlink(extended_path(plain))


# ==============================================================================
# 1. 扩展名与 mime：纯字符串
# ==============================================================================


def test_ext_of_lowercases_and_drops_the_dot():
    assert ext_of("D:/x/Report.TXT") == "txt"
    assert ext_of("D:/x/a.Md") == "md"


def test_ext_of_takes_only_the_last_suffix():
    """`a.tar.gz` 归 `gz`——解归档不是 v1 的事，"这文件是不是压缩包"由规则判。"""
    assert ext_of("D:/x/a.tar.gz") == "gz"
    assert ext_of("D:/x/a.b.c") == "c"


def test_dotfiles_count_as_having_no_extension():
    """`.gitignore` 是一个**纯文本文件**，不是"名为空、扩展名 gitignore 的文件"。

    这条决定 `plaintext` 的无扩展名那一支能不能接住它们（`README` / `Makefile` 同理）。
    """
    assert ext_of("D:/x/.gitignore") == ""
    assert ext_of("D:/x/Makefile") == ""
    assert ext_of("D:/x/README") == ""
    assert ext_of("D:/x/.env.local") == "local"      # 这个不是点开头，而是"名字里有点"


def test_ext_of_on_paths_with_no_file_part():
    assert ext_of("D:/x/y") == ""
    assert ext_of("D:/x/a.") == ""                    # 尾部孤零零一个点，没有扩展名


def test_mime_of_returns_empty_when_it_has_no_guess():
    """猜不出就给空串，**不给 `application/octet-stream` 之类的默认值**。

    默认值会让"猜不出"与"猜出是二进制"看起来一样，而调用方需要区分这两件事。
    """
    assert mime_of("D:/x/a.md") == "text/markdown"
    assert mime_of("D:/x/a.zzzz") == ""
    assert mime_of("D:/x/README") == ""


# ==============================================================================
# 2. 二进制嗅探：判据只有"NUL 字节"
# ==============================================================================


def test_looks_binary_finds_a_nul_byte():
    assert looks_binary(b"MZ\x90\x00\x03") is True
    assert looks_binary(b"hello\x00world") is True


def test_plain_text_is_not_binary_in_any_common_encoding():
    """判据宁窄勿宽：误判的代价是"用户的正常文件被拒绝提取"，比漏判贵得多。"""
    for text, encoding in (
        ("纯中文文本", "utf-8"),
        ("純中文文本", "big5"),
        ("中文文本", "gb18030"),
        ("café résumé", "cp1252"),
        ("Привет мир", "cp1251"),
        ("これは日本語", "shift_jis"),
    ):
        assert looks_binary(text.encode(encoding)) is False


def test_the_sniff_only_looks_at_the_head():
    """只嗅前 `SNIFF_BYTES` 字节：全盘千兆文件不能为了判这个把整个文件读一遍。

    代价是**尾部有 NUL 的文本会被放过**——那正是判据宁窄勿宽的那一面。
    """
    filler = b"a" * base.SNIFF_BYTES
    assert looks_binary(filler + b"\x00") is False
    assert looks_binary(b"\x00" + filler) is True


def test_a_bom_only_utf16_file_is_the_known_miss():
    """无 BOM 的 UTF-16 会被判成二进制——**这是记下来的已知行为，不是意外**。

    ASCII 字符在 UTF-16 里第二字节就是 `0x00`。放宽判据去接住它，代价是正常的 GBK
    文件也可能被按 UTF-16 解（那才是真会出事的写法）。哪天要改，改的是这条测试，
    会有人先读到这些话。
    """
    assert looks_binary("hello".encode("utf-16-le")) is True


# ==============================================================================
# 3. 编码：断言"还原得回来"与标志位，不断言探测器的内部结论
# ==============================================================================


def test_utf8_needs_no_explanation():
    got = decode_bytes("中文与 ASCII 混排 test 123".encode())

    assert got.text == "中文与 ASCII 混排 test 123"
    assert (got.guessed, got.lossy) == (False, False)
    assert got.warning is None


def test_ascii_is_utf8():
    got = decode_bytes(b"hello world")
    assert (got.text, got.encoding) == ("hello world", "utf-8")
    assert got.warning is None


def test_an_empty_file_is_not_a_guess():
    """0 字节是合法输入，不该被记成"猜出来的编码"（那会凭空多一堆警告）。"""
    got = decode_bytes(b"")
    assert (got.text, got.encoding) == ("", "utf-8")
    assert (got.guessed, got.lossy) == (False, False)


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
    ],
)
def test_a_bom_settles_it_without_guessing(encoding, bom):
    got = decode_bytes(bom + "中文与 ASCII".encode(encoding))

    assert got.text == "中文与 ASCII"          # BOM 已经被 codec 吃掉了
    assert got.guessed is False
    assert got.warning is None


def test_utf32_is_not_read_as_utf16():
    """BOM 表里 UTF-32 必须排在 UTF-16 **前面**。

    `FF FE 00 00`（UTF-32 LE 的 BOM）以 `FF FE`（UTF-16 LE 的 BOM）开头，顺序反了
    就会按 UTF-16 解出满屏 NUL——而它**不报错**，只是正文全是空白。这条测试盯的就是
    那个顺序。
    """
    got = decode_bytes("中文测试".encode("utf-32"))

    assert got.text == "中文测试"
    assert got.encoding == "utf-32"
    assert got.warning is None


@pytest.mark.parametrize(
    "text",
    [
        "你好",                                  # 4 字节：两种走法都猜错的那一档
        "中文测试",                               # 8 字节：自由模式猜成 cp949
        "这是一份较短的便签",                        # 20 字节
        "姓名：张三\n城市：北京\n",                   # 带换行的一份便签
        "本项目的目标是本地离线全盘全文搜索。" * 20,     # 长数据（超过 SHORT_BYTES）
    ],
)
def test_gbk_chinese_survives_whole(text):
    r"""GBK 中文必须还原得回来，**长短都要**——这是本机语料的主力（计划 §7.4）。

    实测（2026-10-15，59 个长度档）：自由模式只对 46 档，`SHORT_BYTES` 以内直接认
    `gb18030` 对 59 档。这条测试覆盖三个不同的档位：4 字节（连收窄候选表都猜错）、
    8 字节（自由模式猜成 `cp949` 韩文）、长数据（走探测器那一档）。
    """
    got = decode_bytes(text.encode("gbk"))

    assert got.text == text
    assert got.encoding == "gb18030"
    assert got.guessed is True                 # 还是"猜的"：BOM 没有、严格 UTF-8 也不成立
    assert got.lossy is False


def test_gb18030_is_only_the_short_data_rule():
    """超过 `SHORT_BYTES` 时**一个字都不干预**——这条把"只在短数据上"钉死。

    如果哪天有人把这一档改成无条件，长韩文/长俄文就会开始变乱码；这里用一条长数据的
    断言提醒：`gb18030` 不是全程的首选，它只是短数据上的语料先验。
    """
    assert SHORT_BYTES < 64
    got = decode_bytes(b"a" * (SHORT_BYTES + 1))       # 超过线，走严格 UTF-8 那一档
    assert got.text == "a" * (SHORT_BYTES + 1)


def test_short_korean_is_a_known_casualty_of_the_chinese_prior():
    """短韩文会被解成中文乱码——**这是记下来的代价，不是意外**。

    自由模式本来判得对（`cp949`），短数据那一档按语料先验认了 `gb18030`。取舍的依据
    是语料分布（本机是中文 Windows），翻盘条件是"盘上真出现韩文语料"。这条测试的
    用处是：将来有人发现韩文乱码时，能在这里读到"这是知道的"。
    """
    text = "한국어 테스트"                        # 13 字节，小于 SHORT_BYTES
    raw = text.encode("euc_kr")
    got = decode_bytes(raw)

    assert len(raw) <= SHORT_BYTES
    assert got.encoding == "gb18030"
    assert got.text != text


def test_long_korean_is_untouched():
    """长韩文一个字都不干预：探测器判得对，而它正是干预会毁掉的那一类。"""
    text = "이것은 한국어 테스트 파일입니다. " * 6        # 198 字节，远大于 SHORT_BYTES
    got = decode_bytes(text.encode("euc_kr"))

    assert len(text.encode("euc_kr")) > SHORT_BYTES
    assert got.text == text


def test_a_bom_does_not_promise_clean_content():
    """BOM 是编码的硬证据，但**不保证内容没被截断**——有损要说出来。"""
    got = decode_bytes(codecs.BOM_UTF8 + b"AB\xff\xfeCD")

    assert got.encoding == "utf-8-sig"
    assert got.lossy is True
    assert got.warning is not None
    assert "替换" in got.warning


def test_every_decoded_text_can_be_encoded_back():
    """不管走了哪一档，`encoding` 必须是一个真 codec 名——否则它就只是句注释。"""
    for raw in (b"hello", "中文".encode("gbk"), codecs.BOM_UTF16_LE + "hi".encode("utf-16-le")):
        got = decode_bytes(raw)
        got.text.encode(got.encoding)          # 解不回来就会在这里抛异常


# ==============================================================================
# 4. 读字节：要真文件
# ==============================================================================


def test_read_bytes_gives_the_content(tmp_path, config):
    target = tmp_path / "a.txt"
    target.write_bytes("你好".encode())
    assert read_bytes(str(target), config=config) == "你好".encode()


def test_read_text_decodes_in_one_step(tmp_path, config):
    target = tmp_path / "a.txt"
    target.write_bytes("你好".encode("gbk"))
    assert read_text(str(target), config=config).text == "你好"


def test_over_the_limit_is_a_failure_not_a_truncation(tmp_path):
    """静默截断会让"为什么只搜到前半段"变成一个没有痕迹的问题。"""
    target = tmp_path / "big.txt"
    target.write_bytes(b"a" * 101)
    config = ExtractConfig(max_bytes=100)

    with pytest.raises(FileUnreadable) as excinfo:
        read_bytes(str(target), config=config)

    assert "100" in str(excinfo.value)


def test_exactly_the_limit_is_allowed(tmp_path):
    target = tmp_path / "edge.txt"
    target.write_bytes(b"a" * 100)
    assert len(read_bytes(str(target), config=ExtractConfig(max_bytes=100))) == 100


def test_a_missing_file_becomes_an_extract_error(tmp_path, config):
    with pytest.raises(FileUnreadable):
        read_bytes(str(tmp_path / "nope.txt"), config=config)


def test_a_directory_is_a_failure_not_silence(tmp_path, config):
    with pytest.raises(FileUnreadable):
        read_bytes(str(tmp_path), config=config)


def test_a_device_named_file_reads_its_real_content(device_file, config):
    """**`E27` 的回归线**：原样 `open()` 会"成功"读到 **0 字节**，不报错。

    这条测试存在的唯一理由是盯着 `read_bytes()` 里那行 `extended_path()`——把它删掉
    之后，只有这一条会红。
    """
    got = read_bytes(device_file, config=config)

    assert got == DEVICE_CONTENT.encode()
    assert decode_bytes(got).text == DEVICE_CONTENT


# ==============================================================================
# 5. 派发与两个小工具
# ==============================================================================


class _Fake:
    """一个什么都认领的替身；`extract` 的返回内容写死，好断言"确实派给了它"。"""

    name = "fake"

    def __init__(self, claimed: tuple[str, ...] = ("txt",)) -> None:
        self._claimed = claimed
        self.calls: list[str] = []

    def supports(self, ext: str, mime: str) -> bool:
        del mime
        return ext in self._claimed

    def extract(self, path: str) -> ExtractedDoc:
        self.calls.append(path)
        return ExtractedDoc(text="来自替身", source=self.name)


def test_find_extractor_picks_the_first_claiming_one():
    first, second = _Fake(("txt",)), _Fake(("txt",))
    second.name = "second"
    registry: Registry[Extractor] = Registry("extractor")
    registry.register(first.name, first)
    registry.register(second.name, second)

    assert find_extractor("D:/x/a.txt", registry=registry) is first


def test_find_extractor_returns_none_when_nobody_claims_it():
    registry: Registry[Extractor] = Registry("extractor")
    registry.register("fake", _Fake(("txt",)))

    assert find_extractor("D:/x/a.bin", registry=registry) is None


def test_extract_file_hands_the_path_to_the_extractor(tmp_path):
    fake = _Fake(("txt",))
    registry: Registry[Extractor] = Registry("extractor")
    registry.register(fake.name, fake)
    path = str(tmp_path / "a.txt")

    doc = extract_file(path, registry=registry, config=ExtractConfig())

    assert doc.text == "来自替身"
    assert fake.calls == [path]


def test_extract_file_names_the_extension_when_nobody_claims_it():
    """报错信息里要有扩展名与路径——`A6` 要它进 `files.error_msg`，那是给人看的。"""
    registry: Registry[Extractor] = Registry("extractor")

    with pytest.raises(UnsupportedFormat) as excinfo:
        extract_file("D:/x/a.zzz", registry=registry, config=ExtractConfig())

    assert ".zzz" in str(excinfo.value)
    assert "a.zzz" in str(excinfo.value)


def test_extract_file_says_something_for_an_extensionless_file_too():
    registry: Registry[Extractor] = Registry("extractor")

    with pytest.raises(UnsupportedFormat) as excinfo:
        extract_file("D:/x/README", registry=registry, config=ExtractConfig())

    assert "无扩展名" in str(excinfo.value)


def test_with_warning_appends_and_keeps_the_object_frozen():
    doc = ExtractedDoc(text="x")

    once = with_warning(doc, "第一条")
    twice = with_warning(once, "第二条")

    assert doc.warnings == ()               # 原对象没被改动
    assert twice.warnings == ("第一条", "第二条")


def test_with_warning_none_changes_nothing():
    doc = ExtractedDoc(text="x")
    assert with_warning(doc, None) is doc


def test_meta_str_flattens_and_truncates():
    assert meta_str(None) == ""
    assert meta_str(42) == "42"
    assert meta_str("  x  ") == "x"
    assert len(meta_str("y" * 5000)) == 200
