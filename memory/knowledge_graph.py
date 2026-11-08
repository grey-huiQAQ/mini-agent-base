"""
知识图谱管理器
提供结构化知识节点的存储、查询与关系维护
"""

import json
import sqlite3
from datetime import datetime
from .database import get_db


class KnowledgeGraph:
    """知识图谱 CRUD 与查询"""

    # ---------- 节点创建 ----------
    @staticmethod
    def add_node(
        name: str,
        domain: str,
        parent_id: int | None = None,
        prerequisites: list[int] | None = None,
        related_nodes: list[int] | None = None,
        mastery_level: int = 0,
        confidence: float = 0.0,
    ) -> int:
        """添加知识节点，返回节点 ID"""
        conn = get_db()
        cursor = conn.execute(
            """
            INSERT INTO knowledge_nodes
                (name, domain, parent_id, prerequisites, related_nodes, mastery_level, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, name) DO UPDATE SET
                parent_id = excluded.parent_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                name, domain, parent_id,
                json.dumps(prerequisites or []),
                json.dumps(related_nodes or []),
                mastery_level, confidence,
            ),
        )
        conn.commit()
        node_id = cursor.lastrowid if cursor.lastrowid else \
            conn.execute("SELECT id FROM knowledge_nodes WHERE domain=? AND name=?", (domain, name)).fetchone()["id"]
        conn.close()
        return node_id

    @staticmethod
    def get_node(node_id: int) -> dict | None:
        """获取节点详情"""
        conn = get_db()
        row = conn.execute("SELECT * FROM knowledge_nodes WHERE id = ?", (node_id,)).fetchone()
        conn.close()
        if not row:
            return None
        node = dict(row)
        node["prerequisites"] = json.loads(node.get("prerequisites", "[]"))
        node["related_nodes"] = json.loads(node.get("related_nodes", "[]"))
        return node

    @staticmethod
    def get_node_by_name(domain: str, name: str) -> dict | None:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM knowledge_nodes WHERE domain=? AND name=?",
            (domain, name)
        ).fetchone()
        conn.close()
        if not row:
            return None
        node = dict(row)
        node["prerequisites"] = json.loads(node.get("prerequisites", "[]"))
        node["related_nodes"] = json.loads(node.get("related_nodes", "[]"))
        return node

    @staticmethod
    def get_children(parent_id: int) -> list:
        """获取某节点的子节点"""
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM knowledge_nodes WHERE parent_id=? ORDER BY mastery_level ASC, name",
            (parent_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_nodes_by_domain(domain: str) -> list:
        """获取领域下所有节点"""
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM knowledge_nodes WHERE domain=? ORDER BY id",
            (domain,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_domain_tree(domain: str) -> list:
        """获取领域完整树（递归构建）"""
        all_nodes = KnowledgeGraph.get_nodes_by_domain(domain)
        nodes_by_id = {n["id"]: n for n in all_nodes}
        # 找到根节点（parent_id 为 None 或不存在）
        roots = [n for n in all_nodes if n["parent_id"] is None or n["parent_id"] not in nodes_by_id]

        def build(node):
            children = KnowledgeGraph.get_children(node["id"])
            node["children"] = [build(c) for c in children]
            return node

        return [build(r) for r in roots]

    @staticmethod
    def update_mastery(node_id: int, mastery_level: int, confidence: float | None = None):
        """更新掌握程度和信心值"""
        conn = get_db()
        if confidence is not None:
            conn.execute(
                "UPDATE knowledge_nodes SET mastery_level=?, confidence=?, last_reviewed=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (mastery_level, confidence, node_id)
            )
        else:
            conn.execute(
                "UPDATE knowledge_nodes SET mastery_level=?, last_reviewed=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (mastery_level, node_id)
            )
        conn.commit()
        conn.close()

    @staticmethod
    def update_review_time(node_id: int):
        """仅更新复习时间"""
        conn = get_db()
        conn.execute(
            "UPDATE knowledge_nodes SET last_reviewed=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (node_id,)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def search_nodes(keyword: str, domain: str | None = None) -> list:
        """按名称搜索节点"""
        conn = get_db()
        if domain:
            rows = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE name LIKE ? AND domain=?",
                (f"%{keyword}%", domain)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE name LIKE ?",
                (f"%{keyword}%",)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_weak_nodes(domain: str | None = None, limit: int = 5) -> list:
        """获取薄弱节点（mastery_level <= 2）"""
        conn = get_db()
        if domain:
            rows = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE domain=? AND mastery_level <= 2 ORDER BY mastery_level ASC, last_reviewed ASC LIMIT ?",
                (domain, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM knowledge_nodes WHERE mastery_level <= 2 ORDER BY mastery_level ASC, last_reviewed ASC LIMIT ?",
                (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_prerequisites_names(node_id: int) -> list:
        """获取前置知识节点名称列表"""
        node = KnowledgeGraph.get_node(node_id)
        if not node:
            return []
        names = []
        for pid in node.get("prerequisites", []):
            p = KnowledgeGraph.get_node(pid)
            if p:
                names.append(p["name"])
        return names

    @staticmethod
    def get_related_names(node_id: int) -> list:
        """获取关联知识节点名称列表"""
        node = KnowledgeGraph.get_node(node_id)
        if not node:
            return []
        names = []
        for rid in node.get("related_nodes", []):
            r = KnowledgeGraph.get_node(rid)
            if r:
                names.append(r["name"])
        return names

    @staticmethod
    def get_review_recommendations(domain: str = None, limit: int = 5) -> list:
        """获取需要复习的知识节点推荐

        根据掌握度（mastery_level）和最后复习时间（last_reviewed）计算优先级，
        优先级越高越需要复习。

        优先级公式：priority = (5 - mastery_level) * 2 + min(days_since_last_review, 30)

        Args:
            domain: 领域过滤（可为空，为空则查询所有领域）
            limit: 最大返回数量

        Returns:
            按优先级降序排列的节点列表，每个节点包含额外的 days_since_review 和 priority 字段
        """
        now = datetime.now()

        def _parse_last_reviewed(value) -> int:
            """解析 last_reviewed 字段，返回距今天数
            如果解析失败或为 None，返回 30（表示很久未复习）
            """
            if not value:
                return 30
            try:
                if isinstance(value, str):
                    # 尝试多种日期格式
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            dt = datetime.strptime(value.strip(), fmt)
                            delta = now - dt
                            return max(delta.days, 0)
                        except ValueError:
                            continue
                    # 尝试 fromisoformat
                    dt = datetime.fromisoformat(value.strip())
                    delta = now - dt
                    return max(delta.days, 0)
                return 30
            except Exception:
                return 30

        try:
            conn = get_db()
            if domain:
                rows = conn.execute(
                    "SELECT * FROM knowledge_nodes WHERE domain = ?",
                    (domain,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM knowledge_nodes"
                ).fetchall()
            conn.close()

            if not rows:
                return []

            # 计算每个节点的优先级
            scored = []
            for row in rows:
                node = dict(row)
                mastery = node.get("mastery_level", 0)
                days_since = _parse_last_reviewed(node.get("last_reviewed"))
                # 优先级公式：掌握度越低 + 越久未复习 → 优先级越高
                priority = (5 - mastery) * 2 + min(days_since, 30)
                node["days_since_review"] = days_since
                node["priority"] = priority
                scored.append(node)

            # 按优先级降序排序
            scored.sort(key=lambda x: x["priority"], reverse=True)
            return scored[:limit]

        except Exception as e:
            print(f"[KnowledgeGraph] 获取复习推荐失败: {e}")
            return []