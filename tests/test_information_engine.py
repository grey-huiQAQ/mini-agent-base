# -*- coding: utf-8 -*-
"""
Information Engine V0.1 测试（深度搜索基础设施）

覆盖：
- 数据库建表：search_tasks / search_evidence 表存在，含全部必需列与预留字段 (2026-09-12)
  （entities / claims / source_quality），init_db 幂等；
- 数据结构：Evidence 默认值与 now_str 时间格式；
- 任务管理：create_task（含空查询校验）/ run_task 同步执行（done/failed/空结果）
  / 单源失败不拖垮整体 / 全部失败标记 failed；
- 并发控制：submit 后台执行，threading 信号量保证同时运行不超过 3 个；
- 证据存储：add/get/update/delete/count，预留字段可空，saved_note_id 标记； (2026-09-12)
- 笔记内容渲染：render_evidence_note 纯函数；
- UI 回调契约：导入 app.py 构建界面 + 用 mock 搜索源跑通
  「开始搜索 → 刷新 → 查看证据 → 勾选确认保存」全流程（保存用 mock NoteStore）。

所有测试离线运行，不访问真实网络、不调用 LLM：
- 搜索源一律使用 MockProvider（可配置结果 / 异常 / 延迟）；
- 测试产生的 search_tasks / search_evidence 行在 teardown 中按 ID 精确清理，
  不触碰开发库中已有的真实任务。
"""
import os
import re
import sys
import threading
import time
import uuid
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection
from core.information_engine.models import Evidence, now_str, TaskStatus
from core.information_engine.evidence_store import EvidenceStore, render_evidence_note
from core.information_engine.search_task import SearchTaskManager
# Phase 3b：深度搜索回调已迁入 search_controller，task manager 注入目标随之迁移
from ui.controllers import search_controller as _search_ctl

# pytest 可选依赖（本文件可脱离 pytest 直接运行）
try:
    import pytest
except ImportError:
    pytest = None


# ---------- 测试数据管理 ----------

_PREFIX = "ie_test_" + uuid.uuid4().hex[:6]
_created_task_ids = []
_created_evidence_ids = []
_created_goal_ids = []


def _track_task(task_id):
    if task_id:
        _created_task_ids.append(task_id)
    return task_id


def _track_goal(goal_id):
    """登记本测试创建的目标（teardown 时按 ID 清理，不触碰真实目标）"""
    if goal_id:
        _created_goal_ids.append(goal_id)
    return goal_id


def _cleanup():
    """按 ID 精确清理本测试产生的任务/证据/目标行（不触碰真实数据）"""
    conn = get_connection()
    try:
        if _created_task_ids:
            placeholders = ",".join("?" * len(_created_task_ids))
            conn.execute(
                f"DELETE FROM search_evidence WHERE task_id IN ({placeholders})",
                _created_task_ids,
            )
            conn.execute(
                f"DELETE FROM search_tasks WHERE id IN ({placeholders})",
                _created_task_ids,
            )
        if _created_evidence_ids:
            placeholders = ",".join("?" * len(_created_evidence_ids))
            conn.execute(
                f"DELETE FROM search_evidence WHERE id IN ({placeholders})",
                _created_evidence_ids,
            )
        if _created_goal_ids:
            placeholders = ",".join("?" * len(_created_goal_ids))
            conn.execute(
                f"DELETE FROM goals WHERE goal_id IN ({placeholders})",
                _created_goal_ids,
            )
        conn.commit()
    finally:
        conn.close()
    _created_task_ids.clear()
    _created_evidence_ids.clear()
    _created_goal_ids.clear()


def setup():
    """初始化数据库（触发 search_tasks / search_evidence 表创建）"""
    init_db()
    _cleanup()


def teardown():
    """测试后清理"""
    _cleanup()


# ---------- Mock 搜索源 ----------

class MockProvider:
    """测试用 mock 搜索源：固定返回预置证据，可配置抛异常 / 延迟 / 并发追踪"""

    def __init__(self, name="Mock", results=None, error=None, delay=0.0, track=None):
        self.name = name
        self.results = list(results or [])
        self.error = error
        self.delay = delay
        self.track = track
        self.search_calls = 0

    def search(self, query, limit=5):
        self.search_calls += 1
        if self.track:
            self.track.enter()
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error:
                raise RuntimeError(self.error)
            return list(self.results)
        finally:
            if self.track:
                self.track.exit()


class ConcurrencyTracker:
    """统计同时进入 search() 的最大数量（验证并发上限）"""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self):
        with self.lock:
            self.active -= 1


def make_evidence(title="测试证据", url="https://example.com/1", source="Mock"):
    """构造一条测试证据"""
    return Evidence(
        source_url=url, source_name=source, title=title,
        snippet=f"{title} 的摘要内容", confidence=0.7,
        retrieved_at=now_str(),
    )


def _extract_task_id(status_text):
    """从 do_start_search 的返回文本中解析任务 ID"""
    m = re.search(r"#(\d+)", status_text)
    return int(m.group(1)) if m else None


# ---------- 1. 数据库建表 ----------

