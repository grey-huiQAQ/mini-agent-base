# -*- coding: utf-8 -*-
"""
IntentAgent 意图识别模块测试（离线：通过替换 model_client.chat_sync 模拟 LLM）
覆盖：
- 对话历史压平工具 build_text_from_history（dict 与旧式元组格式）
- 意图解析容错：无意图、多候选去重、非法 JSON、LLM 异常降级
- 校验器过滤：空标题 / 非法 suggestion_type / 优先级与周期钳制
- apply_intent 写库：创建新目标 / 关联既有目标 / adjust_plan 与 none 不写库
- GoalManager 向后兼容：旧式 create_goal 调用、find_goals_by_title 模糊查找
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager
from memory.database import init_db, get_connection
from agents import intent_agent as ia_module
from agents.intent_agent import IntentAgent

# 本测试文件创建过的目标 ID，cleanup 时精确清理，不触碰其他数据
_created_goal_ids = []


def setup():
    """初始化数据库"""
    init_db()


def _make_goal(gm: GoalManager, title: str, desc: str = "", **kwargs):
    """创建目标并记录 ID，供 cleanup 精确删除"""
    goal = gm.create_goal(title, desc, **kwargs)
    _created_goal_ids.append(goal.goal_id)
    return goal


def _cleanup():
    """清理本测试产生的目标数据（含任务）"""
    conn = get_connection()
    for gid in _created_goal_ids:
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()
    _created_goal_ids.clear()


def _patch_chat_sync(fake):
    """返回 (还原函数) 用于 with 结构中临时替换 model_client.chat_sync"""
    original = ia_module.model_client.chat_sync
    ia_module.model_client.chat_sync = fake

    def restore():
        ia_module.model_client.chat_sync = original
    return restore


def test_build_text_from_history():
    """测试对话历史压平：只取用户消息、条数限制、旧式元组格式兼容、空输入"""
    history = [
        {"role": "user", "content": "我想在三个月内学会基础日语"},
        {"role": "assistant", "content": "好的，我来帮你规划。"},
        {"role": "user", "content": "顺便准备 N5 考试"},
    ]
    text = ia_module.build_text_from_history(history)
    assert "基础日语" in text and "N5" in text, "应包含用户发言"
    assert "帮你规划" not in text, "不应包含助手消息"

    # 条数限制：limit=1 只保留最近一条用户消息
    text2 = ia_module.build_text_from_history(history, limit=1)
    assert "N5" in text2 and "三个月" not in text2

    # 旧式元组格式（Gradio 旧版 Chatbot）
    text3 = ia_module.build_text_from_history([("元组用户发言", "元组助手回复")])
    assert "元组用户发言" in text3 and "元组助手回复" not in text3

    # 空输入容错
    assert ia_module.build_text_from_history([]) == ""
    assert ia_module.build_text_from_history(None) == ""
    print("[OK] build_text_from_history")


def test_analyze_create_new_with_mock_llm():
    """mock LLM 全链路：识别出 create_new 意图并生成 Markdown 展示"""
    agent = IntentAgent(GoalManager())
    fake_response = '''```json
{"has_intent": true, "intents":[{"title":"掌握基础日语","description":"达到N5水平","priority":1,"estimated_weeks":12,"suggestion_type":"create_new","target_title":null,"reason":"用户明确表达想学日语"}]}
```'''
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.analyze_intent("- 我想在三个月内学会基础日语")
        assert result["ok"], result["message"]
        assert len(result["intents"]) == 1
        intent = result["intents"][0]
        assert intent["title"] == "掌握基础日语"
        assert intent["description"] == "达到N5水平"
        assert intent["priority"] == 1 and intent["estimated_weeks"] == 12
        assert intent["suggestion_type"] == "create_new"
        assert "掌握基础日语" in result["display"], "展示文本应包含目标标题"
        assert "🆕 创建新目标" in result["display"]
    finally:
        restore()
    print("[OK] analyze create_new with mock llm")


def test_analyze_no_intent():
    """mock LLM 返回无意图：ok=True 且 intents 为空，给出友好提示"""
    agent = IntentAgent(GoalManager())
    restore = _patch_chat_sync(
        lambda messages, **kw: '{"has_intent": false, "intents": []}'
    )
    try:
        result = agent.analyze_intent("- 今天天气怎么样？")
        assert result["ok"], result["message"]
        assert result["intents"] == []
        assert "未识别到明确" in result["message"]
    finally:
        restore()
    print("[OK] analyze no intent")


def test_analyze_multiple_candidates_and_dedupe():
    """多候选场景：合法意图保留、重复标题去重、非法类型过滤"""
    agent = IntentAgent(GoalManager())
    _make_goal(GoalManager(), "英语口语强化训练")

    def fake(messages, **kw):
        return ('{"has_intent": true, "intents":['
                '{"title":"每天背50个单词","description":"","priority":2,"estimated_weeks":4,"suggestion_type":"create_new"},'
                '{"title":"每天背50个单词","description":"重复项","priority":3,"estimated_weeks":5,"suggestion_type":"associate_existing","target_title":"英语口语"},'
                '{"title":"口语流利对话","description":"","priority":2,"estimated_weeks":8,"suggestion_type":"associate_existing","target_title":"英语口语强化训练"},'
                '{"title":"非法类型项","suggestion_type":"delete_all"}'
                ']}')

    restore = _patch_chat_sync(fake)
    try:
        result = agent.analyze_intent("- 我想坚持背单词，也想练口语")
        assert result["ok"], result["message"]
        # 重复标题被去重，非法 suggestion_type 被过滤 → 剩 2 条
        assert len(result["intents"]) == 2
        types = [it["suggestion_type"] for it in result["intents"]]
        assert types[0] == "create_new" and types[1] == "associate_existing"
        assert result["intents"][1]["target_title"] == "英语口语强化训练"
    finally:
        restore()
        _cleanup()
    print("[OK] analyze multiple candidates and dedupe")


def test_analyze_empty_and_error_degrade():
    """输入为空、模型异常、非法 JSON 三种情况的友好降级"""
    agent = IntentAgent(GoalManager())

    # 1. 空输入
    result = agent.analyze_intent("")
    assert not result["ok"] and "没有可分析的内容" in result["message"]

    # 2. LLM 抛异常 → 不崩溃，返回错误提示
    def boom(messages, **kw):
        raise RuntimeError("网络中断模拟")
    restore = _patch_chat_sync(boom)
    try:
        result = agent.analyze_intent("- 我想学 Rust")
        assert not result["ok"] and "模型调用失败" in result["message"]
    finally:
        restore()

    # 3. 非法 JSON → 解析失败提示
    restore = _patch_chat_sync(lambda messages, **kw: "这不是 JSON 输出")
    try:
        result = agent.analyze_intent("- 我想学 Rust")
        assert not result["ok"] and "无法从模型回复中解析" in result["message"]
    finally:
        restore()
    print("[OK] analyze empty input / llm error / bad json degrade")


def test_validate_intents_filters_invalid():
    """校验器单元测试：脏数据过滤与数值钳制"""
    agent = IntentAgent(GoalManager())
    dirty = {
        "intents": [
            "junk_string",                                                    # 非法元素 → 过滤
            {"title": "", "suggestion_type": "create_new"},                   # 无标题 → 过滤
            {"title": "缺类型项"},                                             # 缺 suggestion_type → 过滤
            {"title": "周期越界项", "suggestion_type": "create_new",
             "priority": "9", "estimated_weeks": 999},                        # 数值钳制
            {"title": "周期越界项", "suggestion_type": "create_new"},          # 与上行同标题 → 去重
            {"title": None, "suggestion_type": "none"},                       # none 允许无标题
        ]
    }
    cleaned = agent._validate_intents(dirty)
    assert len(cleaned) == 2, f"应保留 2 条，实际 {len(cleaned)}"
    first = cleaned[0]
    assert first["priority"] == 3, "优先级 9 应钳制到 3"
    assert first["estimated_weeks"] == 52, "周期 999 应钳制到 52"
    assert cleaned[1]["title"] == "" and cleaned[1]["suggestion_type"] == "none"

    # 完全非法结构
    assert agent._validate_intents(None) == []
    assert agent._validate_intents({"intents": "not a list"}) == []
    print("[OK] validate intents filters invalid")


def test_apply_create_new_writes_db():
    """应用 create_new：目标落库且优先级持久化"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    intent = {
        "title": "意图测试-创建目标流程",
        "description": "由 IntentAgent 创建",
        "priority": 1,
        "estimated_weeks": 6,
        "suggestion_type": "create_new",
    }
    result = agent.apply_intent(intent)
    assert result["ok"], result["message"]
    assert result["goal_id"] and result["goal_id"].startswith("goal_")
    loaded = gm.get_goal(result["goal_id"])
    assert loaded is not None and loaded.title == intent["title"]
    assert loaded.priority == 1, "优先级应持久化为 1"
    assert loaded.deadline == "", "deadline 未设置时应为空字符串"
    _created_goal_ids.append(result["goal_id"])
    print("[OK] apply create_new writes db")


