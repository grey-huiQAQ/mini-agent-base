# -*- coding: utf-8 -*-
"""
🎯 当前重点（P7 阶段二 组 5 子批 A，2026-10-18）

被测的是 `core/current_focus.py`（存储＋确定性解析＋变更预览＋推断候选）与
`ui/controllers/focus_controller.py`（首页那一格的读与写），加上 `ui/app_factory.py`
里那个新组件块和它的绑定。设计席 组 5 子批 A 的四条判据逐条对上用例：

- **判据①「active 超过 3 个时被拒（写路径）」** →
  `test_fourth_active_focus_is_rejected_by_the_write_path`（第 4 条 raise、库里仍 3 行）＋
  `test_pausing_frees_a_slot_and_never_deletes_the_row`（腾位的是状态、不是删除）＋
  `test_restoring_a_focus_still_pays_the_active_cap`（满档时把一条暂停过的重点推回
  active 同样被拒。⚠️ 2026-10-18 负控实测：这条与新增那条是**两处各写各的判断**
  （`create_focus` 与 `set_focus_status` 里各一句、满档消息各写各的尾巴），摘掉任一处
  另一处不受影响——所以它俩必须各摘一次才各红一次，见下方判据④ 的 N1 与 N4）＋
  `test_parse_and_preview_write_nothing_even_when_full`（满档时预览里那个 `blocked`
  只是**预告**，最终判定仍然只在写路径那一道闸上——两道都判会让「预览说行、库里说不行」
  成为一种可能）；
- **判据②「『我不做 X 了』→ 该重点暂停（预览确认之后）」** →
  `test_pause_goes_through_preview_then_writes_the_status`（先跑解析＋预览并对账本
  取指纹确认没写，再 apply，回库核对 `status` 列**与那一行还在**）＋
  `test_multi_match_or_no_match_preview_writes_nothing`（对上一条以上、或一条都对不上
  ⇒ items 是空的，账本一个字没动）；
- **判据③「候选不带 evidence／confidence 不落库；候选不自动改 source='explicit'」** →
  `test_inferred_row_without_evidence_or_confidence_is_refused`（门槛在存储层的写口上，
  缺出处的推断连门都进不来）＋
  `test_candidate_function_writes_nothing_at_all`（静态：候选那一格的函数体内既没有写
  调用、也没有一句写 SQL；反向证据是同模块里真写库的 `create_focus` 过不了同一张表）＋
  `test_adopted_candidate_stays_inferred_with_its_evidence`（人点采纳之后落的那一行
  来历仍是 inferred、证据与置信照候选原样带上）；
- **判据④ 负控四项的可红点**（2026-10-18 实测；逐项改坏生产代码 → 看本文件哪几条红。
  设计席点名 N1／N2，N3／N4 是本席自加。基线先跑一次＝全绿，四条各自跑完再还原、
  还原后重跑一次＝重新全绿；逐条红点见执行席回报，工具在会话目录
  `_g5a_mutate.py`／`_g5a_negctrl.py`）：
  **N1** 摘掉 `create_focus` 里那道满档闸（把上限多让一格）⇒ 红在判据① 新增那一条，
  连带红在满档预览那条（它断言「写不进去、回执说『没写』」）；**恢复**那一条**不红**，
  因为它走的是 `set_focus_status` 里的另一句判断——这条不红正是 N4 存在的理由。
  **N2** 把 `do_focus_adopt` 落库时的 source 写成 `explicit` ⇒ 只红在「采纳仍 inferred」
  那一条，红点在 `row["source"] != "explicit"` 那句，报错里同时带着 `confidence`／
  `evidence` 被存成 NULL。
  **N3** 让 `suggest_focus_candidates` 顺手写一行 ⇒ 静态那一条（函数体内出现写调用）
  与两条指纹用例（`refresh_focus_panel` 读入口、各条拒路径的「不动账本」）一起红，
  连带红在采纳与忽略那两条——前者因候选已被提前写掉而「不在当前候选里」、后者因
  忽略前的那次现算已经落了一行。**N4** 摘掉 `set_focus_status` 里那道恢复侧的闸
  ⇒ 只红在「恢复也要过闸」那一条，红点在 `raised is not None` 那句。

其余钉「边界」的用例：`test_current_focus_table_shape_matches_the_given_ddl`（列名列序
默认值逐字对设计席给的 DDL，且 `init_db()` 再跑一次不清空既有行）／
`test_storage_module_never_deletes_or_drops_rows`（「不删历史」是**形状**上的事实：扫的是
SQL 字符串而不是中文注释，反向证据扫 `core/goal_manager.py`）／
`test_active_rows_list_before_a_fresher_paused_one`（排序两键）／
`test_candidates_come_from_recent_activity_not_from_old_ones`（窗口内外、状态两档）／
`test_candidates_skip_titles_already_active`（系统不拿推断去劝人改他自己说过的话）／
`test_saying_an_existing_focus_does_not_duplicate_it`（含糊与重复都不覆盖已有重点）／
`test_focus_read_entries_write_nothing` ＋ `test_guard_paths_refuse_without_writing`／
`test_ignore_is_this_run_only_and_writes_nothing`（「忽略」不写库的**后果**也照实测：
还原那次会话级内存过滤后同一条候选还会回来）／
`test_ui_says_the_confidence_is_not_a_probability`（那把粗尺必须把话说完）／
`test_focus_widgets_are_declared_and_bound` ＋
`test_focus_widgets_are_in_the_component_tree` ＋
`test_app_py_reexports_focus_callbacks`。

⚠️ 夹具用裸 SQL 清行——**产品代码从不 DELETE**（上面那条静态用例钉的正是这件事），
清的是夹具自己造的数据、不是被测形状：`current_focus` 整表清（顺带复位本表的
`sqlite_sequence`），`goals`／`goal_tasks`／`decisions` **只按本文件记下的那几个键删**。
后者必须清：候选函数默认按 3 条**先排序再截断**，前面用例留下的来路会把本用例刚造的
那条挤出前三——那是「用例之间互相留数据」，不是被测形状（口径同
`tests/test_home_dashboard.py` 那句「刻意不写死条数」）。
⚠️ 上限那个数在本文件里写死成 3（抄设计席判据原文），**不读 `cf.MAX_ACTIVE_FOCUS`**：
读常量的话，谁把常量改成 99 这条用例就跟着变绿，而那正是判据① 要拦的形状。
⚠️ 导入顺序即隔离顺序：`ui/controllers/_shared.py` 在**导入期**调 `init_db()`，
所以必须先 `setup_test_db()` 再碰任何 UI/core 模块，否则本文件一 import 就往生产库建表。
⚠️ 中文串里一律用「」而不用 ASCII 双引号。
"""

import ast
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from core import current_focus as cf  # noqa: E402
from core import decision_store as ds  # noqa: E402
from ui.controllers import focus_controller as fc  # noqa: E402
from ui.controllers import goals_controller as gc  # noqa: E402
import app as app_module  # noqa: E402  （导入即 create_app()，新组件块能建起来就是冒烟）

md.init_db()

_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")
_STORAGE_SOURCE = os.path.join("core", "current_focus.py")
# 反向证据用的对照文件：这个模块里**真的有** DELETE 语句，扫不出它来的扫描器等于没跑。
_DELETE_BEARING_SOURCE = os.path.join("core", "goal_manager.py")

#: active 上限，**逐字抄设计席判据原文「active 最多 3 个」**，不读实现常量（理由见文件头）。
_CAP_AS_GIVEN = 3

