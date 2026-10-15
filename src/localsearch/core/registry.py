"""扩展点注册表：按注册顺序匹配的那张表（P3 起用，P5 的嵌入器复用）。

------------------------------------------------------------------------------
它不是"通用插件框架"，是三条约束
------------------------------------------------------------------------------

计划 §4.2 要的是"新提取器 / 嵌入器通过注册表接上，不改核心分发逻辑"。落到代码里
只有三句话：

1. **顺序即优先级**：第一个 `supports()` 说"归我"的提取器赢。所以注册顺序是**语义**，
   不是实现细节——`plaintext` 与 `web_mail` 都能声明认识 `.html` 的话，谁在前面
   决定了 HTML 是被当网页解析还是被当一坨带标签的文本。顺序只有一处说了算：
   `extract/build_registry()`（装配的地方），不在这里。
2. **名字必须唯一且非空**：重名就报错。名字唯一才让"这个文件的正文是哪个提取器出的"
   能在日志、报错、`doc.source` 里指回一个具体的东西；允许重名等于允许报表说谎。
3. **注册不返回东西、也不热插拔**：`register()` 只是往表尾放一条。没有反注册、没有
   生命周期、没有钩子——v1 用不上，加上去只会制造"某个实例到底在不在表里"的疑问。

------------------------------------------------------------------------------
为什么不用装饰器（import 副作用）注册
------------------------------------------------------------------------------

`@register_extractor` 这种写法看着更省事，代价是**匹配顺序变成"谁先被 import"的
函数**：某个模块少 import 一次，顺序就变了，而且**不报错**——症状是"HTML 突然被
当纯文本提取了"，出现在一个与本次改动毫无关系的文件里。这与 `filters.py` 那条
"不要靠顺序的默契"是同一个失败模式。显式 `register()` 在装配函数里排成几行，
顺序看得见、也 diff 得出来。
"""

from __future__ import annotations

from typing import Callable, Generic, Iterator, TypeVar

T = TypeVar("T")


class RegistryError(Exception):
    """注册表自己的错误：重名、空名、名字对不上。**不是**被注册对象的错误。"""


class Registry(Generic[T]):
    """有名字、有顺序、可反查的一张表。

    泛型是为了 `extract`（`Extractor`）与 `P5` 的 `embed`（`Embedder`）能共用同一
    份实现——两边的共同点就只有"按顺序找第一个认领的"。**不做**任何超出这个范围的
    抽象（不要依赖注入、不要懒加载工厂）：`build_registry()` 里显式造实例，出错的
    时候栈是直的。
    """

    __slots__ = ("_name", "_items")

    def __init__(self, name: str) -> None:
        #: 报错信息里带上它（"extractor 注册表里没有重名的……"），否则将来两个
        #: 注册表同时出错时，光看异常分不清是哪一个。
        self._name = name
        self._items: list[tuple[str, T]] = []

    @property
    def name(self) -> str:
        return self._name

    def register(self, name: str, item: T) -> T:
        """放进表尾（= 最低优先级），并把这个对象原样还回去。

        还回去是为了让调用方能写成 `reg.register("x", X(cfg))` 而不用先落一个变量；
        它**不**做"已经注册过就跳过"这种幂等处理——重复调用就是重复注册，重名会报错。
        """
        key = name.strip()
        if not key:
            raise RegistryError(f"{self._name} 注册表里不允许空名字：注册名要能在日志与报错里指回来")
        if any(existing == key for existing, _ in self._items):
            raise RegistryError(
                f"{self._name} 注册表里已经有叫 {key!r} 的了。"
                "名字重复会让「这个文件是谁提的」指到两个东西上，所以这里直接报错。"
            )
        self._items.append((key, item))
        return item

    def get(self, name: str) -> T:
        """按名字取，**取不到就报错**（不做"没有就返回 None"）。

        调用点全是"我确定它注册过"（比如配置里点名了某个后端），返回 None 只会把
        错误推到几百行之外变成一个 `AttributeError`。
        """
        for existing, item in self._items:
            if existing == name:
                return item
        raise RegistryError(
            f"{self._name} 注册表里没有 {name!r}；现有的是 {[n for n, _ in self._items]}"
        )

    def all(self) -> tuple[T, ...]:
        """按注册顺序的全部对象。"""
        return tuple(item for _, item in self._items)

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._items)

    def first_match(self, predicate: Callable[[T], bool]) -> T | None:
        """第一个满足 `predicate` 的对象；一个都没有就返回 `None`。

        **返回 `None` 而不是抛异常**，与 `get()` 的态度相反，这是有意的：这里的
        "没有"是一个**正常结果**（"没有提取器认领这个扩展名"），调用方各有各的处置
        ——`extract_file()` 把它变成一个带原因的失败，`lsearch explain` 可能只想显示
        一行"没有提取器"。抛异常会把一个正常分支变成控制流。
        """
        for _, item in self._items:
            if predicate(item):
                return item
        return None

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self.all())

    def __repr__(self) -> str:  # pragma: no cover - 只为调试时好看
        return f"Registry({self._name!r}, {list(self.names())})"
