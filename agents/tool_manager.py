"""
工具管理器
- 定义工具 Schema（供 Function Call）
- 执行工具调用
"""

from tools.local_tools import read_file, list_files, write_file, get_current_time, web_search, rag_search
from core.tool_injection import FILE_TOOLS, apply_goal_context
from prompts.role_prompts import get_role_tools
import json

# ---------- 工具 Schema 定义（OpenAI Function Call 格式） ----------
TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取你的专属目录与只读「公共书架」里的文件内容。当用户询问某个文件的内容、笔记、文档时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要读取的文件名，例如 'notes.txt'、'python_basics.md'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大读取字符数，默认 3000",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出你可读的全部文件（你的专属目录 ＋ 只读「公共书架」）。当用户询问有哪些文件、资料时可调用。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间。当用户询问现在几点、今天日期时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
        {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "当需要获取最新信息、实时数据或查找资料时，使用此工具进行网络搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，例如 'Python 3.12 新特性'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认3条",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "从本地知识库（只读「公共书架」的文档 ＋ 你的笔记）检索与问题相关的内容。当用户询问已上传的资料、笔记或私有知识时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词或问题",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回文档数量，默认3",
                    },
                },
              "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件到指定目录（当前目标专属目录或默认 data/files）。当需要保存笔记、总结、报告等内容时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名，例如 'notes.txt'、'summary.md'",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文本内容",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
]

# 工具名 → 函数映射
TOOL_MAP = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "get_current_time": get_current_time,
    "web_search": web_search,
    "rag_search": rag_search,
}


class ToolManager:
    """工具调用管理器"""

    # 文件类工具集合与注入逻辑统一由 core/tool_injection 提供（单一载体，2026-09-12 收拢）。
    # 保留 _FILE_TOOLS 这个名字仅为兼容既有引用（测试与内部判断），它指向同一个 frozenset。
    _FILE_TOOLS = FILE_TOOLS

    @staticmethod
    def _role_denial_reason(tool_name: str) -> str:
        """判工具名是否在当前角色的白名单里：返回空串＝放行，非空＝拒绝理由

        为什么执法落在本处（P7 批次 6-③）：core/runtime 的角色白名单只用来**过滤发给
        模型的 schema**，而真正执行工具的是本类的 execute_tool——模型凭历史或幻觉调用
        一个白名单外的工具名时，此前没有任何机制拦得住，系统的保证只是"模型一开始没看到
        那个 schema"。这与批次 5 的 base_dir 完全同形（承诺只写在别处的过滤上）。

        两种情形必须分开，一条 fail-open、一条 fail-closed：

          - **没设角色**（`core.runtime.get_current_role()` 返回 None）→ **不执法、放行**。
            这是刚需：仓内既有用例与任何非对话路径都直接调 execute_tool，没有角色上下文。
          - **设了角色** → 按 `get_role_tools(role)` 判这个名字在不在册：不在册就拒绝
            （绝不调用 func），理由里带上角色名与工具名；在册则放行。
          - 设了角色但这个名字不认识 ⇒ 白名单为空 ⇒ **全部拒绝**（fail-closed）。这与上一
            条不矛盾："没设角色"＝不执法（测试 / 非对话路径）；"设了角色但配置里没有它"＝
            配置错误，出声拒绝，不要安静地当成没设。

        拒绝时另往 `agent_resource_log` 记一条（resource_type=sandbox、
        operation=tool_not_allowed，detail 带角色名与工具名），埋点规格照批次 5b：
        延迟导入 + 整体 try/except，失败不影响本次调用。

        导入说明：`get_current_role` 必须在函数内延迟导入，理由与 core/tool_injection.py
        记的完全相同——core.runtime 在模块级导入 agents.supervisor 等，而本模块会被很早
        导入，模块级导入会形成循环。该导入刻意**不包 try/except**（与 tool_injection 里
        同一处导入的写法一致）：静默降级会让执法"看起来在、其实没跑"，那正是本批要消除的
        形态。`prompts.role_prompts` 则在模块顶层导入（agents=L2 > prompts=L1，层序合法）。
        """
        from core.runtime import get_current_role

        role = get_current_role()
        if not role:
            return ""
        if tool_name in get_role_tools(role):
            return ""

        reason = f"❌ 工具 {tool_name} 不在角色「{role}」的工具白名单里，已拒绝执行。"
        try:
            from memory.resource_log import log_resource_usage
            log_resource_usage("sandbox", 0, "tool_not_allowed",
                               f"角色 {role} 调用了白名单外的工具 {tool_name}")
        except Exception:
            pass
        return reason

    @staticmethod
    def execute_tool(tool_name: str, arguments: dict) -> str:
        """
        执行工具调用

        文件类工具（read_file / list_files / write_file）在「目标上下文」下自动注入
        base_dir 指向当前激活目标的专属文件目录 data/goals/{goal_id}/files，使各目标
        的文件互相隔离；无目标上下文（core.runtime 未设置 current_goal_id）时保持
        默认行为（回落到全局 data/files 目录）。

        注入规则自 2026-09-12 起由 core.tool_injection.apply_goal_context 单一承担
        （本函数与 plugins/local_tools 的插件入口共用同一实现，语义均为"拷贝注入"）。

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果字符串
        """
        func = TOOL_MAP.get(tool_name)
        if not func:
            return f"❌ 未知工具：{tool_name}"

        # 角色工具白名单执法（P7 批次 6-③）：放在 TOOL_MAP.get() 之后、apply_goal_context()
        # 之前——被拒的调用连入参处理都不该做（也就不会打 [沙箱] 告警、不会留下 base_dir 痕迹）。
        denial = ToolManager._role_denial_reason(tool_name)
        if denial:
            return denial

        # 目标上下文注入：统一走共享实现（拷贝语义，不污染调用方传入的参数字典）
        arguments = apply_goal_context(tool_name, arguments)

        try:
            result = func(**arguments)
            return str(result)
        except Exception as e:
            return f"❌ 工具执行失败：{str(e)}"
