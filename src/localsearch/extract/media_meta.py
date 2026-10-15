r"""图片的元数据（`F3-04`）：文件里除了文件名之外还带着的那点字。

对应功能点 **F3-04**；验收项 **A6**（单个文件解析失败要被隔离，不许把整轮索引带下去）。

------------------------------------------------------------------------------
这一层只出"文件里带的文字"：JPEG / TIFF 一如字面，**PNG 是个例外**
------------------------------------------------------------------------------

`format` / `size` / `mode` 是文件头里写着的，`getexif()` 读的是元数据段——所以
"这张图多大"这件事在这里只值几个字节，与"把它解开成 4032x3024x3 的张量"无关。
`2026-10-16` 用一个桩验过（`test_a_jpeg_is_never_decoded`：把 `Image.Image.load`
换成抛异常的桩，JPEG / TIFF / WebP / AVIF 的抽取照常走完）。

⚠ **PNG 不成立**：Pillow 的 `PngImageFile.getexif()` 与它的 `text` 属性里都调了
`self.load()`（同一个桩在 PNG 上当场炸掉，`test_png_does_go_through_load_and_that_is_pillows_choice`
把这条钉着）。原因是 PNG 的 `tEXt` / `iTXt` / `zTXt` 块**可以排在 `IDAT` 后面**，
要确定没有更多文字就必须把整张图解出来。三条推论，都得说清楚：

1. **一张大 PNG 的抽取成本与解码它相当**（时间与内存）。这不是我们的选择，是上游的
   实现。要绕开只能自己扫块（PNG 的块结构很简单），代价是 PNG 会走一条**自己的**
   EXIF 路径（`info["exif"]` + `Image.Exif().load()` 实测可行、不触发 `load()`），
   而两条路径迟早会在"Pillow 怎么合并 eXIf"这件事上分岔——**v1 不绕**，把代价写在这里。
2. **`.lsearchignore` 默认把 `*.png` 标成只记名字**，所以默认规则下这笔钱不花；
   要花是因为用户自己改了规则去索引 PNG。改之前先看这一条。
3. **解压炸弹那道闸门在 PNG 上是真的在防**，在 JPEG 那几类上是预防性的（`P9` 真要
   解码像素时它们才会真撞上）。所以 `DecompressionBombWarning` **不丢**、照实记成
   一条警告——把 PNG 那条真的风险一起丢掉，是拿"看起来更干净"换掉一个真信号。

同样是这条，决定了下面的失败分类：**坏元数据不是坏图片**。一个 EXIF 段烂掉的 JPEG
在这里仍然是一张读得出来的图片——Pillow 会发一条 `Corrupt EXIF data` 警告、把读不动
的部分跳过，于是我们把它记成**一条警告**（`warnings`），而不是 `ParseFailed`。

------------------------------------------------------------------------------
认领哪几类，以及**故意不认领**哪些
------------------------------------------------------------------------------

`IMAGE_EXTS` 里有五族、共 12 个扩展名：JPEG（`jpg` / `jpeg` / `jpe` / `jfif`）、
TIFF（`tif` / `tiff`）、PNG（`png` / `apng`）、WebP、AVIF（`avif` / `avifs`）。
这一族一族的名单短，是因为认领的判据不是"这是不是图片"，而是**"这个格式里有没有
我们拿得到的文字"**。

**故意不认领**的，两类，理由不同：

| 不认领的 | 为什么 |
|---|---|
| `heic` / `heif`、相机 RAW（`cr2` / `nef` / `arw` / `dng` / `orf` / `rw2`） | 本机 Pillow 12.3.0 的注册表里**根本没有**这些扩展名（`IMAGE_EXTS` 那一行的探针见 `PROGRESS.md` §3 `E34` 的邻居那一栏），要读得另装 `pillow-heif` / `rawpy`——计划 §7.4 没有它们，v1 不加。**不认领的下场是 `UnsupportedFormat`**：那些文件留在 `NAME_ONLY`，文件名照搜、**不进失败报表**。反过来说，如果在这里认领了它们，全盘上每一张 iPhone 照片都会变成一条 `ParseFailed`，把真坏的文件淹掉。 |
| `gif` / `bmp` / `tga` / `ico` / `psd` / `jp2` 一族 | 这些格式**没有** EXIF 这个概念的（GIF 有注释扩展、PSD 有 image resource，但 Pillow 这一侧读不到，**未实测的不写进来**）。认领它们只会造出一批"提取成功、正文是空的"记录——那是 `Extractor` 协议文档里点名的、最难查的一种形态。 |

⚠ 上面这两条与 `.lsearchignore` 的默认规则**方向一致但不是同一件事**：那份规则是
**用户可改的唯一来源**（它今天把 `*.jpg` 也标成只记名字，所以这个提取器在当前规则下
根本不会被调用）。改规则之前先读 `PROGRESS.md` §4 P3「遗留」第 2 / 3 条。

------------------------------------------------------------------------------
正文是什么：只有元数据字段，没有格式与尺寸
------------------------------------------------------------------------------

`text` 里逐行是**字段**：EXIF 的 `名字: 值`，加上容器里的文本块（PNG 的
`tEXt` / `iTXt` / `zTXt`、JPEG 的 COM 注释）。`format` / `width` / `height` / `mode`
**只进 `meta`，不进正文**——三个理由，按重要性排：

1. **"JPEG" 会命中全盘每一张图。** 把格式写进正文，等于给每张图都塞一个高频词进
   FTS，这是纯噪声；尺寸（`4032x3024`）同理，没有人拿它当查询词。
2. **文件名与路径不是这一层的事。** 计划 §1.3 的"v1 只索引其文件名、路径与元数据"
   里，前两件由扫描器在 Tier0 就索引了（`files` 表），再往正文里抄一份是我们自己
   制造的重复计数——与「遗留」第 11 条（邮件的 `主题` 同时进 `title` 与正文）是
   同一种代价，区别只在于邮件那份是**故意的**（要服务两条检索路径），这里不是。
3. **`meta` 的定位就是"给人看与排查用"**（`core/models.py`）。格式与尺寸正是那种
   一问就有的东西，它们不必进检索。

由此推出一条**看起来像遗漏、其实是想清楚了的**结果：一张没有 EXIF 的图（截图、
被处理过的图、纯色 PNG）**正文就是空的**，`is_empty` 为真，同时带一条警告说明原因。
空正文在 `Extractor` 的约定里是合法结果（`0` 字节文件就是），不是搪塞。

**`title` 也一律是 `None`**，包括 EXIF 里明明有 `ImageDescription` 的时候：图片没有
"文档标题"这个概念，而把一个 EXIF 字段提成 `title` 会让同一份文字在两条检索路径下
不一致（词法那条看 `text`、语义那条看 `title: {t} | text: {c}`）。要两条都留就得像
邮件那样**故意重复**，那件事等到 `P5` 真接上前缀、量得出代价时再定（见「遗留」）。

------------------------------------------------------------------------------
字段怎么挑：类型过滤 + 按名跳过 + GPS 一处例外
------------------------------------------------------------------------------

**不写白名单**，因为 EXIF 的 tag 表是个开放集合：`LensModel`、`BodySerialNumber`、
`OffsetTimeOriginal` 这些较新的 tag 与厂商私有段恰恰是"值钱"的地方，白名单会随着
新机型的出厂日期而过时。所以走**通用的类型过滤 + 一张很小的跳过表**：

| 值长什么样 | 怎么办 | 为什么 |
|---|---|---|
| `bytes` | 跳过，**`XP*` 与 `UserComment` 两族除外** | 大多数字节值没有编码声明（`MakerNote` 的格式只有厂商知道），认不出编码的字节不该当字收进来。例外的那两族是**声明过编码的**，见下一节 |
| 序列（`tuple` / `list`） | 跳过，**GPS 那三个除外** | 序列在这张表里要么是布局（`StripOffsets` 是一串偏移）、要么是含义只能靠 tag 定的东西（`LensSpecification` 是"最小焦距、最大焦距、最小光圈、最大光圈"）。硬猜含义是把噪声写进正文；GPS 之所以值得单独处理，见下 |
| 数字 | `_number()`：能整除的写成整数，否则留 6 位小数并去掉尾部的 0 | EXIF 里的分数是 `28/10` 这种形态，直接打出来是 `2.7999999999999998` |
| 字符串 | 去掉 NUL 与首尾空白，空的丢掉 | 有些字段在尾上垫 NUL |

外加 `_SKIP_TAGS` 那张按**名字**的跳过表，三组：指针（值是一个数字，说着"另一个 IFD
在文件里的第几个字节"——真正的内容由子 IFD 分别走进来读）、二进制块（`MakerNote` 一族，
类型过滤其实已经挡得住，列出来是为了让"我们确实想过这些字段"有据可查）、以及
**TIFF 的结构性字段**（`ImageWidth` / `Compression` / `StripOffsets` 那一批：`.tif`
的 IFD0 里全是这些，而它们与 `meta` 里的 `format` / `width` / `height` 是同一件事）。

------------------------------------------------------------------------------
两处例外：GPS 的度分秒，以及中文照片唯一的落点 `XP*`
------------------------------------------------------------------------------

**（一）GPS 的度分秒要合成十进制度。** `GPSLatitude` 的原始形态是三个分数
（度、分、秒）加上另一个字段里放的方向字母（`GPSLatitudeRef: N`），这个形态在文本里
既读不出来也搜不到——搜 `31.23` 命中不了 `31, 13, 49.6`。所以把它合成：
`GPSLatitude: 31.230444 N`，方向字母并进值里（南半球、西半球取负）；`GPSAltitude`
同理（`GPSAltitudeRef` 为 `1` 表示海平面**以下**，取负），单位写出来。
**其余 GPS 字段照原规则走**：`GPSDateStamp` 是字符串、收；`GPSTimeStamp` 是三段分数、
按序列跳过（v1 不合成它）。

**（二）`XP*` 与 `UserComment` 要从字节里解出来。** 这一条是为中文语料写的：相机字段
（`Make` / `Model` / `Software`）全是英文，而**中文照片唯一可能带着自己那句中文的地方**
就是这两族——`XPTitle` / `XPComment` / `XPAuthor` / `XPKeywords` / `XPSubject` 是
Windows 资源管理器里右键改的"标题 / 标记 / 备注 / 作者"（小端 UTF-16、尾部垫 NUL），
`UserComment` 是相机备注（前 8 字节编码声明 + 内容）。它们跟着通用规则走的话会**因为
类型是 bytes 被整族丢掉**，而丢掉的是这批文件里唯一值得搜的文字。

不信这两样、或者声明与实际不符（奇数长度）时**不猜**：返回空并留一条警告。⚠ `XP*`
那一族的字节序（小端、不带 BOM）是**二手信息**——本机没有 Windows 资源管理器写出来的
样本可对照，`PIL` 的往返只能证明我们写进去的小端字节原样还回来。带 BOM 的按 BOM 走。

------------------------------------------------------------------------------
一行太长会**整行**被丢掉，而不是被切一半
------------------------------------------------------------------------------

上限是 `MAX_CHARS`（比 `office` / `web_mail` 的 1e6 小得多：那两个的正文是**文档**，
这里的正文是**元数据**，正常的 EXIF 是几百字节量级）。攒的地方是 `_text._Text`，
它到顶时**整行地停**——所以一个超长的 `ImageDescription`（几万字的描述）不是被截成
半句，而是整行不收，`ExtractedDoc.truncated` 为真、警告里说明截在哪。这与 `office`
的 `xlsx` 截断是同一条规矩：**"正文到哪里为止"要能一句话说得清**。

⚠ 这里**没有标记行**（不用 `_Text.marker()`）：图片是一整块内容，没有"第 N 页"那种
位置信息可标。于是 `_Text.has_body` 在这个模块里的含义就退化成"有没有收到过字段
行"，那正是"这张图里有没有可索引的文字"这个问题的答案。

------------------------------------------------------------------------------
读盘：走 `read_bytes()`，字节交给 `BytesIO`
------------------------------------------------------------------------------

`Image.open()` 本来是可以接路径的，这里**不接**：

- `base.read_bytes()` 是**唯一的读盘入口**，它第一行过 `extended_path`
  （`F2-02` 的债，`E27`）——自己写 `Image.open(path)` 等于把那条债背回来。
- 读进来的字节有上限（`[scan] max_file_mb`），超了是 `FileUnreadable` 而不是
  让 Pillow 自己去读一个可能已经变大的文件。
- 顺带得到两个好处：Pillow 拿到的是 `BytesIO`，不存在"文件句柄被关掉之后还能不能
  懒加载"的问题；`IOError` 与"格式坏了"也就分得干干净净（前者在 `read_bytes` 里
  就已经抛成 `FileUnreadable` 了）。

代价是**整张图的字节进内存**，上限就是那个 `max_file_mb`（默认 64 MB）。这一条写在
这里是为了下次有人想"改成接路径省内存"时先看见：省下来的是这个上限以内的内存，
换回来的是两条已经修好的债。

⚠ `M2` 那条"模型只接受 bf16/fp32"与本模块无关；这里是图片，不是权重。
"""

