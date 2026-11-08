# -*- coding: utf-8 -*-
"""core.tool_injection —— 「目标专属文件目录」注入的单一实现

为什么要单独一个模块（2026-09-12 收拢）：
    项目里有两条工具执行入口，各自实现同一套注入规则：
      - `agents/tool_manager.py` 的 `ToolManager.execute_tool()`——**当前唯一的活路径**：
        core/runtime → 模型插件 → business/model_client.stream_chat_with_tools → 它；
      - `plugins/local_tools/tool_plugin.py` 的 `LocalToolsPlugin.execute()`——`ToolPlugin`
        抽象接口要求实现（不实现就无法被插件加载器实例化），但**当前零调用方**。
    两处各写了一份「文件类工具集合」和一份注入逻辑，且行为不同：前者用**拷贝**注入
    （注释明确写"避免污染调用方传入的参数字典"），后者**原地**改写入参；而 tool_manager
    的注释只"承诺"与插件一致，没有任何机制强制（P4-7 曾把这条承诺变成相等断言，
    随后按用户决定收拢为同一对象）。
    现收拢到本模块：集合只有一份、注入语义只有一种（拷贝），两条入口都调用它。

    直接收益不只是去重——**这里是"再出现第三条执行入口"时的落点**：任何人写新的工具
    执行路径，只要调用 `apply_goal_context()` 就自动与既有语义一致，不会再复制一份规则。

    **本模块同时是文件读写边界的咽喉点（P7 批次 5 起）**：模型侧**不认识** `base_dir`
    （`TOOLS_DEFINITIONS` 里根本没有这个字段），但函数调用的 JSON 是模型自己生成的，
    它照样可以塞一个进来；而 `execute_tool` 用 `func(**arguments)` 透传全部入参，
    于是"仅当未传 base_dir 才注入"这条规则可以被一次性绕过，直达 local_tools——那里
    唯一的护栏是"filepath 是否在 base_dir 之下"，对**攻击者自选的根**永远成立。
    现在文件类工具的 `base_dir` 由本模块**无条件接管**：模型自带的会被丢弃，并打印
    告警、往 `agent_resource_log` 记一条痕迹（P7 批次 5b 起）。
    这与 P4-7 同一条教训——注释里写的承诺，必须有机制兜住。

    **本模块挡不住什么（P7 批次 6-④）**：这里管的是**读根**——模型能读的只有自己的
    专属目录与只读「公共书架」。但它合法读到的内容，下一步可以原样写进 `web_search`
    的查询词，那是一次真正离开本机的请求，而 `web_search` 对 query 的来源不做任何检查。
    这条「第二跳」不在本模块管辖内，并且**有意不做技术拦截**：模型本来就有正当理由
    搜索刚读到的内容，"内容撞库就拦"既会误杀、也很容易被改写绕过（不可判定）。
    现有的可见性来自 `web_search` 自身的埋点（记 query 前 60 字，可疑即可事后审计），
    以及系统提示里那句"不要把读到的原文写进查询词"——那是**要求**，不是防护。

    **工具名白名单执法也不在本模块（P7 批次 6-③）**：本模块是**入参**咽喉点，而 core 属
    L0 层、拿不到 prompts（L1）的 `get_role_tools`，故执法落在
    `agents/tool_manager.py` 的 `ToolManager._role_denial_reason`（由 `execute_tool`
    在 `TOOL_MAP` 查询之后、`apply_goal_context` 之前调用）。要改"哪个角色能调哪些工具"，
    去那里，不要在本模块再写一份。
"""

from typing import Any, Dict

# 需要注入目标专属目录的文件类工具（唯一权威定义；tool_manager 与 local_tools 插件都引用它）
FILE_TOOLS = frozenset({"read_file", "list_files", "write_file"})


def apply_goal_context(tool_name: str, arguments: Any) -> Dict[str, Any]:
    """按当前目标上下文为文件类工具补 `base_dir`；返回**新字典**，不修改调用方入参

    规则（P7 批次 5 起）：
      - 工具属于 `FILE_TOOLS` 时，**无条件接管** `base_dir`：
        入参里若带了它，**先丢弃**（并打印一条 `[沙箱]` 告警、把被丢弃的值也打出来，
        再往 `agent_resource_log` 记一条 `base_dir_ignored`（P7 批次 5b；埋点失败
        不影响本次调用）），然后按上下文注入。**无目标上下文时同样要丢弃**——否则
        "无目标"这一支的洞照样开着。
      - 有目标上下文（`core.runtime` 的 `current_goal_id`，线程本地）→ 注入
        `data/goals/{goal_id}/files`，使各目标的文件互相隔离；
      - 无目标上下文 → 不设 `base_dir`（调用方回落到默认目录 `data/files`）；
      - 非文件工具、或 `tool_name` 不认识 → 原样返回（不碰入参、不打告警）；
      - 入参不是 dict → 按空字典处理（与收拢前两处实现的宽松行为一致）。

    注意（保留的既有语义）：返回的是**拷贝**，调用方传入的参数字典绝不被修改
    （tests/test_skeleton.py 钉着这条）。

    注意：`core.runtime` 在模块级导入了 `agents.supervisor` 等，而 `agents.tool_manager`
    会在很早的时候导入本模块，因此对 `get_current_goal_id` 采用**函数内延迟导入**，
    避免形成模块级循环导入（收拢前 tool_manager 里的注释记录过同一原因）。
    """
    args = dict(arguments) if isinstance(arguments, dict) else {}
    if tool_name not in FILE_TOOLS:
        return args
    if "base_dir" in args:
        # 模型自带 base_dir ＝ 试图自选读写根：丢弃它，并且**出声**（安静地忽略等于
        # 把这个洞继续藏在注释里——本批要修的正是"承诺没有机制"）。
        dropped = args["base_dir"]
        print(f"[沙箱] 工具 {tool_name} 的入参自带了 base_dir={dropped!r}，"
              f"已强制忽略（文件类工具的读写根由目标上下文接管）")
        # 越界留痕（P7 批次 5b）：除了滚过屏幕的一行，再往 agent_resource_log 落一条，
        # 让"有东西在诱导模型越界"可被事后统计。埋点规格照 tools/local_tools.py 的
        # web_search：延迟导入 + 整体 try/except，失败只影响这条记录，绝不影响工具调用。
        try:
            from memory.resource_log import log_resource_usage
            log_resource_usage("sandbox", 0, "base_dir_ignored",
                               f"工具 {tool_name} 丢弃模型自带的 base_dir={dropped!r}")
        except Exception:
            pass
        del args["base_dir"]
    from core.runtime import get_current_goal_id

    goal_id = get_current_goal_id()
    if goal_id:
        args["base_dir"] = f"data/goals/{goal_id}/files"
    return args
