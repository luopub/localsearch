r"""HNSW 向量索引（faiss），`F5-04`。测试落点 `tests/unit/test_vectors.py`。

这一层回答一个问题：**一批已归一化的向量，怎么放进去、怎么按相似度取回来。**

------------------------------------------------------------------------------
一、为什么是 HNSW，以及 int8 是**落盘**的量化（`D2` / `D5`）
------------------------------------------------------------------------------

`D2` 定的是 SQLite FTS5 + HNSW：关键词那头 `F4-02` 已经落地，这一层是向量那头。
`D5` 定的是 **768 原生维 + int8 量化落盘**——注意"落盘"两个字：嵌入器对外一律给
`float32`（`embed/base.py` 写着这一条），**把 768 个 float32 变成 768 个 int8 是这一层
的事**，而且是唯一一处做这件事的地方。

用的索引类型因此不是 `IndexHNSWFlat`（那是每个向量 3072 字节）而是
**`IndexHNSWSQ` + `QT_8bit`**（`META` 里那个 `quantize` 与之对应；`quantize = "none"`
时退成 `IndexHNSWFlat`，两条路都在，因为配置项允许两种取值）。

⚠ **量化范围是标定出来的，而"用什么标定"是一个会静默改变检索结果的决定**：
`QT_8bit` 给每一维学一个 `[min, max]` 区间，区间外的一律截断。本模块**用一组确定的
标定向量**（±e_i，见 `calibration_vectors()`）把每一维的区间钉成 `[-1, 1]`，
而不是拿"头一批真实向量"去学。理由有两条，第二条更硬：

* 向量是**已归一化**的（`embed/base.py` 的 `postprocess_vectors()` 保证），所以每一维
  的取值本来就在 `[-1, 1]` 里，拿 ±e_i 标出来的区间**恰好是这个上界**，不截断任何
  合法向量；
* 拿头一批标定的话，**索引的内容取决于"哪一批先来"**——同一份语料、同一个模型，
  因为索引顺序不同（比如中断续跑过），得到的量化区间不同、检索结果也就不同。
  那是一种**无法复现**的差异，而且它不会报错。

⚠ **代价要说清楚**：真实嵌入的每一维分布比 `[-1, 1]` 窄得多（本机实测那组合成语料
里标准差约 0.036），区间钉死在 `[-1, 1]` 等于把 8 位的精度用在了一个宽 20 多倍的
范围上。这在读数上是量得出来的：`E43` 记的是"同一批向量、同一套图参数下，
int8 与 float32 的 recall 差"。**不要把"没测出差别"当成"没有差别"**——
`F5-01` 量过一次"合成语料量不出前缀效应"的教训，同一条。

------------------------------------------------------------------------------
二、索引文件里**已经**有一半的元信息，别再抄一份
------------------------------------------------------------------------------

`dim`（`index.d`）、`M`（`index.hnsw.nb_neighbors`）、`efConstruction`
（`index.hnsw.efConstruction`）、已有的条数（`index.ntotal`）与索引类型
（`IndexHNSWSQ` / `IndexHNSWFlat`）**都在文件里**，`faiss.read_index()` 之后就看得见。
所以本模块**不给索引文件配一份 sidecar 元数据**——两份必然漂移，而漂移的方向是
"拿旧参数去查新索引"，不报错。

**不在**文件里的是两样，都要库里替它记着（`meta` 表的两行）：

* **"下一个 label 用几"**（[`META_NEXT_LABEL`]）。label 是 `chunks.vec_label` 的
  全部含义，重用一个已经用过的 label 会**静默**把两条不相干的切片指到同一个向量上；
* **"这个文件是哪一版模型建的"**（[`META_EMBED_VER`]）。少了它，换了模型之后
  新旧向量会混进同一个 HNSW——都是 768 维单位向量，搜索照常返回结果，只是排序
  是两套语义混着排。

两样都不是"顺手抄一份文件里已有的东西"，而是文件里**根本没有**的信息；`bind_embed_ver()`
与 `next_label()` 是会看这两行的那两个入口。

------------------------------------------------------------------------------
三、这一层不做的事
------------------------------------------------------------------------------

* **不分配 label**。`add()` 要调用方把 label 一起给进来（[`META_NEXT_LABEL`] 归写入
  那一趟管）。这一层只保证"同一批里 label 不重复"与"label 是 int64"。
* **不认识库**。本模块**不 import** `sqlite_store` / `vector_cache`：它只有 faiss 与
  numpy 两个依赖，因此能单测（`tests/unit/test_vectors.py` 里没有一条需要 SQLite）。
  一致性的核对（索引里的条数与缓存里的条目数对不对得上）在
  `check_consistency()`——它拿的是两个已经建好的对象，仍然不认识 SQLite。
* **不做删除与压缩**。`R7` 的 tombstone、`P6` 的重建，这一层一行都没有：`add()` 只有
  加、没有减。⚠ 这也意味着**索引文件只会变大**，`P6` 之前不要指望它自己回收。
* **不做归一化**。`add()` 会**检查**向量是不是归一化的（`check_unit_norm()`，超差当场
  抛），但不替你归一化：悄悄归一化会把"上游漏了一步"变成一个查不出来的偏差。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import faiss
import numpy as np

from localsearch.embed.base import check_unit_norm

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from collections.abc import Sequence

    from localsearch.core.config import Config

__all__ = [
    "INDEX_FILENAME",
    "META_EMBED_VER",
    "META_NEXT_LABEL",
    "VECTORS_RECIPE",
    "VectorHit",
    "VectorIndex",
    "VectorIndexError",
    "VectorIndexMissing",
    "VectorIncompatible",
    "VectorSpec",
    "bind_embed_ver",
    "calibration_vectors",
    "check_consistency",
    "next_label",
]

#: 索引文件名（落在 `Config.index_dir_path()` 那一层，与 `lsearch.db` 并排）。
#:
#: ⚠ **一个数据目录只有一个向量索引文件**。多模态（`P9`）要按模态分开建，
#: 那时这个常量会变成 `vectors-<modality>.faiss` 之类的形态；现在先不做，
#: 因为"按模态分开"要动的还有 `chunks.modality` 的检索侧。
INDEX_FILENAME = "vectors.faiss"

#: 建索引这件事的**配方号**。改了任何影响"向量怎么进索引"的实现之后把它加一。
#:
#: 与 `build.PARSER_RECIPE` 同一个套路、同一个失败形态：忘了加，旧索引留着、
#: 新参数没有生效，**且不报错**。它进 `meta`（`VECTORS_RECIPE` → `vectors.recipe`），
#: 打开索引时对不上就当场拒绝（`VectorIncompatible`）。
VECTORS_RECIPE = 1

#: `meta` 表里"下一个 label 用几"的键。
#:
#: ⚠ **为什么必须持久化一个计数器，而不是"用 `ntotal` 当下一个 label"**：
#: label 是 HNSW 里的 id，`chunks.vec_label` 与 `embed_cache.vec_label` 都指向它。
#: 用 `ntotal` 的话，只要**删过一次**（`P6` 的 tombstone 压缩、或将来任何一次
#: 真删除），条数就会掉回去，于是新向量拿到的 label 会与库里已有的撞上——而
#: `IndexIDMap2` **允许两个不同位置挂同一个 id**，撞了不报错，只是那条 label
#: 对应的切片从此可能取回**另一条**切片的向量。计数器只增不减，这个错就不存在。
META_NEXT_LABEL = "vectors.next_label"

#: `meta` 表里配方号的键。
_META_RECIPE = "vectors.recipe"

#: `meta` 表里"这个索引是拿哪一版模型建的"的键（值就是 `embed_ver`）。
#:
#: ⚠ **这是本文件唯一一处必须存在库里的信息**（`dim` / `M` / `efConstruction` /
#: 量化都能从文件里读出来，见模块文档第二节），但它非存在库里不可：`embed_ver`
#: 含模型 id 与配方（`embed/base.py` 的 `embed_ver()`），文件里**没有**它。
#: 没有它的后果是 `F5-04` 这一层最坏的一种错法——**换了模型之后新旧向量混在同一个
#: 索引里**：两边都是 768 维、都是单位向量、都能算内积，搜索照常返回结果，
#: 只是排序是"两套语义混着排"。它不报错，也无法从任何一条读数上看出来。
META_EMBED_VER = "vectors.embed_ver"

#: 标定向量的两倍维度是个上界（±e_i 各 768 条），够喂给 `train()`。
_CALIBRATION_SCALE: Final = 1.0


class VectorIndexError(Exception):
    """向量索引这一层的错误。**不继承 `BuildError`**：本模块不认识编排层。"""


class VectorIndexMissing(VectorIndexError):
    """要**读**的索引文件不存在（查询那一侧的常态：Tier2 还没跑过）。

    与"文件坏了"分开，理由在两个方向上都不对称：文件不存在是**可以预期的**
    （`D10` 的三级里 Tier2 是最后一趟，没跑过就该降级并标注），而文件坏了只能重建。
    调用方因此不能把两者写成一个 `except`。
    """


class VectorIncompatible(VectorIndexError):
    """磁盘上那个索引文件与当前配置**不是同一种东西**（维度/M/量化/配方对不上）。

    单开一个类型是为了让调用方能分开处理：这种情况**唯一安全的做法**是重建
    （删掉索引 + 清缓存 + 清 `chunks.vec_label`），而"文件读坏了"（`VectorIndexError`）
    没有这个选项。⚠ 无论如何都**不许**默默在旧文件上接着加——那样得到的是一个
    "里面有一半是按旧参数算的向量"的索引，而它查起来完全正常。
    """


@dataclass(frozen=True, slots=True)
class VectorSpec:
    """一个向量索引的建法：**这四个量必须与将要喂进来的向量对得上。**

    参数都来自配置（`[index] hnsw_m` / `hnsw_ef_construction` / `hnsw_ef_search`
    与 `[embed] dim` / `[embed] quantize`），但本类**不 import 配置模块**——
    `from_config()` 是唯一的桥。
    """

    dim: int
    m: int = 32
    ef_construction: int = 200
    ef_search: int = 64
    #: `int8`（`IndexHNSWSQ` + `QT_8bit`）或 `none`（`IndexHNSWFlat`）。
    #: ⚠ 它同时是 `embed_ver` 的一半（`F5-01` 把 `quantize` 拼进了版本串），
    #: 所以"改了量化"会让缓存整体失效——这正是想要的。
    quantize: str = "int8"

    def __post_init__(self) -> None:
        if self.dim <= 0:
            raise ValueError(f"维度必须是正数，拿到 {self.dim}")
        if self.m <= 0:
            raise ValueError(f"HNSW 的 M 必须是正数，拿到 {self.m}")
        if self.ef_construction <= 0 or self.ef_search <= 0:
            raise ValueError(
                f"efConstruction / efSearch 必须是正数，拿到 "
                f"{self.ef_construction} / {self.ef_search}"
            )
        if self.quantize not in _QUANTIZERS:
            raise ValueError(
                f"量化方式只有 {sorted(_QUANTIZERS)}，拿到 {self.quantize!r}"
            )

    @classmethod
    def from_config(cls, config: Config) -> VectorSpec:
        """从生效的配置摘出这一层要的四个量（`[index]` 与 `[embed]` 各出几个）。"""
        return cls(
            dim=config.embed.dim,
            m=config.index.hnsw_m,
            ef_construction=config.index.hnsw_ef_construction,
            ef_search=config.index.hnsw_ef_search,
            quantize=config.embed.quantize,
        )

    @property
    def is_quantized(self) -> bool:
        return self.quantize == "int8"

    def describe(self) -> str:
        """日志与报表用的一句话（**不进缓存键**，缓存键是 `embed_ver`）。"""
        return (
            f"{self.dim} 维 / M={self.m} / efC={self.ef_construction} / "
            f"量化={self.quantize}"
        )


@dataclass(frozen=True, slots=True)
class VectorHit:
    """一次查询命中的一条：label 与相似度。

    `score` 是**内积**（`METRIC_INNER_PRODUCT`），而库里的向量都是单位向量，
    所以它同时就是余弦相似度。

    ⚠ **`quantize = "int8"` 时它只是近似值，而且会略微超出 `[-1, 1]`**：本机实测
    拿库里的向量查它自己，返回的是 `1.0039`（量化再反量化带来的溢出，`E43`）。
    所以任何 `assert score <= 1.0` 都会在真实数据上炸，而"分数略大于 1"不是错误、
    是这一档量化的固有偏差。要精确的余弦就得用 `quantize = "none"`（`IndexHNSWFlat`，
    每向量 3072 字节，是 int8 的 2.9 倍）。

    ⚠ 也不要拿它去与 BM25 的分数比大小：两种分数的量纲无关，融合是 `F5-05` 的 RRF
    的事（`D2`）。
    """

    label: int
    score: float


#: `quantize` 取值 → 建索引的方式。`None` 表示不量化（`IndexHNSWFlat`）。
_QUANTIZERS: dict[str, int | None] = {
    "int8": faiss.ScalarQuantizer.QT_8bit,
    "none": None,
}


def calibration_vectors(dim: int) -> np.ndarray:
    r"""给 `QT_8bit` 标定用的 `2 * dim` 条向量：每一维各取一次 `+1` 与 `-1`。

    形状 `(2 * dim, dim)`、`float32`。理由写在模块文档第一节：它把每一维的量化区间
    钉成 `[-1, 1]`，也就是**单位向量每一维的数学上界**，于是"用哪一批数据标定"
    这件事从索引里消失。⚠ 它是**确定的**（同样的 `dim` 永远得到同一个数组），
    这一点有测试钉着：标定一旦不确定，索引就不可复现，而不可复现不会报错。
    """
    if dim <= 0:
        raise ValueError(f"维度必须是正数，拿到 {dim}")
    cal = np.zeros((2 * dim, dim), dtype=np.float32)
    idx = np.arange(dim)
    cal[idx, idx] = _CALIBRATION_SCALE
    cal[dim + idx, idx] = -_CALIBRATION_SCALE
    return cal


class VectorIndex:
    r"""一个 HNSW 索引文件。**同一时刻只开一个**（faiss 的索引对象不是线程安全的）。

    用法（写入那一趟）：

    ```python
    index = VectorIndex.open(path, spec)         # 没有就新建，有就对参数
    index.add(vectors, labels)                   # 一批一批地加
    index.save()                                 # 原子落盘（临时文件 + 替换）
    ```

    查询那一趟用 `open()` + `search()`，**不要 `add()`**：查询侧不需要写权限，
    而"顺手加了一条"会让索引与 `embed_cache` 的条数对不上——`check_consistency()`
    会当场把它抓出来，但那已经是下一次索引的事了。
    """

    def __init__(self, path: Path, index: faiss.Index, spec: VectorSpec) -> None:
        self.path = path
        self._index = index
        self.spec = spec

    # ---------------------------------------------------------------- 打开 / 保存

    @classmethod
    def open(
        cls, path: str | Path, spec: VectorSpec, *, fresh: bool = False
    ) -> VectorIndex:
        r"""打开（或新建）`path` 上的索引。**参数对不上就抛 `VectorIncompatible`。**

        `fresh=True` 表示"**丢掉**已有的重建"——调用方必须在同一次流程里把
        `embed_cache` 与 `chunks.vec_label` 一起清掉（`F5-03` 那条警告：缓存说有、
        索引里没有，会让那批切片**永远不再被嵌**）。这里不替调用方做，因为这一层
        不认识库；`build.run_tier2()` 是唯一有权这么调的地方。

        ⚠ 文件存在但读不出来（写了一半、被别的进程占着）**不当成"没有"**：
        当成没有就会在损坏的文件上重新开始，于是原来那 400 万个向量无声无息地
        没了，而这一趟跑完之后索引看起来完全正常。
        """
        path = Path(path)
        if fresh or not path.exists():
            if fresh and path.exists():
                path.unlink()
            index = _new_index(spec)
            return cls(path, index, spec)
        return cls._read(path, spec)

    @classmethod
    def load(cls, path: str | Path, spec: VectorSpec) -> VectorIndex:
        r"""打开一个**必须已经存在**的索引。**查询那一侧只用这个。**

        ⚠ 查询**不许**新建文件：`open()` 在没有索引时会造一个空的出来，而"查一次
        就顺手在数据目录里留下一个空索引"会让下一次索引那一趟以为"索引已经在了"
        （`check_consistency()` 靠条数能看出来，但那已经是另一趟的事了）。更直接的
        理由是 `D10`：Tier2 没跑过时语义检索**本来就该降级并标注**，而不是在现场
        造一个空索引出来——那会把"还没建"变成"建了、里面什么都没有"。
        """
        path = Path(path)
        if not path.exists():
            raise VectorIndexMissing(
                f"向量索引还没建（{path} 不存在）。它由 `lsearch index --tier 2` 建出来，"
                "在那之前语义检索给不出结果——这一趟该降级并标注，不要新建一个空索引。"
            )
        return cls._read(path, spec)

    @classmethod
    def _read(cls, path: Path, spec: VectorSpec) -> VectorIndex:
        """读盘 + 对参数（`open()` 与 `load()` 共用的那一半）。"""
        try:
            index = faiss.read_index(str(path))
        except Exception as exc:  # faiss 抛的是 RuntimeError，消息里带文件名
            raise VectorIndexError(
                f"向量索引读不出来：{path}（{type(exc).__name__}: {exc}）。"
                "它可能是上一次运行写到一半留下的——确认之后删掉它重建，"
                "不要指望这一层替你把损坏的文件当成'还没有索引'。"
            ) from exc

        found = _spec_of(index)
        if found.dim != spec.dim or found.quantize != spec.quantize:
            raise VectorIncompatible(
                f"索引文件与配置不是同一种东西：文件里是 {found.describe()}，"
                f"配置要的是 {spec.describe()}。重建（会清空嵌入缓存与 chunks.vec_label）"
                f"或者把配置改回去，二者选一。"
            )
        # ⚠ M / efConstruction 与文件里的不一致**不算致命**：HNSW 的图已经建好了，
        # 改这两个只会影响"以后加进来的那些"。但它是必须说出来的不一致，因此留在
        # `spec` 上（调用方拿 `index.file_spec()` 与配置比对去决定要不要提一句）。
        #
        # ⚠ 设置要穿过 `IndexIDMap2` 那一层：`read_index()` 回来的是包装后的对象，
        # 它**没有** `.hnsw`（`AttributeError`，不是静默失效——这一点算运气好）。
        _unwrap(index).hnsw.efSearch = spec.ef_search
        return cls(path, index, spec)

    def file_spec(self) -> VectorSpec:
        """**文件里**实际记着的建法（与 `self.spec` 可能只在 M / efC 上不同）。"""
        found = _spec_of(self._index)
        return VectorSpec(
            dim=found.dim,
            m=found.m,
            ef_construction=found.ef_construction,
            ef_search=self.spec.ef_search,
            quantize=found.quantize,
        )

    def save(self) -> None:
        r"""原子落盘：先写 `*.tmp`，再 `os.replace()` 顶上去。

        ⚠ **不能直接 `faiss.write_index(path)`**：那个写不是原子的，中途断电会留下
        一个截断的文件，而它的症状与"索引还没建"长得一样（见 `open()` 的 ⚠）。
        临时文件名带 `.tmp` 后缀，与 `.lsearchignore` 无关、也不会被当索引文件打开。

        ⚠ **它必须排在"写库并提交"之前**，而不是之后。两种顺序各留一个窗口：

        * `save()` → `commit()`：崩在中间 ⇒ 索引多了这批向量、库里没有记录。
          后果是 `check_consistency()` 报的**孤儿**——浪费一点磁盘，`next_label_hint()`
          还能保证它们的号不被重发。**可以继续跑。**
        * `commit()` → `save()`：崩在中间 ⇒ 库里记着那批 label、索引里没有。
          `F5-03` 的缓存语义是"命中了就不再嵌"，于是那批切片**永远**拿不到向量，
          每次都少几条结果，而唯一能发现它的是下一次 `check_consistency()` 的
          **当场抛**——代价是整批重嵌。

        两个窗口的大小一样，后果不对称，所以顺序固定成先存索引、后提交。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        faiss.write_index(self._index, str(tmp))
        os.replace(tmp, self.path)

    def close(self) -> None:
        """放掉索引对象（faiss 的对象是 C++ 侧持有，靠引用计数回收）。"""
        self._index = None  # type: ignore[assignment]

    # ---------------------------------------------------------------- 读

    @property
    def ntotal(self) -> int:
        """索引里有几条向量。**等于去重后的文本数**，不是切片数。"""
        return int(self._index.ntotal)

    def ids(self) -> np.ndarray:
        r"""索引里现有的全部 id（按插入顺序，`int64`）。测试与自检用。

        ⚠ 走 `faiss.vector_to_array()`：`IndexIDMap2.id_map` 是一个 swig 的
        `Int64Vector`，`np.asarray()` 对它**不做转换**（会得到一个包着 swig 对象、
        `dtype=object` 的 0 维数组，而它的 `.max()` 返回的还是那个对象）——
        `int(np.asarray(...).max())` 会抛 `TypeError`，不是给出错的数。实测见
        `PROGRESS.md` §3 的 `E43`。
        """
        return faiss.vector_to_array(self._index.id_map).astype(np.int64, copy=False)

    def next_label_hint(self) -> int:
        r"""**仅凭索引文件**能安全发出的下一个 label（`max(ids) + 1`；空的给 0）。

        存在的理由只有一个：**跨进程崩溃时不让 label 重号**。`meta` 里的计数器
        （[`META_NEXT_LABEL`]）是权威的，但它跟着 SQL 事务走，而索引文件是**分别**
        落盘的——两者之间那个窗口里，索引可能已经多了一批向量、而计数器还停在
        `commit` 之前的值上。此时若只信计数器，下一趟就会**重发**那一批 label，
        而 `IndexIDMap2` 允许同一个 id 出现两次：一个 label 从此对应两个向量，
        取回来的是哪一条要看图的内部顺序。**不报错。**

        ⚠ 与 `ntotal` 不是一回事：中间删过（`P6`）或崩过之后两者不相等，而这里要的
        是"发出去过的最大号"。"发出去过"就够——多留几个空号只浪费一点 id 空间，
        重号才会毁掉一条记录。
        """
        ids = self.ids()
        return 0 if ids.size == 0 else int(ids.max()) + 1

    def search(self, vector: np.ndarray, k: int) -> list[VectorHit]:
        r"""查一条向量，返回至多 `k` 条（按相似度降序）。

        `vector` 必须是 `(dim,)` 或 `(1, dim)` 的 `float32` 单位向量；faiss 只接受
        后者，形状归一在这里做。⚠ **不足 `k` 条时 faiss 用 label `-1` 补位**，
        那些补位必须滤掉：`-1` 一旦流到调用方，它会被当成"某个 label"去库里查，
        而 `chunks` 里**没有** `vec_label = -1` 的行，于是那条命中凭空消失——
        不报错，只是少了一条。
        """
        q = np.ascontiguousarray(np.atleast_2d(vector), dtype=np.float32)
        if q.shape[1] != self.spec.dim:
            raise VectorIndexError(
                f"查询向量的维度是 {q.shape[1]}，索引是 {self.spec.dim} 维。"
                "这两者不同说明查询侧用的不是同一个 `[embed] dim`。"
            )
        if k <= 0:
            raise ValueError(f"k 必须是正数，拿到 {k}")
        scores, labels = self._index.search(q, k)
        return [
            VectorHit(label=int(label), score=float(score))
            for label, score in zip(labels[0], scores[0], strict=True)
            if label >= 0
        ]

    # ---------------------------------------------------------------- 写

    def add(self, vectors: np.ndarray, labels: Sequence[int]) -> int:
        r"""把一批向量连同它们的 label 加进索引，返回加进去的条数。

        三道当场抛的校验，每条都对应一个**不报错的**错法：

        * **条数不等**：`labels` 与 `vectors` 错位之后，每条切片都会挂上别人的向量
          （与 `CachePlan.record()` 同一条理由）→ `VectorIndexError`；
        * **同一批里 label 重复**：`IndexIDMap2` 允许一个 id 挂两个位置，取回来的是
          哪一条要看图的内部顺序 → `VectorIndexError`；
        * **维度不对**：faiss 会拿 `assert` 在 C++ 侧崩，消息里没有"是这一批坏了"
          → `VectorIndexError`；
        * **不是单位向量**：内积就不再等于余弦，`VectorHit.score` 从此是另一个量纲，
          而检索**看着仍然正常**（排序大体是对的，只是分数不可解释）→ 这一条抛的是
          `EmbedError`，不是本模块的异常：判据在 `embed/base.py` 的
          `check_unit_norm()` 那一份，**不在这里抄第二份**（抄一份的下场是两处阈值
          慢慢漂开，而"哪一份在说话"看不出来）。调用方要接就接 `EmbedError`。
        """
        arr = np.ascontiguousarray(np.atleast_2d(vectors), dtype=np.float32)
        ids = np.asarray(labels, dtype=np.int64)
        if arr.shape[0] != ids.shape[0]:
            raise VectorIndexError(
                f"向量条数与 label 条数不等：{arr.shape[0]} 对 {ids.shape[0]}。"
                "两条是逐条对应的，长度不等说明调用方把某一列搞错了。"
            )
        if arr.shape[0] == 0:
            return 0
        if arr.shape[1] != self.spec.dim:
            raise VectorIndexError(
                f"这一批向量是 {arr.shape[1]} 维，索引是 {self.spec.dim} 维"
            )
        check_unit_norm(arr, source="VectorIndex.add")
        if len(set(ids.tolist())) != ids.shape[0]:
            raise VectorIndexError(
                "同一批里 label 有重复。撞了的两个位置都挂着同一个 id，"
                "查出来的是哪一条要看 faiss 的内部顺序——**不报错**。"
            )
        self._index.add_with_ids(arr, ids)
        return int(arr.shape[0])

    # ---------------------------------------------------------------- 自述

    def __repr__(self) -> str:
        return (
            f"<VectorIndex {self.path.name} ntotal={self.ntotal} "
            f"{self.spec.describe()}>"
        )


