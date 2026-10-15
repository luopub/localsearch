"""纯文本与代码（`F3-01`）。四类提取器里最简单的一类：字节 → 文本，中间只有编码。

------------------------------------------------------------------------------
这一层故意"什么都不做"
------------------------------------------------------------------------------

不做的事，逐条写下理由——它们每一条都有人会想顺手加上：

- **不归一化换行符**。`\r\n` 原样留着。`Chunk.char_start` / `char_end` 是**在正文
  上的下标**（`core/models.py` 的约定），把 `\r\n` 折叠成 `\n` 会让每一行的偏移往后
  少 1，而"跳到这一段的原文位置"正是这套偏移存在的唯一理由。代价是切片文本里带
  `\r`，对 FTS、对 token 计数都无害。
- **不做行尾空格清理、不做空行折叠**。这些都是"让正文更好看"，代价是同一份内容在
  不同版本下算出不同的切片——而 `A9` 靠内容哈希复用向量，**切片一变，缓存全废**。
- **不设 title**。纯文本没有标题这个概念，文件名不是标题（`core/models.py` 里写了
  理由：把文件名塞进标题会让改名后的同一份内容复用不上向量缓存）。
- **不切分、不摘要、不截断**。`D8` 的 200 片上限是 `chunk/` 的事，提取器只管把
  正文原样端出来。

------------------------------------------------------------------------------
无扩展名的文件：接住，但先嗅一下
------------------------------------------------------------------------------

`README` / `Makefile` / `LICENSE` / `Dockerfile` / `.gitignore` 这类**真的没有扩展名**
的文本文件在盘上很常见，而且是会有人去搜的那一类。所以 `ext`` 为空时**认领**，
但 `extract()` 里要先过 `looks_binary()`——不认领的话它们一条都搜不到，而随手认领
的话每一个无扩展名的二进制都会变成一段乱码正文。

被判成二进制时抛 `BinaryContent`（是**失败**，会带原因进报表），不是"悄悄返回空"：
后者与"这是个 0 字节文件"完全分不开。

⚠ 这条兜底**只覆盖无扩展名**。有扩展名但不在名单里的（`.bin` / `.dat` / `.db` /
`.exe`）一律 `UnsupportedFormat`，**不**去嗅探内容再决定——那等于让"这个文件里有
没有 NUL"决定它进不进全文索引，是一条没法预测、也没法向用户解释的规则。
"""

from __future__ import annotations

from localsearch.core.models import ExtractedDoc

from .base import (
    BinaryContent,
    ExtractConfig,
    Extractor,
    decode_bytes,
    looks_binary,
    read_bytes,
    with_warning,
)

#: 认领的扩展名。分组只是给人看的，代码不解析注释。
#:
#: ⚠ **不含** `.html` / `.htm` / `.xhtml` / `.mht` / `.mhtml` / `.eml` / `.msg`
#: （`web_mail` 的）、也不含 `.pdf` / `.docx` / `.xlsx` / `.pptx`（`office` 的）。
#: 两边都声明的话，谁在前面决定了结果，而那种冲突是**静默**的——症状是"HTML 里的
#: 标签全成了正文"。所以这里逐条列清楚，宁可漏报也不与别人抢。
PLAINTEXT_EXTS = frozenset({
    # 纯文本
    "txt", "text", "log", "md", "markdown", "mdx", "rst", "adoc", "asciidoc",
    "org", "tex", "latex", "bib", "srt", "vtt", "ass", "sub", "po", "pot",
    # 结构化文本
    "csv", "tsv", "psv", "json", "jsonl", "ndjson", "yaml", "yml", "toml",
    "ini", "cfg", "conf", "properties", "env", "xml", "xsl", "xslt", "dtd",
    "plist",
    # 代码
    "py", "pyi", "pyw", "js", "mjs", "cjs", "ts", "mts", "cts", "tsx", "jsx",
    "vue", "svelte", "astro", "java", "kt", "kts", "scala", "groovy", "clj",
    "cljs", "cljc", "edn", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx",
    "cs", "go", "rs", "rb", "rake", "gemspec", "php", "pl", "pm", "lua", "r",
    "jl", "swift", "m", "mm", "dart", "ex", "exs", "erl", "hrl", "hs", "lhs",
    "ml", "mli", "fs", "fsx", "fsi", "vb", "pas", "asm", "s", "f90", "f95",
    # 脚本与配置
    "sh", "bash", "zsh", "ksh", "fish", "ps1", "psm1", "psd1", "bat", "cmd",
    "awk", "sed", "diff", "patch", "reg", "sql", "graphql", "gql", "proto",
    # 证书与密钥（**只索引它们的文本形式**：PEM 的 base64 是纯 ASCII）
    "pem", "crt", "cer", "key", "pub", "csr", "asc", "gpg",
    # 样式（也是文本）
    "css", "scss", "sass", "less", "styl", "pcss",
})


class PlaintextExtractor:
    """字节 → 文本，中间只夹一层编码判定（`base.decode_bytes`）。"""

    name = "plaintext"

    def __init__(self, config: ExtractConfig) -> None:
        self._config = config

    def supports(self, ext: str, mime: str) -> bool:
        """名单里的扩展名，或者**没有扩展名**（理由见模块文档）。

        `mime` 一个字符都不用——`base.mime_of()` 的头一段写了为什么（Windows 上它读
        注册表，"本机装过什么软件"不能决定索引结果）。
        """
        del mime
        return (not ext) or (ext in PLAINTEXT_EXTS)

    def extract(self, path: str) -> ExtractedDoc:
        data = read_bytes(path, config=self._config)
        if looks_binary(data):
            raise BinaryContent(
                "内容里有 NUL 字节，按二进制处理（无扩展名的文件才走这条判定；"
                "名单内的扩展名不会走到这里）"
            )
        decoded = decode_bytes(data)
        return with_warning(
            ExtractedDoc(
                text=decoded.text,
                title=None,
                meta={
                    "encoding": decoded.encoding,
                    "bytes": len(data),
                    "chars": len(decoded.text),
                },
                modality="text",
                source=self.name,
            ),
            decoded.warning,
        )


def build(config: ExtractConfig) -> Extractor | None:
    """按 `[extract] text_code` 决定建不建；关掉时返回 `None`。

    关掉时**不注册**而不是"注册了但 `extract()` 返回空正文"：前者让这一类文件明确
    落到 `UnsupportedFormat`（`P4` 据此把它们留在 `NAME_ONLY`，文件名照搜），后者会
    造出一批"提取成功但正文是空的"记录——那是最难查的一种，因为每一层都觉得自己没错。
    """
    if not config.text_code:
        return None
    return PlaintextExtractor(config)
