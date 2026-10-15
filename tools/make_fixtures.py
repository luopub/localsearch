r"""造 `tests/fixtures/` 里的样本（`F3-06`）：每种格式一个最小好样本 + 几个坏样本。

```
uv run python tools/make_fixtures.py            # 写进 tests/fixtures/（先比对再写，报告变化）
uv run python tools/make_fixtures.py --check    # 只比不写；有漂移退出码 1
uv run python tools/make_fixtures.py --list     # 列出清单（名字 / 路径 / 大小）
```

**为什么样本也要有生成器**，而不是直接把文件往仓库里一扔：

- 二进制格式（docx / xlsx / pptx / pdf / 图片）没法用字符串字面量写，只能由库造。
  造样本的那几行代码**总得有个地方放**，放这里比散在测试文件里好查。
- 光有文件没有"它是什么"的说明，下一个人只能靠试。清单（`MANIFEST.toml`）里每
  一条都带 `note`，写的就是"为什么它算最小、它钉的是哪一件事"。
- **逐字节重造得出来**，`--check` 就是这件事的判据。这不只是洁癖：这份清单里的
  `builder` 换一个参数（比如 pdf 少写 `no_new_id=True`）产出的字节就变了，而
  自检比的是"提交上去的字节 = 生成器现在造出来的字节"，所以**变了一定会红**。

⚠ **确定性靠三件事**，任何一件少了 `--check` 就会间歇性红：

1. PDF 必须 `no_new_id=True`——不加的话 MuPDF 每次保存写一个按时间算的 `/ID`；
2. 三个 OOXML 里写"现在"的地方**必须被抹掉**，而**不是**指望库肯听你的。实测
   （`tools/probe_ooxml_time.py`，读数见 `PROGRESS.md` §3 的 `E36`）：`.docx` /
   `.xlsx` / `.pptx` **三个都**把"现在"写进**每个 zip 条目**的时间戳；`.xlsx` 之外
   还多一处——`openpyxl` 在 `save()` 里**无条件**覆写 `docProps/core.xml` 的
   `created` 与 `modified`，先给 `wb.properties.modified` 赋固定值也拦不住；
   `.docx` / `.pptx` 的那两个时刻取自各自的模板，是稳定的。两处都由
   `_normalize_zip()` 统一钉死。
   ⚠ 这一点**跨秒才暴露**：同一个进程里连着造两份，两次落在同一秒，看起来完全
   稳定——第一版就是那样被骗过去的（而探针里那句 `sleep(1.5)` 正是这个结论的全部）；
3. **行尾不许被 git 改写**——`.gitattributes` 里 `tests/fixtures/** -text` 那一行
   就是为这个加的（`.mht` / `.eml` 是文本形态、`binary` 判定未必覆盖得到它们）。

字节一律走 `write_bytes()`（不用 `write_text`）——`newline=None` 会把 `\n` 按平台
落成 CRLF，那正是 `check_mutations.py` 踩过的那一脚（`PROGRESS.md` §4 P3「偏差」
第 20 条）。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import re
import struct
import sys
import tomllib
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
MANIFEST = FIXTURES / "MANIFEST.toml"

#: 五份图片样本共用的元数据。ASCII 是**硬要求**：`Image.Exif.tobytes()` 会把 `str`
#: 类型字段里的非 ASCII 换成 `?`（`E34` 第 5 条实测），所以这里不写中文。
PHOTO_FIELD = "fixture photo"

#: 钉进 OOXML 核心属性的那个时刻（理由见模块文档第 2 条）。
FIXED_MOMENT = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

#: 钉进 zip 条目时间戳的那个时刻（MS-DOS 时代的下限，也是多数打包工具的做法）。
FIXED_STAMP = (1980, 1, 1, 0, 0, 0)


# ==============================================================================
# 造字节
# ==============================================================================


def _image_bytes(fmt: str, **save: Any) -> bytes:
    """8×8 的纯色图 + 一个 `ImageDescription`，按 `fmt` 存成字节。

    `exif` 那个关键字由调用方传（PNG 那份还要带 `pnginfo`），所以这里只负责
    建图与收字节。
    """
    from PIL import Image

    image = Image.new("RGB", (8, 8), (1, 2, 3))
    note = io.BytesIO()
    image.save(note, format=fmt, **save)
    return note.getvalue()


def _exif_bytes() -> bytes:
    """一个只带 `ImageDescription` 的 EXIF 段（`0x010E` = 270）。"""
    from PIL import Image

    exif = Image.Exif()
    exif[270] = PHOTO_FIELD
    return exif.tobytes()


#: `docProps/core.xml` 里那两个时刻元素——`openpyxl` 在 `save()` 里**无条件**把
#: `dcterms:modified` 覆写成"现在"，赋值给 `wb.properties.modified` 也拦不住它，
#: 所以这一类只能落在打包之后改。改成 `W3C_MOMENT` 那个固定串。
_CORE_TIMES = re.compile(
    rb"(<dcterms:(created|modified)\b[^>]*>)([^<]*)(</dcterms:\2>)"
)


def _pin_core_times(data: bytes) -> bytes:
    """把 `docProps/core.xml` 里 `created` / `modified` 的值钉成固定串（没有就不动）。"""
    value = f"{FIXED_MOMENT:%Y-%m-%dT%H:%M:%S}Z".encode("ascii")
    return _CORE_TIMES.sub(lambda match: match.group(1) + value + match.group(4), data)


def _normalize_zip(data: bytes) -> bytes:
    """钉两处时间：**每个条目**的时间戳，以及核心属性里的创建/修改时刻。

    三个 OOXML 库里有写"现在"的（实测：`pptx` 是纯条目时间戳问题；`xlsx` 除此之外
    还往 `docProps/core.xml` 的正文里写）。⚠ 这是**跨进程**才暴露的：同一个进程里连着
    造两份，两次落在同一秒，看起来完全稳定——第一版就是这么被自己骗过去的
    （`PROGRESS.md` §4 P3 的 `F3-06` 记录里记着这条）。
    """
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            entry = zipfile.ZipInfo(info.filename, date_time=FIXED_STAMP)
            entry.compress_type = info.compress_type
            entry.external_attr = info.external_attr
            entry.create_system = info.create_system
            body = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                body = _pin_core_times(body)
            target.writestr(entry, body)
    return out.getvalue()


def plaintext_txt() -> bytes:
    """纯文本：一行 ASCII、一行中文。**超过 32 字节**（理由见清单里的 `note`）。"""
    return (
        "hello, fixture\n"
        "你好，fixture。这一行是为了让字节数超过 SHORT_BYTES 那条线，\n"
        "好让编码那一档走探测器而不是语料先验。\n"
    ).encode("utf-8")


def plaintext_no_ext() -> bytes:
    """没有扩展名的文本（`README` 那一支的兜底）。"""
    return "fixture README\nno extension here\n".encode("utf-8")


def binary_no_ext() -> bytes:
    """没有扩展名、内容是二进制。`looks_binary()` 一看见 NUL 就判它，不再往下解。"""
    return b"\x00\x01\x02 binary \x00 stuff \xff\xfe" + b"\x00" * 8


def office_pdf() -> bytes:
    """一页 PDF。`no_new_id=True` 是确定性的关键（见模块文档）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "fixture pdf text")
    data = doc.tobytes(no_new_id=True)
    doc.close()
    return data


