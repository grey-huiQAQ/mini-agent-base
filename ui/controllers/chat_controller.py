# -*- coding: utf-8 -*-
"""ui.controllers.chat_controller —— 会话 / 聊天 / 导出

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from tools.input_validator import validate_input, is_duplicate, clear_duplicate_cache
from tools import (
    export_as_txt,
    export_as_markdown,
)
from core.conversation_adapter import (
    JsonConversationStoreAdapter,
    message_content_to_text,
    normalize_chat_history,
)
from core.event_handlers import register_event_handlers, record_learning_from_text
from agents.supervisor import supervisor_decision

from ui.controllers._shared import (
    chat_store,
    conversation_store,
    runtime,
)




def user_message(message, history, _temp, _max_tok, _memory):
    """把用户消息追加到会话历史（_temp/_max_tok/_memory 为 Gradio 按位绑定的占位参数）"""
    history = list(history)
    is_valid, clean_msg = validate_input(message)
    if not is_valid:
        return history, ""
    if is_duplicate(clean_msg):
        return history, ""
    history.append({"role": "user", "content": clean_msg})
    return history, ""


def bot_response(history, mode, temp, max_tok, memory, enable_eval, current_session):
    history = list(history)
    if not history or history[-1]["role"] != "user":
        yield history
        return

    user_msg = message_content_to_text(history[-1]["content"])

    # 保存用户消息到 conversation_store（供 runtime 使用；统一为纯文本）
    conversation_store.save_message(current_session, "user", user_msg)

    # 移除上一轮可能残留的空 assistant 占位（保留当前用户消息）
    # 必须同时满足：角色为 assistant 且内容为空/纯空白——
    # 否则会把正常的 assistant 回复误判为占位删除，导致对话历史丢失
    # （content 经 message_content_to_text 收敛，兼容 Gradio 6 parts 列表）
    if len(history) > 1 and isinstance(history[-2], dict) \
            and history[-2].get("role") == "assistant" \
            and not message_content_to_text(history[-2].get("content", "")).strip():
        history = history[:-2] + [history[-1]]

    # 添加一个空的 assistant 用于流式填充
    history.append({"role": "assistant", "content": ""})

    full_response = ""
    try:
        for chunk in runtime.process_user_input(
            current_session,
            user_msg,
            temperature=temp,
            max_tokens=max_tok,
            memory=memory,
            enable_eval=enable_eval,
        ):
            full_response = chunk
            history[-1]["content"] = full_response
            yield list(history)
    except Exception as e:
        history[-1]["content"] = f"❌ 错误：{e}"
        yield list(history)


def manual_summarize(history, mode):
    history = list(history)
    if not history:
        yield history
        return

    if mode == "自动（Supervisor）":
        recent_user = next((msg["content"] for msg in reversed(history) if msg["role"] == "user"), "")
        decision = supervisor_decision(recent_user) if recent_user else {"agent": ["通用助手"]}
        selected_agent = decision["agent"][0]
    else:
        selected_agent = mode

    conversation_text = ""
    for msg in history[-10:]:
        role = "用户" if msg["role"] == "user" else "助手"
        conversation_text += f"{role}：{msg.get('content', '')[:300]}\n"

    prompt = (
        "请根据以下对话，生成学习总结 JSON：\n"
        "```json\n"
        '{"topic":"本次主题","summary":"30字内总结","mastery_score":1-5,'
        '"weak_points":["薄弱点"],"next_task":"建议下一步"}\n'
        "```\n\n对话内容：\n" + conversation_text +
        "\n只输出 JSON，不要其他内容。"
    )
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": ""})
    messages = [
        {"role": "system", "content": "你是学习记录助手，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    from business import stream_chat
    # 预算复用 core.ai_filter 的单一常量（P7 批次 11-①）：这里原先写死 300，而上面
    # :102-108 的 prompt 要的是"只输出 JSON"——生产实际模型是推理型
    # （deepseek-ai/DeepSeek-V4-Pro-0813，依据 agent_resource_log.detail），会先把
    # 预算用于内部推理，300 必然被 reasoning_content 耗尽，拿回来的是半截 JSON。
    # 不写 1500 字面量（本仓已被"两处手工复制保持一致"咬过两次），函数内延迟导入
    # 照 agents/daily_agent.py 的 P7 批次 9-② 先例。
    from core.ai_filter import DEFAULT_MAX_TOKENS
    for chunk in stream_chat(messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=0.3):
        history[-1]["content"] = chunk
        yield list(history)

    # 手动总结完成后记录学习
    domain_map = {"AI学习": "AI", "英语": "英语", "系统科学": "系统科学", "通用助手": "通用"}
    domain = domain_map.get(selected_agent, "通用")
    recorded = record_learning_from_text(history[-1]["content"], selected_agent, domain)

    if not recorded:
        # 诚实降级（P7 批次 11-①）：上面那轮是**流式**调用，而 business.model_client
        # 的 stream_chat 当时**没有任何截断守卫**——截断只会拿到半截文本，连异常都不抛
        # （对比 chat_sync 在空内容/非空截断两处都有 raise）。半截文本解析不出学习记录，
        # record_learning_from_text 会静默 return。用户是**手动点了按钮**的，不能让他
        # 以为记录已经存上——所以这里必须出声。
        # 注（P7 批次 13，2026-09-27）：stream_chat 已于 P7 批次 13 加守卫（读
        # finish_reason，值为 'length' 时把提示追加在既有全文之后）。本条降级**仍然
        # 保留**：截断提示只告知"回答短了一截"，不保证拿到的是完整可解析的学习 JSON。
        # 只对"手动"这条路出声：事件订阅那条路（普通聊天每轮都触发）的静默是正确行为。
        history.append({
            "role": "assistant",
            "content": "⚠️ 本次总结没能解析成学习记录，因此没有写入任何记录。"
                       "模型这次没有输出合法的学习 JSON（可能被截断）。"
                       "可以再点一次「📝 结束学习并总结」。",
        })
        yield list(history)


def save_current(history, sid):
    if sid and history:
        # 存盘前统一规范化：content 一律为纯文本字符串，
        # 杜绝 Gradio 6 parts 列表结构写入会话 JSON（历史缺陷的数据源头）
        chat_store.save_history(sid, normalize_chat_history(history))
    return sid


def refresh_session_list():
    sessions = chat_store.get_all_sessions()
    choices = [f"{s['id']} - {s['title']}" for s in sessions]
    return gr.update(choices=choices, value=choices[0] if choices else None)


def create_new_session():
    sid = chat_store.create_session()
    return sid, refresh_session_list(), [], "当前会话：新会话"


def load_session(selected, current_sid):
    if not selected:
        return current_sid, [], "未选择会话"
    sid = selected.split(" - ")[0].strip()
    history = chat_store.load_history(sid)
    sessions = chat_store.get_all_sessions()
    info = next((s for s in sessions if s["id"] == sid), None)
    if info:
        info_text = f"📂 {info['title']} | 消息数：{info['message_count']}"
    else:
        info_text = f"会话：{sid}"
    return sid, history, info_text


def delete_current_session(sid):
    if not sid:
        new_sid = chat_store.create_session()
        return new_sid, [], refresh_session_list(), "无会话"
    chat_store.delete_session(sid)
    new_sid = chat_store.create_session()
    return new_sid, [], refresh_session_list(), f"已删除旧会话，新建：{new_sid}"


def rename_session(new_name, sid):
    if not sid or not new_name.strip():
        return refresh_session_list()
    chat_store.rename_session(sid, new_name.strip())
    return refresh_session_list()


def do_export(history, fmt, sid):
    if not history or not sid:
        return None
    sessions = chat_store.get_all_sessions()
    title = next((s["title"] for s in sessions if s["id"] == sid), "对话记录")
    if fmt == "TXT":
        content = export_as_txt(history, title)
        filename = f"{title}.txt"
    else:
        content = export_as_markdown(history, title)
        filename = f"{title}.md"
    filepath = f"data/conversations/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def show_confirm():
    return (gr.update(visible=True),) * 3


def hide_confirm():
    return (gr.update(visible=False),) * 3


def do_clear(sid):
    clear_duplicate_cache()
    return [], sid, (gr.update(visible=False),) * 3
