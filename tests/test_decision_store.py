# -*- coding: utf-8 -*-
"""
决策账本（core/decision_store.py，P7 第一闭环 Phase 1b，2026-10-06）模块测试
全程离线：只碰临时库，不调模型、不联网、不碰生产库。

覆盖（逐条对着拆分文档 1b 的三条判据）：
- 判据 ①「状态机非法转换必须被拒」：proposed→executed 直跳、evaluated 之后再改、
  rejected 之后再改、同态重复设置，四例都要响；
  并含一条**负控用例**（test_negative_control_guards_have_teeth）：把守卫摘掉之后
  这些断言必须变红——恒真的断言比没有断言更坏，所以这里不是"写一句大概会红"，
  是在同一个测试进程里真摘一次、真看到它放行。
- 判据 ②「mark_executed 是纯 DB 写、不依赖任何内存对象」：用**新进程**直接调用一次
  （子进程只拿到一个 id），并把调用后库里的行读回来。这是缺口 1 的正面证据。
- 判据 ③「五类 kind 互不混淆」：fact / user_report / inference / assumption / unknown
  各写各读，kind='revision' 追加且**原条目不删**＋计数，非法 kind 被拒。
另外钉住：create_decision 的默认值与 UTC 轴、decision_scope 非法值被拒（含**改前的
三个频率词** daily／weekly／long_term 现在必须被拒——Fix A 的证据就在这一条上）、
executed/evaluated 只能由专用函数走（两道守卫互相独立）、evaluate 的五值全可写且
evaluation 与 actual_result 必须同时非空、不存在的决策读 None 写 False、
list_decisions 的三个过滤参数（**没有 min_scope**：四值之间没有全序，「哪几档我要」
由调用方拿 `EXPERIENCE_ELIGIBLE_SCOPES` 这个集合表达）。
还钉一条跨层一致性：**建表语句里的 decision_scope 默认值必须是四值之一**——
写路径永远显式传值，所以这一列的 DEFAULT 平时没人走；它一旦漂出枚举就是
「绕过 create_decision 的 INSERT 会静默写进一个非法档」。

**跑不过去时的第一诊断**：本文件的表结构断言读的是临时库的 pragma，
若报 no such table，是 memory/database.py 的建表语句没进 CREATE_TABLES_SQL，
不是测试的问题。
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

from memory.database import init_db, get_connection
from core import decision_store as ds

# 本文件的用例前缀（便于排查；临时库随 cleanup 整体删除）
_PREFIX = "dec1b_" + uuid.uuid4().hex[:6]

# 两张新表的列清单（与 memory/database.py 的建表语句逐列对齐，改动 1/2 之后的形态）
_DECISION_COLUMNS = [
    "id", "goal_id", "title", "summary", "status", "decision_scope",
    "evaluation", "actual_result", "execution_reference",
    "created_at", "updated_at",
]
_ITEM_COLUMNS = [
    "id", "decision_id", "kind", "content", "source", "created_at",
]

# 子进程里跑的代码：只带一个 id 进来，验证 mark_executed 不依赖任何内存对象。
# 输出用 ensure_ascii 的 JSON，避开 Windows 控制台编码对断言的干扰。
_CHILD_CODE = """
import json
import os
import sys

sys.path.insert(0, os.environ["DECISION_TEST_ROOT"])
import memory.database as _db
_db.DB_PATH = os.environ["DECISION_TEST_DB"]

from core import decision_store as ds

decision_id = int(os.environ["DECISION_TEST_ID"])
report = {"pid": os.getpid()}

# 新进程里没有任何父进程的对象，只有一行库记录可读
row = ds.get_decision(decision_id)
report["seen_status"] = row["status"] if row else None

# 纯 DB 写：把这一步补上，返回值不依赖进程内的任何状态
report["mark_ok"] = ds.mark_executed(decision_id, "child-process-reference")
report["after_status"] = ds.get_decision(decision_id)["status"]
report["after_reference"] = ds.get_decision(decision_id)["execution_reference"]

