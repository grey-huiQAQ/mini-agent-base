# -*- coding: utf-8 -*-
"""
项目状态采集（project_states，P7 第一闭环 Phase 2）模块测试（离线：只读写临时库，文件只读）

覆盖（逐条对应拆分文档 Phase 2 的验收判据，负控另列）：
- 独立测试数据库 + 新表：project_states 建表、6 个字段一个都不省
- **顺序反转的第 1 步可核对**：DECISION_FIELDS 里每个字段都有「谁消费它」的说明；
  采集到的每一条四元组指名的 decision_field 都必须在字段集合内，**指不出来的项数＝0**
- 判据①「自动段不含任何模型生成的文字」：拆成两条可执行的断言——每条 fact 的 source
  非空（无 source 的 fact 计数＝0），且 value 是 `k=v` 机器形态、不含句读与判断用词
- 判据②「四类分离」：同一条内容以四种 kind 传入，四条都在，谁也没覆盖谁
- 判据③「落盘是追加」：写两次行数＝2，且第 1 行逐字节未变
- 判据④「库为空／表缺失时不抛异常，输出未知而不是猜」：表不存在 → unknown，0 行 → unknown
- 写路径规则：fact 空 source 被拒、kind 不在五类被拒、指不出字段被拒、content 空被拒
- goal_id 三种形状：唯一 active 给值／0 个登记 unknown／多个登记 unknown（不替人挑）
- test_runs 那条真实缺口：只给 id 最大的一行，并登记「分不清全量与子集」的 unknown
- agent_resource_log 没有成败列：近 N 天调用次数可采，「失败几次」必须是 unknown
- 上一条 decisions：0 行时 decision_scope 走 unknown，有行时给范围与间隔
- CAPABILITY_MAP：carrier 是**非空列表**，里面每一条路径逐条验真实存在（指向已消失模块
  的能力表就是假账）；键名不许等于任何一个 carrier 的模块名（Fix C 把键改成语义名，
  键名退回模块名＝词表粒度重新交给目录结构决定）
- 只读纪律：采集＋落盘前后，八张既有表的逐行签名一字不差
- 三条负控（每条都证明对应断言「换个实现就会红」）：
  · 摘掉 fact-source 守卫 → 无 source 的 fact 真的落进 State（判据①不是恒真）
  · 改成「先删后插」的单行状态写法 → 行数与第 1 行两条断言同时抓到（判据③不是恒真）
  · 改成「以内容为键的字典」存储 → 四类塌成一条（判据②不是恒真）
  · 只读断言的比对载体对写入敏感（否则那条「八表不变」是恒真的）

**阶段一（2026-10-11，决策模型改造）在本文件加的是采集契约的第四位与 unknown 的码**：
- 采集契约由三元组变**四元组** `(decision_field, value, source, evidence)`：第四位由
  **生产者自己声明**这条读数算不算「项目活动的证据」，复盘侧闸门②只读它、不比条数。
  口径（设计席拍板 5）：goal_tasks／notes／decisions 三族非零 ⇒ `activity`；同一族读到
  但为零 ⇒ `activity_empty`；test_runs／台账／llm_calls／pending_approvals／goal_id
  ⇒ `context`。所以本文件里「三族非零给 activity、读到零给 activity_empty、
  卫生类即使非零也给 context」三条都要验——标错了位，闸门②就在读一个假信号。
- `evidence` 与 unknown 的 `code` 都是**给了才写进 dict** 的可选键：人工段条目与
  历史快照没有它们。`test_evidence_and_code_round_trip_through_json` 验这两个键能过
  `sort_keys=True` 的落盘与读回，且读回的形状与写进去的逐键相等。
- `_collect_goal` 的两个「选不出目标」分支各带一个码（`goal_none`／`goal_ambiguous`），
  它们是复盘分流的唯一判据——那条分流**不解析中文句子**，所以码不在这里，形状就立不住。
"""
import io
import json
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = Path(__file__).resolve().parent.parent

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_connection
from core import project_state as ps
from core import decision_store as ds

# project_states 的字段清单（「字段不许省」的机器可核形态：与建表语句逐列对齐）
_STATE_COLUMNS = ["id", "snapshot_at", "goal_id", "items", "unknowns", "note"]

# 采集要读的既有表：只读纪律的核对对象，也是「表缺失」负控的操作对象
_READONLY_TABLES = [
    "goals", "goal_tasks", "test_runs", "agent_resource_log",
    "pending_approvals", "notes", "decisions", "decision_items",
]

# 拆分文档 Phase 2 点名的 8 个来源（逐个核对一个没漏）
_PLAN_SOURCES = [
    "goals", "goal_tasks", "test_runs", "checks/*_exemptions.json",
    "agent_resource_log", "pending_approvals", "notes", "decisions",
]


def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()


# ---------- 辅助 ----------

def _insert_goal(goal_id, status="active"):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO goals (goal_id, title, status) VALUES (?, ?, ?)",
            (goal_id, f"目标_{goal_id}", status),
        )
        conn.commit()
    finally:
        conn.close()


def _clear(table, where="1=1"):
    conn = get_connection()
    try:
        conn.execute(f"DELETE FROM {table} WHERE {where}")
        conn.commit()
    finally:
        conn.close()


def _insert_test_run(total_files=49, total_cases=619, timed_out_files=0,
                     duration_seconds=1216.2):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO test_runs (started_at, status, total_files, passed_files, "
            "failed_files, timed_out_files, total_cases, passed_cases, "
            "duration_seconds) VALUES (?, 'success', ?, ?, 0, ?, ?, ?, ?)",
            ("2026-10-06 14:01:10", total_files, total_files, timed_out_files,
             total_cases, total_cases, duration_seconds),
        )
        conn.commit()
    finally:
        conn.close()


def _row_bytes(table, row_id):
    """把一行原样读成字符串：逐字节比对的载体，绕开 JSON 反序列化的还原误差"""
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.dumps(dict(zip(cols, row)), ensure_ascii=False, sort_keys=True)


def _row_signature(table):
    """整表逐行签名（只读纪律的比对载体）"""
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        conn.close()
    return json.dumps([[str(v) for v in r] for r in rows], ensure_ascii=False)


def _count(table):
    conn = get_connection()
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _unknown_fields(pairs):
    """collect_auto_items() 返回的 unknown 是 (decision_field, reason, code) 三元组

    第三位 `code` 是阶段一（2026-10-11）加的，只有 `_collect_goal` 选不出目标那两条带值
    （goal_none／goal_ambiguous），其余一律 None——所以这里只取第一位，不按长度解包。
    """
    return [u[0] for u in pairs]


