# -*- coding: utf-8 -*-
"""聊天上下文接入（`CHAT_CONTEXT_SPEC.md` v3）：「## 🧭 当前工作现场」摘要的验收用例

2026-10-24，执行席。被测的生产改动只有一处：`core/context_manager.py` 新增
`ContextManager._build_work_context()`，并由 `build_system_prompt` 插到所有注入段最前；
同一处接线里移除了 `_build_goal_context` 与 `_build_tasks` 两个调用（函数体都保留不删）。
本文件的判据逐条对应 spec §5 的 A.1–A.7／B.1–B.6／C.1–C.3，**没有加判据之外的断言**。

三条与仓库既有教训对齐的写法约定：

- **隔离**：模块顶部先 `setup_test_db()` 再 import 任何会碰库的业务模块。本文件**不 import
  任何 ui 模块**（`ui/controllers/_shared.py` 导入期就调 `init_db()`，import 了就会在生产库上
  跑迁移）。
- **日期**：直测 `_build_work_context(today=...)` 的用例一律用**固定字面量**（`2026-10-20`
  那一族），跨天重跑不会变成另一件事；走 `build_system_prompt` 的用例只能现算
  `date.today()`——那条路径不接 `today` 参数（本批不扩签名），夹具就照它现算（spec §6.5）。
- **C 组零网络**：每轮必走的 `supervisor_decision` 在 `core.runtime` 那一侧桩掉，模型换成
  本文件的确定性桩；同时把 `business/model_client.py` 的三个咽喉点（`chat_sync`／
  `stream_chat`／`stream_chat_with_tools`）装成「一调用就 raise」的哨兵——它证明的是
  「除了桩之外没人打真网络」，不是用来凑断言。

⚠️ 中文串里一律用「」而不用 ASCII 双引号；超限那一句与四句空态文案都逐字照 spec §2.2，
改动任何一句都要先改 spec，不许在用例里悄悄换词。
"""

import contextlib
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import business.model_client as model_client  # noqa: E402
import memory.database as md  # noqa: E402
from agents.tool_manager import TOOLS_DEFINITIONS  # noqa: E402
from core import runtime as runtime_module  # noqa: E402
from core.conversation_adapter import JsonConversationStoreAdapter  # noqa: E402
from core.context_manager import (  # noqa: E402
    WORK_ADVICE_NOTE,
    WORK_FOCUS_CAP,
    WORK_GOAL_CAP,
    WORK_OVERDUE_CAP,
    WORK_READ_FAIL,
    WORK_TODAY_CAP,
    ContextManager,
)
from core.current_focus import create_focus  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.goal_manager import GoalManager  # noqa: E402
from core.interfaces import ModelPlugin  # noqa: E402
from core.runtime import AgentRuntime  # noqa: E402
from core.trace_store import TraceStore  # noqa: E402
from memory.chat_store import ChatStore  # noqa: E402

md.init_db()

gm = GoalManager()

#: spec §2.1 那一句段标题，逐字。闭环教训段的标题也以「## 🧭 」开头，所以判存在
#: 必须按整句，不许只按 emoji（否则 B.5 那种「不含逾期／不含活跃目标」会被串台）。
_WORK_HEADER = "## 🧭 当前工作现场"

#: 固定字面量那一族：直测 `_build_work_context(today=...)` 用它们
_TODAY = "2026-10-20"
_YESTERDAY = "2026-10-18"
_TWO_DAYS_AGO = "2026-10-17"
_THREE_DAYS_AGO = "2026-10-16"
_TOMORROW = "2026-10-21"

#: 走 `build_system_prompt` 的那几条只能现算（该路径不接 today 参数）
_REAL_TODAY = date.today().isoformat()

#: spec §3.4 冲突夹具里那三条原话，逐字照抄
_SESSION_GOAL_TITLE = "构建长期自进化ai"
_FOCUS_TITLE = "提升英语成绩"
_ENGLISH_TASK = "高考核心词汇每日背诵与复习"
_SESSION_GOAL_TASK = "给记忆模块写用例"

_BASE = "基础提示词。"

#: 「零迁移」断言的基准：`memory/database.py` 里 `goal_tasks` 的建表语句（DDL）加上三条
#: `_add_column_if_not_exists`（milestone_id／planned_date／is_home）＝此刻的 11 列；旧表
#: `tasks` 的 DDL 加一条 `goal_id` 迁移＝此刻的 8 列。列名集合变了就说明有迁移动作，本批
#: 不许它变。复跑（不要照抄这里的顺序，以复跑为准）：
#:     .\.venv\Scripts\python.exe -c "import memory.database as d; d.DB_PATH='x.db'; ..."
#: 或直接在测试库里看本用例打印出来的那一串。
_GOAL_TASK_COLUMNS = ("id", "goal_id", "title", "description", "status", "priority",
                      "source", "created_at", "milestone_id", "planned_date", "is_home")
_LEGACY_TASK_COLUMNS = ("id", "title", "subject", "status", "priority", "due_date",
                        "created_at", "goal_id")

