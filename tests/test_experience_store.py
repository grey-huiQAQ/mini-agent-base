# -*- coding: utf-8 -*-
"""
ExperienceStore 经验沉淀存储测试（离线，不依赖 LLM）
覆盖：
- 新增与全量查询（字段完整性、来源会话、排序）
- 相关性检索（多主题区分、得分排序、limit 生效）
- 空查询 / 无命中 / 空白查询容错
- 删除（存在/不存在）、计数
- 检索词提取单元测试（英文词、中文 bigram、去重）
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_db
from memory.experience_store import ExperienceStore, _extract_terms

# 本测试创建的经验 ID，cleanup 时精确清理
_created_ids = []

# 本文件全部 fixture 的 scenario 名单：setup 时按名单清除历史残留
# （中途失败的运行来不及执行末尾 cleanup，重跑时在此自愈，避免污染检索类断言）
_FIXTURE_SCENARIOS = (
    "用户初次学习新概念", "背诵英语单词容易遗忘",
    "Python 调试报错看不懂", "做饭总是太咸", "待删除场景",
)


def setup():
    """初始化数据库，并清除本文件 fixture 的历史残留"""
    init_db()
    conn = get_db()
    placeholders = ", ".join("?" * len(_FIXTURE_SCENARIOS))
    conn.execute(f"DELETE FROM experiences WHERE scenario IN ({placeholders})",
                 _FIXTURE_SCENARIOS)
    conn.commit()
    conn.close()


def _make_exp(**kwargs) -> int:
    """新增经验并记录 ID 供清理"""
    new_id = ExperienceStore.add_experience(
        kwargs.get("scenario", "默认场景"),
        kwargs.get("strategy", "默认策略"),
        effect=kwargs.get("effect", ""),
        conditions=kwargs.get("conditions", ""),
        source_session_id=kwargs.get("source_session_id"),
    )
    _created_ids.append(new_id)
    return new_id


def test_add_and_get_all():
    """测试新增与全量查询：字段完整、来源会话持久化、按时间倒序"""
    before = ExperienceStore.count_experiences()
    new_id = _make_exp(
        scenario="用户初次学习新概念",
        strategy="先用生活化例子再给定义",
        effect="用户理解速度明显加快",
        conditions="适合抽象概念入门阶段",
        source_session_id="sess_test_001",
    )
    assert isinstance(new_id, int) and new_id > 0

    all_exp = ExperienceStore.get_all_experiences(limit=50)
    assert len(all_exp) >= before + 1
    assert ExperienceStore.count_experiences() == before + 1, "计数应恰好加一"

    target = next(e for e in all_exp if e["id"] == new_id)
    assert target["scenario"] == "用户初次学习新概念"
    assert target["strategy"] == "先用生活化例子再给定义"
    assert target["effect"] == "用户理解速度明显加快"
    assert target["conditions"] == "适合抽象概念入门阶段"
    assert target["source_session_id"] == "sess_test_001"
    assert target["created_at"], "created_at 应由数据库默认生成"

    # 倒序：最新插入的排在最前
    assert all_exp[0]["id"] == new_id
    print("[OK] add and get all experiences")


def test_search_relevance_ordering():
    """测试相关性检索：不同主题经验能被对应查询命中且排序正确"""
    id_word = _make_exp(
        scenario="背诵英语单词容易遗忘",
        strategy="间隔重复：每天复习前一天的单词再加新词",
        conditions="需要每天固定时间执行",
    )
    id_code = _make_exp(
        scenario="Python 调试报错看不懂",
        strategy="先读错误类型和行号，再复现最小示例",
    )
    id_food = _make_exp(
        scenario="做饭总是太咸",
        strategy="先少放盐出锅前再调味",
    )

    # 查询英语单词 → 单词记忆经验应排第一，做饭经验不应出现
    hits = ExperienceStore.search_experiences("怎么背英语单词高效？", limit=3)
    assert hits, "应至少命中一条"
    assert hits[0]["id"] == id_word, f"单词相关经验应排第一，实际: {hits[0]['scenario']}"
    assert all(h["id"] != id_food for h in hits), "做饭经验不应被英语查询命中"

    # 查询 Python 报错 → 编程经验应命中
    hits2 = ExperienceStore.search_experiences("python debug 报错", limit=3)
    assert any(h["id"] == id_code for h in hits2), "编程经验应被 python 查询命中"

    # limit 生效
    hits3 = ExperienceStore.search_experiences("英语单词", limit=1)
    assert len(hits3) <= 1
    print("[OK] search relevance and ordering")


def test_search_edge_cases():
    """测试检索边界：空查询、纯空白、无命中的返回值"""
    assert ExperienceStore.search_experiences("") == []
    assert ExperienceStore.search_experiences("   ") == []
    assert ExperienceStore.search_experiences("完全无关的查询ZZZQQQ") == []
    print("[OK] search edge cases")


def test_delete_experience():
    """测试删除：存在的记录删除成功，不存在的返回 False"""
    new_id = _make_exp(scenario="待删除场景", strategy="待删除策略")
    assert ExperienceStore.delete_experience(new_id) is True
    assert ExperienceStore.delete_experience(new_id) is False, "重复删除应返回 False"
    assert ExperienceStore.delete_experience(99999999) is False
    remaining = [e["id"] for e in ExperienceStore.get_all_experiences(limit=500)]
    assert new_id not in remaining
    print("[OK] delete experience")


def test_extract_terms():
    """检索单元测试：英文词提取、中文 bigram、去重、空输入"""
    # 英文 + 数字
    terms = _extract_terms("learn Python3 fast")
    assert "learn" in terms and "python3" in terms and "fast" in terms
    assert "a" not in terms, "单字符英文词应被过滤"

    # 中文 bigram："背单词" → 背单 / 单词
    terms2 = _extract_terms("如何背单词")
    assert "背单" in terms2 and "单词" in terms2

    # 中英混合 + 大小写归一
    terms3 = _extract_terms("学 RUST 和 Rust")
    assert "rust" in terms3, "英文应统一小写并去重"

    # 空输入
    assert _extract_terms("") == []
    assert _extract_terms(None) == []
    print("[OK] extract terms")


def test_cleanup():
    """清理本测试产生的全部经验数据"""
    for exp_id in _created_ids:
        ExperienceStore.delete_experience(exp_id)
    _created_ids.clear()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== experience store test ===\n")
    setup()
    test_add_and_get_all()
    test_search_relevance_ordering()
    test_search_edge_cases()
    test_delete_experience()
    test_extract_terms()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
