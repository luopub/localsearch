r"""`index/tokens.py` 那套切法的读数（`F4-01`）。

------------------------------------------------------------------------------
为什么要有这个
------------------------------------------------------------------------------

`A8` 要求"全盘跑完 Tier0 后**立刻能按文件名搜到**"。这句话能不能成立，全看中文怎么切：
`D10` 的 `unicode61` 一个汉字都不切（`M9`），所以入库前得先用 jieba 切开。而 jieba 有
两种切法，**选错了不会报错，只会搜不到**——

* `jieba.cut`（精确模式）：`财务报表` 是一个 token。
* `jieba.cut_for_search`（搜索引擎模式）：`财务` / `报表` / `财务报表` 都倒出来。

`M9` 证明"2 字词要能搜到"用的语料是**一句话恰好被切成 2 字词**的情形，那时两种切法
结果一样，所以它证明的是"必须切"、不是"该怎么切"。本探针把语料换成真实文件名，
把"该怎么切"量出来。

------------------------------------------------------------------------------
判据是机械的：**原名字符串包含**，不是我的判断
------------------------------------------------------------------------------

真阳性（oracle）= "这个名字里**原样包含**查询串"。于是 2 字查询 `报表` 的真阳性
就是 `财务报表.xlsx`、`2024年度财务报表汇总.xlsx` 这类。

窗口只取**主文件名里含至少一个汉字的 2/3 字连续子串**：

* **纯 ASCII 的窗口不算**——英文的词就是词，用户敲 `bc` 不该匹配 `abc.txt`，
  这不是缺陷。而汉字里 2 字词**就是**词法单位，敲 `报表` 必须匹配 `财务报表`。
* **不含扩展名**——`表.x` 这种跨"名字.扩展名"边界的窗口两种切法都必然落空，
  留着它只会把差别冲淡。
* ⚠ **有下限，量出来就是有下限**：`务报` 这种**跨词**窗口（落在 `财务|报表` 的缝上）
  两种切法都不是 token。这个下限是词法索引的固有性质，本探针把它**单独报一行**
  而不是藏进总数——它决定了将来要不要补一层真正的 2-gram。

------------------------------------------------------------------------------
它量四件事
------------------------------------------------------------------------------

1. 两种切法在几串真实文件名上的**逐条对照**（表里那些话的证据）。
2. **召回**：7 种组合（索引侧切法 × 查询侧切法 × 丢不丢单字汉字 × 有没有 OR 兜底）
   对上面那批窗口查询各差多少，顺带报平均返回条数（精确度的粗糙代理）。
3. **开销**：索引侧每个文件名 / 每 2 KB 正文多少 µs，查询侧每条多少 µs，
   以及 jieba 首次加载词典那笔一次性的钱。
4. 三个**长查询**逐条打印命中——前三个分区的数字都好看时，人还是会这样打字。

⚠ 语料是**现造**的（真实文件名形状的字符串），不读盘、不碰任何用户文件、跑完什么
也不留下。索引用的是**真 FTS5**（内存库），不是模拟匹配。

用法：

    PYTHONIOENCODING=utf-8 uv run python tools/bench_tokenize.py
"""

from __future__ import annotations

import sqlite3
import time

import jieba

from localsearch.core.console import use_safe_output
from localsearch.index.tokens import (
    HAN,
    and_expr,
    han_bigrams,
    keep_token,
    or_expr,
    query_tokens,
    tokenize,
)

FTS_DDL = "CREATE VIRTUAL TABLE {t} USING fts5(name_tok, tokenize = 'unicode61 remove_diacritics 2')"

#: 真实文件名形状的语料。带路径分隔符的那几条是故意的：Tier0 索引的是**完整路径**，
#: 盘符与目录名也会进 token 流。
CORPUS: tuple[str, ...] = (
    r"D:\work\项目\2024\财务报表.xlsx",
    r"D:\work\项目\2024\2024年度财务报表汇总.xlsx",
    r"D:\work\项目\预算\项目预算表.xlsx",
    r"D:\work\doc\产品需求文档PRD.docx",
    r"D:\work\doc\本地离线全盘全文搜索需求说明.docx",
    r"D:\work\doc\数据库设计说明书-v2.docx",
    r"D:\work\doc\会议纪要-第3次.docx",
    r"D:\work\doc\员工手册.pdf",
    r"D:\work\doc\中华人民共和国专利法.pdf",
    r"D:\work\校园\北京大学研究生招生简章.md",
    r"D:\work\校园\清华大学本科生培养方案.md",
    r"D:\work\ml\深度学习模型训练日志.log",
    r"D:\work\src\CMakeLists.txt",
    r"D:\work\src\README.md",
    r"D:\work\pic\微信图片_20240101.jpg",
    r"D:\work\pic\屏幕截图 2024-01-01 101010.png",
)

