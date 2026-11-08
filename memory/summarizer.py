"""
对话摘要生成器
当对话超过阈值时，自动压缩历史对话为摘要
"""

from business.model_client import chat_sync


# 摘要提示词
SUMMARY_PROMPT = """请用一段话（不超过 200 字）总结以下对话的核心内容，保留：

1. 用户的学习目标和背景
2. 已讨论的关键知识点
3. 用户的薄弱点和待解决问题
4. 任何重要的用户偏好或习惯

对话内容：
{conversation}

请直接输出摘要，不要加任何前缀或解释。"""


def generate_summary(messages: list) -> str:
    """
    生成对话摘要

    Args:
        messages: 需要压缩的消息列表 [{"role":"...","content":"..."}]

    Returns:
        摘要文本
    """
    # 将消息列表转为可读文本
    conversation_text = ""
    for msg in messages:
        role = "👤 用户" if msg["role"] == "user" else "🤖 助手"
        content = msg.get("content", "")[:500]  # 每条消息截断
        conversation_text += f"{role}：{content}\n"

    prompt = SUMMARY_PROMPT.format(conversation=conversation_text)

    summary_messages = [
        {"role": "system", "content": "你是对话摘要助手，负责压缩对话内容。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result = chat_sync(summary_messages, max_tokens=300, temperature=0.3)
        return f"[历史对话摘要]\n{result.strip()}"
    except Exception as e:
        print(f"[摘要] 生成失败：{e}")
        return ""


def compress_history(history: list, keep_recent: int = 4, max_total: int = 12) -> list:
    """
    压缩对话历史

    策略：
    1. 保留最近 keep_recent 轮（用户+助手各算一轮）
    2. 把更早的对话压缩为摘要
    3. 如果历史不超过 max_total 条，不做处理

    Args:
        history: 完整历史 [{"role":"...","content":"..."}]
        keep_recent: 保留最近 N 条消息
        max_total: 超过多少条才开始压缩

    Returns:
        压缩后的消息列表
    """
    if len(history) <= max_total:
        return history

    # 需要压缩的消息数量（保留最近 keep_recent 条）
    keep_count = min(keep_recent, len(history))
    recent = history[-keep_count:]
    to_compress = history[:-keep_count]

    if not to_compress:
        return history

    # 生成摘要
    summary = generate_summary(to_compress)

    if not summary:
        return history  # 摘要失败则原样返回

    # 构建压缩后的消息
    compressed = [
        {"role": "system", "content": summary},
    ]
    compressed.extend(recent)

    return compressed