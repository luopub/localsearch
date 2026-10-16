r"""`lsearch` 命令行入口（P1 / `F1-04`）。测试落点 `tests/unit/test_cli.py`。

本文件只做**装配**：建应用、挂子命令、解析全局选项、在子命令真正跑起来之前把
环境变量与日志准备好。每个命令自己的逻辑在 `cli/commands/` 下（计划 §4.3）。

四条约定，每条都是为了避免一种具体的坏结果：

**1. 全局选项集中在回调里，子命令通过 `ctx.obj` 拿配置。**
   于是"配置来自哪个文件""日志写哪儿"只解析一次。让每个子命令各读一次配置，
   迟早出现"上游按 A 文件跑、下游按 B 文件跑"这种要挖很久的分叉。

**2. `bootstrap_env()` 在回调里、任何子命令之前调用。**
   计划 §3.3 要求它发生在"任何 torch / transformers 导入之前"。P1 还没有一处
   导入 torch，所以这条现在看不出作用——它的意义是把**位置**固定下来：
   等 P2 往子命令里塞模型代码时，不需要谁再想起来补一句。
   注意 `bootstrap_env` 是 `setdefault` 语义，本机已有的 `HF_HOME` 一字不动。

**3. 只注册真的能跑的命令。**
   `status` / `explain` / `bench` / `reindex` 属于后面的阶段，**现在不挂上去**。
   挂一个能列出、一跑就崩的命令，用户读到的是"这命令有 bug"，而不是"这功能还没做"——
   前者会让人去查一个不存在的问题。

   `index` 曾经是个**半分**的情形（`F2-06`：那时只有 `--dry-run` 是真的，命令靠
   "自己会拒绝"换来注册），`F4-04` 之后它不再半分：两趟都跑得起来，`--dry-run` 与
   真索引两条路都有自己的测试。`search` 同样在 `F4-04` 落地（最小的那一版，只有词法；
   语义那一半到不了时**显式标注**「语义未就绪」，见 `commands/search.py`）。

   判定标准始终不是"这条命令有没有完全实现"，而是**"它会不会给出误导的结果"**：
   `search --mode semantic` 现在仍然做不到，但它**拒绝**而不是悄悄按词法跑。
   两张名单（`test_cli.py` 里那份 `PLANNED_COMMANDS` 与 `NOT_YET_IMPLEMENTED`）
   必须恰好拼成计划 §4.6 的命令面——这条不变量由
   `test_the_two_lists_partition_the_planned_command_surface` 钉住。

**4. 无参数运行只打印帮助。**
   交互式 REPL 是 P5 的 `F5-07`（D7 定的"一次性命令 + REPL 双形态"）。
   现在假装自己是 REPL，比干脆不做更难查。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from localsearch import __version__
from localsearch.cli.commands.config import config_app
from localsearch.cli.commands.doctor import doctor
from localsearch.cli.commands.index import index
from localsearch.cli.commands.search import search
from localsearch.cli.state import CliState
from localsearch.core.console import use_safe_output
from localsearch.core.config import (
    Config,
    ConfigError,
    bootstrap_env,
    load_config,
    resolve_config_file,
)
from localsearch.core.logging import setup_logging

#: 这些命令**不写日志文件**（也不建数据目录）。**值是"为什么"**，不是装饰：
#: 这张表里每多一条都要能被复核，而这张表最容易被顺手改错。
#:
#: 理由是 A7 那条"不产生计划外持久化落点"：只读命令不该为了回答一个问题就在数据
#: 目录里留下目录和日志文件——那会让"检查过"这件事本身改变被检查的对象。
#: 真要留下它们的输出，重定向终端即可。
#:
#: ⚠ **`index` 留在这里，但理由变了**（`F4-04`）：它不再是"只读命令"，而是**按自己
#: 的参数决定**——`--dry-run` 不留任何落点（那是它对用户的承诺），真索引在跑起来之前
#: 由**命令自己**把文件 handler 补上（`commands/index.py` 的 `_enable_file_log()`）。
#:
#: 原先这里写着"按 `--dry-run` 决定要不要写日志本来也做不到，因为 Click 在调用根回调
#: **之前**就把子命令的参数清空了（`test_cli.py` 的
#: `test_broken_config_blocks_every_command` 记着这件事）"。那句话**只对根回调成立**：
#: 根回调确实看不到 `--dry-run`，而这个命令自己看得到——把决定晚一点做就行了，
#: 不必不做。所以这一条的到期条件从"P4 必须删掉"变成"这一条不到期"。
#:
#: `search` 在这里是因为它**确实是只读的**：它连日志文件都不需要（它的输出就是
#: 答案本身），而且它要能在数据目录还不存在时给出那句"还没有索引库"。
LOG_FILE_EXEMPT_COMMANDS = {
    "config": "只读的配置管理（show / path / init 都不需要索引库）",
    "doctor": "只读的环境自检，且要在数据目录还不存在时也能跑",
    "index": "按参数决定：--dry-run 不留落点，真索引由命令自己补上文件日志（F4-04）",
    "search": "只读检索：输出就是答案，且要在数据目录还不存在时给出提示",
}

#: 用了但没有给出子命令时的退出码。2 是"用法不对"的惯例值（与 Click 对用法错误的取值一致）。
USAGE_EXIT_CODE = 2

app = typer.Typer(
    name="lsearch",
    help=(
        "本地离线全盘全文搜索。\n\n"
        "现在能用的：`config`（配置管理）、`doctor`（环境自检）、"
        "`index`（建索引，`--dry-run` 只统计不落库）、"
        "`search`（检索，现在只有词法——语义那一半要等 P5，会显式标注「语义未就绪」）。"
        "`status` / `explain` / `bench` / `reindex` 在后面的阶段落地。"
    ),
    # 不能用 no_args_is_help：Click 在调用回调**之前**就检查"有没有子命令"，
    # 于是 `lsearch --version`（只有全局选项、没有子命令）会先被判为 Missing command，
    # 回调里的 `--version` 永远执行不到。改成让回调自己处理"没有子命令"这一情形。
    invoke_without_command=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config")
# 单个命令直接挂，不套一层子应用：套了就会变成 `lsearch doctor run`，
# 而计划 §4.6 的命令面写的是 `lsearch doctor`。
app.command("doctor")(doctor)
app.command("index")(index)
app.command("search")(search)


@app.callback()
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", help="打印版本号后退出", is_eager=True
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="显式指定配置文件（优先级高于环境变量 LSEARCH_CONFIG 与 config/config.toml）",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL"
    ),
    no_log_file: bool = typer.Option(
        False, "--no-log-file", help="不写日志文件，日志只输出到终端"
    ),
) -> None:
    """解析全局选项、准备环境，然后把控制权交给子命令。"""
    if version:
        typer.echo(f"lsearch {__version__}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        # 没有子命令：只打印帮助。这里**不做任何别的事**——不读配置、不建数据目录、
        # 不装日志，所以裸跑一条 `lsearch` 不会留下任何落点（A7）。
        # 交互式 REPL 是 P5 的 F5-07；在那之前，假装自己是 REPL 只会更难查。
        typer.echo(ctx.get_help())
        raise typer.Exit(USAGE_EXIT_CODE)

    try:
        config, config_file = _load(config_path)
    except ConfigError as exc:
        # 配置坏了就**立刻停**，不要带着半份配置继续跑：读不到的配置与写错的键
        # 长得一样（都会静默用默认值），跑下去只会得到一堆看着正常的结果。
        typer.secho(f"配置有误：{exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    # 快照必须在 bootstrap_env **之前**拍：它之后我们就分不清哪个值是系统给的、
    # 哪个是自己刚填的兜底值了（见 cli/state.py 的 env_at_start）
    env_at_start = dict(os.environ)

    # 见文件头第 2 条：必须在任何 torch / transformers 导入之前
    bootstrap_env(config.data_dir_path)

    subcommand = ctx.invoked_subcommand
    write_log_file = not no_log_file and subcommand not in LOG_FILE_EXEMPT_COMMANDS
    try:
        log = setup_logging(
            config.data_dir_path,
            level=log_level,
            to_file=write_log_file,
            to_console=True,
        )
    except ValueError as exc:
        # resolve_level 认不出级别。静默按 INFO 处理会让 `--log-level=verbose`
        # 看起来生效了、其实什么都没改。
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc

    ctx.obj = CliState(
        config=config, config_file=config_file, log=log, env_at_start=env_at_start
    )


def _load(config_path: Path | None) -> tuple[Config, Path | None]:
    """解析配置文件，同时给出**它是哪一份**。

    先 `resolve_config_file()` 再把结果喂给 `load_config()`，是为了只走一遍优先级链：
    `load_config(None)` 会自己再解析一次，那条链跑两遍就可能给出两个答案
    （比如这中间有人建了 `config/config.toml`）。"报告配置来自哪里"和"实际用的是哪份"
    必须来自同一次解析。
    """
    source = resolve_config_file(config_path)
    return load_config(source), source


def main(argv: list[str] | None = None) -> int:
    """入口点的目标函数，返回进程退出码。

    **刻意**让 Typer 走它自己的 standalone 模式（默认），只在最外层把 `SystemExit`
    转成返回值。理由：Typer 自带一份 click（`typer._click`），它的异常类型与外面
    `import click` 拿到的**不是同一个类**——自己写 `except click.ClickException` 会
    一个都接不住。本文件第一版正是这么写的，症状是 `lsearch index` 甩一屏 traceback、
    退出码 1。帮助怎么打印、用法错误为何是 2、Ctrl-C 为何是 130，都归 Typer 自己管。

    **第一行就把输出面设成"编不了就降级"**（`core/console.py`）：`lsearch <子命令>
    --help` 打印文档串里的 `⚠` 发生在 Typer 回调**之前**，所以这件事挂在回调里来不
    及；而重定向到文件时 (`lsearch index --help > x.txt`) Python 用 ANSI 代码页，
    那一行会当场 `UnicodeEncodeError`、退出码 1（2026-10-16 实测，见 `E37`）。
    """
    use_safe_output()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        app(args=args)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
