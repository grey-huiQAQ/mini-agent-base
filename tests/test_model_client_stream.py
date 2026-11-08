# -*- coding: utf-8 -*-
"""
business/model_client 流式解析测试（离线，mock OpenAI 客户端）

覆盖 stream_chat_with_tools：
- 工具调用分片仅有 index/id 而缺失 function 字段时不崩溃（安全访问回归，审查用例）
- 多分片拼装同一工具调用：首片缺 function 不影响执行，工具完成后进入下一轮拿最终回答
- 空流 / 纯内容流优雅返回

覆盖 stream_chat 的截断守卫（P7 批次 13）：
- 有正文 ＋ finish_reason='length' ⇒ 末帧 = 半截正文 ＋ 提示，且**中途帧不带提示**
- 无任何正文 ＋ finish_reason='length' ⇒ 唯一一帧是"没得到正文"的提示（改前一个
  yield 都没有，调用方只能拿到空串）
- finish_reason='stop' ⇒ 逐帧与改前完全一致、不带提示
- 分片**完全没有** finish_reason 属性 ⇒ 不炸、不带提示（守卫必须用 getattr 读）
- 同一流里 length 出现多次 ⇒ 提示只追加一次

**数据库隔离（P6-6d，2026-09-14）**：本文件本身不 import `memory.database`，但
`stream_chat_with_tools` 的生产代码里有资源埋点（`log_resource_usage`），它会
写 `agent_resource_log` 表——于是每跑一次本文件就往**生产库**多写 4 行。
实测：改前单跑本文件，生产库该表 **386 → 390 行**；上一轮全量回归（P5 的 run #52）
同样留了 4 行（`created_at` 是 UTC，12:27:18 UTC = 20:27:18 本地，正落在该轮窗口内）。
这是 `check_test_db_isolation.py` 的**另一类盲区**：它的静态规则只看"这个测试文件
有没有直接使用 DB"，看不见"它调用的生产代码会用 DB"，因此本文件从未被点名。
处置按 P0-5 的既定做法：模块级调用 `setup_test_db()`，把 `DB_PATH` 切到临时库，
使埋点落在临时库上（这不是新增豁免，而是**把一个从未被护栏覆盖的写生产库行为修好**）。
`stream_chat` 同样带埋点，故它的用例也在这道隔离之下。
"""
import os
import sys
from types import SimpleNamespace

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（须在导入任何访问数据库的模块之前）----
# 本文件不直接用 DB，但被调用的生产代码会用（资源埋点），故同样必须隔离
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

from business import model_client as mc  # noqa: E402


# ---------- 伪流式分片构造 ----------

def _content_chunk(text):
    """构造一个仅含文本增量的流式分片"""
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, tool_calls=None))])


def _final_chunk(finish):
    """构造"只有 finish_reason、content=None"的收尾分片（P7 批次 13）

    流式响应把 finish_reason 放在最后一个分片上；该分片通常不带正文。
    """
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish,
        delta=SimpleNamespace(content=None, tool_calls=None))])


def _tool_chunk(index, call_id=None, name=None, arguments=None):
    """构造一个工具调用分片；name=None 时 function 字段为 None，
    模拟部分服务端只下发 index/id 的空增量分片"""
    fn = None if name is None else SimpleNamespace(name=name, arguments=arguments or "")
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(index=index, id=call_id, function=fn)],
        ))])


class _FakeCompletions:
    """按调用次数依次返回预置轮次的伪 chat.completions"""

    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        rnd = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        return iter(rnd)


class _Patch:
    """临时替换 model_client.get_client 与 ToolManager.execute_tool（用后还原）"""

    def __init__(self, rounds, tool_result="工具结果模拟"):
        self.fake = _FakeCompletions(rounds)
        self.tool_calls = []

        self._orig_get_client = mc.get_client
        mc.get_client = lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=self.fake))

        from agents import tool_manager as tm
        self._tm = tm
        self._orig_execute = tm.ToolManager.execute_tool

        def fake_execute(tool_name, arguments):
            self.tool_calls.append((tool_name, arguments))
            return tool_result
        tm.ToolManager.execute_tool = staticmethod(fake_execute)

    def restore(self):
        mc.get_client = self._orig_get_client
        self._tm.ToolManager.execute_tool = self._orig_execute


