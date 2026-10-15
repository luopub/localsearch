r"""网页与邮件（`F3-03`）：html / htm / xhtml / mht / mhtml / eml / msg → 正文。

七类扩展名，三种走法（计划 §4.3 那一行写的是 `html / htm / mht / eml / msg`，
`xhtml` 与 `mhtml` 是同一件事的别名）：

| 扩展名 | 是什么 | 怎么提 |
|---|---|---|
| `html` / `htm` / `xhtml` | 一份 HTML 文本 | 字节先解编码（`base.decode_bytes`），再交给 BeautifulSoup |
| `mht` / `mhtml` | MIME 容器，浏览器存的"单个文件网页"，里面是 HTML + 它的图片/CSS | 先按 MIME 拆开，取那个 `text/html` 部分 |
| `eml` | RFC 5322 邮件文本（MIME 树在里面） | `email` 模块 |
| `msg` | OLE 复合文档（Outlook 的邮件，二进制） | `extract-msg` |

`plaintext.py` 的名单里**故意**没有这七个（那里的注释写着为什么）：两边都声明的话，
谁在注册表前面决定了 HTML 是被当网页解析还是当一坨带标签的文本，而那种冲突是**静默**的。

------------------------------------------------------------------------------
HTML 怎么变成正文
------------------------------------------------------------------------------

规则四条，每条都会影响"用户能不能搜到"：

1. **块级元素各自成行**（`_BLOCK_TAGS`）：`<p>` / `<div>` / `<li>` / `<tr>` / 标题
   这些都断行，行内的 `<span>` / `<a>` / `<b>` 接在当前行上。不这么做的话整页会挤成
   一行（`get_text()` 不带分隔符就是这样），而切片是按行切的。
2. **`script` / `style` / `noscript` / `template` 整块丢掉**（`_DROP_TAGS`）。前两个
   里的字不是正文；后两个里装的常常是"标记本身的文本"（`noscript` 里的
   `&lt;img src=…&gt;` 那种），收进来等于往索引里灌标签。⚠ 丢 `noscript` 是**有代价**
   的：JS 重的站点把正文也放在那儿，那种页面会收不到字。代价写在这里，不藏在代码里。
3. **注释、文档类型、XML 声明不算正文**。`<!DOCTYPE html>` 与 `<?xml …?>` 在 BeautifulSoup
   里是**文本节点**（`Doctype` / `ProcessingInstruction` 都是 `NavigableString` 的子类），
   不显式排掉的话，`html` 这个词会变成正文的第一行——`xhtml` 文件大多带 XML 声明，
   所以这一条不是洁癖。
4. **`<pre>` 里的换行是内容**，不压平（代码、ASCII 图都在里面）。其余地方的连续空白
   一律压成一个空格。

`<title>` 进 `ExtractedDoc.title`，**不进正文**（与 docx 的核心属性同一个口径）。
没有 `<title>` 时 `title` 是 `None`，不是文件名——理由写在 `core/models.py`。

------------------------------------------------------------------------------
编码：HTML 走 `base`，容器走容器自己
------------------------------------------------------------------------------

- **`.html` / `.htm` / `.xhtml`**：解编码只有一处说了算，就是 `base.decode_bytes()`
  （BOM → 严格 UTF-8 → 探测器）。它的 `Decoded.warning` 原样进 `warnings`。
- **`.mht` / `.eml` / `.msg`**：**容器自己说了算**——MIME 部分有 `charset=` 参数、
  `msg` 有它自己的代码页。先按容器声明解，解不动（编解码器不存在、字节不是那个编码）
  才回落到 `base.decode_bytes()`，并且把它的警告一起留下。

这条分工是刻意的：一份 GBK 的 `.html` 没有 BOM、也没有 HTTP 头，只能猜（`base` 里那
五档）；而 MIME 部分**声明**了编码，那是文件自己给的硬证据，比猜准。⚠ **不主动去看
HTML 里的 `<meta charset>`**：那等于在本模块里再养一份编码判定，两处判定迟早打架。

------------------------------------------------------------------------------
邮件：正文选哪一段、附件收什么、`email_attachment_depth` 是什么
------------------------------------------------------------------------------

**正文**：走一遍 MIME 树，把"正文候选"与"附件候选"分开收着（`_EmailParts`）。
`text/plain` 优先，没有才用 `text/html`（剥成文本）。一个部分只要有 `filename`
或 `Content-Disposition: attachment`，它就是**附件**，哪怕它的类型是 `text/plain`——
把一封带 `.txt` 附件的邮件的正文写成那个附件的内容，是这类提取器最容易犯的错。

**正文里会出现的标记行**（与 `office.py` 同一套约定，理由也一样：这些是文件里真有的
事实，只是没写进文字流）：

| 标记 | 什么时候加 |
|---|---|
| `【邮件】发件人：…` / `收件人：…` / `抄送：…` / `时间：…` / `主题：…` | 每封邮件开头，**有值才加** |
| `【附件】名字（N 字节）` | 每个附件一行（大小拿不到时括号不出现） |

⚠ **`主题` 同时进 `title` 与正文**（office 的 docx 只进 `title`）。理由：对一封邮件来说
主题是**内容**（任何邮件客户端都会把它排在正文上方），而 `title` 会不会进 FTS 要到
`P4` 定；不重复的话，"按标题搜邮件"在 v1 可能一条都搜不到。代价是将来 `title` 真进了
FTS 时，主题那几个词会在 BM25 里被算两次——记在 `PROGRESS.md` 的遗留里，届时再定。

**附件只收名字**（`【附件】…` 那一行），**正文不进全文索引**，并且**出声**：附件里的
字搜不到是用户最容易误会的一件事。理由不是偷懒：提取器的接口是 `extract(path)`
（`base.Extractor`），没有"从内存里的字节提一份"这一档，所以附件正文要成为可搜索的
内容，得等 `P4` 的"容器内条目"机制（计划 §4.7 / `R16`）把附件落成独立记录。

**`email_attachment_depth`（`[extract]`，默认 1）= 允许内联展开的**邮件附件**层数**：

- `0`：附件只列名字，一层都不展开；
- `1`：**是邮件的那一层附件**（`message/rfc822` 部分、内嵌 `.msg`）把它的邮件头与正文
  一并收进来（形式上就是：`【附件】转发.msg` 那一行后面接它自己的 `【邮件】…` 块）；
- `2`：连那封被转发邮件里的邮件附件也展开，以此类推。

**非邮件的附件（pdf / docx / 图片）在任何深度都只列名字**——上面那段说了为什么
（接口里没有内存入口）。这条限制不藏着：只要还有没展开的附件，就会多一条 `warnings`，
把名字列出来。深度不够而停下的那一档也单独出声（那条警告里带着当前的配置值），
因为"配置的锅"与"v1 不做的锅"对用户是两件事。

------------------------------------------------------------------------------
上限与截断：与 office 同一套 `_Text`
------------------------------------------------------------------------------

`MAX_CHARS = 1e6`，`extract/_text.py` 的 `_Text` **整行地停**并置 `truncated`，这与
`office.py` 逐字相同（上限的算术在那儿，不必抄两遍）。⚠ 附件名在正文的**末尾**，
所以正文极长时截断会先吃掉附件那几行——不要紧：附件名还会出现在那条"没收录"的
警告里，名字不会因为截断而消失。

**0 字节文件**在这一层有两种结局，都不算意外：`.html` 是一个空页面（收成空正文 +
一条警告），`.eml` / `.msg` 判 `ParseFailed`（这两种格式的"合法性"就在结构上，
0 字节没有结构）——与 `office.py` 对空 PDF 的态度一致。

------------------------------------------------------------------------------
解析成功但正文是空的：留一条警告（与 office 同一条理由）
------------------------------------------------------------------------------

"提取成功、正文是空的"与"这个文件本来就是空的"在下游完全分不开，而用户会以为全文
索引是完整的。所以正文里一个字都没有时（`_Text.has_body` 为假，**标记行不算**）加一条
警告，且那句话按格式不同说清是哪一种情况。

------------------------------------------------------------------------------
库按需导入，不在模块顶层
------------------------------------------------------------------------------

`beautifulsoup4` / `extract-msg` / 连 `email.policy` 都在各自的函数里导入，理由与
`office.py` 那张表完全一样（`E32`）：`build_registry()` 会 import 本模块，顶层 import
等于让每一次 `lsearch` 启动都多付这笔钱。读数与复跑方式在 `PROGRESS.md` §3 的
**E33**（`tools/bench_imports.py`，与 office 那四个库同一张表）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable

from localsearch.core.models import ExtractedDoc

from ._text import _Text
from .base import (
    ExtractConfig,
    Extractor,
    ParseFailed,
    UnsupportedFormat,
    decode_bytes,
    ext_of,
    read_bytes,
)

# ==============================================================================
# 认领的扩展名：三组，对应三种走法（见模块文档那张表）
# ==============================================================================

#: 一份 HTML 文本。
HTML_EXTS = frozenset({"html", "htm", "xhtml"})

#: MIME 容器：网页 + 它的资源（浏览器"另存为 → 单个文件网页"的产物）。
MHT_EXTS = frozenset({"mht", "mhtml"})

#: 邮件：`.eml` 是 RFC 5322 文本，`.msg` 是 OLE 复合文档。
MAIL_EXTS = frozenset({"eml", "msg"})

#: 本提取器认领的全部扩展名。`supports()` 与它共用一份名单。
WEB_MAIL_EXTS = HTML_EXTS | MHT_EXTS | MAIL_EXTS

#: 正文的字符上限，与 `office.py` 同一个数、同一个理由（200 片的算术 + 内存）。
MAX_CHARS = 1_000_000

#: 块级标签：它们**各自成行**（理由见模块文档第 1 条）。行内的（`span` / `a` / `b` /
#: `em` / `code` / `td` / `th` …）不在这里，它们的字接在当前行上。
#:
#: `br` / `hr` 也在这里：它们没有字，但要求"在这里断行"——收进这一组正好得到那个行为
#: （`flush()` 一次，然后递归进一个空元素，什么也不加）。
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "audio", "blockquote", "br", "canvas", "caption",
    "dd", "details", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "legend",
    "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "tfoot", "thead", "tr", "ul", "video",
})

#: 整块丢掉的标签（理由与代价见模块文档第 2 条）。
#:
#: `title` 也在里面，理由不同：它的字**已经进了 `ExtractedDoc.title`**，留着就等于
#: 正文第一行又抄一遍标题（`_html_lines()` 是**先读 `soup.title` 再 decompose**，
#: 顺序反了标题就没了）。
_DROP_TAGS = ("script", "style", "noscript", "template", "title")

#: 哪些**邮件头**出现一个就说明"这确实是一封邮件 / 一个 MIME 容器"。用来把"扩展名写错
#: 的文件"（把一张 JPEG 改名成 `.eml`）在灌进索引之前拦下来——`email` 模块对任何字节
#: 都能解出一个 `Message`，不设这一关，乱码会**静默**变成正文。
_MIME_HEADERS = frozenset({
    "from", "to", "cc", "bcc", "subject", "date", "sender", "reply-to", "return-path",
    "message-id", "mime-version", "content-type", "content-location", "content-transfer-encoding",
    "received", "x-mailer", "user-agent",
})


# ==============================================================================
# 解析结果：与 office 的 `_Parsed` 同一个形状（那边是私有的，这里自己写一份）
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _Parsed:
    """一个文件解析出来的东西。`bytes` / `chars` / `format` 由 `extract()` 统一补。"""

    text: str
    title: str | None
    meta: dict[str, Any]
    warnings: tuple[str, ...] = ()


def _failed(kind: str, exc: BaseException) -> ParseFailed:
    """把库的异常翻成一条人能读的理由。**带上库自己说的话**——那是唯一的线索。"""
    return ParseFailed(f"{kind} 解析失败（{type(exc).__name__}：{exc}）")


def _one_line(value: Any) -> str | None:
    """收敛成一个单行的短字符串（`None` / 全空白 → `None`）。

    邮件头可以**折行**（RFC 5322 的折叠空白：续行以空白开头），折行原样留下会让
    `【邮件】主题：` 那行断成两行，`title` 也会带着换行进嵌入前缀
    （`title: {t} | text: …`，`M3`）而撞上分隔符。所以内部空白一律压成一个空格。
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


