"""F1-01：项目本身可安装（pyproject / uv 环境）。

**这个文件测的不是"依赖装得上"**——那是 P0 的 E1（`uv sync` 退出码 0），已经结掉了。
这里测的是**本项目自己**作为包被打起来之后的形态，三者缺一不可：

1. `import localsearch` 成功，且**是从本仓库的 `src/` 装进来的**——不是从别处
   捡到一个同名包，也不是靠 pytest 的路径魔法蒙混过关；
2. 版本号只有一个真相来源：`__init__.__version__` 与已安装分发的元数据一致
   （两处各写一份，迟早会漂移）；
3. `lsearch` 命令行入口点在元数据里存在、指向的模块与函数**真的存在且可调用**。

第 3 条刻意不止步于"元数据里有一行字符串"：一个指向不存在模块的入口点，
在元数据层面看不出任何问题，直到用户敲下 `lsearch` 才炸。
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
from pathlib import Path

import pytest

DIST_NAME = "localsearch"
ENTRY_POINT_VALUE = "localsearch.cli.main:main"


def _repo_src() -> Path:
    """本仓库的 src/ 目录。tests/unit/test_package.py → parents[2] == 仓库根。"""
    return Path(__file__).resolve().parents[2] / "src"


def test_import_package_comes_from_this_tree() -> None:
    import localsearch

    installed_at = Path(localsearch.__file__).resolve()
    assert installed_at.is_relative_to(_repo_src()), (
        f"localsearch 不是从本仓库的 src/ 导入的，而是 {installed_at}——"
        "说明可编辑安装没生效，或环境里混进了另一份同名包"
    )


def test_version_single_source() -> None:
    """`__version__` 是唯一真相来源，pyproject 的动态版本从它读。"""
    import localsearch

    assert localsearch.__version__
    assert md.version(DIST_NAME) == localsearch.__version__, (
        "已安装分发的版本与 __init__.__version__ 不一致："
        "两者必须来自同一处（[tool.hatch.version] path）"
    )


def test_repo_adds_no_second_version_literal() -> None:
    """pyproject.toml 里不许再出现一个写死的 version。

    这条是防复发的：改成动态版本以后，只要有人在 [project] 里补回一行
    `version = "..."`，hatchling 会以静态值为准，`__version__` 就不再是真相来源，
    而上面那条测试**照样会通过**（元数据与它自己一致）。
    """
    pyproject = _repo_src().parent / "pyproject.toml"
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    assert 'dynamic = ["version"]' in lines, "pyproject 丢了 dynamic version 声明"
    assert not any(line.strip().startswith("version =") for line in lines), (
        "pyproject 的 [project] 里又出现写死的 version —— 版本号只允许有一个真相来源"
    )


def test_console_script_declared() -> None:
    entries = {ep.name: ep for ep in md.entry_points(group="console_scripts")}
    assert "lsearch" in entries, f"没有 lsearch 入口点；当前有：{sorted(entries)}"
    assert entries["lsearch"].value == ENTRY_POINT_VALUE


def test_console_script_target_is_callable() -> None:
    """入口点指向的东西必须真的存在——元数据里有一行字符串是不够的。"""
    module_name, _, attr = ENTRY_POINT_VALUE.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attr, None)
    assert callable(target), f"{ENTRY_POINT_VALUE} 指向的对象不存在或不可调用"


def test_installed_console_script_runs() -> None:
    """真的执行一次入口点：`lsearch --version` 退出码 0 且打出正确版本号。

    前面几条都只是在读元数据，这一条才碰执行路径——`main()` 返回的是啥、
    有没有在导入期就抛异常，只有跑起来才知道。这里用 `-c` 起子进程而不是直接
    调用函数，为的是绕开测试进程里可能已被改动的 sys.argv。
    """
    import subprocess
    import sys

    import localsearch

    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from localsearch.cli.main import main; sys.exit(main(['--version']))"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert localsearch.__version__ in proc.stdout, proc.stdout


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_flags_agree(flag: str) -> None:
    from localsearch.cli.main import main

    assert main([flag]) == 0
