r"""`F4-04`：`index/build.py` 的编排（批量、提交、断点续跑）与 `lsearch index` 的命令面。

关联验收项：**`A1`**（"中断后 `--resume` 能接续"）、**`A6`**（单个文件出事不中断整轮，
结局写进 `files`）、**`A8`** 的前半句（Tier0 做完就能按文件名搜到）、**`A9`**（同一份
内容只提取一次）。

本文件钉的是**静默错误**——它们不抛异常、不写日志，只在很久以后表现为"这一趟跑了
但库里什么都没变""续跑之后重复提取了 100 万个文件""大文件永远搜不到正文"：

* 目录被写进 `files` → 每一趟的失败报表里多出百万条假的失败（`content_hash()` 在目录上
  抛 `IsADirectoryError`），而"这一行是目录"在库里**看不出来**（`files` 没有这一列）；
* `--resume` 被实现成"要不要从头重跑"的开关 → 它其实什么都不用做（选择条件本来就挑
  没做完的），真做了就成了"每跑一次都重扫一遍全盘"；
* 批次没有推进（`set_file_extracted()` 写了 0 行）→ `while True` 永不停，占着机器不报错；
* `find_blob()` 与 `put_blob()` 的判断漂了 → 一行**声称有正文、其实一片都没有**的记录，
  搜不到且不报错；
* 超限文件（`NAME_ONLY`）记成 `metadata` → 它会被 Tier1 反复挑中、反复失败，而"这一类
  不做"这件事在库里没有落点；
* `parser_ver` 与实际的切法不符 → 换了参数之后旧切片永远留在库里（`F4-02` 的测试已经
  钉了 store 那一侧，这里钉**组装**那一侧）。

⚠ 这一组**不用 `--dry-run`**（那是 `test_cli_index_dryrun.py` 的地盘）：前半段全落在
内存库上，后半段（命令面）落在 `tmp_path` 里的一棵小树上。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from localsearch.cli.main import main
from localsearch.core.config import (
    ENV_CONFIG_PATH,
    ChunkCfg,
    Config,
    PathsCfg,
    PerfCfg,
    ScanCfg,
)
from localsearch.core.logging import reset_logging
from localsearch.core.models import ExtractedDoc
from localsearch.core.registry import Registry
from localsearch.extract import ExtractConfig, Extractor
from localsearch.index.build import (
    PARSER_RECIPE,
    STATUS_METADATA,
    BuildError,
    parser_ver,
    run,
    run_tier0,
    run_tier1,
)
from localsearch.index.sqlite_store import SqliteStore, store_path
from localsearch.ingest.base import ScanConfig, make_scanner
from localsearch.ingest.filters import IgnoreRules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEFAULT_CHUNK = ChunkCfg()

#: 受控提取器认领的自造扩展名。**不用 `.txt`**：真注册表里的 `plaintext` 也认它，万一
#: 哪天有人把真注册表塞进这一层，"两个提取器都认"会变成一条更难查的失败。
ZZZ = "zzz"


# ==============================================================================
# 受控的提取器：数"到底提取了几个文件"
# ==============================================================================


class CountingExtractor:
    """认领 `.zzz` 的提取器，把每次 `extract()` 记下来。

    `A9` 那一条（"同一份内容只提取一次"）**只能这样测**：`blobs` 的 `reused` 是
    `put_blob()` 说的，而走到那里时文件已经读进来、正文已经解析出来了——正好是 `A9`
    要避免的那件事（`index/build.py` 的模块文档里写了这个顺序）。所以判据必须是
    "提取器被调了几次"，而不是"库里复用了几次"。

    `boom_at` 用来做**中断**：第 N 次调用时抛 `KeyboardInterrupt`。它模拟的是一次真的
    Ctrl-C——`batch.extract_many()` 只接 `Exception`，所以这个异常会一路冒到调用方，
    与用户在跑的中途按了 Ctrl-C 是同一个形状。
    """

    name = "counting"

    def __init__(
        self,
        *,
        ext: str = ZZZ,
        text: str = "受控正文 关键词",
        boom_at: int | None = None,
    ) -> None:
        self.ext = ext
        self.text = text
        self.boom_at = boom_at
        self.calls: list[str] = []

    def supports(self, ext: str, mime: str) -> bool:  # noqa: ARG002 - 协议要求两个参数
        return ext == self.ext

    def extract(self, path: str) -> ExtractedDoc:
        self.calls.append(path)
        if self.boom_at is not None and len(self.calls) == self.boom_at:
            self.calls.pop()  # 这次不算"提过的文件"——它是被打断的那一个
            raise KeyboardInterrupt
        return ExtractedDoc(text=self.text, title=Path(path).stem, source=self.name)


def registry_with(extractor: CountingExtractor) -> Registry[Extractor]:
    registry: Registry[Extractor] = Registry("extractor")
    registry.register(extractor.name, extractor)
    return registry


# ==============================================================================
# 小树与配置
# ==============================================================================


def make_tree(tmp_path: Path, **files: str) -> Path:
    """铺一棵小树：`make_tree(tmp, **{"docs/a.txt": "…"})`。"""
    root = tmp_path / "tree"
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    root.mkdir(exist_ok=True)
    return root


def as_store_path(*parts: str) -> str:
    r"""`files.path` 的形态：`/` 分隔。

    ⚠ **必须过 `store_path()`**，不能只把几段用 `/` 拼起来：`str(tmp_path)` 在 Windows
    上是 `C:\Users\…`，而库里那份是 `C:/Users/…`（`store_path()` 干了这件事）。拿拼出来的
    串去比 `row.path`、去喂一个"只对某一条路径抛错"的 `content_hash()`，都会**静默比不中**
    ——第一版就是这么错的：那条本该失败的路径照样走了正常流程，测试却以为造好了条件。
    """
    return store_path("/".join(parts))


def config_for(tree: Path, data: Path, *, every: int = 2, max_file_mb: int = 64) -> Config:
    """`build.run()` 要的那一份配置。**不读 TOML**：`run()` 收的就是 `Config`。"""
    return Config(
        paths=PathsCfg(data_dir=str(data), roots=(str(tree),)),
        scan=ScanCfg(backend="scandir", max_file_mb=max_file_mb),
        perf=PerfCfg(commit_every_files=every),
    )


def scan_config_for(tree: Path, data: Path, *, max_file_mb: int = 64) -> ScanConfig:
    return ScanConfig(
        roots=(str(tree),),
        data_dir=str(data),
        backend="scandir",
        max_file_mb=max_file_mb,
    )


def run_t0(
    store: SqliteStore,
    tree: Path,
    data: Path,
    *,
    every: int = 2,
    max_file_mb: int = 64,
    now: int | None = None,
):
    """跑一趟 Tier0（这一层直接收 `scanner` + `rules`，所以每次都得现造）。"""
    return run_tier0(
        store,
        scanner=make_scanner(scan_config_for(tree, data, max_file_mb=max_file_mb)),
        rules=IgnoreRules.empty(roots=(str(tree),)),
        every=every,
        now=now,
    )


@pytest.fixture()
def store() -> SqliteStore:
    s = SqliteStore.in_memory()
    yield s
    s.close()


# ==============================================================================
# 1. Tier0：写哪些行、写多少、什么时候提交
# ==============================================================================


def test_tier0_writes_files_and_leaves_directories_out(
    tmp_path: Path, store: SqliteStore
) -> None:
    """**目录不留行**，而这一点必须能被看见（`stats.dirs` 与扫描器的账对得上）。

    写进去的话，Tier1 会对它算内容哈希、抛 `IsADirectoryError`、记一笔失败——一棵小树
    上就多一条，全盘上是百万条假的失败（`build.py` 模块文档那一节）。而 `files` 里
    **没有"这一行是目录"这一列**，所以那种行在库里与一个 0 字节文件分不开。
    """
    tree = make_tree(tmp_path, **{"a.txt": "甲", "sub/b.txt": "乙"})
    stats = run_t0(store, tree, tmp_path / "data")

    assert store.count_files() == 2, "两个文件两行，目录一行都不该有"
    assert stats.dirs == 1, "没写行的目录数要记出来（报表靠它与'保留的目录'对上）"
    assert stats.dirs == stats.scan.dirs_kept, "这个数要与扫描器那份账一致"
    assert store.get_file(as_store_path(str(tree), "sub")) is None, "目录那一行不该存在"
    assert stats.kept == 2


def test_tier0_marks_over_limit_files_name_only(tmp_path: Path, store: SqliteStore) -> None:
    """超过 `max_file_mb` 的文件：**有行、名字能搜到、但正文这一轮不做**。

    记成 `skipped`（终态）而不是 `metadata`，两个后果都是必须的：它不会被 Tier1 每趟
    重新挑中（否则永远失败、永远重试），也不算进"正文就绪"的分母（否则全盘上任何一个
    压缩包都会让那句话永远为假）。两个后果都由下面的断言钉住。
    """
    tree = make_tree(tmp_path, **{"small.txt": "小的", "big.txt": "x" * 2048})
    stats = run_t0(store, tree, tmp_path / "data", max_file_mb=0)  # 0 = 全都只记名字

    row = store.get_file(as_store_path(str(tree), "big.txt"))
    assert row is not None and row.status == "skipped" and row.tier == 0
    assert stats.name_only == 2, "两个非空文件都超限"
    # 名字确实是搜得到的（这才叫"只记名字"）
    assert store.search_paths("big").hits, "只记名字的文件也必须能按名字搜到"
    # 它不在 Tier1 的活里
    assert list(store.iter_tier_work(1)) == []


def test_tier0_is_idempotent_on_a_second_pass(tmp_path: Path, store: SqliteStore) -> None:
    """第二遍：`new_rows = 0`、`known_rows = 全部`，行数与 FTS 都不变。

    这不是"什么都没做"（把 `INSERT OR IGNORE` 换成 `INSERT OR REPLACE` 就会变成"每跑
    一遍改写全库"，而症状只是慢），而是 Tier0 该有的样子。
    """
    # 文件名里带一个**只有它才有的**词：`files_fts.path_tok` 存的是整条路径，而
    # `tmp_path` 那串目录名里什么字母都可能有，用一个字的查询会撞上它（第一版就是这么
    # 错的：搜 "a" 搜到了两个文件，因为生成的临时目录名里有个 `a`）。
    tree = make_tree(tmp_path, **{"idemhit.txt": "甲", "plain.txt": "乙"})
    data = tmp_path / "data"
    first = run_t0(store, tree, data)
    second = run_t0(store, tree, data)

    assert (first.new_rows, first.known_rows) == (2, 0)
    assert (second.new_rows, second.known_rows) == (0, 2)
    assert store.count_files() == 2
    hits = store.search_paths("idemhit").hits
    assert [h.path for h in hits] == [as_store_path(str(tree), "idemhit.txt")], (
        "第二遍不该往 files_fts 里再插一份（重复的 FTS 行会搜出两条一样的）"
    )


def test_tier0_commits_every_n_files_and_once_more_at_the_end(
    tmp_path: Path, store: SqliteStore
) -> None:
    """每 `commit_every_files` 个提交一次，**最后不足一批的也要提交**。

    少最后那一次提交的症状是"跑完了但最后几百个文件没进库"——`close()` 的兜底只在正常
    退出时发生，而"跑了一整天然后被 kill"正是 `F4-04` 要伺候的场景。
    """
    tree = make_tree(tmp_path, **{f"f{i}.txt": str(i) for i in range(5)})
    stats = run_t0(store, tree, tmp_path / "data", every=2)
    assert stats.kept == 5
    assert stats.commits == 3, "5 个 / 每 2 个一批 = 2 批 + 收尾 1 次"


def test_tier0_uses_one_timestamp_for_the_whole_pass(
    tmp_path: Path, store: SqliteStore
) -> None:
    """`indexed_at` 是**整趟共用的一个时刻**：`files.indexed_at` 答的是"这一趟什么时候
    跑的"，不是"这个文件什么时候被记下的"。一次 `time.time()` 也比 100 万次便宜。"""
    tree = make_tree(tmp_path, **{"a.txt": "甲", "b.txt": "乙"})
    run_t0(store, tree, tmp_path / "data", now=1_700_000_000)
    stamps = set()
    for name in ("a.txt", "b.txt"):
        row = store.get_file(as_store_path(str(tree), name))
        assert row is not None
        stamps.add(row.indexed_at)
    assert stamps == {1_700_000_000}


