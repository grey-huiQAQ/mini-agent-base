"""
学习计划生成器技能 - 根据目标、薄弱点、待办任务生成个性化学习计划
"""
import json
import re
from typing import Any, Dict, List
from pathlib import Path
import sys

# 将项目根目录加入路径，以便导入 core 和 business 模块
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.skill_base import Skill

# 掌握度渲染的唯一真源（P7 批次 12-①）：本模块把它拼进提示词，未评必须是「未评」
from memory.learning_recorder import format_mastery_ratio


class SkillImpl(Skill):
    """学习计划生成器技能实现"""

    def on_load(self):
        """技能加载时初始化依赖管理器"""
        try:
            from core.goal_manager import GoalManager
            from memory.learning_recorder import LearningRecorder
            from memory.knowledge_graph import KnowledgeGraph
            self.goal_manager = GoalManager()
            self.learning_recorder = LearningRecorder
            self.knowledge_graph = KnowledgeGraph
        except Exception as e:
            print(f"[StudyPlanGenerator] 初始化失败: {e}")
            self.goal_manager = None
            self.learning_recorder = None
            self.knowledge_graph = None

    def validate_input(self, context: Dict[str, Any]) -> bool:
        """验证输入参数（所有参数可选，验证通过）"""
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行学习计划生成

        Args:
            context: 包含 goal_id、days、save_tasks 的上下文

        Returns:
            包含 result 的结果字典
        """
        goal_id = context.get("goal_id", "").strip() or None
        days = context.get("days", 7)
        save_tasks = context.get("save_tasks", False)

        # 确保 days 为合理整数
        try:
            days = max(1, min(30, int(days)))
        except (ValueError, TypeError):
            days = 7

        if not self.goal_manager:
            return {"error": "GoalManager 未初始化"}

        try:
            # 1. 收集信息
            info = self._collect_info(goal_id)

            # 2. 调用 LLM 生成计划
            plan = self._generate_plan(info, days)
            if "error" in plan:
                return plan

            # 3. 可选：保存为任务
            saved_count = 0
            if save_tasks and goal_id:
                saved_count = self._save_tasks(goal_id, plan)

            # 4. 构建结果
            result = self._format_result(plan, info, days, goal_id, saved_count)
            return {"result": result}

        except Exception as e:
            print(f"[StudyPlanGenerator] 执行失败: {e}")
            return {"error": f"执行失败: {str(e)}"}

    def _collect_info(self, goal_id: str = None) -> Dict[str, Any]:
        """收集学习信息：目标、薄弱点、待办任务、复习推荐"""
        info = {
            "goal_title": "",
            "goal_description": "",
            "pending_tasks": [],
            "weak_points": [],
            "review_recommendations": [],
        }

        # 目标信息
        if goal_id and self.goal_manager:
            goal = self.goal_manager.get_goal(goal_id)
            if goal:
                info["goal_title"] = goal.title
                info["goal_description"] = goal.description
                info["pending_tasks"] = self.goal_manager.get_tasks(goal_id, status="pending")

        # 薄弱知识点
        if self.learning_recorder:
            try:
                info["weak_points"] = self.learning_recorder.get_weak_points(limit=5)
            except Exception:
                pass

        # 复习推荐
        if self.knowledge_graph:
            try:
                info["review_recommendations"] = self.knowledge_graph.get_review_recommendations(limit=5)
            except Exception:
                pass

        return info

    def _generate_plan(self, info: Dict[str, Any], days: int) -> Dict[str, Any]:
        """调用 LLM 生成学习计划"""
        # 预算与重试统一实现（P7 批次 10-①）：输出要过 _parse_json_response，
        # 属「输出当 JSON 解析」类。原先写死 1000，低于推理地板 1500——
        # 截断后只会返回"无法解析 LLM 响应"。
        from core.ai_filter import chat_sync_with_retry_budget

        # 构建上下文信息
        context_parts = []

        if info["goal_title"]:
            context_parts.append(f"当前目标：{info['goal_title']}")
            if info["goal_description"]:
                context_parts.append(f"目标描述：{info['goal_description']}")

        if info["pending_tasks"]:
            task_list = "\n".join([f"  - {t['title']}" for t in info["pending_tasks"][:5]])
            context_parts.append(f"待办任务：\n{task_list}")

        if info["weak_points"]:
            # 未评渲染成「未评」而不是「None/5」（P7 批次 12-①）：这一段会直接进提示词，
            # 出现 None 或 0/5 等于把"没测到"当成事实喂给模型。
            weak_list = "\n".join(
                [f"  - {w['topic']}（掌握度: {format_mastery_ratio(w['mastery_score'])}）"
                 for w in info["weak_points"][:5]])
            context_parts.append(f"薄弱知识点：\n{weak_list}")

        if info["review_recommendations"]:
            review_list = "\n".join([f"  - {r['name']} [{r['domain']}]" for r in info["review_recommendations"][:5]])
            context_parts.append(f"需要复习的知识点：\n{review_list}")

        context_text = "\n".join(context_parts) if context_parts else "暂无具体上下文信息，请生成通用学习计划"

        prompt = f"""你是一个学习规划专家。请根据以下信息，生成一份 {days} 天的个性化学习计划。