def test_search_tables_created():
    """init_db 后 search_tasks / search_evidence 表应存在且含必需列"""
    init_db()
    conn = get_connection()
    try:
        task_cols = [r[1] for r in conn.execute("PRAGMA table_info(search_tasks)").fetchall()]
        ev_cols = [r[1] for r in conn.execute("PRAGMA table_info(search_evidence)").fetchall()]
    finally:
        conn.close()
    for col in ["id", "query", "status", "created_at", "completed_at", "result_summary"]:
        assert col in task_cols, f"search_tasks 缺少列 {col}，实际为 {task_cols}"
    for col in ["id", "task_id", "source_url", "source_name", "title", "snippet",
                "confidence", "published_at", "retrieved_at",
                "entities", "claims", "source_quality"]:
        assert col in ev_cols, f"search_evidence 缺少列 {col}，实际为 {ev_cols}"
    print("[OK] search tables created with all columns")


def test_search_tables_idempotent():
    """重复调用 init_db 不应报错（幂等）"""
    init_db()
    init_db()
    print("[OK] idempotent init")


# ---------- 2. 数据结构 ----------

def test_evidence_model_defaults():
    """Evidence 默认值：来源/标题为空串，置信度 0.5，预留字段为 None (2026-09-12)"""
    ev = Evidence()
    assert ev.source_url == "" and ev.title == ""
    assert ev.confidence == 0.5
    assert ev.entities is None and ev.claims is None and ev.source_quality is None
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", now_str())
    print("[OK] evidence model defaults")


# ---------- 3. 任务管理（同步执行） ----------

def test_create_task_pending():
    """create_task 返回任务 ID，状态为 pending，查询词正确存储"""
    manager = SearchTaskManager(providers=[MockProvider()])
    task_id = _track_task(manager.create_task("  什么是 Transformer？  "))
    task = manager.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["query"] == "什么是 Transformer？"  # 首尾空白被去除
    assert task["completed_at"] is None
    print(f"[OK] create task pending (id={task_id})")


def test_create_task_empty_query_raises():
    """空查询应抛 ValueError"""
    manager = SearchTaskManager(providers=[MockProvider()])
    try:
        manager.create_task("   ")
    except ValueError:
        print("[OK] empty query raises ValueError")
        return
    raise AssertionError("空查询未抛出 ValueError")


def test_run_task_with_mock_provider():
    """同步执行：done + 证据写入临时存储 + 摘要生成"""
    provider = MockProvider(name="MockWeb", results=[
        make_evidence("证据A", "https://a.example.com", "MockWeb"),
        make_evidence("证据B", "https://b.example.com", "MockWeb"),
    ])
    manager = SearchTaskManager(providers=[provider])
    task_id = _track_task(manager.create_task("测试查询"))
    task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "2 条证据" in task["result_summary"], task["result_summary"]
    assert "MockWeb" in task["result_summary"]
    evidences = EvidenceStore.get_evidence_by_task(task_id)
    assert len(evidences) == 2
    assert evidences[0]["title"] == "证据A"
    assert evidences[0]["source_url"] == "https://a.example.com"
    assert evidences[0]["source_name"] == "MockWeb"
    # V0.2 起 confidence 由 rank_evidence 重新评分（0~1），不再等于 Provider 原始值 0.7
    assert 0 < evidences[0]["confidence"] <= 1
    assert task["completed_at"] is not None
    print(f"[OK] run task done (evidence={len(evidences)})")


def test_run_task_partial_provider_failure():
    """单个来源失败不拖垮整体：另一个来源的结果仍被保存，任务 done"""
    ok = MockProvider(name="OkSource", results=[make_evidence("好来源证据")])
    bad = MockProvider(name="BadSource", error="网络超时")
    manager = SearchTaskManager(providers=[ok, bad])
    task_id = _track_task(manager.create_task("查询"))
    task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "1 条证据" in task["result_summary"]
    assert EvidenceStore.count_evidence(task_id) == 1
    print("[OK] partial provider failure tolerated")


def test_run_task_all_providers_fail():
    """所有来源均失败：任务标记 failed，摘要保留错误信息"""
    bad1 = MockProvider(name="A", error="连接失败")
    bad2 = MockProvider(name="B", error="限流 403")
    manager = SearchTaskManager(providers=[bad1, bad2])
    task_id = _track_task(manager.create_task("查询"))
    task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.FAILED, task
    assert "连接失败" in task["result_summary"]
    assert EvidenceStore.count_evidence(task_id) == 0
    print("[OK] all providers fail -> failed")


def test_run_task_empty_results():
    """来源返回空列表：任务 done，摘要提示未找到证据"""
    manager = SearchTaskManager(providers=[MockProvider(name="Empty", results=[])])
    task_id = _track_task(manager.create_task("冷门查询"))
    task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "未找到任何相关证据" in task["result_summary"]
    print("[OK] empty results handled")


# ---------- 4. 证据存储 ----------

