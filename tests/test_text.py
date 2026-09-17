"""字节截断工具测试。"""
from app.core.text import TRUNCATED_MARK, truncate_chars, truncate_utf8, utf8_len


def test_ascii_within_budget():
    assert truncate_utf8("hello", 10) == "hello"


def test_chinese_bytes_counted():
    # 中文一字 3 字节
    assert utf8_len("中") == 3
    assert truncate_utf8("中中中", 6) == "中中" + TRUNCATED_MARK
    assert truncate_utf8("中中中", 9) == "中中中"


def test_truncated_mark_appended():
    result = truncate_utf8("abcdef", 4)
    assert result == "abcd" + TRUNCATED_MARK


def test_no_split_multibyte():
    # 8 字节预算：中(3)中(3)后只剩 2 字节，装不下第三个中，也不应切半个字符
    result = truncate_utf8("中中中", 8)
    assert result.startswith("中中")


def test_emoji_surrogate_pair():
    # emoji 4 字节，作为整体保留（a+emoji 恰好 5 字节内）
    result = truncate_utf8("a😀b", 5)
    assert result == "a😀" + TRUNCATED_MARK


def test_none_and_empty():
    assert truncate_utf8(None, 10) is None
    assert truncate_utf8("", 10) == ""


def test_truncate_chars():
    assert truncate_chars("abcdef", 3) == "abc..."
    assert truncate_chars("abc", 5) == "abc"
    assert truncate_chars(None, 5) is None
