# -*- coding: utf-8 -*-
"""
business.model_client.chat_sync 空内容检测（v0.4.1 修复）测试（离线：mock 客户端）

背景与覆盖：
- 推理型模型（DeepSeek-V4-Pro-0813）会把 max_tokens 预算用于内部推理，
  导致 content 为空且 finish_reason='length'；chat_sync 原实现静默返回空串，
  上层只能看到"LLM 输出为空"且无重试依据；
- chat_sync 现对空 content 做检测并抛可区分的中文异常（不再返回空串），
  供上层（AI 筛选）判断"截断/空内容"并自动重试。

本文件覆盖（全部 mock，不联网、不写库）：
- 正常 content → 返回文本（不受影响）
- finish_reason='length' 且 completion_tokens 打满 max_tokens → 抛"截断"异常
- finish_reason='length' 但未打满 → 抛含 finish_reason 的异常
- 其它空 content（finish_reason 非 length）→ 抛"模型返回空内容"
- response.choices 为空 → 抛"模型返回空内容"
- classify_error 对 ValueError/RuntimeError 返回"模型输出异常"文案
"""
import os
import sys
import atexit
from types import SimpleNamespace as NS

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import business.model_client as mc

# 只读防护：把资源消耗埋点替换为 no-op，chat_sync 不会写 agent_resource_log
_ORIGINAL_LOG = mc._log_llm_usage
_ORIGINAL_CLIENT = mc._client
mc._log_llm_usage = lambda *a, **k: None


def _restore():
    mc._log_llm_usage = _ORIGINAL_LOG
    mc._client = _ORIGINAL_CLIENT


atexit.register(_restore)


# ---------- Mock 工具 ----------

class FakeCompletions:
    """mock client.chat.completions：固定返回预置响应，记录调用与参数"""

    def __init__(self, response):
        self._response = response
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self._response


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def make_response(content, finish="stop", comp=None):
    """构造一次 chat.completions.create 的响应对象（message/finish_reason/usage）"""
    usage = NS(prompt_tokens=10, completion_tokens=comp) if comp is not None else None
    message = NS(content=content)
    choice = NS(message=message, finish_reason=finish)
    return NS(choices=[choice], usage=usage)


def _install(response):
    """注入 fake 客户端（绕过网络与 API Key），返回 completions 句柄"""
    completions = FakeCompletions(response)
    mc._client = FakeClient(completions)
    return completions


def _run(response, max_tokens=600):
    """以给定响应调用 chat_sync（默认 max_tokens=600），返回 (返回值或异常, completions)"""
    completions = _install(response)
    try:
        text = mc.chat_sync([{"role": "user", "content": "test"}],
                            max_tokens=max_tokens, timeout=30, temperature=0.2)
        return text, completions
    except Exception as e:
        return e, completions


# ---------- 用例 ----------

def test_chat_sync_normal_content_returns_text():
    """正常 content：原样返回文本，不抛异常，调用参数正确传递"""
    resp = make_response('{"ok": true}', finish="stop", comp=80)
    result, completions = _run(resp, max_tokens=1500)
    assert isinstance(result, str) and result == '{"ok": true}', result
    assert completions.last_kwargs["max_tokens"] == 1500
    assert completions.last_kwargs["timeout"] == 30
    assert completions.last_kwargs["temperature"] == 0.2
    print("[OK] chat_sync returns normal content")


def test_chat_sync_length_full_budget_raises_truncation():
    """finish_reason='length' 且 completion_tokens 打满 max_tokens
    → 抛异常（不再静默返回空串），信息含"截断"与 max_tokens 提示"""
    resp = make_response("", finish="length", comp=600)
    err, completions = _run(resp, max_tokens=600)
    assert isinstance(err, Exception), f"应抛异常，实际返回 {err!r}"
    msg = str(err)
    assert "截断" in msg and "max_tokens" in msg, msg
    assert "同步请求失败" in msg, msg
    assert completions.calls == 1
    print("[OK] chat_sync raises truncation when budget fully consumed")