def _new_index(spec: VectorSpec) -> faiss.Index:
    """按 `spec` 造一个空索引。`IndexIDMap2` 那一层是为了能挂自己的 label。"""
    metric = faiss.METRIC_INNER_PRODUCT
    qtype = _QUANTIZERS[spec.quantize]
    if qtype is None:
        sub: faiss.Index = faiss.IndexHNSWFlat(spec.dim, spec.m, metric)
    else:
        sub = faiss.IndexHNSWSQ(spec.dim, qtype, spec.m, metric)
    sub.hnsw.efConstruction = spec.ef_construction
    sub.hnsw.efSearch = spec.ef_search
    # ⚠ `IndexHNSWSQ` 本身**不支持 `add_with_ids`**（faiss 的 `Index::add_with_ids`
    # 在它上面直接抛 "add_with_ids not implemented for this type of index"），
    # 所以外面必须套一层 `IndexIDMap2`。套了之后 `index.index` 才是那个 HNSW，
    # 读 `hnsw.*` 参数要先 `downcast_index()`——这一层封装把这件事收在
    # `_spec_of()` 里，调用方不必知道。
    wrapped = faiss.IndexIDMap2(sub)
    # ⚠ 标定必须在**任何 add 之前**做一次：`QT_8bit` 的每一维区间是训练学出来的，
    # 没训练就 add，faiss 会把这些向量的范围当成"区间"（等效于用头一批标定，
    # 正是模块文档第一节否掉的那条路）。`none` 不量化，也就不需要标定。
    if qtype is not None:
        wrapped.train(calibration_vectors(spec.dim))
    return wrapped


