"""P0 依赖探针：把"这些库在本机到底能不能用"变成可复现的原始输出。

对应功能点：F0-05（FAISS / hnswlib 在 Windows + py3.12 的可用性）、
            F0-06（FTS5 + jieba 可用性）。

用法（在仓库根执行；`PYTHONUTF8=1` 是为了绕开本机代码页 936 的乱码，
`-u` 是为了让 stdout 与 stderr 的先后顺序与真实发生的一致）：

    PYTHONUTF8=1 uv run python -u tools/probe_deps.py > docs/reports/F0-05-F0-06-依赖探针.md 2>&1

为什么用重定向、而不是让脚本自己写报告文件：这样落盘的是**未经加工的原始
输出**。脚本不参与排版，也就不可能把失败修饰成好看的样子。脚本只负责打印。

退出码：全部通过为 0，出现 FAIL 为 1。因此它同时能当冒烟闸门用。

这里刻意**不做**的两件事，免得与其它功能点重复：
  - 不测嵌入吞吐（那是 F0-01/F0-03，要 `tools/bench_embed.py` 和真模型）
  - 不测 MFT 枚举（那是 F0-04，要管理员权限，另写 `tools/probe_mft.py`）
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import warnings

from localsearch.core.console import use_safe_output

# ---------------------------------------------------------------------------
# 极简的结果收集。刻意不用第三方测试框架：这是"实测探针"，不是单测，
# 输出要能直接当报告读。
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记一条结果并立刻打印，避免异常中途打断时前面的结论丢失。"""
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def sub(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 74 - len(title)))


# ---------------------------------------------------------------------------
# 头部：把"这份输出是哪个版本跑出来的"钉死。缺了它，报告无法复现。
# ---------------------------------------------------------------------------


