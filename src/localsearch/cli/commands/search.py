r"""`lsearch search`（`F4-04` 起：**最小的那一版**，只有词法）。测试落点
`tests/unit/test_cli_search.py`。

`P4` 的搜索面刻意小：能按文件名/路径搜（Tier0 的 `files_fts`）、能按正文搜（Tier1 的
`chunks_fts`），**不做融合排序**（那是 `P5` 的 `search/fusion.py`，RRF 需要语义那一路
才有得融）。两段结果分开印，各自按自己的 BM25 排——一个"看起来像融合过"的排序是
这一版最不该给的东西：用户没有办法从输出里看出它没融。

------------------------------------------------------------------------------
`--mode`：显式要语义 → 拒绝；配置默认成混合 → 降级并标注
------------------------------------------------------------------------------

判据是"这句话是不是人说的"（`search/tier.py` 的 `explicit`）：

* `--mode semantic` / `--mode hybrid` → `ModeUnavailable`，退出码 2。**不悄悄换成词法**：
  一个人明确要了语义检索，词法会给他一堆看起来合理的关键词命中，而他没法察觉
  自己拿到的是另一种东西。
* 配置里 `mode = "hybrid"`（模板的缺省值）→ 降到 `lexical`，**并打一条 note**，
  那句话里逐字含「语义未就绪」（`A8` 的可观察形态）。

注意**两种都要标注**：第二种虽然没报错，但它同样回答了另一个问题——区别只在
"要不要连拒绝一起给"。所以 `Readiness.notes` 无论哪条路都会带上那句话。

------------------------------------------------------------------------------
只声明真能兑现的参数：`--ext` / `--under` / `--after` / `--json` 都没有
------------------------------------------------------------------------------

计划 §4.6 给 `search` 列了 `--ext pdf,md` / `--under PATH` / `--after 2026-01-01` /
`--json` 四个。本次**一个都不声明**，因为 `search_paths()` / `search_chunks()` 都不收
过滤参数，而"声明了却忽略"正是这个项目反复吃亏的那一类错（`--workers 8` 给了、
没反应、也不报错，用户会以为它生效了）：`--ext pdf` 被忽略的结果是"搜出来的还是
全格式"，看起来完全正常。Click 对未声明的参数回 `No such option: --ext` + 退出码 2
——明确，只是没解释为什么没有。理由记在 `PROGRESS.md` §4 P4「偏差」里，归 `P5`。

------------------------------------------------------------------------------
库不存在：**先看再接**
------------------------------------------------------------------------------

`sqlite3.connect()` 会**建**文件，所以"库不存在"这条路必须手上一句
`config.db_path.exists()`：不查的话，在一个空数据目录上跑一次 `lsearch search`，
得到的是"什么都没搜到"加一个**刚被创建出来的空库**——而那个空库会让下一次
`lsearch index` 之外的所有命令都以为"索引建好了"（`D10` 的就绪台账在空库上是
"不是就绪"，还好，但目录已经在了）。退出码 2，与"用法不对"同档。
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

from localsearch.cli.state import CliState
from localsearch.core.config import TiersCfg
from localsearch.index.sqlite_store import (
    ChunkHit,
    ChunkSearch,
    PathSearch,
    SqliteStore,
)
from localsearch.index.tier import (
    TIER_FULLTEXT,
    TIER_METADATA,
    TIER_SEMANTIC,
    TierLedger,
    ledger_from,
    target_tier,
    tier_name,
)
from localsearch.search.tier import MODES, ModeUnavailable, Readiness, resolve

EXIT_OK = 0
#: 与 `cli/main.py`、`cli/commands/index.py` 同一个惯例值（Click 对用法错误的取值）。
EXIT_USAGE = 2

#: 正文命中印多少字符的上下文（前后各这么多）。**不是排版参数，是可用性参数**：
#: 一片正文最多 `[chunk] target_tokens` 个 token，整片印出来会把终端刷掉，而
#: "这个词在这份文件的哪里"才是人要看的东西。
SNIPPET_CHARS = 90

console = Console()


def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="查询词：自然语言或关键词"),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help=f"检索模式（{' / '.join(MODES)}）；缺省用配置里的值",
    ),
    limit: int = typer.Option(
        0, "--limit", min=0, help="每段最多几条；缺省用配置里的 search.top_k"
    ),
) -> None:
    """在本机索引里检索（P4：词法；语义那一半要等 P5）。"""
    state: CliState = ctx.obj
    config = state.config
    db_path = config.db_path
    if not db_path.exists():
        # 见模块文档：`sqlite3.connect()` 会建文件，所以这一句是**必须**的。
        typer.secho(f"还没有索引库：{db_path}", fg="red", err=True)
        typer.echo("先跑一次 `lsearch index`（或 `lsearch index --dry-run` 先看看扫什么）。", err=True)
        raise typer.Exit(EXIT_USAGE)

    explicit = mode is not None
    requested = mode if mode is not None else config.search.mode
    top_k = limit or config.search.top_k

    with SqliteStore.open(db_path) as store:
        # ⚠ 台账在**这一趟开始时**取一次，往下只用这一份：`resolve()` 不读库，所以它
        # 不知道手里的数是不是旧的。放到格式化那一层现取的话，标注说的就是另一时刻
        # 的库（`tier.resolve()` 的文档里点了这一条）。
        ledger = ledger_from(store.tier_counts())
        try:
            readiness = resolve(requested, ledger, tiers=config.tiers, explicit=explicit)
        except ModeUnavailable as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        except ValueError as exc:
            # 配置里 `mode` 写了个错字——那一格只能是配置的问题（`--mode` 有下面
            # 那道校验兜着），所以按"用法不对"退出而不是甩栈。
            typer.secho(f"配置里的 search.mode 不认识：{exc}", fg="red", err=True)
            raise typer.Exit(EXIT_USAGE) from exc

        paths = store.search_paths(query, limit=top_k)
        chunks = store.search_chunks(query, limit=top_k)
        render(readiness, paths, chunks, store=store, ledger=ledger, tiers=config.tiers)


def render(
    readiness: Readiness,
    paths: PathSearch,
    chunks: ChunkSearch,
    *,
    store: SqliteStore,
    ledger: TierLedger,
    tiers: TiersCfg,
) -> None:
    """把这一趟的结果与标注打出来。

    ⚠ **每一段动态文本都要过 `escape()`**（`cli/commands/index.py` 那段段首注里
    列了本项目已经踩过的三次）：这里的动态串比那边更多——路径、正文片段、以及
    note 里的数字。正文片段尤其危险：它是**用户文件里的字**，出现方括号再正常不过。
    """
    _render_notes(readiness, ledger, tiers=tiers)
    if not paths.has_query:
        # `has_query` 为假 = 查询串里**一个 token 都没留下**。它只有一种成因：整串
        # 都是标点或空白（`keep_token()` 把纯标点丢掉）——⚠ **不是"单个汉字"**：
        # `tokens.query_tokens()` 里那条"全是单字时退回丢之前的列表"是刻意的
        # （用户真的在搜"书"，丢掉它等于什么都不搜），所以搜单字是**正常查询**，
        # 它走的是下面那条 0 命中的路。这句话写错会让人以为工具丢了他的查询。
        console.print(
            "\n[bold]这个查询里没有可以搜的词[/bold]：整串都是标点或空白。"
            "搜一个词或者一个字都行。"
        )
        return

    _render_paths(paths)
    _render_chunks(chunks, store=store)


def _render_notes(readiness: Readiness, ledger: TierLedger, *, tiers: TiersCfg) -> None:
    """标注：这一趟按什么模式检索、差了多少、`D10` 要求的那句「语义未就绪」。"""
    if readiness.degraded:
        console.print(
            f"检索模式      [bold]{escape(readiness.mode)}[/bold]"
            f"（要的是 {escape(readiness.requested)}）"
        )
    else:
        console.print(f"检索模式      [bold]{escape(readiness.mode)}[/bold]")
    _render_readiness(ledger, tiers=tiers)
    for note in readiness.notes:
        # 一律 escape：note 的文本里有进度数字，将来也可能带上别的动态内容
        # （`search/tier.py` 那一侧不负责排版，所以这边不能假设它是干净的）。
        console.print(f"[yellow]⚠ {escape(note.message)}[/yellow]")


def _render_readiness(ledger: TierLedger, *, tiers: TiersCfg) -> None:
    """一行台账：三级各自"做到多少 / 该做多少"。

    ⚠ **只印配置允许到的那几级**：配置里 `fulltext = false` 时 Tier1 永远不会做，
    此时还印一个恒为 `0 / N` 的正文进度，就是一句**永远为假的坏消息**——而永远
    为假的警告会被学会忽略，连带着真的那一条一起（`search/tier.py` 的模块文档
    把这条写成了"永远为真的废话"的反面）。判据与它用的是同一个函数。
    """
    ceiling = target_tier(tiers)
    if ceiling < TIER_METADATA:
        return
    parts = []
    for tier in (TIER_METADATA, TIER_FULLTEXT, TIER_SEMANTIC):
        if tier > ceiling:
            continue
        done, expected = ledger.progress(tier)
        parts.append(f"{tier_name(tier)} {done}/{expected}")
    console.print(f"分级就绪      {' · '.join(parts)}")


def _render_paths(paths: PathSearch) -> None:
    """Tier0：按文件名/路径命中的那些。"""
    hits = paths.hits
    console.print(f"\n[bold]按文件名/路径[/bold]  {len(hits)} 条")
    if not hits:
        console.print("  （一条都没有）")
        return
    for hit in hits:
        # 路径过长时让它折行而不是截断：截断会把"在哪个目录下"这条信息切掉，
        # 而那正是 Tier0 命中的理由（名字与路径一起算分）。
        console.print(f"  {escape(hit.path)}")
        if hit.error_msg:
            # 顺手把这一行的结局带出来：一个 `error` 的文件名能搜到、正文搜不到，
            # 用户此刻最需要知道的正是"为什么只有名字"。
            console.print(f"    [dim]{escape(hit.status)}：{escape(hit.error_msg)}[/dim]")


def _render_chunks(chunks: ChunkSearch, *, store: SqliteStore) -> None:
    """Tier1：按正文命中的那些。**按内容（blob）分堆**（`D12`）。

    同一个 blob 的多个切片命中同一个查询是常态（一份长文档里同一个词出现十次），
    而用户要的是"哪几个文件里有这个词"，不是"哪十片里有"。分堆之后每一堆印的是
    **文件路径列表**（`paths_for_blob()`）——同一份内容被好几个路径指着的那些
    副本因此一次就露出来（`D12` 去重在结果面上的落点）。
    """
    hits: tuple[ChunkHit, ...] = chunks.hits
    console.print(f"\n[bold]按正文[/bold]  {len(hits)} 片")
    if not hits:
        console.print("  （一条都没有）")
        return
    seen: dict[int, list[ChunkHit]] = {}
    order: list[int] = []
    for hit in hits:
        if hit.blob_id not in seen:
            seen[hit.blob_id] = []
            order.append(hit.blob_id)
        seen[hit.blob_id].append(hit)
    for blob_id in order:
        group = seen[blob_id]
        paths = store.paths_for_blob(blob_id)
        extra = f"（另有 {len(paths) - 1} 个路径指着同一份内容）" if len(paths) > 1 else ""
        console.print(f"  {escape(paths[0] if paths else '（没有路径指着它？）')}{escape(extra)}")
        # 一片就够当样例：同一个 blob 的其余切片是同一份文件里的另外几处，
        # 一条条印出来只会把终端刷满。片数用一行交代。
        console.print(f"    [dim]命中 {len(group)} 片[/dim]")
        console.print(f"    {escape(_snippet(group[0].text, tuple(chunks.tokens)))}")


def _snippet(text: str, tokens: tuple[str, ...], *, width: int = SNIPPET_CHARS) -> str:
    r"""正文里匹配的那一小段，前后各留 `width` 个字符。

    匹配位置是这样找的：**按 token 逐个试**（`tokens` 就是这一趟真正用来 MATCH 的
    那几个，拼法见 `index/tokens.py`），取第一个在原文里找得到的、位置最靠前的那个。
    找不到就退回正文开头——⚠ 那**不是**"没匹配上"，而是"匹配的位置没法用字面搜索
    复原"（FTS 的 tokenizer 与原文之间隔着大小写折叠、`unicode61` 的切分规则、
    以及中文的二元组）。所以退回开头时前面不加 `…`，第 3 行的 `…` 只表示"两侧还有
    内容"。

    这里**不做高亮**：那要一套跨终端的能力探测（`rich` 的 highlight 在重定向到日志
    文件时会变成一串 ANSI 转义码，而日志正是这一层的输出落点之一）。归 `P5` 的
    呈现层。
    """
    if not text:
        return ""
    lowered = text.lower()
    best: int | None = None
    for token in tokens:
        if not token:
            continue
        found = lowered.find(token.lower())
        if found >= 0 and (best is None or found < best):
            best = found
    if best is None:
        start = 0
    else:
        start = max(0, best - width)
    end = min(len(text), (best if best is not None else 0) + width)
    head = "…" if start > 0 else ""
    tail = "…" if end < len(text) else ""
    # 换行一律折成空格：这一行要能在一行里看完（与 `extract.batch.one_line()` 同一个
    # 理由——正文里的换行会把一条结果撑成好几行，看起来像好几条）。
    body = " ".join(text[start:end].split())
    return f"{head}{body}{tail}"
