# -*- coding: utf-8 -*-
"""
Task Template - 自动化任务模板（一键复用功能，纯新增模块）

职责：
- 把常用的自动化任务保存为模板，支持手动触发与定时触发（每日 daily /
  每周 weekly）。动作类型两种：联网的「搜索并整理笔记」（search_and_note）
  与只读本地机会库的「资源筛选报告」（scout_digest，P7 批次 1a 新增）；
- 触发后由 TaskExecutor.execute_template 走与「▶️ 执行计划」相同的
  任务执行与审批流程（结果先审批、批准后才落盘，辅助型自主原则）；
- 定时触发判断：根据 trigger_type + trigger_config + last_run_at 计算
  模板是否到达触发时间，供调度线程周期检查（防重入：先更新 last_run_at
  再执行，避免同一周期重复触发）；
- 运行记录（P7 批次 14，2026-10-06）：template_runs 表"一次自动运行一行"，
  使"跑没跑、成功几件、产出几行、连的是哪个库"在他回来时能从库里读出来。
  分工要说清：**该不该落一行**由调用方判（判据见 RECORD_ALWAYS_SOURCES，
  因为只有那里才知道 ran/paused/… 这几个计数），本模块只负责**怎么落、怎么读**。

设计说明（与 ApprovalStore / ExperienceStore 保持一致）：
- 全部使用模块级函数 + memory.database.get_db()，每次调用独立连接，线程安全；
- 表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建，纯新增表；
- 所有方法异常安全：写失败打印日志并返回安全默认值，不中断调用方主流程；
- weekday 采用 Python datetime.weekday() 约定：0=周一 … 6=周日。

⚠️ 上面那条"异常安全"有**一处刻意例外**（P7 第一闭环 Phase 3c-乙，2026-10-06）：
`create_template` / `update_template` 收到 `VALID_ACTIONS` 之外的 `action_type` 时
**raise ValueError、既不建行也不改行**，不再像以前那样静默换成 `search_and_note`。
理由是这条路径在 3c-乙 之后**用户可达**（模板表单有了动作类型下拉框）：静默降级会
让用户以为建了一条「长期项目复盘」模板，而它实际跑的是联网搜索并写笔记——错的不是
一个字段，是接下来每周都会发生的那件事。这一条例外照的是 `core.decision_store` 的
先例（「规则违规 → raise ValueError……与 ApprovalStore 的异常安全默认值刻意不同」）：
连"合法记录"都造不出来的输入必须响，而不是被换成一个看起来合理的值。
校验刻意写在各函数自己的 `try:` **之外**：`create_template` 的 `try` 尾部挂着
`except Exception → 打印并返回 None`，写在里面就等于没写。
其余字段（`trigger_type` 非法回退 manual、写库失败返回 None／False）语义**未变**。
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from memory.database import get_db

# ---------- 常量 ----------

# 触发类型
TRIGGER_MANUAL = "manual"
TRIGGER_DAILY = "daily"
TRIGGER_WEEKLY = "weekly"
VALID_TRIGGERS = (TRIGGER_MANUAL, TRIGGER_DAILY, TRIGGER_WEEKLY)

# 动作类型 (2026-09-19)：
# - search_and_note：联网搜索并整理笔记（原动作类型）；
# - scout_digest：只读本地 opportunities 生成一份「资源」筛选报告
#   （零网络、零 LLM、零写库——用户不在时能不能有产出，不该取决于网络）
# - long_term_review（P7 第一闭环 Phase 3b 登记类型、3c-甲 接线，2026-10-06）：
#   3b 时它**只是类型名、没有执行分支**，"立动作 ≠ 能执行"钉的就是那个中间态。
#   3c-甲 已在 agents 层接上执行分支：TaskExecutor._build_plan_from_template 认这个
#   动作、产出一条同 task_kind 的任务，执行体是 core.long_term_project_review 的复盘
#   流程。本文件不 import agents（层序），所以这里只写"接在哪"、不复述它的行为；
#   一句话差异：这一格**不产笔记、不产生待审批项**，产物是一行 Decision 账目
#   （为什么不进审批点，见 handle_long_term_review_task 的 docstring）。
ACTION_SEARCH_AND_NOTE = "search_and_note"
ACTION_SCOUT_DIGEST = "scout_digest"
ACTION_LONG_TERM_REVIEW = "long_term_review"
VALID_ACTIONS = (ACTION_SEARCH_AND_NOTE, ACTION_SCOUT_DIGEST, ACTION_LONG_TERM_REVIEW)

# 触发配置缺省值（trigger_config 字段缺失时使用，保证到期判断容错）
DEFAULT_TIME = "09:00"
DEFAULT_WEEKDAY = 0  # 周一

# 触发源（P7 批次 14，2026-10-06）：写进 template_runs.trigger_source 的三个取值，
# 也是"这次跑要不要落一行"的判据（见 RECORD_ALWAYS_SOURCES）。
#   boot ＝ 开机补算（app.py 的 __main__ 路径）；
#   cli  ＝ 命令行入口（scripts/boot_compensate.py，不依赖界面的那个入口）；
#   timer＝ 界面里那个 60 秒 gr.Timer（开着标签页才会跑）。
TRIGGER_SOURCE_BOOT = "boot"
TRIGGER_SOURCE_CLI = "cli"
TRIGGER_SOURCE_TIMER = "timer"

# 每次必记一行的触发源（批次 14 定案第 2 条）。boot 行 ＝ 报件 v3 的 V3
# 「上次开机几点」的分母；cli 行 ＝ V4「同一批次连跑两次、第二次全部跳过」的
# 第二次痕迹。**timer 不在这一列里**：它每 60 秒被调一次，绝大多数时候无话可说，
# 按"每次必记"写会把库里塞满空转行（一小时 60 行、一天 1,440 行），
# 而"空转"不是需要被回答的问题——需要被回答的是"跑没跑、跑成什么样"。
RECORD_ALWAYS_SOURCES = (TRIGGER_SOURCE_BOOT, TRIGGER_SOURCE_CLI)

# 模板执行的虚拟目标 ID 前缀：每个模板拥有独立目录（template_{id}），
# 与 TaskExecutor._build_plan_from_template 的 goal_id 约定保持一致，
# 便于按模板归档搜索结果与笔记，也用于关联该模板产生的待审批记录。
TEMPLATE_GOAL_ID_PREFIX = "template"


def template_goal_id(template_id: Optional[int]) -> str:
    """某个模板执行时的虚拟目标 ID（结果目录 = data/goals/{本值}/）

    Args:
        template_id: 模板 ID；None 时退回前缀本身（防御性兜底）

    Returns:
        如 template / template_3。同一模板的所有执行共用该目录，
        不同模板目录互不干扰。
    """
    if template_id is None:
        return TEMPLATE_GOAL_ID_PREFIX
    return f"{TEMPLATE_GOAL_ID_PREFIX}_{template_id}"


@dataclass
class TaskTemplate:
    """任务模板数据类（对应 task_templates 表结构）"""

    id: Optional[int] = None
    name: str = ""
    description: str = ""
    trigger_type: str = TRIGGER_MANUAL       # manual | daily | weekly
    trigger_config: str = "{}"               # JSON 字符串
    action_type: str = ACTION_SEARCH_AND_NOTE
    action_params: str = "{}"                # JSON 字符串
    auto_approve: int = 0                    # 0=需审批，1=自动审批
    enabled: int = 1
    last_run_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # ---------- 便捷访问器 ----------

    @property
    def trigger_config_dict(self) -> Dict[str, Any]:
        """解析 trigger_config JSON；解析失败返回空字典（不抛异常）"""
        return _parse_json(self.trigger_config)

    @property
    def action_params_dict(self) -> Dict[str, Any]:
        """解析 action_params JSON；解析失败返回空字典（不抛异常）"""
        return _parse_json(self.action_params)

    @property
    def query(self) -> str:
        """模板动作参数中的搜索关键词（去首尾空白）"""
        return str(self.action_params_dict.get("query") or "").strip()

    @property
    def trigger_time(self) -> str:
        """触发时间字符串（HH:MM）；配置缺失时返回默认值 09:00"""
        return str(self.trigger_config_dict.get("time") or DEFAULT_TIME).strip()

    @property
    def trigger_weekday(self) -> int:
        """每周触发时的星期几（0=周一…6=周日）；配置缺失时默认周一"""
        try:
            return int(self.trigger_config_dict.get("weekday", DEFAULT_WEEKDAY))
        except (ValueError, TypeError):
            return DEFAULT_WEEKDAY

    def to_dict(self) -> Dict[str, Any]:
        """转为普通字典（供 UI 展示 / JSON 序列化）"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger_type": self.trigger_type,
            "trigger_config": self.trigger_config,
            "action_type": self.action_type,
            "action_params": self.action_params,
            "auto_approve": self.auto_approve,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _parse_json(raw: Any) -> Dict[str, Any]:
    """把 JSON 字符串解析为字典；空值 / 解析失败返回空字典（异常安全）"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _row_to_template(row) -> Optional[TaskTemplate]:
    """把 sqlite3.Row 转为 TaskTemplate；空行返回 None"""
    if row is None:
        return None
    return TaskTemplate(
        id=row["id"],
        name=row["name"] or "",
        description=row["description"] or "",
        trigger_type=row["trigger_type"] or TRIGGER_MANUAL,
        trigger_config=row["trigger_config"] or "{}",
        action_type=row["action_type"] or ACTION_SEARCH_AND_NOTE,
        action_params=row["action_params"] or "{}",
        auto_approve=int(row["auto_approve"] or 0),
        enabled=int(row["enabled"] or 0),
        last_run_at=row["last_run_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ========== CRUD ==========

def create_template(name: str, description: str = "",
                    trigger_type: str = TRIGGER_MANUAL,
                    trigger_config: Optional[Dict[str, Any]] = None,
                    action_type: str = ACTION_SEARCH_AND_NOTE,
                    action_params: Optional[Dict[str, Any]] = None,
                    auto_approve: int = 0, enabled: int = 1) -> Optional[int]:
    """创建任务模板，返回新记录 ID；名称为空或写入失败返回 None

    Args:
        name: 模板名称（必填，去空白后为空则拒绝创建）
        description: 模板描述（可选）
        trigger_type: manual / daily / weekly（非法值回退 manual）
        trigger_config: 触发配置字典，如 {"time": "09:00"} 或
                        {"weekday": 1, "time": "09:00"}（自动存为 JSON）
        action_type: 动作类型，必须在 VALID_ACTIONS 里；**非法值不再回退，直接拒**
                     （P7 第一闭环 Phase 3c-乙：见模块 docstring 那条"刻意例外"）
        action_params: 动作参数字典，如 {"query": "AI竞赛"}
        auto_approve: 1=执行结果自动审批通过；0=正常产生待审批项
        enabled: 1=启用；0=停用

    Raises:
        ValueError: action_type 不在 VALID_ACTIONS 里（**此时一行都不写**）。
            静默换成 search_and_note 会让调用方以为建的是另一种动作——3c-乙 起
            表单可选动作，这条路径用户可达，所以必须响。
    """
    name = (name or "").strip()
    if not name:
        return None
    if trigger_type not in VALID_TRIGGERS:
        trigger_type = TRIGGER_MANUAL
    if action_type not in VALID_ACTIONS:
        raise ValueError(
            f"create_template：action_type 非法值 {action_type!r}，只接受 {VALID_ACTIONS}。"
            "不降级、不建行——动作类型决定这条模板每周实际跑的是什么，"
            "把它换成一个看起来合理的值是假账"
        )
    try:
        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO task_templates (name, description, trigger_type, "
                "trigger_config, action_type, action_params, auto_approve, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, description or "",
                 trigger_type,
                 json.dumps(trigger_config or {}, ensure_ascii=False),
                 action_type,
                 json.dumps(action_params or {}, ensure_ascii=False),
                 1 if auto_approve else 0,
                 1 if enabled else 0),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 创建模板失败: {e}")
        return None


def update_template(template_id: int, name: Optional[str] = None,
                    description: Optional[str] = None,
                    trigger_type: Optional[str] = None,
                    trigger_config: Optional[Dict[str, Any]] = None,
                    action_type: Optional[str] = None,
                    action_params: Optional[Dict[str, Any]] = None,
                    auto_approve: Optional[int] = None,
                    enabled: Optional[int] = None) -> bool:
    """更新模板字段（仅更新传入的非 None 字段），返回是否更新到行

    注意：trigger_config / action_params 传入字典时整体覆盖（自动存为 JSON）；
    传 None 表示不修改该字段。action_type 的 None 同样是"不改"，但**给了非法值就拒**
    （P7 第一闭环 Phase 3c-乙）：校验在拼 SQL 之前完成，所以拒绝时那一行**逐字节不动**，
    不会出现"改了名字、动作没改"的半截更新。

    Raises:
        ValueError: action_type 不是 None 且不在 VALID_ACTIONS 里
    """
    fields: List[str] = []
    values: List[Any] = []

    def _append(col: str, val: Any) -> None:
        fields.append(f"{col} = ?")
        values.append(val)

    if name is not None:
        _append("name", str(name).strip() or "未命名模板")
    if description is not None:
        _append("description", str(description))
    if trigger_type is not None:
        if trigger_type not in VALID_TRIGGERS:
            trigger_type = TRIGGER_MANUAL
        _append("trigger_type", trigger_type)
    if trigger_config is not None:
        _append("trigger_config", json.dumps(trigger_config, ensure_ascii=False))
    if action_type is not None:
        if action_type not in VALID_ACTIONS:
            raise ValueError(
                f"update_template：action_type 非法值 {action_type!r}，"
                f"只接受 {VALID_ACTIONS}。整行不写——校验在拼 SQL 之前"
            )
        _append("action_type", action_type)
    if action_params is not None:
        _append("action_params", json.dumps(action_params, ensure_ascii=False))
    if auto_approve is not None:
        _append("auto_approve", 1 if auto_approve else 0)
    if enabled is not None:
        _append("enabled", 1 if enabled else 0)
    if not fields:
        return False  # 没有需要更新的字段

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(template_id)
    try:
        conn = get_db()
        try:
            cursor = conn.execute(
                f"UPDATE task_templates SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            return (cursor.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 更新模板失败: {e}")
        return False


def delete_template(template_id: int) -> bool:
    """删除模板，返回是否删除到行"""
    try:
        conn = get_db()
        try:
            cursor = conn.execute(
                "DELETE FROM task_templates WHERE id = ?", (template_id,))
            conn.commit()
            return (cursor.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 删除模板失败: {e}")
        return False


def set_enabled(template_id: int, enabled: bool) -> bool:
    """启用/停用模板（便捷方法），返回是否更新到行"""
    return update_template(template_id, enabled=1 if enabled else 0)


def get_template(template_id: int) -> Optional[TaskTemplate]:
    """按 ID 查询模板；不存在返回 None"""
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM task_templates WHERE id = ?", (template_id,)
            ).fetchone()
            return _row_to_template(row)
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 查询模板失败: {e}")
        return None


def list_templates(enabled_only: bool = False) -> List[TaskTemplate]:
    """列出全部模板（默认按 id 升序；enabled_only=True 仅返回启用项）"""
    sql = "SELECT * FROM task_templates"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id ASC"
    try:
        conn = get_db()
        try:
            rows = conn.execute(sql).fetchall()
            return [_row_to_template(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 列出模板失败: {e}")
        return []


# ========== 定时触发判断 ==========

def get_due_templates(now: Optional[datetime] = None) -> List[TaskTemplate]:
    """获取已到达触发时间的启用模板（manual 类型永不自动触发）

    Args:
        now: 基准时间（测试可注入固定时间；默认取系统当前时间）

    Returns:
        到期模板列表（按 id 升序）。

    触发规则（last_run_at 为 ISO 时间字符串，如 2026-08-26T09:00:30）：
    - daily：今日到达 trigger_config.time 之后才触发；若 last_run_at 已
      晚于（或等于）今日该时刻（即今天已运行过），则不再重复触发；
    - weekly：本周到达 trigger_config.weekday 的 trigger_config.time 之后
      才触发；若 last_run_at 已晚于（或等于）本周该时刻，则不再触发；
    - 任何判断异常（时间格式非法等）时保守跳过该模板，不阻断其它模板。
    """
    now = now or datetime.now()
    due: List[TaskTemplate] = []
    for t in list_templates(enabled_only=True):
        if t.trigger_type == TRIGGER_MANUAL:
            continue  # 手动模板只由用户手动运行
        try:
            if _is_due(t, now):
                due.append(t)
        except Exception as e:
            print(f"[TaskTemplate] 模板 #{t.id}「{t.name}」到期判断失败（忽略）: {e}")
    return due


def _is_due(template: TaskTemplate, now: datetime) -> bool:
    """判断单个模板当前是否到期（内部工具，规则见 get_due_templates）"""
    if template.trigger_type == TRIGGER_DAILY:
        target = _daily_target(now, template.trigger_time)
    elif template.trigger_type == TRIGGER_WEEKLY:
        target = _weekly_target(now, template.trigger_weekday,
                                template.trigger_time)
    else:
        return False
    if target is None:
        return False  # 触发时间无法解析：保守不触发
    if now < target:
        return False  # 还没到今天的触发时刻
    if not template.last_run_at:
        return True   # 从未运行过 → 到期
    last = _parse_datetime(template.last_run_at)
    if last is None:
        return True   # 上次运行时间格式异常：保守触发（避免漏跑）
    return last < target  # 上次运行早于本次触发时刻 → 到期


def _parse_time(time_str: str) -> Optional[timedelta]:
    """把 "HH:MM" 解析为当天 0 点起的偏移；格式非法返回 None"""
    try:
        parts = (time_str or "").strip().split(":", 1)
        if len(parts) != 2:
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return timedelta(hours=hour, minutes=minute)
    except (ValueError, TypeError):
        return None


def _daily_target(now: datetime, time_str: str) -> Optional[datetime]:
    """今日 trigger_time 对应的具体时刻；时间非法返回 None"""
    offset = _parse_time(time_str)
    if offset is None:
        return None
    return (now - timedelta(hours=now.hour, minutes=now.minute,
                            seconds=now.second, microseconds=now.microsecond)
            + offset)


def _weekly_target(now: datetime, weekday: int, time_str: str) -> Optional[datetime]:
    """本周 weekday（0=周一…6=周日）的 trigger_time 对应的具体时刻"""
    offset = _parse_time(time_str)
    if offset is None:
        return None
    weekday = weekday % 7  # 容错：越界值回绕到 0-6
    monday = now - timedelta(days=now.weekday(),
                             hours=now.hour, minutes=now.minute,
                             seconds=now.second, microseconds=now.microsecond)
    return monday + timedelta(days=weekday) + offset


def _parse_datetime(raw: Any) -> Optional[datetime]:
    """解析 last_run_at 时间字符串（兼容 ISO "T" 与 SQLite "空格" 两种分隔）"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace(" ", "T"))
    except (ValueError, TypeError):
        return None