# ========== 测试用例 ==========

def test_tool_shard_without_function_field_no_crash():
    """分片仅有 index/id 而无 function 字段：不崩溃、不误执行工具、优雅结束（审查回归用例）"""
    rounds = [[_tool_chunk(0, call_id="call_empty")]]  # 唯一分片且 function=None
    p = _Patch(rounds)
    try:
        outputs = list(mc.stream_chat_with_tools(
            [{"role": "user", "content": "你好"}], tools=[{"type": "function"}]))
    finally:
        p.restore()

    assert outputs == [], f"无有效工具名时应直接结束且无输出，实际: {outputs}"
    assert p.fake.calls == 1, "不应发起第二轮请求"
    assert p.tool_calls == [], "不应执行任何工具"
    print("[OK] tool shard without function field no crash")


def test_multi_shard_tool_call_executes_and_finalizes():
    """多分片拼装同一工具调用：首片缺 function 不影响，工具执行后进入下一轮拿最终回答"""
    rounds = [
        [
            _content_chunk("让我查一下。"),
            _tool_chunk(0, call_id="call_1"),                         # 首片：仅 id，无 function
            _tool_chunk(0, name="get_current_time", arguments='{}'),  # 补充片：携带函数名与参数
        ],
        [_content_chunk("现在是白天。")],  # 第二轮：普通内容 → 正常收尾
    ]
    p = _Patch(rounds)
    try:
        outputs = list(mc.stream_chat_with_tools(
            [{"role": "user", "content": "现在几点"}], tools=[{"type": "function"}]))
    finally:
        p.restore()

    joined = "".join(outputs)
    assert "🔧 调用工具：get_current_time" in joined, f"应输出工具调用提示，实际: {joined!r}"
    assert "工具结果模拟" in joined, "工具返回内容应透出"
    assert joined.endswith("现在是白天。"), f"最终回答应完整输出，实际: {joined!r}"
    assert p.tool_calls == [("get_current_time", {})], \
        f"工具应被正确执行一次，实际: {p.tool_calls}"

    # 第二轮请求应携带 assistant(tool_calls) 与 tool 结果消息
    msgs = p.fake.last_kwargs["messages"]
    roles = [m["role"] for m in msgs]
    assert "assistant" in roles and "tool" in roles, f"第二轮消息应包含工具上下文，实际: {roles}"
    print("[OK] multi shard tool call executes and finalizes")


def test_empty_stream_returns_gracefully():
    """空流（无任何分片）：立即返回且不崩溃"""
    p = _Patch([[]])
    try:
        outputs = list(mc.stream_chat_with_tools(
            [{"role": "user", "content": "你好"}], tools=[{"type": "function"}]))
    finally:
        p.restore()
    assert outputs == [], f"空流应无输出，实际: {outputs}"
    print("[OK] empty stream returns gracefully")


# ========== stream_chat 的截断守卫（P7 批次 13） ==========
# 契约三条：① yield 的是"到目前为止的全文"（调用方 `full_response = chunk` 直接覆盖），
# 故提示只能**追加**在最后；② 绝不抛异常（core/runtime.py 的调用处只有 finally、
# 没有 except）；③ 只在 finish_reason == 'length' 时出声，且最多一次。

def _stream_chat_frames(chunks, max_tokens):
    """以一批伪分片跑一遍 stream_chat，返回逐帧列表"""
    p = _Patch([chunks])
    try:
        return list(mc.stream_chat(
            [{"role": "user", "content": "你好"}], max_tokens=max_tokens))
    finally:
        p.restore()


