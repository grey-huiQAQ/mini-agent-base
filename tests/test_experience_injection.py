# -*- coding: utf-8 -*-
"""
经验注入（P7 第一闭环 Phase 4b，2026-10-07）模块测试：`ExperienceStore.latest_goal_lessons`
＋ `ContextManager._build_goal_lesson_context` 那一段追加注入。全程离线：只碰临时库，
不调模型、不联网、不碰生产库（生产库此刻连 `decision_id` 那一列都还没有）。

**本文件存在的理由就是 `test_injection_changes_the_prompt_for_the_same_state_and_question`
那一条**（拆分文档 4b 判据①，设计席 2026-10-07 第三次重申）：同一个 State、同一个问题，
注入前与注入后各构建一次 system prompt，断言两次输出**确实不同**。把注入摘掉，这一条必须
变红——写成「断言注入段非空」的话，整份设计就作废了，因为空不空与「有没有影响下一轮」是
两件事。`test_negative_control_injection_call_is_what_makes_the_difference` 就是去真摘一次。

覆盖（逐条对着裁决 2 的四环筛选与两条硬约束）：
- 候选链方向（锚点 §一.8）：`experiences.decision_id` → `decisions.goal_id`，**单向**；
  反过来的那一列（`decisions.experience_id`）已被 D1 裁决否掉、库里也不存在；
- 四环：goal 一致 → `decision_scope` ∈ 传入的集合（成员判断，不是 `>=`）→ 最近
  （`created_at DESC, id DESC`）→ 最多 1 条；
- 只注入 `future_effect` 那一列（锚点 §一.9）：`scenario`／`effect`／`strategy`／决策全文
  一个都不进片段；legacy 那两行（没有 `decision_id`）自然不注入，属预期不是缺失；
- 判据②：无候选时注入段为空**且这一轮不报错**；
- 判据③：system prompt 的长度增量低于硬上限（用 5000 字的教训与荒谬长的决策 id 顶到边界）。

负控另有三类，各配一条"红在哪"：
① 进程内放宽集合（`patch` 掉 `EXPERIENCE_ELIGIBLE_SCOPES`）——被挡的形状必须放行；
② 进程内摘掉注入调用——差异断言那两个 prompt 必须变成相等；
③ 被定案禁止的形状在测试里用裸 SQL 造一次（`tactical` 的已链接行、空 `future_effect`
   的已链接行、`created_at` 与 `id` 相互矛盾的两行）——写路径本来进不来，
   这三条钉的是**读侧自己**有没有牙齿。

**跑不过去时的第一诊断**：本文件的候选查询走 `experiences.decision_id` 与
`decisions.goal_id` 两列。报 `no such column: decision_id` 就是 `init_db()` 的迁移段
（`memory/database.py` 里那句 `_add_column_if_not_exists(conn, "experiences",
"decision_id", ...)`）没跑进 `setup()`；查询本身没有列就报错的兜底，兜底在
`ContextManager` 那一侧（异常 → 这一轮不注入）。
"""
import atexit
import os
import shutil
import sys
import uuid
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

from memory.database import init_db, get_db                      # noqa: E402
from memory.experience_store import ExperienceStore              # noqa: E402
from core import decision_store as ds                            # noqa: E402
from core.context_manager import (                               # noqa: E402
    ContextManager,
    LESSON_SECTION_MAX_CHARS,
    LESSON_TEXT_MAX_CHARS,
    LESSON_HEADER_MAX_CHARS,
)

# 本文件的用例前缀（临时库随 cleanup 整体删除，前缀只便于排查）
_PREFIX = "inj4b_" + uuid.uuid4().hex[:6]
_GOAL_A = f"{_PREFIX}goal_a"
_GOAL_B = f"{_PREFIX}goal_b"
_BASE = "你是英语学习助手。"
_BODY_PREFIX = "- 下次改什么："


def setup():
    """初始化独立测试数据库（`experiences.decision_id` 由 init_db 的迁移段加出来）"""
    init_db()


def _cleanup():
    if os.path.isdir(_TMP_DIR):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup)


# ---------- 辅助 ----------

