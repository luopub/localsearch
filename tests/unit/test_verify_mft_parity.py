"""`tools/verify_mft_parity.py` 的**行为**测试（不是它的排版）。

这个脚本是 `F2-02` 真机验收的入口：判定在 `ingest/parity.py`（那边有单测），本文件
钉的是**脚本出口**那几件已经各自咬过人的事：

1. **在本机默认控制台下跑得起来**（`PYTHONIOENCODING=gbk`）。2026-10-15 01:24:49
   实测过一次同类的翻车：账目里混进了 U+2212，`print` 抛 `UnicodeEncodeError`，
   整轮实测的输出**一条都没留下**。参数化 gbk / utf-8，因为"为了修 gbk 把 utf-8
   弄坏"是完全可能的修法。
2. **退出码分得清"跑不起来"与"核对不通过"**。两者都要人去查，但查的地方不同；
   合成一个非零码，报告就给不出这个区别。更坏的是**假通过**：两侧都是 0 条时
   `ParityCheck.ok` 是 `True`，如果就这么退出 0，一次什么都没比的运行看起来与一次
   真正的验收一模一样。这一条单独测（`test_an_empty_run_is_not_a_pass`）。
3. **没提权时不许印"通过"**。`probe` 失败就走不了 MFT，那时 stdout 一个判定字都
   不该有。

⚠ **两种跑法，各自的理由**：

- **子进程**（`PYTHONIOENCODING` 只有子进程能干净地设）跑 `--mft-from-tree`：它把
  MFT 侧换成从真目录树合成的记录流，于是**不需要提权**、也不碰卷——本机（以及将来
  的 CI）因此都能跑，且结果与"终端是不是管理员"无关。
- **同进程**（`importlib` 载入脚本、`monkeypatch` 掉 `probe`）跑那几条要**注入失败**
  的：探针失败、少一条记录、空目录。这些在真机上是"要凑"的状态，注进去才测得稳。
  同进程同时也绕开了"提权终端里第一条测试会真的去枚举 C: 整卷"这件事。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from localsearch.ingest import ntfs
from localsearch.ingest.scanner_mft import MftProbe

TOOL = Path(__file__).resolve().parents[2] / "tools" / "verify_mft_parity.py"

#: 规则文件：一行中文注释（GBK 认得的汉字）+ 一条真规则。刻意不用仓库那份
#: `.lsearchignore`——那份有 284 条规则，将来某一条命中临时目录的名字时，
#: 这里失败的原因会看起来像后端出了错。
IGNORE = "# 中文注释：这一行不进规则表\n*.log\n"


def load_tool():
    """把脚本当模块载入，好 `monkeypatch` 它的 `probe`、给它注入 `mft_source`。

    `tools/` 不是包（没有 `__init__.py`），所以按路径载入；名字取脚本自己的名字，
    这样栈回溯里显示的仍是 `verify_mft_parity.py`。
    """
    spec = importlib.util.spec_from_file_location("verify_mft_parity", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool():
    return load_tool()


def tree(root: Path) -> tuple[Path, Path]:
    """一棵小树 + 一份规则文件。返回 `(root, ignore_file)`。

    树里必须有**目录**（否则"根不是条目"这类判据测不到）与**一个带内容的文件**。
    """
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("BB", encoding="utf-8")
    (root / "空目录").mkdir()
    (root / "x.log").write_text("x", encoding="utf-8")
    ignore = root / "ignore.txt"
    ignore.write_text(IGNORE, encoding="utf-8")
    return root, ignore


def run_tool(*args: str, encoding: str = "gbk") -> subprocess.CompletedProcess[str]:
    """按指定编码起一条真进程。编码即 `PYTHONIOENCODING`，也就是 stdout 的编码。

    ⚠ `-u`：不加的话 stdout 走缓冲，进程中途崩掉时报告会停在管道里——
    `probe_mft.py` 的用法注释里也是这个理由。
    """
    env = dict(os.environ, PYTHONIOENCODING=encoding)
    return subprocess.run(
        [sys.executable, "-u", str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding=encoding,
        env=env,
    )


def source_of(records: list[ntfs.Record]):
    """把一份记录流包成 `MftScanner` 认的 `VolumeSource`（替身，不碰卷）。

    `counters.records` 必须自己填：报告里的"整卷记录"取自它，不填就是 0。
    """
    @contextmanager
    def source(device: str, counters: ntfs.RecordCounters):
        counters.records = len(records)
        yield iter(records)

    return source


# ==============================================================================
# A. 子进程：控制台编码 + 通过的那一轮
# ==============================================================================


@pytest.mark.parametrize("encoding", ["gbk", "utf-8"])
def test_it_runs_under_the_console_encoding(tmp_path: Path, encoding: str) -> None:
    """两种编码下都要跑通，且**判定要印在 stdout 上**（`--mft-from-tree` 不需要提权，
    所以这一条在管理员终端里也一样跑）。
    """
    root, ignore = tree(tmp_path)
    proc = run_tool(str(root), "--ignore-file", str(ignore),
                    "--mft-from-tree", str(root), encoding=encoding)
    assert proc.returncode == 0, proc.stderr
    assert "扫描根" in proc.stdout
    assert "核对（判据：按 FRN 折叠硬链接后集合相等）" in proc.stdout
    assert "结论          通过" in proc.stdout
    # 合成的那一轮必须**自己说清楚它不是证据**：它退出 0，很容易被当成一次真验收
    assert "这一轮不是 F2-02 的证据" in proc.stdout
    # 被 `*.log` 排掉的那一条要能在账目里看见（报告只给计数，不给被排掉的路径清单——
    # 清单是 `lsearch index --dry-run` 的活）
    assert "排除 1" in proc.stdout


def test_json_says_a_synthesized_run_is_not_evidence(tmp_path: Path) -> None:
    """`--json` 里 `is_evidence` 必须是**显式的一栏**：拿 JSON 的人（包括将来的 CI）
    不该靠"命令行上有没有 `--mft-from-tree`"去推断这一轮算不算数。
    """
    root, ignore = tree(tmp_path)
    proc = run_tool(str(root), "--ignore-file", str(ignore),
                    "--mft-from-tree", str(root), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mft_side"] == "synthesized"
    assert payload["is_evidence"] is False
    assert payload["ok"] is True and payload["problem"] == ""
    parity = payload["parity"]
    assert parity["mft_only_total"] == 0
    assert parity["unexplained_total"] == 0
    assert parity["action_mismatch_total"] == 0
    assert parity["is_dir_mismatch_total"] == 0
    # 两侧条目数**本来就不等也能通过**（等是因为这棵树里没有硬链接）
    assert parity["mft_entries"] == parity["scandir_entries"]
    assert payload["mft_stats"]["roots_scanned"] == 1
    # MFT 从不打开目录：这一栏在那一侧恒为 0（base.ScanStats 里写了理由）
    assert payload["mft_stats"]["dirs_entered"] == 0
    assert payload["scandir_stats"]["dirs_entered"] > 0


def test_log_writes_the_same_report_it_prints(tmp_path: Path) -> None:
    """`--log` 与 `>` 等价：**同一段排版代码**，所以文件与终端永远长得一样。
    两份排版迟早漂移，而漂移出来的那份通常是文件——也就是被收进报告的那份。
    """
    root, ignore = tree(tmp_path)
    log = tmp_path / "报告.md"
    proc = run_tool(str(root), "--ignore-file", str(ignore),
                    "--mft-from-tree", str(root), "--log", str(log))
    assert proc.returncode == 0, proc.stderr
    assert log.read_text(encoding="utf-8") == proc.stdout


# ==============================================================================
# B. 同进程：退出码的四种含义
# ==============================================================================


def test_a_failed_probe_is_a_cannot_run_and_prints_no_verdict(
    tmp_path: Path, tool, monkeypatch, capsys
) -> None:
    """探针失败 ≠ 核对不通过：退出 2，理由进 stderr，**stdout 上不许出现任何判定**。

    没提权是常态（计划 §4.8 的 `LeastPrivilege`），不能让它的报告看起来像一次
    "MFT 与 scandir 不一致"的结论。
    """
    root, ignore = tree(tmp_path)
    reason = "打开卷设备 失败：\\\\.\\C: WinError 5 ERROR_ACCESS_DENIED —— 没提权"
    monkeypatch.setattr(tool, "probe", lambda config: MftProbe(False, reason))

    code = tool.main([str(root), "--ignore-file", str(ignore)])
    captured = capsys.readouterr()
    assert code == 2
    assert reason in captured.err
    assert captured.out == "", "跑不起来的一轮不该印报告，更不该印结论"


def test_a_dropped_record_is_a_failure_not_a_cannot_run(tmp_path: Path, tool, capsys) -> None:
    """少一条记录 = **核对不通过**（退出 1）。这是判据真正在工作的那一条：
    `ParityCheck` 的 `unexplained_total` 必须亮，且样本要指出是哪个文件。

    ⚠ 反过来的一步也在这里：**多**一条记录（比如记录流里有一条盘上不存在的文件）
    会以"读不到属性"记进 `errors`，**不会**进 `mft_only`——把它当成差异去查是白查。
    """
    root, ignore = tree(tmp_path)
    records = [r for r in tool.records_of_tree(root) if r.name != "b.txt"]

    code = tool.main([str(root), "--ignore-file", str(ignore)],
                     mft_source=source_of(records))
    captured = capsys.readouterr()
    assert code == 1
    assert "结论          不通过" in captured.out
    assert "无法解释（仅有 scandir 且 FRN 对不上）" in captured.out
    assert "b.txt" in captured.out


def test_an_empty_run_is_not_a_pass(tmp_path: Path, tool, capsys) -> None:
    """**假通过**：两侧都空时 `ParityCheck.ok` 是 `True`。若就这么退出 0，一次什么都
    没比的运行与一次真正的验收长得一模一样——这正是本项目最怕的那种失败。
    """
    root = tmp_path / "空树"
    root.mkdir()
    ignore = tmp_path / "ignore.txt"
    ignore.write_text(IGNORE, encoding="utf-8")

    code = tool.main([str(root), "--ignore-file", str(ignore)],
                     mft_source=tool.tree_source(root))
    captured = capsys.readouterr()
    assert code == 2
    assert "不能作为证据" in captured.out
    assert "结论          通过" not in captured.out


def test_a_second_name_on_a_hard_link_is_not_a_difference(tmp_path: Path, tool, capsys) -> None:
    """**判据本身**：真硬链接（`os.link`）在 MFT 里是一条记录、在 scandir 里是两个
    目录项，而这一轮必须**通过**——`FRN` 折叠生效了才算。

    判定逻辑的单测在 `test_scanner_parity.py`（更细）；这里钉的是脚本出口：
    同样的情形从 `main()` 走一趟，退出码必须是 0，且计数要说出"这是硬链接"。
    """
    root, ignore = tree(tmp_path)
    os.link(root / "a.txt", root / "另一个名字.txt")

    code = tool.main([str(root), "--ignore-file", str(ignore)],
                     mft_source=tool.tree_source(root))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "结论          通过" in captured.out
    assert "按 FRN 判为硬链接 1" in captured.out
    # 问 FRN 的次数与"差异有多少"成正比，不与条目数成正比（parity.py 的省钱之处）
    assert "问 FRN         1 次" in captured.out


def test_unexpected_errors_get_their_own_exit_code(tmp_path: Path, tool, monkeypatch, capsys):
    """未预期异常退出 3：**与"跑不起来"（2）和"不通过"（1）都不重号**，栈回溯进
    stderr。否则一次编程错误会被读成一次核对结论。
    """
    root, ignore = tree(tmp_path)

    def boom(config, *, source=None):
        raise RuntimeError("假装这里有个 bug")

    monkeypatch.setattr(tool, "MftScanner", boom)
    code = tool.main([str(root), "--ignore-file", str(ignore)],
                     mft_source=tool.tree_source(root))
    captured = capsys.readouterr()
    assert code == 3
    assert "RuntimeError: 假装这里有个 bug" in captured.err
