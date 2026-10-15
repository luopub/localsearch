r"""提取层的公共形态：派发、读字节、判编码（P3）。

这一层回答三个问题，**只有三个**：一个文件归哪个提取器、字节怎么读进来、字节是什么
编码。至于"PDF 怎么解析"之类的事，全在各自的提取器里，这里一行都没有。

------------------------------------------------------------------------------
派发：看扩展名，按注册顺序取第一个认领的
------------------------------------------------------------------------------

`supports(ext, mime)` 收两个信号，但**判定一律以 `ext` 为准**。`mime` 是从
`mimetypes.guess_type()` 来的，而在 Windows 上这个函数会去读注册表——同一台机器上
装了不同的软件，`.md` 的 mime 就可能不一样（实测过的典型：`.js` 会被若干编辑器改成
`text/plain` 之外的东西）。拿它当主判据，等于让"本机装过什么软件"决定索引结果，
那是没法复现的。所以 `mime` 只作为**次要信号**存在（例如同名扩展名想区分 mht 与
eml 时），任何一条测试都不许依赖它。

没有提取器认领时**明确失败**（`UnsupportedFormat`），**不兜底**：

- 不做"扩展名不认识就当纯文本试试"。全盘上 `.bin` / `.dat` / `.db` / 无扩展名的
  二进制多得是，兜底等于把它们的字节当文本灌进 FTS——`A6` 要的是失败隔离，不是
  "尽量塞点东西进去"。
- 只有**完全没有扩展名**的文件例外，而且那条例外写在 `plaintext.py` 里、带二进制
  检查（`README` / `Makefile` / `.gitignore` 这类真的没有扩展名）。

------------------------------------------------------------------------------
打开文件一律先过 `core.paths.extended_path`
------------------------------------------------------------------------------

这是 `F2-02` 那轮真机核对留下的债，**不能只在扫描器里修**：路径最后一段是 `nul`
一族时，原样 `open()` **不报错**、读到 **0 字节**（`E27`）。扫描器已把这类条目正常
收进来了（它们是真文件），所以**提取层是它们真正出事的地方**——一份 83 字节的正文
会被静默索引成空正文，而"搜不到"没有任何错误提示。

所以本模块的 `read_bytes()` 是**唯一的读盘入口**，它第一行就过 `extended_path`。
哪个提取器自己写 `open()` / `Path.read_bytes()`，就等于把这条债重新背回来——
`tests/unit/test_extract_plaintext.py` 里有一条专门建真 `nul` 文件的测试盯着它。

返回的路上**一律用原样路径**：`\\?\` 是 Win32 的实现细节，不许进 `meta`、不许进
日志里当路径用（`core/paths.py` 那条规则）。

------------------------------------------------------------------------------
读进来的字节有个上限，超了就是**失败**，不是截断
------------------------------------------------------------------------------

上限来自 `[scan] max_file_mb`（**不新增配置项**：同一件事只许有一个真相来源）。
正常情况下这里永远不该超——扫描器已经按同一个上限把超限文件判成 `NAME_ONLY` 了。
真超了只有两种可能：文件在扫描之后长到了上限以上，或者有人绕开扫描器直接调用。
两种都该**出声**：静默截断会让"这个文件为什么只搜到前半段"变成一个没有痕迹的问题。

（`max_bytes = 0` 的语义与扫描器一致，是字面的：任何非空文件都超限。）

------------------------------------------------------------------------------
编码：四档，猜的那一档要留下痕迹
------------------------------------------------------------------------------

1. **BOM**（UTF-8 / UTF-16 LE·BE / UTF-32 LE·BE）——最硬的证据，直接定案；
2. **严格 UTF-8**——要么是、要么不是，没有歧义，且便宜；
3. **短数据（≤ 32 字节）：`gb18030` 严格解码**——**整档跳过探测器**，理由见 `SHORT_BYTES`；
4. **`charset-normalizer`**——长数据的主力，中文 Windows 语料里的 GBK/GB18030 老文件
   靠它（计划 §7.4 加这个依赖就是为了这一档）；
5. **`gb18030` 严格解码**（长数据的兜底）；
6. 都不行才 `errors="replace"`，并且**标成有损**。

第 3 档起就是**猜**了，所以 `Decoded.guessed` 会一路传到 `ExtractedDoc.warnings`
里——乱码是"看不出来哪里错了"的典型，留一条警告才有得查。
"""

from __future__ import annotations

import codecs
import mimetypes
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from charset_normalizer import from_bytes

