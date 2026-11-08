# -*- coding: utf-8 -*-
"""
辅助测试运行器（core/test_runner.py，P4-2 自 tests/ 迁回 core/ 以切断生产→测试反向依赖）测试（离线：临时目录假测试文件 + 临时数据库）
覆盖：
- 文件发现：只匹配 test_*.py，按文件名排序
- 单个执行：通过文件（打印 [OK] xN + ALL TESTS PASSED）→ ok=True、用例数正确
- 失败重试：首次失败二次通过 → ok=True 且 retried=True
- 最终失败：重试后仍失败 → ok=False，已执行用例数被记录
- 超时看门狗：睡眠无限的文件 → timed_out=True 且尽快返回（僵死强制终止）
- 超时行的记账口径（阶段二 组 2，2026-10-18）：seconds 记**实测等待时长**而不是阈值
  常数；kill 之后的管道收集与关管道都带期限（孙进程持有写端那一型也不能把运行器拖住）
- 汇总与落库：run_all_tests 写入 test_runs 表（先 running 后更新），状态/统计正确
- 最近记录查询：list_recent_runs 最新在前
"""
import contextlib
import os
import shutil
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import test_runner


# ---------- 工具：构造临时假测试文件 ----------

@contextlib.contextmanager
def _tmp_dir():
    """创建临时目录（放在项目 data/.tmp_tests 下，自定义命名 + 手动清理）

    说明：
    - 不用 tempfile 临时目录机制，避免受限文件沙箱对系统临时区的写保护；
    - 目录名用 run_<uuid> 自定义命名，避免与沙箱识别的 tmp* 临时目录混淆；
    - 子进程被看门狗 kill 后可能短暂占用目录句柄，清理失败由 ignore_errors
      容忍（残留目录由 test_cleanup 兜底删除）。
    """
    base = (Path(__file__).resolve().parent.parent / "data" / ".tmp_tests")
    base.mkdir(parents=True, exist_ok=True)
    d = base / ("run_" + uuid.uuid4().hex[:10])
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield str(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write_file(dirpath, name, content) -> Path:
    """在临时目录写入一个假测试文件"""
    p = Path(dirpath) / name
    p.write_text(content, encoding="utf-8")
    return p


def _passing_content(n_ok=2) -> str:
    """通过文件：打印 n_ok 个 [OK] + ALL TESTS PASSED（与项目测试文件同约定）"""
    lines = ["print('=== demo test ===')"]
    for i in range(n_ok):
        lines.append(f"print('[OK] case {i}')")
    lines.append("print('\\n=== ALL TESTS PASSED ===')")
    return "\n".join(lines) + "\n"


def _failing_content() -> str:
    """失败文件：打印 1 个 [OK] 后抛异常"""
    return "print('[OK] case 0')\nraise RuntimeError('模拟失败')\n"


def _flaky_content() -> str:
    """首次失败、二次通过：用测试文件同目录的 marker 文件跨进程传递状态"""
    return (
        "from pathlib import Path\n"
        "marker = Path(__file__).with_name('_flaky_marker.txt')\n"
        "if not marker.exists():\n"
        "    marker.write_text('x', encoding='utf-8')\n"
        "    raise RuntimeError('模拟首次失败')\n"
        "print('[OK] case 0')\n"
        "print('\\n=== ALL TESTS PASSED ===')\n"
    )


def _hanging_content() -> str:
    """僵死文件：打印后无限睡眠（测试看门狗超时强制终止）"""
    return "import time\nprint('[OK] start')\ntime.sleep(3600)\nprint('[OK] never')\n"


def _partial_then_hang_content(n_ok=3) -> str:
    """先打印 n_ok 条 [OK]（**带 flush**），再无限睡眠

    P4-12 子项 2 的夹具。**绝大多数测试文件的 print 不带 flush**（实测 tests/ 下
    39 个文件只有 1 个用过，就是本文件）——正因如此 `_run_one_file()` 需要注入
    PYTHONUNBUFFERED（P4-13 子项 ①）。本夹具显式 flush 是为了**单独验证
    "输出可见时运行器留得下"这一半**；"输出可见"那一半由
    `test_timeout_keeps_unflushed_output` 用不带 flush 的夹具（靠
    PYTHONUNBUFFERED）验证。两者并存，缺一不可。
    """
    lines = ["import time"]
    for i in range(n_ok):
        lines.append(f"print('[OK] partial {i}', flush=True)")
    lines.append("time.sleep(3600)")
    return "\n".join(lines) + "\n"


def _unflushed_then_hang_content(n_ok=3) -> str:
    """先打印 n_ok 条 [OK]（**不带 flush**），再无限睡眠

    P4-13 子项 ①：与 `_partial_then_hang_content` 的唯一差别就是没有 flush。
    它验证的是"输出可见"这一半——即 PYTHONUNBUFFERED 是否真的让行落到了管道。
    没有那行 env 时，该夹具在超时后 cases 会停在 0（缓冲区随进程消失）。
    """
    lines = ["import time"]
    for i in range(n_ok):
        lines.append(f"print('[OK] unflushed {i}')")
    lines.append("time.sleep(3600)")
    return "\n".join(lines) + "\n"


def _grandchild_holds_pipe_content(n_ok=2, hold_sec=30, pid_file=None) -> str:
    """先打印 n_ok 条 [OK]，再拉起一个**继承 stdout 的孙进程**，然后自己挂

    阶段二 组 2 的夹具，复现的是旧写法真正卡住的那一型：`kill()` 只结束子进程自己，
    孙进程仍握着管道写端 ⇒ 读端等不到 EOF。2026-10-18 本机实测两档形状：
    第二次**带期限**的 `communicate()` 在宽限到点时放弃（不无限等），而
    `proc.stdout.close()` 单是调一次就 block 13.50s（要等孙进程睡完）——所以
    「关管道」必须转后台线程，否则修掉一个无限阻塞、又换来另一个。

    `pid_file`：把孙进程 pid 落到文件，用例收尾时自己把它终止掉。不留它睡满
    `hold_sec` 有两个原因——一是别在机器上挂一串 python 进程，二是**解释器退出**
    时那个后台 close 线程还堵在读上（Python 收尾会关掉仍打开的文件对象），
    留着它会让本测试文件的退出阶段被拖长（本仓 §五.63 记过同族形态：
    「断言全部跑完，挂发生在解释器退出阶段」）。终止之后写端关闭 ⇒ 读立刻 EOF。
    """
    lines = ["import subprocess, sys, time"]
    for i in range(n_ok):
        lines.append(f"print('[OK] pipe-held {i}', flush=True)")
    lines.append(
        f"_p = subprocess.Popen([sys.executable, '-c', 'import time; "
        f"time.sleep({hold_sec})'])")
    if pid_file is not None:
        lines.append(f"open(r'{pid_file}', 'w').write(str(_p.pid))")
    lines.append("time.sleep(3600)")
    return "\n".join(lines) + "\n"


# ---------- 测试用例 ----------

def test_discover_only_test_files():
    """文件发现：只匹配 test_*.py 且按文件名排序"""
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content())
        _write_file(d, "test_b.py", _passing_content())
        _write_file(d, "not_a_test.py", "x = 1")
        _write_file(d, "helper.py", "x = 1")
        _write_file(d, "test_c.py", _passing_content())
        files = test_runner.discover_test_files(d)
        names = [f.name for f in files]
        assert names == ["test_a.py", "test_b.py", "test_c.py"], \
            f"应只发现 test_*.py 且排序稳定：{names}"
    print("[OK] discover only test_*.py files")


