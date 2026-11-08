"""
本地工具函数
供 AI 通过 Function Call 调用
"""

import time
from pathlib import Path
from datetime import datetime

# 两个根，职责不同（P7 批次 5b 拆开）。拆开的原因：此前读侧回落目标就是私有写入根
# 同一个 data/files，于是"模型随手写的草稿"同时充当"共享书架"。
#   DEFAULT_FILES_ROOT —— **私有写入根**：无目标上下文时的读写根（有目标上下文时是
#     data/goals/{id}/files）。模型写文件只会落在这里，不会落进书架；
#   SHARED_LIBRARY_ROOT —— **只读「公共书架」**：放的是用户维护的知识与经验，模型只读；
#     带目标上下文时 read_file / list_files 在自己目录里找不到，会到书架再找一次。
#
# 读写边界由**两处共同**保证，单靠任何一处都不够（P7 批次 5 收口）：
#   ① 模型侧不认识 base_dir（TOOLS_DEFINITIONS 里没有这个字段），而
#      core.tool_injection.apply_goal_context 对文件类工具**无条件接管** base_dir：
#      模型自带的值会被丢弃，并打印 `[沙箱]` 告警、往 agent_resource_log 记一次
#      "base_dir_ignored"（P7 批次 5b 起）；无目标上下文时同样丢弃。
#      —— 这一层拦的是"模型自选根"。
#   ② 本模块用 Path.is_relative_to 校验解析后的路径必须落在根之下（含兄弟前缀混淆）。
#      —— 这一层拦的是"库级直接调用 / 任何绕过咽喉点的调用"。
DEFAULT_FILES_ROOT = Path("data/files")
DEFAULT_FILES_ROOT.mkdir(parents=True, exist_ok=True)

# 公共书架（只读）：用户维护、模型只读。独立于私有写入根，避免写侧把草稿写进共享语料
SHARED_LIBRARY_ROOT = Path("data/library")
SHARED_LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)


def _resolve_within(root: Path, filename: str):
    """把 filename 解析到 root 之下；越界或解析失败返回 None

    纯路径判断，**不做任何 IO、不建目录**。用 `is_relative_to` 而不是字符串
    `startswith`：后者会把 `data/files_probe` 误判成 `data/files` 的子路径
    （兄弟前缀混淆，实测可读到隔壁目录）。
    """
    try:
        candidate = (root / filename).resolve()
        root_resolved = root.resolve()
    except Exception:
        return None
    return candidate if candidate.is_relative_to(root_resolved) else None


def _describe_roots(roots) -> str:
    """按根逐个列出可读文件（list_files 与 read_file 的"找不到"提示共用一份实现）

    不存在的根会被跳过（不 iterdir 抛错）；同名文件按先出现的根计一次（去重）。
    """
    seen, blocks = set(), []
    for root in roots:
        if not root.is_dir():
            blocks.append(f"{root} 目录不存在。")
            continue
        names = []
        for f in sorted(root.iterdir(), key=lambda p: p.name):
            if not f.is_file() or f.name in seen:
                continue
            seen.add(f.name)
            names.append(f.name)
        if names:
            blocks.append(f"{root} 目录下可用文件：\n"
                          + "\n".join(f"- {n}" for n in names))
        else:
            blocks.append(f"{root} 目录下暂无文件。")
    return "\n".join(blocks)


def read_file(filename: str, max_chars: int = 3000, base_dir: str = None) -> str:
    """
    读取本地文件内容

    Args:
        filename: 文件名（仅限指定目录下）
        max_chars: 最大读取字符数
        base_dir: 文件根目录，为 None 时使用默认目录 data/files

    Returns:
        文件内容字符串或错误信息

    只读语义（P7 批次 5 / 5b）：**不创建任何目录**；在自己 root 里找不到时，若 root
    不是公共书架，再到 `data/library`（书架）找一次。
    """
    root = Path(base_dir) if base_dir else DEFAULT_FILES_ROOT
    filepath = _resolve_within(root, filename)
    if filepath is None:
        return f"❌ 安全限制：只能读取 {root} 目录下的文件。"

    searched = [root]
    if not filepath.exists() and root.resolve() != SHARED_LIBRARY_ROOT.resolve():
        fallback = _resolve_within(SHARED_LIBRARY_ROOT, filename)
        if fallback is not None:
            filepath = fallback
            searched.append(SHARED_LIBRARY_ROOT)

    if not filepath.exists():
        return f"❌ 文件 '{filename}' 不存在。已搜索：\n" + _describe_roots(searched)

    if not filepath.is_file():
        return f"❌ '{filename}' 不是有效文件。"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read(max_chars)
        if len(content) >= max_chars:
            content += "\n\n...（内容过长，已截断）"
        return content
    except UnicodeDecodeError:
        return f"❌ 无法读取 '{filename}'，可能不是文本文件（尝试读取二进制文件）。"
    except Exception as e:
        return f"❌ 读取失败：{str(e)}"


