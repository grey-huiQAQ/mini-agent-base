"""
Intent Agent - 意图识别与目标建议代理

职责：
1. 从用户最近对话（或手动输入文本）中提取潜在的学习/工作目标意图；
2. 以结构化 JSON 建议呈现：目标标题、描述、优先级（1-3）、预计周期（周）、
   建议操作类型（create_new / associate_existing / adjust_plan / none）；
3. 用户确认后调用 GoalManager 完成对应操作（创建新目标 / 关联既有目标），
   adjust_plan 类型不直接修改数据，提示用户使用 PlannerAgent 调整。

设计说明：
- 与 planner_agent 保持一致：直接通过 business.model_client.chat_sync 调用模型
  （测试时可通过替换 model_client.chat_sync 进行离线 mock）。
- 所有 LLM 输出均经过 extract_json + 校验器过滤；异常时降级为错误提示，
  不抛出到 UI 层。
- 不修改数据库表结构：创建目标复用 goals 表现有字段（priority/deadline 已由
  PlannerAgent 冲突分析迁移引入），关联既有目标通过添加任务实现。
"""
import re
from typing import Any, Dict, List, Optional

# 通过模块引用而非 from-import，便于单测中 monkeypatch chat_sync。
# P7 批次 10-①：LLM 调用已改走 core.ai_filter.chat_sync_with_retry_budget（首档预算
# 抬到推理地板 1500），该 helper 调用时读的仍是 business.model_client.chat_sync
# 这一模块属性，故单测里替换 model_client.chat_sync 依然拦截得到。
from business import model_client

# ---------- JSON 提取（统一实现，2026-09-12 收敛） ----------
# 本模块刻意保留 extract_json 这一对外名称：agents/reflection_agent.py 仍以
# `from agents.intent_agent import build_text_from_history, extract_json` 导入使用。
# 实现已收敛到 core/json_utils，同一段文本含多个 JSON 对象时不再因贪婪匹配而失败。
from core.json_utils import extract_json_object as extract_json

# 预算与重试统一实现（P7 批次 10-①）
from core.ai_filter import chat_sync_with_retry_budget


def _safe_int(value: Any, default: int, lo: int, hi: int) -> int:
    """容错转换整数并限制范围"""
    try:
        return max(lo, min(hi, int(value)))
    except (ValueError, TypeError):
        return default


# ---------- 意图结构常量 ----------

# 合法的建议操作类型
VALID_SUGGESTION_TYPES = {"create_new", "associate_existing", "adjust_plan", "none"}

# 建议操作类型的中文标签（用于 Markdown 展示）
SUGGESTION_LABELS = {
    "create_new": "🆕 创建新目标",
    "associate_existing": "🔗 关联已有目标",
    "adjust_plan": "🗺️ 调整现有计划",
    "none": "🤔 无明确意图",
}


def build_text_from_history(history: Any, limit: int = 8,
                            max_chars_per_msg: int = 300) -> str:
    """把会话历史压平为供意图分析的纯文本

    Args:
        history: Gradio Chatbot 的历史值，支持两种格式：
                 - [{"role": "user"/"assistant", "content": "..."}]（本项目当前格式）
                 - [("user 文本", "bot 文本"), ...]（旧式元组格式，容错兼容）
        limit: 最多取最近多少条用户消息
        max_chars_per_msg: 单条消息截断长度，避免超长对话撑爆提示词

    Returns:
        以 "- " 开头的多行文本；无可用内容时返回空字符串
    """
    texts: List[str] = []
    if not history:
        return ""
    for msg in history[-(limit * 2):]:  # 用户/助手交替出现，多取一倍再过滤
        content = ""
        if isinstance(msg, dict):
            if msg.get("role") != "user":
                continue
            content = str(msg.get("content") or "")
        elif isinstance(msg, (list, tuple)) and msg and isinstance(msg[0], str):
            # 旧式元组格式：第一个元素是用户发言
            content = msg[0]
        else:
            continue
        content = content.strip()
        if content:
            texts.append(content[:max_chars_per_msg])
        if len(texts) >= limit:
            break
    return "\n".join(f"- {t}" for t in texts)


