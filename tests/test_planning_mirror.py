# -*- coding: utf-8 -*-
"""
PlanningMirrorAgent 规划镜像模块测试（离线：通过替换 model_client.chat_sync 模拟 LLM）
覆盖：
- daily_time 档案解析：小时 / 分钟 / 非法值 / 缺失默认
- 状态快照：无目标提示、本地统计正确性、LLM 评估降级、LLM 正常注入
- 影响模拟：输入校验、超载判定与挤占目标排序、LLM 建议解析与降级
- 行动建议：校验器过滤非法项、LLM 异常时本地规则兜底
- 应用建议：优先级/截止日期更新、pause 归档、纯提醒类不写库、批量计数
- 全程异常降级：LLM 抛错不影响 ok 结果，UI 不会崩溃
"""
import os
import sys
from datetime import date, timedelta

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager
from memory.database import init_db, get_connection
from memory.profile_manager import ProfileManager
from agents import planning_mirror_agent as pm_module
from agents.planning_mirror_agent import (
    PlanningMirrorAgent,
    parse_daily_hours,
    parse_deadline_days_left,
)

# 本测试文件创建过的目标 ID，cleanup 时精确清理，不触碰其他数据
_created_goal_ids = []


def setup():
    """初始化数据库并清掉可能残留的 daily_time 档案项"""
    init_db()
    _clear_profile()


def _clear_profile():
    """清理 daily_time 档案项，避免影响其他测试"""
    try:
        ProfileManager.delete("daily_time")
    except Exception:
        pass


def _make_goal(gm: GoalManager, title: str, desc: str = "", **kwargs):
    """创建目标并记录 ID，供 cleanup 精确删除

    幂等保护：先删除同名历史目标（如上次运行中途失败留下的残留），
    避免重名目标干扰按标题取值的断言。
    """
    conn = get_connection()
    dups = conn.execute("SELECT goal_id FROM goals WHERE title = ?", (title,)).fetchall()
    for r in dups:
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (r["goal_id"],))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (r["goal_id"],))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (r["goal_id"],))
    conn.commit()
    conn.close()

    goal = gm.create_goal(title, desc, **kwargs)
    _created_goal_ids.append(goal.goal_id)
    return goal


def _add_task(gm: GoalManager, goal_id: str, title: str, **kwargs) -> int:
    """添加任务并返回其数据库 ID（GoalManager.add_task 本身无返回值）"""
    gm.add_task(goal_id, title, **kwargs)
    return [t for t in gm.get_tasks(goal_id) if t["title"] == title][0]["id"]


