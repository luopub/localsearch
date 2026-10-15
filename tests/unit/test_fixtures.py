r"""`tests/fixtures/` 三者的对账：样本、清单、生成器（`F3-06`）。

------------------------------------------------------------------------------
这一组盯的五件事
------------------------------------------------------------------------------

1. **清单自己没写错**——每条记录的字段齐全（`good` 必须有 `text_has`、`broken` 必须
   有 `raises`）、名字与路径唯一、`builder` 都在 `BUILDERS` 里、`raises` 都是
   `ExtractError` 的子类名。清单是这份样本集的**唯一真相来源**，它自己写错的话，
   下面四条都在认真地验一个错的东西。
2. **盘上的字节 = 生成器现在造出来的字节**——逐条重造一份再比。它拦两件事：
   "改了 builder 忘了重跑生成器"与"手改了目录里的文件"。
3. **目录与清单一样大，两个方向都查**——多出来的文件也要红。只管"清单里的都在盘上"
   的话，把一个改坏了的旧文件留在目录里就能让自检保持绿，而那正是两份真相。
4. **`good` 的真的出正文、`broken` 的真的按名报错**——把样本喂进 `build_registry()`
   装出来的**真**注册表，看它是不是清单说的那样。只比字节的话，"字节对、但提取器
   认不出来"照样是绿的，而 fixtures 的用处恰恰是"拿它去试提取器"。
5. **那条命令真的能跑**——`uv run python tools/make_fixtures.py --check` 走真子进程，
   并且**故意把 `PYTHONIOENCODING` 设成 gbk**：本机默认控制台就是 936，这个项目已经
   在"打印中文把控制台炸了"上吃过一次亏（`test_scan_report.py` 里记着那一次，账目
   一行的输出**一条都没留下来**）。生成器打印的全是中文，所以这条护栏得留着。

------------------------------------------------------------------------------
为什么是 importlib 把 `tools/` 当模块引进来自检
------------------------------------------------------------------------------

`tools/` 不是包（没有 `__init__.py`），`make_fixtures.py` 也不是被测模块——它是
**造样本的工具**。但这一组要按清单逐条参数化 21 个 builder，走子进程就得 21 次
解释器启动，而在进程内 `import` 又不可能（不在 `sys.path` 上）。所以走
`spec_from_file_location`：既能拿到 `BUILDERS` 与 `check()`，又不用为了让测试好写
而把 `tools/` 变成包（那会顺带改掉生产代码的布局，代价比这条加载方式大）。

⚠ 它比的是**同一条清单**：`load_manifest()` 由生成器提供，测试不另写一份解析。
一份清单两处解析，漂移是早晚的事。

------------------------------------------------------------------------------
"红了的才是护栏"：这几条各自怎么红
------------------------------------------------------------------------------

- 改 `BUILDERS` 里任何一个 builder 的参数、不重跑生成器 → 第 2 条红；
- 手改目录里任一文件 → 第 2 条红；
- 目录里多放一个没进清单的文件 → 第 3 条红；
- 把某条 `broken` 换成 `good`（或反之）→ 第 1 条与第 4 条红；
- 提取器不再认领某个后缀（比如把 `.webp` 从名单里删掉）→ 第 4 条红，
  且报出来的是"这条样本没人要"而不是"字节变了"，定位直接。

正文里的中文一律写成字面量（不是 `\uXXXX`）：这个项目的语料就是中文。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from localsearch.extract import (
    BinaryContent,
    ExtractConfig,
    ExtractError,
    FileUnreadable,
    ParseFailed,
    UnsupportedFormat,
    build_registry,
    extract_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GENERATOR = REPO_ROOT / "tools" / "make_fixtures.py"

#: 清单里 `raises` 能取的名字。写成名字的字典而不是直接写类，是因为清单是 TOML
#: （存不了类对象），而这一层要把"清单里那串字"翻回"要 `pytest.raises` 什么"。
EXCEPTIONS = {
    cls.__name__: cls
    for cls in (BinaryContent, FileUnreadable, ParseFailed, UnsupportedFormat)
}


def load_generator():
    """把 `tools/make_fixtures.py` 当模块引进来（理由见模块文档）。"""
    spec = importlib.util.spec_from_file_location("make_fixtures", GENERATOR)
    assert spec is not None and spec.loader is not None, GENERATOR
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # 先进 sys.modules 再执行，模块内部自引用才拿得到
    spec.loader.exec_module(module)
    return module


make_fixtures = load_generator()

SAMPLES = make_fixtures.load_manifest()
GOOD = [sample for sample in SAMPLES if sample["kind"] == "good"]
BROKEN = [sample for sample in SAMPLES if sample["kind"] == "broken"]

BY_NAME = pytest.mark.parametrize("sample", SAMPLES, ids=[s["name"] for s in SAMPLES])
GOOD_ONLY = pytest.mark.parametrize("sample", GOOD, ids=[s["name"] for s in GOOD])
BROKEN_ONLY = pytest.mark.parametrize("sample", BROKEN, ids=[s["name"] for s in BROKEN])


def path_of(sample: dict) -> Path:
    return FIXTURES / sample["file"]


# ==============================================================================
# 1. 清单自己
# ==============================================================================


def test_every_builder_in_the_manifest_exists():
    """清单里的 `builder` 都得在 `BUILDERS` 里，一个都不能是凭空写的名字。"""
    unknown = sorted({s["builder"] for s in SAMPLES} - set(make_fixtures.BUILDERS))
    assert unknown == [], f"清单里写了不存在的 builder：{unknown}"


def test_no_builder_is_left_unused():
    """反过来：`BUILDERS` 里的每个 builder 都得被清单用上。

    留着没用的 builder 会慢慢长成"另一份样本集"——写得挺好、没人跑、也没人验。
    """
    unused = sorted(set(make_fixtures.BUILDERS) - {s["builder"] for s in SAMPLES})
    assert unused == [], f"BUILDERS 里这些没被清单用上：{unused}"


def test_names_and_paths_are_unique():
    """名字是测试 id 的前半段、路径是文件本身，两者重一条就意味着有一条被顶掉。"""
    for field in ("name", "file"):
        seen = [s[field] for s in SAMPLES]
        duplicates = sorted({value for value in seen if seen.count(value) > 1})
        assert duplicates == [], f"{field} 重了：{duplicates}"


def test_each_sample_declares_what_it_should():
    """`good` 与 `broken` 各自该有什么字段、不该有什么字段。

    这里把"不该有的"也钉住：一条记录同时写着 `text_has` 与 `raises`，读的人没法知道
    到底期望哪一半——自检也不该去猜。
    """
    for sample in SAMPLES:
        where = sample["name"]
        assert sample["note"].strip(), f"{where}：没写 note（这份样本为什么算最小？）"
        assert sample["extractor"] in ("plaintext", "office", "web_mail", "media_meta"), where
        if sample["kind"] == "good":
            assert sample["text_has"], f"{where}：good 样本必须说清正文里得有什么"
            assert "raises" not in sample, f"{where}：good 样本不该写 raises"
            if "title_in_text" in sample:
                assert "title_has" in sample, f"{where}：没写 title_has，title_in_text 无从谈起"
        else:
            assert sample["raises"] in EXCEPTIONS, f"{where}：raises 得是那个异常类名"
            assert "text_has" not in sample, f"{where}：broken 样本不该写 text_has"
            assert "title_has" not in sample, f"{where}：broken 样本不该写 title_has"


def test_the_directory_says_whether_it_is_good_or_broken():
    """路径的第一段与 `kind` 必须一致（`good/` 里放好样本、`broken/` 里放坏的）。

    目录名是给人看的（"这份样本能不能出正文"一眼就分），写反了目录就在骗人。
    """
    for sample in SAMPLES:
        top = sample["file"].split("/")[0]
        assert top == sample["kind"], f"{sample['name']}：kind={sample['kind']} 却放在 {top}/"


# ==============================================================================
# 2. 盘上的字节 = 生成器现在造出来的字节
# ==============================================================================


@BY_NAME
def test_the_bytes_on_disk_are_what_the_builder_makes_now(sample):
    """逐条重造一份再比（"改了 builder 没重跑生成器"与"手改了文件"都在这儿红）。"""
    path = path_of(sample)
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} 不在盘上"
    on_disk = path.read_bytes()
    fresh = make_fixtures.BUILDERS[sample["builder"]]()
    assert on_disk == fresh, (
        f"{sample['name']} 的字节和生成器造出来的对不上"
        f"（盘上 {len(on_disk)} 字节 / 现在 {len(fresh)} 字节）；"
        "改的是 builder 就跑一次 `uv run python tools/make_fixtures.py`，"
        "手改过目录里的文件就把它改回去——见 MANIFEST.toml 开头那段。"
    )


# ==============================================================================
# 3. 目录与清单一样大
# ==============================================================================


def test_nothing_in_the_directory_is_missing_from_the_manifest():
    """两个方向：清单里的都在盘上、盘上的都在清单里。`--check` 查的是同一件事。"""
    listed = {path_of(sample).resolve() for sample in SAMPLES}
    on_disk = {
        path.resolve()
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.name != "MANIFEST.toml"
    }
    assert listed - on_disk == set(), f"清单里有、盘上没有：{sorted(p.name for p in listed - on_disk)}"
    assert on_disk - listed == set(), (
        f"盘上有、清单里没有：{sorted(p.name for p in on_disk - listed)}"
        "——多出来的样本等于第二份真相，要么进清单、要么删掉"
    )


def test_the_check_command_says_clean():
    """生成器自己的判据也是干净的（与本文件的第 2、3 条是同一件事的两个出口）。"""
    assert make_fixtures.check(SAMPLES) == 0


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把生成器的 `FIXTURES` 指到一棵**复制**出来的临时树。

    下面两条要"目录里多一个文件"、"少一个文件"，而真目录是受保护的（第 2、3 条
    就盯着它一个字节都不许动）。复制一份来动，真目录只读——这比"改完记得改回去"
    可靠，后者失败一次就把仓库弄脏了。
    """
    tree = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, tree)
    monkeypatch.setattr(make_fixtures, "FIXTURES", tree)
    return tree


