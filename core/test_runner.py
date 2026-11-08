# -*- coding: utf-8 -*-
"""
辅助测试运行器 v0.1（Auxiliary Test Runner）

职责：
- 自动遍历 tests/test_*.py，逐个以子进程执行（自带超时看门狗：僵死进程强制终止）；
- 失败自动重试（默认 1 次）；解析输出中的 [OK] 标记统计用例数（无法解析时为 0）；
- 汇总结果写入 SQLite 表 test_runs（先写 running 行，结束后更新），
  提供最近回归结果查询与 Markdown 渲染（Gradio UI 展示用）；
- 提供 CLI 入口：python core/test_runner.py（也可 --list 仅列出测试文件）。

设计说明：
- 独立轻量：不依赖 pytest，与项目现有「逐文件直跑」方式（每个测试文件自带
  __main__ 直接运行）完全一致；
- 非阻塞：UI 中放入后台线程调用 run_all_tests()，本模块自身不引入新阻塞；
- 兼容性：只新增 test_runs 表（CREATE TABLE IF NOT EXISTS），不改任何现有表；
- 防误伤：任何单文件失败 / 超时只影响该文件，不影响其它文件与整体流程；
- 超时这一格的记账（阶段二 组 2，2026-10-18）：记**实测等待时长**而不是阈值常数，
  且 kill 之后的管道收集与关管道**都有期限**——依据与两条实测形状见
  `POST_KILL_COLLECT_GRACE_SEC` 旁注释与 `_run_one_file` 的 docstring；
- 结果落库全程 try/except，写库失败不影响测试本身运行。
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 默认参数（可被 CLI / 函数参数覆盖）
# 单文件超时看门狗（秒）。取值依据是**实测分布**，不是"最慢文件"（P4-13 子项 ②）：
#   同一文件 `test_daily_planning.py` 的历史耗时跨 15.2s ~ 298.1s（run #45 vs #19），
#   约 19 倍摆动；它在 run #7 正是被 180s 砍掉的，却在实际运行中合法跑完过 298.1s。
#   全仓历史上有 8 次「合法完成」超过 180s，涉及 6 个文件（最大 298.1s）。
#   （旧注释称"最慢文件 test_note_enhancements.py 约 171s，180s 覆盖绝大多数"，
#     该依据与自身数据表冲突：171s 低于表内 298.1s 的合法完成，属错误依据。）
# 360 = 覆盖观测到的最大合法完成 298.1s 之后留约 20% 余量。
# 为什么阈值不再是关键决策：看门狗唯一的职责是抓"无限挂"，同时要避让"合法慢"，
#   而后者在 19 倍摆动的变量上无法由一个固定值同时满足——真正的修复在
#   `AGENTS.md`「执行纪律」第 2 条（阈值降级为"待复验"的排队标记，不是故障判定）。
#   在这个前提下阈值偏高只多等：期望成本 ≈ 37.5% × 阈值（300→112s、360→135s，
#   每轮差价约 23s），而一次误杀的代价是白等一整轮阈值 + 一轮单跑复验，故宁可偏高。
DEFAULT_TIMEOUT_SEC = 360
DEFAULT_RETRIES = 1         # 失败重试次数（超时文件不重试，避免等待一整轮超时周期）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"

# 「杀完之后，再给管道一次收集机会」的宽限（秒）（阶段二 组 2，2026-10-18 本机实测）：
# `subprocess.run()` 的超时分支在 Windows 上 kill 之后调的是**不带 timeout** 的
# `communicate()`，它会等管道写端关闭——写端若被子进程的后代持有，这一等就没有上限
# （#195 那条超时行记 360.0s、整轮墙钟却比逐文件合计多 5,473.0s，缺口只出现在含超时
# 的那一轮：四条干净轮的同一缺口是 0.1s）。本机两条实测形状：
#   ① 子进程自己挂（没有后代持写端）⇒ kill 后第二次 collect **0.00s** 返回全部已写入
#     管道的输出，`stdout.close()`/`wait()` 各 0.00s；
#   ② 有后代持写端 ⇒ 第二次 collect 收不到（它要等 EOF），而 **`stdout.close()` block
#     13.50s**（要等那个后代自己睡完）—— 所以收集必须有期限，且关管道不能放在主流程。
# 5s 的取值口径：相对 ①（0.00s）留了两个数量级的余量，相对 ②（无上限）是一个封顶；
# 它只加在**已被看门狗判定挂掉**的那一个文件上，正常文件一个字节都不经过这里。
POST_KILL_COLLECT_GRACE_SEC = 5.0

# 逐文件结果文档（P4-4）：只重写下列标记之间的区块，标记外的说明文字由人维护。
# 动机：这份文档原先是"让 AI/人顺手更新"的手写件，实测必然腐烂——PROJECT_STATUS §七
# 把读者指向它，而它的头行停在 2026-09-06 那轮，2026-09-12 的实测结果只活在 commit
# message 里。把记录从"手写"改成"程序的输出"，是让记录不再腐烂的唯一办法。
TEST_RESULTS_FILE = TESTS_DIR / "TEST_RESULTS.md"
GENERATED_BEGIN = "<!-- BEGIN GENERATED:test-results -->"
GENERATED_END = "<!-- END GENERATED:test-results -->"

# 以 `python core/test_runner.py` 直跑时，脚本目录（core/）会加入 sys.path 而
# 项目根目录不会，导致 `from memory.database import get_db` 等导入失败；
# 这里显式把项目根目录加入 sys.path（被 app 等导入时根目录已在路径中，幂等）。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 用例数解析：每个测试函数通过时打印一行 [OK] ...（项目既有约定）
_OK_RE = re.compile(r"^\[OK\]", re.MULTILINE)

# 控制台安全输出（GBK 兼容）：Windows 旧编码控制台 / 管道重定向场景下
# sys.stdout 编码为 ANSI 代码页（如 cp936/GBK），无法编码 emoji
# （✅ U+2705 等），直接 print 会抛 UnicodeEncodeError 中断回归汇总/明细
# 打印（连带丢失退出码）。与项目 agents/task_executor.log_safe 同思路：
# 保留 CJK、CJK 标点、全角与 ASCII 可打印字符，剔除其余不可编码字符；
# 状态 emoji 先替换为 ASCII 标记以保持可读性。仅影响控制台文本，不影响
# 测试执行、通过判定与 test_runs 数据库记录。
_CONSOLE_EMOJI_MARK = {"✅": "[OK]", "⏱️": "[超时]", "❌": "[FAIL]"}
_CONSOLE_SAFE_RE = re.compile(
    r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u0020-\u007e]")


def _console_safe(text) -> str:
    """把任意文本转为控制台安全文本（emoji → ASCII 标记，其余剔除）"""
    s = str(text or "")
    for emoji, mark in _CONSOLE_EMOJI_MARK.items():
        s = s.replace(emoji, mark)
    return _CONSOLE_SAFE_RE.sub("", s)

# test_runs 表结构（与 memory/database.py 中的定义保持一致；此处为独立场景自建用）
_TEST_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    total_files INTEGER DEFAULT 0,
    passed_files INTEGER DEFAULT 0,
    failed_files INTEGER DEFAULT 0,
    timed_out_files INTEGER DEFAULT 0,
    total_cases INTEGER DEFAULT 0,
    passed_cases INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0,
    details TEXT DEFAULT '[]',
    summary TEXT DEFAULT ''
)
"""

