#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨层依赖方向检查器（checks/check_import_direction.py）

目的：
    用显式层序表约束模块间的依赖方向，拦截两类结构倒置：
      ① 低层 import 高层（如 core/ → agents/，属"底层反向依赖上层"）；
      ② 生产代码 import 测试包（如 app.py → tests.*，会把"tests/ 排除出发行包"
         这条路堵死）。
    四个既有检查器查的都是局部性质（跨模块重名 / 动态命名空间 / 注释日期 /
    测试 DB 隔离），没有一个查跨层依赖——而 P4-2 修掉的 app.py:99 正是一次
    层序倒置。修实例不修规则，等于每轮人肉抓一次。

层序表（数字越大越上层；**禁止 import 层序高于自己的模块**）：
    0  core   memory  business  tools  rag
    1  plugins  prompts  skills
    2  agents   scheduler
    3  ui
    4  app            （app.py：薄壳，位于 ui 之上）
    99 tests          （测试包位于最顶层）

    为什么 tests 是 99 而不是 -1：规则是单调的"不得向上"。把 tests 放在最高层，
    同时得到两个想要的语义——测试可以 import 一切（0..4 都在它下面），而任何
    生产模块 import tests 都是"向上"从而违规。若把 tests 放在最低层，则"不得
    向上"会把每个测试文件的每条导入都判成违规（实测 214 条），表与规则自相矛盾。
    本项目的 tests/ 只允许被自己引用，故取最高层是唯一自洽的取值。

    为什么 app 比 ui 高一层：ui/app_factory.py:34 在函数内延迟 `import app`，
    属 Phase 3b 拆分骨架遗留的运行时反向引用。app 设为 4 使这条成为可见的存量，
    而不是藏在"同层允许"的缝里——那正是"UI 与业务层进一步解耦"要处理的地方。

检测口径：
    - 扫描项目根下全部 .py（跳过 SKIP_DIRS 与隐藏目录）；
    - 模块所属层：路径首段目录名（app.py / scheduler.py / 根目录 test_*.py 单独映射）；
    - 只统计 ast.Import / ast.ImportFrom(level=0) 的顶层模块名；
      相对导入（level>0）不出层，天然不可能违规，跳过；
    - 函数体内/条件内的延迟导入同样计入（它们也是真实的依赖边）。

豁免机制：
    首扫得到的全部命中写入 checks/import_direction_exemptions.json，条目为**对象
    格式**：{"key": "文件:行号", "reason": "为什么可以放行", "since": "YYYY-MM-DD"}。
    见 AGENTS.md「新检查器落地惯例」：写了理由的豁免将来能被质疑，裸字符串只能
    被整体删或整体留。读取时兼容 key / name 两种身份字段名。

输出词根（全仓库统一）：
    [LAYER_VIOLATION]  —— 新增的未豁免违规（"东西是新的"）
    [STALE_EXEMPTION]  —— 台账条目已不再命中（"豁免是旧的"）

退出码：
    0 —— 无未豁免的层序违规，且台账无过期条目；
    1 —— 存在未豁免的层序违规，或台账存在过期条目；
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

# 需要跳过的目录（任意层级同名目录均被剪枝）
SKIP_DIRS = {
    "data",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    "checks",
}

# 豁免台账路径：本文件所在目录（checks/）下的 import_direction_exemptions.json
EXEMPTIONS_FILE = (
    Path(__file__).resolve().parent / "import_direction_exemptions.json"
)

# 层序表：数字越大越上层，禁止 import 层序高于自己的模块
LAYER = {
    "core": 0,
    "memory": 0,
    "business": 0,
    "tools": 0,
    "rag": 0,
    "plugins": 1,
    "prompts": 1,
    "skills": 1,
    "agents": 2,
    "scheduler": 2,
    "ui": 3,
    "app": 4,
    "tests": 99,
}

# 根目录下的单文件模块映射（它们不属于任何包目录）
ROOT_FILES = {
    "app.py": "app",
    "scheduler.py": "scheduler",
}


def iter_py_files(root: Path):
    """按目录树遍历 root 下所有 .py 文件，就地剪枝 SKIP_DIRS 与隐藏目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def module_layer(rel_path: str):
    """返回 (模块名, 层序)；不属于层序表内的模块返回 (None, None)

    rel_path 为相对项目根的 POSIX 路径，如 "core/runtime.py"、"app.py"。
    """
    if "/" in rel_path:
        top = rel_path.split("/")[0]
        return (top, LAYER[top]) if top in LAYER else (None, None)
    name = ROOT_FILES.get(rel_path)
    if name is not None:
        return name, LAYER[name]
    # 根目录下的 test_*.py（如 test_runtime.py 冒烟脚本）按测试包处理
    if rel_path.startswith("test_") and rel_path.endswith(".py"):
        return "tests", LAYER["tests"]
    return None, None


def load_exemptions() -> set:
    """读取豁免台账（对象格式，兼容 key / name 字段）；非法内容以退出码 2 终止。"""
    if not EXEMPTIONS_FILE.exists():
        return set()
    try:
        # utf-8-sig：容忍 Windows 编辑器/终端写入的 BOM
        data = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] 豁免台账无法解析：{EXEMPTIONS_FILE}（{exc}）", file=sys.stderr)
        sys.exit(2)
    return collect_keys(data, EXEMPTIONS_FILE)


def scan(root: Path):
    """扫描全部 .py，返回 命中键(文件:行号) -> 描述 的映射。"""
    hits = {}
    for py_file in iter_py_files(root):
        rel = py_file.relative_to(root).as_posix()
        src_name, src_layer = module_layer(rel)
        if src_name is None:
            continue
        try:
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
                f"[ERROR] 语法错误，无法扫描 {rel}：{exc.msg}（行 {exc.lineno}）",
                file=sys.stderr,
            )
            sys.exit(2)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入不出层
                    continue
                if node.module:
                    targets = [node.module]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            for full in targets:
                top = full.split(".")[0]
                if top not in LAYER or top == src_name:
                    continue
                if LAYER[top] > src_layer:
                    hits[f"{rel}:{node.lineno}"] = (
                        f"{full}（本层 L{src_layer} → 上层 L{LAYER[top]}）"
                    )
    return hits


def main() -> int:
    started = time.perf_counter()
    root = Path(__file__).resolve().parent.parent  # 项目根目录（checks/ 的上一级）

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    exemptions = load_exemptions()
    hits = scan(root)

    ordered = sorted(
        ((key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1]), key, desc)
         for key, desc in hits.items()),
        key=lambda item: (item[0], item[1]),
    )

    unexempted = [(path, lineno, key, desc)
                  for path, lineno, key, desc in ordered
                  if key not in exemptions]

    for path, lineno, key, desc in unexempted:
        print(f"[LAYER_VIOLATION] {key}: {desc}")

    stale = sorted(exemptions - set(hits))
    for key in stale:
        print(f"[STALE_EXEMPTION] {key}（已不再命中，建议从台账移除）")

    print(
        f"层序违规共 {len(hits)} 处，未豁免 {len(unexempted)} 处，"
        f"台账过期 {len(stale)} 处"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
