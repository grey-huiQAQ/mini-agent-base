# -*- coding: utf-8 -*-
"""
🏠 首页（工作台）测试（P7 阶段二 B1，2026-10-17）

被测的是 `ui/controllers/home_controller.py`（三类卡片的读取与渲染）与 `ui/app_factory.py`
里那一格新 Tab。设计席 B1 的六条验收判据逐个对上：

- **判据①「恰好三类卡片、每类 ≤3 条」** →
  `test_card_types_are_exactly_three_and_registered`（三类的登记本身：`CARD_TYPES`／
  采集注册表／两张文案表必须同为那三个键——加第四族却漏登记，这里就红）
  ＋ `test_every_card_type_has_its_words`（每一族的文案非空、采集函数返回列表）
  ＋ `test_each_region_keeps_at_most_three_cards`（造五条，卡块数／下拉选项数／概览数
  三处都必须是 3）＋ `test_decision_cards_sorted_by_updated_at`（截断之前先排对：
  留下的是哪三条由 `updated_at` 决定，不是「插入顺序的最后三条」）；
- **判据②「每张卡片四要素齐全，② 允许合法缺省」** →
  `test_decision_card_carries_all_four_elements` ＋
  `test_empty_summary_shows_the_field_is_empty_not_nothing`（summary 为空时那一行**还在**、
  写成「账本里这一格是空的」）＋ `test_task_card_has_no_take_line_at_all`
  （待办：那一行**根本不出现**——印「（无）」就是假装它本来该有值）＋
  `test_opportunity_take_comes_from_ai_reason_not_summary`（发现：B1b 起这一族**有** ② 了，
  内容只许来自 `ai_reason`，**不许拿原始 `summary` 凑**）；
- **判据③「点决策卡片 → 能到详情 → 能批准 → 能标记已执行」（端到端，复用现有回调）** →
  `test_end_to_end_jump_detail_approve_execute`（四个动作在同一个用例里连做一次，
  每一步回库核对状态，不是只看返回文案）＋ `test_jump_task_selects_it_in_goal_page`
  ＋ `test_jump_works_for_an_in_progress_task`（组 4：跳转的集合跟着扩，进行中的那条
  同样选得中，而回调本身一字未改）
  ＋ `test_jump_opportunity_selects_it_in_resource_page`（另两族的跳转真把那一条**选中**
  了，不是只刷了列表；后半截还验「被筛选挡在外面时照实说」）
  ＋ `test_jump_targets_are_real_tabs`（指路文案里那三个页签必须真在组件树里）
  ＋ `test_home_backfill_is_in_demo_load_chain` ＋
  `test_home_jump_bindings_reuse_read_callbacks`（静态读 `ui/app_factory.py`：绑定接的是
  既有读取回调，首页按钮没接任何写回调；**组 5 子批 A（2026-10-18，设计席定案「写口放新
  模块」）之后这一条读成「除『🎯 当前重点』那一格外没接任何写回调」**——切片的边界是
  `ui/app_factory.py` 里那行 marker 注释，本格自己的正向证据在
  `tests/test_current_focus.py::test_write_callbacks_live_only_in_the_focus_block_of_home`；
  口径照 `tests/test_review_tab.py::test_start_up_backfill_is_in_demo_load_chain`，
  刻意不重跑 `create_app()`）；
- **判据④「筛选正确」** → `test_finished_decisions_do_not_appear`（executed／evaluated／
  rejected／insufficient 四档都不出现）＋ `test_done_tasks_stay_out_and_in_progress_comes_in`
  （组 4，2026-10-18：待办的状态集合由 pending 扩到 pending ＋ in_progress，done 仍在门外）
  ＋ `test_in_progress_card_carries_the_status_and_the_label`（「进行中」那条 ③ 带标签、
  pending 那条不带）＋ `test_task_cards_sort_by_created_at_not_by_status`（扩集合没有
  把排序键换成「状态优先」）
  ＋ `test_non_new_opportunities_do_not_appear`
  ＋ `test_archived_goal_tasks_are_out_of_the_scope`（归档目标下的 pending 不算——
  这条收窄**已由设计席 2026-10-17 定案采纳**（§五.62 偏离①），口径写全为「待办 =
  active 目标下的 goal_tasks pending」；用例继续钉住它，将来谁要改回全集必须先在
  锚点／根文档里留话）
  ⚠️ 组 4（2026-10-18）扩的是**状态**那一维，**目标**那一维一字未动 ⇒ 这条守门职责照旧
  ＋ `test_below_threshold_opportunity_is_out` ＋
  `test_excluded_eligibility_hint_is_out`（B1b 把发现区的门槛从「只看 status」换成三条，
  这两条各钉住新加的那一条：分数不够／资格是排除档都不上首页）；
- **判据⑤「首页零写库、零 LLM」** → `test_home_reads_write_nothing`
  （**逐表**比行数与 MAX(rowid)，另把三类卡片的真源整行取回比对——只数行数抓不到
  原地 UPDATE）＋ `test_home_never_calls_the_model`（把 `business.model_client.chat_sync`
  ／`stream_chat` 换成计数器桩跑一遍全部公开函数，计数必须为 0；`core.ai_filter` 是在
  函数内延迟导入这两个的，所以换咽喉点连「绕道打模型」一起抓住）＋
  `test_home_controller_source_has_no_write_or_llm`（静态：用 `ast` 取 import 根名与
  **被调用名**，注释与 docstring 里的字面词不参与判断）＋
  `test_static_write_scan_is_not_vacuous`（同一个扫描器扫 `review_controller.py`
  必须报出写调用——那文件真的写库；扫不出来的扫描器等于没有）；

⚠️ **B1b（2026-10-17）之后，判据⑤ 读成「零直接 LLM 调用」**（口径由设计席在 B1b
锁定决定 ④ 修订，实现侧的文件头那段是同一句话）：首页刷新时允许**发起**一轮后台加工，
但发起不等于打模型。这一头的落地是三层：
① 上面那两条运行期用例**照旧跑**，只是 `_call_everything()` 里先用 `_trigger_guard()`
把 `core.ai_filter.ai_filter_opportunities` 在内存里换成记录桩——**测试绝不打真网络**
（锚点 §五.7 那条纪律），桩同时就是「触发真的发生了」的探针；
② `test_refresh_triggers_processing_for_home_candidates` 正向钉「刷新确实发起了、
limit 用的是首页那一个数」，`test_trigger_reuses_the_shared_state_and_does_not_stack_up`
钉节流——`running` 已真时
一次都不许再发起，并断言首页复用的就是 `_shared` 那**同一个 dict 对象**（再起一套
就会有两个线程摸同一批候选）；
③ `test_llm_entry_name_only_inside_the_trigger` 静态钉「被调用名
`ai_filter_opportunities` 只许出现在 `ensure_opportunities_processed` 的函数体内」——
别处冒出来一次就是绕过触发的直接调用，而那正是判据⑤ 要拦的形状。
写库那一条**没有**被修订：后台线程写的是 `core.ai_filter` 落库路径里的 `ai_*` 列，
首页自己一格都不写，`test_home_reads_write_nothing` 的指纹对拍仍须零命中（桩住触发之后）。
- **形状要钉**：`test_app_import_builds_home_first` ＋
  `test_home_tab_is_first_and_old_fourteen_unchanged`（逐字列出 14 个旧 Tab 标签并比
  **相对顺序**，刻意不写「共 15 个」这种数字断言——数字在任何一次加 Tab 时都得跟着改，
  改完就等于没有断言）＋ `test_home_tab_widgets_are_in_component_tree`
  ＋ `test_card_field_is_named_card_type_not_type`（§五.62 偏离④ 定案：卡片那一格叫
  `card_type`、不许留裸 `type`——渲染只认 `card.get('card_type')`，字段写回旧名会让
  决策卡的 ② 整格静默消失，界面上看不出来）。

另有 `test_region_read_error_is_not_reported_as_empty`：读失败与「读到但没有」不许混成
同一句话（本模块文件头第 6 条），而概览那一行也不许把读不出来写成 0 条。

⚠️ 导入顺序即隔离顺序：`ui/controllers/_shared.py` 在**导入期**调 `init_db()`，
所以必须先 `setup_test_db()` 再碰任何 UI/core 模块，否则本文件一 import 就往生产库建表。
⚠️ 字符串里一律用「」而不用 ASCII 双引号（中文串里嵌 ASCII 双引号在本仓库是语法错误）。
"""

import ast
import contextlib
import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core import ai_filter as af  # noqa: E402 （发现卡那三条门槛的真源在这，测试读它的常量）
from core import decision_store as ds  # noqa: E402
from core.resource_scout import OpportunityStatus, ResourceScout  # noqa: E402

md.init_db()

# 控制器在 app 之前导入：控制器只依赖 core 与 _shared，先导入能确认「界面回调不靠 app 存在」
from ui.controllers import home_controller as hc  # noqa: E402
# 资源页那个轮询是失败痕迹的**清除方**，`test_failure_flag_lifetime_...` 要按实跑它一次
from ui.controllers import resource_controller as res  # noqa: E402
from ui.controllers import review_controller as rc  # noqa: E402
import app as app_module  # noqa: E402  （导入即 create_app()，这就是判据①的冒烟方式）

_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")
_HOME_SOURCE = os.path.join("ui", "controllers", "home_controller.py")
_REVIEW_SOURCE = os.path.join("ui", "controllers", "review_controller.py")
_HOME_TAB_LABEL = "🏠 首页"

#: 改前（HEAD d0afea0）的 14 个 Tab 标签，**按源码里的出现顺序**逐字抄自
#: `git show HEAD:ui/app_factory.py` 的 `with gr.Tab(...)` 清单。
#: ⚠️ 这个序是**量出来的**不是排的：执行席第一版把 `🧭 项目复盘` 抄到了末尾，照着
#: `tests/test_review_tab.py` 那份清单——那是 3c-乙 之前 13 个 Tab 的基线、本来不含它。
#: 实测 `demo.blocks` 里 Tab 的出现序与源码书写序一致，`🧭 项目复盘` 夹在
#: `🧩 任务模板` 与 `💡 资源机会` 之间。
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
    "🧭 项目复盘",
    "💡 资源机会",
    "🧪 测试与资源监控",
)

#: 首页三区那两个下拉标签的**显示文本**，逐字抄自 `ui/app_factory.py`。
#: 刻意不从 `hc._SUMMARY_LABEL_BY_TYPE` 推：那条用例要防的正是「实现里的称呼和界面上的
#: 标签各长各的」，用实现推实现等于没校验。
_HOME_DROPDOWN_LABELS = (
    "选一张决策卡（跳转用）",
    "选一张待办卡（跳转用）",
    "选一张发现卡（跳转用）",
)

#: 概览行「读不出来」那几个字，同样**在测试里另写一份**（理由同上）。
_ERROR_COUNT_TEXT = "读不出来"

#: 静态扫描的**全禁表**：命中即「动了写，或动了模型」。取的是**被调用名**与
#: **import 根名**，所以注释里写「INSERT」不会误报（反向证据见
#: `test_static_write_scan_is_not_vacuous`）。
#: 第一批「今日工作流」（2026-10-20，spec §6.1）之后，首页那一格用的是这张表**减去**
#: 下面那张授权表；其余页面（复盘页那一条反向证据）仍按这张全禁表扫。
_FORBIDDEN_CALLS = {
    # 裸 SQL／连接／事务
    "execute", "executemany", "executescript", "commit", "get_db", "get_connection",
    # 决策状态机与写路径
    "create_decision", "set_status", "mark_executed", "evaluate", "add_item",
    "link_experience",
    # 目标与任务写路径
    "create_goal", "update_goal", "archive_goal", "add_task", "update_task_status",
    "update_task_priority", "add_note", "generate_tasks_from_goal", "add_milestone",
    "mark_goal_completed", "mark_goal_archived", "set_task_home",
    # 本批补进来的一条（spec §6.1 原文「把 `set_task_planned_date` 补进表作为『本批放开前
    # 曾未被扫到』的记录」）：它过去**不在这张表里**，不是因为被允许，是因为没人扫过它。
    # 先登记为禁、再由下面那张授权表减掉，这样「它现在是被授权的」在文件里留得下证据。
    "set_task_planned_date",
    # 机会写路径
    "add_opportunity", "update_status", "delete_opportunity", "recompute_eligibility",
    "recompute_relevance", "mark_digest_notified", "ignore_overdue",
    # 模型咽喉点
    "chat_sync", "stream_chat",
}

#: 第一批「今日工作流」在**首页**放开的两个写口（spec §1.4：整屏只放开两列——
#: `goal_tasks.status` 与 `goal_tasks.planned_date`）。这张表与上面那张全禁表**同时留着**，
#: 首页真正适用的禁词集合是 `_HOME_FORBIDDEN_CALLS = 全禁 − 授权`；
#: 「这两个写口各自只写自己那一列」不由这里钉，由 `tests/test_today_region.py` 逐列对拍钉。
#: ⚠️ 授权表**只对首页那一格生效**：复盘页／目标页的写路径照旧按全禁表扫。
_AUTHORIZED_HOME_WRITES = {"update_task_status", "set_task_planned_date"}

_HOME_FORBIDDEN_CALLS = _FORBIDDEN_CALLS - _AUTHORIZED_HOME_WRITES

#: 首页这个模块不许碰的层（层序见 `checks/check_import_direction.py`）：
#: agents=2 是「会跑复盘、会问模型」的那一层，business=0 里坐着 model_client。
_FORBIDDEN_IMPORT_ROOTS = {"agents", "business", "sqlite3"}


# ---------- 夹具 ----------

def _utc_now_text() -> str:
    return _now().isoformat(timespec="seconds")


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


