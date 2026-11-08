# -*- coding: utf-8 -*-
"""
Resource Scout —— AI 深度筛选（v0.4，辅助型自主：评估只是建议，不自动改任何状态）

定位：
- 在相关性粗筛（core/resource_scout.check_relevance）基础上，对少量候选机会
  调用 LLM 做深度评估（资格 / 价值 / 可复用性 / 建议 / 下一步），输出结构化 JSON 落库；
- 候选条件（确定性）：relevance_score >= MIN_RELEVANCE_SCORE 且
  eligibility_hint != EXCLUDED_ELIGIBILITY_HINT 且尚未 AI 筛选过（ai_screened_at 为空）
  的**前 N 条**（默认 N=5，按发现时间倒序）；
- 两个触发口（P7 阶段二 B1b，2026-10-17）：① 资源页「🤖 AI 深度筛选」按钮（v0.4 原路）；
  ② 首页刷新时由 `ui/controllers/home_controller.ensure_opportunities_processed` 自动发起
  同一批工作（limit 默认 3）。**两个口跑的是同一个函数、同一套节流**
  （`ui/controllers/_shared.ai_filter_state`），自动的那一头只**发起**加工，
  既不改任何机会的 status，也不在前台等模型；
- LLM 调用复用 business.model_client.chat_sync（同步、30s 超时），任何单条
  失败 / 超时 / JSON 解析异常都只影响该条（不写库、记入报告、可重试），
  绝不中断整批筛选；
- 评估结果仅是**建议**（UI 展示），绝不自动标记 / 自动忽略任何机会，
  最终决策权始终在用户；无 LLM 结果时字段保持默认值，不影响既有流程。
- **强过滤、弱删除**（P7 阶段二 B2，2026-10-17，口径见 `DESIGN_ANCHORS.md` §四）：
  本模块写出的那一格 `usable_assets` 只用来给首页**排序降权**，**不构成门槛**——
  没有可拿走资源的机会照样进首页、照样在资源页里搜得到、照样一行都不删。
  「评估结果只是建议」这条边界因为多了这一列而**没有**松动：多写的是给人看的一格，
  没碰 status 那台状态机。

设计说明（与 Resource Scout 其余部分保持一致）：
- 数据库统一走 memory.database.get_db()，全部 try/finally conn.close()；
- LLM 延迟导入 business.model_client，避免模块加载期引入网络 / SDK 依赖；
- 所有字段 / 分数做白名单与 0~1 区间钳制，非法输出回退默认值；文本类字段另有一条
  「空值哨兵」（`USABLE_ASSET_NULL_VALUES`）：模型按 Prompt 要求答「无」那一族（含近义写法）
  时存 NULL，不原样落库；`usable_assets` 被答成数组时另有一条顿号拼接分支，见解析那一段；
- 与核心筛选解耦：本模块只读写 opportunities 的加工产出列（`ai_*` 与 B2 的 `usable_assets`），
  不修改 scan_once / check_eligibility 等既有逻辑。
"""

import json
import re
from datetime import datetime

from memory.database import get_db, get_user_profile

from .resource_scout import (
    normalize_profile,
    STUDENT_TYPE_LABELS,
    TYPE_LABELS,
    EligibilityHint,
    OpportunityStatus,
)

# ---------- 常量 ----------

#: AI 资格判断的可取值（与资格过滤同一套语义，默认 unknown）
AI_ELIGIBILITY_VALUES = (EligibilityHint.LIKELY_YES,
                         EligibilityHint.LIKELY_NO,
                         EligibilityHint.UNKNOWN)

#: AI 推荐语的可取文案（白名单，避免模型输出任意长文本）
AI_RECOMMENDATIONS = ("建议关注", "可以试试", "不建议", "无法判断")

