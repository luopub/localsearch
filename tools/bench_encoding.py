r"""F3-01 微基准：短数据那一档"中文优先"值不值（`SHORT_BYTES = 32` 的复跑依据）。

对应功能点：**F3-01**（`extract/plaintext.py` 与它的编码判定）。产出指标 **E31**
（`docs/PROGRESS.md` §3）。

---- 为什么需要它 --------------------------------------------------------------

`extract/base.py` 的 `SHORT_BYTES = 32` 那条线是这样来的：把一段连续中文按 2–60 字
逐长度切成 **59 档**，每档 GBK 编码后用三种走法解，数还原得回来几档——自由模式
**46/59**、收窄候选表 **57/59**、短数据直接认 `gb18030` **59/59**。这三个数是
`decode_bytes()` 里"短数据整档跳过探测器"的**唯一依据**。

⚠ 那份读数**原来只有一次性内联脚本量过，脚本没入库**。按 `E12` 立下的规矩
（缺命令与计数的读数无法复跑），这里把它补上——**这是本脚本存在的全部理由**，
它不改任何结论。

---- 量的是什么 ----------------------------------------------------------------

1. **59 档逐长度的判对档数**（三种走法对照），含**逐档明细表**：错在哪几档、
   错成什么、错的档位落在哪个字节区间——`SHORT_BYTES` 取 32 就是按"失败档的上界
   28 加一档余量"定的，所以那个上界必须看得见。
2. **现行实现（`decode_bytes`）的护栏**：59 档**一档都不许错**。前两项是"别的走法
   怎么样"，这一项是"我们实际用的那个怎么样"。
3. **代价那一栏**：短韩文 / 短日文 / 短德文在现行实现下会变成中文乱码，而自由模式
   本来判得对前两种。**这条代价要能被复跑，不能只写在文档里。**
4. **两档的单价**：`decode_bytes` 在短数据（≤ 32 字节，走 `gb18030` 直解、不问探测器）
   与长数据（> 32 字节，要跑 `charset_normalizer`）上各多少微秒。

---- 顺序是一条未受控的变量（E26 / E29 的教训）---------------------------------

第 4 项**A/B 交替、每轮内部换先后、7 轮取中位数**，另有 1 轮预热不计。
**不要把这段改成"先把短数据跑完再跑长数据"**：先量的那一档会吃掉冷启动。

---- 语料为什么是这一句话 ------------------------------------------------------

`SRC` 就是项目自己的一句话（中文、无生僻字、GBK 可编码），逐长度截取得到 59 档。
**确定性**：没有随机数，同一条命令给出同一批输入。换一句话会得到不同的明细表
（哪几档猜成 `big5` 与字节内容有关），所以**换语料要连文档里的数一起换**。

---- 用法（仓库根执行）--------------------------------------------------------

    PYTHONIOENCODING=utf-8 uv run python tools/bench_encoding.py
    PYTHONIOENCODING=utf-8 uv run python tools/bench_encoding.py --max-chars 200 --rounds 9

原始输出落盘用外部重定向（`> docs/reports/xxx.md`）；**脚本不参与排版**，
与 `bench_embed.py` / `bench_filters.py` / `bench_fingerprint.py` / `bench_paths.py`
同一个理由。

⚠ 输出的字符**必须都能被本机控制台编码**（默认代码页 936，`E16`）：本文件只用
汉字与 ASCII，**不打 `U+2212`**——那个字符会让整轮输出在 GBK 终端下全丢。
"""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime

from charset_normalizer import from_bytes

from localsearch.core.console import use_safe_output
from localsearch.extract.base import SHORT_BYTES, decode_bytes

#: 逐长度截取用的中文句子。**确定性语料**，换它就要连文档里的数一起换。
#:
#: ⚠ 这一句是**原样照抄**的：`E31` 那三个数（46 / 57 / 59）就是用它在 2026-10-15 量
#: 出来的，所以它连同"截到几个字"一起被钉在这里——换一句话，明细表里"哪几档猜成
#: `big5`"就会变（那是字节内容决定的），数也就不可比了。
SRC = "本项目的目标是本地离线全盘全文搜索索引本机磁盘上的文档用自然语言或关键词检索命中定位到文件与片段"

#: "收窄候选表"那一档用的候选集：只留本机语料上说得通的编码（中文 + 拉丁 + Unicode 硬证据）。
#: 自由模式的失败里有一大半是 `cp949`（韩文）这类**本机语料里不该出现的猜测**，
#: 收窄就是把这些剔除掉之后的结果——它修不掉"原理上分不开"的那两档。
NARROW = (
    "utf_8", "gb18030", "big5", "big5hkscs", "cp932", "shift_jis", "euc_jp",
    "cp1252", "cp1250", "latin_1", "utf_16", "utf_32",
)


