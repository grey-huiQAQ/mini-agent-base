"""
Daily Agent - 每日计划与复盘
"""
import json
from datetime import datetime, timedelta
from business.model_client import chat_sync
from memory import ProfileManager, LearningRecorder
from memory.knowledge_graph import KnowledgeGraph
from memory.daily_planner import DailyPlanner

def _get_yesterday_date():
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

def _get_today_date():
    return datetime.now().strftime("%Y-%m-%d")

def _generate_plan() -> str:
    # 1. Observe 数据
    profile = ProfileManager.get_all()
    long_goal = profile.get("long_goal", "未设置长期目标")
    daily_time = profile.get("daily_time", "未设置每日时间")
    
    yesterday = _get_yesterday_date()
    recent_records = LearningRecorder.get_records_by_date(yesterday)
    weak_nodes = KnowledgeGraph.get_weak_nodes(domain="AI", limit=5)  # 也可汇总多领域
    pending_tasks = LearningRecorder.get_pending_tasks(limit=5)
    
    # 组装上下文
    context = {
        "长期目标": long_goal,
        "每日可用时间": daily_time,
        "昨日学习": [r["topic"] for r in recent_records],
        "薄弱知识点": [n["name"] for n in weak_nodes],
        "待办任务": [t["title"] for t in pending_tasks],
    }
    
    prompt = f"""你是一位学习规划师。根据以下信息，生成今日学习计划。
信息：
{json.dumps(context, ensure_ascii=False, indent=2)}

请输出 JSON 格式的计划，包含 2-5 个学习任务，每个任务包含标题、优先级(1-3)、预计时长(分钟)和简短说明。
格式：
{{"tasks":[{{"title":"...","priority":1,"duration":30,"description":"..."}}]}}
只输出 JSON，不要其他内容。"""
    
    # 预算复用 core.ai_filter 的单一常量（P7 批次 9-②）：这里的 500 曾低于它自己记录的
    # "实测 600 会被 reasoning_content 耗尽"，推理型模型下必然截断。函数内延迟导入，
    # 不复制字面量（本仓已被"两处手工复制保持一致"咬过两次）。
    from core.ai_filter import DEFAULT_MAX_TOKENS
    try:
        response = chat_sync(
            messages=[
                {"role": "system", "content": "你是学习规划助手，只输出 JSON。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=DEFAULT_MAX_TOKENS
        )
    except Exception as e:
        # 诚实降级（P7 批次 9-②）：本函数在调用链上没有 try（ui/app_factory.py 也没有），
        # 不接住就是 Gradio 层的一次报错事件。计划没生成出来就说没生成。
        return f"⚠️ 本次计划生成失败：{e}"
    # 提取 JSON
    try:
        data = json.loads(response)
        # 保存为格式化的 JSON 字符串
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        # 模型未返回合法 JSON 时原样透传，交由上层展示
        return response

def generate_daily_plan() -> str:
    """生成今日计划并保存，返回计划文本"""
    today = _get_today_date()
    plan_text = _generate_plan()
    DailyPlanner.save_plan(today, plan_text)
    return plan_text

def _generate_review() -> str:
    today = _get_today_date()
    plan_text = DailyPlanner.get_plan(today)
    if not plan_text:
        plan_json = {"tasks": []}
    else:
        try:
            plan_json = json.loads(plan_text)
        except (json.JSONDecodeError, TypeError):
            plan_json = {"tasks": []}
    
    # 今日学习记录
    today_records = LearningRecorder.get_records_by_date(today)
    
    # 完成率简单估算：记录数 / 计划任务数
    task_count = len(plan_json.get("tasks", []))
    record_count = len(today_records)
    completion_rate = min(1.0, record_count / task_count) if task_count > 0 else 0.0
    
    prompt = f"""你是一位学习复盘教练。根据以下数据，生成今日学习复盘。
计划任务：{json.dumps(plan_json.get("tasks", []), ensure_ascii=False)}
实际学习记录：{[r["topic"] for r in today_records]}
完成率：{completion_rate:.0%}

请输出 JSON 格式的复盘，包含：
- summary: 今日总结（50字内）
- completed: 已完成的学习主题列表
- unfinished: 未完成的计划任务列表
- insight: 学习收获或问题
- suggestion: 明日建议

格式：
{{"summary":"...","completed":["..."],"unfinished":["..."],"insight":"...","suggestion":"..."}}
只输出 JSON，不要其他内容。"""
    
    # 预算与降级口径同 _generate_plan（P7 批次 9-②）：复用 core.ai_filter 的单一常量
    from core.ai_filter import DEFAULT_MAX_TOKENS
    try:
        response = chat_sync(
            messages=[
                {"role": "system", "content": "你是学习复盘助手，只输出 JSON。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=DEFAULT_MAX_TOKENS
        )
    except Exception as e:
        return f"⚠️ 本次复盘生成失败：{e}"
    try:
        data = json.loads(response)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return response

def generate_daily_review() -> str:
    """生成今日复盘并落库保存（DailyPlanner.save_review），返回复盘文本。

    注：docstring 曾声称“更新用户画像”，实际尚未实现——下方分支仅解析
    suggestion 后占位跳过，未对用户画像做任何写入。
    """
    today = _get_today_date()
    review_text = _generate_review()
    DailyPlanner.save_review(today, review_text)
    
    # 更新画像：例如根据复盘内容调整每日时间等（简化示例）
    try:
        review_json = json.loads(review_text)
        if "suggestion" in review_json:
            # 可以调用 LLM 提取更新，但这里直接跳过或简单保存
            pass
    except (json.JSONDecodeError, TypeError):
        pass
    
    return review_text