"""F1-05：`lsearch doctor`。测试落点。

这个文件里有几条是针对**已经踩到的错**写的：

- `test_env_source_is_attributed_against_the_start_snapshot`：第一次跑 doctor 时，
  表格把 `TORCH_HOME` 的来源报成了"系统已设"，而系统里根本没这个变量——是根回调里的
  `bootstrap_env` 在几毫秒前刚把它写进 `os.environ`。来源栏撒谎，恰恰发生在最需要
  看它的兜底项上，等于把这一行变成装饰。
- `test_side_effect_audit_detects_a_write`：那条"落点审计"如果只是把两个空元组比一下，
  它永远是 OK，永远发现不了问题。要证明它真在读目录。
- `test_render_survives_square_brackets`：说明文字里会出现 `[paths]` 这类方括号内容，
  rich 会把它们当标记解析——不转义的话 doctor 直接抛 MarkupError 崩掉。

另外注意：本文件**不查真实机器**。所有检查都喂构造出来的配置与目录，否则"这台机器
今天恰好是绿的"会变成测试的一部分，换台机器就红。
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console

from localsearch.cli.commands import doctor as doctor_mod
from localsearch.cli.commands.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    check_data_dir,
    check_dependencies,
    check_env,
    check_interpreter,
    check_model,
    check_no_side_effects,
    check_roots,
    render,
    run_checks,
)
from localsearch.cli.commands.doctor import _snapshot
from localsearch.cli.main import LOG_FILE_EXEMPT_COMMANDS, app, main
from localsearch.cli.state import CliState
from localsearch.core import config as config_mod
from localsearch.core.config import load_config
from localsearch.core.logging import LoggingSetup


# ==============================================================================
# 脚手架
# ==============================================================================


def write_template(
    box: dict[str, Path],
    *,
    data_dir: str | None = None,
    roots: list[str] | None = None,
    all_drives: bool = False,
) -> None:
    """写一份最小但合法的模板。

    路径一律用 `as_posix()`：TOML 的双引号字符串里 `C:\\Users\\...` 的 `\\U` 会被当成
    转义序列，直接解析失败——这是真踩过的（`core/config.py` 的 docstring 就为此
    改成了 raw string）。
    """
    where = data_dir if data_dir is not None else box["data_dir"].as_posix()
    entries = roots if roots is not None else [(box["tmp"] / "docs").as_posix()]
    inside = ", ".join(f'"{entry}"' for entry in entries)
    box["template"].write_text(
        "[paths]\n"
        f'data_dir = "{where}"\n'
        f"roots = [{inside}]\n"
        f"all_drives = {'true' if all_drives else 'false'}\n",
        encoding="utf-8",
    )


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """把配置的四个落点全挪进 tmp_path（理由同 test_cli.py 的同名脚手架）。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / "docs").mkdir()
    local = config_dir / "config.toml"
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "TEMPLATE_PATH", config_dir / "default.toml")
    monkeypatch.setattr(config_mod, "LOCAL_CONFIG_PATH", local)
    monkeypatch.delenv(config_mod.ENV_CONFIG_PATH, raising=False)
    sandbox: dict[str, Path] = {
        "tmp": tmp_path,
        "template": config_dir / "default.toml",
        "local": local,
        "data_dir": tmp_path / "data",
        "hf_home": tmp_path / "hf",
    }
    write_template(sandbox)
    return sandbox


def make_state(config_file: Path, *, env: dict[str, str] | None = None) -> CliState:
    return CliState(
        config=load_config(config_file),
        config_file=config_file,
        log=LoggingSetup(level="INFO", log_file=None, file_ok=False, console=True),
        env_at_start={} if env is None else env,
    )


def run(box: dict[str, Path], *args: str) -> int:
    """跑一次 CLI，返回退出码。

    `--config` 是**全局选项**（根回调上的），Click 只认它出现在子命令**之前**：
    `lsearch doctor --config x` 会报 `No such option: --config`，退出码 2。
    这不是我们的 bug 而是 Click 的约定，但顺序反了报的错与"选项写错"长得一样，
    所以把顺序封在这一个地方，免得每条测试各踩一次。
    """
    return main(["--config", str(box["template"]), *args])


def seed_model(hf_home: Path, model_id: str = "google/embeddinggemma-2") -> Path:
    snapshots = hf_home / "hub" / ("models--" + model_id.replace("/", "--")) / "snapshots" / "abc"
    snapshots.mkdir(parents=True)
    return snapshots


def status_of(checks: list[Check], name: str) -> str:
    return next(c for c in checks if c.name == name).status


class VersionInfo(tuple):
    """`sys.version_info` 的替身。

    真货造不出来——CPython 的 `sys.version_info` 是 structseq，`type(sys.version_info)((3, 11, 9))`
    会抛 `TypeError: cannot create 'sys.version_info' instances`。所以只能自己搭一个，
    只实现被测代码用到的那两件事：`.major/.minor/.micro` 与切片比较 `[:2] < (3, 12)`
    （tuple 本身就支持后者）。
    """

    def __new__(cls, major: int, minor: int, micro: int) -> "VersionInfo":
        self = super().__new__(cls, (major, minor, micro))
        self.major, self.minor, self.micro = major, minor, micro
        return self


