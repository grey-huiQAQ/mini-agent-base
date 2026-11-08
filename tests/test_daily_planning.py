# -*- coding: utf-8 -*-
"""
DailyPlannerAgent 每日规划模块测试（离线：通过替换 model_client.chat_sync 模拟 LLM）
覆盖：
- 数据库迁移：goal_tasks.planned_date 列存在、旧任务默认 NULL
- 启发式估时：普通/长标题任务耗时估算
- 候选任务收集：排序规则（优先级 → 截止日期）、已完成任务与归档目标过滤
- 生成计划：无待办提示、mock LLM 正常解析、非法 task_id 过滤、LLM 异常本地规则降级、超载提醒、经验注入
- 缺 daily_time 提示与本次时长 override（spec §4 判据 7）：提示句、override 生效、不写档案、输入框不自动重算
- 老版生成停用、历史可读（spec §4 判据 8）：开机不再起调度线程、速览页两枚手动生成按钮无绑定且不可点、预览不往 daily_plans 写新行、历史行仍读得到
- 应用计划：写入 planned_date、覆盖旧标记、跳过不存在任务、空计划拒绝、清除标记接口
- 全程异常降级：LLM 抛错不影响 ok 结果，UI 不会崩溃
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
from memory.profile_manager import ProfileManager
from memory.experience_store import ExperienceStore
from agents import daily_planning_agent as dp_module
from agents.daily_planning_agent import (
    DailyPlannerAgent,
    estimate_task_hours,
    MAX_PLAN_TASKS,
)

# 本测试文件创建过的目标 ID，cleanup 时精确清理，不触碰其他数据
_created_goal_ids = []
# 本测试文件添加过的经验 ID，cleanup 时精确删除
_created_experience_ids = []


def setup():
    """初始化数据库（触发 planned_date 迁移）并清理档案项"""
    init_db()
    _clear_profile()


def _clear_profile():
    """清理 daily_time 档案项，避免影响其他测试对默认可用时间的假设"""
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


def _add_experience(scenario: str, strategy: str) -> int:
    """添加经验并记录 ID，供 cleanup 精确删除"""
    exp_id = ExperienceStore.add_experience(scenario=scenario, strategy=strategy)
    _created_experience_ids.append(exp_id)
    return exp_id


def _cleanup():
    """清理本测试产生的目标数据（含任务）、经验与档案项"""
    conn = get_connection()
    for gid in _created_goal_ids:
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    for eid in _created_experience_ids:
        conn.execute("DELETE FROM experiences WHERE id = ?", (eid,))
    conn.commit()
    conn.close()
    _created_goal_ids.clear()
    _created_experience_ids.clear()
    _clear_profile()


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync，返回还原函数"""
    original = dp_module.model_client.chat_sync
    dp_module.model_client.chat_sync = fake

    def restore():
        dp_module.model_client.chat_sync = original
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


# ---------- 数据库迁移测试 ----------

def test_migration_planned_date_column():
    """init_db 后 goal_tasks 应包含 planned_date 列，旧任务默认为 NULL"""
    init_db()  # 幂等：重复调用不报错、不重复加列
    conn = get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(goal_tasks)").fetchall()]
    conn.close()
    assert "planned_date" in cols, "迁移后 goal_tasks 应存在 planned_date 列"

    gm = GoalManager()
    g = _make_goal(gm, "迁移验证目标")
    tid = _add_task(gm, g.goal_id, "普通旧任务")
    task = [t for t in gm.get_tasks(g.goal_id) if t["id"] == tid][0]
    # SELECT * 天然带出新列；未标记过的任务该字段为 None，旧查询/展示不受影响
    assert task.get("planned_date", "missing") is None, \
        f"新任务的 planned_date 应为 None，实际为 {task.get('planned_date')!r}"
    print("[OK] migration planned_date column")


