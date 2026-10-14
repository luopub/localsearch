# P2 / `F2-01`：D:/work 全盘试跑（扫描器真机实测）

**这是一次「脚本化实测」，不是单测**——性能测量在单测里做不出可信数字。按项目规矩：脚本入库、原始输出落盘、数字连条件一起记。

| 项 | 值 |
|---|---|
| 功能点 | `F2-01`（`os.scandir` 扫描后端），关联验收项 **A1**（全量索引不得有未捕获异常） |
| 代码版本 | `cd9cd6e`。⚠ **跑的时刻（00:47）这五个文件还没提交**（提交于 00:51:41），所以严格说是"跑在未提交的工作区上"——但**内容与该提交逐字节相同**（跑完到提交之间只写了提交消息，没动过代码）。 |
| 机器时间 | `2026-10-15 00:47:00`（脚本自己打印的时间） |
| 复跑命令 | `PYTHONIOENCODING=utf-8 uv run python tools/scan_report.py D:/work --limit 30` |
| 环境 | Windows 11 Home China 10.0.26300；C: 与 D: 是**同一块 Samsung SSD 990 EVO Plus 2TB 上的两个分区**（总线类型实测见 §3 的 E13） |

---

## 一、先看这几条

1. **0 条错误、0 个 reparse point**（`D:/work` 上没有 junction/symlink），整轮没有任何未捕获异常——**`A1` 在扫描这一层成立**。注意这与 C: 是两个世界：C: 上有 **354 个**权限被拒的目录、**422 个** reparse point（E11）。**"在 D:/work 上跑得干净"不能外推成"全盘跑得干净"**，错误隔离那几条测试用的是合成目录树，不是 C:。
2. 扫完 **1,028,677 条目**（目录 47,754 + 文件 980,923）用 **23.28 s**，即 **265 万条目/分钟**，比 E11 的 C: 全盘平均（169 万条目/分钟）**快 1.57×**。原因不是代码不同，是**构成不同**：`D:/work` 的目录只占 4.6%（C: 是 18.9%），而目录比文件贵（每个目录都要开一次 `os.scandir`）。
3. **真正要提正文的只有 234,896 个文件 / 22.91 GiB**，另外 744,562 个只记名字、1,465 个被排除。→ 这是 P4/P5 的输入量级：**22.91 GiB 要过提取与嵌入**。
4. **`*.zip` 一项就是 17.6 GiB**（315 个文件），`*.rar` 3.0 GiB、`*.png` 2.8 GiB——**name-only 这几条挡掉的是字节数的大头**；如果它们改成 `skip`，正文量级不会变，但用户搜不到这些文件名。
5. ⚠ **缓存没做对照实验，所以这一栏不能当基准。** 同一天更早（`2026-10-15 00:36`）一次**未入库的临时脚本**对同一个目录量到 **42.84 s**，与本轮 23.28 s 相差 **1.84×**；两次的条目计数也只差 2（见下条）。**最可能是文件系统缓存（先跑的那次偏冷）**——但这是推断，**没有实测确认**，故两个数都记下来，谁也不当"标准值"。
6. ⚠ **目录是活的。** 两轮之间文件数 **+2**（980,921 → 980,923）、正文文件数 **+2**（234,894 → 234,896），增量就是本次工作新加的工具与测试。**`D:/work` 的两次试跑计数不该逐位相等**，比较时只看量级与构成。

---

## 二、原始输出

