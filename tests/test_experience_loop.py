# -*- coding: utf-8 -*-
"""
经验回流（P7 第一闭环 Phase 4a，2026-10-07）模块测试：`core.decision_store.link_experience`
＋ `experiences.decision_id` 那一列。全程离线：只碰临时库，不调模型、不联网、不碰生产库。

覆盖（逐条对着拆分文档 4a 的判据与锚点 §一.8／§一.9）：
- 判据 ①「`evaluation` 五值全可写」的 4a 版：五值逐个评完都能提炼成一条经验
  （回流不挑食，五格一视同仁）；
- 判据 ②「`evaluated` 必须有 `evaluation` 与 `actual_result`」的 4a 版：
  `effect` 那一格**从库里读** `actual_result`，不接参数；
- 判据 ③「跨重启」：父进程写经验行，**子进程只带一个 decision_id** 从同一个临时库读回；
- 判据 ④ 负控：`wrong_assumption` 那条决策**照样能提炼**——防止实现顺手做成只接受正面结论。
另钉四件定案形状：
- 单向链接（锚点 §一.8）：`experiences.decision_id` 存在且**可空**，而 `decisions` 那边
  **没有** `experience_id` 这一列；老办法 `execution_reference` 也没被拿去装教训；
- 落列对照表（锚点 §一.9）：`scenario`／`effect`／`future_effect`／`decision_id` 各归各，
  `strategy` 是**显式空串**（legacy 字段，不写它 ≠ 写 NULL）；
- **不回填存量**：加列之前已有的经验行，那一列必须还是 NULL；
- 四道守卫各配一条"被拒的写不许留下半行"。

负控另有一层（`test_negative_control_distill_guards`）：在同一个进程里真把
`EXPERIENCE_ELIGIBLE_SCOPES` 与"必须是 evaluated"那把尺摘掉一次，看到放行——
恒真的断言比没有断言更坏。

**跑不过去时的第一诊断**：`experiences` 的 `decision_id` 列由 `memory/database.py`
的迁移段加，报 no such column 就是那句 `_add_column_if_not_exists` 没跑进 `init_db()`。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import uuid
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.abspath(os.path.join(_TMP_DIR, "test_memory.db"))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_db
from memory.experience_store import ExperienceStore
from core import decision_store as ds

# 本文件的用例前缀（临时库随 cleanup 整体删除，前缀只便于排查）
_PREFIX = "exp4a_" + uuid.uuid4().hex[:6]

# 子进程里跑的代码：只带一个 decision_id 进来，从同一个库文件把经验行读回来。
# 输出用 ensure_ascii 的 JSON，避开 Windows 控制台编码对断言的干扰。
_CHILD_CODE = """
import json
import os
import sys

sys.path.insert(0, os.environ["EXPERIENCE_TEST_ROOT"])
import memory.database as _db
_db.DB_PATH = os.environ["EXPERIENCE_TEST_DB"]

from core import decision_store as ds

decision_id = int(os.environ["EXPERIENCE_TEST_ID"])
report = {"pid": os.getpid()}

conn = _db.get_db()
try:
    rows = conn.execute(
        "SELECT id, scenario, strategy, effect, future_effect, decision_id, "
        "source_session_id FROM experiences WHERE decision_id = ? ORDER BY id ASC",
        (decision_id,),
    ).fetchall()
    report["rows"] = [dict(r) for r in rows]
    report["decision_status"] = ds.get_decision(decision_id)["status"]
    # 新进程里凭 id 再提炼一条：证明写点不依赖父进程的任何内存对象
    report["second_id"] = ds.link_experience(
        decision_id, "新进程里也能凭 id 补上这一步")
    again = conn.execute(
        "SELECT COUNT(*) AS c FROM experiences WHERE decision_id = ?",
        (decision_id,)).fetchone()
    report["count_after"] = int(again["c"])
finally:
    conn.close()

