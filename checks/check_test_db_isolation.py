#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试数据库隔离守卫（checks/check_test_db_isolation.py）

目的：
    防止未来新增的测试文件直接操作生产库 data/memory.db。P0-5 已把存量
    测试改为调用统一 helper setup_test_db()（或显式切换 DB_PATH），但缺少
    机械护栏。本脚本只检查“使用了数据库但未隔离”的测试文件：
    - 不调用 setup_test_db()；
    - 也未手动把 DB_PATH 切到临时库；
    - 却检测到 memory.database 导入或 init_db/get_connection/get_db 调用。
    完全不碰数据库的测试文件不受打扰。

扫描范围：
    仅 tests/ 目录下文件名匹配 test_*.py 的文件（_test_db_helpers.py、
    __init__.py 等辅助文件天然不匹配，不在范围内）。

检测口径（刻意保持简单，供后人参考）：
    - “使用 DB”：
        1) ast.Import / ast.ImportFrom 模块名包含 memory.database；
        2) ast.Call 的 func 为裸名或属性名，且名字属于
           {init_db, get_connection, get_db}。
    - “已隔离”：
        1) 存在对 setup_test_db 的调用（裸名或属性名）；
        2) 存在对 DB_PATH 的赋值（Assign/AnnAssign/AugAssign 目标含
           Attribute.attr == "DB_PATH"，或 Subscript 键为常量 "DB_PATH"）；
        3) **字符串靶子**（P6-6④ 新增，2026-09-13）：存在对 patch 的调用
           （裸名 patch / 属性名 patch / patch.object / patch.dict），其调用参数里
           有一个**字符串常量**含 "DB_PATH"——即
           `with patch("memory.database.DB_PATH", tmp):` 这类逐测隔离。
           这条规则落地后，`test_db_isolation_exemptions.json` 里唯一那条
           （`test_resource_log.py`，登记理由就是"检查器看不见字符串靶子"）已经
           **回收**，该台账归零。
           为什么不看 patch 被赋给了谁、包住了哪一段：检测口径刻意保持"有没有
           做这件事"这个粒度；一个写了 `patch("memory.database.DB_PATH", ...)`
           却不生效的测试是另一种错误（会以真实 DB 断言失败的形式暴露），
           由测试本身负责，不由静态检查器替它兜底。
    - “顺序正确”（P4-3c 新增，P4-3 补做）：
        仅对**已判定使用 DB**的文件追加要求——隔离调用必须出现在首个业务模块
        import 之前。业务模块前缀：memory. / core. / agents. / business. / tools.。
        遍历用「顶层语句保序 + 下钻 If/Try 体」的浅遍历（模块级条件导入是常见写法）。
        为什么必须是附加条件而非独立触发：不碰 DB 的测试文件（如纯函数的
        tests/test_json_utils.py 导入 core.json_utils）不该被迫引入 DB 隔离，
        否则新规则会把"不使用 DB"的文件一并判违规。
        只查"是否存在隔离调用"是不够的——一个在第 200 行才隔离的文件照样通过，
        而那时业务模块早已 import 完毕、DB_PATH 也可能已被读走。
    - 已知盲区：已无（P6-6④ 起，`patch("memory.database.DB_PATH", ...)` 这类
      **字符串靶子**已被认作隔离；此前 `test_resource_log.py` 因这条盲区登记在
      豁免台账里，现该条已回收）。其余未被识别的隔离写法（例如把隔离逻辑藏在
      被 import 的辅助模块里）仍会报警，此时按「禁止绕过规范」正面处理：优先
      改成 `setup_test_db()` 或显式 `DB_PATH` 赋值，确属误报再登记豁免。

豁免机制：
    命中但文件名在 checks/test_db_isolation_exemptions.json
    （P4-5c 起为对象数组 `{"name": 文件名, "reason": …, "since": …}`，解析统一走
    checks/exemption_loader.py；裸字符串仍可读但会打印 [EXEMPTION_NO_REASON]）
    中的文件跳过，不参与退出码判定。
    过期检测（P4-1a 起）：台账里有、本次扫描未被命中的文件名打印
    [STALE_EXEMPTION] 并以退出码 1 失败。本台账以“文件名”为键，不随代码
    行号漂移，但会在“文件被修好或被删除”时静默变成死条目——只能累积、
    不能自报，故必须计算过期并计入退出码。

输出词根约定（全仓库统一，四个检查器一致）：
    [NO_DB_ISOLATION]  —— 新增的未豁免违规（“东西是新的”）
    [STALE_EXEMPTION]  —— 台账条目已不再命中（“豁免是旧的”）

