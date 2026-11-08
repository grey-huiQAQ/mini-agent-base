"""
Reminder Agent - 主动提醒与通知代理（v3.7.4 新增）

定位：把"今日计划"（goal_tasks.planned_date 为今天的任务）在用户设定的
提醒时间点推送到 UI 通知区域与控制台日志：
1. 周期检查：check_reminders()          → 由 app.py 的 gr.Timer 每 60 秒调用
2. 时间设置：set_reminder_times()       → 解析并保存提醒时间到档案（ProfileManager）
3. 数据来源：get_today_pending_tasks()  → 所有进行中目标中 planned_date=今天且未完成的任务

设计说明（与 planner_agent / daily_planning_agent / intent_agent 保持一致）：
- 纯本地规则，不调用 LLM、不写业务库；提醒设置仅写入 profile 键值对，
  不修改任何现有数据库表结构；
- 通过模块引用 ProfileManager 的静态方法读取/保存设置，测试可直接操作
  档案数据离线验证；
- check_reminders(current_time_str=None) 允许传入 "HH:MM" 以便离线测试
  （默认取系统当前时间）；
- 去重策略：实例内维护 _last_reminded_minute（(日期, HH:MM) 元组），同一
  分钟内不重复触发；不同提醒时间点可再次提醒，直到任务完成。该状态仅存
  内存，进程重启后允许重新提醒（符合需求约定）；
- 通知持久化：触发后消息保留在 _active_notification 中持续展示，避免被
  下一分钟的空检查清掉；当今日待办全部完成后自动撤下通知。

UI 集成约定（app.py）：
- gr.Timer(60).tick(do_check_reminders) 每 60 秒拉取一次 {"reminder": str}；
- Gradio < 5 无内置 Timer 时由 app.py 启动后台线程轮询本代理（仅日志）。
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.profile_manager import ProfileManager

# 提醒时间存储键（profile 表 key-value，无需新增表或列）
REMINDER_TIMES_KEY = "reminder_times"
# 关闭提醒的哨兵值：清空输入框保存时写入，get_reminder_times 读到即返回空列表
REMINDER_OFF_FLAG = "OFF"
# 默认提醒时间（档案未设置时生效）
DEFAULT_REMINDER_TIMES = ["09:00", "14:00"]

# 提醒消息中最多列出的任务条数（超出部分折叠为"等 N 个任务"，防止通知区过长）
MAX_TASKS_IN_MESSAGE = 10

# 时间字符串拆分：兼容英文逗号、中文逗号、分号（中英文）、空白等多种分隔符
_TIME_SPLIT_RE = re.compile(r"[,，;；\s]+")
# 单个时间片段：HH:MM（小时允许 1-2 位，如 "9:05"）
_TIME_ITEM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _today() -> str:
    """今天的日期字符串（YYYY-MM-DD），独立成函数便于测试 monkeypatch"""
    return datetime.now().strftime("%Y-%m-%d")


def parse_reminder_times(text: Any) -> List[str]:
    """把用户输入的提醒时间文本解析为规范化 HH:MM 列表（升序去重）

    Args:
        text: 如 "09:00,14:30"、"9:05，20:00"；容忍多余空白与混合分隔符。

    Returns:
        规范化后的时间列表，如 ["09:00", "14:30"]；
        无任何合法片段时返回 []（调用方决定回退默认值还是报错）。

    合法性规则：小时 0-23、分钟 0-59；非法片段直接忽略。
    """
    if text is None:
        return []
    fragments = [f for f in _TIME_SPLIT_RE.split(str(text).strip()) if f]
    parsed = set()
    for frag in fragments:
        match = _TIME_ITEM_RE.match(frag)
        if not match:
            continue  # 非 HH:MM 片段忽略
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue  # 越界时间忽略（如 25:99）
        parsed.add(f"{hour:02d}:{minute:02d}")
    return sorted(parsed)


def format_reminder_message(now_min: str,
                            tasks: List[Dict[str, Any]]) -> str:
    """生成提醒 Markdown 消息（纯函数，便于单测断言）

    Args:
        now_min: 当前分钟字符串（HH:MM）
        tasks: get_today_pending_tasks 返回的任务字典列表
               （需含 task_title / goal_title 字段）

    Returns:
        形如 "📅 现在是 09:00，你有 3 个任务计划今天完成：\\n- 任务1（目标A）..."
    """
    count = len(tasks)
    lines = [f"📅 **现在是 {now_min}，你有 {count} 个任务计划今天完成：**"]
    for t in tasks[:MAX_TASKS_IN_MESSAGE]:
        lines.append(f"- {t.get('task_title') or '（无标题任务）'}（{t.get('goal_title') or '未知目标'}）")
    if count > MAX_TASKS_IN_MESSAGE:
        lines.append(f"- …等共 {count} 个任务")
    return "\n".join(lines)


class ReminderAgent:
    """主动提醒代理：定时检查今日计划并在设定时间点生成提醒"""

    def __init__(self, goal_manager=None):
        # 复用全局 GoalManager；未注入时延迟导入默认实例（模式同 DailyPlannerAgent）
        if goal_manager is not None:
            self.goal_manager = goal_manager
        else:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()
        # 上次实际触发提醒的 (日期, HH:MM) 标记；None 表示本次进程尚未触发过
        self._last_reminded_minute: Optional[tuple] = None
        # 当前应在通知区持续展示的消息（触发后保留，任务全部完成后撤下）
        self._active_notification: str = ""

    # ========== 设置读写 ==========

    def get_reminder_times(self) -> List[str]:
        """读取提醒时间列表（HH:MM 升序）

        优先级：档案 reminder_times 键 → 未设置/解析失败回退 DEFAULT_REMINDER_TIMES；
        键值为 OFF 哨兵时返回 [] 表示已关闭提醒。
        """
        try:
            raw = ProfileManager.get(REMINDER_TIMES_KEY)
        except Exception as e:
            print(f"[ReminderAgent] 档案读取失败（使用默认提醒时间）: {e}")
            raw = None
        if raw is None:
            return list(DEFAULT_REMINDER_TIMES)
        text = str(raw).strip()
        if not text or text.upper() == REMINDER_OFF_FLAG:
            return []  # 用户显式关闭提醒
        return parse_reminder_times(text) or list(DEFAULT_REMINDER_TIMES)

    def set_reminder_times(self, text: Any) -> Dict[str, Any]:
        """解析并保存提醒时间设置到档案（profile 键值对，不改表结构）

        Args:
            text: 用户输入，如 "09:00,14:30"；空值表示关闭提醒。

        Returns:
            {"ok": bool, "times": List[str], "message": str}
            ok=False 表示输入非法且原设置保持不变（档案不被破坏）。
        """
        raw = "" if text is None else str(text).strip()
        if not raw:
            # 清空输入框保存 → 写入 OFF 哨兵，关闭提醒（区别于"未设置用默认"）
            try:
                ProfileManager.set(REMINDER_TIMES_KEY, REMINDER_OFF_FLAG)
                self._active_notification = ""  # 关闭时同步撤下已有通知
            except Exception as e:
                return {"ok": False, "times": self.get_reminder_times(),
                        "message": f"❌ 保存失败：{e}"}
            return {"ok": True, "times": [],
                    "message": "⏸️ 提醒已关闭（输入如 09:00,14:00 并保存可重新开启）"}

        parsed = parse_reminder_times(raw)
        if not parsed:
            return {"ok": False, "times": self.get_reminder_times(),
                    "message": "⚠️ 无法识别的时间格式，请使用 HH:MM 并以逗号分隔"
                               "（如 09:00,14:00）；原设置保持不变"}
        try:
            ProfileManager.set(REMINDER_TIMES_KEY, ",".join(parsed))
        except Exception as e:
            return {"ok": False, "times": parsed,
                    "message": f"❌ 保存失败：{e}"}
        print(f"[ReminderAgent] 提醒时间已更新为：{'、'.join(parsed)}")
        return {"ok": True, "times": parsed,
                "message": f"✅ 已保存提醒时间：{'、'.join(parsed)}（每天到点自动检查今日计划）"}

    # ========== 今日计划数据采集 ==========

    def get_today_pending_tasks(self) -> List[Dict[str, Any]]:
        """收集「计划今天完成且未完成」的任务——筛选交给 core 的共享查询，这里只做投影

        第一批「今日工作流」（2026-10-20）之前这里是手写的两重循环
        （`list_goals(status="active")` × `get_tasks()` × 比 `planned_date` × 排除 done），
        与 `core/goal_manager.py::get_tasks_planned_for` 是同一件事的两份实现——
        spec §1.2 的 D1 方案 B 点名的就是这一对，判据 §4.2 ③ 钉的是「只剩一份」
        （负控＝把这里改回手写循环，那条用例应当红）。
        排序键一字不变（优先级数字小者在前、同优先级按任务 ID），对外返回的五键 shape
        一字不变：`check_reminders`、UI 通知区与既有测试都不受影响。
        与首页「今日安排」共用同一把尺之后，两边看到的今日集合必然相同。

        Returns:
            [{"task_id", "task_title", "goal_id", "goal_title", "priority"}]
        """
        tasks: List[Dict[str, Any]] = [
            {
                "task_id": row["id"],
                "task_title": row.get("title") or "",
                "goal_id": row["goal_id"],
                "goal_title": row.get("goal_title") or "",
                "priority": int(row.get("priority") or 1),
            }
            for row in self.goal_manager.get_tasks_planned_for(_today())
        ]
        tasks.sort(key=lambda x: (x["priority"], x["task_id"]))
        return tasks

    # ========== 核心检查逻辑 ==========

    def _resolve_current_minute(self, current_time_str: Optional[str]) -> str:
        """把传入时间参数解析为 HH:MM；None 取系统当前时间

        Raises:
            ValueError: 传入格式非法（非 HH:MM 或越界），由上层捕获兜底。
        """
        if current_time_str is None:
            return datetime.now().strftime("%H:%M")
        parsed = parse_reminder_times(current_time_str)
        if len(parsed) != 1:
            # 空、多片段或含非法片段均视为无效输入（严格模式，防止静默错判）
            raise ValueError(f"无效的时间字符串：{current_time_str!r}")
        return parsed[0]

    def check_reminders(self, current_time_str: Optional[str] = None) -> Dict[str, Any]:
        """检查当前时刻是否应触发提醒，返回结构化结果

        逻辑：
        1. 统计今日未完成任务数 task_count（无论是否到点都计算）；
        2. 当前分钟精确匹配某个提醒时间（HH:MM 级）且同一分钟未触发过 →
           有任务则生成消息、打印控制台日志并更新活动通知；无任务则不打扰；
        3. 非提醒时刻：若活动通知存在但今日任务已全部完成 → 自动撤下通知；
        4. reminder 字段始终返回"当前应在通知区显示的内容"，空串表示无需提醒。

        Args:
            current_time_str: 测试注入的 "HH:MM"；None 使用系统当前时间。

        Returns:
            {"ok": bool, "reminder": str, "task_count": int}
            ok=False 表示内部异常（已捕获并打日志），reminder/task_count 为安全默认值。
        """
        try:
            now_min = self._resolve_current_minute(current_time_str)
            tasks = self.get_today_pending_tasks()
            task_count = len(tasks)

            dedup_key = (_today(), now_min)
            if now_min in self.get_reminder_times() and self._last_reminded_minute != dedup_key:
                # 标记去重必须先于生成动作：保证同一分钟无论有无任务都只处理一次
                self._last_reminded_minute = dedup_key
                if tasks:
                    message = format_reminder_message(now_min, tasks)
                    self._active_notification = message
                    # 控制台日志提醒（需求要求的第二种提醒方式）
                    print(f"\n[ReminderAgent] ⏰ 提醒触发（{now_min}）：\n{message}\n")
                else:
                    # 到点但今天没有计划任务：不打扰，同时撤下可能残留的旧通知
                    self._active_notification = ""
            elif self._active_notification and task_count == 0:
                # 非提醒时刻：今日任务已全部完成 → 自动撤下通知
                self._active_notification = ""

            return {"ok": True,
                    "reminder": self._active_notification or "",
                    "task_count": task_count}
        except Exception as e:
            # 异常安全：任何失败都不向 UI 抛出，返回安全默认值
            print(f"[ReminderAgent] 提醒检查失败（忽略）: {e}")
            return {"ok": False, "reminder": "", "task_count": 0}


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent / daily_planner_agent）
reminder_agent = ReminderAgent()
