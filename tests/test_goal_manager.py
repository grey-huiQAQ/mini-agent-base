# -*- coding: utf-8 -*-
"""
📅 第一批「今日工作流」第 1 步：core 的三个只读共享查询（2026-10-20）

被测的是 `core/goal_manager.py` 里本批新增的三个函数：`list_active_pending_tasks`／
`get_tasks_planned_for`／`get_overdue_tasks`。设计席 spec §4.2 给这一层的落点原文是
「`tests/test_goal_manager.py`（共享函数返回与边界）」，断言 ①② 逐字对上用例：

- **① 「`get_tasks_planned_for("2026-10-20")` 返回 active 目标下 `planned_date==2026-10-20`
  且非 done 的任务」** → `test_planned_query_returns_only_matching_active_non_done_rows`
  （同一天里 done 的那条、排更早／更晚的那两条、从未排期的那条、排期被清成空串的那条、
  以及**挂在已归档目标下**的那天那条，一个都不许出现；出现的那条还得带着 `goal_title`）；
- **② 「`get_overdue_tasks("2026-10-20")` 返回 `planned_date<2026-10-20` 且非空且非 done」**
  → `test_overdue_query_excludes_null_empty_today_and_future`。这一条同时是 spec §4.2 那个
  负控的落点：「让 `get_overdue_tasks` 把 `planned_date` NULL 也算进往日 ⇒ ② 红」——
  夹具专门造了一条 NULL 与一条空串，两者都不许进往日组（那叫「从未排期」，归持续关注）；
- **「边界」那一半**（spec §4.2 给这一文件的第二个词）另有三条：
  `is_home` 不参与 `get_tasks_planned_for` 的筛选（`test_planned_query_does_not_filter_on_is_home`，
  这是 §1.2「矛盾 1 已修正」在 core 这一层的证据，判据 §4.1 在首页那一层的同一件事由
  `tests/test_today_region.py` 负责）；空日期参数一律返回 []
  （`test_shared_queries_refuse_an_empty_date_argument`：拿空串去比会让所有未排期任务冒充
  「今天安排」，或让所有已排期任务冒充「过期」）；三个函数**一格不写**
  （`test_the_three_shared_queries_write_nothing`，逐表整行指纹对拍，口径同
  `tests/test_task_lifecycle.py` 的 `_db_state`）。

⚠️ 本文件**不 import 任何 ui 模块**：`ui/controllers/_shared.py` 在导入期就调 `init_db()`，
只要 import 了 UI 链，取证与用例都会在生产库上跑迁移（仓库既有教训）。这里只碰
`memory.database` 与 `core.goal_manager`，切库在那两次 import 之前完成。
⚠️ 日期用**固定字面量**而不是「今天」：spec §4.2 的断言原文写的就是「2026-10-20」，
跟着它写死，跨天重跑不会因为时钟漂移变成另一件事（同口径见 `tests/test_daily_planning.py`
里那个 `2099-01-01`）。
⚠️ 夹具用裸 SQL 写列（`status`／`planned_date`），因为要走的那几档形状（空串排期、
in_progress）没有对应的公开写口；清理只按本文件记下的那几个 id 删。
⚠️ 中文串里一律用「」而不用 ASCII 双引号。
"""

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core.goal_manager import GoalManager  # noqa: E402

md.init_db()

#: spec §4.2 断言里那个日期，逐字照抄（「2026-10-20」是本批的「今天」）
PLAN_DATE = "2026-10-20"
EARLIER_DATE = "2026-10-19"
FAR_EARLIER_DATE = "2025-12-31"
LATER_DATE = "2026-10-21"

gm = GoalManager()

_created_goals: List[str] = []
_created_tasks: List[int] = []


# ---------- 夹具 ----------

def _execute(sql: str, params: tuple = ()) -> int:
    conn = md.get_connection()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _set_column(row_id: int, column: str, value: Any) -> None:
    _execute(f"UPDATE goal_tasks SET {column} = ? WHERE id = ?", (value, int(row_id)))


def _make_goal(title: str, archived: bool = False) -> str:
    goal_id = gm.create_goal(title, "共享查询夹具目标").goal_id
    _created_goals.append(goal_id)
    if archived:
        gm.archive_goal(goal_id)
    return goal_id


def _make_task(goal_id: str, title: str, planned: Optional[str],
               status: str = "pending", home: Optional[bool] = None,
               priority: int = 1) -> int:
    """建一条任务并把 `planned_date`／`status` 造成指定形状，返回任务 id

    `planned=None` ⇒ 这一列**从不被写过**（DDL 默认 NULL，即「从未排期」）；
    `planned=""` ⇒ 显式写成空串（这是另一档形状，spec §1.2 把两档一起排除出往日组）。
    `home=None` ⇒ 不碰 `is_home`（DDL 默认 0，即「没准入首页」）。
    """
    gm.add_task(goal_id, title, priority=priority)
    conn = md.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM goal_tasks WHERE goal_id = ? AND title = ? ORDER BY id DESC",
            (goal_id, title)).fetchone()
        assert row is not None, f"add_task 没落库：{title}"
        task_id = int(row[0])
    finally:
        conn.close()
    _created_tasks.append(task_id)
    if planned is not None:
        _set_column(task_id, "planned_date", planned)
    if status != "pending":
        _set_column(task_id, "status", status)
    if home is not None:
        assert gm.set_task_home(task_id, home) is True, f"夹具没能写 is_home：{title}"
    return task_id