def print_header() -> None:
    section("P0 依赖探针 — 原始输出")
    print(f"生成时间    : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"解释器      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台        : {platform.platform()}")
    print(f"架构        : {platform.machine()}")
    print(f"在 venv 内  : {sys.prefix != sys.base_prefix}")

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        # 工作区脏必须标出来：在未提交的代码上跑出的结果，别人 clone 不到同一份。
        state = f"{head} + 工作区脏（{len(dirty.splitlines())} 条改动）" if dirty else f"{head} 干净"
        print(f"代码版本    : {state}")
    except Exception as exc:  # noqa: BLE001 —— 取不到版本不该让探针失败，但要明说
        print(f"代码版本    : 取不到（{type(exc).__name__}: {exc}）")


# ---------------------------------------------------------------------------
# F0-05：FAISS / hnswlib
# ---------------------------------------------------------------------------


def probe_faiss() -> None:
    sub("F0-05a  FAISS：能不能用，真建索引真检索")

    try:
        faiss = importlib.import_module("faiss")
    except Exception as exc:  # noqa: BLE001
        check("import faiss", False, f"{type(exc).__name__}: {exc}")
        return

    check("import faiss", True, f"版本 {getattr(faiss, '__version__', '未知')}")

    dim, n = 768, 2000  # 768 = D5 决定的原生维度
    try:
        import numpy as np

        rng = np.random.default_rng(20261014)  # 固定种子：报告要可复现
        base = rng.standard_normal((n, dim), dtype="float32")
        base /= np.linalg.norm(base, axis=1, keepdims=True)

        # 查询向量直接用第 7 条，正确答案已知，省得再算一遍最近邻。
        query = base[7:8].copy()

        t0 = time.perf_counter()
        flat = faiss.IndexFlatIP(dim)  # 内积；向量已归一化，等价余弦
        flat.add(base)
        t_flat_build = time.perf_counter() - t0

        t0 = time.perf_counter()
        _, idx = flat.search(query, 5)
        t_flat_query = (time.perf_counter() - t0) * 1000  # 毫秒
        check(
            "IndexFlatIP 建索引 + 检索并命中自身",
            idx[0][0] == 7,
            f"{n} 条 {dim} 维；建 {t_flat_build * 1000:.1f} ms，"
            f"单次检索 {t_flat_query:.3f} ms，top1={idx[0][0]}",
        )

        # HNSW 是 A3（延迟）真正要用的索引类型，必须单独验一次。
        t0 = time.perf_counter()
        hnsw = faiss.IndexHNSWFlat(dim, 32)  # 32 = 每节点连接数
        hnsw.hnsw.efConstruction = 200
        hnsw.add(base)
        t_hnsw_build = time.perf_counter() - t0

        hnsw.hnsw.efSearch = 64
        t0 = time.perf_counter()
        _, hidx = hnsw.search(query, 5)
        t_hnsw_query = (time.perf_counter() - t0) * 1000
        check(
            "IndexHNSWFlat 建索引 + 检索并命中自身",
            hidx[0][0] == 7,
            f"建 {t_hnsw_build:.3f} s，单次检索 {t_hnsw_query:.3f} ms，top1={hidx[0][0]}",
        )

        # 落盘/回读：P5 要把索引存到 D:/localsearch-data/，这条路得先通。
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.faiss")
            faiss.write_index(hnsw, path)
            size_mb = os.path.getsize(path) / 1024 / 1024
            back = faiss.read_index(path)
            _, ridx = back.search(query, 1)
            check(
                "write_index / read_index 往返",
                ridx[0][0] == 7,
                f"{n} 条 {dim} 维索引落盘 {size_mb:.2f} MiB，回读后仍命中自身",
            )
    except Exception as exc:  # noqa: BLE001
        check("FAISS 功能性验证", False, f"{type(exc).__name__}: {exc}")


def probe_hnswlib() -> None:
    sub("F0-05b  hnswlib（备选方案）：是否真的需要它")

    # hnswlib 不在 §7.4 的依赖表里——计划写的是"FAISS 优先，hnswlib 备选，
    # 只在 FAISS 不可用时才引入"。所以这里只回答"万一要用，拿得到吗"，
    # 装了会污染 venv、还会让 §7.4 多一个未决依赖，故只做 dry-run 解析。
    try:
        import hnswlib  # noqa: F401

        check("hnswlib 已安装", True, "（本不该装，若已装请查明来源）")
        return
    except ImportError:
        pass

    # 注意：uv 是独立可执行文件，**不是** venv 里的 Python 模块，
    # 所以必须走 PATH 上的 uv，不能写成 `sys.executable -m uv`（那样必然报
    # "No module named uv"，探针自己就成了坏的）。
    uv = shutil.which("uv")
    if uv is None:
        check("hnswlib 可解析（未安装，仅 dry-run）", False, "PATH 上找不到 uv，无法做 dry-run 解析")
        return

    try:
        proc = subprocess.run(
            [uv, "pip", "install", "--dry-run", "--python", sys.executable, "hnswlib"],
            capture_output=True, text=True, timeout=180,
        )
        out = (proc.stdout + proc.stderr).strip()
        tail = out.splitlines()[-6:]
        low = out.lower()
        # "build" 这个判断是启发式的，不是结论：dry-run 里若出现构建字样，
        # 说明该平台没有预编译轮，真要装就得有 MSVC（R2）。
        needs_build = "build" in low
        detail = f"退出码 {proc.returncode}；"
        detail += "输出中出现构建字样 → 可能需本地编译（见 R2）" if needs_build else "未出现构建字样"
        if tail:
            detail += "\n      原始输出末几行：\n      " + "\n      ".join(tail)
        check("hnswlib 可解析（未安装，仅 dry-run）", proc.returncode == 0, detail)
    except Exception as exc:  # noqa: BLE001
        check("hnswlib dry-run 解析", False, f"{type(exc).__name__}: {exc}（多半是网络，非结论）")


# ---------------------------------------------------------------------------
# F0-06：FTS5 + jieba
# ---------------------------------------------------------------------------


def probe_fts5() -> sqlite3.Connection | None:
    sub("F0-06a  SQLite FTS5：编译选项与中文分词行为")

    # 只报 sqlite_version（SQLite C 库的版本，这才是有意义的那个）。
    # 刻意不碰 sqlite3.version：它是 "Python 模块版本"，自 3.12 起已弃用、
    # 3.14 移除，用它会让我自己的探针往报告里吐一条 DeprecationWarning。
    print(f"sqlite3 库版本 : {sqlite3.sqlite_version}")

    con = sqlite3.connect(":memory:")
    try:
        opts = [r[0] for r in con.execute("PRAGMA compile_options").fetchall()]
        fts_opts = [o for o in opts if "FTS" in o.upper()]
        print(f"编译选项（含 FTS 的）: {fts_opts if fts_opts else '（无）'}")
    except Exception as exc:  # noqa: BLE001
        print(f"编译选项读取失败: {exc}")
        fts_opts = []

    try:
        con.execute("CREATE VIRTUAL TABLE t_raw USING fts5(body, tokenize='unicode61')")
        check("CREATE VIRTUAL TABLE ... USING fts5", True, "tokenize='unicode61'")
    except Exception as exc:  # noqa: BLE001
        check("CREATE VIRTUAL TABLE ... USING fts5", False, f"{type(exc).__name__}: {exc}")
        # 返回 None：FTS5 都建不出来时，后面的 jieba 检索测不出任何东西，
        # 硬跑只会抛一个与结论无关的异常，把真正的失败信息盖掉。
        return None

    # 关键行为：unicode61 **不切分汉字**——一串汉字是一个 token。
    # 这正是 M9 说"必须 jieba 预分词"的原因，这里把它测出来而不是靠推理。
    raw_doc = "北京大学的研究生前来报到"
    con.execute("INSERT INTO t_raw(body) VALUES (?)", (raw_doc,))

    n_whole = con.execute("SELECT count(*) FROM t_raw WHERE t_raw MATCH ?", (raw_doc,)).fetchone()[0]
    check(
        "未预分词时整句可命中（说明整句就是一个 token）",
        n_whole == 1,
        f"MATCH '{raw_doc}' → {n_whole} 条",
    )

    for word in ("大学", "研究生", "北京"):
        n = con.execute("SELECT count(*) FROM t_raw WHERE t_raw MATCH ?", (word,)).fetchone()[0]
        print(f"      未预分词 MATCH '{word}' → {n} 条{'（搜不到，符合预期）' if n == 0 else ''}")

    # snippet / highlight / bm25 是 F5-06 与排序要用的，顺手确认可用。
    try:
        row = con.execute(
            "SELECT snippet(t_raw, 0, '[', ']', '…', 8), bm25(t_raw) FROM t_raw WHERE t_raw MATCH ?",
            (raw_doc,),
        ).fetchone()
        check("snippet() 与 bm25() 可用", row is not None, f"snippet={row[0]!r}, bm25={row[1]:.4f}")
    except Exception as exc:  # noqa: BLE001
        check("snippet() 与 bm25() 可用", False, f"{type(exc).__name__}: {exc}")

    return con


def probe_jieba(con: sqlite3.Connection) -> None:
    sub("F0-06b  jieba：预分词后中文 2 字词检索是否真的有效（M9）")

    # jieba 0.42.1 的源码用了旧式正则字符串（如 "([一-鿕a-zA-Z...])"），
    # 在 py3.12 下抛 SyntaxWarning: invalid escape sequence。这不是错误，功能不受
    # 影响，但会污染输出。详见 PROGRESS §4 P0「与计划的偏差」第 4 条。
    #
    # 为什么不能简单地"导入时数一遍警告"：SyntaxWarning 只在**首次编译**时发出，
    # `__pycache__` 一旦命中就不再报。同一台机器上第一次跑看到 3 条、第二次看到
    # 0 条——那样的数字不能进报告。所以这里直接编译源码，绕开字节码缓存。
    jieba = importlib.import_module("jieba")
    # jieba 默认往 stderr 打"Building prefix dict ..."，那几行会插到报告的开头
    # （stdout 有缓冲，stderr 没有），把顺序搅乱。它是第三方库的闲聊，不是实测
    # 数据，关掉不影响结论。
    jieba.setLogLevel(logging.ERROR)
    check("import jieba", True, f"版本 {getattr(jieba, '__version__', '未知')}")

    src_dir = os.path.dirname(jieba.__file__ or "")
    py_files = sorted(
        os.path.join(root, name)
        for root, _dirs, files in os.walk(src_dir)
        for name in files
        if name.endswith(".py")
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for path in py_files:
            try:
                with open(path, encoding="utf-8") as fh:
                    compile(fh.read(), path, "exec")
            except (SyntaxError, UnicodeDecodeError):
                pass  # 只关心警告，编译失败不影响结论
        syntax_warns = [w for w in caught if issubclass(w.category, SyntaxWarning)]

    check(
        "统计 jieba 源码的 SyntaxWarning（编译源码，不受 __pycache__ 影响）",
        True,
        f"{len(py_files)} 个 .py 文件，共 {len(syntax_warns)} 条（已知，功能不受影响）",
    )
    for w in syntax_warns[:3]:
        print(f"      {w.category.__name__}: {w.message}")

    doc = "本工具是一个本地离线全盘全文搜索工具，支持中文检索与语义检索。"
    tokens = [t for t in jieba.cut(doc) if t.strip()]
    print(f"      原句: {doc}")
    print(f"      分词: {' / '.join(tokens)}")

    two_char = [t for t in tokens if len(t) == 2]
    print(f"      其中 2 字词 {len(two_char)} 个: {' '.join(two_char)}")
    if not two_char:
        check("jieba 分出 2 字词", False, "一个 2 字词都没分出来，无法验证 M9")
        return

    # 对照组必须用**同一句话**：预分词存一份、原样存一份，再拿同一个词去查两遍。
    # 若拿 A 句的词去查 B 句，得 0 是必然的，什么也证明不了。
    con.execute("CREATE VIRTUAL TABLE t_seg USING fts5(body, tokenize='unicode61')")
    con.execute("CREATE VIRTUAL TABLE t_raw2 USING fts5(body, tokenize='unicode61')")
    con.execute("INSERT INTO t_seg(body) VALUES (?)", (" ".join(tokens),))
    con.execute("INSERT INTO t_raw2(body) VALUES (?)", (doc,))

    probe_word = two_char[0]
    n_seg = con.execute("SELECT count(*) FROM t_seg WHERE t_seg MATCH ?", (probe_word,)).fetchone()[0]
    n_raw = con.execute("SELECT count(*) FROM t_raw2 WHERE t_raw2 MATCH ?", (probe_word,)).fetchone()[0]

    check(
        f"预分词后 2 字词可检索到（以 '{probe_word}' 为例）",
        n_seg == 1,
        f"预分词 MATCH '{probe_word}' → {n_seg} 条",
    )
    check(
        "对照组：同一句话原样入库则同一个词搜不到",
        n_raw == 0,
        f"未预分词 MATCH '{probe_word}' → {n_raw} 条",
    )

    hits = []
    for word in two_char:
        n = con.execute("SELECT count(*) FROM t_seg WHERE t_seg MATCH ?", (word,)).fetchone()[0]
        hits.append((word, n))
    n_hit = sum(1 for _, n in hits if n > 0)
    check(
        "全部 2 字词都能检索到",
        n_hit == len(hits),
        f"{n_hit}/{len(hits)} 命中；" + ", ".join(f"{w}:{n}" for w, n in hits),
    )


# ---------------------------------------------------------------------------
# 其余：顺手取下来，但不构成 F0-05 / F0-06 的结论
# ---------------------------------------------------------------------------


def probe_incidental() -> None:
    sub("附：与 D9（ST vs ONNX int8）相关的信息，非本次结论")

    try:
        ort = importlib.import_module("onnxruntime")
        print(f"onnxruntime {ort.__version__}")
        print(f"可用 providers: {ort.get_available_providers()}")
    except Exception as exc:  # noqa: BLE001
        print(f"onnxruntime 探测失败: {type(exc).__name__}: {exc}")

    try:
        torch = importlib.import_module("torch")
        print(f"torch {torch.__version__}；CUDA={torch.version.cuda}；线程={torch.get_num_threads()}")
        print("CPU 是否支持 bf16 的常用算子: 由 bench_embed.py 实测，此处不下结论")
    except Exception as exc:  # noqa: BLE001
        print(f"torch 探测失败: {type(exc).__name__}: {exc}")


def print_summary() -> int:
    section("结论摘要")
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_fail = len(RESULTS) - n_pass
    for name, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print()
    print(f"合计 {len(RESULTS)} 项：通过 {n_pass}，失败 {n_fail}")
    print()
    print("本探针只回答'这些库在本机能不能用'。它**不**回答：")
    print("  嵌入吞吐（F0-01 / F0-03，需真实模型与 tools/bench_embed.py）")
    print("  MFT 枚举可行性与权限（F0-04，需管理员权限）")
    print("  ST 与 ONNX int8 谁更快（F0-02 / D9，需双跑）")
    return 1 if n_fail else 0


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    print_header()
    probe_faiss()
    probe_hnswlib()
    con = probe_fts5()
    if con is not None:
        probe_jieba(con)
        con.close()
    probe_incidental()
    return print_summary()


if __name__ == "__main__":
    raise SystemExit(main())