from __future__ import annotations

import io
import math
import numbers
import struct
import warnings
import zlib
from typing import Any

from localsearch.core.models import ExtractedDoc

from ._text import _Text
from .base import (
    ExtractConfig,
    Extractor,
    ParseFailed,
    decode_bytes,
    read_bytes,
)

#: 认领的扩展名。**判据是"这个格式里有没有我们拿得到的文字"，不是"这是不是图片"**——
#: 逐族写清、故意不收的那两类写在模块文档的表格里。分组只是给人看的，代码不解析注释。
IMAGE_EXTS = frozenset({
    # JPEG 家族：EXIF 的主要载体，全盘上最可能带相机与拍摄信息的就是它们
    "jpg", "jpeg", "jpe", "jfif",
    # TIFF：EXIF 的老家（读出来的字段最多；结构性 tag 由 _SKIP_TAGS 挡掉）
    "tif", "tiff",
    # PNG：传统上没有 EXIF，但 tEXt / iTXt / zTXt 里有真文字（生成图的参数就写在那里）
    "png", "apng",
    # 新容器：EXIF 各走各的块（本机 Pillow 12.3.0 实测写得进也读得出）
    "webp", "avif", "avifs",
})

#: 正文上限。比 `office` / `web_mail` 的 1e6 小一个数量级，理由见模块文档：
#: 那两个的正文是文档，这里的正文是元数据。正常的 EXIF 是几百字节量级，
#: 20 万字符已经覆盖"某个字段里塞了一篇长文"这种异常情况。
MAX_CHARS = 200_000

