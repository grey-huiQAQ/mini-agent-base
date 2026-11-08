# -*- coding: utf-8 -*-
"""
Project State - 决策账本的输入层（P7 第一闭环 Phase 2，2026-10-06，纯新增叶子模块）

【这个模块是倒着写的，顺序本身就是它的契约】
拆分文档改动 4 明确禁止「打开 scripts/snapshot_state.py 看有什么可采，然后照着采」。
本文件按下面三步落成常量，读代码的人可以逐条核对顺序没有反过来：

  第 1 步（DECISION_INPUT_WHY）：先写下「一条 Decision 需要哪些输入、谁消费它」；
  第 2 步（AUTO_COLLECTORS）：再为每一项输入找数据来源，**找到的才进自动段**；
  第 3 步（unknowns）：找不到来源的一律登记成「这一项 State 没能为这个字段提供输入＋原因」，
       **不要编**。所以 unknowns 不是错误列表，是这个模块诚实性的证据。

【职责】
- 采集：8 项自动采集（goals / goal_tasks / test_runs / 债务台账 / agent_resource_log /
  pending_approvals / notes / 上一条 decisions），每项返回
  `(decision_field, value, source, evidence)` 四元组——指不出 decision_field 的项
  **不允许出现在自动段里**；
  → 第四位 `evidence` 是阶段一（2026-10-11）加的：它声明**这条读数算不算「项目活动的证据」**。
    加在它而不是让复盘侧去猜，理由有两条：① 只有生产者知道自己读的是哪一族表，复盘侧
    要判就得解析 `goal_tasks[total=0]` 这种字符串——两处手工保持一致在本仓库咬过两次；
    ② 历史快照没有这一格，`.get()` 读出来是 None，「不知道」与「不算证据」必须是两种读法，
    写在数据里才分得开。
- 收：人工段（user_report / inference / assumption）与 Unknown 的接收入口；
- 落：`project_states` 表**追加**一行（一次快照一行，永不 UPDATE 历史行）。

【不做什么】
- **不采任何模型生成的「项目目前状态」描述**。自动段的 value 一律是 `k=v` 形态的计数与
  标识符，不带形容词、不带句子——判据原文是「自动段里出现自然语言总结句，即为失败」；
- 不做推断：`inference` 要有 Fact 支撑并写明支撑关系，那是 Phase 3b 或人工入口的事；
- 不写既有表：goals / notes / pending_approvals / test_runs … 全部只读（「冻结」纪律）；
- 不 import 除 `memory.database.get_db` 与 `core.decision_store` 的 kind 常量之外的任何东西。

【时间轴（C9 家族，这里必须说清，因为本模块同时碰四根轴）】
- `project_states.snapshot_at` ＝ **UTC 且带 +00:00**（与 decisions / template_runs 同一处置）；
- `notes` / `goal_tasks` / `agent_resource_log` / `pending_approvals` 的 `created_at` ＝
  `DEFAULT CURRENT_TIMESTAMP` ＝ **UTC 但没有偏移**。近 N 天窗口的边界一律用
  `datetime('now', '-N days')` 在 SQL 里算，**左右两边同轴**，绝不在 Python 里换算；
- `test_runs.started_at` ＝ **本地时间且没有偏移**（`core/test_runner.py:305` 用
  `datetime.now()`）。所以本模块对 test_runs **只按 id 取行、不做任何时间比较**；
- `goals.goal_id` 里嵌的那串数字（如 `goal_20260927102535_…`）＝ **本地时间**。
  实测（2026-10-06，只读取生产库）：同一条 goals 行的 created_at 比它早 8 小时，
  正是 UTC 与本地两根轴的差。

【错误语义（与 core.decision_store 刻意不同，理由是两种数据的性质不同）】
- 采集层读不到（表不存在、库为空、台账文件缺失、SQL 报错）→ **登记成 unknown，不抛异常**。
  快照是给人看的读数，抛异常会让整份快照消失，而「这一项没读到」本身就是要告诉人的信息；
- 写路径的规则违规（fact 没有 source、kind 不在五类里、content 为空、items 里出现
  指不出 decision_field 的项）→ **raise ValueError**。这类数据连「合法快照」都算不上，
  静默写进去会留下一份看起来完整、实际是编的 State，而第一闭环评的就是这种假账。
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.decision_store import VALID_ITEM_KINDS
from memory.database import get_db

# 项目根目录（本文件位于 core/ 下）：台账文件按根目录解析，
# 这样在 git worktree 的净工作区里跑也读到自己那一份，不会串到别的项目根去。
_ROOT = Path(__file__).resolve().parent.parent
_CHECKS_DIR = _ROOT / "checks"
_BASELINE_FILE = _CHECKS_DIR / "debt_baseline.json"

# ---------- 第 1 步：一条 Decision 需要哪些输入 ----------

# 决策行的四个自有字段
DECISION_FIELD_GOAL_ID = "goal_id"
DECISION_FIELD_TITLE = "title"
DECISION_FIELD_SUMMARY = "summary"
DECISION_FIELD_SCOPE = "decision_scope"
# 决策条目：五类各是一个「字段」，因为 Phase 3b 是按类取用的
DECISION_ITEM_FIELDS = tuple("items." + kind for kind in VALID_ITEM_KINDS)

DECISION_FIELDS: Tuple[str, ...] = (
    DECISION_FIELD_GOAL_ID, DECISION_FIELD_TITLE, DECISION_FIELD_SUMMARY,
    DECISION_FIELD_SCOPE,
) + DECISION_ITEM_FIELDS

# 每个字段谁消费。**这张表是「顺序反转」的第 1 步成品**：先有它，才允许有采集器。
# 写在这里而不是只写在文档里，是因为 tests/test_project_state.py 要逐条核对
# 「每条采集项指名的字段都在这张表里」——文档里的清单核对不了，代码里的清单可以。
DECISION_INPUT_WHY: Dict[str, str] = {
    DECISION_FIELD_GOAL_ID:
        "4b 经 decisions.goal_id 找候选经验；NULL 的决策在 4b 里天然拿不到候选",
    DECISION_FIELD_TITLE:
        "3b 的 Human View 首行；它是对事实集合的判断结果，不是事实，故采集层不给文本",
    DECISION_FIELD_SUMMARY:
        "3b 的正文（问题 / 建议下一步 / 需要的能力）；同上，采集层不给",
    DECISION_FIELD_SCOPE:
        "4a／4b 的经验回流筛选（成员判断，看 `decision_store.EXPERIENCE_ELIGIBLE_SCOPES`；"
        "这里没有「下限」那种写法）；定跨度要依据上一条决策的跨度与间隔",
    "items.fact":
        "3b 的事实底座，也是 4a 评 wrong_assumption 时的对照物；每条必须能追到表或文件",
    "items.user_report":
        "3b 里「他本人说过」的那一侧；只有人工段能填",
    "items.inference":
        "3b 的结论草稿；必须写明支撑它的那条 fact，采集层不做推断",
    "items.assumption":
        "4b 优先注入的就是它（未验证的 Assumption）；只有人工段能填，采集层不许代填",
    "items.unknown":
        "3b 走 insufficient_information 路径的依据；本模块的 unknowns 列表就是它的原料",
}

# 近 N 天窗口。7 天与「每周一次复盘」的节奏对齐（那是设计席的定案，不是本模块的证据）。
RECENT_WINDOW_DAYS = 7

# ---------- 第 3 步的一部分：自动段结构上提供不了的字段 ----------
# 这些不是「读库失败」，是**这一层没有这种东西可读**。逐条写明，是为了下一个人不会
# 把空着的那格当成缺数据去补一个模型生成的总结句——那正是改动 4 要防的入口。
AUTO_UNSERVABLE: Tuple[Tuple[str, str], ...] = (
    (DECISION_FIELD_TITLE,
     "自动段不产标题：标题是对事实的判断，写出来就是「模型生成的项目状态描述」的入口"),
    (DECISION_FIELD_SUMMARY,
     "自动段不产正文：同上，正文由 Phase 3b 从 items 与 unknowns 生成"),
    ("items.user_report",
     "自动段读不出「他说过什么」：库里没有一句话是为此而写的，需人工段传入"),
    ("items.inference",
     "自动段不做推断：推断要有 fact 支撑并写明支撑关系，那属 Phase 3b 或人工段"),
    ("items.assumption",
     "自动段不许先采信任何前提：assumption 只能由人登记，之后要被拿去验证"),
)

# ---------- 第 2 步的第四位：evidence（这条读数算不算「项目活动的证据」）----------
#
# 定案来源：设计席阶段一裁定「实质口径只认项目活动计数（goal_tasks／notes／decisions 非零；
# 仓库卫生类与 llm_calls／pending_approvals 不算）」。这三族由**读它们的采集器自己声明**，
# 消费方是 `core.long_term_project_review.has_substantive_evidence`（复盘闸门②）。
# ⚠️ 三族各自把读数判成 `activity` 的**尺子并不相同**，逐条以采集器 docstring 与
# `DESIGN_ANCHORS.md` §一.10 为准；`notes` 一条 2026-10-11 由设计席裁决收窄成
# 「只认窗口内新增」，不认全表存量。
#
# 为什么不写成「任何非零计数都算实质」（本席评估过并排除的两个候选口径，留档免得重来）：
# - 「非零即实质」：空库上 `exemptions[duplicate_name=15]` 与 `debt_baseline_total=22` 都非零
#   ⇒ 空库也算「有实质依据」，清单要的「询问方向／引导建立上下文」两个分支照样不可达；
# - 「人工段非空即实质」：与闸门①同条件 ⇒ 闸门②空转。
# 所以这三族之外的读数一律标 context：它们说明「仓库是什么状况」，不说明「项目在往前推进」。
EVIDENCE_ACTIVITY = "activity"                  # 这一族读到非零的项目活动
EVIDENCE_ACTIVITY_EMPTY = "activity_empty"      # 同一族读到了，但计数为零（不算证据）
EVIDENCE_CONTEXT = "context"                    # 上下文／仓库卫生类读数（不参与实质判断）
FACT_EVIDENCE_VALUES = (EVIDENCE_ACTIVITY, EVIDENCE_ACTIVITY_EMPTY, EVIDENCE_CONTEXT)

# ---------- unknown 的机器可读码（阶段一新增，只在「选不出目标」那两分支给）----------
# 复盘要区分「一个 active 目标都没有」与「有多个、采集层不替人挑」——两个形状给人的下一步
# 完全不同（前者要引导建立上下文，后者只要先选一个）。判据如果去解析 reason 里那句中文，
# 就变成「两处手工保持一致」（改文案即静默分叉），所以把这一格写成码，跟着 unknown 一起落库。
UNKNOWN_CODE_GOAL_NONE = "goal_none"
UNKNOWN_CODE_GOAL_AMBIGUOUS = "goal_ambiguous"
UNKNOWN_CODES = (UNKNOWN_CODE_GOAL_NONE, UNKNOWN_CODE_GOAL_AMBIGUOUS)

# ---------- CAPABILITY_MAP：能力闭集（定案来源 `DESIGN_ANCHORS.md` §一.6；落地：拆分文档 Phase 2 内容 ③ ＋ Fix C）----------
# 「P5 的降级落点」在磁盘上没有对应文档（2026-10-06 全量 grep 无出处），本模块按下面
# 这条能执行的解释落地：Phase 3a 的旁路输出有一格是「需要的能力」，那一格**只能从这张
# 表里选**；选不出来就写 UNKNOWN_CAPABILITY，**不许造一个能力名**。
# 模型不可用时同理：3a 直接输出「无法判断」，而不是编一条能力清单。
#
# **Fix C 改掉了两件事**（改前这张表的键是模块名：note_store / rag_search / db_backup……）：
#   ① 键改成**能力的语义名**（动词开头，说「做得到什么」）。拿模块名当词表，模型只能在
#      「哪些文件存在」里挑，而它这一格要回答的是「这件事得靠什么能力」；而且词表的粒度
#      从此被目录结构决定——重构一次改一次名。
#   ② `carrier` 由**单路径改列表**：一个语义能力天然由多个模块共同承载，单路径会把
#      「还由谁做」这条信息直接丢掉。列表里每一条都由 tests/test_project_state.py 逐条
#      验存在——一张指向已消失模块的能力表就是假账。
#
# ✅ **下面这张表的键名已与设计席锚点对账通过（2026-10-07）**：`DESIGN_ANCHORS.md` §一.6 是
# 唯一来源，键名与这里逐字、逐序一致。**出处坐标按 2026-10-07 定案写成「符号名（文件）」**：
# 锚点 §一.6 指 `CAPABILITY_MAP`（core/project_state.py），不带行号——上一轮那句「锚点标的
# 出处 core/project_state.py:146 当场复跑确认」已随 F 作废（它当时为守住那一个行号，把这段
# 注释硬约束成等行数替换；靠保持行数维持的引用本身就脆弱，这正是 F 的理由）。
# 此前这一块写的是「键名系执行席推导、待设计席确认」——那份对照表当时在磁盘上确实零副本
# （§五.54），于是按两条硬约束落地：**键数与锚点一致**、**改前每条 carrier 路径一条不丢**。
# 分工从此固定：**键名以 `DESIGN_ANCHORS.md` 为准**（要改名先改锚点、再改这里，不拿哪个
# 窗口的对话当依据），**`carrier` 列表以本文件为准**、由 tests/test_project_state.py 逐条
# 验存在。下面每个键仍注着它由哪些旧键归并而来——那是沿革说明，不是定案来源。
#
# carrier 一律写仓库相对路径（POSIX 形态、不写行号）。
UNKNOWN_CAPABILITY = "unknown"

CAPABILITY_MAP: Dict[str, Dict[str, Any]] = {
    # ← 旧键 rag_search
    "recall_local_material": {
        "label": "检索本地已存资料（RAG）",
        "carrier": ["rag/retriever.py"],
    },
    # ← 旧键 note_store
    "read_write_notes": {
        "label": "读写目标笔记",
        "carrier": ["memory/note_store.py"],
    },
    # ← 旧键 deep_search ＋ resource_scout（两条合并成一个语义能力：都是「向外找东西」）
    "find_external_information": {
        "label": "向外检索信息与机会（深度搜索、资源发现）",
        "carrier": ["core/information_engine", "core/resource_scout.py"],
    },
    # ← 旧键 eligibility_filter
    "filter_by_explicit_rules": {
        "label": "按显式规则筛资格与相关性",
        "carrier": ["core/ai_filter.py"],
    },
    # ← 旧键 scheduled_template（多带一个执行侧 carrier：模板「到点」与「跑起来」是两模块）
    "run_scheduled_tasks": {
        "label": "按计划执行任务并记账",
        "carrier": ["core/task_template.py", "agents/task_executor.py"],
    },
    # ← 旧键 approval_queue
    "queue_for_approval": {
        "label": "待审批队列（重启可恢复）",
        "carrier": ["memory/approval_store.py"],
    },
    # ← 旧键 experience_store ＋ reflection（沉淀与召回是同一种能力的两面）
    "distill_experience": {
        "label": "沉淀经验并在之后召回",
        "carrier": ["memory/experience_store.py", "agents/reflection_agent.py"],
    },
    # ← 旧键 decision_ledger ＋ project_state_snapshot（「记项目的账」：决策一行、快照一行）
    "keep_project_ledger": {
        "label": "记项目的账（决策账本与状态快照）",
        "carrier": ["core/decision_store.py", "core/project_state.py"],
    },
    # ← 旧键 context_injection
    "inject_context": {
        "label": "把经验注入每轮对话的上下文",
        "carrier": ["core/context_manager.py"],
    },
    # ← 旧键 skill_extension ＋ plugin_extension（同一机制的两个目录）
    "extend_with_skills": {
        "label": "用 drop-in 目录扩能力（技能与插件）",
        "carrier": ["core/skill_manager.py", "core/plugin_manager.py"],
    },
    # ← 旧键 test_regression ＋ db_backup（同一件事的两半：验证得出来、出事退得回去）
    #    scripts/snapshot_state.py 是 Fix C 之后新挂上的第三个 carrier（台账快照）。
    "verify_and_rollback": {
        "label": "验证与回滚（全量回归、库快照、台账快照）",
        "carrier": ["core/test_runner.py", "core/db_backup.py",
                    "scripts/snapshot_state.py"],
    },
    # ← 旧键 llm_chat
    "talk_to_model": {
        "label": "与模型对话（同步与流式）",
        "carrier": ["business/model_client.py"],
    },
}


def _utc_now_text() -> str:
    """快照时间：UTC 且带 +00:00 偏移（口径见模块 docstring 的时间轴一节）"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 采集结果的内部表示 ----------