def office_docx() -> bytes:
    """一段话的 `.docx`。

    ⚠ `python-docx` 的三条核心属性**默认就是固定值**（模板里带着的），所以这份样本
    本来跨进程也稳定；这里仍然显式写一遍——**不靠"恰好如此"**（`E26` 那条教训：
    看起来确定的读数未必是测出来的）。
    """
    import docx

    document = docx.Document()
    document.add_paragraph("fixture docx text")
    document.core_properties.created = FIXED_MOMENT
    document.core_properties.modified = FIXED_MOMENT
    note = io.BytesIO()
    document.save(note)
    return _normalize_zip(note.getvalue())


def office_xlsx() -> bytes:
    """一个格子（字符串）的 `.xlsx`。

    `openpyxl` 默认把**当前时刻**写进 `docProps/core.xml` 的 `dcterms:created` /
    `dcterms:modified`（本地时间换算成 UTC），所以这里必须显式钉死，否则同一份输入
    隔一秒跑一次就是另一份字节。
    """
    import openpyxl

    book = openpyxl.Workbook()
    book.active["A1"] = "fixture xlsx text"
    book.properties.created = FIXED_MOMENT
    book.properties.modified = FIXED_MOMENT
    note = io.BytesIO()
    book.save(note)
    return _normalize_zip(note.getvalue())