def test_run_passing_file():
    """通过文件：ok=True 且 [OK] 用例数解析正确"""
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_pass.py", _passing_content(n_ok=3))
        r = test_runner._run_one_file(f, timeout_sec=30, retries=0)
        assert r["ok"] is True, f"通过文件应 ok=True：{r}"
        assert r["cases"] == 3, f"应解析出 3 个用例：{r}"
        assert r["timed_out"] is False and r["retried"] is False
    print("[OK] passing file ok + case count")


def test_run_failing_file_with_retry():
    """失败重试：首次失败二次通过 → ok=True 且 retried=True"""
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_flaky.py", _flaky_content())
        r = test_runner._run_one_file(f, timeout_sec=30, retries=1)
        assert r["ok"] is True, f"重试后应通过：{r}"
        assert r["retried"] is True, "应发生一次重试"
        assert r["cases"] == 1
    print("[OK] flaky file passes after retry")


def test_run_failing_file_final_failure():
    """最终失败：重试后仍失败 → ok=False，已执行用例数被记录"""
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_bad.py", _failing_content())
        r = test_runner._run_one_file(f, timeout_sec=30, retries=1)
        assert r["ok"] is False, f"失败文件应保持失败：{r}"
        assert r["cases"] == 1, "失败文件的已执行用例数应被记录"
    print("[OK] failing file stays failed")


