r"""`F4-01` 的预分词层：`index/tokens.py`。

关联验收项 **A8**（Tier0 跑完立刻能按文件名搜到）。这一层的三个决定全是 `E38`
量出来的，不是推理：索引侧 `cut_for_search` + 相邻汉字 2-gram、查询侧 `cut` +
丢单字汉字、**不做 OR 兜底**。

`test_store_files_fts.py` 那些检索测试是**端到端**的（落库再搜），本文件打的是
**函数本身**。两个方向都要有，因为端到端那条路上同一个词有两份 token（名字一列、
路径一列），而路径**包含**名字——一列坏了另一列会替它兜住。这条不是空话：
`f4-01` 的第一轮九条变体里"文件名那一列不再加 2-gram"就是这么**活下来**的
（改坏之后 51 条一条都不红，因为那一行测试里的路径本身带着同样的 2-gram）。

第二条活下来的变体是"token 不再加引号"，它只能由本文件的**形状**测试来钉：
它的行为后果被 `keep_token` 挡在前面了——`*` `"` `(` `)` 都不在 `\w` 里，根本
走不到拼表达式那一步，所以从 `search_paths()` 那一头看不见引号这一层。
"""

from __future__ import annotations

import inspect

import pytest

from localsearch.index import sqlite_store as store_module
from localsearch.index.tokens import (
    HAN,
    SEPARATOR,
    and_expr,
    han_bigrams,
    index_tokens,
    keep_token,
    or_expr,
    query_tokens,
    tokenize,
)

# ==============================================================================
# 1. 一个 token 算什么
# ==============================================================================


@pytest.mark.parametrize(
    "token",
    ["财务", "a", "Z", "3", "xlsx", "_", "my_var", "报表2", "µ"],
)
def test_a_token_with_a_word_character_is_kept(token: str):
    assert keep_token(token) is True


@pytest.mark.parametrize("token", ["", " ", ".", "-", "/", "*", '"', "(", ")", "：", "——"])
def test_a_token_of_pure_punctuation_is_dropped(token: str):
    """判据取 `\\w`，与 `unicode61` 认定词内字符的口径对齐。

    这条是"没有可搜的内容"与"没搜到"能分开的前提：`!!!` 必须一个 token 都不剩。
    """
    assert keep_token(token) is False


# ==============================================================================
# 2. 相邻汉字 2-gram（只给名/路径那一层）
# ==============================================================================


def test_bigrams_take_every_adjacent_pair_of_han_characters():
    r"""`财务报表` 的 2-gram 里有 `务报`——**这正是词法索引够不到的那个缝**。

    `cut_for_search` 给出的是 `财务` / `报表` / `财务报表`，没有 `务报`。
    """
    assert han_bigrams("财务报表") == ["财务", "务报", "报表"]


def test_bigrams_refuse_a_pair_with_a_separator_or_a_digit_on_one_side():
    r"""`D:\work\项目\2024` 里只该留下 `项目`。

    `目\` / `\项` / `4年` 这一类既不是词，也没有人会这样搜；放进去等于往索引里
    塞噪音（且会让落盘量跟路径的标点密度挂钩）。
    """
    assert han_bigrams(r"D:\work\项目\2024") == ["项目"]


def test_repeated_bigrams_are_kept_not_deduplicated():
    """去重只在文本自身重复时才省东西，而它会让"2-gram 让落盘量涨多少"这个数
    **依赖语料**（`E38` 的正文样本把 25 句话重复六遍，去重后增量从 2.76× 掉到
    1.30×——那是样本的性质，不是 prose 的性质）。"""
    assert han_bigrams("哈哈哈") == ["哈哈", "哈哈"]


def test_bigrams_are_off_for_nothing_but_han():
    assert han_bigrams("abc123") == []
    assert han_bigrams("") == []


# ==============================================================================
# 3. 索引侧（`cut_for_search` + 可选的 2-gram）
# ==============================================================================


