"""F1-02：配置加载与校验。关联验收项 **A7**。

两件事分开测，理由不同：

- **加载**：优先级（显式路径 > `LSEARCH_CONFIG` > `config/config.toml` > 模板 >
  代码默认值）、覆盖参数不落盘、坏 TOML 报错而不是回退；
- **校验**：未知键、类型不符、取值越界都要**报错**。这几条不是洁癖——
  这个文件决定"什么文件会被索引"，静默忽略一个拼错的键，得到的症状是
  "规则没生效"，而用户无从判断到底是规则写错了还是工具没读。

`config/default.toml`（入库模板）与代码默认值必须逐字段相等，靠两条测试维持：
一条比**值**（`test_template_matches_code_defaults`），一条比**键的集合**
（`test_template_declares_every_key`）——后者能抓到"模板漏了一个键但它的默认值
恰好也对"这种前者看不出来的情况。
"""

from __future__ import annotations

import dataclasses
import os
import textwrap
import tomllib
from pathlib import Path

import pytest

from localsearch.core import config as cfg_mod
from localsearch.core.config import (
    TEMPLATE_PATH,
    Config,
    ConfigError,
    bootstrap_env,
    config_source_kind,
    effective_env,
    load_config,
    resolve_config_file,
)

#: 最小可用配置：只给 [paths]，其余小节全部取默认值。
MINIMAL = textwrap.dedent(
    """
    [paths]
    data_dir = "D:/tmp-data"
    roots = ["D:/work"]
    """
)


