"""
Task Executor - 任务执行自动化代理（v3.7.5 新增）

定位：用户手动触发「▶️ 执行计划」后，自动顺序执行计划中标记为
auto_executable=True 的任务（首批支持「资料搜索」与「笔记整理」两类）：
1. 逐项推进并产出进度事件：execute_plan()          → 生成器，逐步 yield
2. 产生结果即暂停等待审批：approval 事件           → 用户确认/拒绝后才落盘
3. 批准后真正写入磁盘/数据库：save_approval()      → 唯一写入口

设计说明（与 planner_agent / daily_planning_agent / reminder_agent 保持一致）：
- 通过模块引用调用 tools.local_tools.web_search 与 business.model_client.chat_sync，
  测试可通过替换模块属性离线 mock，不需要真实网络与 LLM；
- 辅助型自主：执行器只对低风险的「搜索 / 整理笔记」类任务自动产出**草稿**，
  一切写盘动作都必须经过用户审批（yield approval 后暂停，
  gen.send(True/False) 决定批准或拒绝；用 next() 恢复一律视为拒绝——
  安全默认：未经确认绝不写入）；
- LLM 异常或输出为空时，笔记整理处理器降级为「直接提取任务描述作为草稿」，
  不阻断整个计划的执行；
- 提示音：进程内用标准库 wave/struct 合成一段极短正弦哔声 WAV 并 base64
  内嵌（无外部文件、无新增依赖），由 app.py 经 gr.HTML 注入 <audio autoplay>。

事件协议（execute_plan yield 出的字典）：
- {"type": "progress", "message": str}                  阶段进度
- {"type": "result",   "message": str}                  单个任务的结论
- {"type": "approval", "message": str,
   "approval": PendingApproval, "audio": str}           待审批（生成器在此暂停）
- {"type": "done",     "message": str, "stats": dict}   全部结束

PendingApproval 字段：
- task_id / task_title / goal_id / goal_title：来源任务信息
- type：'search' 或 'note'
- content：待审阅的 Markdown 草稿
- suggested_path：建议保存目录；filename：建议文件名
- pending_id：持久化审批记录 ID（V0.2 新增；yield approval 前已写入
  pending_approvals 表，确认/拒绝后由执行器更新其状态）

任务模板（v3.8 新增）：
- execute_template(template_id, triggered_by) 把模板动作翻译为计划任务后复用
  execute_plan 执行，事件协议完全一致；auto_approve=1 的模板在审批暂停点自动
  批准保存，否则正常暂停等待用户确认；
- 每个模板使用独立虚拟目标目录（data/goals/template_{id}/，见
  core.task_template.template_goal_id），搜索结果与笔记按模板归档，
  互不干扰，也便于后续按模板管理；
- check_scheduled_templates() 由调度线程周期调用：先「抢占」更新
  last_run_at 再执行（防重入），到期模板不会在同一周期内被重复触发；
  手动审批的定时模板执行到首个审批点即停止，审批记录已持久化到
  pending_approvals 表，用户可从「待审批」列表处理（绝不自动写盘）；
- 待审批堆积控制：模板存在未处理的待审批时跳过本次触发（定时路径在
  抢占 last_run_at 之前判断并保持到期状态，**处理完之后不会立即重跑**——
  下一次产出等它自己的触发日；唯一边界是「那次触发发生在本期触发点之前」，
  此时本期触发点仍会照常触发一次，属一次触发消费一期，不是对拍；
  手动路径直接给出跳过提示），避免待审批无限积累；
- 运行记录（P7 批次 14，2026-10-06）：check_scheduled_templates 每次跑完按触发源
  落 0 或 1 行 template_runs（写行条件见 core.task_template.RECORD_ALWAYS_SOURCES：
  boot／cli 必落一行，timer 只在确有执行时落），把「到期几件／成功几件／停在审批点
  几件／堆积跳过几件／抢占失败几件／失败几件／产出几行待审批／连的是哪个库」从一行
  print 变成库里读得出的痕迹；五个去向计数器的不变式写在该表的建表注释里；
  **返回值不因记账而多一个键**。

「资源」格（P7 批次 1a 新增动作类型 scout_digest）：
- 只读本地 opportunities 生成一份筛选报告，**零网络、零 LLM**：这一格存在的
  理由就是「用户不在时替他看一眼本地已经抓到的东西」，不能反过来依赖网络；
- 计划任务里显式带 task_kind 标记（不靠标题关键词猜），execute_plan 优先按它
  分发；搜索/笔记两类仍走既有 classify_task 关键词规则，行为不变；
- 产出顶部带「缺席页眉」（上一份**运行**于哪天、距今几天）。N 只能由**时间差**算
  出来：到期判断是当期式的（core.task_template._is_due 每次按当前周期现算
  target），错过的周期从未被记录，引擎里没有「错过了几次」这个信息。

「复盘」格（P7 第一闭环 Phase 3c-甲 新增动作类型 long_term_review，2026-10-06）：
- 一条任务＝跑一次 `core.long_term_project_review.run_long_term_project_review`，
  输入是一份 Project State（读 `latest_project_state()`，空表时现采一份只含自动段的），
  观测函数用 `agents.supervisor.supervisor_observe`——**这是全仓第一处生产调用点**，
  调用频率由模板的触发周期决定（复盘口径＝每周一次），不是每轮对话；
- **这一格不走审批暂停点**：它的产物是一行 Decision 账目而不是「要写进笔记库的文档」，
  算不算数已由 core 层的四个出口判完，handler 返回 requires_approval=False，
  execute_plan 据此直接播报结果（落账计 saved、没落账计 skipped）；
- 因此它会**打一次模型**：与「资源」格的「零网络零 LLM」正相反。测试里全程 patch
  掉观测函数，不打网络。
"""

import base64
import io
import math
import re
import struct
import wave
from datetime import date, datetime, timezone
from typing import Any, Dict, Generator, List, Optional

# 通过模块引用而非 from-import，便于单测中替换 web_search / chat_sync 离线 mock
from tools import local_tools
from business import model_client
from memory.note_store import (
    NoteStore,
    research_dir_for,
    note_dir_for,
    build_note_filename,
)
from memory.approval_store import ApprovalStore

# 任务模板（一键复用）：模板数据读写与定时到期判断（通过模块引用便于单测 mock）
from core import task_template as template_module

# 本地机会库（「资源」格数据源）：同样经模块引用调用，便于单测替换
from core import resource_scout as scout_module

# 长期项目复盘（P7 第一闭环 Phase 3c-甲，2026-10-06）：流程本体在 core 层，
# State 读写在 core 层，观测函数在 agents 层（core 不许引 agents，所以观测由
# 这一层递进去——3b 把 observe 做成注入参数就是为了此刻不必反过来改 core）。
# 三者都走**模块引用**而不是 from-import：单测里要替换的就是这三个名字。
from core import long_term_project_review as review_module
from core import project_state as state_module
from agents import supervisor as supervisor_module

