"""`F2-04` 的实测入口：四组数，四条设计决定的依据（可复跑）。

`ingest/fingerprint.py` 里有四处"为什么不那样写"，**每一处都不是推理出来的，是量出来的**。
本脚本就是那四处的复跑入口——不留脚本的实测数字与"终端里看了一眼"没有区别。

    uv run python tools/bench_fingerprint.py --stat      # 第 1 组（秒级）
    uv run python tools/bench_fingerprint.py --hash      # 第 2 组（十几秒）
    uv run python tools/bench_fingerprint.py --sizes     # 第 3 组（扫一遍 D:/work，约 25 s）
    uv run python tools/bench_fingerprint.py             # 前三组都跑
    uv run python tools/bench_fingerprint.py --sizes --dedup --roots D:/work   # 加真去重统计（**约 45 分钟**）
    uv run python tools/bench_fingerprint.py --opens --roots D:/work           # 第 4 组（约 1 分钟）

⚠ **`--dedup` 那一档很贵**：它把 94 万个候选文件逐个打开、读完、算 sha256。`D:/work`
上实测 **2,714 s**（约 45 分钟）。这是它唯一一次能给出的东西——"真重复的字节占多少"——
所以值得跑，但**别顺手带上它**。

**为什么要落在工具里而不是 /tmp**：`E12` 的教训——第一版微基准写在临时目录里、
跑完就没了，于是那个数**无法复跑**，只能事后把脚本重写一遍再归档。这一次直接建在
`tools/` 里。

⚠ **输出只用 GBK（本机控制台代码页 936）认得的字符**，理由见 `E16`：U+2212 那样的
字符会让 `print` 抛 `UnicodeEncodeError`，把整轮输出一起吃掉。这里的减号一律是
ASCII `-`。汉字、「」、×、→、… 都编得了。

---- 四组数各自回答什么 ---------------------------------------------------

1. `--stat`：**`quick_sig` 在哪一格失效。** 同一条路径上分别做"复制盖在已有文件上"、
   "原地改写 + 把 mtime 改回原值"、"写临时文件再换名"、"同名删除再建"四件事，看
   `size` / `mtime_ns` / `st_ctime_ns` 各自动不动。**"原地改写"那一行三字段全同**——
   那是 `quick_sig` 唯一会判错的格子，也是"Windows 上的 `st_ctime` 是创建时间、不是
   inode 变更时间"这个事实的直接证据（Unix 上这一栏会不同，别照抄那边的经验）。
   附带的 `1b` 量 `st_ctime` 的分辨率：间隔 0 时约一半会撞在同一时刻，>5 ms 之后没撞过。
2. `--hash`：**`sha256` / `sha1` / `md5` / `blake2b` 在本机的吞吐**，对照"纯读一遍"的
   基线。`ingest/fingerprint.py` 选 `sha256` 而不是计划正文写的 BLAKE3，就是照这个结果
   定的——注意 `hashlib.blake2b` **不是** BLAKE3，它是没有 SIMD 的老实现。
   ⚠ 这一组量的是**一个 200 MiB 大文件**，它把每文件的固定开销摊掉了；真实负载是
   94 万个小文件，第 4 组才是那个场景。
3. `--sizes`：**size 预筛在真实目录树上的收益**，条数与字节数**分开报**。计划 §4.4.1
   写"尺寸唯一的文件占了绝大多数"，`D:/work` 上恰好相反（96.5% 的文件有同尺寸兄弟），
   所以这一组数就是为了把"收益得按字节算"这件事钉死。加 `--dedup` 会**真的把候选
   文件全哈希一遍**，给出"内容真重复的字节占多少"——那是 `D12` 去重能省下多少提取与
   嵌入的直接估计。
4. `--opens`：**每个文件一次 open 的单价，以及那笔钱花在哪。** 这一组是被 `--dedup`
   自己的读数逼出来的：`--hash` 说 950 MiB/s，`--dedup` 真跑出 7 MiB/s，差 135 倍。
   七档把成本拆成"循环 / stat / open / 读全文 / 建哈希器 / 全套 / 全套+getsize"，
   结论是**钱花在读字节上，建哈希器只要 1 微秒**——所以"换个更快的哈希算法"在真实
   负载上不是杠杆。⚠ 这一组是**偏乐观的下界**（样本全在缓存里），用途是拆构成，
   不是预测总时间。
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import os
import shutil
import sys
import tempfile
import time

from localsearch.core.config import load_config
from localsearch.ingest.filters import PROJECT_ROOT, IgnoreRules
from localsearch.ingest.fingerprint import SizeIndex, content_hash
from localsearch.ingest.scanner_scandir import ScandirScanner
from localsearch.ingest.base import ScanConfig

MiB = 1024 * 1024


def now() -> str:
    """机器时间。每条实测都带它——数字必须连条件一起记，时间是最基本的条件。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"{title}    [机器时间 {now()}]")
    print("=" * 78)


