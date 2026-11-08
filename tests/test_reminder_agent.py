# -*- coding: utf-8 -*-
"""
ReminderAgent 主动提醒模块测试（离线：时间通过参数注入，不依赖真实时钟）
覆盖：
- 时间解析：混合分隔符、补零规范化、升序去重、非法片段忽略、越界拒绝
- 设置读写：未设置用默认、保存后读回、清空关闭（OFF 哨兵）、非法输入不改原设置
- 今日任务采集：planned_date=今天且未完成；done 任务 / 其他日期 / 归档目标被过滤
- 提醒触发：到点生成消息并打印日志、无任务不打扰、消息含任务与目标名
- 去重逻辑：同一分钟仅触发一次、不同时间点可再次提醒、进程内状态隔离
- 通知生命周期：触发后持续显示、任务全部完成后自动撤下
- 异常安全：非法时间字符串返回安全默认值；UI 回调契约与异常兜底
"""
import io
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from core.goal_manager import GoalManager
from memory.database import init_db, get_connection
from memory.profile_manager import ProfileManager
from agents.reminder_agent import (
    ReminderAgent,
    parse_reminder_times,
    format_reminder_message,
    DEFAULT_REMINDER_TIMES,
    REMINDER_TIMES_KEY,
)

TODAY = datetime.now().strftime("%Y-%m-%d")

# 本测试文件创建过的目标 ID，cleanup 时精确清理，不触碰其他数据
_created_goal_ids = []


def setup():
    """初始化数据库并清理提醒时间档案项，保证各用例从干净状态开始"""
    init_db()
    _clear_profile()


def _clear_profile():
    """删除本模块用到的档案键，避免影响其他测试对默认值的假设"""
    try:
        ProfileManager.delete(REMINDER_TIMES_KEY)
    except Exception:
        pass


def _make_goal(gm: GoalManager, title: str, desc: str = "", **kwargs):
    """创建目标并记录 ID，供 cleanup 精确删除（先删同名历史残留，幂等）"""
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


def _add_task(gm: GoalManager, goal_id: str, title: str,
              planned_date: str = TODAY, **kwargs) -> int:
    """添加任务并默认标记为今天执行（模拟「应用计划」后的状态），返回任务 ID"""
    gm.add_task(goal_id, title, **kwargs)
    tid = [t for t in gm.get_tasks(goal_id) if t["title"] == title][0]["id"]
    if planned_date:
        gm.set_task_planned_date(tid, planned_date)
    return tid


def _archive_all_active_temporarily():
    """临时把库中所有活跃目标置为 archived 以隔离出"空目标"场景

    直接 SQL 更新 status 不触碰 updated_at；返回恢复函数放 finally 中调用，
    保证测试结束后所有目标状态原样还原。
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


def _capture_check(agent: ReminderAgent, time_str):
    """捕获 stdout 执行一次 check_reminders，返回 (结果字典, 日志文本)"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = agent.check_reminders(time_str)
    return result, buf.getvalue()


def _cleanup():
    """清理本测试产生的目标（含任务）与档案键"""
    conn = get_connection()
    for gid in _created_goal_ids:
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()
    _created_goal_ids.clear()
    _clear_profile()


# ---------- 时间解析 ----------

def test_parse_reminder_times():
    """解析规范化：混合分隔符、一位小时补零、升序排列、重复去重、非法忽略"""
    assert parse_reminder_times("09:00,14:00") == ["09:00", "14:00"]
    assert parse_reminder_times("9:05，20:00；8:00") == ["08:00", "09:05", "20:00"]
    assert parse_reminder_times("14:00 09:00 14:00") == ["09:00", "14:00"], "空白分隔且应去重"
    assert parse_reminder_times("abc,09:00,,xyz") == ["09:00"], "非法片段应被忽略"
    assert parse_reminder_times("25:99") == [], "越界时间（25:99）非法"
    assert parse_reminder_times("") == [] and parse_reminder_times(None) == []
    assert parse_reminder_times("  10:30  ") == ["10:30"], "容忍首尾空白"
    print("[OK] parse reminder times")