def office_pptx() -> bytes:
    """一张幻灯片 + 一个文本框的 `.pptx`。

    `python-pptx` 把当前时刻写进三个核心属性（正文里），条目时间戳也是"现在"。
    """
    from pptx import Presentation

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])  # 6 = 空白版式
    frame = slide.shapes.add_textbox(0, 0, 100, 100).text_frame
    frame.text = "fixture pptx text"
    deck.core_properties.created = FIXED_MOMENT
    deck.core_properties.modified = FIXED_MOMENT
    note = io.BytesIO()
    deck.save(note)
    return _normalize_zip(note.getvalue())


def web_html() -> bytes:
    """一个 `<p>` 的 HTML，Doctype 与 `<title>` 都在（相邻形态那一条的样本）。"""
    return (
        "<!DOCTYPE html>\n"
        "<html><head><title>fixture html subject</title></head>"
        "<body><p>fixture html text</p></body></html>\n"
    ).encode("utf-8")


def web_mht() -> bytes:
    """最小的「单个文件网页」：`multipart/related` + 一个 quoted-printable 的 HTML 部分。"""
    import quopri

    boundary = "----=_NextPart_000"
    markup = (
        "<html><head><title>fixture mht subject</title></head>"
        "<body><p>fixture mht text</p></body></html>"
    )
    head = (
        "MIME-Version: 1.0\r\n"
        "Subject: fixture mht subject\r\n"
        f'Content-Type: multipart/related; boundary="{boundary}"\r\n'
        "Content-Location: http://example.com/page.html\r\n"
        "\r\n"
    )
    part = (
        "------=_NextPart_000\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n" + quopri.encodestring(markup.encode("utf-8")).decode("ascii") + "\r\n"
    )
    return (head + part + "------=_NextPart_000--\r\n").encode("ascii")


def web_eml() -> bytes:
    """一封最小邮件：四个头 + 一段 `text/plain`。"""
    import email.policy
    from email.message import EmailMessage

    message = EmailMessage(policy=email.policy.default)
    message["From"] = "fixture sender <from@example.com>"
    message["To"] = "fixture to <to@example.com>"
    message["Subject"] = "fixture eml subject"
    message["Date"] = "Thu, 15 Oct 2026 01:30:00 +0800"
    message.set_content("fixture eml text")
    return message.as_bytes()


# --- .msg：按 MS-OXMSG 拼一份**最小**的（真 OLE 复合文档，不是打桩）-----------

#: `0037` = `PidTagSubject`、`0C1A` = `PidTagSenderName`、`1000` = `PidTagBody`、
#: `001A` = `PidTagMessageClass`。类型码是 `extract_msg.constants.PTYPES` 的十六进制写法。
_PT_STR = "001F"


def _msg_property(pid: str, text: str) -> tuple[str, bytes]:
    """一个字符串属性：值在 `__substg1.0_<id><type>` 流里，UTF-16LE。"""
    return (f"__substg1.0_{pid}{_PT_STR}", text.encode("utf-16-le"))