print(json.dumps(report))
"""


def setup():
    """初始化独立测试数据库（`experiences` 的新列由 init_db 的迁移段加出来）"""
    init_db()


def _cleanup():
    """删除临时库目录（cleanup_test_db 亦有 atexit 兜底）"""
    if os.path.isdir(_TMP_DIR):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup)


# ---------- 辅助 ----------

def _new_decision(title=None, scope="operational", goal_id=None):
    """建一条 proposed 决策并返回 id（每条用例各建各的，互不共用）"""
    return ds.create_decision(
        title or f"{_PREFIX}{uuid.uuid4().hex[:6]}",
        goal_id=goal_id,
        decision_scope=scope,
    )


def _push_to(decision_id, status, evaluation=None, actual_result=None):
    """沿合法边走到目标态（不改状态机、不绕守卫）

    两条形状是这里刻意踩过的：`executed`／`evaluated` **只能走专用函数**
    （`mark_executed()`／`evaluate()`），用 `set_status` 去置它会被守卫拒——
    那不是测试写错了，那正是判据「状态与配套列不许矛盾」在响。
    """
    if status == "proposed":
        return decision_id
    if status in ("discussed", "approved", "rejected"):
        if status == "approved":
            ds.set_status(decision_id, "discussed")
        ds.set_status(decision_id, status)
        return decision_id
    if status == "executed":
        ds.set_status(decision_id, "discussed")
        ds.set_status(decision_id, "approved")
        ds.mark_executed(decision_id, "note://4a-test")
        return decision_id
    if status == "evaluated":
        assert evaluation and actual_result, "走到 evaluated 必须给 evaluate() 的两个必填值"
        _push_to(decision_id, "executed")
        ds.evaluate(decision_id, evaluation, actual_result)
        return decision_id
    raise AssertionError(f"_push_to 不认识目标态 {status!r}")


def _evaluated(evaluation="helpful", actual_result="两周后确实少问了一次",
               scope="operational", title=None):
    """造一条"已经评完"的决策（4a 的输入形状），返回 id"""
    decision_id = _new_decision(title=title, scope=scope)
    return _push_to(decision_id, "evaluated", evaluation=evaluation,
                    actual_result=actual_result)


def _exp_row(experience_id):
    """裸 SQL 读回一行 experiences（本模块没有按 id 读经验的公开入口）"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM experiences WHERE id = ?",
                           (experience_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _exp_count():
    conn = get_db()
    try:
        return int(conn.execute("SELECT COUNT(*) AS c FROM experiences").fetchone()["c"])
    finally:
        conn.close()


def _columns(table):
    """{列名: (类型, notnull)}——只读 pragma，用来钉"这列存在／不存在／可不可空" """
    conn = get_db()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]): (str(r["type"]), int(r["notnull"])) for r in rows}
    finally:
        conn.close()


# ---------- 那一列本身（锚点 §一.8） ----------

def test_decision_id_column_exists_and_is_nullable():
    """新列存在、可空，且**反向那一列没有**（链接是单向的）"""
    cols = _columns("experiences")
    assert "decision_id" in cols, f"experiences 没有 decision_id 列：{sorted(cols)}"
    assert cols["decision_id"] == ("INTEGER", 0), \
        f"那一列必须是可空的 INTEGER，实际 {cols['decision_id']}"
    # ⚠️ 别读错列：decision_items.decision_id 是既有的「条目 → 决策」NOT NULL 外键，
    # 与这一列同名不同物。这里把两边同时钉住，防下一个人把它们当同一件事。
    item_cols = _columns("decision_items")
    assert item_cols["decision_id"] == ("INTEGER", 1), \
        f"decision_items.decision_id 本该是 NOT NULL 外键，实际 {item_cols['decision_id']}"
    # 反向那一列不许存在（定案：不做 decisions.experience_id、不加中间表）
    assert "experience_id" not in _columns("decisions"), \
        "decisions 出现了 experience_id——单向链接的定案被写成了双向"
    print("[OK] experiences.decision_id exists, nullable, one-way")


def test_migration_does_not_backfill_existing_rows():
    """不回填存量：加列之前已有的经验行，那一列必须还是 NULL

    做法照 1a 给 `future_effect` 立的那条形判据——**用老签名写一行**（那时还没有
    `decision_id` 这个概念），再跑一次 `init_db()`（幂等），比对旧行的各列取值没变。
    """
    legacy_id = ExperienceStore.add_experience(
        scenario=f"{_PREFIX}legacy 场景", strategy="legacy 策略", effect="旧效果")
    before = _exp_row(legacy_id)
    assert before["decision_id"] is None, "新写的 legacy 行该列应是 NULL"

    init_db()  # 迁移必须幂等：跑第二次不许报错、也不许改数据

    after = _exp_row(legacy_id)
    for column in ("scenario", "strategy", "effect", "conditions", "id"):
        assert after[column] == before[column], f"迁移改动了既有列 {column} 的取值"
    assert after["decision_id"] is None, "迁移不许回填存量（旧数据没有的信息宁可空着）"
    assert after["future_effect"] is None
    print("[OK] column is added without touching existing rows")