def test_timeout_watchdog():
    """超时看门狗：僵死文件被强制终止，且尽快返回（不无限等待）"""
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_hang.py", _hanging_content())
        t0 = time.time()
        r = test_runner._run_one_file(f, timeout_sec=2, retries=0)
        elapsed = time.time() - t0
        assert r["timed_out"] is True, "超时应被看门狗捕获"
        assert r["ok"] is False
        assert elapsed < 30, f"看门狗应在超时后尽快返回：{elapsed:.1f}s"
    print("[OK] timeout watchdog kills hung process")


def test_run_all_writes_db():
    """run_all_tests：汇总统计正确 + test_runs 表落库 + 最近记录查询"""
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content(n_ok=2))
        _write_file(d, "test_b.py", _failing_content())
        db = str(Path(tmp) / "runs.db")
        summary = test_runner.run_all_tests(
            tests_dir=d, timeout_sec=30, retries=0, db_path=db)
        assert summary["total_files"] == 2
        assert summary["passed_files"] == 1
        assert summary["failed_files"] == 1
        assert summary["timed_out_files"] == 0
        assert summary["status"] == "partial", f"1 过 1 败应为 partial：{summary}"
        assert summary["total_cases"] >= 3, "用例数应包含两个文件的 [OK] 标记"
        assert summary["passed_cases"] >= 2

        # 落库校验：最近一条记录状态/统计与汇总一致
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT * FROM test_runs ORDER BY id DESC LIMIT 1").fetchone()
            assert row is not None, "应写入 test_runs 表"
            assert row[1] is not None and row[2] is not None, "应有开始/结束时间"
            assert row[3] == "partial", f"状态应为 partial：{row[3]}"
            assert row[4] == 2 and row[5] == 1 and row[6] == 1, "文件统计应正确"
        finally:
            conn.close()

        runs = test_runner.list_recent_runs(limit=5, db_path=db)
        assert len(runs) == 1
        assert runs[0]["status"] == "partial"
    print("[OK] run_all writes test_runs + recent query")


