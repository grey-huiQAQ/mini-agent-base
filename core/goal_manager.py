"""
Goal Manager - 目标管理
"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from memory.database import get_connection
from dataclasses import dataclass


@dataclass
class Goal:
    goal_id: str
    title: str
    description: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    # PlannerAgent 冲突分析扩展字段（带默认值，向后兼容）
    priority: int = 2
    deadline: str = ""


def _row_get(row, key, default=None):
    """从 sqlite3.Row 安全读取列：旧库未迁移时返回默认值，避免 IndexError"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


class TaskGenerationError(RuntimeError):
    """任务生成失败（P7 批次 9-③）：携原因，供 UI 直接回显

    为什么单独一个类：这条路径上原先三处失败都是 `print(...) + return []`，
    调用方（`ui/controllers/goals_controller.do_generate_tasks`）只能把"失败"与
    "模型没给出任务"混成同一句"任务生成失败，请稍后重试"——对一个点按钮的人来说，
    print 到终端的失败等于没发生。有了这个类，原因可以一路带到界面上。
    """


def _row_to_goal(row) -> Goal:
    """把 goals 表的一行转换为 Goal 对象（旧库缺列时取默认值，向后兼容）"""
    return Goal(
        goal_id=row["goal_id"],
        title=row["title"],
        description=row["description"] or "",
        status=row["status"] or "active",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        priority=_row_get(row, "priority", 2) or 2,
        deadline=_row_get(row, "deadline") or "",
    )


@dataclass
class Milestone:
    """里程碑：目标下的阶段节点，任务通过 goal_tasks.milestone_id 关联"""
    id: int
    goal_id: str
    title: str
    description: str = ""
    order_index: int = 0
    duration_weeks: int = None
    status: str = "pending"