# ==============================================================================
# HTML：BeautifulSoup 只在这一段里用，两种入口共用（网页 / mht 里的那一部分 / 邮件的 HTML 正文）
# ==============================================================================


def _html_lines(markup: str) -> tuple[str | None, list[str]]:
    """HTML 文本 → `(标题, 正文行)`。规则见模块文档那个四条。

    `markup` 已经是**解好编码的 `str`**（`str` 给 `BeautifulSoup` 时它不再自己猜编码，
    所以这里不会出现第二份编码判定）。
    """
    from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction

    soup = BeautifulSoup(markup, "lxml")
    title = _one_line(soup.title.get_text()) if soup.title is not None else None
    for element in soup(_DROP_TAGS):
        element.decompose()

    lines: list[str] = []
    run: list[str] = []
    _walk_lines(soup, lines, run, (Comment, Declaration, Doctype, ProcessingInstruction))
    _flush(run, lines)
    return title, lines


def _flush(run: list[str], lines: list[str]) -> None:
    """把当前攒着的一行收尾：内部空白压成一个空格，全空白的丢掉。"""
    text = " ".join("".join(run).split())
    run.clear()
    if text:
        lines.append(text)


def _walk_lines(node: Any, lines: list[str], run: list[str], skip: tuple[type, ...]) -> None:
    r"""把一个节点的子节点摊成行：块级各自成行，其余接在当前行上。

    `run` 是**当前这一行的字**，由调用方持有、一路传下去。⚠ 这个共享是**必须**的，
    第一版把它做成每次递归里的局部变量，`2026-10-16` 写探针时当场抓到：`<p>第一段有
    <b>加粗</b> 与 <a>链接</a>。</p>` 收成三行、而且顺序是
    `第一节 / 加粗 / 链接 / 第一段里有 与 ，还有…`——递归进 `<b>` 那次在自己的 `run`
    上收了尾并 append，外层那半行就被**插到后面**去了。行内元素的字属于**当前行**，
    这一条只能由一个共享的累积器表达。

    `skip` 是**不算正文的文本节点类**（注释、`DOCTYPE`、XML 声明）：它们在 BeautifulSoup
    里也是 `NavigableString` 的子类，所以必须**先**判它们，否则 `<!DOCTYPE html>` 里的
    `html` 会变成正文第一行。两个参数都由调用方传来是为了让 bs4 的 import 留在函数里。

    ⚠ 非块级元素**一律递归**，不是 `get_text()` 一下接上来。第一版是 `get_text()`，
    同一个探针抓到第二处：`<html>` / `<body>` 不在 `_BLOCK_TAGS` 里（它们是容器，不该
    成行），于是整份文档被那一次 `get_text()` 摊成**一行**——`meta["lines"]` 是 1，
    而下游切片是按行切的。判据是"块级才断行"这条规则本身，**不是**"认识所有行内标签"
    （那需要一张必然过时的名单）。
    """
    from bs4 import NavigableString

    for child in node.children:
        if isinstance(child, skip):
            continue
        if isinstance(child, NavigableString):
            run.append(str(child))
            continue
        if child.name in _BLOCK_TAGS:
            _flush(run, lines)
            if child.name == "pre":
                lines.extend(_pre_lines(child.get_text()))   # 换行是内容，整块收
                continue
        _walk_lines(child, lines, run, skip)