def slice_text(n: int) -> str:
    """`SRC` 的第 `n` 个字起头的一段（不够就循环接上，保证长度正好是 `n`）。"""
    return (SRC * (n // len(SRC) + 1))[:n]


def guess_text(raw: bytes, *, narrow: bool) -> str | None:
    """交给探测器解一段字节；**解不回来或猜错就返回 `None`**（不抛异常）。"""
    best = from_bytes(raw, cp_isolation=list(NARROW)).best() if narrow else from_bytes(raw).best()
    if best is None or not best.encoding:
        return None
    try:
        return str(best)
    except (UnicodeDecodeError, LookupError):
        return None


def sweep_classes(min_chars: int, max_chars: int) -> list[dict]:
    """59 档逐长度的三种走法对照。**每一档都返回明细**，不只返回计数。"""
    rows: list[dict] = []
    for n in range(min_chars, max_chars + 1):
        text = slice_text(n)
        raw = text.encode("gbk")
        free = guess_text(raw, narrow=False)
        narrow = guess_text(raw, narrow=True)
        shipped = decode_bytes(raw)
        rows.append({
            "chars": n,
            "bytes": len(raw),
            "free_enc": _enc_of(raw, free),
            "free_ok": free == text,
            "narrow_ok": narrow == text,
            "shipped_ok": shipped.text == text,
            "shipped_enc": shipped.encoding,
            "shipped_guessed": shipped.guessed,
        })
    return rows


def _enc_of(raw: bytes, decoded: str | None) -> str:
    """猜出来的编码名（只为打印）：把它再解一遍，能对上就是它。"""
    if decoded is None:
        return "?"
    best = from_bytes(raw).best()
    return (best.encoding.lower() if best and best.encoding else "?")


#: "代价"那一栏的样本：(语言, 短样本, 长样本)。短的要 ≤ `SHORT_BYTES`，
#: 长的要远大于它——两档在现行实现里走的是**完全不同的路**。
CASUALTIES = (
    ("韩文", "cp949", "한국어 테스트", "이것은 한국어 테스트 파일입니다. " * 6),
    ("日文", "cp932", "テスト文書", "これは日本語のテスト文書です。 " * 6),
    ("德文", "cp1252", "Grüße Test", "Grüße aus München, dies ist ein Test. " * 6),
)


def decode_with_probe(raw: bytes) -> str | None:
    """"如果不跳探测器，会怎么走"——就是自由模式那一条（`from_bytes().best()`）。"""
    return guess_text(raw, narrow=False)


def measure(fn, samples: list[bytes], rounds: int) -> list[float]:
    """`fn` 在这批样本上的**每一轮**单价（微秒/次）。另有 1 轮预热不计。

    返回**全部轮次**而不是中位数：`E12` / `E17` 那两条教训说的就是"只报一个点值
    会让人以为它有三位有效数字"，所以波动要看得见。
    """
    for raw in samples:                      # 预热：第一轮会带上 codec 查找的冷启动
        fn(raw)
    timings: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for raw in samples:
            fn(raw)
        timings.append((time.perf_counter() - t0) / len(samples) * 1e6)
    return timings


def measure_all(series: list[tuple], rounds: int) -> list[list[float]]:
    """多组一起量：**每轮都换各组之间的先后**，避免"谁先跑谁吃亏"（`E26` 的教训）。

    `series` 的每一项是 `(名字, 函数, 样本)`；返回的一组读数与它一一对应。
    """
    out: list[list[float]] = [[] for _ in series]
    for i in range(rounds):
        order = list(range(len(series)))
        if i % 2:                            # 每轮把顺序翻过来
            order.reverse()
        for k in order:
            _, fn, samples = series[k]
            out[k].extend(measure(fn, samples, 1))
    return out


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    ap = argparse.ArgumentParser(
        description="F3-01 编码判定微基准（纯内存、不碰盘；SHORT_BYTES 的复跑依据）")
    ap.add_argument("--min-chars", type=int, default=2, help="逐长度截取的起点（字数）")
    ap.add_argument("--max-chars", type=int, default=60,
                    help="逐长度截取的终点（字数）。默认 60 即 2-60 字共 59 档，"
                         "与 E31 那次量的是同一批输入；改它会得到另一组计数")
    ap.add_argument("--rounds", type=int, default=7, help="计时取中位数的轮数（另有 1 轮预热不计）")
    args = ap.parse_args()

    rows = sweep_classes(args.min_chars, args.max_chars)
    total = len(rows)

    print("=" * 78)
    print("一、逐长度 59 档：三种走法判对几档（语料是项目自己的一句话，GBK 编码）")
    print("=" * 78)
    print(f"{'字数':>4} {'字节':>5} {'自由模式':>10} {'收窄候选表':>12} {'现行实现':>10}")
    print("-" * 78)
    for r in rows:
        mark = "" if r["free_ok"] else "   <- 自由模式判错"
        print(f"{r['chars']:>4} {r['bytes']:>5} "
              f"{(r['free_enc'] if not r['free_ok'] else 'OK'):>10} "
              f"{('OK' if r['narrow_ok'] else 'BAD'):>12} "
              f"{('OK' if r['shipped_ok'] else 'BAD'):>10}{mark}")

    free_ok = sum(1 for r in rows if r["free_ok"])
    narrow_ok = sum(1 for r in rows if r["narrow_ok"])
    shipped_ok = sum(1 for r in rows if r["shipped_ok"])
    print("-" * 78)
    print(f"{'合计':>4} {'':>5} {free_ok:>8}/{total} {narrow_ok:>10}/{total} {shipped_ok:>8}/{total}")

    fail = [r for r in rows if not r["free_ok"]]
    print()
    print("自由模式判错的档位落在哪个字节区间（那条线就是照它定的）：")
    if fail:
        print(f"  共 {len(fail)} 档；最小 {min(r['bytes'] for r in fail)} 字节、"
              f"最大 {max(r['bytes'] for r in fail)} 字节")
        print(f"  猜到什么：{sorted({r['free_enc'] for r in fail})}")
        print(f"  在线以内的档数（<= SHORT_BYTES={SHORT_BYTES}）："
              f"{sum(1 for r in fail if r['bytes'] <= SHORT_BYTES)}")
        print(f"  在线以外的档数（> SHORT_BYTES，这一档现行实现一个字都不干预）："
              f"{sum(1 for r in fail if r['bytes'] > SHORT_BYTES)}")
    else:
        print("  一档都没有（那 SHORT_BYTES 这条线就没有依据了，先查语料）")

    print()
    print(f"护栏：现行实现（decode_bytes）判错 {total - shipped_ok} / {total} 档"
          f"（**必须是 0**；不是 0 说明 SHORT_BYTES 这条线没兜住）")
    guessed = sum(1 for r in rows if r["shipped_guessed"])
    print(f"      其中被标成「猜出来的」 {guessed} / {total} 档"
          f"（留警告的那一档；BOM / 严格 UTF-8 定下来的不计）")

    print()
    print("=" * 78)
    print("二、代价那一栏：短数据按中文先验解的后果（自由模式本来判得对的不再判对）")
    print("=" * 78)
    print(f"{'语言':>4} {'':>2} {'字节':>5} {'自由模式':>10} {'现行实现':>10} {'还原得回来':>10}")
    print("-" * 78)
    for label, codec, short, long in CASUALTIES:
        for tag, text in (("短", short), ("长", long)):
            raw = text.encode(codec)
            free = guess_text(raw, narrow=False)
            shipped = decode_bytes(raw)
            print(f"{label:>4} {tag:>2} {len(raw):>5} "
                  f"{('OK' if free == text else 'BAD'):>10} "
                  f"{('OK' if shipped.text == text else 'BAD'):>10} "
                  f"{('是' if shipped.text == text else '否'):>10}")
    print("-" * 78)
    print("短的那一档变乱码是**已知代价**（`SHORT_BYTES` 的文档里写了），长的那一档")
    print("一个字都不干预——这正是「只在短数据上」这条边界的意义。")

    print()
    print("=" * 78)
    print("三、跳过探测器值多少（同一批输入、同一种问法，只换走法）")
    print("=" * 78)
    short = [slice_text(r["chars"]).encode("gbk") for r in rows if r["bytes"] <= SHORT_BYTES]
    long_ = [slice_text(r["chars"]).encode("gbk") for r in rows if r["bytes"] > SHORT_BYTES]
    print(f"短数据（<= {SHORT_BYTES} 字节）{len(short)} 档、长数据（> {SHORT_BYTES} 字节）"
          f"{len(long_)} 档")
    print()
    print("  走法                             单价（中位数）      各轮                说明")
    print("-" * 78)
    # ⚠ 前两条是同一批输入上的同一问、只换走法；第三条换了输入长度，**不可与前两条相除**
    # ——那样量到的是"字多字少"，不是"跳没跳探测器"（这个脚本第一版就是这么写错的）。
    series = [
        ("现行·短", decode_bytes, short),
        ("不跳·短", decode_with_probe, short),
        ("现行·长", decode_bytes, long_),
    ]
    got = measure_all(series, args.rounds)
    note = ["实际用的这一条，短数据整档跳过探测器",
            f"短数据若不跳：{statistics.median(got[1]) / statistics.median(got[0]):.1f}x",
            "长数据跳不掉；输入字节数是短数据的 "
            f"{sum(len(b) for b in long_) / sum(len(b) for b in short):.1f}x，与前两条不可比"]
    for (name, _, _), timings, why in zip(series, got, note):
        print(f"  {name:<10}{statistics.median(timings):>10.3f} µs"
              f"      {' '.join(f'{v:.2f}' for v in timings):<20} {why}")
    print("-" * 78)
    print(f"同一批输入换个走法**省下 "
          f"{statistics.median(got[1]) / statistics.median(got[0]):.1f} 倍**——这是跳过探测器的")
    print("价值，不是「短文本比长文本快」（后者是当然的）。长数据那一档跳不掉、也占大头，")
    print("但它是语料决定的，`SHORT_BYTES` 这条线管不着。")

    print()
    print(f"机器时间            {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"语料                项目自己的一句话，逐长度 {args.min_chars}-{args.max_chars} 字（确定性）")
    print(f"SHORT_BYTES         {SHORT_BYTES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
