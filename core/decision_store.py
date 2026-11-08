# -*- coding: utf-8 -*-
"""
Decision Store - 第一闭环的决策账本（P7 第一闭环 Phase 1b，2026-10-06，纯新增叶子模块）

职责（只做下面这几件，别的都不做）：
- 写：把一条「值得改变的地方」作为 **一行 decisions** 立起来，并按状态机推进它；
- 记：把支撑它的条目作为 **decision_items** 分五类登记（fact / user_report /
  inference / assumption / unknown），修订走 kind='revision' 追加、原条目不删；
- 读：按 id / 按状态 / 按目标 / 按作用域取回，供 Phase 2 采集与 Phase 3 的 View 渲染；
- 回流：把一条**已评估**的决策提炼成一行 `experiences`（`link_experience`，Phase 4a）。
  它写的是别的模块拥有的那张表，所以走 `get_db()` 的裸 SQL 而**不 import
  `ExperienceStore`**——保住下面那句叶子性质。代价如实记一句：`experiences` 的列清单
  从此在两处各有一份（本函数与 `ExperienceStore.add_experience`），改那张表的列时要
  同时想起这里。

**本模块是一个叶子**：它不被任何既有代码 import，也不 import 除
`memory.database.get_db` 之外的任何项目模块。它连网络、调模型、读文件、渲染界面
一概不做。

> **2026-10-07（Phase 4a）就地更正**：这里原话说「它不被任何既有代码 import」
> 「3c 的界面只读」，两句到今天都不成立了——`core/project_state.py`（取 kind 常量）、
> `core/long_term_project_review.py` 与 `ui/controllers/review_controller.py` 都 import 它，
> 而界面那三个按钮（「✅ 批准」「🚫 否决」「🏁 标记已执行」）调的是 `set_status()`／
> `mark_executed()`，那是写路径。**仍然成立的那半句**是：本模块不 import 除
> `memory.database.get_db` 之外的任何项目模块——4a 的 `link_experience` 写的虽是别的模块
> 拥有的那张表，走的仍是裸 SQL。另记一句现状：`link_experience` **目前没有生产调用方**
> （4a 先把写路径与判据立住，界面出口未做）。

设计说明（与 ApprovalStore / core.task_template 保持一致）：
- 全部使用模块级函数 + memory.database.get_db()，每次调用独立连接，线程安全；
- 表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建，本模块不负责建表；
- 时间戳一律 UTC 带 +00:00（批次 14 定案第 5 条的 C9 口径），由写路径显式写入，
  不给 SQL DEFAULT——CURRENT_TIMESTAMP 产出的串不带偏移，混进来就两根轴了。

错误语义（这是本模块对外契约的核心，两种情况刻意分开处理）：
- **规则违规 → raise ValueError**（状态机禁跳、非法枚举值、空标题、把条目挂到不存在的
  决策上）。这类调用连"合法账目"都写不出来，静默返回 False 会让调用方以为自己写完了，
  于是账本上留下一条从未推进过的决策——第一闭环评的就是这类假账，所以必须响。
- **记录不存在 → 写路径返回 False、读路径返回 None**（这不是规则违规，
  只是"那一条不在这库里"，调用方本来就要处理）。
- 数据库层的异常（表缺失、磁盘满）**照常向上抛、不吞**：吞掉它会让人以为决策已经记账。
  这一点与 ApprovalStore 的"异常安全返回安全默认值"刻意不同，理由是审批写入失败
  最多让用户多点一次按钮，而决策账本写失败会让整轮复盘的历史消失。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from memory.database import get_db

# ---------- 常量 ----------

# 决策状态七值（拆分文档的定案，**revised 不在其中**：修订不是状态，见下面 ITEM_KIND_REVISION）
STATUS_PROPOSED = "proposed"
STATUS_DISCUSSED = "discussed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXECUTED = "executed"
STATUS_EVALUATED = "evaluated"
STATUS_INSUFFICIENT = "insufficient_information"
VALID_DECISION_STATUSES = (
    STATUS_PROPOSED, STATUS_DISCUSSED, STATUS_APPROVED, STATUS_REJECTED,
    STATUS_EXECUTED, STATUS_EVALUATED, STATUS_INSUFFICIENT,
)

# 合法转换表（2026-10-06 定案）。逐条给理由，因为下一个人一定会"顺手加一条边"：
#   proposed → discussed / approved / rejected / insufficient_information
#       一条候选决策可以直接批准（讨论过的内容记成条目就是讨论的证据），
#       也可以不看就否掉，还可以因为信息不足先搁置。
#   discussed → approved / rejected / insufficient_information
#       讨论完只有这三种收场；回到 proposed 不允许——那等于抹掉"讨论过"这件事。
#   approved → executed   只能由 mark_executed() 走（要顺带写 execution_reference）。
#   approved → rejected   **这条边是有意留的**：批准≠执行，人在执行前反悔是真事。
#       若不许反悔，唯一出路是把那行 approved 留着假装没这回事——那正是闭环要防的假账。
#   executed → evaluated  只能由 evaluate() 走（要顺带写 evaluation 与 actual_result）。
#   insufficient_information → discussed
#       信息补齐了就重回讨论。这是全表唯一一条向后的边，因为它表达的是"重新看过"，
#       不是"把决策改回没提过的样子"。
#   evaluated / rejected  ＝ 终态。评完的决策不许再改状态；要推翻它请立一条新决策，
#       并在老决策上追加 kind='revision' 的条目——评"我当时以为是什么"需要老账不动。
LEGAL_TRANSITIONS: Dict[str, tuple] = {
    STATUS_PROPOSED: (STATUS_DISCUSSED, STATUS_APPROVED, STATUS_REJECTED,
                      STATUS_INSUFFICIENT),
    STATUS_DISCUSSED: (STATUS_APPROVED, STATUS_REJECTED, STATUS_INSUFFICIENT),
    STATUS_APPROVED: (STATUS_EXECUTED, STATUS_REJECTED),
    STATUS_EXECUTED: (STATUS_EVALUATED,),
    STATUS_INSUFFICIENT: (STATUS_DISCUSSED,),
    STATUS_EVALUATED: (),
    STATUS_REJECTED: (),
}

# 状态机里这两条边只能由专用函数走，set_status() 直接拒（见 set_status 的 docstring）
STATUS_ONLY_VIA_HELPER = (STATUS_EXECUTED, STATUS_EVALUATED)

# 决策作用域四值：**跨度词，不是频率词**（Fix A，2026-10-07 设计席定案）。
# 改前这里是 daily / weekly / long_term / critical——那是"多久动一次"的口径，而这一格
# 真正要表达的是**这条决策的影响跨度有多大**。把频率当跨度会让两类东西混进同一格：
# 「每周跑一次复盘」是频率，「这一周的运营要换个做法」才是跨度为 operational 的决策。
# 改名对照（生产库 decisions 当前 0 行，所以这是"改常量"而不是"迁数据"）：
#   daily → tactical ／ weekly → operational ／ long_term → strategic ／ critical 不变。
# 它是 4b 注入筛选的主键，不是给人看的标签。
SCOPE_TACTICAL = "tactical"        # 只影响这几天的当下调整
SCOPE_OPERATIONAL = "operational"  # 一周内要落地的改法（复盘产物的默认档）
SCOPE_STRATEGIC = "strategic"      # 跨周以上的项目走向
SCOPE_CRITICAL = "critical"        # 不可逆、或撞上硬时点
VALID_DECISION_SCOPES = (
    SCOPE_TACTICAL, SCOPE_OPERATIONAL, SCOPE_STRATEGIC, SCOPE_CRITICAL,
)

# 哪些跨度的决策值得回流成经验（4a 的筛选口径）。写成 frozenset 只是防"顺手 add 一档"，
# 成员与定案原文一致（frozenset 与 set 的 == 与 in 都等价）。
EXPERIENCE_ELIGIBLE_SCOPES = frozenset(
    {SCOPE_OPERATIONAL, SCOPE_STRATEGIC, SCOPE_CRITICAL})

# ⚠️ 原先这里有一张 `SCOPE_ORDER` 秩表（daily 0 / weekly 1 / long_term 2 / critical 3），
# 供 `list_decisions(min_scope=...)` 做「decision_scope >= weekly」的**下限比较**，
# **Fix A 已把它连同 min_scope 一起删掉**。删它不是因为不好用，是因为那根序不存在：
# 这四个值是"跨度"与"可逆性"两根轴上的东西（tactical 和 critical 谁大？无法回答），
# 借一个全序来表达"这几档我都要"，会让**新增一档变成一次坐标系改动**而不是加一个集合成员，
# 而 4a／4b 真正需要的从来只是集合成员判断。要放宽或收紧这一格，改
# `EXPERIENCE_ELIGIBLE_SCOPES` 这一个集合并同批改文档。

# 条目五类（拆分文档改动 2 定案）。**inference 与 assumption 的区别只在于有没有 Fact 支撑**，
# 把这条判据写在代码里是因为它只写在文档里的时候，下一个人一定会把没证据的前提登记成
# inference——那样它就永远不会被拿去验证。
KIND_FACT = "fact"                 # 可复验的观测，source 记表／文件／命令
KIND_USER_REPORT = "user_report"   # 他本人说过的话，source 记出处
KIND_INFERENCE = "inference"       # 有事实支撑的推断，source 记支撑它的那条 fact
KIND_ASSUMPTION = "assumption"     # 没有证据、但当前必须先采信的前提
KIND_UNKNOWN = "unknown"           # 明确承认不知道（Phase 2「找不到来源就登记、不要编」落这里）
VALID_ITEM_KINDS = (
    KIND_FACT, KIND_USER_REPORT, KIND_INFERENCE, KIND_ASSUMPTION, KIND_UNKNOWN,
)

# 修订条目：不在五类里，它记的是"把哪一条改成什么"。
# **原条目不删**——删掉就让"我当时以为是什么"失去痕迹，而第一闭环要评的恰恰是这件事。
ITEM_KIND_REVISION = "revision"
ALL_ITEM_KINDS = VALID_ITEM_KINDS + (ITEM_KIND_REVISION,)

# 复盘结论五值（拆分文档 P4 定案）：
#   helpful＝帮上了 / neutral＝说不上帮也没碍事 / harmful＝把事带偏了
#   wrong_assumption＝当初先采信的那条前提被证伪（这一格是闭环的牙齿：它要求我们
#     **允许承认自己错**，且写它不需要改动任何别的状态）
#   insufficient＝没攒够能判断的结果，先不下结论
EVAL_HELPFUL = "helpful"
EVAL_NEUTRAL = "neutral"
EVAL_HARMFUL = "harmful"
EVAL_WRONG_ASSUMPTION = "wrong_assumption"
EVAL_INSUFFICIENT = "insufficient"
VALID_EVALUATIONS = (
    EVAL_HELPFUL, EVAL_NEUTRAL, EVAL_HARMFUL, EVAL_WRONG_ASSUMPTION,
    EVAL_INSUFFICIENT,
)


def _utc_now_text() -> str:
    """当前时刻的 UTC 定宽字符串（带 +00:00），与 template_runs／scripts.boot_compensate
    同一个写法：定宽＋固定偏移 ⇒ 字符串序就是时间序，能直接比大小。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row) -> Dict[str, Any]:
    """sqlite3.Row → dict（get_db() 已挂 row_factory，这里只做形态转换）"""
    return dict(row)


