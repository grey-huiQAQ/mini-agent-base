# -*- coding: utf-8 -*-
"""
审批持久化测试（TaskExecutor V0.2 新增：pending_approvals 表 + ApprovalStore）

覆盖：
- 记录写入：add_pending 插入 pending 记录，字段完整，detail_json 无损回读；
- 状态更新：update_status 置 approved/rejected 并记录 resolved_at；
- 重启后读取：ApprovalStore 无内存状态，新实例/新读取可还原同一批记录；
- 执行器集成：execute_plan 暂停审批前写入 pending，确认/拒绝后自动更新状态；
- 持久化处理：内存生成器丢失后，从记录 detail 重新执行 save_approval 落盘；
- UI 回调：approve_pending_from_db / reject_pending_from_db（确认重新执行/拒绝仅更新状态）；
- 异常安全：非法输入、存储故障不中断执行器流程。

所有测试离线运行：mock web_search / chat_sync / NoteStore，不依赖网络与 LLM。
"""
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection
from memory.approval_store import ApprovalStore
from agents import task_executor as te_module
from agents.task_executor import TaskExecutor

# 本测试使用的目标 ID 前缀（避免污染共享开发库）
_GOAL_PREFIX = "goal_appr_" + uuid.uuid4().hex[:6]
_used_goal_ids = []
_created_pending_ids = []


def setup():
    """初始化数据库（触发 pending_approvals 表创建）"""
    init_db()


def _new_goal_id() -> str:
    gid = f"{_GOAL_PREFIX}_{len(_used_goal_ids)}_{int(time.time())}"
    _used_goal_ids.append(gid)
    return gid


def _track_pending(pid):
    if pid and pid > 0:
        _created_pending_ids.append(pid)
    return pid


def _make_task(gid, title, auto=True, desc=""):
    """构造与 DailyPlannerAgent 输出结构一致的计划任务"""
    return {
        "task_id": 1, "goal_id": gid, "task_title": title,
        "goal_title": "测试目标", "estimated_hours": 2.0,
        "reason": "测试", "auto_executable": auto, "description": desc,
    }


def _make_approval(gid, ntype="search", title="审批测试任务"):
    """构造一条审批字典（与 handle_search_task / handle_note_task 输出结构一致）"""
    if ntype == "search":
        return {
            "task_id": 1, "task_title": title, "goal_id": gid,
            "goal_title": "测试目标", "type": "search",
            "content": "## 🔍 资料搜索结果：审批测试任务\n\n1. 结果一\n   摘要内容",
            "suggested_path": str(Path("data") / "goals" / gid / "research_notes"),
            "filename": "审批测试任务.md",
        }
    return {
        "task_id": 2, "task_title": title, "goal_id": gid,
        "goal_title": "测试目标", "type": "note",
        "content": "## 📝 笔记草稿:审批测试任务\n\n笔记正文",
        "suggested_path": str(Path("data") / "goals" / gid / "notes"),
        "filename": "审批测试任务.md",
    }


def _patch_web_search(fake):
    """临时替换 local_tools.web_search，返回还原函数"""
    original = te_module.local_tools.web_search
    te_module.local_tools.web_search = fake

    def restore():
        te_module.local_tools.web_search = original
    return restore


def _cleanup():
    """清理本测试产生的 pending 记录与目标专属目录"""
    conn = get_connection()
    try:
        if _created_pending_ids:
            placeholders = ",".join("?" * len(_created_pending_ids))
            conn.execute(
                f"DELETE FROM pending_approvals WHERE id IN ({placeholders})",
                _created_pending_ids,
            )
        for gid in _used_goal_ids:
            conn.execute("DELETE FROM notes WHERE goal_id = ?", (gid,))
        conn.commit()
    finally:
        conn.close()
    for gid in _used_goal_ids:
        goal_root = Path("data") / "goals" / gid
        if goal_root.exists():
            shutil.rmtree(goal_root, ignore_errors=True)
    _used_goal_ids.clear()
    _created_pending_ids.clear()


# ---------- 1. 记录写入与回读 ----------

def test_add_pending_writes_row():
    """add_pending：插入 pending 记录，字段完整，detail_json 无损回读"""
    gid = _new_goal_id()
    approval = _make_approval(gid, "search", "写入测试任务")
    pid = _track_pending(ApprovalStore.add_pending(approval))
    assert pid > 0

    record = ApprovalStore.get_pending(pid)
    assert record is not None
    assert record["status"] == "pending"
    assert record["task_type"] == "search"
    assert record["task_title"] == "写入测试任务"
    assert record["goal_id"] == gid
    assert record["content"] == approval["content"]
    assert record["suggested_path"].replace("\\", "/").endswith(
        f"data/goals/{gid}/research_notes")
    # detail_json 无损回读（确认后重放 save_approval 的依据）
    assert record["detail"]["type"] == "search"
    assert record["detail"]["content"] == approval["content"]
    assert record["detail"]["filename"] == "审批测试任务.md"
    assert record["resolved_at"] is None
    print(f"[OK] add pending writes row (id={pid})")


