# -*- coding: utf-8 -*-
"""
PlannerAgent 与里程碑层级测试（离线：通过替换 model_client.chat_sync 模拟 LLM）
覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
- 里程碑 CRUD 与任务挂接
- 旧数据兼容（无里程碑任务 / goals 新字段默认值）
- 分阶段计划生成与写入（mock LLM）
- 冲突分析建议的校验、过滤与应用
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；本文件不再引用 DB_PATH 变量，故只保留 helper 返回的目录名。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

from core.goal_manager import GoalManager
from memory.database import init_db
from agents import planner_agent as pa_module
from agents.planner_agent import PlannerAgent


def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()




def test_milestone_crud():
    """测试里程碑增删改查"""
    gm = GoalManager()
    goal = gm.create_goal("ms test goal")
    mid = gm.add_milestone(goal.goal_id, "第一阶段：基础", "打基础", order_index=1, duration_weeks=2)
    assert mid and mid > 0

    ms_list = gm.get_milestones(goal.goal_id)
    assert len(ms_list) == 1
    assert ms_list[0]["title"] == "第一阶段：基础"
    assert ms_list[0]["duration_weeks"] == 2
    assert ms_list[0]["order_index"] == 1

    gm.update_milestone_status(mid, "done")
    assert gm.get_milestones(goal.goal_id)[0]["status"] == "done"

    gm.delete_milestone(mid)
    assert gm.get_milestones(goal.goal_id) == []
    print("[OK] milestone crud")


def test_task_with_milestone_and_legacy_compat():
    """测试任务挂接里程碑；无里程碑旧任务 milestone_id 为 None"""
    gm = GoalManager()
    goal = gm.create_goal("task ms goal")
    mid = gm.add_milestone(goal.goal_id, "阶段A", order_index=1)

    gm.add_task(goal.goal_id, "挂在里程碑下的任务", milestone_id=mid)
    gm.add_task(goal.goal_id, "旧式无里程碑任务")  # 兼容旧调用方式

    tasks = gm.get_tasks(goal.goal_id)
    by_title = {t["title"]: t for t in tasks}
    assert by_title["挂在里程碑下的任务"]["milestone_id"] == mid
    # 旧数据兼容：未挂里程碑的任务该字段为空
    assert by_title["旧式无里程碑任务"]["milestone_id"] is None
    print("[OK] task with milestone and legacy compat")


def test_delete_milestone_keeps_tasks():
    """删除里程碑后其任务保留且解除关联"""
    gm = GoalManager()
    goal = gm.create_goal("del ms goal")
    mid = gm.add_milestone(goal.goal_id, "将删除的阶段")
    gm.add_task(goal.goal_id, "幸存任务", milestone_id=mid)

    gm.delete_milestone(mid)
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 1, "任务不应被删除"
    assert tasks[0]["milestone_id"] is None, "任务的里程碑关联应被解除"
    print("[OK] delete milestone keeps tasks")


def test_goal_priority_deadline_fields():
    """测试 goals 新增的 priority/deadline 字段默认值与更新"""
    gm = GoalManager()
    goal = gm.create_goal("prio goal")
    loaded = gm.get_goal(goal.goal_id)
    assert loaded.priority == 2, "默认优先级应为 2"
    assert loaded.deadline == "", "默认截止日期应为空"

    gm.update_goal(goal.goal_id, priority=1, deadline="2026-12-31")
    updated = gm.get_goal(goal.goal_id)
    assert updated.priority == 1
    assert updated.deadline == "2026-12-31"
    print("[OK] goal priority/deadline fields")


def test_update_task_priority_clamp():
    """测试任务优先级更新与范围钳制"""
    gm = GoalManager()
    goal = gm.create_goal("clamp goal")
    gm.add_task(goal.goal_id, "t1")
    task = gm.get_tasks(goal.goal_id)[0]

    gm.update_task_priority(task["id"], 9)
    assert gm.get_tasks(goal.goal_id)[0]["priority"] == 3, "应钳制到最大 3"

    gm.update_task_priority(task["id"], 0)
    assert gm.get_tasks(goal.goal_id)[0]["priority"] == 1, "应钳制到最小 1"
    print("[OK] update task priority clamp")


def test_active_goals_overview():
    """测试冲突分析用的活跃目标概览结构"""
    gm = GoalManager()
    goal = gm.create_goal("overview goal")
    gm.add_task(goal.goal_id, "t1")

    overview = gm.get_active_goals_overview()
    target = [g for g in overview if g["goal_id"] == goal.goal_id]
    assert len(target) == 1
    keys = {"goal_id", "title", "priority", "deadline",
            "total_tasks", "completed_tasks", "pending_tasks", "completion_rate"}
    assert keys.issubset(target[0].keys())
    assert target[0]["total_tasks"] == 1
    print("[OK] active goals overview")


def test_validate_plan_filters_invalid():
    """测试计划校验器对非法结构的过滤"""
    agent = PlannerAgent(GoalManager())
    bad = agent._validate_plan({"milestones": "not a list"})
    assert bad is None

    dirty = {"milestones": [
        {"title": "", "tasks": [{"title": "x"}]},                       # 空标题 → 过滤
        {"title": "没有任务的阶段"},                                     # 无任务 → 过滤
        {"title": "正常阶段", "description": "d", "duration_weeks": "3",
         "tasks": [{"title": "合法任务", "priority": "2"},
                   {"title": "  ", "priority": 1},                        # 空标题任务 → 过滤
                   "junk"]}                                               # 非法元素 → 过滤
    ]}
    cleaned = agent._validate_plan(dirty)
    assert cleaned is not None and len(cleaned) == 1
    ms = cleaned[0]
    assert len(ms["tasks"]) == 1
    assert ms["duration_weeks"] == 3 and ms["tasks"][0]["priority"] == 2
    print("[OK] validate plan filters invalid")


def test_generate_staged_plan_with_mock_llm():
    """mock LLM 全链路：生成分阶段计划（新目标路径）"""
    gm = GoalManager()
    agent = PlannerAgent(gm)

    fake_response = '''```json
{"milestones":[
  {"title":"第一阶段：基础准备","description":"入门","duration_weeks":2,
   "tasks":[{"title":"学基础概念","description":"每天1小时","priority":1},
            {"title":"做练习题","priority":2}]},
  {"title":"第二阶段：进阶实战","duration_weeks":3,
   "tasks":[{"title":"完成小项目","priority":1}]}
]}```'''
    original = pa_module.model_client.chat_sync
    try:
        pa_module.model_client.chat_sync = lambda messages, **kw: fake_response
        result = agent.generate_staged_plan(new_goal_title="mock 计划目标", total_weeks=5)
        assert result["ok"], result["message"]
        staged = result["plan"]
        assert len(staged.milestones) == 2
        assert staged.milestones[0]["tasks"][0]["title"] == "学基础概念"
        assert "第一阶段：基础准备" in result["display"]
        # 目标已创建
        assert gm.get_goal(staged.goal_id) is not None
    finally:
        pa_module.model_client.chat_sync = original
    print("[OK] generate staged plan with mock llm")


def test_apply_staged_plan_writes_db():
    """确认写入：里程碑与任务落库，任务挂到对应里程碑"""
    gm = GoalManager()
    agent = PlannerAgent(gm)

    from agents.planner_agent import StagedPlan
    goal = gm.create_goal("apply plan goal")
    staged = StagedPlan(
        goal_id=goal.goal_id, goal_title=goal.title,
        milestones=[{
            "title": "阶段一", "description": "desc", "duration_weeks": 2,
            "tasks": [
                {"title": "任务1", "description": "", "priority": 1},
                {"title": "任务2", "description": "", "priority": 2},
            ],
        }],
    )
    result = agent.apply_staged_plan(staged)
    assert result["ok"] and result["milestone_count"] == 1 and result["task_count"] == 2

    ms_list = gm.get_milestones(goal.goal_id)
    assert len(ms_list) == 1 and ms_list[0]["title"] == "阶段一"
    tasks = gm.get_tasks(goal.goal_id)
    assert all(t["milestone_id"] == ms_list[0]["id"] for t in tasks), "任务应挂到里程碑下"
    assert all(t["source"] == "ai_generated" for t in tasks)
    print("[OK] apply staged plan writes db")


def test_analyze_conflicts_with_mock_llm():
    """mock LLM 冲突分析：返回建议并过滤非法项"""
    gm = GoalManager()
    agent = PlannerAgent(gm)
    g1 = gm.create_goal("冲突分析目标A")
    g2 = gm.create_goal("冲突分析目标B")

    def fake_chat_sync(messages, **kwargs):
        return '''{"analysis":"两个目标争抢精力",
"suggestions":[
  {"goal_id":"''' + g1.goal_id + '''","action":"focus","reason":"进度更快，先完成","new_priority":1,"new_deadline":"2026-10-01","task_advice":""},
  {"goal_id":"''' + g2.goal_id + '''","action":"pause","reason":"精力不足","new_priority":null,"new_deadline":null,"task_advice":""},
  {"goal_id":"goal_not_exist","action":"focus","reason":"非法目标"},
  {"goal_id":"''' + g2.goal_id + '''","action":"hack","reason":"非法动作"}
]}'''

    original = pa_module.model_client.chat_sync
    try:
        pa_module.model_client.chat_sync = fake_chat_sync
        result = agent.analyze_conflicts()
        assert result["ok"], result["message"]
        # 非法 goal_id 和非法 action 被过滤
        assert len(result["suggestions"]) == 2
        assert "冲突分析" in result["display"]
    finally:
        pa_module.model_client.chat_sync = original
    print("[OK] analyze conflicts with mock llm")


def test_apply_conflict_suggestions_updates_db():
    """应用建议：优先级/截止日期更新，pause 归档目标"""
    gm = GoalManager()
    agent = PlannerAgent(gm)
    g1 = gm.create_goal("待调整目标")
    g2 = gm.create_goal("待搁置目标")

    suggestions = [
        {"goal_id": g1.goal_id, "action": "adjust_deadline",
         "new_priority": 1, "new_deadline": "2026-11-15", "reason": "", "task_advice": ""},
        {"goal_id": g2.goal_id, "action": "pause",
         "new_priority": None, "new_deadline": None, "reason": "", "task_advice": ""},
    ]
    result = agent.apply_conflict_suggestions(suggestions)
    assert result["ok"] and result["applied"] == 2

    updated = gm.get_goal(g1.goal_id)
    assert updated.priority == 1 and updated.deadline == "2026-11-15"
    paused = gm.get_goal(g2.goal_id)
    assert paused.status == "archived", "pause 建议应归档目标"
    print("[OK] apply conflict suggestions updates db")


def test_apply_staged_plan_replace_semantics():
    """replace_ai_pending=True：清除旧 AI 未完成任务，手动任务保留"""
    gm = GoalManager()
    agent = PlannerAgent(gm)
    goal = gm.create_goal("replace semantics goal")
    # 预置：一个 AI 旧任务（应被替换）、一个手动任务（应保留）、一个已完成 AI 任务（应保留）
    gm.add_task(goal.goal_id, "旧AI任务", source="ai_generated")
    gm.add_task(goal.goal_id, "手动任务", source="manual")
    done_task = gm.add_task(goal.goal_id, "已完成AI任务", source="ai_generated") or None
    tasks_before = gm.get_tasks(goal.goal_id)
    done_id = [t for t in tasks_before if t["title"] == "已完成AI任务"][0]["id"]
    gm.update_task_status(done_id, "done")

    from agents.planner_agent import StagedPlan
    staged = StagedPlan(
        goal_id=goal.goal_id, goal_title=goal.title,
        milestones=[{"title": "新阶段", "duration_weeks": 1,
                     "tasks": [{"title": "新任务", "description": "", "priority": 1}]}],
    )
    result = agent.apply_staged_plan(staged, replace_ai_pending=True)
    assert result["ok"], result["message"]

    titles = {t["title"] for t in gm.get_tasks(goal.goal_id)}
    assert "旧AI任务" not in titles, "旧的 AI 未完成任务应被替换"
    assert {"手动任务", "已完成AI任务", "新任务"}.issubset(titles), "手动/已完成/新计划任务应保留"
    print("[OK] apply staged plan replace semantics")


def test_cleanup():
    """清理测试数据"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== planner & milestone test ===\n")
    setup()
    test_milestone_crud()
    test_task_with_milestone_and_legacy_compat()
    test_delete_milestone_keeps_tasks()
    test_goal_priority_deadline_fields()
    test_update_task_priority_clamp()
    test_active_goals_overview()
    test_validate_plan_filters_invalid()
    test_generate_staged_plan_with_mock_llm()
    test_apply_staged_plan_writes_db()
    test_apply_staged_plan_replace_semantics()
    test_analyze_conflicts_with_mock_llm()
    test_apply_conflict_suggestions_updates_db()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