# ==============================================================================
# 2. Tier1：提取、切片、写回结局
# ==============================================================================


def seed(tmp_path: Path, store: SqliteStore, **files: str) -> Path:
    """铺树 + 跑 Tier0，返回树根。下面每一条 Tier1 的测试都从这里开始。"""
    tree = make_tree(tmp_path, **files)
    run_t0(store, tree, tmp_path / "data")
    return tree


def run_t1(store: SqliteStore, extractor: CountingExtractor, *, every: int = 100):
    return run_tier1(
        store,
        registry=registry_with(extractor),
        extract_config=ExtractConfig(),
        chunk=DEFAULT_CHUNK,
        every=every,
    )


def parser_vers(store: SqliteStore) -> list[str]:
    """库里所有 blob 的 `parser_ver`。

    ⚠ 直接读 `_conn`：`SqliteStore` **没有**公开的"列出 blob"的入口（检索那一侧只需要
    `find_blob()` 与 `paths_for_blob()`）。为一个断言加一个公开方法，等于把内部结构固定
    下来——这里要钉的是"写进去的版本是不是算出来的那个"，走一次 `SELECT` 就够。
    """
    return [
        str(row[0])
        for row in store._conn.execute("SELECT parser_ver FROM blobs ORDER BY id")
    ]


