r"""`extract/web_mail.py`：网页与邮件的正文、标记、深度、失败（`F3-03`）。

------------------------------------------------------------------------------
这一组盯的五件事
------------------------------------------------------------------------------

1. **认领的边界**——七个扩展名认领（`html` / `htm` / `xhtml` / `mht` / `mhtml` /
   `eml` / `msg`），其余一个不认；`supports()` 不看 MIME。另有两条是"接进注册表"
   之后的整体行为：`.html` 归 `web_mail`（**不能**被 `plaintext` 抢走），绕过
   `supports()` 直接 `extract()` 一个别的扩展名要照实报 `UnsupportedFormat`。
2. **HTML 摊成行的那三条规则**——块级各自成行、行内的接在当前行上、`<pre>` 的换行
   是内容。⚠ 这一组里有两条**回归线**（`2026-10-16` 写探针时各抓到一次，两处都属于
   "看起来是绿的"那一档）：`<p>a <b>粗</b> 与 <a>链</a></p>` 必须收成**一行且顺序
   不乱**，`<html>` / `<body>` 这种容器**不能**让整份文档塌成一行。后者来自
   `meta["lines"]`——那是"为什么这份网页只有一行"的第一个线索。
3. **邮件怎么选正文、附件怎么算**——`text/plain` 优先于 `text/html`；有 `filename`
   或 `Content-Disposition: attachment` 的部分是**附件**，哪怕它的类型是 `text/plain`
   （把 `.txt` 附件当正文是这类提取器最容易犯的错）；附件名进正文，附件**正文不进**，
   而且要出声。另外 `email_attachment_depth` 的 0 / 1 两档各一条。
4. **失败一律是 `ParseFailed`，且带上是哪个库报的**——`.eml` / `.mht` 里一个 MIME 头
   都没有（扩展名写反了）要拦下来，不能把乱码当正文；`.msg` 有两族异常
   （`ExMsgBaseException` 与 `olefile` 的 `OSError`），**截断到一半的那一份**是第二族
   的回归线：少接一族，坏文件会变成未捕获异常、把整轮索引带下去（`A6` 的反面）。
5. **共用件**——上限到了**整行地停**并留警告（`D8` 的 200 片上限要能说清"正文到
   哪里为止"）、`meta` 里那三个数、以及"正文是空的"那条警告（`has_body` 不把标记行
   算数）。

⚠ **`.msg` 的样本是真写的**（`extract_msg.ole_writer.OleWriter` 造一个真的 OLE 复合
文档，不是打桩）：`_msg()` 那一段按 MS-OXMSG 拼 `__properties_version1.0` 的记录与
`__substg1.0_*` 流。所以"库读不读得出来"这一层**不**自洽——有一条测试专门把造出来的
字节喂回 `extract_msg.Message` 验一遍，样本本身写歪了会在那里红，而不是伪装成提取器的
bug。其余格式的样本用标准库直接写（`email` / 字符串字面量），它们没有造样本这一层。

⚠ 与 `office.py` 那一组同一条已知边界：`.eml` / `.mht` 的样本**不跨库版本**。
独立样本（落盘就不再变）是 `F3-06` 的 fixtures 要补的。

`tmp_path` 落在 `C:` 下——项目只允许碰 `C:` 与 `D:`。
"""

from __future__ import annotations

import base64
import datetime
import email.policy
import io
import quopri
import struct
from email.header import Header
from email.message import EmailMessage
from typing import Any

import pytest

from localsearch.extract import build_registry
from localsearch.extract.base import (
    ExtractConfig,
    FileUnreadable,
    ParseFailed,
    UnsupportedFormat,
    extract_file,
)
from localsearch.extract.web_mail import (
    HTML_EXTS,
    MAIL_EXTS,
    MAX_CHARS,
    MHT_EXTS,
    WEB_MAIL_EXTS,
    WebMailExtractor,
    build,
)

# ==============================================================================
# 造样本
# ==============================================================================


def write(tmp_path: Any, name: str, data: bytes) -> str:
    target = tmp_path / name
    target.write_bytes(data)
    return str(target)


@pytest.fixture
def extractor():
    return WebMailExtractor(ExtractConfig())


def page(tag: str = "html", body: str = "<p>正文</p>", title: str | None = None) -> bytes:
    """一份最小的 HTML 字节（`tag` 用来写 `xhtml` 那张 XML 声明）。"""
    head = "" if title is None else f"<title>{title}</title>"
    decl = '<?xml version="1.0" encoding="UTF-8"?>\n' if tag == "xhtml" else "<!DOCTYPE html>\n"
    return f'{decl}<html><head>{head}</head><body>{body}</body></html>'.encode("utf-8")


def utf8(markup: str) -> bytes:
    """一份 HTML 的 UTF-8 字节。

    ⚠ 正文里的字都是中文，而 `b"…"` 字面量**只能是 ASCII**（写中文是 `SyntaxError`），
    所以凡是要字节的地方都过这一下——不然每次都得回头改一遍（`2026-10-16` 改了四处）。
    """
    return markup.encode("utf-8")


def eml(**kwargs: Any) -> bytes:
    """一封 `EmailMessage` 序列化出来的 `.eml`（正文默认纯文本）。"""
    message = EmailMessage(policy=email.policy.default)
    message["From"] = kwargs.pop("sender", "张三 <zs@example.com>")
    message["To"] = kwargs.pop("to", "李四 <ls@example.com>")
    for header, value in kwargs.pop("headers", {}).items():
        message[header] = value
    message.set_content(kwargs.pop("body", "纯文本正文"))
    for markup in kwargs.pop("html", ()):
        message.add_alternative(markup, subtype="html")
    for part in kwargs.pop("attach", ()):
        message.add_attachment(part)
    for data, name in kwargs.pop("files", ()):
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    assert not kwargs, kwargs
    return message.as_bytes()