# 防重复启动：同一进程内只允许一轮回归同时运行（UI 重复点击保护）
_run_lock = threading.Lock()
_running = False


def is_test_run_running() -> bool:
    """是否有一轮回归正在后台运行（UI 防重复启动用）"""
    return _running


def _get_conn(db_path=None):
    """获取连接：显式 db_path（测试/独立使用传临时库）或默认项目库"""
    if db_path is not None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    from memory.database import get_db
    return get_db()


def discover_test_files(tests_dir=None):
    """发现 tests/test_*.py 测试文件（按文件名排序，保证输出稳定）

    说明（P4-2，2026-09-12）：运行器已迁回 core/，不再位于 tests/ 内，因此
    "排除运行器自身"的分支（Phase 3c 为迁入 tests/ 而加）随之删除——它已是死代码。
    """
    base = Path(tests_dir) if tests_dir else TESTS_DIR
    if not base.is_dir():
        return []
    return sorted(base.glob("test_*.py"))


def _parse_cases(output):
    """解析输出中的 [OK] 标记数（用例数粗粒度统计；无法解析返回 0）"""
    try:
        return len(_OK_RE.findall(output or ""))
    except Exception:
        return 0


def _normalize_only_names(only) -> list:
    """把 --files 传来的名字规整为小写去 .py 的文件名列表（保持入参顺序，去重）

    接受 "a" / "a.py" / "A.PY" 三种写法（大小写不敏感）。非字符串元素抛
    ValueError——静默忽略非法元素会让"名字打错"变成"跑了个空集"。
    """
    names = []
    for item in only or []:
        if not isinstance(item, str):
            raise ValueError(f"--files 只接受文件名，收到非法元素：{item!r}")
        name = item.strip()
        if not name:
            continue
        if name.lower().endswith(".py"):
            name = name[:-3]
        if name and name.lower() not in names:
            names.append(name.lower())
    return names