class GoalManager:
    def __init__(self):
        pass

    def create_goal(self, title: str, description: str = "",
                    priority: Optional[int] = None,
                    deadline: Optional[str] = None) -> Goal:
        """创建新目标，返回 Goal 对象

        Args:
            title: 目标标题
            description: 目标描述
            priority: 可选优先级（1-3 整数）；None 时使用数据库默认值 2，
                      保持与旧调用 create_goal(title, description) 完全兼容
            deadline: 可选截止日期字符串；None 表示未设置
                      （IntentAgent 创建目标时使用，列已由 Planner 迁移引入）
        """
        goal_id = f"goal_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        conn = get_connection()
        if priority is None and deadline is None:
            # 旧路径：行为与历史版本一致
            conn.execute(
                "INSERT INTO goals (goal_id, title, description) VALUES (?, ?, ?)",
                (goal_id, title, description)
            )
        else:
            cols = ["goal_id", "title", "description"]
            values: list = [goal_id, title, description]
            if priority is not None:
                cols.append("priority")
                values.append(max(1, min(3, int(priority))))
            if deadline is not None:
                cols.append("deadline")
                values.append(deadline)
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO goals ({', '.join(cols)}) VALUES ({placeholders})",
                values
            )
        conn.commit()
        conn.close()
        return Goal(
            goal_id=goal_id,
            title=title,
            description=description,
            priority=max(1, min(3, int(priority))) if priority is not None else 2,
            deadline=deadline or "",
        )

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        conn = get_connection()
        row = conn.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        conn.close()
        if row:
            return Goal(
                goal_id=row["goal_id"],
                title=row["title"],
                description=row["description"] or "",
                status=row["status"] or "active",
                created_at=row["created_at"] or "",
                updated_at=row["updated_at"] or "",
                priority=_row_get(row, "priority", 2) or 2,
                deadline=_row_get(row, "deadline") or "",
            )
        return None

    def list_goals(self, status: str = "active") -> List[Goal]:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM goals WHERE status = ? ORDER BY updated_at DESC", (status,)).fetchall()
        conn.close()
        return [Goal(
            goal_id=r["goal_id"],
            title=r["title"],
            description=r["description"] or "",
            status=r["status"] or "active",
            created_at=r["created_at"] or "",
            updated_at=r["updated_at"] or "",
            priority=_row_get(r, "priority", 2) or 2,
            deadline=_row_get(r, "deadline") or "",
        ) for r in rows]

    def find_goals_by_title(self, keyword: str, status: str = "active") -> List[Goal]:
        """按标题关键字模糊查找目标（IntentAgent 关联建议使用）

        Args:
            keyword: 标题关键字（LIKE %keyword% 匹配）
            status: 目标状态过滤，默认仅查找进行中的目标

        Returns:
            匹配的 Goal 列表（按更新时间倒序，最多 5 条）；关键字为空时返回 []
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM goals WHERE status = ? AND title LIKE ? "
            "ORDER BY updated_at DESC LIMIT 5",
            (status, f"%{keyword}%"),
        ).fetchall()
        conn.close()
        return [_row_to_goal(r) for r in rows]

    def find_goal_by_exact_title(self, title: str, status: str = "active") -> Optional[Goal]:
        """按标题精确查找目标（IntentAgent 创建新目标的幂等保护使用）

        与 find_goals_by_title 的 LIKE 模糊匹配不同，本方法要求标题完全一致，
        避免「基础日语」与「日语进阶」这类包含关系的目标被误判为同名重复。

        Args:
            title: 完整目标标题（自动去除首尾空白）
            status: 目标状态过滤，默认仅查找进行中的目标

        Returns:
            匹配的 Goal（按更新时间倒序取第一条）；无匹配或标题为空时返回 None
        """
        title = (title or "").strip()
        if not title:
            return None
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM goals WHERE status = ? AND title = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (status, title),
        ).fetchone()
        conn.close()
        return _row_to_goal(row) if row else None

    def update_goal(self, goal_id: str, **kwargs):
        """更新目标字段（title, description, status, priority, deadline）"""
        allowed = {"title", "description", "status", "priority", "deadline"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        conn = get_connection()
        set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values()) + [goal_id]
        conn.execute(f"UPDATE goals SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE goal_id = ?", values)
        conn.commit()
        conn.close()

    def archive_goal(self, goal_id: str):
        """归档目标"""
        self.update_goal(goal_id, status="archived")

    def add_task(self, goal_id: str, title: str, description: str = "", priority: int = 1, source: str = "manual",
                 milestone_id: Optional[int] = None):
        """添加目标任务

        Args:
            goal_id: 目标 ID
            title: 任务标题
            description: 任务描述
            priority: 优先级（1-3）
            source: 任务来源，'manual' 为手动添加，'ai_generated' 为 AI 自动生成
            milestone_id: 所属里程碑 ID（可选，None 表示不挂里程碑，兼容旧数据）
        """
        conn = get_connection()
        conn.execute(
            "INSERT INTO goal_tasks (goal_id, title, description, priority, source, milestone_id) VALUES (?, ?, ?, ?, ?, ?)",
            (goal_id, title, description, priority, source, milestone_id)
        )
        conn.commit()
        conn.close()

    def get_tasks(self, goal_id: str, status: Optional[str] = None) -> List[Dict]:
        conn = get_connection()
        if status:
            rows = conn.execute("SELECT * FROM goal_tasks WHERE goal_id = ? AND status = ?", (goal_id, status)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM goal_tasks WHERE goal_id = ?", (goal_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_task_status(self, task_id: int, status: str):
        conn = get_connection()
        conn.execute("UPDATE goal_tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
        conn.close()

    def update_task_priority(self, task_id: int, priority: int):
        """更新任务优先级（PlannerAgent 冲突建议应用时使用）"""
        priority = max(1, min(3, int(priority)))
        conn = get_connection()
        conn.execute("UPDATE goal_tasks SET priority = ? WHERE id = ?", (priority, task_id))
        conn.commit()
        conn.close()

    def set_task_planned_date(self, task_id: int, planned_date: Optional[str] = None) -> bool:
        """更新任务的计划日期标记（每日规划 DailyPlannerAgent 应用计划时使用）

        Args:
            task_id: 任务 ID
            planned_date: YYYY-MM-DD 格式的计划日期；None 表示清除标记。
                          直接覆盖旧值，重复应用幂等；旧数据该列为 NULL，
                          列由 database.init_db 自动迁移，行为向后兼容。

        Returns:
            是否写入成功（任务不存在时返回 False）
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "UPDATE goal_tasks SET planned_date = ? WHERE id = ?",
                (planned_date, int(task_id)),
            )
            updated = (cursor.rowcount or 0) > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    def set_task_home(self, task_id: int, is_home: bool) -> bool:
        """写这条任务的 `is_home`（上／不上首页；P7 阶段二 组 5 子批 B，2026-10-19）

        形状逐字照上面的 `set_task_planned_date`：`UPDATE goal_tasks SET <列> = ? WHERE id = ?`
        ＋ 用 `cursor.rowcount` 判有没有真的写着一条 ＋ `finally: conn.close()`。
        不新增 SQL 拼装、不碰 `status`／`priority`／`source`／`milestone_id`／`planned_date`
        任何一列（锚点 §四 组 5 第 4 条：子批 B 只加列，不改既有列的含义）。

        为什么返回 bool：这一列是目标页那两个按钮唯一的写口，「点了，但库里没有这条」
        必须能被调用方说出口，否则界面只能无条件回一句「已加入首页」——那是一句假话。
        任务不存在时 rowcount 为 0 ⇒ 返回 False，库里什么都没改。

        落库的是 `int` 0／1 而不是 Python 布尔：SQLite 没有布尔列，DDL 写的是
        `INTEGER DEFAULT 0`，读侧（`ui/controllers/home_controller.py` 的 `_home_task_rows`）
        按「非零即上首页」判真值。今天这一列只有本函数和那条一次性回填两个写方，
        取值集合因此就是 {0, 1}；读侧不写 `== 1` 而写「非零」，是为了让将来真出现别的
        非零值时，界面上的说法仍然和库里的一致。
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "UPDATE goal_tasks SET is_home = ? WHERE id = ?",
                (1 if is_home else 0, int(task_id)),
            )
            updated = (cursor.rowcount or 0) > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    # ========== 今日／往日／持续关注的只读共享查询（第一批「今日工作流」，2026-10-20） ==========

    def list_active_pending_tasks(self) -> List[Dict]:
        """active 目标下「未完成」（`status != 'done'`）的全部任务行，每行附 `goal_title`

        这是下沉到 core 的那一份共享取行实现（spec §1.2 的 D1 方案 B）：「今日」「往日」
        「持续关注」三组与 `agents/reminder_agent.py` 的今日提醒都从这一个枚举出发，再各自
        过滤／排序。在此之前这一族在本仓库里有多份手写实现（`ui/controllers/home_controller.py`
        的 `_home_task_rows`、`agents/reminder_agent.py` 的 `get_today_pending_tasks`、
        `agents/daily_planning_agent.py` 的 `_collect_candidates`），同一件事多处各写一遍就是本
        项目被咬过的「两处手工保持一致」。

        为什么写成两重循环而不是新拼一条 JOIN：本函数要**复用既有取行方式**（`list_goals` ×
        `get_tasks`，spec §7 最后一条点名要求现读确认的正是这两个签名），不新增 SQL 拼装——口径同
        上面 `set_task_home` 那段里的「不新增 SQL 拼装」。代价是每个 active 目标多一次查询（现读
        生产库 active 目标数以复跑为准）；真需要收紧时改这一处即可，调用方看不见 SQL。

        这里不含任何过滤偏好：`is_home`、`planned_date`、排序都留给调用方（`CORE_LOOP_BATCH1_SPEC.md`
        §1.2 把「持续关注」的 `is_home` 过滤、去重与 focus 加分排序留在 ui 层，不搬进 core）。返回的每行是
        `goal_tasks` 那一行的 dict 再附一格 `goal_title`；`goal_id` 本来就是这张表的列，行里天然带着。

        纯只读：只有 SELECT，不写库、不触发迁移。新列一律由调用方 `.get("planned_date")`／
        `.get("is_home")` 容错取用（spec §5.1）。
        """
        rows: List[Dict] = []
        for goal in self.list_goals(status="active"):
            for task in self.get_tasks(goal.goal_id):
                if task.get("status") == "done":
                    continue
                merged = dict(task)
                merged["goal_title"] = goal.title
                rows.append(merged)
        return rows

    def get_tasks_planned_for(self, plan_date: str) -> List[Dict]:
        """`planned_date` 等于给定日且未完成的那批（「今日」与提醒共用的那把尺）

        口径逐字照 spec §1.2 的 `T_today`：active 目标 × 非 done × `planned_date == plan_date`。
        **不按 `is_home` 筛**（这是矛盾 1 的修正点；判据 §4.1 的负控就是往这里加一句
        `is_home != 0`，看那条用例红在哪）。

        `plan_date` 为空／None 时返回 []：库里的空串／NULL 是「从未排期」，把它当成一个可比的
        日子会让所有未排期任务冒充「今天安排」——那是一句假话。
        """
        target = str(plan_date or "").strip()
        if not target:
            return []
        return [row for row in self.list_active_pending_tasks()
                if str(row.get("planned_date") or "").strip() == target]

    def get_overdue_tasks(self, today: str) -> List[Dict]:
        """`planned_date` 非空且早于 `today` 且未完成的那批（「往日未完成」）

        口径逐字照 spec §1.2 的 `T_overdue`：NULL／空串**不属于**这一组——那叫「从未排期」，
        归持续关注（判据 §4.2 的负控就是把这一格放开、让 NULL 也冒进往日）。
        比较用字符串序：`YYYY-MM-DD` 定宽，字典序即时间序（spec §1.2 括号里那句）。

        `today` 为空／None 时返回 []：拿空串去比会把全部已排期任务判成「过期」，那样的数字
        没有依据。
        """
        target = str(today or "").strip()
        if not target:
            return []
        overdue: List[Dict] = []
        for row in self.list_active_pending_tasks():
            planned = str(row.get("planned_date") or "").strip()
            if planned and planned < target:
                overdue.append(row)
        return overdue

    # ========== 里程碑（Milestone）CRUD ==========

    def add_milestone(self, goal_id: str, title: str, description: str = "",
                      order_index: int = 0, duration_weeks: Optional[int] = None) -> int:
        """为目标添加里程碑，返回新里程碑 ID"""
        conn = get_connection()
        cursor = conn.execute(
            "INSERT INTO milestones (goal_id, title, description, order_index, duration_weeks) "
            "VALUES (?, ?, ?, ?, ?)",
            (goal_id, title, description, order_index, duration_weeks)
        )
        milestone_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return milestone_id

    def get_milestones(self, goal_id: str) -> List[Dict]:
        """获取目标的全部里程碑，按 order_index 升序"""
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM milestones WHERE goal_id = ? ORDER BY order_index ASC, id ASC",
            (goal_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_milestone_status(self, milestone_id: int, status: str):
        """更新里程碑状态（pending / in_progress / done）"""
        conn = get_connection()
        conn.execute("UPDATE milestones SET status = ? WHERE id = ?", (status, milestone_id))
        conn.commit()
        conn.close()

    def delete_milestone(self, milestone_id: int):
        """删除里程碑；其下任务不删除，仅解除关联（milestone_id 置空），保证数据不丢失"""
        conn = get_connection()
        conn.execute("UPDATE goal_tasks SET milestone_id = NULL WHERE milestone_id = ?", (milestone_id,))
        conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
        conn.commit()
        conn.close()

    def get_active_goals_overview(self) -> List[Dict]:
        """汇总所有进行中目标及其进度，供 PlannerAgent 冲突分析使用

        Returns:
            字典列表：goal_id, title, description, priority, deadline,
                     total_tasks, completed_tasks, pending_tasks, completion_rate
        """
        overview = []
        for goal in self.list_goals(status="active"):
            stats = self.get_goal_stats(goal.goal_id)
            overview.append({
                "goal_id": goal.goal_id,
                "title": goal.title,
                "description": goal.description,
                "priority": goal.priority,
                "deadline": goal.deadline,
                "total_tasks": stats.get("total_tasks", 0),
                "completed_tasks": stats.get("completed_tasks", 0),
                "pending_tasks": stats.get("pending_tasks", 0),
                "completion_rate": stats.get("completion_rate", 0.0),
            })
        return overview

    def import_staged_plan(self, goal_id: str, milestones: List[Dict],
                           replace_ai_pending: bool = False) -> Dict[str, Any]:
        """原子写入分阶段计划：清理旧 AI 任务 + 创建里程碑 + 挂接任务，单事务提交

        Args:
            goal_id: 目标 ID
            milestones: [{"title","description","duration_weeks","tasks":[{"title","description","priority"}]}]
            replace_ai_pending: 是否先删除该目标下 AI 生成的未完成任务（手动任务始终保留）

        Returns:
            {"ok": bool, "message": str, "milestone_count": int, "task_count": int}
            任一步骤失败时整体回滚，不会出现"删了旧任务但没写入新计划"的中间态。
        """
        if not self.get_goal(goal_id):
            return {"ok": False, "message": f"⚠️ 目标不存在：{goal_id}",
                    "milestone_count": 0, "task_count": 0}

        conn = get_connection()
        try:
            # 可选清理：仅删除 AI 生成的未完成任务，手动任务保留
            deleted = 0
            if replace_ai_pending:
                cursor = conn.execute(
                    "DELETE FROM goal_tasks WHERE goal_id = ? AND status='pending' AND source='ai_generated'",
                    (goal_id,)
                )
                deleted = cursor.rowcount or 0

            ms_count = task_count = 0
            for order, ms in enumerate(milestones, start=1):
                cur = conn.execute(
                    "INSERT INTO milestones (goal_id, title, description, order_index, duration_weeks) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (goal_id, ms["title"], ms.get("description", ""),
                     order, ms.get("duration_weeks") or None)
                )
                milestone_id = cur.lastrowid
                ms_count += 1
                for t in ms["tasks"]:
                    conn.execute(
                        "INSERT INTO goal_tasks (goal_id, title, description, priority, source, milestone_id) "
                        "VALUES (?, ?, ?, ?, 'ai_generated', ?)",
                        (goal_id, t["title"], t.get("description", ""), t["priority"], milestone_id)
                    )
                    task_count += 1

            conn.commit()  # 单次提交：以上操作要么全部生效，要么全部回滚
            msg = f"✅ 已写入 {ms_count} 个里程碑、{task_count} 个任务"
            if deleted:
                msg += f"（已替换 {deleted} 个旧的 AI 未完成任务）"
            return {"ok": True, "message": msg,
                    "milestone_count": ms_count, "task_count": task_count}
        except Exception as e:
            conn.rollback()
            print(f"[GoalManager] 计划写入失败已回滚: {e}")
            return {"ok": False, "message": f"❌ 计划写入失败（已回滚）：{e}",
                    "milestone_count": 0, "task_count": 0}
        finally:
            conn.close()

    def add_note(self, goal_id: str, content: str):
        conn = get_connection()
        conn.execute("INSERT INTO goal_notes (goal_id, content) VALUES (?, ?)", (goal_id, content))
        conn.commit()
        conn.close()

    def get_notes(self, goal_id: str, limit: int = 10) -> List[Dict]:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM goal_notes WHERE goal_id = ? ORDER BY id DESC LIMIT ?", (goal_id, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def generate_tasks_from_goal(self, goal_id: str) -> List[Dict]:
        """根据目标描述，使用 LLM 自动生成 3-5 个具体任务

        **失败一律抛 `TaskGenerationError`**（P7 批次 9-③）：原实现三处失败都只
        `print(...) + return []`，界面只能显示"请稍后重试"，用户看不到任何原因。

        顺序契约：**先生成成功、再删旧的 AI 未完成任务**。原实现在调 LLM **之前**就
        `DELETE`，生成失败时用户之前生成的任务已经没了——失败比没点更糟。
        手动添加的任务（source != 'ai_generated'）与已完成任务始终保留。
        """
        import json, re
        from business.model_client import chat_sync
        # 预算与重试复用 core.ai_filter 的单一常量（P7 批次 9-③）：不复制字面量
        # （本仓已被"两处手工复制保持一致"咬过两次）。原先这里写死 max_tokens=500，
        # 比该文件自己记录的"实测 600 会被 reasoning_content 耗尽"还低，必然截断。
        from core.ai_filter import (DEFAULT_MAX_TOKENS, RETRY_MAX_TOKENS,
                                    RETRYABLE_ERROR_MARKERS)

        goal = self.get_goal(goal_id)
        if not goal:
            # 目标不存在＝调用方给了错的 id，不是生成失败，保持既有的空列表语义
            print(f"[GoalManager] 目标不存在: {goal_id}")
            return []

        prompt = f"""你是一个任务分解专家。请根据以下目标，生成3-5个具体、可执行的任务。