def test_update_status_and_resolved_at():
    """update_status：置 approved/rejected 并记录 resolved_at；非法输入拒绝"""
    gid = _new_goal_id()
    pid1 = _track_pending(ApprovalStore.add_pending(_make_approval(gid, "note")))
    assert ApprovalStore.update_status(pid1, "approved") is True
    rec1 = ApprovalStore.get_pending(pid1)
    assert rec1["status"] == "approved"
    assert rec1["resolved_at"], "终态应记录 resolved_at"

    pid2 = _track_pending(ApprovalStore.add_pending(_make_approval(gid, "search")))
    assert ApprovalStore.update_status(pid2, "rejected") is True
    assert ApprovalStore.get_pending(pid2)["status"] == "rejected"

    # 非法状态 / 不存在的记录 → False
    assert ApprovalStore.update_status(pid1, "invalid_status") is False
    assert ApprovalStore.update_status(999999, "approved") is False
    print("[OK] update status and resolved at")


def test_list_pending_after_restart():
    """重启模拟：ApprovalStore 无内存状态，重新读取可还原同一批记录"""
    gid = _new_goal_id()
    pid = _track_pending(ApprovalStore.add_pending(_make_approval(gid, "search")))
    pid2 = _track_pending(ApprovalStore.add_pending(_make_approval(gid, "note")))
    ApprovalStore.update_status(pid2, "approved")

    # 重新读取（等价于重启后从 DB 还原）：pending 只含未处理记录
    rows = ApprovalStore.list_pending(status="pending")
    assert any(r["id"] == pid for r in rows), "pending 记录应可还原"
    assert not any(r["id"] == pid2 for r in rows), "已处理记录不应出现在 pending 列表"
    # 按状态过滤与全量
    assert any(r["id"] == pid2 for r in ApprovalStore.list_pending("approved"))
    assert ApprovalStore.count_pending() >= 1
    assert ApprovalStore.list_pending(None)  # 全量不抛异常
    print("[OK] list pending after restart")


# ---------- 2. 执行器集成（内存流持久化 + 自动更新状态） ----------

def test_execute_plan_persists_and_resolves():
    """执行器：yield approval 前写入 pending；确认/拒绝后自动更新状态"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "1. Attention 论文\n   摘要")
    try:
        gen = executor.execute_plan([_make_task(gid, "搜索注意力机制资料")])
        next(gen), next(gen)
        ev = next(gen)
        assert ev["type"] == "approval"
        pid = ev["approval"].get("pending_id")
        assert pid is not None, "审批事件应携带持久化记录 ID"
        _track_pending(pid)

        # 暂停时记录为 pending
        record = ApprovalStore.get_pending(pid)
        assert record["status"] == "pending"
        assert record["detail"]["type"] == "search"

        # 确认 → 落盘 + 状态置 approved
        ev_saved = gen.send(True)
        assert ev_saved["type"] == "result" and "✅" in ev_saved["message"]
        assert ApprovalStore.get_pending(pid)["status"] == "approved"
        while next(gen)["type"] != "done":
            pass
    finally:
        restore()
    print("[OK] execute plan persists and resolves approved")


def test_execute_plan_reject_updates_status():
    """执行器：拒绝后持久化记录状态置 rejected，不产生任何写盘"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "1. 某结果\n   摘要")
    try:
        gen = executor.execute_plan([_make_task(gid, "检索强化学习教程")])
        next(gen), next(gen)
        ev = next(gen)
        pid = ev["approval"]["pending_id"]
        _track_pending(pid)
        assert ApprovalStore.get_pending(pid)["status"] == "pending"

        ev_rejected = gen.send(False)
        assert "已拒绝并丢弃" in ev_rejected["message"]
        assert ApprovalStore.get_pending(pid)["status"] == "rejected"
    finally:
        restore()
    assert not (Path("data") / "goals" / gid / "research_notes").exists(), \
        "拒绝后不应写入任何文件"
    print("[OK] execute plan reject updates status")


# ---------- 3. 持久化处理（重启后重新执行 / 拒绝） ----------

def test_approve_from_persisted_after_restart():
    """重启模拟：内存生成器丢失后，从持久化 detail 重新执行并落盘"""
    gid = _new_goal_id()
    executor1 = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "1. 重启恢复结果\n   内容")
    try:
        gen = executor1.execute_plan([_make_task(gid, "搜索重启恢复资料")])
        next(gen), next(gen)
        ev = next(gen)
        pid = _track_pending(ev["approval"]["pending_id"])
    finally:
        restore()

    # 模拟重启：丢弃内存生成器，用全新实例从持久化记录重新执行
    record = ApprovalStore.get_pending(pid)
    assert record["status"] == "pending"
    executor2 = TaskExecutor()  # 重启后的新实例
    outcome = executor2.save_approval(record["detail"])
    assert outcome["ok"] is True, outcome["message"]
    ApprovalStore.update_status(pid, "approved")
    assert ApprovalStore.get_pending(pid)["status"] == "approved"

    research_dir = Path("data") / "goals" / gid / "research_notes"
    files = list(research_dir.glob("*.md"))
    assert files, "重启后重新执行应写入文件"
    assert "重启恢复结果" in files[0].read_text(encoding="utf-8")
    print("[OK] approve from persisted after restart")