def _cleanup():
    """清理本测试产生的目标数据（含任务）与档案项"""
    conn = get_connection()
    for gid in _created_goal_ids:
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()
    _created_goal_ids.clear()
    _clear_profile()


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync，返回还原函数"""
    original = pm_module.model_client.chat_sync
    pm_module.model_client.chat_sync = fake

    def restore():
        pm_module.model_client.chat_sync = original
    return restore


def _archive_all_active_temporarily():
    """临时把库中所有活跃目标置为 archived（用于隔离出"空目标"场景）

    直接用 SQL 更新 status，不触碰 updated_at；返回恢复函数，保证测试结束后
    所有目标状态原样还原（放在 finally 中调用）。
    """
    conn = get_connection()
    foreign = [r["goal_id"] for r in
               conn.execute("SELECT goal_id FROM goals WHERE status='active'").fetchall()]
    for gid in foreign:
        conn.execute("UPDATE goals SET status='archived' WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()

    def _restore():
        conn = get_connection()
        for gid in foreign:
            conn.execute("UPDATE goals SET status='active' WHERE goal_id = ?", (gid,))
        conn.commit()
        conn.close()
    return _restore


# ---------- 工具函数测试 ----------

def test_parse_daily_hours():
    """daily_time 文本解析：小时/数字/分钟/非法/缺失均得到合理结果"""
    assert parse_daily_hours(None) == 2.0, "缺失时应回退默认 2 小时"
    assert parse_daily_hours("") == 2.0
    assert parse_daily_hours("abc") == 2.0, "无法解析时应回退默认"
    assert parse_daily_hours("2") == 2.0
    assert parse_daily_hours("1.5小时") == 1.5
    assert parse_daily_hours("3H") == 3.0
    assert parse_daily_hours("90分钟") == 1.5, "分钟应换算为小时"
    assert parse_daily_hours("0小时") == 2.0, "非正数应回退默认"
    assert parse_daily_hours("99") == 16.0, "异常大值应钳制到上限"
    print("[OK] parse daily hours")


def test_parse_deadline_days_left():
    """截止日期解析：合法日期返回距今天数，空/非法返回 None"""
    today = date(2026, 6, 15)
    assert parse_deadline_days_left("2026-06-20", today=today) == 5
    assert parse_deadline_days_left("2026-06-10", today=today) == -5, "过期返回负数"
    assert parse_deadline_days_left("", today=today) is None
    assert parse_deadline_days_left(None, today=today) is None
    assert parse_deadline_days_left("不是日期", today=today) is None
    # 兼容带时间的完整时间戳（取前 10 位）
    assert parse_deadline_days_left("2026-06-16 08:30:00", today=today) == 1
    print("[OK] parse deadline days left")


# ---------- 状态快照测试 ----------

def test_snapshot_no_goals():
    """无进行中目标时给出友好提示且不崩溃（临时归档既有目标以隔离场景）"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    restore = _archive_all_active_temporarily()
    try:
        result = agent.generate_snapshot()
    finally:
        restore()
    assert not result["ok"]
    assert "暂无" in result["message"]
    assert result["snapshot"] == {} and result["display"] == ""
    print("[OK] snapshot no goals")


def test_snapshot_local_stats_with_llm_down():
    """LLM 抛异常时快照仍成功生成（本地统计降级），数据正确

    开发库中可能存在其他活跃目标，聚合值改为与 get_active_goals_overview
    实时对账；单目标的估算字段按本次创建的目标精确断言。
    """
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    g1 = _make_goal(gm, "镜像目标A", deadline="2099-01-01", priority=1)
    g2 = _make_goal(gm, "镜像目标B")
    gm.add_task(g1.goal_id, "t1")
    t2_id = _add_task(gm, g1.goal_id, "t2")
    gm.update_task_status(t2_id, "done")
    gm.add_task(g2.goal_id, "t3")

    restore = _patch_chat_sync(lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("网络断开")))
    try:
        result = agent.generate_snapshot()
    finally:
        restore()

    assert result["ok"], f"LLM 失败不应导致快照失败：{result['message']}"
    snap = result["snapshot"]
    # 聚合值与实时概览对账（不假设库中只有本测试的目标）
    overview_all = gm.get_active_goals_overview()
    exp_total = sum(int(g["total_tasks"]) for g in overview_all)
    exp_completed = sum(int(g["completed_tasks"]) for g in overview_all)
    assert snap["total_goals"] == len(overview_all)
    assert snap["total_tasks"] == exp_total, "任务总数应与实时概览一致（含本测试新建的 3 个）"
    assert snap["completed_tasks"] == exp_completed
    assert snap["pending_tasks"] == exp_total - exp_completed
    exp_rate = round(exp_completed / exp_total * 100, 1) if exp_total else 0.0
    assert snap["overall_completion_rate"] == exp_rate
    assert snap["llm_summary"] == "", "降级时不应有 LLM 总结"
    assert "降级" in result["message"]
    # 单目标估算字段按自建目标精确断言
    by_title = {g["title"]: g for g in snap["goals"]}
    assert by_title["镜像目标A"]["remaining_hours"] == 2.0, "1 个待办 × 2h"
    assert by_title["镜像目标B"]["days_left"] is None, "未设截止日期 days_left 为 None"
    assert by_title["镜像目标A"]["is_lagging"] is False, "远期截止 + 低负荷不算滞后"
    assert "目标状态快照" in result["display"]
    print("[OK] snapshot local stats with llm down")


