# -*- coding: utf-8 -*-
"""ui.controllers.notes_controller —— 笔记系统

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from memory.note_store import NoteStore, render_note_preview
from pathlib import Path

from ui.controllers._shared import (
    goal_manager,
)




def _notes_panel_view(goal_id, query: str = None, tag: str = None,
                      search_mode: str = "keyword"):
    """构建笔记面板数据：(列表 Markdown, 笔记下拉更新对象)

    供刷新与搜索共用；目标未选择或加载异常时返回安全默认值。
    支持按标签过滤（tag）和语义检索模式（search_mode）。

    Args:
        goal_id: 当前选中的目标 ID
        query: 检索关键词/语义查询（None 或空表示不检索）
        tag: 标签过滤关键词（None 或空表示不过滤）
        search_mode: "keyword" 关键词 / "semantic" 语义
    """
    empty_dd = gr.update(choices=[], value=None)
    if not goal_id:
        return "请先在上方选择一个目标", empty_dd
    try:
        if search_mode == "semantic":
            if query:
                # 语义搜索：向量检索 → 映射回 NoteStore（延迟导入 RAG 依赖）
                from rag.note_indexer import NoteIndexer as _NI
                notes = _NI.search_notes_semantic(query, goal_id=goal_id, top_k=10)
                # 降级：语义搜索无结果时自动回退到关键词模式，
                # 并在结果头部给出提示，避免用户误以为程序崩溃
                if not notes:
                    notes = NoteStore.search_notes(query, goal_id=goal_id, tag=tag)
                    if notes:
                        notes = [{"_fallback_hint": True}] + notes
            else:
                # 语义模式但无查询 → 退化为列表/标签筛选
                if tag:
                    notes = NoteStore.search_notes_by_tag(tag, goal_id=goal_id)
                else:
                    notes = NoteStore.get_notes_by_goal(goal_id)
        elif query:
            notes = NoteStore.search_notes(query, goal_id=goal_id, tag=tag)
        elif tag:
            notes = NoteStore.search_notes_by_tag(tag, goal_id=goal_id)
        else:
            notes = NoteStore.get_notes_by_goal(goal_id)
    except Exception as e:
        return f"⚠️ 笔记加载失败：{e}", empty_dd

    if not notes:
        hint_parts = []
        if search_mode == "semantic":
            hint_parts.append("语义")
        if query:
            hint_parts.append(f"「{query}」")
        if tag:
            hint_parts.append(f"标签：{tag}")
        if hint_parts:
            hint = f"没有匹配{' '.join(hint_parts)}的笔记"
        else:
            hint = "该目标下暂无笔记（可在每日规划中执行「整理笔记」类任务自动生成）"
        return hint, empty_dd

    # 检测降级提示标记（语义搜索无结果时自动回退关键词模式）
    fallback_hint = False
    if notes and isinstance(notes[0], dict) and notes[0].get("_fallback_hint"):
        fallback_hint = True
        notes = notes[1:]  # 移除标记，保留实际笔记

    mode_label = "语义检索" if search_mode == "semantic" else "关键词检索"
    filter_parts = []
    if query:
        filter_parts.append(f"「{query}」")
    if tag:
        filter_parts.append(f"标签：{tag}")
    filter_str = " + ".join(filter_parts) if filter_parts else ""
    header = f"**📝 笔记列表（{len(notes)} 篇"
    if filter_str:
        header += f" · {mode_label} {filter_str}"
    header += "）**"

    lines = [header, ""]
    # 语义搜索降级提示：模型不可用或向量库为空时回退到关键词模式
    if fallback_hint:
        lines.append("> ℹ️ 语义搜索暂不可用（模型未加载或向量库为空），已自动切换为关键词检索。")
        lines.append("")
    choices = []
    for n in notes:
        fname = Path(n.get("file_path") or "").name or "（未存文件）"
        preview = render_note_preview(n.get("content") or "")
        tags_str = n.get("tags") or ""
        tag_display = f" 🏷️ {tags_str}" if tags_str else ""
        label = f"#{n['id']} {n['title']}"
        lines.append(f"- **{label}**｜`{fname}`{tag_display}")
        lines.append(f"  {preview}")
        choices.append((label, n["id"]))
    dropdown_update = gr.update(choices=choices,
                                value=choices[0][1] if choices else None)
    return "\n".join(lines), dropdown_update


def do_refresh_notes(goal_id, tag_filter=None):
    """刷新所选目标的笔记列表面板（可选标签过滤）"""
    tag = (tag_filter or "").strip().lower() or None
    return _notes_panel_view(goal_id, tag=tag)


def do_search_notes(goal_id, query, search_mode, tag_filter):
    """搜索笔记：支持关键词搜索和语义搜索模式（可选标签过滤）

    Args:
        goal_id: 当前目标 ID
        query: 搜索关键词 / 语义查询
        search_mode: "keyword" 或 "semantic"
        tag_filter: 标签过滤关键词（逗号分隔或单个）
    """
    q = (query or "").strip()
    tag = (tag_filter or "").strip().lower() or None
    mode = search_mode or "keyword"
    return _notes_panel_view(goal_id, query=q or None, tag=tag, search_mode=mode)


def do_show_note(note_id):
    """展示所选笔记的完整内容（Markdown）"""
    if not note_id:
        return "选择一篇笔记后在此显示内容"
    try:
        note = NoteStore.get_note(int(note_id))
    except Exception as e:
        return f"⚠️ 笔记读取失败：{e}"
    if not note:
        return "⚠️ 笔记不存在或已被删除"
    updated = str(note.get("updated_at") or "")[:19]
    tags_str = note.get("tags") or ""
    tag_line = f"🏷️ 标签：{tags_str}\n\n" if tags_str else ""
    header = (f"### 📄 {note['title']}\n\n"
              f"> 目标：{note['goal_id']}｜更新时间：{updated}\n\n"
              f"{tag_line}")
    return header + (note.get("content") or "（空笔记）")


def show_quick_save(goal_id, chat_history):
    """显示快速保存面板，预填当前目标 + 最后一条助手回复作为内容

    Args:
        goal_id: 当前选中的目标 ID（来自 goal_dropdown）
        chat_history: 聊天记录（list of dict）

    Returns:
        6 个输出：(accordion_visible_update, goal_dropdown_update,
                  title, tags, content, status)
    """
    # 获取目标列表用于下拉框
    goals = goal_manager.list_goals()
    choices = [(g.title, g.goal_id) for g in goals]

    # 提取最后一条助手回复作为预填内容
    content = ""
    if chat_history:
        for msg in reversed(chat_history):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                c = msg.get("content", "")
                if isinstance(c, list):
                    content = " ".join(str(x) for x in c)
                else:
                    content = str(c)
                break

    title = "来自对话的笔记"
    if goal_id:
        goal = goal_manager.get_goal(goal_id)
        status = f"正在保存到目标：{goal.title if goal else goal_id}"
    else:
        status = "⚠️ 未选择目标，请先在「🎯 目标管理」中选择目标"

    return (
        gr.update(visible=True),
        gr.update(choices=choices, value=goal_id),
        title,
        "",
        content,
        status,
    )


def do_save_note_from_chat(goal_id, title, tags, content):
    """从快速保存表单提交：写入 NoteStore + 触发 RAG 索引，刷新笔记面板

    Returns:
        7 个输出：(status, notes_list_display, note_view_dropdown,
                  accordion_visible_update, title_clear, tags_clear, content_clear)
    """
    if not goal_id:
        return (
            "⚠️ 请先在「🎯 目标管理」中选择目标",
            gr.update(), gr.update(),
            gr.update(visible=True), "", "", "",
        )
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        return (
            "⚠️ 请输入笔记标题",
            gr.update(), gr.update(),
            gr.update(visible=True), "", "", "",
        )

    try:
        note_id = NoteStore.add_note(goal_id, title, content, tags=tags)
        if note_id and note_id > 0:
            note = NoteStore.get_note(note_id)
            tag_str = f" 🏷️ {note.get('tags', '')}" if note.get("tags") else ""
            msg = f"✅ 笔记已保存：#{note_id} {title}{tag_str}"
        else:
            msg = "❌ 笔记保存失败，请检查参数"
    except Exception as e:
        msg = f"❌ 保存失败：{e}"

    # 刷新笔记面板（使用当前目标 + 无过滤）
    try:
        panel_md, panel_dd = _notes_panel_view(goal_id)
    except Exception:
        panel_md, panel_dd = "笔记面板加载失败", gr.update(choices=[], value=None)

    return (
        msg,
        panel_md, panel_dd,
        gr.update(visible=False),  # 隐藏快速保存面板
        "",  # 清空标题
        "",  # 清空标签
        "",  # 清空内容
    )