def mht(
    *,
    html: bytes | None = None,
    extra_html: int = 0,
    plain: str | None = None,
    image: bool = True,
) -> bytes:
    """手写一个 `multipart/related` 的 `.mht`（正文走 quoted-printable、资源走 base64）。

    `html=None` 就是"没有 HTML 部分"（那条回落分支的样本），`plain` 与 `image` 各自
    控制另外两类部分在不在。
    """
    boundary = "----=_NextPart_000"
    second = "<html><body><p>第二个 HTML 部分</p></body></html>".encode("utf-8")
    markups = [] if html is None else [html] + [second] * extra_html
    head = (
        "MIME-Version: 1.0\r\n"
        "Subject: =?utf-8?B?5Y+m5a2Y5Li655qE572R6aG1?=\r\n"
        f'Content-Type: multipart/related; boundary="{boundary}"\r\n'
        "Content-Location: http://example.com/page.html\r\n"
        "\r\n"
    )
    body = ""
    for markup in markups:
        body += (
            "------=_NextPart_000\r\n"
            'Content-Type: text/html; charset="utf-8"\r\n'
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "\r\n" + quopri.encodestring(markup).decode("ascii") + "\r\n"
        )
    if plain is not None:
        body += (
            "------=_NextPart_000\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "\r\n" + quopri.encodestring(plain.encode("utf-8")).decode("ascii") + "\r\n"
        )
    if image:
        body += (
            "------=_NextPart_000\r\n"
            "Content-Type: image/png\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "Content-Location: http://example.com/logo.png\r\n"
            "\r\n"
            + base64.encodebytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode("ascii")
        )
    return (head + body + "------=_NextPart_000--\r\n").encode("ascii")


# --- .msg：按 MS-OXMSG 真拼一份出来（不是打桩）-------------------------------

#: 属性的类型码（`extract_msg.constants.PTYPES` 的键的十六进制写法）。
PT_I32 = "0003"
PT_STR = "001F"
PT_TIME = "0040"


def _rec(pid: str, ptype: str, value: bytes) -> bytes:
    """一条 16 字节的**定长**属性记录：type(2) + id(2) + flags(4) + 值(8)。

    值必须正好 8 字节（短的类型后面补 0）——这是 `ST_PROP_BASE` 那个 `<2HI`
    加上 8 字节数据段定的，写少了整个属性流的解析会从这一条开始错位。
    """
    return struct.pack("<HHI", int(ptype, 16), int(pid, 16), 0) + value


def _i32(pid: str, value: int) -> bytes:
    return _rec(pid, PT_I32, struct.pack("<I", value) + b"\x00" * 4)


def _filetime(text: str) -> bytes:
    """`YYYY-MM-DD HH:MM:SS`（UTC）→ FILETIME（100ns 一格，起于 1601-01-01）。"""
    moment = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc
    )
    epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    return struct.pack("<Q", int((moment - epoch).total_seconds() * 10_000_000))


def _store(records: bytes, kind: str) -> bytes:
    r"""`__properties_version1.0` 的字节。

    头部的长度**按用途分三种**（`properties_store.py` 里那个 `skip`）：
    `MESSAGE` 32 字节（24 字节头 + 8 字节保留）、`MESSAGE_EMBED` 24 字节、
    `ATTACHMENT` / `RECIPIENT` 8 字节。写错一种，后面整片属性都按错位读。
    """
    if kind == "message":
        head = struct.pack("<8x4I", 1, 1, 1, 1) + b"\x00" * 8
    elif kind == "embed":
        head = struct.pack("<8x4I", 0, 0, 0, 0)
    else:
        head = b"\x00" * 8
    return head + records


def _stream(ole: Any, base: str, pid: str, text: str) -> None:
    """一个字符串属性：值在 `__substg1.0_<id><type>` 流里，UTF-16LE。"""
    ole.addEntry(f"{base}/__substg1.0_{pid}{PT_STR}", text.encode("utf-16-le"))


def _add_message(
    ole: Any,
    base: str,
    *,
    subject: str,
    sender: str,
    body: str | None = None,
    html: bytes | None = None,
    date: str | None = None,
    kind: str = "message",
) -> None:
    """往 `base` 下写一封邮件。`001A` = `IPM.Note` 是**必须**的：`.

    `openMsg` 拿不到类别的文件会判"这不是一封 MSG"（`InvalidFileFormatError`），
    而那个报错在 `ATTACH_SUPPRESS_ALL` 下会退化成"附件读不出来"，很难查。
    """
    _stream(ole, base, "001A", "IPM.Note")
    _stream(ole, base, "0037", subject)
    _stream(ole, base, "0C1A", sender)
    if body is not None:
        _stream(ole, base, "1000", body)
    if html is not None:
        ole.addEntry(f"{base}/__substg1.0_10130102", html)
    records = _rec("0039", PT_TIME, _filetime(date)) if date else b""
    ole.addEntry(f"{base}/__properties_version1.0", _store(records, kind))