def test_snapshot_lagging_detection():
    """滞后判定：已过期 → 滞后；临近截止但工作量超容量 → 滞后"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    yesterday = date.today() - timedelta(days=3)
    soon = date.today() + timedelta(days=2)
    g_expired = _make_goal(gm, "过期目标", deadline=yesterday.strftime("%Y-%m-%d"))
    g_tight = _make_goal(gm, "超负荷目标",
                         deadline=soon.strftime("%Y-%m-%d"), priority=1)
    gm.add_task(g_expired.goal_id, "e1")
    for i in range(5):  # 5 个待办 × 2h = 10h，2 天内需 5h/天 > 默认 2h/天
        gm.add_task(g_tight.goal_id, f"s{i}")

    restore = _patch_chat_sync(lambda messages, **kw: "")
    try:
        result = agent.generate_snapshot()
    finally:
        restore()

    loads = {g["title"]: g for g in result["snapshot"]["goals"]}
    assert loads["过期目标"]["expired"] is True and loads["过期目标"]["is_lagging"] is True
    assert loads["超负荷目标"]["is_lagging"] is True
    assert loads["超负荷目标"]["required_daily_hours"] == 5.0
    print("[OK] snapshot lagging detection")


def test_snapshot_with_mock_llm_summary():
    """mock LLM 正常返回总结：注入快照并出现在展示区；daily_time 生效"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    ProfileManager.set("daily_time", "4小时")
    g1 = _make_goal(gm, "总结目标")
    gm.add_task(g1.goal_id, "t1")

    fake_response = '```json\n{"summary":"整体进度可控，注意 B 目标已滞后。"}\n```'
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.generate_snapshot()
    finally:
        restore()

    assert result["ok"]
    snap = result["snapshot"]
    assert snap["llm_summary"].startswith("整体进度可控")
    assert snap["daily_capacity_hours"] == 4.0, "档案中的 daily_time 应生效"
    assert "总体评估" in result["display"] and "滞后" in result["display"]
    assert "降级" not in result["message"]
    # 及时清理档案项，避免影响后续用例对默认容量（2h）的假设
    ProfileManager.delete("daily_time")
    print("[OK] snapshot with mock llm summary")


# ---------- 影响模拟测试 ----------

def test_simulate_new_goal_validation():
    """输入校验：空标题拒绝；周期与优先级钳制到合法范围"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    bad = agent.simulate_new_goal("   ")
    assert not bad["ok"] and "标题" in bad["message"]

    restore = _patch_chat_sync(lambda messages, **kw: "")
    try:
        result = agent.simulate_new_goal("钳制测试", estimated_weeks=999, priority=9)
    finally:
        restore()
    ng = result["impact"]["new_goal"]
    assert ng["estimated_weeks"] == 52 and ng["priority"] == 3, "超出范围应被钳制"
    print("[OK] simulate new goal validation")


def test_simulate_new_goal_overload_and_crowded():
    """现有目标占满容量时新增高优先级长周期目标 → 超载 + 给出挤占目标"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    # 构造一个每日需求约 4 小时的既有高优目标（超过默认 2h 容量）
    g_busy = _make_goal(gm, "繁忙目标", priority=1,
                        deadline=(date.today()
                                  + timedelta(days=5)).strftime("%Y-%m-%d"))
    for i in range(10):
        gm.add_task(g_busy.goal_id, f"b{i}")  # 10 待办 × 2h / 5 天 = 4h/天

    restore = _patch_chat_sync(lambda messages, **kw: "")
    try:
        result = agent.simulate_new_goal("新的大目标", "描述", estimated_weeks=8, priority=1)
    finally:
        restore()

    assert result["ok"]
    impact = result["impact"]
    assert impact["overloaded"] is True
    after = impact["after_add"]
    # 开发库可能还有其他活跃目标：总数应为"当前活跃数 + 1"
    assert after["total_goals"] == len(gm.get_active_goals_overview()) + 1
    assert after["total_required_daily_hours"] > after["daily_capacity_hours"]
    crowded_titles = [g["title"] for g in impact["crowded_goals"]]
    assert "繁忙目标" in crowded_titles, "时间需求最大的既有目标应被标记为可能被挤占"
    assert any("超载" in a or "暂缓" in a for a in impact["advice"]), "降级建议应指出超载"
    assert "影响模拟" in result["display"]
    print("[OK] simulate new goal overload and crowded")