# ========== 运行时间维护 ==========

def update_last_run(template_id: int, run_time: Optional[str] = None) -> bool:
    """更新模板最近运行时间（事务内完成），返回是否更新到行

    说明：定时调度「防重入」的关键——调度线程在真正执行模板之前先调用本
    方法抢占 last_run_at，即使执行耗时超过一个检查周期，也不会被重复触发。
    BEGIN IMMEDIATE 保证并发检查时只有一个线程能成功更新。

    Args:
        template_id: 模板 ID
        run_time: 运行时间字符串（ISO 格式）；None 时取当前时间
    """
    if not run_time:
        run_time = datetime.now().isoformat(timespec="seconds")
    try:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE task_templates SET last_run_at = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(run_time), template_id),
            )
            conn.commit()
            return (cursor.rowcount or 0) > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 更新模板运行时间失败: {e}")
        return False


# ========== 待审批堆积控制 ==========

def pending_approval_count(template_id: int) -> int:
    """统计某模板当前未处理的待审批记录数（status='pending'）

    模板执行产生的审批记录 goal_id 形如 template_{id}（见 template_goal_id），
    定时触发前调用本函数做堆积控制：存在未处理待审批时跳过本次触发，
    避免待审批无限积累。

    Args:
        template_id: 模板 ID

    Returns:
        pending 记录数；查询失败时打印日志并返回 0（fail-open：不阻断
        正常触发主流程，与代码库「异常不影响主流程」的约定一致）。
    """
    goal_id = template_goal_id(template_id)
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pending_approvals "
                "WHERE goal_id = ? AND status = 'pending'",
                (goal_id,),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 待审批堆积查询失败（忽略，按无堆积处理）: {e}")
        return 0


