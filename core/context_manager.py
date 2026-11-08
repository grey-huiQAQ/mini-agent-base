"""
Context Manager - 负责构建注入模型的系统提示词上下文
支持按 detail_level 分层加载记忆，并注入目标相关上下文
"""
from typing import Optional, Dict, Any, List
import json
from memory.profile_manager import ProfileManager
from memory.learning_recorder import (
    LearningRecorder,
    format_mastery_stars,
    format_mastery_ratio,
)
from memory.knowledge_graph import KnowledgeGraph
from memory.experience_store import ExperienceStore
from core.memory_retriever import KeywordMemoryRetriever
from core.conversation_adapter import message_content_to_text
# 闭环教训的筛选口径来自 4a 那一份集合，这里只 import 不复制（同一真源）。
# decision_store 是叶子模块（只 import memory.database），不会与本模块绕成环。
from core.decision_store import EXPERIENCE_ELIGIBLE_SCOPES
# 「今天」只取这一根轴：与首页 `home_today()` 同一个 `date.today()`，core 里不另造
# 第二套日期解析（CHAT_CONTEXT_SPEC.md §2.1）。
from datetime import date
# 「当前焦点」读的是既有那一份 active 标题列表，与首页／候选跳过共用同一个来源。
from core.current_focus import active_focus_titles

# ---- Phase 4b：闭环教训注入的长度上限（拆分文档判据③「长度增量低于硬上限」）----
# 上限靠**构造**保证，不留「超长就整段不注入」那种分支：标题被 LESSON_HEADER_MAX_CHARS
# 钉死、正文被 LESSON_TEXT_MAX_CHARS 钉死，两者加上固定前缀的和恒小于段上限，
# 于是每一段都必然在限内、又必然不为空。有一条形同虚设的分支比没有分支更坏——
# 它被触发时闭环静默变成开环，而没有任何一条用例能看出它被触发过。
LESSON_SECTION_MAX_CHARS = 320   # 整段（标题＋前缀＋正文）的硬上限
LESSON_TEXT_MAX_CHARS = 240      # 那一句 future_effect 的上限
LESSON_HEADER_MAX_CHARS = 60     # 标题行的上限（含决策 id 那串数字）


# ---- 「当前工作现场」的五栏上限（聊天上下文断连最小修复，2026-10-24）----
# 五个上限与上面三个 LESSON_* 同一个做法：靠**构造**保证「读侧被顶到边界时只截断、
# 不整段消失」，不留「超了就不注入」那种分支。超限那一句必须同时给出**真实总数 M**
# 与**本段展示条数 K** 两个数（CHAT_CONTEXT_SPEC.md §2.2「统一超限格式」）：只写其中
# 一个，读的人就分不清「一共只有这几条」与「还有没列出来的」。
WORK_FOCUS_CAP = 3      # 当前焦点
WORK_TODAY_CAP = 5      # 今天排期
WORK_OVERDUE_CAP = 3    # 逾期未完成：只决定展示标题数，总数另写在标题里
WORK_GOAL_CAP = 5       # 活跃目标
WORK_SCOPE_CAP = 1      # 本轮会话范围：最多一条，不参与超限句
# `goal_tasks.status` 的中文标签（§2.2「附状态（进行中/待开始）」）。词表外的值原样带出
# ——读侧不许把不认识的状态说成这两档之一，口径同 `review_controller` 那句「表外状态按
# 原样读」。
WORK_STATUS_LABELS = {"pending": "待开始", "in_progress": "进行中"}
# 读失败那一句与四句空态互斥：读不出来 ≠ 没有（§2.4）。
WORK_READ_FAIL = "工作台数据读不出来（原因见控制台）"
#: 摘要末尾那一句口径：建议必须标成「建议（非安排）」、不写成既定计划、不另拟一份，
#: 证据不足时问一句就够（§4.1 第三行与 §4.2 都落在这句上）。
WORK_ADVICE_NOTE = ("已有的就是上面这些。要另给建议请标「建议（非安排）」，"
                    "别写成既定计划、也别另拟一份计划；证据不足就问一句想先推进哪件事。")


#: 超限那一句：四栏共用。M＝该类真实总数，K＝本段展示的标题条数（两个数分写）。
def _work_overflow(total: int, shown: int) -> str:
    """没超过上限返回空串；超过了返回 §2.2 那句逐字格式的提示"""
    if total <= shown:
        return ""
    return "（共 %d 条，此处列 %d 条，另 %d 条未列）" % (total, shown, total - shown)


