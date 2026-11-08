"""
魔搭（ModelScope）模型客户端封装

职责：
- 从项目根目录 .env 加载配置（python-dotenv）
- OpenAI 兼容客户端单例初始化
- 普通流式请求 / Function Call 工具调用 / 同步请求
- 异常分类与中文提示转换

配置项（写在项目根目录的 .env 中，已被 .gitignore 忽略，勿提交到仓库）：
- MODELSCOPE_API_KEY      必填，魔搭 API Token
- MODELSCOPE_API_BASE_URL 选填，默认 https://api-inference.modelscope.cn/v1/
- MODELSCOPE_MODEL_NAME   选填，默认 Qwen/Qwen3.5-35B-A3B
"""

import os
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import (
    OpenAI,
    APIError,
    APIConnectionError,
    RateLimitError,
    AuthenticationError,
)

# ---------- 配置（从项目根目录 .env 读取，显式指定路径以兼容任意启动目录） ----------
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
API_BASE_URL = os.getenv("MODELSCOPE_API_BASE_URL", "https://api-inference.modelscope.cn/v1/")
MODEL_NAME = os.getenv("MODELSCOPE_MODEL_NAME", "Qwen/Qwen3.5-35B-A3B")

# ---------- 单例 ----------
_client = None


def get_client():
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "未配置魔搭 API Key：请在项目根目录的 .env 文件中设置 "
                "MODELSCOPE_API_KEY=你的Token（模板见 .env.example）"
            )
        _client = OpenAI(api_key=API_KEY, base_url=API_BASE_URL)
    return _client


# ---------- 异常分类 ----------
def classify_error(error: Exception) -> str:
    if isinstance(error, AuthenticationError):
        return "🔑 **令牌验证失败**\n请检查 API Key 是否正确或已过期。"
    if isinstance(error, RateLimitError):
        return "⏳ **请求频率超限**\n模型额度耗尽或请求过于频繁，请稍后再试。"
    if isinstance(error, APIConnectionError):
        return "🌐 **网络连接失败**\n请检查网络环境或魔搭服务状态。"
    if isinstance(error, APIError):
        error_msg = str(error).lower()
        if "quota" in error_msg or "limit" in error_msg or "exceeded" in error_msg:
            return "⏳ **模型调用额度已达上限**，请检查账户配额。"
        return f"⚠️ **API 返回错误**：{str(error)[:200]}"
    if isinstance(error, TimeoutError):
        return "⏱️ **请求超时**，模型响应过慢，请稍后重试。"
    if isinstance(error, (ValueError, RuntimeError)):
        # 业务层主动抛出的模型输出问题（如 chat_sync 空内容检测：
        # 输出被 max_tokens 截断 / 模型返回空内容），保留原文案便于上层判断
        return f"⚠️ **模型输出异常**：{str(error)[:200]}"
    return f"❌ **未知错误**：{str(error)[:200]}"


# ---------- 资源消耗埋点（v0.1：粗粒度估算，不追求精确计费） ----------
def _estimate_input_tokens(messages) -> int:
    """按消息字符数粗估输入 token 数（字符数 ÷ 4，中英文混合经验值）"""
    try:
        from memory.resource_log import estimate_messages_tokens
        return estimate_messages_tokens(messages)
    except Exception:
        return 0


def _log_llm_usage(operation, messages, response_text="", usage=None):
    """记录一次 LLM 调用资源消耗（写入 agent_resource_log 表）

    埋点原则：
    - 全程 try/except 包裹，任何失败只打印日志，绝不影响业务主流程；
    - 优先使用响应中的 usage 信息（精确 token 数），缺失时按输入/输出
      字符数粗估（字符数 ÷ 4）；
    - 延迟导入 memory.resource_log，避免模块加载期形成循环依赖。
    """
    try:
        from memory.resource_log import log_resource_usage, estimate_tokens
        in_tokens = _estimate_input_tokens(messages)
        out_tokens = 0
        if usage is not None:
            try:
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                if prompt_tokens > 0 or completion_tokens > 0:
                    # 响应带 usage 信息时以精确值为准
                    in_tokens = prompt_tokens or in_tokens
                    out_tokens = completion_tokens
            except Exception:
                out_tokens = estimate_tokens(response_text)
        else:
            out_tokens = estimate_tokens(response_text)
        log_resource_usage("llm", in_tokens + out_tokens, operation,
                           f"模型 {MODEL_NAME}")
    except Exception:
        pass


