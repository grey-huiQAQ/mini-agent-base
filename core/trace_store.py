import sqlite3
import json
from typing import Any, Dict, List, Optional
from memory.database import get_connection


class TraceStore:
    def __init__(self):
        conn = get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                agent_state_id TEXT,
                step_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                token_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def record(self, session_id: str, step_type: str, detail: Dict[str, Any],
               agent_state_id: Optional[str] = None, token_used: int = 0):
        # 说明：token_used 字段当前恒为 0，未接入实际 token 统计（预留 (2026-09-06) 后续接线）
        conn = get_connection()
        conn.execute(
            "INSERT INTO execution_traces (session_id, agent_state_id, step_type, detail, token_used) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_state_id, step_type, json.dumps(detail, ensure_ascii=False), token_used)
        )
        conn.commit()
        conn.close()

    def get_traces(self, session_id: str, limit: int = 50) -> List[Dict]:
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM execution_traces WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]