def test_the_index_side_uses_cut_for_search():
    """`E38`：精确模式 `cut` 只给 `财务报表` 一个 token，微召回 45.2%。

    ⚠ 这个元组是**实测的**（`jieba` 0.42.1），不是照着文档猜的：`.` 被 `keep_token`
    丢掉，`xlsx` 留着。
    """
    assert index_tokens("财务报表.xlsx", bigrams=False) == ["财务", "报表", "财务报表", "xlsx"]


def test_a_seam_word_is_only_there_when_bigrams_are_on():
    """`务报` 落在 `财务` 与 `报表` 之间——**只有 2-gram 那一层有它**。

    这就是"两种切法不许顺手统一"的那条缝，也是名字列与路径列各存一份时**必须
    分开看**的地方。
    """
    assert "务报" not in index_tokens("财务报表.xlsx", bigrams=False)
    assert "务报" in index_tokens("财务报表.xlsx", bigrams=True)


def test_words_survive_alongside_the_bigrams():
    """加了 2-gram 不等于换了一套切法：**它是叠加**（`E38` 的 98.2% / 98.8% 两行
    就是这么来的）。"""
    with_bigrams = index_tokens("财务报表.xlsx", bigrams=True)
    for word in index_tokens("财务报表.xlsx", bigrams=False):
        assert word in with_bigrams


def test_both_sides_fold_case():
    """索引侧与查询侧都要小写——`unicode61` 自己也会折叠 ASCII，显式做一遍只是让
    落库的那一列可读，但**两边必须是同一个口径**。"""
    assert index_tokens("Report.XLSX", bigrams=False) == index_tokens("report.xlsx", bigrams=False)
    assert index_tokens("Report.XLSX", bigrams=False) == ["report", "xlsx"]
    assert query_tokens("Report.XLSX") == ["report", "xlsx"]


def test_empty_text_gives_no_tokens():
    assert index_tokens("", bigrams=True) == []
    assert index_tokens("   ", bigrams=True) == []


# ==============================================================================
# 4. 查询侧（`cut` + 丢单字汉字）
# ==============================================================================


def test_the_query_side_uses_plain_cut_not_cut_for_search():
    """`E38`：两者召回一样（75.3% / 75.3%），而多出来的 token 在 AND 下**只能让
    结果变少**，所以查询侧取小的那个。"""
    assert query_tokens("财务报表") == ["财务报表"]


def test_a_single_han_character_is_dropped_when_others_remain():
    """`E38` 里最值钱的一个数：36.1% → 75.3%。AND 下一个 `的` 就掐死整条查询，
    而查询里带「的」是中文用户的常态。"""
    assert query_tokens("项目的报表") == ["项目", "报表"]
    assert query_tokens("北京大学的研究生") == ["北京大学", "研究生"]


def test_single_han_characters_stay_when_they_are_all_there_is():
    """用户真的在搜「书」时一个字也不许丢，否则查询会变成空的。"""
    assert query_tokens("书") == ["书"]
    # `文搜` 不是词典里的词，切出来就是两个单字——这是 `E38` 那 2/156 条残留
    # 落空的直接原因：AND 要求两个单字 token 都在，而索引里没有单字。
    assert query_tokens("文搜") == ["文", "搜"]


def test_a_single_ascii_character_is_never_dropped():
    """`a.txt` 里的 `a` 是用户明确打出来的东西；索引侧也不跟着丢——索引侧丢掉的
    词就再也搜不到了。"""
    assert query_tokens("a.txt") == ["a", "txt"]


def test_an_empty_query_gives_no_tokens():
    assert query_tokens("") == []
    assert query_tokens("!!!") == []


# ==============================================================================
# 5. MATCH 表达式（每个 token 各自加引号）
# ==============================================================================


def test_the_expression_is_an_implicit_and():
    """FTS5 里引号之间留空格就是 AND，**不需要写 `AND` 这个词**。"""
    assert and_expr(["报", "表"]) == '"报" "表"'
    assert SEPARATOR == " "


