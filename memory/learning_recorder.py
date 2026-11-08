"""
学习记录、错误记录、任务管理
"""

import json
from datetime import datetime
from .database import get_db
from memory.database import get_connection

#: 掌握度「未评」的统一文案（P7 批次 12-①）：渲染层遇到缺失值一律用它，
#: 绝不产出 "None"、"0/5" 或空星星。
UNRATED_LABEL = "未评"

#: 掌握度的合法区间（含端点，1–5）——写入侧 docstring、给模型的指令、渲染层同一口径
MASTERY_SCORE_RANGE = (1, 5)


def normalize_mastery_score(value) -> int | None:
    """把任意输入收成 1–5 的整数，或 None（＝「未评」）

    为什么 0 也收成 None（P7 批次 12-①）：0 不是这个量表上的取值（量表是 1–5），
    它只可能来自列默认值（memory/database.py 的 `mastery_score INTEGER DEFAULT 0`）
    或旧签名默认值（本模块 add_learning_record 曾写 `mastery_score: int = 0`），
    两个来源都不该产生一个"合法分数"。把 0 读成"掌握度 0"是本仓先前最大的一个谎：
    0 分与未评在界面上长得一样，却会在薄弱点过滤（`mastery_score <= 2` 会匹配 0）
    与平均分里被当成"最差"处理。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    lo, hi = MASTERY_SCORE_RANGE
    return v if lo <= v <= hi else None


def format_mastery_stars(value) -> str:
    """掌握度 → 星星串；未评 → 「未评」（不画星星）"""
    v = normalize_mastery_score(value)
    return "⭐" * v if v else UNRATED_LABEL


def format_mastery_ratio(value) -> str:
    """掌握度 → 「3/5」；未评 → 「未评」（不给分母，避免出现「未评/5」）"""
    v = normalize_mastery_score(value)
    return f"{v}/5" if v else UNRATED_LABEL


def format_mastery_plain(value) -> str:
    """掌握度 → 「3」；未评 → 「未评」"""
    v = normalize_mastery_score(value)
    return str(v) if v else UNRATED_LABEL


class LearningRecorder:
    """学习数据管理器"""

    # ========== 学习记录 ==========

    @staticmethod
    def add_learning_record(subject: str, topic: str, summary: str,
                            mastery_score: int | None = None, difficulty: int = 1,
                            weak_points: list | None = None,
                            goal_id: str | None = None):
        """添加学习记录

        Args:
            subject: 学科/领域
            topic: 学习主题
            summary: 学习摘要
            mastery_score: **模型在一轮学习结束时对自己"这一轮讲清了没有"的印象分，
                取值 1–5 的整数。缺失（库里存 NULL）＝「未评」**——未评不进任何决策：
                不算薄弱点、不进平均分（AVG 天然忽略 NULL）、不进提示词（渲染成
                「未评」而不是 None/0）。不传、传 None、传 0、传区间外的值一律存
                NULL（0 视同未评：它只可能来自列默认值或旧签名默认值，两个来源都
                不该产生合法分数，详见 normalize_mastery_score）。
            difficulty: 难度（1-5）
            weak_points: 薄弱点列表
            goal_id: 关联的目标 ID（可选）
        """
        # 唯一真源：把"没填算什么"收在一处（P7 批次 12-①）。此前这里有签名默认 0、
        # 调用方有 `data.get(..., 3)`，同一个缺失能变出两个答案。
        mastery_score = normalize_mastery_score(mastery_score)
        weak_json = json.dumps(weak_points, ensure_ascii=False) if weak_points else None
        conn = get_db()
        conn.execute(
            """INSERT INTO learning_records (subject, topic, summary, mastery_score, difficulty, weak_points, goal_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (subject, topic, summary, mastery_score, difficulty, weak_json, goal_id),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_recent_records(subject: str = None, limit: int = 5) -> list:
        """获取最近学习记录"""
        conn = get_db()
        if subject:
            rows = conn.execute(
                """SELECT * FROM learning_records WHERE subject = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (subject, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM learning_records ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_weak_points(subject: str = None, limit: int = 5) -> list:
        """获取薄弱点（掌握度 <= 2 的记录）"""
        conn = get_db()
        if subject:
            rows = conn.execute(
                """SELECT topic, weak_points, mastery_score FROM learning_records
                   WHERE subject = ? AND mastery_score <= 2
                   ORDER BY created_at DESC LIMIT ?""",
                (subject, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT topic, weak_points, mastery_score FROM learning_records
                   WHERE mastery_score <= 2
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ========== 错误记录 ==========

    @staticmethod
    def record_mistake(subject: str, description: str, category: str = None):
        """记录错误（已存在则增加频次）"""
        conn = get_db()
        existing = conn.execute(
            "SELECT id, frequency FROM mistakes WHERE subject = ? AND description = ?",
            (subject, description),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE mistakes SET frequency = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?",
                (existing["frequency"] + 1, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO mistakes (subject, category, description) VALUES (?, ?, ?)",
                (subject, category, description),
            )
        conn.commit()
        conn.close()

    @staticmethod
    def get_common_mistakes(subject: str = None, limit: int = 5) -> list:
        """获取常见错误（按频率排序）"""
        conn = get_db()
        if subject:
            rows = conn.execute(
                """SELECT * FROM mistakes WHERE subject = ?
                   ORDER BY frequency DESC, last_seen DESC LIMIT ?""",
                (subject, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mistakes ORDER BY frequency DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ========== 任务管理 ==========

    @staticmethod
    def add_task(title: str, subject: str = None, priority: int = 1, due_date: str = None):
        """添加任务"""
        conn = get_db()
        conn.execute(
            "INSERT INTO tasks (title, subject, priority, due_date) VALUES (?, ?, ?, ?)",
            (title, subject, priority, due_date),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_pending_tasks(subject: str = None, limit: int = 5) -> list:
        """获取待办任务"""
        conn = get_db()
        if subject:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE status = 'pending' AND subject = ?
                   ORDER BY priority DESC, due_date ASC LIMIT ?""",
                (subject, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE status = 'pending'
                   ORDER BY priority DESC, due_date ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def complete_task(task_id: int):
        """完成任务"""
        conn = get_db()
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def delete_task(task_id: int):
        """删除任务"""
        conn = get_db()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
    @staticmethod
    def get_records_by_date(date: str):
        conn = get_connection()
        rows = conn.execute(
             "SELECT * FROM learning_records WHERE date(created_at) = ?", (date,)
        ).fetchall()
        return [dict(r) for r in rows]