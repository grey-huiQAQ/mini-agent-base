# -*- coding: utf-8 -*-
"""
模板运行记录（template_runs，P7 批次 14）模块测试（离线：mock web_search / chat_sync）

覆盖：
- 独立测试数据库 + 新表/新列：template_runs 建表、16 个字段一个都不省（C8），
  pending_approvals.run_id 由迁移补上
- 存储层读写：record_template_run / latest_template_run / list_template_runs，
  due_count=None 存成 NULL（"没读到"≠"0 件"），latest_template_run 按触发源过滤
- 写行条件（批次 14 定案第 2 条）：boot / cli 每次必记一行（哪怕到期 0 件），
  timer 只在确有执行时记行——界面那个 60 秒空转不产行
- 计数器不变式：due_count == ran + paused + backlog_skip + claim_skip + failed
- 抢占失败不再静默：claim_skip 计入运行行，且 stdout 必须有一行提示
- 读失败那一行的形状：status='error'、due_count=NULL、note 写原因；
  同一条读失败在 timer 路径下**不落行**（写行条件按定案第 2 条，不是遗漏）
- C9 两根时钟轴：新表的 started_at/finished_at 是 UTC（带 +00:00），
  而 task_templates.last_run_at 保持本地写法（改成 UTC 会让模板永久到期）
- C4 归属：本次产出的 pending 行被标上 run_id，跑前已有的行不被认领
- C8 库指纹：db_path 绝对路径 + db_bytes + 该库 pending 总数
- 跑前基线函数 latest_pending_approval_id 的契约：空表返回 0，查询失败返回 None
  （**不能拿 0 兜底**：那等于把全部历史行认领成本次产出）
- V4 那一格：同一批次连跑两次，第二次 due_count=0、产出 0 行，且两行痕迹都在库里
- 接线：ui 回调 do_check_scheduled_templates 把 trigger_source 原样传给执行器
"""
import atexit
import contextlib
import io
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_connection
from core import task_template as tt
from agents import task_executor as te_module
from agents.task_executor import TaskExecutor

# 模板执行的虚拟目标目录根（约定见 core.task_template.template_goal_id）
_GOALS_ROOT = Path("data") / "goals"

# 本文件新建目录的清理依据：只删本次新建的，不动业务运行留下的真实目录
_pre_existing_template_dirs = {
    p.name for p in _GOALS_ROOT.glob("template_*") if p.is_dir()
} if _GOALS_ROOT.exists() else set()

# template_runs 的字段清单（C8"字段不许省"的机器可核形态：与建表语句逐列对齐）
_RUN_COLUMNS = [
    "id", "trigger_source", "started_at", "finished_at", "status", "db_path",
    "db_bytes", "db_pending_total", "due_count", "ran", "paused",
    "backlog_skip", "claim_skip", "failed", "produced_rows",
    "budget_exhausted", "note",
]


def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()


# ---------- 辅助 ----------

def _new_template_name(prefix="运行记录"):
    """生成唯一模板名，便于排查与清理"""
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def _template_goal_dir(template_id) -> Path:
    return _GOALS_ROOT / tt.template_goal_id(template_id)


def _reset_template_goal_dir(template_id):
    """清空指定模板的结果目录，保证执行类测试互相独立"""
    goal_dir = _template_goal_dir(template_id)
    if goal_dir.exists():
        for sub in ("notes", "research_notes"):
            sub_dir = goal_dir / sub
            if sub_dir.exists():
                shutil.rmtree(sub_dir, ignore_errors=True)


