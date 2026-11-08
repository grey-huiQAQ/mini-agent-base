#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""债务棘轮（checks/check_debt_ratchet.py）

目的：
    台账过期检测（P4-1a）只告诉你"豁免死了"，不告诉你"台账长了"。本检查器读
    checks/debt_baseline.json（每本台账的条数基线），任一条数**与基线不符**即失败：
      - 条数 > 基线 → 债务增长（[DEBT_GROWTH]）；
      - 条数 < 基线 → 基线未收紧（[DEBT_BASELINE_DRIFT]）。
    两者都失败，是为了让"台账只减不增"从自然段变成被机械守卫的事实——本项目的
    教训是"自然段是最弱的载体"。收紧基线必须是一次显式、可见、可审计的改动
    （diff 里出现 -N 才是烧减的证据）。

只校验、不写回（重要）：
    本检查器在 pre-commit 钩子内运行，而钩子在 `git add` **之后**执行。若在此写回
    基线，改的是工作区而非索引——提交里仍是旧值，工作区却永远脏，且下次再被挡。
    因此棘轮只校验并打印新基线；写回由人显式执行：
        .\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py --update-baseline
    再自行 git add（见 AGENTS.md「新检查器落地惯例」第 2 条：台账先入基线再接 CHECKS）。

台账发现方式：
    按 checks/*_exemptions.json **自动发现**（不依赖手工清单），因此新增一本台账文件
    后若未登记基线，本检查器会立即失败——这正是"先入基线"要求的机械保障。

退出码：
    0 —— 各台账条数与基线完全一致；
    1 —— 存在债务增长或基线未收紧；
    2 —— 基线文件缺失或格式非法等内部错误（fail-closed，不静默放行）。
"""

import json
import sys
import time
from pathlib import Path

# 本文件所在目录（checks/）与项目根目录
CHECKS_DIR = Path(__file__).resolve().parent
ROOT = CHECKS_DIR.parent

# 基线文件路径
BASELINE_FILE = CHECKS_DIR / "debt_baseline.json"

# 台账文件命名约定：<名称>_exemptions.json
SUFFIX = "_exemptions.json"


def discover_ledgers() -> dict:
    """按命名约定发现全部台账文件，返回 名称 -> 路径。"""
    found = {}
    for path in sorted(CHECKS_DIR.glob(f"*{SUFFIX}")):
        found[path.name[: -len(SUFFIX)]] = path
    return found


def load_baseline() -> dict:
    """读取基线文件；缺失或格式非法以退出码 2 终止（fail-closed）。"""
    if not BASELINE_FILE.exists():
        print(
            f"[ERROR] 基线文件缺失：{BASELINE_FILE}（请先生成，见 AGENTS.md 落地惯例）",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] 基线文件无法解析：{BASELINE_FILE}（{exc}）", file=sys.stderr)
        sys.exit(2)
    ledgers = data.get("ledgers") if isinstance(data, dict) else None
    if not isinstance(ledgers, dict) or not all(
        isinstance(k, str) and isinstance(v, int) for k, v in ledgers.items()
    ):
        print(
            f"[ERROR] 基线文件格式非法（应为 {{\"ledgers\": {{名称: 条数}}}}）：{BASELINE_FILE}",
            file=sys.stderr,
        )
        sys.exit(2)
    return ledgers


def count_entries(path: Path):
    """统计台账条目数；不可解析返回 None（由调用方报错，不静默按 0 处理）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return len(data) if isinstance(data, list) else None


def main() -> int:
    started = time.perf_counter()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    baseline = load_baseline()
    ledgers = discover_ledgers()

    failures = []
    actual = {}

    # 1) 磁盘上有、基线里没有 → 新台账未登记（违反"先入基线"）
    for name in sorted(ledgers):
        if name not in baseline:
            print(
                f"[DEBT_GROWTH] {name}：新台账未登记基线"
                f"（当前 {BASELINE_FILE.name} 里没有该条目）"
            )
            failures.append(name)

    # 2) 基线里有、磁盘上没有 → 台账被删/改名，基线须同步
    for name in sorted(baseline):
        if name not in ledgers:
            print(
                f"[DEBT_BASELINE_DRIFT] {name}：基线有条目但台账文件不存在"
                f"（已删除或改名？请同步基线）"
            )
            failures.append(name)

    # 3) 双方都有 → 逐本比对条数
    for name in sorted(set(ledgers) & set(baseline)):
        count = count_entries(ledgers[name])
        if count is None:
            print(f"[ERROR] 台账无法解析为 JSON 数组：{ledgers[name]}", file=sys.stderr)
            return 2
        actual[name] = count
        base = baseline[name]
        if count > base:
            print(f"[DEBT_GROWTH] {name}：基线 {base} → 实际 {count}（+{count - base}）")
            failures.append(name)
        elif count < base:
            print(
                f"[DEBT_BASELINE_DRIFT] {name}：基线 {base} → 实际 {count}"
                f"（-{base - count}，债务已减少，请在同一次提交内收紧基线）"
            )
            failures.append(name)

    if failures:
        print("")
        print("收紧基线并写回（然后 git add 本文件）：")
        print("    .\\.venv\\Scripts\\python.exe scripts\\snapshot_state.py --update-baseline")
        print("")

    print(
        f"台账棘轮：比对 {len(set(ledgers) & set(baseline))} 本，"
        f"不符 {len(failures)} 本"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
