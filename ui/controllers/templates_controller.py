# -*- coding: utf-8 -*-
"""ui.controllers.templates_controller —— 任务模板

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from core.task_template import (
    ACTION_SEARCH_AND_NOTE,
    list_templates,
    get_template,
    create_template,
    update_template,
    delete_template,
    set_enabled,
    pending_approval_count,
    template_goal_id,
)
from memory.note_store import NoteStore
from datetime import datetime
from agents.task_executor import task_executor, beep_audio_html, log_safe
import threading

_ACTION_LABELS = {
    "search_and_note": "🔍 搜索并整理笔记",
    # P7 批次 1a：「资源」格（只读本地机会库，不联网）。
    # 表单目前只提供 search_and_note 一种动作，故本键只影响**列表展示**：
    # 缺了它，列表会把 action_type 原样打成 "scout_digest"。
    "scout_digest": "🧊 资源筛选报告（本地机会库）",
    # P7 第一闭环 Phase 3c-乙（2026-10-06）：3b 只登记类型名、库里造不出这种模板，
    # 所以这个键当时永远不会命中；3c-甲 接上执行分支、3c-乙 表单能选动作之后，
    # 它就成了列表里真实会出现的一行。**缺标签不会报错，只会把 slug 原样印给人看**
    # ——这条覆盖关系由 tests/test_review_tab.py 拿 VALID_ACTIONS 逐个比对钉住，
    # 不靠这里写全（写全一次不等于永远写全）。
    "long_term_review": "🧭 长期项目复盘（产一行 Decision 账目）",
}
_TRIGGER_LABELS = {"manual": "🖐 手动", "daily": "📅 每日", "weekly": "📆 每周"}
_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

from ui.controllers.task_controller import (
    _advance_execution,
)
from ui.controllers.learning_controller import (
    _valid_time_str,
)




def _template_form_defaults():
    """新建表单默认值（与表单组件初值一致；顺序与表单 outputs 一一对应）

    顺序（P7 第一闭环 Phase 3c-乙 起共 9 项）：编辑状态 ID、名称、描述、触发类型、
    时间、星期几、关键词、自动审批、**动作类型**。动作类型**排在最后**是刻意的：
    前三项与前 8 个组件的位置关系是批次 1c-② 就定下的，往中间插一格会让所有
    "按位置对齐"的地方（本文件、app_factory 的三处 outputs 列表、用例）一起错位；
    追加在末尾时，老调用方少传一个参数只会"动作那一格没重置"，不会把关键词填进星期几。
    """
    return (None, "", "", "manual", "09:00", 0, "", False, ACTION_SEARCH_AND_NOTE)


def refresh_template_panel(current_id=None):
    """刷新模板列表（概览 Markdown + 下拉框），保留当前选中值

    **停摆必须可见**（P7 批次 1c-①）：堆积控制（存在未处理待审批就跳过触发）的
    副作用是模板**静默停摆**——它不触发、不报错，而「上次运行」还显示得好好的，
    用户没有任何线索知道它为什么不再产出。判据不是"他觉得卡了"，而是"他能不能
    从界面上看出它为什么不再产出"，所以停摆原因必须写在这一行里。

    Returns:
        (模板列表 Markdown, 下拉框 update) —— 与 outputs 一一对应
    """
    try:
        templates = list_templates()
    except Exception as e:
        print(f"[TaskTemplate] 模板列表加载失败（忽略）: {e}")
        return "⚠️ 模板列表加载失败", gr.update(choices=[], value=None)
    if not templates:
        return "暂无任务模板（在下方表单填写并点击「💾 保存模板」新建）", \
            gr.update(choices=[], value=None)

    lines = [f"**共 {len(templates)} 个模板：**", ""]
    for t in templates:
        state = "✅ 启用" if t.enabled else "⏸️ 停用"
        trigger = _TRIGGER_LABELS.get(t.trigger_type, t.trigger_type)
        if t.trigger_type == "daily":
            trigger += f" {t.trigger_time}"
        elif t.trigger_type == "weekly":
            trigger += f" {_WEEKDAY_NAMES[t.trigger_weekday % 7]} {t.trigger_time}"
        action = _ACTION_LABELS.get(t.action_type, t.action_type)
        last = str(t.last_run_at or "从未运行")[:16]
        # pending_approval_count 内部异常安全（失败按 0 处理），不会拖垮列表渲染
        pending = pending_approval_count(t.id)
        stall = f"｜⏸️ 有待审批未处理 → 已暂停触发（{pending} 条）" if pending else ""
        lines.append(f"- #{t.id} **{t.name}**｜{trigger}｜{action}｜{state}｜"
                     f"上次运行 {last}{stall}")
        if pending:
            # 文案更正（P7 批次 3）：这里原来写"下一次检查自动恢复触发"——**那是假的**，
            # 而且描述的是我们已经修掉的那个行为。创建这条待审批的那次触发**已经消费了
            # 当期**（P7 批次 2 的"触发即记账"：决定执行就写 last_run_at），所以处理完
            # 之后 `_is_due` 不再为真，下一次检查不会重跑；下一次产出要等它自己的触发日。
            # 原文等于把"对拍"当特性写在了界面上。
            # （唯一边界：若这次触发发生在**本期触发点之前**，则本期触发点仍会照常触发
            #   一次——那是一次触发消费一期，不是对拍。）
            lines.append("  - 手动运行与定时触发都会被跳过；处理完（确认或拒绝）后"
                         "不会立即重跑——下一次触发等它自己的触发日。"
                         "（若这次产出发生在本期触发点之前——例如在触发日之前手动跑过——"
                         "本期触发点仍会照常触发一次：一次触发消费一期，不会无限重复。）")
        if t.description:
            lines.append(f"  - {t.description}")
    choices = [(f"#{t.id}｜{t.name}", t.id) for t in templates]
    ids = {t.id for t in templates}
    value = current_id if current_id in ids else None
    return "\n".join(lines), gr.update(choices=choices, value=value)


def load_template_to_form(template_id):
    """选中模板 → 回填表单（进入编辑模式）；无选中/加载失败回退默认值

    Returns:
        (编辑状态 ID, 名称, 描述, 触发类型, 时间, 星期几, 关键词, 自动审批, 动作类型)
        —— 顺序与表单 outputs 一一对应（口径见 `_template_form_defaults`）
    """
    if not template_id:
        return _template_form_defaults()
    try:
        t = get_template(int(template_id))
    except Exception:
        return _template_form_defaults()
    if not t:
        return _template_form_defaults()
    cfg = t.trigger_config_dict
    time_str = str(cfg.get("time") or "09:00")
    try:
        weekday = int(cfg.get("weekday", 0))
    except (ValueError, TypeError):
        weekday = 0
    return (t.id, t.name, t.description or "", t.trigger_type, time_str,
            weekday, t.query, bool(t.auto_approve), t.action_type)


def do_save_template(name, description, trigger_type, time_str, weekday,
                     query, auto_approve, edit_id, action_type=None):
    """新建或更新模板（edit_id 为空=新建，否则=更新）

    **动作类型现在是表单里的一格**（P7 第一闭环 Phase 3c-乙，2026-10-06）。批次
    1c-② 时表单**没有** action_type 字段，所以"编辑"必须回读这条模板**现在**是什么
    动作，才能决定"关键词要不要强制、`action_params` 要不要原样保留"。现在表单自己
    带这一格（选中模板时由 `load_template_to_form` 回填），规则改成按**本次生效的
    动作**判，并把 1c-② 那两条语义原样保住：

    - `action_type` 传 None（不带这一格的老调用方）：编辑取模板原动作、新建取
      `search_and_note`——**逐键等于 1c-② 的行为**，这是刻意留的兼容读法；
    - 生效动作是 `search_and_note`：关键词必填、`action_params` 写 `{"query": ...}`；
    - 生效动作不是它且**动作没换**：跳过关键词校验、`action_params` **完全不传**
      （`update_template` 的 None 语义是"不修改该字段"，原值一个字节都不重写。
      这不是洁癖：把 `{}` 改写成 `{"query": ""}` 虽然不会崩，但那是一次**静默的参数
      污染**，"下游能容错"并不能把静默改写开脱掉——1c-② 原文）；
    - 生效动作不是它且**动作换了**：`action_params` 写 `{}`——旧参数描述的是上一个
      动作的条件，留着等于让下一任读到一个不属于这个动作的东西。

    非法动作名**不会**再被静默换成 search_and_note：`create_template` /
    `update_template` 从 3c-乙 起 `raise ValueError`（既不建行也不改行），本函数把它
    接成一行状态文案。表单只提供 `VALID_ACTIONS` 里的三个值，所以真走到那句 raise
    说明传进来的是表单之外的东西（脚本、旧页面缓存、被改过的库）。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 表单回填值 9 项)
        —— 顺序与 outputs 一一对应；保存成功后表单回到新建默认状态。
    """
    name = (name or "").strip()
    query = (query or "").strip()
    if not name:
        return "⚠️ 模板名称不能为空", *(refresh_template_panel(None)), \
            *_template_form_defaults()

    original = None
    if edit_id:
        try:
            original = get_template(int(edit_id))
        except Exception as e:
            print(f"[TaskTemplate] 编辑前读取模板失败（按新建语义继续）: {e}")
            original = None
    # 本次生效的动作：表单给了用表单的，没给则编辑取原动作、新建取默认动作
    effective_action = (str(action_type or "").strip()
                        or (original.action_type if original is not None
                            else ACTION_SEARCH_AND_NOTE))
    action_switched = (original is not None
                       and effective_action != original.action_type)

    if effective_action == ACTION_SEARCH_AND_NOTE and not query:
        return "⚠️ 搜索关键词不能为空", *(refresh_template_panel(None)), \
            *_template_form_defaults()
    if trigger_type in ("daily", "weekly") and not _valid_time_str(time_str):
        return "⚠️ 触发时间格式应为 HH:MM（如 09:00）", \
            *(refresh_template_panel(None)), *_template_form_defaults()

    trigger_config = {"time": str(time_str).strip() or "09:00"}
    if trigger_type == "weekly":
        try:
            trigger_config["weekday"] = int(weekday) % 7
        except (ValueError, TypeError):
            trigger_config["weekday"] = 0
    if effective_action == ACTION_SEARCH_AND_NOTE:
        action_params = {"query": query}
    elif action_switched:
        action_params = {}
    else:
        action_params = None
    auto = 1 if auto_approve else 0

    try:
        if edit_id:
            ok = update_template(int(edit_id), name=name, description=description or "",
                                 trigger_type=trigger_type,
                                 trigger_config=trigger_config,
                                 action_type=effective_action,
                                 action_params=action_params, auto_approve=auto)
            msg = f"✅ 已更新模板「{name}」" if ok else \
                "❌ 更新失败（模板可能已被删除）"
            if ok and action_switched:
                msg += (f"（动作换成 {effective_action}，动作参数已按新动作重置）")
            elif ok and effective_action != ACTION_SEARCH_AND_NOTE:
                msg += (f"（该模板动作是 {effective_action}，不需要搜索关键词，"
                        "动作参数原样保留未改写）")
        else:
            tid = create_template(name, description=description or "",
                                  trigger_type=trigger_type,
                                  trigger_config=trigger_config,
                                  action_type=effective_action,
                                  action_params=action_params, auto_approve=auto)
            msg = f"✅ 已创建模板「{name}」（#{tid}）" if tid else "❌ 创建失败"
            if tid and effective_action != ACTION_SEARCH_AND_NOTE:
                msg += f"（动作是 {effective_action}，这一格不读搜索关键词）"
    except ValueError as e:
        # 非法动作类型：一条都没写，把原因原样给人看（不换成"创建失败"那种含糊话）
        return f"⚠️ 没有写入：{e}", *(refresh_template_panel(None)), \
            *_template_form_defaults()
    panel_md, dd_update = refresh_template_panel(None)
    return msg, panel_md, dd_update, *_template_form_defaults()


def do_delete_template(template_id):
    """删除模板（不可恢复）；返回 (状态, 列表, 下拉框)"""
    if not template_id:
        return "⚠️ 请先选择一个模板", *(refresh_template_panel(None))
    try:
        t = get_template(int(template_id))
        ok = delete_template(int(template_id))
    except Exception as e:
        return f"❌ 删除失败：{e}", *(refresh_template_panel(None))
    name = t.name if t else f"#{template_id}"
    msg = f"✅ 已删除模板「{name}」" if ok else f"⚠️ 模板「{name}」删除失败或不存在"
    panel_md, dd_update = refresh_template_panel(None)
    return msg, panel_md, dd_update


def do_toggle_template(template_id):
    """启用/停用切换；返回 (状态, 列表, 下拉框)"""
    if not template_id:
        return "⚠️ 请先选择一个模板", *(refresh_template_panel(None))
    try:
        t = get_template(int(template_id))
        if not t:
            return "⚠️ 模板不存在或已被删除", *(refresh_template_panel(None))
        ok = set_enabled(int(template_id), not t.enabled)
    except Exception as e:
        return f"❌ 操作失败：{e}", *(refresh_template_panel(None))
    state = "已停用" if t.enabled else "已启用"
    msg = f"✅ 模板「{t.name}」{state}" if ok else "❌ 状态更新失败"
    panel_md, dd_update = refresh_template_panel(None)
    return msg, panel_md, dd_update


def do_run_template(template_id):
    """手动运行选中模板：生成计划任务并走执行 + 审批流程

    Returns:
        (状态, 进度 Markdown, 提示音 HTML, 生成器或 None) —— 与 outputs
        [template_action_status, template_exec_display, template_exec_audio,
         template_exec_state] 一一对应（契约同 do_execute_plan）。
    """
    if not template_id:
        return "⚠️ 请先选择一个模板", "模板执行进度将显示在此", "", None
    try:
        gen = task_executor.execute_template(int(template_id), triggered_by="manual")
    except Exception as e:
        return f"❌ 启动模板执行失败：{e}", "", "", None
    return _advance_execution(gen, None)


def do_approve_template_pending(gen_state):
    """批准模板执行中当前待审阅的结果（复用 _advance_execution）"""
    if not gen_state:
        return "⚠️ 当前没有待审批的结果，请先选择模板并点击「▶️ 运行」", \
            "模板执行进度将显示在此", "", None
    return _advance_execution(gen_state, True)


def do_reject_template_pending(gen_state):
    """拒绝模板执行中当前待审阅的结果（复用 _advance_execution）"""
    if not gen_state:
        return "⚠️ 当前没有待审批的结果，请先选择模板并点击「▶️ 运行」", \
            "模板执行进度将显示在此", "", None
    return _advance_execution(gen_state, False)


def do_check_scheduled_templates(trigger_source: str = "timer"):
    """定时调度入口：每 60 秒检查并执行到期的任务模板

    说明：真正耗时的工作（搜索/整理）放入独立后台线程执行，避免阻塞
    Gradio 事件队列；线程内全量异常捕获，任何失败只打日志不影响其它组件。
    防重入由 TaskExecutor.check_scheduled_templates 内部「先更新 last_run_at
    再执行」保证，同一周期不会重复触发。
    返回状态 Markdown（显示最近一次调度检查时间）。

    Args:
        trigger_source: 触发源，原样传给 check_scheduled_templates，只影响
            **这次跑要不要落一行 template_runs**（P7 批次 14）：界面定时器用默认
            "timer"（空转不记行），开机补算路径（app.py）传 "boot"（每次必记一行，
            它是"上次开机几点"的分母）。界面回调由 gr.Timer 以 `inputs=[]` 触发，
            因此默认值就是它的实际取值。
    """
    def _runner():
        try:
            results = task_executor.check_scheduled_templates(
                trigger_source=trigger_source)
            for r in results:
                # 日志用 ASCII 状态标记，消息经 log_safe 剔除 emoji，
                # 兼容 GBK 控制台编码
                tag = "OK" if r.get("ok") else "FAIL"
                print(f"[TaskTemplate] 定时触发「{r.get('template_name')}」"
                      f"{tag} {log_safe(r.get('message'))}")
        except Exception as e:
            print(f"[TaskTemplate] 定时检查失败（忽略）: {e}")
    threading.Thread(target=_runner, daemon=True,
                     name="template-scheduler").start()
    return (f"⏳ 定时调度运行中（最近检查 {datetime.now().strftime('%H:%M:%S')}；"
            "到期的每日/每周模板将自动触发并产生待审批项）")


# ---------- 本模板产物（只读，P7 批次 1c-③） ----------
#
# 为什么需要这一块：模板产出的笔记 `goal_id` 是虚拟目标（`template_goal_id(id)`，
# 形如 template_1），而「笔记」页的目标下拉框来自 `goal_manager.list_goals()` ——
# 读的是 `goals` 表，虚拟目标**不是一行 goal**，永远不出现在那里。结果是产物批准
# 落盘后在界面上没有任何入口。
#
# 为什么不把虚拟目标塞进「笔记」页的下拉框：那条路会立刻波及 `select_goal` /
# `refresh_goal_detail` 以及一批按 goal_id 取目标的回调（如
# `goals_controller.select_goal` 会调 `goal_manager.get_goal("template_1")` 得到
# None 并显示"目标不存在"），是一次跨页面的改动。放在模板 Tab 内部则完全自包含：
# 只读、零 schema 变更、可整体回退。


def do_load_template_notes(template_id):
    """列出某模板名下的产物笔记（只读）

    Args:
        template_id: 当前选中的模板 ID

    Returns:
        (产物概览 Markdown, 产物下拉框 update) —— 与 outputs 一一对应
    """
    empty_dd = gr.update(choices=[], value=None)
    if not template_id:
        return "选择模板后在此列出它产出的笔记", empty_dd
    goal_id = template_goal_id(int(template_id))
    try:
        notes = NoteStore.get_notes_by_goal(goal_id)
    except Exception as e:
        print(f"[TaskTemplate] 产物列表加载失败（忽略）: {e}")
        return f"⚠️ 产物列表加载失败：{e}", empty_dd
    if not notes:
        return (f"该模板（`{goal_id}`）名下暂无产物 —— 模板产出要经「待审批」"
                "确认后才会落盘到这里。"), empty_dd

    lines = [f"**`{goal_id}` 名下共 {len(notes)} 篇产物：**", ""]
    for n in notes:
        title = n.get("title") or "（无标题）"
        created = str(n.get("created_at") or "")[:16]
        lines.append(f"- #{n.get('id')} {title}（{created}）")
    choices = [(f"#{n.get('id')}｜{(n.get('title') or '无标题')[:24]}", n.get("id"))
               for n in notes]
    return "\n".join(lines), gr.update(choices=choices, value=None)


def do_view_template_note(note_id):
    """展示某条产物笔记的**全文**（只读）

    刻意不做截断（`render_note_preview` 会按预览长度截）：这一块存在的意义就是
    "落盘后的产物在界面上读得到"，而「资源」格正是要通读的一份报告。
    """
    if not note_id:
        return "选择一条产物后在此显示正文"
    try:
        note = NoteStore.get_note(int(note_id))
    except Exception as e:
        return f"⚠️ 读取失败：{e}"
    if not note:
        return "⚠️ 笔记不存在或已被删除"
    head = (f"### 📄 {note.get('title') or '（无标题）'}\n\n"
            f"> 笔记 #{note.get('id')}｜目标：{note.get('goal_id')}｜"
            f"创建时间：{str(note.get('created_at') or '')[:19]}\n\n---\n\n")
    return head + (note.get("content") or "（内容为空）")
