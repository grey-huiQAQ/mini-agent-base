"""
Planner Agent - 规划代理

两大功能：
1. 分阶段学习计划生成：结合用户档案、学习记录、知识图谱，调用 LLM 生成
   「里程碑 → 任务」两级结构的计划；用户确认后写入目标系统（GoalManager）。
2. 多目标冲突分析：读取所有进行中目标的进度，调用 LLM 分析时间/精力/优先级
   冲突，输出优先级建议与调整方案；用户确认后应用（更新目标优先级、截止日期，
   或暂停目标）。

设计说明：
- 与 supervisor / evaluator / daily_agent 保持一致：直接通过
  business.model_client.chat_sync 调用模型（测试时可通过替换
  model_client.chat_sync 进行离线 mock）。
- 所有 LLM 输出均经过 extract_json + 校验器过滤，异常时降级为错误提示，
  不抛出到 UI 层。
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 通过模块引用而非 from-import，便于单测中 monkeypatch chat_sync。
# P7 批次 10-①：行动建议那处调用已改走 core.ai_filter.chat_sync_with_retry_budget
# （首档预算抬到推理地板 1500），该 helper 调用时读的仍是
# business.model_client.chat_sync 这一模块属性，故 mock 缝保持不变。
from business import model_client
from memory.profile_manager import ProfileManager
from memory.learning_recorder import LearningRecorder, format_mastery_plain
from memory.knowledge_graph import KnowledgeGraph


# ---------- JSON 提取（统一实现，2026-09-12 收敛到 core/json_utils） ----------
# 保留 extract_json 名称以兼容既有调用点（agents/reflection_agent.py 与
# agents/daily_planning_agent.py 分别从 intent_agent / planning_mirror_agent 导入同名函数）。
from core.json_utils import extract_json_object as extract_json

# 预算与重试统一实现（P7 批次 10-①）
from core.ai_filter import chat_sync_with_retry_budget


def _safe_int(value: Any, default: int, lo: int, hi: int) -> int:
    """容错转换整数并限制范围"""
    try:
        return max(lo, min(hi, int(value)))
    except (ValueError, TypeError):
        return default


@dataclass
class StagedPlan:
    """分阶段学习计划的结构化表示（内存态，写入前使用）"""
    goal_id: str
    goal_title: str
    milestones: List[Dict[str, Any]] = field(default_factory=list)


class PlannerAgent:
    """规划代理：目标分解（含里程碑）+ 多目标冲突分析"""

    def __init__(self, goal_manager=None):
        # 复用全局 GoalManager；未注入时延迟导入默认实例
        if goal_manager is not None:
            self.goal_manager = goal_manager
        else:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()

    # ========== 内部工具：用户上下文采集 ==========

    def _collect_user_context(self, goal_id: Optional[str] = None) -> str:
        """汇总档案 / 学习记录 / 知识图谱薄弱点，作为计划生成的个性化依据"""
        lines = []

        # 用户档案
        try:
            profile = ProfileManager.get_all()
        except Exception:
            profile = {}
        if profile:
            labels = {
                "english_level": "英语水平", "ai_basis": "AI基础",
                "daily_time": "每天可学习时间", "prefer_style": "偏好讲解方式",
                "long_goal": "长期目标",
            }
            prof_lines = [f"- {labels[k]}：{v}" for k, v in profile.items() if k in labels and v]
            if prof_lines:
                lines.append("【用户档案】\n" + "\n".join(prof_lines))

        # 最近学习记录
        try:
            records = LearningRecorder.get_recent_records(limit=5)
        except Exception:
            records = []
        if records:
            # 未评渲染成「未评」（P7 批次 12-①）：此前是 r.get('mastery_score', '?')，
            # 那个 '?' 兜底不可达（get_recent_records 用 SELECT *，键一定在），
            # 真正会渲染出来的是"掌握度 None"。
            rec_lines = [f"- {r.get('topic', '')}（掌握度 {format_mastery_plain(r.get('mastery_score'))}）"
                         for r in records]
            lines.append("【最近学习记录】\n" + "\n".join(rec_lines))

        # 薄弱知识点（学习记录 + 知识图谱）
        try:
            weak = LearningRecorder.get_weak_points(limit=5)
        except Exception:
            weak = []
        if weak:
            lines.append("【学习记录中的薄弱点】\n" +
                         "\n".join(f"- {r.get('topic', '')}" for r in weak))
        try:
            weak_nodes = KnowledgeGraph.get_weak_nodes(limit=5)
        except Exception:
            weak_nodes = []
        if weak_nodes:
            lines.append("【知识图谱薄弱节点】\n" +
                         "\n".join(f"- {n.get('name', '')}（{n.get('domain', '')}）"
                                   for n in weak_nodes))

        # 当前目标统计（已选目标时补充进度信息）
        if goal_id:
            stats = self.goal_manager.get_goal_stats(goal_id) or {}
            if stats:
                lines.append(
                    f"【当前目标进度】已完成 {stats.get('completed_tasks', 0)}"
                    f"/{stats.get('total_tasks', 0)} 个任务"
                )

        return "\n\n".join(lines) if lines else "暂无用户画像数据。"

    # ========== 功能一：分阶段学习计划生成 ==========

    def generate_staged_plan(self, goal_id: Optional[str] = None,
                             new_goal_title: str = "",
                             new_goal_desc: str = "",
                             total_weeks: int = 8) -> Dict[str, Any]:
        """生成分阶段学习计划（不写库）

        Args:
            goal_id: 已有目标 ID；为空且提供 new_goal_title 时自动创建新目标
            new_goal_title / new_goal_desc: 新目标标题与描述
            total_weeks: 计划总周期（周）

        Returns:
            {"ok": bool, "message": str, "plan": StagedPlan|None, "display": markdown}
        """
        total_weeks = _safe_int(total_weeks, default=8, lo=1, hi=52)

        # 1. 定位或创建目标
        goal = self.goal_manager.get_goal(goal_id) if goal_id else None
        if goal is None:
            title = (new_goal_title or "").strip()
            if not title:
                return {"ok": False, "plan": None,
                        "message": "⚠️ 请选择已有目标，或填写新目标标题",
                        "display": ""}
            goal = self.goal_manager.create_goal(title, (new_goal_desc or "").strip())

        # 2. 采集个性化上下文
        user_context = self._collect_user_context(goal.goal_id)

        # 3. 构建提示词并调用 LLM
        prompt = f"""你是一位资深学习规划专家。请根据以下信息，为目标制定一份分阶段学习计划。