def _unknown_code(pairs, field):
    """取某个字段上登记的码（阶段一）；没有这一格就是 None

    按位取（`u[2]`）之前先验长度：`AUTO_UNSERVABLE` 那五条也归一成三元组，但**读侧**
    要能容忍当年只有二元组的旧写法——这里就是那句容忍的现场。
    """
    for u in pairs:
        if u[0] == field:
            return u[2] if len(u) > 2 else None
    return None


def _evidence(items, prefix):
    """按 value 前缀找一条采集项，取它的第四位（证据标记）

    刻意要求**恰好一条**：前缀写错或采集器改出了两条，都会在这里红，
    而不是让断言悄悄落在别的那一族上。
    """
    hits = [it for it in items if it[1].startswith(prefix)]
    assert len(hits) == 1, f"前缀 {prefix!r} 应恰好命中一条采集项，实得 {len(hits)} 条：{hits}"
    return hits[0][3]


def _insert_note(title="近笔记", created_offset="-1 day"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO notes (goal_id, title, created_at) "
                     "VALUES ('goal_x', ?, datetime('now', ?))",
                     (title, created_offset))
        conn.commit()
    finally:
        conn.close()


# ---------- 建表与字段（与批次 14 的 C8 同型：字段不许省）----------

def test_project_states_table_and_columns():
    conn = get_connection()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "project_states" in tables, "init_db() 没把 project_states 建出来"
        info = conn.execute("PRAGMA table_info(project_states)").fetchall()
    finally:
        conn.close()
    cols = [r[1] for r in info]
    assert cols == _STATE_COLUMNS, f"字段清单对不上：{cols} != {_STATE_COLUMNS}"
    notnull = {r[1]: r[3] for r in info}
    assert notnull["snapshot_at"] == 1, "snapshot_at 该 NOT NULL：没时间戳的快照无从排顺序"
    assert notnull["items"] == 1 and notnull["unknowns"] == 1, (
        "items / unknowns 该 NOT NULL——空集用 '[]' 表达，"
        "不用 NULL 表达「这列还没写过」，否则读侧要分两种空")
    print("[OK] project_states 建表与字段清单一致")


def test_plan_named_sources_are_all_covered():
    """拆分文档点名的 8 个来源一个没漏（漏了说明采集面比设计小）"""
    covered = set(ps.AUTO_COLLECTOR_SOURCES)
    assert covered == set(_PLAN_SOURCES), (
        f"来源集合与拆分文档不一致：缺 {sorted(set(_PLAN_SOURCES) - covered)}，"
        f"多 {sorted(covered - set(_PLAN_SOURCES))}")
    print("[OK] 八个来源逐个对上拆分文档")


# ---------- 第 1 步：Decision 需要的输入清单本身 ----------

def test_every_decision_field_has_a_consumer():
    """DECISION_FIELDS 与 DECISION_INPUT_WHY 必须一一对应。

    这条是「顺序反转」的结构证据：先有「谁消费这个字段」，才允许有采集器。
    将来谁加一个采集字段而没写清谁消费它，这里就红。
    在旧实现上：旧实现不存在，所以这条是**新契约的形状断言**，不是回归断言。
    """
    only_fields = set(ps.DECISION_FIELDS) - set(ps.DECISION_INPUT_WHY)
    only_why = set(ps.DECISION_INPUT_WHY) - set(ps.DECISION_FIELDS)
    assert not only_fields and not only_why, (
        f"字段表与消费方表不一致：只在字段表 {only_fields}，只在消费方表 {only_why}")
    for field, why in ps.DECISION_INPUT_WHY.items():
        assert why and why.strip(), f"{field} 的「谁消费它」是空的"
        assert len(why) >= 8, f"{field} 的消费方说明短到等于没写：{why!r}"
    print("[OK] Decision 输入清单与消费方一一对应")


def test_collected_field_names_are_never_missing():
    """改动 4 的硬判据：每条采集项都指名它服务哪个 Decision 字段，指不出的＝0"""
    _insert_goal("goal_only_active")
    _insert_test_run()
    items, unknowns = ps.collect_auto_items()
    assert items, "临时库里放了数据，自动段不该是空的"
    assert all(len(it) == 4 for it in items), \
        f"采集项必须是 (decision_field, value, source, evidence) 四元组：{items}"
    # 第四位不是装饰：它是阶段一闸门②唯一的输入，取值必须落在声明过的三个值里
    bad_evidence = [it for it in items if it[3] not in ps.FACT_EVIDENCE_VALUES]
    assert bad_evidence == [], f"证据标记不在三个合法值里的采集项：{bad_evidence}"
    offenders = [it for it in items
                 if not (it[0] or "").strip() or it[0] not in ps.DECISION_FIELDS]
    assert len(offenders) == 0, f"指不出 Decision 字段的采集项：{offenders}"
    offenders_u = [u for u in unknowns
                   if not (u[0] or "").strip() or u[0] not in ps.DECISION_FIELDS]
    assert len(offenders_u) == 0, f"指不出 Decision 字段的 unknown 项：{offenders_u}"
    # 自动段只产 fact：推断要有支撑、assumption 只能人来登记，两者都只在人工段入口
    state = ps.build_project_state()
    auto_kinds = {i["kind"] for i in state["items"]}
    assert auto_kinds == {"fact"}, f"自动段只该产 fact，实得 {auto_kinds}"
    _clear("goals")
    _clear("test_runs")
    print("[OK] 每条采集项都指得出 Decision 字段（指不出的＝0）")


# ---------- 判据①：自动段不含模型生成的文字 ----------

def test_no_fact_without_source():
    _insert_goal("goal_src_1")
    items, _ = ps.collect_auto_items()
    missing = [it for it in items if not (it[2] or "").strip()]
    assert len(missing) == 0, f"无 source 的采集项必须为 0，实得 {missing}"
    for field, value, source, _evidence in items:
        assert ("表" in source) or ("checks/" in source), \
            f"source 要落到一个具体的表或文件，实得 {source!r}"
    _clear("goals")
    print("[OK] 自动段每条 fact 都有 source")


