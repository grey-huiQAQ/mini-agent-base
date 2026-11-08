"""
📚 私人学习助手（Personal Agent Harness）- Gradio 主界面
自动兼容 Gradio 5.x 和 6.x

版本口径：见 PROJECT_STATUS.md §四 里程碑表（唯一权威）。
本文件头与 UI 标题不再自带版本字面量（P5-2 删除，避免再造一个会说谎的编号）。

功能：Supervisor 自动路由、多角色 System Prompt、Function Call 工具调用、
      目标系统、长期记忆与知识图谱、技能系统、每日计划复盘、
      Prompt 进化、复习推荐、导出、任务执行自动化与目标笔记系统（v3.7.5）
"""

import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"


import json
import re
import socket
import sys
import threading
import gradio as gr
from datetime import datetime
from pathlib import Path

from prompts import get_prompt, get_all_roles
from prompts.role_prompts import get_role_tools
from tools import (
    export_as_txt,
    export_as_markdown,
)
from tools.input_validator import validate_input, is_duplicate, clear_duplicate_cache
from memory import ChatStore, init_db, ProfileManager, LearningRecorder
from memory.summarizer import compress_history
from memory.knowledge_graph import KnowledgeGraph
from agents.supervisor import supervisor_decision

# 核心组件
from core.runtime import AgentRuntime
from core.event_bus import EventBus, Event
from core.plugin_manager import PluginManager
from core.trace_store import TraceStore
from core.conversation_adapter import (
    JsonConversationStoreAdapter,
    message_content_to_text,
    normalize_chat_history,
)
from core.event_handlers import register_event_handlers, record_learning_from_text
from core.goal_manager import GoalManager

# Information Engine（深度搜索基础设施 V0.1：搜索源插件化 + 后台任务 + 证据临时存储）
from core.information_engine.search_task import SearchTaskManager
from core.information_engine.evidence_store import EvidenceStore, render_evidence_note

# Daily Agent
from agents.daily_agent import generate_daily_plan, generate_daily_review
from memory.daily_planner import DailyPlanner

# Prompt Evolution
from memory.prompt_store import PromptStore
from agents.prompt_evolution import evaluate_and_evolve, apply_new_version

# Planner Agent（规划助手：分阶段计划 + 多目标冲突分析）
from agents.planner_agent import planner_agent

# Intent Agent（意图识别：从最近对话提取潜在目标建议）
from agents.intent_agent import intent_agent, build_text_from_history

# Planning Mirror Agent（规划镜像：状态快照 + 影响模拟 + 行动建议）
from agents.planning_mirror_agent import planning_mirror_agent

# Daily Planner Agent（每日规划：生成今日计划建议 → 确认后写入计划日期）
from agents.daily_planning_agent import daily_planner_agent

# Reflection Agent（经验沉淀：从对话提取可复用策略/偏好/成功模式）
from agents.reflection_agent import reflection_agent

# Reminder Agent（主动提醒：到点检查今日计划并推送到通知区域）
from agents.reminder_agent import reminder_agent

# Task Executor（任务执行自动化：自动执行可自动任务 + 结果审批落盘 + 提示音）
from agents.task_executor import task_executor, beep_audio_html, log_safe

# Note Store（目标笔记系统：SQLite 索引 + Markdown 文件存储）
from memory.note_store import NoteStore, render_note_preview

# Approval Store（任务执行审批持久化：重启后仍可处理未完成的审批）
from memory.approval_store import ApprovalStore

# 任务模板（一键复用，v3.8 新增）：模板 CRUD 与定时到期判断
from core.task_template import (
    list_templates,
    get_template,
    create_template,
    update_template,
    delete_template,
    set_enabled,
)

# 辅助测试运行器 & Agent 资源消耗监控（v0.1 新增，运维辅助，独立轻量模块）
# P4-2：运行器自 tests/ 迁回 core/，切断"生产代码 import 测试包"的反向依赖
from core.test_runner import (
    run_all_tests,
    list_recent_runs,
    render_runs_markdown,
    is_test_run_running,
)
from memory.resource_log import (
    get_resource_stats,
    get_recent_resource_logs,
)

# Resource Scout（资源机会发现 MVP v0.1 → v0.2 资格过滤 → v0.3 用户画像
# → v0.4 相关性粗筛 + AI 深度筛选：复用搜索 Provider 扫描外部资源机会；
# 资格过滤结合用户画像判断硬性条件；相关性粗筛 + AI 深度筛选供人工评估）
from core.resource_scout import (
    ResourceScout,
    OpportunityStatus,
    EligibilityHint,
    RelevanceStatus,
    TYPE_LABELS,
    STATUS_LABELS,
    ELIGIBILITY_LABELS,
    RELEVANCE_LABELS,
    STUDENT_TYPE_LABELS,
)
from memory.database import get_user_profile, update_user_profile

