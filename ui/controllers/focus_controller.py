# -*- coding: utf-8 -*-
"""ui.controllers.focus_controller —— 首页「🎯 当前重点」那一格（P7 阶段二 组 5 子批 A，2026-10-18）

为什么单独一个模块而不写进 `home_controller.py`（2026-10-18 设计席定案「写口放新模块」）：
首页那一格从 B1 起就有一条被三条用例钉住的契约——**只做读**（`tests/test_home_dashboard.py`
里的静态禁词、逐表指紋对拍、绑定区写回调名单）。子批 A 要的是「首页加一个当前重点区块，
支持添加／编辑／暂停／完成」，那是一张**写**口。把写塞进 `home_controller` 就等于把那条
契约改成「除了这里以外只读」；写口放在本模块，`home_controller` 一个字都不必改，
那条契约仍然是它字面说的那句话，而「这一页有写」这件事由**页面级**文档去改口径
（`PROJECT_STATUS.md` §三 首页行 ＋ `DESIGN_ANCHORS.md` §一.11）。

四条按实写下的口径：

1. **解析与预览全程零写库**（判据：自然语言解析与推断候选零写库除非用户确认）。
   `do_focus_preview` 只把 `core.current_focus` 的解析结果渲染成人读得懂的预览并存进
   `gr.State`；真的写只发生在 `do_focus_confirm` → `cf.apply_focus_preview` 那一条路径上。
2. **推断候选只建议、不覆盖、不自动转正**（判据③）：`refresh_focus_panel` 每次现算候选、
   一行都不写；只有人点「📌 采纳这条建议」才落库，落库时 `source='inferred'` 并带上候选自己的
   `evidence` 与 `confidence`——存储层会拒掉缺这两格的 inferred 行，所以「候选转正成
   explicit」在这个形状里发生不了。
3. **「🚫 先不看这条」只活本次运行**（按实登记的限度）：忽略记在模块内的一个集合里，
   **不写库**。后果照实写在这里：重启后同一条候选还会再提一次。为什么不建「已忽略」那一列——
   子批 A 的边界里没有这一项，而为一个会话级偏好新建持久字段是「多存一处、多一处会说谎的地方」。
4. **置信度那一把尺不是概率**：`0.4 + 0.1 × 近期活动条数`、封顶 0.8（`core.current_focus`）。
   界面上必须把这句话说完，只印一个 0.6 会让人以为有谁算过它。

错误语义：存储层的规则违规（active 满档、inferred 缺出处、非法状态）走 `ValueError`，
本模块**catch 成一句回执**而不是让它冒到 Gradio 事件循环上——那一格没刷新比「为什么没写进去」
更难查。数据库层异常（表缺失、磁盘满）照 `core/decision_store.py` 那一条**不吞**，
吞掉会让人以为重点已经存上了。
"""

from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

# ui=3 引 core=0 是正向依赖（层序见 checks/check_import_direction.py）
from core import current_focus as cf

#: 本次运行里被点过「🚫 先不看这条」的候选标题（归一化后）。**不写库**，见模块 docstring 第 3 条。
_dismissed_candidates: set = set()

#: 上限那一格在文案里出现一次就够，取存储层的常量而不是在这里抄一个 3
_CAP = cf.MAX_ACTIVE_FOCUS


def _live_candidates() -> List[Dict[str, Any]]:
    """现算候选，并去掉本次运行里被点过「先不看」的那几条（**只读**）"""
    return [c for c in cf.suggest_focus_candidates()
            if cf.normalize_focus_title(c.get("title")) not in _dismissed_candidates]