from localsearch.core.models import ExtractedDoc
from localsearch.core.paths import extended_path
from localsearch.core.registry import Registry

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不需要 core.config
    from localsearch.core.config import Config


# ==============================================================================
# 失败：四种，各带一个能写进 files.error_msg 的原因
# ==============================================================================


class ExtractError(Exception):
    """这个文件提取不了。**消息就是给用户看的理由**（`A6` 要它进 `files.error_msg`）。

    与 `ScanError` 的分工：那个是"这一轮开不起来"（根目录配错、后端没落地），
    这个只针对**单个文件**，而且**必须被调用方接住**——`F3-07` 的批量入口接住它、
    记下原因、继续下一个。**提取器里不许出现裸 `except:`**：真接不住的异常（内存、
    编码库自己的 bug）继续往上冒，那才是该让人看见的东西。
    """


class UnsupportedFormat(ExtractError):
    """没有提取器认领这个扩展名。**不是坏文件**——是这一类文件 v1 不提取正文。

    单独一个类是为了让 `P4` 能把它与"文件坏了"分开处置：前者应当留在 `NAME_ONLY`
    那一档（文件名照搜），后者是要在报表里露头的真失败。
    """


class FileUnreadable(ExtractError):
    """读盘就失败了：打不开、权限、读到一半没了、超过提取上限。"""


class ParseFailed(ExtractError):
    """字节读进来了，但**这个格式解析不出来**：结构坏了、要密码、不是它自称的那个格式。

    与 `UnsupportedFormat` 的分工是这份分类里最要紧的一条，因为两者的下游处置相反：

    | 异常 | 是什么 | `P4` 该做的 |
    |---|---|---|
    | `UnsupportedFormat` | 这一类文件 v1 **不做**（`.db` / 老式 `.doc`） | 留在 `NAME_ONLY`，文件名照搜，**不进失败报表** |
    | `ParseFailed` | 该做的格式，但**这一个文件坏了** | 进失败报表、原因写进 `files.error_msg` |

    混起来会同时坏两头：把"不支持"记成失败，报表里会塞满 `.db` 这种噪音，真坏的文件
    淹在里面；把"坏了"记成不支持，那些文件就**静默**降级成只搜文件名，而用户以为
    全文索引是完整。`A6` 要的是隔离，不是"悄悄少收几个文件"。

    **消息必须带上是哪个库、坏在哪一步**（`office.py` 里把库自己的异常名与消息也带上）——
    这条消息就是用户唯一能看见的东西，写"解析失败"四个字等于没写。
    """


class BinaryContent(ExtractError):
    """看着是二进制，拒绝当文本读（只有 `plaintext` 会抛）。"""


# ==============================================================================
# 提取真正需要的那几项
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ExtractConfig:
    """从 `core.config.Config` 摘出来的那几项。

    与 `ScanConfig` 同样的理由：提取器要能在**不碰 TOML、不碰配置文件**的前提下
    造出来，否则 `test_extract_plaintext.py` 里每条断言都得先铺一份配置目录。

    `max_bytes` 来自 `[scan] max_file_mb`，**不是** `[extract]` 里的一项——上限只有
    一个真相来源（扫描器与提取器必须用同一个数，否则会出现"扫描器说能提、提取器说
    太大"这种自相矛盾的账）。
    """

    text_code: bool = True
    office: bool = True
    web_mail: bool = True
    media_meta: bool = True
    #: 展开邮件的附件到第几层（计划 §4.5 的 `email_attachment_depth`）。
    email_attachment_depth: int = 1
    max_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_config(cls, config: "Config") -> "ExtractConfig":
        return cls(
            text_code=config.extract.text_code,
            office=config.extract.office,
            web_mail=config.extract.web_mail,
            media_meta=config.extract.media_meta,
            email_attachment_depth=config.extract.email_attachment_depth,
            max_bytes=config.scan.max_file_mb * 1024 * 1024,
        )


# ==============================================================================
# 接口
# ==============================================================================


@runtime_checkable
class Extractor(Protocol):
    """一类文件的正文怎么出来（计划 §4.2 的扩展点）。

    `supports` 与 `extract` 之间有一条**不写在类型里的约定**：`supports` 说"归我"
    之后，`extract` 要么给出 `ExtractedDoc`，要么抛 `ExtractError`——**不许返回空
    正文来搪塞**。空正文是一个合法结果（0 字节文件就是），所以"我不太确定但还是
    返回了空"在下游与"这个文件本来就是空的"完全分不开。
    """

    #: 注册表里的名字，也是 `ExtractedDoc.source` 的值。取类名不带 `Extractor` 后缀
    #: 的短名（`plaintext` / `office` / `web_mail` / `media_meta`）。
    name: str

    def supports(self, ext: str, mime: str) -> bool:
        """这个扩展名 / mime 归不归我。`ext` 是**小写、不带点**的扩展名，没有则空串。"""
        ...

    def extract(self, path: str) -> ExtractedDoc: ...


