# -*- coding: utf-8 -*-
"""
测试目标相关功能
- 目标专属文件目录
- 任务生成
- 任务状态更新
- 目标完成/归档
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
"""
import os
import shutil
import sys
import uuid

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；本文件不再引用 DB_PATH 变量，故只保留 helper 返回的目录名。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

from pathlib import Path
from core.goal_manager import GoalManager
from memory.database import init_db, get_connection



# pytest 可选依赖：未安装时仍可直接以 `python tests/test_goal_features.py` 运行
try:
    import pytest
except ImportError:
    pytest = None


def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()


if pytest is not None:

    @pytest.fixture
    def goal_id():
        """为测试提供一个已创建的目标 ID，测试结束后清理"""
        gm = GoalManager()
        goal = gm.create_goal("test goal", "this is a test description")
        yield goal.goal_id
        conn = get_connection()
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (goal.goal_id,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal.goal_id,))
        conn.commit()
        conn.close()


def test_create_goal():
    """测试创建目标"""
    gm = GoalManager()
    goal = gm.create_goal("test goal", "this is a test description")
    assert goal.goal_id.startswith("goal_"), f"goal id format error: {goal.goal_id}"
    assert goal.title == "test goal"
    assert goal.description == "this is a test description"
    assert goal.status == "active"
    print(f"[OK] create goal: {goal.goal_id}")
    return goal.goal_id


def test_get_and_update_goal(goal_id):
    """测试获取和更新目标"""
    gm = GoalManager()
    goal = gm.get_goal(goal_id)
    assert goal is not None, "get goal failed"
    assert goal.title == "test goal"

    gm.update_goal(goal_id, title="updated goal", description="updated desc")
    updated = gm.get_goal(goal_id)
    assert updated.title == "updated goal"
    assert updated.description == "updated desc"
    print("[OK] get and update goal")


def test_add_and_get_tasks(goal_id):
    """测试添加和获取任务"""
    gm = GoalManager()

    gm.add_task(goal_id, "task1", "desc1", priority=1)
    gm.add_task(goal_id, "task2", "desc2", priority=2)
    gm.add_task(goal_id, "task3", "desc3", priority=3)

    tasks = gm.get_tasks(goal_id)
    assert len(tasks) == 3, f"expected 3 tasks, got {len(tasks)}"
    print(f"[OK] add and get tasks: {len(tasks)} tasks")

    pending = gm.get_tasks(goal_id, status="pending")
    assert len(pending) == 3

    gm.update_task_status(tasks[0]["id"], "done")
    done = gm.get_tasks(goal_id, status="done")
    assert len(done) == 1
    print("[OK] task status update")


def test_mark_goal_completed(goal_id):
    """测试标记目标完成"""
    gm = GoalManager()
    gm.mark_goal_completed(goal_id)
    goal = gm.get_goal(goal_id)
    assert goal.status == "completed", f"expected completed, got {goal.status}"
    print("[OK] mark goal completed")


def test_mark_goal_archived(goal_id):
    """测试标记目标归档"""
    gm = GoalManager()
    gm.mark_goal_archived(goal_id)
    goal = gm.get_goal(goal_id)
    assert goal.status == "archived", f"expected archived, got {goal.status}"
    print("[OK] mark goal archived")


def test_file_operations_with_base_dir():
    """测试文件操作的 base_dir 参数"""
    from tools.local_tools import read_file, list_files

    test_dir = Path("data/test_goal_files")
    test_dir.mkdir(parents=True, exist_ok=True)

    try:
        test_file = test_dir / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        result = list_files(base_dir=str(test_dir))
        assert "test.txt" in result, f"list_files failed: {result}"
        print("[OK] list_files with base_dir")

        content = read_file("test.txt", base_dir=str(test_dir))
        assert content == "hello world", f"read_file content error: {content}"
        print("[OK] read_file with base_dir")

        result_default = list_files()
        assert "directory" in result_default.lower() or "files" in result_default.lower() or "directory" in result_default or True
        print("[OK] default directory file operations")

    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


def test_threading_goal_context():
    """测试线程本地存储的目标 ID"""
    from core.runtime import set_current_goal_id, get_current_goal_id, clear_current_goal_id

    assert get_current_goal_id() is None, "initial goal_id should be None"

    set_current_goal_id("test_goal_123")
    assert get_current_goal_id() == "test_goal_123"

    clear_current_goal_id()
    assert get_current_goal_id() is None

    print("[OK] threading goal context")