def _focus_line(row: Dict[str, Any]) -> str:
    """一条重点的显示行。来历、置信、证据都照实带上——「这条怎么来的」是本表存在的理由之一"""
    source = str(row.get("source") or "")
    origin = "人自己说的" if source == cf.SOURCE_EXPLICIT else "推断来的（已采纳）"
    parts = [f"- **#{int(row['id'])} {row.get('title') or '（无标题）'}**（{origin}）"]
    desc = str(row.get("description") or "").strip()
    if desc:
        parts.append(f"  说明：{desc}")
    if source == cf.SOURCE_INFERRED:
        parts.append(
            f"  凭什么在这儿：{row.get('evidence') or '（库里这一格是空的）'}"
            f"｜置信 {row.get('confidence')}（按近期活动条数给的粗量，不是概率）")
    expires = str(row.get("expires_at") or "").strip()
    if expires:
        parts.append(f"  到期：{expires}（只写不自动动状态）")
    parts.append(f"  更新于 {str(row.get('updated_at') or '')[:16]}（UTC）")
    return "\n".join(parts)


def render_focus_region(rows: List[Dict[str, Any]], others: int,
                        candidates: List[Dict[str, Any]]) -> str:
    """那一格的 Markdown：active 列表 ＋ 历史条数一句 ＋ 候选建议行（纯渲染，不读库）

    Args:
        rows: active 行（调用方给的，已排好序）。
        others: 非 active（paused／completed）的行数——**只报数、不列全**，
            列全了首页就变成历史页；要看旧的那几条去库里读，本批没做这一格。
        candidates: 当前活着的候选（已滤掉本次运行里忽略过的）。
    """
    lines: List[str] = []
    if rows:
        lines.append(f"**当前重点（active {len(rows)}／上限 {_CAP}）**")
        lines.append("")
        lines.extend(_focus_line(row) for row in rows)
    else:
        lines.append("（还没有 active 的当前重点。可以在下面那句「说一句你的重点变化」"
                     "里讲『我现在做 X』，先生成预览再写入；也可以直接填标题点「➕ 新增重点」。）")
    if others:
        lines.append("")
        lines.append(f"另有 {others} 条处于暂停／完成状态——**这一格从不删行**，"
                     "旧的那几条还在库里，只是不在这一屏列出来。")
    if candidates:
        lines.append("")
        lines.append("**建议设为当前重点**（系统从近期活动里挑的，**你不点就什么都不写**）：")
        for cand in candidates:
            lines.append(
                f"- 建议：{cand.get('title')}｜凭 {cand.get('evidence')}"
                f"｜置信 {cand.get('confidence')}（按近期活动条数给的粗量，不是概率）")
        lines.append("")
        lines.append("> 选中一条后：「📌 采纳这条建议」才会落库，且落库时它的来历仍记为"
                     "**推断来的**并带走上面那句证据；「🚫 先不看这条」只在这次运行里有效，"
                     "重启后同一条还会再提。")
    return "\n".join(lines)


def _updates() -> Tuple[str, Dict, Dict]:
    """读一遍账本 ＋ 现算候选，返回（那一格的 Markdown, 管理下拉 update, 候选下拉 update）

    三个地方共用它：启动回填、刷新按钮、每一次写之后。**写之后重新取候选**是故意的——
    刚被设成重点的那一条不该再出现在建议里（存储层的候选本来也把它挡在门外，
    这里再取一次是让界面说的和库里的一致）。
    """
    rows = cf.list_focus(status=cf.STATUS_ACTIVE)
    all_rows = cf.list_focus()
    others = len(all_rows) - len(rows)
    candidates = _live_candidates()
    manage_choices = [(f"#{int(r['id'])}｜{str(r.get('title') or '')[:20]}", int(r["id"]))
                      for r in rows]
    cand_choices = [(f"{c['title']}", c["title"]) for c in candidates]
    return (render_focus_region(rows, others, candidates),
            gr.update(choices=manage_choices, value=None,
                      interactive=bool(manage_choices)),
            gr.update(choices=cand_choices, value=None,
                      interactive=bool(cand_choices)))


def refresh_focus_panel() -> Tuple[str, Dict, Dict]:
    """启动回填与「🔄 刷新首页」用的那一格（**只读**，一行都不写）

    挂在 `demo.load` 链尾，与首页三区同族（判据③「未处理项在启动时可见」）：
    重启之后不点任何东西也得看见现在的重点和当下这批候选。
    """
    return _updates()