#: 按**名字**跳过的 EXIF tag。三组，每组一句理由（模块文档里那张表在讲判据）。
_SKIP_TAGS = frozenset({
    # （一）指针：值是一个数字，说着"另一个 IFD 在文件里的第几个字节"。
    # 真正的内容由 `_ifd_lines()` 分别走进子 IFD 去读，指针本身对搜索没有意义。
    "ExifOffset", "GPSInfo", "InteroperabilityOffset", "ThumbnailOffset",
    "ThumbnailLength", "JpegIFOffset", "JpegIFByteCount",
    "JPEGInterchangeFormat", "JPEGInterchangeFormatLength", "Thumbnail",
    # （二）二进制块：类型过滤（bytes 一律跳过）其实已经挡得住，列在这里是为了让
    # "我们确实想过这些字段"有据可查，而不是"碰巧它们的类型是 bytes"。
    "MakerNote", "ApplicationNotes", "PrintImageMatching", "XMLPacket",
    "ImageResources", "ICC_PROFILE", "Photoshop", "GlobalParametersIFD",
    # （三）TIFF 的结构性字段：`.tif` 的 IFD0 里全是这些（`E34` 那一轮实测的
    # 一个 20x10 的 TIFF，IFD0 上 11 个 tag 里有 9 个是这一组）。它们与 `meta` 里的
    # format / width / height 是同一件事，而且没有人会去搜 "Compression: 1"。
    "ImageWidth", "ImageLength", "ImageHeight", "BitsPerSample", "Compression",
    "PhotometricInterpretation", "SamplesPerPixel", "RowsPerStrip",
    "StripOffsets", "StripByteCounts", "TileWidth", "TileLength", "TileOffsets",
    "TileByteCounts", "PlanarConfiguration", "Predictor", "SampleFormat",
    "FillOrder", "ResolutionUnit", "XResolution", "YResolution",
    "YCbCrSubSampling", "YCbCrPositioning", "WhitePoint", "TransferFunction",
    "PrimaryChromaticities", "YCbCrCoefficients", "ReferenceBlackWhite",
    "ExtraSamples", "DotRange", "JPEGTables", "PageNumber", "FreeOffsets",
    "FreeByteCounts", "SubIFDs", "Rating", "RatingPercent",
})