def msg(
    *,
    subject: str = "季度汇报",
    sender: str = "张三",
    to: str = "李四",
    body: str | None = "正文里有个独特的词：企鹅。",
    html: bytes | None = None,
    date: str | None = "2026-10-15 01:30:00",
    file_attachment: bool = True,
    embedded: str | None = "内层正文：海绵宝宝。",
) -> bytes:
    """一份真的 `.msg` 字节（OLE 复合文档）。"""
    from extract_msg.ole_writer import OleWriter

    ole = OleWriter()
    _add_message(ole, "", subject=subject, sender=sender, body=body, html=html, date=date)

    if to:
        rcpt = "__recip_version1.0_#00000000"
        ole.addEntry(rcpt, storage=True)
        _stream(ole, rcpt, "3001", to)
        _stream(ole, rcpt, "39FE", "ls@example.com")
        ole.addEntry(f"{rcpt}/__properties_version1.0", _store(_i32("0C15", 1), "other"))

    if file_attachment:
        att = "__attach_version1.0_#00000000"
        ole.addEntry(att, storage=True)
        _stream(ole, att, "3707", "图表.png")
        _stream(ole, att, "3704", "图表.png")
        ole.addEntry(f"{att}/__substg1.0_37010102", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        ole.addEntry(
            f"{att}/__properties_version1.0", _store(_i32("3705", 1) + _i32("0E20", 48), "other")
        )

    if embedded is not None:
        att = "__attach_version1.0_#00000001"
        ole.addEntry(att, storage=True)
        _stream(ole, att, "3707", "转发的邮件")
        inner = f"{att}/__substg1.0_3701000D"
        ole.addEntry(inner, storage=True)
        _add_message(ole, inner, subject="内层主题", sender="赵六", body=embedded, kind="embed")
        ole.addEntry(f"{att}/__properties_version1.0", _store(_i32("3705", 5), "other"))

    buf = io.BytesIO()
    ole.write(buf)
    return buf.getvalue()


def msg_bytes(**kwargs: Any) -> bytes:
    """`msg()` 的调用点都用这个名字（读起来与上面几个造样本的函数一致）。"""
    return msg(**kwargs)# ==============================================================================
# 1. 认领的边界、开关、接进注册表
# ==============================================================================


def test_it_claims_exactly_the_seven_extensions():
    assert WEB_MAIL_EXTS == {
        "html", "htm", "xhtml", "mht", "mhtml", "eml", "msg",
    }


def test_the_three_name_lists_add_up_to_the_whole():
    """三组名单互不相交、加起来正好是全部——分组是"走法"的说法，不是三个提取器。"""
    assert HTML_EXTS | MHT_EXTS | MAIL_EXTS == WEB_MAIL_EXTS
    assert not (HTML_EXTS & MHT_EXTS) and not (HTML_EXTS & MAIL_EXTS)
    assert not (MHT_EXTS & MAIL_EXTS)


def test_supports_looks_only_at_the_extension():
    """`mime` 一个字符不用：同一个 `.html` 文件的 MIME 可以报 `text/plain`。

    反过来也认——"扩展名不是我的，但 MIME 说是 HTML"**不进**这个提取器：那会让
    一个 `.dat` 文件被当网页解析，而这类判定不归提取器管（`base.mime_of()` 是
    次要信号，只在少数地方用）。
    """
    instance = WebMailExtractor(ExtractConfig())

    assert instance.supports("html", "text/plain")
    assert instance.supports("msg", "application/octet-stream")
    assert not instance.supports("pdf", "text/html")
    assert not instance.supports("txt", "text/html")
    assert not instance.supports("", "text/html")


def test_build_follows_the_switch():
    assert build(ExtractConfig(web_mail=True)) is not None
    assert build(ExtractConfig(web_mail=False)) is None


def test_extract_refuses_an_extension_it_never_claimed(tmp_path, extractor):
    """绕过 `supports()` 直接调 `extract()`：**照实说**，不许返回空正文。

    "注册了但返回空"是最难查的一类失败——下游分不出它和"这个文件本来就是空的"。
    这里用的是 `.mht` 的字节配 `.zip` 的名字：**字节是对的**，所以红的只可能是
    扩展名那一关。
    """
    path = write(tmp_path, "a.zip", mht(html=b"<p>x</p>"))

    with pytest.raises(UnsupportedFormat) as excinfo:
        extractor.extract(path)

    assert "zip" in str(excinfo.value)


def test_the_registry_routes_html_to_web_mail(tmp_path):
    """接进注册表之后的整体行为：`.html` 归 `web_mail`，**不能**被 `plaintext` 抢走。

    `plaintext` 排在前面（`build_registry()` 的顺序），它一旦把 `.html` 收进名单，
    标签就会变成正文——而那条路**不报错**。地址是对的这件事只能由这一条看着。
    """
    registry = build_registry(ExtractConfig())
    path = write(tmp_path, "a.html", page(body="<p>季度汇报</p>", title="标题"))

    doc = extract_file(path, registry=registry, config=ExtractConfig())

    assert doc.source == "web_mail"
    assert doc.text == "季度汇报"


def test_the_registry_does_not_claim_what_nobody_owns_yet(tmp_path):
    """老式 office 格式与音频明确不做——这些一律"没人认领"。

    这不是本模块的职责，但它与上面的路由测试是一对：**认领地收窄**这件事得有反面
    样本，否则"`WEB_MAIL_EXTS` 写宽了"不会在那里红（写宽了会被这里的循环抓到，
    因为那时 `extract()` 会照实报 `UnsupportedFormat`——只是从 `first_match` 那层
    过不去，症状是 `find_extractor` 返回了 `web_mail`）。

    ⚠ 这里原先还有 `a.jpg`，`F3-04` 落地时**必须**把它删掉——`media_meta` 认领了
    它，同一个样本的结局从 `UnsupportedFormat` 变成了 `ParseFailed`（内容是
    `b"nothing"`，认得出这是图片类的扩展名但认不出图片）。这条改动正是"绿灯不自动
    续期"说的那种：老测试不是坏了，是**它盯的那个事实变了**。
    图片那一侧的边界现在归 `test_extract_media_meta.py` 的
    `test_nothing_outside_the_list_is_claimed` 与 `test_the_unclaimed_image_formats_fall_to_unsupported`
    （`heic` / 相机 RAW / `gif` / `bmp` / `psd` 一族在那儿逐条钉着）。
    """
    registry = build_registry(ExtractConfig())

    for name in ("a.mp3", "a.doc", "a.xls"):
        path = write(tmp_path, name, b"nothing")
        with pytest.raises(UnsupportedFormat):
            extract_file(path, registry=registry, config=ExtractConfig())


# ==============================================================================
# 2. HTML
# ==============================================================================


def test_block_elements_become_lines_and_inline_ones_join_the_current_line(tmp_path, extractor):
    """⚠ 两条回归线挤在一条断言里，它们各自都被写错过一次（`2026-10-16`）。

    红色会怎么出现：`text` 里出现 `\\n` 的数量不对（塌成一行 / 行内元素各自成行），
    或者那几个字的**顺序**变成 `第一节 / 加粗 / 链接 / 第一段里有 …`——那是"递归进
    行内元素时用了自己的累积器"的症状，第一版就是这样。
    """
    markup = (
        "<html><head><title>标题</title></head><body>"
        "<h1>第一节</h1>"
        "<p>第一段里有 <b>加粗</b> 与 <a href=\"/x\">链接</a>，都在同一行。</p>"
        "<div>第二段</div>"
        "</body></html>"
    )
    path = write(tmp_path, "a.html", markup.encode("utf-8"))

    doc = extractor.extract(path)

    assert doc.text == (
        "第一节\n"
        "第一段里有 加粗 与 链接，都在同一行。\n"
        "第二段"
    )
    assert doc.meta["lines"] == 3


def test_a_container_element_does_not_flatten_the_page_into_one_line(tmp_path, extractor):
    """`<html>` / `<body>` 这些容器**不**成行，但它们的子节点要照常摊开。

    第一版把非块级元素一律 `get_text()` 一下接上来，于是整份文档被 `<html>` 那一次
    摊成**一行**：`meta["lines"]` 是 1，而切片是按行切的（`D8` 那 200 片的算术前提
    就是行）。
    """
    path = write(tmp_path, "a.html", page(body="<p>甲</p><p>乙</p><p>丙</p>"))

    doc = extractor.extract(path)

    assert doc.text == "甲\n乙\n丙"
    assert doc.meta["lines"] == 3


def test_the_title_goes_to_title_and_not_to_the_text(tmp_path, extractor):
    """`<title>` 进 `ExtractedDoc.title`，**不进正文**（与 docx 的核心属性同一个口径）。

    不进正文这一半是有代价的（`title` 会不会进 FTS 要到 `P4` 才定），所以它是一条
    会被"顺手加进去"的决定——测试把它钉住。
    """
    path = write(tmp_path, "a.html", page(body="<p>正文</p>", title="  会议   纪要  "))

    doc = extractor.extract(path)

    assert doc.title == "会议 纪要"
    assert doc.text == "正文"
    assert "会议" not in doc.text


def test_an_html_without_a_title_has_no_title(tmp_path, extractor):
    """没有 `<title>` 就是 `None`，**不是文件名**（`core/models.py` 写了理由）。"""
    doc = extractor.extract(write(tmp_path, "a.html", page(title=None)))

    assert doc.title is None


def test_script_and_style_are_dropped_whole(tmp_path, extractor):
    markup = (
        "<html><head><title>标题</title>"
        "<style>body { color: #咖啡色; }</style></head><body>"
        '<script>var 变量 = "脚本里的字";</script>'
        "<p>正文</p></body></html>"
    )
    doc = extractor.extract(write(tmp_path, "a.html", markup.encode("utf-8")))

    assert doc.text == "正文"
    assert "脚本里的字" not in doc.text
    assert "咖啡色" not in doc.text


def test_noscript_is_dropped_too(tmp_path, extractor):
    """丢 `noscript` 是**有代价**的：JS 重的站点把正文放在那儿，那种页面会收不到字。

    代价写在模块文档里，这里把行为钉住——真要改这条得先改文档，不能顺手放开。
    """
    markup = (
        "<html><body><p>正文</p>"
        '<noscript>&lt;img src="x.png"&gt; 给没有脚本的浏览器的字</noscript>'
        "</body></html>"
    )
    doc = extractor.extract(write(tmp_path, "a.html", markup.encode("utf-8")))

    assert doc.text == "正文"


def test_doctype_and_the_xml_declaration_are_not_text(tmp_path, extractor):
    """`<!DOCTYPE html>` 与 `<?xml …?>` 在 BeautifulSoup 里**是文本节点**。

    不显式排掉的话 `html` 会变成正文的第一行——`xhtml` 文件大多带 XML 声明，
    所以这不是洁癖。
    """
    for tag in ("html", "htm", "xhtml"):
        path = write(tmp_path, f"a.{tag}", page(tag=tag, body="<p>正文</p>"))

        doc = extractor.extract(path)

        assert doc.text == "正文", tag


def test_pre_keeps_its_line_breaks(tmp_path, extractor):
    """`<pre>` 里的换行是**内容**（代码、ASCII 图），不压平。"""
    markup = "<html><body><pre>def f():\n    return 42\n\n\n尾</pre></body></html>"

    doc = extractor.extract(write(tmp_path, "a.html", markup.encode("utf-8")))

    assert doc.text == "def f():\n    return 42\n尾"


def test_entities_and_whitespace_are_normalized(tmp_path, extractor):
    """实体照常解开，行内的连续空白压成一个空格——**只压行内**，不跨行。"""
    markup = (
        "<html><body><p>甲 &amp; 乙&nbsp;丙 &#x4E2D;</p>"
        "<p>多   个     空格</p></body></html>"
    )
    doc = extractor.extract(write(tmp_path, "a.html", markup.encode("utf-8")))

    assert doc.text == "甲 & 乙 丙 中\n多 个 空格"


def test_a_gbk_page_without_a_bom_is_decoded_and_the_guess_is_recorded(tmp_path, extractor):
    """GBK 网页（没有 BOM、没有 `<meta charset>`）由 `base.decode_bytes` 那一套猜出来。

    两件事一起钉：**字是对的**（不是乱码），而且**猜这件事留了痕**——乱码是
    "看不出来哪里错了"的典型，没有这条痕迹就只能靠猜。⚠ 第二件是这一条的要害：
    只断言第一件的话，把警告删掉它照样绿。
    """
    markup = f"<html><body><p>{'中文正文内容' * 20}</p></body></html>".encode("gbk")

    doc = extractor.extract(write(tmp_path, "a.html", markup))

    assert "中文正文内容" in doc.text
    assert any("猜" in warning for warning in doc.warnings)


def test_an_html_with_only_scripts_is_an_empty_doc_with_a_warning(tmp_path, extractor):
    """正文一个字都没有时要出声——标记行不算正文（`_Text.has_body`）。

    这里恰好没有标记行，所以这条测的是"空正文警告"本身；`.eml` 那条（只有邮件头的
    邮件）测的是"标记行不算正文"那一半。
    """
    path = write(tmp_path, "a.html", b"<html><body><script>x=1</script></body></html>")

    doc = extractor.extract(path)

    assert doc.text == ""
    assert doc.is_empty
    assert any("没有可提取的文字" in warning for warning in doc.warnings)


def test_a_zero_byte_html_says_zero_bytes(tmp_path, extractor):
    """0 字节与"有内容但都不是正文"是两件事，警告不一样。

    （`.eml` / `.msg` 的 0 字节走 `ParseFailed`：那两种格式的"合法性"就在结构上。
    这条口径写在模块文档里。）
    """
    doc = extractor.extract(write(tmp_path, "a.html", b""))

    assert doc.text == ""
    assert doc.warnings == ("文件是 0 字节，没有任何内容可提取",)


# ==============================================================================
# 3. mht
# ==============================================================================


def test_mht_takes_the_html_part_and_decodes_both_transfer_encodings(tmp_path, extractor):
    """正文在 `text/html` 部分里，而且是 quoted-printable 编过的。

    同一个文件里的图片走 base64——它**不是**正文（收进来会往索引里灌几万字符的
    二进制编码）。两个部分共用一条造样本的路径，所以这条同时是"别把资源当正文"。
    """
    markup = "<html><head><title>快照标题</title></head><body><p>快照里的字：蓝鲸。</p></body></html>"
    path = write(tmp_path, "a.mht", mht(html=markup.encode("utf-8")))

    doc = extractor.extract(path)

    assert doc.text == "快照里的字：蓝鲸。"
    assert doc.title == "快照标题"
    assert doc.meta["html_parts"] == 1
    assert "PNG" not in doc.text
    assert "iVBOR" not in doc.text


def test_mht_title_falls_back_to_the_subject_header(tmp_path, extractor):
    """HTML 自己没有 `<title>` 时用头里的 `Subject`。

    "另存为单个文件网页"就是那么写的（头里的 Subject = 网页标题）。**不编造**：
    这是文件里真的写着的一行。
    """
    path = write(tmp_path, "a.mht", mht(html=utf8("<html><body><p>正文</p></body></html>")))

    doc = extractor.extract(path)

    assert doc.title == "另存为的网页"


def test_mht_without_html_uses_the_plain_part_and_says_so(tmp_path, extractor):
    """没有 HTML 部分但有 `text/plain`：用它，并出声。

    判失败更"严格"，但把手上真有的字丢掉的代价更大——所以这里收，同时说清这不是
    正常形态。
    """
    path = write(tmp_path, "a.mht", mht(plain="只有纯文本的那一份"))

    doc = extractor.extract(path)

    assert doc.text == "只有纯文本的那一份"
    assert any("不是正常形态" in warning for warning in doc.warnings)


def test_mht_with_two_html_parts_keeps_the_first_and_says_so(tmp_path, extractor):
    path = write(
        tmp_path,
        "a.mht",
        mht(html=utf8("<html><body><p>第一个</p></body></html>"), extra_html=1),
    )

    doc = extractor.extract(path)

    assert doc.text == "第一个"
    assert doc.meta["html_parts"] == 2
    assert any("只收了第一个" in warning for warning in doc.warnings)


def test_mht_with_neither_html_nor_plain_is_a_ParseFailed(tmp_path, extractor):
    """只有图片的 mht：MIME 头合法、但一个能当正文的部分都没有。"""
    raw = mht(html=b"<p>x</p>").replace(b"text/html", b"image/gif", 1)

    with pytest.raises(ParseFailed) as excinfo:
        extractor.extract(write(tmp_path, "a.mht", raw))

    assert "HTML" in str(excinfo.value)


@pytest.mark.parametrize("name", ["a.mht", "a.mhtml"])
def test_bytes_that_are_not_a_mime_container_are_a_ParseFailed(tmp_path, extractor, name):
    """扩展名写反了（这里是一张 JPEG 的字节）：**拦下来**。

    `email` 模块对任何字节都能解出一个 `Message`（所有头都是空的），随后
    `walk()` 会把那坨字节当 `text/plain`——不设这一关，一张图的乱码会**静默**变成
    正文，而且"解析成功"。
    """
    jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4

    with pytest.raises(ParseFailed) as excinfo:
        extractor.extract(write(tmp_path, name, jpeg))

    assert "MIME" in str(excinfo.value)


# ==============================================================================
# 4. eml
# ==============================================================================


def test_eml_headers_become_marker_lines(tmp_path, extractor):
    """邮件头进正文（标记行），`Subject` 同时进 `title`。

    重复是有意的：对一封邮件来说主题是**内容**（任何客户端都把它排在正文上方），
    而 `title` 会不会进 FTS 要到 `P4` 才定。代价（将来 BM25 会把主题算两次）记在
    `PROGRESS.md` 的遗留里。
    """
    data = eml(
        body="正文",
        headers={
            "Cc": "王五 <ww@example.com>",
            "Date": "Wed, 15 Oct 2026 09:30:00 +0800",
            "Subject": "季度汇报",
        },
    )
    doc = extractor.extract(write(tmp_path, "a.eml", data))

    assert doc.text == (
        "【邮件】发件人：张三 <zs@example.com>\n"
        "【邮件】收件人：李四 <ls@example.com>\n"
        "【邮件】抄送：王五 <ww@example.com>\n"
        "【邮件】时间：2026-10-15 09:30:00+08:00\n"
        "【邮件】主题：季度汇报\n"
        "正文"
    )
    assert doc.title == "季度汇报"
    assert doc.meta["attachments"] == 0


def test_a_folded_subject_becomes_one_line(tmp_path, extractor):
    """邮件头可以**折行**（续行以空白开头），折行原样留下会把标记行断成两行。

    `title` 更经不起这个：它会带着换行进嵌入前缀（`title: {t} | text: …`）。
    RFC 2047 那种编码头另有一条（`mht` 那一组的 Subject 就是编码过的）。
    """
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: a long subject that the writer folded\r\n"
        b" over two lines\r\n"
        b"\r\n"
        b"body\r\n"
    )

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert doc.title == "a long subject that the writer folded over two lines"
    assert "\n" not in doc.title
    assert "【邮件】主题：a long subject that the writer folded over two lines" in doc.text.splitlines()


