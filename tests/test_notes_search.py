# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cc_light as L


def test_match_record():
    # 空 query 恒匹配
    assert L.match_record("任意", "") is True
    # 不区分大小写子串
    assert L.match_record("Hello World", "world") is True
    assert L.match_record("Hello World", "WORLD") is True
    # 子串
    assert L.match_record("abcdef", "bcd") is True
    # query 比文本长 → 不匹配
    assert L.match_record("abc", "abcd") is False
    # 无命中
    assert L.match_record("abc", "xyz") is False
    # 多行/空白被压平
    assert L.match_record("a  b\tc", "a b") is True


def test_preview_snippet_empty_query():
    # 空 query:前 head 字,超长补 …
    s, spans = L.preview_snippet("0123456789ABCDEFGHIJ", "")   # 20 字
    assert s == "0123456789ABCD …"
    assert spans == []
    # 不超长:无 …
    s, spans = L.preview_snippet("短文本", "")
    assert s == "短文本"
    assert spans == []


def test_preview_snippet_hit_middle():
    s, spans = L.preview_snippet("今天要改 cc-light 的搜索功能很好用", "搜索")
    assert "搜索" in s
    assert len(spans) == 1
    a, b = spans[0]
    assert s[a:b] == "搜索", (s, spans)
    assert s.startswith("…")   # 命中不在开头 → 头部补 …


def test_preview_snippet_hit_start():
    s, spans = L.preview_snippet("搜索功能很好用今天要改", "搜索")
    a, b = spans[0]
    assert s[a:b] == "搜索"
    assert not s.startswith("…")   # 命中在开头 → 不补 …


def test_preview_snippet_no_hit():
    s, spans = L.preview_snippet("abc", "xyz")
    assert s is None
    assert spans == []


if __name__ == "__main__":
    test_match_record()
    test_preview_snippet_empty_query()
    test_preview_snippet_hit_middle()
    test_preview_snippet_hit_start()
    test_preview_snippet_no_hit()
    print("ALL PASS")
