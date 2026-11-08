"""
目标统计功能测试
测试 get_goal_stats 方法
"""
import os
import sys
import shutil

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager


def test_goal_stats_empty():
    """测试无任务时的统计"""
    gm = GoalManager()
    goal = gm.create_goal("统计测试目标_空", "测试无任务时的统计")
    stats = gm.get_goal_stats(goal.goal_id)

    assert stats["total_tasks"] == 0
    assert stats["completed_tasks"] == 0
    assert stats["pending_tasks"] == 0
    assert stats["completion_rate"] == 0.0
    assert stats["notes_count"] == 0
    assert stats["files_count"] == 0

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal stats empty")


def test_goal_stats_with_tasks():
    """测试有任务时的统计"""
    gm = GoalManager()
    goal = gm.create_goal("统计测试目标_任务", "测试任务统计")

    # 添加3个任务
    gm.add_task(goal.goal_id, "任务1", "描述1", 1)
    gm.add_task(goal.goal_id, "任务2", "描述2", 2)
    gm.add_task(goal.goal_id, "任务3", "描述3", 3)

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["total_tasks"] == 3
    assert stats["completed_tasks"] == 0
    assert stats["pending_tasks"] == 3
    assert stats["completion_rate"] == 0.0

    # 完成1个任务
    tasks = gm.get_tasks(goal.goal_id)
    gm.update_task_status(tasks[0]["id"], "done")

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["total_tasks"] == 3
    assert stats["completed_tasks"] == 1
    assert stats["pending_tasks"] == 2
    assert abs(stats["completion_rate"] - 33.3) < 0.1  # 1/3 = 33.3%

    # 完成第2个任务
    gm.update_task_status(tasks[1]["id"], "done")

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["completed_tasks"] == 2
    assert abs(stats["completion_rate"] - 66.7) < 0.1  # 2/3 = 66.7%

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal stats with tasks")


def test_goal_stats_notes():
    """测试笔记数量统计"""
    gm = GoalManager()
    goal = gm.create_goal("统计测试目标_笔记", "测试笔记统计")

    # 添加笔记
    gm.add_note(goal.goal_id, "笔记1")
    gm.add_note(goal.goal_id, "笔记2")
    gm.add_note(goal.goal_id, "笔记3")

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["notes_count"] == 3

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal stats notes")


def test_goal_stats_files():
    """测试文件数量统计"""
    gm = GoalManager()
    goal = gm.create_goal("统计测试目标_文件", "测试文件统计")

    # 创建临时文件目录
    files_dir = f"data/goals/{goal.goal_id}/files"
    os.makedirs(files_dir, exist_ok=True)

    # 创建临时文件
    test_files = []
    for i in range(3):
        filepath = os.path.join(files_dir, f"test_{i}.txt")
        with open(filepath, "w") as f:
            f.write(f"测试内容 {i}")
        test_files.append(filepath)

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["files_count"] == 3

    # 清理临时文件
    for fp in test_files:
        if os.path.exists(fp):
            os.remove(fp)

    # 清理目录
    try:
        shutil.rmtree(f"data/goals/{goal.goal_id}")
    except Exception:
        pass

    # 清理数据库
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal stats files")


def test_goal_stats_nonexistent():
    """测试不存在的目标"""
    gm = GoalManager()
    stats = gm.get_goal_stats("nonexistent_goal_id")
    assert stats == {}

    print("[OK] goal stats nonexistent")


def test_goal_stats_rate_precision():
    """测试完成率精度"""
    gm = GoalManager()
    goal = gm.create_goal("统计测试目标_精度", "测试精度")

    # 添加任务并部分完成
    gm.add_task(goal.goal_id, "任务1", "", 1)
    gm.add_task(goal.goal_id, "任务2", "", 1)
    gm.add_task(goal.goal_id, "任务3", "", 1)
    gm.add_task(goal.goal_id, "任务4", "", 1)
    gm.add_task(goal.goal_id, "任务5", "", 1)

    # 完成2个 = 40%
    tasks = gm.get_tasks(goal.goal_id)
    gm.update_task_status(tasks[0]["id"], "done")
    gm.update_task_status(tasks[1]["id"], "done")

    stats = gm.get_goal_stats(goal.goal_id)
    assert stats["completion_rate"] == 40.0

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] goal stats rate precision")


if __name__ == "__main__":
    print("=== goal stats test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_goal_stats_empty()
    test_goal_stats_with_tasks()
    test_goal_stats_notes()
    test_goal_stats_files()
    test_goal_stats_nonexistent()
    test_goal_stats_rate_precision()

    print("\n=== ALL TESTS PASSED ===")
