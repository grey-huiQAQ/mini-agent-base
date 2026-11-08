"""
AgentState 持久化存储
"""
import json
from typing import Optional
from datetime import datetime
from core.state import AgentState, AgentStatus
from memory.database import get_connection


class StateStore:
    @staticmethod
    def save(state: AgentState):
        conn = get_connection()
        conn.execute("""
            INSERT INTO agent_states (agent_id, session_id, agent_type, goal, current_task, tasks, checkpoint_ref, token_budget, loaded_skills, status, updated_at, metadata, goal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                goal = excluded.goal,
                current_task = excluded.current_task,
                tasks = excluded.tasks,
                checkpoint_ref = excluded.checkpoint_ref,
                token_budget = excluded.token_budget,
                loaded_skills = excluded.loaded_skills,
                status = excluded.status,
                updated_at = excluded.updated_at,
                metadata = excluded.metadata,
                goal_id = excluded.goal_id
        """, (
            state.agent_id,
            state.session_id,
            state.agent_type,
            state.goal,
            state.current_task,
            json.dumps(state.tasks, ensure_ascii=False),
            state.checkpoint_ref,
            state.token_budget,
            json.dumps(state.loaded_skills, ensure_ascii=False),
            state.status.value,
            state.updated_at.isoformat(),
            json.dumps(state.metadata, ensure_ascii=False),
            state.goal_id
        ))
        conn.commit()
        conn.close()

    @staticmethod
    def load(agent_id: str) -> Optional[AgentState]:
        conn = get_connection()
        row = conn.execute("SELECT * FROM agent_states WHERE agent_id = ?", (agent_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return AgentState(
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            agent_type=row["agent_type"],
            goal=row["goal"] or "",
            current_task=row["current_task"] or "",
            tasks=json.loads(row["tasks"]) if row["tasks"] else [],
            checkpoint_ref=row["checkpoint_ref"],
            token_budget=row["token_budget"] or 4000,
            loaded_skills=json.loads(row["loaded_skills"]) if row["loaded_skills"] else [],
            status=AgentStatus(row["status"] or "idle"),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.now(),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            goal_id=row["goal_id"] if "goal_id" in row.keys() else None
        )

    @staticmethod
    def load_by_session(session_id: str) -> Optional[AgentState]:
        conn = get_connection()
        row = conn.execute("SELECT agent_id FROM agent_states WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
        conn.close()
        if row:
            return StateStore.load(row["agent_id"])
        return None