#: `usable_assets` 的**空值哨兵**（P7 阶段二 B2，2026-10-17 锁定决定 1 先给前三档；
#: 同日 §五.64 ② 定案把「无」的近义写法一并归 NULL，扩为下面这八档）：
#: 去空白后命中任一项 ⇒ 存 NULL，**不替模型补一句**。Prompt 里要求模型「没有就写『无』」，
#: 所以「无」是模型表达「这条确实没有可直接拿走的资源」的正式答案，不是漏答；把它原样存进
#: 文本列会让首页那行显示成「能直接拿走：无」——一句看起来像清单、其实不是清单的话。
#: ⚠️ 这一族仍然是**逐字**匹配、不是前缀或包含匹配：「无。」要带那个句号才命中，
#: 「暂无可用资源」「不清楚」「N/A」都不在表里、会原样保留（差一个标点就不算同一档）。
#: ⚠️ 「未知／不确定／待定」**刻意不进这一族**（§五.64 ② 定案给的判据）：那三档说的是
#: 「模型不知道」，不是「这条没有」；归进 NULL 等于把一次没答上来洗成一条结论。
#: 八档的内容与顺序、以及「近义归 NULL／不知道三档保留」这两头，都由
#: `tests/test_ai_filter.py::test_usable_assets_sentinel_set_is_verbatim` 逐字钉住。
#: 空串也在这一族里——它与哨兵走同一个出口（存 NULL），但含义不同：空串多半是模型漏答。
#: 两者落库后无法区分，所以首页对 NULL **整行不显示**（见 `memory/database.py` 那列注释）。
USABLE_ASSET_NULL_VALUES = ("", "无", "无可用", "无。", "没有", "暂无",
                            "暂无可用", "无可用资源")

#: 候选的相关性粗筛门槛。**AI 加工队列与首页「发现」卡共用这一个数**：两处各写一份
#: 字面量就是两个阈值，改一处不动另一处时，「首页出现的每一条都在可被加工的门槛内」
#: 这条保证当场失去依据（P7 阶段二 B1b，2026-10-17 把它收成常量的原因；那句保证的
#: 准确范围见 `fetch_home_candidates` 的 docstring——2026-10-17 设计席裁定 ① 改准过）。
#: 数值与 v0.4 起 `_fetch_candidates` 里那个字面量一字未变。
MIN_RELEVANCE_SCORE = 0.3

#: 被排除的资格提示（同上，这一格也只许有一处字面量）
EXCLUDED_ELIGIBILITY_HINT = EligibilityHint.LIKELY_NO

#: 首页「发现」卡取哪一档机会（B1b 定案：只看 NEW）。**这里刻意不看
#: `ai_screened_at`**——那一格只决定「这条还没加工、要不要进加工队列」，
#: 决定不了它该不该留在首页：加工完的机会正是首页要展示的对象，
#: 把已加工的赶出首页等于「一加工就消失」。
HOME_CANDIDATE_STATUS = OpportunityStatus.NEW

#: 候选排序键。两条查询（加工队列 / 首页卡片）用**同一个序**，否则「首页显示的那三条」
#: 与「送去加工的那三条」会错开，上面那条保证也就无从核对。
#: ⚠️ B2（2026-10-17）：首页**卡片**在这个序之上又叠了一层界面层的内存重排
#: （`usable_assets` 非空优先）——那是**显示序**，不改这一条 SQL 序，也不改两条查询的
#: 取回集合。写在这里是为了让下笔的人不必去猜两层谁是真源：门槛与取回看这里，顺序看界面层。
CANDIDATE_ORDER_BY = "discovered_at DESC, id DESC"

#: 单条 AI 评估的 LLM 超时秒数（网络保护；超时视为该条失败，不影响其它条）
DEFAULT_TIMEOUT_SEC = 30.0

#: 单条 AI 评估的 LLM 最大输出 token 数（v0.4.1 修复）：推理型模型
#: （如 DeepSeek-V4-Pro-0813）会把输出预算先用于内部推理，实测 600 会被
#: reasoning_content 耗尽（finish_reason='length'、content 为空），故提到
#: 1500 给推理留足预算；重试轮用 RETRY_MAX_TOKENS（2048）进一步放宽。
DEFAULT_MAX_TOKENS = 1500

#: 重试轮的 LLM 最大输出 token 数（比首轮更宽裕，降低再次截断概率）
RETRY_MAX_TOKENS = 2048

#: 可自动重试的"模型输出问题"标记（命中任一即自动重试一次）：
#: 输出为空 / 被 max_tokens 截断 / content 为空 / JSON 解析失败——
#: 这些与模型输出质量相关，重试通常有效；认证 / 配额 / 网络类错误
#: 不在标记内，不重试（重试无意义且浪费配额 / 增加等待）。
RETRYABLE_ERROR_MARKERS = (
    "LLM 输出为空", "输出不含 JSON 对象", "不是 JSON 对象",
    "截断", "空内容",
)

