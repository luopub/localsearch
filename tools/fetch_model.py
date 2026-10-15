"""一次性预下载嵌入模型权重，并校验落盘的文件。

    PYTHONUTF8=1 uv run python -u tools/fetch_model.py

**默认走上游 huggingface.co，脚本不设 HF_ENDPOINT。** 这个默认值有过一次反转，
理由值得留着，免得以后有人凭直觉改回去：

    本机 `hf-mirror.com` 按**出口 IP** 分诊。
      代理开（本机常态，出口 185.141.57.20，海外）→ 镜像 **308 重定向回上游**，
        走镜像等于白绕一跳；而上游此时完全可用。
      代理关 → 上游直连被 reset（DNS 被污染到 199.59.149.205），
        镜像 200 可用。
    两者互补，不是二选一。所以脚本默认走上游；代理关掉时改用镜像：
        HF_ENDPOINT=https://hf-mirror.com uv run python tools/fetch_model.py

    之所以一开始写成镜像默认，是因为拿 `curl` 量到"上游 6 秒无响应"。那个测量
    是错的：Git Bash 里的 curl **不读 Windows 系统代理**，Python 读。curl 走直连
    撞上被污染的 DNS，Python 一直经 127.0.0.1:7892 正常访问上游。
    **量网络请用将来真正发起请求的那个客户端量。**

三条环境变量的规矩，来源是 CLAUDE.md「Hugging Face / 模型」一节：

1. `HF_HOME` —— 用 `setdefault` **尊重**系统既有值（本机已设为
   `C:\\Users\\luopu\\.cache\\huggingface`，里面已有 14GB 别的模型）。
   脚本**不替用户改**它；想换位置请改系统变量。
2. `HF_ENDPOINT` —— **不设默认**，你设了就用你的。走上游时由 huggingface_hub
   自己内置默认，脚本不去覆盖一个它本来就对的答案。
3. `HF_HUB_OFFLINE` —— **本脚本必须显式关掉它**。它只约束索引/检索的运行时，
   而这里是"下载"这个动作本身，开着就什么都下不到。反过来，运行时
   （P4/P5 的索引与检索）要把它设成 1，见计划 §7.1 第 12 行。

**绝不**设置 `SENTENCE_TRANSFORMERS_HOME`：它会造成第二份权重副本。
sentence-transformers 会去读 `HF_HOME`，不需要额外一份。

关于校验的**能力边界**（别读成比实际更强的话）：本脚本比对 `model.safetensors`
的 SHA256 与 Hub API 公布的哈希。走**上游**时期望值就是官方权威值，这是一次
真校验——能查出传输损坏、截断、写入错误，也能查出上游内容被调包。
走**镜像**时期望值也来自镜像，就只能证明传输没坏，证明不了镜像没被替换。
所以默认走上游这件事，除了可用性，还多一层校验强度上的理由。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from localsearch.core.console import use_safe_output

REPO_ID = "google/embeddinggemma-2"
MAIN_WEIGHT = "model.safetensors"

# ---------------------------------------------------------------------------
# 环境必须在 import huggingface_hub **之前**设好 —— 那个库在导入时就把这些
# 值读进配置对象了，之后再改 os.environ 不会生效。这个顺序不是风格问题。
# ---------------------------------------------------------------------------
os.environ.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
os.environ["HF_HUB_OFFLINE"] = "0"  # 显式覆盖，不只是 setdefault：下载动作本身需要联网


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GiB"


def sha256_of(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
            done += len(block)
            if total:
                print(f"\r      校验中 … {done * 100 // total:3d}%", end="", flush=True)
    print("\r      校验中 … 100%")
    return h.hexdigest()


def print_env() -> None:
    from huggingface_hub import constants

    section("第 1 步  环境（这三条决定了从哪儿下、下到哪）")
    print(f"  HF_HOME            : {os.environ['HF_HOME']}")
    src = "你设的环境变量" if os.environ.get("HF_ENDPOINT") else "huggingface_hub 内置默认（脚本未覆盖）"
    print(f"  HF_ENDPOINT        : {constants.ENDPOINT}   ← {src}")
    if "hf-mirror" in constants.ENDPOINT:
        print("                       ⚠ 走镜像：代理开着时镜像会把请求 308 甩回上游")
    print(f"  HF_HUB_OFFLINE     : {os.environ['HF_HUB_OFFLINE']}   ← 下载动作本身必须联网")

    sth = os.environ.get("SENTENCE_TRANSFORMERS_HOME")
    print(f"  SENTENCE_TRANSFORMERS_HOME : {sth or '（未设）'}"
          + ("   ← 正确，未设；设了会有第二份权重" if not sth else "   ⚠ 被设了，会多一份权重副本"))

    root = os.environ["HF_HOME"] if os.path.isdir(os.environ["HF_HOME"]) \
        else os.path.splitdrive(os.environ["HF_HOME"])[0] + "\\"
    print(f"  HF_HOME 所在盘剩余 : {human(shutil.disk_usage(root).free)}"
          "   （Windows 无符号链接权限时缓存会复制一份，实际占用约为权重的两倍）")


def ask_hub() -> tuple[str | None, int | None, bool]:
    """向 Hub 要文件清单与主权重的官方哈希。

    取不到**不算失败**：下载本身还有 hf 库自己的重试，而 429/500 这类暂时性
    错误不该掐死整轮。取不到就退化成"下完只记录实际哈希、不做比对"，并在
    结尾把这件事说清楚，不装作校验过了。
    """
    section("第 2 步  向 Hub 索取文件清单与官方哈希")
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(REPO_ID, files_metadata=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] 取仓库信息失败: {type(exc).__name__}: {str(exc)[:300]}")
        print("         继续下载；但本轮**无法**比对官方哈希，结尾会标注。")
        return None, None, False

    print(f"  gated: {info.gated} | private: {info.private} | 最后修改: {info.lastModified}")
    print(f"  文件 {len(info.siblings or [])} 个，带 LFS 元数据的标出大小：")
    expect_sha = expect_size = None
    for sib in info.siblings or []:
        lfs = getattr(sib, "lfs", None)  # BlobLfsInfo（dataclass）或 None，不是 dict
        size = getattr(lfs, "size", None)
        print(f"    {sib.rfilename:38s} {human(size) if size else ''}")
        if sib.rfilename == MAIN_WEIGHT and lfs is not None:
            expect_sha = getattr(lfs, "sha256", None)
            expect_size = getattr(lfs, "size", None)

    if expect_sha:
        print(f"  主权重官方 SHA256: {expect_sha}")
    else:
        print(f"  [WARN] 清单里没拿到 {MAIN_WEIGHT} 的 sha256，下载后不做比对")
    return expect_sha, expect_size, True


def fetch() -> str:
    section("第 3 步  下载（只有第一次真的传数据，之后走本地缓存）")
    from huggingface_hub import snapshot_download

    t0 = time.perf_counter()
    path = snapshot_download(repo_id=REPO_ID)
    print(f"  落点: {path}")
    print(f"  耗时: {time.perf_counter() - t0:.1f} s")
    return path


def verify(local_dir: str, expect_sha: str | None, expect_size: int | None) -> bool:
    section("第 4 步  校验")
    weight = os.path.join(local_dir, MAIN_WEIGHT)
    if not os.path.exists(weight):
        print(f"  [FAIL] 找不到 {MAIN_WEIGHT}")
        return False

    size = os.path.getsize(weight)
    size_ok = expect_size is None or size == expect_size
    print(f"  文件大小 : {size:,} B（{human(size)}）"
          + (f"，Hub 公布 {expect_size:,} B → {'一致 ✔' if size_ok else '不一致 ✘'}"
             if expect_size else "，Hub 未提供大小"))

    actual = sha256_of(weight)
    print(f"  实际 SHA256: {actual}")
    if expect_sha:
        print(f"  官方 SHA256: {expect_sha}")
        sha_ok = actual.lower() == expect_sha.lower()
        print(f"  比对结果   : {'一致 ✔' if sha_ok else '不一致 ✘'}")
    else:
        print("  本轮没拿到官方 SHA256，**未做比对**——上面这个值只能留作日后对照，"
              "不能当作「校验通过」")
        sha_ok = True

    print(f"  文件总字节 : {size:,}")
    return size_ok and sha_ok


def inspect_shapes(local_dir: str) -> None:
    section("第 5 步  读元数据（不加载权重，只读 safetensors 头）")
    try:
        from safetensors import safe_open

        with safe_open(os.path.join(local_dir, MAIN_WEIGHT), framework="numpy") as f:
            keys = list(f.keys())
            print(f"  张量数: {len(keys)}")
            interesting = [k for k in keys
                           if any(s in k.lower() for s in ("embed_tokens", "word_embed", "projection", "dense"))]
            for k in interesting[:8]:
                try:
                    print(f"  {k:56s} {f.get_slice(k).get_shape()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {k:56s} （读形状失败: {type(exc).__name__}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] 读取 safetensors 元数据失败: {type(exc).__name__}: {exc}")


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    section("P0  预下载 google/embeddinggemma-2")
    print(f"  开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    print_env()
    expect_sha, expect_size, got_meta = ask_hub()
    local_dir = fetch()
    ok = verify(local_dir, expect_sha, expect_size)
    inspect_shapes(local_dir)

    section("结论")
    print(f"  下载落盘   : {'通过' if ok else '失败'}")
    print(f"  官方哈希   : {'已比对，一致' if got_meta and expect_sha and ok else '未比对（见第 2 步的 WARN）'}")
    print(f"  结束时间   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("  本脚本只把权重取到本地。它**不**回答：")
    print("    本机真实嵌入吞吐（F0-01 / F0-03）→ tools/bench_embed.py")
    print("    ST 与 ONNX int8 谁更快（F0-02 / D9）→ 双跑对比报告")
    print()
    print("  运行时（索引 / 检索）记得设 HF_HUB_OFFLINE=1，见计划 §7.1。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