```text
扫描根        D:/work
排除规则      D:\work\test\localsearch\.lsearchignore（284 条）
机器时间      2026-10-15 00:47:00

账目
    根目录        1 / 1 可达
    条目总数      1,028,677（目录 47,754 + 文件 980,923）
    进入的目录    46,167（'看见的目录 − 剪掉的' + 根目录数）
    保留的目录    46,166（剪掉 1,588 / reparse 0）
    文件：正文    234,896（22.91 GiB）
          仅名字  744,562
          排除    1,465
          超限    29（已计入「仅名字」）
    事件时间      23.28 s（含回调；含 IO 等待）
    读不到的      0 条（样本最多 0）

被排除掉的（44 条，命中 > 0 的）
          目录        文件         大小   归属 / 例子
           0       988     224.9M   [规则] skip 第 201 行  '*.a'
        例：D:/work/parttime/geyu-xr7.5-surveillance-7714643320/phase-2/stream-server-test/11/Analyzer/3rdparty/ffmpeg/x64/lib/libavdevice.dll.a
         541         0       0.0M   [规则] skip 第 90 行  '__pycache__/'
        例：D:/work/test/yolov5/__pycache__
         282         0       0.0M   [规则] skip 第 61 行  'node_modules/'
        例：D:/work/test/vercel-labs/skills/node_modules
         255         0       0.0M   [规则] skip 第 50 行  '.git/'
        例：D:/work/开课吧/资金流入流出预测/第六章强化学习与推荐系统/L14/PyGame-Learning-Environment/.git
           0       148      22.0M   [规则] skip 第 199 行  '*.o'
        例：D:/work/sxn-104/STM32 开发板 ARM工控板 核心板 STM32F103C8T6 带 2路 RS485 CAN/STM32F103C8T6+双RS485/STM32F103C8T6工控板资料/程序资料/资料包控制芯片STM32F103C8T6/MINI板资料包控制芯片STM32F103C8T6/MINI板实验代码/高级例程-基于两个MINI板的RS485通讯/Output/core_cm3.o
           0       127       0.2M   [规则] skip 第 168 行  '*.orig'
        例：D:/work/test/awtk/design/default/ui/main.xml.orig
         114         0       0.0M   [规则] skip 第 54 行  '.idea/'
        例：D:/work/开课吧/资金流入流出预测/第六章强化学习与推荐系统/L14/.idea
          64         0       0.0M   [规则] skip 第 188 行  'dist/'
        例：D:/work/test/vercel-labs/skills/dist
          58         0       0.0M   [规则] skip 第 190 行  'bin/'
        例：D:/work/大数据/source code/kafka-2.7.0-src/bin
          57         0       0.0M   [规则] skip 第 187 行  'build/'
        例：D:/work/zstack/build
           0        49       1.5M   [规则] skip 第 166 行  '*.bak'
        例：D:/work/test/smolagents-old/examples/text_to_sql.py.bak
           0        41       0.0M   [规则] skip 第 163 行  '~$*'
        例：D:/work/PycharmProjects/temu_spiders/~$product_sku_sell_status-20260921T193648.xlsx
          36         0       0.0M   [规则] skip 第 192 行  'Debug/'
        例：D:/work/zstack/test/src/test/java/org/zstack/test/core/debug
           0        31       5.9M   [规则] skip 第 202 行  '*.lib'
        例：D:/work/parttime/geyu-xr7.5-surveillance-7714643320/phase-2/stream-server-test/11/Analyzer/3rdparty/tbb/lib/tbb.lib
          28         0       0.0M   [规则] skip 第 193 行  'Release/'
        例：D:/work/test/opencode/packages/desktop/src-tauri/release
          26         0       0.0M   [规则] skip 第 123 行  'Cache/'
        例：D:/work/大数据/source code/kafka-2.7.0-src/jmh-benchmarks/src/main/java/org/apache/kafka/jmh/cache
          23         0       0.0M   [规则] skip 第 74 行  'vendor/'
        例：D:/work/test/openclaw/vendor
           0        19       0.0M   [规则] skip 第 196 行  '*.pyc'
        例：D:/work/parttime/yanghai-20230705-django/bookstore/DjangoUeditor/manage.pyc
          18         0       0.0M   [规则] skip 第 115 行  '*.egg-info/'
        例：D:/work/开课吧/资金流入流出预测/第六章强化学习与推荐系统/L14/PyGame-Learning-Environment/ple.egg-info
           0        18       0.7M   [规则] skip 第 177 行  '.DS_Store'
        例：D:/work/test/deepface-test/QMUL-SurvFace-v1/QMUL-SurvFace/Face_Verification_Test_Set/verification_images/.DS_Store
           0        15       0.0M   [规则] skip 第 198 行  '*.class'
        例：D:/work/PycharmProjects/nc19spiders/rl-learning/java/mdp/VIcore.class
           0        15       0.0M   [规则] skip 第 209 行  '*.la'
        例：D:/work/lvgl/SDL2-devel-2.28.0-mingw.tar/SDL2-2.28.0/x86_64-w64-mingw32/lib/libSDL2.la
          11         0       0.0M   [规则] skip 第 189 行  'out/'
        例：D:/work/test/electron/my-electron-app/out
          10         0       0.0M   [规则] skip 第 67 行  '.venv/'
        例：D:/work/test/video-generator/.venv
          10         0       0.0M   [规则] skip 第 191 行  'obj/'
        例：D:/work/test/chrome-extensions-samples/_archive/apps/samples/webgl-pointer-lock/obj
           0        10      21.5M   [规则] skip 第 181 行  '*.download'
        例：D:/work/parttime/lingchuan-50678-20220507/exam-20220512/exam-paper-20220512/paper/virtualassetfront_files/app.js.download
           9         0       0.0M   [规则] skip 第 55 行  '.vs/'
        例：D:/work/test/ConsoleApplication1/.vs
           9         0       0.0M   [规则] skip 第 71 行  'site-packages/'
        例：D:/work/PycharmProjects/amazon-spiders/amazon-reviews-spider-all-in-one/programs-python/Lib/site-packages
           8         0       0.0M   [规则] skip 第 121 行  'tmp/'
        例：D:/work/test/pocoerp/tmp
           7         0       0.0M   [规则] skip 第 68 行  'venv/'
        例：D:/work/test/product_image_creator3/venv
    …另有 14 条，合计 26 条

只记名字的（67 条，命中 > 0 的）
          目录        文件         大小   归属 / 例子
           2    680447    5323.4M   [规则] name-only 第 269 行  '*.jpg'
        例：D:/work/秦臻账目/微信图片_20210429112226.jpg
           6     26800     156.2M   [规则] name-only 第 326 行  '*.ts'
        例：D:/work/test/wolframalpha-llm-mcp/src/index.ts
           0     21030    2777.5M   [规则] name-only 第 271 行  '*.png'
        例：D:/work/wish账号绑定微信.png
           0     10749    1260.4M   [规则] name-only 第 306 行  '*.mp3'
        例：D:/work/test/voice-clone/output_150100836.mp3
           0      1812     286.1M   [规则] name-only 第 270 行  '*.jpeg'
        例：D:/work/test/ui-tars-desktop/multimodal/websites/docs/docs/public/hangzhou-weather.jpeg
          26       527      10.5M   [规则之外] —  '无规则命中；继承上级的「只索引名字」'
        例：D:/work/test/awesome-design-md/design-md/x.ai/DESIGN.md
           0       469     242.8M   [规则] name-only 第 272 行  '*.gif'
        例：D:/work/开课吧/资金流入流出预测/第六章强化学习与推荐系统/L14/PyGame-Learning-Environment/docs/_static/catcher.gif
           0       332     358.6M   [规则] name-only 第 229 行  '*.bin'
        例：D:/work/test/easyaiot/.scripts/minio/export-bucket/exports/model_3/openvino/model_openvino_model/model.bin
           0       315   17580.5M   [规则] name-only 第 245 行  '*.zip'
        例：D:/work/nc19spiders-essentials.zip
           0       309    1518.4M   [规则] name-only 第 218 行  '*.exe'
        例：D:/work/串口调试助手/UartAssist.exe
           0       280     208.8M   [规则] name-only 第 332 行  '*.ttf'
        例：D:/work/test/video-generator/OpenMontage/ink-theater/examples/mocap-figure/assets/patrickhand.ttf
           0       275     270.0M   [规则] name-only 第 219 行  '*.dll'
        例：D:/work/test/pywinauto/apps/WinForms_samples/ListViewEx.dll
           0       161      97.1M   [规则] name-only 第 335 行  '*.woff'
        例：D:/work/test/nc19/static/rest_framework/fonts/fontawesome-webfont.woff
           0       139      90.1M   [规则] name-only 第 273 行  '*.bmp'
        例：D:/work/test/opencode/packages/desktop/src-tauri/assets/nsis-header.bmp
           0        99       2.8M   [规则] name-only 第 280 行  '*.ico'
        例：D:/work/test/ui-tars-desktop/multimodal/websites/tarko/docs/public/favicon.ico
           0        80      24.7M   [规则] name-only 第 336 行  '*.woff2'
        例：D:/work/test/video-generator/OpenMontage/.agents/skills/music-to-video/references/templates/logo-split-lockup-pulse/assets/fredoka-700.woff2
           0        77    1517.0M   [规则] name-only 第 249 行  '*.gz'
        例：D:/work/zstack/conf/tools/ansible-1.8.2.tar.gz
           0        72    1614.8M   [规则] name-only 第 316 行  '*.mp4'
        例：D:/work/test/video-generator/outputs/画板例子-亚马逊宣传视频-16x9-20s.mp4
           0        60      33.4M   [规则] name-only 第 362 行  '*.xls'
        例：D:/work/开课吧/资金流入流出预测/第三章资金流入流出预测/自测文档-资金流入流出预测.xls
           0        59       0.9M   [规则] name-only 第 274 行  '*.webp'
        例：D:/work/tools/cc-blender-skill/plugin/skills/text-to-blender/assets/v1.2.0-validation/01_broadcaster_too_bright.webp
           0        51       0.2M   [规则] name-only 第 279 行  '*.avif'
        例：D:/work/test/video-generator/OpenMontage/.agents/skills/hyperframes-animation/examples/assets/brands/github.avif
           0        49     429.4M   [规则] name-only 第 230 行  '*.dat'
        例：D:/work/test/awtk/res/assets/default/raw/data/gpinyin.dat
           0        45       0.2M   [规则] name-only 第 309 行  '*.aac'
        例：D:/work/test/opencode/packages/ui/src/assets/audio/alert-01.aac
           0        35    3043.3M   [规则] name-only 第 246 行  '*.rar'
        例：D:/work/开课吧/山顶会/512行业报告.rar
           0        34       2.7M   [规则] name-only 第 337 行  '*.eot'
        例：D:/work/test/nc19/static/rest_framework/fonts/fontawesome-webfont.eot
           0        25      18.0M   [规则] name-only 第 233 行  '*.jar'
        例：D:/work/zstack/test/ext-libs/groovy-sandbox-1.27-SNAPSHOT.jar
           0        21      65.0M   [规则] name-only 第 289 行  '*.psd'
        例：D:/work/yes24-buyticket/pictures/yes24-alreadydated.psd
           0        18       7.4M   [规则] name-only 第 307 行  '*.wav'
        例：D:/work/开课吧/资金流入流出预测/第六章强化学习与推荐系统/L14/FlappyBird_QLearning/assets/audio/die.wav
           1        16     122.0M   [规则] name-only 第 248 行  '*.tar'
        例：D:/work/test/openclaw/test/fixtures/hooks-install/tar-evil-id.tar
           0        15      28.3M   [规则] name-only 第 346 行  '*.sqlite3'
        例：D:/work/test/crewAI-examples/notebooks/Simple QA Crew + Flow/db/chroma.sqlite3
    …另有 37 条，合计 166 条
```

