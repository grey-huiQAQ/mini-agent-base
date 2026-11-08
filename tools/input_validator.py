"""
用户输入验证与去重检测
"""
import hashlib
from collections import deque

# 保留最近 5 条输入用于去重检测
_recent_input_hashes: deque = deque(maxlen=5)


def validate_input(message: str, max_length: int = 2000) -> tuple[bool, str]:
    """
    验证用户输入

    Args:
        message: 用户原始输入
        max_length: 允许的最大字符数

    Returns:
        (是否有效, 处理后的消息或错误提示)
    """
    stripped = message.strip() if message else ""

    # 空输入检测
    if not stripped:
        return False, "⚠️ 输入不能为空，请输入你的问题。"

    # 长度限制
    if len(stripped) > max_length:
        return False, f"⚠️ 输入过长（超过 {max_length} 字符），请精简后重新发送。"

    return True, stripped


def is_duplicate(message: str) -> bool:
    """
    检测是否为最近重复输入（通过 MD5 哈希比对）

    Args:
        message: 用户输入内容

    Returns:
        True 表示重复，False 表示不重复
    """
    msg_hash = hashlib.md5(message.strip().encode("utf-8")).hexdigest()

    if msg_hash in _recent_input_hashes:
        return True

    _recent_input_hashes.append(msg_hash)
    return False


def clear_duplicate_cache():
    """清空去重缓存（用于清空对话时调用）"""
    _recent_input_hashes.clear()