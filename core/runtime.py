"""
Agent Runtime - 核心运行时
"""
import threading
from typing import Generator, Optional, Dict, Any, List
from datetime import datetime
from .state import AgentState, AgentStatus
from .event_bus import EventBus, Event
from .plugin_manager import PluginManager
from .interfaces import ConversationStore, ModelPlugin, ToolPlugin
from .trace_store import TraceStore
from .state_store import StateStore
from .context_manager import ContextManager
from .skill_manager import SkillManager

from agents.supervisor import supervisor_decision
from prompts.role_prompts import get_prompt, get_role_tools
from memory.summarizer import compress_history

# 线程本地存储，用于在工具调用间传递当前目标 ID
_goal_context = threading.local()


def set_current_goal_id(goal_id: str):
    """设置当前线程的目标 ID"""
    _goal_context.goal_id = goal_id


def get_current_goal_id() -> Optional[str]:
    """获取当前线程的目标 ID"""
    return getattr(_goal_context, 'goal_id', None)


def clear_current_goal_id():
    """清除当前线程的目标 ID"""
    if hasattr(_goal_context, 'goal_id'):
        del _goal_context.goal_id


# 线程本地存储，用于在工具调用间传递当前角色名（P7 批次 6-③：工具白名单执法）。
# 与目标那份**分开存**，不共用 _goal_context：两者生命周期不同（目标是会话绑定的目标，
# 角色是本轮对话选中的角色），合成一个 local 只会让"清目标"顺手把角色也清掉。
_role_context = threading.local()


def set_current_role(role: str):
    """设置当前线程的角色名（供 agents.tool_manager 的工具白名单执法读取）"""
    _role_context.role = role


def get_current_role() -> Optional[str]:
    """获取当前线程的角色名；从未设置过时返回 None

    None 这一支有语义：`execute_tool` 只在**设了角色**时才执法（fail-open），
    详见 agents/tool_manager.ToolManager._role_denial_reason 的 docstring。
    """
    return getattr(_role_context, 'role', None)


def clear_current_role():
    """清除当前线程的角色名"""
    if hasattr(_role_context, 'role'):
        del _role_context.role