#: 「资源」格任务类型标记（与模板动作类型同值：一个动作对一类任务，不另立词汇）
TASK_KIND_SCOUT_DIGEST = template_module.ACTION_SCOUT_DIGEST

#: 「复盘」格任务类型标记（同上：与模板动作类型同值）
TASK_KIND_LONG_TERM_REVIEW = template_module.ACTION_LONG_TERM_REVIEW

#: execute_plan 认可的任务类型：显式 task_kind 命中即按它分发，否则回退到
#: classify_task 的标题关键词分类（既有搜索/笔记两类行为因此完全不变）
SUPPORTED_TASK_KINDS = ("search", "note", TASK_KIND_SCOUT_DIGEST,
                        TASK_KIND_LONG_TERM_REVIEW)

#: 「调用方没传这个可选参数」的哨兵（P7 批次 1b）
#: 为什么不能拿 None 当「没传」：execute_template 的 previous_run_at 有**两个合法
#: 语义**——「没传」（手动路径不传，回退到模板当前值）与「传了 None」（定时路径抢占
#: 前的值本就是 None＝首次运行，必须输出「首次运行」）。用 `is not None` 判会把
#: 后者误判成前者，于是首次运行的页眉算出「0 天前」。哨兵是唯一不改变「未传」语义的改法。
_UNSET = object()

# ---------- 控制台日志安全化 ----------
# Windows GBK 控制台无法编码 emoji（如 🏁 ✅），打印用户可见文案（如模板
# 执行结果消息）前先剔除无法编码的字符，避免 UnicodeEncodeError 崩溃
# （与 v3.7.6 的 Supervisor 回退日志 GBK 兼容处理保持一致）。
# 保留范围：CJK 汉字、CJK 标点、全角符号、ASCII 可打印字符。
_LOG_SAFE_RE = re.compile(r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u0020-\u007e]")


def log_safe(text: Any) -> str:
    """把任意文本转为控制台安全日志文本（剔除 emoji 等无法编码的字符）"""
    return _LOG_SAFE_RE.sub("", str(text or ""))


# ---------- 任务类型关键词规则（daily_planning_agent 的本地标记规则也复用本分类器） ----------
# 资料搜索类关键词（命中任意一个即视为搜索任务）
SEARCH_KEYWORDS = ("搜索", "检索", "查找", "调研", "查阅", "资料", "文献",
                   "资讯", "search", "research")
# 笔记整理类关键词
NOTE_KEYWORDS = ("整理", "笔记", "总结", "归纳", "摘要", "梳理", "note", "summar")


def classify_task(text: str) -> Optional[str]:
    """根据任务标题/描述关键词判断任务类型

    Returns:
        'search'（资料搜索）/ 'note'（笔记整理）/ None（暂不支持自动执行）。

    规则：搜索词优先于笔记词——标题同时含两类词时按「先收集资料、
    再整理成文」的自然顺序优先归为搜索任务；空白文本返回 None。
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    for kw in SEARCH_KEYWORDS:
        if kw in lowered:
            return "search"
    for kw in NOTE_KEYWORDS:
        if kw in lowered:
            return "note"
    return None


def guess_auto_executable(title: str, description: str = "") -> bool:
    """本地规则判断任务是否适合自动执行（LLM 未给出标记时的兜底）

    只有能被 classify_task 识别为受支持类型（搜索/笔记）的任务才标记 True，
    其余一律 False（默认保守，符合辅助型自主原则）。
    """
    return classify_task(f"{title or ''} {description or ''}") is not None


def _absence_header(previous_run_at: Any, now: Optional[datetime] = None) -> str:
    """生成「缺席页眉」：上一份**运行**距今几天（纯函数）

    口径（用户 2026-09-18 定）：N 取**时间差**，而不是去问触发判断「错过了几次」
    ——到期判断是**当期式**的（core.task_template._is_due 每次都按当前周期现算
    target），错过的周期从未被记录，引擎里根本没有「错过了几次」这个信息。
    同时给出绝对日期（便于直接核对，与 build_session_bill 的绝对日期口径一致）。
    措辞口径（P7 批次 3 更正）：说的是「运行」而不是「生成」——一次被拒绝的运行
    **什么都没生成**，而 last_run_at 记的正是「那次运行」，两个字不能混用。

    Args:
        previous_run_at: **抢占前**的 last_run_at 字符串；空 / 非法视为首次运行
        now: 基准时间（测试可注入）

    Returns:
        一行 Markdown 引用文案（由调用方放在产出顶部）。
    """
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    previous = template_module._parse_datetime(previous_run_at)
    if previous is None:
        return f"> 🕒 首次运行（本模板此前未运行过）｜本次生成 {stamp}"
    # 时钟回拨 / 手动改过库都可能让差值变成负数，钳到 0 而不是报一个负天数
    days = max(0, (now.date() - previous.date()).days)
    return (f"> 🕒 上一份运行于 {previous.strftime('%Y-%m-%d')}"
            f"（{days} 天前）｜本次生成 {stamp}")


# ---------- 内置提示音合成（纯标准库，无外部音频文件） ----------

def _make_beep_wav_base64(freq_hz: int = 880, duration_s: float = 0.18,
                          sample_rate: int = 8000, volume: float = 0.6) -> str:
    """在内存中合成一段短促正弦哔声并编码为 WAV base64 字符串

    首尾各 10% 线性淡入淡出，避免爆音；参数固定即可满足提示需求。
    """
    total = max(1, int(sample_rate * duration_s))
    fade = max(1, int(total * 0.1))
    frames = bytearray()
    for i in range(total):
        envelope = min(1.0, i / fade, (total - i) / fade)
        value = volume * envelope * math.sin(2 * math.pi * freq_hz * i / sample_rate)
        frames += struct.pack("<h", int(value * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode("ascii")


# 进程内缓存：base64 数据只生成一次
_BEEP_BASE64_CACHE: Optional[str] = None


def beep_audio_html(autoplay: bool = True) -> str:
    """生成插入 UI 的 <audio> 标签（base64 内嵌提示音）

    Returns:
        HTML 字符串；合成失败时返回空串（静默降级，不影响审批流程）。
    """
    global _BEEP_BASE64_CACHE
    if _BEEP_BASE64_CACHE is None:
        try:
            _BEEP_BASE64_CACHE = _make_beep_wav_base64()
        except Exception as e:
            print(f"[TaskExecutor] 提示音合成失败（忽略）: {e}")
            _BEEP_BASE64_CACHE = ""
    if not _BEEP_BASE64_CACHE:
        return ""
    autoplay_attr = " autoplay" if autoplay else ""
    return (f"<audio{autoplay_attr}>"
            f'<source src="data:audio/wav;base64,{_BEEP_BASE64_CACHE}" type="audio/wav">'
            f"</audio>")


class TaskExecutor:
    """任务执行自动化代理：顺序执行可自动任务，结果经用户审批后落盘

    审批持久化（V0.2 新增）：
    - 每次 yield approval 暂停前，先把待审批请求写入 pending_approvals 表
      （status=pending），并把记录 ID 附在审批字典的 pending_id 字段；
    - 用户确认/拒绝后（send(True/False) 或从「待审批」列表处理）更新记录状态
      为 approved/rejected 并记录 resolved_at；
    - 重启后内存生成器丢失，用户仍可从「待审批」列表确认 → 用持久化的
      detail_json 重新执行 save_approval 落盘，或拒绝 → 仅更新状态。
    """

    def __init__(self, note_store: Optional[NoteStore] = None,
                 approval_store=None):
        # 复用全局 NoteStore；测试可注入独立实例（NoteStore 为静态方法集合，
        # 注入主要为语义清晰与未来扩展预留） (2026-09-12)
        self.note_store = note_store or NoteStore()
        # 审批持久化存储（默认 ApprovalStore；测试可注入 mock 验证异常安全）
        self.approval_store = approval_store or ApprovalStore()

    # ========== 内部工具 ==========

    @staticmethod
    def _task_text(task: Dict[str, Any]) -> str:
        """拼接任务的标题与描述用于类型识别"""
        return f"{task.get('task_title') or ''} {task.get('description') or ''}"

    @staticmethod
    def _format_search_result(raw: str, query: str) -> str:
        """把 web_search 的原始文本包装成 Markdown 审阅稿"""
        lines = [
            f"## 🔍 资料搜索结果：{query}",
            "",
            f"> 搜索时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            raw.strip(),
        ]
        return "\n".join(lines)

    def _draft_note_with_llm(self, task: Dict[str, Any]) -> str:
        """调用 LLM 生成笔记草稿；异常或空输出时降级为直接提取任务描述"""
        title = task.get("task_title") or "未命名笔记"
        prompt = f"""你是一位学习助手。请根据以下任务信息整理一篇结构化学习笔记（Markdown 格式）：

