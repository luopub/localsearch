"""`lsearch` 命令行入口。

P1 的 `F1-01` 只要求"包可安装、入口点存在"，所以这里先是**最小的真入口**：
它真的能被 `lsearch` 调用、真的返回退出码，不是一个指向不存在模块的假声明。

命令面（`doctor` / `config`）在 `F1-04` 落地后替换本文件；届时 `F1-01` 的绿灯
按「绿灯不自动续期」重跑一次（见 PROGRESS.md §5.2）。
"""

from __future__ import annotations

import sys

from localsearch import __version__


def main(argv: list[str] | None = None) -> int:
    """入口点的目标函数。返回进程退出码。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-V", "--version"):
        print(f"lsearch {__version__}")
        return 0
    print(f"lsearch {__version__} —— P1 骨架阶段，命令面（doctor / config）在 F1-04 落地。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