# ==============================================================================
# 路径 → 扩展名 / mime
# ==============================================================================


def ext_of(path: str) -> str:
    """小写、不带点的扩展名；没有扩展名返回空串。

    ⚠ 点开头的隐藏文件**算没有扩展名**：`os.path.splitext(".gitignore")` 给的是
    `(".gitignore", "")`。这是对的——`.gitignore` 是个纯文本文件，而不是"一个名叫
    空、扩展名为 gitignore 的文件"。`plaintext.py` 的无扩展名那一支正是靠这条接住它。
    """
    return os.path.splitext(os.path.basename(path))[1].lower().lstrip(".")


def mime_of(path: str) -> str:
    """`mimetypes` 猜的 mime，猜不出给空串。**只当次要信号用**——理由见模块文档。

    用 `strict=False`：严格的表只认标准扩展名，`jsonl` / `pyi` 这类会拿不到，
    而非严格档至少在 Linux/mac 上一台机器给同一答案（Windows 上它还会读注册表，
    所以仍然只是次要信号）。
    """
    guess, _encoding = mimetypes.guess_type(path, strict=False)
    return guess or ""


# ==============================================================================
# 读盘：唯一的入口
# ==============================================================================


def read_bytes(path: str, *, config: ExtractConfig) -> bytes:
    r"""把文件读进来（上限 `config.max_bytes`）。

    **第一行就是 `extended_path`**——这个模块存在的理由有一半在这条（见模块文档）。
    少写这一行不会报错，只会让 `nul` 那族文件读到 0 字节，而且**所有测试照样绿**
    （除非有一条测试专门去建真设备名文件），所以它旁边那条测试不是可选的。

    超上限抛 `FileUnreadable`，**不截断**：见模块文档那一节。
    """
    real = extended_path(path)
    try:
        with open(real, "rb") as handle:
            data = handle.read(config.max_bytes + 1)
    except OSError as exc:
        raise FileUnreadable(f"打不开：{exc.strerror or exc}") from exc
    if len(data) > config.max_bytes:
        raise FileUnreadable(
            f"超过提取上限 {config.max_bytes} 字节（[scan] max_file_mb 定的）："
            "扫描器本该把它判成「只记名字」，走到这里说明文件在上次扫描之后变大了，"
            "或有人绕开扫描器直接调用"
        )
    return data


# ==============================================================================
# 编码
# ==============================================================================

#: BOM → codec 名。**顺序有讲究**：UTF-32 LE 的 BOM（`FF FE 00 00`）以 UTF-16 LE 的
#: BOM（`FF FE`）开头，短的排前面就会把 UTF-32 的文件按 UTF-16 解出满屏 NUL。
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

#: 有 BOM 的一律按 BOM 解，没 BOM 的才轮到"严格 UTF-8 → 探测"。
_STRICT = "utf-8"

#: 探测结果的兜底，**也是短数据那一档的唯一选择**。放在 UTF-8 之后、
#: `errors="replace"` 之前，理由见模块文档。
_LAST_RESORT = "gb18030"

#: 二进制嗅探看前多少字节。
SNIFF_BYTES = 8192

#: "太短了，探测器不可信"的那条线：**这条线以内不再问探测器，直接按语料先验认 GB18030**。
#:
#: **是量出来的，不是拍的。** 2026-10-15 把一段连续的中文按 2–60 字逐长度切成 59 档，
#: 每档 GBK 编码后分别用三种走法解，还原成原文的档数：
#:
#: | 走法 | 判对的档数 |
#: |---|---|
#: | 自由模式（`charset_normalizer` 自己挑） | 46 / 59 |
#: | 自由模式 + 短数据收窄候选表 | 57 / 59 |
#: | **短数据直接认 GB18030** | **59 / 59** |
#:
#: 自由模式判错的档位**全部在 4–28 字节（2–14 字）之间**：前两档（4 与 6 字节）猜
#: `big5`、其余 11 档全猜 `cp949`（韩文），都是乱码且**不抛异常**；**30 字节起全部判对**。
#: 收窄候选表修掉了那 11 档，剩下的两档连收窄表也猜成 `big5`——那是**原理上分不开**的：
#: `C4 E3 BA C3` 在 GBK 里是"你好"，在 Big5 里也是两个合法汉字，字节本身没有更多信息。
#:
#: 所以这条线取 **32 字节**：实测失败档的上界 28 再加一档余量。
#:
#: **复跑**：`PYTHONIOENCODING=utf-8 uv run python tools/bench_encoding.py`——那三个数、
#: 逐档明细表、"哪几档猜错"、以及这条线的代价（短韩文 / 短日文变乱码）都在它的输出里。
#: 读数记在 `PROGRESS.md` 的 **E31**。（它同时给出另一个数：短数据上跳过探测器**快 96 倍**——
#: 2.63 µs 对 252 µs。这里原先写的是 98 倍，是脚本第三段修好之前留下的一个数，与
#: 最终读数对不上，`2026-10-15` 一并改正。）
SHORT_BYTES = 32


