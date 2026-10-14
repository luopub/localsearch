r"""`.lsearchignore` 解析与两层分区（P2 / `F2-03`）。测试落点 `tests/unit/test_filters.py`。

规则文件的**唯一真相来源**是仓库根的 `.lsearchignore`——那个文件的头部写了一整段
语法说明，本文件是**照着那段说明写的**，不是反过来。改这里的语义之前先改那个文件，
否则用户读到的规则和他实际得到的行为会分叉。

------------------------------------------------------------------------------
八条语义，每条都对应一种"不说清就会静默做错"的情形
------------------------------------------------------------------------------

**1. 两个分区，`#@section skip` 与 `#@section name-only`。**
   `skip` = 完全跳过，连文件名都搜不到；`name-only` = 只记文件名与路径，不做正文
   提取与向量化。分区是**规则自带**的，不是按文件类型推出来的。

**2. 第一个 `#@section` 标记之前的规则一律算 `skip`。**
   这是选出来的默认值，不是想当然：这个文件里除了 `# 注释`，每一行本来就在说
   "不要什么"，`skip` 是"排除"的传统含义。反过来默认成 `name-only`，会把一条本想
   彻底排除的 `^C:/Windows` 变成"索引全盘系统文件名"的指令——错得更响。这条**写进
   了 `.lsearchignore` 的头部**，因为用户有权知道。

**3. 后写的规则优先（last match wins），`!` 例外同样遵守。**
   于是"先大致排除、再点名放开"按阅读顺序生效，与 `.gitignore` 的直觉一致。
   这与"父目录被排除后子项不可救回"不冲突：后者是 walk 层剪枝的结果，剪掉的子树
   根本不会再被问到。

**4. `name/` 只匹配目录，`name` 两者都匹配。**
   带尾斜杠是"目录规则"，不带就是"名字规则"。于是 `node_modules/` 不会误伤一个
   叫 `node_modules` 的文件，而 `pagefile.sys` 两样都排。

**5. 一律大小写不敏感。**
   NTFS 是大小写不敏感的。规则里写 `^C:/Windows` 而盘上是 `C:/WINDOWS` 时，敏感
   匹配会**静默**放行整棵系统目录树——正是本文件要防的那类失败。

**6. 三类写法，三种匹配方式。**
   `^C:/x` 绝对前缀（在路径边界收口，故 `^C:/Windows` **不**匹配 `C:/WindowsOld`）；
   含 `/` 的相对路径（相对某个扫描根）；其余按名字。不带 `^` 但形如 `D:/x` 的一律
   按绝对路径处理——去掉前导 `/` 之后它没有第二种读法。

**7. `decide()` 与 `explain()` 是两个入口，不是一个加参数。**
   walker 逐层下问，每层只需看**自己这一层**，被剪掉的子树永远不会被问到；而
   `explain <path>` 面对的是一个已经拿到的路径，必须自己把祖先补上。把两者塞进
   同一个函数，只会得到"快路径做多余的祖先遍历、慢到不能用"或者"explain 忘了看
   祖先、给出与真实索引不一致的答案"。`test_decide_and_explain_agree` 用语料核对
   两者给同一个答案。

**8. 匹配有快慢两条路，快路径必须与逐条全扫给出**逐条相同**的结果。**
   全盘 340 万条目 × 350 条规则跑正则要几分钟，纯属浪费：这个文件里绝大多数规则是
   **字面量**（`node_modules/`、`pagefile.sys`、`^C:/Windows`），可以走 dict 与
   `startswith` 命中，只有真通配的才落到正则。索引路径与逐条全扫路径
   （`_decide_reference`，测试里保留）对同一语料比对，防止"优化顺手改了语义"——
   那是这类代码最典型的坏结局。
"""

from __future__ import annotations

import enum
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# `src/localsearch/ingest/filters.py` → parents: [ingest, localsearch, src, 仓库根]
PROJECT_ROOT = Path(__file__).resolve().parents[3]

SECTION_SKIP = "skip"
SECTION_NAME_ONLY = "name-only"
SECTIONS: tuple[str, ...] = (SECTION_SKIP, SECTION_NAME_ONLY)
#: 见语义第 2 条。
DEFAULT_SECTION = SECTION_SKIP