# ========== 运行记录（P7 批次 14，2026-10-06）==========
#
# 这一段回答的是报件 v3 的那四问：上次开机几点／期间到期几件／补算成功几件失败几件／
# 这批行是哪一次运行从哪个库产出的。写行的**判据**在 agents.task_executor
# .check_scheduled_templates（那里才知道 ran/paused/backlog_skip/claim_skip/failed），
# 本模块只负责"怎么落、怎么读"，不判断该不该落。

def _run_db_fingerprint() -> Dict[str, Any]:
    """本次运行连到了哪个库：绝对路径＋文件字节数＋该库的 pending 总数（C8）

    DB_PATH 走**延迟 import**（同 core.db_backup._default_db_path 的理由）：它会被
    `tests/_test_db_helpers.setup_test_db()` 在导入后改写到临时目录，写成模块级常量
    就把指纹固化在生产路径上了。

    任何一项取不到都记 0／空串而不抛异常：这一列的用途是"事后能看出这批行来自哪个
    文件"，不该因为取不到文件大小就让整行记录写不进去（那才是真损失）。
    """
    from memory.database import DB_PATH
    abs_path = os.path.abspath(DB_PATH)
    info: Dict[str, Any] = {"db_path": abs_path, "db_bytes": 0,
                           "db_pending_total": 0}
    try:
        info["db_bytes"] = os.path.getsize(abs_path)
    except OSError as e:
        print(f"[TaskTemplate] 运行记录的库大小取不到（记 0）: {e}")
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pending_approvals "
                "WHERE status = 'pending'").fetchone()
            info["db_pending_total"] = int(row["c"]) if row else 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 运行记录的 pending 总数取不到（记 0）: {e}")
    return info


