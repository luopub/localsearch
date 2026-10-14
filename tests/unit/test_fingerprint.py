"""`F2-04` 的指纹与 size 预筛：`ingest/fingerprint.py`。

关联验收项 **A9**（同一份内容出现在多个路径时只提取与嵌入一次）——本文件管到"内容
是不是同一份"这一层，`A9` 的另一半（真的只嵌入一次）在 `test_dedup.py`。

本文件里有几条**故意钉住一个坑**的测试（`quick_sig` 会在"原地改写 + 还原 mtime"上
判错、`st_ctime` 在 Windows 上是创建时间），它们不是"测试现状"，是**把一条实测事实
写死**：将来有人改这三行配方时，会先撞上它们，从而必须回头读 `fingerprint.py` 模块
文档第 1 节。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time

import pytest

from localsearch.ingest.fingerprint import (
    CONTENT_HASH_ALGO,
    QUICK_SIG_RECIPE,
    SizeIndex,
    content_hash,
    hash_algo_of,
    is_same_content,
    quick_sig,
    quick_sig_of,
    sig_is_current,
    sig_recipe,
)


@pytest.fixture
def tmp_tree(tmp_path):
    """一棵小树 + 一个写文件的助手。写文件后**推进 mtime**，好让"变没变"看得见。"""
    def write(name: str, text: str) -> str:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    return write


# ==============================================================================
# 1. quick_sig：形状与配方号
# ==============================================================================


def test_the_signature_carries_its_recipe_number():
    """返回值里必须带配方号。没有它，配方一改，旧值会被拿去与新值逐字比。"""
    st = os.stat(__file__)
    sig = quick_sig(st)
    assert sig.startswith(f"stat:{QUICK_SIG_RECIPE}:")
    assert sig_recipe(sig) == QUICK_SIG_RECIPE
    assert sig_is_current(sig)


def test_the_signature_contains_the_three_stat_fields():
    st = os.stat(__file__)
    assert quick_sig(st) == (
        f"stat:{QUICK_SIG_RECIPE}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    )


def test_a_foreign_signature_is_not_treated_as_current():
    """不是本函数的产物 → `sig_recipe` 给 `None`、`sig_is_current` 给假。

    用法是**当成"变了"**（重算），所以这两个都必须能对陌生输入说"不认识"，
    而不是抛异常或者碰巧比出一段相等。
    """
    for junk in ("", "sha256:abc", "stat:1:1:2", "stat:x:1:2:3", "stat:1:1:2:3:4",
                 "STAT:1:1:2:3", "1:1:2:3"):
        assert sig_recipe(junk) is None, junk
        assert not sig_is_current(junk), junk


def test_a_signature_from_a_different_recipe_is_not_current():
    """别的配方号 → 不认。这条是那一整套前缀设计唯一的消费方。"""
    assert sig_recipe("stat:99:1:2:3") == 99
    assert not sig_is_current("stat:99:1:2:3")


def test_the_same_file_gives_the_same_signature(tmp_tree):
    a = tmp_tree("a.txt", "hello\n")
    assert quick_sig_of(a) == quick_sig_of(a)


def test_size_mtime_and_ctime_each_move_the_signature(tmp_tree):
    """三个字段任意一个变了，指纹就得变——这是它"能当过滤器"的下限。"""
    a = tmp_tree("a.txt", "AAAA\n")
    st0 = os.stat(a)
    sig0 = quick_sig(st0)

    time.sleep(0.01)
    with open(a, "w") as f:          # 长度不同 → size 变
        f.write("AAAAAAAAAA\n")
    sig_size = quick_sig(os.stat(a))
    assert sig_size != sig0

    time.sleep(0.01)
    os.utime(a, None)                # 只动 mtime
    sig_mtime = quick_sig(os.stat(a))
    assert sig_mtime != sig_size


def test_a_copy_to_a_new_path_gets_a_new_creation_time(tmp_tree):
    """`copy2` 到**新路径**：mtime 被保留，但那是个新文件 → 创建时间不同。

    ⚠ **这一条与"盖在已有文件上"是两个结果**（下一条），区别只在目标路径存不存在：
    `copy2` 打开一个已存在的路径是**截断**，不是新建，创建时间因此保留。
    所以"复制保不保留创建时间"这个问法本身是错的——**得看目标**。
    """
    a = tmp_tree("a.txt", "same content\n")
    dst = os.path.join(os.path.dirname(a), "brand-new.txt")
    time.sleep(0.05)
    shutil.copy2(a, dst)
    st_a, st_b = os.stat(a), os.stat(dst)
    assert st_a.st_size == st_b.st_size
    assert st_a.st_mtime_ns == st_b.st_mtime_ns      # copy2 就是要保这个
    assert st_a.st_ctime_ns != st_b.st_ctime_ns, "新路径 = 新文件 = 新的创建时间"
    assert quick_sig(st_a) != quick_sig(st_b)


def test_overwriting_an_existing_file_keeps_its_creation_time(tmp_tree):
    """⚠ 钉住"ctime 兜不住"的第一个反例：`copy2` **盖在已有文件上**。

    目标文件是被**截断**的，不是新建的，所以创建时间原样保留。这不是 bug，是 NTFS
    的创建时间语义——但它的后果是：**不能指望 ctime 在内容被换掉时一定动**。

    这条测试同时钉住"Windows 上 st_ctime 是创建时间"这个前提：**在 Unix 上它会失败**
    （那边 ctime 是 inode 变更时间，截断改写必然动它）。

    ⚠ **`dst` 与 `src` 之间要睡够**，否则这条测试会以另一种方式失败（下一段）。
    """
    dst = tmp_tree("dst.txt", "a much longer placeholder\n")
    c_before = os.stat(dst).st_ctime_ns
    time.sleep(0.05)
    src = tmp_tree("src.txt", "AAAA\n")
    time.sleep(0.05)
    shutil.copy2(src, dst)

    st = os.stat(dst)
    assert st.st_size == os.stat(src).st_size, "前提：内容确实被换掉了"
    assert st.st_ctime_ns == c_before, (
        "创建时间被保留了。这条一旦失败，说明换到了 Unix 语义的环境"
        "（那边 ctime 是 inode 变更时间，这里会变）"
    )
    # mtime 被换成了源的，所以指纹**仍然**变了——这一格不是漏网的那一格。
    assert st.st_mtime_ns == os.stat(src).st_mtime_ns
    assert quick_sig(st) != quick_sig(os.stat(src))


def test_copying_between_files_born_in_the_same_tick_is_invisible(tmp_tree, tmp_path):
    """⚠ **`copy2` 能把目标变成与源"三字段全等"的文件**——只要两者诞生于同一时刻。

    这条测试第一版写错了：当时断言"盖写之后目标的指纹与源不同"，因为它想当然地以为
    「目标保留自己的创建时间」必然意味着「与源的创建时间不同」。**不一定**：

    - 目标保留**自己**的创建时间（上一条）；
    - 源与目标如果是**同一时刻**诞生的（同一批解压、同一次批量复制、同一瞬间连建两个
      文件），两者的创建时间**就是同一个值**；
    - 于是盖写之后，目标的 size/mtime 来自源、ctime 与源相同 → **指纹完全一致**。

    本机实测这个"同一时刻"并不罕见：连续新建两个文件（间隔 0）时，8 次里有 2 次创建
    时间相同（`tools/bench_fingerprint.py` 的 1b 节）。

    本测试用**显式构造**把这个组合摆出来，而不是去赌概率：连续建一对文件、看它们有没有
    落在同一时刻，撞上就往下断言，没撞上就**再建一对**（最多 12 对）。本机约 1/4 的
    命中率，12 对都撞不上的概率是 0.75¹² ≈ 3%——那时才 `skip`，并在消息里说明原因。
    **不许**把它写成"重跑就能过"的 flaky 测试：跳过 80% 的断言等于没有断言。
    """
    collision: tuple[str, str] | None = None
    for i in range(12):
        a = str(tmp_path / f"tick-a{i}.txt")
        b = str(tmp_path / f"tick-b{i}.txt")
        with open(a, "w") as f:
            f.write("AAAA\n")
        with open(b, "w") as f:
            f.write("BBBBBBBB\n")          # 长度不同，好让"被盖写"看得出来
        if os.stat(a).st_ctime_ns == os.stat(b).st_ctime_ns:
            collision = (a, b)
            break
    if collision is None:
        pytest.skip("12 对连续新建的文件都没落在同一时刻——本机约 1/4 命中率，这话本身可疑")

    a, b = collision
    assert os.path.getsize(a) != os.path.getsize(b), "前提：两者长度不同"
    shutil.copy2(a, b)
    assert os.path.getsize(b) == os.path.getsize(a), "前提：b 的内容确实被换成了 a 的"
    assert quick_sig_of(a) == quick_sig_of(b), (
        "目标被盖写成了源的内容，而指纹与源一模一样——"
        "这一格就是 quick_sig 判不出来的那种，成因是创建时间撞在同一时刻"
    )


def test_a_rename_replace_is_caught_by_ctime(tmp_tree):
    """编辑器原子保存（写临时文件再换名）**会**动 ctime——这是 ctime 真正有用的两处之一。

    另一处是"新建"（上上条）。把这两条与上面那条反例放在一起，才说得清 ctime 到底
    能指望多少：**能兜住换名与新建，兜不住截断改写与同名重建。**
    """
    a = tmp_tree("a.txt", "AAAA\n")
    sig_before = quick_sig_of(a)
    time.sleep(0.05)
    tmp = os.path.join(os.path.dirname(a), "a.txt.tmp")
    with open(tmp, "w") as f:
        f.write("CCCC\n")
    os.replace(tmp, a)
    assert quick_sig_of(a) != sig_before


def test_deleting_and_recreating_the_same_name_can_keep_the_creation_time(tmp_tree):
    """⚠ 钉住"ctime 兜不住"的第二个反例，也是**最难发现的一个**：同名删除再建。

    Windows 会把被删掉那个文件的创建时间**继承**给同名的新文件（NTFS tunnel cache）。
    后果有两层，第二层比第一层严重：

    1. ctime 这一栏在这里不动；
    2. 于是**路径与指纹都没变**——增量扫描（`F6-01`）光靠这两样**发现不了**这次改动。

    本机实测：间隔 1.2 s 时仍然继承（`tools/bench_fingerprint.py` 的第 5 行）。
    那条实测**没有**验证继承窗口到底多长，只验证了"至少 1.2 s 之后还会继承"——
    别把"15 秒"当成已核实的事实（那是 Windows 的常见说法，此处没验）。
    """
    a = tmp_tree("a.txt", "AAAA\n")
    before = os.stat(a)
    sig_before = quick_sig(before)
    time.sleep(0.05)
    os.remove(a)
    with open(a, "w") as f:
        f.write("DDDD\n")            # 内容不同、长度相同
    after = os.stat(a)
    assert after.st_mtime_ns != before.st_mtime_ns, "前提：mtime 确实动了"
    assert after.st_ctime_ns == before.st_ctime_ns, (
        "创建时间被继承。这条失败 = 换到了没有 tunnel cache 的文件系统（或 Unix）"
    )
    # 指纹**仍然**变了——因为 mtime 动了。所以这一格单独看并不漏；
    # 它漏的是"连 mtime 也被一起还原"的那种组合（第 3 格 + 这一格叠加）。
    assert quick_sig(after) != sig_before


def test_an_in_place_rewrite_with_restored_mtime_is_INVISIBLE(tmp_tree):
    """⚠ 钉住 `quick_sig` **唯一会判错的那一格**（本机实测，见模块文档第 1 节）。

    原地改写、长度不变、再把 mtime 改回原值 → 三个字段**全部相同**，而内容已经变了。
    这条测试**不是在断言缺陷可以接受**，而是让"配方被改动"的人先撞上它一次。

    **它同时也是"Windows 上 `st_ctime` 是创建时间"的证据**：原地改写不碰创建时间，
    所以 ctime 这一栏救不回来。**在 Unix 上这条会失败**——那边的 `st_ctime` 是 inode
    变更时间，改写会动它。本项目只跑 Windows（计划的技术栈），所以这条测试成立；
    真要在 Linux 上跑，**它应当是 xfail 而不是改断言**——因为"ctime 能兜住"这件事
    在那边是真的，把它改掉等于丢掉一条事实。

    （同名的两条反例——盖写已有文件、同名删除再建——各自单独一条测试，见上面。）
    """
    a = tmp_tree("a.txt", "AAAA\n")
    st0 = os.stat(a)
    sig0 = quick_sig(st0)
    origin = st0.st_atime_ns, st0.st_mtime_ns

    time.sleep(0.02)                 # 确保 mtime 真的有机会变
    with open(a, "w") as f:
        f.write("BBBB\n")            # 内容变了，长度没变
    os.utime(a, ns=origin)           # 把 mtime 改回去

    st1 = os.stat(a)
    assert open(a).read() == "BBBB\n", "前提：内容确实变了"
    assert st1.st_size == st0.st_size
    assert st1.st_mtime_ns == st0.st_mtime_ns
    assert st1.st_ctime_ns == st0.st_ctime_ns, (
        "本机 st_ctime 是创建时间，原地改写不动它。"
        "这条一旦失败，说明换到了 Unix 语义的环境（那边 ctime 会动）"
    )
    assert quick_sig(st1) == sig0, "指纹判不出来——这正是这一格的失效"


def test_the_docstring_documents_that_failure(tmp_tree):
    """那条失效必须写在文档里，不能只写在这条测试里。

    判据很粗但有效：模块文档里同时出现 `quick_sig`、`ctime` 与"创建时间"。
    """
    import localsearch.ingest.fingerprint as mod

    doc = mod.__doc__ or ""
    assert "quick_sig" in doc
    assert "创建时间" in doc, "模块文档必须写明 Windows 上 st_ctime 是创建时间"
    assert "原地改写" in doc or "还原 mtime" in doc


# ==============================================================================
# 2. content_hash：正确性与前缀
# ==============================================================================


def test_the_same_bytes_give_the_same_hash(tmp_tree):
    a = tmp_tree("a.txt", "同一份内容\n")
    b = tmp_tree("b.txt", "同一份内容\n")
    assert content_hash(a) == content_hash(b)


def test_different_bytes_give_different_hashes(tmp_tree):
    a = tmp_tree("a.txt", "AAAA\n")
    b = tmp_tree("b.txt", "BBBB\n")
    assert content_hash(a) != content_hash(b)


def test_the_hash_is_streamed_not_read_at_once(tmp_tree):
    """分块读：内容跨过块边界也要与一次性读出来的哈希一致。

    块大小比自己传一个很小的值来测——**不然这条测试永远只走第一条分支**，
    而"跨块拼接"正是流式哈希唯一能写错的地方。
    """
    text = "0123456789" * 5000          # 50,000 字节
    a = tmp_tree("a.txt", text)
    expected = f"{CONTENT_HASH_ALGO}:{hashlib.new(CONTENT_HASH_ALGO, text.encode()).hexdigest()}"
    assert content_hash(a) == expected
    for chunk in (1, 7, 4096, 10_000):
        assert content_hash(a, chunk_size=chunk) == expected, chunk


def test_an_empty_file_hashes_like_the_empty_string(tmp_tree):
    """空文件不特判：它之间**本来就是同一份内容**，去重正该把它们折叠到一起。"""
    a = tmp_tree("empty1.txt", "")
    b = tmp_tree("empty2.txt", "")
    expected = f"{CONTENT_HASH_ALGO}:{hashlib.new(CONTENT_HASH_ALGO).hexdigest()}"
    assert content_hash(a) == expected
    assert content_hash(a) == content_hash(b)


def test_a_missing_file_raises_rather_than_returning_something(tmp_tree):
    """读不到就抛——**不吞**。吞掉异常会让调用方分不清"没内容"与"没读到"。"""
    with pytest.raises(OSError):
        content_hash(str(tmp_tree("a.txt", "x").replace("a.txt", "nope.txt")))


def test_the_hash_carries_its_algorithm_name():
    assert CONTENT_HASH_ALGO == "sha256", "换算法要连带重建库，改这一行前先读模块文档第 2 节"
    assert hash_algo_of("sha256:abc") == "sha256"
    assert hash_algo_of("abc") == ""
    assert hash_algo_of("") == ""


def test_hashes_from_different_algorithms_are_never_equal():
    """没有前缀、或者前缀对不上的值，一律判**不同**（于是会被重算）。

    撞运气能撞上的概率可以忽略，但"可以忽略"不是"不会"——这里要的是确定。
    """
    assert is_same_content("sha256:abc", "sha256:abc")
    assert not is_same_content("sha256:abc", "blake3:abc")
    assert not is_same_content("abc", "abc"), "两组都没前缀 = 都不知道是什么算法"
    assert not is_same_content("abc", "sha256:abc")
    assert not is_same_content("", "")


# ==============================================================================
# 3. size 预筛：它为什么不会漏
# ==============================================================================


def test_only_sizes_with_a_sibling_need_hashing():
    idx = SizeIndex()
    idx.add_all([100, 100, 200, 300, 300, 300])
    assert idx.duplicate_sizes() == {100, 300}
    assert idx.needs_hash(100) and idx.needs_hash(300)
    assert not idx.needs_hash(200)


def test_a_sole_file_of_its_size_would_be_skipped_soundly():
    """这条钉住 size 预筛的**可靠性论证**：内容相同 ⇒ 尺寸必然相同。

    取逆否：尺寸独一 ⇒ 不可能有同内容兄弟。所以跳过它**永远不会漏掉一对重复**——
    这是逻辑上的必然，与本机的分布无关（对比 `quick_sig` 那一条，那是经验）。
    """
    idx = SizeIndex()
    idx.add_all([7, 11, 13])
    assert idx.duplicate_sizes() == set()
    for size in (7, 11, 13):
        assert not idx.needs_hash(size)


def test_zero_byte_files_are_counted_so_they_can_dedup(tmp_tree):
    """尺寸为 0 也要记：两个空文件是同一份内容，预筛得把它们留下成一对。"""
    idx = SizeIndex()
    idx.add_all([0, 0, 5])
    assert 0 in idx.duplicate_sizes()
    assert idx.needs_hash(0) and not idx.needs_hash(5)


def test_the_answer_is_only_valid_after_the_whole_pass():
    """⚠ **扫到一半问 `needs_hash()` 会把先出现的那个重复文件判成"独一"而跳过。**

    这条测试故意把错误用法摆出来：先 `add(1)` 再问，得到 `False`；再 `add(1)` 之后
    才变 `True`。第一份文件正是重复对的第一个——**漏的是它**，而没有任何异常或日志。
    """
    idx = SizeIndex()
    idx.add(1)
    assert not idx.needs_hash(1), "扫到一半：此时'还没有兄弟'与'永远没有兄弟'分不开"
    idx.add(1)
    assert idx.needs_hash(1), "扫完之后才是可信的答案"


def test_needs_hash_is_false_for_a_size_never_seen():
    assert not SizeIndex().needs_hash(12345)


def test_the_summary_reports_files_and_bytes_separately():
    """**两个量纲都得给**：只报条数会让人以为这预筛没用，只报字节能让人以为它把
    文件数也降下来了。`D:/work` 实测就是条数 3.5% / 字节 75%，两个数差得极远。"""
    idx = SizeIndex()
    # 1000 个 1 字节的文件（撞车）+ 1 个 1000 字节的（独一份）
    idx.add_all([1] * 1000 + [1000])
    s = idx.summary()
    assert s["files"] == 1001
    assert s["sizes"] == 2
    assert s["bytes"] == 2000
    assert s["files_to_hash"] == 1000          # 条数几乎没省
    assert s["bytes_to_hash"] == 1000          # 字节省掉一半
    assert s["files_skipped"] == 1
    assert s["bytes_skipped"] == 1000
    assert s["files_to_hash"] + s["files_skipped"] == s["files"]
    assert s["bytes_to_hash"] + s["bytes_skipped"] == s["bytes"]


def test_the_summary_of_an_empty_index_is_all_zero():
    s = SizeIndex().summary()
    assert s["files"] == 0 and s["bytes"] == 0
    assert s["files_to_hash"] == 0 and s["bytes_skipped"] == 0


def test_the_index_keeps_only_a_histogram_not_the_paths():
    """`SizeIndex` 只存尺寸的计数——这是它能用在 347 万条目上的原因（模块文档第 4 节）。

    反过来说：它**不**能回答"哪些路径要哈希"。这条测试把这件事写死，免得将来有人
    以为可以顺手从这里拿路径。
    """
    idx = SizeIndex()
    idx.add_all([10, 10, 20])
    assert idx.counts == {10: 2, 20: 1}
    assert not any(isinstance(k, str) for k in idx.counts), "键只能是尺寸，不能是路径"


def test_the_real_distribution_contradicts_the_plan_wording():
    """计划 §4.4.1 说"尺寸唯一的文件占了绝大多数"——`D:/work` 上实测不成立。

    这条不测 `SizeIndex`，测的是**那句断言本身**有没有被如实纠正：模块文档里必须
    留下"96.5% 的文件有同尺寸兄弟"这类实测结论，以及"收益要按字节算"。
    """
    import localsearch.ingest.fingerprint as mod

    doc = mod.__doc__ or ""
    assert "96.5%" in doc, "计划那句反例必须留在文档里"
    assert "按字节" in doc, "必须写明收益要按字节看，否则读的人会以为这条预筛没用"


def test_the_docstring_keeps_the_small_file_cost_finding():
    """`F2-04` 收尾时被自己的读数推翻的那段话，必须留下**纠正后的**版本。

    第一版文档写的是"哈希不贵、读一遍就十几秒、真正的杠杆是 size 预筛"——那是拿一个
    200 MiB 大文件的吞吐去估 94 万个小文件。真跑（`--dedup`）是 **7 MiB/s、2.87 ms
    每个文件**，差 135 倍；`--opens` 把它拆开后大头是"读 9.2 KiB 的字节"（372 微秒），
    而**建一个哈希器只要 1 微秒**。

    这条测试钉的是这三件事都在文档里，因为**每一件都改变结论**：
    第一件说明"换哈希算法"不是杠杆，第二件说明"预筛省字节"仍然值，第三件说明
    "每次 open 的固定开销"预筛救不了。文档若被改回那句好听的旧话，这条会红。
    """
    import localsearch.ingest.fingerprint as mod

    doc = mod.__doc__ or ""
    assert "2.87 ms" in doc, "真跑每文件 2.87 ms 必须留下——它是推翻旧话的那一个数"
    assert "135 倍" in doc, "与「大文件吞吐」的差距必须写出来，否则两个数会被混用"
    assert "1 微秒" in doc, "「建哈希器可忽略」必须留下，它才是否定换算法的依据"
    assert "22–24 MiB/s" in doc or "22-24 MiB/s" in doc, "小文件的真实读速要留下"
    assert "没有\n归因" in doc or "没有归因" in doc, "差 6 倍的原因没查，这一点不能省略"