def test_a_stray_file_in_the_directory_is_reported(sandbox, capsys):
    """盘上有、清单里没有的文件必须被报出来——**这是"两边各查一次"的另一半**。

    ⚠ 这一条是 `tools/check_mutations.py` 那条变体点名的：把 `check()` 里查多出来
    文件的那两行去掉，本文件别的断言仍然全绿（真目录本来就干净），于是"两个方向
    都查"这句话就成了一句没人看着的声明。所以这里主动造一份多出来的文件。
    """
    (sandbox / "good" / "多出来的.txt").write_bytes(b"stray\n")

    extra = make_fixtures.stray(SAMPLES)
    assert [path.name for path in extra] == ["多出来的.txt"]

    assert make_fixtures.check(SAMPLES) == 1
    assert "多出来" in capsys.readouterr().out


def test_write_says_what_it_newly_made(sandbox, capsys):
    """`write()` 对"盘上还没有这个文件"要说**新建**，不能说成"同长不一致"。

    理由是这条消息的用法：它印给人看，而 0 字节 → N 字节的"不一致"读起来像
    "这个文件被人改小了"，实际是"第一次生成"。判据是同一件事（比字节），
    但说错了会让人往错的方向查。
    """
    (sandbox / "good" / "minimal.txt").unlink()

    assert make_fixtures.write(SAMPLES) == 1
    out = capsys.readouterr().out
    assert "新建" in out and "不一致" not in out

    # 写完之后两个方向都干净（新造出来的字节就是生成器的字节）
    assert make_fixtures.check(SAMPLES) == 0


