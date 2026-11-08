# -*- coding: utf-8 -*-
"""
复盘界面（ui/controllers/review_controller.py ＋ ui/app_factory.py 的「🧭 项目复盘」Tab，
P7 第一闭环 Phase 3c-乙，2026-10-06）测试

设计席给 3c 的验收原文是「不是『Tab 能打开』，而是端到端一条链」，所以本文件的**主用例**
是那条链：建 `long_term_review` 模板 → 手动触发 → 复盘跑起来 → 账本里出现 Decision 行 →
界面函数读得到、能讨论、能批准、能否决、能标记已执行。其余用例分别钉 3c-乙 的四条判据：

- 判据①「`create_app()` 能构建且新控件在组件树内」→
  `test_app_import_builds_and_isolated` ＋ `test_review_tab_widgets_are_in_component_tree`；
- 「形状要钉：断言既有 Tab 标签**逐个**仍在」（不是断言数字，数字会随重构漂移）→
  `test_existing_tab_labels_all_still_present`；
- 判据②「**可选性有牙齿**：`prior_intent` 为空时全流程仍产出／走通」→
  `test_end_to_end_review_to_executed_with_empty_prior_intent`（空值走完整条链）
  ＋ `test_prior_intent_written_as_assumption_when_given`（非空时才多一条 assumption，
  两条合起来才证明「可选」不是「被忽略」）；
- 判据③「未处理项启动时可见（复用 `demo.load` 回填）」→
  `test_start_up_backfill_is_in_demo_load_chain`（静态读源码，口径照
  `tests/test_concept_explainer_skill.py`：刻意不去重跑一遍 `create_app()`）
  ＋ `test_pending_count_shown_in_list_header`；
- 判据④（设计席裁定「改的时机是 3c」）**非法动作类型从静默降级改成拒绝** →
  核心侧两条在 `tests/test_project_review_loop.py`（`test_unknown_action_type_is_rejected`
  ／`test_update_template_rejects_invalid_action_without_touching_row`），本文件管界面这一头：
  `test_form_can_create_review_template_without_keyword`（表单能选动作）
  ＋ `test_action_labels_cover_every_valid_action`（补标签，覆盖关系逐个比对）
  ＋ `test_negative_control_bad_action_via_form_writes_nothing`（非法值进来：一行都不写、
  状态文案说清了原因）。

另有一组是 4a 的 **UI 收尾**（设计席 2026-10-07 裁决 1＋2，同一天）：闭环里
「已执行 → 已评估 → 已提炼」那两跳当时只有存储层、没有界面出口，`link_experience` 落地后
**一个生产调用方都没有**，4b 也就无米下锅。补的是三格输入＋两个按钮，判据跟着换成：
- **裁决 1 的牙齿**：执行痕迹**填了要落库、留空要仍是 NULL**（不许写空串、更不许编一个）
  → `test_mark_executed_reference_written_and_blank_stays_null`；
- **裁决 2 的牙齿**：五值下拉与 `VALID_EVALUATIONS` **逐值比对**（加第六值而忘改界面即红），
  而「下次改什么」留空是**跳过**、不是拿空串去撞守卫
  → `test_evaluation_choices_cover_every_valid_evaluation`
  ＋ `test_distill_with_empty_lesson_skips_without_writing`；
- **端到端一条链**（口径与本文件主用例同一句话）：批准 → 标记已执行（带痕迹）→ 评估 →
  提炼 → `experiences` 里真有一行、且各列按锚点 §一.9 落对
  → `test_end_to_end_execute_evaluate_and_distill_flowback`；
- **界面不另写判据**：详情区那句「能不能回流」用的是
  `decision_store.EXPERIENCE_ELIGIBLE_SCOPES` **这个集合本身**，不是界面里复制的一份
  → `test_tactical_decision_flowback_line_matches_the_eligibility_set`。

另有一组是 4a 开工前小批次补的「🚫 否决」那一格（设计席 2026-10-07 对偏离 4 的裁定：
**没有 reject 按钮＝用户无法说「不」**，一处 `set_status('rejected')` ＋ 一个按钮，
不是新子系统）→ `test_reject_button_moves_proposed_to_rejected_and_keeps_the_row`
（推得动、**账本不删行**）＋ `test_reject_after_approval_works_and_rejected_is_terminal`
（批准之后能反悔，rejected 是终态）＋ `test_reject_without_a_real_target_writes_nothing`
（没选中／幽灵 id：一行都不写）。

另有 **人工段缺口修复** 一组（设计席 2026-10-07 清单，同一天落地）。缺口是：复盘的第一道
闸门要求 State 里有人工段（kind 不为 fact 的条目），而界面上从来没有写入人工段的入口，
`latest_project_state()` 因此在生产库永远返回 None，手动触发的复盘退回收自动段、
永远走 `manual_section_missing` —— 闭环断在第一步。补的是一格自由文本＋一个保存按钮
（`review_controller.do_save_manual_section`），清单那五条判据逐个对上：

- 判据①保存后闸门①过 ＋ 判据②原话逐字进载荷 →
  `test_save_manual_section_clears_gate_one_and_keeps_words_verbatim`（原话刻意带换行；
  这一条末尾自带清单的负控：`manual_items=[]` 时闸门①必须仍过不去）；
- 判据③端到端（缺口堵上的唯一证明）→ `test_manual_section_entry_closes_the_gap_end_to_end`
  （负控半边先把 `project_states` 清空、不保存就触发，出口必须仍是 `manual_section_missing`；
  第三段再把闸门②放过去，验观测**真的被调用**且载荷里带着人工段）；
- 判据④空输入不落盘 → `test_save_manual_section_empty_input_writes_no_row`
  （数 `project_states` 的行数，不读文案——四种「空」里含 None）；
- 判据⑤两个 active 目标时不许报错 → `test_save_manual_section_with_two_active_goals_still_saves`
  （定案是 `goal_id` 传 None：解不出唯一目标就登记 unknown、照常落盘，界面不加目标选择器）；
- 两条边界：「保存 ≠ 触发」→ `test_save_manual_section_does_not_trigger_review`
  （观测桩计数为 0 才叫没跑复盘，行数只证明没写账本）；「追加式」→
  `test_save_manual_section_is_append_only`（比的是旧行的 items 整体，不是行数）；
- 一条设计决定的牙齿：字段名取 `DECISION_ITEM_FIELDS` 里与 kind 同位的那一个，不在界面写死
  → `test_manual_section_field_comes_from_project_state_table`。

阶段一（设计席 2026-10-11 的决策模型改造）动的是本文件的**夹具与桩**，不是判据：
- `_state_for_review()` 过闸门②的理由从「fact 比 unknown 多」换成了条目上的
  `evidence` 标记（`unknowns_dominant` 这个函数已经不存在了），所以夹具里那条笔记计数
  必须标 `activity`、另两条标 `context`——**标错不是红，而是整文件改道去打模型**；
- `_ObserveStub` 补上第四格「置信度」（0.8，落在线上方 ⇒ 仍走「建议」那一档），
  并新增 `_AskStub`：**凡能触发复盘的地方两个旁路都要接桩**。少接问句那一个，
  「本文件全程离线」就是假话，而且它只在夹具标错证据时才暴露——那种红来得最晚；
- 快照那一格（`test_snapshot_line_handles_empty_table_then_a_saved_one`）原来断言界面
  会说「这一轮会去观测」，阶段一之后闸门①过了之后打哪条旁路连闸门自己都要先看活动读数，
  界面算不出来，所以那句已从文案里删掉，测试改成**正向钉「只预判闸门①」＋反向钉那句话
  不再出现**；配套的 `test_snapshot_gate_prediction_matches_the_real_gate` 顺带钉住两道
  闸门的先后：那份夹具摘掉人工段之后**剩下的正是会过闸门②的形状**，两条旁路仍零调用
  才说明顺序不是措辞。

界面形状另有两条：`test_manual_section_widgets_are_in_component_tree`（连 `placeholder`
与 `lines` 一起读——这一格被压成一行就等于没做：闸门①只要非空就过，一句「还行」也能过）
与 `test_manual_save_button_chains_snapshot_refresh`（保存后必须跟着刷一次快照，
且那条链里不许出现任何触发动作）。

全程离线：两条旁路函数（观测与问句）一律注入桩，**不打网络、不调模型**；`import app` 会构建
整个界面（本项目的既有冒烟方式），所以本文件导入 app 之前必须先切临时库——`ui/controllers/_shared.py`
在导入期就调 `init_db()`，顺序错了会打到生产库。

⚠️ 字符串里一律用「」而不用 ASCII 双引号：本文件有过一次
`"没把"空表时会怎样"说清楚"` 这种写法——它在 Python 里是**语法错误**，而这类写法
在本仓库已经出现过三次。中文串里要引用一个词，只用「」或『』。
"""

import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core import decision_store as ds  # noqa: E402
from core import long_term_project_review as rv  # noqa: E402
from core import project_state as ps  # noqa: E402
from core import task_template as tt  # noqa: E402
import agents.task_executor as te  # noqa: E402
from agents.task_executor import TaskExecutor  # noqa: E402

md.init_db()

# 控制器在 app 之前导入：控制器只依赖 core 与 _shared，先导入能确认「界面回调不靠 app 存在」
from ui.controllers import review_controller as rc  # noqa: E402
from ui.controllers import templates_controller as tc  # noqa: E402
import app as app_module  # noqa: E402  （导入即 create_app()，这就是判据①的冒烟方式）

_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")

#: 改前（HEAD 850a7d7，3c-乙 之前）的 Tab 标签基线，逐字抄自
#: `git show 850a7d7:ui/app_factory.py` 里的 `with gr.Tab(...)` 清单。
#: ⚠️ 刻意**列标签而不列数字**：数字（「13 个」）会在任何一次加 Tab 时都必须同步改，
#: 而标签清单只在「某个既有 Tab 被改名或被删」时才红——那正是设计席要防的那件事。
_BASELINE_TAB_LABELS = (
    "📋 会话与参数",
    "👤 档案与记忆",
    "🎯 目标管理",
    "🧠 知识图谱",
    "🔍 深度搜索",
    "🧩 技能列表",
    "📅 计划与复盘",
    "🧪 Prompt 进化",
    "🔁 复习推荐",
    "📤 导出",
    "🧩 任务模板",
    "💡 资源机会",
    "🧪 测试与资源监控",
)
_NEW_TAB_LABEL = "🧭 项目复盘"