def _filter_by_names(files, only) -> list:
    """按 only 里的文件名过滤（**只作过滤器，不重排**）

    输出顺序恒等于 `discover_test_files()` 的排序结果，与 --files 的书写顺序
    无关——逐文件输出/文档的可复现性依赖这一点。

    Raises:
        ValueError: only 中有任一名字匹配不上（禁止"匹配不到就跑空集"：
            那会产出一条 total_files=0 却看起来成功的运行记录，污染 test_runs 台账）。
    """
    names = _normalize_only_names(only)
    if not names:
        raise ValueError("--files 传了空列表：请给出至少一个测试文件名")
    matched = [f for f in files if Path(f).name[:-3].lower() in names]
    found = {Path(f).name[:-3].lower() for f in matched}
    missing = [n for n in names if n not in found]
    if missing:
        available = ", ".join(sorted(Path(f).name[:-3] for f in files))
        raise ValueError(
            f"--files 中的名字匹配不到测试文件：{', '.join(missing)}；"
            f"可用文件：{available}")
    return matched


def _discard_pipes_and_reap(proc):
    """把「关管道 ＋ 收尸」转到后台守护线程（阶段二 组 2，形状由实测决定）

    为什么不直接在主流程里关：`proc.stdout.close()` 在管道写端被子进程的**后代**持有
    时会 block 到那个后代退出（本机实测 13.50s），而本分支的职责恰恰是「不再等这个
    已经挂掉的文件」。守护线程不参与解释器退出等待 ⇒ 主流程立即返回；代价是那一对
    管道句柄由后台线程在后代退出后才关掉（OS 会在本进程结束时兜底）。
    """
    def _worker():
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=POST_KILL_COLLECT_GRACE_SEC)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _run_one_file(test_file, timeout_sec=DEFAULT_TIMEOUT_SEC,
                  retries=DEFAULT_RETRIES) -> dict:
    """执行单个测试文件（失败自动重试，超时强制终止），返回结果 dict

    关于超时行的 `cases` 口径（P4-12 子项 2 起）：它是
    **"被看门狗杀掉之前子进程已打印的 [OK] 行数"，不是通过数**。
    看到"超时 cases=7"不要读成"7/8 通过"——那个文件可能只是打印完 7 条
    之后卡在某处（甚至卡在退出期），第 8 条是否通过无从得知。
    组 2 起再加一层：若 kill 之后 `POST_KILL_COLLECT_GRACE_SEC` 内仍收不回管道
    （写端被后代持有），`cases` 记 **0 且 tail 写明原因** ⇒ 这个 0 是「没收到」，
    不再冒充「一条都没打印」。

    关于超时行的 `seconds` 口径（阶段二 组 2，2026-10-18 起）：它是**实测等待时长**
    （`time.monotonic()` 前后差），不是写死的 `float(timeout_sec)`。旧口径把这一格
    记成阈值常数，于是含超时轮的「墙钟 − 逐文件合计」被系统性低估（实测 #195 差
    5,473.0s，四条干净轮只差 0.1s）——那条缺口就是本轮要修的测量本身。

    为什么不用 `subprocess.run()`（组 2）：它的超时分支在 Windows 上 kill 之后调的是
    **不带 timeout** 的 `communicate()`（本仓 `C:\\...\\Lib\\subprocess.py` 的
    `run()`），管道写端被后代持有时那就是无限阻塞；改成 `Popen` ＋ 两次带期限的
    `communicate()` 才能封顶。副作用如实记：Windows 上 `communicate()` 抛出的
    `TimeoutExpired.output` **是 None**（只有 POSIX 那条 `_communicate` 会填），
    所以部分输出**必须**从第二次 collect 的返回值拿，不能读异常属性。

    Returns:
        {"file", "ok", "timed_out", "retried", "cases", "seconds", "tail"}
    """
    fname = Path(test_file).name
    result = {"file": fname, "ok": False, "timed_out": False,
              "retried": False, "cases": 0, "seconds": 0.0, "tail": ""}
    cmd = [sys.executable, str(Path(test_file))]
    # 两个环境变量各管一件事，缺任何一个都会静默丢证据（P4-13 子项 ①）：
    # - PYTHONIOENCODING=utf-8：保证 [OK] 标记的**编码**可解析
    #   （Windows 控制台默认 GBK，子进程按 GBK 输出时中文标记会乱码/解析失败）；
    # - PYTHONUNBUFFERED=1：保证**行行落到管道**。子进程 stdout 重定向到管道时是
    #   块缓冲，print() 的内容先留在子进程用户态缓冲区里；看门狗 kill() 一到，
    #   缓冲区随进程一起消失，父进程的 communicate() 只能拿到已写进管道的字节。
    #   实测（P4-12 子项 5）：同一条命令绕过 shutdown 时 cases 记 0、正常退出记 8；
    #   而 tests/ 下 39 个文件里只有 1 个用过 flush=True —— 故没有这个变量，
    #   超时分支捕获的 cases 与 tail 对 38/39 个文件是空转的。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(1, max_attempts + 1):
        attempt_started = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
                cwd=str(PROJECT_ROOT),
            )
            stdout, stderr = proc.communicate(timeout=float(timeout_sec))
            last_output = (stdout or "") + (stderr or "")
            result["seconds"] = time.monotonic() - attempt_started
            result["cases"] = _parse_cases(last_output)
            # 通过判定：退出码 0 即视为通过。项目测试约定是"失败即抛异常 → 非零
            # 退出码"，故返回码是最可靠信号；个别文件使用自定义 PASSED 标语
            # （如 approval_persistence），不能依赖统一文案。
            result["ok"] = (proc.returncode == 0)
            result["tail"] = "\n".join(last_output.splitlines()[-15:])
            if result["ok"]:
                break
            if attempt < max_attempts:
                result["retried"] = True
                print(f"[TestRunner] {fname} 第 {attempt} 次执行失败"
                      f"（退出码 {proc.returncode}），重试中…")
        except subprocess.TimeoutExpired:
            # 看门狗：超过 timeout_sec 强制终止。视为确定性问题不再重试
            #（避免再等待一整轮超时周期）。
            # `run()` 里那句"内部已 kill"现在由本分支自己负责：kill() 实测 0.00s 返回。
            result["timed_out"] = True
            result["ok"] = False
            proc.kill()
            collected = None
            collect_gave_up = False
            try:
                # 第二次 collect 是**带期限**的（这就是原来那个无限阻塞的洞）
                so, se = proc.communicate(
                    timeout=POST_KILL_COLLECT_GRACE_SEC)
                collected = (so or "") + (se or "")
            except subprocess.TimeoutExpired:
                collect_gave_up = True
            # 实测等待时长：从发起这一次尝试，到"确定它挂了并且我们不再等了"为止。
            # 放在 collect 之后取 ⇒ 它包含宽限期，这正是墙钟里真实发生的那段。
            result["seconds"] = time.monotonic() - attempt_started
            head = (f"[超时] 超过 {timeout_sec:g}s 看门狗，已强制终止测试进程；"
                    f"实测等待 {result['seconds']:.1f}s")
            if collect_gave_up:
                _discard_pipes_and_reap(proc)
                result["cases"] = 0
                result["tail"] = (
                    f"{head}；kill 后 {POST_KILL_COLLECT_GRACE_SEC:g}s 内仍没收回管道"
                    f"（写端被子进程的后代持有）⇒ cases=0 是「没收到」，"
                    f"不代表一条 [OK] 都没打印；关管道与收尸已转后台线程")
            else:
                # P4-12 子项 2 / P4-13 子项 1：超时时尽量把已捕获的输出留下。
                # **拿得到的前提是子进程已把字节写入管道**；未 flush 的输出随其用户态
                # 缓冲区一起丢失，故本分支须配合本函数注入的 PYTHONUNBUFFERED 才真正
                # 生效（P4-12 时曾把这里写成"部分输出本来就拿得到"，那作为事实是错的，
                # 已被实测反驳：不带 flush 时 exc.stdout 为空串）。
                partial_text = collected or ""
                # cases 口径：被杀之前**已打印**的 [OK] 行数，不是通过数（见 docstring）
                result["cases"] = _parse_cases(partial_text)
                tail_lines = partial_text.splitlines()[-15:]
                result["tail"] = "\n".join(
                    [f"{head}；被杀前已打印 [OK] {result['cases']} 条"]
                    + tail_lines)
            break
        except Exception as e:
            result["seconds"] = time.monotonic() - attempt_started
            result["tail"] = f"[执行异常] {e}"
            # 换成裸 Popen 之后要补回的一条保证：`subprocess.run()` 内部是
            # `with Popen(...)`，任何异常都会 kill ＋ 关管道。这里不补就会留下
            # 一个还在跑的子进程和一对没关的句柄（"执行异常"轮因此比旧写法更脏）。
            # 如实登记：本分支没有用例钉它——触发它需要让 communicate() 抛一个
            # 非 Timeout 的异常，那个形状构造不出来。
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
                _discard_pipes_and_reap(proc)
            break
    return result