---

## 三、这次分布直接指向的三件事

1. **`*.ts`（规则文件第 326 行，`name-only`）命中了 26,800 个 TypeScript 源文件 / 156.2 MiB。**
   TS 源码是**有正文检索价值的文本**，把它们整类挡在正文之外，是规则文件写得过粗——这一条大概率该改成 `skip` 只对编译产物（`*.d.ts`? 不是）或干脆保留但不该排这么前。**这是规则文件的问题，不是扫描器的问题**；要不要改是用户的决定（`.lsearchignore` 是用户可改的唯一来源）。同类还有 `*.tsx` 是否也在里面，本次没查。
2. **文件扩展名规则会命中目录**：`*.tar` 名下记着 **1 个目录**、`*.jpg` 名下记着 **2 个目录**（目录名以那串字符结尾），于是那两处**整棵子树**被降级成「只记名字」。报表里 `规则之外` 那一行 **26 个目录 + 527 个文件**（"继承上级的「只索引名字」"）就是这些子树的子项。
   `F2-03` 的语义（`name/` 才只匹配目录、扩展名规则两边都匹配）**没有错**，错的是这样会让"排掉一个目录"的代价比预期大得多——**这正是 `--dry-run` 要把目录与文件分开数的理由**（`F2-06`）。
3. **`Thumbs.db` 那处覆盖（`F2-03` 遗留第 1 条）没有出现在前 30 行里**，所以本次**仍不能定它要不要调顺序**。`--limit` 之外的那部分要靠 `F2-06` 的完整分布（或把 `--limit` 调大）才看得到——记在这里，免得下次以为它不存在。

---

## 四、与 `F2-06` 的关系

`F2-06` 的 `lsearch index --dry-run` 要交付的是**同一份分布**，只是从 CLI 出、并按计划 §5 的 P2 验收方式给用户看。本报告先用手边的 `tools/scan_report.py` 把数据做出来了：**`--dry-run` 的呈现可以直接照这份报表的形状定**（目录/文件分列、按 `(分区, 规则原文, 行号)` 分组、`规则之外` 单列一桶）。届时这里的数字**要重跑**——不要把本报告的计数当成 `--dry-run` 的期望值抄进测试。
