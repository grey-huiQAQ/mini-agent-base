"""
对话导出功能
支持 TXT / Markdown 格式
"""

from datetime import datetime


def export_as_txt(history: list, session_title: str = "对话记录") -> str:
    """导出为纯文本格式"""
    lines = [f"{'='*50}"]
    lines.append(f"  {session_title}")
    lines.append(f"  导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"{'='*50}\n")

    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"👤 用户：\n{content}\n")
        elif role == "assistant":
            lines.append(f"🤖 助手：\n{content}\n")
        lines.append("-" * 40)

    return "\n".join(lines)


def export_as_markdown(history: list, session_title: str = "对话记录") -> str:
    """导出为 Markdown 格式"""
    lines = [f"# {session_title}"]
    lines.append(f"\n> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append("---\n")

    for i, msg in enumerate(history, 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"### 💬 第 {i} 轮 - 用户\n")
            lines.append(f"{content}\n")
        elif role == "assistant":
            lines.append(f"### 🤖 第 {i} 轮 - 助手\n")
            lines.append(f"{content}\n")
        lines.append("---\n")

    return "\n".join(lines)