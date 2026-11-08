"""
Reflection Agent - 经验沉淀代理

职责：
1. 从最近对话中提取可复用的经验：有效策略、用户偏好、成功模式；
2. 以结构化 JSON 建议呈现（场景 / 策略 / 效果 / 适用条件），仅提取不写库；
3. 用户在 UI 确认后，调用 ExperienceStore.add_experience 存入经验库；
   之后 ContextManager 构建上下文时会自动检索相关经验注入提示词。

设计说明：
- 与 planner_agent / intent_agent 保持一致：直接通过
  business.model_client.chat_sync 调用模型（测试时可替换为离线 mock）。
- 所有 LLM 输出均经过 extract_json + 校验器过滤；异常时降级为错误提示，
  不抛出到 UI 层。
- 复用 intent_agent.build_text_from_history 压平会话历史（dict 与旧式元组格式兼容）。
"""
from typing import Any, Dict, List, Optional

# 通过模块引用而非 from-import，便于单测中 monkeypatch chat_sync。
# P7 批次 10-①：LLM 调用已改走 core.ai_filter.chat_sync_with_retry_budget（首档预算
# 抬到推理地板 1500），该 helper 调用时读的仍是 business.model_client.chat_sync。
from business import model_client
from memory.experience_store import ExperienceStore

# 复用既有历史压平工具（agents 内部共享工具函数，无循环依赖）
from agents.intent_agent import build_text_from_history, extract_json

# 预算与重试统一实现（P7 批次 10-①）
from core.ai_filter import chat_sync_with_retry_budget


# 经验各字段的最大长度（截断保护，避免超长文本撑爆提示词与数据库）
_FIELD_MAX_LEN = {
    "scenario": 200,
    "strategy": 400,
    "effect": 200,
    "conditions": 200,
}


def _clip(value: Any, max_len: int) -> str:
    """清洗并截断字符串字段：压缩空白、限制长度"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


class ReflectionAgent:
    """经验沉淀代理：从对话中提取可复用策略，确认后写入经验库"""

    # ========== 功能一：反思提取（只分析，不写库） ==========

    def reflect_on_conversation(self, chat_history: Any,
                                max_context_chars: int = 6000) -> Dict[str, Any]:
        """从最近对话中提取潜在的可复用经验

        Args:
            chat_history: Gradio 会话历史（[{role, content}]）、旧式元组列表，
                          或已压平的纯文本字符串
            max_context_chars: 送入模型的最大文本长度

        Returns:
            {"ok": bool, "message": str, "suggestions": list[dict], "display": markdown}
            suggestions 中每条包含：scenario / strategy / effect / conditions
        """
        if isinstance(chat_history, str):
            text = chat_history.strip()
        else:
            text = build_text_from_history(chat_history, limit=10)
        if not text:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 没有可分析的对话内容：请先在聊天区进行几轮对话再沉淀经验",
                    "display": ""}
        text = text[:max_context_chars]

        prompt = f"""你是一位经验沉淀顾问。请从下面的对话中，提取值得长期保存的可复用经验。

【对话内容】
{text}

要求：
1. 只提取以下类型的经验（0 到 3 条），不要硬凑：
   - 有效策略：某种做法在本次互动中被验证有效（如"先给例子再讲理论，用户理解更快"）
   - 用户偏好：用户明确表达的喜好或习惯（如"喜欢简洁的要点式回答"）
   - 成功模式：可复现的良好互动模式（如"用类比解释抽象概念效果好"）
2. scenario 描述这条经验适用的场景；strategy 描述具体做法；effect 描述观察到的效果；
   conditions 描述适用条件或注意事项（后两项可为空字符串）；
