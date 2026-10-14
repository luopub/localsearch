"""在真实目录树上跑一遍扫描器，把账目与「谁被排除掉了」按规则打出来。

`F2-01` 的实测入口（可复跑）。它**不是** `F2-06` 的 `lsearch index --dry-run`：
后者现在是 CLI 上的一条正式命令，而本脚本只用到 `ingest/` 这一层、不需要有效配置、
可以用任意 roots 跑，用来取 `F2-01` / `F2-05` / `F2-06` 那几轮记录里的数字。

**两处读的是同一份账目**：分桶与计数在 `ingest/report.py`（`F2-06` 落地时从本脚本
搬过去的），本脚本只负责排版。搬家的理由很具体——分桶规则要改（标记单独成栏）时，
如果两处各有一份，CLI 与脚本迟早给出不同的数。

---- 用法（仓库根执行）-----------------------------------------------------

    uv run python tools/scan_report.py
    uv run python tools/scan_report.py D:/work
    uv run python tools/scan_report.py --json > 报告.json
    PYTHONIOENCODING=utf-8 uv run python tools/scan_report.py     # 想要 UTF-8 时

**不需要设 `PYTHONIOENCODING`**：输出只用 GBK（本机默认控制台的代码页 936）也认得
的字符，直接跑得起来。⚠ **打印路径上不要引入 GBK 编不了的字符**——U+2212（减号）
踩过一次（2026-10-15 01:24:49）：`print` 抛 `UnicodeEncodeError`，**整轮实测的输出
一条都没留下**，包括那行之前已经打出来的账目。汉字、「」、`×`、`→`、`…` 都没问题，
ASCII 的 `-` 也没问题。`tests/unit/test_scan_report.py` 在 gbk 与 utf-8 两种编码下
各跑一遍这个脚本，专门防这件事复发。

不给 roots 就用 `config/config.toml` 里 `[paths] roots` 的当前值。给了 roots 就
**同时**用于构造 `IgnoreRules` 与 `ScanConfig`——两边不一致时扫描器会直接报错
（含 `/rel` 的规则靠 roots 解释，不一致会静默不命中，见 `ingest/base.py`）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from localsearch.core.config import load_config
from localsearch.ingest.base import ScanConfig, ScanStats
from localsearch.ingest.filters import PROJECT_ROOT, IgnoreRules
from localsearch.ingest.report import (
    TABLE_NAME_ONLY,
    TABLE_SKIPPED,
    Distribution,
    collect,
)
from localsearch.ingest.scanner_scandir import ScandirScanner


def run(roots: tuple[str, ...], ignore_file: Path, *, follow: bool, data_dir: str) -> dict:
    config = ScanConfig(roots=roots, data_dir=data_dir, follow_symlinks=follow)
    rules = IgnoreRules.from_file(ignore_file, roots=roots)
    report = collect(ScandirScanner(config), rules)

    return {
        "roots": list(roots),
        "ignore_file": str(ignore_file),
        "rules_total": len(rules.rules),
        "stats": report.stats,
        "wall_seconds": report.seconds,
        "dist": report.dist,
    }


# ==============================================================================
# 呈现
# ==============================================================================


def print_table(title: str, dist: Distribution, which: str, *, limit: int) -> None:
    rows = dist.rows(which)
    kinds = dist.kind_totals(which)
    print(f"\n{title}（{len(rows)} 条分组，命中 > 0 的）")
    for kind, hits in kinds.items():
        if hits.total:
            print(
                f"    [{kind}] {hits.dirs:>8} 目录 {hits.files:>9} 文件"
                f" {hits.bytes / 1024 / 1024:>11.1f}M"
            )
    if not rows:
        print("    （无）")
        return
    print(f"    {'目录':>8} {'文件':>9} {'大小':>11}   归属 / 规则")
    for (kind, where, raw), hits in rows[:limit]:
        print(
            f"    {hits.dirs:>8} {hits.files:>9} {hits.bytes / 1024 / 1024:>10.1f}M"
            f"   [{kind}] {where}  {raw!r}"
        )
        print(f"        例：{hits.example}")
    if len(rows) > limit:
        rest = sum(h.total for _, h in rows[limit:])
        print(f"    …另有 {len(rows) - limit} 条，合计 {rest} 条")


def print_unhit_rules(dist: Distribution, *, limit: int) -> None:
    """一条都没管到的规则。两种成因（被盖掉 / 没匹配上）在这里分不开，文案里写明。"""
    rules = dist.rules_with_no_hits()
    print(f"\n一条都没管到的规则（{len(rules)} / {len(dist.rules)} 条；"
          "「被后面的规则盖掉」与「压根没匹配上」两种成因在这里分不开）")
    if not rules:
        print("    （无）")
        return
    for rule in rules[:limit]:
        print(f"    {rule.origin}")
    if len(rules) > limit:
        print(f"    …另有 {len(rules) - limit} 条")


def print_stats(stats: ScanStats) -> None:
    gib = stats.bytes_index / 1024**3
    print("\n账目")
    print(f"    根目录        {stats.roots_scanned} / {stats.roots_total} 可达")
    # 「没扫的根」而不是「不可达」：`roots_unreachable` 从 F2-05 起也收被目录级标记
    # 排除的根，把那种情形印成"不可达"是在报表里说错话。
    for raw, why in stats.roots_unreachable:
        print(f"      没扫        {raw}：{why}")
    print(f"    条目总数      {stats.entries_seen:,}（目录 {stats.dirs_seen:,} + 文件 {stats.files_seen:,}）")
    # 这里的减号必须是 ASCII 的 `-`：U+2212 是 GBK 编不了的字符，本机默认控制台
    # （代码页 936）会直接抛 UnicodeEncodeError，把整轮实测的输出全吃掉——实测过
    # 一次（2026-10-15 01:24:49），见 PROGRESS §4 P2 的 F2-05 那条。
    print(f"    进入的目录    {stats.dirs_entered:,}（'看见的目录 - 剪掉的' + 根目录数）")
    print(f"    保留的目录    {stats.dirs_kept:,}（剪掉 {stats.dirs_pruned:,} / reparse {stats.dirs_reparse:,}）")
    print(f"    文件：正文    {stats.files_index:,}（{gib:.2f} GiB）")
    print(f"          仅名字  {stats.files_name_only:,}")
    print(f"          排除    {stats.files_skipped:,}")
    print(f"          超限    {stats.files_over_limit:,}（已计入「仅名字」）")
    print(f"    事件时间      {stats.seconds:.2f} s（含回调；含 IO 等待）")
    if stats.reparse_tags:
        tags = "  ".join(f"0x{t:X}×{n}" for t, n in sorted(stats.reparse_tags.items()))
        print(f"    reparse tag   {tags}")
    print(f"    读不到的      {stats.errors_total:,} 条（样本最多 {len(stats.errors)}）")
    for path, why in stats.errors:
        print(f"      {path}：{why}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", help="要扫的目录（默认为配置里的 [paths] roots）")
    ap.add_argument("--ignore-file", default=str(PROJECT_ROOT / ".lsearchignore"))
    ap.add_argument("--limit", type=int, default=25, help="每张表最多列几行（默认 25）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给记录用）")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="跟随 reparse point。⚠ v1 扫描器会拒绝：见 R4")
    args = ap.parse_args(argv)

    config = load_config()
    roots = tuple(args.roots) if args.roots else tuple(config.paths.roots)
    if not roots:
        print("没有 roots：要么在命令行给，要么在 config/config.toml 的 [paths] 里写。",
              file=sys.stderr)
        return 2
    ignore_file = Path(args.ignore_file)
    if not ignore_file.exists():
        print(f"排除规则文件不存在：{ignore_file}", file=sys.stderr)
        return 2

    out = run(roots, ignore_file, follow=args.follow_symlinks,
              data_dir=config.paths.data_dir)

    if args.json:
        stats = out["stats"]
        dist = out["dist"]
        payload = {
            "roots": out["roots"],
            "ignore_file": out["ignore_file"],
            "wall_seconds": out["wall_seconds"],
            "stats": {
                k: v for k, v in vars(stats).items() if not isinstance(v, dict)
            }
            | {"reparse_tags": {f"0x{t:X}": n for t, n in stats.reparse_tags.items()}},
            # 键仍是 `栏|归属|原文`，只是现在多了一个「目录标记」栏（F2-06 的遗留第 8 条）。
            "skipped": {
                f"{kind}|{where}|{raw}": vars(h)
                for (kind, where, raw), h in dist.rows(TABLE_SKIPPED)
            },
            "name_only": {
                f"{kind}|{where}|{raw}": vars(h)
                for (kind, where, raw), h in dist.rows(TABLE_NAME_ONLY)
            },
            "rules_with_no_hits": [rule.origin for rule in dist.rules_with_no_hits()],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描根        {'、'.join(roots)}")
    print(f"排除规则      {out['ignore_file']}（{out['rules_total']} 条）")
    print(f"机器时间      {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print_stats(out["stats"])
    print_table("被排除掉的", out["dist"], TABLE_SKIPPED, limit=args.limit)
    print_table("只记名字的", out["dist"], TABLE_NAME_ONLY, limit=args.limit)
    print_unhit_rules(out["dist"], limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
