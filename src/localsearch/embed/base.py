r"""嵌入层的公共形态：任务前缀、批处理、向量后处置（`F5-01`，验收项 `A2` / `A9`）。

这一层管"一个向量怎么出来"里的**公共那一半**，与"用哪个后端算"无关：

* **任务前缀**（`TaskPrefixes`）——查询与文档**非对称**地加前缀；
* **批处理**（`iter_batches` + `[embed] batch_size`）——一次喂几条；
* **向量后处置**（`postprocess_vectors`）——按 `dim` 截断（`D5` 的 MRL）→ 查有限性 →
  L2 归一化 → 复核单位长度。

两个后端（`gemma_st.py` / `gemma_onnx.py`）各自只实现 `_forward()`：一批**已加好前缀**
的文本 → 一批原始向量。其余全部继承 `BatchEmbedder`。

------------------------------------------------------------------------------
一、前缀必须从模型文件里读，一个字都不许手写
------------------------------------------------------------------------------

`M3` 的原话是"写错不会报错，只会**静默**降低召回"。这句话决定了这一层的形状：前缀是
**模型自己的契约**，不是我们的配置项。同一个模型的下一版、或者换一家供应商，前缀词表
就可能变，而**任何一处的偏差都不表现为异常**——recall 从 0.9 掉到 0.6，搜索结果看起来
完全正常。

所以前缀**从模型自带的 `config_sentence_transformers.json` 里读**（两个后端的快照里
都有这个文件，实测两份内容逐字节相同），读进来之后再过一遍 `TaskPrefixes` 的四条硬
约束。`test_embed_base.py` 里另有一条**直接读本机那份缓存文件**的测试，把
`Retrieval-query` / `Retrieval-document` 两条前缀钉死在 `M3` 的口径上——模型文件一旦
变了，先红的是那条测试，而不是召回。

⚠ **这件事的幅度在本机量过，比 `M3` 那句话暗示的小得多**（`F5-01`，
`tools/bench_prefix.py`，读数在 `PROGRESS.md` §3）。一份 6 条的语料上，"相关文档 -
无关文档"的余弦间距是：正确前缀 **+0.2421**、两条前缀互换 **+0.2390**、完全不加
**+0.2250**。方向与 `M3` 一致（正确前缀间距最大），但三种拼法下相关文档的**名次都是
`[1, 2]`**——这份语料太容易，分不出名次上的差异。所以这一轮能确立的只是"用模型自己的
前缀"这个决定，**量不出它的收益**；真能判断的是 `F5-05` 那 20 条标注评测（`A4`）。
在那之前，别把 `M3` 那句"0.9 掉到 0.6"当实测数字引用。

⚠ 同一份读数还给出一个陷阱：**绝对余弦会随前缀整体平移**（不加前缀时相关文档是
0.8691，加了正确前缀反而降到 0.8639）。所以任何"相似度超过 X 才算命中"的阈值**都不能
跨前缀标定**，`F5-04` / `F5-05` 里若要引入这类阈值，得把它与 `embed_ver` 绑在一起。

------------------------------------------------------------------------------
二、标题槽：`title: none` 是一个**占位符**，不是一个默认值
------------------------------------------------------------------------------

文档前缀里有 `title: none | text: ` 这么一段。`none` 不是"标题这一项的值就叫 none"，
而是"这条文档没有标题"——`M3` 的口径是 `title: {t} | text: {c}`，无标题时把 `t` 写成
`none`。所以：

* 有标题 → 把 `title: none` 这一段**换成** `title: {真标题}`；
* 没标题 → 原样用模型给的前缀。

`TaskPrefixes.__post_init__` 要求文档前缀里 `title: none` **有且只有一次**：没有它就没法
换（有标题也永远按无标题处理，静默），有两次就不知道该换哪一个。

⚠ 查询前缀里**不许**出现标题槽：查询侧没有标题，一旦出现，说明查询与文档用了同一份
前缀——那正是 `M3` 说的"非对称"被破坏的形态，而它同样不报错。`__post_init__` 把
非对称本身也做成了硬约束（两条前缀必须逐字不同）。

------------------------------------------------------------------------------
三、前缀以空白结尾，拼接时不另加分隔符
------------------------------------------------------------------------------

模型给的前缀形如 `"task: search result | query: "`，**末尾那个空格是前缀的一部分**。
拼接就是 `prefix + text`，中间不加任何东西——这与 `sentence-transformers` 自己的做法
一致（它也是直接 `p + s`），与 P0 基准脚本一致。所以"前缀不以空白结尾"是一条会**静默
粘词**的错误配置（`query:` 紧贴正文），`__post_init__` 里直接拦掉。

------------------------------------------------------------------------------
四、`dim` 截断在归一化**之前**，量化不在这里
------------------------------------------------------------------------------

`D5` 允许把原生 768 维按 MRL 截到 512 / 256 / 128。截断的顺序是先切前 `dim` 列、
再 L2 归一化（P0 的 ONNX 路径就是这么做的）。切完不重新归一化的话，向量长度会小于 1，
而余弦相似度仍然"看着像对的"——`check_unit_norm()` 那一步复核盯着这个。

**`quantize` 不在这里做**：`[embed] quantize = "int8"` 是**落盘**的量化
（`F5-03` / `F5-04` 的事），嵌入器对外一律给 `float32`。把落盘量化混进嵌入器，会让
"算出来的向量"与"存下去的向量"变成两个东西，而缓存键（`text_hash` + `embed_ver`）
是按前者算的。

------------------------------------------------------------------------------
五、`embed_ver` 收"会改数的"，不收"只改快慢的"
------------------------------------------------------------------------------

`F5-03` 的 `embed_cache` 以 `(text_hash, embed_ver)` 为键，`A9` 的"只嵌入一次"挂在它
上面。`embed_ver` 变了就等于**全库重嵌**——本机按 `A1` 的两万文件基准集估算是一趟
数小时到一天的活。所以这个串里放什么，直接影响"改配置要付多大代价"。

规则只有一条：**改变向量数值的收进来，只影响快慢的不收**（逐项理由写在
`embed_ver()` 的文档里）。唯一需要实测才能定的是 `backend`：同一模型、同一维度、
同一量化的两路向量实测余弦 0.9999，差异在 float32 抖动与 int8 量化同一个量级上，
所以后端名**不进**指纹——为一次"换个更快的实现"付一整趟全量重嵌不划算。

⚠ 这是一个**权衡过的**决定，不是省事。什么时候要推翻它：两个后端给出**实质性**不同的
向量（某个后端的前缀处理不同、或者池化方式换了）。判据是**余弦**，不是"看着不一样"——
重跑一遍的浮点抖动也能让两个向量不逐位相同。真到了那一天，要改的是这条判断，
并且必须在 `test_embed_backends_agree` 那条实测上留下新的读数。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from localsearch.core.config import EmbedCfg

__all__ = [
    "DEFAULT_BACKEND",
    "DOCUMENT_PROMPT_NAME",
    "EMBED_RECIPE",
    "PREFIX_CONFIG_FILE",
    "QUERY_PROMPT_NAME",
    "TITLE_NONE",
    "BatchEmbedder",
    "Embedder",
    "EmbedError",
    "EmbedUnavailable",
    "TaskPrefixes",
    "check_finite",
    "check_unit_norm",
    "create_embedder",
    "embed_ver",
    "iter_batches",
    "l2_normalize",
    "postprocess_vectors",
    "prefixes_from_file",
    "prefixes_from_prompts",
    "resolve_backend",
]

# ==============================================================================
# 常量
# ==============================================================================

#: 模型自己的提示词表所在的文件名。两个后端的快照里都有，实测内容相同。
PREFIX_CONFIG_FILE = "config_sentence_transformers.json"

#: 在这个表里取哪两条。`Retrieval-query` / `Retrieval-document` 是**检索任务**那一对
#: （表里还有 `query` / `document` 两条短名，值逐字相同；以及 `STS` / `Classification`
#: 等别的任务的前缀，**不要拿错**——拿错同样是静默降召回）。取带 `Retrieval` 的那一对，
#: 是因为本项目做的正是检索，而这一对的名字把用途写在了脸上。
QUERY_PROMPT_NAME = "Retrieval-query"
DOCUMENT_PROMPT_NAME = "Retrieval-document"

#: 文档前缀里表示"这条文档没有标题"的那一段。它是**占位符**，见模块文档第二节。
TITLE_NONE = "title: none"

#: `embed_ver` 的配方版本。**手改**：这个数只在"嵌入结果会因为代码改动而变"时 +1。
#: 它与 `index/build.py` 的 `PARSER_RECIPE` 是同一个套路（那边是解析口径，这边是嵌入口径），
#: 两处都刻意不做成"自动从代码算哈希"——自动哈希会因为注释改动而全库重嵌。
EMBED_RECIPE = 1

#: `[embed] backend = "auto"` 解析成哪个。`D9`：ONNX int8 是生产后端
#: （稳态快 14.7%、冷启动快 11.8 倍，P0 双跑实测）。
DEFAULT_BACKEND = "onnx"

#: 归一化时判定"这是零向量"的下限。低于它的向量除以自己的长度会得到 NaN 或爆炸值，
#: 而那是必须出声的（见 `l2_normalize`）。
_ZERO_NORM_EPS = 1e-12

#: `check_unit_norm` 的容差。float32 下真正的单位向量偏差在 1e-7 量级（P0 实测），
#: 给到 1e-3 是留出 int8 后端与将来别的后端的余量。
_UNIT_NORM_TOL = 1e-3


# ==============================================================================
# 失败：两种，"这个后端用不了"与"算出来的东西不对"
# ==============================================================================


class EmbedError(Exception):
    """嵌入这一层出错了。**消息就是给用户/日志看的理由**。

    与 `extract/base.py` 的 `ExtractError` 同一个分工：只管"这一次调用不成立"，由调用方
    接住并决定是重试、跳过还是中止。
    """


class EmbedUnavailable(EmbedError):
    """这个后端**现在用不了**：库没装、权重不在本机缓存里、模型文件缺件。

    单独一个类，是因为它的处置与"算错了"完全不同：这一类是**环境**问题（跑一次
    `tools/fetch_model.py`、或者改 `[embed] backend`），而不是**这一批数据**的问题，
    调用方应当**立刻停下整趟索引**而不是逐条跳过——一条都算不出来的话，跳过两万个文件
    只是把一次失败摊成两万次。
    """


# ==============================================================================
# 任务前缀
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TaskPrefixes:
    """查询与文档两条前缀。**非对称**——它们逐字不同，这是模型的要求（`M3`）。

    这个类存在的唯一理由是把它那四条约束**放在数据里**而不是放在注释里：前缀的错法
    全部是静默的，所以构造不成立的东西就该构造不出来。
    """

    #: 加在查询前面，例如 `"task: search result | query: "`。
    query: str
    #: 加在文档前面，例如 `"title: none | text: "`，其中 `title: none` 是标题槽。
    document: str

    def __post_init__(self) -> None:
        if self.query == self.document:
            raise EmbedError(
                f"查询前缀与文档前缀逐字相同（{self.query!r}）。EmbeddingGemma 的两条"
                "前缀是**非对称**的：查询用检索任务前缀、文档用标题+正文前缀。"
                "用成同一条不会报错，只会静默降低召回。"
            )
        if self.query.count(TITLE_NONE):
            raise EmbedError(
                f"查询前缀里出现了标题槽 {TITLE_NONE!r}（{self.query!r}）：查询这一侧"
                "没有标题。出现它说明查询与文档用了同一份前缀（那会被上一条拦下），"
                "或者模型文件换了一份我们还没适配的词表。"
            )
        found = self.document.count(TITLE_NONE)
        if found != 1:
            raise EmbedError(
                f"文档前缀里 {TITLE_NONE!r} 出现 {found} 次（要求恰好 1 次）：{self.document!r}。"
                "它是标题占位符，`document_prefix()` 要靠替换它来写标题——"
                "0 次的话有标题也永远按无标题处理（静默），多次的话不知道该换哪一个。"
            )
        for name, text in (("查询", self.query), ("文档", self.document)):
            if not text.endswith((" ", "\n", "\t")):
                raise EmbedError(
                    f"{name}前缀没有以空白结尾：{text!r}。拼接时是 `prefix + text`，"
                    "中间不加分隔符（与 sentence-transformers 自己的做法一致），"
                    "所以前缀末尾那个空格是前缀的一部分——少了它会把前缀与正文粘成一个词。"
                )

    def document_prefix(self, title: str | None) -> str:
        """这条文档该用的前缀：有标题就把标题槽换成标题，没有就原样。

        ⚠ 标题里的空白会被**压成一个空格**再写进去：标题来自文件名与文档属性，里面
        可能有换行或制表符，而前缀里的这一段是**单行**的（后面紧跟 `| text: `）。
        压空白的代价是"标题里的多个空格变成一个"——与"前缀被换行截断"相比，前者无害。
        """
        if title is None:
            return self.document
        cleaned = " ".join(title.split())
        if not cleaned:
            # 空标题与没有标题是同一件事：只写了空格的文件名不会带来任何信息，
            # 而写成 `title:    | text:` 只会多几个噪声 token。
            return self.document
        return self.document.replace(TITLE_NONE, f"title: {cleaned}")


def prefixes_from_prompts(
    prompts: Mapping[str, str],
    *,
    query_name: str = QUERY_PROMPT_NAME,
    document_name: str = DOCUMENT_PROMPT_NAME,
    source: str = PREFIX_CONFIG_FILE,
) -> TaskPrefixes:
    """从模型的提示词表里取那一对。**取不到就是错的**，不兜底。

    不兜底的理由：兜底只能用一份**我们手写的前缀**，而手写前缀正是本模块最想避免的
    东西（`M3`）。取不到说明模型换了词表命名，那时候正确的行为是**当场失败**，让人去
    看一眼新词表，而不是用旧前缀继续跑出一份召回已经掉了的索引。
    """
    missing = [n for n in (query_name, document_name) if n not in prompts]
    if missing:
        raise EmbedUnavailable(
            f"{source} 里没有 {'、'.join(repr(m) for m in missing)}。"
            f"这份文件里有的名字：{', '.join(sorted(prompts))}。"
            "前缀词表是模型契约的一部分，本项目不内置任何手写前缀——"
            "换了词表命名时请核对新表，并同步修改 base.QUERY_PROMPT_NAME / DOCUMENT_PROMPT_NAME。"
        )
    return TaskPrefixes(query=prompts[query_name], document=prompts[document_name])


def prefixes_from_file(
    path: str | Path,
    *,
    query_name: str = QUERY_PROMPT_NAME,
    document_name: str = DOCUMENT_PROMPT_NAME,
) -> TaskPrefixes:
    """`prefixes_from_prompts` 的文件版：读模型快照里的 `config_sentence_transformers.json`。

    两个后端都走这一条（ONNX 那一支没有 `SentenceTransformer` 对象可问）。文件不在、
    不是 JSON、或者没有 `prompts` 这一段，一律 `EmbedUnavailable`——**包括文件不存在**：
    走到这里时快照已经解析出来了，文件却不在，只能是缓存坏了或缺件。
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EmbedUnavailable(f"读不了 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise EmbedUnavailable(f"{path} 不是合法的 JSON：{exc}") from exc
    prompts = raw.get("prompts")
    if not isinstance(prompts, dict) or not prompts:
        raise EmbedUnavailable(
            f"{path} 里没有可用的 `prompts`（拿到的是 {type(prompts).__name__}: {prompts!r}）"
        )
    return prefixes_from_prompts(
        prompts, query_name=query_name, document_name=document_name, source=str(path)
    )


# ==============================================================================
# 指纹
# ==============================================================================


def embed_ver(cfg: EmbedCfg) -> str:
    """向量索引的**版本串**：同一份文本在这串一样时，向量一定一样。

    形状与 `index/build.parser_ver()` 对齐，落点是 `chunks.embed_ver` 与
    `embed_cache.embed_ver`（计划 §4.4，`F5-03`）。

    **收哪些、不收哪些，只有一条规则**：会**改变向量数值**的都收进来；**只影响快慢**的
    都不收。

    进指纹的（全部真的会改数）：

    * `EMBED_RECIPE`——代码口径变了就 +1（池化方式、归一化时机……）；
    * `model_id`——换了模型当然要重嵌；
    * `dim`——`D5` 的 MRL 截断到 512/256/128 得到的是**另一组向量**（前 `dim` 列）；
    * `dtype`——`bfloat16` 只有 8 位尾数，与 `float32` 算出来的不是同一批数
      （本机 bfloat16 还慢 6.21 倍，所以实际只有 `float32`，`M2`）；
    * `max_seq`——它决定**多长以后的正文被丢掉**，凡是超过它的切片，换个值就是另一个
      向量。切片目标长度虽然也是 512，但"通常不超过"不是"一定不超过"；
    * `quantize`——int8 量化是**有损**的，`none` 与 `int8` 落盘的不是同一批数。

    不进指纹的（只影响快慢）：`batch_size`、`threads`，以及**`backend`**。

    `backend` 是这条规则里唯一需要**实测**才能下结论的那一项，理由见模块文档第五节：
    两个后端的向量余弦 0.9999，差异在 float32 抖动与 int8 量化同一个量级上。把后端放进
    指纹，就等于为一次"换个更快的实现"付一整趟全库重嵌（按 `A1` 的两万文件基准集估算是
    数小时到一天），而换来的向量差别比重新跑一遍的浮点抖动还小。

    ⚠ `model_id` 用的是**配置里的名字**，不是快照的哈希：换快照（同一个名字下权重更新）
    这件事在离线运行里不会自动发生，真发生了要用 `lsearch reindex --stale`（`F6-01`）。
    """
    return (
        f"embed:{EMBED_RECIPE}|{cfg.model_id}|{cfg.dim}|{cfg.dtype}"
        f"|{cfg.max_seq}|{cfg.quantize}"
    )


# ==============================================================================
# 向量后处置
# ==============================================================================


def check_finite(vecs: np.ndarray, *, source: str) -> None:
    """有 NaN / ±inf 就报错。**这是 `R6` 那条 fp16 坑的唯一探测器**。

    fp16 溢出在 CPU 上不抛异常，只在结果里留一堆 NaN——而 NaN 往下走会变成"这个切片
    永远搜不到"（比较恒假），一句话都不会说。所以这一步放在**归一化之前**：NaN 过了
    归一化还是 NaN，但错误消息会变成"零向量"，指向错误的方向。
    """
    if not np.isfinite(vecs).all():
        bad = int((~np.isfinite(vecs)).any(axis=1).sum()) if vecs.ndim == 2 else 0
        raise EmbedError(
            f"{source} 给出的向量里有 NaN 或 inf（{bad} / {len(vecs)} 行）。"
            "最常见的原因是 [embed] dtype 用了 float16（R6：fp16 在 CPU 上溢出产出 NaN，"
            "且不报错）。"
        )


def l2_normalize(vecs: np.ndarray, *, source: str) -> np.ndarray:
    """按行 L2 归一化到单位长度。

    ⚠ 零向量**当场报错**，不返回零向量、不加 eps 兜底：一个全零行意味着这个切片与
    任何查询的余弦都是 0，也就是**永远搜不到**。让它悄悄躺在索引里，等于给自己埋一条
    "某个文件怎么都搜不着"的谜题；而分母加 eps 更坏——它会算出 `0/1e-12 = 0`，看起来
    一切正常。
    """
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or float(norms.min(initial=np.inf)) < _ZERO_NORM_EPS:
        rows = np.flatnonzero(norms.reshape(-1) < _ZERO_NORM_EPS)[:5].tolist()
        raise EmbedError(
            f"{source} 给出的向量里有零向量（第 {rows} 行，共 "
            f"{int((norms.reshape(-1) < _ZERO_NORM_EPS).sum())} 行）。"
            "零向量与任何查询的余弦都是 0，也就是永远搜不到——不允许落进索引。"
        )
    return (vecs / norms).astype(np.float32, copy=False)


def check_unit_norm(vecs: np.ndarray, *, source: str, tol: float = _UNIT_NORM_TOL) -> None:
    """复核归一化真的做成了。**它的目标是"以为归一化了、其实没有"**。

    真实存在的那条：先按 `dim` 截断再忘了重新归一化（截断会缩短向量），此时余弦相似度
    仍然"看起来合理"、排序也仍然单调，只是尺度错了——而 `F5-04` 的 HNSW 用内积当余弦
    的前提正是单位长度。
    """
    norms = np.linalg.norm(vecs, axis=1)
    worst = float(np.abs(norms - 1.0).max(initial=0.0))
    if worst > tol:
        raise EmbedError(
            f"{source} 归一化之后仍有非单位长度的行（最大偏差 {worst:.3g} > {tol}）。"
            "按 dim 截断后忘了重新归一化是这个症状的典型来源。"
        )


def postprocess_vectors(raw: np.ndarray, dim: int, *, source: str) -> np.ndarray:
    """后端给的原始向量 → 可以落盘/入库的向量。**五个后端共用的那一条路**。

    顺序固定：**截断 → 查有限性 → 归一化 → 复核**。中间的每一步都有它非在那儿的理由，
    写在各自函数的文档里；这里只说顺序：截断必须在归一化之前（归一化之后截断得到的
    不是 MRL 截断，而是一个长度小于 1 的向量）。
    """
    arr = np.asarray(raw)
    if arr.ndim != 2:
        raise EmbedError(f"{source} 返回的向量不是二维的：shape={arr.shape}")
    if arr.shape[1] < dim:
        raise EmbedError(
            f"{source} 返回的向量只有 {arr.shape[1]} 维，而要截到 {dim} 维。"
            "'截断'只会让它变短——维数不够说明 [embed] dim 配错了，或者后端的池化层换了。"
        )
    vecs = np.ascontiguousarray(arr[:, :dim], dtype=np.float32)
    check_finite(vecs, source=source)
    vecs = l2_normalize(vecs, source=source)
    check_unit_norm(vecs, source=source)
    return vecs


# ==============================================================================
# 批处理
# ==============================================================================


def iter_batches(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """把一批文本切成每片 `size` 条。`size < 1` 当场报错（否则下面的 `range` 会**空转**，
    返回一个什么都不产出的迭代器——那是一次不报错的空跑）。"""
    if size < 1:
        raise ValueError(f"批大小至少是 1，拿到 {size}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ==============================================================================
# 接口
# ==============================================================================


@runtime_checkable
class Embedder(Protocol):
    """文本 → 单位长度向量（计划 §4.2 的扩展点）。GUI 与将来的多模态后端都接这一层。

    约定里有两件**不写在类型里**的事：

    * 返回值一律是 `(n, dim)` 的 `float32`、L2 归一化过的（`postprocess_vectors` 保证）；
    * `encode_documents` 收一个 `title` 序列，是因为文档前缀里有标题槽（模块文档第二节）。
      `titles` 与 `texts` **等长**，逐条对应；不关心标题时传 `None`。
    * **`document_input()` 是"这条文档实际喂给模型的字符"的唯一出口**（`F5-03` 起）。
      谁要按"文本"做键——缓存（`index/vector_cache.py`）、对账、日志——就调它，**不要
      自己拼前缀**：两处各拼一遍的失败形态是静默的，见那个方法的文档串。
    """

    #: 向量维度（= `[embed] dim`）。
    dim: int
    #: 这个嵌入器用的两条前缀。暴露出来是为了让日志/测试能看见**实际用的是什么**。
    prefixes: TaskPrefixes

    @property
    def embed_ver(self) -> str: ...

    def document_input(self, text: str, title: str | None = None) -> str: ...

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray: ...

    def encode_documents(
        self, texts: Sequence[str], titles: Sequence[str | None] | None = None
    ) -> np.ndarray: ...

    def close(self) -> None: ...


class BatchEmbedder(ABC):
    """两个后端的公共那一半：**前缀、分批、后处置**。

    子类只实现 `_forward()`：一批已经加好前缀的文本 → 一批**原始**向量（原维数、
    未归一化）。这样切的原因很实在——两个后端各写一遍前缀拼接与归一化，就是两处会
    各自漂移的地方，而这两处出错**都不报错**。
    """

    def __init__(self, cfg: EmbedCfg, prefixes: TaskPrefixes) -> None:
        self.cfg = cfg
        self.prefixes = prefixes
        self.dim = cfg.dim
        self._closed = False

    #: 后端名（`onnx` / `sentence_transformers`），进日志用。
    name: str = "base"

    @property
    def embed_ver(self) -> str:
        return embed_ver(self.cfg)

    # ---------------------------------------------------------------- 子类实现

    @abstractmethod
    def _forward(self, texts: Sequence[str]) -> np.ndarray:
        """一批**已加前缀**的文本 → `(len(texts), 原生维数)` 的原始向量。"""

    def close(self) -> None:
        """放掉后端持有的资源。**默认什么也不做**，而不是报 `NotImplementedError`：
        有的后端（ONNX）确实没有需要显式释放的东西，让它们必须写一个空的 `close()` 是
        多余的仪式。**幂等**——调用两次不该出事。"""
        self._closed = True

    # ---------------------------------------------------------------- 公共入口

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        """查询侧：加查询前缀。空输入返回 `(0, dim)` 的空数组（**不是**报错：
        "一个查询都没给"是调用方的事，不该在这一层炸）。"""
        return self._encode([self.prefixes.query + t for t in _as_texts(texts)])

    def document_input(self, text: str, title: str | None = None) -> str:
        r"""这条文档**实际喂给嵌入器的那串字符**：文档前缀 + 正文。

        `F5-03` 加的。存在的理由是**它必须只有一处实现**：`encode_documents()` 用它拼
        输入，`index/vector_cache.py` 用它算缓存键。两处各拼一遍的失败形态是**静默**的
        ——算键那边少拼了前缀，于是"标题不同、正文相同"的两条文档撞成同一个键，第二条
        复用第一条的向量，**标题就被悄悄丢掉了**：搜不到、不报错。

        `title` 走 `TaskPrefixes.document_prefix()`，所以 `None`、空串、只有空白的
        标题得到的是**同一串字符**（`title: none`）——也就是说它们共享一个缓存键，
        这是对的：喂给模型的东西确实一模一样。
        """
        return self.prefixes.document_prefix(title) + text

    def encode_documents(
        self, texts: Sequence[str], titles: Sequence[str | None] | None = None
    ) -> np.ndarray:
        """文档侧：逐条加"标题+正文"前缀。`titles` 与 `texts` 必须等长。"""
        items = _as_texts(texts)
        if titles is None:
            title_list: list[str | None] = [None] * len(items)
        else:
            title_list = list(titles)
            if len(title_list) != len(items):
                raise ValueError(
                    f"titles 与 texts 不等长：{len(title_list)} 对 {len(items)}。"
                    "两条是逐条对应的，长度不等说明调用方把某一列的切片搞错了——"
                    "而错位之后每一条文档都会带上**别人**的标题，搜出来看着还挺像那么回事。"
                )
        prefixed = [
            self.document_input(text, title) for text, title in zip(items, title_list)
        ]
        return self._encode(prefixed)

    # ---------------------------------------------------------------- 内部

    def _encode(self, prefixed: list[str]) -> np.ndarray:
        if self._closed:
            raise EmbedError(f"{self.name} 后端的嵌入器已经 close() 过了")
        if not prefixed:
            return np.zeros((0, self.dim), dtype=np.float32)
        chunks: list[np.ndarray] = []
        for batch in iter_batches(prefixed, self.cfg.batch_size):
            raw = self._forward(batch)
            if len(raw) != len(batch):
                raise EmbedError(
                    f"{self.name} 后端对 {len(batch)} 条文本返回了 {len(raw)} 个向量"
                )
            chunks.append(postprocess_vectors(raw, self.dim, source=f"{self.name} 后端"))
        return np.concatenate(chunks, axis=0) if len(chunks) > 1 else chunks[0]


def _as_texts(texts: Sequence[str]) -> list[str]:
    """规范成 `list[str]`，并挡掉"传了一整个字符串"这个经典错误。

    传字符串进来时，Python 的 `Sequence[str]` 说得通，遍历会**逐字符**加前缀——于是
    一条查询变成十几个单字查询，返回十几个向量而不报错。这个坑值得一行代码。
    """
    if isinstance(texts, str):
        raise TypeError(
            "texts 收到的是一个字符串而不是字符串序列。逐字符加前缀会得到一堆单字向量，"
            "且不报错——请写成 [text]。"
        )
    return list(texts)


# ==============================================================================
# 工厂
# ==============================================================================


def resolve_backend(name: str) -> str:
    """`"auto"` → 生产后端（`D9`：`onnx`）。别的名字原样返回，由工厂去认。

    ⚠ 认不出来的名字**不在这一层报错**：`core/config.py` 的 `_ENUM_CHOICES` 已经挡过
    一道（写进 TOML 的非法值在那儿就炸了），走到这里还认不出来只可能是调用方直接传了个
    字符串——那种错该在工厂里报，好把"有哪些后端"一起说出来。
    """
    return DEFAULT_BACKEND if name == "auto" else name


def create_embedder(
    cfg: EmbedCfg,
    *,
    prefixes: TaskPrefixes | None = None,
    model_dir: str | Path | None = None,
) -> BatchEmbedder:
    """按 `cfg.backend` 造一个嵌入器。**唯一装配点**。

    后端的类是**函数内导入**的，这一条是有意的：`gemma_st.py` 的模块级 `import torch`
    要 2.4 秒、`import sentence_transformers` 再加 7.6 秒，`gemma_onnx.py` 要
    `onnxruntime`。放在模块级，就等于"只想用 ONNX 的一次 `lsearch search` 也要先把
    torch 拖进来"——`D9` 选 ONNX 的**首要理由**正是冷启动（0.50 秒对 5.89 秒），
    而模块级导入会把那个理由整个抵消掉。测试也因此能在没装 torch 的进程里跑 ONNX 那一条。

    `model_dir` 是给测试与将来的"模型放在别处"留的口子：给了就按它找快照，
    不给就按 `model_id` 去 HF 缓存里解析。`prefixes` 同理（给了就不去读模型文件）。
    """
    backend = resolve_backend(cfg.backend)
    if backend == "onnx":
        from localsearch.embed.gemma_onnx import OnnxEmbedder

        return OnnxEmbedder(cfg, prefixes=prefixes, model_dir=model_dir)
    if backend == "sentence_transformers":
        from localsearch.embed.gemma_st import GemmaSTEmbedder

        return GemmaSTEmbedder(cfg, prefixes=prefixes, model_dir=model_dir)
    raise EmbedUnavailable(
        f"不认识的嵌入后端 {cfg.backend!r}（解析后是 {backend!r}）。"
        f"可选：auto、{DEFAULT_BACKEND}、sentence_transformers。"
    )