# AI 深度筛选模块（v0.4：手动按钮触发；LLM 调用放后台线程，不阻塞界面）
from core import ai_filter as ai_filter_module

# 笔记 RAG 向量索引器（语义搜索 + 增删同步）—延迟导入：
# rag 包会触发 chromadb + sentence_transformers 链式导入（约 29 秒），
# 仅在语义搜索时按需加载，避免影响应用启动速度。
NoteIndexer = None  # type: ignore[assignment]

# 经验存储（评分反馈与淘汰机制使用）
from memory.experience_store import ExperienceStore

# 调度器。每日 08:00／20:00 的定时生成自 2026-10-20 阶段二 第一批 第 7 步起不再
# 开机启动（下方 __main__ 里那行调用已删，注记写在那儿）；scheduler.py 与
# run_scheduler／start_scheduler 两个函数原样保留，本行 import 一并留着作指路。
from scheduler import start_scheduler


# ---------- 控制器接入（Phase 3b，2026-09-12：回调迁至 ui/controllers/*，app 为薄壳） ----------
# 先导入 ui.controllers._shared 完成共享上下文初始化（init_db / 单例 / 插件 / 运行时）；
# 下方各控制器 re-export 保持 app 命名空间的公开符号（UI 经 app 模块引用，契约不变）。
from ui.controllers import _shared  # noqa: F401

from ui.controllers.chat_controller import (
    user_message,
    bot_response,
    manual_summarize,
    save_current,
    refresh_session_list,
    create_new_session,
    load_session,
    delete_current_session,
    rename_session,
    do_export,
    show_confirm,
    hide_confirm,
    do_clear,
)
from ui.controllers.profile_controller import (
    save_all_profiles,
    load_profile_to_ui,
    analyze_and_update_profile,
    get_memory_display,
    get_knowledge_tree_display,
)
from ui.controllers.learning_controller import (
    refresh_plan,
    refresh_review,
    evaluate_prompt,
    apply_prompt,
    refresh_review_recommendations,
    refresh_plan_goal_list,
    generate_study_plan,
    incubate_idea,
    generate_quiz,
    explain_concept,
    get_skill_display,
    do_generate_daily_plan,
    do_apply_daily_plan,
    do_check_reminders,
    load_reminder_settings,
    do_save_reminder_times,
    _valid_time_str,
)
from ui.controllers.goals_controller import (
    refresh_goal_list,
    create_new_goal,
    select_goal,
    refresh_goal_detail,
    do_generate_tasks,
    do_start_task,
    do_complete_task,
    # 首页准入那两个按钮（P7 阶段二 组 5 子批 B，2026-10-19）：写的是 `goal_tasks.is_home`，
    # 写口在目标页——首页自己仍然不许准入（第一批「今日工作流」2026-10-20 只给首页放开了
    # `status` 与 `planned_date` 两列，`is_home` 不在其中；见 `home_controller` 文件头）。
    do_add_task_home,
    do_remove_task_home,
    do_mark_goal_completed,
    do_mark_goal_archived,
    do_generate_staged_plan,
    do_apply_staged_plan,
    do_analyze_conflicts,
    do_apply_conflicts,
    do_analyze_intent,
    do_apply_intent_suggestion,
)
from ui.controllers.experience_controller import (
    do_reflect_experiences,
    do_save_experiences,
    _exp_item_label,
    build_experience_feedback_view,
    refresh_exp_panel,
    do_exp_feedback,
    _set_feedback_buttons,
    disable_feedback_buttons,
    enable_feedback_buttons,
    do_cleanup_low_experiences,
    do_mirror_snapshot,
    do_mirror_simulate,
    do_mirror_suggest,
    do_apply_mirror_suggestions,
)
from ui.controllers.task_controller import (
    _advance_execution,
    do_execute_plan,
    do_approve_pending,
    do_reject_pending,
    refresh_pending_list,
    view_pending,
    approve_pending_from_db,
    reject_pending_from_db,
)
from ui.controllers.notes_controller import (
    _notes_panel_view,
    do_refresh_notes,
    do_search_notes,
    do_show_note,
    show_quick_save,
    do_save_note_from_chat,
)
from ui.controllers.templates_controller import (
    _template_form_defaults,
    refresh_template_panel,
    load_template_to_form,
    do_save_template,
    do_delete_template,
    do_toggle_template,
    do_run_template,
    do_approve_template_pending,
    do_reject_template_pending,
    do_check_scheduled_templates,
    do_load_template_notes,
    do_view_template_note,
)
from ui.controllers.review_controller import (
    refresh_review_panel,
    refresh_review_snapshot,
    do_save_manual_section,
    do_view_review_decision,
    do_review_discuss,
    do_review_approve,
    do_review_reject,
    do_review_mark_executed,
    do_review_evaluate,
    do_review_distill,
    EVALUATION_LABELS_ZH,
    do_run_review_now,
)
from ui.controllers.home_controller import (
    refresh_home_panel,
    do_home_jump_decision,
    do_home_jump_task,
    do_home_jump_opportunity,
    # 第一批「今日工作流」（2026-10-20，spec §1.4）：首页上放开的那两个写口、共三枚回调，
    # 各只写自己那一列（开始／完成写 status，安排到今天写 planned_date）。
    do_home_start_task,
    do_home_complete_task,
    do_home_reschedule_today,
)
# 首页「🎯 当前重点」那一格（P7 阶段二 组 5 子批 A，2026-10-18）：写口独立成模块，
# 当时 home_controller 那条「只做读」的契约一个字没动（设计席定案「写口放新模块」）；
# 第一批「今日工作流」（2026-10-20）才在那条契约上开出上面 re-export 的那两个写口例外。
from ui.controllers.focus_controller import (
    refresh_focus_panel,
    do_focus_add,
    do_focus_select,
    do_focus_update,
    do_focus_pause,
    do_focus_complete,
    do_focus_preview,
    do_focus_confirm,
    do_focus_clear,
    do_focus_adopt,
    do_focus_ignore,
)
from ui.controllers.testmon_controller import (
    do_start_test_run,
    do_refresh_test_runs,
    do_refresh_resource_stats,
)
from ui.controllers.search_controller import (
    do_start_search,
    do_refresh_search_panel,
    do_view_search_task,
    refresh_search_save_goals,
    resolve_search_save_goal,
    do_save_search_results,
    show_search_delete_confirm,
    hide_search_delete_confirm,
    do_delete_search_task,
)
from ui.controllers.resource_controller import (
    refresh_opportunities_panel,
    do_scan_opportunities,
    do_ai_filter_opportunities,
    do_set_opportunity_status,
    do_save_user_profile,
    do_recompute_relevance,
    do_cleanup_overdue,
    poll_ai_filter_results,
    load_session_bill,
)