#: 「还等人定」的那几个状态，**刻意在测试里另写一份字面量**，不引用
#: `review_controller._PENDING_STATUSES`：这条断言要防的正是「界面把已执行的也算成等人定」，
#: 拿同一个常量去校验同一个常量等于没校验。两边分叉时这条必须红。
_PENDING_STATUSES_FOR_TEST = (
    ds.STATUS_PROPOSED, ds.STATUS_DISCUSSED,
    ds.STATUS_APPROVED, ds.STATUS_INSUFFICIENT,
)


# ---------- 夹具 ----------

def _utc_now_text() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_for_review() -> Dict[str, Any]:
    """一份「能过两道闸门」的 State，落库后由执行侧经 `latest_project_state()` 读回来

    刻意走**真落盘＋真读回**而不是手搓 dict 直接传：接线读的是库，键名对不上这类缺陷
    用手工 dict 会被掩盖（同 3c-甲 那几条用例的口径）。

    ⚠️ 阶段一（2026-10-11，决策模型改造）之后，**过闸门②的理由换了**：原来这条夹具靠
    「3 条 fact 不多于 1 条 unknown」放行，现在闸门②判的是「快照里有没有项目活动读数」，
    也就是条目上的 `evidence` 标记。所以标记**按真采集器的分族口径分开标**（哪一族算活动
    证据由 `core/project_state.py` 的采集器决定；条目文本本身仍是手搓的，只有标记参与判断）：
    笔记计数是项目活动，最近一次测试结果与仓库卫生台账按用户的拍板不是。三条全标 activity
    会让这份夹具只是在假装自己过得去，也会让「一条活动读数就够」那种可分辨的形状消失。
    少标这一格，本文件所有走 `_make_proposed_decision` 的用例会一起改道去「请求补充」那一档
    ——而那一档**会打问句旁路**，桩没接上就是真网络。

    条目构成：3 条 fact（其中 1 条 activity）＋ 1 条 user_report（人工段，过闸门①）
    ＋ 1 条 unknown。
    """
    return {
        "snapshot_at": _utc_now_text(),
        "goal_id": "goal_review_tab",
        "items": [
            {"decision_field": "items.notes.total", "kind": "fact",
             "content": "total=7", "source": "table:notes[goal_id=goal_review_tab]",
             "evidence": ps.EVIDENCE_ACTIVITY},
            {"decision_field": "items.test_run.latest", "kind": "fact",
             "content": "run_id=122", "source": "table:test_runs.id",
             "evidence": ps.EVIDENCE_CONTEXT},
            {"decision_field": "items.debt_ledgers.total", "kind": "fact",
             "content": "22", "source": "file:checks/debt_baseline.json",
             "evidence": ps.EVIDENCE_CONTEXT},
            {"decision_field": "summary", "kind": "user_report",
             "content": "模板产出的笔记我一篇都没读过", "source": "用户 2026-10-06 口述"},
        ],
        "unknowns": [{"decision_field": "items.notes.read_count",
                      "reason": "库里没有「读过没读过」这一列"}],
        "note": None,
    }


class _ObserveStub:
    """观测桩：永远返回可用的问题与建议（不碰网络）

    「需要的能力」给的是 `CAPABILITY_MAP` 里真实存在的键（3a 的闭集过滤在 supervisor
    那一头，桩绕过了它，所以这里必须自己给合法键——否则这条链上会出现一个生产拿不到的
    形状，`render_human_view` 的分支就被假测过了）。

    「置信度」这一格同理（阶段一 2026-10-11 起的第四格）：生产那份永远带着它，桩不给
    就成了「生产拿不到的形状」；给 0.8 是让它落在 `CONFIDENCE_SUGGESTION_MIN` 之上，
    本文件的用例由此仍走「建议」那一档（分档本身由 `tests/test_project_review_loop.py`
    钉，这里不重复）。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __call__(self, payload: str) -> Dict[str, Any]:
        self.calls.append(payload)
        return {
            rv.OBSERVE_KEY_PROBLEM: "复盘产出了决策但没人被提醒去看",
            rv.OBSERVE_KEY_NEXT_STEP: "把复盘列表挂到启动回填上",
            rv.OBSERVE_KEY_CAPABILITY: ["queue_for_approval"],
            rv.OBSERVE_KEY_CONFIDENCE: 0.8,
        }

    @property
    def count(self) -> int:
        return len(self.calls)


class _AskStub:
    """问句桩：记下载荷并返回三条「只有模型才生成得出来」的问题（不碰网络）

    本文件里它的主要用途是**安全网**：阶段一改判闸门②之后，一份夹具如果没标出活动证据
    就会改道去「请求补充」那一档，而那一档会调用 `supervisor_ask_questions`。生产实现
    在桩没接上时是真打模型的——所以凡能触发复盘的地方都必须把**两个**旁路一起接上，
    否则「本文件全程离线」这句就是假话。计数为 0 的断言顺便钉住另一件事：走「建议」那一档
    时一次问句都不该花。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __call__(self, payload: str) -> Dict[str, Any]:
        self.calls.append(payload)
        return {rv.ASK_KEY_QUESTIONS: [
            "桩问甲：这一轮你希望我按哪一条来判？",
            "桩问乙：已经做出来的东西里，哪一件能直接用上？",
            "桩问丙：这周推进到哪一步就算有进展？",
        ]}

    @property
    def count(self) -> int:
        return len(self.calls)


def _tab_labels() -> List[str]:
    """从**构建好的** Blocks 树里取全部 Tab 标签（不是读源码字符串）"""
    blocks = getattr(app_module.demo, "blocks", None)
    assert blocks, "app.demo 没有 blocks——import app 没构建出界面，判据①无从谈起"
    import gradio as gr
    return [str(getattr(block, "label", "") or "")
            for block in blocks.values() if isinstance(block, gr.Tab)]


def _labels_of(kind: str) -> List[str]:
    """取界面上某一类组件的全部 label（kind: Dropdown / Textbox / Markdown）"""
    import gradio as gr
    cls = getattr(gr, kind, None)
    assert cls is not None, f"gradio 没有 {kind} 这一类，本文件的取法要跟着改"
    out: List[str] = []
    for block in app_module.demo.blocks.values():
        if isinstance(block, cls):
            out.append(str(getattr(block, "label", "") or ""))
    return out


def _button_values() -> List[str]:
    import gradio as gr
    return [str(getattr(block, "value", "") or "")
            for block in app_module.demo.blocks.values() if isinstance(block, gr.Button)]


def _radio_blocks():
    """yield (label, [(choice_label, choice_value), ...])——choices 归一成二元组"""
    import gradio as gr
    for block in app_module.demo.blocks.values():
        if isinstance(block, gr.Radio):
            raw = list(getattr(block, "choices", None) or [])
            normalized = []
            for item in raw:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    normalized.append((str(item[0]), item[1]))
                else:  # Gradio 对纯字符串选项会回退成「文本即取值」
                    normalized.append((str(item), item))
            yield str(getattr(block, "label", "") or ""), normalized


def radio_labels():
    return [label for label, _choices in _radio_blocks()]


def _make_proposed_decision() -> int:
    """经**真执行链**立一条 proposed 决策（两个旁路都打桩），返回 decision_id

    这是本文件唯一造 Decision 的方式——直接 `ds.create_decision` 会绕开 3c-甲 那条
    接线，端到端判据就变成「我测我自己」。

    ⚠️ 阶段一（2026-10-11）之后**观测与问句两个旁路都要接上桩**。本函数走的是「存原值＋
    finally 还原」的手工替换，少接一个不会报错，只会在夹具标错证据的那一轮**真打生产端点**
    ——那种红要到没人改这条链的时候才冒出来。所以末尾那句 `ask.count == 0` 有两个身份：
    对「建议这一档不打问句旁路」的断言，以及「桩确实接上了」的自检。
    """
    stub = _ObserveStub()
    ask = _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    try:
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        ps.save_project_state(_state_for_review())
        template_id = tt.create_template(
            name="复盘Tab端到端", trigger_type="manual",
            action_type=tt.ACTION_LONG_TERM_REVIEW, action_params={})
        assert template_id, "复盘模板建不出来"
        events = list(TaskExecutor().execute_template(template_id, triggered_by="manual"))
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask
    assert stub.count == 1, f"复盘没真的跑：观测调用 {stub.count} 次"
    assert ask.count == 0, f"这一轮走的是「建议」那一档，一次问句都不该花（实得 {ask.count}）"
    messages = "｜".join(str(e.get("message") or "") for e in events)
    matched = re.search(r"已写入 Decision #(\d+)", messages)
    assert matched, f"端到端没立出 Decision：{messages}"
    return int(matched.group(1))


def _header_counts(md_text: str):
    """从列表首行抠出（总条数, 等你定的条数）——抠不出来即形状变了，直接红"""
    header = md_text.splitlines()[0]
    matched = re.match(r"^\*\*账本里共 (\d+) 条决策，其中 (\d+) 条等你定", header)
    assert matched, f"列表首行的形状变了：{header}"
    return int(matched.group(1)), int(matched.group(2))


def _dropdown_blocks():
    """yield (label, [(choice_label, choice_value), ...])，取法照 `_radio_blocks`

    复盘结论那一格是 Dropdown 不是 Radio，但归一逻辑必须**共用同一形状**：
    Gradio 对纯字符串选项会回退成「文本即取值」，不复用这套归一就会把
    「选项写法变了」误报成「选项不见了」。
    """
    import gradio as gr
    for block in app_module.demo.blocks.values():
        if isinstance(block, gr.Dropdown):
            raw = list(getattr(block, "choices", None) or [])
            normalized = []
            for item in raw:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    normalized.append((str(item[0]), item[1]))
                else:
                    normalized.append((str(item), item))
            yield str(getattr(block, "label", "") or ""), normalized


def _experience_rows(decision_id: int) -> List[Dict[str, Any]]:
    """直接读临时库的 `experiences`——回流这一头的断言要落在库上，不只读状态文案

    只看界面那一句「已提炼」是**读侧断言**：它证明有人调用过，不证明写进去的列是对的。
    锚点 §一.9 那张落列表要能被验，就得把行读回来逐列比。
    """
    conn = md.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, scenario, strategy, effect, future_effect, decision_id "
            "FROM experiences WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _all_experience_count() -> int:
    conn = md.get_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0])
    finally:
        conn.close()


