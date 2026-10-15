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