# 四元组 (decision_field, value, source, evidence)；unknown 是三元组 (decision_field, reason, code)。
ItemTriple = Tuple[str, str, str, str]
UnknownPair = Tuple[str, str, Optional[str]]


def _kv(pairs: List[Tuple[str, Any]]) -> str:
    """把采集值拼成 `k=v, k=v` 形态（键序固定，不含任何句子）。

    刻意不用自然语言：判据写的是「自动段里出现自然语言总结句，即为失败」，
    而 `k=v` 形态既好核对，也让下一个人没法顺手往里加形容词。
    """
    return ", ".join(f"{k}={v}" for k, v in pairs)


def _unknown(field: str, reason: str, code: Optional[str] = None) -> UnknownPair:
    """一条 unknown。`code` 只有"选不出目标"那两分支会给（阶段一新增），其余留 None——
    复盘要按码判形状的那几格本来就只有这两处，给不出的地方硬编一个码就是假账。"""
    return (field, reason, code)


def _table_missing(err: sqlite3.Error) -> bool:
    """区分「表不存在／库是空的」与别的 SQL 错误：两种都要如实写进原因里"""
    return "no such table" in str(err).lower()


# ---------- 第 2 步：8 项自动采集（全部只读）----------

def _collect_goal(conn, goal_id: Optional[str]) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """①goals：选得出唯一 active 目标才给值，否则登记 unknown（不猜）

    这一条 fact 的 evidence 恒为 `EVIDENCE_CONTEXT`：goal_id 说「这份快照关于谁」，
    它本身不证明项目在往前推进（一个刚建出来、什么都没动的目标也是 active）。
    两个「选不出目标」的分支各带一个码，复盘按码分流，不去解析下面那句中文。
    """
    if goal_id:
        # 来源如实写成「没核对过」：调用方给的东西采集层不负责验证它是否存在，
        # 把它写成「goals 表读出来的」会让下游以为这条 fact 有过一道存在性检查。
        return [(DECISION_FIELD_GOAL_ID, goal_id,
                 "调用方显式指定，采集层未在 goals 表里核对过这一条",
                 EVIDENCE_CONTEXT)], []
    try:
        rows = conn.execute(
            "SELECT goal_id FROM goals WHERE status = 'active' ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.Error as e:
        return [], [_unknown(DECISION_FIELD_GOAL_ID, f"goals 表读不出来：{e}")]
    ids = [r["goal_id"] for r in rows]
    if len(ids) == 1:
        return [(DECISION_FIELD_GOAL_ID, ids[0], "goals 表（WHERE status='active'）",
                 EVIDENCE_CONTEXT)], []
    if not ids:
        return [], [_unknown(
            DECISION_FIELD_GOAL_ID,
            "goals 表里没有 status='active' 的行，选不出目标（0 个不等于「无需目标」）",
            UNKNOWN_CODE_GOAL_NONE)]
    return [], [_unknown(
        DECISION_FIELD_GOAL_ID,
        f"active 目标有 {len(ids)} 个（{'，'.join(ids)}），采集层不替人挑一个；"
        "需调用方显式传 goal_id",
        UNKNOWN_CODE_GOAL_AMBIGUOUS)]


def _collect_goal_tasks(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """②goal_tasks：按 status 分组计数。表空是「读到且没有」，仍算 fact

    这一族是**项目活动证据**（阶段一拍板三族之一）：total 非零 ⇒ activity，为零 ⇒
    activity_empty。标 empty 而不是不标：读到了「没有任务」这件事本身是给复盘看的，
    但它不能充当「这轮有依据可判」的理由。
    """
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM goal_tasks GROUP BY status ORDER BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM goal_tasks").fetchone()["n"]
    except sqlite3.Error as e:
        return [], [_unknown("items.fact", f"goal_tasks 表读不出来：{e}")]
    pairs = [(r["status"] or "NULL", r["n"]) for r in rows]
    pairs.append(("total", total))
    evidence = EVIDENCE_ACTIVITY if total else EVIDENCE_ACTIVITY_EMPTY
    return [("items.fact", f"goal_tasks[{_kv(pairs)}]",
             "goal_tasks 表（GROUP BY status）", evidence)], []


def _collect_last_test_run(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """③test_runs：只按 id 取最新一行，并如实说明「这一行未必是全量」

    ⚠️ 反序采集在这里逼出一条真实缺口：test_runs 没有任何列能区分「全量回归」与
    「--files 子集跑」（见 PROJECT_STATUS.md §七 2026-10-06 的新条目与批次 14 的
    运行记录），所以「最近一次全量回归的结果」这一项**采集层读不出来**。
    按定案第 3 步：读不出来就登记 unknown，不拿最新一行冒充全量。
    """
    try:
        row = conn.execute(
            "SELECT id, status, total_files, passed_files, failed_files, timed_out_files, "
            "total_cases, passed_cases, duration_seconds "
            "FROM test_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        reason = ("test_runs 表不存在（从没跑过回归）"
                  if _table_missing(e) else f"test_runs 表读不出来：{e}")
        return [], [_unknown("items.fact", reason)]
    if row is None:
        return [], [_unknown("items.fact", "test_runs 表 0 行：从没记录过任何一轮测试运行")]
    value = "test_runs_last[" + _kv([
        ("id", row["id"]), ("status", row["status"]),
        ("total_files", row["total_files"]), ("passed_files", row["passed_files"]),
        ("failed_files", row["failed_files"]), ("timed_out_files", row["timed_out_files"]),
        ("total_cases", row["total_cases"]), ("passed_cases", row["passed_cases"]),
        ("duration_seconds", row["duration_seconds"]),
    ]) + "]"
    unknown = _unknown(
        "items.fact",
        "test_runs 无列区分「全量」与「--files 子集」，说不出哪一行是最近一次全量回归；"
        "本项只给 id 最大的一行，不拿它冒充全量")
    # evidence=context：跑过回归说明仓库被验过，不说明项目朝目标推进了（阶段一拍板排除的一族）
    return [("items.fact", value, "test_runs 表（ORDER BY id DESC LIMIT 1）",
             EVIDENCE_CONTEXT)], [unknown]


def _count_ledger_entries(path: Path) -> Optional[int]:
    """单本台账的条目数；读不到或形状不认识返回 None（由调用方登记 unknown）"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return len(data)


def _collect_debt_ledgers() -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """④债务台账：checks/*_exemptions.json 逐本条数 ＋ 棘轮基线合计

    这里**不 import scripts/snapshot_state.py**，也不照抄它的 LEDGER_FILES：
    那会在 core/（层序 0）与 scripts/（不在层序表内）之间新加一条边，而且「两处手工
    复制保持一致」这个项目已经咬过两次。改为按 `*_exemptions.json` 通配扫描——
    新加一本台账会自动被数到，不会留下「脚本数得到、State 数不到」的差。
    """
    try:
        paths = sorted(_CHECKS_DIR.glob("*_exemptions.json"))
    except OSError as e:
        return [], [_unknown("items.fact", f"checks/ 目录读不出来：{e}")]
    if not paths:
        return [], [_unknown("items.fact", "checks/ 下没有 *_exemptions.json：台账目录不在预期位置")]
    counts: List[Tuple[str, Any]] = []
    unreadable: List[str] = []
    for path in paths:
        n = _count_ledger_entries(path)
        if n is None:
            unreadable.append(path.name)
        else:
            counts.append((path.name[: -len("_exemptions.json")], n))
    baseline_total: Optional[int] = None
    try:
        with open(_BASELINE_FILE, "r", encoding="utf-8-sig") as f:
            baseline = json.load(f)
        ledgers = baseline.get("ledgers") if isinstance(baseline, dict) else None
        if isinstance(ledgers, dict):
            baseline_total = sum(int(v) for v in ledgers.values())
    except (OSError, ValueError, TypeError):
        baseline_total = None
    items: List[ItemTriple] = []
    unknowns: List[UnknownPair] = []
    # 台账与基线都是「仓库卫生」读数（阶段一拍板明确排除的一族）：它们非零不代表项目在推进，
    # 恰恰相反——台账条数是欠账，所以这一族永远是 context，永不参与实质判断。
    if counts:
        items.append(("items.fact", f"exemptions[{_kv(counts)}]",
                      "checks/*_exemptions.json（逐本数组长度）", EVIDENCE_CONTEXT))
    if baseline_total is not None:
        items.append(("items.fact", f"debt_baseline_total={baseline_total}",
                      "checks/debt_baseline.json（ledgers 求和）", EVIDENCE_CONTEXT))
    else:
        unknowns.append(_unknown("items.fact", "checks/debt_baseline.json 读不出来或形状不认识"))
    if unreadable:
        unknowns.append(_unknown(
            "items.fact",
            f"这些台账读不出来或不是数组：{'，'.join(unreadable)}"))
    return items, unknowns


def _collect_llm_calls(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """⑤agent_resource_log：近 N 天调用行数；**它没有成败列，所以"失败几次"读不出来**"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_resource_log "
            "WHERE created_at >= datetime('now', ?)",
            (f"-{RECENT_WINDOW_DAYS} days",),
        ).fetchone()
    except sqlite3.Error as e:
        reason = ("agent_resource_log 表不存在（从没记过埋点）"
                  if _table_missing(e) else f"agent_resource_log 表读不出来：{e}")
        return [], [_unknown("items.fact", reason)]
    items = [("items.fact",
              f"llm_calls_last{RECENT_WINDOW_DAYS}d[{_kv([('rows', row['n'])])}]",
              "agent_resource_log 表（created_at >= datetime('now','-7 days')）",
              EVIDENCE_CONTEXT)]  # 花过配额不等于推进了项目（阶段一拍板排除的一族）
    unknowns = [_unknown(
        "items.fact",
        f"近 {RECENT_WINDOW_DAYS} 天调用失败了几次读不出来：agent_resource_log 只有 "
        "resource_type / operation / token_estimate / detail / created_at 五列，"
        "没有任何成败列（2026-10-06 复测确认，与 PROJECT_STATUS.md §五.47 记的"
        "「报件里的失败记录是假的」同因）")]
    return items, unknowns


def _collect_pending_approvals(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """⑥pending_approvals：未处理行数 ＋ 最早一条的年龄（天）"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "CAST(julianday('now') - julianday(MIN(created_at)) AS INTEGER) AS age_days "
            "FROM pending_approvals WHERE status = 'pending'"
        ).fetchone()
    except sqlite3.Error as e:
        reason = ("pending_approvals 表不存在"
                  if _table_missing(e) else f"pending_approvals 表读不出来：{e}")
        return [], [_unknown("items.fact", reason)]
    pending = row["n"] or 0
    age = "NULL" if pending == 0 or row["age_days"] is None else row["age_days"]
    # evidence=context：待审批是「等人点头」的队列长度，阶段一拍板把它排除在实质依据之外
    # （堆着没批的东西不等于项目在推进，恰恰是要人先处理的东西）
    return [("items.fact",
             f"pending_approvals[{_kv([('pending', pending), ('oldest_age_days', age)])}]",
             "pending_approvals 表（WHERE status='pending'）", EVIDENCE_CONTEXT)], []


def _collect_recent_notes(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """⑦notes：近 N 天新增行数（updated_at 不算新增，这里刻意只数 created_at）

    项目活动证据三族之一，**口径只认 `new`**（设计席 2026-10-11 裁决 1 收窄）。
    周复盘关心的是「最近在推进」，不是「曾经有产出」：`total` 仍逐字写进快照文本、复盘
    读得到，只是证据标记归 `activity_empty`。
    这里原本写的是「`new` 或 `total` 非零就算 activity」——那句在生产库的只读副本上被量出
    漏口（窗口内读数为零、全表非零时也照样放行），2026-10-11 起作废；口径的同源记录见
    `DESIGN_ANCHORS.md` §一.10，本文件不另立一份判据。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notes WHERE created_at >= datetime('now', ?)",
            (f"-{RECENT_WINDOW_DAYS} days",),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
    except sqlite3.Error as e:
        reason = ("notes 表不存在"
                  if _table_missing(e) else f"notes 表读不出来：{e}")
        return [], [_unknown("items.fact", reason)]
    evidence = EVIDENCE_ACTIVITY if row["n"] else EVIDENCE_ACTIVITY_EMPTY
    return [("items.fact",
             f"notes[{_kv([('recent_days', RECENT_WINDOW_DAYS), ('new', row['n']), ('total', total)])}]",
             "notes 表（created_at >= datetime('now','-7 days')）", evidence)], []


def _collect_previous_decision(conn) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """⑧上一条 decisions：它服务的是 decision_scope——这次定多长，要看上次的范围与隔了多久"""
    try:
        row = conn.execute(
            "SELECT id, decision_scope, status, "
            "CAST(julianday('now') - julianday(created_at) AS INTEGER) AS age_days "
            "FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        reason = ("decisions 表不存在（Phase 1a 的表还没在这库里建出来）"
                  if _table_missing(e) else f"decisions 表读不出来：{e}")
        return [], [_unknown(DECISION_FIELD_SCOPE, reason)]
    if row is None:
        return [], [_unknown(
            DECISION_FIELD_SCOPE,
            "decisions 表 0 行：没有历史节奏可依据，本次范围只能由人定，采集层不给默认范围")]
    # 项目活动证据三族之一（阶段一拍板）。⚠️ 一条自然结果，如实写在这里而不是藏起来：
    # 复盘的「请求补充」出口自己也会往 decisions 落一行，所以下一轮采集到 previous_decision
    # 就变成「有实质依据」了——三问形状因此实际只出现在库里从没立过决策的头一两轮。
    # 要它常驻得把 decisions 摘出实质口径，那是在改拍板，本模块不自己动。
    return [(DECISION_FIELD_SCOPE,
             "previous_decision[" + _kv([
                 ("id", row["id"]), ("decision_scope", row["decision_scope"]),
                 ("status", row["status"]), ("age_days", row["age_days"]),
             ]) + "]",
             "decisions 表（ORDER BY id DESC LIMIT 1）", EVIDENCE_ACTIVITY)], []


AUTO_COLLECTOR_SOURCES = (
    "goals", "goal_tasks", "test_runs", "checks/*_exemptions.json",
    "agent_resource_log", "pending_approvals", "notes", "decisions",
)


def collect_auto_items(goal_id: Optional[str] = None) -> Tuple[List[ItemTriple], List[UnknownPair]]:
    """跑完 8 项自动采集，返回（四元组列表，unknown 三元组列表）。全程只读，不抛异常。

    每项采集器各自吞自己的 sqlite3.Error 并转成 unknown（理由见模块 docstring
    的「错误语义」），所以这里拿到的一定是可用数据或明确的「读不出来」。

    Args:
        goal_id: 显式指定这条 State 关于哪个目标；None 时由 goals 表里唯一 active
                 目标推出，推不出（0 个或多个）则登记 unknown。

    Returns:
        (items, unknowns)：items 的元素是 (decision_field, value, source, evidence)；
        unknowns 的元素是 (decision_field, reason, code)——`AUTO_UNSERVABLE` 那五条
        结构上给不出的字段在这里被归一成同样长度的三元组（code 为 None），
        所以下游不必区分「读不出来」与「根本没有这种东西」。
    """
    items: List[ItemTriple] = []
    unknowns: List[UnknownPair] = []
    conn = get_db()
    try:
        for part, part_unknown in (
            _collect_goal(conn, goal_id),
            _collect_goal_tasks(conn),
            _collect_last_test_run(conn),
            _collect_llm_calls(conn),
            _collect_pending_approvals(conn),
            _collect_recent_notes(conn),
            _collect_previous_decision(conn),
        ):
            items.extend(part)
            unknowns.extend(part_unknown)
    finally:
        conn.close()
    # 台账那一项读文件，不占连接，放在连接之外
    part, part_unknown = _collect_debt_ledgers()
    items.extend(part)
    unknowns.extend(part_unknown)
    unknowns.extend(_unknown(field, reason) for field, reason in AUTO_UNSERVABLE)
    return items, unknowns


# ---------- 人工段与 Unknown 的接收入口 ----------

def validate_state_item(decision_field: str, kind: str, content: str,
                        source: Optional[str],
                        evidence: Optional[str] = None) -> Dict[str, Any]:
    """校验并组装一条 State 条目；违规一律 raise ValueError（不静默返回 None）

    四条规则，逐条给理由：
    - decision_field 必须在 DECISION_FIELDS 里。这是改动 4 那条「指不出 decision
      字段的项数必须为 0」在写路径上的落点——只靠测试挡不住下一个调用方。
    - kind 必须在五类里（复用 core.decision_store 的常量，不在这里再造一份）。
    - **kind='fact' 时 source 必须非空**（判据①「无 source 的 fact 必须为 0」）。
      其余四类不强制：assumption 的定义就是「没有证据」，硬要它填 source 等于逼调用方
      编一个出处；inference 该写明支撑它的那条 fact 这件事记在 decision_store 的
      docstring 里，本模块不再加一条设计席没要求的硬约束。
    - `evidence`（阶段一新增）给了就必须落在 `FACT_EVIDENCE_VALUES` 里，且**只有自动段的
      fact 能带**：人工段的条目不参与「有没有实质依据」的判断（闸门①已经管着人工段）。
      这个键**可选**——不给就不写进条目，于是人工段条目与历史快照一样保持四键，
      读侧一句 `.get("evidence")` 三种读法分得开：activity／非 activity／当年没这个概念。
    """
    if decision_field not in DECISION_FIELDS:
        raise ValueError(f"采集项指不出 Decision 字段：{decision_field!r} 不在 DECISION_FIELDS 里")
    if kind not in VALID_ITEM_KINDS:
        raise ValueError(f"非法的条目类别：{kind!r}，允许的五类是 {VALID_ITEM_KINDS}")
    if not (content or "").strip():
        raise ValueError("条目内容不能为空")
    if kind == "fact" and not (source or "").strip():
        raise ValueError("fact 必须写明来源（表或文件），不接受空 source")
    if evidence is not None:
        if evidence not in FACT_EVIDENCE_VALUES:
            raise ValueError(f"非法的证据标记：{evidence!r}，允许的三个值是 {FACT_EVIDENCE_VALUES}")
        if kind != "fact":
            raise ValueError(f"只有自动段的 fact 能带证据标记，kind={kind!r} 不许带")
    item: Dict[str, Any] = {
        "decision_field": decision_field,
        "kind": kind,
        "content": content,
        "source": source or None,
    }
    if evidence is not None:
        item["evidence"] = evidence
    return item


def build_project_state(goal_id: Optional[str] = None,
                        manual_items: Optional[List[Tuple[str, str, str, str]]] = None,
                        note: Optional[str] = None) -> Dict[str, Any]:
    """采一份快照（不落库）：自动段 ＋ 人工段 ＋ unknowns。

    Args:
        goal_id: 见 collect_auto_items
        manual_items: 人工段入口，每个元素是 (decision_field, kind, content, source)。
            自动段的 fact 由采集器直接给出四元组（第四位是证据标记，阶段一），人工段的
            四类（user_report / inference / assumption / unknown）走这里，逐条过
            validate_state_item，**不带证据标记**。
        note: 人写的一句话备注，可空。它**不进 items**，免得它被当成事实读。

    Returns:
        {"snapshot_at", "goal_id", "items", "unknowns", "note"}。items 是
        [{"decision_field","kind","content","source"} ＋（自动段才有）"evidence"] 的列表
        ——**列表而不是以内容为键的字典**：同一条内容以不同 kind 传入要两条都在（判据②）。
        unknowns 的元素是 {"decision_field","reason"} ＋（只有选不出目标那两条有）"code"。
    """
    auto_items, unknowns = collect_auto_items(goal_id=goal_id)
    items: List[Dict[str, Any]] = []
    for field, value, source, evidence in auto_items:
        items.append(validate_state_item(field, "fact", value, source, evidence))
    for entry in (manual_items or []):
        field, kind, content, source = entry
        items.append(validate_state_item(field, kind, content, source))
    resolved_goal_id = goal_id
    if resolved_goal_id is None:
        for item in items:
            if item["decision_field"] == DECISION_FIELD_GOAL_ID:
                resolved_goal_id = item["content"]
                break
    unknown_dicts: List[Dict[str, Any]] = []
    for entry in unknowns:
        field, reason = entry[0], entry[1]
        code = entry[2] if len(entry) > 2 else None
        record: Dict[str, Any] = {"decision_field": field, "reason": reason}
        # code 同样是「给了才写」：历史行与 AUTO_UNSERVABLE 那五条都没有这一格，
        # 读侧 .get("code") 读出 None 就是「这一条不携带形状信息」，不是「形状是空字符串」。
        if code:
            record["code"] = code
        unknown_dicts.append(record)
    return {
        "snapshot_at": _utc_now_text(),
        "goal_id": resolved_goal_id,
        "items": items,
        "unknowns": unknown_dicts,
        "note": note,
    }


# ---------- 落盘：追加行，永不改历史 ----------

def save_project_state(state: Dict[str, Any]) -> int:
    """把一份快照作为**新的一行**追加进 project_states，返回新行 id

    只 INSERT、不 UPDATE、不 DELETE（判据③「落盘是追加」）。历史行逐字节不动，
    因为这份数据的用途就是「当时读到的是什么」——改了它，4a 就没法对照「我当时以为是什么」。
    """
    items = state.get("items") or []
    if not isinstance(items, list):
        raise ValueError("state['items'] 必须是列表")
    unknowns = state.get("unknowns") or []
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO project_states (snapshot_at, goal_id, items, unknowns, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (state["snapshot_at"], state.get("goal_id"),
             json.dumps(items, ensure_ascii=False, sort_keys=True),
             json.dumps(unknowns, ensure_ascii=False, sort_keys=True),
             state.get("note")),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _state_row_to_dict(row) -> Dict[str, Any]:
    """行 → dict：把 items / unknowns 两列的 JSON 还原成列表。

    解不出来（历史行被手工改坏）时给空列表而不是抛异常——读路径的性质与写路径相反，
    这里静默降级不会造出假账，最坏是「这份快照的条目读不出来」，而原文仍在那一列里。
    """
    out = dict(row)
    for key in ("items", "unknowns"):
        raw = out.get(key) or "[]"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = []
        out[key] = parsed if isinstance(parsed, list) else []
    return out


def get_project_state(state_id: int) -> Optional[Dict[str, Any]]:
    """按 id 取回一份快照；不存在返回 None"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM project_states WHERE id = ?", (state_id,)
        ).fetchone()
    finally:
        conn.close()
    return _state_row_to_dict(row) if row else None


def latest_project_state(goal_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """取最近一份快照；给 goal_id 时只在该目标的快照里取最近一份。

    ⚠️ 「最近」按 id 最大算，不按 snapshot_at 排——同一根理由见 test_runs 那条 unknown：
    标识符列（id）比字符串时间戳可靠，而本表的 snapshot_at 带偏移、跨实现写法可能不同。
    """
    conn = get_db()
    try:
        if goal_id is None:
            row = conn.execute(
                "SELECT * FROM project_states ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM project_states WHERE goal_id = ? ORDER BY id DESC LIMIT 1",
                (goal_id,)).fetchone()
    finally:
        conn.close()
    return _state_row_to_dict(row) if row else None