#: AI 深度筛选 Prompt 模板（要求模型只输出一个 JSON 对象；
#: {{ }} 为 .format 转义花括号，正文不使用其它花括号）
#:
#: `ai_next_step`（P7 阶段二 B1b，2026-10-17）刻意排在 `ai_reason` **后面**而不是列表末尾：
#: 推理型模型会把输出预算先吃掉一截（依据本文件上方 DEFAULT_MAX_TOKENS 那段实测），
#: 真被截断时掉的是**靠后**的键。④「现在能做什么」是给人看的那一句，`ai_confidence`
#: 是个数字——两者里宁可让后者承担被截的风险，也不让首页那格空掉。
#: 截断在实现里仍是硬失败（整条 JSON 解析不出来 ⇒ 该条不落库、下次重试），
#: 这一条只是将来到期时的排序依据，不是「截断没关系」的许可证。
#:
#: `usable_assets`（P7 阶段二 B2，2026-10-17）插在 `ai_next_step` **之后**、数值键**之前**，
#: 依据与上面那句同一个：这一格和 ④ 一样是首页要显示给人看的内容，而截断先吃掉靠后的键。
#: 位置是执行席定的（设计席只说「prompt 增 usable_assets」），文案本身逐字照锁定决定 1；
#: 把这条记进 `PROJECT_STATUS.md` §五.64 等确认，与 B1b 那两条自决项同一处。
AI_FILTER_PROMPT_TEMPLATE = """你是「资源机会 AI 深度筛选助手」。请根据机会信息与用户画像，
评估这条资源机会是否值得用户关注，并**严格只输出一个 JSON 对象**（不要输出
任何其它文字、不要 Markdown 围栏）。JSON 字段说明如下：
{{
  "ai_eligibility": "likely_yes 或 likely_no 或 unknown（该机会对用户的资格判断）",
  "ai_reason": "一句话中文理由，不超过 60 字",
  "ai_next_step": "一句话建议下一步，不超过 60 字",
  "usable_assets": "列出这条机会里能直接拿走/利用的东西（免费额度、API、数据集、开源代码、SDK、教程、社区支持等），用顿号分隔、最多 5 项；没有就写「无」",
  "ai_value_score": 0 到 1 的小数（对该用户的实际价值）,
  "ai_project_reuse": 0 到 1 的小数（内容/算力/代码等可复用到用户项目的程度）,
  "ai_recommendation": "建议关注 或 可以试试 或 不建议 或 无法判断",
  "ai_confidence": 0 到 1 的小数（你对上述判断的把握程度）
}}

机会信息：
- 标题：{title}
- 摘要：{summary}
- 粗分类：{type_label}
- 链接：{url}

用户画像：
- 学生类型：{student_type_label}
- 年龄：{age}
- 地区：{region}
- 学校邮箱：{has_school_email}"""


# ---------- 工具函数 ----------

