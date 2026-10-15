r"""`extract/batch.py`：一个文件出事，不许带走整轮（`F3-07`，验收项 `A6`）。

------------------------------------------------------------------------------
这一组为什么这样切
------------------------------------------------------------------------------

`A6` 的原话是"单文件解析失败不中断整轮索引，失败原因计入 `files.error_msg`"。它有两个
半边，这一组盯的是**前半个**（不中断）与"原因确实出来了"；**后半个**（真写进数据库那一列）
在 `P4`，`files` 表都还没建。

所以这里证明的是四件事，各自都能单独红：

1. **每个路径恰好一条 `Outcome`、顺序不变**——"不中断"的可断言形态就是这个：
   一个文件出事时是**换回一条失败**，而不是抛出来。缺一条与多一条都要红。
2. **坏文件后面的好文件照样出正文**——语料**故意好坏交替、以好文件收尾**。要是实现是
   "遇到失败就 `break`"或者"把剩下的一起记成失败"，第一组与第二组测试会红。
3. **四种失败各自翻成对的异常类、`status` 与一句话**——用**真的四个提取器**、真造的
   坏文件，不注入替身（`PROGRESS.md` §2 里定的：这一条的被测对象就是这四个提取器）。
4. **提取器自己炸了也不带走整轮**，而且**单列计数**——那三个提取器模块"异常族名单漏
   一个"的侦测器，是唯一用替身的地方。

另外钉死几条**契约**（不是实现细节，是 `P4` 会依赖的）：`UnsupportedFormat` 不写
`error_msg`、`status` 那三个字符串就是 schema 里那三个、`error_msg` 折成一行且有上限、
`BaseException` 不被接住（Ctrl-C 必须停得下来）、空正文不是失败。理由都写在
`batch.py` 的模块文档里，这里只把它们钉住。

⚠ **语料是现场造的，不是 `tests/fixtures/` 里的**——`F3-06` 才建那批 fixtures，它在
计划里的顺序排在 `F3-07` 之后（那批样本收的是前面一路上造出来的形态）。这一组自带的
语料因此与四个提取器自己的测试文件里那批**有意重复**：那批证明"单个文件会怎么失败"，
这一组证明"它们一起喂进批处理会怎么样"。**不要**为了省事去 import 兄弟测试文件的
helper——那会让这一组跟着别人的重构一起红，而它要盯的东西跟那些 helper 无关。

⚠ **`tmp_path` 落在 `C:` 下**（项目只允许碰 `C:` 与 `D:`）。
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from localsearch.core.models import ExtractedDoc
from localsearch.core.registry import Registry
from localsearch.extract import (
    ERROR_MSG_MAX,
    BinaryContent,
    ExtractConfig,
    Extractor,
    FileUnreadable,
    Outcome,
    ParseFailed,
    Tally,
    UnsupportedFormat,
    build_registry,
    extract_many,
)
from localsearch.extract import ExtractedDoc as ReExportedDoc

# ==============================================================================
# 语料：五个好文件、八种坏法（好坏交替，以好文件收尾）
# ==============================================================================

#: 结局的种类。`text` 是出正文，其余四种各自对应一个异常类（表在后面）。
KIND_TEXT = "text"
KIND_UNSUPPORTED = "unsupported"
KIND_BROKEN = "broken"
KIND_BINARY = "binary"
KIND_UNREADABLE = "unreadable"

#: 名字 → 期望的结局。四类失败各出现至少一次，**而且按 `A6` 的字面要求交替排**：
#: 每一个坏文件后面都跟着一个好文件。
CORPUS: tuple[tuple[str, str], ...] = (
    ("a.txt", KIND_TEXT),
    ("b.qqq", KIND_UNSUPPORTED),      # 这一类 v1 不做：`skipped`，不进失败报表
    ("c.html", KIND_TEXT),
    ("d.mht", KIND_BROKEN),           # 该做的格式，这一个坏了
    ("e.pdf", KIND_TEXT),
    ("f.pdf", KIND_BROKEN),
    ("g.jpg", KIND_TEXT),
    ("h.jpg", KIND_BROKEN),
    ("i.dat", KIND_UNSUPPORTED),
    ("BLOBBY", KIND_BINARY),          # 无扩展名 + NUL 字节（`plaintext` 那一支的兜底）
    ("gone.txt", KIND_UNREADABLE),    # 扫描之后被删掉：`FileUnreadable`
    ("adir", KIND_UNREADABLE),        # 传来的根本不是文件
    ("z.md", KIND_TEXT),              # 最后一个：前面十二个都没把它挡在后面
)

#: 四种失败 → 该抛的异常类。**判据只用 `isinstance`**，不看消息——消息属于提取器
#: 自己的测试文件（那里逐条钉着"要带上库报的话"）。
EXPECTED_ERROR: dict[str, type[Exception]] = {
    KIND_UNSUPPORTED: UnsupportedFormat,
    KIND_BROKEN: ParseFailed,
    KIND_BINARY: BinaryContent,
    KIND_UNREADABLE: FileUnreadable,
}

#: 结局 → `files.status` 该写成什么。**这里写的是字面量，不是 `batch` 里的常量**：
#: 用常量的话，"常量被改成别的字符串"这种错就两处一起改、测试照样绿，而它正是
#: `P4` 建库时要对齐的那个值。
EXPECTED_STATUS: dict[str, str] = {
    KIND_TEXT: "fulltext",
    KIND_UNSUPPORTED: "skipped",
    KIND_BROKEN: "error",
    KIND_BINARY: "error",
    KIND_UNREADABLE: "error",
}

#: 坏文件里的字节。`b"junk" * 50` 是 `test_extract_office.py` 用过的形态：PDF 与 mht
#: 都栽在"头不对"上，jpg 栽在 Pillow 认不出上，三者的失败消息各不相同（这一组不管消息）。
JUNK = b"junk" * 50

#: `BLOBBY` 的内容：有扩展名的名单外文件**不会**被嗅探（`plaintext` 的模块文档），
#: 所以只有无扩展名这一支会走到 `BinaryContent`。
BINARY_BYTES = b"\x00\x01\x02 binary \x00 stuff"


def write(tmp_path, name: str, data: bytes) -> str:
    """往 `tmp_path` 里落一个文件，返回**原样路径**（不是 `Path`——被测层收 `str`）。"""
    target = os.path.join(str(tmp_path), name)
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def jpeg_with_exif(tmp_path, name: str) -> str:
    """一张带 EXIF 的极小 JPEG。**没有 EXIF 的 jpg 正文是空的**（`media_meta` 只出
    文件名与 EXIF），那会让"好文件出正文"那条断言变成在测别的东西（`2026-10-16` 实测）。
    """
    exif = Image.Exif()
    exif[271] = "Canon"
    exif[272] = "EOS R5"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (5, 5, 5)).save(buffer, "JPEG", exif=exif.tobytes())
    return write(tmp_path, name, buffer.getvalue())


def pdf_with_text(tmp_path, name: str) -> str:
    """一页有文字层的 PDF。用被测库自己写（与 `test_extract_office.py` 同一个写法）。

    只写 ASCII：这一组要的是"能出正文"，中文那边有 `CJK_FONT` 的专门测试盯着，
    这里引入字体别名只会在别的机器上多一个失败点。
    """
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "hello localsearch", fontsize=14)
    data = document.tobytes()
    document.close()
    return write(tmp_path, name, data)


@pytest.fixture
def corpus(tmp_path):
    """造语料，返回 `[(路径, 结局), ...]`，顺序与喂给 `extract_many` 的完全一致。"""
    os.mkdir(str(tmp_path / "adir"))
    made: list[tuple[str, str]] = []
    for name, kind in CORPUS:
        if kind == KIND_UNSUPPORTED:
            path = write(tmp_path, name, b"whatever")
        elif kind == KIND_BROKEN:
            path = write(tmp_path, name, JUNK)
        elif kind == KIND_BINARY:
            path = write(tmp_path, name, BINARY_BYTES)
        elif kind == KIND_UNREADABLE:
            path = str(tmp_path / name)  # `gone.txt` 不创建、`adir` 是目录
        elif name.endswith(".html"):
            path = write(tmp_path, name, b"<html><body><p>hi there</p></body></html>")
        elif name.endswith(".pdf"):
            path = pdf_with_text(tmp_path, name)
        elif name.endswith(".jpg"):
            path = jpeg_with_exif(tmp_path, name)
        else:
            path = write(tmp_path, name, b"hello world\n")
        made.append((path, kind))
    return made


@pytest.fixture
def registry():
    """**真的四个提取器**（`build_registry()` 的默认装配，四个开关都开着）。"""
    return build_registry(ExtractConfig())


@pytest.fixture
def config():
    return ExtractConfig()


def run(corpus, *, registry, config) -> list[Outcome]:
    """把语料整批喂进去，收成列表（`list()` 就是把生成器走完）。"""
    return list(extract_many([path for path, _ in corpus], registry=registry, config=config))


# ==============================================================================
# 一、不中断整轮：每个路径恰好一条，顺序不变
# ==============================================================================


def test_one_outcome_per_path_in_input_order(corpus, registry, config):
    """`A6` 的可断言形态：**一条不少、一条不多、顺序一致**。

    实现若是"遇到失败就 `break`"，长度会短一截；若是"跳过不产出"，长度也会短；
    若是"把剩下的路径一起记成失败"，长度对但内容与顺序对不上——所以三个都要断言。
    """
    outcomes = run(corpus, registry=registry, config=config)

    assert len(outcomes) == len(corpus)
    assert [one.path for one in outcomes] == [path for path, _ in corpus]


def test_the_last_good_file_still_gets_its_text(corpus, registry, config):
    """语料以好文件收尾，且中间夹着十二个坏文件——整轮没被带走，最后那条就是证据。"""
    outcomes = run(corpus, registry=registry, config=config)

    last_path, last_kind = corpus[-1]
    assert last_kind == KIND_TEXT
    assert outcomes[-1].path == last_path
    assert outcomes[-1].ok
    assert outcomes[-1].doc.text.strip()
    assert outcomes[-1].error_msg == ""


def test_the_four_real_extractors_are_the_ones_answering(corpus, registry, config):
    """好文件里有四个来源各一份，一个都不能少。

    少一个来源就说明语料退化了（比如 `.jpg` 那条被改成无 EXIF 的形态而没人在意），
    那时这一组仍然会绿——但它盯的东西已经不在场上。
    """
    outcomes = [one for one in run(corpus, registry=registry, config=config) if one.ok]

    assert {one.doc.source for one in outcomes} == {
        "plaintext",
        "office",
        "web_mail",
        "media_meta",
    }


# ==============================================================================
# 二、四种失败各自翻成对的异常类与一句话
# ==============================================================================


@pytest.mark.parametrize(
    "index,name,kind",
    [
        (index, name, kind)
        for index, (name, kind) in enumerate(CORPUS)
        if kind != KIND_TEXT
    ],
)
def test_each_kind_of_failure_comes_back_as_a_reason(
    index, name, kind, corpus, registry, config
):
    """每一个坏文件都换回一条**带原因的结局**，而不是一个异常。

    原因里那句消息由提取器负责（各自的测试文件逐条盯着），这里只管三条：
    异常类对得上、`status` 对得上、该写原因的那几类确实写了。

    `UnsupportedFormat` **故意**是这一组里唯一不写原因的（`error_msg` 为空）——
    它不是"这个文件坏了"，是"这一类 v1 不做"；判据在 `batch.py` 的模块文档里，
    另一条测试专门钉着它。
    """
    outcome = run(corpus, registry=registry, config=config)[index]

    assert outcome.path.endswith(name)
    assert outcome.doc is None
    assert isinstance(outcome.error, EXPECTED_ERROR[kind])
    assert outcome.status == EXPECTED_STATUS[kind]

    if kind == KIND_UNSUPPORTED:
        assert outcome.error_msg == ""
    else:
        assert outcome.error_msg.startswith(type(outcome.error).__name__ + ": ")
        assert len(outcome.error_msg) > len(type(outcome.error).__name__) + 2


def test_unsupported_formats_stay_out_of_the_failure_report(corpus, registry, config):
    """`UnsupportedFormat` → `skipped` + **空的 `error_msg`**，而且不计进失败数。

    `error_msg` 非空是"失败报表"的唯一判据。全盘上 `.db` / `.dat` / `.exe` 多得是，
    把"这一类不做"也填上原因，真坏的文件就淹在噪音里了（`base.UnsupportedFormat`
    与 `batch.py` 的模块文档都写了这条分工，这里把它钉住）。
    """
    outcomes = run(corpus, registry=registry, config=config)
    tally = Tally()
    for outcome in outcomes:
        tally.add(outcome)

    skipped = [one for one in outcomes if one.status == "skipped"]
    expected = sum(1 for _, kind in CORPUS if kind == KIND_UNSUPPORTED)
    assert len(skipped) == expected
    assert all(one.error_msg == "" for one in skipped)
    assert tally.skipped == expected
    assert tally.total - tally.skipped - tally.fulltext == tally.error


def test_the_status_strings_are_the_ones_the_schema_uses():
    """三个 `status` 字面量：计划 §4.3 的 `files.status` 里就是这三个词。

    单独一条是因为它**跨文件**：`batch.py` 的常量与计划里的 schema 要一致，而计划
    那边不会因为这里改了而红——反过来，这里改了而计划没改，`P4` 写出来的行就是查不到的。
    """
    from localsearch.extract import batch

    assert batch.STATUS_FULLTEXT == "fulltext"
    assert batch.STATUS_SKIPPED == "skipped"
    assert batch.STATUS_ERROR == "error"


# ==============================================================================
# 三、替身：懒、提取器自己炸了、Ctrl-C
# ==============================================================================


class _Scripted:
    """按剧本行事的替身提取器：`raises` 写什么就抛什么（`BaseException` 也照抛），
    不写就返回一个正文；每次 `extract()` 都记一笔调用，好断言"走没走到"。

    它模拟的是**提取器模块的异常族名单里漏了一个**——那个漏掉的族会一路冒出来。
    真提取器的名单今天没有漏（`F3-02` / `F3-03` / `F3-04` 各自补过），所以这一档
    只能用替身造。
    """

    name = "scripted"

    def __init__(
        self,
        *,
        claimed: tuple[str, ...] = ("txt",),
        raises: BaseException | None = None,
    ) -> None:
        self._claimed = claimed
        self._raises = raises
        self.calls: list[str] = []

    def supports(self, ext: str, mime: str) -> bool:
        del mime
        return ext in self._claimed

    def extract(self, path: str) -> ExtractedDoc:
        self.calls.append(path)
        if self._raises is not None:
            raise self._raises
        return ExtractedDoc(text="来自替身", source=self.name)


def only(scripted: _Scripted) -> Registry[Extractor]:
    """一个只装了替身的注册表。"""
    registry: Registry[Extractor] = Registry("extractor")
    registry.register(scripted.name, scripted)
    return registry


def test_the_batch_is_lazy_until_consumed(tmp_path, config):
    """不迭代就一个都不做——`P4` 要在循环里打进度，`break` 也真的要能停下来。

    判据是**替身的调用记录**：调用 `extract_many()` 之后它必须是空的（返回列表的实现
    在这里就会红），`next()` 之后才有且只有第一条。用"造一个会被删掉的文件"那种写法
    是挡不住的：会接异常的实现把 `FileUnreadable` 做成了一条 `Outcome`，列表照样能 `next()`。
    """
    scripted = _Scripted()
    path = write(tmp_path, "a.txt", b"hello\n")

    pending = extract_many([path], registry=only(scripted), config=config)
    assert scripted.calls == []

    outcome = next(pending)
    assert scripted.calls == [path]
    assert outcome.ok


def test_an_unexpected_exception_is_caught_and_marked(tmp_path, config):
    """一个**没人预料到**的异常：照样记一笔、**前后两个好文件都不受影响**、单列计数。

    这条是 `except Exception` 的护栏：把它收窄成 `except ExtractError`（看起来更"精确"）
    这一条就红。收窄的后果不是少收一个文件，而是**整轮索引白跑**。

    语料是"真好文件 / 炸掉的替身 / 真好文件"——真提取器和替身装在同一个注册表里，
    这就是 `P4` 实际会遇到的形态：四个提取器里有一个的异常族名单漏了一格。
    """
    scripted = _Scripted(claimed=("boom",), raises=RuntimeError("替身炸了"))
    registry = build_registry(config)
    registry.register(scripted.name, scripted)      # `boom` 没人跟它抢，注册顺序无所谓
    first = write(tmp_path, "a.txt", b"hello\n")
    exploding = write(tmp_path, "b.boom", b"\x00\x01")
    last = write(tmp_path, "c.html", b"<html><body><p>hi there</p></body></html>")

    outcomes = list(extract_many([first, exploding, last], registry=registry, config=config))
    tally = Tally()
    for outcome in outcomes:
        tally.add(outcome)

    boom = outcomes[1]
    assert [one.ok for one in outcomes] == [True, False, True]
    assert scripted.calls == [exploding]                # 它自己的前后都没被丢下
    assert boom.status == "error"
    assert boom.error is None
    assert isinstance(boom.unexpected, RuntimeError)   # 异常对象本身留着，要复现就再抛一次
    assert boom.error_msg.startswith("未预期的异常 RuntimeError: ")
    assert "替身炸了" in boom.error_msg
    assert (tally.total, tally.fulltext, tally.error, tally.unexpected) == (3, 2, 1, 1)
    assert tally.has_failures


def test_ctrl_c_is_not_swallowed(tmp_path, config):
    """`KeyboardInterrupt` 必须一路冒出去。

    接住它的症状是"按了 Ctrl-C 没反应，而且一点提示都没有"——全盘跑起来时那是最气人的
    一种失败。把 `except Exception` 改成 `except BaseException` 这一条就红。
    """
    scripted = _Scripted(raises=KeyboardInterrupt())
    path = write(tmp_path, "a.txt", b"hello\n")

    with pytest.raises(KeyboardInterrupt):
        list(extract_many([path], registry=only(scripted), config=config))


# ==============================================================================
# 四、契约：空正文、去重、计数
# ==============================================================================


def test_an_empty_document_is_not_a_failure(tmp_path, registry, config):
    """0 字节的 `.txt` 出的是**空正文**，不是失败——空正文是合法结果（`base.Extractor`）。

    这条挡的是"顺手加一句：正文为空就记失败"。加了之后 0 字节文件会变成一行
    `files.error_msg`，而它什么都没坏。
    """
    path = write(tmp_path, "empty.txt", b"")

    outcome = list(extract_many([path], registry=registry, config=config))[0]

    assert outcome.ok
    assert outcome.doc.text == ""
    assert outcome.status == "fulltext"
    assert outcome.error_msg == ""


def test_an_empty_batch_yields_nothing(registry, config):
    assert list(extract_many([], registry=registry, config=config)) == []


def test_duplicate_paths_are_not_merged(tmp_path, registry, config):
    """同一个路径出现两次就产出两条——**去重不是这一层的活**（`A9` 的去重在 `ingest`）。

    在这一层合并看起来是"顺手做好事"，代价是把"这件事做过几次"抹掉，
    而进度显示要的正是次数。
    """
    path = write(tmp_path, "a.txt", b"hello\n")

    outcomes = list(extract_many([path, path], registry=registry, config=config))

    assert len(outcomes) == 2
    assert [one.path for one in outcomes] == [path, path]
    assert all(one.ok for one in outcomes)


def test_tally_counts_by_status_and_flags_failures(corpus, registry, config):
    """整轮计数：四类各归各的桶，`unexpected` 是 `error` 的子集，`has_failures` 说话。"""
    tally = Tally()
    for outcome in extract_many(
        [path for path, _ in corpus], registry=registry, config=config
    ):
        assert tally.add(outcome) is outcome  # 返回它接到的那个，好让 map() 串得起来

    kinds = [kind for _, kind in CORPUS]
    assert tally.total == len(CORPUS)
    assert tally.fulltext == kinds.count(KIND_TEXT)
    assert tally.skipped == kinds.count(KIND_UNSUPPORTED)
    assert tally.error == len(CORPUS) - kinds.count(KIND_TEXT) - kinds.count(KIND_UNSUPPORTED)
    assert tally.unexpected == 0          # 真提取器一次都没炸
    assert tally.has_failures


def test_error_msg_is_one_short_line_with_the_class_name(tmp_path, config):
    """`error_msg` 的形态：`<类名>: <消息>`、**一行**、有上限（超了带 `...`）。

    换行与制表符会被折成空格：这一格会进表格、进日志行，带着换行的消息能把一整屏输出
    搅乱。上限的理由与 `base.meta_str` 同一个（那是给人看的一行），只多一个标记——
    错误消息的尾巴常常是库真正想说的那句话，截过就得留痕。
    """
    long_message = "第一行\n第二行\t带制表符" + "长" * (ERROR_MSG_MAX * 2)
    path = write(tmp_path, "a.txt", b"hello\n")

    outcome = list(
        extract_many(
            [path], registry=only(_Scripted(raises=ParseFailed(long_message))), config=config
        )
    )[0]

    assert outcome.error_msg.startswith("ParseFailed: ")
    assert "\n" not in outcome.error_msg
    assert "\t" not in outcome.error_msg
    assert "第一行 第二行 带制表符" in outcome.error_msg  # 折成一行，不是删掉
    assert len(outcome.error_msg) == ERROR_MSG_MAX
    assert outcome.error_msg.endswith("...")


# ==============================================================================
# 五、`Outcome` 自己的不变量
# ==============================================================================


def test_outcome_refuses_anything_but_exactly_one_of_the_three():
    """`doc` / `error` / `unexpected` **恰好一个**非空：多一个、少一个都当场报错。

    放任"一个都没有"的对象流到 `P4`，会写成一行**没有原因的失败**——那种记录最难查，
    因为每一层都觉得自己没错（这个理由逐字来自 `base.py` 里"注册了但返回空正文"
    那一段，是同一个坑的另一种形态）。
    """
    doc = ExtractedDoc(text="正文", source="plaintext")

    with pytest.raises(ValueError, match="恰好有一个"):
        Outcome("D:/x/a.txt")
    with pytest.raises(ValueError, match="恰好有一个"):
        Outcome("D:/x/a.txt", doc=doc, error=ParseFailed("坏了"))
    with pytest.raises(ValueError, match="恰好有一个"):
        Outcome("D:/x/a.txt", error=ParseFailed("坏了"), unexpected=RuntimeError("炸了"))


def test_extracted_doc_is_the_same_object_the_layer_reexports():
    """顺手钉一下转发：`extract.ExtractedDoc` 就是 `core.models.ExtractedDoc`。

    两处各定义一份的话，`Outcome.doc` 的类型判断会在某个 import 顺序下失效——那是个
    只在特定调用栈里才炸的坑。
    """
    assert ReExportedDoc is ExtractedDoc
