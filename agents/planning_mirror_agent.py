"""
Planning Mirror Agent - 规划镜像代理

定位：用户计划的"动态模型"，回答三个问题：
1. 现在各目标的状态如何？        → generate_snapshot()   状态快照
2. 如果新增/调整一个目标会怎样？  → simulate_new_goal()   影响模拟
3. 下一步最应该做什么？          → suggest_next_actions() 行动建议（确认后 apply_suggestion 应用）

设计说明：
- 与 planner_agent / intent_agent 保持一致：直接通过 business.model_client.chat_sync
  调用模型（测试时可通过替换 model_client.chat_sync 进行离线 mock）。
- 核心数据（进度、剩余工作量、时间冲突）全部由本地规则计算，LLM 只负责生成
  总结评估与建议润色；LLM 异常时自动降级为本地规则结果，不崩溃、不写库。
- 所有 LLM 输出均经过 extract_json + 校验器过滤；应用建议复用 GoalManager 现有的
  update_goal / archive_goal，不修改数据库表结构。
"""
import re
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

# 通过模块引用而非 from-import，便于单测中 monkeypatch chat_sync。
# P7 批次 10-①：本模块三处 LLM 调用已改走 core.ai_filter.chat_sync_with_retry_budget
# （首档预算抬到推理地板 1500），该 helper 在调用时 `from business.model_client import
# chat_sync`，读的是同一个模块属性——故单测里替换 model_client.chat_sync 仍然拦截得到，
# 这条 mock 缝保持不变。
from business import model_client
from memory.profile_manager import ProfileManager


# ---------- JSON 提取（统一实现，2026-09-12 收敛） ----------
# 本模块保留 extract_json 这一对外名称：agents/daily_planning_agent.py 仍以
# `from agents.planning_mirror_agent import (..., extract_json, ...)` 导入使用。
from core.json_utils import extract_json_object as extract_json

# 预算与重试统一实现（P7 批次 10-①）
from core.ai_filter import chat_sync_with_retry_budget


def _safe_int(value: Any, default: int, lo: int, hi: int) -> int:
    """容错转换整数并限制范围"""
    try:
        return max(lo, min(hi, int(value)))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float) -> float:
    """容错转换浮点数"""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# ---------- 常量与启发式参数 ----------

# 用户档案无 daily_time 时默认每天可用学习时间（小时）
DEFAULT_DAILY_HOURS = 2.0
# 每日可用时间上限钳制（防止档案里填了异常数值）
MAX_DAILY_HOURS = 16.0
# 每个未完成任务的平均预估工时（启发式：项目未记录单任务工时，按 2 小时估）
DEFAULT_TASK_HOURS = 2.0
# 无有效截止日期时，剩余工作量默认摊到的天数
FALLBACK_SPREAD_DAYS = 14
# 新目标在其周期内占用每日可用时间的份额（按优先级）
PRIORITY_TIME_SHARE = {1: 0.6, 2: 0.4, 3: 0.25}

# 合法的行动建议类型（suggest_next_actions 输出 / apply_suggestion 输入）
VALID_ACTION_TYPES = {"focus", "pause", "adjust_deadline", "adjust_priority", "complete_soon"}

ACTION_LABELS = {
    "focus": "🎯 建议优先完成",
    "pause": "⏸️ 建议暂停搁置",
    "adjust_deadline": "📅 建议调整截止日期",
    "adjust_priority": "⚡ 建议调整优先级",
    "complete_soon": "🏁 即将完成，集中收尾",
}

PRIORITY_LABELS = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}


# 时间单位正则片段（长词在前，避免 h 抢走 hours 的首字母、m 抢走 min 的首字母）
_HOURS_UNIT_RE = r"(?:小时|hours?|hrs?|h)"
_MINUTES_UNIT_RE = r"(?:分钟|分|minutes?|mins?|m)"
_NUM_RE = r"(\d+(?:\.\d+)?)"


