# -*- coding: utf-8 -*-
"""ui.controllers.learning_controller —— 计划复盘 / Prompt / 复习 / 技能 / 提醒

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from memory.daily_planner import DailyPlanner
from memory.knowledge_graph import KnowledgeGraph
from memory import ChatStore, init_db, ProfileManager, LearningRecorder
from agents.prompt_evolution import evaluate_and_evolve, apply_new_version
from agents.daily_planning_agent import daily_planner_agent
from datetime import datetime
from agents.reminder_agent import reminder_agent

from ui.controllers._shared import (
    goal_manager,
    runtime,
)




def refresh_plan():
    today = datetime.now().strftime("%Y-%m-%d")
    plan = DailyPlanner.get_plan(today)
    return plan if plan else "暂无计划"


def refresh_review():
    today = datetime.now().strftime("%Y-%m-%d")
    review = DailyPlanner.get_review(today)
    return review if review else "暂无复盘"


def evaluate_prompt(task_name, score, feedback):
    if not task_name.strip():
        return "⚠️ 请输入任务名称", "", gr.update(visible=False)
    result = evaluate_and_evolve(task_name.strip(), score, feedback)
    if result["suggestion"]:
        return f"当前版本 v{result['current_version']}，建议改进如下：\n\n---\n\n{result['suggestion']}", result["suggestion"], gr.update(visible=True)
    else:
        return f"当前版本 v{result['current_version']} 评分良好，无需优化。", "", gr.update(visible=False)


def apply_prompt(task_name, suggestion):
    if not task_name.strip() or not suggestion:
        return "⚠️ 任务名称或建议为空"
    version = apply_new_version(task_name.strip(), suggestion)
    return f"✅ 已应用新版本 v{version}"


def refresh_review_recommendations(domain):
    """刷新复习推荐：根据掌握度和最后复习时间推荐需要复习的知识节点"""
    if not domain:
        domain = None
    try:
        nodes = KnowledgeGraph.get_review_recommendations(domain=domain, limit=5)
    except Exception as e:
        return f"⚠️ 获取推荐失败：{e}"

    if not nodes:
        return "暂无需要复习的知识节点"

    lines = ["## 🔁 推荐复习\n"]
    for i, n in enumerate(nodes, 1):
        name = n.get("name", "未知")
        node_domain = n.get("domain", "")
        mastery = n.get("mastery_level", 0)
        stars = "⭐" * mastery + "☆" * (5 - mastery)
        days = n.get("days_since_review", 30)
        priority = n.get("priority", 0)

        # 上次复习时间
        last_reviewed = n.get("last_reviewed")
        if last_reviewed:
            try:
                # 尝试截取日期部分显示
                review_str = str(last_reviewed).split(" ")[0] if " " in str(last_reviewed) else str(last_reviewed)
            except Exception:
                review_str = str(last_reviewed)
        else:
            review_str = "从未复习"

        # 建议理由
        reasons = []
        if days >= 30:
            reasons.append(f"已 {days} 天未复习")
        elif days >= 7:
            reasons.append(f"已 {days} 天未复习")
        if mastery <= 2:
            reasons.append("掌握度较低")
        reason = "；".join(reasons) if reasons else "建议定期复习"

        lines.append(f"**{i}. {name}** [{node_domain}]（掌握度：{stars}）")
        lines.append(f"   上次复习：{review_str}")
        lines.append(f"   建议理由：{reason}\n")

    return "\n".join(lines)


def refresh_plan_goal_list():
    """刷新学习计划目标下拉列表"""
    goals = goal_manager.list_goals()
    choices = [(g.title, g.goal_id) for g in goals]
    return gr.update(choices=choices, value=None)


def generate_study_plan(goal_id, days, save_tasks):
    """调用学习计划生成器技能"""
    params = {
        "goal_id": goal_id or None,
        "days": int(days) if days else 7,
        "save_tasks": bool(save_tasks),
    }
    try:
        result = runtime.skill_manager.run_skill("study_plan_generator", params)
    except Exception as e:
        return f"❌ 技能执行异常：{str(e)}"

    if not isinstance(result, dict):
        return "❌ 技能返回了意外的结果类型"

    if "error" in result:
        return f"❌ 生成失败：{result['error']}"

    return result.get("result", "生成完成，但未返回结果")


def incubate_idea(idea, mode):
    """调用想法孵化器技能，将想法转化为目标或计划"""
    if not idea or not str(idea).strip():
        return "⚠️ 请输入你的想法"
    try:
        result = runtime.skill_manager.run_skill(
            "idea_incubator", {"idea": str(idea).strip(), "mode": str(mode or "goal")}
        )
    except Exception as e:
        return f"❌ 技能执行异常：{str(e)}"

    if not isinstance(result, dict):
        return f"❌ 技能返回了意外的结果类型"

    if "error" in result:
        return f"❌ 孵化失败：{result['error']}"

    return result.get("result", "孵化完成，但未返回结果")


def generate_quiz(concept, difficulty, num_questions):
    """调用概念测验技能，生成测验题"""
    params = {
        "concept": str(concept).strip() if concept else None,
        "difficulty": str(difficulty or "medium"),
        "num_questions": int(num_questions) if num_questions else 3,
    }
    try:
        result = runtime.skill_manager.run_skill("concept_quiz", params)
    except Exception as e:
        return f"❌ 技能执行异常：{str(e)}"

    if not isinstance(result, dict):
        return "❌ 技能返回了意外的结果类型"

    if "error" in result:
        return f"❌ 生成测验失败：{result['error']}"

    return result.get("result", "生成完成，但未返回结果")


def explain_concept(concept, level):
    """调用概念讲解技能，生成通俗易懂的概念讲解

    （P4-9，2026-09-12：`skills/concept_explainer/` 自 Phase 2 起一直"只有目录与
    manifest、没有任何 UI 入口"——技能列表里能看见，却点不到。本回调把技能接到
    「🧩 技能列表」页的「📖 概念讲解」区块，结构与 generate_quiz 同构。）
    """
    if not concept or not str(concept).strip():
        return "⚠️ 请输入要讲解的概念"

    params = {
        "concept": str(concept).strip(),
        "level": str(level or "beginner"),
    }
    try:
        result = runtime.skill_manager.run_skill("concept_explainer", params)
    except Exception as e:
        return f"❌ 技能执行异常：{str(e)}"

    if not isinstance(result, dict):
        return "❌ 技能返回了意外的结果类型"

    if "error" in result:
        return f"❌ 生成讲解失败：{result['error']}"

    # 该技能的输出键是 explanation（见 skills/concept_explainer/manifest.json 的
    # output_schema），不是其它技能用的 result——run_skill 原样透传技能返回值。
    return result.get("explanation") or "生成完成，但未返回讲解内容"


def get_skill_display():
    """获取已加载技能的展示信息"""
    skills = runtime.skill_manager.list_skills()
    if not skills:
        return "暂无已加载技能"

    lines = []
    for s in skills:
        tags = f" `{s['tags']}`" if s.get('tags') else ""
        lines.append(f"**{s['display_name']}** (v{s['version']}){tags}\n{s['description']}")
    return "\n\n".join(lines)


def do_generate_daily_plan(daily_hours_override=None):
    """生成今日计划建议（只分析不写库，确认后应用）

    `daily_hours_override` 是首页「⚡ 帮我排今天」与「🔁 按此时长重新生成预览」共用的
    那只「本次可用小时」输入框的值，原样透传给代理：非空时本次按它安排，
    **不写档案**（spec §2.1 裁定 5）；留空时 Gradio 给 None，走档案 daily_time。
    """
    try:
        result = daily_planner_agent.generate_daily_plan(
            daily_hours_override=daily_hours_override)
    except Exception as e:
        return f"❌ 生成今日计划失败：{e}", "", None
    return result["message"], result["display"], result.get("plan") or []


def do_apply_daily_plan(daily_state):
    """应用确认后的今日计划：把建议任务标记为今天执行（写入 planned_date）

    无论成败均把 State 置 None，防止重复点击导致同一批任务被重复标记
    （模式同 do_apply_mirror_suggestions）。
    """
    if not daily_state:
        return "⚠️ 请先点击「📅 生成今日计划」", None
    try:
        result = daily_planner_agent.apply_daily_plan(daily_state)
    except Exception as e:
        return f"❌ 应用计划失败：{e}", None
    msg = result["message"]
    skipped = [s for s in (result.get("skipped") or []) if s]
    if skipped:
        msg = f"{msg}\n" + "\n".join(f"- ⚠️ {s}" for s in skipped)
    return msg, None


def do_check_reminders():
    """定时/手动检查提醒：返回当前应在通知区显示的 Markdown（无提醒为空串）

    被 gr.Timer.tick（每 60 秒）、「🔔 检查提醒」按钮与页面加载链共用；
    ReminderAgent.check_reminders 内部已全量异常捕获，此处再兜底一层，
    保证任何异常下都返回空串、不干扰其他组件。
    """
    try:
        result = reminder_agent.check_reminders()
        return str(result.get("reminder") or "")
    except Exception as e:
        print(f"[Reminder] 提醒回调失败（忽略）: {e}")
        return ""


def load_reminder_settings():
    """页面加载时把已保存的提醒时间回填到输入框，并给出状态说明"""
    try:
        times = reminder_agent.get_reminder_times()
        if not times:
            # 已被用户显式关闭（档案存 OFF 哨兵）：输入框留空
            return "", "⏸️ 主动提醒当前已关闭"
        saved_raw = ProfileManager.get("reminder_times")
        display = ",".join(times)
        note = ("✅ 提醒时间：" + display +
                ("（默认值，可在上方修改后保存）" if saved_raw is None else ""))
        return display, note
    except Exception as e:
        print(f"[Reminder] 加载提醒设置失败（忽略）: {e}")
        return "", ""


def do_save_reminder_times(text):
    """保存提醒时间设置：合法 HH:MM 列表写档案；清空输入框 = 关闭提醒

    Returns:
        (状态 Markdown, 回显到输入框的规范化文本) —— 与 outputs 一一对应。
    """
    raw = text or ""
    try:
        result = reminder_agent.set_reminder_times(raw)
        echo_text = ",".join(result.get("times") or [])
        if result.get("ok"):
            return result["message"], echo_text
        # 输入非法：保持原设置不变，输入框原样回显，便于用户修正
        return result["message"], raw
    except Exception as e:
        return f"❌ 保存失败：{e}", raw


def _valid_time_str(text) -> bool:
    """校验触发时间格式：HH:MM（0-23 时、0-59 分）"""
    try:
        parts = (text or "").strip().split(":", 1)
        if len(parts) != 2:
            return False
        hour, minute = int(parts[0]), int(parts[1])
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, TypeError):
        return False
