r"""`chunk/sliding.py`：滑窗的七条规则、`D8` 的上限与那两条进度保证（`F3-05`）。

------------------------------------------------------------------------------
这一组盯的八件事
------------------------------------------------------------------------------

1. **空输入与参数契约**——空正文与全空白都出**空列表**（不是一片空的）；三个数字
   参数非法时**报错**（不是尽力而为）；参数名与 `ChunkCfg` 的字段**逐字相同**
   （喂的正是 `asdict(ChunkCfg())`，改名时这里会红）。
2. **不变式**——非占位片必须满足 `text[c.char_start:c.char_end] == c.text`，`ord`
   从 0 连续。这条是 `P4` 的高亮与 `snippet` 取回原文的前提。
3. **窗口是"最大的那一个"**——不是"差不多装下"。用两种计数器各钉一遍：一个按
   字符数、一个**故意不均匀**（数字贵一倍），后者拿**暴力法**算出的边界当预期。
4. **重叠是按字符算的那一份**——`int(overlap_ratio * 片长)`，逐片核对；`0` 时
   相邻片不重叠。
5. **末尾不再切**（模块文档规则 7）——窗口碰到正文末尾就停。没有这一条时，末尾
   会多出一串**互相包含**的碎片；这里断言"每一片的 `char_end` 都严格大于前一片"
   （那串碎片正是靠这条被挡住的），并核对末尾那几片的精确边界。
6. **`D8` 的上限与占位片**——正好等于上限时**不**出占位片；超一片就出，片数**永远**
   不超过上限；被保留的片与"不设上限"时的前几片**逐字相同**（截断砍尾巴，不重切）；
   占位片是最后一片、偏移是 0、文字里带着真实片数。上限为 `1` 的退化另有一条。
7. **两条进度保证**（这一层最坏的失败形态是**不终止**）——注入一个"一个字符就值
   1000 个 token"的计数器，要求它照样前进、一片一个字符、片数等于字符数。
8. **默认计数器本身**——宽字符一个 token、ASCII 四个一个、空白不算；**前缀单调**；
   以及宽字符区段里**不能有空白字符**（那个减法会变成负数，见 `_WIDE_RANGES` 的
   注释）。

------------------------------------------------------------------------------
为什么这些测试里有那么多"注入计数器"
------------------------------------------------------------------------------

默认的 `estimate_tokens` 是启发式，拿它当预期就会变成"用实现验证实现"。所以凡是要
核对**边界在哪**的，都换成一个**能在纸上算出来**的计数器（`len`、或者"数字算两个
token"），预期值直接写成字面量或暴力法的输出。默认计数器只在第 8 组里被单独量。
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from localsearch.chunk import estimate_tokens, split
from localsearch.chunk.sliding import _WIDE_RANGES
from localsearch.core.config import ChunkCfg


def run(n: int) -> str:
    """`n` 个**非空白** ASCII 字符。内容不重要（断言全在偏移上），但"非空白"很重要
    ——带空白的样本会被 `_trim` 改掉边界，那是另一组测试的事。"""
    return "".join(chr(ord("a") + i % 26) for i in range(n))


#: 带空白、带换行、中英数字混排的一小段，用来测不变式与"没有内容掉在缝里"。
MIXED = "本地离线搜索 abc 123 全盘 456 索引\n第二行\t结尾  \n\n  再一行\n"


def counted(text: str, target: int, *, ratio: float = 0.0, cap: int = 10_000, counter=len):
    """套一层默认值，让下面每一条测试只写它自己关心的那几个参数。"""
    return split(
        text,
        target_tokens=target,
        overlap_ratio=ratio,
        max_chunks_per_doc=cap,
        count_tokens=counter,
    )


# ==============================================================================
# 1. 空输入与参数契约
# ==============================================================================


def test_an_empty_text_yields_no_chunks():
    """空正文不是"一片空的"——空片会进 FTS、进向量，白占一行还带回一个恒不命中的
    向量（模块文档规则 4）。全空白同理：`ExtractedDoc.is_empty` 的口径就是这样。"""
    assert split("", **asdict(ChunkCfg())) == []
    assert split("   \n\t\r\n  ", **asdict(ChunkCfg())) == []


def test_a_text_shorter_than_the_target_is_a_single_chunk():
    text = "就一句话"
    chunks = split(text, **asdict(ChunkCfg()))

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len(text))
    assert chunks[0].truncated is False


@pytest.mark.parametrize(
    "keywords",
    [
        {"target_tokens": 0, "overlap_ratio": 0.15, "max_chunks_per_doc": 200},
        {"target_tokens": -1, "overlap_ratio": 0.15, "max_chunks_per_doc": 200},
        {"target_tokens": 512, "overlap_ratio": 0.15, "max_chunks_per_doc": 0},
        {"target_tokens": 512, "overlap_ratio": 1.0, "max_chunks_per_doc": 200},
        {"target_tokens": 512, "overlap_ratio": 0.0, "max_chunks_per_doc": 200},   # 这条合法
        {"target_tokens": 512, "overlap_ratio": -0.1, "max_chunks_per_doc": 200},
    ],
)
def test_the_parameters_are_checked(keywords):
    """`overlap_ratio = 1.0` 是唯一"看着像边界、其实是死循环"的那个值——退回去
    等于原地踏步。这三个数的合法范围在配置层也有一道，但这一层不经过配置也可能被
    调用（模块文档里 `_check_args` 的文档写了为什么再挡一道）。"""
    legal = 0.0 <= keywords["overlap_ratio"] < 1.0 and keywords["target_tokens"] >= 1 \
        and keywords["max_chunks_per_doc"] >= 1
    if legal:
        assert split("abc", **keywords) != []
    else:
        with pytest.raises(ValueError):
            split("abc", **keywords)


def test_the_parameter_names_match_the_config_fields():
    """`split(doc.text, **asdict(cfg.chunk))` 是计划里的调用形态。参数名或 `ChunkCfg`
    的字段名任一边改了、另一边没跟上，这一条会红——那是**第三处默认值**的开端。"""
    split("随便一段正文", **asdict(ChunkCfg()))


# ==============================================================================
# 2. 不变式
# ==============================================================================


def test_every_chunk_points_at_its_own_text():
    """`text[c.char_start:c.char_end] == c.text`。偏移对不上时，`P4` 的高亮与
    `snippet` 会**取到别的字**——那不是崩溃，是搜出来的片段和标题对不上。"""
    text = MIXED * 30
    chunks = counted(text, 40, ratio=0.15)

    assert len(chunks) > 5                    # 别让这条测试在"只切了一片"上空转
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_the_ord_is_consecutive_from_zero():
    """`(blob_id, ord)` 是 `chunks` 表上的索引，也是 `embed_cache` 复用向量的依据。"""
    chunks = counted(MIXED * 20, 30, ratio=0.15)

    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_no_content_is_lost_between_adjacent_chunks():
    """相邻两片之间**只允许掉空白**——裁两端空白时掉的那点。这条比"覆盖完整"更好
    断言：它不要求两片的偏移相接（重叠时它们本来就相接得更多）。"""
    text = MIXED * 30
    chunks = counted(text, 40, ratio=0.15)

    for earlier, later in zip(chunks, chunks[1:]):
        assert text[earlier.char_end : later.char_start].strip() == ""


def test_no_chunk_is_empty_or_all_whitespace():
    chunks = counted(MIXED * 30, 40, ratio=0.15)

    for chunk in chunks:
        assert chunk.text != ""
        assert chunk.text.strip() == chunk.text


def test_the_whitespace_at_the_edges_is_trimmed_and_the_offsets_follow():
    text = "  \n  正文在这里  \n\n"
    chunks = counted(text, 512)

    assert len(chunks) == 1
    assert chunks[0].text == "正文在这里"
    assert chunks[0].char_start == 5          # 前导的 "  \n  " 是 5 个字符


def test_the_last_chunk_reaches_the_end_of_the_text():
    """最后一片必须铺到末尾（没有占位片时），否则尾巴上的内容谁也搜不到。"""
    text = run(100)
    chunks = counted(text, 10, ratio=0.15)

    assert chunks[-1].char_end == len(text)


# ==============================================================================
# 3. 窗口是"最大的那一个"
# ==============================================================================


def test_a_window_is_the_largest_one_that_fits():
    """计数按字符算时，边界就是整齐的十字符一格——"最大"与"差不多"在这一组里
    是能分辨的：任何提前一格都会让下面这个列表全错。"""
    text = run(95)
    chunks = counted(text, 10)

    assert [(c.char_start, c.char_end) for c in chunks] == [
        (10 * i, min(10 * i + 10, 95)) for i in range(10)
    ]


def test_the_window_search_finds_the_maximum_with_an_uneven_counter():
    """数字算两个 token、字母算一个。窗口落在哪里没有纸面公式（得看下一个字符是
    谁），所以预期是**暴力法**给的：一格一格加，直到装不下。这条同时钉住了"先倍增
    再二分"那段搜索——它找的必须是**最大**的 `end`，而不是"第一个装不下的前一个
    随机点"。"""
    text = "ab1cd2ef3gh4ij5kl" * 3
    target = 12

    def counter(part: str) -> int:
        return sum(2 if ch.isdigit() else 1 for ch in part)

    expected: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = start
        while end < len(text) and counter(text[start : end + 1]) <= target:
            end += 1
        end = max(end, start + 1)             # 进度保证 1
        expected.append((start, end))
        if end == len(text):
            break
        start = end                            # ratio = 0
    else:                                      # pragma: no cover - 只防上面写错
        raise AssertionError("暴力法自己没走到末尾")

    chunks = counted(text, target, counter=counter)

    assert [(c.char_start, c.char_end) for c in chunks] == expected
    assert [c.n_tokens for c in chunks] == [
        counter(text[a:b]) for a, b in expected
    ]