@dataclass(frozen=True, slots=True)
class Decoded:
    """一段字节解出来的文本，以及**怎么解的**。

    `encoding` 是真正用上的 codec 名（能直接交回 `str.encode()`），最后一档兜底时是
    `utf-8` + `lossy=True`。三个标志分开记，是因为它们导向三种不同的处置：

    | 标志 | 含义 | 该做的事 |
    |---|---|---|
    | `guessed` | 不是 BOM 或严格 UTF-8 定的，是探测猜的 | 留一条警告；乱码时先查这里 |
    | `lossy` | 有字节没能原样还原（替换过） | 正文缺字，必须留警告 |
    | 都 False | 硬证据定的 | 什么都不用说 |
    """

    text: str
    encoding: str
    guessed: bool = False
    lossy: bool = False

    @property
    def warning(self) -> str | None:
        """该写进 `ExtractedDoc.warnings` 的那句话（没得说就 `None`）。"""
        if self.lossy:
            return f"编码 {self.encoding}，其中有字节无法还原，已替换（正文缺字）"
        if self.guessed:
            return f"编码是猜出来的（{self.encoding}），不是 BOM 或严格 UTF-8；乱码请先查这里"
        return None


def looks_binary(data: bytes) -> bool:
    r"""前 `SNIFF_BYTES` 字节里有没有 NUL——有就当成二进制。

    判据只有这一条，因为它是**便宜且不会误伤**的那一条：UTF-8 / GBK / GB18030 / 各种
    Latin 编码的文本里都不会出现 `0x00`。反过来不成立（不是二进制就一定没有 NUL），
    但漏判的后果只是"这段二进制被当文本读出来了"，而**误判**的后果是"用户的正常文件
    被拒绝提取"——两者的代价不对称，所以判据宁可写窄。

    已知会漏的一种：**没有 BOM 的 UTF-16**。它的 ASCII 字符后半字节就是 `0x00`，
    所以它**会被判成二进制**。这是有意接受的：无 BOM 的 UTF-16 在本机语料里罕见，
    而按 UTF-16 去试着解一个正常的 GBK 文件才是真会出事的写法。
    """
    return b"\x00" in data[:SNIFF_BYTES]


