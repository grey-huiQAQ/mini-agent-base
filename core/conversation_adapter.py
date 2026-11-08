from typing import Optional, List, Dict
from .interfaces import ConversationStore
from memory.chat_store import ChatStore


def message_content_to_text(content) -> str:
    """把 Chatbot 消息 content 统一收敛为纯文本字符串（Gradio 5/6 兼容）

    背景：Gradio 6 的 gr.Chatbot 在部分路径下会把 content 序列化为
    parts 列表（如 [{'text': '...', 'type': 'text'}]），这类结构曾被
    原样写入会话 JSON；后续对话链路对其调用 .strip() 会抛出
    AttributeError: 'list' object has no attribute 'strip'。

    转换规则：
    - str → 原样返回；
    - list/tuple → 拼接其中文本 part（str 或 {'text': ...} 字典），
      图片/文件等非文本 part 忽略；
    - None → ""；其余类型 str() 兜底。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return str(content)


def normalize_chat_history(history) -> List[Dict[str, str]]:
    """深拷贝并规范化的消息列表：每条消息 content 均为纯文本字符串

    非字典元素原样保留（不猜测结构）；不修改传入列表本身。
    """
    result: List[Dict[str, str]] = []
    for m in history or []:
        if isinstance(m, dict):
            result.append({
                "role": m.get("role", "user"),
                "content": message_content_to_text(m.get("content")),
            })
        else:
            result.append(m)
    return result


class JsonConversationStoreAdapter(ConversationStore):
    def __init__(self, chat_store: ChatStore):
        self._store = chat_store

    def create_session(self, title: str = "") -> str:
        return self._store.create_session(title)

    def save_message(self, session_id: str, role: str, content: str):
        history = self._store.load_history(session_id)
        # 入口防御：无论上游传入 str 还是 parts 列表，落盘一律为纯文本
        history.append({"role": role, "content": message_content_to_text(content)})
        self._store.save_history(session_id, history)

    def get_messages(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
        history = self._store.load_history(session_id)
        if limit:
            history = history[-limit:]
        # 出口防御：历史 JSON 中可能残留 list 型 content（旧版本写入），
        # 读取时统一规范化，保证 runtime / 模型消息链路只见到字符串
        return normalize_chat_history(history)

    def get_session(self, session_id: str) -> Dict:
        sessions = self._store.get_all_sessions()
        for s in sessions:
            if s["id"] == session_id:
                return s
        return {}

    def delete_session(self, session_id: str):
        self._store.delete_session(session_id)

    def list_sessions(self) -> List[Dict]:
        return self._store.get_all_sessions()
