#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机补算的命令行入口（scripts/boot_compensate.py，P7 批次 14，2026-10-06）

用途：
    不开界面也能把"这一期欠下的模板"补算一次，并把这次跑**落进行运行记录**。
    它是报件 v3 验收 V4 里今天唯一没演示过的那一格的入口：同一批次连跑两次，
    第二次必须显示"到期 0 件（这一期已经算过）"，且两次的痕迹都能按 C8 读出来。

为什么需要这个文件（而不是"开机那次已经够了"）：
    补算今天有两个生产入口——`app.py` 的启动钩子和界面里那个 60 秒 `gr.Timer`，
    它们都长在 Gradio 进程里。V4 要的是"存在一个不依赖界面的入口"，将来接云端时
    换的只是触发源，补算逻辑一行不动。所以本文件**只做两件事**：把
    `TaskExecutor.check_scheduled_templates(trigger_source="cli")` 从命令行调起来，
    再把库里已经存在的痕迹读出来给人看。到期判定仍然只有 `core.task_template._is_due`
    一处——本文件不重写、不复制它（这是报件里"不许另造一套"的那条）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\boot_compensate.py

    连跑两次即可验证 V4：第一次正常补算，第二次的运行行 due_count=0。

输出三段：
    1) 距上次开机：读最近一行 trigger_source='boot' 的 started_at（V3 那一问的
       分母，**只读**，本入口自己不写 boot 行——它写的是 cli 行）；
    2) 本次逐模板的执行结果（没有模板进入执行时如实说明，不静默）；
    3) 本次写入的运行行 ＋ 最近几行运行记录（含"连到哪个库"，C8）。

边界（与报件的硬约束对齐）：
    - 不 import ui：`ui/controllers/_shared.py` 在**导入期**就调 `init_db()`，
      命令行不该背上这条副作用，也不该为此加载 Gradio。建表与迁移由本文件
      在打印完目标库路径之后**显式**调 `init_db()`；
    - 不改 `task_templates.auto_approve`（C5：整个试验期间保持 0）。因此本入口
      跑出来的产物一律停在待审批队列里，等用户在界面逐条决定（C7 一条＝一个决定）；
      运行行里的 `produced_rows` 量的就是"待处理产出数"，**不代表已落盘**；
    - 不引入常驻进程／定时循环（C1）：跑一次退一次；
    - 日志只落标识与计数，不落内容全文、不落密钥（C10）。

退出码：
    0 —— 跑完并读到了本次写入的运行行，且无失败项；
    1 —— 本次运行有失败项（运行行 status 为 partial／error），或没能读回本次运行行；
    2 —— 入口自身失败（导入／建库／迁移异常），fail-closed，不静默放行。
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

# 项目根目录（本文件位于 scripts/ 下）：命令行入口直接 python 运行时，
# sys.path[0] 是 scripts/，不加根目录就 import 不到业务模块
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.task_executor import TaskExecutor, log_safe  # noqa: E402
from core import task_template as template_module  # noqa: E402
from memory import database  # noqa: E402


