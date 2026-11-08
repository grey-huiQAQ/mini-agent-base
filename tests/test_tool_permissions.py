# -*- coding: utf-8 -*-
"""工具名白名单执法（P7 批次 6-③）

缺口：core/runtime 的角色白名单只用来**过滤发给模型的 schema**，真正执行工具的
agents.tool_manager.execute_tool 不做角色校验。本文件钉住新加的执法，覆盖四格：

  ① 未设角色 → 不执法（fail-open），write_file 正常写入（旧行为不变）；
  ② 设一个白名单里没有 write_file 的角色 → 拒绝执行、文件未创建、临时库里恰好一条
     sandbox / tool_not_allowed，且 detail 带角色名与工具名；
  ③ 设一个白名单含 write_file 的角色 → 正常写入，库里无 sandbox 留痕；
  ④ 设一个 roles.json 里不存在的角色名 → 连 get_current_time 都被拒（fail-closed）。

DB 隔离（必须）：拒绝留痕会往 agent_resource_log 写一行，故每个用例都把
`memory.database.DB_PATH` patch 到本进程临时目录下的独立库（P6-6④ 认可的字符串靶子
写法）；不做这步，生产库 sha256 哨兵必红——那正是它该有的反应。本文件不碰 memory.db
以外的东西。
"""
import contextlib
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROD_DB = Path("data") / "memory.db"


@contextlib.contextmanager
def _isolated_log_db():
    """把 DB_PATH 指向临时库（留痕写这里），yield 该库路径

    临时库放 data/.tmp_tests（.gitignore 已忽略），退出时删除。
    """
    tmp = Path("data") / ".tmp_tests"
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / f"perm_{uuid.uuid4().hex[:8]}.db"
    try:
        with patch("memory.database.DB_PATH", str(db)):
            yield db
    finally:
        for _ in range(6):
            try:
                db.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.2)


def _prod_digest():
    """生产库 sha256（库不存在时返回 None）"""
    return hashlib.sha256(_PROD_DB.read_bytes()).hexdigest() if _PROD_DB.exists() else None


def _sandbox_rows(db) -> list:
    """临时库里的 sandbox 留痕（最新在前）"""
    from memory.resource_log import get_recent_resource_logs
    return [r for r in get_recent_resource_logs(limit=50, db_path=str(db))
            if r["resource_type"] == "sandbox"]


def _roles_by_write_permission():
    """按 roles.json 现况挑出「含 write_file」与「不含 write_file」各一个角色

    不硬编码角色名：roles.json 改了名字也不该让本用例变成"测的是过期约定"。
    """
    from prompts.role_prompts import get_all_roles, get_role_tools
    writable = no_write = None
    for role in get_all_roles():
        if "write_file" in get_role_tools(role):
            writable = writable or role
        else:
            no_write = no_write or role
    assert writable and no_write, \
        "roles.json 里必须同时存在含/不含 write_file 的角色，否则四格测不全"
    return writable, no_write


def test_no_role_means_no_enforcement():
    """① 未设角色 → 不执法（fail-open）：write_file 正常写入，且不产生留痕"""
    from agents.tool_manager import ToolManager
    from core.runtime import clear_current_role, get_current_role
    from tools.local_tools import DEFAULT_FILES_ROOT

    name = f"_p7b6_perm_norole_{uuid.uuid4().hex[:8]}.txt"
    clear_current_role()
    try:
        assert get_current_role() is None, "本用例要求线程里没有角色"
        with _isolated_log_db() as db:
            msg = ToolManager.execute_tool(
                "write_file", {"filename": name, "content": "无角色时的写入"})
            assert "已写入" in msg, f"没设角色就不该执法：{msg}"
            assert (DEFAULT_FILES_ROOT / name).exists(), "无角色时文件应正常写入"
            assert _sandbox_rows(db) == [], "放行的调用不该留 tool_not_allowed 痕迹"
    finally:
        (DEFAULT_FILES_ROOT / name).unlink(missing_ok=True)
        clear_current_role()
        assert get_current_role() is None, "用例退出前必须把角色清干净"
    print("[OK] 未设角色时不执法")


def test_role_without_write_file_is_denied_and_traced():
    """② 角色白名单里没有 write_file → 拒绝、文件不存在、临时库恰好一条留痕

    本用例同时是「**拒绝先于 apply_goal_context**」的顺序断言（P7 批次 7-②）：那次调用
    自带 base_dir，而本用例没有目标上下文 ⇒ base_dir 一定会被 apply_goal_context 丢弃并
    打印 `[沙箱]`、记一条 base_dir_ignored。所以只要拒绝晚于注入，就会同时多出一条
    base_dir_ignored 痕迹（库里的 sandbox 行数会从 1 变 2）。断言的是"没有第二条"，
    因此不存在"因为没触发所以空过"。
    """
    import contextlib
    import io

    from agents.tool_manager import ToolManager
    from core.runtime import clear_current_role, set_current_role
    from tools.local_tools import DEFAULT_FILES_ROOT

    _, no_write = _roles_by_write_permission()
    name = f"_p7b6_perm_denied_{uuid.uuid4().hex[:8]}.txt"
    digest_before = _prod_digest()
    try:
        set_current_role(no_write)
        with _isolated_log_db() as db:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                msg = ToolManager.execute_tool(
                    "write_file",
                    {"filename": name, "content": "不该被写进去",
                     "base_dir": str(DEFAULT_FILES_ROOT)})
            out = buf.getvalue()
            assert "拒绝" in msg, f"白名单外的工具应被拒：{msg}"
            assert no_write in msg and "write_file" in msg, \
                f"拒绝文案要能看出原因（角色名 + 工具名）：{msg}"
            assert not (DEFAULT_FILES_ROOT / name).exists(), "被拒的调用绝不该真的写文件"
            # 被拒的调用连入参处理都不该做
            assert "[沙箱]" not in out, \
                f"拒绝必须早于 apply_goal_context，否则会打出 [沙箱] 告警：{out!r}"

            rows = _sandbox_rows(db)
            assert len(rows) == 1, \
                f"拒绝应恰好留一条 sandbox 记录（多出 base_dir_ignored 即顺序反了）：{rows}"
            assert rows[0]["operation"] == "tool_not_allowed", rows
            assert no_write in rows[0]["detail"] and "write_file" in rows[0]["detail"], \
                f"留痕 detail 应带角色名与工具名：{rows[0]['detail']!r}"
    finally:
        (DEFAULT_FILES_ROOT / name).unlink(missing_ok=True)
        clear_current_role()
    if digest_before is not None:
        assert _prod_digest() == digest_before, \
            "生产库 data/memory.db 被改动了：留痕本该写进临时库"
    print(f"[OK] 白名单外的工具被拒并留痕（角色：{no_write}）")