def test_stream_truncation_notice_appended_after_partial_text():
    """有正文 ＋ finish_reason='length' ⇒ 末帧 = 半截正文 ＋ 提示；中途帧不带提示"""
    frames = _stream_chat_frames(
        [_content_chunk("第一段。"), _content_chunk("第二段。"), _final_chunk("length")],
        max_tokens=1234)

    assert len(frames) == 3, f"应为两帧正文 + 一帧带提示的收尾，实际 {frames!r}"
    assert frames[0] == "第一段。", frames
    assert frames[1] == "第一段。第二段。", f"逐帧应是全文而非增量，实际 {frames!r}"
    assert all("⚠️" not in f for f in frames[:2]), f"中途帧不得带提示：{frames!r}"

    last = frames[-1]
    assert last.startswith("第一段。第二段。"), \
        f"提示必须追加在'到目前为止的全文'之后（不能替换正文），实际 {last!r}"
    assert "截断" in last and "max_tokens=1234" in last, \
        f"提示须同时含「截断」与 max_tokens=（core/ai_filter.RETRYABLE_ERROR_MARKERS 之一），实际 {last!r}"
    print("[OK] stream truncation notice appended after partial text")


def test_stream_truncation_notice_when_no_content_at_all():
    """无任何正文 ＋ finish_reason='length' ⇒ 唯一一帧是"没得到正文"的提示

    改前这条流**一个 yield 都没有**，调用方拿到空串（core/runtime.py 会把空串
    存进会话并当作 assistant_message_completed 发出去），界面上什么都不显示。
    """
    frames = _stream_chat_frames([_final_chunk("length")], max_tokens=2048)

    assert len(frames) == 1, f"应恰好一帧（改前是 []），实际 {frames!r}"
    assert "截断" in frames[0] and "max_tokens=2048" in frames[0], frames
    assert "正文" in frames[0], f"这一帧要说明'没有得到正文'，实际 {frames[0]!r}"
    print("[OK] stream truncation notice when no content at all")


def test_stream_stop_finish_adds_no_notice():
    """finish_reason='stop' ⇒ 逐帧与改前完全一致、不带任何提示"""
    frames = _stream_chat_frames(
        [_content_chunk("完整回答。"), _final_chunk("stop")], max_tokens=2048)
    assert frames == ["完整回答。"], f"stop 不应产生任何额外帧，实际 {frames!r}"
    print("[OK] stream stop finish adds no notice")


def test_stream_chunk_without_finish_reason_attribute_no_crash():
    """分片**完全没有** finish_reason 属性 ⇒ 不炸、不带提示

    既有 _content_chunk 就是这个形状（choices[0] 只有 delta）。这也是守卫必须用
    getattr(choice, "finish_reason", None) 读、而不能直接属性访问的原因。
    """
    frames = _stream_chat_frames([_content_chunk("甲"), _content_chunk("乙")],
                                 max_tokens=2048)
    assert frames == ["甲", "甲乙"], f"无该属性时应逐帧原样输出，实际 {frames!r}"
    print("[OK] stream chunk without finish_reason attribute no crash")


def test_stream_repeated_length_notice_only_once():
    """同一流里 finish_reason='length' 出现多次 ⇒ 提示只追加一次"""
    frames = _stream_chat_frames(
        [_content_chunk("半截正文"), _final_chunk("length"), _final_chunk("length")],
        max_tokens=999)

    assert len(frames) == 2, f"应为 1 帧正文 + 1 帧带提示的收尾，实际 {frames!r}"
    assert frames[-1].count("⚠️") == 1, f"提示不得重复，实际 {frames[-1]!r}"
    assert frames[-1].count("max_tokens=999") == 1, frames[-1]
    print("[OK] stream repeated length notice only once")


if __name__ == "__main__":
    print("=== model client stream test ===\n")
    try:
        test_tool_shard_without_function_field_no_crash()
        test_multi_shard_tool_call_executes_and_finalizes()
        test_empty_stream_returns_gracefully()
        test_stream_truncation_notice_appended_after_partial_text()
        test_stream_truncation_notice_when_no_content_at_all()
        test_stream_stop_finish_adds_no_notice()
        test_stream_chunk_without_finish_reason_attribute_no_crash()
        test_stream_repeated_length_notice_only_once()
    finally:
        cleanup_test_db()
    print("\n=== ALL TESTS PASSED ===")