def _push_to_evaluated(decision_id: int,
                       reference: str = "note://4a-ui-收尾",
                       evaluation: str = ds.EVAL_WRONG_ASSUMPTION,
                       actual_result: str = "每周一次的节奏根本没人看，产出全堆着") -> None:
    """沿界面把一条决策推到 evaluated（**一格都不绕**：三个按钮都真点一次）

    默认结论是 `wrong_assumption`：这一档是闭环的牙齿（允许承认当初先采信的前提被证伪），
    端到端用例走它比走 `helpful` 更贴近本项目的真实处境。
    """
    rc.do_review_approve(decision_id)
    _status, _list, _dd, _detail = rc.do_review_mark_executed(decision_id, reference)
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_EXECUTED, "前置没推到 executed"
    rc.do_review_evaluate(decision_id, evaluation, actual_result)
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_EVALUATED, "前置没推到 evaluated"


# ---------- 判据①：构建 ＋ 新控件在树内 ＋ 形状 ----------

def test_app_import_builds_and_isolated():
    """导入 app 即构建界面（本项目的既有冒烟方式），且本文件连的是临时库"""
    current = os.path.abspath(md.DB_PATH)
    assert "_test_tmp_" in current.replace(os.sep, "/"), (
        f"DB 没落在临时目录，本文件会打到生产库：{current}")
    assert current != os.path.abspath(os.path.join("data", "memory.db"))
    assert hasattr(app_module, "demo"), "app.demo 不存在——create_app() 没被调用"
    print("[OK] import app 构建出 demo，且连的是临时库")


def test_existing_tab_labels_all_still_present():
    """形状要钉：改前那 13 个 Tab 标签**逐个**仍在，且新 Tab 已加上

    设计席原文：「13 个既有 Tab 不受影响，但形状要钉：断言既有 13 个 Tab 的标签逐个仍在
    （防『加 Tab 顺手改了布局』）」。刻意不写成 `assert len(tabs) == 14`：数字断言在任何
    一次加 Tab 时都会红，红了之后人会去把数字改大，那就等于没有断言。
    """
    labels = _tab_labels()
    missing = [name for name in _BASELINE_TAB_LABELS if name not in labels]
    assert not missing, f"这些既有 Tab 不见了或被改名：{missing}"
    assert _NEW_TAB_LABEL in labels, f"新 Tab「{_NEW_TAB_LABEL}」没进组件树"
    assert labels.count(_NEW_TAB_LABEL) == 1, "新 Tab 被注册了不止一次"
    print(f"[OK] 既有 {len(_BASELINE_TAB_LABELS)} 个 Tab 标签逐个仍在，"
          f"新 Tab 已注册一次（当前共 {len(labels)} 个）")


def test_review_tab_widgets_are_in_component_tree():
    """新控件真的在组件树里（只测「建出来了」，不测「长得对」）

    取的是**标签文本**而不是组件变量名：变量名在 app_factory 函数体里，外面看不见；
    而标签是人在界面上实际看到的字，测试与界面之间因此没有第二层翻译。
    """
    dropdowns = _labels_of("Dropdown")
    assert any("选择一条决策" in text for text in dropdowns), (
        f"复盘列表的下拉框不在树里：{[t for t in dropdowns if '决策' in t]}")
    textboxes = _labels_of("Textbox")
    assert any("讨论内容" in text for text in textboxes), "讨论输入框不在树里"
    assert any("我当时的判断" in text for text in textboxes), "prior_intent 输入框不在树里"
    buttons = _button_values()
    for name in ("💬 记入讨论", "✅ 批准", "🚫 否决", "🏁 标记已执行", "🔄 刷新账本"):
        assert name in buttons, f"按钮「{name}」不在树里：{buttons}"
    print("[OK] 复盘 Tab 的下拉框／两格输入／那一排按钮都在组件树内")


def test_template_form_action_field_offers_every_valid_action():
    """模板表单新增的动作那一格，选项恰好是 `VALID_ACTIONS` 三个成员

    这条是**覆盖关系**的断言，不是「我写了三个选项」的自述：将来往 `VALID_ACTIONS`
    加第四种动作而忘了表单，这里就红。
    """
    radios = dict(_radio_blocks())
    target = [text for text in radios if text.startswith("动作类型")]
    assert target, f"表单里没有动作类型那一格：{radio_labels()}"
    choices = radios[target[0]]
    offered = {value for _label, value in choices}
    assert offered == set(tt.VALID_ACTIONS), (
        f"表单给的动作与 VALID_ACTIONS 不一致：多选 {offered - set(tt.VALID_ACTIONS)}、"
        f"少选 {set(tt.VALID_ACTIONS) - offered}")
    print("[OK] 动作那一格的选项与 VALID_ACTIONS 逐个对得上")


def test_action_labels_cover_every_valid_action():
    """`_ACTION_LABELS` 补齐 `long_term_review`，且**逐个**覆盖 VALID_ACTIONS

    缺标签的后果不是报错而是「把 slug 原样印给人看」（列表那一行会显示 long_term_review），
    所以这条按覆盖关系比，而不是断言某一句中文存在。
    """
    missing = [action for action in tt.VALID_ACTIONS
               if action not in tc._ACTION_LABELS]
    assert not missing, f"这些动作没有中文标签，列表会直接印 slug：{missing}"
    assert "长期项目复盘" in tc._ACTION_LABELS[tt.ACTION_LONG_TERM_REVIEW], (
        "复盘动作的标签要让人一眼看出它产的是账本行，不是笔记")
    print(f"[OK] {len(tt.VALID_ACTIONS)} 种动作全都有中文标签，复盘那条措辞正确")


# ---------- 判据②：端到端一条链（prior_intent 留空） ----------

def test_end_to_end_review_to_executed_with_empty_prior_intent():
    """设计席的 3c 验收原文：建模板 → 手动触发 → 复盘跑起来 → 账本有行 → 读得到、
    能讨论、能标记已执行。**全程 `prior_intent` 传空字符串**——这就是「可选性有牙齿」：
    谁把它改成必填，这条立刻红。

    「标记已执行」之前必须有一次「批准」：状态机里只有 `approved → executed` 这条边
    （`mark_executed` 对其它当前态一律 raise），所以批准格是这条链的**承重前置**，
    不是顺手多加的按钮。
    """
    decision_id = _make_proposed_decision()

    list_md, dd_update = rc.refresh_review_panel(decision_id)
    assert f"#{decision_id}" in list_md, "账本列表里看不到刚立的那条决策"
    assert dd_update.get("value") == decision_id, (
        f"下拉框没把新决策列成选中值：{dd_update.get('value')!r}")

    detail = rc.do_view_review_decision(decision_id)
    assert "待讨论" in detail, f"全文回读里没有状态中文标签：{detail[:120]}"
    assert "观测结论" in detail, "全文里没有那条 inference 条目"

    status_md, list_md, _dd, detail = rc.do_review_discuss(decision_id, "同意，先挂一周", "")
    assert "已记入" in status_md, status_md
    assert "assumption" not in status_md, (
        f"prior_intent 留空却写出了 assumption 条目：{status_md}")
    current = ds.get_decision(decision_id)
    assert current["status"] == ds.STATUS_DISCUSSED, (
        f"讨论之后状态没推进：{current['status']}")
    items = ds.list_items(decision_id)
    kinds = [item["kind"] for item in items]
    # 复盘落库时人工段抄进来 1 条 user_report，本条讨论再写 1 条 ⇒ 恰好 2 条。
    # 断言「恰好」而不是「增加」：多写一条空条目、或把讨论登记成别的类别，这里都会红。
    assert kinds.count(ds.KIND_USER_REPORT) == 2, (
        f"user_report 该是 2 条（State 抄来 1 ＋ 本次讨论 1）：{kinds}")
    # 事前意图留空 ⇒ assumption 只剩观测那条能力需求（1 条）。这一句与
    # `test_prior_intent_written_as_assumption_when_given` 的「+1」合起来才是「可选」。
    assert kinds.count(ds.KIND_ASSUMPTION) == 1, (
        f"留空却多写了 assumption，或连能力那条都没落：{kinds}")
    assert "同意，先挂一周" in detail, "讨论内容没出现在全文回读里"

    status_md, _list, _dd, _detail = rc.do_review_approve(decision_id)
    assert "已批准" in status_md, status_md
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_APPROVED

    status_md, _list, _dd, detail = rc.do_review_mark_executed(decision_id)
    assert "已记为已执行" in status_md, status_md
    after = ds.get_decision(decision_id)
    assert after["status"] == ds.STATUS_EXECUTED, after["status"]
    assert after["execution_reference"] is None, (
        "没填执行痕迹时库里必须是 NULL——留空表示「没有可指的东西」，"
        "写空串或编一个路径都是假账；填了的那条由 "
        "test_mark_executed_reference_written_and_blank_stays_null 钉")
    assert "已执行" in detail, "全文回读没跟着状态变"
    print(f"[OK] 端到端（prior_intent 留空）：#{decision_id} proposed → discussed "
          f"→ approved → executed")


def test_prior_intent_written_as_assumption_when_given():
    """`prior_intent` 非空时多登记一条 assumption，空时一条都不多

    两条合起来才叫「可选」：**只测非空**则实现可以把它当必填（空值时报错也能过），
    **只测空**则实现可以把它当摆设（永远不写也过）。
    """
    decision_id = _make_proposed_decision()
    before_assumption = sum(1 for item in ds.list_items(decision_id)
                            if item["kind"] == ds.KIND_ASSUMPTION)
    status_md, _list, _dd, detail = rc.do_review_discuss(
        decision_id, "这条我同意", "我以为每周跑一次就够，不用人看")
    assert "已记入" in status_md, status_md
    assert "assumption" in status_md, f"填了事前意图却没登记：{status_md}"
    after_assumption = sum(1 for item in ds.list_items(decision_id)
                           if item["kind"] == ds.KIND_ASSUMPTION)
    assert after_assumption == before_assumption + 1, (
        f"事前意图该多出一条 assumption：{before_assumption} → {after_assumption}")
    assert "我以为每周跑一次就够" in detail, "事前意图的原文没出现在全文回读里"
    for item in ds.list_items(decision_id):
        if item["kind"] == ds.KIND_ASSUMPTION and "事前意图" in str(item["content"]):
            assert item["source"] is None, (
                f"assumption 的出处按 decision_store 口径要留空（为空＝还没有证据）："
                f"{item['source']}")
    print(f"[OK] 填了事前意图才多那条 assumption：{before_assumption} → "
          f"{after_assumption}，且出处留空")