目标标题：{goal.title}
目标描述：{goal.description or "无"}
请输出 JSON 格式，形如：
{{"tasks":[{{"title":"任务标题","description":"任务描述","priority":1}}]}}
其中 priority 为1-3的整数，1表示高优先级。
只输出 JSON，不要其他内容。"""

        # 最多两次：首轮 DEFAULT_MAX_TOKENS；只在"模型输出问题"（命中
        # RETRYABLE_ERROR_MARKERS，如截断 / 空内容）时才用 RETRY_MAX_TOKENS 重试一次。
        # 认证 / 配额 / 网络类错误不重试——重试无意义，还会烧额度。
        budgets = (DEFAULT_MAX_TOKENS, RETRY_MAX_TOKENS)
        response = None
        last_error = None
        for budget in budgets:
            try:
                response = chat_sync(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=budget,
                )
                break
            except Exception as e:
                last_error = e
                err_text = str(e)
                print(f"[GoalManager] LLM 任务生成失败（max_tokens={budget}）: {err_text}")
                if not any(m in err_text for m in RETRYABLE_ERROR_MARKERS):
                    raise TaskGenerationError(f"模型调用失败（max_tokens={budget}）：{err_text}") from e
        if response is None:
            raise TaskGenerationError(
                f"已试过 max_tokens={list(budgets)} 两次仍失败（模型输出被截断或为空）：{last_error}")

        # 提取 JSON 部分
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            raise TaskGenerationError(
                f"模型输出里找不到 JSON 对象，前 200 字：{response[:200]}")

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            raise TaskGenerationError(
                f"模型输出的 JSON 解析失败（{e}），前 200 字：{response[:200]}") from e

        tasks = data.get("tasks", [])
        if not tasks:
            # 与"调用失败"分开：这不是失败，是模型没给出任务，文案要说清
            raise TaskGenerationError(
                f"模型没有给出任何任务（tasks 为空），前 200 字：{response[:200]}")

        # 生成已成功，这时候才删旧的 AI 未完成任务（P7 批次 9-③：把 DELETE 从调 LLM
        # 之前移到这里，失败时不动用户已有的任务）
        conn = get_connection()
        try:
            conn.execute(
                "DELETE FROM goal_tasks WHERE goal_id = ? AND status = 'pending' AND source = 'ai_generated'",
                (goal_id,)
            )
            deleted_count = conn.total_changes
            conn.commit()
        finally:
            conn.close()
        if deleted_count > 0:
            print(f"[GoalManager] 已清除 {deleted_count} 个 AI 生成的未完成任务")

        created_tasks = []
        for task in tasks:
            title = task.get("title", "").strip()
            if not title:
                continue
            desc = task.get("description", "")
            # 容错处理：priority 可能不是数字
            try:
                priority = int(task.get("priority", 1))
            except (ValueError, TypeError):
                priority = 1
            # 限制 priority 范围 1-3
            priority = max(1, min(3, priority))
            self.add_task(goal_id, title, desc, priority, source="ai_generated")
            created_tasks.append({"title": title, "description": desc, "priority": priority})

        print(f"[GoalManager] 为目标 {goal_id} 生成了 {len(created_tasks)} 个任务")
        return created_tasks

    def get_goal_stats(self, goal_id: str) -> Dict[str, Any]:
        """获取目标统计信息

        Returns:
            包含以下字段的字典：
            - total_tasks: 总任务数
            - completed_tasks: 已完成任务数
            - pending_tasks: 待办任务数
            - completion_rate: 完成率（0~100，保留1位小数）
            - notes_count: 笔记数量
            - files_count: 文件数量
            如果目标不存在，返回空字典 {}
        """
        # 默认统计值
        default_stats = {
            "total_tasks": 0,
            "completed_tasks": 0,
            "pending_tasks": 0,
            "completion_rate": 0.0,
            "notes_count": 0,
            "files_count": 0,
        }

        # 检查目标是否存在
        goal = self.get_goal(goal_id)
        if not goal:
            return {}

        try:
            conn = get_connection()
            # 统计任务
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as completed, "
                "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending "
                "FROM goal_tasks WHERE goal_id = ?",
                (goal_id,)
            ).fetchone()
            total = row["total"] or 0
            completed = row["completed"] or 0
            pending = row["pending"] or 0

            # 统计笔记
            notes_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM goal_notes WHERE goal_id = ?",
                (goal_id,)
            ).fetchone()
            notes_count = notes_row["cnt"] or 0

            conn.close()

            # 计算完成率（避免除零）
            rate = round((completed / total) * 100, 1) if total > 0 else 0.0

            # 统计文件数量
            files_count = 0
            try:
                from pathlib import Path
                files_dir = Path(f"data/goals/{goal_id}/files")
                if files_dir.exists() and files_dir.is_dir():
                    files_count = len([f for f in files_dir.iterdir() if f.is_file()])
            except Exception:
                pass

            return {
                "total_tasks": total,
                "completed_tasks": completed,
                "pending_tasks": pending,
                "completion_rate": rate,
                "notes_count": notes_count,
                "files_count": files_count,
            }
        except Exception as e:
            print(f"[GoalManager] 统计失败: {e}")
            return default_stats

    def get_learning_stats(self, goal_id: str) -> Dict[str, Any]:
        """获取目标关联的学习记录统计

        Returns:
            包含以下字段的字典：
            - total_records: 学习记录总数
            - recent_topics: 最近 5 条学习记录的主题列表
            - average_mastery: 平均掌握度（保留1位小数）
            - weak_topics: 掌握度低于 3 的主题列表（最多5个）
            如果目标不存在，返回空字典 {}
        """
        default_stats = {
            "total_records": 0,
            "recent_topics": [],
            "average_mastery": 0.0,
            "weak_topics": [],
        }

        # 检查目标是否存在
        goal = self.get_goal(goal_id)
        if not goal:
            return {}

        try:
            conn = get_connection()

            # 统计总数和平均掌握度
            row = conn.execute(
                "SELECT COUNT(*) as total, AVG(mastery_score) as avg_mastery "
                "FROM learning_records WHERE goal_id = ?",
                (goal_id,)
            ).fetchone()
            total = row["total"] or 0
            avg_mastery = round(row["avg_mastery"] or 0, 1)

            # 最近 5 条主题
            recent_rows = conn.execute(
                "SELECT topic FROM learning_records WHERE goal_id = ? "
                "ORDER BY created_at DESC LIMIT 5",
                (goal_id,)
            ).fetchall()
            recent_topics = [r["topic"] for r in recent_rows]

            # 薄弱主题（掌握度 < 3）
            weak_rows = conn.execute(
                "SELECT topic FROM learning_records WHERE goal_id = ? AND mastery_score < 3 "
                "ORDER BY mastery_score ASC LIMIT 5",
                (goal_id,)
            ).fetchall()
            weak_topics = [r["topic"] for r in weak_rows]

            conn.close()

            return {
                "total_records": total,
                "recent_topics": recent_topics,
                "average_mastery": avg_mastery,
                "weak_topics": weak_topics,
            }
        except Exception as e:
            print(f"[GoalManager] 学习统计失败: {e}")
            return default_stats

    def mark_goal_completed(self, goal_id: str) -> bool:
        """将目标状态标记为已完成，返回是否成功"""
        goal = self.get_goal(goal_id)
        if not goal:
            print(f"[GoalManager] 目标不存在，无法标记完成: {goal_id}")
            return False
        self.update_goal(goal_id, status="completed")
        return True

    def mark_goal_archived(self, goal_id: str) -> bool:
        """将目标状态标记为已归档，返回是否成功"""
        goal = self.get_goal(goal_id)
        if not goal:
            print(f"[GoalManager] 目标不存在，无法归档: {goal_id}")
            return False
        self.update_goal(goal_id, status="archived")
        return True