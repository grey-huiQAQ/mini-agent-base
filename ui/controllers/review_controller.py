# -*- coding: utf-8 -*-
"""ui.controllers.review_controller —— 🧭 项目复盘（P7 第一闭环 Phase 3c-乙，2026-10-06）

职责（只做界面这一侧的几件事，业务规则一条都不在这里写）：
- **复盘列表**：把 `decisions` 账本读出来给人看，并明说「有几条还等人定」；
- **全文回读**：选中一条 → 标题／摘要／状态／作用域＋**全部条目**（类别、出处、时间），
  刻意不截断（照 `templates_controller.do_view_template_note` 那条口径：这一块存在的
  理由就是「账本上的东西在界面上读得到」，截断等于把它变回读不到）；
- **讨论输入**：把人写的东西登记成条目，并按状态机把 proposed／insufficient_information
  推进到 discussed。其中 `prior_intent`（「我当时的判断」）是**可选**的一格；
- **「批准」「否决」「标记已执行」「评估」「提炼经验」**：五个按钮都只调
  `decision_store`（`set_status()`／`mark_executed()`／`evaluate()`／`link_experience()`），
  状态机说什么它们显示什么。「批准」是「标记已执行」的前置，「标记已执行」是「评估」的
  前置，「评估」是「提炼经验」的前置（4a 的回流写点定在 `evaluate()` **之后**），
  **「否决」是给用户说「不」的那一格**——没有它，这一页只能表达同意（设计席 2026-10-07
  对偏离 4 的裁定，见 `do_review_reject` 的 docstring）。
  后两格是设计席 2026-10-07 **裁决 2** 补的：闭环「已执行 → 已评估 → 已提炼」那两跳
  当时没有界面出口，缺了它 4b 无米下锅（`link_experience` 落地后一直没有生产调用方）。

**回调全部集中在本文件**：拆分文档 3c 的备注写了「他之后要重整 UI，这个 Tab 要能整体
搬走」，所以 `ui/app_factory.py` 里只放组件与事件绑定，读数与推进状态的入口只有本文件
导出的这几个函数。

本模块**不 import agents、不调模型**：复盘「怎么跑起来」是 3c-甲 在执行侧的事
（`long_term_review` 模板被触发 → `TaskExecutor.handle_long_term_review_task`）。
这一格只读账本、只按状态机推进，所以零网络、零 LLM。

错误语义跟着 `core.decision_store` 那三层：
- **规则违规它 raise ValueError**（非法转换、跳批准直接标已执行……）⇒ 本函数**接成一行
  人能读的状态文案**，不让异常冒到 Gradio 的事件循环上（按钮点了崩、界面只剩一条
  traceback 不是「响」，那是把错误推回给用户看热闹）；
- **记录不存在它返回 None／False** ⇒ 这里照实说「没这一条」；
- **DB 层异常照常抛** ⇒ 这一类不接（接了就是把「账本读不出来」说成「没有账」，
  而 `list_decisions` 那条读取路径除外：列表读失败只打印并显示一行警告，
  因为它没有任何写副作用，最坏是这一屏没刷新）。
"""

from typing import Any, Dict, Optional

import gradio as gr

# ui=3 引 core=0 是正向依赖（见 checks/check_import_direction.py 的层序）
from core import decision_store as ds
from core import long_term_project_review as review
from core import project_state as ps

#: 讨论条目的出处。写在代码里一次，不在每条 item 上各拼一份——
#: 出处将来要改格式时，两处手工保持一致是本项目已经被咬过的形态。
UI_SOURCE = "ui:项目复盘 Tab（用户当场填写）"

#: 人工段那一条的 `decision_field`：**不在界面里写死那串字符**，从
#: `core.project_state.DECISION_ITEM_FIELDS` 里按 kind 的位置取——那张表正是由
#: `"items." + kind for kind in VALID_ITEM_KINDS` 生成的，两处同源才不会分叉
#: （`tests/test_review_tab.py` 有一条把这个名字与 `ps.DECISION_FIELDS` 对上）。
_MANUAL_DECISION_FIELD = ps.DECISION_ITEM_FIELDS[
    list(ds.VALID_ITEM_KINDS).index(ds.KIND_USER_REPORT)]

