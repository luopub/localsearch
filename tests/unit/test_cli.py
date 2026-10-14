"""F1-04：CLI 壳。测试落点。

几条测试是针对**已经踩过的坑**写的，不是凑覆盖：

- `test_version_flag_works_without_subcommand`：Click 的分组在调用回调**之前**就检查
  "有没有子命令"。所以 `lsearch --version`（只有全局选项、没有子命令）会先被判成
  Missing command，回调里的 `--version` 根本执行不到。本文件的第一版就是这么写的，
  症状是 `--version` 打印用法错误、退出码 2。
- `test_unimplemented_commands_are_not_registered`：挂上一个能列出、一跑就崩的命令，
  用户读到的是"这命令有 bug"而不是"这功能还没做"，会去查一个不存在的问题。
- `test_read_only_commands_leave_no_trace`：只读命令为了回答"环境对不对"就在数据目录里
  建目录、写日志，会让"检查过"这件事本身改变被检查的对象（A7）。
- `test_cli_does_not_overwrite_existing_hf_home`：`bootstrap_env` 必须是 `setdefault`
  语义。覆盖系统已有的 `HF_HOME` 会让同一份模型权重在本机存两遍。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from localsearch import __version__
from localsearch.cli.main import LOG_FILE_EXEMPT_COMMANDS, app, main
from localsearch.core import config as config_mod
from localsearch.core.config import load_config
from localsearch.core.logging import reset_logging

#: 计划 §4.6 的命令面里，还没实现的那些。
#: 刻意**不含** `doctor`（F1-05 的交付物）与 `index`（F2-06 只实现了它的 `--dry-run`，
#: 行为测试在 `test_cli_index_dryrun.py` 里；本文件只保留"它已被注册"这一条）。
NOT_YET_IMPLEMENTED = ("search", "status", "explain", "bench", "reindex")


@pytest.fixture(autouse=True)
def clean_logging():
    """每个测试前后把 CLI 装到根 logger 上的 handler 摘干净。

    `main()` 走一遍就会装一次 handler。不摘的话 handler 会跨测试累积，而且它抓住的是
    安装那一刻的 `sys.stderr`——那正是 capsys 每个测试都会换掉的对象。
    """
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """把配置的四个落点全挪进 tmp_path。

    不这么做的话，测试会读**真实的** `config/config.toml`（如果哪天它存在），
    甚至让 `config init` 往仓库里写文件。测试碰真实配置的后果不是"这次失败"，
    而是"同一份测试在你机器上和在我机器上结果不同"。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    template = config_dir / "default.toml"
    template.write_text(
        f'[paths]\ndata_dir = "{data_dir.as_posix()}"\nroots = ["{(tmp_path / "docs").as_posix()}"]\n',
        encoding="utf-8",
    )
    local = config_dir / "config.toml"
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "TEMPLATE_PATH", template)
    monkeypatch.setattr(config_mod, "LOCAL_CONFIG_PATH", local)
    monkeypatch.delenv(config_mod.ENV_CONFIG_PATH, raising=False)
    return {"template": template, "local": local, "data_dir": data_dir, "config_dir": config_dir}


def write_valid_local_config(sandbox: dict[str, Path], *, data_dir: str) -> None:
    """写一份**合法**的本机配置。

    必须合法，这一点有讲究：根回调在任何子命令之前就会加载并校验配置，配置坏掉时
    连 `config init` 都跑不到（见 `test_broken_config_blocks_every_command`）。
    """
    sandbox["local"].write_text(
        f'[paths]\ndata_dir = "{data_dir}"\nroots = ["D:/work"]\n', encoding="utf-8"
    )


def flat(text: str) -> str:
    """把输出压成一行再断言。

    Typer 的帮助是用 rich 画表格，长行会被**折行**（中文尤其容易），
    直接对原文做子串断言会随终端宽度飘。压平之后断言的才是内容本身。
    """
    return " ".join(text.split())


