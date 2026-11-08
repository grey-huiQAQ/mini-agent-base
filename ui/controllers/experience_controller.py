# -*- coding: utf-8 -*-
"""ui.controllers.experience_controller —— 经验沉淀与反馈 / 规划镜像

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from memory.experience_store import ExperienceStore
from agents.planning_mirror_agent import planning_mirror_agent
from agents.reflection_agent import reflection_agent

_EXP_ITEM_MAX_LEN = 40
_EXP_LIST_LIMIT = 20




def do_reflect_experiences(chat_history):
    """分析当前会话最近对话，提取可复用的策略/偏好/成功模式（仅提取不写库）"""
    try:
        result = reflection_agent.reflect_on_conversation(chat_history)
    except Exception as e:
        return f"❌ 经验反思失败：{e}", None
    # 状态行 + 建议正文合并展示；无建议时仅显示提示
    display = (f"{result['message']}\n\n{result['display']}"
               if result.get("display") else result["message"])
    return display, result.get("suggestions") or []


def do_save_experiences(suggestions_state, current_session):
    """保存用户确认的经验条目到经验库（记录来源会话便于溯源）"""
    if not suggestions_state:
        return "⚠️ 请先点击「🧠 沉淀经验」获取建议", None
    try:
        result = reflection_agent.apply_suggestions(
            suggestions_state, source_session_id=(current_session or None)
        )
    except Exception as e:
        return f"❌ 保存经验失败：{e}", None
    lines = "\n".join(f"- {d}" for d in result["details"] if d)
    if result["ok"]:
        return f"✅ 已保存 {result['saved']} 条经验，后续对话将自动参考：\n{lines}", None
    return lines or "⚠️ 没有可保存的经验", None


def _exp_item_label(exp: dict) -> str:
    """生成单条经验的展示标签：#ID 场景摘要 → 策略摘要（⭐ 评分 N）"""
    scenario = " ".join(str(exp.get("scenario") or "").split())
    strategy = " ".join(str(exp.get("strategy") or "").split())
    if len(scenario) > _EXP_ITEM_MAX_LEN:
        scenario = scenario[:_EXP_ITEM_MAX_LEN - 1] + "…"
    if len(strategy) > _EXP_ITEM_MAX_LEN:
        strategy = strategy[:_EXP_ITEM_MAX_LEN - 1] + "…"
    score = exp.get("score") or 0
    return f"#{exp['id']} 场景「{scenario}」→ {strategy}（⭐ 评分 {score}）"


def build_experience_feedback_view() -> tuple:
    """构建经验反馈面板数据：(Markdown 列表, Dropdown 更新对象)

    列表按评分降序（高评分在前，便于优先处理），下拉按同一顺序提供选项。
    """
    exps = ExperienceStore.get_all_experiences(limit=_EXP_LIST_LIMIT, sort_by_score=True)
    if not exps:
        return "暂无已保存的经验（可在上方「🧠 沉淀经验」后确认保存）", gr.update(choices=[], value=None)
    lines = [f"**📚 已保存经验（{len(exps)} 条，按评分排序）**", ""]
    choices = []
    for exp in exps:
        label = _exp_item_label(exp)
        lines.append(f"- {label}")
        choices.append((label, exp["id"]))
    lines.append("")
    lines.append("> 💡 在下方选择一条经验并点击 👍/👎 反馈；评分影响后续对话注入时的优先级。")
    markdown = "\n".join(lines)
    # 默认选中第一条（若有）
    default_value = exps[0]["id"]
    return markdown, gr.update(choices=choices, value=default_value)


def refresh_exp_panel():
    """刷新经验反馈面板（列表 + 下拉框）"""
    try:
        return build_experience_feedback_view()
    except Exception as e:
        return f"❌ 经验列表加载失败：{e}", gr.update(choices=[], value=None)


