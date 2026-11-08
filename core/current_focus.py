# -*- coding: utf-8 -*-
"""core.current_focus —— 「当前重点」的存储与确定性解析（P7 阶段二 组 5 子批 A，2026-10-18）

职责（只做下面这几件，别的都不做）：
- 写：把一行 `current_focus` 立起来、改标题／说明、推进它的状态；
- 读：按 id／按状态取回，供首页「当前重点」那一格渲染；
- 匹配：`matches_focus_title` 是全项目**唯一**那把「这两条说法是不是同一件事」的粗尺
  （归一化后相等或互为子串，不做分词／同义词／评分）。子批 B（2026-10-19）把它从候选
  函数的内联判断里**抽出来**，因为首页两类卡片的排序也要用它——同一把尺只许有一处实现；
- 解析：把人说的「我现在做 X／我不做 Y 了」**确定地**读成一份变更计划，
  再由 `build_focus_preview` 拼成**预览**，`apply_focus_preview` 才真的写；
- 建议：`suggest_focus_candidates` 从近期活动里挑候选（**只读、零写**）。

四条按实写下的口径（每条都有用例钉，见 `tests/test_current_focus.py`）：

1. **active 最多 3 个是写路径校验，不靠 DDL**（判据①）。超限直接 `raise ValueError`，
   不是返回 False——返回 False 会让调用方以为「库里本来就有 3 条、我这条没写也合理」，
   而界面上那一格会安静地停在 3 条不变，人看不出是被拒还是没点着。
   唯一能改这一档的地方是 `MAX_ACTIVE_FOCUS` 这一个常量。
2. **paused／completed 从不删行**（边界：本模块没有任何 DELETE）。暂停与完成是状态推进，
   「上个月停过哪件事」是要能回读的事实。
3. **推断候选不自动成为正式重点**（判据③）。`suggest_focus_candidates` 只返回列表、
   一行都不写；只有人在界面上点「采纳」才落库，落库时 `source='inferred'` **并带上它的
   evidence 与 confidence**——这一档的写函数会**拒掉缺这两格的行**，所以「候选不带
   evidence／confidence 就落库」在形状上就不可能。
4. **含糊表达不覆盖已有重点**（判据：解析不确定就不动）。`parse_focus_intent` 只认
   下面词表里那几种「明确说法」；认不出、或对不上、或对上一条以上，返回的 items 都是空的，
   只有 note 说明为什么没得写。**解析与预览全程零写库**，写只发生在 `apply_focus_preview`。

层序与副作用：本模块是 core=0 层的叶子，只 import `memory.database.get_db` 与本项目的
`core.decision_store`（取状态常量，不复制词表）。它不调模型、不起线程、不渲染界面。
自然语言那一头为什么**不走 `agents.supervisor`**：层序表里 agents=2 不许被 core 引
（`checks/check_import_direction.py`），而 `supervisor_decision` 是每轮对话都要走的路径
（见 `agents/supervisor.py` 头部那段「立函数不接线」的定案）——为一句话的重点变更去加
一次每轮必付的模型调用，是拿全项目的延迟换一个正则表达式能做的事。

时间戳照 `goals`／`goal_tasks` 那一轴（`CURRENT_TIMESTAMP`，UTC、不带偏移），
不照 `core/decision_store.py` 的 ISO+00:00 那一轴。全项目此刻**四根**轴并存，出处是
`PROJECT_STATUS.md` §五.49（不是 §五.64——本席 2026-10-18 首版注释把根数说成两根、
出处指成 §五.64，复跑 grep 后当场更正，注释保留这段自犯）；本模块只碰前两根、
不新增第五根，也不新写一种截法。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from memory.database import get_db

# 同层引用（core=0 → core=0）：只取它的状态常量，不在这里复制一份「等你定」的词表。
from core import decision_store as ds

# ==================== 词表与上限 ====================

#: 来源两值。**explicit**＝人自己说出来的；**inferred**＝候选被人采纳后落的那一行。
#: 没有第三档，也不该有：这一格回答的是「这条重点怎么来的」，不是「它重不重要」。
SOURCE_EXPLICIT = "explicit"
SOURCE_INFERRED = "inferred"
VALID_FOCUS_SOURCES = (SOURCE_EXPLICIT, SOURCE_INFERRED)

#: 状态三值。paused 与 completed 都只是状态，**不是删除**（见模块 docstring 第 2 条）。
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
VALID_FOCUS_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED)

#: active 的上限（判据①）。写在函数里而不是 CHECK 里，理由见 DDL 注释第 ① 条。
MAX_ACTIVE_FOCUS = 3

#: 候选的默认窗口与条数上限。窗口取 7 天是因为首页那句「现在最值得关注的几件事」
#: 问的是这几天；上限 3 是照首页那一格最多显示三条的既有形状（`CARD_CAP` 同档）。
CANDIDATE_WINDOW_DAYS = 7
CANDIDATE_CAP = 3

#: 置信度的那把粗尺（`_coarse_confidence`）。起点 0.4、每条近期活动 +0.1、封顶 0.8。
#: ⚠️ 它是**同一把尺量出来的相对数**，不是概率，也不是「模型认为有多可能」——
#: 界面上要说「这是按近期活动条数给的粗量」，不能只印一个 0.5 让人以为谁算过。
CONFIDENCE_BASE = 0.4
CONFIDENCE_STEP = 0.1
CONFIDENCE_CAP = 0.8

#: 候选的两种来路（卡片文案与测试按它分头断言，不数总数）。
CANDIDATE_KIND_TASKS = "recent_tasks"
CANDIDATE_KIND_DECISIONS = "recent_decisions"


# ==================== 写路径 ====================

def normalize_focus_title(text: Any) -> str:
    """比对用的归一化：去空白、去常见中文标点、转小写。

    只用于**判断两条说法是不是同一件事**，不用于存储或显示（存的是原话）。
    刻意不做词形还原、不做拼音、不做同义词：那样一来「对得上」就成了猜，
    而这一模块的判据是「不确定就不动」。
    """
    raw = str(text or "").strip().lower()
    for ch in (" ", "\t", "\n", "，", "。", "、", "：", "；", "！", "？", "「", "」",
               "『", "』", "（", "）", "(", ")", ".", ",", "!", "?", ":", ";"):
        raw = raw.replace(ch, "")
    return raw


def _confidence_for_write(source: str, confidence: Any, evidence: Any) -> Tuple[Optional[float], Optional[str]]:
    """把 inferred 那一档的「必须带出处」这条门槛落成一次校验。

    Returns:
        (可信的 confidence, 可信的 evidence)；explicit 时原样返回 (None, None)。
    Raises:
        ValueError —— source 是 inferred 而 confidence 缺失／不是 0~1 的数，
        或 evidence 是空的。判据③ 的第一半就拦在这里。
    """
    if source != SOURCE_INFERRED:
        return None, None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError(
            f"source='inferred' 必须带一个 0~1 的 confidence，收到 {confidence!r}："
            "推断来的重点不写明它凭什么在这儿，就不该进账")
    value = float(confidence)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"confidence 要在 0~1 之间，收到 {value!r}")
    text = str(evidence or "").strip()
    if not text:
        raise ValueError(
            "source='inferred' 必须带 evidence（证据摘要，写明来源与时间），"
            "空证据的推断不落库")
    return value, text


def create_focus(title: str, description: Optional[str] = None,
                 source: str = SOURCE_EXPLICIT, confidence: Any = None,
                 evidence: Any = None, expires_at: Optional[str] = None,
                 status: str = STATUS_ACTIVE) -> int:
    """新增一行当前重点，返回新 id。

    Args:
        title: 原话标题（必填、非空）。
        description: 为什么是它、做到什么算完（可空）。
        source: 'explicit'／'inferred'（见 `VALID_FOCUS_SOURCES`）。
        confidence／evidence: **inferred 必填**（缺或非法直接 raise，判据③）；
            explicit 时这两格被忽略、存 NULL——人自己说的事不需要系统给出处。
        expires_at: 可空的 'YYYY-MM-DD'；本模块只存它、显示它，**不据它自动改状态**
            （到期就暂停属于「自动行动」，不在子批 A 的边界里）。
        status: 建档时的状态，默认 active。
    Raises:
        ValueError —— 标题为空／source 或 status 不在词表／inferred 缺出处／
            active 已达 `MAX_ACTIVE_FOCUS`（判据①）。
    """
    clean = str(title or "").strip()
    if not clean:
        raise ValueError("当前重点的标题不能是空的")
    if source not in VALID_FOCUS_SOURCES:
        raise ValueError(
            f"source 只能是 {VALID_FOCUS_SOURCES} 之一，收到 {source!r}")
    if status not in VALID_FOCUS_STATUSES:
        raise ValueError(
            f"status 只能是 {VALID_FOCUS_STATUSES} 之一，收到 {status!r}")
    conf, evid = _confidence_for_write(source, confidence, evidence)
    if status == STATUS_ACTIVE and count_active_focus() >= MAX_ACTIVE_FOCUS:
        raise ValueError(
            f"active 的当前重点最多 {MAX_ACTIVE_FOCUS} 条，已经满了："
            "要先暂停或完成一条，这里不替你决定动哪一条")
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO current_focus (title, description, source, status, "
            "confidence, evidence, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clean, str(description).strip() if description else None,
             source, status, conf, evid, expires_at))
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def update_focus(focus_id: int, title: Optional[str] = None,
                 description: Optional[str] = None) -> bool:
    """改一条重点的标题／说明（「编辑」那一格）。None＝这一格不动，空串＝清空说明。

    不碰 source：一条重点**怎么来的**是它的来历，改标题不等于来历从 inferred 变成
    explicit。也不碰 status——推进状态走 `set_focus_status`，两处都能改会让「谁把它暂停的」
    对不上账。

    Returns:
        True＝改了；False＝库里没有这一条（不是规则违规，调用方本来就要处理）。
    Raises:
        ValueError —— 显式传了 title 但去掉空白后是空的。
    """
    clean: Optional[str] = None
    if title is not None:
        clean = str(title).strip()
        if not clean:
            raise ValueError("当前重点的标题不能改成空的")
    conn = get_db()
    try:
        cursor = conn.execute(
            "UPDATE current_focus SET title = COALESCE(?, title), "
            "description = COALESCE(?, description), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (clean, description, int(focus_id)))
        conn.commit()
        return (cursor.rowcount or 0) > 0
    finally:
        conn.close()


def set_focus_status(focus_id: int, status: str) -> bool:
    """把一条重点推进到 status（active／paused／completed）。

    暂停与完成**只改状态、不删行**（模块 docstring 第 2 条）。往 active 推时同样要过
    `MAX_ACTIVE_FOCUS` 那道闸：恢复第 4 条重点和新增第 4 条是同一件事，不能因为
    它「以前就在」就放行。

    Returns:
        True＝改了；False＝库里没有这一条。
    Raises:
        ValueError —— status 不在词表，或 active 已满。
    """
    if status not in VALID_FOCUS_STATUSES:
        raise ValueError(
            f"status 只能是 {VALID_FOCUS_STATUSES} 之一，收到 {status!r}")
    if status == STATUS_ACTIVE:
        current = get_focus(focus_id)
        if current and current.get("status") != STATUS_ACTIVE \
                and count_active_focus() >= MAX_ACTIVE_FOCUS:
            raise ValueError(
                f"active 的当前重点最多 {MAX_ACTIVE_FOCUS} 条，已经满了："
                f"要恢复 #{int(focus_id)}，请先暂停或完成一条")
    conn = get_db()
    try:
        cursor = conn.execute(
            "UPDATE current_focus SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (status, int(focus_id)))
        conn.commit()
        return (cursor.rowcount or 0) > 0
    finally:
        conn.close()


# ==================== 读路径 ====================

def get_focus(focus_id: int) -> Optional[Dict[str, Any]]:
    """按 id 取回一条（没有返回 None，不抛）"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM current_focus WHERE id = ?", (int(focus_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_focus(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出重点行；status 给定就只取那一档。

    排序是**定死的两键**：active 在最前，其余按 `updated_at` 倒序、并列按 id 倒序。
    为什么要给 active 单独一层：那一格是首页这一屏要人看的，让一条刚被暂停的行
    因为时间戳新就排在最前，界面上会读成「这还是我现在的事」。
    两键都确定 ⇒ 连点两次刷新不会换顺序（口径同首页那三区）。
    """
    conn = get_db()
    try:
        if status is None:
            rows = conn.execute(
                "SELECT * FROM current_focus ORDER BY "
                "CASE WHEN status = ? THEN 0 ELSE 1 END, "
                "updated_at DESC, id DESC", (STATUS_ACTIVE,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM current_focus WHERE status = ? ORDER BY "
                "updated_at DESC, id DESC", (status,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_active_focus() -> int:
    """现在有几条 active（写路径那道闸读的就是这一格）"""
    conn = get_db()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM current_focus WHERE status = ?",
            (STATUS_ACTIVE,)).fetchone()[0])
    finally:
        conn.close()


# ==================== 粗匹配（全项目唯一的一把「是不是同一件事」的尺）====================

def active_focus_titles() -> List[str]:
    """现在 active 的那几条重点的**标题原话**（顺序照 `list_focus`，不做加工）。

    给读侧用（首页排序、候选跳过），不写库、不调模型。返回的是原样字符串，
    归一化留给 `matches_focus_title`——存的是人说的话，加工只发生在比对那一刻。
    """
    return [str(row.get("title") or "").strip() for row in list_focus(status=STATUS_ACTIVE)]


def matches_focus_title(text: Any, focus_texts: List[Any]) -> bool:
    """一条说法与那一串重点里**任何一条**是否粗匹配对上（子批 B，2026-10-19）。

    谓词是子批 A 候选函数里那两行内联判断**原样抽出来的**：两边都过
    `normalize_focus_title`，然后「相等／互为子串」任一成立即算对上。**没有加东西**——
    不做分词、不做同义词、不做评分（锚点 §四 组 5 第 3 条「不做复杂评分」）。

    为什么要抽成函数而不是让首页自己再写一遍这三行：这一把尺现在是**两个**消费方共用
    （`suggest_focus_candidates` 用它跳过「人已经说过的」，首页两类卡片用它排序），
    而「两处各写一遍同一个谓词」正是本项目被咬过的那个形状——一处改了另一处不改，
    界面上的「与当前重点相关」就和候选里那句「跳过」悄悄不是同一件事了。
    口径同 `home_controller._home_task_rows` 的 docstring。

    Args:
        text: 要比的那一条说法（任务标题、机会标题……）。
        focus_texts: 那一串重点标题（通常是 `active_focus_titles()` 的返回；给空列表
                     就恒为 False ⇒ 「没有 active 重点时卡片顺序一字不变」是这条谓词的
                     直接后果，不靠调用方另写 if）。
    Returns:
        True／False。两边任意一边归一化后为空串 ⇒ False（空标题不配「匹配」这个词，
        否则一条空重点会把所有说法都说成相关）。
    """
    key = normalize_focus_title(text)
    if not key:
        return False
    for focus_text in focus_texts or []:
        existing = normalize_focus_title(focus_text)
        if not existing:
            continue
        if key == existing or key in existing or existing in key:
            return True
    return False


# ==================== 确定性解析（零写库） ====================

#: 说「不做了」的那几种**明确**说法。词表收窄是刻意的：只认「我不做 X 了」这一族，
#: 「这个先放一放吧」这种句子放进来，就会在一次含糊表达之后把一条真重点暂停掉。
_STOP_MARKERS = ("我不再做", "我不做", "先不做", "不再做", "不用做了", "不做了",
                 "不用管")

#: 说「在做」的那几种明确开头。**只按前缀认**：句子中间冒出「现在做」不算，
#: 因为那样「你说你现在做什么来着？」这种问句也会被读成一条指令。
_START_PREFIXES = ("我现在开始做", "我接下来要做", "我接下来要做的是", "我现在要做",
                   "我现在做", "我要开始做", "我开始做", "我现在专注", "我专注",
                   "从今天起我做", "我最近在做")

#: 句末要剥掉的东西（「我不做 X 了」的「了」不是标题的一部分）
_TRAILING_STRIP = ("了", "啦", "吧", "啊", "呀", "。", "！", "～", "~", "，")


def _object_of(text: str, start: int) -> str:
    """取从 start 开始到句末的那段作「对象」，并剥掉句末语气字"""
    tail = str(text or "")[start:].strip()
    changed = True
    while tail and changed:
        changed = False
        for ch in _TRAILING_STRIP:
            if tail.endswith(ch):
                tail = tail[: -len(ch)]
                changed = True
    return tail.strip()


def parse_focus_intent(text: Any) -> Dict[str, Any]:
    """把一句人话确定地读成一份变更计划（**零写库、不调模型**）。

    Returns:
        {"recognized": bool, "action": 'add'|'pause'|None, "object": str,
         "reason": str}
        认不出时 recognized=False、action=None，reason 说清是没认出说法还是句子是空的。
        ⚠️ 这里**不查库、也不决定对得上谁**——那是 `build_focus_preview` 的事。
        分开是因为这一步必须与「账上有几条」无关：同一句话在任何账本上解析结果都该一样，
        否则「含糊表达不覆盖已有重点」这条判据就成了要靠改数据才能测的东西。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"recognized": False, "action": None, "object": "",
                "reason": "那句关于重点的话是空的，什么都不做"}
    for marker in _STOP_MARKERS:
        index = raw.find(marker)
        if index >= 0:
            obj = _object_of(raw, index + len(marker))
            if not obj:
                return {"recognized": False, "action": None, "object": "",
                        "reason": f"听到「{marker}」但没说出是哪件事，"
                                  "不暂停任何一条"}
            return {"recognized": True, "action": "pause", "object": obj,
                    "reason": f"按「{marker}」读到：要暂停「{obj}」"}
    for prefix in _START_PREFIXES:
        if raw.startswith(prefix):
            obj = _object_of(raw, len(prefix))
            if not obj:
                return {"recognized": False, "action": None, "object": "",
                        "reason": f"听到「{prefix}」但没说要做什么，不新增"}
            return {"recognized": True, "action": "add", "object": obj,
                    "reason": f"按「{prefix}」读到：要做的是「{obj}」"}
    return {"recognized": False, "action": None, "object": "",
            "reason": "这句话没落在「我现在做 X」／「我不做 Y 了」这两类明确说法里："
                      "不新增、也不暂停任何一条重点"}


# ==================== 变更预览（零写库） ====================

def build_focus_preview(intent: Dict[str, Any],
                        existing: Optional[List[Dict[str, Any]]] = None
                        ) -> Dict[str, Any]:
    """把一份解析结果对上当前的重点，拼成一份**变更预览**（仍然零写库）。

    Args:
        intent: `parse_focus_intent` 的返回。
        existing: 当前 active 行；不给就自己读（测试传内存列表可以完全脱库）。
    Returns:
        {"summary": str, "items": [{"action", "title", "focus_id", "blocked"}],
         "notes": [str]}
        items 为空就是「这次什么都不会写」。`blocked=True` 的那一条只是**预告**
        写入时会为什么被拒——最终判定仍然只在写路径那一道闸上（判据① 的口径），
        预览不提前替它放行，也不替它决定删哪一条。
    """
    if not isinstance(intent, dict) or not intent.get("recognized"):
        reason = str((intent or {}).get("reason") if isinstance(intent, dict)
                     else "解析结果形状不对")
        return {"summary": "没有可写入的变更", "items": [],
                "notes": [reason, "这一句不会改动任何一条重点"]}
    rows = existing if existing is not None else list_focus(status=STATUS_ACTIVE)
    action = str(intent.get("action") or "")
    obj = str(intent.get("object") or "").strip()
    wanted = normalize_focus_title(obj)
    matched = [row for row in rows
               if wanted and wanted == normalize_focus_title(row.get("title"))]
    if not matched:
        matched = [row for row in rows
                   if wanted and (wanted in normalize_focus_title(row.get("title"))
                                  or normalize_focus_title(row.get("title")) in wanted)]
    notes = [str(intent.get("reason") or "")]
    if action == "pause":
        if not matched:
            return {"summary": "没有对得上的当前重点，什么都不写", "items": [],
                    "notes": notes + [f"active 的这几条里没有「{obj}」——不新建、"
                                      "也不改任何一条（不猜你要说的是哪件）"]}
        if len(matched) > 1:
            ids = "、".join(f"#{int(r['id'])} {r.get('title')}" for r in matched)
            return {"summary": "不止一条对得上，先不动", "items": [],
                    "notes": notes + [f"有 {len(matched)} 条都和「{obj}」对得上（{ids}）："
                                      "请去那几条里点名一条，这里不替你选"]}
        row = matched[0]
        return {"summary": f"准备暂停一条：#{int(row['id'])} {row.get('title')}",
                "items": [{"action": "pause", "title": str(row.get("title") or ""),
                           "focus_id": int(row["id"]), "blocked": False}],
                "notes": notes + ["暂停只改状态，那一行不删（历史还在）"]}
    # add
    if matched:
        row = matched[0]
        return {"summary": "这件事已经在当前重点里，不用新增", "items": [],
                "notes": notes + [f"已有的 #{int(row['id'])} {row.get('title')} "
                                  "标题和这句对得上：不改它的来历、也不复制一条"]}
    blocked = len(rows) >= MAX_ACTIVE_FOCUS
    if blocked:
        notes.append(f"当前已有 {len(rows)} 条 active，上限是 {MAX_ACTIVE_FOCUS} 条："
                     "这一条写入时会被拒，要先暂停或完成一条——预览不替你决定动哪条")
    return {"summary": f"准备新增一条：{obj}",
            "items": [{"action": "add", "title": obj, "focus_id": None,
                       "blocked": blocked}],
            "notes": notes}


#: `apply_focus_preview` 认得的动作。**别的字符串一律拒**（fail-closed）：
#: 预览 dict 会在界面 State 里过一轮往返，谁也不能保证它没被改过。
_PREVIEW_ACTIONS = ("add", "pause")


def apply_focus_preview(preview: Any) -> List[str]:
    """按预览真的写入，返回逐条回执（**唯一会写库的这一步**）。

    写的时候不重读预览里的 `blocked` 那一格——上限的判定只在写路径那一道闸上做一次，
    预览里那个数只是给人看的预告。两道都判会让「预览说行、库里说不行」变成一种可能的状态。
    """
    if not isinstance(preview, dict):
        raise ValueError(f"预览形状不对（应为 dict），收到 {type(preview).__name__}")
    items = preview.get("items") or []
    if not isinstance(items, list):
        raise ValueError("预览里的 items 不是列表，拒绝写入")
    receipts: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"预览里有一条不是对象：{item!r}")
        action = str(item.get("action") or "")
        if action not in _PREVIEW_ACTIONS:
            raise ValueError(f"预览里出现不认识的动作 {action!r}，拒绝写入")
        if action == "add":
            title = str(item.get("title") or "").strip()
            try:
                new_id = create_focus(title)
            except ValueError as e:
                receipts.append(f"没写「{title}」：{e}")
                continue
            receipts.append(f"✅ 已新增当前重点 #{new_id}：{title}")
        else:
            focus_id = item.get("focus_id")
            if focus_id is None:
                raise ValueError("预览里那条要暂停的记录没有 focus_id，拒绝写入")
            ok = set_focus_status(int(focus_id), STATUS_PAUSED)
            title = str(item.get("title") or "")
            receipts.append(
                f"✅ 已暂停 #{int(focus_id)}：{title}（那一行不删，历史还在）" if ok
                else f"⚠️ #{int(focus_id)} 不在库里了（大概被改过）：一行都没动")
    if not receipts:
        return ["这次没有要写入的东西：账本一个字没动"]
    return receipts


# ==================== 推断候选（只读、零写） ====================

def _utc_stamp_text(dt: datetime) -> str:
    """SQLite CURRENT_TIMESTAMP 那轴的文本形状（'YYYY-MM-DD HH:MM:SS'，定宽可字典序比）"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _coarse_confidence(count: int) -> float:
    """那把粗尺：0.4 起步、每条近期活动 +0.1、封顶 0.8。

    ⚠️ 它不是概率，也没有任何东西「算过」它——它是**同一把尺**给的相对数，
    为的是让候选能排序、能让 `source='inferred'` 那一档有非空 confidence 可写。
    界面上必须带上这句说明（见 `ui/controllers/focus_controller.py` 的渲染）。
    """
    return round(min(CONFIDENCE_CAP,
                     CONFIDENCE_BASE + CONFIDENCE_STEP * max(0, int(count))), 2)


def suggest_focus_candidates(window_days: int = CANDIDATE_WINDOW_DAYS,
                             limit: int = CANDIDATE_CAP,
                             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """从近期活动里挑出「要不要把它设成当前重点」的候选（**只读、零写、不调模型**）。

    两种来路（同一种形状，都带 evidence＋confidence）：
    - `recent_tasks`：active 目标在窗口内新建过任务 ⇒ 那个目标本身是候选，
      evidence 数的是窗口内新建的任务条数与最近一条的 id／时刻；
    - `recent_decisions`：窗口内还挂在「等你定」那两档（proposed／discussed）的决策。

    三条不许越过的线：
    1. **不自动成为正式重点**（判据③）：本函数一行都不写，写只在人点「采纳」之后；
    2. **不覆盖已有重点**：标题与任一 active 重点对得上的候选直接跳过（「对得上」由
       `matches_focus_title` 判，全项目只有这一把尺）——
       系统不能拿一个推断去劝人改他自己说过的话；
    3. evidence 里只有**库里已有的事实**（表名／id／时刻／计数），不写推测句。

    Args:
        window_days: 窗口天数（默认 7）。
        limit: 最多返回几条（默认 3，与首页那一格的容量同档）。
        now: 注入的「当下」，只给测试用（默认取 UTC 当下）；窗口是相对时刻，
             不注入就得等真过 7 天才能测出边界。
    Returns:
        [{"title","evidence","confidence","kind"}]，按 confidence 倒序、并列按标题。
    """
    since = (now or datetime.now(timezone.utc)) - timedelta(
        days=max(0, int(window_days)))
    # 两根轴要分别对齐（全项目此刻四根轴并存，登记见 `PROJECT_STATUS.md` §五.49；
    # 本格读到的恰好是前两根，这就是那份「多轴并存」在这里的代价）：
    # `goal_tasks.created_at` 是 CURRENT_TIMESTAMP 那轴（'YYYY-MM-DD HH:MM:SS'），
    # 可以整串字典序比；`decisions.updated_at` 是 ISO+00:00 那轴，第 11 位是 'T'
    # 而不是空格，拿全时间戳去比会让边界那一天多算或少算最多一天——所以那一族
    # 只取前 10 位（两根轴**共有**的那一段）比到「日」。
    cutoff_stamp = _utc_stamp_text(since)
    cutoff_date = since.strftime("%Y-%m-%d")
    # 「对得上」这一判断用的是全项目唯一那把尺（`matches_focus_title`，子批 B 抽出来的），
    # 这里不再自带一份内联谓词——首页排序和候选跳过必须是同一件事。
    active_texts = active_focus_titles()
    out: List[Dict[str, Any]] = []
    seen: set = set()
    conn = get_db()
    try:
        # 来路一：active 目标在窗口内新建过任务。
        # ⚠️ 这里连的是 `goals.goal_id`（TEXT 业务键），不是 `goals.id`——
        # `goal_tasks.goal_id` 存的就是前者；连错了会静默得到空集
        # （本席位 2026-10-18 在这上面翻过一次车，见组 5 开工前事实核查）。
        task_rows = conn.execute(
            "SELECT g.goal_id AS goal_id, g.title AS title, "
            "COUNT(*) AS n, MAX(t.id) AS last_id, MAX(t.created_at) AS last_at "
            "FROM goal_tasks t JOIN goals g ON g.goal_id = t.goal_id "
            "WHERE g.status = ? AND t.created_at >= ? "
            "GROUP BY g.goal_id, g.title ORDER BY n DESC, last_at DESC",
            # ⚠️ 传进去的 "active" 是 **goals 那一族**的词表值，不是本表 `STATUS_ACTIVE`：
            # 两个词表今天都含 "active"，将来谁改谁的不等于改另一个，
            # 用常量冒充跨词表的一致是假统一（口径同首页文件头第 5 条）。
            ("active", cutoff_stamp)).fetchall()
        for row in task_rows:
            title = str(row["title"] or "").strip()
            key = normalize_focus_title(title)
            if not title or not key or key in seen:
                continue
            if matches_focus_title(title, active_texts):
                continue
            seen.add(key)
            out.append({
                "title": title,
                "kind": CANDIDATE_KIND_TASKS,
                "confidence": _coarse_confidence(int(row["n"])),
                "evidence": (f"近 {window_days} 天该目标下有 {int(row['n'])} 条新建任务"
                             f"（最近一条 goal_tasks #{int(row['last_id'])}，"
                             f"创建于 {str(row['last_at'] or '')[:16]} UTC）"),
            })
        # 来路二：窗口内还等你定的决策（**取 `core.decision_store` 的常量，不复制词表**）。
        decision_rows = conn.execute(
            "SELECT id, title, status, updated_at FROM decisions "
            "WHERE status IN (?, ?) AND "
            "SUBSTR(COALESCE(NULLIF(updated_at, ''), created_at, ''), 1, 10) >= ? "
            "ORDER BY updated_at DESC, id DESC",
            (ds.STATUS_PROPOSED, ds.STATUS_DISCUSSED, cutoff_date)).fetchall()
        for row in decision_rows:
            title = str(row["title"] or "").strip()
            key = normalize_focus_title(title)
            if not title or not key or key in seen:
                continue
            if matches_focus_title(title, active_texts):
                continue
            seen.add(key)
            out.append({
                "title": title,
                "kind": CANDIDATE_KIND_DECISIONS,
                "confidence": _coarse_confidence(1),
                "evidence": (f"Decision #{int(row['id'])}（{row['status']}）"
                             f"更新于 {str(row['updated_at'] or '')[:16]} UTC，"
                             "还等你定"),
            })
    finally:
        conn.close()
    out.sort(key=lambda c: (-float(c["confidence"]), str(c["title"])))
    return out[:max(0, int(limit))]
