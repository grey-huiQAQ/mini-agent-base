#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注释腐烂哨兵（checks/check_comment_freshness.py）

目的：
    找出项目中携带“债务关键词”但未标注日期/版本的无主注释与 docstring，
    拦截“新增”的无日期标记注释，防止注释腐烂持续扩散。存量全部写入豁免
    台账（文件:行号），作为已知债务；后续新增未标记注释即报警。

检测对象与关键词：
    - 注释：用 tokenize 提取 COMMENT token（字符串里的 # 不会被误判）；
    - docstring：用 ast 定位模块/类/函数首条字符串常量的行区间；
    - 关键词（大小写不敏感匹配 ASCII 词汇，中文按子串匹配）：
        TODO / FIXME / XXX / 需要实现 / 待实现 / 待补 / 可扩展 / 预留 /
        「后续」——但仅当与 待/未/暂/需 相邻时才算命中
    - 豁免标记（须与关键词同一行）：
        (YYYY-MM-DD) 或 (vX.Y.Z)，形如 (2025-01-05) / (v0.3.1)。

关键词收窄（P4-5a0，2026-09-12）：
    原关键词表里的「暂不」「后续」是中文的日常行文用词，实测把大量**行为描述**
    误判成债务——如「None（暂不支持自动执行）」（描述返回值语义）、
    「scan_text 为后续关键词扫描用文本」（描述参数用途）、
    「超时不算失败，而是影响后续批次的默认容量」（描述测试语义）。50 条存量里
    这一类约占五分之一。处置：
      - 「暂不」移出关键词表；
      - 「后续」改为必须与 待/未/暂/需 相邻（如「后续待接线」）才命中；
      - 「预留」保留（抽查确认都是真的接口预留点，如 token 预算 / 模型路由接口）。
    这使得原先"要改代码才能清掉"的条目降级为"删台账条目"，是让这本台账变短
    而不再增噪的根本手段；正则只会让命中集合变小，不会新增命中。

设计取舍（供后人参考）：
    - 日期/版本标记按“行”粒度判断：关键词与标记不在同一行即视为未标注；
    - 只检查注释与 docstring，不检查普通字符串（提示文案里的“后续”等
      不会被误报）；
    - 不做语义判断：带日期的 TODO 视为有意记录，直接放行。

豁免机制：
    存量命中按“文件相对路径:行号”写入
    checks/comment_freshness_exemptions.json。
    **台账格式（P4-5c）**：对象数组 `{"key": 文件:行号, "reason": 为何可放行,
    "since": 登记日期}`，解析统一走 checks/exemption_loader.py；裸字符串仍可读
    但会打印 [EXEMPTION_NO_REASON] 并以退出码 1 失败（P5-1 起生效）。
    与 P1-1/P1-2 一致：豁免只放行存量，行号变动即失效，强迫后续处理。
    过期检测（P4-1a 起）：台账里有、本次扫描未命中的条目打印
    [STALE_EXEMPTION] 并以退出码 1 失败——位置级台账本就会随代码位移失配，
    把失配变成硬失败，才能保证台账不沉淀死条目。

输出词根约定（全仓库统一，四个检查器一致）：
    [NO_DATE]          —— 新增的未豁免违规（“东西是新的”）
    [STALE_EXEMPTION]  —— 台账条目已不再命中（“豁免是旧的”）
    本检查器早期把前者印成 [STALE_COMMENT]，与后者的词根撞车且语义相反，
    P4-1 起彻底分开。

退出码：
    0 —— 无未豁免的无日期标记注释，且台账无过期条目；
    1 —— 存在未豁免的无日期标记注释，或台账存在过期条目；
    2 —— 豁免台账缺失或格式非法等内部错误（fail-closed）。