def _patch_web_search(fake):
    """临时替换 local_tools.web_search（TaskExecutor 经模块引用调用）"""
    original = te_module.local_tools.web_search
    te_module.local_tools.web_search = fake

    def restore():
        te_module.local_tools.web_search = original
    return restore


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync（笔记草稿 LLM 调用）"""
    original = te_module.model_client.chat_sync
    te_module.model_client.chat_sync = fake

    def restore():
        te_module.model_client.chat_sync = original
    return restore


def _patch_get_due(fake):
    """临时替换到期清单读取（执行器按 template_module.get_due_templates 引用）"""
    original = tt.get_due_templates
    tt.get_due_templates = fake

    def restore():
        tt.get_due_templates = original
    return restore


def _patch_update_last_run(fake):
    """临时替换防重入抢占（返回 False 即"抢占失败"分支）"""
    original = tt.update_last_run
    tt.update_last_run = fake

    def restore():
        tt.update_last_run = original
    return restore


def _rows(order="ASC"):
    """直接读全部运行行（asc 便于看顺序；读接口由其它用例负责验证）"""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM template_runs ORDER BY id {order}").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _row_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM template_runs").fetchone()["c"]
    finally:
        conn.close()


def _latest_run_row():
    rows = _rows("DESC")
    return rows[0] if rows else None


def _pending_count_by_title(needle):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM pending_approvals WHERE task_title LIKE ?",
            (f"%{needle}%",)).fetchone()["c"]
    finally:
        conn.close()


def _pending_rows_by_title(needle):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, run_id, status FROM pending_approvals "
            "WHERE task_title LIKE ? ORDER BY id", (f"%{needle}%",)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _insert_pending(goal_id, title, status="pending"):
    """直接插一条待审批行（模拟"跑前就存在的行"，不经执行器）"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pending_approvals (task_type, task_title, goal_id, "
            "content, status) VALUES ('search', ?, ?, '草稿内容', ?)",
            (title, goal_id, status),
        )
        conn.commit()
    finally:
        conn.close()


# 本文件的用例**一律不删 pending_approvals 里的行**（只删模板、只清 template_runs）。
# 理由：差值法（`latest_pending_approval_id()` 跑前取基线、跑后取新最大值）量的是
# **id 差**而不是行数，而 `pending_approvals` 用 AUTOINCREMENT——删行会把 MAX(id) 拉低，
# 下一条新行却仍拿"历史最大 id＋1"，于是之后每次运行的 `produced_rows` 都**虚高**
# （实测虚报 7、实际标上 1）。生产代码没有一处 `DELETE FROM pending_approvals`
# （审批是改 status 不是删行），所以这个失真只在测试环境出现；而执行器在这种情况下
# 会打印"产出数与归属行数不一致"，不静默取其中一个数——那正是它该有的样子。
# 唯一需要清空该表的用例（连接失败契约）排在 `__main__` 的最后一位，删完就收尾。
def _delete_all_runs():
    """清空运行行（用例之间以"本次新增几行"为准，绝对行数不可比）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM template_runs")
        conn.commit()
    finally:
        conn.close()


def _utc_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _capture_stdout(func, *args, **kwargs):
    """在捕获 stdout 的上下文里调用 func，返回 (返回值, 捕获到的输出文本)

    「安静」类缺陷只能靠 stdout 断言抓（本仓库的实测经验）：库里没写东西、
    屏幕上也没出声，就是同一件事的两种写法，断言库里的行抓不到"该出声而没出声"。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


def _cleanup():
    """清理本次新建的模板结果目录（临时库由 helper 的 atexit 兜底）"""
    if _GOALS_ROOT.exists():
        for p in _GOALS_ROOT.glob("template_*"):
            if p.is_dir() and p.name not in _pre_existing_template_dirs:
                shutil.rmtree(p, ignore_errors=True)


atexit.register(_cleanup)


# ---------- 建表与迁移 ----------