def test_evidence_store_crud():
    """EvidenceStore 增删改查全流程"""
    task_id = _track_task(SearchTaskManager(providers=[MockProvider()]).create_task("查询"))
    eid = EvidenceStore.add_evidence(
        task_id, source_url="https://x.example.com", source_name="GitHub",
        title="仓库X", snippet="描述", confidence=0.8,
        published_at="2024-01-01T00:00:00Z",
    )
    assert eid > 0
    _created_evidence_ids.append(eid)

    ev = EvidenceStore.get_evidence(eid)
    assert ev["title"] == "仓库X" and ev["source_name"] == "GitHub"
    assert ev["confidence"] == 0.8
    assert ev["retrieved_at"]  # 未传时自动取当前时间
    assert ev["entities"] is None and ev["claims"] is None and ev["source_quality"] is None

    assert EvidenceStore.count_evidence(task_id) == 1
    assert len(EvidenceStore.get_evidence_by_task(task_id)) == 1

    # 更新（列白名单）
    assert EvidenceStore.update_evidence(eid, confidence=0.9, saved_note_id=77)
    ev2 = EvidenceStore.get_evidence(eid)
    assert ev2["confidence"] == 0.9 and ev2["saved_note_id"] == 77

    # 不允许的列应抛错
    try:
        EvidenceStore.update_evidence(eid, evil="x")
    except ValueError:
        pass
    else:
        raise AssertionError("非法列未抛 ValueError")

    # 删除
    assert EvidenceStore.delete_evidence(eid)
    assert EvidenceStore.get_evidence(eid) is None
    print("[OK] evidence store crud")


def test_evidence_reserved_fields_writable():
    """预留字段（entities/claims/source_quality）本次不填充但列可用、可写入 (2026-09-12)"""
    task_id = _track_task(SearchTaskManager(providers=[MockProvider()]).create_task("查询"))
    eid = EvidenceStore.add_evidence(task_id, title="预留字段测试")
    _created_evidence_ids.append(eid)
    # 预留字段默认 NULL (2026-09-12)
    ev = EvidenceStore.get_evidence(eid)
    assert ev["entities"] is None and ev["claims"] is None and ev["source_quality"] is None
    # 可写入（为 V0.2 接口预留） (2026-09-12)
    assert EvidenceStore.update_evidence(
        eid, entities='["Transformer"]', claims='["位置编码是关键"]', source_quality="high"
    )
    ev2 = EvidenceStore.get_evidence(eid)
    assert ev2["entities"] == '["Transformer"]'
    assert ev2["claims"] == '["位置编码是关键"]'
    assert ev2["source_quality"] == "high"
    print("[OK] reserved fields writable")


def test_render_evidence_note():
    """笔记内容渲染纯函数：包含标题/来源/链接/摘要，空证据安全返回"""
    task_id = _track_task(SearchTaskManager(providers=[MockProvider()]).create_task("查询"))
    eid = EvidenceStore.add_evidence(
        task_id, source_url="https://n.example.com", source_name="Mock",
        title="笔记证据", snippet="第一行摘要",
    )
    _created_evidence_ids.append(eid)
    evidences = EvidenceStore.get_evidence_by_task(task_id)
    md = render_evidence_note("查询", evidences)
    assert "深度搜索：查询" in md
    assert "笔记证据" in md and "https://n.example.com" in md and "Mock" in md
    assert "第一行摘要" in md
    empty_md = render_evidence_note("空查询", [])
    assert "无证据" in empty_md
    print("[OK] render evidence note")


# ---------- 5. 后台执行与并发控制 ----------

def test_concurrency_limit():
    """6 个任务并发执行，同时运行数不得超过 max_concurrency（3）"""
    tracker = ConcurrencyTracker()
    provider = MockProvider(name="Slow", delay=0.25, track=tracker)
    manager = SearchTaskManager(providers=[provider], max_concurrency=3)

    ids = []
    for i in range(6):
        ids.append(_track_task(manager.submit(f"并发查询{i}")))

    # 等待全部到达终态
    deadline = time.time() + 20
    while time.time() < deadline:
        statuses = [manager.get_status(tid) for tid in ids]
        if all(s in (TaskStatus.DONE, TaskStatus.FAILED) for s in statuses):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"任务未在期限内完成: {statuses}")

    assert tracker.max_active <= 3, f"并发数超过上限: {tracker.max_active}"
    assert all(s == TaskStatus.DONE for s in statuses), statuses
    assert provider.search_calls == 6
    print(f"[OK] concurrency limited to {tracker.max_active} (max 3)")


def test_submit_and_wait_for_completion():
    """submit = create + start；wait_for_completion 等待终态"""
    manager = SearchTaskManager(providers=[MockProvider(results=[make_evidence()])])
    task_id = _track_task(manager.submit("后台查询"))
    task = manager.wait_for_completion(task_id, timeout=15)
    assert task["status"] == TaskStatus.DONE, task
    assert EvidenceStore.count_evidence(task_id) == 1
    print("[OK] submit + wait for completion")


def test_start_returns_false_for_non_pending():
    """非 pending 状态（如已完成）的任务不可重复启动"""
    manager = SearchTaskManager(providers=[MockProvider(results=[make_evidence()])])
    task_id = _track_task(manager.submit("查询"))
    manager.wait_for_completion(task_id, timeout=15)
    assert manager.start(task_id) is False  # 已 done，拒绝二次启动
    # 同一个 pending 任务也不能重复 start（登记表防护）
    task_id2 = _track_task(manager.create_task("查询2"))
    assert manager.start(task_id2) is True
    assert manager.start(task_id2) is False
    manager.wait_for_completion(task_id2, timeout=15)
    print("[OK] start guards non-pending / duplicate start")


# ---------- 6. UI 回调契约（导入 app.py 构建界面 + mock 搜索源全流程） ----------