def _ids(rows: List[Dict]) -> List[int]:
    return sorted(int(row["id"]) for row in rows)


def _db_fingerprint() -> Dict[str, List[tuple]]:
    """逐表整行内容（只数行数抓不到原地 UPDATE，所以按整行取回）"""
    conn = md.get_connection()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()]
        out: Dict[str, List[tuple]] = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = [tuple(r) for r in rows]
        return out
    finally:
        conn.close()


def _reset() -> None:
    """每条用例从同一起点开始：只删本文件夹具记下的那几行"""
    if _created_tasks:
        _execute("DELETE FROM goal_tasks WHERE id IN ("
                 + ",".join("?" * len(_created_tasks)) + ")", tuple(_created_tasks))
        _created_tasks.clear()
    if _created_goals:
        _execute("DELETE FROM goals WHERE goal_id IN ("
                 + ",".join("?" * len(_created_goals)) + ")", tuple(_created_goals))
        _created_goals.clear()


# ---------- ① 今日那一把尺 ----------

def test_planned_query_returns_only_matching_active_non_done_rows():
    """断言 ①：只留「active 目标 × 非 done × planned_date == 给定日」，并把 goal_title 附上"""
    active = _make_goal("今日尺的活跃目标")
    archived = _make_goal("今日尺的已归档目标", archived=True)
    hit = _make_task(active, "排在这天且未完成", planned=PLAN_DATE)
    _make_task(active, "排在这天但已完成", planned=PLAN_DATE, status="done")
    _make_task(active, "排在更早的那天", planned=EARLIER_DATE)
    _make_task(active, "排在更晚的那天", planned=LATER_DATE)
    _make_task(active, "从未排期的那条", planned=None)
    _make_task(active, "排期被清成空串", planned="")
    _make_task(archived, "归档目标里排在这天的", planned=PLAN_DATE)

    rows = gm.get_tasks_planned_for(PLAN_DATE)
    assert _ids(rows) == [hit], f"今日尺留错了行：{_ids(rows)}，期望 [{hit}]"
    row = rows[0]
    assert row["goal_id"] == active, f"行里的 goal_id 不对：{row}"
    assert row["goal_title"] == "今日尺的活跃目标", (
        f"共享查询没把目标名带下来（首页那一格就印不出「属于哪个目标」）：{row}")
    print("[OK] 今日尺＝active×非done×同日一条，行里带 goal_id／goal_title")


def test_planned_query_does_not_filter_on_is_home():
    """边界：`is_home` 不参与「今日」的筛选（spec §1.2 矛盾 1 的修正点，落在 core 这一层）

    两条都排在同一天、都没勾完成，只差 `is_home`：0 的那条**必须在**结果里。
    spec §4.1 的负控（「在 `get_tasks_planned_for` 里加 `and is_home != 0`」）摘的就是这一格，
    红点应当落在这条与首页那条用例上，而不是悄悄被别的断言挡住。
    """
    active = _make_goal("准入无关的目标")
    not_admitted = _make_task(active, "没准入首页但排在今天", planned=PLAN_DATE)
    admitted = _make_task(active, "准入首页且排在今天", planned=PLAN_DATE, home=True)
    outsider = _make_task(active, "准入首页但没排期", planned=None, home=True)

    ids = _ids(gm.get_tasks_planned_for(PLAN_DATE))
    assert not_admitted in ids, f"is_home=0 的那条被今日尺挡掉了（准入制混进了今日）：{ids}"
    assert admitted in ids, f"准入那条反而不在今日尺里（夹具坏了）：{ids}"
    assert outsider not in ids, f"没排期的条目混进今日尺：{ids}"
    print("[OK] 今日尺不看 is_home：0 与 1 两条都在，没排期那条不在")


# ---------- ② 往日那一组 ----------

def test_overdue_query_excludes_null_empty_today_and_future():
    """断言 ②：`planned_date` 非空且早于给定日且非 done；NULL／空串／当天／将来都不算"""
    active = _make_goal("往日尺的活跃目标")
    archived = _make_goal("往日尺的已归档目标", archived=True)
    hit_recent = _make_task(active, "昨天没过完的", planned=EARLIER_DATE)
    hit_far = _make_task(active, "很久前没过完的", planned=FAR_EARLIER_DATE,
                         status="in_progress")
    _make_task(active, "今天也要做的", planned=PLAN_DATE)
    _make_task(active, "将来才做的", planned=LATER_DATE)
    _make_task(active, "往日但已完成", planned=EARLIER_DATE, status="done")
    _make_task(active, "从未排期（NULL）", planned=None)
    _make_task(active, "排期是空串", planned="")
    _make_task(archived, "归档目标里的往日任务", planned=EARLIER_DATE)

    ids = _ids(gm.get_overdue_tasks(PLAN_DATE))
    assert ids == sorted([hit_recent, hit_far]), (
        f"往日组留错了行：{ids}，期望 {sorted([hit_recent, hit_far])}")
    for row in gm.get_overdue_tasks(PLAN_DATE):
        planned = str(row.get("planned_date") or "")
        assert planned and planned < PLAN_DATE, f"混进了一条不该算往日的：{row}"
    print("[OK] 往日组＝非空且早于给定日且非 done（NULL 与空串都被挡在门外）")