【目标标题】{goal.title}
【目标描述】{goal.description or "无"}
【总周期】{total_weeks} 周

{user_context}

要求：
1. 将计划划分为 2-4 个里程碑（如"第一阶段：基础准备"），按学习顺序排列；
2. 每个里程碑包含 2-4 个具体可执行的任务；
3. 任务难度循序渐进，结合用户的薄弱点和当前水平；
4. 严格只输出 JSON，格式如下：
{{"milestones":[{{"title":"第一阶段：xxx","description":"阶段目标简述","duration_weeks":2,"tasks":[{{"title":"任务标题","description":"任务怎么做","priority":1}}]}}]}}
其中 priority 为 1-3 的整数（1 最高）。不要输出任何其他文字。"""

        try:
            response = model_client.chat_sync(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1500,
            )
        except Exception as e:
            return {"ok": False, "plan": None,
                    "message": f"❌ 模型调用失败：{e}", "display": ""}

        # 4. 解析与校验
        plan_data = extract_json(response)
        plan = self._validate_plan(plan_data)
        if plan is None:
            return {"ok": False, "plan": None,
                    "message": "⚠️ 无法从模型回复中解析出有效计划，请重试",
                    "display": ""}

        staged = StagedPlan(goal_id=goal.goal_id, goal_title=goal.title,
                            milestones=plan)
        display = self.format_plan_markdown(staged, total_weeks)
        return {"ok": True,
                "message": f"✅ 已生成 {len(plan)} 个里程碑的计划（尚未写入，请确认后点击写入按钮）",
                "plan": staged, "display": display}

    def _validate_plan(self, plan_data: Optional[Dict]) -> Optional[List[Dict]]:
        """校验并清洗 LLM 返回的计划结构；无效返回 None"""
        if not plan_data or not isinstance(plan_data.get("milestones"), list):
            return None
        cleaned = []
        for ms in plan_data["milestones"]:
            if not isinstance(ms, dict):
                continue
            title = str(ms.get("title", "")).strip()
            if not title:
                continue
            tasks = []
            for t in ms.get("tasks") or []:
                if not isinstance(t, dict):
                    continue
                t_title = str(t.get("title", "")).strip()
                if not t_title:
                    continue
                tasks.append({
                    "title": t_title,
                    "description": str(t.get("description", "")).strip(),
                    "priority": _safe_int(t.get("priority"), default=2, lo=1, hi=3),
                })
            if not tasks:
                continue  # 没有有效任务的里程碑跳过
            cleaned.append({
                "title": title,
                "description": str(ms.get("description", "")).strip(),
                "duration_weeks": _safe_int(ms.get("duration_weeks"), default=0, lo=0, hi=52),
                "tasks": tasks,
            })
        return cleaned or None

    def format_plan_markdown(self, staged: StagedPlan, total_weeks: int = 0) -> str:
        """将计划格式化为 Markdown，用于聊天区/UI 展示"""
        lines = [f"## 🗺️ 分阶段学习计划：{staged.goal_title}", ""]
        if total_weeks:
            lines.append(f"> 总周期：约 **{total_weeks} 周**")
        week_cursor = 1
        for i, ms in enumerate(staged.milestones, start=1):
            weeks = ms.get("duration_weeks") or 0
            span = f"（约 {weeks} 周：第 {week_cursor}-{week_cursor + weeks - 1} 周）" if weeks else ""
            lines.append(f"### 🚩 里程碑 {i}：{ms['title']} {span}")
            if ms.get("description"):
                lines.append(f"{ms['description']}")
            for t in ms["tasks"]:
                pri = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}.get(t["priority"], "⚪")
                lines.append(f"- **{t['title']}** [{pri}]")
                if t["description"]:
                    lines.append(f"  - {t['description']}")
            week_cursor += weeks
        lines.append("")
        lines.append("> 💡 确认无误后，请点击「✅ 写入目标系统」保存。")
        return "\n".join(lines)

    def apply_staged_plan(self, staged: StagedPlan, replace_ai_pending: bool = False) -> Dict[str, Any]:
        """将确认后的计划写入目标系统：创建里程碑并把任务挂到里程碑下

        实际写入委托 GoalManager.import_staged_plan，在单事务内完成
        "清理旧 AI 任务 + 写入里程碑 + 挂接任务"，失败自动回滚。

        Args:
            staged: generate_staged_plan 返回的 plan 对象
            replace_ai_pending: 是否先清除该目标下 AI 生成的未完成任务（避免重复）

        Returns:
            {"ok": bool, "message": str, "milestone_count": int, "task_count": int}
        """
        return self.goal_manager.import_staged_plan(
            staged.goal_id, staged.milestones, replace_ai_pending=replace_ai_pending
        )

    # ========== 功能二：多目标冲突分析 ==========

    def analyze_conflicts(self) -> Dict[str, Any]:
        """分析所有进行中目标的冲突（时间/精力/优先级），输出建议（不写库）

        Returns:
            {"ok": bool, "message": str, "suggestions": list, "display": markdown}
        """
        overview = self.goal_manager.get_active_goals_overview()
        if len(overview) == 0:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 暂无进行中的目标，先创建一个吧", "display": ""}

        # 单目标无需冲突分析，但仍给出聚焦建议
        overview_text = "\n".join(
            f"- [{g['goal_id']}] {g['title']}｜优先级:{g['priority']}｜"
            f"截止:{g['deadline'] or '无'}｜任务 {g['completed_tasks']}/{g['total_tasks']}"
            f"（完成率 {g['completion_rate']}%）"
            for g in overview
        )

        prompt = f"""你是一位学习规划顾问。用户同时在进行多个学习目标，请分析它们之间的时间、精力与优先级冲突，给出调整建议。