def test_ui_callback_contract():
    """UI 回调契约：导入 app.py（构建完整界面）后用 mock 搜索源跑通全流程

    流程：开始搜索 → 刷新任务列表 → 查看证据 → 勾选确认保存（mock NoteStore）。
    保存动作使用 mock，避免真实写盘；若环境无法导入 app.py 则打印 SKIP 跳过。
    """
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 回调契约测试")
        return

    provider = MockProvider(name="MockWeb", results=[
        make_evidence("UI 契约证据", "https://ui.example.com/1", "MockWeb"),
    ])
    manager = SearchTaskManager(providers=[provider], max_concurrency=3)
    original = _search_ctl.search_task_manager
    _search_ctl.search_task_manager = manager
    task_id = None
    try:
        # 1) 开始搜索（含首尾空白清理）
        status, cleared, _ev_update = app_module.do_start_search("  深度搜索 UI 契约测试  ")
        assert "已创建" in status, status
        task_id = _track_task(_extract_task_id(status))
        assert task_id is not None, status
        assert cleared == ""

        # 2) 刷新任务列表（保留选中值逻辑不抛异常）
        panel_md, _dd_update = app_module.do_refresh_search_panel(task_id)
        assert str(task_id) in panel_md, panel_md

        # 3) 等待后台任务完成（mock 源瞬时返回）
        task = manager.wait_for_completion(task_id, timeout=15)
        assert task and task["status"] == TaskStatus.DONE, task

        # 4) 查看任务：结果 Markdown + 勾选框选项
        result_md, check_update = app_module.do_view_search_task(task_id)
        assert "条证据" in result_md and "UI 契约证据" in result_md, result_md
        choices = check_update["choices"]
        assert choices, "证据勾选框应为空时无选项"

        # 确认保存为笔记（mock NoteStore.add_note；目标需真实存在 —— 先创建临时目标）。
        # 解冻：直接 patch memory.note_store.NoteStore 类方法（app.NoteStore 即该类对象）
        goal = app_module.goal_manager.create_goal(
            f"UI契约目标_{uuid.uuid4().hex[:6]}", "UI 契约测试临时目标")
        _track_goal(goal.goal_id)
        with patch("memory.note_store.NoteStore.add_note", return_value=1001) as mock_add_note:
            save_status = app_module.do_save_search_results(
                task_id, goal.goal_id, [str(choices[0][1])]
            )
            assert "已把 1 条证据保存为笔记" in save_status, save_status
            assert "1001" in save_status, save_status
            assert mock_add_note.call_count == 1

        # 6) 用户确认保护：未勾选任何证据时拒绝保存
        empty_status = app_module.do_save_search_results(task_id, None, [])
        assert "勾选" in empty_status, empty_status

        # 7) 防重复保存：同一批证据再次保存应被拒绝（saved_note_id 已非空）
        with patch("memory.note_store.NoteStore.add_note", return_value=1002) as mock_add_note:
            dup_status = app_module.do_save_search_results(
                task_id, goal.goal_id, [str(choices[0][1])]
            )
            assert "已保存过，无需重复保存" in dup_status, dup_status
            assert mock_add_note.call_count == 0

        # 8) 保存目标下拉框刷新（无异常）
        goal_dd = app_module.refresh_search_save_goals()
        assert "choices" in goal_dd

        print(f"[OK] ui callback contract flow (task #{task_id})")
    finally:
        _search_ctl.search_task_manager = original


# ---------- 7. 删除任务接口 ----------

def test_delete_task_removes_task_and_evidence():
    """delete_task：任务与证据级联删除（事务），任务不存在返回 False"""
    manager = SearchTaskManager(providers=[MockProvider(results=[make_evidence()])])
    task_id = _track_task(manager.submit("待删除查询"))
    manager.wait_for_completion(task_id, timeout=15)
    assert manager.get_task(task_id) is not None
    assert EvidenceStore.count_evidence(task_id) == 1

    assert manager.delete_task(task_id) is True
    assert manager.get_task(task_id) is None
    assert EvidenceStore.count_evidence(task_id) == 0
    # 重复删除：任务已不存在 → False
    assert manager.delete_task(task_id) is False
    print("[OK] delete task removes task and evidence")


# ---------- 8. 保存目标校验与证据去重（UI 回调） ----------

