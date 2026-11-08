#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨模块重复公共名扫描器（checks/check_duplicate_names.py）

目的：
    通过 AST 静态分析，找出项目内被 2 个及以上模块在“模块顶层”重复定义的
    公共函数/类名，用于拦截“新增”的复制粘贴型重复，避免公共契约继续漂移。

设计要点：
    1. 扫描范围：项目根目录下所有 .py 文件，跳过 SKIP_DIRS 所列目录
       （data/.venv/venv/__pycache__/.git/node_modules/checks 等）及所有
       以 . 开头的隐藏目录，保证秒级完成（data/ 含约 7.6GB 模型，不进入遍历）。
    2. 只检查模块顶层（tree.body）的 FunctionDef / AsyncFunctionDef / ClassDef；
       嵌套函数、类内方法、__main__ 块内定义天然不在 tree.body 中，故不计入
       （跨模块契约只看模块顶层）。
    3. 跳过以 _ 开头的私有名。已知盲区（刻意不处理，供后人参考）：
       - 类内私有方法（如某个类里的 _chunk_text 成员）不属于模块顶层契约；
        - 模块级“私有但实际被多处复制的共享工具”（如某个顶层 _chunk_text）
          不在本检查范围内——它们需要的是显式工具目录，而非重复检测。
       **上述两条是「假设性举例」，不是债务清单**（2026-09-12 补注）：这两句曾被
       PROJECT_STATUS §六.1 转抄、再被执行清单第三次转抄，把一句虚构的例子变成了
       待办，让「多处复制的 _chunk_text」这个并不存在的债务被追查了两轮。真实存在过
       的同型债务是 rag/document_loader.load_and_split 与 rag/note_indexer._chunk_text
       的**同算法异名**（两处名字不同，本检查器按设计看不到），已于 P4-6 收拢到
       core/text_utils.split_paragraphs。
    4. 豁免机制：首次扫描得到的全部重复名写入
       checks/duplicate_name_exemptions.json，作为已知债务台账；此后仅“不在台账
       中的重复名”才报警（[DUP]）并以退出码 1 失败。豁免按名字匹配：台账中某名字
       若新增了副本位置，仍视为已知债务（存量模式），不会误报。
       **台账格式（P4-5c）**：对象数组 `{"name": 名字, "reason": 为何可放行,
       "since": 登记日期}`，解析统一走 checks/exemption_loader.py；裸字符串仍可读
       但会打印 [EXEMPTION_NO_REASON] 并以退出码 1 失败（P5-1 起生效）——写不出理由的
       豁免无法被质疑，只能被整体删或整体留。
    5. 过期检测（P4-1a 起）：台账中已不再重复的名字打印
       [STALE_EXEMPTION] 并同样以退出码 1 失败。原因：以“名字”为键的台账
       不随代码漂移自动失效（少了一份定义照样命中剩下的），只能累积、不能
       自报，必须靠硬失败迫使台账只减不增。

输出词根约定（全仓库统一，四个检查器一致）：
    [DUP]              —— 新增的未豁免违规（“东西是新的”）
    [STALE_EXEMPTION]  —— 台账条目已不再命中（“豁免是旧的”）
    两者语义相反，刻意使用完全不同的词根，避免读输出的人（或 AI）据错义行动。

退出码：
    0 —— 无未豁免重复公共名，且台账无过期条目；
    1 —— 存在未豁免重复公共名，或台账存在过期条目；
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

# 豁免台账路径：本文件所在目录（checks/）下的 duplicate_name_exemptions.json
EXEMPTIONS_FILE = Path(__file__).resolve().parent / "duplicate_name_exemptions.json"


def iter_py_files(root: Path):
    """按目录树遍历 root 下所有 .py 文件，就地剪枝 SKIP_DIRS 与隐藏目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地裁剪 dirnames：不深入 SKIP_DIRS 及任何 . 开头的隐藏目录，
        # 从而不会遍历 data/（7.6GB 模型）与 .venv/，保证秒级完成。
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
        # utf-8-sig：容忍 Windows 编辑器/终端写入的 BOM。带 BOM 的台账本身合法，
        # 若用裸 utf-8 读取会抛异常并使所有提交以退出码 2 被拒——fail-closed 的
        # 拒绝理由应当只留给真正非法的内容，而不是 UTF-8 的无害变体。
        data = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] 豁免台账无法解析：{EXEMPTIONS_FILE}（{exc}）", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, list):
        print(f"[ERROR] 豁免台账必须是 JSON 数组：{EXEMPTIONS_FILE}", file=sys.stderr)
        sys.exit(2)
    return collect_keys(data, EXEMPTIONS_FILE)


def scan(root: Path):
    """扫描全部 .py 文件，返回 顶层公共名 -> [module:lineno, ...] 的映射。"""
    name_locations = {}
    for py_file in iter_py_files(root):
        try:
            # utf-8-sig 会自动剥离文件头 BOM，兼容 Windows 编辑器保存的带 BOM 文件
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
        for node in tree.body:
            # 只统计模块顶层定义；嵌套函数/类内方法/__main__ 内定义不在 tree.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("_"):
                continue  # 私有名不构成跨模块公共契约，跳过（见 docstring 盲区说明）
            name_locations.setdefault(name, []).append(f"{module}:{node.lineno}")
    return name_locations


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
    name_locations = scan(root)

    # 只保留出现于 2 个及以上模块的公共名（重复定义）
    duplicates = {name: locs for name, locs in name_locations.items() if len(locs) > 1}
    dup_names = set(duplicates)

    # 未豁免 = 当前重复但不在台账中（即“新增重复”，需要拦截）
    unexempted = sorted(dup_names - exemptions)
    # 台账过时项 = 已登记豁免但当前已不再重复的名字（建议清理台账）
    stale = sorted(exemptions - dup_names)
    # 台账覆盖的存量重复 = 当前仍重复且已被豁免的名字（已知债务）
    exempted_dup = sorted(dup_names & exemptions)

    # 1) 打印未豁免的重复公共名（按字母序）及其全部出现位置
    for name in unexempted:
        print(f"[DUP] {name}")
        for loc in duplicates[name]:
            print(f"        {loc}")

    # 2) 打印台账过期条目（已不再重复的死条目，须清理）
    for name in stale:
        print(f"[STALE_EXEMPTION] {name}（已不再重复，建议从豁免清单中移除）")

    # 3) 统计与耗时
    print(
        f"重复公共名共 {len(dup_names)} 个，"
        f"其中未豁免 {len(unexempted)} 个，已豁免 {len(exempted_dup)} 个，"
        f"台账过期 {len(stale)} 个"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