def test_rfc2047_encoded_headers_are_decoded(tmp_path, extractor):
    """`=?utf-8?B?…?=` 那一种是编码过的头，要解开（不然标题是一串 base64）。"""
    encoded = Header("季度汇报", "utf-8").encode()

    doc = extractor.extract(write(tmp_path, "a.eml", eml(headers={"Subject": encoded})))

    assert doc.title == "季度汇报"


def test_unparseable_dates_are_kept_as_they_were(tmp_path, extractor):
    """头里的时间是**文本**，格式自由。能解就归一，解不动就原样留着——不编造。"""
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: s\r\n"
        b"Date: sometime last week\r\n"
        b"\r\n"
        b"body\r\n"
    )

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert "【邮件】时间：sometime last week" in doc.text


def test_eml_prefers_the_plain_part_over_the_html_one(tmp_path, extractor):
    """`multipart/alternative` 里两份正文都有：用 `text/plain` 那一份。

    顺序不能随解析顺序变——两份的字不一样，抓住哪份算哪份会让结果看起来"随机"。
    """
    data = eml(body="纯文本那一份的字", html=["<html><body><p>HTML 那一份的字</p></body></html>"])

    doc = extractor.extract(write(tmp_path, "a.eml", data))

    assert doc.text.endswith("纯文本那一份的字")
    assert "HTML 那一份的字" not in doc.text