#: B.6 的基准工具名集合：与 `agents/tool_manager.py::TOOLS_DEFINITIONS` 里那六条 schema 的
#: `function.name` 一字对应（spec §0「工具只有 6 个、无工作台读工具」那一行）。
_BASELINE_TOOL_NAMES = {"read_file", "list_files", "get_current_time",
                        "web_search", "rag_search", "write_file"}

#: 只有「共 M 条／此处列 K 条」这两个数一起出现才算符合统一超限格式（spec §2.2）
_OVERFLOW_RE = re.compile(r"（共 (\d+) 条，此处列 (\d+) 条，另 (\d+) 条未列）")

_created_goals: List[str] = []
_created_tasks: List[int] = []
_created_focus: List[int] = []
_created_legacy_tasks: List[int] = []


# ==================== 夹具 ====================

def _execute(sql: str, params: tuple = ()) -> int:
    conn = md.get_connection()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _scalar_rows(sql: str, params: tuple = ()) -> List[tuple]:
    conn = md.get_connection()
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _make_goal(title: str, archived: bool = False) -> str:
    goal_id = gm.create_goal(title, "聊天上下文接入的夹具目标").goal_id
    _created_goals.append(goal_id)
    if archived:
        gm.archive_goal(goal_id)
    return goal_id


def _make_task(goal_id: str, title: str, planned: Optional[str],
               status: str = "pending", priority: int = 1) -> int:
    """建一条任务并把 `planned_date`／`status` 造成指定形状，返回任务 id

    照 `tests/test_goal_manager.py` 的同一形状：那几档形状（未排期 NULL、空串排期、
    逾期且已完成）没有对应的公开写口，夹具在测试库里用裸 SQL 补列，不碰生产库。
    `planned=None` ⇒ 这一列从不被写过（DDL 默认 NULL＝从未排期）。
    """
    gm.add_task(goal_id, title, priority=priority)
    conn = md.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM goal_tasks WHERE goal_id = ? AND title = ? ORDER BY id DESC",
            (goal_id, title)).fetchone()
        assert row is not None, f"add_task 没落库：{title}"
        task_id = int(row[0])
    finally:
        conn.close()
    _created_tasks.append(task_id)
    if planned is not None:
        _execute("UPDATE goal_tasks SET planned_date = ? WHERE id = ?", (planned, task_id))
    if status != "pending":
        _execute("UPDATE goal_tasks SET status = ? WHERE id = ?", (status, task_id))
    return task_id


def _make_legacy_task(title: str, subject: str = "英语") -> int:
    """往**旧 `tasks` 表**插一条待办（B.3 用；本批不迁移，这张表此刻仍在工作）"""
    _execute("INSERT INTO tasks (title, subject, status, priority) "
             "VALUES (?, ?, 'pending', 1)", (title, subject))
    rows = _scalar_rows("SELECT id FROM tasks WHERE title = ?", (title,))
    assert rows, f"旧表夹具没插进去：{title}"
    task_id = int(rows[-1][0])
    _created_legacy_tasks.append(task_id)
    return task_id


def _make_focus(title: str) -> int:
    """走公开写口建一条 active 重点（`create_focus` 拒绝第 4 条，判据①）"""
    focus_id = create_focus(title)
    _created_focus.append(focus_id)
    return focus_id


def _force_focus_beyond_cap(title: str) -> int:
    """裸 SQL 插第 4 条 active 重点：读侧要能被顶到上限，才有超限句可验

    `active ≤ 3` 是**写路径**校验（锚点 §一.12），不是 DDL 约束，所以这一档形状在库里
    本来就可能存在（旧数据／直接改库）。A.4 要的就是「读侧遇到 4 条只列 3 条」，
    不是绕过写口去污染数据——测试库由 `setup_test_db()` 隔离，收尾整目录删除。
    """
    _execute("INSERT INTO current_focus (title, source, status) "
             "VALUES (?, 'explicit', 'active')", (title,))
    rows = _scalar_rows("SELECT id FROM current_focus WHERE title = ?", (title,))
    assert rows, f"重点夹具没插进去：{title}"
    focus_id = int(rows[-1][0])
    _created_focus.append(focus_id)
    return focus_id


def _db_fingerprint() -> Dict[str, List[tuple]]:
    """逐表整行内容（只数行数抓不到原地 UPDATE，所以按整行取回）"""
    conn = md.get_connection()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()]
        out: Dict[str, List[tuple]] = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = [tuple(r) for r in rows]
        return out
    finally:
        conn.close()


def _reset() -> None:
    """每条用例从同一起点开始：只删本文件夹具记下的那几行"""
    if _created_tasks:
        _execute("DELETE FROM goal_tasks WHERE id IN ("
                 + ",".join("?" * len(_created_tasks)) + ")", tuple(_created_tasks))
        _created_tasks.clear()
    if _created_goals:
        _execute("DELETE FROM goals WHERE goal_id IN ("
                 + ",".join("?" * len(_created_goals)) + ")", tuple(_created_goals))
        _created_goals.clear()
    if _created_focus:
        _execute("DELETE FROM current_focus WHERE id IN ("
                 + ",".join("?" * len(_created_focus)) + ")", tuple(_created_focus))
        _created_focus.clear()
    if _created_legacy_tasks:
        _execute("DELETE FROM tasks WHERE id IN ("
                 + ",".join("?" * len(_created_legacy_tasks)) + ")",
                 tuple(_created_legacy_tasks))
        _created_legacy_tasks.clear()


