"""
事件处理器
负责注册事件回调，处理自动学习记录、学习评估等
"""
from core.event_bus import EventBus
from memory.learning_recorder import LearningRecorder, normalize_mastery_score
from memory.knowledge_graph import KnowledgeGraph
from agents.evaluator import evaluate_conversation, apply_evaluation

# JSON 提取统一实现（2026-09-12 收敛）：原模块内的 parse_learning_json 及其嵌套
# 括号栈辅助函数已上移到 core/json_utils；本模块继续以同名符号对外提供，
# 调用方（record_learning_from_text 等）无需改动。
from core.json_utils import parse_learning_json


def register_event_handlers(event_bus: EventBus):
    """注册所有事件处理器"""
    event_bus.subscribe("assistant_message_completed", handle_auto_record_learning)
    event_bus.subscribe("assistant_message_completed", handle_evaluation)


def record_learning_from_text(message: str, agent_type: str, domain: str) -> bool:
    """
    从助手回复中提取学习记录并保存（供手动总结和其他模块调用）

    Returns:
        True  —— 解析出学习记录并确实写入了库；
        False —— 这段文本不是合法学习 JSON（缺 topic / summary），**没有写入任何记录**。

    为什么要返回这个布尔（P7 批次 11-①）：调用方有两种，语义要求相反——
    「用户手动点按钮」那条路（ui/controllers/chat_controller.manual_summarize）
    必须让用户知道"没写进去"，否则他会以为记录已经存上了；而事件订阅那条路
    （handle_auto_record_learning，普通聊天每轮都会触发）的静默才是正确行为，
    把它变成用户可见提示等于每轮都往对话里塞噪声。故这里只**报告事实**，
    由调用方决定是否出声。
    """
    data = parse_learning_json(message)
    if not data:
        return False

    subject_map = {"AI学习": "AI", "英语": "英语", "系统科学": "系统科学", "通用助手": "通用"}
    subject = subject_map.get(agent_type, domain)

    LearningRecorder.add_learning_record(
        subject=subject,
        topic=data.get("topic", "未命名"),
        summary=data.get("summary", ""),
        # 不给兜底数字（P7 批次 12-①）：模型没给就是"未评"，落库为 NULL。
        # 这里原先是 `data.get("mastery_score", 3)`——那个 3 让"没测到"变成了
        # "中等偏上"，而且同一个缺失在下面 :54 还有第二个答案。
        mastery_score=data.get("mastery_score"),
        difficulty=1,
        weak_points=data.get("weak_points", []),
    )

    topic = data.get("topic", "")
    mastery = normalize_mastery_score(data.get("mastery_score"))
    # 分数缺失时整段知识图谱写入都跳过（P7 批次 12-①）：knowledge_nodes 那行记的
    # 就是掌握度，没有掌握度就没有可记的东西（主题与摘要已经在 learning_records 里）。
    # 节点已存在就少知道一件事、别改它（宁可漏一次，也不要把 None/0 写成掌握度）；
    # 节点不存在也不新建——新建一个"掌握度未知"的节点，等于在图谱里放一行永远
    # 不该被复习算法读到的数据。
    if topic and mastery is not None:
        existing = KnowledgeGraph.get_node_by_name(subject, topic)
        if existing:
            KnowledgeGraph.update_mastery(existing["id"], mastery, confidence=0.7)
        else:
            KnowledgeGraph.add_node(name=topic, domain=subject, mastery_level=mastery, confidence=0.7)

    next_task = data.get("next_task", "")
    if next_task:
        LearningRecorder.add_task(title=next_task, subject=subject, priority=2)

    print(f"[AutoRecord] 已记录：{topic}")
    return True


def handle_auto_record_learning(event):
    """事件处理：自动记录学习"""
    data = event.data
    message = data.get("message", "")
    agent_type = data.get("agent_type", "通用助手")
    domain = data.get("domain", "通用")
    record_learning_from_text(message, agent_type, domain)


def handle_evaluation(event):
    """事件处理：学习评估"""
    data = event.data
    user_message = data.get("user_message", "")
    assistant_message = data.get("message", "")
    domain = data.get("domain", "通用")

    if not user_message or not assistant_message:
        return

    evaluation = evaluate_conversation(
        user_message=user_message,
        assistant_message=assistant_message,
        domain=domain,
    )
    if evaluation:
        apply_evaluation(evaluation, domain)