def test_eml_falls_back_to_the_html_part_and_strips_it(tmp_path, extractor):
    """只有 HTML 的邮件（Outlook 也存这种）：剥成文本，标签不进正文。"""
    message = EmailMessage(policy=email.policy.default)
    message["From"] = "a@example.com"
    message["Subject"] = "只有 HTML"
    message.set_content("<html><body><p>只有 HTML 的那一份</p><script>x=1</script></body></html>",
                        subtype="html")

    doc = extractor.extract(write(tmp_path, "a.eml", message.as_bytes()))

    assert doc.text.endswith("只有 HTML 的那一份")
    assert "script" not in doc.text


def test_a_text_attachment_is_not_the_body(tmp_path, extractor):
    """⚠ `.txt` 附件不是正文——这类提取器最容易犯的错。

    这个文件里**只有一个** `text/plain` 部分，而它是附件。按"找第一个 text/plain"
    写的实现会把这个附件的内容当成收件人的正文：症状是正文完全不对、却看不出哪里错了。
    """
    data = eml(
        body="真正的正文",
        files=[("附件的字：河马。".encode("utf-8"), "备注.txt")],
    )

    doc = extractor.extract(write(tmp_path, "a.eml", data))

    assert "真正的正文" in doc.text
    assert "河马" not in doc.text
    assert "【附件】备注.txt" in doc.text