# ==================== 读取摘要文本的小工具 ====================

def _cm() -> ContextManager:
    """带 goal_manager 的 ContextManager——生产里 `runtime.py:82` 就是这么构造的"""
    return ContextManager(goal_manager=gm)


def _segment(today: str = _TODAY, goal_id: Optional[str] = None,
             detail_level: str = "high") -> str:
    """直取摘要段（不整份提示词），A 组与 B.5 都读它"""
    return _cm()._build_work_context(goal_id=goal_id, today=today,
                                     detail_level=detail_level)


def _work_block_or_none(prompt: str) -> Optional[str]:
    """从整份 system prompt 里切出摘要段；没有这一段返回 None（负控时就会 None）"""
    idx = prompt.find(_WORK_HEADER)
    if idx < 0:
        return None
    tail = prompt[idx:]
    end = tail.find("\n\n")
    return tail if end < 0 else tail[:end]


def _work_block(prompt: str) -> str:
    block = _work_block_or_none(prompt)
    assert block is not None, f"提示词里没有「{_WORK_HEADER}」那一段"
    return block


def _column(block: str, prefix: str) -> Optional[str]:
    """取摘要段里以 `prefix` 开头的那一行；没有这一行返回 None"""
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line
    return None


def _listed_items(line: str, prefix: str) -> List[str]:
    """把一栏里列出来的标题拆成列表（超限句先削掉，再按顿号拆）"""
    body = line[len(prefix):].split("（共 ")[0]
    return [part.strip() for part in body.split("、") if part.strip()]


def _first_title(line: str, prefix: str) -> str:
    """某栏列出的第一条，尾部那对括号（状态／原排期日期）一起削掉"""
    items = _listed_items(line, prefix)
    first = items[0] if items else ""
    if first.endswith("）") and "（" in first:
        first = first[:first.rfind("（")].strip()
    return first


# ==================== C 组：确定性桩，零网络 ====================

class _BoomManager:
    """任何一条查询都抛：验「读失败」与「空态」必须可分（A.6）"""

    def get_tasks_planned_for(self, plan_date):
        raise RuntimeError("夹具：工作台读不出来")

    def get_overdue_tasks(self, today):
        raise RuntimeError("夹具：工作台读不出来")

    def list_goals(self, status=None):
        raise RuntimeError("夹具：工作台读不出来")

    def get_goal(self, goal_id):
        raise RuntimeError("夹具：工作台读不出来")


class _StubModelPlugin(ModelPlugin):
    """确定性「模型」：答案**只从 system prompt 里读**，读不到就如实说读不到

    三条 C 用例的牙就在这格——把注入摘掉，这里立刻引用不到真实标题（spec §5 D 要求
    C 与 A.1、B.1 一起变红）。措辞不追求像人说话，只求可判定；它不调工具、不打网络、
    每次只产出一个整块（`runtime.py:232` 那一句「最后一块即全文」因此天然成立）。
    """

    def __init__(self):
        self.system_prompts: List[str] = []

    def load(self, plugin_info):
        self.info = plugin_info

    def unload(self):
        pass

    def stream_chat(self, messages, **kwargs):
        yield self._reply(messages)

    def stream_chat_with_tools(self, messages, tools, **kwargs):
        yield self._reply(messages)

    def sync_chat(self, messages, **kwargs):
        return self._reply(messages)

    def _reply(self, messages: List[Dict[str, Any]]) -> str:
        system = ""
        for msg in messages:
            if msg.get("role") == "system":
                system = str(msg.get("content") or "")
        self.system_prompts.append(system)
        return _stub_answer(system)


class _StubPluginManager:
    """只回答 runtime 要的两类插件：model＝上面那个桩，tool＝空

    空工具集 ⇒ `runtime.py:217` 那个分支走 `stream_chat`，不碰工具链。
    不往 `PluginManager` 的私有注册表里塞东西、也不在 data/ 下造临时插件目录：C 组要的
    是「runtime 的接线真的走到桩上」，插件发现机制本身由 `tests/test_skeleton.py` 覆盖。
    """

    def __init__(self, model_plugin: ModelPlugin):
        self._model_plugin = model_plugin

    def get_plugins_by_type(self, plugin_type: str):
        return [self._model_plugin] if plugin_type == "model" else []


def _fake_supervisor_decision(user_input: str) -> Dict[str, Any]:
    """把每轮必走的决策钉成 medium：本批要验的正是五栏那一档"""
    return {"task": "规划", "domains": ["通用"], "difficulty": "中级",
            "agent": ["通用助手"]}


