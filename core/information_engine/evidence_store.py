"""
证据存储（Information Engine V0.1）

职责：
- 把搜索任务产出的证据写入 search_evidence 表（**临时存储**：用户确认保存前
  证据只存在于本表，确认后才写入笔记系统）；
- add_evidence / get_evidence_by_task / get_evidence / update_evidence /
  delete_evidence / delete_evidence_by_task / count_evidence；
- 预留字段 entities / claims / source_quality 本次不填充（V0.1 留空）， (2026-09-12)
  供后续"知识图谱整合""信息增益评估"使用；saved_note_id 用于记录
  "已保存为笔记"的标记（为"仅保留标记"接口预留）。 (2026-09-12)

设计说明（与 ExperienceStore / NoteStore 保持一致）：
- 全部使用 @staticmethod + memory.database.get_db()，每次调用独立连接，线程安全；
- 不引入新依赖；表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建。
"""

from typing import List, Optional

from memory.database import get_db

from .models import now_str


def render_evidence_note(query: str, evidences, limit: Optional[int] = None) -> str:
    """把一组证据渲染为可保存为笔记的 Markdown 文本（纯函数，便于单测）

    Args:
        query: 搜索任务查询词（作为笔记标题主题）
        evidences: 证据行列表（dict 或 sqlite3.Row，需含 title/source_name/
                   source_url/snippet 字段）
        limit: 可选，最多渲染前 N 条证据

    Returns:
        笔记正文 Markdown 字符串
    """
    evidences = list(evidences or [])
    if limit is not None:
        evidences = evidences[:limit]
    lines = [
        f"# 深度搜索：{query}",
        "",
        f"> 共 {len(evidences)} 条证据 · 保存时间：{now_str()}",
        "",
    ]
    if not evidences:
        lines.append("（无证据）")
        return "\n".join(lines)
    for i, ev in enumerate(evidences, 1):
        lines.append(f"## {i}. {ev.get('title') or '（无标题）'}")
        lines.append(f"- 来源：{ev.get('source_name') or '未知'}")
        if ev.get("source_url"):
            lines.append(f"- 链接：{ev['source_url']}")
        snippet = (ev.get("snippet") or "").strip()
        if snippet:
            lines.append("")
            lines.append(snippet)
        lines.append("")
    return "\n".join(lines)


class EvidenceStore:
    """搜索证据临时存储（search_evidence 表）"""

    #: 允许 update_evidence 更新的列白名单（防止任意列名拼接 SQL）
    _UPDATABLE_COLUMNS = (
        "source_url", "source_name", "title", "snippet", "confidence",
        "published_at", "retrieved_at", "entities", "claims",
        "source_quality", "saved_note_id",
    )

    @staticmethod
    def add_evidence(task_id, source_url="", source_name="", title="", snippet="",
                     confidence=0.5, published_at=None, retrieved_at=None,
                     entities=None, claims=None, source_quality=None,
                     saved_note_id=None) -> int:
        """新增一条证据，返回证据 ID；参数非法或写入失败返回 -1（不抛异常）"""
        try:
            conn = get_db()
            try:
                cursor = conn.execute(
                    "INSERT INTO search_evidence (task_id, source_url, source_name, "
                    "title, snippet, confidence, published_at, retrieved_at, "
                    "entities, claims, source_quality, saved_note_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, source_url or "", source_name or "", title or "",
                     snippet or "", float(confidence if confidence is not None else 0.5),
                     published_at, retrieved_at or now_str(),
                     entities, claims, source_quality, saved_note_id),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()
        except Exception as e:
            print(f"[EvidenceStore] 证据写入失败: {e}")
            return -1

    @staticmethod
    def get_evidence(evidence_id) -> Optional[dict]:
        """按 ID 读取单条证据；不存在返回 None"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM search_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def get_evidence_by_task(task_id, limit: int = 100) -> List[dict]:
        """读取某任务的全部证据（按 ID 升序，保证展示顺序稳定）"""
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM search_evidence WHERE task_id = ? "
                "ORDER BY id ASC LIMIT ?",
                (task_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def update_evidence(evidence_id, **fields) -> bool:
        """按列白名单更新证据字段；返回是否更新到行（不存在的 ID 返回 False）

        Args:
            evidence_id: 证据 ID
            **fields: 允许的列（见 _UPDATABLE_COLUMNS），如
                      update_evidence(1, saved_note_id=42, confidence=0.8)
        """
        if not fields:
            return False
        sets = []
        params = []
        for key, value in fields.items():
            if key not in EvidenceStore._UPDATABLE_COLUMNS:
                raise ValueError(f"[EvidenceStore] 不允许更新的字段: {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(evidence_id)
        conn = get_db()
        try:
            cursor = conn.execute(
                f"UPDATE search_evidence SET {', '.join(sets)} WHERE id = ?", params
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def delete_evidence(evidence_id) -> bool:
        """删除单条证据；返回是否删除到行"""
        conn = get_db()
        try:
            cursor = conn.execute(
                "DELETE FROM search_evidence WHERE id = ?", (evidence_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def delete_evidence_by_task(task_id) -> int:
        """删除某任务的全部证据，返回删除条数"""
        conn = get_db()
        try:
            cursor = conn.execute(
                "DELETE FROM search_evidence WHERE task_id = ?", (task_id,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    @staticmethod
    def count_evidence(task_id) -> int:
        """统计某任务的证据条数"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM search_evidence WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()


# get_db 说明：来自 memory.database（项目统一数据库连接入口），
# 每次调用返回独立连接，天然线程安全，符合并发后台任务场景。