#: 状态中文标签**不在这里复制一份**：用 3b 已经建好的
#: `core.long_term_project_review.STATUS_LABELS_ZH`（七值齐全）。界面上人读的是
#: 「待讨论（还没批准）」，程序之间传的是 proposed——这条分界是判据①「两个 View
#: 按内容分离」的同一条线，多一处标签表就多一处会说谎的地方。
_STATUS_LABELS = review.STATUS_LABELS_ZH

#: 作用域（跨度）中文标签。词表是**跨度词不是频率词**（Fix A，2026-10-07）：
#: tactical＝只影响这几天、operational＝这一周要落地的改法、strategic＝跨周以上的走向、
#: critical＝不可逆或撞上硬时点。界面这一列刻意**不做「档高一等」的暗示**——四值之间
#: 没有全序，账本上的排序另有依据（`core.decision_store.EXPERIENCE_ELIGIBLE_SCOPES`
#: 是成员集合，不是秩）。
_SCOPE_LABELS_ZH = {
    ds.SCOPE_TACTICAL: "当下调整（只影响这几天）",
    ds.SCOPE_OPERATIONAL: "本周运营（这一周要落地的改法）",
    ds.SCOPE_STRATEGIC: "长期方向（跨周以上的走向）",
    ds.SCOPE_CRITICAL: "关键（不可逆，或撞上硬时点）",
}

#: 复盘结论五值的中文标签。**词表真源不是这里**，是
#: `core.decision_store.VALID_EVALUATIONS`（锚点 §一.4）——本表只做显示层：下拉框给人读
#: 中文，传给 `evaluate()` 的仍是 slug。覆盖关系由 `tests/test_review_tab.py` 逐值比对，
#: 那边加第六值而这里没跟上（或反过来）时那条用例即红。
#: 「wrong_assumption＝当时先采信的那条前提被证伪」这句措辞刻意留在界面上：
#: 它是 3c 那格「我当时的判断」（登记成 assumption）的回收口，看不见就没人在这里填它。
EVALUATION_LABELS_ZH = {
    ds.EVAL_HELPFUL: "有帮助（这条路走对了，下次还这么改）",
    ds.EVAL_NEUTRAL: "说不清（改了，但看不出好坏）",
    ds.EVAL_HARMFUL: "有害（改了之后更糟）",
    ds.EVAL_WRONG_ASSUMPTION: "前提错了（当时先采信的那条被证伪）",
    ds.EVAL_INSUFFICIENT: "信息不够（还评不了，先去补事实）",
}

#: 条目六类的中文标签（含 revision——它不在五类里，但账本读得出来就得看得懂）
_ITEM_KIND_LABELS_ZH = {
    ds.KIND_FACT: "事实",
    ds.KIND_USER_REPORT: "他本人的话",
    ds.KIND_INFERENCE: "有依据的推断",
    ds.KIND_ASSUMPTION: "先采信的前提",
    ds.KIND_UNKNOWN: "明说不知道",
    ds.ITEM_KIND_REVISION: "修订",
}

_EMPTY_LIST_HINT = ("账本里还没有决策。跑一次「🧭 长期项目复盘」模板（手动点「▶️ 运行」"
                    "或到点自动触发）才会立第一条。")
_NO_SELECTION_DETAIL = "选择一条决策后在此显示全文"

#: 还等人定的状态：proposed／discussed／approved／insufficient_information。
#: executed 与 evaluated 是「已经做完／已经评过」，rejected 是「已经否决」，都不再等人。
#: 这个集合只用于列表上那一句「有 N 条等你定」，**不参与任何写判断**（写判断只看状态机表）。
_PENDING_STATUSES = (
    ds.STATUS_PROPOSED, ds.STATUS_DISCUSSED, ds.STATUS_APPROVED,
    ds.STATUS_INSUFFICIENT,
)


def _status_label(status: Any) -> str:
    """状态 → 中文标签；表外值如实说表外，不静默印 slug"""
    return _STATUS_LABELS.get(status, f"表外状态 {status!r}（词表里没有，按原样读）")


