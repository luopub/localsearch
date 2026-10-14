"""子命令共用的上下文对象（P1 / `F1-04`）。

单独一个模块，不是为了好看，是为了**避免循环导入**：`cli/main.py` 要 import 各个
子命令模块去注册它们，子命令又要 import 这个类型来做标注。放在这里，两边都只指向它。

它承载的是"全局选项解析完之后的结果"——配置、配置来自哪个文件、日志落点。
子命令**不要**自己再调 `load_config()`：那会出现"上游按 A 文件跑、下游按 B 文件跑"
这类查起来很费劲的分叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from localsearch.core.config import Config
from localsearch.core.logging import LoggingSetup


@dataclass
class CliState:
    #: 生效的配置（已合并、已校验）
    config: Config
    #: 生效配置文件的路径；`None` 表示没找到任何配置文件，用的是代码内置默认值
    config_file: Path | None
    #: 本次运行的日志落点（`doctor` / `config` 这类只读命令不写文件，此时 file_ok=False）
    log: LoggingSetup
    #: **进程启动时**的环境变量快照，取自 `bootstrap_env` 之前。
    #:
    #: 必须存这一份：`bootstrap_env` 是往 `os.environ` 里真的写值的（`setdefault`），
    #: 它跑完之后谁也没法从 `os.environ` 分辨"这个值是系统给的"还是"我们刚填的兜底值"。
    #: 而 `doctor` 要报告的恰恰是这件事——它曾据此把 `TORCH_HOME` 报成"系统已设"，
    #: 实际上系统里根本没这个变量，是我们自己刚写进去的。
    env_at_start: dict[str, str]