# 守卫在新进程里同样生效：executed 之后不许回退到 discussed
try:
    ds.set_status(decision_id, "discussed")
    report["late_write"] = "accepted"
except ValueError as exc:
    report["late_write"] = "refused"
    report["late_write_message"] = str(exc)

print(json.dumps(report))
"""


def setup():
    """初始化独立测试数据库（两张新表建在临时库中）"""
    init_db()


def _cleanup():
    """删除临时库目录（cleanup_test_db 亦有 atexit 兜底）"""
    if os.path.isdir(_TMP_DIR):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup)


# ---------- 辅助 ----------

def _new_decision(title=None, scope="operational", goal_id=None):
    """建一条 proposed 决策并返回它的 id（每条用例各建各的，互不共用）"""
    return ds.create_decision(
        title or f"{_PREFIX}{uuid.uuid4().hex[:6]}",
        goal_id=goal_id,
        decision_scope=scope,
    )


def _push_to(decision_id, status):
    """沿合法边把一条决策推到目标状态（不改状态机、不绕守卫，只按表走）"""
    path = {
        "proposed": [],
        "discussed": ["discussed"],
        "approved": ["discussed", "approved"],
        "rejected": ["rejected"],
        "insufficient_information": ["insufficient_information"],
        "executed": ["discussed", "approved"],   # 之后走 mark_executed
        "evaluated": ["discussed", "approved"],  # 之后走 mark_executed + evaluate
    }[status]
    for step in path:
        ds.set_status(decision_id, step)
    if status == "executed":
        ds.mark_executed(decision_id, "helper")
    if status == "evaluated":
        ds.mark_executed(decision_id, "helper")
        ds.evaluate(decision_id, "neutral", "helper 写入的结果")
    return decision_id


def _table_columns(table):
    conn = get_connection()
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def _raw_row(decision_id):
    """绕过存储层直接读一行（断言"库里真的写着"，而不是"函数返回了好看的东西"）"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------- 表与隔离 ----------

def test_independent_database_and_new_tables():
    """两张新表建在临时库里，列集合与拆分文档改动 1/2 之后的形态逐列对齐"""
    assert os.path.abspath(_database_module.DB_PATH) == _TMP_DB, \
        "DB 隔离未生效：断言会打到生产库上，这条必须先成立"
    assert _table_columns("decisions") == _DECISION_COLUMNS, \
        "decisions 列集与建表语句不一致（改动 1 的 decision_scope 在第 6 列）"
    assert _table_columns("decision_items") == _ITEM_COLUMNS, \
        "decision_items 列集与建表语句不一致"
    print("[OK] new tables live in the isolated test database")


def test_create_and_read_back():
    """create_decision 立一行 proposed，默认值与 UTC 轴都在库里有形状"""
    decision_id = _new_decision(scope="tactical", goal_id=f"{_PREFIX}goal")
    row = ds.get_decision(decision_id)
    assert row is not None and row["id"] == decision_id
    assert row["status"] == "proposed", "新建决策必须是 proposed"
    assert row["decision_scope"] == "tactical", "decision_scope 要按传入值写"
    assert row["goal_id"] == f"{_PREFIX}goal"
    for column in ("evaluation", "actual_result", "execution_reference"):
        assert row[column] is None, f"{column} 在刚建出来时必须是 NULL，不能是空串"
    for column in ("created_at", "updated_at"):
        assert str(row[column]).endswith("+00:00"), \
            f"{column} 必须是带 +00:00 的 UTC（C9 口径），实际 {row[column]!r}"
    # 缺省作用域要分两条路验，它们读的是两个不同的默认值：
    # ① 走 `create_decision()` 而不传 scope ⇒ 函数签名的默认值；
    # ② 绕过函数、用裸 INSERT 省略这一列 ⇒ 建表语句的 DEFAULT（见
    #    test_ddl_scope_default_is_in_enum，那条才管 DEFAULT 漂没漂）。
    # 助手 `_new_decision()` 永远显式传值，所以这里必须直接调 create_decision。
    plain_id = ds.create_decision(f"{_PREFIX}bare_scope")
    assert ds.get_decision(plain_id)["decision_scope"] == "operational", \
        "decision_scope 的函数默认值必须是 operational（改动 1 ＋ Fix A：跨度词）"
    print("[OK] create_decision writes defaults and UTC stamps")


