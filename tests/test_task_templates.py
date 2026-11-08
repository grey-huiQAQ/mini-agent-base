# -*- coding: utf-8 -*-
"""
任务模板（自动化任务模板，一键复用）模块测试（离线：mock web_search / chat_sync）
覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
- 模板 CRUD：创建 / 查询 / 列表 / 更新 / 停用 / 删除
- 定时到期判断：daily / weekly 触发规则（未到时间、从未运行、当天已运行、
  隔天补跑、停用不触发、manual 永不自动触发）
- 手动触发：execute_template → 生成计划任务事件 + 持久化审批记录；
  批准后落盘、拒绝不落盘；**触发即记账**（决定执行就消费当期，P7 批次 2）与
  生成器跑完后更新 last_run_at（后者是缺席页眉的天数基准）
- 自动审批开关：auto_approve=1 时不暂停、结果自动保存；=0 时正常产生待审批
- 定时调度：check_scheduled_templates 执行到期模板、先更新 last_run_at 防重入
  （同一周期不会重复触发）、手动审批模板在首个审批点暂停保留 pending 记录
- 模板目录分离：每个模板独立结果目录（data/goals/template_{id}/），
  审批记录通过 goal_id 关联到对应模板
- 待审批堆积控制：模板存在未处理待审批时，定时/手动触发均跳过（不执行、
  不更新 last_run_at）；**处理完之后不会立即重跑**——下一次产出等它自己的触发日
  （创建那条待审批的触发已经消费了当期，见 P7 批次 2 的"触发即记账"）
- 「资源」格（P7 批次 1a，动作类型 scout_digest）：计划构造、本地机会库筛选
  （临期必进格 / 无时点默认类两条都要满足 / 未初筛只计数 / 空状态如实输出）、
  产出顶部的缺席页眉取**抢占前**的 last_run_at（含"传了 None ＝ 首次运行"这条分支）
- UI 回调契约：模板 Tab 的 do_run_template / 审批回调返回元组与 outputs 匹配
"""
import atexit
import os
import shutil
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；保留 _TMP_DIR / _TMP_DB 两个旧变量名，既有断言无需改动。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_connection
from memory.approval_store import ApprovalStore
from memory.note_store import NoteStore
from core import task_template as tt
from agents import task_executor as te_module
from agents.task_executor import TaskExecutor

# 模板执行的虚拟目标目录：每个模板独立（data/goals/template_{id}/，
# 约定见 core.task_template.template_goal_id）
_GOALS_ROOT = Path("data") / "goals"


def _template_goal_dir(template_id) -> Path:
    """某个模板的执行结果目录（与实现约定保持一致）"""
    return _GOALS_ROOT / tt.template_goal_id(template_id)


# 测试启动前已存在的 template_* 目录快照：清理时只删除本次测试新建的目录，
# 避免误删业务运行产生的真实模板数据
_pre_existing_template_dirs = {
    p.name for p in _GOALS_ROOT.glob("template_*") if p.is_dir()
} if _GOALS_ROOT.exists() else set()


def _reset_template_goal_dir(template_id):
    """清空指定模板的结果目录（notes / research_notes 子目录）

    每个执行类测试开始前调用，保证各测试互相独立（不依赖上一次的执行结果）。
    """
    goal_dir = _template_goal_dir(template_id)
    if goal_dir.exists():
        for sub in ("notes", "research_notes"):
            sub_dir = goal_dir / sub
            if sub_dir.exists():
                shutil.rmtree(sub_dir, ignore_errors=True)


def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()


# ---------- mock 工具 ----------

def _patch_web_search(fake):
    """临时替换 local_tools.web_search（TaskExecutor 经模块引用调用），返回还原函数"""
    original = te_module.local_tools.web_search
    te_module.local_tools.web_search = fake

    def restore():
        te_module.local_tools.web_search = original
    return restore


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync（笔记草稿 LLM 调用），返回还原函数"""
    original = te_module.model_client.chat_sync
    te_module.model_client.chat_sync = fake

    def restore():
        te_module.model_client.chat_sync = original
    return restore


def _drive(gen, approve=True, first_send=None):
    """消费执行生成器直到 done 事件

    Args:
        gen: execute_template / execute_plan 生成器
        approve: True 时批准每个审批暂停点；False 时全部拒绝
        first_send: 当生成器已暂停在审批点时，传入首次决定（True/False）；
                    否则传 None（从生成器的下一个事件开始消费）

    Returns:
        (done 事件, 遇到的审批事件列表)
    """
    approvals = []
    send_val = first_send
    while True:
        event = gen.send(send_val) if send_val is not None else next(gen)
        send_val = None
        if event.get("type") == "approval":
            approvals.append(event)
            send_val = approve
        elif event.get("type") == "done":
            return event, approvals


def _new_template_name(prefix="模板"):
    """生成唯一模板名，便于排查与清理"""
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def _cleanup():
    """清理测试产物：本次测试新建的模板结果目录

    注意：临时数据库目录已由 tests/_test_db_helpers.setup_test_db() 的 atexit
    兜底清理（P4-3a），本函数只负责 helper 不管的文件系统产物
    （data/goals/template_*）——删掉这段会让模板产物泄漏进 data/goals/。
    """
    if _GOALS_ROOT.exists():
        for p in _GOALS_ROOT.glob("template_*"):
            if p.is_dir() and p.name not in _pre_existing_template_dirs:
                shutil.rmtree(p, ignore_errors=True)


# 进程退出时兜底清理（测试中途断言失败也能清理模板结果目录）
atexit.register(_cleanup)


# ---------- 独立测试数据库 ----------

def test_independent_database_and_table():
    """隔离验证：task_templates 表建在临时库，且临时库与默认库路径不同"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    assert _TMP_DB != os.path.join("data", "memory.db"), "不得使用默认业务库"
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_templates'"
        ).fetchall()
        assert rows, "init_db 应创建 task_templates 表"
    finally:
        conn.close()
    print("[OK] independent database and table")


# ---------- CRUD ----------

def test_template_crud():
    """模板 CRUD：创建 → 查询 → 列表 → 更新 → 停用 → 删除"""
    name = _new_template_name("CRUD")
    tid = tt.create_template(
        name, description="描述", trigger_type="daily",
        trigger_config={"time": "09:00"},
        action_params={"query": "AI竞赛"}, auto_approve=1)
    assert tid and tid > 0, "创建应返回新记录 ID"

    t = tt.get_template(tid)
    assert t is not None
    assert t.name == name
    assert t.description == "描述"
    assert t.trigger_type == "daily"
    assert t.trigger_time == "09:00"
    assert t.query == "AI竞赛"
    assert t.auto_approve == 1 and t.enabled == 1

    # 列表包含新模板；enabled_only 只含启用项
    names = [x.name for x in tt.list_templates()]
    assert name in names
    assert all(x.enabled for x in tt.list_templates(enabled_only=True))

    # 参数校验：空名称拒绝创建
    assert tt.create_template("   ") is None

    # 更新（仅更新传入字段）
    assert tt.update_template(tid, name="改名模板", trigger_config={"time": "10:30"},
                             auto_approve=0)
    t2 = tt.get_template(tid)
    assert t2.name == "改名模板"
    assert t2.trigger_time == "10:30"
    assert t2.auto_approve == 0
    assert t2.query == "AI竞赛", "未传 action_params 不应被覆盖"

    # 停用：enabled_only 不再包含；get 仍可读取
    assert tt.set_enabled(tid, False)
    assert tt.get_template(tid).enabled == 0
    assert name not in [x.name for x in tt.list_templates(enabled_only=True)]

    # 删除
    assert tt.delete_template(tid)
    assert tt.get_template(tid) is None
    assert tt.delete_template(tid) is False, "重复删除应返回 False"
    print("[OK] template crud")


# ---------- 定时到期判断 ----------

def test_due_daily():
    """每日触发：未到时间不触发；到达时间从未运行触发；当天已运行不重复；
    隔天补跑；停用不触发（2026-08-26 为周三）"""
    tid = tt.create_template(
        _new_template_name("每日"), trigger_type="daily",
        trigger_config={"time": "09:00"}, action_params={"query": "Q1"})
    assert tid

    # 08:00 未到触发时刻 → 不触发
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 8, 0))] == []
    # 09:00 到达且从未运行 → 触发
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 9, 0))]
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 23, 59))]

    # 今天 09:30 已运行 → 当天不再触发（防重复）
    assert tt.update_last_run(tid, "2026-08-26T09:30:00")
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 10, 0))] == []
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 23, 59))] == []

    # 昨天运行过 → 今天补跑触发
    assert tt.update_last_run(tid, "2026-08-25T09:30:00")
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 10, 0))]

    # 手动运行（当天 08:00）早于触发时刻 → 仍触发（手动运行不顶替定时）
    assert tt.update_last_run(tid, "2026-08-26T08:00:00")
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 9, 5))]

    # 停用 → 不触发
    assert tt.set_enabled(tid, False)
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 26, 10, 0))] == []

    assert tt.delete_template(tid)
    print("[OK] due daily")