def test_set_task_planned_date_roundtrip():
    """set_task_planned_date 写入 / 清除往返，且不存在的任务返回 False"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "计划日期读写目标")
    tid = _add_task(gm, g.goal_id, "被安排的任务")

    assert agent.goal_manager.set_task_planned_date(tid, "2026-01-15") is True
    saved = gm.get_tasks(g.goal_id)[0]["planned_date"]
    assert saved == "2026-01-15"

    assert agent.goal_manager.set_task_planned_date(tid, None) is True, "None 应清除标记"
    assert gm.get_tasks(g.goal_id)[0]["planned_date"] is None

    assert agent.goal_manager.set_task_planned_date(99999999, "2026-01-15") is False, \
        "不存在的任务应返回 False"
    print("[OK] set task planned date roundtrip")


# ---------- 启发式估时与候选收集 ----------

def test_estimate_task_hours():
    """启发式估时：短标题基础 2 小时，长标题适度上调并钳制上限"""
    assert estimate_task_hours("背单词") == 2.0, "短标题应为基础估时"
    assert estimate_task_hours("") == 2.0, "空标题回退基础估时"
    long_title = "完成项目综合实战演练并整理完整学习笔记与复盘报告"  # >= 15 字符
    assert estimate_task_hours(long_title) == 2.5, "长标题应在基础上调 0.5 小时"
    print("[OK] estimate task hours")


def test_collect_candidates_order_and_filter():
    """候选收集：优先级小者在前 → 截止早者在前；done 任务与非 active 目标被过滤"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)

    # 目标 A：无截止日期；目标 B：截止 2099 年（晚）；目标 C：截止 2090 年（最早）
    g_a = _make_goal(gm, "规划目标A")
    g_b = _make_goal(gm, "规划目标B", deadline="2099-12-31")
    g_c = _make_goal(gm, "规划目标C", deadline="2090-01-01")
    g_archived = _make_goal(gm, "规划目标D已归档")
    gm.archive_goal(g_archived.goal_id)

    t_low_in_a = _add_task(gm, g_a.goal_id, "A低优任务", priority=2)
    t_high_in_b = _add_task(gm, g_b.goal_id, "B高优任务", priority=1)
    t_done = _add_task(gm, g_c.goal_id, "C已完成", priority=1)
    gm.update_task_status(t_done, "done")
    t_pending_c = _add_task(gm, g_c.goal_id, "C高优任务", priority=1)
    _add_task(gm, g_archived.goal_id, "归档目标任务", priority=1)

    candidates = agent._collect_candidates()
    by_title = {c["task_title"]: c for c in candidates}

    # 过滤断言
    assert all(c["task_title"] != "C已完成" for c in candidates), "已完成任务不应进入候选"
    assert all(c["goal_title"] != "规划目标D已归档" for c in candidates), "归档目标不应进入候选"

    # 排序断言：P1 且截止最早的目标 C 任务最前；同为 P1 时无期限的 A 排在 B 后；
    # P2 的 A 低优任务最后
    titles = [c["task_title"] for c in candidates]
    assert titles.index("C高优任务") < titles.index("B高优任务"), \
        "同优先级下截止更早的任务应排前"
    assert titles.index("C高优任务") < titles.index("A低优任务"), "优先级数字小的任务应排前"
    assert titles.index("B高优任务") < titles.index("A低优任务")

    # 结构断言：候选字段完整
    item = by_title["C高优任务"]
    for field in ("task_id", "goal_id", "task_title", "goal_title",
                  "estimated_hours", "priority", "deadline"):
        assert field in item, f"候选缺少字段 {field}"
    assert item["estimated_hours"] > 0 and item["task_id"] == t_pending_c
    assert by_title["A低优任务"]["task_id"] == t_low_in_a
    print("[OK] collect candidates order and filter")


# ---------- 生成今日计划 ----------