def do_exp_feedback(exp_id, feedback: str):
    """记录用户对所选经验的有用/无用反馈，并刷新面板

    Args:
        exp_id: 下拉框选中的经验 ID（int；未选择时为 None）
        feedback: "up" / "down"
    """
    if not exp_id:
        return "⚠️ 请先选择要评价的经验", *refresh_exp_panel()
    try:
        # dedup_window_seconds=1：同一条经验 1 秒内的重复反馈直接忽略，
        # 与按钮置灰双保险，防止连续点击重复累计评分
        ok = ExperienceStore.record_feedback(int(exp_id), feedback,
                                             dedup_window_seconds=1)
    except Exception as e:
        return f"❌ 反馈记录失败：{e}", *refresh_exp_panel()
    if not ok:
        return "⚠️ 反馈未生效：经验可能已被删除或类型非法", *refresh_exp_panel()
    word = "有用（+1）" if feedback == "up" else "无用（-1）"
    return f"✅ 已记录反馈：#{exp_id} {word}", *refresh_exp_panel()


def _set_feedback_buttons(enabled: bool) -> tuple:
    """统一生成 👍/👎 反馈按钮的启用/禁用更新对象（防抖用）

    返回元组顺序与 exp_up_btn / exp_down_btn 两个组件一一对应。
    """
    state = gr.update(interactive=enabled)
    return state, state


def disable_feedback_buttons():
    """点击瞬间禁用 👍/👎 按钮，防止连续点击重复累计评分（.click 第一步调用）"""
    return _set_feedback_buttons(False)


def enable_feedback_buttons():
    """反馈回调完成后重新启用 👍/👎 按钮（经 .then 链调用）"""
    return _set_feedback_buttons(True)


def do_cleanup_low_experiences():
    """清理低分经验（评分 ≤ -3），返回结果消息并刷新面板"""
    try:
        deleted = ExperienceStore.cleanup_low_score(threshold=-3)
    except Exception as e:
        return f"❌ 清理失败：{e}", *refresh_exp_panel()
    msg = (f"🧹 已清理 {deleted} 条低分经验（评分 ≤ -3）"
           if deleted > 0 else "ℹ️ 没有评分 ≤ -3 的经验需要清理")
    return msg, *refresh_exp_panel()


def do_mirror_snapshot():
    """生成所有进行中目标的状态快照（只读分析，不写库）"""
    try:
        result = planning_mirror_agent.generate_snapshot()
    except Exception as e:
        return f"❌ 快照生成失败：{e}", ""
    return result["message"], result["display"]


def do_mirror_simulate(new_title, new_desc, weeks, priority):
    """模拟新增一个目标对现有规划的影响（只分析不写库）"""
    try:
        result = planning_mirror_agent.simulate_new_goal(
            new_goal_title=new_title or "",
            new_goal_desc=new_desc or "",
            estimated_weeks=weeks,
            priority=priority,
        )
    except Exception as e:
        return f"❌ 影响模拟失败：{e}", ""
    return result["message"], result["display"]


def do_mirror_suggest():
    """生成下一步最优行动建议（仅分析不写库，确认后应用）"""
    try:
        result = planning_mirror_agent.suggest_next_actions(limit=3)
    except Exception as e:
        return f"❌ 行动建议失败：{e}", "", []
    return result["message"], result["display"], result.get("suggestions") or []


def do_apply_mirror_suggestions(mirror_state):
    """应用确认后的行动建议：更新优先级/截止日期或暂停目标

    无论成败均在返回值中把 State 置 None，防止重复点击导致同一批建议被
    重复写库（模式同 do_save_experiences 的 (msg, None) 返回）。
    """
    if not mirror_state:
        return "⚠️ 请先点击「💡 生成行动建议」", None
    try:
        result = planning_mirror_agent.apply_suggestion(mirror_state)
    except Exception as e:
        return f"❌ 应用建议失败：{e}", None
    lines = "\n".join(f"- {d}" for d in result["details"] if d)
    if result["ok"]:
        return f"✅ 已应用 {result['applied']} 条建议：\n{lines}", None
    return lines or "⚠️ 没有可应用的建议", None
