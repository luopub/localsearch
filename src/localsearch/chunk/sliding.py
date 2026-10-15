r"""滑窗分块：一段正文 → 若干 `Chunk`（计划 §4.3 的 `chunk/sliding.py`，`F3-05`）。

------------------------------------------------------------------------------
它接什么、不接什么
------------------------------------------------------------------------------

输入**就是一段字符串**（`ExtractedDoc.text`），输出 `list[Chunk]`。它不看 `title`、
不看 `meta`、不看 `modality`、不认识段落也不认识 Markdown、不碰磁盘、不写库。
`core/models.py` 里那句"`text` 是切片的唯一输入"落到的就是这一层——所以这里
对四类提取器一视同仁（`extract/` 那边则完全不知道有分块这回事）。

------------------------------------------------------------------------------
七条规则
------------------------------------------------------------------------------

1. **窗口装到 `target_tokens` 为止**：从 `start` 起取**最大的** `end`，使
   `count_tokens(text[start:end]) <= target_tokens`。是"最大的"而不是"接近的"：
   切片边界必须是**正文的函数**，这样同一份内容今天切 5 片、明天还是这 5 片，
   `(blob_id, ord)` 才对得上（`A9` 的 `embed_cache` 复用全建立在这上面）。
2. **片内两端的空白裁掉，偏移跟着改**。于是有一条**不变式**：
   `text[c.char_start : c.char_end] == c.text`（非占位片）。裁掉的那几个空白
   不算"丢了内容"——它本来就会被 FTS 与嵌入器忽略，留着只是让每一片都以
   `\n` 开头。
3. **重叠按字符算**：下一片从 `end - int(overlap_ratio * (end - start))` 起。
   不是按 token 算——窗口已经装到了 `target_tokens`，两者比值接近，而按字符算
   每片只要一次计数搜索。⚠ 裁空白会让**实际**共享的那段比这个数短，两个边界
   都落在空白上时甚至可能一点都不共享；"相邻两片共享一段"是重叠的**意图**，
   不是可断言的不变量（`core/models.py` 里那句话也写的是"因重叠而共享时"）。
4. **整片都是空白就不产出**：空片会进 FTS、进向量，白占一行还带回一个恒不
   命中的向量。正文全是空白时直接返回**空列表**（`ExtractedDoc.is_empty` 的
   口径就是"全空白也算空"）。
5. **`D8` 的每文档上限**：本来会切出超过 `max_chunks_per_doc` 片时，只保留
   **开头** `max_chunks_per_doc - 1` 片，再补**一片占位片**（`truncated=True`）
   说明这件事——它就是 `D8` 那句"只索引表头与统计摘要"里的**统计摘要**。
   占位片**也占一个名额**，所以一篇正文的片数**永远** `<= max_chunks_per_doc`。
   被保留的那些片与"不设上限"时的前几片**逐字相同**：截断是砍掉尾巴，不是
   换一种切法（否则同一个 blob 换个上限重跑，"前面那部分"的向量就全废了）。
6. **`ord` 从 0 起连续**，占位片（如果有）永远是最后一片——`Chunk` 的文档里
   写着它的 `char_start` / `char_end` 一律是 0，因为它不是原文的一段。
7. **窗口碰到正文末尾就停**。一旦某一片的窗口右端到了 `limit`，再往后切出来的
   每一片都停在 `limit`、起点却更靠后，也就是**纯子集**——内容一个字不多，
   却各占一个向量与一行 FTS。没有这一条的话，重叠会把每篇正文的**末尾**切成一串
   越来越小、互相包含的片（`overlap_ratio = 0.15` 下 512 → 76 → 11 → 1 这样缩
   下去，四片废片）。⚠ 这一条**不牺牲覆盖**：能停的前提就是"已经有一片铺到了
   末尾"。

------------------------------------------------------------------------------
两条进度保证（这一层最坏的失败形态是"不终止"）
------------------------------------------------------------------------------

一个死循环会让整轮索引卡住、**而且没有任何输出**——不像异常那样留下痕迹。
所以有两条硬保证，代价都是"结果可能比 `target_tokens` 难看"：

- **一片至少一个字符**：注入的计数器要是说"一个字符就值 1000 个 token"，
  窗口也得吐出一个字符来，不能吐一个空窗口然后原地打转。
- **下一片的起点严格大于这一片的起点**（`overlap_ratio < 1` 已经保证了这一点，
  代码里那个 `max()` 是第二道）。

------------------------------------------------------------------------------
代价与陷阱
------------------------------------------------------------------------------

- **默认计数器是启发式，它不准**：宽字符（CJK / 假名 / 谚文 / 全角）算 1 个
  token，其余非空白字符 4 个算 1 个。真正的 tokenizer 在 `P5`（`Chunk.n_tokens`
  的文档里写着这条交接）。它不准**不影响**"切完了没有"，只影响**边界落在哪**。
- ⚠ **换计数器 = 全量重切**。边界一变，`(blob_id, ord)` 的对应、每个 `n_tokens`、
  每个要嵌的字符串全变，`embed_cache` 一条都复用不上。计划 §4.4 那边管这叫
  `parser_ver` 变更——它是一次**重跑全盘**，不是一次参数微调。要动它，把它当成
  一次版本升级来做。
- ⚠ **二分搜索依赖计数器的"前缀单调性"**（往窗口尾巴上加字符，计数不会减少）。
  `estimate_tokens` 满足（它的 `ceil` 是按"非空白字符总数"一次性取的整），但一个
  **不单调的注入计数器**会让搜索**静默地找不到那个最大的 `end`**——不是报错，
  是悄悄切短一片。测试里有两条盯着这条假设。
- **空白不计 token**。一篇由空白堆出来的正文只会切出很少的片，而每片的**字符数**
  可能远超 512：`target_tokens` 是 token 预算，不是字符预算，这两者只有在"正文
  密度正常"时才近似等价。
- **不偏好断句、不偏好换行**：v1 就是在 token 边界硬切，一片可能从半句话开始。
  这是**已知的质量缺口，不是遗漏**——"偏到最近的句号"能换回多少召回、付出多少
  片数膨胀，得等 `P5` 的 20 条评测（`A4`）说话；现在加这条规则只是换一个**同样
  没测过**的说法，还把"边界是输入的函数"这条确定性弄松了。留到 `A4` 之后按读数定。
- 与 `extract/_text.py` 那个 `MAX_CHARS = 1e6` 是**两件事**：那个管"一篇正文最多
  提取多长"（提取层），这里管"切开之后最多几片"（分块层）。两个上限各自记各自的
  截断标志（`ExtractedDoc.truncated` 与 `Chunk.truncated`）。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from localsearch.core.models import Chunk

__all__ = ["estimate_tokens", "split"]

#: 宽字符区段：CJK 标点、假名、CJK 扩展 A、CJK 统一表意、谚文音节、CJK 兼容
#: 表意、全角形式。**就这些**——多一个区段就多一份"哪些字算一个 token"的说法，
#: 而这个数的用途只是"窗口大概多大"，不值得为它把 Unicode 全表背下来。
#:
#: ⚠ 第一段从 `U+3001` 起、**不含 `U+3000`**（全角空格）：下面那个减法
#: （`非空白字符数 - 宽字符数`）只有在"宽字符区段里没有任何空白"时才不会是负数。
#: 谁要往里加一段（`U+2000-U+206F` 那类通用标点里就藏着好几个空白），
#: `test_chunk_sliding.py` 里有一条测试会把这件事喊出来。
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x3001, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFF60),
)

#: 由上面那张表**现生成**（不另写一份字面量：名单与名单的使用者分开维护，
#: 迟早会有一份先烂）。不加 `+`：`subn` 要数的是**字符**个数，不是连续段的个数。
_WIDE_RE = re.compile(
    "["
    + "".join(
        f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in _WIDE_RANGES
    )
    + "]"
)


def estimate_tokens(text: str) -> int:
    """`count_tokens` 的默认实现：**字符启发式**，不是分词器（理由见模块文档）。

    规则：宽字符 1 个 token，其余**非空白**字符 4 个 1 个 token（不足 4 个也算
    1 个）。

    ⚠ 它必须满足**前缀单调性**（`estimate_tokens(a) <= estimate_tokens(a + b)`），
    因为窗口搜索靠二分。当前实现满足：三类字符各自只增不减，而那个 `ceil` 是按
    "非空白字符总数"**一次性**取的整——不是每 4 个字符取一次整（那样会在
    "abc" + "de" 这种拼接上不单调，也就做不了前缀和）。改这个函数之前先想清楚
    这一条。

    ⚠ 两趟都是 C 层扫描：字符类正则找宽字符、`str.split()` 找空白。这个函数会被
    调得**非常**频繁（每片十几次、每次一整个窗口），Python 层的逐字符循环在这里
    是主要开销。
    """
    wide = _WIDE_RE.subn("", text)[1]
    # `split()` 按 Unicode 空白切，返回的段数远少于字符数，所以这趟求和的
    # Python 层开销是"段数"级的而不是"字符数"级的。
    narrow = sum(len(part) for part in text.split()) - wide
    return wide + (narrow + 3) // 4


def split(
    text: str,
    *,
    target_tokens: int,
    overlap_ratio: float,
    max_chunks_per_doc: int,
    count_tokens: Callable[[str], int] = estimate_tokens,
) -> list[Chunk]:
    """把 `text` 切成 `list[Chunk]`。规则见模块文档。

    ⚠ **三个数字参数没有默认值**，这是有意的：它们的默认值只有两处——`core/config.py`
    的 `ChunkCfg` 与 `config/default.toml`（两条的一致性由 `test_config.py` 盯着）。
    这里再写一遍就是第三处，而"同一件事只允许有一个真相来源"。

    所以正常的调用就是 `split(doc.text, **asdict(cfg.chunk))`：参数名与
    `ChunkCfg` 的字段名**逐字相同**，改名时两边一起改（`test_chunk_sliding.py`
    里有一条测试喂的正是 `asdict(ChunkCfg())`，它会在改名时红）。
    """
    _check_args(target_tokens, overlap_ratio, max_chunks_per_doc)
    if not text.strip():
        return []

    #: 先按"不截断"收着，收满 `max_chunks_per_doc` 片就不再收——**但继续走**，
    #: 因为统计摘要要说的是"本来一共多少片"。走完之后如果发现超了，回头把最后一个
    #: 名额让给占位片（规则 5）。
    #:
    #: ⚠ 不能在这里先扣掉那一个名额（`max_chunks_per_doc - 1`）：**没截断**的时候
    #: 扣的这一片是真的丢了。上限为 1、正文又只有一片时，"空出一个名额等着放摘要"
    #: 的结果是这篇正文**一片都不剩**而且也没有摘要——一个合法的配置换回一段
    #: 谁都搜不到的正文。这个 bug 在写这一条注释之前刚刚发生过。
    windows: list[tuple[int, int, int]] = []   # (窗口右端, 裁过空白的 left, right)
    #: 这篇正文**本来**会切出多少片（含没保留的那些）。占位片上的统计就是它。
    total = 0

    start = 0
    limit = len(text)
    while start < limit:
        end = _window_end(text, start, target_tokens, count_tokens)
        left, right = _trim(text, start, end)
        if right > left:                       # 规则 4：整片空白不算一片
            total += 1
            if len(windows) < max_chunks_per_doc:
                windows.append((end, left, right))
        if end == limit:
            # 规则 7：窗口碰到正文末尾了，到此为止。后面再切出来的每一片都从
            # `start` 之后的某个位置开始、仍然停在 `limit`，也就是**这一片的
            # 子集**；它们各占一个向量与一行 FTS，内容一个字都不多。没有这一条
            # 的话，重叠会把末尾切成一串越来越小、互相包含的片（512 -> 76 -> 11
            # -> 1，`overlap_ratio = 0.15` 下每次缩到上一片的 15%）。
            break
        # 规则 3 的重叠 + 进度保证 2。`overlap < span` 由 `overlap_ratio < 1`
        # 保证，所以那个 `max` 今天走不到——留着是因为这一层的失败形态是**不终止**，
        # 而它只要一次比较。
        start = max(end - int(overlap_ratio * (end - start)), start + 1)

    truncated = total > max_chunks_per_doc
    if truncated:
        del windows[max_chunks_per_doc - 1 :]  # 让出最后一个名额给统计摘要

    chunks = [
        Chunk(
            ord=index,
            text=text[left:right],
            char_start=left,
            char_end=right,
            n_tokens=count_tokens(text[left:right]),
        )
        for index, (_, left, right) in enumerate(windows)
    ]
    if truncated:
        # "还有多少字符没被索引"按**窗口**的右端算（不是裁过空白的那个），否则
        # 最后那点空白会被算成"没索引"。
        covered = windows[-1][0] if windows else 0
        body = _summary_text(total, max_chunks_per_doc, len(windows), limit - covered)
        chunks.append(
            Chunk(
                ord=len(chunks),
                text=body,
                char_start=0,                  # 规则 6：它不是原文的一段
                char_end=0,
                n_tokens=count_tokens(body),
                truncated=True,
            )
        )
    return chunks


def _window_end(
    text: str, start: int, target_tokens: int, count_tokens: Callable[[str], int]
) -> int:
    """最大的 `end`（`start < end <= len(text)`）使窗口的计数 `<= target_tokens`。

    先**倍增**探出上界再二分：正文大多比一片短（倍增几步就撞到 `len(text)` 直接
    返回，一次二分都不用），而真正长的正文里倍增的探测点也落在"一片"附近，
    每次切片的 `count_tokens` 调用次数是 `O(log 片长)` 而不是 `O(log 全文长)`。
    """
    limit = len(text)
    if count_tokens(text[start : start + 1]) > target_tokens:
        # 进度保证 1：一个字符就超预算也得吐出一个字符，不能返回空窗口。
        return start + 1

    feasible = start + 1                       # 已知可行
    step = 1
    while feasible < limit:
        high = min(feasible + step, limit)
        if count_tokens(text[start:high]) <= target_tokens:
            if high == limit:
                return limit                    # 整条尾巴都装得下
            feasible = high
            step *= 2
            continue
        # 现在 `feasible` 可行、`high` 不可行，二分它们中间那段。
        while high - feasible > 1:
            middle = (feasible + high) // 2
            if count_tokens(text[start:middle]) <= target_tokens:
                feasible = middle
            else:
                high = middle
        return feasible
    return feasible                            # `start + 1 == limit`：只剩一个字符


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """把窗口两端的空白裁掉（规则 2）。全空白时返回一个空区间。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _summary_text(total: int, max_chunks: int, kept: int, rest_chars: int) -> str:
    """`D8` 那片统计摘要的正文。

    它是**一句说明**，不是原文的一段——所以它的 `char_start` / `char_end` 一律写
    0（`Chunk` 的文档里定的）。它自己也会进 FTS、也会被嵌进向量，这是有意的：
    搜"未索引"这类词时能碰到它，比在结果里留一个**查不到的缺口**强。
    """
    return (
        f"【已截断】这篇正文太长：一共切出了 {total} 片，超过每文档 {max_chunks} 片的"
        f"上限（D8），所以只保留了开头的 {kept} 片，剩下的 {total - kept} 片"
        f"（约 {rest_chars} 字符）没有被索引，搜不到。"
    )


def _check_args(target_tokens: int, overlap_ratio: float, max_chunks_per_doc: int) -> None:
    """挡住三种会让这一层**不终止**或产出无意义结果的参数。

    配置层（`core/config.py` 的 `_POSITIVE_KEYS` 与 `_RANGES`）已经挡过一道，
    这里再挡一道不是重复：那一层管的是"配置文件里写了什么"，而这一层的失败形态
    是**死循环**——调用方可以不经过配置（测试、将来的 REPL、直接 import），
    而卡住的那一轮索引不会留下任何报错。
    """
    if target_tokens < 1:
        raise ValueError(f"target_tokens 必须 >= 1，收到 {target_tokens}")
    if max_chunks_per_doc < 1:
        raise ValueError(f"max_chunks_per_doc 必须 >= 1，收到 {max_chunks_per_doc}")
    if not 0.0 <= overlap_ratio < 1.0:
        # `>= 1` 会让"退回 overlap_ratio * 片长"退到起点之前，也就是原地打转。
        raise ValueError(f"overlap_ratio 必须落在 [0, 1)，收到 {overlap_ratio}")