def test_generate_no_pending_tasks():
    """无进行中目标的待办任务时给出友好提示且不崩溃（临时归档既有目标以隔离场景）"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    restore = _archive_all_active_temporarily()
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()
    assert not result["ok"]
    assert "暂无" in result["message"]
    assert result["plan"] == [] and result["display"] == ""
    print("[OK] generate no pending tasks")


def test_generate_with_mock_llm():
    """mock LLM 返回合法计划：正常解析、结构完整、展示区含推荐理由与总结"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g1 = _make_goal(gm, "计划目标一")
    g2 = _make_goal(gm, "计划目标二", deadline="2099-06-01")
    tid1 = _add_task(gm, g1.goal_id, "今日攻坚任务", priority=1)
    tid2 = _add_task(gm, g2.goal_id, "次要收尾任务", priority=3)

    fake_response = ('{"summary":"先集中攻克高优先级","advice":"",'
                     '"plan":[{"task_id":%d,"reason":"优先级最高且临近考核"},'
                     '{"task_id":%d,"reason":"时间充裕时顺手收尾"}]}' % (tid1, tid2))
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()

    assert result["ok"], result["message"]
    plan = result["plan"]
    assert len(plan) == 2
    assert [p["task_id"] for p in plan] == [tid1, tid2], "计划应按 LLM 返回顺序包含两个合法任务"
    first = plan[0]
    assert first["task_id"] == tid1 and first["goal_id"] == g1.goal_id
    assert first["task_title"] == "今日攻坚任务" and first["goal_title"] == "计划目标一"
    assert isinstance(first["estimated_hours"], float) and first["reason"]
    assert "本地规则" not in result["message"], "LLM 成功时消息不应出现降级说明"
    display = result["display"]
    assert "今日计划建议" in display
    assert "先集中攻克高优先级" in display, "LLM 总结应出现在展示区"
    assert "优先级最高且临近考核" in display
    print("[OK] generate with mock llm")


def test_generate_llm_filters_invalid_entries():
    """mock LLM 混入非法项：编造的 task_id / 重复项 / 非字典条目被过滤，数量受上限约束"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "过滤校验目标")
    real_ids = [_add_task(gm, g.goal_id, f"任务{i}", priority=i + 1) for i in range(3)]

    fake_response = ('{"summary":"","advice":"","plan":['
                     '{"task_id":999999,"reason":"编造的任务"},'
                     '"not-a-dict",'
                     '{"task_id":"abc","reason":"非法ID"},'
                     '{"task_id":%d,"reason":"第一条"},'
                     '{"task_id":%d,"reason":"重复条目"},'
                     '{"task_id":%d,"reason":"合法第二条"}]}' % (real_ids[0], real_ids[0], real_ids[1]))
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()

    assert result["ok"]
    got_ids = [p["task_id"] for p in result["plan"]]
    assert got_ids == [real_ids[0], real_ids[1]], \
        f"应只保留去重后的合法任务：{got_ids}"
    assert all(i in real_ids for i in got_ids)
    print("[OK] generate llm filters invalid entries")


def test_generate_fallback_when_llm_down():
    """LLM 抛异常时降级为本地规则：按排序取任务直到时间用尽，消息标注降级来源

    先临时归档库中已有活跃目标再创建本测试目标（结束后还原），
    保证"按排序取前三个"的绝对位置断言只取决于本测试数据。
    """
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    isolate = _archive_all_active_temporarily()
    ProfileManager.set("daily_time", "6")  # 6 小时可容纳 3 个 2 小时任务
    g1 = _make_goal(gm, "降级目标一", priority=1)
    g2 = _make_goal(gm, "降级目标二", priority=3, deadline="2099-01-01")
    tid_first = _add_task(gm, g1.goal_id, "最高优先任务", priority=1)
    tid_second = _add_task(gm, g1.goal_id, "无期限的次高任务", priority=2)
    tid_third = _add_task(gm, g2.goal_id, "有期限的次高任务", priority=2)
    _add_task(gm, g2.goal_id, "第四顺位任务", priority=3)

    raise_error = lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("网络断开"))
    restore = _patch_chat_sync(raise_error)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()
        ProfileManager.delete("daily_time")
        isolate()

    assert result["ok"], "LLM 失败不应导致生成失败：" + result["message"]
    plan = result["plan"]
    got_ids = [p["task_id"] for p in plan]
    # P1 最先；两个 P2 中截止日期更早者（2099-01-01）排在无期限之前
    assert got_ids == [tid_first, tid_third, tid_second], \
        f"降级时应按 优先级→截止日期 排序取任务：{got_ids}"
    assert len(plan) <= 3, "6 小时容量最多容纳 3 个 2 小时任务"
    assert all(p.get("reason") for p in plan), "降级计划也应给出推荐理由"
    assert "本地规则" in result["message"], "降级时消息应注明来自本地规则"
    assert "今日计划建议" in result["display"]
    print("[OK] generate fallback when llm down")


def test_generate_overload_note_in_display():
    """可用时间不足时：展示区给出超载提醒与调整建议"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    ProfileManager.set("daily_time", "1小时")  # 仅 1 小时，任何 2 小时任务都会超载
    g = _make_goal(gm, "超载场景目标")
    _add_task(gm, g.goal_id, "必须完成的核心任务", priority=1)

    raise_error = lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("LLM 宕机"))
    restore = _patch_chat_sync(raise_error)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()
        ProfileManager.delete("daily_time")

    assert result["ok"]
    assert len(result["plan"]) == 1, "兜底规则应至少保留 1 个任务供用户取舍"
    assert "超载提醒" in result["display"], "总耗时超过可用时间应有超载提醒"
    assert "调整建议" in result["display"]
    print("[OK] generate overload note in display")