@contextlib.contextmanager
def _offline():
    """`supervisor_decision` 桩成定值 ＋ 三条模型咽喉点装哨兵：C 组一次网都不许出

    哨兵不是用来凑断言的：它证明的是「除了本文件的桩，这一轮没有任何路径走到
    `business/model_client.py`」——`core/runtime.py:16` 那句 import 决定了决策的桩必须打在
    `core.runtime` 这一侧，打在 `agents.supervisor` 上换不到运行时的绑定。
    """

    def _tripwire(*args, **kwargs):
        raise AssertionError("C 组不许打真网络：business.model_client 的调用点被装成了哨兵")

    with patch.object(runtime_module, "supervisor_decision",
                      _fake_supervisor_decision), \
            patch.object(model_client, "chat_sync", _tripwire), \
            patch.object(model_client, "stream_chat", _tripwire), \
            patch.object(model_client, "stream_chat_with_tools", _tripwire):
        yield


def _stub_answer(system_prompt: str) -> str:
    """只认摘要段里那两栏的确定性回答（措辞不逐字要求，见 spec §5 顶部那句）"""
    block = _work_block_or_none(system_prompt)
    if block is None:
        # 注入被摘掉 ⇒ 模型手上什么都没有。这一句让 C 组在负控下必然变红。
        return "我这边没有找到你的安排。"
    today = _column(block, "今天排期：")
    overdue = _column(block, "逾期未完成")
    if today is None and overdue is None:
        # §4.1 第三行：证据不足时只问一个必要的问题，不凑建议
        return "这周你想先推进哪件事？"
    lines = []
    if today is not None:
        lines.append("已有安排：" + today[len("今天排期："):])
    if overdue is not None:
        lines.append("逾期未完成：" + overdue[len("逾期未完成 "):].split("：", 1)[-1])
    anchor = _first_title(today, "今天排期：") if today else _first_title(overdue, "逾期未完成 ")
    if anchor:
        lines.append("建议（非安排）：先做「%s」。" % anchor)
    return "\n".join(lines)


def _make_runtime(tag: str) -> Tuple[AgentRuntime, _StubModelPlugin]:
    store = JsonConversationStoreAdapter(
        ChatStore(str(Path(_TMP_DIR) / ("conversations_" + tag))))
    stub = _StubModelPlugin()
    runtime = AgentRuntime(
        conversation_store=store,
        plugin_manager=_StubPluginManager(stub),
        event_bus=EventBus(),
        trace_store=TraceStore(),
        goal_manager=gm,
    )
    return runtime, stub


def _say(runtime: AgentRuntime, session_id: str, text: str) -> str:
    """走真 `process_user_input` 说完一轮，返回整块回答"""
    chunks = list(runtime.run(session_id, text))
    assert chunks, "这一轮一个字都没产出"
    return chunks[-1]


def _conflict_fixture() -> str:
    """spec §3.4 那条冲突夹具，返回**会话绑定**的那个目标 id

    会话目标（AI 项目）／当前焦点（英语）／今天排期（英语任务）**三者互不相同**，
    这正是「不被会话目标带偏」要验的形状。
    """
    ai_goal = _make_goal(_SESSION_GOAL_TITLE)
    _make_task(ai_goal, _SESSION_GOAL_TASK, planned=None)
    eng_goal = _make_goal(_FOCUS_TITLE)
    _make_task(eng_goal, _ENGLISH_TASK, planned=_REAL_TODAY, status="in_progress")
    _make_focus(_FOCUS_TITLE)
    return ai_goal


# ==================== A. 摘要与数据正确性 ====================

def test_wc_a1_real_task_and_focus_reach_the_system_prompt():
    """A.1：真实任务标题与当前焦点标题进到提示词里，且这一段排在所有注入段最前"""
    goal_id = _make_goal(_SESSION_GOAL_TITLE)
    _make_task(goal_id, _ENGLISH_TASK, planned=_REAL_TODAY, status="in_progress")
    _make_focus(_FOCUS_TITLE)

    prompt = _cm().build_system_prompt("英语", _BASE, detail_level="medium",
                                       goal_id=goal_id)
    assert prompt.startswith(_BASE), (
        "基础提示词被顶到后面了：既有三处 `startswith(base)` 断言把「最前」钉成"
        "「紧跟基础提示词之后」，不许插到它前面")
    assert prompt[len(_BASE):].startswith("\n\n" + _WORK_HEADER), (
        "「当前工作现场」没有紧跟在基础提示词之后、先于档案／学习等段")
    block = _work_block(prompt)
    assert _ENGLISH_TASK in block, f"摘要段里没有那条真实任务：{block}"
    assert _FOCUS_TITLE in block, f"摘要段里没有当前焦点：{block}"
    assert WORK_ADVICE_NOTE in block, "摘要段末尾缺「建议（非安排）」那一句口径"
    print("[OK] A.1 真实任务与焦点标题进了提示词，摘要段位置在所有注入段最前")


