r"""`lsearch index`（P2 / `F2-06` 起，P4 / `F4-04` 起是真的索引）。测试落点
`tests/unit/test_cli_index_dryrun.py`（`--dry-run` 那一半）与
`tests/unit/test_index_resume.py`（真索引那一半）。

两个模式，只有一件共同的事（"扫哪些根、按哪些规则"）：

* **`--dry-run`**——计划 §4.6 那一行写的事：按规则打印命中文件数。跑真实的扫描器、
  读真实的 `.lsearchignore`、走真实的 `--max-file-mb` 与 reparse 判断，只是不落库。
  它现在是**真的"只看看"**：不写索引库、不写日志文件、连数据目录都不建。
* **真索引**（缺省）——`index/build.py` 的两趟（Tier0 写文件名、Tier1 写正文），
  分阶段提交、可中断续跑。

------------------------------------------------------------------------------
只声明真能兑现的参数
------------------------------------------------------------------------------

计划 §4.6 给 `index` 列了 `--workers` / `--rebuild` / `--prune`，本次**一个都不声明**：

- `--workers` 是**嵌入**的并行（`A2`：worker 数按可用内存与单 worker 实测 RSS 算），
  Tier1 这一段是单进程顺序跑，`P5` 才有意义；
- `--rebuild` / `--prune` 都要"按路径删行"，而那条路要处理 `files_fts` 的孤儿行与
  `R14` 的"只在本次真正可达的卷内删"，归 `F6-03`。

不声明时 Click 给的是 `No such option: --workers`（退出码 2）——明确，只是没解释
"为什么没有"。理由写在 `index/build.py` 的模块文档里（那一层才是"这一版不做这件事"
的真相来源）。**偷偷忽略是最坏的一种**：`--workers 8` 给了、没反应、也不报错，
用户会以为并行跑过了（同 `backend = "mft"` 直接报错而不是静默按 scandir 跑）。

------------------------------------------------------------------------------
`--dry-run` 与真索引的日志：由**这个命令自己**决定
------------------------------------------------------------------------------

`index` 仍然挂在 `cli/main.py` 的 `LOG_FILE_EXEMPT_COMMANDS` 里，但理由变了：它不再
是"只读命令"，而是**按自己的参数决定**——`--dry-run` 不留任何落点（那是它对用户的
承诺，也是 `A7` 的落点），真索引在跑起来之前把文件 handler 补上（见
`_enable_file_log`）。

原先那张表里写着"按 `--dry-run` 决定要不要写日志本来做不到，因为 Click 在根回调之前
就把子命令的参数清空了"。那句话**只对根回调成立**：根回调确实看不到 `--dry-run`，
但**这个命令自己看得到**——决定晚一点做就行了，不必不做。到期条件因此从"P4 必须删掉
这一条"变成"这一条不再到期"（`test_cli.py` 里那条测试跟着改成了钉这件事）。

------------------------------------------------------------------------------
`--tier`：显式要 Tier2 与配置里写着 `semantic = true` 处理相反
------------------------------------------------------------------------------

`P4` 没有 Tier2（向量归 `P5`）。两处到不了那一级的路径**故意不一样**，判据是它
是不是一个人说的（与 `search/tier.py` 的 `explicit` 是同一条判据）：

* **配置里的 `semantic = true`**（这是模板的缺省值）→ **压低到 Tier1 并说出来**。
  不这么说的话默认配置直接跑不起来，而一个连默认配置都跑不动的工具等于没有。
* **命令行显式 `--tier 2`** → **当场拒绝**（退出码 2）。一个人明确要了向量那一级，
  说明他要的是"意思相近也能搜到"；默默做到 Tier1 停下，他拿到的报告看起来是
  "索引跑完了"，而 Tier2 是空的——宁可失败，不可错答。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from localsearch.cli.state import CliState
from localsearch.core.config import Config
from localsearch.core.logging import setup_logging
from localsearch.index.build import (
    BuildError,
    BuildResult,
    Tier0Stats,
    Tier1Stats,
    run,
)
from localsearch.index.sqlite_store import SqliteStore
from localsearch.index.tier import (
    TIER_FULLTEXT,
    TIER_METADATA,
    TIER_SEMANTIC,
    target_tier,
    tier_name,
)
from localsearch.ingest.base import (
    ScanConfig,
    ScanError,
    Scanner,
    ScanStats,
    make_scanner,
)
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
#: 「跑完了，但有文件没成」——`A6` 要的"不中断整轮"那一档，`P6` 的启动器把它归一成 0
#: （只有真失败才该触发重试）。见计划 §4.8「退出码归一」。
EXIT_FAILURES = 1
#: "用法不对"的惯例值（与 Click 对用法错误的取值一致，见 `cli/main.py`）
EXIT_USAGE = 2

#: `P4` 真的能跑到的最高一级（`D10` 的第三级要向量，归 `P5`）。
P4_MAX_TIER = TIER_FULLTEXT

#: 每张表最多列几行。**不是取整好看的数**：`--dry-run` 要能一眼看完，
#: 而剩下的用一行合计交代（`…另有 N 条，合计 M 条`）——全列出来等于把报表
#: 做成第二个 `files` 表，那正是 `F2-01` 当初只想"先看分布"时要避免的。
DEFAULT_LIMIT = 25

log = logging.getLogger(__name__)

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
        False, "--dry-run", help="只统计不落库：按规则打印命中文件数"
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
    tier: int | None = typer.Option(
        None,
        "--tier",
        min=0,
        max=2,
        help="只跑到这一级（0 文件名 / 1 正文 / 2 向量）；缺省跑到配置允许的最高级",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="接着上一轮跑完，**不重新扫盘**（普通 index 本来就接着上次跑）",
    ),
    max_chunks_per_doc: int | None = typer.Option(
        None,
        "--max-chunks-per-doc",
        min=1,
        help="一个文件最多切几片；缺省用配置里的值（改了它会让相关内容重切一遍）",
    ),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit", help="每张表最多列几行（只对 --dry-run 有意义）"),
) -> None:
    """索引本机文档。缺省是真索引；`--dry-run` 只统计不落库。"""
    state: CliState = ctx.obj
    _check_flags(dry_run=dry_run, resume=resume, tier=tier)

    try:
        scan_config, rules, roots, ignore_file = _prepare(
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

    if dry_run:
        report = collect(_open_scanner(scan_config), rules)
        render(
            report,
            roots=roots,
            ignore_file=ignore_file,
            rules_total=len(rules.rules),
            extra_label=rules.extra_label,
            limit=limit,
        )
        return

    code = _build(
        state,
        scan_config=scan_config,
        rules=rules,
        roots=roots,
        ignore_file=ignore_file,
        tier=tier,
        resume=resume,
        max_chunks_per_doc=max_chunks_per_doc,
    )
    if code:
        raise typer.Exit(code)


# ==============================================================================
# 参数之间说不通的地方：在跑之前拦住
# ==============================================================================


def _check_flags(*, dry_run: bool, resume: bool, tier: int | None) -> None:
    """三处自相矛盾的组合，当场报错。

    都在**读配置、开库、扫盘之前**判：这三种错误与磁盘状态无关，等到跑了半小时才
    发现"这两个参数不能一起用"是最没有必要的浪费。
    """
    if dry_run and resume:
        _refuse("--dry-run 与 --resume 说的是相反的两件事")
        _hint("--dry-run 只扫一遍给你看（不落库），--resume 反过来：不扫盘、只把库里没做完的做完。")
        raise typer.Exit(EXIT_USAGE)
    if resume and tier == TIER_METADATA:
        _refuse("--resume 与 --tier 0 说的是相反的两件事")
        _hint("--resume 跳过 Tier0（不扫盘），而 --tier 0 的意思是『只做 Tier0』。")
        raise typer.Exit(EXIT_USAGE)
    if tier == TIER_SEMANTIC:
        # 见模块文档最后一段：显式要 Tier2 与配置里写着 semantic = true 处理相反。
        _refuse("Tier2（向量语义）还没落地，要等 P5")
        _hint(
            "现在最多能到 --tier 1（正文）。Tier2 缺的是嵌入那一步，"
            "默默做到 Tier1 停下会让你以为索引跑完了。"
        )
        raise typer.Exit(EXIT_USAGE)


def _refuse(text: str) -> None:
    typer.secho(text, fg="red", err=True)


def _hint(text: str) -> None:
    typer.echo(text, err=True)


def _open_scanner(scan_config: ScanConfig) -> Scanner:
    """造扫描器；开不起来就按"用户能改的配置问题"退出码 2，**不甩栈**。

    ⚠ 这一段必须与 `_prepare` 那道 `except (ScanError, IgnoreError)` 同款，因为
    `ScanError` 有**两个**来源：`_prepare` 那一侧（根目录为空、规则文件的 roots 与
    扫描根对不上）与这里（`all_drives = true`、显式的 `backend = "mft"` 用不了）。
    `F4-04` 把 `make_scanner()` 从 `_dry_run()` 里拉出来的时候漏了后者，
    症状是 `lsearch index --all-drives` 甩一屏 traceback、退出码 1——而退出码 1 在
    计划 §4.8 里的意思是"跑完了但有文件没成"，一个把配置错误报成那种东西的命令，
    会让脚本去重试一件永远不成的事。钉它的测试是
    `test_cli_index_dryrun.py::test_all_drives_is_refused_with_the_measured_reason`。

    它还被**有意**放在 `_enable_file_log()` 与 `SqliteStore.open()` **之前**：那样
    一次被拒绝的 `index` 连数据目录都不建。建了的话，那个**空库**会让之后每一次
    `lsearch search` 都以为"索引已经建好了"（`search.py` 的模块文档记着同一件事）。
    """
    try:
        return make_scanner(scan_config)
    except ScanError as exc:
        typer.secho(f"无法开始：{exc}", fg="red", err=True)
        raise typer.Exit(EXIT_USAGE) from exc


# ==============================================================================
# 真索引
# ==============================================================================


def _build(
    state: CliState,
    *,
    scan_config: ScanConfig,
    rules: IgnoreRules,
    roots: tuple[str, ...],
    ignore_file: Path,
    tier: int | None,
    resume: bool,
    max_chunks_per_doc: int | None,
) -> int:
    """跑真索引，返回退出码（0 / 1 / 2）。

    ⚠ 这一层**不接 `BuildError` 之外的东西**：`KeyboardInterrupt` 一路冒上去
    （`main()` 把它当 130），而"打开库失败"一类真的是崩溃，该甩栈就甩栈——
    一个把库打不开报成"跑完了"的索引命令，是这一层能犯的最坏的错。
    """
    config = _effective_config(state.config, max_chunks_per_doc=max_chunks_per_doc)
    ceiling = target_tier(config.tiers)
    if ceiling < TIER_METADATA:
        # 三级全关。**拒绝**而不是"跑一趟什么都不做"：那两件事在终端上没有区别，
        # 而用户明明改过配置，他要的是"为什么没跑"，不是一句"完成"。
        _refuse("配置里 [tiers] 的三个开关全是 false：没有哪一级是要做的。")
        _hint("要么打开 metadata / fulltext，要么先改回 config/default.toml 的默认值。")
        return EXIT_USAGE

    # ⚠ 顺序：**先把"不做了"说完，再说"能做，但比你要的少一级"**。两段都是要说的话，
    # 但后一段是说明、前一段是拒绝——混着排的话，一条 `--resume` 却报"库不存在"的
    # 命令会在拒绝之前先冒一句与它无关的语义提示（实测看见过一次，读起来像
    # "配置有问题"，而真正的原因在第二行）。
    db_path = config.db_path
    if not config.scan.dedup_by_content:
        # 关掉去重这条路**没有实现**（`blobs` 的键只有 `content_hash` 一种）。
        # 报错而不是照常跑：照常跑的话用户以为去重关了，而库里其实是开着的
        # ——与 `backend = "mft"` 用不了时报错是同一条判据（见 index/build.py）。
        _refuse("scan.dedup_by_content = false 这条路 P4 没有实现")
        _hint("blobs 的键只有内容哈希一种，去重的判据与它绑定。先把它改回 true。")
        return EXIT_USAGE
    if resume and not db_path.exists():
        _refuse(f"--resume 没有可接续的东西：索引库还不存在（{db_path}）")
        _hint("第一趟请用 `lsearch index`（它会扫盘并建库）。")
        return EXIT_USAGE

    target = ceiling if tier is None else min(tier, ceiling)
    if target > P4_MAX_TIER:
        # 配置允许到 Tier2（模板的缺省就是这样）而 P4 只做到 Tier1。**说出来**：
        # 不说的话用户拿到的报告看起来是"索引跑完了"，而 Tier2 是空的。
        target = P4_MAX_TIER
        typer.secho(
            "配置里 semantic = true，但 Tier2（向量语义）要等 P5：这一趟做到 Tier1 为止。",
            fg="yellow",
            err=True,
        )
    if tier is not None and tier > ceiling:
        # 显式要的级别被配置压住了——这是"声明了却没用"的另一种形态，必须说出来。
        typer.secho(
            f"--tier {tier} 被配置压到 {target}（[tiers] 里最高的那一级是 "
            f"{tier_name(ceiling)}）",
            fg="yellow",
            err=True,
        )

    # ⚠ 在 `_enable_file_log()` 之前造：见 `_open_scanner()` 的文档（被拒绝的一趟连
    # 数据目录都不该建）。`--resume` 其实用不上它（那一趟不扫盘），仍然照造——一个
    # 用不了的 `backend` 是**配置错**，早点说出来比"在一条不扫盘的命令上静默放过、
    # 下一次真扫盘时才炸"好。
    scanner = _open_scanner(scan_config)

    _enable_file_log(state, db_path=db_path)
    log.info(
        "索引开始：roots=%s tier=%s resume=%s db=%s",
        roots,
        target,
        resume,
        db_path,
    )
    console = Console()
    try:
        with SqliteStore.open(db_path) as store:
            result = run(
                store,
                scanner=scanner,
                rules=rules,
                config=config,
                target_tier=target,
                resume=resume,
                on_progress=_progress,
            )
    except BuildError as exc:
        # `BuildError` 是"再跑下去每一步都会是错的"（见 build.py）。已经提交的批次
        # 是完好的，所以这里不用清理任何东西——**退出去、说清楚**就够了。
        _refuse(f"索引中止：{exc}")
        log.error("索引中止：%s", exc)
        return EXIT_USAGE

    render_build(
        result,
        roots=roots,
        ignore_file=ignore_file,
        rules_total=len(rules.rules),
        db_path=db_path,
        resume=resume,
        console=console,
    )
    failures = result.has_failures
    log.info(
        "索引结束：用时 %.1f s 失败文件=%s",
        result.seconds,
        (result.tier1.error if result.tier1 is not None else 0)
        + (result.tier0.scan.errors_total if result.tier0 is not None else 0),
    )
    if failures:
        # 「完成，但有文件没成」——`A6` 要的正是"整轮跑完"，所以这不是失败退出码。
        typer.echo(
            "\n有文件没成（上面的「失败」与「读不到」两栏）：这一轮**已经跑完**，"
            "大多数文件是好的。退出码 1 表示『该看一眼』，不是崩溃。",
            err=True,
        )
    return EXIT_FAILURES if failures else EXIT_OK


def _progress(stats: object) -> None:
    """每提交一批打一行进度。

    打给**人和日志**看：全量索引可能跑几小时，一条不响的命令分不出"在跑"与"卡死"。
    所以这里连"到目前为止"的耗时都不打，只打计数——几个数就够回答那件事。

    `stats` 是 `Tier0Stats` 或 `Tier1Stats`（`build.Progress` 的标注是 `object`，
    因为它两个都收）。两趟的进度**长得不一样**，所以这里按类型分。⚠ 判据是
    `Tier0Stats` 而不是 `ScanStats`：`run_tier0` 传出来的是**它自己的账**
    （里面才装着扫描器那份），拿 `ScanStats` 判会一路漏到 else 分支上去。
    """
    if isinstance(stats, Tier0Stats):
        typer.echo(f"  Tier0 已写 {stats.kept:,} 条（新 {stats.new_rows:,}）")
        return
    assert isinstance(stats, Tier1Stats)  # noqa: S101 - Progress 只有这两个实现
    typer.echo(
        f"  Tier1 已做 {stats.work:,} 个（正文 {stats.tally.fulltext:,} / "
        f"跳过 {stats.tally.skipped:,} / 失败 {stats.error:,}）· "
        f"切片 {stats.chunks_written:,}"
    )


def _effective_config(config: Config, *, max_chunks_per_doc: int | None) -> Config:
    """把命令行覆盖合进配置，得到**这一趟生效的那一份**。

    `Config` 是 frozen 的，所以是 `replace` 出一个新对象而不是改它：`Config` 在
    `CliState` 里还被别的命令共享着（同一个进程里 `search` 之类会读它），
    就地改会让"这一趟的参数"变成"这个进程的参数"。

    ⚠ 覆盖必须**在这里**合掉、往下只传这一份：`parser_ver()` 与 `split()` 要拿到
    同一个 `ChunkCfg`（`index/build.py` 的文档里写着为什么）。
    """
    if max_chunks_per_doc is None:
        return config
    return replace(config, chunk=replace(config.chunk, max_chunks_per_doc=max_chunks_per_doc))


def _enable_file_log(state: CliState, *, db_path: Path) -> None:
    r"""真索引这一趟把日志补写到 `data_dir/logs/` 下（`--dry-run` 不补）。

    为什么要有这一步：`index` 在根回调那一步被豁免了文件日志（它可能是只读的
    `--dry-run`），而根回调**看不到** `--dry-run`（Click 在调根回调之前就把子命令的
    参数清空了）。决定因此晚到这里做——`setup_logging()` 可重复调用（它先把上一次
    装上去的 handler 摘掉），所以重装一次不会让日志写两遍。

    建不起来（磁盘满、目录权限）**不报错**：日志是旁证，不该让索引跑不起来
    （`core/logging.py` 定过这条）。但那次失败的原因要打出来，否则"日志里什么都没有"
    又成了一个没有痕迹的现象。
    """
    if state.log.file_ok:
        return
    state.log = setup_logging(
        state.config.data_dir_path,
        level=state.log.level,
        to_file=True,
        to_console=True,
    )
    if not state.log.file_ok:
        typer.secho(
            f"⚠ 日志文件没建成：{state.log.note or '原因不明'}（索引照常跑）",
            fg="yellow",
            err=True,
        )
    else:
        typer.echo(f"日志：{state.log.log_file}")


# ==============================================================================
# 扫哪些根、按哪些规则（两个模式共用）
# ==============================================================================


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


def _prepare(
    config: Config,
    *,
    roots: tuple[str, ...],
    max_file_mb: int | None,
    exclude: tuple[str, ...],
    include: tuple[str, ...],
    all_drives: bool,
) -> tuple[ScanConfig, IgnoreRules, tuple[str, ...], Path]:
    """把一轮的扫描条件准备好：`(ScanConfig, 规则, 生效的根, 规则文件)`。

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
    return scan_config, rules, scan_roots, ignore_file


