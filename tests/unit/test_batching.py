r"""`embed/batching.py`：`W` 的内存账、进程池、退避、**每个 index 恰好交付一次**（`F5-02`）。

------------------------------------------------------------------------------
这一组盯的六件事
------------------------------------------------------------------------------

1. **`W` 是除出来的，不是按核数取的**——`worker_budget()` 的输入（可用内存、单 worker
   峰值、保留额）在测试里**全部注入**。理由不是"好写"：真去读本机的可用内存等于拿一台
   随机机器当测试环境（`E7`：两轮测量之间可用内存 4–8 GiB），而这条断言要断的是
   "分母变了结果就跟着变"。
2. **夹住要说出来**——配置里要 4 个、内存只放得下 1 个时，`workers` 取 1 **并且**
   `notes` 里有一条能让人看懂的说明。静默降级是这里唯一不能接受的形态。
3. **`W == 1` 真的就地跑**——没有子进程（探针文件里没有别的 pid），`worker_pid` 就是
   父进程自己。这是本机当前的实际分支，不是一段注释。
4. **`W > 1` 真的开进程**——探针里有别的 pid、每个 worker **只建一次**嵌入器、
   `worker_pid` 全部等于那个子进程。⚠ **不断言"两个子进程都在算"**：进程池把哪一批派给
   哪个 worker 是**竞态**（`ProcessPoolExecutor._adjust_process_count` 按"有没有空闲
   worker"起进程），拿它当断言就是一条会偶尔变红的测试。要钉的那件事是"算出来的东西
   来自**另一个进程**、而且那个进程只建了一次模型"，这两条都能确定地断言。
5. **退避不丢批、也不重复交付**——`MemoryError` 与 `BrokenProcessPool` 两条路各一条
   测试：worker 数减半、每个 `index` 仍然**恰好**出现一次、向量还能对上。
6. **非退避的错误整趟中止并收摊**——`EmbedUnavailable` 直接抛出去（不是跳过这一批），
   而且池被收掉了（再用一次就报"已经 close() 过了"）。

真模型（权重、tokenizer、两路对照）在 `test_embed_onnx.py` / `test_embed_gemma_st.py`：
真模型在本机只能跑起 1 个 worker（`A2` 的算术结果），"两个进程"这条根本没机会验。

------------------------------------------------------------------------------
为什么这里 `import pool_fakes`，而不是在 `tests/conftest.py` 里建 fixture
------------------------------------------------------------------------------

`test_cli_index_dryrun.py` 里写着一条约定：共用的东西走 conftest，不跨模块 import。
这一份是**明确的例外**，而且是被 `spawn` 逼出来的：子进程要能 `import` 出那个假的
嵌入器（进程池按名字 pickle 工厂与类），而 conftest 里的 fixture 是**没法**被子进程
import 的——它只活在 pytest 的收集结果里。所以 `pool_fakes.py` 是一个真模块，
靠的是 pytest 的 `prepend` 导入方式把 `tests/unit` 放进了 `sys.path`、而 `spawn`
会把父进程的 `sys.path` 一起传下去。`pool_fakes.py` 的模块文档里记着这条依赖。
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from localsearch.core.config import EmbedCfg
from localsearch.embed.base import EmbedError, EmbedUnavailable
from localsearch.embed.batching import (
    MIN_WORKERS,
    PER_WORKER_RSS_BYTES,
    RESERVE_BYTES,
    BatchRequest,
    EmbedPool,
    available_bytes,
    iter_requests,
    peak_rss_bytes,
    resolve_workers,
    total_bytes,
    worker_budget,
)
from pool_fakes import (
    CLOSE_PROBE_ENV,
    KILL_ENV,
    KILL_MARKER,
    PROBE_ENV,
    RAISE_ENV,
    RAISE_MARKER,
    UNAVAILABLE_MARKER,
    expected_documents,
    fake_factory,
    probe_lines,
    tiny_cfg,
)

GIB = 1024**3


# ==============================================================================
# 夹具与助手
# ==============================================================================


@pytest.fixture
def probes(monkeypatch, tmp_path) -> dict[str, str]:
    """四个回报文件的路径，靠环境变量传下去（`spawn` 的子进程继承 `os.environ`）。

    ⚠ **四个都设**、而不是按测试只设一个：`raise` / `kill` 那两条只在文本里出现对应
    标记时才触发，所以没用到它们的测试不会因此变味；反过来，漏设的后果是"触发静默
    失效、测试看起来绿"——正是这一组要防的那一类。
    """
    paths = {}
    for name, env in (
        ("probe", PROBE_ENV),
        ("close", CLOSE_PROBE_ENV),
        ("raise", RAISE_ENV),
        ("kill", KILL_ENV),
    ):
        path = tmp_path / f"{name}.txt"
        monkeypatch.setenv(env, str(path))
        paths[name] = str(path)
    return paths


def child_pids(probes: dict[str, str]) -> set[int]:
    """探针里出现过的、**不是本进程**的 pid。空集 = 一个子进程都没起。"""
    pids = {int(line.split()[0]) for line in probe_lines(probes["probe"])}
    return pids - {os.getpid()}


def two_worker_pool(cfg: EmbedCfg, **kwargs) -> EmbedPool:
    """一个"确定会开两个进程"的池：内存账的四个输入全部注入，不读本机。

    8 GiB 可用 − 2 GiB 保留 = 6 GiB，单 worker 记 1 GiB → `budget = 6`，
    写 2 就取 2，**不会**被夹住（被夹住的话这条路就悄悄退化成单进程了）。
    """
    return EmbedPool(
        cfg,
        workers=2,
        factory=fake_factory,
        available=8 * GIB,
        per_worker_bytes=1 * GIB,
        reserve_bytes=2 * GIB,
        **kwargs,
    )


def collect(pool: EmbedPool, requests: list[BatchRequest]) -> list:
    return list(pool.embed_documents(requests))


def assert_aligned(cfg: EmbedCfg, results: list, requests: list[BatchRequest]) -> None:
    """每条结果的向量必须是**它自己那个 index** 该有的向量。"""
    by_index = {request.index: request for request in requests}
    for result in results:
        request = by_index[result.index]
        want = expected_documents(cfg, request.texts, request.titles)
        assert result.vectors.shape == (len(request.texts), cfg.dim)
        assert np.allclose(result.vectors, want), f"index={result.index} 的向量串行了"


def assert_each_index_once(results: list, requests: list[BatchRequest]) -> None:
    """`F5-02` 的硬约束：每个 index 恰好出现一次（退避重投也不例外）。"""
    got = sorted(result.index for result in results)
    assert got == sorted(request.index for request in requests)
    assert len(got) == len(set(got))


# ==============================================================================
# 1. W 的内存账
# ==============================================================================


@pytest.mark.parametrize(
    ("available_gib", "per_worker_gib", "reserve_gib", "want"),
    [
        (8, 2, 2, 3),   # (8-2)/2
        (10, 2, 2, 4),
        (16, 4, 4, 3),
        (7, 2, 2, 2),   # 整除，不含余数
        (6, 2, 2, 2),
        (5, 2, 2, 1),   # 3//2 = 1，不是 2：宁可少开（向下取整，不四舍五入）
        (3, 4, 2, 1),   # 可用 > 保留、但整除得 0 → 仍然是 1，**不是 0**
        (2, 4, 2, 1),   # usable 恰好为 0
    ],
)
def test_budget_is_arithmetic(available_gib, per_worker_gib, reserve_gib, want) -> None:
    assert worker_budget(available_gib * GIB, per_worker_gib * GIB, reserve_gib * GIB) == want


def test_budget_ignores_cores() -> None:
    """同一个内存账，逻辑核从 4 换到 64，`W` 一个都不变（重定口径时的明确决定）。"""
    for cores in (4, 8, 64):
        plan = resolve_workers(
            0,
            available=8 * GIB,
            per_worker=2 * GIB,
            reserve=2 * GIB,
            logical_cores=cores,
        )
        assert plan.budget == 3
        assert plan.workers == 3


def test_budget_never_below_one() -> None:
    """"一个都放不下"不是一个可行结论：返回 1，让 `MemoryError` 去说话。

    ⚠ 最后一条**不能少**（2026-10-16 由 `tools/check_mutations.py` 当场抓到）：
    上面三条走的都是 `usable <= 0` 那条**提前返回**，一行都没碰到整除。把
    `max(MIN_WORKERS, …)` 拿掉之后它们照样绿——那处改动的存活就是这么来的。
    可用 > 保留、但**整除得 0** 才是这一行真正的边界。
    """
    assert worker_budget(1 * GIB, 4 * GIB, 2 * GIB) == 1
    assert worker_budget(0, 4 * GIB, 0) == 1
    assert worker_budget(2 * GIB, 4 * GIB, 2 * GIB) == 1  # usable 恰好为 0
    assert worker_budget(3 * GIB, 4 * GIB, 2 * GIB) == 1  # usable 1 GiB，1 // 4 = 0
    assert MIN_WORKERS == 1


@pytest.mark.parametrize("per_worker", [0, -1])
def test_budget_rejects_nonpositive_per_worker(per_worker) -> None:
    with pytest.raises(ValueError, match="单 worker 内存"):
        worker_budget(8 * GIB, per_worker, 2 * GIB)


def test_auto_explains_its_arithmetic() -> None:
    """`workers = 0`（自动）时必须说清"这个数是拿什么除出来的"。"""
    plan = resolve_workers(0, available=8 * GIB, per_worker=2 * GIB, reserve=2 * GIB)
    assert plan.requested == 0
    assert plan.workers == plan.budget == 3
    (note,) = plan.notes
    assert "8.00 GiB" in note and "2.00 GiB" in note and "= 3 个" in note
    assert "不按核数" in note
    assert (plan.available, plan.per_worker, plan.reserve) == (8 * GIB, 2 * GIB, 2 * GIB)


def test_explicit_request_is_clamped_and_says_so() -> None:
    plan = resolve_workers(4, available=4 * GIB, per_worker=2 * GIB, reserve=2 * GIB)
    assert plan.budget == 1
    assert plan.workers == 1
    (note,) = plan.notes
    assert "要 4 个" in note and "只放得下 1 个" in note and "被夹住" in note


def test_explicit_request_within_budget_stays_quiet() -> None:
    """放得下就照用户写的来，**不该**多出一条 note（噪声会让人学会忽略它）。"""
    plan = resolve_workers(2, available=8 * GIB, per_worker=1 * GIB, reserve=2 * GIB)
    assert plan.workers == 2
    assert plan.notes == ()


def test_negative_request_is_rejected() -> None:
    with pytest.raises(ValueError, match="负数"):
        resolve_workers(-1, available=8 * GIB, per_worker=1 * GIB, reserve=2 * GIB)


def test_threads_oversubscription_is_noted_but_not_changed() -> None:
    """`[embed] threads` 与 worker 数是相乘的：总线程数超核数要提醒，但**不替用户改**。"""
    hot = resolve_workers(
        2, available=8 * GIB, per_worker=1 * GIB, reserve=2 * GIB,
        threads=8, logical_cores=10,
    )
    assert hot.workers == 2
    assert any("线程" in note and "逻辑核只有 10" in note for note in hot.notes)

    cool = resolve_workers(
        2, available=8 * GIB, per_worker=1 * GIB, reserve=2 * GIB,
        threads=8, logical_cores=64,
    )
    assert cool.notes == ()


def test_machine_memory_readings_are_plausible() -> None:
    """不钉具体数字（它会随别的程序起落），只钉"读到了、而且是可信的量级"。"""
    total = total_bytes()
    available = available_bytes()
    assert total > 1 * GIB
    assert 0 < available <= total
    peak = peak_rss_bytes()
    if os.name == "nt":
        assert peak is not None and peak > 1 * 1024 * 1024
    else:  # pragma: no cover - 本机是 Windows
        assert peak is None


def test_prior_is_at_least_a_few_gib() -> None:
    """先验值只钉**量级**：它是"一个嵌入进程要多少内存"，写小了 `W` 就会开爆。

    ⚠ 这个数**准不准**测试断不出来（它是 `E41` 的读数，改了要拿
    `tools/measure_worker_rss.py` 复跑）。能断的是"它不是一个显然荒谬的小数"——
    而那一档错误（比如少写三位）恰好会让 `W` 从 1 变成 7，并且看起来一切正常。
    """
    assert PER_WORKER_RSS_BYTES > 1 * GIB
    assert RESERVE_BYTES > 0
    assert PER_WORKER_RSS_BYTES > RESERVE_BYTES


# ==============================================================================
# 2. 工作单位：切片与序号
# ==============================================================================


def test_iter_requests_slices_and_numbers() -> None:
    requests = list(iter_requests(["a", "b", "c", "d", "e"], size=2))
    assert [request.index for request in requests] == [0, 1, 2]
    assert [len(request.texts) for request in requests] == [2, 2, 1]
    assert requests[0].texts == ("a", "b")
    assert requests[2].texts == ("e",)
    assert all(request.titles is None for request in requests)


def test_iter_requests_honours_start() -> None:
    """`index` 是调用方给的序号（`F5-04` 靠它归位），所以 `start` 必须真的挪。"""
    requests = list(iter_requests(["a", "b", "c"], size=1, start=100))
    assert [request.index for request in requests] == [100, 101, 102]


def test_iter_requests_titles_follow_the_slice() -> None:
    """标题与切片**一起**切，错位的症状是每条文本带上别人的标题。"""
    requests = list(iter_requests(["a", "b", "c"], ["A", "B", None], size=2))
    assert requests[0].titles == ("A", "B")
    assert requests[1].titles == (None,)


def test_iter_requests_rejects_mismatched_titles() -> None:
    with pytest.raises(ValueError, match="不等长"):
        list(iter_requests(["a", "b"], ["only-one"], size=1))


def test_batch_request_rejects_mismatched_titles() -> None:
    with pytest.raises(ValueError, match="不等长"):
        BatchRequest(index=0, texts=("a",), titles=("t", "u"))


# ==============================================================================
# 3. W == 1：就地跑，不起子进程
# ==============================================================================


def test_single_worker_runs_in_this_process(probes) -> None:
    cfg = tiny_cfg()
    requests = list(iter_requests([f"文本 {i}" for i in range(5)], size=2))
    with EmbedPool(cfg, factory=fake_factory, available=3 * GIB) as pool:
        assert pool.workers == 1  # (3-2)/1 → 1，本机当前就是这个分支
        assert any("自动" in note for note in pool.notes)
        results = collect(pool, requests)
        assert child_pids(probes) == set(), "W == 1 不该开子进程"
        assert {result.worker_pid for result in results} == {os.getpid()}
        assert_aligned(cfg, results, requests)
        assert_each_index_once(results, requests)
        assert all(result.peak_bytes is None or result.peak_bytes > 0 for result in results)


def test_single_worker_builds_one_embedder_for_many_batches(probes) -> None:
    """一个进程一个嵌入器：`batch_size = 2`、10 条文本 → 5 个批、**1** 次构造。"""
    cfg = tiny_cfg()
    with EmbedPool(cfg, factory=fake_factory, available=3 * GIB) as pool:
        collect(pool, list(iter_requests([f"t{i}" for i in range(10)], size=2)))
    assert probe_lines(probes["probe"]) == [f"{os.getpid()} 32 2"]


def test_close_closes_the_local_embedder(probes) -> None:
    """`close()` 要把就地那个嵌入器也放掉（它占的正是那 2 GiB 量级的内存）。"""
    pool = EmbedPool(tiny_cfg(), factory=fake_factory, available=3 * GIB)
    collect(pool, list(iter_requests(["a"], size=2)))
    assert probe_lines(probes["close"]) == []
    pool.close()
    assert probe_lines(probes["close"]) == [str(os.getpid())]
    pool.close()  # 幂等：第二次不该出事，也不该再 close 一次
    assert probe_lines(probes["close"]) == [str(os.getpid())]


def test_use_after_close_is_rejected() -> None:
    pool = EmbedPool(tiny_cfg(), factory=fake_factory, available=3 * GIB)
    pool.close()
    with pytest.raises(EmbedError, match="已经 close"):
        list(pool.embed_documents([]))


def test_context_manager_closes() -> None:
    with EmbedPool(tiny_cfg(), factory=fake_factory, available=3 * GIB) as pool:
        collect(pool, list(iter_requests(["a"], size=2)))
    with pytest.raises(EmbedError, match="已经 close"):
        list(pool.embed_documents([]))


def test_a_clean_run_leaves_the_pool_reusable(probes) -> None:
    """一趟索引里有好几段：正常跑完**不**收摊，同一个池要能接着用。"""
    pool = two_worker_pool(tiny_cfg())
    try:
        first = list(iter_requests([f"a{i}" for i in range(3)], size=2))
        second = list(iter_requests([f"b{i}" for i in range(3)], size=2, start=10))
        results = collect(pool, first) + collect(pool, second)
    finally:
        pool.close()
    assert_each_index_once(results, first + second)
    assert child_pids(probes), "两段都该由子进程算（正常跑完不该收摊）"


# ==============================================================================
# 4. W > 1：真的开进程
# ==============================================================================


def test_two_workers_use_two_processes(probes) -> None:
    cfg = tiny_cfg()
    texts = [f"文档 {i}" for i in range(8)]
    titles = [f"标题 {i}" for i in range(8)]
    requests = list(iter_requests(texts, titles, size=2))
    pool = two_worker_pool(cfg)
    try:
        assert pool.workers == 2
        assert pool.notes == []  # 放得下就没什么好说的
        results = collect(pool, requests)
    finally:
        pool.close()

    pids = child_pids(probes)
    assert pids, "W == 2 必须有子进程在算"
    assert {result.worker_pid for result in results} == pids
    assert os.getpid() not in {result.worker_pid for result in results}
    assert_aligned(cfg, results, requests)  # 标题也要跟着走：`_encode_request` 传的是 titles
    assert_each_index_once(results, requests)
    if peak_rss_bytes() is not None:
        assert pool.observed_peak_bytes is not None
        assert pool.observed_peak_bytes > 0


def test_each_worker_builds_the_embedder_once(probes) -> None:
    """9 个批分给 2 个 worker：每个 worker 的探针行**恰好一行**（懒建 + 缓存）。"""
    cfg = tiny_cfg()
    requests = list(iter_requests([f"t{i}" for i in range(18)], size=2))
    with two_worker_pool(cfg) as pool:
        results = collect(pool, requests)
    assert_each_index_once(results, requests)
    lines = probe_lines(probes["probe"])
    pids = [line.split()[0] for line in lines]
    assert len(pids) == len(set(pids)), f"某个 worker 建了不止一个嵌入器：{lines}"
    assert set(pids) == {str(one) for one in child_pids(probes)}


def test_break_early_closes_the_pool(probes) -> None:
    """调用方 `break`（或 `GeneratorExit`）也要收摊，否则子进程会一直占着内存。"""
    cfg = tiny_cfg()
    requests = list(iter_requests([f"t{i}" for i in range(8)], size=2))
    pool = two_worker_pool(cfg)
    for _result in pool.embed_documents(requests):
        break
    with pytest.raises(EmbedError, match="已经 close"):
        list(pool.embed_documents([]))


# ==============================================================================
# 5. 退避：不丢批，也不重复交付
# ==============================================================================


def test_memory_error_halves_workers_and_retries(probes) -> None:
    cfg = tiny_cfg()
    texts = [f"t{i}" for i in range(8)]
    texts[3] = RAISE_MARKER + texts[3]  # 第 2 个批里那一条会抛 MemoryError，只抛一次
    requests = list(iter_requests(texts, size=2))
    pool = two_worker_pool(cfg)
    try:
        results = collect(pool, requests)
    finally:
        pool.close()

    assert_each_index_once(results, requests)
    assert_aligned(cfg, results, requests)
    assert pool.workers == 1, "退避必须真的减半（这里 2 → 1，之后就地跑）"
    assert any("减半到 1" in note and "不会丢" in note for note in pool.notes)


def test_broken_process_pool_halves_workers_and_retries(probes) -> None:
    """子进程被 `os._exit(1)` 打死 → `BrokenProcessPool`，走的是**同一条**退避。"""
    cfg = tiny_cfg()
    texts = [f"t{i}" for i in range(8)]
    texts[5] = KILL_MARKER + texts[5]
    requests = list(iter_requests(texts, size=2))
    pool = two_worker_pool(cfg)
    try:
        results = collect(pool, requests)
    finally:
        pool.close()

    assert_each_index_once(results, requests)
    assert_aligned(cfg, results, requests)
    assert pool.workers == 1
    assert any("BrokenProcessPool" in note for note in pool.notes)


def test_shrink_at_one_worker_raises_with_what_to_do(probes) -> None:
    """退到底还不行：抛 `EmbedError`，并且要说清人能做的三件事。

    ⚠ 必须带 `probes`：触发那条路要 `LSEARCH_FAKE_RAISE` 环境变量——忘了它这条测试
    会**在没有任何异常的情况下通过**（第一版就是这么写错的，pytest 报 DID NOT RAISE）。
    """
    texts = [RAISE_MARKER + "t0", RAISE_MARKER + "t1"]
    requests = list(iter_requests(texts, size=2))
    pool = EmbedPool(tiny_cfg(), factory=fake_factory, available=3 * GIB)
    with pytest.raises(EmbedError) as caught:
        collect(pool, requests)
    message = str(caught.value)
    assert "连一个嵌入" in message
    assert "batch_size" in message and "关掉别的程序" in message
    with pytest.raises(EmbedError, match="已经 close"):
        list(pool.embed_documents([]))


# ==============================================================================
# 6. 非退避的错误：整趟中止
# ==============================================================================


def test_unavailable_aborts_the_whole_run(probes) -> None:
    """`EmbedUnavailable` 是环境问题（`base.py` 写着调用方应当立刻停下整趟索引）。

    "逐条跳过"会把一次失败摊成两万次，所以它**不**进退避的捕获列表。
    """
    cfg = tiny_cfg()
    texts = ["fine 0", "fine 1", UNAVAILABLE_MARKER + "boom", "fine 3"]
    requests = list(iter_requests(texts, size=2))
    pool = two_worker_pool(cfg)
    with pytest.raises(EmbedUnavailable):
        collect(pool, requests)
    with pytest.raises(EmbedError, match="已经 close"):
        list(pool.embed_documents([]))
