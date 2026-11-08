# -*- coding: utf-8 -*-
"""
Agent 资源消耗监控（v0.1）

职责：
- log_resource_usage()：轻量记录一次资源操作（LLM 调用 / 搜索调用 / 网页抓取等）的
  估算 token 数、类型与时间戳，写入 agent_resource_log 表（粗粒度统计，非精确计费）；
- 埋点原则：写日志全程 try/except 包裹、单条 INSERT，任何失败都不影响业务主流程，
  不引入额外 API 调用；
- 统计：get_resource_stats() 提供最近 N 天按类型汇总（UI 展示用）；
- 表结构由 memory/database.init_db() 统一创建（CREATE TABLE IF NOT EXISTS），
  本模块在写入前也会自建表，保证临时库 / 独立使用场景可用。

用法（在业务代码中埋点）：
    from memory.resource_log import log_resource_usage, estimate_tokens
    log_resource_usage("llm", 1234, "chat_sync", "模型 Qwen/Qwen3.5-35B-A3B")
"""

import sqlite3
from datetime import datetime, timedelta

# 字符 → token 粗粒度换算（中英文混合场景的经验值：约 4 字符 / token）
_CHARS_PER_TOKEN = 4


def _get_conn(db_path=None):
    """获取数据库连接：显式 db_path（测试/独立使用传临时库）或默认项目库

    默认库走 memory.database.get_db()（带行工厂）；显式路径新建连接并同样
    启用行工厂，保证查询结果可用字典方式访问。
    """
    if db_path is not None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    from memory.database import get_db
    return get_db()


def estimate_tokens(text) -> int:
    """按字符数粗估 token 数（text 可为 None / 非字符串；空文本返回 0）"""
    try:
        if text is None:
            return 0
        n = len(str(text))
        if n == 0:
            return 0
        return max(1, n // _CHARS_PER_TOKEN)
    except Exception:
        return 0


def estimate_messages_tokens(messages) -> int:
    """按消息列表粗估输入 token 数（含 role 与 content 的字符）

    content 为列表（多模态 / 工具调用）时拼接文本部分估算，兼容 OpenAI 消息格式。
    """
    try:
        total = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or ""
            content = msg.get("content") or ""
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                    else:
                        parts.append(str(item))
                content = " ".join(parts)
            total += estimate_tokens(role) + estimate_tokens(content)
        return total
    except Exception:
        return 0


def log_resource_usage(resource_type, token_estimate=0, operation="", detail="",
                       db_path=None):
    """记录一次资源操作（写入 agent_resource_log 表）

    Args:
        resource_type: 资源类型（llm / search / fetch 等，写入前截断到 32 字符）
        token_estimate: 估算 token 数（整数，负数按 0 处理）
        operation: 操作名（如 chat_sync / ddgs / web_search，截断到 64 字符）
        detail: 简要描述（如模型名 / 查询前 300 字符）
        db_path: 显式指定数据库路径（默认使用项目 data/memory.db；测试传临时库）

    任何异常仅打印日志，绝不影响业务主流程。
    """
    try:
        conn = _get_conn(db_path)
        try:
            # 自建表（幂等）：保证临时库 / 未执行 init_db 的场景也能写入
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_resource_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource_type TEXT NOT NULL,"
                " operation TEXT DEFAULT '',"
                " token_estimate INTEGER DEFAULT 0,"
                " detail TEXT DEFAULT '',"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO agent_resource_log "
                "(resource_type, operation, token_estimate, detail) "
                "VALUES (?, ?, ?, ?)",
                (str(resource_type or "unknown")[:32],
                 str(operation or "")[:64],
                 max(0, int(token_estimate or 0)),
                 str(detail or "")[:300]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[ResourceLog] 写入资源日志失败（忽略）: {e}")


def get_resource_stats(days=7, db_path=None) -> dict:
    """最近 N 天资源消耗统计（按类型汇总 + 每日明细 + 总 token）

    Returns:
        {"days": N, "total_tokens": int, "by_type": [{type, calls, tokens}],
         "daily": [{day, type, calls, tokens}]}
    """
    result = {"days": int(days), "by_type": [], "daily": [], "total_tokens": 0}
    try:
        conn = _get_conn(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_resource_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource_type TEXT NOT NULL,"
                " operation TEXT DEFAULT '',"
                " token_estimate INTEGER DEFAULT 0,"
                " detail TEXT DEFAULT '',"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            since = (datetime.now() - timedelta(days=int(days))).strftime(
                "%Y-%m-%d %H:%M:%S")
            rows = conn.execute(
                "SELECT resource_type, COUNT(*) AS calls, "
                "       SUM(token_estimate) AS tokens "
                "FROM agent_resource_log WHERE created_at >= ? "
                "GROUP BY resource_type ORDER BY tokens DESC", (since,)
            ).fetchall()
            for r in rows:
                result["by_type"].append({
                    "type": r["resource_type"],
                    "calls": r["calls"],
                    "tokens": r["tokens"] or 0,
                })
            result["total_tokens"] = sum(i["tokens"] for i in result["by_type"])
            daily = conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, resource_type, "
                "       COUNT(*) AS calls, SUM(token_estimate) AS tokens "
                "FROM agent_resource_log WHERE created_at >= ? "
                "GROUP BY day, resource_type ORDER BY day", (since,)
            ).fetchall()
            for r in daily:
                result["daily"].append({
                    "day": r["day"], "type": r["resource_type"],
                    "calls": r["calls"], "tokens": r["tokens"] or 0,
                })
        finally:
            conn.close()
    except Exception as e:
        print(f"[ResourceLog] 资源统计失败（忽略）: {e}")
    return result


def get_recent_resource_logs(limit=30, db_path=None) -> list:
    """最近 N 条资源日志（最新在前，UI 列表展示用）"""
    try:
        conn = _get_conn(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_resource_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource_type TEXT NOT NULL,"
                " operation TEXT DEFAULT '',"
                " token_estimate INTEGER DEFAULT 0,"
                " detail TEXT DEFAULT '',"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            rows = conn.execute(
                "SELECT id, resource_type, operation, token_estimate, detail, "
                "       created_at FROM agent_resource_log "
                "ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        print(f"[ResourceLog] 读取最近资源日志失败（忽略）: {e}")
        return []


def clear_resource_logs(before_days=30, db_path=None) -> int:
    """清理超过 N 天的旧日志（防止表无限膨胀；返回删除行数）"""
    try:
        conn = _get_conn(db_path)
        try:
            since = (datetime.now() - timedelta(days=int(before_days))).strftime(
                "%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "DELETE FROM agent_resource_log WHERE created_at < ?", (since,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[ResourceLog] 清理旧日志失败（忽略）: {e}")
        return 0