def test_auto_values_are_machine_shaped():
    """value 是 `k=v` 形态而不是句子——「自动段里出现自然语言总结句，即为失败」。

    这条断言**能红**：谁哪天把 fact 写成「项目整体健康，但测试有点不稳」，
    句读先撞上；不写句读，「整体／建议／因为」这类判断用词也在被禁集合里。
    """
    _insert_goal("goal_shape_1")
    _insert_test_run()
    items, _ = ps.collect_auto_items()
    banned_marks = ["。", "！", "？", ";", "；", "\n"]
    banned_words = ["整体", "比较", "看起来", "建议", "因为", "所以", "健康", "不稳", "可能"]
    for field, value, source, _evidence in items:
        if field == ps.DECISION_FIELD_GOAL_ID:
            continue  # goal_id 的值是标识符本身，不是 kv 形态
        for mark in banned_marks:
            assert mark not in value, f"value 里出现了句读 {mark!r}：{value!r}"
        for word in banned_words:
            assert word not in value, f"value 里出现了判断用词 {word!r}：{value!r}"
        assert "=" in value, f"value 不是 k=v 形态：{value!r}"
    _clear("goals")
    _clear("test_runs")
    print("[OK] 自动段取值是机器形态的 k=v，没有总结句")


def test_unknowns_record_missing_inputs_not_guesses():
    """找不到来源的必须登记成 unknown，不许编一个值填进 items"""
    _clear("goals")
    items, unknowns = ps.collect_auto_items()
    goal_items = [it for it in items if it[0] == ps.DECISION_FIELD_GOAL_ID]
    assert goal_items == [], f"goals 表为空却给出了 goal_id：{goal_items}"
    assert ps.DECISION_FIELD_GOAL_ID in _unknown_fields(unknowns), \
        f"goal_id 读不出来却没登记 unknown：{unknowns}"
    for u in unknowns:
        assert (u[1] or "").strip(), f"unknown 没写原因：{u}"
    # 结构上提供不了的五个字段，每一条都在（这是设计，不是遗漏）
    for field, _reason in ps.AUTO_UNSERVABLE:
        assert field in _unknown_fields(unknowns), \
            f"{field} 应作为 unknown 登记（采集层不该代填），实得 {unknowns}"
    print("[OK] 读不出来的字段登记为 unknown，items 里没有凭空值")


# ---------- 判据②：四类分离 ----------

def test_same_content_under_different_kinds_does_not_overwrite():
    content = "每周复盘一次足够"
    state = ps.build_project_state(
        goal_id="goal_kinds",
        manual_items=[
            ("items.user_report", "user_report", content, "本会话 2026-10-06"),
            ("items.inference", "inference", content, "items.fact: test_runs 每周 1 行"),
            ("items.assumption", "assumption", content, None),
            ("items.unknown", "unknown", content, None),
        ],
    )
    same = [i for i in state["items"] if i["content"] == content]
    assert len(same) == 4, f"同内容应四条都在，实得 {len(same)}：{same}"
    kinds = sorted(i["kind"] for i in same)
    assert kinds == sorted(["user_report", "inference", "assumption", "unknown"]), kinds
    state_id = ps.save_project_state(state)
    stored = ps.get_project_state(state_id)
    stored_same = [i for i in stored["items"] if i["content"] == content]
    assert len(stored_same) == 4, f"落库后合成了 {len(stored_same)} 条：{stored_same}"
    _clear("project_states")
    print("[OK] 同一内容以四类传入，谁也没覆盖谁")


def test_negative_control_items_as_dict_collapses():
    """负控：判据②那条断言在「以内容为键的字典」这种存储下必须红（实得 1 条而非 4 条）

    按内容合键是这份数据最容易被写坏的形状（它看上去省空间），所以把它显式跑一遍：
    同一条 content 塌成一条，四类里只剩最后写入的那一类。真把 items 存成字典，
    test_same_content_under_different_kinds_does_not_overwrite 当场就红。
    """
    content = "同样一句话"
    state = ps.build_project_state(goal_id="goal_dict_nc", manual_items=[
        ("items.user_report", "user_report", content, "本会话"),
        ("items.inference", "inference", content, "items.fact: x=1"),
        ("items.assumption", "assumption", content, None),
        ("items.unknown", "unknown", content, None),
    ])
    as_list = [i for i in state["items"] if i["content"] == content]
    assert len(as_list) == 4, f"当前列表形状应留四条：{as_list}"
    as_dict = {i["content"]: i for i in state["items"] if i["content"] == content}
    assert len(as_dict) == 1, "字典形状既然只留一条，判据②的 len==4 就有牙齿"
    assert as_dict[content]["kind"] == "unknown", "字典形状会把先写的三类静默吃掉"
    _clear("project_states")
    print("[OK] 负控：字典式存储会让同一条内容塌成一类")


def test_manual_item_kind_is_validated():
    for bad_kind in ["facts", "knowledge", "", "revision"]:
        try:
            ps.validate_state_item("items.fact", bad_kind, "内容", "notes 表")
        except ValueError:
            continue
        raise AssertionError(f"kind={bad_kind!r} 应该被拒（revision 不在五类里）")
    try:
        ps.validate_state_item("items.whatever", "fact", "内容", "notes 表")
        raise AssertionError("指不出 Decision 字段的条目应该被拒")
    except ValueError:
        pass
    try:
        ps.validate_state_item("items.fact", "fact", "   ", "notes 表")
        raise AssertionError("空内容应该被拒")
    except ValueError:
        pass
    print("[OK] 人工段入口逐条校验 kind / 字段 / 内容")


# ---------- 判据①的写路径守卫与负控 ----------

def test_fact_without_source_is_refused():
    try:
        ps.validate_state_item("items.fact", "fact", "goal_tasks[total=10]", None)
        raise AssertionError("fact 空 source 应该被拒")
    except ValueError as e:
        assert "source" in str(e), f"报错信息要说清是 source 缺失：{e}"
    # 非 fact 类不强制 source：assumption 的定义就是「没有证据」，硬要填等于逼人造出处
    ok = ps.validate_state_item("items.assumption", "assumption", "每周一次足够", None)
    assert ok["source"] is None, f"assumption 的 source 应保持 None：{ok}"
    print("[OK] fact 缺 source 被拒，assumption 不强制 source")


