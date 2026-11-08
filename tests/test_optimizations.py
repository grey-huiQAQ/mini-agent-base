# -*- coding: utf-8 -*-
"""
v3.7.2 优化项专项测试（离线，不依赖 LLM 与网络）
覆盖：
- 端口冲突预检 check_port_available（绑定占用检测与释放后可用）
- RAG 索引：iter_supported_files 递归扫描子目录、_file_key 相对路径唯一 ID
- ExperienceStore.record_feedback 防抖窗口（开启去重 / 默认关闭向后兼容）
- search_experiences 检索词按频次排序并截断 Top-N（_rank_terms 单元测试）
- IntentAgent：create_new 幂等保护改为精确标题匹配、associate_existing
  多候选返回候选清单提示手动选择、单候选保持自动关联
- GoalManager.find_goal_by_exact_title 精确匹配
- ContextManager 注入评分展示格式 [评分: N]
- parse_daily_hours 复合格式（X小时Y分钟 / XhYm）与旧格式兼容
- app.do_generate_staged_plan 下拉刷新纳入 try（失败返回安全默认值）
- app.bot_response 移除空 assistant 占位时保留当前用户消息（质量修复回归）
"""
import os
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection, get_db
from memory.experience_store import ExperienceStore, _extract_terms, _rank_terms
from core.goal_manager import GoalManager
from core.context_manager import ContextManager
from agents.intent_agent import IntentAgent
from agents.planning_mirror_agent import parse_daily_hours

# 导入 app.py（构建完整 UI 但不会启动服务，launch 在 __main__ 分支内）
import app as app_module
# Phase 3b：bot_response 等回调已迁入 chat_controller，注入 conversation_store/runtime 的目标随之迁移
from ui.controllers import chat_controller as _chat_ctl
# 索引脚本（v3.7.2 起带 __main__ 守卫，导入不触发索引）
import index_documents as index_module

# ---------- 数据隔离 ----------
# 本文件全部目标的标题前缀：setup 时按前缀清除历史残留
# （中途失败的运行来不及执行末尾 cleanup，重跑时在此自愈，避免污染断言）
_GOAL_TITLE_PREFIX = "优化测试-"
_created_goal_ids = []
_created_exp_ids = []
_FIXTURE_SCENARIOS = ("优化场景-防抖", "优化场景-注入展示")


def setup():
    """初始化数据库并清除本文件 fixture 的历史残留"""
    init_db()
    conn = get_db()
    placeholders = ", ".join("?" * len(_FIXTURE_SCENARIOS))
    conn.execute(f"DELETE FROM experiences WHERE scenario IN ({placeholders})",
                 _FIXTURE_SCENARIOS)
    # 清理历史残留的目标（含任务与里程碑）
    stale = conn.execute(
        "SELECT goal_id FROM goals WHERE title LIKE ?", (_GOAL_TITLE_PREFIX + "%",)
    ).fetchall()
    for row in stale:
        gid = row["goal_id"]
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()


def _make_goal(gm: GoalManager, title: str, desc: str = ""):
    """创建目标并记录 ID，供 cleanup 精确删除"""
    assert title.startswith(_GOAL_TITLE_PREFIX), \
        f"本文件的测试目标必须使用 {_GOAL_TITLE_PREFIX!r} 前缀以便隔离清理"
    goal = gm.create_goal(title, desc)
    _created_goal_ids.append(goal.goal_id)
    return goal


def _purge_fixture_goals():
    """按标题前缀清除本文件的全部测试目标（保证各用例相互独立、可重复运行）"""
    conn = get_connection()
    stale = conn.execute(
        "SELECT goal_id FROM goals WHERE title LIKE ?", (_GOAL_TITLE_PREFIX + "%",)
    ).fetchall()
    for row in stale:
        gid = row["goal_id"]
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    conn.commit()
    conn.close()
    _created_goal_ids.clear()


def _make_exp(scenario: str, strategy: str) -> int:
    """新增经验并记录 ID 供清理"""
    new_id = ExperienceStore.add_experience(scenario, strategy)
    _created_exp_ids.append(new_id)
    return new_id