def test_tier1_writes_chunks_and_stamps_the_parser_version(
    tmp_path: Path, store: SqliteStore
) -> None:
    """走完 Tier1 的样子：`status = fulltext`、`tier = 1`、`blobs` 上带着**组装出来的**
    `parser_ver`、切片进 `chunks`。

    `parser_ver` 由 `build.parser_ver(chunk)` 组装（提取配方 + 三个分块参数），断言的是
    "写进去的那个字符串与当前参数算出来的一致"——不一致的症状是换了参数之后旧切片永远
    留在库里（`F4-02` 的测试钉的是 store 那一侧的同一条）。
    """
    tree = seed(tmp_path, store, **{"a.zzz": "受控正文内容"})
    ext = CountingExtractor()
    stats = run_t1(store, ext)

    assert ext.calls == [as_store_path(str(tree), "a.zzz")]
    assert stats.tally.fulltext == 1 and stats.blobs_created == 1
    assert stats.chunks_written == 1
    row = store.get_file(as_store_path(str(tree), "a.zzz"))
    assert row is not None and row.status == "fulltext" and row.tier == 1
    assert row.blob_id is not None and row.content_hash is not None
    assert store.count_chunks(blob_id=row.blob_id) == 1
    assert parser_vers(store) == [parser_ver(DEFAULT_CHUNK)]
    # 正文确实可搜（这一行的 `fulltext` 不是谎话）。搜的是受控提取器那句台词里的词，
    # **不是文件里的字**——它不看文件内容。
    assert store.search_chunks("关键词").hits