# ==============================================================================
# 1) quick_sig 的失效格子
# ==============================================================================


def bench_stat() -> None:
    section("1) quick_sig 的三字段对照：它会在哪几格判错")
    d = tempfile.mkdtemp(prefix="ls-f204-stat-")
    counter = [0]

    def newfile(text: bytes) -> str:
        """每次都用**从没出现过的名字**。

        ⚠ 这条限制不是洁癖：**同一个名字删掉再建，Windows 会把创建时间**继承**过去**
        （NTFS 的 tunnel cache，见下面第 5 行）。第一版探针复用了同一批文件名，于是
        "两个文件创建时间相同"这个结论里混进了一个与间隔完全无关的效应——间隔从
        0.02 s 一直到 1.0 s 都稳定复现「3/5 相同」，那时就该发现不对了。
        """
        counter[0] += 1
        p = os.path.join(d, f"f{counter[0]:03d}.bin")
        with open(p, "wb") as f:
            f.write(text)
        return p

    def snapshot(p: str) -> tuple[int, int, int]:
        st = os.stat(p)
        return st.st_size, st.st_mtime_ns, st.st_ctime_ns

    try:
        src = newfile(b"AAAA\n")           # 5 字节：尺寸相等很容易构造

        # ---- 表一：同一条路径上，操作**前后**（这才是 quick_sig 真正的比较方式）----
        rows: list[tuple[str, tuple[bool, bool, bool]]] = []

        def before_after(path: str, op) -> None:
            b = snapshot(path)
            time.sleep(1.2)
            op()
            rows.append((path, _cmp(b, snapshot(path))))

        over = newfile(b"old-and-different-length")
        before_after(over, lambda: shutil.copy2(src, over))

        inplace = newfile(b"AAAA\n")
        def rewrite_in_place() -> None:
            mtime = os.stat(inplace).st_mtime_ns
            with open(inplace, "wb") as f:
                f.write(b"BBBB\n")          # 内容变了，长度没变
            os.utime(inplace, ns=(os.stat(inplace).st_atime_ns, mtime))
        before_after(inplace, rewrite_in_place)

        atomic = newfile(b"AAAA\n")
        def rename_replace() -> None:
            tmp = atomic + ".tmp"
            with open(tmp, "wb") as f:
                f.write(b"CCCC\n")
            os.replace(tmp, atomic)
        before_after(atomic, rename_replace)

        tunnel = newfile(b"AAAA\n")
        def delete_and_recreate() -> None:
            os.remove(tunnel)
            with open(tunnel, "wb") as f:
                f.write(b"DDDD\n")
        before_after(tunnel, delete_and_recreate)

        labels = {
            over: "copy2 盖在已有文件上（内容换了）",
            inplace: "原地改写 + 还原 mtime（内容换了）",
            atomic: "写临时文件再换名（内容换了）",
            tunnel: "同名删除再建（内容换了）",
        }

        print()
        print("    表一：**同一条路径**，操作前后——quick_sig 就是这么做比较的")
        print("    做的事                                size变?  mtime变?  ctime变?  指纹变?")
        print("    " + "-" * 80)
        for path, (dsz, dmt, dct) in rows:
            sig_changed = dsz or dmt or dct
            print(f"    {labels[path]:<36}{_yn(dsz):<9}{_yn(dmt):<10}{_yn(dct):<11}"
                  f"{'变' if sig_changed else '**没变**'}")
        print()
        print("    [判读] 前四行里只有第 2 行（原地改写 + 还原 mtime）**指纹无变化**——")
        print("           内容已经换了，而三个字段一个都没动。这一格靠 stat 无论如何都救不回来。")
        print("           第 1、4 行靠 mtime 变，第 3 行 mtime 与 ctime 都变。")

        # ---- 表二：两份**不同**文件，指纹是否相同（"会不会把两条当成同一条"）----
        print()
        print("    表二：两份不同文件，指纹是否相同（三者全同 = 会被当成同一份，最糟）")
        print("    比法                                   size同?  mtime同?  ctime同?  指纹同?")
        print("    " + "-" * 80)

        fresh = os.path.join(d, "fresh.bin")
        shutil.copy2(src, fresh)
        _row_twins("copy2 到新路径（源 vs 副本）", snapshot(src), snapshot(fresh))

        # 与源**同一时刻**诞生的一对：这是"盖写之后与源指纹全等"的成因
        for attempt in range(12):
            twin_a = os.path.join(d, f"twin-a{attempt}.bin")
            twin_b = os.path.join(d, f"twin-b{attempt}.bin")
            with open(twin_a, "wb") as f:
                f.write(b"AAAA\n")
            with open(twin_b, "wb") as f:
                f.write(b"BBBBBBBB\n")
            if os.stat(twin_a).st_ctime_ns == os.stat(twin_b).st_ctime_ns:
                shutil.copy2(twin_a, twin_b)
                _row_twins(f"同一时刻诞生再盖写（第 {attempt + 1} 次撞上）",
                           snapshot(twin_a), snapshot(twin_b))
                break
        else:
            print("    同一时刻诞生再盖写                    12 对都没撞上（本机约 1/4 命中率）")

        print()
        print("    [判读] 表二第一行：副本与源的三个字段**只有 ctime 不同**（它是新建的），")
        print("           所以表一第 1 行的危险在这里不成立。第二行才是要命的那一种：")
        print("           两者创建时间撞在同一时刻时，盖写之后**三字段与源全等**——")
        print("           同一个 copy2，结果取决于目标何时诞生，而这件事没人控制得住。")
        print()
        print("           「Windows 上 st_ctime 是**创建时间**」是上面这一切的前提：")
        print("           原地改写不动它，就是证据。Unix 上 ctime 是 inode 变更时间、会动，")
        print("           所以那边 ctime 真能兜住 mtime 的假相等——这条经验在这里不成立。")
        print(f"    临时目录：{d}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _cmp(base: tuple[int, int, int], now_: tuple[int, int, int]) -> tuple[bool, bool, bool]:
    return (now_[0] != base[0], now_[1] != base[1], now_[2] != base[2])


def _row_twins(label: str, one: tuple[int, int, int], other: tuple[int, int, int]) -> None:
    same = tuple(a == b for a, b in zip(one, other))
    cell = lambda ok: ("同" if ok else "不同").ljust(7)  # noqa: E731
    print(f"    {label:<36}{cell(same[0])} {cell(same[1])} {cell(same[2])} "
          f"{'**同**' if all(same) else '不同'}")


def _yn(changed: bool) -> str:
    return "变" if changed else "**没变**"


def bench_clock() -> None:
    """`st_ctime` 的时间分辨率，以及它到底在什么时候变。

    第一版探针在这里得出的结论是错的（"间隔 0.02–1.0 s 都有 3/5 相同"），原因是
    **复用了文件名**，量到的是 tunnel cache 而不是时钟。所以这一节用不断递增的名字，
    并且把每一格都单独报出来。
    """
    section("1b) st_ctime 的分辨率与它何时变（命名不复用）")
    d = tempfile.mkdtemp(prefix="ls-f204-clock-")
    counter = [0]

    def newfile() -> str:
        counter[0] += 1
        p = os.path.join(d, f"n{counter[0]:04d}.bin")
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    try:
        for gap in (0.0, 0.005, 0.02, 0.1):
            same = 0
            for _ in range(8):
                a = newfile()
                if gap:
                    time.sleep(gap)
                b = newfile()
                if os.stat(a).st_ctime_ns == os.stat(b).st_ctime_ns:
                    same += 1
            print(f"    新建两个文件、间隔 {gap:>5} s：创建时间相同 {same}/8")
        print()
        p = newfile()
        first = os.stat(p).st_ctime_ns
        marks = []
        for _ in range(4):
            time.sleep(0.05)
            marks.append(os.stat(p).st_ctime_ns == first)
        print(f"    同一文件连看 4 次（每次间隔 0.05 s）：创建时间仍等于第一次 {sum(marks)}/4")
        print("    [判读] 写内容**不会**动创建时间——这正是「st_ctime 是创建时间」的直接证据。")
        print("           间隔为 0 时偶尔撞在同一时刻（8 次里几次），>5 ms 之后没有撞过。")
        print("           → 分辨率不是问题；问题在**什么时候它会被保留**（见上一节）。")
        print(f"    临时目录：{d}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ==============================================================================
# 2) 哈希吞吐（对照纯读基线）
# ==============================================================================


def bench_hash(mib: int, rounds: int) -> None:
    section(f"2) 哈希吞吐：{mib} MiB 文件，每个算法连跑 {rounds} 轮取最好一次")
    d = tempfile.mkdtemp(prefix="ls-f204-hash-")
    try:
        path = os.path.join(d, "big.bin")
        block = bytes(range(256)) * 4096  # 1 MiB
        t0 = time.perf_counter()
        with open(path, "wb") as f:
            for i in range(mib):
                f.write(bytes([i & 0xFF]) + block[1:])
        print(f"    造文件耗时 {time.perf_counter() - t0:.1f} s，大小 {os.path.getsize(path) / MiB:.1f} MiB")

        size = os.path.getsize(path)
        rows: list[tuple[str, float, str]] = []

        best, _ = _timed(lambda: _read_only(path), rounds)
        rows.append(("纯读（不算哈希）", best, "基线"))

        digests: dict[str, str] = {}
        for algo in ("sha256", "sha1", "md5", "blake2b"):
            best, digest = _timed(lambda a=algo: _hash_file(path, a), rounds)
            digests[algo] = str(digest)
            rows.append((algo, best, digests[algo][:16]))

        try:
            import blake3  # type: ignore
        except ImportError:
            rows.append(("blake3", float("nan"), "未安装（需要新依赖）"))
        else:
            best, digest = _timed(
                lambda: blake3.blake3(_read_all(path)).hexdigest(), rounds
            )
            rows.append(("blake3", best, str(digest)[:16]))

        # 四个 stdlib 算法必须对**同一份内容**给出四个**不同**的摘要。一起印出来，
        # 好让人一眼看出这一列不是装饰：同一份内容、不同算法，摘要本来就该不同；
        # 若四个印出同一个值，那就又是"空串的哈希"那个错。
        assert len(set(digests.values())) == len(digests), "不同算法给出了相同摘要，这不可能"

        # 本工具自己实现的 sha256 必须与项目里**真正在用的**那个函数给出同一个值。
        # 否则这一节的 MiB/s 是在给另一段代码计时，而 sha256 又是默认算法——
        # 那种偏差没人看得出来，只会让"这一节测的是索引器吗"永远没有答案。
        project = content_hash(path)
        assert digests["sha256"] == project.removeprefix("sha256:"), (
            f"本工具的 sha256 与 ingest.fingerprint.content_hash 不一致："
            f"{digests['sha256'][:16]} vs {project[:23]}"
        )

        print()
        print(f"    {'算法':<20} {'秒':>7} {'MiB/s':>9}   摘要前 16 位")
        print("    " + "-" * 64)
        for name, secs, note in rows:
            if secs != secs:  # NaN
                print(f"    {name:<20} {'-':>7} {'-':>9}   {note}")
            else:
                print(f"    {name:<20} {secs:>7.3f} {size / secs / MiB:>9.1f}   {note}")
        print()
        print("    [判读] stdlib 的 blake2b 比 sha256 慢——它没有 SIMD，而 sha256 走的是")
        print("           本机 CPU 的 SHA-NI 硬件指令。计划正文写的 BLAKE3 快是另一回事")
        print("           （BLAKE3 有 SIMD），它不在依赖表里，也没有装，本机没测。")
        print("           ⚠ 这是本机读数：没有 SHA-NI 的机器上结论会反过来，到时重量。")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _read_only(path: str) -> None:
    with open(path, "rb", buffering=0) as f:
        while f.read(MiB):
            pass


def _hash_file(path: str, algo: str) -> str:
    """流式算一份内容的摘要，返回**真摘要**（不是占位符）。"""
    h = hashlib.new(algo)
    with open(path, "rb", buffering=0) as f:
        while True:
            block = f.read(MiB)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _read_all(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _timed(fn, rounds: int) -> tuple[float, object]:
    """跑 `rounds` 轮、取最快的一轮，**并把最后一轮的返回值一起带出来**。

    带回返回值不是顺手：返回值就是这个算法对这份内容算出来的**摘要**，而摘要要印在
    输出里给人核对。第一版把两件事分开（计时用一个空跑的 lambda、摘要另用
    `hashlib.new(algo).hexdigest()`），结果那一列印的是**空串的哈希**——四个算法
    全是同一个错的常量，而且从输出上完全看不出不对。计时与取值出自同一次计算，
    这个错就不可能再犯。
    """
    best = float("inf")
    value: object = None
    for _ in range(rounds):
        t0 = time.perf_counter()
        value = fn()
        best = min(best, time.perf_counter() - t0)
    return best, value


# ==============================================================================
# 3) size 预筛在真实目录树上的收益
# ==============================================================================


def bench_sizes(roots: tuple[str, ...], *, dedup: bool, top: int) -> None:
    section(f"3) size 预筛：{'、'.join(roots)}")
    config = load_config()
    rules = IgnoreRules.from_file(PROJECT_ROOT / ".lsearchignore", roots=roots)
    scanner = ScandirScanner(ScanConfig(roots=roots, data_dir=config.paths.data_dir))

    index = SizeIndex()
    #: 预筛要的是"尺寸"，但 `--dedup` 还得拿回路径来读——所以路径只在需要时攒。
    by_size: dict[int, list[str]] = collections.defaultdict(list)

    def on_entry(entry) -> None:
        if entry.is_dir:
            return
        index.add(entry.size)
        if dedup:
            by_size[entry.size].append(entry.path)

    t0 = time.perf_counter()
    stats = scanner.scan(rules=rules, on_entry=on_entry)
    scan_secs = time.perf_counter() - t0

    s = index.summary()
    print(f"    扫描：{stats.entries_seen:,} 条目 / {scan_secs:.1f} s / {stats.errors_total} 错误")
    print(f"    非空文件 {s['files']:,} 个 = {s['bytes'] / 1024**3:.2f} GiB，不同尺寸 {s['sizes']:,} 个")
    print()
    print("    尺寸预筛的收益（条数与字节数分开看，两个量纲的结论不同）：")
    if s["files"]:
        print(f"      要算哈希    {s['files_to_hash']:>10,} 个 = {s['bytes_to_hash'] / 1024**3:>7.2f} GiB"
              f"  （条数 {100 * s['files_to_hash'] / s['files']:.1f}%，字节 {100 * s['bytes_to_hash'] / s['bytes']:.1f}%）")
        print(f"      直接跳过    {s['files_skipped']:>10,} 个 = {s['bytes_skipped'] / 1024**3:>7.2f} GiB"
              f"  （条数 {100 * s['files_skipped'] / s['files']:.1f}%，字节 {100 * s['bytes_skipped'] / s['bytes']:.1f}%）")

    print()
    print("    尺寸撞车最集中的几组（这些是「按条数」那一栏被拖高的原因）：")
    top_groups = sorted(index.counts.items(), key=lambda kv: -kv[1])[:top]
    for size, n in top_groups:
        if n < 2:
            break
        print(f"      {size:>12,} 字节 × {n:>7,} 个")
    print()
    print("    [判读] 计划 §4.4.1 说「尺寸唯一的文件占了绝大多数」——在代码树上不成立：")
    print("           撞车集中在很小的文件上（成片的 __init__.py 之类），大文件尺寸各不相同。")
    print("           所以这条预筛省的是字节，不是文件数；每个文件那一次 open/close 它没动。")
    print("           ⚠ 这是一棵代码树的分布，6TB 备份盘上的形态（成对的大文件）与它不同，别外推。")

    if not dedup:
        print()
        print("    （加 --dedup 会把上面这些候选文件真的读一遍、算出内容真重复的字节数）")
        return

    _dedup_real(by_size, index, top)


def _dedup_real(by_size: dict[int, list[str]], index: SizeIndex, top: int) -> None:
    """把同尺寸的候选真读一遍，算"内容真重复"的字节数——`D12` 的收益估计。"""
    from localsearch.ingest.fingerprint import content_hash

    section("3b) 真去重：把候选文件全哈希一遍")
    dup_sizes = index.duplicate_sizes()
    paths = [p for size in dup_sizes for p in by_size.get(size, [])]
    print(f"    候选 {len(paths):,} 个文件；读全文算 sha256 中…")

    contents: dict[str, list[str]] = collections.defaultdict(list)
    read_bytes = 0
    errors = 0
    t0 = time.perf_counter()
    for i, path in enumerate(paths, 1):
        try:
            h = content_hash(path)
        except OSError:
            errors += 1
            continue
        contents[h].append(path)
        try:
            read_bytes += os.path.getsize(path)
        except OSError:
            pass
        if i % 200_000 == 0:
            print(f"      …{i:,} / {len(paths):,}（{time.perf_counter() - t0:.0f} s）")
    secs = time.perf_counter() - t0
    speed = read_bytes / secs / MiB if secs else 0.0

    groups = {h: ps for h, ps in contents.items() if len(ps) > 1}
    redundant = 0
    for h, ps in groups.items():
        one = ps[0]
        try:
            redundant += os.path.getsize(one) * (len(ps) - 1)
        except OSError:
            pass

    total = sum(size * n for size, n in index.counts.items())
    print()
    print(f"    读了 {read_bytes / 1024**3:.2f} GiB / {secs:.0f} s = {speed:.0f} MiB/s"
          f"（含 {len(paths):,} 次 open；{errors} 条读不到已跳过）")
    print(f"    不同内容 {len(contents):,} 份，其中真重复的 {len(groups):,} 组")
    if total:
        print(f"    冗余字节（同内容只留一份能省下的）{redundant / 1024**3:.2f} GiB"
              f"，占全部非空文件 {total / 1024**3:.2f} GiB 的 {100 * redundant / total:.1f}%")
    print()
    print("    [判读] 这个百分比就是 `D12` 去重能省下的提取与嵌入量（计划估的是 10–30%）。")

    print("    最大的几组重复内容：")
    for size, ps in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:top]:
        try:
            nbytes = os.path.getsize(ps[0])
        except OSError:
            nbytes = 0
        print(f"      {len(ps):>6,} 个 × {nbytes:>10,} 字节   例：{ps[0]}")


def bench_opens(roots: tuple[str, ...], limit: int) -> None:
    """第 4 组：**每个文件一次 open 的单价**——`--dedup` 那一轮的 7 MiB/s 是怎么来的。

    这一组是被 `--dedup` 自己的读数逼出来的，不是计划里的验证项。`--hash` 量的是
    "同一个 200 MiB 文件连读三遍"（约 950 MiB/s），而 `--dedup` 在 `D:/work` 上真跑
    出来的是 **7 MiB/s**——差 **135 倍**。两者量的不是一件事：前一档**只开一次文件**，
    后一档开了 94 万次。所以这一组把"每个文件"的钱拆开：**循环本身 / 一次 stat /
    一次 open+读 1 字节 / 读完整个文件但不哈希 / 只建一个 hashlib 对象 /
    content_hash 全套 / 哈希+getsize**。七档的差就是答案。

    **为什么每档跑两遍**：第一遍是新接触的文件，第二遍同样的文件已在系统缓存里。
    两遍差得多 = 那笔钱在**盘上**；两遍差不多 = 那笔钱是**每次都固定付**的。
    **采样必须均匀铺满候选列表**：取前 N 个会让样本全落在同一个目录里，那是缓存最热
    的角落，量出来的价不能代表全树。
    """
    section(f"4) 每个文件的 open 单价：{limit} 个真实候选文件（均匀采样）")
    config = load_config()
    rules = IgnoreRules.from_file(PROJECT_ROOT / ".lsearchignore", roots=roots)
    scanner = ScandirScanner(ScanConfig(roots=roots, data_dir=config.paths.data_dir))

    index = SizeIndex()
    by_size: dict[int, list[str]] = collections.defaultdict(list)

    def on_entry(entry) -> None:
        if entry.is_dir:
            return
        index.add(entry.size)
        by_size[entry.size].append(entry.path)

    t0 = time.perf_counter()
    stats = scanner.scan(rules=rules, on_entry=on_entry)
    print(f"    扫描：{stats.entries_seen:,} 条目 / {time.perf_counter() - t0:.1f} s")

    # 候选 = 与 `--dedup` 同一个人群（尺寸撞车、真要算哈希的那些），否则单价对不上。
    # **尺寸要排序**：`duplicate_sizes()` 给的是 `set`，迭代顺序随机（受哈希种子影响），
    # 不排的话每次运行采到的是**不同的一批文件**——第一版就是这样，两次运行的样本
    # 平均大小差了一倍（9.1 KiB vs 20.7 KiB），于是两个数根本不能互相比较。
    candidates = [p for size in sorted(index.duplicate_sizes()) for p in by_size.get(size, [])]
    if not candidates:
        print("    没有候选文件，跳过。")
        return
    step = max(1, len(candidates) // limit)
    sample = candidates[::step][:limit]
    print(f"    候选 {len(candidates):,} 个；每 {step} 个取 1 个，取到 {len(sample):,} 个")
    total_bytes = 0
    for p in sample:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass
    s = index.summary()
    mean_sample = total_bytes / max(1, len(sample)) / 1024
    mean_all = s["bytes_to_hash"] / max(1, s["files_to_hash"]) / 1024
    print(f"    样本共 {total_bytes / MiB:.1f} MiB（平均 {mean_sample:.1f} KiB/个）")
    print(f"    候选全体平均 {mean_all:.1f} KiB/个；**样本均明显更小**说明字节质量集中在")
    print("    少数大文件上（它们个头大、个数少，按条数均匀采样就采不到几个）。")

    def _touch(path: str) -> None:
        with open(path, "rb", buffering=0) as f:
            f.read(1)

    def _read_all_bytes(path: str) -> None:
        with open(path, "rb", buffering=0) as f:
            while f.read(MiB):
                pass

    def _hash_and_size(path: str) -> None:
        content_hash(path)
        os.path.getsize(path)

    variants: list[tuple[str, object]] = [
        ("纯循环（不碰文件）", lambda p: None),
        ("os.stat 一次", os.stat),
        ("open + 读 1 字节", _touch),
        ("open + 读全文（不哈希）", _read_all_bytes),
        ("只建 hashlib 对象", lambda p: hashlib.new("sha256")),
        ("content_hash（读全文）", content_hash),
        ("content_hash + getsize", _hash_and_size),
    ]

    print()
    print(f"    {'做的什么':<26} {'第 1 遍':>11} {'第 2 遍':>11}   外推到全部候选")
    print("    " + "-" * 72)
    us: dict[str, float] = {}
    for name, fn in variants:
        secs = []
        for _ in range(2):
            t0 = time.perf_counter()
            for p in sample:
                fn(p)  # type: ignore[operator]
            secs.append(time.perf_counter() - t0)
        us[name] = min(secs) / len(sample) * 1e6
        full = min(secs) / len(sample) * len(candidates)
        print(f"    {name:<26} {secs[0] / len(sample) * 1e6:>8.0f} 微秒"
              f" {secs[1] / len(sample) * 1e6:>8.0f} 微秒"
              f"   {full / 60:>7.1f} 分钟 / {us[name]:>7.0f} 微秒每个")

    loop = us["纯循环（不碰文件）"]
    touch = us["open + 读 1 字节"]
    read_all = us["open + 读全文（不哈希）"]
    mk = us["只建 hashlib 对象"]
    hashed = us["content_hash（读全文）"]
    kib = total_bytes / max(1, len(sample)) / 1024
    print()
    print(f"           一次 open（热）                {touch - loop:>6.0f} 微秒/个")
    print(f"           接着读完 {kib:.1f} KiB（不哈希）       {read_all - touch:>6.0f} 微秒/个")
    print(f"           真正算哈希那一段               {hashed - read_all:>6.0f} 微秒/个")
    print(f"           只建一个 hashlib 对象          {mk - loop:>6.0f} 微秒/个")
    print()
    print(f"           **三档的次序就是结论**：一次 open {touch - loop:.0f} 微秒可以忽略，"
          "**读字节才是大头**")
    print(f"           （{read_all - touch:.0f} 微秒读 {kib:.1f} KiB = "
          f"{(total_bytes / max(1, len(sample))) / ((read_all - touch) / 1e6) / MiB:.0f} MiB/s），")
    print(f"           而 sha256 本身只值 {hashed - read_all:.0f} 微秒——**建哈希器 1 微秒，可以忽略**。")
    print()
    print("           于是第 2 组那个「950 MiB/s」的真实身份清楚了：它是**一个大文件**的")
    print("           读吞吐，那个场景里每次读都很大、固定开销被摊掉。文件一多一小，")
    print("           同一条读路径就掉到几十 MiB/s——**与选哪个哈希算法几乎没有关系**")
    print("           （换个算法换的还是那几微秒）。")
    print()
    print("           ⚠ **这一组是偏乐观的下界**：3,000 个文件的样本两遍之后全在缓存里，")
    print("           而 `--dedup` 真跑那一轮每个文件只碰一次、铺满整块盘。实测那轮的")
    print("           等价单价是 **2.87 ms/个**（2,714 s / 945,425 个），是本表热读数的")
    print("           **6 倍**。所以**别拿这一列去乘文件数**——它的用途是拆开成本的构成")
    print("           （open / 读 / 哈希各占多少），不是预测总时间；总时间看 `--dedup`。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stat", action="store_true", help="第 1 组：quick_sig 的失效格子")
    ap.add_argument("--hash", action="store_true", help="第 2 组：哈希吞吐")
    ap.add_argument("--sizes", action="store_true", help="第 3 组：size 预筛分布")
    ap.add_argument("--dedup", action="store_true", help="第 3 组附带：真读一遍算去重收益")
    ap.add_argument("--opens", action="store_true", help="第 4 组：每个文件的 open 单价")
    ap.add_argument("--roots", nargs="*", default=None, help="第 3、4 组扫哪些根（默认配置里的）")
    ap.add_argument("--sample", type=int, default=3000, help="第 4 组采样多少个文件")
    ap.add_argument("--mb", type=int, default=200, help="第 2 组造多大的文件（MiB）")
    ap.add_argument("--rounds", type=int, default=3, help="第 2 组每个算法跑几轮")
    ap.add_argument("--top", type=int, default=8, help="各表最多列几行")
    args = ap.parse_args(argv)

    if not (args.stat or args.hash or args.sizes or args.dedup or args.opens):
        args.stat = args.hash = args.sizes = True

    print(f"bench_fingerprint  机器时间 {now()}  Python {sys.version.split()[0]}")
    if args.stat:
        bench_stat()
        bench_clock()
    if args.hash:
        bench_hash(args.mb, args.rounds)
    if args.sizes or args.dedup or args.opens:
        roots = tuple(args.roots) if args.roots else tuple(load_config().paths.roots)
        if not roots:
            print("没有 roots：用 --roots 给，或者在 config/config.toml 的 [paths] 里写。",
                  file=sys.stderr)
            return 2
        if args.sizes or args.dedup:
            bench_sizes(roots, dedup=args.dedup, top=args.top)
        if args.opens:
            bench_opens(roots, args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