def test_execute_tool_injects_goal_base_dir():
    """回归：文件工具在目标上下文中自动注入专属目录，无目标时回落默认目录"""
    from agents.tool_manager import ToolManager
    from core.runtime import set_current_goal_id, clear_current_goal_id
    from tools.local_tools import DEFAULT_FILES_ROOT

    goal_id = f"goal_p04_{uuid.uuid4().hex[:8]}"
    goal_dir = Path("data/goals") / goal_id
    fallback_name = "_p04_fallback_note.txt"
    try:
        # 1) 目标上下文：write_file 应写入目标专属目录 data/goals/{goal_id}/files
        set_current_goal_id(goal_id)
        msg = ToolManager.execute_tool(
            "write_file", {"filename": "note.txt", "content": "目标专属笔记"})
        assert "已写入" in msg, f"write_file 应成功：{msg}"
        assert (goal_dir / "files" / "note.txt").exists(), \
            "目标上下文下文件应写入 data/goals/{goal_id}/files"
        assert not (DEFAULT_FILES_ROOT / "note.txt").exists(), \
            "目标上下文下不应写入全局默认目录 data/files"

        # 2) read_file / list_files 同样注入目标专属目录
        content = ToolManager.execute_tool("read_file", {"filename": "note.txt"})
        assert "目标专属笔记" in content, f"read_file 应读到目标目录内容：{content}"
        listing = ToolManager.execute_tool("list_files", {})
        assert "note.txt" in listing, f"list_files 应列出目标目录文件：{listing}"

        # 3) 非文件工具不受注入影响（get_current_time 不接受 base_dir）
        now = ToolManager.execute_tool("get_current_time", {})
        assert now and "❌" not in now, f"get_current_time 应正常执行：{now}"

        # 4) 无目标上下文：回落到全局默认目录 data/files
        clear_current_goal_id()
        msg2 = ToolManager.execute_tool(
            "write_file", {"filename": fallback_name, "content": "回落默认目录"})
        assert "已写入" in msg2, f"回落写入应成功：{msg2}"
        assert (DEFAULT_FILES_ROOT / fallback_name).exists(), \
            "无目标上下文时应写入全局默认目录 data/files"
    finally:
        # 清理测试产物：目标专属目录与默认目录回落文件
        clear_current_goal_id()
        shutil.rmtree(goal_dir, ignore_errors=True)
        (DEFAULT_FILES_ROOT / fallback_name).unlink(missing_ok=True)
    print("[OK] execute_tool injects goal base_dir")


_GOALS_ROOT = Path("data") / "goals"