# ==============================================================================
# 4. good 的真的出正文，broken 的真的按名报错
# ==============================================================================


@pytest.fixture(scope="module")
def registry():
    """真注册表：四个提取器按 `build_registry()` 的顺序装上。

    ⚠ 用真注册表而不是直接调 `PlaintextExtractor().extract()`：fixtures 的用处是
    "拿它试**派发**"——"这个后缀归谁"与"这个后缀的正文长什么样"是两件事，四份名单
    挤在一起的时候，前者才是先坏的那一个。
    """
    return build_registry(ExtractConfig())


@GOOD_ONLY
def test_a_good_sample_comes_out_whole(sample, registry):
    """正文里该有的都在、**按清单写的顺序**；`title` 与 `meta` 也照清单认。

    `text_has` 逐条**往后找**（不是各自 `in` 一下就完）：顺序写反了是清单的错，而
    "顺序"这件事本身有意义——`.eml` 的标记行必须在正文之前，反过来说明拼接走样了。
    """
    doc = extract_file(str(path_of(sample)), registry=registry, config=ExtractConfig())

    assert doc.source == sample["extractor"], (
        f"{sample['name']} 归了 {doc.source}，清单说该归 {sample['extractor']}"
    )

    cursor = 0
    for fragment in sample["text_has"]:
        found = doc.text.find(fragment, cursor)
        assert found >= 0, (
            f"{sample['name']}：正文里从第 {cursor} 个字符往后找不到 {fragment!r}"
            f"（正文 {doc.text[:200]!r}）"
        )
        cursor = found + len(fragment)

    if "title_has" in sample:
        assert doc.title is not None, f"{sample['name']}：清单要求有 title，实际是 None"
        assert sample["title_has"] in doc.title, f"{sample['name']}：title 里没有 {sample['title_has']!r}"
        # 标题那一串是不是**也**该在正文里，逐条由清单说了算（默认不该）：
        # HTML 的 `<title>` 只进 `title`，进了正文就是"同一段字切两遍、算两次向量"；
        # 邮件的标记行相反，它**就是**要写主题（`【邮件】主题：…`）。
        # 两个方向都钉住，免得把某一边的现状当成规矩。
        expected_in_text = sample.get("title_in_text", False)
        leaked = sample["title_has"] in doc.text
        assert leaked is expected_in_text, (
            f"{sample['name']}：标题那一串{'在' if leaked else '不在'}正文里，"
            f"清单说该{'在' if expected_in_text else '不在'}"
        )

    for key, expected in sample.get("meta", {}).items():
        assert doc.meta.get(key) == expected, (
            f"{sample['name']}：meta[{key!r}] 是 {doc.meta.get(key)!r}，清单说该是 {expected!r}"
        )


