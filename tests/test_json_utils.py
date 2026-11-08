# -*- coding: utf-8 -*-
"""JSON 提取统一实现（core/json_utils.py）测试 —— 纯函数，不涉及数据库

覆盖：
- extract_json_object：```json 代码块、无代码块纯 JSON、说明文字包裹、嵌套对象、
  数组/标量等非对象结果、非法 JSON、空输入（None / 空串 / 非字符串）；
- 关键回归：同一段文本含**两个** JSON 对象时只取第一个
  （收敛前的贪婪正则 r"\\{.*\\}" 会把首尾括号拼在一起导致解析失败并静默降级）；
- parse_learning_json：含 topic + summary 返回对象，缺任一字段或非对象返回 None；
- 收敛一致性：7 处历史实现已全部改为导入 core/json_utils，源文件内不再有本地
  同名 def（静态 AST 断言）+ 可安全导入的 6 个模块共享同一函数对象（身份断言）。
"""
import ast
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.json_utils import extract_json_object, parse_learning_json

# 需断言「不再有本地同名 def」的 7 个历史重复实现文件（ui/controllers/_shared.py
# 不导入：其模块级代码会 init_db 并构建运行时，测试只用静态 AST 校验）
_MIGRATED_FILES = [
    "agents/intent_agent.py",
    "agents/planner_agent.py",
    "agents/planning_mirror_agent.py",
    "agents/evaluator.py",
    "agents/supervisor.py",
    "core/event_handlers.py",
    "ui/controllers/_shared.py",
]

# 收敛后不应再出现在上述文件顶层的 JSON 提取函数名
_FORBIDDEN_DEFS = {"extract_json", "_extract_json", "extract_json_object",
                   "parse_learning_json"}


def test_code_fence():
    """标准 ```json 代码块：夹在说明文字中也能取到对象"""
    text = '好的，我来分析：\n```json\n{"assessment": "correct", "score": 3}\n```\n以上。'
    data = extract_json_object(text)
    assert data == {"assessment": "correct", "score": 3}, data

    # 裸 ``` 围栏（无 json 语言标记）同样支持
    bare = '```\n{"topic": "闭包"}\n```'
    assert extract_json_object(bare) == {"topic": "闭包"}
    print("[OK] 代码块提取：```json 与裸 ``` 围栏均可用")


def test_plain_json():
    """无代码块的纯 JSON（含首尾空白与换行）"""
    assert extract_json_object('  {"a": 1}  ') == {"a": 1}
    assert extract_json_object('{\n  "b": [1, 2],\n  "c": null\n}') == {"b": [1, 2], "c": None}
    print("[OK] 纯 JSON 提取：无围栏亦可解析")


def test_two_objects_takes_first():
    """关键回归：含两个 JSON 对象时只取第一个（贪婪匹配缺陷已修复）"""
    text = '第一个：{"id": 1, "name": "甲"}\n第二个：{"id": 2, "name": "乙"}'
    data = extract_json_object(text)
    assert data == {"id": 1, "name": "甲"}, \
        f"应只取第一个对象，实际：{data}（贪婪匹配会把首尾括号拼在一起导致解析失败）"

    # 对象之间夹带说明文字同样只取第一个
    text2 = '{"step": 1} 然后继续说明 {"step": 2}'
    assert extract_json_object(text2) == {"step": 1}
    print("[OK] 多对象文本：只提取第一个（贪婪匹配缺陷回归通过）")


def test_nested_object():
    """嵌套对象：括号栈按平衡配对，不会在中途截断"""
    text = '```json\n{"plan": [{"task_id": 1, "meta": {"deep": {"k": "v"}}}], "summary": "s"}\n```'
    data = extract_json_object(text)
    assert data is not None
    assert data["plan"][0]["meta"]["deep"] == {"k": "v"}
    assert data["summary"] == "s"
    print("[OK] 嵌套对象：括号栈平衡配对正确")


def test_fence_priority():
    """围栏优先：说明文字里出现的花括号不应干扰围栏内对象的提取"""
    text = '提示：这里提到了 {占位} 这种写法\n```json\n{"ok": true}\n```'
    assert extract_json_object(text) == {"ok": True}
    print("[OK] 围栏优先：围栏内对象优先于正文花括号")


def test_invalid_inputs():
    """非法输入一律返回 None，且不抛异常"""
    cases = [
        None, 123, ["{", "}"], {"a": 1},
        "", "   ", "没有任何 JSON 的纯文字",
        "{不是 JSON}", '{"a": }',
        '{"a": 1', '[]', '"just a string"', '{"unbalanced": "}"}',
    ]
    for case in cases:
        result = extract_json_object(case)
        assert result is None, f"输入 {case!r} 应返回 None，实际 {result!r}"
    print(f"[OK] 非法输入：{len(cases)} 种均返回 None 且不抛异常")


def test_parse_learning_json():
    """parse_learning_json：topic + summary 齐备才算有效学习记录"""
    valid = '```json\n{"topic": "偏导数", "summary": "理解了方向导数", "mastery_score": 3}\n```'
    data = parse_learning_json(valid)
    assert data is not None and data["topic"] == "偏导数", data
    assert data["mastery_score"] == 3

    # 缺少 topic / summary 任一字段 → None
    assert parse_learning_json('{"topic": "只有主题"}') is None
    assert parse_learning_json('{"summary": "只有总结"}') is None
    # 合法 JSON 但非对象 → None
    assert parse_learning_json("[1, 2, 3]") is None
    # 非法 / 空输入 → None
    assert parse_learning_json("") is None
    assert parse_learning_json(None) is None
    print("[OK] parse_learning_json：字段契约与非法输入处理正确")


def test_no_local_duplicate_defs():
    """收敛回归：7 个历史文件顶层不再定义 JSON 提取函数（静态 AST 断言）"""
    root = Path(__file__).resolve().parent.parent
    for rel in _MIGRATED_FILES:
        path = root / rel
        assert path.exists(), f"待检文件不存在：{rel}"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        found = sorted(
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _FORBIDDEN_DEFS
        )
        assert not found, f"{rel} 仍存在本地 JSON 提取实现：{found}"
    print(f"[OK] 收敛一致性：{len(_MIGRATED_FILES)} 个文件均无本地 JSON 提取 def")


def test_shared_function_identity():
    """收敛回归：可安全导入的 6 个模块共享 core/json_utils 的同一函数对象"""
    from agents import evaluator, intent_agent, planner_agent, planning_mirror_agent, supervisor
    from core import event_handlers

    # 三兄弟保留 extract_json 名称（reflection_agent / daily_planning_agent 仍从此导入）
    for mod in (intent_agent, planner_agent, planning_mirror_agent):
        assert mod.extract_json is extract_json_object, \
            f"{mod.__name__}.extract_json 未指向统一实现"
    assert evaluator.extract_json_object is extract_json_object
    assert supervisor.extract_json_object is extract_json_object
    assert event_handlers.parse_learning_json is parse_learning_json
    print("[OK] 统一实现身份：6 个模块共享 core/json_utils 的同一函数对象")


if __name__ == "__main__":
    print("=== json utils test ===\n")
    test_code_fence()
    test_plain_json()
    test_two_objects_takes_first()
    test_nested_object()
    test_fence_priority()
    test_invalid_inputs()
    test_parse_learning_json()
    test_no_local_duplicate_defs()
    test_shared_function_identity()
    print("\n=== ALL TESTS PASSED ===")