def test_independent_database_and_new_table():
    """隔离验证 ＋ 新表字段齐全：template_runs 建在临时库、列名与 C8 清单一一对应，
    pending_approvals.run_id 由迁移补上（可空列，不回填）"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    assert _TMP_DB != os.path.join("data", "memory.db"), "不得使用默认业务库"
    conn = get_connection()
    try:
        cols = [r["name"] for r in
                conn.execute("PRAGMA table_info(template_runs)").fetchall()]
        pending_cols = [r["name"] for r in
                        conn.execute("PRAGMA table_info(pending_approvals)").fetchall()]
    finally:
        conn.close()
    assert cols == _RUN_COLUMNS, f"template_runs 列名或顺序与 C8 清单不符：{cols}"
    assert "run_id" in pending_cols, "pending_approvals 应迁移出 run_id 列"
    print("[OK] independent database and new table")


# ---------- 存储层读写 ----------

def test_record_and_read_back():
    """record_template_run 返回新行 id；读接口能取回，且 due_count=None 存成 NULL
    latest_template_run 的触发源过滤要真的过滤（没有 boot 行时返回 None，不是返回别的行）"""
    _delete_all_runs()
    assert tt.latest_template_run() is None, "空表应读不到运行行"
    assert tt.list_template_runs() == [], "空表列表应为空"

    run_id = tt.record_template_run(
        trigger_source=tt.TRIGGER_SOURCE_CLI, started_at=_utc_text(),
        finished_at=_utc_text(), status="ok", due_count=None, note="读不到到期清单")
    assert isinstance(run_id, int) and run_id > 0, "写行应返回新行 id"

    row = tt.latest_template_run(tt.TRIGGER_SOURCE_CLI)
    assert row and row["id"] == run_id
    assert row["due_count"] is None, "due_count=None 必须存成 NULL（没读到≠0 件）"
    assert row["note"] == "读不到到期清单"
    assert row["trigger_source"] == tt.TRIGGER_SOURCE_CLI
    assert row["ran"] == 0 and row["produced_rows"] == 0, "未给的计数默认 0"
    assert row["budget_exhausted"] == 0, "C8 要求字段存在，取值今天只会是 0"
    assert tt.latest_template_run(tt.TRIGGER_SOURCE_BOOT) is None, \
        "只写了 cli 行，按 boot 过滤不该命中"

    # 再写一行 boot：latest 无参取最大 id 那一行，带参取该触发源最近一行
    boot_id = tt.record_template_run(trigger_source=tt.TRIGGER_SOURCE_BOOT,
                                     started_at=_utc_text(), finished_at=_utc_text())
    assert boot_id and boot_id > run_id
    assert tt.latest_template_run()["id"] == boot_id
    listed = tt.list_template_runs(limit=2)
    assert [r["id"] for r in listed] == [boot_id, run_id], "列表按 id 倒序"
    assert tt.list_template_runs(limit=0) == [], "limit=0 不应报错"
    print("[OK] record and read back")


# ---------- 写行条件 ----------

def test_boot_and_cli_always_record_one_row():
    """定案第 2 条：boot / cli 每次必记一行，**哪怕到期 0 件**——
    boot 行是 V3"距上次开机多久"的分母，cli 行是 V4 第二次跑的痕迹"""
    _delete_all_runs()
    executor = TaskExecutor()
    restore = _patch_get_due(lambda *a, **k: [])
    try:
        assert executor.check_scheduled_templates(trigger_source="boot") == []
        row = _latest_run_row()
        assert row["trigger_source"] == "boot", "到期 0 件也必须留下 boot 行"
        assert row["due_count"] == 0 and row["status"] == "ok"
        for field in ("ran", "paused", "backlog_skip", "claim_skip", "failed",
                      "produced_rows"):
            assert row[field] == 0, f"{field} 在无到期模板时应为 0"

        assert executor.check_scheduled_templates(
            trigger_source=tt.TRIGGER_SOURCE_CLI) == []
        assert _row_count() == 2, "cli 同样每次必记"
    finally:
        restore()
    print("[OK] boot and cli always record one row")


def test_timer_records_row_only_when_something_ran():
    """timer（界面 60 秒那个）空转不记行，确有执行才记——
    否则这张表每 60 秒被灌一行零值，V3 的"上次开机"反而读不出来"""
    executor = TaskExecutor()
    restore_due = _patch_get_due(lambda *a, **k: [])
    try:
        _delete_all_runs()
        executor.check_scheduled_templates()  # 默认 timer
        assert _row_count() == 0, "timer 空转（到期 0 件）不应落行"
    finally:
        restore_due()

    tid = tt.create_template(
        _new_template_name("定时记行"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "记行关键词ABC"}, auto_approve=0)
    _reset_template_goal_dir(tid)
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        _delete_all_runs()
        results = executor.check_scheduled_templates()  # 默认 timer，有到期模板
        assert len(results) == 1 and results[0]["paused"] is True
        row = _latest_run_row()
        assert row["trigger_source"] == tt.TRIGGER_SOURCE_TIMER
        assert row["paused"] == 1, "确有执行（停在审批点也算）就要记行"
        # 同一周期再跑：到期 0 件 → timer 不再记行（库里行数不变）
        n = _row_count()
        executor.check_scheduled_templates()
        assert _row_count() == n, "timer 第二次空转不应追加行"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] timer records row only when something ran")


# ---------- 计数器与不变式 ----------

def test_counter_invariant_holds():
    """不变式 due_count == ran + paused + backlog_skip + claim_skip + failed：
    用一个"两个到期模板、一个执行一个被堆积挡住"的现场把它撑满"""
    executor = TaskExecutor()
    _delete_all_runs()
    tid_run = tt.create_template(
        _new_template_name("不变式执行"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "不变式执行词ABC"}, auto_approve=0)
    tid_backlog = tt.create_template(
        _new_template_name("不变式堆积"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "不变式堆积词ABC"}, auto_approve=0)
    _insert_pending(tt.template_goal_id(tid_backlog), "搜索「不变式堆积词ABC」相关资料")
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        results = executor.check_scheduled_templates(trigger_source="cli")
        row = _latest_run_row()
        total = (row["ran"] + row["paused"] + row["backlog_skip"]
                 + row["claim_skip"] + row["failed"])
        assert row["due_count"] == total, \
            f"不变式破了：due={row['due_count']} 五去向之和={total}"
        assert row["due_count"] == 2
        assert row["backlog_skip"] == 1 and row["paused"] == 1
        assert len(results) == 2, "堆积跳过也要进返回值（它是一次决定）"
        assert any("跳过" in r["message"] for r in results)
    finally:
        restore_search()
        restore_chat()
    for tid in (tid_run, tid_backlog):
        assert tt.delete_template(tid)
    print("[OK] counter invariant holds")


def test_claim_skip_is_counted_and_printed():
    """抢占失败分支：这是本批要消灭的那个静默点——库里要有 claim_skip 计数，
    屏幕上要有一行提示（缺任一即"什么都没发生又什么都不留"）"""
    executor = TaskExecutor()
    _delete_all_runs()
    tid = tt.create_template(
        _new_template_name("抢占失败"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "抢占失败词ABC"}, auto_approve=0)
    restore_claim = _patch_update_last_run(lambda *a, **k: False)
    restore_search = _patch_web_search(lambda query, **kw: "1. 不应执行\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 不应执行")
    try:
        results, out = _capture_stdout(
            executor.check_scheduled_templates, trigger_source="cli")
        assert results == [], "抢占失败不进返回值（定案第 3 条：计数走局部变量）"
        row = _latest_run_row()
        assert row["due_count"] == 1 and row["claim_skip"] == 1
        assert row["ran"] == 0 and row["paused"] == 0 and row["failed"] == 0
        assert row["due_count"] == (row["ran"] + row["paused"] + row["backlog_skip"]
                                    + row["claim_skip"] + row["failed"])
        assert row["status"] == "ok", "抢占失败是并发语义，不是出错（partial 只留给 failed）"
        assert "抢占失败" in out, f"抢占失败必须出声，实际输出：{out!r}"
    finally:
        restore_claim()
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] claim skip is counted and printed")


def test_read_failure_row_shape():
    """到期清单读不出来：boot/cli 那一行记成 status='error'、due_count=NULL、
    note 写原因（不再是静默的 0）。timer 路径同样不落行——写行条件按定案第 2 条，
    "读失败但没有任何东西被执行"在 60 秒路径上不进库，只出声（下方断言）"""
    executor = TaskExecutor()
    _delete_all_runs()

    def _boom(*a, **k):
        raise RuntimeError("模拟到期清单读取失败")

    restore = _patch_get_due(_boom)
    try:
        results, out = _capture_stdout(
            executor.check_scheduled_templates, trigger_source="boot")
        assert results == []
        row = _latest_run_row()
        assert row["status"] == "error", "读失败应记 error，不是 ok"
        assert row["due_count"] is None, "读失败时 due_count 必须是 NULL（≠0 件）"
        assert "模拟到期清单读取失败" in (row["note"] or ""), "原因要留在库里"

        n = _row_count()
        _, out_timer = _capture_stdout(executor.check_scheduled_templates)
        assert _row_count() == n, "timer 读失败不落行（写行条件只认 ran/paused/failed）"
        assert "定时模板检查失败" in out_timer, "不落行就必须出声，两者不能都省"
    finally:
        restore()
    print("[OK] read failure row shape")


# ---------- 时钟轴（C9）----------

def test_utc_clock_and_local_last_run():
    """两根轴各留各的写法：运行行的起止是 UTC（带 +00:00），
    而 task_templates.last_run_at **保持本地无偏移**——把它改成 UTC 会让
    `_is_due` 读出的时刻差 8 小时、模板永久到期（批次 14 定案第 5 条）"""
    executor = TaskExecutor()
    _delete_all_runs()
    tid = tt.create_template(
        _new_template_name("时钟轴"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "时钟轴关键词ABC"}, auto_approve=0)
    _reset_template_goal_dir(tid)
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        executor.check_scheduled_templates(trigger_source="cli")
        row = _latest_run_row()
        for field in ("started_at", "finished_at"):
            value = row[field]
            assert value.endswith("+00:00"), f"{field} 应为 UTC：{value}"
            parsed = datetime.fromisoformat(value)
            assert parsed.utcoffset().total_seconds() == 0
        assert row["started_at"] <= row["finished_at"]

        last_run = tt.get_template(tid).last_run_at
        assert last_run and "+00:00" not in last_run and not last_run.endswith("Z"), \
            f"last_run_at 必须仍是本地写法：{last_run}"
        assert datetime.fromisoformat(last_run).tzinfo is None
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] utc clock and local last_run")


# ---------- 归属与库指纹（C4 / C8）----------

def test_run_id_attribution_and_produced_rows():
    """C4 撤销单位＝一次运行：本次产出的 pending 行被标上 run_id，
    跑前就存在的行**不被认领**（差值法只认 id > 跑前基线）"""
    executor = TaskExecutor()
    _delete_all_runs()
    other_goal = tt.template_goal_id(999999)
    _insert_pending(other_goal, "搜索「跑前就有的行ABC」相关资料")

    tid = tt.create_template(
        _new_template_name("归属"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "归属关键词ABC"}, auto_approve=0)
    _reset_template_goal_dir(tid)
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        executor.check_scheduled_templates(trigger_source="cli")
        row = _latest_run_row()
        assert row["produced_rows"] == 1, "停在审批点＝产出一条待审批行"
        mine = _pending_rows_by_title("归属关键词ABC")
        assert len(mine) == 1 and mine[0]["run_id"] == row["id"], \
            "本次产出的行应标上本次 run_id"
        old = _pending_rows_by_title("跑前就有的行ABC")
        assert old[0]["run_id"] is None, "跑前基线之前的行不能被本次认领"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] run_id attribution and produced_rows")


def test_db_fingerprint_points_at_this_db():
    """C8「连到了哪个库」：绝对路径 ＋ 文件字节数 ＋ 该库 pending 总数。
    这一列挡的是"接错库／写进平行库"——本席自己就踩过一次（不带副本的目录是陈旧库）"""
    executor = TaskExecutor()
    _delete_all_runs()
    restore = _patch_get_due(lambda *a, **k: [])
    try:
        executor.check_scheduled_templates(trigger_source="cli")
    finally:
        restore()
    row = _latest_run_row()
    expected = os.path.abspath(_database_module.DB_PATH)
    assert row["db_path"] == expected, "db_path 应是本次真正连上的绝对路径"
    assert os.path.isabs(row["db_path"])
    # db_bytes 是**写行之前**的快照（指纹在 INSERT 之前取），所以它只要求"取到了、
    # 且不比当前文件大"——拿它和 getsize 逐字节相等会因这次 INSERT 自己撑大的页而误报
    assert row["db_bytes"] > 0, "db_bytes 应取到真实文件大小"
    assert row["db_bytes"] <= os.path.getsize(expected)
    conn = get_connection()
    try:
        pending_now = conn.execute(
            "SELECT COUNT(*) AS c FROM pending_approvals WHERE status='pending'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert row["db_pending_total"] == pending_now, "pending 总数应与该库实况一致"
    print("[OK] db fingerprint points at this db")


def test_latest_pending_id_baseline_contract():
    """跑前基线与读写接口的失败契约：空表 → 0（"所有行都属于将来"），
    查询失败 → None（"不知道基线"，调用方据此跳过归属），写行失败 → None 且不抛异常。
    拿 0 兜底失败分支＝把库里全部历史行认领成本次产出，是最坏的一种静默"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM pending_approvals")
        conn.commit()
    finally:
        conn.close()
    assert tt.latest_pending_approval_id() == 0, "空表应返回 0"

    restore_db = _patch_get_db_to_fail()
    try:
        value, out = _capture_stdout(tt.latest_pending_approval_id)
        assert value is None, f"连接失败必须返回 None（不能返回 0），实际 {value}"
        assert "审批基线 id 查询失败" in out, "取不到基线要出声"

        attached, out_attach = _capture_stdout(tt.attach_pending_approvals_to_run, 1, 0)
        assert attached == 0, "归属失败应返回 0 行，而不是把全部历史行标上"
        assert "run_id 归属失败" in out_attach

        written, out_record = _capture_stdout(
            tt.record_template_run, trigger_source="cli",
            started_at=_utc_text(), finished_at=_utc_text())
        assert written is None, "写行失败应返回 None，让调用方知道这次没落库"
        assert "运行记录写入失败" in out_record, "写行失败要出声，不静默返回"
    finally:
        restore_db()
    print("[OK] latest pending id baseline contract")