def test_the_parser_version_shape_is_a_literal_contract() -> None:
    r"""`parser_ver` 的**字面形状**本身就是契约：`parse:<配方号>|chunk:<三个参数>`。

    ⚠ 为什么不能只比 `parser_ver(chunk)`（上面那条与 CLI 那条就是这么比的）：格式被改坏
    的时候**两边一起改**，测试照样绿。这不是假设——补这条正是因为它**真的漏过**：
    这一版 `f4-04` 的变体里本来有一条"把 `chunk` 段改成只留 `target_tokens`"，实测它
    **一条测试都没红**，因为三个断言都是拿 `parser_ver()` 现算一份来比的
    （同类的先例见 `tools/check_mutations.py` 里 `f4-01` 那条"第一版活下来了"）。

    这个字符串落进 `blobs.parser_ver`，是"这份切片该不该重切"的**唯一**判据；它一变，
    全库的 blob 都会走一遍 `replaced`（重切，`P5` 之后还要重嵌）。所以三个参数一个都不
    许从串里掉出去——`overlap_ratio` 那种"看起来像排版"的小数也在内。

    配方号那一半**用 `PARSER_RECIPE` 而不是写死 1**：把它加一是改了提取逻辑的人该做的
    那件**刻意**动作，那不该让这条测试红；该红的是"形状变了"。
    """
    assert parser_ver(DEFAULT_CHUNK) == f"parse:{PARSER_RECIPE}|chunk:512:0.15:200"
    assert parser_ver(ChunkCfg(max_chunks_per_doc=7)) == (
        f"parse:{PARSER_RECIPE}|chunk:512:0.15:7"
    )


def test_tier1_records_skipped_for_formats_nobody_claims_without_a_reason(
    tmp_path: Path, store: SqliteStore
) -> None:
    """没人认领的格式：`status = skipped`、**`error_msg` 为空**，且它退出 Tier1 的活集。

    `error_msg` 非空是"失败报表"的唯一判据，而全盘上不认识扩展名的文件多得是——给它们
    也填原因，报表里就只剩噪音（`extract/batch.py` 的模块文档写了这条分工）。
    """
    tree = seed(tmp_path, store, **{"a.txt": "甲"})  # 这一趟的注册表只认 .zzz
    stats = run_t1(store, CountingExtractor())

    row = store.get_file(as_store_path(str(tree), "a.txt"))
    assert row is not None and row.status == "skipped" and row.error_msg is None
    assert stats.tally.skipped == 1 and stats.error == 0
    assert list(store.iter_tier_work(1)) == [], "终态的行不该被下一趟再挑中"


def test_tier1_writes_the_reason_when_a_file_is_broken(
    tmp_path: Path, store: SqliteStore
) -> None:
    """坏文件：`status = error` + 一句带异常类名的原因（`A6` 的后半句）。

    用**真的 fixture 坏样本**（`F3-06` 造的 `broken.docx`）走**真的提取器**：这一条钉的
    是"结局写回 `files` 的那条路在真异常上也是通的"，与 `F4-02` 里那几个手造的 `Outcome`
    是两件事。
    """
    from localsearch.extract import build_registry

    tree = make_tree(tmp_path, **{"ok.txt": "好的"})
    shutil.copyfile(FIXTURES / "broken" / "broken.docx", tree / "broken.docx")
    run_t0(store, tree, tmp_path / "data")

    extract_config = ExtractConfig()
    stats = run_tier1(
        store,
        registry=build_registry(extract_config),
        extract_config=extract_config,
        chunk=DEFAULT_CHUNK,
        every=100,
    )
    row = store.get_file(as_store_path(str(tree), "broken.docx"))
    assert row is not None and row.status == "error"
    assert row.error_msg and "ParseFailed" in row.error_msg, f"原因要带类名：{row.error_msg!r}"
    assert stats.tally.error == 1
    # 坏文件旁边的那个文件照样做完了——**整轮不被打断**（A6）
    ok = store.get_file(as_store_path(str(tree), "ok.txt"))
    assert ok is not None and ok.status == "fulltext"


