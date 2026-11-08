#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态快照（scripts/snapshot_state.py）

用途：
    在任何会改动仓库的动作前后各跑一次，输出一段**纯 ASCII 键名**的可 diff 文本，
    用于机器/人逐项比对「改动前后数据是否一致」，而不是只看"测试全绿"。

    设计动机：本仓库反复出现「修好一个 bug 会揭开另一个」的模式——删库 bug 修好
    后，它顺手充当的意外垃圾回收随之消失，残留才开始累积。因此"改动前后数据对比"
    是必需动作，测试通过从来不是充分证据。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py before
    .\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py after

    # 把当前台账条数写回 checks/debt_baseline.json（棘轮基线，只由人显式执行）
    .\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py --update-baseline

    写回后须 git add checks/debt_baseline.json。棘轮检查器（check_debt_ratchet.py）
    在 pre-commit 内**只校验不写回**：钩子运行于 git add 之后，写回只会改工作区、
    不进本次提交。此处分两步正是为了让"收紧基线"成为一次显式且可审计的改动。

输出各项含义：
    HEAD=            当前提交短哈希
    dirty=           工作区改动条数（git status --porcelain 行数，0 表示干净）
    goals= …         业务库五张点名表的行数
    data/goals_dirs= 目标产物目录数（文件系统泄漏哨兵：回归前后应完全不变）
    data/conversations_files= 会话文件数（同上）
    ledger: …        四本豁免台账的条目数
    checkers: …      checks/ 下每个检查器的名=exit:<退出码>,stale:<过期条数>

退出码：
    0 —— 快照成功输出；
    2 —— 快照自身失败（业务库不可读 / 台账不可解析），fail-closed 不静默放行。
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

# 项目根目录（本文件位于 scripts/ 下）
ROOT = Path(__file__).resolve().parent.parent

# 业务库路径（只读打开，避免与正在运行的界面抢写锁）
DB_PATH = ROOT / "data" / "memory.db"

# 快照点名的业务表（缺失时记 -1，不抛异常，便于前后对比看出差异）
DB_TABLES = ["goals", "goal_tasks", "milestones", "notes", "experiences"]

# 豁免台账（新增台账在此登记即可一并计数）
LEDGER_FILES = [
    ("duplicate_name", "checks/duplicate_name_exemptions.json"),
    ("comment_freshness", "checks/comment_freshness_exemptions.json"),
    ("dynamic_namespace", "checks/dynamic_namespace_exemptions.json"),
    ("test_db_isolation", "checks/test_db_isolation_exemptions.json"),
    ("import_direction", "checks/import_direction_exemptions.json"),
    ("doc_integrity", "checks/doc_integrity_exemptions.json"),
]

# 检查器脚本（按文件名字典序执行，保证输出顺序稳定）
CHECKER_DIR = ROOT / "checks"

# 棘轮基线文件与写入时的固定说明（--update-baseline 用）
BASELINE_FILE = ROOT / "checks" / "debt_baseline.json"
BASELINE_NOTE = (
    "台账条数基线（P4-1f 债务棘轮）。任一条数与基线不符都会使 "
    "checks/check_debt_ratchet.py 退出码 1：条数增加 = 债务增长；条数减少 = 基线未收紧。"
    "两种情形都必须在同一次提交内更新本文件，使「烧减」在 diff 里可见（-N 才是证据）。"
    "写回方式：.\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py --update-baseline，"
    "然后 git add 本文件。"
)


def _run_git(args):
    """执行 git 命令并返回 stdout（失败返回空串，不中断快照）"""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _count_table(conn, table):
    """统计单表行数；表不存在返回 -1（如实报告差异，不掩盖）"""
    try:
        return conn.execute(f"select count() from {table}").fetchone()[0]
    except sqlite3.Error:
        return -1


def _db_counts():
    """读取业务库五张表的行数；库不可读时全部记 -1"""
    if not DB_PATH.exists():
        return {t: -1 for t in DB_TABLES}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {t: -1 for t in DB_TABLES}
    try:
        return {t: _count_table(conn, t) for t in DB_TABLES}
    finally:
        conn.close()