def do_focus_add(title: str, description: str = "") -> Tuple[str, str, Dict, Dict, str, Any]:
    """「➕ 新增重点」：直接建一条 explicit 重点（人自己填的标题＝明确指令，不需要预览）

    Returns:
        (回执, 那一格 Markdown, 管理下拉 update, 候选下拉 update, 预览文案, 预览 State)
        —— 预览那一格被清空：刚改过账本，上一份预览已经不代表当下。
    """
    try:
        new_id = cf.create_focus(str(title or "").strip(),
                                 description=str(description or "").strip() or None,
                                 source=cf.SOURCE_EXPLICIT)
    except ValueError as e:
        return f"⚠️ 没写进去：{e}", *_updates(), "", None
    return f"✅ 已新增当前重点 #{new_id}", *_updates(), "", None


def do_focus_select(focus_id: Optional[int]) -> Tuple[Dict, Dict]:
    """选中一条重点后把它的标题／说明装进那两个输入框（**只读**，改名与暂停都从这一格起手）"""
    if not focus_id:
        return gr.update(value=""), gr.update(value="")
    row = cf.get_focus(int(focus_id))
    if not row:
        return gr.update(value=""), gr.update(value="")
    return (gr.update(value=str(row.get("title") or "")),
            gr.update(value=str(row.get("description") or "")))


def do_focus_update(focus_id: Optional[int], title: str,
                    description: str = "") -> Tuple[str, str, Dict, Dict, str, Any]:
    """「✏️ 保存所选的修改」：改标题／说明。**不碰来历**——改标题不等于 explicit／inferred 互换"""
    if not focus_id:
        return "⚠️ 先在上方选一条当前重点", *_updates(), "", None
    try:
        ok = cf.update_focus(int(focus_id), title=str(title or "").strip() or None,
                             description=description if description is not None else None)
    except ValueError as e:
        return f"⚠️ 没改成：{e}", *_updates(), "", None
    if not ok:
        return (f"⚠️ #{int(focus_id)} 不在库里了（大概被改过）：一行都没动",
                *_updates(), "", None)
    return f"✅ 已更新 #{int(focus_id)} 的标题／说明（来历仍照原样）", *_updates(), "", None


def _advance(focus_id: Optional[int], status: str) -> Tuple[str, str, Dict, Dict, str, Any]:
    """暂停／完成两个按钮共用的那一歩：只推进状态，**不删行**"""
    if not focus_id:
        return "⚠️ 先在上方选一条当前重点", *_updates(), "", None
    label = "暂停" if status == cf.STATUS_PAUSED else "完成"
    try:
        ok = cf.set_focus_status(int(focus_id), status)
    except ValueError as e:
        return f"⚠️ 没{label}成：{e}", *_updates(), "", None
    if not ok:
        return (f"⚠️ #{int(focus_id)} 不在库里了：一行都没动",
                *_updates(), "", None)
    row = cf.get_focus(int(focus_id))
    return (f"✅ 已把 #{int(focus_id)} 标为{label}："
            f"「{row.get('title') if row else ''}」那一行**没有删**，历史还在",
            *_updates(), "", None)


def do_focus_pause(focus_id: Optional[int]) -> Tuple[str, str, Dict, Dict, str, Any]:
    """「⏸️ 暂停所选」：status → paused"""
    return _advance(focus_id, cf.STATUS_PAUSED)


def do_focus_complete(focus_id: Optional[int]) -> Tuple[str, str, Dict, Dict, str, Any]:
    """「✅ 完成所选」：status → completed"""
    return _advance(focus_id, cf.STATUS_COMPLETED)


