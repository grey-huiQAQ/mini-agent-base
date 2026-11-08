# -*- coding: utf-8 -*-
"""
「📝 结束学习并总结」手动总结路径（ui/controllers/chat_controller.manual_summarize）测试

背景（P7 批次 11-①）：这条路径把模型输出当学习 JSON 解析
（core/event_handlers.record_learning_from_text → core/json_utils.parse_learning_json），
而它原先写死 max_tokens=300——对推理型模型（生产实际是
deepseek-ai/DeepSeek-V4-Pro-0813）必然被 reasoning_content 耗尽。更糟的是它走的是
**stream_chat**：后者当时没有任何截断守卫，截断只 yield 半截文本、连异常都不抛
（对比 chat_sync 在空内容/非空截断两处都 raise）。于是整条链是
「截断 → 解析不出来 → 静默 return → 一行记录都没写」，而用户是**手动点的按钮**。
注（P7 批次 13，2026-09-27）：stream_chat 已于 P7 批次 13 加守卫（读 finish_reason，
值为 'length' 时把提示追加在既有全文之后）。本文件的用例把 stream_chat 整个换成
生成器假的，走不到那段守卫，故不受影响。

本文件三条断言（都要有牙齿，不是 isinstance 式的空断言）：
  ① 不可解析 ⇒ 聊天里出现提示文案，且 learning_records 行数**不变**；
  ② 可解析   ⇒ 聊天里**没有**提示文案，且 learning_records 行数 **+1**；
  ③ 首档预算 == core.ai_filter.DEFAULT_MAX_TOKENS（不许退回 300，也不许写 1500 字面量）。

patch 靶子是 business.stream_chat（**不是** business.model_client.stream_chat）：
chat_controller 在函数内 `from business import stream_chat`，读的是**包命名空间**属性
（business/__init__.py:2 从 .model_client 转出），所以只有打在 business 上才拦得住。
fake **必须是生成器函数**——调用方是 `for chunk in stream_chat(...)`。

mode 传具体角色名 "英语" 以绕开 supervisor_decision（chat_controller.py 里传
"自动（Supervisor）"会真调模型）。

数据库：本文件会写 learning_records，故必须走 setup_test_db() 切到临时库。
"""
import os
import sys
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离：须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db

setup_test_db()

from memory.database import init_db, get_connection  # noqa: E402
from core.ai_filter import DEFAULT_MAX_TOKENS  # noqa: E402
from ui.controllers import chat_controller  # noqa: E402

#: 提示文案里必须出现的两个片段（"没解析成记录" + "没写任何记录"，缺一不可）
_WARN_MARKERS = ("没能解析成学习记录", "没有写入任何记录")

_LEGAL_REPLY = ('{"topic":"闭包","summary":"理解了作用域链","mastery_score":4,'
                '"weak_points":["this 绑定"],"next_task":"复习装饰器"}')

#: 半截 JSON：模拟 stream_chat 被 max_tokens 截断时的真实产物
_TRUNCATED_REPLY = '{"topic":"闭包","summary":"理解了作用域'


def setup():
    """初始化临时库（learning_records 等表）"""
    init_db()


def _learning_count() -> int:
    """当前临时库里 learning_records 的行数"""
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM learning_records").fetchone()[0]
    finally:
        conn.close()


def _run_summarize(reply_text):
    """以 fake stream_chat 跑一遍 manual_summarize，返回 (最后一帧 history, 收到的预算列表)"""
    budgets = []

    def fake_stream_chat(messages, max_tokens=None, temperature=None, **kwargs):
        # 生成器函数（调用方 for chunk in ...）；每轮 yield 累积文本的最终形态
        budgets.append(max_tokens)
        yield reply_text

    seed = [
        {"role": "user", "content": "讲讲 JS 闭包"},
        {"role": "assistant", "content": "闭包是函数与其词法作用域的组合。"},
    ]
    with patch("business.stream_chat", fake_stream_chat):
        frames = list(chat_controller.manual_summarize(seed, "英语"))

    assert frames, "manual_summarize 应至少 yield 一帧（否则 UI 收不到任何更新）"
    return frames[-1], budgets


def _joined(final_history) -> str:
    """把最终 history 的全部内容拼起来，便于断言'提示文案出现在聊天里'"""
    return "\n".join(str(m.get("content", "")) for m in final_history)


def test_unparseable_reply_warns_and_writes_nothing():
    """① 模型输出不可解析（半截 JSON）⇒ 聊天里明说，且一行记录都不写"""
    before = _learning_count()

    final, budgets = _run_summarize(_TRUNCATED_REPLY)
    text = _joined(final)

    for marker in _WARN_MARKERS:
        assert marker in text, \
            f"不可解析时聊天里必须出现提示（缺「{marker}」），实际内容：{text!r}"

    # 提示必须落在**最后一条消息**里（用户直接看得到的位置），不是埋在别处
    assert all(m in str(final[-1].get("content", "")) for m in _WARN_MARKERS), \
        f"提示应出现在最后一条消息里，实际：{final[-1]!r}"

    after = _learning_count()
    assert after == before, \
        f"不可解析时不得写入任何学习记录，行数 {before} → {after}"

    print(f"[OK] 不可解析 → 聊天出提示且未写记录（learning_records 保持 {after} 行，"
          f"调用 {len(budgets)} 次）")


def test_parseable_reply_records_silently():
    """② 模型输出可解析 ⇒ 写入记录，且聊天里**不出现**任何失败提示"""
    before = _learning_count()

    final, _ = _run_summarize(_LEGAL_REPLY)
    text = _joined(final)

    for marker in _WARN_MARKERS:
        assert marker not in text, \
            f"成功写入时不应出现失败提示（却发现「{marker}」），实际内容：{text!r}"

    after = _learning_count()
    assert after == before + 1, \
        f"可解析时应恰好新增 1 条学习记录，行数 {before} → {after}"

    print(f"[OK] 可解析 → 静默写入记录（learning_records {before} → {after} 行）")


def test_summarize_uses_floor_budget():
    """③ 首档预算取 core.ai_filter.DEFAULT_MAX_TOKENS（不得回到 300）"""
    _, budgets = _run_summarize(_LEGAL_REPLY)

    assert budgets, "stream_chat 应被调用至少一次"
    assert budgets[0] == DEFAULT_MAX_TOKENS, \
        f"首档预算应为 DEFAULT_MAX_TOKENS={DEFAULT_MAX_TOKENS}，实际 {budgets[0]}"
    assert DEFAULT_MAX_TOKENS >= 1500, \
        f"地板常量本身不得低于 1500，实际 {DEFAULT_MAX_TOKENS}"

    print(f"[OK] 手动总结首档预算 == DEFAULT_MAX_TOKENS（{budgets[0]}）")


if __name__ == "__main__":
    print("=== manual summarize test ===\n")
    setup()
    test_unparseable_reply_warns_and_writes_nothing()
    test_parseable_reply_records_silently()
    test_summarize_uses_floor_budget()
    print("\n=== ALL MANUAL SUMMARIZE TESTS PASSED ===")