def _reset_tables():
    """清空 decisions 与 experiences：每条用例自己造自己的行，互不共用

    为什么每条都清：候选查询按「最近一条」取，任何一条别的用例留下的行都可能变成
    答案，那会让断言随 `__main__` 里的调用顺序漂移——而"红在哪一条"必须是稳定的。
    """
    conn = get_db()
    try:
        conn.execute("DELETE FROM experiences")
        conn.execute("DELETE FROM decisions")
        conn.commit()
    finally:
        conn.close()


def _evaluated(goal_id=_GOAL_A, scope="operational", title=None,
               actual_result="两周后确实少问了一次") -> int:
    """造一条已经走到 evaluated 的决策，返回 id（4a 的输入形状）"""
    decision_id = ds.create_decision(
        title or f"{_PREFIX}决策{uuid.uuid4().hex[:6]}",
        goal_id=goal_id, decision_scope=scope)
    ds.set_status(decision_id, "discussed")
    ds.set_status(decision_id, "approved")
    ds.mark_executed(decision_id, "note://4b-test")
    ds.evaluate(decision_id, "helpful", actual_result)
    return decision_id


def _flow_back(lesson, goal_id=_GOAL_A, scope="operational", title=None,
               actual_result="两周后确实少问了一次"):
    """回流一条教训（走 4a 的写路径，不绕守卫），返回 (decision_id, experience_id)"""
    decision_id = _evaluated(goal_id=goal_id, scope=scope, title=title,
                             actual_result=actual_result)
    return decision_id, ds.link_experience(decision_id, lesson)


def _candidates(goal_id=_GOAL_A, scopes=None, limit=1):
    return ExperienceStore.latest_goal_lessons(
        goal_id, ds.EXPERIENCE_ELIGIBLE_SCOPES if scopes is None else scopes,
        limit=limit)


def _force_created_at(experience_id, stamp):
    """裸 UPDATE 造时间先后：写路径不给 created_at 参数，只能在这里直接改库"""
    conn = get_db()
    try:
        conn.execute("UPDATE experiences SET created_at = ? WHERE id = ?",
                     (stamp, experience_id))
        conn.commit()
    finally:
        conn.close()


def _raw_update_scope(decision_id, scope):
    """裸 UPDATE 把已回流的决策改成 tactical——**这个形状 4a 的守卫三拒在写路径外**，
    这里造它只为验读侧过滤自己的牙齿（纪律 §五.2 的"被定案禁止的形状在测试里构造一次"）
    """
    conn = get_db()
    try:
        conn.execute("UPDATE decisions SET decision_scope = ? WHERE id = ?",
                     (scope, decision_id))
        conn.commit()
    finally:
        conn.close()


# ---------- 候选查询：链方向与四环筛选 ----------

def test_candidate_query_follows_the_one_way_link():
    """判据链方向：经 experiences.decision_id → decisions.goal_id 找得到，别的 goal 找不到

    什么实现下会红：JOIN 反了（去 decisions 上找经验列）会直接报 no such column；
    少了 goal 过滤会把 _GOAL_B 那一条一起捞进来。
    """
    _reset_tables()
    did_a, _ = _flow_back("先看日志时间戳，别先看自己的感觉", goal_id=_GOAL_A,
                          title=f"{_PREFIX}把复盘从周日挪到周六")
    _flow_back("别的目标的那一句教训", goal_id=_GOAL_B)

    rows = _candidates()
    assert len(rows) == 1, f"最多 1 条是裁决 2 的定值，实际 {len(rows)} 条：{rows}"
    row = rows[0]
    assert row["decision_id"] == did_a
    assert row["future_effect"] == "先看日志时间戳，别先看自己的感觉"
    assert row["decision_goal_id"] == _GOAL_A
    assert row["decision_scope"] == "operational"
    # 带着决策自己的字段回来，调用方才不需要为了渲染再查一次 decisions 表
    assert row["evaluation"] == "helpful"

    # goal_id 是库里比较的键：带空白的传入被收敛掉之后仍然命中（Gradio 下拉值常带空白）
    padded = _candidates(goal_id=f"  {_GOAL_A} ")
    assert [r["decision_id"] for r in padded] == [did_a], "strip 之后的同一个目标该命中同一条"
    assert _candidates(goal_id=_GOAL_B)[0]["decision_id"] != did_a
    print("[OK] candidates come from the one-way link, filtered by goal_id")


