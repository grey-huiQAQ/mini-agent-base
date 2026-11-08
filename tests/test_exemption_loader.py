# -*- coding: utf-8 -*-
"""
豁免台账解析（checks/exemption_loader.py）测试 —— P5-1 的"无理由豁免转为失败"

背景：P4-5c 起台账支持对象格式 `{"name"/"key", "reason", "since"}`；裸字符串仍可读
但只打印 `[EXEMPTION_NO_REASON]` 警告。Phase 4 把"P5 起转为失败"记进了约定，
P5-1 兑现它：**退出码 1（违规）**，与既有的**退出码 2（格式非法 fail-closed）**分开。

覆盖：
- 裸字符串 → SystemExit.code == 1，stderr **恰好 1 行**且含 [EXEMPTION_NO_REASON]
  （"恰好 1 行"是 P4-5c 噪声教训的回归哨兵：一本 26 条的台账逐条打印会让每次提交
   刷 26 行噪声，吵闹的门禁等于没有门禁）
- 对象但缺 reason / 缺 since / 两者皆缺 → 同样 exit 1
- 汇总行列出键名（≤5 条全列；>5 条列前 5 + "…详见台账"）
- 干净台账 → 正常返回键集合，stderr 无输出
- 格式非法仍走 exit 2（不是 1）：台账非数组 / 条目类型不对 / 缺 name|key
- 退出码分工不混淆（1 与 2 各自只在自己的分支出现）

隔离：本文件不访问数据库（只喂内存数据给纯函数），但为满足
checks/check_test_db_isolation.py 对"测试文件"的统一约定，仍先调用
tests/_test_db_helpers.setup_test_db()。
"""
import contextlib
import io
import json
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

setup_test_db()

# checks/ 不是包，按检查器自身的做法把该目录加入 sys.path 后直接导入模块
_CHECKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "checks")
if _CHECKS_DIR not in sys.path:
    sys.path.insert(0, _CHECKS_DIR)
from exemption_loader import collect_keys  # noqa: E402


class _FakeSource:
    """模拟检查器传入的 Path 对象（collect_keys 会用 .name 取文件名）"""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"/fake/checks/{self.name}"


def _run(data, source_name="demo_exemptions.json"):
    """调 collect_keys，捕获 stdout/stderr 与 SystemExit

    Returns:
        (keys_or_None, stdout, stderr, exit_code) —— 未退出时 exit_code 为 None
    """
    out, err = io.StringIO(), io.StringIO()
    code = None
    keys = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            keys = collect_keys(data, _FakeSource(source_name))
    except SystemExit as e:
        code = e.code
    return keys, out.getvalue(), err.getvalue(), code


def _stderr_lines(text):
    """stderr 的非空行数"""
    return [ln for ln in text.splitlines() if ln.strip()]


# ---------- 1. 无理由豁免 → 退出码 1 ----------

def test_bare_string_exits_1():
    """裸字符串 → exit 1，且 stderr **恰好 1 行**含 [EXEMPTION_NO_REASON]"""
    keys, out, err, code = _run(["some_file.py"])
    assert code == 1, f"裸字符串应使退出码为 1（违规），实际 {code!r}"
    lines = _stderr_lines(err)
    assert len(lines) == 1, f"每本台账只允许打印一行汇总，实际 {len(lines)} 行：{lines}"
    assert "[EXEMPTION_NO_REASON]" in lines[0], lines[0]
    assert "some_file.py" in lines[0], "汇总行应列出待补条目名：" + lines[0]
    assert out == "", f"不该往 stdout 写东西：{out!r}"
    print("[OK] bare string exemption exits 1 with one stderr line")


