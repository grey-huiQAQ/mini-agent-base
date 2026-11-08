# -*- coding: utf-8 -*-
"""
TaskExecutor 任务执行自动化模块测试（离线：mock web_search / chat_sync，不依赖网络与 LLM）
覆盖：
- 任务类型识别：搜索/笔记关键词、搜索词优先、英文小写、空白与未知类型
- 可自动标记兜底规则：识别为受支持类型才为 True
- 提示音：<audio autoplay> + base64 WAV 内嵌、进程内缓存、可关闭 autoplay
- 执行计划生成器：空计划、无可自动任务、批准写盘、拒绝不写盘、
  next() 恢复视为拒绝（安全默认）、不支持类型跳过、单任务异常不阻断
- 处理器：资料搜索（mock 工具）、笔记整理（LLM 草稿 / 异常降级提取）
- 审批落盘：save_approval 写库写文件、非法审批内容拒绝
- UI 回调契约：do_execute_plan / do_approve_pending / do_reject_pending
  返回元组长度与 outputs 匹配、无审批时友好提示、执行结束清空 State
"""
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection
from agents import task_executor as te_module
from agents.task_executor import (
    TaskExecutor,
    classify_task,
    guess_auto_executable,
    beep_audio_html,
)

# 本测试使用的目标 ID 前缀（带随机后缀避免污染共享开发库中的真实目标数据）
_GOAL_PREFIX = "goal_txtest_" + uuid.uuid4().hex[:6]
_used_goal_ids = []
_created_note_ids = []


def setup():
    """初始化数据库（notes 表等）"""
    init_db()


# ---------- mock 工具 ----------

