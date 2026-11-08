# -*- coding: utf-8 -*-
"""ui.controllers.profile_controller —— 档案与记忆

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""

from memory.knowledge_graph import KnowledgeGraph
from memory import ChatStore, init_db, ProfileManager, LearningRecorder
from memory.learning_recorder import format_mastery_stars, format_mastery_ratio
import json
import re




def save_all_profiles(eng, ai, time, style, goal):
    profiles = {
        "english_level": eng or "",
        "ai_basis": ai or "",
        "daily_time": time or "",
        "prefer_style": style or "",
        "long_goal": goal or "",
    }
    for k, v in profiles.items():
        if v.strip():
            ProfileManager.set(k, v.strip())
    return "✅ 已保存全部档案", get_memory_display()


def load_profile_to_ui():
    profile = ProfileManager.get_all()
    return (
        profile.get("english_level", ""),
        profile.get("ai_basis", ""),
        profile.get("daily_time", ""),
        profile.get("prefer_style", ""),
        profile.get("long_goal", ""),
    )


def analyze_and_update_profile(history):
    if not history:
        return "⚠️ 没有对话历史", get_memory_display()
    conversation_text = ""
    for msg in history[-20:]:
        role = "用户" if msg["role"] == "user" else "助手"
        conversation_text += f"{role}：{msg.get('content', '')[:300]}\n"
    prompt = (
        "请根据以下对话，提取用户的个人信息，以 JSON 格式返回，没有的字段可留空字符串：\n"
        "```json\n{\n"
        '"english_level": "英语水平",\n'
        '"ai_basis": "AI基础",\n'
        '"daily_time": "每天可学习时间",\n'
        '"prefer_style": "偏好的讲解方式",\n'
        '"long_goal": "长期学习目标"\n'
        "}\n```\n\n对话内容：\n" + conversation_text +
        "\n只输出 JSON，不要任何解释。"
    )
    messages = [
        {"role": "system", "content": "你是用户档案分析助手，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    # 预算与重试统一实现（P7 批次 10-①）：本函数的输出直接 json.loads，
    # 属「输出当 JSON 解析」类——原先 max_tokens=300 低于推理地板 1500，
    # 截断即解析失败，用户只看到"❌ 分析失败"。
    from core.ai_filter import chat_sync_with_retry_budget
    try:
        result = chat_sync_with_retry_budget(messages, temperature=0.3)
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', result)
        if json_match:
            result = json_match.group(1)
        data = json.loads(result)
        for key in ["english_level", "ai_basis", "daily_time", "prefer_style", "long_goal"]:
            value = data.get(key, "").strip()
            if value:
                ProfileManager.set(key, value)
        return "✅ 档案已更新", get_memory_display()
    except Exception as e:
        return f"❌ 分析失败：{e}", get_memory_display()


def get_memory_display() -> str:
    parts = []
    profile = ProfileManager.get_all()
    if profile:
        parts.append("**👤 用户档案**")
        labels = {
            "english_level": "英语水平", "ai_basis": "AI基础",
            "daily_time": "每日时间", "prefer_style": "讲解偏好",
            "long_goal": "长期目标",
        }
        for k, v in profile.items():
            parts.append(f"- {labels.get(k, k)}：{v}")
    else:
        parts.append("**👤 用户档案**\n- ⚠️ 尚未设置")
    parts.append("")
    records = LearningRecorder.get_recent_records(limit=5)
    if records:
        parts.append("**📚 最近学习**")
        for r in records:
            # 未评渲染成「未评」（P7 批次 12-①）：此前 `"⭐" * r["mastery_score"]`
            # 遇 NULL 直接 TypeError，整个档案与记忆面板打不开。
            stars = format_mastery_stars(r["mastery_score"])
            parts.append(f"- [{r['subject']}] {r['topic']} {stars}")
    else:
        parts.append("**📚 最近学习**\n- 暂无记录")
    parts.append("")
    tasks = LearningRecorder.get_pending_tasks(limit=5)
    if tasks:
        parts.append("**📋 待办任务**")
        for t in tasks:
            icon = {1: "🟢", 2: "🟡", 3: "🔴"}.get(t["priority"], "⚪")
            parts.append(f"- {icon} {t['title']}")
    else:
        parts.append("**📋 待办任务**\n- 暂无任务")
    parts.append("")
    weak = LearningRecorder.get_weak_points(limit=5)
    if weak:
        parts.append("**⚠️ 薄弱知识点**")
        for w in weak:
            parts.append(f"- {w['topic']}（掌握度: {format_mastery_ratio(w['mastery_score'])}）")
    else:
        parts.append("**⚠️ 薄弱知识点**\n- 暂无记录")
    return "\n".join(parts)


def get_knowledge_tree_display(domain: str) -> str:
    tree = KnowledgeGraph.get_domain_tree(domain)
    if not tree:
        return "暂无知识节点"
    lines = []
    def render(nodes, depth=0):
        for n in nodes:
            prefix = "  " * depth + ("├─ " if depth > 0 else "")
            lines.append(f"{prefix}{n['name']} {'⭐'*n['mastery_level']}")
            if 'children' in n:
                render(n['children'], depth+1)
    render(tree)
    return "\n".join(lines)
