"""`core/registry.py`：扩展点注册表的四条约定。

这一组测试盯的**不是**"能存取"，而是那三条会被后来人无意破坏的约定：

1. **顺序即优先级**——`first_match` 必须给先注册的那个。这条最容易被破坏的方式是
   "顺手把 `_items` 换成 `dict` 或 `set`"，那样顺序就成了插入顺序的偶然（`set` 连
   插入顺序都没有），而症状是"HTML 被当纯文本提取了"，出现在别的模块里。
2. **重名报错**——允许重名等于允许 `doc.source` 指到两个东西上。
3. **`get()` 抛、`first_match()` 返回 `None`**——两个相反的约定是刻意的（前者是
   "我确定它注册过"，后者是"没人认领"这个正常结果），测试把它们钉在一起，
   免得将来有人"统一"成一种。

`Registry` 是泛型，测试用字符串当元素——它的行为与元素类型毫无关系，用替身类型
反而能把"注册表自己不碰元素"这条看清。
"""

from __future__ import annotations

import pytest

from localsearch.core.registry import Registry, RegistryError


def test_registration_order_is_the_priority_order():
    reg: Registry[str] = Registry("demo")
    reg.register("first", "A")
    reg.register("second", "B")

    assert reg.first_match(lambda item: True) == "A"
    assert reg.first_match(lambda item: item == "B") == "B"


def test_a_later_registration_can_never_shadow_an_earlier_one():
    """两个都认领时，先注册的那个赢——这条就是 `build_registry()` 的顺序有意义的原因。"""
    reg: Registry[str] = Registry("demo")
    reg.register("broad", "先注册的、什么都认领的")
    reg.register("narrow", "后注册的、只认领 B 的")

    got = reg.first_match(lambda item: True)

    assert got == "先注册的、什么都认领的"


def test_all_and_names_keep_the_registration_order():
    reg: Registry[str] = Registry("demo")
    reg.register("a", "A")
    reg.register("b", "B")
    reg.register("c", "C")

    assert reg.all() == ("A", "B", "C")
    assert reg.names() == ("a", "b", "c")
    assert list(reg) == ["A", "B", "C"]
    assert len(reg) == 3


def test_register_hands_the_object_back():
    reg: Registry[str] = Registry("demo")
    assert reg.register("a", "A") == "A"


def test_duplicate_names_are_refused():
    reg: Registry[str] = Registry("demo")
    reg.register("a", "A")

    with pytest.raises(RegistryError) as excinfo:
        reg.register("a", "别的对象")

    assert "a" in str(excinfo.value)


def test_blank_names_are_refused():
    reg: Registry[str] = Registry("demo")

    with pytest.raises(RegistryError):
        reg.register("   ", "A")


def test_names_are_trimmed_so_padding_does_not_create_a_second_entry():
    reg: Registry[str] = Registry("demo")
    reg.register("  a  ", "A")

    assert reg.names() == ("a",)
    assert reg.get("a") == "A"


def test_get_raises_and_says_what_is_there():
    reg: Registry[str] = Registry("demo")
    reg.register("a", "A")

    with pytest.raises(RegistryError) as excinfo:
        reg.get("nope")

    # 报错里要带上现有的名字：调用点全是"我确定它注册过"，
    # 不带现状的报错会把人推回去读代码。
    assert "a" in str(excinfo.value)


def test_first_match_returns_none_instead_of_raising():
    reg: Registry[str] = Registry("demo")
    reg.register("a", "A")

    assert reg.first_match(lambda item: item == "B") is None
    assert reg.first_match(lambda item: False) is None


def test_an_empty_registry_is_a_normal_state():
    reg: Registry[str] = Registry("demo")

    assert len(reg) == 0
    assert reg.all() == ()
    assert reg.first_match(lambda item: True) is None


def test_the_registry_name_shows_up_in_errors():
    """两个注册表同时出错时，光看异常要能分清是哪一个。"""
    reg: Registry[str] = Registry("extractor")

    with pytest.raises(RegistryError) as excinfo:
        reg.register("", "A")

    assert "extractor" in str(excinfo.value)
