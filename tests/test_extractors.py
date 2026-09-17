"""表名提取与堆栈解析测试。"""
from app.extractors.stack import (business_file_paths, class_name_to_path,
                                  find_exception_class, parse_business_frames)
from app.extractors.tables import extract_tables


# ---------------------------------------------------------------- 表名提取

def test_extract_from_sql_statement():
    text = "### Error updating database. Cause: INSERT INTO t_order(user_id) VALUES(1)"
    assert extract_tables(text, None) == ["t_order"]


def test_extract_from_db_table_error():
    text = "Table 'order_db.t_order' doesn't exist"
    assert extract_tables(text, None) == ["t_order"]


def test_extract_dedup_and_keyword_filter():
    text = "select * from t_a join t_b on t_a.id = t_b.id from select where"
    # "select" 是关键字应被过滤，重复表名去重
    tables = extract_tables(text, None)
    assert "t_a" in tables and "t_b" in tables
    assert "select" not in tables
    assert len(tables) == len(set(tables))


def test_extract_max_five():
    text = "from a1 from a2 from a3 from a4 from a5 from a6"
    assert len(extract_tables(text, None)) == 5


def test_extract_empty():
    assert extract_tables(None, None) == []
    assert extract_tables("", "") == []


# ---------------------------------------------------------------- 堆栈解析

STACK = """java.lang.NullPointerException: null
	at com.xizhao.order.service.OrderService.createOrder(OrderService.java:92)
	at com.xizhao.order.service.OrderService$$Lambda$12/0x0.apply(Unknown Source)
	at org.springframework.web.method.Handler.invoke(Handler.java:100)
	at com.xizhao.order.service.OrderService.createOrder(OrderService.java:92)
	at com.xizhao.user.client.UserClient.get(UserClient.java:31)"""


def test_parse_business_frames():
    frames = parse_business_frames(STACK, max_frames=10)
    # lambda 合成帧（文件名不匹配）与 spring 帧被跳过；同类多帧去重
    assert list(frames.keys()) == ["com.xizhao.order.service.OrderService",
                                   "com.xizhao.user.client.UserClient"]
    assert frames["com.xizhao.order.service.OrderService"].line_number == 92


def test_parse_business_frames_max():
    frames = parse_business_frames(STACK, max_frames=1)
    assert len(frames) == 1


def test_business_file_paths():
    paths = business_file_paths(STACK, 10)
    assert paths[0] == "src/main/java/com/xizhao/order/service/OrderService.java"


def test_class_name_to_path_test_class():
    assert class_name_to_path("com.xizhao.foo.BarTest") == "src/test/java/com/xizhao/foo/BarTest.java"
    assert class_name_to_path("com.xizhao.test.Bar") == "src/test/java/com/xizhao/test/Bar.java"
    assert class_name_to_path("com.xizhao.foo.BarIT") == "src/test/java/com/xizhao/foo/BarIT.java"
    assert class_name_to_path("com.xizhao.foo.Bar") == "src/main/java/com/xizhao/foo/Bar.java"


def test_find_exception_class():
    assert find_exception_class(None, STACK) == "java.lang.NullPointerException"
    assert find_exception_class("RuntimeException: boom", None) == "RuntimeException"
    assert find_exception_class("no exception here", None) is None