def _patch_web_search(fake):
    """临时替换 local_tools.web_search（TaskExecutor 经模块引用调用），返回还原函数"""
    original = te_module.local_tools.web_search
    te_module.local_tools.web_search = fake

    def restore():
        te_module.local_tools.web_search = original
    return restore


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync（笔记草稿 LLM 调用），返回还原函数"""
    original = te_module.model_client.chat_sync
    te_module.model_client.chat_sync = fake

    def restore():
        te_module.model_client.chat_sync = original
    return restore


def _new_goal_id() -> str:
    """生成一个本测试专属的目标 ID 并登记，供 cleanup 精确清理"""
    gid = f"{_GOAL_PREFIX}_{len(_used_goal_ids)}_{int(time.time())}"
    _used_goal_ids.append(gid)
    return gid


def _make_task(gid, title, auto=True, desc=""):
    """构造一条与 DailyPlannerAgent 输出结构一致的计划任务"""
    return {
        "task_id": 1,
        "goal_id": gid,
        "task_title": title,
        "goal_title": "测试目标",
        "estimated_hours": 2.0,
        "reason": "测试",
        "auto_executable": auto,
        "description": desc,
    }


def _cleanup():
    """清理本测试产生的笔记行、待审批记录、目标专属目录（research_notes / notes）"""
    conn = get_connection()
    try:
        for gid in _used_goal_ids:
            conn.execute("DELETE FROM notes WHERE goal_id = ?", (gid,))
            # TaskExecutor 审批持久化：执行计划暂停时会写入 pending_approvals
            conn.execute("DELETE FROM pending_approvals WHERE goal_id = ?", (gid,))
        for nid in _created_note_ids:
            conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
        conn.commit()
    finally:
        conn.close()
    for gid in _used_goal_ids:
        goal_root = Path("data") / "goals" / gid
        if goal_root.exists():
            shutil.rmtree(goal_root, ignore_errors=True)
    _used_goal_ids.clear()
    _created_note_ids.clear()


# ---------- 任务类型识别 ----------

def test_classify_task_rules():
    """关键词分类：搜索类、笔记类、搜索词优先于笔记词、英文小写、未知类型"""
    assert classify_task("搜索 Transformer 相关资料") == "search"
    assert classify_task("调研 RAG 检索方案") == "search"
    assert classify_task("整理本周学习笔记") == "note"
    assert classify_task("总结错题并归纳要点") == "note"
    # 同时含两类词时按"先收集资料"的顺序归为搜索任务
    assert classify_task("搜索资料后整理成笔记") == "search"
    assert classify_task("Weekly Research on Agents").lower().find("search") >= 0, \
        "应先命中 search 再做后续判断"
    assert classify_task("Research papers about attention") == "search"
    # 英文关键词大小写不敏感
    assert classify_task("Take NOTES after class") == "note"
    # 空白与未知类型
    assert classify_task("") is None
    assert classify_task("   ") is None
    assert classify_task("线下跑步五公里") is None
    print("[OK] classify task rules")


def test_guess_auto_executable():
    """本地标记兜底：仅受支持类型（搜索/整理）标记 True，其余默认 False"""
    assert guess_auto_executable("搜索论文资料") is True
    assert guess_auto_executable("整理课堂笔记", "包含第三章内容") is True
    assert guess_auto_executable("完成线下报名缴费") is False
    assert guess_auto_executable("") is False
    print("[OK] guess auto executable")


# ---------- 提示音 ----------

def test_beep_audio_html():
    """提示音 HTML：autoplay 属性、base64 WAV 数据源、进程内缓存一致"""
    html = beep_audio_html(autoplay=True)
    assert "<audio autoplay>" in html, "应包含 <audio autoplay> 标签"
    assert 'src="data:audio/wav;base64,' in html, "应以 base64 内嵌 WAV 数据"
    base64_part = html.split("base64,", 1)[1].split('"', 1)[0]
    assert len(base64_part) > 100, "音频数据不应为空"

    html2 = beep_audio_html(autoplay=False)
    assert "autoplay" not in html2 and "data:audio/wav;base64," in html2

    # 进程内缓存：两次生成的 base64 完全一致（只合成一次）
    again = beep_audio_html(autoplay=True)
    assert again.split("base64,", 1)[1] == html.split("base64,", 1)[1]
    print("[OK] beep audio html")


# ---------- 执行计划生成器 ----------

def test_execute_plan_empty_and_no_auto():
    """空计划与无可自动任务：直接产出 done 事件且不做任何处理"""
    executor = TaskExecutor()
    events = list(executor.execute_plan([]))
    assert events and events[-1]["type"] == "done"
    assert events[-1]["stats"]["total"] == 0

    plan_dict = {"plan": [_make_task(_new_goal_id(), "线下活动", auto=False)]}
    events2 = list(executor.execute_plan(plan_dict))
    assert events2[-1]["type"] == "done"
    assert "没有标记为" in events2[-1]["message"], "应说明没有可自动任务"
    assert events2[-1]["stats"]["auto"] == 0
    # 整包字典容错：plan 字段被解出，total 统计正确
    assert events2[-1]["stats"]["total"] == 1
    print("[OK] execute plan empty and no auto")


def test_execute_plan_search_approve_writes_file():
    """搜索任务批准流：暂停等待审批 → send(True) → 结果写入 research_notes 目录"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    fake_result = "1. Attention Is All You Need\n   经典 Transformer 论文\n   https://example.com/a"
    restore = _patch_web_search(lambda query, **kw: fake_result)
    try:
        gen = executor.execute_plan([_make_task(gid, "搜索注意力机制相关资料")])
        ev_progress = next(gen)
        assert ev_progress["type"] == "progress" and "可自动执行" in ev_progress["message"]
        ev_working = next(gen)
        assert ev_working["type"] == "progress" and "正在处理" in ev_working["message"]

        ev_approval = next(gen)
        assert ev_approval["type"] == "approval"
        approval = ev_approval["approval"]
        assert approval["type"] == "search"
        assert "Attention Is All You Need" in approval["content"], "审阅稿应包含搜索结果"
        assert approval["suggested_path"].replace("\\", "/").endswith(
            f"data/goals/{gid}/research_notes"), "建议保存路径应为目标的 research_notes 目录"
        assert approval["filename"].endswith(".md")
        assert "<audio autoplay>" in (ev_approval.get("audio") or ""), "审批事件应携带提示音"

        ev_saved = gen.send(True)  # 批准保存
        assert ev_saved["type"] == "result" and "✅" in ev_saved["message"]

        ev_done = next(gen)
        assert ev_done["type"] == "done"
        assert ev_done["stats"]["saved"] == 1
        assert ev_done["stats"]["rejected"] == 0
    finally:
        restore()

    # 文件确实写入磁盘
    research_dir = Path("data") / "goals" / gid / "research_notes"
    files = list(research_dir.glob("*.md"))
    assert files, "批准后应在 research_notes 下生成 Markdown 文件"
    text = files[0].read_text(encoding="utf-8")
    assert "资料搜索结果" in text and "Attention Is All You Need" in text
    print("[OK] execute plan search approve writes file")