def test_run_row_transitions_running_to_finished():
    """落库流程：_insert_running_run 写 running 行，_finish_run 更新为终态"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "runs.db")
        run_id = test_runner._insert_running_run(2, db_path=db)
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT status FROM test_runs WHERE id = ?", (run_id,)).fetchone()
            assert row[0] == "running", "初始应为 running"
        finally:
            conn.close()
        fake_summary = {
            "finished_at": "2026-01-01 10:00:01", "status": "success",
            "passed_files": 2, "failed_files": 0, "timed_out_files": 0,
            "total_cases": 4, "passed_cases": 4, "duration_seconds": 1.0,
            "results": [], "summary_text": "全部通过",
        }
        test_runner._finish_run(run_id, fake_summary, db_path=db)
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT status, passed_files FROM test_runs WHERE id = ?",
                (run_id,)).fetchone()
            assert row[0] == "success" and row[1] == 2, \
                f"结束后应更新为 success：{row[0]}"
        finally:
            conn.close()
    print("[OK] run row transitions running -> finished")


def test_run_all_leaves_no_running_row():
    """run_all_tests 结束后不留 running 行（同一条记录更新为终态）"""
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content(n_ok=1))
        db = str(Path(tmp) / "runs.db")
        summary = test_runner.run_all_tests(
            tests_dir=d, timeout_sec=30, retries=0, db_path=db)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT status FROM test_runs").fetchall()
            assert len(rows) == 1, f"应只有 1 条运行记录：{rows}"
            assert rows[0][0] == "success", f"应更新为 success：{rows[0][0]}"
            n_running = conn.execute(
                "SELECT COUNT(*) FROM test_runs WHERE status='running'"
            ).fetchone()[0]
            assert n_running == 0, "不应残留 running 行"
        finally:
            conn.close()
        assert summary["status"] == "success"
    print("[OK] run_all leaves no running row")


def test_render_runs_markdown():
    """Markdown 渲染：空列表 / 有记录（含失败明细）均不抛异常"""
    md_empty = test_runner.render_runs_markdown([])
    assert "暂无回归记录" in md_empty
    fake_runs = [{
        "id": 1, "started_at": "2026-01-01 10:00:00",
        "status": "partial", "passed_files": 1, "failed_files": 1,
        "timed_out_files": 0, "passed_cases": 2, "total_cases": 3,
        "duration_seconds": 1.5, "details": "[{\"file\": \"test_b.py\", "
        "\"ok\": false, \"timed_out\": false, \"cases\": 1, \"seconds\": 0.3, "
        "\"tail\": \"Traceback\"}]",
    }]
    md = test_runner.render_runs_markdown(fake_runs)
    assert "partial" in md or "部分失败" in md
    assert "test_b.py" in md, "应展示失败明细"
    print("[OK] render_runs_markdown works")


def test_render_test_results_markdown():
    """逐文件区块渲染（P4-4）：含表格与合计行，且**不含** tail"""
    summary = {
        "started_at": "2026-09-12 13:22:32", "finished_at": "2026-09-12 13:33:57",
        "status": "success", "total_files": 2, "passed_files": 2,
        "failed_files": 0, "timed_out_files": 0,
        "total_cases": 5, "passed_cases": 5, "duration_seconds": 12.3,
        "results": [
            {"file": "test_a.py", "ok": True, "timed_out": False,
             "retried": False, "cases": 3, "seconds": 1.5, "tail": "不该出现的 tail 文本"},
            {"file": "test_b.py", "ok": False, "timed_out": True,
             "retried": False, "cases": 2, "seconds": 10.8, "tail": "另一段 tail"},
        ],
    }
    md = test_runner.render_test_results_markdown(summary, run_id=42)
    assert "test_a.py" in md and "test_b.py" in md
    assert "⏱️ 超时" in md, "超时文件应有独立标记"
    assert "| **合计** |" in md
    assert "#42" in md
    # 关键约束：tail 不得进入文档（否则文档 diff 永不稳定）
    assert "不该出现的 tail 文本" not in md and "另一段 tail" not in md, \
        "生成区块不得包含子进程 tail"
    print("[OK] render_test_results_markdown works")


def test_update_test_results_file_partitioned():
    """分区渲染（P4-4）：只替换标记之间内容，标记外文字不动；缺标记时不写"""
    with _tmp_dir() as tmp:
        target = Path(tmp) / "TEST_RESULTS.md"
        target.write_text(
            "# 标题\n\n手写前言（不得被改）\n\n"
            f"{test_runner.GENERATED_BEGIN}\n旧内容\n{test_runner.GENERATED_END}\n\n"
            "手写后记（不得被改）\n",
            encoding="utf-8",
        )
        summary = {
            "started_at": "a", "finished_at": "b", "status": "success",
            "total_files": 1, "passed_files": 1, "failed_files": 0,
            "timed_out_files": 0, "total_cases": 1, "passed_cases": 1,
            "duration_seconds": 1.0,
            "results": [{"file": "test_a.py", "ok": True, "timed_out": False,
                         "retried": False, "cases": 1, "seconds": 1.0, "tail": ""}],
        }
        assert test_runner.update_test_results_file(summary, run_id=7, path=target) is True
        text = target.read_text(encoding="utf-8")
        assert "手写前言（不得被改）" in text and "手写后记（不得被改）" in text, \
            "标记外的文字必须原样保留"
        assert "旧内容" not in text and "test_a.py" in text
        assert not target.with_name(target.name + ".tmp").exists(), "临时文件应已原子替换掉"

        # 标记缺失 → 不写、返回 False（避免把一份没有标记的文档写坏）
        no_marker = Path(tmp) / "no_marker.md"
        no_marker.write_text("# 没有标记的文档\n", encoding="utf-8")
        assert test_runner.update_test_results_file(summary, run_id=8, path=no_marker) is False
        assert no_marker.read_text(encoding="utf-8") == "# 没有标记的文档\n"
    print("[OK] update_test_results_file partitioned + marker guard")


def test_timeout_records_partial_cases():
    """P4-12 子项 2：超时分支保留被杀前已打印的 [OK] 条数（不再是恒 0）

    动机：过去 40 轮里 15 次挂，cases 全是 0，"它停在第几条 [OK]"没有数据能回答。
    """
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_partial_hang.py",
                        _partial_then_hang_content(n_ok=3))
        r = test_runner._run_one_file(f, timeout_sec=3, retries=0)
        assert r["timed_out"] is True, f"应被看门狗判超时：{r}"
        assert r["ok"] is False
        assert r["cases"] == 3, \
            f"应记录被杀前已打印的 3 条 [OK]，实际 {r['cases']}：{r['tail'][:300]}"
        # tail 保留原有超时文案，并在其后附上部分输出
        assert "[超时]" in r["tail"], r["tail"][:200]
        assert "已打印 [OK] 3 条" in r["tail"], r["tail"][:300]
        assert "[OK] partial 2" in r["tail"], "应附上部分输出的末几行"
    print("[OK] timeout keeps partial case count")


def test_timeout_keeps_unflushed_output():
    """P4-13 子项 ①：**不带 flush** 的输出也要能落盘（靠 PYTHONUNBUFFERED）

    这是 P4-12 子项 2 真正生效的前提。子进程 stdout 重定向到管道时是块缓冲，
    不注入 PYTHONUNBUFFERED 的话 print() 还留在子进程用户态缓冲区里，
    kill() 一到就随进程消失 —— 那时 cases 会停在 0，tail 里也没有任何 [OK] 行。
    """
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_unflushed_hang.py",
                        _unflushed_then_hang_content(n_ok=3))
        r = test_runner._run_one_file(f, timeout_sec=3, retries=0)
        assert r["timed_out"] is True, f"应被看门狗判超时：{r}"
        assert r["cases"] == 3, (
            f"不带 flush 的输出应靠 PYTHONUNBUFFERED 落到管道；"
            f"实际 cases={r['cases']}（0 说明缓冲区丢了）。tail={r['tail'][:300]!r}")
        # 契约：tail 里必须能看到**最后一条 [OK] 的完整文字**（不只是条数）
        assert "[OK] unflushed 2" in r["tail"], \
            f"tail 应含最后一条 [OK] 的完整文字：{r['tail']!r}"
    print("[OK] timeout keeps unflushed output")


def test_timeout_row_records_measured_wait():
    """超时那一格记**实测等待时长**，不是写死的 `float(timeout_sec)`（组 2 判据本体）

    旧实现把 `seconds` 记成阈值常数 ⇒ 用「墙钟 − 逐文件合计」去量真实损耗时，
    含超时的那一轮被系统性低估（实测 run #195 差 5,473.0s，而四条干净轮只差 0.1s）。
    这条用例的牙齿在**严格大于阈值**：常数写法下 `seconds` 与 `timeout_sec` 恒等，
    当场红。上界同时钉住——「改成实测」不能顺手把一个无限阻塞的洞换成一个更大的数。
    最后一条把记账与外侧墙钟对上：记进 `details` 的数就是这一次真实消耗的数。
    """
    with _tmp_dir() as tmp:
        f = _write_file(tmp, "test_measured_hang.py",
                        _partial_then_hang_content(n_ok=2))
        timeout_sec = 2
        t0 = time.monotonic()
        r = test_runner._run_one_file(f, timeout_sec=timeout_sec, retries=0)
        elapsed = time.monotonic() - t0
        assert r["timed_out"] is True, f"应被看门狗判超时：{r}"
        assert r["seconds"] > float(timeout_sec), (
            f"seconds={r['seconds']:.3f} 不大于阈值 {timeout_sec} ⇒ 它还是那个写死的"
            f"常数（实测等待必然严格大于阈值：看门狗只在超过它之后才动手）")
        ceiling = timeout_sec + test_runner.POST_KILL_COLLECT_GRACE_SEC + 10
        assert r["seconds"] <= ceiling, (
            f"seconds={r['seconds']:.1f} 越过「阈值＋收集宽限＋余量」{ceiling:.0f}s "
            f"⇒ 收集没有封顶：{r['tail'][:200]}")
        # 记账与 tail 互相能对上：tail 打印的就是这一格实测值
        assert f"实测等待 {r['seconds']:.1f}s" in r["tail"], r["tail"][:200]
        # 旧的两条超时契约不能被这次改动带崩（cases 仍是被杀前已打印的条数）
        assert r["cases"] == 2, f"cases 应仍记被杀前已打印的 2 条：{r}"
        assert abs(elapsed - r["seconds"]) < 5.0, (
            f"外侧墙钟 {elapsed:.1f}s 与记账 {r['seconds']:.1f}s 不同量级 ⇒ "
            f"这一格量的不是这次等待")
    print("[OK] timeout row records measured wait, not the threshold constant")


def test_timeout_with_descendant_holding_pipe_is_bounded():
    """孙进程握着管道写端时，超时分支**既不能无限等、也不能无限关**（组 2 第二条判据）

    这是 `subprocess.run` 超时分支真正卡住的那一型：kill 只结束子进程，写端还在
    孙进程手里 ⇒ 读端等不到 EOF。2026-10-18 本机实测：旧写法在这条夹具上要等满
    孙进程的睡眠时长（对照探针里 25.54s），而新写法应在「阈值＋收集宽限」内返回。
    上限刻意卡在孙进程睡眠时长**之内**（hold_sec=30 vs 2＋5＋8＝15s）⇒
    把宽限收集改回无限等待，这一条必红；把 `stdout.close()` 放回主流程也必红
    （实测那一次 close 单独 block 13.50s，叠在 collect 的 5s 之上就越过 15s）。

    同时钉住**诚实记账**这一半：宽限内收不到输出 ⇒ `cases` 记 0，且 tail 必须写明
    「没收回管道」，不能让这个 0 冒充「一条 [OK] 都没打印」（口径同 §五.63 那条
    「读 tail 看它停在第几条 [OK]」的判据——它现在多了一种要区分的情况）。
    """
    with _tmp_dir() as tmp:
        pid_file = str(Path(tmp) / "grandchild_pid.txt")
        f = _write_file(tmp, "test_pipe_held_hang.py",
                        _grandchild_holds_pipe_content(n_ok=2, hold_sec=30,
                                                       pid_file=pid_file))
        timeout_sec = 2
        t0 = time.monotonic()
        try:
            r = test_runner._run_one_file(f, timeout_sec=timeout_sec, retries=0)
            elapsed = time.monotonic() - t0
            assert r["timed_out"] is True, f"应被看门狗判超时：{r}"
            assert elapsed < timeout_sec + test_runner.POST_KILL_COLLECT_GRACE_SEC + 8, (
                f"返回耗时 {elapsed:.1f}s ⇒ 收集或关管道仍在等孙进程（它睡 30s）："
                f"{r['tail'][:200]}")
            assert r["seconds"] > float(timeout_sec), (
                f"这一格仍要记实测：{r['seconds']:.3f} 不大于阈值")
            assert r["cases"] == 0, f"宽限内收不到输出就该记 0，不猜：{r}"
            assert "没收回管道" in r["tail"], (
                f"tail 必须写明这个 0 是「没收到」：{r['tail'][:200]}")
        finally:
            # 收尾时自己把孙进程终止掉：别留一串 python 睡 30s，也别让那个后台
            # close 线程在本解释器退出时还堵在读上（写端一关它立刻 EOF）
            if Path(pid_file).exists():
                try:
                    os.kill(int(Path(pid_file).read_text()), signal.SIGTERM)
                except Exception as e:
                    print(f"[note] 孙进程没能终止（忽略，它会自己睡完）：{e}")
    print("[OK] hung descendant holding the pipe cannot block the runner")


def test_files_filter_selects_subset():
    """P4-12 子项 1：only 只跑子集，且只作过滤器不重排"""
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content(n_ok=2))
        _write_file(d, "test_b.py", _passing_content(n_ok=3))
        _write_file(d, "test_c.py", _passing_content(n_ok=4))
        db = str(Path(tmp) / "runs.db")

        # 传 b,a（故意逆序）→ 只跑 2 个，且顺序仍为发现顺序 a,b
        summary = test_runner.run_all_tests(
            tests_dir=d, timeout_sec=30, retries=0, db_path=db,
            only=["test_b", "test_a"])
        assert summary["total_files"] == 2, summary
        assert [r["file"] for r in summary["results"]] == \
            ["test_a.py", "test_b.py"], "only 不得重排（可复现性依赖发现顺序）"
        assert summary["total_cases"] == 5, \
            f"用例数应为 a(2)+b(3)=5：{summary['total_cases']}"

        # 带 .py / 大小写不敏感
        summary2 = test_runner.run_all_tests(
            tests_dir=d, timeout_sec=30, retries=0, db_path=db,
            only=["TEST_C.PY"])
        assert summary2["total_files"] == 1
        assert summary2["total_cases"] == 4
        assert summary2["results"][0]["file"] == "test_c.py"
    print("[OK] --files filter selects subset without reordering")


def test_files_filter_unknown_name_fails_without_db_row():
    """P4-12 子项 1：only 含未知名字 → 抛 ValueError，且不写 test_runs 行

    禁止"匹配不到就跑空集"：那会留下 total_files=0 却看似成功的运行记录。
    """
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content())
        db = str(Path(tmp) / "runs.db")

        for bad in (["test_nope"], ["test_a", "test_nope"], []):
            try:
                test_runner.run_all_tests(
                    tests_dir=d, timeout_sec=30, retries=0, db_path=db,
                    only=bad)
                assert False, f"only={bad!r} 应抛 ValueError"
            except ValueError:
                pass

        # 一次都没落库（连 running 行都没有）
        conn = sqlite3.connect(db)
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='test_runs'").fetchone()
            n = 0 if not exists else conn.execute(
                "SELECT COUNT(*) FROM test_runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0, f"失败路径不得写 test_runs 行，实际 {n} 行"

        # 名字大小写/后缀差异不该被误判为"未知"
        summary = test_runner.run_all_tests(
            tests_dir=d, timeout_sec=30, retries=0, db_path=db,
            only=["Test_A.py"])
        assert summary["total_files"] == 1
    print("[OK] --files unknown name raises without db row")


def test_subset_run_does_not_touch_test_results_doc():
    """P4-12 子项 1：子集运行不得覆盖 tests/TEST_RESULTS.md（权威全量记录）

    做法：把 TEST_RESULTS_FILE 指到临时文件，再**用 --max-files 制造子集**
    （守卫条件里 max_files is None 不成立）。若守卫没写全，写路径会把临时文档
    刷成子集结果，断言即可捕获。用 max-files 而非 --files 是刻意的：它证明
    "这个坑在加 --files 之前就已经能被踩到"，即守卫收紧不只是为新功能。
    """
    with _tmp_dir() as tmp:
        d = Path(tmp)
        _write_file(d, "test_a.py", _passing_content(n_ok=1))
        _write_file(d, "test_b.py", _passing_content(n_ok=1))
        db = str(Path(tmp) / "runs.db")
        fake_doc = Path(tmp) / "TEST_RESULTS.md"
        sentinel = ("# 假文档\n\n"
                    f"{test_runner.GENERATED_BEGIN}\n哨兵内容\n"
                    f"{test_runner.GENERATED_END}\n")
        fake_doc.write_text(sentinel, encoding="utf-8")

        original = test_runner.TEST_RESULTS_FILE
        test_runner.TEST_RESULTS_FILE = fake_doc
        try:
            # tests_dir=None（走默认）+ db_path=None 之外的条件尽量满足，
            # 唯一拦住写的是 max_files is not None
            summary = test_runner.run_all_tests(
                tests_dir=d, timeout_sec=30, retries=0, db_path=db,
                max_files=1)
        finally:
            test_runner.TEST_RESULTS_FILE = original

        assert summary["total_files"] == 1, summary
        assert fake_doc.read_text(encoding="utf-8") == sentinel, \
            "子集运行（max_files=1）不得写 TEST_RESULTS.md"
    print("[OK] subset run leaves test results doc untouched")


def test_cleanup():
    """清理临时目录（子进程被 kill 后可能残留，兜底删除）"""
    base = Path(__file__).resolve().parent.parent / "data" / ".tmp_tests"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== test runner test ===\n")
    test_discover_only_test_files()
    test_run_passing_file()
    test_run_failing_file_with_retry()
    test_run_failing_file_final_failure()
    test_timeout_watchdog()
    test_timeout_records_partial_cases()
    test_timeout_keeps_unflushed_output()
    test_timeout_row_records_measured_wait()
    test_timeout_with_descendant_holding_pipe_is_bounded()
    test_run_all_writes_db()
    test_run_row_transitions_running_to_finished()
    test_run_all_leaves_no_running_row()
    test_render_runs_markdown()
    test_render_test_results_markdown()
    test_update_test_results_file_partitioned()
    test_files_filter_selects_subset()
    test_files_filter_unknown_name_fails_without_db_row()
    test_subset_run_does_not_touch_test_results_doc()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