def test_apply_create_new_duplicate_guard():
    """幂等保护：已存在同名进行中目标时不重复创建"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    existing = _make_goal(gm, "意图测试-重名保护目标")
    before = gm.list_goals(status="active")
    result = agent.apply_intent({
        "title": "意图测试-重名保护目标",
        "suggestion_type": "create_new",
    })
    assert not result["ok"], "同名目标存在时不应创建"
    assert "已存在同名进行中目标" in result["message"]
    after = gm.list_goals(status="active")
    assert len(after) == len(before), "目标总数不应变化"
    print("[OK] apply create_new duplicate guard")


def test_apply_associate_existing_adds_task():
    """应用 associate_existing：按标题模糊匹配目标并添加 AI 任务"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    goal = _make_goal(gm, "英语口语强化训练计划")

    result = agent.apply_intent({
        "title": "每天开口练习20分钟",
        "description": "跟读影子练习",
        "priority": 2,
        "suggestion_type": "associate_existing",
        "target_title": "英语口语",
        "reason": "匹配现有口语目标",
    })
    assert result["ok"], result["message"]
    assert result["goal_id"] == goal.goal_id

    tasks = gm.get_tasks(goal.goal_id)
    matched = [t for t in tasks if t["title"] == "每天开口练习20分钟"]
    assert len(matched) == 1, "任务应写入匹配到的目标"
    assert matched[0]["source"] == "ai_generated"
    assert matched[0]["priority"] == 2
    assert matched[0].get("milestone_id") is None
    assert "来源：意图识别" in (matched[0]["description"] or "")
    print("[OK] apply associate_existing adds task")