def test_object_missing_reason_or_since_exits_1():
    """对象缺 reason / 缺 since / 两者皆缺 → 同样 exit 1（:62-63 两条分支都覆盖）"""
    cases = {
        "缺 reason": [{"name": "a.py", "since": "2026-09-13"}],
        "缺 since": [{"name": "a.py", "reason": "有理由"}],
        "两者皆缺": [{"name": "a.py"}],
        "reason 为空串": [{"name": "a.py", "reason": "", "since": "2026-09-13"}],
        "since 为空串": [{"name": "a.py", "reason": "有理由", "since": ""}],
    }
    for label, data in cases.items():
        keys, out, err, code = _run(data)
        assert code == 1, f"{label} 应 exit 1，实际 {code!r}"
        assert "[EXEMPTION_NO_REASON]" in err, f"{label}: {err!r}"
        assert len(_stderr_lines(err)) == 1, f"{label} 应只 1 行：{err!r}"
    # key 字段（另一种键名）同样适用
    _, _, err2, code2 = _run([{"key": "file.py:12"}])
    assert code2 == 1 and "[EXEMPTION_NO_REASON]" in err2, (code2, err2)
    print("[OK] object missing reason/since exits 1")


def test_summary_lists_keys_capped_at_five():
    """汇总行列出键名：≤5 条全列；>5 条列前 5 + '…详见台账'"""
    # 3 条：全列
    _, _, err3, code3 = _run(["a.py", "b.py", "c.py"])
    assert code3 == 1
    for n in ("a.py", "b.py", "c.py"):
        assert n in err3, f"≤5 条应全列，缺 {n}：{err3}"
    assert "…详见台账" not in err3, err3

    # 8 条：列前 5，其余省略
    names = [f"f{i}.py" for i in range(8)]
    _, _, err8, code8 = _run(names)
    assert code8 == 1
    for n in names[:5]:
        assert n in err8, f"应列出的前 5 条缺 {n}：{err8}"
    assert "…详见台账" in err8, err8
    assert names[7] not in err8, f"第 8 条应省略：{err8}"
    # 计数正确（8/8 条无理由）
    assert "8/8" in err8, err8
    print("[OK] summary lists keys capped at five")


def test_mixed_ledger_counts_and_still_one_line():
    """混合台账（2 条干净 + 3 条无理由）→ exit 1，计数 3/5，仍只 1 行"""
    data = [
        {"name": "ok1", "reason": "r", "since": "2026-09-13"},
        {"name": "ok2", "reason": "r", "since": "2026-09-13"},
        "bad1", {"name": "bad2"}, {"name": "bad3", "reason": "r"},
    ]
    _, _, err, code = _run(data)
    assert code == 1, code
    assert "3/5" in err, f"应报 3/5 条缺 reason/since：{err}"
    assert len(_stderr_lines(err)) == 1, err
    print("[OK] mixed ledger reports counts in one line")


# ---------- 2. 干净台账正常返回 ----------

def test_clean_ledger_returns_keys_silently():
    """干净台账 → 返回键集合，退出码 None，stderr 无输出"""
    data = [
        {"name": "dup_name", "reason": "接口契约要求同名", "since": "2026-09-06"},
        {"key": "file.py:12", "reason": "埋点防御性取值", "since": "2026-09-06"},
    ]
    keys, out, err, code = _run(data)
    assert code is None, f"干净台账不该退出：{code!r} / {err!r}"
    assert keys == {"dup_name", "file.py:12"}, keys
    assert err == "", f"干净台账不该有 stderr 输出：{err!r}"
    # 空台账也合法
    keys0, _, err0, code0 = _run([])
    assert code0 is None and keys0 == set() and err0 == ""
    print("[OK] clean ledger returns keys silently")


# ---------- 3. 格式非法仍走退出码 2（不与 1 混淆） ----------

def test_malformed_ledger_exits_2_not_1():
    """台账非数组 / 条目类型不对 / 缺 name|key → exit 2（fail-closed），不是 1"""
    bad_cases = {
        "非数组（dict）": {"name": "x"},
        "非数组（字符串）": "not-a-list",
        "条目是数字": [123],
        "条目是 None": [None],
        "条目是嵌套数组": [["a"]],
        "对象缺 name/key": [{"reason": "r", "since": "2026-09-13"}],
        "name 非字符串": [{"name": 123, "reason": "r", "since": "2026-09-13"}],
        "name 空串": [{"name": "", "reason": "r", "since": "2026-09-13"}],
    }
    for label, data in bad_cases.items():
        _, out, err, code = _run(data)
        assert code == 2, f"{label} 应走退出码 2（格式非法），实际 {code!r}"
        assert "[ERROR]" in err, f"{label}: {err!r}"
        assert "[EXEMPTION_NO_REASON]" not in err, \
            f"{label} 是格式问题，不该报无理由豁免：{err!r}"
    print("[OK] malformed ledger exits 2 not 1")