def test_wc_a2_today_and_overdue_never_impersonate_each_other():
    """A.2：今天栏与逾期栏各自独立，两边的标题都不许出现在对面"""
    goal_id = _make_goal("两栏互不冒充的目标")
    _make_task(goal_id, "今天的单词卡", planned=_TODAY)
    _make_task(goal_id, "昨天的错题整理", planned=_YESTERDAY)

    block = _segment()
    today = _column(block, "今天排期：")
    overdue = _column(block, "逾期未完成")
    assert today is not None and overdue is not None, f"两栏少了一栏：{block}"
    assert "今天的单词卡" in today and "昨天的错题整理" not in today, today
    assert "昨天的错题整理" in overdue and "今天的单词卡" not in overdue, overdue
    print("[OK] A.2 今天栏与逾期栏互不冒充")


def test_wc_a3_classification_keeps_original_date_and_one_group_max():
    """A.3：① 今日只进今天栏；② 逾期只进逾期栏并保留**原始** planned_date；③ 任一任务最多一组

    今日／逾期的条件本身互斥（`planned_date` 不可能既等于又不等于今天，spec §0 那一行），
    所以这里**不构造**「同一条同时进两栏」那种逻辑上不成立的场景，改验三条可判定的事实。
    """
    goal_id = _make_goal("分类与保留日期的目标")
    _make_task(goal_id, "只进今天这一栏", planned=_TODAY)
    _make_task(goal_id, "只进逾期这一栏", planned=_YESTERDAY)
    _make_task(goal_id, "逾期但已完成的那条", planned=_TWO_DAYS_AGO, status="done")
    _make_task(goal_id, "从未排期的那条", planned=None)
    _make_task(goal_id, "排在后天的那条", planned=_TOMORROW)

    block = _segment()
    today = _column(block, "今天排期：") or ""
    overdue = _column(block, "逾期未完成") or ""
    # ①
    assert "只进今天这一栏" in today and "只进今天这一栏" not in overdue, block
    # ② 保留原始排期日期，不自动归到今天
    assert "只进逾期这一栏" in overdue and "只进逾期这一栏" not in today, block
    assert _YESTERDAY in overdue, f"逾期那一行没带原始 planned_date：{overdue}"
    assert _YESTERDAY not in today, f"今天的标题行里混进了往日的日期：{today}"
    # ③ 任一任务最多出现在一组（既不在今天也不在逾期的那三条＝「持续关注」那一侧，本批摘要不列）
    for title in ("逾期但已完成的那条", "从未排期的那条", "排在后天的那条"):
        assert title not in today and title not in overdue, (
            f"这条本不该进摘要的任何一栏：{title}｜{block}")
    for title in ("只进今天这一栏", "只进逾期这一栏"):
        assert not (title in today and title in overdue), f"同一条出现在两栏：{title}｜{block}"
    print("[OK] A.3 分类正确、逾期保留原日期、每任务最多一组")


def test_wc_a4_each_column_caps_and_shares_one_overflow_format():
    """A.4：四栏各自的上限（5／3／5／3）＋**同一句**超限格式，逾期的 M 与 K 分写"""
    goal_id = _make_goal("超限夹具的目标")
    for i in range(WORK_TODAY_CAP + 1):          # 6 条排今天
        _make_task(goal_id, f"今日排期第{i + 1}号", planned=_TODAY)
    for planned, label in ((_YESTERDAY, "甲"), (_TWO_DAYS_AGO, "乙"),
                           ("2026-10-19", "丙"), (_THREE_DAYS_AGO, "丁")):
        _make_task(goal_id, f"逾期任务{label}号", planned=planned)
    for i in range(WORK_GOAL_CAP):                # 再加 5 条 ⇒ 活跃目标共 6
        _make_goal(f"活跃目标第{i + 1}号")
    _make_focus("重点第一条")
    _make_focus("重点第二条")
    _make_focus("重点第三条")
    _force_focus_beyond_cap("重点第四条")          # 第 4 条只能裸 SQL 插（上限是写路径校验）

    block = _segment(goal_id=goal_id)
    columns = (("当前焦点：", WORK_FOCUS_CAP, WORK_FOCUS_CAP + 1),
               ("今天排期：", WORK_TODAY_CAP, WORK_TODAY_CAP + 1),
               ("活跃目标：", WORK_GOAL_CAP, WORK_GOAL_CAP + 1))
    for prefix, cap, total in columns:
        line = _column(block, prefix)
        assert line is not None, f"少了「{prefix}」这一栏：{block}"
        matched = _OVERFLOW_RE.search(line)
        assert matched, f"这一栏的超限句不是统一格式：{line}"
        assert (int(matched.group(1)), int(matched.group(2)), int(matched.group(3))) \
            == (total, cap, total - cap), f"这一栏的三个数不对：{line}"
        assert len(_listed_items(line, prefix)) == cap, f"这一栏列出的条数超了上限：{line}"

    # 逾期那一栏：总数写在标题里、展示数写在超限句里，两个数分写、不许混成一个
    overdue = _column(block, "逾期未完成")
    assert overdue is not None, f"少了逾期那一栏：{block}"
    assert overdue.startswith("逾期未完成 4 条："), overdue
    matched = _OVERFLOW_RE.search(overdue)
    assert matched and (int(matched.group(1)), int(matched.group(2)),
                        int(matched.group(3))) == (4, WORK_OVERDUE_CAP, 1), overdue
    assert len(_listed_items(overdue, "逾期未完成 4 条：")) == WORK_OVERDUE_CAP, overdue
    # 「最近」＝按 planned_date 降序：最旧那条（2026-10-16）应当没被列出，另两条在
    assert _THREE_DAYS_AGO not in block, f"最旧的一条逾期被列出来了：{overdue}"
    assert _YESTERDAY in overdue and _TWO_DAYS_AGO in overdue, overdue
    print("[OK] A.4 四栏上限各自成立、超限句同一格式、逾期总数与展示数分写")


