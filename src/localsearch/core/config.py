r"""配置加载、校验与环境变量兜底（P1 / `F1-02`）。

（本文件是 raw docstring：里面有 `C:\Users\...` 这类 Windows 路径字面量，
 不写 r 前缀的话 `\U` 会被当成转义序列，直接 SyntaxError。）

测试落点 `tests/unit/test_config.py`，关联验收项 **A7**（`doctor` 报告 `HF_HOME` /
`data_dir` / `TORCH_HOME` 的实际生效值；不产生计划外的持久化落点）。

------------------------------------------------------------------------------
三个刻意的设计决定
------------------------------------------------------------------------------

**1. 默认值写在代码里，`config/default.toml` 是给人看的模板。**
   模板与代码默认值必须逐字段相等，由 `tests/unit/test_config.py` 里的一条测试
   盯着——不允许靠"我记得改了两处"来维持一致。

**2. 未知键一律报错，不静默忽略。**
   这个项目的配置决定"什么文件会被索引"，而写错的键（`mxa_file_mb`）如果被静默
   忽略，症状是"规则没生效"而不是"报错"，属于本仓库反复吃亏的那类静默失败。
   同理，类型不对（`max_file_mb = "64"`）也报错，不做隐式转换。

**3. `bootstrap_env()` 用 `setdefault` 语义，绝不覆盖系统已有的值。**
   本机已有用户级 `HF_HOME`（`C:\Users\luopu\.cache\huggingface`，已存 14GB 模型）。
   程序擅自改指会在磁盘上多分出一套权重副本，见计划 §3.3。缺失才补兜底值，
   并且把"这个值是我们补的、还是系统本来就有的"如实报出来（`doctor` 要用）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

# `src/localsearch/core/config.py` → parents: [core, localsearch, src, 仓库根]
PROJECT_ROOT = Path(__file__).resolve().parents[3]

CONFIG_DIR = PROJECT_ROOT / "config"
TEMPLATE_PATH = CONFIG_DIR / "default.toml"
LOCAL_CONFIG_PATH = CONFIG_DIR / "config.toml"

#: 显式指定配置文件的环境变量。命令行 `--config`（F1-04 起）优先于它。
ENV_CONFIG_PATH = "LSEARCH_CONFIG"


class ConfigError(Exception):
    """配置非法。消息里必须写清楚**哪个键、期望什么、实际拿到什么**。"""


# ==============================================================================
# 配置的形态：每个 TOML 小节一个冻结 dataclass
# ==============================================================================
# 字段顺序与 `config/default.toml` 一致，便于逐行对照。


@dataclass(frozen=True)
class PathsCfg:
    data_dir: str = "D:/localsearch-data"
    roots: tuple[str, ...] = ("D:/work",)
    all_drives: bool = False


@dataclass(frozen=True)
class ScanCfg:
    backend: str = "auto"
    follow_symlinks: bool = False
    max_file_mb: int = 64
    ignore_file: str = ".lsearchignore"
    respect_gitignore: bool = False
    dedup_by_content: bool = True


@dataclass(frozen=True)
class ExtractCfg:
    text_code: bool = True
    office: bool = True
    web_mail: bool = True
    media_meta: bool = True
    email_attachment_depth: int = 1


@dataclass(frozen=True)
class ChunkCfg:
    target_tokens: int = 512
    overlap_ratio: float = 0.15
    max_chunks_per_doc: int = 200


@dataclass(frozen=True)
class EmbedCfg:
    backend: str = "onnx"
    model_id: str = "google/embeddinggemma-2"
    dim: int = 768
    dtype: str = "float32"
    quantize: str = "int8"
    #: 一次喂进模型的切片数。**8 是 P0 那两张扫表共同给出的最优**，不是随手定的：
    #: ONNX int8 在批 8 是 553.0 tok/s / 常驻 2.64 GiB，批 16 是 442.8 tok/s / 4.88 GiB
    #: （**更慢且多占 2.2 GiB**，批 32 到 6.11 GiB）；ST float32 在批 8 也比批 16 略快。
    #: 计划 §4.5 的模板原本写 16，是按"一次多喂点更划算"的直觉写的——实测推翻了它，
    #: 而本机真正卡住并行的是内存（A2：W < 2），所以这个数直接决定能开几个 worker。
    batch_size: int = 8
    #: 单条文本喂进模型的最长 token 数。模型自己的推荐上限是 2048；**必须显式设**，
    #: 否则 sentence-transformers 读到的默认值是 1e36（它取的是模型顶层
    #: `max_position_embeddings`，而这份 config 把它藏在 `text_config` 里），
    #: 于是**永不截断**。见 embed/gemma_st.py 与 gemma_onnx.py 的模块文档。
    max_seq: int = 2048
    #: 嵌入用的 CPU 线程数。默认 8 = 本机**物理核**数（P0 的 E6：8 物理 / 16 逻辑）。
    #: 两个后端都用这个数（ST 走 `torch.set_num_threads`，ONNX 走 `intra_op_num_threads`）。
    #: ⚠ 它与 `[perf] workers`（进程数）是**相乘**的：4 个 worker × 8 线程 = 32 个线程
    #: 抢 8 个物理核，A2 的并行加速会因此反过来变成减速。`workers` 缺省是 0（自动，
    #: 本机算出来 1），所以这里保持 8；真要开多个 worker 时一起往下调
    #: （约 物理核 ÷ workers），`batching.py` 会在总线程数超过核数时提醒一句。
    threads: int = 8


@dataclass(frozen=True)
class IndexCfg:
    vector_backend: str = "faiss"
    hnsw_m: int = 32
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64


@dataclass(frozen=True)
class TiersCfg:
    metadata: bool = True
    fulltext: bool = True
    semantic: bool = True


@dataclass(frozen=True)
class SearchCfg:
    mode: str = "hybrid"
    top_k: int = 20
    rrf_k: int = 60
    weight_lexical: float = 1.0
    weight_semantic: float = 1.0
    weight_path: float = 0.5


@dataclass(frozen=True)
class CliCfg:
    language: str = "zh"
    interactive_default: bool = True


@dataclass(frozen=True)
class PerfCfg:
    # 并行嵌入的**进程数**。0 = 自动：由 `embed/batching.py` 按内存算出来，公式是
    # `A2` 重定后的口径 `W = ⌊(可用内存 − 保留额) ÷ 单 worker 实测 RSS⌋`。
    #
    # 这里原本写 4，来由是"cores//2"（计划 §4.5 的模板，而当时把 CPU 记成了 16 核）。
    # P0 实测（PROGRESS.md §3 的 E6）是 **8 物理核 / 16 逻辑核**；而真正卡住并行的
    # 不是核数而是**内存**：单 worker 峰值 RSS 2.01 GiB（`E8`），8 路要 16.09 GiB，
    # 整机只有 15.37 GiB——`A2` 因此重定口径，并明确写了"**W 不按核数取**"。
    #
    # 所以现在写 0：按核数写死的那个 4 是一条**从没被内存检验过**的假设（F5-02 之前
    # 没有任何代码读它），而"按这台机器算"才是这个配置项该有的语义。写一个正数仍然
    # 可以，那是**上限**：实际取 min(你写的, 内存算出来的)，被夹住时会在日志与
    # `EmbedPool.notes` 里说出来，不静默降级。
    workers: int = 0
    commit_every_files: int = 500


@dataclass(frozen=True)
class Config:
    paths: PathsCfg = PathsCfg()
    scan: ScanCfg = ScanCfg()
    extract: ExtractCfg = ExtractCfg()
    chunk: ChunkCfg = ChunkCfg()
    embed: EmbedCfg = EmbedCfg()
    index: IndexCfg = IndexCfg()
    tiers: TiersCfg = TiersCfg()
    search: SearchCfg = SearchCfg()
    cli: CliCfg = CliCfg()
    perf: PerfCfg = PerfCfg()

    @property
    def data_dir_path(self) -> Path:
        return Path(self.paths.data_dir)

    def log_dir_path(self) -> Path:
        return self.data_dir_path / "logs"

    def index_dir_path(self) -> Path:
        """索引库与向量索引所在的那一层（计划 §3.3 的 `index/`）。"""
        return self.data_dir_path / "index"

    @property
    def db_path(self) -> Path:
        """索引库的落点（计划 §3.3：`data_dir/index/lsearch.db`）。

        **它是"库在哪儿"唯一的真相来源**——`lsearch index` 写它、`lsearch search` 与
        将来的 `status` / `explain` 读它。写在别处（比如各自的命令里拼一次）就会
        出现"索引写进 A、检索去 B 里找"这种分叉，而它的症状是"搜什么都没结果"。
        """
        return self.index_dir_path() / "lsearch.db"


# ==============================================================================
# 校验规则：一张表，避免 30 个键散在 30 个 if 里
# ==============================================================================

_ENUM_CHOICES: dict[tuple[str, str], tuple[Any, ...]] = {
    ("scan", "backend"): ("auto", "scandir", "mft"),
    ("embed", "backend"): ("auto", "sentence_transformers", "onnx"),
    # 严禁 float16：R6，且 P0 的 M2 实测确认本机（Zen 2，无 bf16 硬件）
    # 不显式写 float32 会掉进慢 6.21 倍的路径，**且不报错**。
    ("embed", "dtype"): ("float32", "bfloat16"),
    ("embed", "quantize"): ("none", "int8"),
    # 原生 768，MRL 可截断到 512/256/128（D5）
    ("embed", "dim"): (768, 512, 256, 128),
    ("index", "vector_backend"): ("faiss", "hnswlib"),
    ("search", "mode"): ("hybrid", "lexical", "semantic"),
}

_INT_MIN: dict[tuple[str, str], int] = {
    ("scan", "max_file_mb"): 0,
    ("extract", "email_attachment_depth"): 0,
    ("chunk", "target_tokens"): 1,
    ("chunk", "max_chunks_per_doc"): 1,
    ("embed", "batch_size"): 1,
    ("embed", "max_seq"): 1,
    ("embed", "threads"): 1,
    ("index", "hnsw_m"): 1,
    ("index", "hnsw_ef_construction"): 1,
    ("index", "hnsw_ef_search"): 1,
    ("search", "top_k"): 1,
    ("search", "rrf_k"): 1,
    # workers 的下界从 1 改成 0（F5-02）：0 = **自动**（按内存算），负数才是写错了。
    # 别的几个 0 是"不限 / 关闭"的意思（max_file_mb、email_attachment_depth），
    # 这一个 0 是"你自己去算"——语义不同，所以它归 `embed/batching.py` 解释。
    ("perf", "workers"): 0,
    ("perf", "commit_every_files"): 1,
}

#: 半开区间 [lo, hi)
_FLOAT_RANGE: dict[tuple[str, str], tuple[float, float]] = {
    ("chunk", "overlap_ratio"): (0.0, 1.0),
}

#: 下界含，无上界
_FLOAT_MIN: dict[tuple[str, str], float] = {
    ("search", "weight_lexical"): 0.0,
    ("search", "weight_semantic"): 0.0,
    ("search", "weight_path"): 0.0,
}

def _expected_type(section: str, key: str) -> type:
    """从 dataclass 的默认值反推这个键该是什么类型。"""
    cfg = _SECTION_TYPES[section]()
    return type(getattr(cfg, key))


_SECTION_TYPES: dict[str, type] = {
    "paths": PathsCfg,
    "scan": ScanCfg,
    "extract": ExtractCfg,
    "chunk": ChunkCfg,
    "embed": EmbedCfg,
    "index": IndexCfg,
    "tiers": TiersCfg,
    "search": SearchCfg,
    "cli": CliCfg,
    "perf": PerfCfg,
}


def _check_type(section: str, key: str, value: Any) -> None:
    want = _expected_type(section, key)
    if want is bool:
        ok = isinstance(value, bool)
    elif want is int:
        # bool 是 int 的子类，单独排掉：workers = true 不该被当成 1
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif want is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif want is str:
        ok = isinstance(value, str)
    else:  # pragma: no cover - 现无其它类型
        ok = isinstance(value, want)
    if not ok:
        raise ConfigError(
            f"[{section}] {key} 的类型不对：期望 {want.__name__}，实际拿到 "
            f"{type(value).__name__}（{value!r}）。配置不做隐式转换——"
            "写错类型时报错比'悄悄按默认值跑'好，后者会让人以为规则生效了。"
        )


def _validate_scalar(section: str, key: str, value: Any) -> None:
    slot = (section, key)
    if slot in _ENUM_CHOICES:
        allowed = _ENUM_CHOICES[slot]
        if value not in allowed:
            extra = ""
            if slot == ("embed", "dtype"):
                extra = (
                    "。float16 是**硬性禁止**的（R6：fp16 产出 NaN）；"
                    "本机是 Zen 2，没有 bf16 硬件，bfloat16 会让嵌入慢 6.21 倍"
                    "（P0 的 M2 实测），所以这里实际只有 float32 是可用值"
                )
            raise ConfigError(
                f"[{section}] {key} = {value!r} 不是合法取值，"
                f"允许：{', '.join(map(str, allowed))}{extra}"
            )
        return
    if slot in _INT_MIN:
        if value < _INT_MIN[slot]:
            raise ConfigError(
                f"[{section}] {key} = {value} 太小，至少要 {_INT_MIN[slot]}"
            )
        return
    if slot in _FLOAT_RANGE:
        lo, hi = _FLOAT_RANGE[slot]
        if not (lo <= value < hi):
            raise ConfigError(
                f"[{section}] {key} = {value} 超出范围 [{lo}, {hi})"
            )
        return
    if slot in _FLOAT_MIN:
        if value < _FLOAT_MIN[slot]:
            raise ConfigError(f"[{section}] {key} = {value} 不能为负")
        return


def _validate_paths(cfg: PathsCfg) -> None:
    if not cfg.data_dir.strip():
        raise ConfigError("[paths] data_dir 不能为空——索引库、向量与日志都要有落点")
    if not cfg.all_drives:
        if not cfg.roots:
            raise ConfigError(
                "[paths] roots 为空且 all_drives = false：没有任何东西会被索引。"
                "要么填 roots，要么显式把 all_drives 打开"
            )
        for root in cfg.roots:
            if not root.strip():
                raise ConfigError("[paths] roots 里有空字符串")
    for root in cfg.roots:
        # 这里只判"写法像不像一个路径"，**不判存不存在**——盘可能没插、网络盘可能
        # 没挂载，那是运行时事实，不是配置错误。存在性由 doctor 报（A7）。
        if len(root) >= 2 and root[1] == ":" and root[0].isalpha():
            continue
        if root.startswith("\\\\"):
            continue
        raise ConfigError(
            f"[paths] roots 里的 {root!r} 既不是盘符路径（如 D:/work）也不是 UNC 路径"
        )


# ==============================================================================
# 加载
# ==============================================================================


def resolve_config_file(path: str | os.PathLike[str] | None = None) -> Path | None:
    """决定"生效的是哪个配置文件"。优先级从高到低：

    1. 显式传入的 `path`（命令行 `--config`）
    2. 环境变量 `LSEARCH_CONFIG`
    3. `config/config.toml`（本机配置，不入库，由 `lsearch config init` 生成）
    4. `config/default.toml`（入库的模板）

    四者都没有时返回 `None`——此时用代码里的默认值，行为与模板相同。
    显式指定的文件不存在时**报错而不是静默回退**：静默回退会让"我明明指定了
    另一个配置"变成一个查不出来的问题。
    """
    if path is not None:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"指定的配置文件不存在：{p}")
        return p

    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise ConfigError(f"{ENV_CONFIG_PATH} 指向的文件不存在：{p}")
        return p

    for candidate in (LOCAL_CONFIG_PATH, TEMPLATE_PATH):
        if candidate.is_file():
            return candidate
    return None


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> Config:
    """读配置并校验。

    `overrides` 给命令行参数用（如 `index --max-file-mb 128`），它**不写回文件**：
    计划 §4.6 已经定下"`--include/--exclude` 只临时覆盖，不写回"，其余参数同理，
    免得出现"配置有两处状态、哪边生效说不清"。
    """
    config_file = resolve_config_file(path)
    raw: dict[str, Any] = {}
    if config_file is not None:
        try:
            with config_file.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_file} 不是合法的 TOML：{exc}") from exc

    merged = _merge(raw, overrides)
    return _build_config(merged, source=config_file)


def _merge(
    raw: Mapping[str, Any], overrides: Mapping[str, Mapping[str, Any]] | None
) -> dict[str, Any]:
    merged: dict[str, Any] = {section: dict(values) for section, values in raw.items()}
    if overrides:
        for section, values in overrides.items():
            if section not in merged:
                merged[section] = {}
            merged[section].update(values)
    return merged


def _build_config(raw: Mapping[str, Any], *, source: Path | None) -> Config:
    if not isinstance(raw, Mapping):  # pragma: no cover - tomllib 只会给 dict
        raise ConfigError("配置的顶层结构必须是表（[section]）")

    known_sections = set(_SECTION_TYPES)
    unknown = set(raw) - known_sections
    if unknown:
        raise ConfigError(
            f"配置里有多余的小节 {sorted(unknown)}，已知的小节是 "
            f"{sorted(known_sections)}。刻意不做静默忽略——"
            "拼错的键会让规则看起来写了、其实没生效"
        )

    sections: dict[str, Any] = {}
    for section, cls in _SECTION_TYPES.items():
        values = raw.get(section, {})
        if not isinstance(values, Mapping):
            raise ConfigError(f"[{section}] 必须是一个表")
        known_keys = {f.name for f in fields(cls)}
        extra_keys = set(values) - known_keys
        if extra_keys:
            raise ConfigError(
                f"[{section}] 里有未知的键 {sorted(extra_keys)}，"
                f"该小节已知的键是 {sorted(known_keys)}"
            )
        kwargs = dict(values)
        # TOML 的数组 → tuple，好让 Config 可哈希、可比较
        if section == "paths" and "roots" in kwargs and isinstance(kwargs["roots"], list):
            kwargs["roots"] = tuple(kwargs["roots"])
        for key, value in kwargs.items():
            _check_type(section, key, value)
            _validate_scalar(section, key, value)
        sections[section] = cls(**kwargs)

    _validate_paths(sections["paths"])
    return Config(**sections)


def config_source_kind(source: Path | None) -> str:
    """给人看的"配置从哪来"。`doctor` 与 `config path` 都要显示它。"""
    if source is None:
        return "内置默认值（未找到任何配置文件）"
    if source == TEMPLATE_PATH:
        return "模板 config/default.toml（还没有 config/config.toml）"
    if source == LOCAL_CONFIG_PATH:
        return "本机配置 config/config.toml"
    return f"显式指定：{source}"


# ==============================================================================
# 环境变量兜底（计划 §3.3）
# ==============================================================================


@dataclass(frozen=True)
class EnvVar:
    r"""一个生效的环境变量，以及**它是谁设的**。

    A7 要求 `doctor` 报告实际生效值。只报值不够——`HF_HOME` 指向
    `C:\Users\luopu\.cache\huggingface` 这件事，是"系统本来就设了"还是
    "程序补的兜底"，决定了换台机器会不会变成另一份缓存。
    """

    name: str
    value: str
    source: str  # "系统已设" | "兜底值"
    why: str

    @property
    def is_fallback(self) -> bool:
        return self.source == "兜底值"


#: (变量名, data_dir 下的兜底相对路径, 说明)
_FALLBACK_ENV: tuple[tuple[str, str, str], ...] = (
    (
        "HF_HOME",
        "models/hf",
        "模型权重缓存位置。系统已有用户级值时不覆盖——覆盖会让同一份权重存两遍",
    ),
    (
        "TORCH_HOME",
        "models/torch",
        "torch hub 缓存；本项目不用 torchvision 的预训练权重，留着只为不让它落到 C 盘用户目录",
    ),
)

#: 值固定、与 data_dir 无关的兜底
_FIXED_ENV: tuple[tuple[str, str, str], ...] = (
    (
        "HF_HUB_OFFLINE",
        "1",
        "索引与检索默认离线（A5）；tools/fetch_model.py 显式关掉它才能联网下载",
    ),
)


def bootstrap_env(
    data_dir: Path,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[EnvVar, ...]:
    """在**任何 torch / transformers 导入之前**调用，补齐缺失的环境变量。

    一律 `setdefault` 语义：已有则一字不动。返回实际生效的取值与来源，
    供 `doctor` 打印（A7）。

    `environ` 传参是为了测试——测试里不要碰真正的 `os.environ`，
    否则一个测试顺序的差别就会让后面的测试看到不同的环境。
    """
    env: dict[str, str] = os.environ if environ is None else environ
    applied: list[EnvVar] = []
    for name, rel, why in _FALLBACK_ENV:
        fallback = (Path(data_dir) / rel).as_posix()
        env.setdefault(name, fallback)
        applied.append(
            EnvVar(
                name=name,
                value=env[name],
                source="系统已设" if env[name] != fallback else "兜底值",
                why=why,
            )
        )
    for name, value, why in _FIXED_ENV:
        env.setdefault(name, value)
        applied.append(
            EnvVar(
                name=name,
                value=env[name],
                source="系统已设" if env[name] != value else "兜底值",
                why=why,
            )
        )
    return tuple(applied)


def effective_env(
    data_dir: Path, *, environ: Mapping[str, str] | None = None
) -> tuple[EnvVar, ...]:
    """只**读**不写：报告各变量"现在会取到什么值"，用于 `doctor` 之外的只读场景。

    与 `bootstrap_env` 的区别是它不动 `os.environ`——`doctor` 这类诊断命令
    改环境变量的唯一理由是为了报告生效值，而报告可以算出来，不必真去改。
    """
    source_env: Mapping[str, str] = os.environ if environ is None else environ
    out: list[EnvVar] = []
    for name, rel, why in _FALLBACK_ENV:
        fallback = (Path(data_dir) / rel).as_posix()
        value = source_env.get(name) or fallback
        out.append(
            EnvVar(
                name=name,
                value=value,
                source="系统已设" if source_env.get(name) else "兜底值",
                why=why,
            )
        )
    for name, value, why in _FIXED_ENV:
        actual = source_env.get(name) or value
        out.append(
            EnvVar(
                name=name,
                value=actual,
                source="系统已设" if source_env.get(name) else "兜底值",
                why=why,
            )
        )
    return tuple(out)


__all__ = [
    "CONFIG_DIR",
    "LOCAL_CONFIG_PATH",
    "PROJECT_ROOT",
    "TEMPLATE_PATH",
    "Config",
    "ConfigError",
    "EnvVar",
    "bootstrap_env",
    "config_source_kind",
    "effective_env",
    "load_config",
    "resolve_config_file",
]