def decode_bytes(data: bytes) -> Decoded:
    """按模块文档那五档解一段字节。**永不抛异常**（最后一档是有损替换）。

    空的字节串返回 `("", "utf-8")` 且两个标志都不置——0 字节文件是合法输入，
    不该被记成"猜出来的编码"。

    ⚠ **第 3 档对短数据是整档跳过的**，这条不是洁癖，是实测逼出来的——**数据在
    `SHORT_BYTES` 的文档里**（59 档逐长度的对照：自由模式对 46、收窄候选表对 57、
    直接认 GB18030 对 59），这里只说结论与代价：

    - **`SHORT_BYTES` 以内（32 字节）**：不再问探测器，严格试 `gb18030`，解得下来就用它。
      本机语料以中文 Windows 为主（计划 §7.4 就是为这批 GBK/GB18030 老文件加的
      `charset-normalizer`），而实测在这一档上"中文优先"是**唯一 100% 正确的走法**。
    - **超过这条线**：一个字都不干预，全交给探测器（长数据的读数是 30 字节起全对，
      而且长韩文 `cp949`、长俄文 `cp1251`、长繁体 `big5` 都判得对——那些正是干预会
      毁掉的东西）。
    - **代价是量过的，不藏着**：短韩文、短日文、短德文在这一档会变成中文乱码，而
      自由模式本来能判对其中前两种。这条取舍的依据是**语料分布**，不是"那几种语言
      不重要"。`PROGRESS.md` 的 P3「遗留」里记着这一条与它的翻盘条件（盘上真出现
      韩文语料就把 `SHORT_BYTES` 调回 0，或者加一档语言判断）。
    - `gb18030` 解不下来时**继续往下走**（它会失败——4 字节序列有非法形态），
      所以这一档不会把文件卡死，最坏就是回到探测器那一档。
    """
    if not data:
        return Decoded("", _STRICT)

    for bom, encoding in _BOMS:
        if data.startswith(bom):
            # BOM 是硬证据，但**不保证内容干净**：一段被截断的 UTF-8 文件同样可能带着
            # BOM 开头。所以这里也留了有损的出口，而不是让它抛出去变成"文件坏了"。
            try:
                return Decoded(data.decode(encoding), encoding)
            except UnicodeDecodeError:
                return Decoded(data.decode(encoding, errors="replace"), encoding, lossy=True)

    try:
        return Decoded(data.decode(_STRICT), _STRICT)
    except UnicodeDecodeError:
        pass

    if len(data) <= SHORT_BYTES:
        # 短数据：不问探测器，按语料先验认 GB18030（读数与代价见函数文档）。
        try:
            return Decoded(data.decode(_LAST_RESORT), _LAST_RESORT, guessed=True)
        except UnicodeDecodeError:
            pass                                  # 解不下来就往下走，不留在这里

    best = from_bytes(data).best()

    if best is not None and best.encoding:
        encoding = best.encoding.lower()
        try:
            return Decoded(data.decode(encoding), encoding, guessed=True)
        except (UnicodeDecodeError, LookupError):
            # 探测给了一个名字但解不动：`LookupError` 是本机的编解码器表里没有、
            # `UnicodeDecodeError` 是它自己也解不全。两种都往下走，不留在这里。
            pass

    try:
        return Decoded(data.decode(_LAST_RESORT), _LAST_RESORT, guessed=True)
    except UnicodeDecodeError:
        return Decoded(data.decode(_STRICT, errors="replace"), _STRICT, guessed=True, lossy=True)


def read_text(path: str, *, config: ExtractConfig) -> Decoded:
    """读 + 解，合成一步（`plaintext` 与 `web_mail` 的正文都走这里）。"""
    return decode_bytes(read_bytes(path, config=config))


# ==============================================================================
# 派发
# ==============================================================================


def find_extractor(path: str, *, registry: Registry[Extractor]) -> Extractor | None:
    """这个文件归谁；没人认领返回 `None`（**不抛**，见 `Registry.first_match`）。"""
    ext = ext_of(path)
    mime = mime_of(path)
    return registry.first_match(lambda e: e.supports(ext, mime))


def extract_file(
    path: str,
    *,
    registry: Registry[Extractor],
    config: ExtractConfig,
) -> ExtractedDoc:
    """派发 + 提取。**单个文件这一档的入口**（批量那一档在 `F3-07`）。

    这里只做一件事：没人认领时把 `None` 变成一个带原因的 `UnsupportedFormat`。
    `config` 目前只递下去（提取器各自持有它），保留这个参数是因为调用点需要一个
    **与提取器无关的**入口——批量层与 `lsearch explain` 不该知道有哪几个提取器。
    """
    del config  # 提取器自己持有 config；这里留着是为了签名稳定，见文档
    extractor = find_extractor(path, registry=registry)
    if extractor is None:
        ext = ext_of(path)
        raise UnsupportedFormat(
            f"没有提取器认领 {('.' + ext) if ext else '无扩展名'}（{path}）"
        )
    return extractor.extract(path)


def with_warning(doc: ExtractedDoc, warning: str | None) -> ExtractedDoc:
    """给 `doc` 追一条警告（`warning` 为 `None` 时原样返回）。

    `warnings` 是元组、`ExtractedDoc` 是 frozen，所以追加只能靠 `replace`。写成这个
    小函数是为了让调用点读起来是"记一条警告"而不是"我在这儿造了个新对象"。
    """
    if warning is None:
        return doc
    return replace(doc, warnings=(*doc.warnings, warning))


def meta_str(value: Any) -> str:
    """把要进 `meta` 的值收敛成短字符串（`None` → 空串）。

    `meta` 是给人看与排查用的，不是给机器比的——所以这里不保留类型，一律字符串化，
    并且**截断到 200 字符**：EXIF 里有些字段能是几 KB 的二进制描述，原样塞进日志会
    把有用的一行淹掉。
    """
    if value is None:
        return ""
    text = str(value).strip()
    return text[:200]