#: 长查询：用户真的会这样打。逐条打印命中，不做汇总。
LONG_QUERIES: tuple[str, ...] = (
    "北京大学的研究生",
    "项目的预算表",
    "本地离线全盘全文搜索的需求说明",
)

#: 每一行是（索引侧切法，查询侧切法，查询侧丢不丢单字汉字，AND 落空后要不要 OR 兜底）。
#: 索引侧四种取值见 `INDEX_MODES`。第一行是"最朴素的实现"，其余是反例与对照——
#: **采用哪一行由读数决定，不由我决定**。
VARIANTS: tuple[tuple[str, str, bool, bool], ...] = (
    ("cut", "cut", False, False),
    ("cut", "cut", True, False),
    ("search", "cut", False, False),
    ("search", "cut", True, False),
    ("search", "search", True, False),
    ("search", "cut", True, True),
    ("cut", "cut", True, True),
    ("search+2g", "cut", True, False),
    ("search+2g", "cut", False, False),
    ("search+2g", "search+2g", True, False),
    ("cut+2g", "cut", True, False),
    ("cut+2g", "cut", False, False),
)

#: **实际采用的那一行**，`tokens.py` 与 `sqlite_store.py` 的行为就是它。
#: 第 4 节用它，所以"表里选中哪一行"与"长查询跑的是哪一套"不会各说各话。
ADOPTED: tuple[str, str, bool, bool] = ("search+2g", "cut", True, False)


def section(number: int, title: str) -> None:
    print(f"\n{'=' * 78}\n{number}. {title}\n{'=' * 78}")


def base_of(path: str) -> str:
    """取主文件名（去掉最后一段扩展名）；没有点就整串。"""
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    stem, dot, _ext = name.rpartition(".")
    return stem if dot else name


def has_han(s: str) -> bool:
    return bool(HAN.search(s))


def windows(text: str, n: int) -> list[str]:
    """`text` 里所有**全是汉字**的 n 字连续子串（去重，保序）。

    「全是汉字」这个限定是**判据的一部分**，不是省事：`书-` / `24年` 这种夹着标点或
    数字的窗口，任何按词切分的索引都服务不了（`员工手册.pdf` 的 token 里根本没有
    `书`），把它算进来只会把"2 字词召回"这个指标冲淡成一个说不清的混合数。
    用户搜中文词打的是连续的汉字，这一档才是 `M9` 关心的那一档。
    """
    seen: dict[str, None] = {}
    for i in range(len(text) - n + 1):
        piece = text[i : i + n]
        if all(has_han(ch) for ch in piece):
            seen.setdefault(piece, None)
    return list(seen)


def cut_by(text: str, mode: str) -> list[str]:
    return list(jieba.cut_for_search(text.lower()) if mode == "search" else jieba.cut(text.lower()))


def _tokens_of(text: str, mode: str) -> list[str]:
    """探针自己拼的 token 列表（要与 `tokens.py` 的实现**逐字节一致**）。"""
    words = "search" if mode.startswith("search") else "cut"
    out = [t for t in cut_by(text, words) if keep_token(t)]
    if mode.endswith("+2g"):
        out += han_bigrams(text.lower())
    return out


def q_tokens(query: str, mode: str, drop_single_han: bool) -> list[str]:
    raw = [t for t in cut_by(query, mode) if keep_token(t)]
    if not drop_single_han:
        return raw
    trimmed = [t for t in raw if not (len(t) == 1 and HAN.match(t))]
    return trimmed or raw


def han_bigrams(text: str) -> list[str]:
    """原文里**相邻两个汉字**的全部组合（跨词边界也照取）。

    这是"词法索引够不到跨词窗口"那条下限的对症药：`财务报表` 切出 `财务` / `报表`，
    **`务报` 不在里面**；`前来报到` 被 HMM 切成 `报` / `到`，**`报到` 也不在里面**。
    2-gram 不依赖词典，所以这两类都补得上——代价是 token 数涨，涨多少也要量。
    """
    out: list[str] = []
    for i in range(len(text) - 1):
        piece = text[i : i + 2]
        if all(has_han(ch) for ch in piece):
            out.append(piece)
    return out


