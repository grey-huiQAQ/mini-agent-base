"""
Prompt Evolution Agent - 提示词优化代理
"""
import json
from business.model_client import chat_sync
from memory.prompt_store import PromptStore

def suggest_improvement(task_name: str, current_prompt: str, score: float, feedback: str = "") -> str:
    """
    根据当前提示词和评分，生成优化建议
    """
    # 最小改动：空输入直接返回，不调用模型
    if not current_prompt or not current_prompt.strip():
        return ""

    prompt = f"""你是一位提示词工程专家。当前有一个提示词版本效果不佳，需要优化。

任务名称：{task_name}
当前提示词：
{current_prompt}

当前评分：{score}/100
用户反馈：{feedback if feedback.strip() else "无"}

请分析问题，并输出改进后的提示词。改进思路可以包括：
- 增加类比、示例
- 调整结构（如先直觉后数学）
- 补充输出格式要求
- 简化语言

输出格式：直接输出完整的改进后提示词，不要包含任何解释或标记。
"""
    try:
        improved_resp = chat_sync(
            messages=[
                {"role": "system", "content": "你是提示词优化专家。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=500
        )
        # chat_sync 返回字符串，直接使用
        if hasattr(improved_resp, 'content'):
            improved_text = improved_resp.content.strip()
        else:
            improved_text = str(improved_resp).strip()
        return improved_text
    except Exception as e:
        print(f"[PromptEvolution] 生成建议失败: {e}")
        return ""

def evaluate_and_evolve(task_name: str, score: float, feedback: str = "") -> dict:
    """
    评估当前版本，如果评分低则生成优化建议（不自动更新）
    返回：{"current_version": int, "suggestion": str}
    """
    versions = PromptStore.get_all_versions(task_name)
    if not versions:
        return {"current_version": 0, "suggestion": ""}
    active = next((v for v in versions if v["is_active"] == 1), versions[0])
    current_version = active["version"]
    current_prompt = active["prompt_text"]

    # 更新评分
    PromptStore.update_score(task_name, current_version, score, feedback)

    # 如果评分 < 80，生成优化建议
    if score < 80:
        suggestion = suggest_improvement(task_name, current_prompt, score, feedback)
        suggestion = suggestion.strip()
        return {"current_version": current_version, "suggestion": suggestion}
    return {"current_version": current_version, "suggestion": ""}

def apply_new_version(task_name: str, new_prompt: str) -> int:
    """人工确认后，应用新版本"""
    # 禁止空提示词写入新版本
    new_prompt = new_prompt.strip()
    if not new_prompt:
        raise ValueError("new_prompt cannot be empty")
    version = PromptStore.add_version(task_name, new_prompt, is_active=1)
    return version