r"""`extract/media_meta.py`：图片的元数据字段、跳过规则、失败与两条实测钉子（`F3-04`）。

------------------------------------------------------------------------------
这一组盯的七件事
------------------------------------------------------------------------------

1. **认领的边界**——五族 12 个扩展名认领，其余的**一个不认**；`supports()` 不看 MIME。
   故意不认的那两类（`heic` / 相机 RAW；没有 EXIF 概念的 `gif` / `bmp` / `psd` 一族）
   各有一条测试：它们必须落 `UnsupportedFormat`（`P4` 据此留 `NAME_ONLY`，**不进失败
   报表**）。这里认领错一个，全盘上那些文件就会变成一堆 `ParseFailed`。
2. **接进注册表之后**——`.jpg` / `.png` 归 `media_meta`（**不能**被 `plaintext` 抢走），
   顺序在最后，关掉开关就落 `UnsupportedFormat`。
3. **字段怎么挑**——`bytes` / 序列跳过、指针跳过、TIFF 的结构性字段跳过、数字怎么写
   （`28/10 -> 2.8`）、以及"格式与尺寸**不进正文**只进 `meta`"。这一组是"搜得到什么"
   的正面清单，缺一条就会有一个静默的召回缺口。
4. **`XP*` 与 `UserComment` 要从字节里解出来**——这是中文照片唯一可能带着自己那句
   中文的地方（`Make` / `Model` 是英文的）。解不出来时必须留警告，不许静默丢。
5. **GPS 合成十进制度**——方向并进值里、南纬西经取负、海拔正负看 `GPSAltitudeRef`；
   值不合法（不是三段分数）时**不猜**，那个字段就不出现。
6. **上限与截断**——`_text._Text` 的整行语义：到顶就**整行地停**，一个超长的值
   **整行**被丢掉而不是切一半，两种情况都要 `truncated` 与警告。
7. **失败一律是 `ParseFailed` / `FileUnreadable`**——改名、截断、解压炸弹各一条
   （`A6`：单文件失败要被隔离，不许把整轮索引带下去），而**坏 EXIF 段不算失败**：
   坏元数据不是坏图片。

------------------------------------------------------------------------------
另有两条测试是"钉住模块文档里的话"，不是钉行为
------------------------------------------------------------------------------

`test_a_jpeg_is_never_decoded` 与 `test_png_does_go_through_load_and_that_is_pillows_choice`
都靠把 `Image.Image.load` 换成抛异常的桩：前者证明 JPEG / TIFF / WebP 的抽取一行像素
都不解，后者证明 **PNG 会解**（Pillow 的 `getexif()` 与 `text` 里都调了 `load()`）。
第二条看起来像在钉一个缺陷，其实是钉**文档的诚实**：模块文档第一段写了这件事，
以后谁要改成"PNG 也走 `info["exif"]` 那条不解码的路"，就该同时改这两条测试与那段文档。

⚠ **这里没有 `nul` 那族文件的测试，这是量过的结论**（`2026-10-16`）：`plaintext` 那一组
的 `nul` 回归线在 media_meta 上**接不上**——不带扩展名的 `nul` 归 `plaintext`，而带扩展名的
`nul.jpg` 在本机（Windows 11 build 26300）原样 `open()` 读得到真字节（21 字节，`nul.txt` /
`NUL.JPG` / `com1.jpg` / `con.png` 四个名字各一次），所以照抄过来会得到一条**永远不会红**
的测试。这一层的读盘入口由另外两条盯着：不存在的路径与超过 `max_bytes` 的文件都必须
是 `FileUnreadable`（自己去 `Image.open(path)` 的话，前者是 `FileNotFoundError`）。

样本是**在测试里用 Pillow 现造**的（与 `office` / `web_mail` 那两组同一条已知边界：
不跨库版本）。落盘就不再变的独立样本是 `F3-06` 的 fixtures 要补的。

`tmp_path` 落在 `C:` 下——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import ExifTags, Image, PngImagePlugin
from PIL.ExifTags import GPSTAGS, IFD, TAGS
from PIL.TiffImagePlugin import IFDRational

from localsearch.extract import build_registry
from localsearch.extract.base import (
    ExtractConfig,
    FileUnreadable,
    ParseFailed,
    UnsupportedFormat,
    extract_file,
)
from localsearch.extract.media_meta import (
    IMAGE_EXTS,
    MAX_CHARS,
    MediaMetaExtractor,
    build,
)

#: 名字 → tag 号（写样本时用名字，读起来才知道自己在造什么）
_BY_NAME = {name: tag for tag, name in TAGS.items()}
_GPS_BY_NAME = {name: tag for tag, name in GPSTAGS.items()}

#: 扩展名 → Pillow 那边的格式名（`save()` 要的是后者，且不是简单的大写）
PIL_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "tif": "TIFF", "webp": "WEBP"}

#: 该归**别的**提取器的扩展名，以及媒体里**故意不认领**的那些。一条都不许认领。
NOT_OURS = [
    "txt", "md", "log", "json", "py", "html", "htm", "mht", "eml", "msg",
    "pdf", "docx", "xlsx", "pptx", "epub", "zip", "db", "exe", "bin",
    "heic", "heif", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2",
    "gif", "bmp", "tga", "ico", "cur", "psd", "psb", "jp2", "j2k", "svg",
    "mp3", "mp4", "wav", "mov",
]


@pytest.fixture
def extractor():
    return MediaMetaExtractor(ExtractConfig())


def _fracs(*pairs: tuple[int, int]) -> tuple[IFDRational, ...]:
    """把 `(分子, 分母)` 写成分数——EXIF 里的分数在小端序字节里就是这种形态。"""
    return tuple(IFDRational(a, b) for a, b in pairs)


def _exif_blob(*, ifd0=None, sub=None, gps=None, size=(40, 30)) -> bytes:
    """造一段 EXIF。键可以是名字也可以是 tag 号；`gps` 里用 GPS 的名字。"""
    image = Image.new("RGB", size)
    exif = image.getexif()
    for key, value in (ifd0 or {}).items():
        exif[key if isinstance(key, int) else _BY_NAME[key]] = value
    if sub:
        node = exif.get_ifd(IFD.Exif)
        for key, value in sub.items():
            node[key if isinstance(key, int) else _BY_NAME[key]] = value
    if gps:
        node = exif.get_ifd(IFD.GPSInfo)
        for key, value in gps.items():
            node[key if isinstance(key, int) else _GPS_BY_NAME[key]] = value
    return exif.tobytes()


def _write(tmp_path, name: str, data: bytes) -> str:
    target = os.path.join(str(tmp_path), name)
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def jpeg(tmp_path, name: str = "shot.jpg", *, exif=None, comment=None, size=(40, 30)) -> str:
    image = Image.new("RGB", size, (9, 9, 9))
    buffer = io.BytesIO()
    extra = {}
    if exif is not None:
        extra["exif"] = exif
    if comment is not None:
        extra["comment"] = comment
    image.save(buffer, "JPEG", **extra)
    return _write(tmp_path, name, buffer.getvalue())


def png(tmp_path, name: str = "shot.png", *, text=None, exif=None, size=(8, 8)) -> str:
    image = Image.new("RGB", size, (3, 4, 5))
    buffer = io.BytesIO()
    extra = {}
    if text:
        info = PngImagePlugin.PngInfo()
        for key, value in text.items():
            info.add_itxt(key, value)
        extra["pnginfo"] = info
    if exif is not None:
        extra["exif"] = exif
    image.save(buffer, "PNG", **extra)
    return _write(tmp_path, name, buffer.getvalue())


def tiff(tmp_path, name: str = "shot.tif", *, exif=None, size=(40, 30)) -> str:
    image = Image.new("RGB", size, (7, 7, 7))
    buffer = io.BytesIO()
    extra = {"exif": exif} if exif is not None else {}
    image.save(buffer, "TIFF", **extra)
    return _write(tmp_path, name, buffer.getvalue())


def lines_of(doc) -> list[str]:
    return doc.text.splitlines()


# ==============================================================================
# 一、认领的边界
# ==============================================================================


@pytest.mark.parametrize("ext", sorted(IMAGE_EXTS))
def test_every_claimed_extension_is_claimed(ext, extractor):
    assert extractor.supports(ext, "") is True


@pytest.mark.parametrize("ext", NOT_OURS)
def test_nothing_outside_the_list_is_claimed(ext, extractor):
    """认领错一个的代价是不对称的：多认一个 = 那类文件全变成 `ParseFailed` 噪音。"""
    assert extractor.supports(ext, "") is False


def test_a_file_without_an_extension_is_not_ours(extractor):
    """无扩展名那一支归 `plaintext`（带二进制嗅探）；两边都想要的话谁在前面决定结果。"""
    assert extractor.supports("", "") is False


def test_mime_is_never_consulted(extractor):
    """`base.mime_of()` 在 Windows 上会读注册表，所以它一个字符都不许参与判定。"""
    assert extractor.supports("jpg", "text/plain") is True
    assert extractor.supports("txt", "image/jpeg") is False


def test_the_unclaimed_image_formats_fall_to_unsupported(tmp_path):
    """不认领的下场写清楚：`UnsupportedFormat`（不是 `ParseFailed`），`P4` 留 `NAME_ONLY`。

    这条与 `.lsearchignore` 的默认规则方向一致——那份规则今天把 `*.heic` 标成只记名字。
    """
    registry = build_registry(ExtractConfig())
    path = _write(tmp_path, "phone.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32)
    with pytest.raises(UnsupportedFormat):
        extract_file(path, registry=registry, config=ExtractConfig())


# ==============================================================================
# 二、接进注册表之后
# ==============================================================================


def test_registry_order_is_the_documented_one():
    """顺序即优先级：新来的提取器要插进这条元组，而不是改写别人。"""
    assert build_registry(ExtractConfig()).names() == (
        "plaintext", "office", "web_mail", "media_meta",
    )


@pytest.mark.parametrize("name", ["a.jpg", "a.jpeg", "a.png", "a.tif", "a.webp"])
def test_the_registry_dispatches_images_to_media_meta(tmp_path, name):
    """⚠ 这一条是"`.png` 没被 `plaintext` 抢走"的回归线（`plaintext` 那一组也钉了）。"""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, PIL_FORMATS[name.rsplit(".", 1)[1]])
    doc = extract_file(_write(tmp_path, name, buffer.getvalue()),
                       registry=build_registry(ExtractConfig()), config=ExtractConfig())
    assert doc.source == "media_meta"


def test_switching_it_off_means_unsupported_not_empty(tmp_path):
    """关掉开关 = **不注册**（那一类文件明确落 `UnsupportedFormat`），不是"注册了但返回空"。"""
    config = ExtractConfig(media_meta=False)
    assert build(config) is None
    registry = build_registry(config)
    path = jpeg(tmp_path)
    with pytest.raises(UnsupportedFormat):
        extract_file(path, registry=registry, config=config)


# ==============================================================================
# 三、字段怎么出来
# ==============================================================================


def test_exif_fields_come_out_one_per_line(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={
        "Make": "TestMake", "Model": "TestCam 12", "Software": "Photoshop 24",
    }))
    assert lines_of(extractor.extract(path)) == [
        "Make: TestMake",
        "Model: TestCam 12",
        "Software: Photoshop 24",
    ]


def test_fields_are_ordered_by_tag_number(extractor, tmp_path):
    """正文逐字稳定是硬要求（切片偏移与 `A9` 的向量缓存都靠它），所以组内按 tag 号排。"""
    # Software(0x0131) 写在 Model(0x0110) 前面，出来时必须换回去
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={"Software": "S", "Model": "M", "Make": "K"}))
    assert lines_of(extractor.extract(path)) == ["Make: K", "Model: M", "Software: S"]


def test_the_three_ifds_come_out_in_order(extractor, tmp_path):
    """IFD0 → ExifIFD → GPS：后两组的字段永远排在基础字段后面。"""
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"Model": "M"},
        sub={"DateTimeOriginal": "2021:07:04 12:00:01"},
        gps={"GPSLatitudeRef": "N", "GPSLatitude": _fracs((31, 1), (13, 1), (4960, 100))},
    ))
    names = [line.split(":", 1)[0] for line in lines_of(extractor.extract(path))]
    assert names == ["Model", "DateTimeOriginal", "GPSLatitude"]


def test_fractions_become_readable_decimals(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(sub={
        "FNumber": _fracs((28, 10)),
        "ExposureTime": _fracs((1, 250)),
        "FocalLength": _fracs((425, 100)),
        "ISOSpeedRatings": 100,
    }))
    text = extractor.extract(path).text
    assert "FNumber: 2.8" in text
    assert "ExposureTime: 0.004" in text
    assert "FocalLength: 4.25" in text
    assert "ISOSpeedRatings: 100" in text
    assert "2.7999999999999998" not in text


def test_the_lossy_rendering_of_exposure_time_is_pinned(extractor, tmp_path):
    """`1/250` 变成 `0.004` 是**有意的取舍**（模块文档写了理由）：搜 `1/250` 搜不到。

    钉住它是为了让它以后**看起来像 bug** 的时候有人能查到"这是选过的"。
    """
    path = jpeg(tmp_path, exif=_exif_blob(sub={"ExposureTime": _fracs((1, 250))}))
    text = extractor.extract(path).text
    assert "ExposureTime: 0.004" in text
    assert "1/250" not in text


def test_long_decimals_are_rounded_to_six_places(extractor, tmp_path):
    r"""`1/3` 写成 `0.333333`，**不是** `0.3333333333333333`。

    ⚠ 这条是补上来的：`_number()` 里那句 `f"{value:.6f}".rstrip("0").rstrip(".")`
    差点没有一个测试盯着——上面那三个样本（`28/10`、`1/250`、`425/100`）里，
    Pillow 的 `IFDRational.__str__` 给出的字面量**本来就和我们算的一样**，所以把
    整句换成 `str(value)` 之后 112 条一条都不红（`2026-10-16` 用变体实测过）。
    `1/3` 是能分出两边的值：`IFDRational(1, 3)` 的浮点展开有 16 位。
    （EXIF 里 `1/3` 是常见曝光时间；APEX 那一族 `ShutterSpeedValue` / `ApertureValue`
    更是动不动就长这样，所以这不是一个为测试编出来的值。）
    """
    path = jpeg(tmp_path, exif=_exif_blob(sub={
        "ExposureTime": _fracs((1, 3)), "ShutterSpeedValue": _fracs((1234, 7)),
    }))
    text = extractor.extract(path).text
    assert "ExposureTime: 0.333333" in text
    assert "ShutterSpeedValue: 176.285714" in text
    assert "0.3333333333333333" not in text
    assert "176.28571428571428" not in text


def test_no_nul_bytes_reach_the_text(extractor, tmp_path):
    """`bytes` 一族要挡住：`MakerNote` 那种二进制块里的 NUL 进了正文，FTS 会收到垃圾。"""
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"XPTitle": "标题".encode("utf-16-le") + b"\x00\x00"},
        sub={"MakerNote": b"\x00\x01MAKER\xff" * 40},
    ))
    text = extractor.extract(path).text
    assert "\x00" not in text
    assert "MakerNote" not in text
    assert "XPTitle: 标题" in text


def test_meta_carries_what_is_kept_out_of_the_text(extractor, tmp_path):
    """格式与尺寸**只进 `meta`**：正文里出现 "JPEG" 等于给全盘每张图塞一个高频词。"""
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={"Model": "M"}), size=(40, 30))
    doc = extractor.extract(path)
    assert doc.meta["kind"] == "image"
    assert doc.meta["format"] == "JPEG"
    assert doc.meta["width"] == 40 and doc.meta["height"] == 30
    assert doc.meta["mode"] == "RGB"
    assert doc.meta["frames"] == 1
    assert doc.meta["bytes"] == os.path.getsize(path)
    assert doc.meta["exif_fields"] == 1 and doc.meta["text_fields"] == 0
    assert "JPEG" not in doc.text and "40" not in doc.text


def test_title_stays_none_even_when_the_image_describes_itself(extractor, tmp_path):
    r"""图片没有"文档标题"这个概念；`ImageDescription` 进正文，不当 `title`（模块文档）。

    ⚠ 这里的描述只能是 ASCII：`ExifTags` 那一族**字符串**字段经 Pillow 的 `tobytes()`
    落盘时非 ASCII 会被**换成 `?`**（`2026-10-16` 实测：`exif[desc]` 在内存里还是中文，
    出来的 blob 里已经没有任何 UTF-8 字节、只剩 `?`，JPEG 与 TIFF 都是）。
    所以"图片里的中文"**造不出来走这条路**——中文样本一律走 `XP*` / `UserComment`
    （字节字段，见本文件第五节），那一族是**真的**能带着中文落盘的。
    这条对判别的意义：`ImageDescription: ??????` 是**写样本的人**造成的，不是我们解错了。
    """
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={"ImageDescription": "The Bund at dusk"}))
    doc = extractor.extract(path)
    assert doc.title is None
    assert "ImageDescription: The Bund at dusk" in doc.text


def test_the_doc_is_a_text_modality_doc_from_this_extractor(extractor, tmp_path):
    doc = extractor.extract(jpeg(tmp_path, exif=_exif_blob(ifd0={"Model": "M"})))
    assert doc.modality == "text"          # v1 全是 text，见 `core/models.py`
    assert doc.source == "media_meta"
    assert doc.truncated is False
    assert doc.warnings == ()


def test_an_animated_png_reports_its_frame_count(extractor, tmp_path):
    frames = []
    for red, green, blue in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
        one = Image.new("P", (8, 8))
        one.putpalette([red, green, blue] + [0] * 765)
        frames.append(one)
    buffer = io.BytesIO()
    frames[0].save(buffer, "PNG", save_all=True, append_images=frames[1:], duration=100)
    doc = extractor.extract(_write(tmp_path, "anim.png", buffer.getvalue()))
    assert doc.meta["frames"] == 3


# ==============================================================================
# 四、跳过规则
# ==============================================================================


def test_pointer_tags_are_skipped(extractor, tmp_path):
    """`ExifOffset` / `GPSInfo` 的值是一个字节偏移，真正的内容由子 IFD 读进来。"""
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"Model": "M"},
        sub={"DateTimeOriginal": "2021:07:04 12:00:01"},
        gps={"GPSLatitudeRef": "N", "GPSLatitude": _fracs((31, 1), (13, 1), (4960, 100))},
    ))
    text = extractor.extract(path).text
    assert "ExifOffset" not in text and "GPSInfo" not in text
    assert "DateTimeOriginal: 2021:07:04 12:00:01" in text


def test_sequences_are_skipped_except_the_gps_three(extractor, tmp_path):
    """序列的含义只能靠 tag 定，硬猜是把噪声写进正文（`LensSpecification` 是典型）。"""
    path = jpeg(tmp_path, exif=_exif_blob(sub={
        "LensSpecification": _fracs((4, 1), (4, 1), (28, 10), (28, 10)),
        "LensModel": "back camera 4.25mm",
    }))
    text = extractor.extract(path).text
    assert "LensSpecification" not in text
    assert "LensModel: back camera 4.25mm" in text


def test_tiff_structural_tags_are_skipped(extractor, tmp_path):
    """`.tif` 的 IFD0 里全是布局字段（实测 11 个里有 9 个），它们与 meta 是同一件事。"""
    path = tiff(tmp_path, exif=_exif_blob(ifd0={"Make": "TestMake"}))
    text = extractor.extract(path).text
    for name in ("ImageWidth", "ImageLength", "BitsPerSample", "Compression",
                 "StripOffsets", "StripByteCounts", "SamplesPerPixel", "RowsPerStrip"):
        assert name not in text
    assert "Make: TestMake" in text


def test_a_whitespace_only_value_is_dropped(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={"Software": "   ", "Model": "M"}))
    text = extractor.extract(path).text
    assert "Software" not in text
    assert "Model: M" in text


# ==============================================================================
# 五、中文那一族：`XP*` 与 `UserComment`
# ==============================================================================


def test_xp_title_is_decoded_from_utf16_bytes(extractor, tmp_path):
    """Windows 资源管理器里改的"标题"就写在这里——中文照片唯一的中文落点。"""
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"XPTitle": "外滩日落".encode("utf-16-le") + b"\x00\x00"},
    ))
    assert "XPTitle: 外滩日落" in extractor.extract(path).text


def test_xp_keywords_keep_their_separator(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"XPKeywords": "上海;外滩;夜景".encode("utf-16-le") + b"\x00\x00"},
    ))
    assert "XPKeywords: 上海;外滩;夜景" in extractor.extract(path).text


def test_an_xp_field_with_a_bom_is_decoded_by_the_bom(extractor, tmp_path):
    """带 BOM 的按 BOM 走——这是唯一一处先看数据再决定字节序的地方。"""
    raw = b"\xfe\xff" + "标题".encode("utf-16-be")
    path = jpeg(tmp_path, exif=_exif_blob(ifd0={"XPTitle": raw}))
    assert "XPTitle: 标题" in extractor.extract(path).text


def test_a_broken_xp_field_is_skipped_with_a_warning(extractor, tmp_path):
    """奇数长度解不出来：**不许静默丢**——那会让"这张图的中文标题为什么搜不到"没有线索。"""
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"XPTitle": "标题".encode("utf-16-le") + b"\x00", "Model": "M"},
    ))
    doc = extractor.extract(path)
    assert "XPTitle" not in doc.text
    assert any("XPTitle 解不出来" in one for one in doc.warnings)
    assert "Model: M" in doc.text


def test_user_comment_with_a_unicode_declaration(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(
        sub={"UserComment": b"UNICODE\x00" + "相机里的备注".encode("utf-16-le") + b"\x00\x00"},
    ))
    assert "UserComment: 相机里的备注" in extractor.extract(path).text


def test_user_comment_with_an_ascii_declaration(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(sub={"UserComment": b"ASCII\x00\x00\x00hello there"}))
    assert "UserComment: hello there" in extractor.extract(path).text


def test_user_comment_with_an_undefined_declaration_is_not_guessed(extractor, tmp_path):
    """前 8 字节全是 NUL = "没声明编码"。这里**不回退**去猜（猜出来的是乱码，且不报错）。

    同一条也说明它**不算解失败**：没有"解不出来"的警告，只是这个字段不出现。
    （`Model` 是陪衬，否则整篇正文为空、那条"没有可索引的文字"的警告会掺进来。）
    """
    path = jpeg(tmp_path, exif=_exif_blob(
        ifd0={"Model": "M"}, sub={"UserComment": b"\x00" * 8 + b"whatever"},
    ))
    doc = extractor.extract(path)
    assert "UserComment" not in doc.text
    assert "Model: M" in doc.text
    assert not any("UserComment" in one for one in doc.warnings)


# ==============================================================================
# 六、GPS 的度分秒
# ==============================================================================


def test_gps_is_composed_into_decimal_degrees(extractor, tmp_path):
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        "GPSLatitudeRef": "N", "GPSLatitude": _fracs((31, 1), (13, 1), (4960, 100)),
        "GPSLongitudeRef": "E", "GPSLongitude": _fracs((121, 1), (28, 1), (2552, 100)),
    }))
    text = extractor.extract(path).text
    assert "GPSLatitude: 31.230444 N" in text
    assert "GPSLongitude: 121.473756 E" in text


@pytest.mark.parametrize("ref,sign", [("S", "-"), ("W", "-")])
def test_southern_and_western_hemispheres_go_negative(extractor, tmp_path, ref, sign):
    name = "GPSLatitude" if ref == "S" else "GPSLongitude"
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        f"{name}Ref": ref, name: _fracs((31, 1), (13, 1), (0, 1)),
    }))
    assert f"{name}: {sign}31.216667 {ref}" in extractor.extract(path).text


@pytest.mark.parametrize("ref,expected", [(0, "12.3 m"), (1, "-12.3 m")])
def test_gps_altitude_sign_follows_the_ref(extractor, tmp_path, ref, expected):
    """`GPSAltitudeRef` 在 Pillow 这一侧是**字节**（实测 `b"\\x00"`），所以这里两种形态都过。"""
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        "GPSAltitudeRef": ref, "GPSAltitude": _fracs((1234, 100)),
    }))
    assert f"GPSAltitude: {expected}" in extractor.extract(path).text


def test_a_gps_value_without_a_direction_keeps_the_number(extractor, tmp_path):
    """方向字母缺失时不编一个——只给度，不给一个猜出来的半球。"""
    path = jpeg(tmp_path, exif=_exif_blob(gps={"GPSLatitude": _fracs((31, 1), (0, 1), (0, 1))}))
    assert "GPSLatitude: 31.000000" in extractor.extract(path).text


def test_the_direction_partners_are_not_listed_again(extractor, tmp_path):
    """方向并进值里了，`GPSLatitudeRef: N` 再单列一行只是噪声。"""
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        "GPSLatitudeRef": "N", "GPSLatitude": _fracs((31, 1), (0, 1), (0, 1)),
    }))
    text = extractor.extract(path).text
    assert "GPSLatitudeRef" not in text
    assert "GPSLatitude: 31.000000 N" in text


def test_a_malformed_gps_value_is_dropped_not_guessed(extractor, tmp_path):
    """不是三段的（坏 EXIF 里会出现）就整个字段不要——**不猜**。"""
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        "GPSLatitude": _fracs((31, 1), (13, 1)), "GPSDateStamp": "2021:07:04",
    }))
    text = extractor.extract(path).text
    assert "GPSLatitude" not in text
    assert "GPSDateStamp: 2021:07:04" in text   # 别的 GPS 字段照原规则走


def test_gps_time_stamp_is_not_composed(extractor, tmp_path):
    """`GPSTimeStamp` 是三段分数，v1 不合成它（它按序列规则跳过）。"""
    path = jpeg(tmp_path, exif=_exif_blob(gps={
        "GPSTimeStamp": _fracs((12, 1), (0, 1), (1, 1)),
    }))
    assert "GPSTimeStamp" not in extractor.extract(path).text


# ==============================================================================
# 七、容器自己带的文本块
# ==============================================================================


def test_png_text_chunks_become_lines(extractor, tmp_path):
    """生成图的参数就写在这里（提示词、步数、种子）——全盘上最难从别处找回来的内容。"""
    path = png(tmp_path, text={
        "parameters": "a photo of a cat, steps: 30, seed: 12345",
        "Software": "Stable Diffusion",
    })
    text = extractor.extract(path).text
    assert "parameters: a photo of a cat, steps: 30, seed: 12345" in text
    assert "Software: Stable Diffusion" in text


def test_png_text_keys_come_out_sorted(extractor, tmp_path):
    path = png(tmp_path, text={"zeta": "Z", "alpha": "A", "middle": "M"})
    assert lines_of(extractor.extract(path)) == ["alpha: A", "middle: M", "zeta: Z"]


def test_a_multiline_value_becomes_multiple_lines(extractor, tmp_path):
    """续行不再带键名（`_Text.block()` 的口径）：键名只在第一行，后面是原样的内容。"""
    path = png(tmp_path, text={"parameters": "a cat\nsteps: 30\nseed: 12345"})
    assert lines_of(extractor.extract(path)) == [
        "parameters: a cat", "steps: 30", "seed: 12345",
    ]


def test_png_exif_comes_out_too(extractor, tmp_path):
    """PNG 的 `eXIf` 块（新工具写的 PNG 有它）走的是同一条路。"""
    path = png(tmp_path, exif=_exif_blob(ifd0={"Make": "TestMake"}))
    assert "Make: TestMake" in extractor.extract(path).text


def test_jpeg_comment_is_decoded(extractor, tmp_path):
    """COM 段在 Pillow 这一侧是**裸字节**，所以走本项目自己那套解码。"""
    path = jpeg(tmp_path, comment="这张是扫描件，来源见文件夹".encode("utf-8"))
    doc = extractor.extract(path)
    assert "Comment: 这张是扫描件，来源见文件夹" in doc.text
    assert doc.warnings == ()                   # 严格 UTF-8 解得下来，不算猜


def test_a_bare_png_is_an_empty_doc_with_a_warning(extractor, tmp_path):
    """没有元数据 = 正文是空的，**不是失败**；但必须出声，否则读起来像"搜不到"。"""
    doc = extractor.extract(png(tmp_path))
    assert doc.text == ""
    assert doc.is_empty is True
    assert doc.meta["format"] == "PNG"          # 格式与尺寸照旧记着，只是不进正文
    assert len(doc.warnings) == 1
    assert "没有可索引的文字" in doc.warnings[0]


# ==============================================================================
# 八、上限与截断
# ==============================================================================


def test_truncation_stops_on_a_whole_line(tmp_path):
    """上限到了就**整行地停**：正文到哪里为止要一句话说得清（`D8` 的同一套语义）。"""
    path = png(tmp_path, text={"a": "1", "b": "2", "c": "3"})
    doc = MediaMetaExtractor(ExtractConfig(), max_chars=len("a: 1\nb: 2")).extract(path)
    assert lines_of(doc) == ["a: 1", "b: 2"]     # 第三行整条不在，而不是被切一半
    assert doc.truncated is True
    assert any("MAX_CHARS" in one for one in doc.warnings)


def test_an_over_long_value_is_dropped_whole_and_says_so(tmp_path):
    """一个超长的值不是被截半句，而是**整行不收**——两种警告都要留下（截断 + 正文空）。"""
    path = png(tmp_path, text={"parameters": "x" * 200})
    doc = MediaMetaExtractor(ExtractConfig(), max_chars=50).extract(path)
    assert doc.text == ""
    assert doc.truncated is True
    assert any("MAX_CHARS" in one for one in doc.warnings)
    assert any("没有可索引的文字" in one for one in doc.warnings)


def test_the_default_cap_is_documented():
    """上限是模块级常量（比 office / web_mail 的 1e6 小一个数量级，理由在模块文档）。"""
    assert MAX_CHARS == 200_000


# ==============================================================================
# 九、失败
# ==============================================================================


def test_a_renamed_file_is_a_parse_failure(extractor, tmp_path):
    """扩展名写着 .jpg、内容不是图片：这是**这个文件坏了**，不是"这一类不做"。"""
    path = _write(tmp_path, "fake.jpg", "这不是图片，是一段文本\n".encode("utf-8"))
    with pytest.raises(ParseFailed) as caught:
        extractor.extract(path)
    assert "UnidentifiedImageError" in str(caught.value)
    assert "Pillow" in str(caught.value)


def test_a_truncated_header_is_a_parse_failure(extractor, tmp_path):
    """头都没读完（SOI 之后断了）：Pillow 抛 `OSError`，必须翻译成 `ParseFailed`。

    漏掉这一族的话，它会以"未捕获的 OSError"冒到批量层去——`A6` 的反面。
    """
    path = _write(tmp_path, "short.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
    with pytest.raises(ParseFailed) as caught:
        extractor.extract(path)
    assert "Pillow" in str(caught.value)


def test_a_decompression_bomb_is_a_failure_not_a_crash(extractor, tmp_path, monkeypatch):
    """`DecompressionBombError` **不是** `OSError`（Pillow 里它直接继承 `Exception`）。

    漏掉它 = 一个没有被隔离的例外从提取层冒出去。这一条就是那个回归线。
    """
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ParseFailed) as caught:
        extractor.extract(png(tmp_path, size=(8, 8)))     # 64 像素 > 上限的 2 倍
    assert "DecompressionBombError" in str(caught.value)


def test_a_missing_file_is_unreadable_not_a_raw_oserror(extractor, tmp_path):
    """自己去 `Image.open(path)` 的话这里抛的是 `FileNotFoundError`——不是 `ExtractError`。

    所以这条同时钉着"读盘走 `base.read_bytes()`"这一半。
    """
    with pytest.raises(FileUnreadable):
        extractor.extract(os.path.join(str(tmp_path), "nope.jpg"))


def test_the_size_cap_comes_from_the_scan_config(tmp_path):
    """上限只有 `[scan] max_file_mb` 一个真相来源（超了是失败，**不截断**）。"""
    path = jpeg(tmp_path)
    with pytest.raises(FileUnreadable) as caught:
        MediaMetaExtractor(ExtractConfig(max_bytes=10)).extract(path)
    assert "上限" in str(caught.value)


def test_a_corrupt_exif_segment_is_not_a_failure(extractor, tmp_path):
    """**坏元数据不是坏图片**：EXIF 段烂掉时 Pillow 发警告、跳过，我们照收这个文件。

    钉的是我们的处置（正文里没有乱码、有一条 `Pillow 警告`），**不是 Pillow 的那句话**
    ——那句话是库自己的措辞，随版本会变，写死在断言里只会让升级时红一条假线。
    """
    good = jpeg(tmp_path, name="good.jpg", exif=_exif_blob(ifd0={"Model": "M"}))
    with open(good, "rb") as handle:
        raw = handle.read()
    start = raw.index(b"\xff\xe1")
    declared = int.from_bytes(raw[start + 2:start + 4], "big")
    junk = b"Exif\x00\x00" + b"MM\x00\x2a" + b"\xff" * 8
    broken = raw[:start] + b"\xff\xe1" + (len(junk) + 2).to_bytes(2, "big") + junk \
        + raw[start + 2 + declared:]
    path = _write(tmp_path, "bad-exif.jpg", broken)

    doc = extractor.extract(path)               # 不抛异常
    assert doc.meta["format"] == "JPEG"
    assert doc.text == ""                        # 读不出来的字段没有进正文
    assert any(one.startswith("Pillow 警告") for one in doc.warnings)
    assert "�" not in doc.text


# ==============================================================================
# 十、钉住模块文档第一段的测试
# ==============================================================================


def _boom(*_args, **_kwargs):
    raise AssertionError("load() 被调用了：这一层不该解码像素")


def _assert_the_trap_is_armed(data: bytes, name: str) -> None:
    """确认"桩接上了"——**真去 `.load()` 必须炸**。

    ⚠ 这几条"不解码"的测试有一个**静默失效**的形态：桩打在 `Image.Image.load`
    上，而 `TiffImageFile` / `WebPImageFile` 都**自己覆盖了 `load`**。要是哪个格式
    的解码路径绕开了父类那个 `load`，桩就接不上，于是"没炸"会被读成"没解码"——
    测试**永远是绿的，而它什么都没验**。所以每条都先正着炸一次。
    （`2026-10-16` 实测：四个格式都接得上，因为两个覆盖版最后都走到
    `ImageFile.load` 里那句 `pixel = Image.Image.load(self)`。）
    """
    with pytest.raises(AssertionError):
        Image.open(io.BytesIO(data)).load()
    assert name  # 只是为了在断言失败时看得出是哪条参数化的样本


@pytest.mark.parametrize("name", ["a.jpg", "a.tif", "a.webp"])
def test_a_jpeg_is_never_decoded(tmp_path, monkeypatch, name):
    """JPEG / TIFF / WebP 上一行像素都不解（模块文档第一段）。"""
    fmt = PIL_FORMATS[name.rsplit(".", 1)[1]]
    holder = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(
        holder, fmt, exif=_exif_blob(ifd0={"Model": "M"})
    )
    path = _write(tmp_path, name, holder.getvalue())
    monkeypatch.setattr(Image.Image, "load", _boom)
    _assert_the_trap_is_armed(holder.getvalue(), name)
    assert "Model: M" in MediaMetaExtractor(ExtractConfig()).extract(path).text


def test_png_does_go_through_load_and_that_is_pillows_choice(tmp_path, monkeypatch):
    """**PNG 是个例外**，而触发它的是**我们**读 `.text` 那一行。

    `PngImageFile.text` 只要 `_text is None` 就调 `self.load()`
    （`PngImagePlugin.py:835`，注释写着 `tEXt` 可以排在 `IDAT` 后面），而
    `_container_lines()` 对每种格式都取 `image.text`——所以每张 PNG 都会被解出来。

    钉住它是为了让模块文档第一段那句实话**不会**在下次重构时被悄悄抹掉：
    谁要改成"PNG 也走不解码的路"，就该连着那段文档一起改。
    """
    path = png(tmp_path, text={"Software": "Stable Diffusion"})
    monkeypatch.setattr(Image.Image, "load", _boom)
    with open(path, "rb") as handle:          # 桩接没接上：拿盘上那份真字节正着试一次
        _assert_the_trap_is_armed(handle.read(), "shot.png")
    with pytest.raises(AssertionError):
        MediaMetaExtractor(ExtractConfig()).extract(path)


def test_png_getexif_only_loads_without_an_exif_chunk(tmp_path, monkeypatch):
    """模块文档第一段的**另一半**：`PngImageFile.getexif()` 只在没有 `eXIf` 块时解码。

    源码就一句 `if "exif" not in self.info: self.load()`（`PngImagePlugin.py:1097`）。
    初稿把 `getexif()` 与 `text` 写成含糊的一句"都调了 `self.load()`"，**在带 `eXIf`
    块的 PNG 上是假的**——这条钉住"块在时它真懒"，也就是说明"PNG 会解码"确实有两个
    来源，只堵 `getexif()` 那条不够。
    """
    # ⚠ 样本必须在**打桩之前**造好：`Image.save()` 自己就会调 `load()`
    with_exif = png(tmp_path, "with.png", text={"Software": "S"},
                    exif=_exif_blob(ifd0={"Model": "M"}))
    without = png(tmp_path, "without.png", text={"Software": "S"})
    monkeypatch.setattr(Image.Image, "load", _boom)

    # 块在时 `getexif()` 不碰像素；块不在时它自己去解
    Image.open(with_exif).getexif()
    with pytest.raises(AssertionError):
        Image.open(without).getexif()

    # 而 `text` 两条路都解——所以整条抽取在任何一种 PNG 上都会解码
    for path in (with_exif, without):
        with pytest.raises(AssertionError):
            getattr(Image.open(path), "text", None)