def test_simulate_new_goal_with_mock_llm():
    """mock LLM 返回建议列表：正常解析展示；超载判定与需求/容量关系一致"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)

    fake_response = '''{"advice":["建议把「旧目标」的截止日期顺延两周","建议新目标先从每周 3 小时起步"]}'''
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.simulate_new_goal("轻量新目标", estimated_weeks=2, priority=3)
    finally:
        restore()

    assert result["ok"]
    impact = result["impact"]
    after = impact["after_add"]
    # 超载标志与"总需求 > 容量"的判定保持一致（不假设库中其他目标的负荷）
    expected_overload = after["total_required_daily_hours"] > after["daily_capacity_hours"]
    assert impact["overloaded"] == expected_overload
    assert after["total_required_daily_hours"] == round(
        after["existing_required_daily_hours"] + impact["new_goal"]["assumed_daily_hours"], 2)
    assert len(impact["advice"]) == 2
    assert "顺延两周" in result["display"]
    print("[OK] simulate new goal with mock llm")


def test_simulate_advice_validator_filters_junk():
    """建议校验器：非字符串/None/空白项被过滤，最多保留 5 条"""
    data = {"advice": ["合法建议", None, "   ", 123, "第二条", "三", "四", "五", "六"]}
    cleaned = PlanningMirrorAgent._validate_advice(data)
    assert cleaned[0] == "合法建议" and "123" in cleaned, "数字会被转为字符串保留"
    assert len(cleaned) == 5, "最多 5 条"
    assert PlanningMirrorAgent._validate_advice(None) == []
    assert PlanningMirrorAgent._validate_advice({"advice": "not-a-list"}) == []
    print("[OK] simulate advice validator filters junk")


# ---------- 行动建议测试 ----------

def test_suggest_actions_no_goals():
    """无目标时行动建议返回友好提示（临时归档既有目标以隔离场景）"""
    agent = PlanningMirrorAgent(GoalManager())
    restore = _archive_all_active_temporarily()
    try:
        result = agent.suggest_next_actions()
    finally:
        restore()
    assert not result["ok"] and "暂无" in result["message"]
    print("[OK] suggest actions no goals")


def test_suggest_actions_with_mock_llm_filters_invalid():
    """mock LLM 混合返回：非法 action 与不存在 goal_id 被过滤，数值/日期校验生效"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    g1 = _make_goal(gm, "建议目标A", priority=1)
    g2 = _make_goal(gm, "建议目标B", priority=3)
    gm.add_task(g1.goal_id, "a1")
    gm.add_task(g2.goal_id, "b1")

    def fake_chat_sync(messages, **kwargs):
        return ('{"analysis":"两个目标并行但时间紧张","suggestions":['
                '{"goal_id":"' + g1.goal_id + '","action":"focus","reason":"接近可收尾",'
                '"new_priority":1,"new_deadline":"2026-12-01"},'
                '{"goal_id":"' + g2.goal_id + '","action":"pause","reason":"精力不足",'
                '"new_priority":null,"new_deadline":null},'
                '{"goal_id":"goal_not_exist","action":"focus","reason":"非法目标"},'
                '{"goal_id":"' + g2.goal_id + '","action":"hack","reason":"非法动作"},'
                '{"goal_id":"' + g1.goal_id + '","action":"complete_soon","reason":"ok",'
                '"new_priority":"x","new_deadline":"坏日期"}]}')

    restore = _patch_chat_sync(fake_chat_sync)
    try:
        result = agent.suggest_next_actions(limit=3)
    finally:
        restore()

    assert result["ok"], result["message"]
    suggestions = result["suggestions"]
    assert len(suggestions) == 3, "两条非法建议应被过滤"
    actions = {s["action"] for s in suggestions}
    assert actions.issubset({"focus", "pause", "complete_soon"})
    focus = [s for s in suggestions if s["action"] == "focus"][0]
    assert focus["new_priority"] == 1 and focus["new_deadline"] == "2026-12-01"
    cs = [s for s in suggestions if s["action"] == "complete_soon"][0]
    assert cs["new_priority"] is None and cs["new_deadline"] is None, "非法值应置 None"
    assert "行动建议" in result["display"]
    print("[OK] suggest actions with mock llm filters invalid")


