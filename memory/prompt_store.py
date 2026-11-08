"""
Prompt 版本存储与管理
"""
from memory.database import get_connection

class PromptStore:
    @staticmethod
    def add_version(task_name: str, prompt_text: str, version: int = None, is_active: int = 1):
        """添加新版本（或更新现有版本）"""
        conn = get_connection()
        if version is None:
            row = conn.execute(
                "SELECT MAX(version) as max_v FROM prompt_versions WHERE task_name = ?",
                (task_name,)
            ).fetchone()
            version = (row["max_v"] or 0) + 1
        if is_active:
            conn.execute("UPDATE prompt_versions SET is_active = 0 WHERE task_name = ?", (task_name,))
        conn.execute("""
            INSERT INTO prompt_versions (task_name, version, prompt_text, is_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_name, version) DO UPDATE SET prompt_text = ?, is_active = ?
        """, (task_name, version, prompt_text, is_active, prompt_text, is_active))
        conn.commit()
        conn.close()
        return version

    @staticmethod
    def get_active_prompt(task_name: str) -> str | None:
        """获取当前活跃的提示词"""
        conn = get_connection()
        row = conn.execute(
            "SELECT prompt_text FROM prompt_versions WHERE task_name = ? AND is_active = 1",
            (task_name,)
        ).fetchone()
        conn.close()
        return row["prompt_text"] if row else None

    @staticmethod
    def get_version(task_name: str, version: int) -> str | None:
        conn = get_connection()
        row = conn.execute(
            "SELECT prompt_text FROM prompt_versions WHERE task_name = ? AND version = ?",
            (task_name, version)
        ).fetchone()
        conn.close()
        return row["prompt_text"] if row else None

    @staticmethod
    def update_score(task_name: str, version: int, score: float, feedback: str = None):
        conn = get_connection()
        conn.execute("""
            UPDATE prompt_versions SET score = ?, feedback = ?
            WHERE task_name = ? AND version = ?
        """, (score, feedback, task_name, version))
        conn.commit()
        conn.close()

    @staticmethod
    def get_all_versions(task_name: str) -> list:
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM prompt_versions WHERE task_name = ? ORDER BY version DESC",
            (task_name,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_all_tasks() -> list:
        conn = get_connection()
        rows = conn.execute("SELECT DISTINCT task_name FROM prompt_versions").fetchall()
        conn.close()
        return [r["task_name"] for r in rows]