def test_illegal_scope_rejected():
    """decision_scope 非法取值被拒（改动 1 的"照既有写法拒"）

    名单里刻意把 **Fix A 改前的三个频率词** daily／weekly／long_term 也列成非法值：
    它们今天仍然是"看起来像枚举成员"的串，只有把这一条钉住，才能保证改完之后
    旧词不会作为别名偷偷活着（`critical` 改前改后都在枚举里，所以它不在名单内）。
    """
    for bad in ("Weekly", "longterm", "", None, "monthly",
                "daily", "weekly", "long_term"):
        try:
            _new_decision(scope=bad)
        except ValueError:
            continue
        raise AssertionError(f"decision_scope={bad!r} 竟然被放行了")
    # 空标题同样拒：一条没有名字的决策无法被复盘
    try:
        ds.create_decision("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("空标题被放行")
    print("[OK] illegal decision_scope and empty title rejected")


# ---------- 条目五类 ----------

def test_five_item_kinds_do_not_mix():
    """五类 kind 各写各读、互不串门；inference 与 assumption 是两行不同的记录"""
    decision_id = _new_decision()
    written = {}
    for kind, content, source in (
        ("fact", "回归 run #104 全绿", "tests/TEST_RESULTS.md"),
        ("user_report", "他说周末才开机", "会话原话"),
        ("inference", "系统不稳定", "fact: 回归里 15 轮含超时"),
        ("assumption", "每周一次足够", None),
        ("unknown", "云端那边有没有跑，不知道", None),
    ):
        written[kind] = ds.add_item(decision_id, kind, content, source=source)
    assert len(set(written.values())) == 5, "五类条目应是五行不同的记录"

    all_items = ds.list_items(decision_id)
    assert [item["kind"] for item in all_items] == [
        "fact", "user_report", "inference", "assumption", "unknown"
    ], "list_items 要按登记顺序（id 正序）返回，五类一个都不合并"
    for kind in written:
        only = ds.list_items(decision_id, kind=kind)
        assert len(only) == 1 and only[0]["id"] == written[kind], \
            f"按 kind={kind} 过滤时串进了别的类"
    assert all_items[3]["source"] is None, \
        "assumption 的 source 为空是合法的——为空正是「没有证据」的形状"

    for bad in ("opinion", "FACT", "", None, "revision_note"):
        try:
            ds.add_item(decision_id, bad, "内容")
        except ValueError:
            continue
        raise AssertionError(f"kind={bad!r} 竟然被放行了")
    print("[OK] five item kinds stay distinct")


def test_add_item_rejects_missing_decision():
    """条目不许挂到不存在的决策上（SQLite 默认不启用外键，这条边只能由写路径守）"""
    ghost = 900_001
    assert ds.get_decision(ghost) is None, "前置：这条决策确实不存在"
    try:
        ds.add_item(ghost, "fact", "会变成孤儿条目的一行")
    except ValueError as exc:
        assert "不存在" in str(exc), f"报错信息要指出是决策不存在，实际 {exc}"
    else:
        raise AssertionError("孤儿条目被写进了库")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM decision_items WHERE decision_id = ?", (ghost,)
        ).fetchone()
        assert int(row["c"]) == 0, "库里确实没留下那条孤儿行"
    finally:
        conn.close()
    print("[OK] add_item refuses orphan items")


# ---------- 状态机 ----------

