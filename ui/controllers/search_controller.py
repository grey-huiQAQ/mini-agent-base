# -*- coding: utf-8 -*-
"""ui.controllers.search_controller —— 深度搜索

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from core.information_engine.evidence_store import EvidenceStore, render_evidence_note
from memory.note_store import NoteStore, render_note_preview

DEFAULT_SEARCH_GOAL_TITLE = "资料搜索"

from ui.controllers._shared import (
    goal_manager,
    goal_manager as _shared_goal_manager,
    search_task_manager,
)




def do_start_search(query):
    """创建并后台启动一个深度搜索任务（异步执行，不阻塞 UI）

    Returns:
        (状态 Markdown, 清空输入框, 清空证据勾选框)
    """
    query = (query or "").strip()
    if not query:
        return "⚠️ 请输入搜索查询", "", gr.update(choices=[], value=[])
    try:
        task_id = search_task_manager.submit(query)
    except Exception as e:
        return f"❌ 创建搜索任务失败：{e}", "", gr.update(choices=[], value=[])
    return (
        f"✅ 搜索任务 #{task_id} 已创建，后台执行中（最多同时运行 3 个任务，"
        f"当前进行中 {search_task_manager.get_running_count()} 个）…",
        "",  # 清空输入框
        gr.update(choices=[], value=[]),  # 清空证据勾选
    )


def do_refresh_search_panel(current_task_id=None):
    """刷新任务列表（Markdown 表格 + 任务下拉框），保留当前选中值

    Returns:
        (任务列表 Markdown, 任务下拉框 update)
    """
    try:
        tasks = search_task_manager.list_tasks(limit=20)
    except Exception as e:
        print(f"[Search] 任务列表刷新失败: {e}")
        return "⚠️ 任务列表加载失败", gr.update(choices=[], value=None)
    if not tasks:
        return "暂无搜索任务", gr.update(choices=[], value=None)
    lines = ["| # | 查询 | 状态 | 结果摘要 |", "|---|------|------|---------|"]
    status_icon = {
        "pending": "⏳ 排队中",
        "running": "🔄 进行中",
        "done": "✅ 已完成",
        "failed": "❌ 失败",
    }
    for t in tasks:
        icon = status_icon.get(t["status"], t["status"])
        query_short = ((t.get("query") or "").replace("|", "｜"))[:24]
        summary = ((t.get("result_summary") or "").replace("|", "｜"))[:30]
        lines.append(f"| {t['id']} | {query_short} | {icon} | {summary} |")
    choices = [(f"#{t['id']} {t['query'][:20]}", t["id"]) for t in tasks]
    ids = {t["id"] for t in tasks}
    value = current_task_id if current_task_id in ids else None
    return "\n".join(lines), gr.update(choices=choices, value=value)


def do_view_search_task(task_id):
    """查看任务的证据结果：渲染 Markdown 结果 + 填充证据勾选框

    Returns:
        (结果 Markdown, 证据勾选框 update)
    """
    if not task_id:
        return "选择任务后在此预览证据", gr.update(choices=[], value=[])
    try:
        task_id = int(task_id)
        task = search_task_manager.get_task(task_id)
        evidences = EvidenceStore.get_evidence_by_task(task_id)
    except Exception as e:
        return f"⚠️ 任务读取失败：{e}", gr.update(choices=[], value=[])
    if not task:
        return "⚠️ 任务不存在", gr.update(choices=[], value=[])

    status_icon = {
        "pending": "⏳ 排队中",
        "running": "🔄 进行中",
        "done": "✅ 已完成",
        "failed": "❌ 失败",
    }
    icon = status_icon.get(task["status"], task["status"])
    lines = [
        f"### 📄 任务 #{task['id']}：{task['query']}",
        "",
        f"> 状态：{icon}｜创建时间：{(task.get('created_at') or '')[:19]}",
    ]
    if task.get("completed_at"):
        lines.append(f"｜完成时间：{(task['completed_at'] or '')[:19]}")
    if task.get("result_summary"):
        lines.append(f"\n> {task['result_summary']}")
    lines.append("")

    if not evidences:
        lines.append("（该任务暂无证据 —— 任务可能尚未完成，或未找到结果）")
        return "\n".join(lines), gr.update(choices=[], value=[])

    lines.append(f"共 **{len(evidences)}** 条证据（**临时存储**，"
                 f"勾选后点击下方「确认保存」才会写入笔记系统）：\n")
    for i, ev in enumerate(evidences, 1):
        title = (ev.get("title") or "（无标题）").replace("|", "｜")
        lines.append(f"**{i}. {title}** — {ev.get('source_name')}（置信度 {ev.get('confidence')}）")
        snippet = (ev.get("snippet") or "").strip().replace("\n", " ")
        if snippet:
            lines.append(f"　{snippet[:140]}")
        if ev.get("source_url"):
            lines.append(f"　🔗 {ev['source_url']}")
        lines.append("")
    choices = [(f"#{ev['id']}｜{(ev['title'] or '无标题')[:28]}", str(ev["id"]))
               for ev in evidences]
    return "\n".join(lines), gr.update(choices=choices, value=[])


def refresh_search_save_goals():
    """刷新「保存为笔记」的目标下拉框"""
    try:
        goals = goal_manager.list_goals()
        choices = [(g.title, g.goal_id) for g in goals]
    except Exception as e:
        print(f"[Search] 目标列表刷新失败（忽略）: {e}")
        choices = []
    return gr.update(choices=choices, value=None)


def resolve_search_save_goal(goal_id, goal_manager=None):
    """解析搜索结果的保存目标（目标存在性校验 + 自动创建）

    Args:
        goal_id: 用户选择/输入的目标 ID（可为空字符串/None）
        goal_manager: 目标管理器实例（默认使用全局 goal_manager，便于测试注入）

    Returns:
        (goal_id, error)：error 非空表示解析失败（不执行保存）；
        成功时 goal_id 为最终使用的目标 ID。
    """
    gm = goal_manager if goal_manager is not None else _shared_goal_manager
    goal_id = (goal_id or "").strip()
    if not goal_id:
        # 留空：复用已存在的「资料搜索」目标；不存在则自动创建，创建失败报错
        try:
            existing = gm.find_goal_by_exact_title(DEFAULT_SEARCH_GOAL_TITLE)
            if existing:
                return existing.goal_id, ""
            created = gm.create_goal(DEFAULT_SEARCH_GOAL_TITLE,
                                     "深度搜索结果的默认保存目标")
            return created.goal_id, ""
        except Exception as e:
            return None, f"❌ 自动创建「{DEFAULT_SEARCH_GOAL_TITLE}」目标失败：{e}"
    # 用户提供目标 ID：先校验存在性，不存在返回友好错误
    try:
        goal = gm.get_goal(goal_id)
    except Exception as e:
        return None, f"❌ 目标校验失败：{e}"
    if not goal:
        return None, (f"❌ 目标不存在：{goal_id}，请先在「🎯 目标管理」中创建该目标"
                      f"或选择其他目标")
    return goal_id, ""


def do_save_search_results(task_id, goal_id, evidence_ids):
    """用户确认后把勾选的证据保存为笔记（V0.1 实现保存为笔记；仅保留标记接口已预留） (2026-09-12)

    流程：校验任务 → 过滤勾选证据 → **去重**（跳过 saved_note_id 非空的已保存证据）
    → **目标校验**（留空自动创建「资料搜索」/ 提供 ID 校验存在性）→ 写入笔记
    → 打 saved_note_id 标记。

    Returns:
        状态 Markdown（与 outputs 一一对应）
    """
    if not task_id:
        return "⚠️ 请先在上方选择一个搜索任务"
    try:
        task_id = int(task_id)
        task = search_task_manager.get_task(task_id)
        evidences = EvidenceStore.get_evidence_by_task(task_id)
    except Exception as e:
        return f"❌ 读取任务失败：{e}"
    if not task:
        return "⚠️ 任务不存在"
    if task["status"] != "done":
        return f"⚠️ 任务尚未完成（当前状态：{task['status']}），完成后才能保存"
    if not evidences:
        return "⚠️ 该任务没有可保存的证据"

    selected_ids = set(evidence_ids or [])
    selected = [ev for ev in evidences if str(ev["id"]) in selected_ids]
    if not selected:
        return "⚠️ 请先勾选要保留的证据（☑️）再点击保存"

    # 防重复保存：过滤掉已保存过的证据（saved_note_id 非空）
    already_saved = [ev for ev in selected if ev.get("saved_note_id")]
    to_save = [ev for ev in selected if not ev.get("saved_note_id")]
    if not to_save:
        return "ℹ️ 这些证据已保存过，无需重复保存"

    # 解析并校验保存目标（留空自动创建「资料搜索」；提供 ID 时校验存在性）
    target_goal, goal_error = resolve_search_save_goal(goal_id)
    if goal_error:
        return goal_error

    # 组装笔记内容（纯函数 render_evidence_note，便于单测；仅包含未保存过的证据）
    content = render_evidence_note(task["query"], to_save)
    title = f"深度搜索：{(task['query'] or '')[:30]}"
    try:
        note_id = NoteStore.add_note(target_goal, title, content, tags="搜索,资料")
    except Exception as e:
        return f"❌ 保存笔记失败：{e}"
    if not note_id or note_id <= 0:
        return "❌ 笔记保存失败，请检查参数（目标与标题不能为空）"

    # 保存成功后给本次保存的证据打上 saved_note_id 标记（为"仅保留标记"接口预留） (2026-09-12)
    for ev in to_save:
        try:
            EvidenceStore.update_evidence(ev["id"], saved_note_id=note_id)
        except Exception as e:
            print(f"[Search] 证据标记失败（忽略）: {e}")

    msg = f"✅ 已把 {len(to_save)} 条证据保存为笔记（#{note_id}，目标：{target_goal}）"
    if already_saved:
        msg += f"；已跳过 {len(already_saved)} 条已保存过的证据"
    return msg


def show_search_delete_confirm():
    """显示删除任务确认行（删除不可逆，先提示再执行）"""
    return (gr.update(visible=True),) * 3


def hide_search_delete_confirm():
    """隐藏删除任务确认行"""
    return (gr.update(visible=False),) * 3


def do_delete_search_task(task_id):
    """删除选中的搜索任务及其全部证据（不可逆；确认后执行）

    Returns:
        (删除状态, 任务列表, 任务下拉框, 结果预览, 证据勾选框,
         确认行/确认/取消 —— 共 8 个输出，与 outputs 一一对应)
    """
    hidden = (gr.update(visible=False),) * 3
    if not task_id:
        return (("⚠️ 请先在上方选择一个搜索任务",) + (gr.update(),) * 2 +
                ("选择任务后在此预览证据", gr.update(choices=[], value=[])) + hidden)
    try:
        task_id = int(task_id)
        deleted = search_task_manager.delete_task(task_id)
    except Exception as e:
        return (f"❌ 删除任务失败：{e}",) + (gr.update(),) * 2 + \
               ("选择任务后在此预览证据", gr.update(choices=[], value=[])) + hidden
    if not deleted:
        return ("⚠️ 任务不存在或已被删除",) + (gr.update(),) * 2 + \
               ("选择任务后在此预览证据", gr.update(choices=[], value=[])) + hidden

    # 刷新任务列表（选中任务已删除，传 None 由回调内部处理选中值）
    panel_md, dd_update = do_refresh_search_panel(None)
    return (
        f"🗑️ 已删除任务 #{task_id}（含其全部证据，不可恢复）",
        panel_md, dd_update,
        "选择任务后在此预览证据",           # 清空结果预览
        gr.update(choices=[], value=[]),    # 清空证据勾选框
    ) + hidden