def test_due_weekly():
    """每周触发：本周目标时刻未到不触发；到达触发；本周已运行不重复；
    跨周补跑（2026-08-24 周一 / 08-25 周二 / 09-01 周二）"""
    tid = tt.create_template(
        _new_template_name("每周二"), trigger_type="weekly",
        trigger_config={"weekday": 1, "time": "09:00"},
        action_params={"query": "Q2"})
    assert tid

    # 周一 10:00（本周二 09:00 未到）→ 不触发
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 24, 10, 0))] == []
    # 周二 09:00 到达且从未运行 → 触发
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 8, 25, 9, 0))]
    # 本周二 09:30 已运行 → 本周不再触发
    assert tt.update_last_run(tid, "2026-08-25T09:30:00")
    assert [t.id for t in tt.get_due_templates(datetime(2026, 8, 25, 23, 0))] == []
    # 下周二 09:05 → 再次触发（跨周）
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 9, 1, 9, 5))]
    # 仅上周运行过、本周未运行 → 本周补跑触发
    assert tt.update_last_run(tid, "2026-08-25T09:30:00")
    assert tid in [t.id for t in tt.get_due_templates(datetime(2026, 9, 1, 9, 5))]

    assert tt.delete_template(tid)
    print("[OK] due weekly")


def test_manual_never_due():
    """手动模板永不自动触发"""
    tid = tt.create_template(_new_template_name("手动"), trigger_type="manual",
                             action_params={"query": "Q3"})
    assert tid
    assert [t.id for t in tt.get_due_templates(datetime(2030, 1, 1, 23, 59))] == []
    assert tt.delete_template(tid)
    print("[OK] manual never due")


def test_update_last_run_unknown_id():
    """update_last_run：模板不存在返回 False（不抛异常）"""
    assert tt.update_last_run(999999, "2026-08-26T09:00:00") is False
    print("[OK] update last run unknown id")


# ---------- 执行模板：手动触发 ----------

def test_execute_template_edge_cases():
    """边界：模板不存在 / 已停用 / 缺少搜索关键词 → done 事件友好提示，不执行"""
    executor = TaskExecutor()
    # 不存在的模板
    events = list(executor.execute_template(999999, triggered_by="manual"))
    assert events[-1]["type"] == "done" and "不存在" in events[-1]["message"]
    # 缺少搜索关键词
    tid = tt.create_template(_new_template_name("无关键词"), trigger_type="manual",
                             action_params={})
    events2 = list(executor.execute_template(tid, triggered_by="manual"))
    assert events2[-1]["type"] == "done" and "缺少有效" in events2[-1]["message"]
    # 已停用
    assert tt.update_template(tid, enabled=0)
    events3 = list(executor.execute_template(tid, triggered_by="manual"))
    assert events3[-1]["type"] == "done" and "停用" in events3[-1]["message"]
    assert tt.delete_template(tid)
    print("[OK] execute template edge cases")


def test_execute_template_manual_approval():
    """手动触发（需审批）：生成两条自动任务与两条持久化审批记录；
    批准后全部落盘（文件/笔记）、审批记录转 approved、last_run_at 更新"""
    tid = tt.create_template(
        _new_template_name("手动触发"), trigger_type="manual",
        action_params={"query": "测试关键词XYZ"})
    assert tid
    _reset_template_goal_dir(tid)
    executor = TaskExecutor()
    fake_search = "1. 结果一\n   摘要内容"
    restore_search = _patch_web_search(lambda query, **kw: fake_search)
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 要点A\n- 要点B")
    try:
        gen = executor.execute_template(tid, triggered_by="manual")
        # 消费到第一个审批暂停点：验证进度事件与审批记录
        approvals = []
        events = []
        ev = next(gen)
        while ev["type"] != "approval":
            events.append(ev)
            ev = next(gen)
        approvals.append(ev)

        # 生成了计划任务记录（进度事件声明 2 个自动任务）
        progress_msgs = [e["message"] for e in events if e["type"] == "progress"]
        assert any("共生成 2 个自动任务" in m for m in progress_msgs), \
            "模板应生成 2 条自动任务（搜索 + 整理）"
        # 审批记录已持久化（pending 状态），且关联到该模板的独立目标目录
        approval = ev["approval"]
        assert approval["type"] == "search"
        assert "测试关键词XYZ" in approval["content"]
        expected_path = f"data/goals/template_{tid}/research_notes"
        assert approval["suggested_path"].replace("\\", "/").endswith(
            expected_path), "审批建议保存路径应为该模板的独立目录"
        pending_id = approval.get("pending_id")
        assert pending_id, "审批暂停前应持久化 pending 记录"
        rec = ApprovalStore.get_pending(pending_id)
        assert rec is not None and rec["status"] == "pending"
        assert rec["task_title"] == "搜索「测试关键词XYZ」相关资料"
        assert rec["goal_id"] == tt.template_goal_id(tid), \
            "待审批记录应关联该模板的虚拟目标 ID"

        # 批准第一条 → 继续执行第二条（整理笔记）→ 全部批准到 done
        # （生成器正暂停在第一条审批上，须用 send(True) 恢复而不是 next）
        done, all_approvals = _drive(gen, approve=True, first_send=True)
        approvals.extend(all_approvals)
        assert len(approvals) == 2, "搜索 + 整理应产生两条审批"
        assert done["type"] == "done"
        assert done["stats"]["saved"] == 2 and done["stats"]["rejected"] == 0

        # 审批记录全部 approved
        for a in approvals:
            pid = a["approval"].get("pending_id")
            if pid:
                assert ApprovalStore.get_pending(pid)["status"] == "approved"

        # last_run_at 已更新
        t = tt.get_template(tid)
        assert t is not None and t.last_run_at, "执行完成后应更新 last_run_at"

        # 搜索文件已写入该模板的独立虚拟目标目录
        research_dir = _template_goal_dir(tid) / "research_notes"
        files = list(research_dir.glob("*.md")) if research_dir.exists() else []
        assert files, "批准后应在该模板的独立目录下生成文件"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] execute template manual approval")


def test_execute_template_reject_does_not_write():
    """手动触发（全部拒绝）：审批记录转 rejected，磁盘无任何新文件"""
    tid = tt.create_template(
        _new_template_name("拒绝流"), trigger_type="manual",
        action_params={"query": "拒绝关键词ABC"})
    _reset_template_goal_dir(tid)
    executor = TaskExecutor()
    restore_search = _patch_web_search(lambda query, **kw: "1. 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        gen = executor.execute_template(tid, triggered_by="manual")
        done, approvals = _drive(gen, approve=False)
        assert done["stats"]["saved"] == 0 and done["stats"]["rejected"] == 2
        for a in approvals:
            pid = a["approval"].get("pending_id")
            if pid:
                assert ApprovalStore.get_pending(pid)["status"] == "rejected"
        # 未产生任何文件（该模板的独立目录不应存在）
        assert not (_template_goal_dir(tid) / "research_notes").exists(), \
            "拒绝后不应写入搜索文件"
        assert not (_template_goal_dir(tid) / "notes").exists(), "拒绝后不应写入笔记"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] execute template reject does not write")


# ---------- 自动审批开关 ----------

def test_execute_template_auto_approve():
    """自动审批开关：auto_approve=1 → 执行不暂停、结果自动保存并落盘；
    审批记录自动置为 approved"""
    tid = tt.create_template(
        _new_template_name("自动审批"), trigger_type="manual",
        action_params={"query": "自动关键词ABC"}, auto_approve=1)
    _reset_template_goal_dir(tid)
    executor = TaskExecutor()
    restore_search = _patch_web_search(lambda query, **kw: "1. 自动结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 自动笔记内容")
    try:
        events = list(executor.execute_template(tid, triggered_by="manual"))
        types = [e["type"] for e in events]
        assert "approval" not in types, "自动审批模板不应产生暂停点"
        done = events[-1]
        assert done["type"] == "done"
        assert done["stats"]["saved"] == 2, "自动审批应自动保存全部结果"

        # 审批记录已生成且状态为 approved（自动通过）
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT status FROM pending_approvals "
                "WHERE task_title LIKE '%自动关键词ABC%'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2, "自动审批也应产生持久化审批记录"
        assert all(r["status"] == "approved" for r in rows)

        # 搜索文件与笔记文件均落盘（该模板的独立目录）
        research_files = list((_template_goal_dir(tid) / "research_notes").glob("*.md"))
        note_files = list((_template_goal_dir(tid) / "notes").glob("*.md"))
        assert research_files, "自动审批应写入搜索文件"
        assert note_files, "自动审批应写入笔记文件"

        # last_run_at 已更新
        assert tt.get_template(tid).last_run_at
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] execute template auto approve")


# ---------- 定时调度（check_scheduled_templates） ----------