#: GPS 那三个"要合成"的字段：值 → 方向/正负写在哪个字段里。
#: `None` 表示这个字段没有方向字母（高度用的是 `GPSAltitudeRef` 的 0/1）。
_GPS_COMPOSED = {
    "GPSLatitude": "GPSLatitudeRef",
    "GPSLongitude": "GPSLongitudeRef",
    "GPSAltitude": "GPSAltitudeRef",
}

#: 值**是字节、其实是文字**的两族字段（判据与代价见模块文档那一节）。
#: `XP*` 是 Windows 的图片属性：在资源管理器里右键改的"标题 / 标记 / 备注 / 作者"
#: 就写在这里，是**中文照片唯一可能带着自己那句中文**的地方（`Make` / `Model` 那些
#: 是英文的）。`UserComment` 是相机自己的备注，前面挂着 8 字节的编码声明。
_XP_TAGS = frozenset({"XPTitle", "XPComment", "XPAuthor", "XPKeywords", "XPSubject"})

#: `UserComment` 开头那 8 字节编码声明 → codec。声明不足 8 字节时右边补 NUL
#: （`ASCII` 补 3 个、`UNICODE` 补 1 个、`JIS` 补 5 个），所以比对前先去 NUL。
_USER_COMMENT_CODECS = {
    b"ASCII": "ascii",
    b"UNICODE": "utf-16",                       # 字节序看数据自己的 BOM，没有就按小端
    b"JIS": "shift_jis",
}