def test_suggest_actions_fallback_rules():
    """LLM 抛异常时降级为本地规则引擎：滞后目标在前、动作类型合法、数量受限"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    # 先归档库中已有活跃目标（此刻尚无本测试的目标），再创建本测试目标，
    # 保证"滞后排序最前"的断言只取决于本测试数据；结束后全部还原。
    isolate = _archive_all_active_temporarily()
    expired_date = (date.today()
                    - timedelta(days=10)).strftime("%Y-%m-%d")
    g_low = _make_goal(gm, "低优过期目标", priority=3, deadline=expired_date)
    gm.add_task(g_low.goal_id, "l1")
    g_ok = _make_goal(gm, "健康目标", priority=1)
    gm.add_task(g_ok.goal_id, "o1")

    restore = _patch_chat_sync(lambda messages, **kw: (_ for _ in ()).throw(TimeoutError("超时")))
    try:
        result = agent.suggest_next_actions(limit=3)
    finally:
        restore()
        isolate()

    assert result["ok"], result["message"]
    suggestions = result["suggestions"]
    assert 1 <= len(suggestions) <= 3
    assert all(s["action"] in pm_module.VALID_ACTION_TYPES for s in suggestions)
    first = suggestions[0]
    assert first["goal_id"] == g_low.goal_id, "过期滞后目标应排在最前"
    assert first["action"] == "adjust_deadline" and first["new_deadline"], "过期目标建议顺延截止日期"
    import re as _re
    assert _re.match(r"^\d{4}-\d{2}-\d{2}$", first["new_deadline"]), "兜底日期应为 YYYY-MM-DD"
    assert "本地规则" in result["message"]
    print("[OK] suggest actions fallback rules")


# ---------- 应用建议测试 ----------

def test_apply_single_updates_db():
    """单条应用：优先级/截止日期更新生效；未知目标安全跳过"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    g1 = _make_goal(gm, "待调整目标")

    r1 = agent._apply_single({"goal_id": g1.goal_id, "action": "adjust_priority",
                              "reason": "", "new_priority": 1, "new_deadline": "2026-12-31"})
    assert r1["ok"] and r1["changed"]
    updated = gm.get_goal(g1.goal_id)
    assert updated.priority == 1 and updated.deadline == "2026-12-31"

    r2 = agent._apply_single({"goal_id": "goal_missing", "action": "focus"})
    assert not r2["ok"] and "不存在" in r2["message"]

    r3 = agent._apply_single({"goal_id": g1.goal_id, "action": "weird_action"})
    assert not r3["ok"] and "未知" in r3["message"]
    print("[OK] apply single updates db")


