-- ==============================================================================
-- 索引库的表结构（P4 / F4-01 落地；F4-02 起正文那一层的写入走同一份）。
-- 真相来源：docs/DEVELOPMENT_PLAN.md §4.4 那张 SQL 块。本文件是它的落地形态，
-- 与那块 SQL 只有两处刻意的差异（都在下面就地注明）+ 一处形态差异（不写 PRAGMA）。
--
-- 为什么是 .sql 文件而不是 Python 里的一段字符串：
--   * 它要能被 `sqlite3 lsearch.db < schema.sql` 直接喂进去——排障时不想先写一段
--     Python 才能看到表长什么样；
--   * 语法高亮与 diff 都可读。夹在字符串里的 SQL 在 review 时是一坨。
-- ⚠ 代价：它是**包数据**，打进 wheel 要带上（`pyproject.toml` 的
--   `[tool.hatch.build.targets.wheel]` 现在是整个 `src/localsearch`，默认包含非 .py
--   文件）。F7-03 打包测试要盯住这一条。
--
-- ⚠ 本文件里**不写 PRAGMA**：`journal_mode = WAL` 与 `foreign_keys = ON` 是**连接**
--   的属性、不是库的属性（WAL 那个是库级的但设一次就落盘，混在里面会让"重新建库"
--   与"打开已有库"两条路径行为不同）。它们都在 `sqlite_store.py` 的 `_configure()`。
-- ==============================================================================

-- 版本与全局元信息（embed_ver 指纹、各卷最后扫描状态与 USN、Tier 进度）
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- 内容表（D12 去重）：一个内容实体只提取、分块、嵌入一次。
-- 同一份内容的多个副本（不同路径、备份、改名）共享同一行。
CREATE TABLE blobs (
  id           INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL UNIQUE,      -- 全文 sha256；仅对 size 重复的文件计算（§4.4.1）
  size_bytes   INTEGER NOT NULL,
  extractor    TEXT,                      -- 提取器名
  parser_ver   TEXT,                      -- 提取器版本，参与缓存键，防止旧解析结果被复用
  chunk_count  INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER
);

-- 文件表：增量判定与 Tier0 路径维度检索
--
-- ⚠ **与计划 §4.4 的唯一一处差异：`path` 上多了 `COLLATE NOCASE`。**
--   计划原文写的是 `path TEXT NOT NULL UNIQUE`，而 NTFS 的路径比较**不分大小写**：
--   `C:/Windows` 与 `c:/windows` 是同一个目录。少了这一条，同一棵树用两种大小写的
--   根扫进来会得到两行——而"两行"这件事不会报错，只会让计数偏大、让 `--prune`
--   删不干净（P6）。加 NOCASE 之后 `files.path` 仍然**存原样大小写**（显示、报错、
--   打开文件都靠它），只是"唯一"这一条按不分大小写来判。
--   ⚠ SQLite 的 NOCASE 只折叠 ASCII A–Z，不做 Unicode 大小写折叠；NTFS 做的是
--   完整的 Unicode 大写映射。差异只可能出现在**非 ASCII 文件名**上，而那种情况下
--   两个大小写写法要同时出现在同一棵树里，得先有人把根写成两种大小写。
--   记在 P4「遗留」里，不在这一版解决。
CREATE TABLE files (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL COLLATE NOCASE UNIQUE,  -- 原样大小写 + 正斜杠（见上方注）
  volume       TEXT NOT NULL,             -- 小写盘符 + 冒号（c: / d:）——与 ingest.base.drive_of 同一口径
  ext          TEXT,                      -- 不带点的小写扩展名；无扩展名是空串，不是 NULL
  mime         TEXT,
  size_bytes   INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  quick_sig    TEXT,                      -- 尺寸 + mtime + ctime 的摘要，带配方号前缀 "stat:1:"
                                          -- ⚠ P4 **一律留 NULL**：填它要给 ScanEntry 加 ctime_ns，
                                          --   而两个扫描器都没带这个字段（MFT 那侧要走
                                          --   GetFileAttributesExW 的 ftCreationTime，与本机 os.stat
                                          --   的 st_ctime_ns 是否相等**没测过**）。归 F6-02，见 P4「遗留」
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
-- 上面三条来自计划 §4.4。**这一条是落地时加的**：Tier1 那一趟的选择条件是
-- "tier 还没到目标 且 status 还要做"，没有索引就是每趟全表扫 100 万行；
-- 而 `idx_files_volume` 的前导列是 volume，帮不上这个查询。
CREATE INDEX idx_files_tier   ON files(tier, status);

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
