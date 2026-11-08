#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""禁止动态命名空间操作检查器（checks/check_no_dynamic_namespace.py）

目的：
    通过 AST 静态分析，拦截“新增”的动态命名空间操作，让模块间的依赖关系
    对静态分析与重构工具重新可见。本项目 ui/app_factory.py 曾用
    vars(app) + globals() 在运行时把 app 模块的公开符号注入自身全局命名
    空间（规避循环导入），导致 UI 构建块与回调之间的依赖无法被静态追踪——
    这类操作不允许继续扩散。

检测范围（BANNED_CALLS）：
    以裸名直接调用的 globals() / locals() / vars() / exec() / eval() /
    compile() / __import__()，以及 setattr(模块对象, ...) 对模块对象的
    动态属性设置（首个位置参数为裸名模块引用时才命中）。

豁免机制：
    存量已知违规按“文件相对路径:行号”写入
    checks/dynamic_namespace_exemptions.json。
    **台账格式（P4-5c）**：对象数组 `{"key": 文件:行号, "reason": 为何可放行,
    "since": 登记日期}`，解析统一走 checks/exemption_loader.py；裸字符串仍可读
    但会打印 [EXEMPTION_NO_REASON] 并以退出码 1 失败（P5-1 起生效）。
    豁免与行号绑定：命中行号一旦变动即失效，重新变为未豁免并导致退出码
    1 —— 强制任何触碰该处代码的重构都必须正面处理这笔债务，无法悄悄保留。
    过期检测（P4-1a 起）：台账里有、本次扫描未命中的条目打印
    [STALE_EXEMPTION] 并以退出码 1 失败。

输出词根约定（全仓库统一，四个检查器一致）：
    [DYNAMIC]          —— 新增的未豁免违规（“东西是新的”）
    [STALE_EXEMPTION]  —— 台账条目已不再命中（“豁免是旧的”）

已知盲区（刻意不处理，供后人参考）：
    - 不做作用域/遮蔽分析：局部函数或参数恰好命名为 globals/eval 等时
      可能误报；经别名（g = globals; g()）或显式 builtins.eval(...)
      调用的形式会漏报；
    - 仅识别直接调用，不追踪 getattr 等间接等价写法。
    误报宁可保留（人工豁免），漏报则需依赖人工评审。

退出码：
    0 —— 不存在未豁免的动态命名空间操作，且台账无过期条目；
    1 —— 存在未豁免的动态命名空间操作（即“新增违规”），或台账存在过期条目；
    2 —— 豁免台账缺失或格式非法等内部错误（fail-closed，不静默放行）。
"""

import ast
import json
import os
import sys
import time
from pathlib import Path

# 共享台账解析（checks/exemption_loader.py，P4-5c 统一格式）：
# 把本文件所在目录加入 sys.path，兼容"直跑脚本"与"pre-commit 钩子按文件路径加载"两种方式
_CHECKS_DIR = Path(__file__).resolve().parent
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))
from exemption_loader import collect_keys  # noqa: E402

# 需要跳过的目录（任意层级同名目录均被剪枝，如 data/、任意层级的 __pycache__）
SKIP_DIRS = {
    "data",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    "checks",
}

# 豁免台账路径：本文件所在目录（checks/）下的 dynamic_namespace_exemptions.json
EXEMPTIONS_FILE = (
    Path(__file__).resolve().parent / "dynamic_namespace_exemptions.json"
)

# 禁止以裸名直接调用的动态命名空间相关内建函数/特殊名
BANNED_CALLS = {
    "globals",
    "locals",
    "vars",
    "exec",
    "eval",
    "compile",
    "__import__",
}


def iter_py_files(root: Path):
    """按目录树遍历 root 下所有 .py 文件，就地剪枝 SKIP_DIRS 与隐藏目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地裁剪 dirnames：不深入 SKIP_DIRS 及任何 . 开头的隐藏目录，
        # 从而不会遍历 data/（约 7.6GB 模型）与 .venv/，保证秒级完成。
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def load_exemptions() -> set:
    """读取豁免台账；文件不存在返回空集，内容非法则报错并以退出码 2 终止。"""
    if not EXEMPTIONS_FILE.exists():
        return set()
    try:
        # utf-8-sig：容忍 Windows 编辑器/终端写入的 BOM（见 check_duplicate_names.py
        # 同处说明：合法台账不应因 BOM 被误判为"格式非法"）。
        data = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] 豁免台账无法解析：{EXEMPTIONS_FILE}（{exc}）", file=sys.stderr)
        sys.exit(2)
    return collect_keys(data, EXEMPTIONS_FILE)