# ==============================================================================
# 全绿与退出码
# ==============================================================================


def test_everything_passes_on_a_healthy_setup(box: dict[str, Path]) -> None:
    seed_model(box["hf_home"])
    state = make_state(box["template"], env={"HF_HOME": box["hf_home"].as_posix()})

    checks = run_checks(state)

    assert len(checks) >= 8, "检查项少了一半，多半是有人在 run_checks 里漏挂了"
    assert [c.status for c in checks] == [OK] * len(checks), [
        (c.name, c.status, c.detail) for c in checks if c.status != OK
    ]


def test_exit_code_is_zero_when_only_warnings(box: dict[str, Path]) -> None:
    """没有模型只是 WARN：P1 用不到它，拿它挡路会让人开始忽略退出码。"""
    state = make_state(box["template"], env={"HF_HOME": (box["tmp"] / "empty").as_posix()})
    checks = run_checks(state)

    assert status_of(checks, "模型权重") == WARN
    assert all(c.status != FAIL for c in checks)


def test_exit_code_is_one_when_a_check_fails(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor_mod, "check_dependencies", lambda: Check("依赖", FAIL, "构造出来的失败")
    )
    assert run(box, "doctor") == 1


def test_exit_code_is_zero_when_healthy(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_model(box["hf_home"])
    monkeypatch.setenv("HF_HOME", box["hf_home"].as_posix())
    assert run(box, "doctor") == 0


# ==============================================================================
# 环境变量来源（A7 的核心）
# ==============================================================================


def test_env_source_is_attributed_against_the_start_snapshot(box: dict[str, Path]) -> None:
    """来源必须按**进程启动时**的环境判，不是按已被 `bootstrap_env` 改过的 `os.environ`。

    这条是回归测试：doctor 第一次跑起来时，`TORCH_HOME` 明明是我们自己填的兜底值，
    却被报成"系统已设"——因为根回调在它之前已经 `setdefault` 过了。
    """
    state = make_state(box["template"], env={})
    detail = check_env(state.config, state.env_at_start).detail
    lines = {line.split(" = ")[0]: line for line in detail.splitlines()}

    assert "兜底值" in lines["TORCH_HOME"], lines["TORCH_HOME"]
    assert "兜底值" in lines["HF_HUB_OFFLINE"], lines["HF_HUB_OFFLINE"]

    # 系统真的设了的时候，仍然要报"系统已设"——否则修完变成另一个极端
    given = make_state(box["template"], env={"HF_HOME": "C:/somewhere"})
    detail = check_env(given.config, given.env_at_start).detail
    assert "HF_HOME = C:/somewhere" in detail
    assert "系统已设" in next(l for l in detail.splitlines() if l.startswith("HF_HOME"))


def test_cli_passes_the_start_snapshot_to_the_checks(
    box: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一条测的是 `check_env` 本身，这条测**接线**：main 有没有把快照递下去。"""
    monkeypatch.delenv("TORCH_HOME", raising=False)
    captured: list[list[Check]] = []
    monkeypatch.setattr(doctor_mod, "render", lambda checks, console=None: captured.append(checks))

    assert run(box, "doctor") == 0

    env_check = next(c for c in captured[0] if c.name == "环境变量")
    torch_line = next(l for l in env_check.detail.splitlines() if l.startswith("TORCH_HOME"))
    assert "兜底值" in torch_line, (
        f"{torch_line} —— 根回调没有把 bootstrap_env 之前的环境快照传进来，"
        "doctor 于是把自己的写当成了系统的写"
    )


def test_model_reports_a_warning_when_missing(box: dict[str, Path]) -> None:
    state = make_state(box["template"], env={"HF_HOME": box["hf_home"].as_posix()})
    check = check_model(state.config, state.env_at_start)
    assert check.status == WARN
    assert "embeddinggemma" in check.detail
    assert check.hint, "不能只说'没有'，得说怎么办"


def test_model_points_at_the_hf_home_we_actually_use(box: dict[str, Path]) -> None:
    """模型位置跟着 `HF_HOME` 走，且用的是**生效值**（可能是兜底值）。"""
    seed_model(box["hf_home"])
    state = make_state(box["template"], env={"HF_HOME": box["hf_home"].as_posix()})
    check = check_model(state.config, state.env_at_start)
    assert check.status == OK
    assert box["hf_home"].as_posix() in check.detail.replace("\\", "/")


# ==============================================================================
# 落点审计（A7）
# ==============================================================================


def test_side_effect_audit_detects_a_write(tmp_path: Path) -> None:
    """审计必须真在读目录。只比两个空元组的话它永远 OK，等于没做。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    before = _snapshot(data_dir)

    (data_dir / "lsearch.log").write_text("有人写东西了", encoding="utf-8")

    assert check_no_side_effects(before, _snapshot(data_dir)).status == FAIL


def test_side_effect_audit_passes_when_nothing_changed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    before = _snapshot(data_dir)
    assert check_no_side_effects(before, _snapshot(data_dir)).status == OK


def test_doctor_does_not_create_the_data_dir(box: dict[str, Path]) -> None:
    """A7：跑一次 doctor 不能在数据目录里留下任何东西。"""
    assert not box["data_dir"].exists()
    assert run(box, "doctor") == 0
    assert not box["data_dir"].exists(), "doctor 创建了数据目录"
    assert not box["data_dir"].joinpath("logs").exists(), "doctor 写了日志文件"


def test_doctor_is_exempt_from_the_log_file() -> None:
    """`doctor` 不写日志文件——它与 `config` 是同一类只读命令。"""
    assert "doctor" in LOG_FILE_EXEMPT_COMMANDS
    names = {info.name or getattr(info.callback, "__name__", "") for info in app.registered_commands}
    assert "doctor" in names, "doctor 没挂到应用上，`lsearch doctor` 会是用法错误"


# ==============================================================================
# 单项检查的判定规则
# ==============================================================================


def test_data_dir_not_created_yet_is_still_ok(box: dict[str, Path]) -> None:
    """首次索引前数据目录不存在是**正常**状态，不该报成问题。"""
    state = make_state(box["template"])
    check = check_data_dir(state.config)
    assert check.status == OK
    assert "尚未创建" in check.detail


def test_data_dir_blocked_by_a_file_is_a_failure(box: dict[str, Path], tmp_path: Path) -> None:
    """上级路径上有个同名文件，索引目录永远建不起来——这种要报 FAIL，不能报 OK。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    write_template(box, data_dir=(blocker / "data").as_posix())

    state = make_state(box["template"])
    check = check_data_dir(state.config)
    assert check.status == FAIL
    assert "blocker" in check.detail
    assert check.hint


def test_missing_root_is_a_warning(box: dict[str, Path]) -> None:
    write_template(box, roots=[(box["tmp"] / "not-mounted").as_posix()])
    state = make_state(box["template"])
    check = check_roots(state.config)
    assert check.status == WARN
    assert "不存在或未挂载" in check.detail
    assert check.hint, "要说清「不可达的根会怎样」（不会被索引，也不会被 prune 清掉）"


def test_all_drives_is_a_failure_in_v1(box: dict[str, Path]) -> None:
    """`all_drives = true` 在 v1 里是 FAIL，不是 WARN。

    它原先报 WARN（"现在能用，你知道一下"），但 `F2-01` 的扫描器**直接拒绝**这个开关
    （`ingest/base.py` 的 `make_scanner`），所以按 `doctor` 自己的定义——
    **FAIL 是"这样跑不了"**——它已经不该是 WARN 了。危险本身仍是"本地盘判不出来"
    （E10：E: 也报 `DRIVE_FIXED`），这一点写在 hint 里。
    """
    write_template(box, all_drives=True)
    state = make_state(box["template"])
    check = check_roots(state.config)
    assert check.status == FAIL
    assert "本地" in check.hint


def test_existing_root_is_ok(box: dict[str, Path]) -> None:
    state = make_state(box["template"])
    check = check_roots(state.config)
    assert check.status == OK
    assert "存在" in check.detail


@pytest.mark.parametrize(
    ("version", "expected"),
    [((3, 11, 9), FAIL), ((3, 12, 12), OK), ((3, 13, 5), WARN)],
)
def test_interpreter_rule(monkeypatch: pytest.MonkeyPatch, version: tuple, expected: str) -> None:
    monkeypatch.setattr(sys, "version_info", VersionInfo(*version))
    assert check_interpreter().status == expected


def test_dependencies_check_counts_what_pyproject_declares() -> None:
    """这条真正查的是"当前这个 venv 装齐了没有"——它失败说明环境真的坏了。

    比的是 `n/n` 这个**形状**，不是固定的 `19/19`：写死数字的话，以后每加一个依赖
    就要顺手改一次测试，而"顺手改绿"多了，这条测试就没人信了。
    """
    check = check_dependencies()
    assert check.status == OK, check.detail
    declared, installed = re.match(r"(\d+)/(\d+) 已安装", check.detail).groups()
    assert declared == installed, check.detail
    assert int(declared) >= 10, f"依赖名单只剩 {declared} 项，多半是元数据读错了：{check.detail}"


# ==============================================================================
# 呈现
# ==============================================================================


def test_render_survives_square_brackets() -> None:
    """说明与提示里的方括号不能被 rich 当标记解析（否则直接抛 MarkupError）。"""
    stream = io.StringIO()
    checks = [
        Check("data_dir", FAIL, "C:/x（[paths] data_dir 不可写）", "改 [paths] data_dir"),
        Check("落点审计", OK, "未改动"),
    ]
    render(checks, console=Console(file=stream, width=100, no_color=True))

    out = stream.getvalue()
    assert "[paths] data_dir" in out
    assert "FAIL" in out