# ---------- 普通流式请求 ----------

#: 流式回答被 max_tokens 截断、但**已经吐出了正文**时的提示（P7 批次 13）。
#: 必须追加在"到目前为止的全文"之后——本函数的 yield 值是全文而不是增量。
TRUNCATION_NOTICE_PARTIAL = (
    "\n\n---\n⚠️ 本次回答被截断了：模型输出撞上 max_tokens={max_tokens} 的上限，"
    "上面这段是半截、不是完整回答；重发一次通常能拿到续写。")

#: 流式回答被 max_tokens 截断、且**一个字正文都没有**时的提示（P7 批次 13）。
#: 此时没有全文可追加，这一帧本身就是调用方拿到的唯一内容。
TRUNCATION_NOTICE_EMPTY = (
    "\n\n---\n⚠️ 本次没有得到正文：模型在截断前一个字都没输出"
    "（推理型模型可能把 max_tokens={max_tokens} 的预算用在了内部推理上）。可以重发一次。")


def stream_chat(messages, max_tokens=2048, timeout=60, temperature=0.7):
    client = get_client()
    response = ""
    truncated = False
    try:
        stream = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            stream=True,
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            # 截断信号（P7 批次 13）：流式响应把 finish_reason 放在最后一个分片上，
            # 值 'length' 表示撞上了 max_tokens 上限。必须用 getattr 读——分片完全
            # 可能没有该属性（测试里的假分片就是这个形状），直接属性访问会抛
            # AttributeError 并被下面的 except 吞成一段错误文案。多次出现只记一次。
            if getattr(choice, "finish_reason", None) == "length":
                truncated = True
            if choice.delta.content:
                response += choice.delta.content
                yield response
        if truncated:
            # 出声方式与既有的 `yield f"\n\n---\n{classify_error(e)}"` 同形：**追加一句
            # 提示**，绝不单独 yield（那会把整段回答替换成提示），也绝不抛异常——
            # core/runtime.py 的调用处只有 finally、没有 except，异常穿出去会让存盘 /
            # 事件发布 / 状态复位全部跳过（状态卡在 RUNNING）；手动总结那条路则会跳过
            # 它自己的诚实降级。流式拿不到 completion_tokens，故只印 max_tokens。
            limit = int(max_tokens or 0)
            if response:
                yield response + TRUNCATION_NOTICE_PARTIAL.format(max_tokens=limit)
            else:
                yield TRUNCATION_NOTICE_EMPTY.format(max_tokens=limit)
    except Exception as e:
        yield f"\n\n---\n{classify_error(e)}"
    finally:
        # 资源消耗埋点：流式响应结束（或生成器被关闭）时估算 token 写入日志
        _log_llm_usage("stream_chat", messages, response_text=response)