#: 夹具时间戳的**单调计数器**。为什么不用当下时刻：本文件每条用例都往库里留数据，
#: 「我刚造的那条必须排在前三」这种前提用真实时钟表达就成了「用例之间互相留数据」
#: （本项目吃过这个亏，口径同 `tests/test_review_tab.py::pending_count_shown_in_list_header`
#: 那句「刻意不写死条数」）。改成 2099 年起单调递增的日期后：**任何一条夹具数据都比
#: 上一条新**，所以本用例刚造的那条永远是第一名，期望序可以手算写死。
_seq = [0]


def _next_day() -> str:
    _seq[0] += 1
    return (date(2099, 1, 1) + timedelta(days=_seq[0])).isoformat()


def _ts_iso() -> str:
    """`decisions.updated_at` 那一族的写法（定宽＋固定偏移 ⇒ 字符串序就是时间序）"""
    return f"{_next_day()}T00:00:00+00:00"


def _ts_sql() -> str:
    """`goal_tasks.created_at`／`opportunities.discovered_at` 的写法
    （CURRENT_TIMESTAMP 是「YYYY-MM-DD HH:MM:SS」，无偏移标注、UTC）"""
    return f"{_next_day()} 00:00:00"


def _set_column(table: str, row_id: int, column: str, value: Any) -> None:
    """测试侧直接改列：排序键要靠真实时钟错开就得 sleep，而夹具要的是**可手算的确定序**

    生产代码从不这样写（`home_controller` 里一条 SQL 都没有）；这是夹具特权。
    `value` 自 B1b 起不再只有字符串——`relevance_score` 是小数，绑进 `?` 时按原类型存。
    """
    conn = md.get_connection()
    try:
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
        conn.commit()
    finally:
        conn.close()


def _make_decision(title: str = "首页夹具决策", summary: str = "夹具写的判断",
                   status: str = ds.STATUS_PROPOSED,
                   scope: str = ds.SCOPE_OPERATIONAL) -> int:
    """按状态机把一条决策推到指定状态，并把它排到「目前最新」（不绕守卫）"""
    decision_id = ds.create_decision(title, summary=summary, decision_scope=scope)
    if status == ds.STATUS_DISCUSSED:
        ds.set_status(decision_id, ds.STATUS_DISCUSSED)
    elif status == ds.STATUS_APPROVED:
        ds.set_status(decision_id, ds.STATUS_APPROVED)
    elif status == ds.STATUS_REJECTED:
        ds.set_status(decision_id, ds.STATUS_REJECTED)
    elif status == ds.STATUS_INSUFFICIENT:
        ds.set_status(decision_id, ds.STATUS_INSUFFICIENT)
    elif status == ds.STATUS_EXECUTED:
        ds.set_status(decision_id, ds.STATUS_APPROVED)
        ds.mark_executed(decision_id, "夹具的执行痕迹")
    elif status == ds.STATUS_EVALUATED:
        ds.set_status(decision_id, ds.STATUS_APPROVED)
        ds.mark_executed(decision_id, "夹具的执行痕迹")
        ds.evaluate(decision_id, ds.EVAL_HELPFUL, "夹具写的实际结果")
    assert ds.get_decision(decision_id)["status"] == status, (
        f"夹具没能把决策推到 {status}（状态机改了这里就得跟着改）")
    _set_column("decisions", decision_id, "updated_at", _ts_iso())
    return decision_id


def _make_goal(title: str) -> str:
    return hc.goal_manager.create_goal(title, "首页夹具目标").goal_id


