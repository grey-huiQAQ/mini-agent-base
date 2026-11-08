"""
Supervisor Agent - 任务理解与路由
"""
import sys
from typing import List

# JSON 提取统一实现（2026-09-12 收敛）：原内联的「剥 ```json 代码块 + json.loads」
# 已删除；统一实现额外提供括号栈兜底，模型回复夹带说明文字时也能取到对象。
from core.json_utils import extract_json_object

# 预算与重试统一实现（P7 批次 10-①）：本函数的输出直接交给 extract_json_object，
# 属「输出当 JSON 解析」类，截断即硬失败。原先写死 max_tokens=300，比推理型模型
# 的推理地板（core/ai_filter.DEFAULT_MAX_TOKENS=1500）低一个量级，必然被
# reasoning_content 耗尽——失败还不报错，只是静默回退通用助手。
from core.ai_filter import chat_sync_with_retry_budget, _clamp01
# ^ 阶段一（2026-10-11）多 import 一个 `_clamp01`：设计席的规格点名用它钳置信度。
# 它在 `core/ai_filter` 里本来就在钳同类值（机会打分的 ai_value_score／ai_project_reuse／
# ai_confidence 三格都走它），再造一把本地的尺就是第四份「两处手工保持一致」。
# 名字带下划线是历史（同模块内部先用的），不代表不许跨模块用；agents→core 是上层引下层。

# 能力闭集（P7 第一闭环 Phase 3a，2026-10-06）：只有下面这段旁路观测用它，路由部分
# 一个字没动。agents→core 是上层引下层（checks/check_import_direction.py 的层序允许）。
# 本模块只在**函数里**读它的键集合，不碰数据库；import 它不产生任何建表副作用
# （core/project_state.py 只 import memory.database.get_db，而 get_db 不跑迁移）。
from core.project_state import CAPABILITY_MAP, UNKNOWN_CAPABILITY

AVAILABLE_AGENTS = {
    "通用助手": {
        "description": "通用生活助手，回答日常问题",
        "domains": ["通用"],
        "tools": ["get_current_time", "web_search"],
    },
    "AI学习": {
        "description": "AI/机器学习/深度学习知识教学",
        "domains": ["AI", "机器学习", "深度学习"],
        "tools": ["get_current_time", "web_search", "read_file", "list_files"],
    },
    "系统科学": {
        "description": "系统科学、控制论、复杂系统等",
        "domains": ["系统科学", "控制论", "复杂系统"],
        "tools": ["get_current_time", "web_search", "read_file", "list_files"],
    },
    "英语": {
        "description": "英语学习与辅导",
        "domains": ["英语", "语言学习"],
        "tools": ["get_current_time", "web_search"],
    },
}

# 注意：花括号已转义为 {{ 和 }}
SUPERVISOR_SYSTEM_PROMPT = """你是一个任务路由 Supervisor。分析用户输入，输出 JSON 决策，格式为：
{{
    "task": "学习/搜索/闲聊/其他",
    "domains": ["领域1", "领域2"],
    "difficulty": "初级/中级/高级",
    "agent": ["Agent名称1", "Agent名称2"],
    "reason": "简要理由"
}}

可用的 Agent 有：
{agent_list}

注意：
- 如果问题涉及多个领域，可以同时选择多个 Agent（agent字段为列表）。
- 如果只是简单搜索，task 为 "搜索"，agent 为 ["通用助手"]。
- 如果不明确，默认 agent 为 ["通用助手"]。
- 只输出 JSON，不要任何其他文字。"""

def build_agent_list_text():
    """构建 Agent 列表描述文本"""
    lines = []
    for name, info in AVAILABLE_AGENTS.items():
        lines.append(f"- {name}: {info['description']} (领域: {', '.join(info['domains'])})")
    return "\n".join(lines)