def test_attachment_names_are_listed_and_the_missing_bodies_are_announced(tmp_path, extractor):
    """附件**只收名字**，所以必须出声：用户会以为附件里的字也搜得到。

    那条警告要能读出两件事——有几个、叫什么。
    """
    data = eml(
        body="正文",
        files=[
            (b"PDF-ish", "报告.pdf"),
            (b"XLSX-ish", "表格.xlsx"),
            (b"TXT-ish", "备注.txt"),
            (b"FOURTH", "第四个.bin"),
        ],
    )

    doc = extractor.extract(write(tmp_path, "a.eml", data))

    assert doc.meta["attachments"] == 4
    listed = [line for line in doc.text.splitlines() if line.startswith("【附件】")]
    assert len(listed) == 4
    warning = next(w for w in doc.warnings if "没有收录" in w)
    assert "4 个附件" in warning
    assert "报告.pdf" in warning and "等 4 个" in warning


def test_the_declared_charset_beats_guessing(tmp_path, extractor):
    """MIME 部分**声明**了编码，那是文件自己给的硬证据，比 `base` 那五档的猜准。

    这几个字节按"猜"的路径会读成 cp1250 一类的乱码（实测过），按声明解才是 `café`。
    所以这条红的方式很具体：正文里出现乱码而不是 `café résumé`。
    """
    raw = (
        "From: a@example.com\r\n"
        "Subject: latin1\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="iso-8859-1"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        "caf\xe9 r\xe9sum\xe9\r\n"
    ).encode("iso-8859-1")

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert doc.text.endswith("café résumé")


def test_a_nested_message_is_inlined_at_depth_1(tmp_path, extractor):
    """`message/rfc822` 那一层在默认深度（1）下展开：它的邮件头与正文都进来。

    ⚠ 判 `message/rfc822` 必须**排在** `is_multipart()` **前面**：按后者它也是
    "多部分"，先判它才会走"这是一封附件邮件"那一支；判反了，转发件的正文会变成
    收件人的正文，而两者的区别在正文里**看不出来**（都长着邮件头的样子）。
    """
    inner = EmailMessage(policy=email.policy.default)
    inner["From"] = "赵六 <zl@example.com>"
    inner["Subject"] = "转发件：内层主题"
    inner.set_content("内层正文：海绵宝宝。")
    data = eml(
        body="外层正文",
        headers={"Subject": "外层主题"},
        attach=[inner],
    )

    doc = extractor.extract(write(tmp_path, "a.eml", data))

    assert doc.title == "外层主题"
    assert doc.meta["inlined_mails"] == 1
    assert "内层正文：海绵宝宝。" in doc.text
    assert "【邮件】主题：转发件：内层主题" in doc.text
    # 内层那封的头排在外层正文的**后面**（阅读顺序），不是插到前面
    assert doc.text.index("外层正文") < doc.text.index("内层正文")


def test_a_nested_message_is_not_inlined_at_depth_0(tmp_path, extractor):
    """深度 0：只列名字，且警告里要带着**当前的配置值**。

    "配置的锅"与"v1 不做的锅"对用户是两件事：前者改一行 TOML 就好，后者要等 `P4`。
    """
    inner = EmailMessage(policy=email.policy.default)
    inner["From"] = "赵六 <zl@example.com>"
    inner["Subject"] = "内层主题"
    inner.set_content("内层正文：海绵宝宝。")
    data = eml(attach=[inner])
    path = write(tmp_path, "a.eml", data)

    doc = WebMailExtractor(ExtractConfig(email_attachment_depth=0)).extract(path)

    assert "海绵宝宝" not in doc.text
    assert doc.meta["inlined_mails"] == 0
    warning = next(w for w in doc.warnings if "没有展开" in w)
    assert "email_attachment_depth = 0" in warning
    assert "内层主题" in warning


def test_the_injected_depth_wins_over_the_config(tmp_path, extractor):
    """`depth=` 那个注入点压过配置（测试要能不动配置对象就走各档）。"""
    inner = EmailMessage(policy=email.policy.default)
    inner["From"] = "zl@example.com"
    inner["Subject"] = "内层主题"
    inner.set_content("内层正文：海绵宝宝。")
    path = write(tmp_path, "a.eml", eml(attach=[inner]))

    forced = WebMailExtractor(ExtractConfig(email_attachment_depth=0), depth=1).extract(path)

    assert "海绵宝宝" in forced.text


def test_a_mail_with_only_headers_is_an_empty_doc_with_a_warning(tmp_path, extractor):
    """只有邮件头的邮件：**正文**是空的，那条"没有提取到任何文本"照样出现。

    `_Text.marker()` 收进来的字不算正文行（`has_body` 只认 `line()` / `block()`），
    不区分的话一封只有邮件头的邮件看起来"有正文"，警告永远不出现。

    ⚠ `doc.is_empty` 在这里是**假**，这是故意的：标记行（发件人 / 主题……）进的是
    `text`、会被索引，所以"这份文档有没有可搜的字"的答案确实是"有"。区分"一个字的
    正文都没有"要靠那条警告，不能靠 `is_empty`——否则一封只写了主题的邮件会被下游
    当成空文件丢掉，而主题正是最该被搜到的字段。
    """
    raw = b"From: a@example.com\r\nSubject: \xe7\xa9\xba\xe7\x9a\x84\r\n\r\n"

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert all(line.startswith("【") for line in doc.text.splitlines())
    assert "【邮件】主题：空的" in doc.text
    assert any("没有正文" in warning for warning in doc.warnings)


