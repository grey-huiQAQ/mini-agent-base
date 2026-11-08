# -*- coding: utf-8 -*-
"""ui.controllers.goals_controller —— 目标管理 / Planner / Intent

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from agents.intent_agent import intent_agent, build_text_from_history
from datetime import datetime
from agents.planner_agent import planner_agent
from core.goal_manager import TaskGenerationError

from ui.controllers._shared import (
    goal_manager,
    runtime,
)




def refresh_goal_list():
    """刷新目标下拉列表
    Gradio Dropdown 元组格式: (显示名, 返回值)
    显示目标标题，返回 goal_id
    """
    goals = goal_manager.list_goals()
    # 格式: (显示名=title, 返回值=goal_id)
    choices = [(g.title, g.goal_id) for g in goals]
    default_value = choices[0][1] if choices else None
    return gr.update(choices=choices, value=default_value)


def create_new_goal(title, desc):
    # 处理 Gradio 可能返回 None 的情况
    title = title or ""
    desc = desc or ""
    if not title.strip():
        # 获取当前目标列表用于更新下拉框
        goals = goal_manager.list_goals()
        choices = [(g.title, g.goal_id) for g in goals]
        # 保持当前选中值不变，或选择第一个
        default_value = choices[0][1] if choices else None
        return "⚠️ 请输入目标标题", gr.update(choices=choices, value=default_value)
    goal = goal_manager.create_goal(title.strip(), desc.strip())
    # 创建成功后刷新目标列表，并选中新创建的目标
    goals = goal_manager.list_goals()
    choices = [(g.title, g.goal_id) for g in goals]
    return f"✅ 已创建目标：{goal.title}", gr.update(choices=choices, value=goal.goal_id)


def select_goal(goal_id, current_session):
    """选择目标并显示目标标题"""
    if goal_id:
        runtime.set_active_goal(current_session, goal_id)
        # 获取目标信息显示标题
        goal = goal_manager.get_goal(goal_id)
        if goal:
            return f"当前目标：{goal.title}"
        return f"当前目标：{goal_id}"
    return "未选择目标"


def refresh_goal_detail(goal_id):
    """刷新目标详情展示区：显示目标描述和任务列表"""
    if not goal_id:
        return "请先选择一个目标", [], []
    goal = goal_manager.get_goal(goal_id)
    if not goal:
        return "⚠️ 目标不存在", [], []

    # 构建目标详情 Markdown
    status_map = {"active": "🟢 进行中", "completed": "🏁 已完成", "archived": "📦 已归档"}
    detail = f"**🎯 {goal.title}**\n"
    detail += f"状态：{status_map.get(goal.status, goal.status)}\n"
    if goal.description:
        detail += f"描述：{goal.description}\n"
    detail += f"创建时间：{goal.created_at}\n"
    detail += f"更新时间：{goal.updated_at}\n"

    # 获取统计信息
    try:
        stats = goal_manager.get_goal_stats(goal_id)
    except Exception:
        stats = {}

    if stats:
        # 构建进度条（10格，每格10%）
        rate = stats.get("completion_rate", 0)
        filled = int(rate / 10)
        progress_bar = "█" * filled + "░" * (10 - filled)

        detail += f"\n**📊 目标统计**\n"
        detail += f"- 任务进度：{progress_bar} {rate}%"
        detail += f"（{stats.get('completed_tasks', 0)}/{stats.get('total_tasks', 0)}）\n"
        detail += f"- 待办任务：{stats.get('pending_tasks', 0)} 个\n"
        detail += f"- 笔记数量：{stats.get('notes_count', 0)} 篇\n"
        detail += f"- 文件数量：{stats.get('files_count', 0)} 个\n"

    # 获取学习记录统计
    try:
        learning_stats = goal_manager.get_learning_stats(goal_id)
    except Exception:
        learning_stats = {}

    if learning_stats and learning_stats.get("total_records", 0) > 0:
        detail += f"\n**📚 学习记录统计**\n"
        detail += f"- 记录总数：{learning_stats.get('total_records', 0)} 条\n"
        detail += f"- 平均掌握度：{learning_stats.get('average_mastery', 0)}\n"
        recent = learning_stats.get("recent_topics", [])
        if recent:
            detail += f"- 最近学习：{', '.join(recent)}\n"
        weak = learning_stats.get("weak_topics", [])
        if weak:
            detail += f"- 薄弱主题：{', '.join(weak)}\n"

    # 获取任务列表（按里程碑分组展示；无里程碑的旧任务平铺在"未分组"下，向后兼容）
    tasks = goal_manager.get_tasks(goal_id)
    priority_map = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}
    status_icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅"}

    def _render_task(t):
        """渲染单行任务 Markdown"""
        icon = status_icon.get(t.get("status", "pending"), "⬜")
        pri = priority_map.get(t.get("priority", 1), "⚪")
        source = t.get("source", "manual")
        source_tag = " 🤖" if source == "ai_generated" else " ✍️"
        # 计划日期标记（每日规划功能写入）：旧数据 planned_date 为 NULL 不显示，向后兼容
        planned = t.get("planned_date") or ""
        if planned == datetime.now().strftime("%Y-%m-%d"):
            planned_tag = " 📅 今天"
        elif planned:
            planned_tag = f" 📅 {planned}"
        else:
            planned_tag = ""
        # 首页准入标记（P7 阶段二 组 5 子批 B，2026-10-19）：`is_home` 非零＝这条在首页
        # 那三格里。为什么要在列表里印出来而不是只让按钮说：那两个按钮是**两枚**、
        # 不是一枚切换，点了哪一枚就写哪个值，而人决定点哪一枚之前得先知道现在是什么值
        # ——不印出来，这一屏就要求人记住自己上次点了什么（本席自决，设计席可驳）。
        # 判真值与首页读侧同一口径（非零即已准入，见 `goal_manager.set_task_home`）。
        home_tag = " 🏠" if int(t.get("is_home") or 0) != 0 else ""
        line = f"- {icon} **{t['title']}** [{pri}]{source_tag}{planned_tag}{home_tag}\n"
        if t.get("description"):
            line += f"  {t['description']}\n"
        return line

    if tasks:
        detail += f"\n**📋 任务列表（{len(tasks)} 个）**\n"
        milestones = goal_manager.get_milestones(goal_id) or []
        grouped = {m["id"]: [] for m in milestones}
        ungrouped = []
        for t in tasks:
            mid = t.get("milestone_id")
            if mid in grouped:
                grouped[mid].append(t)
            else:
                # 旧数据 / 未挂里程碑的任务
                ungrouped.append(t)

        # 按顺序渲染各里程碑及其任务
        for m in milestones:
            ms_icon = status_icon.get(m.get("status", "pending"), "⬜")
            weeks = f"（约 {m['duration_weeks']} 周）" if m.get("duration_weeks") else ""
            detail += f"\n**🚩 {m['title']}** {ms_icon}{weeks}\n"
            if m.get("description"):
                detail += f"  {m['description']}\n"
            ms_tasks = grouped.get(m["id"], [])
            detail += ("".join(_render_task(t) for t in ms_tasks)
                       if ms_tasks else "- （暂无任务）\n")

        # 无里程碑的任务（含历史数据）
        if ungrouped:
            detail += "\n**📌 未分组任务**\n"
            detail += "".join(_render_task(t) for t in ungrouped)
    else:
        detail += "\n**📋 任务列表**\n- 暂无任务（点击'生成任务'按钮自动创建）"

    # 构建未完成任务列表（供下拉框使用）
    # 格式: (显示名=title, 返回值=id字符串)
    pending_tasks = [t for t in tasks if t.get("status", "pending") != "done"]
    task_choices = [(t["title"], str(t["id"])) for t in pending_tasks]

    return detail, task_choices, gr.update(choices=task_choices, value=None)


def do_generate_tasks(goal_id):
    """调用 LLM 自动生成任务列表；失败时把**原因**回显（P7 批次 9-③）

    `generate_tasks_from_goal` 现在把三处静默失败改成抛 `TaskGenerationError`，
    这里接住并把原因写进状态栏——原实现只能退回"任务生成失败，请稍后重试"，
    对点按钮的人来说那句话等于没有信息（原因原先只 print 在终端里）。
    """
    if not goal_id:
        return "⚠️ 请先选择一个目标"
    try:
        tasks = goal_manager.generate_tasks_from_goal(goal_id)
    except TaskGenerationError as e:
        return f"⚠️ 任务生成失败：{e}"
    except Exception as e:
        return f"⚠️ 任务生成失败：{e}"
    if tasks:
        return f"✅ 已生成 {len(tasks)} 个任务"
    return "⚠️ 任务生成失败：模型没有返回可用任务"


def do_start_task(task_id, goal_id):
    """把选中的待办从 pending 置为「进行中」（P7 阶段二 组 3，2026-10-18）

    形状逐字镜像下面的 `do_complete_task`：读同一个下拉的选中值（字符串 id）、返回同样的
    两元组（状态文案＋刷出来的详情 Markdown），写路径只走既有的
    `goal_manager.update_task_status(task_id, "in_progress")`——不新增 SQL、不动
    `goal_tasks` 的表结构、不碰待办的准入集合。

    与「标记完成」一样**不做状态前置校验**：设计席 组 3 的边界只做 pending → in_progress
    这一格转换（「放弃／暂停」本批不做）。所以对着一条已经是 `in_progress` 的任务再点一次，
    落库的是同一个值——幂等，界面上看不出差别。要不要拒绝这种点击属「准入」那一层的口径，
    本批按原文未动，已登记在执行席回报的待拍板项里。
    """
    if not task_id:
        return "⚠️ 请先选择要开始的任务", ""
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        return "⚠️ 无效的任务 ID", ""
    goal_manager.update_task_status(task_id_int, "in_progress")
    return "▶️ 任务已标记进行中", refresh_goal_detail(goal_id)[0]


def do_complete_task(task_id, goal_id):
    """标记指定任务为已完成"""
    if not task_id:
        return "⚠️ 请先选择要完成的任务", ""
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        return "⚠️ 无效的任务 ID", ""
    goal_manager.update_task_status(task_id_int, "done")
    return "✅ 任务已标记完成", refresh_goal_detail(goal_id)[0]


def _set_task_home(task_id, goal_id, on_home):
    """两个公开回调共用的那一段：校验 → 写 `is_home` → 回状态文案＋刷新的详情

    形状照上面的 `do_start_task`／`do_complete_task`（读同一个下拉的选中值、返回同样的
    两元组），差别只有两处，且都是故意的：
    ① 写路径走 `goal_manager.set_task_home`，它**返回 bool**，所以这里能区分
       「写成了」与「库里没有这条」——那两个按钮要是无条件回一句「已完成」，
       在人删掉任务之后再点一次就成了一句假话；
    ② 两个方向共用一段代码而不是各写一遍：本项目在「两处手工保持一致」上翻过多次车，
       而这一对的差异只有那一列写 1 还是写 0。
    """
    action = "加入首页" if on_home else "移出首页"
    if not task_id:
        return f"⚠️ 请先选择要{action}的任务", ""
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        return "⚠️ 无效的任务 ID", ""
    if not goal_manager.set_task_home(task_id_int, on_home):
        return (f"⚠️ 待办 #{task_id_int} 没找到，「{action}」没写成"
                "（那条大概刚被删掉）：一行都没改",
                refresh_goal_detail(goal_id)[0])
    if on_home:
        # 状态串里点名「🎯 目标」页之外的那一屏：人要能在首页找到刚才那一下的效果。
        return (f"🏠 待办 #{task_id_int} 已加入首页（回「🏠 首页」刷新即可看到；"
                "能不能真的排进前三，还要看那一格只有三个位置）",
                refresh_goal_detail(goal_id)[0])
    return (f"➖ 待办 #{task_id_int} 已移出首页：它仍是一条正常待办，"
            "状态没改、行没删，在「🎯 目标」的列表里一字不变",
            refresh_goal_detail(goal_id)[0])


def do_add_task_home(task_id, goal_id):
    """把选中的待办**准入首页**（写 `goal_tasks.is_home = 1`；P7 阶段二 组 5 子批 B，2026-10-19）

    只写这一列：不改 `status`（那条不是「开始」也不是「完成」）、不碰其它列、
    不动 `current_focus` 的任何一行（锚点 §四 组 5 第 4 条：子批 B 只加列、不改既有含义）。

    与 `do_start_task` 一样**不做前置校验**：对一条已经在首页的任务再点一次，落库的是
    同一个值——幂等，界面上回同一句「已加入首页」。要不要拒绝这种点击属「准入」那一层的
    口径，本批按指派原文未动。
    新生成的任务（手动 add_task／`do_generate_tasks` 那批 AI 生成）**默认不在首页**：
    DDL 的 `INTEGER DEFAULT 0` 就是「准入制」这四个字的落库形状，只有存量行被一次性
    迁移回填成 1（理由与「回填只跑一次」记在 `memory/database.py` 那一段注释里）。
    """
    return _set_task_home(task_id, goal_id, True)


def do_remove_task_home(task_id, goal_id):
    """把选中的待办**移出首页**（写 `goal_tasks.is_home = 0`；与上一条镜像）

    ⚠️ 这一格是「离开主视图」，不是「找不到」：移出之后那条任务在「🎯 目标」页的列表里
    照旧出现、状态照旧、行照旧在库（口径见锚点 §四「强过滤、弱删除」与
    `home_controller` 文件头第 9 条）。所以这里没有「确认移出」那道闸——它不需要：
    最坏情况是人点错了，再点左边那枚就回来了，一次点击的代价，不值得一道对话框。
    """
    return _set_task_home(task_id, goal_id, False)


def do_mark_goal_completed(goal_id):
    """标记目标为已完成"""
    if not goal_id:
        return "⚠️ 请先选择一个目标", refresh_goal_list(), ""
    goal_manager.mark_goal_completed(goal_id)
    return "✅ 目标已标记完成", refresh_goal_list(), refresh_goal_detail(goal_id)[0]


def do_mark_goal_archived(goal_id):
    """标记目标为已归档"""
    if not goal_id:
        return "⚠️ 请先选择一个目标", refresh_goal_list(), ""
    goal_manager.mark_goal_archived(goal_id)
    return "✅ 目标已归档", refresh_goal_list(), refresh_goal_detail(goal_id)[0]


def do_generate_staged_plan(goal_id, new_title, new_desc, weeks):
    """生成分阶段学习计划（里程碑 + 任务），仅展示不写库"""
    try:
        result = planner_agent.generate_staged_plan(
            goal_id=goal_id,
            new_goal_title=new_title or "",
            new_goal_desc=new_desc or "",
            total_weeks=weeks,
        )
        # 若自动创建了新目标，刷新目标下拉框并选中新目标
        # （纳入 try：下拉刷新失败时同样走异常分支，不向 UI 抛原始错误）
        goals = goal_manager.list_goals()
        choices = [(g.title, g.goal_id) for g in goals]
        dropdown_update = gr.update(choices=choices, value=result["plan"].goal_id if result["ok"] else None)
        return result["message"], result["display"], result["plan"], dropdown_update
    except Exception as e:
        # 安全默认值：清空下拉选项，避免 UI 弹出原始异常
        return f"❌ 生成计划失败：{e}", "", None, gr.update(choices=[])


def do_apply_staged_plan(plan_state):
    """将确认后的分阶段计划写入目标系统

    返回 (消息, staged_plan_state 新值)：写入成功（ok）后把 State 置 None，
    防止同一计划被重复点击而重复写库；空 State / 异常 / 未写入分支保留原值，
    便于重新生成或重试（模式同 do_apply_daily_plan 的 (msg, None) 返回）。
    """
    if not plan_state:
        return "⚠️ 请先生成计划再写入", plan_state
    try:
        result = planner_agent.apply_staged_plan(plan_state, replace_ai_pending=True)
    except Exception as e:
        return f"❌ 写入失败：{e}", plan_state
    if result.get("ok"):
        return result["message"], None
    return result["message"], plan_state


def do_analyze_conflicts():
    """多目标冲突分析（仅分析不写库）"""
    try:
        result = planner_agent.analyze_conflicts()
    except Exception as e:
        return f"❌ 冲突分析失败：{e}", "", []
    return result["message"], result["display"], result.get("suggestions") or []


def do_apply_conflicts(suggestions_state):
    """应用冲突建议：更新目标优先级/截止日期或归档目标

    返回 (消息, suggestions_state 新值)：确有建议被应用（ok）后把 State
    置 None，防止重复点击把同一批建议重复写库；空 State / 异常 / 无可应用
    建议分支保留原值（未写库，供继续查看分析或重试）。
    """
    if not suggestions_state:
        return "⚠️ 请先执行冲突分析", suggestions_state
    try:
        result = planner_agent.apply_conflict_suggestions(suggestions_state)
    except Exception as e:
        return f"❌ 应用建议失败：{e}", suggestions_state
    if result["ok"]:
        return f"✅ 已应用 {result['applied']} 条建议：" + "；".join(result["details"]), None
    return "⚠️ 没有可应用的建议", suggestions_state


def do_analyze_intent(chat_history, extra_text):
    """从当前会话最近对话（或手动输入文本）识别潜在目标意图（仅分析不写库）

    优先使用手动输入的补充文本；为空时自动提取聊天区最近的用户发言。
    """
    text = (extra_text or "").strip()
    if not text:
        text = build_text_from_history(chat_history)
    try:
        result = intent_agent.analyze_intent(text)
    except Exception as e:
        return f"❌ 意图识别失败：{e}", "", None
    return result["message"], result["display"], result.get("intents") or []


def do_apply_intent_suggestion(intents_state):
    """应用确认后的意图建议：创建新目标 / 关联既有目标（逐条尝试）

    返回 (消息, intent_state 新值)：确有建议被应用（ok）后把 State 置 None，
    防止重复点击重复创建目标/重复关联；空 State / 异常 / 无可应用建议分支
    保留原值（未写库，供继续查看分析或重试）。
    """
    if not intents_state:
        return "⚠️ 请先点击「🎯 识别意图」获取建议", intents_state
    try:
        result = intent_agent.apply_intents(intents_state)
    except Exception as e:
        return f"❌ 应用建议失败：{e}", intents_state
    lines = "\n".join(f"- {d}" for d in result["details"] if d)
    if result["ok"]:
        return f"✅ 已应用 {result['applied']} 条建议：\n{lines}", None
    return lines or "⚠️ 没有可应用的建议", intents_state