#: 按钮的显示文本与两个下拉的标签，逐字抄自 `ui/app_factory.py`。
#: 刻意不从实现推：这一条要防的正是「界面上的文案和用例里的那份各长各的」。
_FOCUS_BUTTON_LABELS = (
    "➕ 新增重点",
    "✏️ 保存所选的修改",
    "⏸️ 暂停所选",
    "✅ 完成所选",
    "🔍 生成变更预览",
    "✅ 按预览写入",
    "🧹 清掉预览",
    "📌 采纳这条建议",
    "🚫 先不看这条",
)
_FOCUS_DROPDOWN_LABELS = (
    "选一条当前重点（改名／暂停／完成用）",
    "建议设为当前重点（选一条）",
)

#: 置信度那把粗尺的三个刻度，**逐字抄**自 `core/current_focus.py` 的
#: `CONFIDENCE_BASE`／`STEP`／`CAP`。抄而不是读常量的理由与 `_CAP_AS_GIVEN` 同：
#: 这一格要钉的是「界面上印出来的那个数是怎么来的」，不是「实现里现在写着几」。
_CONF_ZERO_ACTIVITY = 0.4
_CONF_ONE_ACTIVITY = 0.5
_CONF_CAPPED = 0.8

_WRITE_SQL_RX = re.compile(r"\b(DELETE|DROP|TRUNCATE|REPLACE)\b", re.I)
#: 一句 SQL 的开头（关键词后必须断词，理由见 `_sql_strings`）
_SQL_HEAD_RX = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|PRAGMA|CREATE)\b", re.I)


# ---------- 夹具 ----------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(days_ago: int) -> str:
    """`CURRENT_TIMESTAMP` 那轴的文本（'YYYY-MM-DD HH:MM:SS'，UTC 不带偏移）"""
    return (_now_utc() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _iso(days_ago: int) -> str:
    """`decisions` 那一族的 ISO+00:00 文本（本文件两根轴各写各的；全项目此刻四根轴
    并存，登记见 `PROJECT_STATUS.md` §五.49 与 `core/current_focus.py` 头部）"""
    return (_now_utc() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _execute(sql: str, params: Tuple = ()) -> int:
    """夹具直接写库（生产代码从不这样写；这是夹具特权，见文件头）"""
    conn = md.get_connection()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _reset_focus_rows() -> None:
    """把 current_focus 清干净，并把自增序列复位（每条用例从同一起点开始）"""
    _execute("DELETE FROM current_focus")
    conn = md.get_connection()
    try:
        has_seq = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
    finally:
        conn.close()
    if has_seq:
        _execute("DELETE FROM sqlite_sequence WHERE name = 'current_focus'")


#: 本文件夹具**自己建过**的行（表名, 该表的键列, 值）。清理只按这份清单删行、
#: **不整表 DELETE**：临时库里本来只有夹具数据，但「只删自己建过的那几行」让这条
#: 纪律在代码里看得见，也不会顺手把别人（或以后别的用例）的数据带走。
_fixture_deletes: List[Tuple[str, str, Any]] = []


def _remember_deletion(table: str, column: str, value: Any) -> None:
    _fixture_deletes.append((table, column, value))


def _reset_activity_rows() -> None:
    """删掉本文件建过的目标／任务／决策行（候选的来路表）

    为什么必须清：候选函数按 `CANDIDATE_CAP`（默认 3）**先排序再截断**，前面用例留下的
    来路会把本用例刚造的那条挤出前三——那是「数据互相留痕」，不是被测形状。
    """
    for table, column, value in list(_fixture_deletes):
        _execute(f"DELETE FROM {table} WHERE {column} = ?", (value,))
    _fixture_deletes.clear()


def _reset_everything() -> None:
    _reset_activity_rows()
    _reset_focus_rows()


def _set_column(table: str, row_id: int, column: str, value: Any) -> None:
    """改一列的夹具（排序键与时间窗要靠它摆到能手算的位置，不睡真时间）"""
    _execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, int(row_id)))


def _make_goal(title: str) -> str:
    goal_id = gc.goal_manager.create_goal(title, "组 5 子批 A 夹具目标").goal_id
    _remember_deletion("goals", "goal_id", goal_id)
    return goal_id


def _make_task(goal_id: str, title: str, days_ago: int = 0) -> int:
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
    _remember_deletion("goal_tasks", "id", task_id)
    _set_column("goal_tasks", task_id, "created_at", _stamp(days_ago))
    return task_id


def _make_decision(title: str, days_ago: int = 0,
                   status: str = ds.STATUS_PROPOSED) -> int:
    decision_id = ds.create_decision(title, summary="夹具写的一句判断")
    _remember_deletion("decisions", "id", decision_id)
    if status != ds.STATUS_PROPOSED:
        ds.set_status(decision_id, status)
    _set_column("decisions", decision_id, "updated_at", _iso(days_ago))
    assert ds.get_decision(decision_id)["status"] == status, (
        f"夹具没能把决策推到 {status}（状态机改了这里就得跟着改）")
    return decision_id


def _candidate_titles(candidates: List[Dict[str, Any]]) -> List[str]:
    return [str(c.get("title") or "") for c in candidates]


def _dropdown_values(update: Any) -> List[str]:
    """把 `gr.update(choices=[(label, value), …])` 里的 value 抽出来（兼容 list／tuple）"""
    out: List[str] = []
    for pair in (update.get("choices") or []):
        out.append(str(pair[1] if len(pair) > 1 else pair[0]))
    return out


# ---------- 指纹与静态扫描 ----------