def test_default_when_not_set():
    """档案未设置 reminder_times 时回退默认 ["09:00", "14:00"]"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    assert ProfileManager.get(REMINDER_TIMES_KEY) is None
    assert agent.get_reminder_times() == list(DEFAULT_REMINDER_TIMES) == ["09:00", "14:00"]

    # 档案存了完全非法的内容时同样回退默认（而不是崩溃或清空）
    ProfileManager.set(REMINDER_TIMES_KEY, "garbage!!")
    assert agent.get_reminder_times() == list(DEFAULT_REMINDER_TIMES)
    print("[OK] default when not set")


def test_set_and_reload_reminder_times():
    """保存合法时间列表后可从档案正确读回（跨实例持久化验证）"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    result = agent.set_reminder_times("9:30, 21:15")
    assert result["ok"] and result["times"] == ["09:30", "21:15"]
    assert "09:30" in result["message"] and "21:15" in result["message"]

    # 新实例（模拟重启进程）读取同一档案值
    assert ReminderAgent(gm).get_reminder_times() == ["09:30", "21:15"]

    # 单一时间也支持
    assert agent.set_reminder_times("07:05")["times"] == ["07:05"]
    print("[OK] set and reload reminder times")


def test_empty_input_disables_reminder():
    """清空输入框保存 → 写 OFF 哨兵关闭提醒；get_reminder_times 返回 []"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        g = _make_goal(gm, "关闭提醒场景目标")
        _add_task(gm, g.goal_id, "待提醒的任务")
        ProfileManager.set(REMINDER_TIMES_KEY, "09:00")

        # 先到点触发，确认存在活动通知
        r1, log1 = _capture_check(agent, "09:00")
        assert r1["reminder"] and "待提醒的任务" in r1["reminder"] and log1

        # 清空输入框保存 → 关闭
        off = agent.set_reminder_times("")
        assert off["ok"] and off["times"] == []
        assert agent.get_reminder_times() == [], "OFF 哨兵应使提醒时间列表为空"
        r2, _ = _capture_check(agent, "09:01")
        assert r2["ok"] and r2["reminder"] == "", "关闭后活动通知应被撤下"
    finally:
        iso()
    print("[OK] empty input disables reminder")


def test_invalid_input_keeps_old_setting():
    """输入全部非法时保存失败（ok=False）、档案原值不变、给出修正提示"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    assert agent.set_reminder_times("10:00,11:00")["ok"]
    bad = agent.set_reminder_times("abc-def")
    assert not bad["ok"], "纯非法输入不应成功"
    assert "无法识别" in bad["message"]
    assert agent.get_reminder_times() == ["10:00", "11:00"], "原设置必须保持不变"
    print("[OK] invalid input keeps old setting")


# ---------- 今日任务采集 ----------