def refresh_review_panel(current_id: Optional[int] = None,
                        limit: int = 50):
    """刷新复盘列表（概览 Markdown ＋ 下拉框），保留当前选中值

    判据③「未处理项在启动时可见」落在这里：列表首行就写「有 N 条等你定」，
    并由 `demo.load` 链回填（与待审批面板同族——「重启后仍然要看到没处理完的东西」）。

    Returns:
        (列表 Markdown, 下拉框 update) —— 与 outputs 一一对应
    """
    try:
        decisions = ds.list_decisions(limit=limit)
    except Exception as e:
        print(f"[Review] 决策账本读取失败（忽略）: {e}")
        return "⚠️ 决策账本读取失败，请刷新或查看控制台", gr.update(choices=[], value=None)
    if not decisions:
        return _EMPTY_LIST_HINT, gr.update(choices=[], value=None)

    pending = [d for d in decisions if d.get("status") in _PENDING_STATUSES]
    lines = [f"**账本里共 {len(decisions)} 条决策，其中 {len(pending)} 条等你定"
             f"（待讨论／讨论过／已批准／等信息都算）：**", ""]
    if not pending:
        lines.append("- 没有等你定的了：全部已经走完执行＋复盘，或被否决。\n")
    for d in decisions:
        decision_id = d.get("id")
        title = str(d.get("title") or "（无标题）")
        scope = _SCOPE_LABELS_ZH.get(d.get("decision_scope"),
                                     f"作用域 {d.get('decision_scope')!r}")
        goal = d.get("goal_id") or "没绑目标"
        updated = str(d.get("updated_at") or "")[:16]
        lines.append(f"- #{decision_id}【{_status_label(d.get('status'))}】{title}"
                     f"｜{scope}｜目标 {goal}｜更新于 {updated}")
        summary = str(d.get("summary") or "").strip()
        if summary:
            lines.append(f"  - 建议下一步：{summary}")
    choices = [(f"#{d.get('id')}｜{_status_label(d.get('status'))}｜"
                f"{str(d.get('title') or '')[:24]}", d.get("id"))
               for d in decisions]
    ids = [d.get("id") for d in decisions]
    try:
        value = current_id if current_id is not None and int(current_id) in ids else None
    except (TypeError, ValueError):
        value = None
    return "\n".join(lines), gr.update(choices=choices, value=value)


def do_view_review_decision(decision_id: Optional[int]):
    """全文回读一条决策（只读，**不做预览截断**）

    读的是账本而不是某个内存对象：4a 之后隔一个进程也要能凭 id 把这条读回来，
    界面上这一屏就是那件事的现成入口。

    Returns:
        详情 Markdown（一个字符串，与 outputs 一一对应）
    """
    if not decision_id:
        return _NO_SELECTION_DETAIL
    try:
        decision = ds.get_decision(int(decision_id))
    except Exception as e:
        return f"⚠️ 读取失败：{e}"
    if not decision:
        return "⚠️ 这条决策不存在或已被删除"
    revision_note = ""
    try:
        items = ds.list_items(int(decision_id))
        revision_count = ds.count_revisions(int(decision_id))
    except Exception as e:
        # 条目读不出来时不印「修订 读取失败 次」那种半截话：降级成空列表并单独成行说明
        items, revision_count = [], None
        revision_note = f"\n\n⚠️ 条目读取失败：{e}"

    head = [
        f"### 🧭 Decision #{decision.get('id')}"
        f"｜{_status_label(decision.get('status'))}",
        "",
        f"**{str(decision.get('title') or '（无标题）')}**",
        "",
    ]
    summary = str(decision.get("summary") or "").strip()
    head.append(f"> 建议下一步：{summary if summary else '（账本里这一格是空的）'}")
    head.append("")
    scope_raw = decision.get("decision_scope")
    scope_zh = _SCOPE_LABELS_ZH.get(scope_raw, f"作用域 {scope_raw!r}")
    #: 「能不能提炼成经验」这一行**只做成员判定、不另写判据**：用的就是
    #: `decision_store.EXPERIENCE_ELIGIBLE_SCOPES` 那个集合本身（口径同
    #: `refresh_review_snapshot` 复用 `has_manual_section` 那条——两份判据一旦分叉，
    #: 界面上那句「能提炼」就成了假话）。这里出现的位置在**详情区**而不是在按钮上，
    #: 因为「隐藏按钮」要在界面里另算一遍能不能写，那才是复制规则。
    flowback = ("可提炼成经验" if scope_raw in ds.EXPERIENCE_ELIGIBLE_SCOPES
                else "这一档按定案不回流，点「✨ 提炼经验」会被守卫拒")
    head.append(
        f"作用域：{scope_zh}"
        f"｜经验回流：{flowback}"
        f"｜目标：{decision.get('goal_id') or '没绑目标'}"
        f"｜创建：{str(decision.get('created_at') or '')[:19]}"
        f"｜更新：{str(decision.get('updated_at') or '')[:19]}"
        + (f"｜修订 {revision_count} 次" if revision_count is not None else ""))
    if revision_note:
        head.append(revision_note)
    trace = []
    if decision.get("execution_reference"):
        trace.append(f"执行痕迹：{decision['execution_reference']}")
    if decision.get("evaluation"):
        # 复盘结论这一列在 4a 之前恒为空，所以这条渲染从来没被走到过——它当时直接印
        # slug（wrong_assumption），正是本项目已经点过一次名的形态：「缺标签的后果不是
        # 报错，而是把 slug 原样印给人看」（口径同 `_SCOPE_LABELS_ZH` 与
        # templates_controller 的 `_ACTION_LABELS`）。词表真源仍是
        # `decision_store.VALID_EVALUATIONS`，本表只做显示层；表外值如实说表外。
        eval_raw = str(decision["evaluation"])
        eval_zh = EVALUATION_LABELS_ZH.get(
            eval_raw, f"表外结论 {eval_raw!r}（词表里没有，按原样读）")
        trace.append(f"复盘结论：{eval_zh}"
                     f"｜实际结果：{decision.get('actual_result') or '（空）'}")
    if trace:
        head.append("")
        head.append("｜".join(trace))
    head += ["", "---", "", f"**条目 {len(items)} 条（按登记顺序，原文不截）：**", ""]
    if not items:
        head.append("- （这一条决策还没有任何条目——账本上只有标题与建议下一步）")
    for item in items:
        kind = str(item.get("kind") or "")
        kind_zh = _ITEM_KIND_LABELS_ZH.get(kind, f"表外类别 {kind!r}")
        source = item.get("source")
        source_zh = f"｜出处：{source}" if source else "｜出处：（空）"
        head.append(f"{item.get('id')}. [{kind_zh}] {item.get('content')}"
                    f"{source_zh}｜{str(item.get('created_at') or '')[:19]}")
    return "\n".join(head)