【当前进行中的目标】
{overview_text}

要求：
1. 分析维度：时间分配是否可行、目标间是否争抢精力、优先级是否合理、截止日期是否冲突；
2. 给出 1-3 条建议，action 只能是以下四种之一：
   - focus：建议优先集中完成某目标
   - pause：建议暂时搁置某目标
   - reorder：建议调整某目标下的任务顺序（文字说明即可）
   - adjust_deadline：建议修改某目标的优先级和/或截止日期
3. 严格只输出 JSON，格式如下：
{{"analysis":"一段总体分析","suggestions":[{{"goal_id":"目标ID","action":"focus","reason":"简要理由","new_priority":null,"new_deadline":null,"task_advice":""}}]}}
其中 new_priority 为 1-3 整数或 null；new_deadline 为 YYYY-MM-DD 或 null；reorder 类建议在 task_advice 里写明怎么调。不要输出任何其他文字。"""

        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            return {"ok": False, "suggestions": [],
                    "message": f"❌ 模型调用失败：{e}", "display": ""}

        data = extract_json(response)
        suggestions = self._validate_suggestions(data, overview)
        analysis = str(data.get("analysis", "")).strip() if data else ""

        if not suggestions:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 未能解析出有效建议，请重试", "display": ""}

        display = self.format_suggestions_markdown(analysis, suggestions, overview)
        return {"ok": True, "message": f"✅ 分析完成，共 {len(suggestions)} 条建议（确认后点击应用）",
                "suggestions": suggestions, "display": display}

    def _validate_suggestions(self, data: Optional[Dict],
                              overview: List[Dict]) -> List[Dict]:
        """校验建议列表：过滤非法 action 和不存在的 goal_id"""
        valid_ids = {g["goal_id"] for g in overview}
        valid_actions = {"focus", "pause", "reorder", "adjust_deadline"}
        result = []
        if not data or not isinstance(data.get("suggestions"), list):
            return result
        for s in data["suggestions"]:
            if not isinstance(s, dict):
                continue
            gid = s.get("goal_id")
            action = s.get("action")
            if gid not in valid_ids or action not in valid_actions:
                continue
            item = {
                "goal_id": gid,
                "action": action,
                "reason": str(s.get("reason", "")).strip(),
                "task_advice": str(s.get("task_advice", "")).strip(),
                "new_priority": None,
                "new_deadline": None,
            }
            # 优先级建议：仅 adjust_deadline/focus 场景接受数值
            np = s.get("new_priority")
            if np is not None:
                item["new_priority"] = _safe_int(np, default=-1, lo=1, hi=3)
                if item["new_priority"] == -1:
                    item["new_priority"] = None
            # 截止日期建议：宽松校验 YYYY-MM-DD 格式
            nd = str(s.get("new_deadline") or "").strip()
            if nd and re.match(r"^\d{4}-\d{2}-\d{2}$", nd):
                item["new_deadline"] = nd
            result.append(item)
        return result

    ACTION_LABELS = {
        "focus": "🎯 建议优先完成",
        "pause": "⏸️ 建议暂时搁置",
        "reorder": "🔀 建议调整任务顺序",
        "adjust_deadline": "📅 建议调整优先级/截止日期",
    }

    def format_suggestions_markdown(self, analysis: str, suggestions: List[Dict],
                                    overview: List[Dict]) -> str:
        """将冲突分析与建议格式化为 Markdown"""
        title_map = {g["goal_id"]: g["title"] for g in overview}
        lines = ["## ⚖️ 多目标冲突分析", ""]
        if analysis:
            lines += [f"**总体分析**：{analysis}", ""]
        for i, s in enumerate(suggestions, start=1):
            label = self.ACTION_LABELS.get(s["action"], s["action"])
            lines.append(f"### 建议 {i}：{label} —— {title_map.get(s['goal_id'], s['goal_id'])}")
            if s["reason"]:
                lines.append(f"- 理由：{s['reason']}")
            if s["new_priority"] is not None:
                pri = {1: "高", 2: "中", 3: "低"}.get(s["new_priority"], s["new_priority"])
                lines.append(f"- 目标优先级调整为：**{pri}**")
            if s["new_deadline"]:
                lines.append(f"- 截止日期调整为：**{s['new_deadline']}**")
            if s["task_advice"]:
                lines.append(f"- 任务安排：{s['task_advice']}")
            lines.append("")
        lines.append("> 💡 确认后请点击「✅ 应用建议」，将更新对应目标的优先级/截止日期或归档搁置目标。")
        return "\n".join(lines)

    def apply_conflict_suggestions(self, suggestions: List[Dict]) -> Dict[str, Any]:
        """应用冲突建议：更新目标优先级/截止日期，pause 动作归档目标

        Returns:
            {"ok": bool, "applied": int, "details": [str]}
        """
        applied, details = 0, []
        for s in suggestions or []:
            gid = s.get("goal_id")
            if not gid or not self.goal_manager.get_goal(gid):
                continue
            action = s.get("action")
            changes = []
            if action == "pause":
                self.goal_manager.archive_goal(gid)
                changes.append("已归档（搁置）")
            else:
                if s.get("new_priority") is not None:
                    self.goal_manager.update_goal(gid, priority=s["new_priority"])
                    changes.append(f"优先级→{s['new_priority']}")
                if s.get("new_deadline"):
                    self.goal_manager.update_goal(gid, deadline=s["new_deadline"])
                    changes.append(f"截止日期→{s['new_deadline']}")
            if changes:
                applied += 1
                details.append(f"[{gid}] " + "；".join(changes))
        return {"ok": applied > 0, "applied": applied, "details": details}


# 模块级默认实例（供 app.py 直接使用，模式同 daily_agent 的函数导出）
planner_agent = PlannerAgent()