退出码：
    0 —— 无未豁免的“使用 DB 但未隔离”测试文件，且台账无过期条目；
    1 —— 存在未豁免的越界文件，或台账存在过期条目；
    2 —— 豁免台账格式非法等内部错误（fail-closed）。
"""

import ast
import json
import sys
import time
from pathlib import Path

# 共享台账解析（checks/exemption_loader.py，P4-5c 统一格式）：
# 把本文件所在目录加入 sys.path，兼容"直跑脚本"与"pre-commit 钩子按文件路径加载"两种方式
_CHECKS_DIR = Path(__file__).resolve().parent
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))
from exemption_loader import collect_keys  # noqa: E402

# 项目 tests/ 目录（本文件位于 checks/ 下）
TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

# 豁免台账路径：本文件所在目录（checks/）下的 test_db_isolation_exemptions.json
EXEMPTIONS_FILE = (
    Path(__file__).resolve().parent / "test_db_isolation_exemptions.json"
)

# 视为“使用数据库”的 database API 调用名
DB_API_CALLS = {"init_db", "get_connection", "get_db"}

# 目标模块名（导入或 from-import 时模块名包含即视为使用数据库）
DB_MODULE_MARK = "memory.database"

# 业务模块前缀（P4-3c 顺序规则用）：这些模块一旦 import，就可能已经读走 DB_PATH
BIZ_PREFIXES = ("memory.", "core.", "agents.", "business.", "tools.")

# 视为“字符串靶子隔离”的 patch 调用名（P6-6④）：裸名或属性名命中即算
PATCH_CALL_NAMES = {"patch", "patch.object", "patch.dict"}

# 字符串靶子里必须出现的目标名
DB_PATH_MARK = "DB_PATH"


def iter_test_files():
    """遍历 tests/ 下所有 test_*.py（按文件名排序，保证输出稳定）。"""
    for py_file in sorted(TESTS_DIR.glob("test_*.py")):
        yield py_file


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


def _target_sets_db_path(target: ast.AST) -> bool:
    """判断赋值目标中是否存在 DB_PATH（属性形式或下标键形式）。"""
    for node in ast.walk(target):
        if isinstance(node, ast.Attribute) and node.attr == "DB_PATH":
            return True
        if isinstance(node, ast.Subscript):
            key = node.slice
            # Python 3.9+ 简单下标：slice 直接是值节点
            if isinstance(key, ast.Constant) and key.value == "DB_PATH":
                return True
    return False


def _iter_top_level(body):
    """保序遍历顶层语句，并下钻 If/Try 的语句体（模块级条件导入是常见写法）"""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try)):
            yield from _iter_top_level(node.body)
            for handler in getattr(node, "handlers", []):
                yield from _iter_top_level(handler.body)
            yield from _iter_top_level(getattr(node, "orelse", []) or [])
            yield from _iter_top_level(getattr(node, "finalbody", []) or [])


def _setup_call_lineno(node):
    """若该顶层语句是 setup_test_db() 调用，返回其行号，否则 None

    两种写法都要认：独立语句 `setup_test_db()` 与赋值 `_TMP_DIR = setup_test_db()`
    （后者是本项目迁移到共享 helper 后的实际写法 —— 只认 ast.Expr 会把 7 个已正确
    隔离的文件全部误判，实测踩过）。
    """
    call = None
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
    elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
        call = node.value
    elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call):
        call = node.value
    if call is None:
        return None
    func = call.func
    if isinstance(func, ast.Name) and func.id == "setup_test_db":
        return node.lineno
    if isinstance(func, ast.Attribute) and func.attr == "setup_test_db":
        return node.lineno
    return None


def _order_positions(tree):
    """返回 (首个业务模块 import 行号, 首个顶层 setup_test_db() 调用行号)

    两者都可能为 None（无业务 import / 未在顶层调用）。判断依据是"谁先出现"，
    故必须用保序的 _iter_top_level，不能用 ast.walk（它不保序）。
    """
    first_biz = None
    first_setup = None
    for node in _iter_top_level(tree.body):
        lineno = getattr(node, "lineno", None)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif node.level == 0 and node.module:
                mods = [node.module]
            else:
                mods = []
            if first_biz is None and any(m.startswith(BIZ_PREFIXES) for m in mods):
                first_biz = lineno
        elif first_setup is None:
            first_setup = _setup_call_lineno(node)
    return first_biz, first_setup


def _call_name(call: ast.Call) -> str:
    """调用名（裸名原样，属性名取其 attr；`a.b.patch` 与 `patch` 同视）"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _patch_targets_db_path(call: ast.Call) -> bool:
    """该调用是否是"用字符串靶子把 DB_PATH 换掉"（P6-6④）

    认三种写法，它们的共同点是**靶子是字符串字面量、AST 上没有 DB_PATH 节点**：
        with patch("memory.database.DB_PATH", tmp):
        with patch.object(_database_module, "DB_PATH", tmp):
        with mock.patch.dict(os.environ, {"...": "DB_PATH"}):   # 同样按字符串处理
    参数里任意一个字符串常量含 "DB_PATH" 即算命中；判据刻意不追究"patch 是否真的
    生效"——那是测试自己的责任，静态检查器只回答"有没有做隔离这件事"。
    """
    if _call_name(call) not in PATCH_CALL_NAMES:
        return False
    for node in ast.walk(call):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and DB_PATH_MARK in node.value:
            return True
    return False