def idx_text(text: str, mode: str) -> str:
    return " ".join(_tokens_of(text, mode))


def check_matches_shipped() -> None:
    """探针自己拼的那一套，与 `tokens.py` 里**真正发货的那个函数**逐字节一致吗？

    ⚠ 这一段是必需的，不是保险：本探针为了做对照，把 `tokens.py` 的切法**抄了一遍**
    （四种模式，其中两种模块里根本没有）。抄的那一份与发货的那一份一旦分叉，
    量出来的数就与代码无关了——而它们**长得一模一样**，没有任何症状。
    两个方向都查：采用的那一套必须完全相同，另外两种（没有 2-gram 的）也必须只有
    "少了 2-gram"这一处差别。
    """
    bad: list[str] = []
    for path in CORPUS:
        # 对比的**只能是**探针的 `search` 与 `search+2g`：`tokens.py` 的词那一半
        # 就是 `cut_for_search`，探针的 `cut` 模式是它的对照，本来就不该相等。
        if idx_text(path, ADOPTED[0]) != tokenize(path, bigrams=True):
            bad.append(f"采用的那一套（{ADOPTED[0]}）  {path}")
        if idx_text(path, "search") != tokenize(path, bigrams=False):
            bad.append(f"无 2-gram 的那一套（search）  {path}")
    if bad:
        raise SystemExit("探针与 shipped 的切法不一致，读数无效：\n  " + "\n  ".join(bad))
    print("自检：探针的切法与 tokens.py 的发货实现一致（两种模式各查一遍）")


# ---------------------------------------------------------------------------
# 1. 两种切法的对照
# ---------------------------------------------------------------------------

SAMPLES: tuple[str, ...] = (
    "财务报表.xlsx",
    "北京大学的研究生前来报到",
    "本地离线全盘全文搜索需求说明.docx",
    "会议纪要-第3次.docx",
)


def part1() -> None:
    section(1, "两种切法的对照（索引侧切出来的是什么）")
    for text in SAMPLES:
        cut = cut_by(text, "cut")
        search = cut_by(text, "search")
        print(f"\n原文  {text}")
        print(f"  cut        ({len(cut):2d})  {' · '.join(cut)}")
        print(f"  cut_search ({len(search):2d})  {' · '.join(search)}")


# ---------------------------------------------------------------------------
# 2. 召回
# ---------------------------------------------------------------------------


#: 索引侧的四种取值，与 `VARIANTS` 第一列一致。`+2g` = 在该词切法之上**再加**
#: 原文里相邻汉字的全部 2-gram。
INDEX_MODES: tuple[str, ...] = ("cut", "search", "cut+2g", "search+2g")

#: 模式 → 建哪张表。不用模式名直接当表名：`+` 不是合法的 SQL 标识符字符。
TABLES: dict[str, str] = {mode: f"f{i}" for i, mode in enumerate(INDEX_MODES)}


def build_db() -> sqlite3.Connection:
    """建内存 FTS5 库：每种切法一张表，行号与 `CORPUS` 下标一一对应。"""
    conn = sqlite3.connect(":memory:")
    for mode in INDEX_MODES:
        table = TABLES[mode]
        conn.execute(FTS_DDL.format(t=table))
        conn.executemany(
            f"INSERT INTO {table}(rowid, name_tok) VALUES (?, ?)",
            list(enumerate([idx_text(name, mode) for name in CORPUS])),
        )
    conn.commit()
    return conn


def truth_sets() -> list[tuple[str, set[int]]]:
    """（查询串, 真阳性行号集合）。真阳性 = **原名字符串包含**该查询串。

    同一个窗口在不同文件名里出现（`财务`）只留一条——否则高频窗口按次数加权，
    微召回的算术就变成了"高频词的召回"，与"随机挑一个窗口"不是一回事。
    """
    by_query: dict[str, set[int]] = {}
    for n in (2, 3):
        for path in CORPUS:
            for w in windows(base_of(path), n):
                hits = {j for j, other in enumerate(CORPUS) if w.lower() in other.lower()}
                assert hits, (path, w)
                by_query.setdefault(w, set()).update(hits)
    return sorted(by_query.items())


