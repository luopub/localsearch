r"""输出面不许因为多字节字符崩掉（`E16` 的护栏，2026-10-16 扩成跨模块的一层）。

**为什么单开一个文件**：这一层管的是 `stdout` / `stderr` 的**编码错误策略**，被测
对象横跨三处——`core/console.py`、CLI 入口、`tools/` 下每个有 `main()` 的脚本。挂在
任何单个功能点的测试文件里，那个文件名都会说谎。

三组，对应三种漏法：

1. **机制本身**（`test_...the_hatch...`）：`use_safe_output()` 之后，GBK 编不出的
   字符**降级而不是抛**。这条用真子进程 + `PYTHONIOENCODING=gbk`：只有进程级的
   编码设置才能重现"重定向到文件 / 管道"的那一档，`capsys` 捕获出来的对象根本没有
   编码这回事。
2. **产品面**（`test_...cli...`）：`lsearch <子命令> --help` 在 GBK 管道下退出码 0。
   它崩在 click 打印**命令文档串**的时候（`⚠` 是项目写作习惯），所以这一组的价值
   不是"帮助好看"，是"重定向到文件不再丢掉整屏输出"。
3. **纪律面**（`test_...calls_the_hatch...`，静态）：凡是打印的入口都必须调它。
   前两组只钉住**今天**这三个进程；这一条才是拦"新写的脚本又漏一次"的东西——
   `probe_ooxml_time.py`（2026-10-16 04:41）与 `check_mutations.py`（同日 04:49）
   就是在同一周里各漏了一次，前一次靠删字符、后一次靠这个模块。

⚠ 这一层是**兜底**，不是许可证：「打印路径上只用 GBK 认得的字符」（`E16`）**不撤销**，
`?` 终究是降级。能避免引入 `⚠` 的地方仍然避免。
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
CLI_MAIN = REPO_ROOT / "src" / "localsearch" / "cli" / "main.py"
HATCH = "use_safe_output"


def gbk_env() -> dict[str, str]:
    """本机默认控制台那一档：ANSI 代码页 936（`E16`）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    return env


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """真子进程 + 管道 + GBK：`PYTHONIOENCODING` 只有子进程能干净地设。"""
    return subprocess.run(args, cwd=REPO_ROOT, env=gbk_env(), text=True,
                          capture_output=True, encoding="gbk", errors="replace")


def calls_hatch(node: ast.AST) -> bool:
    """这段字节码里有没有直接调 `use_safe_output()`。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                and sub.func.id == HATCH:
            return True
    return False


def main_functions(path: pathlib.Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


# ---------------------------------------------------------------------------
# 1. 机制：降级，不许抛
# ---------------------------------------------------------------------------

def test_the_hatch_degrades_instead_of_raising() -> None:
    """装了兜底之后，GBK 编不出的字符印成 `?`，退出码 0。

    对照组是同一个子进程去掉那一句：它必须**崩**（否则这条测试就证明不了兜底在
    起作用——绿灯的含义会自动退化成"这个字符本来就编得了"）。
    """
    body = "print('\\u26a0 \\u2714 \\u00b5')"
    guarded = run([sys.executable, "-c",
                   f"from localsearch.core.console import {HATCH}; "
                   f"{HATCH}(); {body}"])
    assert guarded.returncode == 0, guarded.stderr
    assert guarded.stdout.strip() == "? ? ?", guarded.stdout

    bare = run([sys.executable, "-c", body])
    assert bare.returncode != 0, "没有兜底也不崩？那这条测试盯不住任何东西"
    assert "UnicodeEncodeError" in bare.stderr


def test_the_hatch_is_idempotent_and_skips_capture_objects() -> None:
    """幂等；对没有 `reconfigure` 的流（pytest 的捕获对象）直接跳过、不抛。

    `capsys` 装出来的是个没有 `reconfigure` 的对象，正是要跳过的那一档。
    """
    from localsearch.core.console import use_safe_output

    use_safe_output()
    use_safe_output()


# ---------------------------------------------------------------------------
# 2. 产品面：真进程里 `--help` 不再崩
# ---------------------------------------------------------------------------

CLI_HELP_CASES = [["--help"], ["index", "--help"]]


@pytest.mark.parametrize("argv", CLI_HELP_CASES, ids=["根命令", "index"])
def test_cli_help_survives_a_gbk_pipe(argv: list[str]) -> None:
    source = (f"from localsearch.cli.main import main; "
              f"raise SystemExit(main({argv!r}))")
    done = run([sys.executable, "-c", source])
    assert done.returncode == 0, done.stderr[-800:]
    assert "Usage" in done.stdout or "用法" in done.stdout, done.stdout[:200]


@pytest.mark.parametrize("name,argv", [
    ("check_mutations.py", ["--self-test"]),
    ("scan_report.py", ["--help"]),
], ids=["check_mutations", "scan_report"])
def test_the_tools_survive_a_gbk_pipe(name: str, argv: list[str]) -> None:
    """两个脚本自己的 `⚠` 都在打印路径上：`check_mutations.py` 更坏——它全部变体
    都抓到的那次跑法**退出码是 1**（它的退出码就是它的结论）。"""
    done = run([sys.executable, str(TOOLS / name), *argv])
    assert done.returncode == 0, done.stderr[-800:]


# ---------------------------------------------------------------------------
# 3. 纪律面：凡是打印的入口都得调它
# ---------------------------------------------------------------------------

TOOL_NAMES = sorted(path.name for path in TOOLS.glob("*.py"))


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_with_a_main_calls_the_hatch(name: str) -> None:
    mains = main_functions(TOOLS / name)
    if "main" not in mains:
        pytest.skip(f"{name} 没有 main()")
    assert calls_hatch(mains["main"]), \
        f"{name}: main() 里没有 {HATCH}()，它的输出在多字节字符上会崩（见 core/console.py）"


def test_the_cli_entry_calls_the_hatch() -> None:
    mains = main_functions(CLI_MAIN)
    assert "main" in mains, CLI_MAIN
    assert calls_hatch(mains["main"]), \
        f"cli/main.py: main() 里没有 {HATCH}()——`lsearch index --help > x.txt` 会崩"
