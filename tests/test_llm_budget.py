# -*- coding: utf-8 -*-
"""
两档预算 LLM 调用（core/ai_filter.chat_sync_with_retry_budget）单元测试

背景（P7 批次 10-①）：生产模型是推理型，max_tokens 预算会先被内部推理吃掉。
凡把输出当 JSON 解析的调用，首档预算低于推理地板就是**硬失败**——解析不出来
⇒ 功能整块失效（且只表现为静默降级）。本 helper 是该策略的唯一真源，故必须
能单独证伪它的三条契约：

  ① 首次成功 ⇒ 只调一次，且预算 == DEFAULT_MAX_TOKENS；
  ② 首档抛"模型输出问题"（命中 RETRYABLE_ERROR_MARKERS）⇒ 恰好两次，
     第二次预算 == RETRY_MAX_TOKENS，返回二档结果；
  ③ 首档抛非标记错误（认证 / 配额 / 网络类）⇒ 只调一次且原样抛出。

靶子用**字符串形式** patch("business.model_client.chat_sync", ...)：helper 在
函数内延迟导入 chat_sync，靶子必须是导入源（而不是 helper 自己的名字，也不是
本测试模块里导入的名字），否则 patch 打在一个 helper 根本不读的引用上，
三条断言会全部"通过"而毫无意义。

不碰数据库：本文件只调用纯函数 + mock，故不需要 setup_test_db / 切 DB_PATH
（check_test_db_isolation 只拦"使用 DB 但未隔离"，不拦不使用 DB 的文件）。
"""
import os
import sys
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai_filter import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    RETRY_MAX_TOKENS,
    chat_sync_with_retry_budget,
)

_MESSAGES = [{"role": "user", "content": "只输出 JSON"}]


def _recorder(behaviour):
    """构造 (fake_chat_sync, calls)：calls 逐次记录实际收到的 max_tokens

    behaviour(budget, n) -> str | Exception：
    返回 Exception 实例则抛出，否则作为返回值。
    """
    calls = []

    def fake_chat_sync(messages, max_tokens=None, **kwargs):
        calls.append(max_tokens)
        outcome = behaviour(max_tokens, len(calls))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake_chat_sync, calls


def test_first_attempt_succeeds_single_call():
    """① 首次成功 ⇒ 只调一次，且用的是 DEFAULT_MAX_TOKENS（1500，不是 300/500）"""
    fake, calls = _recorder(lambda budget, n: '{"agent": ["通用助手"]}')
    with patch("business.model_client.chat_sync", fake):
        result = chat_sync_with_retry_budget(_MESSAGES, temperature=0.1)

    assert result == '{"agent": ["通用助手"]}', result
    assert calls == [DEFAULT_MAX_TOKENS], \
        f"应只调一次且预算为 DEFAULT_MAX_TOKENS，实际 {calls}"
    assert DEFAULT_MAX_TOKENS >= 1500, \
        f"首档预算不得低于项目实测的推理地板 1500，实际 {DEFAULT_MAX_TOKENS}"
    print(f"[OK] 首档即成功：调用 {len(calls)} 次，预算 {calls}")


def test_truncation_retries_once_with_larger_budget():
    """② 首档抛截断类异常、二档成功 ⇒ 恰好两次，第二次 RETRY_MAX_TOKENS，返回二档结果"""
    # chat_sync 的真实截断文案（见 business/model_client.py 的两处 raise）
    truncation = ValueError(
        "同步请求失败：⚠️ **模型输出异常**：输出被 max_tokens 截断"
        "（推理可能占满预算），请提高 max_tokens 或关闭思考模式")

    def behaviour(budget, n):
        if n == 1:
            return truncation
        return '{"agent": ["英语"]}'

    fake, calls = _recorder(behaviour)
    with patch("business.model_client.chat_sync", fake):
        result = chat_sync_with_retry_budget(_MESSAGES, temperature=0.1)

    assert result == '{"agent": ["英语"]}', result
    assert calls == [DEFAULT_MAX_TOKENS, RETRY_MAX_TOKENS], \
        f"应恰好两次且第二次用 RETRY_MAX_TOKENS，实际 {calls}"
    assert len(calls) == 2, f"不得无限重试，实际 {len(calls)} 次"
    assert RETRY_MAX_TOKENS > DEFAULT_MAX_TOKENS, \
        f"重试轮预算必须更大，实际 {RETRY_MAX_TOKENS} vs {DEFAULT_MAX_TOKENS}"
    print(f"[OK] 截断后重试一次成功：预算 {calls}")