def parse_daily_hours(raw: Any) -> float:
    """把用户档案中的 daily_time 文本解析为每天可用小时数

    支持的写法：
    - 小时："2"、"1.5"、"2小时"、"1.5h"、"3H"、"2 hours"
    - 分钟："90分钟"、"45m"、"30min"、"90分"
    - 复合格式："1小时30分钟" → 1.5，"2h30m" → 2.5
    缺失、非法或非正数时返回默认值 DEFAULT_DAILY_HOURS，结果钳制到 MAX_DAILY_HOURS。
    """
    if raw is None:
        return DEFAULT_DAILY_HOURS
    text = str(raw).strip().lower().replace(" ", "")
    if not text:
        return DEFAULT_DAILY_HOURS

    # 复合/带单位格式：分别捕获"数值+小时单位"与"数值+分钟单位"，任一命中即按分量换算
    hours_match = re.search(_NUM_RE + _HOURS_UNIT_RE, text)
    minutes_match = re.search(_NUM_RE + _MINUTES_UNIT_RE, text)
    if hours_match or minutes_match:
        total = 0.0
        if hours_match:
            total += _safe_float(hours_match.group(1), 0.0)
        if minutes_match:
            total += _safe_float(minutes_match.group(1), 0.0) / 60.0
        # 数值非正（如"0小时"）时回退默认值，与旧行为一致
        return round(min(total, MAX_DAILY_HOURS), 2) if total > 0 else DEFAULT_DAILY_HOURS

    # 兜底：纯数字写法（如"2"、"1.5"、"99"），行为与历史版本一致
    match = re.search(r"([\d.]+)", text)
    if not match:
        return DEFAULT_DAILY_HOURS
    num = _safe_float(match.group(1), -1.0)
    if num <= 0:
        return DEFAULT_DAILY_HOURS
    return round(min(num, MAX_DAILY_HOURS), 2)


def parse_deadline_days_left(deadline: str, today: Optional[date] = None) -> Optional[int]:
    """解析截止日期字符串（YYYY-MM-DD），返回距今天数；无日期或格式非法返回 None"""
    deadline = (deadline or "").strip()
    if not deadline:
        return None
    try:
        target = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    base = today or date.today()
    return (target - base).days