def latest_pending_approval_id() -> Optional[int]:
    """取 pending_approvals 当前的最大 id（本次运行的"跑前基线"，C4 用）

    跑完之后 `id > 该基线` 的行就是本次新建的行——**差值法**，与 produced_rows
    同一个口径（批次 14 定案第 5 条：不把这个键穿过 execute_plan / _persist_approval，
    只在跑前跑后各取一次）。

    Returns:
        最大 id（表里没有行时为 0，此时"所有行都属于将来"，语义正确）；
        **查询失败返回 None**——None 是"不知道基线"，调用方据此跳过归属回填。
        这里不能拿 0 兜底：那等于把库里全部历史行都认领成本次的产出。
    """
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT MAX(id) AS m FROM pending_approvals").fetchone()
        finally:
            conn.close()
        return int(row["m"]) if row and row["m"] is not None else 0
    except Exception as e:
        print(f"[TaskTemplate] 审批基线 id 查询失败（本次不做 run_id 归属）: {e}")
        return None


def record_template_run(trigger_source: str, started_at: str, finished_at: str,
                        status: str = "ok", due_count: Optional[int] = 0,
                        ran: int = 0, paused: int = 0, backlog_skip: int = 0,
                        claim_skip: int = 0, failed: int = 0,
                        produced_rows: int = 0, budget_exhausted: int = 0,
                        note: str = "") -> Optional[int]:
    """写一行模板运行记录，返回新行 id；失败返回 None（打印日志，不抛异常）

    Args:
        trigger_source: boot / cli / timer（含义见 TRIGGER_SOURCE_* 常量）
        started_at / finished_at: **UTC 字符串**（带 +00:00 偏移，C9）。调用方用
            `datetime.now(timezone.utc).isoformat(timespec="seconds")` 生成；
            本函数不换算——换算一处、生成另一处，两根轴的差别就看不见了
        status: ok / partial（有失败项）/ error（到期清单没读出来）
        due_count: 本次读到的到期模板数；**None ＝ "没读到"，不等于 0**
        ran / paused / backlog_skip / claim_skip / failed: 到期模板的去向计数。
            不变式 `due_count == ran + paused + backlog_skip + claim_skip + failed`
            由调用方构造性保证（每个到期模板恰好走一条分支并给一个计数器 +1）
        produced_rows: 本次新建的 pending_approvals 行数（差值法）。注意它量的是
            "待处理产出数"，**不代表已落盘**：auto_approve=0 时产物停在审批队列里
        budget_exhausted: 调用预算是否用尽（C2）；当前生产者 scout_digest 零 LLM
            零网络，故今天只会是 0，字段存在是 C8 的要求
        note: 一句话说明（如读失败原因）。C10：只落标识与计数，不落内容全文

    写行条件（该不该调用本函数）由调用方判断，判据见 RECORD_ALWAYS_SOURCES。
    """
    try:
        fp = _run_db_fingerprint()
        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO template_runs (trigger_source, started_at, finished_at, "
                "status, db_path, db_bytes, db_pending_total, due_count, ran, paused, "
                "backlog_skip, claim_skip, failed, produced_rows, budget_exhausted, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trigger_source, started_at, finished_at, status,
                 fp["db_path"], fp["db_bytes"], fp["db_pending_total"],
                 due_count, ran, paused, backlog_skip, claim_skip, failed,
                 produced_rows, budget_exhausted, note),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 运行记录写入失败（忽略）: {e}")
        return None