def test_retryable_empty_output_also_retries():
    """② 的第二种形状：空内容类异常（另一条 RETRYABLE_ERROR_MARKERS）同样重试一次"""
    fake, calls = _recorder(lambda budget, n: (
        ValueError("同步请求失败：⚠️ **模型输出异常**：模型返回空内容（content 为空）")
        if n == 1 else '{"ok": true}'))
    with patch("business.model_client.chat_sync", fake):
        result = chat_sync_with_retry_budget(_MESSAGES)

    assert result == '{"ok": true}', result
    assert calls == [DEFAULT_MAX_TOKENS, RETRY_MAX_TOKENS], calls
    print(f"[OK] 空内容后重试一次成功：预算 {calls}")


def test_non_retryable_error_raises_immediately():
    """③ 认证类错误不重试：只调一次，且**原样**抛出（不包装、不降级）"""
    auth_error = RuntimeError("同步请求失败：🔑 **令牌验证失败**\n请检查 API Key 是否正确或已过期。")
    fake, calls = _recorder(lambda budget, n: auth_error)

    with patch("business.model_client.chat_sync", fake):
        try:
            chat_sync_with_retry_budget(_MESSAGES, temperature=0.1)
        except Exception as e:
            raised = e
        else:
            raised = None

    assert raised is auth_error, \
        f"应原样抛出同一个异常对象，实际 {raised!r}"
    assert calls == [DEFAULT_MAX_TOKENS], \
        f"认证类错误不得重试（烧额度且无意义），实际 {calls}"
    print(f"[OK] 非标记错误原样抛出且不重试：调用 {len(calls)} 次")


def test_both_budgets_exhausted_raises_last_error():
    """两档都用尽仍失败 ⇒ 抛出**最后一个**异常（上层据此走既有降级路径）"""
    first_err = ValueError("同步请求失败：⚠️ **模型输出异常**：输出被 max_tokens 截断")
    last_err = ValueError("同步请求失败：⚠️ **模型输出异常**：模型返回空内容（content 为空）")
    fake, calls = _recorder(lambda budget, n: first_err if n == 1 else last_err)

    with patch("business.model_client.chat_sync", fake):
        try:
            chat_sync_with_retry_budget(_MESSAGES)
        except Exception as e:
            raised = e
        else:
            raised = None

    assert raised is last_err, f"应抛最后一个异常，实际 {raised!r}"
    assert calls == [DEFAULT_MAX_TOKENS, RETRY_MAX_TOKENS], calls
    print(f"[OK] 两档用尽抛最后一个异常：预算 {calls}")


def test_first_budget_at_or_above_retry_skips_second_round():
    """首档预算已 ≥ RETRY_MAX_TOKENS 时不追加第二轮（绝不用更小的预算重试）"""
    err = ValueError("同步请求失败：⚠️ **模型输出异常**：输出被 max_tokens 截断")
    fake, calls = _recorder(lambda budget, n: err)

    with patch("business.model_client.chat_sync", fake):
        try:
            chat_sync_with_retry_budget(_MESSAGES, first_max_tokens=RETRY_MAX_TOKENS)
        except Exception as e:
            raised = e
        else:
            raised = None

    assert raised is err, f"应原样抛出，实际 {raised!r}"
    assert calls == [RETRY_MAX_TOKENS], f"预算已足够不应再试，实际 {calls}"
    print(f"[OK] 首档已达标时只调一次：预算 {calls}")


if __name__ == "__main__":
    print("=== llm budget helper test ===\n")
    test_first_attempt_succeeds_single_call()
    test_truncation_retries_once_with_larger_budget()
    test_retryable_empty_output_also_retries()
    test_non_retryable_error_raises_immediately()
    test_both_budgets_exhausted_raises_last_error()
    test_first_budget_at_or_above_retry_skips_second_round()
    print("\n=== ALL LLM BUDGET TESTS PASSED ===")