def test_role_with_write_file_is_allowed():
    """③ 角色白名单含 write_file → 放行、内容真的落盘、库里无留痕"""
    from agents.tool_manager import ToolManager
    from core.runtime import clear_current_role, set_current_role
    from tools.local_tools import DEFAULT_FILES_ROOT

    writable, _ = _roles_by_write_permission()
    name = f"_p7b6_perm_allowed_{uuid.uuid4().hex[:8]}.txt"
    try:
        set_current_role(writable)
        with _isolated_log_db() as db:
            msg = ToolManager.execute_tool(
                "write_file", {"filename": name, "content": "白名单内允许的内容"})
            assert "已写入" in msg, f"白名单内的工具应放行：{msg}"
            assert (DEFAULT_FILES_ROOT / name).read_text(encoding="utf-8") == "白名单内允许的内容"
            assert _sandbox_rows(db) == [], "放行的调用不该留 tool_not_allowed 痕迹"
    finally:
        (DEFAULT_FILES_ROOT / name).unlink(missing_ok=True)
        clear_current_role()
    print(f"[OK] 白名单内的工具被放行（角色：{writable}）")


def test_unknown_role_denies_everything():
    """④ 设了 roles.json 里没有的角色名 → 连 get_current_time 都被拒（fail-closed）"""
    from agents.tool_manager import ToolManager
    from core.runtime import clear_current_role, set_current_role

    unknown = f"_p7b6_no_such_role_{uuid.uuid4().hex[:6]}"
    digest_before = _prod_digest()
    try:
        set_current_role(unknown)
        with _isolated_log_db() as db:
            msg = ToolManager.execute_tool("get_current_time", {})
            assert "拒绝" in msg, f"未知角色的白名单为空，应全部拒绝：{msg}"
            assert unknown in msg, f"拒绝文案应带角色名：{msg}"

            rows = _sandbox_rows(db)
            assert len(rows) == 1 and rows[0]["operation"] == "tool_not_allowed", rows
            assert unknown in rows[0]["detail"], rows[0]["detail"]
    finally:
        clear_current_role()
    if digest_before is not None:
        assert _prod_digest() == digest_before, \
            "生产库 data/memory.db 被改动了：留痕本该写进临时库"
    print("[OK] 未知角色全拒（fail-closed）")


def test_runtime_clears_role_in_finally():
    """接线断言：process_user_input 的 finally 里同时清了目标 ID 与角色

    **这是接线断言，不是行为断言**：它只证明"清这两句被写在同一个 finally 里"，
    不证明"清在对的时机"（时机对不对要看那两句是否真在 finally —— 本断言正是查这个
    语法位置）。为什么要用 ast 而不是跑一遍 AgentRuntime：跑真 runtime 要造会话、
    插件、模型桩，成本远高于它证明的东西；而这里要防的坏形态很具体——
    "设了角色却从没清过"（6-③ 就是这样进来的：clear_current_role 只有测试在调）。

    两个都要断言，不能只钉角色：将来有人把 goal 那一句删了，只钉角色的用例仍会绿。
    """
    import ast

    src = Path(__file__).resolve().parent.parent / "core" / "runtime.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "process_user_input":
            target = node
            break
    assert target is not None, "core/runtime.py 里找不到 AgentRuntime.process_user_input"

    called_per_finally = []
    for inner in ast.walk(target):
        if not isinstance(inner, ast.Try) or not inner.finalbody:
            continue
        called = set()
        for stmt in inner.finalbody:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    called.add(sub.func.id)
        called_per_finally.append(called)
    assert called_per_finally, "process_user_input 里找不到任何带 finalbody 的 Try"
    assert any({"clear_current_goal_id", "clear_current_role"} <= c
               for c in called_per_finally), (
        "process_user_input 的**同一个** finally 里必须同时清目标 ID 与角色，"
        f"实际各 finally 清的是：{called_per_finally}")
    print("[OK] runtime 在同一个 finally 里清了角色（接线断言）")


if __name__ == "__main__":
    print("=== tool permissions test ===\n")
    test_no_role_means_no_enforcement()
    test_role_without_write_file_is_denied_and_traced()
    test_role_with_write_file_is_allowed()
    test_unknown_role_denies_everything()
    test_runtime_clears_role_in_finally()
    print("\n=== ALL TESTS PASSED ===")