def test_execute_plan_reject_does_not_write():
    """搜索任务拒绝流：send(False) → 丢弃结果，磁盘无任何文件"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "1. 结果一\n   摘要")
    try:
        gen = executor.execute_plan([_make_task(gid, "检索强化学习教程")])
        next(gen), next(gen)
        approval_event = next(gen)
        assert approval_event["type"] == "approval"
        ev = gen.send(False)
        assert ev["type"] == "result" and "已拒绝并丢弃" in ev["message"]
        done = next(gen)
        assert done["stats"]["rejected"] == 1 and done["stats"]["saved"] == 0
    finally:
        restore()

    research_dir = Path("data") / "goals" / gid / "research_notes"
    assert not research_dir.exists() or not list(research_dir.glob("*.md")), \
        "拒绝后不应写入任何文件"
    print("[OK] execute plan reject does not write")


def test_execute_plan_next_resume_treated_as_reject():
    """安全默认：在审批暂停点用 next() 恢复视为拒绝，绝不未经确认写盘"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "1. 某结果\n   摘要")
    try:
        gen = executor.execute_plan([_make_task(gid, "查找代理系统文献")])
        next(gen), next(gen)
        assert next(gen)["type"] == "approval"
        ev = next(gen)  # 未 send 决定，直接 next → 应走拒绝路径
        assert ev["type"] == "result" and "已拒绝并丢弃" in ev["message"]
        done = next(gen)
        assert done["stats"]["rejected"] == 1
    finally:
        restore()
    assert not (Path("data") / "goals" / gid / "research_notes").exists(), \
        "next() 恢复不得产生任何写盘"
    print("[OK] execute plan next resume treated as reject")


def test_execute_plan_unsupported_type_skipped():
    """被误标为可自动但类型不受支持的任务：跳过并计入 skipped，不中断流程"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    gen = executor.execute_plan([
        _make_task(gid, "去体育馆完成体测"),
        _make_task(gid, "搜索错题相关资料"),  # 正常搜索任务对照
    ])
    restore = _patch_web_search(lambda query, **kw: "1. 对照结果\n   ok")
    try:
        events = []
        event = next(gen)
        while event["type"] != "approval":
            events.append(event)
            event = next(gen)
        skipped_msgs = [e["message"] for e in events if e["type"] == "result"]
        assert any("暂不支持自动执行" in m for m in skipped_msgs), "不受支持的任务应被跳过"
        assert any("正在处理：**搜索错题相关资料**" in e["message"]
                   for e in events if e["type"] == "progress")
        done = gen.send(True)
        while done["type"] != "done":
            done = next(gen)
        stats = done["stats"]
        assert stats["skipped"] == 1 and stats["saved"] == 1 and stats["auto"] == 2
    finally:
        restore()
    print("[OK] execute plan unsupported type skipped")


def test_execute_plan_handler_exception_does_not_break_flow():
    """工具抛异常时单个任务计为 failed，后续任务继续执行"""
    gid = _new_goal_id()
    executor = TaskExecutor()

    def boom(query, **kw):
        raise RuntimeError("网络断开")

    restore = _patch_web_search(boom)
    try:
        events = list(executor.execute_plan([
            _make_task(gid, "搜索会失败的任务"),
        ]))
    finally:
        restore()
    results = [e for e in events if e["type"] == "result"]
    # 异常被处理器捕获并转为失败消息（不进入审批、不写盘），流程继续到 done
    assert any(("搜索失败" in e["message"] or "执行异常" in e["message"])
               for e in results), "异常应转为失败消息"
    done = events[-1]
    assert done["type"] == "done" and done["stats"]["failed"] == 1
    print("[OK] execute plan handler exception does not break flow")


# ---------- 两类处理器 ----------

def test_handle_search_task_failure_message():
    """web_search 返回 ❌ 前缀时：处理器判定失败且不进入审批"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    restore = _patch_web_search(lambda query, **kw: "❌ 搜索功能未安装")
    try:
        outcome = executor.handle_search_task(_make_task(gid, "搜索必然失败的关键词"))
    finally:
        restore()
    assert outcome["ok"] is False
    assert "搜索失败" in outcome["message"]
    assert outcome["approval"] is None
    print("[OK] handle search task failure message")


