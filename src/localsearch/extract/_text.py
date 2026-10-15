r"""正文累积器：逐行攒、到上限**整行地停**，并把"停过"记下来。

这个类原先长在 `office.py` 里（`F3-02`）。`F3-03` 的 `web_mail.py` 需要**同一套**
截断语义（`D8` 的 200 片上限要能说清"正文到哪里为止"），所以提到这里共用，而不是
再抄一份——两份实现迟早会在"整行地停"与 `has_body` 的判定上分岔，而这两条正是
下游读数的依据。

⚠ 文件名以 `_` 开头：它**不是一个提取器模块**。`build_registry()` 不 import 它、
`extract/__init__.py` 也不转发它，它只是提取器之间共用的零件。

类名保持 `_Text` 不改（原先在 `office.py` 里就叫这个名字，那里有十余处正文与文档串
提到它，几条盯着 xlsx 截断的测试的叙述里也有）：搬家的目的是**只有一份实现**，
顺手改名只会让 diff 里混进一堆与目的无关的行。名字本身与文件名的 `_` 前缀一致——
这不对外，只是提取器内部的零件。

两个用户当前的分工：`office.py` 要 `reset()`（xlsx 的第二遍），`web_mail.py` 只用
`line()` / `marker()` / `block()`。
"""

from __future__ import annotations


class _Text:
    r"""逐行攒正文，`max_chars` 到顶就停，并把"停过"记下来。

    判定用的是 `len("\\n".join(lines))` 这个口径（每行之间一个换行），所以
    **整行收或整行不收**：不切一半的行，因为"正文到哪里为止"这件事要一句话说得清，
    而那条截断警告说的就是它。

    行分两种：**正文行**（`line()` / `block()`）与**标记行**（`marker()`）。两者在
    正文里的地位一样（都进 `text`、都算偏移），分开只是为了回答 `has_body` 那一个
    问题——见它的文档。
    """

    __slots__ = ("_lines", "_size", "_max", "_body", "truncated")

    def __init__(self, max_chars: int) -> None:
        self._max = max_chars
        self._lines: list[str] = []
        self._size = 0
        self._body = False
        self.truncated = False

    @property
    def max_chars(self) -> int:
        return self._max

    @property
    def full(self) -> bool:
        """已经到顶（到顶之后 `line()` / `marker()` 什么都不做）。"""
        return self.truncated

    @property
    def has_body(self) -> bool:
        """收进来过**非空白的正文行**（标记行不算）。

        ⚠ 不加这个区分的话，"正文是不是空的"会被**我们自己的标记**骗过去：pptx 每张
        幻灯片都先加一个 `【第 N 张幻灯片】`、邮件每封都先加几行 `【邮件】…`，于是一份
        只有图片的 pptx、一封只有邮件头的邮件看起来都"有正文"，那条"没有提取到任何
        文本"的警告就永远不出现。（`2026-10-15` 的第一版 office 就是这样，写测试时
        当场抓到：`test_an_image_only_pptx_is_an_empty_doc_with_a_warning`。）
        """
        return self._body

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def line(self, text: str) -> None:
        """收一行正文。**空串也是一行**——段落之间的空行是结构，与 `plaintext.py`
        不清理空行的口径一致。"""
        self._add(text, body=True)

    def marker(self, text: str) -> None:
        """收一行**标记**（`【第 N 页】` / `【附件】…` 这类，见各模块文档那张表）。"""
        self._add(text, body=False)

    def block(self, text: str) -> None:
        """把一段**多行**文本按行收进来（库自己给的换行在这种场合就是段落边界）。

        这里统一用 `\\n` 重接：PDF / xlsx / pptx / 邮件的正文本来就是库**合成**出来的，
        不存在"原样字节"可比——`plaintext.py` 那条"不归一化换行"的规矩是针对**原始
        解码文本**的，这里没有同一件事要保护。
        """
        for one in text.splitlines():
            self.line(one)
            if self.truncated:
                return

    def reset(self) -> None:
        """倒空重攒（xlsx 的第二遍用），**对象不换**。

        ⚠ 必须是"重置"而不是"再造一个 `_Text`"：office 的 `extract()` 拿着的是
        **创建时那个对象**，`truncated` / `has_body` 都从它身上读。换对象的话，那两个
        结论会留在第一遍上，而**两遍的长度可以反过来**——缓存值可能比公式原文长得多
        （`=A1` 算出 16 个汉字），那时第一遍装得下、第二遍装不下，换对象给出的账是
        "正文只到第 2 行、`truncated` 却是假、截断警告也没有"。

        （`2026-10-15` 实测：上限 30、缓存值 16 字时，旧写法给 `truncated=False` 且
        `warnings=()`。**注意拦得住这一支的只有"缓存值更长"**——第一版那条测试用的是
        "缓存值更短"的工作簿，两遍都装得下，换不换对象都绿。回归线是
        `test_the_truncation_flag_describes_the_second_pass`。）

        另一头见 office 的 `_parse_xlsx`：第一遍就满了的时候第二遍**根本不跑**。那条
        闸门与这里配套——`reset()` 会把 `truncated` 清掉，闸门一撤，满了的正文就会被
        重置成"没满"，标志与警告一起消失。
        """
        self._lines.clear()
        self._size = 0
        self._body = False
        self.truncated = False

    def _add(self, text: str, *, body: bool) -> None:
        if self.truncated:
            return
        extra = len(text) + (1 if self._lines else 0)
        if self._size + extra > self._max:
            self.truncated = True
            return
        self._lines.append(text)
        self._size += extra
        if body and text.strip():
            self._body = True