def _work_line(label: str, items: List[str], total: int) -> str:
    """拼一栏：`标签：A、B、C` 收尾接超限句；没超限才接句号（`…。` 与「）。」不并存）"""
    return f"{label}{'、'.join(items)}{_work_overflow(total, len(items)) or '。'}"


class ContextManager:
    # 领域映射
    domain_map = {
        "AI学习": "AI",
        "英语": "英语",
        "系统科学": "系统科学",
        "通用助手": "通用",
    }

    def __init__(self, goal_manager=None):
        # 目标管理器
        self.goal_manager = goal_manager
        # 记忆检索器（基于关键词匹配）
        self.retriever = KeywordMemoryRetriever()
        # 预留 token 预算和模型路由接口 (2026-09-12)
        self.token_budget_manager = None
        self.model_router = None

    def set_token_budget_manager(self, manager):
        self.token_budget_manager = manager

    def set_model_router(self, router):
        self.model_router = router

    def build_system_prompt(self, role: str, base_prompt: str, detail_level: str = "high", goal_id: Optional[str] = None, user_input: Optional[str] = None) -> str:
        """根据角色、详细程度和目标构建完整系统提示词

        新增 user_input 参数：当提供用户输入时，动态检索最相关的记忆，
        替代固定规则的全量注入，减少无关记忆、降低 Token 消耗。
        """
        # 入参防御：user_input 必须是字符串。Gradio 6 的 Chatbot 在部分
        # 路径下会产出 parts 列表（[{'text': ..., 'type': 'text'}]），
        # 直接 .strip() 会抛 AttributeError；此处统一收敛为 str。
        if user_input is not None and not isinstance(user_input, str):
            user_input = message_content_to_text(user_input)
        system_prompt = base_prompt

        # 1. 当前工作现场（CHAT_CONTEXT_SPEC.md §2.1「放在最前」）：本段先于档案／检索／
        #    学习／经验／教训任何一段，且**无条件**注入——low 档也要（§2.3 末行，理由＝
        #    「知道在推进什么」是基础）。位置受三处既有 `startswith(base)` 断言约束，
        #    只能在 base 之后、不能之前。
        #    这里同时移除了原「1. 目标相关上下文」的调用（§3.1）：`_build_goal_context`
        #    函数体保留不删，但「会话范围」自本批起只有 `_build_work_context` 一个注入来源，
        #    它那段「### 📌 目标任务」也不再默认混进每轮上下文（风险 A）。
        system_prompt += "\n\n" + self._build_work_context(
            goal_id=goal_id, detail_level=detail_level)

        # 2. 用户档案（根据 detail_level 选择性注入）
        profile_context = self._build_profile_context(detail_level)
        if profile_context:
            system_prompt += "\n\n" + profile_context

        # 3. 动态记忆检索（仅在 medium/high 级别启用，控制 token 消耗）
        #    检索结果优先于固定规则注入，避免冗余
        retrieved = None
        if detail_level in ("medium", "high") and user_input and user_input.strip():
            try:
                retrieved = self.retriever.retrieve(user_input, role=role, limit=3)
            except Exception as e:
                print(f"[ContextManager] 记忆检索失败: {e}")
                retrieved = None

        # 4. 注入学习记录（检索结果优先，固定规则作为补充）
        has_retrieved_learning = False
        has_retrieved_kg = False

        if retrieved:
            # 注入检索到的学习记录
            if retrieved.get("learning_records"):
                lr = retrieved["learning_records"]
                lr_section = self._build_retrieved_learning(lr)
                if lr_section:
                    system_prompt += "\n\n" + lr_section
                    has_retrieved_learning = True

            # 注入检索到的知识节点
            if retrieved.get("knowledge_nodes"):
                kn = retrieved["knowledge_nodes"]
                kn_section = self._build_retrieved_knowledge_nodes(kn)
                if kn_section:
                    system_prompt += "\n\n" + kn_section
                    has_retrieved_kg = True

        # 5. 固定规则注入（与检索结果互补，避免重复）
        if detail_level in ("medium", "high") and not has_retrieved_learning:
            # 仅在未检索到相关学习记录时，才注入最近学习记录
            recent_learning = self._build_recent_learning(role, limit=3)
            if recent_learning:
                system_prompt += "\n\n" + recent_learning

        if detail_level == "high":
            # 薄弱知识点（即使有检索结果也保留，因为这是诊断性信息）
            weak_points = self._build_weak_points(role, limit=3)
            if weak_points:
                system_prompt += "\n\n" + weak_points

            # 旧 `tasks` 表的待办段本批停注入（§3.2）：只移除调用，`_build_tasks` 函数体
            # 与 `tasks` 表都保留不删；不迁移、不清数据。影响面如实记：那批旧待办不再进入
            # 普通聊天上下文，`profile_controller.get_memory_display` 与 `agents/daily_agent`
            # 仍读旧表，属界面与停用模块，本批不动。

            # 知识图谱薄弱节点（仅在未检索到相关节点时补充）
            if not has_retrieved_kg:
                kg_weak = self._build_kg_weak_nodes(role, limit=3)
                if kg_weak:
                    system_prompt += "\n\n" + kg_weak

        # 6. 相关经验注入（medium/high 级别启用，控制 token 消耗；
        #    检索或构建失败时不影响正常对话——_build_experiences_context 内部已兜底）
        if detail_level in ("medium", "high") and user_input and user_input.strip():
            exp_context = self._build_experiences_context(user_input, limit=3)
            if exp_context:
                system_prompt += "\n\n" + exp_context

        # 7. 闭环教训注入（P7 第一闭环 Phase 4b）：把 4a 回流出来的那一句「下次改什么」
        #    放回下一轮的 system prompt。**只追加，不改上面任何一段**。候选由 goal_id
        #    决定、与 user_input 无关，所以这一段既不看不用 detail_level 也不看 user_input
        #    （理由见 _build_goal_lesson_context 那句「两处刻意不做」）。
        lesson_context = self._build_goal_lesson_context(goal_id)
        if lesson_context:
            system_prompt += "\n\n" + lesson_context

        return system_prompt


    def _build_work_context(self, goal_id: Optional[str] = None,
                            today: Optional[str] = None,
                            detail_level: str = "medium") -> str:
        """构建「## 🧭 当前工作现场」：聊天那一侧读得到的工作台摘要（只读）

        这一段的唯一存在理由＝`CHAT_CONTEXT_SPEC.md` §1.1 那个端到端场景：用户问「今天我
        应该做什么」时，模型手上有**真实的**排期／焦点／逾期／活跃目标，不必让用户把工作
        台重新粘贴一遍（§0 最后一行那条真实失败会话就是这个缺口）。

        四条口径都写死在 spec 里，本函数只照抄：

        - **只读**：只用 `goal_manager` 既有的四条查询与 `active_focus_titles()`，不新增
          SQL、一格不写（判据 A.7 用逐表整行指纹对拍钉这一句）。「今天」「逾期」两栏直接
          复用第一批下沉到 core 的那两把尺（`DESIGN_ANCHORS.md` §一.14），这里不第三写
          一遍筛选口径；
        - **五栏与上限**：焦点／今天排期／逾期未完成／活跃目标／本轮会话范围，逐栏见 §2.2
          那张表；四栏共用一句统一超限格式（`_work_overflow`），逾期那一栏的总数与展示数
          分开写；
        - **读失败与空态必须分开**：`goal_manager` 为 None、或任何一条查询抛 ⇒ 整段只写
          那一句失败提示，**不许**回落成「今天没有排期。」那类空态（§2.4）；
        - **档位**：low 只送「当前焦点＋今天排期」两栏，medium/high 送全部五栏（§2.3）。
          「本轮会话范围」那一行**只在本函数里输出**——`_build_goal_context` 本批已停调用
          （§3.1），不存在第二处注入。

        Args:
            goal_id: 本轮会话绑定的目标 id；为空（或该 id 已查不到）⇒ 不输出会话范围那行。
            today: 可注入的「今天」（YYYY-MM-DD），默认取 `date.today().isoformat()`。
                   测试靠这个参数钉住日期，不造第二套日期解析（§2.1、§6.5）。
            detail_level: 决定两栏还是五栏，取值与 `build_system_prompt` 一致。
        Returns:
            以「## 🧭 当前工作现场」开头的一段文本（读失败与全空态也各自成段）。
        """
        header = "## 🧭 当前工作现场"
        if self.goal_manager is None:
            # 构造期就没拿到 goal_manager：这不是「今天没排期」，如实说读不出来。
            return f"{header}\n{WORK_READ_FAIL}"
        target = str(today or "").strip() or date.today().isoformat()
        try:
            focus_titles = [str(t or "").strip() for t in active_focus_titles()
                            if str(t or "").strip()]
            planned_rows = self.goal_manager.get_tasks_planned_for(target)
            overdue_rows = self.goal_manager.get_overdue_tasks(target)
            active_goals = self.goal_manager.list_goals(status="active")
            scope_goal = self.goal_manager.get_goal(goal_id) if goal_id else None
        except Exception as e:
            # 原因打到控制台，段里只留那一句：摘要不许把自己读不到东西说成「没有」。
            print(f"[ContextManager] 工作台摘要读取失败: {e}")
            return f"{header}\n{WORK_READ_FAIL}"

        lines = [header]

        # 栏 1 当前焦点（≤3）
        if focus_titles:
            lines.append(_work_line("当前焦点：", focus_titles[:WORK_FOCUS_CAP],
                                    len(focus_titles)))
        else:
            lines.append("（未设置当前焦点）")

        # 栏 2 今天排期（≤5，标题附状态）
        today_items = []
        for row in planned_rows[:WORK_TODAY_CAP]:
            title = str(row.get("title") or "").strip()
            status = str(row.get("status") or "").strip()
            label = WORK_STATUS_LABELS.get(status, status)
            today_items.append(f"{title}（{label}）" if label else title)
        if planned_rows:
            lines.append(_work_line("今天排期：", today_items, len(planned_rows)))
        else:
            lines.append("今天没有排期。")

        # low 档到此为止：不含会话范围、不含逾期、不含活跃目标（§2.3）
        if detail_level not in ("medium", "high"):
            lines.append(WORK_ADVICE_NOTE)
            return "\n".join(lines)

        # 栏 3 逾期未完成：先按 task.id 去今日（今日优先），再按 planned_date 降序取最近
        # ≤3 条；标题里带**原始** planned_date——把逾期直接算进今天是 §4.2 明令不许的。
        planned_ids = {str(row.get("id")) for row in planned_rows}
        overdue = [row for row in overdue_rows
                   if str(row.get("id")) not in planned_ids]
        overdue.sort(key=lambda r: (str(r.get("planned_date") or ""),
                                    -int(r.get("id") or 0)), reverse=True)
        overdue_items = []
        for row in overdue[:WORK_OVERDUE_CAP]:
            title = str(row.get("title") or "").strip()
            planned = str(row.get("planned_date") or "").strip()
            overdue_items.append(f"{title}（{planned}）" if planned else title)
        if overdue:
            lines.append(_work_line(f"逾期未完成 {len(overdue)} 条：",
                                    overdue_items, len(overdue)))
        else:
            lines.append("（没有逾期未完成）")

        # 栏 4 活跃目标（≤5）
        goal_titles = [str(getattr(g, "title", "") or "").strip() for g in active_goals]
        goal_titles = [t for t in goal_titles if t]
        if goal_titles:
            lines.append(_work_line("活跃目标：", goal_titles[:WORK_GOAL_CAP],
                                    len(goal_titles)))
        else:
            lines.append("（没有进行中的目标）")

        # 栏 5 本轮会话范围（≤1，仅当传入 goal_id 且查得到）：那一句「不是唯一背景」
        # 是风险 A 的刹车，防这一格被模型读成整场对话的视野。
        if scope_goal is not None:
            lines.append("本轮会话范围：「%s」（会话范围标签，不是唯一背景）"
                         % str(getattr(scope_goal, "title", "") or "").strip())
        lines.append(WORK_ADVICE_NOTE)
        return "\n".join(lines)

    def _build_goal_context(self, goal_id: str) -> str:
        """构建目标相关上下文"""
        if not self.goal_manager:
            return ""
        goal = self.goal_manager.get_goal(goal_id)
        if not goal:
            return ""
        lines = [f"## 🎯 当前目标：{goal.title}"]
        if goal.description:
            lines.append(f"目标描述：{goal.description}")
        # 添加进行中的目标任务
        tasks = self.goal_manager.get_tasks(goal_id, status="pending")
        if tasks:
            lines.append("### 📌 目标任务")
            for t in tasks[:5]:
                lines.append(f"- {t['title']} (优先级 {t['priority']})")
        return "\n".join(lines)

    def _build_profile_context(self, detail_level: str) -> str:
        """构建用户档案片段"""
        profile = ProfileManager.get_all()
        if not profile:
            return ""

        if detail_level == "low":
            # 只保留长期目标（str() 包裹防御非字符串档案值）
            if "long_goal" in profile and str(profile["long_goal"] or "").strip():
                return f"## 👤 用户长期目标\n- {profile['long_goal']}"
            return ""
        else:
            # medium/high 包含全部关键字段
            lines = ["## 👤 用户档案"]
            key_labels = {
                "english_level": "英语水平",
                "ai_basis": "AI基础",
                "daily_time": "每天可学习时间",
                "prefer_style": "偏好讲解方式",
                "long_goal": "长期目标",
            }
            for key, label in key_labels.items():
                if key in profile and str(profile[key] or "").strip():
                    lines.append(f"- {label}：{profile[key]}")
            return "\n".join(lines)

    def _build_recent_learning(self, role: str, limit: int = 3) -> str:
        """构建最近学习记录"""
        subject = self.domain_map.get(role, "通用")
        records = LearningRecorder.get_recent_records(subject, limit=limit)
        if not records:
            return ""
        lines = ["## 📚 最近学习记录"]
        for r in records:
            # 未评一律渲染成「未评」（P7 批次 12-①）：此前是 `"⭐" * r["mastery_score"]`，
            # 值为 NULL 时直接 TypeError——而这是每次注入上下文都会走的路。
            stars = format_mastery_stars(r["mastery_score"])
            lines.append(f"- [{r['subject']}] {r['topic']} {stars}")
        return "\n".join(lines)

    def _build_weak_points(self, role: str, limit: int = 3) -> str:
        """构建薄弱知识点"""
        subject = self.domain_map.get(role, "通用")
        weak_points = LearningRecorder.get_weak_points(subject, limit=limit)
        if not weak_points:
            return ""
        lines = ["## ⚠️ 需要加强的知识点"]
        for w in weak_points:
            lines.append(f"- {w['topic']}（掌握度: {format_mastery_ratio(w['mastery_score'])}）")
            if w.get("weak_points"):
                try:
                    points = json.loads(w["weak_points"])
                    for p in points:
                        lines.append(f"  - {p}")
                except (json.JSONDecodeError, TypeError):
                    pass
        return "\n".join(lines)

    def _build_tasks(self, role: str, limit: int = 3) -> str:
        """构建待办任务"""
        subject = self.domain_map.get(role, "通用")
        tasks = LearningRecorder.get_pending_tasks(subject, limit=limit)
        if not tasks:
            return ""
        lines = ["## 📋 待完成任务"]
        for t in tasks:
            icon = {1: "🟢", 2: "🟡", 3: "🔴"}.get(t["priority"], "⚪")
            due = f"（截止: {t['due_date']}）" if t.get("due_date") else ""
            lines.append(f"- {icon} {t['title']} {due}")
        return "\n".join(lines)

    def _build_kg_weak_nodes(self, role: str, limit: int = 3) -> str:
        """构建知识图谱薄弱节点"""
        domain = self.domain_map.get(role, "通用")
        weak_nodes = KnowledgeGraph.get_weak_nodes(domain, limit=limit)
        if not weak_nodes:
            return ""
        lines = ["## 🧠 需要加强的知识节点"]
        for n in weak_nodes:
            lines.append(f"- {n['name']}（掌握度：{'⭐'*n['mastery_level']}）")
        return "\n".join(lines)

    def _build_retrieved_learning(self, records: list) -> str:
        """构建检索到的相关学习记录（标记为动态检索结果）"""
        if not records:
            return ""
        lines = ["## 📚 相关学习记忆（动态检索）"]
        for r in records:
            # `.get(..., 0)` 的默认值救不了"键在但值为 NULL"（P7 批次 12-①）：
            # 改为把值交给统一渲染，None 与 0 都渲染成「未评」。
            stars = format_mastery_stars(r.get("mastery_score"))
            topic = r.get("topic", "")
            summary = r.get("summary", "")
            entry = f"- [{r.get('subject', '')}] {topic} {stars}"
            if summary:
                entry += f" — {summary}"
            lines.append(entry)
        return "\n".join(lines)

    def _build_retrieved_knowledge_nodes(self, nodes: list) -> str:
        """构建检索到的相关知识图谱节点（标记为动态检索结果）"""
        if not nodes:
            return ""
        lines = ["## 🧠 相关知识节点（动态检索）"]
        for n in nodes:
            name = n.get("name", "")
            domain = n.get("domain", "")
            mastery = n.get("mastery_level", 0)
            lines.append(f"- [{domain}] {name}（掌握度：{'⭐'*mastery}）")
        return "\n".join(lines)

    def _build_experiences_context(self, user_input: str, limit: int = 3) -> str:
        """根据当前用户输入检索相关经验并构建注入片段（经验沉淀功能）

        每条经验末尾附加当前评分（[评分: N]，结构化标记格式，避免被模型
        误读为自然语言）；评分列不存在（旧库未迁移）时按 0 显示。
        异常安全：检索失败或表不存在时返回空字符串，不影响正常对话。
        """
        try:
            experiences = ExperienceStore.search_experiences(user_input, limit=limit)
        except Exception as e:
            print(f"[ContextManager] 经验检索失败: {e}")
            return ""
        if not experiences:
            return ""
        lines = ["## 💡 相关经验参考（历史沉淀）"]
        for exp in experiences:
            scenario = self._short_text(exp.get("scenario"), 80)
            strategy = self._short_text(exp.get("strategy"), 120)
            score = exp.get("score") or 0
            lines.append(f"- 场景「{scenario}」→ 策略：{strategy} [评分: {score}]")
        lines.append("- 使用建议：若与本轮问题相关，请参考上述已验证有效的做法；评分越高的经验越值得优先参考。")
        return "\n".join(lines)

    def _build_goal_lesson_context(self, goal_id: Optional[str]) -> str:
        """构建「上次复盘的教训」注入片段（P7 第一闭环 Phase 4b）

        闭环的这一头把 4a 提炼出来的那一句教训放回每轮对话：经 `goal_id` 找候选
        （`ExperienceStore.latest_goal_lessons` 的单向链接读法），**最多 1 条**、
        **只注入 `future_effect` 那一列**——决策全文、`scenario`、`effect`、`strategy`
        一个都不带（设计席 2026-10-07 裁决 2 ＋ `DESIGN_ANCHORS.md` §一.9）。

        两处刻意不做：
        - **不加 `detail_level` 闸门**。裁决 2 给的是四环筛选，里面没有这一环；把注入绑到
          某几个档上，等于让「注入必须改变下一轮输出」这条判据只在部分轮次成立，而那正是
          本阶段存在的全部理由。长度成本已经由上面三个上限接住。
        - **不新写句子**。这段的正文全部来自库里那一条 `future_effect`，标题只带它的来路
          （决策 id），让人和模型都能回去查账。

        异常安全：读库失败（旧库还没有 `decision_id` 列、表被删掉）与检索异常都返回空串，
        不影响正常对话——与既有的 `_build_experiences_context` 同一惯例。
        """
        key = str(goal_id or "").strip()
        if not key:
            return ""
        try:
            rows = ExperienceStore.latest_goal_lessons(
                key, EXPERIENCE_ELIGIBLE_SCOPES, limit=1)
        except Exception as e:
            print(f"[ContextManager] 闭环教训检索失败: {e}")
            return ""
        if not rows:
            return ""
        row = rows[0]
        lesson = self._short_text(row.get("future_effect"), LESSON_TEXT_MAX_CHARS)
        if not lesson:
            # SQL 已经滤过 NULL 与空串，走到这一支只剩「TRIM 削不掉的空白」那一种形状
            # （SQLite 的 TRIM 只去空格，制表符与换行去不掉）。空教训不是一句空气的理由，
            # 如实不注入。
            return ""
        header = self._short_text(
            f"## 🧭 上次复盘的教训（决策 {row.get('decision_id')}）",
            LESSON_HEADER_MAX_CHARS)
        body_prefix = "- 下次改什么："
        # 段上限在这里真的接进构造：标题与固定前缀先占额度，教训只往剩下的那部分里放。
        # 用 max(1, room) 而不是「装不下就整段不注入」——少一句是闭环静默变成开环，
        # 削短一句仍然把教训送了出去；两者都必然落在 LESSON_SECTION_MAX_CHARS 以内。
        lesson = self._short_text(lesson, max(1, LESSON_SECTION_MAX_CHARS
                                             - len(header) - 1 - len(body_prefix)))
        return f"{header}\n{body_prefix}{lesson}"

    @staticmethod
    def _short_text(text: Any, max_len: int) -> str:
        """压缩空白并截断文本，超长以省略号结尾"""
        text = " ".join(str(text or "").split())
        return text if len(text) <= max_len else text[:max_len - 1] + "…"