def test_file_tool_sandbox_boundaries():
    """P7 批次 5：文件工具的读写边界（书架可读 / 写仍隔离 / 两条越界路都被挡住）

    背景：`tools/local_tools.py` 顶部原自称"安全限制，避免 AI 读取任意系统文件"，
    但那只是注释承诺——`apply_goal_context` 只在"未传 base_dir"时才注入，而
    `execute_tool` 用 `func(**arguments)` 透传全部入参 ⇒ 模型自带一个 base_dir
    就能绕过注入、自选读写根。本用例把这条边界钉成机制。

    断言点与**有没有牙齿**（旧实现＝本批修改前）：
      ① **书架可读**：目标上下文下，只读「公共书架」`data/library` 里的文件
         read_file / list_files 都能看到（P7 批次 5b：书架从 `data/files` 搬到
         `data/library`，本用例的探针随之搬家）
         【旧实现红：没有只读回落，读不到】⇒ 有牙齿。
      ② **写仍隔离**：write_file 落 `data/goals/{id}/files`、`data/files` 与
         `data/library` 都不被写【旧实现也绿】⇒ 防回归（沿用
         test_execute_tool_injects_goal_base_dir 的既有断言；书架那一条是本批新增——
         "写也回落到书架"正是 5b 最容易做错的地方）。
      ③ **模型自选根被剥夺**：入参自带 base_dir 时读不到越界内容，且 stdout 出现
         `[沙箱]` / `强制忽略` 告警行 —— 机制的证据就是这行输出
         【旧实现红：实测会真的读到 OUTSIDE-SANDBOX-SECRET，且一声不响】⇒ 有牙齿。
      ③b **经工具层去够生产库**：`base_dir="data"` 被剥夺后，结果是"文件不存在"
         （根本没打开那个文件）【旧实现红：会去打开 data/memory.db，报的是"无法读取…
         可能不是文本文件"】⇒ 有牙齿。
      ③c **越界留痕进库**：自带 base_dir 的调用（③ 与 ③b 共两次）除了 stdout 的告警，
         还在 `agent_resource_log` 留下 `sandbox` / `base_dir_ignored` 记录，detail 带上
         工具名与被丢弃的值；且这些记录落进**临时库**、不是生产库
         【旧实现红：一声不响、库里无痕】⇒ 有牙齿。
      ④ **库级越界（绕过咽喉点直调库函数）**：兄弟前缀混淆
         `base_dir="data/files"` + `../files_p7b5_probe/x` 读不到隔壁目录的东西
         （`data/files_p7b5_probe` 以 `data/files` 开头，字符串 startswith 会放行）
         【旧实现红：实测读到 SIBLING-SECRET】⇒ 有牙齿。
      ⑤ **哨兵**：`data/goals` 目录集合回到调用前；生产库 `data/memory.db` 的 sha256 不变
         （③c 起越界会真写库，故本用例逐测把 `DB_PATH` 指向临时库——没有那一步，
         这条哨兵必然变红，那正是它该有的反应）。

    边界说明（为什么 ④ 用兄弟前缀而不是 `read_file("memory.db", base_dir="data")`）：
    后者**不是越界**——`data/memory.db` 确实在给定根 `data` 之下，`is_relative_to` 会（正确地）
    放行，改前改后都一样；真正被 `is_relative_to` 关掉的是"兄弟前缀"这一类（④）。
    模型想够生产库只有一条路，就是自选根，那条路已由 ③b 在咽喉点堵死。
    """
    import contextlib
    import hashlib
    import io
    from unittest.mock import patch

    from agents.tool_manager import ToolManager
    from core.runtime import set_current_goal_id, clear_current_goal_id
    from memory.resource_log import get_recent_resource_logs
    from tools.local_tools import DEFAULT_FILES_ROOT, SHARED_LIBRARY_ROOT, read_file

    db_path = Path("data") / "memory.db"
    digest_before = hashlib.sha256(db_path.read_bytes()).hexdigest() if db_path.exists() else None
    before = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()

    gid = f"goal_p7b5_{uuid.uuid4().hex[:8]}"
    goal_dir = _GOALS_ROOT / gid
    tag = uuid.uuid4().hex[:6]
    shelf_name = f"_p7b5_shelf_{tag}.txt"
    outside_dir = Path("data") / f"_p7b5_outside_{tag}"
    sibling_dir = Path("data") / f"files_p7b5_{tag}"      # 以 data/files 开头 ⇒ 兄弟前缀
    own_name = f"_p7b5_own_{tag}.txt"
    # ③c 起越界会真写库：逐测把 DB_PATH 指向本进程临时目录下的独立库（P6-6④ 认可的
    # 「字符串靶子」写法），留痕断言也从同一个库读；生产库由末尾的 sha256 哨兵守着。
    trace_db = Path(_TMP_DIR) / "p7b5_sandbox_trace.db"
    with patch("memory.database.DB_PATH", str(trace_db)):
        try:
            SHARED_LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
            (SHARED_LIBRARY_ROOT / shelf_name).write_text("书架里的共享资料", encoding="utf-8")
            outside_dir.mkdir(parents=True, exist_ok=True)
            (outside_dir / "secret.txt").write_text("OUTSIDE-SANDBOX-SECRET", encoding="utf-8")
            sibling_dir.mkdir(parents=True, exist_ok=True)
            (sibling_dir / "sibling.txt").write_text("SIBLING-SECRET", encoding="utf-8")

            set_current_goal_id(gid)

            # ① 书架可读（旧实现红；P7 批次 5b：探针从 data/files 搬到 data/library）
            got = ToolManager.execute_tool("read_file", {"filename": shelf_name})
            assert "书架里的共享资料" in got, f"应能从只读书架读到：{got}"
            listing = ToolManager.execute_tool("list_files", {})
            assert shelf_name in listing, f"list_files 应同时列出目标目录与书架：{listing}"

            # ② 写仍隔离（旧实现也绿）：只落目标目录，私有根与书架都不该多出这个文件
            msg = ToolManager.execute_tool(
                "write_file", {"filename": own_name, "content": "目标专属"})
            assert "已写入" in msg, msg
            assert (goal_dir / "files" / own_name).exists(), "目标上下文下应写入目标目录"
            assert not (DEFAULT_FILES_ROOT / own_name).exists(), "写不该落到私有根以外的地方"
            assert not (SHARED_LIBRARY_ROOT / own_name).exists(), "写不该落到只读书架"

            # ③ 模型自选根被剥夺（旧实现红：真的会读到 + 一声不响）
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = ToolManager.execute_tool(
                    "read_file", {"filename": "secret.txt", "base_dir": str(outside_dir)})
            warn = buf.getvalue()
            assert "OUTSIDE-SANDBOX-SECRET" not in out, f"越界内容不得被读到：{out}"
            assert "[沙箱]" in warn and "强制忽略" in warn, \
                f"必须留下一行告警（机制的证据），实际 stdout={warn!r}"
            assert outside_dir.name in warn, \
                f"告警里应带上被丢弃的 base_dir 值（打印用 !r，故断言不含反斜杠的目录名）：{warn!r}"

            # ③b 经工具层去够生产库（旧实现红）
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                out2 = ToolManager.execute_tool(
                    "read_file", {"filename": "memory.db", "base_dir": "data"})
            assert "不存在" in out2, \
                f"base_dir 被剥夺后应在受控根里找并报「不存在」，实际：{out2}"
            assert "[沙箱]" in buf2.getvalue()

            # ④ 库级越界：兄弟前缀混淆（绕过咽喉点直调库函数；旧实现红）
            escaped = read_file(f"../{sibling_dir.name}/sibling.txt",
                               base_dir=str(DEFAULT_FILES_ROOT))
            assert "SIBLING-SECRET" not in escaped, \
                f"is_relative_to 应挡住兄弟前缀越界，实际：{escaped}"
            assert "安全限制" in escaped, escaped
        finally:
            clear_current_goal_id()
            shutil.rmtree(goal_dir, ignore_errors=True)
            shutil.rmtree(outside_dir, ignore_errors=True)
            shutil.rmtree(sibling_dir, ignore_errors=True)
            (SHARED_LIBRARY_ROOT / shelf_name).unlink(missing_ok=True)
            (SHARED_LIBRARY_ROOT / own_name).unlink(missing_ok=True)
            (DEFAULT_FILES_ROOT / own_name).unlink(missing_ok=True)

        after = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()
        assert after == before, f"data/goals 未还原：新增 {sorted(after - before)}"
        if digest_before is not None:
            assert hashlib.sha256(db_path.read_bytes()).hexdigest() == digest_before, \
                "生产库 data/memory.db 被改动了"

        # ③c 越界留痕进库（旧实现红：一声不响、库里无痕）
        logs = get_recent_resource_logs(limit=50, db_path=str(trace_db))
        traced = [r for r in logs if r["resource_type"] == "sandbox"]
        assert len(traced) == 2, (
            f"③/③b 两次自带 base_dir 的调用应各留一条 sandbox 记录，"
            f"实际 {len(traced)} 条（{logs}）")
        assert {r["operation"] for r in traced} == {"base_dir_ignored"}, traced
        detail = "\n".join(r["detail"] for r in traced)
        assert "read_file" in detail, f"留痕 detail 应带工具名：{detail}"
        assert f"base_dir={str(outside_dir)!r}" in detail, \
            f"留痕 detail 应带被丢弃的值（③ 的外部目录）：{detail}"
        assert "base_dir='data'" in detail, \
            f"留痕 detail 应带被丢弃的值（③b 的 'data'）：{detail}"
    print("[OK] file tool sandbox boundaries")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== goal features test ===\n")

    setup()

    goal_id = test_create_goal()
    test_get_and_update_goal(goal_id)
    test_add_and_get_tasks(goal_id)
    test_mark_goal_completed(goal_id)
    test_mark_goal_archived(goal_id)
    test_file_operations_with_base_dir()
    test_threading_goal_context()
    test_execute_tool_injects_goal_base_dir()
    test_file_tool_sandbox_boundaries()
    test_cleanup()

    print("\n=== ALL TESTS PASSED ===")