def test_resolve_search_save_goal_validation():
    """目标解析：提供 ID 校验存在性；留空复用/自动创建「资料搜索」"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过目标解析测试")
        return
    from core.goal_manager import Goal

    class StubGM:
        """目标管理器桩：可配置查询/创建结果与异常"""

        def __init__(self, existing=None, get_result=None, create_error=None):
            self.existing = existing
            self.get_result = get_result
            self.create_error = create_error
            self.created_titles = []

        def find_goal_by_exact_title(self, title, status="active"):
            return self.existing

        def get_goal(self, goal_id):
            return self.get_result

        def create_goal(self, title, description="", **kwargs):
            if self.create_error:
                raise self.create_error
            self.created_titles.append(title)
            return Goal(goal_id="goal_auto_created", title=title, description=description)

    # 用户提供 ID 但目标不存在 → 友好错误
    gid, err = app_module.resolve_search_save_goal("no_such_goal", StubGM(get_result=None))
    assert gid is None and "目标不存在" in err, (gid, err)
    # 用户提供 ID 且目标存在 → 原样返回
    gid, err = app_module.resolve_search_save_goal(
        "real_goal", StubGM(get_result=Goal(goal_id="real_goal", title="真目标")))
    assert gid == "real_goal" and err == "", (gid, err)
    # 留空且「资料搜索」已存在 → 复用
    gid, err = app_module.resolve_search_save_goal(
        "", StubGM(existing=Goal(goal_id="goal_ziliao", title="资料搜索")))
    assert gid == "goal_ziliao" and err == "", (gid, err)
    # 留空且不存在 → 自动创建
    stub = StubGM(existing=None, get_result=None)
    gid, err = app_module.resolve_search_save_goal("", stub)
    assert gid == "goal_auto_created" and err == "", (gid, err)
    assert stub.created_titles == ["资料搜索"]
    # 自动创建失败 → 报错
    stub = StubGM(existing=None, create_error=RuntimeError("磁盘满"))
    gid, err = app_module.resolve_search_save_goal("", stub)
    assert gid is None and "自动创建" in err and "磁盘满" in err, (gid, err)
    print("[OK] resolve search save goal validation")


def test_save_search_results_goal_validation_and_autocreate():
    """保存时目标校验：不存在的目标报错；留空自动创建「资料搜索」目标"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过保存目标校验测试")
        return

    provider = MockProvider(name="GoalCheck", results=[
        make_evidence("目标校验证据", "https://goal.example.com/1", "GoalCheck"),
    ])
    manager = SearchTaskManager(providers=[provider], max_concurrency=3)
    original = _search_ctl.search_task_manager
    _search_ctl.search_task_manager = manager
    try:
        task_id = _track_task(manager.create_task("目标校验查询"))
        manager.run_task(task_id)
        evidences = EvidenceStore.get_evidence_by_task(task_id)
        eid = str(evidences[0]["id"])

        # 1) 提供不存在的目标 ID → 友好错误，不写入
        with patch("memory.note_store.NoteStore.add_note") as mock_add_note:
            status = app_module.do_save_search_results(task_id, "no_such_goal_xyz", [eid])
            assert "目标不存在" in status, status
            assert mock_add_note.call_count == 0

        # 2) 留空 → 自动创建/复用「资料搜索」目标并保存（mock NoteStore）
        before = app_module.goal_manager.find_goal_by_exact_title("资料搜索")
        created_by_us = before is None
        with patch("memory.note_store.NoteStore.add_note", return_value=3001) as mock_add_note:
            status = app_module.do_save_search_results(task_id, None, [eid])
            assert "已把 1 条证据保存为笔记" in status, status
            assert "3001" in status, status
        after = app_module.goal_manager.find_goal_by_exact_title("资料搜索")
        assert after is not None, "「资料搜索」目标应存在（复用或自动创建）"
        if created_by_us:
            _track_goal(after.goal_id)  # 本测试创建的目标，teardown 清理

        # 证据已打保存标记
        ev = EvidenceStore.get_evidence(int(eid))
        assert ev["saved_note_id"] == 3001, ev
        print("[OK] save goal validation + auto create")
    finally:
        _search_ctl.search_task_manager = original


def test_save_search_results_dedup():
    """证据去重：部分已保存只保存未保存的并提示跳过数量；全部已保存拒绝保存"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过去重测试")
        return

    provider = MockProvider(name="Dedup", results=[
        make_evidence("去重证据1", "https://d.example.com/1", "Dedup"),
        make_evidence("去重证据2", "https://d.example.com/2", "Dedup"),
    ])
    manager = SearchTaskManager(providers=[provider], max_concurrency=3)
    original = _search_ctl.search_task_manager
    _search_ctl.search_task_manager = manager
    try:
        task_id = _track_task(manager.create_task("去重查询"))
        manager.run_task(task_id)
        evidences = EvidenceStore.get_evidence_by_task(task_id)
        e1, e2 = str(evidences[0]["id"]), str(evidences[1]["id"])
        # 预置：e1 已保存过（saved_note_id 非空）
        EvidenceStore.update_evidence(int(e1), saved_note_id=888)
        goal = app_module.goal_manager.create_goal(
            f"去重目标_{uuid.uuid4().hex[:6]}", "去重测试临时目标")
        _track_goal(goal.goal_id)

        # 部分已保存：只保存未保存的 e2，跳过已保存的 e1
        with patch("memory.note_store.NoteStore.add_note", return_value=2002) as mock_add_note:
            status = app_module.do_save_search_results(task_id, goal.goal_id, [e1, e2])
            assert "已把 1 条证据保存为笔记" in status, status
            assert "已跳过 1 条已保存过的证据" in status, status
            assert mock_add_note.call_count == 1
            # 笔记内容只包含未保存过的证据
            call_content = mock_add_note.call_args[0][2]
            assert "去重证据2" in call_content and "去重证据1" not in call_content, call_content
        ev2 = EvidenceStore.get_evidence(int(e2))
        assert ev2["saved_note_id"] == 2002, ev2

        # 全部已保存：拒绝重复保存
        with patch("memory.note_store.NoteStore.add_note") as mock_add_note:
            status = app_module.do_save_search_results(task_id, goal.goal_id, [e1, e2])
            assert "已保存过，无需重复保存" in status, status
            assert mock_add_note.call_count == 0
        print("[OK] save dedup (partial + all saved)")
    finally:
        _search_ctl.search_task_manager = original


def test_ui_delete_search_task():
    """UI 删除任务回调：删除后任务/证据消失，列表刷新、预览与勾选框清空"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 删除测试")
        return

    provider = MockProvider(name="DelUI", results=[
        make_evidence("删除测试证据", "https://del.example.com/1", "DelUI"),
    ])
    manager = SearchTaskManager(providers=[provider], max_concurrency=3)
    original = _search_ctl.search_task_manager
    _search_ctl.search_task_manager = manager
    try:
        task_id = _track_task(manager.create_task("删除测试查询"))
        manager.run_task(task_id)
        assert EvidenceStore.count_evidence(task_id) == 1

        # 未选择任务 → 友好提示
        status = app_module.do_delete_search_task(None)[0]
        assert "请先在上方选择一个搜索任务" in status, status

        # 删除选中任务：状态 + 列表刷新 + 预览/勾选框清空 + 确认行隐藏
        (status, panel_md, dd_update, preview, check_update,
         row_upd, _yes_upd, _no_upd) = app_module.do_delete_search_task(task_id)
        assert "已删除任务" in status and str(task_id) in status, status
        assert str(task_id) not in panel_md, panel_md      # 列表已刷新，任务消失
        assert dd_update["value"] is None                  # 下拉框选中值清空
        assert preview == "选择任务后在此预览证据"            # 结果预览重置
        assert check_update["choices"] == []               # 勾选框清空
        assert row_upd["visible"] is False                 # 确认行隐藏

        # 数据库层面：任务与证据均不存在
        assert manager.get_task(task_id) is None
        assert EvidenceStore.count_evidence(task_id) == 0

        # 再次删除（任务已不存在）→ 提示
        status = app_module.do_delete_search_task(task_id)[0]
        assert "任务不存在" in status, status
        print("[OK] ui delete search task")
    finally:
        _search_ctl.search_task_manager = original


