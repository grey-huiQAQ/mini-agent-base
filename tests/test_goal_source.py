"""
目标任务来源字段（source）测试
验证手动任务保留、AI 任务删除、source 字段正确保存
"""
import os
import sys

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager
from memory.database import get_connection


def test_add_task_source_default():
    """测试 add_task 默认 source 为 manual"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_默认", "测试默认来源")

    gm.add_task(goal.goal_id, "手动任务1", "描述", 1)
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 1
    assert tasks[0]["source"] == "manual", f"默认来源应为 manual: {tasks[0]['source']}"

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] add_task source default")


def test_add_task_source_manual():
    """测试 add_task 显式指定 source='manual'"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_手动", "测试手动来源")

    gm.add_task(goal.goal_id, "手动任务", "描述", 1, source="manual")
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 1
    assert tasks[0]["source"] == "manual"

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] add_task source manual")


def test_add_task_source_ai():
    """测试 add_task 指定 source='ai_generated'"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_AI", "测试AI来源")

    gm.add_task(goal.goal_id, "AI任务", "描述", 1, source="ai_generated")
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 1
    assert tasks[0]["source"] == "ai_generated"

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] add_task source ai_generated")


def test_manual_tasks_preserved_on_regenerate():
    """测试重新生成时手动任务保留"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_保留手动", "测试手动任务保留")

    # 添加手动任务
    gm.add_task(goal.goal_id, "手动任务1", "描述1", 1, source="manual")
    gm.add_task(goal.goal_id, "手动任务2", "描述2", 2, source="manual")

    # 添加 AI 任务
    gm.add_task(goal.goal_id, "AI任务1", "描述3", 1, source="ai_generated")

    # 验证初始状态
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 3

    # 模拟重新生成：只删除 AI 生成的未完成任务
    conn = get_connection()
    conn.execute(
        "DELETE FROM goal_tasks WHERE goal_id = ? AND status = 'pending' AND source = 'ai_generated'",
        (goal.goal_id,)
    )
    conn.commit()
    conn.close()

    # 验证手动任务保留
    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 2, f"应保留2个手动任务: {len(tasks)}"
    titles = [t["title"] for t in tasks]
    assert "手动任务1" in titles
    assert "手动任务2" in titles
    assert "AI任务1" not in titles

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] manual tasks preserved on regenerate")


def test_source_field_in_get_tasks():
    """测试 get_tasks 返回 source 字段"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_字段查询", "测试字段查询")

    gm.add_task(goal.goal_id, "任务A", "", 1, source="manual")
    gm.add_task(goal.goal_id, "任务B", "", 1, source="ai_generated")

    tasks = gm.get_tasks(goal.goal_id)
    assert len(tasks) == 2

    sources = {t["title"]: t["source"] for t in tasks}
    assert sources["任务A"] == "manual"
    assert sources["任务B"] == "ai_generated"

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] source field in get_tasks")


def test_source_field_in_stats():
    """测试统计中 source 字段不影响统计"""
    gm = GoalManager()
    goal = gm.create_goal("来源测试_统计", "测试统计兼容")

    gm.add_task(goal.goal_id, "手动任务", "", 1, source="manual")
    gm.add_task(goal.goal_id, "AI任务", "", 1, source="ai_generated")
    gm.add_task(goal.goal_id, "AI任务2", "", 1, source="ai_generated")

    # 完成1个手动任务
    tasks = gm.get_tasks(goal.goal_id)
    gm.update_task_status(tasks[0]["id"], "done")

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["total_tasks"] == 3
    assert stats["completed_tasks"] == 1
    assert stats["pending_tasks"] == 2

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] source field in stats")


def test_old_tasks_compatible():
    """测试旧数据（source 为 NULL）兼容性"""
    # 直接插入一条 source 为 NULL 的旧数据
    conn = get_connection()
    goal_id = "goal_test_old_data"
    conn.execute("INSERT OR IGNORE INTO goals (goal_id, title) VALUES (?, ?)", (goal_id, "旧数据测试"))
    conn.execute(
        "INSERT INTO goal_tasks (goal_id, title, source) VALUES (?, ?, NULL)",
        (goal_id, "旧任务")
    )
    conn.commit()
    conn.close()

    # 查询应能正常返回
    gm = GoalManager()
    tasks = gm.get_tasks(goal_id)
    old_task = [t for t in tasks if t["title"] == "旧任务"]
    assert len(old_task) == 1
    # source 为 NULL，不应被删除
    assert old_task[0]["source"] is None

    # 模拟重新生成：只删除 source='ai_generated' 的任务
    conn2 = get_connection()
    conn2.execute(
        "DELETE FROM goal_tasks WHERE goal_id = ? AND status = 'pending' AND source = 'ai_generated'",
        (goal_id,)
    )
    conn2.commit()
    conn2.close()

    # 旧任务应保留
    tasks = gm.get_tasks(goal_id)
    old_task = [t for t in tasks if t["title"] == "旧任务"]
    assert len(old_task) == 1, "旧数据（source=NULL）应保留"

    # 清理
    conn3 = get_connection()
    conn3.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (goal_id,))
    conn3.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
    conn3.commit()
    conn3.close()

    print("[OK] old tasks compatible")


if __name__ == "__main__":
    print("=== goal source test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_add_task_source_default()
    test_add_task_source_manual()
    test_add_task_source_ai()
    test_manual_tasks_preserved_on_regenerate()
    test_source_field_in_get_tasks()
    test_source_field_in_stats()
    test_old_tasks_compatible()

    print("\n=== ALL TESTS PASSED ===")