def run_variant(
    conn: sqlite3.Connection,
    queries: list[tuple[str, set[int]]],
    index_mode: str,
    query_mode: str,
    drop_single_han: bool,
    fallback_or: bool,
) -> tuple[int, int, int, int, int, int]:
    """跑一种组合，返回（真阳性总数, 找到的, 全对的查询数, 返回条数合计, 兜底次数, 一条没找到的查询数）。"""
    table = TABLES[index_mode]
    total = found = perfect = returned = fallbacks = zero = 0
    for q, truth in queries:
        toks = q_tokens(q, query_mode, drop_single_han)
        total += len(truth)
        rows: list[int] = []
        expr = and_expr(toks)
        if expr is not None:
            rows = [r[0] for r in conn.execute(f"SELECT rowid FROM {table} WHERE name_tok MATCH ?", (expr,))]
        if not rows and fallback_or:
            expr = or_expr(toks)
            if expr is not None:
                rows = [r[0] for r in conn.execute(f"SELECT rowid FROM {table} WHERE name_tok MATCH ?", (expr,))]
                fallbacks += 1
        hit = set(rows)
        returned += len(hit)
        found += len(hit & truth)
        perfect += hit >= truth
        zero += not (hit & truth)
    return total, found, perfect, returned, fallbacks, zero


def query_side_divergence(queries: list[tuple[str, set[int]]]) -> tuple[int, int, list[str]]:
    """查询侧两种切法**真的分叉**的地方，以及分叉里的代价。

    AND 语义下多一个 token 只能让结果**变少**——所以 `cut_for_search` 在查询侧要么无事
    发生、要么丢命中，不可能多找到东西。返回（分叉的查询数, 其中多出"语料里根本没有的
    token"的查询数, 例子）。

    这个度量**替换了**第一版的"幽灵 token 计数"：那一版对两种切法给出同一个数
    （54 条 / 55 个），因为它数的是**窗口本身的毛病**（`书-` 里的 `书`），与切法无关。
    """
    universe: set[str] = set()
    for path in CORPUS:
        universe.update(idx_text(path, "search").split())
    diverged = 0
    harmful = 0
    samples: list[str] = []
    for q, _ in queries:
        coarse = set(q_tokens(q, "cut", True))
        fine = set(q_tokens(q, "search", True))
        if coarse == fine:
            continue
        diverged += 1
        extra = fine - coarse
        if any(t not in universe for t in extra):
            harmful += 1
            if len(samples) < 6:
                samples.append(f"{q} 多出 {'/'.join(sorted(extra))}")
    return diverged, harmful, samples


def part2(conn: sqlite3.Connection, queries: list[tuple[str, set[int]]]) -> None:
    section(2, "召回：主文件名里含汉字的 2/3 字窗口查询")
    n2 = sum(1 for q, _ in queries if len(q) == 2)
    print(f"语料 {len(CORPUS)} 条；窗口查询 {len(queries)} 条（2 字 {n2} / 3 字 {len(queries) - n2}）")
    print("微召回 = 找到的真阳性 / 真阳性总数；「全对」= 该查询的真阳性一条不漏；")
    print("「全落空」= 该查询的真实命中一条都没找到（下限，跨词窗口是主要来源）\n")
    head = (
        f"{'索引侧':>7} {'查询侧':>7} {'丢单字':>7} {'OR兜底':>7} | "
        f"{'微召回':>7} {'全对':>8} {'全落空':>7} {'返回合计':>8} {'兜底次数':>8}"
    )
    print(head)
    print("-" * (len(head) + 8))
    for im, qm, drop, fb in VARIANTS:
        total, found, perfect, returned, fallbacks, zero = run_variant(conn, queries, im, qm, drop, fb)
        mark = "  ← 采用" if (im, qm, drop, fb) == ADOPTED else ""
        print(
            f"{im:>7} {qm:>7} {str(drop):>7} {str(fb):>7} | "
            f"{found / total:>6.1%} {perfect:>4d}/{len(queries):<3d} {zero:>7d} "
            f"{returned:>8d} {fallbacks:>8d}{mark}"
        )
    diverged, harmful, samples = query_side_divergence(queries)
    print(f"\n查询侧 cut vs cut_for_search：**{diverged}/{len(queries)}** 条查询的 token 集合不同"
          f"（其余 {len(queries) - diverged} 条两种切法**一模一样**，所以召回也不可能不同）")
    print(f"  其中多出来的 token **语料里任何一条都没有**的：**{harmful}** 条 —— 这些在 AND 下必定落空")
    if samples:
        print(f"  例：{'  '.join(samples)}")

    # 采用的那一套还够不到的查询：单独印出来，别让"98% 召回"盖住那 2 条是什么
    im, qm, drop, fb = ADOPTED
    table = TABLES[im]
    missed: list[str] = []
    for q, truth in queries:
        expr = and_expr(q_tokens(q, qm, drop))
        rows = {r[0] for r in conn.execute(f"SELECT rowid FROM {table} WHERE name_tok MATCH ?", (expr,))} if expr else set()
        if not (rows & truth):
            missed.append(f"{q}（该中的 {len(truth)} 条）")
    print(f"\n采用的那一套（{im}）**一条都没找到**的查询：{len(missed)}/{len(queries)} 条")
    if missed:
        print(f"  {'  '.join(missed)}")


