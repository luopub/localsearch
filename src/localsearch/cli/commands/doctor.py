r"""`lsearch doctor`（P1 / `F1-05`）。测试落点 `tests/unit/test_doctor.py`。

回答一个问题：**这台机器现在能不能用**。验收项 A7 要求它报告 `HF_HOME` /
`data_dir` / `TORCH_HOME` / 模型位置，并且**不产生计划外持久化落点**。

三条设计约束：

**1. 绝不写任何东西。** 不建数据目录、不写日志、不下载模型（`HF_HUB_OFFLINE=1` 也
   决定了它不该联网）。医生的检查不该改变被检查的对象——否则"跑一次 doctor 就好了"
   这类问题会永远查不出来。最后一项「落点审计」就是在运行时自证这一点：检查前后各
   拍一次数据目录的快照，不一致就报 FAIL。

**2. 只做便宜的检查。** 不 `import torch`（几秒起步），不加载模型（那是 P2 的事，
   而且 14GB 权重要读盘）。依赖是否齐全改从**已安装分发的元数据**里读——
   元数据里没有的包，`import` 也必然失败，而读元数据是毫秒级。
   模型只按**文件存在性**判断，真正能不能加载由 P2 落实。

**3. 三种状态，含义写死。** `OK` / `WARN` / `FAIL`；**只有 FAIL 影响退出码**。
   `WARN` 是"现在能用，但你知道一下"（比如模型还没下），`FAIL` 是"这样跑不了"
   （比如 Python 版本不对、依赖缺了）。把两者混起来，用户就会开始忽略警告。
"""

from __future__ import annotations

import ctypes
import importlib.metadata as md
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from localsearch.cli.state import CliState
from localsearch.core.config import Config, config_source_kind, effective_env

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

#: 只有 FAIL 影响退出码（见文件头第 3 条）
EXIT_OK = 0
EXIT_PROBLEM = 1

#: 数据目录所在卷低于这个剩余空间就提示一句。索引体积还没实测过（P2 才知道），
#: 所以这只是"早一点提醒"，不是"低于它就跑不了"。
LOW_FREE_SPACE_BYTES = 5 * 1024**3


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


# ==============================================================================
# 各项检查
# ==============================================================================