def _pre_lines(text: str) -> list[str]:
    r"""`<pre>` 的内容：**逐行收**，不压平（那里的换行是内容），去掉行尾空白。

    空行丢掉：代码块里的空行对"搜得到"没有贡献，而正文越长越早碰到上限。
    """
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _parse_html(markup: str, cap: _Text, warnings: list[str]) -> _Parsed:
    """一份 HTML → 正文。收了几行记在 `meta["lines"]`（排查"为什么只有两行"的第一个数）。

    `warnings` 是**调用方的那个列表**（解编码的警告已经先一步放进去了），
    `_Parsed.warnings` 就是它的快照——三个入口的警告汇到同一条路上。
    """
    title, lines = _html_lines(markup)
    kept = 0
    for line in lines:
        cap.line(line)
        if cap.full:
            break
        kept += 1
    return _Parsed(cap.text, title, {"lines": kept}, tuple(warnings))


# ==============================================================================
# MIME 部分：mht 与 eml 共用那两个小工具
# ==============================================================================


def _part_payload(part: Any) -> bytes | None:
    r"""一个 MIME 部分的**原始字节**（`Content-Transfer-Encoding` 已经还原）。

    `get_payload(decode=True)` 在"这个部分的 payload 本身还是个 `Message` 列表"
    （`message/rfc822` 那种）时返回 `None`，调用方要能分得清——所以这里不把它变成空串。
    """
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None


