r"""`embed/base.py`：前缀、批处理、向量后处置、工厂。

------------------------------------------------------------------------------
这一组为什么这样切
------------------------------------------------------------------------------

`base.py` 里的东西有一个共同点：**它们错了都不报错**。前缀拼错只是召回变差，归一化漏了
只是排序尺度变了，`dim` 截断忘了重新归一化只是向量短了一点点。所以这里断言的都不是
"能跑通"，而是**具体到字面的形状**：

1. **前缀**——`M3` 那两条字符串被**逐字**钉死，来源是模型自己那份
   `config_sentence_transformers.json`（真读一次，不是抄一份进测试）。四条构造约束
   各有一条**反例**测试：它们才是有价值的那一半。
2. **标题槽**——`title: none` 是占位符，所以要有标题、没标题、空白标题、带换行的标题
   四种。
3. **后处置**——NaN、零向量、维数不够、非二维，四种失败各一条。
4. **批处理**——用假 `_forward` 数调用次数与每批的条数。**顺序**也要断言：向量与切片
   的对应关系错了，搜出来的片段就是**别人的**，而这同样不报错。
5. **工厂**——最要紧的一条在**子进程**里跑：导入 `embed` 这一层**不许**把 torch 拖
   进来（`D9` 选 ONNX 的首要理由就是冷启动，模块级 `import torch` 会把它整个抵消掉）。

真模型（权重、tokenizer、两路对照）在 `test_embed_gemma_st.py` / `test_embed_onnx.py`。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import (
    DOCUMENT_PROMPT_NAME,
    EMBED_RECIPE,
    PREFIX_CONFIG_FILE,
    QUERY_PROMPT_NAME,
    TITLE_NONE,
    BatchEmbedder,
    Embedder,
    EmbedError,
    EmbedUnavailable,
    TaskPrefixes,
    check_finite,
    check_unit_norm,
    create_embedder,
    embed_ver,
    iter_batches,
    l2_normalize,
    postprocess_vectors,
    prefixes_from_file,
    prefixes_from_prompts,
    resolve_backend,
)

#: 本机缓存里那份模型配置（`M3` 的落地口径，来源是 `google/embeddinggemma-2`）。
MODEL_ID = "google/embeddinggemma-2"

#: 仓库根。给子进程那条测试用（`tests/unit/xxx.py` 往上两级）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 两条真前缀（逐字，含末尾那个空格）。**这不是抄的**：下面
#: `test_retrieval_prefixes_match_the_model_file` 会去模型文件里核对一遍，
#: 这一对常量的作用是把"核对"变成"钉死"——模型文件变了、而口径没跟着变时，先红的
#: 是那条测试，而不是召回。两个后端的四份快照（base / ONNX × 两份）都读过，内容逐字节相同。
QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "


@pytest.fixture
def prefixes() -> TaskPrefixes:
    return TaskPrefixes(query=QUERY_PREFIX, document=DOCUMENT_PREFIX)


# ==============================================================================
# 前缀：真模型文件 + 四条构造约束
# ==============================================================================


def _cached_snapshot(repo: str = MODEL_ID) -> Path:
    """本机缓存里这份快照的目录。**取不到就跳过**，理由写在测试自己的 docstring 里。"""
    from huggingface_hub import snapshot_download

    try:
        return Path(
            snapshot_download(
                repo,
                allow_patterns=[PREFIX_CONFIG_FILE],
                local_files_only=True,
            )
        )
    except Exception as exc:  # pragma: no cover - 取决于本机缓存
        pytest.skip(f"本机缓存里没有 {repo} 的 {PREFIX_CONFIG_FILE}：{exc}")


def test_retrieval_prefixes_match_the_model_file():
    """`M3` 的两条前缀与模型自己的文件**逐字**相同。

    这一条是 `F5-01` 里最要紧的断言：前缀错了不会报错，只会让召回悄悄下降，所以它的
    正确性不能靠"我读了一遍觉得对"，只能靠一条会红的测试。
    """
    path = _cached_snapshot() / PREFIX_CONFIG_FILE
    prompts = json.loads(path.read_text(encoding="utf-8"))["prompts"]
    got = prefixes_from_prompts(prompts, source=str(path))
    assert got.query == QUERY_PREFIX
    assert got.document == DOCUMENT_PREFIX
    # 取的是检索任务那一对，不是表里同样存在的 `query` / `document` 短名——值恰好相同，
    # 但换成短名就等于把"用哪个任务的提示词"这件事交给巧合。
    assert prompts[QUERY_PROMPT_NAME] == prompts["query"]
    assert prompts[DOCUMENT_PROMPT_NAME] == prompts["document"]


def test_the_two_backends_read_the_same_prefix_file():
    """ONNX 那份快照里的前缀与 base 那份**逐字节**相同。

    两个后端各读各的快照。这条测试证明"两路向量可以比"这件事有一个前提：它们用的是
    同一份前缀契约。任意一天上游只更新了其中一份，这条会先红。
    """
    first = (_cached_snapshot() / PREFIX_CONFIG_FILE).read_bytes()
    second = (
        _cached_snapshot("onnx-community/embeddinggemma-2-ONNX") / PREFIX_CONFIG_FILE
    ).read_bytes()
    assert first == second


@pytest.mark.parametrize(
    ("query", "document", "why"),
    [
        (QUERY_PREFIX, QUERY_PREFIX, "两条前缀相同（非对称被破坏）"),
        ("task: search result | query: | title: none | text: ", DOCUMENT_PREFIX, "查询里有标题槽"),
        ("task: search result | query: ", "| text: ", "文档里没有标题槽"),
        ("task: search result | query: ", f"{TITLE_NONE}{TITLE_NONE} | text: ", "标题槽出现两次"),
        ("task: search result | query:", DOCUMENT_PREFIX, "查询前缀不以空白结尾"),
        (QUERY_PREFIX, "title: none | text:", "文档前缀不以空白结尾"),
    ],
)
def test_prefix_guards_reject(query: str, document: str, why: str):
    """六种错法的**反例**。每一条都是静默降召回，所以每一条都要构造不出来。"""
    with pytest.raises(EmbedError):
        TaskPrefixes(query=query, document=document)


def test_prefix_guards_accept_the_real_pair(prefixes: TaskPrefixes):
    """正例：模型给的那一对必须过——否则上面那六条就成了"凡是前缀都拦"。"""
    assert prefixes.query == QUERY_PREFIX


def test_prefixes_from_prompts_names_what_is_missing():
    with pytest.raises(EmbedUnavailable) as info:
        prefixes_from_prompts({"STS": "task: sentence similarity | query: "})
    message = str(info.value)
    assert QUERY_PROMPT_NAME in message and DOCUMENT_PROMPT_NAME in message
    # 报错要把"这份文件里有什么"一起说出来，否则还得自己去翻文件。
    assert "STS" in message


def test_prefixes_from_file_round_trip(tmp_path: Path):
    path = tmp_path / PREFIX_CONFIG_FILE
    path.write_text(
        json.dumps({"prompts": {QUERY_PROMPT_NAME: QUERY_PREFIX, DOCUMENT_PROMPT_NAME: DOCUMENT_PREFIX}}),
        encoding="utf-8",
    )
    assert prefixes_from_file(path) == TaskPrefixes(query=QUERY_PREFIX, document=DOCUMENT_PREFIX)


def test_prefixes_from_file_reports_a_missing_file(tmp_path: Path):
    """文件不在时是 `EmbedUnavailable`，**不是** `FileNotFoundError`。

    走到这一步时快照已经解析出来了，文件却不在——那是缓存缺件（环境问题），
    调用方应当整趟停下，而不是逐条跳过。
    """
    with pytest.raises(EmbedUnavailable):
        prefixes_from_file(tmp_path / "nope.json")


def test_prefixes_from_file_rejects_junk(tmp_path: Path):
    path = tmp_path / PREFIX_CONFIG_FILE
    path.write_text("{\"prompts\": {}}", encoding="utf-8")
    with pytest.raises(EmbedUnavailable):
        prefixes_from_file(path)


# ==============================================================================
# 标题槽
# ==============================================================================


@pytest.mark.parametrize("title", [None, "", "   ", "\t\n"])
def test_no_title_keeps_the_model_prefix(prefixes: TaskPrefixes, title: str | None):
    """没有标题 / 空标题 / 只有空白的标题：**原样用模型给的那一条**。"""
    assert prefixes.document_prefix(title) == DOCUMENT_PREFIX


def test_title_replaces_the_slot(prefixes: TaskPrefixes):
    assert (
        prefixes.document_prefix("季度财务报表.xlsx")
        == "title: 季度财务报表.xlsx | text: "
    )


def test_title_whitespace_is_collapsed(prefixes: TaskPrefixes):
    """标题里的换行/多个空格压成一个空格。

    标题来自文件名与文档属性，里面有换行的话会把前缀**截断**——而这是单行的一段。
    """
    assert prefixes.document_prefix("会议\n纪要\t第 3 次") == "title: 会议 纪要 第 3 次 | text: "


# ==============================================================================
# `embed_ver`：字面契约
# ==============================================================================


def test_embed_ver_literal():
    """字面契约（与 `parser_ver` 那一条同样的写法）：字段、分隔符、顺序。

    `embed_ver` 是 `F5-03` 缓存键的一半，它变一位就等于全库重嵌。改这个形状**必须**
    同时改这条测试——那正是它存在的意义。
    """
    assert (
        embed_ver(EmbedCfg())
        == f"embed:{EMBED_RECIPE}|google/embeddinggemma-2|768|float32|2048|int8"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "google/embeddinggemma-3"),
        ("dim", 512),
        # bfloat16 只有 8 位尾数：与本机唯一可用的 float32 算出来的不是同一批数。
        ("dtype", "bfloat16"),
        # 决定"多长以后的正文被丢掉"：超过它的切片换个值就是另一个向量。
        ("max_seq", 512),
        ("quantize", "none"),
    ],
)
def test_embed_ver_changes_with_the_fingerprint(field: str, value: object):
    assert embed_ver(EmbedCfg(**{field: value})) != embed_ver(EmbedCfg())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # ⚠ 这一条是**决策**，不是巧合：两个后端算的是同一个函数（实测余弦 0.9999），
        # 换实现不该让全库重嵌。推翻它的判据与新读数写在 embed/base.py 第五节。
        ("backend", "sentence_transformers"),
        # 批大小与线程数只影响快慢。
        ("batch_size", 32),
        ("threads", 4),
    ],
)
def test_embed_ver_ignores_knobs_that_only_change_the_speed(field: str, value: object):
    assert embed_ver(EmbedCfg(**{field: value})) == embed_ver(EmbedCfg())


# ==============================================================================
# 向量后处置
# ==============================================================================


def test_postprocess_truncates_then_normalizes():
    """`dim` 截断取的是**前**几列，且归一化在截断**之后**。

    用一个长度不均的行来验顺序：先归一化再截断的话，结果列方向一样但长度小于 1。
    """
    raw = np.array([[3.0, 4.0, 100.0], [0.0, 5.0, 25.0]], dtype=np.float32)
    out = postprocess_vectors(raw, 2, source="测试")
    assert out.shape == (2, 2)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[0], [0.6, 0.8], atol=1e-6)
    np.testing.assert_allclose(out[1], [0.0, 1.0], atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), [1.0, 1.0], atol=1e-6)


def test_postprocess_rejects_more_dims_than_the_backend_gives():
    with pytest.raises(EmbedError, match="192"):
        postprocess_vectors(np.zeros((1, 192), dtype=np.float32), 768, source="测试")


def test_postprocess_rejects_non_2d():
    with pytest.raises(EmbedError):
        postprocess_vectors(np.zeros((4,), dtype=np.float32), 2, source="测试")


def test_postprocess_rejects_nan():
    """`R6` 的 fp16 坑就是这个症状：**不抛异常**，只在结果里留 NaN。"""
    raw = np.array([[np.nan, 1.0], [1.0, 1.0]], dtype=np.float32)
    with pytest.raises(EmbedError, match="NaN"):
        postprocess_vectors(raw, 2, source="测试")


def test_postprocess_rejects_a_zero_row():
    """零向量与任何查询的余弦都是 0（永远搜不到），所以不许落进索引。"""
    with pytest.raises(EmbedError, match="零向量"):
        postprocess_vectors(np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32), 2, source="测试")


def test_l2_normalize_does_not_add_an_epsilon_crash_guard():
    """分母加 eps 会让零向量变成"长度为 0 的正常向量"——那正是这条检查要避免的。"""
    with pytest.raises(EmbedError):
        l2_normalize(np.zeros((1, 3), dtype=np.float32), source="测试")


def test_check_finite_reports_inf_and_accepts_ordinary_values():
    with pytest.raises(EmbedError):
        check_finite(np.array([[1.0, np.inf]], dtype=np.float32), source="测试")
    check_finite(np.zeros((1, 2), dtype=np.float32), source="测试")


def test_check_unit_norm_catches_a_forgotten_renormalization():
    with pytest.raises(EmbedError, match="归一化"):
        check_unit_norm(np.array([[0.6, 0.8], [0.3, 0.4]], dtype=np.float32), source="测试")


# ==============================================================================
# 批处理
# ==============================================================================


@pytest.mark.parametrize(
    ("count", "size", "expected"),
    [
        (5, 2, [2, 2, 1]),
        (4, 2, [2, 2]),
        (3, 8, [3]),
        (1, 1, [1]),
    ],
)
def test_iter_batches_sizes(count: int, size: int, expected: list[int]):
    batches = list(iter_batches([str(i) for i in range(count)], size))
    assert [len(b) for b in batches] == expected
    assert [item for batch in batches for item in batch] == [str(i) for i in range(count)]


def test_iter_batches_rejects_zero():
    """`size = 0` 不报错的话，`range(0, n, 0)` 会直接抛 `ValueError`……但真正危险的是
    "什么都没产出"的迭代器：它让调用方以为"这批处理完了"。"""
    with pytest.raises(ValueError):
        list(iter_batches(["a"], 0))


# ==============================================================================
# `BatchEmbedder`：用假后端验公共那一半
# ==============================================================================


class _FakeBackend(BatchEmbedder):
    """记下每一次 `_forward` 收到的东西，返回一批**可控**的原始向量。

    `width` 给 `_forward` 返回几维（默认按 `cfg.dim`），`rows` 给行数（默认与输入等长）——
    后者用来构造"后端返回的行数不对"这种坏情况。
    """

    name = "fake"

    def __init__(self, cfg: EmbedCfg, prefixes: TaskPrefixes, *, width=None, rows=None):
        super().__init__(cfg, prefixes)
        self.calls: list[list[str]] = []
        self.width = width
        self.rows = rows

    def _forward(self, texts):
        self.calls.append(list(texts))
        n = len(texts) if self.rows is None else self.rows
        width = self.cfg.dim if self.width is None else self.width
        return np.arange(1, n * width + 1, dtype=np.float32).reshape(n, width)


@pytest.fixture
def fake(prefixes: TaskPrefixes) -> _FakeBackend:
    return _FakeBackend(EmbedCfg(batch_size=2), prefixes)


def test_queries_get_the_query_prefix(fake: _FakeBackend):
    fake.encode_queries(["甲", "乙"])
    assert fake.calls == [[QUERY_PREFIX + "甲", QUERY_PREFIX + "乙"]]


def test_documents_get_the_document_prefix_and_their_own_title(fake: _FakeBackend):
    fake.encode_documents(["正文一", "正文二"], ["标题一", None])
    assert fake.calls == [
        ["title: 标题一 | text: 正文一", "title: none | text: 正文二"]
    ]


def test_documents_without_titles_use_the_model_prefix(fake: _FakeBackend):
    fake.encode_documents(["正文"])
    assert fake.calls == [[DOCUMENT_PREFIX + "正文"]]


def test_batching_splits_and_keeps_the_order(prefixes: TaskPrefixes):
    """分两批（2 + 1），且返回的向量与输入**逐条对应**。

    顺序错了不会报错：向量照样是单位长度、索引照样建得起来，只是每一个切片都挂着别人
    的向量。所以这里断言的是"第 n 行来自第 n 条"。
    """
    backend = _FakeBackend(EmbedCfg(batch_size=2), prefixes)
    texts = [f"第{i}条" for i in range(3)]
    out = backend.encode_queries(texts)
    assert [len(call) for call in backend.calls] == [2, 1]
    assert out.shape == (3, 768)
    for index, text in enumerate(texts):
        assert backend.calls[index // 2][index % 2] == QUERY_PREFIX + text


def test_a_single_batch_does_not_get_split(prefixes: TaskPrefixes):
    backend = _FakeBackend(EmbedCfg(batch_size=100), prefixes)
    backend.encode_queries(["甲", "乙", "丙"])
    assert [len(call) for call in backend.calls] == [3]


def test_empty_input_returns_an_empty_array(prefixes: TaskPrefixes):
    """空输入**不是**错误：`(0, dim)` 的空数组让调用方（`F5-02` 的批量）不必特判。"""
    backend = _FakeBackend(EmbedCfg(), prefixes)
    out = backend.encode_queries([])
    assert out.shape == (0, 768)
    assert backend.calls == []


def test_title_count_must_match(prefixes: TaskPrefixes):
    backend = _FakeBackend(EmbedCfg(), prefixes)
    with pytest.raises(ValueError, match="不等长"):
        backend.encode_documents(["甲", "乙"], ["只有一条"])


def test_a_bare_string_is_rejected(prefixes: TaskPrefixes):
    """传字符串进来会**逐字符**加前缀：一条查询变成十几个单字查询，还不报错。"""
    backend = _FakeBackend(EmbedCfg(), prefixes)
    with pytest.raises(TypeError, match="字符串序列"):
        backend.encode_queries("北京大学")


def test_wrong_row_count_from_the_backend_is_caught(prefixes: TaskPrefixes):
    """后端少给几行时**当场报错**：悄悄按 min(行数) 对齐，会让每一个切片都挂上别人的
    向量，而那与"索引建好了"长得一模一样。"""
    backend = _FakeBackend(EmbedCfg(batch_size=4), prefixes, rows=2)
    with pytest.raises(EmbedError, match="返回了 2 个向量"):
        backend.encode_queries(["甲", "乙", "丙"])


def test_close_is_idempotent_and_stops_further_use(prefixes: TaskPrefixes):
    backend = _FakeBackend(EmbedCfg(), prefixes)
    backend.close()
    backend.close()
    with pytest.raises(EmbedError, match="close"):
        backend.encode_queries(["甲"])


def test_satisfies_the_protocol(fake: _FakeBackend):
    assert isinstance(fake, Embedder)
    assert fake.dim == 768
    assert fake.embed_ver == embed_ver(EmbedCfg())
    assert fake.prefixes.query == QUERY_PREFIX


# ==============================================================================
# 工厂
# ==============================================================================


def test_auto_resolves_to_the_production_backend():
    """`D9`：ONNX int8 是生产后端。这条测试把那个决定写死在代码里。"""
    assert resolve_backend("auto") == "onnx"
    assert resolve_backend("onnx") == "onnx"
    assert resolve_backend("sentence_transformers") == "sentence_transformers"


def test_unknown_backend_names_the_alternatives():
    with pytest.raises(EmbedUnavailable) as info:
        create_embedder(EmbedCfg(backend="nope"))
    assert "onnx" in str(info.value)
    assert "sentence_transformers" in str(info.value)


def test_importing_the_embed_layer_does_not_drag_in_torch():
    """**在子进程里**验：导入 `embed` 这一层不许把 torch / sentencetransformers /
    onnxruntime 拉进来。

    为什么这条值得单开一个进程：`import torch` 2.4 秒、`import sentence_transformers`
    再加 7.6 秒（P0 实测），而 `D9` 选 ONNX 的**首要理由**就是冷启动 0.50 秒对 5.89 秒。
    模块级导入会把这 10 秒加回每一次 `lsearch search` —— 那正好抵消掉选它的理由。

    在本进程里断言 `"torch" not in sys.modules` 是不行的：同一个 pytest 进程里别的
    测试（真模型那两条）早就把它导进来了。所以只能开子进程。
    """
    code = (
        "import sys;"
        "import localsearch.embed.base;"
        "import localsearch.embed.gemma_st;"
        "import localsearch.embed.gemma_onnx;"
        "print(*[name in sys.modules for name in "
        "('torch', 'sentence_transformers', 'onnxruntime', 'transformers')])"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False False False False"
