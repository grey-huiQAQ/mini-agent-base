# -*- coding: utf-8 -*-
"""
📅 首页「今日安排」那一区的测试（P7 阶段二 第一批「今日工作流」，2026-10-20）

规格出处＝`CORE_LOOP_BATCH1_SPEC.md` §4 的第 **1／3／4／5／9** 条，用例符号名逐字照该节。
测的是 `ui/controllers/home_controller.py` 里新下沉的那三组（今日／往日／持续关注）
与首页那三枚写回调，外加 `ui/app_factory.py` 里那一条启动回填链。逐条对上：

- **判据 1「今日任务不用 `is_home` 筛选」**（spec §0 矛盾 1 的修正点）→
  `test_today_shows_planned_even_if_not_home`：夹具造一条 `is_home=0`、
  `planned_date=今天`、未完成的任务，它必须进 `T_today` 并把标题渲染进今日那一区。
  负控＝往 `core/goal_manager.py::GoalManager.get_tasks_planned_for` 里加一句
  `and is_home != 0` ⇒ 红在本条（那条从今日区消失）。
- **判据 3「往日未完成展示 ＋ 安排到今天不碰 status」** →
  `test_overdue_shows_and_reschedules_without_touching_status`：① 往日条目渲染在往日子分组；
  ② 点「📌 安排到今天」后它离开往日组、进今日组；③ **全行对拍**只有 `planned_date` 变
  （`status` 也在「不许动」那一列里）。
  负控甲＝摘掉 `get_overdue_tasks` 里 `status != 'done'` 那半 ⇒ 已完成的往日任务冒出来，
  红在本条①；负控乙＝把「安排到今天」改成走 `update_task_status` ⇒ 红在本条③。
- **判据 4「持续关注去重」** → `test_watch_excludes_today_and_overdue`：同一条任务排到今天
  （或已过期）之后，不得在持续关注那一组、那一串卡片、那一片渲染里再出现一次。
  负控＝去掉 `_group_rows()` 里「减去 今日∪往日」那一句 ⇒ 它出现两次，红在本条。
- **判据 5「首页写口只开两列」** → `test_home_start_complete_reschedule_write_only_their_column`：
  三枚回调各写自己那一列，`is_home`／`priority`／`source`／`milestone_id`／`created_at`
  ＋ 同表其余行逐列不变（对拍写法照 `tests/test_task_lifecycle.py`）。
  负控＝在 `do_home_reschedule_today` 里顺手补一句 `status='in_progress'` ⇒ 红在本条。
- **判据 9「启动即见今日区」** → `test_home_load_shows_today_region`：`demo.load` 链尾那条
  `refresh_home_panel` 的 outputs 就是首页那十一格那份清单（静态读源码，口径照
  `tests/test_home_dashboard.py::test_home_backfill_is_in_demo_load_chain`），且运行期返回的
  第 2／4／6 格里真的带着今日／往日／持续关注那三片内容。
  负控＝从 `refresh_home_panel` 的返回值里漏掉今日组 ⇒ 红在本条（位置与内容两头都对不上）。

⚠️ 两条**结构性**前提也钉在这里，因为它们是上面那几条的红能不能落地的地基：
`test_three_groups_are_pinned_even_when_empty`（三组键恒在：今日空≠没有这一组）与
`test_overdue_group_keeps_unscheduled_tasks_out`（`planned_date` 为 NULL／空串**不算**往日，
spec §1.2 括弧里那句；这条一旦退化成「所有未排期都过期」，判据 3 的①就成了假话）。

排序键（spec §1.3 的 R1／R2）不另开用例，随判据 1／3 的夹具各钉一句。

⚠️ 导入顺序即隔离顺序：`ui/controllers/_shared.py` 在**导入期**调 `init_db()`，
所以必须先 `setup_test_db()` 再碰任何 UI／core 模块，否则本文件一 import 就往生产库建表。
⚠️ 本文件**不 import `app`**：`home_controller` 只依赖 core 与 `_shared`，用例要的
「三组与写回调」全在控制器这一层；界面那一头按既有惯例**静态读** `ui/app_factory.py`。
⚠️ 中文串里一律用「」而不用 ASCII 双引号。
"""
import ast
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core import ai_filter as af  # noqa: E402 （后台加工那个入口要被桩住）

md.init_db()

from ui.controllers import home_controller as hc  # noqa: E402

