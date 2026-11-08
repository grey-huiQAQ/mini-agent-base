# -*- coding: utf-8 -*-
"""ui.controllers.task_controller —— 任务执行与审批

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from memory.approval_store import ApprovalStore
from agents.task_executor import task_executor, beep_audio_html, log_safe
from agents.daily_planning_agent import daily_planner_agent




def _advance_execution(gen, send_value):
    """推进执行生成器直到下一次审批暂停或全部结束（内部工具）

    Args:
        gen: task_executor.execute_plan 返回的生成器（经 gr.State 暂存）。
        send_value: None 表示刚启动、用 next 拉取首个事件；True/False 表示
                    对上一次审批请求的决定（批准保存 / 拒绝丢弃）。

    Returns:
        (状态消息, 进度 Markdown, 提示音 HTML, 生成器或 None) —— 与 outputs
        [goal_action_status, exec_display, exec_audio_slot, execution_gen_state]
        一一对应；结束时生成器位返回 None 防止重复恢复。
    """
    log_parts: list = []
    audio = ""
    try:
        while True:
            if send_value is None:
                event = next(gen)
            else:
                event = gen.send(send_value)
            send_value = None

            etype = event.get("type")
            message = str(event.get("message") or "")
            if etype == "approval":
                # 暂停点：展示待审阅内容 + 播放提示音，把生成器存回 State
                approval = event.get("approval") or {}
                log_parts.append(message)
                log_parts.append(str(approval.get("content") or ""))
                audio = beep_audio_html(autoplay=True)
                kind_label = "资料搜索结果" if approval.get("type") == "search" else "笔记草稿"
                return (f"⏸️ 暂停等待审批：{kind_label}已生成，"
                        "请点击「✅ 确认保存」或「❌ 拒绝」",
                        "\n\n".join(p for p in log_parts if p),
                        audio, gen)
            if etype == "done":
                log_parts.append(message)
                stats = event.get("stats") or {}
                status = (f"🏁 执行结束：保存 {stats.get('saved', 0)}｜"
                          f"拒绝 {stats.get('rejected', 0)}｜跳过 {stats.get('skipped', 0)}｜"
                          f"失败 {stats.get('failed', 0)}")
                return (status, "\n\n".join(p for p in log_parts if p), "", None)
            # progress / result 事件：累积进展示区后继续消费
            log_parts.append(message)
    except StopIteration:
        # 生成器已耗尽（如重复点击恢复）：安全收尾，不让 UI 报错
        return ("ℹ️ 本次执行已结束", "\n\n".join(p for p in log_parts if p), "", None)
    except Exception as e:
        return (f"❌ 执行过程出错：{e}", "\n\n".join(p for p in log_parts if p), "", None)


def do_execute_plan(daily_state):
    """手动触发「执行计划」：自动执行标记为可自动的任务，结果需审批后才落盘"""
    plan = daily_state
    if not plan:
        # 未先生成计划时现场补一份（LLM 失败会走本地规则降级，仍可执行）
        try:
            result = daily_planner_agent.generate_daily_plan()
            plan = result.get("plan") or []
        except Exception as e:
            return f"❌ 自动生成计划失败：{e}", "", "", None
    if not plan:
        return "⚠️ 当前没有可执行的计划任务，请先创建目标并添加待办", "暂无可执行的任务", "", None
    try:
        gen = task_executor.execute_plan(plan)
    except Exception as e:
        return f"❌ 启动执行失败：{e}", "", "", None
    return _advance_execution(gen, None)


def do_approve_pending(gen_state):
    """批准当前待审阅的结果：写入磁盘/数据库，并继续执行后续任务"""
    if not gen_state:
        return "⚠️ 当前没有待审批的结果，请先点击「▶️ 执行计划」", "", "", None
    return _advance_execution(gen_state, True)


def do_reject_pending(gen_state):
    """拒绝当前待审阅的结果：丢弃草稿（不写盘），并继续执行后续任务"""
    if not gen_state:
        return "⚠️ 当前没有待审批的结果，请先点击「▶️ 执行计划」", "", "", None
    return _advance_execution(gen_state, False)


def refresh_pending_list(current_id=None):
    """刷新待审批列表（概览 Markdown + 下拉框），保留当前选中值

    Returns:
        (待审批概览 Markdown, 下拉框 update) —— 与 outputs 一一对应
    """
    try:
        records = ApprovalStore.list_pending(status="pending", limit=50)
    except Exception as e:
        print(f"[Approval] 待审批列表加载失败（忽略）: {e}")
        return "⚠️ 待审批列表加载失败", gr.update(choices=[], value=None)
    if not records:
        return "暂无待审批的记录", gr.update(choices=[], value=None)

    lines = [f"**待审批 {len(records)} 条：**", ""]
    for r in records:
        kind = "🔍 搜索" if r.get("task_type") == "search" else "📝 笔记"
        title = r.get("task_title") or "（无标题）"
        created = str(r.get("created_at") or "")[:16]
        lines.append(f"- #{r['id']} {kind}「{title}」（{created}）")
    choices = [(f"#{r['id']}｜{(r.get('task_title') or '无标题')[:20]}", r["id"])
               for r in records]
    ids = {r["id"] for r in records}
    value = current_id if current_id in ids else None
    return "\n".join(lines), gr.update(choices=choices, value=value)


def view_pending(pending_id):
    """查看选中待审批记录的完整内容（Markdown）"""
    if not pending_id:
        return "选择待审批记录后在此显示内容"
    try:
        record = ApprovalStore.get_pending(int(pending_id))
    except Exception as e:
        return f"⚠️ 读取失败：{e}"
    if not record:
        return "⚠️ 记录不存在或已被处理"
    kind = "🔍 资料搜索结果" if record.get("task_type") == "search" else "📝 笔记草稿"
    header = (f"### ⏸️ 待审批 #{record['id']}：{record.get('task_title') or '（无标题）'}\n\n"
              f"> 类型：{kind}｜目标：{record.get('goal_title') or record.get('goal_id') or '未指定'}｜"
              f"创建时间：{str(record.get('created_at') or '')[:19]}\n\n"
              f"建议保存路径：`{record.get('suggested_path') or '（未指定）'}`\n\n---\n\n")
    return header + (record.get("content") or "（内容为空）")


def approve_pending_from_db(pending_id):
    """从待审批列表确认：用持久化的审批内容重新执行对应任务（落盘），更新状态

    说明：绕过内存生成器直接调用 save_approval（避免复杂断点恢复）；
    保存成功 → 状态置 approved；保存失败 → 保留 pending 供重试。

    Returns:
        (状态 Markdown, 概览 Markdown, 下拉框 update, 内容区 Markdown)
    """
    empty = (gr.update(),) * 2
    if not pending_id:
        return "⚠️ 请先选择一条待审批记录", "暂无待审批的记录", \
            gr.update(choices=[], value=None), "选择待审批记录后在此显示内容"
    try:
        pending_id = int(pending_id)
        record = ApprovalStore.get_pending(pending_id)
    except Exception as e:
        return f"❌ 读取审批记录失败：{e}", *empty, "选择待审批记录后在此显示内容"
    if not record:
        return "⚠️ 记录不存在或已被处理", *empty, "选择待审批记录后在此显示内容"
    if record.get("status") != "pending":
        return (f"⚠️ 该记录已被处理（状态：{record.get('status')}）",
                *empty, "选择待审批记录后在此显示内容")

    approval = record.get("detail") or {}
    if not approval:
        return "❌ 审批记录缺少内容，无法重新执行", *empty, "选择待审批记录后在此显示内容"
    try:
        outcome = task_executor.save_approval(approval)
    except Exception as e:
        return f"❌ 重新执行失败：{e}", *empty, "选择待审批记录后在此显示内容"

    if outcome.get("ok"):
        try:
            ApprovalStore.update_status(pending_id, "approved")
        except Exception as e:
            print(f"[Approval] 状态更新失败（忽略）: {e}")
        msg = f"✅ 已处理 #{pending_id}：{outcome.get('message')}"
    else:
        # 保存失败：保留 pending 状态供重试
        msg = f"❌ 处理失败（记录保留待重试）：{outcome.get('message')}"

    panel_md, dd_update = refresh_pending_list(None)
    return msg, panel_md, dd_update, "选择待审批记录后在此显示内容"


def reject_pending_from_db(pending_id):
    """从待审批列表拒绝：仅更新状态为 rejected，不产生任何写操作"""
    empty = (gr.update(),) * 2
    if not pending_id:
        return "⚠️ 请先选择一条待审批记录", "暂无待审批的记录", \
            gr.update(choices=[], value=None), "选择待审批记录后在此显示内容"
    try:
        pending_id = int(pending_id)
        record = ApprovalStore.get_pending(pending_id)
    except Exception as e:
        return f"❌ 读取审批记录失败：{e}", *empty, "选择待审批记录后在此显示内容"
    if not record:
        return "⚠️ 记录不存在或已被处理", *empty, "选择待审批记录后在此显示内容"
    if record.get("status") != "pending":
        return (f"⚠️ 该记录已被处理（状态：{record.get('status')}）",
                *empty, "选择待审批记录后在此显示内容")
    try:
        ok = ApprovalStore.update_status(pending_id, "rejected")
    except Exception as e:
        return f"❌ 更新状态失败：{e}", *empty, "选择待审批记录后在此显示内容"
    msg = (f"🚫 已拒绝 #{pending_id}（{(record.get('task_title') or '无标题')}），"
           f"未产生任何写入") if ok else f"⚠️ 记录 #{pending_id} 状态更新失败"
    panel_md, dd_update = refresh_pending_list(None)
    return msg, panel_md, dd_update, "选择待审批记录后在此显示内容"