class AgentRuntime:
    def __init__(
        self,
        conversation_store: ConversationStore,
        plugin_manager: PluginManager,
        event_bus: EventBus,
        trace_store: TraceStore,
        state_store: StateStore = None,
        context_manager: ContextManager = None,
        goal_manager=None,
    ):
        self.conversation_store = conversation_store
        self.plugin_manager = plugin_manager
        self.event_bus = event_bus
        self.trace_store = trace_store
        self.state_store = state_store or StateStore()
        self.context_manager = context_manager or ContextManager(goal_manager=goal_manager)
        self.goal_manager = goal_manager
        self.skill_manager = SkillManager()
        self._active_states: Dict[str, AgentState] = {}
        # 加载所有技能
        self.skill_manager.load_all_skills()

    def create_state(self, session_id: str, agent_type: str, goal: str = "") -> AgentState:
        # 尝试从存储加载已有状态
        existing = self.state_store.load_by_session(session_id)
        if existing:
            self._active_states[session_id] = existing
            return existing

        state = AgentState(
            agent_id=f"{session_id}:{agent_type}:{len(self._active_states)}",
            session_id=session_id,
            agent_type=agent_type,
            goal=goal,
        )
        self._active_states[session_id] = state
        self.state_store.save(state)
        self.event_bus.publish(Event("agent_started", {"session_id": session_id, "agent_type": agent_type}))
        return state

    def get_state(self, session_id: str) -> Optional[AgentState]:
        return self._active_states.get(session_id)

    def set_active_goal(self, session_id: str, goal_id: str):
        """设置会话的当前目标"""
        state = self.get_state(session_id)
        if state:
            state.goal_id = goal_id
            self.state_store.save(state)
        else:
            state = self.create_state(session_id, "learning")
            state.goal_id = goal_id
            self.state_store.save(state)

    def run(self, session_id: str, user_input: str, save_user_message: bool = True, **kwargs) -> Generator[str, None, None]:
        if save_user_message:
            self.conversation_store.save_message(session_id, "user", user_input)
        yield from self.process_user_input(session_id, user_input, **kwargs)

    def process_user_input(self, session_id: str, user_input: str, **kwargs) -> Generator[str, None, None]:
        state = self.get_state(session_id)
        if state is None:
            state = self.create_state(session_id, "learning")

        state.status = AgentStatus.RUNNING
        state.updated_at = datetime.now()
        self.state_store.save(state)
        self.event_bus.publish(Event("agent_started", {"session_id": session_id, "agent_type": state.agent_type}))
        self.trace_store.record(session_id, "state_change", {"status": "running", "agent_type": state.agent_type}, agent_state_id=state.agent_id)

        # 设置当前目标 ID（供工具插件使用，实现目标专属文件目录）
        set_current_goal_id(state.goal_id)

        try:
            # 获取历史消息（适当多取一些，以便压缩后有足够上下文）
            raw_history = self.conversation_store.get_messages(session_id, limit=50)

            # Supervisor 决策
            decision = supervisor_decision(user_input)
            selected_agent = decision["agent"][0]
            print(f"[Runtime] Supervisor 选择: {selected_agent}")

            # 工具白名单执法（P7 批次 6-③）：上面那行只把**发给模型的 schema** 按角色
            # 过滤掉，真正执行工具的 agents.tool_manager.execute_tool 不做角色校验。这里把
            # 本轮角色放进线程本地，由 execute_tool 在调用前判"这个工具名在不在该角色白名单
            # 里"。设置点必须在调模型之前（工具调用发生在模型流式生成期间）。
            # 与既有的 set_current_goal_id 各管各的线程本地量，互不覆盖。
            set_current_role(selected_agent)

            # 获取基础系统提示词
            base_prompt = get_prompt(selected_agent)

            # 根据任务类型设置 detail_level
            task_type = decision.get("task", "闲聊")
            if task_type in ["闲聊", "搜索"]:
                detail_level = "low"
            elif task_type in ["学习"]:
                detail_level = "high"
            else:
                detail_level = "medium"

            # 构建系统提示词，传入当前目标 ID 和用户输入（用于动态记忆检索）
            system_prompt = self.context_manager.build_system_prompt(
                selected_agent, base_prompt,
                detail_level=detail_level,
                goal_id=state.goal_id,
                user_input=user_input
            )

            # 自动压缩历史
            keep_recent = 6
            max_total = 14
            if len(raw_history) > max_total:
                compressed = compress_history(raw_history, keep_recent=keep_recent, max_total=max_total)
                history_for_model = compressed
                print(f"[Runtime] 历史消息 {len(raw_history)} 条，已压缩为 {len(compressed)} 条")
            else:
                history_for_model = raw_history

            # 构建消息列表
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(history_for_model)

            # 获取工具插件，并根据角色权限过滤
            tool_plugins = self.plugin_manager.get_plugins_by_type("tool")
            all_tools = []
            for plugin in tool_plugins:
                all_tools.extend(plugin.get_tools())

            # 根据角色工具权限过滤工具
            allowed_tool_names = get_role_tools(selected_agent)
            if allowed_tool_names:
                tools = [t for t in all_tools if t.get("function", {}).get("name") in allowed_tool_names]
            else:
                tools = []

            # 获取模型插件
            model_plugins = self.plugin_manager.get_plugins_by_type("model")
            if not model_plugins:
                yield "错误：未加载模型插件"
                return
            model_plugin = model_plugins[0]

            # 提取模型参数
            temperature = kwargs.get('temperature', 0.7)
            max_tokens = kwargs.get('max_tokens', 2048)
            timeout = kwargs.get('timeout', 60)

            # 调用模型流式生成
            full_response = ""
            if tools:
                gen = model_plugin.stream_chat_with_tools(
                    messages, tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout
                )
            else:
                gen = model_plugin.stream_chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout
                )

            for chunk in gen:
                full_response = chunk
                yield chunk

            # 保存助手回复
            self.conversation_store.save_message(session_id, "assistant", full_response)

            # 发布事件
            domain = self.context_manager.domain_map.get(selected_agent, "通用")
            self.event_bus.publish(Event("assistant_message_completed", {
                "session_id": session_id,
                "message": full_response,
                "agent_type": selected_agent,
                "domain": domain,
                "user_message": user_input,
            }))

        finally:
            # 清除当前目标 ID 与角色，避免污染其他线程/请求
            # （角色是 6-③ 的工具白名单执法开关：设了不清会把 fail-closed 全拒留在
            #   线程池的线程上，任何在 process_user_input 之外调 execute_tool 的路径
            #   都会静默翻到 enforcement；goal 那一对本来就是"设 + finally 清"）
            clear_current_goal_id()
            clear_current_role()

        # 更新状态
        state.status = AgentStatus.IDLE
        state.updated_at = datetime.now()
        self.state_store.save(state)
        self.trace_store.record(session_id, "state_change", {"status": "idle"}, agent_state_id=state.agent_id)
        self.event_bus.publish(Event("agent_stopped", {"session_id": session_id, "status": "idle"}))