def test_empty_comment_is_refused_and_nothing_written():
    """讨论内容为空 ⇒ 拒，且账本条目数一个都不变（不是「写了条空的」）"""
    decision_id = _make_proposed_decision()
    items_before = len(ds.list_items(decision_id))
    status_md, _list, _dd, _detail = rc.do_review_discuss(decision_id, "   ", "随手填的一句")
    assert "讨论内容不能为空" in status_md, status_md
    assert len(ds.list_items(decision_id)) == items_before, (
        "空讨论被登记成条目了——账本上会留下一条没有任何内容的记录")
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_PROPOSED, (
        "内容都没写进去，状态却推进了")
    print("[OK] 空讨论被拒：条目数不变、状态仍 proposed")


def test_negative_control_mark_executed_before_approval_is_refused():
    """未批准就点「标记已执行」：`mark_executed` 的 raise 被接成人能读的一行，状态不动

    这条同时是负控与守卫：如果实现为了「按钮好使」而把 `mark_executed` 的当前态检查放宽，
    这里就会看到 executed——那正是状态机要防的「跳过批准」。
    """
    decision_id = _make_proposed_decision()
    status_md, _list, _dd, _detail = rc.do_review_mark_executed(decision_id)
    assert "没能标记为已执行" in status_md, f"跳批准没被拦住：{status_md}"
    assert "批准" in status_md, f"文案没告诉人下一步该点什么：{status_md}"
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_PROPOSED, (
        "报错之外还偷偷改了状态")
    print(f"[OK] 未批准就标记已执行被拦住，状态仍是 proposed（#{decision_id}）")


# ---------- 「🚫 否决」那一格（设计席 2026-10-07 对偏离 4 的裁定） ----------

def test_reject_button_moves_proposed_to_rejected_and_keeps_the_row():
    """「🚫 否决」把 proposed 推到 rejected，**账本行一条都不少**

    这一格的存在理由（裁定原话）：「没有 reject 按钮 = 用户无法说『不』」。
    断言刻意把「行还在」与「状态变了」分成两句——如果实现顺手把它做成**删除**，
    只断言状态会读成 `None`、红成一个 AttributeError，看不出是「删了」还是「没写」；
    先断 `row is not None` 才能让红的形状直接说出是哪一种走法。
    """
    decision_id = _make_proposed_decision()
    items_before = len(ds.list_items(decision_id))
    status_md, list_md, _dd, detail = rc.do_review_reject(decision_id)
    assert "已否决" in status_md, status_md
    row = ds.get_decision(decision_id)
    assert row is not None, (
        "否决把账本行删掉了——「他不要这条建议」正是第一闭环要复盘的记录，"
        "它不许从账上消失")
    assert row["status"] == ds.STATUS_REJECTED, f"状态没走到 rejected：{row['status']}"
    assert len(ds.list_items(decision_id)) == items_before, (
        "否决不该新写条目：它只推进状态，内容还是原来那几条")
    assert f"#{decision_id}" in list_md, "被否决的那条从列表里消失了"
    assert "已否决" in detail, f"全文回读的状态不是中文标签：{detail[:120]}"
    print(f"[OK] 否决生效：#{decision_id} 待讨论 → 已否决，行与条目都在、列表还列得出来")


def test_reject_after_approval_works_and_rejected_is_terminal():
    """批准之后还能反悔；进了 rejected 就没有下一条边

    前半句是 `approved → rejected` 这条边在界面上的落点：账本上「批准了但其实没做」
    比「从没批准」更接近真实处境，不给这条路就等于逼人留着一条假账。
    后半句是终态：rejected 之后再点「批准」或「标记已执行」，两次都必须被状态机拒、
    且状态纹丝不动——界面不重复写规则，所以这里验的是 raise 被接成了人话。
    """
    decision_id = _make_proposed_decision()
    rc.do_review_approve(decision_id)
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_APPROVED, (
        "前置没建起来：批准没走到 approved")

    status_md, _list, _dd, _detail = rc.do_review_reject(decision_id)
    assert "已否决" in status_md, f"批准之后说不出「不」：{status_md}"
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_REJECTED

    status_md, _list, _dd, _detail = rc.do_review_approve(decision_id)
    assert "没批准" in status_md, f"终态 rejected 竟然被按钮重新批准了：{status_md}"
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_REJECTED, (
        "报错之外还偷偷改了状态")

    status_md, _list, _dd, _detail = rc.do_review_mark_executed(decision_id)
    assert "没能标记为已执行" in status_md, f"被否决的决策能被标记已执行：{status_md}"
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_REJECTED
    print("[OK] approved 可反悔成 rejected；rejected 是终态，批准与已执行都推不动它")


def test_reject_without_a_real_target_writes_nothing():
    """没选中／选中一个读不到的 id：各给一句人话，账本一行都不动

    两个分支分开断言，因为它们防的是两种不同的实现偷懒：
    - `None` 那支防「把 None 当 0 写进去」；
    - 幽灵 id 那支防「先 UPDATE 再判断，读不到就当成功」。
    """
    existing = [int(row["id"]) for row in ds.list_decisions(limit=500)]

    status_md, _list, _dd, _detail = rc.do_review_reject(None)
    assert "请先在上方选择一条决策" in status_md, status_md

    ghost = (max(existing) if existing else 0) + 1000
    status_md, _list, _dd, _detail = rc.do_review_reject(ghost)
    assert "不存在" in status_md, status_md
    after = [int(row["id"]) for row in ds.list_decisions(limit=500)]
    assert sorted(after) == sorted(existing), (
        "读不到那条决策，却往账本里写行了")
    print("[OK] 没选中与幽灵 id 都被拒：两条人话，账本行数一字不差")


# ---------- 判据③：启动回填 ＋ 未处理可见 ＋ 空快照 ----------