def _after_write(decision_id: Optional[int], status_md: str):
    """写完账本之后的统一返回：状态行 ＋ 刷新列表 ＋ 刷新详情

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    panel_md, dd_update = refresh_review_panel(decision_id)
    return status_md, panel_md, dd_update, do_view_review_decision(decision_id)


def do_review_discuss(decision_id: Optional[int], comment: Optional[str],
                      prior_intent: Optional[str] = None):
    """把人写的讨论登记进账本，并按状态机推进一格

    两格输入的分工是刻意的（这是本 Tab 唯一有语义分界的地方）：
    - `comment` **必填**：登记成 `user_report`（「他本人说过的话」）。空讨论在账本上
      没有可复盘的东西，所以拒；
    - `prior_intent` **可选**：非空时登记成 `assumption`（「没有证据、但当前必须先
      采信的前提」），并带一个「事前意图」前缀。为什么不登记成 `user_report`：
      这一格的用途是 4a 的 `wrong_assumption`——它必须是「当初先采信的那条前提」，
      才有被证伪的资格；一句「他说过什么」不能被证伪，只有一条前提才能。
      留空时**什么都不写**，讨论照常登记、状态照常推进（拆分文档 3c 判据②要的牙齿
      就是这一句：不许有人把它顺手改成必填）。

    状态推进只在 `proposed` 与 `insufficient_information` 两个态发生（它们到
    `discussed` 是状态机上合法的边，后者还是全表唯一那条向后的边＝「信息补齐了重新看」）。
    `approved`／`executed`／`evaluated`／`rejected` 上来讨论的：条目照记（那是事实），
    但**状态不动**——同态重复设置会被 `set_status` 拒，硬推就是把状态机当摆设。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    comment = str(comment or "").strip()
    prior_intent = str(prior_intent or "").strip()
    if not comment:
        return _after_write(decision_id,
                            "⚠️ 讨论内容不能为空——空白讨论在账本上没有可复盘的东西")
    try:
        decision = ds.get_decision(int(decision_id))
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 读取失败：{e}")
    if not decision:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除，没有写入任何东西")

    try:
        comment_item_id = ds.add_item(int(decision_id), ds.KIND_USER_REPORT, comment,
                                      source=UI_SOURCE)
        written = [f"user_report 条目 #{comment_item_id}"]
        if prior_intent:
            intent_item_id = ds.add_item(
                int(decision_id), ds.KIND_ASSUMPTION,
                f"事前意图（我当时这么判断）：{prior_intent}", source=None)
            written.append(f"assumption 条目 #{intent_item_id}")
    except ValueError as e:
        return _after_write(decision_id, f"⚠️ 条目没写进去：{e}")

    status = str(decision.get("status"))
    if status in (ds.STATUS_PROPOSED, ds.STATUS_INSUFFICIENT):
        try:
            ds.set_status(int(decision_id), ds.STATUS_DISCUSSED)
            moved = f"状态 {status} → discussed"
        except ValueError as e:
            moved = f"状态没动（{e}）"
    else:
        moved = (f"状态没动：这条现在是「{_status_label(status)}」，"
                 "讨论已经登记成条目，但要改状态请走批准／执行那两条按钮")

    return _after_write(decision_id,
                        f"✅ 已记入 Decision #{decision_id}："
                        f"{'、'.join(written)}；{moved}")


