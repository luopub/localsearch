r"""把"这条测试真的盯着那处代码"从一句话变成一条可复跑的结论。

------------------------------------------------------------------------------
为什么要有这个工具
------------------------------------------------------------------------------

`CLAUDE.md` 写着"每个功能点必须有自动化测试"，而这条规矩的落点在"红了的才是
护栏"——**一条永远绿的测试等于没有测试**。P3 到现在为止，验这件事的办法是
"把旧写法临时换回代码里，看它红不红"，而那是**一次性内联脚本**：按 §3 的
规矩（`E12`）属于**最弱一档证据**，因为别人没法复跑、连当时改的是哪一行都
只能靠叙述。

`F3-02` 的「偏差」第 6 条与 `F3-03` 的记录都是这么做的，两次都靠它抓到了真
问题（xlsx 那条截断测试**换不换对象都绿**）。所以这里把它做成工具：**改动的是
被测源码、跑的是真 pytest、结论是"哪些测试红了"**——而不是我自己说它红了。

------------------------------------------------------------------------------
它做什么
------------------------------------------------------------------------------

对 `MUTATIONS` 里的每一条（按 `--only <id>` 可以只跑一组）：

1. **要求那处改动在源码里出现且只出现一次**（`old` 在文件里的匹配数必须是 1）。
   匹配 0 次说明这句代码已经被改过形（这条变体**过期了**），匹配多次说明它
   改不准——两种都必须报错退出，不许"尽力而为"。
2. **点名的测试必须真的叫这个名字**（扫一遍 `def`）。2026-10-16 实测：新写的
   `console` 变体把 `expect` 写成 `test_cli_entry_calls_the_hatch`（真名多一个
   `the_`），报告说的是**「这里没有护栏」**，而真相是名字写错了。这两种情形在
   红名单上看不出区别——红名单里躺着的正是那条测试，只有判定匹配不上。
3. **要求目标文件在 git 上是干净的**——判**两次**：一次 `git status`，一次拿
   工作区的字节跟索引里那份比。这个工具会把工作区的源码改坏几秒钟，要是文件
   本来就有未提交的改动，恢复之后分不清哪些是我的，所以**宁可拒绝跑**；而
   `git status` 单独用会漏（理由见 `_check_preconditions`）。
4. 改坏 → 跑 `pytest`（只跑这一组点名的测试）→ **还原**。
5. 判定：**必须红**，而且**红的必须包括点名的那条测试**、pytest 的退出码必须是
   `1`（测试失败）。退出码 `2` 是收集期出错——那种情况下"红了"是假的：
   整个文件没导进来，所有测试一起红，看着像护栏其实什么都没验。

**任何一条没红，退出码就是 1**——它是这套东西的存在意义。

退出时**逐字节比对**还原结果与原文，不一致就报错（那种情况下工作区已经被弄脏，
得 `git checkout` 回来）。

⚠ **行尾是这一层的雷**（2026-10-16 踩到，工具自己的 bug）：改写源码用的是
`pathlib.write_text`，它的 `newline=None` 会把 `\n` 按**平台**落成换行——在
Windows 上就是把目标文件整篇换成 CRLF。于是"改坏→还原"这一趟下来内容一个字
没变、**行尾变了**，工作区平白变脏，下一次调用被自己的前置检查拦在门外。
现在写的是**字节**：改的时候用文件自己的行尾，还原的时候把它原来的字节原样
写回。

------------------------------------------------------------------------------
一句话的诚实交代
------------------------------------------------------------------------------

它验的是"**这处代码被改坏时，这条测试会红**"，**不是**"这处代码是对的"。变体
是**双向**的：把 `if direction in ("S", "W")` 改成 `if False` 证明南纬那条测试
盯着它；反过来，要是有人把真正的逻辑写错、而测试也照着错的写，这里一条都抓
不到。**它补的是"测试不是摆设"这一半，不是"实现是对的"那一半。**

------------------------------------------------------------------------------
用法
------------------------------------------------------------------------------

    uv run python tools/check_mutations.py
    uv run python tools/check_mutations.py --only f3-04
    uv run python tools/check_mutations.py --list

**不需要设 `PYTHONIOENCODING`**：输出面上的多字节兜底在 `core/console.py`
（`use_safe_output()`，本文件的 `main()` 第一行调用）——编不出的字符降级成 `?`，
不再让退出码无端变 1（`E16` / `E37`）。

⚠ **跑的时候本机在跑 pytest，占用率会上去**。它量的是"红不红"、不是耗时，
所以不怕噪声——但也别拿它的运行时间当什么基准。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

from localsearch.core.console import use_safe_output

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 变体表。每一条是"把一处代码改坏，点名的那条测试必须红"。
#:
#: 字段：`id`（组名）、`label`（这条变体在验什么，一句话）、`file`（相对仓库根）、
#: `old` / `new`（必须是能在文件里**唯一**匹配的一段）、`tests`（点名的测试），
#: `why`（这条变体对应哪个决定——**没有理由的变体不许留在表里**）。
MUTATIONS: list[dict[str, object]] = [
    {
        "id": "f3-04",
        "label": "解压炸弹不再翻译成 ParseFailed",
        "file": "src/localsearch/extract/media_meta.py",
        "old": "            except Image.DecompressionBombError as exc:",
        "new": "            except Image.UnidentifiedImageError as exc:",
        "tests": ["tests/unit/test_extract_media_meta.py"],
        "expect": "test_a_decompression_bomb_is_a_failure_not_a_crash",
        "why": "DecompressionBombError 不是 OSError（Pillow 里直接继承 Exception），"
               "少接这一族它就会以未捕获异常冒出批量层——`A6` 的反面",
    },
    {
        "id": "f3-04",
        "label": "分数不再化成小数（退回 str()）",
        "file": "src/localsearch/extract/media_meta.py",
        "old": '    return f"{value:.6f}".rstrip("0").rstrip(".")',
        "new": "    return str(value)",
        "tests": ["tests/unit/test_extract_media_meta.py"],
        "expect": "test_long_decimals_are_rounded_to_six_places",
        "why": "⚠ 这条变体**一开始没有测试抓得住**：28/10、1/250、425/100 这几个样本上，"
               "Pillow 的 IFDRational.__str__ 给出的字面量本来就和我们算的一样，"
               "所以整句换成 str() 之后 113 条一条都不红（2026-10-16 实测）。"
               "补了 1/3 与 1234/7（浮点展开 16 位）才闭上——这条变体就是那个补丁的护栏",
    },
    {
        "id": "f3-04",
        "label": "南纬西经不再取负",
        "file": "src/localsearch/extract/media_meta.py",
        "old": '    if direction in ("S", "W"):\n        degrees = -degrees',
        "new": "    if False:\n        degrees = -degrees",
        "tests": ["tests/unit/test_extract_media_meta.py"],
        "expect": "test_southern_and_western_hemispheres_go_negative",
        "why": "方向字母并进值里是这一层的设计；符号错了会让南半球的照片定位到北半球，"
               "而正文里那串数字看起来完全正常",
    },
    {
        "id": "f3-04",
        "label": "XP* 不再从字节里解文字",
        "file": "src/localsearch/extract/media_meta.py",
        "old": "    if name in _XP_TAGS:",
        "new": "    if False:",
        "tests": ["tests/unit/test_extract_media_meta.py"],
        "expect": "test_xp_title_is_decoded_from_utf16_bytes",
        "why": "XP* 是中文照片里唯一可能带着中文的地方（Make/Model 是英文的）；"
               "这条分支一没，中文标题就静默消失",
    },
    {
        "id": "f3-04",
        "label": "正文为空时不再出声",
        "file": "src/localsearch/extract/media_meta.py",
        "old": "        if not cap.has_body:\n            notes.append(",
        "new": "        if False:\n            notes.append(",
        "tests": ["tests/unit/test_extract_media_meta.py"],
        "expect": "test_a_bare_png_is_an_empty_doc_with_a_warning",
        "why": "一张没有元数据的图与一张元数据没被我们的名单覆盖的图，在库里长得一样；"
               "不出声的话「搜不到」就没有线索",
    },
    {
        "id": "f3-05",
        "label": "末尾不再收尾（切出一串互相包含的碎片）",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "        if end == limit:",
        "new": "        if False:",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_the_tail_is_not_sliced_into_nested_pieces",
        "why": "规则 7。没有它时每篇正文的末尾都会多出几片**互相包含**的碎片"
               "（512 -> 76 -> 11 -> 1），内容一个字不多、却各占一个向量与一行 FTS；"
               "而它们看起来完全正常，只是多花几倍的钱",
    },
    {
        "id": "f3-05",
        "label": "相邻片不再重叠",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "        start = max(end - int(overlap_ratio * (end - start)), start + 1)",
        "new": "        start = end",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_the_overlap_is_the_stated_fraction_of_the_char_span",
        "why": "`overlap_ratio` 是配置里的一个数，切在被句子中间的那个词落在两片之间时，"
               "没有重叠就意味着它两边都搜不回来——而片数、片长、一切报表都正常",
    },
    {
        "id": "f3-05",
        "label": "D8 的截断不再生效",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "    truncated = total > max_chunks_per_doc",
        "new": "    truncated = False",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_one_over_the_limit_truncates_and_adds_the_summary",
        "why": "`D8` 的 200 片上限挡的是 `R12`（一个 5 万行的表格造出上万片、吃掉"
               "数小时索引时间）；判定一没，`R12` 就直接回来了",
    },
    {
        "id": "f3-05",
        "label": "让出的名额差一位（上限变成 +1 片）",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "        del windows[max_chunks_per_doc - 1 :]",
        "new": "        del windows[max_chunks_per_doc - 2 :]",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_one_over_the_limit_truncates_and_adds_the_summary",
        "why": "占位片**也占名额**：`D8` 说的是每文档 200 片，不是「200 片正文 + 1 片"
               "说明」。差一位时总数还是 200 上下，报表看不出来",
    },
    {
        "id": "f3-05",
        "label": "窗口的边界从「装得下」变成「差一点装得下」",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "            if count_tokens(text[start:middle]) <= target_tokens:",
        "new": "            if count_tokens(text[start:middle]) < target_tokens:",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_a_window_is_the_largest_one_that_fits",
        "why": "规则 1 要的是**最大**的 `end`。差一个 token 看着无所谓，但它让"
               "「边界是正文的函数」这条确定性变成了「搜索路径的函数」——同一个 blob"
               "重跑时边界可能就变了，`embed_cache` 跟着全废（`A9`）",
    },
    {
        "id": "f3-05",
        "label": "整片空白的窗口不再跳过",
        "file": "src/localsearch/chunk/sliding.py",
        "old": "        if right > left:",
        "new": "        if True:",
        "tests": ["tests/unit/test_chunk_sliding.py"],
        "expect": "test_no_chunk_is_empty_or_all_whitespace",
        "why": "规则 4。空片会进 FTS、进向量：白占一行，还带回一个恒不命中的向量"
               "（它连「最不像」都算不上，就是没有内容）",
    },
    {
        "id": "f3-07",
        "label": "遇到失败就停下整轮（`A6` 的字面反面）",
        "file": "src/localsearch/extract/batch.py",
        "old": "            yield Outcome(path, error=exc)",
        "new": "            return",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_one_outcome_per_path_in_input_order",
        "why": "`A6` 就是这一行在守。停下时症状最难查的一种是**没有任何报错**："
               "这一轮“跑完了”、退出码 0、报表干净，而它后面那些文件一个都没处理"
               "（`R4` 那条风险的形态：权限拒绝 / 长路径 / 符号链接环上必然会踩到）",
    },
    {
        "id": "f3-07",
        "label": "第二道防线收窄成只接 `ExtractError`",
        "file": "src/localsearch/extract/batch.py",
        "old": "        except Exception as exc:  # noqa: BLE001 - 第二道防线，见模块文档",
        "new": "        except ExtractError as exc:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_an_unexpected_exception_is_caught_and_marked",
        "why": "看着更“精确”的写法。`F3-02` / `F3-03` / `F3-04` 每一轮都踩到过"
               "“异常族名单里少列一个”（库升级就会改它），所以判据不回头依赖提取器；"
               "漏一个的代价不是少收一个文件，是整轮索引白跑",
    },
    {
        "id": "f3-07",
        "label": "连 `BaseException` 也接（Ctrl-C 被吃掉）",
        "file": "src/localsearch/extract/batch.py",
        "old": "        except Exception as exc:  # noqa: BLE001 - 第二道防线，见模块文档",
        "new": "        except BaseException as exc:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_ctrl_c_is_not_swallowed",
        "why": "更“彻底”的写法。接住 `KeyboardInterrupt` 的症状是“按了 Ctrl-C 没反应、"
               "而且一点提示都没有”——全盘跑起来时那是没有逃生口的",
    },
    {
        "id": "f3-07",
        "label": "把“这一类不做”也写进 `error_msg`",
        "file": "src/localsearch/extract/batch.py",
        "old": "        if self.error is None or isinstance(self.error, UnsupportedFormat):",
        "new": "        if self.error is None:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_unsupported_formats_stay_out_of_the_failure_report",
        "why": "`error_msg` 非空是“失败报表”的唯一判据，而全盘上 `.db` / `.dat` / `.exe` "
               "多得是。填满之后真坏的文件淹在噪音里——正是 `base.UnsupportedFormat` "
               "那份表格里写明不要做的事",
    },
    {
        "id": "f3-07",
        "label": "`UnsupportedFormat` 降级成 `error`",
        "file": "src/localsearch/extract/batch.py",
        "old": "        if isinstance(self.error, UnsupportedFormat):\n            return STATUS_SKIPPED",
        "new": "        if False:\n            return STATUS_SKIPPED",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_each_kind_of_failure_comes_back_as_a_reason",
        "why": "同上一条的另一半：这一格决定 `P4` 把它写进 `files` 的**哪一档**。"
               "记成 `error` 之后，报表里的噪音与上一条一样多，但连“文件名照搜”这层"
               "说明也没有了",
    },
    {
        "id": "f3-07",
        "label": "`error_msg` 不再折成一行",
        "file": "src/localsearch/extract/batch.py",
        "old": '    flat = " ".join(text.split())',
        "new": "    flat = text",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_error_msg_is_one_short_line_with_the_class_name",
        "why": "这一格会进表格、进日志行，而库报的原文里常有换行（PDF / OOXML 的"
               "解析错误尤其）。带换行的一句能把一整屏输出搅乱，排错的人反而看不到重点",
    },
    {
        "id": "f3-07",
        "label": "`error_msg` 不再截断",
        "file": "src/localsearch/extract/batch.py",
        "old": "    if len(flat) <= ERROR_MSG_MAX:",
        "new": "    if True:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_error_msg_is_one_short_line_with_the_class_name",
        "why": "上限与 `base.meta_str` 同一个理由（那一行是给人看的），只多一个 `...` "
               "标记。不截断时一行 `error_msg` 能是几 KB，`status` / 报表按行排版就全乱了",
    },
    {
        "id": "f3-07",
        "label": "未预期的异常不再单列计数",
        "file": "src/localsearch/extract/batch.py",
        "old": "        if outcome.unexpected is not None:",
        "new": "        if False:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_an_unexpected_exception_is_caught_and_marked",
        "why": "`unexpected` 是那三个提取器模块“异常族名单漏了一个”的**唯一侦测器**。"
               "混进“文件坏了”里就等于没有：报表上两者长得一样，于是漏掉的那一族"
               "永远不会有人去补",
    },
    {
        "id": "f3-07",
        "label": "`Outcome` 的不变量不再把关",
        "file": "src/localsearch/extract/batch.py",
        "old": "        if sum(given) != 1:",
        "new": "        if False:",
        "tests": ["tests/unit/test_extract_failure_isolation.py"],
        "expect": "test_outcome_refuses_anything_but_exactly_one_of_the_three",
        "why": "“一个都没有”的 `Outcome` 流到 `P4` 会写成一行**没有原因的失败**——"
               "那种记录最难查，因为每一层都觉得自己没错（`base.py` 里"
               "“注册了但返回空正文”是同一个坑的另一种形态）",
    },
    {
        "id": "f3-06",
        "label": "`--check` 不再报“盘上有、清单里没有”的文件",
        "file": "tools/make_fixtures.py",
        "old": "    for extra in stray(samples):",
        "new": "    for extra in []:",
        "tests": ["tests/unit/test_fixtures.py"],
        "expect": "test_a_stray_file_in_the_directory_is_reported",
        "why": "自检只查“清单里的都在盘上”的话，把一个改坏了的旧文件留在目录里就能让"
               "它保持绿——那正是两份真相。反之，这一半也是“手改过目录”唯一会被"
               "抓到的地方（清单里那条改了名，它就成了多余的那一个）",
    },
    {
        "id": "f3-06",
        "label": "第一次生成的文件被说成“同长不一致”而不是“新建”",
        "file": "tools/make_fixtures.py",
        "old": "        note = f\"新建（{len(data)} 字节，{_digest(data)}）\" if before is None else _same(before, data)",
        "new": "        note = _same(before or b\"\", data)",
        "tests": ["tests/unit/test_fixtures.py"],
        "expect": "test_write_says_what_it_newly_made",
        "why": "这条消息印给人看，而 0 → N 字节的“不一致”读起来像“这文件被人改小了”，"
               "实际是“第一次生成”。判据是同一件事（比字节），说错方向会把人带偏",
    },
    {
        "id": "f3-06",
        "label": "清单里 `broken.pdf` 的 `raises` 写成别的异常族",
        "file": "tests/fixtures/MANIFEST.toml",
        "old": "builder = \"broken_pdf\"\nkind = \"broken\"\nextractor = \"office\"\nraises = \"ParseFailed\"",
        "new": "builder = \"broken_pdf\"\nkind = \"broken\"\nextractor = \"office\"\nraises = \"FileUnreadable\"",
        "tests": ["tests/unit/test_fixtures.py"],
        "expect": "test_a_broken_sample_raises_the_declared_error",
        "why": "“坏文件按名报错”是这份样本集的用处所在：`raises` 写错一族就等于把"
               "这一条样本的判据换成了另一件事。⚠ 它同时也是清单**自己**被验的证明"
               "——清单不是文档，是这份样本集的真相来源，写错必须红",
    },
    {
        "id": "f3-06",
        "label": "五份图片样本共用的那个 EXIF 字段改了名",
        "file": "tools/make_fixtures.py",
        "old": "PHOTO_FIELD = \"fixture photo\"",
        "new": "PHOTO_FIELD = \"fixture photos\"",
        "tests": ["tests/unit/test_fixtures.py"],
        "expect": "test_the_bytes_on_disk_are_what_the_builder_makes_now",
        "why": "builder 改了而没重跑生成器，是 fixtures 这一类“文件本身就是被测物”"
               "最容易出的错：改了 builder 的人手上是绿的（他跑的是新字节），"
               "clone 出来的人拿到的是旧文件。这一条把“提交的字节 = 生成器现在的字节”"
               "钉死（`--check` 是同一个判据的命令行出口）",
    },
    {
        "id": "console",
        "label": "CLI 入口不再给输出面兜底",
        "file": "src/localsearch/cli/main.py",
        "old": "    use_safe_output()\n    args = list(",
        "new": "    args = list(",
        "tests": ["tests/unit/test_console_output.py"],
        "expect": "test_the_cli_entry_calls_the_hatch",
        "why": "这一条盯的是**静态**那一半（入口里有没有那句调用）。兜底一旦从入口"
               "消失，`lsearch index --help > x.txt` 就会重新崩在重定向上——但静态"
               "检查先红，因为它跑的是一条 AST，比真跑一个进程便宜得多",
    },
    {
        "id": "console",
        "label": "CLI 入口不再给输出面兜底（真跑一条子进程看退出码）",
        "file": "src/localsearch/cli/main.py",
        "old": "    use_safe_output()\n    args = list(",
        "new": "    args = list(",
        "tests": ["tests/unit/test_console_output.py"],
        "expect": "test_cli_help_survives_a_gbk_pipe",
        "why": "同一次改动、另一条测试在盯：它起真进程、管道、GBK 编码跑 `lsearch "
               "index --help`。这一条才是用户那一档（把帮助重定向到文件）——静态"
               "那条只证明「有调用」，这条证明「调用真的挡住了崩」",
    },
    {
        "id": "console",
        "label": "兜底自己把 `errors` 设成 strict（等于不降级）",
        "file": "src/localsearch/core/console.py",
        "old": '            reconfigure(errors="replace")',
        "new": '            reconfigure(errors="strict")',
        "tests": ["tests/unit/test_console_output.py"],
        "expect": "test_the_hatch_degrades_instead_of_raising",
        "why": "`errors` 是这一层的**全部**：改成 strict 之后 `use_safe_output()` "
               "照样能调、照样返回，只有编不出的那一刻才炸——一条「装了兜底就不崩」"
               "的测试是唯一抓得住它的东西",
    },
    {
        "id": "console",
        "label": "`check_mutations.py` 自己不再兜底",
        "file": "tools/check_mutations.py",
        "old": "    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉\n"
               "    use_safe_output()\n",
        "new": "",
        "tests": ["tests/unit/test_console_output.py"],
        "expect": "test_every_tool_with_a_main_calls_the_hatch",
        "why": "静态那一半按**文件名**参数化，17 个工具各一条；它抓的是「新写的脚本"
               "又漏一次」这个形态——`check_mutations.py` 自己就是这么漏的（它全部"
               "变体都抓到的那次跑法，退出码是 1）",
    },
    {
        "id": "console",
        "label": "`scan_report.py` 不再兜底（真跑一次非子进程）",
        "file": "tools/scan_report.py",
        "old": "    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉\n"
               "    use_safe_output()\n",
        "new": "",
        "tests": ["tests/unit/test_console_output.py"],
        "expect": "test_the_tools_survive_a_gbk_pipe",
        "why": "这条走的是**真子进程**那一半：`tools/scan_report.py --help` 会打印"
               "模块文档串，里面有 `⚠`，在 GBK 管道下退出码 1（2026-10-16 实测；"
               "`0dcb708` 当年只修了它的账目行，够不着 `--help`）",
    },
    # ------------------------------------------------------------------ f4-01
    # Tier0 那一层（`index/` 三个新文件）的九条。⚠ 其中一条改的是 **`schema.sql`**
    # ——它是包数据、也是"那条刻意的差异"唯一的落点，SQL 里没有别的东西会喊。
    {
        "id": "f4-01",
        "label": "名/路径这一层不再加 2-gram",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "FILES_BIGRAMS = True",
        "new": "FILES_BIGRAMS = False",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_search_reaches_across_a_word_seam",
        "why": "`E38` 量出来的那一层：`财务报表` 在 `cut_for_search` 下给出 `财务` / "
               "`报表` / `财务报表`，**没有 `务报`**（跨词缝），微召回 75.3% → 98.8% "
               "全靠它。少了它不报错，只是搜「落在词缝上的两个字」时静默回 0 条——"
               "所以这条测试是故意钉住「两种切法不许顺手统一」。"
               "⚠ **这一条的第一版只关掉了 `row.name` 那一列，它活下来了**"
               "（9 条里 2 条没红，2026-10-16 实测）：端到端那条路上的查询命中的是 "
               "`path_tok`，而路径**包含**文件名——同一份 token 存了两列，一列坏了"
               "另一列替它兜住。所以这条改的是那个常量本身",
    },
    {
        "id": "f4-01",
        "label": "查询侧不再丢单字汉字",
        "file": "src/localsearch/index/tokens.py",
        "old": "    trimmed = [t for t in raw if not (len(t) == 1 and HAN.match(t))]",
        "new": "    trimmed = raw",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_search_uses_and_between_the_words_of_a_long_query",
        "why": "`E38` 里最值钱的一个数：丢单字让微召回 36.1% → 75.3%（叠上 2-gram 是 "
               "58.4% → 98.8%）。AND 语义下一个 `的` 就掐死整条查询，而查询里带「的」"
               "是中文用户的常态。丢了它同样不报错，只是「经常搜不到」",
    },
    {
        "id": "f4-01",
        "label": "token 不再加引号（FTS5 的保留字按语法解释）",
        "file": "src/localsearch/index/tokens.py",
        "old": "    return '\"' + token.replace('\"', '\"\"') + '\"'",
        "new": "    return token",
        "tests": ["tests/unit/test_index_tokens.py"],
        "expect": "test_every_token_is_quoted_even_when_it_looks_harmless",
        "why": "`and.txt` 里的 `and` 是 FTS5 的保留字，裸拼进 MATCH 表达式直接抛"
               "`fts5: syntax error`。加引号是唯一**不用维护保留字名单**的做法"
               "（黑名单早晚会漏，而且漏的那次只影响一个文件名的搜索）。"
               "⚠ **点名的那条测试是补出来的**：这条变体的第一版点的就是 "
               "`test_a_file_named_and_txt_is_searchable_by_its_own_name`，"
               "**它没红**（2026-10-16 实测，pytest 退出码 1 但红的是另外两条断言 "
               "`expr` 形状的测试）。原因是引号这一层的**行为**后果被前一层挡住了："
               "`keep_token` 只放行 `\\w`（字母/数字/汉字/`_`），`*` `\"` `(` `)` 根本"
               "走不到拼表达式那一步；而查询串在 `query_tokens` 里已经被小写，"
               "`AND` 到了 FTS5 就是普通词。于是这一层今天靠**形状**守着，"
               "补在 `tests/unit/test_index_tokens.py` 里——那是两层防线里靠内的"
               "一层的正常状态，写在这里免得下次有人以为它多余",
    },
    {
        "id": "f4-01",
        "label": "`keep_token` 不再滤掉纯标点",
        "file": "src/localsearch/index/tokens.py",
        "old": "    return bool(_HAS_WORD_CHAR.search(token))",
        "new": "    return True",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_a_query_with_nothing_searchable_is_not_a_search",
        "why": "标点自己也算 token 之后，「没有可搜的内容」与「搜了但没命中」就分不开了"
               "——`!!!` 会拼出一个非空表达式，用户看到的是一句普通的「没找到」。"
               "这一层替掉的是 `MATCH ''` 那条语法错，所以它必须**在拼表达式之前**生效",
    },
    {
        "id": "f4-01",
        "label": "`put_file` 不再看 `rowcount`（重复行也写 FTS）",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        if cur.rowcount == 0:\n            return False",
        "new": "        if False:\n            return False",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_a_duplicate_does_not_add_a_second_fts_row",
        "why": "这是本项目里少见的一处「靠**别人的**行为」：`INSERT OR IGNORE` 被忽略时 "
               "`rowcount` 是 0。它变坏时**不报错**，只是 `files_fts` 里慢慢长出重复行"
               "——`files` 表看上去完全正常，搜索时同一个文件出现好几遍",
    },
    {
        "id": "f4-01",
        "label": "`files.path` 不再 `NOCASE`",
        "file": "src/localsearch/index/schema.sql",
        "old": "TEXT NOT NULL COLLATE NOCASE UNIQUE",
        "new": "TEXT NOT NULL UNIQUE",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_put_file_says_false_for_a_case_variant",
        "why": "NTFS 的路径比较不分大小写（`C:/Windows` 与 `c:/windows` 是同一个目录），"
               "两行只差大小写时的症状是计数偏大、同一个文件在结果里出现两遍、`--prune` "
               "删不干净。⚠ 这是 `schema.sql` 与计划 §4.4 原文**唯一**一处刻意的差异，"
               "撤销它等于把那处差异悄悄删掉——SQL 里没有任何东西会喊",
    },
    {
        "id": "f4-01",
        "label": "`volume` 不再统一成小写",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        object.__setattr__(self, \"volume\", drive_of(self.path).lower())",
        "new": "        object.__setattr__(self, \"volume\", drive_of(self.path))",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_volume_is_lowercased_whatever_the_scanner_said",
        "why": "两个扫描后端给的写法不一样（scandir 从 `D:/work` 出发给 `D:`，MFT 按卷"
               "枚举给 `d:`）。不统一的话 `count_files(volume=\"d:\")` 静默少算一半，"
               "而「盘点少了一半」看上去只是一个偏小的数字，不会有人去查",
    },
    {
        "id": "f4-01",
        "label": "`store_path` 不再换正斜杠",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "    return str(path).replace(\"\\\\\", \"/\")",
        "new": "    return str(path)",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_store_path_turns_backslashes_forward",
        "why": "库里只存一种写法（正斜杠）是「同一条路径只有一个真相」的落点：两种写法"
               "混进来时 `UNIQUE` 拦不住（`D:\\work\\x` 与 `D:/work/x` 是两个不同的"
               "字符串），同一个文件就成了两行。⚠ `old` 那里是**四个**反斜杠——这一处"
               "比的是 Python 源码里的字面量，不是路径本身",
    },
    {
        "id": "f4-01",
        "label": "文件名那一列不再加权",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "NAME_WEIGHT = 4.0",
        "new": "NAME_WEIGHT = 0.0",
        "tests": ["tests/unit/test_store_files_fts.py"],
        "expect": "test_a_hit_in_the_name_outranks_a_hit_only_in_the_directory",
        "why": "**这条变体该写成什么形状是量出来的**（2026-10-16，同一个库、同一条查询、"
               "只换 `bm25` 的列权重）：出厂 (4.0, 1.0) 两个文件是 -1.2978 / -0.7302"
               "（叫这个名字的在前）；名字权重调 0 → -0.7302 / -0.6465，**顺序反过来**，"
               "测试红；而把两列对调 (1.0, 4.0) → -1.2978 / -1.2907，**顺序没变**、"
               "测试不红。所以这一组钉住的是「名字那一列有正权重」，**钉不住 4.0 这个"
               "具体数**——「为什么是 4.0」至今没有护栏，记在 P4「遗留」里",
    },
    {
        "id": "f4-02",
        "label": "正文这一层也加上 2-gram",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "CHUNK_BIGRAMS = False",
        "new": "CHUNK_BIGRAMS = True",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_the_body_layer_has_no_bigrams",
        "why": "`E38` 的读数：2-gram 让正文的 token 涨到 2.76×，而收益**没实测过**"
               "（量的是文件名碎片，正文检索打的是词）。两层切法**故意不一样**，而"
               "「顺手统一成一份实现」是最自然的重构——落盘量翻倍、一个字都不报错。"
               "⚠ 判据不能写成「token 串里含不含 `度报`」：`cut_for_search` 会把整词 "
               "`年度报告` 也吐出来，那两个字的**子串**本来就在里面（第一版就是这么写"
               "的，它让变体假绿）。只有「搜 `度报` 有没有落点」才是那条分界",
    },
    {
        "id": "f4-02",
        "label": "`parser_ver` 变了也当复用",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        if row[1] == parser_ver:",
        "new": "        if True:",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_a_new_parser_version_replaces_the_old_chunks",
        "why": "计划 §4.4.1 那条「缓存键必须带版本」的落点。少了它，换了提取器之后"
               "**旧切片永远留着、新格式的正文永远进不了库**——而库里看上去一切正常"
               "（有 blob、有切片、有 FTS 行），只是那些文件的内容搜不到",
    },
    {
        "id": "f4-02",
        "label": "换版本时不删旧切片",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        self._delete_chunks(blob_id)",
        "new": "        None",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_a_new_parser_version_replaces_the_old_chunks",
        "why": "与上一条是两个不同的坏法：那一条是「压根没认出版本变了」，这一条是"
               "「认出来了但没收拾旧的」。后者更坏——`chunk_count` 被清成 0 而 `chunks` "
               "里躺着旧行，于是「这一份内容有几片」与「库里实际有几片」从此对不上",
    },
    {
        "id": "f4-02",
        "label": "删切片时不删 `chunks_fts` 那些行",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '"DELETE FROM chunks_fts WHERE rowid IN"',
        "new": '"SELECT 1 FROM chunks_fts WHERE rowid IN"',
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_replacing_leaves_no_orphan_fts_row",
        "why": "FTS5 是**独立表**，`chunks` 上的 `ON DELETE CASCADE` 管不着它。孤儿行的"
               "症状是「搜到一条、显示不出来」——`search_chunks` 的 JOIN 挂不上，"
               "而那看起来只是「某几个结果点不开」",
    },
    {
        "id": "f4-02",
        "label": "同一个 blob 允许写两遍切片",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        if existing:",
        "new": "        if False:",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_writing_chunks_twice_is_refused",
        "why": "续跑（`F4-04`）与「一轮里第二个路径碰到同一份内容」都会让人以为该再写"
               "一遍。两份一样的文本进库不报错，只在结果列表里表现为重复条目——"
               "而 `chunk_count` 也是个偏大的数",
    },
    {
        "id": "f4-02",
        "label": "`chunks_fts` 的 rowid 不再等于 `chunks.id`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "                    chunk_id,\n                    tokenize(chunk.text, bigrams=CHUNK_BIGRAMS),",
        "new": "                    999,\n                    tokenize(chunk.text, bigrams=CHUNK_BIGRAMS),",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_the_fts_rowid_is_the_chunk_id",
        "why": "这个 id 是**三条路径共用的**：`search_chunks` 的 JOIN、`_delete_chunks` "
               "的删除条件、`chunk_id` 那一列。`chunk_id` 在 `schema.sql` 里是 "
               "`UNINDEXED`，所以拿它代替 rowid 等于每次删除全扫一遍 FTS 表；而不一致"
               "本身就是「搜得到、取不回」",
    },
    {
        "id": "f4-02",
        "label": "`chunks.text` 存的是分词后的文本",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "                    chunk.text,\n                    chunk.n_tokens,",
        "new": "                    tokenize(chunk.text, bigrams=CHUNK_BIGRAMS),\n                    chunk.n_tokens,",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_the_stored_text_is_the_original_not_the_tokenized_form",
        "why": "计划 §4.4.1 把「展示片段一律从 `chunks.text` 取回原文」写成了义务。存成"
               "分词形态不报错，只是每个结果都变成小写、词间带空格的串——而"
               "`char_start` / `char_end` 从此指不进 `chunks.text`，高亮全废",
    },
    {
        "id": "f4-02",
        "label": "`blobs.chunk_count` 不再更新",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '"UPDATE blobs SET chunk_count = ? WHERE id = ?", (written, blob_id)',
        "new": '"UPDATE blobs SET chunk_count = ? WHERE id = ?", (0, blob_id)',
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_chunk_count_is_updated_on_the_blob",
        "why": "这一列是「这份内容切出几片」的**唯一**记录，`replaced` 那条路还要靠它"
               "清账。它偏小时的分级就绪判断（`F4-03`）会以为 Tier1 没跑过",
    },
    {
        "id": "f4-02",
        "label": "标题不再分词（原样进 FTS 列）",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": 'title_tok = tokenize(title or "", bigrams=CHUNK_BIGRAMS)',
        "new": 'title_tok = title or ""',
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_the_title_is_tokenized_too",
        "why": "`chunks_fts.title` 是要被 MATCH 的列，走的必须是同一套切法。原样进去"
               "之后中文标题**一个词也搜不到**（`unicode61` 把整串汉字当一个 token，"
               "而查询侧给的是切开的词）——`SELECT` 出来看还挺正常",
    },
    {
        "id": "f4-02",
        "label": "`error` 不再要求带原因",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        if status == _STATUS_WITH_ERROR and msg is None:",
        "new": "        if False:",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_an_error_outcome_needs_a_reason",
        "why": "`A6` 的后半句是「原因写入 `files.error_msg`」，而 `error_msg` 非空是全库"
               "**唯一**的失败判据。一个没有原因的失败是最难查的记录——每一层都觉得"
               "自己没错，失败报表里只剩下一堆文件路径",
    },
    {
        "id": "f4-02",
        "label": "`error_msg` 不再归一成 `NULL`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": 'msg = (error_msg or "").strip() or None',
        "new": "msg = error_msg",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_an_empty_error_message_becomes_null",
        "why": "`extract/batch.py` 的 `Outcome.error_msg` 给的是**空串**、库里要的是 "
               "`NULL`。两处形态不同的话，「这一格是不是空的」要写两种判断，而漏掉的"
               "那一种会让 `skipped` 的文件在 `WHERE error_msg IS NOT NULL` 里冒出来——"
               "失败报表被噪音淹掉",
    },
    {
        "id": "f4-02",
        "label": "结局写到不存在的路径上也算成功",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "        return cur.rowcount > 0",
        "new": "        return True",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_an_unknown_path_returns_false_and_writes_nothing",
        "why": "全盘一轮里「结局落到空处」意味着 `files` 的路径与提取那一侧对不上"
               "（归一化漏了一处、大小写、外接盘掉了）。计数对不上是唯一的线索，"
               "返回常量 `True` 把这条线索也堵死",
    },
    {
        "id": "f4-02",
        "label": "达成的 tier 不落库（一律 0）",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "                status,\n                tier,",
        "new": "                status,\n                0,",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_a_fulltext_outcome_links_the_blob_and_lifts_the_tier",
        "why": "`tier` 记的是**达成的最高级**（`D10`），`F4-03` 的选择条件与「语义未就绪」"
               "的显式标注都读它。全落 0 之后那一趟会把全盘重做一遍，而分级就绪永远"
               "停在 Tier0——两件事都不报错",
    },
    {
        "id": "f4-02",
        "label": "正文检索忽略 `limit`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": "            (expr, limit),",
        "new": "            (expr, 20),",
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_limit_is_honoured",
        "why": "⚠ 这里的 `limit` 默认就是 20，所以这一条变体只钉得住「调用方给的 `limit` "
               "被用上了」——钉不住默认值本身（那要看 `F4-04` 的 CLI 面）。"
               "正文一片 512 token，一页几十条就是几 MB 的结果",
    },
    {
        "id": "f4-02",
        "label": "`paths_for_blob` 不再排序",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '"SELECT path FROM files WHERE blob_id = ? ORDER BY path", (blob_id,)',
        "new": '"SELECT path FROM files WHERE blob_id = ? ORDER BY path DESC", (blob_id,)',
        "tests": ["tests/unit/test_store_body_fts.py"],
        "expect": "test_paths_for_blob_is_sorted",
        "why": "一份内容对应好几个路径时，去重（`D12`）在结果面上的样子就是这一串。"
               "顺序不定的话同一个查询两次跑出不同的输出——而「哪些文件里有这份内容」"
               "的答案会随扫描顺序漂移（`F6` 的 `--prune` 也会跟着不稳）",
    },
    # ============================================================== F4-03
    {
        "id": "f4-03",
        "label": "Tier0 那一趟也加上 `tier < ?` 子句",
        "file": "src/localsearch/index/tier.py",
        "old": '    if tier == TIER_METADATA:\n        return f"status IN ({marks})", names\n',
        "new": "",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_tier0_has_no_tier_clause",
        "why": "⚠ `F4-03` 最容易写错、也最不容易发现的一处：`files.tier` 的默认值就是 0，"
               "所以 `tier < 0` **恒假**——Tier0 那一趟从此挑不到任何文件，而它不报错、"
               "不写日志，只见「文件名索引 0 / 1000000 没做完」永远挂着",
    },
    {
        "id": "f4-03",
        "label": "Tier1 的选择条件用「是什么」写",
        "file": "src/localsearch/index/tier.py",
        "old": '    TIER_FULLTEXT: frozenset(STATUSES - TERMINAL_STATUSES - {"pending"}),',
        "new": '    TIER_FULLTEXT: frozenset({"metadata", "stale"}),',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_body_pass_asks_for_the_rows_that_still_need_a_body",
        "why": "「是什么」漏掉的是**自相矛盾的行**（如 `status='fulltext', tier=0`）：它既不"
               "算做完、又不在名单里，于是永远不再被处理。这一格会被永久跳过，"
               "而库里看不出任何异常",
    },
    {
        "id": "f4-03",
        "label": "上两级不排除终态（`skipped` / `error`）",
        "file": "src/localsearch/index/tier.py",
        "old": '    TIER_FULLTEXT: frozenset(STATUSES - TERMINAL_STATUSES - {"pending"}),',
        "new": '    TIER_FULLTEXT: frozenset(STATUSES - {"pending"}),',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_both_higher_passes_skip_the_terminal_statuses",
        "why": "全盘上 `.exe` / `.db` 有几千个，它们**每一趟都会被重新挑出来**再判一次"
               "「这一类不做」——多花的不是正确性而是每趟几十秒，以及「这一轮处理了"
               "12 万个文件」这句统计从第一天起就是假的",
    },
    {
        "id": "f4-03",
        "label": "Tier0 的「做到了」按 `tier >= 0` 数",
        "file": "src/localsearch/index/tier.py",
        "old": "    reached[TIER_METADATA] = total - pending",
        "new": "    reached[TIER_METADATA] = total",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_tier0_done_is_total_minus_pending",
        "why": "`tier >= 0` 对每一行都成立（0 是默认值）——那是一个**恒真**的判断，"
               "等于说「名字全进过索引了」。于是「哪几个文件连名字都还没进去」"
               "这个问题永远得不到答案",
    },
    {
        "id": "f4-03",
        "label": "就绪的分母一律用总数",
        "file": "src/localsearch/index/tier.py",
        "old": "        if tier == TIER_METADATA:\n            return self.total\n        return self.total - self.terminal",
        "new": "        return self.total",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_denominator_excludes_the_terminal_statuses",
        "why": "全盘上任何一个 `.exe`（`skipped`）都让正文那一级**永远不算就绪**——"
               "一个永远为假的标志等于没有标志，而它连着的是 `D10` 那句「显式标注」",
    },
    {
        "id": "f4-03",
        "label": "空库也报「就绪」",
        "file": "src/localsearch/index/tier.py",
        "old": "        if self.is_empty:\n            return False\n        return self.done_at(tier) >= self.expected_at(tier)",
        "new": "        return self.done_at(tier) >= self.expected_at(tier)",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_an_empty_index_is_never_ready",
        "why": "空库时两个数都是 0，`0 >= 0` 让三级**全部就绪**——这是这个模块最坏的一种"
               "错法：它把「根本没索引过」报成「一切正常」，而用户搜不到任何东西时"
               "拿到的是一句「索引没做完吗？没有啊」",
    },
    {
        "id": "f4-03",
        "label": "`stale` 的行算作做到了它到过的那一级",
        "file": "src/localsearch/index/tier.py",
        "old": '        if status != "stale":\n            for level in (TIER_FULLTEXT, TIER_SEMANTIC):\n                if tier >= level:\n                    reached[level] += count',
        "new": "        for level in (TIER_FULLTEXT, TIER_SEMANTIC):\n            if tier >= level:\n                reached[level] += count",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_stale_row_does_not_count_as_done_for_the_tier_it_reached",
        "why": "`stale` 的行内容已经变了、库里那一份是旧的，而它**看起来是有正文的**。"
               "把它算成就绪就是「报就绪而内容是旧的」——少算一行只会多一句警告，"
               "多算一行会给出错的答案",
    },
    {
        "id": "f4-03",
        "label": "台账遇见不认识的 `status` 就跳过",
        "file": "src/localsearch/index/tier.py",
        "old": '        if status not in STATUSES:\n            raise ValueError(f"不认识的 status：{status!r}（{STATUSES}）")',
        "new": "        if status not in STATUSES:\n            continue",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_ledger_refuses_a_status_it_does_not_know",
        "why": "不认识的 `status` 只有两个来源：手改过库，或者 `schema.sql` 与代码漂了。"
               "「照单全收」会给出一个**看起来正常**的数（多出来的行算进 `total` 却落在"
               "三级之外，于是三级全就绪而文件总数对不上）",
    },
    {
        "id": "f4-03",
        "label": "`target_tier` 按「先看 metadata」的顺序取",
        "file": "src/localsearch/index/tier.py",
        "old": "    if tiers.semantic:\n        return TIER_SEMANTIC\n    if tiers.fulltext:\n        return TIER_FULLTEXT\n    if tiers.metadata:\n        return TIER_METADATA\n    return -1",
        "new": "    if tiers.metadata:\n        return TIER_METADATA\n    if tiers.fulltext:\n        return TIER_FULLTEXT\n    if tiers.semantic:\n        return TIER_SEMANTIC\n    return -1",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_switches_are_cumulative",
        "why": "三个开关是**累进**的（`metadata` 默认就是 `True`），先看它就等于说"
               "「配置里永远只做 Tier0」——索引跑得飞快、只有文件名搜得到，"
               "而所有开关看上去都开着",
    },
    {
        "id": "f4-03",
        "label": "`status` 声称的级别可以高于 `tier` 记的",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '        floor = STATUS_MIN_TIER.get(status, 0)\n        if tier < floor:\n            raise ValueError(\n                f"status={status!r} 说这个文件做到了第 {floor} 级，而 tier={tier}"\n                f" 比它低——两列在说两件事，其中一件是假的"\n            )\n',
        "new": "",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_status_that_claims_more_than_the_tier_is_refused",
        "why": "`status='fulltext', tier=0` 的行会被台账按 `tier` 数成「正文没做」、"
               "被选择条件按 `status` 跳过——两边各自看都没错，合起来是「那一级永远"
               "不就绪」，而没有任何一趟会去修它",
    },
    {
        "id": "f4-03",
        "label": "`status` 说「什么都没做」却带着 `tier`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '        ceiling = STATUS_MAX_TIER.get(status)\n        if ceiling is not None and tier > ceiling:\n            raise ValueError(\n                f"status={status!r} 说这个文件什么都没做，而 tier={tier} 说它到过"\n                f"第 {tier} 级：这样的行没有任何一趟会再挑到它（它既不算做完，"\n                f"也不在选择条件里）"\n            )\n',
        "new": "",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_status_that_claims_nothing_cannot_have_a_tier",
        "why": "`status='metadata', tier=1` 那种行**三趟都不接它**（Tier0 只挑 `pending`、"
               "Tier1 要求 `tier < 1`、Tier2 的 status 名单里没有 `metadata`），"
               "而台账还把它记成「第 1 级做到了」——**报就绪、其实没有正文**",
    },
    {
        "id": "f4-03",
        "label": "`iter_tier_work` 接受 `limit=0`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '        if limit is not None and limit < 1:\n            raise ValueError(f"limit 要么不传，要么 ≥ 1，收到 {limit}")\n',
        "new": "",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_non_positive_limit_is_refused",
        "why": "`LIMIT 0` 是合法的 SQL、返回 0 行——于是「这一趟没活干」与「分批的"
               "那个参数写错了」长得一模一样，而 `F4-04` 的续跑正是在这里判「跑完了」",
    },
    {
        "id": "f4-03",
        "label": "`tier_counts` 忘了 `GROUP BY`",
        "file": "src/localsearch/index/sqlite_store.py",
        "old": '"SELECT status, tier, count(*) FROM files GROUP BY status, tier"',
        "new": '"SELECT status, tier, count(*) FROM files"',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_tier_counts_groups_by_status_and_tier",
        "why": "SQLite 允许「裸列 + 聚合」（返回其中任意一行的 `status`/`tier`），"
               "于是这条查询**不报错**、只返回一行：台账从此永远说「库里只有 1 个"
               "文件」，而三级进度全部基于那一行",
    },
    {
        "id": "f4-03",
        "label": "命令行明确要的语义检索被悄悄换成词法",
        "file": "src/localsearch/search/tier.py",
        "old": "        if explicit:\n            raise ModeUnavailable(requested, IMPLEMENTED_MODES)\n",
        "new": "",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_named_semantic_mode_is_refused_rather_than_silently_downgraded",
        "why": "`A8` 里「不允许静默」那半句最要紧的一格：一个人要的是「意思相近」，"
               "而词法检索会给出一堆**看起来合理**的关键词命中——他没有任何办法"
               "察觉自己拿到的是另一种东西",
    },
    {
        "id": "f4-03",
        "label": "`IMPLEMENTED_MODES` 把没实现的模式也算上",
        "file": "src/localsearch/search/tier.py",
        "old": 'IMPLEMENTED_MODES = frozenset({"lexical"})',
        "new": "IMPLEMENTED_MODES = frozenset(MODES)",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_lexical_is_the_mode_that_works_today",
        "why": "这一个常量是「哪些模式真的能跑」的唯一来源，降级与拒绝都读它。"
               "写成全量就等于宣称 `P5` 已经做完了：`--mode semantic` 不再被拒，"
               "一路走到一个没有向量的检索引擎上",
    },
    {
        "id": "f4-03",
        "label": "`hybrid` 降级时跳过 `semantic` 直接落到 `lexical`",
        "file": "src/localsearch/search/tier.py",
        "old": '    "hybrid": ("hybrid", "semantic", "lexical"),',
        "new": '    "hybrid": ("hybrid", "lexical"),',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_hybrid_falls_back_to_semantic_before_lexical_once_vectors_exist",
        "why": "那张表里的**顺序**表达的是「谁是谁的子集」。跳过 `semantic` 在 `P4` 上"
               "看不出区别（两级都不能跑），到 `P5` 那天就是用户白丢一半语义命中，"
               "而代码一个字都不用改",
    },
    {
        "id": "f4-03",
        "label": "`semantic` 降级时不留词法这个底座",
        "file": "src/localsearch/search/tier.py",
        "old": '    "semantic": ("semantic", "lexical"),',
        "new": '    "semantic": ("semantic",),',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_a_configured_semantic_mode_downgrades_and_says_语义未就绪",
        "why": "`lexical` 是唯一一个**永远在**的模式（它不依赖任何一级索引的额外"
               "产物）。降级表里把它删掉之后，配置里写了 `mode = \"semantic\"` 的人"
               "连搜索都跑不起来——而那是模板里默认值的近邻",
    },
    {
        "id": "f4-03",
        "label": "降级那句话把「词法」写死",
        "file": "src/localsearch/search/tier.py",
        "old": "                _DOWNGRADE_TEMPLATE[requested].format(\n                    label=_MODE_LABEL[effective], limit=_MODE_LIMIT[effective]\n                ),",
        "new": '                _DOWNGRADE_TEMPLATE[requested].format(\n                    label="词法（关键词）", limit="只按关键词命中，换个说法搜不到"\n                ),',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_hybrid_falls_back_to_semantic_before_lexical_once_vectors_exist",
        "why": "`P5` 之后 `hybrid` 会先落到 `semantic`，那时写死的那句话就成了一处"
               "**看起来正常**的错话——它描述的是一种这一趟没有发生的检索。"
               "`F4-03` 第一版就是这么写的，被这条变体自己的测试抓住过",
    },
    {
        "id": "f4-03",
        "label": "标注不看 `target_tier`",
        "file": "src/localsearch/search/tier.py",
        "old": "        if tier > target or not ledger.can_reach(tier) or ledger.ready(tier):\n            continue",
        "new": "        if not ledger.can_reach(tier) or ledger.ready(tier):\n            continue",
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_switching_a_level_off_silences_its_note",
        "why": "配置里 `fulltext = false` 意味着 Tier1 永远不做，此时还一直警告"
               "「正文未就绪」就是一句**永远为真的废话**——而永远为真的警告会被"
               "学会忽略，连带着真警告一起被忽略",
    },
    {
        "id": "f4-03",
        "label": "`TIER_NAMES` 与 `TiersCfg` 的字段名漂开",
        "file": "src/localsearch/index/tier.py",
        "old": '    TIER_FULLTEXT: "fulltext",',
        "new": '    TIER_FULLTEXT: "content",',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_three_tier_names_are_the_config_field_names",
        "why": "配置里写 `[tiers] fulltext = false`、日志里说「停在 fulltext」、报表的列名"
               "是 `fulltext`——三处是同一个词。改名之后这句配置**看起来关掉了正文那一"
               "层**，而代码读的是另一个名字",
    },
    {
        "id": "f4-03",
        "label": "第 2 级那句标注不再说「语义未就绪」",
        "file": "src/localsearch/search/tier.py",
        "old": '        "语义未就绪：{done} / {expected}。向量还没建完，这一级现在给不出结果；"',
        "new": '        "向量还没建完：{done} / {expected}。这一级现在给不出结果；"',
        "tests": ["tests/unit/test_tier_state.py"],
        "expect": "test_the_tier2_note_speaks_up_once_semantic_mode_really_exists",
        "why": "「语义未就绪」是计划与 `CLAUDE.md` 点名的那句话，也是 `A8` 唯一的"
               "可观察形态。改措辞可以，改掉这五个字就是把验收项指认的那个字符串"
               "删了——而测试里钉的就是它",
    },
    # ------------------------------------------------------------------ f4-04
    # 编排（`index/build.py`）与两个命令面（`cli/commands/`）。这一层最容易出的错
    # 是"**什么都不做也看着正常**"：批没提交（库里少一片，但报告说完成了）、
    # 目录也写了行（多几行假文件）、复用那一问没问（白提取一遍）……
    # 每一条变体的 `why` 写的都是那个坏结果**在终端上看起来是什么样**。
    {
        "id": "f4-04",
        "label": "目录也写一行进 `files`",
        "file": "src/localsearch/index/build.py",
        "old": "        if entry.is_dir:",
        "new": "        if False:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_tier0_writes_files_and_leaves_directories_out",
        "why": "`files` 里没有'这一行是目录'这一列，写进去的一行目录与一个 0 字节的文件"
               "**完全分不开**；接着 Tier1 会去提取它，`content_hash()` 在目录上抛 "
               "`IsADirectoryError`，被记成一笔失败。全盘上目录是百万级的——"
               "那就是百万条**全部是假的失败**，而报告看起来只是'坏文件有点多'",
    },
    {
        "id": "f4-04",
        "label": "Tier0 跑完不再收尾提交",
        "file": "src/localsearch/index/build.py",
        "old": "    store.commit()\n    stats.commits += 1\n    stats.seconds = time.monotonic() - started\n    return stats",
        "new": "    stats.seconds = time.monotonic() - started\n    return stats",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_tier0_commits_every_n_files_and_once_more_at_the_end",
        "why": "最后一个不满一批的尾巴就交给 `close()` 的兜底，而兜底只在**正常退出**时"
               "发生。被 Ctrl-C 或断电打断时那一批**没了**，而终端上最后一次进度行说的是"
               "'已写 N 行'——用户以为它在库里",
    },
    {
        "id": "f4-04",
        "label": "Tier0 中途一次也不提交",
        "file": "src/localsearch/index/build.py",
        "old": "        if since_commit >= every:",
        "new": "        if False:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_tier0_commits_every_n_files_and_once_more_at_the_end",
        "why": "全盘的 300 多万个条目会落在**一个**事务里：中断丢整趟（`A1` 的续跑"
               "从头再来一遍），内存随事务长（`R1` 那类'扫到一半机器没响应'）。"
               "唯一看得出来的是事务日志的大小，而没人会去看它",
    },
    {
        "id": "f4-04",
        "label": "超限文件记成 `metadata` 而不是 `skipped`",
        "file": "src/localsearch/index/build.py",
        "old": "        return STATUS_SKIPPED",
        "new": "        return STATUS_METADATA",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_tier0_marks_over_limit_files_name_only",
        "why": "`metadata` 不是终态，于是 Tier1 会去提取它——而它存在的理由正是"
               "**不要**读它（64 MiB 以上的日志、镜像）。读进来的是几百 MB 的正文、"
               "还要切片，而进度条上只有'正文 N 片'变大了",
    },
    {
        "id": "f4-04",
        "label": "A9：不再先问库里有没有这份内容",
        "file": "src/localsearch/index/build.py",
        "old": "    known = store.find_blob(digest, parser_ver=ver)",
        "new": "    known = None",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_the_same_content_is_extracted_only_once",
        "why": "`A9` 的字面要求是'同一份内容只**提取**与嵌入一次'。只靠 `put_blob()` 的"
               "`reused` 做不到那一半：走到那里时文件已经读进来、正文已经解析出来了。"
               "第二次提取白做，而结果**完全正确**——只是慢，且慢得没有理由",
    },
    {
        "id": "f4-04",
        "label": "`find_blob()` 与 `put_blob()` 漂了也照跑",
        "file": "src/localsearch/index/build.py",
        "old": "    if not write.needs_chunks:",
        "new": "    if False:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_find_blob_and_put_blob_must_agree",
        "why": "跳过 `put_chunks()` 会在库里留下一行**声称有正文、其实一片都没有**的记录："
               "搜不到，且不报错。两个方法的判据本该是同一个 `WHERE`——这条守卫是"
               "那条耦合唯一的哨兵",
    },
    {
        "id": "f4-04",
        "label": "批次没推进时改成静默退出",
        "file": "src/localsearch/index/build.py",
        "old": "            raise BuildError(\n                f\"这一批没有推进",
        "new": "            break\n            raise BuildError(\n                f\"这一批没有推进",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_a_batch_that_does_not_advance_stops_loudly",
        "why": "⚠ 这一条**不能**写成 `if first == previous_first:` → `if False`：那样"
               "`while True` 会**真的转不完**，而这个工具的 `subprocess.run` 没有超时，"
               "整次检查会挂在那里。改成 `break` 才是那个坏行为的最小等价物——"
               "安静地退出、报告说'跑完了'，而库里那一行还在最前面，下一趟再挑中它",
    },
    {
        "id": "f4-04",
        "label": "`parser_ver` 的 `chunk` 段丢掉两个参数",
        "file": "src/localsearch/index/build.py",
        "old": '        f"|chunk:{chunk.target_tokens}:{chunk.overlap_ratio:g}:{chunk.max_chunks_per_doc}"',
        "new": '        f"|chunk:{chunk.target_tokens}"',
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_the_parser_version_shape_is_a_literal_contract",
        "why": "`parser_ver` 是'这份切片该不该重切'的**唯一**判据。少了两个参数之后"
               "`lsearch index --max-chunks-per-doc 10` 写下的版本与上一趟**一模一样**，"
               "于是换参数永远不重切——而库里那些切片确实是按旧参数切的，搜出来的正文"
               "看起来没问题。⚠ 这一条的第一版**一条测试都没红**（三个断言都是拿 "
               "`parser_ver()` 现算一份来比的），那条字面契约的测试是跟着它补出来的",
    },
    {
        "id": "f4-04",
        "label": "`--resume` 不再跳过 Tier0",
        "file": "src/localsearch/index/build.py",
        "old": "    if not resume and target_tier >= TIER_METADATA:",
        "new": "    if target_tier >= TIER_METADATA:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_resume_skips_tier0_and_finishes_what_is_left",
        "why": "`--resume` 的**全部**意思就是'这一趟不重新扫盘'（接续本身不需要开关，"
               "普通 `index` 本来就接着上次跑）。不跳的话它在 `E11` 那个读数上是"
               "300 多万个目录项白走一遍——而输出与'跳过了'一模一样",
    },
    {
        "id": "f4-04",
        "label": "`--tier 0` 照样去做正文",
        "file": "src/localsearch/index/build.py",
        "old": "    if target_tier >= TIER_FULLTEXT:",
        "new": "    if True:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_target_tier_zero_stops_before_the_body_pass",
        "why": "`--tier 0` 是'先只建名字这一层就够用了'（`F4-01` 那个阶段目标的用法）。"
               "多做一级不是错答，是**多花几小时**，而且用户没有办法从报告里看出来"
               "它做了他没要的那一级",
    },
    {
        "id": "f4-04",
        "label": "扫描器开不起来时甩栈（不再当配置错误）",
        "file": "src/localsearch/cli/commands/index.py",
        "old": "    try:\n        return make_scanner(scan_config)\n    except ScanError as exc:\n        typer.secho(f\"无法开始：{exc}\", fg=\"red\", err=True)\n        raise typer.Exit(EXIT_USAGE) from exc",
        "new": "    return make_scanner(scan_config)",
        "tests": ["tests/unit/test_cli_index_dryrun.py"],
        "expect": "test_a_refused_config_error_leaves_no_trace",
        "why": "⚠ 这是**真实发生过**的回归：`F4-04` 把 `make_scanner()` 从 `_dry_run()` "
               "里拉出来时漏了这道 `except`，`lsearch index --all-drives` 甩一屏 "
               "traceback、退出码 1——而退出码 1 在计划 §4.8 里的意思是'跑完了但有文件"
               "没成'，脚本会去重试一件永远不成的事",
    },
    {
        "id": "f4-04",
        "label": "`search` 不先看库在不在",
        "file": "src/localsearch/cli/commands/search.py",
        "old": "    if not db_path.exists():",
        "new": "    if False:",
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_a_missing_database_is_refused_and_leaves_no_trace",
        "why": "`sqlite3.connect()` **会建文件**：用户得到的是'什么都没搜到'加一个刚被"
               "创建出来的空库，而那个空库会让之后每一次命令都以为'索引已经建好了'"
               "（连带 `data_dir` 也建出来了，`A7` 的落点）",
    },
    {
        "id": "f4-04",
        "label": "显式 `--mode semantic` 悄悄降级（不拒绝）",
        "file": "src/localsearch/search/tier.py",
        "old": "        if explicit:",
        "new": "        if False:",
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_an_explicit_semantic_mode_is_refused",
        "why": "判据是**这句话是不是人说的**：一个人明确要了语义检索，词法给他的是一堆"
               "**看起来合理**的关键词命中，他没有任何办法察觉自己拿到的是另一种东西。"
               "这是 `A8` 的极端形态——不是结果不完整，是结果答的是另一个问题",
    },
    {
        "id": "f4-04",
        "label": "`IMPLEMENTED_MODES` 声称三种都能跑",
        "file": "src/localsearch/search/tier.py",
        "old": 'IMPLEMENTED_MODES = frozenset({"lexical"})',
        "new": 'IMPLEMENTED_MODES = frozenset({"lexical", "semantic", "hybrid"})',
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_the_config_default_hybrid_is_downgraded_with_a_note",
        "why": "这一格一变，降级与拒绝**两条路一起消失**：默认配置不再降级（也就不再"
               "标注「语义未就绪」），而 `--mode semantic` 不再被拒绝。终端上它看起来"
               "完全正常——`检索模式 semantic`，只是结果全是关键词命中",
    },
    {
        "id": "f4-04",
        "label": "标注一条都不打印",
        "file": "src/localsearch/cli/commands/search.py",
        "old": "    for note in readiness.notes:",
        "new": "    for note in ():",
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_a_tier0_only_index_still_answers_by_name_and_says_body_is_not_ready",
        "why": "`search_chunks()` 在正文一片都没进库时返回空列表，而那与'跑完了、但这"
               "个词没出现过'**在数据上完全一样**。那句话说出口的责任在这一层："
               "少了它，用户看到的是'一条都没有'，他会去改查询",
    },
    {
        "id": "f4-04",
        "label": "`--limit` 不往下传给库",
        "file": "src/localsearch/cli/commands/search.py",
        "old": "        paths = store.search_paths(query, limit=top_k)",
        "new": "        paths = store.search_paths(query)",
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_limit_caps_each_section",
        "why": "`--limit 2` 给了、输出还是缺省那 20 条，而用户以为自己限住了。参数被"
               "声明了却没人用，是这个项目反复点名的那一类错——它**看起来完全正常**",
    },
    {
        "id": "f4-04",
        "label": "整串标点的查询当成'没搜到'",
        "file": "src/localsearch/cli/commands/search.py",
        "old": "    if not paths.has_query:",
        "new": "    if False:",
        "tests": ["tests/unit/test_cli_search.py"],
        "expect": "test_a_query_with_no_searchable_token_says_so",
        "why": "'这个查询里没有可以搜的词'与'没搜到'的下一步完全不同：前者要用户改查询，"
               "后者要他改索引。只回两个空列表的话，用户会以为工具丢了他的查询",
    },
    {
        "id": "f4-04",
        "label": "自相矛盾的参数组合不再拦",
        "file": "src/localsearch/cli/commands/index.py",
        "old": "    if dry_run and resume:",
        "new": "    if False:",
        "tests": ["tests/unit/test_index_resume.py"],
        "expect": "test_self_contradicting_flags_are_refused_before_touching_anything",
        "why": "`--dry-run --resume` 走到 dry-run 那一支、报出一份看着完整的分桶报表，"
               "退出码 0——而用户要的'接着上一轮跑完'一个字都没做。这类组合与磁盘状态"
               "无关，等跑了半小时才发现是最没有必要的浪费",
    },
    # ---------------------------------------------------------------- f5-01
    #: `F5-01`（嵌入器双后端）这一组的共同点：**每一处改坏都不报错**，只让向量变差、
    #: 或让不该进索引的东西进索引。前缀、归一化、掩码池化、指纹这四处是本轮全部
    #: 「静默失效」的落点，所以它们各有一条。
    #:
    #: ⚠ 这里**没有**「查询前缀与文档前缀必须逐字不同」那一条：实测它不可变体化。
    #: 那处守卫的三条分支互相覆盖——两条前缀相同时，文档那条要么没有标题槽（被
    #: 「标题槽恰好一次」拦下），要么有（被「查询前缀不许有标题槽」拦下）。去掉它，
    #: `test_prefix_guards_reject` 照样全绿，也就是说**它现在没有护栏**：它保住的是
    #: 报错消息指得准（「非对称被破坏」而不是「标题槽次数不对」），不是行为。记在
    #: 这里而不是补一条测试，是因为要补就得先造出一个只有它拦得住的输入，而那样的
    #: 输入不存在。
    #:
    #: ⚠ 同一天这个工具**抓到过我自己**：`sentence_embedding` 那条变体第一版只改了
    #: 判据（`if _EMBEDDING_OUTPUT in names:` → `if _HIDDEN_OUTPUT in names:`）而没改
    #: 返回的值，于是"两个输出都在"的图上行为**一模一样**——它照样报「没红」。那不是
    #: 缺护栏，是我的变体没生效；把两行一起换掉才是真的反转优先级。这条记在这儿，是
    #: 因为"改坏一处代码"这件事本身就有一个不生效的失败形态，而它长得与"没有护栏"
    #: 完全一样（`--self-test` 那一条喂的就是这个东西）。
    {
        "id": "f5-01",
        "label": "标题槽不再替换成真标题",
        "file": "src/localsearch/embed/base.py",
        "old": '        return self.document.replace(TITLE_NONE, f"title: {cleaned}")',
        "new": "        return self.document",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_title_replaces_the_slot",
        "why": "`title: none` 是**占位符**（`M3` 的 `title: {t} | text: {c}`），不是"
               "「标题这一项的值叫 none」。不替换时索引照建、搜索照出结果、向量照样"
               "是单位长度——只是**标题里的词一个都搜不到**，而文件名恰恰是 Tier0 "
               "最依赖的那一列（`A8` 的分级就绪全指望着它）",
    },
    {
        "id": "f5-01",
        "label": "前缀不再要求以空白结尾",
        "file": "src/localsearch/embed/base.py",
        "old": '            if not text.endswith((" ", "\\n", "\\t")):',
        "new": "            if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_prefix_guards_reject",
        "why": "拼接是 `prefix + text`，中间不加分隔符（与 sentence-transformers 自己"
               "一致）。前缀末尾少一个空格，`query:` 就与正文粘成一个 token——模型"
               "照样吐出一个看着正常的向量，只是它算的是另一个词",
    },
    {
        "id": "f5-01",
        "label": "标题槽的出现次数不再是硬约束",
        "file": "src/localsearch/embed/base.py",
        "old": "        if found != 1:",
        "new": "        if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_prefix_guards_reject",
        "why": "0 次时 `document_prefix()` 无可替换，**有标题也永远按无标题处理**"
               "（同上一条的症状）；2 次时不知道该换哪一个。两种都只在「换了份前缀"
               "词表」时才会遇到，而那时最需要的就是当场出声",
    },
    {
        "id": "f5-01",
        "label": "指纹里不再收 `max_seq`",
        "file": "src/localsearch/embed/base.py",
        "old": '        f"|{cfg.max_seq}|{cfg.quantize}"',
        "new": '        f"|{cfg.quantize}"',
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_embed_ver_changes_with_the_fingerprint",
        "why": "`max_seq` 决定「多长以后的正文被丢掉」，所以同一个切片在 2048 与 512 "
               "下是**两个不同的向量**。少了它，改截断长度不会让任何一行失效：库里"
               "一半的向量按旧口径算、另一半按新口径算，而索引说它们都是新的（`A9`）",
    },
    {
        "id": "f5-01",
        "label": "指纹里混进 `backend`",
        "file": "src/localsearch/embed/base.py",
        "old": '        f"|{cfg.max_seq}|{cfg.quantize}"',
        "new": '        f"|{cfg.max_seq}|{cfg.quantize}|{cfg.backend}"',
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_embed_ver_ignores_knobs_that_only_change_the_speed",
        "why": "这是**反方向**的错：把后端放进指纹不会算错任何东西，代价是把「换个"
               "跑得更快的实现」（`D9` 选 ONNX 的整个理由）变成一整趟全库重嵌——"
               "`A1` 的两万文件基准集上是数小时到一天，而两条路的向量余弦实测 "
               "0.999935（`test_embed_backends_agree` 钉着那个读数）",
    },
    {
        "id": "f5-01",
        "label": "零向量不再当场报错",
        "file": "src/localsearch/embed/base.py",
        "old": "    if not np.isfinite(norms).all() or float(norms.min(initial=np.inf)) < _ZERO_NORM_EPS:",
        "new": "    if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_postprocess_rejects_a_zero_row",
        "why": "零向量除以自己的长度是 NaN，而 NaN 与任何查询的余弦都是 NaN——比较恒假，"
               "那个切片**永远搜不到**，一句话都不会说。这条变体实测还有个二次教训："
               "去掉这道守卫之后，下游的 `check_unit_norm` 也**接不住**（`nan > tol` "
               "恒为假），所以它就是这一档的最后一道",
    },
    {
        "id": "f5-01",
        "label": "按 `dim` 截断后不再重新归一化",
        "file": "src/localsearch/embed/base.py",
        "old": "    vecs = l2_normalize(vecs, source=source)",
        "new": "    vecs = vecs",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_postprocess_truncates_then_normalizes",
        "why": "`D5` 的 MRL 截断取前 `dim` 列，向量长度随之小于 1。余弦仍然单调、排序"
               "仍然「看着对」，而 `F5-04` 的 HNSW 用内积当余弦，前提正是单位长度"
               "——那时错误表现为「近邻里混进一批远的」，没有任何异常",
    },
    {
        "id": "f5-01",
        "label": "传进来一整条字符串不再拦",
        "file": "src/localsearch/embed/base.py",
        "old": "    if isinstance(texts, str):",
        "new": "    if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_a_bare_string_is_rejected",
        "why": "`Sequence[str]` 对 `str` 说得通，于是遍历**逐字符**加前缀：一条查询变成"
               "十几个单字查询，返回十几个向量。调用方拿到的形状完全合法，只是搜出来"
               "的东西与用户输入的那句话无关",
    },
    {
        "id": "f5-01",
        "label": "`titles` 与 `texts` 不等长不再拦（zip 会静默截断）",
        "file": "src/localsearch/embed/base.py",
        "old": "            if len(title_list) != len(items):",
        "new": "            if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_title_count_must_match",
        "why": "两条是逐条对应的，`zip` 截短到短的那一边——每一条文档会带上**别人**的"
               "标题（或者最后几条根本没标题），而向量、行数、索引全都正常。标题是"
               "Tier0/Tier1 的主要召回来源，错位之后最像「这个文件的标题真不相关」",
    },
    {
        "id": "f5-01",
        "label": "后端返回的行数不对不再拦",
        "file": "src/localsearch/embed/base.py",
        "old": "            if len(raw) != len(batch):",
        "new": "            if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_wrong_row_count_from_the_backend_is_caught",
        "why": "少给几行时若按 `min(行数)` 对齐，**每一个切片都挂上别人的向量**——而它与"
               "「索引建好了」长得一模一样。后端换一版导出、或者哪次 padding 出了岔子，"
               "这就是默认的失败形态",
    },
    {
        "id": "f5-01",
        "label": "`close()` 之后继续算",
        "file": "src/localsearch/embed/base.py",
        "old": "        if self._closed:",
        "new": "        if False:",
        "tests": ["tests/unit/test_embed_base.py"],
        "expect": "test_close_is_idempotent_and_stops_further_use",
        "why": "`close()` 是 `F5-02` 多进程调度收尾时的动作（一个子进程一批文件，跑完"
               "就关）。关掉之后继续用会**算出一堆零或直接崩**，而不是报一句「已经 "
               "close 过了」——这正是「静默失效」的形态",
    },
    {
        "id": "f5-01",
        "label": "多模态槽的符号维当成 1（「一条带黑图的样本」）",
        "file": "src/localsearch/embed/gemma_onnx.py",
        "old": "        shape.append(dim if isinstance(dim, int) and dim >= 0 else 0)",
        "new": "        shape.append(dim if isinstance(dim, int) and dim >= 0 else 1)",
        "tests": ["tests/unit/test_embed_onnx.py"],
        "expect": "test_empty_shapes_come_from_the_graph_not_from_a_constant",
        "why": "`(0, 512)` 的空张量是在说「这条样本没有图」；`(1, 512)` 的零张量说的是"
               "「这条样本带一张全黑的图」。**后者不报错**，模型照常算出一个看着正常的"
               "向量——所以形状只能从图自己声明的元数据里算，一个数都不许写死",
    },
    {
        "id": "f5-01",
        "label": "两个输出都在时改成优先用 `last_hidden_state`",
        "file": "src/localsearch/embed/gemma_onnx.py",
        "old": "    if _EMBEDDING_OUTPUT in names:\n        return _EMBEDDING_OUTPUT",
        "new": "    if _HIDDEN_OUTPUT in names:\n        return _HIDDEN_OUTPUT",
        "tests": ["tests/unit/test_embed_onnx.py"],
        "expect": "test_sentence_embedding_is_preferred",
        "why": "图里那份池化是**模型作者实现的那一份**，我们自己 mean pool 只是「等价"
               "实现」。换成自己池化不会报错，只是绕开了作者的池化层（这一类模型的"
               "池化细节并不总是「对 mask 求平均」），而向量照样是单位长度",
    },
    {
        "id": "f5-01",
        "label": "自己池化时漏乘注意力掩码",
        "file": "src/localsearch/embed/gemma_onnx.py",
        "old": "    weights = mask[..., None].astype(np.float32)",
        "new": "    weights = np.ones(mask.shape + (1,), dtype=np.float32)",
        "tests": ["tests/unit/test_embed_onnx.py"],
        "expect": "test_mean_pool_ignores_padding",
        "why": "padding 位的 hidden state **不是 0**（是 pad token 过完模型的结果），"
               "直接求平均就让向量随**批里最长的那条**变化：同一条文本与不同的邻居"
               "拼批得到不同向量，而 `F5-03` 的缓存整个建立在「同一文本 → 同一向量」上",
    },
    {
        "id": "f5-01",
        "label": "`token_type_ids` 用真 id 而不是全 0",
        "file": "src/localsearch/embed/gemma_onnx.py",
        "old": "                feed[meta.name] = np.zeros_like(ids)",
        "new": "                feed[meta.name] = ids",
        "tests": ["tests/unit/test_embed_onnx.py"],
        "expect": "test_token_type_ids_is_zero_filled_when_the_graph_wants_it",
        "why": "这个模型不用句对，标准答案是全 0。填成真 id 之后语法上完全合法，模型也"
               "多算不出错——只是把每条样本当成「两个句子」处理，向量静默变差",
    },
    {
        "id": "f5-01",
        "label": "交给 `encode()` 先归一化（截断之后就不是单位长度了）",
        "file": "src/localsearch/embed/gemma_st.py",
        "old": "            normalize_embeddings=False,",
        "new": "            normalize_embeddings=True,",
        "tests": ["tests/unit/test_embed_gemma_st.py"],
        "expect": "test_normalization_is_left_to_the_base_class",
        "why": "看着更「完整」的写法。归一化必须发生在按 `dim` 截断**之后**（`D5` 的 "
               "MRL），先归一化再截断得到的向量长度小于 1——余弦照样算得出来，只是"
               "尺度错了，而 HNSW 用内积当余弦的前提正是单位长度",
    },
    {
        "id": "f5-01",
        "label": "不再显式设 `max_seq_length`（读回 1e36）",
        "file": "src/localsearch/embed/gemma_st.py",
        "old": "    model.max_seq_length = cfg.max_seq",
        "new": "    pass",
        "tests": ["tests/unit/test_embed_gemma_st.py"],
        "expect": "test_real_max_seq_length_is_not_1e36",
        "why": "**这一条是 `F5-01` 那个意外发现的护栏**：这个模型的 `config.json` 把"
               "上限（262144）藏在 `text_config` 里，顶层没有 `max_position_embeddings`，"
               "于是 `SentenceTransformer` 读到的 `max_seq_length` 是 **1e36**——"
               "**它永远不会自己截断**。不设的话超长切片被整条喂进模型：慢、吃内存，"
               "而那一段的向量是把几十页正文混在一起的**平均**，语义上也没什么用。"
               "⚠ 这条要真权重（约 20 秒 / 3 GB），取不到时它自己 skip——那时这条变体"
               "会报「没红」，那不是假警报，是这台机器上确实没有护栏",
    },
    {
        "id": "f5-02",
        "label": "`W` 不再留保留额（把「当时可用」全算进预算）",
        "file": "src/localsearch/embed/batching.py",
        "old": "    usable = available - reserve",
        "new": "    usable = available",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_budget_is_arithmetic",
        "why": "`A2` 公式里那个「− 保留额」不是装饰：`E7` 记着本机可用内存两轮之间在 "
               "4–8 GiB 之间波动，而 P0 实测过「内存池按可用内存生长」——把当时的可用"
               "**全部**算进预算，等于把墙算到贴着墙，而墙会往后挪（`R1`：硬跑两次把"
               "机器拖到无响应）",
    },
    {
        "id": "f5-02",
        "label": "整除的结果可以掉到 0（`MIN_WORKERS` 那层兜底拿掉）",
        "file": "src/localsearch/embed/batching.py",
        "old": "    return max(MIN_WORKERS, usable // per_worker)",
        "new": "    return usable // per_worker",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_budget_never_below_one",
        "why": "算出 0 就是「一个都不跑」，而 Tier2 是**增强**不是前提（`D10` 分级就绪）"
               "——把一次内存紧张变成一次静默的无所作为。注意短路顺序：`usable <= 0` "
               "那条分支**挡不住**这一条（可用 3 GiB、保留 2 GiB、单 worker 4 GiB 时"
               "整除得 0，走的是这一行），所以测试里专门有这一组输入",
    },
    {
        "id": "f5-02",
        "label": "除法改成四舍五入（宁可多开一个）",
        "file": "src/localsearch/embed/batching.py",
        "old": "    return max(MIN_WORKERS, usable // per_worker)",
        "new": "    return max(MIN_WORKERS, (usable + per_worker - 1) // per_worker)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_budget_is_arithmetic",
        "why": "看着更「不浪费」：可用 5 GiB、保留 2 GiB、单 worker 2 GiB 时它开 2 个，"
               "而 3 ÷ 2 的分母摆在那里——多出来的那一个 worker 是拿「差一点放不下」"
               "换来的。这条变体就是那个 (5,2,2,1) 输入存在的理由",
    },
    {
        "id": "f5-02",
        "label": "`per_worker` 的守卫只挡 0",
        "file": "src/localsearch/embed/batching.py",
        "old": "    if per_worker <= 0:",
        "new": "    if per_worker == 0:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_budget_rejects_nonpositive_per_worker",
        "why": "负数会走进整除：Python 的 `//` 对负数是**向下**取整，于是「放不下」会被"
               "算成一个 1（或者一个荒谬的大数），而两种都不该被兜底成一个看起来正常的 W",
    },
    {
        "id": "f5-02",
        "label": "负的 `requested` 放过去（当成自动）",
        "file": "src/localsearch/embed/batching.py",
        "old": "    if requested < 0:",
        "new": "    if requested < -1:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_negative_request_is_rejected",
        "why": "配置层已经拦过一道（`_INT_MIN`），这里再拦一次是因为这个函数的调用方"
               "不止配置那一条路。‑1 个 worker 不是「更少的并行」，是一个没有意义的数",
    },
    {
        "id": "f5-02",
        "label": "忽略注入的可用内存，一律读本机",
        "file": "src/localsearch/embed/batching.py",
        "old": "    available = available_bytes() if available is None else available",
        "new": "    available = available_bytes()",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_auto_explains_its_arithmetic",
        "why": "四个输入可注入不是「好写测试」的便利，是**必须**：本机可用内存随浏览器"
               "开开关关而变（`E7`），不可注入就等于拿一台随机机器当测试环境。这条变体"
               "把注入堵死，于是所有算术测试一起红——它证明那些断言真的在拿注入值算",
    },
    {
        "id": "f5-02",
        "label": "自动模式不再按内存算，写死 1",
        "file": "src/localsearch/embed/batching.py",
        "old": "        workers = budget\n        notes.append(",
        "new": "        workers = 1\n        notes.append(",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_auto_explains_its_arithmetic",
        "why": "`workers = 0` 的语义是「这台机器放得下几个就开几个」（`A2` 重定口径时"
               "明确的「不按核数取」）。写死 1 在本机**看起来一样**（本机就是 1）——"
               "这正是那种只在一部分机器上错的写法，而 note 里的算式会跟着一起错",
    },
    {
        "id": "f5-02",
        "label": "被内存夹住时静默（不写 note）",
        "file": "src/localsearch/embed/batching.py",
        "old": "        if workers < requested:\n            notes.append(",
        "new": "        if False:\n            notes.append(",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_explicit_request_is_clamped_and_says_so",
        "why": "夹住（clamp）而不拒绝是一个判断（只改变快慢、不改变结果），但这个判断"
               "成立的**前提是要说出来**：用户写了 4、实际跑 1，不说就是「配置没生效"
               "却毫无提示」——与 `--tier 2` 被压低一级时那条规矩同一个道理",
    },
    {
        "id": "f5-02",
        "label": "夹住时不夹，照用户写的开",
        "file": "src/localsearch/embed/batching.py",
        "old": "        workers = min(requested, budget)",
        "new": "        workers = requested",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_explicit_request_is_clamped_and_says_so",
        "why": "内存这边是硬顶（`A2`：硬顶是内存不是核数）。照用户写的开就是「按配置"
               "把机器开爆」，而失败形态不是报错，是机器变慢或者进程被内存压力打掉",
    },
    {
        "id": "f5-02",
        "label": "总线程数超过核数时不提醒",
        "file": "src/localsearch/embed/batching.py",
        "old": "    if threads > 1 and workers * threads > cores:",
        "new": "    if False:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_threads_oversubscription_is_noted_but_not_changed",
        "why": "`[embed] threads` 与 `[perf] workers` 是**相乘**的（两个配置项的注释里"
               "都写着），超订之后多出来的线程大概率是负收益。这里只提醒不替用户改"
               "（`threads` 不在 `embed_ver` 里，但「多少个线程最快」是 `M4` 量出来的）",
    },
    {
        "id": "f5-02",
        "label": "`MIN_WORKERS` 抬到 2",
        "file": "src/localsearch/embed/batching.py",
        "old": "MIN_WORKERS = 1",
        "new": "MIN_WORKERS = 2",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_budget_never_below_one",
        "why": "「一个都放不下」的底线是**就地跑一个**，不是两个：把底线抬到 2 就等于"
               "在放不下的机器上照开两个（`A2` 的算术结果，本机就是这种情况）",
    },
    {
        "id": "f5-02",
        "label": "`iter_requests` 丢掉调用方给的起始序号",
        "file": "src/localsearch/embed/batching.py",
        "old": "        yield BatchRequest(index=start + ordinal, texts=tuple(batch), titles=picked)",
        "new": "        yield BatchRequest(index=ordinal, texts=tuple(batch), titles=picked)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_iter_requests_honours_start",
        "why": "序号唯一的作用是让并行算出来的结果**归位**（`F5-04` 按它写回切片）。"
               "第二段索引从 0 重来，症状是后一段的向量**覆盖**前一段的位置，"
               "而每一批的向量本身都完全正确",
    },
    {
        "id": "f5-02",
        "label": "标题不跟着切片走（每批都从第一条取）",
        "file": "src/localsearch/embed/batching.py",
        "old": "            else tuple(title_list[ordinal * size : ordinal * size + len(batch)])",
        "new": "            else tuple(title_list[: len(batch)])",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_iter_requests_titles_follow_the_slice",
        "why": "标题进的是文档前缀（`F5-01`），所以错位之后**向量会变**、而长度与形状"
               "全都正常：每条文本带上了**别人**的标题，搜出来看着还挺像那么回事",
    },
    {
        "id": "f5-02",
        "label": "`BatchRequest` 不再查 `titles` 与 `texts` 等长",
        "file": "src/localsearch/embed/batching.py",
        "old": "        if self.titles is not None and len(self.titles) != len(self.texts):",
        "new": "        if False:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_batch_request_rejects_mismatched_titles",
        "why": "等长的检查必须在**构造**时做：放进 `encode_documents` 的话，一路走到"
               "子进程里才报错，那时错的是一个已经被 pickling 过一次的批——而这一层"
               "的整个意义就是「错的东西构造不出来」",
    },
    {
        "id": "f5-02",
        "label": "worker 每算一批就重建一次嵌入器",
        "file": "src/localsearch/embed/batching.py",
        "old": '    embedder = _WORKER_STATE.get("embedder")\n    if embedder is None:',
        "new": "    embedder = None\n    if embedder is None:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_each_worker_builds_the_embedder_once",
        "why": "「一个进程一个模型」不是优化：每个 worker 的常驻内存**就是**那份模型"
               "（`E41`：7.17 GiB），而本机放不下的正是它；加上每次重建的冷启动"
               "（`E40`：0.5–3.6 秒），批越小亏得越多",
    },
    {
        "id": "f5-02",
        "label": "子进程报父进程的 pid",
        "file": "src/localsearch/embed/batching.py",
        "old": "        vectors=vectors,\n        worker_pid=os.getpid(),",
        "new": "        vectors=vectors,\n        worker_pid=0,",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_two_workers_use_two_processes",
        "why": "`worker_pid` 是这一层唯一「证明结果真的来自另一个进程」的东西——"
               "它错了，测试就没法区分「起了子进程」与「悄悄退回就地跑」，而后者在"
               "本机（`W == 1`）是真的会发生的那条路",
    },
    {
        "id": "f5-02",
        "label": "子进程不再自报峰值内存",
        "file": "src/localsearch/embed/batching.py",
        "old": "        worker_pid=os.getpid(),\n        peak_bytes=peak_rss_bytes(),",
        "new": "        worker_pid=os.getpid(),\n        peak_bytes=None,",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_two_workers_use_two_processes",
        "why": "`PER_WORKER_RSS_BYTES` 是先验、会偏（`E41`：同一配置下 1.81 与 7.17 GiB "
               "差四倍），自报峰值是唯一的**观测值**——它没了，先验就永远不会被回报修正",
    },
    {
        "id": "f5-02",
        "label": "就地跑时每批重建嵌入器",
        "file": "src/localsearch/embed/batching.py",
        "old": "            if self._local is None:\n                self._local = self.factory(self.cfg)",
        "new": "            if True:\n                self._local = self.factory(self.cfg)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_single_worker_builds_one_embedder_for_many_batches",
        "why": "与子进程那条是**同一件事的两条路**（本机走的是这一条）：`W == 1` 不是"
               "「凑合一下」，它要交的东西与子进程那条一模一样——一个进程一个模型",
    },
    {
        "id": "f5-02",
        "label": "`close()` 不放掉就地那个嵌入器",
        "file": "src/localsearch/embed/batching.py",
        "old": "        if self._local is not None:\n            self._local.close()\n"
               "            self._local = None",
        "new": "        if self._local is not None:\n            self._local = None",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_close_closes_the_local_embedder",
        "why": "引用丢掉不等于放掉：那一份是 7 GiB 量级的常驻（`E41`），而 `close()` "
               "存在的理由就是「一段算完就把模型放掉」（P0 的 `release_models()` 那一条）",
    },
    {
        "id": "f5-02",
        "label": "正常跑完就把池收摊（一趟一块，用完即废）",
        "file": "src/localsearch/embed/batching.py",
        "old": "                if not pending:\n                    return",
        "new": "                if not pending:\n                    self.close()\n                    return",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_a_clean_run_leaves_the_pool_reusable",
        "why": "一趟索引里会有好几段（`F5-04` 边算边写库、按段提交），每段都把模型重新"
               "加载一遍等于把冷启动乘以段数（`E40`：0.5–3.6 秒/次）。**收摊只该发生在"
               "出错与提前退出时**——那两种情形下留着子进程才是漏（它们在 Windows 上"
               "会一直占着内存到解释器退出）",
    },
    {
        "id": "f5-02",
        "label": "出错时不再收摊（子进程留着）",
        "file": "src/localsearch/embed/batching.py",
        "old": "        except BaseException:\n"
               "            # 生成器被提前关掉（`GeneratorExit`）与任何异常都走这里：把进程池收掉，\n"
               "            # 别留下一堆没人管的子进程（它们在 Windows 上会一直占着内存直到解释器退出）。\n"
               "            self.close()\n"
               "            raise",
        "new": "        except BaseException:\n"
               "            # 变体：出错时不再收摊\n"
               "            raise",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_break_early_closes_the_pool",
        "why": "调用方 `break`（或者 `GeneratorExit`）是最容易被忘掉的一条退出路径："
               "异常路径人总会想起 `finally`，而「for 到一半不看了」不会——"
               "留下的是一堆没人回收的子进程，每个都占着 7 GiB 量级",
    },
    {
        "id": "f5-02",
        "label": "退避只接 `MemoryError`，不接进程池被打死",
        "file": "src/localsearch/embed/batching.py",
        "old": "_BACKOFF_ERRORS = (MemoryError, BrokenProcessPool)",
        "new": "_BACKOFF_ERRORS = (MemoryError,)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_broken_process_pool_halves_workers_and_retries",
        "why": "内存压力打掉一个 worker 时，父进程看到的是 `BrokenProcessPool` 而**不是**"
               "`MemoryError`（P0 实测过 onnxruntime `EXIT=139`、没有 traceback）。只接"
               "前者的退避等于在最需要它的那条路上不生效",
    },
    {
        "id": "f5-02",
        "label": "把 `EmbedUnavailable` 也算进退避（该停的时候接着算）",
        "file": "src/localsearch/embed/batching.py",
        # ⚠ 这里**不能**改 `_BACKOFF_ERRORS` 那一行去加 `EmbedUnavailable`：那个名字在
        # `batching.py` 里**没有被导入**（只在文档串里出现过），加进去是 `NameError`，
        # 整个模块导入不了、pytest 退出码 2——**收集期出错会被误判成"红了"**，而那
        # 什么都验不到（2026-10-16 实测踩到，工具自己也拦了：它要求退出码必须是 1）。
        # 改在 `except` 这一行，用**已经导入**的 `EmbedError`：`EmbedUnavailable` 是它的
        # 子类，所以这条改动一样能把"该停的没停"复现出来。
        "old": "                    except _BACKOFF_ERRORS as exc:",
        "new": "                    except _BACKOFF_ERRORS + (EmbedError,) as exc:",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_unavailable_aborts_the_whole_run",
        "why": "`EmbedUnavailable` 是**环境**问题（权重不在本机、快照缺件），`base.py` "
               "的文档写着调用方应当**立刻停下整趟索引**。把它当退避处理，结果是"
               "「权重不在」这件事被翻译成 `EmbedError`（连一个 worker 都放不下）——"
               "一次环境错误变成一次内存诊断，而且两万个文件逐个再试一遍",
    },
    {
        "id": "f5-02",
        "label": "退避不减半",
        "file": "src/localsearch/embed/batching.py",
        "old": "        self.workers = max(MIN_WORKERS, self.workers // 2)",
        "new": "        self.workers = max(MIN_WORKERS, self.workers)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_memory_error_halves_workers_and_retries",
        "why": "退避的全部内容就是「少开几个」：不减半的话重建出来的池与原来一模一样，"
               "于是下一批大概率再撞一次同一堵墙——而 note 还在说「已经减半」",
    },
    {
        "id": "f5-02",
        "label": "退避时把这一批重投两遍（重复交付）",
        "file": "src/localsearch/embed/batching.py",
        "old": "                        retry.append(request)\n"
               "                        self._shrink(exc)",
        "new": "                        retry.extend([request, request])\n"
               "                        self._shrink(exc)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_memory_error_halves_workers_and_retries",
        "why": "「不会重复交付」是退避的硬约束（模块文档第三节）：结果只从 future 里取"
               "出来一次就 `yield` 一次，重复交出去在 `F5-04` 那一侧就是同一批向量写两遍",
    },
    {
        "id": "f5-02",
        "label": "重投的批不再提交（丢掉）",
        "file": "src/localsearch/embed/batching.py",
        "old": "                for one in retry:\n                    pending[self._submit(one)] = one",
        "new": "                for one in retry:\n                    pass",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_memory_error_halves_workers_and_retries",
        "why": "退避的承诺是「减半，但这一批不会丢」。丢掉的话那一批的切片就永远没有"
               "向量——`F5-04` 写库时表现为「少了一部分」，而整趟索引会报告成功",
    },
    {
        "id": "f5-02",
        "label": "子进程里不再把 `titles` 交给嵌入器",
        "file": "src/localsearch/embed/batching.py",
        "old": "    vectors = embedder.encode_documents(request.texts, request.titles)",
        "new": "    vectors = embedder.encode_documents(request.texts)",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_two_workers_use_two_processes",
        "why": "`BatchRequest` 把标题一路带到子进程，就是为了这一行。丢了之后向量照样"
               "是单位长度的、形状也对——只是每一条文档都按「无标题」算，"
               "`F5-01` 那条前缀里唯一的文件名信息静默消失",
    },
    {
        "id": "f5-02",
        "label": "单 worker 的先验值被写小三位（`W` 于是开爆）",
        "file": "src/localsearch/embed/batching.py",
        "old": "PER_WORKER_RSS_BYTES = 7_700_000_000",
        "new": "PER_WORKER_RSS_BYTES = 77_000_000",
        "tests": ["tests/unit/test_batching.py"],
        "expect": "test_prior_is_at_least_a_few_gib",
        "why": "先验值准不准，测试断不出来（那是 `E41` 的读数，要复跑 "
               "`tools/measure_worker_rss.py` 才知道）；能断的是**量级**。"
               "而量级写错恰好是最坏的那一类：本机 `W` 从 1 变成 19，"
               "而配置、日志、`notes` 里那个算式看起来全都正常——只有机器会出事",
    },
    {
        "id": "f5-02",
        "label": "`[perf] workers` 的下界又变回 1（0 不再当自动）",
        "file": "src/localsearch/core/config.py",
        "old": '    ("perf", "workers"): 0,',
        "new": '    ("perf", "workers"): 1,',
        "tests": ["tests/unit/test_config.py"],
        "expect": "test_zero_workers_means_auto",
        "why": "0（自动，按内存算）与 1（就地跑一个）在这个配置项里是**两件不同的事**。"
               "下界回到 1 之后，`config/default.toml` 里那个 0 会被校验判成非法——"
               "模板自己跑不起来，而这件事只在真正加载模板时才现形",
    },
]


def _git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip()


def _index_blob(relative: str) -> bytes:
    """索引里那份内容（`git cat-file blob :<路径>`）。

    按**内容**判干净要用它——`git status` 会漏，理由见 `_check_preconditions`。
    """
    result = subprocess.run(["git", "cat-file", "blob", f":{relative}"], cwd=ROOT,
                            capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"读不到索引里的 {relative}（{detail}）——它还没被 git 跟踪？")
    return result.stdout


def _decode(raw: bytes) -> str:
    """字节 → 文本，行尾统一成 `\\n`。

    统一在**入口**做一次，后面 `old` / `new` 那些字面量就只用管 `\\n`；还原那
    一步不走这条路（直接写回字节），所以行尾不会被这一步改掉。
    """
    return raw.decode("utf-8").replace("\r\n", "\n")


def _check_preconditions(mutation: dict) -> tuple[pathlib.Path, bytes]:
    """拒绝在脏文件上动手，并确认那处改动匹配得上、且只匹配一处。

    ⚠ **干净要判两次**：`git status` 先比索引里记下的 stat（大小 / 修改时间），
    对得上就不再读内容，所以它**有看不见的脏**。2026-10-16 实测到一条能踩进去
    的路径：`media_meta.py` 工作区是 CRLF、索引里是 LF，差 617 字节，而
    `git status --porcelain` 报的是**空**（同一个仓库里 `sliding.py` 同样是
    CRLF 却报 `M`）。成因是"索引里记的 stat 恰好等于工作区现在的 stat"——它是
    状态依赖的，不是这个文件的固有属性，所以靠它做的检查今天拦得住、明天拦不住。
    再加的这一道按**内容**比（行尾归一化之后），就跟 stat 无关了。
    """
    relative = str(mutation["file"])
    path = ROOT / relative
    raw = path.read_bytes()

    dirty = _git(["status", "--porcelain", "--", relative])
    if dirty:
        raise SystemExit(
            f"拒绝跑：{relative} 有未提交的改动（{dirty!r}）。\n"
            "这个工具会把源码改坏几秒钟再还原，文件本来就不干净的话分不清哪些是我的。\n"
            "先提交或 stash 再跑。"
        )
    if raw.replace(b"\r\n", b"\n") != _index_blob(relative).replace(b"\r\n", b"\n"):
        raise SystemExit(
            f"拒绝跑：{relative} 的内容和索引里的那份不一样，但 `git status` 说它干净。\n"
            "这正是这道检查存在的理由（stat 缓存会让 status 漏掉内容差异）。\n"
            f"先看一眼 `git diff -- {relative}`，确认那些改动是你要的再提交。"
        )

    source = _decode(raw)
    hits = source.count(str(mutation["old"]))
    if hits != 1:
        raise SystemExit(
            f"变体过期：{mutation['label']} 在 {mutation['file']} 里匹配到 {hits} 处"
            "（必须是 1）。\n"
            "匹配 0 处说明那句代码已经被改过形——**这条变体验的东西可能已经不在了**，"
            "要重新想一条；匹配多处说明它改不准。两种都不许「尽力而为」。"
        )

    # ⚠ **点名的测试必须真的叫这个名字**。2026-10-16 实测到这条：新写的 `console`
    # 变体把 `expect` 写成 `test_cli_entry_calls_the_hatch`（真名多一个 `the_`），
    # 于是"改坏之后那条测试没红"——**报告说的是"这里没有护栏"，而真相是我把名字写
    # 错了**。这两种情形在红名单上看不出区别（红名单里躺着的正是那条测试），只有
    # 判定的 `matched` 是空的。它比"没红"更隐蔽：真没护栏会让人去补测试，写错名字
    # 会让人以为那条测试是假的。所以按 `def` 扫一遍，名字对不上就当场拒绝。
    defined: set[str] = set()
    for one in mutation["tests"]:
        defined |= set(re.findall(r"^\s*def\s+(\w+)",
                                  (ROOT / one).read_text(encoding="utf-8"), re.M))
    expect = str(mutation["expect"])
    if expect not in defined:
        near = sorted(name for name in defined if expect.split("_")[-1] in name)
        raise SystemExit(
            f"变体点名错了：`{expect}` 在 {'、'.join(str(o) for o in mutation['tests'])} "
            f"里没有这个 `def`（{len(defined)} 个测试函数里找不着）。\n"
            + (f"是不是想写：{'、'.join(near)}？\n" if near else "")
            + "⚠ 这一条**不是**「没护栏」：名字写错时那条测试照样会红，红名单里也看得见"
              "它，只有判定匹配不上。先改名字，别去补测试。"
        )
    return path, raw


def _run(mutation: dict) -> dict:
    path, raw = _check_preconditions(mutation)
    source = _decode(raw)
    mutated = source.replace(str(mutation["old"]), str(mutation["new"]), 1)

    #: 写回去用**文件自己的**行尾，不按平台（`Path.write_text` 的 `newline=None`
    #: 会在 Windows 上把整篇换成 CRLF，见文件头那段 ⚠）。
    newline = "\r\n" if b"\r\n" in raw else "\n"

    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--no-header", *[str(one) for one in mutation["tests"]]]
    try:
        path.write_bytes(mutated.replace("\n", newline).encode("utf-8"))
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    finally:
        # 还原写的是**原来的字节**，不是"把读出来的文本再编一遍"——那样每跑一趟
        # 都要经过一次编解码，行尾就是在这条路上被改掉的。
        path.write_bytes(raw)

    if path.read_bytes() != raw:
        raise SystemExit(f"还原失败：{mutation['file']} 与原文不一致，请 git checkout 回来")

    failed = sorted(
        line.split("::")[-1].split()[0]
        for line in result.stdout.splitlines() if line.startswith("FAILED")
    )
    # ⚠ 点名测试可能是**参数化**的，pytest 报的是 `名字[S--]`、`名字[W--]`。
    # 第一版这里写的是 `str(...) in failed`（精确相等），于是"南纬西经"那条变体
    # 明明红了 2 条却被判成**没红**——工具自己的假阴性，2026-10-16 首跑当场抓到。
    # 所以按"名字本身"或"名字[参数]"两种形态认。
    expect = str(mutation["expect"])
    matched = [one for one in failed if one == expect or one.startswith(f"{expect}[")]

    summary = [line for line in result.stdout.splitlines()
               if "passed" in line or "failed" in line or "error" in line]
    return {
        "label": mutation["label"],
        "why": mutation["why"],
        "expect": expect,
        "exit_code": result.returncode,
        "failed": failed,
        "matched": matched,
        "summary": summary[-1].strip() if summary else "（没有读到 pytest 的汇总行）",
        # 退出码 2 是收集期出错：那时所有测试一起红，看着像护栏其实什么都没验
        "ok": result.returncode == 1 and bool(matched),
        "collection_error": result.returncode not in (0, 1),
    }


#: `--self-test` 用的那条变体：**故意无效**的一处改动（加一句注释）。
#:
#: 为什么工具自己也要一条：它的失败形态是**静默的假绿**——判定逻辑一坏（第一版
#: 就坏过一次，见 `_run` 里那段注释），它会对每一条变体都印"红"，然后照样印
#: "5/5 条变体全部被抓到"、退出码 0。**那正是这个工具用来防别人的东西。**
#: 喂它一条不生效的改动，它必须回"这条没红"——回不了就说明它自己的判定坏了。
SELF_TEST: dict[str, object] = {
    "id": "self-test",
    "label": "故意无效的改动（只加一句注释）——它**必须**被判成「没红」",
    "file": "src/localsearch/extract/media_meta.py",
    "old": "        self._config = config",
    "new": "        self._config = config  # self-test: 这行改动不改变任何行为",
    "tests": ["tests/unit/test_extract_media_meta.py"],
    "expect": "test_a_bare_png_is_an_empty_doc_with_a_warning",
    "why": "喂给判定逻辑一条不生效的改动，它必须回「没红」；"
           "回「红」就说明判定坏了，那么它报的每一个「红」都不可信",
}


def _self_test() -> int:
    print("自检：喂一条**故意无效**的改动（只加一句注释），它必须被判成「没红」")
    print("-" * 78)
    outcome = _run(SELF_TEST)
    print(f"    结果：pytest 退出码 {outcome['exit_code']}  {outcome['summary']}")
    print(f"    红的测试：{outcome['failed'] or '（一条都没红）'}")
    if outcome["exit_code"] != 0:
        print("    ⚠ 退出码不是 0：一条不生效的改动不该让任何测试红，"
              "先查这个工具跑的那个测试文件本身是不是绿的")
        return 1
    if outcome["ok"]:
        print("    ⚠ 判定坏了：这条改动不改变任何行为，却被判成「红」——"
              "那么它对真变体报的「红」也都不算数（点名测试没匹配上时最容易这样）")
        return 1
    print("    判定正常：不生效的改动被判成「没红」。")
    print("-" * 78)
    print("⚠ 自检只证明「判定逻辑能把没红的认出来」；")
    print("  真变体的那一轮请照常跑（不带 --self-test）。")
    return 0


def main() -> int:
    # 输出面兜底（core/console.py）：管道 / 重定向下不许因多字节字符崩掉
    use_safe_output()
    parser = argparse.ArgumentParser(description="变体检查：改坏一处代码，点名的测试必须红")
    parser.add_argument("--only", help="只跑这个 id 的变体（默认全跑）")
    parser.add_argument("--list", action="store_true", help="只列出变体，不改任何文件")
    parser.add_argument("--self-test", action="store_true",
                        help="只跑自检：喂一条故意无效的改动，它必须被判成「没红」")
    options = parser.parse_args()

    if options.self_test:
        return _self_test()

    chosen = [one for one in MUTATIONS if not options.only or one["id"] == options.only]
    if not chosen:
        print(f"没有 id 为 {options.only!r} 的变体")
        return 2
    if options.list:
        for one in chosen:
            print(f"[{one['id']}] {one['label']}\n    点名测试：{one['expect']}"
                  f"\n    理由：{one['why']}")
        return 0

    print(f"仓库根：{ROOT}")
    print(f"变体 {len(chosen)} 条；每条都要：改坏 → pytest 退出码 1 → 点名的那条测试在红名单里")
    print("=" * 78)

    survived: list[dict] = []
    for one in chosen:
        outcome = _run(one)
        mark = "红" if outcome["ok"] else "★没红★"
        print(f"[{one['id']}] {outcome['label']}\n"
              f"    点名测试：{outcome['expect']}\n"
              f"    结果：{mark}  pytest 退出码 {outcome['exit_code']}  {outcome['summary']}")
        if outcome["failed"]:
            print(f"    红的测试（{len(outcome['failed'])} 条）："
                  f"{', '.join(outcome['failed'][:6])}"
                  f"{' …' if len(outcome['failed']) > 6 else ''}")
        if outcome["collection_error"]:
            print("    ⚠ 退出码不是 0/1：这是**收集期就出错**，所有测试一起红不算护栏")
        if not outcome["ok"]:
            survived.append(outcome)
            print(f"    为什么要有这一条：{outcome['why']}")
        print("-" * 78)

    if survived:
        print(f"⚠ {len(survived)}/{len(chosen)} 条变体**活下来了**（改坏了却没有测试红）：")
        for one in survived:
            print(f"    - {one['label']}")
        print("这正是这个工具存在的理由：那些地方现在是**没有护栏**的。")
        return 1

    print(f"{len(chosen)}/{len(chosen)} 条变体全部被抓到——每一条都有测试盯着它。")
    print("⚠ 这只说明「这些测试不是摆设」，不说明「实现是对的」（见文件头）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
