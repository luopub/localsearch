"""F1-04：CLI 壳。测试落点。

几条测试是针对**已经踩过的坑**写的，不是凑覆盖：

- `test_version_flag_works_without_subcommand`：Click 的分组在调用回调**之前**就检查
  "有没有子命令"。所以 `lsearch --version`（只有全局选项、没有子命令）会先被判成
  Missing command，回调里的 `--version` 根本执行不到。本文件的第一版就是这么写的，
  症状是 `--version` 打印用法错误、退出码 2。
- `test_only_implemented_commands_are_registered`：挂上一个能列出、一跑就崩的命令，
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

#: 计划 §4.6 的命令面，**整份**。下面那两条测试靠它把"已实现"与"还没实现"分成两半，
#: 而这两半必须恰好拼成这一份——多一个少一个都是接线漏了（见那条测试的文档）。
PLANNED_COMMANDS = (
    "config",
    "doctor",
    "index",
    "search",
    "status",
    "explain",
    "bench",
    "reindex",
)

#: 计划 §4.6 的命令面里，还没实现的那些。`search` 在 `F4-04` 落地（最小的那一版，
#: 行为测试在 `test_cli_search.py`），`index` 的真索引同样在 `F4-04` 落地
#: （`test_index_resume.py`）；这一行里的每一个都是**已经确认不想现在挂上去**的。
NOT_YET_IMPLEMENTED = ("status", "explain", "bench", "reindex")


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


def test_the_two_lists_partition_the_planned_command_surface() -> None:
    """命令面被"已注册"与"还没实现"**恰好分成两半**，两个方向都要钉。

    `F4-04` 之前这里钉的是 `index` 那个"半分例外"（它靠**自己会拒绝**换来了注册）。
    真索引落地之后那条例外没了，剩下的是一条更要紧的不变量——**两张名单必须对得上**：

    - 注册了却还留在 `NOT_YET_IMPLEMENTED` 里 → 那张名单在骗人。这个失败形态**真的
      发生过**：`F4-04` 把 `search` 挂上去时忘了划掉，于是
      `test_unimplemented_command_is_a_usage_error[search]` 跑到一半才发现自相矛盾。
    - 名单里有、两块都没有 → 计划 §4.6 的命令面少了一条，而它**不报错**：
      `lsearch <那个命令>` 只是回一句 `No such command`，与"还没实现"长得一样。
    """
    names = registered_names()
    assert names | set(NOT_YET_IMPLEMENTED) == set(PLANNED_COMMANDS), (
        "命令面与计划 §4.6 对不上：注册的那些加上没实现的那些，应当恰好是整份名单"
    )
    assert not (names & set(NOT_YET_IMPLEMENTED)), (
        "名单里有的命令其实已经注册了——实现之后忘了把它划掉，那张名单就不再可信"
    )


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
    `index` 留在这里，但**理由栏在 `F4-04` 换过一次**（下面单独钉）。
    """
    assert "config" in LOG_FILE_EXEMPT_COMMANDS
    for name, why in LOG_FILE_EXEMPT_COMMANDS.items():
        assert why.strip(), f"`{name}` 被豁免了日志，但没写原因——这张表最容易被顺手改错"


def test_the_index_exemption_no_longer_expires() -> None:
    """`index` 的豁免**从"到 P4 必须删掉"变成了"这一条不再到期"**，理由栏要跟着改。

    原先那张表里写着"P4 那天必须删掉这一行"，因为真索引落地之后它就该写日志，
    而"忘了删"的症状是"任务跑过了但日志里什么都没有"——与"根本没跑"长得一样。
    `F4-04` 落地时**没有**删掉它，而是把决定权交给了命令自己：`--dry-run` 不留落点
    （那是它对用户的承诺），真索引在跑起来之前自己把文件 handler 补上。理由是那句
    "做不到"**只对根回调成立**——Click 在根回调之前就清空了子命令的参数，而这个命令
    自己看得到（详见 `cli/main.py` 那段注释）。

    所以这一条钉两件事：**旧理由不见了**（否则下一个人会照着一句已经作废的话去删一行
    现在删不得的表项），以及**新理由说清了决定权在哪儿**——只写"按参数决定"而不写
    "谁在执行这个决定"，读的人仍然要去猜。
    """
    why = LOG_FILE_EXEMPT_COMMANDS["index"]
    assert "必须删" not in why and "还没实现" not in why, (
        "理由栏里还留着作废的到期条件：照着它去删，真索引那一趟就没有文件日志了"
    )
    assert "--dry-run" in why, "要说明两个模式里哪一个才是不留落点的那一个"
    assert "命令自己" in why, "要指出去哪儿看这个决定（`cli/commands/index.py`）"


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
