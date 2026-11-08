#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pre-commit 钩子主体（scripts/pre_commit_hook.py）

被 .git/hooks/pre-commit（由 scripts/install_pre_commit.ps1 生成的 python
启动器）通过 runpy 调用，按固定顺序执行七项机械检查：

    1. checks/check_duplicate_names.py      —— 跨模块重复公共名
    2. checks/check_no_dynamic_namespace.py —— 动态命名空间操作
    3. checks/check_comment_freshness.py    —— 无日期标记的腐烂注释
    4. checks/check_test_db_isolation.py    —— 测试数据库隔离
    5. checks/check_import_direction.py     —— 跨层依赖方向（P4-1e 新增）
    6. checks/check_doc_integrity.py        —— 文档拼接事故（P6-6① 新增）
    7. checks/check_debt_ratchet.py         —— 台账条数棘轮（P4-1f 新增，末位执行）

新增检查器时须同时改三处：本文件的 CHECKS 列表与这段说明、项目根 AGENTS.md
的检查器表、PROJECT_STATUS.md §八 的检查器一览（见 AGENTS.md
「新检查器落地惯例」）。

实现说明：
    - 采用“进程内导入”方式逐个执行检查器（调用各自 main()），不在钩子内
      派生子进程——避免 Windows 受限环境（沙箱/杀软）对子进程管道的限制；
    - 任一检查器返回非零即视为失败，汇总后阻止提交并输出处置指引：
      禁止 git commit --no-verify 绕过（见项目根 AGENTS.md）；
      确认为误报时，将命中项加入对应 checks/*_exemptions.json 并在
      commit message 中说明原因。
"""

import importlib.util
import sys
from pathlib import Path

# 项目根目录（本文件位于 scripts/ 下）
ROOT = Path(__file__).resolve().parent.parent

# 按顺序执行的机械检查（模块名 -> 文件名 -> 一句说明）
CHECKS = [
    ("check_duplicate_names", "check_duplicate_names.py", "跨模块重复公共名"),
    ("check_no_dynamic_namespace", "check_no_dynamic_namespace.py", "动态命名空间操作"),
    ("check_comment_freshness", "check_comment_freshness.py", "无日期标记的腐烂注释"),
    ("check_test_db_isolation", "check_test_db_isolation.py", "测试数据库隔离"),
    ("check_import_direction", "check_import_direction.py", "跨层依赖方向"),
    ("check_doc_integrity", "check_doc_integrity.py", "文档拼接事故"),
    ("check_debt_ratchet", "check_debt_ratchet.py", "台账条数棘轮（只校验不写回）"),
]


def _run_checker(module_name: str, file_name: str, desc: str) -> int:
    """进程内加载并运行单个检查器，返回其 main() 的退出码。"""
    path = ROOT / "checks" / file_name
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            print(f"❌ [{desc}] 无法加载 {file_name}（spec 为空）", file=sys.stderr)
            return 1
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return int(module.main())
    except SystemExit as exc:  # 兼容检查器内部可能存在的 sys.exit 用法
        return int(exc.code or 0)
    except Exception as exc:  # 任何异常都视为该检查失败，不让钩子静默放行
        print(f"❌ [{desc}] {file_name} 执行异常：{exc!r}", file=sys.stderr)
        return 1


def main() -> int:
    # Windows 控制台/重定向编码兜底：避免输出 ❌/中文时因编码崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    failed = []
    for module_name, file_name, desc in CHECKS:
        code = _run_checker(module_name, file_name, desc)
        if code != 0:
            failed.append((file_name, desc))
            print(
                f"❌ [{desc}] {file_name} 检查未通过（退出码 {code}）",
                file=sys.stderr,
            )

    if failed:
        print("", file=sys.stderr)
        print("❌ pre-commit 机械检查失败，本次提交已被阻止。", file=sys.stderr)
        print("   处置要求（详见项目根 AGENTS.md）：", file=sys.stderr)
        print("   1. 禁止使用 git commit --no-verify 绕过本钩子；", file=sys.stderr)
        print("   2. 请修复代码；若确认为误报，将命中项加入对应", file=sys.stderr)
        print("      checks/*_exemptions.json 豁免台账，并在 commit message", file=sys.stderr)
        print("      中说明原因。", file=sys.stderr)
        return 1

    print(f"✅ pre-commit {len(CHECKS)} 项机械检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