3. 严格只输出 JSON，格式如下：
{{"has_experience": true, "experiences":[{{"scenario":"适用场景","strategy":"具体策略","effect":"观察效果","conditions":"适用条件"}}]}}
没有值得沉淀的经验时输出 {{"has_experience": false, "experiences": []}}。不要输出任何其他文字。"""

        try:
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            return {"ok": False, "suggestions": [],
                    "message": f"❌ 模型调用失败：{e}", "display": ""}

        data = extract_json(response)
        if data is None:
            return {"ok": False, "suggestions": [],
                    "message": "⚠️ 无法从模型回复中解析出有效经验，请重试",
                    "display": ""}

        suggestions = self._validate_suggestions(data)
        if not suggestions:
            return {"ok": True, "suggestions": [],
                    "message": "✅ 反思完成：本次对话暂无值得沉淀的经验",
                    "display": ""}

        display = self.format_suggestions_markdown(suggestions)
        return {"ok": True,
                "message": f"✅ 提取到 {len(suggestions)} 条经验建议（确认后点击保存）",
                "suggestions": suggestions, "display": display}

    def _validate_suggestions(self, data: Optional[Dict]) -> List[Dict]:
        """校验并清洗 LLM 返回的经验列表：非法项过滤、字段截断"""
        result: List[Dict] = []
        if not data or not isinstance(data.get("experiences"), list):
            return result
        for item in data["experiences"]:
            if not isinstance(item, dict):
                continue
            scenario = _clip(item.get("scenario"), _FIELD_MAX_LEN["scenario"])
            strategy = _clip(item.get("strategy"), _FIELD_MAX_LEN["strategy"])
            # scenario / strategy 为必填（对应表 NOT NULL 约束），缺失则丢弃
            if not scenario or not strategy:
                continue
            result.append({
                "scenario": scenario,
                "strategy": strategy,
                "effect": _clip(item.get("effect"), _FIELD_MAX_LEN["effect"]),
                "conditions": _clip(item.get("conditions"), _FIELD_MAX_LEN["conditions"]),
            })
        return result

    def format_suggestions_markdown(self, suggestions: List[Dict]) -> str:
        """将经验建议格式化为 Markdown，用于 UI 展示"""
        lines = ["## 🧠 经验沉淀建议", ""]
        for i, s in enumerate(suggestions, start=1):
            lines.append(f"### 建议 {i}")
            lines.append(f"- **适用场景**：{s['scenario']}")
            lines.append(f"- **策略/偏好**：{s['strategy']}")
            if s.get("effect"):
                lines.append(f"- **观察效果**：{s['effect']}")
            if s.get("conditions"):
                lines.append(f"- **适用条件**：{s['conditions']}")
            lines.append("")
        lines.append("> 💡 确认无误后请点击「✅ 确认保存经验」写入经验库；"
                     "保存后，后续对话中系统会自动检索相关经验作为参考。")
        return "\n".join(lines)

    # ========== 功能二：保存经验（写库操作） ==========

    def store_experience(self, exp: Dict[str, Any],
                         source_session_id: Optional[str] = None) -> int:
        """将单条经验写入经验库，返回新记录 ID

        Args:
            exp: 包含 scenario / strategy / effect / conditions 的字典
            source_session_id: 来源会话 ID（溯源用）
        """
        return ExperienceStore.add_experience(
            scenario=str(exp.get("scenario") or "").strip(),
            strategy=str(exp.get("strategy") or "").strip(),
            effect=str(exp.get("effect") or "").strip(),
            conditions=str(exp.get("conditions") or "").strip(),
            source_session_id=source_session_id,
        )

    def apply_suggestions(self, suggestions: List[Dict[str, Any]],
                          source_session_id: Optional[str] = None) -> Dict[str, Any]:
        """批量保存用户确认的经验建议（UI「✅ 确认保存经验」按钮使用）

        Returns:
            {"ok": bool, "saved": int, "details": [str]}
        """
        saved, details = 0, []
        for exp in suggestions or []:
            try:
                if not isinstance(exp, dict) or not str(exp.get("scenario") or "").strip() \
                        or not str(exp.get("strategy") or "").strip():
                    details.append("⚠️ 已跳过 1 条缺少场景或策略的无效建议")
                    continue
                # 防重复沉淀：完全相同的「场景+策略」已存在则跳过
                dup = ExperienceStore.find_exact(str(exp.get("scenario")),
                                                 str(exp.get("strategy")))
                if dup:
                    details.append(f"⚠️ 已跳过 1 条重复经验（与已有 #{dup['id']} 相同）")
                    continue
                new_id = self.store_experience(exp, source_session_id=source_session_id)
                saved += 1
                details.append(f"#{new_id} 场景「{str(exp.get('scenario')).strip()}」")
            except Exception as e:
                details.append(f"❌ 单条保存失败：{e}")
        return {"ok": saved > 0, "saved": saved, "details": details}


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent / intent_agent）
reflection_agent = ReflectionAgent()