def test_check_scheduled_templates_auto():
    """定时触发（自动审批）：到期模板被执行；last_run_at 先更新实现防重入，
    同一周期重复检查不会再次触发"""
    # 触发时间设为 00:00：任何时刻运行测试都会到期（从未运行过）
    tid = tt.create_template(
        _new_template_name("定时自动"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "定时关键词ABC"}, auto_approve=1)
    _reset_template_goal_dir(tid)
    executor = TaskExecutor()
    restore_search = _patch_web_search(lambda query, **kw: "1. 定时结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 定时笔记")
    try:
        results = executor.check_scheduled_templates()
        assert len(results) == 1, "应有一个到期模板被执行"
        r = results[0]
        assert r["template_id"] == tid
        assert r["ok"] is True and r["paused"] is False
        assert "执行完成" in r["message"]

        # last_run_at 已被更新（抢占）→ 再次检查不再触发（防重入）
        assert tt.get_template(tid).last_run_at, "调度执行后应更新 last_run_at"
        results2 = executor.check_scheduled_templates()
        assert results2 == [], "同一周期内不应重复触发已执行过的模板"

        # 自动审批结果已落盘（该模板的独立目录）
        assert list((_template_goal_dir(tid) / "research_notes").glob("*.md")), \
            "定时自动审批应写入搜索文件"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] check scheduled templates auto")


def test_check_scheduled_templates_manual_pending():
    """定时触发（手动审批）：执行到首个审批点暂停，审批记录保留 pending
    供用户处理（绝不自动写盘）；last_run_at 已抢占防重入"""
    tid = tt.create_template(
        _new_template_name("定时手动"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "手动定时词ABC"}, auto_approve=0)
    _reset_template_goal_dir(tid)
    executor = TaskExecutor()
    restore_search = _patch_web_search(lambda query, **kw: "1. 定时结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 内容")
    try:
        results = executor.check_scheduled_templates()
        assert len(results) == 1
        r = results[0]
        assert r["ok"] is True and r["paused"] is True, \
            "手动审批模板应在首个审批点暂停等待用户处理"
        assert "待审批" in r["message"]

        # 审批记录保持 pending（用户稍后从「待审批」列表处理）
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT status FROM pending_approvals "
                "WHERE task_title LIKE '%手动定时词ABC%'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, "应产生一条待审批记录"
        assert rows[0]["status"] == "pending", "定时手动审批不得自动通过/拒绝"

        # 防重入：当天不再重复触发
        assert executor.check_scheduled_templates() == []
        assert tt.get_template(tid).last_run_at, "调度执行后应抢占 last_run_at"

        # 未自动写盘（安全默认；该模板的独立目录不应存在）
        assert not (_template_goal_dir(tid) / "research_notes").exists(), \
            "未经用户确认不得写入任何文件"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] check scheduled templates manual pending")


def test_scheduled_skip_when_pending_backlog():
    """待审批堆积控制：模板存在未处理待审批时，定时触发跳过（不执行、
    不更新 last_run_at，保持到期状态）。

    **恢复的归因是本用例的模板从未运行过（last_run_at is None），不是"堆积跳过"的
    后果**（P7 批次 4-④ 更正措辞）：跳过路径从不记账，所以它处理完后仍是"到期"、
    下一次检查自然会被触发。而归因写错会误导后来人——由一次**已记账**的触发创建的
    pending（手动跑过、或定时抢占过），处理完之后**不会**立即重跑、下一次产出等它
    自己的触发日（见 agents/task_executor.py 模块 docstring 的「待审批堆积控制」条）。
    """
    tid = tt.create_template(
        _new_template_name("堆积控制"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_params={"query": "堆积控制词ABC"}, auto_approve=1)
    _reset_template_goal_dir(tid)
    # 直接插入一条属于该模板的 pending 审批记录（模拟上次运行尚未处理）
    goal_id = tt.template_goal_id(tid)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pending_approvals (task_type, task_title, goal_id, "
            "content, status) VALUES ('search', '搜索「堆积控制词ABC」相关资料', "
            "?, '草稿内容', 'pending')",
            (goal_id,),
        )
        conn.commit()
    finally:
        conn.close()

    executor = TaskExecutor()
    restore_search = _patch_web_search(lambda q, **kw: "1. 不应执行\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- 不应执行")
    try:
        # 存在未处理待审批 → 跳过本次触发（不执行、不写盘、不更新 last_run_at）
        results = executor.check_scheduled_templates()
        assert len(results) == 1, "应返回一条跳过结果"
        r = results[0]
        assert r["template_id"] == tid
        assert r["ok"] is True and r["paused"] is True
        assert "待审批" in r["message"] and "跳过" in r["message"]
        assert not (_template_goal_dir(tid) / "research_notes").exists(), \
            "堆积时不得执行模板（不应写入任何文件）"
        assert tt.get_template(tid).last_run_at is None, \
            "堆积跳过不应更新 last_run_at（本用例的模板从未运行过＝仍到期，" \
            "这才是它随后被恢复触发的原因，不是「堆积跳过」的功劳）"

        # 手动路径同样受堆积控制：execute_template 直接给出跳过提示
        events_manual = list(executor.execute_template(tid, triggered_by="manual"))
        assert events_manual[-1]["type"] == "done"
        assert "待审批" in events_manual[-1]["message"], \
            "手动触发遇到未处理待审批时也应跳过并提示"
        assert tt.get_template(tid).last_run_at is None, \
            "手动跳过同样不应更新 last_run_at"

        # 处理完待审批 → 下一检查周期恢复触发并完整执行
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?",
                         (goal_id,))
            conn.commit()
        finally:
            conn.close()
        results2 = executor.check_scheduled_templates()
        assert len(results2) == 1, "清理积压后应恢复触发"
        assert results2[0]["paused"] is False, "自动审批模板应完整执行到结束"
        assert tt.get_template(tid).last_run_at, "恢复执行后应更新 last_run_at"
        assert list((_template_goal_dir(tid) / "research_notes").glob("*.md")), \
            "恢复执行后应写入搜索文件"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] scheduled skip when pending backlog")


# ---------- UI 回调契约 ----------

def test_ui_callback_contract():
    """模板 Tab 回调契约：do_run_template / 审批回调返回元组与 outputs 匹配"""
    # 延迟导入 app（导入时 init_db 已在临时库执行，业务库不受影响）
    from app import (
        do_run_template,
        do_approve_template_pending,
        do_reject_template_pending,
    )

    # 未选择模板 / 无待审批 → 友好提示而非崩溃
    res = do_run_template(None)
    assert isinstance(res, tuple) and len(res) == 4, \
        "do_run_template 必须返回四元组以匹配 outputs"
    assert "请先选择一个模板" in res[0]
    for fn in (do_approve_template_pending, do_reject_template_pending):
        r = fn(None)
        assert isinstance(r, tuple) and len(r) == 4
        assert "没有待审批" in r[0]

    # 真实执行：手动模板（需审批）→ 暂停 → 批准 → 结束并清空 State
    tid = tt.create_template(
        _new_template_name("UI模板"), trigger_type="manual",
        action_params={"query": "UI契约关键词"})
    restore_search = _patch_web_search(lambda query, **kw: "1. UI 结果\n   内容")
    restore_chat = _patch_chat_sync(lambda messages, **kw: "- UI 笔记")
    try:
        paused = do_run_template(tid)
        status, display, audio, gen_state = paused
        assert "暂停等待审批" in status
        assert isinstance(display, str) and display
        assert gen_state is not None, "暂停中的生成器应存入 State"

        # 模板生成 2 条自动任务（搜索 + 整理）→ 需批准 2 次才执行结束；
        # 循环批准直到 State 清空（每次批准返回下一暂停点或结束态；
        # 中间暂停点会携带下一条待审批的提示音，仅结束时音频槽为空）
        result = None
        while gen_state is not None:
            result = do_approve_template_pending(gen_state)
            status_i, display_i, audio_i, gen_state_i = result
            assert isinstance(status_i, str) and isinstance(display_i, str)
            if gen_state_i is None:
                assert audio_i == "", "执行结束后提示音插槽应为空"
            gen_state = gen_state_i
        status2, display2, audio2, gen_state2 = result
        assert "执行结束" in status2
        assert gen_state2 is None, "执行结束后 State 应置 None 防止重复恢复"
    finally:
        restore_search()
        restore_chat()
    assert tt.delete_template(tid)
    print("[OK] ui callback contract")


# ---------- 「资源」格（P7 批次 1a：动作类型 scout_digest，零网络 / 零 LLM） ----------