def _part_text(part: Any, warnings: list[str]) -> str:
    r"""一个文本部分的字：**先按部分自己声明的 charset**，解不动才回落 `base.decode_bytes`。

    回落的**痕迹必须留下**：那是"猜"的（`Decoded.guessed` / `lossy`），乱码时第一个
    要查的就是它。声明了 charset 而解不下来时会连试都不再试第二种——`base` 那五档
    里包含严格 UTF-8 与 GB18030，对"声明错了"的文件够用。
    """
    payload = _part_payload(part)
    if payload is None:
        return ""
    charset = part.get_content_charset()
    if charset:
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    decoded = decode_bytes(payload)
    if decoded.warning:
        warnings.append(decoded.warning)
    return decoded.text


def _looks_like_mime(message: Any) -> bool:
    """有没有一个像样的邮件 / MIME 头（判据见 `_MIME_HEADERS` 的文档）。"""
    if message.is_multipart():
        return True
    return any(str(key).lower() in _MIME_HEADERS for key in message.keys())


# ==============================================================================
# mht / mhtml：MIME 容器，取里面那个 HTML 部分
# ==============================================================================


def _parse_mht(data: bytes, cap: _Text) -> _Parsed:
    r"""mht：拆 MIME，取 **`text/html` 部分**（多个时取第一个，并出声）。

    浏览器存"单个文件网页"时把网页与它的图片 / CSS 一起塞进一个 `multipart/related`，
    正文只有那个 HTML 部分；也有工具存成"只有一个 `text/html` 部分"的形态，所以这里
    用 `walk()`（它把根节点也算进来）而不是只看子部分。

    没有 HTML 部分但有 `text/plain` 时**用它**并出声——与其把这类文件判成失败，不如把
    手上真有的字收进来，同时说清这不是正常形态。两者都没有就 `ParseFailed`：那不是
    "这个文件没内容"，是"这不是一个 mht"。
    """
    import email
    import email.policy

    message = email.message_from_bytes(data, policy=email.policy.default)
    if not _looks_like_mime(message):
        raise ParseFailed(
            "mht 里一个 MIME 头都没有（MIME-Version / Content-Type / Content-Location …），"
            "这看起来不是一个 MIME 容器（扩展名写错了？）"
        )

    warnings: list[str] = []
    parts = list(message.walk())
    htmls = [p for p in parts if p.get_content_type() == "text/html" and not p.is_multipart()]
    plains = [p for p in parts if p.get_content_type() == "text/plain" and not p.is_multipart()]

    title: str | None = None
    lines: list[str] = []
    if htmls:
        markup = _part_text(htmls[0], warnings)
        title, lines = _html_lines(markup)
        if len(htmls) > 1:
            warnings.append(
                f"mht 里有 {len(htmls)} 个 HTML 部分，只收了第一个（其余多半是内嵌框架）"
            )
    elif plains:
        warnings.append("mht 里没有 HTML 部分，收的是它的 text/plain 部分（不是正常形态）")
        lines = [one for one in _part_text(plains[0], warnings).splitlines() if one.strip()]
    else:
        kinds = sorted({p.get_content_type() for p in parts if not p.is_multipart()})
        raise ParseFailed(f"mht 里既没有 HTML 也没有 text/plain 部分，只有 {kinds}")

    # 主题头可能是浏览器写的网页标题（"另存为"时就是这么存的），HTML 自己没有
    # `<title>` 时拿它顶上——不编造：这是文件里真的写着的一行。
    if title is None:
        title = _one_line(message.get("Subject"))

    kept = 0
    for line in lines:
        cap.line(line)
        if cap.full:
            break
        kept += 1
    return _Parsed(cap.text, title, {"parts": len(parts), "html_parts": len(htmls)}, tuple(warnings))


# ==============================================================================
# 邮件：eml / msg 都先摘成同一形状，**正文的收法只写一遍**（`_mail_into`）
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _Attach:
    """一个附件。**只保证有名字**（大小与内容都可能拿不到）。

    `is_mail` 与 `nested` 是两件事，分开记：`is_mail` 说"这附件是一封邮件"，
    `nested` 在**深度允许时**才挂着那封信。深度不够时 `nested` 是 `None` 而 `is_mail`
    仍是真——统计里这两档要分开（一个是"配置的锅"，一个是"v1 不做"）。
    """

    name: str
    size: int | None = None
    is_mail: bool = False
    nested: "_Mail | None" = None