# ---------------------------------------------------------------------------
# 3. 开销
# ---------------------------------------------------------------------------

#: 正文样本：**大量互不相同的句子**拼起来，重复的只有整张表。
#: ⚠ 第一版写的是一句话 ×40（`"……测试正文。" * 40`），于是 2-gram **去重**把它的
#: 增量抹掉了一大半，印出"正文只涨 1.16×"——**那是语料的性质，不是正文的性质**。
#: 真实正文里的 2-gram 大部分各不相同，所以下面这个比值应当读作**下界**。
SENTENCES: tuple[str, ...] = (
    "本地离线全盘全文搜索的目标是把整台机器上的文档变成可以被检索的东西",
    "索引的第一层只记录文件名与完整路径因此几分钟就能跑完",
    "第二层要打开每一个文件把正文抽出来再切成分片",
    "第三层才是向量语义检索它需要模型与显卡之外的漫长时间",
    "分层的意义在于任何一层没跑完时仍然可以降级使用",
    "一个文件解析失败不能中断整轮索引失败的原因要写进数据库",
    "排除规则放在项目根目录的忽略文件里用户可以自己修改",
    "配置文件里的绝对路径属于本机信息因此不进版本库",
    "数据库使用预写日志模式以便读写可以同时进行",
    "全文检索由虚拟表提供中文需要预先切词否则整句会被当成一个词",
    "切片长度与重叠比例直接影响召回率与存储占用",
    "内容去重让同一份文档的多个副本只被嵌入一次",
    "任何被索引的数据都不离开这台机器网络只用于首次下载模型",
    "日志文件写在数据目录里而数据目录本身不在索引范围内",
    "每一个功能点都要有自动化测试否则不算完成",
    "测试通过的含义是在某个确切的提交上通过而不是永远通过",
    "测出来的数字要立刻记进进度文档并写上是哪台机器什么时候测的",
    "没有测过的东西必须标注出来不能与实测过的混在一起",
    "命令行工具优先于图形界面因为前者可以被脚本调用",
    "交互式界面是后面阶段的事情现在假装有它只会更难查",
    "输出里的多字节字符在重定向时可能编不出来所以要留一层降级",
    "计划任务的上线要等到开发完成之后",
    "内存中的索引需要允许用户指定大小而默认值要足够保守",
    "扫描后端可以有多个但任何一个都要能被换掉",
    "文件记录号可以识别改名之后的同一个文件",
)


