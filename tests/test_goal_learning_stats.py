"""
目标关联学习记录统计测试
验证 get_learning_stats 方法和 goal_id 关联
"""
import os
import sys

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager
from memory.learning_recorder import LearningRecorder


def test_learning_stats_with_records():
    """测试有学习记录时的统计"""
    gm = GoalManager()
    goal = gm.create_goal("学习统计测试_有记录", "测试学习记录统计")

    # 添加学习记录（关联 goal_id）
    LearningRecorder.add_learning_record(
        "AI", "机器学习基础", "介绍了监督学习", mastery_score=4, goal_id=goal.goal_id
    )
    LearningRecorder.add_learning_record(
        "AI", "深度学习入门", "讲解神经网络", mastery_score=2, goal_id=goal.goal_id
    )
    LearningRecorder.add_learning_record(
        "AI", "自然语言处理", "NLP基础", mastery_score=1, goal_id=goal.goal_id
    )

    stats = gm.get_learning_stats(goal.goal_id)
    assert stats["total_records"] == 3
    assert stats["average_mastery"] == round((4 + 2 + 1) / 3, 1)  # 2.3
    assert len(stats["recent_topics"]) == 3
    assert "机器学习基础" in stats["recent_topics"]
    assert len(stats["weak_topics"]) == 2  # 深度学习入门(2) 和 自然语言处理(1)
    assert "深度学习入门" in stats["weak_topics"]
    assert "自然语言处理" in stats["weak_topics"]

    # 清理
    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] learning stats with records")


def test_learning_stats_no_records():
    """测试无学习记录时的统计"""
    gm = GoalManager()
    goal = gm.create_goal("学习统计测试_无记录", "测试空记录统计")

    stats = gm.get_learning_stats(goal.goal_id)
    assert stats["total_records"] == 0
    assert stats["average_mastery"] == 0.0
    assert stats["recent_topics"] == []
    assert stats["weak_topics"] == []

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] learning stats no records")


def test_learning_stats_nonexistent_goal():
    """测试不存在的目标"""
    gm = GoalManager()
    stats = gm.get_learning_stats("nonexistent_goal")
    assert stats == {}

    print("[OK] learning stats nonexistent goal")


def test_learning_stats_goal_id_isolation():
    """测试不同目标的学习记录隔离"""
    gm = GoalManager()
    goal1 = gm.create_goal("学习统计隔离测试1", "")
    goal2 = gm.create_goal("学习统计隔离测试2", "")

    # 给 goal1 添加记录
    LearningRecorder.add_learning_record("AI", "目标1主题", "摘要", mastery_score=3, goal_id=goal1.goal_id)
    # 给 goal2 添加记录
    LearningRecorder.add_learning_record("英语", "目标2主题", "摘要", mastery_score=5, goal_id=goal2.goal_id)

    stats1 = gm.get_learning_stats(goal1.goal_id)
    stats2 = gm.get_learning_stats(goal2.goal_id)

    assert stats1["total_records"] == 1
    assert stats1["recent_topics"] == ["目标1主题"]
    assert stats2["total_records"] == 1
    assert stats2["recent_topics"] == ["目标2主题"]

    gm.update_goal(goal1.goal_id, status="archived")
    gm.update_goal(goal2.goal_id, status="archived")
    print("[OK] learning stats goal_id isolation")


def test_add_learning_record_with_goal_id():
    """测试 add_learning_record 支持 goal_id 参数"""
    gm = GoalManager()
    goal = gm.create_goal("学习记录goal_id测试", "")

    # 添加带 goal_id 的记录
    LearningRecorder.add_learning_record(
        "AI", "测试主题", "测试摘要", mastery_score=3, goal_id=goal.goal_id
    )

    # 验证记录存在且关联正确
    stats = gm.get_learning_stats(goal.goal_id)
    assert stats["total_records"] == 1
    assert stats["recent_topics"] == ["测试主题"]

    gm.update_goal(goal.goal_id, status="archived")
    print("[OK] add learning record with goal_id")


def test_add_learning_record_backward_compatible():
    """测试 add_learning_record 向后兼容（不传 goal_id）"""
    # 不传 goal_id，应正常工作
    LearningRecorder.add_learning_record(
        "通用", "无目标主题", "摘要", mastery_score=3
    )

    # 验证记录存在（goal_id 为 None）
    from memory.database import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM learning_records WHERE topic = '无目标主题'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["goal_id"] is None

    print("[OK] add learning record backward compatible")


if __name__ == "__main__":
    print("=== goal learning stats test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_learning_stats_with_records()
    test_learning_stats_no_records()
    test_learning_stats_nonexistent_goal()
    test_learning_stats_goal_id_isolation()
    test_add_learning_record_with_goal_id()
    test_add_learning_record_backward_compatible()

    print("\n=== ALL TESTS PASSED ===")
