# -*- coding: utf-8 -*-
"""
mastery_score 的「未评」语义测试（P7 批次 12-①）

背景：这个分此前有**五种"没填算什么"**的写法（列默认 0、旧签名默认 0、
调用方两处各一个 3、`.get(..., 0)`），同一个"没测到"能变出两个答案（0 或 3）。
本文件把口径钉死成一句话：**缺失 ＝ NULL ＝「未评」，未评不进任何决策。**

四条断言（前三条对应验收要求，第四条是反例护栏）：
  ① 缺失 ⇒ NULL：模型没给 mastery_score 时，库里那行是 NULL（不是 3、不是 0），
     且**不新建知识节点**（knowledge_nodes 那行记的就是掌握度）；
  ② 未评不进决策：同一目标下 4 分与 NULL 各一条 ⇒ average_mastery 是 4.0 而不是
     2.0（AVG 忽略 NULL），且 NULL 那条**不出现**在 get_weak_points() 里
     （SQL `mastery_score <= 2` 对 NULL 天然不匹配）；
  ③ 渲染不炸：取数结果含 NULL 行时，context_manager 的两条注入路径与档案页展示
     都不抛异常，且输出里**不含 "None"、不含 "0/5"**；
  ④ 反例护栏：显式传 3 仍写 3；显式传 0 按「未评」处理（库里不留歧义值 0）。

另有一组纯函数断言覆盖三个渲染器对 None / 0 / 越界值 / 字符串的行为——表格里
「薄弱点」那三行（context_manager._build_weak_points、profile_controller 的薄弱
知识点头、study_plan_generator 的提示词段）都取自 get_weak_points()，而它的 SQL
过滤 `mastery_score <= 2` 对 NULL 天然不匹配 ⇒ **那三行拿不到 NULL**；它们的
"未评"分支靠这组纯函数断言覆盖（详见报告 ④）。

数据库隔离走 tests/_test_db_helpers.setup_test_db()，不碰生产库。
"""
import json
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离：须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db

setup_test_db()

from memory.database import init_db, get_connection  # noqa: E402
from memory.learning_recorder import (  # noqa: E402
    LearningRecorder,
    UNRATED_LABEL,
    normalize_mastery_score,
    format_mastery_stars,
    format_mastery_ratio,
    format_mastery_plain,
)
from core.event_handlers import record_learning_from_text  # noqa: E402
from core.goal_manager import GoalManager  # noqa: E402
from core.context_manager import ContextManager  # noqa: E402

_SUBJECT_AGENT = "英语"
_SUBJECT = "英语"


def setup():
    """初始化临时库"""
    init_db()


def _row(topic):
    """按主题取那条学习记录的 mastery_score 原始值（None 表示库里的 NULL）"""
    conn = get_connection()
    try:
        r = conn.execute(
            "SELECT mastery_score FROM learning_records WHERE topic = ?", (topic,)
        ).fetchone()
    finally:
        conn.close()
    assert r is not None, f"没找到主题为 {topic} 的记录"
    return r["mastery_score"]


def _kg_node_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
    finally:
        conn.close()