def do_review_approve(decision_id: Optional[int]):
    """批准一条决策（proposed／discussed → approved）

    这一格是「标记已执行」的**前置**，不是顺手加的：状态机里只有
    `approved → executed` 这条边能进「已执行」（`mark_executed` 对其它当前态一律 raise），
    而账本上产生的行都是 proposed——不给批准入口，「标记已执行」就永远只能报错。
    「批准」的语义就是「最终决策权在用户」这一步（辅助型自主原则），所以它必须是人点的。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    try:
        ok = ds.set_status(int(decision_id), ds.STATUS_APPROVED)
    except ValueError as e:
        return _after_write(decision_id, f"⚠️ 没批准：{e}")
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 没批准（DB 层异常，账本没改）：{e}")
    if not ok:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除")
    return _after_write(decision_id,
                        f"✅ 已批准 Decision #{decision_id}，现在可以「标记已执行」")


def do_review_reject(decision_id: Optional[int]):
    """否决一条决策（proposed／discussed／approved → rejected；rejected 是终态）

    **这一格不是「删除按钮」，也不该是**：`rejected` 那一行仍然留在账本上，被否决这件事
    本身就是第一闭环要评的东西——「它当时建议了 X、他不要」是一条记录，「没这回事」不是。

    为什么必须有这个按钮（设计席 2026-10-07 对偏离 4 的裁定）：状态机里
    `proposed／discussed／approved` **三态都能进** rejected，包括「批准之后反悔」那条边
    （`approved → rejected` 的注释原话：不许反悔的唯一出路是留着那行假装没这回事，
    而那正是闭环要防的假账）。**没有它，用户在界面上说不出「不」**，这套闭环就只能
    学得到同意、学不到拒绝。

    界面**不重复写状态机规则**：哪些当前态能进 rejected，一律由
    `decision_store.set_status()` 判并 raise，本函数只把那句 raise 接成人能读的一行。
    （口径同文件头：多一处判断就多一处会说谎的地方。）

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    try:
        decision = ds.get_decision(int(decision_id))
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 读取失败：{e}")
    if not decision:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除，没有写入任何东西")
    before = str(decision.get("status"))
    try:
        ok = ds.set_status(int(decision_id), ds.STATUS_REJECTED)
    except ValueError as e:
        return _after_write(decision_id,
                            f"🚫 没否决：{e}（当前是「{_status_label(before)}」；"
                            "要推翻已经执行／已经评过的结论，请立一条新决策，"
                            "并在老决策上追加修订条目）")
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 没否决（DB 层异常，账本没改）：{e}")
    if not ok:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除")
    return _after_write(decision_id,
                        f"✅ 已否决 Decision #{decision_id}：状态 "
                        f"「{_status_label(before)}」→「{_status_label(ds.STATUS_REJECTED)}」。"
                        "**账本不删行**——「他不要这条建议」本身就是要复盘的东西。")


