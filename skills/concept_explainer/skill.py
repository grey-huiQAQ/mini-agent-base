"""
概念讲解技能 - 示例技能
通过 LLM 将复杂概念讲解为通俗易懂的语言

（接线状态 (2026-09-12)：Phase 2 曾在此标注「未接线」——本技能当时只有目录与
manifest、没有任何 UI 入口；P4-9 已把它接到「🧩 技能列表」页的「📖 概念讲解」区块，
回调为 ui/controllers/learning_controller.explain_concept。调用入口仍是统一的
runtime.skill_manager.run_skill("concept_explainer", {...})，本文件未因接线改动。）
"""
from typing import Any, Dict
import sys
from pathlib import Path

# 将项目根目录加入路径，以便导入 business 模块
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.skill_base import Skill


class SkillImpl(Skill):
    """概念讲解技能实现"""

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行概念讲解

        Args:
            context: 包含 concept（概念名称）和 level（目标水平）的上下文

        Returns:
            包含 explanation 的结果字典
        """
        concept = context.get("concept", "")
        level = context.get("level", "beginner")

        if not concept:
            return {"error": "请提供需要讲解的概念名称"}

        # 构建讲解提示词
        level_map = {
            "beginner": "零基础初学者",
            "intermediate": "有一定基础的学习者",
            "advanced": "进阶学习者",
        }
        target_level = level_map.get(level, "零基础初学者")

        prompt = (
            f"请用通俗易懂的语言向{target_level}讲解以下概念：{concept}\n\n"
            f"要求：\n"
            f"1. 使用日常生活中的类比\n"
            f"2. 给出 1-2 个具体实例\n"
            f"3. 总结核心要点\n"
            f"4. 控制在 300 字以内"
        )

        try:
            from business.model_client import chat_sync
            explanation = chat_sync(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500,
            )
            return {"explanation": explanation}
        except Exception as e:
            return {"error": f"调用 LLM 失败: {str(e)}"}

    def validate_input(self, context: Dict[str, Any]) -> bool:
        """验证输入参数"""
        return bool(context.get("concept"))
