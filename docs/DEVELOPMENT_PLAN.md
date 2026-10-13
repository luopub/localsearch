# LocalSearch 开发计划（v1.0 draft）

- **状态**：待你批准，尚未开始任何开发与环境配置
- **制定日期**：2026-10-14
- **项目一句话**：纯离线、基于 EmbeddingGemma 2 的本地全盘全文语义搜索，首版交付命令行工具，架构上为后续 GUI 与多模态检索预留扩展位。

---

## 1. 需求与验收

### 1.1 首版目标

1. 在**完全离线**（无网络、无外部服务、无 MCP）环境下，对本地磁盘建立可检索的全文本索引。
2. 提供 CLI，支持自然语言语义检索 + 关键词精确检索的混合排序，命中结果可定位到文件与片段。
3. 索引过程**可中断、可续跑、可增量更新**——这是 6TB 级数据的生存前提。
4. 核心检索层与呈现层解耦，未来接入 GUI 时只增加一层"皮"，不重构内核。
5. 数据类型在 schema 与接口层就带 `modality` 概念，未来接入图像/音频编码器时不需要改表。

### 1.2 验收标准（可度量）

| 编号 | 标准 | 验证方式 |
|---|---|---|
| A1 | 在 2 万文件的基准集上完成全量索引，无未捕获异常，中断后 `--resume` 能接续且不重复嵌入 | 自动化测试 + 日志核对 |
| A2 | 嵌入吞吐由 **P0 在本机实测**确定，整机并行效率 ≥ 单进程的 8 倍；**不再预设绝对数字**（见 §3.2 重估说明） | `lsearch bench` 实测报告 |
| A3 | 检索端到端 p95 延迟 < 1.5s（索引规模 500 万切片） | `lsearch bench --query-set` |
| A4 | 自建 20 条人工标注查询集上，混合检索 Top-10 命中率 ≥ 85%（查询集来源见 §7.3） | `tests/eval/` 评测脚本 |
| A5 | **权重预下载完成后**，断网状态下全流程可运行（`HF_HUB_OFFLINE=1`）；首次下载天然需要联网 | CI 式冒烟测试 |
| A6 | 单文件解析失败不中断整轮索引，失败原因计入 `files.error_msg` | 注入损坏文件测试 |
| A7 | **不产生计划外的持久化落点**：模型权重跟随系统 `HF_HOME`（这是唯一被认可的例外），索引与临时文件全在 `data_dir`，项目目录内不出现任何缓存 | `lsearch doctor` 报告 `HF_HOME` / `data_dir` / `TORCH_HOME` 的**实际生效值**并校验 |
| A8 | **分级就绪**：Tier0 完成后即可按文件名/路径检索；Tier1 完成后全文可检索；Tier2 未完成时搜索自动降级并在结果里显式标注"语义未就绪" | 在各 Tier 之间中断，逐级验证可用性 |
| A9 | 同一份内容出现在多个路径时，只提取与嵌入一次（去重生效） | 构造重复文件，核对 `blobs` 与 `embed_cache` 命中 |

> A2/A3 中的绝对数字**不在本计划里拍死**。P0 阶段实测后再填，避免写出无法履约的指标。

### 1.3 非目标（v1 明确不做）

- OCR：扫描版 PDF、图片内文字不识别（属多模态阶段）
- 图片/音频/视频的**内容**向量化（v1 只索引其文件名、路径与元数据）
- GUI（架构预留，不实现）
- 实时文件监听（v1.1 用 watchdog，v1 用扫描式增量）
- 加密盘/网络盘/受保护系统目录的深度遍历
- 服务化、多用户、权限隔离

---

## 2. 已确认的技术决策

