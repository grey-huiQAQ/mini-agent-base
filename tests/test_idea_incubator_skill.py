"""
想法孵化器技能测试
测试技能加载、输入验证、JSON 解析等基础逻辑（mock LLM 调用）
"""
import os
import sys
import json
from unittest.mock import patch

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()


def test_skill_load():
    """测试技能可以被 SkillManager 加载"""
    from core.skill_manager import SkillManager

    sm = SkillManager()
    count = sm.load_all_skills()

    # 应至少加载 concept_explainer 和 idea_incubator
    skill = sm.get_skill("idea_incubator")
    assert skill is not None, "idea_incubator 技能未加载"

    manifest = sm.get_manifest("idea_incubator")
    assert manifest is not None
    assert manifest.name == "idea_incubator"
    assert manifest.display_name == "想法孵化器"
    assert "创意" in manifest.tags

    print("[OK] skill load")


def test_validate_input():
    """测试输入验证"""
    from skills.idea_incubator.skill import SkillImpl

    skill = SkillImpl()

    # 有效输入
    assert skill.validate_input({"idea": "我想做一个个人博客"}) is True

    # 空输入
    assert skill.validate_input({"idea": ""}) is False
    assert skill.validate_input({"idea": "   "}) is False
    assert skill.validate_input({}) is False

    print("[OK] validate input")


def test_parse_json_response():
    """测试 JSON 解析"""
    from skills.idea_incubator.skill import SkillImpl

    # 正常 JSON
    text1 = '{"goal_title": "测试目标", "goal_description": "描述"}'
    result = SkillImpl._parse_json_response(text1)
    assert result is not None
    assert result["goal_title"] == "测试目标"

    # 带 markdown 包裹的 JSON
    text2 = '这是分析\n```json\n{"goal_title": "分析目标", "tasks": []}\n```\n结束'
    result = SkillImpl._parse_json_response(text2)
    assert result is not None
    assert result["goal_title"] == "分析目标"

    # 无效 JSON
    text3 = "这不是 JSON"
    result = SkillImpl._parse_json_response(text3)
    assert result is None

    # 空文本
    result = SkillImpl._parse_json_response("")
    assert result is None

    print("[OK] parse json response")


def test_skill_run_empty_input():
    """测试空输入执行"""
    from core.skill_manager import SkillManager

    sm = SkillManager()
    sm.load_all_skills()

    result = sm.run_skill("idea_incubator", {"idea": ""})
    assert "error" in result

    result = sm.run_skill("idea_incubator", {})
    assert "error" in result

    print("[OK] run empty input")


def test_skill_run_invalid_mode():
    """无效模式回退为 goal 分支：离线 mock + 能失败的断言（P7 批次 11-②）

    为什么重写：原用例没有任何 mock，而 idea_incubator 的 goal 分支会真的走到
    chat_sync_with_retry_budget（skills/idea_incubator/skill.py 的 _incubate_to_goal）
    ——故它每跑一次全量回归就真打一次生产端点、消耗一次额度；而它唯一的断言是
    `assert isinstance(result, dict)`，可 SkillImpl.execute 的**每一条**返回路径
    （含 {"error": ...} 与异常兜底）都返回 dict，该断言在任何情况下都成立，
    属"不可能失败的用例"。

    现在两条断言都能失败：
      ① 恰好调用一次 LLM —— 多一次说明重试轮被触发，零次说明根本没走到 LLM；
      ② 结果里**没有** task_count —— _incubate_to_plan 的返回值恒含 task_count，
         _incubate_to_goal 恒不含，故这是"确实走了 goal 分支"的判别式。只断言
         goal_id 不够：两个分支都会写 goal_id。
    """
    from core.skill_manager import SkillManager
    from core.goal_manager import GoalManager
    from core.ai_filter import DEFAULT_MAX_TOKENS
    # 复用 tests/test_llm_budget.py 的逐次预算记录器（不再手工复制第二份）
    from tests.test_llm_budget import _recorder

    sm = SkillManager()
    sm.load_all_skills()

    reply = json.dumps({
        "questions": "1. 这个想法要解决什么问题？\n2. 第一步能做什么？",
        "analysis": "想法清晰、可执行",
        "goal_title": "无效模式回退目标",
        "goal_description": "mode=invalid 应回退到 goal 分支",
    }, ensure_ascii=False)
    fake, calls = _recorder(lambda budget, n: reply)

    with patch("business.model_client.chat_sync", fake):
        result = sm.run_skill("idea_incubator", {"idea": "测试想法", "mode": "invalid"})

    assert result.get("goal_id"), f"goal 分支应创建并返回目标，实际 {result}"
    assert "task_count" not in result, \
        f"mode=invalid 应回退到 goal 分支（plan 分支恒带 task_count），实际 {result}"
    assert len(calls) == 1, f"应恰好调用一次 LLM，实际调用了 {len(calls)} 次：{calls}"
    assert calls[0] == DEFAULT_MAX_TOKENS, \
        f"首档预算应为 DEFAULT_MAX_TOKENS={DEFAULT_MAX_TOKENS}，实际 {calls}"

    # 清理：把本次新建的目标 archive 掉（同本文件 test_goal_manager_integration 的做法）
    GoalManager().update_goal(result["goal_id"], status="archived")

    print(f"[OK] run invalid mode（离线，恰好 {len(calls)} 次调用，走 goal 分支）")


def test_goal_manager_integration():
    """测试与 GoalManager 的集成（直接调用，不涉及 LLM）"""
    from core.goal_manager import GoalManager

    gm = GoalManager()
    goal = gm.create_goal("孵化器测试目标", "测试想法孵化器创建的目标")

    # 验证目标创建成功
    retrieved = gm.get_goal(goal.goal_id)
    assert retrieved is not None
    assert retrieved.title == "孵化器测试目标"

    # 验证 can add_task with source
    gm.add_task(goal.goal_id, "测试任务", "描述", 1, source="ai_generated")
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 1
    assert tasks[0]["source"] == "ai_generated"

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal manager integration")


if __name__ == "__main__":
    print("=== idea incubator skill test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_skill_load()
    test_validate_input()
    test_parse_json_response()
    test_skill_run_empty_input()
    test_skill_run_invalid_mode()
    test_goal_manager_integration()

    print("\n=== ALL TESTS PASSED ===")