def _unwrap(index: faiss.Index) -> faiss.Index:
    """剥掉 `IndexIDMap2` 那层包装，拿到里面的 HNSW。"""
    inner = index.index if hasattr(index, "index") else index
    return faiss.downcast_index(inner)


def _spec_of(index: faiss.Index) -> VectorSpec:
    r"""从**已加载的索引**里读出它的建法（模块文档第二节：这些量文件里都有）。

    ⚠ 三处 faiss API 的形态，都实测过（`E43`）：

    * `hnsw.efConstruction` / `hnsw.efSearch` 是**属性**（可读可写），不是方法；
    * `hnsw.nb_neighbors(layer_no)` 是**方法**，要传层号——写成属性会得到一个
      方法对象，`int()` 它抛 `TypeError`（不是给出错的数，这一点算运气好）；
    * **第 0 层的邻居数是 `2 * M`**（faiss 的 `set_default_probas()` 就是这么定的：
      第 0 层 2M、其余层 M）。所以要拿回用户写的那个 M 得**除以 2**——直接把
      `nb_neighbors(0)` 当成 M 会让"文件里记着 M=64"这句话变成一句谎话，
      而 `VectorSpec.__post_init__` 只检查 M > 0，不会拦住它。
    """
    inner = _unwrap(index)
    quantized = isinstance(inner, faiss.IndexHNSWSQ)
    return VectorSpec(
        dim=int(inner.d),
        m=int(inner.hnsw.nb_neighbors(0)) // 2,
        ef_construction=int(inner.hnsw.efConstruction),
        ef_search=int(inner.hnsw.efSearch),
        quantize="int8" if quantized else "none",
    )