def test_the_same_content_is_extracted_only_once(tmp_path: Path, store: SqliteStore) -> None:
    """`A9`：同一份内容的第二个路径**不读盘、不提取**，直接记 `fulltext` 指向已有的 blob。

    判据必须是"提取器被调了几次"（见 `CountingExtractor` 的文档）。两个路径都要能搜到
    ——它们共享同一个 blob，所以那两行**确实**可搜，"记 fulltext"不是谎话。
    """
    # 两个文件的**字节**一模一样（`A9` 判的是 sha256），而提取出来的正文由受控提取器
    # 说了算（它不看文件内容）——所以下面搜的是它那句台词里的词，不是文件里的字。
    body = "一模一样的内容" * 10
    tree = make_tree(tmp_path, **{"a.zzz": body, "sub/b.zzz": body})
    run_t0(store, tree, tmp_path / "data")
    ext = CountingExtractor(text="共享正文 标记词")
    stats = run_t1(store, ext)

    assert len(ext.calls) == 1, f"同一份内容提取了不止一次：{ext.calls}"
    assert stats.reused == 1 and stats.blobs_created == 1
    first = store.get_file(as_store_path(str(tree), "a.zzz"))
    second = store.get_file(as_store_path(str(tree), "sub", "b.zzz"))
    assert first is not None and second is not None
    assert first.blob_id == second.blob_id
    assert second.status == "fulltext" and second.tier == 1
    assert first.blob_id is not None
    assert store.paths_for_blob(first.blob_id) == (first.path, second.path)
    # 复用的那一行**确实**可搜（它的切片是同一个 blob 的，`set_file_extracted()` 写
    # `fulltext` 不是谎话）——这一格是 `A9` 最容易写坏的地方：跳过提取之后忘了让这一行
    # 指着已有 blob，症状就是"文件在库里、正文一片都没有"，且不报错。
    assert store.search_chunks("标记词").hits