class PlanningMirrorAgent:
    """规划镜像代理：状态快照 + 影响模拟 + 行动建议（确认后应用）"""

    def __init__(self, goal_manager=None):
        # 复用全局 GoalManager；未注入时延迟导入默认实例（模式同 PlannerAgent）
        if goal_manager is not None:
            self.goal_manager = goal_manager
        else:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()

    # ========== 内部工具：数据采集与负荷估算 ==========

    def _get_daily_hours(self) -> float:
        """读取用户档案的 daily_time 字段，缺失/非法时回退默认值"""
        try:
            raw = ProfileManager.get("daily_time")
        except Exception:
            raw = None  # 档案读取失败不影响分析
        return parse_daily_hours(raw)

    def _estimate_goal_load(self, item: Dict[str, Any], daily_hours: float,
                            today: Optional[date] = None) -> Dict[str, Any]:
        """为单个目标估算时间负荷（纯本地计算）

        在 get_active_goals_overview 的字段基础上补充：
        - days_left：距截止日期天数（None 表示未设置或格式非法）
        - remaining_hours：预计剩余工作量（小时）= 未完成任务数 × 单任务均时
        - required_daily_hours：为按时完成每天需要投入的小时数
        - is_lagging：是否滞后（已过期，或剩余时间不够用）
        """
        pending = _safe_int(item.get("pending_tasks"), 0, 0, 10 ** 6)
        remaining_hours = round(pending * DEFAULT_TASK_HOURS, 1)

        days_left = parse_deadline_days_left(item.get("deadline") or "", today=today)
        expired = days_left is not None and days_left < 0

        # 有效分摊天数：有剩余天数用之；已过期按 1 天（需立即投入）；无期限按兜底值
        spread_days = days_left if (days_left is not None and days_left > 0) else (
            1 if expired else FALLBACK_SPREAD_DAYS)
        required_daily = round(remaining_hours / max(spread_days, 1), 2)

        is_lagging = expired or (days_left is not None and required_daily > daily_hours)

        result = dict(item)
        result.update({
            "days_left": days_left,
            "remaining_hours": remaining_hours,
            "required_daily_hours": required_daily,
            "is_lagging": bool(is_lagging),
            "expired": bool(expired),
        })
        return result

    def _build_goal_loads(self, overview: List[Dict[str, Any]],
                          today: Optional[date] = None) -> List[Dict[str, Any]]:
        """批量估算所有活跃目标的时间负荷"""
        daily_hours = self._get_daily_hours()
        return [self._estimate_goal_load(g, daily_hours, today=today) for g in overview]

    @staticmethod
    def _time_status(total_required: float, capacity: float) -> str:
        """根据总需求与容量给出时间状态标签"""
        if total_required <= capacity * 0.8:
            return "正常"
        if total_required <= capacity:
            return "偏紧"
        return "超载"

    def _overview_text(self, loads: List[Dict[str, Any]]) -> str:
        """把目标负荷列表压平为供 LLM 分析的文本"""
        lines = []
        for g in loads:
            ddl = g.get("deadline") or "无"
            lag = "是" if g.get("is_lagging") else "否"
            lines.append(
                f"- [{g['goal_id']}] {g['title']}｜优先级:{g.get('priority', 2)}｜"
                f"截止:{ddl}｜任务 {g.get('completed_tasks', 0)}/{g.get('total_tasks', 0)}"
                f"（完成率 {g.get('completion_rate', 0)}%）｜"
                f"预计剩余 {g.get('remaining_hours', 0)} 小时｜滞后:{lag}"
            )
        return "\n".join(lines)

    # ========== 功能一：状态快照 ==========

    def generate_snapshot(self) -> Dict[str, Any]:
        """汇总所有进行中目标的状态，生成规划快照（只读，不写库）

        Returns:
            {"ok": bool, "message": str, "snapshot": dict, "display": markdown}
        """
        overview = self.goal_manager.get_active_goals_overview()
        if not overview:
            return {"ok": False, "snapshot": {},
                    "message": "⚠️ 暂无进行中的目标，请先在上方创建一个吧", "display": ""}

        daily_hours = self._get_daily_hours()
        loads = self._build_goal_loads(overview)
        total_tasks = sum(int(g.get("total_tasks", 0)) for g in loads)
        completed = sum(int(g.get("completed_tasks", 0)) for g in loads)
        total_required = round(sum(float(g["required_daily_hours"]) for g in loads), 2)
        overall_rate = round(completed / total_tasks * 100, 1) if total_tasks else 0.0

        snapshot = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "daily_capacity_hours": daily_hours,
            "total_goals": len(loads),
            "total_tasks": total_tasks,
            "completed_tasks": completed,
            "pending_tasks": total_tasks - completed,
            "overall_completion_rate": overall_rate,
            "total_remaining_hours": round(sum(float(g["remaining_hours"]) for g in loads), 1),
            "total_required_daily_hours": total_required,
            "time_status": self._time_status(total_required, daily_hours),
            "goals": loads,
            "llm_summary": "",
        }

        # LLM 增强：生成一段总体评估（失败降级为空，不影响快照本身）
        summary, degraded = self._ask_llm_summary(snapshot)
        snapshot["llm_summary"] = summary

        display = self.format_snapshot_markdown(snapshot)
        message = f"✅ 快照已生成：{len(loads)} 个进行中目标，时间状态「{snapshot['time_status']}」"
        if degraded:
            message += "（LLM 评估不可用，已降级为本地统计）"
        return {"ok": True, "message": message, "snapshot": snapshot, "display": display}

    def _ask_llm_summary(self, snapshot: Dict[str, Any]) -> tuple:
        """让 LLM 对快照做总体评估；返回 (summary_text, degraded: bool)"""
        prompt = f"""你是一位学习规划顾问。以下是用户当前所有进行中目标的规划快照数据，请给出不超过 3 句话的总体评估。

【每日可用时间】{snapshot['daily_capacity_hours']} 小时
【目标数量】{snapshot['total_goals']} 个，任务总数 {snapshot['total_tasks']}，总完成率 {snapshot['overall_completion_rate']}%
【时间需求】合计每天约 {snapshot['total_required_daily_hours']} 小时（状态：{snapshot['time_status']}）
【各目标概况】
{self._overview_text(snapshot['goals'])}

要求：指出最值得注意的一两个问题（如超载、滞后、接近完成），语气简洁务实。
严格只输出 JSON，格式：{{"summary":"你的评估"}}。不要输出任何其他文字。"""
        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            data = extract_json(response)
            if data and str(data.get("summary", "")).strip():
                return str(data["summary"]).strip(), False
            return "", True
        except Exception as e:
            print(f"[PlanningMirror] LLM 总体评估失败（降级）: {e}")
            return "", True

    def format_snapshot_markdown(self, snapshot: Dict[str, Any]) -> str:
        """将快照格式化为 Markdown 展示"""
        lines = ["## 📊 目标状态快照", ""]
        lines.append(
            f"> 生成时间：{snapshot['generated_at']}｜每日可用时间：**{snapshot['daily_capacity_hours']} 小时**｜"
            f"时间状态：**{snapshot['time_status']}**（每日共需约 {snapshot['total_required_daily_hours']} 小时）")
        lines.append("")
        lines.append(
            f"- 进行中目标：**{snapshot['total_goals']}** 个｜任务总数：{snapshot['total_tasks']}｜"
            f"总完成率：**{snapshot['overall_completion_rate']}%**")
        lines.append(f"- 预计剩余总工作量：约 **{snapshot['total_remaining_hours']} 小时**")
        if snapshot.get("llm_summary"):
            lines.append("")
            lines.append(f"**🤖 总体评估**：{snapshot['llm_summary']}")
        lines.append("")
        lines.append("| 目标 | 优先级 | 截止 | 剩余天数 | 完成率 | 预计剩余 | 每日所需 | 状态 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for g in snapshot["goals"]:
            ddl = g.get("deadline") or "—"
            days = g.get("days_left")
            days_txt = f"{days} 天" if days is not None else "—"
            if days is not None and days < 0:
                days_txt = f"⚠️ 已过期 {-days} 天"
            status_parts = []
            if g.get("is_lagging"):
                status_parts.append("⏳ 滞后")
            if g.get("expired"):
                status_parts.append("❗ 过期")
            status = " ".join(status_parts) if status_parts else "正常"
            pri = PRIORITY_LABELS.get(g.get("priority", 2), g.get("priority"))
            lines.append(
                f"| {g['title']} | {pri} | {ddl} | {days_txt} | {g.get('completion_rate', 0)}% "
                f"({g.get('completed_tasks', 0)}/{g.get('total_tasks', 0)}) "
                f"| 约 {g['remaining_hours']}h | {g['required_daily_hours']}h | {status} |")
        lines.append("")
        lines.append("> 💡 可结合下方「💡 行动建议」获取下一步最优行动；快照为只读分析，不会修改任何数据。")
        return "\n".join(lines)

    # ========== 功能二：影响模拟 ==========

    def simulate_new_goal(self, new_goal_title: str, new_goal_desc: str = "",
                          estimated_weeks: int = 4, priority: int = 2) -> Dict[str, Any]:
        """模拟新增一个目标对现有规划的影响（只模拟，不写库）

        Args:
            new_goal_title: 新目标标题（必填）
            new_goal_desc: 新目标描述（可选）
            estimated_weeks: 预计周期（周），1-52，超出自动钳制
            priority: 新目标优先级 1-3

        Returns:
            {"ok": bool, "message": str, "impact": dict, "display": markdown}
        """
        title = (new_goal_title or "").strip()
        if not title:
            return {"ok": False, "impact": {},
                    "message": "⚠️ 请先填写新目标的标题", "display": ""}
        estimated_weeks = _safe_int(estimated_weeks, default=4, lo=1, hi=52)
        priority = _safe_int(priority, default=2, lo=1, hi=3)

        daily_hours = self._get_daily_hours()
        share = PRIORITY_TIME_SHARE[priority]
        new_daily_need = round(daily_hours * share, 2)
        new_total_hours = round(new_daily_need * estimated_weeks * 7, 1)

        overview = self.goal_manager.get_active_goals_overview()
        loads = self._build_goal_loads(overview)
        existing_required = round(sum(float(g["required_daily_hours"]) for g in loads), 2)
        after_required = round(existing_required + new_daily_need, 2)
        overloaded = after_required > daily_hours

        # 最可能被挤占的目标：时间需求大的在前；同等需求下低优先级（数字大）更容易被牺牲
        crowded = sorted(loads, key=lambda g: (-float(g["required_daily_hours"]),
                                               -int(g.get("priority", 2))))[:3]

        impact = {
            "new_goal": {
                "title": title,
                "description": (new_goal_desc or "").strip(),
                "priority": priority,
                "estimated_weeks": estimated_weeks,
                "assumed_daily_hours": new_daily_need,
                "estimated_total_hours": new_total_hours,
            },
            "after_add": {
                "total_goals": len(loads) + 1,
                "existing_required_daily_hours": existing_required,
                "total_required_daily_hours": after_required,
                "daily_capacity_hours": daily_hours,
                "utilization_pct": round(after_required / daily_hours * 100, 1) if daily_hours else 0.0,
            },
            "overloaded": bool(overloaded),
            "crowded_goals": crowded,
            "advice": [],
        }

        # LLM 增强：给出针对性调整建议；失败降级为本地规则建议
        advice, degraded = self._ask_llm_impact_advice(title, impact)
        impact["advice"] = advice

        display = self.format_impact_markdown(impact)
        verdict = "❌ 时间将超载" if overloaded else "✅ 时间可容纳"
        message = f"{verdict}：新增后每天共需约 {after_required} 小时 / 可用 {daily_hours} 小时"
        if degraded:
            message += "（LLM 建议不可用，已使用本地规则）"
        return {"ok": True, "message": message, "impact": impact, "display": display}

    def _ask_llm_impact_advice(self, title: str, impact: Dict[str, Any]) -> tuple:
        """让 LLM 针对新目标给出调整建议；返回 (advice_list, degraded: bool)"""
        after = impact["after_add"]
        crowded_text = self._overview_text(impact["crowded_goals"]) or "（暂无进行中的目标）"
        prompt = f"""你是一位学习规划顾问。用户想新增一个学习目标，以下是模拟影响数据，请给出 1-3 条简短的可执行调整建议。

【拟新增目标】{title}（优先级 {impact['new_goal']['priority']}，预计 {impact['new_goal']['estimated_weeks']} 周，
按份额估算每天需投入 {impact['new_goal']['assumed_daily_hours']} 小时，总计约 {impact['new_goal']['estimated_total_hours']} 小时）
【新增后】每天共需约 {after['total_required_daily_hours']} 小时 / 每天可用 {after['daily_capacity_hours']} 小时
{"❌ 将超载" if impact['overloaded'] else "✅ 可容纳"}
【现有目标中时间需求最高的】
{crowded_text}

要求：建议要具体到目标（如"建议降低现有目标X的优先级""建议暂停目标Y""建议把新目标排到Z之后再启动"）。
严格只输出 JSON，格式：{{"advice":["建议1","建议2"]}}。不要输出任何其他文字。"""
        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            data = extract_json(response)
            advice = self._validate_advice(data)
            if advice:
                return advice, False
            return self._fallback_impact_advice(impact), True
        except Exception as e:
            print(f"[PlanningMirror] LLM 影响建议失败（降级）: {e}")
            return self._fallback_impact_advice(impact), True

    @staticmethod
    def _validate_advice(data: Optional[Dict]) -> List[str]:
        """校验 LLM 返回的建议列表：仅保留非空字符串，最多 5 条"""
        if not data or not isinstance(data.get("advice"), list):
            return []
        result = []
        for item in data["advice"]:
            text = str(item).strip() if item is not None else ""
            if text:
                result.append(text[:200])
            if len(result) >= 5:
                break
        return result

    def _fallback_impact_advice(self, impact: Dict[str, Any]) -> List[str]:
        """本地规则版影响建议（LLM 不可用时的降级方案）"""
        advice = []
        after = impact["after_add"]
        ng = impact["new_goal"]
        if impact["overloaded"]:
            advice.append(
                f"时间超载：新增后每天需约 {after['total_required_daily_hours']} 小时，"
                f"超过可用的 {after['daily_capacity_hours']} 小时，建议暂缓新增或压缩其他目标的时间占比")
        for g in impact["crowded_goals"][:2]:
            if float(g["required_daily_hours"]) > 0:
                advice.append(
                    f"「{g['title']}」当前时间需求最大（约 {g['required_daily_hours']} 小时/天），"
                    f"可考虑降低其优先级或调整截止日期，为新目标腾出空间")
                break
        high_pri_existing = [g for g in impact["crowded_goals"] if int(g.get("priority", 2)) == 1]
        if ng["priority"] == 1 and high_pri_existing:
            advice.append(f"新目标与现有目标「{high_pri_existing[0]['title']}」同为高优先级，注意分配精力，避免互相争抢")
        if not advice:
            advice.append("当前时间容量充足，可以按计划推进新目标")
        return advice[:3]

    def format_impact_markdown(self, impact: Dict[str, Any]) -> str:
        """将影响模拟结果格式化为 Markdown 展示"""
        ng = impact["new_goal"]
        after = impact["after_add"]
        verdict = "❌ **时间将超载**" if impact["overloaded"] else "✅ **时间可容纳**"
        lines = ["## 🧪 新增目标影响模拟", ""]
        lines.append(
            f"- 拟新增目标：**{ng['title']}**（优先级 {PRIORITY_LABELS.get(ng['priority'], ng['priority'])}"
            f"，预计 {ng['estimated_weeks']} 周，估算总投入约 {ng['estimated_total_hours']} 小时，"
            f"每天占用约 {ng['assumed_daily_hours']} 小时）")
        lines.append(
            f"- 新增后目标总数：**{after['total_goals']}** 个｜每日需求合计约 "
            f"**{after['total_required_daily_hours']} 小时** / 可用 {after['daily_capacity_hours']} 小时"
            f"（利用率 {after['utilization_pct']}%）")
        lines.append(f"- 结论：{verdict}")

        if impact["crowded_goals"]:
            lines.append("")
            lines.append("**⏱️ 最可能被挤占的现有目标**")
            for g in impact["crowded_goals"]:
                pri = PRIORITY_LABELS.get(g.get("priority", 2), g.get("priority"))
                lines.append(
                    f"- {g['title']}（{pri}，当前每日所需约 {g['required_daily_hours']} 小时，"
                    f"完成率 {g.get('completion_rate', 0)}%）")

        if impact["advice"]:
            lines.append("")
            lines.append("**💡 调整建议**")
            for a in impact["advice"]:
                lines.append(f"- {a}")
        lines.append("")
        lines.append("> ℹ️ 本次仅为模拟分析，未写入任何数据；确认可行后再去创建目标。")
        return "\n".join(lines)

    # ========== 功能三：下一步行动建议 ==========

    def suggest_next_actions(self, limit: int = 3) -> Dict[str, Any]:
        """基于当前状态与快照，推荐下一步最优行动（不写库）

        Returns:
            {"ok": bool, "message": str, "suggestions": list, "display": markdown}
            每条建议包含：goal_id / action / reason / new_priority(可选) / new_deadline(可选)
        """
        limit = _safe_int(limit, default=3, lo=1, hi=10)
        overview = self.goal_manager.get_active_goals_overview()
        if not overview:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 暂无进行中的目标，先创建一个吧", "display": ""}

        daily_hours = self._get_daily_hours()
        loads = self._build_goal_loads(overview)

        # 主路径：LLM 给出建议；失败/无效时降级为本地规则引擎
        suggestions, analysis, degraded = self._ask_llm_actions(loads, daily_hours)
        if not suggestions:
            suggestions = self._fallback_action_suggestions(loads, daily_hours, limit=limit)
            degraded = True
        suggestions = suggestions[:limit]

        if not suggestions:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 未能生成有效建议，请稍后重试", "display": ""}

        display = self.format_actions_markdown(analysis, suggestions, loads)
        source = "（LLM 不可用，来自本地规则）" if degraded else ""
        message = f"✅ 已生成 {len(suggestions)} 条行动建议{source}（确认后点击应用）"
        return {"ok": True, "message": message, "suggestions": suggestions, "display": display}

    def _ask_llm_actions(self, loads: List[Dict[str, Any]],
                         daily_hours: float) -> tuple:
        """调用 LLM 生成行动建议；返回 (suggestions, analysis, degraded: bool)"""
        prompt = f"""你是一位学习规划顾问。以下是一位用户所有进行中目标的最新状态，请推荐最多 3 条"下一步最优行动"。

【每日可用时间】{daily_hours} 小时
【各目标状态】
{self._overview_text(loads)}

要求：
1. action 只能是以下五种之一：
   - focus：集中精力优先推进某目标
   - pause：暂时搁置某目标
   - adjust_deadline：调整某目标的截止日期和/或优先级
   - adjust_priority：调整某目标的优先级
   - complete_soon：某目标已接近完成，建议集中收尾
2. 优先处理：已滞后/过期的目标、接近完成可以收尾的目标、高优先级但被拖延的目标；
3. 严格只输出 JSON，格式如下：
{{"analysis":"一段总体判断","suggestions":[{{"goal_id":"目标ID","action":"focus","reason":"简要理由","new_priority":null,"new_deadline":null}}]}}
其中 new_priority 为 1-3 整数或 null；new_deadline 为 YYYY-MM-DD 或 null。不要输出任何其他文字。"""
        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            print(f"[PlanningMirror] LLM 行动建议失败（降级）: {e}")
            return [], "", True

        data = extract_json(response)
        if not data:
            return [], "", True
        suggestions = self._validate_action_suggestions(data, loads)
        analysis = str(data.get("analysis", "")).strip()
        return suggestions, analysis, False

    def _validate_action_suggestions(self, data: Optional[Dict],
                                     loads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """校验行动建议列表：过滤非法 action 与不存在的 goal_id，钳制数值与日期格式"""
        valid_ids = {g["goal_id"] for g in loads}
        result = []
        if not data or not isinstance(data.get("suggestions"), list):
            return result
        for s in data["suggestions"]:
            if not isinstance(s, dict):
                continue
            gid = s.get("goal_id")
            action = s.get("action")
            if gid not in valid_ids or action not in VALID_ACTION_TYPES:
                continue
            item = {
                "goal_id": gid,
                "action": action,
                "reason": str(s.get("reason", "")).strip(),
                "new_priority": None,
                "new_deadline": None,
            }
            np = s.get("new_priority")
            if np is not None:
                parsed = _safe_int(np, default=-1, lo=1, hi=3)
                item["new_priority"] = None if parsed == -1 else parsed
            nd = str(s.get("new_deadline") or "").strip()
            if nd and re.match(r"^\d{4}-\d{2}-\d{2}$", nd):
                item["new_deadline"] = nd
            result.append(item)
        return result

    def _fallback_action_suggestions(self, loads: List[Dict[str, Any]],
                                     daily_hours: float, limit: int = 3) -> List[Dict[str, Any]]:
        """本地规则版行动建议（LLM 不可用时的降级方案）

        规则优先级：接近完成 > 已过期 > 超负荷 > 截止临近但进度慢 > 默认聚焦。
        """
        suggestions: List[Dict[str, Any]] = []
        used_pause = False

        # 排序：滞后目标在前，其后按优先级升序（1 最高）、完成率降序
        ordered = sorted(
            loads,
            key=lambda g: (not g.get("is_lagging"), int(g.get("priority", 2)),
                           -float(g.get("completion_rate", 0))),
        )
        for g in ordered:
            if len(suggestions) >= limit:
                break
            gid = g["goal_id"]
            rate = float(g.get("completion_rate", 0))
            pending = int(g.get("pending_tasks", 0))
            days_left = g.get("days_left")

            # 1) 接近完成 → 集中收尾
            if rate >= 80 and pending > 0:
                suggestions.append({
                    "goal_id": gid, "action": "complete_soon",
                    "reason": f"完成率已达 {rate}%，集中收尾即可拿下",
                    "new_priority": None, "new_deadline": None})
                continue
            # 2) 已过期 → 顺延截止日期
            if g.get("expired"):
                new_ddl = (date.today() + timedelta(days=max(pending * 2, 7))).strftime("%Y-%m-%d")
                suggestions.append({
                    "goal_id": gid, "action": "adjust_deadline",
                    "reason": f"截止日期已过期 {-days_left} 天且仍有 {pending} 个待办任务",
                    "new_priority": None, "new_deadline": new_ddl})
                continue
            # 3) 时间不够用 → 高/中优先级提权压缩工期，低优先级建议暂停
            if g.get("is_lagging") and days_left is not None:
                if int(g.get("priority", 2)) >= 3 and not used_pause:
                    suggestions.append({
                        "goal_id": gid, "action": "pause",
                        "reason": f"每天需约 {g['required_daily_hours']} 小时但时间不足，且优先级较低，建议先搁置",
                        "new_priority": None, "new_deadline": None})
                    used_pause = True
                    continue
                suggestions.append({
                    "goal_id": gid, "action": "adjust_priority",
                    "reason": f"剩余 {g['remaining_hours']} 小时但每天仅剩 {days_left} 天，建议提升优先级加大投入",
                    "new_priority": 1, "new_deadline": None})
                continue
            # 4) 截止临近但进度落后 → 提升优先级
            if days_left is not None and 0 <= days_left <= 7 and rate < 50 \
                    and int(g.get("priority", 2)) > 1:
                suggestions.append({
                    "goal_id": gid, "action": "adjust_priority",
                    "reason": f"距离截止仅 {days_left} 天但完成率只有 {rate}%",
                    "new_priority": 1, "new_deadline": None})
                continue
            # 5) 兜底：建议聚焦第一个未被建议过的目标
            if all(s["goal_id"] != gid for s in suggestions):
                suggestions.append({
                    "goal_id": gid, "action": "focus",
                    "reason": f"当前优先级较高（{g.get('priority', 2)}）且时间状态可控，适合作为本周主攻方向",
                    "new_priority": None, "new_deadline": None})
        return suggestions

    def format_actions_markdown(self, analysis: str, suggestions: List[Dict],
                                loads: List[Dict[str, Any]]) -> str:
        """将行动建议格式化为 Markdown 展示"""
        title_map = {g["goal_id"]: g["title"] for g in loads}
        lines = ["## 💡 下一步行动建议", ""]
        if analysis:
            lines += [f"**总体判断**：{analysis}", ""]
        for i, s in enumerate(suggestions, start=1):
            label = ACTION_LABELS.get(s["action"], s["action"])
            lines.append(f"### 建议 {i}：{label} —— {title_map.get(s['goal_id'], s['goal_id'])}")
            if s["reason"]:
                lines.append(f"- 理由：{s['reason']}")
            if s.get("new_priority") is not None:
                pri = PRIORITY_LABELS.get(s["new_priority"], s["new_priority"])
                lines.append(f"- 目标优先级调整为：**{pri}**")
            if s.get("new_deadline"):
                lines.append(f"- 截止日期调整为：**{s['new_deadline']}**")
            lines.append("")
        lines.append("> 💡 确认后请点击「✅ 应用建议」：将更新对应目标的优先级/截止日期，"
                     "或归档暂停目标；「即将完成」类建议仅作提醒，不会改动数据。")
        return "\n".join(lines)

    # ========== 功能四：应用建议（唯一写库入口） ==========

    def _apply_single(self, suggestion: Dict[str, Any]) -> Dict[str, Any]:
        """应用单条建议；返回 {"ok": bool, "changed": bool, "message": str}

        changed=False 表示该条属于纯提醒类（如 complete_soon），未修改任何数据。
        """
        if not isinstance(suggestion, dict):
            return {"ok": False, "changed": False, "message": "⚠️ 无效的建议数据"}

        gid = suggestion.get("goal_id")
        goal = self.goal_manager.get_goal(gid) if gid else None
        if not goal:
            return {"ok": False, "changed": False,
                    "message": f"⚠️ 目标不存在或已删除：{gid}"}

        action = suggestion.get("action")
        changes = []

        if action == "pause":
            self.goal_manager.archive_goal(gid)
            changes.append("已归档（暂停）")
        elif action in ("focus", "adjust_deadline", "adjust_priority"):
            np = suggestion.get("new_priority")
            nd = suggestion.get("new_deadline")
            if np is not None:
                self.goal_manager.update_goal(gid, priority=_safe_int(np, default=2, lo=1, hi=3))
                changes.append(f"优先级→{_safe_int(np, default=2, lo=1, hi=3)}")
            if nd:
                self.goal_manager.update_goal(gid, deadline=str(nd).strip())
                changes.append(f"截止日期→{nd}")
            if not changes:
                return {"ok": True, "changed": False,
                        "message": f"ℹ️ 「{goal.title}」的聚焦建议无需修改数据（可在任务层面安排更多时间）"}
        elif action == "complete_soon":
            stats = self.goal_manager.get_goal_stats(gid) or {}
            pending = stats.get("pending_tasks", 0)
            return {"ok": True, "changed": False,
                    "message": f"ℹ️ 「{goal.title}」还有 {pending} 个待办任务，"
                               "请到目标详情逐个勾选完成（该建议不修改数据）"}
        else:
            return {"ok": False, "changed": False,
                    "message": f"⚠️ 未知的建议类型：{action}"}

        return {"ok": True, "changed": True,
                "message": f"✅ [{gid}] 「{goal.title}」：" + "；".join(changes)}

    def apply_suggestion(self, suggestions: Any) -> Dict[str, Any]:
        """应用行动建议（用户在 UI 确认后调用）

        Args:
            suggestions: suggest_next_actions 返回的建议列表（也容忍单条字典）

        Returns:
            {"ok": bool, "applied": int, "details": [str]}
            applied 仅统计实际修改了数据的建议条数。
        """
        # 兼容误传单条字典的情况
        if isinstance(suggestions, dict):
            suggestions = [suggestions]
        applied, details = 0, []
        for s in suggestions or []:
            try:
                result = self._apply_single(s)
            except Exception as e:
                details.append(f"❌ 应用建议失败：{e}")
                continue
            if result.get("changed"):
                applied += 1
            if result.get("message"):
                details.append(result["message"])
        return {"ok": applied > 0, "applied": applied, "details": details}

    # 向后友好的别名：语义上"批量应用"
    def apply_suggestions(self, suggestions: Any) -> Dict[str, Any]:
        """apply_suggestion 的别名（批量应用建议）"""
        return self.apply_suggestion(suggestions)


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent / intent_agent）
planning_mirror_agent = PlanningMirrorAgent()
