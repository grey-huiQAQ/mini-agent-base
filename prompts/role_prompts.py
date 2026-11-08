"""
加载 roles.json 中的角色提示词
"""
import json
from pathlib import Path
from core.tool_injection import FILE_TOOLS
from memory.prompt_store import PromptStore

ROLES_FILE = Path(__file__).parent / "roles.json"

# 文件用法说明（P7 批次 5b）：按角色工具白名单追加到系统提示末尾。
# 为什么落点在 get_prompt() 而不是 roles.json 的 prompt 字段：get_prompt() 优先返回
# PromptStore.get_active_prompt(role)，角色人设一旦"进化"过，写进 roles.json 的那句
# 会被整体替换掉、静默消失；追加发生在"取到哪一版人设"之后，因此对两种来源都生效。
# 文案里刻意不写 data/library 这类字面路径——模型只能传 filename、根由系统决定，
# 写死路径只会在将来改目录时多一个漂移点。
FILE_USAGE_WRITABLE = (
    "文件用法：写入只会落在你的专属目录，路径由系统决定——不要在工具参数里传路径，"
    "传了会被忽略。另有一个只读的「公共书架」，放的是共享的知识与经验，可直接用 "
    "read_file / list_files 读取；想贡献内容，请在回复里整理好交给用户决定是否收录。"
)

FILE_USAGE_READONLY = (
    "文件用法：你只读不写。可用 read_file / list_files 读取你的专属目录，"
    "以及只读「公共书架」（共享的知识与经验）。不要在工具参数里传路径，传了会被忽略。"
)

# 搜索用法说明（P7 批次 6-④）：按角色工具白名单追加（只给有 web_search 的角色）。
# 为什么要单独一句：咽喉点管的是**读根**，模型合法读到的内容下一步可以原样塞进
# web_search 的查询词——那是一次真正离开本机的请求，而 web_search 对 query 的来源
# 不做任何检查。这条「第二跳」不可判定（模型本就有正当理由搜索刚读到的内容），
# 故不拦截，只把要求写进系统提示，让"原样外发"从默认行为变成一个需要刻意违反的动作。
SEARCH_USAGE_NOTICE = (
    "搜索用法：调用 web_search 时，查询词里不要出现你从文件或知识库里读到的内容原文——"
    "搜索词会离开本机，需要检索时请自己概括成关键词。"
)


def load_roles():
    """从 roles.json 加载所有角色配置"""
    with open(ROLES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# 导出 ROLE_PROMPTS 供 __init__.py 使用
ROLE_PROMPTS = load_roles()

def load_default_prompt(role: str) -> str:
    """从 roles.json 加载默认提示词

    键名约定：roles.json 中每个角色配置的提示词键为 "prompt"（与 "tools"
    并列），并非 "system_prompt"——曾误读后者导致四段角色人设恒为空、
    从未随 system 消息发送给模型（P0-2 回归）。
    """
    roles = load_roles()
    return roles.get(role, {}).get("prompt", "")

def file_usage_notice(role: str) -> str:
    """按角色工具白名单返回文件用法说明；不含任何文件类工具时返回空串

    三档（P7 批次 5b）：含 write_file → 可写版；不含 write_file 但含文件类工具
    → 只读版；不含任何文件类工具 → 空串（**不塞一句与工具集不符的话**）。
    文件类工具集合取自 core.tool_injection.FILE_TOOLS，不在这里另写一份字面量。
    """
    tools = set(get_role_tools(role))
    if not tools & FILE_TOOLS:
        return ""
    if "write_file" in tools:
        return FILE_USAGE_WRITABLE
    return FILE_USAGE_READONLY

def search_usage_notice(role: str) -> str:
    """角色白名单里有 web_search 时返回搜索用法说明，否则返回空串

    判据必须是 `"web_search" in tools`，**不能**用 rag_search 或「有检索类工具」近似：
    prompts/roles.json 里的「英语」有 rag_search 但没有 web_search，它不该拿到这句
    （同一原则：不塞一句与工具集不符的话）。
    """
    if "web_search" in set(get_role_tools(role)):
        return SEARCH_USAGE_NOTICE
    return ""

def get_prompt(role: str) -> str:
    """获取角色提示词：人设（进化版优先）＋ 搜索用法（若有 web_search）＋ 文件用法

    片段顺序固定为「基础人设 → 搜索说明 → 文件用法」，即**文件用法留在结尾**：
    既有用例钉的是 `prompt.endswith(可写版/只读版)`，把搜索说明插在文件用法之前
    可以让那两条断言一字不改。空片段一律不参与拼接，避免前导空行或连续空行。
    """
    active_prompt = PromptStore.get_active_prompt(role)
    base_prompt = active_prompt if active_prompt else load_default_prompt(role)
    parts = [base_prompt, search_usage_notice(role), file_usage_notice(role)]
    return "\n\n".join(p for p in parts if p)

def get_role_tools(role: str) -> list:
    """获取角色工具权限"""
    roles = load_roles()
    return roles.get(role, {}).get("tools", [])

def get_all_roles() -> list:
    """获取所有角色名称"""
    roles = load_roles()
    return list(roles.keys())