def test_a_file_that_cannot_be_hashed_is_one_file_s_ending(
    tmp_path: Path, store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """算 sha256 时读不出来（权限、扫描与提取之间被删、盘掉线）：**记一笔、接着跑**。

    它不是崩溃，与提取失败同一个档。⚠ 这一格单独计数（`stats.hash_errors`）：它连提取
    都没开始，混进 `tally.error` 里就没有人能回答"这些文件到底坏在哪一步"。
    """
    from localsearch.index.build import content_hash as original

    tree = seed(tmp_path, store, **{"gone.zzz": "甲", "ok.zzz": "乙"})
    broken = as_store_path(str(tree), "gone.zzz")

    def flaky(path: str) -> str:
        if path == broken:
            raise OSError("[WinError 32] 另一个程序正在使用此文件")
        return original(path)

    # 只改 `build` 模块里那一个名字（`_one_file` 就是在那里找它的）
    monkeypatch.setattr("localsearch.index.build.content_hash", flaky)
    ext = CountingExtractor()
    stats = run_t1(store, ext)

    row = store.get_file(broken)
    assert row is not None and row.status == "error" and row.tier == 0
    assert row.error_msg and row.error_msg.startswith("OSError:")
    assert stats.hash_errors == 1 and stats.error == 1
    assert ext.calls == [as_store_path(str(tree), "ok.zzz")], "另一个文件照样做完"
    ok = store.get_file(as_store_path(str(tree), "ok.zzz"))
    assert ok is not None and ok.status == "fulltext"


def test_a_batch_that_does_not_advance_stops_loudly(
    tmp_path: Path, store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_file_extracted()` 一行都没改 → **当场报错，不要空转**。

    这是一个**不抛异常的死循环**：每一批都重新问库、库里那一行还在最前面，于是
    `while True` 永远转下去，占着机器、日志里什么都没有。宁可停在半路——已经提交的
    批次是完好的（那是 `BuildError` 的文档）。
    """
    seed(tmp_path, store, **{"a.zzz": "甲"})
    monkeypatch.setattr(store, "set_file_extracted", lambda *a, **k: None)
    with pytest.raises(BuildError, match="没有推进"):
        run_t1(store, CountingExtractor())


def test_find_blob_and_put_blob_must_agree(
    tmp_path: Path, store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`find_blob()` 说"没有"、`put_blob()` 说"复用" → 报错，不许把没切片的行记成有正文。

    这两个方法的判据本该是同一个 `WHERE`（`find_blob()` 的文档写着这条耦合）。漂了之后
    跳过 `put_chunks()` 会在库里留下一行**声称有正文、其实一片都没有**的记录：搜不到，
    且不报错。这里把漂动造出来（先让那份内容进库，再让 `find_blob()` 一律说没有）。
    """
    body = "同一份内容"
    tree = make_tree(tmp_path, **{"a.zzz": body})
    run_t0(store, tree, tmp_path / "data")
    run_t1(store, CountingExtractor())  # 第一趟：blob 进库

    # 第二趟：把这一行改回"还没做正文"，同时让 find_blob() 假装库里没有
    path = as_store_path(str(tree), "a.zzz")
    store.set_file_extracted(path, status=STATUS_METADATA, tier=0)
    store.commit()
    monkeypatch.setattr(store, "find_blob", lambda *a, **k: None)
    with pytest.raises(BuildError, match="判断不一致"):
        run_t1(store, CountingExtractor())


# ==============================================================================
# 3. `run()` 与续跑
# ==============================================================================


def test_resume_skips_tier0_and_finishes_what_is_left(
    tmp_path: Path, store: SqliteStore
) -> None:
    """`A1` 的正面：**中断之后接着跑，不重扫盘、不重做已完成的文件**。

    造法：Tier1 跑到第 3 个文件时抛 `KeyboardInterrupt`（模拟 Ctrl-C），已经提交的那一批
    （`commit_every_files = 2`）留在库里；第二趟带 `resume=True`。断言三件事：`tier0 is
    None`（没扫盘）、剩下那个文件做完了、**前两个没有被重新提取**。
    """
    tree = make_tree(tmp_path, **{f"f{i}.zzz": f"内容{i}" for i in range(3)})
    data = tmp_path / "data"
    config = config_for(tree, data, every=2)
    scanner = make_scanner(scan_config_for(tree, data))
    rules = IgnoreRules.empty(roots=(str(tree),))

    with pytest.raises(KeyboardInterrupt):
        run(
            store,
            scanner=scanner,
            rules=rules,
            config=config,
            target_tier=1,
            registry=registry_with(CountingExtractor(boom_at=3)),
            extract_config=ExtractConfig(),
        )
    assert store.count_files(status="fulltext") == 2, "崩溃前那一批必须已经落盘"

    second = CountingExtractor()
    result = run(
        store,
        scanner=scanner,
        rules=rules,
        config=config,
        target_tier=1,
        resume=True,
        registry=registry_with(second),
        extract_config=ExtractConfig(),
    )
    assert result.tier0 is None, "--resume 不扫盘"
    assert result.tier1 is not None and result.tier1.work == 1
    assert len(second.calls) == 1, f"已完成的文件被重做了：{second.calls}"
    assert store.count_files(status="fulltext") == 3
    assert not result.has_failures


def test_resume_on_a_store_with_nothing_to_do(tmp_path: Path, store: SqliteStore) -> None:
    """没有可接续的东西时**什么都不做、也不报错**，但调用方得把这句话说出来。

    这一层只保证"没干活"，那句话（"这一趟没有可接续的东西"）在
    `cli/commands/index.py` 的 `render_build()` 里——所以这里钉的是那个前提：
    `work == 0`、`tier0 is None`，而**不是**抛错。
    """
    tree = make_tree(tmp_path, **{"a.zzz": "甲"})
    result = run(
        store,
        scanner=make_scanner(scan_config_for(tree, tmp_path / "data")),
        rules=IgnoreRules.empty(roots=(str(tree),)),
        config=config_for(tree, tmp_path / "data"),
        target_tier=1,
        resume=True,
        registry=registry_with(CountingExtractor()),
        extract_config=ExtractConfig(),
    )
    assert result.tier0 is None and result.tier1 is not None
    assert result.tier1.work == 0 and result.tier1.chunks_written == 0


def test_target_tier_zero_stops_before_the_body_pass(tmp_path: Path, store: SqliteStore) -> None:
    """`--tier 0`：只扫盘写名字，`tier1 is None`。

    这一格是"先让工具能用"那条路的开关（跑完全盘立刻能按文件名搜到），所以它必须真的
    停在 Tier1 之前，而不是"跑了但很快"。
    """
    tree = make_tree(tmp_path, **{"a.zzz": "甲"})
    result = run(
        store,
        scanner=make_scanner(scan_config_for(tree, tmp_path / "data")),
        rules=IgnoreRules.empty(roots=(str(tree),)),
        config=config_for(tree, tmp_path / "data"),
        target_tier=0,
        registry=registry_with(CountingExtractor()),
        extract_config=ExtractConfig(),
    )
    assert result.tier0 is not None and result.tier1 is None
    assert store.count_files(status=STATUS_METADATA) == 1
    assert store.count_chunks() == 0


# ==============================================================================
# 4. 命令面：`lsearch index`
# ==============================================================================

#: 规则文件的内容：**一条都不命中这棵树**。用它而不是仓库根那份真 `.lsearchignore`，
#: 是因为 `resolve_ignore_file()` 会把相对名字解析到仓库根下——那样跑出来的结果取决于
#: 仓库里那份文件写了什么，而它随时会变。
IGNORE_LINES = ("# 这一条也不命中任何东西", "*.never")


@pytest.fixture(autouse=True)
def clean_logging():
    """每个测试前后把 CLI 装到根 logger 上的 handler 摘干净（同 `test_cli.py`）。"""
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """一棵小树 + 一份配置 + 一份规则文件，全部落在 `tmp_path` 里。

    与 `test_cli_index_dryrun.py` 那份**不共用**（理由见那个文件的开头：`tests/` 下没有
    `__init__.py`，跨模块 import fixture 在不同跑法下行为不同）。树里的每一样东西都对应
    一条断言：`ok.txt` 认得出、`sub/deep.txt` 在子目录里、`broken.docx` 是真坏样本
    （退出码 1 全靠它）。
    """
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "ok.txt").write_text("可以提取的正文", encoding="utf-8")
    (tree / "sub" / "deep.txt").write_text("另一份正文", encoding="utf-8")
    shutil.copyfile(FIXTURES / "broken" / "broken.docx", tree / "broken.docx")

    ignore = tmp_path / "ignore.txt"
    ignore.write_text("\n".join(IGNORE_LINES) + "\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'roots = ["{tree.as_posix()}"]\n'
        "[scan]\n"
        # 绝对路径：见上面 IGNORE_LINES 那段（相对名字会被解析到仓库根）
        f'ignore_file = "{ignore.as_posix()}"\n'
        "[perf]\n"
        "commit_every_files = 2\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("COLUMNS", "300")
    return {"cfg": cfg, "data_dir": data_dir, "tree": tree, "ignore": ignore}


def cli(sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], *argv: str):
    """跑一条命令，返回 `(退出码, stdout, stderr)`。"""
    code = main(["--config", str(sandbox["cfg"]), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def flat(text: str) -> str:
    """压成一行再断言（rich 画的表格会折行，原文比对会飘）。"""
    return " ".join(text.split())


def db_of(sandbox: dict[str, Path]) -> Path:
    """`Config.db_path` 的落点（`data_dir/index/lsearch.db`）。"""
    return sandbox["data_dir"] / "index" / "lsearch.db"


def test_a_real_index_run_creates_the_database_and_a_log(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """真索引**要**留下落点：索引库、日志文件、数据目录。

    与 `--dry-run` 的"一样都没有"是同一个承诺的两面（`A7`）：`--dry-run` 不留任何东西，
    所以它必须先问"要不要干活"；真索引用同一份 `setup_logging()` 再把文件 handler 补上
    （它是幂等的），于是**不必**为了写日志而把 `index` 从 `LOG_FILE_EXEMPT_COMMANDS` 里
    删掉。文件日志这一半必须有：计划任务的输出被重定向之后，"任务跑过了但日志里什么都
    没有"与"根本没跑"长得一样。
    """
    code, out, err = cli(sandbox, capsys, "index")
    assert code == 1, "树里有一个坏文件：跑完了但有文件没成 → 退出码 1"
    assert db_of(sandbox).exists()
    logs = list((sandbox["data_dir"] / "logs").glob("*.log"))
    assert logs, "真索引必须留下日志文件"
    text = logs[0].read_text(encoding="utf-8")
    assert "索引开始" in text and "索引结束" in text, (
        "日志要有头有尾，否则分不出'跑过'与'跑到一半被打断'"
    )
    assert "有文件没成" in flat(out + " " + err), "有文件没成要说出来"


def test_a_real_run_keeps_going_when_one_file_is_broken(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`A6`：一个坏文件不带走整轮——另外两个文件照样有正文。

    在命令面上重新钉一次（`extract/batch.py` 那一层已经有测试），因为中间隔了写库、提交
    与报表三段，任何一段把失败当成"整轮失败"都会让这句话变成假的。
    """
    code, _out, err = cli(sandbox, capsys, "index")
    assert code == 1
    with SqliteStore.open(db_of(sandbox)) as store:
        assert store.count_files(status="fulltext") == 2
        assert store.count_files(status="error") == 1
        # 子目录里的那个文件也在（Tier0 是递归的）
        deep = store.get_file(as_store_path(str(sandbox["tree"]), "sub", "deep.txt"))
        assert deep is not None and deep.status == "fulltext"
    assert "有文件没成" in flat(err)


def test_the_config_ceiling_is_announced_not_refused(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """配置里 `semantic = true`（模板缺省）→ **压到 Tier1 并说出来**，不是拒绝。

    两件相反的处理，判据是"这句话是不是人说的"（与 `search/tier.py` 的 `explicit` 同
    一条）：默认配置不压低的话 `lsearch index` 开箱即报错；而**不说**的话，用户拿到的
    报告看起来是"索引跑完了"，Tier2 却是空的。
    """
    code, _out, err = cli(sandbox, capsys, "index")
    assert code == 1  # 树里有坏文件，退出码与这一条无关
    text = flat(err)
    assert "semantic = true" in text and "Tier2" in text and "P5" in text, text


def test_an_explicit_tier_2_is_refused(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """显式 `--tier 2`：**当场拒绝**，退出码 2，并且一个落点都不留。

    一个人明确要了向量那一级，说明他要的是"意思相近也能搜到"；默默做到 Tier1 停下，
    他拿到的报告是"索引跑完了"而 Tier2 是空的。宁可失败，不可错答。
    """
    code, _out, err = cli(sandbox, capsys, "index", "--tier", "2")
    assert code == 2 and "P5" in flat(err)
    assert not sandbox["data_dir"].exists(), "拒绝的那条路不许建数据目录"


def test_max_chunks_per_doc_lands_in_the_parser_version(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--max-chunks-per-doc` 真的进了 `parser_ver`（而它是"该不该重切"的判据）。

    盯的是**参数被声明了却没人用**这一类错：命令行覆盖在 `_effective_config()` 里合进
    配置，而 `parser_ver()` 与 `split()` 必须拿到**同一份** `ChunkCfg`——两份的话"写下的
    版本"与"实际的切法"就对不上了，症状是换了参数却永远不重切（不报错）。
    """
    code, _out, _err = cli(sandbox, capsys, "index", "--max-chunks-per-doc", "7")
    assert code == 1
    with SqliteStore.open(db_of(sandbox)) as store:
        vers = parser_vers(store)
    assert vers, "至少得有一个 blob"
    assert all(v == parser_ver(ChunkCfg(max_chunks_per_doc=7)) for v in vers), vers


def test_resume_without_a_database_refuses_and_says_what_to_run(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--resume` 而没有库：拒绝（退出码 2），并给出下一条该敲的命令。

    ⚠ 顺带钉住**拒绝与说明的先后**：拒绝的那条路走完就返回，所以那句"配置允许 Tier2"的
    说明**一句都不该出现**。混着排的话，一条 `--resume` 报"库不存在"的输出会先冒一句与
    它无关的语义提示，读起来像"配置有问题"，而真正的原因在第二行（实测见过一次）。
    """
    code, _out, err = cli(sandbox, capsys, "index", "--resume")
    text = flat(err)
    assert code == 2 and "没有可接续" in text
    assert "lsearch index" in text, "拒绝之后要给出下一条能敲的命令"
    assert "semantic = true" not in text, "拒绝的路径上不该冒出语义那一句说明"
    assert not sandbox["data_dir"].exists()


def test_self_contradicting_flags_are_refused_before_touching_anything(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """自相矛盾的参数组合在**读配置、开库、扫盘之前**就被拦下，且不留落点。

    这两种错误与磁盘状态无关，等跑了半小时才发现"这两个参数不能一起用"是最没有必要的
    浪费。`--tier 9` 那一格走的是另一条路（Click 的取值域），一并钉在这里。
    """
    for argv in (
        ("index", "--dry-run", "--resume"),
        ("index", "--resume", "--tier", "0"),
    ):
        code, _out, err = cli(sandbox, capsys, *argv)
        assert code == 2, argv
        assert "相反" in flat(err), argv
        assert not sandbox["data_dir"].exists(), argv
    code, _out, err = cli(sandbox, capsys, "index", "--tier", "9")
    assert code == 2 and "9" in flat(err), "越界的 --tier 由 Click 拦下（用法错误）"


def test_undeclared_options_are_unknown_rather_than_silently_ignored(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """计划 §4.6 里有、这一版**故意不声明**的几个参数：Click 报 `No such option`。

    `--workers`（嵌入的并行，`A2`/`P5`）、`--rebuild` / `--prune`（删除路径，`F6-03`）都
    不做。"声明了却忽略"这一类错的形态是**看起来完全正常**：`--workers 8` 给了、没反应、
    也不报错，用户会以为它并行跑过了。不声明 + 报错是刻意的，理由在 `index/build.py`。
    """
    for flag in ("--workers", "--rebuild", "--prune"):
        code, out, err = cli(sandbox, capsys, "index", flag, "1")
        assert code == 2, f"{flag} 不该被接受"
        assert "No such option" in flat(out + " " + err), flag
        assert not sandbox["data_dir"].exists()


def test_disabling_dedup_is_refused_rather_than_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`scan.dedup_by_content = false`：**拒绝**（这一版没有实现那条路）。

    照常跑的话用户以为去重关了，而库里其实开着——他的判断与库里的事实不一致，且没有任何
    地方能看出来。判据与 `backend = "mft"` 用不了时报错是同一条。
    """
    tree = make_tree(tmp_path, **{"a.txt": "甲"})
    data_dir = tmp_path / "data"
    ignore = tmp_path / "ignore.txt"
    ignore.write_text("\n".join(IGNORE_LINES) + "\n", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'roots = ["{tree.as_posix()}"]\n'
        "[scan]\n"
        f'ignore_file = "{ignore.as_posix()}"\n'
        "dedup_by_content = false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    code = main(["--config", str(cfg), "index"])
    err = capsys.readouterr().err
    assert code == 2 and "dedup_by_content" in flat(err)
    assert not data_dir.exists()