def do_review_mark_executed(decision_id: Optional[int],
                            execution_reference: Optional[str] = None):
    """把一条已批准的决策记为已执行（approved → executed），痕迹由人填

    **2026-10-07 裁决 1**：`execution_reference` 走「由人填」这一读法（给这一格补一个
    可选输入框，零 schema 变更）。另外两条读法被否掉了，为什么记在这里：
    - 给任务／审批链加一条显式 `decision_id`：那是「系统自动执行决策」的前置，而自动改代码
      属 §十六 明令暂缓的方向，不是第一闭环的事；
    - 把复盘那一格自己的 `template_runs.id` 当痕迹：**那是来路不是执行**，写进去等于
      造出一列看起来像证据、实际什么都指不到的东西。

    留空仍是合法出口，且这里**把空串归一成 None**（不写 `''`）：`mark_executed` 的既有
    语义是「执行了但没找到可指的东西，比编一个路径出来更诚实」，而库里 NULL 与空串的差别
    就是「没填」与「填了一个空东西」的差别——这一列 DDL 允许 NULL，就照允许的那半做。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    clean_reference = str(execution_reference or "").strip() or None
    try:
        ok = ds.mark_executed(int(decision_id), execution_reference=clean_reference)
    except ValueError as e:
        return _after_write(decision_id,
                            f"⚠️ 没能标记为已执行：{e}（要先点「✅ 批准」）")
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 没能标记为已执行（DB 层异常）：{e}")
    if not ok:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除")
    trace = (f"执行痕迹：{clean_reference}" if clean_reference
             else "执行痕迹留空（这一格可选，留空表示没有可指的东西）")
    return _after_write(decision_id,
                        f"✅ Decision #{decision_id} 已记为已执行｜{trace}"
                        "｜下一步是「📝 评估」——没执行过的决策没有效果可评")


def do_review_evaluate(decision_id: Optional[int], evaluation: Optional[str],
                       actual_result: Optional[str]):
    """评一条已执行的决策：写 evaluation ＋ actual_result，executed → evaluated

    界面**一格判据都不重复写**：五值合法性、`actual_result` 必须非空、当前态必须是
    `executed`，全部由 `core.decision_store.evaluate()` 判并 raise，本函数只把那句 raise
    接成人能读的一行（口径同文件头：多一处判断就多一处会说谎的地方）。
    下拉框没选＝传进来的是 None，也照这条走——不替人默认一个结论。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    try:
        ok = ds.evaluate(int(decision_id), str(evaluation or ""),
                         str(actual_result or ""))
    except ValueError as e:
        return _after_write(decision_id,
                            f"⚠️ 没评成：{e}（顺序是死的：批准 → 标记已执行 → 评估）")
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 没评成（DB 层异常，账本没改）：{e}")
    if not ok:
        return _after_write(decision_id, "⚠️ 这条决策不存在或已被删除，没有写入任何东西")
    label = EVALUATION_LABELS_ZH.get(str(evaluation), str(evaluation))
    return _after_write(decision_id,
                        f"✅ Decision #{decision_id} 已评估：{label}"
                        "｜想把它变成一条经验就点「✨ 提炼经验」，不想变就不点")


def do_review_distill(decision_id: Optional[int], future_effect: Optional[str]):
    """把一条**已评估**的决策提炼成一行经验（4a 的回流写点在界面上的那一格）

    `future_effect` 是**可选**的（裁决 2 原话：写不出明确教训就跳过）。这里对「跳过」的
    实现是**根本不调 `link_experience`**，而不是喂它一个空串：四道守卫里那条
    「空 future_effect 拒」是 4a 定下来的（空教训回流进闭环＝往每轮 system prompt 注入
    一句空气），界面不许为了让按钮好使而绕开它。同理，非空但作用域不回流／还没评估的
    决策，本函数也**不预判**，交给守卫响——测试里那条「界面提示与集合本身一致」只针对
    详情区那一行措辞，不针对写判断。

    Returns:
        (状态 Markdown, 列表 Markdown, 下拉框 update, 详情 Markdown)
    """
    if not decision_id:
        return _after_write(None, "⚠️ 请先在上方选择一条决策")
    lesson = str(future_effect or "").strip()
    if not lesson:
        return _after_write(decision_id,
                            "⏭️ 「下次改什么」留空 ⇒ 这一条**不提炼**：写不出明确教训是"
                            "设计里的正常出口，不是失败。账本与经验表都没动一个字。")
    try:
        experience_id = ds.link_experience(int(decision_id), lesson)
    except ValueError as e:
        return _after_write(decision_id,
                            f"⚠️ 没提炼：{e}（顺序是死的：标记已执行 → 评估 → 提炼；"
                            "作用域不在回流集合里的决策按定案不回流）")
    except Exception as e:
        return _after_write(decision_id, f"⚠️ 没提炼（DB 层异常）：{e}")
    return _after_write(decision_id,
                        f"✅ 已提炼成经验 #{experience_id}（指回 Decision #{decision_id}）"
                        "｜同一决策允许再提炼多条，4b 注入时最多取一条")