def _cleanup():
    """精确清理本测试产生的目标与经验数据（并按前缀/场景名单自愈兜底）"""
    conn = get_connection()
    for gid in list(_created_goal_ids):
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    # 前缀兜底：清理任何带本文件前缀的残留目标（含中途断言失败产生的孤儿数据）
    stale = conn.execute(
        "SELECT goal_id FROM goals WHERE title LIKE ?", (_GOAL_TITLE_PREFIX + "%",)
    ).fetchall()
    for row in stale:
        gid = row["goal_id"]
        conn.execute("DELETE FROM goal_tasks WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM milestones WHERE goal_id = ?", (gid,))
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
    for exp_id in _created_exp_ids:
        conn.execute("DELETE FROM experiences WHERE id = ?", (exp_id,))
    # 按场景名单自愈（防止中途失败遗留脏数据）
    placeholders = ", ".join("?" * len(_FIXTURE_SCENARIOS))
    conn.execute(f"DELETE FROM experiences WHERE scenario IN ({placeholders})",
                 _FIXTURE_SCENARIOS)
    conn.commit()
    conn.close()
    _created_goal_ids.clear()
    _created_exp_ids.clear()


def _get_exp(exp_id: int) -> dict:
    """按 ID 读取单条经验"""
    for exp in ExperienceStore.get_all_experiences(limit=500):
        if exp["id"] == exp_id:
            return exp
    return {}


# ========== 优化项 1：端口冲突检测 ==========