def scan(root: Path):
    """扫描全部 .py 文件，返回 命中键(文件:行号) -> 描述 的映射。"""
    hits = {}
    for py_file in iter_py_files(root):
        try:
            # utf-8-sig 自动剥离文件头 BOM，兼容 Windows 编辑器保存的带 BOM 文件
            text = py_file.read_text(encoding="utf-8-sig", errors="replace")
            tree = ast.parse(text, filename=str(py_file))
        except SyntaxError as exc:
            # 语法错误 = 扫描覆盖率静默丢失，故 fail-closed（退出码 2）而非跳过。
            # 2026-09-12 实测教训：向 docstring 结束引号之后追加日期标记会生成
            # `(2026 - 09 - 12)` 表达式（09 是非法八进制字面量）→ SyntaxError，
            # 而旧行为"打 WARN 后跳过整个文件"让"命中归零"看起来像成功，
            # 实际是覆盖率丢失——只有当两个检查器的下游数字（重复名 15→14、
            # 使用 DB 文件 30→29）同时变化时才被发现。
            print(
                f"[ERROR] 语法错误，无法扫描 {py_file.relative_to(root)}："
                f"{exc.msg}（行 {exc.lineno}）",
                file=sys.stderr,
            )
            sys.exit(2)
        module = py_file.relative_to(root).as_posix()
        # ast.walk 遍历整棵树：函数体内、条件中、下标里的裸名调用都能命中
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            desc = None
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                # 裸名调用 banned 内建：globals() / vars() / __import__() 等
                desc = f"{func.id}()"
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "setattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                # setattr 的第一个位置参数是裸名（模块对象引用）时视为动态设置
                desc = f"setattr({node.args[0].id}, ...)"
            if desc is not None:
                hits[f"{module}:{node.lineno}"] = desc
    return hits


def main() -> int:
    started = time.perf_counter()
    root = Path(__file__).resolve().parent.parent  # 项目根目录（checks/ 的上一级）

    # Windows 控制台/重定向编码兜底：尽量以 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    exemptions = load_exemptions()
    hits = scan(root)

    # 按 (文件路径, 行号数值) 排序，保证同一文件内按行号升序输出
    ordered = sorted(
        ((key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1]), key, desc)
         for key, desc in hits.items()),
        key=lambda item: (item[0], item[1]),
    )

    # 未豁免 = 当前命中但不在台账中（即“新增违规”，需要拦截）
    unexempted = [(path, lineno, key, desc)
                  for path, lineno, key, desc in ordered
                  if key not in exemptions]

    # 打印未豁免的动态命名空间操作（[DYNAMIC] = 违规是新的）
    for path, lineno, key, desc in unexempted:
        print(f"[DYNAMIC] {key}: {desc}")

    # 台账过期条目 = 台账里有、本次扫描未命中（豁免是旧的）
    # 注意 hits 是 dict（键 -> 描述），求差集前必须 set(hits)
    stale = sorted(exemptions - set(hits))
    for key in stale:
        print(f"[STALE_EXEMPTION] {key}（已不再命中，建议从台账移除）")

    # 统计与耗时
    print(
        f"动态命名空间操作共 {len(hits)} 处，未豁免 {len(unexempted)} 处，"
        f"台账过期 {len(stale)} 处"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