def test_negative_control_source_rule_has_teeth():
    """负控：摘掉 validate_state_item 的 source 规则（守卫没写时模块的形状），
    判据①「无 source 的 fact＝0」那条断言必须抓到东西——抓不到就说明它是恒真断言。
    """
    def _rule_without_source_check(decision_field, kind, content, source,
                                   evidence=None):
        # 第五位是阶段一加的（自动段那条路径按位置传 5 个实参），这里必须收下它才跑得动；
        # 收下但**不写进条目**正是本负控要的：它摘的是 source 守卫，不是证据标记。
        if decision_field not in ps.DECISION_FIELDS:
            raise ValueError(f"字段不在集合内：{decision_field}")
        if kind not in ps.VALID_ITEM_KINDS:
            raise ValueError(f"非法类别：{kind}")
        if not (content or "").strip():
            raise ValueError("内容为空")
        return {"decision_field": decision_field, "kind": kind,
                "content": content, "source": source or None}

    original = ps.validate_state_item
    ps.validate_state_item = _rule_without_source_check
    try:
        state = ps.build_project_state(
            goal_id="goal_nc",
            manual_items=[("items.fact", "fact", "health=good", None)])
        missing = [i for i in state["items"]
                   if i["kind"] == "fact" and not (i["source"] or "").strip()]
        assert len(missing) == 1, (
            "摘掉守卫后本应留下一条无 source 的 fact，实得 0 条——"
            "那说明 test_no_fact_without_source 是恒真的")
    finally:
        ps.validate_state_item = original
    print("[OK] 负控：摘掉 source 守卫，无 source 的 fact 真的落进了 State")


# ---------- 判据③：追加，不改历史 ----------

def test_save_appends_and_leaves_first_row_untouched():
    _insert_goal("goal_append")
    state_a = ps.build_project_state(goal_id="goal_append", note="第一次")
    id_a = ps.save_project_state(state_a)
    before = _row_bytes("project_states", id_a)
    assert before is not None, "第一行没写进去"

    # 第二次：note 不同，且既有表数据也变了（两次内容不可能相同）
    conn = get_connection()
    try:
        conn.execute("INSERT INTO notes (goal_id, title) VALUES ('goal_append', '中间多一条笔记')")
        conn.commit()
    finally:
        conn.close()
    state_b = ps.build_project_state(goal_id="goal_append", note="第二次")
    id_b = ps.save_project_state(state_b)
    assert id_b != id_a, "第二次写入复用了同一行（追加被写成了覆盖）"

    after = _row_bytes("project_states", id_a)
    assert after == before, f"第 1 行被改了：\n改前 {before}\n改后 {after}"
    assert _count("project_states") == 2, f"写两次应是 2 行，实得 {_count('project_states')}"
    latest = ps.latest_project_state(goal_id="goal_append")
    assert latest["id"] == id_b, "latest 应取 id 最大的那一行"
    assert latest["note"] == "第二次"
    assert ps.get_project_state(id_a)["note"] == "第一次", "历史行应原样读回"
    _clear("project_states")
    _clear("notes")
    _clear("goals")
    print("[OK] 落盘是追加：两行并存且第 1 行逐字节未变")


def test_negative_control_overwrite_style_is_caught():
    """负控：换成「当前状态只留一份」的写法（先删后插），判据③两条断言都必须抓到

    这条针对的是**另一种合理设计**——UPSERT 式单行状态表。它看着更干净，但会让
    「当时读到的是什么」失去痕迹，而 Phase 4a 要评的正是这件事。负控要证明的不是
    「当前实现对」，而是「断言抓得住那种写法」。
    """
    _clear("project_states")
    _insert_goal("goal_nc_overwrite")
    state1 = ps.build_project_state(goal_id="goal_nc_overwrite", note="一")
    state2 = ps.build_project_state(goal_id="goal_nc_overwrite", note="二")

    def _overwrite_style(state):
        conn = get_connection()
        try:
            conn.execute("DELETE FROM project_states WHERE goal_id = ?",
                         (state.get("goal_id"),))
            cursor = conn.execute(
                "INSERT INTO project_states (snapshot_at, goal_id, items, unknowns, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (state["snapshot_at"], state.get("goal_id"),
                 json.dumps(state["items"], ensure_ascii=False, sort_keys=True),
                 json.dumps(state["unknowns"], ensure_ascii=False, sort_keys=True),
                 state.get("note")))
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    original = ps.save_project_state
    ps.save_project_state = _overwrite_style
    try:
        first = ps.save_project_state(state1)
        before = _row_bytes("project_states", first)
        assert before is not None, "第一次写入没落库，负控无法继续"
        ps.save_project_state(state2)
        row_count = _count("project_states")
        after = _row_bytes("project_states", first)
        # 这两条就是「判据③的两条断言换成那种写法会得到的结果」：
        assert row_count == 1, (
            f"先删后插本该把行数压回 1，实得 {row_count}——负控自身没生效")
        assert after is None, (
            "先删后插本该让第 1 行消失，它还在——那说明 test_save_appends… 里的"
            "逐字节比对是恒真的")
    finally:
        ps.save_project_state = original
    _clear("project_states")
    _clear("goals")
    print("[OK] 负控：先删后插的写法被判据③抓到")


# ---------- 判据④：库为空／表缺失时输出「未知」而不是猜 ----------

def test_missing_tables_yield_unknowns_not_exceptions():
    """把八张既有表全 DROP 掉：采集必须不抛异常，且每处缺失都落成 unknown"""
    conn = get_connection()
    try:
        for table in _READONLY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    finally:
        conn.close()
    state = ps.build_project_state()  # 不许抛
    assert isinstance(state["items"], list) and isinstance(state["unknowns"], list)
    fields = [u["decision_field"] for u in state["unknowns"]]
    assert ps.DECISION_FIELD_GOAL_ID in fields, "goals 表没了，goal_id 必须是 unknown"
    assert "items.fact" in fields, "其余表没了，事实项必须是 unknown"
    assert ps.DECISION_FIELD_SCOPE in fields, "decisions 表没了，范围必须是 unknown"
    reasons = " ".join(u["reason"] for u in state["unknowns"])
    assert "读不出来" in reasons or "不存在" in reasons, f"unknown 要说清是没读到：{reasons}"
    for item in state["items"]:
        assert (item["source"] or "").strip(), f"空库条件下出现了无 source 的 fact：{item}"
    state_id = ps.save_project_state(state)
    assert ps.get_project_state(state_id)["goal_id"] is None
    init_db()  # 把表建回来，后面的用例还要用
    print("[OK] 表缺失时不抛异常，缺失处一律登记 unknown")


