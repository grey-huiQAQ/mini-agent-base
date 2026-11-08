# -*- coding: utf-8 -*-
"""
长期项目复盘（P7 第一闭环 Phase 3b，2026-10-06）——State → 观测 → Decision 的那一跳

职责：把 Phase 2 的一份 Project State 交给 Phase 3a 的观测旁路，再按观测结果决定
**要不要立一行 Decision**。本模块是本闭环第一个「读 State、写 decisions」的流程，
也是 `supervisor_observe` 的第一个消费者——3a′ 把它的输入泛化成「观测载荷」就是为这一步。

四件事（拆分文档的口径）：
- **改哪些文件**：本模块（新增）＋ `core/task_template.py` 的 `VALID_ACTIONS` 增
  `long_term_review` ＋ `tests/test_project_review_loop.py`（新增）。
- **本模块刻意不做的事**：不 import `agents`（层序：core=0 不许引 agents=2，见
  `checks/check_import_direction.py`）⇒ 观测函数由**调用方注入**，3c 的控制器把
  `agents.supervisor.supervisor_observe` 传进来；不渲染界面（3c）；不回写经验（4a）；
  不注入上下文（4b）。
  → 更正（Phase 3c-乙，2026-10-07）：「不渲染界面（3c）」里的 3c 已经做了——「🧭 项目复盘」
    Tab 与模板表单的动作类型格都在 `ui/` 侧（回调集中在 `ui/controllers/review_controller.py`）。
    本模块仍然不含任何 UI 代码，这一条的范围没变，变的是「谁做了后半程」。
- **对已有功能的影响**：无。`VALID_ACTIONS` 多一个成员只回答「能不能建这种模板」；
  执行侧对不认识的 action_type 返回空计划（`agents/task_executor.py` 的
  `_build_plan_from_template` 里那条兜底），本批**不给 `long_term_review` 任何执行分支**
  ——立动作 ≠ 能执行，这条由测试钉住。
  → 更正（Phase 3c-甲，2026-10-06）：执行分支已经接上，`long_term_review` 现在跑得动；
    → 更正（Phase 3c-乙，2026-10-07）：且非法 `action_type` 从「静默降级成 search_and_note」
    改成**报错不建行**（判据④，设计席裁定「改的时机是 3c，不是现在」）。上面两句都是
    3b 当时的范围声明，不是现在的状态。
- **回滚**：`git revert`；本模块写的是 `decisions`／`decision_items`，回滚数据要按
  写进去的 id 删行（没有别的表引用它们）。

四个出口（`outcome` 四值，每个对应拆分文档里的一句判据）：
    manual_section_missing         人工段为空 ⇒ **不调用任何模型、不立 Decision**。设计席补的
                                   第二条判据：宁可这轮什么都不产出，也不把「只有 k=v 事实
                                   的载荷」喂进去换一句伪观测——伪观测与「本周无需决策」同源，
                                   都是「不该说话时没稳住」。
    insufficient_information       缺少实质依据 ⇒ 立一行 status=insufficient_information 的
                                   Decision **请求补充**，不给方案。阶段一（2026-10-11）把
                                   这一格的判据从「未知条数 > 事实条数」换成「有没有项目活动
                                   计数」，并把这一格里再分三个形状（询问方向／引导建立上下文／
                                   先选目标）；出口名字没动，动的是**什么时候走到它**与**到了
                                   之后说什么**。理由见下面「闸门②为什么换掉」一节。
    no_change_needed               观测判不了（问题与建议两格都是「无法判断」）⇒ 不立 Decision。
    proposed                       观测给出可用的问题与建议 ⇒ 立一行 status=proposed 的
                                   Decision，并把 State 的条目按类抄进账本。观测自报的
                                   置信度决定它是「建议」还是「探索性假设」（`gate_shape`），
                                   两者都落账，差别写在 summary 的措辞里。

闸门②为什么换掉（阶段一，2026-10-11，实测底座见 PROJECT_STATUS.md §五 本批条目）：
旧判据是「unknowns 条数 > 自动段 fact 条数」，而 unknowns 里有 5 条是采集层结构上给不出的
字段（`core.project_state.AUTO_UNSERVABLE`，每一份快照都在），fact 里又有一堆「读到且为零」
的计数与仓库卫生台账。两边都不是活动信号，比条数就是必输——实测四种真实库形状（新库／
一个目标／两个目标／生产库副本）旧闸门②**全部判「未知占主导」**，这就是用户「反复得不到决策」
的根。新判据不问条数，问「这份快照里有没有一条项目活动计数是非零的」，由采集器自己标。

配额口径（阶段一改了，旧那句「前两个出口一次配额都不花」已经是假话，留原文在这里会误导）：
    · 闸门①（缺人工段）——**两个旁路都不打**，仍然是零模型调用；
    · 闸门②的「先选目标」形状——数据里已经写明该问什么（active 目标不止一个、采集层不替人
      挑），打模型只会把那串 id 抄一遍 ⇒ **不打**；
    · 闸门②的「询问方向／引导建立上下文」形状——三问按设计席裁定由模型现生成 ⇒ **打一次问句
      旁路**（不是观测旁路），代价是每次这样的轮次花一格配额；
    · 「建议／探索性假设／判不了」——观测旁路一次。
只有「人工段为空」这一条还能用「观测调用次数为 0」验；测试里那句计数断言已收窄到闸门①。
"""

from typing import Any, Callable, Dict, List, Optional

# core → core 同层，合法（见 checks/check_import_direction.py 的层序）
# 证据标记与 goal 码由**采集器自己声明**（阶段一）：本模块只比对常量，不解析 `k=v` 字符串，
# 所以采集器改了值形态不会让这里的判断静默变味——改了标记名两边都对不上，测试先红。
# 这里刻意**不引** `UNKNOWN_CODE_GOAL_NONE`：那个码描述的形状（没有 active 目标）是
# `_request_shape` 的默认档，不需要比码；要比的只有 ambiguous 那一格。留一个没人读的
# 常量名只会让人以为分流看了两个码。
from core.project_state import (
    CAPABILITY_MAP, UNKNOWN_CAPABILITY,
    EVIDENCE_ACTIVITY, UNKNOWN_CODE_GOAL_AMBIGUOUS)