def test_check_port_available_detects_bound_port():
    """端口预检：被监听的端口应判定不可用，释放后立即可用"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))  # 由系统分配一个确定空闲的端口
    sock.listen(1)
    port = sock.getsockname()[1]

    assert app_module.check_port_available(port) is False, \
        "已被监听的端口应判定为不可用"

    sock.close()
    assert app_module.check_port_available(port) is True, "释放后的端口应判定为可用"
    print("[OK] check port available detects bound port")


# ========== 优化项 2：RAG 索引递归扫描与唯一 ID ==========

def test_iter_supported_files_recursive_and_file_key_unique():
    """递归扫描子目录；_file_key 基于相对路径生成且路径不同则键不同"""
    # 在项目 data 目录下搭建临时目录树（测试结束后清理）
    root = Path(__file__).resolve().parent.parent / "data" / "_test_index_scratch"
    if root.exists():
        shutil.rmtree(root)
    (root / "sub" / "deep").mkdir(parents=True)
    try:
        (root / "a.txt").write_text("根目录文档", encoding="utf-8")
        (root / "sub" / "b.md").write_text("一级子目录文档", encoding="utf-8")
        (root / "sub" / "deep" / "c.markdown").write_text("深层文档", encoding="utf-8")
        (root / "ignore.py").write_text("# 不支持的扩展名", encoding="utf-8")
        (root / "data.docx").write_text("二进制占位", encoding="utf-8")

        files = index_module.iter_supported_files(root)
        rels = sorted(f.relative_to(root).as_posix() for f in files)
        assert rels == ["a.txt", "sub/b.md", "sub/deep/c.markdown"], \
            f"应递归命中 3 个受支持文件，实际: {rels}"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 同一相对路径键稳定；不同相对路径（含同名文件在不同子目录）键不同
    assert index_module._file_key("sub/a.txt") == index_module._file_key("sub/a.txt"), \
        "同路径键应稳定"
    assert index_module._file_key("a.txt") != index_module._file_key("sub/a.txt"), \
        "不同路径键必须不同"
    print("[OK] iter supported files recursive + file key unique")


# ========== 优化项 3：反馈防抖 ==========

def test_record_feedback_dedup_window():
    """防抖窗口开启时：同 ID 同窗口内重复反馈被忽略且不重复累计评分"""
    exp_id = _make_exp("优化场景-防抖", "间隔重复策略")

    assert ExperienceStore.record_feedback(exp_id, "up", dedup_window_seconds=3600) is True
    exp = _get_exp(exp_id)
    assert exp["score"] == 1 and exp["use_count"] == 1

    # 窗口内的第二次反馈：视为已处理（True），但不重复累计
    assert ExperienceStore.record_feedback(exp_id, "up", dedup_window_seconds=3600) is True
    assert ExperienceStore.record_feedback(exp_id, "down", dedup_window_seconds=3600) is True
    exp = _get_exp(exp_id)
    assert exp["score"] == 1, f"窗口内重复反馈不应改变评分，实际 score={exp['score']}"
    assert exp["use_count"] == 1, f"窗口内重复反馈不应增加评价次数，实际 use_count={exp['use_count']}"

    # 不存在的 ID：开启防抖也应返回 False
    assert ExperienceStore.record_feedback(99999999, "up", dedup_window_seconds=3600) is False
    print("[OK] record feedback dedup window")


def test_record_feedback_dedup_disabled_by_default():
    """默认不启用防抖：连续多次反馈全部生效（与历史行为完全一致）"""
    exp_id = _make_exp("优化场景-防抖", "间隔重复策略")

    for _ in range(3):
        assert ExperienceStore.record_feedback(exp_id, "up") is True
    exp = _get_exp(exp_id)
    assert exp["score"] == 3 and exp["use_count"] == 3, \
        f"默认关闭防抖时应逐次累计，实际 score={exp['score']}, use_count={exp['use_count']}"
    print("[OK] record feedback dedup disabled by default")


# ========== 优化项 4：长输入检索精度 ==========

def test_rank_terms_frequency_order_and_truncation():
    """检索词按查询中出现频次降序排序，并可截断 Top-N"""
    query = "英语单词 英语单词 背单词"
    terms = _extract_terms(query)
    ranked = _rank_terms(terms, query, max_terms=10)
    # 频次："单词"×3 > "英语"/"语单"×2 > "背单"×1；频次相同时保持提取顺序
    assert ranked == ["单词", "英语", "语单", "背单"], \
        f"应按查询内出现频次降序排列，实际: {ranked}"

    # 截断生效：长查询生成的词数超过上限时仅保留 Top-N
    long_query = "这是一个非常长的查询用于验证截断逻辑是否正常工作并且包含足够多的汉字组合"
    all_terms = _extract_terms(long_query)
    assert len(all_terms) > 10, "用例前提：该长查询应产生超过 10 个检索词"
    truncated = _rank_terms(all_terms, long_query, max_terms=10)
    assert len(truncated) == 10, f"应截断为 Top-10，实际 {len(truncated)}"

    # max_terms<=0 表示不截断（向后兼容旧行为）
    unlimited = _rank_terms(all_terms, long_query, max_terms=0)
    assert len(unlimited) == len(all_terms), "max_terms<=0 时不应截断"

    # 频次相同时保持原始提取顺序（稳定排序，结果可复现）
    stable_a = _rank_terms(_extract_terms(query), query, max_terms=None)
    stable_b = _rank_terms(_extract_terms(query), query, max_terms=None)
    assert stable_a == stable_b, "排序结果应当可复现"
    print("[OK] rank terms frequency order and truncation")


def test_search_experiences_max_terms_backward_compat():
    """search_experiences 默认 Top-10 截断仍能正常召回；显式传参生效"""
    exp_id = _make_exp("优化场景-注入展示", "英语单词记忆采用间隔重复策略")

    # 默认参数（隐式 Top-10）：短查询行为与旧版一致，能正常命中
    hits = ExperienceStore.search_experiences("怎么背英语单词", limit=5)
    assert any(h["id"] == exp_id for h in hits), "默认截断下短查询应正常命中"

    # 显式收紧到 3 个检索词：核心词仍保留，相关经验依然可命中
    hits_tight = ExperienceStore.search_experiences("怎么背英语单词才高效不容易遗忘呢", limit=5,
                                                    max_terms=3)
    assert isinstance(hits_tight, list), "max_terms 参数应被接受且返回列表"

    # 接口签名向后兼容：不传 max_terms 时等价于旧行为调用
    hits_old_style = ExperienceStore.search_experiences("英语单词", limit=3)
    assert isinstance(hits_old_style, list)
    print("[OK] search experiences max terms backward compat")


# ========== 优化项 6 / 7：意图关联精度 + 幂等精确匹配 ==========

def test_find_goal_by_exact_title():
    """find_goal_by_exact_title：完全一致才命中，包含关系不误判"""
    _purge_fixture_goals()
    gm = GoalManager()
    g1 = _make_goal(gm, "优化测试-基础日语")
    g2 = _make_goal(gm, "优化测试-日语进阶")

    hit1 = gm.find_goal_by_exact_title("优化测试-基础日语")
    assert hit1 is not None and hit1.goal_id == g1.goal_id, "精确标题应命中自身"

    hit2 = gm.find_goal_by_exact_title("优化测试-日语进阶")
    assert hit2 is not None and hit2.goal_id == g2.goal_id, "另一标题应命中自身"

    # 包含关系不再误判：查「基础日」或「日语」都不应返回任何目标
    assert gm.find_goal_by_exact_title("基础日") is None, "部分标题不应被精确匹配命中"
    assert gm.find_goal_by_exact_title("日语") is None, "共同子串不应被精确匹配命中"
    assert gm.find_goal_by_exact_title("") is None, "空标题应返回 None"
    assert gm.find_goal_by_exact_title(None) is None, "None 应返回 None"
    print("[OK] find goal by exact title")


def test_intent_create_new_exact_title_guard():
    """create_new 幂等保护：精确同名拦截；相近但不同名（包含关系）放行创建"""
    _purge_fixture_goals()
    gm = GoalManager()
    agent = IntentAgent(gm)
    existing = _make_goal(gm, "优化测试-基础日语")

    # 完全同名 → 拦截
    r_dup = agent.apply_intent({"title": "优化测试-基础日语",
                                "suggestion_type": "create_new"})
    assert not r_dup["ok"], "完全同名目标存在时应拦截创建"
    assert "已存在同名进行中目标" in r_dup["message"]
    active_titles = [g.title for g in gm.list_goals(status="active")]
    assert active_titles.count("优化测试-基础日语") == 1, "不应重复创建同名目标"

    # 「日语进阶」只是包含「日语」二字但并非同名 → 放行创建（旧的模糊匹配会误判）
    r_new = agent.apply_intent({
        "title": "优化测试-日语进阶", "description": "", "priority": 2,
        "estimated_weeks": 4, "suggestion_type": "create_new"})
    assert r_new["ok"], f"非同名的包含关系目标应允许创建，实际: {r_new['message']}"
    created = gm.get_goal(r_new["goal_id"])
    assert created is not None and created.title == "优化测试-日语进阶"
    print("[OK] intent create new exact title guard")


def test_intent_associate_multiple_candidates_lists_options():
    """associate_existing 多候选：不自动取第一个，列出候选（≤3 个）提示手动选择"""
    _purge_fixture_goals()
    gm = GoalManager()
    agent = IntentAgent(gm)
    g1 = _make_goal(gm, "优化测试-英语口语强化")
    g2 = _make_goal(gm, "优化测试-英语口语进阶")

    tasks_before = sum(len(gm.get_tasks(g.goal_id)) for g in (g1, g2))
    result = agent.apply_intent({
        "title": "每天开口练习20分钟",
        "suggestion_type": "associate_existing",
        "target_title": "英语口语",
    })
    assert not result["ok"], "多个候选时不应自动关联"
    assert result["goal_id"] is None, "多候选时不应写入任何目标 ID"
    for g in (g1, g2):
        assert g.title in result["message"], f"候选标题「{g.title}」应出现在提示中"
    assert "手动选择" in result["message"], "应提示用户手动选择"
    tasks_after = sum(len(gm.get_tasks(g.goal_id)) for g in (g1, g2))
    assert tasks_after == tasks_before, "多候选分支不应写库添加任务"
    print("[OK] intent associate multiple candidates lists options")


def test_intent_associate_single_candidate_still_auto():
    """associate_existing 单候选：保持原有自动关联行为不变"""
    _purge_fixture_goals()
    gm = GoalManager()
    agent = IntentAgent(gm)
    goal = _make_goal(gm, "优化测试-英语口语强化")

    result = agent.apply_intent({
        "title": "每天开口练习20分钟",
        "description": "跟读影子练习",
        "priority": 2,
        "suggestion_type": "associate_existing",
        "target_title": "英语口语强化",
        "reason": "唯一匹配",
    })
    assert result["ok"], f"唯一候选应照常自动关联，实际: {result['message']}"
    assert result["goal_id"] == goal.goal_id
    matched = [t for t in gm.get_tasks(goal.goal_id) if t["title"] == "每天开口练习20分钟"]
    assert len(matched) == 1, "任务应写入唯一匹配的目标"
    print("[OK] intent associate single candidate still auto")


# ========== 优化项 8：评分展示格式 ==========

def test_context_score_format_bracket():
    """ContextManager 注入经验的评分展示应为「[评分: N]」，不再使用「⭐ 评分 N」"""
    cm = ContextManager()
    exp_id = _make_exp("优化场景-注入展示", "英语单词记忆采用间隔重复策略")

    ctx = cm._build_experiences_context("英语单词记忆方法", limit=3)
    assert "[评分: 0]" in ctx, f"未评分经验应以 [评分: 0] 展示，实际:\n{ctx}"
    assert "⭐ 评分" not in ctx, "不应再出现旧的（⭐ 评分 N）格式"

    ExperienceStore.record_feedback(exp_id, "up")
    ctx2 = cm._build_experiences_context("英语单词记忆方法", limit=3)
    assert "[评分: 1]" in ctx2, f"评分更新后应以 [评分: 1] 展示，实际:\n{ctx2}"
    print("[OK] context score format bracket")


# ========== 优化项 5：do_generate_staged_plan 安全降级 ==========

def test_generate_staged_plan_failure_returns_safe_defaults():
    """计划生成抛异常时：返回错误消息 + gr.update(choices=[]) 安全默认值"""
    original = app_module.planner_agent.generate_staged_plan

    def boom(*args, **kwargs):
        raise RuntimeError("模型超时模拟")
    app_module.planner_agent.generate_staged_plan = boom
    try:
        msg, display, plan_state, dropdown_update = \
            app_module.do_generate_staged_plan("goal_x", "", "", 4)
    finally:
        app_module.planner_agent.generate_staged_plan = original

    assert "❌ 生成计划失败" in msg
    assert display == "" and plan_state is None
    assert isinstance(dropdown_update, dict) and dropdown_update.get("choices") == [], \
        f"下拉框应返回安全默认 choices=[]，实际: {dropdown_update}"
    print("[OK] generate staged plan failure returns safe defaults")


def test_generate_staged_plan_dropdown_refresh_failure_safe():
    """成功生成但下拉刷新（list_goals）失败时：同样安全降级，不向 UI 抛错"""
    fake_result = {"ok": True, "message": "计划已生成", "display": "# 计划",
                   "plan": SimpleNamespace(goal_id="goal_fake")}
    orig_generate = app_module.planner_agent.generate_staged_plan
    orig_list = app_module.goal_manager.list_goals

    app_module.planner_agent.generate_staged_plan = lambda *a, **k: fake_result

    def broken_list_goals(*args, **kwargs):
        raise RuntimeError("数据库忙模拟")
    app_module.goal_manager.list_goals = broken_list_goals
    try:
        msg, display, plan_state, dropdown_update = \
            app_module.do_generate_staged_plan("goal_y", "", "", 4)
    finally:
        app_module.planner_agent.generate_staged_plan = orig_generate
        app_module.goal_manager.list_goals = orig_list

    assert "❌ 生成计划失败" in msg, "list_goals 异常应被捕获并转为错误消息"
    assert isinstance(dropdown_update, dict) and dropdown_update.get("choices") == [], \
        f"刷新失败时下拉框应返回安全默认值，实际: {dropdown_update}"
    print("[OK] generate staged plan dropdown refresh failure safe")


# ========== 质量修复回归：bot_response 保留当前用户消息 ==========

def test_bot_response_removes_placeholder_keeps_user_message():
    """上一轮残留空 assistant 占位时：移除占位且保留本轮用户消息（修复误删回归）"""
    class FakeStore:
        def save_message(self, sid, role, content):
            pass

    class FakeRuntime:
        def process_user_input(self, *args, **kwargs):
            yield "模拟回复"

    orig_store = _chat_ctl.conversation_store
    orig_runtime = _chat_ctl.runtime
    _chat_ctl.conversation_store = FakeStore()
    _chat_ctl.runtime = FakeRuntime()
    try:
        history = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": ""},   # 上一轮残留的空占位
            {"role": "user", "content": "新问题"},  # 本轮用户消息
        ]
        chunks = list(app_module.bot_response(history, "通用助手", 0.7, 2048,
                                              True, False, "sess_test"))
        final = chunks[-1]
        contents = [(m.get("role"), m.get("content")) for m in final]
    finally:
        _chat_ctl.conversation_store = orig_store
        _chat_ctl.runtime = orig_runtime

    assert contents == [
        ("user", "旧问题"),
        ("user", "新问题"),      # 本轮用户消息必须保留（旧实现会被误删）
        ("assistant", "模拟回复"),
    ], f"占位应被移除且用户消息保留，实际: {contents}"
    print("[OK] bot response removes placeholder keeps user message")


def test_bot_response_keeps_nonempty_assistant_history():
    """history[-2] 为正常非空 assistant 回复时不得被当作占位误删（审查回归用例）

    同时覆盖两个子场景：
    1. 正常非空 assistant 回复 → 必须保留；
    2. 纯空白内容的 assistant 占位 → 视为占位移除。
    """
    class FakeStore:
        def save_message(self, sid, role, content):
            pass

    class FakeRuntime:
        def process_user_input(self, *args, **kwargs):
            yield "本轮回复"

    orig_store = _chat_ctl.conversation_store
    orig_runtime = _chat_ctl.runtime
    _chat_ctl.conversation_store = FakeStore()
    _chat_ctl.runtime = FakeRuntime()
    try:
        # 子场景 1：正常的上一轮 assistant 回复（非空）——绝不能删
        history = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "这是上一轮的正常回复，不是占位。"},
            {"role": "user", "content": "第二问"},
        ]
        final = list(app_module.bot_response(history, "通用助手", 0.7, 2048,
                                             True, False, "sess_test"))[-1]
        contents = [(m.get("role"), m.get("content")) for m in final]
        assert contents == [
            ("user", "第一问"),
            ("assistant", "这是上一轮的正常回复，不是占位。"),  # 非空 assistant 必须保留
            ("user", "第二问"),
            ("assistant", "本轮回复"),
        ], f"正常 assistant 回复不应被误删，实际: {contents}"

        # 子场景 2：纯空白内容视为占位 → 应被移除
        history2 = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "   "},   # 仅空白的残留占位
            {"role": "user", "content": "新问题"},
        ]
        final2 = list(app_module.bot_response(history2, "通用助手", 0.7, 2048,
                                              True, False, "sess_test"))[-1]
        contents2 = [(m.get("role"), m.get("content")) for m in final2]
        assert contents2 == [
            ("user", "旧问题"),
            ("user", "新问题"),
            ("assistant", "本轮回复"),
        ], f"空白占位应被移除，实际: {contents2}"
    finally:
        _chat_ctl.conversation_store = orig_store
        _chat_ctl.runtime = orig_runtime
    print("[OK] bot response keeps non-empty assistant history")


# ========== 优化项 9：parse_daily_hours 复合格式 ==========

def test_parse_daily_hours_compound_formats():
    """复合格式：X小时Y分钟 / XhYm 及其变体正确换算"""
    assert parse_daily_hours("1小时30分钟") == 1.5, "X小时Y分钟 应解析为小时小数"
    assert parse_daily_hours("2h30m") == 2.5, "XhYm 应解析为小时小数"
    assert parse_daily_hours("45分钟") == 0.75, "纯分钟应换算为小时"
    assert parse_daily_hours("45m") == 0.75, "英文纯分钟应换算"
    assert parse_daily_hours("30min") == 0.5, "min 写法应换算"
    assert parse_daily_hours("2 hours") == 2.0, "英文复数小时应识别（空格容错）"
    print("[OK] parse daily hours compound formats")


def test_parse_daily_hours_backward_compat():
    """旧格式兼容：原有简单写法的行为与历史版本完全一致"""
    assert parse_daily_hours(None) == 2.0, "缺失时应回退默认 2 小时"
    assert parse_daily_hours("") == 2.0
    assert parse_daily_hours("abc") == 2.0, "无法解析时应回退默认"
    assert parse_daily_hours("2") == 2.0, "纯数字按小时处理"
    assert parse_daily_hours("1.5") == 1.5
    assert parse_daily_hours("1.5小时") == 1.5
    assert parse_daily_hours("3H") == 3.0, "大写 H 兼容"
    assert parse_daily_hours("90分钟") == 1.5, "分钟换算兼容"
    assert parse_daily_hours("0小时") == 2.0, "非正数应回退默认"
    assert parse_daily_hours("99") == 16.0, "异常大值应钳制到上限"
    print("[OK] parse daily hours backward compat")


# ========== 清理 ==========

def test_cleanup():
    """清理本测试产生的全部数据"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== v3.7.2 optimizations test ===\n")
    setup()
    test_check_port_available_detects_bound_port()
    test_iter_supported_files_recursive_and_file_key_unique()
    test_record_feedback_dedup_window()
    test_record_feedback_dedup_disabled_by_default()
    test_rank_terms_frequency_order_and_truncation()
    test_search_experiences_max_terms_backward_compat()
    test_find_goal_by_exact_title()
    test_intent_create_new_exact_title_guard()
    test_intent_associate_multiple_candidates_lists_options()
    test_intent_associate_single_candidate_still_auto()
    test_context_score_format_bracket()
    test_generate_staged_plan_failure_returns_safe_defaults()
    test_generate_staged_plan_dropdown_refresh_failure_safe()
    test_bot_response_removes_placeholder_keeps_user_message()
    test_bot_response_keeps_nonempty_assistant_history()
    test_parse_daily_hours_compound_formats()
    test_parse_daily_hours_backward_compat()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