def test_empty_test_runs_and_decisions_yield_unknowns():
    """表在、0 行，也是"没读到东西"而不是"读到了 0"——两种情形都要登记 unknown"""
    _clear("test_runs")
    _clear("decisions")
    items, unknowns = ps.collect_auto_items()
    assert not [it for it in items if it[2].startswith("test_runs")], "空表不该产 fact"
    assert not [it for it in items if it[0] == ps.DECISION_FIELD_SCOPE], \
        "decisions 空表不该产范围依据"
    scope_unknown = [u for u in unknowns if u[0] == ps.DECISION_FIELD_SCOPE
                     and "decisions" in u[1]]
    assert scope_unknown, f"decisions 0 行应登记 scope 的 unknown：{unknowns}"
    assert any("0 行" in u[1] for u in unknowns), \
        f"空表要写明是 0 行（区别于表不存在）：{[u[1] for u in unknowns]}"
    print("[OK] 空表登记为 unknown，不拿 0 冒充「读到且没有」")


# ---------- 各采集项的真实形状 ----------

def test_goal_id_three_shapes():
    """0 个／1 个／多个 active 目标：只有 1 个才给值，其余两种都登记 unknown"""
    _clear("goals")

    items, unknowns = ps.collect_auto_items()
    assert [it for it in items if it[0] == ps.DECISION_FIELD_GOAL_ID] == []
    assert ps.DECISION_FIELD_GOAL_ID in _unknown_fields(unknowns), "0 个 active 应登记 unknown"
    # 阶段一：复盘按**码**分流，不解析这句中文。0 个与多个必须是两个不同的码，
    # 否则「引导建立上下文」与「先选目标」这两个形状在数据上就长得一样。
    assert _unknown_code(unknowns, ps.DECISION_FIELD_GOAL_ID) == ps.UNKNOWN_CODE_GOAL_NONE, \
        f"0 个 active 的码不是 goal_none：{[u for u in unknowns if u[0] == ps.DECISION_FIELD_GOAL_ID]}"

    _insert_goal("goal_sole")
    items, unknowns = ps.collect_auto_items()
    got = [it for it in items if it[0] == ps.DECISION_FIELD_GOAL_ID]
    assert len(got) == 1 and got[0][1] == "goal_sole", f"唯一目标应给值：{got}"
    assert got[0][3] == ps.EVIDENCE_CONTEXT, (
        f"goal_id 只说这份快照关于谁，不证明项目在推进，不许标成活动证据：{got[0]}")
    assert ps.build_project_state()["goal_id"] == "goal_sole", "快照自己的 goal_id 应跟着解出来"

    _insert_goal("goal_second")
    items, unknowns = ps.collect_auto_items()
    got = [it for it in items if it[0] == ps.DECISION_FIELD_GOAL_ID]
    assert got == [], f"两个 active 目标时采集层不该挑一个：{got}"
    pair = [u for u in unknowns if u[0] == ps.DECISION_FIELD_GOAL_ID]
    assert pair and "2" in pair[0][1], f"unknown 要写清是几个：{pair}"
    assert _unknown_code(unknowns, ps.DECISION_FIELD_GOAL_ID) == ps.UNKNOWN_CODE_GOAL_AMBIGUOUS, \
        f"多个 active 的码不是 goal_ambiguous：{pair}"
    # 调用方显式指定时，指定值优先，且来源要写明是「指定的」而不是「读出来的」
    items, unknowns = ps.collect_auto_items(goal_id="goal_second")
    got = [it for it in items if it[0] == ps.DECISION_FIELD_GOAL_ID]
    assert got and got[0][1] == "goal_second", f"显式 goal_id 应被采入：{got}"
    assert "指定" in got[0][2], f"来源要写明是调用方指定的：{got[0][2]}"
    _clear("goals")
    print("[OK] goal_id 的三种形状：给值／登记 unknown／显式指定优先")


def test_test_run_latest_row_and_the_full_or_subset_gap():
    """test_runs 只给 id 最大的一行，并如实登记「分不清全量与子集」这条缺口"""
    _clear("test_runs")
    _insert_test_run(total_files=2, total_cases=31)      # 一次 --files 子集跑
    _insert_test_run(total_files=49, total_cases=619)    # 之后的一次全量跑
    items, unknowns = ps.collect_auto_items()
    runs = [it for it in items if it[2].startswith("test_runs")]
    assert len(runs) == 1, f"test_runs 只应给一行：{runs}"
    assert "total_files=49" in runs[0][1], f"应取 id 最大的那一行：{runs[0][1]}"
    gap = [u for u in unknowns if "全量" in u[1]]
    assert gap and gap[0][0] == "items.fact", "「分不清全量与子集」必须登记为 unknown"
    _clear("test_runs")
    print("[OK] test_runs 取最新一行，且把「分不清全量/子集」登记为 unknown")


def test_resource_log_has_no_success_column():
    """agent_resource_log 能数调用次数，数不出失败次数——后者必须是 unknown"""
    _clear("agent_resource_log")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO agent_resource_log (resource_type, operation, token_estimate, "
            "created_at) VALUES ('llm', 'chat_sync', 100, datetime('now','-1 day'))")
        conn.execute(
            "INSERT INTO agent_resource_log (resource_type, operation, token_estimate, "
            "created_at) VALUES ('llm', 'stream_chat', 200, datetime('now','-300 day'))")
        conn.commit()
    finally:
        conn.close()
    items, unknowns = ps.collect_auto_items()
    recent = [it for it in items if "llm_calls_last" in it[1]]
    assert len(recent) == 1 and "rows=1" in recent[0][1], \
        f"窗口内应只数到 1 行（300 天前那条不算）：{recent}"
    assert any("失败" in u[1] for u in unknowns), \
        "没有成败列这件事必须登记为 unknown，而不是报「失败 0 次」"
    _clear("agent_resource_log")
    print("[OK] agent_resource_log：次数可采、失败次数不可采（后者进 unknown）")