# ---------- 落列对照表（锚点 §一.9） ----------

def test_link_experience_writes_the_mapped_columns():
    """主路径：评完之后提炼，四格各归各，strategy 是显式空串"""
    title = f"{_PREFIX}把复盘从周日挪到周六"
    lesson = "下次先看日志时间戳，别先看自己的感觉"
    decision_id = _evaluated(title=title, actual_result="挪到周六之后两次都没漏")

    experience_id = ds.link_experience(decision_id, lesson)
    row = _exp_row(experience_id)
    assert row is not None, "提炼出来的经验行读不回来"
    assert row["decision_id"] == decision_id, "链接要指回那条决策"
    assert row["future_effect"] == lesson
    assert row["effect"] == "挪到周六之后两次都没漏", \
        "effect 必须等于库里那条决策的 actual_result（不接参数，从库读）"
    assert row["scenario"] == title, "没给 scenario 时抄决策的 title，不新写句子"
    assert row["strategy"] == "", f"legacy 字段要落成空串，实际 {row['strategy']!r}"
    assert row["strategy"] is not None, \
        "空串与 NULL 是两件事：NULL 会让「这条没策略」和「这条有 legacy 策略」分不清"
    assert row["conditions"] is None, "conditions 是可选参数，没给就该是 NULL"
    assert row["source_session_id"] is None, \
        "来路记在 decision_id 那一列，不许占用会话 id 的语义"

    # 给了 scenario／conditions 就用给的，不覆盖成 title
    other = ds.link_experience(decision_id, "第二条教训",
                               scenario="每周整理本地机会库时", conditions="只在离线时适用")
    row2 = _exp_row(other)
    assert row2["scenario"] == "每周整理本地机会库时"
    assert row2["conditions"] == "只在离线时适用"
    print("[OK] columns land exactly as the anchor table says")


def test_one_decision_can_yield_several_experiences():
    """单向链接的另半句：一条经验只有一条来路，一个决策可以提炼出多条经验"""
    decision_id = _evaluated()
    first = ds.link_experience(decision_id, "教训一")
    second = ds.link_experience(decision_id, "教训二")
    assert first != second, "两次提炼应是两行不同的记录"
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT future_effect FROM experiences WHERE decision_id = ? ORDER BY id ASC",
            (decision_id,)).fetchall()
    finally:
        conn.close()
    assert [r["future_effect"] for r in rows] == ["教训一", "教训二"], \
        "按 decision_id 要能把这个决策的全部来路读回来"
    print("[OK] one decision fans out into several experiences")


def test_link_does_not_touch_the_decision_row():
    """提炼经验不许回写决策：status 与 updated_at 一个字都不动（终态不可回改）"""
    decision_id = _evaluated()
    before = ds.get_decision(decision_id)
    ds.link_experience(decision_id, "一句教训")
    after = ds.get_decision(decision_id)
    assert after["status"] == before["status"] == "evaluated"
    assert after["updated_at"] == before["updated_at"], "加一条经验不许推决策的时间戳"
    assert after["execution_reference"] == "note://4a-test", \
        "execution_reference 的语义是「执行了什么」，提炼经验不许改它"
    print("[OK] distilling an experience leaves the decision untouched")


# ---------- 判据 ①②④ ----------

def test_all_five_evaluations_are_distillable():
    """判据 ① 的 4a 版：五值逐个评完都能提炼，回流不挑食"""
    for value in ds.VALID_EVALUATIONS:
        decision_id = _evaluated(evaluation=value, actual_result=f"评成 {value} 的实际结果")
        experience_id = ds.link_experience(decision_id, f"{value} 那条的教训")
        row = _exp_row(experience_id)
        assert row["future_effect"] == f"{value} 那条的教训"
        assert row["effect"] == f"评成 {value} 的实际结果", \
            f"evaluation={value} 时 effect 串了别的内容"
        assert row["decision_id"] == decision_id
    print("[OK] all five evaluation values can flow back")


