# -*- coding: utf-8 -*-
"""
聊天消息 content 规范化修复测试
背景：Gradio 6 Chatbot 曾把 parts 列表（[{'text':...,'type':'text'}]）
写入会话 JSON，导致对话链路 .strip() 抛 AttributeError。
覆盖：
- message_content_to_text：str / parts 列表 / 混合 part / None / 其他类型
- normalize_chat_history：批量规范化且不改写传入列表
- JsonConversationStoreAdapter：脏 JSON 读出为纯文本、save_message 传列表落盘为文本
- ContextManager.build_system_prompt：list 型 user_input 不再抛 strip 异常（主 bug 回归）
- app.save_current：存盘前规范化，JSON 中不再出现 list 型 content
"""
import json
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.conversation_adapter import (
    JsonConversationStoreAdapter,
    message_content_to_text,
    normalize_chat_history,
)
from core.context_manager import ContextManager
from memory.chat_store import ChatStore
from memory.database import init_db

PARTS_TEXT = [{"text": "搜索一下最近的ai领域成果", "type": "text"}]


def setup():
    init_db()


# ---------- message_content_to_text ----------

def test_str_passthrough():
    """纯字符串原样返回（含空串），不做任何加工"""
    assert message_content_to_text("你好") == "你好"
    assert message_content_to_text("") == ""
    print("[OK] str passthrough")


def test_parts_list_to_text():
    """parts 列表提取 text 字段拼接（Gradio 6 实测格式）"""
    assert message_content_to_text(PARTS_TEXT) == "搜索一下最近的ai领域成果"
    multi = [{"text": "第一段", "type": "text"}, {"text": "第二段", "type": "text"}]
    assert message_content_to_text(multi) == "第一段\n第二段"
    # 混合：字符串 part 与字典 part 并存；非文本 part（图片等）忽略
    mixed = ["纯文本part", {"text": "字典part", "type": "text"},
             {"file": "x.png", "type": "image"}]
    assert message_content_to_text(mixed) == "纯文本part\n字典part"
    print("[OK] parts list to text")


def test_none_and_other_types():
    """None → 空串；数字等其他类型 str() 兜底；元组同列表处理"""
    assert message_content_to_text(None) == ""
    assert message_content_to_text(123) == "123"
    assert message_content_to_text(("a", "b")) == "a\nb"
    print("[OK] none and other types")


def test_normalize_history_pure_copy():
    """normalize_chat_history：全部转纯文本，且不修改传入列表（深拷贝语义）"""
    original = [
        {"role": "user", "content": PARTS_TEXT},
        {"role": "assistant", "content": "回复文本"},
        {"role": "user", "content": ""},
        "非字典元素原样保留",
    ]
    snapshot = [dict(m) if isinstance(m, dict) else m for m in original]
    result = normalize_chat_history(original)
    assert result[0]["content"] == "搜索一下最近的ai领域成果"
    assert result[1]["content"] == "回复文本"
    assert result[2]["content"] == ""
    assert result[3] == "非字典元素原样保留"
    assert all(isinstance(m["content"], str) for m in result[:3])
    # 原列表不被修改
    assert original[0]["content"] == PARTS_TEXT
    assert original == snapshot
    print("[OK] normalize history pure copy")


# ---------- 存储适配层 ----------

def test_adapter_reads_dirty_json_as_text():
    """磁盘上残留 list 型 content 的旧会话：经 get_messages 读出一律为纯文本"""
    init_db()
    store = ChatStore("data/conversations")
    sid = store.create_session(title="_test_脏数据读取")
    adapter = JsonConversationStoreAdapter(store)
    try:
        # 直接用底层 store 写入脏数据（模拟旧版本遗留）
        store.save_history(sid, [
            {"role": "user", "content": PARTS_TEXT},
            {"role": "assistant", "content": "正常文本"},
        ])
        messages = adapter.get_messages(sid)
        assert all(isinstance(m["content"], str) for m in messages), \
            f"runtime 通道必须只见到字符串 content：{messages}"
        assert messages[0]["content"] == "搜索一下最近的ai领域成果"
        assert messages[1]["content"] == "正常文本"
    finally:
        store.delete_session(sid)
    print("[OK] adapter reads dirty json as text")


def test_adapter_save_message_accepts_list():
    """save_message 收到 parts 列表时安全落盘为纯文本"""
    init_db()
    store = ChatStore("data/conversations")
    sid = store.create_session(title="_test_保存列表消息")
    adapter = JsonConversationStoreAdapter(store)
    try:
        adapter.save_message(sid, "user", PARTS_TEXT)
        raw = store.load_history(sid)
        assert isinstance(raw[0]["content"], str), f"落盘应为字符串：{raw[0]}"
        assert raw[0]["content"] == "搜索一下最近的ai领域成果"
    finally:
        store.delete_session(sid)
    print("[OK] adapter save message accepts list")


# ---------- 主 bug 回归 ----------

def test_build_system_prompt_with_list_user_input():
    """list 型 user_input 进入 build_system_prompt 不再抛 strip 异常（核心回归）

    修复前此处抛 AttributeError: 'list' object has no attribute 'strip'。
    """
    cm = ContextManager()
    prompt = cm.build_system_prompt(
        "通用助手", "基础提示词", detail_level="medium",
        goal_id=None, user_input=PARTS_TEXT,
    )
    assert isinstance(prompt, str) and prompt.startswith("基础提示词")
    # 提取出的文本应参与记忆检索路径而不报错（medium 级别触发检索分支）
    prompt_high = cm.build_system_prompt(
        "AI学习", "基础提示词", detail_level="high",
        goal_id=None, user_input=PARTS_TEXT,
    )
    assert isinstance(prompt_high, str)
    print("[OK] build system prompt with list user input")


def test_app_save_current_writes_clean_json():
    """app.save_current 存盘前规范化：JSON 中不再出现 list 型 content（数据源头修复）"""
    from app import save_current, chat_store  # noqa: E402

    sid = chat_store.create_session(title="_test_save_current规范化")
    try:
        dirty_history = [
            {"role": "user", "content": PARTS_TEXT},
            {"role": "assistant", "content": "回答"},
        ]
        save_current(dirty_history, sid)
        raw = chat_store.load_history(sid)
        assert len(raw) == 2
        assert all(isinstance(m["content"], str) for m in raw), \
            f"存盘后的 JSON 必须只含字符串 content：{raw}"
        assert raw[0]["content"] == "搜索一下最近的ai领域成果"
    finally:
        chat_store.delete_session(sid)
    print("[OK] app save current writes clean json")


def test_cleanup():
    """清理测试数据"""
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== chat content fix test ===\n")
    setup()
    test_str_passthrough()
    test_parts_list_to_text()
    test_none_and_other_types()
    test_normalize_history_pure_copy()
    test_adapter_reads_dirty_json_as_text()
    test_adapter_save_message_accepts_list()
    test_build_system_prompt_with_list_user_input()
    test_app_save_current_writes_clean_json()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