SECTION_MARKER = "#@section"
MANAGED_BEGIN = "#@begin managed"
MANAGED_END = "#@end managed"
#: managed 区块里的那一行说明。由工具生成而不是留着让人手写——否则它下一次
#: 运行就会被覆盖掉，而"我写的注释怎么没了"是个查不出原因的现象。
MANAGED_NOTE = "# 内容 = config 里的 paths.data_dir + 项目自身的 .venv"

_GLOB_META = frozenset("*?[")


class IgnoreError(Exception):
    """规则文件本身写错了。不是"匹配不上"，是没法解释——必须报出来。"""


class Action(enum.Enum):
    """一条路径的最终处置。取值顺序就是"信息保留量"从少到多。"""

    SKIP = "skip"
    NAME_ONLY = "name-only"
    INDEX = "index"

    @property
    def label(self) -> str:
        return {
            Action.SKIP: "完全跳过",
            Action.NAME_ONLY: "只索引名字",
            Action.INDEX: "正常索引",
        }[self]


# ==============================================================================
# 规范化
# ==============================================================================


def normalize(path: str | Path) -> str:
    """把路径或规则体统一成"小写 + 正斜杠 + 无尾斜杠"的比较形式。

    自己换反斜杠而不用 `PurePath.as_posix()`，是因为输入里可能混着**设备路径**
    （`\\\\?\\C:\\...`，R4 的长路径），后者交给 `Path` 会被解释成一个奇怪的 UNC 名字。
    这里只在字符串层面统一。
    """
    text = str(path).replace("\\", "/")
    for prefix in ("//?/", "//./"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = posixpath.normpath(text)
    if text == ".":
        text = ""
    # normpath 会把 `c:/` 收成 `c:`：正好，前缀比较时 `c:` + "/" 才成立
    return text.lower()


def _basename(norm: str) -> str:
    return norm.rsplit("/", 1)[-1]


def _relative_to_any(norm: str, roots: Sequence[str]) -> str | None:
    """norm 相对某个扫描根的路径；不在任何根下时返回 `None`。

    没有根时相对路径规则无从解释——这不是错误，是"这条规则此刻用不上"，所以返回
    `None` 而不是抛异常。
    """
    for root in roots:
        if not root:
            continue
        if norm == root:
            return ""
        if norm.startswith(root + "/"):
            return norm[len(root) + 1 :]
    return None


# ==============================================================================
# glob → 正则
# ==============================================================================


def _glob_regex(pattern: str) -> str:
    """把 glob 片段翻成正则片段。

    `*` 不跨 `/`，`**` 跨任意层级（`**/` 还允许零层），`?` 是单个非 `/` 字符，
    `[abc]` / `[!abc]` 是字符类。其余字符字面处理——`$`、`.`、`(` 在 glob 里没有
    特殊含义，必须转义，否则 `^C:/$Recycle.Bin` 会变成"行尾锚 + Recycle"。
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                if pattern[i + 2 : i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            start = i + 2 if pattern[i + 1 : i + 2] in ("!", "^") else i + 1
            close = pattern.find("]", start)
            if close == -1:
                # 没有闭合的 `[`：按字面量。静默当成字符类会让规则"看起来写了"
                # 却匹配不到任何东西。
                out.append(r"\[")
                i += 1
            else:
                inner = pattern[i + 1 : close]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _literal_ends(pattern: str) -> tuple[str, str]:
    """取出字面前缀与字面后缀，供快路径在跑正则之前先做一次廉价排除。

    这两个值只用来**提前排除**，不参与判定——所以即使取不精确也不会判错。
    """
    idx = len(pattern)
    for i, ch in enumerate(pattern):
        if ch in _GLOB_META:
            idx = i
            break
    j = len(pattern)
    # 后缀不能跨过 `]`：`[abc]x.txt` 的尾巴是 `x.txt`，不是 `abc]x.txt`。
    # 取错了不会报错，只会让**这条规则永远不命中**——静默失效，正是本文件要防的。
    while j > idx and pattern[j - 1] not in _GLOB_META and pattern[j - 1] != "]":
        j -= 1
    return pattern[:idx], pattern[j:]


def _simple_ext(pattern: str) -> str | None:
    """`*.ext` 这种只有一处通配的扩展名规则，取出 `ext` 供字典命中。

    这个文件里 120 多条规则是这种形状，一次字典查找就能命中，不值得跑正则。
    """
    if not pattern.startswith("*."):
        return None
    ext = pattern[2:]
    if not ext or any(c in _GLOB_META for c in ext):
        return None
    return ext


# ==============================================================================
# 规则
# ==============================================================================


@dataclass(frozen=True)
class Rule:
    """一条规则。

    `line` 是它在文件里的行号（从 1 起）。保留它有两个硬理由：`--dry-run` 要按规则
    报告命中数，而"第 39 行 `^D:/localsearch-data`"是用户唯一能对上号的标识；
    `explain` 也要能指出**是哪一行**排除了这个文件。没有行号的规则无法被讨论。
    """

    line: int
    raw: str
    pattern: str
    negated: bool
    section: str
    kind: str  # "abs" | "path" | "name"
    dir_only: bool
    regex: re.Pattern[str] | None = None
    prefix: str = ""
    suffix: str = ""

    @property
    def origin(self) -> str:
        """给 `explain` 与 `--dry-run` 用的一行人话。"""
        if self.line == 0:
            return self.raw  # 命令行临时覆盖，没有行号
        return f"第 {self.line} 行 {self.raw}"

    def _glob_hit(self, text: str) -> bool:
        assert self.regex is not None
        if self.prefix and not text.startswith(self.prefix):
            return False
        if self.suffix and not text.endswith(self.suffix):
            return False
        return self.regex.fullmatch(text) is not None

    def matches(
        self,
        norm: str,
        *,
        base: str,
        is_dir: bool | None,
        roots: Sequence[str],
        ancestors: bool,
    ) -> bool:
        """`norm` 是否命中本规则。

        `is_dir = None` 表示"不知道"（`explain` 面对不存在的路径时会出现），此时按
        "可能是个目录"处理：宁可多报一条原因，也不要把真实原因藏起来。
        """
        if self.kind == "abs":
            return self._abs_hit(norm)
        if self.kind == "path":
            rel = _relative_to_any(norm, roots)
            if not rel:
                # 不在任何扫描根下，或路径就是根自己：相对路径规则无从谈起。
                # 根自己不该被规则排除——排除根等于"什么都不索引"，那是用户的
                # 配置错误，不是这里该静默处理的事。
                return False
            segs = rel.split("/")
            if not self.dir_only:
                return self._glob_hit(rel)
            limit = len(segs) if is_dir is not False else len(segs) - 1
            return any(self._glob_hit("/".join(segs[:i])) for i in range(1, limit + 1))
        if ancestors:
            comps = norm.split("/")
            if self.dir_only and is_dir is False:
                comps = comps[:-1]
        else:
            if self.dir_only and is_dir is False:
                return False
            comps = [base]
        return any(self._glob_hit(c) for c in comps if c)

    def _abs_hit(self, norm: str) -> bool:
        """绝对前缀规则。没有通配时走 `startswith`，并在路径边界处收口。"""
        if self.regex is None:
            if norm == self.pattern:
                return True
            return norm.startswith(self.pattern) and norm[len(self.pattern) :].startswith(
                "/"
            )
        return self.regex.match(norm) is not None


def _parse_pattern(
    raw: str, *, line: int, section: str, negated: bool
) -> Rule:
    body = raw.strip()
    dir_only = body.endswith("/")
    body = body.rstrip("/")
    if not body:
        raise IgnoreError(
            f"第 {line} 行 {raw!r} 解析不出任何路径：'/'、'!'、'!/' 都不是规则"
        )

    if body.startswith("^"):
        kind, pattern = "abs", body[1:]
    elif re.match(r"^[a-zA-Z]:[/\\]", body.lstrip("/")):
        # `D:/x` 与 `/D:/x` 都按绝对路径处理：去掉 `^` 或前导 `/` 之后，它仍然是一个
        # 盘符绝对路径，没有第二种读法。
        kind, pattern = "abs", body.lstrip("/")
    elif "/" in body:
        kind, pattern = "path", body.lstrip("/")
    else:
        kind, pattern = "name", body

    pattern = pattern.replace("\\", "/").lower()
    base_kwargs = {
        "line": line,
        "raw": raw,
        "negated": negated,
        "section": section,
        "dir_only": dir_only,
        "kind": kind,
    }

    if kind == "abs":
        norm_pattern = normalize(pattern)
        if not any(c in _GLOB_META for c in norm_pattern):
            return Rule(pattern=norm_pattern, **base_kwargs)
        rx = _glob_regex(norm_pattern)
        if not norm_pattern.endswith("/"):
            rx += "(?:/|$)"
        return Rule(pattern=norm_pattern, regex=re.compile(rx), **base_kwargs)

    prefix, suffix = _literal_ends(pattern)
    return Rule(
        pattern=pattern,
        regex=re.compile(_glob_regex(pattern)),
        prefix=prefix,
        suffix=suffix,
        **base_kwargs,
    )


# ==============================================================================
# 结果
# ==============================================================================


@dataclass(frozen=True)
class Decision:
    """一条路径的处置，以及**为什么会是这个处置**。

    带上 `rule` 是刻意的：`--dry-run` 的逐规则命中数与 `explain` 的"被哪一条排除"
    要的都是规则本身，而不是动作。只回一个 `Action`，那两件事就得各自再匹配一遍，
    于是"算出来的原因"与"实际生效的原因"迟早分叉。
    """

    action: Action
    rule: Rule | None = None
    inherited: Action | None = None

    @property
    def reason(self) -> str:
        if self.rule is None:
            if self.inherited is not None:
                return f"无规则命中；继承上级的「{self.inherited.label}」"
            return "无规则命中"
        if self.rule.negated:
            return f"白名单例外 {self.rule.origin}：取消先前的排除"
        return f"{self.rule.origin}（{self.rule.section}）"

    @property
    def matched(self) -> bool:
        return self.rule is not None


def _decision_of(rule: Rule) -> Decision:
    """规则 → 处置。`!` 例外一律给出"正常索引"，与它落在哪个分区无关。"""
    if rule.negated:
        return Decision(Action.INDEX, rule)
    action = Action.SKIP if rule.section == SECTION_SKIP else Action.NAME_ONLY
    return Decision(action, rule)


# ==============================================================================
# 反向索引（快路径）
# ==============================================================================


class _Index:
    """按"这条路径可能命中哪些规则"建的反向索引。

    每个桶只装**可能**命中的规则号，桶外的规则连比较都不做。这是语义第 8 条的落地：
    桶只影响快慢，不影响结果——判定永远由 `Rule.matches` 给出。
    """

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.exact: dict[str, list[int]] = {}
        self.ext: dict[str, list[int]] = {}
        self.glob: list[int] = []
        self.path_by_first: dict[str, list[int]] = {}
        self.path_any: list[int] = []
        self.abs_by_drive: dict[str, list[int]] = {}
        self.abs_any: list[int] = []

        for i, rule in enumerate(rules):
            if rule.kind == "name":
                if not any(c in _GLOB_META for c in rule.pattern):
                    self.exact.setdefault(rule.pattern, []).append(i)
                    continue
                ext = _simple_ext(rule.pattern)
                if ext is not None:
                    self.ext.setdefault(ext, []).append(i)
                else:
                    self.glob.append(i)
            elif rule.kind == "path":
                head = rule.pattern.split("/", 1)[0]
                if head and not any(c in _GLOB_META for c in head):
                    self.path_by_first.setdefault(head, []).append(i)
                else:
                    self.path_any.append(i)
            else:
                drive = rule.pattern[:2] if rule.pattern[1:2] == ":" else ""
                if drive:
                    self.abs_by_drive.setdefault(drive, []).append(i)
                else:
                    self.abs_any.append(i)

    def candidates(
        self, norm: str, base: str, rel: str | None, *, is_dir: bool
    ) -> list[int]:
        out: list[int] = []
        out += self.exact.get(base, ())
        dot = base.rfind(".")
        if dot > 0:
            out += self.ext.get(base[dot + 1 :], ())
        out += self.glob
        if rel:
            head = rel.split("/", 1)[0]
            out += self.path_by_first.get(head, ())
        out += self.path_any
        drive = norm[:2] if norm[1:2] == ":" else ""
        if drive:
            out += self.abs_by_drive.get(drive, ())
        out += self.abs_any
        # 去重后升序：调用方从后往前扫，正好就是"后写的规则优先"
        return sorted(set(out))


# ==============================================================================
# 规则集合
# ==============================================================================


class IgnoreRules:
    """一份解析好的规则集合。

    `roots` 是**扫描根**（规范化后的形式）：相对路径规则 `/doc/build/` 怎么解释完全
    依赖它。没有根时那类规则不参与匹配——`--dry-run` 会打印它选定的根，免得
    "规则一条都没命中"变成一个查不出来的现象。
    """

    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        roots: Iterable[str] = (),
        source: Path | None = None,
        extra_label: str = "",
    ) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.roots: tuple[str, ...] = tuple(normalize(r) for r in roots if str(r).strip())
        self.source = source
        self.extra_label = extra_label
        self._index = _Index(self.rules)

    # ---- 构造 ----------------------------------------------------------------

    @classmethod
    def empty(cls, *, roots: Iterable[str] = ()) -> "IgnoreRules":
        return cls((), roots=roots)

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        roots: Iterable[str] = (),
        source: Path | None = None,
    ) -> "IgnoreRules":
        rules: list[Rule] = []
        section = DEFAULT_SECTION
        for n, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(SECTION_MARKER):
                rest = stripped[len(SECTION_MARKER) :].strip()
                name = rest.split()[0] if rest else ""
                if name not in SECTIONS:
                    raise IgnoreError(
                        f"第 {n} 行：{SECTION_MARKER} 只认 {' / '.join(SECTIONS)}，"
                        f"实际是 {rest!r}"
                    )
                section = name
                continue
            if stripped.startswith("#"):
                continue
            negated = stripped.startswith("!")
            body = stripped[1:].strip() if negated else stripped
            rules.append(_parse_pattern(body, line=n, section=section, negated=negated))
        return cls(rules, roots=roots, source=source)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        roots: Iterable[str] = (),
        missing_ok: bool = False,
    ) -> "IgnoreRules":
        file = Path(path)
        if not file.is_file():
            if missing_ok:
                return cls.empty(roots=roots)
            raise IgnoreError(
                f"找不到规则文件 {file}。它是排除规则的唯一来源；当它不存在时"
                "只能按「什么都没排除」跑，那会把 .venv 和索引库自己一并索引进去，"
                "所以这里报错，而不是静默当成空规则"
            )
        return cls.parse(file.read_text(encoding="utf-8"), roots=roots, source=file)

    def with_extra(
        self, *, excludes: Sequence[str] = (), includes: Sequence[str] = ()
    ) -> "IgnoreRules":
        """`--include` / `--exclude` 的**临时**覆盖：不写回文件（计划 §4.6）。

        追加在最后，于是它们天然"后写优先"——命令行参数压过文件里的规则，这正是
        敲这一条时想要的顺序。这些规则 `line = 0`：它们不在文件里，那个行号是假的，
        `origin` 会改印原文。
        """
        extra: list[Rule] = []
        for raw in excludes:
            extra.append(_parse_pattern(raw, line=0, section=SECTION_SKIP, negated=False))
        for raw in includes:
            extra.append(_parse_pattern(raw, line=0, section=SECTION_SKIP, negated=True))
        if not extra:
            return self
        labels = []
        if excludes:
            labels.append(f"--exclude ×{len(excludes)}")
        if includes:
            labels.append(f"--include ×{len(includes)}")
        return IgnoreRules(
            (*self.rules, *extra),
            roots=self.roots,
            source=self.source,
            extra_label="、".join(labels),
        )

    # ---- 判定 ----------------------------------------------------------------

    def decide(
        self,
        path: str | Path,
        *,
        is_dir: bool,
        inherited: Action | None = None,
    ) -> Decision:
        """walker 用的判定：**只看这条路径自己**。

        祖先的处置由调用方通过 `inherited` 传进来——walker 逐层下问，它比谁都清楚
        上级是什么处置，不需要这里再猜一次。
        """
        norm = normalize(path)
        base = _basename(norm)
        rel = _relative_to_any(norm, self.roots)
        for idx in reversed(self._index.candidates(norm, base, rel, is_dir=is_dir)):
            rule = self.rules[idx]
            if rule.matches(
                norm, base=base, is_dir=is_dir, roots=self.roots, ancestors=False
            ):
                return _decision_of(rule)
        return Decision(inherited or Action.INDEX, None, inherited)

    def explain(self, path: str | Path, *, is_dir: bool | None = None) -> Decision:
        """解释一个**已拿到的**路径：把祖先一起看，给出最终生效的那条规则。

        与 `decide()` 故意分成两个入口（见语义第 7 条）。`is_dir=None` 时问一次
        文件系统；路径不存在则按"可能是目录"处理。
        """
        norm = normalize(path)
        if is_dir is None:
            try:
                is_dir = Path(norm).is_dir()
            except OSError:
                is_dir = None
        base = _basename(norm)
        last: Rule | None = None
        for rule in self.rules:
            if rule.matches(
                norm, base=base, is_dir=is_dir, roots=self.roots, ancestors=True
            ):
                last = rule
        if last is None:
            return Decision(Action.INDEX)
        return _decision_of(last)

    # ---- 报告 ----------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)

    def describe_source(self) -> str:
        parts = [str(self.source) if self.source else "（无规则文件）"]
        parts.append(f"{len(self.rules)} 条规则")
        if self.extra_label:
            parts.append(self.extra_label)
        if self.roots:
            parts.append("扫描根 " + "、".join(self.roots))
        return "，".join(parts)


def _decide_reference(rules: IgnoreRules, path: str, *, is_dir: bool,
                      inherited: Action | None = None) -> Decision:
    """逐条全扫的参考实现，**只给测试用**。

    语义第 8 条：快路径必须与它逐条一致。留在生产模块里（而不是藏在测试里）是为了
    让"参考实现"与"被参考的规则对象"永远待在一起——`Rule.matches` 一改，两者一起
    过时，测试立刻会红。
    """
    norm = normalize(path)
    base = _basename(norm)
    last: Rule | None = None
    for rule in rules:
        if rule.matches(norm, base=base, is_dir=is_dir, roots=rules.roots,
                        ancestors=False):
            last = rule
    if last is None:
        return Decision(inherited or Action.INDEX, None, inherited)
    return _decision_of(last)


# ==============================================================================
# managed 区块（计划 §4.5.1 实现要求第 1 条）
# ==============================================================================


def managed_entries(
    data_dir: str | Path, *, venv: str | Path | None = None
) -> list[str]:
    """managed 区块应有的**整块内容**（含那一行说明）。

    规则只有两条：`paths.data_dir` 与项目自身的 `.venv`。**故意只放这两样**——区块
    由工具生成，进去的每一条都必须是"工具自己知道的东西"；把用户手写的规则搬进来，
    下次运行就会被覆盖掉（那是本机制明码标价的代价，所以说明也由工具生成，
    免得它自己也变成会被覆盖的手写内容）。
    """
    venv_path = PROJECT_ROOT / ".venv" if venv is None else Path(venv)
    return [
        MANAGED_NOTE,
        "^" + Path(data_dir).as_posix(),
        "^" + venv_path.as_posix(),
    ]


def replace_managed_block(text: str, entries: Sequence[str]) -> str:
    """把 `#@begin managed` 与 `#@end managed` 之间的行换成 `entries`。

    区块**之外**的每一个字节原样保留——用户在这个文件里写的注释、分组与规则不许被
    动。找不到标记时**报错**：一个没有区块的文件说明它被换过或从别处拷来，这时候
    自作主张追加一个区块，比报错更难查。
    """
    lines = text.splitlines(keepends=True)
    begin = end = -1
    for i, line in enumerate(lines):
        if line.startswith(MANAGED_BEGIN):
            begin = i
        elif line.startswith(MANAGED_END) and begin >= 0:
            end = i
            break
    if begin < 0:
        raise IgnoreError(
            f"规则文件里没有 {MANAGED_BEGIN} 标记，无法确定自动区块的位置。"
            "这一块由仓库带过来，工具不会凭空插入。"
        )
    if end < 0:
        raise IgnoreError(
            f"第 {begin + 1} 行有 {MANAGED_BEGIN}，但后面找不到 {MANAGED_END}"
        )
    body = [entry + "\n" for entry in entries]
    return "".join(lines[: begin + 1] + body + lines[end:])


def update_managed_block(path: str | Path, entries: Sequence[str]) -> bool:
    """就地更新 managed 区块。返回**是否真的写了盘**。

    内容没变时不写：`mtime` 不动，用户对"这个文件什么时候被动过"的判断才可信。
    """
    file = Path(path)
    original = file.read_text(encoding="utf-8")
    updated = replace_managed_block(original, entries)
    if updated == original:
        return False
    file.write_text(updated, encoding="utf-8", newline="")
    return True


__all__ = [
    "Action",
    "DEFAULT_SECTION",
    "Decision",
    "IgnoreError",
    "IgnoreRules",
    "MANAGED_BEGIN",
    "MANAGED_END",
    "MANAGED_NOTE",
    "PROJECT_ROOT",
    "Rule",
    "SECTION_MARKER",
    "SECTION_NAME_ONLY",
    "SECTION_SKIP",
    "SECTIONS",
    "managed_entries",
    "normalize",
    "replace_managed_block",
    "update_managed_block",
]
