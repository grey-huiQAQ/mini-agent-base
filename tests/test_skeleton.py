# -*- coding: utf-8 -*-
"""架构骨架层行为固化测试（P4-7）—— 只覆盖"已被改过或即将被改"的 5 个模块

为什么需要它（用户给出的定位）：
    本项目 36 个测试文件、420 个用例里，**架构骨架层长期零覆盖**：core/ 8 个模块、
    plugins/ 全部、rag/ 4 个、tools/ 3 个、scheduler.py、agents/daily_agent.py 都没有
    直接测试。而重构要动的恰恰是没网的那一层——"413 用例全绿"对这一层不构成任何证据。
    本文件只给 Phase 3 已动过或即将动的 5 个模块补**行为固化**（不补覆盖率、不改行为）：
      - memory/database.py      —— 所有隔离机制的地基（P4-3 刚动过）
      - core/runtime.py         —— 有状态 + current_goal_id 线程本地全局
      - core/plugin_manager.py  —— 加载失败静默（只打印）
      - agents/tool_manager.py  —— 刚被 P0-4 改过（目标专属文件目录注入）
      - scheduler.py            —— 每日 08:00 / 20:00 注册

设计说明：
- 本文件同样走共享 helper 做 DB 隔离，且隔离调用早于任何业务模块 import
  （满足 checks/check_test_db_isolation.py 的 P4-3c 顺序规则）。
- 涉及文件系统产物的用例（tool_manager 的目标目录注入）**必须在 finally 里还原
  data/goals 目录集合**——`read_file` 会 mkdir，不还原就会让 §七 那条
  "data/goals 目录数前后不变"的泄漏哨兵失效。
"""
import ast
import os
import shutil
import sys
import threading
import time
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db

_TMP_DIR = setup_test_db()

from memory import database as db
from memory.chat_store import ChatStore
from memory.database import get_connection
from core.conversation_adapter import JsonConversationStoreAdapter
from core.plugin_manager import PluginManager
from core.event_bus import EventBus
from core.trace_store import TraceStore
from core.goal_manager import GoalManager
from core import runtime as runtime_module
from agents import tool_manager
import scheduler as scheduler_module

_ROOT = Path(__file__).resolve().parent.parent
_GOALS_ROOT = _ROOT / "data" / "goals"


# ---------- memory/database.py：所有隔离机制的地基 ----------

def test_database_isolation_takes_effect():
    """隔离生效：DB_PATH 必须落在 helper 给的临时目录内"""
    current = os.path.abspath(db.DB_PATH)
    expected = os.path.abspath(_TMP_DIR)
    assert current.startswith(expected + os.sep), f"DB_PATH={current} 不在 {expected} 下"
    print("[OK] database：DB_PATH 已切到临时库")


def test_database_init_idempotent():
    """init_db 幂等：连续两次调用不报错，且表集合不变"""
    db.init_db()
    conn = get_connection()
    try:
        first = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    db.init_db()
    conn = get_connection()
    try:
        second = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert first == second or first <= second, "重复 init_db 不应丢失表"
    assert "goals" in second, "init_db 后应存在 goals 表"
    print(f"[OK] database：init_db 幂等（{len(second)} 张表）")


def test_database_get_db_and_connection_row_factory():
    """get_db / get_connection 返回可用连接（具备 row_factory 的字典式取值）"""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1, "连接应按列名取值"
    finally:
        conn.close()
    conn2 = get_connection()
    try:
        assert conn2.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn2.close()
    print("[OK] database：get_db / get_connection 可用")


def test_database_add_column_if_not_exists_idempotent():
    """_add_column_if_not_exists：重复调用不报错，列只加一次（25 处迁移的幂等保证）"""
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS _p47_probe (id INTEGER)")
        db._add_column_if_not_exists(conn, "_p47_probe", "probe_col", "TEXT")
        db._add_column_if_not_exists(conn, "_p47_probe", "probe_col", "TEXT")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(_p47_probe)")]
        assert cols.count("probe_col") == 1, cols
        conn.execute("DROP TABLE _p47_probe")
        conn.commit()
    finally:
        conn.close()
    print("[OK] database：_add_column_if_not_exists 幂等且不重复加列")


# ---------- core/runtime.py：有状态 + current_goal_id 全局 ----------

def test_runtime_goal_context_lifecycle():
    """current_goal_id 生命周期：初始 None → set → get → clear 后回 None"""
    runtime_module.clear_current_goal_id()
    assert runtime_module.get_current_goal_id() is None, "清理后应为 None"
    runtime_module.set_current_goal_id("goal_p47")
    assert runtime_module.get_current_goal_id() == "goal_p47"
    runtime_module.clear_current_goal_id()
    assert runtime_module.get_current_goal_id() is None, "clear 后应回到 None"
    print("[OK] runtime：current_goal_id 生命周期正确")