def _build_summary(results, started, total) -> dict:
    """汇总运行结果（文件 / 用例统计 + 状态判定）"""
    finished = datetime.now()
    passed_files = sum(1 for r in results if r["ok"])
    timed_out_files = sum(1 for r in results if r["timed_out"])
    failed_files = sum(1 for r in results if not r["ok"] and not r["timed_out"])
    total_cases = sum(r["cases"] for r in results)
    passed_cases = sum(r["cases"] for r in results if r["ok"])
    if not results or total == 0:
        status = "failed"
    elif failed_files == 0 and timed_out_files == 0:
        status = "success"
    elif passed_files == 0:
        status = "failed"
    else:
        status = "partial"
    duration = (finished - started).total_seconds()
    status_text = {
        "success": "全部通过",
        "partial": "部分失败",
        "failed": "失败",
        "running": "运行中",
    }.get(status, status)
    summary_text = (f"{status_text}：文件 {passed_files} 通过 / "
                    f"{failed_files} 失败 / {timed_out_files} 超时"
                    f"（共 {total} 个），用例 [OK] 通过 {passed_cases} / "
                    f"{total_cases}，耗时 {duration:.1f}s")
    return {
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "total_files": total,
        "passed_files": passed_files,
        "failed_files": failed_files,
        "timed_out_files": timed_out_files,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "duration_seconds": round(duration, 2),
        "results": results,
        "summary_text": summary_text,
    }