@pytest.mark.parametrize("name", ["a.eml"])
def test_bytes_that_are_not_a_mail_are_a_ParseFailed(tmp_path, extractor, name):
    jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4

    with pytest.raises(ParseFailed) as excinfo:
        extractor.extract(write(tmp_path, name, jpeg))

    assert "RFC 5322" in str(excinfo.value)


def test_a_zero_byte_eml_is_a_ParseFailed(tmp_path, extractor):
    """0 字节的 `.eml` 判失败，与 `office.py` 对空 PDF 的态度一致（容器要求有结构）。"""
    with pytest.raises(ParseFailed):
        extractor.extract(write(tmp_path, "a.eml", b""))


def test_an_inline_non_text_part_is_not_counted_as_a_lost_attachment(tmp_path, extractor):
    """`multipart/related` 里那个**没有文件名**的内联图片不是"附件正文没收录"。

    它按 `Content-Disposition: inline` 标注，是渲染用的资源。把它算成损失的话，
    每一封带 logo 的 HTML 邮件都会多一条警告——而警告一多就没人看了。
    """
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: with logo\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"\r\n"
        + "正文".encode("utf-8")
        + b"\r\n"
        b"--B\r\n"
        b"Content-Type: image/png\r\n"
        b"Content-Disposition: inline\r\n"
        b"Content-ID: <logo@example>\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"iVBORw0KGgo=\r\n"
        b"--B--\r\n"
    )

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert doc.text.splitlines()[-1] == "正文"
    assert doc.meta["attachments"] == 0
    assert doc.warnings == ()


def test_a_nameless_non_inline_part_is_counted_as_lost(tmp_path, extractor):
    """没有任何标注的二进制部分算损失（不管它有没有名字）——不能静默。"""
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: s\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"\r\n"
        + "正文".encode("utf-8")
        + b"\r\n"
        b"--B\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"\r\n"
        b"\x00\x01\x02\r\n"
        b"--B--\r\n"
    )

    doc = extractor.extract(write(tmp_path, "a.eml", raw))

    assert doc.meta["attachments"] == 1
    assert any("没有收录" in warning for warning in doc.warnings)


# ==============================================================================
# 5. msg
# ==============================================================================


def test_the_hand_written_fixture_is_a_real_msg():
    """样本本身先过一遍真库：**这几条流拼歪了要在这里红**，而不是伪装成提取器的 bug。

    `.msg` 是这一组里唯一"样本自己造"的格式（OLE 复合文档没法用字符串字面量写），
    所以它需要一条自证：把造出来的字节直接喂给 `extract_msg.Message`，看它读出来的是
    不是我们写进去的东西。
    """
    from extract_msg import Message as MsgFile
    from extract_msg.enums import ErrorBehavior

    message = MsgFile(
        io.BytesIO(msg_bytes()),
        errorBehavior=ErrorBehavior.STANDARDS_VIOLATION | ErrorBehavior.ATTACH_SUPPRESS_ALL,
    )
    try:
        assert message.classType == "IPM.Note"
        assert message.subject == "季度汇报"
        assert message.body == "正文里有个独特的词：企鹅。"
        assert message.sender == "张三"
        assert message.to == "李四 <ls@example.com>"
        assert message.date is not None
        assert [a.name for a in message.attachments] == ["图表.png", "转发的邮件"]
    finally:
        message.close()


def test_msg_reads_the_mail_fields(tmp_path, extractor):
    """五个字段都进正文，`Subject` 进 `title`，时间归一成一种写法。"""
    path = write(tmp_path, "a.msg", msg_bytes())

    doc = extractor.extract(path)

    assert doc.title == "季度汇报"
    assert doc.text == (
        "【邮件】发件人：张三\n"
        "【邮件】收件人：李四 <ls@example.com>\n"
        "【邮件】时间：2026-10-15 09:30:00+08:00\n"
        "【邮件】主题：季度汇报\n"
        "正文里有个独特的词：企鹅。\n"
        "【附件】图表.png（48 字节）\n"
        "【附件】转发的邮件\n"
        "【邮件】发件人：赵六\n"
        "【邮件】主题：内层主题\n"
        "内层正文：海绵宝宝。"
    )
    assert doc.meta["format"] == "msg"
    assert doc.meta["attachments"] == 2
    assert doc.meta["inlined_mails"] == 1


def test_msg_attachment_size_comes_from_the_property(tmp_path, extractor):
    """大小从 `0E200003` 读，**不读 `data` 去量长度**（那会把附件整个读进内存）。"""
    doc = extractor.extract(write(tmp_path, "a.msg", msg_bytes()))

    assert "【附件】图表.png（48 字节）" in doc.text


def test_a_msg_without_an_attachment_has_no_attachment_warning(tmp_path, extractor):
    path = write(tmp_path, "a.msg", msg_bytes(file_attachment=False, embedded=None))

    doc = extractor.extract(path)

    assert doc.meta["attachments"] == 0
    assert doc.warnings == ()


def test_msg_with_only_an_html_body_uses_it(tmp_path, extractor):
    """只有 HTML 正文的邮件（Outlook 也存这一份）：剥成文本。"""
    markup = utf8("<html><body><p>HTML 那一份</p></body></html>")
    path = write(tmp_path, "a.msg", msg_bytes(body=None, html=markup))

    doc = extractor.extract(path)

    assert "HTML 那一份" in doc.text


def test_the_embedded_message_is_not_inlined_at_depth_0(tmp_path, extractor):
    path = write(tmp_path, "a.msg", msg_bytes())

    doc = WebMailExtractor(ExtractConfig(email_attachment_depth=0)).extract(path)

    assert "海绵宝宝" not in doc.text
    assert "【附件】转发的邮件" in doc.text
    assert any("email_attachment_depth = 0" in warning for warning in doc.warnings)