class IntentAgent:
    """意图识别代理：从对话中提取潜在目标建议，确认后写入目标系统"""

    def __init__(self, goal_manager=None):
        # 复用全局 GoalManager；未注入时延迟导入默认实例（模式同 PlannerAgent）
        if goal_manager is not None:
            self.goal_manager = goal_manager
        else:
            from core.goal_manager import GoalManager
            self.goal_manager = GoalManager()

    # ========== 功能一：意图识别（只分析，不写库） ==========

    def analyze_intent(self, user_message: str,
                       max_context_chars: int = 6000) -> Dict[str, Any]:
        """从文本中识别潜在的目标/计划意图

        Args:
            user_message: 参与分析的文本（最近对话拼接结果或当前输入）
            max_context_chars: 送入模型的最大文本长度

        Returns:
            {"ok": bool, "message": str, "intents": list[dict], "display": markdown}
            intents 中每条包含：title / description / priority(1-3) /
            estimated_weeks / suggestion_type / target_title / reason
        """
        text = (user_message or "").strip()
        if not text:
            return {"ok": False, "intents": [],
                    "message": "⚠️ 没有可分析的内容：请先在聊天区对话，或在输入框中粘贴要分析的文本",
                    "display": ""}
        text = text[:max_context_chars]

        # 附带当前进行中的目标清单，帮助模型判断是"新建"还是"关联"
        goals_text = "（暂无进行中的目标）"
        try:
            active_goals = self.goal_manager.list_goals(status="active")[:10]
            if active_goals:
                goals_text = "\n".join(
                    f"- [{g.goal_id}] {g.title}" for g in active_goals
                )
        except Exception:
            pass  # 目标列表读取失败不影响分析，仅缺少参考信息

        prompt = f"""你是一位学习规划顾问。请从下面的对话内容中，提取用户表达的潜在学习/工作目标或计划意图。

【用户当前进行中的目标】（用于判断应新建还是关联）
{goals_text}

【对话内容】
{text}

要求：
1. 只提取用户明确想学、想做、想达成的目标意图；闲聊、提问类内容不要硬凑；
2. 可以识别出 0 到 3 条意图；
3. suggestion_type 只能是以下四种之一：
   - create_new：全新目标，与现有目标都不重合
   - associate_existing：与某个现有目标相关，在 target_title 里给出最匹配的现有目标标题（找不到就留空）
   - adjust_plan：与现有目标相关，但更像是调整计划安排而非新建目标
   - none：没有明确意图
4. priority 为 1-3 整数（1 最紧急）；estimated_weeks 为预计周期周数整数；
5. 严格只输出 JSON，格式如下：
{{"has_intent": true, "intents":[{{"title":"目标标题","description":"目标描述","priority":2,"estimated_weeks":4,"suggestion_type":"create_new","target_title":null,"reason":"识别依据简述"}}]}}
没有明确意图时输出 {{"has_intent": false, "intents": []}}。不要输出任何其他文字。"""

        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            return {"ok": False, "intents": [],
                    "message": f"❌ 模型调用失败：{e}", "display": ""}

        data = extract_json(response)
        if data is None:
            return {"ok": False, "intents": [],
                    "message": "⚠️ 无法从模型回复中解析出有效意图，请重试",
                    "display": ""}

        intents = self._validate_intents(data)
        if not intents:
            return {"ok": True, "intents": [],
                    "message": "✅ 分析完成：未识别到明确的目标意图",
                    "display": "## 🧭 意图识别结果\n\n未识别到明确的目标或计划意图。\n\n> 💡 可以继续聊天后再试，或在输入框直接粘贴你的想法。"}

        display = self.format_intents_markdown(intents)
        return {"ok": True,
                "message": f"✅ 识别到 {len(intents)} 条目标意图（确认后点击应用）",
                "intents": intents, "display": display}

    def _validate_intents(self, data: Optional[Dict]) -> List[Dict]:
        """校验并清洗 LLM 返回的意图列表：非法项过滤、数值钳制、按标题去重"""
        result: List[Dict] = []
        seen_titles = set()
        if not data or not isinstance(data.get("intents"), list):
            return result
        for item in data["intents"]:
            if not isinstance(item, dict):
                continue
            stype = str(item.get("suggestion_type", "")).strip()
            if stype not in VALID_SUGGESTION_TYPES:
                continue  # 非法操作类型 → 过滤
            title = str(item.get("title", "")).strip()
            key = re.sub(r"\s+", "", title.lower())
            if stype == "none":
                # none 类型仅作占位提示，不参与去重，也不要求标题
                title = ""
            else:
                if not title or key in seen_titles:
                    continue  # 无标题或重复标题 → 过滤
                seen_titles.add(key)
            result.append({
                "title": title,
                "description": str(item.get("description", "")).strip(),
                "priority": _safe_int(item.get("priority"), default=2, lo=1, hi=3),
                "estimated_weeks": _safe_int(item.get("estimated_weeks"),
                                             default=0, lo=0, hi=52),
                "suggestion_type": stype,
                "target_title": str(item.get("target_title") or "").strip(),
                "reason": str(item.get("reason", "")).strip(),
            })
        return result

    def format_intents_markdown(self, intents: List[Dict]) -> str:
        """将识别到的意图建议格式化为 Markdown，用于 UI 展示"""
        lines = ["## 🧭 意图识别结果", ""]
        for i, it in enumerate(intents, start=1):
            label = SUGGESTION_LABELS.get(it["suggestion_type"], it["suggestion_type"])
            lines.append(f"### 建议 {i}：{label}")
            if it["title"]:
                lines.append(f"- **目标标题**：{it['title']}")
            if it["description"]:
                lines.append(f"- **描述**：{it['description']}")
            pri = {1: "🟢 高", 2: "🟡 中", 3: "🔴 低"}.get(it["priority"], it["priority"])
            lines.append(f"- **优先级**：{pri}")
            weeks = f"约 {it['estimated_weeks']} 周" if it["estimated_weeks"] else "未指定"
            lines.append(f"- **预计周期**：{weeks}")
            if it["suggestion_type"] == "associate_existing":
                target = it["target_title"] or "（未匹配到，应用时会提示手动选择）"
                lines.append(f"- **拟关联目标**：{target}")
            if it["reason"]:
                lines.append(f"- **识别依据**：{it['reason']}")
            lines.append("")
        lines.append("> 💡 确认后请点击「✅ 应用建议」：「创建新目标」会写入目标系统，"
                     "「关联已有目标」会在匹配的进行中目标下添加一条 AI 任务，其余类型仅作提示不修改数据。")
        return "\n".join(lines)

    # ========== 功能二：应用建议（写库操作） ==========

    def apply_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """应用单条意图建议（用户在 UI 确认后调用）

        根据 suggestion_type 执行：
        - create_new：调用 goal_manager.create_goal 创建新目标（含优先级）；
          已存在完全同名（精确匹配）的进行中目标时不重复创建
        - associate_existing：模糊匹配进行中目标，唯一候选时 add_task 添加 AI 任务；
          多个候选时列出候选标题提示用户手动选择（不写库）；匹配不到则提示手动关联
        - adjust_plan：不修改任何数据，提示使用 PlannerAgent 调整计划
        - none：提示未识别到明确意图

        Returns:
            {"ok": bool, "action": str, "goal_id": str|None, "message": str}
        """
        if not isinstance(intent, dict):
            return {"ok": False, "action": "unknown", "goal_id": None,
                    "message": "⚠️ 无效的建议数据"}
        action = intent.get("suggestion_type")

        # ---- 创建新目标 ----
        if action == "create_new":
            title = str(intent.get("title", "")).strip()
            if not title:
                return {"ok": False, "action": action, "goal_id": None,
                        "message": "⚠️ 该建议缺少目标标题，无法创建"}
            # 幂等保护：已存在完全同名进行中目标时不重复创建。
            # 使用精确标题匹配（而非模糊匹配），避免「基础日语」与「日语进阶」
            # 这类包含关系的目标被误判为重复
            exists = self.goal_manager.find_goal_by_exact_title(title, status="active")
            if exists:
                gid = exists.goal_id
                return {"ok": False, "action": action, "goal_id": gid,
                        "message": f"⚠️ 已存在同名进行中目标「{exists.title}」"
                                   f"（ID: {gid}），无需重复创建"}
            goal = self.goal_manager.create_goal(
                title,
                str(intent.get("description", "")).strip(),
                priority=_safe_int(intent.get("priority"), default=2, lo=1, hi=3),
                deadline=None,
            )
            message = (f"✅ 已创建目标「{goal.title}」"
                       f"（ID: {goal.goal_id}，优先级 {goal.priority}）")
            weeks = intent.get("estimated_weeks")
            if weeks:
                message += f"，预计周期约 {weeks} 周（可用 Planner 规划助手生成分阶段计划）"
            return {"ok": True, "action": action, "goal_id": goal.goal_id,
                    "message": message}

        # ---- 关联已有目标：在匹配到的目标下添加一条任务 ----
        if action == "associate_existing":
            keyword = str(intent.get("target_title") or intent.get("title") or "").strip()
            candidates = (self.goal_manager.find_goals_by_title(keyword, status="active")
                          if keyword else [])
            if not candidates:
                return {"ok": False, "action": action, "goal_id": None,
                        "message": f"⚠️ 未找到与「{keyword or '该建议'}」匹配的进行中目标，"
                                   "请在上方的目标下拉框中选择后手动关联"}
            if len(candidates) > 1:
                # 匹配精度改进：多个候选时不自动猜第一个（可能关联错目标），
                # 列出候选标题（最多 3 个），提示用户在目标下拉框手动选择后手动关联
                shown = "、".join(f"「{g.title}」" for g in candidates[:3])
                more = f" 等 {len(candidates)} 个候选" if len(candidates) > 3 else ""
                return {"ok": False, "action": action, "goal_id": None,
                        "message": f"⚠️ 「{keyword}」匹配到多个进行中目标：{shown}{more}。"
                                   "为避免关联错误，请在上方的目标下拉框中手动选择目标后"
                                   "手动添加任务完成关联"}
            target = candidates[0]  # 仅一个候选时照常自动关联
            task_title = str(intent.get("title", "")).strip() or "来自意图识别的任务"
            desc_parts = []
            if intent.get("description"):
                desc_parts.append(str(intent["description"]).strip())
            if intent.get("reason"):
                desc_parts.append(f"来源：意图识别（{str(intent['reason']).strip()}）")
            self.goal_manager.add_task(
                target.goal_id, task_title, "；".join(desc_parts),
                priority=_safe_int(intent.get("priority"), default=2, lo=1, hi=3),
                source="ai_generated",
            )
            return {"ok": True, "action": action, "goal_id": target.goal_id,
                    "message": f"✅ 已在目标「{target.title}」（ID: {target.goal_id}）下"
                               f"添加任务「{task_title}」"}

        # ---- 调整计划：不直接修改，引导使用 PlannerAgent ----
        if action == "adjust_plan":
            title = str(intent.get("title", "")).strip() or "相关目标"
            return {"ok": False, "action": action, "goal_id": None,
                    "message": f"ℹ️ 「{title}」更适合调整现有计划：请在下方 Planner 规划助手处"
                               "重新生成或调整分阶段计划（本次未修改任何数据）"}

        # ---- 未识别到明确意图 ----
        if action == "none":
            return {"ok": False, "action": action, "goal_id": None,
                    "message": "ℹ️ 未识别到明确的目标意图，已忽略该建议"}

        return {"ok": False, "action": str(action), "goal_id": None,
                "message": f"⚠️ 未知的建议类型：{action}"}

    def apply_intents(self, intents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量应用多条意图建议（UI「✅ 应用建议」按钮使用）

        Returns:
            {"ok": bool, "applied": int, "details": [str]}
        """
        applied, details = 0, []
        for intent in intents or []:
            try:
                result = self.apply_intent(intent)
            except Exception as e:
                details.append(f"❌ 应用建议失败：{e}")
                continue
            if result.get("ok"):
                applied += 1
            if result.get("message"):
                details.append(result["message"])
        return {"ok": applied > 0, "applied": applied, "details": details}


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent）
intent_agent = IntentAgent()