def _insert_running_run(total_files, db_path=None) -> int:
    """写入一条 running 状态的运行记录，返回 run_id"""
    conn = _get_conn(db_path)
    try:
        conn.execute(_TEST_RUNS_DDL)
        cur = conn.execute(
            "INSERT INTO test_runs (started_at, status, total_files) "
            "VALUES (?, 'running', ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(total_files)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _finish_run(run_id, summary, db_path=None):
    """运行结束后更新 test_runs 行（状态 / 统计 / 明细）"""
    conn = _get_conn(db_path)
    try:
        conn.execute(_TEST_RUNS_DDL)
        conn.execute(
            "UPDATE test_runs SET finished_at=?, status=?, "
            "passed_files=?, failed_files=?, timed_out_files=?, "
            "total_cases=?, passed_cases=?, duration_seconds=?, "
            "details=?, summary=? WHERE id=?",
            (summary["finished_at"], summary["status"],
             summary["passed_files"], summary["failed_files"],
             summary["timed_out_files"], summary["total_cases"],
             summary["passed_cases"], summary["duration_seconds"],
             json.dumps(summary["results"], ensure_ascii=False),
             summary["summary_text"], int(run_id)),
        )
        conn.commit()
    except Exception as e:
        print(f"[TestRunner] 更新 test_runs 失败（忽略）: {e}")
    finally:
        conn.close()


def run_all_tests(tests_dir=None, timeout_sec=DEFAULT_TIMEOUT_SEC,
                  retries=DEFAULT_RETRIES, max_files=None, db_path=None,
                  progress_cb=None, only=None) -> dict:
    """运行全部测试文件，结果写入 test_runs 表，返回汇总 dict

    Args:
        tests_dir: 测试目录（默认项目 tests/；测试可传临时目录）
        timeout_sec: 单文件超时（秒，看门狗）
        retries: 失败重试次数（超时文件不重试）
        max_files: 最多运行的文件数（None=全部；测试/演示用）
        db_path: 显式指定数据库路径（默认项目 data/memory.db；测试传临时库）
        progress_cb: 可选回调 progress_cb(done, total, fname, ok, timed_out)
        only: 只跑这些文件（P4-12 子项 1）。接受文件名列表，元素可带或不带
            `.py`、大小写不敏感。**只作过滤器，不重排**（输出顺序恒为
            discover_test_files 的排序，可复现性依赖它）。None=跑全部。
            任一名字匹配不上即抛 ValueError（且不写 test_runs 行）；
            传了空列表同样抛错——"跑空集"会留下 total_files=0 却看似成功的
            运行记录，污染台账。

    Returns:
        汇总 dict（见 _build_summary）

    Raises:
        RuntimeError: 已有回归正在运行（防重复启动）
        ValueError: only 为空或含匹配不到的名字
    """
    global _running
    with _run_lock:
        if _running:
            raise RuntimeError("已有回归正在运行，请等待完成后再试")
        _running = True
    try:
        files = discover_test_files(tests_dir)
        # 先过滤再取 max_files：--files 与 --max-files 组合时以目标集合为准
        if only is not None:
            files = _filter_by_names(files, only)
        if max_files is not None:
            files = files[: int(max_files)]
        started = datetime.now()
        total = len(files)
        results = []
        run_id = _insert_running_run(total, db_path=db_path)
        for i, f in enumerate(files, 1):
            try:
                r = _run_one_file(f, timeout_sec=timeout_sec, retries=retries)
            except Exception as e:
                r = {"file": Path(f).name, "ok": False, "timed_out": False,
                     "retried": False, "cases": 0, "seconds": 0.0,
                     "tail": f"[执行异常] {e}"}
            results.append(r)
            if progress_cb:
                try:
                    progress_cb(i, total, r["file"], r["ok"], r["timed_out"])
                except Exception:
                    pass
        summary = _build_summary(results, started, total)
        _finish_run(run_id, summary, db_path=db_path)
        # 只在"真·全量回归"时更新逐文件文档。除临时目录/临时库（测试驱动运行器）
        # 外，还要排除 --files / --max-files 的**子集运行**（P4-12 子项 1）：
        # 否则一次 7 文件的定向跑会把 tests/TEST_RESULTS.md 这份权威全量记录
        # 覆盖成子集结果。这个坑加 --files 之前就能用 --max-files 踩到。
        if (tests_dir is None and db_path is None
                and only is None and max_files is None):
            try:
                update_test_results_file(summary, run_id=run_id)
            except Exception as e:
                print(f"[TestRunner] 更新结果文档失败（忽略）: {e}")
        return summary
    finally:
        with _run_lock:
            _running = False


def list_recent_runs(limit=10, db_path=None) -> list:
    """读取最近 N 次回归运行记录（最新在前）"""
    try:
        conn = _get_conn(db_path)
        try:
            conn.execute(_TEST_RUNS_DDL)
            rows = conn.execute(
                "SELECT * FROM test_runs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        print(f"[TestRunner] 读取最近运行记录失败（忽略）: {e}")
        return []


def render_runs_markdown(runs) -> str:
    """把运行记录渲染为 Markdown（Gradio UI 展示用）

    展示最近 N 次运行表格 + 最近一次已结束运行的失败/超时明细。
    """
    if not runs:
        return ("暂无回归记录（点击「▶️ 运行全部测试」开始第一次回归；"
                "运行在后台线程，完成后点「🔄 刷新结果」查看）")
    status_icon = {
        "success": "✅ 全部通过",
        "partial": "⚠️ 部分失败",
        "failed": "❌ 失败",
        "running": "🔄 运行中",
    }
    lines = [
        "| # | 开始时间 | 状态 | 文件(通过/失败/超时) | 用例(通过/总数) | 耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        icon = status_icon.get(r.get("status"), r.get("status", ""))
        lines.append(
            f"| {r['id']} | {(r.get('started_at') or '')[:19]} | {icon} | "
            f"{r.get('passed_files', 0)}/{r.get('failed_files', 0)}/"
            f"{r.get('timed_out_files', 0)} | "
            f"{r.get('passed_cases', 0)}/{r.get('total_cases', 0)} | "
            f"{r.get('duration_seconds', 0):.1f}s |"
        )
    # 最近一次已结束运行的失败/超时明细
    done_runs = [r for r in runs if r.get("status") != "running"]
    if done_runs:
        latest = done_runs[0]
        if (latest.get("failed_files") or 0) + (latest.get("timed_out_files") or 0) > 0:
            try:
                details = json.loads(latest.get("details") or "[]")
                fails = [d for d in details if not d.get("ok")]
                if fails:
                    lines.append("")
                    lines.append(f"#### ❌ 最近一次失败明细（运行 #{latest['id']}）")
                    for d in fails:
                        tag = "⏱️ 超时" if d.get("timed_out") else "❌ 失败"
                        tail = (d.get("tail") or "").replace("\n", " ")[:160]
                        lines.append(
                            f"- **{d.get('file', '?')}** {tag}"
                            f"（用例 {d.get('cases', 0)}，{d.get('seconds', 0):.1f}s）"
                            f"`{tail}`")
            except Exception:
                pass
    return "\n".join(lines)


def render_test_results_markdown(summary, run_id=None) -> str:
    """渲染 tests/TEST_RESULTS.md 的生成区块（P4-4）

    设计说明（刻意**不含** tail）：tail 是子进程的控制台输出，含耗时、插件加载顺序、
    临时路径等每次都变的内容——渲染进来会让文档 diff 永不稳定，而"文档 diff 稳定"
    正是自动生成的意义所在。需要 tail 调试时请查 test_runs 表或 UI「🧪 测试与资源监控」。
    """
    status_text = {
        "success": "✅ 全部通过",
        "partial": "⚠️ 部分失败",
        "failed": "❌ 失败",
        "running": "🔄 运行中",
    }.get(summary.get("status"), summary.get("status", ""))
    total_cases = summary.get("total_cases", 0)
    lines = [
        "## 最新一轮全量回归（自动生成，请勿手改）",
        "",
        f"- **执行时间**：{summary.get('started_at', '?')} → "
        f"{summary.get('finished_at', '?')}"
        f"（总耗时 {summary.get('duration_seconds', 0):.1f} 秒）",
        f"- **运行编号**：`test_runs` #{run_id if run_id is not None else '未记录'}",
        f"- **结果**：{summary.get('total_files', 0)} 个测试文件 / "
        f"**{total_cases} 个用例（[OK] {summary.get('passed_cases', 0)}/{total_cases}）**，"
        f"{status_text}；失败 {summary.get('failed_files', 0)}，"
        f"超时 {summary.get('timed_out_files', 0)}",
        "",
        "| 测试文件 | 用例 | 耗时 | 结果 |",
        "|---|---:|---:|---|",
    ]
    for r in summary.get("results", []) or []:
        if r.get("ok"):
            mark = "✅"
            if r.get("retried"):
                mark += "（重试后通过）"
        elif r.get("timed_out"):
            mark = "⏱️ 超时"
        else:
            mark = "❌"
        lines.append(
            f"| `{r.get('file', '?')}` | {r.get('cases', 0)} | "
            f"{r.get('seconds', 0.0):.1f}s | {mark} |"
        )
    lines.append(
        f"| **合计** | **{total_cases}** | "
        f"**{summary.get('duration_seconds', 0):.1f}s** | {status_text} |"
    )
    return "\n".join(lines)


def update_test_results_file(summary, run_id=None, path=None) -> bool:
    """把生成区块写回 tests/TEST_RESULTS.md（原子替换；标记缺失则跳过）

    只替换 GENERATED_BEGIN / GENERATED_END 之间的内容，标记外的说明文字一律不动
    （分区渲染：拿到自动化又不丢历史）。写入用 tmp + os.replace，避免中途异常把
    被文档指向的权威记录写坏。返回是否实际写入。
    """
    target = Path(path) if path is not None else TEST_RESULTS_FILE
    if not target.exists():
        print(f"[TestRunner] 未找到 {target.name}，跳过结果文档更新")
        return False
    text = target.read_text(encoding="utf-8")
    begin = text.find(GENERATED_BEGIN)
    end = text.find(GENERATED_END)
    if begin == -1 or end == -1 or end < begin:
        print(f"[TestRunner] {target.name} 缺少生成标记，跳过结果文档更新")
        return False
    body = render_test_results_markdown(summary, run_id=run_id)
    new_text = f"{text[:begin + len(GENERATED_BEGIN)]}\n{body}\n{text[end:]}"
    tmp = target.with_name(target.name + ".tmp")
    # newline="\n"：避免 Windows 上把 \n 翻成 \r\n 让工作区在 diff 之外变脏
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    print(f"[TestRunner] 已更新 {target.name}（生成区块 / 运行 #{run_id}）")
    return True


def main(argv=None) -> int:
    """CLI 入口：python core/test_runner.py [--timeout N] [--retries N]
    [--max-files N] [--files a,b,c] [--db PATH] [--list]"""
    # GBK 等旧编码控制台（输出被重定向为管道时 sys.stdout 为 ANSI 代码页）
    # 兜底：把 stdout/stderr 编码错误策略降级为 replace，无法编码字符以
    # 替换符输出而非抛 UnicodeEncodeError，保证汇总/明细打印完成、退出码
    # 正常返回；可读性由 _console_safe 在打印前完成 emoji→ASCII 替换。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass
    import argparse
    parser = argparse.ArgumentParser(description="辅助测试运行器 v0.1")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC,
                        help="单文件超时秒数（看门狗，默认 360；"
                             "不要逐批临时调大，见 AGENTS.md 执行纪律第 2 条）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="失败重试次数（默认 1；超时文件不重试）")
    parser.add_argument("--max-files", type=int, default=None,
                        help="最多运行文件数（调试用）")
    parser.add_argument("--files", default=None,
                        help="只跑指定测试文件，逗号分隔，可带或不带 .py"
                             "（如 --files test_eligibility_filter,test_contracts）；"
                             "名字匹配不上即报错，不会跑空集")
    parser.add_argument("--db", default=None,
                        help="SQLite 数据库路径（默认项目 data/memory.db）")
    parser.add_argument("--list", action="store_true",
                        help="仅列出发现的测试文件")
    args = parser.parse_args(argv)

    files = discover_test_files()
    if args.list:
        for f in files:
            print(f.name)
        return 0

    only = None
    if args.files is not None:
        only = [x for x in (s.strip() for s in args.files.split(",")) if x]

    print(f"[TestRunner] 发现 {len(files)} 个测试文件，开始运行…")
    if only:
        print(f"[TestRunner] 定向执行（--files）：{', '.join(only)}")
    try:
        summary = run_all_tests(
            timeout_sec=args.timeout, retries=args.retries,
            max_files=args.max_files, db_path=args.db, only=only,
            progress_cb=lambda done, total, fname, ok, timed_out: print(
                _console_safe(f"  [{done}/{total}] "
                              f"{'✅' if ok else ('⏱️' if timed_out else '❌')} "
                              f"{fname}"),
                flush=True),
        )
    except ValueError as e:
        # --files 名字匹配不上 / 空列表：直接失败，不写 test_runs 行
        print(_console_safe(f"[TestRunner] ❌ {e}"))
        return 2
    print()
    print("[TestRunner] ===== 回归汇总 =====")
    print(f"  状态：{summary['status']}（{summary['started_at']} → "
          f"{summary['finished_at']}）")
    print(f"  文件：通过 {summary['passed_files']} / "
          f"失败 {summary['failed_files']} / 超时 {summary['timed_out_files']}"
          f" / 共 {summary['total_files']}")
    print(f"  用例：[OK] 通过 {summary['passed_cases']} / "
          f"总数 {summary['total_cases']}")
    print(f"  耗时：{summary['duration_seconds']:.1f} 秒")
    print()
    for r in summary["results"]:
        tag = "✅" if r["ok"] else ("⏱️" if r["timed_out"] else "❌")
        retry_tag = "（重试后通过）" if r.get("retried") and r["ok"] else ""
        print(_console_safe(
            f"  {tag} {r['file']}  用例 {r['cases']}  "
            f"{r['seconds']:.1f}s{retry_tag}"))
    return 0 if summary["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
