# -*- coding: utf-8 -*-
"""core.json_utils —— 项目内「从 LLM 回复中提取 JSON」的唯一权威实现

目的：
    统一 Agent / 事件处理两处的 JSON 提取口径，消除重复实现，以及由贪婪正则
    带来的「解析失败即静默降级」缺陷。

收敛背景（2026-09-12）：
    本模块建立前，项目内共 7 处重复实现（5 种行为）：
    - agents/intent_agent.py、agents/planner_agent.py、agents/planning_mirror_agent.py
      三份逐字相同的 extract_json：用贪婪正则 r"\\{.*\\}"（DOTALL）抓取，
      同一段文本里出现两个 JSON 对象时会从首个 "{" 一路吃到末个 "}"，
      拼出的子串必然非法 → 返回 None，调用方只能静默降级为兜底逻辑；
    - agents/evaluator.py 的「整体 JSON 解析 + 括号计数」版（不剥代码块）；
    - agents/supervisor.py 的内联解析（无括号兜底）；
    - core/event_handlers.py 的括号栈版；
    - ui/controllers/_shared.py 中一份无任何调用方、且缺少 import re / import json 的
      死副本（一旦被调用即 NameError）。
    现已全部改为导入本模块：7 处收敛为 1 处，三兄弟的贪婪匹配缺陷随之消失。

行为约定（纯函数：不打日志、不写库、不抛异常）：
    - extract_json_object(text)：取文本中**第一个括号平衡**的 {...} 子串并解析为
      dict；输入为空 / 非字符串 / JSON 非法 / 解析结果不是 dict，一律返回 None；
      优先在 ```json 或 ``` 围栏代码块内定位，围栏缺失时在整段文本上扫描。
    - parse_learning_json(text)：在上者基础上要求同时含 "topic" 与 "summary"
      两个字段（学习记录的最小契约），否则返回 None。

已知边界（如实记录，本次收敛刻意保持算法不变）：
    括号栈按字符配对、**不感知字符串字面量**——若 JSON 字符串值内部含未转义的
    花括号（形如 {"a": "}"}），定位会偏移并返回 None。这是沿用原
    core/event_handlers.py 实现时的既有语义；模型正常输出的 JSON 不受影响。
"""
import json
import re
from typing import Any, Dict, Optional

__all__ = [
    "extract_json_object",
    "parse_learning_json",
]

# 围栏代码块：```json ... ``` 或 ``` ... ```（json 语言标记可省略）
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _extract_balanced_object(text: str) -> Optional[str]:
    """返回 text 中第一个括号平衡的 {...} 子串；找不到返回 None

    实现：从首个 "{" 起用栈记录未闭合的左括号位置，栈再次变空时即为一个完整对象。
    遇到多余的右括号（括号不平衡）立即放弃，不做猜测性修补。
    """
    start = text.find("{")
    if start == -1:
        return None
    stack = []
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            stack.append(i)
        elif char == "}":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从文本中提取第一个 JSON 对象并解析为 dict；失败返回 None

    Args:
        text: 模型回复或任意文本；为空 / 非字符串时直接返回 None。

    Returns:
        解析成功且结果为 JSON 对象时返回 dict，其余情况返回 None。
    """
    if not text or not isinstance(text, str):
        return None
    fenced = _CODE_FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    json_str = _extract_balanced_object(candidate)
    if not json_str:
        return None
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_learning_json(text: str) -> Optional[Dict[str, Any]]:
    """从文本中提取学习记录 JSON（须同时含 "topic" 与 "summary" 字段）

    供 core/event_handlers.record_learning_from_text 等调用方使用：缺少任一必填
    字段即视为无效学习记录并返回 None（与收敛前 core/event_handlers.py 的行为一致）。
    """
    data = extract_json_object(text)
    if isinstance(data, dict) and "topic" in data and "summary" in data:
        return data
    return None