def test_exit_codes_are_distinct():
    """退出码分工哨兵：1 只用于"无理由"，2 只用于"格式非法"（不互相串台）"""
    _, _, err1, code1 = _run(["bare.py"])
    assert code1 == 1 and "格式非法" not in err1
    _, _, err2, code2 = _run("not-a-list")
    assert code2 == 2 and "[EXEMPTION_NO_REASON]" not in err2
    print("[OK] exit codes are distinct (1 vs 2)")


# ---------- 4. 真实台账受校验 ----------

def test_repo_ledgers_all_pass_strict_check():
    """全仓各本真实台账都过严格校验（无理由豁免=0）——P5-1 的核心验收

    直接读 checks/*_exemptions.json 喂 collect_keys：任一含裸字符串或
    缺 reason/since 的条目都会让本用例拿到 exit 1 而失败。

    台账本数与条数**一律不写死**（P6-6b，2026-09-13）：本用例原先断言"恰有 5 本 /
    合计 23 条"，而"条数只降不升"正是本项目的既定方向——每回收一条豁免就要改一次
    测试，等于给"还债"额外加一道手续。改为从 checks/debt_baseline.json（棘轮基线，
    台账条数的**唯一权威**）读取：
    - 台账文件集合必须与基线的键**逐一对应**（多一本=新台账未登记，少一本=基线未
      同步；这正是 check_debt_ratchet 的口径，这里只是让它也在严格校验这关被覆盖）；
    - 每本的实际条数必须等于基线值（棘轮在 pre-commit 里已拦，这里再拦一次是为了
      让"台账改动没同步基线"在**测试**里也立刻可见）。
    另外仍保留一条与数量无关的硬断言：comment_freshness 合法地可以是空台账
    （P4-5 已把无日期注释清零），故不能断言每本都非空。
    """
    ledgers = sorted(
        f for f in os.listdir(_CHECKS_DIR) if f.endswith("_exemptions.json"))
    with open(os.path.join(_CHECKS_DIR, "debt_baseline.json"),
              encoding="utf-8-sig") as fh:
        baseline = json.load(fh)["ledgers"]
    assert ledgers, "至少应有一本台账"
    assert ledgers == sorted(f"{name}_exemptions.json" for name in baseline), \
        f"台账文件集合与棘轮基线不一致：{ledgers} vs {sorted(baseline)}"
    for name in ledgers:
        path = os.path.join(_CHECKS_DIR, name)
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        keys, _, err, code = _run(data, source_name=name)
        assert code is None, \
            f"{name} 存在无理由豁免（退出码 {code}）：{err.strip()}"
        expect = baseline[name[: -len("_exemptions.json")]]
        assert len(keys) == expect, \
            f"{name} 实际 {len(keys)} 条，棘轮基线是 {expect} 条（须在同一次提交内对齐）"
    print(f"[OK] repo ledgers all pass strict check "
          f"({len(ledgers)} ledgers, {sum(baseline.values())} entries, 0 bare)")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== exemption loader test ===\n")
    test_bare_string_exits_1()
    test_object_missing_reason_or_since_exits_1()
    test_summary_lists_keys_capped_at_five()
    test_mixed_ledger_counts_and_still_one_line()
    test_clean_ledger_returns_keys_silently()
    test_malformed_ledger_exits_2_not_1()
    test_exit_codes_are_distinct()
    test_repo_ledgers_all_pass_strict_check()
    test_cleanup()
    print("\n=== ALL EXEMPTION LOADER TESTS PASSED ===")