# ==============================================================================
# 呈现：dry-run
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


# ==============================================================================
# 呈现：真索引
# ==============================================================================
#
# 与 dry-run 那一半同样的一条：**从数据里来的字符串一律 `escape()`**（路径、扫描器
# 造的理由、用户写的规则原文）。这里额外多一类——`roots_unreachable` 里的理由。
# 静态文案（`[bold]` / `[yellow]`）是自己写的，不 escape。


def render_build(
    result: BuildResult,
    *,
    roots: tuple[str, ...],
    ignore_file: Path,
    rules_total: int,
    db_path: Path,
    resume: bool,
    console: Console | None = None,
) -> None:
    console = console or Console()
    console.print(f"扫描根        {escape('、'.join(roots))}")
    console.print(f"排除规则      {escape(str(ignore_file))}（{rules_total} 条）")
    console.print(f"索引库        {escape(str(db_path))}")

    if result.tier0 is None:
        console.print(
            f"\n[bold]Tier0（文件名/路径）[/bold]  这一趟跳过了（--resume 不重新扫盘）"
            if resume
            else "\n[bold]Tier0（文件名/路径）[/bold]  没跑"
        )
    else:
        console.print("\n[bold]Tier0（文件名/路径）[/bold]  ")
        _render_tier0(result.tier0, console)

    if result.tier1 is None:
        console.print("\n[bold]Tier1（正文）[/bold]  这一趟没做（没到那一级，或三级里没打开）")
    else:
        console.print("\n[bold]Tier1（正文）[/bold]  ")
        _render_tier1(result.tier1, console)

    console.print(f"\n总共用时      {result.seconds:.1f} s")
    if resume and result.tier1 is not None and result.tier1.work == 0:
        # 见 index/build.py 的模块文档：空库上的 --resume 什么都不做，且**不报错**。
        # 但那句话必须说出来——"跑完了什么都没做"与"索引是齐的"在终端上不该长得一样。
        console.print(
            "\n[bold]这一趟没有可接续的东西[/bold]：库里没有『还没做完正文』的行。"
            "新文件要跑一遍普通的 `lsearch index`（它会扫盘）。"
        )