def test_runtime_goal_context_is_thread_local():
    """current_goal_id 是线程本地：子线程不继承、也不污染主线程"""
    runtime_module.clear_current_goal_id()
    runtime_module.set_current_goal_id("goal_main")
    seen = {}

    def worker():
        seen["inherited"] = runtime_module.get_current_goal_id()
        runtime_module.set_current_goal_id("goal_worker")
        seen["own"] = runtime_module.get_current_goal_id()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    try:
        assert seen["inherited"] is None, "子线程不应继承主线程的目标上下文"
        assert seen["own"] == "goal_worker"
        assert runtime_module.get_current_goal_id() == "goal_main", "主线程上下文被污染"
    finally:
        runtime_module.clear_current_goal_id()
    print("[OK] runtime：current_goal_id 线程本地隔离正确")


def _make_runtime():
    """构造一个最小可用 AgentRuntime（会话存储指向临时目录，避免污染真实会话）"""
    store = JsonConversationStoreAdapter(ChatStore(str(Path(_TMP_DIR) / "conversations")))
    return runtime_module.AgentRuntime(
        conversation_store=store,
        plugin_manager=PluginManager(),
        event_bus=EventBus(),
        trace_store=TraceStore(),
        goal_manager=GoalManager(),
    )


def test_runtime_create_and_get_state():
    """create_state 后 get_state 应返回同一状态对象（活动状态是内存态）"""
    runtime = _make_runtime()
    state = runtime.create_state("sess_p47_a", "通用助手", goal="测试目标")
    assert runtime.get_state("sess_p47_a") is state, "get_state 应返回同一对象"
    assert state.goal == "测试目标"
    print("[OK] runtime：create_state / get_state 状态身份一致")


def test_runtime_set_active_goal():
    """set_active_goal：已有状态则改 goal_id；无状态则自动建一个再设置"""
    runtime = _make_runtime()
    runtime.set_active_goal("sess_p47_b", "goal_p47_new")
    assert runtime.get_state("sess_p47_b").goal_id == "goal_p47_new"
    print("[OK] runtime：set_active_goal 写入会话目标")


# ---------- core/plugin_manager.py：加载失败静默 ----------

def test_plugin_manager_load_success():
    """加载两个内置插件目录后，注册表按键可取到实例"""
    pm = PluginManager()
    pm.load_plugin_from_dir(_ROOT / "plugins" / "modelscope_model")
    pm.load_plugin_from_dir(_ROOT / "plugins" / "local_tools")
    assert pm.get_plugin("modelscope_model") is not None
    assert pm.get_plugin("local_tools") is not None
    pm.get_plugin("local_tools").unload()
    print("[OK] plugin_manager：两个内置插件加载成功")


def test_plugin_manager_missing_dir_is_silent():
    """目录不存在（无 manifest.json）时不抛异常、不注册任何插件（加载失败静默）"""
    pm = PluginManager()
    result = pm.load_plugin_from_dir(_ROOT / "plugins" / "__p47_not_exist__")
    assert result is None, "加载失败应返回 None 而不是抛异常"
    assert pm.get_plugin("__p47_not_exist__") is None
    assert pm.get_plugins_by_type("tool") == [], "失败加载不应留下插件"
    print("[OK] plugin_manager：缺失目录静默返回，不抛异常")


def test_plugin_manager_get_by_type_uses_string_type():
    """按类型取插件用的是**字符串**（PluginInfo.type 刻意用 str，避免 Enum 序列化问题）"""
    pm = PluginManager()
    pm.load_plugin_from_dir(_ROOT / "plugins" / "local_tools")
    pm.load_plugin_from_dir(_ROOT / "plugins" / "modelscope_model")
    assert len(pm.get_plugins_by_type("tool")) == 1
    assert len(pm.get_plugins_by_type("model")) == 1
    assert pm.get_plugins_by_type("memory") == [], "未实现的类型应返回空列表"
    print("[OK] plugin_manager：按字符串类型检索正确")


# ---------- agents/tool_manager.py：P0-4 的目标目录注入 ----------

def test_tool_manager_schema_shape():
    """声明的工具 schema 形状：每项含 function.name / function.parameters"""
    for item in tool_manager.TOOLS_DEFINITIONS:
        assert item.get("type") == "function", item
        fn = item.get("function") or {}
        assert isinstance(fn.get("name"), str) and fn["name"], item
        assert isinstance(fn.get("parameters"), dict), item
    print(f"[OK] tool_manager：{len(tool_manager.TOOLS_DEFINITIONS)} 个工具 schema 形状合法")