def check_interpreter() -> Check:
    """解释器版本与"是不是在 venv 里"。

    本机同时存在 3.11.9（裸 `python`）、3.13.5（`py`）与 3.12.12（uv 管理的 venv），
    建环境时用错**不会报错**，只会在后面某处诡异地失败。所以值得占一行。
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    in_venv = sys.prefix != sys.base_prefix
    where = "venv" if in_venv else "系统解释器"

    if sys.version_info[:2] < (3, 12):
        return Check(
            "Python 解释器",
            FAIL,
            f"{version}（{where}，{sys.executable}）低于要求的 3.12",
            "计划要求 3.12。本机裸 python 是 3.11.9、py 是 3.13.5，"
            "用 uv 建的 .venv 才是 3.12.12",
        )
    if sys.version_info[:2] > (3, 12):
        return Check(
            "Python 解释器",
            WARN,
            f"{version}（{where}）比锁定的 3.12 新，依赖尚未在这个版本上验证过",
            "依赖是按 3.12 锁的（uv.lock）；换版本前先 uv sync 并重跑测试",
        )
    return Check("Python 解释器", OK, f"{version}（{where}，{sys.executable}）")


def check_dependencies() -> Check:
    """依赖是否齐。

    名单从**已安装分发的元数据**里读（即 pyproject 的 `dependencies`），不在这里
    再抄一份——抄一份就会漂移，而漂移的症状是"测试说有这个包、运行时没有"。
    """
    try:
        requires = md.distribution("localsearch").requires or []
    except md.PackageNotFoundError:  # pragma: no cover - 装了包才会有这个命令
        return Check("依赖", FAIL, "读不到 localsearch 自己的元数据", "重新 uv sync")

    names = [req.split(";", 1)[0].split("[", 1)[0].strip() for req in requires]
    names = [n for n in names if n]
    missing = []
    for name in names:
        try:
            md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)

    if missing:
        return Check(
            "依赖",
            FAIL,
            f"{len(names) - len(missing)}/{len(names)} 已安装，缺：{', '.join(missing)}",
            "在项目根跑 `uv sync`",
        )
    return Check("依赖", OK, f"{len(names)}/{len(names)} 已安装")


def check_config(state: CliState) -> Check:
    source = "内置默认值" if state.config_file is None else str(state.config_file)
    return Check("配置", OK, f"{source}（{config_source_kind(state.config_file)}）")


def _existing_ancestor(path: Path) -> Path | None:
    """向上找到第一个存在的**目录**。`data_dir` 在首次索引前是不存在的，这不是错误。"""
    for candidate in [path, *path.parents]:
        if candidate.is_dir():
            return candidate
    return None


def _blocking_file(path: Path) -> Path | None:
    """路径上已有的**文件**。文件挡住的路，目录永远建不起来。

    这里必须单独判：`os.access()` 对一个文件是**可写**的（普通文件当然可写），
    所以只查可写性的话，`C:/x/file/data` 这种配置会一路报 OK，而首次索引时才炸。
    """
    for candidate in path.parents:
        if candidate.is_dir():
            return None
        if candidate.exists():
            return candidate
    return None


def drive_type(path: Path) -> str:
    """Windows 报的卷类型。**别当成"本地性"的可靠判据。**

    本机实测（`PROGRESS.md` §3 的 E10）：`GetDriveType` 把 E: 报成 DRIVE_FIXED(3)、
    文件系统 NTFS，与 C:/D: 逐项相同，而 E: 并不是本地盘。所以这里只把
    可移动 / 网络 / 光驱 / RAM 盘 判为可疑，DRIVE_FIXED 一律当"说不好"。
    """
    if os.name != "nt":  # pragma: no cover - 本机是 Windows
        return "非 Windows，未判"
    root = Path(path).drive
    if not root:
        return "无盘符"
    kinds = {
        0: "未知",
        1: "无根目录",
        2: "可移动",
        3: "固定盘（Windows 的说法，不等于本地）",
        4: "网络",
        5: "光驱",
        6: "RAM 盘",
    }
    try:
        value = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root + "\\"))
    except (OSError, AttributeError):  # pragma: no cover - 非 Windows 或调用失败
        return "查询失败"
    return kinds.get(value, f"其他（{value}）")


SUSPECT_DRIVE_KINDS = ("可移动", "网络", "光驱", "RAM 盘", "无根目录", "未知")


def check_data_dir(config: Config) -> Check:
    data_dir = config.data_dir_path
    if data_dir.exists() and not data_dir.is_dir():
        return Check(
            "data_dir",
            FAIL,
            f"{data_dir} 是个文件，不是目录",
            "索引数据要写进这个目录；把它移走，或把 [paths] data_dir 改到别处",
        )
    if data_dir.exists():
        writable = os.access(data_dir, os.W_OK)
        status = OK if writable else FAIL
        detail = f"{data_dir}（已存在，{'可写' if writable else '不可写'}）"
        hint = "" if writable else "检查目录权限，或把 [paths] data_dir 改到别处"
    else:
        blocker = _blocking_file(data_dir)
        if blocker is not None:
            return Check(
                "data_dir",
                FAIL,
                f"{data_dir} 建不起来：{blocker} 是个文件，挡住了这条路径",
                "把这个文件移走，或把 [paths] data_dir 改到别处",
            )
        ancestor = _existing_ancestor(data_dir)
        if ancestor is None:  # pragma: no cover - 盘不存在时才会走到
            return Check("data_dir", FAIL, f"{data_dir} 不存在，且上级目录也不存在")
        writable = os.access(ancestor, os.W_OK)
        status = OK if writable else FAIL
        detail = (
            f"{data_dir}（尚未创建，首次索引时创建；"
            f"上级 {ancestor} {'可写' if writable else '不可写'}）"
        )
        hint = "" if writable else f"{ancestor} 不可写，索引建不起来"

    if status is OK:
        target = data_dir if data_dir.exists() else _existing_ancestor(data_dir)
        try:
            free = shutil.disk_usage(target).free  # type: ignore[arg-type]
        except OSError:  # pragma: no cover
            return Check("data_dir", status, detail, hint)
        gib = free / 1024**3
        detail += f"，所在卷剩余 {gib:.1f} GiB"
        if free < LOW_FREE_SPACE_BYTES:
            return Check(
                "data_dir",
                WARN,
                detail,
                "剩余空间偏低；索引体积还没实测过（P2），先留意",
            )
    return Check("data_dir", status, detail, hint)


def check_roots(config: Config) -> Check:
    """索引根目录：**存在性只在这里判**。

    `core/config.py` 刻意不判存不存在——盘可能没插、网络盘可能没挂载，那是运行时
    事实而非配置错误。所以"报哪些根不可达"是 doctor 的活。
    """
    if config.paths.all_drives:
        # WARN → FAIL（2026-10-15 随 F2-01 改）：v1 的扫描器**拒绝** all_drives = true，
        # 所以这不是"现在能用、你知道一下"，而是"这样跑不了"——正是 FAIL 的定义。
        return Check(
            "索引根目录",
            FAIL,
            "all_drives = true：v1 不支持，扫描器会直接报错",
            "本机 GetDriveType 判不出本地性（E10：E: 报 DRIVE_FIXED 却不是本地盘），"
            "枚举「所有固定盘」会把会掉线的卷当一直在的卷建全量索引。"
            "请在 [paths] roots 里显式写明要索引的目录",
        )

    lines: list[str] = []
    status = OK
    missing: list[str] = []
    suspect: list[str] = []
    for root in config.paths.roots:
        path = Path(root)
        exists = path.exists()
        kind = drive_type(path)
        kind_note = f"，{kind}" if "固定盘（" not in kind else ""
        lines.append(f"{root} —— {'存在' if exists else '不存在或未挂载'}{kind_note}")
        if not exists:
            status = WARN
            missing.append(root)
        elif kind.startswith(SUSPECT_DRIVE_KINDS):
            status = WARN
            suspect.append(root)

    hint = ""
    if missing:
        hint = "不可达的根不会被索引（也不会被 --prune 清掉它的旧记录，见计划 §4.6）"
    elif suspect:
        hint = "这些根不是本地固定盘；本机实测 GetDriveType 会把非本地的 E: 也报成固定盘"
    return Check("索引根目录", status, "\n".join(lines), hint)


def check_env(config: Config, environ: Mapping[str, str]) -> Check:
    """A7 的核心：报告实际生效的环境变量与**它的来源**。

    只读（`effective_env`），不写 `os.environ`。

    `environ` **不是**当前的 `os.environ`，而是进程启动时的快照：根回调在 doctor 之前
    已经通过 `bootstrap_env` 往 `os.environ` 里写过了兜底值，拿它来判来源会把
    "我们自己刚填的"报成"系统已设"。本机第一次跑 doctor 时就是这么错的——
    系统里根本没有 `TORCH_HOME`，表格却写着"← 系统已设"。来源栏在这些兜底项上撒谎，
    恰恰让最需要看它的地方失去意义。
    """
    lines = [
        f"{var.name} = {var.value}   ← {var.source}"
        for var in effective_env(config.data_dir_path, environ=environ)
    ]
    return Check("环境变量", OK, "\n".join(lines))


def _hf_hub_dir(hf_home: str, model_id: str) -> Path:
    return Path(hf_home) / "hub" / ("models--" + model_id.replace("/", "--"))


def check_model(config: Config, environ: Mapping[str, str]) -> Check:
    """模型位置（A7）。**只看文件在不在**，不加载。

    真正"能不能推理"由 P2 的 `F5-01` 落实；现在去 import torch 加载 14GB 权重，
    会让一条自检命令跑几十秒——那正是没人愿意跑自检的开始。
    """
    hf_home = next(
        var.value
        for var in effective_env(config.data_dir_path, environ=environ)
        if var.name == "HF_HOME"
    )
    model_id = config.embed.model_id
    target = _hf_hub_dir(hf_home, model_id)
    if target.is_dir():
        snapshots = target / "snapshots"
        count = len(list(snapshots.iterdir())) if snapshots.is_dir() else 0
        return Check("模型权重", OK, f"{model_id}\n{target}（{count} 个快照）")
    return Check(
        "模型权重",
        WARN,
        f"{model_id} 不在 {target}",
        "P2 的语义检索需要它；届时用 tools/fetch_model.py 下载"
        "（该脚本会显式关掉 HF_HUB_OFFLINE）",
    )


#: 只审计一层：够抓住"建了目录 / 写了文件"这类改动，而数据目录将来会有几十万个
#: 条目，`rglob` 一遍会让 doctor 本身变得很慢。
def _snapshot(path: Path) -> tuple:
    if not path.exists():
        return (False,)
    entries = tuple(
        sorted((child.name, child.stat().st_mtime_ns) for child in path.iterdir())
    )
    return (True, entries)


def check_no_side_effects(before: tuple, after: tuple) -> Check:
    """自证 A7：doctor 自己没在数据目录里留下任何东西。"""
    if before != after:
        return Check(
            "落点审计",
            FAIL,
            "doctor 运行期间数据目录发生了变化——它本该什么都不写",
            "这是 bug：检查行为改变了被检查的对象，结果不再可信",
        )
    return Check("落点审计", OK, "数据目录未被创建、未被修改（本命令不写日志文件）")


def run_checks(state: CliState) -> list[Check]:
    """跑全部检查，返回结果。不打印、不退出——测试直接调它。"""
    data_dir = state.config.data_dir_path
    before = _snapshot(data_dir)

    checks = [
        check_interpreter(),
        check_dependencies(),
        check_config(state),
        check_data_dir(state.config),
        check_roots(state.config),
        check_env(state.config, state.env_at_start),
        check_model(state.config, state.env_at_start),
    ]

    checks.append(check_no_side_effects(before, _snapshot(data_dir)))
    return checks


# ==============================================================================
# 呈现
# ==============================================================================

_STATUS_STYLE = {OK: "green", WARN: "yellow", FAIL: "red"}


def render(checks: list[Check], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="lsearch doctor —— 本机环境自检", show_lines=True)
    table.add_column("检查项", style="bold", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("说明")
    for check in checks:
        style = _STATUS_STYLE.get(check.status, "")
        # 说明文字必须转义：里面会出现 `[paths]`、`[red]` 这类方括号内容，
        # 不转义的话 rich 会把它当标记解析，轻则显示错、重则直接抛 MarkupError 崩掉。
        detail = escape(check.detail) + (
            f"\n[dim]→ {escape(check.hint)}[/dim]" if check.hint else ""
        )
        table.add_row(check.name, f"[{style}]{check.status}[/{style}]", detail)
    console.print(table)

    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    summary = f"{len(checks)} 项：{len(checks) - len(failed) - len(warned)} 通过"
    if warned:
        summary += f"，{len(warned)} 项警告"
    if failed:
        summary += f"，[red]{len(failed)} 项失败[/red]"
    console.print(summary + ("。" if not failed else "（失败项要先解决）"))


def doctor(ctx: typer.Context) -> None:
    """自检本机环境：解释器、依赖、配置、路径、环境变量、模型位置。

    不创建任何文件、不下载任何东西——检查不该改变被检查的对象。
    """
    state: CliState = ctx.obj
    checks = run_checks(state)
    render(checks)
    raise typer.Exit(EXIT_PROBLEM if any(c.status == FAIL for c in checks) else EXIT_OK)