# ---------- 13. V0.2：查询改写 ----------

class _RoundAwareProvider:
    """按子查询返回不同结果的 mock 搜索源（验证多轮合并）"""

    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.calls = []  # 记录收到的子查询

    @property
    def name(self):
        return "RoundMock"

    def search(self, query, limit=5):
        self.calls.append(query)
        return list(self.results_by_query.get(query, []))


def test_llm_query_rewriter_success():
    """LLM 查询改写成功：返回 2~4 个子查询且包含原始查询（兼容代码块）"""
    from core.information_engine.query_rewriter import LLMQueryRewriter
    rewriter = LLMQueryRewriter()
    with patch("business.model_client.chat_sync", return_value=(
        '```json\n["Transformer 位置编码", "positional encoding implementation", '
        '"Transformer attention 中文资料"]\n```'
    )):
        subs = rewriter.rewrite("Transformer 位置编码")
    assert len(subs) >= 2 and len(subs) <= 4, subs
    assert "Transformer 位置编码" in subs, subs
    print("[OK] llm query rewriter success")


def test_llm_query_rewriter_failure_fallback():
    """LLM 调用失败 / 返回非法 → 降级为仅原始查询"""
    from core.information_engine.query_rewriter import LLMQueryRewriter
    rewriter = LLMQueryRewriter()
    # 调用抛异常
    with patch("business.model_client.chat_sync", side_effect=RuntimeError("LLM 超时")):
        assert rewriter.rewrite("原查询") == ["原查询"]
    # 返回非 JSON
    with patch("business.model_client.chat_sync", return_value="这不是 JSON"):
        assert rewriter.rewrite("原查询") == ["原查询"]
    # 返回对象而非数组
    with patch("business.model_client.chat_sync", return_value='{"q": "x"}'):
        assert rewriter.rewrite("原查询") == ["原查询"]
    # 空数组
    with patch("business.model_client.chat_sync", return_value="[]"):
        assert rewriter.rewrite("原查询") == ["原查询"]
    print("[OK] llm query rewriter failure fallback")


def test_query_rewriter_preserves_original():
    """子查询未包含原始查询时，强制保留原始查询"""
    from core.information_engine.query_rewriter import LLMQueryRewriter
    rewriter = LLMQueryRewriter()
    with patch("business.model_client.chat_sync", return_value='["only other query"]'):
        subs = rewriter.rewrite("原查询")
    assert subs[0] == "原查询"  # 强制插入原始查询
    assert len(subs) == 2
    print("[OK] query rewriter preserves original")


def test_rewrite_disabled_by_default():
    """默认不启用查询改写：query_rewriter 为 None，单轮使用原始查询"""
    provider = MockProvider(results=[make_evidence()])
    manager = SearchTaskManager(providers=[provider])
    assert manager.query_rewriter is None
    task_id = _track_task(manager.create_task("单轮查询"))
    manager.run_task(task_id)
    assert provider.search_calls == 1  # 只用原始查询跑一轮
    print("[OK] rewrite disabled by default")


# ---------- 14. V0.2：去重排序 ----------

def test_deduplicate_evidence_url_and_similar():
    """去重：URL 完全一致去重；相同标题（不同 URL）去重；不同证据保留"""
    from core.information_engine.evidence_quality import deduplicate_evidence
    e1 = make_evidence("相同URL", "https://dup.example.com/a")
    e2 = make_evidence("相同URL", "https://dup.example.com/a")       # 同 URL
    e3 = make_evidence("Transformer 位置编码", "https://x.example.com/1")
    e4 = make_evidence("Transformer 位置编码", "https://x.example.com/2")  # 同标题不同 URL
    e5 = make_evidence("完全不同的内容", "https://y.example.com/1")
    deduped = deduplicate_evidence([e1, e2, e3, e4, e5], threshold=0.9)
    urls = [e.source_url for e in deduped]
    assert urls.count("https://dup.example.com/a") == 1
    assert urls.count("https://x.example.com/1") + urls.count("https://x.example.com/2") == 1
    assert "https://y.example.com/1" in urls
    print("[OK] deduplicate evidence url + similar")