def test_tool_manager_unknown_tool_is_friendly():
    """未知工具返回中文提示而不是抛异常"""
    out = tool_manager.ToolManager.execute_tool("__p47_no_such_tool__", {})
    assert isinstance(out, str) and "未知工具" in out, out
    print("[OK] tool_manager：未知工具返回友好提示")


def test_file_tools_contract_with_plugin():
    """文件类工具集合只有一份：tool_manager 与 local_tools 插件都指向共享定义

    收拢前两端各写一份、tool_manager 的注释只**承诺**"与插件一致"而无人强制；
    P4-7 首版把它变成"相等"断言，随后按用户决定（①c）收拢到 core/tool_injection，
    于是现在断言的是**身份**（is）而不是相等——即使有人再写一份内容相同的集合，
    本用例也会失败。
    """
    from core.tool_injection import FILE_TOOLS
    from plugins.local_tools.tool_plugin import LocalToolsPlugin
    from core.interfaces import PluginInfo
    plugin = LocalToolsPlugin()
    plugin.load(PluginInfo(name="local_tools", version="1.0.0", type="tool",
                           entry_point="tool_plugin.LocalToolsPlugin"))
    assert tool_manager.ToolManager._FILE_TOOLS is FILE_TOOLS, "tool_manager 未使用共享集合"
    assert plugin._file_tools is FILE_TOOLS, "local_tools 插件未使用共享集合"
    print("[OK] tool_manager：文件类工具集合为单一载体（3 个）")


def test_two_tool_entry_points_agree():
    """两条工具执行入口对同一入参给出一致结果（收拢后语义统一，且都不污染入参）

    ToolManager.execute_tool 是活路径；LocalToolsPlugin.execute 目前零调用方，
    但它必须实现 ToolPlugin 的抽象方法、因此会长期存在——本用例保证两者不会再次分叉
    （收拢前一个是拷贝注入、一个是原地写入）。

    注意：read_file 会 mkdir data/goals/{id}/files，故在 finally 清理并断言 data/goals 还原。
    """
    from plugins.local_tools.tool_plugin import LocalToolsPlugin
    from core.interfaces import PluginInfo
    before = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()
    gid = "goal_p47_two"
    try:
        plugin = LocalToolsPlugin()
        plugin.load(PluginInfo(name="local_tools", version="1.0.0", type="tool",
                               entry_point="tool_plugin.LocalToolsPlugin"))
        runtime_module.set_current_goal_id(gid)
        args_a = {"filename": "不存在.txt"}
        args_b = {"filename": "不存在.txt"}
        out_a = tool_manager.ToolManager.execute_tool("read_file", args_a)
        out_b = plugin.execute("read_file", args_b)
        assert out_a == out_b, f"两条入口结果分叉：\n 工具管理器={out_a}\n 插件入口={out_b}"
        assert "base_dir" not in args_a, "工具管理器路径污染了入参"
        assert "base_dir" not in args_b, "插件路径污染了入参（收拢前它会原地写入）"
    finally:
        runtime_module.clear_current_goal_id()
        shutil.rmtree(_GOALS_ROOT / gid, ignore_errors=True)
    after = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()
    assert after == before, f"data/goals 未还原：新增 {sorted(after - before)}"
    print("[OK] tool_manager：两条执行入口结果一致且均不污染入参")


def test_tool_manager_does_not_mutate_arguments():
    """P0-4 行为固化：tool_manager 路径用拷贝注入 base_dir，不污染调用方字典

    （收拢后两条路径共用同一实现，本用例与 test_two_tool_entry_points_agree 互补：
    后者比对两条入口的结果一致性，本用例单独钉住"入参零副作用"这条性质。）

    关于下面的「目录集合回到调用前」哨兵：P7 批次 5 起 read_file / list_files 是**只读
    路径、不再建目录**（原先 execute_tool 会经 read_file 在 data/goals/{id}/files 下
    mkdir），但这个哨兵仍然保留——它守的是"任何工具调用都不该在 data/goals 下留下残留
    目录"，与建不建目录无关。
    """
    before = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()
    gid = "goal_p47_mut"
    try:
        runtime_module.set_current_goal_id(gid)
        args = {"filename": "不存在.txt"}
        tool_manager.ToolManager.execute_tool("read_file", args)
        assert "base_dir" not in args, "调用方传入的参数字典被污染了"
    finally:
        runtime_module.clear_current_goal_id()
        shutil.rmtree(_GOALS_ROOT / gid, ignore_errors=True)
    after = {p.name for p in _GOALS_ROOT.iterdir()} if _GOALS_ROOT.is_dir() else set()
    assert after == before, f"data/goals 未还原：新增 {sorted(after - before)}"
    print("[OK] tool_manager：注入使用拷贝，未污染调用方参数且无残留目录")