def _make_task(goal_id: str, title: str, status: str = "pending") -> int:
    """建一条**能上首页**的待办

    组 5 子批 B（2026-10-19）之后光有 active 目标 ＋ pending／in_progress 状态已经不够：
    首页待办区换成了**准入制**（`is_home` 非零才算，判据原文＝设计席指派「is_home=0 不上
    首页」），而这一列的默认是 0——按旧夹具形状造的数据**一条都进不了首页**，待办区会恒
    0 张。这里替夹具补上「人已点过「🏠 加入首页」」那一格，让「造一条就来首页」这个既有
    前提继续成立（与 `_make_opportunity` 在 B1b 里为 `relevance_score` 做的同一件事）。
    不给你留 `home=` 开关：本文件的用例测的都是「上得了首页的那些条长什么样、怎么排、
    点了跳转会去哪」，反面（没准入的那条不在）由 `tests/test_task_home.py` 整族负责，
    两处都留一套等于同一件事写两个地方。
    """
    hc.goal_manager.add_task(goal_id, title)
    conn = md.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM goal_tasks WHERE goal_id = ? AND title = ? ORDER BY id DESC",
            (goal_id, title)).fetchone()
        assert row is not None, f"add_task 没落库：{title}"
        task_id = int(row[0])
        conn.execute("UPDATE goal_tasks SET is_home = 1 WHERE id = ?", (task_id,))
        if status != "pending":
            conn.execute("UPDATE goal_tasks SET status = ? WHERE id = ?",
                         (status, task_id))
        conn.execute("UPDATE goal_tasks SET created_at = ? WHERE id = ?",
                     (_ts_sql(), task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id


def _make_opportunity(title: str, status: str = "new", score: float = 0.8,
                      hint: str = "likely_yes", reason: Optional[str] = None,
                      next_step: Optional[str] = None,
                      screened: bool = False,
                      assets: Optional[str] = None) -> int:
    """建一条**能上首页**的机会，并可指定「加工后那两格」长什么样

    B1b（2026-10-17）之后光 `status="new"` 已经不够：发现区的候选集换成了三条门槛
    （new ∧ `relevance_score` ≥ 门槛 ∧ 资格 ≠ 排除档），而 `relevance_score` 的列默认是 0
    ——按旧夹具形状造的数据**一条都进不了首页**，发现区会恒 0 张。默认值因此按那三条给
    （0.8 分、`likely_yes`），让「造一条就来首页」这个既有前提继续成立；要构造被挡在
    门外的场景就显式传 `score=`／`hint=`，门槛的两个数值都读 `core.ai_filter` 的常量，
    测试里不另抄一份阈值（抄了就成了第二个可改的地方）。

    `reason`／`next_step` 是 ②④ 读的内容，`screened=True` 才写 `ai_screened_at`
    （＝这条「加工过」）。三者的组合正是首页那几种措辞的分工：
    没加工过 ⇒「还没加工」那一句；加工过而那一格空 ⇒「加工过了，但没有那一句」。
    第三种态（**没加工过、且上一轮加工报了错**）不在这个夹具里——它的真源是
    `_shared.ai_filter_state["error"]` 那一格，不在库行上，要造得用 `_processing_error()`。

    `assets`（B2，2026-10-17）写 `usable_assets` 那一格，**只有显式传才 UPDATE**：不传就
    保持迁移后的 NULL。这一列的 NULL 同时是「还没加工／加工过但答了『无』／模型漏答」
    三件事，夹具替它们造出默认形状就等于替测试撒谎——要造「加工过、答了无」传
    `screened=True` 而**不传** `assets`，要造「有资源」传一句非空文本。
    """
    opportunity_id = ResourceScout.add_opportunity(
        title, source_url=f"https://home-fixture.invalid/{title}",
        type_key="hackathon", summary="夹具写的一句话说明",
        status=status, eligibility_hint=hint)
    assert opportunity_id, f"夹具没能建机会：{title}"
    _set_column("opportunities", opportunity_id, "discovered_at", _ts_sql())
    _set_column("opportunities", opportunity_id, "relevance_score", float(score))
    if screened:
        _set_column("opportunities", opportunity_id, "ai_screened_at", _ts_sql())
    if reason is not None:
        _set_column("opportunities", opportunity_id, "ai_reason", reason)
    if next_step is not None:
        _set_column("opportunities", opportunity_id, "ai_next_step", next_step)
    if assets is not None:
        _set_column("opportunities", opportunity_id, "usable_assets", assets)
    return opportunity_id


# ---------- 判据⑤用的两个探针 ----------

class _ModelStub:
    """咽喉点计数器：接到调用就记下来，绝不真打网络

    为什么是这两个名字：全项目的模型调用都从 `business.model_client.chat_sync`／
    `stream_chat` 走，而 `core.ai_filter` 这类「函数内延迟导入」的绕道写法在调用时读的
    也是**模块属性**——属性被换掉，绕道一起被抓住。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(repr((args, kwargs)))
        return "桩返回的一句话——任何首页代码都不该用到它"

    @property
    def count(self) -> int:
        return len(self.calls)


class _TriggerStub:
    """自动加工那一步的记录桩：**顶替** `core.ai_filter.ai_filter_opportunities`

    两件活一起干：① 让测试永远不真去摸模型（锚点 §五.7 那条纪律：负控与用例都不许打
    真网络，而 B1b 之后首页每次刷新都可能发起一轮加工）；② 留下「触发真的发生了」的
    探针——`calls` 记的是每一次被请求的 limit，正向用例就靠它数发起过几次、传的是哪一档。
    返回一份形状对得上的报告 dict：`ensure_opportunities_processed` 会把它写进
    `ai_filter_state["report"]`，桩不该顺带改掉那个形状。
    """

    def __init__(self) -> None:
        self.calls: List[int] = []

    def __call__(self, limit: int = 0, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(int(limit))
        return {"candidates": 0, "screened": 0, "failed": 0, "ids": [],
                "errors": [], "summary_text": "桩：候选 0 条，成功 0 条，失败 0 条"}


@contextlib.contextmanager
def _trigger_guard():
    """把首页发起的那一轮后台加工在**内存里**换成 `_TriggerStub`，退出时还原

    换的是 `core.ai_filter` 的模块属性，而 `ensure_opportunities_processed` 里调的是
    `af.ai_filter_opportunities(...)`（属性在调用那一刻才查）——所以这层替换真拦得住，
    不必改任何文件。装上与还原各断言一次：名字写错会**静默多加一个属性**、真函数照样活着，
    那种桩下的「一次都没发起」是恒真的假话（口径同 `_ModelStub` 那句探针）。
    """
    original = af.ai_filter_opportunities
    stub = _TriggerStub()
    af.ai_filter_opportunities = stub
    try:
        assert af.ai_filter_opportunities is stub, "触发桩没装上到 core.ai_filter"
        yield stub
    finally:
        af.ai_filter_opportunities = original
        assert af.ai_filter_opportunities is original, "触发桩没还原干净"


def _wait_idle(timeout: float = 10.0) -> None:
    """等首页那根后台线程把 `running` 落回 False（桩是瞬时的，这里只等线程调度）"""
    deadline = time.time() + timeout
    while time.time() < deadline and app_module.ai_filter_state.get("running"):
        time.sleep(0.02)
    assert not app_module.ai_filter_state.get("running"), (
        f"{timeout}s 后自动加工的线程还没结束：这一轮的桩是瞬时的，"
        "卡住说明复位那段没写对（或被别的用例留在 running=True）")


def _all_tables() -> List[str]:
    conn = md.get_connection()
    try:
        return [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    finally:
        conn.close()


def _db_fingerprint() -> Dict[str, Any]:
    """逐表 (行数, MAX(rowid)) ＋ 三类真源的**整行内容**

    只数行数抓不到原地 UPDATE：把 decisions／goal_tasks／opportunities 三张卡片真源
    每一行取回来比对，谁被改了一个字就红。其余表用行数＋rowid 兜住。
    """
    conn = md.get_connection()
    try:
        out: Dict[str, Any] = {}
        for table in _all_tables():
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            max_rowid = conn.execute(
                f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
            out[table] = (count, int(max_rowid))
        for table in ("decisions", "goal_tasks", "opportunities"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            out[f"{table}:rows"] = repr([tuple(row) for row in rows])
        return out
    finally:
        conn.close()


def _fingerprint_diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return [f"{key}: {before.get(key)!r} → {after.get(key)!r}"
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)]


def _call_everything() -> List[str]:
    """把公开入口全跑一遍，但**先桩住自动加工**（B1b，2026-10-17）

    分两层是因为职责分得开：下面那一层是「判据⑤ 的分子＝调用集」，这一层只管一件事——
    `refresh_home_panel` 现在末尾会发起一轮后台加工，而测试不许真去打网络（锚点 §五.7）。
    整个调用集都包在桩里，不是只包那一个：万一将来有人在别处也发起了加工，这一层仍然拦得住，
    最坏是那次的 limit 记在桩上、不会漏网成一次真请求。
    """
    with _trigger_guard():
        return _call_everything_raw()


def _call_everything_raw() -> List[str]:
    """把本模块所有公开读取入口各跑一遍（判据⑤的分子就是这个调用集）

    Returns:
        「哪一次调用动了库」的清单，空列表＝一次都没动。

    指纹**贴着每次调用**取，买到的是**归因**不是覆盖面：红了能指名是哪一次调用动的库，
    而不是只知道这一轮里某处动了。别把它当成「什么写都抓得住」——M9 实测：往
    `refresh_home_panel` 塞一条 `add_opportunity`（URL 固定 ⇒ 被 `opportunities.source_url`
    的 UNIQUE 去重）时，本用例**照样绿**，因为那一行在排在前面的用例第一次调用时就已经
    落库了，到这里的每一次 before/after 对拍两边都有它。那种「只写得进一次」的写由
    `test_home_controller_source_has_no_write_or_llm` 那一层（静态禁词）抓住，
    两层各抓一种形状，不是冗余。
    """
    traces: List[str] = []

    def _one(label, fn):
        before = _db_fingerprint()
        fn()
        diff = _fingerprint_diff(before, _db_fingerprint())
        if diff:
            traces.append(f"{label} 动了库：{diff[:3]}")

    _one("collect_decision_cards", hc.collect_decision_cards)
    _one("collect_task_cards", hc.collect_task_cards)
    _one("collect_opportunity_cards", hc.collect_opportunity_cards)
    _one("collect_home_cards", hc.collect_home_cards)
    cards, _errors = hc.collect_home_cards()
    for card_type in hc.CARD_TYPES:
        for card in cards[card_type]:
            _one(f"render_card({card_type})", lambda c=card: hc.render_card(c))
            _one(f"render_home_region({card_type})",
                 lambda c=card, t=card_type: hc.render_home_region([c], t))
    _one("refresh_home_panel", hc.refresh_home_panel)
    # 等刚发起的那一轮**在桩还挂着的时候**真的结束：daemon 线程查 `af.ai_filter_opportunities`
    # 这个属性可能晚于本层 `with` 退出，早一步退出就把真函数放了回去、让它去打网络。
    # 这一层自己收摊，不依赖 `main()` 外面那层守护（单独调这个函数时也得是安全的）。
    _wait_idle()
    # 三个跳转也跑一遍：它们才是「首页会碰到别的页面」的那三处，空选与真 id 两种都走。
    _one("do_home_jump_decision(None)", lambda: hc.do_home_jump_decision(None))
    _one("do_home_jump_task(None)", lambda: hc.do_home_jump_task(None))
    _one("do_home_jump_opportunity(None)",
         lambda: hc.do_home_jump_opportunity(None))
    if cards[hc.CARD_TYPE_DECISION]:
        _one("do_home_jump_decision(真 id)", lambda: hc.do_home_jump_decision(
            cards[hc.CARD_TYPE_DECISION][0]["id"]))
    if cards[hc.CARD_TYPE_TASK]:
        _one("do_home_jump_task(真 id)", lambda: hc.do_home_jump_task(
            cards[hc.CARD_TYPE_TASK][0]["id"]))
    if cards[hc.CARD_TYPE_OPPORTUNITY]:
        _one("do_home_jump_opportunity(真 id)", lambda: hc.do_home_jump_opportunity(
            cards[hc.CARD_TYPE_OPPORTUNITY][0]["id"]))
    return traces


def _called_names_in(node: ast.AST) -> set:
    """一个 AST 节点内**被调用的名字**（B1b 用它把触发函数体单独圈出来）"""
    out: set = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _source_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _called_names(path: str) -> Dict[str, set]:
    """用 ast 取一个模块里**被调用的名字**与**import 的根名**（注释与字符串不参与）"""
    tree = ast.parse(_source_text(path))
    calls: set = set()
    roots: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(str(alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(str(node.module).split(".")[0])
    return {"calls": calls, "roots": roots}


# ---------- 形状：Tab 顺序与新组件 ----------

def test_app_import_builds_home_first():
    """导入 app 即构建界面（既有冒烟方式），且本文件连的是临时库"""
    current = os.path.abspath(md.DB_PATH)
    assert "_test_tmp_" in current.replace(os.sep, "/"), (
        f"DB 没落在临时目录，本文件会打到生产库：{current}")
    assert current != os.path.abspath(os.path.join("data", "memory.db"))
    assert hasattr(app_module, "demo"), "app.demo 不存在——create_app() 没被调用"
    for name in ("refresh_home_panel", "do_home_jump_decision", "do_home_jump_task",
                 "do_home_jump_opportunity"):
        assert hasattr(app_module, name), (
            f"app.py 没有 re-export {name}——app_factory 里那句 "
            f"_app_module.{name} 会在构建期 AttributeError")
    print("[OK] import app 构建出 demo，连的是临时库，首页四个回调都在 app 的命名空间里")


def _tab_labels() -> List[str]:
    """从**构建好的** Blocks 树里取全部 Tab 标签（不是读源码字符串）"""
    import gradio as gr
    blocks = getattr(app_module.demo, "blocks", None)
    assert blocks, "app.demo 没有 blocks——import app 没构建出界面"
    return [str(getattr(block, "label", "") or "")
            for block in blocks.values() if isinstance(block, gr.Tab)]


def test_home_tab_is_first_and_old_fourteen_unchanged():
    """🏠 首页排在最前（默认落地页），14 个旧 Tab 逐个仍在且**相对顺序不变**

    「默认选中」这一格靠的是 gradio Tabs 的 `selected` 默认值「Defaults to the first
    TabItem」（`gradio/layouts/tabs.py`）——所以「排第一」就是「默认落地页」，界面代码里
    没有第二处开关；这一条因此同时是布局判据与默认页判据。
    """
    labels = _tab_labels()
    assert labels[0] == _HOME_TAB_LABEL, f"首页不在第一位：{labels[:3]}"
    assert labels.count(_HOME_TAB_LABEL) == 1, "首页 Tab 被注册了不止一次"
    missing = [name for name in _BASELINE_TAB_LABELS if name not in labels]
    assert not missing, f"这些既有 Tab 不见了或被改名：{missing}"
    rest = [label for label in labels if label != _HOME_TAB_LABEL]
    assert rest == list(_BASELINE_TAB_LABELS), (
        "旧 Tab 被动过（不删不改名但被重排也算偏离 §七）：\n"
        f"实际 {rest}\n基线 {list(_BASELINE_TAB_LABELS)}")
    print(f"[OK] 首页排第一，14 个旧 Tab 逐个仍在且相对顺序不变（当前共 {len(labels)} 个，"
          "数字只打印、不作判据）")


def _labels_of(kind: str) -> List[str]:
    import gradio as gr
    cls = getattr(gr, kind, None)
    assert cls is not None, f"gradio 没有 {kind} 这一类，本文件的取法要跟着改"
    return [str(getattr(block, "label", "") or "")
            for block in app_module.demo.blocks.values() if isinstance(block, cls)]


def _button_values() -> List[str]:
    import gradio as gr
    return [str(getattr(block, "value", "") or "")
            for block in app_module.demo.blocks.values() if isinstance(block, gr.Button)]


def test_home_tab_widgets_are_in_component_tree():
    """三对「卡片区＋选择下拉」与四个按钮真在组件树里（取人看到的文本，不是变量名）"""
    dropdowns = _labels_of("Dropdown")
    for label in _HOME_DROPDOWN_LABELS:
        assert label in dropdowns, (
            f"首页缺下拉「{label}」；现有带「跳转」的：{[t for t in dropdowns if '跳转' in t]}")
    buttons = _button_values()
    for name in ("🔄 刷新首页", "🔎 查看与批准 →", "✅ 去标记完成 →", "🔎 查看 →"):
        assert name in buttons, f"按钮「{name}」不在树里：{buttons}"
    print("[OK] 首页三个下拉与四个按钮都在组件树内")


def test_jump_targets_are_real_tabs():
    """指路文案里那三个页签名必须真能在组件树里找到（把人带去一个不存在的页＝假话）"""
    labels = set(_tab_labels())
    for tab in (hc.TAB_REVIEW, hc.TAB_GOALS, hc.TAB_RESOURCE):
        assert tab in labels, f"首页说去「{tab}」，组件树里却没有这个 Tab"
    assert _HOME_TAB_LABEL not in (hc.TAB_REVIEW, hc.TAB_GOALS, hc.TAB_RESOURCE), (
        "跳转目标不该指回首页自己")
    print("[OK] 三个跳转目标页签都真实存在，且都不是首页")


# ---------- 判据①：恰好三类、每类 ≤3 ----------

def test_card_types_are_exactly_three_and_registered():
    """`CARD_TYPES`／采集注册表／两张文案表**同为那三个键**

    这一条管 §五 的形状：加一族要「只加类型、不改骨架」。四处各自列一遍三类是复制，
    但**必须一致**——漏一处就让首页出现「有卡片没文案」或「有文案没卡片」。
    """
    assert hc.CARD_TYPES == (hc.CARD_TYPE_DECISION, hc.CARD_TYPE_TASK,
                             hc.CARD_TYPE_OPPORTUNITY), (
        f"三类卡片的取值或顺序变了：{hc.CARD_TYPES}")
    assert set(hc._COLLECTORS) == set(hc.CARD_TYPES), (
        f"采集注册表与 CARD_TYPES 分叉：{set(hc._COLLECTORS) ^ set(hc.CARD_TYPES)}")
    assert set(hc._EMPTY_HINT_BY_TYPE) == set(hc.CARD_TYPES), "空态文案表与 CARD_TYPES 分叉"
    assert set(hc._SUMMARY_LABEL_BY_TYPE) == set(hc.CARD_TYPES), "概览称呼表与 CARD_TYPES 分叉"
    print(f"[OK] 恰好三类卡片，注册表与两张文案表键一致：{hc.CARD_TYPES}")


def test_every_card_type_has_its_words():
    """每一族的两张词表都得是**非空句子**，采集函数返回的是列表

    键对上了但值是空串＝界面上一块无声的空白；采集返回 None 会让 `len()` 在
    `refresh_home_panel` 里炸，而那个炸点在 Gradio 事件里只表现为「这一屏没刷新」。
    """
    for card_type in hc.CARD_TYPES:
        assert hc._EMPTY_HINT_BY_TYPE[card_type].strip(), f"{card_type} 空态文案是空的"
        assert hc._SUMMARY_LABEL_BY_TYPE[card_type].strip(), f"{card_type} 概览称呼是空的"
        assert isinstance(hc._COLLECTORS[card_type](), list), (
            f"{card_type} 的采集函数没返回列表")
    print("[OK] 三族的空态句与概览称呼逐个非空，采集函数形状对")


def test_card_field_is_named_card_type_not_type():
    """§五.62 偏离④ 定案：卡片那一格叫 `card_type`，且**不许再留裸 `type` 键**

    为什么值得一条用例、而不是只改一行字段名：`opportunities.type` 是那条机会自己的
    粗分类（hackathon／competition／…），与卡片种类同名不同义——「同一格两个含义」
    正是本项目反复咬过的坑。改名之后如果没有用例拦住，下一个人「顺手」把字段写回
    `type` 时什么都不会响：渲染只认 `card.get('card_type')`，取不到就把决策卡的 ②
    整格省掉，界面上与「summary 本来就空」长得一模一样。
    """
    goal_id = _make_goal("字段命名目标")
    task_id = _make_task(goal_id, "字段命名待办")
    decision_id = _make_decision("字段命名决策")
    opportunity_id = _make_opportunity("字段命名发现")
    collectors = {
        hc.CARD_TYPE_DECISION: hc.collect_decision_cards,
        hc.CARD_TYPE_TASK: hc.collect_task_cards,
        hc.CARD_TYPE_OPPORTUNITY: hc.collect_opportunity_cards,
    }
    wanted = {hc.CARD_TYPE_DECISION: decision_id, hc.CARD_TYPE_TASK: task_id,
              hc.CARD_TYPE_OPPORTUNITY: opportunity_id}
    for family, collect in collectors.items():
        card = next(c for c in collect() if c["id"] == wanted[family])
        assert "card_type" in card, (
            f"{family} 这一族没有 card_type 这一格（键：{sorted(card)}）")
        assert card["card_type"] == family, (
            f"card_type 的值不是这一族的常量：{card['card_type']} ≠ {family}")
        assert "type" not in card, (
            f"{family} 的卡片又留下了裸 type 键——§五.62 偏离④ 已定案改名为 "
            f"card_type（键：{sorted(card)}）")
    # 库里的 `opportunities.type` 没被这次改名牵连：它还在，卡片上照旧印成「类别」
    opportunity_card = next(c for c in hc.collect_opportunity_cards()
                            if c["id"] == opportunity_id)
    assert "类别" in opportunity_card["why_now"], (
        f"发现卡的「类别」那一格没了：{opportunity_card['why_now']}")
    print("[OK] 三类卡片都带 card_type 且无裸 type 键；opportunities.type 仍读成「类别」")


def test_each_region_keeps_at_most_three_cards():
    """判据①：造五条只出三张，**三类都**如此；卡块数／下拉选项数／概览数三处对上

    3 这个数在这里**写死**而不引用 `hc.CARD_CAP`：§二 锁的就是「每类最多 3 条」，
    谁把 CARD_CAP 改了这条就该红（跟着常量走的那半个断言只会跟着一起改，等于没有）。
    """
    goal_id = _make_goal("首页 cap 目标")
    for i in range(5):
        _make_decision(f"cap 决策 {i}")
        _make_task(goal_id, f"cap 待办 {i}")
        _make_opportunity(f"cap 发现 {i}")

    cards, errors = hc.collect_home_cards()
    assert not errors, f"夹具这一轮读失败了：{errors}"
    for card_type in hc.CARD_TYPES:
        assert len(cards[card_type]) == 3, (
            f"{card_type} 出了 {len(cards[card_type])} 张——库里有 5 条候选，"
            f"判据①要求恰好 3 张：{cards[card_type]}")

    (summary, _today_md, _today_dd, _overdue_md, _overdue_dd, task_md, task_dd,
     decision_md, decision_dd, opportunity_md, opportunity_dd) = hc.refresh_home_panel()
    for text, dd, card_type in ((decision_md, decision_dd, hc.CARD_TYPE_DECISION),
                                (task_md, task_dd, hc.CARD_TYPE_TASK),
                                (opportunity_md, opportunity_dd,
                                 hc.CARD_TYPE_OPPORTUNITY)):
        assert text.count("**① ") == 3, f"{card_type} 区渲染出的卡块数不是 3：{text[:200]}"
        assert len(dd.get("choices") or []) == 3, (
            f"{card_type} 的下拉选项数与卡数对不上：{dd}")
    # 概览那四格的词与序按 `CORE_LOOP_BATCH1_SPEC.md` §1.3 逐字：
    # 「今日 N · 持续关注 M · 决策 K · 发现 L」。四个数**在这里现算**而不写死：
    # 本文件没有任何夹具会写 `planned_date`（现读：全文只有 `is_home` 那一格被夹具改过），
    # 所以今日那一格此刻是 0——把这个 0 写死就等于把「往日不计进 N、也不硬凑」那半条
    # 判据偷偷改成「今日恒为 0」。数由 `collect_task_groups()`／`cards` 各取一次，
    # 与它下面那一区的取法同源而**不同调用**，红了能分辨是概览算错还是区里算错。
    groups, group_errors = hc.collect_task_groups()
    assert not group_errors, f"三组这一轮读失败了，上面那些数就没法对：{group_errors}"
    assert len(groups[hc.GROUP_TODAY]) == 0, (
        "前提塌了：今日区居然有货——那下面那句期望前缀就是跟着实现走，不是钉住实现")
    expected = (f"**现在等你看的：0 今日 · {len(cards[hc.CARD_TYPE_TASK])} 持续关注 · "
                f"{len(cards[hc.CARD_TYPE_DECISION])} 决策 · "
                f"{len(cards[hc.CARD_TYPE_OPPORTUNITY])} 发现。**")
    assert summary.startswith(expected), (
        f"概览那四格与「卡块数／下拉选项数」对不上：期望前缀 {expected}；"
        f"实际 {summary[:160]}")
    print("[OK] 造五条时每类恰好三张，卡块数／下拉选项数／概览四格三处一致")


def test_decision_cards_sorted_by_updated_at():
    """§二 锁的是 `updated_at` 倒序：截断之前先排对，留下的不能是「插入顺序的最后三条」

    期望序**手算写死**（五条按创建先后拿到递增的时间戳，倒序前三＝第 5、4、3 条），
    不在测试里重跑一遍同款 sort——拿同一个算法校验同一个算法等于没校验。
    前 5 条留给 cap 用例的候选在这里都更旧，所以它们被截在门外，期望序仍然只有这一种。
    """
    older = [_make_decision(f"排序 {i} 号") for i in range(1, 6)]
    ids = [card["id"] for card in hc.collect_decision_cards()]
    # 手算的期望序：五条按创建先后拿到严格递增的 updated_at，倒序取前三 ⇒ 第 5、4、3 条。
    # cap 用例留下的那五条更旧、被截在门外，所以期望里不含它们——这条顺带也钉住了截断。
    assert ids == [older[4], older[3], older[2]], (
        f"决策卡的序不是 updated_at 倒序的前三条：{ids}（夹具创建序 {older}）")
    assert older[0] not in ids and older[1] not in ids, (
        f"updated_at 最旧的两条挤进来了：{ids}")
    print("[OK] 决策按 updated_at 倒序截断，留下的正是最新三条且顺序正确")


# ---------- 判据②：四要素齐全 ----------

def test_decision_card_carries_all_four_elements():
    """有 summary 的决策卡：①②③④ 四行都在，且每行都带着内容"""
    decision_id = _make_decision("四要素决策", summary="这一轮先把没人看的产出收口")
    card = next(c for c in hc.collect_decision_cards() if c["id"] == decision_id)
    text = hc.render_card(card)
    for marker in ("① ", "② Agent 怎么判断：", "③ 为什么现在重要：", "④ 现在能做什么："):
        assert marker in text, f"四要素缺了「{marker}」这一行：\n{text}"
    assert "这一轮先把没人看的产出收口" in text, f"② 没把 summary 带上：\n{text}"
    assert card["card_type"] == hc.CARD_TYPE_DECISION, "卡片没带 §五 要求的 card_type 字段"
    assert hc.TAB_REVIEW in card["can_do"], f"④ 没交代去哪一页：{card['can_do']}"
    print("[OK] 决策卡四要素齐、② 用的是账本里的 summary、卡片带 card_type")


def test_empty_summary_shows_the_field_is_empty_not_nothing():
    """② 的**合法缺省**长什么样：那一行还在、明说这格是空的，而不是消失或编一句

    §三 原话是「没有就不硬凑」。硬凑有两种都算假话：拿标题复读一遍当判断，
    或整行删掉——后者让人以为四要素本来就少一格。
    """
    decision_id = _make_decision("空 summary 决策", summary="")
    card = next(c for c in hc.collect_decision_cards() if c["id"] == decision_id)
    assert card["agent_take"] is None, f"summary 为空时应留 None：{card}"
    text = hc.render_card(card)
    assert "② Agent 怎么判断：（账本里这一格是空的）" in text, text
    assert text.count("空 summary 决策") == 1, (
        f"标题在卡上出现了不止一次——② 疑似拿它凑了一句判断：\n{text}")
    print("[OK] summary 为空：② 那行仍在且写成「这一格是空的」，没拿标题凑")


def test_task_card_has_no_take_line_at_all():
    """待办：② 整行**不出现**，①③④ 必须在（结构上就没有这一格）

    B1b 之前这条与发现族合着写；现在发现族有 ② 了（读 `ai_reason`），两族的「缺」是两种
    不同的缺——决策那格是「可能有但这次是空的」，待办那格是「压根不该有」。共用一条用例的
    代价是改一族时另一族的断言跟着漂，所以拆开各数各的。
    """
    goal_id = _make_goal("四要素待办目标")
    task_id = _make_task(goal_id, "四要素待办")
    card = next(c for c in hc.collect_task_cards() if c["id"] == task_id)
    text = hc.render_card(card)
    assert "四要素待办" in text, f"① 没带上标题：\n{text}"
    assert "② Agent 怎么判断" not in text, (
        f"待办本不该有 ② 这一格，渲染却写出来了：\n{text}")
    assert "③ 为什么现在重要：" in text and "创建于" in text, f"③ 缺或没内容：\n{text}"
    assert "④ 现在能做什么：" in text and card["can_do"], f"④ 缺：\n{text}"
    assert card["agent_take"] is None, f"待办的 agent_take 该是 None：{card}"
    print("[OK] 待办：② 整行不出现，①③④ 齐且 ③ 带着可核对的信息")


def test_opportunity_take_comes_from_ai_reason_not_summary():
    """发现族的 ②④（B1b 起的改动）：只许搬加工后的那两格，**不许搬原始 summary**

    `opportunities.summary` 是外部资源自己的说明文字（夹具里那句「夹具写的一句话说明」
    就是它）。把它搬进 ② 等于把别人的广告词冒充成助理的判断，正是 §三 那句
    「没有就不硬凑」要防的那一种。所以这里两头都断言：② 必须是 `ai_reason` 那一句，
    整张卡上不许出现 summary 那一句。
    """
    opportunity_id = _make_opportunity("加工过的发现", screened=True,
                                       reason="学生队伍正好能报这条赛道",
                                       next_step="先去报名页确认组队人数上限")
    card = next(c for c in hc.collect_opportunity_cards()
                if c["id"] == opportunity_id)
    text = hc.render_card(card)
    assert card["agent_take"] == "学生队伍正好能报这条赛道", card
    assert card["can_do"] == "先去报名页确认组队人数上限", card
    assert "② Agent 怎么判断：学生队伍正好能报这条赛道" in text, text
    assert "④ 现在能做什么：先去报名页确认组队人数上限" in text, text
    assert "夹具写的一句话说明" not in text, f"原始 summary 又冒上卡片了：\n{text}"
    assert "类别" in text and "资格提示" in text, f"③ 少了可核对的信息：\n{text}"
    assert card["processed"] is True, f"这条加工过了：{card}"
    print("[OK] 发现卡 ②④ 用的是加工后的两格，原始 summary 不在卡上出现")


def test_unprocessed_opportunity_uses_the_two_current_phrases():
    """还没加工的那条：② 逐字仍是 B1b 锁定决定 ③ 那一句，④ 逐字是裁定 ⑤ 改过的那一句

    为什么值得单独钉：这两句是「有真源、只是这一次还没加工到」的说法，写成空行会让人
    以为首页漏了一格，自己凑一句又成了假话。② 那句是设计席定的词、执行席不许顺手机改；
    ④ 那句旧文案「正在加工，刷新后再看」已由设计席 2026-10-17 裁定 ⑤ 判为「本身是谎」
    并授权改成「还没加工出结论」——旧句子留在测试里等于把已作废的口径钉回代码。
    """
    opportunity_id = _make_opportunity("还没加工的发现")
    card = next(c for c in hc.collect_opportunity_cards()
                if c["id"] == opportunity_id)
    assert card["processed"] is False and card["agent_take"] is None, card
    assert card["can_do"] is None, card
    text = hc.render_card(card)
    assert "② Agent 怎么判断：（AI 还没加工这一条，稍后刷新）" in text, text
    assert "④ 现在能做什么：（还没加工出结论，稍后刷新）" in text, text
    assert "正在加工" not in text, f"裁定 ⑤ 作废的那句又冒回来了：\n{text}"
    print("[OK] 未加工的发现卡：② 逐字是锁定决定 ③，④ 逐字是裁定 ⑤ 的新句")


@contextlib.contextmanager
def _processing_error(text: str = "夹具：模型端点不可用"):
    """把「最近一轮加工失败」这件事装进 `_shared.ai_filter_state`，出来时还原

    只在内存里改那一个键——它是**资源页与首页共用的同一个 dict**，留着不还原会让后面的
    用例都看见一句夹具编的失败。进出各断言一次：写错键名不会报错，只会静默多一个键，
    那种状态下「失败态可见」是恒真的假话（口径同 `_trigger_guard` 那句探针）。
    """
    state = app_module.ai_filter_state
    original = state.get("error")
    state["error"] = text
    try:
        assert state.get("error") == text, "失败态没装进 ai_filter_state"
        yield
    finally:
        state["error"] = original
        assert state.get("error") == original, "失败态没还原干净"


def test_failed_processing_says_so_and_stays_distinct_from_unprocessed():
    """裁定 ⑤ 的判据：失败态与未加工态**必须可分**，两句各占一句、不许互抄

    装一次 `error` 再渲染同一批未加工数据，卡片 ④ 从「还没加工出结论」换成「加工失败」；
    两头各断言一句、并断言对方那句**不出现**——只断言「说了失败」的那半条抓不到
    「两态合回一句」这种改法（把 `_FAILED_NEXT_STEP_TEXT` 改成 `_UNPROCESSED_...` 的
    那次变异就红在这里）。
    """
    opportunity_id = _make_opportunity("失败态用的发现")
    before = hc.render_card(next(c for c in hc.collect_opportunity_cards()
                                 if c["id"] == opportunity_id))
    with _processing_error():
        card = next(c for c in hc.collect_opportunity_cards()
                    if c["id"] == opportunity_id)
        assert card["last_processing_failed"] is True, card
        assert card["processed"] is False, card
        text = hc.render_card(card)
        assert "④ 现在能做什么：（加工失败，模型可能不可用）" in text, text
        assert hc._UNPROCESSED_NEXT_STEP_TEXT not in text, (
            f"失败与未加工合回了一句：\n{text}")
    after = hc.render_card(next(c for c in hc.collect_opportunity_cards()
                                if c["id"] == opportunity_id))
    assert hc._UNPROCESSED_NEXT_STEP_TEXT in after, after
    assert hc._FAILED_NEXT_STEP_TEXT not in after, after
    assert after == before, "撤掉 error 之后卡片没回到原样：失败痕迹漏在了别处"
    print("[OK] 失败态那句独立可见，撤掉 error 后回到未加工那句，两态可分")


def test_failure_flag_lifetime_is_one_resource_page_poll():
    """按实登记：`error` 被资源页那个 timer 消费掉就清成 None ⇒ 失败态只活一个轮询周期

    这一条不是判据，是**测出来的限度**（设计席裁定 ⑤ 要求「失败态与未加工态可分」，
    而清除方在资源页、不在首页）。`poll_ai_filter_results` 读走 `report`／`error`
    之后把两格置回 None，所以首页那句「加工失败」只在失败后的第一屏可见。
    首页不为此新建第二套状态（裁定 ② 接受副作用那条同口径）。
    """
    with _processing_error():
        assert app_module.ai_filter_state.get("error"), "夹具没把失败装上"
        res.poll_ai_filter_results("all", "all")
        assert app_module.ai_filter_state.get("error") is None, (
            "本轮读回：timer 那一步没清 error——那 §五.64 记的限度就写错了，得改")
    print("[OK] 实测：poll_ai_filter_results 消费后 error 置回 None（失败态可见期一个周期）")


def test_processed_but_empty_is_not_reported_as_unprocessed():
    """加工过、模型那一次没给这一句 ⇒ **不许**套用「还没加工」那一句

    设计席 B1b 锁定的那句覆盖的是「还没问」这一态；「问过了没答」若也说成「还没问」，
    就是把两态混成一句（本模块文件头第 6 条目点过的那种混法）。这一条钉的是执行席
    按事实补的那两句最小措辞——**已由设计席 2026-10-17 裁定 ③ 原样采纳**（§五.63③ 关闭），
    换词时这里就是换词点。
    """
    opportunity_id = _make_opportunity("加工过但没给话", screened=True, reason="")
    card = next(c for c in hc.collect_opportunity_cards()
                if c["id"] == opportunity_id)
    assert card["processed"] is True, card
    text = hc.render_card(card)
    assert "还没加工" not in text and "正在加工" not in text, (
        f"这条明明加工过了，卡片却说成还没加工：\n{text}")
    assert hc._PROCESSED_EMPTY_TAKE_TEXT in text, text
    assert hc._PROCESSED_EMPTY_STEP_TEXT in text, text
    print("[OK] 加工过但两格为空：说的是「加工过了没给这一句」，不是「还没加工」")


# ---------- B2（2026-10-17）：usable_assets 那一行与排序降权 ----------

def _opportunity_row_count() -> int:
    """`opportunities` 的行数（「弱删除」那头要用一个数来证明没删东西）"""
    conn = md.get_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
    finally:
        conn.close()


def _clear_opportunities() -> None:
    """清掉 `opportunities` 里所有行——**只在需要确定排序的那条用例里用**（夹具特权）

    为什么要清：本文件的夹具数据是**跨用例累积**的（每条都往库里留一行，靠 `_next_day`
    的单调时钟保证「刚造的那条最新」）。这个前提对「这条在不在卡上」够用，对
    「前三条是谁」不够用——B2 之后凡是带 `usable_assets` 的行都会**永久**占掉一个显示位，
    期望序就变成「取决于本文件里谁先跑」。那种断言一改注册顺序就红，红得没有信息量。
    排序这一条要的是**能手算的确定序**，所以先把这一张表清空，再造四条。
    """
    conn = md.get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


def test_assets_line_shown_only_when_present_and_not_numbered():
    """有资源那行逐字是「能直接拿走：…」；空值时**整行不出现**，且四要素形状不变

    两头都要钉：
    - 非空 ⇒ 那一行出现、内容与库里那一格一字不差（裁定 ④：不新增第五格，所以它
      **不带编号**——这里连「⑤」都不许出现，判据① 数的还是 ①②③④）；
    - 空 ⇒ 整行不出现，而且卡片上不许冒出「无直接可利用资源」「没有可用资源」这类
      **冒充判断**的话：这一列的 NULL 同时是「还没加工／加工过但答了『无』／模型漏答」
      三件事，写任何一句「没有」都可能是假话（选择与理由见 `render_card` 那段注释）。
    """
    with_assets = _make_opportunity("有资源的发现", screened=True,
                                    reason="这条能直接拿来用",
                                    next_step="先领额度",
                                    assets="免费 GPU 额度、数据集、开源代码")
    card = next(c for c in hc.collect_opportunity_cards() if c["id"] == with_assets)
    assert card["usable_assets"] == "免费 GPU 额度、数据集、开源代码", card
    text = hc.render_card(card)
    assert "- 能直接拿走：免费 GPU 额度、数据集、开源代码" in text, text
    for marker in ("① ", "② Agent 怎么判断：", "③ 为什么现在重要：", "④ 现在能做什么："):
        assert marker in text, f"多了那一行不该动四要素，缺了「{marker}」：\n{text}"
    assert "⑤" not in text, f"附注行占掉了编号，四要素的形状被改动：\n{text}"

    empty = _make_opportunity("没有资源的发现", screened=True,
                              reason="看着热闹但拿不走东西", next_step="先记着")
    empty_card = next(c for c in hc.collect_opportunity_cards() if c["id"] == empty)
    assert empty_card["usable_assets"] is None, empty_card
    empty_text = hc.render_card(empty_card)
    assert "能直接拿走" not in empty_text, f"空值也把那一行印出来了：\n{empty_text}"
    for liar in ("无直接可利用资源", "没有可用资源", "没有可拿走"):
        assert liar not in empty_text, f"空值被冒充成一句判断（{liar}）：\n{empty_text}"
    # 四要素照旧齐全——省掉一行不等于省掉一格
    for marker in ("① ", "② Agent 怎么判断：", "③ 为什么现在重要：", "④ 现在能做什么："):
        assert marker in empty_text, f"空值那条少了「{marker}」：\n{empty_text}"
    print("[OK] 能直接拿走那行：非空逐字出现、不占编号；空值整行不出现也不冒充判断")


def test_assets_promote_a_card_over_newer_ones_and_window_beats_cap():
    """排序判据（＋窗口那半边）：有资源的那条**压过更新的**，并因此挤掉一条

    这一条同时是「取回窗口必须大于卡片数」的负控点：窗口若退回 `CARD_CAP`，SQL 先按日期
    截三条，`老但有资源` 那条根本进不了重排 ⇒ 它不在卡片上 ⇒ 这里红。也就是说
    「降权」只有在窗口放大之后才是降权，否则当场变成删除（文件头实现读法第 1、8 条）。

    夹具时间戳是单调递增的（`_next_day`），所以四条的日期序就是创建序：
    老但有资源 < 新1 < 新2 < 新3。按日期取前三是 新3/新2/新1；按本判据应是
    老但有资源 排第一，新1 被挤到门外。

    ⚠️ 本用例清过表，也**留了一条带资源的行在库里**：它会占掉后面每条用例的一个显示位
    （剩两个名额）。后面的用例只断言「我自己那条在不在卡上」，两名够用，所以这个残留
    是**按实登记**而不是修掉——真要每条都手算前三，就得每条都清表，那等于把夹具的
    累积约定改成「各扫门前雪」，改动面比这一条用例大。
    """
    _clear_opportunities()
    old_with = _make_opportunity("老但有资源", screened=True,
                                 assets="数据集、社区支持")
    new1 = _make_opportunity("最新但没资源 1")
    new2 = _make_opportunity("最新但没资源 2")
    new3 = _make_opportunity("最新但没资源 3")

    cards = hc.collect_opportunity_cards()
    ids = [c["id"] for c in cards]
    assert len(cards) == hc.CARD_CAP, f"卡片数不等于 cap，这条的期望序要重算：{ids}"
    assert ids == [old_with, new3, new2], (
        f"排序降权没生效（期望：有资源的第一、更新的依次跟上、最旧那条没资源的出局）：{ids}")
    assert new1 not in ids, "有资源的那条没能挤掉任何一条，降权是恒真的"
    # 卡片上带的是库里那一格的原值，且顺序就是上面那个序
    assert cards[0]["usable_assets"] == "数据集、社区支持", cards[0]
    assert cards[1]["usable_assets"] is None, cards[1]
    # 被挤掉的那条**只是没排上**，没被删、也没被改状态（判据 1 与判据 4 是一对）
    still = {r["id"] for r in af.fetch_home_candidates(hc._OPPORTUNITY_SCAN_LIMIT)}
    assert new1 in still, f"被降权的那条从取回集合里消失了——排序写成了筛选：{sorted(still)}"
    print("[OK] 有资源的发现卡压过更新的，被挤掉那条仍在首页取回集合里")


def test_no_assets_opportunity_still_listed_and_not_deleted():
    """弱删除判据（设计席点名的另一半）：没有可用资源的机会照旧搜得到、照旧在库里

    「强过滤弱删除」写反的方式不是把这一句删掉，而是把它做成门槛——界面上一条异常都没有，
    只有那几条机会从此不再出现。所以这里断言的是**两个出口都还开着**：
    ① 资源页按状态筛选仍然列得出来（那是这轮的逃生口，不新做）；
    ② `opportunities` 的行数只增不减（这一轮不硬删任何机会）。
    """
    before = _opportunity_row_count()
    opportunity_id = _make_opportunity("没有可用资源也要留着")
    assert _opportunity_row_count() == before + 1, (
        "建一条就少一条？本轮明令不硬删任何机会")

    panel_md, _dd = res.refresh_opportunities_panel(None, "all", "new")
    assert "没有可用资源也要留着" in panel_md, (
        f"资源页 new 那一档筛不到它（被当已处理隐藏了？）：\n{panel_md[:400]}")

    # 首页那一头：它**在取回窗口里**，有没有挤进前三不作断言（那是排序判据的活）
    window = {r["id"] for r in af.fetch_home_candidates(hc._OPPORTUNITY_SCAN_LIMIT)}
    assert opportunity_id in window, "这一轮里被挤掉的应当只是显示位，不是取回集合"
    print("[OK] usable_assets 为空：资源页筛得到、库里没少行、首页取回窗口里还在")


# ---------- 判据④：筛选正确 ----------

def test_finished_decisions_do_not_appear():
    """已执行／已评估／已否决／信息不足四档都不上首页，只有三档白名单能上

    白名单在测试里**另写一份字面量**（不引用 `hc.HOME_DECISION_STATUSES`）：这条判据要防
    的正是「把已经做完的也算成等你定」，拿实现自己的集合校验实现等于没校验。
    最后一句比对是**方向**用的：先让三条行为断言各管各的，再单独钉「实现与判据一致」，
    红的时候能一眼看出是实现漂了还是判据被改过。
    """
    whitelist = (ds.STATUS_PROPOSED, ds.STATUS_DISCUSSED, ds.STATUS_APPROVED)
    on_home = _make_decision("该出现的 proposed", status=ds.STATUS_PROPOSED)
    gone = {status: _make_decision(f"不该出现的 {status}", status=status)
            for status in (ds.STATUS_EXECUTED, ds.STATUS_EVALUATED,
                           ds.STATUS_REJECTED, ds.STATUS_INSUFFICIENT)}
    ids = [card["id"] for card in hc.collect_decision_cards()]
    assert on_home in ids, f"proposed 反而没上首页：{ids}"
    for status, decision_id in gone.items():
        assert decision_id not in ids, (
            f"{status} 的决策还占着首页（判据④要它消失）：{ids}")
    assert hc.HOME_DECISION_STATUSES == whitelist, (
        f"实现的白名单与判据④分叉：{hc.HOME_DECISION_STATUSES}")
    print("[OK] 四档走完流程的决策都不上首页，proposed 在；白名单与判据一致")


def test_done_tasks_stay_out_and_in_progress_comes_in():
    """pending 与 in_progress 都上首页，done 不出现（组 4，2026-10-18 把 in_progress 接进来）

    集合在测试里**另写一份字面量**（不引用 `hc.HOME_TASK_STATUSES`）：这条判据要防的是
    「实现自己悄悄收回去」，拿实现自己的集合校验实现等于没校验（同上一条决策白名单用例）。
    done 仍在门外这半条**一字未动**：组 4 扩的是状态这一维，勾完就该离开「等你处理」。
    """
    expected = ("pending", "in_progress")
    goal_id = _make_goal("筛选待办目标")
    pending_id = _make_task(goal_id, "该出现的待办", status="pending")
    done_id = _make_task(goal_id, "已完成的待办", status="done")
    progress_id = _make_task(goal_id, "进行中的待办", status="in_progress")
    ids = [card["id"] for card in hc.collect_task_cards()]
    assert pending_id in ids, f"pending 任务没上首页：{ids}"
    assert progress_id in ids, f"in_progress 任务没上首页（组 4 要它在）：{ids}"
    assert done_id not in ids, f"done 任务占着首页：{ids}"
    assert hc.HOME_TASK_STATUSES == expected, (
        f"实现的状态集合与判据分叉：{hc.HOME_TASK_STATUSES}")
    print("[OK] 待办认 pending／in_progress 两档，done 仍不出现；集合与判据一致")


def test_in_progress_card_carries_the_status_and_the_label():
    """进行中的那条：卡片存 `status` 事实、③ 开头带「进行中」；pending 那条两处都干净

    两头都断（该带的带、不该带的没带）：这一格的失败形状有两种——收回去＝进行中那条又不
    显示了（上一条管），**到处都印「进行中」**＝pending 与 in_progress 在 ③ 里分不开，后者
    只有「pending 不许带」这半条能抓。标签写成 ③ 里的前缀而不是新造一张词表，理由见实现
    里那段注释与文件头第 5 条（多一处标签表就多一处会说谎的地方）。
    """
    goal_id = _make_goal("标签组目标")
    progress_id = _make_task(goal_id, "进行中的那条", status="in_progress")
    pending_id = _make_task(goal_id, "没开始的那条", status="pending")
    cards = {c["id"]: c for c in hc.collect_task_cards()}
    prog, pend = cards[progress_id], cards[pending_id]
    assert prog["status"] == "in_progress", f"卡片没存状态事实：{prog}"
    assert pend["status"] == "pending", f"pending 那条的 status 存错了：{pend}"
    assert prog["why_now"].startswith("进行中｜"), (
        f"③ 开头没有『进行中』那三个字：{prog['why_now']}")
    assert "进行中" not in pend["why_now"], (
        f"pending 那条也带上了『进行中』：{pend['why_now']}")
    text = hc.render_card(prog)
    assert "进行中｜目标「标签组目标」" in text, f"渲染出来的 ③ 没有那一段：\n{text}"
    print("[OK] in_progress 卡存 status 且 ③ 以『进行中』开头，pending 卡两处都干净")


def test_task_cards_sort_by_created_at_not_by_status():
    """三条交错：排序键仍是 created_at 倒序，状态不参与（组 4 只扩集合、没改排序）

    造序＝pending 最早 → in_progress 居中 → pending 最新，期望前三就是「最新、居中、最早」。
    谁把「进行中优先」写进排序键这条当场红——那一层改动得先过锚点 §一.11，不在本批。
    组 5 子批 B（2026-10-19）在最前面又加了一层「与当前重点粗匹配者优先」；本夹具一条
    active 重点都没建，那一个分量恒 False ⇒ 顺序仍然只由 created_at 决定，所以这一条
    继续按原样成立（有 active 重点时那一层做了什么，由 `tests/test_task_home.py` 的
    `test_admitted_and_focus_matching_task_beats_a_newer_unrelated_one` 与
    `test_task_card_order_is_the_old_one_when_no_focus_is_active` 两条正反面负责）。
    """
    goal_id = _make_goal("排序交错目标")
    old_pending = _make_task(goal_id, "最早建没开始")
    middle_progress = _make_task(goal_id, "中间建进行中", status="in_progress")
    new_pending = _make_task(goal_id, "最新建没开始")
    ids = [c["id"] for c in hc.collect_task_cards()]
    assert ids[:3] == [new_pending, middle_progress, old_pending], (
        f"待办卡的顺序不再是 created_at 倒序（状态参与了排序？）：{ids[:3]}")
    print("[OK] in_progress 那条夹在两条 pending 中间，位置只由日期决定")


def test_non_new_opportunities_do_not_appear():
    """interested／ignored／favorited 三种都不出现，只有 new 出现

    ⚠️ B1b 之后这一档只是**三条门槛里的第一条**：那两条新的由
    `test_below_threshold_opportunity_is_out`／`test_excluded_eligibility_hint_is_out` 各自
    钉住。状态这一档的真源也从本模块搬走了（`HOME_OPPORTUNITY_STATUS` 已删，界面上不再
    留一个**不参与筛选**的状态常量）——最后一句方向断言因此指向 `core.ai_filter`。
    """
    new_id = _make_opportunity("该出现的新发现", status="new")
    others = {status: _make_opportunity(f"不该出现的 {status}", status=status)
              for status in (OpportunityStatus.INTERESTED, OpportunityStatus.IGNORED,
                             OpportunityStatus.FAVORITED)}
    ids = [card["id"] for card in hc.collect_opportunity_cards()]
    assert new_id in ids, f"new 机会没上首页：{ids}"
    for status, opportunity_id in others.items():
        assert opportunity_id not in ids, f"{status} 的机会占着首页：{ids}"
    assert hc.af.HOME_CANDIDATE_STATUS == "new", (
        f"首页筛的不是 new：{hc.af.HOME_CANDIDATE_STATUS}")
    print("[OK] 发现只认 new，另外三档都不出现（方向断言指向 ai_filter 那一个常量）")


def test_below_threshold_opportunity_is_out():
    """粗筛分数不到门槛的机会不上首页（B1b 新加的第二道门槛）

    压线那条用的是**门槛常量本身**（`>=` 不是 `>`）：改比较符号会在这里红。
    差一点那条用「门槛 − 0.01」而不是写死 0.29，是为了让这个数值不随阈值漂——
    但它漂不漂都由上一条那句方向断言（0.3）看着，两边凑起来才不漏。
    """
    on_home = _make_opportunity("压线够分的发现", score=af.MIN_RELEVANCE_SCORE)
    off_home = _make_opportunity("差一点的发现", score=af.MIN_RELEVANCE_SCORE - 0.01)
    ids = [card["id"] for card in hc.collect_opportunity_cards()]
    assert on_home in ids, f"压线那条被挡在门外了：{ids}"
    assert off_home not in ids, f"分数不够的那条冒上来了：{ids}"
    assert af.MIN_RELEVANCE_SCORE == 0.3, (
        f"门槛被改动（首页与加工队列共用它，改一处就是改两处）：{af.MIN_RELEVANCE_SCORE}")
    print("[OK] 分数 ≥ 门槛才上首页，压线算过、差 0.01 不算；门槛数值仍是 0.3")


def test_excluded_eligibility_hint_is_out():
    """资格提示是排除档的一条都不上首页——分数再高也不行

    这一条顺手钉住「`unknown` 仍然算」：排除档只有一档，把「未知」一起挡掉会让首页
    在最常见的那种数据形状上变成空的（`add_opportunity` 默认就是 unknown）。
    """
    on_home = _make_opportunity("资格待定的发现", hint="unknown")
    off_home = _make_opportunity("明确不符的发现", hint="likely_no", score=1.0)
    ids = [card["id"] for card in hc.collect_opportunity_cards()]
    assert on_home in ids, f"unknown 被误挡了（只该挡排除档）：{ids}"
    assert off_home not in ids, f"likely_no 占着首页：{ids}"
    assert af.EXCLUDED_ELIGIBILITY_HINT == "likely_no", (
        f"排除档被改动：{af.EXCLUDED_ELIGIBILITY_HINT}")
    print("[OK] likely_no 一条都不上首页（满分也不行），unknown 仍算")


def test_archived_goal_tasks_are_out_of_the_scope():
    """归档目标下的 pending 不上首页（执行席按 §七 收窄的那半条，钉成用例）

    为什么这是**定案的一半**而不是漏洞：读取 API `get_tasks()` 必须带 goal_id，
    而 §七 不许改任何回调、本格也不新增 SQL ⇒ 只能「列 active 目标 × 取其首页状态」，
    仓库里同形状的既有先例是 `agents/reminder_agent.py::get_today_pending_tasks`。
    与 §二 那句字面全集有差，执行席回报已单列。要改成全集得先改锚点／根文档——
    这条用例就是那道变更的守门人。
    ⚠️ 组 4（2026-10-18）扩的是**状态**那一维（pending ＋ in_progress），本条守的仍是
    **目标**那一维：归档目标底下不管哪一档都不上首页。
    """
    active_goal = _make_goal("仍在推进的目标")
    archived_goal = _make_goal("已经归档的目标")
    on_home = _make_task(active_goal, "active 目标的待办")
    hc.goal_manager.archive_goal(archived_goal)
    # 故意让归档那条**更新**：它时间上占优却仍被排除，才证明排除靠的是目标状态不是时间
    off_home = _make_task(archived_goal, "归档目标的残留待办")
    assert hc.goal_manager.get_goal(archived_goal).status == "archived", (
        "archive_goal 没把目标改成 archived，本用例的前提出错了")
    ids = [card["id"] for card in hc.collect_task_cards()]
    assert on_home in ids, f"active 目标下的 pending 没上首页：{ids}"
    assert off_home not in ids, f"归档目标下的 pending 冒上来了：{ids}"
    print("[OK] 归档目标下的 pending 不算首页待办（收窄口径已钉住）")


# ---------- 判据③：端到端一条链＋启动回填 ----------

def _review_list_rendered(list_md: str) -> bool:
    """复盘列表那一屏确实渲染出来了（拿人看的那句话当证据，不数组件）"""
    return "等你定" in list_md or "共 " in list_md


def test_end_to_end_jump_detail_approve_execute():
    """首页 → 详情 → 批准 → 标记已执行，一条链走完（四个动作全是既有回调）

    判据③ 原文要的是「点决策卡片→能到详情→能批准→能标记已执行」。界面上的「点」是
    「选下拉＋按跳转按钮」，本用例按**绑定实际会跑的那两个回调**依次调
    `do_home_jump_decision` → `do_view_review_decision`，再按复盘页自己的按钮回调走
    `do_review_approve` → `do_review_mark_executed`，每步回库核对，而不是只看返回文案。
    ⚠️ 后两跳发生在复盘页的回调上，正是 §四「现有回调一字不改，只是首页链接过去」的意思：
    首页自己**不做**批准与标记（§三 ④ 与判据⑤ 的相容读法）。
    """
    decision_id = _make_decision("端到端一条链", summary="先让它可见再谈别的")
    assert decision_id in [c["id"] for c in hc.collect_decision_cards()], "前置：卡片没上首页"

    status_md, list_md, dd_update = hc.do_home_jump_decision(decision_id)
    assert "已把 Decision" in status_md, f"跳转后状态文案没交代去处：{status_md}"
    assert _review_list_rendered(list_md), f"复盘列表没刷出来：{list_md[:200]}"
    assert dd_update.get("value") == decision_id, f"跳转没把这条选中：{dd_update}"

    detail = rc.do_view_review_decision(dd_update.get("value"))
    assert "端到端一条链" in detail, f"详情区读不到这一条：{detail[:200]}"

    rc.do_review_approve(decision_id)
    assert ds.get_decision(decision_id)["status"] == ds.STATUS_APPROVED, "批准没落库"

    rc.do_review_mark_executed(decision_id, "端到端用例的执行痕迹")
    row = ds.get_decision(decision_id)
    assert row["status"] == ds.STATUS_EXECUTED, f"标记已执行没落库：{row}"
    assert row.get("execution_reference") == "端到端用例的执行痕迹", (
        f"执行痕迹没写进去：{row}")

    assert decision_id not in [c["id"] for c in hc.collect_decision_cards()], (
        "已执行的决策还占着首页（判据③与判据④在这一条上交圈）")
    print("[OK] 首页→详情→批准→标记已执行 一条链走完，每步落库，走完即离开首页")


def test_jump_task_selects_it_in_goal_page():
    """待办跳转：目标页详情刷出来、任务下拉选中**这一条**，且一行都不写

    §三 ④ 那格写的是「标记完成」，首页上的读法是「把人放到那一步前面」：下拉选中这条，
    人按目标页原有的「✅ 标记任务完成」才落库。
    """
    goal_id = _make_goal("跳转待办目标")
    task_id = _make_task(goal_id, "要被选中的待办")
    before = _db_fingerprint()
    status_md, goal_dd, detail_md, task_choices, dd_update = hc.do_home_jump_task(task_id)
    assert "已把目标" in status_md and "标记任务完成" in status_md, status_md
    assert goal_dd.get("value") == goal_id, f"目标下拉没选中原目标：{goal_dd}"
    assert "要被选中的待办" in detail_md, f"目标详情没刷出来：{detail_md[:200]}"
    assert dd_update.get("value") == str(task_id), (
        f"完成任务的下拉选中的不是这一条（那一页的 choices 值是字符串）：{dd_update}")
    assert str(task_id) in [value for _label, value in task_choices], (
        f"任务选项列表里根本没有这一条：{task_choices}")
    diff = _fingerprint_diff(before, _db_fingerprint())
    assert not diff, f"待办跳转写了库：{diff}"
    print("[OK] 待办跳转选中那一条、刷新详情，指纹逐项不变")


def test_jump_works_for_an_in_progress_task():
    """跳转的集合跟着扩了：进行中的那条同样能装进目标页并选中，仍一行都不写

    组 4 的边界写的是「跳转仍复用现有跳转回调」，所以这条测的是**集合扩了而回调没变**：
    `do_home_jump_task` 里「找不到就照实说」那一支对 in_progress 不该触发。
    """
    goal_id = _make_goal("进行中跳转目标")
    task_id = _make_task(goal_id, "进行中被跳转", status="in_progress")
    before = _db_fingerprint()
    status_md, goal_dd, detail_md, _choices, dd_update = hc.do_home_jump_task(task_id)
    assert "已把目标" in status_md, (
        f"进行中的那条被当成『不在首页』：{status_md[:160]}")
    assert goal_dd.get("value") == goal_id, f"目标下拉没选中原目标：{goal_dd}"
    assert "进行中被跳转" in detail_md, f"目标详情没刷出来：{detail_md[:200]}"
    assert dd_update.get("value") == str(task_id), (
        f"完成任务的下拉选中的不是这一条：{dd_update}")
    diff = _fingerprint_diff(before, _db_fingerprint())
    assert not diff, f"进行中跳转写了库：{diff}"
    print("[OK] in_progress 那条照样跳转选中，指纹逐项不变")


def test_jump_opportunity_selects_it_in_resource_page():
    """发现跳转：把那一条选中；被筛选挡住时照实说「不在当前视图里」而不是假装选中"""
    opportunity_id = _make_opportunity("要被选中的发现", status="new")
    status_md, _list_md, dd_update = hc.do_home_jump_opportunity(opportunity_id)
    assert dd_update.get("value") == opportunity_id, (
        f"默认筛选视图里没选中这一条：{dd_update}")
    assert "已把机会" in status_md, status_md
    ignored = _make_opportunity("被筛掉的发现", status=OpportunityStatus.IGNORED)
    note_hit, _md, dd_hit = hc.do_home_jump_opportunity(ignored, status="ignored")
    assert dd_hit.get("value") == ignored, f"放宽到 ignored 视图应选中它：{dd_hit}"
    assert "已把机会" in note_hit, note_hit
    note_miss, _md2, dd_miss = hc.do_home_jump_opportunity(ignored, status="new")
    assert dd_miss.get("value") != ignored, (
        "new 视图里没有它，却把 value 留成了它（假装选中）")
    assert "不在资源页当前的筛选视图里" in note_miss, note_miss
    print("[OK] 发现跳转选得中；视图里没有它时照实说")


def test_home_backfill_is_in_demo_load_chain():
    """首页三区挂在 `demo.load` 链尾（判据③「重启后不点刷新也得看见」）

    静态读源码，口径照 `tests/test_review_tab.py` 的同名用例：刻意不再重跑一次
    `create_app()`。切片右边界取链结束后第一条独立语句，断言只可能命中链内。
    """
    source = _source_text(_FACTORY_SOURCE)
    start = source.index("demo.load(")
    end = source.index("new_session_btn.click", start)
    chain = source[start:end]
    assert "refresh_home_panel" in chain, "首页那一段没进启动回填链"
    # 第一批（2026-10-20）把清单抽成 `_home_panel_outputs` 那**一份**唯一清单。
    # 这里再断言字面量 `[home_summary` 钉的是「清单必须内联写」，不是本批要的东西；
    # 现在这条钉的是「接的是那份唯一清单、不是另一份手抄的」——判据⑨ 要防的正是
    # 「加了一格却忘了加 outputs」与「两处各写一遍、迟早与返回值漂移」。
    # 清单本身有几格、每一格是什么，由 `tests/test_today_region.py` 按序号钉死。
    assert "outputs=_home_panel_outputs" in chain, (
        "启动回填接的不是那份唯一清单（两处各写一遍迟早与返回值漂移）")
    print("[OK] 启动回填链里有首页那一段，outputs 接的是那份唯一清单")


def _bound_callbacks(text: str) -> set:
    """一段绑定代码里**真接上去**的回调名（取 `_app_module.<name>` 那种接线形状）

    为什么按前缀取而不是按裸名子串取：`ui/app_factory.py` 现读 184 处 `fn=` 全走
    `_app_module.` 前缀，11 处 lambda 里调用它同样带前缀，所以「接上去」一定留下前缀；
    而注释里提一句函数名（例如「不许借道 `do_start_task`」）**不带**前缀。
    按裸名子串判会让「把注释写空」变成通过这条守卫的唯一办法——那不是判据的目的。
    """
    return set(re.findall(r"_app_module\.(\w+)", text))


def test_home_jump_bindings_reuse_read_callbacks():
    """绑定区静态判据：跳转按钮接的全是读取回调；写回调只许在**那两格**里

    判据⑤ 在**界面接线**这一头的形状：跳转按钮的 fn 只能是 `do_home_*`，其后 `.then`
    只允许 `do_view_review_decision`（读详情）。写回调出现在首页按钮上就红——
    这比只查控制器源码更靠外：接线把写挂上去，控制器再干净也没用。
    组 3（2026-10-18）在目标页新加的 `do_start_task`（写 in_progress）一并进了这张表：
    它现在不在首页绑定区，哪天接上去就该红，而不是安静地成立。
    ⚠️ 组 5 子批 A（2026-10-18，设计席定案「写口放新模块」）之后首页**有**一格会写
    （`ui/controllers/focus_controller.py` 的 `do_focus_*`），所以这一条读成
    「**除那一格外**不许出现写回调」：切片边界是那行 marker 注释。
    ⚠️ 第一批「今日工作流」（2026-10-20，spec §6.3）开出**第二格**会写的地方
    （`📅 今日安排`，放开的只有 `goal_tasks.status` 与 `planned_date` 两列），
    所以这里按**两枚 marker** 切成三段：marker₁ 之前＝B1 判据⑤ 的原辖境（一条写回调
    都不许接）；两枚 marker 之间＝「当前重点」那一格，只许 `do_focus_*`；
    marker₂ 之后＝「今日安排」那一格，接上去的回调名**逐个对上**一张白名单，
    白名单之外多一枚就红。三段各有断言，所以这两个「排除」不会退化成假排除；
    marker 被删、被改字、两枚顺序被换，都会在这里红
    （正向对拍另有一条在 `tests/test_current_focus.py`）。
    """
    source = _source_text(_FACTORY_SOURCE)
    start = source.index("# ==================== 🏠 首页")
    end = source.index("    return demo", start)
    marker = "# -------------------- 🏠 首页 · 🎯 当前重点（写口）--------------------"
    marker_today = ("# -------------------- 🏠 首页 · 📅 今日安排（写口：只有两列）"
                    "--------------------")
    marker_pos = source.index(marker, start)
    today_pos = source.index(marker_today, marker_pos)
    assert marker_pos < today_pos < end, (
        "两枚 marker 不在首页绑定区之内（或顺序被换）：下面那三段切片会圈空或圈重")
    assert source.count(marker) == 1 and source.count(marker_today) == 1, (
        "marker 不唯一：切片按第一处取，两段边界会跟着错位")
    block = source[start:marker_pos]
    focus_block = source[marker_pos:today_pos]
    today_block = source[today_pos:end]
    assert "home_refresh_btn.click" in block, "刷新按钮没绑定"
    for name in ("do_home_jump_decision", "do_home_jump_task",
                 "do_home_jump_opportunity"):
        assert f"fn=_app_module.{name}" in block, f"跳转按钮没接 {name}"
    assert "fn=_app_module.do_view_review_decision" in block, (
        "决策跳转没接上既有详情回调（§四 端到端那一跳断在界面这里）")
    assert "refresh_focus_panel" in block, (
        "「🔄 刷新首页」那一下没带上当前重点那一格的**只读**刷新（它是 .then，不是写）")
    write_callbacks = (
        "do_review_approve", "do_review_reject", "do_review_mark_executed",
        "do_review_evaluate", "do_review_distill", "do_review_discuss",
        "do_complete_task", "do_start_task", "do_save_manual_section", "create_new_goal",
        "do_generate_tasks", "do_update_opportunity_status", "do_run_review_now",
        "do_add_task_home", "do_remove_task_home",
    )
    hit = [name for name in write_callbacks if f"_app_module.{name}" in block]
    assert not hit, (
        f"marker₁ 之前那段（三区＋三个跳转）出现了写回调：{hit}（判据⑤ 的原辖境）")
    borrowed = [name for name in write_callbacks if f"_app_module.{name}" in focus_block]
    assert not borrowed, (
        f"「当前重点」那一格借道了别的写回调：{borrowed}（这一格的写只许走 do_focus_*）")
    borrowed_today = [name for name in write_callbacks
                      if f"_app_module.{name}" in today_block]
    assert not borrowed_today, (
        f"「今日安排」那一格借道了目标页／复盘页那批写回调：{borrowed_today}"
        "（spec §6.3：这一格只许三枚 do_home_*，借道会连 `is_home` 一起写）")
    focus_callbacks = (
        "do_focus_select", "do_focus_add", "do_focus_update", "do_focus_pause",
        "do_focus_complete", "do_focus_preview", "do_focus_confirm", "do_focus_clear",
        "do_focus_adopt", "do_focus_ignore",
    )
    missing = [name for name in focus_callbacks
               if f"fn=_app_module.{name}" not in focus_block]
    assert not missing, (
        f"marker₁ 与 marker₂ 之间少了这些绑定：{missing}——少了就说明这一格没接上或被搬走，"
        "而上面那个『排除』当场就成了假排除")
    leaked = [name for name in focus_callbacks
              if f"fn=_app_module.{name}" in block]
    assert not leaked, f"focus 的写回调漏到了 marker₁ 之前：{leaked}"
    # 「今日安排」那一格的白名单：**逐个对上**，不是「不在禁词表里就算过」。
    # 前三个是 §6.3 点名的三枚首页写回调；后三个是指令第 4 步与 spec §2 逐字要求挂在
    # 这一格上的主流程（「⚡ 帮我排今天」＝`do_generate_daily_plan` 出预览、
    # 「🔁 按此时长重新生成预览」＝同一个回调带小时数、「✅ 确认并应用」＝
    # `do_apply_daily_plan`，写的正是被放开的那一列 `planned_date`，应用后 `.then` 刷首页）。
    # ⚠️ 这三枚不在 §6.3 点名的「借道」名单（`do_start_task`／`do_complete_task`／
    # `do_add_task_home`）里，但白名单的意义就是**不再多第四枚**：将来谁在这一格接别的
    # 写回调，这条当场红，而不是靠「反正禁词表没它」。
    today_allowed = {
        "do_home_start_task", "do_home_complete_task", "do_home_reschedule_today",
        "do_generate_daily_plan", "do_apply_daily_plan", "refresh_home_panel",
    }
    today_callbacks = _bound_callbacks(today_block)
    extra = sorted(today_callbacks - today_allowed)
    assert not extra, (
        f"「今日安排」那一格接了白名单之外的回调：{extra}（这一屏只放开 status 与 "
        "planned_date 两列；主流程那三枚之外不再多接）")
    for name in ("do_home_start_task", "do_home_complete_task",
                 "do_home_reschedule_today"):
        assert f"fn=_app_module.{name}" in today_block, (
            f"今日区那三枚写回调少了 {name}——三枚里有谁被摘掉或改名")
    after_jump = block.split("home_decision_jump_btn.click", 1)[1]
    assert "refresh_home_panel" not in after_jump, (
        "跳转之后又刷首页：读操作不改账本，刷了只会把人刚选中的那张卡清空")
    print(f"[OK] marker₁ 之前零写回调（查了 {len(write_callbacks)} 个名字）、"
          f"「当前重点」{len(focus_callbacks)} 枚 do_focus_* 全在且不借道、"
          f"「今日安排」接的 {sorted(today_callbacks)} 逐个对上白名单"
          "（判据⑤ 读成『除那两格外不许写』）")


# ---------- 判据⑤：零写库、零 LLM ----------

def test_home_reads_write_nothing():
    """把首页所有公开入口跑一遍：逐表行数＋MAX(rowid) 不变，三张真源整行不变

    两道对拍：`_call_everything` 里**贴着每次调用**取的指纹（买到的是归因——红了能指名
    哪一次调用动的库），加上这里首尾各取一次的整库指纹（兜跨调用累积的改动，例如某次
    调用把 `updated_at` 顶了一下、下一次又抹平）。
    ⚠️ 这两道**都抓不到**「只写得进一次」那种写（UNIQUE 去重后，本用例开始时它早已落库），
    那一条由静态禁词那层负责——分工与实测编号见 `_call_everything` 的 docstring。

    B1b 之后这一条测的是**除被授权的触发之外**首页自己零写：`_call_everything` 已经把
    触发桩住了，所以这里对拍的仍然是「首页这一格有没有动账本」。后台加工真写的那几列
    （`ai_*`）由 `core.ai_filter` 负责，那是它的落库路径、也有自己的用例
    （`tests/test_ai_filter.py::test_next_step_persisted_by_batch`），不归首页。
    """
    _make_goal("零写入夹具目标")
    cards, errors = hc.collect_home_cards()
    assert not errors, f"前置读失败，指纹对拍没意义：{errors}"
    assert any(cards[t] for t in hc.CARD_TYPES), (
        "前置没造出任何卡片，这条用例就成了空跑")
    before = _db_fingerprint()
    traces = _call_everything()
    assert not traces, "首页的某一次读取动了库：" + " | ".join(traces)
    diff = _fingerprint_diff(before, _db_fingerprint())
    assert not diff, f"首页读操作写了库（差异逐项列出）：{diff}"
    print(f"[OK] 公开入口全跑一遍，{len(before)} 项指纹逐项不变"
          f"（逐入口 {len(traces)} 次取指纹，零命中）")


def test_home_never_calls_the_model():
    """咽喉点计数桩：`chat_sync`／`stream_chat` 一次都不许被调到

    桩返回一句**不是 JSON 的话**：真有谁在等模型输出，那条链会当场报错，
    比静默通过更容易被发现。finally 里还原，并断言还原成功——桩漏在进程里会让
    后面的用例看起来「本来就不打模型」。

    B1b 把判据⑤ 那句修订成「零**直接** LLM 调用」，这一条因此测的是修订后的口径，
    而且它比修订前更有牙：`_call_everything` 里触发已经被换成记录桩（不会真发起加工），
    所以这里的零命中说的是「首页这几格代码自己一次都没摸模型」——发起那一头允许，
    而发起之后的那一切发生在 `core.ai_filter` 的线程里，由 `tests/test_ai_filter.py` 管。
    反过来说，如果谁把 `ai_filter_opportunities` 直接 call 在首页的渲染路径上，
    这一条会当场红（那是真打咽喉点），而 `test_llm_entry_name_only_inside_the_trigger`
    在同一件事上还有静态那一头。
    """
    import business.model_client as mc
    stub_sync, stub_stream = _ModelStub(), _ModelStub()
    original = (mc.chat_sync, mc.stream_chat)
    mc.chat_sync, mc.stream_chat = stub_sync, stub_stream
    # 先验桩真的装上了：名字写错（例如把 stream_chat 打成别的属性）会**静默加一个新属性**、
    # 真函数仍然活着，那条计数就永远为 0——一条恒真断言。这一句把它变成红。
    assert mc.chat_sync is stub_sync and mc.stream_chat is stub_stream, "桩没装上到咽喉点"
    # 再验计数器本身有牙齿（M8 那轮实测：真往首页塞一次 `chat_sync`，红落在**排在前面的**
    # #7 上——那次调用带的是真网络请求，压根没走到这一条桩。所以「计数为 0」这条断言
    # 有没有能力变红，必须在这里就地证明一次：桩挂着时直接打一发，计数必须 +1。
    # 这一发只进桩、不落网络（桩本身就是被换上去的可调用对象），也不写库。
    mc.chat_sync("探针：只打给桩自己")
    assert stub_sync.count == 1, "桩挂着、打进去却计不上数 ⇒ 下面那条『计数为 0』是恒真的假话"
    stub_sync.calls.clear()
    try:
        _call_everything()
    finally:
        mc.chat_sync, mc.stream_chat = original
    assert stub_sync.count == 0, (
        f"首页打了 {stub_sync.count} 次 chat_sync（判据⑤要求零 LLM）：{stub_sync.calls[:1]}")
    assert stub_stream.count == 0, (
        f"首页打了 {stub_stream.count} 次 stream_chat：{stub_stream.calls[:1]}")
    assert mc.chat_sync is original[0] and mc.stream_chat is original[1], "桩没还原干净"
    print("[OK] 全部公开入口跑完，两个咽喉点计数都为 0，桩已还原")


def test_refresh_triggers_processing_for_home_candidates():
    """判据「相关新机会被自动加工，不点手动按钮」在首页这一头的正向形状

    三个点分别对着 B1b 锁定决定 ④ 的三句原文：① **刷新即发起**（不是等人点按钮）；
    ② limit 用的是首页那一个数（`HOME_PROCESS_LIMIT`，方向断言里另写一份 3）；
    ③ **非阻塞**——`refresh_home_panel` 自己不许等模型，所以量它返回用了多久。
    ⚠️ 这里的「发起」由 `_trigger_guard()` 的桩接住，真请求一次都不会发生（锚点 §五.7）；
    桩记的就是被请求的 limit，所以这条断言有对象，不是「没炸就算过」。
    """
    opportunity_id = _make_opportunity("等着被自动加工的发现")
    _wait_idle()
    snapshot = dict(app_module.ai_filter_state)
    app_module.ai_filter_state.update(running=False, report=None, error=None)
    assert af.has_unprocessed_home_candidates(), (
        f"前置：#{opportunity_id} 这条未加工的候选没进得了队列，本用例会是空跑")
    try:
        with _trigger_guard() as stub:
            started = time.monotonic()
            hc.refresh_home_panel()
            elapsed = time.monotonic() - started
            _wait_idle()
            assert stub.calls == [hc.HOME_PROCESS_LIMIT], (
                f"刷新首页时该发起一轮 limit={hc.HOME_PROCESS_LIMIT} 的加工，"
                f"实际记到 {stub.calls}")
    finally:
        app_module.ai_filter_state.update(snapshot)
    assert elapsed < 2.0, f"refresh_home_panel 等了后台 {elapsed:.1f}s：判据要非阻塞"
    assert hc.HOME_PROCESS_LIMIT == 3, (
        f"首页那一档被改动（发现区就 3 张卡，多送的是首页看不到的条目）："
        f"{hc.HOME_PROCESS_LIMIT}")
    print(f"[OK] 刷新即发起（limit=3、返回只用 {elapsed * 1000:.0f}ms），触发真的走到了")


def test_trigger_reuses_the_shared_state_and_does_not_stack_up():
    """节流复用那**一个** `ai_filter_state`：`running` 已真时一次都不许再发起

    两半各防一种事故。后半句（`is` 同一个 dict 对象）防的是「首页另起一套状态」——
    看着无害，实际让节流彻底失效（两个闸各自数自己的，两个线程摸同一批候选、同一份配额）。
    前半句在复位之后**再发起一次并确认这次真的发起了**：没有那一句，`calls == []`
    可能是恒真的假话（触发整条坏了也会一样绿）。
    """
    assert hc.ai_filter_state is app_module.ai_filter_state, (
        "首页用的不是 `_shared` 那一个 dict——第二套节流被造出来了")
    _make_opportunity("排队等加工的发现")
    _wait_idle()
    snapshot = dict(app_module.ai_filter_state)
    try:
        with _trigger_guard() as stub:
            app_module.ai_filter_state["running"] = True
            hc.ensure_opportunities_processed()
            hc.refresh_home_panel()          # 走整条链：界面上那次刷新
            assert stub.calls == [], f"running=True 时还发起了 {stub.calls}"
            # 复位后再来一次：必须真的发起，才证明上面那句空清单是节流拦的、不是坏了
            app_module.ai_filter_state["running"] = False
            hc.ensure_opportunities_processed()
            _wait_idle()
            assert stub.calls == [hc.HOME_PROCESS_LIMIT], (
                f"复位后仍不发起（{stub.calls}）⇒ 上面那句『一次都没发起』是恒真的假话")
    finally:
        app_module.ai_filter_state.update(snapshot)
    print("[OK] running 时零发起、复位后照常发起；用的还是那一个共享状态 dict")


def test_all_processed_home_candidates_means_no_trigger():
    """首页那几条都加工过了 ⇒ 刷新**一次都不发起**（那道「先问一眼」的闸门的另一半）

    为什么值得和上一条分开：`test_trigger_reuses_the_shared_state_and_does_not_stack_up`
    钉的是「有人在跑就别再排一个」，这一条钉的是「没活可干就别起线程」。首页是
    `demo.load` 每次都跑的，闸门一旦失效，代价是每次打开页面都去摸一次模型、花一次配额，
    而界面上**什么都看不出来**——加工早就做完了，卡片照常显示。
    库里的行是前面十几条用例攒下来的，所以这里必须把「还没加工」这一格一次性抹平
    （夹具特权：生产代码从不这样写），才能构造出「首页有内容、但没有活可干」那一态。
    """
    conn = md.get_connection()
    try:
        conn.execute("UPDATE opportunities SET ai_screened_at = '2026-01-01 00:00:00' "
                     "WHERE ai_screened_at IS NULL OR ai_screened_at = ''")
        conn.commit()
    finally:
        conn.close()
    assert not af.has_unprocessed_home_candidates(), (
        "前置：还有没加工的首页候选，本用例构想的『没活可干』没造出来")
    snapshot = dict(app_module.ai_filter_state)
    app_module.ai_filter_state.update(running=False, report=None, error=None)
    try:
        with _trigger_guard() as stub:
            hc.refresh_home_panel()
            _wait_idle()
            assert stub.calls == [], f"没活可干却发起了加工：{stub.calls}"
        assert hc.collect_opportunity_cards(), (
            "前置塌了：首页一个候选都没有，上面那句『没发起』就可能是空屏造成的假话")
    finally:
        app_module.ai_filter_state.update(snapshot)
    print("[OK] 首页有卡片但都加工过了：一次都不发起（不是空屏造成的恒真）")


def test_llm_entry_name_only_inside_the_trigger():
    """静态那一头（B1b 新增）：`ai_filter_opportunities` 只许出现在触发函数体内

    为什么运行期那两条不够：`_call_everything` 把触发桩住了，所以「谁在调用它」这件事
    在运行期看不出来；而判据⑤ 修订后的口径正是**只有**发起这一处允许碰加工入口。
    将来有人在 `collect_opportunity_cards` 里顺手加一次同步调用（那就是前台等模型），
    这一条会在实现改动的当场红，而不是等某次刷新刚好没数据时才红。
    """
    source = _source_text(_HOME_SOURCE)
    tree = ast.parse(source)
    trigger = next((node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "ensure_opportunities_processed"), None)
    assert trigger is not None, (
        "首页没有 ensure_opportunities_processed 这个函数——触发那头被改名或删掉了，"
        "本文件的判据⑤ 口径（零**直接** LLM 调用、发起是唯一例外）跟着失效")
    inside = _called_names_in(trigger)
    assert "ai_filter_opportunities" in inside, (
        f"触发函数没调加工入口，那它调的是谁：{sorted(inside)}")
    assert "has_unprocessed_home_candidates" in inside, (
        "「先问一眼才起线程」那道闸门没了——首页每次刷新都会白摸一次模型")
    outside = _called_names(_HOME_SOURCE)["calls"] - inside
    assert "ai_filter_opportunities" not in outside, (
        f"触发之外还有地方直接调用加工入口（那就是前台等模型）："
        f"{sorted(n for n in outside if 'ai_filter' in n)}")
    print(f"[OK] 静态：加工入口只出现在触发函数体内（体内 {len(inside)} 个被调用名）")


def test_home_controller_source_has_no_write_or_llm():
    """静态那一头：ast 取 import 根名与被调用名，禁词（全禁表减那两个授权写口）命中即红

    用 ast 而不是扫文本：本模块的注释与 docstring 里**本来就要写**「INSERT／UPDATE／
    模型」这些词来解释为什么不做它们，扫文本会把自己判红（或者逼人把注释写空）。
    口径（spec §6.2，2026-10-20 第一批）＝「除 `update_task_status` 与
    `set_task_planned_date` 这两个授权写口外，无其它写／模型调用」。
    ⚠️ 这一条**不因为减了两项就变松**：下面那三句现算核对「减项只有那两个」
    「`set_task_home`／`add_task`／`execute` 仍在禁词集合里」「两个授权写口必须先在全禁表
    里再被减掉」，把§6.1 那句「其余仍禁」钉成可执行的断言而不是注释。
    而「这两个写口各自只写自己那一列」不由这里钉，由 `tests/test_today_region.py`
    那两条逐列对拍钉。
    """
    info = _called_names(_HOME_SOURCE)
    hit_calls = sorted(info["calls"] & _HOME_FORBIDDEN_CALLS)
    assert not hit_calls, f"home_controller 调用了未获授权的写／模型函数：{hit_calls}"
    hit_roots = sorted(info["roots"] & _FORBIDDEN_IMPORT_ROOTS)
    assert not hit_roots, (
        "home_controller 引入了不该碰的层（agents 会跑复盘、business 里坐着模型客户端）："
        f"{hit_roots}")
    assert "gradio" in info["roots"], (
        "本模块的形状是靠 Gradio 组件回 update；import 里没了 gradio 说明大改过，"
        "本文件的取法要跟着重看")
    removed = sorted(_FORBIDDEN_CALLS - _HOME_FORBIDDEN_CALLS)
    assert removed == sorted(_AUTHORIZED_HOME_WRITES), (
        f"首页禁词集合的减项不是那两个授权写口：{removed}（授权表被偷偷扩项，"
        "这条静态守卫当场失效）")
    for name in ("set_task_home", "add_task", "execute"):
        assert name in _HOME_FORBIDDEN_CALLS, (
            f"全禁表里 {name} 没了或被减掉了——§6.1 那句『其余仍禁』当场成了假话")
    assert "update_task_status" in _FORBIDDEN_CALLS \
        and "set_task_planned_date" in _FORBIDDEN_CALLS, (
        "两个授权写口必须先在全禁表里、再由授权表减掉：少了全禁表那一半，"
        "就证明不了『它曾是被禁的、现在是被授权的』")
    print(f"[OK] 静态扫描：{len(info['calls'])} 个被调用名无一命中禁词"
          f"（减项＝{removed}），import 根名 {sorted(info['roots'])}")


def test_static_write_scan_is_not_vacuous():
    """反向证据：同一个扫描器扫 `review_controller.py` **必须**报出写调用

    扫不出来的扫描器等于没有。复盘页真的写账本，所以它必须命中禁词——命中了才证明上一条
    「零命中」是实现干净而不是词表失效。
    """
    info = _called_names(_REVIEW_SOURCE)
    hits = sorted(info["calls"] & _FORBIDDEN_CALLS)
    assert hits, (
        "review_controller 一个写调用都没扫出来——要么词表失效了（上一条用例是假的），"
        "要么那个文件被大改过（本用例该跟着改）")
    print(f"[OK] 扫描器有牙齿：复盘页命中 {len(hits)} 个写调用 {hits}")


def test_region_read_error_is_not_reported_as_empty():
    """一族读失败：那一区说「读不出来」，另两区照常，**概览那一格不许写 0**

    0 条＝账上没有；读不出来＝这一屏没数出来。把两者写成同一句话是本项目点过名的假话
    形态，而概览行是三区里最先被看到的那一行。桩用「换注册表里那一行」构造，
    不改文件、不动实现（负控第三类：只验断言能否反应实现）。
    """
    _make_goal("错误语义夹具目标")
    _make_decision("错误语义夹具决策")
    _make_opportunity("错误语义夹具发现")
    original = hc._COLLECTORS[hc.CARD_TYPE_TASK]
    boom = RuntimeError("人为让待办这一族读不出来")

    def _raise():
        raise boom

    hc._COLLECTORS[hc.CARD_TYPE_TASK] = _raise
    try:
        cards, errors = hc.collect_home_cards()
        assert cards[hc.CARD_TYPE_TASK] == [], "读失败却留下了半截列表"
        assert hc.CARD_TYPE_TASK in errors, "errors 没登记这一族"
        assert cards[hc.CARD_TYPE_DECISION], "另一族被读失败带崩了"
        (summary, _today_md, _today_dd, _overdue_md, _overdue_dd, task_md, _watch_dd,
         _decision_md, _decision_dd, opportunity_md, _opportunity_dd) = \
            hc.refresh_home_panel()
        assert "读不出来" in task_md, f"待办区没说明读失败：{task_md}"
        assert str(boom) in task_md, f"待办区没带上原因：{task_md}"
        assert _ERROR_COUNT_TEXT in summary, (
            f"概览把读失败混成了 0 条：{summary[:140]}")
        assert str(boom) not in opportunity_md, "错误串位到了别的区"
    finally:
        hc._COLLECTORS[hc.CARD_TYPE_TASK] = original
    assert hc._COLLECTORS[hc.CARD_TYPE_TASK] is original, "桩没还原"
    print("[OK] 一族读失败：该区照实说、概览写「读不出来」、另两区不受影响")


TESTS = [
    test_app_import_builds_home_first,
    test_home_tab_is_first_and_old_fourteen_unchanged,
    test_home_tab_widgets_are_in_component_tree,
    test_jump_targets_are_real_tabs,
    test_card_types_are_exactly_three_and_registered,
    test_every_card_type_has_its_words,
    test_card_field_is_named_card_type_not_type,
    test_each_region_keeps_at_most_three_cards,
    test_decision_cards_sorted_by_updated_at,
    test_decision_card_carries_all_four_elements,
    test_empty_summary_shows_the_field_is_empty_not_nothing,
    test_task_card_has_no_take_line_at_all,
    test_opportunity_take_comes_from_ai_reason_not_summary,
    test_unprocessed_opportunity_uses_the_two_current_phrases,
    test_processed_but_empty_is_not_reported_as_unprocessed,
    test_failed_processing_says_so_and_stays_distinct_from_unprocessed,
    test_failure_flag_lifetime_is_one_resource_page_poll,
    test_assets_line_shown_only_when_present_and_not_numbered,
    test_assets_promote_a_card_over_newer_ones_and_window_beats_cap,
    test_no_assets_opportunity_still_listed_and_not_deleted,
    test_finished_decisions_do_not_appear,
    test_done_tasks_stay_out_and_in_progress_comes_in,
    test_in_progress_card_carries_the_status_and_the_label,
    test_task_cards_sort_by_created_at_not_by_status,
    test_non_new_opportunities_do_not_appear,
    test_below_threshold_opportunity_is_out,
    test_excluded_eligibility_hint_is_out,
    test_archived_goal_tasks_are_out_of_the_scope,
    test_end_to_end_jump_detail_approve_execute,
    test_jump_task_selects_it_in_goal_page,
    test_jump_works_for_an_in_progress_task,
    test_jump_opportunity_selects_it_in_resource_page,
    test_home_backfill_is_in_demo_load_chain,
    test_home_jump_bindings_reuse_read_callbacks,
    test_home_reads_write_nothing,
    test_home_never_calls_the_model,
    test_refresh_triggers_processing_for_home_candidates,
    test_trigger_reuses_the_shared_state_and_does_not_stack_up,
    test_all_processed_home_candidates_means_no_trigger,
    test_llm_entry_name_only_inside_the_trigger,
    test_home_controller_source_has_no_write_or_llm,
    test_static_write_scan_is_not_vacuous,
    test_region_read_error_is_not_reported_as_empty,
]


def main():
    # 整个文件默认桩住自动加工（B1b，2026-10-17）：`refresh_home_panel` 现在会发起一轮
    # 后台加工，而这一轮的默认实现真去打模型——**测试一次都不许碰真网络**（锚点 §五.7）。
    # 放在这一层而不是每条用例里，是因为漏一处就是一次真请求：漏写断言只会让用例变弱，
    # 漏写桩会花配额、拖时间，还可能把结果漂成随网络而定。
    # 要**观察**触发的用例在里面再套一层自己的 `_trigger_guard()`（嵌套是安全的：
    # 进去时记下外层桩、出来时还原成外层桩）。
    try:
        with _trigger_guard():
            for fn in TESTS:
                fn()
    finally:
        cleanup_test_db()


if __name__ == "__main__":
    main()