def test_collect_today_pending_filters():
    """采集过滤：done 任务 / 非今天的 planned_date / 归档目标均不计入；按优先级排序"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        g_active = _make_goal(gm, "采集目标A")
        g_archived = _make_goal(gm, "采集目标B已归档")
        gm.archive_goal(g_archived.goal_id)

        t_p2 = _add_task(gm, g_active.goal_id, "P2今日任务", priority=2)
        t_p1 = _add_task(gm, g_active.goal_id, "P1今日任务", priority=1)
        tid_done = _add_task(gm, g_active.goal_id, "已完成任务", priority=1)
        gm.update_task_status(tid_done, "done")
        _add_task(gm, g_active.goal_id, "明天的任务", priority=1, planned_date="2099-01-01")
        _add_task(gm, g_active.goal_id, "从未排期的任务", priority=1, planned_date="")
        _add_task(gm, g_archived.goal_id, "归档目标的今日任务", priority=1)

        tasks = agent.get_today_pending_tasks()
    finally:
        iso()

    titles = [t["task_title"] for t in tasks]
    assert titles == ["P1今日任务", "P2今日任务"], f"应只剩两条且 P1 在前：{titles}"

    by_title = {t["task_title"]: t for t in tasks}
    assert by_title["P1今日任务"]["task_id"] == t_p1
    assert by_title["P2今日任务"]["task_id"] == t_p2
    item = by_title["P1今日任务"]
    assert item["goal_id"] == g_active.goal_id and item["goal_title"] == "采集目标A"
    assert item["priority"] == 1
    print("[OK] collect today pending filters")


def test_reminder_uses_shared_planned_query():
    """判据 §4.2 ③：提醒的今日集合 == core 共享查询的集合（同 id 集合，不留第二把尺）

    断的是「同 id 集合」而不是文案：这一条要钉的就是**没有第二份筛选实现**。
    负控＝把 `get_today_pending_tasks` 改回手写两重循环 ⇒ 本条红。
    ⚠️ 「退回手写」在正常数据上未必当场不等，所以夹具专门造了两档能分辨的形状：
    ① 挂在**已归档目标**下且 planned_date=今天的一条——共享查询按 active 目标枚举，把它
       排除在外，而「只比 planned_date」的退化写法会算进来；
    ② `is_home=1`、今天、未完成的一条——共享查询**不看** is_home（矛盾 1 的修正点），
       手写那份若把首页准入当成条件就会漏掉它。
    另配「今天但已完成」与「从未排期」两条作反面对照。
    """
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        g_active = _make_goal(gm, "共享尺活跃目标")
        g_archived = _make_goal(gm, "共享尺归档目标")
        gm.archive_goal(g_archived.goal_id)

        t_plain = _add_task(gm, g_active.goal_id, "共享尺今天要提醒", priority=1)
        t_home = _add_task(gm, g_active.goal_id, "共享尺今天已准入", priority=2)
        gm.set_task_home(t_home, True)
        tid_done = _add_task(gm, g_active.goal_id, "共享尺今天已完成", priority=1)
        gm.update_task_status(tid_done, "done")
        _add_task(gm, g_active.goal_id, "共享尺没排期", priority=1, planned_date="")
        _add_task(gm, g_archived.goal_id, "共享尺归档目标的今日任务", priority=1)

        reminded = agent.get_today_pending_tasks()
        shared = gm.get_tasks_planned_for(TODAY)
    finally:
        iso()

    assert shared, "夹具没能造出共享查询的行，下面那条集合相等是空的"
    reminded_ids = {int(t["task_id"]) for t in reminded}
    shared_ids = {int(r["id"]) for r in shared}
    assert reminded_ids == shared_ids, (
        f"提醒与共享查询的今日集合不等（说明还留着第二把尺）："
        f"reminder={sorted(reminded_ids)} shared={sorted(shared_ids)}")
    assert t_plain in reminded_ids and t_home in reminded_ids, (
        f"今日两条都该在集合里：{sorted(reminded_ids)}")
    assert tid_done not in reminded_ids, "已完成那条混进了今日集合"
    print("[OK] reminder set == shared planned query (same id set)")


# ---------- 提醒触发与去重 ----------

def test_trigger_generates_message_and_log():
    """到点且有今日任务：生成提醒消息（含任务名与目标名）、task_count 正确、打印日志"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        ProfileManager.set(REMINDER_TIMES_KEY, "09:00")  # 本用例独立的提醒时间
        g1 = _make_goal(gm, "触发目标一")
        g2 = _make_goal(gm, "触发目标二")
        _add_task(gm, g1.goal_id, "上午攻坚任务", priority=1)
        _add_task(gm, g2.goal_id, "下午收尾任务", priority=2)

        result, log = _capture_check(agent, "09:00")
    finally:
        iso()

    assert result["ok"] and result["task_count"] == 2
    msg = result["reminder"]
    assert "09:00" in msg and "2 个任务" in msg, f"消息应包含时间与数量：{msg}"
    assert "上午攻坚任务（触发目标一）" in msg.replace("**", ""), "消息应列出任务及所属目标"
    assert "下午收尾任务（触发目标二）" in msg
    assert "[ReminderAgent]" in log and "09:00" in log, "触发时应打印控制台日志"
    print("[OK] trigger generates message and log")


