"""
用户档案管理
存储用户的背景信息、偏好、目标等
"""

from .database import get_db


class ProfileManager:
    """用户档案 CRUD"""

    @staticmethod
    def set(key: str, value: str):
        """设置或更新档案项"""
        conn = get_db()
        conn.execute(
            """
            INSERT INTO profile (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get(key: str) -> str | None:
        """获取单个档案项"""
        conn = get_db()
        row = conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else None

    @staticmethod
    def get_all() -> dict:
        """获取全部档案"""
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM profile ORDER BY key").fetchall()
        conn.close()
        return {row["key"]: row["value"] for row in rows}

    @staticmethod
    def delete(key: str):
        """删除档案项"""
        conn = get_db()
        conn.execute("DELETE FROM profile WHERE key = ?", (key,))
        conn.commit()
        conn.close()

    @staticmethod
    def set_batch(items: dict):
        """批量设置档案"""
        for key, value in items.items():
            ProfileManager.set(key, value)