def test_a_window_never_exceeds_the_token_target():
    """默认计数器、真实混排文本：每一片的 `n_tokens` 都要 `<= target`。唯一的例外是
    "一个字符本身就超预算"（下面进度保证那一组单独钉），这里不是那种输入。"""
    text = MIXED * 40
    target = 50
    chunks = counted(text, target, ratio=0.15, counter=estimate_tokens)

    for chunk in chunks:
        assert chunk.n_tokens <= target


def test_split_uses_the_default_counter():
    """不传 `count_tokens` 时用的是 `estimate_tokens`——拿它自己算一遍最大前缀，
    边界必须**逐字符相同**（再按规则 2 把尾巴上的空白退掉）。"""
    text = MIXED * 25
    target = 50
    chunks = split(
        text, target_tokens=target, overlap_ratio=0.0, max_chunks_per_doc=10_000
    )

    end = 0
    while end < len(text) and estimate_tokens(text[: end + 1]) <= target:
        end += 1
    while end > 0 and text[end - 1].isspace():   # 规则 2 的裁剪
        end -= 1
    assert chunks[0].char_end == end


# ==============================================================================
# 4. 重叠
# ==============================================================================


def test_the_overlap_is_the_stated_fraction_of_the_char_span():
    """`target = 10`、`overlap = 0.5` → 每片退 5 个字符，相邻两片共享 5 个字符。
    数字是算得出来的，所以这里写死。"""
    text = run(100)
    chunks = counted(text, 10, ratio=0.5)

    assert [c.char_start for c in chunks] == list(range(0, 95, 5))
    for earlier, later in zip(chunks, chunks[1:]):
        assert earlier.char_end - later.char_start == 5
        assert earlier.text[-5:] == later.text[:5]      # 共享的确实是同一段字