def test_deduplicate_evidence_keeps_higher_confidence():
    """去重：重复组内保留置信度更高者（先排序再去重，与管理器流程一致）"""
    from core.information_engine.evidence_quality import deduplicate_evidence
    low = make_evidence("位置编码", "https://k.example.com/a")
    low.confidence = 0.5
    high = make_evidence("位置编码", "https://k.example.com/b")
    high.confidence = 0.9
    deduped = deduplicate_evidence(
        sorted([low, high], key=lambda e: e.confidence, reverse=True))
    assert len(deduped) == 1
    assert deduped[0].confidence == 0.9
    print("[OK] dedup keeps higher confidence")


def test_rank_evidence_scoring():
    """排序评分：GitHub > DuckDuckGo；关键词命中加分；confidence 在 0~1；顺序不变"""
    from core.information_engine.evidence_quality import rank_evidence
    gh = make_evidence("transformer attention implementation",
                       "https://g.example.com", "GitHub")
    ddg = make_evidence("transformer attention implementation",
                        "https://d.example.com", "DuckDuckGo")
    nohit = make_evidence("完全无关的内容", "https://n.example.com", "GitHub")
    ranked = rank_evidence([gh, ddg, nohit], query="transformer attention")
    by_url = {e.source_url: e.confidence for e in ranked}
    assert by_url["https://g.example.com"] > by_url["https://d.example.com"]  # 来源权重
    assert by_url["https://g.example.com"] > by_url["https://n.example.com"]  # 关键词命中
    for c in by_url.values():
        assert 0 <= c <= 1
    # 顺序不变（供调用方排序后去重）
    assert [e.source_url for e in ranked] == [
        "https://g.example.com", "https://d.example.com", "https://n.example.com"]
    print("[OK] rank evidence scoring")


# ---------- 15. V0.2：信息增益停止 ----------

def test_information_gain_stop_triggers():
    """增益停止触发：本轮无新增来源且无新增事实 → True"""
    from core.information_engine.evidence_quality import information_gain_stop
    ev1 = make_evidence("事实A", "https://a.example.com", "GitHub")
    ev1.snippet = "注意力机制是 Transformer 的核心"
    ev2 = make_evidence("事实A2", "https://a2.example.com", "GitHub")
    ev2.snippet = "注意力机制是 Transformer 的核心"  # 同事实（相似度 1.0）
    assert information_gain_stop([ev1], [ev1, ev2], window=2,
                                 source_min=1, fact_min=2) is True
    print("[OK] information gain stop triggers")


def test_information_gain_stop_not_triggered():
    """增益停止不触发：新增来源数/事实数突破阈值 → False；首轮视为充足"""
    from core.information_engine.evidence_quality import information_gain_stop
    old = make_evidence("旧事实", "https://a.example.com", "GitHub")
    old.snippet = "注意力机制是 Transformer 的核心"
    # 新增 2 个来源（> source_min=1）→ 突破来源轴 → False
    new_src1 = make_evidence("新来源1", "https://d1.example.com", "DuckDuckGo")
    new_src1.snippet = "DuckDuckGo 上完全不同的内容 1"
    new_src2 = make_evidence("新来源2", "https://b1.example.com", "Bing")
    new_src2.snippet = "Bing 上完全不同的内容 2"
    assert information_gain_stop([old], [old, new_src1, new_src2],
                                 source_min=1, fact_min=2) is False
    # 仅新增 3 个事实（> fact_min=2，同来源）→ 突破事实轴 → False
    facts = []
    for i in range(3):
        f = make_evidence(f"新事实{i}", f"https://a{i}.example.com", "GitHub")
        f.snippet = f"完全不同的新事实片段内容 编号{i}"
        facts.append(f)
    assert information_gain_stop([old], [old] + facts,
                                 source_min=1, fact_min=2) is False
    # 首轮（无历史基准）视为增益充足
    assert information_gain_stop([], [old], source_min=1, fact_min=2) is False
    print("[OK] information gain stop not triggered")


def test_information_gain_stop_fact_similarity():
    """事实相似度阈值：与历史片段相似（ratio >= 0.8）不算新增事实"""
    from core.information_engine.evidence_quality import information_gain_stop
    old = make_evidence("旧", "https://a.example.com", "GitHub")
    old.snippet = "Transformer 位置编码使用正弦余弦函数"
    similar = make_evidence("新", "https://a2.example.com", "GitHub")
    similar.snippet = "Transformer 位置编码采用正弦和余弦函数"  # 高度相似
    assert information_gain_stop([old], [old, similar],
                                 source_min=1, fact_min=2) is True
    print("[OK] information gain fact similarity")


# ---------- 16. V0.2：多轮搜索流程整合 ----------