def test_pending_and_notes_and_tasks_shapes():
    """三条计数型采集项的形状：表空也给值（"读到且没有"是事实，不是没读到）"""
    _clear("pending_approvals")
    _clear("notes")
    _clear("goal_tasks")
    conn = get_connection()
    try:
        conn.execute("INSERT INTO goal_tasks (goal_id, title, status) "
                     "VALUES ('goal_x', '任务甲', 'pending')")
        conn.execute("INSERT INTO goal_tasks (goal_id, title, status) "
                     "VALUES ('goal_x', '任务乙', 'done')")
        conn.execute("INSERT INTO pending_approvals (task_type, task_title, goal_id, "
                     "status, created_at) VALUES ('note', '待批甲', 'goal_x', 'pending', "
                     "datetime('now','-3 day'))")
        conn.execute("INSERT INTO notes (goal_id, title, created_at) "
                     "VALUES ('goal_x', '近笔记', datetime('now','-1 day'))")
        conn.execute("INSERT INTO notes (goal_id, title, created_at) "
                     "VALUES ('goal_x', '远笔记', datetime('now','-365 day'))")
        conn.commit()
    finally:
        conn.close()
    items, _ = ps.collect_auto_items()
    tasks = [it for it in items if it[1].startswith("goal_tasks[")]
    assert len(tasks) == 1, f"goal_tasks 应有一项：{tasks}"
    assert "done=1, pending=1, total=2" in tasks[0][1], tasks[0][1]
    pend = [it for it in items if it[1].startswith("pending_approvals[")]
    assert len(pend) == 1 and "pending=1" in pend[0][1], pend
    assert "oldest_age_days=3" in pend[0][1], f"年龄应按天给出：{pend[0][1]}"
    notes = [it for it in items if it[1].startswith("notes[")]
    assert len(notes) == 1 and "new=1" in notes[0][1] and "total=2" in notes[0][1], notes
    # 阶段一的口径（设计席拍板 5）：**只有项目活动计数**算实质依据，非零才算。
    # 这三条一起把「哪一族算活动」钉在采集侧——复盘那边只读这个标记，别的什么都不看。
    assert _evidence(items, "goal_tasks[") == ps.EVIDENCE_ACTIVITY, "goal_tasks 非零应标 activity"
    assert _evidence(items, "notes[") == ps.EVIDENCE_ACTIVITY, "notes 有行应标 activity"
    assert _evidence(items, "pending_approvals[") == ps.EVIDENCE_CONTEXT, (
        "待审批队列是「等人点头」的读数，拍板把它排除在实质依据之外")
    for table in ("pending_approvals", "notes", "goal_tasks"):
        _clear(table)
    # 空表也要给值，且是 0（区别于「读不出来」）
    items, unknowns = ps.collect_auto_items()
    empty_notes = [it for it in items if it[1].startswith("notes[")]
    assert empty_notes and "new=0" in empty_notes[0][1], empty_notes
    # 读到零 **不等于** 读不出来：标 empty 而不是不标，也不标成 activity。
    # 这一格标错，闸门②就把「这个目标下什么都没动」读成「有依据可判」。
    assert _evidence(items, "notes[") == ps.EVIDENCE_ACTIVITY_EMPTY
    assert _evidence(items, "goal_tasks[") == ps.EVIDENCE_ACTIVITY_EMPTY
    print("[OK] 三条计数型采集项形状正确，空表给 0 而不给 unknown")


def test_note_outside_the_window_is_not_evidence():
    """窗口内零篇、全表有行 ⇒ `activity_empty`（设计席 2026-10-11 裁决 1 收窄出来的那条漏口）

    这条是漏口本身的守卫：改前的口径是「`new` 或 `total` 非零都算 activity」，于是「写过
    一篇很久以前的笔记」在当时足以让闸门②放行，而周复盘要回答的是「最近在推进吗」。
    收窄的是**证据标记**，不是那一格读数——`total` 必须仍然逐字出现在快照文本里，
    否则复盘就从「看见它很久没动」变成「根本不知道它有过产出」，那是另一种假话。
    """
    for table in ("notes", "goal_tasks", "pending_approvals"):
        _clear(table)
    _insert_note("去年写过一篇", created_offset="-365 day")
    items, _ = ps.collect_auto_items()
    notes = [it for it in items if it[1].startswith("notes[")]
    assert len(notes) == 1, f"notes 应恰好一条采集项：{notes}"
    assert "new=0" in notes[0][1] and "total=1" in notes[0][1], notes[0][1]
    assert _evidence(items, "notes[") == ps.EVIDENCE_ACTIVITY_EMPTY, (
        "窗口外的一行笔记又被标成实质依据了：`_collect_recent_notes` 里那句 `or total` 回来了")
    _clear("notes")
    print("[OK] 全表有行但窗口内为零：标 activity_empty，total 仍留在文本里，且不给后面的用例留脏行")


def test_previous_decision_feeds_scope():
    """上一条 decisions 服务的是 decision_scope：给范围与隔了多少天，不给结论"""
    _clear("decisions")
    decision_id = ds.create_decision("上次定的范围", goal_id=None,
                                     decision_scope="strategic")
    items, unknowns = ps.collect_auto_items()
    scope = [it for it in items if it[0] == ps.DECISION_FIELD_SCOPE]
    assert len(scope) == 1, f"decision_scope 应由上一条决策喂一条：{scope}"
    assert f"id={decision_id}" in scope[0][1], scope[0][1]
    assert "decision_scope=strategic" in scope[0][1], scope[0][1]
    assert "age_days=0" in scope[0][1], f"刚建的决策应算 0 天：{scope[0][1]}"
    assert "status=proposed" in scope[0][1], scope[0][1]
    assert not [u for u in unknowns if u[0] == ps.DECISION_FIELD_SCOPE
                and "上一条" in u[1] or u[0] == ps.DECISION_FIELD_SCOPE
                and "decisions" in u[1]], "读到了上一条决策就不该再为它登记 unknown"
    _clear("decisions")
    _clear("decision_items")
    print("[OK] 上一条决策喂 decision_scope：范围／状态／隔了多少天，无一句判断")


def test_debt_ledgers_read_from_files():
    """台账那一项：按通配扫描 checks/*_exemptions.json，值与文件实际条数逐本一致"""
    items, unknowns = ps.collect_auto_items()
    ledgers = [it for it in items if it[1].startswith("exemptions[")]
    assert len(ledgers) == 1, f"台账应有一项：{ledgers}"
    paths = sorted((_ROOT / "checks").glob("*_exemptions.json"))
    assert paths, "checks/ 下没找到任何台账文件（测试环境不对）"
    for path in paths:
        with io.open(str(path), "r", encoding="utf-8-sig") as f:
            entries = json.load(f)
        assert isinstance(entries, list), f"{path.name} 不是数组，本用例的口径不成立"
        name = path.name[: -len("_exemptions.json")]
        assert f"{name}={len(entries)}" in ledgers[0][1], \
            f"{name} 的条数与文件不一致：{ledgers[0][1]} vs {len(entries)}"
    base = [it for it in items if it[1].startswith("debt_baseline_total")]
    assert base, "棘轮基线合计应是一项 fact"
    assert base[0][2] == "checks/debt_baseline.json（ledgers 求和）", base[0][2]
    print("[OK] 台账条数与文件逐本对上，基线合计有来源")


# ---------- CAPABILITY_MAP ----------