def test_apply_associate_existing_not_found():
    """关联失败路径：匹配不到目标时不写库，提示手动关联"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    result = agent.apply_intent({
        "title": "随便一个任务",
        "suggestion_type": "associate_existing",
        "target_title": "绝对不存在的目标关键字XYZ",
    })
    assert not result["ok"]
    assert "手动关联" in result["message"]
    print("[OK] apply associate_existing target not found")


def test_apply_adjust_plan_and_none_do_not_write():
    """adjust_plan 与 none 均不写库，仅返回引导性提示"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    goals_before = len(gm.list_goals(status="active"))

    r1 = agent.apply_intent({"title": "调整我的日语计划", "suggestion_type": "adjust_plan"})
    assert not r1["ok"] and "Planner" in r1["message"], "应引导使用 PlannerAgent"

    r2 = agent.apply_intent({"title": "", "suggestion_type": "none"})
    assert not r2["ok"] and "未识别到明确" in r2["message"]

    r3 = agent.apply_intent({"title": "x", "suggestion_type": "weird_action"})
    assert not r3["ok"] and "未知" in r3["message"]

    assert len(gm.list_goals(status="active")) == goals_before, "不应产生新目标"
    print("[OK] apply adjust_plan / none do not write db")


def test_apply_intents_batch_aggregates():
    """批量应用：逐条执行并聚合结果（一条成功一条跳过）"""
    gm = GoalManager()
    agent = IntentAgent(gm)
    intents = [
        {"title": "意图测试-批量创建目标", "description": "", "priority": 3,
         "estimated_weeks": 0, "suggestion_type": "create_new"},
        {"title": "", "suggestion_type": "none"},  # 会被忽略
    ]
    result = agent.apply_intents(intents)
    assert result["ok"] and result["applied"] == 1, f"应成功 1 条：{result}"
    assert len(result["details"]) == 2, "每条建议都应有反馈明细"

    # 空列表与无效输入
    assert agent.apply_intents([])["applied"] == 0
    bad = agent.apply_intent("not a dict")
    assert not bad["ok"] and "无效的建议数据" in bad["message"]
    print("[OK] apply_intents batch aggregates")