def _patch_get_db_to_fail():
    """临时让本模块的取连接失败（连接层故障分支靠这一种写法覆盖，不造真损坏库）"""
    original = tt.get_db

    def _broken_get_db(*a, **k):
        raise RuntimeError("模拟连接失败")

    tt.get_db = _broken_get_db

    def restore():
        tt.get_db = original
    return restore


# ---------- V4：同一批次连跑两次 ----------

def test_same_batch_twice_second_run_all_skipped():
    """V4 那一格（报件里今天唯一没演示过的验收）：同一个入口把同一批次跑第二次，
    第二次必须 due_count=0、产出 0 行，且两次的痕迹都能按 C8 读出来。
    这是"换一台机器／换一个触发源跑"的前置能力——去重逻辑只有 `_is_due` 一处"""
    executor = TaskExecutor()
    _delete_all_runs()
    tid = tt.create_template(
        _new_template_name("连跑两次"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "连跑两次词ABC"}, auto_approve=0)
    _reset_template_goal_dir(tid)
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        first = executor.check_scheduled_templates(
            trigger_source=tt.TRIGGER_SOURCE_CLI)
        row1 = _latest_run_row()
        assert len(first) == 1 and first[0]["paused"] is True
        assert row1["due_count"] == 1 and row1["produced_rows"] == 1

        second = executor.check_scheduled_templates(
            trigger_source=tt.TRIGGER_SOURCE_CLI)
        row2 = _latest_run_row()
        assert second == [], "第二次不应有任何模板进入执行"
        assert row2["id"] > row1["id"], "第二次也要留下自己的运行行"
        assert row2["due_count"] == 0, (
            f"第二次应读到到期 0 件（这一期已算过），实际 {row2['due_count']}")
        for field in ("ran", "paused", "backlog_skip", "claim_skip", "failed",
                      "produced_rows"):
            assert row2[field] == 0, f"第二次不应有 {field} 计数"
        assert _pending_count_by_title("连跑两次词ABC") == 1, \
            "同批次重复补算＝失败（报件 V1 的可数形态）：待审批只能有一条"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] same batch twice second run all skipped")