def _fetch_decision_row(conn, decision_id: int):
    """内部读取：按 id 取一行 decisions，没有返回 None（不抛，交给调用方定性）"""
    return conn.execute(
        "SELECT * FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()


def create_decision(
    title: str,
    goal_id: Optional[str] = None,
    summary: Optional[str] = None,
    decision_scope: str = SCOPE_OPERATIONAL,
) -> int:
    """新建一条决策（status=proposed），返回新行 id

    Args:
        title: 一句话的决策名（必填、非空）
        goal_id: 关联的目标 id；None＝不绑目标的决策。这类决策在 4b 的
                 「经 decisions.goal_id 找候选经验」里天然拿不到候选，属预期行为
        summary: 决策的展开说明（可空）
        decision_scope: tactical / operational / strategic / critical，默认 operational
                 （跨度词，不是频率词；哪些档能回流成经验看
                 `EXPERIENCE_ELIGIBLE_SCOPES`，这里**没有**"下限"那种写法）

    Raises:
        ValueError: title 为空或 decision_scope 不在四值里
    """
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("create_decision：title 不能为空——一条没有名字的决策无法被复盘")
    if decision_scope not in VALID_DECISION_SCOPES:
        raise ValueError(
            f"create_decision：decision_scope 非法值 {decision_scope!r}，"
            f"只接受 {VALID_DECISION_SCOPES}"
        )
    now = _utc_now_text()
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO decisions (goal_id, title, summary, status, decision_scope, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (goal_id, clean_title, summary, STATUS_PROPOSED, decision_scope, now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_decision(decision_id: int) -> Optional[Dict[str, Any]]:
    """按 id 读取一条决策；不存在返回 None（不抛：读取失败就是"没这一条"）"""
    conn = get_db()
    try:
        row = _fetch_decision_row(conn, decision_id)
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_decisions(
    status: Optional[str] = None,
    goal_id: Optional[str] = None,
    decision_scope: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """按条件列出决策（参数全为 None＝列全部），按 id 倒序返回

    Args:
        status: 只取某个状态；None＝不限
        goal_id: 只取绑了某个目标的；None＝不限
        decision_scope: 只取恰好等于这个跨度的；None＝不限

    ⚠️ 这里**没有** `min_scope`／"下限"那种参数（Fix A，2026-10-07 删）。四值之间没有
    全序（跨度与可逆性是两根轴），"哪几档我要"应当由调用方拿集合来表达：
    `row["decision_scope"] in ds.EXPERIENCE_ELIGIBLE_SCOPES`。留一根假的序在库里，
    下一个人就会去维护那个坐标系。
    """
    if status is not None and status not in VALID_DECISION_STATUSES:
        raise ValueError(
            f"list_decisions：status 非法值 {status!r}，只接受 {VALID_DECISION_STATUSES}"
        )
    clauses: List[str] = []
    params: List[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if goal_id is not None:
        clauses.append("goal_id = ?")
        params.append(goal_id)
    if decision_scope is not None:
        if decision_scope not in VALID_DECISION_SCOPES:
            raise ValueError(
                f"list_decisions：decision_scope 非法值 {decision_scope!r}，"
                f"只接受 {VALID_DECISION_SCOPES}"
            )
        clauses.append("decision_scope = ?")
        params.append(decision_scope)
    sql = "SELECT * FROM decisions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    conn = get_db()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def add_item(
    decision_id: int,
    kind: str,
    content: str,
    source: Optional[str] = None,
) -> int:
    """给一条决策追加一个条目（五类之一，或 kind='revision'），返回新条目 id

    Args:
        decision_id: 必须已经存在
        kind: fact / user_report / inference / assumption / unknown / revision
        content: 条目正文（必填、非空）
        source: 出处。五类的出处要求不同（fact 记表／文件／命令，inference 记支撑它的
                那条 fact，assumption 通常为空——**为空正是"没有证据"的形状**），
                本函数不强制 source 的有无：强制会诱导编造出处，而编造的出处比空着更坏。

    Raises:
        ValueError: kind 不在六值里、content 为空、或 decision_id 不存在。
            第三种情况必须拒而不是留一行孤儿：SQLite 默认不启用外键约束，
            这条边只能由写路径守（见 database.py 里 decision_items 的建表注释）。
    """
    if kind not in ALL_ITEM_KINDS:
        raise ValueError(
            f"add_item：kind 非法值 {kind!r}，只接受 {ALL_ITEM_KINDS}"
        )
    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("add_item：content 不能为空——空条目会占掉一个 kind 的名额")
    conn = get_db()
    try:
        if _fetch_decision_row(conn, decision_id) is None:
            raise ValueError(
                f"add_item：decision_id={decision_id} 不存在，拒绝写入孤儿条目"
            )
        cursor = conn.execute(
            "INSERT INTO decision_items (decision_id, kind, content, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (decision_id, kind, clean_content, source, _utc_now_text()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def list_items(
    decision_id: int,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """读取一条决策的全部条目（按 id 正序＝登记顺序）；kind 给定时只取那一类

    读取端为什么要有公开函数：Phase 3b 的 View 与 3c 的界面都要渲染条目。
    不给出入口的话，它们只能各自写裸 SQL 打这张表——读写边界就又散了。
    """
    conn = get_db()
    try:
        if kind is None:
            rows = conn.execute(
                "SELECT * FROM decision_items WHERE decision_id = ? ORDER BY id ASC",
                (decision_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decision_items WHERE decision_id = ? AND kind = ? "
                "ORDER BY id ASC",
                (decision_id, kind),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def count_revisions(decision_id: int) -> int:
    """这条决策被修订过几次（kind='revision' 的条目数）

    拆分文档改动 3 里"修订不进状态机、走一条 kind='revision' ＋计数"的**那半句计数**。
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM decision_items "
            "WHERE decision_id = ? AND kind = ?",
            (decision_id, ITEM_KIND_REVISION),
        ).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


def set_status(decision_id: int, status: str) -> bool:
    """按状态机推进一条决策；返回是否更新到行（决策不存在返回 False）

    守卫三条，每条都是负控用例的对象（摘掉任意一条，对应用例必须变红）：
    1. status 必须在七值里；
    2. **executed / evaluated 只能由 mark_executed() / evaluate() 走**——
       它们各自带着必填的配套列（execution_reference、evaluation＋actual_result），
       允许 set_status 直接置这两个态，就会长出"状态说做完了、配套列全空"的行；
    3. 当前态 → 目标态必须在 LEGAL_TRANSITIONS 的边上；**同态重复设置也算非法**，
       因为一次不改任何东西的写照样会把 updated_at 推到当下——
       "这条决策什么时候被推进过"从此读不准，而那个时间点是复盘的输入。

    Raises:
        ValueError: 上面三条任一被违反（消息里带上当前态与目标态，便于定位）
    """
    if status not in VALID_DECISION_STATUSES:
        raise ValueError(
            f"set_status：status 非法值 {status!r}，只接受 {VALID_DECISION_STATUSES}"
        )
    if status in STATUS_ONLY_VIA_HELPER:
        raise ValueError(
            f"set_status：{status} 不能由 set_status 置，请走 "
            f"{'mark_executed()' if status == STATUS_EXECUTED else 'evaluate()'}——"
            "这两个态各自带必须写入的配套列，绕过去会留下状态与内容矛盾的账"
        )
    conn = get_db()
    try:
        row = _fetch_decision_row(conn, decision_id)
        if row is None:
            return False
        current = str(row["status"])
        if status not in LEGAL_TRANSITIONS.get(current, ()):
            raise ValueError(
                f"set_status：非法转换 {current} → {status}（决策 {decision_id}）。"
                f"{current} 的合法去向：{LEGAL_TRANSITIONS.get(current, ())}"
            )
        conn.execute(
            "UPDATE decisions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now_text(), decision_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def mark_executed(decision_id: int, execution_reference: Optional[str] = None) -> bool:
    """把一条 approved 的决策记为已执行，并写下执行痕迹；返回是否更新到行

    **这是纯 DB 写，不依赖任何内存对象**（拆分文档 1b 判据 ②／缺口 1 的正面证据）：
    它当前态是从库里读出来的，不是从某个调用方持有的对象里读的。所以隔一个进程、
    隔一次重启，只要拿到 decision_id 就能把这一步补上——测试里用一个子进程真调一次。

    只有 approved → executed 这一条边能进来（proposed／discussed 直接执行＝跳过批准，
    与"最终决策权在用户"的辅助型自主原则冲突）。

    Args:
        execution_reference: 这次执行留下的痕迹（笔记路径／任务 id／模板运行 id 都行）。
            允许为 None：执行了但没找到可指的东西，比编一个路径出来更诚实。
    """
    conn = get_db()
    try:
        row = _fetch_decision_row(conn, decision_id)
        if row is None:
            return False
        current = str(row["status"])
        if current != STATUS_APPROVED:
            raise ValueError(
                f"mark_executed：决策 {decision_id} 当前是 {current}，"
                f"只有 {STATUS_APPROVED} 才能记为已执行（未经批准不能执行）"
            )
        conn.execute(
            "UPDATE decisions SET status = ?, execution_reference = ?, updated_at = ? "
            "WHERE id = ?",
            (STATUS_EXECUTED, execution_reference, _utc_now_text(), decision_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def evaluate(decision_id: int, evaluation: str, actual_result: str) -> bool:
    """评一次已执行的决策：置 evaluated 并同时写 evaluation 与 actual_result

    四道守卫（对应拆分文档 4a 的判据，Phase 1b 先把写路径立起来，4a 只加经验回写）：
    1. evaluation 必须在五值里；
    2. **actual_result 必须非空**——evaluated 态的定义就是"evaluation 与
       actual_result 同时非空"（database.py 的建表注释里说明了这条为什么不在 DDL 里：
       SQLite 的 CHECK 看不见另一列此前的历史，拒不掉"先置 evaluated 再把
       actual_result 更新成空串"那条路径；这里也不给任何把 actual_result 清空的入口）；
    3. 当前态必须是 executed（没执行过的决策没有"效果"可评）；
    4. 决策不存在返回 False。

    wrong_assumption 这一格在这里写得出来，不需要任何额外条件——它是五值里的普通一值。
    """
    if evaluation not in VALID_EVALUATIONS:
        raise ValueError(
            f"evaluate：evaluation 非法值 {evaluation!r}，只接受 {VALID_EVALUATIONS}"
        )
    clean_result = str(actual_result or "").strip()
    if not clean_result:
        raise ValueError(
            "evaluate：actual_result 不能为空——evaluated 态要求它与 evaluation 同时存在"
        )
    conn = get_db()
    try:
        row = _fetch_decision_row(conn, decision_id)
        if row is None:
            return False
        current = str(row["status"])
        if current != STATUS_EXECUTED:
            raise ValueError(
                f"evaluate：决策 {decision_id} 当前是 {current}，"
                f"只有 {STATUS_EXECUTED} 才能被评（没执行过的决策没有效果可评）"
            )
        conn.execute(
            "UPDATE decisions SET status = ?, evaluation = ?, actual_result = ?, "
            "updated_at = ? WHERE id = ?",
            (STATUS_EVALUATED, evaluation, clean_result, _utc_now_text(), decision_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ---------- Phase 4a：把评完的决策提炼成经验（写点与守卫） ----------

def link_experience(
    decision_id: int,
    future_effect: str,
    scenario: Optional[str] = None,
    conditions: Optional[str] = None,
) -> int:
    """把一条**已评估**的决策提炼成一行 `experiences`，返回新行 id（Phase 4a 的回流写点）

    定案出处：`DESIGN_ANCHORS.md` §一.8（链接是**单向**的）与 §一.9（落列对照表）。
    落列逐条照那张表，本函数**不新写句子**：

    | `experiences` 的列 | 内容从哪来 |
    |---|---|
    | `scenario` | 调用方给的场景；不给就**抄决策自己的 `title`**（那本来就是它的上下文） |
    | `effect`   | **从库里读**该决策的 `actual_result`——不接参数，免得调用方重打一遍、两列还长得不一样 |
    | `future_effect` | 参数，「下次改什么」的那句教训（4b 只注入这一列） |
    | `decision_id`   | 参数，那条决策的 id（单向链接） |
    | `strategy` | **显式空串 `""`**：它是 v3.7.1 对话反思的 legacy 字段，与 `future_effect` 来源不同，不合并、不重命名、不互相回填。该列 DDL 是 `TEXT NOT NULL`、参数又没有默认值 ⇒「不写它」的唯一落法是传空串（空串与真策略可区分，不污染） |
    | `source_session_id` | 恒为 None：这条经验的来路记在 `decision_id` 那一列，不占用会话 id 的语义 |

    四道守卫（每道都是负控用例的对象，摘掉任意一条对应用例必须变红）：
    1. **决策必须存在**——写一条指向幽灵决策的经验，账本上永远读不回它是谁；
    2. **当前态必须是 `evaluated`**——写点定在 `evaluate()` **之后**（先被评、再提炼），
       没评估过的决策没有「效果」可提炼，硬写会把猜测当教训喂给 4b；
    3. **`decision_scope` 必须在 `EXPERIENCE_ELIGIBLE_SCOPES` 里**——这是 4a 的筛选口径；
       `tactical` 结构性地回不了流（要放宽就改那一个集合，别在这里加特例）；
    4. **`future_effect` 必须非空**——4b 只读这一列，空教训回流进闭环等于往每轮
       system prompt 注入一句空气。

    一个决策可以提炼出多条经验（一条经验只有一条来路，反过来不成立），所以**没有**
    "这条决策已经提炼过就不再提炼"那种唯一性约束。

    Raises:
        ValueError: 上面四道守卫任一被违反
    返回值:
        int: 新写的 `experiences.id`
    """
    clean_lesson = str(future_effect or "").strip()
    if not clean_lesson:
        raise ValueError(
            "link_experience：future_effect 不能为空——4b 只注入这一列，"
            "空教训回流进闭环等于往每轮 system prompt 注入一句空气"
        )
    conn = get_db()
    try:
        row = _fetch_decision_row(conn, decision_id)
        if row is None:
            raise ValueError(
                f"link_experience：decision_id={decision_id} 不存在，"
                "拒绝写下指向幽灵决策的经验"
            )
        current = str(row["status"])
        if current != STATUS_EVALUATED:
            raise ValueError(
                f"link_experience：决策 {decision_id} 当前是 {current}，"
                f"只有 {STATUS_EVALUATED} 才能被提炼成经验——写点定在 evaluate() 之后，"
                "先被评、再提炼"
            )
        scope = str(row["decision_scope"])
        if scope not in EXPERIENCE_ELIGIBLE_SCOPES:
            raise ValueError(
                f"link_experience：决策 {decision_id} 的 decision_scope={scope} "
                f"不在回流集合 {sorted(EXPERIENCE_ELIGIBLE_SCOPES)} 里；"
                "要放宽请改那个集合本身，不要在这里加特例"
            )
        clean_scenario = str(scenario or "").strip() or str(row["title"] or "").strip()
        if not clean_scenario:
            raise ValueError(
                f"link_experience：决策 {decision_id} 的 title 为空且没给 scenario，"
                "而 experiences.scenario 是 NOT NULL——宁可响，不静默写一个占位场景"
            )
        cursor = conn.execute(
            "INSERT INTO experiences (scenario, strategy, effect, conditions, "
            "source_session_id, future_effect, decision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clean_scenario, "", row["actual_result"], conditions,
             None, clean_lesson, int(decision_id)),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()
