"""
概念测验技能 - 基于知识点生成测验题，检验掌握程度
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


class SkillImpl(Skill):
    """概念测验技能实现"""

    def on_load(self):
        """技能加载时初始化依赖管理器"""
        try:
            from memory.knowledge_graph import KnowledgeGraph
            from memory.learning_recorder import LearningRecorder
            self.knowledge_graph = KnowledgeGraph
            self.learning_recorder = LearningRecorder
        except Exception as e:
            print(f"[ConceptQuiz] 初始化失败: {e}")
            self.knowledge_graph = None
            self.learning_recorder = None

    def validate_input(self, context: Dict[str, Any]) -> bool:
        """验证输入参数（所有参数可选，验证通过）"""
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行概念测验

        Args:
            context: 包含 concept、difficulty、num_questions 的上下文

        Returns:
            包含 result 的结果字典
        """
        # 注意：dict.get(key, default) 只在 key 缺失时给默认值；
        # key 存在但值为 None（如用户未填写输入）时仍需 (变量 or 默认) 兜底，
        # 否则 None.strip() 会抛 AttributeError
        concept = (context.get("concept") or "").strip()
        difficulty = (context.get("difficulty") or "medium").strip().lower()
        num_questions = context.get("num_questions", 3)

        # 参数校验
        if difficulty not in ("easy", "medium", "hard"):
            difficulty = "medium"
        try:
            num_questions = max(1, min(5, int(num_questions)))
        except (ValueError, TypeError):
            num_questions = 3

        # 如果未指定概念，自动选取薄弱知识点
        if not concept:
            concept = self._select_weak_concept()
            if not concept:
                return {"error": "未找到薄弱知识点，请手动指定要测验的概念"}

        try:
            return self._generate_quiz(concept, difficulty, num_questions)
        except Exception as e:
            print(f"[ConceptQuiz] 执行失败: {e}")
            return {"error": f"执行失败: {str(e)}"}

    def _select_weak_concept(self) -> str:
        """从薄弱知识点中自动选取一个概念"""
        # 优先从知识图谱薄弱节点中选取
        if self.knowledge_graph:
            try:
                weak_nodes = self.knowledge_graph.get_weak_nodes(limit=5)
                if weak_nodes:
                    return weak_nodes[0]["name"]
            except Exception:
                pass

        # 其次从学习记录薄弱点中选取
        if self.learning_recorder:
            try:
                weak_points = self.learning_recorder.get_weak_points(limit=5)
                if weak_points:
                    return weak_points[0]["topic"]
            except Exception:
                pass

        return ""

    def _generate_quiz(self, concept: str, difficulty: str, num_questions: int) -> Dict[str, Any]:
        """调用 LLM 生成测验题"""
        from business.model_client import chat_sync

        difficulty_map = {
            "easy": "基础概念，适合初学者",
            "medium": "中等难度，需要理解核心原理",
            "hard": "较高难度，需要深入理解和应用",
        }
        difficulty_desc = difficulty_map.get(difficulty, "中等难度")

        prompt = f"""你是一个出题专家，擅长设计精准的概念测验题。

请针对以下知识点生成 {num_questions} 道单选题：
知识点：{concept}
难度：{difficulty_desc}

要求：
1. 每道题有 4 个选项（A/B/C/D），只有 1 个正确答案
2. 题目应考察对核心概念的理解，而非细枝末节
3. 每道题附带简要解析

请以 JSON 格式输出（只输出 JSON）：
{{
  "questions": [
    {{
      "question": "题目内容",
      "options": ["A. 选项1", "B. 选项2", "C. 选项3", "D. 选项4"],
      "answer": "A",
      "explanation": "解析说明"
    }}
  ]
}}"""

        try:
            response = chat_sync(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1500,
            )
        except Exception as e:
            return {"error": f"调用 LLM 失败: {str(e)}"}

        # 解析 JSON
        result = self._parse_json_response(response)
        if not result:
            return {"error": "无法解析 LLM 响应", "raw_response": response}

        questions = result.get("questions", [])
        if not questions:
            return {"error": "LLM 未返回有效题目"}

        # 格式化输出
        formatted = self._format_quiz(concept, difficulty, questions)
        return {"result": formatted}

    def _format_quiz(self, concept: str, difficulty: str, questions: List[Dict]) -> str:
        """将测验题格式化为 Markdown"""
        difficulty_label = {"easy": "🟢 基础", "medium": "🟡 中等", "hard": "🔴 困难"}
        label = difficulty_label.get(difficulty, "🟡 中等")

        lines = [f"## 📝 概念测验：{concept}\n"]
        lines.append(f"**难度：** {label} | **题目数：** {len(questions)}\n")
        lines.append("---\n")

        for i, q in enumerate(questions, 1):
            # LLM 返回的 JSON 字段可能缺失或为 null：统一用 (字段 or 默认) 兜底，
            # 避免显示 "None" 或对 None 迭代崩溃
            question = q.get("question") or "未知题目"
            options = q.get("options") or []
            answer = q.get("answer") or "?"
            explanation = q.get("explanation") or ""

            lines.append(f"### 第 {i} 题")
            lines.append(f"**{question}**\n")

            for opt in options:
                lines.append(f"  {opt}")
            lines.append("")

            lines.append(f"**✅ 答案：** {answer}")
            if explanation:
                lines.append(f"**💡 解析：** {explanation}")
            lines.append("\n---\n")

        # 学习建议
        lines.append("### 📌 学习建议")
        lines.append("完成测验后，如果某些题目答错，建议：")
        lines.append("- 重新学习相关概念（使用「概念讲解」技能）")
        lines.append("- 将薄弱点加入学习计划（使用「学习计划生成器」技能）")

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
