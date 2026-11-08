"""
每日计划与复盘存储
"""
import sqlite3
from datetime import datetime
from memory.database import get_connection

class DailyPlanner:
    @staticmethod
    def save_plan(date: str, content: str):
        conn = get_connection()
        conn.execute("""
            INSERT INTO daily_plans (plan_date, content, status)
            VALUES (?, ?, 'generated')
            ON CONFLICT(plan_date) DO UPDATE SET content = ?, status = 'generated'
        """, (date, content, content))
        conn.commit()
        conn.close()

    @staticmethod
    def get_plan(date: str) -> str | None:
        conn = get_connection()
        row = conn.execute(
            "SELECT content FROM daily_plans WHERE plan_date = ?", (date,)
        ).fetchone()
        conn.close()
        return row["content"] if row else None

    @staticmethod
    def save_review(date: str, content: str):
        conn = get_connection()
        conn.execute("""
            INSERT INTO daily_reviews (review_date, content)
            VALUES (?, ?)
            ON CONFLICT(review_date) DO UPDATE SET content = ?
        """, (date, content, content))
        conn.commit()
        conn.close()

    @staticmethod
    def get_review(date: str) -> str | None:
        conn = get_connection()
        row = conn.execute(
            "SELECT content FROM daily_reviews WHERE review_date = ?", (date,)
        ).fetchone()
        conn.close()
        return row["content"] if row else None