def test_wc_a5_empty_states_are_verbatim_and_never_say_zero():
    """A.5：四句空态逐字照 spec §2.2，且**不出现**「0 条」这种写法"""
    block = _segment()
    assert "（未设置当前焦点）" in block, block
    assert "今天没有排期。" in block, block
    assert "（没有逾期未完成）" in block, block
    assert "（没有进行中的目标）" in block, block
    assert "0 条" not in block, f"空态里冒出了「0 条」：{block}"
    assert "未列" not in block, f"没数据的一栏不该带超限句：{block}"
    print("[OK] A.5 四句空态逐字成立、无「0 条」")


def test_wc_a6_read_failure_is_not_reported_as_empty():
    """A.6：goal_manager 为 None、或查询抛 ⇒ 只写那一句读失败，绝不回落成空态（§2.4）"""
    empty_states = ("（未设置当前焦点）", "今天没有排期。",
                    "（没有逾期未完成）", "（没有进行中的目标）")
    cases = (("goal_manager 为 None", ContextManager()),
             ("查询抛异常", ContextManager(goal_manager=_BoomManager())))
    for label, manager in cases:
        text = manager._build_work_context(today=_TODAY)
        assert _WORK_HEADER in text, f"{label}：连段标题都没了：{text}"
        assert WORK_READ_FAIL in text, f"{label}：没写那句读失败：{text}"
        for empty in empty_states:
            assert empty not in text, f"{label}：读失败被写成了空态「{empty}」：{text}"
        assert "0 条" not in text, f"{label}：读失败被写成了计数：{text}"
    print("[OK] A.6 读失败与空态可分（None 与抛异常两条路都是那一句）")


def test_wc_a7_building_the_prompt_writes_nothing():
    """A.7：调 `build_system_prompt`（含 user_input 那一头）与摘要构建前后逐表整行指纹对拍"""
    goal_id = _make_goal("零写入对拍的目标")
    _make_task(goal_id, "对拍用的今日任务", planned=_REAL_TODAY)
    _make_task(goal_id, "对拍用的往日任务", planned=_YESTERDAY)
    _make_focus(_FOCUS_TITLE)
    _make_legacy_task("对拍用的旧表待办")

    manager = _cm()
    before = _db_fingerprint()
    manager.build_system_prompt("英语", _BASE, detail_level="high", goal_id=goal_id,
                               user_input="今天我应该做什么？")
    manager._build_work_context(goal_id=goal_id, today=_TODAY, detail_level="low")
    manager._build_work_context(goal_id=goal_id, today=_TODAY, detail_level="high")
    after = _db_fingerprint()
    assert after == before, (
        f"摘要构建写了库，差在：{[t for t in after if after[t] != before.get(t)]}")
    print("[OK] A.7 三次构建之后逐表整行指纹一字不变")


# ==================== B. 上下文冲突与兼容性 ====================

def test_wc_b1_summary_survives_a_conflicting_session_goal():
    """B.1：会话目标与焦点不是一件事时，摘要仍把真实任务与焦点送到（不被会话目标替代）"""
    ai_goal = _conflict_fixture()

    prompt = _cm().build_system_prompt("英语", _BASE, detail_level="medium",
                                       goal_id=ai_goal)
    block = _work_block(prompt)
    assert _ENGLISH_TASK in block, f"摘要段里没有那条英语任务：{block}"
    assert _FOCUS_TITLE in block, f"摘要段里没有焦点「{_FOCUS_TITLE}」：{block}"
    assert _SESSION_GOAL_TITLE in block, (
        f"会话范围那一行应当出现会话目标（它只是范围标签，不是唯一背景）：{block}")
    assert "不是唯一背景" in block, f"会话范围少了那句刹车：{block}"
    print("[OK] B.1 会话目标与焦点冲突时摘要仍以工作台为准")