def do_save_manual_section(manual_text: Optional[str]) -> str:
    """把「你这周自己的话」存成一份**带人工段**的快照（补上闭环缺的那条写入路径）

    这一格存在的理由（2026-10-07 人工段缺口修复）：复盘的第一道闸门
    （`core.long_term_project_review.has_manual_section`）要的是一条 kind 不为 fact 的条目，
    而生产代码里从来没有地方往 `build_project_state(manual_items=...)` 喂过东西、
    也从来没有地方调 `save_project_state` ⇒ `latest_project_state()` 只能返回 None ⇒
    执行体退回当场现采的那份只含自动段的快照 ⇒ 出口恒为 `manual_section_missing`：
    闭环在第一步就断。`core.project_state.AUTO_UNSERVABLE` 里那句「items.user_report
    自动段读不出，需人工段传入」说的就是同一件事，这一格是它的落地入口。

    三处刻意的边界（设计席 2026-10-07 清单第四节，逐条对着写）：
    - **保存 ≠ 触发**：这里只 build＋save，不调 `run_long_term_project_review`、也不运行
      模板。触发口径仍然只有「🧩 任务模板」那一侧的「▶️ 运行」——那是 3c 的既有定案，
      界面另起一条直调复盘的路会绕开 `template_runs` 那套记账（见 `do_run_review_now`）；
    - **追加式**：每次保存都是 `project_states` 的一行新记录，不 UPDATE、不 DELETE
      （`save_project_state` 本身已是追加语义），下一次复盘经 `latest_project_state()`
      读到的就是最新那一份；
    - **不复制判据**：条目合法性（内容非空、kind 在五类里、字段名在 `DECISION_FIELDS` 里）
      全部由 `core.project_state.validate_state_item` 判并 raise，本函数只把 raise 接成
      一行人在界面上读得懂的话（口径同文件头的三层错误语义）。

    一格自由文本 → 一条 `user_report` 条目：`assumption`／`inference` 的人工段入口
    属 UI 重整时一起设计的东西，这一批不造（清单原文）。原话按**逐字**进观测载荷，
    所以这里只剥掉首尾空白、**不压缩段内换行**——载荷里那一条因此可能占多行，
    那是原话的形状，不是渲染的缺陷。

    目标那一格：本回调**不选目标**（`goal_id=None`，项目级快照）。生产库此刻有两个
    active 目标，采集层解不出唯一目标时按既有语义登记一条 unknown、不报错，
    快照照常落盘；「目标 未绑定」那一句由 `refresh_review_snapshot` 显示，不在这里重说。

    Returns:
        状态 Markdown（**只有一个字符串**）。快照那一格的刷新走事件链上的
        `refresh_review_snapshot`：同一句渲染不许有两个出口。
    """
    text = str(manual_text or "").strip()
    if not text:
        return ("⚠️ 人工段不能为空——空的人工段过不了闸门①，写一句你自己的判断"
                "（这一格不落库：`project_states` 一行都没多）")
    try:
        state = ps.build_project_state(manual_items=[
            (_MANUAL_DECISION_FIELD, ds.KIND_USER_REPORT, text, UI_SOURCE)])
        snapshot_id = ps.save_project_state(state)
    except ValueError as e:
        return (f"⚠️ 没能保存这份快照：{e}"
                "（条目校验由 `core.project_state` 出，界面不复制判据；`project_states` 没动）")
    except Exception as e:
        return f"⚠️ 没能保存这份快照（DB 层异常）：{e}（`project_states` 没写进去）"
    return (f"✅ 已保存快照 #{snapshot_id}（人工段 1 条）"
            "｜下一步：去「🧩 任务模板」跑「长期项目复盘」模板，"
            "它会经 `latest_project_state()` 读回这一份")


