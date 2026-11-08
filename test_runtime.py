from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.trace_store import TraceStore
from core.runtime import AgentRuntime
from core.conversation_adapter import JsonConversationStoreAdapter
from memory.chat_store import ChatStore
from memory.database import init_db
from pathlib import Path

def main():
    # 初始化数据库
    init_db()

    # 初始化组件
    chat_store = ChatStore("data/conversations")
    conversation_store = JsonConversationStoreAdapter(chat_store)
    plugin_manager = PluginManager()
    event_bus = EventBus()
    trace_store = TraceStore()

    # 加载插件
    plugin_manager.load_plugin_from_dir(Path("plugins/modelscope_model"))
    plugin_manager.load_plugin_from_dir(Path("plugins/local_tools"))

    # 创建 Runtime
    runtime = AgentRuntime(conversation_store, plugin_manager, event_bus, trace_store)

    # 创建会话
    session_id = conversation_store.create_session("test")

    # 发送消息
    print("Assistant: ", end="", flush=True)
    for chunk in runtime.run(session_id, "你好，请介绍一下你自己", temperature=0.7, max_tokens=500):
        print(chunk, end="", flush=True)
    print()

if __name__ == "__main__":
    main()