# ==============================================================================
# 全局选项与退出码
# ==============================================================================


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_flag_prints_version(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([flag]) == 0
    assert capsys.readouterr().out.strip() == f"lsearch {__version__}"


def test_version_flag_works_without_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """`--version` 不能因为"没跟子命令"就被判成用法错误（见文件头第 1 条）。"""
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert "Missing command" not in (captured.out + captured.err)
    assert captured.out.strip() == f"lsearch {__version__}"


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_zero(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([flag]) == 0
    assert "Usage:" in flat(capsys.readouterr().out)


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """裸跑只打印帮助，且退出码是"用法不对"。

    REPL 是 P5 的 `F5-07`；在那之前把裸跑做成任何别的东西，都是在提前承诺一个
    还没实现的行为。

    断言里**不比对程序名**：帮助头部那个名字取自 `sys.argv[0]`，在测试进程里就是
    `pytest`。按程序名断言等于在测"谁启动了进程"，不是在测我们的帮助内容。
    """
    assert main([]) == 2
    out = flat(capsys.readouterr().out)
    assert "Usage:" in out
    assert "本地离线全盘全文搜索" in out
    assert "config" in out


# ==============================================================================
# 命令面：只暴露真的能跑的
# ==============================================================================


def registered_names() -> set[str]:
    names = {
        (info.name or getattr(info.callback, "__name__", ""))
        for info in app.registered_commands
    }
    names |= {group.name for group in app.registered_groups}
    return {name for name in names if name}


def test_only_implemented_commands_are_registered() -> None:
    names = registered_names()
    assert "config" in names
    for name in NOT_YET_IMPLEMENTED:
        assert name not in names, (
            f"`{name}` 属于后面的阶段，现在挂上去只会让用户以为是它坏了——"
            "要么实现它，要么别注册"
        )


def test_index_is_registered_even_though_only_dry_run_works() -> None:
    """`index` 是"只注册真的能跑的命令"这一条的**半分例外**，判定标准写在这里。

    它被挂上去是因为**命令自己会拒绝**：没有 `--dry-run` 时打印缺哪三块并退出码 2
    （行为在 `test_cli_index_dryrun.py` 里逐条钉住），不会静默跑出一个看着正常的空结果。
    所以这一条不是"它实现了"，而是"它不会误导人"——
    这也意味着 `NOT_YET_IMPLEMENTED` 那张名单的判据不是"阶段到了没有"。
    """
    assert "index" in registered_names()
    assert "index" not in NOT_YET_IMPLEMENTED


@pytest.mark.parametrize("name", NOT_YET_IMPLEMENTED)
def test_unimplemented_command_is_a_usage_error(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([name]) == 2, "没注册的命令应当是用法错误（退出码 2），不是崩溃"
    captured = capsys.readouterr()
    assert "No such command" in flat(captured.out + captured.err)


def test_config_group_has_three_subcommands() -> None:
    from localsearch.cli.commands.config import config_app

    assert {c.name for c in config_app.registered_commands} == {"init", "show", "path"}


# ==============================================================================
# `lsearch config …`
# ==============================================================================


def test_config_path_lists_where_things_are(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "path"]) == 0
    out = flat(capsys.readouterr().out)
    for label in ("项目根", "模板", "本机配置", "生效文件", "data_dir", "日志目录", "日志文件"):
        assert label in out, f"`config path` 少了 {label} 这一行"


def test_config_path_points_at_the_effective_file(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """没有本机配置时报的必须是**模板**，而不是"我以为在用的那个文件"。

    断言里必须连标签一起比：`本机配置` 那一行**也是**本机配置的路径，
    只比路径的话，`生效文件` 指错了地方这条测试照样会绿。
    """
    assert main(["config", "path"]) == 0
    assert f"生效文件 {sandbox['template']}" in flat(capsys.readouterr().out)

    write_valid_local_config(sandbox, data_dir="D:/other-data")
    assert main(["config", "path"]) == 0
    assert f"生效文件 {sandbox['local']}" in flat(capsys.readouterr().out)


def test_config_show_output_can_be_loaded_back(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`show` 打印的东西必须能原样存成配置文件再读回来，且解析结果一模一样。

    这是唯一能证明 `show` **没写错**的检查。少了它，`dump_toml` 漏个引号、把 tuple
    写成 `(a, b)`、把 bool 写成 `True`，都要等用户真去粘贴一次才发现。
    """
    assert main(["config", "show"]) == 0
    body = "\n".join(
        line for line in capsys.readouterr().out.splitlines() if not line.startswith("#")
    )
    parsed = tomllib.loads(body)
    assert parsed["paths"]["roots"], "打印出来的配置里 roots 是空的"

    round_trip = tmp_path / "round-trip.toml"
    round_trip.write_text(body, encoding="utf-8")
    assert load_config(round_trip) == load_config(config_mod.TEMPLATE_PATH)


def test_config_show_says_where_it_came_from(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from localsearch.core.config import config_source_kind

    assert main(["config", "show"]) == 0
    assert config_source_kind(sandbox["template"]) in capsys.readouterr().out


def test_config_init_copies_the_template(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "init"]) == 0
    assert sandbox["local"].read_text(encoding="utf-8") == sandbox["template"].read_text(
        encoding="utf-8"
    )
    assert str(sandbox["local"]) in capsys.readouterr().out


def test_config_init_refuses_to_overwrite(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """本机配置里可能有用户改过的路径。静默覆盖等于把别人改的东西删了还不说。"""
    write_valid_local_config(sandbox, data_dir="D:/mine")
    assert main(["config", "init"]) == 1
    assert "D:/mine" in sandbox["local"].read_text(encoding="utf-8")
    assert "--force" in capsys.readouterr().err


def test_config_init_force_overwrites(sandbox: dict[str, Path]) -> None:
    write_valid_local_config(sandbox, data_dir="D:/mine")
    assert main(["config", "init", "--force"]) == 0
    assert sandbox["local"].read_text(encoding="utf-8") == sandbox["template"].read_text(
        encoding="utf-8"
    )


def test_config_init_reports_missing_template(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox["template"].unlink()
    assert main(["config", "init"]) == 2
    assert "模板" in capsys.readouterr().err


# ==============================================================================
# 坏配置就该立刻停
# ==============================================================================


def test_broken_config_exits_two(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[paths]\ndata_dir = "D:/x"\nnope = 1\n', encoding="utf-8")
    assert main(["--config", str(bad), "config", "path"]) == 2
    assert "配置有误" in capsys.readouterr().err


def test_missing_explicit_config_exits_two(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """显式指定的配置文件不存在时必须报错，**不能**静默回退到别的文件。"""
    assert main(["--config", str(tmp_path / "nope.toml"), "config", "path"]) == 2
    assert "配置有误" in capsys.readouterr().err


def test_bad_log_level_exits_two(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--log-level", "verbose", "config", "path"]) == 2
    assert "verbose" in capsys.readouterr().err


def test_broken_config_blocks_every_command(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """记录一个**已知的取舍**：配置坏了时所有命令都跑不了，包括 `config init`。

    `config init` 本来是最该在这种情况下还能跑的（推倒重来），没做成是因为根回调
    必须知道二级子命令名才能给它豁免，而 Click 在调用回调**之前**就把子命令参数
    清空了，拿不到。代价写在测试里，不至于以后有人以为这是漏测：

    配置写错时得先手动删掉 `config/config.toml`（或 `--config` 指一份能读的）才能
    重建。报错会指名哪个键不对，所以这条路不至于无解——但确实比理想情况多一步。
    """
    sandbox["local"].write_text('[paths]\ndata_dir = "D:/x"\nnope = 1\n', encoding="utf-8")
    assert main(["config", "init"]) == 2
    assert "配置有误" in capsys.readouterr().err


# ==============================================================================
# 只读命令不留落点
# ==============================================================================


@pytest.mark.parametrize(
    "argv",
    [[], ["--version"], ["config"], ["config", "path"], ["config", "show"]],
    ids=["bare", "version", "config", "config-path", "config-show"],
)
def test_read_only_commands_leave_no_trace(
    argv: list[str], sandbox: dict[str, Path]
) -> None:
    main(argv)
    assert not sandbox["data_dir"].exists(), (
        f"`lsearch {' '.join(argv)}` 在数据目录里留下了东西："
        "只读命令不该让「检查过」这件事本身改变被检查的对象"
    )


def test_log_file_is_exempt_exactly_for_read_only_commands() -> None:
    """写入日志文件的那条判断，规则要能一眼看清是**哪些**命令被豁免、**为什么**。

    `doctor` 在 F1-05 落地时也应当在这一格里——它是只读诊断，同样不留落点。
    `index` 在 F2-06 落地时进这一格，因为它那时只做只读的 `--dry-run`；**它的理由
    栏里写着 P4 必须把它删掉**，这一条下面单独钉。
    """
    assert "config" in LOG_FILE_EXEMPT_COMMANDS
    for name, why in LOG_FILE_EXEMPT_COMMANDS.items():
        assert why.strip(), f"`{name}` 被豁免了日志，但没写原因——这张表最容易被顺手改错"


def test_index_exemption_says_when_to_remove_it() -> None:
    """`index` 的豁免是**暂时的**，且到期条件必须写在表里。

    真正去索引的那天它要写日志，而"忘了把它从豁免表里删掉"的症状是
    "任务跑过了但日志里什么都没有"——与"根本没跑"长得一样，属于最难查的一类。
    把到期条件写进值里，是为了让改的人（很可能是几个月后的我）在读到这一行时
    就知道该做什么，而不是去猜这张表当初为什么这么填。
    """
    why = LOG_FILE_EXEMPT_COMMANDS["index"]
    assert "P4" in why, "到期条件不见了：写清楚哪一天必须把它删掉"
    assert "dry-run" in why, "要说明它现在凭什么被豁免"


# ==============================================================================
# 环境变量兜底
# ==============================================================================


def test_cli_does_not_overwrite_existing_hf_home(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", "X:/keep-me")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert main(["config", "path"]) == 0
    assert os.environ["HF_HOME"] == "X:/keep-me", (
        "CLI 覆盖了系统已有的 HF_HOME；那会让同一份模型权重在本机存两遍"
    )
    assert os.environ["HF_HUB_OFFLINE"] == "1", (
        "离线兜底没设上：索引/检索在联网环境下会去访问 Hugging Face"
    )