def test_legal_transition_path():
    """proposed→discussed→approved→executed→evaluated 全程走得通，每步都从库里读"""
    decision_id = _new_decision()
    for step in ("discussed", "approved"):
        assert ds.set_status(decision_id, step) is True
        assert _raw_row(decision_id)["status"] == step, "状态要写进库，不只返回 True"
    assert ds.mark_executed(decision_id, "note://x") is True
    assert _raw_row(decision_id)["execution_reference"] == "note://x"
    assert ds.evaluate(decision_id, "helpful", "两周后确实少问了一次") is True
    row = _raw_row(decision_id)
    assert row["status"] == "evaluated"
    assert row["evaluation"] == "helpful" and row["actual_result"], \
        "evaluated 态必须 evaluation 与 actual_result 同时非空"
    print("[OK] the legal path walks end to end")


def test_illegal_transitions_rejected():
    """判据 ①：拆分文档点名的两类非法转换，加上终态与同态重复，四例都拒"""
    # (a) proposed → executed 直接跳
    fresh = _new_decision()
    try:
        ds.set_status(fresh, "executed")
    except ValueError as exc:
        assert "executed" in str(exc)
    else:
        raise AssertionError("proposed 直接跳到 executed 竟然成功了")
    assert _raw_row(fresh)["status"] == "proposed", "被拒的写不许留下半个"
    assert _raw_row(fresh)["execution_reference"] is None

    # (b) proposed → evaluated 直接跳
    try:
        ds.set_status(fresh, "evaluated")
    except ValueError:
        pass
    else:
        raise AssertionError("proposed 直接跳到 evaluated 竟然成功了")

    # (c) evaluated 之后再改（拆分文档点名）
    done = _push_to(_new_decision(), "evaluated")
    for target in ("discussed", "approved", "executed", "insufficient_information"):
        try:
            ds.set_status(done, target)
        except ValueError:
            continue
        raise AssertionError(f"evaluated 之后被改成了 {target}")
    assert _raw_row(done)["status"] == "evaluated", "终态必须留在终态"

    # (d) rejected 也是终态
    refused = _push_to(_new_decision(), "rejected")
    for target in ("proposed", "discussed", "approved", "executed", "evaluated"):
        try:
            ds.set_status(refused, target)
        except ValueError:
            continue
        raise AssertionError(f"rejected 之后被改成了 {target}")

    # (e) 同态重复设置也算非法：不改任何东西的写照样会推 updated_at，
    #     而"这条决策什么时候被推进过"是复盘的输入。
    #     这里把时钟钉到一个哨兵值再试——否则两次写落在同一秒里，
    #     "updated_at 没变"这句断言会因为精度而恒真（恒真的断言比没有断言更坏）。
    again = _new_decision()
    ds.set_status(again, "approved")
    before = _raw_row(again)["updated_at"]
    with patch.object(ds, "_utc_now_text", lambda: "2099-01-01T00:00:00+00:00"):
        try:
            ds.set_status(again, "approved")
        except ValueError as exc:
            assert "非法转换" in str(exc) or "approved" in str(exc)
        else:
            raise AssertionError("同态重复设置被放行")
    assert _raw_row(again)["updated_at"] == before, "被拒的写不许碰 updated_at"
    print("[OK] illegal transitions refused, no half-written rows left")