#: 合成时要一起丢掉的"同伴字段"：方向与正负已经并进值里，再单列一行只会变成噪声。
_GPS_DROPPED = frozenset({
    "GPSLatitudeRef", "GPSLongitudeRef", "GPSAltitudeRef",
})


def _number(value: float) -> str:
    """把分数写成好读的十进制。

    EXIF 里的分数在 Pillow 这一侧是 `IFDRational`（`numbers.Rational`，不是 `float`），
    直接把 `float` 打出来是 `28/10 -> 2.7999999999999998` 这种东西。能整除的写成整数
    （`ISOSpeedRatings` 的 `100`、`FocalLength` 的 `4`），否则留 6 位小数再去掉尾部的 0
    （`FNumber` 的 `28/10 -> 2.8`、`ExposureTime` 的 `1/250 -> 0.004`）。

    ⚠ **这一档是有损的**：`1/250` 变成 `0.004` 之后，"搜 `1/250`"搜不到。留下这个
    取舍是因为 EXIF 的分数含义只能靠 tag 定（同一个 `28/10` 在 `FNumber` 上是光圈、
    在别处可能是别的量），按 tag 写特例不值得；要搜快门速度的人改搜 `0.004`，
    或者按 `ExposureTime` 那个字段名去搜。**这不是遗漏**，与 `xlsx` 不复刻数字格式
    （「遗留」第 8 条）是同一种取舍。
    """
    if not math.isfinite(value):
        return str(value)                       # inf / nan 原样，别在这儿造一个假的数
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _bytes_text(name: str, value: bytes, notes: list[str]) -> str | None:
    """`XP*` 与 `UserComment` 这两族：把字节里的文字解出来；别的字段返回 `None`。

    - **`XP*`**：小端 UTF-16，尾部垫着 NUL（MS 的定义。⚠ "小端、不带 BOM"这一条是
      **二手信息**——本机没有 Windows 资源管理器写出来的样本可以对照，`PIL` 的
      往返只能证明我们自己写进去的小端字节原样还回来）。带 BOM 的按 BOM 走，
      这是唯一一处我们**先看数据再决定字节序**的地方。
    - **`UserComment`**：前 8 字节是编码声明（`ASCII` / `UNICODE` / `JIS`），后面才是
      内容。声明不认（8 个 NUL 是"未定义"）就返回 `None`，**不猜**。

    ⚠ 解不动时（奇数长度、声明与实际不符）返回 `None` 并留一条警告——这一族字段是
    "字节里藏着文字"，静默丢掉等于"这张图的中文标题为什么搜不到"没有线索。
    """
    if name in _XP_TAGS:
        raw = bytes(value)
        for bom, codec in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
            if raw.startswith(bom):
                try:
                    return raw.decode(codec).strip("\x00").strip() or None
                except UnicodeDecodeError as exc:
                    notes.append(f"{name} 解不出来（UTF-16，{exc}）：这个字段没有进正文")
                    return None
        codec = "utf-16-le"
    elif name == "UserComment":
        raw = bytes(value)
        declaration = raw[:8].rstrip(b"\x00")
        codec = _USER_COMMENT_CODECS.get(declaration)
        if codec is None:
            return None                          # 未定义 / 认不出的声明：不猜
        raw = raw[8:]
    else:
        return None

    try:
        return raw.decode(codec).strip("\x00").strip() or None
    except (UnicodeDecodeError, LookupError) as exc:
        notes.append(f"{name} 解不出来（声明是 {codec}，{exc}）：这个字段没有进正文")
        return None


def _render(value: Any) -> str | None:
    """一个 EXIF 值能不能变成一行字；不能就 `None`（判据与理由见模块文档那张表）。"""
    if value is None or isinstance(value, (bytes, bytearray)):
        return None
    if isinstance(value, (tuple, list)):
        # 序列一律跳过（GPS 那三个在 `_ifd_lines()` 里另有处理，走不到这里）
        return None
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):          # float 与 IFDRational 都在这一档
        return _number(float(value))
    text = str(value).replace("\x00", "").strip()
    return text or None


