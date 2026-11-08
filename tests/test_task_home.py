# -*- coding: utf-8 -*-
"""
🏠 待办准入与「当前重点」关联排序（P7 阶段二 组 5 子批 B，2026-10-19）

被测的是四件东西：`memory/database.py` 里那条**带回填**的 `goal_tasks.is_home` 迁移、
`core/goal_manager.py::set_task_home`、`ui/controllers/home_controller.py` 的内存筛与两处
排序键、以及目标页那两枚新按钮（`goals_controller.do_add_task_home`／`do_remove_task_home`
＋ `ui/app_factory.py` 的声明与绑定 ＋ `app.py` 的 re-export）。设计席 组 5 子批 B 的
判据与负控逐条对上用例：

- **判据「is_home=0 不上首页」** → `test_a_task_not_admitted_stays_off_the_home`
  （两条都在同一个 active 目标下、都没勾掉，**没准入的那条一条都不出现**；并且专门把
  没准入的那条造成**更新**的——筛子没生效时它会因为 `created_at` 最新而排第一，这一
  造序让「靠日期挤进来」与「根本没筛」两种走法都能被抓到）＋
  `test_new_tasks_are_not_on_home_until_someone_admits_them`（`add_task` 的新行读回 0，
  「准入制」四个字的落库形状就是那个 DEFAULT 0）＋
  `test_home_admission_survives_the_whole_screen_aggregation`（整屏聚合那一头也对一次账）。
- **负控 N1（设计席点名）「摘掉 is_home 筛选 ⇒ 所有 pending 又冒上首页」** → 工具在会话目录
  `_g5b_mutate.py`（只动生产代码，不动本文件）。把那两行 `continue` 摘掉后实测**红 4 条**：
  `test_new_tasks_are_not_on_home_until_someone_admits_them`（红在「没准入的新行出现在首页
  待办集合里」那一句）、`test_a_task_not_admitted_stays_off_the_home`（红在「没准入那条上了
  首页」那一句，报错里带着那条的 id）、`test_home_still_finds_the_row_on_the_goals_page`
  （红在「它还在首页卡片上（移出没生效）」那一句）、
  `test_home_admission_survives_the_whole_screen_aggregation`（红在整屏聚合那一头）。
  本文件另加四条自加负控：**N2** 把回填那道 `if` 摘成恒真（helper 照旧调用，但每次
  `init_db()` 都跑一遍 `SET is_home = 1`）⇒ 只红在
  `test_migration_backfills_preexisting_rows_exactly_once`（红在「第二次 init_db 把已移出的
  那条拉回来了」那一句）；**N3** 把 focus 那一层从**发现卡**排序键的最前挪到 id 之后 ⇒ 只红在
  `test_focus_matching_opportunity_beats_assets_and_freshness`（这一条就是「加分键必须放在
  主键位置」那句判读的运行期证据：现有三键以唯一的 id 收尾、已经构成全序，追加在 id
  之后的键永远取不到）；**N4** 把匹配条件从排序层挪进
  `core/ai_filter._home_candidate_where`（写成「资格排除档 ＋ 与重点对得上」也放行）⇒ 红在
  `test_the_ruler_never_becomes_a_threshold` 的**静态**那一头（先撞上「ai_filter 里出现了
  focus」那一句，运行期那半没轮到）；**N5** 与 N3 同形、动的是**待办卡**那一族 ⇒ 只红在
  `test_admitted_and_focus_matching_task_beats_a_newer_unrelated_one`。
  五条逐条改坏、逐条还原，每条还原后与动手前 **md5 对平**，全部还原后复跑＝19／19 全绿
  （逐条记录＝会话目录 `_g5b_negctrl_report.md`）。

三条「不许越界」也各有钉子，因为这一批最容易写反的就是它们：
① **移出首页 ≠ 删掉任务、≠ 勾完成** → `test_removing_from_home_changes_only_that_one_column`
（整行指纹只差那一列）＋ `test_home_still_finds_the_row_on_the_goals_page`（库里那行还在、
目标页列表照旧有它）；② **排序不是门槛** → 两类卡片的对照用例
（`test_task_card_order_is_the_old_one_when_no_focus_is_active`／
`test_opportunity_order_falls_back_to_the_b2_keys_without_focus`）都同时断言「集合一字
不变、只是换位置」，而 `test_the_ruler_never_becomes_a_threshold` 另钉住 SQL 那一头没被
加条件、三条查询**共用**的那串 `CANDIDATE_ORDER_BY` 一字未动（写进那串就等于让首页的
显示偏好去改 AI 加工队列谁先被处理）；③ **全项目只有一把「是不是同一件事」的尺** →
`test_the_ruler_has_exactly_one_implementation`（`core/current_focus.py` 里那句互为子串的
判断只出现一次、候选函数改调它、首页两处排序也调它，且首页**没有**自己再调
`normalize_focus_title` 拼第二份），反向证据是同一次 AST 扫 `current_focus` 必须命中
`normalize_focus_title`——扫不出来的扫描器等于没跑。

⚠️ 夹具用裸 SQL 清行与直接改列（生产代码从不这样写，夹具特权）：清理只按本文件
记下的那几个 id 删；`current_focus` 整表清（本文件的夹具是那张表在这一库里的唯一作者）。
每条用例开始前 `_reset()` 一次——不留「上一条留下的行」：首页那三格只有 `CARD_CAP`
个位置，前一条用例留下的已准入任务会把本条刚造的那张卡挤出前三，那是用例互相喂数据，
不是被测形状（口径同 `tests/test_current_focus.py` 文件头）。
⚠️ `test_migration_backfills_preexisting_rows_exactly_once` 会在 `data/` 下另开一个临时库
文件、把 `memory.database.DB_PATH` 指过去跑两次 `init_db()`，`finally` 里逐字还原
`DB_PATH` 并删掉那个目录——**它不碰生产库**，本文件全程只在临时库里跑迁移。
⚠️ 不调 `refresh_home_panel()`：它末尾会**发起**后台 AI 加工（独立线程、真摸咽喉点），
本文件要的是纯读聚合那一层，所以用 `collect_home_cards()`。
⚠️ 导入顺序即隔离顺序：`ui/controllers/_shared.py` 在**导入期**调 `init_db()`，
所以必须先 `setup_test_db()` 再碰任何 UI/core 模块。
⚠️ 中文串里一律用「」而不用 ASCII 双引号。
"""