def test_executed_and_evaluated_only_via_helper():
    """set_status 不许直接置 executed / evaluated，且两道守卫各自独立成立

    先把两条守卫的分工说清（它们挡的是两件不同的事，所以必须分开验）：
    - 状态机表挡的是**跳级**（proposed 一步到 executed）；
    - helper 规则挡的是**状态与配套列矛盾**（approved→executed 本来就是合法边，
      但走 set_status 会写出 status=executed、execution_reference=NULL 的行）。
    所以"摘掉任意一条，判据 ① 那格就失守"这句话是错的——正确的形状是两条都在挡、
    各自挡的是不同的一类，下面把两种摘法都跑一遍。
    """
    ready = _push_to(_new_decision(), "approved")
    for target in ("executed", "evaluated"):
        try:
            ds.set_status(ready, target)
        except ValueError as exc:
            assert "mark_executed" in str(exc) or "evaluate" in str(exc), \
                f"报错要指路到专用函数，实际 {exc}"
        else:
            raise AssertionError(f"set_status 竟然能置 {target}")
    assert _raw_row(ready)["status"] == "approved", "被拒的写不许留下半个"

    # 摘掉 helper 规则、保留真实状态机表：proposed→executed 仍被表拒（判据 ① 不靠单点）
    fresh = _new_decision()
    with patch.object(ds, "STATUS_ONLY_VIA_HELPER", ()):
        try:
            ds.set_status(fresh, "executed")
        except ValueError as exc:
            assert "非法转换" in str(exc), \
                f"该由状态机表拒，实际报的是 {exc}"
        else:
            raise AssertionError("摘掉 helper 规则后状态机表竟然放行了跳级")
    assert _raw_row(fresh)["status"] == "proposed"

    # 摘掉状态机表、保留 helper 规则：矛盾账仍被拒（helper 规则也不靠单点）
    permissive = {s: tuple(ds.VALID_DECISION_STATUSES) for s in ds.VALID_DECISION_STATUSES}
    with patch.object(ds, "LEGAL_TRANSITIONS", permissive):
        try:
            ds.set_status(ready, "executed")
        except ValueError:
            pass
        else:
            raise AssertionError("摘掉状态机表之后 helper 规则也必须守住")
    assert _raw_row(ready)["status"] == "approved"
    print("[OK] two guards each block a different failure, neither is a single point")


def test_mark_executed_requires_approval():
    """没批准的决策不能记为已执行（辅助型自主：最终决策权在用户）"""
    for status in ("proposed", "discussed", "insufficient_information"):
        target = _push_to(_new_decision(), status)
        try:
            ds.mark_executed(target, "x")
        except ValueError as exc:
            assert "approved" in str(exc)
        else:
            raise AssertionError(f"{status} 竟然能直接记为已执行")
        assert _raw_row(target)["status"] == status
    print("[OK] mark_executed refuses unapproved decisions")


def test_evaluate_five_values_and_guards():
    """4a 的写路径判据提前立住：五值全可写、非法被拒、两列必须同时非空"""
    for value in ds.VALID_EVALUATIONS:
        target = _push_to(_new_decision(), "executed")
        assert ds.evaluate(target, value, f"评成 {value} 的实际结果") is True, \
            f"{value} 写不进去"
        row = _raw_row(target)
        assert row["evaluation"] == value and row["status"] == "evaluated"
    # wrong_assumption 不需要任何额外条件就能写——这一格是闭环的牙齿
    target = _push_to(_new_decision(), "executed")
    ds.add_item(target, "assumption", "周末才会开机", source=None)
    assert ds.evaluate(target, "wrong_assumption", "工作日其实也开了") is True
    assert ds.list_items(target, kind="assumption"), \
        "评错了不该顺手删掉当初那条前提"

    for bad in ("great", "", None, "Harmful", "help_full"):
        armed = _push_to(_new_decision(), "executed")
        try:
            ds.evaluate(armed, bad, "结果")
        except ValueError:
            continue
        raise AssertionError(f"evaluation={bad!r} 竟然被放行了")

    armed = _push_to(_new_decision(), "executed")
    for bad_result in ("", "   ", None):
        try:
            ds.evaluate(armed, "helpful", bad_result)
        except ValueError:
            continue
        raise AssertionError(f"actual_result={bad_result!r} 竟然被放行了")
    assert _raw_row(armed)["status"] == "executed", "被拒的 evaluate 不许推进状态"

    # 没执行过的决策没有"效果"可评
    not_yet = _push_to(_new_decision(), "approved")
    try:
        ds.evaluate(not_yet, "helpful", "结果")
    except ValueError as exc:
        assert "executed" in str(exc)
    else:
        raise AssertionError("approved 状态竟然能直接 evaluate")

    # 评完之后再评一次也要拒
    twice = _push_to(_new_decision(), "evaluated")
    try:
        ds.evaluate(twice, "harmful", "第二次改口")
    except ValueError:
        pass
    else:
        raise AssertionError("evaluated 竟然能被重新评")
    assert _raw_row(twice)["evaluation"] != "harmful"
    print("[OK] all five evaluations writable, guards hold")