def test_candidate_query_refuses_empty_keys_and_limits():
    """三种「没有候选」返回 []，且都不抛——判据②的前半句要的是这个返回

    三条都有牙齿，不是恒真：
    - 空 goal：库里刻意留一条 `goal_id=''` 的已链接行，少了早返回就会把它捞出来；
    - 空集合：少了早返回，`IN ()` 是 SQL 语法错，直接抛 OperationalError；
    - limit 为负：SQLite 的 `LIMIT -1` 等于「不限」，少了早返回就会把候选全吐出来。
    """
    _reset_tables()
    blank_decision = ds.create_decision(f"{_PREFIX}没绑目标的决策", goal_id="",
                                        decision_scope="operational")
    ds.set_status(blank_decision, "discussed")
    ds.set_status(blank_decision, "approved")
    ds.mark_executed(blank_decision, "note://4b-test")
    ds.evaluate(blank_decision, "helpful", "空 goal 的实际结果")
    ds.link_experience(blank_decision, "空 goal 的那一句不该被注入")
    _flow_back("正常目标的那一句", goal_id=_GOAL_A)

    for bad_goal in ("", "   ", None):
        assert _candidates(goal_id=bad_goal) == [], \
            f"没选目标（goal_id={bad_goal!r}）不该拿到任何候选"
    assert _candidates(goal_id=_GOAL_A, scopes=set()) == [], \
        "空集合该返回 []，而不是把 IN () 那种语法错抛给调用方"
    assert _candidates(goal_id=_GOAL_A, limit=-1) == [], \
        "负数 limit 在 SQLite 里是「不限」，早返回必须挡住"
    # 正面形状各留一条对照，证明上面三个 [] 不是"查询整个坏了"
    assert len(_candidates(goal_id=_GOAL_A)) == 1
    print("[OK] the three no-candidate shapes return [] without raising")


def test_candidate_query_skips_rows_without_a_decision_link():
    """4a 之前存量的两行不注入：legacy 经验（add_experience 写的）本就没有来路

    锚点 §一.9 原话：生产库现有那 1 行 legacy 经验在 4b 自然不注入，**属预期，不是缺失**。
    什么实现下会红：把 JOIN 改成 LEFT JOIN，这两行就都进来了。
    """
    _reset_tables()
    legacy_id = ExperienceStore.add_experience(
        scenario=f"{_PREFIX}legacy 场景", strategy="legacy 策略",
        effect="旧效果")
    # add_experience 的签名里**没有** future_effect 这个参数（它是 4a 才加上的列），
    # 所以「有教训但没有来路」这一行只能走裸 SQL——它模拟的正是 4a 之前手写的存量。
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO experiences (scenario, strategy, effect, conditions, "
            "source_session_id, future_effect, decision_id) "
            "VALUES (?, '', '', '', NULL, ?, NULL)",
            (f"{_PREFIX}只有教训没有来路", "没有 decision_id 的一句"))
        conn.commit()
        unlinked = conn.execute(
            "SELECT COUNT(*) AS c FROM experiences WHERE decision_id IS NULL").fetchone()["c"]
        with_lesson = conn.execute(
            "SELECT COUNT(*) AS c FROM experiences WHERE decision_id IS NULL "
            "AND future_effect IS NOT NULL").fetchone()["c"]
    finally:
        conn.close()
    assert legacy_id > 0
    assert int(unlinked) == 2 and int(with_lesson) >= 1, \
        f"前置：该造出两行没有来路的经验，实际 {unlinked}/{with_lesson}"

    assert _candidates() == [], "没有 decision_id 的经验不该成为闭环候选"
    # 同一批数据里补一条有来路的，它必须立刻出现——证明空不是查询坏了
    _flow_back("有来路的那一句", goal_id=_GOAL_A)
    assert len(_candidates()) == 1
    print("[OK] unlinked legacy experiences stay out of the loop")