def test_no_tasks_at_reminder_time_stays_silent():
    """到点但今天没有任何计划任务：不产生提醒、不打印触发日志"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        ProfileManager.set(REMINDER_TIMES_KEY, "14:00")  # 本用例独立的提醒时间
        result, log = _capture_check(agent, "14:00")
    finally:
        iso()
    assert result["ok"] and result["reminder"] == "" and result["task_count"] == 0
    assert "[ReminderAgent] ⏰" not in log, "无任务时不应打印提醒日志"
    print("[OK] no tasks at reminder time stays silent")


def test_dedup_same_minute_single_fire():
    """同一分钟多次检查只触发一次日志；但活动通知持续显示不清空"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        ProfileManager.set(REMINDER_TIMES_KEY, "09:00")  # 本用例独立的提醒时间
        g = _make_goal(gm, "去重场景目标")
        _add_task(gm, g.goal_id, "去重观察任务")

        r1, log1 = _capture_check(agent, "09:00")
        r2, log2 = _capture_check(agent, "09:00")
        r3, log3 = _capture_check(agent, "09:00")
    finally:
        iso()

    assert r1["reminder"] and "去重观察任务" in r1["reminder"]
    assert log1.count("[ReminderAgent] ⏰") == 1, "首次应打印一次触发日志"
    assert log2.count("[ReminderAgent] ⏰") == 0, "同分钟第二次不应重复触发"
    assert log3.count("[ReminderAgent] ⏰") == 0, "同分钟第三次不应重复触发"
    assert r2["reminder"] and r3["reminder"], "通知区内容应保持显示"
    assert agent._last_reminded_minute[1] == "09:00", "去重标记应记录本次分钟"
    print("[OK] dedup same minute single fire")


def test_next_reminder_time_fires_again():
    """不同提醒时间点可再次触发：09:00 与 14:00 各打印一次，消息反映最新任务集"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        ProfileManager.set(REMINDER_TIMES_KEY, "09:00,14:00")  # 本用例的两个时间点
        g = _make_goal(gm, "多时段提醒目标")
        tid_a = _add_task(gm, g.goal_id, "早间任务")
        r1, log1 = _capture_check(agent, "09:00")
        gm.update_task_status(tid_a, "done")  # 用户完成早间任务
        tid_b = _add_task(gm, g.goal_id, "午后新任务")
        r2, log2 = _capture_check(agent, "14:00")
    finally:
        iso()

    assert log1.count("[ReminderAgent] ⏰") == 1
    assert log2.count("[ReminderAgent] ⏰") == 1, "第二个时间点应再次触发"
    assert "午后新任务" in r2["reminder"], "第二次提醒应反映最新任务列表"
    assert "早间任务" not in r2["reminder"], "已完成的早间任务不应出现在新提醒中"
    print("[OK] next reminder time fires again")


def test_notification_persists_then_auto_clears():
    """非提醒时刻活动通知持续展示；今日任务全部完成后自动撤下"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    iso = _archive_all_active_temporarily()
    try:
        ProfileManager.set(REMINDER_TIMES_KEY, "09:00")  # 本用例独立的提醒时间
        g = _make_goal(gm, "通知生命周期目标")
        tid = _add_task(gm, g.goal_id, "唯一任务")

        r_trigger, _ = _capture_check(agent, "09:00")
        # 09:01 非提醒时刻：任务未完成 → 通知仍在
        r_hold, _ = _capture_check(agent, "09:01")
        # 完成任务后再查 → 通知自动撤下
        gm.update_task_status(tid, "done")
        r_done, _ = _capture_check(agent, "09:02")
    finally:
        iso()

    assert r_trigger["reminder"]
    assert r_hold["reminder"] == r_trigger["reminder"], "非提醒时刻应保持上次通知"
    assert r_done["reminder"] == "", "任务全部完成后通知应自动撤下"
    assert r_done["task_count"] == 0
    print("[OK] notification persists then auto clears")