from core import decision_store as ds

# ---------- 出口四值 ----------

OUTCOME_MANUAL_MISSING = "manual_section_missing"
OUTCOME_INSUFFICIENT = "insufficient_information"
OUTCOME_NO_CHANGE = "no_change_needed"
OUTCOME_PROPOSED = "proposed"
REVIEW_OUTCOMES = (
    OUTCOME_MANUAL_MISSING, OUTCOME_INSUFFICIENT, OUTCOME_NO_CHANGE, OUTCOME_PROPOSED,
)

# ⚠️ 观测那几格的键名与哨兵：这里按**字面量**复制了一份，因为 core 不许 import agents。
# 这不是「两处手工保持一致」的邀请——tests 层允许引任何层，所以
# tests/test_project_review_loop.py 里有一条用例直接 import agents.supervisor 逐值比对：
# supervisor 那边一改这些字面量，那条用例立刻红，而不是让本模块把观测静默读成空。
OBSERVE_KEY_PROBLEM = "问题"
OBSERVE_KEY_NEXT_STEP = "建议下一步"
OBSERVE_KEY_CAPABILITY = "需要的能力"
#: 阶段一新增的第四格：模型自报的把握（0–1 的小数，或 None＝它没说）
OBSERVE_KEY_CONFIDENCE = "置信度"
OBSERVE_KEYS = (OBSERVE_KEY_PROBLEM, OBSERVE_KEY_NEXT_STEP,
                OBSERVE_KEY_CAPABILITY, OBSERVE_KEY_CONFIDENCE)
UNABLE_TO_JUDGE = "无法判断"

#: 问句旁路的输出键（同上，按字面量复制、由跨层比对用例钉住）
ASK_KEY_QUESTIONS = "补充问题"

#: 置信度分档线：≥ 这条线才敢说成「建议」，之下只能说成「探索性假设」。
#: 写成具名常量而不是散在 if 里的 0.5（设计席阶段一判据原文要求），且**模型没给置信度时
#: 按最低档处理**——把缺席读成中性（0.5）等于替模型填一个它没说过的数。
CONFIDENCE_SUGGESTION_MIN = 0.5

# ---------- 闸门②之后的形状（`gate_shape`；出口四值不动，这是四值**里面**的分档）----------
# 定案来源：设计席阶段一裁定「四值不动＋结果里加一个形状字段」。ASCII 键名是给程序用的
# （`DESIGN_ANCHORS.md` 已登记），中文标签只进 Human View——两个视图按内容分离那条判据
# 在这两格上同样是执行着的。
SHAPE_SUGGESTION = "suggestion"                       # 有实质依据＋置信度够：说成建议
SHAPE_EXPLORATORY = "exploratory"                     # 有实质依据＋置信度低：说成探索性假设
SHAPE_ASK_DIRECTION = "ask_direction"                 # 无实质依据、目标明确：就问这个目标
SHAPE_BUILD_CONTEXT = "build_context"                 # 无实质依据、连目标都没有：先搭上下文
SHAPE_CHOOSE_GOAL = "choose_goal"                     # 无实质依据、目标有多个：先请人选一个
GATE_SHAPES = (SHAPE_SUGGESTION, SHAPE_EXPLORATORY, SHAPE_ASK_DIRECTION,
               SHAPE_BUILD_CONTEXT, SHAPE_CHOOSE_GOAL)

#: 复盘产出的决策默认跨度。**这里刻意不写「因为每周跑一次所以选 weekly」**——那是把频率
#: 当跨度（Fix A 之前这句就写在原地）。复盘这一轮给出的是「接下来这一周要落地的改法」，
#: 跨度＝operational；它还必须落在 `ds.EXPERIENCE_ELIGIBLE_SCOPES` 里，否则复盘产出的
#: 账永远回流不成经验（4a 的筛选会结构性地把它挡掉）。这一格由测试钉住。
REVIEW_DECISION_SCOPE = ds.SCOPE_OPERATIONAL

# ---------- 载荷渲染：一份 State 怎么变成「观测材料」 ----------

SECTION_FACTS = "【项目状态摘要 · 自动段（k=v 事实）】"
SECTION_MANUAL = "【人工段（用户原话，不是模型生成）】"
SECTION_UNKNOWN = "【未知（读不出来就明说，不猜）】"
_EMPTY_SECTION_LINE = "（本段为空）"


def _state_reference(state: Dict[str, Any]) -> str:
    """这份 State 的出处标识，写进 decision_items.source

    没落库的快照（直接传进来的 dict）也要有个能写的出处，所以给一个明说「未落库」的形式，
    **不假造一个 id**。
    """
    state_id = state.get("id")
    if state_id is None:
        return "project_states#(未落库快照)"
    return f"project_states#{state_id}"