def list_files(base_dir: str = None) -> str:
    """列出可读文件：指定根（未指定时为私有根）**加上**公共书架 data/library

    只读操作：**不创建目录**，不存在的根跳过；同名文件两边都有时只列一次。
    """
    root = Path(base_dir) if base_dir else DEFAULT_FILES_ROOT
    roots = [root]
    if root.resolve() != SHARED_LIBRARY_ROOT.resolve():
        roots.append(SHARED_LIBRARY_ROOT)
    return _describe_roots(roots)


def write_file(filename: str, content: str, base_dir: str = None) -> str:
    """
    写入文件到指定目录

    Args:
        filename: 文件名
        content: 要写入的文本内容
        base_dir: 文件根目录，为 None 时使用默认目录 data/files

    Returns:
        成功消息或错误信息

    写入语义（P7 批次 5）：**只落单一窄根**（目标目录，或无目标时的 data/files），
    没有只读书架回落；目录校验在任何 mkdir 之前完成。
    """
    # 校验文件名
    if not filename or not filename.strip():
        return "❌ 文件名不能为空"
    filename = filename.strip()
    # 禁止路径分隔符（仅允许纯文件名）
    if "/" in filename or "\\" in filename:
        return f"❌ 文件名不能包含路径分隔符：{filename}"

    root = Path(base_dir) if base_dir else DEFAULT_FILES_ROOT
    filepath = _resolve_within(root, filename)
    if filepath is None:
        return f"❌ 安全限制：只能写入 {root} 目录下的文件。"

    try:
        # 校验通过之后才建目录（读路径不建目录，写路径才建）
        root.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入文件：{filename}（{len(content)} 字符）"
    except Exception as e:
        return f"❌ 写入失败：{str(e)}"


def get_current_time() -> str:
    """获取当前日期时间"""
    return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
# ---------- 网络搜索 ----------
def web_search(query: str, max_results: int = 3) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        return "❌ 搜索功能未安装，请运行：pip install ddgs"

    def try_search(q):
        with DDGS() as ddgs:
            res = []
            for r in ddgs.text(q, max_results=max_results):
                res.append(r)
                if len(res) >= max_results:
                    break
            return res

    try:
        results = try_search(query)
        if not results:
            # 尝试用英文关键词
            results = try_search("latest news")
        if not results:
            return "未找到相关结果，请更换关键词重试。"
        # 资源消耗埋点（v0.1）：记录一次搜索调用（粗粒度 token 估算，失败不影响主流程）
        try:
            from memory.resource_log import log_resource_usage, estimate_tokens
            log_resource_usage("search", estimate_tokens(query), "web_search",
                               f"查询：{query[:60]}；结果 {len(results)} 条")
        except Exception:
            pass
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get('title', '无标题')
            href = r.get('href', '')
            body = r.get('body', '')[:200]
            lines.append(f"{i}. {title}\n   {body}\n   {href}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"❌ 搜索失败：{str(e)}"
# ---------- RAG 检索 ----------
def rag_search(query: str, top_k: int = 3) -> str:
    """从本地知识库检索相关内容（RAG）"""
    start = time.time()
    print(f"[Tool] 调用 rag_search，query: {query}, top_k: {top_k}")
    try:
        from rag.retriever import rag_search as _rag_search
        result = _rag_search(query, top_k)
        print(f"[Tool] rag_search 返回，耗时 {time.time()-start:.2f}s")
        return result
    except ImportError as e:
        print(f"[Tool] RAG 模块导入失败: {e}")
        return f"❌ RAG 模块未安装或加载失败：{str(e)}"
    except Exception as e:
        print(f"[Tool] rag_search 异常: {e}")
        return f"❌ 检索知识库时出错：{e}"