def test_generate_includes_experiences_in_prompt():
    """相关经验应注入 LLM 提示词；经验检索异常时不阻断主流程"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "经验注入目标")
    _add_task(gm, g.goal_id, "需要经验参考的任务", priority=1)
    # 场景文案刻意包含检索关键词"今日计划"，确保 search_experiences 能命中
    _add_experience(scenario="制定今日计划时容易贪多",
                    strategy="每天只安排 2 个核心任务，其余列为可选")

    captured = []

    def fake_chat_sync(messages, **kwargs):
        captured.append(messages)
        return 'not-a-json'  # 返回非法 JSON 触发降级，但提示词已被捕获

    restore = _patch_chat_sync(fake_chat_sync)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()

    assert result["ok"] and "本地规则" in result["message"], "非法 JSON 应走降级路径"
    assert captured, "应至少发起一次 LLM 调用"
    prompt_text = captured[0][-1]["content"]
    assert "每天只安排 2 个核心任务" in prompt_text, "相关经验策略应出现在提示词中"
    print("[OK] generate includes experiences in prompt")


def test_daily_plan_missing_daily_time_hint_and_override():
    """判据 7（spec §4）：缺 daily_time 提示句 + 本次 override 不写档案 + 输入框不自动重算

    四条断言逐条对应 spec §4 判据 7 的 ①②③④：
    ① 档案没有 daily_time 时，展示区显式给出「尚未设置每日可用时间，本次暂按 2 小时安排」；
       反面再验一次「档案设了 2」时这句不出现——回落值与用户自己填的 2 在数值上不可分辨，
       提示句只能按来源说话，否则会当着用户撒谎。
    ② generate_daily_plan(daily_hours_override=1.5) 的展示与状态消息都按 1.5 小时。
    ③ 上面两趟生成走完，profile 表里 daily_time 的行与取值一字不变（本次调整只影响本次预览）。
    ④ 那只「本次可用小时」输入框在 ui/app_factory.py 里没被接上任何事件
       （.change／.input／.blur 一概不许有）：动一下框就重算＝把人随手滑动变成一串模型
       调用（R4），重算只发生在点按钮那一下。
    """
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root_dir, "ui", "app_factory.py"), encoding="utf-8") as f:
        app_factory_src = f.read()

    def _daily_time_rows():
        """profile 表里 daily_time 那些行（取值一起比，改写也算数）"""
        conn = get_connection()
        rows = conn.execute(
            "SELECT key, value FROM profile WHERE key = 'daily_time'").fetchall()
        conn.close()
        return [(r["key"], r["value"]) for r in rows]

    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "时长提示与本次调整目标")
    _add_task(gm, g.goal_id, "本次调整要排的任务", priority=1)

    ProfileManager.delete("daily_time")
    rows_before = _daily_time_rows()
    assert rows_before == [], "前置没立起来：本用例开头档案里就不该有 daily_time 行"

    captured = {"calls": 0}

    def fake_chat_sync(messages, **kwargs):
        captured["calls"] += 1
        return "not-a-json"  # 非法 JSON ⇒ 走本地规则降级，展示区照常生成（离线，不打网络）

    restore = _patch_chat_sync(fake_chat_sync)
    try:
        # ① 缺值：提示句必须出现
        r_missing = agent.generate_daily_plan()
        assert r_missing["ok"], r_missing["message"]
        assert "尚未设置每日可用时间，本次暂按 2 小时安排" in r_missing["display"], (
            "档案缺 daily_time 时展示区必须说明这次用的是回落值——"
            "spec §2.1：不把默认 2 小时当成用户确认过的时间")

        # ② 本次 override=1.5：展示与消息都按 1.5 小时
        r_override = agent.generate_daily_plan(daily_hours_override=1.5)
        assert r_override["ok"], r_override["message"]
        assert "1.5 小时" in r_override["display"], "展示区应按本次 override 的小时数"
        assert "可用 1.5 小时" in r_override["message"], "状态消息同样要按本次 override"
        assert "尚未设置每日可用时间" not in r_override["display"], (
            "这次用的是用户刚填的 1.5 小时，再说「本次暂按 2 小时安排」"
            "就与同一屏上面那行自相矛盾（spec §2.1：有 override 时按 override 展示）")

        # ③ 两趟生成都不许碰档案
        assert _daily_time_rows() == rows_before, (
            f"生成预览不该写档案，但 daily_time 变成了 {_daily_time_rows()}")
        assert captured["calls"] >= 2, "两次生成都应各发起一次模型调用（已被桩住）"

        # ① 的反面：档案设了 2 ⇒ 数值与回落时一模一样，但提示句不该出现
        ProfileManager.set("daily_time", "2")
        try:
            r_profile = agent.generate_daily_plan()
            assert r_profile["ok"], r_profile["message"]
            assert "今日可用时间：**2.0 小时**" in r_profile["display"]
            assert "尚未设置每日可用时间" not in r_profile["display"], (
                "档案里真的设了值，就不能对用户说「尚未设置」")
        finally:
            ProfileManager.delete("daily_time")

        # ④ 输入框不绑事件（静态读绑定区）
        wired = [ln.strip() for ln in app_factory_src.splitlines()
                 if "home_plan_hours." in ln]
        assert not wired, (
            f"「本次可用小时」输入框被接上了事件（R4：只有点按钮才重算）：{wired}")
        assert app_factory_src.count("inputs=[home_plan_hours]") == 2, (
            "这只框应只作为两枚按钮（⚡ 帮我排今天／🔁 按此时长重新生成预览）的输入")
        assert "home_plan_regen_btn.click(" in app_factory_src, (
            "「🔁 按此时长重新生成预览」必须真接上，否则本次调整没有入口")
    finally:
        restore()
        ProfileManager.delete("daily_time")
    assert _daily_time_rows() == rows_before, "本用例没把档案还原成开头那样"
    print("[OK] daily plan missing daily_time hint and override")


def test_old_plan_generation_disabled_history_readable():
    """判据 8（spec §4）：老版生成停用、历史可读（裁定 2／R5／R6）

    四条断言逐条对应 spec §4 判据 8 的 ①②③④：
    ① app.py 的 __main__ 不再调用 start_scheduler()。用 ast 扫调用节点而不是按文字扫：
       那一段里留着「原来那行调用已删」的注记，按文字匹配会把注记当成调用。
       反向两头一起钉——scheduler.py 文件与两个函数仍在（第 7 步只删调用）。
    ② 速览页那两枚「⚡ 手动生成」没有任何 .click 绑定，且声明处带 interactive=False；
       两枚「🔄 刷新」仍接在 refresh_plan／refresh_review 上：停用生成不等于拆掉读历史那两口。
    ③ daily_plans 里的一行历史文本能被 learning_controller.refresh_plan() 原样读回，
       清掉后读回占位文案；而现存的预览链路（do_generate_daily_plan → DailyPlannerAgent）
       走完一遍，daily_plans 除夹具那一行以外不许多行——「不再产新行」与「历史可读」
       是同一条判据的两半，只钉任一半都会放过另一半的回归。
    ④ spec §6.4 那道护栏改判到位：tests/test_skeleton.py 里「不再注册每日生成任务」的
       新口径存在、旧断言串不存在、新旧函数名各在其位（含 __main__ 那一行调用清单）。
    """
    import ast
    from datetime import datetime
    from memory.daily_planner import DailyPlanner
    from ui.controllers import learning_controller as lc

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(*parts):
        with open(os.path.join(root_dir, *parts), encoding="utf-8") as fh:
            return fh.read()

    def _startup_calls_named(src, name):
        """app.py 的 __main__ 段里名为 name 的调用行号（ast 扫，注释里的解释语不算）"""
        hits = []
        for node in ast.parse(src).body:
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
                continue
            left = node.test.left
            if not (isinstance(left, ast.Name) and left.id == "__name__"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    label = (func.id if isinstance(func, ast.Name)
                             else getattr(func, "attr", ""))
                    if label == name:
                        hits.append(sub.lineno)
        return hits

    # ---- ① 开机不再起调度线程，模块与两个函数仍在 ----
    calls = _startup_calls_named(_read("app.py"), "start_scheduler")
    assert calls == [], (
        f"app.py 的 __main__ 又调用 start_scheduler（行 {calls}）：每日 08:00／20:00 的"
        "定时生成被接回来了，spec §3.3 裁定 2（老版生成停用）破坏")
    scheduler_src = _read("scheduler.py")
    assert ("def run_scheduler" in scheduler_src
            and "def start_scheduler" in scheduler_src), (
        "scheduler.py 里的两个函数不见了：第 7 步只删那一行调用，模块与函数原样保留")

    # ---- ② 速览页那两枚生成按钮：无绑定 + 不可点，两枚刷新还在 ----
    factory_src = _read("ui", "app_factory.py")
    for btn in ("generate_plan_btn", "generate_review_btn"):
        bound = [ln.strip() for ln in factory_src.splitlines() if btn + ".click" in ln]
        assert not bound, f"旧版那枚 {btn} 又被接上了点击绑定：{bound}"
        decl = [ln.strip() for ln in factory_src.splitlines()
                if ln.strip().startswith(btn + " = gr.Button")]
        assert len(decl) == 1, f"{btn} 的声明应只有一行，读到 {len(decl)} 行：{decl}"
        assert "interactive=False" in decl[0], (
            f"{btn} 声明处没有 interactive=False，按钮还点得动：{decl[0]}")
    assert "refresh_plan_btn.click(fn=_app_module.refresh_plan" in factory_src, (
        "速览页「🔄 刷新」计划被拆了：判据 8 要的是历史仍可读，不是把那一页拆掉")
    assert "refresh_review_btn.click(fn=_app_module.refresh_review" in factory_src, (
        "速览页「🔄 刷新」复盘被拆了：同上")

    # ---- ③ 历史读得到 + 预览不产新行 ----
    conn = get_connection()
    try:
        rows_before = [tuple(r) for r in conn.execute(
            "SELECT plan_date, content FROM daily_plans ORDER BY rowid").fetchall()]
    finally:
        conn.close()
    today = datetime.now().strftime("%Y-%m-%d")
    assert all(d != today for d, _ in rows_before), (
        f"前置没立起来：临时库今天的 daily_plans 已有行 {rows_before}")

    history_text = "旧版历史计划：这一行只用来验「读得到」"
    DailyPlanner.save_plan(today, history_text)
    try:
        got = lc.refresh_plan()
        assert got == history_text, f"refresh_plan 读不到刚写进去的那一行历史：{got!r}"

        gm = GoalManager()
        g = _make_goal(gm, "老版停用判据目标")
        _add_task(gm, g.goal_id, "老版停用判据任务", priority=1)
        restore = _patch_chat_sync(lambda messages, **kw: "not-a-json")
        try:
            out = lc.do_generate_daily_plan()
        finally:
            restore()
        assert isinstance(out, tuple) and len(out) == 3, (
            f"do_generate_daily_plan 的三元组契约变了：{type(out)}")
        conn = get_connection()
        try:
            rows_after = [tuple(r) for r in conn.execute(
                "SELECT plan_date, content FROM daily_plans ORDER BY rowid").fetchall()]
        finally:
            conn.close()
        assert rows_after == rows_before + [(today, history_text)], (
            "预览生成往 daily_plans 写了新行（老版生成没真停用）："
            f"改前 {rows_before} 改后 {rows_after}")
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM daily_plans WHERE plan_date = ?", (today,))
            conn.commit()
        finally:
            conn.close()
    cleared = lc.refresh_plan()
    assert cleared == "暂无计划", f"清掉历史行后应读到占位文案，实际：{cleared!r}"

    # ---- ④ §6.4 那道护栏确实改了口径 ----
    skeleton_src = _read("tests", "test_skeleton.py")
    assert "不再注册每日生成任务" in skeleton_src, (
        "spec §6.4 那道护栏没改：tests/test_skeleton.py 仍按「注册 08:00／20:00」断言，"
        "本批一落地它必红")
    assert 'sorted(times) == ["08:00", "20:00"]' not in skeleton_src, (
        "旧断言串还留在 tests/test_skeleton.py 里（口径没换干净）")
    assert "def test_scheduler_registers_two_daily_jobs" not in skeleton_src, (
        "旧函数名还挂在 tests/test_skeleton.py 上（改判只改了文字、没改这条用例）")
    assert ("def test_daily_generation_not_registered_on_startup" in skeleton_src
            and "    test_daily_generation_not_registered_on_startup()" in skeleton_src), (
        "新口径用例没写进文件、或写进了却没接进 __main__ 清单（运行器数 [OK] 行，"
        "漏一行＝这条从没跑过却报绿）")
    print("[OK] old plan generation disabled, history readable")


# ---------- 应用今日计划 ----------

def test_apply_daily_plan_writes_planned_date():
    """应用计划：建议任务的 planned_date 被写为今天；返回统计正确"""
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")

    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "应用计划目标")
    tid1 = _add_task(gm, g.goal_id, "应用任务一", priority=1)
    tid2 = _add_task(gm, g.goal_id, "应用任务二", priority=2)

    plan = [
        {"task_id": tid1, "goal_id": g.goal_id, "task_title": "应用任务一",
         "goal_title": g.title, "estimated_hours": 2.0, "reason": "r"},
        {"task_id": tid2, "goal_id": g.goal_id, "task_title": "应用任务二",
         "goal_title": g.title, "estimated_hours": 2.0, "reason": "r"},
    ]
    result = agent.apply_daily_plan(plan)
    assert result["ok"] and result["applied"] == 2
    assert str(today) in result["message"], "返回消息应说明计划日期"
    dates = {t["id"]: t["planned_date"] for t in gm.get_tasks(g.goal_id)}
    assert dates[tid1] == today and dates[tid2] == today, "两个任务都应标记为今天"
    print("[OK] apply daily plan writes planned date")


def test_apply_overwrites_old_and_skips_missing():
    """应用计划幂等覆盖旧标记；已删除任务计入 skipped；空计划拒绝"""
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")

    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "覆盖标记目标")
    tid = _add_task(gm, g.goal_id, "曾被安排到过去", priority=1)
    gm.set_task_planned_date(tid, "2020-01-01")

    result = agent.apply_daily_plan([{"task_id": tid, "reason": ""}])
    assert result["ok"] and result["applied"] == 1
    assert gm.get_tasks(g.goal_id)[0]["planned_date"] == today, "旧标记应被覆盖为今天"

    # 已删除的任务：不计入 applied，进入 skipped
    dead_tid = _add_task(gm, g.goal_id, "将被删除的任务", priority=2)
    conn = get_connection()
    conn.execute("DELETE FROM goal_tasks WHERE id = ?", (dead_tid,))
    conn.commit()
    conn.close()
    result2 = agent.apply_daily_plan([{"task_id": dead_tid, "reason": ""}])
    assert not result2["ok"] and result2["applied"] == 0
    assert any("不存在" in s for s in result2["skipped"])

    # 空计划 / 非法结构：安全拒绝
    empty = agent.apply_daily_plan([])
    assert not empty["ok"] and empty["applied"] == 0
    junk = agent.apply_daily_plan(["bad", {"no_task_id": 1}])
    assert not junk["ok"] and len(junk["skipped"]) == 2
    single = agent.apply_daily_plan({"task_id": tid})  # 容错：误传单条字典
    assert single["ok"] and single["applied"] == 1
    print("[OK] apply overwrites old and skips missing")


def test_ui_callback_helpers_contract():
    """app 层回调契约：do_generate/do_apply 返回元组长度与 outputs 匹配且异常安全"""
    # 直接复用 app.py 中的回调函数做离线契约验证：
    # do_generate_daily_plan → (status, display, state)；do_apply_daily_plan → (status, state)
    from app import do_generate_daily_plan, do_apply_daily_plan  # noqa: E402

    # LLM 调用必须 mock（离线）：无论库中有无待办，回调都应返回合法三元组
    raise_error = lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("离线测试"))
    restore = _patch_chat_sync(raise_error)
    try:
        results_gen = do_generate_daily_plan()
    finally:
        restore()
    assert isinstance(results_gen, tuple) and len(results_gen) == 3, \
        "do_generate_daily_plan 必须返回三元组以匹配 outputs"
    status_msg, display_md, state = results_gen
    assert isinstance(status_msg, str) and isinstance(display_md, str)
    assert state is None or isinstance(state, list)

    # 未先生成计划就点击应用 → 友好提示而非崩溃
    results_apply_none = do_apply_daily_plan(None)
    assert isinstance(results_apply_none, tuple) and len(results_apply_none) == 2
    assert "请先" in results_apply_none[0]

    # 正常应用链路：State 置 None 防止重复提交
    gm = GoalManager()
    g = _make_goal(gm, "回调契约目标")
    tid = _add_task(gm, g.goal_id, "回调契约任务", priority=1)
    msg, new_state = do_apply_daily_plan([{"task_id": tid, "reason": ""}])
    assert "已将" in msg
    assert new_state is None, "应用后 State 应置 None 防止重复写库"
    print("[OK] ui callback helpers contract")


def test_generate_auto_executable_flag():
    """v3.7.5 可自动执行标记：LLM 给出合法 auto 时以其为准；缺失时按关键词本地判断"""
    gm = GoalManager()
    agent = DailyPlannerAgent(gm)
    g = _make_goal(gm, "自动标记目标")
    tid_search = _add_task(gm, g.goal_id, "搜索注意力机制资料", priority=1)
    tid_offline = _add_task(gm, g.goal_id, "线下参加读书会", priority=2)

    # 场景一：LLM 明确给出 auto 布尔字段 → 以 LLM 为准
    fake_response = ('{"summary":"","advice":"","plan":['
                     '{"task_id":%d,"reason":"r","auto":true},'
                     '{"task_id":%d,"reason":"r","auto":false}]}' % (tid_search, tid_offline))
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        result = agent.generate_daily_plan()
    finally:
        restore()
    by_id = {p["task_id"]: p for p in result["plan"]}
    assert by_id[tid_search]["auto_executable"] is True
    assert by_id[tid_offline]["auto_executable"] is False
    assert "⚡ 可自动" in result["display"], "可自动任务应在展示区带 ⚡ 标记"

    # 场景二：LLM 未给 auto → 本地关键词规则兜底（含"搜索"→True；无关键词→False）
    fake_response2 = ('{"summary":"","advice":"","plan":['
                      '{"task_id":%d,"reason":"r"},'
                      '{"task_id":%d,"reason":"r"}]}' % (tid_search, tid_offline))
    restore2 = _patch_chat_sync(lambda messages, **kw: fake_response2)
    try:
        result2 = agent.generate_daily_plan()
    finally:
        restore2()
    by_id2 = {p["task_id"]: p for p in result2["plan"]}
    assert by_id2[tid_search]["auto_executable"] is True, "标题含'搜索'应被本地规则标记"
    assert by_id2[tid_offline]["auto_executable"] is False, "默认应保守标记为不可自动"
    print("[OK] generate auto executable flag")


def test_cleanup():
    """清理测试数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== daily planning test ===\n")
    setup()
    test_migration_planned_date_column()
    test_set_task_planned_date_roundtrip()
    test_estimate_task_hours()
    test_collect_candidates_order_and_filter()
    test_generate_no_pending_tasks()
    test_generate_with_mock_llm()
    test_generate_llm_filters_invalid_entries()
    test_generate_fallback_when_llm_down()
    test_generate_overload_note_in_display()
    test_generate_includes_experiences_in_prompt()
    test_daily_plan_missing_daily_time_hint_and_override()
    test_old_plan_generation_disabled_history_readable()
    test_apply_daily_plan_writes_planned_date()
    test_apply_overwrites_old_and_skips_missing()
    test_generate_auto_executable_flag()
    test_ui_callback_helpers_contract()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