def analyze_file(py_file: Path):
    """分析单个测试文件，返回 (使用DB, 调用setup_test_db, 手动切换DB_PATH, 首个业务import行, 首个顶层隔离调用行)。"""
    try:
        text = py_file.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(text, filename=str(py_file))
    except SyntaxError as exc:
        # 语法错误 = 扫描覆盖率静默丢失，故 fail-closed（退出码 2）而非跳过（2026-09-12 实测）
        print(
            f"[ERROR] 语法错误，无法扫描 {py_file.name}：{exc.msg}（行 {exc.lineno}）",
            file=sys.stderr,
        )
        sys.exit(2)

    uses_db = False
    calls_setup = False
    sets_db_path = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id == "setup_test_db":
                    calls_setup = True
                elif func.id in DB_API_CALLS:
                    uses_db = True
            elif isinstance(func, ast.Attribute):
                if func.attr == "setup_test_db":
                    calls_setup = True
                elif func.attr in DB_API_CALLS:
                    uses_db = True
            # 字符串靶子隔离（P6-6④）：patch("memory.database.DB_PATH", ...)
            if _patch_targets_db_path(node):
                sets_db_path = True
        elif isinstance(node, ast.Import):
            if any(DB_MODULE_MARK in (alias.name or "") for alias in node.names):
                uses_db = True
        elif isinstance(node, ast.ImportFrom):
            if node.module and DB_MODULE_MARK in node.module:
                uses_db = True
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if target is not None and _target_sets_db_path(target):
                    sets_db_path = True
    first_biz, first_setup = _order_positions(tree)
    return uses_db, calls_setup, sets_db_path, first_biz, first_setup


def main() -> int:
    started = time.perf_counter()

    # Windows 控制台/重定向编码兜底：尽量以 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    exemptions = load_exemptions()

    flagged = []      # (文件名, 原因) —— 使用 DB 且未隔离（原始命中）
    uses_db_count = 0
    for py_file in iter_test_files():
        uses_db, calls_setup, sets_db_path, first_biz, first_setup = analyze_file(py_file)
        if not uses_db:
            continue
        uses_db_count += 1
        if not calls_setup and not sets_db_path:
            reason = (
                "检测到 memory.database 导入或 init_db/get_connection/get_db "
                "调用，但未调用 setup_test_db() 且未发现 DB_PATH 切换"
            )
            flagged.append((py_file.name, reason))
        elif calls_setup:
            # 顺序规则（P4-3c）：附加条件，只在"已使用 DB"的文件上生效
            if first_setup is None:
                flagged.append((
                    py_file.name,
                    "setup_test_db() 不在模块顶层调用——隔离必须发生在模块级，"
                    "否则无法保证早于业务模块 import",
                ))
            elif first_biz is not None and first_setup > first_biz:
                flagged.append((
                    py_file.name,
                    f"隔离调用晚于首个业务模块 import（setup_test_db 在第 {first_setup} 行，"
                    f"首个业务 import 在第 {first_biz} 行）",
                ))

    # 豁免过滤：文件名在台账中的文件跳过
    unexempted = [(name, reason) for name, reason in flagged if name not in exemptions]

    # 输出未豁免的未隔离文件（按文件名排序）
    for name, reason in sorted(unexempted):
        print(f"[NO_DB_ISOLATION] {name}：{reason}")

    # 台账过期条目 = 台账里有、本次扫描未被命中（豁免是旧的）
    stale = sorted(exemptions - {name for name, _ in flagged})
    for name in stale:
        print(f"[STALE_EXEMPTION] {name}（已不再命中，建议从台账移除）")

    # 统计与耗时
    print(
        f"使用 DB 的测试文件共 {uses_db_count} 个，"
        f"其中未隔离 {len(flagged)} 个，未豁免 {len(unexempted)} 个，"
        f"台账过期 {len(stale)} 个"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
