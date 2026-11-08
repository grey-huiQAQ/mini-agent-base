"""
复习推荐功能测试
测试 get_review_recommendations 方法
"""
import os
import sys
from datetime import datetime, timedelta

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.knowledge_graph import KnowledgeGraph
from memory.database import get_db


def _cleanup_test_nodes():
    """清理测试节点"""
    conn = get_db()
    conn.execute("DELETE FROM knowledge_nodes WHERE name LIKE '复习测试_%'")
    conn.commit()
    conn.close()


def _set_last_reviewed(node_id, days_ago):
    """设置节点的 last_reviewed 为指定天数之前"""
    dt = datetime.now() - timedelta(days=days_ago)
    conn = get_db()
    conn.execute(
        "UPDATE knowledge_nodes SET last_reviewed = ? WHERE id = ?",
        (dt.strftime("%Y-%m-%d %H:%M:%S"), node_id)
    )
    conn.commit()
    conn.close()


def test_review_recommendations_basic():
    """测试基本排序：掌握度低 + 久未复习 → 优先级高"""
    _cleanup_test_nodes()

    # 创建测试节点：掌握度低，30天未复习
    id1 = KnowledgeGraph.add_node("复习测试_低掌握", "AI", mastery_level=1)
    _set_last_reviewed(id1, 30)

    # 创建测试节点：掌握度高，最近复习
    id2 = KnowledgeGraph.add_node("复习测试_高掌握", "AI", mastery_level=5)
    _set_last_reviewed(id2, 1)

    # 创建测试节点：掌握度中等，7天未复习
    id3 = KnowledgeGraph.add_node("复习测试_中等", "AI", mastery_level=3)
    _set_last_reviewed(id3, 7)

    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=5)
    assert len(recs) == 3, f"应返回3个推荐: {len(recs)}"

    # 验证排序：低掌握30天未复习 应排第一
    assert recs[0]["name"] == "复习测试_低掌握", f"第一个应是低掌握: {recs[0]['name']}"
    assert recs[1]["name"] == "复习测试_中等", f"第二个应是中等: {recs[1]['name']}"
    assert recs[2]["name"] == "复习测试_高掌握", f"第三个应是高掌握: {recs[2]['name']}"

    # 验证优先级字段
    assert recs[0]["priority"] > recs[1]["priority"] > recs[2]["priority"]

    # 验证 days_since_review 字段
    assert recs[0]["days_since_review"] >= 29  # ~30天
    assert recs[2]["days_since_review"] <= 2    # ~1天

    _cleanup_test_nodes()
    print("[OK] basic ranking")


def test_review_recommendations_null_last_reviewed():
    """测试 last_reviewed 为 NULL 的情况"""
    _cleanup_test_nodes()

    # 创建节点但不设置 last_reviewed（默认 NULL）
    id1 = KnowledgeGraph.add_node("复习测试_未复习", "AI", mastery_level=2)

    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=5)
    assert len(recs) >= 1, f"应至少返回1个推荐: {len(recs)}"

    # 未复习的节点 days_since_review 应为 30
    node = [r for r in recs if r["id"] == id1][0]
    assert node["days_since_review"] == 30
    assert node["priority"] == (5 - 2) * 2 + 30  # 6 + 30 = 36

    _cleanup_test_nodes()
    print("[OK] null last_reviewed")


def test_review_recommendations_domain_filter():
    """测试领域过滤"""
    _cleanup_test_nodes()

    id_ai = KnowledgeGraph.add_node("复习测试_AI节点", "AI", mastery_level=1)
    _set_last_reviewed(id_ai, 30)

    id_en = KnowledgeGraph.add_node("复习测试_英语节点", "英语", mastery_level=1)
    _set_last_reviewed(id_en, 30)

    # 只查 AI 领域
    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=5)
    names = [r["name"] for r in recs]
    assert "复习测试_AI节点" in names
    assert "复习测试_英语节点" not in names

    # 查所有领域
    recs = KnowledgeGraph.get_review_recommendations(domain=None, limit=5)
    names = [r["name"] for r in recs]
    assert "复习测试_AI节点" in names
    assert "复习测试_英语节点" in names

    _cleanup_test_nodes()
    print("[OK] domain filter")


def test_review_recommendations_empty():
    """测试空表返回空列表"""
    _cleanup_test_nodes()

    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=5)
    assert recs == [], f"空表应返回空列表: {recs}"

    print("[OK] empty table")


def test_review_recommendations_limit():
    """测试 limit 参数"""
    _cleanup_test_nodes()

    # 创建5个节点
    ids = []
    for i in range(5):
        nid = KnowledgeGraph.add_node(f"复习测试_节点{i}", "AI", mastery_level=i)
        _set_last_reviewed(nid, i * 5)
        ids.append(nid)

    # 限制返回2个
    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=2)
    assert len(recs) == 2, f"limit=2 应返回2个: {len(recs)}"

    # 验证返回的是优先级最高的2个
    assert recs[0]["priority"] >= recs[1]["priority"]

    _cleanup_test_nodes()
    print("[OK] limit")


def test_review_recommendations_priority_formula():
    """测试优先级公式正确性"""
    _cleanup_test_nodes()

    # 创建节点：mastery=0, 60天未复习 → priority = (5-0)*2 + min(60,30) = 10+30 = 40
    id1 = KnowledgeGraph.add_node("复习测试_公式验证", "AI", mastery_level=0)
    _set_last_reviewed(id1, 60)

    recs = KnowledgeGraph.get_review_recommendations(domain="AI", limit=5)
    assert len(recs) >= 1
    node = [r for r in recs if r["id"] == id1][0]
    assert node["priority"] == 40, f"优先级应为40: {node['priority']}"
    assert node["days_since_review"] == 60, f"天数应为60: {node['days_since_review']}"

    _cleanup_test_nodes()
    print("[OK] priority formula")


def test_review_recommendations_exception_safety():
    """测试异常安全性"""
    # 传入非法 domain 不应崩溃
    recs = KnowledgeGraph.get_review_recommendations(domain="不存在的领域", limit=5)
    assert isinstance(recs, list)

    # 传入 None domain
    recs = KnowledgeGraph.get_review_recommendations(domain=None, limit=5)
    assert isinstance(recs, list)

    print("[OK] exception safety")


if __name__ == "__main__":
    print("=== review recommendations test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_review_recommendations_basic()
    test_review_recommendations_null_last_reviewed()
    test_review_recommendations_domain_filter()
    test_review_recommendations_empty()
    test_review_recommendations_limit()
    test_review_recommendations_priority_formula()
    test_review_recommendations_exception_safety()

    print("\n=== ALL TESTS PASSED ===")
