r"""按批提取：**一个文件出事，不许带走整轮**（`F3-07`，验收项 `A6`）。

全盘索引里"这一轮跑完没跑完"与"这一轮里哪些文件没成"是两件事。`base.extract_file()`
回答前半个（一个文件归谁、正文是什么），这个模块回答后半个：把一串路径喂进去，
**每一个都换回一条 `Outcome`**，好坏都换，中间的失败不改变后面的处理。

------------------------------------------------------------------------------
三类结局，对应 `files.status` 的三个值
------------------------------------------------------------------------------

`P4` 要把每条结局写成 `files` 里的一行，所以这里就把**该写哪个 `status`** 定死
（`docs/DEVELOPMENT_PLAN.md` §4.3 的 `files.status`：`pending|metadata|fulltext|
indexed|skipped|error|stale`）：

| 结局 | 异常 | `status` | `error_msg` | 含义 |
|---|---|---|---|---|
| 出正文了 | — | `fulltext` | 空 | 这一类文件 v1 该做，且这个文件做成了 |
| 这一类不做 | `UnsupportedFormat` | `skipped` | **空** | `.db` / `.exe` / `.dat`：留在 Tier0，文件名照搜 |
| 这个文件坏了 | `ParseFailed` / `FileUnreadable` / `BinaryContent` | `error` | **有** | 该做的格式，但这一个读不了 |
| 提取器自己炸了 | 任何别的 `Exception` | `error` | **有**（带"未预期的异常"前缀） | 见下面第二道防线 |

`UnsupportedFormat` 那一格**故意不写 `error_msg`**，这一条比它看起来重要：`error_msg`
非空是"失败报表"的唯一判据，而全盘上 `.db` / `.dat` / `.exe` / 各种未知扩展名多得是。
把"这一类不做"也填上原因，报表里就会塞满噪音，真坏的文件淹在里面——这正是
`base.UnsupportedFormat` 的文档里定过的那条分工（"不进失败报表"），这里只是把它落成代码。
**要看"为什么没收录某个扩展名"，答案是 `UnsupportedFormat` 那句消息本身，不需要落库**
（`lsearch explain` 会现场再算一次，那句话才是有用的那个）。

`BinaryContent` 归到失败那一档，与 `UnsupportedFormat` 相反，依据是 `plaintext.py`
的模块文档（"是**失败**，会带原因进报表"）：有扩展名但不在名单里的一律 `UnsupportedFormat`、
**不去嗅内容**，所以走到 `BinaryContent` 的只有两种情况——无扩展名的真二进制（`README`
那一支的兜底），或者有扩展名的文本文件里其实装着二进制（扩展名写错了、文件被截断了）。
两种都是"这个文件跟它的名字不符"，是该让人看见的。

------------------------------------------------------------------------------
第一道防线是提取器自己的异常族，第二道防线在这里
------------------------------------------------------------------------------

`office` / `web_mail` / `media_meta` 各自有一份"这个库会抛什么"的名单（`_pdf_errors()`
那几组），它们**已经**把库的异常翻成 `ParseFailed`。那这份 `except Exception` 是多余的
吗？不是，它是**同一条教训的第二次落地**：`F3-02` / `F3-03` / `F3-04` 每一轮都踩到过
"名单里少列一个异常族"——漏一个的症状就是**未捕获的异常直接结束整轮**，而不是少收一个
文件。名单要漏是早晚的事（库升级、换版本、语料里出现没见过的形态都会改它），而**漏一个
异常族与少收一个文件，代价差着量级**：前者是整轮索引白跑（`R4` 那条风险的形态），
后者是本轮少一个文件、下轮还在。

所以这里的判据**不回头依赖任何提取器**：`except Exception` 接住一切，
**单独计数**（`Tally.unexpected`，也单列在 `Outcome.unexpected` 里），原因照写。
单列是让报表能说出"其中 N 个是提取器自己炸了"——这一句是那三个模块的 bug 侦测器，
混进"文件坏了"里就等于没有。

**代价说清楚**：真正的 bug 会被接住而不是当场炸掉，从"一跑就红"变成"报表里多一行"。
它的补偿是：`Outcome` 里留着**异常对象本身**（不只是字符串），要复现的人可以直接取
`outcome.unexpected` 再抛一次。

------------------------------------------------------------------------------
`BaseException` 不接：Ctrl-C 必须停得下来
------------------------------------------------------------------------------

`except Exception` 而不是 `except BaseException`，是**有意**的：`KeyboardInterrupt`
与 `SystemExit` 都属于 `BaseException`，它们**必须**一路冒到调用方。把它们也接住的话，
Ctrl-C 会变成"这个文件失败，接着跑下一个"——症状是全盘跑起来按了没用，而且一点提示都
没有。这条有测试盯着（`test_ctrl_c_is_not_swallowed`），因为把 `Exception` 改成
`BaseException` 是个看起来"更彻底"的改动。

`MemoryError` 属于 `Exception`，所以它会**被接住并记一笔**，接着跑下一个文件。这是个
判断而不是定理：真·内存不足之后进程状态好不好没有定论。选接住是因为 `A6` 的字面要求是
"不中断整轮"，而这一档在实测里几乎总是某一个大文件撑不住（`[scan] max_file_mb` 之外
的形态），不是进程要死了。真要改（比如内存不足时停下来），改的就是这一层，别去改提取器。

------------------------------------------------------------------------------
`error_msg` 的形态：一行、带类名、有上限
------------------------------------------------------------------------------

写进 `files.error_msg` 的是 `"<异常类名>: <消息>"`：

- **带类名**是因为 `P4` / `P6` 的报表要能按类分堆（"多少是 ParseFailed，多少是
  FileUnreadable"），只有一句自然语言消息就得靠字符串匹配来分组。
- **折成一行**：异常消息里可能带换行（库报的原文常有），而这一格会进表格、进日志行。
  `" ".join(split())` 把一切空白展开（顺带去了首尾）。
- **上限 200 字符**，与 `base.meta_str` 同一个数、同一个理由（那一行是给人看的）。
  比它多一个"…"：`meta_str` 截断的是 EXIF 描述，截了看不出来也无妨；错误消息的**尾巴
  常常是库真正想说的那句话**，所以这里必须留下"被截过"的痕迹。

------------------------------------------------------------------------------
生成器：懒、按序、不留状态
------------------------------------------------------------------------------

`extract_many()` 是生成器。三个理由：`P4` 要在循环里打进度（"还剩 N 个"）；中途 Ctrl-C
就是中途离开，不需要在这里做任何清理；调用方想只看前几个就 `break`，不必先把整批做完。
输入里同一个路径出现两次就产出两条——**去重不是这一层的活**（`ingest/dedup.py` 按内容去重，
路径级重复本来就是扫描器不该给出的东西，给了也照实处理两条，不在这里悄悄合并）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import (
    ExtractConfig,
    ExtractError,
    ExtractedDoc,
    Extractor,
    UnsupportedFormat,
    extract_file,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不需要 core.registry
    from localsearch.core.registry import Registry

__all__ = [
    "ERROR_MSG_MAX",
    "STATUS_ERROR",
    "STATUS_FULLTEXT",
    "STATUS_SKIPPED",
    "Outcome",
    "Tally",
    "extract_many",
]

#: 写进 `files.status` 的三个值（取值域见计划 §4.3 的 `files.status`）。三个常量而不是
#: 字面量，是为了让 `P4` 写库时**引用它们**而不是手抄字符串——抄错一处不会报错，
#: 只会让"失败报表"少几行。
STATUS_FULLTEXT = "fulltext"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

#: `error_msg` 的长度上限（理由见模块文档）。
ERROR_MSG_MAX = 200

#: 截断时补的标记。ASCII 三点，不用 `…`：这个字符串会一路走到 CLI 的输出路径上，
#: 而控制台是代码页 936（见计划 §10.6 与 `PROGRESS.md` 里那条编码踩坑记录）。
_ELLIPSIS = "..."


def _one_line(text: str) -> str:
    """折成一行、去掉首尾空白、截到 `ERROR_MSG_MAX`（超了在尾巴上留 `...`）。"""
    flat = " ".join(text.split())
    if len(flat) <= ERROR_MSG_MAX:
        return flat
    return flat[: ERROR_MSG_MAX - len(_ELLIPSIS)] + _ELLIPSIS


@dataclass(frozen=True, slots=True)
class Outcome:
    """一个路径的结局：**出正文 / 这一类不做 / 这个文件坏了 / 提取器炸了**，四选一。

    `doc` / `error` / `unexpected` **恰好有一个非空**，由 `__post_init__` 把关。这不是
    形式主义：`P4` 要按 `status` 决定写不写 `files.error_msg`、要不要建 chunks，
    一个"既没有 doc 也没有 error"的对象在那里会变成一行**没有原因的失败**，
    而那种记录查起来最费劲（每一层都觉得自己没错）。

    为什么留着异常对象本身而不只留字符串：见上面"第二道防线"那一段的代价说明。
    """

    path: str
    doc: ExtractedDoc | None = None
    error: ExtractError | None = None
    unexpected: BaseException | None = None

    def __post_init__(self) -> None:
        given = (self.doc is not None, self.error is not None, self.unexpected is not None)
        if sum(given) != 1:
            kinds = " / ".join(
                name
                for name, present in zip(("doc", "error", "unexpected"), given)
                if present
            )
            raise ValueError(
                f"Outcome 里 doc / error / unexpected 必须恰好有一个非空，"
                f"实际有 {sum(given)} 个（{kinds or '一个都没有'}）：{self.path}"
            )

    @property
    def ok(self) -> bool:
        """出正文了没有。**空正文也算出**——0 字节文件是合法输入（`base.Extractor` 的约定）。"""
        return self.doc is not None

    @property
    def status(self) -> str:
        """`P4` 该把这个文件记成 `files.status` 的哪一个（三个常量的取值见模块文档）。"""
        if self.ok:
            return STATUS_FULLTEXT
        if isinstance(self.error, UnsupportedFormat):
            return STATUS_SKIPPED
        return STATUS_ERROR

    @property
    def error_msg(self) -> str:
        """该写进 `files.error_msg` 的那一句；**不需要写的时候是空串**。

        空串只出现在两种结局上：出正文了（没事可说），以及 `UnsupportedFormat`
        （这一类不做——理由见模块文档那一段，别在这里补上原因）。
        """
        if self.unexpected is not None:
            return _one_line(
                f"未预期的异常 {type(self.unexpected).__name__}: {self.unexpected}"
            )
        if self.error is None or isinstance(self.error, UnsupportedFormat):
            return ""
        return _one_line(f"{type(self.error).__name__}: {self.error}")


@dataclass(slots=True)
class Tally:
    """整轮的计数（`P4` 结尾那行报表用它）。

    `error` 是"这个文件坏了"与"提取器炸了"的和，`unexpected` 是其中后者的**子集**——
    单列出来是为了让报表能说"失败 N 个，其中 M 个是提取器自己炸的"（见模块文档）。
    """

    total: int = 0
    fulltext: int = 0
    skipped: int = 0
    error: int = 0
    unexpected: int = 0

    def add(self, outcome: Outcome) -> Outcome:
        """记一条。**返回它接到的那个 outcome**，好让 `map(tally.add, ...)` 串得起来。"""
        self.total += 1
        status = outcome.status
        if status == STATUS_FULLTEXT:
            self.fulltext += 1
        elif status == STATUS_SKIPPED:
            self.skipped += 1
        else:
            self.error += 1
        if outcome.unexpected is not None:
            self.unexpected += 1
        return outcome

    @property
    def has_failures(self) -> bool:
        """「完成但有文件失败」——`P6` 的启动器靠它区分退出码 1（完成、但要人看一眼）
        与 ≥ 2（真失败，该重试）。见计划 §4.8 的退出码归一那一条。
        """
        return self.error > 0


def extract_many(
    paths: Iterable[str],
    *,
    registry: "Registry[Extractor]",
    config: ExtractConfig,
) -> Iterator[Outcome]:
    r"""逐个提取，**一个文件出事不停下整轮**（`A6`）。

    产出顺序与输入顺序一致，每个路径恰好一条。抛不出来的情况只有一个：调用方自己那个
    `paths` 迭代器抛了（那不是文件的问题）。

    这里不写库、不建 chunks、不判 tier、不重试——那几件事分别在 `P4`（写 `files` /
    `chunks`）、`chunk/sliding.py`（切片）与 `P6`（增量重试）里。这一层唯一的职责是
    **把"每个文件一个结局"这条保证做出来**，让上面那几层可以在"整轮跑完了"的前提下
    各管各的。
    """
    for path in paths:
        try:
            doc = extract_file(path, registry=registry, config=config)
        except ExtractError as exc:
            # 已知的四类：原因就在异常里（`ExtractError` 的文档：消息就是给用户看的理由）。
            yield Outcome(path, error=exc)
        except Exception as exc:  # noqa: BLE001 - 第二道防线，见模块文档
            # 提取器自己的异常族名单漏了一个：照样记一笔、继续下一个。
            yield Outcome(path, unexpected=exc)
        else:
            yield Outcome(path, doc=doc)
