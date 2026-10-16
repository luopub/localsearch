r"""`lsearch search`（`F4-04` 起的最小形态）：`cli/commands/search.py` 的命令面。

关联验收项 **`A8`**——"Tier2 未完成时必须降级并**显式标注**「语义未就绪」，
不允许静默返回不完整结果"。这一条的可观察形态就是本文件里那几条断言：一句逐字含
「语义未就绪」的话，加上"这一趟实际按什么检索、因此少了什么"。

本文件钉的是**静默错误**——它们不抛异常，输出看起来完全正常：

* 库不存在时 `sqlite3.connect()` **会建文件** → 搜一次空库，得到"什么都没搜到"外加一个
  刚被创建出来的空库（它会让下一次的一切都以为"索引建好了"）；
* 显式 `--mode semantic` 被悄悄换成词法 → 用户拿到一堆**看起来合理**的关键词命中，
  没有任何办法察觉；
* 配置默认 `hybrid` 被降级却**不说** → 与上一条相反方向的错，同样看不出来；
* 把"整串都是标点"当成"没搜到" → 用户会以为工具丢了他的查询；
* 声明了却忽略的过滤参数（`--ext` / `--under` / `--after` / `--json`）→ `--ext pdf`
  被忽略的结果是"搜出来的还是全格式"，看起来完全正常。

`search` 的**词法与降级判据**本身（`search/tier.py`）在 `test_tier_state.py` 里有测试，
这里只管命令面：接线、退出码、输出里有没有那句话。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localsearch.cli.main import main
from localsearch.core.config import ENV_CONFIG_PATH
from localsearch.core.logging import reset_logging

#: 正文里的词。**选它是因为它在 jieba 里切成两个词（成本 / 核算）而查询侧也一样**
#: （`test_store_body_fts.py` 里用的就是这个组合）：正文那条路的切法不匹配的症状是
#: "这个词明明在文件里，怎么都搜不到"，而它不报错。
BODY_WORD = "成本核算"

#: 文件名与路径里的词。纯 ASCII，用来钉 Tier0 那一段（`files_fts.path_tok` 存整条路径）。
NAME_WORD = "report"


@pytest.fixture(autouse=True)
def clean_logging():
    """每个测试前后把 CLI 装到根 logger 上的 handler 摘干净（同 `test_cli.py`）。"""
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """一棵小树 + 一份配置，全部落在 `tmp_path` 里（**不碰仓库根的 `.lsearchignore`**：
    `resolve_ignore_file()` 会把相对名字解析到那里，那样结果就取决于仓库里那份文件）。

    树里的每一样东西都对应一条断言：三个文件名里带 `report`（纯 ASCII，钉 `--limit` 的
    "第 N 条被砍掉"）、`年度报告.txt`（**中文名**，钉"按文件名搜得到"这条在中文上的形态：
    `files_fts` 那一列的切法见 `index/tokens.py` 的 `E38`）、其中一个文件的正文里有
    `成本核算`（钉 Tier1）。

    ⚠ **中文名那一个不能省**：只有 ASCII 名字时，"按文件名"那段对中文查询永远是 0 条
    （路径里没有汉字），而 0 条与"这一级没做"在输出上长得一样——这一版最早就是靠它才发现
    自己把一条断言写反了。

    每份正文都刻意**不含**别的文件的查询词：`报告` 只有一个文件名里有、只有一份正文里
    有，两段各自只该出现一行——否则"两段混没混"就断言不出来了。
    """
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "report-2024.txt").write_text(f"年度报告 {BODY_WORD} 明细", encoding="utf-8")
    (tree / "sub" / "report-2025.txt").write_text("另一年的存档", encoding="utf-8")
    (tree / "report-2026.txt").write_text("第三份存档", encoding="utf-8")
    (tree / "年度报告.txt").write_text("存档正文", encoding="utf-8")
    (tree / "notes.txt").write_text("与报表无关的笔记", encoding="utf-8")

    ignore = tmp_path / "ignore.txt"
    ignore.write_text("*.never\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'roots = ["{tree.as_posix()}"]\n'
        "[scan]\n"
        f'ignore_file = "{ignore.as_posix()}"\n'
        # 默认关掉语义那一级：这里钉的是词法那一面的命令接线。若不关，默认 `index` 会去加载
        # 14 GB 的嵌入模型，而"这台机器上有没有那份缓存"就成了测试结果的一部分。显式
        # `--mode semantic` 的拒绝路径有测试；**成功的语义检索路径目前没有命令面测试**（P5 遗留）。
        "[tiers]\n"
        "semantic = false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("COLUMNS", "300")
    return {"cfg": cfg, "data_dir": data_dir, "tree": tree, "ignore": ignore}


def cli(sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], *argv: str):
    """跑一条命令，返回 `(退出码, stdout, stderr)`。"""
    code = main(["--config", str(sandbox["cfg"]), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def flat(text: str) -> str:
    return " ".join(text.split())


def indexed(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str], *index_argv: str
) -> None:
    """先把索引跑好（这一步自己的账由 `test_index_resume.py` 钉），再往下测检索。"""
    code, out, err = cli(sandbox, capsys, "index", *index_argv)
    assert code == 0, f"这一趟索引没跑成，后面的断言没有意义：\n{flat(out + err)}"


def section(out: str, title: str) -> str:
    """输出里从 `title` 那一段到下一段之前的文本（段与段之间靠空行分）。"""
    parts = out.split("\n\n")
    for part in parts:
        if title in part:
            return part
    raise AssertionError(f"输出里没有 {title!r} 这一段：\n{out}")


# ==============================================================================
# 1. 库不存在：先看再接
# ==============================================================================


def test_a_missing_database_is_refused_and_leaves_no_trace(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    r"""没有索引库时**拒绝**（退出码 2）、给出下一条命令、**不建任何东西**。

    ⚠ 这一条的重点是**不建**：`sqlite3.connect()` 会创建文件，所以少了
    `config.db_path.exists()` 那一句，用户得到的是"什么都没搜到"外加一个刚被创建出来的
    空库——而那个空库会让之后的一切都以为"索引建好了"（`data_dir` 也跟着建出来了）。
    """
    code, _out, err = cli(sandbox, capsys, "search", "任意词")
    text = flat(err)
    assert code == 2 and "还没有索引库" in text
    assert "lsearch index" in text, "拒绝之后要给出下一条能敲的命令"
    assert not sandbox["data_dir"].exists(), "只读的检索命令不该留下任何落点（A7）"


# ==============================================================================
# 2. 模式：显式要语义 → 拒绝；配置默认成混合 → 降级并标注
# ==============================================================================


def test_an_explicit_semantic_mode_is_refused(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--mode semantic`：**拒绝**（退出码 2），并点名 `P5`。

    不悄悄换成词法的理由见模块文档：一个人明确要了语义检索，词法给他的是一堆看起来
    合理的**关键词**命中，他没有办法察觉自己拿到的是另一种东西。这正是 `A8` 里
    "不允许静默返回不完整结果"的极端形态——不是结果不完整，是结果答的是另一个问题。
    """
    indexed(sandbox, capsys)
    code, out, err = cli(sandbox, capsys, "search", "报告", "--mode", "semantic")
    text = flat(out + " " + err)
    # `indexed()` 只建到 Tier1：向量一条都没有，所以是"缺数据"（`ModeNotReady`），
    # 而不是"缺代码"——下一步是去建索引，不是等 P5。
    assert code == 2 and "lsearch index --tier 2" in text and "0 / 5" in text, text
    assert "按文件名" not in text, "拒绝的那条路不该打印任何检索结果"
    assert "Traceback" not in text, "拒绝必须是用法错误，不能是未捕获的异常"


