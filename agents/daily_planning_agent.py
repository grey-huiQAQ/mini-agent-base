"""
Daily Planner Agent - 每日规划代理（v3.7.2 新增）

定位：把"目标系统中的待办任务"落成"今天具体做什么"：
1. 生成今日计划建议：generate_daily_plan()   → 只分析不写库
2. 用户确认后应用计划：apply_daily_plan()    → 给任务写入 planned_date 标记

设计说明（与 planner_agent / intent_agent / planning_mirror_agent 保持一致）：
- 通过 business.model_client.chat_sync 调用模型，且经模块引用调用，
  测试可通过替换 model_client.chat_sync 离线 mock；
- 候选任务收集、优先级排序、耗时估算、超载判定全部由本地规则完成，
  LLM 只负责挑选任务并给出理由；LLM 异常或输出非法时自动降级为本地规则
  （按优先级取任务直到可用时间用尽），不崩溃、不写库；
- 只有 apply_daily_plan 会写库（goal_tasks.planned_date，由 database.init_db
  自动迁移的可空列），生成阶段完全只读。
- 复用 planning_mirror_agent 的通用工具（档案时间解析 / JSON 提取 /
  单任务均时常量），保证各规划类 Agent 对同一档案字段的解释一致。

与既有 agents/daily_agent.generate_daily_plan 的关系：
- 后者面向"学习计划文本"（Tab 计划与复盘），返回字符串且已绑定 UI；
- 本代理面向"目标任务级计划"，返回结构化结果，二者互不影响。
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# 通过模块引用而非 from-import，便于单测中 monkeypatch chat_sync（模式同 planning_mirror_agent）。
# P7 批次 10-①：LLM 调用已改走 core.ai_filter.chat_sync_with_retry_budget（首档预算抬到
# 推理地板 1500），该 helper 调用时读的仍是 business.model_client.chat_sync 这一模块属性。
from business import model_client
from memory.profile_manager import ProfileManager
from memory.experience_store import ExperienceStore

# 复用规划镜像模块的通用工具与常量（纯函数，无副作用）
from agents.planning_mirror_agent import (
    DEFAULT_DAILY_HOURS,
    DEFAULT_TASK_HOURS,
    extract_json,
    parse_daily_hours,
)

# 任务执行自动化标记（v3.7.5 新增）：复用 TaskExecutor 的关键词分类器，
# LLM 未给出 auto 标记时按标题/描述关键词本地判断（搜索/整理类任务可自动执行）
from agents.task_executor import guess_auto_executable

# 预算与重试统一实现（P7 批次 10-①）
from core.ai_filter import chat_sync_with_retry_budget

# 今日计划最多推荐的任务数
MAX_PLAN_TASKS = 5
# 相关经验检索条数与关键词（ExperienceStore.search_experiences 使用）
EXPERIENCE_LIMIT = 3
EXPERIENCE_QUERY = "今日计划 时间安排 学习"

# 启发式工时估算参数：每任务基础耗时；标题较长（步骤多、信息量大）适度上调
LONG_TITLE_THRESHOLD = 15      # 标题长度阈值（字符数），达到视为复杂任务
LONG_TITLE_EXTRA_HOURS = 0.5   # 复杂任务的额外估时（小时）
MAX_ESTIMATED_HOURS = 3.0      # 单任务估时上限（小时）

# 参与提示词的候选任务上限（防止任务过多撑爆上下文；排序靠前的优先保留）
MAX_PROMPT_TASKS = 20

# 无截止日期时的排序键（用大数表示"无穷远"，排在有期限的任务之后）
_NO_DEADLINE_KEY = 99991231.0

# 「今日可用时间」这个数是从哪来的（2026-10-20 阶段二 第一批 spec §2.1）。
# 展示层要靠来源分辨「档案里真的设了这个数」与「档案没设、所以回落默认值」：
# 两种情况在 parse_daily_hours 的返回值上可能一模一样（档案设成 2 与回落都是 2.0），
# 而「尚未设置每日可用时间」这句提示只在后者才可以说，否则就是当面对用户撒谎。
HOURS_FROM_OVERRIDE = "override"   # 本次在「本次可用小时」框里填的数（不写档案）
HOURS_FROM_PROFILE = "profile"     # 档案 daily_time 给了值
HOURS_FROM_FALLBACK = "fallback"   # 档案没给值 ⇒ 用 DEFAULT_DAILY_HOURS


def estimate_task_hours(title: str) -> float:
    """启发式估算单个任务的执行耗时（小时）

    项目未记录单任务工时，沿用规划镜像的启发式：
    基础 DEFAULT_TASK_HOURS 小时；标题长度达到 LONG_TITLE_THRESHOLD 个字符时
    追加 LONG_TITLE_EXTRA_HOURS 小时，结果钳制到 MAX_ESTIMATED_HOURS 上限。
    """
    base = float(DEFAULT_TASK_HOURS)
    if len((title or "").strip()) >= LONG_TITLE_THRESHOLD:
        base += LONG_TITLE_EXTRA_HOURS
    return min(base, MAX_ESTIMATED_HOURS)


def _today() -> str:
    """今天的日期字符串（YYYY-MM-DD）"""
    return datetime.now().strftime("%Y-%m-%d")


class DailyPlannerAgent:
    """每日规划代理：生成今日计划建议（只读）+ 确认后写入计划日期"""

    def __init__(self, goal_manager=None):
        # 复用全局 GoalManager；未注入时延迟导入默认实例（模式同 PlanningMirrorAgent）
        if goal_manager is not None:
            self.goal_manager = goal_manager
        else:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()

    # ========== 内部工具：数据采集与排序 ==========

    def _get_daily_hours(self,
                         daily_hours_override: Optional[float] = None) -> tuple:
        """本次规划用的每日可用小时数，以及这个数是从哪来的

        返回 (小时数, 来源)，来源取 HOURS_FROM_* 三值之一：
        - 传了 daily_hours_override（首页「本次可用小时」框里的数）⇒ 直接用它，
          来源记 override，并且**不写档案**（spec §2.1 裁定 5：本次调整只影响本次预览）；
        - 没传 ⇒ 走原逻辑读档案 daily_time，缺失/非法时回退默认值（同规划镜像）；
          档案里没这个 key 或值是空白时来源记 fallback，给了值记 profile。
        """
        if daily_hours_override is not None:
            return float(daily_hours_override), HOURS_FROM_OVERRIDE
        try:
            raw = ProfileManager.get("daily_time")
        except Exception:
            raw = None  # 档案读取失败不影响规划
        # 「缺值」按档案字面判：ProfileManager.get 对不存在的 key 返回 None，
        # 存过空串也算没给值。解析不出正数（如「abc」「0小时」）仍按有值处理——
        # 那半由 parse_daily_hours 负责回落，这里不复制它的判断，免得两处漂移。
        missing = raw is None or not str(raw).strip()
        return parse_daily_hours(raw), (HOURS_FROM_FALLBACK if missing
                                        else HOURS_FROM_PROFILE)

    @staticmethod
    def _deadline_sort_key(deadline: str) -> float:
        """截止日期排序键：早截止在前；无日期/格式非法排最后（稳定可复现）"""
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", (deadline or "").strip())
        if not match:
            return _NO_DEADLINE_KEY
        try:
            y, m, d = (int(x) for x in match.groups())
            return y * 10000 + m * 100 + d
        except ValueError:
            return _NO_DEADLINE_KEY

    def _collect_candidates(self) -> List[Dict[str, Any]]:
        """收集所有进行中目标的未完成任务，估算耗时并按规则排序

        排序规则（需求约定）：任务优先级数字小者优先 → 目标截止日期早者在前；
        再以目标优先级、任务 ID 兜底，保证顺序稳定可复现。
        """
        candidates: List[Dict[str, Any]] = []
        for goal in self.goal_manager.list_goals(status="active"):
            for t in self.goal_manager.get_tasks(goal.goal_id):
                if (t.get("status") or "pending") == "done":
                    continue  # 已完成任务不进入今日计划
                candidates.append({
                    "task_id": t["id"],
                    "goal_id": goal.goal_id,
                    "task_title": t.get("title") or "",
                    "goal_title": goal.title,
                    "priority": int(t.get("priority") or 1),
                    "goal_priority": int(goal.priority or 2),
                    "deadline": goal.deadline or "",
                    "estimated_hours": estimate_task_hours(t.get("title") or ""),
                    # 可自动执行标记（v3.7.5）：本地关键词规则先行预判，
                    # LLM 阶段若给出更准确的 auto 标记会覆盖此默认值
                    "auto_executable": guess_auto_executable(
                        t.get("title") or "", t.get("description") or ""),
                })
        candidates.sort(key=lambda c: (c["priority"],
                                       self._deadline_sort_key(c["deadline"]),
                                       c["goal_priority"], c["task_id"]))
        return candidates

    @staticmethod
    def _get_experiences() -> List[Dict[str, Any]]:
        """检索与"今日计划"相关的经验；异常时返回空列表，不阻断主流程"""
        try:
            return ExperienceStore.search_experiences(EXPERIENCE_QUERY, limit=EXPERIENCE_LIMIT)
        except Exception as e:
            print(f"[DailyPlanner] 经验检索失败（忽略）: {e}")
            return []

    # ========== 功能一：生成今日计划（只读） ==========

    @staticmethod
    def _fallback_plan(candidates: List[Dict[str, Any]],
                       daily_hours: float) -> List[Dict[str, Any]]:
        """本地规则兜底：按排序依次入选任务，直到可用时间用尽

        规则：第一个任务无条件入选（保证有产出、供用户取舍）；
        之后仅当"累计耗时 + 当前任务估时 ≤ 可用时间"才继续纳入；
        总数不超过 MAX_PLAN_TASKS。
        """
        plan: List[Dict[str, Any]] = []
        used = 0.0
        for i, c in enumerate(candidates):
            if len(plan) >= MAX_PLAN_TASKS:
                break
            if i > 0 and used + float(c["estimated_hours"]) > daily_hours:
                break
            if i == 0:
                reason = f"候选中优先级最高（P{c['priority']}），应最先推进"
            else:
                reason = f"优先级 P{c['priority']}，预计 {c['estimated_hours']} 小时，仍在今日容量内"
            plan.append({**c, "reason": reason})
            used += float(c["estimated_hours"])
        return plan

    def _ask_llm_plan(self, candidates: List[Dict[str, Any]], daily_hours: float,
                      experiences: List[Dict[str, Any]]) -> tuple:
        """调用 LLM 从候选任务中挑选今日计划

        Returns:
            (plan_items, summary, advice, degraded)
            degraded=True 表示 LLM 失败或输出无效，应由调用方走本地规则兜底。
        """
        task_lines = "\n".join(
            f"- task_id={c['task_id']}｜「{c['task_title']}」｜目标「{c['goal_title']}」｜"
            f"任务优先级:P{c['priority']}｜目标截止:{c['deadline'] or '无'}｜"
            f"预估:{c['estimated_hours']}小时"
            for c in candidates[:MAX_PROMPT_TASKS]
        ) or "（无）"
        exp_lines = "\n".join(
            f"- 场景「{e.get('scenario') or ''}」→ 策略「{e.get('strategy') or ''}」"
            for e in experiences
        ) or "（暂无相关经验）"

        prompt = f"""你是一位学习规划师。请为用户从候选任务中挑选今天要完成的任务，生成今日计划。