def test_a_msg_with_only_headers_is_an_empty_doc(tmp_path, extractor):
    """只有邮件头的 `.msg`：与 `.eml` 那条同一回事（标记行不算正文，警告照出）。"""
    path = write(
        tmp_path, "a.msg", msg_bytes(body=None, file_attachment=False, embedded=None)
    )

    doc = extractor.extract(path)

    assert all(line.startswith("【") for line in doc.text.splitlines())
    assert doc.text.startswith("【邮件】发件人：张三")
    assert any("没有正文" in warning for warning in doc.warnings)


@pytest.mark.parametrize("size", [16, 64, 200, 1536])
def test_a_truncated_msg_is_a_ParseFailed(tmp_path, extractor, size):
    r"""⚠ 坏 `.msg` 有两族异常，**这一条盯的是第二族**（`2026-10-16` 实测）。

    | 文件长什么样 | 谁抛的 | 基类 |
    |---|---|---|
    | 根本不是 OLE2（16 / 64 / 200 字节） | `InvalidFileFormatError` | `ExMsgBaseException` |
    | 是 OLE2、扇区链坏了（截到 1536 字节） | `olefile.OleFileError` | **`OSError`** |

    只接第一族的话，1536 那一条会变成**未捕获异常**（`OSError` 不在 `ExMsgBaseException`
    里），在整轮索引里把别的文件一起带下去——`A6` 要的正好相反。删掉 `_msg_errors()`
    里的 `OSError`，这四个参数里最后一个会红。
    """
    path = write(tmp_path, "a.msg", msg_bytes()[:size])

    with pytest.raises(ParseFailed):
        extractor.extract(path)


def test_a_msg_cut_in_half_is_a_ParseFailed(tmp_path, extractor):
    """同一条回归线的**不写死长度**的那一半：样本长多少，就从中间截。

    上面那条的参数（16 / 64 / 200 / 1536）是实测出来的**具体**分界，写死是为了让
    "哪一族异常"这件事留在代码里；这一条不依赖那些数字——样本将来长大了它照样管用。
    """
    data = msg_bytes()
    path = write(tmp_path, "a.msg", data[: len(data) // 2])

    with pytest.raises(ParseFailed):
        extractor.extract(path)


def test_garbage_bytes_are_a_ParseFailed(tmp_path, extractor):
    path = write(tmp_path, "a.msg", b"\x00\x01\x02garbage" * 20)

    with pytest.raises(ParseFailed) as excinfo:
        extractor.extract(path)

    assert "msg" in str(excinfo.value)
    # 库自己说的话必须在——那是排查时唯一的线索
    assert "InvalidFileFormatError" in str(excinfo.value)


def test_a_zero_byte_msg_is_a_ParseFailed(tmp_path, extractor):
    with pytest.raises(ParseFailed):
        extractor.extract(write(tmp_path, "a.msg", b""))


# ==============================================================================
# 6. 共用件：上限、meta、大小
# ==============================================================================


def test_truncation_stops_between_lines_and_leaves_a_warning(tmp_path):
    """上限到了**整行地停**（不在行里切），`truncated` 为真，并留一条警告。

    "正文到哪里为止"这件事要一句话说得清——切片与偏移都建立在这句话上。
    这里注入一个小的 `max_chars`，不然要造一个 1 MB 的样本。
    """
    lines = "".join(f"<p>第 {index} 行有一点点字</p>" for index in range(50))
    path = write(tmp_path, "a.html", page(body=lines))

    doc = WebMailExtractor(ExtractConfig(), max_chars=40).extract(path)

    assert doc.truncated
    assert len(doc.text) <= 40
    assert doc.text.splitlines()[0] == "第 0 行有一点点字"
    assert any(str(MAX_CHARS) in w or "上限" in w for w in doc.warnings)
    # 每一行都是完整的：末尾那一行要是被切过，这里会红
    for line in doc.text.splitlines():
        assert line.endswith("字")


def test_the_same_page_is_not_truncated_at_the_default_cap(tmp_path, extractor):
    """默认上限下这份小文件不该被截断——上面那条测试的另一半。"""
    path = write(tmp_path, "a.html", page(body="<p>短</p>"))

    doc = extractor.extract(path)

    assert not doc.truncated
    assert not any("上限" in warning for warning in doc.warnings)


def test_meta_carries_the_three_common_numbers(tmp_path, extractor):
    """三个数每个入口都有，键名固定（排查"这份文件的正文为什么长这样"的第一步）。"""
    for name, data in (
        ("a.html", page(body="<p>甲乙</p>")),
        ("a.mht", mht(html=utf8("<html><body><p>甲乙</p></body></html>"))),
        ("a.eml", eml(body="甲乙")),
        ("a.msg", msg_bytes()),
    ):
        doc = extractor.extract(write(tmp_path, name, data))
        assert doc.meta["bytes"] == len(data), name
        assert doc.meta["chars"] == len(doc.text), name
        assert doc.source == "web_mail", name
        assert doc.modality == "text", name


def test_no_warning_leaks_a_path_or_an_exception_class_into_the_text(tmp_path, extractor):
    """警告是给人看的、正文是给索引的——两者不能混。

    这条拦的是"顺手把警告也写进正文"这种改动：它会让一个坏文件的报错信息变成可搜索
    的内容，而且每一条警告都跟着 200 片上限的算术一起被切。
    """
    doc = extractor.extract(write(tmp_path, "a.mht", mht(plain="只有纯文本")))

    for warning in doc.warnings:
        assert warning not in doc.text


def test_extract_file_surfaces_the_warnings_at_the_extract_layer(tmp_path):
    """接进 `extract_file()` 之后警告原样在上层（它不吞、不改）。"""
    registry = build_registry(ExtractConfig())
    path = write(tmp_path, "a.eml", eml(body="正文", files=[(b"x", "附件.bin")]))

    doc = extract_file(path, registry=registry, config=ExtractConfig())

    assert any("没有收录" in warning for warning in doc.warnings)


def test_an_over_limit_file_is_a_FileUnreadable(tmp_path):
    """`[scan] max_file_mb` 那条上限也管着这一层（与 `office` 同一条路径）。"""
    path = write(tmp_path, "a.html", page())

    with pytest.raises(FileUnreadable):
        WebMailExtractor(ExtractConfig(max_bytes=16)).extract(path)
