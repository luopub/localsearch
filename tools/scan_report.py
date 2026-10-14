"""在真实目录树上跑一遍扫描器，把账目与「谁被排除掉了」按规则打出来。

`F2-01` 的实测入口（可复跑）。它**不是** `F2-06` 的 `lsearch index --dry-run`：
那个最终会进 CLI，排在数据目录与库表之后；本脚本只用到 `ingest/` 这一层，所以
在 P2 中途就能跑，用来回答两个问题：

1. **扫描器在真机上跑不跑得起来、跑多久、有多少条读不到**（`A1` 的"无未捕获异常"）；
2. **排除规则到底命中了什么**——按 `(分区, 规则原文, 行号)` 分组，目录与文件分开
   计数，因为剪掉一个目录会带走整棵子树，与跳过单个文件不是一个量级。

第 2 问与 `F2-06` 的 `--dry-run` 要的是同一份分布，差异只在呈现方式；这一版先把
数据做出来，`F2-06` 再决定 CLI 上长什么样。

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
import collections
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from localsearch.core.config import load_config
from localsearch.ingest.base import Action, ScanConfig, ScanEntry, ScanStats
from localsearch.ingest.filters import PROJECT_ROOT, IgnoreRules
from localsearch.ingest.scanner_scandir import ScandirScanner

#: 非规则原因（「规则之外的原因」）在报表里按前 28 个字符归并：错误消息里带路径，
#: 原样分组会得到几百个各含 1 条的分组，等于没分组。
OTHER_PREFIX = 28


def bucket(entry: ScanEntry) -> tuple[str, str, str]:
    """这条该记在谁的账上。

    「有规则命中」的判据是 `reason` 与 `decision.reason` **相同**：reparse point、
    `data_dir` 硬剔除、嵌套根、`max_file_mb` 超限都会改写 `reason`，但它们的
    `decision` 仍然是规则怎么说。把后者算到规则头上，会让 `--dry-run` 的报告里
    出现"规则 X 排掉了 3 万条"这种与规则无关的数字。
    """
    rule = entry.decision.rule
    if rule is not None and entry.reason == entry.decision.reason:
        return ("规则", f"{rule.section} 第 {rule.line} 行", rule.raw)
    return ("规则之外", "—", entry.reason[:OTHER_PREFIX])


@dataclass
class Hits:
    """一条规则（或一条"规则之外的原因"）的命中数。目录与文件分开。"""

    dirs: int = 0
    files: int = 0
    bytes: int = 0
    example: str = ""

    @property
    def total(self) -> int:
        return self.dirs + self.files


def run(roots: tuple[str, ...], ignore_file: Path, *, follow: bool, data_dir: str) -> dict:
    config = ScanConfig(roots=roots, data_dir=data_dir, follow_symlinks=follow)
    rules = IgnoreRules.from_file(ignore_file, roots=roots)
    scanner = ScandirScanner(config)

    skipped: dict[tuple[str, str, str], Hits] = collections.defaultdict(Hits)
    name_only: dict[tuple[str, str, str], Hits] = collections.defaultdict(Hits)

    def note(table, entry: ScanEntry) -> None:
        hits = table[bucket(entry)]
        if entry.is_dir:
            hits.dirs += 1
        else:
            hits.files += 1
            hits.bytes += max(entry.size, 0)
        if not hits.example:
            hits.example = entry.path

    def on_skip(entry: ScanEntry) -> None:
        note(skipped, entry)

    def on_entry(entry: ScanEntry) -> None:
        # 只记「留着但没提正文」的：它们是 NAME_ONLY 的成本构成（`.ts`、`*.jpg`
        # 这类规则把成千上万个文件挡在正文之外），与"排掉"是两回事。
        if entry.action is Action.NAME_ONLY:
            note(name_only, entry)

    t0 = time.perf_counter()
    stats = scanner.scan(rules=rules, on_entry=on_entry, on_skip=on_skip)
    wall = time.perf_counter() - t0

    return {
        "roots": list(roots),
        "ignore_file": str(ignore_file),
        "rules_total": len(rules.rules) if hasattr(rules, "rules") else -1,
        "stats": stats,
        "wall_seconds": wall,
        "skipped": skipped,
        "name_only": name_only,
    }


# ==============================================================================
# 呈现
# ==============================================================================


def print_table(title: str, table: dict, *, limit: int, show_bytes: bool) -> None:
    rows = sorted(table.items(), key=lambda kv: kv[1].total, reverse=True)
    print(f"\n{title}（{len(rows)} 条，命中 > 0 的）")
    if not rows:
        print("    （无）")
        return
    head = f"    {'目录':>8} {'文件':>9}"
    if show_bytes:
        head += f" {'大小':>10}"
    print(head + "   归属 / 例子")
    for (kind, where, raw), hits in rows[:limit]:
        line = f"    {hits.dirs:>8} {hits.files:>9}"
        if show_bytes:
            line += f" {hits.bytes / 1024 / 1024:>9.1f}M"
        line += f"   [{kind}] {where}  {raw!r}"
        print(line)
        print(f"        例：{hits.example}")
    if len(rows) > limit:
        rest = sum(h.total for _, h in rows[limit:])
        print(f"    …另有 {len(rows) - limit} 条，合计 {rest} 条")


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
        payload = {
            "roots": out["roots"],
            "ignore_file": out["ignore_file"],
            "wall_seconds": out["wall_seconds"],
            "stats": {
                k: v for k, v in vars(stats).items() if not isinstance(v, dict)
            }
            | {"reparse_tags": {f"0x{t:X}": n for t, n in stats.reparse_tags.items()}},
            "skipped": {f"{kind}|{where}|{raw}": vars(h) for (kind, where, raw), h in
                        sorted(out["skipped"].items(), key=lambda kv: -kv[1].total)},
            "name_only": {f"{kind}|{where}|{raw}": vars(h) for (kind, where, raw), h in
                          sorted(out["name_only"].items(), key=lambda kv: -kv[1].total)},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描根        {'、'.join(roots)}")
    print(f"排除规则      {out['ignore_file']}（{out['rules_total']} 条）")
    print(f"机器时间      {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print_stats(out["stats"])
    print_table("被排除掉的", out["skipped"], limit=args.limit, show_bytes=True)
    print_table("只记名字的", out["name_only"], limit=args.limit, show_bytes=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