def check_consistency(index: VectorIndex, cache: object) -> int:
    r"""核对 索引条数 与 嵌入缓存条目数，**返回孤儿向量的条数**。

    为什么这个等式（几乎）成立：`embed_cache` 每个**唯一文本**一行、`vec_label`
    指向索引里的一个位置；索引里每个位置对应一个唯一文本。两者的条数是同一个东西的
    两种数法。**差的那一头只在索引那边**：

    * `ntotal >= cached`——多出来的是**孤儿向量**：索引里有、库里没有哪一行指着它。
      它由"先存索引、后提交 SQL"这个**刻意**的顺序造出来（见 `save()` 那一段）：
      两次落盘之间崩溃，索引就多了这一批，而它们在库里没有记录。**无害**（浪费一点
      磁盘，而 `next_label_hint()` 保证它们的号不会被重发），所以这里**返回**那个差、
      让调用方报出来，不当作错误。
    * `ntotal < cached`——**当场抛**。那是要命的那个方向：缓存里那些 label 指向
      索引里不存在的位置，而 `F5-03` 的缓存语义是"命中了就不再嵌"。于是那批切片
      永远拿不到向量，查询时命中一个空号、结果里就少一条——**每一次都少，且不报错**。
      它只可能来自"删过索引文件 / 换过索引文件 / 库回滚过"，三种都要人来看一眼。

    ⚠ 它**故意收下一个 `object`**、只要求对方有 `count()`：这一层不认识
    `VectorCache`（也就不 import 它），而可以被 `F5-04` 的编排、`doctor`、测试三处
    复用同一个判据。这与 `SqliteStore.find_blob()` / `put_blob()` 那对"必须一致的
    WHERE"是同一个思路，只是这次把判据写成了一个能调用的东西。
    """
    cached = int(cache.count())  # type: ignore[attr-defined]
    if index.ntotal < cached:
        raise VectorIndexError(
            f"嵌入缓存比向量索引还多：缓存 {cached} 条、索引只有 {index.ntotal} 条。"
            "缓存里那些多出来的 label 指向索引里不存在的位置，而缓存语义是"
            "「命中了就不再嵌」——那批切片会永远拿不到向量，查询时少几条，且不报错。"
            "两条路：确认索引文件是不是被删过/换过，然后重建这一层"
            "（`lsearch index --tier 2 --rebuild`）。"
        )
    return index.ntotal - cached