def test_handle_note_task_llm_draft_and_fallback():
    """笔记整理：LLM 可用时用其草稿；异常时降级为任务信息提取，均不写盘"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    task = _make_task(gid, "整理今日学习笔记", desc="包含注意力机制与微调实践")

    # LLM 正常：草稿来自 LLM 输出
    restore_llm = _patch_chat_sync(lambda messages, **kw: "- 要点A\n- 要点B")
    try:
        outcome = executor.handle_note_task(task)
    finally:
        restore_llm()
    assert outcome["ok"] is True
    approval = outcome["approval"]
    assert approval["type"] == "note"
    assert "要点A" in approval["content"]
    assert approval["suggested_path"].replace("\\", "/").endswith(
        f"data/goals/{gid}/notes")
    # 未确认前不产生任何文件
    assert not (Path("data") / "goals" / gid / "notes").exists()

    # LLM 抛异常：降级为直接提取任务描述
    def boom(messages, **kw):
        raise RuntimeError("LLM 宕机")

    restore_boom = _patch_chat_sync(boom)
    try:
        outcome2 = executor.handle_note_task(task)
    finally:
        restore_boom()
    assert outcome2["ok"] is True
    content2 = outcome2["approval"]["content"]
    assert "注意力机制与微调实践" in content2, "降级草稿应包含任务描述"
    assert "LLM 暂不可用" in content2
    print("[OK] handle note task llm draft and fallback")


def test_save_approval_note_and_invalid():
    """save_approval：note 类型经 NoteStore 写库+文件；空内容/未知类型拒绝"""
    gid = _new_goal_id()
    executor = TaskExecutor()
    from agents.task_executor import build_note_filename  # 复用同一命名规则
    approval = {
        "task_id": 7, "task_title": "存储验证笔记", "goal_id": gid,
        "goal_title": "测试目标", "type": "note",
        "content": "## 📝 笔记草稿:存储验证笔记\n\n正文内容",
        "suggested_path": str(Path("data") / "goals" / gid / "notes"),
        "filename": build_note_filename("存储验证笔记"),
    }
    result = executor.save_approval(approval)
    assert result["ok"] is True, result["message"]

    from memory.note_store import NoteStore
    rows = [n for n in NoteStore.get_notes_by_goal(gid)]
    assert len(rows) == 1 and rows[0]["title"] == "存储验证笔记"
    _created_note_ids.append(rows[0]["id"])
    assert Path(rows[0]["file_path"]).exists(), "笔记文件应同步落盘"

    # 非法输入：空内容 / 缺字段
    assert executor.save_approval({}) ["ok"] is False
    assert executor.save_approval({"type": "note", "content": ""}) ["ok"] is False
    assert executor.save_approval({"type": "unknown", "content": "x",
                                   "task_title": "t"}) ["ok"] is False
    print("[OK] save approval note and invalid")


# ---------- UI 回调契约 ----------

def test_ui_callback_contract():
    """app 回调契约：四元组输出匹配、无审批提示、批准后 State 清空"""
    from app import do_execute_plan, do_approve_pending, do_reject_pending

    # 未开始执行就点审批按钮 → 友好提示而非崩溃
    for fn in (do_approve_pending, do_reject_pending):
        res = fn(None)
        assert isinstance(res, tuple) and len(res) == 4, f"{fn.__name__} 应返回四元组"
        assert "请先点击" in res[0]

    gid = _new_goal_id()
    plan = [_make_task(gid, "搜索回调契约资料")]
    restore = _patch_web_search(lambda query, **kw: "1. 契约结果\n   内容")
    try:
        paused = do_execute_plan(plan)
        assert isinstance(paused, tuple) and len(paused) == 4, \
            "do_execute_plan 必须返回四元组以匹配 outputs"
        status, display, audio, gen_state = paused
        assert "审批" in status and isinstance(display, str)
        assert "资料搜索结果" in display, "展示区应包含待审阅内容"
        assert "<audio autoplay>" in audio, "待审批时应注入提示音 HTML"
        assert gen_state is not None, "暂停中的生成器应存入 State"

        approved = do_approve_pending(gen_state)
        status2, display2, audio2, gen_state2 = approved
        assert "执行结束" in status2
        assert "✅ 已写入文件" in display2, "批准后进度区应有保存成功记录"
        assert audio2 == "", "非审批状态下提示音插槽应为空"
        assert gen_state2 is None, "执行结束后 State 应置 None 防止重复恢复"

        # 对已结束的 State 再次恢复 → 安全收尾不崩溃
        res_again = do_approve_pending(None)
        assert "请先点击" in res_again[0]

        # 拒绝链路：State 全程正常流转
        paused3 = do_execute_plan(plan)
        rejected = do_reject_pending(paused3[3])
        assert rejected[3] is None and "执行结束" in rejected[0]
        assert "已拒绝并丢弃" in rejected[1]
    finally:
        restore()
    print("[OK] ui callback contract")


def test_cleanup():
    """清理测试数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== task executor test ===\n")
    setup()
    test_classify_task_rules()
    test_guess_auto_executable()
    test_beep_audio_html()
    test_execute_plan_empty_and_no_auto()
    test_execute_plan_search_approve_writes_file()
    test_execute_plan_reject_does_not_write()
    test_execute_plan_next_resume_treated_as_reject()
    test_execute_plan_unsupported_type_skipped()
    test_execute_plan_handler_exception_does_not_break_flow()
    test_handle_search_task_failure_message()
    test_handle_note_task_llm_draft_and_fallback()
    test_save_approval_note_and_invalid()
    test_ui_callback_contract()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