【今日可用时间】约 {daily_hours} 小时
【候选任务（已按任务优先级、截止日期排序）】
{task_lines}

【相关经验（历史验证过的有效做法，安排时可参考）】
{exp_lines}

要求：
1. 最多推荐 {MAX_PLAN_TASKS} 个任务，推荐任务的总预估耗时应尽量不超过可用时间 {daily_hours} 小时；
2. task_id 必须从候选列表中原样选取，禁止编造或修改；
3. 每个任务给出一句话推荐理由；
4. 为每个推荐任务标注 auto 字段：布尔值。资料搜索、整理/总结笔记等可由 AI 自动完成并供用户确认的任务标记 true；需要用户深度参与、线下完成或高风险的任务标记 false；不确定时标记 false；
5. 若候选任务总耗时明显超出可用时间，请在 advice 中给出调整建议（如减少任务数量、顺延低优先级任务）；否则 advice 留空。

严格只输出 JSON，不要输出任何其他文字，格式：
{{"summary":"一句话说明今天的安排思路","advice":"超载时的调整建议，无则留空","plan":[{{"task_id":123,"reason":"推荐理由","auto":true}}]}}"""

        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            print(f"[DailyPlanner] LLM 生成计划失败（降级本地规则）: {e}")
            return [], "", "", True

        data = extract_json(response)
        if not data or not isinstance(data.get("plan"), list):
            return [], "", "", True

        # 校验：过滤不存在的 task_id / 重复项 / 非法条目，最多保留 MAX_PLAN_TASKS 条
        by_id = {c["task_id"]: c for c in candidates}
        seen = set()
        plan: List[Dict[str, Any]] = []
        for item in data["plan"]:
            if len(plan) >= MAX_PLAN_TASKS:
                break
            if not isinstance(item, dict):
                continue
            try:
                tid = int(item.get("task_id"))
            except (ValueError, TypeError):
                continue
            if tid not in by_id or tid in seen:
                continue
            seen.add(tid)
            # 可自动执行标记（v3.7.5）：LLM 给出合法布尔 auto 时以其为准；
            # 缺失或非法时沿用候选收集阶段的本地关键词预判结果
            local_auto = bool(by_id[tid].get("auto_executable"))
            llm_auto = item.get("auto")
            auto_executable = llm_auto if isinstance(llm_auto, bool) else local_auto
            plan.append({**by_id[tid],
                         "reason": str(item.get("reason") or "").strip(),
                         "auto_executable": auto_executable})

        summary = str(data.get("summary") or "").strip()
        advice = str(data.get("advice") or "").strip()
        if not plan:
            # LLM 未给出任何有效任务 → 视为失败，交给本地规则兜底
            return [], summary, advice, True
        return plan, summary, advice, False

    @staticmethod
    def format_plan_markdown(summary: str, plan: List[Dict[str, Any]],
                             total_hours: float, daily_hours: float,
                             experiences: List[Dict[str, Any]],
                             advice: str = "",
                             daily_time_missing: bool = False) -> str:
        """将今日计划格式化为 Markdown 展示

        `daily_time_missing` 为真表示档案里没有 daily_time、这次用的是回落的默认值，
        此时显式说明「尚未设置每日可用时间」——spec §2.1 要求不把默认 2 小时
        当成用户确认过的时间。（本次用了 override 时不走这一支：那时展示的是用户
        刚填的数，再说「本次暂按 2 小时安排」就与上面那行自相矛盾。）
        """
        lines = ["## 📅 今日计划建议", ""]
        lines.append(
            f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜"
            f"今日可用时间：**{daily_hours} 小时**｜"
            f"推荐任务：**{len(plan)}** 个｜预计总耗时：**约 {total_hours} 小时**")
        if daily_time_missing:
            lines.append("")
            lines.append(f"> ⏳ 尚未设置每日可用时间，本次暂按 {DEFAULT_DAILY_HOURS:g} 小时安排")
        if summary:
            lines.append("")
            lines.append(f"**🤖 安排思路**：{summary}")
        lines.append("")
        for i, p in enumerate(plan, start=1):
            auto_tag = " ⚡ 可自动" if p.get("auto_executable") else ""
            lines.append(f"### {i}. {p['task_title']}{auto_tag}")
            lines.append(f"- 所属目标：{p['goal_title']}｜预估耗时：约 {p['estimated_hours']} 小时")
            if p.get("reason"):
                lines.append(f"- 推荐理由：{p['reason']}")
        if total_hours > daily_hours:
            lines.append("")
            lines.append(f"⚠️ **超载提醒**：推荐任务预计共需约 {total_hours} 小时，"
                         f"超过今日可用的 {daily_hours} 小时。")
            lines.append(f"- 调整建议：{advice or '可减少任务数量、缩短单任务投入，或将低优先级任务顺延到明天。'}")
        if experiences:
            lines.append("")
            lines.append("**📚 参考的相关经验**")
            for e in experiences:
                scenario = e.get("scenario") or ""
                strategy = e.get("strategy") or ""
                lines.append(f"- 「{scenario}」→ {strategy}")
        lines.append("")
        lines.append("> 💡 确认后请点击「✅ 应用计划」：将以上任务标记为今天执行（写入计划日期）。")
        if any(p.get("auto_executable") for p in plan):
            lines.append("> 🤖 带 ⚡ 标记的任务可在应用后点击「▶️ 执行计划」由 AI 自动完成（结果需你确认保存）。")
        return "\n".join(lines)

    def generate_daily_plan(self,
                            daily_hours_override: Optional[float] = None
                            ) -> Dict[str, Any]:
        """生成今日计划建议（只分析，不写库）

        Args:
            daily_hours_override: 本次可用小时数，来自首页那只「本次可用小时」输入框。
                非空时这次按它安排；留空则走档案 daily_time（缺失时回落默认值）。
                **两种情况都不写档案**（spec §2.1 裁定 5）。

        Returns:
            {"ok": bool, "message": str, "plan": list, "display": markdown}
            plan 中每项包含：task_id / goal_id / task_title / goal_title /
            estimated_hours / reason / auto_executable（v3.7.5 新增布尔字段，
            标记该任务是否适合 AI 自动执行，默认 False）
        """
        candidates = self._collect_candidates()
        if not candidates:
            return {"ok": False, "plan": [],
                    "message": "⚠️ 暂无进行中目标的待办任务，请先创建目标或添加任务",
                    "display": ""}

        daily_hours, hours_source = self._get_daily_hours(daily_hours_override)
        experiences = self._get_experiences()

        # 主路径：LLM 挑选任务；失败/无效时降级为本地规则
        plan, summary, advice, degraded = self._ask_llm_plan(candidates, daily_hours, experiences)
        if not plan:
            plan = self._fallback_plan(candidates, daily_hours)
            degraded = True

        total_hours = round(sum(float(p["estimated_hours"]) for p in plan), 1)
        display = self.format_plan_markdown(summary, plan, total_hours,
                                            daily_hours, experiences, advice,
                                            daily_time_missing=(
                                                hours_source == HOURS_FROM_FALLBACK))
        source = "（LLM 不可用，来自本地规则）" if degraded else ""
        message = (f"✅ 已生成今日计划：{len(plan)} 个任务，"
                   f"预计约 {total_hours} 小时 / 可用 {daily_hours} 小时{source}")
        return {"ok": True, "message": message, "plan": plan, "display": display}

    # ========== 功能二：应用今日计划（唯一写库入口） ==========

    def apply_daily_plan(self, plan: Any, plan_date: Optional[str] = None) -> Dict[str, Any]:
        """应用今日计划：把建议任务标记为指定日期执行（默认今天）

        Args:
            plan: generate_daily_plan 返回的计划列表（也容忍误传单条字典）
            plan_date: 计划日期（YYYY-MM-DD）；None 表示今天。直接覆盖旧标记，
                       同一计划重复应用幂等，不会产生重复数据。

        Returns:
            {"ok": bool, "applied": int, "skipped": [str], "message": str}
            applied 仅统计实际写入成功的任务数。
        """
        # 容错：误传单条字典时包装为列表（模式同 PlanningMirrorAgent.apply_suggestion）
        if isinstance(plan, dict):
            plan = [plan]
        target_date = (plan_date or _today()).strip()
        applied, skipped = 0, []
        for item in plan or []:
            if not isinstance(item, dict):
                skipped.append("无效条目（非字典）已跳过")
                continue
            try:
                tid = int(item.get("task_id"))
            except (ValueError, TypeError):
                skipped.append(f"无效任务ID「{item.get('task_id')}」已跳过")
                continue
            try:
                if self.goal_manager.set_task_planned_date(tid, target_date):
                    applied += 1
                else:
                    skipped.append(f"#{tid} 任务不存在（可能已被删除）已跳过")
            except Exception as e:
                skipped.append(f"#{tid} 写入失败：{e}")

        if applied > 0:
            message = f"✅ 已将 {applied} 个任务加入 {_today()} 的计划"
        else:
            message = "⚠️ 没有任务被标记，请重新生成计划后再试"
        return {"ok": applied > 0, "applied": applied,
                "skipped": skipped, "message": message}


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent / intent_agent / planning_mirror_agent）
daily_planner_agent = DailyPlannerAgent()