@dataclass(frozen=True, slots=True)
class _Mail:
    """一封邮件里我们真正要的那些东西（eml / msg / 内嵌邮件都归到这个形状上）。"""

    subject: str | None = None
    sender: str | None = None
    to: str | None = None
    cc: str | None = None
    date: str | None = None
    #: 已经是**纯文本**（HTML 正文在适配器里就剥好了）。空串 = 没有正文。
    body: str = ""
    attachments: tuple[_Attach, ...] = ()


@dataclass
class _MailStat:
    """收邮件时攒的几个数，最后决定要说哪几句警告。**可变**（一路上加）。"""

    attachments: int = 0
    inlined: int = 0
    #: 收了名字、正文没进索引的附件名。
    unparsed: list[str] = field(default_factory=list)
    #: **是**邮件、但因为深度不够没展开的附件名。
    depth_stopped: list[str] = field(default_factory=list)
    #: 读都读不出来的附件名（库自己报的错）。
    broken: list[str] = field(default_factory=list)


def _name_list(names: list[str], limit: int = 3) -> str:
    """前几个名字 + 总数（警告是给人看的，不能因为附件多就变成长篇）。"""
    head = "、".join(names[:limit])
    return head if len(names) <= limit else f"{head} 等 {len(names)} 个"


def _mail_into(mail: _Mail, cap: _Text, stat: _MailStat) -> None:
    """把一封邮件收进 `cap`：邮件头（标记行）→ 正文 → 附件名（邮件附件按深度展开）。

    附件排在正文**后面**（与邮件客户端的阅读顺序一致）。截断会先吃掉附件那几行——
    可以接受：附件名还会出现在警告里（见模块文档）。
    """
    for label, value in (
        ("发件人", mail.sender),
        ("收件人", mail.to),
        ("抄送", mail.cc),
        ("时间", mail.date),
        ("主题", mail.subject),
    ):
        if value:
            cap.marker(f"【邮件】{label}：{value}")

    if mail.body:
        cap.block(mail.body)

    for attach in mail.attachments:
        stat.attachments += 1
        size = "" if attach.size is None else f"（{attach.size} 字节）"
        cap.marker(f"【附件】{attach.name}{size}")
        if attach.nested is not None:
            stat.inlined += 1
            _mail_into(attach.nested, cap, stat)
        elif attach.is_mail:
            stat.depth_stopped.append(attach.name)
        else:
            stat.unparsed.append(attach.name)


def _attach_warnings(stat: _MailStat, depth: int) -> list[str]:
    """附件那三档警告。**每一档都要出声**，理由见模块文档。"""
    warnings: list[str] = []
    if stat.unparsed:
        warnings.append(
            f"{len(stat.unparsed)} 个附件的正文没有收录（v1 只收附件名，附件里的字搜不到）："
            f"{_name_list(stat.unparsed)}"
        )
    if stat.depth_stopped:
        warnings.append(
            f"{len(stat.depth_stopped)} 个邮件附件没有展开："
            f"[extract] email_attachment_depth = {depth} 只往下展开 {depth} 层（附件里的附件没进去）："
            f"{_name_list(stat.depth_stopped)}"
        )
    if stat.broken:
        warnings.append(f"{len(stat.broken)} 个附件读不出来，只留了名字：{_name_list(stat.broken)}")
    return warnings


# ------------------------------------------------------------------------------
# eml
# ------------------------------------------------------------------------------


class _EmailParts:
    """走一遍 MIME 树，把"正文候选"与"附件候选"分开收着。

    **必须分开收**：`multipart/alternative` 里同一封邮件有两份（纯文本 + HTML），
    抓到哪份算哪份就会随解析顺序变；`text/plain` 的**附件**（`.txt`）也不该被当成正文。
    """

    __slots__ = ("plain", "html", "files", "resources", "warnings")

    def __init__(self) -> None:
        self.plain: list[Any] = []
        self.html: list[Any] = []
        self.files: list[Any] = []
        #: 行内的非文本部分（`multipart/related` 里的图片）：是渲染用的资源，
        #: 不是"用户以为能搜到的附件"，所以不计入损失。
        self.resources: list[Any] = []
        self.warnings: list[str] = []

    def body_text(self) -> str:
        """正文：`text/plain` 优先，没有才把 HTML 剥成文本（模块文档写了理由）。"""
        if self.plain:
            return _part_text(self.plain[0], self.warnings)
        if self.html:
            _title, lines = _html_lines(_part_text(self.html[0], self.warnings))
            return "\n".join(lines)
        return ""


def _walk_email(part: Any, parts: _EmailParts) -> None:
    """递归走 MIME 树，把每个部分归到 `parts` 的四档里。

    ⚠ `message/rfc822` **必须第一个判**：按 `is_multipart()` 它是"多部分"（里面就是
    那封被转发的邮件），先判它才会走"这是一封附件邮件"那一支，否则会被当成正文，
    把转发件的正文与转发件自己的标题一起当成收件人的正文。
    """
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        parts.files.append(part)
        return
    if part.is_multipart():
        for sub in part.iter_parts():
            _walk_email(sub, parts)
        return

    disposition = part.get_content_disposition()
    if part.get_filename() or disposition == "attachment":
        parts.files.append(part)
    elif ctype == "text/plain" or (ctype.startswith("text/") and not _is_html(ctype)):
        parts.plain.append(part)
    elif _is_html(ctype):
        parts.html.append(part)
    elif disposition == "inline":
        parts.resources.append(part)
    else:
        parts.files.append(part)          # 无名、内联都不是的二进制：算损失，出声


def _is_html(ctype: str) -> bool:
    return ctype in ("text/html", "application/xhtml+xml")