【任务标题】{title}
【所属目标】{task.get('goal_title') or '未指定'}
【任务描述】{task.get('description') or '（无）'}

要求：包含「要点整理」「我的理解」「下一步行动」三个小节，总长度不超过 300 字。
只输出笔记正文，不要输出任何解释。"""
        try:
            draft = model_client.chat_sync(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=600,
            )
        except Exception as e:
            print(f"[TaskExecutor] LLM 笔记草稿失败（降级提取）: {e}")
            draft = ""
        draft = (draft or "").strip()
        if not draft:
            # 降级：直接以任务信息作为草稿内容，保证流程不中断
            draft = (f"# {title}\n\n"
                     f"- 所属目标：{task.get('goal_title') or '未指定'}\n"
                     f"- 任务描述：{task.get('description') or '（无）'}\n"
                     f"- 备注：LLM 暂不可用，此草稿由任务信息直接提取，请编辑完善。")
        return f"## 📝 笔记草稿:{title}\n\n{draft}"

    # ========== 两类自动任务处理器（只产出草稿，绝不写盘） ==========

    def handle_search_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """处理资料搜索任务：调用 web_search 并格式化摘要供审阅

        Returns:
            {"ok": bool, "message": str, "approval": PendingApproval}
            ok=False 表示搜索失败，无需进入审批环节。
        """
        title = (task.get("task_title") or "").strip() or "资料搜索"
        goal_id = task.get("goal_id") or ""
        try:
            raw = local_tools.web_search(title)
        except Exception as e:
            return {"ok": False,
                    "message": f"❌ 「{title}」搜索失败：{e}",
                    "approval": None}
        raw = (raw or "").strip()
        if raw.startswith("❌"):
            return {"ok": False, "message": f"❌ 「{title}」搜索失败：{raw}", "approval": None}

        content = self._format_search_result(raw, title)
        target_dir = research_dir_for(goal_id)
        filename = build_note_filename(title)
        approval = {
            "task_id": task.get("task_id"),
            "task_title": title,
            "goal_id": goal_id,
            "goal_title": task.get("goal_title") or "",
            "type": "search",
            "content": content,
            "suggested_path": str(target_dir).replace("\\", "/"),
            "filename": filename,
        }
        return {"ok": True,
                "message": f"🔍 「{title}」已完成搜索，共 {raw.count(chr(10)) + 1} 行结果待确认",
                "approval": approval}

    def handle_note_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """处理笔记整理任务：生成笔记草稿（LLM 优先，降级提取）供审阅

        Returns:
            {"ok": bool, "message": str, "approval": PendingApproval}
        """
        title = (task.get("task_title") or "").strip() or "未命名笔记"
        goal_id = task.get("goal_id") or ""
        content = self._draft_note_with_llm(task)
        target_dir = note_dir_for(goal_id)
        filename = build_note_filename(title)
        approval = {
            "task_id": task.get("task_id"),
            "task_title": title,
            "goal_id": goal_id,
            "goal_title": task.get("goal_title") or "",
            "type": "note",
            "content": content,
            "suggested_path": str(target_dir).replace("\\", "/"),
            "filename": filename,
        }
        return {"ok": True,
                "message": f"📝 「{title}」已生成笔记草稿，请审阅确认",
                "approval": approval}

    def handle_scout_digest_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """处理「资源」格任务：只读本地机会库，产出筛选报告草稿

        与搜索/整理两类的根本区别：**不联网、不调 LLM**。这一格存在的理由就是
        「用户不在时替他看一眼本地已经抓到的东西」——若它依赖联网或模型可用，
        就把「他不在」又换成了「网络/模型得在」，等于没解决原问题。

        筛完为空时同样产出（内容是空状态那一句），不当失败、不静默跳过：
        「报告缺席」本身就是要给他看的结果。

        Returns:
            {"ok": bool, "message": str, "approval": PendingApproval}
        """
        title = (task.get("task_title") or "").strip() or "资源筛选报告"
        goal_id = task.get("goal_id") or ""
        try:
            rows = scout_module.ResourceScout.list_opportunities(limit=500)
        except Exception as e:
            return {"ok": False,
                    "message": f"❌ 「{title}」读取本地机会库失败：{e}",
                    "approval": None}
        # 水位线（P7 批次 3）：从行里读出「上次播报过的桶」。
        # 依赖两列在行 dict 里存在——`list_opportunities` 走的是 `SELECT *`，
        # 故新增列自动可见；这里显式取用（缺列只会得到 None，等于「从未播报」，
        # 行为退化成「每期都播」，不会崩，但功能静默失效，故测试里钉了断言）。
        # 键必须与 `digest_notify_items` 返回的键**同一套规则**，否则水位线会错位，
        # 故这里直接复用 `core.resource_scout._digest_key`（不是另写一份取键逻辑）。
        notified = {scout_module._digest_key(r): r.get("notified_bucket")
                    for r in rows if isinstance(r, dict)}
        try:
            body = scout_module.build_resource_digest(
                rows, params=task.get("digest_params"), notified=notified)
        except Exception as e:
            return {"ok": False,
                    "message": f"❌ 「{title}」筛选报告生成失败：{e}",
                    "approval": None}
        # 记账时点 ＝ **生成时，不是批准时**（P7 批次 3 的主观选择）：
        # 与批次 2 的「决定执行就消费当期」同源——报告一旦生成，这些事实就已经
        # 「说过一次」了。代价（如实记）：报告被拒绝、或压根没打开，这些条目也不会
        # 再被播报；兜底不是本功能而是启动对账单（它无状态地一直列着过期/临期条目）。
        # 若改成「批准才算播报」，需要 pending → 机会行的回指，今天不存在，属另一批。
        try:
            scout_module.ResourceScout.mark_digest_notified(
                scout_module.digest_notify_items(
                    rows, notified=notified, params=task.get("digest_params")))
        except Exception as e:
            print(f"[TaskExecutor] 「资源」格水位线记账失败（忽略）: {e}")
        content = f"{_absence_header(task.get('previous_run_at'))}\n\n{body}"
        approval = {
            "task_id": task.get("task_id"),
            "task_title": title,
            "goal_id": goal_id,
            "goal_title": task.get("goal_title") or "",
            "type": "note",
            "content": content,
            "suggested_path": str(note_dir_for(goal_id)).replace("\\", "/"),
            "filename": build_note_filename(title),
        }
        return {"ok": True,
                "message": f"🧊 「{title}」已生成（本地机会库筛选，未联网）",
                "approval": approval}

    def handle_long_term_review_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """处理「复盘」格任务（P7 第一闭环 Phase 3c-甲，2026-10-06）：跑一次长期项目复盘

        与另外三格的根本区别：**这一格没有「待审批」这一步**，所以返回里显式带
        `requires_approval=False`。搜索/整理/资源三格产出的是「要写进笔记库的一份
        文档」，写盘前必须有人点头（绝不未经确认写盘）；这一格产出的是一行 Decision
        账目，而「这轮算不算数」在 `run_long_term_project_review` 的四个出口里已经判完
        了——人工段为空就不落账、缺少实质依据就落「请求补充」（阶段一 2026-10-11 改判：
        旧口径是「未知占主导」，实测会把每一份真实快照都挡在这一格）、观测判不了就不落账。
        再插一道审批等于把同一件事问两遍，且第二遍问的是「要不要把已经写好的账撤掉」，
        而 Decision 账本没有删除语义（状态机里根本没有指向「作废」的边）。

        State 从哪来（本批定案，设计席把这一格留给了执行席）：先
        `latest_project_state()` 读最近一份快照；**表为空时退回
        `build_project_state()` 现采一份只含自动段的**。退回不是为了凑数据，而是让
        「没有人工段」这件事由闸门①用**设计好的那句话**出声（「请先在人工段写下你自己的
        判断」），而不是由这里现编一条「project_states 是空的」——两条路终点相同
        （都走 manual_section_missing、都不写库），差别只在文案由谁负责。现采那份
        **不落盘**（不调 `save_project_state`）：这一格的职责是复盘，不是替用户攒快照。

        两个旁路都由这里**把函数直接递进去，不包 lambda**（阶段一 2026-10-11 起是「两个」）：
        复盘模块在 core 层，层序上不许 import agents（`checks/check_import_direction.py`），
        所以观测与问句都得由调用方注入。`agents.supervisor.supervisor_observe` 与
        `run_long_term_project_review` 的 observe 形参同为单参、签名天然匹配（这正是
        3a′ 只改参数名、没加第二个参数的回报）；问句旁路 `supervisor_ask_questions`
        同一形状，只有闸门②请求补充那一档会调它。经模块引用取属性，单测里 patch 那
        两个名字即可全程离线——**本批的测试不打网络、不调模型**；两个名字都必须 patch，
        少 patch 一个，那句「没打网络」就成了假话（这一条由 tests 的注入用例钉住）。

        Returns:
            {"ok", "message", "approval"(恒为 None), "requires_approval"(恒为 False),
             "outcome", "decision_id", "reason"}。`reason` 这一格是设计席点名要的：
            「不写库也要出声」——四个出口各自为什么没产出，原文必须**单独成格**传出去，
            不能只折进 message 里。理由不是排版洁癖：变异复验时实测过，只靠 message
            断言会被出口标签（如「这一轮没有产出：缺人工段」）先满足掉，那句真原因
            丢了测试也不知道。
        """
        title = (task.get("task_title") or "").strip() or "长期项目复盘"
        try:
            state = state_module.latest_project_state()
            if state is None:
                state = state_module.build_project_state()
            result = review_module.run_long_term_project_review(
                state, supervisor_module.supervisor_observe,
                ask_questions=supervisor_module.supervisor_ask_questions)
        except Exception as e:
            return {"ok": False,
                    "message": f"❌ 「{title}」复盘失败：{e}",
                    "approval": None, "requires_approval": False,
                    "outcome": None, "decision_id": None,
                    "reason": f"复盘没有跑起来：{e}"}

        outcome = result.get("outcome")
        decision_id = result.get("decision_id")
        label = review_module.OUTCOME_LABELS_ZH.get(
            outcome, f"未知出口 {outcome}（不该发生，照原样报出来）")
        if decision_id:
            tail = f"已写入 Decision #{decision_id}"
        else:
            tail = "本轮不写账本"
        return {"ok": True,
                "message": f"🧭 「{title}」{label}｜{tail}。{result.get('reason') or ''}",
                "approval": None,
                "requires_approval": False,
                "outcome": outcome,
                "decision_id": decision_id,
                "reason": str(result.get("reason") or "")}

    # ========== 审批落盘（唯一写入口） ==========

    def save_approval(self, approval: Dict[str, Any]) -> Dict[str, Any]:
        """把用户批准的结果写入磁盘（搜索）或数据库+文件（笔记）

        Returns:
            {"ok": bool, "message": str}；任何异常都被捕获并以 ok=False 返回。
        """
        if not isinstance(approval, dict) or not approval.get("content"):
            return {"ok": False, "message": "⚠️ 审批内容为空，已忽略"}
        ntype = approval.get("type")
        title = approval.get("task_title") or "未命名"
        content = approval["content"]
        try:
            if ntype == "note":
                note_id = self.note_store.add_note(approval.get("goal_id") or "",
                                                   title, content)
                if note_id and note_id > 0:
                    path_hint = approval.get("suggested_path") or ""
                    return {"ok": True,
                            "message": f"💾 已保存笔记「{title}」（#{note_id}）到 {path_hint}"}
                return {"ok": False, "message": f"❌ 笔记「{title}」保存失败（参数非法或写入出错）"}
            if ntype == "search":
                base_dir = approval.get("suggested_path") or ""
                filename = approval.get("filename") or build_note_filename(title)
                result = local_tools.write_file(filename, content, base_dir=base_dir)
                ok = str(result).startswith("✅")
                return {"ok": ok, "message": f"{'💾 ' if ok else ''}{result}"}
            return {"ok": False, "message": f"⚠️ 未知的审批类型：{ntype}"}
        except Exception as e:
            return {"ok": False, "message": f"❌ 保存失败：{e}"}

    @staticmethod
    def discard_approval(approval: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """丢弃用户拒绝的结果（不产生任何写操作）"""
        title = (approval or {}).get("task_title") or "该结果"
        return {"ok": True, "message": f"🚫 已拒绝并丢弃「{title}」的结果"}

    # ========== 审批持久化（V0.2 新增） ==========

    @staticmethod
    def _persist_approval(approval_store, approval: Dict[str, Any]) -> Optional[int]:
        """把待审批请求写入持久化表，返回记录 ID；失败返回 None（不阻断流程）

        Args:
            approval_store: ApprovalStore 实例（可注入 mock）
            approval: 审批字典
        """
        try:
            pending_id = approval_store.add_pending(approval)
            return pending_id if pending_id and pending_id > 0 else None
        except Exception as e:
            print(f"[TaskExecutor] 审批记录写入失败（忽略，仅内存流）: {e}")
            return None

    @staticmethod
    def _resolve_pending(approval_store, pending_id, status: str) -> None:
        """更新持久化审批记录状态；失败仅打印日志，不影响主流程

        说明：保存失败时保留 pending 状态（status 不变），用户稍后可从
        「待审批」列表重试；确认成功/拒绝时分别置为 approved/rejected。
        """
        if not pending_id:
            return
        try:
            approval_store.update_status(pending_id, status)
        except Exception as e:
            print(f"[TaskExecutor] 审批状态更新失败（忽略）: {e}")

    # ========== 主流程：计划执行生成器 ==========

    def execute_plan(self, plan: Any) -> Generator[Dict[str, Any], Any, None]:
        """逐项执行计划中的可自动任务（生成器）

        Args:
            plan: 计划列表（generate_daily_plan 返回的 plan 字段；
                  也容忍误传 {"plan": [...]} 整包字典）。

        Yields:
            事件字典（协议见模块 docstring）。遇到 approval 事件时暂停：
            - gen.send(True)  → 批准保存，继续执行后续任务；
            - gen.send(False) → 拒绝丢弃，继续执行后续任务；
            - next(gen)       → 视为拒绝（安全默认，绝不未经确认写盘）。

        Notes:
            非 auto_executable 的任务不会被执行（仅在开始时统计说明）；
            单个任务失败不影响后续任务；全程不抛异常给调用方。
        """
        # 容错：误传整包 {"plan": [...]} 时解出列表
        if isinstance(plan, dict):
            plan = plan.get("plan") or []
        tasks: List[Dict[str, Any]] = [t for t in (plan or []) if isinstance(t, dict)]
        auto_tasks = [t for t in tasks if t.get("auto_executable")]

        stats = {"total": len(tasks), "auto": len(auto_tasks),
                 "saved": 0, "rejected": 0, "skipped": 0, "failed": 0}

        if not tasks:
            yield {"type": "done", "message": "⚠️ 计划为空，没有可执行的任务",
                   "stats": stats}
            return
        if not auto_tasks:
            yield {"type": "done",
                   "message": (f"ℹ️ 共 {len(tasks)} 个任务，其中没有标记为「⚡ 可自动」的任务；"
                               "如需自动执行请在生成计划时让系统标记（搜索/整理类任务会自动命中）"),
                   "stats": stats}
            return

        yield {"type": "progress",
               "message": (f"▶️ 开始执行计划：共 {len(tasks)} 个任务，"
                           f"其中 **{len(auto_tasks)} 个可自动执行**"),
               "stats": dict(stats)}

        for idx, task in enumerate(auto_tasks, start=1):
            title = (task.get("task_title") or "").strip() or f"任务#{task.get('task_id', '?')}"
            goal_title = task.get("goal_title") or ""
            prefix = f"[{idx}/{len(auto_tasks)}]"
            yield {"type": "progress",
                   "message": f"{prefix} ▶️ 正在处理：**{title}**（{goal_title}）"}

            # 任务类型：显式 task_kind 优先（「资源」格走这条），其余回退到
            # 标题关键词分类（搜索/笔记两类的既有行为完全不变）
            kind = task.get("task_kind")
            task_type = kind if kind in SUPPORTED_TASK_KINDS \
                else classify_task(self._task_text(task))
            if not task_type:
                stats["skipped"] += 1
                yield {"type": "result",
                       "message": f"{prefix} ⏭️ 「{title}」暂不支持自动执行，已跳过"}
                continue

            if task_type == "search":
                handler = self.handle_search_task
            elif task_type == "note":
                handler = self.handle_note_task
            elif task_type == TASK_KIND_SCOUT_DIGEST:
                handler = self.handle_scout_digest_task
            else:
                handler = self.handle_long_term_review_task
            try:
                outcome = handler(task)
            except Exception as e:
                stats["failed"] += 1
                yield {"type": "result",
                       "message": f"{prefix} ❌ 「{title}」执行异常：{e}"}
                continue

            if not outcome.get("ok"):
                stats["failed"] += 1
                yield {"type": "result", "message": f"{prefix} {outcome.get('message')}"}
                continue

            # 「复盘」格走这条出口（P7 第一闭环 Phase 3c-甲）：账本已由 core 层写完，
            # 这里只播报、不进审批暂停点。判据用 `is False` 而不是「假值」——既有三个
            # handler 都不带这个键，取到的是 None，`None is False` 为假，因此它们
            # 一行代码都不会改道（这条是纯追加）。
            # 计数口径：落了一行 Decision 算 saved，没落（缺人工段／本周无需决策）算
            # skipped——后者是设计里的正常出口、不是失败，但它也必须被计一次，
            # 否则 saved+rejected+skipped+failed 不再等于 auto。
            if outcome.get("requires_approval") is False:
                if outcome.get("decision_id"):
                    stats["saved"] += 1
                else:
                    stats["skipped"] += 1
                yield {"type": "result",
                       "message": f"{prefix} {outcome.get('message')}"}
                continue

            approval = outcome.get("approval") or {}
            # 审批持久化：暂停前写入 pending 记录（重启后仍可在 UI 处理）；
            # 写入失败仅打印日志，不影响内存生成器流程
            pending_id = self._persist_approval(self.approval_store, approval)
            approval = dict(approval)
            approval["pending_id"] = pending_id

            # 暂停等待审批；next()/None 恢复一律视为拒绝（安全默认）
            decision = yield {"type": "approval",
                              "message": f"{prefix} ⏸️ 「{title}」需要你确认后才会保存",
                              "approval": approval,
                              "audio": beep_audio_html(autoplay=True)}

            if decision:
                save_outcome = self.save_approval(approval)
                if save_outcome.get("ok"):
                    stats["saved"] += 1
                    self._resolve_pending(self.approval_store, pending_id, "approved")
                else:
                    # 保存失败：保留 pending 状态，用户可从「待审批」列表重试
                    stats["failed"] += 1
                yield {"type": "result",
                       "message": f"{prefix} {save_outcome.get('message')}"}
            else:
                stats["rejected"] += 1
                self._resolve_pending(self.approval_store, pending_id, "rejected")
                discard = self.discard_approval(approval)
                yield {"type": "result",
                       "message": f"{prefix} {discard.get('message')}"}

        summary = (f"🏁 执行完成：自动任务 {stats['auto']} 个｜"
                   f"已保存 {stats['saved']}｜拒绝 {stats['rejected']}｜"
                   f"跳过 {stats['skipped']}｜失败 {stats['failed']}")
        yield {"type": "done", "message": summary, "stats": stats}

    # ========== 任务模板（一键复用，v3.8 新增） ==========

    @staticmethod
    def _build_plan_from_template(template,
                                  previous_run_at: Any = None
                                  ) -> List[Dict[str, Any]]:
        """把模板动作翻译为计划任务列表（任务结构与每日计划完全一致）

        按 action_type 分派（**这里是模板→任务的唯一分发点**）：

        1) action_type='search_and_note'（搜索并整理笔记）：按「先收集资料、
           再整理成文」的自然顺序生成两条自动任务——搜索「关键词」相关资料
           （search 类）+ 整理「关键词」学习笔记（note 类）；
        2) action_type='scout_digest'（P7 批次 1a「资源」格）：只生成一条任务，
           读本地 opportunities 产出筛选报告。任务里显式带 task_kind 标记，
           execute_plan 据此直接分发到 handle_scout_digest_task，不走关键词猜测；
           筛选条件由 action_params 传入（见 core.resource_scout.build_resource_digest
           的 _digest_params：limit / preopen_days / dormant_days /
           min_value_score / min_project_reuse，缺省即默认口径）。
        3) action_type='long_term_review'（P7 第一闭环 Phase 3c-甲，2026-10-06）：
           同样只生成一条任务、同样显式带 task_kind，但**不读 action_params**——
           复盘的输入是一份 Project State，不是关键词也不是筛选条件。这一条任务
           刻意不带 previous_run_at：那格是「缺席页眉」的天数基准，只有搜索/资源
           两格的产物文案会用到它，复盘的四个出口里没有一处读它（传了等于留一个
           没人消费的字段，下一个人还得回来查它到底有没有用）。

        模板没有所属真实目标：统一挂到虚拟目标 template_goal_id(template.id)
        （即 data/goals/template_{id}/，每个模板独立目录，互不干扰；
        NoteStore.add_note 要求 goal_id 非空）。该 goal_id 同时用于关联
        模板产生的待审批记录（堆积控制查询依据）。

        Args:
            template: TaskTemplate 实例
            previous_run_at: **抢占前**的 last_run_at（定时路径由
                check_scheduled_templates 显式传入）。执行期该字段已被防重入
                抢占改成本次运行时刻，直接读模板只会得到「0 天前」。

        Returns:
            计划任务列表；模板不存在 / 动作类型不支持 / 缺少搜索关键词时
            返回空列表（调用方据此给出友好提示）。
        """
        if template is None:
            return []
        if template.action_type == template_module.ACTION_SCOUT_DIGEST:
            return [{
                "task_id": None,
                "goal_id": template_module.template_goal_id(template.id),
                "goal_title": "任务模板",
                "task_title": f"资源筛选报告（{date.today().strftime('%Y-%m-%d')}）",
                "description": ("只读本地机会库，按「临期 / 无时点长期挂着」两段"
                                "生成筛选报告（不联网）"),
                "estimated_hours": 0.1,
                "reason": f"模板：{template.name or '未命名模板'}",
                "auto_executable": True,
                "task_kind": TASK_KIND_SCOUT_DIGEST,
                "digest_params": template.action_params_dict,
                "previous_run_at": previous_run_at,
            }]
        if template.action_type == template_module.ACTION_LONG_TERM_REVIEW:
            return [{
                "task_id": None,
                "goal_id": template_module.template_goal_id(template.id),
                "goal_title": "任务模板",
                "task_title": f"长期项目复盘（{date.today().strftime('%Y-%m-%d')}）",
                "description": ("读最近一份项目 State：人工段为空就不调观测；"
                                "调了就把结论写进 Decision 账本（不产笔记、"
                                "不产待审批项）"),
                "estimated_hours": 0.1,
                "reason": f"模板：{template.name or '未命名模板'}",
                "auto_executable": True,
                "task_kind": TASK_KIND_LONG_TERM_REVIEW,
            }]
        if template.action_type != template_module.ACTION_SEARCH_AND_NOTE:
            return []
        query = (template.query or "").strip()
        if not query:
            return []
        reason = f"模板：{template.name or '未命名模板'}"
        goal_id = template_module.template_goal_id(template.id)
        return [
            {
                "task_id": None,
                "goal_id": goal_id,
                "goal_title": "任务模板",
                "task_title": f"搜索「{query}」相关资料",
                "description": f"模板自动任务：搜索关键词「{query}」的相关资料",
                "estimated_hours": 0.5,
                "reason": reason,
                "auto_executable": True,
            },
            {
                "task_id": None,
                "goal_id": goal_id,
                "goal_title": "任务模板",
                "task_title": f"整理「{query}」学习笔记",
                "description": f"模板自动任务：整理关键词「{query}」的学习笔记",
                "estimated_hours": 0.5,
                "reason": reason,
                "auto_executable": True,
            },
        ]

    def execute_template(self, template_id: int,
                         triggered_by: str = "manual",
                         previous_run_at: Any = _UNSET
                         ) -> Generator[Dict[str, Any], Any, None]:
        """执行任务模板（生成器，事件协议与 execute_plan 完全一致）

        Args:
            template_id: 任务模板 ID
            triggered_by: 'manual'（手动）/ 'scheduled'（定时），仅用于文案
            previous_run_at: **抢占前**的 last_run_at（缺席页眉的天数基准）。
                定时路径必须由 check_scheduled_templates 传入——那条路在真正
                执行之前就把 last_run_at 抢占成了本次运行时刻，此处再读模板
                只会得到「0 天前」。默认值用哨兵 `_UNSET` 而不是 None，以区分
                「没传」（手动路径不传，回退到模板当前值）与「传了 None」
                （抢占前的值本就是 None＝首次运行，产出「首次运行」）。
                None 在这里是**合法实参**，不是「缺省」标记。

        Yields:
            与 execute_plan 相同的事件字典；模板在审批暂停点暂停：
            - auto_approve=1：不暂停，自动批准保存（yield progress 说明）；
            - auto_approve=0：暂停等待外部决定（send(True/False)；
              next() 恢复视为拒绝，安全默认——绝不未经确认写盘）。

        生成器被完整消费（跑完所有任务与审批）后，自动把模板 last_run_at
        更新为当前时间。
        """
        template = template_module.get_template(template_id)
        empty_stats = {"total": 0, "auto": 0, "saved": 0,
                       "rejected": 0, "skipped": 0, "failed": 0}
        if template is None:
            yield {"type": "done",
                   "message": f"⚠️ 模板 #{template_id} 不存在或已被删除",
                   "stats": dict(empty_stats)}
            return
        if not template.enabled:
            yield {"type": "done",
                   "message": f"⏸️ 模板「{template.name}」当前已停用，已跳过执行",
                   "stats": dict(empty_stats)}
            return
        # 待审批堆积控制：该模板已有未处理的待审批（上次运行未处理完），
        # 跳过本次触发，避免待审批无限积累（手动 / 定时统一生效；
        # 定时路径在 check_scheduled_templates 中还会前置检查一次以保留
        # 「推迟到积压清空后再触发」的语义，此处作为兜底与手动入口）
        if template_module.pending_approval_count(template_id) > 0:
            yield {"type": "done",
                   "message": (f"⏸️ 模板「{template.name}」存在未处理的待审批"
                               "（请先在界面「待审批」列表处理后再运行），"
                               "已跳过本次触发"),
                   "stats": dict(empty_stats)}
            return
        plan = self._build_plan_from_template(
            template,
            previous_run_at=(previous_run_at if previous_run_at is not _UNSET
                             else template.last_run_at))
        if not plan:
            yield {"type": "done",
                   "message": (f"⚠️ 模板「{template.name}」缺少有效的动作参数"
                               "（如搜索关键词），已跳过执行"),
                   "stats": dict(empty_stats)}
            return

        # 触发即记账（P7 批次 2）：**一旦决定执行，就当场把「这一期已被本次占用」
        # 写进 last_run_at**。为什么必须在触发点记账——auto_approve=0 的模板会在
        # 审批暂停点把生成器交给调用方（界面存进 gr.State）；用户从「待审批」页处理时
        # 走的是纯 DB 路径（approve_pending_from_db / reject_pending_from_db 都不推进
        # 生成器），生成器被永久遗弃 ⇒ 永远到不了 done ⇒ 下面那次「跑完记账」永不发生。
        # 后果不是「延迟」而是**对拍**：那次不记账、档期没被消费，只要模板处于「当期已到期」，
        # 处理掉 pending 之后下一周期就会再补一条同名报告，可以无限重复。
        # 与下面 done 后那次的分工（两种语义都要保留）：
        #   这里 = 「这一期已被本次占用」（防对拍，决定跑就记）；
        #   done 后 = 「跑完的时刻」（缺席页眉的天数基准）。
        # 位置刻意放在 plan 建成**之后**：_build_plan_from_template 读的是 template
        # 对象快照，而 update_last_run 只写库、不改这个对象，页眉基准因此不受影响；
        # 两个「没执行就不记账」的早返回点（堆积跳过 / 参数非法）都在这之前，语义未变。
        try:
            template_module.update_last_run(
                template_id, datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            print(f"[TaskExecutor] 模板「{template.name}」"
                  f"触发记账失败（忽略）: {e}")

        trigger_label = "手动" if triggered_by == "manual" else "定时"
        yield {"type": "progress",
               "message": (f"🧩 模板「{template.name}」（{trigger_label}触发）："
                           f"共生成 {len(plan)} 个自动任务，开始执行…")}

        auto = bool(template.auto_approve)
        gen = self.execute_plan(plan)
        decision: Any = None
        finished = False
        while not finished:
            try:
                event = gen.send(decision) if decision is not None else next(gen)
            except StopIteration:
                finished = True
                break
            except Exception as e:
                yield {"type": "result",
                       "message": f"❌ 模板「{template.name}」执行异常：{e}"}
                finished = True
                break
            decision = None

            if event.get("type") == "approval":
                if auto:
                    # 自动审批：不暂停，直接批准保存（send True 让内层继续）
                    yield {"type": "progress",
                           "message": "🤖 自动审批：模板已配置自动通过，正在保存…"}
                    decision = True
                else:
                    # 手动审批：暂停等待外部决定；next()/None 视为拒绝
                    decision = yield event
            else:
                if event.get("type") == "done":
                    # 内层执行完毕：先记录本次运行时间，再转发 done 事件。
                    # 注意必须在 yield 之前更新——调用方（UI/调度线程）收到
                    # done 后通常立即停止消费，循环后的代码不会被执行。
                    # 与函数上方「触发即记账」的分工：那次是「这一期已被占用」（防对拍），
                    # 这次是「跑完的时刻」（缺席页眉的天数基准）——两者都要保留。
                    try:
                        template_module.update_last_run(
                            template_id,
                            datetime.now().isoformat(timespec="seconds"))
                    except Exception as e:
                        print(f"[TaskExecutor] 模板「{template.name}」"
                              f"运行时间更新失败（忽略）: {e}")
                yield event

        # 说明：生成器在 done 事件前已更新 last_run_at；此处不再重复更新。
        # 另外两处记账：触发点那次（「这一期已被占用」，防对拍）与调度路径执行前的
        # 抢占（check_scheduled_templates，同样是先记账再执行）。三者都不冲突：
        # 抢占与触发点记账解决「会不会重复产出同一期」，done 那次决定页眉的天数基准。

    def _consume_template_gen(self, gen, template) -> Dict[str, Any]:
        """后台（调度）线程消费模板执行生成器，返回结构化结果

        自动审批模板：完整执行（执行器内部自动批准所有结果并落盘）；
        手动审批模板：执行到首个审批暂停点即停止——审批记录已由内层
        execute_plan 持久化到 pending_approvals 表（status=pending），
        本方法不发送任何决定（绝不未经用户确认写入磁盘），用户稍后可从
        界面的「待审批」列表处理。

        Returns:
            {"ok": bool, "paused": bool, "message": str}
            message 取最后一条事件文案（如模板跳过 / 暂停提示 / 完成总结），
            便于调度日志与 UI 直观展示执行结果。
        """
        last_message = ""
        try:
            while True:
                event = next(gen)
                last_message = str(event.get("message") or "")
                if event.get("type") == "approval":
                    approval = event.get("approval") or {}
                    pending_id = approval.get("pending_id")
                    title = approval.get("task_title") or ""
                    pause_msg = (f"已产生待审批项 "
                                 f"#{pending_id if pending_id else '?'}「{title}」，"
                                 "请在界面「待审批」列表确认后保存")
                    return {"ok": True, "paused": True, "message": pause_msg}
        except StopIteration:
            return {"ok": True, "paused": False,
                    "message": last_message or "执行完成"}
        except Exception as e:
            return {"ok": False, "paused": False, "message": f"执行异常：{e}"}

    def check_scheduled_templates(self, trigger_source: str = "timer"
                                  ) -> List[Dict[str, Any]]:
        """检查并执行所有到期的定时模板（由调度线程/定时器周期调用）

        防重入设计：对每个到期模板先调用 update_last_run（BEGIN IMMEDIATE
        事务内抢占 last_run_at）再执行；即使执行耗时超过一个检查周期，
        也不会被并发/重复检查再次触发（先更新再执行，见需求约定）。

        运行记录（P7 批次 14，2026-10-06）：本次跑结束时按 trigger_source 落一行
        template_runs——「跑没跑、成功几件失败几件、产出几行、连的是哪个库」从此在库里
        有痕迹，不再只活在一行 print 里。写行条件与五个计数器的含义见
        core.task_template.RECORD_ALWAYS_SOURCES 与 template_runs 的建表注释。

        Args:
            trigger_source: 'boot'（开机补算，app.py）/ 'cli'（命令行入口，
                scripts/boot_compensate.py）/ 'timer'（界面 60 秒定时器，默认）。
                它只决定**这次要不要落一行**，不改变执行与防重入的任何行为。

        Returns:
            每个到期模板的执行结果列表，元素形如：
            {"template_id", "template_name", "ok", "paused", "message"}
            （批次 14 定案第 3 条：计数走函数内的局部变量，**不往这个返回值里加键**）
        """
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        results: List[Dict[str, Any]] = []
        due: List[Any] = []
        due_count: Optional[int] = None
        read_fail_note = ""
        try:
            due = template_module.get_due_templates()
            due_count = len(due)
        except Exception as e:
            # 读不出到期清单 ≠ 到期 0 件：due_count 留 NULL、status 记 error，
            # 让「这次没读到」在库里与「这次确实没东西要跑」长得不一样（判据 3 那一族）
            read_fail_note = f"到期清单读取失败: {e}"
            print(f"[TaskExecutor] 定时模板检查失败（忽略）: {e}")
        # C4「撤销单位＝一次运行」的跑前基线：跑后 id > 该值的待审批行即本次产出
        # （差值法，见 core.task_template.latest_pending_approval_id 的已知限制说明）
        after_id = template_module.latest_pending_approval_id()
        claim_time = datetime.now().isoformat(timespec="seconds")
        ran = paused = backlog_skip = claim_skip = failed = 0
        for template in due:
            # 待审批堆积控制：该模板存在未处理的待审批 → 跳过本次触发。
            # 注意必须在抢占 last_run_at 之前判断——跳过时不更新 last_run_at，
            # 模板保持「到期」状态。**但「处理完后自动恢复触发」是句假话**（P7 批次 3
            # 更正）：创建那条待审批的触发已经消费了当期（批次 2 的触发即记账），
            # 所以处理完之后不会立即重跑，下一次产出等它自己的触发日。
            # （边界：若那次触发发生在本期触发点之前，本期触发点仍会照常触发一次。）
            pending_count = template_module.pending_approval_count(template.id)
            if pending_count > 0:
                backlog_skip += 1
                outcome = {"ok": True, "paused": True,
                           "message": (f"存在 {pending_count} 条未处理的待审批，"
                                       "跳过本次触发（处理完之前都不会再触发）")}
                outcome["template_id"] = template.id
                outcome["template_name"] = template.name
                results.append(outcome)
                print(f"[TaskExecutor] 定时模板「{template.name}」存在未处理"
                      f"待审批（{pending_count} 条），跳过本次触发")
                continue
            # 防重入抢占：先更新 last_run_at；更新失败说明记录已被并发触发
            # 或已被删除，跳过本次执行
            if not template_module.update_last_run(template.id, claim_time):
                claim_skip += 1
                # 这一支原先**连一行 print 都没有**（P7 批次 14 要消灭的静默点）：
                # 抢占失败＝这一期被别的执行器拿走了（或模板已被删除），是缺席期
                # 里最容易「什么都没发生又什么都不留」的一种情况
                print(f"[TaskExecutor] 定时模板「{template.name}」抢占失败"
                      f"（last_run_at 未被本线程更新），跳过本次执行")
                continue
            try:
                # previous_run_at 传**抢占前**的旧值（due 列表由 get_due_templates
                # 在抢占之前读出）：产出顶部的「缺席页眉」要用它算「上一份距今几天」，
                # 抢占之后模板上的 last_run_at 已是本次时刻，读它只会永远得到 0 天。
                gen = self.execute_template(template.id, triggered_by="scheduled",
                                            previous_run_at=template.last_run_at)
                outcome = self._consume_template_gen(gen, template)
            except Exception as e:
                outcome = {"ok": False, "paused": False,
                           "message": f"执行异常：{e}"}
            outcome["template_id"] = template.id
            outcome["template_name"] = template.name
            results.append(outcome)
            # 五个计数器之一。paused 先判——审批暂停点的 ok 同样是 True，两支不重叠；
            # 到这里的一轮迭代必然命中三者之一（堆积跳过与抢占失败都已在上面 continue）
            if outcome.get("paused"):
                paused += 1
            elif outcome.get("ok"):
                ran += 1
            else:
                failed += 1
            # 日志用 ASCII 状态标记（GBK 控制台无法编码 ✅/❌），消息经
            # log_safe 剔除 emoji 后输出，避免控制台编码崩溃
            status = "OK" if outcome.get("ok") else "FAIL"
            print(f"[TaskExecutor] 定时模板「{template.name}」{status} "
                  f"（{log_safe(outcome.get('message'))}）")

        # ---------- 本次跑落一行运行记录（P7 批次 14）----------
        # 写行条件（定案第 2 条）：boot / cli 每次必记（boot 行是 V3「上次开机几点」的
        # 分母，cli 行是 V4「连跑两次第二次全跳过」的第二次痕迹）；timer 只在确有执行
        # （ran / paused / failed 任一 > 0）时记——界面那个 60 秒空转不产行。
        # 不变式（构造性成立：循环里每个到期模板恰好走一条分支、给一个计数器 +1）：
        #     due_count == ran + paused + backlog_skip + claim_skip + failed
        # 唯一例外是 due_count 为 None（到期清单没读出来），那时五个计数器全为 0。
        should_record = (trigger_source in template_module.RECORD_ALWAYS_SOURCES
                         or (ran + paused + failed) > 0)
        if not should_record:
            return results
        # status 三分支：读失败＝error（due_count 留 NULL）；有失败项＝partial；
        # 其余＝ok。partial 只留给 failed > 0（定案第 3 条），堆积跳过不算 partial——
        # 它是设计内的沉默，不是出错。
        run_status = ("error" if due_count is None
                      else "partial" if failed else "ok")
        produced_rows = 0
        if after_id is not None:
            new_max = template_module.latest_pending_approval_id()
            if new_max is not None:
                produced_rows = max(0, new_max - after_id)
        run_id = template_module.record_template_run(
            trigger_source=trigger_source,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status=run_status, due_count=due_count, ran=ran, paused=paused,
            backlog_skip=backlog_skip, claim_skip=claim_skip, failed=failed,
            produced_rows=produced_rows, note=read_fail_note)
        if run_id is not None and after_id is not None:
            attached = template_module.attach_pending_approvals_to_run(
                run_id, after_id)
            if attached != produced_rows:
                # 两个数本该相等（同一个 id 区间）。不等就是「归属被别的执行器污染的
                # 现场」（差值法那条已知限制），如实出声，不静默取其中一个
                print(f"[TaskExecutor] 运行记录 #{run_id} 的产出数与归属行数不一致："
                      f"差值法 {produced_rows}／实际标上 {attached}")
        return results


# 模块级默认实例（供 app.py 直接使用，模式同 planner_agent / reminder_agent）
task_executor = TaskExecutor()
