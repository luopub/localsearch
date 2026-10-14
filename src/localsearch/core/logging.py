r"""日志（P1 / `F1-03`）。测试落点 `tests/unit/test_logging.py`。

------------------------------------------------------------------------------
三条约束，都是被实际情况逼出来的
------------------------------------------------------------------------------

**1. 轮转阈值必须与 `scripts/windows/run-index.cmd` 的 5 MiB 一致。**
   日志同时有两个写入者：Python 进程写 `data_dir/logs/lsearch.log`，计划任务的
   启动器脚本写它自己的日志并做轮转。两边用不同的阈值，会出现"脚本以为轮转了、
   进程还在往里追"这种谁也说不清的状态。所以阈值是共享常量，不是各处随手写一个数。

**2. 立刻 flush。**
   P0 在 `tools/probe_mft.py` 里踩过一次：包装 `sys.stdout` 之后 `-u` 就管不着它了，
   探针跑了 62 秒、终端上有输出、日志文件 0 字节。**最坏的地方正是"看起来成功了"。**
   这里对文件 handler 显式 `flush()`，宁慢一点。

**3. 已知噪音单独压掉，不靠调高级别。**
   `jieba` 0.42.1 在 py3.12 下导入时会抛若干条 `SyntaxWarning: invalid escape
   sequence`（它的源码用了旧式正则字符串）。功能不受影响，但它会污染 CLI 输出、
   让真的错误淹在里面。压的是**具体的类别 + 具体的模块**，不是把警告全关掉。

**不许把日志目录建失败变成程序崩溃**：`lsearch` 是个搜索工具，磁盘满、日志目录
被别的进程占着，都不该让搜索本身用不了。失败时降级为只输出到终端，并把原因带在
返回值里，由调用方决定要不要提示。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

#: 与 scripts/windows/run-index.cmd 共享的轮转阈值（见文件头第 1 条）
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "lsearch.log"

#: 我们装上去的 handler 都带这个标记，好让重复调用 setup_logging 时能先摘干净
_HANDLER_MARK = "_localsearch_handler"

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class LoggingSetup:
    """`setup_logging` 的结果。调用方拿它决定要不要提示用户。"""

    level: str
    #: 实际写入的日志文件；`to_file=False` 或建目录失败时为 `None`
    log_file: Path | None
    file_ok: bool
    console: bool
    note: str = ""

    def describe(self) -> str:
        if self.file_ok and self.log_file is not None:
            return f"日志：{self.log_file}（级别 {self.level}）"
        if self.console:
            return f"日志：仅终端（级别 {self.level}）{('：' + self.note) if self.note else ''}"
        return f"日志：未启用{('：' + self.note) if self.note else ''}"


def resolve_level(level: str | int) -> int:
    """把 `"info"` / `"INFO"` / `logging.INFO` 都归一成 int。认不出就报错。"""
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    value = logging.getLevelNamesMapping().get(name)
    if not isinstance(value, int):
        raise ValueError(
            f"不认识的日志级别 {level!r}；可用：DEBUG / INFO / WARNING / ERROR / CRITICAL"
        )
    return value


def install_warning_filters() -> None:
    """压掉已知的第三方噪音（见文件头第 3 条）。

    刻意**只匹配 jieba 自己的模块**：`module` 参数是按模块名做正则匹配的，
    写成 `""`（匹配全部）就把用户想看的警告一起吞了。
    """
    warnings.filterwarnings(
        "ignore",
        category=SyntaxWarning,
        module=r"jieba(\..*)?$",
    )


def _strip_our_handlers(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    for handler in list(target.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            target.removeHandler(handler)
            handler.close()


def reset_logging() -> None:
    """摘掉本模块装上去的所有 handler。测试的 teardown 与 CLI 的清理都用它。"""
    _strip_our_handlers()


def setup_logging(
    data_dir: Path,
    *,
    level: str | int = "INFO",
    to_file: bool = True,
    to_console: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    quiet_known_noise: bool = True,
    console_stream: TextIO | None = None,
) -> LoggingSetup:
    """装好根 logger，返回实际落点。

    可重复调用：每次先把上一次装的摘掉。**不这么做的话，第二次调用会让每条记录
    写两遍**——handler 会累加，而症状只是"日志里每行都有两份"，不容易第一眼看见。
    """
    resolved = resolve_level(level)
    name = logging.getLevelName(resolved)

    if quiet_known_noise:
        install_warning_filters()

    root = logging.getLogger()
    _strip_our_handlers(root)
    # 根 logger 自己的级别设成 DEBUG，把过滤交给各 handler——否则
    # `logger.setLevel` 会先拦一道，handler 上的级别就成了摆设。
    root.setLevel(logging.DEBUG)

    console_ok = False
    if to_console:
        stream = console_stream if console_stream is not None else sys.stderr
        handler = logging.StreamHandler(stream)
        handler.setLevel(resolved)
        handler.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
        console_ok = True

    log_file: Path | None = None
    file_ok = False
    note = ""
    if to_file:
        try:
            log_dir = Path(data_dir) / LOG_DIR_NAME
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / LOG_FILE_NAME
            # delay=False 立刻建立文件：日志文件存在与否本身就是"任务跑没跑过"的证据，
            # 延迟创建会让"跑了但没写日志"和"根本没跑"长得一样。
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=False,
            )
            file_handler.setLevel(resolved)
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
            setattr(file_handler, _HANDLER_MARK, True)
            root.addHandler(file_handler)
            file_ok = True
        except OSError as exc:
            # 见文件头最后一段：日志建不起来不该让搜索用不了
            note = f"日志文件不可用（{exc.__class__.__name__}: {exc}）"
            log_file = None

    return LoggingSetup(
        level=name,
        log_file=log_file,
        file_ok=file_ok,
        console=console_ok,
        note=note,
    )


__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_MAX_BYTES",
    "LOG_FILE_NAME",
    "LoggingSetup",
    "install_warning_filters",
    "reset_logging",
    "resolve_level",
    "setup_logging",
]