def _clear_opportunities():
    """清空测试库的机会表（临时库只归本文件使用）

    本格的筛选结果依赖"库里有什么"，不清空就会有测试间串扰——两个用例都会
    因为对方插的行而改变断言，那种失败最难查。
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


# ---------- 「资源」格用例的固定时钟（P7 批次 9-①） ----------
# 为什么需要它：本文件原先把**播种基准**与**判定基准**分成了两个钟——
# `_insert_opportunity` 用 `datetime.now() - timedelta(days=N)` 播种（真钟），
# 而用例把 `today = date(2026, 9, 19)` 钉死（假钟）。两者之间的距离随**真实日期**增长，
# 于是「无时点」那一类的休眠判定（`(today - discovered_at).days >= _DIGEST_DORMANT_DAYS`，
# 阈值 14 天）会自己变红：失效条件是 `播种天数 − (真实今天 − 2026-09-19) < 14`。
# 这不是模型或筛选逻辑的问题，是**测试自己有两个钟**。
# 不变量：同一个用例里，播种用的时刻与判定用的今天，必须来自**同一个常量**。
# （`date(2026, 9, 19)` 是周六，+7 天 = 2026-09-26；带 12:00 是为了让按天相减不踩边界。）
_PINNED_TODAY = datetime(2026, 9, 19, 12, 0, 0)


def _insert_opportunity(title, deadline=None, status="new", discovered_days_ago=30,
                        value=None, reuse=None, eligibility="unknown",
                        ai_eligibility=None, summary=None, base=None):
    """向测试库插入一条机会（列名取迁移后的真实列），返回新行 ID

    base：播种基准时刻；`None`（默认）＝真钟 `datetime.now()`。
    **凡是用例自己钉了 `today` 的，都必须传 `base=_PINNED_TODAY`**——否则
    `discovered_at` 与判定用的 `today` 分属两个钟，与真实日期赛跑，断言会自己变红
    （见 `_PINNED_TODAY` 处的说明）。反过来，**判定也用真钟**的用例（例如
    `test_scout_digest_watermark_end_to_end`，它走 `handle_scout_digest_task`，
    内部不接受 today）应当保持默认的真钟，并把**截止日**写成相对今天的天数。
    """
    when = ((base or datetime.now()) - timedelta(days=discovered_days_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO opportunities (title, source_url, status, discovered_at, "
            "deadline, ai_value_score, ai_project_reuse, eligibility_hint, "
            "ai_eligibility, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, f"https://example.invalid/{uuid.uuid4().hex}", status, when,
             deadline, value, reuse, eligibility, ai_eligibility, summary))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_resource_digest_filters():
    """「资源」格三筛子：带时点临期（含已过）必进格；无时点那一类要同时满足
    「当下需要」（价值分）与「够得着」（可复用度 + 资格）；未初筛只计数不列；
    已忽略、刚发现、尚未临期的一律不进格，且不静默丢弃（有计数）"""
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()          # 周六；+7 天 = 2026-09-26
    _insert_opportunity("临期未过A", deadline="2026-09-22", discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("临期已过B", deadline="2026-09-13", discovered_days_ago=20, base=_PINNED_TODAY)
    _insert_opportunity("更远截止C", deadline="2026-12-31", discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("无时点达标D", discovered_days_ago=17, value=0.7, reuse=0.6, base=_PINNED_TODAY)
    _insert_opportunity("无时点低价值E", discovered_days_ago=17, value=0.05, reuse=0.5, base=_PINNED_TODAY)
    _insert_opportunity("无时点够不着F", discovered_days_ago=17, value=0.7, reuse=0.6,
                        eligibility="likely_no", base=_PINNED_TODAY)
    _insert_opportunity("无时点未初筛G", discovered_days_ago=17, base=_PINNED_TODAY)
    _insert_opportunity("刚发现H", discovered_days_ago=2, value=0.9, reuse=0.9, base=_PINNED_TODAY)
    _insert_opportunity("已忽略I", discovered_days_ago=17, status="ignored",
                        value=0.9, reuse=0.9, base=_PINNED_TODAY)

    md = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                  today=today)

    # ① 有无时点：临期窗口内的必须进格（已过也算，他不在时更该看见）
    assert "临期未过A" in md and "临期已过B" in md, "报名截止落在 +7 天内必须进格"
    assert "2026-09-26" in md, "段标题应写明「下一个可能开机日」的日期"
    # 尚未临期的带时点条目**不进两个可见段**；它们只在未列入段里按"最近 3 条"展开
    # （P7 批次 1b-② 起，见 test_resource_digest_far_preview），故这里分段判定
    head, _, tail = md.partition("**未列入（如实计数，不是判定）**")
    assert "更远截止C" not in head, "尚未临期的带时点条目不进临期/无时点两段"
    assert "更远截止C" in tail, "但应作为未列入段的「最近」条目被展开"

    # ②③ 无时点默认类：当下需要 + 够得着，两条都要
    assert "无时点达标D" in md, "长期挂着 + 高价值 + 有路径应进格"
    for absent in ("无时点低价值E", "无时点够不着F", "无时点未初筛G",
                   "刚发现H", "已忽略I"):
        assert absent not in md, f"{absent} 不应出现在本格"

    # 未列入的必须如实计数（不静默丢弃）
    assert "你已忽略：1 条" in md
    assert f"无时点但悬挂不足 {rs._DIGEST_DORMANT_DAYS} 天：1 条" in md
    assert "未经 AI 初筛" in md and "1 条" in md
    assert "尚未临期）：1 条" in md
    assert rs.DIGEST_EMPTY_TEXT not in md
    _clear_opportunities()
    print("[OK] resource digest filters")


def test_resource_digest_empty_state():
    """筛完为空时**如实输出空状态**，不用通用建议填充"""
    from core import resource_scout as rs

    _clear_opportunities()
    md = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                  today=_PINNED_TODAY.date())
    assert rs.DIGEST_EMPTY_TEXT in md, "空库应输出空状态文案"
    assert "本地机会库当前一条都没有" in md
    # 参数越界 / 非法一律回退默认值，不得抛异常
    assert rs._digest_params({"limit": 0, "preopen_days": "x",
                              "min_value_score": 9}).get("limit") == 1
    print("[OK] resource digest empty state")


def test_scout_digest_template_plan_and_approval():
    """新动作类型的计划构造与执行：只产出 1 条任务（显式 task_kind）；
    执行后产生笔记型待审批（type=note）、顶部带缺席页眉；批准后落盘到
    该模板的独立目录。**全程不 mock**：这一格本来就该零网络、零 LLM。"""
    from core import resource_scout as rs

    _clear_opportunities()
    tid = tt.create_template(
        _new_template_name("资源格"), trigger_type="manual",
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid, "新动作类型应能通过 VALID_ACTIONS 校验"
    _reset_template_goal_dir(tid)
    try:
        template = tt.get_template(tid)
        plan = TaskExecutor._build_plan_from_template(
            template, previous_run_at="2026-09-05T08:00:00")
        assert len(plan) == 1, "「资源」格只产出 1 条任务"
        task = plan[0]
        assert task["auto_executable"] is True
        assert task["task_kind"] == tt.ACTION_SCOUT_DIGEST
        assert task["goal_id"] == tt.template_goal_id(tid)
        assert task["previous_run_at"] == "2026-09-05T08:00:00", \
            "缺席页眉的天数基准必须随计划任务传下去"

        # 手动路径不显式传 previous_run_at：应回退到模板自己的 last_run_at
        assert tt.update_last_run(tid, "2026-09-05T08:00:00")

        executor = TaskExecutor()
        gen = executor.execute_template(tid, triggered_by="manual")
        done, approvals = _drive(gen, approve=True)
        assert done["type"] == "done"
        assert done["stats"]["auto"] == 1 and done["stats"]["saved"] == 1
        assert len(approvals) == 1, "「资源」格只产生 1 个审批暂停点"
        approval = approvals[0]["approval"]
        assert approval["type"] == "note", "「资源」格走笔记型审批（落盘走 NoteStore）"
        assert "上一份运行于 2026-09-05" in approval["content"], "产出顶部应有缺席页眉"
        assert rs.DIGEST_EMPTY_TEXT in approval["content"], \
            "库里没有机会时应如实输出空状态"

        note_files = list((_template_goal_dir(tid) / "notes").glob("*.md"))
        assert note_files, "批准后应落盘为该模板独立目录下的笔记"
    finally:
        tt.delete_template(tid)
    _clear_opportunities()
    print("[OK] scout digest template plan and approval")


def test_weekly_template_triggers_once_in_week():
    """weekly（周六 08:00）且 last_run_at 在两周前：本周任意一天检查，
    **恰好触发一次**——当期式引擎每周期至多产出一份，配额由触发点形状提供。

    说明：本用例是**回归护栏**，不是"去重机制已验证"的证据。它成立的原因是
    `_is_due` 返回值仍是布尔（走过一次就把 last_run_at 推到本次），所以它今天
    不可能失败；真正要防的是"有人把当期式改成记账式/重放式"。
    """
    tid = tt.create_template(
        _new_template_name("每周资源"), trigger_type="weekly",
        trigger_config={"weekday": 5, "time": "08:00"},
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid
    assert tt.update_last_run(tid, "2026-09-05T08:00:00"), "两周前的周六 08:00"
    monday = datetime(2026, 9, 14, 0, 0)          # 2026-09-19 是周六
    fires = 0
    for offset in range(7):
        now = monday + timedelta(days=offset, hours=12)
        if tid in [t.id for t in tt.get_due_templates(now)]:
            fires += 1
            # 模拟真实调度路径：执行前先抢占 last_run_at（防重入）
            assert tt.update_last_run(tid, now.isoformat(timespec="seconds"))
    assert fires == 1, f"同一周内至多触发一次，实际 {fires} 次"
    assert tt.delete_template(tid)
    print("[OK] weekly template triggers once in week")


def test_absence_header_uses_preclaim_last_run():
    """缺席页眉：N 取**抢占前**的 last_run_at

    定时路径在真正执行之前就把 last_run_at 抢占成了本次运行时刻（防重入），
    所以执行期从模板上再读一次只会永远得到"0 天前"——这正是
    check_scheduled_templates 把 due 列表里的旧值显式传下去的原因。
    """
    _clear_opportunities()
    tid = tt.create_template(
        _new_template_name("缺席页眉"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={}, auto_approve=0)
    _reset_template_goal_dir(tid)
    goal_id = tt.template_goal_id(tid)
    two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    assert tt.update_last_run(tid, two_weeks_ago)
    try:
        results = TaskExecutor().check_scheduled_templates()
        mine = [r for r in results if r["template_id"] == tid]
        assert len(mine) == 1 and mine[0]["paused"] is True

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT content FROM pending_approvals WHERE goal_id = ?",
                (goal_id,)).fetchall()
        finally:
            conn.close()
        assert rows, "定时触发应产生待审批记录"
        assert "（14 天前）" in rows[0]["content"], \
            "天数应取抢占前的 last_run_at（取抢占后的值只会得到 0 天前）"
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?", (goal_id,))
            conn.commit()
        finally:
            conn.close()
        tt.delete_template(tid)
    _clear_opportunities()
    print("[OK] absence header uses preclaim last run")


def test_absence_header_first_run_on_scheduled_path():
    """首次运行（新建模板的 last_run_at 为 NULL）走**定时路径**：页眉应是「首次运行」

    与上一条用例的唯一差别是**抢占前的值本身为 None**：定时路径把 due 列表里
    那个（合法地等于 None 的）旧值当实参传下来，若实现拿 `is not None` 判"调用方
    有没有传"，就会回退到抢占后的值、算出"0 天前"——生产库里真实发生过这一条。
    上一条用例种的是"14 天前"这个非空值，走的是另一条分支，因此抓不到它。
    """
    _clear_opportunities()
    tid = tt.create_template(
        _new_template_name("首次运行"), trigger_type="daily",
        trigger_config={"time": "00:00"},
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={}, auto_approve=0)
    _reset_template_goal_dir(tid)
    goal_id = tt.template_goal_id(tid)
    assert tt.get_template(tid).last_run_at is None, "新建模板的 last_run_at 应为 NULL"
    try:
        results = TaskExecutor().check_scheduled_templates()
        mine = [r for r in results if r["template_id"] == tid]
        assert len(mine) == 1 and mine[0]["paused"] is True, "首次定时触发应暂停在审批点"

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT content FROM pending_approvals WHERE goal_id = ?",
                (goal_id,)).fetchall()
        finally:
            conn.close()
        assert rows, "定时触发应产生待审批记录"
        content = rows[0]["content"]
        assert "首次运行" in content, \
            "last_run_at 为 NULL 的首次运行必须输出「首次运行」（传了 None 被误判成没传）"
        assert "0 天前" not in content, "首次运行不得出现「0 天前」"
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?", (goal_id,))
            conn.commit()
        finally:
            conn.close()
        tt.delete_template(tid)
    _clear_opportunities()
    print("[OK] absence header first run on scheduled path")


def test_resource_digest_far_preview():
    """尚未临期那一类：按「条数 + 最近几条」展开最近 3 条（标题 + 报名截止日），
    其余继续计数并保留段末"另有 N 条未列出"；不足 3 条时不出现该行"""
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()
    soon = "2026-09-26"          # 今天 + 7 天（按当天算出的临期窗口上界）
    _insert_opportunity("临期内X", deadline="2026-09-22", discovered_days_ago=3, base=_PINNED_TODAY)
    for idx, dl in enumerate(("2026-10-07", "2026-10-08", "2026-10-19",
                              "2026-11-01"), start=1):
        _insert_opportunity(f"尚未临期{idx}", deadline=dl, discovered_days_ago=3, base=_PINNED_TODAY)

    md = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                  today=today)
    assert f"- 报名截止在 {soon} 之后（尚未临期）：4 条，最近 3 条：" in md, \
        "段首应报总数 + 本次展开的条数"
    for title in ("尚未临期1", "尚未临期2", "尚未临期3"):
        assert title in md, f"{title} 应被展开（最近三条）"
    positions = [md.index(t) for t in ("尚未临期1", "尚未临期2", "尚未临期3")]
    assert positions == sorted(positions), "展开顺序应按报名截止日升序"
    assert "尚未临期4" not in md, "第 4 近的那条只计数、不展开"
    assert "另有 1 条未列出" in md
    assert "2026-10-07" in md and "2026-10-19" in md, "每条应带报名截止日"

    # 不足 3 条时：只展开实际条数，不出现"另有 N 条未列出"
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities WHERE title LIKE '尚未临期%'")
        conn.commit()
    finally:
        conn.close()
    _insert_opportunity("仅一条未临期Y", deadline="2026-10-07", discovered_days_ago=3, base=_PINNED_TODAY)
    md2 = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                   today=today)
    assert "尚未临期）：1 条，最近 1 条：" in md2
    assert "另有" not in md2, "没有剩余条目时不应出现「另有 N 条未列出」"
    _clear_opportunities()
    print("[OK] resource digest far preview")


def test_resource_digest_summary_line():
    """「📄 源站原文」那一行：给出"能得到什么"；摘要为空则整行不加（不留空行）；
    内部空白折叠、超长按 _DIGEST_SUMMARY_LIMIT 截断"""
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()
    _insert_opportunity("有摘要临期A", deadline="2026-09-22", discovered_days_ago=3,
                        summary="最高赢 10000 灵豆；500-2000 字技术帖 + 配图即可投稿", base=_PINNED_TODAY)
    _insert_opportunity("无摘要临期B", deadline="2026-09-22", discovered_days_ago=3,
                        summary=None, base=_PINNED_TODAY)
    long_summary = "免费额度：\n   A100 试用\n- 无需信用卡\n" + "摘要很长" * 40
    _insert_opportunity("有摘要无时点C", discovered_days_ago=17, value=0.7, reuse=0.6,
                        summary=long_summary, base=_PINNED_TODAY)
    _insert_opportunity("空白摘要无时点D", discovered_days_ago=17, value=0.7,
                        reuse=0.6, summary="   ", base=_PINNED_TODAY)

    md = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                  today=today)
    assert "📄 源站原文：最高赢 10000 灵豆" in md, "有摘要的条目应带该行"
    assert md.count("📄 源站原文：") == 2, "只有非空摘要的条目才有该行"

    line = [ln for ln in md.splitlines() if "A100" in ln][0]
    assert line.startswith("   - 📄 源站原文："), "该行应是条目的嵌套子项"
    assert "\n" not in line and "  " not in line.split("源站原文：")[1], "内部空白应折叠"
    body = line.split("源站原文：")[1]
    assert body.endswith("…"), "超长摘要应截断并加省略号"
    assert len(body) == rs._DIGEST_SUMMARY_LIMIT + 1, \
        "截断长度应恰为 _DIGEST_SUMMARY_LIMIT（+1 个省略号）"
    _clear_opportunities()
    print("[OK] resource digest summary line")


def test_template_panel_shows_stall():
    """模板停摆可见（P7 批次 1c-①）：存在未处理待审批时，列表行必须写明
    「已暂停触发」并给出条数与"何时才会再产出"；没有待审批时不出现该标记"""
    from ui.controllers.templates_controller import refresh_template_panel

    _clear_opportunities()
    tid = tt.create_template(_new_template_name("停摆可见"), trigger_type="manual",
                             action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid
    goal_id = tt.template_goal_id(tid)
    try:
        md, _dd = refresh_template_panel(tid)
        assert f"#{tid} " in md, "模板应出现在列表里"
        assert "已暂停触发" not in md, "没有待审批时不应出现停摆标记"

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO pending_approvals (task_type, task_title, goal_id, "
                "content, status) VALUES ('note', '资源筛选报告', ?, '草稿', 'pending')",
                (goal_id,))
            conn.commit()
        finally:
            conn.close()

        md2, _dd2 = refresh_template_panel(tid)
        assert "⏸️ 有待审批未处理 → 已暂停触发（1 条）" in md2, \
            "有待审批未处理时必须在列表行里写明停摆原因与条数"
        assert "下一次触发等它自己的触发日" in md2, \
            "应说明何时才会再产出（不能写成「下一次检查自动恢复触发」——那是旧的对拍行为）"
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?", (goal_id,))
            conn.commit()
        finally:
            conn.close()
        tt.delete_template(tid)
    print("[OK] template panel shows stall")


def test_save_template_non_search_action():
    """编辑表单（P7 批次 1c-②）：编辑非 search_and_note 模板时不要求关键词、
    不改写 action_params；新建与编辑搜索类模板的既有行为不变"""
    from ui.controllers.templates_controller import do_save_template

    _clear_opportunities()
    tid = tt.create_template(_new_template_name("编辑保参"), trigger_type="weekly",
                             trigger_config={"weekday": 5, "time": "08:00"},
                             action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid
    try:
        # 编辑态：关键词留空也必须能保存，且 action_params 一个字节都不许变
        res = do_save_template("改名后的资源格", "描述改成新的", "weekly", "08:00", 5,
                               "", False, tid)
        assert isinstance(res, tuple) and len(res) == 12, \
            "应返回 (状态, 列表, 下拉框) + 9 项表单回填值（第 9 项是动作类型，3c-乙 起）"
        assert "已更新" in res[0], res[0]
        t = tt.get_template(tid)
        assert t.name == "改名后的资源格"
        assert t.action_params == "{}", \
            "非搜索类模板保存后 action_params 必须原样保留（不得被改写成 query）"
        assert t.action_type == tt.ACTION_SCOUT_DIGEST, "动作类型不得被改写"
        assert t.trigger_weekday == 5 and t.trigger_time == "08:00"

        # 搜索类模板：编辑路径的既有行为不变（关键词必填、参数写回 query）
        tid2 = tt.create_template(_new_template_name("搜索类"), trigger_type="manual",
                                  action_params={"query": "旧关键词"})
        try:
            res2 = do_save_template("搜索类模板", "", "manual", "09:00", 0,
                                    "AI竞赛", False, tid2)
            assert "已更新" in res2[0], res2[0]
            assert tt.get_template(tid2).action_params == '{"query": "AI竞赛"}'
            # 关键词留空 → 仍然拒绝（老行为对搜索类模板必须保留）
            res3 = do_save_template("搜索类模板", "", "manual", "09:00", 0,
                                    "", False, tid2)
            assert "搜索关键词不能为空" in res3[0]
        finally:
            tt.delete_template(tid2)

        # 新建路径：仍然要求关键词
        res4 = do_save_template("没有关键词的新模板", "", "manual", "09:00", 0,
                                "", False, None)
        assert "搜索关键词不能为空" in res4[0]
    finally:
        tt.delete_template(tid)
    print("[OK] save template non-search action")


def test_template_notes_panel_readback():
    """本模板产物可回读（P7 批次 1c-③）：列出该虚拟目标（template_{id}）名下的
    笔记；选中后展示**全文**（不做预览截断）；未选中 / 无产物 / 不存在都有友好提示"""
    from ui.controllers.templates_controller import (
        do_load_template_notes, do_view_template_note)

    _clear_opportunities()
    tid = tt.create_template(_new_template_name("产物回读"), trigger_type="manual",
                             action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid
    goal_id = tt.template_goal_id(tid)
    _reset_template_goal_dir(tid)
    note_id = None
    try:
        assert "选择模板后" in do_load_template_notes(None)[0]
        md0, dd0 = do_load_template_notes(tid)
        assert "暂无产物" in md0 and f"`{goal_id}`" in md0

        long_body = "## 本期「资源」\n\n" + "这是很长的一行正文。" * 200
        note_id = NoteStore.add_note(goal_id, "资源筛选报告（回读测试）", long_body)
        assert note_id and note_id > 0, "产物应写入 notes 表"

        md1, dd1 = do_load_template_notes(tid)
        assert "共 1 篇产物" in md1, md1
        assert "资源筛选报告（回读测试）" in md1
        assert any("资源筛选报告" in str(c) for c in dd1.get("choices", [])), \
            "产物下拉框应含该笔记"

        view = do_view_template_note(note_id)
        assert f"笔记 #{note_id}" in view
        assert f"目标：{goal_id}" in view
        assert long_body in view, "正文必须完整展示（不做预览截断）"

        assert "选择一条产物后" in do_view_template_note(None)
        assert "不存在" in do_view_template_note(99999999)
    finally:
        if note_id:
            NoteStore.delete_note(note_id, async_rag=False)
        tt.delete_template(tid)
        _reset_template_goal_dir(tid)
    print("[OK] template notes panel readback")


def test_trigger_records_period_immediately():
    """触发即记账（P7 批次 2）：**决定执行就消费当期**，不等 done

    生产库实测的缺口：`auto_approve=0` 的模板在审批暂停点把生成器交给界面；用户从
    「待审批」页处理时走的是纯 DB 路径（`approve_pending_from_db` /
    `reject_pending_from_db` 都不推进生成器），生成器被永久遗弃 ⇒ 永远到不了 done ⇒
    旧实现那次手动运行**不记账**。于是只要模板处于"当期已到期"，处理掉 pending 之后
    下一周期就会再补一条同名报告，可以无限对拍（生产库里连出两条同名报告就是这个）。

    本用例模拟"推进到审批暂停点就把生成器丢掉"，断言三件事：
    1) `last_run_at` 已不再是两周前那个值（触发即记账）；
    2) 在**同一个注入时刻**上，它已不再到期（`get_due_templates`）；
    3) 拒绝那条 pending 后，`check_scheduled_templates()` 不再为它产出新 pending。

    **已知残留，不把断言写成过度承诺**：
    - 断言 3 的"牙齿"依赖运行时刻落在本期触发点之后（周六 08:00 起）。在周一~周五跑，
      `_is_due` 本来就恒为 False（`now < target`），它只能证明"没有多余产出"，不能证明
      "记账拦住了它"——真正的主力断言是 1 与 2（断言 2 借注入时刻固定在这一期的
      触发点之后，因此与真实时钟无关）。
    - 手动运行发生在"本期触发点**之前**"时（例如周一手动跑一个周六的 weekly），本期的
      自动触发仍会发生——那是一次触发消费一期（周一那次属上一期），不是对拍。
      本用例断言的是"任何一次触发都当场记账 + 已到期的不再补第二条"。
    """
    _clear_opportunities()
    tid = tt.create_template(
        _new_template_name("触发即记账"), trigger_type="weekly",
        trigger_config={"weekday": 5, "time": "08:00"},
        action_type=tt.ACTION_SCOUT_DIGEST, action_params={}, auto_approve=0)
    assert tid
    _reset_template_goal_dir(tid)
    goal_id = tt.template_goal_id(tid)
    two_weeks_ago = "2026-09-05T08:00:00"      # 2026-09-19 是周六，两周前同为周六
    assert tt.update_last_run(tid, two_weeks_ago)
    # 前提自检：模板确实处于"当期已到期"，否则本用例什么也没证明
    probe = datetime(2026, 9, 19, 12, 0)
    assert tid in [t.id for t in tt.get_due_templates(probe)], \
        "用例前提：模板应处于「当期已到期」"
    try:
        executor = TaskExecutor()
        gen = executor.execute_template(tid, triggered_by="manual")
        # 照 _consume_template_gen 的写法内联：推到审批暂停点就停（_drive 会一路跑到
        # done，模拟不出"生成器被遗弃"这个关键条件）
        approval_event = None
        while True:
            event = next(gen)
            if event.get("type") == "approval":
                approval_event = event
                break
            assert event.get("type") != "done", "应停在审批暂停点，而不是跑到 done"
        gen.close()                            # 显式遗弃：再无人推进它
        assert approval_event is not None
        # 页眉仍取"记账前"的基准（计划在记账之前建成、读的是模板对象快照）
        assert "上一份运行于 2026-09-05" in approval_event["approval"]["content"], \
            "页眉基准不应被触发点记账改动"

        # 1) 触发即记账
        after = tt.get_template(tid).last_run_at
        assert after and after != two_weeks_ago, \
            "决定执行就应记账（旧实现要等 done，而被遗弃的生成器永远到不了 done）"
        # 2) 同一注入时刻上不再到期
        assert tid not in [t.id for t in tt.get_due_templates(probe)], \
            "当期已被本次占用，同一时刻不应再判为到期"

        # 3) 拒绝那条 pending（走 DB 路径，与界面「待审批」页处理等义）后：
        #    不再补第二条。不先拒绝的话，堆积控制会替我们挡住，断言就没有意义了。
        pid = approval_event["approval"].get("pending_id")
        assert pid and ApprovalStore.update_status(pid, "rejected")
        results = executor.check_scheduled_templates()
        assert [r for r in results if r["template_id"] == tid] == [], \
            "已记账的当期不应再补一条（防对拍）"
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?", (goal_id,))
            conn.commit()
        finally:
            conn.close()
        tt.delete_template(tid)
    _clear_opportunities()
    print("[OK] trigger records period immediately")


def test_digest_bucket_classification():
    """`digest_bucket` 是桶判定的唯一权威（P7 批次 3）：五个桶各自的边界条件"""
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()
    rows = {
        "过期": _insert_opportunity("桶过期", deadline="2026-09-15",
                                    discovered_days_ago=3, base=_PINNED_TODAY),
        "今天": _insert_opportunity("桶今天", deadline="2026-09-19",
                                    discovered_days_ago=3, base=_PINNED_TODAY),
        "临期": _insert_opportunity("桶临期", deadline="2026-09-22",
                                    discovered_days_ago=3, base=_PINNED_TODAY),
        "远": _insert_opportunity("桶远", deadline="2026-10-19",
                                  discovered_days_ago=3, base=_PINNED_TODAY),
        "无时点": _insert_opportunity("桶无时点", discovered_days_ago=17,
                                      value=0.7, reuse=0.6, base=_PINNED_TODAY),
        "刚发现": _insert_opportunity("桶刚发现", discovered_days_ago=2,
                                      value=0.7, reuse=0.6, base=_PINNED_TODAY),
        "低价值": _insert_opportunity("桶低价值", discovered_days_ago=17,
                                      value=0.05, reuse=0.6, base=_PINNED_TODAY),
        "未初筛": _insert_opportunity("桶未初筛", discovered_days_ago=17, base=_PINNED_TODAY),
        "资格不符": _insert_opportunity("桶资格不符", discovered_days_ago=17,
                                        value=0.7, reuse=0.6,
                                        eligibility="likely_no", base=_PINNED_TODAY),
        "已忽略": _insert_opportunity("桶已忽略", deadline="2026-09-15",
                                      discovered_days_ago=3, status="ignored", base=_PINNED_TODAY),
    }
    by_id = {r["id"]: r for r in rs.ResourceScout.list_opportunities(limit=500)}
    bucket = lambda key: rs.digest_bucket(by_id[rows[key]], today=today)  # noqa: E731
    assert bucket("过期") == "expired"
    assert bucket("今天") == "soon", "今天到期属于临期，不是已过"
    assert bucket("临期") == "soon"
    assert bucket("远") == "far"
    assert bucket("无时点") == "dormant"
    for key in ("刚发现", "低价值", "未初筛", "资格不符", "已忽略"):
        assert bucket(key) == "none", f"{key} 不应进入任何播报桶"
    # 参数可覆盖窗口：preopen_days=1 时"今天+2"就掉出临期
    assert rs.digest_bucket(by_id[rows["临期"]], today=today,
                            params={"preopen_days": 1}) == "far"
    _clear_opportunities()
    print("[OK] digest bucket classification")


def test_resource_digest_three_sections():
    """三段拆分（P7 批次 3）：已过 / 临期 / 无时点 各自成段，尚未临期只进「未列入」"""
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()
    _insert_opportunity("切段已过", deadline="2026-09-10", discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("切段临期", deadline="2026-09-21", discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("切段很远", deadline="2026-11-30", discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("切段无时点", discovered_days_ago=20, value=0.8, reuse=0.7, base=_PINNED_TODAY)

    md = rs.build_resource_digest(rs.ResourceScout.list_opportunities(limit=500),
                                  today=today)
    head_expired = md.index("**⏰ 报名截止日已过")
    head_soon = md.index("**⏳ 临期：")
    head_dormant = md.index("**🧊 无时点、")
    assert head_expired < head_soon < head_dormant, "三段顺序应为 已过 → 临期 → 无时点"
    assert head_expired < md.index("切段已过") < head_soon
    assert head_soon < md.index("切段临期") < head_dormant
    assert md.index("切段无时点") > head_dormant
    assert "切段很远" not in md[:head_expired], "尚未临期的不进播报段"
    tail = md[md.index("**未列入（如实计数，不是判定）**"):]
    assert "切段很远" in tail, "尚未临期的只作为「未列入」段的最近条目出现"
    # 已过段的措辞照 P6-2：只报日期事实，不写"已错过"
    assert "已错过" not in md
    _clear_opportunities()
    print("[OK] resource digest three sections")


def test_resource_digest_watermark():
    """水位线（P7 批次 3）：只播报"新进入某个状态"的条目

    口径（build_resource_digest docstring 的验收口径原文）：
      不变量：同一（机会键 ＋ 状态桶）最多被播报一次。
      可判定形式：把相邻两期报告的**播报段条目**抽成 (键, 桶) 集合，交集必须为空。
    本用例覆盖：重复不播、桶变了重新播、far/none 既不播也不参与比较、无变化输出
    DIGEST_NO_CHANGE_TEXT 且仍带「未列入」段。
    """
    from core import resource_scout as rs

    _clear_opportunities()
    today = _PINNED_TODAY.date()
    soon_id = _insert_opportunity("水位临期", deadline="2026-09-21",
                                  discovered_days_ago=3, base=_PINNED_TODAY)
    dorm_id = _insert_opportunity("水位无时点", discovered_days_ago=20,
                                  value=0.8, reuse=0.7, base=_PINNED_TODAY)
    far_id = _insert_opportunity("水位很远", deadline="2026-11-30",
                                 discovered_days_ago=3, base=_PINNED_TODAY)
    _insert_opportunity("水位刚发现", discovered_days_ago=1, value=0.9, reuse=0.9, base=_PINNED_TODAY)

    rows = rs.ResourceScout.list_opportunities(limit=500)
    p1 = rs.digest_notify_items(rows, today=today)
    md1 = rs.build_resource_digest(rows, today=today)
    assert set(p1) == {(soon_id, "soon"), (dorm_id, "dormant")}, \
        f"首期应播报两条符合条件的变化，实际 {p1}"
    # far / none 的行不进**播报段**（它们只允许出现在「未列入」段的最近 3 条预览里）
    head1, _, tail1 = md1.partition("**未列入（如实计数，不是判定）**")
    assert far_id not in {k for k, _ in p1}
    assert "水位很远" not in head1, "尚未临期的不应进播报段"
    assert "水位很远" in tail1, "但它应作为「未列入」段的最近条目被展开（本批未动该段）"
    assert "本期没有新变化" not in md1

    # 第二期：用第一期实际播报的条目当水位线 → 一条都不再播，输出"没有新变化"
    notified = dict(p1)
    p2 = rs.digest_notify_items(rows, today=today, notified=notified)
    md2 = rs.build_resource_digest(rows, today=today, notified=notified)
    assert p2 == [], f"同一（键＋桶）不应二次播报，实际 {p2}"
    assert set(map(tuple, p1)) & set(map(tuple, p2)) == set(), \
        "相邻两期播报集合的交集必须为空（验收口径的可判定形式）"
    assert rs.DIGEST_NO_CHANGE_TEXT in md2
    assert rs.DIGEST_EMPTY_TEXT not in md2, "有符合条件的条目时不该说「没有符合条件的」"
    assert "**未列入（如实计数，不是判定）**" in md2, "无变化时仍保留未列入段"
    assert "水位临期" not in md2 and "水位无时点" not in md2

    # 桶变了 → 重新播报（真实场景：日子往前走，临期变已过）
    later = date(2026, 9, 25)
    p3 = rs.digest_notify_items(rows, today=later, notified=notified)
    assert (soon_id, "expired") in p3, "桶从 soon 变 expired 应重新播报"
    md3 = rs.build_resource_digest(rows, today=later, notified=notified)
    assert set(map(tuple, p2)) & set(map(tuple, p3)) == set()
    assert md3.index("**⏰ 报名截止日已过") < md3.index("水位临期")
    assert "水位无时点" not in md3, "没变桶的那条不该跟着重复播"

    # far / none 既不播报、也不受 notified 影响
    polluted = dict(notified)
    polluted[far_id] = "soon"          # 人为写一个不一致的水位线值
    p4 = rs.digest_notify_items(rows, today=today, notified=polluted)
    assert p4 == [], "far / none 的行不参与变化判定"
    head4 = rs.build_resource_digest(rows, today=today,
                                     notified=polluted).partition(
        "**未列入（如实计数，不是判定）**")[0]
    assert "水位很远" not in head4, "far 行不因水位线里有个不一致的值就冒进播报段"

    # 盘子里一条都没有 → 用另一句空状态（两句不合并）
    empty_md = rs.build_resource_digest([], today=today)
    assert rs.DIGEST_EMPTY_TEXT in empty_md
    assert rs.DIGEST_NO_CHANGE_TEXT not in empty_md
    _clear_opportunities()
    print("[OK] resource digest watermark")


def test_scout_digest_watermark_end_to_end():
    """水位线端到端（P7 批次 3，"重复＝0"的机器化版本）：生成 → 记账 → 再生成

    第一期播报的条目，第二期**必须不再出现**（这是验收口径"相邻两期播报段条目的
    (键,桶) 集合交集为空"的机器化形式）。同时钉住两件容易静默失效的事：
      · `list_opportunities` 的行 dict 里必须同时有 `id` 与 `notified_bucket`
        （它走 `SELECT *`，新增列自动可见；一旦有人改成显式列名查询却漏列，
        水位线会**静默失效**——键全变空串、或永远读到 None）；
      · 记账真的写进了 `notified_bucket` 列。
    """
    from core import resource_scout as rs

    _clear_opportunities()
    tid = tt.create_template(_new_template_name("水位端到端"), trigger_type="manual",
                             action_type=tt.ACTION_SCOUT_DIGEST, action_params={})
    assert tid
    try:
        # 本用例的**判定基准是真钟**（它走 handle_scout_digest_task，该函数不接受 today
        # 参数），所以截止日也必须相对真钟写：旧写法把 2026-09-22 钉死却期望桶是
        # 'soon'，真实日期一过 09-22 桶就变 'expired'，断言自己变红——与"播种/判定分属
        # 两个钟"是同一族问题，只是这次的假钟换成了截止日。
        today = date.today()
        _insert_opportunity("端到端临期",
                            deadline=(today + timedelta(days=3)).isoformat(),
                            discovered_days_ago=3)
        _insert_opportunity("端到端无时点", discovered_days_ago=20, value=0.8,
                            reuse=0.7)
        _insert_opportunity("端到端很远",
                            deadline=(today + timedelta(days=60)).isoformat(),
                            discovered_days_ago=3)

        rows = rs.ResourceScout.list_opportunities(limit=500)
        assert rows, "应能读到刚插入的机会"
        for r in rows:
            assert "id" in r and r["id"] is not None, "行里必须有 id（水位线的键）"
            assert "notified_bucket" in r, \
                "行里必须有 notified_bucket（列缺失会让水位线静默失效）"

        task = {"task_id": None, "goal_id": tt.template_goal_id(tid),
                "goal_title": "任务模板", "task_title": "资源筛选报告（水位端到端）",
                "task_kind": tt.ACTION_SCOUT_DIGEST, "digest_params": {},
                "previous_run_at": None}
        executor = TaskExecutor()

        out1 = executor.handle_scout_digest_task(task)
        assert out1.get("ok") is True, out1
        p1 = out1["approval"]["content"]
        assert "端到端临期" in p1 and "端到端无时点" in p1
        assert "端到端很远" not in p1.partition("**未列入（如实计数，不是判定）**")[0]

        # 记账：被播报的那两条的 notified_bucket 已落库
        conn = get_connection()
        try:
            saved = {r["title"]: r["notified_bucket"] for r in conn.execute(
                "SELECT title, notified_bucket FROM opportunities")}
        finally:
            conn.close()
        assert saved["端到端临期"] == "soon", saved
        assert saved["端到端无时点"] == "dormant", saved
        assert saved["端到端很远"] is None, "far 的条目不播报、也就不该落水位线"

        # 第二期：走同一条生成路径，同样两条不应再出现
        out2 = executor.handle_scout_digest_task(task)
        assert out2.get("ok") is True
        p2 = out2["approval"]["content"]
        assert "端到端临期" not in p2, "第一期播报过的条目第二期不应再出现"
        assert "端到端无时点" not in p2
        assert rs.DIGEST_NO_CHANGE_TEXT in p2, "没有新变化时应输出那句空状态"
        assert "**未列入（如实计数，不是判定）**" in p2, "无变化时仍保留未列入段"
    finally:
        tt.delete_template(tid)
    _clear_opportunities()
    print("[OK] scout digest watermark end to end")


def test_mark_digest_notified_key_contract():
    """水位线键契约（P7 批次 4-①）：只认 opportunities.id（int），非 int 键必须**出声**

    背景：`mark_digest_notified` 的 SQL 是 `WHERE id = ?`，而 `_digest_key` 在缺 `id`
    时会退回 `source_url` 字符串。旧实现对这种键**完全静默**（rowcount 照样大于 0，
    唯一的 print 只在抛异常时发生）⇒ 那几条每期重复播报、水位线永远写不进去却无痕迹。

    四个情形，**每条断言都标了它在修改前的实现上会不会通过**：
      ① 全是 int 键 → 返回值＝写入行数、库里有值
         【旧实现也绿】⇒ 只起防回归作用，无牙齿。
      ② 掺一个 URL 字符串 → 返回值只数写成功的、那条不写
         【旧实现也绿：`WHERE id='<url>'` 匹配 0 行，rowcount 照样只数写成的】
         ；但"**打印了一行**"【旧实现红】⇒ 本用例唯一有牙齿的断言，
         也正是要抓"安静"这件事。
      ③ 全是非 int 键 → 返回 0 且没有任何行被改
         【旧实现也绿】⇒ 防回归；配套的"打印一行"【旧实现红】有牙齿。
      ④ 对账：传一个不存在的 int id → 返回值只数真正写成的，并打印"对账不符"
         【旧实现红：既没有对账、也不会打印】⇒ 有牙齿。
    """
    import contextlib
    import io

    from core import resource_scout as rs

    _clear_opportunities()
    oid_a = _insert_opportunity("键契约A", deadline="2026-09-22", discovered_days_ago=3)
    oid_b = _insert_opportunity("键契约B", discovered_days_ago=20, value=0.8, reuse=0.7)

    def buckets():
        conn = get_connection()
        try:
            return {r["id"]: r["notified_bucket"] for r in conn.execute(
                "SELECT id, notified_bucket FROM opportunities")}
        finally:
            conn.close()

    def reset_bucket():
        conn = get_connection()
        try:
            conn.execute("UPDATE opportunities SET notified_bucket = NULL")
            conn.commit()
        finally:
            conn.close()

    try:
        # ① 全是 int 键
        n = rs.ResourceScout.mark_digest_notified([(oid_a, "soon"), (oid_b, "dormant")])
        assert n == 2, f"应写入 2 行，实际 {n}"
        saved = buckets()
        assert saved[oid_a] == "soon" and saved[oid_b] == "dormant", saved

        # ② 掺一个非 int 键（URL 字符串）
        reset_bucket()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n2 = rs.ResourceScout.mark_digest_notified(
                [(oid_a, "soon"), ("https://example.invalid/no-id", "dormant"),
                 (oid_b, "dormant")])
        out = buf.getvalue()
        assert n2 == 2, f"只应写入两个 int 键那两条，实际 {n2}"
        saved = buckets()
        assert saved[oid_a] == "soon" and saved[oid_b] == "dormant", saved
        assert "跳过 1 个非 int 键" in out, f"非 int 键必须打印一行，实际 stdout={out!r}"
        assert "https://example.invalid/no-id" in out, "打印里应带上被跳过的键样例"
        assert "下一期仍会播报" in out, "打印里应说明后果"

        # ③ 全是非 int 键
        reset_bucket()
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            n3 = rs.ResourceScout.mark_digest_notified(
                [("https://a.invalid/x", "soon"), ("https://b.invalid/y", "dormant")])
        assert n3 == 0, f"全是非 int 键时应返回 0，实际 {n3}"
        assert all(v is None for v in buckets().values()), \
            f"非 int 键不该改动任何行：{buckets()}"
        assert "跳过 2 个非 int 键" in buf3.getvalue()

        # ④ 对账：传入一个不存在的 int id（模拟"行在读取之后被删掉"）
        reset_bucket()
        buf4 = io.StringIO()
        with contextlib.redirect_stdout(buf4):
            n4 = rs.ResourceScout.mark_digest_notified([(99999999, "soon"),
                                                        (oid_a, "soon")])
        assert n4 == 1, f"只应写入真实存在的那一行，实际 {n4}"
        assert "对账不符" in buf4.getvalue(), \
            f"写入行数与请求条数不等时必须打印一行，实际 stdout={buf4.getvalue()!r}"
        assert buckets()[oid_a] == "soon", buckets()
    finally:
        _clear_opportunities()
    print("[OK] mark digest notified key contract")


def test_cleanup():
    """清理测试产物（临时数据库目录 + 虚拟目标目录下测试文件）"""
    cleanup_test_db()
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== task templates test ===\n")
    setup()
    test_independent_database_and_table()
    test_template_crud()
    test_due_daily()
    test_due_weekly()
    test_manual_never_due()
    test_update_last_run_unknown_id()
    test_execute_template_edge_cases()
    test_execute_template_manual_approval()
    test_execute_template_reject_does_not_write()
    test_execute_template_auto_approve()
    test_check_scheduled_templates_auto()
    test_check_scheduled_templates_manual_pending()
    test_scheduled_skip_when_pending_backlog()
    test_ui_callback_contract()
    test_resource_digest_filters()
    test_resource_digest_empty_state()
    test_resource_digest_far_preview()
    test_resource_digest_summary_line()
    test_digest_bucket_classification()
    test_resource_digest_three_sections()
    test_resource_digest_watermark()
    test_scout_digest_watermark_end_to_end()
    test_mark_digest_notified_key_contract()
    test_scout_digest_template_plan_and_approval()
    test_weekly_template_triggers_once_in_week()
    test_absence_header_uses_preclaim_last_run()
    test_absence_header_first_run_on_scheduled_path()
    test_template_panel_shows_stall()
    test_save_template_non_search_action()
    test_template_notes_panel_readback()
    test_trigger_records_period_immediately()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