def test_candidate_query_needs_a_non_empty_lesson():
    """空教训不算候选：读侧与 4a 守卫四是同一口径，各有一份牙齿

    形状来源是被定案禁止的那一种（写路径的守卫四会拒空串），所以这里用裸 SQL 造。
    """
    _reset_tables()
    decision_id = _evaluated(goal_id=_GOAL_A)
    conn = get_db()
    try:
        for bad in ("", "  "):
            conn.execute(
                "INSERT INTO experiences (scenario, strategy, effect, conditions, "
                "source_session_id, future_effect, decision_id) "
                "VALUES (?, '', '', '', NULL, ?, ?)",
                (f"{_PREFIX}空教训行", bad, decision_id))
        conn.commit()
    finally:
        conn.close()
    assert _candidates() == [], "空 future_effect 不该成为候选——闭环不注入空气"
    _flow_back("有内容的那一句", goal_id=_GOAL_A)
    assert _candidates()[0]["future_effect"] == "有内容的那一句"
    print("[OK] empty lessons are filtered out on the read side too")


def test_candidate_query_orders_by_created_at_then_id():
    """「最近」这一环的定值：created_at 优先于 id——把两者拧反必须能看出来

    做法：先写 A 再写 B（B 的 id 更大），然后把 A 的 created_at 改到未来。
    只按 id 倒序的实现会把 B 当成最近的一条。
    """
    _reset_tables()
    _, exp_a = _flow_back("较早写但时间在后", goal_id=_GOAL_A, title=f"{_PREFIX}A")
    _, exp_b = _flow_back("较晚写但时间在前", goal_id=_GOAL_A, title=f"{_PREFIX}B")
    assert exp_b > exp_a, "前置：B 的 id 该更大"
    _force_created_at(exp_a, "2099-01-01 00:00:00")
    _force_created_at(exp_b, "2020-01-01 00:00:00")

    rows = _candidates()
    assert len(rows) == 1 and rows[0]["id"] == exp_a, \
        f"「最近」应按 created_at 取，实际 {rows}"
    # 不传 limit 时用的默认值也是裁决 2 的那个 1——这一格得被钉住，否则「最多 1 条」
    # 只活在调用点的字面量里，把签名默认值改成 3 谁都不会发现（变异验过：它原先会存活）
    default_rows = ExperienceStore.latest_goal_lessons(_GOAL_A,
                                                      ds.EXPERIENCE_ELIGIBLE_SCOPES)
    assert len(default_rows) == 1, \
        f"库里有两条候选时默认只该回一条，实际 {len(default_rows)} 条"
    # 同一秒落的两条（DEFAULT CURRENT_TIMESTAMP 是秒级）靠 id 分先后：取 id 大的
    _reset_tables()
    _, first = _flow_back("同秒第一条", goal_id=_GOAL_A)
    _, second = _flow_back("同秒第二条", goal_id=_GOAL_A)
    same_stamp = "2026-10-07 03:00:00"
    _force_created_at(first, same_stamp)
    _force_created_at(second, same_stamp)
    rows = _candidates()
    assert rows[0]["id"] == second and second > first, \
        f"created_at 相同该用 id 倒序打破平局，实际 {rows}"
    print("[OK] recency is created_at first, id second")


def test_all_three_eligible_scopes_are_candidates():
    """集合内不分档：operational／strategic／critical 三档都能当候选（裁决 1）

    什么实现下会红：把它做成 `>=` 或某档特例，就会被挡掉一档。三条各造一行、
    每次只留一条（清表后单独造）以确保各自命中。
    """
    for scope in sorted(ds.EXPERIENCE_ELIGIBLE_SCOPES):
        _reset_tables()
        decision_id, _ = _flow_back(f"{scope} 那一档的教训", goal_id=_GOAL_A, scope=scope)
        rows = _candidates()
        assert len(rows) == 1 and rows[0]["decision_id"] == decision_id, \
            f"{scope} 该在集合里，实际候选 {rows}"
    assert sorted(ds.EXPERIENCE_ELIGIBLE_SCOPES) == ["critical", "operational", "strategic"], \
        "前置：集合本身还是 4a 定下来的那三档"
    print("[OK] every scope inside the set flows, no ordering involved")