def web_msg() -> bytes:
    """最小 `.msg`：类别 + 主题 + 发件人 + 正文，就这四条。

    `001A = IPM.Note` **必需**：缺了它 `openMsg` 判"这不是一封 MSG"
    （`InvalidFileFormatError`），而那个报错在附件那一档会退化成"附件读不出来"。

    `__properties_version1.0` 的头部按用途有三种长度（`MESSAGE` 32 字节、
    `MESSAGE_EMBED` 24、`ATTACHMENT` / `RECIPIENT` 8），这里只用第一种。
    """
    from extract_msg.ole_writer import OleWriter

    ole = OleWriter()
    for pid, text in (
        ("001A", "IPM.Note"),
        ("0037", "fixture msg subject"),
        ("0C1A", "fixture sender"),
        ("1000", "fixture msg text"),
    ):
        name, value = _msg_property(pid, text)
        ole.addEntry(name, value)
    head = struct.pack("<8x4I", 1, 1, 1, 1) + b"\x00" * 8
    ole.addEntry("__properties_version1.0", head)

    note = io.BytesIO()
    ole.write(note)
    return note.getvalue()


def image_jpeg() -> bytes:
    return _image_bytes("JPEG", exif=_exif_bytes())


def image_tiff() -> bytes:
    return _image_bytes("TIFF", exif=_exif_bytes())


def image_png() -> bytes:
    """PNG **两个来源都带**：`eXIf` 块（`exif=`）与 `tEXt` 块（`pnginfo=`）。"""
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Description", "fixture png text")
    return _image_bytes("PNG", exif=_exif_bytes(), pnginfo=info)


def image_webp() -> bytes:
    return _image_bytes("WEBP", exif=_exif_bytes())


def image_avif() -> bytes:
    return _image_bytes("AVIF", exif=_exif_bytes())


def broken_pdf() -> bytes:
    """有 `%PDF-` 头、后面是垃圾：**头对、内容坏**那一族（读的时候才炸）。"""
    return b"%PDF-1.7\n" + b"this is not a pdf body at all\n" * 8


def broken_docx() -> bytes:
    """zip 的本地文件头写了一半就断了（`zipfile.BadZipFile` 那一族）。"""
    return b"PK\x03\x04" + b"\x00" * 26


def broken_mht() -> bytes:
    """**没有任何 MIME 头**的 `.mht`——这种格式的合法性就在结构上。"""
    return b"<html><body><p>not a mht at all</p></body></html>\n"


def broken_msg() -> bytes:
    """**不是 OLE2**（`InvalidFileFormatError` 那一族）。"""
    return b"this is not an OLE2 compound document\n" * 4


def broken_jpg() -> bytes:
    """`FFD8` 开头、后面只有半截头——截断的头那一族（光秃秃的 `OSError`）。"""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 10


#: 清单里 `builder` 字段的取值全集。**每条清单记录的 `builder` 都必须在这里**，
#: 多一条少一条都由自检盯着（少一条就是"清单里写了个不存在的东西"）。
BUILDERS: dict[str, Callable[[], bytes]] = {
    "plaintext_txt": plaintext_txt,
    "plaintext_no_ext": plaintext_no_ext,
    "binary_no_ext": binary_no_ext,
    "office_pdf": office_pdf,
    "office_docx": office_docx,
    "office_xlsx": office_xlsx,
    "office_pptx": office_pptx,
    "web_html": web_html,
    "web_mht": web_mht,
    "web_eml": web_eml,
    "web_msg": web_msg,
    "image_jpeg": image_jpeg,
    "image_tiff": image_tiff,
    "image_png": image_png,
    "image_webp": image_webp,
    "image_avif": image_avif,
    "broken_pdf": broken_pdf,
    "broken_docx": broken_docx,
    "broken_mht": broken_mht,
    "broken_msg": broken_msg,
    "broken_jpg": broken_jpg,
}


# ==============================================================================
# 清单
# ==============================================================================


def load_manifest(path: Path = MANIFEST) -> list[dict[str, Any]]:
    """读清单里的 `[[sample]]` 列表（顺序即文件里的顺序）。"""
    with path.open("rb") as handle:
        return tomllib.load(handle)["sample"]


def build_all(samples: list[dict[str, Any]] | None = None) -> list[tuple[Path, bytes]]:
    """按清单造出**全部**字节（不落盘）。顺序与清单一致。"""
    if samples is None:
        samples = load_manifest()
    out: list[tuple[Path, bytes]] = []
    for entry in samples:
        builder = BUILDERS.get(entry["builder"])
        if builder is None:
            raise KeyError(f"清单里的 builder 不存在：{entry['builder']}（{entry['name']}）")
        out.append((FIXTURES / entry["file"], builder()))
    return out