# ---------- 消息格式与异常安全 ----------

def test_format_message_folds_long_list():
    """任务超过 10 条时消息折叠总数，防止通知区过长"""
    tasks = [{"task_title": f"任务{i}", "goal_title": f"目标{i}"}
             for i in range(12)]
    msg = format_reminder_message("09:00", tasks)
    assert "12 个任务" in msg
    assert "…等共 12 个任务" in msg
    assert msg.count("\n- ") == 10 + 1, "正文只列前 10 条 + 1 行折叠说明"

    single = format_reminder_message("08:00",
                                     [{"task_title": "唯一", "goal_title": "单目标"}])
    assert "1 个任务" in single and "唯一" in single
    print("[OK] format message folds long list")


def test_invalid_time_string_safe_default():
    """非法时间字符串：返回安全默认值（ok=False），不向调用方抛异常"""
    gm = GoalManager()
    agent = ReminderAgent(gm)
    for bad in ("25:99", "abc", "09:00,10:00", ""):
        result, _ = _capture_check(agent, bad)
        assert result == {"ok": False, "reminder": "", "task_count": 0}, \
            f"非法输入 {bad!r} 应返回安全默认值：{result}"
    print("[OK] invalid time string safe default")


# ---------- UI 回调契约（复用 app.py 真实回调做离线验证） ----------

def test_ui_callback_helpers_contract():
    """app 层回调契约：返回元组结构与 outputs 匹配，异常安全"""
    from app import do_check_reminders, do_save_reminder_times, load_reminder_settings  # noqa: E402

    # 定时回调：任何情况下都返回字符串（当前真实时间几乎必然不匹配整点 → 空串或既有通知）
    notify = do_check_reminders()
    assert isinstance(notify, str), "do_check_reminders 必须返回字符串以更新 Markdown 组件"

    # 加载回调：未设置档案时回显默认时间并附说明
    _clear_profile()
    echo, note = load_reminder_settings()
    assert echo == ",".join(DEFAULT_REMINDER_TIMES), f"应回显默认提醒时间：{echo}"
    assert isinstance(note, str) and note

    # 保存回调：合法输入 → 状态提示 + 规范化回显；输入框内容被规范化
    status, echoed = do_save_reminder_times("8:15, 22:00")
    assert "✅" in status and echoed == "08:15,22:00"
    assert ProfileManager.get(REMINDER_TIMES_KEY) == "08:15,22:00"

    # 保存回调：非法输入 → 警告且原样回显输入便于修正
    status_bad, echoed_bad = do_save_reminder_times("bad-input")
    assert "⚠️" in status_bad and echoed_bad == "bad-input"
    assert ProfileManager.get(REMINDER_TIMES_KEY) == "08:15,22:00", "非法输入不应破坏原设置"

    # 保存回调：清空 → 关闭提醒
    status_off, echoed_off = do_save_reminder_times("")
    assert "⏸️" in status_off and echoed_off == ""
    _clear_profile()  # 清理档案，避免影响其他测试对默认值的假设
    print("[OK] ui callback helpers contract")


def test_cleanup():
    """清理测试数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== reminder agent test ===\n")
    setup()
    test_parse_reminder_times()
    test_default_when_not_set()
    test_set_and_reload_reminder_times()
    test_empty_input_disables_reminder()
    test_invalid_input_keeps_old_setting()
    test_collect_today_pending_filters()
    test_reminder_uses_shared_planned_query()
    test_trigger_generates_message_and_log()
    test_no_tasks_at_reminder_time_stays_silent()
    test_dedup_same_minute_single_fire()
    test_next_reminder_time_fires_again()
    test_notification_persists_then_auto_clears()
    test_format_message_folds_long_list()
    test_invalid_time_string_safe_default()
    test_ui_callback_helpers_contract()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
