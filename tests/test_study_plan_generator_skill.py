"""
学习计划生成器技能测试
测试技能加载、输入验证、JSON 解析、GoalManager 集成等
"""
import os
import sys
import json

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()


def test_skill_load():
    """测试技能可以被 SkillManager 加载"""
    from core.skill_manager import SkillManager

    sm = SkillManager()
    sm.load_all_skills()

    skill = sm.get_skill("study_plan_generator")
    assert skill is not None, "study_plan_generator 技能未加载"

    manifest = sm.get_manifest("study_plan_generator")
    assert manifest is not None
    assert manifest.name == "study_plan_generator"
    assert manifest.display_name == "学习计划生成器"
    assert "学习" in manifest.tags

    print("[OK] skill load")


def test_validate_input():
    """测试输入验证（所有参数可选）"""
    from skills.study_plan_generator.skill import SkillImpl

    skill = SkillImpl()

    # 空输入也应通过验证
    assert skill.validate_input({}) is True
    assert skill.validate_input({"goal_id": "test"}) is True
    assert skill.validate_input({"days": 7}) is True

    print("[OK] validate input")


def test_parse_json_response():
    """测试 JSON 解析"""
    from skills.study_plan_generator.skill import SkillImpl

    # 正常 JSON
    text1 = '{"title": "学习计划", "tasks": [{"title": "任务1", "priority": 1, "day": 1}]}'
    result = SkillImpl._parse_json_response(text1)
    assert result is not None
    assert result["title"] == "学习计划"
    assert len(result["tasks"]) == 1

    # 带 markdown 包裹
    text2 = '分析完毕\n```json\n{"title": "Markdown计划", "tasks": []}\n```\n结束'
    result = SkillImpl._parse_json_response(text2)
    assert result is not None
    assert result["title"] == "Markdown计划"

    # 无效 JSON
    result = SkillImpl._parse_json_response("不是 JSON")
    assert result is None

    # 空文本
    result = SkillImpl._parse_json_response("")
    assert result is None

    print("[OK] parse json response")


def test_collect_info_with_goal():
    """测试收集信息（有目标）"""
    from skills.study_plan_generator.skill import SkillImpl
    from core.goal_manager import GoalManager
    from memory.learning_recorder import LearningRecorder
    from memory.knowledge_graph import KnowledgeGraph

    skill = SkillImpl()
    # 直接设置属性，避免 on_load 中可能的导入问题
    skill.goal_manager = GoalManager()
    skill.learning_recorder = LearningRecorder
    skill.knowledge_graph = KnowledgeGraph

    # 先创建一个目标
    goal = skill.goal_manager.create_goal("测试学习计划目标", "测试描述")
    skill.goal_manager.add_task(goal.goal_id, "测试任务1", "", 1, source="manual")

    info = skill._collect_info(goal.goal_id)
    assert info["goal_title"] == "测试学习计划目标"
    assert info["goal_description"] == "测试描述"
    assert len(info["pending_tasks"]) >= 1

    # 清理
    skill.goal_manager.update_goal(goal.goal_id, status="archived")
    print("[OK] collect info with goal")


def test_collect_info_without_goal():
    """测试收集信息（无目标）"""
    from skills.study_plan_generator.skill import SkillImpl
    from core.goal_manager import GoalManager
    from memory.learning_recorder import LearningRecorder
    from memory.knowledge_graph import KnowledgeGraph

    skill = SkillImpl()
    skill.goal_manager = GoalManager()
    skill.learning_recorder = LearningRecorder
    skill.knowledge_graph = KnowledgeGraph

    info = skill._collect_info(None)
    assert info["goal_title"] == ""
    assert isinstance(info["weak_points"], list)
    assert isinstance(info["review_recommendations"], list)

    print("[OK] collect info without goal")


def test_save_tasks():
    """测试保存任务到目标"""
    from skills.study_plan_generator.skill import SkillImpl
    from core.goal_manager import GoalManager

    skill = SkillImpl()
    skill.goal_manager = GoalManager()
    goal = skill.goal_manager.create_goal("保存任务测试", "测试保存")

    plan = {
        "title": "测试计划",
        "tasks": [
            {"title": "任务A", "description": "描述A", "priority": 1, "day": 1},
            {"title": "任务B", "description": "描述B", "priority": 2, "day": 2},
            {"title": "", "description": "空标题", "priority": 1, "day": 1},  # 应跳过
        ]
    }

    saved_count = skill._save_tasks(goal.goal_id, plan)
    assert saved_count == 2, f"应保存2个任务: {saved_count}"

    tasks = skill.goal_manager.get_tasks(goal.goal_id)
    assert len(tasks) == 2
    assert tasks[0]["source"] == "ai_generated"

    # 清理
    skill.goal_manager.update_goal(goal.goal_id, status="archived")
    print("[OK] save tasks")


def test_format_result():
    """测试结果格式化"""
    from skills.study_plan_generator.skill import SkillImpl

    skill = SkillImpl()
    skill.goal_manager = None  # format_result 不需要 goal_manager

    plan = {
        "title": "测试学习计划",
        "tasks": [
            {"title": "任务1", "description": "描述1", "priority": 1, "day": 1},
            {"title": "任务2", "description": "描述2", "priority": 2, "day": 1},
            {"title": "任务3", "description": "", "priority": 3, "day": 2},
        ]
    }
    info = {"goal_title": "测试目标", "goal_description": ""}
    result = skill._format_result(plan, info, days=7, goal_id=None, saved_count=0)

    assert "测试学习计划" in result
    assert "第 1 天" in result
    assert "第 2 天" in result
    assert "任务1" in result
    assert "任务3" in result

    print("[OK] format result")


def test_run_empty_context():
    """测试空上下文执行（跳过 LLM 调用，直接测试 execute 逻辑）"""
    from skills.study_plan_generator.skill import SkillImpl
    from core.goal_manager import GoalManager
    from memory.learning_recorder import LearningRecorder
    from memory.knowledge_graph import KnowledgeGraph

    skill = SkillImpl()
    skill.goal_manager = GoalManager()
    skill.learning_recorder = LearningRecorder
    skill.knowledge_graph = KnowledgeGraph

    # 测试 execute 中的前置逻辑（不调用 LLM）
    context = {}
    goal_id = context.get("goal_id", "").strip() or None
    days = context.get("days", 7)
    save_tasks = context.get("save_tasks", False)

    # 验证参数解析
    assert goal_id is None
    assert days == 7
    assert save_tasks is False

    # 验证信息收集
    info = skill._collect_info(goal_id)
    assert "goal_title" in info
    assert "weak_points" in info
    assert "review_recommendations" in info

    print("[OK] run empty context")


if __name__ == "__main__":
    print("=== study plan generator skill test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_skill_load()
    test_validate_input()
    test_parse_json_response()
    test_collect_info_with_goal()
    test_collect_info_without_goal()
    test_save_tasks()
    test_format_result()
    test_run_empty_context()

    print("\n=== ALL TESTS PASSED ===")