# ==============================================================================
# 写 / 比
# ==============================================================================


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _same(left: bytes, right: bytes) -> str:
    """两份字节比一下，给一行人读的结果（只有大小不同时不必再算摘要）。"""
    if left == right:
        return "一致"
    if len(left) != len(right):
        return f"**不一致**（{len(left)} → {len(right)} 字节）"
    return f"**不一致**（同长，{_digest(left)} → {_digest(right)}）"


def write(samples: list[dict[str, Any]] | None = None) -> int:
    """把生成器的字节写进 `tests/fixtures/`。**返回改动的条数**。"""
    changed = 0
    for path, data in build_all(samples):
        before = path.read_bytes() if path.exists() else None
        if before == data:
            print(f"  一致    {path.relative_to(FIXTURES)}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)          # 字节，不走 write_text（见模块文档）
        note = f"新建（{len(data)} 字节，{_digest(data)}）" if before is None else _same(before, data)
        print(f"  写好了  {path.relative_to(FIXTURES)}  （{note}）")
        changed += 1
    return changed


def stray(samples: list[dict[str, Any]]) -> list[Path]:
    """目录里**有、清单里没有**的文件（清单自己不算）。

    ⚠ 清单是按"它在 `FIXTURES` 里的位置"排除的，不是按 `MANIFEST` 那个全局常量
    比路径：`FIXTURES` 会被指到别处（`test_fixtures.py` 里就指到一棵临时树上跑），
    而两个常量各有各的绝对路径。按常量比的话，那份临时树上的清单会被当成"多出来的"。
    """
    known = {(FIXTURES / entry["file"]).resolve() for entry in samples}
    manifest = (FIXTURES / MANIFEST.name).resolve()
    return sorted(
        path for path in FIXTURES.rglob("*")
        if path.is_file() and path.resolve() not in known | {manifest}
    )


def check(samples: list[dict[str, Any]] | None = None) -> int:
    """只比不写。**返回对不上的条数**（0 就是干净）。

    两个方向都查：清单里有而盘上没有（缺文件）、盘上有而清单里没有（多出来）。
    只管一个方向的话，把手改过的旧文件留在目录里就能让自检保持绿——那是"两份真相"。
    """
    if samples is None:
        samples = load_manifest()
    drifted = 0
    for path, data in build_all(samples):
        relative = path.relative_to(FIXTURES)
        if not path.exists():
            print(f"  **缺文件**  {relative}")
            drifted += 1
            continue
        before = path.read_bytes()
        if before == data:
            print(f"  一致    {relative}  （{len(data)} 字节，{_digest(data)}）")
            continue
        print(f"  **漂了**  {relative}  {_same(before, data)}")
        drifted += 1
    for extra in stray(samples):
        print(f"  **多出来**  {extra.relative_to(FIXTURES)}（清单里没有这一条）")
        drifted += 1
    return drifted


def listing(samples: list[dict[str, Any]] | None = None) -> None:
    """列出清单（名字 / 路径 / 大小 / 好还是坏）。"""
    for entry in samples if samples is not None else load_manifest():
        path = FIXTURES / entry["file"]
        size = f"{path.stat().st_size} 字节" if path.exists() else "**不在盘上**"
        print(f"  {entry['kind']:6s} {entry['name']:28s} {entry['file']:22s} {size}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="造 / 比 tests/fixtures/ 里的样本")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只比不写；有漂移退出码 1")
    group.add_argument("--list", action="store_true", help="列出清单")
    args = parser.parse_args(argv)

    samples = load_manifest()
    print(f"清单 {MANIFEST.relative_to(REPO_ROOT)}：{len(samples)} 条")

    if args.list:
        listing(samples)
        return 0
    if args.check:
        drifted = check(samples)
        print(f"漂移 {drifted} 条" + ("（干净）" if drifted == 0 else "——生成器与盘上的字节对不上"))
        return 1 if drifted else 0

    changed = write(samples)
    print(f"改动了 {changed} 条" + ("（本来就是这一份）" if changed == 0 else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())