def test_revision_keeps_original_items():
    """改动 3：修订不进状态机——追加 kind='revision' ＋计数，原条目与状态都不动"""
    decision_id = _push_to(_new_decision(), "discussed")
    original = ds.add_item(decision_id, "assumption", "每周一次足够")
    before = _raw_row(decision_id)

    ds.add_item(decision_id, "revision", "改成每两周一次，依据是两次周报都没内容",
                source=f"item#{original}")
    ds.add_item(decision_id, "revision", "再改：保留每周，但只报 critical")

    assert ds.count_revisions(decision_id) == 2, "计数要能读出修订过几次"
    assert ds.count_revisions(_new_decision()) == 0, "没修订过的决策计数必须是 0"
    after = _raw_row(decision_id)
    assert after["status"] == before["status"], "修订不是状态，不许动 status"
    assert after["updated_at"] == before["updated_at"], "加条目不许推决策自己的时间戳"

    kept = ds.list_items(decision_id, kind="assumption")
    assert len(kept) == 1 and kept[0]["id"] == original, \
        "原条目必须还在——第一闭环要评的就是「我当时以为是什么」"
    print("[OK] revisions append, originals survive")


def test_missing_decision_read_none_write_false():
    """不存在的决策：读 None，写 False（不是抛——"那一条不在库里"不是规则违规）"""
    ghost = 900_002
    assert ds.get_decision(ghost) is None
    assert ds.set_status(ghost, "approved") is False
    assert ds.mark_executed(ghost, "x") is False
    assert ds.evaluate(ghost, "helpful", "y") is False
    assert ds.list_items(ghost) == []
    assert ds.count_revisions(ghost) == 0
    print("[OK] missing decisions read None and write False")


def test_list_decisions_filters():
    """三个过滤参数都生效；**没有 min_scope**（Fix A：四值之间没有全序）

    这一条同时是"删干净了"的证据：`list_decisions` 一旦还留着 min_scope 或秩表，
    下面那句 `TypeError` 断言就会红。集合筛选（「哪几档我要」）不在库里做，
    由调用方在取回的行上判成员——所以这里验的是"集合口径能算出来"，不是"库里有下限"。
    """
    ids = {}
    for status in ("proposed", "discussed", "approved", "executed", "evaluated", "rejected"):
        ids[status] = _push_to(_new_decision(scope="critical"), status)
    tactical = _new_decision(scope="tactical")
    operational = _new_decision(scope="operational")
    strategic = _new_decision(scope="strategic")
    bound = _new_decision(goal_id=f"{_PREFIX}bound", scope="strategic")

    got = ds.list_decisions(status="proposed", limit=200)
    assert tactical in {row["id"] for row in got} and ids["approved"] not in {
        row["id"] for row in got
    }, "status 过滤没生效"

    got_bound = ds.list_decisions(goal_id=f"{_PREFIX}bound", limit=200)
    assert {row["id"] for row in got_bound} == {bound}, \
        "goal_id 过滤要把同前缀的别的目标排除干净"

    # 等值过滤用"取回来的每一行都满足条件"来断言，而不是"恰好等于我这一条"——
    # 前面还有别的用例建过 tactical 的决策，按 id 相等会假红（断言要随用例顺序稳健）
    got_tactical = ds.list_decisions(decision_scope="tactical", limit=200)
    assert tactical in {row["id"] for row in got_tactical}, "等值过滤该取回我建的那条"
    assert all(row["decision_scope"] == "tactical" for row in got_tactical), \
        "decision_scope 等值过滤串进了别的跨度"

    # 秩过滤没了：同一句调用必须报 TypeError，而不是"看起来仍然能返回一批行"
    try:
        ds.list_decisions(min_scope="operational")
    except TypeError:
        pass
    else:
        raise AssertionError("min_scope 还在：Fix A 的删秩没删干净")

    # 「哪几档我要」= 调用方拿集合筛。这里刻意用**取全部再筛**而不是让库做集合查询：
    # 4a／4b 要的正是这个写法，把它钉住，将来谁想加回 min_scope 会先撞这条。
    eligible = {
        row["id"] for row in ds.list_decisions(limit=500)
        if row["decision_scope"] in ds.EXPERIENCE_ELIGIBLE_SCOPES
    }
    assert {operational, strategic, bound} | set(ids.values()) <= eligible, \
        "operational／strategic／critical 都该进经验回流集合"
    assert tactical not in eligible, "tactical（只影响这几天的调整）不该回流成经验"

    for kwargs in ({"status": "Proposed"}, {"decision_scope": "monthly"},
                   {"decision_scope": "weekly"}):
        try:
            ds.list_decisions(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"非法过滤参数被放行：{kwargs}")
    print("[OK] list_decisions filters by equality only (no rank floor)")


