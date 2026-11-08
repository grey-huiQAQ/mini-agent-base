"""
Evaluator Agent - 学习评估代理
分析用户消息与助手回复，判断理解程度、迁移能力与误解，
输出评估结果并更新知识图谱与任务。
"""

from typing import Optional, Dict, Any
from memory.knowledge_graph import KnowledgeGraph
from memory.learning_recorder import LearningRecorder

# JSON 提取统一实现（2026-09-12 收敛）：原 _extract_json（不剥代码块）已删除，
# 统一走 core/json_utils 的括号栈定位，并只接受解析结果为对象的情况。
from core.json_utils import extract_json_object

# 预算与重试统一实现（P7 批次 10-①）：评估结果要经 extract_json_object 解析，
# 属「输出当 JSON 解析」类。原先写死 max_tokens=500（低于推理地板 1500），
# 截断后解析返回 None——掌握度 / 知识图谱 / 评估触发的任务全都不更新，
# 而界面上只看到一行控制台日志。
from core.ai_filter import chat_sync_with_retry_budget

EVALUATOR_SYSTEM_PROMPT = """你是一名严格的学习评估专家。根据用户的最新消息和助手的回复，评估用户对相关知识的掌握情况。

要求：
1. 判断用户是否真正理解该知识点（correct / partial / incorrect）。
2. 判断用户是否能迁移应用该知识（如果能迁移，说明理解较好）。
3. 识别用户表达中可能存在的误解或错误。
4. 将评估结果关联到知识图谱节点（尽量使用已有节点名称，若无则给出一个合适的名称）。
5. 给出 mastery 调整建议（-2 ~ +2 的整数），以及 confidence 调整建议（-0.3 ~ +0.3 的小数）。
6. 如果发现误解，生成一个具体的复习任务标题（例如“重新学习 Attention 机制”），否则任务为空字符串。

输出 JSON 格式（不要包含任何多余文字）：
{
  "assessment": "correct" | "partial" | "incorrect",
  "knowledge_node": "节点名称",
  "misconception": "误解描述（可为空）",
  "mastery_delta": 0,
  "confidence_delta": 0.0,
  "suggested_task": ""
}
"""


def evaluate_conversation(
    user_message: str,
    assistant_message: str,
    domain: str = "AI",
    history: Optional[list] = None
) -> Optional[Dict[str, Any]]:
    """
    调用模型评估用户理解情况。
    """
    messages = [
        {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT}
    ]
    if history:
        # 取最近几条历史，帮助评估上下文
        messages.extend(history[-4:])
    messages.append({
        "role": "user",
        "content": f"领域：{domain}\n用户消息：{user_message}\n助手回复：{assistant_message}"
    })

    try:
        response_text = chat_sync_with_retry_budget(
            messages=messages,
            temperature=0.1
        )
         # chat_sync 直接返回字符串，无需 .choices
        content = response_text
        return extract_json_object(content)
    except Exception as e:
        print(f"[Evaluator] 评估调用失败: {e}")
        return None


def apply_evaluation(evaluation: Dict[str, Any], domain: str) -> None:
    """
    将评估结果应用到知识图谱和任务系统。
    """
    if not evaluation:
        return

    node_name = evaluation.get("knowledge_node")
    mastery_delta = evaluation.get("mastery_delta", 0)
    confidence_delta = evaluation.get("confidence_delta", 0.0)
    misconception = evaluation.get("misconception", "")
    suggested_task = evaluation.get("suggested_task", "")
    assessment = evaluation.get("assessment", "")

    if not node_name:
        return

    kg = KnowledgeGraph()

    # 更新知识图谱节点
    try:
        existing = kg.get_node_by_name(domain, node_name)
        if existing:
            new_mastery = max(0, min(5, existing["mastery_level"] + mastery_delta))
            new_confidence = max(0.0, min(1.0, existing["confidence"] + confidence_delta))
            kg.update_mastery(existing["id"], new_mastery, new_confidence)
            print(f"[Evaluator] 节点 '{node_name}' mastery 调整为 {new_mastery}，confidence {new_confidence}")
        else:
            # 节点不存在，创建初始节点
            if assessment == "incorrect":
                initial_mastery = 1
            else:
                initial_mastery = 3
            kg.add_node(
                name=node_name,
                domain=domain,
                mastery_level=initial_mastery,
                confidence=0.5
            )
            print(f"[Evaluator] 未找到节点 '{node_name}'，已创建初始节点")
    except Exception as e:
        print(f"[Evaluator] 更新知识图谱失败: {e}")

    # 创建复习任务（如果有）
    if suggested_task and assessment != "correct":
        try:
            LearningRecorder.add_task(
                title=suggested_task,
                subject=domain,
                priority=2 if assessment == "incorrect" else 1
            )
            print(f"[Evaluator] 已添加复习任务: {suggested_task}")
        except Exception as e:
            print(f"[Evaluator] 添加任务失败: {e}")

    # 可选：记录误解
    if misconception:
        print(f"[Evaluator] 发现误解: {misconception}")