def attach_pending_approvals_to_run(run_id: int, after_id: int) -> int:
    """把 `id > after_id` 的待审批行标上 run_id（C4 的"撤销单位＝一次运行"），返回行数

    已知限制（批次 14 定案第 5 条，写在这里而不是藏起来）：这个区间按 id 取值，
    窗口内若恰好有**界面手动运行**（do_run_template，不经本路径、不记运行行）产出
    的行，它们的 id 也落在区间里，会被一并标成本次的。取"跑前跑后各读一次 MAX(id)"
    这个便宜办法的代价就是它；换成把 run_id 穿过 execute_plan / _persist_approval
    才分得清，那是本批明确不做的一处改造。
    """
    try:
        conn = get_db()
        try:
            cursor = conn.execute(
                "UPDATE pending_approvals SET run_id = ? "
                "WHERE id > ? AND run_id IS NULL",
                (run_id, after_id),
            )
            conn.commit()
            return cursor.rowcount or 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[TaskTemplate] 待审批行的 run_id 归属失败（忽略）: {e}")
        return 0


def latest_template_run(trigger_source: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
    """读最近一行运行记录（不限触发源，或只看某个触发源）；无行／失败返回 None

    V3 的"这次开机距上次多久"就靠它：调用方在**写本次之前**取
    `latest_template_run(TRIGGER_SOURCE_BOOT)`，拿到上一条 boot 行的 started_at。
    """
    sql = "SELECT * FROM template_runs"
    params: tuple = ()
    if trigger_source:
        sql += " WHERE trigger_source = ?"
        params = (trigger_source,)
    sql += " ORDER BY id DESC LIMIT 1"
    try:
        conn = get_db()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[TaskTemplate] 运行记录读取失败（忽略）: {e}")
        return None


def list_template_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """按 id 倒序列出最近 limit 行运行记录（只读，供命令行与排查用）；失败返回 []"""
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM template_runs ORDER BY id DESC LIMIT ?",
                (max(0, int(limit)),)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[TaskTemplate] 运行记录列表读取失败（忽略）: {e}")
        return []