def _from_email(message: Any, depth: int, stat: _MailStat) -> _Mail:
    """`email` 模块的 `Message` → `_Mail`。"""
    import email.utils

    parts = _EmailParts()
    _walk_email(message, parts)

    attachments: list[_Attach] = []
    for part in parts.files:
        try:
            attachments.append(_email_attach(part, depth, stat))
        except _PART_ERRORS:
            # 一个部分读不出来**不带走整封邮件**：名字留下、内容丢掉、出声。
            # 理由与 `msg` 那边逐字段兜住相同（`A6` 的精神在单个文件内部也一样）。
            name = _one_line(part.get_filename()) or "（无名附件）"
            stat.broken.append(name)
            attachments.append(_Attach(name=name))

    date = _one_line(message.get("Date"))
    if date is not None:
        # 邮件头里的时间是**文本**，格式自由（`+0800` / `GMT` / 缺时区都有）。能解成
        # 结构化时间就写成统一形态（好按时间搜），解不动就原样留着——不编造。
        try:
            parsed = email.utils.parsedate_to_datetime(date)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            date = _stamp(parsed)

    return _Mail(
        subject=_one_line(message.get("Subject")),
        sender=_one_line(message.get("From")),
        to=_one_line(message.get("To")),
        cc=_one_line(message.get("Cc")),
        date=date,
        body=parts.body_text(),
        attachments=tuple(attachments),
    )


def _email_attach(part: Any, depth: int, stat: _MailStat) -> _Attach:
    """一个 eml 附件 → `_Attach`。是 `message/rfc822` 且深度允许时把那一层摘出来。"""
    if part.get_content_type() != "message/rfc822":
        payload = _part_payload(part)
        return _Attach(
            name=_one_line(part.get_filename()) or "（无名附件）",
            size=None if payload is None else len(payload),
        )

    inner = _inner_message(part)
    is_mail = True
    name = _one_line(part.get_filename())
    if inner is not None:
        name = name or _one_line(inner.get("Subject")) or "（无主题的邮件附件）"
    name = name or "（无名邮件附件）"
    if inner is None or depth <= 0:
        return _Attach(name=name, is_mail=is_mail)
    return _Attach(name=name, is_mail=is_mail, nested=_from_email(inner, depth - 1, stat))


def _inner_message(part: Any) -> Any | None:
    """`message/rfc822` 部分里那封邮件。

    `get_payload()` 的形态随解析器 / policy 而变（一个 `Message`、或 `[Message]`、
    或一个 `str`），三种都认。认不出来返回 `None`——调用方把它降级成"只列名字"，
    不假装展开过。
    """
    payload = part.get_payload()
    if isinstance(payload, list):
        for one in payload:
            if hasattr(one, "keys"):
                return one
        return None
    return payload if hasattr(payload, "keys") else None


def _parse_eml(data: bytes, depth: int, stat: _MailStat, warnings: list[str]) -> _Mail:
    """`.eml` → `_Mail`。"""
    import email
    import email.policy

    message = email.message_from_bytes(data, policy=email.policy.default)
    if not _looks_like_mime(message):
        raise ParseFailed(
            "eml 里一个邮件头都没有（From / Subject / MIME-Version / Content-Type …），"
            "这看起来不是一封 RFC 5322 邮件（扩展名写错了？）"
        )
    return _from_email(message, depth, stat)


# ------------------------------------------------------------------------------
# msg（Outlook 的 OLE 复合文档）
# ------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _msg_errors() -> tuple[type[BaseException], ...]:
    r"""`extract-msg` 抛错有**两族**（`2026-10-16` 实测），少列一族就是 `A6` 的反面：

    | 文件长什么样 | 谁抛的 |
    |---|---|
    | 根本不是 OLE2（垃圾字节、截断到几乎没有） | `extract_msg.exceptions.InvalidFileFormatError`（基类 `ExMsgBaseException`） |
    | 是 OLE2、但扇区链坏了（把一个真 .msg 截到一半） | **`olefile.OleFileError`**（基类 **`OSError`**，不是 `ExMsgBaseException`） |

    第二行是实测出来的：把一份 3072 字节的真 `.msg` 截到 1536 字节打开，抛的是
    `OleFileError: incorrect OLE FAT, sector index out of range`。`olefile` 不是
    `extract-msg` 的异常体系，**列漏它，那种坏文件会变成未捕获异常、把整轮索引带下去**。
    这里读的是内存里的字节（`BytesIO`），所以收 `OSError` 不会顺手吞掉真正的磁盘 I/O 错。

    ⚠ 不在这一族里的东西**继续往上冒**（`base` 那条"真接不住的不要接"）。
    """
    from extract_msg.exceptions import ExMsgBaseException

    return (ExMsgBaseException, OSError)


@lru_cache(maxsize=1)
def _msg_field_errors() -> tuple[type[BaseException], ...]:
    """逐字段读的时候敢接住的那些。

    比 `_msg_errors()` 多几个纯 Python 的（`NotImplementedError` 是库自己抛的"这个形态
    我不做"，`ValueError` / `TypeError` 来自它内部的转换）：**每一个字段各自兜住**，
    读不出来的那个记一条警告，其余照收——一个坏字段不该让整封邮件变成"解析失败"。

    ⚠ 这条把 `AttributeError` 也收进来了，代价是"我们自己写错属性名"会退化成一条警告
    而不是当场红。测试里那几条断言（主题 / 正文 / 附件名都要出现在正文里）就是它的
    回归线：名字写错，正文里就没有那个值。
    """
    return _msg_errors() + (NotImplementedError, ValueError, TypeError, KeyError, AttributeError)