| # | 决策项 | 选定方案 | 关键依据 | 主要代价 |
|---|---|---|---|---|
| D1 | 嵌入推理链路 | **sentence-transformers + PyTorch CPU** | Google 官方文档路径：`prompt_name` 自动处理查询/文档非对称前缀、pooling 正确，不需要自己踩 ONNX 导出与 contrib 算子的坑 | torch 依赖约 2.5GB；纯 CPU fp32 吞吐偏低 |
| D2 | 向量索引后端 | **SQLite FTS5 + HNSW（FAISS 优先，hnswlib 备选）** | 关键词 BM25 与向量检索各自原生，RRF 融合；百万至千万级性能可预测；Windows 上最成熟 | 向量文件与增量重建需自行管理，代码量最大 |
| D3 | 索引范围 | **白名单根目录为默认 + `--all-drives` 显式全盘开关** | 日常增量快；全盘作为可选重任务，不至于每次被 6TB 拖死 | 需要维护两套排除规则语义 |
| D4 | 文件类型覆盖 | **全部四类**：纯文本/代码、办公文档、网页与邮件、媒体文件名与元数据 | 用户选定 | 提取器工作量最大，是 v1 的主要成本项 |
| D5 | 嵌入维度 | **768 原生维 + int8 量化落盘** | 用户选定：宁可多占存储也要保检索质量 | 向量约 3.5GB、HNSW 查询更慢；**注意不显著增加嵌入耗时**（MRL 截断发生在主干前向之后，计算量恒定） |
| D6 | PDF 提取 | **PyMuPDF**（单后端，不引入回退） | 比纯 Python 方案快一个数量级，中文 PDF 提取质量明显更好 | AGPL-3.0：自用无碍，将来分发/商用需商业许可；接口隔离在 `extract/office.py`，替换成本低 |
| D7 | CLI 形态 | **一次性命令 + 交互式 REPL 双形态** | 脚本/快捷方式走一次性；直接跑 `lsearch` 进 REPL 连续搜、翻页 | 多维护一个 REPL 层（复用 `SearchService`，成本可控） |
| D8 | 大文档切片 | **每文档 200 片上限 + 截断标注**，超出只索引表头与统计摘要 | 防止单个 5 万行 xlsx 造出上万切片、吃掉数小时索引时间 | 大表深处内容搜不到，需 `--max-chunks-per-doc` 手动放开 |
| D9 | 嵌入后端 | **P0 双跑实测再定**：sentence-transformers 与 ONNX int8 各跑一遍本机吞吐 | 选 D1 时还不知道算力是主要矛盾；[anygrep](https://github.com/kuroko1t/anygrep) 证明同款模型 int8 ONNX 可在 CPU 上跑通 | P0 多花约半天 |
| D10 | 索引就绪策略 | **三级就绪**：Tier0 文件名/路径（分钟级）→ Tier1 正文+FTS（小时级）→ Tier2 向量语义（天级），各级独立可用，搜索自动降级并标注 | 算力现实下，"能用"不能绑定在"全部建完"上 | 检索层需处理"部分就绪"状态；schema 与 CLI 都要带 tier 概念 |
| D11 | 文件枚举 | **可替换扫描后端**：`os.scandir` 为默认，MFT + USN Journal 为可选加速后端（需管理员、限 NTFS） | 全盘枚举 5 万文件/分钟 → 200 万/分钟；USN 让增量变成真·秒级 | 两份实现；MFT 需管理员与 NTFS，必须有无损回退 |
| D12 | 内容去重 | **按内容哈希去重** + 嵌入输入级缓存 | 6TB 盘上备份副本占比高；实测类项目可省 10–30% 嵌入计算，还白赚"找出所有重复文件" | schema 需引入 blobs 与引用计数；删除时不能误删共享内容 |

### 2.1 模型事实基础（公开信息，含待验证标记）

| 项目 | 值 | 置信度 |
|---|---|---|
| 仓库 | `google/embeddinggemma-2` | 高 |
| 许可 | Apache 2.0，**已取消 gating**，可无人值守拉取 | 高 |
| 参数量 | 740M = 270M 文本/代码主干 + 170M 视觉 + 300M 音频（按需加载） | 高 |
| 架构 | Gemma 4 | 中 |
| 向量维度 | 统一 768 维，MRL 可截断 512/256/128 | 高 |
| 上下文 | 8192 tokens | 高 |
| 语言 | 100+ | 高 |
| 精度 | **必须 bf16/fp32，禁止 fp16**（激活值 NaN） | 高 |
| 查询前缀 | `task: search result \| query: {query}` | 高 |
| 文档前缀 | `title: {title} \| text: {content}`，无标题用 `title: none` | 中 |
| 多模态输入 | **不加任何前缀** | 中 |
| Python 依赖版本 | sentence-transformers ≥ 6.1.0、transformers ≥ 5.19 | **待验证** |
| ONNX 成品 | `onnx-community/embeddinggemma-2-ONNX`，int8 约 314MB | 中 |

> **验证优先原则**：标注"待验证"及所有版本号，在 P0 阶段实地验证。验证不通过则回退方案（降 Python 版本 / 源码安装 / 切换 ONNX 后端），**不会把未验证的版本号当成已知事实写进依赖文件**。

---

## 3. 硬约束与容量预算

### 3.1 实机环境（已实测）

| 项目 | 值 |
|---|---|
| CPU | AMD64 16 核 |
| GPU | **无**（`nvidia-smi` 不存在） |
| 磁盘 | C: 488G / D: 1.4T / E: 477G / **M: 3.7T**，合计约 6TB |
| 已有工具 | Python 3.11.9、uv 0.10.2、pip 24.0 |
| 代码与 Python 环境 | `D:\work\test\localsearch\`（`.venv` 由 uv 建在项目根） |
| 数据目录 | `D:/localsearch-data/`（数据库 + 向量索引 + 日志/临时文件集中于此，自动排除；**模型权重不在此处**，见 §3.3 的例外说明） |

**结论：纯 CPU 嵌入是本项目最大的工程风险，D1 的选择在吞吐上是有代价的，必须靠并行、低维、量化、增量来对冲。**

### 3.2 容量推算（估算，P0 后替换为实测）

以"全盘可索引文件 150 万、平均约 1000 tokens 正文"为模型：

| 项目 | 计算 | 结果 |
|---|---|---|
| 切片总数 | 150 万 × ~3 | 450 万 |
| 正文 tokens 总量 | 150 万 × ~1000 | **约 15 亿 tokens** |
| 嵌入耗时（悲观） | 15e9 ÷ 1100 tok/s | **约 400 小时** |
| 嵌入耗时（乐观） | 15e9 ÷ 4000 tok/s | 约 100 小时 |
| 向量存储（**已选**：768 维 int8） | 450 万 × 768 × 1B | **3.5 GB** |
| （参考）768 维 fp32 | 450 万 × 768 × 4B | 13.8 GB |
| （参考）256 维 int8 | 768int8 ÷ 3 | 1.15 GB |
| 关键词索引 + 文本 | 450 万 × ~400B + FTS 开销 | 3–5 GB |

**这个表里最该看的是"嵌入耗时"两行之间 4 倍的差距，而不是其中任何一个具体数字。** 依据是 [anygrep](https://github.com/kuroko1t/anygrep) 作者在 Ryzen 7 7700 上实测的 ~1100 tokens/s（int8 ONNX，8 线程）——别人的机器、口径未必一致、也未确认是否批量。本机是 Zen2（Family 23 Model 96），**没有 VNNI 指令集**，int8 加速收益远不如 Zen4，实际大概率落在悲观侧。

结论不是"要 400 小时"，而是：**在 P0 实测出本机吞吐之前，任何关于首次索引耗时的承诺都不成立。** 本计划早先版本里那句"数量级：10 小时级"已删除——它是拿一个从未实测的参数（"实测单进程吞吐"）推出来的循环论证。

> **关于维度的更正**：`256 维能显著加快嵌入` 是错的。EmbeddingGemma 2 的 MRL 截断发生在主干前向计算**之后**——模型无论如何都算完 768 维，取前 256 维只是一次切片。维度只影响**存储占用**与 **HNSW 建索引/查询开销**，对嵌入吞吐影响很小。所以 D5 选 768 的代价比它看起来小。

**索引库总占用预估 5–20GB**，落地于 `D:/localsearch-data/`（见 §3.3），容量充裕且自动排除。存储不是问题，**算力才是**。

**由此导出的设计强制项：**

1. **分级就绪**（D10）：不能把"能用"绑在"全部建完"上。Tier0 分钟级可用是核心设计，不是优化项
2. 多进程并行嵌入（16 核吃满，受限于 torch 每进程约 1–1.5GB 常驻，worker 数默认 `min(8, cores//2)` 可配）
3. **内容去重**（D12）：同内容只嵌入一次；且只对 size 重复的文件算哈希（见 §4.4.1）
4. **嵌入输入级缓存**：以"实际喂给嵌入器的文本 + 模型/维度指纹"为键跨文件复用向量
5. 严格的排除规则与大小上限——**每砍掉一份语料就是线性砍掉索引时间，这是唯一最有效的杠杆**
6. 索引写盘分阶段提交，任何时刻中断都能续跑

### 3.3 环境与数据目录布局（已确认）

| 内容 | 位置 | 管理方式 |
|---|---|---|
| 源码 | `D:\work\test\localsearch\src\` | 本目录，全部代码集中于此 |
| Python 环境 | `D:\work\test\localsearch\.venv\` | uv 默认建在项目根 |
| uv 包缓存与解释器 | `%LOCALAPPDATA%\uv\`（全局） | **刻意不本地化**：跨项目复用，省下反复下载 torch 的数 GB |
| 数据库 + 向量索引 + 日志/临时文件 | `D:\localsearch-data\` | 单一目录，自动进入排除列表 |
| 模型权重与 HF 缓存 | `C:\Users\luopu\.cache\huggingface` | **跟随系统已有的 `HF_HOME`，不覆盖**（见下文例外说明） |
| 配置 | `%APPDATA%\localsearch\config.toml` | 由 `lsearch config init` 生成 |

数据目录内部结构：

```
D:/localsearch-data/
├─ index/
│  ├─ lsearch.db          # SQLite：files / blobs / chunks / chunks_fts / files_fts
│  └─ vectors.faiss       # HNSW 向量索引
├─ models/                # 仅当系统未设 HF_HOME 时的兜底位置；本机已有 HF_HOME，此目录为空
│  └─ hf/
├─ logs/
└─ tmp/                   # 批量嵌入的中间落盘，可随时清空
```

**关键机制：模型缓存跟随系统 `HF_HOME`，不另起一套。**

实测你的机器已经设了用户级环境变量：

```
HF_HOME = C:\Users\luopu\.cache\huggingface     # 已占用 14GB
```

里面已有 Qwen-Image-Layered、RMBG-2.0、Florence-2-base/-ft、all-MiniLM-L6-v2——你本来就在用 HuggingFace 生态。**本计划原先"把 HF_HOME 改指到 data_dir"的做法是错的**：那会在磁盘上多分出一套缓存，同一个模型存两遍，而且与你在其他项目里的用法割裂。改为尊重既有配置。

设置时机不变：必须在**任何 torch / transformers 导入之前**完成（库一旦被导入就锁定了路径，之后再设无效）。规则是 **`setdefault` 语义——已有则不动，缺失才补**：

| 环境变量 | 缺失时的兜底值 | 说明 |
|---|---|---|
| `HF_HOME` | `D:/localsearch-data/models/hf` | **本机已有值，不会被覆盖** |
| `TORCH_HOME` | `D:/localsearch-data/models/torch` | torch hub 缓存 |
| `HF_HUB_OFFLINE` | `1`（仅索引/检索运行时） | 默认离线；`tools/fetch_model.py` 显式关闭以完成首次下载 |

**原先计划里的 `SENTENCE_TRANSFORMERS_HOME` 已删除。** 它是个遗留变量，而 ST v6 的模型实际走 `huggingface_hub`。把它指到与 `HF_HOME` 不同的位置，会导致同一份权重在两个目录各存一份——正是这一节要避免的事。P0 顺带确认新版本 ST 是否已完全忽略该变量。

实现方式：`cli/main.py` 最顶端、在导入 `localsearch.embed` 之前调用 `bootstrap_env()`，内部一律 `os.environ.setdefault`。`uv run --env-file` 只作便捷写法，**不作唯一机制**——该参数行为一旦变化，环境变量会静默失效。程序内设置才是兜底。

> **`google/embeddinggemma-2` 目前不在你的缓存里**（已核实），首次运行需联网下载约 1–3GB 到这个既有目录。C 盘剩 135G，加进去无碍。这也是为什么 A5 的"全流程离线"指的是**下载完成之后**。

#### 由此产生的一处例外（明确记录）

"所有数据集中在一个目录"这条现在有**一个刻意的例外**：模型权重跟随系统 `HF_HOME`，落在 C 盘用户目录；索引库、向量、日志、临时文件仍全部在 `D:/localsearch-data/`。

如果你希望连权重也收进 `D:/localsearch-data/`，改法是你自己去改那个用户级 `HF_HOME` 变量——**程序不会替你改**。这是有意为之：一个工具擅自覆盖用户已配置的全局缓存路径，比"文件没集中在一个目录"严重得多。

环境重建命令（P1 落地，写进 README）：

```bash
uv venv                      # 在项目根创建 .venv
uv sync                      # 按 pyproject/uv.lock 安装依赖
uv run lsearch doctor        # 自检：依赖版本、模型、FTS5、磁盘、缓存落点
```

---

## 4. 架构设计

### 4.1 数据流

```
                 ┌──────────────┐
   根目录/全盘 →  │  Scanner     │  遍历 + 排除规则 + 变更检测（mtime/size/quick_sig）
                 └──────┬───────┘
                        ↓  FileRecord（增量：新增/变更/删除）
                 ┌──────────────┐
                 │  Extractor   │  按 (ext, mime) 派发 → ExtractedDoc(text, title, meta, modality)
                 └──────┬───────┘
                        ↓
                 ┌──────────────┐
                 │  Chunker     │  结构感知切分 → Chunk[](text, char_start, char_end, ord)
                 └──────┬───────┘
                        ↓
                 ┌──────────────┐
                 │  Embedder    │  封装前缀与批处理 → float32 向量
                 └──────┬───────┘
                        ↓
        ┌───────────────┴────────────────┐
        ↓                                ↓
 ┌─────────────┐                  ┌─────────────┐
 │ SQLite      │  元数据+文本+FTS5 │ VectorStore │  HNSW 索引文件
 └─────────────┘                  └─────────────┘
        └───────────────┬────────────────┘
                        ↓  检索时
                 ┌──────────────┐
                 │  Searcher    │  BM25 + 向量 + 路径匹配 → RRF 融合 → 排序
                 └──────┬───────┘
                        ↓
                 ┌──────────────┐
                 │  Presenter   │  CLI: table / json / 高亮片段
                 └──────────────┘
                        ↑
              SearchService（GUI 未来唯一依赖的门面）
```

### 4.2 扩展点设计（GUI 与多模态的地基）

这是本项目"预留扩展空间"要求的落点，全部在 v1 就用 Protocol/ABC 固化接口，**接口先行，实现可后补**：

| 扩展点 | 契约 | v1 实现 | 未来 |
|---|---|---|---|
| `Scanner` | `walk(roots, since) -> Iterator[FileRecord]` | `ScandirScanner`（默认）；`MftScanner`（可选，D11） | 其他卷格式、网络盘 |
| `Embedder` | `embed_documents(texts, metas) -> np.ndarray`、`embed_query(q) -> np.ndarray`、`fingerprint`（模型+维度+量化指纹） | `GemmaSTEmbedder` 与 `OnnxEmbedder` 双后端（P0 实测后定默认，D9；前缀逻辑封装在内部） | `ImageEmbedder`、`AudioEmbedder`，按 modality 注册 |
| `Extractor` | `supports(ext, mime)` + `extract(path) -> ExtractedDoc` | 文本/代码、Office、网页邮件、媒体元数据 四类 | OCR 提取器、音视频转写 |
| `ChunkPolicy` | `split(doc) -> Chunk[]` | 滑动窗口 + 结构边界 | 按图片/时间轴切分 |
| `VectorStore` | `add(ids, vecs)`、`search(vec, k, filter)`、`remove(ids)`、`compact()` | `FaissHnswStore`（备选 `HnswlibStore`） | LanceDB / Qdrant |
| `IndexStore` | 元数据与文本的读写事务 | `SqliteStore` | 其他 RDB |
| `FusionStrategy` | `merge(result_lists) -> Ranked[]` | RRF (k=60) | 学习式重排 / cross-encoder |
| `Presenter` | `render(results, fmt)` | `cli.table` / `cli.json` | GUI JSON、Web API |
| **`SearchService`** | `search(query, filters, limit) -> list[SearchHit]` | 门面实现 | **GUI 只依赖这一层** |

配套约定：

- `SearchHit` 数据类**预留字段**：`modality`、`score_breakdown`（各路得分）、`thumbnail_ref`（未来缩略图/波形）、`locator`（字符偏移，便于编辑器跳转）
- 注册表模式（`plugins.py`）：新提取器/嵌入器通过装饰器注册，不改核心分发逻辑
- CLI 与未来 GUI 是 `SearchService` 的**对等消费者**，不出现"CLI 里写业务逻辑，GUI 时再抽出来"的技术债
- `embed_ver` 指纹入库：模型/维度/量化任一变更即标记为 stale，触发定向重嵌入，而不是全量重建

### 4.3 目录结构

```
localsearch/
├─ pyproject.toml              # uv 管理，PEP 621
├─ README.md
├─ .gitignore                  # 版本库排除（与 .lsearchignore 是两回事，见文件头说明）
├─ .gitattributes              # 钉死 run-index.cmd 的 CRLF（§4.8）
├─ .lsearchignore              # 索引排除规则（用户可改，单一来源，见 §4.5.1）
├─ push-git.bat                # 手动发布：add + commit + 推 GitHub/Gitee，消息可从命令行带入
├─ docs/
│  ├─ DEVELOPMENT_PLAN.md      # 本文件：打算做什么
│  └─ PROGRESS.md              # 开发与测试进度：实际发生了什么（见 §10）
├─ config/
│  └─ default.toml             # 默认配置模板
├─ src/localsearch/
│  ├─ __init__.py
│  ├─ cli/                     # 仅负责参数解析与呈现
│  │  ├─ main.py               # Typer 入口
│  │  ├─ repl.py               # 交互模式（D7）：连续搜、翻页、看详情
│  │  └─ commands/{index,search,status,bench,config,doctor}.py
│  ├─ core/
│  │  ├─ models.py             # FileRecord / ExtractedDoc / Chunk / SearchHit
│  │  ├─ config.py             # TOML 加载 + 默认值 + 校验
│  │  ├─ registry.py           # 扩展点注册表
│  │  └─ logging.py
│  ├─ ingest/                  # 扫描与增量
│  │  ├─ base.py               # Scanner Protocol（可替换后端，D11）
│  │  ├─ scanner_scandir.py    # 默认后端：os.scandir，无权限要求
│  │  ├─ scanner_mft.py        # 可选后端：MFT 枚举 + USN Journal 追变更（需管理员）
│  │  ├─ filters.py            # .lsearchignore 解析 / 大小上限 / 二进制嗅探 / .lsearch-noindex
│  │  ├─ fingerprint.py        # quick_sig / content_hash（含 size 预筛）
│  │  └─ dedup.py              # 内容去重与引用计数（D12）
│  ├─ extract/                 # 提取器（modality 概念在此落地）
│  │  ├─ base.py
│  │  ├─ plaintext.py
│  │  ├─ office.py             # pdf(PyMuPDF) / docx / xlsx / pptx
│  │  ├─ web_mail.py           # html / htm / mht / eml / msg
│  │  └─ media_meta.py         # 仅文件名 + EXIF/ID3
│  ├─ chunk/
│  │  └─ sliding.py
│  ├─ embed/
│  │  ├─ base.py               # Embedder Protocol
│  │  ├─ gemma_st.py
│  │  └─ batching.py           # 批大小 / 多进程调度 / OOM 退避
│  ├─ index/
│  │  ├─ schema.sql
│  │  ├─ sqlite_store.py
│  │  ├─ vectors_faiss.py
│  │  ├─ vectors_hnswlib.py
│  │  ├─ vector_cache.py       # 嵌入输入级缓存：text_hash + embed_ver → 复用向量
│  │  └─ maintenance.py        # 压缩、重建、vacuum、安全 prune（只在可达卷内删）
│  ├─ search/
│  │  ├─ lexical.py            # FTS5 / BM25（含字面匹配加权，见 §4.7）
│  │  ├─ semantic.py           # HNSW 查询
│  │  ├─ path_match.py         # 文件名/路径加权
│  │  ├─ tier.py               # 分级就绪：按已完成 Tier 自动降级并标注（D10）
│  │  └─ fusion.py             # RRF
│  ├─ presenter/
│  │  ├─ table.py
│  │  └─ json_out.py
│  └─ service.py               # SearchService 门面 ← GUI 唯一入口
├─ tests/
│  ├─ unit/
│  ├─ fixtures/                # 每种格式一个最小样本
│  └─ eval/                    # 20 条标注查询 + 命中率评测
├─ scripts/windows/            # 操作系统层集成（见 §4.8）
│  ├─ localsearch-daily-index.xml  # 计划任务定义，schtasks /Create /XML 注册
│  └─ run-index.cmd                # 任务启动器：建日志目录、轮转、退出码归一
└─ tools/
   ├─ fetch_model.py           # 一次性联网预下载权重到既有 HF_HOME（临时关闭 HF_HUB_OFFLINE）
   └─ bench_embed.py           # P0 吞吐实测
```

### 4.4 数据库 schema

```sql
-- 版本与全局元信息（embed_ver 指纹、各卷最后扫描状态与 USN、Tier 进度）
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- 内容表（D12 去重）：一个内容实体只提取、分块、嵌入一次。
-- 同一份内容的多个副本（不同路径、备份、改名）共享同一行。
CREATE TABLE blobs (
  id           INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL UNIQUE,      -- 全文 blake2b；仅对 size 重复的文件计算（§4.4.1）
  size_bytes   INTEGER NOT NULL,
  extractor    TEXT,                      -- 提取器名
  parser_ver   TEXT,                      -- 提取器版本，参与缓存键，防止旧解析结果被复用
  chunk_count  INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER
);

-- 文件表：增量判定与 Tier0 路径维度检索
CREATE TABLE files (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL UNIQUE,      -- 规范化绝对路径
  volume       TEXT NOT NULL,             -- C:/ D:/ E:/ M:/
  ext          TEXT,
  mime         TEXT,
  size_bytes   INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  quick_sig    TEXT,                      -- 首尾各 4KB 的 blake2b，用于判断"是否变了"
  content_hash TEXT,                      -- 与 blobs.content_hash 对应；未提取时为 NULL
  blob_id      INTEGER REFERENCES blobs(id),
  frn          INTEGER,                   -- NTFS 文件记录号（MFT 后端填）：改名后仍可识别
  usn          INTEGER,                   -- 最后处理的 USN（MFT 后端填）
  status       TEXT NOT NULL,             -- pending|metadata|fulltext|indexed|skipped|error|stale
  tier         INTEGER NOT NULL DEFAULT 0,-- 该文件已达成的最高 Tier（D10）
  indexed_at   INTEGER,
  error_msg    TEXT
);
CREATE INDEX idx_files_volume ON files(volume, status);
CREATE INDEX idx_files_mtime  ON files(mtime_ns);
CREATE INDEX idx_files_blob   ON files(blob_id);

-- 切片表：挂在 blob 上而不是 file——这是去重能生效的前提
CREATE TABLE chunks (
  id         INTEGER PRIMARY KEY,
  blob_id    INTEGER NOT NULL REFERENCES blobs(id) ON DELETE CASCADE,
  ord        INTEGER NOT NULL,
  char_start INTEGER,
  char_end   INTEGER,
  text       TEXT NOT NULL,
  n_tokens   INTEGER,
  modality   TEXT NOT NULL DEFAULT 'text',  -- 多模态预留
  vec_label  INTEGER,                       -- HNSW 中的 id；同文本共享时多行指向同一 label
  embed_ver  TEXT                           -- 模型+维度+量化指纹；NULL = 尚未进入 Tier2
);
CREATE INDEX idx_chunks_blob ON chunks(blob_id, ord);
CREATE INDEX idx_chunks_vec  ON chunks(vec_label);
CREATE INDEX idx_chunks_ever ON chunks(embed_ver);

-- 嵌入输入级缓存（D12）：同一段文本在不同文件里出现时只嵌入一次
CREATE TABLE embed_cache (
  text_hash TEXT NOT NULL,      -- 最终喂给嵌入器的文本（含 task 前缀）的 blake2b
  embed_ver TEXT NOT NULL,      -- 模型+维度+量化指纹
  vec_label INTEGER NOT NULL,   -- 复用已有向量在 HNSW 中的 id
  PRIMARY KEY (text_hash, embed_ver)
);

-- Tier1 正文索引：独立表（非 external content），存分词后文本
-- 注意：这里【不再】放 path——切片已挂在 blob 上，一个切片可能对应多个文件路径，
-- 把 path 写进切片索引是错误的。路径检索归 Tier0 的 files_fts（见下）。
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text_tok,          -- CJK 预分词后的文本
  title,
  chunk_id UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

-- Tier0 名称/路径索引：不依赖正文提取，建完即可"按文件名找"
CREATE VIRTUAL TABLE files_fts USING fts5(
  name_tok,          -- 文件名（含扩展名），CJK 预分词
  path_tok,          -- 完整路径，CJK 预分词
  file_id UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);
```

#### 4.4.1 内容去重（D12）的落地细节

去重的收益在 6TB 盘上很实在，但朴素实现会带来两个真问题，方案如下：

**问题一：给全盘算 BLAKE3 本身就要读一遍所有字节。**
用 fdupes 那套经典技巧——**先按 `size_bytes` 分组，只对"存在同尺寸兄弟"的文件算内容哈希**。全盘里尺寸唯一的文件占了绝大多数，它们根本不需要读。这一步把去重的 I/O 成本降到接近零，同时仍然能抓住备份副本这种主要目标。

**问题二：删文件时不能把共享内容一起删掉。**
引用计数。`files.blob_id` 是多对一，删除文件只减引用；当 `SELECT COUNT(*) FROM files WHERE blob_id=?` 归零时，才删 blob 及其 chunks、FTS 行，并标记对应 HNSW label 为 tombstone。`--prune` 必须走这条计数路径，不能直接按 path 删。

**顺带的两个白赚：**

- **改名不重嵌**：文件改名后 `content_hash` 不变 → 命中已有 blob → 零嵌入成本。用 MFT 后端时 `frn` 还能在哈希之前就识别出改名
- **"找出所有重复文件"** 成了免费的附加能力：`files` 按 `blob_id` 分组即可，将来可以做成一条 `lsearch dupes` 子命令

**缓存键必须带上版本**：`embed_cache` 的键是 `(text_hash, embed_ver)`，`blobs` 里另存 `parser_ver`。否则换了提取器或模型之后，会静默复用按旧逻辑产生的切片与向量——[有实践者踩过这个坑](https://shop.zimaspace.com/blogs/tech-ai-hub/content-hash-indexing-file-fingerprints-redundant-ai-work)。

**关于中文分词的决策（重要）**：

- FTS5 内置 `unicode61` 对中文按连续汉字成串，2 字词检索命中率差；`trigram` 分词器对 CJK 友好，但**要求查询串至少 3 个字符**，2 字中文查询直接失效。
- 因此 v1 采用 **jieba 预分词 + unicode61** 方案：入库前把 CJK 段落切成空格分隔的词，英文小写化，查询侧同样处理。2 字词、专有名词、代码标识符都能正常命中。
- 代价：FTS 表会额外存一份分词文本（估算 1–2GB），且 `snippet()` 返回的是分词后文本——**展示片段一律从 `chunks.text` 按 `chunk_id` 取回原文**，不受影响。
- 备选：若 jieba 依赖不可接受，退回 `trigram` 并在文档中标注"2 字查询需加通配"。

### 4.5 配置模板（`config/default.toml`）

```toml
[paths]
data_dir   = "D:/localsearch-data"     # 索引/向量/日志/临时文件集中于此；自动进入排除列表，见 §4.5.1
roots      = ["D:/work", "M:/docs"]    # 白名单根目录
all_drives = false                     # true 时忽略 roots，扫描全部固定盘
# 模型缓存不在本文件配置：一律跟随系统 HF_HOME（本机 = C:\Users\luopu\.cache\huggingface），
# 缺失时才兜底到 data_dir/models。不提供配置项是有意的——同一件事只许有一个真相来源，
# 否则 config 与系统环境变量会打架，而"哪边生效"将无从判断。改位置请改系统变量。
# `lsearch doctor` 会打印实际生效值。

[scan]
backend         = "auto"               # auto|scandir|mft（D11）；mft 需管理员且限 NTFS，失败自动回退
follow_symlinks = false
max_file_mb     = 64                   # 超过则只索引文件名与元数据
ignore_file     = ".lsearchignore"     # 排除规则的唯一来源，见 §4.5.1
respect_gitignore = false              # v1.1：自动读取各级 .gitignore 作为补充
dedup_by_content  = true               # 内容去重（D12），仅对 size 重复的文件算哈希

[extract]
text_code   = true
office      = true                      # PDF 走 PyMuPDF（D6）
web_mail    = true
media_meta  = true
email_attachment_depth = 1

[chunk]
target_tokens = 512
overlap_ratio = 0.15
max_chunks_per_doc = 200                # 超出只保留表头/统计摘要并标注"已截断"（D8）

[embed]
backend     = "auto"                    # auto|sentence_transformers|onnx；P0 双跑实测后填默认值（D9）
model_id    = "google/embeddinggemma-2"
dim         = 768                       # 已选原生维（D5）；可设 512/256/128，改动后需 reindex --stale
dtype       = "float32"                 # 严禁 float16
quantize    = "int8"                    # 向量落盘量化: none|int8
batch_size  = 16

[index]
vector_backend = "faiss"                # faiss | hnswlib
hnsw_m         = 32
hnsw_ef_construction = 200
hnsw_ef_search       = 64

[tiers]
# 分级就绪（D10）：`lsearch index --tier N` 跑到第 N 级为止，各级独立可用
metadata = true                         # Tier0 文件名/路径：分钟级，建完即可按名字搜
fulltext = true                         # Tier1 正文提取 + FTS：小时级
semantic = true                         # Tier2 向量语义：天级

[search]
mode        = "hybrid"                  # hybrid | lexical | semantic
top_k       = 20
rrf_k       = 60
weight_lexical = 1.0
weight_semantic = 1.0
weight_path = 0.5

[cli]
language            = "zh"              # 帮助与提示语言
interactive_default = true              # 无参数运行 lsearch 时进入交互模式（D7）

[perf]
workers = 8                             # 并行嵌入进程数，默认 min(8, cores//2)
commit_every_files = 500                # 每 N 个文件提交一次事务
```

#### 4.5.1 排除规则文件 `.lsearchignore`

排除规则**不再散落在 config 里**，而是集中在项目根的 `.lsearchignore`（类 .gitignore 语法的纯文本，用户可直接编辑）。

**为什么要单独一个文件而不写进 TOML**：排除清单会长到几百行、需要分组和注释说明取舍，塞进 TOML 的数组里既难读也难维护；而且它是用户最高频的调整对象，独立文件便于版本管理、复制到其他机器、以及未来支持"每个扫描根目录放一份"。

**两层分区**（对齐 §1.3 非目标与 §4.2 的 `modality` 设计）：

| 分区标记 | 语义 | 典型用途 |
|---|---|---|
| `#@section skip` | 完全跳过，连文件名都搜不到 | 系统目录、依赖、缓存、临时文件 |
| `#@section name-only` | 只记文件名与路径，不做正文提取与向量化 | 大二进制、媒体、压缩包、旧版 Office、数据库文件 |

`name-only` 这一层的价值在于：全盘搜索时"那个 ISO 到底在哪个盘"是真实需求，但把几十 GB 二进制喂进 FTS 与向量库是纯浪费。分区让它两者兼得，也正好复用未来多模态阶段的"仅元数据"路径。

**语法**：`name/`（目录名任意层级）、`*.ext`（扩展名）、`/rel/path`、`^C:/Windows`（绝对路径前缀）、`**/`、`!pattern`（白名单例外，后写的优先；父目录被排除后子项无法救回）。

**实现要求**：

- `ingest/filters.py` 负责解析与匹配，两个分区各自编译成按序匹配的规则列表
- 必须按规则维度统计命中数，支撑 `lsearch index --dry-run` 输出"每条规则命中多少文件"——**这是用户调规则的唯一可靠依据，不能只给一个总数**
- `lsearch explain <path>` 输出某文件被哪条规则排除（或为何被跳过）
- 规则变更后 `--prune` 能剔除已索引但新规则不再允许的条目，无需全量重建
- `paths.data_dir` 走**三重机制**，不能只靠用户写规则：(1) 文件内 `#@begin managed` / `#@end managed` 区块由工具自动生成并覆盖，你以后挪数据目录或挪项目都不必回来手改；(2) walker 层硬性剔除该路径前缀——因为索引器在往 data_dir 里持续写文件，纯规则匹配存在竞态（刚遍历过的目录里又新建了文件）；(3) `.venv` 一并纳入 managed 区块。这是"数据目录自动进入 ignore 列表"的落地方式



### 4.6 CLI 命令面

```bash
lsearch doctor                      # 环境自检：依赖版本、模型可加载、FTS5 可用、磁盘权限
                                    # 并打印 HF_HOME / TORCH_HOME / data_dir 的实际生效值（A7）
lsearch config init|show|path       # 配置管理
lsearch index                       # 增量索引（首次即全量）
       [--root PATH]... [--all-drives] [--include GLOB] [--exclude GLOB]
       [--max-file-mb N] [--workers N] [--rebuild] [--resume]
       [--tier 0|1|2]               # 只跑到指定级别（D10）；缺省 = 跑到最高级
       [--dry-run]                  # 只统计不落库：按规则打印命中文件数
       [--prune]                    # 剔除失效条目；仅在本次真正可达的卷内执行（见 R14）
lsearch explain PATH                # 该文件为何被跳过/被哪条规则排除
lsearch search "查询词"              # 混合检索
       [--mode hybrid|lexical|semantic] [--limit N]
       [--ext pdf,md] [--under PATH] [--after 2026-01-01] [--json]
lsearch status                      # 索引规模、各 Tier 完成度、覆盖卷、规则命中分布、错误与 stale 统计
lsearch bench [--samples N]         # 嵌入吞吐实测（P0 校准与回归用）
lsearch reindex --stale             # 仅重嵌入 embed_ver 不匹配的切片
lsearch                             # 无参数 = 进入交互模式（D7）
       #   > 项目预算表            ← 连续查询，不必重复敲命令
       #   > :more 3               ← 展开第 3 条的更多上下文
       #   > :open 3               ← 用系统默认程序打开该文件
       #   > :filter ext=pdf       ← 在当前会话内叠加过滤条件
       #   > :q                    ← 退出
```

> `--include` / `--exclude` 是临时覆盖 `.lsearchignore` 的命令行参数，不写回文件；日常调规则请直接改 `.lsearchignore`，避免两处状态不一致。

### 4.7 参考项目与取舍

制定本计划时对照了若干同类项目。记录**借鉴了什么、明确拒绝了什么**，免得日后（包括未来的我）把已经论证过的结论重新论证一遍。

| 项目 | 借鉴点 | 结论 |
|---|---|---|
| Everything / [mftparser](https://pypi.org/project/mftparser/) | MFT 枚举 + USN Journal 追变更 | **采纳（D11）**：可选扫描后端，需管理员、限 NTFS，失败自动回退 scandir |
| [anygrep](https://github.com/kuroko1t/anygrep) | 同款模型 int8 ONNX 在 CPU 上的实测数据；FTS5 精确匹配层；中断后持久化进度；模型常驻模式 | **采纳**：D9 双跑、Tier 化、字面匹配加权。作者 ~1100 tokens/s 的实测是 §3.2 重估的依据 |
| [Recoll](https://www.recoll.org/) | `skippedNames`（名字都不进）与 `noContentSuffixes`（只进名字）的两分法；`nowalkfn` 目录级标记；per-file 跳过原因分类；**purge 在卷离线时不得执行** | **采纳**：印证了两分区设计；新增 `.lsearch-noindex`、`explain` 原因分类、`--prune` 的卷可达性保护（R14） |
| [sist2](https://github.com/sist2app/sist2) | `--fast` 只索引文件名+MIME；`--content-size` 限长；SQLite FTS5 作为轻量后端 | **采纳**：`--fast` 印证 Tier0 的价值。同时注意其"增量扫描误删未变更压缩包内文档"的修复记录 → 增量 + 容器格式是 bug 高发区（R16） |
| [LEANN](https://github.com/yichuan-w/LEANN) | 不存向量、查询时按需重算，省 97% 存储 | **明确拒绝**：它拿计算换存储，而我们恰恰算力紧张、存储充裕（向量仅 3.5GB）。方向相反 |
| [vexor](https://github.com/scarletkc/vexor) | `.vexorignore`；可复用索引；MCP server | 印证 `.lsearchignore` 方向；MCP server 列为 v1.2 候选（让 Claude Code 直接查这个索引） |
| [llm-tldr](https://github.com/voitta-ai/llm-tldr/blob/main/tldr/dedup.py) / [onyx #14176](https://github.com/onyx-dot-app/onyx/issues/14176) | 内容哈希去重；**按"实际喂给嵌入器的文本"而非原始文件做缓存键** | **采纳（D12）**：见 §4.4.1 |
| tantivy + jieba/lindera | 比 FTS5 更快、查询表达力更强，中文分词在引擎层完成 | **暂不采纳**：需引入编译型 Rust 扩展，与"依赖克制"冲突。若将来 FTS5 成为检索瓶颈再评估 |

### 4.8 自动增量：Windows 计划任务

**不常驻服务，用计划任务。** 常驻进程要处理单实例锁、崩溃自愈、开机自启、升级时替换可执行文件；而计划任务把这些都交给操作系统，代价只是"最坏情况下索引落后一天"。

已落盘两个文件：

| 文件 | 职责 |
|---|---|
| `scripts/windows/localsearch-daily-index.xml` | 计划任务定义。`schtasks /Create /TN "LocalSearch\DailyIncrementalIndex" /XML <文件> /F` 注册 |
| `scripts/windows/run-index.cmd` | 任务实际调用的启动器 |

#### 为什么是两层而不是一层

任务的动作**不直接**指向 `lsearch.exe`，中间隔一个 `.cmd`。理由都是具体的：

- **日志目录必须先存在**。`cmd` 打开重定向句柄时若目录不存在，重定向直接失败——一次静默的"什么都没发生"，而计划任务只留一个退出码。启动器先 `mkdir` 再重定向。
- **控制台日志轮转**。计划任务本身不捕获 stdout。启动器把输出写到 `data_dir\logs\scheduled-index.log`，超过 5 MiB 轮转一次并只保留一代，所以它不会无限增长。
- **退出码归一**。A6 要求"单个文件解析失败不中断整轮"，那这种轮次应当算成功；但进程若以非零退出，`<RestartOnFailure>` 会把它当崩溃反复重启。启动器把"完成但有文件失败"（退出码 1）改写成 0，只让真正的失败（配置/数据库/模型加载，≥2）触发重试。
- **`HF_HOME` 缺失时留下证词**。计划任务的运行环境由 `LogonType` 决定（见下）。环境里没有 `HF_HOME` 时启动器记一条 WARNING——否则表现是模型被静默重新下载一份到 `data_dir\models\hf`，而你在几周后才发现有两份权重。

#### 关键设置及其理由

| 设置 | 值 | 理由 |
|---|---|---|
| 触发器 | 每天 03:10 | 刻意不取整点：Windows 自动维护与多数备份任务挤在 02:00–03:00，这个作业想要独占磁盘 |
| `MultipleInstancesPolicy` | `IgnoreNew` | **最重要的一条**。首轮全量可能跑数天；下一次触发若另起一个实例，会与写者并发打开 SQLite 与 HNSW 索引。跳过新实例才是正确行为 |
| `ExecutionTimeLimit` | `PT0S`（无限制） | 默认 72 小时上限对首轮全量是不够的。中途被杀正是 `IgnoreNew` + `--resume` 要防的事 |
| `StartWhenAvailable` | `true` | 03:10 时机器关着/睡着，就在下次开机后补跑。**因此不需要再加"登录时触发"** |
| `Priority` | `7`（低于正常） | 默认是 5，数值越大优先级越低。索引器不该和你手头的活抢 CPU |
| `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries` | `true` / `true` | 全量嵌入会吃满所有核。台式机或"插电就想索引"的笔记本可改 false |
| `RunOnlyIfNetworkAvailable` | `false` | 本项目的前提就是离线可跑 |
| `WakeToRun` | `false` | 不为了索引把笔记本唤醒；靠 `StartWhenAvailable` 补跑 |
| `RestartOnFailure` | 3 次 / 15 分钟 | 配合上面的退出码归一，只对真失败重试 |

#### 两个需要你知道的取舍

**1. 权限等级决定扫描速度。** 默认 `LeastPrivilege` —— 扫描器回退到 `os.scandir`，无需提权。若改为 `HighestAvailable`，D11 的 MFT + USN Journal 后端可用：枚举快约 40 倍，且增量场景下 USN 只返回**变化**，代价从"每次走遍全盘"降到"读一条日志"。

我没有默认打开它，理由是：一个无人值守、每晚自动运行、且会删除索引条目的作业，按最小权限运行是对的；MFT 是优化不是前提。改一行即可，两种情形都已在 XML 的注释里写明。

**2. 首轮不是"夜间任务"，而是一段持续多日的进程。** 按 §3.2，全盘嵌入本机可能要数百小时。所以第一次跑起来之后，`IgnoreNew` 会让它事实上常驻，直到追平语料；追平之后每晚只需几分钟。

想要一个有界的夜间作业，把 XML 里的参数改成 `index --prune --tier 1`（"温和模式"）：只做文件名与全文，不嵌入，分钟级结束，搜索始终可用；语义结果则在你手动跑一次完整 `lsearch index` 时才出现。两个值都写在 XML 注释里。

#### 与 `HF_HOME` 改动的交叉点

`LogonType` 设为 `InteractiveToken`：不存密码，且任务继承已登录用户的**用户级环境变量**——也就是上一节刚确认过的那个 `HF_HOME`。

如果改成 `S4U`（"无论用户是否登录都运行"）或 `SYSTEM`，这个前提就不成立了。所以启动器里有那条 `HF_HOME` 缺失告警，切换后请用 `lsearch doctor` 复核生效值。

#### 注册时机：推迟到开发完成之后

**你已明确：先不注册、先不测，等开发做完再说。** 这一条取代了原先"P1 后注册"的写法。

理由是充分的：任务指向 `.venv\Scripts\lsearch.exe`，而该文件要到 P1 的 `uv sync` 才存在；在它存在之前注册，任务只会得到退出码 9009。即便存在，真正值得跑一次的是**端到端**验证——索引确实推进、日志确实写入、计划任务的上次运行结果确实能看到——那要等到索引链路可用。

所以两个文件现在就落盘，但**保持未注册状态**。README 会写完整注册步骤，端到端验证放在开发完成之后。

#### 落地前做过的冒烟测试（部分）

写文件时顺手用假的可执行文件跑过启动器的控制流，抓到两个真缺陷，都已修：

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `if (...)` 块内的 echo 文本含未转义圆括号 | cmd 在 `)` 处提前闭合块，报"此时不应有 ."，指向错误行号 | 块内 echo 文本不用括号 |
| 调用另一个 `.cmd` 时没加 `call` | 控制权被永久交出，日志只有 `start` 没有 `end`，退出码归一生效不了 | 改用 `call` |

已确认可用的部分：日志目录自动创建、缺可执行文件时退出码 9009 且留下 FATAL 记录、退出码 `1 → 0` / `2 → 2` 的归一。

**尚未验证**（随你的指示一起推迟）：`HF_HOME` 缺失告警的实际触发、日志 5 MiB 轮转、`schtasks` 对本 XML 的接受度、以及真实的端到端运行。

> **维护提示**：`run-index.cmd` 必须是 **CRLF** 行尾。多数编辑器（包括我的编辑工具）会自动规范化成 LF，而 cmd.exe 对 LF 行尾的 `.cmd` 在多行 `if (...)` 块里行为不稳定。改完这个文件请确认行尾仍是 CRLF。

#### 可观测性

无人值守作业的通病是"坏了没人知道"。三条发现路径，按投入排序：

1. `data_dir\logs\scheduled-index.log` —— 每次运行的完整控制台输出
2. `schtasks /Query /TN "LocalSearch\DailyIncrementalIndex" /V /FO LIST` —— 上次运行结果与退出码
3. **P6 待做**：让 `lsearch status` 顺带读一次计划任务的上次运行结果并显示出来。索引是否在推进、有没有连续失败，应该在一个命令里看到，而不是要你记得去查事件查看器

---

## 5. 分阶段计划

| 阶段 | 目标 | 交付物 | 验证方式 | 预估 |
|---|---|---|---|---|
| **P0** 可行性验证 | 把所有"待验证"变成已知事实，**核心是测出本机真实嵌入吞吐** | `tools/bench_embed.py`；**ST 与 ONNX int8 双跑对比报告**（D9 结论）；本机 tokens/s 实测；MFT 枚举在本机的可行性与权限验证（D11）；FAISS/hnswlib 在 Windows+py3.12 的可用性；FTS5 + jieba 可用性 | 用**你自己的真实文档**跑出可替换 §3.2 估算的实际数字 | 1–1.5 天 |
| **P1** 骨架 | 项目可安装、可运行、可配置 | pyproject/uv 环境、config 加载与校验、日志、CLI 壳、`lsearch doctor` | `uv run lsearch doctor` 全绿 | 0.5 天 |
| **P2** 扫描、指纹与去重 | 能安全遍历、识别变更、识别重复 | 双扫描后端（scandir + MFT）、`.lsearchignore` 解析与两层分区、指纹与 size 预筛去重、`.lsearch-noindex`、`--dry-run` 按规则统计 | 对 D:/work 试跑输出**每条规则的命中分布**；核对 MFT 与 scandir 结果完全一致 | 1.5–2 天 |
| **P3** 提取与分块 | 四类文件都能出正文 | 四类提取器（PDF 走 PyMuPDF）+ 分块器（含 200 片上限与截断标注）+ fixtures | 每种格式的最小样本单测；损坏文件不中断（A6） | 1.5–2 天 |
| **P4** Tier0 + Tier1 落地 | **先让工具真的能用** | `files_fts` 建库（路径/名称检索）、正文 FTS 写入、tier 状态推进、分阶段提交 | 全盘跑完 Tier0 后**立刻能按文件名搜到**（A8）；中断续跑 | 1.5–2 天 |
| **P5** Tier2 向量与混合检索 | 语义索引 + 融合 + 交互 | 嵌入器双后端、多进程调度、`embed_cache`、HNSW 构建、RRF 融合、片段高亮、REPL | 2 万文件基准集（A1）；去重生效（A9）；20 条评测（A4）；延迟实测（A3） | 2.5–3 天 |
| **P6** 增量与维护 | 长期可用 | USN 增量（可用时）、变更/删除、**引用计数 prune**、HNSW 压缩、stale 重嵌入、status 报表、`status` 中显示计划任务上次运行结果 | 增删改后一致性校验；**模拟拔盘验证 prune 不误删**（R14） | 1.5–2 天 |
| **P7** 收尾 | 可交付 | 单测与评测集、README（含计划任务注册步骤）、打包、性能回归基线 | 全流程冒烟 + 全盘试跑 | 1–1.5 天 |
| **（P7 之后）** 计划任务上线 | 无人值守增量 | 注册 §4.8 的 XML、跑通端到端、验证 `HF_HOME` 告警与日志轮转 | 手动触发一次，确认索引推进且日志与 `status` 都能看到结果 | 半天 |

**v1 合计约 11.5–15.5 个工作日**（不含全盘首次索引的机器运行时间；末行的计划任务上线按你的要求推迟到开发完成之后）。

> 比早先版本多了约 2–4 天，全部来自你采纳的四项决策——尤其 **MFT 扫描后端**（两份实现 + 一致性验证）与 **Tier 化**（schema 与检索层都要带 tier 概念）。这是真实成本，不想承担就说明，可以砍回单一 scandir 或两级就绪。

未来阶段（不在本次范围，仅说明扩展位已就绪）：

- **P8 GUI**：`SearchService` 已完成，GUI 只做"配置页 + 搜索框 + 结果列表 + 预览"，可选 PySide6（原生、离线友好）或本地 Web（NiceGUI/FastAPI+前端）
- **P9 多模态**：注册 `ImageEmbedder`/`AudioEmbedder`，开启视觉/音频编码器，`chunks.modality` 与 `SearchHit.modality` 字段已存在，检索侧按模态分开建 HNSW 再融合

---

## 6. 风险与缓解

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| R1 | 无 GPU + 6TB 规模，首次索引耗时可能达**数天到数周**（§3.2 已重估） | **高** | 高 | **Tier 化让工具从第 0 天就能用**（D10）；D9 双跑取更快后端；D12 去重省 10–30%；严格排除规则线性压缩语料（最有效的杠杆）；多进程吃满 16 核；断点续跑支持夜间分批。**不承诺总耗时，P0 出本机实测数字后再谈** |
| R2 | `hnswlib` 在 Windows 无预编译 wheel，需要 MSVC 工具链 | 中 | 中 | **优先用 `faiss-cpu`**（Windows 有预编译 wheel），hnswlib 仅作备选；P0 阶段定论 |
| R3 | sentence-transformers ≥6.1 / transformers ≥5.19 的版本要求在 Windows + py3.12 上不满足 | 中 | 高 | P0 实测；回退链：降 Python 到 3.11 → 指定可用版本区间 → 切 ONNX 后端 |
| R4 | 权限拒绝、超长路径、符号链接环、UNC 路径导致遍历中断 | **高**（Windows 上必然遇到） | 中 | 逐文件 try/except，失败写 `error_msg` 不中断；默认不跟随符号链接；长路径统一走 `\\?\` 前缀 |
| R5 | SQLite 多进程并发写冲突 | 中 | 中 | 单写者模型 + WAL；worker 只做解析与嵌入，回写集中在主进程；批量提交 |
| R6 | 模型 fp16 产出 NaN | 中 | 高 | 配置层硬性禁止 float16，加载后做 NaN 断言（廉价且能救命） |
| R7 | HNSW 频繁删除导致图退化、召回下降 | 中 | 中 | tombstone 标记 + 阈值触发压缩重建；`lsearch status` 暴露碎片率 |
| R8 | FTS5 在目标 Python 构建里未启用 | 低 | 高 | P0 检查 `sqlite3` 编译选项；回退 `apsw` |
| R9 | 中文 2 字词检索失效 | 中 | 中 | jieba 预分词方案（见 §4.4）；备选 trigram 并文档化其限制 |
| R10 | 索引库膨胀占满磁盘（D 盘剩 657G，充裕但非无限） | 中 | 中 | `data_dir` 改一处即可迁到 M 盘（managed 区块自动更新）；`status` 显示占用与增长趋势；`maintenance` 提供 vacuum/compact |
| R18 | 模型缓存所占的 **C 盘**（现有 `HF_HOME`，已 14GB）随多模态阶段继续增长 | 低 | 低 | C 盘剩 135G，v1 只增 1–3GB，够用。风险属于"别的项目在同一个 `HF_HOME` 里堆模型"而非本项目；`doctor` 顺带报告该目录占用，早发现 |
| R11 | 模型权重再分发/商业使用条款 | 低 | 低 | Apache 2.0、已取消 gating；自用无障碍，若将来分发需复核权重卡 |
| R12 | 单个超大表格/文档造出上万切片，吃掉数小时索引时间 | 中 | 中 | D8 的每文档 200 片上限 + 截断标注；`--max-chunks-per-doc` 按需放开；`--dry-run` 提前暴露"切片数 Top 20 的文件" |
| R13 | PyMuPDF 为 AGPL-3.0 | 低 | 低（仅当分发时升高） | v1 自用无需处理；接口隔离在 `extract/office.py`，将来换 pypdf 或买商业许可的改动面很小 |
| R14 | `--prune` 在某个卷不可达时（E 盘没插、M 盘离线）**误删该卷全部索引** | 中 | **高** | 只在**本次运行真正遍历到的卷**内执行删除；卷不可达则跳过并在 `status` 中标注。这是 Recoll 的 purge-pass 设计（§4.7） |
| R15 | MFT 后端需管理员权限，或遇到非 NTFS 卷（exFAT / U 盘 / 网络盘） | **高** | 中 | `backend = "auto"` 先探测可用性再启用，失败静默回退 `scandir`；`doctor` 明确报告当前实际走的是哪条路径 |
| R16 | 增量索引在容器格式上出错：压缩包内文档、带附件的 eml 在增量扫描时被误删 | 中 | 中 | sist2 有过完全相同的 bug（§4.7）。容器内条目单独建记录并绑定父文件指纹，父文件未重扫时绝不动子条目 |
| R17 | USN Journal 被重置或回绕（磁盘清理工具、journal 被删/调整尺寸） | 中 | 中 | 持久化 `journal_id` + 最后处理的 USN；发现 id 变化或记录越界即回退全量扫描并提示，**不静默做增量** |
| R19 | 计划任务静默失败：几周后才发现索引早已停止推进 | 中 | 中 | §4.8 的三层可观测性；退出码只对真失败非零，所以排障不会被"有文件读不了"的噪音淹没；**P6 让 `lsearch status` 直接显示计划任务的上次运行结果** |
| R20 | 计划任务改以 `S4U`/`SYSTEM` 运行后 `HF_HOME` 丢失，模型被静默重新下载一份到 `data_dir\models\hf`（§3.3 的例外失效） | 低 | 中 | 默认 `InteractiveToken`；启动器在 `HF_HOME` 缺失时写 WARNING；切换后用 `lsearch doctor` 复核生效值 |

---

## 7. 决策清单

### 7.1 已确定（你已拍板，不再变动）

| # | 决策项 | 结论 | 出处 |
|---|---|---|---|
| 1 | 包管理 / Python | uv；`.venv` 建在项目根；Python 3.12（P0 验证不通过则回退 3.11） | §3.3 |
| 2 | 项目名 / CLI 名 | 项目 `localsearch`，命令 `lsearch` | §4.6 |
| 3 | 嵌入维度 | **768 原生维 + int8 量化** | D5 |
| 4 | PDF 提取 | **PyMuPDF** | D6 |
| 5 | CLI 形态 | **一次性命令 + 交互 REPL** | D7 |
| 6 | 大文档切片 | **每文档 200 片上限 + 截断标注** | D8 |
| 7 | 索引范围 | 白名单默认 + `--all-drives` 开关 | D3 |
| 8 | 数据目录 | `D:/localsearch-data/`，自动进入排除列表 | §3.3 |
| 9 | 排除规则 | 项目根 `.lsearchignore`（已生成，你可随时改） | §4.5.1 |
| 10 | 中文检索 | jieba 预分词 + unicode61 | §4.4 |
| 11 | 融合算法 | RRF (k=60)，v1 不做 cross-encoder 重排 | §4.2 |
| 12 | 离线 | 权重一次性预下载，此后 `HF_HUB_OFFLINE=1` | §3.3 |
| 13 | 嵌入后端 | ST 与 ONNX int8 **P0 双跑实测后定** | D9 |
| 14 | 索引就绪策略 | **三级就绪** Tier0/1/2，各级独立可用 | D10 |
| 15 | 文件枚举 | **可替换扫描后端**，MFT 为可选加速 | D11 |
| 16 | 内容去重 | **内容哈希去重 + 嵌入输入级缓存** | D12 |
| 17 | 模型缓存位置 | **尊重系统已有 `HF_HOME`**（`C:\Users\luopu\.cache\huggingface`，已含 14GB 模型），仅在缺失时兜底到 `data_dir` | §3.3 |
| 18 | 自动增量方式 | **Windows 计划任务**（非常驻服务），定义与启动器已落盘，**注册与验证推迟到开发完成之后** | §4.8、§7.2 d |
| 19 | 进度记录 | 开发与测试进度持续记录在 `docs/PROGRESS.md`，与本计划互为对照 | §10 |

### 7.2 还差你一句话的小决策

这些我都不打算再追问，**按"默认做法"执行**；不认同哪条就单独说一声：

| # | 事项 | 我的默认做法 | 影响面 |
|---|---|---|---|
| a | 配置文件位置 | 挪到项目内 `config/config.toml`（而非 `%APPDATA%`），与"数据集中"的偏好一致 | 零功能影响，只关系到"有没有文件落在项目外" |
| b | 隐藏/系统属性文件 | **不**按 Windows 文件属性跳过，只按 `.lsearchignore` 的名称与路径规则排除 | 有些用户把重要目录设为隐藏；按属性跳过容易漏掉真正要找的东西 |
| c | CLI 输出语言 | 中文 | 纯观感 |
| d | ~~夜间自动增量~~ | **配置已落盘，注册与验证推迟到开发完成后**（见 §4.8）；权限档默认 `LeastPrivilege` + `scandir`，要更快可改 XML 一行换 `HighestAvailable` + MFT/USN | 从"要不要做"变成"用哪种权限档" |
| e | 单文件大小上限 | `max_file_mb = 64`，超过只索引文件名 | 影响大 PDF / 大日志的正文覆盖度 |
| f | 分块参数 | 512 tokens / 15% 重叠 | 切片粒度与索引体积 |

> d 项的默认是 **`LeastPrivilege`**。要换档改 XML 里一行（`<RunLevel>`），理由与代价见 §4.8。

### 7.3 需要你提供的输入（这不是选择题）

**A4 验收用的评测集**：20 条人工标注查询 + 期望命中的文件或片段。这个我造不出来——只有你知道自己平时想搜什么，用我编的查询去证明"搜得准"是自欺欺人。两种拿法：

- **你手写 20 条**，最好覆盖：精确关键词、模糊语义描述、中文 2 字词、代码标识符、PDF 内正文、跨盘按文件名找
- **或者等 P2 建好小范围索引后，我基于你自己的语料生成 40 条候选查询，你筛掉一半**

建议第二种：省你时间，且分布更贴近真实使用。

### 7.4 依赖清单（P0 验证后锁定版本）

`torch`(CPU)、`sentence-transformers`、`transformers`、**`onnxruntime`**、`faiss-cpu`、`numpy`、`jieba`、`charset-normalizer`、`typer`、`rich`、`PyMuPDF`、`python-docx`、`openpyxl`、`python-pptx`、`beautifulsoup4`、`lxml`、`extract-msg`、`pillow`

**仅在 P0 验证通过时才加入**（不通过则不引入，绝不成为 v1 的拦路依赖）：

- `mftparser`（或退回 `pywin32` + ctypes 直接调 `DeviceIoControl`）—— MFT 枚举与 USN Journal，D11 的加速后端
- `onnxruntime` 已在上表：它是 D9 双跑的必要项，无论最终选谁都要先装上做对比

> 变更说明：`pypdf` 已按 D6 移除（改用 PyMuPDF）；新增 `charset-normalizer`——中文 Windows 语料里 GBK/GB18030 老文件占比很高，不做编码探测会整片乱码；`rich` 用于表格输出与 REPL 高亮。

---

## 8. 建议的执行顺序（P0 的价值）

本计划刻意把 **P0 可行性验证**放在最前面，原因是：D1（sentence-transformers）与 D2（FAISS/hnswlib）这两条决策，在你这台纯 CPU、Windows、py3.12 的机器上存在真实的不确定性。先花半天把它们跑实，再动手写核心代码，可以避免写完三千行才发现推理链路要换。

**P0 的三条出口**，任一结果都会同步给你再决定是否调整 D1/D2：

1. 全部跑通 → 按本计划执行
2. 依赖装不上或吞吐过低 → 讨论切换到 ONNX int8 后端或调整维度/语料范围
3. FTS5 或向量库在 Windows 上不可用 → 调整 D2 方案

---

## 9. 下一步

请你做两件事：

1. 审阅本计划，特别是 **§6 风险表**、**§7 待确认默认值（尤其第 3、6 条）**
2. 确认无误后回复"开始"，我将从 **P0 可行性验证** 入手，并在每一步把实测结果反馈给你，再进入 P1

在此之前，我不会创建任何文件或安装任何依赖。

---

## 10. 进度记录约定

**开发与测试进度持续记录在 [PROGRESS.md](PROGRESS.md)。** 本计划是承诺，那份文件是事实；两者冲突时以 PROGRESS.md 为准，并回头修正本计划。

分工的理由：计划一旦写完就不该频繁改动，否则"当初打算怎么做"这个信息就丢了；而进度必须随时可查。混在一个文件里，两者都会被稀释。

### 写什么

**只记三类信息：**

1. **实测数字** —— 特别是把本计划里标注"二手信息""待验证""未实测"的说法换成事实。PROGRESS.md §3 已经把这些待验证项列成了一张表（M1–M10、D1–D2），P0 的任务就是把它填满
2. **与预期的偏差** —— 计划说 A，实际是 B，为什么
3. **无法立刻解决的问题** —— 留给未来的自己，避免重新踩一遍

**不写什么**：流水账。"今天写了 scanner.py"这种记录没有任何价值，一周后没人会看。判断标准是——**这条信息能不能改变将来的某个决定？** 不能就别写。

### 何时写

| 时机 | 动作 |
|---|---|
| 每完成一个阶段 | 更新 §1 状态总览、填写该阶段小节、追加 §6 变更日志 |
| 每次测量 | **立刻**填进 §3，不要让数字只留在终端里 |
| 每次跑测试 | 记进该阶段「测试记录」与 §5 验收对照 |
| 遇到意外 | 写进该阶段「与计划的偏差」。**只记成功等于没记** |
| 决定变更 | 追加 §6，并回改本计划的对应条目 |

### 一条硬性要求

PROGRESS.md 里每阶段的「与计划的偏差」和「遗留」两栏**不允许留空**。没有就明确写"无"——留空和"确认过确实没有"是两回事，前者会让疏漏长期隐身。