def write_toml(tmp_path: Path, body: str, name: str = "test-config.toml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def toml_paths(data_dir: str = "D:/tmp-data", roots: str = '["D:/work"]') -> str:
    return f'[paths]\ndata_dir = "{data_dir}"\nroots = {roots}\n'


@pytest.fixture
def no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """把自动发现关掉，好让"没有配置文件时用代码默认值"这件事可测。

    本机上 `config/default.toml` 是存在的，不隔离的话测不出回退路径。
    """
    monkeypatch.setattr(cfg_mod, "LOCAL_CONFIG_PATH", Path("/nonexistent/config.toml"))
    monkeypatch.setattr(cfg_mod, "TEMPLATE_PATH", Path("/nonexistent/default.toml"))
    monkeypatch.delenv(cfg_mod.ENV_CONFIG_PATH, raising=False)


# ==============================================================================
# 模板与代码默认值的一致性
# ==============================================================================


def test_template_matches_code_defaults() -> None:
    """模板与 dataclass 默认值必须逐字段相等。"""
    assert load_config(TEMPLATE_PATH) == Config()


def test_template_declares_every_key() -> None:
    """模板里必须**显式写出**每一个键，不能靠"没写就取默认值"来对齐。

    只比值的测试抓不到"模板少写了一个键"——少写的那个键会安静地取默认值，
    于是两边仍然相等，而读模板的人以为那个键不存在。
    """
    raw = tomllib.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    expected = {
        section: {f.name for f in dataclasses.fields(cls)}
        for section, cls in cfg_mod._SECTION_TYPES.items()
    }
    actual = {section: set(values) for section, values in raw.items()}
    missing = {s: sorted(keys - actual.get(s, set())) for s, keys in expected.items()}
    extra = {s: sorted(actual.get(s, set()) - expected.get(s, set())) for s in actual}
    assert not any(missing.values()) and not any(extra.values()), (
        f"模板与代码默认值的键集合不一致：\n"
        f"  只在代码里（模板漏写）：{ {k: v for k, v in missing.items() if v} }\n"
        f"  只在模板里（代码没这个键）：{ {k: v for k, v in extra.items() if v} }"
    )


def test_defaults_when_no_config_file_exists(no_ambient_config: None) -> None:
    assert resolve_config_file() is None
    assert load_config() == Config()
    assert "内置默认值" in config_source_kind(None)


# ==============================================================================
# 加载：优先级、覆盖、报错
# ==============================================================================


def test_explicit_path_wins(tmp_path: Path) -> None:
    path = write_toml(tmp_path, MINIMAL + "[chunk]\ntarget_tokens = 1024\n")
    assert load_config(path).chunk.target_tokens == 1024


def test_env_var_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_toml(tmp_path, MINIMAL + "[search]\ntop_k = 5\n")
    monkeypatch.setenv(cfg_mod.ENV_CONFIG_PATH, str(path))
    assert resolve_config_file() == path
    assert load_config().search.top_k == 5


def test_env_var_pointing_nowhere_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指向的文件不存在时**报错**，不静默回退到模板。

    静默回退会让"我明明指定了另一个配置"变成一个查不出来的问题：
    跑出来的结果看着正常，只是来自另一个文件。
    """
    monkeypatch.setenv(cfg_mod.ENV_CONFIG_PATH, str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="不存在"):
        resolve_config_file()


def test_explicit_path_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.toml")


def test_local_config_beats_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = write_toml(tmp_path, MINIMAL + '[cli]\nlanguage = "en"\n', "config.toml")
    monkeypatch.setattr(cfg_mod, "LOCAL_CONFIG_PATH", local)
    assert resolve_config_file() == local
    assert load_config().cli.language == "en"


def test_broken_toml_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, "[paths]\ndata_dir = \n")
    with pytest.raises(ConfigError, match="不是合法的 TOML"):
        load_config(path)


def test_partial_file_keeps_other_defaults(tmp_path: Path) -> None:
    """只写一个键，其余取默认值——这是配置文件的正常用法。"""
    loaded = load_config(write_toml(tmp_path, MINIMAL))
    assert loaded.paths.data_dir == "D:/tmp-data"
    assert loaded.chunk.target_tokens == Config().chunk.target_tokens
    assert loaded.embed.dim == 768


def test_overrides_apply_but_do_not_touch_the_file(tmp_path: Path) -> None:
    """命令行覆盖临时生效，不写回文件（计划 §4.6：不写回，避免两处状态不一致）。"""
    path = write_toml(tmp_path, MINIMAL)
    before = path.read_bytes()
    loaded = load_config(path, overrides={"scan": {"max_file_mb": 256}})
    assert loaded.scan.max_file_mb == 256
    assert path.read_bytes() == before


def test_source_kind_labels(tmp_path: Path) -> None:
    assert "本机配置" in config_source_kind(TEMPLATE_PATH.with_name("config.toml"))
    assert "模板" in config_source_kind(TEMPLATE_PATH)
    assert "显式指定" in config_source_kind(tmp_path / "x.toml")


# ==============================================================================
# 校验：未知键、类型、取值
# ==============================================================================


def test_unknown_section_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, MINIMAL + '[scann]\nbackend = "auto"\n')
    with pytest.raises(ConfigError, match="多余的小节"):
        load_config(path)


def test_unknown_key_raises(tmp_path: Path) -> None:
    """拼错的键必须炸出来，而不是安静地按默认值跑。"""
    path = write_toml(tmp_path, MINIMAL + "[scan]\nmxa_file_mb = 64\n")
    with pytest.raises(ConfigError, match="未知的键"):
        load_config(path)


@pytest.mark.parametrize(
    "body",
    [
        '[scan]\nmax_file_mb = "64"\n',
        "[perf]\nworkers = true\n",
        '[search]\nrrf_k = "60"\n',
    ],
)
def test_type_mismatch_raises(tmp_path: Path, body: str) -> None:
    path = write_toml(tmp_path, MINIMAL + body)
    with pytest.raises(ConfigError, match="类型不对"):
        load_config(path)


def test_bool_not_accepted_as_paths_value(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        '[paths]\ndata_dir = "D:/tmp-data"\nroots = ["D:/work"]\nall_drives = "yes"\n',
    )
    with pytest.raises(ConfigError, match="类型不对"):
        load_config(path)


def test_bool_is_not_accepted_as_int(tmp_path: Path) -> None:
    """`workers = true` 在 Python 里能当 1 用，但那是配置写错了，不是"用户想要 1"。"""
    with pytest.raises(ConfigError, match="类型不对"):
        load_config(write_toml(tmp_path, MINIMAL + "[perf]\nworkers = false\n"))


@pytest.mark.parametrize("dtype", ["float16", "fp16", "half"])
def test_float16_is_rejected(tmp_path: Path, dtype: str) -> None:
    """R6：fp16 产出 NaN。消息里要带原因，别让人以为是随便挑的合法值。"""
    path = write_toml(tmp_path, MINIMAL + f'[embed]\ndtype = "{dtype}"\n')
    with pytest.raises(ConfigError, match="float16"):
        load_config(path)


def test_bfloat16_is_allowed_but_documented_as_slow(tmp_path: Path) -> None:
    """bfloat16 合法（不是错），只是本机慢 6.21 倍——模板里已注明。"""
    path = write_toml(tmp_path, MINIMAL + '[embed]\ndtype = "bfloat16"\n')
    assert load_config(path).embed.dtype == "bfloat16"


@pytest.mark.parametrize("dim", [768, 512, 256, 128])
def test_mrl_dims_accepted(tmp_path: Path, dim: int) -> None:
    path = write_toml(tmp_path, MINIMAL + f"[embed]\ndim = {dim}\n")
    assert load_config(path).embed.dim == dim


def test_dim_outside_mrl_set_rejected(tmp_path: Path) -> None:
    path = write_toml(tmp_path, MINIMAL + "[embed]\ndim = 300\n")
    with pytest.raises(ConfigError, match="dim"):
        load_config(path)


@pytest.mark.parametrize("value", ["1.0", "1.5", "-0.1"])
def test_overlap_ratio_out_of_range(tmp_path: Path, value: str) -> None:
    path = write_toml(tmp_path, MINIMAL + f"[chunk]\noverlap_ratio = {value}\n")
    with pytest.raises(ConfigError, match="overlap_ratio"):
        load_config(path)


def test_zero_workers_rejected(tmp_path: Path) -> None:
    path = write_toml(tmp_path, MINIMAL + "[perf]\nworkers = 0\n")
    with pytest.raises(ConfigError, match="workers"):
        load_config(path)


def test_unknown_enum_value_rejected(tmp_path: Path) -> None:
    path = write_toml(tmp_path, MINIMAL + '[search]\nmode = "fuzzy"\n')
    with pytest.raises(ConfigError, match="不是合法取值"):
        load_config(path)


def test_empty_roots_requires_all_drives(tmp_path: Path) -> None:
    """roots 空 + all_drives false = 什么都不索引，这是配置错误不是"谨慎"。"""
    bad = write_toml(tmp_path, toml_paths(roots="[]"))
    with pytest.raises(ConfigError, match="roots 为空"):
        load_config(bad)

    ok = write_toml(
        tmp_path,
        '[paths]\ndata_dir = "D:/tmp-data"\nroots = []\nall_drives = true\n',
        "ok.toml",
    )
    assert load_config(ok).paths.all_drives is True


def test_root_typo_rejected(tmp_path: Path) -> None:
    """既不是盘符路径也不是 UNC 的写法要报错——那是笔误，不是"高级用法"。"""
    bad = write_toml(tmp_path, toml_paths(roots='["/mnt/docs"]'))
    with pytest.raises(ConfigError, match="roots"):
        load_config(bad)


def test_windows_style_root_accepted(tmp_path: Path) -> None:
    ok = write_toml(tmp_path, toml_paths(roots='["D:\\\\work", "C:/Users/luopu/Documents"]'))
    assert load_config(ok).paths.roots == ("D:\\work", "C:/Users/luopu/Documents")


def test_unc_root_accepted(tmp_path: Path) -> None:
    ok = write_toml(tmp_path, toml_paths(roots='["\\\\\\\\server\\\\share"]'))
    assert load_config(ok).paths.roots == ("\\\\server\\share",)


def test_nonexistent_root_is_not_a_config_error(tmp_path: Path) -> None:
    """盘没插、网络盘没挂载是**运行时**事实，不是配置错误——存在性由 doctor 报。"""
    ok = write_toml(tmp_path, toml_paths(roots='["Z:/not-mounted"]'))
    assert load_config(ok).paths.roots == ("Z:/not-mounted",)


def test_data_dir_path_helpers() -> None:
    c = Config()
    assert c.data_dir_path == Path("D:/localsearch-data")
    assert c.log_dir_path() == Path("D:/localsearch-data/logs")


# ==============================================================================
# 环境变量兜底（计划 §3.3）
# ==============================================================================


def test_bootstrap_env_fills_missing_values() -> None:
    env: dict[str, str] = {}
    applied = bootstrap_env(Path("D:/localsearch-data"), environ=env)
    assert env["HF_HOME"] == "D:/localsearch-data/models/hf"
    assert env["TORCH_HOME"] == "D:/localsearch-data/models/torch"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert all(v.is_fallback for v in applied)


def test_bootstrap_env_never_overwrites_existing() -> None:
    """本机已有用户级 `HF_HOME`（14GB 模型）。覆盖它会在磁盘上多出一份权重副本。"""
    env = {"HF_HOME": "C:/Users/luopu/.cache/huggingface"}
    applied = bootstrap_env(Path("D:/localsearch-data"), environ=env)
    assert env["HF_HOME"] == "C:/Users/luopu/.cache/huggingface"
    hf = next(v for v in applied if v.name == "HF_HOME")
    assert hf.source == "系统已设" and not hf.is_fallback


def test_bootstrap_env_reports_source_per_variable() -> None:
    env = {"HF_HOME": "C:/existing"}
    applied = {v.name: v for v in bootstrap_env(Path("D:/d"), environ=env)}
    assert applied["HF_HOME"].source == "系统已设"
    assert applied["TORCH_HOME"].source == "兜底值"


def test_bootstrap_env_does_not_touch_real_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """传了 environ 就必须只动那一个字典，不碰进程环境。"""
    monkeypatch.delenv("HF_HOME", raising=False)
    bootstrap_env(Path("D:/d"), environ={})
    assert "HF_HOME" not in os.environ


def test_effective_env_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TORCH_HOME", raising=False)
    before = dict(os.environ)
    reported = {v.name: v for v in effective_env(Path("D:/d"))}
    assert reported["TORCH_HOME"].value == "D:/d/models/torch"
    assert dict(os.environ) == before, "effective_env 不该改动进程环境"


def test_effective_env_respects_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HOME", "C:/existing")
    reported = {v.name: v for v in effective_env(Path("D:/d"))}
    assert reported["HF_HOME"].value == "C:/existing"
    assert reported["HF_HOME"].source == "系统已设"