def _now_str() -> str:
    """当前时间（与 opportunities.discovered_at 同格式）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clamp01(value, default=None):
    """把值钳制到 [0, 1]；无法转换时返回 default（默认 None=存 NULL）"""
    try:
        if value is None:
            return default
        v = float(value)
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return default


def _default_llm_call(messages, timeout_sec=DEFAULT_TIMEOUT_SEC,
                      max_tokens=DEFAULT_MAX_TOKENS) -> str:
    """默认 LLM 调用：business.model_client.chat_sync（延迟导入，30s 超时）

    max_tokens 默认 DEFAULT_MAX_TOKENS（1500）：推理型模型会把输出预算
    用于内部推理，600 会被耗尽导致 content 为空（finish_reason='length'）；
    1500 给推理留足预算，重试轮由调用方传 RETRY_MAX_TOKENS（2048）。
    """
    from business.model_client import chat_sync
    return chat_sync(messages, max_tokens=int(max_tokens),
                     timeout=float(timeout_sec or DEFAULT_TIMEOUT_SEC),
                     temperature=0.2)


def chat_sync_with_retry_budget(messages, temperature=0.3, timeout=None,
                                first_max_tokens=DEFAULT_MAX_TOKENS) -> str:
    """按「两档预算」调用 chat_sync——供**输出会被当作 JSON 解析**的调用点使用

    为什么是两档、且首档不得低于地板（依据本文件上方 DEFAULT_MAX_TOKENS 的实测记录）：
    生产模型是推理型（deepseek-ai/DeepSeek-V4-Pro-0813，依据 agent_resource_log.detail），
    max_tokens 预算会先被内部推理吃掉——实测 600 即被 reasoning_content 耗尽
    （finish_reason='length'、content 为空）。由此得到一条**按用途区分**的判据：
    对「输出当 JSON 解析」的调用，截断是**硬失败**（解析不出来 ⇒ 该功能整块失效，
    且失败往往只表现为一连串静默降级）；对纯文本调用只是"短了一截"。故前者的首档
    预算取 DEFAULT_MAX_TOKENS（1500，项目自己实测出的推理地板），并配一次
    RETRY_MAX_TOKENS（2048）的重试兜底。

    重试口径（与 core/goal_manager.generate_tasks_from_goal 相同）：
    - 首档 first_max_tokens（默认 DEFAULT_MAX_TOKENS）；
    - 仅当异常文本命中 RETRYABLE_ERROR_MARKERS（截断 / 空内容 / 输出为空这类
      "模型输出问题"）时，才用 RETRY_MAX_TOKENS 重试**一次**；
    - 认证 / 配额 / 网络类错误不重试——重试无意义，还会烧额度；
    - 首档预算已 ≥ RETRY_MAX_TOKENS 时不追加第二轮（绝不用更小的预算重试）；
    - 两档都用尽仍未成功 ⇒ 抛出最后一个异常。

    Args:
        messages: OpenAI 格式消息列表
        temperature: 采样温度
        timeout: 超时秒数；None 表示沿用 chat_sync 自身的默认值
        first_max_tokens: 首档预算（默认 DEFAULT_MAX_TOKENS=1500）

    Returns:
        chat_sync 的返回文本（本函数不做任何解析）

    Raises:
        Exception: 原样抛出——本函数只负责"给够预算 + 必要时重试一次"，
            降级语义（回退通用助手 / 返回 None / 本地规则兜底）一律留给调用方
    """
    # 延迟导入：每次调用都取模块属性，测试可用字符串靶子
    # patch("business.model_client.chat_sync", ...) 替换（与 _default_llm_call 同口径）
    from business.model_client import chat_sync

    first = int(first_max_tokens or DEFAULT_MAX_TOKENS)
    budgets = [first]
    if first < RETRY_MAX_TOKENS:
        budgets.append(RETRY_MAX_TOKENS)

    kwargs = {"temperature": temperature}
    if timeout is not None:
        kwargs["timeout"] = float(timeout)

    last_error = None
    for budget in budgets:
        try:
            return chat_sync(messages, max_tokens=budget, **kwargs)
        except Exception as e:
            last_error = e
            if not any(marker in str(e) for marker in RETRYABLE_ERROR_MARKERS):
                raise
    raise last_error


def _parse_llm_json(text: str) -> dict:
    """把 LLM 输出解析为 JSON 对象（容忍 ```json 围栏 / 前后多余文字）

    Raises:
        ValueError: 输出为空 / 不含 JSON 对象 / 不是对象
    """
    if not text or not str(text).strip():
        raise ValueError("LLM 输出为空")
    t = str(text).strip()
    # 去掉 Markdown 代码围栏（```json ... ``` 等）
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("LLM 输出不含 JSON 对象")
    obj = json.loads(t[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("LLM 输出不是 JSON 对象")
    return obj


def _build_messages(opportunity: dict, profile: dict) -> list:
    """构造 LLM 消息（system 约束 + user 模板内容）"""
    student_type = profile.get("student_type") or "high_school"
    content = AI_FILTER_PROMPT_TEMPLATE.format(
        title=(opportunity.get("title") or "")[:200],
        summary=(opportunity.get("summary") or "")[:800],
        type_label=TYPE_LABELS.get(opportunity.get("type"),
                                   opportunity.get("type") or "其他"),
        url=(opportunity.get("source_url") or "")[:200],
        student_type_label=STUDENT_TYPE_LABELS.get(student_type, student_type),
        age=profile.get("age") if profile.get("age") is not None else "未填",
        region=profile.get("region") or "未填",
        has_school_email=("有" if profile.get("has_school_email") else "无"),
    )
    return [
        {"role": "system", "content": "你只输出 JSON 对象，不输出任何其它内容。"},
        {"role": "user", "content": content},
    ]


# ---------- 单条 AI 评估 ----------

def ai_filter_opportunity(opportunity: dict, user_profile: dict,
                          llm_fn=None, timeout_sec=DEFAULT_TIMEOUT_SEC,
                          max_tokens=DEFAULT_MAX_TOKENS) -> dict:
    """对单条机会执行 AI 深度评估，返回规范化字段 dict（永不抛异常）

    Args:
        opportunity: opportunities 表行 dict（需含 title/summary/type/url）
        user_profile: 用户画像 dict（可缺省字段，内部 normalize 兜底）
        llm_fn: 可注入的 LLM 调用函数（messages -> str）；默认走 chat_sync
        timeout_sec: LLM 超时秒数（默认 30）
        max_tokens: 首轮 LLM 最大输出 token 数（默认 DEFAULT_MAX_TOKENS=1500）

    重试机制（v0.4.1）：推理型模型可能把首轮 max_tokens 预算用于内部推理，
    导致输出为空 / 被截断 / JSON 解析失败——这类"模型输出问题"自动用更大
    预算（RETRY_MAX_TOKENS=2048，仅当 max_tokens 已小于该值时）重试**一次**
    （避免无限重试与重复调用）；认证 / 配额 / 网络类错误不重试。重试仍失败
    才返回默认值并记录失败原因。

    Returns:
        dict，含：ok(bool)、ai_eligibility、ai_reason、ai_next_step、usable_assets、
        ai_value_score、ai_project_reuse、ai_recommendation、ai_confidence
        （ok=False 表示调用/解析失败，ai_reason 说明失败原因，ai_* 为默认值，
        调用方不写库即可重试；ai_next_step 在**模型没给这一格**时是 None——
        见下方那句「不替它补」；usable_assets 同口径，另多两样：「无」那一族**八档**空值
        哨兵、以及模型答成数组时的**顿号拼接分支**，都写在 `USABLE_ASSET_NULL_VALUES`
        与解析那一段旁边）
    """
    defaults = {
        "ok": False,
        "ai_eligibility": EligibilityHint.UNKNOWN,
        "ai_reason": "",
        "ai_next_step": None,
        "usable_assets": None,
        "ai_value_score": None,
        "ai_project_reuse": None,
        "ai_recommendation": "",
        "ai_confidence": None,
    }
    profile = normalize_profile(user_profile)
    messages = _build_messages(opportunity, profile)
    # 尝试轮次：首轮 max_tokens；若仍小于重试预算，追加一轮 RETRY_MAX_TOKENS
    attempt_tokens = [max_tokens]
    if int(max_tokens or 0) < RETRY_MAX_TOKENS:
        attempt_tokens.append(RETRY_MAX_TOKENS)
    obj = None
    last_err = None
    for mt in attempt_tokens:
        try:
            if llm_fn is not None:
                raw = llm_fn(messages)  # 注入函数保持 (messages)->str 契约
            else:
                raw = _default_llm_call(messages, timeout_sec=timeout_sec,
                                        max_tokens=mt)
            obj = _parse_llm_json(raw)
            break
        except Exception as e:
            last_err = e
            err_text = str(e)
            # 仅"模型输出问题"自动重试一次；认证/配额/网络等直接失败
            if any(marker in err_text for marker in RETRYABLE_ERROR_MARKERS):
                continue
            break
    if obj is None:
        defaults["ai_reason"] = (
            f"AI 评估失败：{str(last_err or '未知错误')[:120]}")
        return defaults
    # 白名单 / 区间钳制 / 长度限制（非法值回退默认）
    ai_eligibility = obj.get("ai_eligibility")
    if ai_eligibility not in AI_ELIGIBILITY_VALUES:
        ai_eligibility = EligibilityHint.UNKNOWN
    recommendation = str(obj.get("ai_recommendation") or "").strip()
    if recommendation not in AI_RECOMMENDATIONS:
        recommendation = "无法判断" if recommendation else ""
    # ai_next_step（P7 阶段二 B1b，2026-10-17）：**非空才留、空就 None，不替模型补一句**
    # ——判断口径与 ai_reason 同一句话（模型没写就是没写，界面上照实说这一格是空的）。
    # 为什么这一列存 NULL 而 ai_reason 存空串：首页要把「还没加工」与「加工过但模型
    # 没给建议」分开，NULL／'' 在这条新列上正是这两态的载体；ai_reason 那列历史上
    # 就存空串，按 §七 的边界一字不动。
    next_step = str(obj.get("ai_next_step") or "").strip()[:200]
    # usable_assets（P7 阶段二 B2，2026-10-17 锁定决定 1）：去空白后**非空且不在空值哨兵里**
    # 才保留，否则 None（＝落库存 NULL），**不替模型补一句**。口径与上面 `ai_next_step` 同一句：
    # 模型没写就是没写。多出来的只有那一条哨兵——Prompt 明确要求「没有就写『无』」，
    # 「无」是模型给出的**正式答案**，原样存进文本列会变成一句看起来像清单的话（「能直接拿走：无」）。
    # ⚠️ §五.64 ②③ 两处口径差已由设计席 2026-10-17 定案，本批按定案落地，各一支：
    # ① 哨兵仍是逐字比对，但**这一族扩到八档**（「无。」「没有」「暂无」「暂无可用」
    #    「无可用资源」一并归 NULL）；「未知／不确定／待定」不在表里——那是「不知道」，
    #    不是「没有」，集合与判据写在 `USABLE_ASSET_NULL_VALUES` 旁边。
    # ② 模型把这一格答成 **JSON 数组**时不再走 `str()`（那会产出 `"['API', '数据集']"`
    #    这种带引号括号的形状）：逐项去空白、丢掉空项、**最多取 5 项**（Prompt 那句
    #    「最多 5 项」在字符串那档靠模型自觉，数组这档由代码截），再用顿号接成一行，
    #    之后与字符串走**同一个出口**——所以 `["无"]` 这种「数组里只装一个无」也归 NULL。
    raw_assets = obj.get("usable_assets")
    if isinstance(raw_assets, list):
        asset_parts = [p for p in (str(x).strip() for x in raw_assets) if p][:5]
        assets = "、".join(asset_parts)
    else:
        assets = str(raw_assets or "").strip()
    usable_assets = None if assets in USABLE_ASSET_NULL_VALUES else assets[:200]
    return {
        "ok": True,
        "ai_eligibility": ai_eligibility,
        "ai_reason": str(obj.get("ai_reason") or "")[:200],
        "ai_next_step": next_step or None,
        "usable_assets": usable_assets,
        "ai_value_score": _clamp01(obj.get("ai_value_score")),
        "ai_project_reuse": _clamp01(obj.get("ai_project_reuse")),
        "ai_recommendation": recommendation[:20],
        "ai_confidence": _clamp01(obj.get("ai_confidence")),
    }


# ---------- 批量筛选（含候选选取 + 落库） ----------

#: 「还没加工过」那一格的两态写法（NULL 与空串历史上都存在过，两个都算未加工）
UNPROCESSED_CLAUSE = "(ai_screened_at IS NULL OR ai_screened_at = '')"


def _fetch_candidates(limit: int) -> list:
    """取出候选机会：未 AI 筛选过、粗筛分数 ≥ MIN_RELEVANCE_SCORE、资格非
    EXCLUDED_ELIGIBILITY_HINT，按 CANDIDATE_ORDER_BY 取前 limit 条（只返回待筛的，
    已筛自动跳过）

    ⚠️ 这一条查询**不含 status 条件**（P7 阶段二 B1b，2026-10-17 复跑确认）：
     interested／favorited 的历史机会一样进加工队列。首页「发现」卡用的是
    `fetch_home_candidates`，那边多了 `status = NEW`、少了「未加工」——两句的
    关系与为什么要这样分，见那两个函数的 docstring 与设计席 B1b 原文的冲突处置
    （`PROJECT_STATUS.md` §五.63）。本函数的取值行为与改用常量之前一字不变。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM opportunities "
            f"WHERE {UNPROCESSED_CLAUSE} "
            f"  AND relevance_score >= ? "
            f"  AND eligibility_hint != ? "
            f"ORDER BY {CANDIDATE_ORDER_BY} LIMIT ?",
            (MIN_RELEVANCE_SCORE, EXCLUDED_ELIGIBILITY_HINT, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _home_candidate_where(only_unprocessed: bool):
    """拼首页候选的 WHERE（返回 (子句, 参数元组)）

    为什么两族查询要共用这一个拼装点而不是各写一遍 SQL：**两个共享门槛**
    （`relevance_score >= ?` 与 `eligibility_hint != ?`）只要有一处漂了，「首页出现的
    每一条都落在可被加工的门槛内」就静默失效——界面上看不出任何异常，只有队列里才会
    发现某条永远轮不到。排序键同理（共用 `CANDIDATE_ORDER_BY`）。
    两条查询**不共享**的正是这里的另外两格：首页加 `status = NEW`、队列加「未加工」。

    Args:
        only_unprocessed: True 时再并上「还没加工」那一格（加工队列用），
            False 时不并（首页卡片用——已加工过的**更要**留在首页）。
    """
    where = ["status = ?", "relevance_score >= ?", "eligibility_hint != ?"]
    params = [HOME_CANDIDATE_STATUS, MIN_RELEVANCE_SCORE, EXCLUDED_ELIGIBILITY_HINT]
    if only_unprocessed:
        where.append(UNPROCESSED_CLAUSE)
    return " AND ".join(where), tuple(params)


def fetch_home_candidates(limit: int) -> list:
    """首页「发现」卡的真源：status=NEW ∧ 粗筛分数 ≥ 门槛 ∧ 资格非排除档，
    按发现时间倒序取前 limit 条（**含已加工过的**，那正是要展示的内容）

    两条查询**共享**的是两个门槛（分数、资格）与排序键，**不共享**的是各自那一格：
    - 首页多一条 `status = NEW`：首页只报还没处理的新发现；
    - 首页不并「未加工」那一格：加工完的机会一消失，首页就永远展示不出加工后的信息。
    不变量按 2026-10-17 设计席裁定 ① 的准确措辞写在这里：**首页取回的每一条都落在
    「可被加工」的那两个门槛内；其中还没加工的那一子集 ⊆ 加工队列的取回集合。**
    两个符号都是算准的，不是随手写的：
    - 不是「首页 ⊆ 队列」——已加工的那条在首页、却已经离开队列（旧措辞就在这儿说错了，
      设计席裁定把它作废）；
    - 也不是「未加工子集 ＝ 队列取回集」——队列不看 `status`，所以「非 new 且还没加工」
      的行会进队列而不在首页（`tests/test_ai_filter.py` 的对拍夹具里那条 `ignored`
      就是现成的反证），等号只在「未加工的恰好全是 new」那种数据上才成立。
    这条 ⊆ 关系由 `tests/test_ai_filter.py` 逐值对拍钉住（两边同取一个足够大的 limit，
    钉的是两条 WHERE 的关系而不是窗口），不靠读代码的人自己比对 SQL。

    ⚠️ B2（2026-10-17）之后**这一条的返回序不等于首页的显示序**：首页在界面层按
    `usable_assets` 非空优先再做一次内存重排（`ui/controllers/home_controller.py` 的
    `collect_opportunity_cards`），SQL 这一头照旧只按 `CANDIDATE_ORDER_BY`——排序键仍然
    两条查询共用一份，降权发生在**取回之后**、不是门槛。为什么落在界面层而不是这里：
    写进 WHERE 就把「排序降权」变成了「绝对门槛」，而 B2 的边界明令不许（也正因为此，
    首页取回的**窗口**在界面层放大到比卡片数大，见那一段注释）。
    """
    where, params = _home_candidate_where(only_unprocessed=False)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM opportunities WHERE {where} "
            f"ORDER BY {CANDIDATE_ORDER_BY} LIMIT ?",
            params + (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def has_unprocessed_home_candidates() -> bool:
    """首页候选族里有没有还没加工的（自动加工触发前的那一眼）

    为什么要这一眼而不是每次都起线程：模型调用要花配额、单条要等数十秒，而首页是
    `demo.load` 每次都跑的（打开页面就刷）。判断本身**只读、一次 LIMIT 1**。
    返回 False 时什么都不发起；返回 True 时由调用方（界面层）决定怎么起后台线程。

    ⚠️ 这一眼问的是**整族**首页候选（SQL 结尾写死 `LIMIT 1`），既不是「界面那个显示
    窗口取回的条目里有没有」，也不是「每轮送加工的那几条里有没有」。这一点以前由一个
    **没有参与 SQL 的 `limit` 形参**表达——名字暗示按前 N 条问、实现却整个忽略它，
    是 §五.64 ⑤ 登记的口径差；2026-10-17 设计席裁定删掉这个参数（函数返回布尔，
    `LIMIT 1` 够用），删后名字与实情一致，调用方不必再传一个不起作用的数。
    每轮真正送几条去加工由界面层的 `HOME_PROCESS_LIMIT` 决定，与这一眼无关。
    """
    where, params = _home_candidate_where(only_unprocessed=True)
    conn = get_db()
    try:
        row = conn.execute(
            f"SELECT 1 FROM opportunities WHERE {where} "
            f"ORDER BY {CANDIDATE_ORDER_BY} LIMIT 1",
            params,
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _persist_screening(opportunity_id, result: dict) -> None:
    """把一次成功的 AI 评估写入 opportunities 行

    写的是加工产出的那七格 `ai_*` ＋ `ai_screened_at` ＋ B2 新加的 `usable_assets`
    （这一列名不带 `ai_` 前缀，是设计席 B2 锁定决定 1 给的原文列名，按原文落）。
    两格存 NULL 是有含义的（`ai_next_step`：模型没给；`usable_assets`：模型没给／给了
    「无」那一族／数组里只装一个「无」），
    照 `result` 里的 None 原样写，**不在这里替它兜成空串**。
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE opportunities SET "
            " ai_eligibility = ?, ai_reason = ?, ai_next_step = ?, "
            " usable_assets = ?, "
            " ai_value_score = ?, ai_project_reuse = ?, ai_recommendation = ?, "
            " ai_confidence = ?, ai_screened_at = ? "
            "WHERE id = ?",
            (result["ai_eligibility"], result.get("ai_reason") or "",
             result.get("ai_next_step"),
             result.get("usable_assets"),
             result.get("ai_value_score"), result.get("ai_project_reuse"),
             result.get("ai_recommendation") or "",
             result.get("ai_confidence"), _now_str(), int(opportunity_id)),
        )
        conn.commit()
    finally:
        conn.close()


def ai_filter_opportunities(limit: int = 5, profile=None,
                            llm_fn=None, timeout_sec=DEFAULT_TIMEOUT_SEC) -> dict:
    """对"前 N 条"候选机会执行 AI 深度筛选并落库（只写加工产出的列：ai_* ＋ usable_assets；
    不改任何机会的 status）

    两个触发口跑的都是本函数（P7 阶段二 B1b，2026-10-17）：资源页那个手动按钮，
    和首页刷新时自动发起的那一次。"辅助型自主"落在**写什么**上而不是**谁按下**上：
    两个口都只往 ai_* 列里写建议，status 一律不碰，最终决策权仍在用户。

    Args:
        limit: 本次最多评估条数（默认 5，可配置；已筛过的自动跳过）
        profile: 用户画像 dict；None 时从 user_profile 表读取
        llm_fn: 可注入 LLM 函数（测试传 mock；None 用 chat_sync）
        timeout_sec: 单条 LLM 超时秒数

    Returns:
        报告 dict：
            candidates: 本次取到的候选条数（可能小于 limit）
            screened: 成功评估并落库条数
            failed: 调用/解析失败条数（未落库，可重试）
            ids: 成功落库的机会 ID 列表
            errors: 失败原因列表
            summary_text: 一句话汇总（控制台/UI 展示用）
    """
    if profile is None:
        profile = get_user_profile()
    prof = normalize_profile(profile)
    candidates = _fetch_candidates(limit)
    report = {
        "candidates": len(candidates),
        "screened": 0,
        "failed": 0,
        "ids": [],
        "errors": [],
    }
    for row in candidates:
        try:
            result = ai_filter_opportunity(row, prof, llm_fn=llm_fn,
                                           timeout_sec=timeout_sec)
        except Exception as e:  # 双保险：单条任何异常都不中断整批
            result = None
            report["failed"] += 1
            report["errors"].append(f"#{row['id']} 评估异常：{e}")
            continue
        if not result.get("ok"):
            report["failed"] += 1
            reason = (result.get("ai_reason") or "AI 评估失败")[:120]
            report["errors"].append(f"#{row['id']} {reason}")
            continue
        try:
            _persist_screening(row["id"], result)
        except Exception as e:
            report["failed"] += 1
            report["errors"].append(f"#{row['id']} 写入失败：{e}")
            continue
        report["screened"] += 1
        report["ids"].append(row["id"])
    report["summary_text"] = (
        f"AI 深度筛选：候选 {report['candidates']} 条，"
        f"成功 {report['screened']} 条，失败 {report['failed']} 条"
        + (f"（{report['errors'][0][:60]} 等）" if report["errors"] else "")
    )
    return report