def bind_embed_ver(store: object, index: VectorIndex, *, embed_ver: str) -> str | None:
    r"""确认这个索引文件是**拿哪一版模型**建的，返回被换掉的那一版（没有就是 `None`）。

    三种情形，只有一种会往下走：

    * 库里记着同一版 → 什么都不做；
    * 库里**没记过**（第一次建）→ 记上，但**只在索引是空的时候**。一个有向量却说不清
      是哪一版模型建的索引，既不能接着用也不能猜（猜错就是把两套语义混在一起，
      见 `META_EMBED_VER`），所以那样的情况当场抛；
    * 库里记着**另一版** → 抛 `VectorIncompatible`。⚠ 这一条是 `F5-04` 最要紧的
      一道闸：换了模型（`embed_ver` 变了）之后，旧缓存因为版本不同而全部失配、
      会被重嵌一遍，而**索引里那些旧向量不会自己消失**——不拦住的话，新旧向量
      从此混在同一个 HNSW 里，两边都是 768 维单位向量，搜索照常返回结果。

    ⚠ 它收 `store` 而不是 import `SqliteStore`（模块文档第三节）。
    """
    if not embed_ver:
        raise ValueError("embed_ver 不能为空——它就是这道闸要拦的那个东西")
    stored = store.get_meta(META_EMBED_VER)  # type: ignore[attr-defined]
    if stored is None:
        if index.ntotal != 0:
            raise VectorIncompatible(
                f"{index.path} 里有 {index.ntotal} 条向量，但库里没有记它是哪一版模型"
                f"建的（`meta` 的 {META_EMBED_VER} 是空的）。这一层不能猜：猜错就是"
                "两套语义混在同一个索引里，而搜索照常返回结果、只是排序说不清。"
                "重建（`lsearch index --tier 2 --rebuild`）或者把那一行 meta 补回去。"
            )
        store.set_meta(META_EMBED_VER, embed_ver)  # type: ignore[attr-defined]
        return None
    if stored == embed_ver:
        return None
    raise VectorIncompatible(
        f"索引文件是 `{stored}` 建的，现在要用的模型是 `{embed_ver}`。"
        "两版的向量不能混在同一个索引里（都是 768 维单位向量，混了照样能搜、"
        "只是两套语义混着排序）。要换模型就得重建向量那一层："
        "`lsearch index --tier 2 --rebuild`——那会把整库切片重嵌一遍"
        "（`A1` 那套两万文件的基准集上这一趟是数小时到一天，见 `delete_vectors()` 的文档）。"
    )


def next_label(store: object, *, hint: int = 0) -> int:
    r"""下一个该发的 label：`meta` 里记的那个数 与 `hint` 取**大**。

    没有记过就是 0。`hint` 该传 `VectorIndex.next_label_hint()`——它是"索引文件里
    实际发出去过的最大号 + 1"，用来补上"索引已经落盘、计数器还没提交"那个窗口
    （见那个方法的文档）。两者取大，代价是崩溃之后可能跳过几个号，收益是**永不重号**。

    ⚠ **这里收一个 `object` 而不是 `SqliteStore`**：本模块不 import 库那一层
    （模块文档第三节），调用方把 `SqliteStore` 递进来即可。写入那一半
    （`set_meta(META_NEXT_LABEL, ...)`）归编排层——计数器必须与"向量进索引"用**同一条
    顺序**推进，否则会出现"label 已经发出去了、向量还没进去"的缺口。
    """
    value = store.get_meta(META_NEXT_LABEL)  # type: ignore[attr-defined]
    current = 0 if value is None else int(value)
    return max(current, int(hint))