# ---------- 带工具调用的流式请求 ----------
def stream_chat_with_tools(messages, tools, max_tokens=2048, timeout=60, temperature=0.7):
    client = get_client()
    max_tool_rounds = 3  # 最多执行3轮工具调用，防止死循环

    for round_num in range(max_tool_rounds):
        try:
            stream = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools if tools else None,
                stream=True,
                max_tokens=max_tokens,
                timeout=timeout,
                temperature=temperature,
            )

            response_text = ""
            tool_calls_data = []
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue
                if delta.content:
                    response_text += delta.content
                    yield response_text
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        while len(tool_calls_data) <= idx:
                            tool_calls_data.append({"id": "", "function": {"name": "", "arguments": ""}})
                        if tc.id:
                            tool_calls_data[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_data[idx]["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_data[idx]["function"]["arguments"] += tc.function.arguments

            # 如果没有工具调用，正常返回
            # （检查任意片段是否带工具名：多工具流式返回时首个分片可能为空；
            #   用安全访问防御个别分片缺失 function 字段的情况，避免 KeyError）
            if not any((tc.get("function") or {}).get("name") for tc in tool_calls_data):
                return

            # 执行工具
            from agents.tool_manager import ToolManager
            assistant_msg = {
                "role": "assistant",
                "content": response_text or None,
                "tool_calls": []
            }
            for tc in tool_calls_data:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    assistant_msg["tool_calls"].append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": fn,
                    })
            messages.append(assistant_msg)

            for tc in tool_calls_data:
                fn = tc.get("function") or {}
                tool_name = fn.get("name")
                if not tool_name:
                    continue
                try:
                    arguments = json.loads(fn.get("arguments", ""))
                except (json.JSONDecodeError, TypeError):
                    arguments = {}  # 参数解析失败时以空参数调用，由工具自行容错

                yield f"\n🔧 调用工具：{tool_name}\n"
                result = ToolManager.execute_tool(tool_name, arguments)
                yield f"📄 工具返回：\n{result[:500]}\n"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # 继续下一轮（第二次请求会发送包含 tool 结果的消息）
            continue

        except Exception as e:
            yield f"\n\n---\n{classify_error(e)}"
            return
        finally:
            # 资源消耗埋点：每一轮都是一次独立 API 调用，轮次结束（或生成器被
            # 关闭）时估算 token 写入日志；locals() 防御 create 阶段抛异常导致
            # response_text 未定义的情况，任何失败不影响主流程
            try:
                _log_llm_usage("stream_chat_with_tools", messages,
                               response_text=locals().get("response_text", ""))
            except Exception:
                pass

    # 如果循环结束仍没有最终回答，返回提示
    yield "\n⚠️ 工具调用轮次过多，已停止。"

# ---------- 非流式同步请求 ----------
def chat_sync(messages, max_tokens=300, timeout=60, temperature=0.7) -> str:
    client = get_client()
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME, messages=messages, stream=False,
            max_tokens=max_tokens, timeout=timeout, temperature=temperature,
        )
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice is not None else None
        text = (message.content or "") if message is not None else ""
        # finish / usage / completion_tokens 的取值**提到空内容分支之前**（P7 批次 9-②）：
        # 原先这三行整块在 `if not text:` 里面，于是"content 非空但 finish_reason='length'"
        # 这种半截输出会静默通过，一路走到调用方的 JSON 解析才失败（更糟的是被当成完整答案）。
        # 现在两种形状（全空 / 半截）共用同一份口径。
        finish = getattr(choice, "finish_reason", None) if choice else None
        usage = getattr(response, "usage", None)
        comp = 0
        if usage is not None:
            try:
                comp = int(getattr(usage, "completion_tokens", 0) or 0)
            except (TypeError, ValueError):
                comp = 0
        if not text:
            # 空内容检测（v0.4.1）：推理型模型（如 DeepSeek-V4-Pro）会把
            # max_tokens 预算先用于内部推理，导致 content 为空且
            # finish_reason='length'——不再静默返回空串，抛可区分的中文
            # 异常，让上层（如 AI 筛选）据此提高预算或自动重试
            if finish == "length":
                if comp >= int(max_tokens or 0):
                    raise ValueError(
                        "输出被 max_tokens 截断（推理可能占满预算），"
                        "请提高 max_tokens 或关闭思考模式")
                raise ValueError(
                    f"模型输出被截断（finish_reason={finish}，"
                    f"completion_tokens={comp}）")
            raise ValueError("模型返回空内容（content 为空）")
        if finish == "length":
            # 非空但被截断（P7 批次 9-②，v0.4.1 判据只盖了空内容那一半）：
            # 内容是半截，不能当成完整答案交给上层。文案必须同时含「截断」与
            # 「max_tokens」——前者是 core.ai_filter.RETRYABLE_ERROR_MARKERS 之一
            # （上游据此决定重试），后者连同两个实际 token 数供人工排查。
            raise ValueError(
                f"模型输出被截断（finish_reason={finish}，"
                f"completion_tokens={comp}，max_tokens={int(max_tokens or 0)}）："
                "内容是半截、不是完整答案，请提高 max_tokens 或关闭思考模式")
        # 资源消耗埋点：优先使用响应 usage 信息，缺失时按字符粗估
        _log_llm_usage("chat_sync", messages, response_text=text,
                       usage=getattr(response, "usage", None))
        return text
    except Exception as e:
        # 失败也记录输入 token（消耗已发生），再抛原始错误
        _log_llm_usage("chat_sync", messages, response_text="")
        raise Exception(f"同步请求失败：{classify_error(e)}")