_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")

#: `refresh_home_panel()` 应该返回几格。**这里写死一个数而不是从实现推**：
#: 从 `len(hc.refresh_home_panel())` 推就等于「实现返回几个就算几个」，而那一条判据
#: （§4 第 9 条「outputs 数量与组件一致」）要防的正是首页加了一格却忘了加 outputs。
_EXPECTED_OUTPUT_COUNT = 11

#: 今日区那个 Markdown 与往日区那个 Markdown 在返回值里的位置（0 基）。
#: 同样写在测试里而不是读实现：位置错了界面就会把往日的文字刷到今日那一格上。
_TODAY_MD_INDEX = 1
_OVERDUE_MD_INDEX = 3
_WATCH_MD_INDEX = 5


# ---------- 夹具 ----------

def _today() -> str:
    return hc.home_today()


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def _make_goal(title: str) -> str:
    return hc.goal_manager.create_goal(title, "今日区夹具目标").goal_id


def _make_task(goal_id: str, title: str, status: str = "pending",
               planned=None, is_home: int = 0, priority: int = 1) -> int:
    """建一条任务并按参数把 `status`／`planned_date`／`is_home`／`priority` 摆好

    夹具走 SQL 直接摆列，不借被测试的那三枚按钮（借了就等于「用它自己证明它自己」）；
    形状照 `tests/test_task_lifecycle.py::_make_task` 与
    `tests/test_home_dashboard.py::_make_task`。**默认 `is_home=0`**：判据 1 要的正是
    「没准入也进今日区」，而本文件要上持续关注那一组的那几条会显式传 1。
    """
    hc.goal_manager.add_task(goal_id, title, priority=priority)
    conn = md.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM goal_tasks WHERE goal_id = ? AND title = ? ORDER BY id DESC",
            (goal_id, title)).fetchone()
        assert row is not None, f"add_task 没落库：{title}"
        task_id = int(row[0])
        conn.execute("UPDATE goal_tasks SET status = ?, planned_date = ?, is_home = ? "
                     "WHERE id = ?", (status, planned, is_home, task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id


def _task_row(task_id: int) -> dict:
    """一条任务的**整行**（全行对拍用的就是它，写法照 `tests/test_task_lifecycle.py`）"""
    conn = md.get_connection()
    try:
        row = conn.execute("SELECT * FROM goal_tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None, f"任务行不见了：{task_id}"
        return dict(row)
    finally:
        conn.close()


def _task_table_map() -> dict:
    """{任务 id: 那一行的 repr}——全行对拍的「其余行不许动」就靠它逐键比"""
    conn = md.get_connection()
    try:
        rows = conn.execute("SELECT * FROM goal_tasks ORDER BY rowid").fetchall()
        return {int(row["id"]): repr(tuple(row)) for row in rows}
    finally:
        conn.close()


def _changed_columns(task_id: int, before: dict) -> list:
    after = _task_row(task_id)
    return sorted(k for k in after if after.get(k) != before.get(k))


def _ids(rows) -> list:
    return [int(row["id"]) for row in rows]


def _source_text(rel_path: str) -> str:
    with open(rel_path, encoding="utf-8") as fh:
        return fh.read()


def _home_panel_outputs() -> list:
    """从 `ui/app_factory.py` 源码里取出那份首页 outputs 清单（ast，不重跑 create_app）"""
    tree = ast.parse(_source_text(_FACTORY_SOURCE))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_home_panel_outputs"
                for t in node.targets):
            assert isinstance(node.value, ast.List), (
                "`_home_panel_outputs` 不再是字面清单，本用例的取法要跟着重看")
            names = []
            for elt in node.value.elts:
                assert isinstance(elt, ast.Name), (
                    f"清单里出现了非组件名的项：{ast.dump(elt)[:60]}")
                names.append(elt.id)
            return names
    raise AssertionError("ui/app_factory.py 里找不到 `_home_panel_outputs` 这份清单")


class _NoNetworkTrigger:
    """把首页刷新末尾那一步「发起后台加工」换掉的桩（本文件不造机会，桩只是护栏）

    换的是 `core.ai_filter` 的**模块属性**，而 `ensure_opportunities_processed` 里调的是
    `af.ai_filter_opportunities(...)`（属性在调用那一刻才查），所以这层替换真拦得住。
    它同时是探针：一旦本文件哪条用例把「有未加工候选」的状态造出来了，这里会记一次，
    而那条用例的意图就不是「测加工」——测到了就打真网络，故直接红。
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError(
            "首页自动加工真的被发起了：本文件不测 AI 加工，任何一次发起都可能是真网络请求")


# ---------- 判据 1 ----------

def test_today_shows_planned_even_if_not_home():
    """`is_home=0` 但排到今天、又没完成 ⇒ 必须出现在「今日安排」（spec §0 矛盾 1）"""
    goal_id = _make_goal("今日区夹具·未准入")
    kept_out = _make_task(goal_id, "未准入但排到今天", planned=_today())
    assert int(_task_row(kept_out)["is_home"]) == 0, (
        "夹具没能造出「未准入」的那条：判据 1 的前提没了")
    assert _task_row(kept_out)["planned_date"] == _today(), (
        "夹具没能把排期写成今天：判据 1 的前提没了")
    # 同一目标再放一条**进行中**的今日任务（同样 `is_home=0`），顺手钉 spec §1.3 的
    # R1 排序：进行中在前
    started = _make_task(goal_id, "未准入但排到今天·进行中",
                         planned=_today(), status="in_progress")

    groups, errors = hc.collect_task_groups()
    assert not errors, f"三组读取报了错：{errors}"
    today_ids = _ids(groups[hc.GROUP_TODAY])
    assert kept_out in today_ids, "未准入首页的那条没进今日组（今日那把尺不该看 is_home）"
    region = hc.render_task_region(groups[hc.GROUP_TODAY], hc.GROUP_TODAY)
    assert "未准入但排到今天" in region, f"今日区没渲染出那条的标题：{region[:200]}"
    assert today_ids.index(started) < today_ids.index(kept_out), (
        "R1 排序键不是「进行中在前」（今日区那一句「进行中在前」就成了假话）")
    print("[OK] 判据 1：未准入首页但排到今天、未完成的任务仍在今日区（且进行中在前）")


# ---------- 结构性前提 ----------

def test_three_groups_are_pinned_even_when_empty():
    """三组的键恒在，且「今日空」说的是**这一组空**而不是「这一组读不出来」"""
    groups, errors = hc.collect_task_groups()
    for key in (hc.GROUP_TODAY, hc.GROUP_OVERDUE, hc.GROUP_WATCH):
        assert key in groups, f"三组里少了键 {key}（界面那一层按这三档取，少一个就 KeyError）"
    assert not errors, f"三组读取报了错（本文件没造任何读失败状态）：{errors}"
    empty_today = hc.render_task_region([], hc.GROUP_TODAY)
    assert "⚡ 帮我排今天" in empty_today and "✅ 确认并应用" in empty_today, (
        f"今日空态没把人指向那条主流程：{empty_today[:160]}")
    assert "（" in empty_today, "空态没有用括号那一句的形状（与另两区不一致）"
    print("[OK] 三组键恒在；今日空态说的是「今天没有排期」并指向「⚡ 帮我排今天」")


def test_overdue_group_keeps_unscheduled_tasks_out():
    """`planned_date` 为 NULL／空串**不算**往日（spec §1.2 括弧里那句）"""
    goal_id = _make_goal("往日夹具·从未排期")
    never = _make_task(goal_id, "从未排期的那条", is_home=1)
    blank = _make_task(goal_id, "排期是空串的那条", planned="", is_home=1)

    groups, _ = hc.collect_task_groups()
    overdue_ids = _ids(groups[hc.GROUP_OVERDUE])
    assert never not in overdue_ids and blank not in overdue_ids, (
        "未排期（NULL／空串）被算进了往日：那条一旦冒进「往日」，判据 3 的①就成了假话")
    empty_overdue = hc.render_task_region([], hc.GROUP_OVERDUE)
    assert "持续关注" in empty_overdue, (
        f"往日空态没把「从未排期」那批指到持续关注去：{empty_overdue[:160]}")
    print("[OK] 往日那一组只收「排过期又没做完」的，NULL／空串留在持续关注")


# ---------- 判据 3 ----------

def test_overdue_shows_and_reschedules_without_touching_status():
    """往日条目渲染在往日子分组；点「安排到今天」只写 `planned_date`、不碰 `status`"""
    goal_id = _make_goal("往日夹具·安排到今天")
    older = _make_task(goal_id, "三天前没做完", planned=_day(-3), is_home=1)
    recent = _make_task(goal_id, "昨天没做完", planned=_day(-1), is_home=1)

    groups, _ = hc.collect_task_groups()
    overdue_ids = _ids(groups[hc.GROUP_OVERDUE])
    for tid in (older, recent):
        assert tid in overdue_ids, f"往日任务 {tid} 没进往日组"
    region = hc.render_task_region(groups[hc.GROUP_OVERDUE], hc.GROUP_OVERDUE)
    assert "昨天没做完" in region and "三天前没做完" in region, (
        f"往日子分组没渲染出标题：{region[:220]}")
    assert overdue_ids.index(recent) < overdue_ids.index(older), (
        "R2 排序键不是「最近过期优先」（往日区那一句「按最近过期优先」就成了假话）")

    before = _task_row(recent)
    outs = hc.do_home_reschedule_today(recent)
    assert len(outs) == _EXPECTED_OUTPUT_COUNT, (
        f"「安排到今天」返回的不是首页那 {_EXPECTED_OUTPUT_COUNT} 格：{len(outs)}")

    changed = _changed_columns(recent, before)
    assert changed == ["planned_date"], (
        f"点「安排到今天」之后变的不止 planned_date：{changed}（status 也在不许动那一列里）")
    after = _task_row(recent)
    assert after["planned_date"] == _today(), (
        f"新的计划日期不是今天：{after['planned_date']!r}")
    assert after["status"] == before["status"], "往日的 status 被改了"
    assert older in _ids(hc.collect_task_groups()[0][hc.GROUP_OVERDUE]), (
        "没人点它的那条被一起改期了（spec §3.2「不自动改任务日期」）")

    groups2, _ = hc.collect_task_groups()
    assert recent not in _ids(groups2[hc.GROUP_OVERDUE]), "安排到今天之后还留在往日组"
    assert recent in _ids(groups2[hc.GROUP_TODAY]), "安排到今天之后没进今日组"
    print("[OK] 判据 3：往日条目在往日子分组（最近过期优先），安排到今天只写 planned_date、"
          "同一条随即从往日组挪进今日组")


# ---------- 判据 4 ----------

def test_watch_excludes_today_and_overdue():
    """出现在今日／往日组的任务，不得在持续关注那一组与那片渲染里重复出现"""
    goal_id = _make_goal("去重夹具")
    today_task = _make_task(goal_id, "今天这条也要去重", planned=_today(), is_home=1)
    overdue_task = _make_task(goal_id, "往日这条也要去重", planned=_day(-2), is_home=1)
    plain_task = _make_task(goal_id, "从未排期该留在持续关注", is_home=1)

    groups, _ = hc.collect_task_groups()
    watch_ids = _ids(groups[hc.GROUP_WATCH])
    assert today_task not in watch_ids, "今日组那条同时也在持续关注组（去重那句没了）"
    assert overdue_task not in watch_ids, "往日组那条同时也在持续关注组"
    assert plain_task in watch_ids, "从未排期又准入的那条不该被减去（它该留在持续关注）"

    cards, errors = hc.collect_home_cards()
    assert not errors, f"三类卡片读失败：{errors}"
    card_ids = [int(c["id"]) for c in cards[hc.CARD_TYPE_TASK]]
    assert today_task not in card_ids and overdue_task not in card_ids, (
        "卡片这一头没跟着去重：那一屏的持续关注卡里还有它")
    rendered = hc.render_home_region(cards[hc.CARD_TYPE_TASK], hc.CARD_TYPE_TASK)
    assert "今天这条也要去重" not in rendered, "持续关注那片渲染里还能看见今日那条的标题"
    assert "从未排期该留在持续关注" in rendered, (
        f"该留下的那条反倒不在持续关注里：{rendered[:200]}")
    print("[OK] 判据 4：今日／往日那两条不在持续关注组、卡集与渲染里各出现一次")


# ---------- 判据 5 ----------

def test_home_start_complete_reschedule_write_only_their_column():
    """三枚写回调各只写自己那一列，其余逐列与同表其余行一字不变"""
    goal_id = _make_goal("写口只开两列")
    start_id = _make_task(goal_id, "被开始的那一条", planned=_today(), is_home=1, priority=2)
    complete_id = _make_task(goal_id, "被完成的那一条", planned=_today(), is_home=1, priority=3)
    other_id = _make_task(goal_id, "同目标没被点的那一条", planned=_today())
    resched_id = _make_task(goal_id, "被改期的那一条", planned=_day(-1), is_home=1)

    cases = (
        ("开始", start_id, hc.do_home_start_task, "status", "in_progress"),
        ("完成", complete_id, hc.do_home_complete_task, "status", "done"),
        ("安排到今天", resched_id, hc.do_home_reschedule_today, "planned_date", _today()),
    )
    for label, tid, callback, column, value in cases:
        before = _task_row(tid)
        before_map = _task_table_map()
        outs = callback(tid)
        assert len(outs) == _EXPECTED_OUTPUT_COUNT, (
            f"「{label}」返回的不是 {_EXPECTED_OUTPUT_COUNT} 格：{len(outs)}")
        changed = _changed_columns(tid, before)
        assert changed == [column], (
            f"「{label}」写的不止 {column}：{changed}（spec §1.4：每个回调只写自己那一列）")
        assert _task_row(tid)[column] == value, f"「{label}」没把 {column} 写成 {value!r}"
        for col in ("is_home", "priority", "source", "milestone_id", "created_at",
                    "goal_id", "title", "description"):
            assert _task_row(tid)[col] == before[col], f"「{label}」动了 {col} 这一列"
        after_map = _task_table_map()
        others = [k for k in set(after_map) | set(before_map)
                  if k != tid and after_map.get(k) != before_map.get(k)]
        assert not others, f"「{label}」还动了同表别的那些行：{sorted(others)}"

    assert _task_row(other_id)["status"] == "pending", "没被点的那一条也被改了"
    # 完成后离开今日区（判据 5 的那条任务此刻已是 done，三组都不该有它）
    groups, _ = hc.collect_task_groups()
    all_ids = (_ids(groups[hc.GROUP_TODAY]) + _ids(groups[hc.GROUP_OVERDUE])
               + _ids(groups[hc.GROUP_WATCH]))
    assert complete_id not in all_ids, "done 的那条还留在首页某一组里"
    assert start_id in _ids(groups[hc.GROUP_TODAY]), "开始之后不该离开今日区（刚按下的那条要看得见）"
    assert resched_id in _ids(groups[hc.GROUP_TODAY]), "改期之后该出现在今日组"
    print("[OK] 判据 5：三枚写回调各只写一列，其余列与同表其余行不变（完成后离开三组）")


def test_home_writes_go_through_the_two_existing_writers():
    """两个写口的身份：`status` 走 `update_task_status`、`planned_date` 走 `set_task_planned_date`

    为什么要单独钉一层：判据 5 的全行对拍只证明「结果上只动了一列」，证明不了首页**调的是谁**；
    而 spec §1.4 要的是「复用既有写口、首页不养第三份实现」。这一条把两件事分开钉。
    替换发生在内存里（不改文件），跑完还原。
    """
    goal_id = _make_goal("写口身份夹具")
    start_id = _make_task(goal_id, "走 update_task_status 的那条", planned=_today())
    resched_id = _make_task(goal_id, "走 set_task_planned_date 的那条", planned=_day(-1))
    gm = hc.goal_manager
    calls = []
    original_update = gm.update_task_status
    original_set_date = gm.set_task_planned_date

    def _spy_update(task_id, status):
        calls.append(("update_task_status", int(task_id), status))
        return original_update(task_id, status)

    def _spy_set_date(task_id, planned_date):
        calls.append(("set_task_planned_date", int(task_id), planned_date))
        return original_set_date(task_id, planned_date)

    gm.update_task_status = _spy_update
    gm.set_task_planned_date = _spy_set_date
    try:
        hc.do_home_start_task(start_id)
        hc.do_home_reschedule_today(resched_id)
    finally:
        gm.update_task_status = original_update
        gm.set_task_planned_date = original_set_date
    assert gm.update_task_status is original_update, "桩没还原干净"
    assert gm.set_task_planned_date is original_set_date, "桩没还原干净"
    assert calls == [("update_task_status", start_id, "in_progress"),
                     ("set_task_planned_date", resched_id, _today())], (
        f"两个写口的调用形状不符（首页自己写了第三份实现或借道了别的写口）：{calls}")
    print("[OK] 写口身份：开始走 update_task_status，改期走 set_task_planned_date，各一次")


# ---------- 判据 9 ----------

def test_home_load_shows_today_region():
    """启动回填链上的 outputs 就是那份十一格清单，且今日／往日／持续关注三片真在返回值里"""
    source = _source_text(_FACTORY_SOURCE)
    decl = _home_panel_outputs()
    assert len(decl) == _EXPECTED_OUTPUT_COUNT, (
        f"首页 outputs 清单是 {len(decl)} 格，与组件数不一致（加了一格却忘了加 outputs）")
    for name in ("home_summary", "home_today_md", "home_today_select_dd",
                 "home_overdue_md", "home_overdue_select_dd", "home_task_cards"):
        assert name in decl, f"那份清单里少了 {name}（界面上那一格接不到刷新）"
    assert decl[_TODAY_MD_INDEX] == "home_today_md", (
        f"清单第 2 格不是今日区：{decl[_TODAY_MD_INDEX]}（顺序与返回值不对齐）")
    assert decl[_OVERDUE_MD_INDEX] == "home_overdue_md", (
        f"清单第 4 格不是往日区：{decl[_OVERDUE_MD_INDEX]}")

    start = source.index("demo.load(")
    end = source.index("new_session_btn.click", start)
    chain = source[start:end]
    assert "refresh_home_panel" in chain, "首页没进启动回填链（重启后要先点一次刷新才看见）"
    assert "outputs=_home_panel_outputs" in chain, (
        "启动回填接的不是那份唯一清单——两处各写一遍迟早与返回值漂移")

    # 运行期那一头：造三条各占一组，返回值里三片都要有它的标题
    goal_id = _make_goal("启动回填夹具")
    t1 = _make_task(goal_id, "启动就能看见的今日条", planned=_today())
    t2 = _make_task(goal_id, "启动就能看见的往日条", planned=_day(-1), is_home=1)
    t3 = _make_task(goal_id, "启动就能看见的持续关注条", is_home=1)
    outs = hc.refresh_home_panel()
    assert len(outs) == _EXPECTED_OUTPUT_COUNT, (
        f"refresh_home_panel 返回 {len(outs)} 格，与清单那 {len(decl)} 格不一致（判据 9）")
    for index, name, tid in ((_TODAY_MD_INDEX, "今日", t1),
                             (_OVERDUE_MD_INDEX, "往日", t2),
                             (_WATCH_MD_INDEX, "持续关注", t3)):
        cell = outs[index]
        assert isinstance(cell, str), f"第 {index + 1} 格（{name}）不是 Markdown：{type(cell)}"
        assert _task_row(tid)["title"] in cell, (
            f"{name}那一格没带上刚造的条目（返回值里漏了这一组）：{cell[:180]}")
    summary = outs[0]
    # 概览第一格那个数**按今日组当前行数现算**，不写死「1」：写死的数会随本文件里
    # 别的夹具留下的行漂移（同 AGENTS.md 元规则「清单里的行号必须复跑」那一族）。
    groups_now, _groups_now_errors = hc.collect_task_groups()
    assert f"{len(groups_now[hc.GROUP_TODAY])} 今日" in summary, (
        f"概览第一格数的不是今日组的行数（今日区不截断，这个数必须与上面那一区一致）："
        f"{summary[:160]}")
    print("[OK] 判据 9：启动回填接那份十一格清单，今日／往日／持续关注三片都在返回值里")


TESTS = [
    test_today_shows_planned_even_if_not_home,
    test_three_groups_are_pinned_even_when_empty,
    test_overdue_group_keeps_unscheduled_tasks_out,
    test_overdue_shows_and_reschedules_without_touching_status,
    test_watch_excludes_today_and_overdue,
    test_home_start_complete_reschedule_write_only_their_column,
    test_home_writes_go_through_the_two_existing_writers,
    test_home_load_shows_today_region,
]


def main():
    stub = _NoNetworkTrigger()
    original = af.ai_filter_opportunities
    af.ai_filter_opportunities = stub
    try:
        assert af.ai_filter_opportunities is stub, "加工桩没装上到 core.ai_filter（名字写错会静默放行）"
        for fn in TESTS:
            fn()
        assert stub.calls == 0, f"本文件一次都不该发起后台加工，却发起了 {stub.calls} 次"
    finally:
        af.ai_filter_opportunities = original
        assert af.ai_filter_opportunities is original, "加工桩没还原干净"
        cleanup_test_db()


if __name__ == "__main__":
    main()
