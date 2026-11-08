from core.interfaces import ToolPlugin, PluginInfo
from core.tool_injection import FILE_TOOLS, apply_goal_context
from tools.local_tools import read_file, list_files, write_file, get_current_time, web_search, rag_search
from agents.tool_manager import TOOLS_DEFINITIONS  # 复用现有 schema


class LocalToolsPlugin(ToolPlugin):
    def load(self, plugin_info: PluginInfo):
        self._info = plugin_info
        self._tool_map = {
            "read_file": read_file,
            "list_files": list_files,
            "write_file": write_file,
            "get_current_time": get_current_time,
            "web_search": web_search,
            "rag_search": rag_search,
        }
        # 文件类工具集合与注入逻辑统一由 core.tool_injection 提供（与 ToolManager 同一份）
        self._file_tools = FILE_TOOLS
        print(f"[ToolPlugin] 加载 {plugin_info.name}")

    def unload(self):
        pass

    def get_tools(self):
        return TOOLS_DEFINITIONS

    def execute(self, tool_name: str, arguments: dict) -> str:
        func = self._tool_map.get(tool_name)
        if not func:
            return f"未知工具: {tool_name}"
        try:
            # 目标上下文注入走与 ToolManager.execute_tool 同一实现（拷贝语义，不原地改写入参）
            args = apply_goal_context(tool_name, arguments)
            return str(func(**args))
        except Exception as e:
            return f"工具执行失败: {e}"
