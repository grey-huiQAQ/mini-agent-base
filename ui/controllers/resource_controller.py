# -*- coding: utf-8 -*-
"""ui.controllers.resource_controller —— 资源机会（含 AI 筛选 / 画像）

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""
import gradio as gr

from core.resource_scout import (
    ResourceScout,
    OpportunityStatus,
    EligibilityHint,
    RelevanceStatus,
    TYPE_LABELS,
    STATUS_LABELS,
    ELIGIBILITY_LABELS,
    RELEVANCE_LABELS,
    STUDENT_TYPE_LABELS,
    effective_hint,
    deadline_display,
    deadline_weekday_notice,
    notes_segments,
    sort_opportunities_for_panel,
    build_session_bill,
)
from core import ai_filter as ai_filter_module
import json
import threading
from memory.database import get_user_profile, update_user_profile

from ui.controllers._shared import (
    ai_filter_state,
    opportunity_scan_state,
    resource_scout,
)




def refresh_opportunities_panel(current_id=None, eligibility="all",
                                status="all"):
    """刷新机会列表（概览 Markdown + 下拉框），保留当前选中值

    Args:
        current_id: 当前选中的机会 ID（保留选中，消失则置空）
        eligibility: 资格筛选（'all' 不过滤；likely_yes / likely_no / unknown）
        status: 状态筛选（'all' 不过滤；new / interested / ignored /
            favorited，用于查看历史机会，默认全部）

    Returns:
        (机会列表 Markdown, 下拉框 update) —— 与 outputs 一一对应
    """
    filter_val = None if eligibility in (None, "", "all") else str(eligibility)
    filter_label = ELIGIBILITY_LABELS.get(filter_val, "") if filter_val else ""
    status_val = None if status in (None, "", "all") else str(status)
    status_label = STATUS_LABELS.get(status_val, "") if status_val else ""
    try:
        rows = ResourceScout.list_opportunities(limit=200,
                                                eligibility=filter_val,
                                                status=status_val)
    except Exception as e:
        print(f"[ResourceScout] 机会列表加载失败（忽略）: {e}")
        return "⚠️ 机会列表加载失败", gr.update(choices=[], value=None)
    # 列表顶部附上一行最近一次扫描的简要结果（有报告时）
    report = opportunity_scan_state.get("report")
    head = ""
    if report:
        head = f"> 🔍 最近扫描：{report.get('summary_text', '')}\n"
    if not rows:
        if filter_val and not status_val:
            empty_md = (head +
                        f"该资格筛选（{filter_label}）下暂无机会；可切换为「全部」"
                        "查看完整列表，或点击「🔍 立即扫描」发现新机会")
            return empty_md, gr.update(choices=[], value=None)
        if status_val and not filter_val:
            empty_md = (head +
                        f"该状态筛选（{status_label}）下暂无机会；"
                        "可切换为「全部」查看完整列表，或点击「🔍 立即扫描」"
                        "发现新机会")
            return empty_md, gr.update(choices=[], value=None)
        if filter_val or status_val:
            empty_md = (head +
                        f"资格筛选（{filter_label}）+ 状态筛选（{status_label}）"
                        "下暂无机会；可放宽筛选条件后查看")
            return empty_md, gr.update(choices=[], value=None)
        return head + "暂无资源机会（点击「🔍 立即扫描」从外部发现学生可用的机会；" \
                      "扫描需联网，可能耗时数秒到数十秒）", \
            gr.update(choices=[], value=None)

    filters_desc = []
    if filter_val:
        filters_desc.append(f"资格筛选：{filter_label}")
    if status_val:
        filters_desc.append(f"状态筛选：{status_label}")
    suffix = (f"（{'；'.join(filters_desc)}）" if filters_desc
              else "（报名截止未过的按报名截止日先后排前，其余按发现时间倒序）")
    lines = [head, f"**共 {len(rows)} 条机会{suffix}：**", ""]

    # P4-10 子项 2：默认视图按**两侧一致的保守派生值**分组，likely_no 组折叠。
    # 只在"未显式筛资格/状态"（即默认视图）时折叠：用户一旦主动按资格或状态
    # 筛选，他要看的就正是那一组，折叠会挡住他自己点名要看的东西。
    fold_no = not filter_val and not status_val
    visible, folded = [], []
    for r in rows:
        eff = effective_hint(r.get("eligibility_hint"), r.get("ai_eligibility"))
        (folded if (fold_no and eff == EligibilityHint.LIKELY_NO)
         else visible).append(r)

    # P4-11：可见部分里"报名截止未过"的项按 deadline 升序排最前（最紧急先看到），
    # 其余（无报名截止日=未知 / 报名截止日已过）保持原有"按发现时间倒序"跟在后面。
    # **已过项只排到末尾，不删除、不隐藏、不改 status**（本面板从不隐藏机会）。
    # 折叠组不参与排序：它本就是"被判定不符合条件"的一堆，按紧急度重排没有意义，
    # 而且那是用户主动展开去翻的一栏。
    visible = sort_opportunities_for_panel(visible)

    for r in visible:
        lines.extend(_render_opportunity_card(r))

    if folded:
        # 折叠成一行摘要 + 一步可达的展开入口。
        # 实现说明（实测依据，2026-09-13）：本面板是**纯 Markdown 输出**
        # （ui/app_factory.py:612 opportunity_list_display = gr.Markdown），
        # 回调无法返回 gr.Accordion（那是布局期组件，需改组件树与本文件全部
        # 回调的 outputs 元数——当前 8 个调用点，数量以复跑为准，不写死）——
        # 而本子项明确要求"不改动面板整体结构"。
        # 实测 Gradio 6.24 的前端标签白名单（markdown-code chunk 的 Tt 列表）
        # 含 details / summary，故 sanitize_html=True 下 <details> 不被转义，
        # 是原生 disclosure 控件：零 JavaScript、一次点击展开。
        lines.append(f"> ▸ 已折叠 {len(folded)} 条"
                     f"（AI 判定不符合你的条件）— 点击展开")
        lines.append("")
        lines.append("<details>")
        lines.append(f"<summary>展开 {len(folded)} 条已折叠机会"
                     f"（资格提示：可能不符）</summary>")
        lines.append("")
        for r in folded:
            lines.extend(_render_opportunity_card(r))
        lines.append("</details>")
        lines.append("")

    choices = [(f"#{r['id']}｜{r.get('title', '')[:40]}"
                f"（{TYPE_LABELS.get(r.get('type'), r.get('type'))}）", r["id"])
               for r in rows]
    ids = {r["id"] for r in rows}
    value = current_id if current_id in ids else None
    return "\n".join(lines), gr.update(choices=choices, value=value)


def _render_opportunity_card(r: dict) -> list:
    """渲染单条机会的展示行（原有逐行拼装逻辑，逐字迁出，行为不变）

    抽成独立函数的唯一目的是让"渲染哪一组"这个决定在
    refresh_opportunities_panel 里显式可见（折叠组与可见组共用同一渲染），
    保证折叠组里的卡片与未折叠时完全一致。
    """
    lines: list = []
    type_label = TYPE_LABELS.get(r.get("type"), r.get("type") or "其他")
    status_label = STATUS_LABELS.get(r.get("status"), r.get("status", ""))
    elig_hint = r.get("eligibility_hint")
    elig_label = ELIGIBILITY_LABELS.get(elig_hint, elig_hint or "❓ 未知")
    title = (r.get("title") or "").replace("|", "｜")
    summary = ((r.get("summary") or "").replace("\n", " ").replace("|", "｜"))
    if len(summary) > 150:
        summary = summary[:150] + "…"
    lines.append(f"- #{r['id']} **[{type_label}]** {title}　"
                 f"{status_label}　🎯 {elig_label}")
    # 资格判断原因：仅在 likely_no / likely_yes 且有原因时展示（v0.3）
    elig_reason = (r.get("eligibility_reason") or "").strip()
    if elig_reason and elig_hint in (EligibilityHint.LIKELY_NO,
                                     EligibilityHint.LIKELY_YES):
        lines.append(f"  💬 {elig_reason.replace('|', '｜')}")
    # 相关性粗筛结果与软条件提示（v0.4：扫描入库行才有值）
    rel_status = r.get("relevance_status")
    rel_bits = []
    if rel_status in (RelevanceStatus.RELEVANT,
                      RelevanceStatus.IRRELEVANT):
        rel_score = float(r.get("relevance_score") or 0)
        rel_label = RELEVANCE_LABELS.get(rel_status, rel_status or "❓ 未知")
        rel_bits.append(f"相关性：{rel_label}（{rel_score:.2f}）")
    matched_raw = r.get("matched_keywords") or "[]"
    try:
        matched = json.loads(matched_raw) if matched_raw else {}
    except Exception:
        matched = {}
    soft = matched.get("soft") if isinstance(matched, dict) else []
    if soft and rel_status != RelevanceStatus.IRRELEVANT:
        rel_bits.append("注意：" + "、".join(str(s) for s in soft[:5]))
    if rel_bits:
        lines.append("  🔍 " + "｜".join(rel_bits))
    # AI 深度筛选结果（ai_screened_at 非空才展示；v0.4）
    if (r.get("ai_screened_at") or "").strip():
        ai_bits = []
        ai_elig = r.get("ai_eligibility")
        ai_label = ELIGIBILITY_LABELS.get(ai_elig, ai_elig or "❓ 未知")
        ai_bits.append(f"资格：{ai_label}")
        ai_value = r.get("ai_value_score")
        if ai_value is not None:
            ai_bits.append(f"价值 {float(ai_value):.2f}")
        ai_reuse = r.get("ai_project_reuse")
        if ai_reuse is not None:
            ai_bits.append(f"可复用 {float(ai_reuse):.2f}")
        ai_rec = (r.get("ai_recommendation") or "").strip()
        if ai_rec:
            ai_bits.append(ai_rec)
        ai_reason = (r.get("ai_reason") or "").replace("|", "｜")
        if len(ai_reason) > 100:
            ai_reason = ai_reason[:100] + "…"
        if ai_reason:
            ai_bits.append(ai_reason)
        lines.append("  🤖 AI 评估：" + "｜".join(ai_bits))
    if r.get("source_url"):
        lines.append(f"  🔗 {r['source_url']}")
    # 一手源的展示分段（opportunities.notes：类目 / 来源冲突 / 抓取缺口等，见
    # core.resource_scout.notes_segments）。零 schema 变更下 notes 是唯一的展示
    # 载体，故这里逐段铺开而不是只取类目——**冲突与缺口恰恰是最该被看到的两段**。
    for seg in notes_segments(r.get("notes")):
        lines.append(f"  🏷️ {seg.replace('|', '｜')}")
    if summary:
        lines.append(f"  📄 {summary}")
    # P4-11 报名截止日：deadline 为空（未知）时**整项不显示**——"未知"不该
    # 伪装成一条看得见的信息。注意 "🕘 发现" 是"你扫到它的时刻"，与紧迫度无关，
    # 两者语义不同故分行展示。文案一律写明"报名截止"（P6-1①）：项目里还有另一
    # 根轴（作品提交截止），裸"截止"会被读成哪一个完全取决于读者。
    dl_text = deadline_display(r.get("deadline"))
    if dl_text:
        lines.append(f"  {dl_text}（报名截止日 {r['deadline']}）")
    # P6-2：周中报名截止 = 提问，不是判死。这里只追加一句"要确认"，
    # **不**输出"已过期 / 无法参加 / 已错过"式判定（该禁令由措辞快照测试锁死）。
    # 已过去的日期不提问——理由见 core.resource_scout.deadline_weekday_notice。
    weekday_notice = deadline_weekday_notice(r.get("deadline"))
    if weekday_notice:
        lines.append(f"  {weekday_notice}")
    lines.append(f"  🕘 发现 {str(r.get('discovered_at') or '')[:19]}")
    lines.append("")
    return lines


def do_scan_opportunities():
    """后台启动一轮资源扫描（真实联网放入后台线程，界面立即返回不阻塞）

    Returns:
        状态 Markdown（提示已启动 / 已有扫描进行中）
    """
    if opportunity_scan_state.get("running"):
        return ("⚠️ 已有资源扫描正在运行，请稍候；"
                "完成后点击「🔄 刷新列表」查看新增机会")

    def _worker():
        try:
            report = resource_scout.scan_once()
            opportunity_scan_state["report"] = report
            print(f"[ResourceScout] 扫描完成：{report.get('summary_text')}")
        except Exception as e:
            opportunity_scan_state["error"] = str(e)
            print(f"[ResourceScout] 扫描异常（忽略）: {e}")
        finally:
            opportunity_scan_state["running"] = False

    opportunity_scan_state.update(running=True, report=None, error=None)
    threading.Thread(target=_worker, daemon=True,
                     name="resource-scout").start()
    return ("🔍 已启动资源扫描（后台运行中，不会阻塞界面；联网搜索可能耗时"
            "数秒到数十秒，完成后点击「🔄 刷新列表」查看新增机会）")


def do_ai_filter_opportunities(limit: int = 5):
    """后台启动 AI 深度筛选（LLM 调用放入后台线程，界面立即返回不阻塞）

    属于用户**手动触发**的人工辅助动作（辅助型自主）：
    只评估"前 N 条"候选（相关性粗筛 ≥ 0.3、资格非 likely_no、尚未筛过），
    单条失败不影响其它条；完成后写 ai_filter_state，由 ai_filter_timer
    （每 5 秒）轮询检测到后**自动刷新机会列表**并展示 AI 评估结果，
    无需手动点击「🔄 刷新列表」。

    Returns:
        状态 Markdown（提示已启动 / 已有筛选进行中）
    """
    if ai_filter_state.get("running"):
        return ("⚠️ 已有 AI 深度筛选在运行，请稍候；"
                "完成后机会列表将自动刷新展示 AI 评估结果")

    def _worker():
        try:
            report = ai_filter_module.ai_filter_opportunities(limit=limit)
            ai_filter_state["report"] = report
            print(f"[AI筛选] {report.get('summary_text')}")
        except Exception as e:
            ai_filter_state["error"] = str(e)
            print(f"[AI筛选] 异常（忽略）: {e}")
        finally:
            ai_filter_state["running"] = False

    ai_filter_state.update(running=True, report=None, error=None)
    threading.Thread(target=_worker, daemon=True, name="ai-filter").start()
    return ("🤖 已启动 AI 深度筛选（前 5 条候选：相关性 ≥ 0.3、资格非"
            "「可能不符」、未筛过）；模型调用需联网且单条约数十秒，"
            "完成后机会列表将自动刷新展示 AI 评估结果")


def do_set_opportunity_status(opportunity_id, status, eligibility="all",
                              status_filter="all"):
    """把选中的机会标记为指定状态（人工确认动作，写入 opportunities 表）

    Args:
        eligibility: 当前资格筛选值（刷新列表时保持同一筛选视图）
        status_filter: 当前状态筛选值（v0.4 历史机会查看；标记后保持在
            同一状态视图——若机会因此离开该视图，则列表自动移除并提示）

    Returns:
        (状态提示, 列表 Markdown, 下拉框 update) —— 与 outputs 一一对应
    """
    if not opportunity_id:
        return "⚠️ 请先在下拉框中选择一条机会", \
            *(refresh_opportunities_panel(None, eligibility, status_filter))
    try:
        opp = ResourceScout.get_opportunity(int(opportunity_id))
    except Exception as e:
        return f"❌ 操作失败：{e}", \
            *(refresh_opportunities_panel(None, eligibility, status_filter))
    if not opp:
        return "⚠️ 机会不存在或已被删除", \
            *(refresh_opportunities_panel(None, eligibility, status_filter))
    ok = ResourceScout.update_status(int(opportunity_id), status)
    label = STATUS_LABELS.get(status, status)
    msg = (f"✅ 已把 #{opp['id']}「{opp['title'][:40]}」标记为：{label}"
           if ok else "❌ 状态更新失败")
    panel_md, dd_update = refresh_opportunities_panel(int(opportunity_id),
                                                      eligibility,
                                                      status_filter)
    return msg, panel_md, dd_update


def do_save_user_profile(student_type, has_school_email, region, age):
    """保存用户画像设置，并按新画像重算全部存量机会的资格提示（用户主动操作）

    画像设置属于用户主动修改自身信息，直接写库无需二次确认；
    保存后调用 recompute_eligibility 让已有机会立即套用新画像
    （仅更新资格提示/原因，绝不删除、不自动忽略任何机会）。

    Args:
        student_type: 学生类型键（high_school/undergraduate/graduate/other）
        has_school_email: 是否有学校/教育邮箱（bool）
        region: 所在地区文本
        age: 年龄（Number 组件可能传 None / 空串 / 数字）

    Returns:
        (状态提示 Markdown, 机会列表 Markdown, 下拉框 update)
        —— 与 outputs 一一对应
    """
    st = student_type or "high_school"
    if st not in STUDENT_TYPE_LABELS:
        st = "high_school"
    reg = (region or "").strip() or "中国"
    age_val = None
    try:
        if age not in (None, ""):
            age_val = int(float(age))
    except (TypeError, ValueError):
        age_val = None
    if age_val is not None and age_val <= 0:
        age_val = None
    try:
        saved = update_user_profile(student_type=st,
                                    has_school_email=bool(has_school_email),
                                    region=reg, age=age_val)
    except Exception as e:
        print(f"[ResourceScout] 画像保存失败（忽略）: {e}")
        return f"❌ 画像保存失败：{e}", \
            *(refresh_opportunities_panel(None, "all"))
    try:
        n = ResourceScout.recompute_eligibility(saved)
    except Exception as e:
        print(f"[ResourceScout] 资格重算失败（忽略）: {e}")
        n = 0
    panel_md, dd_update = refresh_opportunities_panel(None, "all")
    email_label = "有学校邮箱" if saved["has_school_email"] else "无学校邮箱"
    age_label = f"｜年龄 {saved['age']}" if saved.get("age") else ""
    msg = (f"✅ 画像已保存：{STUDENT_TYPE_LABELS.get(st, st)}｜{email_label}｜"
           f"地区 {saved['region']}{age_label}；"
           f"已按新画像重算 {n} 条机会的资格提示。")
    return msg, panel_md, dd_update


def do_recompute_relevance(eligibility="all", status="all"):
    """重新计算全部存量机会的相关性粗筛结果（v0.4 存量修复，用户手动触发）

    对 opportunities 表每条记录按其 title + summary 重新执行
    ResourceScout.recompute_relevance()（确定性关键词规则、零 LLM 成本，
    更新 relevance_status / relevance_score / matched_keywords 三列，
    不触碰资格提示 / 状态 / 画像等其它字段），完成后立即刷新列表——
    修复 v0.4 落库前的旧记录（迁移默认 0.0 / unknown）导致「AI 深度筛选」
    候选为 0 的问题。纯规则计算毫秒级完成，同步执行无需后台线程
    （与「保存画像并重算资格」同一模式：完成后返回提示 + 刷新后列表）。

    Args:
        eligibility: 当前资格筛选值（刷新列表时保持同一筛选视图）
        status: 当前状态筛选值（v0.4 历史机会查看；保持同一状态视图）

    Returns:
        (状态提示 Markdown, 机会列表 Markdown, 下拉框 update)
        —— 与 outputs 一一对应
    """
    try:
        n = ResourceScout.recompute_relevance()
    except Exception as e:
        print(f"[ResourceScout] 相关性重算失败（忽略）: {e}")
        return f"❌ 相关性重算失败：{e}", \
            *(refresh_opportunities_panel(None, eligibility, status))
    panel_md, dd_update = refresh_opportunities_panel(None, eligibility,
                                                      status)
    if n <= 0:
        msg = "⚠️ 无机会可重算（opportunities 表为空）"
    else:
        msg = (f"✅ 已重新计算 {n} 条机会的相关性"
               "（状态/分数/命中关键词已更新；相关性 ≥ 0.3 且资格非"
               "「可能不符」的未筛机会现可被 AI 深度筛选选中）")
    return msg, panel_md, dd_update


def do_cleanup_overdue(eligibility="all", status="all"):
    """一键清理过期比赛：把报名截止日已过的机会批量置为「已忽略」（用户手动触发）

    形态与 do_recompute_relevance 一致（同步执行 → 状态提示 + 刷新后列表）：
    只发一条确定性 UPDATE，毫秒级完成，不需要后台线程。

    **必须让用户预期到"列表看起来没变"**：本面板从不隐藏机会，已忽略的条目仍然
    照常列在那里；真正变化的是「资源」格与「会话启动对账单」——它们只认
    `_BILL_VISIBLE_STATUSES`，不再反复提这些已经过期的条目。

    Args:
        eligibility: 当前资格筛选值（刷新列表时保持同一筛选视图）
        status: 当前状态筛选值（保持同一状态视图）

    Returns:
        (状态提示 Markdown, 机会列表 Markdown, 下拉框 update)
        —— 与 outputs 一一对应
    """
    try:
        n = ResourceScout.ignore_overdue()
    except Exception as e:
        print(f"[ResourceScout] 过期清理失败（忽略）: {e}")
        return f"❌ 过期清理失败：{e}", \
            *(refresh_opportunities_panel(None, eligibility, status))
    panel_md, dd_update = refresh_opportunities_panel(None, eligibility, status)
    if n <= 0:
        msg = ("⚠️ 没有可清理的条目（没有「报名截止日已过」且仍可见的机会；"
               "截止日未知的不在本按钮范围内）")
    else:
        msg = (f"✅ 已把 {n} 条报名截止日已过的机会置为「已忽略」"
               "（列表看起来没变是正常的：本面板从不隐藏机会；"
               "它们已不再进「资源」格与启动对账单）")
    return msg, panel_md, dd_update


def poll_ai_filter_results(eligibility="all", status="all"):
    """AI 深度筛选完成后的**自动刷新**轮询（由 gr.Timer 每 5 秒 tick 调用）

    后台线程执行完 LLM 筛选后把结果写入 ai_filter_state（running→False、
    report 含 候选/成功/失败 统计与错误明细）。本函数检测到「本轮已结束
    且结果尚未展示」时：立即刷新机会列表（已落库的 ai_* 评估结果随列表
    展示，保持当前资格 + 状态筛选视图），并返回状态行（成功统计 +
    失败原因，部分失败时可见）；空闲 / 运行中返回 gr.update() 占位
    （不打扰界面）。与深度搜索面板的 search_timer（每 5 秒）自动刷新
    同一模式——解决"筛选完成但 UI 不更新、需手动点刷新"的问题。

    Returns:
        (状态 Markdown, 机会列表 Markdown, 下拉框 update) —— 与 outputs 对应
    """
    if ai_filter_state.get("running"):
        return gr.update(), gr.update(), gr.update()
    report = ai_filter_state.get("report")
    error = ai_filter_state.get("error")
    if not report and not error:
        return gr.update(), gr.update(), gr.update()
    # 消费本次结果：仅自动刷新一次（防止 timer 每 5 秒重复触发）；
    # AI 结果已落库，消费后手动「🔄 刷新列表」仍可正常展示
    ai_filter_state["report"] = None
    ai_filter_state["error"] = None
    md, dd_update = refresh_opportunities_panel(None, eligibility, status)
    if error:
        return f"❌ AI 深度筛选失败：{str(error)[:200]}", md, dd_update
    text = f"✅ {report.get('summary_text') or 'AI 深度筛选完成'}"
    errors = report.get("errors") or []
    if errors:
        detail = "；".join(str(e)[:90] for e in errors[:2])
        text += f"　失败明细：{detail}"
    return text, md, dd_update


def load_session_bill():
    """会话启动时生成「对账单」（P4-14 子项 ②；由 app_factory 的 demo.load 链尾调用）

    定位：**会话启动触发的无状态查询**——不记"上次看到哪"，不写库、不改 status、
    不把任何东西标成"已看过"。定时触发（`scheduler.py`）在它之上，不是它的替代：
    本回调和它下面的 `build_session_bill` 每次都从当前数据重算，幂等、可重复。

    为什么放在资源机会控制器（L2 层）而不是 core：`build_session_bill` 是纯函数
    放在 core（可单测、无 UI 依赖），而"读库 + 拼 UI 输出"这层胶水属于 UI。
    core 不 import ui，方向不反。

    Returns:
        (对账单 Markdown, 容器可见性 update) —— 与 app_factory 的两个 outputs 对应；
        账单为空串时把容器设为不可见（而不是留一块空 Markdown 占位）。
    """
    try:
        rows = ResourceScout.list_opportunities(limit=500)
    except Exception as e:
        print(f"[ResourceScout] 对账单读取失败（忽略）: {e}")
        return "", gr.update(visible=False)
    try:
        text = build_session_bill(rows)
    except Exception as e:
        print(f"[ResourceScout] 对账单生成失败（忽略）: {e}")
        return "", gr.update(visible=False)
    if not text:
        return "", gr.update(visible=False)
    return text, gr.update(visible=True)