def refresh_review_snapshot():
    """启动回填：下次复盘**会读到的那一份** State 是哪一份（只读）

    ⚠️ 这一格的存在理由就是 `latest_project_state()` 的两个形状：
    - **空表返回 None、不抛**（不是异常路径，是「库里还没有快照」）⇒ 必须显示成人读得懂
      的一句话，并且如实说清「这种情况下手动触发不会踩空」：3c-甲 的执行体会当场现采一份
      只含自动段的快照，而那种载荷过不了「人工段」那道闸门，出口是「缺人工段」——
      不写账本、不打模型；
    - 有快照 ⇒ 报 id／时间／条目数，并用**闸门自己那个函数**
      （`core.long_term_project_review.has_manual_section`）预判这一轮的**第一道**过不过。
      注意这一格只管闸门①：过了之后打不打模型、落不落账，还要过闸门②（阶段一改判
      「快照里有没有项目活动读数」，2026-10-11），所以旧文案里那句「这一轮会去观测」
      在新分流下是半句假话，已删——这一格不预告它算不出来的东西。
      预判必须复用同一个函数，不许在界面里另写一份「人工段为空」的判断——两份判据
      一旦分叉，界面上那句「会立决策」就成了假话。

    Returns:
        快照状态 Markdown（一个字符串）
    """
    try:
        state = ps.latest_project_state()
    except Exception as e:
        print(f"[Review] 快照读取失败（忽略）: {e}")
        return f"⚠️ 最近快照读取失败：{e}"
    if state is None:
        return ("📌 **下次复盘会读到的快照**：`project_states` 里一份快照都还没有。"
                "手动触发复盘**不会踩空**——执行体会当场采一份只含自动段的快照，"
                "而只有 `k=v` 事实的载荷过不了「人工段」那道闸门，"
                "所以这一轮的出口是「缺人工段」：不写账本、不打模型。")
    counts: Dict[str, int] = {"fact": 0, "manual": 0}
    unknown_count = len(state.get("unknowns") or [])
    for item in (state.get("items") or []):
        if isinstance(item, dict) and item.get("kind") == ds.KIND_FACT:
            counts["fact"] += 1
        else:
            counts["manual"] += 1
    has_manual = review.has_manual_section(state)
    # 阶段一（2026-10-11）把这一句里「这一轮会去观测」那半句删了：闸门②改判「有没有项目
    # 活动读数」之后，闸门①过了之后走的可能是观测旁路、可能是问句旁路、也可能一条都不打
    # （「先选目标」那一档）。留着它，界面就在预告一个它已经算不出来的东西。
    gate = ("闸门①（人工段）过得了——往后打不打模型、打哪一条旁路，由闸门②按快照里的"
            "项目活动读数决定，这一格不替它预判"
            if has_manual else
            "闸门①（人工段）过不了 ⇒ 这一轮**不会立 Decision、也不会打模型**："
            "请先在「人工段」里写下你自己的判断")
    return (f"📌 **下次复盘会读到的快照**：#{state.get('id')}"
            f"｜{state.get('snapshot_at')}"
            f"｜目标 {state.get('goal_id') or '未绑定'}"
            f"｜自动段 {counts['fact']} 条／人工段 {counts['manual']} 条"
            f"｜未知 {unknown_count} 条"
            f"｜{gate}")


def do_run_review_now():
    """「🧭 怎么跑一次复盘」按钮：只指路＋只消歧，不自己调模型

    为什么这个 Tab 里**没有**直接跑复盘的按钮逻辑：复盘的触发口径归模板系统
    （`long_term_review` 动作 ＋ 3c-甲 的执行分支），跑一次要经过 `execute_template`
    的记账（`template_runs`、`last_run_at`），界面自己另起一条「直接调
    `run_long_term_project_review`」的路径会绕开那套记账，等于造出第二个触发入口
    与一根没人管的水位线。所以这一格只把人指回该点的地方。

    2026-10-07 设计席对偏离 2 的唯一补充：指路文案要把「主动跑一次」= **去任务模板
    那一页点「▶️ 运行」**说死。这一页没有运行按钮，用户很容易读成「这页坏了」，
    所以文案里补一句「这是设计如此，不是坏了」——消歧，不是新功能。
    """
    return ("🧭 这一页**不自己跑复盘**——这是设计如此，不是这一页坏了。"
            "「主动跑一次」的按钮在模板那一侧：去「🧩 任务模板」建一条动作选"
            "「🧭 长期项目复盘」的模板，在那一页点它右边的**「▶️ 运行」**"
            "（或等它到点自动触发）。跑完回到这一页点「🔄 刷新账本」，"
            "就能看到新立的决策行。")
