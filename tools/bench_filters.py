"""F2-03 微基准：`.lsearchignore` 的规则判定有多快，快路径值不值。

对应功能点：**F2-03**。产出指标 **E12**（`docs/PROGRESS.md` §3）。

---- 为什么需要它 --------------------------------------------------------------

`ingest/filters.py` 里做了一条**反向索引快路径**（精确名 / `*.ext` / 绝对前缀
分桶），并保留了一份**逐条全扫的参考实现** `_decide_reference` 当护栏。
护栏保证两者给同一条规则；**这个脚本量的是它换来了多少**。

结论要能回答一个具体问题：**排除规则会让全盘扫描慢多少？**
所以脚本的最后一行把数字折算到 C: 全盘的条目数上（`PROGRESS.md` §3 的 E11
量到 3,470,303 条目 / 122.946 s），而不是只报一个"判定/秒"。

---- 它不碰磁盘 ----------------------------------------------------------------

这是**纯内存**的规则匹配微基准：路径由种子确定性生成，**不遍历任何目录**。
真实的 IO 成本是另一回事（E11 管那一头）。这样分开是有意的——把两者混在
一起量，就分不清"慢在规则上"还是"慢在盘上"。

---- 语料为什么是合成的 --------------------------------------------------------

因为要比的是**两份实现对同一批输入的处理速度**，而不是"规则在真实盘上命中
多少"。真实命中分布由 `F2-06` 的 `lsearch index --dry-run` 给（计划 §5 的
P2 验收方式）。合成语料只要求**各类命中与不命中都出现**，好让快路径的分桶
逻辑真的被走到。

---- 用法（仓库根执行）-----------------------------------------------------

    PYTHONIOENCODING=utf-8 uv run python tools/bench_filters.py
    PYTHONIOENCODING=utf-8 uv run python tools/bench_filters.py --repeat 50 --paths 20000

原始输出落盘用外部重定向（`> docs/reports/xxx.md`）；**脚本不参与排版**，
与 `bench_embed.py` / `probe_mft.py` / `probe_deps.py` 同一个理由。
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime
from pathlib import Path

from localsearch.ingest.filters import IgnoreRules, _decide_reference

REPO = Path(__file__).resolve().parent.parent
IGNORE_FILE = REPO / ".lsearchignore"

# 语料里出现的目录名分三类：该被剪掉的、该留下的、以及只记文件名的。
# 三类都要有，否则快路径的某一层分桶根本不会被走到，量出来的速度就不算数。
SKIP_DIRS = [
    "node_modules", ".git", "target", "dist", "build", "__pycache__", ".venv",
    "AppData/Local/Temp", "$Recycle.Bin", "Windows/WinSxS", "obj", ".mypy_cache",
]
KEEP_DIRS = [
    "work/test/localsearch/docs", "work/test/localsearch/src/localsearch/ingest",
    "Users/luopu/Documents/报告", "work/test/localsearch/tests/unit", "data/notes",
]
NAME_ONLY = ["Thumbs.db", "desktop.ini", "data.db", "cache.db"]
KEEP_NAMES = ["main.py", "说明.md", "report-2026.docx", "预算表.xlsx", "笔记.txt"]


def make_corpus(n: int, seed: int) -> list[tuple[str, bool]]:
    rng = random.Random(seed)
    corpus: list[tuple[str, bool]] = []
    for i in range(n):
        root = rng.choice(["C:", "D:"])
        if rng.random() < 0.45:
            d = rng.choice(SKIP_DIRS) + f"/sub{i % 97}"
            corpus.append((f"{root}/{d}/{rng.choice(KEEP_NAMES + NAME_ONLY)}", False))
        elif rng.random() < 0.2:
            corpus.append((f"{root}/{rng.choice(SKIP_DIRS)}/nested{i % 53}", True))
        else:
            d = rng.choice(KEEP_DIRS)
            corpus.append((f"{root}/{d}/{rng.choice(KEEP_NAMES + NAME_ONLY)}", False))
    return corpus


def main() -> int:
    ap = argparse.ArgumentParser(description="F2-03 规则判定微基准（纯内存，不碰磁盘）")
    ap.add_argument("--paths", type=int, default=4000, help="合成语料的路径条数")
    ap.add_argument("--repeat", type=int, default=25, help="每条路径判定的轮数")
    ap.add_argument("--seed", type=int, default=20261014, help="语料种子（换了就不是同一批输入）")
    ap.add_argument("--scan-entries", type=int, default=3_470_303,
                    help="折算用的全盘条目数，默认取 E11 的 C: 全盘")
    ap.add_argument("--scan-seconds", type=float, default=122.946,
                    help="同上，scandir 遍历这些条目花掉的秒数")
    args = ap.parse_args()

    rules = IgnoreRules.parse(IGNORE_FILE.read_text(encoding="utf-8"), source=IGNORE_FILE)
    corpus = make_corpus(args.paths, args.seed)
    n = len(corpus) * args.repeat

    def run(fn) -> float:
        t0 = time.perf_counter()
        for _ in range(args.repeat):
            for path, is_dir in corpus:
                fn(path, is_dir)
        return n / (time.perf_counter() - t0)

    # 预热：第一次判定里含正则编译，不该算进吞吐
    for path, is_dir in corpus[:200]:
        rules.decide(path, is_dir=is_dir)
        _decide_reference(rules, path, is_dir=is_dir)

    fast = run(lambda p, d: rules.decide(p, is_dir=d))
    ref = run(lambda p, d: _decide_reference(rules, p, is_dir=d))

    # 速度只有在"两份实现给同一个答案"时才有意义，所以每轮都顺手核一遍
    mismatch = 0
    for path, is_dir in corpus:
        a = rules.decide(path, is_dir=is_dir)
        b = _decide_reference(rules, path, is_dir=is_dir)
        if (a.action, a.rule and a.rule.line) != (b.action, b.rule and b.rule.line):
            mismatch += 1

    print(f"规则文件            {IGNORE_FILE}")
    print(f"规则条数            {len(rules.rules)}")
    print(f"语料                {len(corpus)} 条路径 x {args.repeat} 轮 = {n} 次判定（种子 {args.seed}）")
    print(f"decide() 快路径     {fast:,.0f} 判定/秒")
    print(f"参考实现 逐条全扫   {ref:,.0f} 判定/秒")
    print(f"加速比              {fast / ref:.1f}x")
    print(f"结果不一致          {mismatch} / {len(corpus)}")
    print(f"折算：全盘 {args.scan_entries:,} 条目")
    print(f"  排除规则耗时      {args.scan_entries / fast:,.1f} s（占 scandir {args.scan_seconds:,.1f} s 的 "
          f"{args.scan_entries / fast / args.scan_seconds * 100:.0f}%）")
    print(f"  若用逐条全扫      {args.scan_entries / ref / 60:,.1f} 分钟")
    print(f"机器时间            {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