{context_text}

请以 JSON 格式输出（只输出 JSON）：
{{
  "title": "学习计划标题（10字以内）",
  "tasks": [
    {{
      "title": "任务标题",
      "description": "任务描述（30字以内）",
      "priority": 1,
      "day": 1
    }}
  ]
}}

要求：
1. 生成 {min(days * 2, 15)} 个任务
2. priority 为 1-3 的整数，1 表示高优先级
3. day 表示第几天执行，范围 1-{days}
4. 任务应覆盖薄弱知识点和复习内容
5. 每天安排 1-3 个任务"""

        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
        except Exception as e:
            return {"error": f"调用 LLM 失败: {str(e)}"}

        # 解析 JSON
        result = self._parse_json_response(response)
        if not result:
            return {"error": "无法解析 LLM 响应", "raw_response": response}

        return result

    def _save_tasks(self, goal_id: str, plan: Dict[str, Any]) -> int:
        """将计划中的任务保存到目标"""
        tasks = plan.get("tasks", [])
        saved_count = 0
        for task in tasks:
            title = task.get("title", "").strip()
            if not title:
                continue
            desc = task.get("description", "")
            try:
                priority = int(task.get("priority", 1))
            except (ValueError, TypeError):
                priority = 1
            priority = max(1, min(3, priority))
            try:
                self.goal_manager.add_task(
                    goal_id, title, desc, priority, source="ai_generated"
                )
                saved_count += 1
            except Exception as e:
                print(f"[StudyPlanGenerator] 保存任务失败: {e}")
        return saved_count

    def _format_result(self, plan: Dict[str, Any], info: Dict[str, Any],
                       days: int, goal_id: str = None, saved_count: int = 0) -> str:
        """格式化输出结果为 Markdown"""
        lines = []

        title = plan.get("title", "学习计划")
        lines.append(f"## 📚 {title}\n")
        lines.append(f"**计划天数：** {days} 天\n")

        # 上下文摘要
        if info["goal_title"]:
            lines.append(f"**🎯 目标：** {info['goal_title']}\n")

        tasks = plan.get("tasks", [])
        if tasks:
            # 按天分组
            by_day: Dict[int, List[Dict]] = {}
            for t in tasks:
                day = t.get("day", 1)
                by_day.setdefault(day, []).append(t)

            for day_num in sorted(by_day.keys()):
                lines.append(f"### 第 {day_num} 天")
                for t in by_day[day_num]:
                    pri = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}.get(t.get("priority", 1), "⚪")
                    lines.append(f"- **{t['title']}** [{pri}]")
                    if t.get("description"):
                        lines.append(f"  {t['description']}")
                lines.append("")

        if saved_count > 0:
            lines.append(f"**✅ 已保存 {saved_count} 个任务到目标**\n")

        return "\n".join(lines)

    @staticmethod
    def _parse_json_response(text: str) -> dict | None:
        """从 LLM 响应中提取 JSON 对象"""
        # 尝试匹配 ```json ... ``` 块
        match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
        if match:
            json_str = match.group(1).strip()
        else:
            # 尝试匹配 { ... } 块
            start = text.find('{')
            if start == -1:
                return None
            stack = []
            for i in range(start, len(text)):
                if text[i] == '{':
                    stack.append(i)
                elif text[i] == '}':
                    stack.pop()
                    if not stack:
                        json_str = text[start:i + 1]
                        break
            else:
                return None

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None
