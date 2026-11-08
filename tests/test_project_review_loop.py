# -*- coding: utf-8 -*-
"""
长期项目复盘（core/long_term_project_review.py ＋ 3c-甲 的执行分支接线，
P7 第一闭环 Phase 3b／3c-甲，2026-10-06）测试

全程离线：只碰临时库，**不打网络、不调模型**。判据①–⑥ 那批观测函数一律注入桩；
3c-甲 的四条走的是模板触发路径（`TaskExecutor.execute_template`），它经
`agents.task_executor` 模块属性取观测函数，测试 patch 那个属性即可同样零网络——
每条都当场恢复原值（在 finally 里），不让桩泄漏到后面的用例。
判据逐条对着拆分文档 3b 的原文与设计席补的两条，每条都在这个 docstring 里点名：

- 判据①「两个 View 真分离＝按内容比对，不按字段名」→
  `test_views_are_separated_by_content` ＋ `test_execution_view_carries_no_natural_language`
  ＋ 负控 `test_negative_control_leaky_execution_view_is_caught`；
- 判据②「本周无需决策可通过，且不得生成 Decision 行」→
  `test_no_change_needed_creates_no_decision_row` ＋ 负控
  `test_negative_control_row_counter_has_teeth`（上一条用的那**两把尺**——决策行数与条目
  行数——都必须能量出 +1，否则「行数没变」与「条目数对得上」这两句都是恒真的）；
- 判据③「Unknown 占主导时走 insufficient_information，请求补充而非给方案」→
  ⚠️ **阶段一（2026-10-11）就地换掉了这一条的判据**，用例名与内容一起改：
  `test_unknown_dominant_returns_insufficient_and_records_request` 改名成
  `test_no_activity_fact_requests_supplement_and_records_it`，边界配对
  `test_unknown_equal_to_facts_is_not_dominant` 改名成
  `test_one_activity_fact_is_enough_even_with_many_unknowns`。换的理由不是阈值：
  实测四种真实库形状（新库／一个目标／两个目标／生产库副本＋一条人工段）**全部**被判
  「未知占主导」——unknown 里有五条是自动段结构上给不出的固定项，fact 里多条是
  「读到且为零」的计数，两边都不是项目活动的信号，条数比条数必然永远输。
  新判据是「有没有一条被采集器标为项目活动证据的读数」（`has_substantive_evidence`），
  标记由 `core/project_state` 的生产者声明，本模块只读它、不解析字符串；
  「零观测调用」这句从这一条**挪走**，只留在闸门①那两条里（请求补充那三档现在会打
  问句旁路，见下面「阶段一新增」第 5 条）。
- 判据④「create_template(action_type='long_term_review') 成功、未知类型仍被拒」→
  `test_create_template_accepts_long_term_review` ＋
  `test_unknown_action_type_is_rejected` ＋
  `test_update_template_rejects_invalid_action_without_touching_row`。
  ⚠️ **这条在 3b 时钉的是相反的行为**，改动过程如实留档：3b 的现状是 `create_template`
  把非法 action_type **静默降级成 search_and_note 并照常建行**，与设计席判据里「要报错」
  那一读不符；执行席当时把它上报而没有就地改，设计席裁定「正确目标行为是**拒绝**，
  但改的时机是 3c，不是现在」，理由三条——3b 时这条路径是死代码（界面上造不出这种
  模板）、3c 才是它变成用户可达的那一刻、项目已有 fail-closed 先例
  （`core/decision_store` 的「规则违规 raise ValueError，与 ApprovalStore 的异常安全
  默认值刻意不同」）。3c-乙 给模板表单加了动作类型那一格 ⇒ 路径可达，本批据此改成
  raise 且不建行，并按设计席的要求补了负控（摘掉拒绝，非法值必须重新变绿）。
  3b 时那段「两种读法」的分析与这次的裁定一起记在 PROJECT_STATUS.md §五.52／§五.53；
- 判据⑤（设计席新增）「`VALID_ACTIONS` 增员后既有两动作行为不变——快照比对，别 assert ==」→
  `test_existing_two_actions_unchanged_by_snapshot`。基线不是在这个文件里编的：它由
  `git worktree add` 出来的**改前代码**跑同一份取证脚本得到（日期与自增 id 已归一化），
  顺序颠倒就等于把改动后的行为写成基线。**收尾时还复算过一次**：同一脚本在 worktree 里
  重跑，用本文件的 `_snapshot_diff` 对拍下面这份内嵌基线，逐键一致——复算过程与结论记在
  `PROJECT_STATUS.md` §五.52（取证脚本随 worktree 一起删，所以这里留的是「怎么再验一遍」，
  不是一个再也找不到的文件路径）。
- 判据⑥（设计席新增）「喂给观测的材料必须含人工段；人工段为空时宁可不产出 Decision」→
  `test_manual_section_missing_is_the_first_gate` ＋ 配对
  `test_adding_one_manual_item_changes_the_exit`（同一份 state 只差一条人工段，出口必须
  不同——这条证明闸门不是因为 state 被读成空才恒红）。

另有两条钉住「结构性事实」的：
- `test_observe_key_literals_match_agents_supervisor`：本模块因为层序（core 不许引
  agents）把观测的四格键名、问句旁路的输出键与「一次最多问几条」按字面量复制了一份，
  这条逐值比对 `agents.supervisor`，改名或改上限即红。

**Phase 3c-甲（2026-10-06 接线）新增四条，并且就地反转了 3b 的一条**：
- 3b 时 `test_long_term_review_template_builds_no_plan` 钉的是「立动作 ≠ 能执行」
  （`long_term_review` 进了 `VALID_ACTIONS` 但执行侧返回空计划）。3c-甲 按设计席裁定
  把执行分支接进 `agents/task_executor.py`，那条断言已**改写成反面**
  `test_review_template_builds_one_review_task`——留着旧的恒绿版本，就等于让文档里
  「接线已完成」那句话没有测试撑着；
- `test_execute_template_review_writes_decision_without_approval` 是设计席要的端到端：
  建模板 → 手动触发 → 账本里多一行 proposed/operational → **不产生待审批项**（复盘的产物是
  账本行而不是待确认的文档，所以它不走审批暂停点，理由写在 handler 的 docstring 里）；
- `test_review_handler_falls_back_when_no_snapshot_exists` 钉「project_states 是空表」
  那条读法（`latest_project_state()` 返回 None 而不是抛）：接线回退去现采一份只含自动
  段的，让闸门①出声、不写库。生产库今天就是 0 行，3c-乙 的启动回填必须处理这个 None；
- `test_negative_control_missing_no_approval_flag_still_pauses` 是上一条端到端里
  「没有 approval 事件」那句的负控：复盘先落账后返回，假如分发点不读
  `requires_approval` 这格，端到端仍可能因为「账确实写了」而假绿。

**Phase 3c-乙（2026-10-06，模板表单加动作类型 ＋ 判据④改拒）动的是本文件的两条**：
- `test_unknown_action_type_keeps_current_semantics`（钉「静默降级」）**就地反转**成
  `test_unknown_action_type_is_rejected`。做法与 3c-甲 反转「立动作 ≠ 能执行」那条一致：
  留着旧的恒绿版本，文档里那句「已改成拒绝」就没有测试撑着；
- 新增 `test_update_template_rejects_invalid_action_without_touching_row`：拒绝的含义是
  **整行不动**，所以校验必须排在拼 SQL 之前——只 raise 却先把名字写进去，留下的是
  一条名字与动作对不上的模板，比报错更坏。
**阶段一（2026-10-11，决策模型改造）在本文件加的是闸门②换判据之后的那一整批形状**：
1. `test_no_activity_fact_requests_supplement_and_records_it` 与配对放行用例
   `test_one_activity_fact_is_enough_even_with_many_unknowns`——前者用「摘掉活动标记、
   条数不动」的夹具命中请求补充，后者用「1 条活动读数 vs 20 条未知」证明新判据不再比条数；
2. `test_gate_two_reads_the_produced_marker` 走**真采集器**（临时库真写一行笔记），
   把手搓夹具与生产标记对了一次表；
3. `test_three_request_shapes_say_three_different_things`：三档的 title／summary／reason
   两两不同（三份夹具的 state_id 相同，所以差异只可能来自措辞）；
4. `test_confidence_splits_suggestion_from_exploratory` ＋
   `test_negative_control_threshold_moves_the_tier`：两档都落 proposed、都不新增列，
   负控在内存里挪 `CONFIDENCE_SUGGESTION_MIN` 证明阈值确实是那一处常量；
   `test_historical_snapshot_without_markers_is_read_honestly` 钉「缺标记」与「读到零」的
   两种话术不许混；
5. **两个旁路**（这是「零观测调用」那句从判据③挪走的直接原因）：
   `test_ask_bypass_questions_are_recorded_with_their_source` 与
   `test_empty_ask_bypass_falls_back_and_says_so` 验问句旁路的来源标注，
   `test_choose_goal_shape_calls_neither_bypass` 验「先选目标」那一档两条旁路都不打
   （模块 docstring 那张配额口径表的可执行形式），
   `test_review_handler_injects_the_ask_bypass` 验接线把**第二个**旁路也递进去了——
   端到端那条走的是有依据那一档，问句旁路本来就不该被打，它只能证明「没被打爆」；
6. 视图那两条的覆盖面同步扩到**四出口 × 五形状**（`GATE_SHAPES` 里每一档都真的被命中
   一次），并在程序视图那一条里加验「阶段一新增的三格都不进 Execution View」。
界面那一头的判据④（表单能选复盘动作、非法值经表单也一行都不写、动作标签逐个覆盖）
在 `tests/test_review_tab.py`。
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：本文件**会写库**（decisions／decision_items／task_templates），
# ---- 所以必须在导入任何会访问数据库的模块之前先切到临时库
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402 （隔离生效之后才导入）
from core import decision_store as ds  # noqa: E402
from core import long_term_project_review as rv  # noqa: E402
from core import project_state as ps  # noqa: E402
from core import task_template as tt  # noqa: E402
from agents.task_executor import TaskExecutor  # noqa: E402
import agents.task_executor as te  # noqa: E402  （3c-甲 的接线测试要 patch 它模块里的三个名字）

md.init_db()

# ---------- 观测桩 ----------

_UNABLE = {
    rv.OBSERVE_KEY_PROBLEM: rv.UNABLE_TO_JUDGE,
    rv.OBSERVE_KEY_NEXT_STEP: rv.UNABLE_TO_JUDGE,
    rv.OBSERVE_KEY_CAPABILITY: [rv.UNKNOWN_CAPABILITY],
    rv.OBSERVE_KEY_CONFIDENCE: None,
}
_GOOD_REPLY = {
    rv.OBSERVE_KEY_PROBLEM: "模板跑了但产出没人读，说明产出的东西不解决问题",
    rv.OBSERVE_KEY_NEXT_STEP: "先只留一条每周复盘，其余模板停用一周",
    rv.OBSERVE_KEY_CAPABILITY: ["read_write_notes", "recall_local_material"],
    # 阶段一的第四格。0.8 在 `CONFIDENCE_SUGGESTION_MIN` 之上 ⇒ 走「建议」那一档，
    # 所以下面 `row["summary"] == 观测那句逐字` 的既有断言仍然成立（设计席定案：
    # 两档都落账，差别只在措辞，suggestion 的 summary 不动）。
    rv.OBSERVE_KEY_CONFIDENCE: 0.8,
}
#: 同一份材料、置信度掉在线下 ⇒ 必须改口说成「探索性假设」，但仍然落账
_LOW_CONF_REPLY = dict(_GOOD_REPLY)
_LOW_CONF_REPLY[rv.OBSERVE_KEY_CONFIDENCE] = 0.2

#: 桩「现生成」的那三问。刻意与 `core` 内置兜底问的措辞**全都不通**，这样
#: 「结果里是旁路给的三条」与「走的是兜底」两种情况才能靠字符串本身分开（判据⑤的负控）。
_MODEL_QUESTIONS = [
    "桩问甲：这一轮你希望我按哪一条来判？",
    "桩问乙：已经做出来的东西里，哪一件能直接用上？",
    "桩问丙：这周推进到哪一步就算有进展？",
]


class _ObserveStub:
    """记录调用次数与收到的载荷；永远返回给定回复（不碰网络）"""

    def __init__(self, reply: Dict[str, Any]) -> None:
        self.reply = reply
        self.calls: List[str] = []

    def __call__(self, payload: str) -> Dict[str, Any]:
        self.calls.append(payload)
        return dict(self.reply)

    @property
    def count(self) -> int:
        return len(self.calls)


class _AskStub:
    """问句旁路的桩：返回固定的几条问题，同样记录调用次数与载荷

    为什么必须是**第二个桩对象**而不是复用观测桩：阶段一的三个请求补充形状里有两个会打
    这一条旁路，而结果里 `asked` 与 `observed` 是分开的两格（「这轮问了问题」与「这轮给过
    判断」是两件事）。两条旁路合成一个计数，就再也验不出「只打了问句、没打观测」那一档。
    """

    def __init__(self, questions: Optional[List[str]] = None) -> None:
        self.questions = list(_MODEL_QUESTIONS if questions is None else questions)
        self.calls: List[str] = []

    def __call__(self, payload: str) -> Dict[str, Any]:
        self.calls.append(payload)
        return {rv.ASK_KEY_QUESTIONS: list(self.questions)}

    @property
    def count(self) -> int:
        return len(self.calls)


# ---------- State 夹具 ----------

def _fact(field: str, content: str, source: str,
          evidence: Optional[str] = ps.EVIDENCE_ACTIVITY) -> Dict[str, Any]:
    """自动段条目：kind 固定 fact，source 必须非空（Phase 2 判据①的口径）

    阶段一加了第四格 `evidence`，默认给 `activity`——本文件的基线夹具要的是「能走到观测」
    那一份，而闸门②读的就是这一格。**传 `evidence=None` 时这一格整键不写**，这是本文件
    唯一能造出「历史快照（标记引入之前存的那一份）」形状的地方：读回来 `.get("evidence")`
    是 None，与「读到了一条零」是两件事，`_record_request` 的 reason 要按前者说话。
    """
    item: Dict[str, Any] = {
        "decision_field": field, "kind": "fact", "content": content, "source": source,
    }
    if evidence:
        item["evidence"] = evidence
    return item


def _manual(field: str, kind: str, content: str, source: str) -> Dict[str, Any]:
    """人工段条目：kind 是 fact 以外的那几类（刻意**不带** evidence，闸门②不看这一堆）"""
    return {"decision_field": field, "kind": kind, "content": content, "source": source}


def _unknown_record(field: str, reason: str,
                    code: Optional[str] = None) -> Dict[str, Any]:
    """一份 unknowns 条目，形状与 `core.project_state.build_project_state` 组装的一致

    `code` 同样是「给了才写」（那条规则在 `tests/test_project_state.py` 里钉着）。这里必须
    能造出带码的那一份，因为「先选目标」与「引导建立上下文」两档**只靠这一格分流**——
    没有码的夹具就只能测到默认档。
    """
    record: Dict[str, Any] = {"decision_field": field, "reason": reason}
    if code:
        record["code"] = code
    return record


def _state(facts: List[Dict[str, Any]], manual: List[Dict[str, Any]],
           unknowns: List[Dict[str, str]], state_id: int = 7,
           goal_id: str = "goal_probe") -> Dict[str, Any]:
    """拼一份与 core.project_state 读回来同形的 State（键名照 _state_row_to_dict）

    刻意不从采集器取：三个请求补充形状与两档置信度要在**受控的证据标记／unknown 码**下
    分别命中，而采集器在临时库上只会给那一种它自己采得出的形状。

    阶段一之前这段写的是「采集器在临时库上只会给『未知多于事实』那一种形状」——那是旧
    闸门②的口径。判据已经换成「有没有一条活动证据」，留着那句会让人以为这套夹具是在绕开
    一个计数阈值。真实采集器那一份由
    `test_real_collector_state_renders_three_sections` 与
    `test_gate_two_reads_the_produced_marker` 单独用。
    """
    return {
        "id": state_id,
        "snapshot_at": "2026-10-06T12:00:00+00:00",
        "goal_id": goal_id,
        "items": list(facts) + list(manual),
        "unknowns": list(unknowns),
        "note": None,
    }


def _base_state(**overrides) -> Dict[str, Any]:
    """一份「能走到观测」的基线 State：3 条事实 ＋ 1 条人工段 ＋ 1 条未知

    三条事实的**证据标记按真采集器的口径分开标**（不是三条全标 activity）：笔记计数是
    项目活动，最近一次测试结果与仓库卫生台账按定案不是。这样闸门②在本夹具上过的理由是
    「真有一条活动读数」，而不是「夹具把所有条目都标成了活动」——后者会让下面那条
    「一条活动读数就够」的用例失去可分辨的形状。
    """
    facts = [
        _fact("items.notes.total", "7", "table:notes[goal_id=goal_probe]"),
        _fact("items.test_run.latest", "run_id=114", "table:test_runs.id",
              evidence=ps.EVIDENCE_CONTEXT),
        _fact("items.debt_ledgers.total", "22", "file:checks/debt_baseline.json",
              evidence=ps.EVIDENCE_CONTEXT),
    ]
    manual = [_manual("summary", "user_report",
                      "模板产出的笔记我一篇都没读过", "用户 2026-10-06 口述")]
    unknowns = [_unknown_record("items.test_run.is_full_run",
                                "表里没有列能区分全量与子集跑")]
    merged = {"facts": facts, "manual": manual, "unknowns": unknowns}
    merged.update(overrides)
    return _state(merged["facts"], merged["manual"], merged["unknowns"])


def _no_activity_facts() -> List[Dict[str, Any]]:
    """三条「读到但为零／不是活动族」的自动段条目：闸门②在这堆上必须判「没有实质依据」

    刻意保留**条目本身**而不是清空 facts：闸门②改判之后，「一条事实都没有」与「有五条事实
    但没有一条是活动读数」是两种完全不同的形状，前者在真实库里几乎不存在（采集器结构上
    总会给出几条 unknown 与若干计数）。这一堆正是用户卡住的那一种。
    """
    return [
        _fact("items.notes.total", "0", "table:notes[total=0]",
              evidence=ps.EVIDENCE_ACTIVITY_EMPTY),
        _fact("items.test_run.latest", "run_id=114", "table:test_runs.id",
              evidence=ps.EVIDENCE_CONTEXT),
        _fact("items.debt_ledgers.total", "22", "file:checks/debt_baseline.json",
              evidence=ps.EVIDENCE_CONTEXT),
    ]


def _request_state(shape: str) -> Dict[str, Any]:
    """按形状拼一份「没有实质依据」的 State（三个请求补充形状共用一份人工段）

    分流只由两件事决定：`goal_id` 定没定，以及 unknown 里有没有 `goal_ambiguous` 那个码。
    build_context 那一份刻意**带着 `goal_none` 码**进来——`_request_shape` 只看 ambiguous
    那一格，所以带了这个码也必须落 build_context；这条由
    `test_three_request_shapes_say_three_different_things` 断言，不留成「看起来在读两个码」。
    """
    manual = [_manual("summary", "user_report",
                      "模板产出的笔记我一篇都没读过", "用户 2026-10-11 口述")]
    # 三份夹具的 state_id 刻意**全部相同**：`_shape_wording` 的 title 里带着快照出处，
    # 要是三份用不同编号，「三者文案必须两两不同」就靠编号也能满足，那条断言就成了装饰。
    if shape == rv.SHAPE_ASK_DIRECTION:
        return _state(_no_activity_facts(), manual,
                      [_unknown_record("items.goal_tasks.total", "读不出来")],
                      state_id=7, goal_id="goal_probe")
    if shape == rv.SHAPE_CHOOSE_GOAL:
        return _state(_no_activity_facts(), manual,
                      [_unknown_record(
                          "items.goal_id",
                          "goals 表里有不止一个 status='active' 的目标，采集层不替人挑",
                          ps.UNKNOWN_CODE_GOAL_AMBIGUOUS)],
                      state_id=7, goal_id=None)
    return _state(_no_activity_facts(), manual,
                  [_unknown_record("items.goal_id",
                                   "goals 表里选不出 status='active' 的目标",
                                   ps.UNKNOWN_CODE_GOAL_NONE)],
                  state_id=7, goal_id=None)


def _count_decisions() -> int:
    """账本行数（负控与"不得生成 Decision 行"共用这把尺）"""
    conn = md.get_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])
    finally:
        conn.close()


def _count_items(decision_id: int) -> int:
    conn = md.get_connection()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM decision_items WHERE decision_id = ?",
            (decision_id,)).fetchone()[0])
    finally:
        conn.close()


def _facts_without_activity(state: Dict[str, Any]) -> Dict[str, Any]:
    """把一份 State 里**已有的**活动标记全改成「读到但为零」，其余一字不动

    这是本文件唯一的「摘掉实质依据」的做法：不改条数、不删条目、不动 unknowns，也不给
    原本没有 evidence 键的历史条目**补上**这一格（那是另一种形状，见
    `test_historical_snapshot_without_markers_is_read_honestly`）。
    """
    for item in state["items"]:
        if item.get("kind") == "fact" and "evidence" in item:
            item["evidence"] = ps.EVIDENCE_ACTIVITY_EMPTY
    return state


def _write_note(goal_id: str) -> None:
    """往临时库写一行笔记——真采集器把 notes 计数认作项目活动证据（阶段一拍板三族之一）"""
    conn = md.get_connection()
    try:
        conn.execute("INSERT INTO notes (goal_id, title, created_at) "
                     "VALUES (?, '闸门②取证用的一行笔记', datetime('now'))", (goal_id,))
        conn.commit()
    finally:
        conn.close()


def _erase_notes(goal_id: str) -> None:
    """清掉上面那一行：本文件除 decisions 之外不碰既有表，留着会让后面的用例看见脏库"""
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE goal_id = ?", (goal_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- 隔离与结构 ----------

def test_isolated_database_and_new_tables_exist():
    """本文件写的必须是临时库；三张新表由 init_db() 在临时库里建出来"""
    current = os.path.abspath(md.DB_PATH)
    assert "_test_tmp_" in current.replace(os.sep, "/"), (
        f"DB 没落在临时目录上，本文件的写会打到生产库：{current}")
    # 与生产库路径正面对撞一次：临时库在子目录里、文件名也不同，两条都不许相等
    assert current != os.path.abspath(os.path.join("data", "memory.db")), (
        f"DB 指向生产库：{current}")
    tables = set()
    conn = md.get_connection()
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    for name in ("decisions", "decision_items", "project_states", "task_templates"):
        assert name in tables, f"临时库里没有 {name} 表，本文件的断言全部失去依据"
    print("[OK] 临时库隔离生效，本文件依赖的表都在")


def test_observe_key_literals_match_agents_supervisor():
    """层序导致的字面量复制必须由一条跨层比对钉住

    阶段一在这本表上加了**三处**新字面量（观测第四格「置信度」、问句旁路的输出键、
    一次最多问几条），它们全都是「两处手工保持一致」的形状：supervisor 那边改名或改上限，
    复盘这一头不会报错，只会把人写的键静默读成「模型没报」。所以这三处都进这一条比对，
    改名即红。
    """
    import agents.supervisor as sv
    assert rv.OBSERVE_KEYS == tuple(sv.OBSERVE_KEYS), (
        f"观测四格键名或顺序与本模块不一致：{sv.OBSERVE_KEYS} vs {rv.OBSERVE_KEYS}")
    assert rv.OBSERVE_KEY_CONFIDENCE == sv.OBSERVE_KEY_CONFIDENCE, (
        "置信度那一格的键名不一致：本模块会把模型报了置信度的观测读成「没拿到数」，"
        "于是每一轮都说成探索性假设，而且没有任何地方报错")
    assert rv.ASK_KEY_QUESTIONS == sv.ASK_KEY_QUESTIONS, (
        "问句旁路的输出键不一致：读不到问题就一律走内置兜底问，"
        "而 reason 里那句「旁路没给出问题」会是假的")
    assert rv.REQUEST_QUESTION_LIMIT == sv.ASK_QUESTION_LIMIT, (
        f"一次最多问几条在两处各写了一份且已经分叉：{sv.ASK_QUESTION_LIMIT} vs "
        f"{rv.REQUEST_QUESTION_LIMIT}")
    assert rv.UNABLE_TO_JUDGE == sv.UNABLE_TO_JUDGE, (
        "「无法判断」哨兵被改名了：本模块会把判不了的观测读成可用结论")
    assert rv.UNKNOWN_CAPABILITY == sv.UNKNOWN_CAPABILITY, "unknown 哨兵不一致"
    print("[OK] 复盘模块与 supervisor 的字面量（四格键名＋问句键＋条数上限）逐值相同")


def test_real_collector_state_renders_three_sections():
    """用 Phase 2 的真采集器产一份 State，载荷三段齐全且人工段原话逐字在里面

    这条把夹具与真实形状对了一次表：`_state()` 是手搓的，采集器改过键名之后这里会红，
    而不是让所有断言继续在一份过期夹具上自说自话。
    """
    state = ps.build_project_state(manual_items=[
        ("summary", "user_report", "模板产出的笔记我一篇都没读过", "用户 2026-10-06 口述"),
    ])
    payload = rv.build_observation_payload(state)
    assert rv.SECTION_FACTS in payload, "自动段标题没进载荷"
    assert rv.SECTION_MANUAL in payload, "人工段标题没进载荷——判据⑥的门形同虚设"
    assert rv.SECTION_UNKNOWN in payload, "未知段标题没进载荷"
    assert "模板产出的笔记我一篇都没读过" in payload, "人工段原话没逐字进载荷"
    assert "\n" in payload, "载荷必须是多行材料，否则又退回成「一句话」那格"
    print("[OK] 真采集器的 State 摊成三段载荷，原话逐字送达")


# ---------- 判据⑥：人工段是门 ----------

def test_manual_section_missing_is_the_first_gate():
    """人工段为空 ⇒ 不调用观测、不立 Decision、连载荷都不生成"""
    state = _base_state(manual=[])
    stub = _ObserveStub(_GOOD_REPLY)
    before = _count_decisions()
    result = rv.run_long_term_project_review(state, stub)
    assert result["outcome"] == rv.OUTCOME_MANUAL_MISSING, f"出口不对：{result}"
    assert stub.count == 0, f"人工段为空却调了观测 {stub.count} 次"
    assert result["payload"] is None, "闸门没过不该产出载荷"
    assert _count_decisions() == before, (
        "人工段为空却写了账——设计席这条要的是「宁可不产出 Decision」")
    assert result["counts"]["manual"] == 0
    print("[OK] 人工段缺失：零观测调用、零 Decision 行")


def test_adding_one_manual_item_changes_the_exit():
    """配对负控：同一份 State 只加一条人工段，出口必须变——上一条不是因为读空而恒红"""
    without = rv.run_long_term_project_review(
        _base_state(manual=[]), _ObserveStub(_GOOD_REPLY))
    with_manual = rv.run_long_term_project_review(
        _base_state(), _ObserveStub(_GOOD_REPLY))
    assert without["outcome"] == rv.OUTCOME_MANUAL_MISSING
    assert with_manual["outcome"] == rv.OUTCOME_PROPOSED, (
        f"补了人工段却还没走到观测：{with_manual['outcome']}")
    assert with_manual["observed"] is True
    print("[OK] 一条人工段就把出口从「缺人工段」翻到「立决策」")


# ---------- 闸门②（阶段一改判「有没有实质依据」） ----------

def test_no_activity_fact_requests_supplement_and_records_it():
    """没有一条活动证据 ⇒ insufficient_information：不给方案、不打观测、落一行请求补充

    阶段一之前这条叫 `test_unknown_dominant_returns_insufficient_and_records_request`，
    钉的是「未知条数 > 事实条数」。现在这一份夹具的**条数配比没动**（3 条事实、1 条未知，
    按旧判据是放行的一档），只是把活动标记全摘掉——所以它测的确实是「有没有活动读数」，
    不是条数。反过来这一份在旧判据下是绿的、在新判据下是红的，这正是换判据要的方向。
    """
    state = _base_state(facts=_no_activity_facts())
    stub = _ObserveStub(_GOOD_REPLY)
    before = _count_decisions()
    result = rv.run_long_term_project_review(state, stub)  # ask_questions 缺省＝None
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT, f"出口不对：{result['outcome']}"
    assert result["gate_shape"] == rv.SHAPE_ASK_DIRECTION, (
        f"goal_id 明明指着 goal_probe，形状却判成 {result['gate_shape']}——"
        "`_request_shape` 那三步顺序（先看目标、再判多个、最后才是没有）被改了")
    assert stub.count == 0, "请求补充这一档不给方案，调观测就是给方案"
    assert result["observed"] is False and result["asked"] is False, (
        "asked 与 observed 是两件事：这一档两个旁路都没打，两格都该是 False")
    assert result["questions"] == list(rv._FIXED_QUESTIONS[rv.SHAPE_ASK_DIRECTION]), (
        f"没注入问句旁路时该用内置兜底问，实得 {result['questions']}")
    assert len(result["questions"]) == rv.REQUEST_QUESTION_LIMIT, (
        "设计席的规格是「三问」，兜底档也得给满三条才算把这一条判据接上")

    assert result["decision_id"] is not None, (
        "请求补充必须落一行账，否则下一轮没人知道上一轮到底问过什么")
    row = ds.get_decision(result["decision_id"])
    assert row["status"] == ds.STATUS_INSUFFICIENT, f"状态没走够：{row['status']}"
    assert row["decision_scope"] == rv.REVIEW_DECISION_SCOPE
    assert _count_decisions() == before + 1, "请求补充只落一行"
    items = ds.list_items(result["decision_id"])
    kinds = {item["kind"] for item in items}
    assert ds.KIND_UNKNOWN in kinds, "未知没进条目，请求补充就没有内容"
    assert ds.KIND_INFERENCE not in kinds, "这一轮不许产出推断——没做过观测就没有推断的来源"
    assert ds.KIND_FACT not in kinds, (
        "请求补充这一档不抄自动段：这一行账要记的是「当时缺什么」，不是「当时读到多少」")
    sources = {item["source"] or "" for item in items if item["kind"] == ds.KIND_UNKNOWN}
    assert any("内置兜底问" in s for s in sources), (
        f"三条兜底问的出处没标明是谁给的，将来和模型生成的混成一堆：{sources}")
    assert "这三问是内置的通用引导问" in result["reason"], (
        "走了兜底却没在 reason 里明说这三问不是针对这份快照生成的——"
        "用户会把通用问当成系统真的懂他")
    assert "未调用问句旁路（三问走内置兜底）" in result["skipped"], (
        f"skipped 没如实说配额花没花：{result['skipped']}")
    human = rv.render_human_view(result)
    assert human["建议下一步"] == "本轮没有做观测", f"给人看的方案句出现了：{human}"
    assert human["等你补充的问题"] == result["questions"], (
        "问出来的三条没原样进 Human View——藏在段落里等于没问")
    assert human["这一轮的说法"] == rv.SHAPE_LABELS_ZH[rv.SHAPE_ASK_DIRECTION]
    assert rv.to_execution_view(result)["observed"] is False
    print("[OK] 无活动证据：零观测调用，落一行 insufficient_information（兜底三问、出处已标）")


def test_one_activity_fact_is_enough_even_with_many_unknowns():
    """一条活动读数就够：未知再多也不再挡决策（阶段一改判据要解决的正是这一条）

    这一份夹具是「旧判据必然挡下、新判据必须放行」的形状：20 条未知 vs 3 条事实。
    末尾把唯一那条活动读数也摘掉，出口必须翻回请求补充——没有那半句，前面的绿就只是
    「闸门②恒不挡」，那是另一种坏（把用户从「永远得不到决策」推到「永远拿依据不足的话敷衍」）。
    """
    unknowns = [_unknown_record(f"f{i}", "读不出来") for i in range(20)]
    state = _base_state(unknowns=unknowns)
    facts = [item for item in state["items"] if item["kind"] == "fact"]
    activity = [item for item in facts if item.get("evidence") == ps.EVIDENCE_ACTIVITY]
    assert (len(facts), len(unknowns), len(activity)) == (3, 20, 1), (
        f"夹具形状不对：事实 {len(facts)}／未知 {len(unknowns)}／活动读数 {len(activity)}——"
        "这一条测的就不是「一条活动读数顶过二十条未知」")
    assert rv.has_substantive_evidence(state) is True, (
        "有一条 activity 标记却被判成没有实质依据：闸门②读的不是这个常量")
    stub = _ObserveStub(_GOOD_REPLY)
    result = rv.run_long_term_project_review(state, stub)
    assert result["outcome"] == rv.OUTCOME_PROPOSED, (
        f"二十条未知又把这一轮挡在请求补充上了：{result['outcome']}｜{result['reason']}")
    assert result["gate_shape"] in (rv.SHAPE_SUGGESTION, rv.SHAPE_EXPLORATORY)
    assert stub.count == 1, f"放行之后该真的观测一次，实得 {stub.count} 次"

    before = _count_decisions()
    flipped = _base_state(unknowns=unknowns, facts=_no_activity_facts())
    assert rv.has_substantive_evidence(flipped) is False, (
        "摘掉唯一那条活动读数之后闸门②还放行——它读的不是证据标记")
    flipped_result = rv.run_long_term_project_review(
        flipped, _ObserveStub(_GOOD_REPLY))
    assert flipped_result["outcome"] == rv.OUTCOME_INSUFFICIENT, (
        "同一份夹具只差证据标记，出口却没翻回去：上面那条放行是恒真的")
    assert _count_decisions() == before + 1, "翻回去之后仍然只落一行（这一档是要落账的）"
    print("[OK] 一条活动读数放行（20 条未知挡不住）；摘掉它同一份夹具必须回到请求补充")


def test_gate_two_reads_the_produced_marker():
    """闸门②读的是**真采集器**标出来的那一格，不是本文件手搓的夹具

    这条把「夹具与生产对得上」钉住：采集器要是改成只写 activity_empty、或把标记名换了，
    `_base_state()` 那几份手搓标记照样能让上面的用例全绿，而真实库里的复盘永远走请求补充
    ——用户看到的就是「改了判据还是反复得不到决策」。所以这里必须走
    `ps.build_project_state`，并且临时库里**真的有一行笔记**。
    """
    goal_id = "goal_gate2"
    _write_note(goal_id)
    try:
        state = ps.build_project_state(goal_id=goal_id, manual_items=[
            ("summary", "user_report", "模板产出的笔记我一篇都没读过", "用户 2026-10-11 口述"),
        ])
        marked = [item for item in state["items"]
                  if item["kind"] == "fact"
                  and item.get("evidence") == ps.EVIDENCE_ACTIVITY]
        assert marked, f"库里有一行笔记，采集器却没标出任何活动证据：{state['items']}"
        assert rv.has_substantive_evidence(state) is True, (
            "采集器标了 activity、闸门②却不认——两边比的不是同一个常量，"
            "`core.project_state` 与这里各留了一份字面量")
        stripped = _facts_without_activity(state)
        assert rv.has_substantive_evidence(stripped) is False, (
            "把标记全改成 activity_empty 之后闸门②仍然放行：它没在读这一格")
    finally:
        _erase_notes(goal_id)
    print("[OK] 真采集器的标记被闸门②读到（有笔记⇒放行，改成清零标记⇒挡下）")


def test_three_request_shapes_say_three_different_things():
    """三个请求补充形状的 title／summary／reason 必须两两不同（设计席：三者文案必须不同）

    为什么单独立一条，而不满足于上面那条只走了 ask_direction：如果实现是「一句通用文案＋
    一个形状词」，三档的差异就只在 `gate_shape` 那一格里，用户收到的其实是同一句话，而
    「先选目标」和「连目标都没有」要他做的动作完全不同。三份夹具的 state_id 相同（见
    `_request_state`），所以文案差异只能来自措辞本身。
    """
    shapes = [rv.SHAPE_ASK_DIRECTION, rv.SHAPE_BUILD_CONTEXT, rv.SHAPE_CHOOSE_GOAL]
    results = {shape: rv.run_long_term_project_review(
        _request_state(shape), _ObserveStub(_GOOD_REPLY)) for shape in shapes}
    assert [results[shape]["gate_shape"] for shape in shapes] == shapes, (
        f"三份夹具没各自命中一个形状："
        f"{ {k: v['gate_shape'] for k, v in results.items()} }")
    assert {r["outcome"] for r in results.values()} == {rv.OUTCOME_INSUFFICIENT}, (
        "拍板 4 定的是「出口四值不动」：三个形状都只能落在 insufficient_information 里")
    rows = {shape: ds.get_decision(results[shape]["decision_id"]) for shape in shapes}
    for i, first in enumerate(shapes):
        for second in shapes[i + 1:]:
            for field in ("title", "summary"):
                assert rows[first][field] != rows[second][field], (
                    f"{first} 与 {second} 的 {field} 是同一句话：{rows[first][field]}")
            assert results[first]["reason"] != results[second]["reason"], (
                f"{first} 与 {second} 的 reason 是同一句话：{results[first]['reason']}")
    # 每一档要有**自己那句**指向动作的话，否则「两两不同」可能只是措辞随机
    assert "询问方向" in results[rv.SHAPE_ASK_DIRECTION]["reason"]
    assert "引导建立上下文" in results[rv.SHAPE_BUILD_CONTEXT]["reason"]
    assert "先选目标" in results[rv.SHAPE_CHOOSE_GOAL]["reason"]
    # 分流只看 ambiguous 那一格：build_context 那份夹具带着 goal_none 码，必须仍落默认档
    assert results[rv.SHAPE_BUILD_CONTEXT]["gate_shape"] == rv.SHAPE_BUILD_CONTEXT, (
        "unknown 里带 goal_none 码就被判成另一个形状：`_request_shape` 在看第二个码了，"
        "而模块头部明说它只比 ambiguous 那一格")
    print("[OK] 三个请求补充形状：两两不同的三格文案，出口仍是同一个词")


def test_ask_bypass_questions_are_recorded_with_their_source():
    """注入问句旁路 ⇒ 结果里是它给的三条，账里那一格写明「模型现生成」

    拍板 2 定的是「三问由模型现生成」而不是固定文案，所以这条同时钉三件事：
    ① 三条问题原样进结果与 Human View；② 落进 `decision_items` 的 source 标的是问句旁路；
    ③ 打的是**问句**旁路，观测旁路一次都没被打（这就是 `asked` 与 `observed` 分成两格的
    可执行形式）。载荷还必须是同一份多行材料——旁路看不到 State 就只能问出通用话。
    """
    state = _base_state(facts=_no_activity_facts())
    ask, observe = _AskStub(), _ObserveStub(_GOOD_REPLY)
    result = rv.run_long_term_project_review(state, observe, ask_questions=ask)
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT
    assert result["asked"] is True and result["observed"] is False
    assert observe.count == 0, "请求补充这一档不该打观测"
    assert ask.count == 1, f"一次请求补充该问一次，实得 {ask.count} 次"
    assert "\n" in ask.calls[0], "问句旁路收到的不是一份多行材料（State 传丢了）"
    assert ask.calls[0] == rv.build_observation_payload(state), (
        "问句旁路收到的载荷与观测载荷不是同一份材料")
    assert result["questions"] == _MODEL_QUESTIONS, (
        f"旁路给的三条没原样带出来：{result['questions']}")
    assert "内置的通用引导问" not in result["reason"], (
        "明明拿到了模型生成的问题，reason 却说这三问是内置的")
    sources = {item["source"] or "" for item in ds.list_items(result["decision_id"])
               if item["kind"] == ds.KIND_UNKNOWN}
    assert any("问句旁路（模型现生成）" in s for s in sources), (
        f"模型生成的问题在账里和兜底问混成同一种出处，4a 回读时分不开：{sources}")
    assert rv.render_human_view(result)["等你补充的问题"] == _MODEL_QUESTIONS
    print("[OK] 问句旁路：三条原样落账并标明「模型现生成」，观测旁路零调用")


def test_empty_ask_bypass_falls_back_and_says_so():
    """问句旁路给回空数组 ⇒ 用内置兜底问，且 reason 如实标「这三问不是针对这份快照生成的」

    契约②在阶段一改成了「返回空列表」（原来那句是「不许编造」），所以「空」是一个**合法**
    返回值，本模块必须把它读成「这一轮拿不到问题」并兜底，而不是把空列表原样交给用户——
    界面上「本轮没有提问」配一个「请求补充」的出口是自我矛盾的。
    """
    state = _base_state(facts=_no_activity_facts())
    ask = _AskStub([])
    result = rv.run_long_term_project_review(state, _ObserveStub(_GOOD_REPLY),
                                             ask_questions=ask)
    assert ask.count == 1, "兜底之前必须真的试过旁路"
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT
    assert result["asked"] is True, (
        "asked 记的是「旁路被打过」，不是「拿到了问题」——合成一件事就分不清配额花没花")
    assert result["questions"] == list(rv._FIXED_QUESTIONS[rv.SHAPE_ASK_DIRECTION]), (
        f"旁路给空却没落到兜底问：{result['questions']}")
    assert "这三问是内置的通用引导问" in result["reason"], (
        f"用了兜底问却没在 reason 里说：{result['reason']}")
    assert "未调用问句旁路" not in " ".join(result["skipped"]), (
        f"旁路明明被打过，skipped 里却说没打：{result['skipped']}")
    human = rv.render_human_view(result)
    assert human["等你补充的问题"] == result["questions"], (
        "兜底问没进 Human View——空列表交到人手上就是「请求补充却没有问题」")
    sources = {item["source"] or "" for item in ds.list_items(result["decision_id"])
               if item["kind"] == ds.KIND_UNKNOWN}
    assert any("内置兜底问" in s for s in sources), f"兜底问的出处没标明：{sources}"
    print("[OK] 旁路给空：走内置兜底三问、如实标注，asked 仍记「打过了」")


def test_choose_goal_shape_calls_neither_bypass():
    """「先选目标」这一档两条旁路都不打：该问什么由数据本身写明，打模型只会把那串 id 抄一遍

    拍板 2 定的是「三问由模型生成」，而这一档**不在那三问里**——它只有一条固定问，
    reason 与 skipped 里都要如实写「没打模型」。模块头部那张配额口径表就是这条的依据，
    这一条把它变成可执行的断言。
    """
    state = _request_state(rv.SHAPE_CHOOSE_GOAL)
    ask, observe = _AskStub(), _ObserveStub(_GOOD_REPLY)
    result = rv.run_long_term_project_review(state, observe, ask_questions=ask)
    assert result["gate_shape"] == rv.SHAPE_CHOOSE_GOAL
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT
    assert ask.count == 0 and observe.count == 0, (
        f"这一档两条旁路都不该打，实得 问句 {ask.count}／观测 {observe.count}")
    assert result["asked"] is False
    assert result["questions"] == list(rv._FIXED_QUESTIONS[rv.SHAPE_CHOOSE_GOAL]), (
        f"这一档只有固定一问，实得 {result['questions']}")
    assert len(result["questions"]) == 1, (
        "目标都没定就问三条「怎么推进」是问在空气上——这一档只该问「复盘哪个」")
    assert "没打模型" in result["reason"], f"reason 没如实说没打模型：{result['reason']}"
    assert "未调用问句旁路" in result["skipped"] and "未调用观测" in result["skipped"]
    row = ds.get_decision(result["decision_id"])
    assert row["goal_id"] is None, (
        "goal_id 为空时不许替用户挑一个目标——那正是这一档要问的那件事")
    sources = {item["source"] or "" for item in ds.list_items(result["decision_id"])
               if item["kind"] == ds.KIND_UNKNOWN}
    assert any("固定一问" in s for s in sources), (
        f"固定一问的出处要写明「由 unknown 码写明，未打模型」，实得 {sources}")
    print("[OK] 先选目标：两条旁路零调用、固定一问、出处与 skipped 都如实")


def test_confidence_splits_suggestion_from_exploratory():
    """同一份材料、同一个观测回复，只差置信度：到线说成建议，线下说成探索性假设

    定案句「取消的不是『允许不知道』，是『无帮助的信息不足状态』」落到这一档就是
    「低置信度仍然产出」，只是措辞改口。两档都落 `decisions`、都是 proposed、**都不新增列**：
    置信度与「我还缺什么」写在 summary 那句话里（设计席：不碰 schema）。
    模型没报这一格时按最低档处理，并且话里要说明「没拿到可用的数」，不许填一个中性值。
    """
    before = _count_decisions()
    high = rv.run_long_term_project_review(_base_state(), _ObserveStub(_GOOD_REPLY))
    low = rv.run_long_term_project_review(_base_state(), _ObserveStub(_LOW_CONF_REPLY))
    assert high["gate_shape"] == rv.SHAPE_SUGGESTION and high["confidence"] == 0.8
    assert low["gate_shape"] == rv.SHAPE_EXPLORATORY and low["confidence"] == 0.2
    assert high["outcome"] == low["outcome"] == rv.OUTCOME_PROPOSED, (
        "两档都落账、都是 proposed——阶段一改的是措辞，不是出口四值")
    assert _count_decisions() == before + 2, "两档各落一行，谁也没多写"
    high_row = ds.get_decision(high["decision_id"])
    low_row = ds.get_decision(low["decision_id"])
    assert high_row["summary"] == _GOOD_REPLY[rv.OBSERVE_KEY_NEXT_STEP], (
        "说成建议时 summary 要保持观测那句逐字：既有断言与 4b 的经验回流都读这一格，"
        "「说成建议」反过来改掉账的内容是本批不想要的连带面")
    assert "试探" in low_row["summary"] and "我还缺" in low_row["summary"], (
        f"低置信度那一行没说清自己是试探：{low_row['summary']}")
    assert "20%" in low_row["summary"], (
        f"summary 里没带上模型自报的数：{low_row['summary']}")
    assert "items.test_run.is_full_run" in low_row["summary"], (
        "「我还缺」那一格没取这份快照自己登记的未知——那它就是一句姿态，不可追溯")
    assert high_row["summary"] != low_row["summary"], "两档的 summary 是同一句话"
    assert high_row["status"] == low_row["status"] == ds.STATUS_PROPOSED

    silent = dict(_GOOD_REPLY)
    silent[rv.OBSERVE_KEY_CONFIDENCE] = None
    no_number = rv.run_long_term_project_review(_base_state(), _ObserveStub(silent))
    assert no_number["gate_shape"] == rv.SHAPE_EXPLORATORY, (
        "模型没报置信度却被当成有把握：把缺席读成中性就是替它填了一个它没说过的数")
    assert no_number["confidence"] is None
    silent_row = ds.get_decision(no_number["decision_id"])
    assert "没拿到可用的数" in silent_row["summary"], (
        f"缺席那一格写成了具体的百分数：{silent_row['summary']}")
    assert "不替它填" in no_number["reason"], (
        f"reason 没说明这一格为什么是空的：{no_number['reason']}")
    print("[OK] 置信度分档：两档都落账、措辞不同，缺席按最低档且如实说明")


def test_negative_control_threshold_moves_the_tier():
    """负控：分档线只在 `CONFIDENCE_SUGGESTION_MIN` 这一处——挪它，档位必须跟着挪

    设计席判据原文要求「阈值写成具名常量」。如果实现把 0.5 硬编码在 if 里，改这个常量之后
    0.8 仍被判成建议，上面那条照样绿——「具名常量」就成了装饰。所以这里在**内存里**替换
    模块属性、finally 还原（本批定案之一：不靠改仓库文件演示负控），并在还原后再跑一次
    确认档位回来，证明中间那次的红是分档逻辑而不是桩泄漏。
    """
    original = rv.CONFIDENCE_SUGGESTION_MIN
    state = _base_state()
    try:
        assert rv.run_long_term_project_review(
            state, _ObserveStub(_GOOD_REPLY))["gate_shape"] == rv.SHAPE_SUGGESTION
        rv.CONFIDENCE_SUGGESTION_MIN = 0.9
        moved = rv.run_long_term_project_review(state, _ObserveStub(_GOOD_REPLY))
        assert moved["gate_shape"] == rv.SHAPE_EXPLORATORY, (
            "把线抬到 0.9 之后 0.8 仍被判成建议：分档读的不是这个常量")
        assert "90%" in moved["reason"], (
            f"reason 里那句线值没跟着常量走，还写着旧的：{moved['reason']}")
    finally:
        rv.CONFIDENCE_SUGGESTION_MIN = original
    restored = rv.run_long_term_project_review(state, _ObserveStub(_GOOD_REPLY))
    assert restored["gate_shape"] == rv.SHAPE_SUGGESTION, (
        "还原常量之后档位没跟着回来——上一条的红是桩泄漏，不是分档逻辑")
    assert rv.CONFIDENCE_SUGGESTION_MIN == original
    print("[OK] 负控：分档线常量挪得动（0.9 时 0.8 成试探），还原后重新回到建议档")


def test_historical_snapshot_without_markers_is_read_honestly():
    """条目里没有 evidence 这一格（标记引入之前存的历史快照）⇒ 按「没有实质依据」处理，
    而 reason 必须明说这**不是**「读到了零」

    `.get("evidence")` 是 None 有两种真实来路：历史行缺这一格，或采集器读到零并标了
    activity_empty。两者在闸门②上都不放行，但对人说的话完全不一样——前者说明系统当时
    还没有这套标记（用户没做错任何事），后者说明项目确实没动过。混成一句就会让人以为
    自己的项目在躺着，而那只是他手上是一份升级前的快照。
    """
    historical = _state(
        [_fact("items.notes.total", "9", "table:notes[total=9]", evidence=None),
         _fact("items.test_run.latest", "run_id=114", "table:test_runs.id", evidence=None)],
        [_manual("summary", "user_report", "这周的产出我没看", "用户 2026-10-11 口述")],
        [_unknown_record("items.goal_tasks.total", "读不出来")])
    assert all("evidence" not in item for item in historical["items"]
               if item["kind"] == "fact"), "夹具没造出「缺这一格」的形状"
    assert rv.has_substantive_evidence(historical) is False, (
        "没有标记却被判成有实质依据：闸门②把 None 读成了 activity")
    result = rv.run_long_term_project_review(historical, _ObserveStub(_GOOD_REPLY))
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT
    assert "没有证据标记" in result["reason"], (
        f"历史快照没被如实标出来：{result['reason']}")
    assert "而不是当作读到了零" in result["reason"], (
        f"「缺这一格」与「读到零」又被并成一句：{result['reason']}")
    # 对照组：同样是不放行，但标记齐全（读到零）时不许出现那句历史说明
    marked = rv.run_long_term_project_review(
        _base_state(facts=_no_activity_facts()), _ObserveStub(_GOOD_REPLY))
    assert "没有证据标记" not in marked["reason"], (
        "读到零的那一份也被说成「没有标记」——两种形状的话又混了")
    print("[OK] 历史快照：不放行，且 reason 分清「缺标记」与「读到零」")


# ---------- 判据②：本周无需决策 ----------

def test_no_change_needed_creates_no_decision_row():
    """观测判不了 ⇒ 输出一句「当前没有发现值得改变的地方」，且**不生成 Decision 行**"""
    before = _count_decisions()
    stub = _ObserveStub(_UNABLE)
    result = rv.run_long_term_project_review(_base_state(), stub)
    assert result["outcome"] == rv.OUTCOME_NO_CHANGE, f"出口不对：{result['outcome']}"
    assert stub.count == 1, "判不了也要真的观测过一次，否则这条测的是闸门"
    assert result["decision_id"] is None, "无异常 State 却立了决策行"
    assert _count_decisions() == before, (
        f"decisions 行数变了：{before} → {_count_decisions()}")
    human = rv.render_human_view(result)
    assert human["这一轮的结论"] == "当前没有发现值得改变的地方", f"人话没给对：{human}"
    assert "未立 Decision" in human["本轮明确没做的事"]
    print("[OK] 判不了＝本周无需决策：零 Decision 行")


def test_negative_control_row_counter_has_teeth():
    """上一条用的那把尺必须能量出 +1，否则「行数没变」是恒真断言

    这条不验被测代码，验的是**尺**：真写一行 decisions、真加一条 decision_item，看两个
    计数函数抓不抓得到。负控在这里不需要「摘掉某个守卫」，因为要否证的不是实现里的分支，
    而是测试自己的读数能力（§五.50 那条边界的另一侧）。
    """
    before_decisions = _count_decisions()
    decision_id = ds.create_decision(title="负控：这条必须让计数变大")
    assert _count_decisions() == before_decisions + 1, (
        f"决策计数抓不到新增行：{before_decisions} → {_count_decisions()}")
    assert ds.get_decision(decision_id) is not None, "写完立刻读不到，临时库不可信"

    before_items = _count_items(decision_id)
    ds.add_item(decision_id, ds.KIND_FACT, "负控条目", source="table:decisions.id")
    assert _count_items(decision_id) == before_items + 1, (
        f"条目计数抓不到新增行：{before_items} → {_count_items(decision_id)}"
        "——那「条目数对得上」就是假的")
    print(f"[OK] 负控：真写一行决策＋一条条目，两把尺都量出 +1"
          f"（{before_decisions}→{before_decisions + 1}）")


# ---------- 判据：正常出口写账 ----------

def test_proposed_writes_ledger_with_typed_items():
    """观测可用 ⇒ 一行 proposed ＋ 按类抄来的条目；fact 条目必须带着 source"""
    before = _count_decisions()
    state = _base_state()
    stub = _ObserveStub(_GOOD_REPLY)
    result = rv.run_long_term_project_review(state, stub)
    assert result["outcome"] == rv.OUTCOME_PROPOSED
    assert _count_decisions() == before + 1, "只该写一行账"
    decision_id = result["decision_id"]
    row = ds.get_decision(decision_id)
    assert row["status"] == ds.STATUS_PROPOSED, "复盘只产出候选，批准在人那边"
    assert row["title"] == _GOOD_REPLY[rv.OBSERVE_KEY_PROBLEM], "决策标题没取观测的问题"
    assert row["summary"] == _GOOD_REPLY[rv.OBSERVE_KEY_NEXT_STEP]
    assert row["goal_id"] == state["goal_id"]

    items = ds.list_items(decision_id)
    kinds: Dict[str, int] = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    assert kinds.get(ds.KIND_FACT, 0) == 3, f"自动段三条事实没照抄：{kinds}"
    assert kinds.get(ds.KIND_USER_REPORT, 0) == 1, f"人工段没进账：{kinds}"
    assert kinds.get(ds.KIND_INFERENCE, 0) == 1, f"观测结论没登记成 inference：{kinds}"
    assert kinds.get(ds.KIND_ASSUMPTION, 0) == 2, (
        f"两条能力键应是两条 assumption（3b 按条计，好让 4b 数出「要几条能力」）：{kinds}")
    assert kinds.get(ds.KIND_UNKNOWN, 0) == 1, f"未知没照抄：{kinds}"
    assert len(result["item_ids"]) == len(items), "返回的 item_ids 与库里不一致"

    # Phase 2 判据①在这条写路径上的延伸：抄下来的 fact 不许丢掉出处
    for item in items:
        if item["kind"] == ds.KIND_FACT:
            assert (item["source"] or "").strip(), (
                f"无 source 的 fact 条目：{item}")
    print(f"[OK] 正常出口：一行 proposed ＋ {len(items)} 条分类条目，fact 全部带出处")


# ---------- 判据①：两个 View 按内容分离 ----------

def _string_leaves(obj: Any) -> List[str]:
    """递归取出结构里所有非空字符串（比对按内容，不按字段名，所以只能这么取）"""
    found: List[str] = []
    if isinstance(obj, str):
        if obj.strip():
            found.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            found.extend(_string_leaves(value))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found.extend(_string_leaves(value))
    return found


def _leaked_sentences(human_view: Any, execution_view: Any) -> List[str]:
    """Human View 里有哪些句子出现在了 Execution View 的文本里（判据①的执行形式）"""
    execution_text = json.dumps(execution_view, ensure_ascii=False, default=str)
    return [sentence for sentence in _string_leaves(human_view)
            if sentence in execution_text]


def test_views_are_separated_by_content():
    """七个形状逐个验：给人看的句子一句都不许出现在给程序的视图里

    阶段一之前这条是「四个出口逐个验」；现在同一个出口**里面**还有形状分档，所以清单按
    能命中的形状配齐：proposed 两档（建议／探索性假设）＋请求补充三档（问方向／
    建上下文／先选目标）＋两个不属于分流的出口（缺人工段、判不了）。少配一档，就留一个
    没有视图证据的形状——而 Human View 那两格新文案（「这一轮的说法」「等你补充的问题」）
    恰恰只在那几档里出现。

    第二道是**覆盖性**的：断言这一批真的把四个出口与五个形状各命中至少一次。没有它，
    「七个都过」可能只是七个都走了同一条路（夹具被改坏时会静默退化成那样）。
    """
    results = [
        rv.run_long_term_project_review(_base_state(manual=[]), _ObserveStub(_GOOD_REPLY)),
        rv.run_long_term_project_review(_request_state(rv.SHAPE_ASK_DIRECTION),
                                        _ObserveStub(_GOOD_REPLY), _AskStub()),
        rv.run_long_term_project_review(_request_state(rv.SHAPE_BUILD_CONTEXT),
                                        _ObserveStub(_GOOD_REPLY), _AskStub()),
        rv.run_long_term_project_review(_request_state(rv.SHAPE_CHOOSE_GOAL),
                                        _ObserveStub(_GOOD_REPLY), _AskStub()),
        rv.run_long_term_project_review(_base_state(), _ObserveStub(_UNABLE)),
        rv.run_long_term_project_review(_base_state(), _ObserveStub(_GOOD_REPLY)),
        rv.run_long_term_project_review(_base_state(), _ObserveStub(_LOW_CONF_REPLY)),
    ]
    assert {r["outcome"] for r in results} == set(rv.REVIEW_OUTCOMES), (
        f"这一批没把四个出口各命中一次：{sorted({r['outcome'] for r in results})}")
    assert {r["gate_shape"] for r in results} == set(rv.GATE_SHAPES) | {None}, (
        f"这一批没把五个形状各命中一次（None＝不在分流里的那两个出口）："
        f"{sorted(str(r['gate_shape']) for r in results)}")
    for result in results:
        human = rv.render_human_view(result)
        execution = rv.to_execution_view(result)
        leaked = _leaked_sentences(human, execution)
        assert not leaked, (
            f"出口 {result['outcome']}／形状 {result['gate_shape']} 的两个视图没分开，"
            f"泄漏的句子：{leaked}")
        # 反向也要成立：视图不是空的（空视图同样能「通过」内容比对，那是假的分离）
        assert _string_leaves(human), f"出口 {result['outcome']} 的 Human View 是空的"
        assert execution["outcome"] == result["outcome"]
    print("[OK] 四出口×五形状的两个视图都按内容分开，且都不是空视图")


def test_execution_view_carries_no_natural_language():
    """程序那一侧只该有标识、枚举与计数——连中文标签都不许有

    阶段一新增的三格（`gate_shape`／`confidence`／`questions`）**刻意不进**这一份：
    拍板 4 要的是「结果里有形状字段」，而今天没有任何程序按形状分流（没有消费者就不留
    字段，本项目在 3c-甲 就按这条删过 `previous_run_at`）。所以这里既验「ASCII」，
    也验「这三格没被顺手加进程序视图」。
    """
    for reply in (_GOOD_REPLY, _LOW_CONF_REPLY):
        result = rv.run_long_term_project_review(_base_state(), _ObserveStub(reply))
        execution = rv.to_execution_view(result)
        for sentence in _string_leaves(execution):
            assert sentence.isascii(), (
                f"Execution View 里出现非 ASCII 文本（那是给人读的句子）：{sentence!r}")
        for key in ("payload", "reason", "skipped",
                    "gate_shape", "confidence", "questions"):
            assert key not in execution, (
                f"{key} 是给人看／等人答的字段，不该进程序视图：{execution}")
        assert result["gate_shape"] in (rv.SHAPE_SUGGESTION, rv.SHAPE_EXPLORATORY), (
            "两档里有一档没被这一批覆盖到")
    # 请求补充那一档的程序视图同样不许夹带问句（三条中文问句是最容易被漏出去的）
    requested = rv.run_long_term_project_review(
        _request_state(rv.SHAPE_ASK_DIRECTION), _ObserveStub(_GOOD_REPLY), _AskStub())
    requested_execution = rv.to_execution_view(requested)
    assert "questions" not in requested_execution
    for sentence in _string_leaves(requested_execution):
        assert sentence.isascii(), (
            f"请求补充那一档把中文问句带进了程序视图：{sentence!r}")
    assert requested["questions"], "夹具这一档本该有三条问句"
    assert rv.render_human_view(requested)["等你补充的问题"] == requested["questions"], (
        "问句只在结果里、没进 Human View——那用户看不到被问了什么")
    execution = rv.to_execution_view(
        rv.run_long_term_project_review(_base_state(), _ObserveStub(_GOOD_REPLY)))
    assert execution["capability_keys"] == ["read_write_notes", "recall_local_material"], (
        "能力要的是闭集键名，不是中文标签")
    print("[OK] 程序视图全 ASCII、阶段一新增三格都不在里面，问句只进 Human View")


def test_negative_control_leaky_execution_view_is_caught():
    """负控：故意做一个夹带人话的执行视图，同一份比对必须抓到"""
    result = rv.run_long_term_project_review(_base_state(), _ObserveStub(_GOOD_REPLY))
    human = rv.render_human_view(result)
    leaky = rv.to_execution_view(result)
    leaky["what_to_do"] = human["为什么要说这件事"]
    leaked = _leaked_sentences(human, leaky)
    assert leaked, "夹带了人话的执行视图竟然通过了比对——判据①是恒真的"
    print(f"[OK] 负控：漏一句人话就被抓到（{len(leaked)} 处）")


# ---------- 判据④⑤：模板动作类型 ----------

# 改前基线快照：由 git worktree add 出来的**改前代码**（HEAD f3051b9，VALID_ACTIONS 还是
# 两个成员）跑同一份取证脚本抓下来，日期与自增 id 已归一化。抓取脚本随 worktree 一起删，
# 归一化规则在本文件里重做一次（两边必须一致，不一致就是基线过期）。
_PRE_3B_TEMPLATE_SNAPSHOT: Dict[str, Any] = {
    "scout_digest": {
        "plan_len": 1,
        "plan_tasks": [{
            "auto_executable": True,
            "description": "只读本地机会库，按「临期 / 无时点长期挂着」两段生成筛选报告（不联网）",
            "digest_params": {"limit": 3},
            "estimated_hours": 0.1,
            "goal_id": "template_N",
            "goal_title": "任务模板",
            "previous_run_at": "YYYY-MM-DDT08:00:00",
            "reason": "模板：probe_scout_digest",
            "task_kind": "scout_digest",
            "task_title": "资源筛选报告（YYYY-MM-DD）",
        }],
        "query": "",
        "stored_row": {
            "action_params": "{\"limit\": 3}",
            "action_type": "scout_digest",
            "auto_approve": 0,
            "description": "",
            "enabled": 1,
            "name": "probe_scout_digest",
            "trigger_config": "{}",
            "trigger_type": "manual",
        },
    },
    "search_and_note": {
        "plan_len": 2,
        "plan_tasks": [{
            "auto_executable": True,
            "description": "模板自动任务：搜索关键词「反向传播」的相关资料",
            "estimated_hours": 0.5,
            "goal_id": "template_N",
            "goal_title": "任务模板",
            "reason": "模板：probe_search_and_note",
            "task_title": "搜索「反向传播」相关资料",
        }, {
            "auto_executable": True,
            "description": "模板自动任务：整理关键词「反向传播」的学习笔记",
            "estimated_hours": 0.5,
            "goal_id": "template_N",
            "goal_title": "任务模板",
            "reason": "模板：probe_search_and_note",
            "task_title": "整理「反向传播」学习笔记",
        }],
        "query": "反向传播",
        "stored_row": {
            "action_params": "{\"query\": \"反向传播\"}",
            "action_type": "search_and_note",
            "auto_approve": 0,
            "description": "",
            "enabled": 1,
            "name": "probe_search_and_note",
            "trigger_config": "{}",
            "trigger_type": "manual",
        },
    },
}


def _snapshot_diff(actual: Any, baseline: Any, path: str = "") -> Dict[str, Any]:
    """递归逐键比对，返回差异种类（多键／少键／改值），不是一句「不相等」

    与 tests/test_supervisor.py 里那份是同一思路的两份实现（都是十来行、都只服务于
    自己那份基线）。第三份出现时就收进 tests/ 的公共 helper，不在测试文件里继续复制。
    """
    if isinstance(baseline, dict) and isinstance(actual, dict):
        diff: Dict[str, Any] = {}
        for key in sorted(set(actual) | set(baseline)):
            here = f"{path}.{key}" if path else str(key)
            if key not in actual:
                diff[here] = {"missing": baseline[key]}
            elif key not in baseline:
                diff[here] = {"unexpected": actual[key]}
            else:
                nested = _snapshot_diff(actual[key], baseline[key], here)
                if nested:
                    diff.update(nested)
        return diff
    if isinstance(baseline, list) and isinstance(actual, list):
        if len(actual) != len(baseline):
            return {path or "list": {"was_len": len(baseline), "now_len": len(actual)}}
        merged: Dict[str, Any] = {}
        for index, (a_item, b_item) in enumerate(zip(actual, baseline)):
            nested = _snapshot_diff(a_item, b_item, f"{path}[{index}]")
            if nested:
                merged.update(nested)
        return merged
    return {} if actual == baseline else {path or "value": {
        "was": baseline, "now": actual}}


def _normalize_probe_text(text: str) -> str:
    """与取证脚本同一套归一化：自增 id 与当天日期不进基线"""
    text = re.sub(r"template_\d+", "template_N", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "YYYY-MM-DD", text)
    return re.sub(r"\"id\": \d+", '"id": N', text)


def _capture_template_behavior(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """跑一遍建模板＋读回＋构计划，摊成可比的快照（与改前脚本逐字段同形）"""
    template_id = tt.create_template(
        name="probe_" + action, trigger_type="manual",
        action_type=action, action_params=params, enabled=1)
    template = tt.get_template(template_id)
    row = template.to_dict()
    for key in ("id", "created_at", "updated_at", "last_run_at"):
        row.pop(key, None)
    plan = TaskExecutor._build_plan_from_template(
        template, previous_run_at="2026-09-05T08:00:00")
    tasks = []
    for task in plan:
        item = dict(task)
        item.pop("task_id", None)
        tasks.append(item)
    raw = json.dumps({"stored_row": row, "query": template.query,
                      "plan_len": len(plan), "plan_tasks": tasks},
                     ensure_ascii=False, sort_keys=True)
    return json.loads(_normalize_probe_text(raw))


def test_existing_two_actions_unchanged_by_snapshot():
    """判据⑤：增员之后，既有两种动作的行为逐键与改前基线一致（快照比对，非 assert ==）"""
    current = {
        "search_and_note": _capture_template_behavior(
            tt.ACTION_SEARCH_AND_NOTE, {"query": "反向传播"}),
        "scout_digest": _capture_template_behavior(
            tt.ACTION_SCOUT_DIGEST, {"limit": 3}),
    }
    diff = _snapshot_diff(current, _PRE_3B_TEMPLATE_SNAPSHOT)
    assert not diff, f"既有动作的行为与改前基线不一致：{diff}"
    print("[OK] 既有两种动作逐键与 worktree 抓的改前基线一致")


def test_snapshot_comparator_catches_a_changed_value():
    """比对器自身的牙齿：改一个值、多一个键、少一个键都必须被抓到"""
    baseline = {"a": 1, "b": {"c": 2}, "d": 3}
    assert _snapshot_diff({"a": 9, "b": {"c": 2}, "d": 3}, baseline) == {
        "a": {"was": 1, "now": 9}}
    assert "unexpected" in _snapshot_diff({"a": 1, "b": {"c": 2}, "d": 3, "x": 0}, baseline)["x"]
    assert "missing" in _snapshot_diff({"a": 1, "d": 3}, baseline)["b"]
    assert _snapshot_diff(baseline, baseline) == {}, "一致时比对器必须不红"
    print("[OK] 快照比对器三种破法都能抓到，一致时不红")


def test_valid_actions_superset_after_change():
    """增员只该是"加一个"：旧的两个还在，长名不在表里（修订 5 定的是短名）"""
    assert tt.ACTION_SEARCH_AND_NOTE in tt.VALID_ACTIONS
    assert tt.ACTION_SCOUT_DIGEST in tt.VALID_ACTIONS
    assert tt.ACTION_LONG_TERM_REVIEW == "long_term_review", (
        "动作名是修订 5 定案的 long_term_review，不是模块名")
    assert tt.ACTION_LONG_TERM_REVIEW in tt.VALID_ACTIONS
    assert "long_term_project_review" not in tt.VALID_ACTIONS, (
        "把模块名当动作名写进表里了")
    print("[OK] VALID_ACTIONS 是旧集合的超集，新动作名是 long_term_review")


def test_create_template_accepts_long_term_review():
    """判据④前半：这种模板能建出来，且动作类型不被改写"""
    template_id = tt.create_template(
        name="复盘模板", trigger_type="weekly",
        action_type=tt.ACTION_LONG_TERM_REVIEW, action_params={})
    assert template_id, "create_template 没返回 id"
    template = tt.get_template(template_id)
    assert template.action_type == tt.ACTION_LONG_TERM_REVIEW, (
        f"动作类型被改写成 {template.action_type}")
    assert template.auto_approve == 0, "auto_approve 必须保持 0（全程定案）"
    print("[OK] long_term_review 模板建得出来、动作名不被改写")


def test_unknown_action_type_is_rejected():
    """判据④后半（3c-乙 按设计席裁定就地反转）：非法动作名要**报错，并且一行都不建**

    这条在 3b 时断言的是「静默降级成 search_and_note 且照常建行」，钉的是当时的现状。
    设计席裁定目标行为是拒绝、改的时机是 3c；3c-乙 把动作类型做成了表单里的一格，
    这条路径从「只有脚本能走」变成「用户可达」，于是降级的代价变成真的：用户选的是
    复盘、库里存的是搜索笔记，而界面上看不出任何异常——那是假账。

    ⚠️ 断言的是**两件事**，缺一件都不算拒：① raise ValueError；② 模板条数没增加。
    只 raise 却先落了行，或反过来不 raise 也不建行，都过不了这条。
    """
    before = len(tt.list_templates())
    raised = None
    try:
        tt.create_template(name="非法动作模板", trigger_type="manual",
                           action_type="totally_not_an_action", action_params={})
    except ValueError as e:
        raised = str(e)
    assert raised is not None, (
        "非法 action_type 没被拒：3c-乙 定的「既不建行也不改行」退回了静默降级")
    assert "非法值" in raised and "totally_not_an_action" in raised, (
        f"报错消息没把那个非法值说出来：{raised}")
    after = len(tt.list_templates())
    assert after == before, f"报了错还是落了行（{before} → {after}）"
    print("[OK] 非法动作名被拒：raise ValueError 且模板条数一条没多")


def test_update_template_rejects_invalid_action_without_touching_row():
    """update_template 给非法动作名：raise，且那一行**一个字段都没动**

    为什么单独立一条而不并入上一条：`update_template` 是先拼字段列表再执行 UPDATE 的，
    所以「拒绝」有两种失败形状——校验排在拼 SQL **之后**会留下半截更新（名字改了、动作没改），
    校验排在**之前**才是整行不动。用户看到一条名字与动作对不上的模板，比看到一次报错更糟，
    因为前者他不知道。这条钉的就是校验的位置。
    """
    template_id = tt.create_template(
        name="原名模板", trigger_type="weekly",
        trigger_config={"weekday": 3, "time": "08:00"},
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={"limit": 3})
    assert template_id, "前置模板建不出来"
    try:
        before = tt.get_template(template_id)
        assert before is not None
        raised = None
        try:
            tt.update_template(template_id, name="改名应当一起被拒",
                               action_type="totally_not_an_action")
        except ValueError as e:
            raised = str(e)
        assert raised is not None, "update_template 没拒非法动作名"
        assert "update_template" in raised, f"报错消息没说是哪个函数：{raised}"
        after = tt.get_template(template_id)
        assert after.name == before.name, (
            "名字被改掉了——校验发生在拼 SQL 之前这一条没做到，留下半截更新")
        assert after.action_type == before.action_type == tt.ACTION_SCOUT_DIGEST, (
            f"动作被改了：{after.action_type}")
        assert after.action_params == before.action_params, (
            "动作参数也被顺手重写了")
        assert after == before, (
            f"整行没有逐字段不变：{before.to_dict()} → {after.to_dict()}")
        # 合法的动作名仍然要能改（否则上面那条「拒」就退化成「update_template 一直接受不了
        # action_type」，那是另一种坏）
        assert tt.update_template(template_id,
                                  action_type=tt.ACTION_LONG_TERM_REVIEW)
        assert tt.get_template(template_id).action_type == tt.ACTION_LONG_TERM_REVIEW
    finally:
        tt.delete_template(template_id)
    print("[OK] update_template 拒非法动作名：整行未变，且合法动作名照常能改")


# ---------- Phase 3c-甲：执行分支接线（模板触发 → State → 观测 → 账本） ----------

def _review_task(template_id: int) -> Dict[str, Any]:
    """经真分派点拿一条复盘任务（不手搓 dict：形状必须由 _build_plan_from_template 给）"""
    template = tt.get_template(template_id)
    plan = TaskExecutor._build_plan_from_template(template, previous_run_at=None)
    assert len(plan) == 1, f"复盘格只该产 1 条任务，实际 {len(plan)} 条：{plan}"
    return plan[0]


def _make_review_template(name: str) -> int:
    """建一条 long_term_review 模板（动作参数给空：这一格既不读关键词也不读筛选条件）"""
    template_id = tt.create_template(
        name=name, trigger_type="weekly",
        action_type=tt.ACTION_LONG_TERM_REVIEW, action_params={})
    assert template_id, f"复盘模板建不出来：{name}"
    assert tt.get_template(template_id).auto_approve == 0, (
        "auto_approve 全程保持 0 是既有定案，接线不该把它翻过来")
    return template_id


def test_review_template_builds_one_review_task():
    """3c-甲 把 3b 那条「立动作 ≠ 能执行」就地反转：现在必须排出 1 条带 task_kind 的任务

    这条在 3b 时断言的是 `plan == []`（钉住「能建不能跑」那个中间态）。接线之后它必须
    反过来——留着旧断言，就等于文档里那句「接线已完成」没有测试撑着。
    """
    template_id = _make_review_template("复盘模板接线")
    task = _review_task(template_id)
    assert task["task_kind"] == te.TASK_KIND_LONG_TERM_REVIEW, (
        f"没带复盘的 task_kind，execute_plan 会拿标题去猜类型：{task.get('task_kind')}")
    assert task["auto_executable"] is True, (
        "不带这个标记的任务根本不会进执行循环，接了等于没接")
    assert task["goal_id"] == tt.template_goal_id(template_id), (
        "归档目录仍该挂模板自己的虚拟目标，与另外两格同一口径")
    assert "previous_run_at" not in task, (
        "这一格的四个出口没有一个读缺席页眉，任务里不该留一个没人消费的字段"
        "（3c-甲 定案，不是漏传）")
    print("[OK] 复盘模板排出 1 条任务：task_kind 显式、无 previous_run_at")


def test_execute_template_review_writes_decision_without_approval():
    """端到端（设计席给 3c-甲 的验收）：手动触发 → 账本里有 Decision 且出口正确 → 不产生待审批

    全程离线：两个旁路都换成桩。patch 的是 `te.supervisor_module.supervisor_observe` 与
    `...supervisor_ask_questions` 这两个**模块属性**——handler 在调用时才取属性
    （两个函数名直接作为实参传进复盘模块），所以桩一定生效；这也顺带证明了接线**没有**用
    lambda 包一层（包了就得 patch 那个 lambda 的闭包，这条 patch 会静默失效、然后真打网络）。
    阶段一起 handler 传的是**两个**名字，所以这里也必须 patch 两个：少 patch 一个，
    本文件那句「不打网络」就成了假话（这一条由下面的注入用例正面钉住）。
    """
    stub = _ObserveStub(_GOOD_REPLY)
    ask = _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    template_id = None
    try:
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        # State 走真落盘再读回来：接线读的是 latest_project_state()，夹具必须经
        # 同一条读路径回来，否则「键名对不上」这类缺陷会被手搓 dict 掩盖。
        ps.save_project_state(_base_state())
        template_id = _make_review_template("复盘端到端")
        before = _count_decisions()
        events = list(TaskExecutor().execute_template(
            template_id, triggered_by="manual"))
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask

    approvals = [e for e in events if e.get("type") == "approval"]
    assert approvals == [], (
        f"复盘这一格不该有审批暂停点，却收到 {len(approvals)} 个 approval 事件")
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1, f"生成器必须自己跑到 done：{[e.get('type') for e in events]}"
    assert stub.count == 1, (
        f"一次触发该调一次观测，实际 {stub.count} 次（0 次＝接线没真跑，2 次＝重复调用）")
    assert "\n" in stub.calls[0], (
        "观测收到的载荷不是一段多行材料——接线把 State 传丢了")
    assert ask.count == 0, (
        f"这一端到端走的是「有实质依据⇒观测」那一档，问句旁路一次都不该被打："
        f"实得 {ask.count} 次")

    stats = done[0].get("stats") or {}
    assert stats.get("auto") == 1 and stats.get("saved") == 1, (
        f"复盘落了一行账，计数该记在 saved 上：{stats}")
    assert stats.get("failed") == 0 and stats.get("rejected") == 0, f"计数串了：{stats}"

    messages = "｜".join(str(e.get("message") or "") for e in events)
    assert _count_decisions() == before + 1, (
        "端到端跑完却没有多出一行 Decision——账本这一头没接上")
    matched = re.search(r"已写入 Decision #(\d+)", messages)
    assert matched, f"播报文案没有把决策编号说出来（用户无从回读）：{messages}"
    decision = ds.get_decision(int(matched.group(1)))
    assert decision is not None, "播报里的编号在账本里读不到"
    assert decision["status"] == ds.STATUS_PROPOSED, (
        f"出口不对：{decision['status']}")
    assert decision["decision_scope"] == ds.SCOPE_OPERATIONAL, (
        f"复盘产出的决策该是 operational（Fix A 之后它是跨度词，3b 定案）："
        f"{decision['decision_scope']}")
    # 常量级的第二道：复盘默认档必须落在「能回流成经验」那个集合里。
    # 这条不是重复上一条——上一条管「这一行走对了」，这一条管「常量没被调到集合外」：
    # 有人把 REVIEW_DECISION_SCOPE 改成 tactical，端到端那条照样绿（写路径不校验集合），
    # 但 4a／4b 的经验回流就再也收不到复盘产物，而且**不会有任何地方报错**。
    assert rv.REVIEW_DECISION_SCOPE in ds.EXPERIENCE_ELIGIBLE_SCOPES, (
        f"复盘默认档 {rv.REVIEW_DECISION_SCOPE!r} 不在经验回流集合里，"
        "复盘产物永远不会回流成经验")
    assert tt.pending_approval_count(template_id) == 0, (
        "复盘不许往待审批里塞东西：它的产物是账本行，不是待确认的文档")
    assert tt.get_template(template_id).last_run_at, (
        "跑完没记 last_run_at，下一期的到期判断会把它当「从没跑过」")
    print(f"[OK] 端到端：触发 → Decision #{matched.group(1)}"
          f"（proposed/{ds.SCOPE_OPERATIONAL}），零待审批")


def test_review_handler_injects_the_ask_bypass():
    """阶段一的接线半边：handler 把**问句旁路**也递进去了，而且递的是模块属性名

    为什么单独一条而不并入端到端：端到端走的是「有实质依据」那一档，那里问句旁路**本来
    就不该被打**——它只能证明「没被打爆」，证不了「接上了」。这一条把 State 换成没有活动证据
    的那一份，于是这一档必须打问句旁路：桩计数为 1、观测计数为 0、`questions` 是桩给的那
    三条。少 patch 一个名字就跑不出这一条的形状（真旁路会打网络），这正是设计席要的
    「两个名字都必须 patch」那句的可执行形式。
    """
    task = _review_task(_make_review_template("问句旁路注入"))
    observe, ask = _ObserveStub(_GOOD_REPLY), _AskStub()
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    original_latest = te.state_module.latest_project_state
    before = _count_decisions()
    try:
        te.supervisor_module.supervisor_observe = observe
        te.supervisor_module.supervisor_ask_questions = ask
        te.state_module.latest_project_state = (
            lambda goal_id=None: _request_state(rv.SHAPE_ASK_DIRECTION))
        result = TaskExecutor().handle_long_term_review_task(task)
    finally:
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask
        te.state_module.latest_project_state = original_latest

    assert result["ok"] is True, f"handler 把异常吃下去了：{result['message']}"
    assert result["outcome"] == rv.OUTCOME_INSUFFICIENT, (
        f"没有活动证据的 State 在该档请求补充：{result['outcome']}")
    assert ask.count == 1, (
        f"问句旁路没被接线递进去（实得 {ask.count} 次）——"
        "这一档会退回内置兜底问，界面上看着正常，实际模型一次都没参与")
    assert observe.count == 0, f"请求补充这一档不该打观测，实得 {observe.count} 次"
    assert result["decision_id"] is not None and _count_decisions() == before + 1
    row_items = ds.list_items(result["decision_id"])
    contents = [item["content"] for item in row_items if item["kind"] == ds.KIND_UNKNOWN]
    sources = {item["source"] or "" for item in row_items
               if item["kind"] == ds.KIND_UNKNOWN}
    assert any("问句旁路（模型现生成）" in s for s in sources), (
        f"经接线生成的问题在账里没标明来源：{sources}")
    for question in _MODEL_QUESTIONS:
        assert any(question in c for c in contents), (
            f"桩生成的那一条没原样进账：{question}｜实得 {contents}")
    assert "请先在人工段写下你自己的判断" not in result["reason"], (
        "这份 State 是有人工段的，却报了闸门①那句——handler 读到的不是注入的那一份")
    assert "复盘失败" not in result["message"], f"handler 里抛了异常：{result['message']}"
    print("[OK] 接线注入：问句旁路被打一次、观测零次，问题带着「模型现生成」的出处落账")


def test_review_handler_falls_back_when_no_snapshot_exists():
    """project_states 空表 ⇒ 现采一份只含自动段的，让闸门①出声，不写库

    「空表」的读法已复跑确认过：`latest_project_state()` 走的是
    `ORDER BY id DESC LIMIT 1` 后 `fetchone()`，无行时返回 None 而**不是抛**——
    所以这里 patch 它返回 None 与「表真的是空的」在代码路径上等价（生产库今天就是
    0 行，3c-乙 的启动回填要正面处理这个 None）。

    阶段一给这一条加了**第二层**用途：回退去现采的那一份用的是 `_base_state(manual=[])`，
    它的事实条目带着活动证据标记，也就是说**闸门②对它是放行的**。所以「出口必须是
    缺人工段」＋「两条旁路都必须零调用」这两句一起，钉的是闸门顺序——把闸门②挪到①前面
    就会从这里调观测／问句，而设计席定的是「不动闸门①」。
    """
    task = _review_task(_make_review_template("空表回退"))
    stub, ask = _ObserveStub(_GOOD_REPLY), _AskStub()
    calls = {"build": 0}
    original_latest = te.state_module.latest_project_state
    original_build = te.state_module.build_project_state
    original_observe = te.supervisor_module.supervisor_observe
    original_ask = te.supervisor_module.supervisor_ask_questions
    before = _count_decisions()
    try:
        te.state_module.latest_project_state = lambda goal_id=None: None
        # 只含自动段那一份：人工段为空，正好落在闸门①上
        te.state_module.build_project_state = lambda **kw: (
            calls.__setitem__("build", calls["build"] + 1)
            or _base_state(manual=[]))
        te.supervisor_module.supervisor_observe = stub
        te.supervisor_module.supervisor_ask_questions = ask
        result = TaskExecutor().handle_long_term_review_task(task)
    finally:
        te.state_module.latest_project_state = original_latest
        te.state_module.build_project_state = original_build
        te.supervisor_module.supervisor_observe = original_observe
        te.supervisor_module.supervisor_ask_questions = original_ask

    assert calls["build"] == 1, (
        "没快照时没有回退去现采（要么直接崩、要么拿着 None 往下走）")
    assert rv.has_substantive_evidence(_base_state(manual=[])) is True, (
        "夹具变了：回退那一份现在连闸门②都过不了，上面那句「钉的是闸门顺序」就成了假话")
    assert result["outcome"] == rv.OUTCOME_MANUAL_MISSING, (
        f"回退那份只含自动段，出口该是缺人工段：{result['outcome']}")
    assert stub.count == 0, "闸门①没过却仍然调了观测（白付一次模型调用）"
    assert ask.count == 0, "闸门①没过却仍然调了问句旁路（闸门顺序被挪了）"
    assert result["decision_id"] is None, "缺人工段这一轮不许落账"
    assert _count_decisions() == before, "空表回退写进了账本"
    assert result["reason"], (
        "设计席原话是「不写库也要出声（返回值里有 reason）」——reason 必须单独成格，"
        "只折进 message 里不算")
    assert "请先在人工段写下你自己的判断" in result["reason"], (
        f"返回值里的原因不是闸门①那句原文：{result['reason']}")
    assert "请先在人工段写下你自己的判断" in result["message"], (
        f"原因只在返回值里、没进播报（用户看不到为什么这一轮没产出）：{result['message']}")
    assert rv.OUTCOME_LABELS_ZH[result["outcome"]] in result["message"], (
        "播报里的出口中文名不是取自 core 那本标签表——文案又分叉成两份了")
    assert result["requires_approval"] is False
    print("[OK] 空表回退：现采一份、闸门①出声、零观测调用、零 Decision 行")


def test_negative_control_missing_no_approval_flag_still_pauses():
    """负控：同一份 handler 返回值，只差 requires_approval 那一格——有则走完、无则必须暂停

    上面那条端到端里的「没有 approval 事件」这句得有牙齿：复盘是**先落账、后返回**的，
    假如 execute_plan 压根不读这个标记，流程会在写完账之后才暂停，`decisions` 那一行
    照样在——那条用例就可能因为它根本不检查暂停而假绿。这里把分发点直接按两种返回值
    各跑一遍，只差那一格，证明确实有人在读它。
    """
    plan = [_review_task(_make_review_template("负控标记"))]
    shape = {"ok": True, "message": "🧭 假 handler 的播报", "approval": None,
             "outcome": rv.OUTCOME_PROPOSED, "decision_id": 99}

    def _run(flag):
        executor = TaskExecutor()
        payload = dict(shape)
        if flag is not None:
            payload["requires_approval"] = flag
        # 假 handler 只换返回值，不改分发点：task_kind 是真的，走的仍是新那条分支
        executor.handle_long_term_review_task = lambda task: dict(payload)
        # 别让假审批落到临时库里留噪声行（这条用例只关心循环停不停）
        executor._persist_approval = lambda store, approval: None
        seen = []
        gen = executor.execute_plan(plan)
        for event in gen:
            seen.append(event)
            if event.get("type") == "approval":
                break
        gen.close()
        return seen

    without_flag = [e for e in _run(None) if e.get("type") == "approval"]
    with_flag = _run(False)
    assert len(without_flag) == 1, (
        "少了 requires_approval 这格居然也不暂停——说明 execute_plan 根本没读它，"
        "那端到端那条用例就是恒真的")
    assert [e for e in with_flag if e.get("type") == "approval"] == [], (
        "带 requires_approval=False 仍然停在审批点：这一格没生效")
    done = [e for e in with_flag if e.get("type") == "done"]
    assert len(done) == 1, f"补上标记后必须自己跑到 done：{with_flag}"
    assert (done[0].get("stats") or {}).get("saved") == 1, (
        f"这一格落了账，计数该在 saved：{done[0].get('stats')}")
    print("[OK] 负控：requires_approval 那一格确实被分发点读着（少它必停）")


TESTS = [
    test_isolated_database_and_new_tables_exist,
    test_observe_key_literals_match_agents_supervisor,
    test_real_collector_state_renders_three_sections,
    test_manual_section_missing_is_the_first_gate,
    test_adding_one_manual_item_changes_the_exit,
    test_no_activity_fact_requests_supplement_and_records_it,
    test_one_activity_fact_is_enough_even_with_many_unknowns,
    test_gate_two_reads_the_produced_marker,
    test_three_request_shapes_say_three_different_things,
    test_ask_bypass_questions_are_recorded_with_their_source,
    test_empty_ask_bypass_falls_back_and_says_so,
    test_choose_goal_shape_calls_neither_bypass,
    test_confidence_splits_suggestion_from_exploratory,
    test_negative_control_threshold_moves_the_tier,
    test_historical_snapshot_without_markers_is_read_honestly,
    test_no_change_needed_creates_no_decision_row,
    test_negative_control_row_counter_has_teeth,
    test_proposed_writes_ledger_with_typed_items,
    test_views_are_separated_by_content,
    test_execution_view_carries_no_natural_language,
    test_negative_control_leaky_execution_view_is_caught,
    test_existing_two_actions_unchanged_by_snapshot,
    test_snapshot_comparator_catches_a_changed_value,
    test_valid_actions_superset_after_change,
    test_create_template_accepts_long_term_review,
    test_unknown_action_type_is_rejected,
    test_update_template_rejects_invalid_action_without_touching_row,
    test_review_template_builds_one_review_task,
    test_execute_template_review_writes_decision_without_approval,
    test_review_handler_injects_the_ask_bypass,
    test_review_handler_falls_back_when_no_snapshot_exists,
    test_negative_control_missing_no_approval_flag_still_pauses,
]


def main():
    try:
        for fn in TESTS:
            fn()
    finally:
        cleanup_test_db()


if __name__ == "__main__":
    main()