@BROKEN_ONLY
def test_a_broken_sample_raises_the_declared_error(sample, registry):
    """坏样本必须**按名**抛出清单写的那个异常，而且得是 `ExtractError` 那一族。"""
    expected = EXCEPTIONS[sample["raises"]]
    with pytest.raises(expected) as caught:
        extract_file(str(path_of(sample)), registry=registry, config=ExtractConfig())
    # 光 `pytest.raises` 不够：若某个异常族以后改成 `ExtractError` 之外的基类，
    # 批量层（`F3-07`）就不再把它当"文件坏了"接住，而这里仍然会绿。
    assert isinstance(caught.value, ExtractError), f"{sample['name']}：抛出的不是 ExtractError 那一族"


def test_every_registered_extractor_has_at_least_one_good_sample(registry):
    """四个提取器每个都得有好样本，否则"这一类能出正文"这句话没人看着。

    加了第五个提取器却忘了补样本，这一条就红——比"等哪天真需要了再补"可靠。
    """
    covered = {sample["extractor"] for sample in GOOD}
    assert covered == set(registry.names()), (
        f"注册的提取器是 {sorted(registry.names())}，好样本只覆盖了 {sorted(covered)}"
    )


# ==============================================================================
# 5. 那条命令真的能跑（含本机默认控制台的编码）
# ==============================================================================


def test_the_generator_survives_the_default_windows_console():
    """`--check` 走真子进程，`PYTHONIOENCODING=gbk`（本机默认代码页 936）。

    生成器打印的全是中文（"清单" / "一致" / "漂移"），所以这条护栏要在：这个项目
    已经因为"账目一行里有个 GBK 编不出来的减号"丢过一整轮扫描的输出。
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        encoding="gbk",
        env=dict(os.environ, PYTHONIOENCODING="gbk"),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "漂移 0 条" in result.stdout, result.stdout