def _clear():
    """清空本文件关心的两张表，保证各用例互相独立"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM learning_records WHERE topic LIKE '掌握度测试%'")
        conn.commit()
    finally:
        conn.close()


# ---------- 纯函数：三个渲染器 + 归一（覆盖"薄弱点"那三行的未评分支） ----------

def test_normalize_and_formatters():
    """归一与渲染：None / 0 / 越界 / 字符串 / 布尔 的取值口径"""
    # 合法值原样保留（含字符串形式、含前后空白）
    assert normalize_mastery_score(1) == 1
    assert normalize_mastery_score(5) == 5
    assert normalize_mastery_score("4") == 4
    assert normalize_mastery_score(" 2 ") == 2

    # 缺什么算什么：None / 0 / 越界 / 非数字 / bool 一律「未评」
    for raw in (None, 0, "0", -1, 6, 99, "", "abc", True, False, [], {}):
        assert normalize_mastery_score(raw) is None, f"{raw!r} 应判为未评"

    # 渲染：未评一律是 UNRATED_LABEL，绝不产出 None / 0/5 / 空星星
    for fn in (format_mastery_stars, format_mastery_ratio, format_mastery_plain):
        for raw in (None, 0):
            assert fn(raw) == UNRATED_LABEL, f"{fn.__name__}({raw!r}) 应为「{UNRATED_LABEL}」"

    assert format_mastery_stars(3) == "⭐⭐⭐"
    assert format_mastery_ratio(3) == "3/5"
    assert format_mastery_plain(3) == "3"

    print("[OK] normalize + 三个渲染器：未评一律「未评」，合法值保持原样")


# ---------- ① 缺失 ⇒ NULL ----------

def test_missing_mastery_writes_null_and_skips_kg():
    """模型没给 mastery_score ⇒ 库里是 NULL（不是 3 也不是 0），且不新建知识节点"""
    _clear()
    kg_before = _kg_node_count()
    topic = "掌握度测试_缺失"
    message = json.dumps(
        {"topic": topic, "summary": "讲了虚拟语气，未自评"},
        ensure_ascii=False)

    assert record_learning_from_text(message, _SUBJECT_AGENT, _SUBJECT) is True

    assert _row(topic) is None, \
        f"缺失时必须落 NULL，实际 {_row(topic)!r}（3 或 0 都是编出来的答案）"
    assert _kg_node_count() == kg_before, \
        "掌握度缺失时不应新建知识节点（knowledge_nodes 那行记的就是掌握度）"
    print("[OK] 缺失 ⇒ NULL，且跳过知识图谱写入")


# ---------- ② 未评不进决策 ----------

def test_unrated_excluded_from_average_and_weak_points():
    """4 分与未评各一条 ⇒ 平均分是 4.0（不是 2.0）；未评那条不算薄弱点"""
    _clear()
    gm = GoalManager()
    goal = gm.create_goal("掌握度测试目标", "")
    try:
        LearningRecorder.add_learning_record(
            _SUBJECT, "掌握度测试_已评", "讲了定语从句", mastery_score=4,
            goal_id=goal.goal_id)
        LearningRecorder.add_learning_record(
            _SUBJECT, "掌握度测试_未评", "讲了虚拟语气", mastery_score=None,
            goal_id=goal.goal_id)

        stats = gm.get_learning_stats(goal.goal_id)
        assert stats["total_records"] == 2, stats
        assert stats["average_mastery"] == 4.0, \
            f"未评不得进平均分（AVG 应忽略 NULL），实际 {stats['average_mastery']}"

        weak_topics = [w["topic"] for w in LearningRecorder.get_weak_points(_SUBJECT, limit=50)]
        assert "掌握度测试_未评" not in weak_topics, \
            f"未评不得算薄弱点，实际薄弱点：{weak_topics}"
        print(f"[OK] 未评不进平均分（平均 {stats['average_mastery']}）也不算薄弱点")
    finally:
        gm.update_goal(goal.goal_id, status="archived")


# ---------- ③ 渲染不炸 ----------

def test_rendering_survives_null():
    """取数结果含 NULL 行时，两条注入路径与档案页展示都不炸、不出现 None 或 0/5"""
    _clear()
    LearningRecorder.add_learning_record(
        _SUBJECT, "掌握度测试_渲染未评", "讲了虚拟语气", mastery_score=None)
    LearningRecorder.add_learning_record(
        _SUBJECT, "掌握度测试_渲染已评", "讲了定语从句", mastery_score=5)

    cm = ContextManager()

    recent = cm._build_recent_learning(_SUBJECT_AGENT, limit=5)
    assert "掌握度测试_渲染未评" in recent, recent
    assert UNRATED_LABEL in recent, f"未评应渲染成「{UNRATED_LABEL}」：{recent}"

    retrieved = cm._build_retrieved_learning(
        LearningRecorder.get_recent_records(_SUBJECT, limit=5))
    assert UNRATED_LABEL in retrieved, retrieved

    # 薄弱点注入路径（取自 get_weak_points ⇒ SQL 已滤掉 NULL，这里另造一条低分行，
    # 证明该路径本身仍能正常渲染数字）
    LearningRecorder.add_learning_record(
        _SUBJECT, "掌握度测试_渲染薄弱", "讲了时态", mastery_score=2)
    weak_section = cm._build_weak_points(_SUBJECT_AGENT, limit=5)
    assert "2/5" in weak_section, weak_section

    # 档案与记忆面板（此前 `"⭐" * r["mastery_score"]` 会在这里 TypeError）
    from ui.controllers.profile_controller import get_memory_display
    memory_text = get_memory_display()
    assert UNRATED_LABEL in memory_text, memory_text

    for name, text in (("recent", recent), ("retrieved", retrieved),
                       ("weak", weak_section), ("memory", memory_text)):
        assert "None" not in text, f"{name} 里不得出现 None：{text}"
        assert "0/5" not in text, f"{name} 里不得出现 0/5：{text}"
        assert "未评/5" not in text, f"{name} 里不得出现「未评/5」：{text}"

    print("[OK] 渲染不炸：注入与档案页均正常，未评显示为「未评」")


# ---------- ④ 反例护栏 ----------

def test_explicit_values_still_honoured():
    """显式传 3 仍写 3；显式传 0 按「未评」处理（库里不留歧义值 0）"""
    _clear()
    LearningRecorder.add_learning_record(
        _SUBJECT, "掌握度测试_显式3", "摘要", mastery_score=3)
    assert _row("掌握度测试_显式3") == 3, "显式传 3 必须原样写 3"

    LearningRecorder.add_learning_record(
        _SUBJECT, "掌握度测试_显式0", "摘要", mastery_score=0)
    assert _row("掌握度测试_显式0") is None, \
        "显式传 0 应按未评存 NULL，库里不留歧义值 0"

    # 完全不传（用默认值）同样是未评
    LearningRecorder.add_learning_record(_SUBJECT, "掌握度测试_不传", "摘要")
    assert _row("掌握度测试_不传") is None, "不传 mastery_score 应为 NULL"

    print("[OK] 显式 3 写 3；显式 0 / 不传 ⇒ NULL")


if __name__ == "__main__":
    print("=== mastery score semantics test ===\n")
    setup()
    test_normalize_and_formatters()
    test_missing_mastery_writes_null_and_skips_kg()
    test_unrated_excluded_from_average_and_weak_points()
    test_rendering_survives_null()
    test_explicit_values_still_honoured()
    print("\n=== ALL MASTERY SCORE TESTS PASSED ===")
