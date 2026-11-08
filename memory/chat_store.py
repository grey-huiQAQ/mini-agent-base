"""
对话持久化存储
- 本地 JSON 文件存储
- 支持新建、切换、删除会话
- 程序重启自动恢复
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


class ChatStore:
    """对话存储管理器"""

    def __init__(self, storage_dir: str = "data/conversations"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.storage_dir / "index.json"
        self._ensure_index()

    def _ensure_index(self):
        """确保索引文件存在"""
        if not self.index_file.exists():
            self._save_index({})

    def _load_index(self) -> dict:
        """加载会话索引"""
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_index(self, index: dict):
        """保存会话索引"""
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def create_session(self, title: Optional[str] = None) -> str:
        """创建新会话，返回 session_id"""
        session_id = uuid.uuid4().hex[:12]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        index = self._load_index()
        index[session_id] = {
            "title": title or f"新会话 {now}",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }
        # 创建空对话文件
        session_file = self.storage_dir / f"{session_id}.json"
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
        self._save_index(index)
        return session_id

    def get_all_sessions(self) -> list:
        """获取所有会话列表"""
        index = self._load_index()
        sessions = []
        for sid, info in index.items():
            sessions.append({
                "id": sid,
                "title": info.get("title", sid),
                "created_at": info.get("created_at", ""),
                "updated_at": info.get("updated_at", ""),
                "message_count": info.get("message_count", 0),
            })
        # 按更新时间倒序
        sessions.sort(key=lambda x: x["updated_at"], reverse=True)
        return sessions

    def load_history(self, session_id: str) -> list:
        """加载指定会话的对话历史"""
        session_file = self.storage_dir / f"{session_id}.json"
        if not session_file.exists():
            return []
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def save_history(self, session_id: str, history: list):
        """保存对话历史到指定会话"""
        session_file = self.storage_dir / f"{session_id}.json"
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        # 更新索引
        index = self._load_index()
        if session_id in index:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            index[session_id]["updated_at"] = now
            index[session_id]["message_count"] = len(history)
            self._save_index(index)

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话"""
        session_file = self.storage_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
        index = self._load_index()
        if session_id in index:
            del index[session_id]
            self._save_index(index)
            return True
        return False

    def rename_session(self, session_id: str, new_title: str):
        """重命名会话"""
        index = self._load_index()
        if session_id in index:
            index[session_id]["title"] = new_title
            self._save_index(index)