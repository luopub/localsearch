"""F1-03：日志。测试落点。

这个文件里几条测试是针对**已经踩过的坑**写的，不是凑覆盖：

- `test_setup_is_idempotent_no_double_lines`：handler 会累加，重复初始化后每条
  记录写两遍。症状是"日志里每行都有两份"，第一眼不容易看见。
- `test_file_handler_flushes_immediately`：`tools/probe_mft.py` 在 P0 真的踩过——
  包装 `sys.stdout` 之后 `-u` 就失效了，跑 62 秒、终端有输出、**日志文件 0 字节**。
- `test_unwritable_log_dir_degrades_instead_of_crashing`：日志建不起来不该让
  搜索本身用不了。
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pytest

from localsearch.core.logging import (
    DEFAULT_MAX_BYTES,
    LOG_FILE_NAME,
    install_warning_filters,
    reset_logging,
    resolve_level,
    setup_logging,
)


@pytest.fixture(autouse=True)
def clean_root_logger():
    """每个测试前后都把本模块装的 handler 摘干净。

    不摘的话，根 logger 上的 handler 会跨测试累积：前一个测试往
    `tmp_path` 里写日志，目录被删后下一个测试的 `logging.info` 就会去写一个
    不存在的路径——**而失败会出现在不相干的测试里**。
    """
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "lsearch-data"


def log_text(data_dir: Path) -> str:
    return (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")


def test_creates_log_dir_and_file(data_dir: Path) -> None:
    result = setup_logging(data_dir, to_console=False)
    assert result.file_ok and result.log_file == data_dir / "logs" / LOG_FILE_NAME
    assert result.log_file.is_file(), "日志文件应当立刻就存在（delay=False）"

    logging.getLogger("localsearch.test").info("索引开始")
    text = log_text(data_dir)
    assert "索引开始" in text
    assert "INFO" in text
    assert "localsearch.test" in text


def test_file_handler_flushes_immediately(data_dir: Path) -> None:
    """不 shutdown、不 close，写完立刻就能读到——这是 P0 踩过的坑。"""
    setup_logging(data_dir, to_console=False)
    logging.getLogger("localsearch.test").warning("中断前最后一行")
    assert "中断前最后一行" in log_text(data_dir)


def test_setup_is_idempotent_no_double_lines(data_dir: Path) -> None:
    setup_logging(data_dir, to_console=False)
    setup_logging(data_dir, to_console=False)
    logging.getLogger("localsearch.test").info("只该出现一次")
    assert log_text(data_dir).count("只该出现一次") == 1


def test_level_filters_file_output(data_dir: Path) -> None:
    setup_logging(data_dir, level="WARNING", to_console=False)
    log = logging.getLogger("localsearch.test")
    log.info("不该出现")
    log.warning("该出现")
    text = log_text(data_dir)
    assert "不该出现" not in text
    assert "该出现" in text


def test_level_is_case_insensitive(data_dir: Path) -> None:
    assert setup_logging(data_dir, level="debug", to_console=False).level == "DEBUG"


def test_console_output_goes_to_injected_stream(data_dir: Path) -> None:
    import io

    stream = io.StringIO()
    setup_logging(data_dir, to_console=True, console_stream=stream)
    logging.getLogger("localsearch.test").error("磁盘满了")
    assert "磁盘满了" in stream.getvalue()
    assert "磁盘满了" in log_text(data_dir), "同一个 logger 应当同时写文件和终端"


def test_rotation_produces_backups(data_dir: Path) -> None:
    """轮转是这套日志的**唯一**清理机制，坏了就是磁盘无声涨满。"""
    setup_logging(data_dir, to_console=False, max_bytes=1, backup_count=2)
    log = logging.getLogger("localsearch.test")
    for i in range(5):
        log.info("第 %d 条", i)
    files = sorted(p.name for p in (data_dir / "logs").iterdir())
    assert LOG_FILE_NAME in files
    assert any(name.endswith(".1") for name in files), f"没有产生备份：{files}"
    assert len(files) <= 3, f"backupCount=2 却留下了 {len(files)} 个文件：{files}"


def test_no_rotation_below_threshold(data_dir: Path) -> None:
    setup_logging(data_dir, to_console=False)
    logging.getLogger("localsearch.test").info("一条短记录")
    files = sorted(p.name for p in (data_dir / "logs").iterdir())
    assert files == [LOG_FILE_NAME], f"没超阈值不该轮转：{files}"


def test_default_threshold_matches_task_launcher() -> None:
    """阈值与 scripts/windows/run-index.cmd 是同一件事，不能各写一个数。"""
    assert DEFAULT_MAX_BYTES == 5 * 1024 * 1024
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "windows" / "run-index.cmd"
    if launcher.is_file():
        text = launcher.read_text(encoding="utf-8", errors="replace")
        assert "5242880" in text, (
            "run-index.cmd 的轮转阈值变了，但没有同步 core/logging.py 的 "
            "DEFAULT_MAX_BYTES —— 两边不一致会出现'脚本以为轮转了、进程还在追'"
        )


# ==============================================================================
# 失败与降级
# ==============================================================================


def test_unwritable_log_dir_degrades_instead_of_crashing(tmp_path: Path) -> None:
    """日志目录建不起来（占位的同名文件、磁盘满）时降级为只写终端。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
    result = setup_logging(blocker, to_console=False)
    assert result.file_ok is False
    assert result.log_file is None
    assert result.note, "降级了却不说明原因，等于把问题藏起来"
    # 降级后仍能用：不抛异常，就是这条测试要的全部
    logging.getLogger("localsearch.test").info("照样能记")