def _db_fingerprint() -> Dict[str, Any]:
    """逐表 (行数, MAX(rowid)) ＋ `current_focus` 的**整行内容**

    只数行数抓不到原地 UPDATE，而这一批的写路径改的正是既有行的 `status`／`title`，
    所以那张表按整行取回（口径同 `tests/test_home_dashboard.py::_db_fingerprint`，
    那里逐的是三类卡片的真源、这里逐的是本表）。
    """
    conn = md.get_connection()
    try:
        tables = [str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        out: Dict[str, Any] = {}
        for table in tables:
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            max_rowid = conn.execute(
                f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
            out[table] = (count, int(max_rowid))
        rows = conn.execute("SELECT * FROM current_focus ORDER BY id").fetchall()
        out["current_focus:rows"] = repr([tuple(r) for r in rows])
        return out
    finally:
        conn.close()


def _fingerprint_diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return [f"{key}: {before.get(key)!r} → {after.get(key)!r}"
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)]


def _source_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _normalized_factory_source() -> str:
    """把 `ui/app_factory.py` 的空白全部压掉，只用于比对**绑定形状**（不用于文案）

    这个文件整份是 CRLF（实测 1,997 段／1,997 行）：按 LF 写的逐字锚点会命中 0 而
    **静默跳过**，那等于没跑（本席位 2026-10-18 在别的文件上栽过一次）。
    """
    return re.sub(r"\s+", "", _source_text(_FACTORY_SOURCE))


def _function_node(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{_STORAGE_SOURCE} 里找不到函数 {name}（改了名要连着改用例）")


def _called_names(node: ast.AST) -> set:
    out: set = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _sql_strings(node: ast.AST) -> List[str]:
    """取一个节点里**像 SQL 的**字符串常量（ast 只给节点，注释不参与判断）

    关键词后面必须正好断词：列名 `updated_at` 转大写后开头也是「UPDATE」，
    不加 `\\b` 会把一个普通列名当成一句 UPDATE 语句（本席位实测撞过一次）。
    """
    out: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if _SQL_HEAD_RX.match(sub.value.lstrip()):
                out.append(sub.value)
    return out


#: 候选那一格与只读入口不许出现的写调用名（`memory.database` 的连接与事务咽喉点）
_FORBIDDEN_WRITE_CALLS = {
    "create_focus", "update_focus", "set_focus_status",
    "commit", "executescript", "executemany",
}


# ---------- 表结构（设计席给的 DDL 原文） ----------

def test_current_focus_table_shape_matches_the_given_ddl():
    """列名／列序／默认值逐字对设计席给的 DDL，且 `init_db()` 复跑不清空既有行"""
    conn = md.get_connection()
    try:
        cols = conn.execute("PRAGMA table_info(current_focus)").fetchall()
        ddl = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='current_focus'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert [c[1] for c in cols] == [
        "id", "title", "description", "source", "status", "confidence",
        "evidence", "created_at", "updated_at", "expires_at"], (
        f"列名或列序与设计席给的 DDL 不一致：{[c[1] for c in cols]}")
    types = {c[1]: str(c[2]).upper() for c in cols}
    assert types["id"] == "INTEGER" and types["confidence"] == "REAL", types
    assert types["title"] == "TEXT" and types["expires_at"] == "TEXT", types
    notnull = {c[1]: int(c[3]) for c in cols}
    assert [notnull[k] for k in ("title", "source", "status")] == [1, 1, 1], (
        f"NOT NULL 那三格变了：{notnull}")
    assert notnull["description"] == 0 and notnull["confidence"] == 0 \
        and notnull["evidence"] == 0 and notnull["expires_at"] == 0, (
        f"可空那四格被写成了非空：{notnull}")
    defaults = {c[1]: c[4] for c in cols}
    assert defaults["status"] == "'active'", (
        f"status 的默认值不是 DDL 里那句 'active'：{defaults['status']!r}")
    assert defaults["created_at"] == "CURRENT_TIMESTAMP" \
        and defaults["updated_at"] == "CURRENT_TIMESTAMP", defaults
    assert defaults["confidence"] is None and defaults["evidence"] is None, defaults
    assert int(dict((c[1], c[5]) for c in cols)["id"]) == 1, "id 不是主键"
    assert "AUTOINCREMENT" in ddl.upper(), "DDL 里那句 AUTOINCREMENT 没照抄"
    # 幂等：再跑两次 init_db() 既不许报错，也不许把既有行清掉
    _reset_focus_rows()
    cf.create_focus("复跑建表之后这一行还要在")
    md.init_db()
    md.init_db()
    rows = cf.list_focus()
    assert len(rows) == 1 and rows[0]["title"] == "复跑建表之后这一行还要在", (
        f"CREATE TABLE IF NOT EXISTS 没做到幂等：{rows}")
    print("[OK] 十列的列名／列序／默认值／可空性都对上 DDL，建表复跑幂等且不清数据")


# ---------- 判据①：active ≤ 3 是写路径的闸 ----------

def test_fourth_active_focus_is_rejected_by_the_write_path():
    """写满 3 条之后第 4 条被拒，且**库里一行都没多**（判据①）"""
    _reset_focus_rows()
    for title in ("第一条重点", "第二条重点", "第三条重点"):
        cf.create_focus(title)
    assert cf.count_active_focus() == _CAP_AS_GIVEN
    raised: Optional[str] = None
    try:
        cf.create_focus("第四条重点")
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None, (
        "第 4 条 active 写进去了——`MAX_ACTIVE_FOCUS` 那道闸被摘了（判据① 的负控点）")
    assert f"最多 {_CAP_AS_GIVEN} 条" in raised, f"拒的理由没说清上限：{raised}"
    assert "不替你决定动哪一条" in raised, (
        f"拒的时候不许替人选该动哪一条：{raised}")
    assert cf.count_active_focus() == _CAP_AS_GIVEN, "被拒的那一条还是占了 active 名额"
    assert len(cf.list_focus()) == _CAP_AS_GIVEN, (
        f"总行数也多了——被拒的写连不 active 的行都不该留下：{cf.list_focus()}")
    print("[OK] 第 4 条 active 在写口被 raise 拒掉，库里 active 与总行数都停在 3")


def test_pausing_frees_a_slot_and_never_deletes_the_row():
    """腾位靠的是状态推进，**不是删除**：暂停一条后能写第 4 行，但总行数只增不减"""
    _reset_focus_rows()
    first = cf.create_focus("要被暂停的那条")
    cf.create_focus("陪跑的第二条")
    cf.create_focus("陪跑的第三条")
    total_before = len(cf.list_focus())
    assert cf.set_focus_status(first, "paused") is True
    assert cf.count_active_focus() == _CAP_AS_GIVEN - 1
    fourth = cf.create_focus("腾出位子之后写的第四条")
    rows = cf.list_focus()
    assert len(rows) == total_before + 1, (
        f"暂停把行删掉了：之前 {total_before} 行，现在 {len(rows)} 行")
    paused = cf.get_focus(first)
    assert paused["status"] == "paused" and paused["title"] == "要被暂停的那条", (
        f"暂停那行的状态或标题被动过：{paused}")
    assert cf.get_focus(fourth)["status"] == "active"
    print("[OK] 暂停只改状态：行还在、标题还在，腾出的名额能写下一条")


def test_restoring_a_focus_still_pays_the_active_cap():
    """恢复一条已被暂停的重点也要过同一道闸——「它以前就在」不是放行理由"""
    _reset_focus_rows()
    spare = cf.create_focus("等着被恢复的那条")
    cf.set_focus_status(spare, "paused")          # 先让位，才好把 3 个名额占满
    for title in ("占位的第一条", "占位的第二条", "占位的第三条"):
        cf.create_focus(title)
    assert cf.count_active_focus() == _CAP_AS_GIVEN, "前置没占满名额"
    raised: Optional[str] = None
    try:
        cf.set_focus_status(spare, "active")
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None, f"满档时恢复第 4 条 active 没被拒：{raised}"
    assert "要恢复" in raised and f"最多 {_CAP_AS_GIVEN} 条" in raised, raised
    assert cf.get_focus(spare)["status"] == "paused", "被拒的恢复还是改了状态"
    # 先腾一个位子（完成一条既有 active），同一条就能恢复回来——这一半证明闸不是「永远拒」
    crowd = cf.list_focus(status="active")[0]["id"]
    cf.set_focus_status(crowd, "completed")
    assert cf.set_focus_status(spare, "active") is True
    assert cf.get_focus(spare)["status"] == "active"
    assert cf.count_active_focus() == _CAP_AS_GIVEN, "恢复之后名额又满了才对"
    print("[OK] 恢复与新增同一道闸；腾到位子后同一条能恢复回来")


def test_parse_and_preview_write_nothing_even_when_full():
    """满档时预览只**预告**会被拒；最终判定只在写路径那一道闸上（不两处各判一次）"""
    _reset_focus_rows()
    for title in ("满档第一条", "满档第二条", "满档第三条"):
        cf.create_focus(title)
    before = _db_fingerprint()
    intent = cf.parse_focus_intent("我现在做满档之外的第四条")
    preview = cf.build_focus_preview(intent)
    assert _fingerprint_diff(before, _db_fingerprint()) == [], (
        "解析或预览写了库——这两步必须零写（边界：确认之前不写）")
    assert preview["items"] and preview["items"][0]["blocked"] is True, (
        f"满档时预览要预告这一条写不进去：{preview}")
    assert f"上限是 {_CAP_AS_GIVEN} 条" in " ".join(preview["notes"]), preview["notes"]
    receipts = cf.apply_focus_preview(preview)
    assert len(receipts) == 1 and "没写" in receipts[0], receipts
    assert cf.count_active_focus() == _CAP_AS_GIVEN, "预告是一回事，写进去是另一回事"
    diff = _fingerprint_diff(before, _db_fingerprint())
    assert not diff, f"被拒的写入还是落了库（应当一格都不动）：{diff}"
    print("[OK] 满档：解析＋预览零写，apply 被写口的闸拒住、回执说清为什么没写且库没动")


# ---------- 边界：不删历史 ----------

def test_storage_module_never_deletes_or_drops_rows():
    """「paused／completed 不删历史」钉在形状上：本模块一句 DELETE／DROP 都没有

    扫的是 ast 里的**字符串常量**，不是中文注释——docstring 里那句「不删行」含中文
    「删」字，按中文扫会假红、按英文关键词扫注释又会假绿。
    反向证据：同一套扫描跑 `core/goal_manager.py` 必须命中（它真的有 DELETE），
    扫不出来的扫描器等于没有（口径同 `tests/test_home_dashboard.py` 那条）。
    """
    tree = ast.parse(_source_text(_STORAGE_SOURCE))
    sql_texts = _sql_strings(tree)
    assert any("INSERT INTO current_focus" in s for s in sql_texts), (
        "扫描器一句写 SQL 都没扫到，后面的断言是空跑")
    assert any("UPDATE current_focus" in s for s in sql_texts), (
        "扫描器没扫到本表的 UPDATE，同上属空跑")
    deletes = [s for s in sql_texts if _WRITE_SQL_RX.search(s)]
    assert not deletes, f"存储层出现了删行／删表的 SQL：{deletes}（边界：不删历史）"
    conn_calls = _called_names(tree)
    assert "commit" in conn_calls and "execute" in conn_calls, "写路径的咽喉点名字变了"

    other = ast.parse(_source_text(_DELETE_BEARING_SOURCE))
    hits = [s for s in _sql_strings(other) if _WRITE_SQL_RX.search(s)]
    assert hits, f"对照文件 {os.path.basename(_DELETE_BEARING_SOURCE)} 本该有 DELETE，"  \
        "扫不到说明这套扫描本身是瞎的"
    print(f"[OK] 存储层 {len(sql_texts)} 句 SQL 里零 DELETE／DROP；"
          f"对照文件命中 {len(hits)} 句（扫描器不是空转）")


# ---------- 词表与出处 ----------

def test_blank_title_or_unknown_word_is_refused():
    """空标题／词表外的 source／词表外的 status 一律 raise，且一格都不写"""
    _reset_focus_rows()
    cases = (
        ({"title": "   "}, "标题"),
        ({"title": "正常的一条", "source": "deleted"}, "source 只能是"),
        ({"title": "正常的一条", "status": "deleted"}, "status 只能是"),
        ({"title": "正常的一条", "status": "active "}, "status 只能是"),
    )
    for kwargs, needle in cases:
        raised: Optional[str] = None
        try:
            cf.create_focus(**kwargs)
        except ValueError as exc:
            raised = str(exc)
        assert raised is not None, f"这一组本该被拒却写进去了：{kwargs}"
        assert needle in raised, f"{kwargs} 被拒了但理由不对：{raised}"
    assert cf.list_focus() == [], f"被拒的写留下了行：{cf.list_focus()}"
    # 不存在的 id：改与推进都返回 False（那是「没这一条」，不是规则违规）
    assert cf.update_focus(999999, title="没人有的一条") is False
    assert cf.set_focus_status(999999, "paused") is False
    assert cf.get_focus(999999) is None
    print("[OK] 空标题与词表外取值都在写口被拒，库里一行没留；不存在的 id 返回 False")


def test_inferred_row_without_evidence_or_confidence_is_refused():
    """判据③ 第一半：inferred 缺 confidence／evidence 就进不了账（门槛在写口，不在界面）"""
    _reset_focus_rows()
    bad = (
        {"confidence": None, "evidence": "近 7 天该目标下有 2 条新建任务"},
        {"confidence": 0.5, "evidence": ""},
        {"confidence": 0.5, "evidence": "   "},
        {"confidence": 1.5, "evidence": "证据一句"},
        {"confidence": -0.1, "evidence": "证据一句"},
        {"confidence": "0.5", "evidence": "证据一句"},
        {"confidence": True, "evidence": "证据一句"},
    )
    for extra in bad:
        raised: Optional[str] = None
        try:
            cf.create_focus("推断来的一条", source="inferred", **extra)
        except ValueError as exc:
            raised = str(exc)
        assert raised is not None, f"缺出处的推断本该被拒：{extra}"
    assert cf.list_focus() == [], f"被拒的推断留下了行：{cf.list_focus()}"
    good = cf.create_focus("带出处的推断", source="inferred",
                           confidence=0.5, evidence="Decision #1（proposed）还等你定")
    row = cf.get_focus(good)
    assert row["source"] == "inferred" and row["confidence"] == 0.5, row
    assert "Decision #" in str(row["evidence"]), row
    explicit = cf.create_focus("人自己说的一条", description="原话写的说明")
    row2 = cf.get_focus(explicit)
    assert row2["confidence"] is None and row2["evidence"] is None, (
        f"explicit 那两格该存 NULL——人自己说的事不需要系统给出处：{row2}")
    print("[OK] inferred 缺 confidence／evidence 或非 0~1 的数全被拒；explicit 两格存 NULL")


def test_update_keeps_source_and_status():
    """改标题／说明不许顺手动来历与状态：两处都能改会让「谁把它暂停的」对不上账"""
    _reset_focus_rows()
    fid = cf.create_focus("旧标题", description="旧说明", source="inferred",
                          confidence=0.6, evidence="近 7 天该目标下有 2 条新建任务")
    cf.set_focus_status(fid, "paused")
    before = _db_fingerprint()
    assert cf.update_focus(fid, title="新标题") is True
    row = cf.get_focus(fid)
    assert row["title"] == "新标题", row
    assert row["description"] == "旧说明", (
        f"没传的 description 被 COALESCE 冲掉了：{row}")
    assert row["source"] == "inferred" and row["confidence"] == 0.6, (
        f"改标题顺手动了来历：{row}")
    assert row["status"] == "paused", f"改标题顺手动了状态：{row}"
    assert _fingerprint_diff(before, _db_fingerprint()), (
        "改标题本该留下指纹差异——抓不到就说明指纹是空转")
    assert cf.update_focus(fid, description="") is True
    assert cf.get_focus(fid)["description"] == "", "空串要能清空说明（None 才是不动）"
    raised: Optional[str] = None
    try:
        cf.update_focus(fid, title="   ")
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None and "标题不能改成空" in raised, raised
    assert cf.get_focus(fid)["title"] == "新标题", "被拒的改名还是把标题动了"
    print("[OK] 改名只改标题／说明；source、status、confidence 一个字没动")


def test_active_rows_list_before_a_fresher_paused_one():
    """排序两键（active 优先，再 updated_at 倒序）：刚被暂停的那条不能排在最前"""
    _reset_focus_rows()
    alive = cf.create_focus("还活着的那条")
    parked = cf.create_focus("刚被暂停的那条")
    cf.set_focus_status(parked, "paused")
    # 刻意把 paused 那条的时间戳摆到「最新」：如果排序没有 active 那一层，它就会排第一
    _set_column("current_focus", parked, "updated_at", "2099-01-01 00:00:00")
    _set_column("current_focus", alive, "updated_at", "2000-01-01 00:00:00")
    rows = cf.list_focus()
    assert [int(r["id"]) for r in rows] == [alive, parked], (
        f"排序没把 active 单独提一层：{[(r['title'], r['status'], r['updated_at']) for r in rows]}")
    assert [int(r["id"]) for r in cf.list_focus(status="active")] == [alive]
    assert [int(r["id"]) for r in cf.list_focus(status="paused")] == [parked]
    again = [int(r["id"]) for r in cf.list_focus()]
    assert again == [alive, parked], "同一份数据两次读取换了顺序（界面上会跳）"
    print("[OK] active 永远排在更晚更新的 paused 之前；给定 status 时只取那一档；两次读一致")


# ---------- 确定性解析 ----------

def test_parse_reads_the_two_explicit_shapes_only():
    """「我现在做 X」／「我不做 Y 了」两族认，含糊句／问句／空句一律不认（判据② 的前半）"""
    add = cf.parse_focus_intent("我现在做深度搜索超时链")
    assert add == {"recognized": True, "action": "add", "object": "深度搜索超时链",
                   "reason": "按「我现在做」读到：要做的是「深度搜索超时链」"}, add
    pause = cf.parse_focus_intent("我不做英语阅读了")
    assert pause == {"recognized": True, "action": "pause", "object": "英语阅读",
                     "reason": "按「我不做」读到：要暂停「英语阅读」"}, (
        f"句末的「了」不该留在标题里：{pause}")
    for vague in ("这个先放一放吧", "重点", "你说我现在做什么来着？", "他说他现在做完了", ""):
        out = cf.parse_focus_intent(vague)
        assert out["recognized"] is False and out["action"] is None \
            and out["object"] == "", f"含糊句被当成了指令：{vague!r} → {out}"
        assert out["reason"], f"不认的时候要说清为什么不认：{vague!r}"
    no_object = cf.parse_focus_intent("我不做 了")
    assert no_object["recognized"] is False, no_object
    assert "没说出是哪件事" in no_object["reason"], no_object
    print("[OK] 两族明确说法各自读出 add／pause（剥掉句末语气字）；含糊与问句一律不当指令")


def test_pause_goes_through_preview_then_writes_the_status():
    """判据②：「我不做 X 了」→ 预览（零写）→ 确认后暂停；那一行不删、状态落库"""
    _reset_focus_rows()
    fid = cf.create_focus("深度搜索超时链")
    before = _db_fingerprint()
    intent = cf.parse_focus_intent("我不做深度搜索超时链了")
    preview = cf.build_focus_preview(intent)
    assert _fingerprint_diff(before, _db_fingerprint()) == [], (
        "解析＋预览就写了库：确认这一步没了意义")
    assert preview["items"] == [{"action": "pause", "title": "深度搜索超时链",
                                 "focus_id": fid, "blocked": False}], preview
    assert "准备暂停一条" in preview["summary"], preview
    receipts = cf.apply_focus_preview(preview)
    assert len(receipts) == 1 and "已暂停" in receipts[0], receipts
    row = cf.get_focus(fid)
    assert row["status"] == "paused", f"确认后没落库：{row}"
    assert len(cf.list_focus()) == 1, "暂停把历史行删掉了"
    assert "那一行不删" in receipts[0], f"回执要照实说这一行没删：{receipts}"
    print("[OK] 暂停这一步＝解析 → 预览（零写）→ apply 落 status；行仍在，回执说清了")


def test_multi_match_or_no_match_preview_writes_nothing():
    """对上一条以上 ⇒ 不替人选；一条都对不上 ⇒ 不新建也不改；两种都零写（判据② 的另一半）"""
    _reset_focus_rows()
    left = cf.create_focus("英语阅读精读")
    right = cf.create_focus("英语阅读笔记")
    before = _db_fingerprint()
    multi = cf.build_focus_preview(cf.parse_focus_intent("我不做英语阅读了"))
    assert multi["items"] == [], f"对上一条以上还给出了可写的 items：{multi}"
    assert "不替你选" in " ".join(multi["notes"]), multi["notes"]
    assert "不止一条" in multi["summary"], multi
    miss = cf.build_focus_preview(cf.parse_focus_intent("我不做游泳训练了"))
    assert miss["items"] == [], miss
    assert "没有对得上的当前重点" in miss["summary"], miss
    assert "不猜你要说的是哪件" in " ".join(miss["notes"]), miss["notes"]
    for preview in (multi, miss):
        receipts = cf.apply_focus_preview(preview)
        assert receipts == ["这次没有要写入的东西：账本一个字没动"], receipts
    assert _fingerprint_diff(before, _db_fingerprint()) == [], "预览与空 apply 动了账本"
    assert cf.get_focus(left)["status"] == "active" \
        and cf.get_focus(right)["status"] == "active"
    print("[OK] 多条命中不替人选、零命中不新建不改；两种 apply 都只回「一个字没动」")


def test_saying_an_existing_focus_does_not_duplicate_it():
    """「我现在做 X」而 X 已经在账上 ⇒ 不复制、也不覆盖已有那条（边界：不覆盖已有重点）"""
    _reset_focus_rows()
    fid = cf.create_focus("英语阅读精读", description="每天二十分钟")
    before = _db_fingerprint()
    preview = cf.build_focus_preview(cf.parse_focus_intent("我现在做英语阅读精读"))
    assert preview["items"] == [], f"已经在账上的事又给了一个新增动作：{preview}"
    assert "已经在当前重点里" in preview["summary"], preview
    assert "不复制一条" in " ".join(preview["notes"]), preview["notes"]
    assert cf.apply_focus_preview(preview) == ["这次没有要写入的东西：账本一个字没动"]
    assert _fingerprint_diff(before, _db_fingerprint()) == [], "重复的说法把已有那条改了"
    row = cf.get_focus(fid)
    assert row["description"] == "每天二十分钟" and row["source"] == "explicit", row
    assert cf.count_active_focus() == 1, (
        f"复制出了第二条：{cf.list_focus()}")
    print("[OK] 说一件已经在账上的事：不新增、不改已有的来历与说明")


# ---------- 推断候选（只读） ----------

def test_candidates_come_from_recent_activity_not_from_old_ones():
    """候选只从窗口内、还「等你定」的活动里来；证据是库里已有的事实，不是推测句"""
    _reset_everything()
    fresh = _make_goal("近几天还在推进的目标")
    _make_task(fresh, "窗口内的任务一")
    _make_task(fresh, "窗口内的任务二")
    stale = _make_goal("一个月没动过的目标")
    _make_task(stale, "窗口外的任务", days_ago=30)
    _make_decision("还等你定的那条决策")
    _make_decision("已经批掉的那条决策", status=ds.STATUS_APPROVED)
    _make_decision("三个月前的那条决策", days_ago=90)
    cands = cf.suggest_focus_candidates(window_days=7, limit=50)
    by_title = {c["title"]: c for c in cands}
    task_cand = by_title.get("近几天还在推进的目标")
    assert task_cand, f"窗口内的 active 目标没成候选：{by_title}"
    assert task_cand["kind"] == "recent_tasks" and task_cand["confidence"] == 0.6, task_cand
    assert "2 条新建任务" in task_cand["evidence"], task_cand["evidence"]
    assert "goal_tasks #" in task_cand["evidence"], task_cand["evidence"]
    decision_cand = by_title.get("还等你定的那条决策")
    assert decision_cand, f"窗口内还等你定的决策没成候选：{by_title}"
    assert decision_cand["kind"] == "recent_decisions" \
        and decision_cand["confidence"] == 0.5, decision_cand
    assert "Decision #" in decision_cand["evidence"], decision_cand["evidence"]
    for gone in ("一个月没动过的目标", "已经批掉的那条决策", "三个月前的那条决策"):
        assert gone not in by_title, f"这条本该在门外：{gone} → {by_title[gone]}"
    assert all(c["evidence"].strip() and isinstance(c["confidence"], float)
               for c in cands), f"候选缺出处或缺置信：{cands}"
    scores = [c["confidence"] for c in cands]
    assert scores == sorted(scores, reverse=True), f"候选没按置信倒序：{cands}"
    print(f"[OK] 窗口内两条来路各成候选（0.6／0.5），窗口外与已定档的都不出现（{len(cands)} 条）")


def test_candidates_skip_titles_already_active():
    """与任一 active 重点对得上号的候选直接跳过——系统不拿推断去劝人改他自己说过的话"""
    _reset_everything()
    title = "已被我自己立为重点的那件事"
    goal = _make_goal(title)
    _make_task(goal, "同名目标下窗口内的任务")
    assert title in _candidate_titles(cf.suggest_focus_candidates(window_days=7, limit=50)), (
        "前置不成立：这条本该先是个候选")
    before = _db_fingerprint()
    cf.create_focus(title, source=cf.SOURCE_EXPLICIT)
    cands = cf.suggest_focus_candidates(window_days=7, limit=50)
    assert title not in _candidate_titles(cands), (
        f"人自己已经说了这件事，系统还在劝他设成重点：{cands}")
    assert _fingerprint_diff(before, _db_fingerprint()), (
        "写下一条重点之后指纹居然没变——前置的账本对拍是空转")
    print("[OK] 与已有 active 重点对得上的候选被跳过；候选这一路仍然一行都没写")


def test_candidate_function_writes_nothing_at_all():
    """判据③ 的静态那一半：`suggest_focus_candidates` 函数体内没有写调用、没有写 SQL

    反向证据在同一个文件里现取：同一套扫描跑真写库的 `create_focus` 必须命中那张禁用表。
    """
    tree = ast.parse(_source_text(_STORAGE_SOURCE))
    node = _function_node(tree, "suggest_focus_candidates")
    calls = _called_names(node)
    hit = sorted(calls & _FORBIDDEN_WRITE_CALLS)
    assert not hit, f"候选那一格里出现了写调用：{hit}（判据：候选零写库，除非人点采纳）"
    sql_texts = _sql_strings(node)
    assert sql_texts and all("SELECT" in s.upper() for s in sql_texts), (
        f"候选那一格本该只有读 SQL：{sql_texts}")
    write_like = [s for s in sql_texts if _WRITE_SQL_RX.search(s)]
    assert not write_like, f"候选那一格里冒出了写 SQL：{write_like}"
    assert "execute" in calls, "这一格连 execute 都没有——扫描器扫了个空"
    control_calls = _called_names(_function_node(tree, "create_focus"))
    assert control_calls & _FORBIDDEN_WRITE_CALLS, (
        "对照函数 create_focus 也没命中禁用表 ⇒ 这张表是空的，前面那条断言恒真")
    print("[OK] 候选函数体内零写调用、只有 SELECT；同一张表在 create_focus 上确实能命中")


# ---------- 界面回调：读写边界 ----------

def test_focus_read_entries_write_nothing():
    """把「不可能写」的那批入口全跑一遍：逐调用取指纹（买到归因）＋首尾整库对拍"""
    _reset_everything()
    fid = cf.create_focus("只读入口的那条重点")
    goal = _make_goal("只读入口的候选目标")
    _make_task(goal, "只读入口窗口内的任务")
    _make_decision("只读入口还等你定的决策")
    entries = (
        ("refresh_focus_panel", lambda: fc.refresh_focus_panel()),
        ("do_focus_select", lambda: fc.do_focus_select(fid)),
        ("do_focus_select(None)", lambda: fc.do_focus_select(None)),
        ("do_focus_preview(add)", lambda: fc.do_focus_preview("我现在做一句不该写的话")),
        ("do_focus_preview(pause)", lambda: fc.do_focus_preview("我不做只读入口的那条重点了")),
        ("do_focus_preview(含糊)", lambda: fc.do_focus_preview("先放一放")),
        ("do_focus_confirm(None)", lambda: fc.do_focus_confirm(None)),
        ("do_focus_clear", lambda: fc.do_focus_clear()),
        ("do_focus_adopt(None)", lambda: fc.do_focus_adopt(None)),
        ("do_focus_ignore(None)", lambda: fc.do_focus_ignore(None)),
        ("cf.suggest_focus_candidates", lambda: cf.suggest_focus_candidates()),
        ("cf.list_focus", lambda: cf.list_focus()),
        ("cf.count_active_focus", lambda: cf.count_active_focus()),
    )
    traces: List[str] = []
    for label, call in entries:
        before = _db_fingerprint()
        result = call()
        assert result is not None, f"{label} 返回了 None，界面那一格会空掉"
        diff = _fingerprint_diff(before, _db_fingerprint())
        if diff:
            traces.append(f"{label}: {diff}")
    assert not traces, "首页当前重点的读入口动了库：" + " | ".join(traces)
    whole = _db_fingerprint()
    _reset_focus_rows()
    cf.create_focus("整库对拍用的那条重点")
    assert _fingerprint_diff(whole, _db_fingerprint()), (
        "整库对拍抓不到「少了一行」——那说明指纹是空转")
    _reset_focus_rows()
    cf.create_focus("只读入口的那条重点")
    print(f"[OK] {len(entries)} 个只读入口逐个取指纹零命中；本文件全部写口用例都在做真写")


def test_guard_paths_refuse_without_writing():
    """空选／无预览／不在候选里：照实拒并说清缺哪一步，一格都不写"""
    _reset_focus_rows()
    before = _db_fingerprint()
    add_status = fc.do_focus_add("")[0]
    assert add_status.startswith("⚠️ 没写进去"), add_status
    assert "标题不能是空的" in add_status, add_status
    for status in (fc.do_focus_update(None, "没有选中的一条")[0],
                   fc.do_focus_pause(None)[0],
                   fc.do_focus_complete(None)[0]):
        assert "先在上方选一条当前重点" in status, status
    assert "先生成一份" in fc.do_focus_confirm(None)[0]
    assert fc.do_focus_confirm({"items": []})[0] == \
        "这次没有要写入的东西：账本一个字没动"
    assert "先在「建议设为当前重点」里选一条" in fc.do_focus_ignore("")[0]
    assert "先在「建议设为当前重点」里选一条" in fc.do_focus_adopt("")[0]
    assert "不在当前候选里" in fc.do_focus_adopt("这条根本不在候选里")[0]
    assert _fingerprint_diff(before, _db_fingerprint()) == [], (
        f"这些拒的路径写了库：{_fingerprint_diff(before, _db_fingerprint())}")
    assert cf.list_focus() == [], f"空标题的新增留下了行：{cf.list_focus()}"
    print("[OK] 五类前置不满足的点击都被拒并说清缺哪一步，current_focus 仍然 0 行")


def test_adopted_candidate_stays_inferred_with_its_evidence():
    """判据③ 第二半：人点采纳之后落库的那一行来历**仍是 inferred**，证据与置信照原样带上"""
    _reset_everything()
    title = "要被采纳的候选目标"
    goal = _make_goal(title)
    _make_task(goal, "候选目标窗口内的任务")
    _make_task(goal, "候选目标窗口内的第二条任务")
    cands = fc._live_candidates()
    match = next((c for c in cands if c["title"] == title), None)
    assert match, f"前置不成立，这条没进候选：{_candidate_titles(cands)}"
    before = _db_fingerprint()
    status_md, _region, manage_dd, cand_dd, preview_md, state = fc.do_focus_adopt(title)
    assert "✅" in status_md and "inferred" in status_md, status_md
    rows = cf.list_focus(status="active")
    assert len(rows) == 1, f"采纳写出了不止一行：{rows}"
    row = rows[0]
    assert row["source"] != "explicit", (
        f"候选被自动转成了人自己说的——判据③ 的负控点：{row}")
    assert row["source"] == "inferred" \
        and row["confidence"] == match["confidence"] \
        and row["evidence"] == match["evidence"], (f"落库的出处与候选不一致：{row} vs {match}")
    assert title not in _dropdown_values(cand_dd), (
        f"刚被设成重点的那条还在候选下拉里：{_dropdown_values(cand_dd)}")
    assert len(_dropdown_values(manage_dd)) == 1, manage_dd
    assert preview_md == "" and state is None, "写过账本之后旧预览还留着"
    assert _fingerprint_diff(before, _db_fingerprint()), "采纳没写库——那这条用例是空跑"
    # 同一条再点一次：候选已经不在了，拒写而不是补一行
    again = fc.do_focus_adopt(title)[0]
    assert "不在当前候选里" in again and cf.count_active_focus() == 1, again
    print("[OK] 采纳落的那行 source 仍 inferred、带原证据与置信；再点一次被拒且不多写")


def test_ignore_is_this_run_only_and_writes_nothing():
    """「🚫 先不看这条」只活本次运行：不写库，还原那次内存过滤后同一条候选还会回来"""
    _reset_everything()
    title = "要被先不看的那件事"
    goal = _make_goal(title)
    _make_task(goal, "先不看那条窗口内的任务")
    before = _db_fingerprint()
    try:
        status_md, _region, _manage, cand_dd, _preview, _state = fc.do_focus_ignore(title)
        assert "本次运行里不再提" in status_md, status_md
        assert "重启后它还会再出现一次" in status_md, (
            f"这句限度要照实在界面上说：{status_md}")
        assert title not in _dropdown_values(cand_dd), _dropdown_values(cand_dd)
        diff = _fingerprint_diff(before, _db_fingerprint())
        assert not diff, f"「忽略」写了库：{diff}"
        assert cf.list_focus() == [], f"忽略顺带建了一条重点：{cf.list_focus()}"
    finally:
        fc._dismissed_candidates.discard(cf.normalize_focus_title(title))
    region_md, _manage2, cand_dd2 = fc.refresh_focus_panel()
    assert title in _dropdown_values(cand_dd2), (
        f"还原内存过滤后候选没回来，说明它被写进库了：{_dropdown_values(cand_dd2)}")
    assert "先不看这条" in region_md, region_md[:400]
    print("[OK] 忽略只是本次运行里的一格内存：零写库，滤掉那格之后候选照旧出现")


def test_ui_says_the_confidence_is_not_a_probability():
    """那把粗尺要在界面上把话说完：只印一个 0.5 会让人以为有谁算过它"""
    inferred_row = {"id": 1, "title": "推断来的那条", "description": None,
                    "source": "inferred", "status": "active", "confidence": 0.5,
                    "evidence": "近 7 天该目标下有 1 条新建任务",
                    "expires_at": None, "updated_at": "2026-10-18 00:00:00"}
    text = fc.render_focus_region([inferred_row], 0, [])
    assert "按近期活动条数给的粗量，不是概率" in text, text[:400]
    assert "凭什么在这儿" in text and "推断来的（已采纳）" in text, text[:400]
    cand_text = fc.render_focus_region([], 0, [{"title": "候选一条", "kind": "recent_tasks",
                                               "confidence": 0.6,
                                               "evidence": "近 7 天该目标下有 2 条新建任务"}])
    assert "你不点就什么都不写" in cand_text, cand_text[:400]
    assert "不是概率" in cand_text, cand_text[:400]
    assert "📌 采纳这条建议" in cand_text and "🚫 先不看这条" in cand_text, cand_text[-600:]
    empty = fc.render_focus_region([], 0, [])
    assert "还没有 active 的当前重点" in empty, empty
    assert "建议设为当前重点" not in empty, empty
    history = fc.render_focus_region([], 2, [])
    assert "另有 2 条处于暂停／完成状态" in history, history
    assert "这一格从不删行" in history, history
    # 尺子的三档刻度（抄常量而不是读常量，理由见文件头）
    assert cf._coarse_confidence(0) == _CONF_ZERO_ACTIVITY
    assert cf._coarse_confidence(1) == _CONF_ONE_ACTIVITY
    assert cf._coarse_confidence(100) == _CONF_CAPPED, "封顶那一档没生效"
    assert cf._coarse_confidence(-5) == _CONF_ZERO_ACTIVITY, "负数活动条数不该把尺子压到起点以下"
    print("[OK] 空态／历史条数／候选三档文案都在，置信那句「不是概率」印得出来")


def test_focus_write_button_texts_and_the_state_box():
    """预览那一格的 State 必须真存在且只由「生成预览」写入——写入按钮读的就是它"""
    src = _source_text(_FACTORY_SOURCE)
    assert re.search(r"focus_preview_state\s*=\s*gr\.State\(", src), (
        "预览没存进 gr.State：那「✅ 按预览写入」就没有可读的那一份预览")
    assert re.search(r"focus_preview_state\s*=\s*gr\.State\(\s*None\s*\)", src), (
        "State 的初值不是 None：一上来就有一个可点的写入按钮")
    norm = _normalized_factory_source()
    assert "focus_confirm_btn.click(fn=_app_module.do_focus_confirm," \
        "inputs=[focus_preview_state]" in norm, (
        "「按预览写入」读的必须是那份 State，而不是那句原话输入框")
    assert norm.count("outputs=[focus_preview_md,focus_preview_state]") == 1, (
        "能写 State 的绑定应当只有一处（生成预览那一步）")
    assert "_focus_write_outputs=[focus_status,focus_region,focus_manage_dd," \
        "focus_candidate_dd,focus_preview_md,focus_preview_state]" in norm, (
        "写回调的 outputs 少了 State 那一格：写完预览不清，旧的「按预览写入」会再写一次")
    print("[OK] State 初值 None、只由「生成预览」写、六个 outputs 含预览那一格与 State")


# ---------- 界面接线（静态） ----------

def test_focus_widgets_are_declared_and_bound():
    """组件声明与绑定形状静态钉住：按钮文案漂移、或写回调没接上新控制器，这里红

    口径照 `tests/test_task_lifecycle.py::test_start_button_is_declared_and_bound`：
    文案用正则从源码抽（排版会被格式化动），绑定用压掉空白的源码比（CRLF 见上）。
    """
    src = _source_text(_FACTORY_SOURCE)
    for var, label in zip(
        ("focus_add_btn", "focus_save_btn", "focus_pause_btn", "focus_complete_btn",
         "focus_preview_btn", "focus_confirm_btn", "focus_clear_btn",
         "focus_adopt_btn", "focus_ignore_btn"),
        _FOCUS_BUTTON_LABELS,
    ):
        match = re.search(re.escape(var) + r"\s*=\s*gr\.Button\(\s*\"([^\"]*)\"", src)
        assert match, f"{var} 没被声明成按钮（整枚按钮没了也算这里红）"
        assert match.group(1) == label, f"{var} 的文案不是用例抄的这一份：{match.group(1)!r}"
    for var, label in (("focus_manage_dd", _FOCUS_DROPDOWN_LABELS[0]),
                       ("focus_candidate_dd", _FOCUS_DROPDOWN_LABELS[1])):
        match = re.search(re.escape(var) + r"\s*=\s*gr\.Dropdown\(\s*label=\"([^\"]*)\"", src)
        assert match and match.group(1) == label, (
            f"{var} 的 label 不在或改了字：{(match.group(1) if match else None)!r}")
    for var in ("focus_region", "focus_status"):
        assert re.search(re.escape(var) + r"\s*=\s*gr\.Markdown\(", src), f"{var} 不见了"
    for var in ("focus_title_box", "focus_desc_box", "focus_say_box"):
        assert re.search(re.escape(var) + r"\s*=\s*gr\.Textbox\(", src), f"{var} 不见了"

    norm = _normalized_factory_source()
    bindings = (
        "focus_manage_dd.change(fn=_app_module.do_focus_select,inputs=[focus_manage_dd],"
        "outputs=[focus_title_box,focus_desc_box],)",
        "focus_add_btn.click(fn=_app_module.do_focus_add,"
        "inputs=[focus_title_box,focus_desc_box],outputs=_focus_write_outputs,)",
        "focus_save_btn.click(fn=_app_module.do_focus_update,"
        "inputs=[focus_manage_dd,focus_title_box,focus_desc_box],"
        "outputs=_focus_write_outputs,)",
        "focus_pause_btn.click(fn=_app_module.do_focus_pause,inputs=[focus_manage_dd],"
        "outputs=_focus_write_outputs,)",
        "focus_complete_btn.click(fn=_app_module.do_focus_complete,inputs=[focus_manage_dd],"
        "outputs=_focus_write_outputs,)",
        "focus_preview_btn.click(fn=_app_module.do_focus_preview,inputs=[focus_say_box],"
        "outputs=[focus_preview_md,focus_preview_state],)",
        "focus_confirm_btn.click(fn=_app_module.do_focus_confirm,"
        "inputs=[focus_preview_state],outputs=_focus_write_outputs,)",
        "focus_adopt_btn.click(fn=_app_module.do_focus_adopt,inputs=[focus_candidate_dd],"
        "outputs=_focus_write_outputs,)",
        "focus_ignore_btn.click(fn=_app_module.do_focus_ignore,inputs=[focus_candidate_dd],"
        "outputs=_focus_write_outputs,)",
    )
    missing = [b for b in bindings if b not in norm]
    assert not missing, f"这 {len(missing)} 处绑定形状不对（红的那段即缺的绑定）：{missing}"
    # 启动回填与刷新按钮都要带上这一格（判据：重启后不点任何东西也得看见现在的重点）
    assert norm.count("fn=_app_module.refresh_focus_panel,outputs=[focus_region,"
                      "focus_manage_dd,focus_candidate_dd],") == 2, (
        "启动回填链与「🔄 刷新首页」那一下必须各接一次只读刷新（共 2 处）")
    chain_start = src.index("demo.load(")
    chain = src[chain_start:src.index("new_session_btn.click", chain_start)]
    assert "refresh_focus_panel" in chain, "启动回填链里没有当前重点那一格"
    print(f"[OK] 九枚按钮文案、两个下拉标签、{len(bindings)} 处绑定与两处只读刷新都在")


def test_focus_widgets_are_in_the_component_tree():
    """组件树里真能找到那些按钮与下拉（取人看到的文本，不是变量名）"""
    import gradio as gr
    assert getattr(app_module, "demo", None) is not None, "import app 没建出 demo"
    buttons = [str(getattr(b, "value", "") or "")
               for b in app_module.demo.blocks.values() if isinstance(b, gr.Button)]
    missing = [label for label in _FOCUS_BUTTON_LABELS if label not in buttons]
    assert not missing, f"这些按钮不在构建好的界面里：{missing}"
    dropdowns = [str(getattr(b, "label", "") or "")
                 for b in app_module.demo.blocks.values() if isinstance(b, gr.Dropdown)]
    missing_dd = [label for label in _FOCUS_DROPDOWN_LABELS if label not in dropdowns]
    assert not missing_dd, f"这两个下拉不在组件树里：{missing_dd}"
    textboxes = [str(getattr(b, "label", "") or "")
                 for b in app_module.demo.blocks.values() if isinstance(b, gr.Textbox)]
    for label in ("当前重点标题", "说一句你的重点变化（只生成预览，不写库）"):
        assert label in textboxes, f"输入框「{label}」不在树里：{textboxes}"
    print(f"[OK] 九枚按钮、两个下拉、那两个输入框都在组件树里（{len(buttons)} 枚按钮中）")


def test_app_py_reexports_focus_callbacks():
    """app.py 薄壳必须 re-export 那十一个名字，且是控制器里**同一个对象**"""
    names = ("refresh_focus_panel", "do_focus_add", "do_focus_select", "do_focus_update",
             "do_focus_pause", "do_focus_complete", "do_focus_preview", "do_focus_confirm",
             "do_focus_clear", "do_focus_adopt", "do_focus_ignore")
    for name in names:
        assert hasattr(app_module, name), (
            f"app.py 没有 re-export {name}——app_factory 里那句 _app_module.{name} "
            "会在建界面时才炸")
        assert getattr(app_module, name) is getattr(fc, name), (
            f"app.py 导出的 {name} 不是 focus_controller 里那一个（两份实现迟早说不一样的话）")
    print(f"[OK] {len(names)} 个名字逐个 re-export 且与被测模块同一对象")


def test_write_callbacks_live_only_in_the_focus_block_of_home():
    """写口只准出现在首页那个 marker 子块里——这是首页绑定区那条用例切片的**正向**证据

    `tests/test_home_dashboard.py` 里「除当前重点这一格外不许出现写回调」那条用例
    按 marker 把本格圈出去；如果 marker 不见了或本格里一条写回调都没有，那个「排除」
    就成了假排除（切片变成整块，写回调就没人管了）。
    """
    src = _source_text(_FACTORY_SOURCE)
    marker = "# -------------------- 🏠 首页 · 🎯 当前重点（写口）--------------------"
    assert src.count(marker) == 1, (
        f"marker 出现 {src.count(marker)} 次——那条排除用例会按第一处切片，切片跟着错位")
    start = src.index(marker)
    block = src[start:src.index("    return demo", start)]
    assert block.count("fn=_app_module.do_focus") >= 9, (
        f"marker 之后的写回调少了（是 {block.count('fn=_app_module.do_focus')} 处）")
    home_only = src[src.index("# ==================== 🏠 首页"):start]
    assert "do_focus" not in home_only, (
        "marker 之前的那一段（三区＋三个跳转）出现了 focus 写回调：B1 判据⑤ 被这批动了")
    print("[OK] marker 唯一、写回调全在其后；三区那一段一条 focus 写回调都没接")


TESTS = [
    test_current_focus_table_shape_matches_the_given_ddl,
    test_fourth_active_focus_is_rejected_by_the_write_path,
    test_pausing_frees_a_slot_and_never_deletes_the_row,
    test_restoring_a_focus_still_pays_the_active_cap,
    test_parse_and_preview_write_nothing_even_when_full,
    test_storage_module_never_deletes_or_drops_rows,
    test_blank_title_or_unknown_word_is_refused,
    test_inferred_row_without_evidence_or_confidence_is_refused,
    test_update_keeps_source_and_status,
    test_active_rows_list_before_a_fresher_paused_one,
    test_parse_reads_the_two_explicit_shapes_only,
    test_pause_goes_through_preview_then_writes_the_status,
    test_multi_match_or_no_match_preview_writes_nothing,
    test_saying_an_existing_focus_does_not_duplicate_it,
    test_candidates_come_from_recent_activity_not_from_old_ones,
    test_candidates_skip_titles_already_active,
    test_candidate_function_writes_nothing_at_all,
    test_focus_read_entries_write_nothing,
    test_guard_paths_refuse_without_writing,
    test_adopted_candidate_stays_inferred_with_its_evidence,
    test_ignore_is_this_run_only_and_writes_nothing,
    test_ui_says_the_confidence_is_not_a_probability,
    test_focus_write_button_texts_and_the_state_box,
    test_focus_widgets_are_declared_and_bound,
    test_focus_widgets_are_in_the_component_tree,
    test_app_py_reexports_focus_callbacks,
    test_write_callbacks_live_only_in_the_focus_block_of_home,
]


def main():
    try:
        for fn in TESTS:
            fn()
    finally:
        _reset_everything()
        fc._dismissed_candidates.clear()
        cleanup_test_db()


if __name__ == "__main__":
    main()