def do_focus_preview(text: str) -> Tuple[str, Any]:
    """「🔍 生成变更预览」：解析一句人话 ＋ 对上当前的重点，**只产出预览、一行都不写**

    Returns:
        (预览文案, 预览 dict 或 None) —— 预览存进 `gr.State`，由「✅ 按预览写入」消费。
        认不出说法／对不上／对上一条以上时 items 是空的，预览那一格就照实说「这一句不会
        写任何东西」，而不是先给一个看起来能点的按钮。
    """
    intent = cf.parse_focus_intent(text)
    preview = cf.build_focus_preview(intent)
    lines = [f"**变更预览**：{preview.get('summary')}"]
    items = preview.get("items") or []
    for item in items:
        if item.get("action") == "add":
            lines.append(f"- 新增：{item.get('title')}")
        else:
            lines.append(f"- 暂停：#{item.get('focus_id')} {item.get('title')}"
                         "（只改状态，那一行不删）")
    for note in (preview.get("notes") or []):
        if note:
            lines.append(f"> {note}")
    if not items:
        lines.append("> **这一句不会写任何东西**，所以「按预览写入」按下去也不会动账本。"
                     "含糊的说法一律不当指令处理：要改重点请说清楚是哪一件事。")
        return "\n".join(lines), None
    lines.append("> 这一步还没写。确认无误再点「✅ 按预览写入」。")
    return "\n".join(lines), preview


def do_focus_confirm(preview: Any) -> Tuple[str, str, Dict, Dict, str, Any]:
    """「✅ 按预览写入」：唯一因为「人说了话」而写库的那一步"""
    if not preview:
        return "⚠️ 现在没有待确认的预览：先生成一份再看这一格", *_updates(), "", None
    receipts = cf.apply_focus_preview(preview)
    return "\n".join(receipts), *_updates(), "", None


def do_focus_clear() -> Tuple[str, str, Dict, Dict, str, Any]:
    """「🧹 清掉预览」：把那份预览丢掉（**不碰账本**，写没写过都由回执那一句说明）"""
    return "已清掉预览：账本一个字没动", *_updates(), "", None


def do_focus_adopt(title: Optional[str]) -> Tuple[str, str, Dict, Dict, str, Any]:
    """「📌 采纳这条建议」：把一条候选落成 `source='inferred'` 的重点，**带上它的证据与置信**

    判据③ 的那一半在这一格落地：采纳之后它的来历**仍然是 inferred**，不改写成 explicit——
    「人点了采纳」是一次批准，不是「这话是他自己说出来的」。查的是当场重算的候选集：
    候选已经消失（比如这条刚被设成重点）就直接拒写并照实说，不拿一份旧候选去补一个新行。
    """
    if not title:
        return "⚠️ 先在「建议设为当前重点」里选一条", *_updates(), "", None
    wanted = cf.normalize_focus_title(title)
    fresh = next((c for c in _live_candidates() if cf.normalize_focus_title(c.get("title")) == wanted),
                 None)
    if not fresh:
        return (f"⚠️ 「{title}」已经不在当前候选里了（大概刚被设成重点）：一行都没写",
                *_updates(), "", None)
    try:
        new_id = cf.create_focus(str(fresh["title"]), source=cf.SOURCE_INFERRED,
                                 confidence=fresh.get("confidence"),
                                 evidence=fresh.get("evidence"))
    except ValueError as e:
        return f"⚠️ 没写进去：{e}", *_updates(), "", None
    return (f"✅ 已把「{fresh['title']}」设为当前重点 #{new_id}，"
            "来历记为 inferred，证据与置信照候选原样带上",
            *_updates(), "", None)


def do_focus_ignore(title: Optional[str]) -> Tuple[str, str, Dict, Dict, str, Any]:
    """「🚫 先不看这条」：只记在本次运行的集合里，**不写库、不改任何一条重点**"""
    if not title:
        return "⚠️ 先在「建议设为当前重点」里选一条", *_updates(), "", None
    _dismissed_candidates.add(cf.normalize_focus_title(title))
    return (f"🚫 本次运行里不再提「{title}」。**重启后它还会再出现一次**："
            "这一格没有把「忽略」写进库", *_updates(), "", None)