def test_capability_map_carriers_exist():
    """能力表的每条 carrier 路径必须真实存在；carrier 是**列表**（Fix C）。

    在旧实现上这条无从谈起（旧实现不存在）；它的牙齿来自「模块被改名或删掉时它红」，
    而这张表正是给 Phase 3a 那一格「需要的能力」当闭集用的——闭集指向不存在的模块，
    下游就会拿着一个谁也找不到的名字去降级。

    ⚠️ carrier 从单路径改列表后，`carrier.startswith("/")` 那类写法会**悄悄失效而不是报错**
    （str.startswith 换成 list.startswith 是 AttributeError，还能红；但
    `(_ROOT / carrier).exists()` 传列表会 TypeError——所以逐条摊开不是可选项，是必须）。
    """
    assert ps.UNKNOWN_CAPABILITY == "unknown"
    assert ps.UNKNOWN_CAPABILITY not in ps.CAPABILITY_MAP, (
        "哨兵不该同时是一条真能力，否则 3a 输出「unknown」时分不出是没查到还是查到了")
    for key, entry in ps.CAPABILITY_MAP.items():
        assert key == key.strip().lower(), f"能力键名应是小写标识符：{key!r}"
        assert entry.get("label"), f"{key} 缺中文名"
        carriers = entry.get("carrier")
        assert isinstance(carriers, list) and carriers, (
            f"{key} 的 carrier 必须是非空列表（Fix C：一个语义能力天然由多个模块承载，"
            f"写成字符串会把「还由谁做」丢掉）：{carriers!r}")
        for carrier in carriers:
            assert isinstance(carrier, str), f"{key} 的 carrier 元素应是路径字符串：{carrier!r}"
            assert not carrier.startswith("/") and ".." not in carrier, \
                f"carrier 应是仓库相对路径：{carrier}"
            assert (_ROOT / carrier).exists(), f"{key} 的 carrier 不存在：{carrier}"
            stem = os.path.splitext(os.path.basename(carrier))[0]
            assert key != stem, (
                f"能力键名 {key!r} 又变回模块名了（carrier {carrier}）——Fix C 改这一格的"
                "理由就是词表粒度不该由目录结构决定")
    print("[OK] CAPABILITY_MAP 每条 carrier 路径都还在，且键名不是模块名")


# ---------- 阶段一（2026-10-11）：采集契约的第四位与 unknown 的码 ----------

def test_previous_decision_is_activity_but_test_run_is_not():
    """同一份快照里三族与卫生族的标记必须**分开**：都标 activity 等于闸门②形同虚设

    阶段一拍板 5 的口径是「只认 goal_tasks／notes／decisions 三族非零」。这条把三族之外
    最容易混进来的两族单独钉住：test_runs（跑过回归只说明仓库被验过）与
    agent_resource_log 的调用次数（花过配额不等于推进过项目）。
    """
    _clear("decisions")
    _clear("test_runs")
    decision_id = ds.create_decision("上一条：先只做每周复盘", decision_scope="operational")
    _insert_test_run()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO agent_resource_log (resource_type, operation, token_estimate, "
            "created_at) VALUES ('llm', 'chat_sync', 100, datetime('now','-1 day'))")
        conn.commit()
    finally:
        conn.close()
    items, _ = ps.collect_auto_items()
    assert _evidence(items, f"previous_decision[id={decision_id}") == ps.EVIDENCE_ACTIVITY, (
        "上一条 decisions 是三族之一，有行就该算活动证据")
    assert _evidence(items, "test_runs_last[") == ps.EVIDENCE_CONTEXT
    assert _evidence(items, "llm_calls_last") == ps.EVIDENCE_CONTEXT
    for table in ("decisions", "decision_items", "test_runs", "agent_resource_log"):
        _clear(table)
    print("[OK] 三族与卫生族的证据标记分开：decisions 算，test_runs／调用台账不算")


def test_evidence_and_code_round_trip_through_json():
    """两个新键都要过 `sort_keys=True` 的落盘与读回，且**给了才写**这条形状不变

    为什么单独立一条：闸门②与分流读的都是 `latest_project_state()` 回来的那一份，
    不是内存里的那一份。键在 JSON 往返里丢了，界面上「这一轮会请求补充」就成了读得到、
    复盘读不到的分裂；反过来，如果人工段条目被自动带上一个标记，闸门①与闸门②就串了。
    """
    _clear("goals")            # 0 个 active ⇒ goal_id 这一条 unknown 一定带码
    state = ps.build_project_state(
        manual_items=[("summary", "user_report", "这周的产出我没看", None)])
    facts = [i for i in state["items"] if i["kind"] == "fact"]
    manual = [i for i in state["items"] if i["kind"] != "fact"]
    assert facts and manual, f"夹具形状不对：fact {len(facts)}／人工段 {len(manual)}"
    assert all(i.get("evidence") in ps.FACT_EVIDENCE_VALUES for i in facts), (
        f"自动段有 fact 没带合法标记：{[i for i in facts if 'evidence' not in i]}")
    assert all("evidence" not in i for i in manual), (
        f"人工段条目被带上了证据标记，闸门②就会把人写的话当项目活动：{manual}")
    saved = ps.save_project_state(state)
    got = ps.get_project_state(saved)
    assert [i for i in got["items"] if i["kind"] == "fact"] == facts, (
        "自动段条目过了一次 JSON 往返就变了（新增键没被持久化）")
    read_facts = [i for i in got["items"] if i["kind"] == "fact"]
    assert all(i.get("evidence") == f.get("evidence")
               for i, f in zip(read_facts, facts)), "证据标记读回来的值与写入时不一致"
    goal_unknowns = [u for u in got["unknowns"] if u["decision_field"] == "goal_id"]
    assert goal_unknowns and goal_unknowns[0].get("code") == ps.UNKNOWN_CODE_GOAL_NONE, (
        f"unknown 的码没跟着落盘：{goal_unknowns}")
    assert all("code" not in u for u in got["unknowns"]
               if u["decision_field"] != "goal_id"), (
        "没有形状的 unknown 被塞了一个空码键——读侧就分不清「没有码」与「码是空串」")
    _clear("project_states")
    print("[OK] evidence 与 code 都能过 JSON 往返，未给的键保持缺失")