def test_an_explicit_hybrid_mode_is_refused_too(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--mode hybrid` 同样是**人说的**，同样拒绝——判据是 `explicit`，不是"混合有没有
    实现"。缺的是 RRF（`F5-05`），不是 `P5` 整体：那句话要点名这个，而不是含糊地说"还没做"。
    `F5-05` 落地后这条测试要按「绿灯不自动续期」重写。"""
    indexed(sandbox, capsys)
    code, _out, err = cli(sandbox, capsys, "search", "报告", "--mode", "hybrid")
    text = flat(err)
    assert code == 2 and "F5-05" in text and "RRF" in text, text
    assert "Traceback" not in text


def test_the_config_default_hybrid_is_downgraded_with_a_note(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """配置里的 `mode = "hybrid"`（模板默认值）→ **降级到词法并说出来**。

    与上一条**相反**的处理，判据是"这句话是不是人说的"：默认值表达的是一份偏好，
    不降级的话 `lsearch search 关键词` 开箱就坏。而**不说**的话，用户拿到的是一份
    看起来完整的词法结果，他不知道混合检索里的语义那一半从没跑过。

    断言两件事：那句逐字含「语义未就绪」的话在，以及它接着说清了"实际按什么检索"。
    显式写 `--mode lexical` 是**要**词法，所以那一边不该有任何折价的提示——两半都钉。
    """
    indexed(sandbox, capsys)
    code, out, _err = cli(sandbox, capsys, "search", "报告")
    text = flat(out)
    assert code == 0
    assert "语义未就绪" in text, "降级必须标注（D10 / A8）"
    assert "混合检索" in text, "要说的不只是'未就绪'，还有'我实际按什么检索'"
    assert "检索模式" in text and "要的是 hybrid" in text

    code, out, _err = cli(sandbox, capsys, "search", "报告", "--mode", "lexical")
    text = flat(out)
    assert code == 0
    assert "语义未就绪" not in text, "人要的就是词法，这里没有打折可言"
    assert "要的是" not in text, "没有降级就不该印'要的是 X'"


# ==============================================================================
# 3. 两段结果各印各的
# ==============================================================================


def test_path_hits_and_body_hits_are_printed_separately(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Tier0 与 Tier1 的命中**分开两段**，各自按自己的 BM25 排。

    `P4` 刻意不做融合（RRF 要语义那一路才有得融）：一个"看起来像融合过"的排序是这一版
    最不该给的东西——用户没有办法从输出里看出它没融。所以两段的标题必须都在。

    这里用中文查询 `报告` 同时打两段：中文名文件 `年度报告.txt` 落在文件名那一段，
    `report-2024.txt` 的**正文**"年度报告 …"落在正文那一段（同一个词、两条路）。
    """
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", "报告")
    assert code == 0
    paths = section(out, "按文件名/路径")
    body = section(out, "按正文")
    # 文件名那一段里有路径（Tier0 的全部价值就是定位到文件）
    assert "年度报告.txt" in flat(paths), flat(paths)
    # ⚠ 正文那一段**不能**混进文件名命中：混了就等于偷偷做了融合排序。
    assert "年度报告.txt" not in flat(body), flat(body)
    # 正文那一段里有片段原文
    assert BODY_WORD in flat(body)


def test_a_body_hit_is_grouped_by_content_not_by_chunk(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """正文命中按 **blob** 分堆，一堆只印一个例子 + 一句"命中 N 片"。

    一份长文档里同一个词出现十次是常态，而用户要的是"哪几个文件里有这个词"。逐片印出来
    会把终端刷满，且第 2 到第 10 行是同一条信息的重复。这里用一份把 `BODY_WORD` 写很多
    遍的文件来撑出多片同 blob 的命中。
    """
    tree = sandbox["tree"]
    (tree / "multi.txt").write_text((f"{BODY_WORD} " * 40).strip(), encoding="utf-8")
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", BODY_WORD)
    assert code == 0
    body = section(out, "按正文")
    assert "multi.txt" in flat(body)
    assert "命中" in flat(body) and "片" in flat(body)


def test_a_query_with_no_searchable_token_says_so(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """整串都是标点时：**"这个查询里没有可以搜的词"**，而不是"没搜到"。

    两件事的下一步完全不同——前者要用户改查询，后者要他改索引。只回一个空列表的话
    调用方分不出来（`PathSearch.has_query` 存在的理由）。

    ⚠ 这句话**不能**写成"单个汉字会被丢掉"：`query_tokens()` 里那条"全是单字时退回丢
    之前的列表"是刻意的（用户真的在搜"书"，丢掉它等于什么都不搜），写错会让人以为
    工具丢了他的查询。
    """
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", "。。。")
    text = flat(out)
    assert code == 0
    assert "没有可以搜的词" in text and "标点" in text
    assert "按文件名" not in text, "没有可搜的 token 时根本没查，不该印两段空结果"

    # 单字是**正常查询**（它走"没搜到"那条路，不是"没有可以搜的词"）
    code, out, _ = cli(sandbox, capsys, "search", "书")
    text = flat(out)
    assert code == 0
    assert "没有可以搜的词" not in text


def test_no_hits_is_still_a_success(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """"搜到了 0 条"是**正常结果**，退出码 0。

    给它一个非零退出码会让脚本里的 `lsearch search x || 处理` 在"盘上真没有这个词"时
    走错分支——而那是最常见的一种正常结果。
    """
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", "完全没有出现过的词组xyz")
    assert code == 0
    assert "一条都没有" in flat(out)


def test_limit_caps_each_section(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--limit` 同时管两段的上限（它们各自问库一次，`top_k` 一路传下去）。

    不生效的症状很安静：`--limit 2` 给了、输出还是 20 条，而用户以为自己限住了。

    ⚠ 断言**只数行数、不点名哪个文件被砍掉**：`LIMIT` 是挂在 `ORDER BY bm25` 后面的，
    被砍掉的是**相关性最低**的那一条（这里恰好是 `sub/` 底下那个），不是名字最大的那个。
    点名一个文件名等于把排序也钉进来——那是另一件事（`test_store_search.py` 的账），
    而且它会随 bm25 的实现或数据变化而失效。
    """
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", NAME_WORD, "--limit", "2")
    assert code == 0
    paths = section(out, "按文件名/路径")
    rows = [line for line in paths.splitlines() if line.startswith("  ") and line.strip()]
    assert len(rows) == 2, paths
    assert "2 条" in flat(paths), "标题里的条数就是返回的行数（同一份 `hits`）"


# ==============================================================================
# 4. 分级就绪：Tier2 没做完这件事要能看见
# ==============================================================================


def test_a_tier0_only_index_still_answers_by_name_and_says_body_is_not_ready(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--tier 0` 之后：**按文件名还能搜到**，而正文那一段必须自己说"未就绪"。

    这是 `A8` 的**主场景**（不是取消词法检索，而是让"这一级还没做"这件事可见）：
    正文一片都没进库的时候，`search_chunks()` 返回空列表，而那与"跑完了、但这个词没
    出现过"在数据上**完全一样**。所以那句话说出口的责任在这一层，不在 store。

    这里同时钉住了这两条的**区别**：正文那一段印的是"一条都没有"（那就是"没搜到"的
    形态），而标注那行说"这一级还没做"。少了标注，用户看到的就只是前者。
    """
    indexed(sandbox, capsys, "--tier", "0")
    code, out, _ = cli(sandbox, capsys, "search", "报告")
    text = flat(out)
    assert code == 0
    # 文件名照样搜得到（Tier0 做完就能用，这是 P4 的阶段目标）
    assert "年度报告.txt" in flat(section(out, "按文件名/路径"))
    # 但正文那一段要标注
    assert "正文未就绪" in text, text
    assert "0 / " in text, "要说'差多少'，`0 / 5` 比一句'未就绪'有用得多"
    assert "一条都没有" in flat(section(out, "按正文")), "正文那一段是空的，而这句话单看是看不出来的"


def test_tier2_is_not_complained_about_when_the_config_turns_it_off(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """配置里 `semantic = false`：**不再提 Tier2**。

    对一个永远不会做的级别一直报"未就绪"，是一句**永远为真的废话**——而永远为真的警告
    会被学会忽略，连带着真警告一起（`search/tier.py` 的模块文档把这条写成了反面）。

    `sandbox` 的配置本来就是 `semantic = false`，所以这里不再追加一行，只做断言。
    """
    indexed(sandbox, capsys)
    code, out, _ = cli(sandbox, capsys, "search", "报告", "--mode", "lexical")
    text = flat(out)
    assert code == 0
    assert "语义未就绪" not in text
    # 台账那一行只印配置允许到的那几级（Tier2 关掉了，就不该出现）
    readiness = [
        line for line in out.splitlines() if "分级就绪" in line
    ]
    assert readiness, out
    assert "语义" not in readiness[0], readiness[0]


# ==============================================================================
# 5. 只声明真能兑现的参数
# ==============================================================================


def test_filter_options_are_not_declared_rather_than_silently_ignored(
    sandbox: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """计划 §4.6 给 `search` 列的 `--ext` / `--under` / `--after` / `--json`：**一个都
    没声明**，Click 报 `No such option`（退出码 2）。

    `search_paths()` / `search_chunks()` 都不收过滤参数，而"声明了却忽略"正是这个项目
    反复吃亏的那一类错：`--ext pdf` 被忽略的结果是"搜出来的还是全格式"，看起来完全正常。
    """
    indexed(sandbox, capsys)
    for argv in (
        ("search", "报告", "--ext", "pdf"),
        ("search", "报告", "--under", "C:/x"),
        ("search", "报告", "--after", "2026-01-01"),
        ("search", "报告", "--json"),
    ):
        code, out, err = cli(sandbox, capsys, *argv)
        assert code == 2, argv
        assert "No such option" in flat(out + " " + err), argv
