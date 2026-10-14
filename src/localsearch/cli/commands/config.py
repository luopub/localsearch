r"""`lsearch config init|show|path`（P1 / `F1-04`）。测试落点 `tests/unit/test_cli.py`。

三件事各自的落点不同，混起来会让人搞不清"我改的到底是哪份配置"：

| 命令 | 作用 | 碰到文件吗 |
|---|---|---|
| `init` | 把模板复制成 `config/config.toml`（本机配置，不入库） | 写 `config/config.toml` |
| `show` | 打印**生效**的配置值与它来自哪个文件 | 只读 |
| `path` | 打印各路径的实际指向，用来回答"东西都放哪儿了" | 只读 |

⚠ `init` 会写进项目目录。这是设计（计划 §7.2 的小决策 a：配置文件放项目内
`config/config.toml`，而不是 `%APPDATA%`），所以它**必须**先确认目标不存在，
除非显式 `--force`。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import typer

from localsearch.cli.state import CliState
from localsearch.core import config as config_mod
from localsearch.core.config import Config, config_source_kind
from localsearch.core.logging import LOG_FILE_NAME

config_app = typer.Typer(
    help="配置管理：查看生效配置、定位文件、生成本机配置",
    no_args_is_help=True,
)


def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return repr(value)


def dump_toml(cfg: Config) -> str:
    """把配置渲染成 TOML 文本，好让人能整段拷进 `config/config.toml`。

    只有一层小节，所以没有引第三方 TOML 写库——多一个依赖只为了这 20 行不划算。
    真出现嵌套结构时再来换。
    """
    lines: list[str] = []
    for section in dataclasses.fields(cfg):
        lines.append(f"[{section.name}]")
        values = getattr(cfg, section.name)
        for key in dataclasses.fields(values):
            value = getattr(values, key.name)
            if isinstance(value, (list, tuple)):
                rendered = "[" + ", ".join(_fmt_scalar(v) for v in value) + "]"
            else:
                rendered = _fmt_scalar(value)
            lines.append(f"{key.name} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@config_app.command("init")
def init(
    force: bool = typer.Option(
        False, "--force", help="覆盖已存在的 config/config.toml"
    ),
) -> None:
    """把入库模板复制成本机配置。（本机配置不入库。）"""
    target = config_mod.LOCAL_CONFIG_PATH
    template = config_mod.TEMPLATE_PATH

    if target.exists() and not force:
        typer.secho(
            f"{target} 已存在，未改动。要覆盖请显式加 --force。",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(1)
    if not template.is_file():
        typer.secho(
            f"找不到模板 {template}：它应当随仓库一起存在。"
            "（若你是从 wheel 安装的，说明打包时漏带了 config/default.toml。）",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    typer.echo(f"已生成 {target}")
    typer.echo("它是本机配置、不入库；改它不影响模板 config/default.toml。")
    typer.echo("只改其中一个键也可以——其余键会取模板里的值。")


@config_app.command("show")
def show(ctx: typer.Context) -> None:
    """打印当前**生效**的配置，并说明它来自哪个文件。"""
    state: CliState = ctx.obj
    typer.echo(f"# 生效配置：{config_source_kind(state.config_file)}")
    if state.config_file is not None:
        typer.echo(f"# 文件：{state.config_file}")
    typer.echo("")
    typer.echo(dump_toml(state.config), nl=False)


@config_app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """打印各路径的实际指向。"""
    state: CliState = ctx.obj
    rows = [
        ("项目根", config_mod.PROJECT_ROOT),
        ("模板", config_mod.TEMPLATE_PATH),
        ("本机配置", config_mod.LOCAL_CONFIG_PATH),
        (
            "生效文件",
            state.config_file if state.config_file is not None else "（无，用内置默认值）",
        ),
        ("data_dir", state.config.data_dir_path),
        ("日志目录", state.config.log_dir_path()),
        ("日志文件", state.config.log_dir_path() / LOG_FILE_NAME),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")
    # 上面这个"日志文件"是**将会**写在哪儿，不是"已经写了"：config 与 doctor 是
    # 只读诊断命令，不建数据目录也不写日志（见 cli/main.py 的 LOG_FILE_EXEMPT_COMMANDS）。
    # 不说这一句，用户会以为文件已经在那儿了。
    typer.echo("（config / doctor 是只读命令，不建数据目录、不写日志文件；上表最后一行是它们将会落在哪。）")