def test_goal_manager_backward_compat():
    """GoalManager 兼容回归：旧式两参调用行为不变，新参数可选生效"""
    gm = GoalManager()
    # 旧调用方式：默认优先级 2、截止日期为空
    old_style = _make_goal(gm, "意图测试-旧式调用目标", "描述")
    loaded = gm.get_goal(old_style.goal_id)
    assert loaded.priority == 2 and loaded.deadline == ""

    # 新调用方式：显式传 priority/deadline
    new_style = _make_goal(gm, "意图测试-新式调用目标", priority=1, deadline="2026-12-31")
    loaded2 = gm.get_goal(new_style.goal_id)
    assert loaded2.priority == 1 and loaded2.deadline == "2026-12-31"

    # 优先级钳制：传入越界值 99 应落库为 3
    clamped = _make_goal(gm, "意图测试-钳制目标", priority=99)
    assert gm.get_goal(clamped.goal_id).priority == 3
    print("[OK] goal manager backward compat")


def test_find_goals_by_title():
    """find_goals_by_title：模糊匹配、状态过滤、空关键字容错"""
    gm = GoalManager()
    active = _make_goal(gm, "Python爬虫进阶实践")
    archived = _make_goal(gm, "Python数据分析入门")
    gm.archive_goal(archived.goal_id)

    hits = gm.find_goals_by_title("Python爬虫", status="active")
    assert [g.goal_id for g in hits] == [active.goal_id], "应精确命中活跃目标"

    hits_all = gm.find_goals_by_title("Python", status="active")
    assert active.goal_id in [g.goal_id for g in hits_all]
    assert archived.goal_id not in [g.goal_id for g in hits_all], "归档目标不应出现在 active 结果"

    assert gm.find_goals_by_title("") == [], "空关键字应返回空列表"
    assert gm.find_goals_by_title("   ") == [], "纯空白关键字应返回空列表"
    assert gm.find_goals_by_title("完全无匹配ZZZ") == []
    print("[OK] find goals by title")


def test_cleanup():
    """清理本测试产生的全部数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== intent agent test ===\n")
    setup()
    test_build_text_from_history()
    test_analyze_create_new_with_mock_llm()
    test_analyze_no_intent()
    test_analyze_multiple_candidates_and_dedupe()
    test_analyze_empty_and_error_degrade()
    test_validate_intents_filters_invalid()
    test_apply_create_new_writes_db()
    test_apply_create_new_duplicate_guard()
    test_apply_associate_existing_adds_task()
    test_apply_associate_existing_not_found()
    test_apply_adjust_plan_and_none_do_not_write()
    test_apply_intents_batch_aggregates()
    test_goal_manager_backward_compat()
    test_find_goals_by_title()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