def test_wrong_assumption_flows_back():
    """判据 ④ 负控（点名那条）：「我判断错了」必须能提炼成经验

    做成"只接受正面结论"是这套东西最顺手的错法——`helpful` 能回流、
    `wrong_assumption` 不能回流，闭环就只会记住自己对的时刻。
    """
    decision_id = _evaluated(
        evaluation="wrong_assumption",
        actual_result="工作日其实也开了，当初那条前提被证伪")
    experience_id = ds.link_experience(
        decision_id, "先验证「只有周末开机」这个前提，再排复盘时间")
    row = _exp_row(experience_id)
    assert row is not None, "wrong_assumption 的决策竟然提炼不出经验"
    assert row["future_effect"] == "先验证「只有周末开机」这个前提，再排复盘时间"
    assert row["effect"] == "工作日其实也开了，当初那条前提被证伪"
    print("[OK] wrong_assumption flows back too (criterion 4)")


def test_only_evaluated_scopes_flow_back():
    """判据 ②／筛选口径：tactical 结构性地回不了流，其余三档能"""
    for scope in sorted(ds.EXPERIENCE_ELIGIBLE_SCOPES):
        decision_id = _evaluated(scope=scope)
        assert ds.link_experience(decision_id, f"{scope} 的教训") > 0
    blocked = _evaluated(scope="tactical")
    count_before = _exp_count()
    try:
        ds.link_experience(blocked, "tactical 不该进闭环")
    except ValueError as exc:
        assert "tactical" in str(exc), f"报错要指出被挡的是哪一档，实际 {exc}"
    else:
        raise AssertionError("tactical 决策竟然被提炼成了经验")
    assert _exp_count() == count_before, "被拒的写不许留下半行"
    print("[OK] the scope filter has teeth on both sides")


# ---------- 守卫 ----------

def test_guards_refuse_bad_inputs():
    """四道守卫逐个响，且被拒的写都留下一模一样的行数"""
    before = _exp_count()

    # (a) 幽灵决策：写一条指向不存在决策的经验，账本上永远读不回它是谁
    ghost = 910_001
    assert ds.get_decision(ghost) is None, "前置：这条决策确实不存在"
    try:
        ds.link_experience(ghost, "指向幽灵的教训")
    except ValueError as exc:
        assert "不存在" in str(exc), f"报错要说是决策不存在，实际 {exc}"
    else:
        raise AssertionError("link_experience 竟然写下了指向幽灵决策的经验")

    # (b) 还没评估：executed 态没有「效果」可提炼，硬写就是把猜测当教训
    not_yet = _push_to(_new_decision(), "executed")
    try:
        ds.link_experience(not_yet, "还没评就提炼")
    except ValueError as exc:
        assert "evaluated" in str(exc), f"报错要点名 evaluated，实际 {exc}"
    else:
        raise AssertionError("未评估的决策被提炼成了经验")

    # (c) 教训为空：4b 只读 future_effect 那一列，空串等于往 prompt 注空气
    done = _evaluated()
    for bad in ("", "   ", None, "\n\t"):
        try:
            ds.link_experience(done, bad)
        except ValueError:
            continue
        raise AssertionError(f"future_effect={bad!r} 竟然被放行")

    # (d) scenario 与 title 同时为空：这一格由**直接 SQL 造出来的行**触发
    #     （`create_decision` 永远拒空 title，所以这个形状走公开路径进不来——
    #      它防的是绕过写路径的 INSERT，属"被定案禁止的形状在测试里构造一次"）
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO decisions (goal_id, title, summary, status, decision_scope, "
            "evaluation, actual_result, created_at, updated_at) "
            "VALUES (?, ?, ?, 'evaluated', 'operational', 'neutral', '库里直接造的行', "
            "?, ?)",
            (None, "", "空标题的历史行", "2026-10-07T00:00:00+00:00",
             "2026-10-07T00:00:00+00:00"),
        )
        conn.commit()
        empty_title = int(cursor.lastrowid)
    finally:
        conn.close()
    try:
        ds.link_experience(empty_title, "标题为空时该问清楚再写")
    except ValueError as exc:
        assert "scenario" in str(exc), f"报错要点名 scenario，实际 {exc}"
    else:
        raise AssertionError("空标题＋空 scenario 竟然被放行")

    assert _exp_count() == before, "上面四组被拒的写一行都不该留下"
    print("[OK] all four guards refuse, no half-written rows")


# ---------- 判据 ③：新进程 ----------

