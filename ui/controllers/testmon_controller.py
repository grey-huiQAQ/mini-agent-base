# -*- coding: utf-8 -*-
"""ui.controllers.testmon_controller —— 测试与资源监控

（Phase 3b，2026-09-12：回调函数体自 app.py 逐字迁入，见 commit 说明）
本模块只依赖真实定义模块与 ui.controllers._shared，不导入 app，避免循环。
"""

from memory.resource_log import (
    get_resource_stats,
    get_recent_resource_logs,
)
# P4-2：运行器自 tests/ 迁回 core/，切断"生产代码 import 测试包"的反向依赖
from core.test_runner import (
    run_all_tests,
    list_recent_runs,
    render_runs_markdown,
    is_test_run_running,
)
import threading




def do_start_test_run():
    """后台启动全量回归（真实运行放入后台线程，UI 立即返回不阻塞）

    Returns:
        状态 Markdown（提示已启动 / 已有回归运行中）
    """
    if is_test_run_running():
        return "⚠️ 已有回归正在运行，请等待完成后再启动（或点击「🔄 刷新结果」查看进度）"

    def _runner():
        try:
            summary = run_all_tests()
            print(f"[TestRunner] 全量回归完成：{summary.get('summary_text')}")
        except Exception as e:
            print(f"[TestRunner] 全量回归异常（忽略）: {e}")

    threading.Thread(target=_runner, daemon=True, name="test-runner").start()
    return ("▶️ 已启动全量回归（后台运行中，不会阻塞界面；"
            "完成后点击「🔄 刷新结果」查看汇总报告）")


def do_refresh_test_runs():
    """刷新最近回归结果展示（运行记录表格 + 最近一次失败明细）"""
    try:
        runs = list_recent_runs(limit=10)
        return render_runs_markdown(runs)
    except Exception as e:
        return f"⚠️ 读取回归记录失败：{e}"


def do_refresh_resource_stats():
    """刷新资源消耗统计展示（最近 7 天按类型汇总 + 每日明细 + 最近记录）"""
    try:
        stats = get_resource_stats(days=7)
        recent = get_recent_resource_logs(limit=20)
        lines = ["### 📊 最近 7 天资源消耗（粗粒度估算，非精确计费）", ""]
        if not stats["by_type"]:
            lines.append("（暂无资源日志 —— 进行对话 / 搜索后会自动记录）")
        else:
            lines.append(f"> 7 天估算总 token：**{stats['total_tokens']:,}**\n")
            lines.append("| 类型 | 调用次数 | 估算 token |")
            lines.append("|---|---|---|")
            for item in stats["by_type"]:
                lines.append(f"| {item['type']} | {item['calls']} | "
                             f"{item['tokens']:,} |")
            lines.append("")
            lines.append("#### 📅 按日明细")
            lines.append("| 日期 | 类型 | 次数 | token |")
            lines.append("|---|---|---|---|")
            for d in stats["daily"]:
                lines.append(f"| {d['day']} | {d['type']} | {d['calls']} | "
                             f"{d['tokens']:,} |")
        lines.append("")
        lines.append("#### 🕘 最近记录")
        if not recent:
            lines.append("（暂无记录）")
        else:
            for r in recent:
                lines.append(
                    f"- `{(r.get('created_at') or '')[:19]}` "
                    f"[{r.get('resource_type')}] {r.get('operation')} "
                    f"约 {r.get('token_estimate', 0)} token（{r.get('detail')}）")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 资源统计失败：{e}"