def test_apply_pause_and_reminder_types():
    """pause 归档目标；complete_soon / 无参 focus 仅提醒不写库"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    g_pause = _make_goal(gm, "将被暂停的目标")
    g_soon = _make_goal(gm, "即将完成的目标")
    gm.add_task(g_soon.goal_id, "收尾任务")

    r1 = agent._apply_single({"goal_id": g_pause.goal_id, "action": "pause",
                              "reason": "", "new_priority": None, "new_deadline": None})
    assert r1["ok"] and r1["changed"]
    assert gm.get_goal(g_pause.goal_id).status == "archived"

    before = gm.get_tasks(g_soon.goal_id)[0]["status"]
    r2 = agent._apply_single({"goal_id": g_soon.goal_id, "action": "complete_soon",
                              "reason": "", "new_priority": None, "new_deadline": None})
    assert r2["ok"] and not r2["changed"], "complete_soon 是提醒类，不改数据"
    assert gm.get_tasks(g_soon.goal_id)[0]["status"] == before

    r3 = agent._apply_single({"goal_id": g_soon.goal_id, "action": "focus",
                              "reason": "", "new_priority": None, "new_deadline": None})
    assert r3["ok"] and not r3["changed"], "无参数 focus 不写库"
    print("[OK] apply pause and reminder types")


def test_apply_batch_counting_and_details():
    """批量应用：applied 只计实际修改数据的条数，details 含每条结果"""
    gm = GoalManager()
    agent = PlanningMirrorAgent(gm)
    g1 = _make_goal(gm, "批处理目标一")
    g2 = _make_goal(gm, "批处理目标二")
    g3 = _make_goal(gm, "批处理目标三")

    suggestions = [
        {"goal_id": g1.goal_id, "action": "adjust_deadline", "reason": "",
         "new_priority": None, "new_deadline": "2026-11-30"},
        {"goal_id": g2.goal_id, "action": "pause", "reason": "",
         "new_priority": None, "new_deadline": None},
        {"goal_id": g3.goal_id, "action": "complete_soon", "reason": "",
         "new_priority": None, "new_deadline": None},
        {"goal_id": "goal_not_exist", "action": "focus", "reason": "",
         "new_priority": None, "new_deadline": None},
    ]
    result = agent.apply_suggestion(suggestions)
    assert result["ok"] and result["applied"] == 2, "只有两条真正改了数据"
    assert len(result["details"]) == 4, "每条建议都应有反馈详情"
    assert gm.get_goal(g1.goal_id).deadline == "2026-11-30"
    assert gm.get_goal(g2.goal_id).status == "archived"

    # 别名方法与单条字典容错
    alias = agent.apply_suggestions(suggestions[:1])
    assert alias["applied"] == 1
    single = agent.apply_suggestion({"goal_id": g1.goal_id, "action": "adjust_priority",
                                     "new_priority": 3, "new_deadline": None})
    assert single["applied"] == 1 and gm.get_goal(g1.goal_id).priority == 3

    empty = agent.apply_suggestion([])
    assert not empty["ok"] and empty["applied"] == 0
    print("[OK] apply batch counting and details")


def test_cleanup():
    """清理测试数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== planning mirror test ===\n")
    setup()
    test_parse_daily_hours()
    test_parse_deadline_days_left()
    test_snapshot_no_goals()
    test_snapshot_local_stats_with_llm_down()
    test_snapshot_lagging_detection()
    test_snapshot_with_mock_llm_summary()
    test_simulate_new_goal_validation()
    test_simulate_new_goal_overload_and_crowded()
    test_simulate_new_goal_with_mock_llm()
    test_simulate_advice_validator_filters_junk()
    test_suggest_actions_no_goals()
    test_suggest_actions_with_mock_llm_filters_invalid()
    test_suggest_actions_fallback_rules()
    test_apply_single_updates_db()
    test_apply_pause_and_reminder_types()
    test_apply_batch_counting_and_details()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