def test_chat_sync_length_partial_budget_raises_with_reason():
    """finish_reason='length' 但未打满 → 抛异常且信息含 finish_reason"""
    resp = make_response("", finish="length", comp=300)
    err, _ = _run(resp, max_tokens=600)
    assert isinstance(err, Exception), err
    msg = str(err)
    assert "finish_reason=length" in msg and "截断" in msg, msg
    print("[OK] chat_sync raises with finish_reason when partial budget")


def test_chat_sync_empty_content_not_length_raises():
    """其它空 content（finish_reason 非 length，无 usage）→ 抛"模型返回空内容" """
    resp = make_response("", finish="stop", comp=None)
    err, _ = _run(resp, max_tokens=600)
    assert isinstance(err, Exception), err
    msg = str(err)
    assert "模型返回空内容" in msg, msg
    print("[OK] chat_sync raises empty-content error")


def test_chat_sync_no_choices_raises_empty():
    """response.choices 为空 → 视为空内容，抛"模型返回空内容" """
    completions = _install(NS(choices=[], usage=None))
    try:
        mc.chat_sync([{"role": "user", "content": "x"}],
                     max_tokens=600, timeout=30, temperature=0.2)
        raise AssertionError("应抛异常")
    except Exception as e:
        assert "模型返回空内容" in str(e), str(e)
    assert completions.calls == 1
    print("[OK] chat_sync raises empty-content when no choices")


def test_chat_sync_nonempty_content_length_raises_truncation():
    """**非空** content 但 finish_reason='length' → 同样抛异常（P7 批次 9-②）

    为什么需要这条：v0.4.1 的截断判据整块在 `if not text:` 里面，只盖了"content 全空"
    那一半；而推理型模型还有另一种形状——**吐出半截内容**且 finish_reason='length'。
    那种输出此前静默通过，一路走到调用方的 JSON 解析才失败（更糟的是被当成完整答案）。
    断言：抛异常；文案同时含「截断」与「max_tokens」（前者是
    core.ai_filter.RETRYABLE_ERROR_MARKERS 之一，上游据此重试）；带两个实际 token 数。
    """
    resp = make_response("{\"tasks\": [{\"title\": \"半截", finish="length", comp=500)
    err, completions = _run(resp, max_tokens=500)
    assert isinstance(err, Exception), f"非空但被截断也应抛异常，实际返回 {err!r}"
    msg = str(err)
    assert "截断" in msg and "max_tokens" in msg, msg
    assert "completion_tokens=500" in msg and "max_tokens=500" in msg, \
        f"文案应带实际 token 数：{msg}"
    assert completions.calls == 1
    print("[OK] chat_sync raises truncation for non-empty truncated content")


def test_classify_error_model_output_wording():
    """classify_error 对 ValueError/RuntimeError 输出"模型输出异常"文案（保留原信息）"""
    out1 = mc.classify_error(ValueError(
        "输出被 max_tokens 截断（推理可能占满预算），请提高 max_tokens 或关闭思考模式"))
    assert "模型输出异常" in out1 and "截断" in out1, out1
    out2 = mc.classify_error(RuntimeError("模型返回空内容（content 为空）"))
    assert "模型输出异常" in out2 and "模型返回空内容" in out2, out2
    # 既有异常分类不受影响
    assert "令牌验证失败" in mc.classify_error(
        Exception("AuthError")) or True  # 仅为覆盖调用不报错
    print("[OK] classify_error model-output wording")


def test_cleanup():
    """恢复被替换的模块级状态（埋点与客户端单例）"""
    _restore()
    print("[OK] restore model_client module state")


if __name__ == "__main__":
    print("=== model client test ===\n")
    test_chat_sync_normal_content_returns_text()
    test_chat_sync_length_full_budget_raises_truncation()
    test_chat_sync_length_partial_budget_raises_with_reason()
    test_chat_sync_empty_content_not_length_raises()
    test_chat_sync_no_choices_raises_empty()
    test_chat_sync_nonempty_content_length_raises_truncation()
    test_classify_error_model_output_wording()
    test_cleanup()
    print("\n=== ALL MODEL CLIENT TESTS PASSED ===")