def test_start_up_backfill_is_in_demo_load_chain():
    """复盘列表与最近快照挂在 `demo.load` 链上（与待审批面板同款）

    静态读源码，口径照 `tests/test_concept_explainer_skill.py`：刻意不再重跑一次
    `create_app()`（那会拉起整个界面）。切片的右边界取「链结束后第一条独立语句」，
    所以断言只可能命中链内，不会被别处的同名调用满足。
    """
    with open(_FACTORY_SOURCE, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("demo.load(")
    end = source.index("new_session_btn.click", start)
    chain = source[start:end]
    assert "refresh_review_panel" in chain, "复盘列表没进启动回填链"
    assert "refresh_review_snapshot" in chain, "最近快照没进启动回填链"
    assert "do_view_review_decision" in chain, "全文回读没在链里跟着选中值跑一次"
    print("[OK] 启动回填链里有复盘列表／全文／最近快照三段")


def test_pending_count_shown_in_list_header():
    """列表首行那一句「有 N 条等你定」的**数字与状态**要经得起核对

    只断言「列表非空」是恒真断言；这里把渲染出来的两个数与库里独立算出来的两个数对上，
    并且构造一个真会区分的场景：一条推到 executed、一条留在 proposed。
    ⚠️ 刻意不写死「1 条等你定」——本文件前面的用例已经在库里留下了决策行，
    写死具体条数等于把用例顺序当成前提（本项目吃过「用例之间互相留数据」的亏）。
    """
    first = _make_proposed_decision()
    second = _make_proposed_decision()
    ds.set_status(first, ds.STATUS_DISCUSSED)
    ds.set_status(first, ds.STATUS_APPROVED)
    ds.mark_executed(first)

    decisions = ds.list_decisions()
    expected_total = len(decisions)
    expected_pending = sum(1 for d in decisions
                           if d["status"] in _PENDING_STATUSES_FOR_TEST)
    md_text, _dd = rc.refresh_review_panel(None)
    total_shown, pending_shown = _header_counts(md_text)
    assert total_shown == expected_total, (
        f"列表首行的总数与账本不一致：显示 {total_shown}，实际 {expected_total}")
    assert pending_shown == expected_pending, (
        f"等你定的条数算错了：显示 {pending_shown}，按状态该是 {expected_pending}")
    assert ds.get_decision(first)["status"] == ds.STATUS_EXECUTED
    assert pending_shown < total_shown, (
        "executed 那条被算进「等你定」了——两个数在任何时候都该有差")
    assert f"#{second}" in md_text and f"#{first}" in md_text, "两条决策都得列出来"
    print(f"[OK] 列表首行按状态数出等你定 {pending_shown}／共 {total_shown}，"
          "executed 那条不计入")


def test_snapshot_line_handles_empty_table_then_a_saved_one():
    """最近快照那一格：空表返回 None 时显示成人读得懂的一句话（**不抛**），有快照时报出处

    ⚠️ 空表那一支用「先把表清空」构造，而不是指望库恰好是空的——指望库空是一条会随
    用例顺序漂移的假设。
    """
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM project_states")
        conn.commit()
    finally:
        conn.close()
    empty_text = rc.refresh_review_snapshot()
    assert "一份快照都还没有" in empty_text, empty_text
    assert "不会踩空" in empty_text, "没把「空表时手动触发会怎样」说清楚"

    saved = ps.save_project_state(_state_for_review())
    line = rc.refresh_review_snapshot()
    assert f"#{saved}" in line, f"报的不是刚存的那一份：{line}"
    assert "闸门①" in line, "没有给出这一轮会不会立 Decision 的预判"
    # 阶段一之后这一格**只预判闸门①**：闸门②改判「快照里有没有项目活动读数」，过了之后
    # 打哪条旁路（观测／问句／一条都不打）它算不出来，所以旧文案那句「这一轮会去观测」
    # 在新分流下是半句假话，已由执行席删掉。两条断言各挡一种回退：留着它=界面在预告
    # 自己算不出的东西；把它删了却改成「预判闸门②」=这一格得去重读快照条目，那才是分叉。
    assert "过得了" in line, f"有闸门口语却没说这一轮过不过：{line}"
    assert "会去观测" not in line, f"这一格还在预告它算不出来的东西：{line}"
    print(f"[OK] 空表时报「一份快照都还没有」，有快照时报 #{saved} 并只预判闸门①")


def test_snapshot_gate_prediction_matches_the_real_gate():
    """预判用的是**闸门自己那个函数**，不是界面里另写的一份判据

    做法：造一份人工段为空的快照（只含 fact），界面那一行必须说「过不了」；
    同一份 State 交给复盘流程，出口必须是 `manual_section_missing`。两边必须一致——
    如果界面上自己算一遍「人工段是不是空」，改了闸门判据时这条就会分叉。

    ⚠️ 阶段一之后这条顺带钉住了**两道闸门的先后**：本夹具摘掉人工段之后剩下的全是
    fact，其中还留着一条标了 `activity` 的笔记计数——也就是闸门②本来会放行的那一份。
    两条旁路都零调用才说明「闸门①在前」不是措辞，而是执行顺序。
    """
    state = _state_for_review()
    state["items"] = [item for item in state["items"] if item["kind"] == "fact"]
    state["unknowns"] = []
    saved = ps.save_project_state(state)
    assert saved, "快照没落库"
    line = rc.refresh_review_snapshot()
    assert "过不了" in line, f"人工段为空却没说闸门①过不了：{line}"

    stub = _ObserveStub()
    ask = _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    try:
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        result = rv.run_long_term_project_review(ps.latest_project_state(), stub, ask)
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask
    assert result["outcome"] == rv.OUTCOME_MANUAL_MISSING, (
        f"界面说闸门①过不了、复盘却说 {result['outcome']}——两处判据分叉了")
    assert result["decision_id"] is None and stub.count == 0 and ask.count == 0, (
        f"闸门①拦下却不花钱：观测 {stub.count} 次／问句 {ask.count} 次")
    print("[OK] 界面预判与复盘闸门同一次返回：都判「人工段为空」，两条旁路都零调用")


# ---------- 判据④（界面这一头）：表单能选动作 ＋ 非法值一行都不写 ----------

def test_form_can_create_review_template_without_keyword():
    """表单走「长期项目复盘」时不要求关键词，建出来的模板动作名没被改写

    批次 1c-② 时这一格不存在，用户**走界面造不出** long_term_review 模板；
    3c-甲 接上执行分支之后必须能造，否则接线只有脚本路径可达。
    """
    result = tc.do_save_template("界面造的复盘模板", "", "weekly", "09:30", 5,
                                 "", False, None, tt.ACTION_LONG_TERM_REVIEW)
    status_md = result[0]
    assert "已创建模板" in status_md, status_md
    assert len(result) == 12, f"返回值形状变了（应为 3 ＋ 表单 9 项）：{len(result)}"
    assert result[11] == tt.ACTION_SEARCH_AND_NOTE, (
        "保存成功后动作那一格要回到新建默认值，否则下一次手滑会沿用上一条的动作")
    created = [t for t in tt.list_templates() if t.name == "界面造的复盘模板"]
    assert len(created) == 1, f"没建出模板：{[t.name for t in tt.list_templates()]}"
    template = created[0]
    try:
        assert template.action_type == tt.ACTION_LONG_TERM_REVIEW, (
            f"动作被改写成了 {template.action_type}")
        assert template.action_params == "{}", (
            f"复盘动作不读参数，落成空对象才对：{template.action_params}")
        assert template.auto_approve == 0, "auto_approve 全程保持 0 是既有定案"
        # 列表里这一行要看得见中文标签，而不是 slug（缺标签时走的是
        # `_ACTION_LABELS.get(action_type, action_type)` 那条兜底——不报错，只是把人打回英文）
        panel_md, _dd = tc.refresh_template_panel(template.id)
        own_line = [line for line in panel_md.splitlines()
                    if line.startswith(f"- #{template.id} ")]
        assert own_line, f"模板列表里没有这一条：{panel_md}"
        assert "长期项目复盘" in own_line[0], own_line[0]
        assert tt.ACTION_LONG_TERM_REVIEW not in own_line[0], (
            f"动作被原样印成 slug，说明 _ACTION_LABELS 缺键：{own_line[0]}")
        print(f"[OK] 表单造出 long_term_review 模板 #{template.id}：零关键词、参数为空对象")
    finally:
        tt.delete_template(template.id)


def test_negative_control_bad_action_via_form_writes_nothing():
    """非法动作名从表单进来：`ValueError` 接成一行状态，且模板数一条都没多

    这是「拒绝」与「静默降级」的分界——改回降级时这条会红（建出一条 search_and_note 模板，
    而用户以为建的是别的动作）。
    """
    before = len(tt.list_templates())
    result = tc.do_save_template("非法动作模板", "", "manual", "09:00", 0,
                                 "", False, None, "totally_not_an_action")
    status_md = result[0]
    assert "没有写入" in status_md and "非法值" in status_md, status_md
    assert len(tt.list_templates()) == before, (
        f"非法动作名照样建了行（{before} → {len(tt.list_templates())}）")
    print("[OK] 非法动作名从表单进来：一行都没写，状态文案明说是非法值")


def test_template_form_defaults_carry_action_at_the_end():
    """表单默认值与回填元组的**位置契约**：动作类型排在最后（第 9 项）

    位置契约是这条链上最容易静默坏掉的地方：Gradio 按列表顺序把返回值喂给组件，
    顺序错了不会报错，只会把关键词填进星期几。这里把契约写成断言，
    改顺序时必须撞到某一条用例。
    """
    defaults = tc._template_form_defaults()
    assert len(defaults) == 9, f"表单默认值项数变了：{len(defaults)}"
    assert defaults[8] == tt.ACTION_SEARCH_AND_NOTE, defaults
    template_id = tt.create_template("位置契约模板", trigger_type="daily",
                                     action_type=tt.ACTION_SCOUT_DIGEST,
                                     action_params={})
    try:
        filled = tc.load_template_to_form(template_id)
        assert len(filled) == 9, f"回填元组项数变了：{len(filled)}"
        assert filled[8] == tt.ACTION_SCOUT_DIGEST, (
            f"动作没回填到最后一项：{filled}")
        assert filled[0] == template_id and filled[1] == "位置契约模板"
        # 老调用方（不带第 9 个实参）的语义必须仍是「按原动作」，这是 1c-② 行为的兼容读法
        status_md = tc.do_save_template("位置契约模板改名", "", "daily", "10:00", 0,
                                        "", False, template_id)[0]
        assert "已更新" in status_md, status_md
        assert tt.get_template(template_id).action_type == tt.ACTION_SCOUT_DIGEST, (
            "action_type 不传时被当成换成默认动作了——老调用方的语义必须不变")
        print("[OK] 元组第 9 项是动作类型；不传第 9 项时按原动作走")
    finally:
        tt.delete_template(template_id)


# ---------- 4a 的 UI 收尾：执行痕迹 ＋ 评估 ＋ 提炼（裁决 1／裁决 2） ----------

def test_review_flowback_widgets_are_in_component_tree():
    """新增的三格输入、一个下拉、两个按钮都在组件树里（判据①的同一口径）"""
    textboxes = _labels_of("Textbox")
    for fragment in ("执行痕迹", "实际结果", "下次改什么"):
        assert any(fragment in text for text in textboxes), (
            f"「{fragment}」那一格不在树里：{[t for t in textboxes if t]}")
    dropdowns = _labels_of("Dropdown")
    assert any("复盘结论" in text for text in dropdowns), (
        f"复盘结论的下拉框不在树里：{dropdowns}")
    buttons = _button_values()
    for name in ("📝 评估", "✨ 提炼经验"):
        assert name in buttons, f"按钮「{name}」不在树里：{buttons}"
    print("[OK] 执行痕迹／复盘结论／实际结果／下次改什么四格与评估、提炼两个按钮都在树内")


def test_evaluation_choices_cover_every_valid_evaluation():
    """下拉给出的复盘结论**恰好**是 `VALID_EVALUATIONS` 五值，且每格都有中文标签

    这是覆盖关系的断言而不是「我写了五个选项」的自述：将来 `VALID_EVALUATIONS` 加第六值
    而界面没跟上（或反过来多写一个词表外的值），这里就红。口径同
    `test_template_form_action_field_offers_every_valid_action`。
    """
    dropdowns = dict(_dropdown_blocks())
    target = [text for text in dropdowns if "复盘结论" in text]
    assert target, f"界面上没有复盘结论那一格：{list(dropdowns)}"
    choices = dropdowns[target[0]]
    offered = {value for _label, value in choices}
    assert offered == set(ds.VALID_EVALUATIONS), (
        f"下拉给的结论与 VALID_EVALUATIONS 不一致：多选 {offered - set(ds.VALID_EVALUATIONS)}、"
        f"少选 {set(ds.VALID_EVALUATIONS) - offered}")
    for label, value in choices:
        assert label != value, f"结论 {value} 的标签就是 slug 本身，人读不懂"
        assert value in rc.EVALUATION_LABELS_ZH, f"{value} 没有中文标签"
    print(f"[OK] 复盘结论下拉与 VALID_EVALUATIONS 逐个对得上，{len(choices)} 项都有中文标签")


def test_mark_executed_reference_written_and_blank_stays_null():
    """裁决 1 的两半：**填了要原样落库**，**留空要仍是 NULL**（不是空串、不是编出来的）

    两半缺一半都放过了错误实现：
    - 只测填的那半 ⇒ 实现可以把空白也写成 `''`，库里的「没填」与「填了个空东西」就混了；
    - 只测留空那半 ⇒ 实现可以永远传 None，那个输入框就成了摆设（本项目吃过
      「判据只盖一半」的亏，见 §五.39 那一族）。
    """
    filled = _make_proposed_decision()
    blank = _make_proposed_decision()
    rc.do_review_approve(filled)
    status_md, _list, _dd, detail = rc.do_review_mark_executed(
        filled, "  note://4a-执行痕迹，前后空格要裁掉  ")
    assert "已记为已执行" in status_md, status_md
    row = ds.get_decision(filled)
    assert row["execution_reference"] == "note://4a-执行痕迹，前后空格要裁掉", (
        f"填的执行痕迹没原样落库：{row['execution_reference']!r}")
    assert "note://4a-执行痕迹" in detail, "全文回读里看不到刚写进去的执行痕迹"

    rc.do_review_approve(blank)
    rc.do_review_mark_executed(blank, "")
    blank_row = ds.get_decision(blank)
    assert blank_row["status"] == ds.STATUS_EXECUTED, blank_row["status"]
    assert blank_row["execution_reference"] is None, (
        f"留空被写成了 {blank_row['execution_reference']!r}——必须是 NULL："
        "「没有可指的东西」与「指了一个空东西」在复盘时是两件事")
    print("[OK] 执行痕迹：填了原样入库（含去空白），留空仍是 NULL")


def test_end_to_end_execute_evaluate_and_distill_flowback():
    """端到端一条链走完 4a：批准 → 标记已执行（带痕迹）→ 评估 → 提炼 → 库里真有一行经验

    这是裁决 2 那句「先补，再开 4b」的正面证据：**闭环那两跳在没有界面的时候是断的**，
    现在人在这一页能把一条决策从「待讨论」一路推到 `experiences` 里有痕迹。
    落列逐列按锚点 §一.9 比，不只看界面那一句成功文案。
    """
    decision_id = _make_proposed_decision()
    _push_to_evaluated(decision_id)

    detail = rc.do_view_review_decision(decision_id)
    # 状态标签在详情区的第一行，所以比对**只取首行**：整段里「复盘」两个字到处都在，
    # 拿全文去比会放过「首行还写着已执行、别处偶然出现该词」这种假绿。
    head_line = detail.splitlines()[0]
    assert "已复盘" in head_line, (
        f"详情区首行的状态不是 evaluated 的中文标签：{head_line}")
    assert "前提错了" in detail, (
        "复盘结论被印成 slug 了——这一列的内容是人读的，不是程序读的")

    lesson = "下次先按天看产出，别再指望每周一次的节奏"
    status_md, _list, _dd, _detail = rc.do_review_distill(decision_id, lesson)
    assert "已提炼成经验" in status_md, status_md
    rows = _experience_rows(decision_id)
    assert len(rows) == 1, f"经验表该恰好一行：{rows}"
    row = rows[0]
    assert int(row["decision_id"]) == decision_id, "链接列没指回那条决策"
    assert row["future_effect"] == lesson, f"教训原文没照写：{row['future_effect']!r}"
    assert row["effect"] == "每周一次的节奏根本没人看，产出全堆着", (
        f"effect 该从库里抄 actual_result，不是让调用方重打一遍：{row['effect']!r}")
    assert row["strategy"] == "" and row["strategy"] is not None, (
        f"legacy 字段按裁决落成显式空串（该列 NOT NULL）：{row['strategy']!r}")
    assert row["scenario"], "scenario 至少要有决策自己的标题兜底，不能空着"
    # 提炼**不回写决策**：status 与执行痕迹都该一个字不动
    after = ds.get_decision(decision_id)
    assert after["status"] == ds.STATUS_EVALUATED, after["status"]
    assert after["execution_reference"] == "note://4a-ui-收尾", (
        "提炼经验这一步改动了执行痕迹——写点定在评估之后，不该再碰决策行")
    print(f"[OK] 端到端：#{decision_id} 一路到经验行 #{row['id']}，落列逐列对锚点")


def test_evaluate_before_execution_is_refused_and_writes_nothing():
    """没执行过的决策点「评估」：`evaluate` 的 raise 接成人话，三列都不动

    这一条同时是负控：实现若为了「按钮好使」把 `evaluate` 的当前态检查放宽，
    就会看到一条 proposed 直接被评成 evaluated——那正是状态机要防的跳过执行。
    """
    decision_id = _make_proposed_decision()
    before = ds.get_decision(decision_id)
    experiences_before = _all_experience_count()
    status_md, _list, _dd, _detail = rc.do_review_evaluate(
        decision_id, ds.EVAL_HELPFUL, "随便写的一句实际结果")
    assert "没评成" in status_md, f"跳过执行没被拦住：{status_md}"
    assert "标记已执行" in status_md, f"文案没指下一步该点什么：{status_md}"
    after = ds.get_decision(decision_id)
    assert after["status"] == before["status"], (
        f"报错之外还改了状态：{before['status']} → {after['status']}")
    assert after["evaluation"] is None and after["actual_result"] is None, (
        "评估没走通却把两列写进去了——那是一行半截账")
    assert _all_experience_count() == experiences_before, (
        "评估被拒时不该顺带提炼出经验")
    print("[OK] 未执行就评估被拒：状态、evaluation、actual_result 三处一字未动")


def test_evaluate_requires_actual_result():
    """`actual_result` 留空 ⇒ 评估被拒（evaluated 态的定义是两格同时非空）

    只把 evaluation 写进去、actual_result 空着的实现，等于造出一个换了名字的存档，
    而 4a 的 `effect` 正是从这一列抄来的——它会跟着变成空。
    """
    decision_id = _make_proposed_decision()
    rc.do_review_approve(decision_id)
    rc.do_review_mark_executed(decision_id, "note://only-executed")
    status_md, _list, _dd, _detail = rc.do_review_evaluate(
        decision_id, ds.EVAL_NEUTRAL, "   ")
    assert "没评成" in status_md, status_md
    row = ds.get_decision(decision_id)
    assert row["status"] == ds.STATUS_EXECUTED, row["status"]
    assert row["evaluation"] is None, "actual_result 都拒了，evaluation 却被写进去了"
    print("[OK] 实际结果留空 ⇒ 评估整笔被拒，仍停在 executed")


def test_distill_with_empty_lesson_skips_without_writing():
    """「下次改什么」留空＝**跳过**，且不调用写点：经验表一行都不多

    与 3c 那格 `prior_intent` 的判据②同一形状：**可选性要有牙齿**。
    这里防的是两种相反的偷懒——把这一格做成必填（人会编一句教训出来，而编出来的教训
    正要被 4b 注入每轮上下文），或把空串喂给 `link_experience`（那是在绕它自己的守卫）。
    """
    decision_id = _make_proposed_decision()
    _push_to_evaluated(decision_id)
    before = _all_experience_count()
    status_md, _list, _dd, _detail = rc.do_review_distill(decision_id, "   ")
    assert "不提炼" in status_md, status_md
    assert _all_experience_count() == before, (
        f"留空却写了行（{before} → {_all_experience_count()}）——空教训进闭环＝注入一句空气")
    assert _experience_rows(decision_id) == [], "该一条都没有，却有行指回这条决策"
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_EVALUATED, "跳过不该动账本"
    print("[OK] 教训留空＝跳过：不调用写点、经验行数不变、状态仍是 evaluated")


def test_distill_before_evaluation_is_refused():
    """评过的才有教训可提炼：executed 就点「✨ 提炼经验」被守卫拒，经验表不写行

    写点定在 `evaluate()` **之后**（锚点 §一.9），顺序是设计的一部分而不是实现细节。
    """
    decision_id = _make_proposed_decision()
    rc.do_review_approve(decision_id)
    rc.do_review_mark_executed(decision_id, "note://executed-only")
    before = _all_experience_count()
    status_md, _list, _dd, _detail = rc.do_review_distill(
        decision_id, "这一句写得很明确，但那条决策还没被评")
    assert "没提炼" in status_md, f"未评估就能被提炼：{status_md}"
    assert _all_experience_count() == before, (
        f"被拒的写留下了行（{before} → {_all_experience_count()}）")
    print("[OK] 未评估就提炼被拒：经验表行数一字不差")


def test_tactical_decision_flowback_line_matches_the_eligibility_set():
    """详情区那句「能不能回流」用的是**集合本身**，不是界面里另写的一份判据

    口径同 `test_snapshot_gate_prediction_matches_the_real_gate`：判据只许有一处。
    这里刻意**直接建两条不同作用域的决策**而不走复盘链——复盘产物的默认档永远是
    `operational`（`REVIEW_DECISION_SCOPE`），造不出 tactical 那一支，也就验不到「不回流」
    这半边；本条测的是渲染，不是接线。
    """
    tactical = ds.create_decision("4a 界面回流提示：当下调整那档",
                                  decision_scope=ds.SCOPE_TACTICAL)
    strategic = ds.create_decision("4a 界面回流提示：长期方向那档",
                                   decision_scope=ds.SCOPE_STRATEGIC)

    assert "这一档按定案不回流" in rc.do_view_review_decision(tactical), (
        "tactical 不在回流集合里，详情区却没说——人会点了才被拒")
    detail = rc.do_view_review_decision(strategic)
    assert "可提炼成经验" in detail, detail[:200]
    # 两个分支都要能被集合本身推出来：界面说「不回流」的那些档，恰好是集合外的
    for scope in ds.VALID_DECISION_SCOPES:
        text = rc.do_view_review_decision(
            ds.create_decision(f"4a 回流提示逐档核对 {scope}", decision_scope=scope))
        eligible = scope in ds.EXPERIENCE_ELIGIBLE_SCOPES
        assert ("可提炼成经验" in text) == eligible, (
            f"{scope} 的界面提示与 EXPERIENCE_ELIGIBLE_SCOPES 的成员判定分叉了")
    print("[OK] 四档作用域的详情区提示逐个与回流集合一致（界面没复制一份判据）")


def _table_row_count(table: str) -> int:
    """数一张表的行数：「一行都没多」这类判据要落在库上，读文案不算验过"""
    conn = md.get_connection()
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _project_state_ids() -> List[int]:
    conn = md.get_connection()
    try:
        return [int(row[0]) for row in conn.execute(
            "SELECT id FROM project_states ORDER BY id").fetchall()]
    finally:
        conn.close()


def _seed_active_goals(*pairs) -> None:
    """把 goals 表换成给定的几条 active 目标（临时库；用例自己在 finally 里清空）

    为什么由测试来摆这两行：`goals` 此刻在生产库里有 2 条 active，而临时库里 0 条——
    「几个 active 目标」正是 `_collect_goal` 分不分支的唯一输入，靠库恰好是什么就是什么，
    这条用例就会随别的用例改动而漂移。
    """
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM goals")
        for goal_id, title in pairs:
            conn.execute(
                "INSERT INTO goals (goal_id, title, status) VALUES (?, ?, 'active')",
                (goal_id, title))
        conn.commit()
    finally:
        conn.close()


def _wipe_active_goals() -> None:
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM goals")
        conn.commit()
    finally:
        conn.close()


def _write_note(goal_id: str) -> None:
    """往临时库写一行笔记，让**真采集器**给出一条 `activity` 证据（阶段一拍板三族之一）

    为什么由测试来摆这一行：第三段要的是「闸门②真的放行」，而放行与否现在只看条目上的
    证据标记。指望前面用例留下的 decisions 恰好非零，是一条会随用例顺序漂移的假设
    （同本文件对 `project_states` 空表那一条的口径），所以自己造一条、自己在 finally 删。
    """
    conn = md.get_connection()
    try:
        conn.execute("INSERT INTO notes (goal_id, title, created_at) "
                     "VALUES (?, '闸门②取证用的一行笔记', datetime('now'))", (goal_id,))
        conn.commit()
    finally:
        conn.close()


def _erase_notes(goal_id: str) -> None:
    """清掉上面那一行：本文件除 decisions／project_states 之外不给库里留东西"""
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE goal_id = ?", (goal_id,))
        conn.commit()
    finally:
        conn.close()


def test_manual_section_widgets_are_in_component_tree():
    """人工段那一格与保存按钮在组件树里，且**给得出该写什么**（判据①的界面那一半）

    取标签文本的口径同 3c／4a 那两条「在树里」的用例。这里额外读 `placeholder` 与
    `lines`：这一格要人写一整段判断，做成一行输入框等于没做——人会写一句「还行」，
    而闸门①只要非空就过，那种快照喂给复盘的是一句空气（清单 §二「一格自由文本」）。
    """
    import gradio as gr
    boxes = [block for block in app_module.demo.blocks.values()
             if isinstance(block, gr.Textbox)
             and "人工段" in str(getattr(block, "label", "") or "")]
    assert len(boxes) == 1, f"人工段那一格该恰好一个，实际 {len(boxes)} 个"
    box = boxes[0]
    assert "必填" in str(getattr(box, "label", "")), (
        f"标签没写明这一格必填：{box.label!r}")
    assert "投入了多少" in (getattr(box, "placeholder", "") or ""), (
        f"提示语没告诉人该写什么：{getattr(box, 'placeholder', None)!r}")
    assert int(getattr(box, "lines", 1) or 1) >= 3, (
        f"这一格被压成 {getattr(box, 'lines', 1)} 行了——人工段是要写整段的")
    assert "💾 保存这份快照（人工段）" in _button_values(), (
        "保存按钮不在树里，或文案变了")
    print("[OK] 人工段那一格（含提示语与行数）与保存按钮都在组件树内")


def test_manual_save_button_chains_snapshot_refresh():
    """保存之后跟着刷一次快照那一格（清单 §一「并刷新快照显示（复用 `refresh_review_snapshot`）」）

    静态读源码，口径照 `test_start_up_backfill_is_in_demo_load_chain`：切片的右边界取
    「这条链结束后第一条独立语句」，所以断言只可能命中链内，不会被别处的同名调用满足；
    而刻意不重跑一次 `create_app()` 去真触发事件（那要把整个界面拉起来两遍）。
    这条为什么值得单独立：人点完保存如果那一格不跟着变，他唯一的核对办法是再点一次
    「🔄 刷新账本」或重开页面——而这一格存在的目的正是让人确认「我这次写的被记下了」。
    """
    with open(_FACTORY_SOURCE, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("review_save_manual_btn.click(")
    end = source.index("review_discuss_btn.click", start)
    chain = source[start:end]
    assert "do_save_manual_section" in chain, "按钮没接上保存回调"
    assert "review_manual_input" in chain, "人工段那一格没接到回调的 inputs 上"
    assert "review_manual_status" in chain, "状态行没接到回调的 outputs 上"
    assert ".then(" in chain and "refresh_review_snapshot" in chain, (
        "保存后没跟着刷一次快照显示：链是 " + chain.replace("\r\n", " ")[:160])
    assert "run_long_term" not in chain and "execute_template" not in chain, (
        "保存按钮的链里出现了触发复盘的东西——边界「保存 ≠ 触发」被 UI 接线破了")
    print("[OK] 保存按钮链：写快照 → 刷状态行 → .then 刷快照，且不含任何触发动作")


def test_save_manual_section_empty_input_writes_no_row():
    """判据④（含负控①）：空输入既要说明为什么不行，也要**一行都不写**

    只断言文案会放过一种实现：它打印了那句话，却照样把空人工段落盘——下一轮复盘仍然
    走 `manual_section_missing`，而库里多了一行没人写过的快照。四种「空」都试：空串、
    纯空格、纯换行、None（界面没填时传来的是空串，而直接调这条链的脚本可能传 None）。
    """
    before = _table_row_count("project_states")
    for bad in ("", "   ", "\n\t  \n", None):
        status_md = rc.do_save_manual_section(bad)
        assert "人工段不能为空" in status_md, f"{bad!r} 没被拒：{status_md}"
        assert "闸门①" in status_md, f"{bad!r} 被拒了却没说为什么：{status_md}"
        assert _table_row_count("project_states") == before, (
            f"{bad!r} 落了库：{before} → {_table_row_count('project_states')}")
    print(f"[OK] 四种空输入全部被拒且零写入（project_states 保持 {before} 行）")


def test_save_manual_section_clears_gate_one_and_keeps_words_verbatim():
    """判据①＋②：保存后闸门①必须过，原话必须**逐字**进观测载荷

    两条合起来才叫「这一格接上了」：只验落库，实现可以把字写进一个闸门读不到的字段；
    只验闸门，实现可以截断或改写原话——而这一格的全部价值就在于它是人的原话
    （`core.project_state.AUTO_UNSERVABLE` 明写自动段读不出「他说过什么」）。
    刻意用带换行的原话：载荷按 `- [kind] content` 逐条渲染，压平一次就看不出是同一句。
    末尾那半是清单的负控②：`manual_items` 传空列表 ⇒ 闸门①不过、载荷里没有那句话。
    """
    text = "这周投入大概十小时\n卡在出题器那条链上\n想法变了：先跑通再谈自动化"
    status_md = rc.do_save_manual_section(text)
    matched = re.search(r"已保存快照 #(\d+)", status_md)
    assert matched, status_md
    state = ps.latest_project_state()
    assert state["id"] == int(matched.group(1)), (
        f"界面报的号与库里最新那份不是同一条：{matched.group(1)} vs {state['id']}")
    assert rv.has_manual_section(state), (
        "落库了却仍过不了闸门①——decision_field 或 kind 接错了")
    manual = [item for item in state["items"] if item["kind"] == ds.KIND_USER_REPORT]
    assert len(manual) == 1, f"一格自由文本该恰好落一条 user_report：{len(manual)}"
    assert manual[0]["decision_field"] == rc._MANUAL_DECISION_FIELD, manual[0]
    assert manual[0]["source"] == rc.UI_SOURCE, (
        f"来源没写界面：{manual[0]['source']!r}")
    assert manual[0]["content"] == text, "原话被改写或截断了"
    payload = rv.build_observation_payload(state)
    assert rv.SECTION_MANUAL in payload and text in payload, (
        "人工段标题或原话没进观测载荷")

    bare = ps.build_project_state(manual_items=[])
    assert not rv.has_manual_section(bare), "manual_items 传空列表却把闸门①放过了"
    assert text not in rv.build_observation_payload(bare)
    print(f"[OK] #{state['id']}：闸门①过、原话逐字进载荷；空 manual_items 那半边仍过不去")


def test_save_manual_section_does_not_trigger_review():
    """边界「保存 ≠ 触发」（清单 §二）：点保存只写快照，不立决策、不跑模板、不调观测

    观测桩在这里是必要的，不是装饰：行数只证明「账本没动」，而实现完全可能真跑了复盘、
    只是停在某个闸门上。桩计数为 0 才把「这一轮复盘被调用过」这件事也钉住。
    阶段一（2026-10-11）之后桩是**两个**：只接观测的话，「保存不花模型调用」这句就只剩
    一半成立——走「请求补充」那一档时被打的是问句旁路，而这条链上没人会替测试发现它。
    触发只有模板页那个「▶️ 运行」一个入口，这是设计定案而不是实现偏好——保存与触发
    绑在一起，人会发现自己每写一句判断就烧掉一次模型调用。
    """
    stub = _ObserveStub()
    ask = _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    decisions_before = _table_row_count("decisions")
    runs_before = _table_row_count("template_runs")
    try:
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        status_md = rc.do_save_manual_section("保存这一下不该顺带把复盘跑了")
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask
    assert "已保存快照" in status_md, status_md
    assert stub.count == 0, f"保存顺带调了观测 {stub.count} 次"
    assert ask.count == 0, f"保存顺带调了问句旁路 {ask.count} 次"
    assert _table_row_count("decisions") == decisions_before, "保存顺带立了决策"
    assert _table_row_count("template_runs") == runs_before, "保存顺带跑了模板"
    print("[OK] 保存只写快照：两条旁路都零调用，decisions／template_runs 行数一字不动")


def test_manual_section_entry_closes_the_gap_end_to_end():
    """判据③（缺口堵上的唯一证明）：保存前后各触发一次，出口必须翻出 manual_section_missing

    两条半边都跑在**真链**上：`TaskExecutor().handle_long_term_review_task` 经
    `latest_project_state()` 读库，而不是拿手搓 dict 喂给复盘函数——后者测的是复盘函数，
    不是这个入口（同 3c-甲 那条「端到端」的口径）。
    负控半边先跑：清空 `project_states`、不保存就触发 ⇒ 出口仍是
    `manual_section_missing`、零观测调用、不立决策。
    第三段是这份证明的收尾：闸门②放行之后观测**真的被调用**、载荷里带着人工段。
    只验「出口换了名字」是不够的——`insufficient_information` 也是换名字。

    ⚠️ 阶段一（2026-10-11）改判闸门②之后，这条有两处必须跟着变，否则它会变成一条
    静默打生产端点的用例：
    - **两个旁路都要接桩**。中间那一段（人工段刚补上、活动证据还没造）在阶段一之后走的
      是「请求补充」那一档，被打的是**问句旁路**；桩只接观测就等于「接上了一个没人调的，
      漏了一个真要调的」。所以三段各有一条计数不变量：闸门①拦下时**两条都零**，闸门①过了
      之后**每轮恰好打一条**（打哪一条由闸门②决定，不由测试决定——所以断言的是
      两条之和，这样既不依赖前面用例留下的 decisions，也不会因为分流改了就该改写）。
    - 第三段原来那句「fact 比 unknown 多所以放行」的判据已经作废（`unknowns_dominant`
      这个函数在阶段一被删了），现在**现采一份真活动读数**：往临时库写一行 notes，
      让采集器自己把它标成 `activity`。这一行是本文件唯一往既有表写的动作，
      finally 里立刻删掉。
    """
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM project_states")
        conn.commit()
    finally:
        conn.close()
    stub = _ObserveStub()
    ask = _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    try:
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        missing = TaskExecutor().handle_long_term_review_task(
            {"task_title": "人工段缺口对拍"})
        assert missing["outcome"] == rv.OUTCOME_MANUAL_MISSING, missing
        assert missing["decision_id"] is None, missing
        assert stub.count == 0 and ask.count == 0, (
            f"闸门①拦下了却仍打了旁路：观测 {stub.count}／问句 {ask.count}")

        rc.do_save_manual_section("保存之后再触发：这一轮该离开缺人工段那个出口")
        flipped = TaskExecutor().handle_long_term_review_task(
            {"task_title": "人工段缺口对拍"})
        assert flipped["outcome"] != rv.OUTCOME_MANUAL_MISSING, (
            f"入口接上了却仍报缺人工段：{flipped}")
        assert flipped["outcome"] in (rv.OUTCOME_PROPOSED, rv.OUTCOME_NO_CHANGE,
                                      rv.OUTCOME_INSUFFICIENT), flipped
        # 这一轮过不过闸门②由库里的活动读数决定（前面用例留下的 decisions 也算一条），
        # 测试不替它假定结论；假定不了结论但可以假定**配额**：一条材料只打一条旁路。
        assert stub.count + ask.count == 1, (
            f"这一轮该恰好打一条旁路，实得观测 {stub.count}／问句 {ask.count}")
        landed = stub.calls if stub.count else ask.calls
        assert rv.SECTION_MANUAL in landed[-1], "人工段没进这一轮的载荷"

        # 第三段：把闸门②也放过去——现采一条真活动读数（写一行 notes），
        # 唯一 active 目标让采集器能把它归到这条目标名下。
        _seed_active_goals(("goal_manual_e2e", "把第一闭环跑通"))
        _write_note("goal_manual_e2e")
        state = ps.build_project_state(manual_items=[
            (rc._MANUAL_DECISION_FIELD, ds.KIND_USER_REPORT,
             "这一轮要把闸门②也过掉", rc.UI_SOURCE)])
        assert rv.has_substantive_evidence(state), (
            f"闸门②仍挡着：{[i.get('evidence') for i in state['items'] if i['kind'] == ds.KIND_FACT]}"
            "——第三段要换构造方式，不能默认它放行")
        rc.do_save_manual_section("这一轮把闸门②也过掉")
        calls_before = stub.count
        asks_before = ask.count
        deep = TaskExecutor().handle_long_term_review_task(
            {"task_title": "人工段缺口对拍"})
        assert stub.count == calls_before + 1, (
            f"闸门都过了却没调观测：新增 {stub.count - calls_before} 次")
        assert ask.count == asks_before, (
            f"这一轮已经有实质依据，不该再花一次问句调用：新增 {ask.count - asks_before} 次")
        assert deep["outcome"] in (rv.OUTCOME_PROPOSED, rv.OUTCOME_NO_CHANGE), deep
        assert rv.SECTION_MANUAL in stub.calls[-1], "最后一轮的载荷里没有人工段"
        print(f"[OK] 端到端：{missing['outcome']} → {flipped['outcome']} → "
              f"{deep['outcome']}（观测 {stub.count} 次／问句 {ask.count} 次）")
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask
        _erase_notes("goal_manual_e2e")
        _wipe_active_goals()


def test_save_manual_section_with_two_active_goals_still_saves():
    """判据⑤：生产库此刻有 2 条 active 目标，这一格不许因此报错，也不许因此要人先选目标

    定案（清单 §二）：`goal_id` 传 None（项目级快照），解不出唯一目标时由 `_collect_goal`
    登记 unknown、**照常落盘**。断言三件事：返回合法快照号、库里 goal_id 为 None、
    unknowns 里有 goal_id 那一条。再加一句界面：那一格要把「目标未绑定」说出来，
    否则人会以为这份快照是关于某个目标的。
    """
    _seed_active_goals(("goal_manual_a", "提高英语成绩"),
                       ("goal_manual_b", "构建长期自进化ai"))
    try:
        before = _table_row_count("project_states")
        status_md = rc.do_save_manual_section("两个目标都在，人工段照样能存")
        matched = re.search(r"已保存快照 #(\d+)", status_md)
        assert matched, status_md
        assert _table_row_count("project_states") == before + 1, (
            f"两个目标时没落库：{before} → {_table_row_count('project_states')}")
        row = ps.get_project_state(int(matched.group(1)))
        assert row["goal_id"] is None, f"这份快照被绑到了一个目标上：{row['goal_id']}"
        unknown_fields = [item["decision_field"] for item in row["unknowns"]]
        assert "goal_id" in unknown_fields, (
            f"目标解不出来却没登记 unknown：{unknown_fields}")
        assert "未绑定" in rc.refresh_review_snapshot(), (
            "快照那一格没说目标未绑定")
        print(f"[OK] 2 个 active 目标：#{matched.group(1)} 照常落库、goal_id None、有 unknown")
    finally:
        _wipe_active_goals()


def test_save_manual_section_is_append_only():
    """边界「追加式」（清单 §二）：每次保存都是新行，旧行一个字不动（不 UPDATE、不 DELETE）

    值得单独钉：快照是复盘的**证据**，覆盖写会让上一轮与这一轮的判断长得一样，
    而 4b 的教训注入要读的正是「上一次那一份」。这里比的是旧行的 items 整体，
    不是行数——行数加了也可能同时把旧行改了。
    """
    rc.do_save_manual_section("追加式的基线那一份")
    ids_before = _project_state_ids()
    assert ids_before, "前面的用例该至少留下一行快照，这里拿不到基线"
    items_before = {state_id: ps.get_project_state(state_id)["items"]
                    for state_id in ids_before}
    status_md = rc.do_save_manual_section("追加式这一条：它只能新增一行")
    matched = re.search(r"已保存快照 #(\d+)", status_md)
    assert matched, status_md
    new_id = int(matched.group(1))
    assert new_id > max(ids_before), f"新行没排在最后：{new_id} vs {ids_before}"
    for state_id in ids_before:
        assert ps.get_project_state(state_id)["items"] == items_before[state_id], (
            f"旧快照 #{state_id} 被改了")
    assert ps.latest_project_state()["id"] == new_id, "最新那份不是刚保存的这条"
    print(f"[OK] 追加式：原有 {len(ids_before)} 行原样不动，#{new_id} 成为最新")


def test_manual_section_field_comes_from_project_state_table():
    """那条人工段的字段名来自 `core.project_state` 那张表，不是界面里写死的一串字符

    定案（清单 §二）：字段名取 `DECISION_ITEM_FIELDS` 里与 kind **同位**的那一个。
    这条断言把它变成可红的：谁在界面里另写一个词表外的字段名，或把 kind 与字段配错，
    落库那条就对不上 `"items." + KIND_USER_REPORT`，也就出不了 `DECISION_ITEM_FIELDS`。
    """
    assert rc._MANUAL_DECISION_FIELD == "items." + ds.KIND_USER_REPORT, (
        rc._MANUAL_DECISION_FIELD)
    assert rc._MANUAL_DECISION_FIELD in ps.DECISION_ITEM_FIELDS, (
        f"界面用的字段名不在 State 的条目字段表里：{rc._MANUAL_DECISION_FIELD}")
    saved = rc.do_save_manual_section("字段名这一条的原话")
    state_id = int(re.search(r"已保存快照 #(\d+)", saved).group(1))
    written = [item for item in ps.get_project_state(state_id)["items"]
               if item["kind"] == ds.KIND_USER_REPORT]
    assert len(written) == 1 and written[0]["decision_field"] == rc._MANUAL_DECISION_FIELD, (
        f"落库那条的字段名与常量分叉了：{written}")
    print(f"[OK] 字段名单一真源：{rc._MANUAL_DECISION_FIELD} 且落库一致")


TESTS = [
    test_app_import_builds_and_isolated,
    test_existing_tab_labels_all_still_present,
    test_review_tab_widgets_are_in_component_tree,
    test_template_form_action_field_offers_every_valid_action,
    test_action_labels_cover_every_valid_action,
    test_end_to_end_review_to_executed_with_empty_prior_intent,
    test_prior_intent_written_as_assumption_when_given,
    test_empty_comment_is_refused_and_nothing_written,
    test_negative_control_mark_executed_before_approval_is_refused,
    test_reject_button_moves_proposed_to_rejected_and_keeps_the_row,
    test_reject_after_approval_works_and_rejected_is_terminal,
    test_reject_without_a_real_target_writes_nothing,
    test_start_up_backfill_is_in_demo_load_chain,
    test_pending_count_shown_in_list_header,
    test_snapshot_line_handles_empty_table_then_a_saved_one,
    test_snapshot_gate_prediction_matches_the_real_gate,
    test_form_can_create_review_template_without_keyword,
    test_negative_control_bad_action_via_form_writes_nothing,
    test_template_form_defaults_carry_action_at_the_end,
    test_review_flowback_widgets_are_in_component_tree,
    test_evaluation_choices_cover_every_valid_evaluation,
    test_mark_executed_reference_written_and_blank_stays_null,
    test_end_to_end_execute_evaluate_and_distill_flowback,
    test_evaluate_before_execution_is_refused_and_writes_nothing,
    test_evaluate_requires_actual_result,
    test_distill_with_empty_lesson_skips_without_writing,
    test_distill_before_evaluation_is_refused,
    test_tactical_decision_flowback_line_matches_the_eligibility_set,
    test_manual_section_widgets_are_in_component_tree,
    test_manual_save_button_chains_snapshot_refresh,
    test_save_manual_section_empty_input_writes_no_row,
    test_save_manual_section_clears_gate_one_and_keeps_words_verbatim,
    test_save_manual_section_does_not_trigger_review,
    test_manual_section_entry_closes_the_gap_end_to_end,
    test_save_manual_section_with_two_active_goals_still_saves,
    test_save_manual_section_is_append_only,
    test_manual_section_field_comes_from_project_state_table,
]


def main():
    try:
        for fn in TESTS:
            fn()
    finally:
        cleanup_test_db()


if __name__ == "__main__":
    main()