# ---------- 注入片段：只带那一列 ----------

def test_lesson_section_carries_only_that_one_column():
    """段形状：标题带决策 id，正文只有 future_effect 那一列

    锚点 §一.9 ＋ 裁决 2「只注入该字段不注入全文」：scenario（= 决策 title）、
    effect（= actual_result）、strategy、summary 都不许出现在片段里。
    """
    _reset_tables()
    title = f"{_PREFIX}把每日播报改成只在周六"
    actual = "改了之后一周少了三次打断"
    lesson = "下次先看日志里的触发时间戳再决定改哪一档"
    decision_id, _ = _flow_back(lesson, goal_id=_GOAL_A, title=title,
                               actual_result=actual)

    section = ContextManager()._build_goal_lesson_context(_GOAL_A)
    assert section == f"## 🧭 上次复盘的教训（决策 {decision_id}）\n{_BODY_PREFIX}{lesson}", \
        f"段形状被改动：{section!r}"
    assert title not in section, "scenario（决策 title）不该进片段"
    assert actual not in section, "effect（actual_result）不该进片段"
    assert "策略" not in section and "[评分" not in section, \
        "legacy 那一段的渲染格式不该混进闭环段"
    assert section.count(_BODY_PREFIX) == 1, "最多 1 条：正文只有一行"
    print("[OK] the section carries exactly the one column")


def test_lesson_section_is_capped_by_construction():
    """判据③：长度增量低于硬上限，且不因为超长就整段不注入

    两个边界各顶一次：正文 5000 字（真实形状），以及荒谬长的决策 id（内存里替换 store
    的返回值——它不碰库，只验渲染代码能不能被顶出上限）。
    """
    _reset_tables()
    _, _ = _flow_back("这" * 5000, goal_id=_GOAL_A)
    cm = ContextManager()
    section = cm._build_goal_lesson_context(_GOAL_A)
    assert len(section) <= LESSON_SECTION_MAX_CHARS, \
        f"正文上限没兜住：{len(section)}"
    assert section.endswith("…"), "超长该被截断并留下省略号，而不是整段消失"

    absurd = {"decision_id": 10 ** 200, "future_effect": "也" * 5000,
              "decision_goal_id": _GOAL_A, "decision_scope": "operational",
              "evaluation": "helpful", "id": 1, "scenario": "s", "effect": "e",
              "created_at": "2026-10-07 00:00:00"}
    with patch.object(ExperienceStore, "latest_goal_lessons", return_value=[absurd]):
        weird = cm._build_goal_lesson_context(_GOAL_A)
    assert len(weird) <= LESSON_SECTION_MAX_CHARS, \
        f"标题没封顶，段长被决策 id 顶穿上限：{len(weird)}"
    assert len(weird) > 0, "顶到边界也要把教训送出去，不许静默返回空段"
    # 三个常量的构造关系（写在这儿防止有人只改一个数就把保证改没了）
    assert LESSON_HEADER_MAX_CHARS + 1 + len(_BODY_PREFIX) + LESSON_TEXT_MAX_CHARS \
        <= LESSON_SECTION_MAX_CHARS, "上限之间的余量被改掉了"
    print("[OK] the section length is guaranteed by construction")


def test_goal_lesson_context_returns_empty_for_no_candidate():
    """判据②：没选目标、该目标没有候选、库抛异常——三种都返回空串且不响"""
    _reset_tables()
    cm = ContextManager()
    assert cm._build_goal_lesson_context(None) == ""
    assert cm._build_goal_lesson_context("") == ""
    assert cm._build_goal_lesson_context(_GOAL_A) == "", "该目标一条候选都没有"

    # 读库失败（生产库此刻就没有 decision_id 列，这条是最现实的形状）：兜成不注入
    with patch.object(ExperienceStore, "latest_goal_lessons",
                      side_effect=RuntimeError("模拟 no such column")):
        assert cm._build_goal_lesson_context(_GOAL_A) == ""
        prompt = cm.build_system_prompt("英语", _BASE, detail_level="high",
                                       goal_id=_GOAL_A, user_input="今天去哪买菜")
        assert prompt.startswith(_BASE), "读库失败不许影响这一轮对话"
    print("[OK] no candidate and read failure both mean: inject nothing, raise nothing")