# ---------- 接线 ----------

class _RecordingExecutor:
    """替身执行器：只记录 do_check_scheduled_templates 传下来的参数"""

    def __init__(self):
        self.calls = []
        self.done = threading.Event()

    def check_scheduled_templates(self, **kwargs):
        self.calls.append(kwargs)
        self.done.set()
        return []


def test_boot_controller_passes_trigger_source():
    """接线验证（V1/V3 的地基）：界面回调把 trigger_source 原样交给执行器——
    开机路径传 boot、gr.Timer 那条走默认 timer。传丢了就是库里少一类行，
    而界面上一切照常，没人会发现"""
    # 延迟导入：gradio 只在需要这条用例时加载（导入 ui.controllers._shared 会
    # 在**导入期**调 init_db()，此时 DB_PATH 已指向临时库，业务库不受影响）
    from ui.controllers import templates_controller as tc

    stub = _RecordingExecutor()
    original = tc.task_executor
    tc.task_executor = stub
    try:
        message = tc.do_check_scheduled_templates(trigger_source="boot")
        assert stub.done.wait(5), "后台线程应在数秒内调用执行器"
        assert stub.calls == [{"trigger_source": "boot"}], \
            f"回调应原样透传触发源，实际 {stub.calls}"
        assert isinstance(message, str) and message, "回调仍返回状态字符串"

        stub.done.clear()
        stub.calls = []
        tc.do_check_scheduled_templates()
        assert stub.done.wait(5)
        assert stub.calls == [{"trigger_source": "timer"}], \
            "默认值必须是 timer（界面 60 秒那条路径的空转不该记行）"
    finally:
        tc.task_executor = original
    print("[OK] boot controller passes trigger_source")


def test_cleanup():
    """清理测试产物（临时数据库目录 + 本次新建的模板结果目录）"""
    cleanup_test_db()
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== template runs test ===\n")
    setup()
    test_independent_database_and_new_table()
    test_record_and_read_back()
    test_boot_and_cli_always_record_one_row()
    test_timer_records_row_only_when_something_ran()
    test_counter_invariant_holds()
    test_claim_skip_is_counted_and_printed()
    test_read_failure_row_shape()
    test_utc_clock_and_local_last_run()
    test_run_id_attribution_and_produced_rows()
    test_db_fingerprint_points_at_this_db()
    test_same_batch_twice_second_run_all_skipped()
    test_boot_controller_passes_trigger_source()
    # 这一条会清空 pending_approvals（差值法的 id 空洞会让之后的 produced_rows 虚高），
    # 所以它排在所有断言 produced_rows 的用例之后、收尾之前——顺序是断言的一部分
    test_latest_pending_id_baseline_contract()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