def test_experience_readable_from_a_fresh_process():
    """判据 ③：写入 → 新进程读 → 结果在（缺口 1 的正面证据）"""
    assert "_test_tmp_" in _TMP_DB, \
        f"前置护栏：子进程只能连临时库，实际要连 {_TMP_DB}"
    decision_id = _evaluated(actual_result="隔一次重启也要读得回来的结果")
    written = ds.link_experience(decision_id, "跨进程那一条教训")

    env = dict(os.environ)
    env.update({
        "EXPERIENCE_TEST_ROOT": _PROJECT_ROOT,
        "EXPERIENCE_TEST_DB": _TMP_DB,
        "EXPERIENCE_TEST_ID": str(decision_id),
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_CODE], env=env, cwd=_PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程没跑成：{proc.stderr[-400:]}"
    line = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert line, f"子进程没吐出 JSON：{proc.stdout[-400:]}"
    report = json.loads(line[-1])

    rows = report["rows"]
    assert len(rows) == 1, f"新进程应读回那一条经验，实际 {len(rows)} 条：{rows}"
    assert rows[0]["id"] == written
    assert rows[0]["future_effect"] == "跨进程那一条教训"
    assert rows[0]["effect"] == "隔一次重启也要读得回来的结果"
    assert rows[0]["strategy"] == "" and rows[0]["decision_id"] == decision_id
    assert report["decision_status"] == "evaluated"
    # 新进程里只凭 id 也能再提炼一条：写点不依赖父进程持有的任何对象
    assert report["count_after"] == 2, \
        f"新进程里凭 id 补写这一步没成立：{report}"

    # 父进程这边从同一个库文件读回来（不是从子进程的返回值读）
    conn = get_db()
    try:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM experiences WHERE decision_id = ? ORDER BY id ASC",
            (decision_id,)).fetchall()]
    finally:
        conn.close()
    assert report["second_id"] in ids, "子进程写的那一行，父进程也要能从库里读到"
    print("[OK] experience survives a restart and the write point is process-free")


# ---------- 负控：断言到底有没有牙齿 ----------

def test_negative_control_distill_guards():
    """把守卫真摘一次，看到放行——否则上面的用例可能是恒真的"""
    tactical = _evaluated(scope="tactical")
    # ① 集合口径：把 tactical 放进集合，被挡那条就该写进去
    widened = ds.EXPERIENCE_ELIGIBLE_SCOPES | {"tactical"}
    with patch.object(ds, "EXPERIENCE_ELIGIBLE_SCOPES", widened):
        assert ds.link_experience(tactical, "放宽集合后应当放行") > 0, \
            "摘掉集合守卫还被拒——上面那条筛选用例是恒真的"

    # ② 状态口径：把"必须是 evaluated"那把尺换成 executed，未评估的行就该被放行
    pending = _push_to(_new_decision(), "executed")
    with patch.object(ds, "STATUS_EVALUATED", "executed"):
        assert ds.link_experience(pending, "摘掉状态守卫后应当放行") > 0, \
            "摘掉状态守卫还被拒——上面那条 (b) 用例是恒真的"

    # 恢复后（with 块结束）两边都回到原样：同一句话再写必须重新被拒
    refused = _evaluated(scope="tactical")
    try:
        ds.link_experience(refused, "恢复之后仍应被拒")
    except ValueError:
        pass
    else:
        raise AssertionError("patch 退出后守卫没有恢复")
    late = _push_to(_new_decision(), "executed")
    try:
        ds.link_experience(late, "恢复之后仍应被拒")
    except ValueError:
        pass
    else:
        raise AssertionError("patch 退出后状态守卫没有恢复")
    print("[OK] negative controls: both guards really do turn red")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== experience loop test ===\n")
    setup()
    test_decision_id_column_exists_and_is_nullable()
    test_migration_does_not_backfill_existing_rows()
    test_link_experience_writes_the_mapped_columns()
    test_one_decision_can_yield_several_experiences()
    test_link_does_not_touch_the_decision_row()
    test_all_five_evaluations_are_distillable()
    test_wrong_assumption_flows_back()
    test_only_evaluated_scopes_flow_back()
    test_guards_refuse_bad_inputs()
    # 负控会真放宽守卫写行，放在主断言之后、跨进程用例之前都行
    test_negative_control_distill_guards()
    test_experience_readable_from_a_fresh_process()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
