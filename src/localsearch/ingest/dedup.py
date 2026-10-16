r"""内容去重与引用计数（`D12`，P2 / `F2-04`）。测试落点 `tests/unit/test_dedup.py`。

一份内容只提取、分块、嵌入**一次**（`A9`），多个路径共用它。`ingest/fingerprint.py`
算出"这条是什么内容"，本模块管"这份内容现在被多少条路径指着"。

------------------------------------------------------------------------------
为什么引用计数不能省
------------------------------------------------------------------------------

`files.blob_id` 是多对一（计划 §4.4.1）。删一个文件时**不能**顺手把它的内容也删了：
那份内容可能还被别的路径指着。计划把这条写成了 `--prune` 的义务——"必须走计数路径，
不能直接按 path 删"。所以计数不是优化，是**正确性**：没有它，`--prune` 每跑一次就可能
静默删掉还有人在用的内容，而症状要到用户搜不到那个文件时才出现。

------------------------------------------------------------------------------
三个会静默出错的格子（都从这里出去）
------------------------------------------------------------------------------

**1. 重扫一遍不能把计数翻倍。** 同一个路径、同一份内容，重复 `put()` 必须**什么都不
做**。计数一翻倍，`remove()` 就永远减不到零，于是内容**永远不会被清理**——一个只涨
不跌的库，跑几个月后才有人发现。反方向也一样：真删了一条却记了两次，内容会**在还有
人引用的时候被删掉**。所以 `put()` 是**幂等**的，这一条是 `F6-03` 的 `--prune`
能不能用的前提。

**2. 同一条路径的内容变了，要同时**减旧**与**加新**，而旧的那一笔不能丢。** 文件改
了内容，它的 `blob_id` 要从旧内容换到新内容。只加不减会**泄漏**（旧内容永远减不到
零，切片与向量留一辈子）；只减不加会**丢掉引用**（新内容可能被人以为没人用了）。
`put()` 一次做两件事，并把旧内容那一笔从 `Put.replaced` 里**交还给调用方**——
"要不要去删旧内容本体"这个判断只能由调用方做（它才知道切片、FTS、HNSW 各自怎么清），
所以本模块只负责让那个判断**不可能被静默跳过**。

**3. `remove()` 一个不认识的路径不能当成功。** 返回 `None` 而不是"删掉了"。静默成功
会把"重复删除"与"记错了"这两种 bug 一起藏起来——而它们都会让计数偏小，于是内容
**在还有人引用的时候被删掉**。

------------------------------------------------------------------------------
与数据库的分工：本模块是纯结构，库里没有第二本账
------------------------------------------------------------------------------

上面三条语义全在"计数怎么变"上，与它是存在内存里还是存在 SQLite 里无关。所以本模块
给的是**纯内存的 `BlobRefs`**，语义在这里定死并测掉；P4 落地时库那一侧给出同样的答案。

⚠ **库那一侧不存计数。** 计划 §4.3 那句话（"`blobs` 表要存 `refcount`"）没有落地，
`schema.sql` 里**没有这一列**：引用数由 `SELECT count(*) FROM files WHERE blob_id = ?`
现算（走 `idx_files_blob`，`F4-01` 建的）。理由是一个只增不减的计数器比没有更坏——
它偏大的方向没有上限（进程崩在写库与写计数之间、`UPDATE` 漏掉一次），于是"没人要了"
这个判断永远不会为真，内容留在盘上一辈子；而 `COUNT(*)` 的偏只能是"库本身就有两行
指着它"，那种偏是事实、不是漂移。同一个理由让 `put_blob()` 的复用判断走的也是现查
（`SELECT id, parser_ver FROM blobs WHERE content_hash = ?`）。

⚠ **纯内存这一版只用于单轮索引**（扫一遍、去重、写库），跨轮的状态在库里。
它**没有**持久化、没有并发保护、也没有"事务失败后回滚"——那三条都归 P4 的库那一层，
**别拿它当跨进程的账本**。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Removal:
    """一条引用被删之后的结果：删掉的引用指向哪份内容，以及**那份内容是不是没人要了**。"""

    key: str
    content_hash: str
    #: 计数降到 0——**只有这一种情况允许去删内容本体**（切片、FTS 行、HNSW 的 tombstone）。
    last_reference: bool
    #: 删完还剩几条引用。
    refs_left: int


@dataclass(frozen=True, slots=True)
class Put:
    """`put()` 的结果。**三个字段是三种不同的事实，别互相推**：

    | 字段 | 回答 | 谁需要它 |
    |---|---|---|
    | `is_new_key` | 这条**路径**以前记账了吗 | 库那一层：要 INSERT 还是 UPDATE |
    | `replaced` | 它原来指着哪份内容、那份内容没人要了吗 | 清理：要不要删旧切片与向量 |
    | `content_is_new` | 这份**内容**值不值得处理 | `A9`：要不要提取与嵌入 |

    第一版把它们压成了两个字段、外加一个 `changed` 属性，结果是**"一条新路径指向一份
    已有的内容"这一格判不出来**：那时 `replaced` 是 `None`、`content_is_new` 是假，
    但记账**确实变了**（多了一条引用）。压缩字段的代价就长这样——被压掉的那个维度
    **只在某一格里显形**，而那一格是"多个文件是同一份内容的副本"这个最常见的场景。
    """

    #: 这条路径以前没在账上。
    is_new_key: bool
    #: 这条路径**原来**指着的内容（如果它改过），以及那份内容是不是没人要了。
    #: `None` = 新键，或者内容没变。
    replaced: Removal | None
    #: 这份内容是**新的**吗——`True` 就是"要提取、要嵌入"，`False` 就是"已经有了，
    #: 省一次"。**`A9` 的全部含义就在这一个布尔上。**
    content_is_new: bool

    @property
    def changed(self) -> bool:
        """这次 `put()` 有没有改变账目。

        **注意它说的是"账变了"，不是"内容变了"**：一条新路径指向一份早就见过的内容，
        账变了、内容没变。这两件事分不清，就会出现"把 `content_is_new` 当成
        '要不要写一条 files 行'来用"这种错——那样第二个副本**根本不会被记下来**。
        """
        return self.is_new_key or self.replaced is not None


class BlobRefs:
    """`路径 → 内容哈希`，外加每份内容的引用计数。

    **路径用 `ScanEntry.key`（规范形）而不是 `path`**：NTFS 大小写不敏感，
    `C:/Windows` 与 `c:/windows` 是同一个文件（`ingest/base.py` 里已经说过这件事）。
    用带大小写的路径当键，同一个文件会被记成两条引用，于是每一份内容都"多"一个引用、
    永远归不了零。**这就是"计数偏大"那条静默错误的入口。**

    **为什么只有一个 `put()`，没有单独的"新增"与"搬移"**：因为记账的对象是"**当前**
    盘上是什么"，而同一个路径的内容变了在一次增量扫描里是**常态**，不是异常。给这条
    路径抛异常的话，P6 每扫一次都要先问 `hash_of()` 再决定调哪个方法——把本模块的
    状态机泄漏给每个调用方，而调用方多的地方就是有人漏掉一个分支的地方。

    防泄漏不靠"不许这么调"，靠**返回值**：旧内容的 `Removal` 在 `Put.replaced` 里，
    调用方想忽略它得**显式地**忽略（`_ = refs.put(...)`），那在代码评审里看得见。
    """

    def __init__(self) -> None:
        #: 规范路径 → 内容哈希
        self._by_key: dict[str, str] = {}
        #: 内容哈希 → 引用条数。**归零即删项**，不留 `0`（见 `remove()`）。
        self._refs: dict[str, int] = {}

    # ---------------------------------------------------------------- 写

    def put(self, key: str, content_hash: str) -> Put:
        """把这条路径记成指着这份内容。**幂等**；内容换了就把引用搬过去。

        三种情形，返回值都能分开：

        - 没见过这个 `key` → 计数 +1，`replaced=None`，`content_is_new` = "此前没有
          别的路径指着这份内容"。
        - 已经指着**同一份内容** → **什么都不做**，`replaced=None`、`content_is_new=False`
          （内容当然不新）。这就是"重扫一遍不会把计数翻倍"（模块文档第 1 条）。
        - 已经指着**别的内容** → 先 `remove()` 掉旧引用（那个 `Removal` 从 `replaced`
          里返回，**别丢**：`last_reference` 决定要不要清旧内容的切片与向量），再按
          上面第一条加新引用。
        """
        old = self._by_key.get(key)
        if old == content_hash:
            return Put(is_new_key=False, replaced=None, content_is_new=False)
        replaced = self.remove(key) if old is not None else None
        self._by_key[key] = content_hash
        refs = self._refs.get(content_hash, 0) + 1
        self._refs[content_hash] = refs
        return Put(is_new_key=old is None, replaced=replaced, content_is_new=refs == 1)

    # ---------------------------------------------------------------- 减

    def remove(self, key: str) -> Removal | None:
        """删掉这条引用。不认识这个 `key` 就返回 `None`（模块文档第 3 条）。

        **只有 `last_reference` 为真时，调用方才可以去删内容本体**：切片、FTS 行、
        HNSW 里的 tombstone 标记。别的任何情况删了都会伤到还指着同一份内容的路径。
        """
        content_hash = self._by_key.pop(key, None)
        if content_hash is None:
            return None
        left = self._refs.get(content_hash, 0) - 1
        if left > 0:
            self._refs[content_hash] = left
        else:
            # 到 0 就把这一项**删掉**，不留 `0`。留着会让"内容哈希在不在表里"这个
            # 判据变成"它有没有出现过"，而并发/重放场景下那是个错误的判据。
            left = 0
            self._refs.pop(content_hash, None)
        return Removal(
            key=key,
            content_hash=content_hash,
            last_reference=left == 0,
            refs_left=left,
        )

    # ---------------------------------------------------------------- 问

    def refs_of(self, content_hash: str) -> int:
        """这份内容现在被几条路径指着。**没出现过与归零了都给 0**——调用方不需要
        区分这两者，需要区分的是"还能不能删"。"""
        return self._refs.get(content_hash, 0)

    def hash_of(self, key: str) -> str | None:
        """这条路径现在指着哪份内容。没记过就是 `None`。"""
        return self._by_key.get(key)

    @property
    def keys(self) -> int:
        """记了多少条路径。"""
        return len(self._by_key)

    @property
    def blobs(self) -> int:
        """有多少份内容还活着（计数 ≥ 1）。"""
        return len(self._refs)

    @property
    def total_refs(self) -> int:
        """引用总数。**恒等于 `keys`**，除非计数逻辑坏了——所以它有个用处：
        把两个数摆在一起，任何一个"计数偏了"的 bug 都会让它们对不上。"""
        return sum(self._refs.values())

    def duplicate_groups(self, *, min_refs: int = 2) -> dict[str, list[str]]:
        """内容 → 指着它的那些路径，只留 `≥ min_refs` 条的。

        **这是 `D12` 白赚的那条能力**（计划 §4.4.1）："找出所有重复文件"不需要额外
        遍历——按 `blob_id` 分组就是。将来做成 `lsearch dupes` 子命令时，本方法就是
        它的数据源；`A9` 的验收也要靠它把"真的合并了"摆出来给人看。
        """
        groups: dict[str, list[str]] = {}
        for key, content_hash in self._by_key.items():
            groups.setdefault(content_hash, []).append(key)
        return {h: ks for h, ks in groups.items() if len(ks) >= min_refs}