# ---------- 4. UI 回调（从待审批列表确认/拒绝） ----------

def test_ui_approve_and_reject_from_list():
    """UI 回调：从待审批列表确认（重新执行+落盘）/ 拒绝（仅更新状态）"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 待审批测试")
        return

    gid = _new_goal_id()
    approval = _make_approval(gid, "note", "UI 待审批任务")
    pid = _track_pending(ApprovalStore.add_pending(approval))

    # 确认：调用 task_executor.save_approval 重新执行（mock）→ 状态 approved。
    # 解冻：app.py 与 agents.task_executor 共用同一 task_executor 单例实例，
    # 直接 patch 该实例的 save_approval 方法，不再依赖 app 命名空间的绑定。
    with patch.object(te_module.task_executor, "save_approval",
                      return_value={"ok": True, "message": "💾 已保存笔记（mock）"}) as mock_save:
        status, panel, dd, display = app_module.approve_pending_from_db(pid)
        assert "已处理" in status and "已保存笔记" in status, status
        assert mock_save.call_count == 1
        assert ApprovalStore.get_pending(pid)["status"] == "approved"
        # 已处理记录应从待审批列表消失（可能仍有其他 pending 记录）
        assert f"#{pid}" not in panel, "处理完成后列表不应再显示该记录"
        assert pid not in [c[1] for c in dd["choices"]], "下拉框不应再包含该记录"
        assert dd["value"] is None and display == "选择待审批记录后在此显示内容"

    # 已处理记录再次处理 → 友好提示，不重复执行
    with patch.object(te_module.task_executor, "save_approval") as mock_save:
        status2, *_ = app_module.approve_pending_from_db(pid)
        assert "已被处理" in status2, status2
        assert mock_save.call_count == 0

    # 拒绝：仅更新状态，不调用 save_approval
    pid2 = _track_pending(ApprovalStore.add_pending(approval))
    with patch.object(te_module.task_executor, "save_approval") as mock_save:
        status3, *_ = app_module.reject_pending_from_db(pid2)
        assert "已拒绝" in status3, status3
        assert ApprovalStore.get_pending(pid2)["status"] == "rejected"
        assert mock_save.call_count == 0

    # 未选择记录 → 友好提示
    status4, *_ = app_module.approve_pending_from_db(None)
    assert "请先选择" in status4, status4
    print("[OK] ui approve and reject from list")


# ---------- 5. 异常安全 ----------

def test_approval_store_exception_safety():
    """ApprovalStore 异常安全：非法输入返回 -1/False，不抛异常"""
    assert ApprovalStore.add_pending(None) == -1
    assert ApprovalStore.add_pending({}) == -1
    assert ApprovalStore.add_pending({"type": "note", "content": ""}) == -1
    assert ApprovalStore.update_status(999999, "approved") is False
    assert ApprovalStore.update_status(1, "bad_status") is False
    print("[OK] approval store exception safety")


class _BoomStore:
    """add_pending 抛异常的 mock 存储：验证执行器流程不中断"""

    def add_pending(self, approval):
        raise RuntimeError("存储故障")

    def update_status(self, pid, status):
        return True


def test_executor_survives_approval_store_failure():
    """异常安全：审批持久化写入失败时，执行器流程不中断（仍可正常审批落盘）"""
    gid = _new_goal_id()
    executor = TaskExecutor(approval_store=_BoomStore())
    restore = _patch_web_search(lambda query, **kw: "1. 异常安全结果\n   内容")
    try:
        gen = executor.execute_plan([_make_task(gid, "搜索异常安全任务")])
        next(gen), next(gen)
        ev = next(gen)
        assert ev["type"] == "approval"
        assert ev["approval"].get("pending_id") is None, "持久化失败时无记录 ID"

        # 内存流仍可正常批准落盘（生成器不因存储故障中断）
        ev_saved = gen.send(True)
        assert ev_saved["type"] == "result" and "✅" in ev_saved["message"]
        done = next(gen)
        assert done["type"] == "done" and done["stats"]["saved"] == 1
    finally:
        restore()
    print("[OK] executor survives approval store failure")


# ---------- 主入口 ----------
def _run_all():
    setup()
    try:
        test_add_pending_writes_row()
        test_update_status_and_resolved_at()
        test_list_pending_after_restart()
        test_execute_plan_persists_and_resolves()
        test_execute_plan_reject_updates_status()
        test_approve_from_persisted_after_restart()
        test_ui_approve_and_reject_from_list()
        test_approval_store_exception_safety()
        test_executor_survives_approval_store_failure()
    finally:
        _cleanup()
    print("\n=== ALL APPROVAL PERSISTENCE TESTS PASSED ===")


if __name__ == "__main__":
    _run_all()