def _decimal_degrees(value: Any, ref: Any) -> str | None:
    """`GPSLatitude` / `GPSLongitude` 的三个分数 + 方向字母 → 十进制度。

    收不了就返回 `None`（**不猜**）：值不是三段、或者某一段不是数（坏 EXIF 里会出现
    整段读不出来、只剩一个空序列的情况），都落回"这个字段没有值"。
    """
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    parts: list[float] = []
    for one in value:
        if not isinstance(one, numbers.Real):
            return None
        parts.append(float(one))
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    if not all(math.isfinite(one) for one in parts):
        return None
    direction = ref.strip().upper()[:1] if isinstance(ref, str) else ""
    if direction in ("S", "W"):
        degrees = -degrees
    elif direction not in ("N", "E"):
        direction = ""                           # 方向缺失 / 认不出来：值照给，不编一个
    return f"{degrees:.6f} {direction}".rstrip()


def _altitude(value: Any, ref: Any) -> str | None:
    """`GPSAltitude` + `GPSAltitudeRef` → 米。`ref` 为 `1`（或 `b"\\x01"`）表示海平面以下。

    ⚠ `GPSAltitudeRef` 在 Pillow 这一侧是 **bytes**（实测：`b"\\x00"`），它在 EXIF 里
    是"一个字节的整数"——所以这里既认字节也认整数。
    """
    if not isinstance(value, numbers.Real):
        return None
    meters = float(value)
    if not math.isfinite(meters):
        return None
    below = ref in (1, b"\x01") or (isinstance(ref, bytes) and ref[:1] == b"\x01")
    return f"{-meters if below else meters:.1f} m"


def _ifd_groups(image: Any) -> list[tuple[dict[int, Any], dict[int, str], bool]]:
    """把三组 IFD 取出来：`(字段字典, tag 号→名字的表, 是不是 GPS 那一组)`。

    **只有这里碰 Pillow 那几处长在文件字节上的读取**（`getexif()` / `get_ifd()` 是懒的，
    坏段上出的事都在这一层），所以异常翻译也只包住这一层——正文的拼装（`_render()` /
    `_Text`）在外面，那里出的异常是本模块自己的 bug，**不该**被伪装成"这个文件坏了"。

    三组固定顺序：IFD0（基础字段）→ ExifIFD（拍摄参数）→ GPS IFD。

    接住的异常族是**一族"说不清"的**：`ValueError` / `KeyError` / `struct.error` 是
    Pillow 按 IFD 结构取数时会抛的那些，`OSError` 是它读不动字节时抛的。本机实测的
    四种坏 EXIF 段（`E34` 那一轮）**一个异常都没抛**、只发了 `Corrupt EXIF data` 警告，
    所以这里**不是有读数才接的**——接它是因为这一段离批量层太远：一个没接住的
    `KeyError` 冒出去就是整轮索引挂掉（`A6` 的反面），而输入的字节来自盘上任意一个
    文件。接了之后最坏是这一张图报失败，其余文件照常。
    """
    from PIL.ExifTags import GPSTAGS, IFD, TAGS

    try:
        exif = image.getexif()
        return [
            (dict(exif.items()), TAGS, False),
            (dict(exif.get_ifd(IFD.Exif).items()), TAGS, False),
            (dict(exif.get_ifd(IFD.GPSInfo).items()), GPSTAGS, True),
        ]
    except (OSError, ValueError, TypeError, KeyError, struct.error) as exc:
        raise _failed("EXIF 段", exc) from exc