def _body() -> str:
    """把 `SENTENCES` 整张表重复到至少 `BODY_CHARS` 个字符。"""
    one = "。".join(SENTENCES) + "。"
    return one * max(1, -(-BODY_CHARS // len(one)))


#: 正文样本的目标长度（字符）。
BODY_CHARS = 4000


#: jieba 首次调用（加载词典）的毫秒数。`main()` 在**碰 jieba 之前**填上它，
#: 因为之后再量就量不到了——那笔开销只在第一次调用里。
COLD_MS = float("nan")


def measure_cold_start() -> None:
    """量 jieba 加载词典那一下。

    ⚠ **必须把生成器走完**：`jieba.cut()` 返回的是**生成器**，不迭代它就一个字符都不切、
    词典也不加载。第一版写的是 `jieba.cut("预热")`（没消费），印出来 `0.0 ms`——
    而真值是 **708 ms**（`lsearch search` 这种一次性命令每次都要付）。
    """
    global COLD_MS
    t0 = time.perf_counter()
    for _ in jieba.cut("预热"):
        pass
    COLD_MS = (time.perf_counter() - t0) * 1000


def timeit(fn, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def part3() -> None:
    section(3, "开销")
    names = [base_of(p) for p in CORPUS]
    # ⚠ 这一行**必须**在 part1/part2 之前量（见 `measure_cold_start`）：jieba 的词典是
    # 首次调用时才加载的，晚一步量到的就是一次热调用——第一版正是这么写的，印出 0.0 ms。
    print(f"jieba 首次调用（加载词典，一次性）  {COLD_MS:>9.1f} ms")
    print("   ⚠ 这笔钱 `lsearch search` 这种**一次性命令**每次都要付；REPL 只付一次")

    n = len(names)
    print("索引侧 切一个主文件名（每种模式各 20 轮取最快）")
    for mode in INDEX_MODES:
        t = timeit(lambda m=mode: [idx_text(x, m) for x in names], 20) / n
        print(f"    {mode:>9}  {t * 1e6:>7.1f} µs")

    body = _body()
    t_body = timeit(lambda: idx_text(body, ADOPTED[0]), 5)
    print(f"索引侧 切 {len(body)} 字符正文     {t_body * 1000:>8.1f} ms  ({t_body / len(body) * 1e6:.2f} µs/字符)")

    for qmode in ("cut", "search"):
        t_q = timeit(lambda m=qmode: q_tokens("北京大学的研究生", m, True), 200)
        print(f"查询侧 切一条查询（{qmode:>6}）      {t_q * 1e6:>7.1f} µs")

    # 2-gram 的价钱：往 FTS 里塞的 token 数涨多少（涨的每一份都要落盘、都要建索引）
    counts = {m: sum(len(idx_text(p, m).split()) for p in CORPUS) for m in INDEX_MODES}
    print(f"\n语料 {len(CORPUS)} 条的 token 总数（名字 + 路径，全部合起来算）")
    for mode in INDEX_MODES:
        ratio = counts[mode] / counts["cut"]
        print(f"    {mode:>9}  {counts[mode]:>4} 个   = cut 的 {ratio:.2f}×")
    print(f"   ⚠ 这是**短文件名 + 路径**的比值；正文里的比值只受汉字占比影响，不随长度变")
    print("   ⚠ 名字那一列里有盘符、目录名、扩展名这些**产词但不产 2-gram**的内容，")
    print("     所以它的比值明显低于正文：正文的汉字占比接近 100%，词与 2-gram 大约是 1 : 1.8")
    body_counts = {m: len(idx_text(body, m).split()) for m in INDEX_MODES}
    print(f"\n{len(body)} 字符正文的 token 数（这才是 `chunks_fts` 的落盘量）")
    for mode in INDEX_MODES:
        print(f"    {mode:>9}  {body_counts[mode]:>5} 个   = cut 的 {body_counts[mode] / body_counts['cut']:.2f}×")


# ---------------------------------------------------------------------------
# 4. 长查询逐条
# ---------------------------------------------------------------------------


def part4(conn: sqlite3.Connection) -> None:
    im, qm, drop, fb = ADOPTED
    section(4, f"长查询：用真正采用的那一套（索引 {im} / 查询 {qm} / 丢单字 {drop} / OR 兜底 {fb}）")
    for q in LONG_QUERIES:
        toks = q_tokens(q, qm, drop)
        expr = and_expr(toks)
        rows = [r[0] for r in conn.execute(f"SELECT rowid FROM {TABLES[im]} WHERE name_tok MATCH ?", (expr,))] if expr else []
        how = "AND"
        if not rows and fb:
            expr = or_expr(toks)
            rows = [r[0] for r in conn.execute(f"SELECT rowid FROM {TABLES[im]} WHERE name_tok MATCH ?", (expr,))] if expr else []
            how = "OR 兜底"
        print(f"\n查询  {q}\n  token  {toks}\n  {how} → {len(rows)} 条")
        for r in rows[:4]:
            print(f"    {CORPUS[r]}")


def main() -> None:
    # 输出兜底放在 main() **里面**（`tests/unit/test_console_output.py` 的纪律面
    # 逐条扫 tools/ 下的 main()）：本脚本打印的语料与切法里全是中文，重定向到文件时
    # Python 落回 ANSI 代码页 936，一个 `⚠` 就能让它当场崩（`E37`）。
    use_safe_output()
    measure_cold_start()  # 必须最先：它量的是"第一次碰 jieba"那一下
    check_matches_shipped()
    part1()
    conn = build_db()
    part2(conn, truth_sets())
    part3()
    part4(conn)
    conn.close()
    print("\n（语料为现造字符串，未读写任何用户文件）")


if __name__ == "__main__":
    main()
