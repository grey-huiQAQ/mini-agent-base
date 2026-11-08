# -*- coding: utf-8 -*-
"""数据契约测试（P4-5f）—— 只断言"字符串约定的两端确实对上"，不测行为

背景（为什么需要它）：
    六个静态检查器 + 五本台账防的都是**结构漂移**（重名 / 动态命名空间 / 注释日期 /
    DB 隔离 / 层序 / 台账条数）。但本项目真正咬过人的两个 bug 都是**语义漂移**：
    ① roles.json 里读错键名（误读 "system_prompt" 导致四段角色人设恒为空、
       从未随消息发给模型，P0-2 回归）；
    ② rag/document_loader.load_and_split 与 rag/note_indexer._chunk_text 同算法异名、
       行为分叉（P4-6）。
    这两种情况下所有检查器都是全绿的——因为没有任何检查器在看"数据契约"。

覆盖（六条断言，两端分别是"文件里的字符串"与"代码里的符号"）：
- roles.json：每个角色必有 "prompt" 与 "tools"，且不得残留 "system_prompt"
- skills/*/manifest.json：字段集不得超出 SkillManifest 的签名参数（防键名拼错），
  且必填字段齐备
- PluginManager：加载 plugins/ 下两个插件目录后，注册表含 local_tools 与 modelscope_model
- agents/tool_manager：TOOLS_DEFINITIONS 里声明的工具名集合 == TOOL_MAP 的键集合
  （声明给 LLM 的工具与本地可执行函数必须一一对应，多一个会调用失败、少一个白声明）
- 数据库表名：goals / goal_tasks / milestones / notes / experiences / test_runs 均在
  sqlite_master 中
"""
import json
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db

_TMP_DIR = setup_test_db()

from core.skill_base import SkillManifest
from core.plugin_manager import PluginManager
from agents import tool_manager
from memory.database import init_db, get_connection

_ROOT = Path(__file__).resolve().parent.parent


def test_roles_json_contract():
    """roles.json：prompt/tools 键名契约（P0-2 回归），且不得残留 system_prompt"""
    roles_file = _ROOT / "prompts" / "roles.json"
    roles = json.loads(roles_file.read_text(encoding="utf-8"))
    assert roles, "roles.json 不应为空"
    for role, cfg in roles.items():
        assert isinstance(cfg, dict), f"{role} 配置应为对象"
        assert "prompt" in cfg, f"{role} 缺少 prompt 键（曾因读 system_prompt 导致人设恒空）"
        assert "tools" in cfg, f"{role} 缺少 tools 键"
        assert isinstance(cfg["prompt"], str) and cfg["prompt"].strip(), \
            f"{role} 的 prompt 应为非空字符串"
        assert isinstance(cfg["tools"], list), f"{role} 的 tools 应为列表"
        assert "system_prompt" not in cfg, \
            f"{role} 残留了 system_prompt 键——这正是 P0-2 那个 bug 的形态"
    print(f"[OK] roles.json 契约：{len(roles)} 个角色均为 prompt/tools 键")


def test_skill_manifest_contract():
    """skills/*/manifest.json 的键名必须落在 SkillManifest 的字段集内"""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(SkillManifest)}
    required = {"name", "display_name", "description"}
    manifests = sorted((_ROOT / "skills").glob("*/manifest.json"))
    assert manifests, "未发现任何 skills/*/manifest.json"
    for mf in manifests:
        data = json.loads(mf.read_text(encoding="utf-8"))
        unknown = set(data) - fields
        assert not unknown, f"{mf.parent.name}/manifest.json 出现未知字段 {sorted(unknown)}（拼错键名会静默失效）"
        missing = required - set(data)
        assert not missing, f"{mf.parent.name}/manifest.json 缺少必填字段 {sorted(missing)}"
    print(f"[OK] skill manifest 契约：{len(manifests)} 个 manifest 字段名均合法")


def test_plugins_load_contract():
    """PluginManager 能加载两个内置插件，且注册名与目录约定一致"""
    pm = PluginManager()
    pm.load_plugin_from_dir(_ROOT / "plugins" / "modelscope_model")
    pm.load_plugin_from_dir(_ROOT / "plugins" / "local_tools")
    for expected in ("modelscope_model", "local_tools"):
        assert pm.get_plugin(expected) is not None, \
            f"插件 {expected} 未被注册（加载器的注册名约定与目录名脱节）"
    print("[OK] plugin 加载契约：modelscope_model / local_tools 均已注册")


def test_tool_schema_matches_tool_map():
    """声明给 LLM 的工具名集合必须与本地可执行函数表一一对应"""
    declared = {t["function"]["name"] for t in tool_manager.TOOLS_DEFINITIONS}
    implemented = set(tool_manager.TOOL_MAP)
    assert declared == implemented, (
        f"声明与实现不一致：仅声明 {sorted(declared - implemented)}；"
        f"仅实现 {sorted(implemented - declared)}"
    )
    print(f"[OK] 工具契约：{len(declared)} 个工具声明与 TOOL_MAP 完全一致")


def test_db_tables_contract():
    """init_db() 之后，被点名依赖的表必须都存在"""
    init_db()
    conn = get_connection()
    try:
        rows = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    expected = {"goals", "goal_tasks", "milestones", "notes",
                "experiences", "test_runs"}
    missing = expected - rows
    assert not missing, f"缺少表 {sorted(missing)}（建表语句与实际使用脱节）"
    print(f"[OK] 表名契约：{len(expected)} 张点名表均存在")


if __name__ == "__main__":
    print("=== contracts test ===\n")
    test_roles_json_contract()
    test_skill_manifest_contract()
    test_plugins_load_contract()
    test_tool_schema_matches_tool_map()
    test_db_tables_contract()
    print("\n=== ALL CONTRACT TESTS PASSED ===")