def _ifd_lines(
    groups: list[tuple[dict[int, Any], dict[int, str], bool]],
    cap: _Text,
    notes: list[str],
) -> int:
    """把三组 IFD 逐行收进 `cap`，返回**收进来的字段数**。

    组**内**按 tag 号排序，而不是跟着 Pillow 内部那个字典的顺序走——正文的逐字稳定是
    硬要求：`Chunk.char_start` / `char_end` 是正文上的下标，`A9` 的向量缓存又按键
    哈希，所以同一份文件必须在任何一次运行里给出**逐字相同**的正文。
    """
    kept = 0
    for fields, table, gps in groups:
        # tag 号 → 值 的名字索引：GPS 那几个要读"同伴字段"（方向字母、海拔正负）
        by_name = {table[tag]: value for tag, value in fields.items() if tag in table}
        for tag in sorted(fields):
            name = table.get(tag)
            if name is None:                     # 表里没有的名字（厂商私有段）：不猜，跳过
                continue
            value = fields[tag]
            if gps and name in _GPS_DROPPED:
                continue
            if gps and name in _GPS_COMPOSED:
                partner = by_name.get(_GPS_COMPOSED[name])
                text = (
                    _altitude(value, partner) if name == "GPSAltitude"
                    else _decimal_degrees(value, partner)
                )
                if text is not None:
                    cap.block(f"{name}: {text}")
                    kept += 1
                continue
            if name in _SKIP_TAGS:
                continue
            if isinstance(value, (bytes, bytearray)):
                # 字节型的值一般是跳过（见模块文档那张表），唯一例外是"字节里藏着
                # 文字"的那两族——它们的解码与失败都在 `_bytes_text()` 里。
                text = _bytes_text(name, bytes(value), notes)
            else:
                text = _render(value)
            if text is not None:
                cap.block(f"{name}: {text}")
                kept += 1
            if cap.truncated:
                return kept
    return kept


def _container_lines(image: Any, cap: _Text, notes: list[str]) -> int:
    """容器自己带的文本块：PNG 的 `tEXt` / `iTXt` / `zTXt`，JPEG 的 COM 注释。

    这两样**不是 EXIF**，但它们是同一种东西——文件里带着的、能被人搜到的文字。
    PNG 上尤其值钱：Stable Diffusion 一类的生成图把参数（提示词、步数、种子）
    原样写在 `tEXt` 里，那是全盘上最难从别处找回来的内容。

    ⚠ 键按名字排序，理由与 `_ifd_lines()` 按 tag 号排序是同一条（正文逐字稳定）。

    这一块是**尽力而为**：`zTXt` 是压缩的，坏掉时 `zlib` 会抛（它的 `error` 不是
    `OSError`），而"某个文本块读不出来"不该把一张好图判成坏图——所以这里的失败记成
    一条警告，正文少一段，文件照收。与 EXIF 段坏掉时的处置同一条道理（模块文档第一段）。
    """
    kept = 0

    try:
        blocks = getattr(image, "text", None) or {}
    except (OSError, ValueError, zlib.error, struct.error) as exc:
        notes.append(f"PNG 文本块读不出来（{type(exc).__name__}：{exc}）：这一部分没有进正文")
        blocks = {}

    for key in sorted(blocks):
        value = blocks.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        # 用 `block()`：`iTXt` / `tEXt` 的值可以是多行的（生成图的参数就是），
        # 按行收进来、逐行受同一个上限管，续行不再带键名（`_Text.block()` 的口径）。
        cap.block(f"{key}: {value}")
        kept += 1

    comment = (getattr(image, "info", None) or {}).get("comment")
    if isinstance(comment, (bytes, bytearray)):
        # JPEG 的 COM 段在 Pillow 这一侧是**裸字节**（实测），没有声明编码。
        # 这里走本项目自己那套解码（`base.decode_bytes`）：BOM / 严格 UTF-8 /
        # 短数据按语料先验认 GB18030 / 探测器，猜出来的会留下一条警告。
        decoded = decode_bytes(bytes(comment))
        if decoded.text.strip():
            cap.block(f"Comment: {decoded.text}")
            kept += 1
            if decoded.warning:
                notes.append(f"JPEG 注释：{decoded.warning}")
    return kept


def _failed(kind: str, exc: BaseException) -> ParseFailed:
    """失败一律翻译成 `ParseFailed`，并把**是哪个库、报了哪句话**带上。

    与 `office.py` / `web_mail.py` 里那个同名函数同一个写法：这条消息是用户唯一能
    看见的东西（它进 `files.error_msg`），写"解析失败"四个字等于没写。
    """
    return ParseFailed(f"{kind} 解析失败（{type(exc).__name__}：{exc}）")


