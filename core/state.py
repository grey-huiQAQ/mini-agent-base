from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"

@dataclass
class AgentState:
    """Agent 状态快照：字段当前仅写入、业务逻辑尚未读取（预留 (2026-09-06)，供后续审计/恢复接线）"""
    agent_id: str
    session_id: str
    agent_type: str
    goal: str = ""
    current_task: str = ""
    tasks: List[str] = field(default_factory=list)
    checkpoint_ref: Optional[str] = None
    token_budget: int = 4000
    loaded_skills: List[str] = field(default_factory=list)
    status: AgentStatus = AgentStatus.IDLE
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 新增：目标 ID
    goal_id: Optional[str] = None