def test_no_file_when_disabled(data_dir: Path) -> None:
    result = setup_logging(data_dir, to_file=False, to_console=False)
    assert result.file_ok is False
    assert not (data_dir / "logs").exists(), "明确关掉了就不该建目录"


def test_describe_mentions_the_actual_destination(data_dir: Path) -> None:
    result = setup_logging(data_dir, to_console=False)
    assert str(data_dir) in result.describe()


# ==============================================================================
# 已知噪音（jieba 在 py3.12 下的 SyntaxWarning）
# ==============================================================================


def test_jieba_syntax_warning_is_filtered() -> None:
    install_warning_filters()
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(
            "invalid escape sequence '\\d'",
            SyntaxWarning,
            "jieba/posseg/__init__.py",
            12,
            module="jieba.posseg",
        )
    assert caught == [], "jieba 的 SyntaxWarning 应当被压掉"


def test_other_warnings_still_surface() -> None:
    """压噪音不能顺手把真问题一起压掉——这是这几行最容易写错的地方。"""
    install_warning_filters()
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(
            "invalid escape sequence '\\d'",
            SyntaxWarning,
            "localsearch/ingest/filters.py",
            9,
            module="localsearch.ingest.filters",
        )
    assert len(caught) == 1


def test_setup_logging_installs_the_filter(data_dir: Path) -> None:
    setup_logging(data_dir, to_console=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(
            "invalid escape sequence", SyntaxWarning, "jieba/_compat.py", 1, module="jieba"
        )
    assert caught == []


# ==============================================================================
# 级别解析
# ==============================================================================


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("Warning", logging.WARNING),
        (logging.ERROR, logging.ERROR),
    ],
)
def test_resolve_level_accepts(given, expected) -> None:
    assert resolve_level(given) == expected


def test_resolve_level_rejects_garbage() -> None:
    """认不出的级别要报错。静默按 INFO 处理，会让 `--log-level=verbose` 看起来生效了。"""
    with pytest.raises(ValueError, match="不认识的日志级别"):
        resolve_level("verbose")


def ours() -> list[logging.Handler]:
    """根 logger 上**由本模块装的** handler。

    不能直接断言 `logging.getLogger().handlers == []`：pytest 自己也会往根 logger
    上挂 handler（`_LiveLoggingNullHandler` 之类），那是它的，不该由我们清掉。
    """
    from localsearch.core.logging import _HANDLER_MARK

    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_MARK, False)]


def test_setup_installs_handlers(data_dir: Path) -> None:
    setup_logging(data_dir, to_console=False)
    assert len(ours()) == 1


def test_reset_logging_removes_our_handlers(data_dir: Path) -> None:
    setup_logging(data_dir, to_console=False)
    setup_logging(data_dir, to_console=True)
    assert len(ours()) == 2
    reset_logging()
    assert ours() == [], "reset 之后不该还留着我们装的 handler"