# ---------- 通用枚举：三组共用的那一份取行 ----------

def test_active_pending_enumeration_carries_goal_title_and_keeps_is_home_rows():
    """枚举只做两件事：排除 done、排除非 active 目标，并给每行补上目标名

    `is_home` 两档都**必须还在**（准入筛选是 ui 层的活，spec §1.2）；`in_progress` 也在
    集合里（本批的今日／持续关注都要看得见进行中的那条）。
    """
    active = _make_goal("枚举的活跃目标")
    archived = _make_goal("枚举的已归档目标", archived=True)
    off_home = _make_task(active, "枚举里没准入的", planned=None, home=False)
    on_home = _make_task(active, "枚举里准入的", planned=PLAN_DATE, home=True)
    running = _make_task(active, "枚举里进行中的", planned=PLAN_DATE,
                         status="in_progress", home=True)
    _make_task(active, "枚举里已完成的", planned=PLAN_DATE, status="done")
    _make_task(archived, "枚举里归档目标的", planned=PLAN_DATE)

    rows = gm.list_active_pending_tasks()
    ids = _ids(rows)
    assert ids == sorted([off_home, on_home, running]), (
        f"枚举集合不对：{ids}，期望 {sorted([off_home, on_home, running])}")
    by_id = {int(row["id"]): row for row in rows}
    assert by_id[off_home].get("goal_title") == "枚举的活跃目标"
    assert by_id[off_home].get("goal_id") == active
    assert int(by_id[off_home].get("is_home") or 0) == 0
    assert int(by_id[on_home].get("is_home") or 0) == 1, (
        "枚举把 is_home 丢了／改写了——它只该原样带着这一列")
    assert by_id[running].get("status") == "in_progress"
    for column in ("planned_date", "is_home", "priority", "created_at", "source"):
        assert column in by_id[on_home], f"行里少了 {column} 那一格（首页与提醒都读它）"
    print("[OK] 枚举＝非active排除＋非done排除，is_home 两档都在，列齐")


def test_shared_queries_refuse_an_empty_date_argument():
    """空／None 的日期参数返回 []：不许拿空串冒充「今天」或「过期」"""
    active = _make_goal("空参数守卫的目标")
    _make_task(active, "有排期的一条", planned=PLAN_DATE)
    _make_task(active, "没排期的一条", planned=None)
    assert gm.list_active_pending_tasks(), "夹具没造出行，下面那四条空集断言是空的"
    assert gm.get_tasks_planned_for("") == [], "空串日期参数不该返回任何行"
    assert gm.get_tasks_planned_for(None) == [], "None 日期参数不该返回任何行"
    assert gm.get_overdue_tasks("") == [], "空串 today 会把全部已排期任务判成过期"
    assert gm.get_overdue_tasks(None) == [], "None today 同上"
    print("[OK] 两把尺的空日期参数四档都返回 []，且夹具本身非空")


# ---------- 只读 ----------

def test_the_three_shared_queries_write_nothing():
    """spec §5.1「core 共享查询是纯只读（SELECT，无 UPDATE/INSERT）」：逐表整行指纹对拍

    对拍覆盖全库而不是只看 goal_tasks：`goal_tasks`／`goals` 的原地 UPDATE、
    `profile` 或 `agent_states` 那种「一打开页面就写一行」的副作用，都会在这份指纹里露出来。
    """
    active = _make_goal("只读对拍的目标")
    _make_task(active, "只读对拍的任务", planned=PLAN_DATE, home=True)
    before = _db_fingerprint()

    rows = gm.list_active_pending_tasks()
    assert rows, "夹具没造出行，下面的对拍是空的"
    gm.get_tasks_planned_for(PLAN_DATE)
    gm.get_tasks_planned_for(EARLIER_DATE)
    gm.get_overdue_tasks(PLAN_DATE)
    gm.get_overdue_tasks("2020-01-01")

    after = _db_fingerprint()
    assert after == before, (
        f"共享查询写了库，差在：{[t for t in after if after[t] != before.get(t)]}")
    print("[OK] 三次枚举＋四次筛选跑完，逐表整行指纹一字不变")


TESTS = [
    test_planned_query_returns_only_matching_active_non_done_rows,
    test_planned_query_does_not_filter_on_is_home,
    test_overdue_query_excludes_null_empty_today_and_future,
    test_active_pending_enumeration_carries_goal_title_and_keeps_is_home_rows,
    test_shared_queries_refuse_an_empty_date_argument,
    test_the_three_shared_queries_write_nothing,
]


def main():
    try:
        for fn in TESTS:
            _reset()
            fn()
    finally:
        _reset()
        cleanup_test_db()


if __name__ == "__main__":
    main()