def _stamp(value: Any) -> str | None:
    """时间 → `YYYY-MM-DD HH:MM:SS[+HH:MM]`。

    `datetime` 原样格式化（`msg` 给的就是它，带不带时区看文件）；其余类型交给 `str()`
    ——**不编造格式**。`None` 原样返回。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return _one_line(value)


def _from_msg(msg: Any, depth: int, stat: _MailStat, warnings: list[str]) -> _Mail:
    """`extract_msg.Message` → `_Mail`。逐字段读、每个字段各自兜住异常。"""
    from extract_msg.enums import AttachmentType

    broken: list[str] = []

    def field(label: str, getter: Callable[[], Any]) -> Any:
        try:
            return getter()
        except _msg_field_errors() as exc:
            broken.append(f"{label}（{type(exc).__name__}）")
            return None

    body = field("正文", lambda: msg.body)
    if not body:
        # 只有 HTML 正文的邮件（Outlook 也常存这一份）。`htmlBody` 给的是字节，
        # 用 `base` 那五档解（msg 的代码页在容器自己身上，这里够用）。
        html = field("HTML 正文", lambda: msg.htmlBody)
        if html:
            decoded = decode_bytes(html)
            if decoded.warning:
                warnings.append(decoded.warning)
            _title, lines = _html_lines(decoded.text)
            body = "\n".join(lines)

    attachments: list[_Attach] = []
    for one in field("附件", lambda: msg.attachments) or ():
        attachments.append(_msg_attach(one, depth, stat, warnings, AttachmentType))

    if broken:
        warnings.append(f"msg 里有几项读不出来，已跳过：{'、'.join(broken)}")

    return _Mail(
        subject=_one_line(field("主题", lambda: msg.subject)),
        sender=_one_line(field("发件人", lambda: msg.sender)),
        to=_one_line(field("收件人", lambda: msg.to)),
        cc=_one_line(field("抄送", lambda: msg.cc)),
        date=_stamp(field("时间", lambda: msg.date)),
        body=body or "",
        attachments=tuple(attachments),
    )


def _msg_size(one: Any) -> int | None:
    """附件大小（`PidTagAttachSize` = `0E200003`）。

    **不读 `data` 去量长度**：那会把整个附件读进内存，只为了印一个数字。拿不到就算了
    （`None`），不编造。
    """
    try:
        value = one.getPropertyVal("0E200003")
    except _msg_field_errors():
        return None
    return value if isinstance(value, int) else None


def _msg_attach(one: Any, depth: int, stat: _MailStat, warnings: list[str], kind: Any) -> _Attach:
    """一个 msg 附件 → `_Attach`。内嵌邮件（`AttachmentType.MSG`）在深度允许时展开。"""
    name = _one_line(getattr(one, "name", None)) or "（无名附件）"
    size = _msg_size(one)

    try:
        is_mail = one.type == kind.MSG
    except _msg_field_errors():
        is_mail = False

    if not is_mail:
        return _Attach(name=name, size=size)
    if depth <= 0:
        return _Attach(name=name, size=size, is_mail=True)

    # 内嵌那一层是**同一个文件里的另一个存储**：`extract-msg` 会为它再开一个 MSGFile，
    # 读完之后要关（它自己挂着 olefile 的句柄）。
    inner: Any = None
    try:
        inner = one.data
        nested = _from_msg(inner, depth - 1, stat, warnings)
    except _msg_field_errors():
        stat.broken.append(name)
        return _Attach(name=name, size=size, is_mail=True)
    finally:
        _close_quietly(inner)
    return _Attach(name=name, size=size, is_mail=True, nested=nested)


def _close_quietly(handle: Any) -> None:
    """关掉库自己开的句柄，关不掉也不出声。

    `MSGFile.close()` 在结构坏掉的文件上自己也可能抛（它要读一遍才关得干净）。这里
    已经在"收尾"路径上，抛出去只会把一次成功的提取变成失败——而这个句柄是内存里
    的（`BytesIO`），漏掉不会有磁盘上的后果。
    """
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # noqa: BLE001 - 见文档串：收尾路径上不值得再报一次
        pass


def _parse_msg(data: bytes, depth: int, stat: _MailStat, warnings: list[str]) -> _Mail:
    r"""`.msg` → `_Mail`。

    `errorBehavior` 传两个标志，故意的：

    - `STANDARDS_VIOLATION`：不合规范的(msg 文件)在本机语料里是常态（缺 `__nameid_version1.0`、
      某个收件人没有属性流……），不设它，它们会抛出来变成 `ParseFailed`，而文件本身
      是能读的。
    - `ATTACH_SUPPRESS_ALL`：坏附件**降级成 `BrokenAttachment` 对象**而不是抛异常。
      这是 `A6` 在单文件内部的样子——一个坏附件不该让整封邮件读不出来；降级之后那个
      附件仍然有名字，会进正文的 `【附件】` 行、也会进那条"读不出来"的警告，**不是静默**。
    """
    import io as _io

    import extract_msg
    from extract_msg.enums import ErrorBehavior

    flags = ErrorBehavior.STANDARDS_VIOLATION | ErrorBehavior.ATTACH_SUPPRESS_ALL
    try:
        msg = extract_msg.Message(_io.BytesIO(data), errorBehavior=flags)
    except _msg_errors() as exc:
        raise _failed("msg", exc) from exc

    try:
        return _from_msg(msg, depth, stat, warnings)
    finally:
        _close_quietly(msg)


def _parse_mail(data: bytes, ext: str, cap: _Text, depth: int) -> _Parsed:
    """`.eml` / `.msg` → `_Parsed`。

    两个适配器（`_from_email` / `_from_msg`）只负责**摘成 `_Mail`**，正文怎么收、
    附件怎么数、说哪几句警告，都在下面这三行里写了一遍（`_mail_into` 那一套）。
    分成两处写的话，eml 与 msg 迟早会在"附件标记行长什么样"上分岔。
    """
    stat = _MailStat()
    warnings: list[str] = []
    mail = (
        _parse_eml(data, depth, stat, warnings)
        if ext == "eml"
        else _parse_msg(data, depth, stat, warnings)
    )
    _mail_into(mail, cap, stat)
    warnings.extend(_attach_warnings(stat, depth))
    return _Parsed(
        cap.text,
        mail.subject,
        {"attachments": stat.attachments, "inlined_mails": stat.inlined},
        tuple(warnings),
    )


# ==============================================================================
# 空正文时那句警告：按格式分开写，"为什么是空的"就是用户唯一的线索
# ==============================================================================

_EMPTY_HINTS = {
    "html": "这个 HTML 里没有可提取的文字（只有脚本、样式或图片？）",
    "htm": "这个 HTML 里没有可提取的文字（只有脚本、样式或图片？）",
    "xhtml": "这个 HTML 里没有可提取的文字（只有脚本、样式或图片？）",
    "mht": "这个 mht 里没有可提取的文字（只有样式表与图片？）",
    "mhtml": "这个 mht 里没有可提取的文字（只有样式表与图片？）",
    "eml": "这封邮件没有正文（只有邮件头？）；附件里的字 v1 不进索引",
    "msg": "这封邮件没有正文（只有邮件头？）；附件里的字 v1 不进索引",
}

#: 读一个 MIME 部分可能抛的那几种（`email` 模块的行为，`2026-10-16` 定的）：声明的
#: 编解码器不存在给 `LookupError`，坏 base64 给 `ValueError`（`binascii.Error` 是它的
#: 子类），结构不合规范时 `get_content_*` 给 `ValueError` / `TypeError`。
_PART_ERRORS = (ValueError, LookupError, TypeError)


# ==============================================================================
# 提取器
# ==============================================================================


class WebMailExtractor:
    """网页与邮件 → 正文。三种走法见模块文档那张表。"""

    name = "web_mail"

    def __init__(
        self,
        config: ExtractConfig,
        *,
        max_chars: int = MAX_CHARS,
        depth: int | None = None,
    ) -> None:
        self._config = config
        #: 可注入，默认 `MAX_CHARS`：测试要能在**不造一个 1 MB 的文件**的前提下把那条
        #: 截断路径走一遍（与 `office.OfficeExtractor` 同一个写法）。
        self._max_chars = max_chars
        #: **默认取配置**（`[extract] email_attachment_depth`）。可注入是为了让测试能
        #: 在不改配置对象的前提下把 0 / 1 / 2 三档都走一遍。
        self._depth = config.email_attachment_depth if depth is None else depth

    def supports(self, ext: str, mime: str) -> bool:
        """只有那七个扩展名。`mime` 一个字符不用（理由在 `base.mime_of()`）。"""
        del mime
        return ext in WEB_MAIL_EXTS

    def extract(self, path: str) -> ExtractedDoc:
        ext = ext_of(path)
        if ext not in WEB_MAIL_EXTS:
            # 走到这里说明有人绕过了 `supports()`（`extract_file()` 不会）。照实说，
            # 不假装成功——"注册了但返回空正文"是最难查的一种失败。
            raise UnsupportedFormat(f"web_mail 不收 .{ext}（supports() 本该拦住它）")

        data = read_bytes(path, config=self._config)
        cap = _Text(self._max_chars)

        if ext in HTML_EXTS:
            decoded = decode_bytes(data)
            warnings = [decoded.warning] if decoded.warning else []
            parsed = _parse_html(decoded.text, cap, warnings)
        elif ext in MHT_EXTS:
            parsed = _parse_mht(data, cap)
        else:
            parsed = _parse_mail(data, ext, cap, self._depth)

        out = list(parsed.warnings)
        if cap.truncated:
            out.append(
                f"正文超过 {self._max_chars} 字符的上限，后面的内容没有提取"
                "（上限是 web_mail.py 的 MAX_CHARS）"
            )
        if not cap.has_body:
            # 0 字节与"有内容但都不是正文"是两件事：前者没什么好猜的，后者要给个方向。
            out.append(
                "文件是 0 字节，没有任何内容可提取"
                if not data
                else f"解析成功但没有提取到任何文本：{_EMPTY_HINTS[ext]}"
            )

        return ExtractedDoc(
            text=parsed.text,
            title=parsed.title,
            meta={**parsed.meta, "format": ext, "bytes": len(data), "chars": len(parsed.text)},
            modality="text",
            source=self.name,
            truncated=cap.truncated,
            warnings=tuple(out),
        )


def build(config: ExtractConfig) -> Extractor | None:
    """按 `[extract] web_mail` 决定建不建；关掉时返回 `None`（不注册，理由见 `plaintext.build()`）。"""
    if not config.web_mail:
        return None
    return WebMailExtractor(config)
