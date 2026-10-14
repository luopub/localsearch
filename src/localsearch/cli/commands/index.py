r"""`lsearch index`（P2 / `F2-06`）。测试落点 `tests/unit/test_cli_index_dryrun.py`。

------------------------------------------------------------------------------
现在只实现了一半，而这一半必须自己说出来
------------------------------------------------------------------------------

真正的索引要写库（P4 的 `F4-01`）、要提取正文（P3）、要嵌入（P5）。**没有 `--dry-run`
时本命令直接拒绝并退出码 2**，而不是"跑一遍看看"：一个跑起来什么都不做、却打印
"索引完成"的命令，会让人以为索引建好了——这正是 `main.py` 第 3 条（不注册跑不起来的
命令）要避免的那类事，只不过这里的"跑不起来"是命令内部的半个功能。

`--dry-run` 做的是计划 §4.6 那一行写的事：**按规则打印命中文件数**。它跑真实的扫描器、
读真实的 `.lsearchignore`、走真实的 `--max-file-mb` 与 reparse 判断，只是不落库。

------------------------------------------------------------------------------
只声明 dry-run 真能兑现的那几个参数
------------------------------------------------------------------------------

计划 §4.6 还给 `index` 列了 `--include/--exclude/--workers/--tier/--rebuild/--resume/--prune`。
本次**只声明 dry-run 会用到的**（`--root` / `--max-file-mb` / `--include` / `--exclude`），
再加上 `--all-drives`（它有一个已经写好的拒绝理由，见 `E10`）。其余参数**留到 P4 与真
索引一起落地**，理由：

- 声明了却不用，用户读到的是一句谎话（`--workers 8` 给了、没反应）；
- **偷偷忽略**比报错坏得多——这条项目里已经有先例（`backend = "mft"` 直接报错而不是
  静默按 scandir 跑，见 `F2-01` 的偏差第 6 条）；
- 而不声明时 Click 给的是 `No such option: --workers`（退出码 2），同样明确，
  只是没解释"为什么没有"。等到 P4 它有了实现，那句话就不需要了。

------------------------------------------------------------------------------
dry-run 不留任何落点
------------------------------------------------------------------------------

"不落库"在这里是**字面的**：不写索引库、不写日志文件、**连数据目录都不建**（`A7`）。
所以 `index` 现在挂在 `LOG_FILE_EXEMPT_COMMANDS` 里，那条注明它为什么在里面、
以及 P4 落地真索引时**必须把它删掉**。

按 `--dry-run` 决定要不要写日志本来做不到：Click 在调用根回调**之前**就把子命令的
参数清空了（`test_cli.py` 的 `test_broken_config_blocks_every_command` 记着这件事）。
现在的形态让这件事不必回答——代价是 P4 那天要记得改一行，而那一行的症状
（"任务跑过了但日志里什么都没有"）与"根本没跑"长得一样，所以在表和测试里都写了重话。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from localsearch.cli.state import CliState
from localsearch.core.config import Config
from localsearch.ingest.base import ScanConfig, ScanError, ScanStats, make_scanner
from localsearch.ingest.filters import IgnoreError, IgnoreRules
from localsearch.ingest.report import (
    KIND_MARKER,
    KIND_OTHER,
    KIND_RULE,
    TABLE_NAME_ONLY,
    TABLE_SKIPPED,
    Distribution,
    Hits,
    Report,
    collect,
    resolve_ignore_file,
)

EXIT_OK = 0
#: "用法不对"的惯例值（与 Click 对用法错误的取值一致，见 `cli/main.py`）
EXIT_USAGE = 2

#: 每张表最多列几行。**不是取整好看的数**：`--dry-run` 要能一眼看完，
#: 而剩下的用一行合计交代（`…另有 N 条，合计 M 条`）——全列出来等于把报表
#: 做成第二个 `files` 表，那正是 `F2-01` 当初只想"先看分布"时要避免的。
DEFAULT_LIMIT = 25

_TITLES = {
    TABLE_SKIPPED: {
        KIND_RULE: "被排除掉的（按规则）",
        # 标记单独成表（`F2-05` 遗留第 8 条）：它是用户亲手放的，量级是"一个标记剪掉
        # 一整棵子树"，混在「规则之外」里就核对不了"我放的标记省了多少"。
        KIND_MARKER: "被排除掉的（目录级标记 .lsearch-noindex）",
        KIND_OTHER: "被排除掉的（规则之外的系统判断）",
    },
    TABLE_NAME_ONLY: {
        KIND_RULE: "只记名字的（留着，但不读正文，按规则）",
        KIND_MARKER: "只记名字的（目录级标记）",
        KIND_OTHER: "只记名字的（规则之外的系统判断）",
    },
}


def index(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只统计不落库：按规则打印命中文件数（现在唯一可用的模式）",
    ),
    root: list[str] = typer.Option(
        [],
        "--root",
        "-r",
        # `\[paths]`：Typer 的帮助是 rich 画的，**方括号会被当成样式标记吃掉**。
        # 第一版写的是裸的 `[paths]`，`--help` 里那半句直接不见了（2026-10-15 实测）。
        # 反斜杠只给 rich 看，Python 这边得写对（`"\("` 那种是无效转义，会告警）。
        help="扫描根，可重复；缺省用配置里 \\[paths] roots",
    ),
    max_file_mb: int | None = typer.Option(
        None, "--max-file-mb", help="超过这个大小的文件只记名字；缺省用配置里的值"
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="临时追加一条排除规则（不写回 .lsearchignore），可重复"
    ),
    include: list[str] = typer.Option(
        [], "--include", help="临时追加一条豁免规则，可重复（不用写 `!`，写了也会去掉）"
    ),
    all_drives: bool = typer.Option(
        False, "--all-drives", help="枚举所有固定盘。⚠ v1 直接报错，见 E10"
    ),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit", help="每张表最多列几行"),
) -> None:
    """索引本机文档。现在只实现了 `--dry-run`（只统计不落库）。"""
    state: CliState = ctx.obj

    if not dry_run:
        _refuse_without_dry_run(state)
        raise typer.Exit(EXIT_USAGE)

    try:
        report, rules, roots, ignore_file = _dry_run(
            state.config,
            roots=tuple(root),
            max_file_mb=max_file_mb,
            exclude=tuple(exclude),
            include=tuple(include),
            all_drives=all_drives,
        )
    except (ScanError, IgnoreError) as exc:
        # 扫描开不起来（根目录为空、all_drives、backend 没落地、规则文件的 roots
        # 与扫描根对不上）与规则文件读不了，都是**用户能改的配置问题**，
        # 不是崩溃。打一条错、退出码 2，不要甩栈。
        typer.secho(f"无法开始：{exc}", fg="red", err=True)
        raise typer.Exit(EXIT_USAGE) from exc

    render(
        report,
        roots=roots,
        ignore_file=ignore_file,
        rules_total=len(rules.rules),
        extra_label=rules.extra_label,
        limit=limit,
    )


# ==============================================================================
# 跑
# ==============================================================================


def _refuse_without_dry_run(state: CliState) -> None:
    """没有 `--dry-run` 时把话说清楚：缺的不是参数，是后面三个阶段。

    **点名缺哪几块**，而不是笼统说"还没实现"：用户拿到的信息应该是"我现在能做
    什么"，否则下一条命令仍然是猜的。
    """
    typer.secho(
        "真正的索引还没落地，现在只有 `lsearch index --dry-run` 可用。",
        fg="red",
        err=True,
    )
    typer.echo(
        "缺的三块：写库（P4 的 F4-01）、正文提取（P3）、向量嵌入（P5）。\n"
        "现在跑真索引只会建出一份空库，而它在终端上看起来和「索引完成」没有区别。\n"
        "先看规则会怎么判：`lsearch index --dry-run`。",
        err=True,
    )
    typer.echo(f"（本次生效的配置：{state.config_file or '代码内置默认值'}）", err=True)


def _strip_bang(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """去掉 `--include` / `--exclude` 里手写的前导 `!`。

    `with_extra` 收的是**模式本身**：`!` 是在读 `.lsearchignore` 那一层被剥掉的
    （`filters.parse` 里的 `negated`）。用户很可能照文件的写法敲 `--include "!D:/keep"`，
    不剥的话模式会带着一个 `!` 去匹配，结果是**一条都不命中、且不报错**——与
    `filters.py` 里 `_literal_ends` 取错后缀属于同一类静默失败，也正是这个项目
    反复吃亏的那一类。

    `--include` 本来就只有"豁免"一个意思，剥掉没有第二种读法；`--exclude` 上的 `!`
    则只可能是从文件里抄过来的，按同一规则处理比让它静默失效有用。
    """
    return tuple(raw.lstrip("!") for raw in patterns)


def _dry_run(
    config: Config,
    *,
    roots: tuple[str, ...],
    max_file_mb: int | None,
    exclude: tuple[str, ...],
    include: tuple[str, ...],
    all_drives: bool,
) -> tuple[Report, IgnoreRules, tuple[str, ...], Path]:
    """把一轮 dry-run 跑完，交出账目。

    两个 roots 必须**来自同一个元组**：`IgnoreRules` 靠 roots 解释 `/rel/path` 这类
    相对规则，而扫描器会核对两边一致（`check_rules_roots`）。命令行给了 `--root` 就
    整体替换配置里的值——"追一个根"这个语义（把两者合并）在这里更难解释，且没有
    使用场景。
    """
    scan_roots = roots or tuple(config.paths.roots)
    ignore_file = resolve_ignore_file(config.scan.ignore_file)
    rules = IgnoreRules.from_file(ignore_file, roots=scan_roots).with_extra(
        excludes=_strip_bang(exclude), includes=_strip_bang(include)
    )

    scan_config = ScanConfig(
        roots=scan_roots,
        data_dir=config.paths.data_dir,
        max_file_mb=config.scan.max_file_mb if max_file_mb is None else max_file_mb,
        backend=config.scan.backend,
        follow_symlinks=config.scan.follow_symlinks,
        # 命令行的 `--all-drives` 与配置里的值**取或**：两边都是"打开"，
        # 没有"关掉配置里的 true"这种用法——关掉应该去改配置。
        all_drives=all_drives or config.paths.all_drives,
    )
    return collect(make_scanner(scan_config), rules), rules, scan_roots, ignore_file


# ==============================================================================
# 呈现
# ==============================================================================
#
# **每一段动态文本都要过 `escape()`**：rich 把方括号当样式标记，**认不出就整段吃掉**，
# 而且不报错。这不是理论风险，本项目已经撞了三次：
#
#   1. `--root` 帮助文本里裸写的 `[paths]`（`--help` 里那半句直接不见）；
#   2. 目录名里带方括号的路径（`node_modules/[foo]` 这种在真实磁盘上很常见）；
#   3. 扫描器自己造的理由 `超过 [scan] max_file_mb = 64`，在终端上印成
#      `超过  max_file_mb = 64`（2026-10-15 实测，见 `docs/reports/F2-06-*.md`）。
#
# 前两次是"少半句话"，第三次是"理由看起来像另一个东西"。静态文案不 escape（本文件的
# 那几个 `[bold]` 是自己写的），**凡是从数据里来的字符串一律 escape**——包括路径、
# 操作系统错误消息、用户写的规则原文（glob 里 `[ch]` 这种字符组是常见写法）。
# `tools/scan_report.py` 走 `print()`，不涉及这件事。


def _mib(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def render(
    report: Report,
    *,
    roots: tuple[str, ...],
    ignore_file: Path,
    rules_total: int,
    extra_label: str,
    limit: int,
    console: Console | None = None,
) -> None:
    console = console or Console()
    console.print(f"扫描根        {escape('、'.join(roots))}")
    label = f"（另有命令行临时规则：{escape(extra_label)}）" if extra_label else ""
    console.print(f"排除规则      {escape(str(ignore_file))}（{rules_total} 条）{label}")
    console.print(f"账目用时      {report.seconds:.2f} s（含回调，含 IO 等待）")
    _render_stats(report.stats, console)
    _render_distribution(report.dist, console, limit=limit)
    _render_unhit_rules(report.dist, console, limit=limit)
    console.print(
        "dry-run 不写索引库；日志落在 data_dir/logs/ 下。"
        "「目录」与「文件」分开计：剪掉一个目录会带走整棵子树。"
    )


def _render_stats(stats: ScanStats, console: Console) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("条目总数", f"{stats.entries_seen:,}（目录 {stats.dirs_seen:,} + 文件 {stats.files_seen:,}）")
    table.add_row(
        "保留的目录",
        f"{stats.dirs_kept:,}（剪掉 {stats.dirs_pruned:,} / reparse {stats.dirs_reparse:,}）",
    )
    table.add_row("正文", f"{stats.files_index:,}（{_mib(stats.bytes_index)}）")
    table.add_row("只记名字", f"{stats.files_name_only:,}（超限 {stats.files_over_limit:,} 已计入）")
    table.add_row("排除", f"{stats.files_skipped:,}")
    table.add_row("根目录", f"{stats.roots_scanned} / {stats.roots_total} 可达")
    # 「没扫的根」而不是「不可达」：被目录级标记排除的根也进这个列表（标记可以放在
    # 扫描根上），把那种情形印成"不可达"是在报表里说错话。F2-05 定下的措辞。
    for raw, why in stats.roots_unreachable:
        table.add_row("  没扫", escape(f"{raw}：{why}"))
    table.add_row("读不到的", f"{stats.errors_total:,} 条（样本最多 {len(stats.errors)}）")
    for path, why in stats.errors:
        table.add_row("  ", escape(f"{path}：{why}"))
    if stats.reparse_tags:
        tags = "  ".join(f"0x{tag:X}×{n}" for tag, n in sorted(stats.reparse_tags.items()))
        table.add_row("reparse tag", tags)
    console.print(table)


def _render_distribution(dist: Distribution, console: Console, *, limit: int) -> None:
    """两张表分开印，表内再按栏分段——而不是一张大表靠 kind 那一列区分。

    `F2-05` 的遗留第 8 条要的就是这件事：标记单独成行。分段比"靠一列区分"更彻底，
    用户核对"我放的标记省了多少"时不必在几百行里找。
    """
    for which, empty_title in (
        (TABLE_SKIPPED, "被排除掉的"),
        (TABLE_NAME_ONLY, "只记名字的"),
    ):
        rows = dist.rows(which)
        if not rows:
            console.print(f"\n{empty_title}：（没有这样的条目）")
            continue
        for kind in (KIND_RULE, KIND_MARKER, KIND_OTHER):
            picked = [(k, h) for k, h in rows if k[0] == kind]
            if not picked:
                continue
            console.print(f"\n{_TITLES[which][kind]}")
            _print_rows(picked, console, limit=limit)


def _print_rows(
    rows: list[tuple[tuple[str, str, str], Hits]], console: Console, *, limit: int
) -> None:
    table = Table(show_lines=False, pad_edge=False)
    table.add_column("目录", justify="right", no_wrap=True)
    table.add_column("文件", justify="right", no_wrap=True)
    table.add_column("大小", justify="right", no_wrap=True)
    table.add_column("归属", no_wrap=True)
    table.add_column("规则", overflow="fold")
    table.add_column("例", overflow="fold")
    for (_kind, where, raw), hits in rows[:limit]:
        table.add_row(
            f"{hits.dirs:,}",
            f"{hits.files:,}",
            _mib(hits.bytes),
            escape(where),
            escape(raw),
            escape(hits.example),
        )
    console.print(table)
    if len(rows) > limit:
        rest = sum(h.total for _, h in rows[limit:])
        console.print(f"  …另有 {len(rows) - limit} 条，合计 {rest:,} 条")


def _render_unhit_rules(dist: Distribution, console: Console, *, limit: int) -> None:
    """一条都没管到的规则。

    ⚠ **两种成因分不开**，所以文案里必须并列说出来：被后面的规则完全盖掉（`F2-03`
    的 `Thumbs.db`）与从来没匹配上任何东西。把这一栏读成"被盖掉的规则"会得到错误的
    结论——`*.log` 在只含 `.txt` 的目录树上也是"零命中"。
    """
    rules = dist.rules_with_no_hits()
    total = len(dist.rules)
    # 强调用 rich 的 `[bold]`，**不是 markdown 的 `**`**：`Console.print` 不认后者，
    # 那几个星号会原样印到终端上（2026-10-15 实测）。同时也就不能出现裸的方括号
    # ——`doctor.py` 里 `escape()` 要处理的是另一个方向的问题。
    console.print(
        f"\n一条都没管到的规则  {len(rules)} / {total} 条"
        "（[bold]被后面的规则完全盖掉[/bold] 与 [bold]压根没匹配上[/bold]"
        " 两种成因在这里分不开）"
    )
    for rule in rules[:limit]:
        # `origin` 里带规则原文，而 glob 里 `[ch]`、`[!x]` 是常见写法（见上面的段首注）。
        console.print(f"  {escape(rule.origin)}")
    if len(rules) > limit:
        console.print(f"  …另有 {len(rules) - limit} 条")
