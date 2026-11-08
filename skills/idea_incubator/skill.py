"""
想法孵化器技能 - 将天马行空的想法转化为具体目标或可执行计划
"""
import json
import re
from typing import Any, Dict
from pathlib import Path
import sys

# 将项目根目录加入路径，以便导入 core 和 business 模块
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.skill_base import Skill


class SkillImpl(Skill):
    """想法孵化器技能实现"""

    def on_load(self):
        """技能加载时初始化 GoalManager"""
        try:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()
        except Exception as e:
            print(f"[IdeaIncubator] 初始化 GoalManager 失败: {e}")
            self.goal_manager = None

    def validate_input(self, context: Dict[str, Any]) -> bool:
        """验证输入参数：idea 必填"""
        return bool(context.get("idea", "").strip())

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行想法孵化器

        Args:
            context: 包含 idea（用户想法）和 mode（goal/plan）的上下文

        Returns:
            包含 result 的结果字典
        """
        idea = context.get("idea", "").strip()
        mode = context.get("mode", "goal").strip().lower()

        if not idea:
            return {"error": "请提供你的想法"}

        if mode not in ("goal", "plan"):
            mode = "goal"

        if not self.goal_manager:
            return {"error": "GoalManager 未初始化，无法创建目标"}

        try:
            if mode == "goal":
                return self._incubate_to_goal(idea)
            else:
                return self._incubate_to_plan(idea)
        except Exception as e:
            print(f"[IdeaIncubator] 执行失败: {e}")
            return {"error": f"执行失败: {str(e)}"}

    def _incubate_to_goal(self, idea: str) -> Dict[str, Any]:
        """将想法转化为目标：苏格拉底式提问 + 创建目标"""
        # 预算与重试统一实现（P7 批次 10-①）：输出要过 _parse_json_response，
        # 属「输出当 JSON 解析」类。原先写死 500，低于推理地板 1500。
        from core.ai_filter import chat_sync_with_retry_budget

        prompt = f"""你是一个创意导师，擅长通过苏格拉底式提问帮助用户澄清和完善想法。

用户提出了一个想法：
{idea}

请通过 2-3 个引导性问题帮助用户思考这个想法的核心价值、可行性和具体方向。
然后，基于你的分析，输出一个具体的目标。

请以以下 JSON 格式输出（只输出 JSON）：
{{
  "questions": "你提出的 2-3 个引导性问题（用换行分隔）",
  "analysis": "你对这个想法的简要分析（50字以内）",
  "goal_title": "基于想法提炼的目标标题（10字以内）",
  "goal_description": "目标的详细描述（50字以内）"
}}"""

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

        # 创建目标
        goal_title = result.get("goal_title", "未命名目标")
        goal_desc = result.get("goal_description", idea)

        try:
            goal = self.goal_manager.create_goal(goal_title, goal_desc)
        except Exception as e:
            return {"error": f"创建目标失败: {str(e)}"}

        # 构建返回结果
        output_lines = []
        questions = result.get("questions", "")
        if questions:
            output_lines.append("**💭 引导性思考：**\n")
            for q in questions.strip().split("\n"):
                q = q.strip()
                if q:
                    output_lines.append(f"- {q}")
            output_lines.append("")

        analysis = result.get("analysis", "")
        if analysis:
            output_lines.append(f"**📊 想法分析：** {analysis}\n")

        output_lines.append(f"**🎯 已创建目标：** {goal.title}")
        output_lines.append(f"**📝 目标描述：** {goal.description}")
        output_lines.append(f"**🆔 目标 ID：** `{goal.goal_id}`")

        return {
            "result": "\n".join(output_lines),
            "goal_id": goal.goal_id,
            "goal_title": goal.title,
        }

    def _incubate_to_plan(self, idea: str) -> Dict[str, Any]:
        """将想法转化为目标 + 可执行任务"""
        # 预算与重试统一实现（P7 批次 10-①）：输出要过 _parse_json_response。
        # 这里原先写 800，仍低于推理地板 1500（"比 600 大"不等于"够用"）。
        from core.ai_filter import chat_sync_with_retry_budget

        prompt = f"""你是一个项目规划专家，擅长将模糊的想法转化为具体可执行的计划。

用户提出了一个想法：
{idea}

请先分析这个想法，然后将其拆解为 3-5 个具体、可执行的任务。

请以以下 JSON 格式输出（只输出 JSON）：
{{
  "goal_title": "基于想法的目标标题（10字以内）",
  "goal_description": "目标的详细描述（50字以内）",
  "analysis": "对想法的简要分析（50字以内）",
  "tasks": [
    {{"title": "任务标题", "description": "任务描述", "priority": 1}},
    {{"title": "任务标题", "description": "任务描述", "priority": 2}}
  ]
}}

其中 priority 为 1-3 的整数，1 表示高优先级。"""

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

        # 创建目标
        goal_title = result.get("goal_title", "未命名目标")
        goal_desc = result.get("goal_description", idea)

        try:
            goal = self.goal_manager.create_goal(goal_title, goal_desc)
        except Exception as e:
            return {"error": f"创建目标失败: {str(e)}"}

        # 添加任务
        tasks = result.get("tasks", [])
        created_count = 0
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
                    goal.goal_id, title, desc, priority, source="ai_generated"
                )
                created_count += 1
            except Exception as e:
                print(f"[IdeaIncubator] 添加任务失败: {e}")

        # 构建返回结果
        output_lines = []
        analysis = result.get("analysis", "")
        if analysis:
            output_lines.append(f"**📊 想法分析：** {analysis}\n")

        output_lines.append(f"**🎯 已创建目标：** {goal.title}")
        output_lines.append(f"**📝 目标描述：** {goal.description}")
        output_lines.append(f"**🆔 目标 ID：** `{goal.goal_id}`\n")
        output_lines.append(f"**📋 已生成 {created_count} 个任务：**")
        for i, task in enumerate(tasks[:created_count], 1):
            title = task.get("title", "")
            pri = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}.get(task.get("priority", 1), "⚪")
            output_lines.append(f"  {i}. {title} [{pri}]")

        return {
            "result": "\n".join(output_lines),
            "goal_id": goal.goal_id,
            "goal_title": goal.title,
            "task_count": created_count,
        }

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
