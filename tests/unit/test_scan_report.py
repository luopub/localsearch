"""`tools/scan_report.py` 的**行为**测试（不是它的排版）。关联验收项：无。

这个脚本是 `F2-01` / `F2-06` 取实测证据的入口，它本身不在被测模块里——跑它才是它的
用途，所以这里只钉两件**已经各自咬过一次**的事：

1. **在本机默认控制台下跑得起来。** 2026-10-15 01:24:49 实测：账目那行里的减号是
   U+2212，GBK（代码页 936）编不出来，`print` 抛 `UnicodeEncodeError`——整轮扫描的
   输出**一条都没留下来**，而且栈回溯只在最后一行，前面几十行已经打印的账目也没了。
   这里用 `PYTHONIOENCODING=gbk` 起子进程，退出码必须是 0。
   （本机 `uv run` 不设 `PYTHONUTF8`，所以 GBK 就是默认值——这个测试跑的就是默认情形。）
2. **`--json` 是记录用的，分桶必须对。** 报告要引用它，所以它得能被解析，且
   「规则之外」的原因（reparse、`data_dir` 硬剔除、`max_file_mb` 超限）
   **不能**被记到某条规则头上——那会让 `--dry-run` 报出"某条规则排掉了 3 万条"。
   ⚠ 目录级标记原本也在这一栏里，`F2-06` 起单独成栏（`目录标记`，`F2-05` 遗留第 8 条）：
   分桶规则只有一份（`ingest/report.py`），所以改的是那一处，本文件的断言跟着改。
   分桶本身的逐条判据在 `tests/unit/test_cli_index_dryrun.py` 里，这里只钉**脚本出口**。

用真子进程而不是把 `main()` import 进来调：要测的正是"在终端里跑一条命令"这件事，
而 `PYTHONIOENCODING` 只有子进程能干净地设。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "scan_report.py"

#: 规则文件：一行中文注释（GBK 认得的汉字）+ 一条真规则，行号是给断言用的。
IGNORE = "# 中文注释：这一行不进规则表\n*.log\n"


def tree(root: Path) -> Path:
    """一棵同时踩到"规则排除"与"规则之外排除"的小树。"""
    (root / "日志").mkdir(parents=True)
    (root / "日志" / "a.log").write_text("x", encoding="utf-8")
    (root / "日志" / "b.txt").write_text("y", encoding="utf-8")
    (root / "标记目录").mkdir()
    (root / "标记目录" / "c.txt").write_text("z", encoding="utf-8")
    (root / "标记目录" / ".lsearch-noindex").write_text("", encoding="utf-8")
    ignore = root / "ignore.txt"
    ignore.write_text(IGNORE, encoding="utf-8")
    return ignore


def run_tool(*args: str, encoding: str = "gbk") -> subprocess.CompletedProcess[str]:
    """按指定编码起一条真进程。编码即 `PYTHONIOENCODING`，也就是 stdout 的编码。"""
    env = dict(os.environ, PYTHONIOENCODING=encoding)
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding=encoding,
        env=env,
    )


@pytest.mark.parametrize("encoding", ["gbk", "utf-8"])
def test_it_runs_under_the_console_encoding(tmp_path: Path, encoding: str) -> None:
    """两种控制台编码下都要跑通：`gbk` 是本机默认值，`utf-8` 是显式设过的情形。

    参数化而不是只测 gbk，是因为"为了修 gbk 把 utf-8 弄坏"是完全可能的修法——
    比如把汉字换成 ASCII 替代物，那会让输出变得没法读。
    """
    ignore = tree(tmp_path)
    proc = run_tool(str(tmp_path), "--ignore-file", str(ignore), encoding=encoding)
    assert proc.returncode == 0, proc.stderr
    assert "扫描根" in proc.stdout
    assert ".lsearch-noindex" in proc.stdout, "标记的理由要能在账目里看见"
    assert "中文注释" not in proc.stdout  # 注释不是规则


def test_json_puts_the_directory_marker_in_its_own_column(tmp_path: Path) -> None:
    """标记剪掉的目录写进「目录标记」（单独一栏），`*.log` 写进「规则」并带行号。

    栏位的区别就是 `bucket()` 的全部意义：写错栏位，报表的数字就没法用。
    标记**不再**落在「规则之外」里（`F2-05` 遗留第 8 条），也不再与 reparse、
    `data_dir` 硬剔除、`max_file_mb` 超限共用一个数字。
    """
    ignore = tree(tmp_path)
    proc = run_tool(str(tmp_path), "--ignore-file", str(ignore), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)

    skipped = payload["skipped"]
    # 标记这一栏只收 `base.marker_reason()` 那一句，所以键是**整句**，不再按前缀截断
    # （按前缀截断是「规则之外」那一栏的事：那里的错误消息里带路径，见 OTHER_PREFIX）。
    marker_keys = [k for k in skipped if k.startswith("目录标记|—|目录里有 .lsearch-noindex")]
    assert len(marker_keys) == 1, sorted(skipped)
    assert skipped[marker_keys[0]]["dirs"] == 1
    assert not [k for k in skipped if k.startswith("规则之外|—|目录里有")], (
        "标记又回到「规则之外」那一栏了：这正是 F2-05 遗留第 8 条要修掉的那件事"
    )

    rule_key = f"规则|skip 第 2 行|*.log"
    assert rule_key in skipped, sorted(skipped)
    assert skipped[rule_key]["files"] == 1

    # 标记目录里的 c.txt 一条都不该进任何一栏：目录被剪掉 = 子树没被下潜
    assert not [k for k in skipped if k.endswith("c.txt")]
    assert payload["stats"]["dirs_pruned"] == 1
    assert payload["stats"]["files_skipped"] == 1