def test_run_task_multi_round_with_rewriter():
    """多轮整合：改写器生成多个子查询，每轮合并证据并去重排序入库"""
    from core.information_engine.query_rewriter import LLMQueryRewriter
    provider = _RoundAwareProvider({
        "原始查询": [make_evidence("轮1证据", "https://r1.example.com", "RoundMock")],
        "english variant query": [make_evidence("轮2证据", "https://r2.example.com", "RoundMock")],
    })
    rewriter = LLMQueryRewriter()
    with patch("business.model_client.chat_sync",
               return_value='["原始查询", "english variant query"]'):
        manager = SearchTaskManager(providers=[provider], query_rewriter=rewriter,
                                    max_concurrency=3)
        task_id = _track_task(manager.create_task("原始查询"))
        task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "2 条证据" in task["result_summary"], task["result_summary"]
    assert "2 个子查询" in task["result_summary"], task["result_summary"]
    evidences = EvidenceStore.get_evidence_by_task(task_id)
    assert len(evidences) == 2
    urls = {e["source_url"] for e in evidences}
    assert urls == {"https://r1.example.com", "https://r2.example.com"}
    assert provider.calls == ["原始查询", "english variant query"]
    print("[OK] multi-round search with rewriter")


def test_run_task_information_gain_stop_integration():
    """多轮整合：连续 window 轮无新增事实 → 提前停止（不跑完所有子查询）"""
    from core.information_engine.query_rewriter import LLMQueryRewriter
    provider = MockProvider(name="Same", results=[
        make_evidence("固定证据", "https://s.example.com", "Same"),
    ])
    rewriter = LLMQueryRewriter()
    with patch("business.model_client.chat_sync",
               return_value='["q1", "q2", "q3", "q4"]'):
        manager = SearchTaskManager(providers=[provider], query_rewriter=rewriter,
                                    max_concurrency=3, gain_window=2)
        task_id = _track_task(manager.create_task("q1"))
        task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "信息增益不足" in task["result_summary"], task["result_summary"]
    # 第 1 轮正常，第 2、3 轮连续无增益 → 只跑 3 轮（未跑完 4 个子查询）
    assert provider.search_calls == 3, provider.search_calls
    assert EvidenceStore.count_evidence(task_id) == 1  # 去重后仅 1 条
    print("[OK] multi-round info gain stop integration")


def test_run_task_max_evidence_stop():
    """多轮整合：总证据数达到上限 → 提前停止（每轮都有全新证据）"""
    from core.information_engine.query_rewriter import LLMQueryRewriter

    class _FreshProvider:
        """每次调用返回全新证据（URL/标题/摘要均不同），验证上限停止"""

        @property
        def name(self):
            return "Fresh"

        def __init__(self, per_round=10):
            self.per_round = per_round
            self.search_calls = 0

        def search(self, query, limit=5):
            self.search_calls += 1
            base = self.search_calls * 1000
            return [
                make_evidence(f"新证据{base + i}", f"https://fresh{base + i}.example.com",
                              "Fresh")
                for i in range(self.per_round)
            ]

    provider = _FreshProvider(per_round=10)
    rewriter = LLMQueryRewriter()
    with patch("business.model_client.chat_sync",
               return_value='["q1", "q2", "q3"]'):
        manager = SearchTaskManager(providers=[provider], query_rewriter=rewriter,
                                    max_concurrency=3, max_evidence=15)
        task_id = _track_task(manager.create_task("q1"))
        task = manager.run_task(task_id)
    assert task["status"] == TaskStatus.DONE, task
    assert "上限" in task["result_summary"], task["result_summary"]
    # 第 1 轮 10 条 < 15（增益充足），第 2 轮合并后 20 ≥ 15 → 只跑 2 轮
    assert provider.search_calls == 2, provider.search_calls
    print("[OK] multi-round max evidence stop")


# ---------- 主入口（无 pytest 时直接运行） ----------
def _run_all():
    setup()
    try:
        test_search_tables_created()
        test_search_tables_idempotent()
        test_evidence_model_defaults()
        test_create_task_pending()
        test_create_task_empty_query_raises()
        test_run_task_with_mock_provider()
        test_run_task_partial_provider_failure()
        test_run_task_all_providers_fail()
        test_run_task_empty_results()
        test_evidence_store_crud()
        test_evidence_reserved_fields_writable()
        test_render_evidence_note()
        test_concurrency_limit()
        test_submit_and_wait_for_completion()
        test_start_returns_false_for_non_pending()
        test_ui_callback_contract()
        test_delete_task_removes_task_and_evidence()
        test_resolve_search_save_goal_validation()
        test_save_search_results_goal_validation_and_autocreate()
        test_save_search_results_dedup()
        test_ui_delete_search_task()
        # V0.2
        test_llm_query_rewriter_success()
        test_llm_query_rewriter_failure_fallback()
        test_query_rewriter_preserves_original()
        test_rewrite_disabled_by_default()
        test_deduplicate_evidence_url_and_similar()
        test_deduplicate_evidence_keeps_higher_confidence()
        test_rank_evidence_scoring()
        test_information_gain_stop_triggers()
        test_information_gain_stop_not_triggered()
        test_information_gain_stop_fact_similarity()
        test_run_task_multi_round_with_rewriter()
        test_run_task_information_gain_stop_integration()
        test_run_task_max_evidence_stop()
    finally:
        teardown()
    print("\n=== ALL INFORMATION ENGINE TESTS PASSED ===")


if __name__ == "__main__":
    _run_all()