def test_zero_overlap_makes_adjacent_chunks_disjoint():
    text = run(100)
    chunks = counted(text, 10, ratio=0.0)

    for earlier, later in zip(chunks, chunks[1:]):
        assert later.char_start >= earlier.char_end


# ==============================================================================
# 5. 末尾不再切（规则 7）
# ==============================================================================


def test_the_tail_is_not_sliced_into_nested_pieces():
    """`target = 10`、`overlap = 0.15` → 每片退 `int(1.5) = 1` 个字符，起点按 9 前进，
    到 `start = 90` 时窗口正好铺满 100 个字符，就此收尾。

    ⚠ 收尾那一条规则没写之前，这里还有三片：`(95,100)`、`(98,100)`、`(99,100)`——
    每一片都是 `(90,100)` 的**子集**，内容一个字不多，却各占一个向量与一行 FTS。
    """
    text = run(100)
    chunks = counted(text, 10, ratio=0.15)

    assert [(c.char_start, c.char_end) for c in chunks] == [
        (9 * k, min(9 * k + 10, 100)) for k in range(11)
    ]
    # 这一条是"没有互相包含的碎片"的紧凑写法：每一片的右端都必须**严格**往后走。
    ends = [c.char_end for c in chunks]
    assert ends == sorted(set(ends))