def test_every_token_is_quoted_even_when_it_looks_harmless():
    """⭐ **这一条是"token 不再加引号"那条变体活下来之后补的护栏。**

    它的行为后果从 `search_paths()` 那一头看不见（`keep_token` 已经把 `*` `"` `(`
    这些字符滤掉了，能走到这里的一定是字母/数字/汉字），所以只能在这里钉形状。
    拼表达式时**逐个 token** 加引号，而不是"发现保留字才加"——那需要维护一份
    保留字名单，而黑名单早晚会漏。
    """
    assert and_expr(["报表"]) == '"报表"'
    assert and_expr(["AND"]) == '"AND"', "保留字必须落在引号里"
    assert and_expr(["报*"]) == '"报*"', "`*` 是前缀通配符，不许溜进表达式"
    assert and_expr(["a", "b", "c"]) == '"a" "b" "c"'


def test_a_quote_inside_a_token_is_doubled():
    """FTS5 的规矩：引号内写两个双引号。走不到这一步（`keep_token` 会滤掉它），
    但留着——将来这个函数被别处调用时，"引号转义"不该是调用方的责任。"""
    assert and_expr(['a"b']) == '"a""b"'


def test_no_tokens_gives_none_not_an_empty_string():
    """`MATCH ''` 是**语法错**；而"没有可搜的内容"与"匹配不到任何东西"是两件事，
    调用方要能分开处理。"""
    assert and_expr([]) is None
    assert or_expr([]) is None


def test_empty_text_never_lands_in_a_match_expression():
    assert and_expr(index_tokens("", bigrams=True)) is None
    assert and_expr(query_tokens("!!!")) is None


# ==============================================================================
# 6. OR 兜底：量过了，**不许有人顺手加回来**
# ==============================================================================


def test_the_or_expression_exists_only_for_the_probe():
    """`E38`：AND 落空改 OR，微召回 75.3% → **75.3%**（一条都没多找回来），
    40 次触发里每一次都返回 0 条；而它的失败形态是**静默给出一堆不相关结果**。

    这条用静态检查钉住决策本身：`or_expr` 留着是给 `tools/bench_tokenize.py` 做
    对照、给将来在真实正文查询上复验用的，**检索路径里不许出现它**。
    """
    assert or_expr(["报", "表"]) == '"报" OR "表"'
    source = inspect.getsource(store_module)
    assert "or_expr" not in source, "检索路径用上了 OR 兜底——那是 E38 否掉的东西"


def test_the_store_does_not_import_the_or_expression():
    assert "or_expr" not in store_module.__dict__


# ==============================================================================
# 7. 落库形态
# ==============================================================================


def test_tokens_are_joined_by_a_single_space():
    r"""空格是 FTS5 的分词边界，也是 `unicode61` 认的分隔符。

    选它还有一个理由：这一列是**要能直接 `sqlite3` 打开看的**——排障时第一件想做
    的事。所以分隔符不能是 `unicode61` 会丢掉的那种（比如 `,`）。
    """
    assert tokenize("财务报表.xlsx", bigrams=False) == "财务 报表 财务报表 xlsx"
    assert "  " not in tokenize("财务报表.xlsx", bigrams=True)


def test_empty_text_tokenizes_to_an_empty_string_not_none():
    """空串是"这一列没有内容"，`None` 是"不知道"——`files_fts` 只会有前者。"""
    assert tokenize("", bigrams=True) == ""
    assert tokenize("...", bigrams=True) == ""


def test_the_han_character_class_covers_the_extension_blocks():
    """`HAN` 用来判"这一对是不是汉字"，也判"这个单字要不要丢"。

    覆盖扩展 A（U+3400–U+4DBF）、基本区（U+4E00–U+9FFF）与兼容区
    （U+F900–U+FAFF）——生僻字名不该整个变成标点。
    """
    assert HAN.match("龘") and HAN.match("㐀") and HAN.match("豈")
    assert not HAN.match("a") and not HAN.match("3") and not HAN.match("。")