def _render_tier0(stats: Tier0Stats, console: Console) -> None:
    scan: ScanStats = stats.scan
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("扫描用时", f"{scan.seconds:.2f} s")
    table.add_row(
        "条目总数",
        f"{scan.entries_seen:,}（目录 {scan.dirs_seen:,} + 文件 {scan.files_seen:,}）",
    )
    table.add_row("写进索引", f"{stats.kept:,}（新 {stats.new_rows:,} / 库里已有 {stats.known_rows:,}）")
    table.add_row("只记名字", f"{stats.name_only:,}（超 max_file_mb，正文这一轮不做）")
    # 目录**不写行**（理由见 `index/build.py` 模块文档那一节）。这一行必须印：
    # 上面"条目总数"含目录，而"写进索引"不含，差出来的数要有出处。
    table.add_row("目录", f"{stats.dirs:,}（不写行：目录名照样能通过文件的路径搜到）")
    table.add_row("被排除", f"{scan.files_skipped:,}")
    table.add_row("根目录", f"{scan.roots_scanned} / {scan.roots_total} 可达")
    for raw, why in scan.roots_unreachable:
        table.add_row("  没扫", escape(f"{raw}：{why}"))
    table.add_row("读不到的", f"{scan.errors_total:,} 条（样本最多 {len(scan.errors)}）")
    for path, why in scan.errors:
        table.add_row("  ", escape(f"{path}：{why}"))
    console.print(table)