class MediaMetaExtractor:
    """图片 → 元数据字段（`F3-04`）。

    JPEG / TIFF / WebP / AVIF 上一行像素都不解；**PNG 除外**（Pillow 那一侧
    `getexif()` 与 `text` 都会 `load()`），代价与不绕它的理由写在模块文档第一段。
    """

    name = "media_meta"

    def __init__(self, config: ExtractConfig, *, max_chars: int = MAX_CHARS) -> None:
        self._config = config
        #: 可注入，默认 `MAX_CHARS`：测试要能在**不造一个 20 万字符的文件**的前提下
        #: 把截断那条路走一遍（与 `office` / `web_mail` 同一个写法）。
        self._max_chars = max_chars

    def supports(self, ext: str, mime: str) -> bool:
        """`IMAGE_EXTS` 里的认领；`mime` 一个字符都不用（`base.mime_of()` 的头一段）。

        没有扩展名的文件**不认领**：无扩展名那一支归 `plaintext`（带二进制嗅探），
        两边都想要的话谁在前面决定结果，而那种冲突是静默的。
        """
        del mime
        return ext in IMAGE_EXTS

    def extract(self, path: str) -> ExtractedDoc:
        from PIL import Image

        data = read_bytes(path, config=self._config)
        cap = _Text(self._max_chars)
        notes: list[str] = []

        # Image 的警告一律**留成正文的警告**：Pillow 的警告基本都是"这个文件有点问题、
        # 我绕过去了"（`Corrupt EXIF data` 就是典型）。绕过去的部分不进正文，
        # 不留下痕迹的话，"这张图的拍摄时间为什么搜不到"就变成一个没有线索的问题。
        # `DecompressionBombWarning` 也照收——PNG 那一支真的会解码，见模块文档。
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                image = Image.open(io.BytesIO(data))
                fmt = image.format or ""
                width, height = image.size
                mode = image.mode
                frames = int(getattr(image, "n_frames", 1) or 1)
            except Image.DecompressionBombError as exc:
                # ⚠ 这个**不是** OSError（Pillow 里它直接继承 `Exception`），所以它
                # 需要单独接：漏掉的话它会一路冒出批量层，把整轮索引带下去（`A6`）。
                # 不放开这道闸门还有一个更实的理由：**PNG 那一支是真要解码的**
                # （见模块文档第一段），"自报 1 亿像素"的 PNG 在这里记成失败，
                # 好过让它在 `load()` 里把内存吃满。
                raise _failed("Pillow（自报像素数超过安全上限）", exc) from exc
            except Image.UnidentifiedImageError as exc:
                # 扩展名写着 .jpg，内容却不是图片——最常见的来源是改名。
                raise _failed("Pillow（认不出这是图片）", exc) from exc
            except OSError as exc:
                # 截断到一半、段结构坏掉、缺插件……Pillow 这一侧这些都走 OSError。
                raise _failed("Pillow（图片数据不完整或已损坏）", exc) from exc

            # 元数据这一段在上面那个 try 的外面：它自己的异常翻译在 `_ifd_groups()` 里，
            # 这样"Pillow 读不动"与"我们自己算错了"不会混成同一个失败。
            exif_fields = _ifd_lines(_ifd_groups(image), cap, notes)
            text_fields = _container_lines(image, cap, notes)

        for item in caught:
            message = " ".join(str(item.message).split())
            if message and message not in notes:
                notes.append(f"Pillow 警告：{message}")
        if len(notes) > 3:
            notes = [*notes[:3], f"（另有 {len(notes) - 3} 条警告未列出）"]
        if cap.truncated:
            notes.append(
                f"正文在 {self._max_chars} 字符处截断（上限是 media_meta.py 的 MAX_CHARS）；"
                "截断以整行为界，被丢掉的那一行整条不在正文里"
            )
        if not cap.has_body:
            notes.append(
                "这张图里没有可索引的文字（EXIF 为空、也没有 PNG 文本块 / JPEG 注释）；"
                "格式与尺寸记在 meta 里、不进正文，文件名与路径由扫描器索引"
            )

        return ExtractedDoc(
            text=cap.text,
            title=None,                          # 图片没有"文档标题"，见模块文档
            meta={
                # `core/models.py` 里那个 `meta["kind"]`：图片这一类的类别名。
                # 那里写的是"照片 / 音频 / 视频"的口语说法，代码里取 `image`——
                # 这一类里还有截图、扫描件、生成图，`photo` 会把它们排除在外。
                "kind": "image",
                "format": fmt,
                "width": width,
                "height": height,
                "mode": mode,
                "frames": frames,
                "bytes": len(data),
                # 两个计数：正文短的时候，"是本来就没有、还是我们挑掉了"要能一眼分开
                "exif_fields": exif_fields,
                "text_fields": text_fields,
            },
            modality="text",                     # v1 全是 text，见 core/models.py
            source=self.name,
            truncated=cap.truncated,
            warnings=tuple(notes),
        )


def build(config: ExtractConfig) -> Extractor | None:
    """按 `[extract] media_meta` 决定建不建；关掉时返回 `None`（不注册，见 `plaintext.build`）。"""
    if not config.media_meta:
        return None
    return MediaMetaExtractor(config)