def test_a_short_text_is_covered_by_one_chunk_and_that_is_the_end():
    """正文比一片短时，第一片的窗口就碰到末尾，也就没有第二片。"""
    text = run(7)
    chunks = counted(text, 512, ratio=0.15)

    assert len(chunks) == 1
    assert chunks[0].char_end == 7


# ==============================================================================
# 6. D8 的上限与占位片
# ==============================================================================


def test_exactly_at_the_limit_keeps_everything_and_adds_no_summary():
    text = run(95)                              # 每片 10 个字符 -> 正好 10 片
    chunks = counted(text, 10, cap=10)

    assert len(chunks) == 10
    assert all(c.truncated is False for c in chunks)


def test_one_over_the_limit_truncates_and_adds_the_summary():
    """上限 9、正文本来 10 片：保留开头 **8** 片，第 9 个名额留给占位片。"""
    text = run(95)
    chunks = counted(text, 10, cap=9)
    summary = chunks[-1]

    assert len(chunks) == 9
    assert summary.truncated is True
    assert summary.is_placeholder is True
    assert (summary.char_start, summary.char_end) == (0, 0)
    assert summary.ord == 8
    assert all(c.truncated is False for c in chunks[:-1])


def test_the_summary_says_what_was_dropped():
    """统计摘要得能自己说明白：本来多少片、留了多少、还有多少字符没索引。这些数字
    是**唯一的线索**——被砍掉的内容在库里一个字都没有。"""
    text = run(95)
    summary = counted(text, 10, cap=9)[-1]

    assert "一共切出了 10 片" in summary.text
    assert "只保留了开头的 8 片" in summary.text
    assert "剩下的 2 片" in summary.text
    assert "约 15 字符" in summary.text          # 8 片铺到 80，还剩 95 - 80 = 15


def test_the_chunk_count_never_exceeds_the_limit():
    """占位片**也占名额**：`D8` 说的是每文档 200 片，不是"200 片正文 + 1 片说明"。"""
    text = run(95)
    for cap in range(1, 13):
        assert len(counted(text, 10, cap=cap)) <= cap


def test_the_kept_chunks_are_identical_to_the_uncapped_run():
    """截断是**砍掉尾巴**，不是换一种切法：前几片必须逐字相同（含 `n_tokens` 与
    偏移）。否则同一个 blob 换个上限重跑，前面那部分的向量就全废了（`A9`）。"""
    text = run(95)
    capped = counted(text, 10, cap=9)
    uncapped = counted(text, 10, cap=1000)

    assert capped[:-1] == uncapped[:8]


def test_a_real_config_run_trips_the_limit_on_a_big_document():
    """拿 `ChunkCfg` 的**真数字**（512 / 0.15）跑一篇 12.8 万字符的正文：D8 会真的
    触发，而且触发的门槛就在这个量级（每片净增 436 个token，`200 × 436 ≈ 8.7 万`）。"""
    text = run(512 * 250)
    uncapped = counted(text, 512, ratio=0.15, cap=10_000)
    capped = counted(text, 512, ratio=0.15, cap=200)

    assert len(uncapped) > 200
    assert len(capped) == 200
    assert capped[:199] == uncapped[:199]
    assert f"一共切出了 {len(uncapped)} 片" in capped[-1].text