def _render_tier1(stats: Tier1Stats, console: Console) -> None:
    tally = stats.tally
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("挑出来的行", f"{stats.work:,}（这一趟要做的文件数）")
    table.add_row("出正文", f"{tally.fulltext:,} → {stats.chunks_written:,} 片切片")
    table.add_row("这一类不做", f"{tally.skipped:,}（没有提取器认领的格式，留在 Tier0）")
    table.add_row("失败", f"{tally.error + stats.hash_errors:,}")
    if tally.unexpected:
        # 单列这一栏（`extract/batch.py` 的模块文档）：它是那三个提取器模块的 bug
        # 侦测器，混进"文件坏了"里就等于没有。
        table.add_row(
            "  其中提取器自己炸了",
            f"{tally.unexpected:,}（这是提取器的 bug，不是文件的问题）",
        )
    if stats.hash_errors:
        table.add_row("  其中读不出来", f"{stats.hash_errors:,}（算内容哈希时打不开）")
    table.add_row(
        "复用已有内容",
        f"{stats.reused:,}（同一份内容的第二个路径：既没重读，也没重切，A9）",
    )
    if stats.truncated:
        table.add_row("正文被截断", f"{stats.truncated:,}（到 max_file_mb 上限）")
    table.add_row(
        "blob",
        f"新建 {stats.blobs_created:,} / 按新 parser_ver 重切 {stats.blobs_replaced:,}",
    )
    console.print(table)