import ast
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core import ai_filter as af  # noqa: E402 （门槛与那串共用 SQL 序的真源在这，读它不抄它）
from core import current_focus as cf  # noqa: E402
from core.resource_scout import ResourceScout  # noqa: E402

md.init_db()

from ui.controllers import goals_controller as gc  # noqa: E402
from ui.controllers import home_controller as hc  # noqa: E402
import app as app_module  # noqa: E402  （导入即 create_app()：新按钮建得起来就是冒烟）

_DB_SOURCE = os.path.join("memory", "database.py")
_FOCUS_SOURCE = os.path.join("core", "current_focus.py")
_HOME_SOURCE = os.path.join("ui", "controllers", "home_controller.py")
_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")
_AIFILTER_SOURCE = os.path.join("core", "ai_filter.py")

#: 设计席指派原文给的那句列定义，逐字抄（钉的是「给的是什么」，不是「实现写着什么」）
_GIVEN_COLUMN = "is_home INTEGER DEFAULT 0"

#: 升级前生产库 `goal_tasks` 的形状，逐字抄自 2026-10-19 现读的只读快照（会话目录
#: `_g5b_prod_goal_tasks_shape.json`：`PRAGMA table_info`＝10 列、库里 10 行、无 `is_home`）。
#: 回填那两条用例要靠它复现「老库升级」那一刻，所以一列都不能少写。
_OLD_SHAPE_DDL = (
    "CREATE TABLE goal_tasks (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    goal_id TEXT NOT NULL,\n"
    "    title TEXT NOT NULL,\n"
    "    description TEXT,\n"
    "    status TEXT DEFAULT 'pending',\n"
    "    priority INTEGER DEFAULT 1,\n"
    "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n"
    "    source TEXT DEFAULT 'manual',\n"
    "    milestone_id INTEGER,\n"
    "    planned_date TEXT,\n"
    "    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)\n"
    ")"
)

#: 目标页那两枚按钮的显示文本，逐字抄自 `ui/app_factory.py`（不从实现推，理由同
#: `tests/test_current_focus.py` 里那份按钮文案表）
_HOME_BUTTON_LABELS = ("🏠 加入首页", "🚪 移出首页")

#: 「开始任务」那一段绑定的原文，两枚新按钮必须与它同形（inputs／outputs 逐个对）
_BINDING_TAIL = (
    "            inputs=[complete_task_dropdown, goal_dropdown],\n"
    "            outputs=[goal_action_status, goal_detail],\n"
    "        ).then(\n"
    "            fn=_app_module.refresh_goal_detail,\n"
    "            inputs=[goal_dropdown],\n"
    "            outputs=[goal_detail, task_choices_state, complete_task_dropdown],\n"
    "        )")


# ---------- 夹具 ----------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(days_ago: int) -> str:
    """`CURRENT_TIMESTAMP` 那一轴的文本（'YYYY-MM-DD HH:MM:SS'，UTC 不带偏移）

    `goal_tasks.created_at` 与 `opportunities.discovered_at` 都在这根轴上，所以两类卡片
    的时间排序键可以直接整串字典序比（全项目四根轴并存，登记见 `PROJECT_STATUS.md`
    §五.49；本文件只碰这一根）。
    """
    return (_now_utc() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _execute(sql: str, params: Tuple = ()) -> int:
    conn = md.get_connection()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _set_column(table: str, row_id: int, column: str, value: Any) -> None:
    _execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, int(row_id)))


