r"""Pillow 12.3.0 在本机的行为探针（`F3-04` 的依据）。

------------------------------------------------------------------------------
为什么要有这个探针
------------------------------------------------------------------------------

`media_meta.py` 里有五条断言不是从文档抄来的、而是在本机量出来的，而**其中三条
没有任何测试盯着**（测试盯的是"我们的处置"，不是"Pillow 的性质"）：

1. **哪些扩展名 Pillow 打得开**（`IMAGE_EXTS` 的名单与"故意不认领哪些"的依据）。
   ⚠ 打得开 ≠ 认得出内容：注册表里有 `.ps` / `.pdf` / `.eps` 这类**不是图片**的
   条目，而 `heic` / 相机 RAW 根本不在里面。
2. **读元数据会不会解码像素**（模块文档第一段那句"PNG 是个例外"的依据）。
   ⚠ 这一条**有测试钉着**（`test_a_jpeg_is_never_decoded` 与
   `test_png_does_go_through_load_and_that_is_pillows_choice`），放在这里是为了
   让"为什么会有这个差别"能当场看见。
   ⚠ **初稿在这里量错过，两遍**，两遍都很值得留着：第一版把样本造在桩**里面**，
   于是 `save()` 自己调的 `load()` 让四种格式一起报"解码了"；第二版把结论写成
   "PNG 的 `getexif()` 与 `text` 都调 `load()`"——**在带 `eXIf` 块的 PNG 上是假
   的**。真机制更细：`text` 永远调，`getexif()` 只在没有 `eXIf` 块时调。
3. **失败怎么分类**（`DecompressionBombError` **不是** `OSError`、
   `UnidentifiedImageError` **是**、截断的头抛 `OSError`）——`A6` 的失败隔离
   直接建立在这上面。⚠ 这一条也**有测试钉着**（三条 `ParseFailed` 的测试）。
4. **损坏的 EXIF 段会不会抛异常**（实测：不抛，只发一条 `UserWarning`）。
   ⚠ **没有测试盯着**——所以它是"坏元数据不是坏图片"这个处置的依据。
5. **写样本时的两个坑**（`F3-06` 造 fixtures 之前必须知道）：
   `str` 类型的 EXIF 字段落盘时**非 ASCII 会被换成 `?`**，
   以及 TIFF 在 `Orientation=6` 下 `image.size` 是**交换过的**。

第 4、5 两条**只有这个探针量过**——按 §3 的规矩（`E12`），一次性内联脚本是最弱
一档证据，而 `F3-06` 要把"每种格式最小样本 + 损坏样本"落盘，正撞在这两条上。

------------------------------------------------------------------------------
它做什么
------------------------------------------------------------------------------

五段，各自打印读数，**不下结论**（结论在 `media_meta.py` 的模块文档与 §3 里）：

1. `Image.OPEN` 注册表里的扩展名全集。
2. 五种样本 × 「读 EXIF / 读容器文本 / 读 info」时 `Image.Image.load` 有没有被调用
   （把 `load` 换成抛异常的桩，看谁中招），外加一列"桩接得上吗"——
   桩接不上时"没解码"是假的，得能当场看出来。
3. 四种坏法的异常类型与 MRO（看清谁继承 `OSError`、谁不继承）。
4. 一段坏掉的 EXIF：抛异常还是发警告？正文里剩下什么？
5. 非 ASCII 的 `ImageDescription` 与 `Orientation=6` 的 TIFF。

⚠ 样本全部现造，写在 `tempfile` 里、跑完删掉。**只碰 `C:` 的临时目录。**

用法：

    PYTHONIOENCODING=utf-8 uv run python tools/probe_pillow.py
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import warnings

from PIL import ExifTags, Image, ImageFile, TiffImagePlugin

from localsearch.core.console import use_safe_output
from localsearch.extract.media_meta import IMAGE_EXTS

BY_NAME = {name: tag for tag, name in ExifTags.TAGS.items()}

#: 我们**故意不认领**的、值得逐条看一眼的扩展名（`media_meta.py` 那两张表里的）。
UNCLAIMED = ["heic", "heif", "cr2", "cr3", "nef", "arw", "dng", "gif", "bmp",
             "tga", "ico", "cur", "psd", "psb", "jp2", "j2k", "svg", "mp3"]


def section(number: int, title: str) -> None:
    print(f"\n{'=' * 78}\n{number}. {title}\n{'=' * 78}")


def make(fmt: str, *, exif: bytes | None = None, comment: bytes | None = None,
         size: tuple[int, int] = (40, 30)) -> bytes:
    """现造一个样本，返回字节。"""
    image = Image.new("RGB", size, (9, 9, 9))
    extra: dict = {}
    if exif is not None:
        extra["exif"] = exif
    if comment is not None:
        extra["comment"] = comment
    buffer = io.BytesIO()
    image.save(buffer, fmt, **extra)
    return buffer.getvalue()


def exif_blob(fields: dict, *, size: tuple[int, int] = (40, 30)) -> bytes:
    image = Image.new("RGB", size)
    exif = image.getexif()
    for name, value in fields.items():
        exif[BY_NAME[name]] = value
    return exif.tobytes()


#: 桩可以打在哪两处。`ImageFile.ImageFile.load` 是 `TiffImageFile` / `WebPImageFile`
#: 的父类，`Image.Image.load` 又是它的下游——两组读数对照着看才知道桩接在哪一层。
TRAPS = {"Image.Image": (Image.Image, "load"),
         "ImageFile.ImageFile": (ImageFile.ImageFile, "load")}


def _patch(owner: object, attribute: str):
    """把 `owner.attribute` 换成抛异常的桩，返回还原用的原值。"""
    boom = AssertionError("load() 被调用了")
    original = getattr(owner, attribute)
    setattr(owner, attribute, lambda *a, **k: (_ for _ in ()).throw(boom))
    return original


def trap_is_armed(data: bytes, target: str = "Image.Image") -> str:
    """这个样本**真去 `.load()`** 时，桩接得住吗？

    接不住的话，同一行的"没解码"是**桩没接上**，不是真的没解码——那是最坏的一种
    读数：看着正常，意思反了。
    """
    owner, attribute = TRAPS[target]
    original = _patch(owner, attribute)
    try:
        Image.open(io.BytesIO(data)).load()
        return "★没接上★"
    except AssertionError:
        return "接得上"
    except Exception as exc:                                  # 格式不支持之类
        return f"（{type(exc).__name__}）"
    finally:
        setattr(owner, attribute, original)


def probe_load(data: bytes, target: str, kind: str) -> str:
    """把 `target` 那头换成抛异常的桩，看**读**这个样本的 EXIF / 容器文本 / info
    有没有走到它。

    ⚠ **样本必须先在桩外面造好**：`image.save()` 自己就会调 `load()`，把
    `make(...)` 写在桩里面的话，五种格式会**一起**报"解码了"——那量到的是
    "存样本要解码"，不是"读元数据要解码"。本探针的第一版就是这么写的，
    而它给出的读数看起来完全正常（整整齐齐几行），只是结论是反的。
    """
    owner, attribute = TRAPS[target]
    original = _patch(owner, attribute)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            image = Image.open(io.BytesIO(data))
            if kind == "getexif":
                image.getexif()
            elif kind == "text":
                getattr(image, "text", None)
            else:
                dict(image.info)
        return "不需要"
    except AssertionError:
        return "★需要★"
    except Exception as exc:                                  # 格式不支持之类
        return f"读不了（{type(exc).__name__}）"
    finally:
        setattr(owner, attribute, original)


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    print(f"Pillow {Image.__version__}；本机 Python {os.sys.version.split()[0]}")
    workdir = tempfile.mkdtemp(prefix="probe-pillow-")

    try:
        # ---------------------------------------------------------------- 1
        section(1, "注册表里的扩展名")
        # ⚠ 两个坑，都在首跑里当场撞到：
        #   (a) `Image.OPEN` **必须先 `Image.init()`** 才有内容（不调它是空的，
        #       而空字典会静默给出"0 个扩展名"这个看起来像结论的东西）；
        #   (b) `Image.OPEN` 的值**不是扩展名列表**，是 `(ImageFile 类, accept 函数)`
        #       这种二元组（Pillow 12.3.0 实测）——扩展名在 `Image.EXTENSION` 里，
        #       值才是格式名。拿 `OPEN` 去取扩展名会得到一堆类与函数。
        #       ⚠ 本探针的第一版就是这么写的，而它**报错的方式**很误导：
        #       `TypeError: '<' not supported between instances of 'type' and 'function'`
        #       ——像是排序写错了，其实是取错了表。
        Image.init()
        extension_map = getattr(Image, "EXTENSION", {})
        registered = sorted(one.lstrip(".").lower() for one in extension_map)
        print(f"`Image.EXTENSION` 共 {len(registered)} 个扩展名：\n    {', '.join(registered)}")
        print(f"\n（另：`Image.OPEN` 有 {len(Image.OPEN)} 个格式条目，"
              "值是 `(ImageFile 类, accept 函数)` 二元组，不是扩展名）")
        # ⚠ 这份名单**从 `IMAGE_EXTS` 现取**，不抄一遍：抄的那一版写的是"12 个"，
        # 而后面的名单只有 11 个——数字与事实分开维护，漂移是迟早的事。
        print(f"\n我们认领的 {len(IMAGE_EXTS)} 个：", ", ".join(
            f"{one}{'' if one in registered else '★不在★'}"
            for one in sorted(IMAGE_EXTS)))
        print("\n我们故意不认领的那批，在不在注册表里：")
        for ext in UNCLAIMED:
            print(f"    {ext:6s} {'在' if ext in registered else '不在'}")
        print("\n⚠ 「在注册表里」只说明 Pillow 认得这个后缀，不说明它读得出典型样本；")
        print("   反过来「不在」是硬的：`heic` / `heif` / 相机 RAW 那一族连后缀都不认。")

        # ---------------------------------------------------------------- 2
        section(2, "读元数据有没有解码像素")
        # PNG 分两行：**有没有 `eXIf` 块**正是那处差别的来源，混成一行就看不见了。
        samples = [
            ("JPEG", make("JPEG", exif=exif_blob({"Model": "M"}))),
            ("PNG+eXIf", make("PNG", exif=exif_blob({"Model": "M"}))),
            ("PNG-eXIf", make("PNG")),
            ("TIFF", make("TIFF", exif=exif_blob({"Model": "M"}))),
            ("WEBP", make("WEBP", exif=exif_blob({"Model": "M"}))),
        ]
        # 哪一层提供的 `load`、桩接不接得上，都要一并印出来：桩打在 `Image.Image`
        # 上，而 `TiffImageFile` / `WebPImageFile` **自己覆盖了 `load`**。要是哪个
        # 格式的解码路径绕开了父类那个 `load`，桩就接不上，而"没炸"会被读成
        # "没解码"——**读数看着正常，意思反了**。所以每次先正着炸一回。
        print(f"{'样本':10s} {'load 来自':16s} {'桩接得上':8s} "
              f"{'getexif()':11s} {'text':11s} {'info':11s}")
        for label, data in samples:
            opener = Image.open(io.BytesIO(data))
            owner = next(one.__qualname__ for one in type(opener).__mro__
                         if "load" in one.__dict__)
            print(f"{label:10s} {owner:16s} {trap_is_armed(data):8s} "
                  f"{probe_load(data, 'Image.Image', 'getexif'):11s} "
                  f"{probe_load(data, 'Image.Image', 'text'):11s} "
                  f"{probe_load(data, 'Image.Image', 'info'):11s}")
        print("\n⚠ 两行 PNG 的差别全在 `getexif()` 那一格上：`eXIf` 块在时它不碰像素，")
        print("   不在时才去解。源码就是一句 `if \"exif\" not in self.info: self.load()`")
        print("   （`PngImagePlugin.py:1097`）；`text` 那一列 PNG 两行都是「★需要★」，")
        print("   因为 `PngImageFile.text` 只要 `_text is None` 就 `self.load()`")
        print("   （`PngImagePlugin.py:835`，注释写着 `tEXt` 可以排在 `IDAT` 后面）。")
        print("⚠ 所以「PNG 会解码」有**两个**来源，只堵 `getexif()` 那条不够；")
        print("   而 `media_meta._container_lines()` 对每种格式都取 `image.text`，")
        print("   于是每张 PNG 都会被解出来——**这一行是我们自己写的**。")
        print("⚠ 同一张表换个桩再跑一遍（`ImageFile.ImageFile.load`，对照）：")
        for label, data in samples:
            print(f"    {label:10s} {probe_load(data, 'ImageFile.ImageFile', 'getexif'):11s} "
                  f"{probe_load(data, 'ImageFile.ImageFile', 'text'):11s} "
                  f"{probe_load(data, 'ImageFile.ImageFile', 'info'):11s}")

        # ---------------------------------------------------------------- 3
        section(3, "四种坏法的异常类型与 MRO")
        cases: list[tuple[str, object]] = []
        good = make("JPEG", exif=exif_blob({"Model": "M"}))
        start = good.index(b"\xff\xe1")
        declared = int.from_bytes(good[start + 2:start + 4], "big")
        junk = b"Exif\x00\x00" + b"MM\x00\x2a" + b"\xff" * 8
        cases.append(("改名：内容不是图片", b"this is not an image at all\n" * 3))
        cases.append(("截断的头（SOI 之后断了）", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"))
        cases.append(("坏 EXIF 段（结构烂掉）",
                      good[:start] + b"\xff\xe1" + (len(junk) + 2).to_bytes(2, "big")
                      + junk + good[start + 2 + declared:]))
        cases.append(("解压炸弹（自报像素数超上限）",
                      make("PNG", size=(2000, 2000))))
        for label, data in cases:
            old = Image.MAX_IMAGE_PIXELS
            if label.startswith("解压炸弹"):
                Image.MAX_IMAGE_PIXELS = 1
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    image = Image.open(io.BytesIO(data))
                    image.getexif()
                    text = getattr(image, "text", None)
                outcome = (f"没抛异常；警告 {len(caught)} 条"
                           f"{'：' + str(caught[0].message)[:48] if caught else ''}"
                           f"；text={'空' if not text else '有'}")
            except BaseException as exc:                       # noqa: BLE001 - 就是要看类型
                mro = " ← ".join(one.__name__ for one in type(exc).__mro__[:4])
                outcome = f"{type(exc).__name__}   MRO: {mro}"
            finally:
                Image.MAX_IMAGE_PIXELS = old
            print(f"    {label}\n        {outcome}")
        print("\n⚠ `DecompressionBombError` 的 MRO 里**没有 OSError**——少接它那一族，")
        print("   它就会以未捕获异常冒出批量层；`UnidentifiedImageError` 反过来是 `OSError`。")

        # ---------------------------------------------------------------- 4
        section(4, "一段坏掉的 EXIF：抛异常还是发警告")
        broken = cases[2][1]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            image = Image.open(io.BytesIO(broken))
            fields = dict(image.getexif())
            text = getattr(image, "text", None)
        print(f"    异常：无（这一条本身就是读数）")
        print(f"    警告 {len(caught)} 条：")
        for one in caught:
            print(f"        [{one.category.__name__}] {one.message}")
        print(f"    EXIF 字段：{fields}")
        print(f"    text：{'空' if not text else text}")

        # ---------------------------------------------------------------- 5
        section(5, "写样本时的两个坑")
        chinese = "日落时的外滩"
        blob = exif_blob({"ImageDescription": chinese})
        print(f"    ① 非 ASCII 的 str 字段")
        print(f"       写进去的（内存里）：{chinese!r}")
        print(f"       tobytes() 里有 UTF-8 字节：{chinese.encode('utf-8') in blob}")
        print(f"       tobytes() 里有 '?'：{b'?' in blob}")
        for fmt in ["JPEG", "TIFF"]:
            back = Image.open(io.BytesIO(make(fmt, exif=blob)))
            print(f"        落盘成 {fmt} 再读回来：{back.getexif().get(BY_NAME['ImageDescription'])!r}")
        print("       → 结论：**中文走不了这条字段**，`F3-06` 的中文样本要用 XP* / UserComment")
        print("         （那是 bytes 字段，真能带着中文落盘）。")

        orientation = exif_blob({"Orientation": 6}, size=(40, 30))
        for fmt in ["TIFF", "JPEG"]:
            image = Image.open(io.BytesIO(make(fmt, exif=orientation, size=(40, 30))))
            print(f"    ② Orientation=6（样本 40×30，f{fmt}）")
            print(f"        image.size = {image.size}"
                  f"    getexif()[Orientation] = {image.getexif().get(BY_NAME['Orientation'])}")

        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