def supervisor_decision(user_message: str) -> dict:
    """
    调用模型分析用户意图，返回路由决策
    """
    system_prompt = SUPERVISOR_SYSTEM_PROMPT.format(
        agent_list=build_agent_list_text()
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        result = chat_sync_with_retry_budget(messages, temperature=0.1)
        # 提取 JSON（统一实现：自动剥代码块 + 括号栈定位，失败返回 None）
        decision = extract_json_object(result)
        if decision is None:
            # 抛异常交由下方 except 统一回退（保持原有「回退通用助手」行为）
            raise ValueError("模型回复中未找到合法 JSON 对象")
        # 补全默认值
        decision.setdefault("agent", ["通用助手"])
        decision.setdefault("domains", ["通用"])
        decision.setdefault("difficulty", "中级")
        decision.setdefault("task", "闲聊")
        return decision
    except Exception as e:
        # GBK 等旧编码控制台无法打印 emoji 等字符：降级为替换字符，
        # 避免回退路径本身崩溃（错误信息里常含 🌐 等符号）
        try:
            print(f"[Supervisor] 决策失败，回退通用助手: {e}")
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            msg = f"[Supervisor] 决策失败，回退通用助手: {e}"
            print(msg.encode(enc, errors="replace").decode(enc))
        return {
            "task": "闲聊",
            "domains": ["通用"],
            "difficulty": "未知",
            "agent": ["通用助手"],
        }


# ============================================================================
# Phase 3a 旁路观测（P7 第一闭环，2026-10-06）
# ============================================================================
# 上面 supervisor_decision 是**每轮对话都要走**的路径（core/runtime.py:145），本段
# 挂在它旁边：函数独立、提示词独立、模型调用独立，**它一个字都没改**。
#
# 为什么「立函数不接线」（用户 2026-10-06 定案，二选一里选的那一个）：
#   拆分文档原文写的是「加一条输出」，字面读法是把观测并进 supervisor_decision 的
#   返回值。但那样每轮对话会多付一次模型调用，而这条旁路的消费者在 Phase 3b 的复盘
#   流程——那是**每周一次**的动作，不是每轮。所以本函数先立起来、由 3b 按需调用；
#   「旁路」因此是真的旁路：摘掉它，路由用例必须逐条仍全绿（tests/test_supervisor.py 负控）。
#
# 3a′（设计席 2026-10-06 补的前置项）：输入从「用户消息」泛化为「观测载荷」
#   原签名只接受一句用户话，但 3b 要喂给它的是 Phase 2 落下来的 State（自动段 k=v
#   事实 ＋ 人工段原话）。泛化只做在**输入这一侧**：函数名、三条契约、闭集词表、
#   已有的旁路用例一条不减。之所以把「用户这句话」这个说法也一并改掉，是因为它会
#   让 3b 以为自己在用错函数，或者反过来——把一份 State 摘要当成一句闲聊去观测。
#
# 三条契约（判据②是这一格存在的主要理由）：
#   ① **绝不抛异常**——它是观测，观测失败不许把调用方一起拖倒；
#   ② 判不了就把「问题」「建议下一步」两格写「无法判断」，**绝不返回编出来的问题**——闭环
#      要拿这条输出立 Decision，一句看起来合理的空话比「没有输出」坏得多（同一形态在本仓库
#      已修过三次：批次 1a 的页眉假话、批次 9 的静默截断、批次 13 的空正文）；
#   ③ 「需要的能力」是**闭集**：只能取 core.project_state.CAPABILITY_MAP 的键，
#      模型自己造的名字一律换成 UNKNOWN_CAPABILITY（不许造能力名，也不许留空）。
#
# 阶段一（2026-10-11）加第四格「置信度」时，这三条一个字没改，改的只是键的个数。
# 新增的问句旁路 `supervisor_ask_questions` 沿用同一套契约，只是第②条换了写法：
# 它判不了时返回**空列表**而不是「无法判断」——那个哨兵是给「一句话」用的，问题列表里
# 塞一个哨兵词会变成一条问不出口的问题，而调用方（复盘的兜底问）本来就在等空列表。
#
# 与路由那份回退的差别是刻意的：路由失败可以退回「通用助手」，因为那是个安全默认值；
# 观测失败**没有安全默认值**——退回一个像样子的「问题」就是编造。

#: 旁路输出的键（3a 原文是三个；阶段一 2026-10-11 加第四个「置信度」，
#: 名字按设计席阶段一规格用中文，与本模块其余键同一语域，不改名便于与文档对得上）
OBSERVE_KEY_PROBLEM = "问题"
OBSERVE_KEY_NEXT_STEP = "建议下一步"
OBSERVE_KEY_CAPABILITY = "需要的能力"
#: 阶段一新增：模型自报的把握，0–1 的小数；**None＝它没报这一格**（与报了 0 是两件事）
OBSERVE_KEY_CONFIDENCE = "置信度"
OBSERVE_KEYS = (OBSERVE_KEY_PROBLEM, OBSERVE_KEY_NEXT_STEP,
                OBSERVE_KEY_CAPABILITY, OBSERVE_KEY_CONFIDENCE)

#: 问句旁路（阶段一）：闸门②请求补充时「该问人哪三句」由它现生成，不复用观测那三格
ASK_KEY_QUESTIONS = "补充问题"
#: 一次最多问几条。设计席阶段一规格给的是「三个具体问题」，多了变问卷。
ASK_QUESTION_LIMIT = 3

#: 判据②的落地词：模型不可用时「问题」与「建议下一步」这两格一律写它，
#: **不写任何看起来合理的说法**。另外两格各有各的「没报」写法、不共用这个词：
#: 能力写 `UNKNOWN_CAPABILITY`（闭集里挑不出来），置信度写 `None`（见 `_unable_observation`）。
UNABLE_TO_JUDGE = "无法判断"

# 注意：花括号已转义为 {{ 和 }}（与上面路由那份同一写法，本串要走 str.format）
SUPERVISOR_OBSERVE_SYSTEM_PROMPT = """你是挂在对话旁边的观测员：不参与选 Agent、不参与回答，只观测下面这份材料，输出一个 JSON 对象，格式为：
{{
    "问题": "这份材料里真正卡住的地方；看不出来就原样写 无法判断",
    "建议下一步": "最小的下一步动作；看不出来就原样写 无法判断",
    "需要的能力": ["从下面清单的键名里挑，最多两个；挑不出来就只填 unknown"],
    "置信度": 0 到 1 之间的小数（你对上面两格的把握：0.2＝很没把握，0.8＝比较有把握；真的说不准就填 null）
}}

可用能力清单（**只能从这些键名里选**，自己造的名字一律作废）：
{capability_list}

材料有两种来源：用户说的一句话，或一份项目状态（State）摘要（自动段是 k=v 事实，人工段是用户原话）。两种都按同一套规则观测，不因为它是摘要就降低标准。

硬规则：
- 不许编造材料里没有写到的困难；说不准就写 无法判断，这四个字是合法答案。
- 置信度是**你自己**的把握，不是材料的多少：材料一堆而你对结论没把握，照样要往低写。
- 不许为了把格式填满就编一个数；填不出来写 null，这比 0.5 诚实。
- 不写鼓励、不写总结、不写"建议保持一致"这类空话。
- 只输出 JSON，不要任何其他文字。"""

#: 问句旁路的提示词。**本串刻意不走 str.format**（它没有要插的清单），所以花括号不转义——
#: 与上面观测那份的写法不同不是疏漏，将来谁要给它加占位符，记得整串改成 {{ }} 再 format。
SUPERVISOR_ASK_SYSTEM_PROMPT = """你现在的任务只有一个：下面这份材料信息不够，请写出**要问人**的问题。

输出一个 JSON 对象，格式为：
{
    "补充问题": ["最多三条，每条是一句能直接问出口的话"]
}

硬规则：
- 问题问的是**人**，不是问你自己。要问那种只有本人才答得出的（这周到底要推进到哪一步、手上有什么材料、什么算有进展），不问数据库里查得出的东西。
- 材料里如果有目标，三条问题都要绕着这个目标问；材料里连目标都没有，就先把"想推进哪件事、手上有什么、给多少时间"问出来。
- 一次最多三条，多了不收。
- 看不出该问什么就返回空数组 []，**不许编一条通用问句凑数**——下面有一份内置兜底问，你说不出不等于它没有，但把兜底说成你生成的同样是编造。
- 不写鼓励、不写解释、不写"你有没有试过"这类正确的废话。
- 只输出 JSON，不要任何其他文字。"""


def build_capability_list_text() -> str:
    """把 CAPABILITY_MAP 摊成提示词里的清单：只给键名与中文标签，不给路径

    刻意不贴 carrier 路径：那是给代码看的，模型照着路径「发明」新能力反而更容易。
    """
    return "\n".join(
        f"- {key}: {info['label']}" for key, info in CAPABILITY_MAP.items()
    )


def _safe_print(message: str) -> None:
    """观测路径的出声：GBK 等旧控制台打不出 emoji 时降级为替换字符，**不让打印本身崩**

    与上面路由那份的回退是同一套理由（错误信息里常含 🌐 等符号）。之所以不直接复用
    那段内联代码：supervisor_decision 要求「一个字不改」，改它去调 helper 就动了路由。
    """
    try:
        print(message)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(message.encode(enc, errors="replace").decode(enc))


def _unable_observation() -> dict:
    """判据②的唯一出口：四键齐全、没有任何一处是编出来的

    「置信度」这一格给 **None 而不是 0.5**：0.5 看起来中性，实际是替模型说了一句它
    没说过的话，而复盘那边把「没报」与「报了 0.5」读成同一件事，阶段一分档就白做了。
    """
    return {
        OBSERVE_KEY_PROBLEM: UNABLE_TO_JUDGE,
        OBSERVE_KEY_NEXT_STEP: UNABLE_TO_JUDGE,
        OBSERVE_KEY_CAPABILITY: [UNKNOWN_CAPABILITY],
        OBSERVE_KEY_CONFIDENCE: None,
    }


def _unable_questions() -> dict:
    """问句旁路的降级出口：空列表 ⇒ 调用方（复盘）走内置兜底问，并如实标是谁写的"""
    return {ASK_KEY_QUESTIONS: []}


def _clean_text_field(value) -> str:
    """取一个非空字符串字段；空／非字符串一律返回空串（由调用方判成"无法判断"）"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _clean_confidence(value):
    """把模型报的置信度钳到 [0,1]；转不动／缺席一律 None（**不补一个 0.5 假装中性**）

    钳制本身复用 `core.ai_filter._clamp01`（设计席阶段一指定的那把尺），这里只补一件事：
    `true`/`false` 也算没报——`_clamp01(True)` 会给出 1.0，那是替模型说「它有十足把握」。
    模型把数写成字符串（"0.7"）是合法的，`_clamp01` 转得动就收。
    """
    if isinstance(value, bool):
        return None
    return _clamp01(value)


def _clean_questions(value) -> List[str]:
    """问句列表的清洗：只收非空字符串、去重、封顶 `ASK_QUESTION_LIMIT` 条

    封顶发生在**这里**而不是提示词里：提示词那句「最多三条」挡不住模型给五条，
    五条一起进 Human View 就成了一页问卷，而这是给人回答的。
    """
    raw = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    picked: List[str] = []
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in picked:
            picked.append(text)
    return picked[:ASK_QUESTION_LIMIT]


def _filter_capabilities(value) -> List[str]:
    """能力闭集：只保留 CAPABILITY_MAP 里真实存在的键，一个都对不上就写 unknown

    这一格是「不许造能力名」唯一的执行点——提示词里的措辞挡不住模型给一个新名字，
    所以这里必须真的把它换掉（tests/test_supervisor.py 用「造名」用例验它有牙齿）。
    """
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        candidates = []
    picked: List[str] = []
    for name in candidates:
        key = name.strip()
        if key in CAPABILITY_MAP and key not in picked:
            picked.append(key)
    return picked or [UNKNOWN_CAPABILITY]


def supervisor_observe(payload: str) -> dict:
    """Phase 3a/3a′：独立第二次模型调用，产 {问题, 建议下一步, 需要的能力, 置信度}

    参数 payload 是**一份观测载荷**，3a′ 起它有两种合法来源（本函数对两者的处理
    完全一致，不分支）：
      ① 用户说的一句话——3a 最初形态，来自对话；
      ② 一份 State 摘要——Phase 2 `core/project_state.py` 落下来的项目状态，
         自动段是 `k=v` 事实、人工段是用户原话。这是 3b 要喂的那一份。

    返回值**只有四个键**（OBSERVE_KEYS；阶段一之前是三个），且任何一条失败路径都返回
    _unable_observation()——不抛异常、不返回编造的问题。
    「置信度」这一格**允许是 None**（模型没报、或报的不是数）：它不是必填的判断，
    而复盘那边把 None 读成「没报」而不是「中等把握」，理由写在
    `core/long_term_project_review._read_confidence`。

    ⚠️ 本函数**不被 supervisor_decision 调用**，也不被 app.py / core/runtime.py /
    ui/controllers/chat_controller.py 调用（2026-10-06 定案：立函数不接线）。
    第一个消费者是 Phase 3b 的复盘流程 `core/long_term_project_review.py`，
    它喂的是来源 ②；3a′ 的泛化就是为这一步做的。
    """
    system_prompt = SUPERVISOR_OBSERVE_SYSTEM_PROMPT.format(
        capability_list=build_capability_list_text()
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload},
    ]
    try:
        raw = chat_sync_with_retry_budget(messages, temperature=0.2)
        parsed = extract_json_object(raw)
        if parsed is None:
            raise ValueError("模型回复中未找到合法 JSON 对象")
        problem = _clean_text_field(parsed.get(OBSERVE_KEY_PROBLEM))
        next_step = _clean_text_field(parsed.get(OBSERVE_KEY_NEXT_STEP))
        if not problem or not next_step:
            # 缺字段**不补默认说法**：补一个「看起来合理」的句子就是判据②要防的编造
            raise ValueError("观测缺少「问题」或「建议下一步」，不代为补全")
        return {
            OBSERVE_KEY_PROBLEM: problem,
            OBSERVE_KEY_NEXT_STEP: next_step,
            OBSERVE_KEY_CAPABILITY: _filter_capabilities(
                parsed.get(OBSERVE_KEY_CAPABILITY)
            ),
            # 缺席不是错误：这一格转不动就写 None，**不影响前两格的有效性**
            OBSERVE_KEY_CONFIDENCE: _clean_confidence(
                parsed.get(OBSERVE_KEY_CONFIDENCE)
            ),
        }
    except Exception as e:
        _safe_print(f"[Supervisor·观测] 本轮未产出观测，返回「{UNABLE_TO_JUDGE}」: {e}")
        return _unable_observation()


def supervisor_ask_questions(payload: str) -> dict:
    """阶段一（2026-10-11）新增的**第二条旁路**：材料不够时，该问人哪三句

    为什么不塞进 `supervisor_observe` 的返回里（设计席拍板 2 之后本席选的实现形状）：
    观测那一旁路的「问题」格语义是「材料里卡住的地方」，是把 3 个要问人的句子塞进去的
    现成口袋，但塞了之后同一格在两个出口里语义不同（一个是诊断、一个是提问），
    而 `core/long_term_project_review` 两边都读它。另外两格也对不上：「建议下一步」
    是给它自己的动作，「需要的能力」是闭集校验。**判不了时该说什么**也不同：
    观测写「无法判断」，这里返回空列表。所以它是独立一次调用、独立提示词。

    三条契约与观测那套同源：
      ① 绝不抛异常——它只是「问句生成」，失败不该把复盘整轮拖倒；
      ② 判不了返回**空列表**，不编一条通用问句凑数。空列表不是缺陷：调用方
         （`core/long_term_project_review._record_request`）看到空就走内置兜底问，
         并在 source／reason 里如实写明那三句是内置的、不是这里生成的；
      ③ 上限 `ASK_QUESTION_LIMIT` 条，且只收非空字符串（`_clean_questions`）。

    ⚠️ 与 `supervisor_observe` 同一状态：**立函数不接线**（3a 的先例）。它的消费者是
    复盘流程的闸门②，由调用方注入（core 不许 import agents），本模块不主动调用它；
    闸门②的「先选目标」那一档**不会走到这里**——该问什么由数据本身写明，复盘那边
    直接用了固定一问，一次模型调用都不花（分档见 `core/long_term_project_review` 的
    模块 docstring「配额口径」）。摘掉本函数，路由与观测的既有用例必须逐条仍全绿。
    """
    messages = [
        {"role": "system", "content": SUPERVISOR_ASK_SYSTEM_PROMPT},
        {"role": "user", "content": payload},
    ]
    try:
        raw = chat_sync_with_retry_budget(messages, temperature=0.3)
        parsed = extract_json_object(raw)
        if parsed is None:
            raise ValueError("模型回复中未找到合法 JSON 对象")
        questions = _clean_questions(parsed.get(ASK_KEY_QUESTIONS))
        # 空列表是**合法答案**（提示词里明说「看不出该问什么就返回空数组」），不补句子：
        # 兜底问由调用方准备，这里的职责是「我说得出就给你，说不出就如实给空」。
        return {ASK_KEY_QUESTIONS: questions}
    except Exception as e:
        _safe_print(f"[Supervisor·问句] 本轮没生成出问题，返回空列表（由调用方兜底）: {e}")
        return _unable_questions()