# ---------- 判据①：致命差异断言 ----------

def test_injection_changes_the_prompt_for_the_same_state_and_question():
    """**本阶段存在的全部理由**（拆分文档 4b 判据①）

    同一个 goal、同一个问题、同一个角色，回流之前与之后各 build 一次：
    ① 两次输出**必须不同**（不是"注入段非空"那种弱断言）；
    ② 增量恰好等于那一段——既有六段一个字都不动（"只追加，不改既有段落"）。
    """
    _reset_tables()
    cm = ContextManager()
    goal = f"{_PREFIX}goal_diff"
    question = "今天去哪买菜比较好"          # 与 title／lesson 无中文双字重合，隔离 legacy 段
    decision_id = _evaluated(goal_id=goal, title=f"{_PREFIX}把复盘固定在周六早上",
                            actual_result="挪到周六之后两次都没漏")

    before = cm.build_system_prompt("英语", _BASE, detail_level="high",
                                    goal_id=goal, user_input=question)
    assert cm._build_goal_lesson_context(goal) == "", "前置：此刻还没有候选"

    lesson = "先验证开机时间这个前提，再排复盘的钟点"
    ds.link_experience(decision_id, lesson)
    after = cm.build_system_prompt("英语", _BASE, detail_level="high",
                                  goal_id=goal, user_input=question)

    assert after != before, "回流之后 prompt 一模一样——闭环没有影响下一次，4b 白做"
    assert lesson not in before, "注入前那句教训不该已经在上下文里"
    assert lesson in after, "注入后那句教训必须出现在上下文里"
    section = cm._build_goal_lesson_context(goal)
    assert after == before + "\n\n" + section, \
        "增量应恰好等于闭环那一段：既有段落被改动了"
    assert len(after) - len(before) <= LESSON_SECTION_MAX_CHARS + 2, "长度增量越过硬上限"
    print("[OK] the same state and question really do get a different prompt")


def test_negative_control_injection_call_is_what_makes_the_difference():
    """负控（纪律 §五.2）：真把注入摘一次，差异必须消失

    只验「红了」不够，要验**红在哪条**：`_build_goal_lesson_context` 被换成返回空串之后，
    上面那条差异断言的两个 prompt 必须变成**相等**——这证明造成差异的确实是这一跳调用，
    而不是别的段落顺带变了。同一个进程内再把 eligible 集合放宽到含 tactical，
    被挡的那条形状必须放行——证明筛选调用的不是恒真。
    """
    _reset_tables()
    cm = ContextManager()
    goal = f"{_PREFIX}goal_mut"
    question = "今天去哪买菜比较好"
    decision_id = _evaluated(goal_id=goal, title=f"{_PREFIX}变异测试用的决策",
                            actual_result="变异用例的实际结果")
    lesson = "摘掉注入调用之后不该有任何差别"
    ds.link_experience(decision_id, lesson)
    section = cm._build_goal_lesson_context(goal)
    assert section, "前置：这一句确实能渲染成一段"

    normal = cm.build_system_prompt("英语", _BASE, detail_level="high",
                                    goal_id=goal, user_input=question)
    assert normal.endswith("\n\n" + section), "前置：那一段就挂在末尾"
    with patch.object(ContextManager, "_build_goal_lesson_context",
                      return_value=""):
        stripped = cm.build_system_prompt("英语", _BASE, detail_level="high",
                                          goal_id=goal, user_input=question)
    assert stripped == normal[: len(normal) - len(section) - 2], \
        "摘掉注入之后那一段还挂着——它不是由这一跳调用加进去的，差异断言的红点找错了地方"
    assert stripped != normal, "前置：摘掉之前它确实给 prompt 加了东西"
    assert lesson not in stripped

    # 集合口径的负控：把 tactical 放进集合，读侧那道过滤必须跟着放行。
    # 注意 patch 的靶子是**模块属性** `core.context_manager.EXPERIENCE_ELIGIBLE_SCOPES`
    # 而不是类属性——渲染代码里那个名字走的是模块全局，挂到类上不会生效，
    # 那样这条负控就成了"永远红"的假负控。
    _reset_tables()
    tactical_decision = _evaluated(goal_id=goal, scope="operational",
                                  title=f"{_PREFIX}被改成 tactical 的那条")
    ds.link_experience(tactical_decision, "放宽集合之后应当放行")
    _raw_update_scope(tactical_decision, "tactical")
    assert cm._build_goal_lesson_context(goal) == "", "前置：默认集合里不含 tactical"
    widened = ds.EXPERIENCE_ELIGIBLE_SCOPES | {"tactical"}
    with patch("core.context_manager.EXPERIENCE_ELIGIBLE_SCOPES", widened):
        allowed = cm._build_goal_lesson_context(goal)
    assert "放宽集合之后应当放行" in allowed, \
        "放宽集合还被挡着——上面那条筛选用例是恒真的"
    assert cm._build_goal_lesson_context(goal) == "", "with 退出之后过滤该恢复原样"
    print("[OK] negative controls: the injection call and the set both really do bite")