from ui.controllers._shared import (
    chat_store,
    conversation_store,
    plugin_manager,
    event_bus,
    trace_store,
    goal_manager,
    search_task_manager,
    resource_scout,
    opportunity_scan_state,
    ai_filter_state,
    runtime,
)

CUSTOM_CSS = """
.app-title { text-align: center; margin-bottom: 5px; }
.app-subtitle { text-align: center; color: #666; margin-bottom: 15px; }
.tree-text { font-family: monospace; white-space: pre-wrap; }
"""

# ---------- 端口冲突检测（启动前预检，避免直接抛出底层 OSError） ----------
# Gradio 默认端口；可通过环境变量 AGENT_SERVER_PORT 覆盖，无需改代码
DEFAULT_SERVER_PORT = 8502


def check_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """检测端口是否可用（尝试绑定后立即释放）

    注意：这里刻意不设置 SO_REUSEADDR——Windows 上该选项会允许绑定
    已被监听的端口，导致检测误报"可用"。

    Returns:
        True 表示端口空闲可用；False 表示已被占用
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return True
        finally:
            sock.close()
    except OSError:
        return False


def print_port_conflict_help(port: int) -> None:
    """打印端口被占用时的友好排查指引"""
    print("=" * 64)
    print(f"❌ 启动失败：端口 {port} 已被占用（通常是上一次运行的")
    print("   学习助手进程尚未完全退出）。请选择以下任一方式解决：")
    print()
    print("   方式一：结束旧的进程")
    print(f"     1. 在命令行执行：netstat -ano | findstr :{port}")
    print("     2. 找到最后一列的 PID 后执行：taskkill /PID <PID号> /F")
    print()
    print("   方式二：更换端口后重新启动（无需修改代码）")
    print("     PowerShell：$env:AGENT_SERVER_PORT=8503; python app.py")
    print("     CMD：       set AGENT_SERVER_PORT=8503 && python app.py")
    print("=" * 64)


def _start_reminder_fallback_thread():
    """Gradio < 5 无内置 gr.Timer 时的降级方案：后台线程每 60 秒检查一次提醒
    与到期的定时任务模板

    线程中只调用 ReminderAgent / 模板调度（模板执行再放入独立后台线程），
    打印控制台日志、不直接改 UI 组件，保证线程安全；界面通知仍可通过
    「🔔 检查提醒」按钮手动刷新。仅在 HAS_GR_TIMER 为 False 时由 __main__ 启动。
    """
    def _loop():
        import time as _time
        while True:
            _time.sleep(60)
            try:
                result = reminder_agent.check_reminders()
                if result.get("reminder"):
                    print("[Reminder] （后台线程模式）到点提醒已生成，"
                          "请在界面点击「🔔 检查提醒」查看")
            except Exception as e:
                print(f"[Reminder] 后台检查失败（忽略）: {e}")
            try:
                # 检查到期的每日/每周任务模板（内部再起线程执行，不阻塞本循环）
                do_check_scheduled_templates()
            except Exception as e:
                print(f"[TaskTemplate] 后台检查失败（忽略）: {e}")
    threading.Thread(target=_loop, daemon=True, name="reminder-fallback").start()

# ---------- 工厂接入（拆分骨架 V1；Phase 3b 后本模块为薄壳 re-export） ----------
# 置于模块底部：本模块已完整加载（控制器与共享上下文已就绪）后才构建 UI。
from ui.app_factory import create_app  # noqa: E402

# 保持既有副作用：导入 app 即构建完整界面（历史行为与 UI 契约测试依赖，
# 如 tests/test_information_engine.py 所述「导入 app.py 构建完整界面」）。
demo = create_app()

if __name__ == "__main__":
    # 读取端口配置（默认 8502，与历史版本一致；可用环境变量覆盖）
    try:
        server_port = int(os.getenv("AGENT_SERVER_PORT", str(DEFAULT_SERVER_PORT)))
    except ValueError:
        print(f"⚠️ 环境变量 AGENT_SERVER_PORT 不是合法整数，"
              f"已回退默认端口 {DEFAULT_SERVER_PORT}")
        server_port = DEFAULT_SERVER_PORT

    if not check_port_available(server_port):
        print_port_conflict_help(server_port)
        sys.exit(1)  # 以明确的状态码退出，而不是抛出底层 OSError 堆栈

    # 启动期数据库快照（P4-14 子项 ①）：带 6 小时节流，保留份数取
    # `core.db_backup.DEFAULT_KEEP`（**这里不写死数字**：数落在这个文件里，
    # 改常量的人却看着另一个文件，正是 AGENTS.md 执行纪律第 6 条点名的漂移来源）。
    # 放在这里（__main__ 里、任何会写库的启动步骤之前）的三个理由：
    #   1) 快照必须在"本次会话可能写库之前"落盘，否则一次崩溃会把这次的备份
    #      一起带走；
    #   2) 测试只 import app、不执行 __main__，所以这里天然不进测试路径，
    #      不会给 tests/ 下的任何测试文件增加磁盘噪声；
    #   3) 备份是辅助设施——db_backup 内部把异常全部吞掉并只打印 [Backup]
    #      警告，绝不允许因备份失败而让应用起不来。
    from core.db_backup import maybe_startup_snapshot
    maybe_startup_snapshot()

    # 启动补算（P7 批次 1a）：上一周期错过的模板，开机即刻补上一次，不必等
    # 界面里那个 60 秒 gr.Timer 的第一跳。do_check_scheduled_templates
    # （ui/controllers/templates_controller.py）已经是「起 daemon 线程后立即返回」
    # 的非阻塞包装——**这里不要再套一层线程**：套了就有两层，日志与异常口径会分叉。
    # 放在快照之后：补算会写库（pending_approvals / last_run_at），而快照的口径是
    # "本次会话可能写库之前"。（2026-10-20 阶段二 第一批 第 7 步删掉了下方那行调度
    # 启动调用，这里原来紧跟其后的半句「放在 start_scheduler 之前：让调度线程起来时
    # 库已补过」一并删——留着一句指向不存在的事实的理由，比少一句更坏。）
    # 返回值经 log_safe 过滤（GBK 控制台无法编码 emoji），只为在控制台留一行证据。
    # trigger_source="boot"（P7 批次 14）：开机这条路**每次必落一行 template_runs**，
    # 它是"上次开机几点"这个只读问题的分母；界面定时器保持默认 "timer"（空转不记行）。
    print(f"[TaskTemplate] 启动补算已触发："
          f"{log_safe(do_check_scheduled_templates(trigger_source='boot'))}")

    # 每日 08:00／20:00 的定时生成已停用（2026-10-20 阶段二 第一批 第 7 步、
    # spec §3.3 裁定 2）：这里原来那行 start_scheduler 调用已删。scheduler.py 与
    # 它的两个函数原样保留，速览页那两枚「⚡ 手动生成」也已停用（见 ui/app_factory.py
    # 的 Tab 6 说明），所以 daily_plans 不再产新行、历史行仍能只读刷出来；
    # 要恢复定时，把这一行接回来即可。
    # Gradio 版本过旧（无 gr.Timer）时启动后台轮询线程作为提醒降级方案
    if not hasattr(gr, "Timer"):
        _start_reminder_fallback_thread()
    demo.launch(server_port=server_port, inbrowser=True, css=CUSTOM_CSS, theme=gr.themes.Soft())