def _count_dirs(path):
    """统计目录下的子目录数（不存在记 -1）"""
    try:
        return sum(1 for item in path.iterdir() if item.is_dir())
    except OSError:
        return -1


def _count_files(path):
    """统计目录下的文件数（不存在记 -1）"""
    try:
        return sum(1 for item in path.iterdir() if item.is_file())
    except OSError:
        return -1


def _ledger_counts():
    """读取各台账条目数；台账不可解析时记 -1 并在结果里可见"""
    counts = {}
    for name, rel in LEDGER_FILES:
        target = ROOT / rel
        if not target.exists():
            counts[name] = -1
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            counts[name] = len(data) if isinstance(data, list) else -1
        except (OSError, json.JSONDecodeError):
            counts[name] = -1
    return counts


def _checker_stats():
    """运行 checks/ 下每个检查器，返回 名 -> (退出码, 过期条数)

    过期条数按输出中带 [STALE 前缀的行计数（兼容 [STALE] 与 [STALE_EXEMPTION]
    两种写法，避免改名期间统计失真）。
    """
    stats = []
    for script in sorted(CHECKER_DIR.glob("check_*.py")):
        try:
            proc = subprocess.run(
                [sys.executable, str(script)], cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            stale = sum(1 for line in out.splitlines() if "[STALE" in line)
            stats.append((script.stem, proc.returncode, stale))
        except (OSError, subprocess.SubprocessError) as exc:
            stats.append((script.stem, -1, -1))
            print(f"[WARN] 检查器执行失败：{script.name}（{exc}）", file=sys.stderr)
    return stats


def update_baseline() -> int:
    """把当前台账条数写回 checks/debt_baseline.json（棘轮基线）

    只由人显式执行。pre-commit 内的棘轮检查器刻意不写回：钩子运行于 git add 之后，
    写回只会改工作区、不进本次提交（提交里仍是旧值，工作区却永远脏）。写回后须自行
    git add。台账缺失或不可解析时拒绝写回（fail-closed，不写一份残缺基线）。
    """
    counts = _ledger_counts()
    broken = [name for name, count in counts.items() if count < 0]
    if broken:
        print(f"[ERROR] 以下台账缺失或不可解析，拒绝写回基线：{broken}", file=sys.stderr)
        return 2

    since = date.today().isoformat()
    if BASELINE_FILE.exists():
        try:
            existing = json.loads(BASELINE_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict) and isinstance(existing.get("since"), str):
                since = existing["since"]  # 保留基线启用的日期，不随每次写回漂移
        except (OSError, json.JSONDecodeError):
            pass

    payload = {"since": since, "note": BASELINE_NOTE, "ledgers": counts}
    try:
        # newline="\n"：避免 Windows 上把 \n 翻成 \r\n（工作区会变脏而 git diff 看不见）
        BASELINE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    except OSError as exc:
        print(f"[ERROR] 基线写回失败：{exc}", file=sys.stderr)
        return 2

    print(f"已写回棘轮基线：{BASELINE_FILE}")
    for name, count in counts.items():
        print(f"  {name}={count}")
    print("请执行 git add checks/debt_baseline.json（须与台账改动在同一次提交内）。")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if "--update-baseline" in sys.argv[1:]:
        return update_baseline()

    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"

    head = _run_git(["rev-parse", "--short", "HEAD"]) or "unknown"
    dirty = _run_git(["status", "--porcelain"])
    dirty_count = len([line for line in dirty.splitlines() if line.strip()])

    db = _db_counts()
    ledgers = _ledger_counts()
    checkers = _checker_stats()

    print(f"=== snapshot:{label} ===")
    print(f"HEAD={head}  dirty={dirty_count}")
    print("  ".join(f"{t}={db[t]}" for t in DB_TABLES))
    print("data/goals_dirs={}  data/conversations_files={}".format(
        _count_dirs(ROOT / "data" / "goals"),
        _count_files(ROOT / "data" / "conversations"),
    ))
    print("ledger: " + " ".join(f"{name}={count}" for name, count in ledgers.items()))
    print("checkers: " + " ".join(
        f"{name}=exit:{code},stale:{stale}" for name, code, stale in checkers
    ))
    print("=== end ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