def test_illegal_evidence_marker_is_refused():
    """写路径上把标记的取值与「只有 fact 能带」两道都钉住（否则口径只在采集器里成立）"""
    ok = ps.validate_state_item("items.fact", "fact", "notes[total=1]", "notes 表",
                                ps.EVIDENCE_ACTIVITY)
    assert ok["evidence"] == ps.EVIDENCE_ACTIVITY, ok
    plain = ps.validate_state_item("items.fact", "fact", "notes[total=1]", "notes 表")
    assert "evidence" not in plain, f"没给标记却写出了这一格：{plain}"
    try:
        ps.validate_state_item("items.fact", "fact", "x=1", "notes 表", "important")
        raise AssertionError("非法的证据标记应该被拒")
    except ValueError as e:
        assert "证据标记" in str(e), f"报错没说是哪一格：{e}"
    try:
        ps.validate_state_item("items.assumption", "assumption", "周末才有空", None,
                               ps.EVIDENCE_CONTEXT)
        raise AssertionError("非 fact 的条目不该能带证据标记")
    except ValueError as e:
        assert "fact" in str(e), f"报错没说是因为只有 fact 能带：{e}"
    print("[OK] 非法标记与非 fact 带标记都被拒，不给标记时键保持缺失")


# ---------- 只读纪律：不写既有表 ----------

def test_collectors_never_write_existing_tables():
    """采集＋落盘前后，八张既有表的逐行签名一字不差（"只读既有表，不写"）"""
    _insert_goal("goal_readonly")
    _insert_test_run()
    before = {t: _row_signature(t) for t in _READONLY_TABLES}
    state = ps.build_project_state(goal_id="goal_readonly")
    state_id = ps.save_project_state(state)
    ps.latest_project_state()
    ps.get_project_state(state_id)
    after = {t: _row_signature(t) for t in _READONLY_TABLES}
    changed = [t for t in _READONLY_TABLES if before[t] != after[t]]
    assert changed == [], f"采集层写了既有表：{changed}"
    _clear("project_states")
    _clear("goals")
    _clear("test_runs")
    print("[OK] 八张既有表在采集前后逐表签名不变")


def test_negative_control_readonly_check_has_teeth():
    """负控：真往 notes 写一行，只读那条断言的比对载体必须变（否则"八表不变"是恒真的）"""
    before = _row_signature("notes")
    conn = get_connection()
    try:
        conn.execute("INSERT INTO notes (goal_id, title) VALUES ('goal_nc', '不该被写')")
        conn.commit()
    finally:
        conn.close()
    after = _row_signature("notes")
    assert before != after, "_row_signature 对写入不敏感，那条只读断言是恒真的"
    conn = get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE title = '不该被写'")
        conn.commit()
    finally:
        conn.close()
    assert _row_signature("notes") == before, "还原后签名应回到改前（比对载体可逆）"
    print("[OK] 负控：只读断言的比对载体对写入敏感")


# ---------- 落盘读回的形状 ----------

def test_saved_state_is_readable_and_json_typed():
    _insert_goal("goal_round_trip")
    state = ps.build_project_state(
        goal_id="goal_round_trip",
        manual_items=[("items.assumption", "assumption", "周末才有空跑", None)],
        note="第一轮快照")
    assert "+00:00" in state["snapshot_at"], \
        f"snapshot_at 必须是带偏移的 UTC（C9）：{state['snapshot_at']}"
    state_id = ps.save_project_state(state)
    got = ps.get_project_state(state_id)
    assert got["note"] == "第一轮快照"
    assert got["goal_id"] == "goal_round_trip"
    assert isinstance(got["items"], list) and isinstance(got["unknowns"], list)
    assert any(i["kind"] == "assumption" for i in got["items"])
    assert all(i["source"] for i in got["items"] if i["kind"] == "fact")
    # 坏 JSON 不抛异常（读路径的降级：静默给空列表，因为抛出来会让整份快照读不到）
    conn = get_connection()
    try:
        conn.execute("UPDATE project_states SET items = ? WHERE id = ?", ("{坏", state_id))
        conn.commit()
    finally:
        conn.close()
    degraded = ps.get_project_state(state_id)
    assert degraded["items"] == [], f"坏 JSON 应降级成空列表：{degraded['items']}"
    assert degraded["unknowns"], "unknowns 没被一起清掉，才说明只是 items 坏了"
    _clear("project_states")
    _clear("goals")
    print("[OK] 快照落库后原样读回，坏 JSON 静默降级")


def test_isolation_never_points_at_production_db():
    """本文件的库必须在临时目录里，且与生产库路径不同（写错的代价是不可逆的）"""
    assert "_test_tmp_" in _TMP_DB, f"测试库不在临时目录：{_TMP_DB}"
    prod = os.path.abspath(os.path.join("data", "memory.db"))
    assert os.path.abspath(_TMP_DB) != prod, f"测试库与生产库同路径：{_TMP_DB}"
    assert _database_module.DB_PATH != prod, \
        f"memory.database.DB_PATH 没被切走：{_database_module.DB_PATH}"
    print("[OK] 全程写的是临时库，生产库路径没被碰过")


TESTS = [
    test_project_states_table_and_columns,
    test_plan_named_sources_are_all_covered,
    test_every_decision_field_has_a_consumer,
    test_collected_field_names_are_never_missing,
    test_no_fact_without_source,
    test_auto_values_are_machine_shaped,
    test_unknowns_record_missing_inputs_not_guesses,
    test_same_content_under_different_kinds_does_not_overwrite,
    test_negative_control_items_as_dict_collapses,
    test_manual_item_kind_is_validated,
    test_fact_without_source_is_refused,
    test_negative_control_source_rule_has_teeth,
    test_save_appends_and_leaves_first_row_untouched,
    test_negative_control_overwrite_style_is_caught,
    test_missing_tables_yield_unknowns_not_exceptions,
    test_empty_test_runs_and_decisions_yield_unknowns,
    test_goal_id_three_shapes,
    test_test_run_latest_row_and_the_full_or_subset_gap,
    test_resource_log_has_no_success_column,
    test_pending_and_notes_and_tasks_shapes,
    test_note_outside_the_window_is_not_evidence,
    test_previous_decision_feeds_scope,
    test_debt_ledgers_read_from_files,
    test_capability_map_carriers_exist,
    test_previous_decision_is_activity_but_test_run_is_not,
    test_evidence_and_code_round_trip_through_json,
    test_illegal_evidence_marker_is_refused,
    test_collectors_never_write_existing_tables,
    test_negative_control_readonly_check_has_teeth,
    test_saved_state_is_readable_and_json_typed,
    test_isolation_never_points_at_production_db,
]


def main():
    setup()
    for fn in TESTS:
        fn()
    cleanup_test_db()


if __name__ == "__main__":
    main()