def test_a_limit_of_one_leaves_only_the_summary():
    """上限 1 是个退化配置，但它合法（配置层只要求 `>= 1`）：正文放不下"一片正文 +
    一片说明"时，**只留说明**。这一条是写给"上限为 0 时会不会返回空列表"这类问题的。"""
    chunks = counted(run(95), 10, cap=1)

    assert len(chunks) == 1
    assert chunks[0].truncated is True


def test_a_limit_of_one_still_keeps_a_one_chunk_document():
    """上限 1、正文本来就只有 1 片时**不出**占位片——截断的条件是"本来超过上限"，
    不是"上限很小"。"""
    chunks = counted(run(5), 10, cap=1)

    assert len(chunks) == 1
    assert chunks[0].truncated is False
    assert chunks[0].text == run(5)


# ==============================================================================
# 7. 两条进度保证
# ==============================================================================


def test_a_counter_that_charges_a_fortune_per_char_still_makes_progress():
    """进度保证 1：一个字符就超预算时，窗口仍要吐出一个字符。

    这条是这一组里最要紧的——这段逻辑**不终止**时的样子不是报错，是整轮索引卡住
    且没有任何输出（模块文档开头写了）。同一条也顺带钉住"`n_tokens` 可以超过
    `target`"这个**唯一的例外**。"""
    text = run(7)

    def greedy(part: str) -> int:
        return 1000 if part else 0

    chunks = counted(text, 5, ratio=0.5, counter=greedy)

    assert len(chunks) == len(text)             # 一片一个字符，且**走完了**
    assert [c.text for c in chunks] == list(text)
    assert all(c.n_tokens == 1000 for c in chunks)


def test_a_counter_that_always_says_one_token_returns_one_chunk():
    """另一个方向的极端：怎么加都只有一个 token，于是第一片就装下整篇（末尾那点空白
    按规则 2 裁掉，所以右端比正文短一点）。"""
    text = MIXED * 5
    chunks = counted(text, 1, counter=lambda part: 1 if part else 0)

    assert len(chunks) == 1
    assert chunks[0].text == text.rstrip()
    assert chunks[0].char_end == len(text.rstrip())


# ==============================================================================
# 8. 默认计数器本身
# ==============================================================================


def test_a_wide_char_costs_one_token():
    assert estimate_tokens("本地离线") == 4
    assert estimate_tokens("。、！？") == 4      # CJK 标点也算宽字符
    assert estimate_tokens("アイウ") == 3        # 片假名
    assert estimate_tokens("한글") == 2          # 谚文


def test_four_ascii_chars_cost_one_token():
    assert estimate_tokens("abc") == 1           # 不足四个也算一个
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_whitespace_costs_nothing():
    """空白不计 token 的**代价**写在模块文档里：一篇由空白堆出来的正文只会切出很少
    的片，而那些片的字符数可能远超 512。"""
    assert estimate_tokens("   \n\t  ") == 0
    assert estimate_tokens("a b c d") == 1       # 四个非空白字符


def test_the_estimate_never_goes_backwards():
    """**二分搜索全靠这一条**（模块文档把它列成了陷阱）：往尾巴上加字符，计数不能
    减少。破坏了它不会报错，只会悄悄切短一片——所以这里把每个前缀都过一遍。"""
    text = "abc 本地 123\tdef。\n全角ＡＢ　全角空格 456"
    counts = [estimate_tokens(text[:n]) for n in range(len(text) + 1)]

    assert counts == sorted(counts)


def test_no_wide_range_contains_a_whitespace_char():
    """`estimate_tokens` 里有个减法：`非空白字符数 - 宽字符数`。宽字符区段里只要混进
    一个空白字符（`U+3000` 全角空格就在 `U+3000` 那个块里、`U+2000-U+200A` 更是一整
    排空白），这个差就会变成负数，而 `(负数 + 3) // 4` 会算出一个**看着像模像样的
    错数**。加区段的人不会想到要查这件事，所以由这条测试来查。"""
    offenders = [
        hex(code)
        for low, high in _WIDE_RANGES
        for code in range(low, high + 1)
        if chr(code).isspace()
    ]

    assert offenders == []