def test_ddl_scope_default_is_in_enum():
    """建表语句里 decision_scope 的 DEFAULT 必须是四值之一（跨层一致性）

    为什么单独立一条：`create_decision()` 永远显式传值，所以这一列的 DEFAULT
    **平时没人走**——它一旦漂出枚举，只有绕过写路径的 INSERT 会踩到，而那种
    INSERT 今天一个都没有，于是漂移是静默的。

    做法：用裸 INSERT 省略这一列，把库真正填出来的默认值读回来判成员。
    这比去正则匹配 DDL 文本强——它量的是**建出来的表**，不是建表的那段字符串。
    刻意不用 `create_decision`：那条路经过函数默认值，证明不了 DDL 的 DEFAULT。

    ⚠️ 本条测的是**本临时库**（由当前 DDL 新建）。已经建好 decisions 的旧库
    （含生产库）里这一列的 DEFAULT 仍是改前的旧串，SQLite 改列默认值要重建表；
    本批刻意不重建（那张表 0 行，且两根写路径都不经过 DEFAULT）。
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO decisions (title) VALUES (?)",
            (f"{_PREFIX}ddl_default_probe",),
        )
        new_id = int(cursor.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT decision_scope FROM decisions WHERE id = ?", (new_id,)
        ).fetchone()
    finally:
        conn.close()
    written = str(row[0])
    assert written in ds.VALID_DECISION_SCOPES, (
        f"DDL 的 decision_scope 默认值 {written!r} 不在枚举 {ds.VALID_DECISION_SCOPES} 里"
        "——绕过写路径的 INSERT 会静默写进一个非法档")
    assert written == ds.SCOPE_OPERATIONAL, (
        f"DDL 默认值应与函数默认值同为 {ds.SCOPE_OPERATIONAL}，实际 {written!r}")
    print(f"[OK] DDL decision_scope default = {written}（枚举成员）")


# ---------- 负控 ----------

def test_negative_control_guards_have_teeth():
    """负控（拆分文档的纪律）：摘掉守卫之后这些断言必须变红，不是"大概会红"

    做法是就地摘、就地验：把守卫换成放行版本，同一句写必须真的能写进去——
    如果它照样被拒，说明那条断言是恒真的（恒真的断言比没有断言更坏），
    本用例就该红在这里。三件事分别验：
    1. evaluate 的枚举校验（摘掉常量表 ⇒ 非法值写得进去）；
    2. evaluate 的 actual_result 判空（**DDL 拒不掉它**，所以守卫只能活在写路径——
       用裸 SQL 写出那行矛盾账来证明这一点）；
    3. 两道状态机守卫一起摘 ⇒ proposed 一步到 executed，
       且 execution_reference 是 NULL（判据 ① 要防的那行假账的完整形状）。
    """
    # (1) 摘掉 evaluate 的枚举校验
    armed = _push_to(_new_decision(), "executed")
    with patch.object(ds, "VALID_EVALUATIONS", ds.VALID_EVALUATIONS + ("garbage",)):
        assert ds.evaluate(armed, "garbage", "摘了守卫就该写得进去") is True, \
            "这条断言在没有守卫时也不会红 ⇒ 它是恒真的，写错了"
    assert _raw_row(armed)["evaluation"] == "garbage", "非法值确实写进了库"

    # (2) 判空守卫活在写路径：DDL 里没有 CHECK，裸 SQL 就能写出矛盾账
    armed2 = _push_to(_new_decision(), "executed")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE decisions SET evaluation = 'helpful', actual_result = '' "
            "WHERE id = ?", (armed2,)
        )
        conn.commit()
        row = _raw_row(armed2)
        assert row["evaluation"] == "helpful" and row["actual_result"] == "", \
            "裸 SQL 竟然也写不出矛盾账 ⇒ 判据不该只写在写路径，本用例的前提要看清"
    finally:
        conn.close()
    # 上一句证的是"DDL 拦不住"，所以同一行用守卫再试一次必须拒
    armed3 = _push_to(_new_decision(), "executed")
    try:
        ds.evaluate(armed3, "helpful", "   ")
    except ValueError:
        pass
    else:
        raise AssertionError("判空守卫失效了")
    assert _raw_row(armed3)["status"] == "executed", "被拒的写不许推进状态"

    # (3) 两道守卫一起摘：假账的完整形状
    fresh = _new_decision()
    permissive = {s: tuple(ds.VALID_DECISION_STATUSES) for s in ds.VALID_DECISION_STATUSES}
    with patch.object(ds, "LEGAL_TRANSITIONS", permissive), \
            patch.object(ds, "STATUS_ONLY_VIA_HELPER", ()):
        assert ds.set_status(fresh, "executed") is True, \
            "两道守卫都摘掉后这句必须成功，否则说明上面某条用例是恒真的"
    row = _raw_row(fresh)
    assert row["status"] == "executed" and row["execution_reference"] is None, \
        "假账的形状应是「状态说做完了、痕迹列全空」"
    print("[OK] negative controls: the assertions do turn red without the guards")


# ---------- 判据 ②：新进程 ----------

def test_mark_executed_from_a_fresh_process():
    """mark_executed 是纯 DB 写：子进程只拿到一个 id，也能把这一步补上"""
    assert "_test_tmp_" in _TMP_DB, \
        f"前置护栏：子进程只能连临时库，实际要连 {_TMP_DB}"
    decision_id = _push_to(_new_decision(), "approved")

    env = dict(os.environ)
    env.update({
        "DECISION_TEST_ROOT": _PROJECT_ROOT,
        "DECISION_TEST_DB": _TMP_DB,
        "DECISION_TEST_ID": str(decision_id),
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

    assert report["seen_status"] == "approved", \
        f"新进程读到的状态不对：{report}"
    assert report["mark_ok"] is True, "新进程里 mark_executed 必须能纯靠 id 写成"
    assert report["after_status"] == "executed"
    assert report["after_reference"] == "child-process-reference", \
        "执行痕迹要真的落到库里那一列"
    assert report["late_write"] == "refused", \
        f"守卫在新进程里也得生效：{report.get('late_write_message')}"

    # 父进程这边从同一个库文件读回来（不是从子进程的返回值读）
    row = _raw_row(decision_id)
    assert row["status"] == "executed" and row["execution_reference"] == "child-process-reference"
    print("[OK] mark_executed works from a brand new process (gap 1 evidence)")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== decision store test ===\n")
    setup()
    test_independent_database_and_new_tables()
    test_create_and_read_back()
    test_illegal_scope_rejected()
    test_five_item_kinds_do_not_mix()
    test_add_item_rejects_missing_decision()
    test_legal_transition_path()
    test_illegal_transitions_rejected()
    test_executed_and_evaluated_only_via_helper()
    test_mark_executed_requires_approval()
    test_evaluate_five_values_and_guards()
    test_revision_keeps_original_items()
    test_missing_decision_read_none_write_false()
    test_list_decisions_filters()
    test_ddl_scope_default_is_in_enum()
    # 负控会真写出非法值（evaluation='garbage'），所以排在主断言之后
    test_negative_control_guards_have_teeth()
    test_mark_executed_from_a_fresh_process()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
