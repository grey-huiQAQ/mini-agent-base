# -*- coding: utf-8 -*-
"""ui.controllers._shared —— 跨控制器共享的运行时上下文（Phase 3b，2026-09-12）

原 app.py「全局组件初始化」段整体迁入（保持对象身份唯一，供各控制器绑定、
UI 经 app 薄壳间接引用、测试注入/patch）：init_db() → 模块级单例 → 插件加载
→ 事件处理器注册 → AgentRuntime 构建。

2026-09-12 收敛说明：随迁入的 extract_json_object / parse_learning_json 两个函数
已删除。核实结论为零调用方（app.py 与各控制器均未导入），且该副本缺少
import re / import json，一旦被调用即 NameError；JSON 提取的唯一权威实现
统一为 core/json_utils。
"""
from memory.database import init_db
from memory.chat_store import ChatStore
from core.conversation_adapter import JsonConversationStoreAdapter
from core.plugin_manager import PluginManager
from core.event_bus import EventBus
from core.trace_store import TraceStore
from core.goal_manager import GoalManager
from core.information_engine.search_task import SearchTaskManager
from core.resource_scout import ResourceScout
from core.runtime import AgentRuntime
from core.event_handlers import register_event_handlers

# ---------- 全局组件初始化（原 app.py，逐字迁入） ----------
init_db()
chat_store = ChatStore("data/conversations")
conversation_store = JsonConversationStoreAdapter(chat_store)
plugin_manager = PluginManager()
event_bus = EventBus()
trace_store = TraceStore()
goal_manager = GoalManager()

# 深度搜索任务管理器（默认搜索源：DuckDuckGo + GitHub；后台异步执行，最大 3 并发）
search_task_manager = SearchTaskManager()

# Resource Scout 扫描器（默认搜索源：DuckDuckGo + GitHub；同步调用带超时保护）
resource_scout = ResourceScout()
# 后台扫描状态（扫描在工作线程执行；完成后写 report，UI 刷新读取展示）
opportunity_scan_state = {"running": False, "report": None, "error": None}
# AI 深度筛选后台状态（v0.4：手动按钮触发，工作线程执行，不阻塞界面）
ai_filter_state = {"running": False, "report": None, "error": None}

# 加载插件（drop-in：扫描 plugins/ 下所有含 manifest.json 的子目录，与 skills/ 同一种用法；
# 此前是硬编码两个目录的路径，等于"加插件必须改代码"）
plugin_manager.load_all_plugins("plugins")

# 注册事件处理器
register_event_handlers(event_bus)

# 创建运行时
runtime = AgentRuntime(
    conversation_store,
    plugin_manager,
    event_bus,
    trace_store,
    goal_manager=goal_manager,
)