def test_wc_b2_session_goal_task_list_is_no_longer_injected():
    """B.2：会话目标下的待办列表不再默认混入（此条与摘要存在与否无关，不用于负控）"""
    ai_goal = _make_goal(_SESSION_GOAL_TITLE)
    _make_task(ai_goal, _SESSION_GOAL_TASK, planned=None)

    prompt = _cm().build_system_prompt("英语", _BASE, detail_level="high",
                                       goal_id=ai_goal)
    assert _SESSION_GOAL_TASK not in prompt, f"会话目标的待办又混进上下文了：{prompt}"
    assert "### 📌 目标任务" not in prompt, "旧的目标任务列表标题还在提示词里"
    assert "## 🎯 当前目标" not in prompt, "会话目标那一整段又被注了一遍（重复注入）"
    # 函数体保留不删：直接调它仍能拼出那一段，本批只停调用
    assert _SESSION_GOAL_TASK in _cm()._build_goal_context(ai_goal), (
        "_build_goal_context 的函数体被改坏了——本批只许停调用，不许删")
    print("[OK] B.2 会话目标的待办列表不再注入，函数体仍在")


def test_wc_b3_legacy_tasks_table_is_no_longer_injected():
    """B.3：旧 `tasks` 表的待办不再进上下文；函数与那张表都保留"""
    title = "旧表待办：整理高三相册"
    _make_legacy_task(title)

    prompt = _cm().build_system_prompt("英语", _BASE, detail_level="high")
    assert title not in prompt, f"旧表待办仍被注入：{prompt}"
    assert "## 📋 待完成任务" not in prompt, "旧待办段的标题还在"
    assert title in _cm()._build_tasks("英语"), "_build_tasks 函数体被动过（本批只停调用）"
    assert _scalar_rows("SELECT id FROM tasks WHERE title = ?", (title,)), (
        "旧表被清空了——本批不迁移、不删数据")
    print("[OK] B.3 旧 tasks 待办停注入，函数与表都还在")