def _source_text(path: str) -> str:
    """按通用换行读源码（`ui/app_factory.py` 在工作区里是 CRLF：以换行敏感的方式比对
    会让命中数静默变成 0，口径同 `tests/test_home_dashboard.py` 里同一个 helper）"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _called_names(path: str) -> Dict[str, set]:
    """ast 取一个模块里被调用的名字与 import 的根名（注释与字符串不参与）"""
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
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return {"calls": calls, "roots": roots}


def _function_source(path: str, name: str) -> str:
    """取某个顶层函数的源码文本（函数体级扫描用；取不到就 raise，不静默给空串）"""
    text = _source_text(path)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(text, node)
            if segment:
                return segment
    raise AssertionError(f"{path} 里没找到顶层函数 {name}——被测形状被搬走了，本用例该跟着重看")


_created_goals: List[str] = []
_created_tasks: List[int] = []
_created_opportunities: List[int] = []


def _make_goal(title: str) -> str:
    goal_id = gc.goal_manager.create_goal(title, "组 5 子批 B 夹具目标").goal_id
    _created_goals.append(goal_id)
    return goal_id


def _make_task(goal_id: str, title: str, days_ago: int = 0,
               home: bool = False) -> int:
    """建一条任务；**默认不准入首页**（要「已在首页」就显式传 `home=True`）

    默认值刻意与生产一致：`goal_manager.add_task` 不写 `is_home`，走 DDL 的 `DEFAULT 0`
    ⇒ 新任务天然不在首页。让夹具替测试撒谎成「默认可见」，判据那条用例当场就空了。
    """
    gc.goal_manager.add_task(goal_id, title)
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
    _set_column("goal_tasks", task_id, "created_at", _stamp(days_ago))
    if home:
        assert gc.goal_manager.set_task_home(task_id, True), "夹具没能准入这条"
    return task_id


def _make_opportunity(title: str, days_ago: int = 0, assets: Any = None,
                      hint: Any = "likely_yes", score: float = 0.8) -> int:
    """建一条**默认能上首页**的机会（三条门槛读 `core.ai_filter` 的常量，不抄阈值）"""
    opportunity_id = ResourceScout.add_opportunity(
        title, source_url=f"https://home-b-fixture.invalid/{title}",
        type_key="hackathon", summary="夹具写的一句话说明",
        status="new", eligibility_hint=hint)
    assert opportunity_id, f"夹具没能建机会：{title}"
    opportunity_id = int(opportunity_id)
    _created_opportunities.append(opportunity_id)
    _set_column("opportunities", opportunity_id, "discovered_at", _stamp(days_ago))
    _set_column("opportunities", opportunity_id, "relevance_score", float(score))
    if assets is not None:
        _set_column("opportunities", opportunity_id, "usable_assets", assets)
    return opportunity_id


def _make_focus(title: str) -> int:
    """建一条 active 重点（`source='explicit'` 默认，不需要 evidence）

    `create_focus` 的回值是**新行 id 那个整数**、不是字典——本 helper 第一版按 `["id"]` 取值，
    跑到第一条用到它的用例才 `TypeError` 暴露（前面 8 条都不碰重点，所以绿着走过）。
    """
    return int(cf.create_focus(title))


def _task_row(task_id: int) -> Dict[str, Any]:
    conn = md.get_connection()
    try:
        row = conn.execute("SELECT * FROM goal_tasks WHERE id = ?",
                           (int(task_id),)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"夹具任务 #{task_id} 不在了"
    return dict(row)


def _all_task_rows() -> List[Tuple]:
    """整张 goal_tasks 的原始行（写失败那条用例用它证明「一个字都没改」）"""
    conn = md.get_connection()
    try:
        rows = conn.execute("SELECT * FROM goal_tasks ORDER BY id").fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


def _is_home_column() -> Dict[str, Any]:
    conn = md.get_connection()
    try:
        cols = conn.execute("PRAGMA table_info(goal_tasks)").fetchall()
    finally:
        conn.close()
    for col in cols:
        if col[1] == "is_home":
            return {"name": col[1], "type": str(col[2]).upper(), "notnull": int(col[3]),
                    "dflt": col[4], "pk": int(col[5])}
    raise AssertionError(f"goal_tasks 里没有 is_home 列：{[c[1] for c in cols]}")


def _goal_task_ddl() -> str:
    conn = md.get_connection()
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name='goal_tasks'").fetchone()
    finally:
        conn.close()
    return str(row[0] if row else "")


def _create_block_of(table: str) -> str:
    """从 `memory/database.py` 的**源码文本**里切出 `CREATE TABLE IF NOT EXISTS <table> (...)` 那一段

    为什么切源码而不是读 `sqlite_master`：`ALTER TABLE ... ADD COLUMN` 会**改写**库内存着的那句
    建表文本（新列被插进括号里、`FOREIGN KEY` 行仍在最后），所以迁移加过之后运行期读到的 DDL
    一定含这一列——拿它证明「这一列没写进建表 SQL」等于拿一条恒假断言验收（本文件第一版就是这么
    写的，跑出来当场红在错误的那一侧）。「没改建表 SQL」是一条**源码**约束，只能在源码上量。
    """
    src = _source_text(_DB_SOURCE)
    head = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = src.find(head)
    assert start >= 0, f"memory/database.py 里找不到 {head}"
    end = src.find(");", start)
    assert end >= 0, f"{table} 的建表 SQL 没有收尾的 );"
    return src[start:end]


def _home_card_ids() -> List[int]:
    return [int(card["id"]) for card in hc.collect_task_cards()]


def _opportunity_card_ids() -> List[int]:
    return [int(card["id"]) for card in hc.collect_opportunity_cards()]


def _task_lines(detail: str) -> Dict[str, str]:
    """从「🎯 目标」详情里取出每条任务的渲染行：{标题: 整行}"""
    lines: Dict[str, str] = {}
    for line in detail.splitlines():
        if not line.startswith("- ") or "**" not in line:
            continue
        title = line.split("**")[1]
        lines[title] = line
    return lines


def _reset() -> None:
    """每条用例从同一起点开始：只删本文件夹具记下的那几行，重点整表清"""
    _execute("DELETE FROM current_focus")
    if _created_tasks:
        _execute("DELETE FROM goal_tasks WHERE id IN ("
                 + ",".join("?" * len(_created_tasks)) + ")", tuple(_created_tasks))
        _created_tasks.clear()
    if _created_opportunities:
        _execute("DELETE FROM opportunities WHERE id IN ("
                 + ",".join("?" * len(_created_opportunities)) + ")",
                 tuple(_created_opportunities))
        _created_opportunities.clear()
    if _created_goals:
        _execute("DELETE FROM goals WHERE goal_id IN ("
                 + ",".join("?" * len(_created_goals)) + ")", tuple(_created_goals))
        _created_goals.clear()


# ---------- 那一列的形状与准入制 ----------

def test_is_home_column_shape_is_the_one_the_design_seat_gave():
    """列存在、类型与默认值逐字对指派原文那句 `is_home INTEGER DEFAULT 0`

    另一半证据在建表 SQL 上，而且是**源码**里的建表 SQL（切法与理由见 `_create_block_of`）：
    那句 `CREATE TABLE IF NOT EXISTS goal_tasks (...)` 里不许出现这一列——它是 `init_db()` 里
    那句 `_add_column_if_not_exists` 加进来的。设计席原话「走 `_add_column_if_not_exists`」与
    `core/resource_scout.py` 头部那条仓库惯例（「新列使用迁移函数，不直接改建表 SQL」）说的是
    同一件事，这条钉的就是它：哪天有人把这一列塞改建表 SQL，既有库不会跟着变
    （`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做），而这条用例先红。
    """
    col = _is_home_column()
    assert col["type"] == "INTEGER", f"类型不是 INTEGER：{col}"
    assert str(col["dflt"]).strip() == "0", (
        f"默认值不是指派原文那个 0（准入制就写在这一格上）：{col}")
    assert col["notnull"] == 0 and col["pk"] == 0, f"不该有的约束被加上了：{col}"
    create_block = _create_block_of("goal_tasks")
    assert "is_home" not in create_block, (
        f"is_home 出现在建表 SQL 里——指派要求走迁移：\n{create_block}")
    # 同一条读数的反面（本文件第一版把它写成了判据，写反了）：迁移跑完后库内存着的建表文本
    # **确实**含这一列 ⇒ 运行期 DDL 证不了这件事，只能当"ALTER 改写了 sqlite_master"的读数留着。
    assert "is_home" in _goal_task_ddl(), (
        "运行期 DDL 里没有 is_home——那说明迁移没跑，本文件其余用例的夹具形状全不可信")
    # 折行匹配：那句调用在 `memory/database.py` 里被排版折成两行，所以比之前压平空白
    flat = " ".join(_source_text(_DB_SOURCE).split())
    assert (f'_add_column_if_not_exists(conn, "goal_tasks", "is_home", '
            f'"{_GIVEN_COLUMN.split(None, 1)[1]}")') in flat, (
        "memory/database.py 里找不到逐字对上的那句迁移（这一列是哪来的？）")
    print(f"[OK] 列形状＝{col['type']} DEFAULT {col['dflt']}；源码建表段不含它、迁移语句逐字在位")


def test_new_tasks_are_not_on_home_until_someone_admits_them():
    """`add_task` 的新行读回 0，且不在 `_home_task_rows()` 与卡片里——准入制的落库形状"""
    goal_id = _make_goal("新任务默认不在首页")
    task_id = _make_task(goal_id, "刚生成的那条")
    row = _task_row(task_id)
    assert int(row["is_home"] or 0) == 0, (
        f"新行的 is_home 不是 0，准入制就不成立：{row['is_home']!r}")
    assert task_id not in [int(r["id"]) for r in hc._home_task_rows()], (
        "没准入的新行出现在首页待办集合里")
    assert task_id not in _home_card_ids(), "没准入的新行出现在首页卡片里"
    print("[OK] add_task 的新行＝0，且不在首页集合与卡片里")


def test_a_task_not_admitted_stays_off_the_home():
    """判据原文「is_home=0 不上首页」，并把没准入的那条造成**更新的**一条

    同一个 active 目标下两条都还是 pending：准入的那条建得早、没准入的那条建得晚。
    筛子没生效时未准入那条会因 `created_at` 最新而排第一（负控 N1 抓的就是这个形状）；
    只有排序出问题则是两条都在、顺序不同——两种走法这条都能抓到。
    """
    goal_id = _make_goal("准入判据目标")
    admitted = _make_task(goal_id, "已准入但建得早", days_ago=3, home=True)
    outsider = _make_task(goal_id, "没准入而且建得晚", days_ago=0, home=False)
    ids = _home_card_ids()
    assert admitted in ids, f"已准入那条反而不在首页：{ids}"
    assert outsider not in ids, (
        f"没准入那条上了首页（is_home 那道筛没生效）：{ids}，未准入的 id＝{outsider}")
    rows = [int(r["id"]) for r in hc._home_task_rows()]
    assert admitted in rows and outsider not in rows, f"原始行集合与卡片不一致：{rows}"
    gc.goal_manager.set_task_home(outsider, True)
    ids_after = _home_card_ids()
    assert outsider in ids_after, f"准入后仍不出现：{ids_after}"
    assert ids_after[0] == outsider, (
        f"两条都准入后应按 created_at 倒序（新的在前）：{ids_after}")
    print("[OK] 未准入的更新那条不在首页；准入后出现且排最前")


def test_removing_from_home_changes_only_that_one_column():
    """「移出首页」那一击只改 `is_home`：整行指纹除这一列外一字不变

    为什么对整行取指纹而不是只看 status：`do_complete_task` 在同一屏、也写同一张表，
    如果「移出首页」顺手把 status 推成 done，那才叫把「离开主视图」写成「这条做完了」。
    """
    goal_id = _make_goal("移出首页只改一列")
    task_id = _make_task(goal_id, "先在首页的那条", days_ago=1, home=True)
    before = _task_row(task_id)
    assert int(before["is_home"]) == 1, before
    text, _detail = gc.do_remove_task_home(str(task_id), goal_id)
    after = _task_row(task_id)
    assert int(after["is_home"]) == 0, f"这一列没写成 0：{after}"
    assert after["status"] == before["status"], (
        f"status 被顺手改了：{before['status']!r} → {after['status']!r}")
    changed = [key for key in after if after[key] != before[key]]
    assert changed == ["is_home"], f"不止那一列在动：{changed}"
    assert "移出首页" in text, f"回执没说要紧事：{text}"
    print("[OK] 整行指纹只差 is_home 一列，status 未动")


def test_home_still_finds_the_row_on_the_goals_page():
    """移出首页之后那条**仍找得回来**：库里没删、目标页列表里没消失（② 那一层）

    锚点 §四「强过滤、弱删除」在待办这一族的落点：本批只让它离开 ① 那一层，不归档、
    不硬删。`get_tasks` 没有 is_home 参数、`refresh_goal_detail` 的列表按 `status != "done"`
    筛，所以两边都不该看见这一列的变化。
    """
    goal_id = _make_goal("移出后仍可找回")
    task_id = _make_task(goal_id, "被移出首页的那条", days_ago=2, home=True)
    gc.do_remove_task_home(str(task_id), goal_id)
    listed = [int(t["id"]) for t in gc.goal_manager.get_tasks(goal_id)]
    assert task_id in listed, f"库里那行被删了（只该改一列）：{listed}"
    assert [t for t in gc.goal_manager.get_tasks(goal_id)
            if t.get("status") == "done"] == [], "「移出首页」被写成了「勾完成」"
    detail = gc.refresh_goal_detail(goal_id)[0]
    assert "被移出首页的那条" in detail, "目标详情的任务列表里它没了"
    assert task_id not in _home_card_ids(), "它还在首页卡片上（移出没生效）"
    print("[OK] 库里、目标详情里都还在；只有首页卡片上没有了它")


def test_goal_detail_marks_admitted_rows_with_the_home_glyph():
    """两枚按钮不是一枚切换，所以列表里得先看得见「现在是什么状态」

    判的是「🏠 只印在已准入那一行的末尾」，而且点完两枚之后标记**当场对调**——不跟着
    写的结果变的标记就是一句装饰。这一格是执行席自决补的（设计席只点名了两枚按钮），
    理由写在 `goals_controller._render_task` 的注释里；自决可驳。
    """
    goal_id = _make_goal("详情行的准入标记")
    on_home = _make_task(goal_id, "已准入这条", days_ago=1, home=True)
    off_home = _make_task(goal_id, "未准入这条", days_ago=1, home=False)
    lines = _task_lines(gc.refresh_goal_detail(goal_id)[0])
    assert "已准入这条" in lines and "未准入这条" in lines, f"任务行没渲染出来：{list(lines)}"
    assert "🏠" in lines["已准入这条"], f"已准入那行没标记：{lines['已准入这条']}"
    assert "🏠" not in lines["未准入这条"], (
        f"未准入那行也印了标记（那等于整屏都「在首页」）：{lines['未准入这条']}")
    gc.do_remove_task_home(str(on_home), goal_id)
    gc.do_add_task_home(str(off_home), goal_id)
    after = _task_lines(gc.refresh_goal_detail(goal_id)[0])
    assert "🏠" not in after["已准入这条"] and "🏠" in after["未准入这条"], (
        "两枚按钮点完标记没跟着换：" + repr((after["已准入这条"], after["未准入这条"])))
    print("[OK] 「🏠」逐行跟着 is_home 走，两枚按钮点完标记当场对调")


def test_set_task_home_writes_both_directions_and_reports_a_missing_row():
    """存储层：写 1／写 0 都能读回；不存在的 id 返回 False，且**整表一个字没改**

    为什么在意那个 False：两个按钮的回执文案就靠它决定说「已加入」还是「没写成」。
    无条件回一句「已加入首页」，在任务刚被人删掉的场景下就是一句假话。
    """
    goal_id = _make_goal("写接口两档")
    task_id = _make_task(goal_id, "两档都走一遍", days_ago=1)
    assert gc.goal_manager.set_task_home(task_id, True) is True, "首次写 1 没返回 True"
    assert int(_task_row(task_id)["is_home"]) == 1, _task_row(task_id)
    assert gc.goal_manager.set_task_home(task_id, False) is True, "写回 0 没返回 True"
    assert int(_task_row(task_id)["is_home"]) == 0, _task_row(task_id)
    fingerprint = _all_task_rows()
    missing = int(task_id) + 999999
    assert gc.goal_manager.set_task_home(missing, True) is False, (
        f"不存在的任务 #{missing} 竟然返回 True")
    assert _all_task_rows() == fingerprint, "写失败那一次动了别行（rowcount 判据失效）"
    print("[OK] 1／0 两向都落库；缺行的写返回 False 且整表指纹不变")


# ---------- 存量回填：只跑一次 ----------

def test_migration_backfills_preexisting_rows_exactly_once():
    """老库升级那一刻把存量回填成 1（设计席定案），但**只有加列那一次**才回填

    怎么复现「升级那一刻」又不碰生产库：在 `data/` 下另开一个临时库文件，按现读的
    老形状（10 列、无 is_home）建表塞三行，再把 `memory.database.DB_PATH` 指过去跑
    `init_db()`；跑完 `finally` 里逐字还原 `DB_PATH` 并删掉那个目录。
    两半各钉一件事：① 加列之后三行都必须是 1（否则升级那一刻首页当场空掉）；
    ② 把其中一条改成 0、再跑一次 `init_db()`，它必须**还是 0**——无条件的
    `UPDATE ... SET is_home = 1` 若每次启动应用都跑，人手动移出首页的那几条会被悄悄
    拉回来，那不是回填，是每次启动改一次账（负控 N2 摘掉那道 `if` 就红在这里）。
    """
    tmp_dir = os.path.join("data", f"_test_tmp_b_oldshape_{uuid.uuid4().hex[:6]}")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, "old.db")
    original = md.DB_PATH
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute(_OLD_SHAPE_DDL)
            for title in ("升级前就有的第一条", "升级前就有的第二条", "升级前就有的第三条"):
                conn.execute("INSERT INTO goal_tasks (goal_id, title) VALUES ('G_OLD', ?)",
                             (title,))
            conn.commit()
        finally:
            conn.close()
        md.DB_PATH = path
        md.init_db()
        conn = sqlite3.connect(path)
        try:
            cols = [c[1] for c in conn.execute("PRAGMA table_info(goal_tasks)").fetchall()]
            assert "is_home" in cols, f"加列没发生：{cols}"
            values = [int(row[0]) for row in conn.execute(
                "SELECT is_home FROM goal_tasks ORDER BY id").fetchall()]
            assert values == [1, 1, 1], (
                f"存量没被回填成 1（升级那一刻首页会空）：{values}")
            defaults = [str(row[0]) for row in conn.execute(
                "SELECT is_home FROM goal_tasks").fetchall()]
            conn.execute("UPDATE goal_tasks SET is_home = 0 WHERE id = 2")
            conn.commit()
        finally:
            conn.close()
        assert defaults == ["1", "1", "1"], defaults
        md.init_db()
        conn = sqlite3.connect(path)
        try:
            after = [int(row[0]) for row in conn.execute(
                "SELECT is_home FROM goal_tasks ORDER BY id").fetchall()]
        finally:
            conn.close()
        assert after == [1, 0, 1], (
            f"第二次 init_db 把已移出的那条拉回来了（回填没绑在加列那一刻）：{after}")
    finally:
        md.DB_PATH = original
        shutil.rmtree(tmp_dir, ignore_errors=True)
    assert md.DB_PATH == original, f"DB_PATH 没还原：{md.DB_PATH}"
    print("[OK] 老库升级三行存量都回填成 1；第二次 init_db 不再复活已移出的那条")


# ---------- 排序：那把尺是排序，不是门槛 ----------

def test_admitted_and_focus_matching_task_beats_a_newer_unrelated_one():
    """待办卡「按 Focus 粗匹配排序」：对得上重点的那条压过一条更新但无关的任务

    造序让两层互相打架：匹配那条建得早、无关那条建得晚。只看 created_at 的话顺序是
    [新无关, 旧匹配]，focus 层生效才是 [旧匹配, 新无关]。
    ⚠️ 两条都**必须出现**（在 `CARD_CAP` 之内）：这一层换的是位置，不是可见集合。
    """
    _make_focus("整理算法错题本")
    goal_id = _make_goal("待办排序目标")
    matching = _make_task(goal_id, "整理算法错题本", days_ago=5, home=True)
    unrelated = _make_task(goal_id, "报名那场黑客松", days_ago=0, home=True)
    ids = _home_card_ids()
    assert matching in ids and unrelated in ids, f"两条都该在（排序不许淘汰卡片）：{ids}"
    assert ids[0] == matching, (
        f"与当前重点对得上的那条没排到最前（focus 层没进排序键？）：{ids}")
    print(f"[OK] 旧但对得上＝第 1 位、新但无关＝第 2 位：{ids}")


def test_task_card_order_is_the_old_one_when_no_focus_is_active():
    """没有 active 重点时待办卡顺序与子批 B 之前一字不变（对照，钉「降权不是门槛」）"""
    goal_id = _make_goal("无重点时的待办对照")
    older = _make_task(goal_id, "没重点可对的那条", days_ago=4, home=True)
    newer = _make_task(goal_id, "最新建的那条", days_ago=1, home=True)
    assert cf.active_focus_titles() == [], "夹具里不该有 active 重点"
    ids = _home_card_ids()
    assert ids[:2] == [newer, older], f"无 active 重点时顺序变了（created_at 倒序呢）：{ids}"
    print(f"[OK] 无 active 重点＝纯 created_at 倒序：{ids[:2]}")


def test_focus_matching_opportunity_beats_assets_and_freshness():
    """发现卡：与重点对得上的那条，压过「有可用资源」与「更新」两层

    这一条同时钉住三件判读（都写在 `home_controller.collect_opportunity_cards` 的
    docstring 里，设计席可驳）：① focus 层排在 `usable_assets` **之前**；② 它排在
    `discovered_at`／`id` 之前而不是追加在那三键之后——那三键以唯一的 id 收尾、已经
    构成全序，追加在后的键永远取不到（负控 N3 就是把这一层挪到最后，看这条红不红）；
    ③ 两条都还在（换位置不等于淘汰）。
    """
    _make_focus("找免费的GPU额度")
    with_assets = _make_opportunity("一场线上黑客松", days_ago=0,
                                    assets="免费 API 额度、数据集")
    matching = _make_opportunity("找免费的 GPU 额度", days_ago=6, assets=None)
    ids = _opportunity_card_ids()
    assert matching in ids and with_assets in ids, f"两条都该在：{ids}"
    assert ids[0] == matching, (
        f"与重点对得上的那条没压过「有可用资源＋最新发现」两层：{ids}")
    fetched = [int(row["id"]) for row in af.fetch_home_candidates(50)]
    assert matching in fetched and with_assets in fetched, "取回集合少了某条＝排序层被写成门槛了"
    print(f"[OK] focus 命中那条排第 1（资源与日期两层都被压过），两条都仍在：{ids}")


def test_opportunity_order_falls_back_to_the_b2_keys_without_focus():
    """同一形状、没有 active 重点 ⇒ 顺序回到 B2 那三键（对照）"""
    with_assets = _make_opportunity("又一场比赛", days_ago=0, assets="免费算力券")
    plainer = _make_opportunity("又一个没资源的机会", days_ago=1, assets=None)
    assert cf.active_focus_titles() == [], "夹具里不该有 active 重点"
    ids = _opportunity_card_ids()
    assert ids[0] == with_assets, (
        f"没重点时 `usable_assets` 非空优先那层应当生效，实际 {ids}")
    assert plainer in ids, f"没资源那条被挤出了首页（排序写成门槛）：{ids}"
    print(f"[OK] 无 active 重点＝B2 原序（有资源优先），没资源那条仍在：{ids[:2]}")


def test_the_ruler_never_becomes_a_threshold():
    """「只能进 ORDER BY，不许进 WHERE」：三处静态 ＋ 一处行为

    静态：① `core/ai_filter.py` 整个文件里既没有 `is_home`、也没有 `current_focus`／
    `focus` 字样（连注释都没有——重点这件事不该进加工层）；② 首页候选的拼装函数
    `_home_candidate_where` 的函数体里没有那一列；③ 三条查询**共用**的那串
    `CANDIDATE_ORDER_BY` 仍是原文那句——把显示层的重点匹配写进那串 SQL，等于让首页
    的偏好去改 AI 加工队列谁先被处理（本批没被要求，且界面上解释不了）。
    行为：一条**被门槛挡在门外**的机会（资格＝排除档），即使与 active 重点完全对得上，
    仍然不在首页——排序层翻不过门槛，这才是「加分」两个字的边界。
    """
    ai_src = _source_text(_AIFILTER_SOURCE)
    assert "is_home" not in ai_src, "core/ai_filter.py 里出现了 is_home（首页准入混进加工层）"
    assert "current_focus" not in ai_src and "focus" not in ai_src.lower(), (
        "core/ai_filter.py 里出现了 current_focus／focus——它不该知道重点这件事")
    where_body = _function_source(_AIFILTER_SOURCE, "_home_candidate_where")
    assert "is_home" not in where_body and "focus" not in where_body.lower(), (
        f"_home_candidate_where 函数体里混进了排序层的条件：{where_body}")
    assert af.CANDIDATE_ORDER_BY == "discovered_at DESC, id DESC", (
        f"三条查询共用的那串 SQL 序被改了（加工队列跟着变）：{af.CANDIDATE_ORDER_BY!r}")
    _make_focus("被门槛挡住的那件事")
    blocked = _make_opportunity("被门槛挡住的那件事", days_ago=0,
                               hint=af.EXCLUDED_ELIGIBILITY_HINT)
    assert blocked not in _opportunity_card_ids(), (
        "排除档、却与重点对得上 ⇒ 它上了首页：排序层翻过了门槛")
    control = _make_opportunity("被门槛挡住的那件事的对照", days_ago=0)
    assert control in _opportunity_card_ids(), "对照那条也没上首页＝夹具坏了"
    print("[OK] ai_filter 零命中、共用 SQL 序未动；排除档即使匹配重点也不上首页")


def test_the_ruler_has_exactly_one_implementation():
    """「是不是同一件事」只许有一处实现：候选跳过与两类卡片排序共用同一把尺

    ① `core/current_focus.py` 里那句互为子串的判断只出现**一次**（在
    `matches_focus_title` 内），且 `suggest_focus_candidates` 的函数体里调的是那个函数、
    不再内联——抽公共函数如果只做一半（留着原来那两处内联），界面上的「与重点相关」
    和候选里的「跳过」就会悄悄变成两件事；② `home_controller` 调用了那把尺，且**没有**
    自己调 `normalize_focus_title` 去拼第二份匹配；③ 反向证据（扫不出来的扫描器等于
    没跑）：同一次 AST 扫 `core/current_focus.py` 必须命中 `normalize_focus_title`。
    """
    focus_src = _source_text(_FOCUS_SOURCE)
    assert focus_src.count("key in existing") == 1, (
        f"互为子串的判断出现了 {focus_src.count('key in existing')} 次——那把尺不止一处实现")
    candidates_body = _function_source(_FOCUS_SOURCE, "suggest_focus_candidates")
    assert "matches_focus_title(" in candidates_body, "候选函数没改调那把公共尺"
    assert "key in existing" not in candidates_body, "候选函数体里还留着内联谓词：只抽了一半"
    assert "key in existing" in _function_source(_FOCUS_SOURCE, "matches_focus_title"), (
        "唯一那处内联判断不在尺里？")
    home_calls = _called_names(_HOME_SOURCE)["calls"]
    assert "matches_focus_title" in home_calls, "首页没在用那把尺"
    assert "normalize_focus_title" not in home_calls, (
        "首页自己在归一化再比对——第二把尺就是这么长出来的")
    assert "normalize_focus_title" in _called_names(_FOCUS_SOURCE)["calls"], (
        "扫描器在 current_focus 里都扫不到归一化调用，那上面那条「零命中」是假的")
    print("[OK] 谓词唯一（内联一处）、候选与首页都调它、首页未自拼归一化，扫描器有牙齿")


def test_empty_ruler_inputs_are_not_matches():
    """尺的空值几档都返回 False——「没有 active 重点时顺序一字不变」的机制证据"""
    assert cf.matches_focus_title("某条任务", []) is False, "空重点列表该返回 False"
    assert cf.matches_focus_title("", ["某条重点"]) is False, "空说法不该匹配任何东西"
    assert cf.matches_focus_title("某条任务", [""]) is False, "空重点不该冒充匹配"
    assert cf.matches_focus_title("   ", ["  "]) is False, "全空白两边都不算匹配"
    assert cf.matches_focus_title("整理算法错题本", ["整理算法错题本"]) is True
    assert cf.matches_focus_title("整理 算法 错题本", ["整理算法错题本"]) is True, (
        "归一化没生效（空白应当被去掉）")
    assert cf.matches_focus_title("错题本", ["整理算法错题本"]) is True, (
        "互为子串那一档没生效（短说法应当算对上）")
    assert cf.matches_focus_title("报名黑客松", ["整理算法错题本"]) is False
    print("[OK] 空值四档恒 False；相等／去空白／互为子串三种命中，无关说法不命中")


# ---------- 界面接线 ----------

def test_home_buttons_are_declared_and_bound_in_goal_page():
    """两枚按钮在目标页声明，绑定形状逐字镜像「开始任务」那一段

    逐字比 inputs／outputs：`[complete_task_dropdown, goal_dropdown]` 进、
    `[goal_action_status, goal_detail]` 出、再 `.then(refresh_goal_detail)` 把详情刷回来
    （那一行末尾的「🏠」就是这次点击的结果）。各绑一次——多一处同名就说明有人在别处
    也接了一次，那才是要红的事。
    """
    src = _source_text(_FACTORY_SOURCE)
    for label in _HOME_BUTTON_LABELS:
        assert src.count(f'gr.Button("{label}", size="sm")') == 1, (
            f"按钮「{label}」的声明数不是 1（重复声明或文案被改）")
    for widget, name, label in (("add_home_btn", "do_add_task_home", _HOME_BUTTON_LABELS[0]),
                                ("remove_home_btn", "do_remove_task_home",
                                 _HOME_BUTTON_LABELS[1])):
        assert f'{widget} = gr.Button("{label}", size="sm")' in src, (
            f"{widget} 没以那个文案声明")
        block = (f"        {widget}.click(\n"
                 f"            fn=_app_module.{name},\n" + _BINDING_TAIL)
        assert src.count(block) == 1, f"{widget} 的绑定段与既有两枚不再同形"
        assert src.count(f"fn=_app_module.{name},") == 1, (
            f"{name} 被绑了 {src.count(f'fn=_app_module.{name},')} 次")
    print("[OK] 两枚按钮各声明一次、各绑定一次，inputs／outputs 与既有两枚同形")


def test_app_py_reexports_the_two_callbacks():
    """`app.py` 薄壳必须 re-export 那两个名字，且是控制器里**同一个对象**"""
    names = ("do_add_task_home", "do_remove_task_home")
    for name in names:
        assert hasattr(app_module, name), (
            f"app.py 没有 re-export {name}——app_factory 里那句 _app_module.{name} "
            "会在建界面时才炸")
        assert getattr(app_module, name) is getattr(gc, name), (
            f"app.py 导出的 {name} 不是 goals_controller 里那一个（两份实现迟早说不一样的话）")
    print(f"[OK] {len(names)} 个名字逐个 re-export 且与被测模块同一对象")


def test_the_two_callbacks_say_what_actually_happened():
    """回执不夸大：没选／无效 id／库里没有那条／写成了，四种各有不同的一句

    最后那句里必须**没有**「一定显示」这种承诺——`CARD_CAP` 只有三个位置，
    「已加入首页」说的是那一列写成了，不等于「它一定出现在那一屏」。
    """
    goal_id = _make_goal("回执文案目标")
    task_id = _make_task(goal_id, "写成了的那条", days_ago=1)
    empty_text, detail_empty = gc.do_add_task_home("", goal_id)
    assert "请先选择" in empty_text and detail_empty == "", empty_text
    bad_text, _bad_detail = gc.do_add_task_home("abc", goal_id)
    assert "无效的任务 ID" in bad_text, bad_text
    removed_empty, _ = gc.do_remove_task_home("", goal_id)
    assert "请先选择" in removed_empty, removed_empty
    missing = int(task_id) + 888888
    miss_text, _miss_detail = gc.do_add_task_home(str(missing), goal_id)
    assert "没写成" in miss_text and "一行都没改" in miss_text, miss_text
    assert int(_task_row(task_id)["is_home"]) == 0, "点了一条不存在的，把别的写了？"
    ok_text, _ok_detail = gc.do_add_task_home(str(task_id), goal_id)
    assert "已加入首页" in ok_text, ok_text
    assert "三个位置" in ok_text, f"回执把「加入」写成「一定显示」了：{ok_text}"
    assert task_id in _home_card_ids(), f"文案说加上了，卡片上却没有：{task_id}"
    print("[OK] 四种回执各不相同；成功那句仍承认那一格只有三个位置")


def test_home_admission_survives_the_whole_screen_aggregation():
    """整屏聚合走一遍：准入那条在、未准入那条不在，且聚合本身不改任何一列

    `collect_home_cards()` 是首页唯一的聚合入口（`refresh_home_panel` 就是包着它渲染，
    而那个函数末尾会**发起**后台 AI 加工，本文件不碰它）。这一格判的是「界面上看到的」
    与「库里那一列」对得上——判据在真机上成不成立就是这一层的事。
    """
    _make_focus("整屏聚合这一条支线")
    goal_id = _make_goal("整屏聚合目标")
    admitted = _make_task(goal_id, "整屏聚合这一条支线", days_ago=1, home=True)
    outsider = _make_task(goal_id, "整屏不进首页的那条", days_ago=0, home=False)
    before = {int(t["id"]): t.get("is_home") for t in gc.goal_manager.get_tasks(goal_id)}
    cards, errors = hc.collect_home_cards()
    assert not errors, f"聚合里某族读失败了：{errors}"
    task_ids = [int(card["id"]) for card in cards[hc.CARD_TYPE_TASK]]
    assert admitted in task_ids and outsider not in task_ids, task_ids
    text = hc.render_home_region(cards[hc.CARD_TYPE_TASK], hc.CARD_TYPE_TASK)
    assert "整屏聚合这一条支线" in text and "整屏不进首页的那条" not in text, text[:300]
    after = {int(t["id"]): t.get("is_home") for t in gc.goal_manager.get_tasks(goal_id)}
    assert before == after, f"聚合把准入改了：{before} → {after}"
    print("[OK] 整屏聚合与 is_home 一致（含渲染文本），聚合本身不动那一列")


TESTS = [
    test_is_home_column_shape_is_the_one_the_design_seat_gave,
    test_new_tasks_are_not_on_home_until_someone_admits_them,
    test_a_task_not_admitted_stays_off_the_home,
    test_removing_from_home_changes_only_that_one_column,
    test_home_still_finds_the_row_on_the_goals_page,
    test_goal_detail_marks_admitted_rows_with_the_home_glyph,
    test_set_task_home_writes_both_directions_and_reports_a_missing_row,
    test_migration_backfills_preexisting_rows_exactly_once,
    test_admitted_and_focus_matching_task_beats_a_newer_unrelated_one,
    test_task_card_order_is_the_old_one_when_no_focus_is_active,
    test_focus_matching_opportunity_beats_assets_and_freshness,
    test_opportunity_order_falls_back_to_the_b2_keys_without_focus,
    test_the_ruler_never_becomes_a_threshold,
    test_the_ruler_has_exactly_one_implementation,
    test_empty_ruler_inputs_are_not_matches,
    test_home_buttons_are_declared_and_bound_in_goal_page,
    test_app_py_reexports_the_two_callbacks,
    test_the_two_callbacks_say_what_actually_happened,
    test_home_admission_survives_the_whole_screen_aggregation,
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