"""

import ast
import io
import json
import os
import re
import sys
import time
import tokenize
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

# 豁免台账路径：本文件所在目录（checks/）下的 comment_freshness_exemptions.json
EXEMPTIONS_FILE = (
    Path(__file__).resolve().parent / "comment_freshness_exemptions.json"
)

# 债务关键词：ASCII 词汇大小写不敏感，中文按子串匹配。
# 「后续」限定为与 待/未/暂/需 相邻才命中（P4-5a0 收窄，理由见模块 docstring）。
KEYWORD_RE = re.compile(
    r"(?i:TODO|FIXME|XXX)|需要实现|待实现|待补|可扩展|预留"
    r"|(?:待|未|暂|需)后续|后续(?:待|未|暂|需)"
)

# 日期/版本豁免标记：同行的 (YYYY-MM-DD) 或 (vX.Y.Z) 视为已标注
MARKER_RE = re.compile(r"\(\d{4}-\d{2}-\d{2}\)|\(v\d+\.\d+\.\d+\)")

# 输出片段最大长度
SNIPPET_MAX = 60


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


def collect_docstring_lines(tree: ast.Module) -> set:
    """返回所有 docstring 占用的源码行号集合（模块/类/函数首条字符串）。"""
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = body[0].lineno
                end = body[0].end_lineno or start
                doc_lines.update(range(start, end + 1))
    return doc_lines


def scan(root: Path):
    """扫描全部 .py 文件，返回 命中键(文件:行号) -> 片段 的映射。"""
    hits = {}
    for py_file in iter_py_files(root):
        module = py_file.relative_to(root).as_posix()
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
                f"[ERROR] 语法错误，无法扫描 {module}：{exc.msg}（行 {exc.lineno}）",
                file=sys.stderr,
            )
            sys.exit(2)
        lines = text.splitlines()

        # 1) 注释：tokenize 提取 COMMENT token（行号 1-based）
        comment_lines = {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    comment_lines[tok.start[0]] = tok.string.strip()
        except (tokenize.TokenError, IndentationError) as exc:
            # 同语法错误：tokenize 失败同样意味着扫描覆盖率丢失，fail-closed（退出码 2）
            print(f"[ERROR] tokenize 失败，无法扫描 {module}：{exc}", file=sys.stderr)
            sys.exit(2)

        # 2) docstring 行区间
        doc_lines = collect_docstring_lines(tree)

        # 3) 逐行判定：关键词命中且同行无日期/版本标记
        for lineno in sorted(set(comment_lines) | doc_lines):
            if lineno < 1 or lineno > len(lines):
                continue
            if lineno in comment_lines:
                # 注释行：在注释文本内查找关键词与标记
                snippet = comment_lines[lineno]
            else:
                snippet = lines[lineno - 1].strip()
            if KEYWORD_RE.search(snippet) and not MARKER_RE.search(snippet):
                key = f"{module}:{lineno}"
                if key not in hits:  # 同行多关键词只记一处
                    hits[key] = snippet[:SNIPPET_MAX]
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

    # 按 (文件路径, 行号数值) 排序
    ordered = sorted(
        ((key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1]), key, snippet)
         for key, snippet in hits.items()),
        key=lambda item: (item[0], item[1]),
    )
    unexempted = [(path, lineno, key, snippet)
                  for path, lineno, key, snippet in ordered
                  if key not in exemptions]

    # 打印未豁免的无日期标记注释（[NO_DATE] = 违规是新的）
    for path, lineno, key, snippet in unexempted:
        print(f"[NO_DATE] {key}: {snippet}")

    # 台账过期条目 = 台账里有、本次扫描未命中（豁免是旧的）
    # 注意 hits 是 dict（键 -> 片段），求差集前必须 set(hits)
    stale = sorted(exemptions - set(hits))
    for key in stale:
        print(f"[STALE_EXEMPTION] {key}（已不再命中，建议从台账移除）")

    # 统计与耗时
    print(
        f"无日期注释共 {len(hits)} 处，未豁免 {len(unexempted)} 处，"
        f"台账过期 {len(stale)} 处"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