def _utc_now_text() -> str:
    """本次调用的开始时刻（UTC 定宽字符串）。

    与 `check_scheduled_templates` 里同一个写法（`datetime.now(timezone.utc)
    .isoformat(timespec="seconds")`），目的是能按字典序比较：定宽 ＋ 固定偏移，
    字符串序就是时间序。用它核"读回来的运行行是不是本次这一条"，比再查一次
    MAX(id) 便宜，也不需要新函数。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_gap(earlier: str) -> str:
    """把一条 UTC 字符串换算成"距今多久"；解析不了就原样说解析不了（不猜）"""
    if not earlier:
        return "（无 started_at，算不出间隔）"
    try:
        parsed = datetime.fromisoformat(str(earlier))
    except ValueError:
        return f"（{earlier} 不是可解析的时间串，算不出间隔）"
    if parsed.tzinfo is None:
        # 定宽但没带偏移：按 C9 的口径这类值只能是 UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return f"（{earlier} 在未来，相差 {-total_seconds} 秒——时钟不一致，如实报）"
    days, rest = divmod(total_seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days} 天 {hours} 小时 {minutes} 分钟前"


def _print_last_boot() -> None:
    """打印最近一行 boot 运行记录（V3："这次开机距上次多久"），只读不写"""
    prev = template_module.latest_template_run(template_module.TRIGGER_SOURCE_BOOT)
    if prev is None:
        print("[BootCompensate] 最近开机留痕：库里还没有 boot 行——"
              "要么今天还没打开过界面，要么那条 boot 行没写进去。"
              "『距上次开机多久』此刻没有分母，不拿 0 冒充。")
        return
    print(f"[BootCompensate] 最近开机留痕：#{prev['id']} 起 "
          f"{prev['started_at']}（UTC）＝{_format_gap(prev['started_at'])}；"
          f"那次 到期={prev['due_count']} 执行={prev['ran']} "
          f"暂停待审批={prev['paused']} 堆积跳过={prev['backlog_skip']} "
          f"抢占失败={prev['claim_skip']} 失败={prev['failed']} "
          f"产出待审批行={prev['produced_rows']}")


def _print_run_row(row) -> None:
    """打印一行运行记录的全部计数字段（C8 的字段一个都不省）"""
    print(f"[BootCompensate] 运行记录 #{row['id']}：status={row['status']} "
          f"到期={row['due_count']} 执行完={row['ran']} 停在审批点={row['paused']} "
          f"堆积跳过={row['backlog_skip']} 抢占失败={row['claim_skip']} "
          f"失败={row['failed']} 产出待审批行={row['produced_rows']} "
          f"预算用尽={row['budget_exhausted']}")
    print(f"[BootCompensate]   触发源={row['trigger_source']}  "
          f"{row['started_at']} → {row['finished_at']}（UTC，C9）")
    print(f"[BootCompensate]   连到的库={row['db_path']}  "
          f"大小={row['db_bytes']} 字节  该库 pending 总数={row['db_pending_total']}")
    note = row.get("note") or ""
    if note:
        print(f"[BootCompensate]   备注：{log_safe(note)}")


def _print_recent_runs(limit: int = 5) -> None:
    """倒序列出最近几行运行记录：V4 连跑两次时，第二次的 due=0 就在这一屏里"""
    rows = template_module.list_template_runs(limit=limit)
    print(f"[BootCompensate] 最近 {len(rows)} 行运行记录（新→旧）：")
    for item in rows:
        print(f"  #{item['id']} {item['trigger_source']} {item['started_at']} "
              f"status={item['status']} due={item['due_count']} ran={item['ran']} "
              f"paused={item['paused']} backlog={item['backlog_skip']} "
              f"claim={item['claim_skip']} fail={item['failed']} "
              f"rows={item['produced_rows']} db={item['db_path']}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # 先报库再动手：本席 09-27 自己接过一次错库（不带"副本"的那个目录是陈旧库），
    # C8 把"连到了哪个库"写进每一行记录，就是为了让人**在写之前**也能看到这件事
    print(f"[BootCompensate] 目标库：{Path(database.DB_PATH).resolve()}")
    try:
        database.init_db()
    except Exception as e:
        print(f"[BootCompensate] 建库／迁移失败，终止：{e}")
        return 2

    _print_last_boot()

    started = _utc_now_text()
    try:
        results = TaskExecutor().check_scheduled_templates(
            trigger_source=template_module.TRIGGER_SOURCE_CLI)
    except Exception as e:
        # 执行器内部对各分支都有兜底打印，能抛到这里的是"整次跑没起来"级别的异常。
        # 这里不吞：非零退出码就是"这次跑没成功"，静默返回 0 是本件最不该有的形态
        print(f"[BootCompensate] 补算调用失败：{e}")
        return 2

    print(f"[BootCompensate] 本次开始（UTC）：{started}")
    if not results:
        print("[BootCompensate] 本次没有模板进入执行——去向计数见下面的运行行。"
              "若那里的 到期＝0，说明这一期已经算过（同一批次连跑第二次就长这样）。")
    else:
        print(f"[BootCompensate] 本次进入执行的模板 {len(results)} 个：")
        for item in results:
            tag = "PAUSED" if item.get("paused") else ("OK" if item.get("ok")
                                                       else "FAIL")
            print(f"  [{tag}] {item.get('template_name')}："
                  f"{log_safe(item.get('message'))}")

    # 读回本次写入的运行行：cli 路径每次必记一行（批次 14 定案第 2 条），
    # 所以"读不到"或"读到的比本次早"都是记录没落成功的实况，要出声
    row = template_module.latest_template_run(template_module.TRIGGER_SOURCE_CLI)
    ok = True
    if row is None:
        print("[BootCompensate] 没能读回本次的运行记录行（写行失败或表不可读）——"
              "这次跑在库里没有痕迹，按失败处理。")
        ok = False
    else:
        if str(row.get("started_at") or "") < started:
            print("[BootCompensate] 读到的 cli 运行行早于本次调用——本次没有写入新行，"
                  "按失败处理。")
            ok = False
        _print_run_row(row)
        if row.get("status") in ("partial", "error") or (row.get("failed") or 0) > 0:
            ok = False
    _print_recent_runs()

    print(f"[BootCompensate] 退出码：{0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
