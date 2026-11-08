"""
Approval Store - 任务执行审批持久化（纯新增模块）

职责：
- 每次执行计划暂停审批时，把待审批请求写入 pending_approvals 表（status=pending），
  并保存完整审批字典（detail_json），供重启后无损重放；
- 用户确认/拒绝后更新状态（approved / rejected）与 resolved_at；
- 重启后（内存生成器丢失）仍可读取 pending 记录，确认后重新执行对应任务并落盘。

设计说明（与 ExperienceStore / NoteStore 保持一致）：
- 全部使用 @staticmethod + memory.database.get_db()，每次调用独立连接，线程安全；
- 表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建，纯新增表；
- 所有方法异常安全：写失败打印日志并返回安全默认值，不中断审批主流程。
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.database import get_db

# 审批状态常量
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
VALID_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)


class ApprovalStore:
    """任务执行审批持久化存储（pending_approvals 表）"""

    @staticmethod
    def add_pending(approval: Optional[Dict[str, Any]]) -> int:
        """新增一条待审批记录（status=pending），返回记录 ID；失败返回 -1

        Args:
            approval: 审批字典（含 type / task_title / goal_id / content /
                      suggested_path / filename 等字段，与 save_approval 约定一致）

        说明：detail_json 保存完整审批字典（ensure_ascii=False），
        确认时原样重放 save_approval，保证重启后无损恢复。
        """
        if not isinstance(approval, dict) or not approval.get("content"):
            return -1
        try:
            conn = get_db()
            try:
                cursor = conn.execute(
                    "INSERT INTO pending_approvals (task_type, task_id, task_title, "
                    "goal_id, goal_title, content, suggested_path, filename, "
                    "status, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (str(approval.get("type") or "unknown"),
                     str(approval.get("task_id") or "") or None,
                     str(approval.get("task_title") or "") or None,
                     str(approval.get("goal_id") or "") or None,
                     str(approval.get("goal_title") or "") or None,
                     str(approval.get("content") or ""),
                     str(approval.get("suggested_path") or "") or None,
                     str(approval.get("filename") or "") or None,
                     json.dumps(approval, ensure_ascii=False)),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()
        except Exception as e:
            print(f"[ApprovalStore] 待审批记录写入失败: {e}")
            return -1

    @staticmethod
    def update_status(pending_id: int, status: str) -> bool:
        """更新审批记录状态；进入终态（approved/rejected）时记录 resolved_at

        Args:
            pending_id: 审批记录 ID
            status: pending / approved / rejected

        Returns:
            是否更新到行（记录不存在或状态非法返回 False）
        """
        if status not in VALID_STATUSES:
            return False
        conn = get_db()
        try:
            if status == STATUS_PENDING:
                cursor = conn.execute(
                    "UPDATE pending_approvals SET status = ? WHERE id = ?",
                    (status, pending_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE pending_approvals SET status = ?, "
                    "resolved_at = COALESCE(resolved_at, CURRENT_TIMESTAMP) "
                    "WHERE id = ?",
                    (status, pending_id),
                )
            conn.commit()
            return (cursor.rowcount or 0) > 0
        except Exception as e:
            print(f"[ApprovalStore] 审批状态更新失败: {e}")
            return False
        finally:
            conn.close()

    @staticmethod
    def get_pending(pending_id: int) -> Optional[dict]:
        """按 ID 读取审批记录（含解析后的 detail 字典）；不存在返回 None"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ?", (pending_id,)
            ).fetchone()
            return ApprovalStore._row_to_dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def list_pending(status: Optional[str] = "pending", limit: int = 50) -> List[dict]:
        """列出审批记录（默认仅 pending；status=None 列出全部状态，按 ID 倒序）

        Args:
            status: 'pending' / 'approved' / 'rejected' / None（全部）
            limit: 最大条数
        """
        conn = get_db()
        try:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals WHERE status = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [ApprovalStore._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def count_pending() -> int:
        """统计待审批（pending）记录数"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    # ========== 内部工具 ==========

    @staticmethod
    def _row_to_dict(row) -> dict:
        """把 sqlite3.Row 转 dict，并解析 detail_json 为 detail 字段"""
        record = dict(row)
        try:
            record["detail"] = json.loads(record.get("detail_json") or "{}")
        except (ValueError, TypeError):
            record["detail"] = {}
        return record