def test_tool_manager_without_goal_uses_default_dir():
    """无目标上下文时回落到全局默认目录（不注入 base_dir）"""
    runtime_module.clear_current_goal_id()
    out = tool_manager.ToolManager.execute_tool("read_file", {"filename": "不存在.txt"})
    expected_dir = str(Path("data") / "files")
    assert expected_dir in out, f"应回落到默认目录，实际：{out}"
    print("[OK] tool_manager：无目标上下文时回落默认目录")


# ---------- scheduler.py：开机不再注册每日生成（时间点仍留在模块里） ----------

def test_scheduler_importable_without_starting():
    """import scheduler 不得启动调度循环（循环在 run_scheduler 内，不在模块级）"""
    assert callable(scheduler_module.run_scheduler)
    assert callable(scheduler_module.start_scheduler)
    assert callable(scheduler_module.generate_daily_plan)
    assert callable(scheduler_module.generate_daily_review)
    print("[OK] scheduler：模块导入不启动循环且四个入口可调用")


def test_daily_generation_not_registered_on_startup():
    """开机不再注册每日生成任务（2026-10-20 阶段二 第一批 第 7 步；spec §6.4 改判口径）

    原名 `test_scheduler_registers_two_daily_jobs`，旧断言是「调 start_scheduler 起线程后
    schedule.jobs 恰好 08:00／20:00」。那行调用已从 app.py 的 __main__ 删掉，旧断言现在
    必红，所以按 §6.4 把口径换成「不再注册每日生成任务」，两头一起钉：
      - 正向：app.py 的 __main__ 段里没有 start_scheduler 调用。用 ast 扫调用节点、
        不按文字扫——注释里写「那行调用已删」是本分，按文字匹配会把解释语当成调用、
        当场假红（AGENTS.md 执行纪律：规范写成「不许」与写成「没用」是两回事）。
      - 反向：scheduler.py 里那两行 08:00／20:00 注册仍在。第 7 步只删调用，模块与
        run_scheduler／start_scheduler 两个函数原样保留，这句要能在 diff 之外被证明。
    两条都只读源码，不起线程、不碰 schedule 的全局注册表（旧写法要 clear() 两次，
    那是往进程级全局上动手）。
    """
    app_src = (_ROOT / "app.py").read_text(encoding="utf-8")
    startup_calls = []
    for node in ast.parse(app_src).body:
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        left = node.test.left
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                label = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if label == "start_scheduler":
                    startup_calls.append(sub.lineno)
    assert startup_calls == [], (
        f"app.py 的 __main__ 里又出现 start_scheduler 调用（行 {startup_calls}）："
        "每日 08:00／20:00 定时生成被接回来了，spec §3.3 裁定 2（老版生成停用）破坏")
    scheduler_src = (_ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert 'at("08:00")' in scheduler_src and 'at("20:00")' in scheduler_src, (
        "scheduler.py 里那两个时间点不见了：第 7 步只删那一行调用，模块与函数要原样保留")
    print("[OK] scheduler：开机不再注册每日生成任务，scheduler.py 与两个时间点保留")


if __name__ == "__main__":
    print("=== skeleton test ===\n")
    test_database_isolation_takes_effect()
    test_database_init_idempotent()
    test_database_get_db_and_connection_row_factory()
    test_database_add_column_if_not_exists_idempotent()
    test_runtime_goal_context_lifecycle()
    test_runtime_goal_context_is_thread_local()
    test_runtime_create_and_get_state()
    test_runtime_set_active_goal()
    test_plugin_manager_load_success()
    test_plugin_manager_missing_dir_is_silent()
    test_plugin_manager_get_by_type_uses_string_type()
    test_tool_manager_schema_shape()
    test_tool_manager_unknown_tool_is_friendly()
    test_file_tools_contract_with_plugin()
    test_two_tool_entry_points_agree()
    test_tool_manager_does_not_mutate_arguments()
    test_tool_manager_without_goal_uses_default_dir()
    test_scheduler_importable_without_starting()
    test_daily_generation_not_registered_on_startup()
    print("\n=== ALL SKELETON TESTS PASSED ===")