def _items_by_kind(state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """把 state['items'] 分成「自动段事实」与「人工段」两堆（判据⑥用的就是这个分法）

    分法只有一条：**kind == 'fact' 的是自动段，其余全是人工段**。Phase 2 已定案
    「自动段刻意只产 fact」「user_report 与 assumption 只有人工段能填」，所以不必再看
    source 长什么样——按 source 判会退化成「猜出处像不像人写的」。
    """
    facts: List[Dict[str, Any]] = []
    manual: List[Dict[str, Any]] = []
    for item in state.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == ds.KIND_FACT:
            facts.append(item)
        else:
            manual.append(item)
    return {"facts": facts, "manual": manual}


def has_manual_section(state: Dict[str, Any]) -> bool:
    """人工段有没有东西：判据⑥的门（没有就不许调用观测）"""
    return bool(_items_by_kind(state)["manual"])


def has_substantive_evidence(state: Dict[str, Any]) -> bool:
    """这份快照里有没有**一条**项目活动计数是非零的：闸门②的新判据（阶段一，2026-10-11）

    只认 `evidence == EVIDENCE_ACTIVITY` 的自动段 fact。三件事按定案各归各位：
    - **哪一族算活动由采集器声明**（`core.project_state` 的 goal_tasks／notes／decisions
      三族，其余标 context 或 activity_empty），这里不比条数、不解析字符串；
    - 人工段不参与计数：kind 不是 fact 的条目根本没有 evidence 这一格，闸门①已经管过它们；
    - 「读不到标记」不等于「读到了零」：历史快照（在这个标记引入之前存的）条目里没有
      `evidence`，`.get()` 出来是 None ⇒ 不算实质依据，而 `_request_shape` 的 reason 会
      如实说「这一份没有证据标记」，不假装它读到了一堆零。
    """
    return any(item.get("evidence") == EVIDENCE_ACTIVITY
               for item in _items_by_kind(state)["facts"])


def unknown_codes(state: Dict[str, Any]) -> List[str]:
    """这份快照登记的 unknown 码（去重、保持出现顺序）

    目前只有 `_collect_goal` 的两个分支会带码（goal_none／goal_ambiguous），
    它们是「先选目标」与「引导建立上下文」这两个形状的唯一判据。
    """
    seen: List[str] = []
    for unknown in state.get("unknowns") or []:
        if not isinstance(unknown, dict):
            continue
        code = unknown.get("code")
        if isinstance(code, str) and code.strip() and code not in seen:
            seen.append(code.strip())
    return seen


def _request_shape(state: Dict[str, Any]) -> str:
    """没有实质依据时该走哪一个形状：先看目标定没定下来，再看是不是多个。

    顺序是刻意的：`goal_id` 非空 ⇒ 问什么都得绕着这个目标说（询问方向）；
    为空时**先判「多个」再判「没有」**——ambiguous 的快照 goal_id 也是 None，
    把它归进 build_context 会让人以为系统不知道自己有目标，而它其实是在等人挑一个。
    """
    if (state.get("goal_id") or "").strip():
        return SHAPE_ASK_DIRECTION
    codes = unknown_codes(state)
    if UNKNOWN_CODE_GOAL_AMBIGUOUS in codes:
        return SHAPE_CHOOSE_GOAL
    return SHAPE_BUILD_CONTEXT


def _read_confidence(observation: Dict[str, Any]) -> Optional[float]:
    """从观测里取置信度；不是 0–1 的数就按「没说」处理（None）

    钳制本来在 supervisor 那一头（`core.ai_filter._clamp01`），这里**再做一次范围判断**
    不是重复劳动：注入进来的观测函数是调用方的（桩、将来的别的实现），本模块不能假设
    它一定钳过。bool 刻意排除——`True` 是一个数，把它当 1.0 就是替模型说「它有十足把握」。
    """
    raw = observation.get(OBSERVE_KEY_CONFIDENCE)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value < 0.0 or value > 1.0:
        return None
    return value


def _percent(value: float) -> str:
    """0.35 → "35%"（给人看的一格，不写 0.35 那种要心算的形态）"""
    return f"{int(round(value * 100))}%"


def _confidence_short(confidence: Optional[float]) -> str:
    """账本 summary 里那一格的短写法：只有数或「没拿到数」，判断留给 reason"""
    if confidence is None:
        return "置信度：这一格没拿到可用的数"
    return f"置信度 {_percent(confidence)}"


def _confidence_clause(confidence: Optional[float]) -> str:
    """置信度那句完整的话：到了线／没到线／没拿到数，三种读法写成三句实话

    刻意不把 None 写成「0%」或「50%」——那是替模型说它没说过的话（与 supervisor 的
    契约②「判不了就写无法判断，绝不编造」同源）。None 有两种来路，话里都点到：模型
    这一格**没报**，或报的不是 0–1 之间的数（`_read_confidence` 按没拿到处理）。
    「没报」与「没到线」要**分开说**：前者是这一格是空的，后者是它说了没把握，
    人看完要做的动作不一样。
    """
    line = _percent(CONFIDENCE_SUGGESTION_MIN)
    if confidence is None:
        return (f"没拿到可用的置信度（模型没报这一格，或报的不是 0–1 之间的数）——"
                f"按不够线处理，不替它填一个 {line} 假装中性")
    if confidence >= CONFIDENCE_SUGGESTION_MIN:
        return f"模型自报的置信度是 {_percent(confidence)}，到了 {line} 这条线"
    return f"模型自报的置信度是 {_percent(confidence)}，没到 {line} 这条线"


def _missing_clue(state: Dict[str, Any]) -> str:
    """「我还缺什么」——只报这份快照自己登记的 unknown 字段名，最多三格

    刻意不叫模型补，也不写「信息不足」这种空话：探索性假设要带的那句「我还缺…」必须能
    追溯到 items／unknowns 里的东西，否则它就是一句姿态。三格封顶是因为这条要写进
    summary 给人读，不是给人做统计的。
    """
    fields: List[str] = []
    for unknown in state.get("unknowns") or []:
        if not isinstance(unknown, dict):
            continue
        field = str(unknown.get("decision_field") or "").strip()
        if field and field not in fields:
            fields.append(field)
    if not fields:
        return "这份快照没有登记任何未知，所以我说不出还缺哪一格"
    return "、".join(fields[:3])


def build_observation_payload(state: Dict[str, Any]) -> str:
    """把一份 State 摊成 3a′ 说的「观测载荷」（多行材料，自动段 ＋ 人工段 ＋ 未知）

    三段结构是刻意的：自动段给机器事实（`decision_field=content`），人工段给原话并标明
    kind，未知段明说「这一格读不出来」。观测被允许看到未知段——**不许**只给它挑剩下的
    漂亮事实，那等于让它在一堆半成品上编结论。
    """
    buckets = _items_by_kind(state)
    lines: List[str] = [SECTION_FACTS]
    if buckets["facts"]:
        for item in buckets["facts"]:
            lines.append(f"- {item.get('decision_field', '')}={item.get('content', '')}")
    else:
        lines.append(_EMPTY_SECTION_LINE)
    lines.append(SECTION_MANUAL)
    if buckets["manual"]:
        for item in buckets["manual"]:
            lines.append(
                f"- [{item.get('kind', '')}] {item.get('content', '')}"
            )
    else:
        lines.append(_EMPTY_SECTION_LINE)
    lines.append(SECTION_UNKNOWN)
    unknowns = state.get("unknowns") or []
    if unknowns:
        for unknown in unknowns:
            if isinstance(unknown, dict):
                lines.append(
                    f"- {unknown.get('decision_field', '')}: {unknown.get('reason', '')}"
                )
    else:
        lines.append(_EMPTY_SECTION_LINE)
    return "\n".join(lines)


# ---------- 结果组装 ----------

def _result(outcome: str, state: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """结果的唯一构造点：公共字段一次给齐，避免某条出口漏掉 counts 而下游读到 None

    `state` 那份原始条目**不放进结果**——结果要交给两个 View，Human View 需要的是
    渲染好的句子，Execution View 需要的是标识与计数；把整份 State 留在中间会让两个视图
    都能顺手捞到自然语言，判据①就白立了。
    """
    buckets = _items_by_kind(state)
    base: Dict[str, Any] = {
        "outcome": outcome,
        "state_ref": _state_reference(state),
        "state_id": state.get("id"),
        "goal_id": state.get("goal_id"),
        "decision_id": None,
        "decision_status": None,
        "decision_scope": None,
        "observation": None,
        "payload": None,
        "observed": False,
        # 阶段一新增的三格。`asked` 与 `observed` 是**两件事**：前者是问句旁路被调过，
        # 后者是观测旁路被调过——请求补充那一档花的是前一格，判不了那一档花的是后一格。
        # 把它们合成一个布尔，界面与调度就再也分不出「这轮问了问题」和「这轮给过判断」。
        "asked": False,
        "gate_shape": None,
        "confidence": None,
        "questions": [],
        "capability_keys": [],
        "item_ids": [],
        "counts": {
            "fact": len(buckets["facts"]),
            "manual": len(buckets["manual"]),
            "unknown": len(state.get("unknowns") or []),
        },
        "reason": "",
        "skipped": [],
    }
    base.update(extra)
    return base


def _clean(value: Any) -> str:
    """取一个非空字符串，否则给哨兵词（View 不许读到 None）"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return UNABLE_TO_JUDGE


def _pick_capabilities(observation: Dict[str, Any]) -> List[str]:
    """从观测结果里取能力键（只认闭集里的键，与本模块之外的过滤形成第二道）"""
    raw = observation.get(OBSERVE_KEY_CAPABILITY)
    keys: List[str] = []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for name in raw:
            if isinstance(name, str) and name.strip() and name.strip() not in keys:
                keys.append(name.strip())
    return keys


# ---------- 主体 ----------

def run_long_term_project_review(
    state: Dict[str, Any],
    observe: Callable[[str], Dict[str, Any]],
    ask_questions: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """跑一次复盘：State → （闸门）→ 观测／问句 → 要不要立一行 Decision

    Args:
        state: 一份 Project State（`core.project_state.build_project_state` 的返回值，
            或 `latest_project_state()` 读回来的那一份）。它的 `items` 里**必须有人工段**
            （kind 不为 fact 的那些），否则走 manual_section_missing 出口。
        observe: 观测函数，签名 `(payload: str) -> {问题, 建议下一步, 需要的能力, 置信度}`。
            生产里传 `agents.supervisor.supervisor_observe`——**本模块不 import 它**，
            因为 core 引 agents 是层序倒置。这一格是注入的，不是可选的：没有观测函数
            就没有「这一轮复盘说了什么」，而「没有」与「判不了」是两件事，不能混。
        ask_questions: 问句旁路，签名 `(payload: str) -> {"补充问题": [...]}`。
            生产里传 `agents.supervisor.supervisor_ask_questions`（3a 那条「立函数不接线」
            的先例：它是独立旁路、独立提示词，不复用观测那三格）。
            **给 None 是可用的**：三个请求补充形状会改用兜底文案，并在 reason 里明说
            「这三问是内置的通用引导问，不是模型针对这份快照生成的」。这不是降级偷懒——
            单测里它就是「模型不可用」那一档的构造方式（本批的测试一律离线）。

    Returns:
        结果 dict（字段见 `_result`）。`outcome` 四值之一；只有 `proposed` 与
        `insufficient_information` 会写库，返回里带 `decision_id`。`gate_shape` 是
        四值**里面**的分档（五值，见 `GATE_SHAPES`），manual_section_missing 与
        no_change_needed 不属分流，那一格是 None。

    这一函数**不抛异常给上层之外的错误语义**：`decision_store` 的规则违规照原样抛出
    （账本写不进去必须响，静默返回会让下一轮以为这轮已经复盘过了），而「闸门没过」是
    正常出口、不是异常。注入进来的两个旁路按各自的契约**不许抛**（agents/supervisor.py
    的三条契约①），本模块不替它们兜——替一个违约的旁路兜异常，等于把「接线错了」这件事
    读成「这一轮判不了」。
    """
    if not isinstance(state, dict):
        raise ValueError("run_long_term_project_review：state 必须是 dict，不接受猜")

    # 闸门①（判据⑥）：人工段为空 ⇒ 连载荷都不成立，不调用任何旁路、不立 Decision
    if not has_manual_section(state):
        return _result(
            OUTCOME_MANUAL_MISSING, state,
            reason="这份 State 没有人工段（kind 不为 fact 的条目为零）。"
                   "只有 k=v 事实的载荷不足以支撑一次观测——我不拿它换一句看起来合理的"
                   "「问题」，请先在人工段写下你自己的判断。",
            skipped=["未调用观测", "未调用问句旁路", "未立 Decision"],
        )

    # 闸门②（阶段一改写）：没有一条项目活动计数是非零 ⇒ 不给方案，按形状请求补充。
    # 旧写法是 unknowns_dominant（比条数），实测四种真实库形状全被判「未知占主导」——
    # 那是判据选错了东西，不是阈值调错了数。理由写在模块 docstring 的
    # 「闸门②为什么换掉」一节，改回去之前请先读完那一段。
    if not has_substantive_evidence(state):
        return _record_request(state, _request_shape(state), ask_questions)

    payload = build_observation_payload(state)
    observation = observe(payload) or {}
    problem = _clean(observation.get(OBSERVE_KEY_PROBLEM))
    next_step = _clean(observation.get(OBSERVE_KEY_NEXT_STEP))

    # 判据②：观测判不了 ⇒ 「本周无需决策」。认「问题」与「建议下一步」两格同时是哨兵
    # 才算判不了——只认「问题」会让「有问题但说不出下一步」那种半成品立成决策。
    if problem == UNABLE_TO_JUDGE and next_step == UNABLE_TO_JUDGE:
        return _result(
            OUTCOME_NO_CHANGE, state,
            observation=observation,
            payload=payload,
            observed=True,
            reason="当前没有发现值得改变的地方（观测给出的问题与建议两格都是「无法判断」）。"
                   "这一轮不立 Decision——没有依据的决策行比空白更坏。",
            skipped=["未立 Decision"],
        )

    # 置信度分档（阶段一）：够线才敢说成「建议」，不够线包括**模型没说**，都说成
    # 「探索性假设」。这里不拿 None 当 0.5：把缺席读成中性就是替模型填了一个它没说过的数。
    confidence = _read_confidence(observation)
    shape = (SHAPE_SUGGESTION if confidence is not None
             and confidence >= CONFIDENCE_SUGGESTION_MIN else SHAPE_EXPLORATORY)
    return _record_proposed(state, payload, observation, problem, next_step,
                            shape, confidence)


#: 一次请求补充最多问几条（设计席阶段一规格：三问）。多了变问卷，少了没有推进力。
REQUEST_QUESTION_LIMIT = 3

#: 固定问题表。两种用途，都如实标在 reason／source 里：
#: ① 「先选目标」这一档**只有**这里这一条——它要问什么由数据本身写明（active 目标不止一个、
#:    采集层不替人挑），打模型只会把那串 goal_id 抄一遍，所以这一档压根不打；
#: ② 另两档在问句旁路不可用（未注入、或它这轮没给出问题）时用它兜底。**兜底问刻意写得通用**：
#:    这一档存在的前提就是「没人针对这份快照生成过问题」，所以不许出现只有模型才知道的内容。
_FIXED_QUESTIONS: Dict[str, List[str]] = {
    SHAPE_CHOOSE_GOAL: [
        "这一轮要围绕哪个目标复盘？请在触发复盘时显式指定 goal_id"
        "——现在有不止一个 active 目标，采集层不替人挑一个。",
    ],
    SHAPE_ASK_DIRECTION: [
        "这个目标本周要推进到哪一步？请用一句"
        "「做完之后能看出来做没做到」的话来说。",
        "它下面现在有没有已经开始但没做完的事？",
        "你手上有什么已经做出来的东西可以直接给它用？",
    ],
    SHAPE_BUILD_CONTEXT: [
        "你现在最想推进的是哪件事？一句话说清它。",
        "这件事手上已经有什么材料（笔记、文件、跑过的记录）？",
        "这一周准备给它多少时间、到什么程度算有进展？",
    ],
}


def _shape_wording(state: Dict[str, Any], shape: str, state_ref: str) -> Dict[str, str]:
    """三个请求补充形状各自的 title／summary／reason 文案（设计席：三者必须不同）

    写成一张函数而不是一句通用文案＋一个形状名：如果三格只换个形状词，用户收到的
    其实是同一句话，而「先选目标」和「连目标都没有」要他做的动作完全不同。
    文案里出现的每个数都从这份 State 自己读（条目数、码），不编。
    """
    goal_id = (state.get("goal_id") or "").strip()
    counts = _items_by_kind(state)
    fact_count = len(counts["facts"])
    unknown_count = len(state.get("unknowns") or [])
    if shape == SHAPE_ASK_DIRECTION:
        return {
            "title": f"关于目标 {goal_id}：先回答三个问题（快照 {state_ref}）",
            "summary": "这份快照绑定到了一个明确的目标，但里面没有一条被采集器标为"
                       "「项目活动证据」的读数，所以这一轮不给方案，先就这个目标问三个具体问题。",
            "reason": f"目标已定（{goal_id}），但自动段 {fact_count} 条事实里没有一条被采集器"
                      f"标为项目活动证据，未知 {unknown_count} 条。依据不足以支撑一个建议，"
                      f"按「询问方向」收场：问题落在人这边。",
        }
    if shape == SHAPE_BUILD_CONTEXT:
        return {
            "title": f"复盘之前先建立上下文：此刻没有可复盘的目标（快照 {state_ref}）",
            "summary": "goals 表里选不出 active 目标，这份快照也没有一条被标为项目活动证据的"
                       "读数——先别问「这个目标怎么推进」，先把上下文三件事说清楚。",
            "reason": f"这份快照既没有绑定目标（采集层在 goals 表里选不出 status='active' 的"
                      f"行），自动段 {fact_count} 条事实里也没有一条被采集器标为项目活动证据，"
                      f"未知 {unknown_count} 条。按「引导建立上下文」收场："
                      f"这一轮问的不是方向，是先有什么东西可复盘。",
        }
    return {
        "title": f"请先选定一个目标再复盘（快照 {state_ref}）",
        "summary": "goals 表里有多个 active 目标，采集层不替人挑一个；"
                   "这一轮连「关于谁」都没定下来，所以只问一句：这次复盘哪个目标。",
        "reason": f"这份快照的 goal_id 为空，且 unknown 里带着 {UNKNOWN_CODE_GOAL_AMBIGUOUS} "
                  f"这个码（active 目标不止一个，采集层不替人挑）。按「先选目标」收场："
                  f"这一句问什么由数据本身写明，所以这一轮**没打模型**。",
    }


def _take_questions(raw: Any) -> List[str]:
    """问句旁路的返回值收成干净的问题列表（非列表／空串／超过上限都在这儿处理掉）

    上限是 `REQUEST_QUESTION_LIMIT`：设计席给的是「三个具体问题」，模型给五条就挑前五条，
    一条都不给则按不可用处理（由调用方走兜底文案）。这里不补句子——补一个它没说的话
    就是本模块在编造。
    """
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        if text:
            out.append(text)
    return out[:REQUEST_QUESTION_LIMIT]


def _record_request(state: Dict[str, Any], shape: str,
                    ask_questions: Optional[Callable[[str], Dict[str, Any]]],
                    ) -> Dict[str, Any]:
    """闸门②的落库：一行 status=insufficient_information 的决策，内容是「请求补充」

    它为什么仍然要占一行账（设计席定案，一字未改）：`proposed → insufficient_information`
    是状态机上合法的边，而 `insufficient_information → discussed` 是全表唯一一条向后的边
    ——信息补齐之后要能「重新看过」。如果这轮只在返回值里说一句「信息不足」而不落库，
    下一轮就没人知道上一轮到底问过什么。

    三个形状（询问方向／引导建立上下文／先选目标）都走这一条落库路径、都是同一个出口词，
    差别只在**说的那三句话**与**打不打模型**（`gate_shape` 把这一格如实带出去）。
    """
    state_ref = _state_reference(state)
    wording = _shape_wording(state, shape, state_ref)
    asked = shape != SHAPE_CHOOSE_GOAL and ask_questions is not None
    questions: List[str] = []
    if asked:
        questions = _take_questions(
            (ask_questions(build_observation_payload(state)) or {}).get(ASK_KEY_QUESTIONS))
    # 没从旁路拿到问题 ⇒ 用固定问，并把这件事标在 source／reason 上。
    # 「先选目标」那一档永远走这里（它本来就不打模型），所以它不算兜底、只是一条固定问。
    from_fixed = not questions
    if from_fixed:
        questions = list(_FIXED_QUESTIONS.get(shape) or [])

    decision_id = ds.create_decision(
        title=wording["title"],
        goal_id=state.get("goal_id"),
        summary=wording["summary"],
        decision_scope=REVIEW_DECISION_SCOPE,
    )
    ds.set_status(decision_id, ds.STATUS_INSUFFICIENT)

    item_ids: List[int] = []
    # ① 问题登记成 unknown 条目：这一轮「缺的东西」就是这些问题的答案，
    #    4a 回读时要能看出当时问了什么（source 写清是谁生成的，三种来源不能混成一个）
    if not from_fixed:
        question_source = f"{state_ref}｜问句旁路（模型现生成）"
    elif shape == SHAPE_CHOOSE_GOAL:
        question_source = f"{state_ref}｜固定一问（由 unknown 码写明，未打模型）"
    else:
        question_source = f"{state_ref}｜内置兜底问（未打模型）"
    for question in questions:
        item_ids.append(ds.add_item(
            decision_id, ds.KIND_UNKNOWN, f"请求补充的问题：{question}",
            source=question_source))
    # ② 未知照抄：这一轮的「读不出来的那几格」是下次重新看过时的对照物
    for unknown in state.get("unknowns") or []:
        if not isinstance(unknown, dict):
            continue
        content = (f"{unknown.get('decision_field', '')}："
                   f"{unknown.get('reason', '')}").strip("：")
        item_ids.append(ds.add_item(
            decision_id, ds.KIND_UNKNOWN, content, source=state_ref))
    # ③ 人工段照抄：这一轮的「他说过什么」是下次重新看过时的对照物
    for item in _items_by_kind(state)["manual"]:
        item_ids.append(ds.add_item(
            decision_id,
            str(item.get("kind") or ds.KIND_USER_REPORT),
            str(item.get("content") or ""),
            source=item.get("source"),
        ))

    reason = wording["reason"]
    if from_fixed and shape != SHAPE_CHOOSE_GOAL:
        reason += ("｜这三问是内置的通用引导问，**不是针对这份快照生成的**"
                   "（问句旁路没注入，或它这轮没给出问题）。")
    if shape != SHAPE_CHOOSE_GOAL and not asked:
        reason += "｜这一轮没打模型。"
    # 历史快照的如实读法：条目里没有 evidence 这一格 ≠ 读到了零，两件事必须分开说
    if any("evidence" not in item for item in _items_by_kind(state)["facts"]):
        reason += ("｜这份快照的条目里没有证据标记（它是在该标记引入之前存的），"
                   "所以按「没有实质依据」处理，而不是当作读到了零。")
    if shape == SHAPE_CHOOSE_GOAL:
        # 这一档的问句来自 `_FIXED_QUESTIONS`，不是旁路——skipped 里要如实说没打过
        skipped = ["未给方案", "未调用观测", "未调用问句旁路"]
    elif asked:
        skipped = ["未给方案", "未调用观测"]
    else:
        skipped = ["未给方案", "未调用观测", "未调用问句旁路（三问走内置兜底）"]
    return _result(
        OUTCOME_INSUFFICIENT, state,
        gate_shape=shape,
        asked=asked,
        questions=questions,
        decision_id=decision_id,
        decision_status=ds.STATUS_INSUFFICIENT,
        decision_scope=REVIEW_DECISION_SCOPE,
        item_ids=item_ids,
        reason=reason,
        skipped=skipped,
    )


def _record_proposed(
    state: Dict[str, Any],
    payload: str,
    observation: Dict[str, Any],
    problem: str,
    next_step: str,
    shape: str,
    confidence: Optional[float],
) -> Dict[str, Any]:
    """判据「观测给出可用结论」的落库：一行 proposed 决策 ＋ 按类抄来的条目

    阶段一把这一档分成两句话（`suggestion`／`exploratory`），但**只有 summary 与 reason
    的措辞**跟着分：两档都落 `decisions`、都是 proposed、都不新增列——置信度与「还缺什么」
    是写进 summary 这句话里的，不是写成新字段（设计席阶段一定案：不碰 schema）。
    `suggestion` 的 summary 保持观测那句「建议下一步」**逐字**：既有断言与 4b 的经验回流
    都读这一格，「说成建议」这件事反过来改掉账的内容是本批不想要的连带面。
    """
    if shape not in (SHAPE_SUGGESTION, SHAPE_EXPLORATORY):
        raise ValueError(
            f"_record_proposed 只收两档形状，收到 {shape!r}——"
            "请求补充那三档的去向是 _record_request，不是这里")
    state_ref = _state_reference(state)
    if shape == SHAPE_EXPLORATORY:
        summary = (
            f"{next_step}｜{_confidence_short(confidence)}，这是一条试探而不是建议。"
            f"我还缺：{_missing_clue(state)}"
        )
        reason = (
            f"观测给出了问题与下一步，但{_confidence_clause(confidence)}，"
            f"所以这一行说成「探索性假设」而不是建议；我还缺：{_missing_clue(state)}。"
            "已立一条待讨论的决策；批准与执行都在人这边，本模块不代替。"
        )
    else:
        summary = next_step
        reason = (
            f"观测给出了可用的问题与建议，{_confidence_clause(confidence)}，"
            "所以这一行说成「建议」。"
            "已立一条待讨论的决策；批准与执行都在人这边，本模块不代替。"
        )
    decision_id = ds.create_decision(
        title=problem,
        goal_id=state.get("goal_id"),
        summary=summary,
        decision_scope=REVIEW_DECISION_SCOPE,
    )
    item_ids: List[int] = []
    # ① 自动段事实照抄（4a 评 wrong_assumption 时的对照物；source 必须跟着抄过来，
    #    否则账本上一条 fact 追不到表，Phase 2 判据①就在这条路径上重新失效）
    for item in _items_by_kind(state)["facts"]:
        item_ids.append(ds.add_item(
            decision_id, ds.KIND_FACT,
            str(item.get("content") or ""),
            source=item.get("source"),
        ))
    # ② 人工段照抄（「他本人说过」的那一侧）
    for item in _items_by_kind(state)["manual"]:
        item_ids.append(ds.add_item(
            decision_id,
            str(item.get("kind") or ds.KIND_USER_REPORT),
            str(item.get("content") or ""),
            source=item.get("source"),
        ))
    # ③ 观测的结论登记成 inference：它有依据（依据就是这份快照），所以不是 assumption。
    #    source 写快照出处，与 decision_store 里「inference 记支撑它的那条 fact」同口径。
    item_ids.append(ds.add_item(
        decision_id, ds.KIND_INFERENCE,
        f"观测结论｜问题：{problem}｜建议下一步：{next_step}",
        source=state_ref,
    ))
    # ④ 需要的能力登记成 assumption：这是「当前必须先采信的前提」，
    #    刻意不写 source——为空正是「还没有证据」的形状（decision_store.add_item 的口径）。
    capability_keys = _pick_capabilities(observation)
    for key in capability_keys:
        item_ids.append(ds.add_item(
            decision_id, ds.KIND_ASSUMPTION, f"需要的能力：{key}", source=None))
    # ⑤ 未知照抄，好让下次回读时知道当时缺了什么
    for unknown in state.get("unknowns") or []:
        if isinstance(unknown, dict):
            item_ids.append(ds.add_item(
                decision_id, ds.KIND_UNKNOWN,
                f"{unknown.get('decision_field', '')}：{unknown.get('reason', '')}".strip("："),
                source=state_ref,
            ))

    return _result(
        OUTCOME_PROPOSED, state,
        gate_shape=shape,
        confidence=confidence,
        decision_id=decision_id,
        decision_status=ds.STATUS_PROPOSED,
        decision_scope=REVIEW_DECISION_SCOPE,
        observation=observation,
        payload=payload,
        observed=True,
        capability_keys=capability_keys,
        item_ids=item_ids,
        reason=reason,
        skipped=["未批准", "未执行"],
    )


# ---------- 两个 View：纯函数，不碰库、不调模型 ----------

#: 状态给人看的那几个中文标签。刻意不把 decision_store 的枚举值直接印给人——
#: 人读的是「待讨论」，程序之间传的是 proposed；把这个分界写在代码里，
#: 判据①（两个 View 按内容分离）才有可执行的形式。
STATUS_LABELS_ZH = {
    ds.STATUS_PROPOSED: "待讨论（还没批准）",
    ds.STATUS_DISCUSSED: "讨论过",
    ds.STATUS_APPROVED: "已批准",
    ds.STATUS_REJECTED: "已否决",
    ds.STATUS_EXECUTED: "已执行",
    ds.STATUS_EVALUATED: "已复盘",
    ds.STATUS_INSUFFICIENT: "信息不足，等补充",
}

OUTCOME_LABELS_ZH = {
    OUTCOME_MANUAL_MISSING: "这一轮没有产出：缺人工段",
    # 2026-10-11 阶段一：旧文案是「未知多于事实」。闸门②改判「有没有实质依据」之后，
    # 这句成了假话——这一档不再比较任何条数，它是「没有一条项目活动计数是非零」。
    OUTCOME_INSUFFICIENT: "这一轮请求补充：缺少实质依据",
    OUTCOME_NO_CHANGE: "当前没有发现值得改变的地方",
    OUTCOME_PROPOSED: "立了一条候选决策，等人定",
}

#: 分流五档给人读的说法。`outcome` 四值是人看的**结论**，`gate_shape` 是同一出口**里面**
#: 的分档：proposed 里分「建议」与「探索性假设」，insufficient 里分「问方向」「建上下文」
#: 「先选目标」。这两个词表不能合成一个——拍板 4 定的是「出口四值不动」。
#: 与 OUTCOME_LABELS_ZH 同理：这里是中文措辞，枚举值只活在 Execution View 与账本里。
SHAPE_LABELS_ZH = {
    SHAPE_SUGGESTION: "说成建议",
    SHAPE_EXPLORATORY: "说成探索性假设",
    SHAPE_ASK_DIRECTION: "问你三条，等你定方向",
    SHAPE_BUILD_CONTEXT: "引导你建立上下文",
    SHAPE_CHOOSE_GOAL: "等你先选一个目标",
}


def render_human_view(result: Dict[str, Any]) -> Dict[str, Any]:
    """给人看的那一份：全是能读的句子，没有任何机器标识符

    为什么 Human View 要带 `decision_id`（一个数字）而 Execution View 也带它：数字不是
    自然语言句子，两视图都要有它——分离的是**措辞**，不是坐标。判据① 的比对只拿
    Human View 的字符串叶子去 Execution View 的序列化文本里找，找到即未分离。
    """
    observation = result.get("observation") or {}
    capability_keys = result.get("capability_keys") or []
    capabilities_zh: List[str] = []
    for key in capability_keys:
        info = CAPABILITY_MAP.get(key)
        if isinstance(info, dict):
            capabilities_zh.append(f"{info.get('label', '')}（键名 {key}）")
        elif key == UNKNOWN_CAPABILITY:
            capabilities_zh.append("说不出来（闭集里没有对应项）")
        else:
            capabilities_zh.append(f"表外键名 {key}，按未知处理")
    status = result.get("decision_status")
    status_zh = STATUS_LABELS_ZH.get(status, "本轮没有决策行") if status else "本轮没有决策行"
    shape = result.get("gate_shape")
    if shape is None:
        shape_zh = "这一轮不在分流里（缺人工段，或观测判不了）"
    else:
        shape_zh = SHAPE_LABELS_ZH.get(shape, f"形状词表外的值 {shape}，按未知处理")
    questions = [str(q) for q in (result.get("questions") or []) if str(q).strip()]

    return {
        "这一轮的结论": OUTCOME_LABELS_ZH.get(
            result.get("outcome"), "出口词表外的结果，按未知处理"),
        # 阶段一新增的两格。为什么要单独成格而不是只写在 reason 那句长话里：
        # 这三问是**等人回答**的，藏在段落里就等于没问；而形状那一格是「这一行该被读成
        # 建议还是试探」的第一句话，它必须比 reason 里的解释更早被人看到。
        "这一轮的说法": shape_zh,
        "等你补充的问题": questions or ["本轮没有提问"],
        "为什么要说这件事": _clean(result.get("reason")),
        "卡住的地方": _clean(observation.get(OBSERVE_KEY_PROBLEM))
                       if observation else "本轮没有做观测",
        "建议下一步": _clean(observation.get(OBSERVE_KEY_NEXT_STEP))
                       if observation else "本轮没有做观测",
        "需要的能力": capabilities_zh or ["本轮没有提出能力需求"],
        "这一条账的状态": status_zh,
        "依据这份快照的出处": str(result.get("state_ref") or ""),
        "本轮明确没做的事": list(result.get("skipped") or []) or ["无"],
        "读到的条目数": [
            f"自动段事实 {result.get('counts', {}).get('fact', 0)} 条",
            f"人工段 {result.get('counts', {}).get('manual', 0)} 条",
            f"未知 {result.get('counts', {}).get('unknown', 0)} 条",
        ],
        "决策编号": result.get("decision_id"),
    }


def to_execution_view(result: Dict[str, Any]) -> Dict[str, Any]:
    """给程序的那一份：只有标识、枚举值与计数，一个自然语言句子都不许有

    这条约束不是为了好看：Execution View 会被下游当指令用（3c 的按钮、将来的调度）。
    一旦它夹带给人读的句子，那句子里的「看起来合理」就会被当成要求执行——这与 Phase 2
    「自动段出现自然语言总结句即为失败」是同一件事的另一端。

    刻意**不带**的东西：payload 全文、观测三格原文、reason、skipped。它们只属于 Human
    View；程序要用的话，按 `decision_id` 回账本读，而不是在视图之间转述。

    阶段一新增的 `gate_shape`／`confidence`／`questions` 同样**不进**这一份，理由是「没有
    消费者就不留字段」（本项目在 3c-甲 就按这条删过 `previous_run_at`）：拍板 4 要的是
    **结果**里有形状字段，而今天没有任何程序按形状分流。将来调度真要按它分支，再加进来，
    而且必须是 ASCII 枚举值——中文标签那份永远只活在 Human View 里。
    """
    counts = result.get("counts") or {}
    return {
        "schema": "long_term_review.execution.v1",
        "outcome": result.get("outcome"),
        "observed": bool(result.get("observed")),
        "decision_id": result.get("decision_id"),
        "decision_status": result.get("decision_status"),
        "decision_scope": result.get("decision_scope"),
        "goal_id": result.get("goal_id"),
        "state_id": result.get("state_id"),
        "capability_keys": list(result.get("capability_keys") or []),
        "item_ids": list(result.get("item_ids") or []),
        "count_fact": int(counts.get("fact", 0)),
        "count_manual": int(counts.get("manual", 0)),
        "count_unknown": int(counts.get("unknown", 0)),
    }