def test_read_side_scope_filter_has_its_own_teeth():
    """tactical 的已链接行不注入：写侧守卫挡过一次，读侧按裁决 2 再挡一次

    这一条钉的是**读侧自己**的过滤，不是复述 4a 的守卫——所以那行必须用裸 SQL 造出来
    （写路径永远产不出这个形状）。
    """
    _reset_tables()
    decision_id, experience_id = _flow_back("tactical 不该进闭环的那一句", goal_id=_GOAL_A)
    _raw_update_scope(decision_id, "tactical")

    conn = get_db()
    try:
        row = conn.execute("SELECT decision_id FROM experiences WHERE id = ?",
                           (experience_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["decision_id"] == decision_id, \
        "前置：那一行确实链接着，只是来路的档被读侧挡下"
    assert _candidates() == [], "读侧的集合过滤没挡住 tactical"
    assert ContextManager()._build_goal_lesson_context(_GOAL_A) == ""
    print("[OK] the read-side filter blocks tactical on its own")


def test_only_the_newest_lesson_is_injected_for_a_goal():
    """最多 1 条：同一目标回流三次，片段里只有最近那一句

    把三条都写成同一秒（默认时间戳就是秒级），靠 id 倒序取最后一条——
    「注入 3 条」与「只注入 1 条」这两种实现在这里必然分叉。
    """
    _reset_tables()
    _flow_back("最早那一句教训", goal_id=_GOAL_A, title=f"{_PREFIX}第一条")
    _flow_back("中间那一句教训", goal_id=_GOAL_A, title=f"{_PREFIX}第二条")
    _, last = _flow_back("最近那一句教训", goal_id=_GOAL_A, title=f"{_PREFIX}第三条")
    _force_created_at(last, "2099-01-01 00:00:00")

    section = ContextManager()._build_goal_lesson_context(_GOAL_A)
    assert "最近那一句教训" in section
    assert "最早那一句教训" not in section and "中间那一句教训" not in section, \
        f"多注入了一条：{section!r}"
    assert section.count(_BODY_PREFIX) == 1
    assert section.count("## 🧭") == 1
    print("[OK] at most one lesson reaches the prompt")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== experience injection test (P7 Phase 4b) ===\n")
    setup()
    test_candidate_query_follows_the_one_way_link()
    test_candidate_query_refuses_empty_keys_and_limits()
    test_candidate_query_skips_rows_without_a_decision_link()
    test_candidate_query_needs_a_non_empty_lesson()
    test_candidate_query_orders_by_created_at_then_id()
    test_all_three_eligible_scopes_are_candidates()
    test_lesson_section_carries_only_that_one_column()
    test_lesson_section_is_capped_by_construction()
    test_goal_lesson_context_returns_empty_for_no_candidate()
    # 判据①：致命的那一条，放在数据形状已经稳定之后跑
    test_injection_changes_the_prompt_for_the_same_state_and_question()
    test_read_side_scope_filter_has_its_own_teeth()
    test_only_the_newest_lesson_is_injected_for_a_goal()
    # 负控会真的放宽集合与摘掉调用，放在主断言之后
    test_negative_control_injection_call_is_what_makes_the_difference()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