def test_wc_b4_legacy_tables_and_columns_are_untouched():
    """B.4：`tasks`／`daily_plans`／`daily_reviews` 三表仍在，`goal_tasks` 列集合一字不变"""
    names = {row[0] for row in _scalar_rows(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in ("tasks", "daily_plans", "daily_reviews"):
        assert table in names, f"三表里少了 {table}：{sorted(names)}"
    goal_cols = tuple(r[1] for r in _scalar_rows("PRAGMA table_info(goal_tasks)"))
    assert goal_cols == _GOAL_TASK_COLUMNS, (
        f"goal_tasks 的列与本批基准不一致（本批不许迁移）：{goal_cols}")
    legacy_cols = tuple(r[1] for r in _scalar_rows("PRAGMA table_info(tasks)"))
    assert legacy_cols == _LEGACY_TASK_COLUMNS, (
        f"旧表 tasks 的列与本批基准不一致：{legacy_cols}")
    # 「不删函数」这一条边界就近钉在这里：两个停用的仍挂在类上，新增的那个也在
    for symbol in ("_build_goal_context", "_build_tasks", "_build_work_context"):
        assert callable(getattr(ContextManager, symbol, None)), (
            f"ContextManager 上少了 {symbol}——本批只停调用，不删函数")
    print("[OK] B.4 三表保留、两表列集合与现读基准一致（零迁移）")


def test_wc_b5_low_level_sends_exactly_focus_and_today():
    """B.5：low 档只送两栏，且 low 之后没有任何段把会话范围／逾期／活跃目标补回来

    这一条两半都要跑，所以夹具用 `_REAL_TODAY`：直测那一半把同一个值显式传进去，
    走 `build_system_prompt` 那一半只能自己现算——两条路必须落在同一个「今天」上，
    否则测的是档位分支却红在日期上。
    """
    goal_id = _make_goal("档位夹具的目标")
    _make_task(goal_id, "低档也要看见的今天任务", planned=_REAL_TODAY)
    _make_task(goal_id, "低档不该看见的逾期任务", planned=_YESTERDAY)
    _make_focus(_FOCUS_TITLE)

    block = _segment(today=_REAL_TODAY, goal_id=goal_id, detail_level="low")
    assert "当前焦点：" in block and "今天排期：" in block, block
    for absent in ("逾期未完成", "活跃目标：", "本轮会话范围"):
        assert absent not in block, f"low 档里冒出了「{absent}」：{block}"

    prompt = _cm().build_system_prompt("英语", _BASE, detail_level="low", goal_id=goal_id)
    assert prompt[len(_BASE):].startswith("\n\n" + _WORK_HEADER)
    low_block = _work_block(prompt)
    assert "低档也要看见的今天任务" in low_block, low_block
    assert "本轮会话范围" not in prompt, "low 档的后续段把会话范围补回来了（spec §6.4 要防的正是这一格）"
    assert "逾期未完成" not in low_block and "活跃目标：" not in low_block, low_block
    print("[OK] B.5 low 档恰两栏，且后续段没有补回会话范围")


def test_wc_b6_no_new_tools_and_no_workbench_write_tool():
    """B.6：工具集合与现读基准**完全相等**（没有新增），里面也没有任何写工作台的动作"""
    names = {str((item.get("function") or {}).get("name") or "")
             for item in TOOLS_DEFINITIONS}
    assert names == _BASELINE_TOOL_NAMES, (
        f"工具集合与本批基准不一致（本批不注册任何新工具）：{sorted(names)}")
    workbench_writers = ("add_task", "update_task_status", "set_task_planned_date",
                         "set_task_home", "create_goal", "save_manual_section")
    hit = [name for name in workbench_writers if name in names]
    assert not hit, f"出现了写工作台的工具：{hit}"
    print("[OK] B.6 六个工具一字未增删，且没有写工作台的工具")


# ==================== C. 实际对话行为（确定性桩，不要求措辞逐字）====================

def test_wc_c1_first_turn_answers_from_the_real_task():
    """C.1：问「今天我应该做什么？」⇒ 回答引用真实英语任务，不重拟计划、不喊读不到"""
    ai_goal = _conflict_fixture()
    runtime, stub = _make_runtime("c1")

    with _offline():
        runtime.set_active_goal("wc_c1", ai_goal)
        answer = _say(runtime, "wc_c1", "今天我应该做什么？")

    assert stub.system_prompts, "桩没被调到：这一轮根本没走到模型"
    assert _ENGLISH_TASK in answer, (
        f"回答里没有那条真实任务（摘掉注入时红点正落这一格）：{answer}")
    assert "没有找到" not in answer and "拟一份" not in answer, (
        f"回答退回成「读不到／另拟一份」那一类：{answer}")
    assert _SESSION_GOAL_TASK not in answer, f"回答被会话目标的任务带偏了：{answer}"
    print("[OK] C.1 第一轮回答引用真实任务，不重拟")


def test_wc_c2_existing_arrangements_and_advice_are_separately_readable():
    """C.2：「已有安排」与「建议（非安排）」分两段，不混写（§4.2）"""
    ai_goal = _conflict_fixture()
    runtime, _stub = _make_runtime("c2")

    with _offline():
        runtime.set_active_goal("wc_c2", ai_goal)
        answer = _say(runtime, "wc_c2", "今天我应该做什么？")

    lines = [line for line in answer.split("\n") if line.strip()]
    arranged = [line for line in lines if line.startswith("已有安排：")]
    advice = [line for line in lines if line.startswith("建议（非安排）：")]
    assert arranged and advice, f"两段少了一整段：{answer}"
    assert len(arranged) == 1 and len(advice) == 1, f"段数不对：{answer}"
    assert _ENGLISH_TASK in arranged[0], arranged[0]
    assert _ENGLISH_TASK in advice[0], advice[0]
    assert "已有安排" not in advice[0], f"建议行里混写了既定计划：{advice[0]}"
    print("[OK] C.2 已有安排与建议（非安排）分行且不互混")


def test_wc_c3_follow_up_stays_on_the_same_real_task():
    """C.3：追问「这条任务怎么办？」⇒ 仍围绕**同一条**真实任务，不要求用户重新粘贴"""
    ai_goal = _conflict_fixture()
    runtime, stub = _make_runtime("c3")

    with _offline():
        runtime.set_active_goal("wc_c3", ai_goal)
        first = _say(runtime, "wc_c3", "今天我应该做什么？")
        second = _say(runtime, "wc_c3", "这条任务怎么办？")

    assert _ENGLISH_TASK in first and _ENGLISH_TASK in second, (
        f"两轮不是同一条任务：第一轮 {first}｜第二轮 {second}")
    assert "复制粘贴" not in second and "没有找到" not in second, (
        f"第二轮把工作台要用户重贴了一遍：{second}")
    assert len(stub.system_prompts) == 2, "两轮没有各自都带上系统提示词"
    for prompt in stub.system_prompts:
        assert _ENGLISH_TASK in _work_block(prompt), "第二轮的上下文里摘要丢了"
    assert _SESSION_GOAL_TASK not in second, f"第二轮被会话目标带偏：{second}"
    print("[OK] C.3 追问仍围绕同一条真实任务，上下文接得上")


TESTS = [
    test_wc_a1_real_task_and_focus_reach_the_system_prompt,
    test_wc_a2_today_and_overdue_never_impersonate_each_other,
    test_wc_a3_classification_keeps_original_date_and_one_group_max,
    test_wc_a4_each_column_caps_and_shares_one_overflow_format,
    test_wc_a5_empty_states_are_verbatim_and_never_say_zero,
    test_wc_a6_read_failure_is_not_reported_as_empty,
    test_wc_a7_building_the_prompt_writes_nothing,
    test_wc_b1_summary_survives_a_conflicting_session_goal,
    test_wc_b2_session_goal_task_list_is_no_longer_injected,
    test_wc_b3_legacy_tasks_table_is_no_longer_injected,
    test_wc_b4_legacy_tables_and_columns_are_untouched,
    test_wc_b5_low_level_sends_exactly_focus_and_today,
    test_wc_b6_no_new_tools_and_no_workbench_write_tool,
    test_wc_c1_first_turn_answers_from_the_real_task,
    test_wc_c2_existing_arrangements_and_advice_are_separately_readable,
    test_wc_c3_follow_up_stays_on_the_same_real_task,
]


def main():
    try:
        for fn in TESTS:
            _reset()
            fn()
    finally:
        _reset()
        cleanup_test_db()


if __name__ == "__main__":
    main()
