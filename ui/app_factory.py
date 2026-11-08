# -*- coding: utf-8 -*-
"""
ui.app_factory —— Gradio 应用工厂（UI 构建逻辑）

背景（2026-09-05 拆分骨架 V1 → 2026-09-12 Phase 3b 演进）：
- 原 app.py 约 4049 行，分为三段：① 导入/常量/模块级单例/全部回调函数；
  ② UI 构建块（gr.Blocks 组件创建、布局与事件绑定）；③ 端口预检与启动逻辑。
  拆分骨架 V1 将第②段 UI 构建整体迁移至本模块 create_app()。
- Phase 3b（2026-09-12）：回调函数体迁至 ui/controllers/*_controller.py，
  app.py 仅保留 re-export 薄壳；本模块不再使用 vars()+globals() 动态注入，
  改为在 create_app() 内延迟 `import app as _app_module` 并显式引用
  `_app_module.<回调名>`（UI 构建期直接调用回调取初始值，事件绑定 fn= 亦指向
  app 薄壳 re-export 的函数对象，契约不变）。
- 已知边界：若在未先导入 app 的情况下独立调用 create_app()，会触发 app
  完整加载（其底部同样调用 create_app 构建 demo）导致重复构建（功能等价、
  浪费资源）；项目内规范用法是直接导入 / 运行 app.py，无重复构建问题。
"""

import gradio as gr


def create_app():
    """构建并返回 Gradio 应用实例（Blocks 树），与原 app.py 模块级构建结果等价。

    说明：
    - UI 构建代码为原 app.py 2568~3961 行的原样迁移（整体缩进 +4），
      未改动任何组件、布局、事件绑定或业务逻辑；
    - 事件绑定回调与初始值取自 app 模块（调用时惰性导入，此时 app 已完整加载）；
    - 返回的 demo 供调用方启动（app.py 的 __main__ 负责 demo.launch() 等）。
    """
    # Phase 3b（2026-09-12）：不再 globals() 注入；延迟导入 app（此时 app 已
    # 完整加载：控制器与共享上下文 ui.controllers._shared 均已就绪），
    # 下方 UI 构建块显式引用 _app_module.<回调名>。
    import app as _app_module
    # UI 构建期使用的下划线私有默认值：自任务模板控制器直接导入
    from ui.controllers.templates_controller import _template_form_defaults  # noqa: F401,E402

    with gr.Blocks(title="📚 私人学习助手") as demo:
        current_session = gr.State("")
        task_choices_state = gr.State([])  # 存储当前未完成任务列表
        staged_plan_state = gr.State(None)  # 暂存待确认写入的分阶段计划（PlannerAgent）
        suggestions_state = gr.State(None)  # 暂存待应用的冲突分析建议（PlannerAgent）
        intent_state = gr.State(None)      # 暂存待应用的意图识别建议（IntentAgent）
        experiences_state = gr.State(None)  # 暂存待保存的经验沉淀建议（ReflectionAgent）
        mirror_suggestions_state = gr.State(None)  # 暂存待应用的行动建议（PlanningMirrorAgent）
        daily_plan_state = gr.State(None)  # 暂存待应用的今日计划（DailyPlannerAgent）

        with gr.Row():
            with gr.Column(scale=1):
                # ==================== 侧边栏 Tabs 布局 ====================
                with gr.Tabs():

                    # ---- Tab 0: 首页（工作台）（P7 阶段二 B1，2026-10-17）----
                    # 默认落地页：放在 Tabs 最前面就够了——gradio 6.24 的 Tabs 参数
                    # 说明逐字写着 selected「Defaults to the first TabItem」。
                    # 14 个旧 Tab 一个都不动、不删、不重排（B1 §七）：它们是原始信息的
                    # 钻取入口，首页只负责把选中的那一条装进**它原本就在的那一页**。
                    # 回调全部在 ui/controllers/home_controller.py，这里只有组件与绑定
                    # （形状照「🧭 项目复盘」那一格：3c-乙 定的「拆分文档 3c 备注要能整体
                    # 搬走」同一条线）。
                    with gr.Tab("🏠 首页"):
                        gr.Markdown("### 🏠 工作台首页")
                        gr.Markdown("> 这一页从上到下：当前重点 → **今日安排** → 持续关注 → 等你定的决策 → "
                                    "还没处理的新发现。卡片那三区每类最多 3 张，**不产生"
                                    "新东西**；批准、勾完成、标感兴趣仍要跟着卡片回到原来"
                                    "那一页点。首页会写库的只有两格：「🎯 当前重点」与"
                                    "「📅 今日安排」，而今日安排那三枚按钮各只写一列"
                                    "（开始／完成写 status，安排到今天写 planned_date），"
                                    "每一次写都要人先点一次按钮，没有弹窗、也没有撤销。")
                        home_summary = gr.Markdown("加载中")
                        home_status = gr.Markdown("")
                        home_refresh_btn = gr.Button("🔄 刷新首页", size="sm")

                        gr.Markdown("---")
                        # ---- 🎯 当前重点（P7 阶段二 组 5 子批 A，2026-10-18）----
                        # 这一格是首页上**唯一**有写的地方，回调全部在
                        # `ui/controllers/focus_controller.py`：上面那三区照旧一条写都不接，
                        # 「只做读」那条契约（B1 §一）因此一字未改，页面级的新口径写在
                        # `PROJECT_STATUS.md` §三 首页行与 `DESIGN_ANCHORS.md` §一.11。
                        gr.Markdown("#### 🎯 当前重点")
                        focus_status = gr.Markdown("")
                        focus_region = gr.Markdown("加载中")
                        focus_manage_dd = gr.Dropdown(
                            label="选一条当前重点（改名／暂停／完成用）", choices=[],
                            value=None, interactive=True,
                        )
                        focus_title_box = gr.Textbox(
                            label="当前重点标题", placeholder="例：把深度搜索的超时链修完")
                        focus_desc_box = gr.Textbox(
                            label="为什么是它、做到什么算完（可留空）",
                            placeholder="选中一条会自动带出原话")
                        with gr.Row():
                            focus_add_btn = gr.Button("➕ 新增重点", size="sm")
                            focus_save_btn = gr.Button("✏️ 保存所选的修改", size="sm")
                            focus_pause_btn = gr.Button("⏸️ 暂停所选", size="sm")
                            focus_complete_btn = gr.Button("✅ 完成所选", size="sm")
                        focus_say_box = gr.Textbox(
                            label="说一句你的重点变化（只生成预览，不写库）",
                            placeholder="例：我现在做 X／我不做 Y 了")
                        focus_preview_md = gr.Markdown("")
                        focus_preview_state = gr.State(None)
                        with gr.Row():
                            focus_preview_btn = gr.Button("🔍 生成变更预览", size="sm")
                            focus_confirm_btn = gr.Button(
                                "✅ 按预览写入", size="sm", variant="primary")
                            focus_clear_btn = gr.Button("🧹 清掉预览", size="sm")
                        focus_candidate_dd = gr.Dropdown(
                            label="建议设为当前重点（选一条）", choices=[], value=None,
                            interactive=True,
                        )
                        with gr.Row():
                            focus_adopt_btn = gr.Button("📌 采纳这条建议", size="sm")
                            focus_ignore_btn = gr.Button("🚫 先不看这条", size="sm")

                        gr.Markdown("---")
                        # ---- 📅 今日安排（阶段二 第一批，2026-10-20；spec §1.1 第 3 项）----
                        # 今日与往日那两份 Markdown 都由 `home_controller.refresh_home_panel` 一次算出来
                        # （口径与去重在 `collect_task_groups`），这一层**只有组件与绑定**。
                        # 两个下拉各配自己那一组：一条任务只属于一组，所以两处选到的不会是同一条。
                        gr.Markdown("#### 📅 今日安排")
                        home_today_md = gr.Markdown("加载中")
                        home_today_select_dd = gr.Dropdown(
                            label="选一条今日任务（开始／完成用）", choices=[], value=None,
                            interactive=True,
                        )
                        with gr.Row():
                            home_today_start_btn = gr.Button("▶️ 开始", size="sm")
                            home_today_complete_btn = gr.Button("✅ 完成", size="sm")
                        # ⚠️ 这两枚按钮的变量名刻意不叫 `*_start_task_btn`／`*_complete_task_btn`：目标页那两枚就叫 `start_task_btn`／`complete_task_btn`，而 `tests/test_task_lifecycle.py::_button_declarations` 按短名扫全文取**第一处**命中（2026-10-23 实测：叫 `home_start_task_btn` 时，那条用例抄到的文案变成了首页这枚的「▶️ 开始」）。首页 Tab 在文件里排在目标页之前，所以后缀撞名的那一处会抢先命中。
                        home_overdue_md = gr.Markdown("加载中")
                        home_overdue_select_dd = gr.Dropdown(
                            label="选一条往日任务（安排到今天用）", choices=[], value=None,
                            interactive=True,
                        )
                        home_reschedule_btn = gr.Button("📌 安排到今天", size="sm")
                        with gr.Row():
                            home_plan_btn = gr.Button("⚡ 帮我排今天", size="sm", variant="primary")
                            home_plan_regen_btn = gr.Button("🔁 按此时长重新生成预览", size="sm")
                        # 这只框**没有 change 绑定**（spec §2.1 的 R4）：动一下框就重算一次＝把人随手滑动变成
                        # 一串模型调用。重算只发生在点「🔁 按此时长重新生成预览」那一下。
                        home_plan_hours = gr.Number(
                            label="本次可用小时（留空＝按档案；档案也没有时暂按 2 小时）",
                            value=None, interactive=True,
                        )
                        home_plan_preview_md = gr.Markdown("")
                        home_plan_apply_btn = gr.Button("✅ 确认并应用", size="sm", variant="primary")

                        gr.Markdown("---")
                        # 区名从「✅ 最近创建的待办」改成「🔄 持续关注」（2026-10-20 阶段二 第一批 spec §1.1）：
                        # 这一族现在**减去**了今日／往日两组里的那些条，旧名字在这一屏上会说谎。界面上的
                        # 跳转按钮与下拉文本一个字没动（它们管的还是「回目标页」那一步）。
                        gr.Markdown("#### 🔄 持续关注")
                        home_task_cards = gr.Markdown("加载中")
                        home_task_select_dd = gr.Dropdown(
                            label="选一张待办卡（跳转用）", choices=[], value=None,
                            interactive=True,
                        )
                        home_task_jump_btn = gr.Button("✅ 去标记完成 →", size="sm")

                        gr.Markdown("---")
                        gr.Markdown("#### 🧭 等你定的决策")
                        home_decision_cards = gr.Markdown("加载中")
                        home_decision_select_dd = gr.Dropdown(
                            label="选一张决策卡（跳转用）", choices=[], value=None,
                            interactive=True,
                        )
                        home_decision_jump_btn = gr.Button(
                            "🔎 查看与批准 →", size="sm", variant="primary")

                        gr.Markdown("---")
                        gr.Markdown("#### 💡 还没处理的新发现")
                        home_opportunity_cards = gr.Markdown("加载中")
                        home_opportunity_select_dd = gr.Dropdown(
                            label="选一张发现卡（跳转用）", choices=[], value=None,
                            interactive=True,
                        )
                        home_opportunity_jump_btn = gr.Button("🔎 查看 →", size="sm")

                    # ---- Tab 1: 会话与参数 ----
                    with gr.Tab("📋 会话与参数"):
                        gr.Markdown("### 📋 会话管理")
                        session_dropdown = gr.Dropdown(
                            label="📂 历史会话", choices=[], value=None,
                            interactive=True, allow_custom_value=True,
                        )
                        with gr.Row():
                            new_session_btn = gr.Button("➕ 新建", size="sm", scale=1)
                            delete_session_btn = gr.Button("🗑️ 删除", size="sm", variant="stop", scale=1)
                        rename_input = gr.Textbox(label="重命名", placeholder="输入新名称后回车")
                        session_info = gr.Markdown("暂无会话")

                        gr.Markdown("---")
                        gr.Markdown("### ⚙️ 参数设置")
                        temperature_slider = gr.Slider(label="🌡️ 温度", minimum=0.0, maximum=2.0, value=0.7, step=0.1)
                        max_tokens_slider = gr.Slider(label="📏 最大长度", minimum=256, maximum=4096, value=2048, step=256)
                        enable_memory_check = gr.Checkbox(label="🧠 上下文记忆", value=True)
                        enable_evaluator_check = gr.Checkbox(label="🧪 学习评估", value=True)

                    # ---- Tab 2: 档案与记忆 ----
                    with gr.Tab("👤 档案与记忆"):
                        gr.Markdown("### 👤 用户档案")
                        profile_english = gr.Textbox(label="英语水平", placeholder="如：初级 / 中级 / 高级")
                        profile_ai = gr.Textbox(label="AI基础", placeholder="如：零基础 / 了解概念 / 能写代码")
                        profile_time = gr.Textbox(label="每日学习时间", placeholder="如：1小时 / 2小时")
                        profile_style = gr.Textbox(label="偏好讲解方式", placeholder="如：举例说明 / 理论推导")
                        profile_goal = gr.Textbox(label="长期目标", placeholder="如：通过面试 / 完成项目")
                        with gr.Row():
                            profile_save_btn = gr.Button("💾 保存档案", size="sm", variant="primary")
                            update_profile_btn = gr.Button("🔄 AI分析对话更新档案", size="sm")
                        profile_status = gr.Markdown("")

                        gr.Markdown("---")
                        gr.Markdown("### 📊 记忆查看")
                        memory_display = gr.Markdown(_app_module.get_memory_display())
                        refresh_memory_btn = gr.Button("🔄 刷新记忆", size="sm")

                    # ---- Tab 3: 目标管理 ----
                    with gr.Tab("🎯 目标管理"):
                        goal_dropdown = gr.Dropdown(label="当前目标", choices=[], value=None, interactive=True, allow_custom_value=True)
                        new_goal_title = gr.Textbox(label="新目标标题", placeholder="输入新目标")
                        new_goal_desc = gr.Textbox(label="目标描述", lines=2, placeholder="可选")
                        create_goal_btn = gr.Button("➕ 创建目标", size="sm")
                        goal_status = gr.Markdown("")

                        goal_detail = gr.Markdown("选择目标后显示详情")
                        generate_tasks_btn = gr.Button("🧠 生成任务", size="sm", variant="primary")
                        refresh_tasks_btn = gr.Button("🔄 刷新任务", size="sm")
                        complete_task_dropdown = gr.Dropdown(label="选择要完成的任务", choices=[], interactive=True)
                        start_task_btn = gr.Button("▶️ 开始任务", size="sm")
                        complete_task_btn = gr.Button("✅ 标记任务完成", size="sm")
                        # 首页准入（P7 阶段二 组 5 子批 B，2026-10-19）：写的是刚选中的那条
                        # `goal_tasks.is_home`。下拉复用上面那个「选择要完成的任务」——
                        # 同一屏里「哪一条」这个问题只该有一个答案来源，再起一个下拉就会
                        # 出现「两个下拉选的不是同一条」这种界面自己造出来的歧义。
                        with gr.Row():
                            add_home_btn = gr.Button("🏠 加入首页", size="sm")
                            remove_home_btn = gr.Button("🚪 移出首页", size="sm")
                        with gr.Row():
                            mark_completed_btn = gr.Button("🏁 标记目标完成", size="sm")
                            mark_archived_btn = gr.Button("📦 归档目标", size="sm")
                        goal_action_status = gr.Markdown("")

                        # ---- 笔记系统（NoteStore：SQLite 索引 + 文件存储） ----
                        with gr.Accordion("📝 笔记（列表 · 搜索 · 查看）", open=False):
                            gr.Markdown("> ℹ️ 展示当前目标的笔记（数据库索引 + "
                                        "`data/goals/{目标}/notes/` 下的 Markdown 文件，"
                                        "目录内自动维护 index.md）；「▶️ 执行计划」中确认保存的笔记会出现在这里。"
                                        "支持标签筛选与关键词/语义双模式搜索。")
                            notes_list_display = gr.Markdown("选择目标后显示笔记列表")
                            with gr.Row():
                                note_search_input = gr.Textbox(
                                    label="🔍 搜索", placeholder="输入关键词",
                                    scale=3,
                                )
                                note_search_mode = gr.Radio(
                                    label="模式",
                                    choices=[("关键词", "keyword"), ("语义", "semantic")],
                                    value="keyword",
                                    scale=1,
                                    interactive=True,
                                )
                                note_search_btn = gr.Button("🔍 搜索", size="sm", scale=1)
                                notes_refresh_btn = gr.Button("🔄 刷新", size="sm", scale=1)
                            note_tag_filter = gr.Textbox(
                                label="🏷️ 标签筛选",
                                placeholder="输入标签（如：学习），留空显示全部",
                                scale=3,
                            )
                            note_view_dropdown = gr.Dropdown(
                                label="选择要查看的笔记", choices=[], value=None, interactive=True,
                            )
                            note_content_display = gr.Markdown("")

                        # ---- Planner 规划助手（分阶段计划 + 冲突分析） ----
                        gr.Markdown("---")
                        gr.Markdown("### 🗺️ Planner 规划助手")
                        with gr.Accordion("🚩 分阶段学习计划（里程碑 + 任务）", open=False):
                            gr.Markdown("> ℹ️ 写入时会**替换该目标下 AI 生成的未完成任务**，手动添加的任务不受影响。")
                            plan_weeks_slider = gr.Slider(
                                label="📅 总周期（周）", minimum=2, maximum=24, value=8, step=1,
                            )
                            plan_new_title = gr.Textbox(
                                label="新目标标题（留空则为上方当前目标生成计划）",
                                placeholder="可选：直接为新目标生成计划",
                            )
                            plan_new_desc = gr.Textbox(label="新目标描述", lines=2, placeholder="可选")
                            staged_plan_btn = gr.Button("🗺️ 生成分阶段计划", variant="primary", size="sm")
                            staged_plan_display = gr.Markdown("生成的计划将显示在此")
                            apply_plan_btn = gr.Button("✅ 写入目标系统", variant="secondary", size="sm")

                        with gr.Accordion("⚖️ 多目标冲突分析", open=False):
                            conflict_btn = gr.Button("⚖️ 分析目标冲突", variant="primary", size="sm")
                            conflict_display = gr.Markdown("冲突分析与优先级建议将显示在此")
                            apply_conflicts_btn = gr.Button("✅ 应用建议", variant="secondary", size="sm")

                        # ---- Intent 意图识别（从对话提取潜在目标） ----
                        with gr.Accordion("🧭 意图识别（从对话提取目标）", open=False):
                            gr.Markdown("> ℹ️ 分析当前会话最近的用户发言（或在下方粘贴文本），"
                                        "识别潜在目标并给出建议；确认后才会写入目标系统。")
                            intent_text_input = gr.Textbox(
                                label="补充分析文本（留空则读取当前会话最近对话）",
                                lines=2, placeholder="可选：直接粘贴一段想法或计划",
                            )
                            intent_analyze_btn = gr.Button("🎯 识别意图", variant="primary", size="sm")
                            intent_display = gr.Markdown("识别到的目标建议将显示在此")
                            intent_apply_btn = gr.Button("✅ 应用建议", variant="secondary", size="sm")

                        # ---- Planning Mirror 规划镜像（快照 / 影响模拟 / 行动建议） ----
                        with gr.Accordion("🪞 规划镜像（状态快照 · 影响模拟 · 行动建议）", open=False):
                            gr.Markdown("> ℹ️ 快照与影响模拟均为**只读分析**；行动建议确认后才会应用"
                                        "（更新优先级/截止日期或暂停目标）。")
                            mirror_snapshot_btn = gr.Button("📊 状态快照", variant="primary", size="sm")
                            mirror_snapshot_display = gr.Markdown("目标状态快照将显示在此")

                            gr.Markdown("---")
                            mirror_new_title = gr.Textbox(
                                label="🧪 拟新增的目标标题",
                                placeholder="例如：三个月通过 N2 考试",
                            )
                            mirror_new_desc = gr.Textbox(
                                label="新目标描述", lines=2, placeholder="可选",
                            )
                            with gr.Row():
                                mirror_weeks_slider = gr.Slider(
                                    label="📅 预计周期（周）", minimum=1, maximum=52, value=4, step=1,
                                )
                                mirror_priority_radio = gr.Radio(
                                    label="⚡ 优先级",
                                    choices=[("🟢 高（1）", 1), ("🟡 中（2）", 2), ("🔴 低（3）", 3)],
                                    value=2,
                                )
                            mirror_simulate_btn = gr.Button("🧪 影响模拟", variant="primary", size="sm")
                            mirror_impact_display = gr.Markdown("新增目标的影响评估将显示在此")

                            gr.Markdown("---")
                            mirror_suggest_btn = gr.Button("💡 生成行动建议", variant="primary", size="sm")
                            mirror_suggest_display = gr.Markdown("下一步行动建议将显示在此")
                            mirror_apply_btn = gr.Button("✅ 应用建议", variant="secondary", size="sm")

                        # ---- Daily Planner 每日规划（生成今日计划 → 确认应用） ----
                        with gr.Accordion("📅 每日规划", open=False):
                            gr.Markdown("> ℹ️ **目标感知版**：结合所有进行中目标的未完成任务、档案中的每日可用时间与相关经验，"
                                        "生成今日计划建议；确认后会把所选任务标记为今天执行"
                                        "（写入计划日期，任务列表中显示 📅 标记）。"
                                        "这里生成的是**结构化计划**，可直接「✅ 应用」，或交给下方「▶️ 执行计划」自动执行。")
                            daily_plan_btn = gr.Button("📅 生成今日计划", variant="primary", size="sm")
                            daily_plan_display = gr.Markdown("今日计划建议将显示在此")
                            daily_plan_apply_btn = gr.Button("✅ 应用计划（标记为今天）", variant="secondary", size="sm")

                            # ---- 任务执行自动化（TaskExecutor：自动执行 + 审批落盘） ----
                            gr.Markdown("---")
                            gr.Markdown("> 🤖 **任务执行自动化**：点击「▶️ 执行计划」后，AI 会依次执行计划中"
                                        "标记为「⚡ 可自动」的任务（首批支持资料搜索、笔记整理两类）；"
                                        "产生的结果先展示供你审阅，点击确认后才会写入磁盘，"
                                        "需要审批时会自动播放提示音。")
                            with gr.Row():
                                execute_plan_btn = gr.Button("▶️ 执行计划", variant="primary", size="sm")
                                exec_approve_btn = gr.Button("✅ 确认保存", variant="secondary", size="sm")
                                exec_reject_btn = gr.Button("❌ 拒绝", variant="stop", size="sm")
                            exec_display = gr.Markdown("执行进度将显示在此")
                            exec_audio_slot = gr.HTML("")  # 审批提示音插槽（暂停时注入 <audio autoplay>）
                            execution_gen_state = gr.State(None)  # 暂存暂停中的执行生成器

                            # ---- 待审批持久化列表（重启后仍可处理未完成的审批） ----
                            gr.Markdown("---")
                            gr.Markdown("> 📋 **待审批列表**：执行计划暂停时，审批请求会**持久化保存**；"
                                        "即使重启应用导致内存中的执行进度丢失，仍可在此查看并处理"
                                        "未完成的审批（确认后重新执行对应任务并落盘）。")
                            with gr.Row():
                                pending_refresh_btn = gr.Button("🔄 刷新待审批", size="sm", scale=1)
                                pending_select_dd = gr.Dropdown(
                                    label="选择待审批记录", choices=[], value=None,
                                    interactive=True, scale=4,
                                )
                            pending_list_display = gr.Markdown("暂无待审批的记录")
                            pending_display = gr.Markdown("选择待审批记录后在此显示内容")
                            with gr.Row():
                                pending_approve_btn = gr.Button("✅ 确认并执行", variant="primary", size="sm")
                                pending_reject_btn = gr.Button("❌ 拒绝", variant="stop", size="sm")
                            pending_status = gr.Markdown("")

                            # ---- 主动提醒设置（ReminderAgent：到点检查今日计划） ----
                            gr.Markdown("---")
                            gr.Markdown("> ⏰ **主动提醒**：到达下方设定的时间点时，系统会自动检查"
                                        "「应用计划」标记的今天任务，并在页面顶部通知区域与控制台日志提醒你。")
                            reminder_times_input = gr.Textbox(
                                label="⏰ 提醒时间（HH:MM，逗号分隔多个）",
                                placeholder="如：09:00,14:00",
                                scale=3,
                            )
                            reminder_save_btn = gr.Button("💾 保存提醒时间", variant="secondary", size="sm")
                            reminder_status = gr.Markdown("")

                    # ---- Tab 4: 知识图谱 ----
                    with gr.Tab("🧠 知识图谱"):
                        knowledge_domain = gr.Dropdown(label="领域", choices=["AI", "英语", "系统科学", "通用"], value="AI")
                        refresh_knowledge_btn = gr.Button("🔄 刷新知识树", size="sm")
                        knowledge_tree = gr.Markdown("暂无知识节点", elem_classes="tree-text")

                    # ---- Tab 5: 深度搜索（Information Engine V0.1） ----
                    with gr.Tab("🔍 深度搜索"):
                        gr.Markdown("### 🔍 深度搜索（Information Engine V0.1）")
                        gr.Markdown("> ℹ️ 手动发起深度搜索任务，**后台异步执行**（最多 3 个并发）；"
                                    "结果先**临时存储**展示，勾选证据并点击「✅ 确认保存」"
                                    "后才会写入笔记系统。")
                        search_query_input = gr.Textbox(
                            label="搜索查询", lines=2,
                            placeholder="输入要深度搜索的问题或关键词（如：Transformer 位置编码实现细节）",
                        )
                        with gr.Row():
                            search_start_btn = gr.Button("🚀 开始搜索", variant="primary", size="sm")
                            refresh_search_btn = gr.Button("🔄 刷新任务状态", size="sm")
                        search_status = gr.Markdown("")

                        gr.Markdown("---")
                        gr.Markdown("### 📋 任务列表")
                        search_task_display = gr.Markdown("暂无搜索任务")

                        gr.Markdown("---")
                        gr.Markdown("### 📄 结果预览（临时存储）")
                        search_view_task_dd = gr.Dropdown(
                            label="选择任务查看证据", choices=[], value=None, interactive=True,
                        )
                        search_result_display = gr.Markdown("选择任务后在此预览证据")

                        # 删除任务（先确认再执行：不可逆，将同时删除该任务下全部证据）
                        with gr.Row(visible=False) as search_delete_confirm_row:
                            gr.Markdown("⚠️ **确认删除该任务？此操作不可逆，"
                                        "将同时删除该任务下的全部证据。**", scale=4)
                            search_delete_yes = gr.Button("🗑️ 确认删除", variant="stop", size="sm", scale=1)
                            search_delete_no = gr.Button("❌ 取消", variant="secondary", size="sm", scale=1)
                        with gr.Row():
                            search_delete_btn = gr.Button("🗑 删除任务", variant="stop", size="sm", scale=1)
                            search_delete_status = gr.Markdown("", scale=4)

                        gr.Markdown("---")
                        gr.Markdown("### 💾 确认保存为笔记（用户确认后才写入）")
                        search_evidence_select = gr.CheckboxGroup(
                            label="☑️ 勾选要保留的证据", choices=[], value=[], interactive=True,
                        )
                        with gr.Row():
                            search_save_goal_dd = gr.Dropdown(
                                label="🎯 保存到目标（可选，留空则存到「资料搜索」）",
                                choices=[], value=None, interactive=True,
                                allow_custom_value=True, scale=4,
                            )
                            search_save_btn = gr.Button("✅ 确认保存", variant="secondary", size="sm", scale=1)
                        search_save_status = gr.Markdown("")

                    # ---- Tab 6: 技能列表 ----
                    with gr.Tab("🧩 技能列表"):
                        skill_display = gr.Markdown("加载中...")

                        gr.Markdown("---")
                        gr.Markdown("### 💡 想法孵化器")
                        idea_input = gr.Textbox(
                            label="💡 你的想法",
                            placeholder="输入一个天马行空的想法，例如：我想做一个自动整理笔记的机器人",
                            lines=3,
                        )
                        idea_mode = gr.Radio(
                            label="输出模式",
                            choices=[("生成目标", "goal"), ("生成目标和任务", "plan")],
                            value="goal",
                        )
                        incubate_btn = gr.Button("🚀 孵化想法", variant="primary", size="sm")
                        incubate_result = gr.Markdown("孵化结果将显示在此")

                        gr.Markdown("---")
                        gr.Markdown("### 📚 学习计划生成器")
                        plan_goal_dropdown = gr.Dropdown(
                            label="🎯 选择目标（可选）",
                            choices=[],
                            value=None,
                            interactive=True,
                            allow_custom_value=True,
                        )
                        plan_days_slider = gr.Slider(
                            label="📅 计划天数",
                            minimum=1,
                            maximum=30,
                            value=7,
                            step=1,
                        )
                        plan_save_checkbox = gr.Checkbox(label="💾 保存为目标任务", value=False)
                        generate_study_plan_btn = gr.Button("📚 生成学习计划", variant="primary", size="sm")
                        plan_study_result_display = gr.Markdown("生成的学习计划将显示在此")

                        gr.Markdown("---")
                        gr.Markdown("### 📘 概念测验")
                        quiz_concept_input = gr.Textbox(
                            label="📘 概念（可选）",
                            placeholder="输入要测验的概念，如：机器学习；留空则自动选取薄弱点",
                            lines=1,
                        )
                        quiz_difficulty_radio = gr.Radio(
                            label="难度",
                            choices=[("基础", "easy"), ("中等", "medium"), ("困难", "hard")],
                            value="medium",
                        )
                        quiz_num_slider = gr.Slider(
                            label="题目数量",
                            minimum=1,
                            maximum=5,
                            value=3,
                            step=1,
                        )
                        generate_quiz_btn = gr.Button("📝 生成测验", variant="primary", size="sm")
                        quiz_result_display = gr.Markdown("生成的测验将显示在此")

                        gr.Markdown("---")
                        gr.Markdown("### 📖 概念讲解")
                        gr.Markdown("> ℹ️ 用生活类比和实例把概念讲一遍（零基础 / 有基础 / 进阶三档），"
                                    "与上面的「概念测验」配套：先听懂，再自测。")
                        explain_concept_input = gr.Textbox(
                            label="📖 概念",
                            placeholder="输入要讲解的概念，如：梯度下降",
                            lines=1,
                        )
                        explain_level_radio = gr.Radio(
                            label="讲解对象",
                            choices=[
                                ("零基础初学者", "beginner"),
                                ("有一定基础", "intermediate"),
                                ("进阶学习者", "advanced"),
                            ],
                            value="beginner",
                        )
                        explain_concept_btn = gr.Button("📖 生成讲解", variant="primary", size="sm")
                        explain_result_display = gr.Markdown("生成的讲解将显示在此")

                    # ---- Tab 6: 计划与复盘 ----
                    with gr.Tab("📅 计划与复盘"):
                        gr.Markdown("### 📅 今日计划")
                        gr.Markdown("> ℹ️ **旧版·仅历史查看**：这一页的计划／复盘是一段 Markdown 文本，"
                                    "**只展示、不写库**，也没有「确认后标记为今天执行」那一层。"
                                    "两枚「⚡ 手动生成」已停用，后台每日 08:00／20:00 的定时"
                                    "生成同样不再运行（`app.py` 不再调 `start_scheduler()`）；"
                                    "两枚「🔄 刷新」仍能读到历史。今日安排请去首页的"
                                    "「📅 今日安排」或目标页的「📅 每日规划」。")
                        plan_display = gr.Markdown("暂无计划")
                        with gr.Row():
                            refresh_plan_btn = gr.Button("🔄 刷新", size="sm")
                            generate_plan_btn = gr.Button("⚡ 手动生成（速览）", size="sm", variant="primary", interactive=False)

                        gr.Markdown("---")
                        gr.Markdown("### 🌙 今日复盘")
                        review_display = gr.Markdown("暂无复盘")
                        with gr.Row():
                            refresh_review_btn = gr.Button("🔄 刷新", size="sm")
                            generate_review_btn = gr.Button("⚡ 手动生成", size="sm", variant="primary", interactive=False)

                    # ---- Tab 7: Prompt 进化 ----
                    with gr.Tab("🧪 Prompt 进化"):
                        prompt_task_name = gr.Textbox(label="任务名称", placeholder="如：解释Transformer")
                        prompt_score_slider = gr.Slider(label="评分", minimum=0, maximum=100, value=70, step=1)
                        prompt_feedback = gr.Textbox(label="反馈（可选）", lines=2)
                        prompt_evaluate_btn = gr.Button("📊 评估并优化", size="sm", variant="primary")
                        prompt_status = gr.Markdown("")
                        prompt_suggestion_display = gr.Markdown("优化建议将显示在此")
                        prompt_apply_btn = gr.Button("✅ 应用新版本", size="sm", variant="secondary", visible=False)

                    # ---- Tab 8: 复习推荐 ----
                    with gr.Tab("🔁 复习推荐"):
                        review_rec_domain = gr.Dropdown(label="领域", choices=["AI", "英语", "系统科学", "通用"], value="AI")
                        refresh_review_rec_btn = gr.Button("🔄 刷新推荐", size="sm", variant="primary")
                        review_rec_display = gr.Markdown("点击刷新按钮查看推荐复习内容")

                    # ---- Tab 9: 导出 ----
                    with gr.Tab("📤 导出"):
                        export_format = gr.Radio(label="格式", choices=["TXT", "Markdown"], value="TXT")
                        export_btn = gr.Button("📥 导出", variant="primary", size="sm")
                        export_output = gr.File(label="下载")

                    # ---- Tab 10: 任务模板（一键复用，v3.8 新增） ----
                    with gr.Tab("🧩 任务模板"):
                        gr.Markdown("### 🧩 自动化任务模板（一键复用）")
                        gr.Markdown("> ℹ️ 把常用的自动化任务保存为模板（动作有三种："
                                    "**「搜索并整理笔记」**（联网）、**「资源筛选报告」**"
                                    "（只读本地机会库、不联网）、**「长期项目复盘」**"
                                    "（打一次模型，产一行 Decision 账目，见「🧭 项目复盘」页）），"
                                    "可**手动一键运行**，或设置**每日/每周定时**自动触发；"
                                    "前两种触发后走与「▶️ 执行计划」相同的任务执行与审批流程"
                                    "（结果先审阅、批准后才写入磁盘）；"
                                    "复盘那一种**不产笔记、不产生待审批项**，所以不走审批点。")
                        template_schedule_status = gr.Markdown(
                            "⏳ 定时调度：每 60 秒自动检查一次到期的每日/每周模板")
                        with gr.Row():
                            template_refresh_btn = gr.Button("🔄 刷新模板列表", size="sm", scale=1)
                            template_run_btn = gr.Button("▶️ 运行", variant="primary", size="sm", scale=1)
                            template_toggle_btn = gr.Button("⏯️ 启用/停用", size="sm", scale=1)
                            template_delete_btn = gr.Button("🗑️ 删除", variant="stop", size="sm", scale=1)
                        template_list_display = gr.Markdown("暂无任务模板")
                        template_select_dd = gr.Dropdown(
                            label="选择模板（先选择再运行/编辑/删除）",
                            choices=[], value=None, interactive=True,
                        )
                        template_action_status = gr.Markdown("")

                        # 本模板产物（只读，P7 批次 1c-③）：模板产出的笔记挂在虚拟目标
                        # （template_{id}）下，而「笔记」页的目标下拉框只列 goals 表里的
                        # 真实目标，所以产物落盘后在界面上没有入口——这一块补的就是它。
                        # 刻意只读、只在本 Tab 内闭环，不动「笔记」页那个共享下拉框。
                        gr.Markdown("---")
                        gr.Markdown("### 📁 本模板的产物（只读）")
                        template_notes_display = gr.Markdown(
                            "选择模板后在此列出它产出的笔记"
                            "（选模板或点上方「🔄 刷新模板列表」即可刷新）")
                        template_notes_dd = gr.Dropdown(
                            label="选择一条产物查看正文", choices=[], value=None,
                            interactive=True,
                        )
                        template_note_view = gr.Markdown("选择一条产物后在此显示正文")

                        gr.Markdown("---")
                        gr.Markdown("### ➕ 新建 / ✏️ 编辑模板")
                        template_id_state = gr.State(None)  # 编辑中的模板 ID（None=新建）
                        with gr.Row():
                            template_name_input = gr.Textbox(
                                label="模板名称", placeholder="如：每周一搜索 AI 竞赛资讯",
                                scale=3,
                            )
                            template_query_input = gr.Textbox(
                                label="搜索关键词（动作：搜索并整理笔记）",
                                placeholder="如：AI竞赛", scale=3,
                            )
                        template_desc_input = gr.Textbox(
                            label="模板描述（可选）", lines=1,
                            placeholder="说明这个模板的用途",
                        )
                        # 动作类型（P7 第一闭环 Phase 3c-乙，2026-10-06）：3b 之前表单
                        # 根本没有这一格，所以库里那种"建得出、跑不出"的中间态在界面上
                        # 不可达；3c-甲 接上执行分支之后这一格必须可选，否则
                        # long_term_review 只能靠脚本造——本处就是那一格。三个值就是
                        # core.task_template.VALID_ACTIONS 的三个成员（覆盖关系由测试
                        # 逐个比对，不靠这里写全）。
                        # ⚠️ 选了「长期项目复盘」时**这一格不读关键词**：关键词那一路是
                        # search_and_note 的参数，复盘动作一条参数都不读（见执行侧那条
                        # docstring），所以留空是正常的，不是漏填。
                        template_action_dd = gr.Radio(
                            label="动作类型（这一格决定模板到点实际跑什么）",
                            choices=[("🔍 搜索并整理笔记（要关键词，联网）", "search_and_note"),
                                     ("🧊 资源筛选报告（只读本地机会库，不联网）", "scout_digest"),
                                     ("🧭 长期项目复盘（打一次模型，产一行 Decision 账目）",
                                      "long_term_review")],
                            value="search_and_note",
                        )
                        with gr.Row():
                            template_trigger_radio = gr.Radio(
                                label="触发类型",
                                choices=[("🖐 手动触发", "manual"),
                                         ("📅 每日", "daily"),
                                         ("📆 每周", "weekly")],
                                value="manual",
                            )
                            template_time_input = gr.Textbox(
                                label="触发时间（HH:MM）", value="09:00",
                                placeholder="如 09:00", scale=1,
                            )
                            template_weekday_dd = gr.Dropdown(
                                label="星期几（每周触发时）",
                                choices=[("周一", 0), ("周二", 1), ("周三", 2),
                                         ("周四", 3), ("周五", 4), ("周六", 5),
                                         ("周日", 6)],
                                value=0, scale=1,
                            )
                        template_auto_approve_cb = gr.Checkbox(
                            label="自动审批（勾选后执行结果自动保存，无需逐条确认）",
                            value=False,
                        )
                        with gr.Row():
                            template_save_btn = gr.Button("💾 保存模板", variant="primary", size="sm")
                            template_cancel_btn = gr.Button("❌ 取消编辑", size="sm")

                        gr.Markdown("---")
                        gr.Markdown("### 🚀 模板执行进度（审批流程与「执行计划」一致）")
                        template_exec_display = gr.Markdown("模板执行进度将显示在此")
                        template_exec_audio = gr.HTML("")  # 审批提示音插槽
                        with gr.Row():
                            template_approve_btn = gr.Button("✅ 确认保存", variant="secondary", size="sm")
                            template_reject_btn = gr.Button("❌ 拒绝", variant="stop", size="sm")
                        template_exec_state = gr.State(None)  # 暂存暂停中的模板执行生成器

                    # ---- 项目复盘（P7 第一闭环 Phase 3c-乙，2026-10-06） ----
                    # 回调**全部集中在** ui/controllers/review_controller.py：拆分文档 3c
                    # 的备注说"这次之后要重整 UI，这一 Tab 要能整体搬走"，所以这里只放
                    # 组件与事件绑定，业务读写的入口只有控制器导出的那几个函数。
                    with gr.Tab("🧭 项目复盘"):
                        gr.Markdown("### 🧭 项目复盘（Decision 账本）")
                        gr.Markdown("> 这一页读的是 `decisions` 账本：每周一次的"
                                    "「长期项目复盘」模板跑完之后在这里立候选决策。"
                                    "**跑复盘不在这一页**：去「🧩 任务模板」页建一条动作"
                                    "选「🧭 长期项目复盘」的模板，再点它右边的**「▶️ 运行」**。"
                                    "本页没有「运行」按钮是设计如此，不是这一页坏了——"
                                    "「主动」指的是模板那一侧的「▶️ 运行」。"
                                    "这一页只做这几件事：读账本、写讨论、批准、否决、"
                                    "标记已执行、评估、提炼经验。后两格是 4a 收尾补的——"
                                    "闭环要的是「已执行 → 已评估 → 已提炼」那两跳也能在界面上走完，"
                                    "缺了它们 `link_experience` 就没有生产调用方。")
                        review_snapshot_display = gr.Markdown("📌 最近快照：加载中")
                        # ---- 人工段输入（缺口修复，2026-10-07）----
                        # 闸门①（core.long_term_project_review.has_manual_section）要一条
                        # kind 不为 fact 的条目，而生产路径上从来没有地方保存过快照
                        # （save_project_state 零调用方）⇒ latest_project_state() 恒为 None
                        # ⇒ 复盘出口恒为 manual_section_missing，闭环第一步就断。
                        # 这一格就是那条缺的写入路径：只落一份新快照，**不触发复盘**——
                        # 触发仍然只有模板那一侧的「▶️ 运行」（单一触发口径是既有定案）。
                        review_manual_input = gr.Textbox(
                            label="人工段（必填：你这周自己的话）", lines=5,
                            placeholder="投入了多少／卡在哪／想法变了没／发现了什么新问题",
                        )
                        review_save_manual_btn = gr.Button(
                            "💾 保存这份快照（人工段）", size="sm")
                        review_manual_status = gr.Markdown("")
                        with gr.Row():
                            review_refresh_btn = gr.Button("🔄 刷新账本", size="sm", scale=2)
                            review_guide_btn = gr.Button("🧭 怎么跑一次复盘", size="sm", scale=1)
                        review_list_display = gr.Markdown("暂无决策记录")
                        review_select_dd = gr.Dropdown(
                            label="选择一条决策（选中后下方显示全文）",
                            choices=[], value=None, interactive=True,
                        )
                        review_action_status = gr.Markdown("")
                        review_guide_status = gr.Markdown("")

                        gr.Markdown("---")
                        gr.Markdown("### 📄 全文回读（原文不截断）")
                        review_detail_display = gr.Markdown("选择一条决策后在此显示全文")

                        gr.Markdown("---")
                        gr.Markdown("### 💬 讨论与推进（状态只能按状态机走：跳批准直接"
                                    "标记已执行会被拒）")
                        review_comment_input = gr.Textbox(
                            label="讨论内容（必填，登记成「他本人的话」）",
                            lines=3,
                            placeholder="你对这一条的判断：同意／不同意／要改哪里",
                        )
                        review_prior_intent_input = gr.Textbox(
                            label="我当时的判断（**可选**，非空时登记成「先采信的前提」，"
                                  "4a 复盘时它就是被验证或被证伪的那一条）",
                            lines=2,
                            placeholder="留空也可以：讨论照常登记、状态照常推进",
                        )
                        review_exec_ref_input = gr.Textbox(
                            label="执行痕迹（**可选**，非空时写进 `execution_reference`："
                                  "笔记路径、链接、一句话都行）",
                            lines=1,
                            placeholder="留空也可以——执行了但没找到可指的东西，"
                                        "比编一个路径出来更诚实",
                        )
                        with gr.Row():
                            review_discuss_btn = gr.Button("💬 记入讨论", variant="primary",
                                                           size="sm", scale=2)
                            review_approve_btn = gr.Button("✅ 批准", size="sm", scale=1)
                            review_reject_btn = gr.Button("🚫 否决", size="sm", scale=1)
                            review_executed_btn = gr.Button("🏁 标记已执行", size="sm", scale=1)

                        # ---- 复盘与回流（P7 第一闭环 Phase 4a UI 收尾，2026-10-07 裁决 2） ----
                        # 回调同样**全部在** review_controller.py 里，这里只有组件与绑定。
                        # 刻意不清空输入框（与上面「记入讨论」那条 `.then` 不同）：这两格的
                        # 内容是一整段话，写失败时把它抹掉比「多点一次」更难恢复；而重复提炼
                        # 的代价由「一个决策可以提炼多条」这条定案兜着（4b 注入时最多取一条）。
                        gr.Markdown("---")
                        gr.Markdown("### 📝 复盘与回流（顺序是死的：批准 → 标记已执行 → 评估 → 提炼）")
                        gr.Markdown("> 跳步不会被界面代填：能不能走由 `core/decision_store` 的守卫判，"
                                    "这一页只把它接成人能读的一行。"
                                    "「评估」要**复盘结论＋实际结果**同时给（只填一个会被拒）；"
                                    "「提炼经验」那句**可以留空**——写不出明确教训就不提炼，"
                                    "这是设计里的正常出口。")
                        review_evaluation_dd = gr.Dropdown(
                            label="复盘结论（选一个，五值之外没有别的写法）",
                            choices=[(label, value) for value, label in
                                     _app_module.EVALUATION_LABELS_ZH.items()],
                            value=None, interactive=True,
                        )
                        review_actual_result_input = gr.Textbox(
                            label="实际结果（**必填**，与复盘结论同时入账——"
                                  "`evaluated` 态的定义就是这两格同时非空）",
                            lines=2,
                            placeholder="做完之后实际发生了什么，照实写；写不出来说明还没做",
                        )
                        review_evaluate_btn = gr.Button("📝 评估", variant="primary", size="sm")
                        review_future_effect_input = gr.Textbox(
                            label="下次改什么（**可选**，这一句就是 4b 要注回上下文的那条教训）",
                            lines=2,
                            placeholder="留空＝这一条不提炼，账本与经验表都不动",
                        )
                        review_distill_btn = gr.Button("✨ 提炼经验", size="sm")

                    # ---- 资源机会（Resource Scout v0.3 → v0.4：资格过滤 + 用户画像
                    #      + 相关性粗筛 + AI 深度筛选） ----
                    with gr.Tab("💡 资源机会"):
                        gr.Markdown("### 💡 资源机会（Resource Scout v0.4）")
                        gr.Markdown("> ℹ️ 复用现有搜索能力（DuckDuckGo + GitHub）执行一组"
                                    "预置查询，发现对学生开发者有价值的**外部资源机会**"
                                    "（比赛 / 黑客松 / 免费额度 / 课程 / 奖学金等），"
                                    "自动粗分类后存入 `opportunities` 表。每条机会带"
                                    "**资格提示**（✅ 可能符合 / ❌ 可能不符 / ❓ 未知，"
                                    "按确定性关键词规则给出），并**结合下方用户画像**"
                                    "判断硬性条件（学校邮箱 / 年龄 / 地区 / 学生类型），"
                                    "`likely_no` 与 `likely_yes` 会附简短原因——"
                                    "**仅用于筛选与展示，绝不自动忽略任何机会**，"
                                    "最终由你人工标记状态（感兴趣 / 忽略 / 收藏）。")
                        # 用户画像设置（v0.3）：编辑后点「保存画像并重算资格」，
                        # 保存即重算全部存量机会的资格提示（用户主动操作，直接写库）
                        profile_init = _app_module.get_user_profile()
                        gr.Markdown("### 👤 用户画像设置")
                        gr.Markdown("> 资格过滤会参考你的画像：机会要求**学校邮箱**但你"
                                    "没有、**年龄门槛**不满足、**限定地区**与你不同、"
                                    "**仅面向某一类学生** → 标为 ❌ 可能不符 并给出原因。"
                                    "默认：**高中生｜无学校邮箱｜中国**。")
                        with gr.Row():
                            opp_profile_student_dd = gr.Dropdown(
                                label="学生类型",
                                choices=[(label, key) for key, label in
                                         _app_module.STUDENT_TYPE_LABELS.items()],
                                value=profile_init["student_type"],
                                interactive=True,
                            )
                            opp_profile_email_cb = gr.Checkbox(
                                label="我有学校/教育邮箱",
                                value=bool(profile_init["has_school_email"]),
                                interactive=True,
                            )
                        with gr.Row():
                            opp_profile_region_tb = gr.Textbox(
                                label="所在地区",
                                value=profile_init["region"] or "中国",
                                placeholder="如：中国 / 美国",
                                interactive=True,
                            )
                            opp_profile_age_nb = gr.Number(
                                label="年龄（选填，用于年龄门槛判断）",
                                value=profile_init["age"], precision=0,
                                interactive=True,
                            )
                        with gr.Row():
                            opp_profile_save_btn = gr.Button(
                                "💾 保存画像并重算资格", variant="secondary", size="sm")
                        opp_profile_status = gr.Markdown(
                            f"当前画像：{_app_module.STUDENT_TYPE_LABELS.get(profile_init['student_type'], profile_init['student_type'])}｜"
                            f"{'有学校邮箱' if profile_init['has_school_email'] else '无学校邮箱'}｜"
                            f"地区 {profile_init['region'] or '中国'}"
                            + (f"｜年龄 {profile_init['age']}"
                               if profile_init.get("age") else ""))
                        gr.Markdown("---")
                        with gr.Row():
                            opportunity_scan_btn = gr.Button(
                                "🔍 立即扫描", variant="primary", size="sm", scale=1)
                            opportunity_refresh_btn = gr.Button(
                                "🔄 刷新列表", size="sm", scale=1)
                        opportunity_scan_status = gr.Markdown("未开始过扫描")
                        # AI 深度筛选（v0.4）：手动触发，只评估前 N 条候选，
                        # LLM 调用放后台线程（不阻塞界面），完成后点刷新查看
                        with gr.Row():
                            opportunity_ai_btn = gr.Button(
                                "🤖 AI 筛选前 5 条", variant="secondary", size="sm")
                            opportunity_ai_hint = gr.Markdown(
                                "> 对「相关性 ≥ 0.3 且资格非可能不符」的未筛机会"
                                "调用模型深度评估（资格/价值/可复用/建议），"
                                "失败单条跳过，绝不自动忽略任何机会。")
                        opportunity_ai_status = gr.Markdown("未运行过 AI 深度筛选")
                        # 相关性重算（v0.4 存量修复）：对全部存量机会按其标题/摘要
                        # 重算相关性粗筛三字段（确定性规则、零 LLM，毫秒级完成），
                        # 修复 v0.4 落库前旧记录默认 0 分/未知导致的「AI 候选为 0」
                        with gr.Row():
                            opportunity_recompute_btn = gr.Button(
                                "🔄 重新计算相关性", variant="secondary", size="sm")
                            opportunity_recompute_hint = gr.Markdown(
                                "> 按**全部存量机会**的标题/摘要重算相关性粗筛结果"
                                "（仅更新相关性三列，不影响资格/状态）；升级前的旧"
                                "记录默认 0 分/未知，重算后才能被 AI 筛选选中。")
                        opportunity_recompute_status = gr.Markdown(
                            "未重算过相关性")
                        # 过期清理（P7 批次 1b）：把报名截止日已过的机会批量置为已忽略，
                        # 让「资源」格与启动对账单不再反复提它们（面板本身照旧全列）
                        with gr.Row():
                            opportunity_cleanup_btn = gr.Button(
                                "🧹 清理过期比赛", variant="secondary", size="sm")
                            opportunity_cleanup_hint = gr.Markdown(
                                "> 把**报名截止日已过**的机会批量置为「已忽略」："
                                "它们不再进「资源」格与启动对账单。"
                                "**点完列表看起来不会变，这是正常的**——本面板从不"
                                "隐藏机会；截止日未知（空）的一律不清。")
                        opportunity_cleanup_status = gr.Markdown("未清理过过期机会")
                        with gr.Row():
                            opportunity_filter_dd = gr.Dropdown(
                                label="🎯 资格筛选",
                                choices=[("全部", "all"),
                                         ("✅ 可能符合", "likely_yes"),
                                         ("❌ 可能不符合", "likely_no"),
                                         ("❓ 未知", "unknown")],
                                value="all", interactive=True,
                            )
                            opportunity_status_dd = gr.Dropdown(
                                label="🗂️ 状态筛选（历史机会）",
                                choices=[("全部", "all")] +
                                        [(label, key) for key, label in
                                         _app_module.STATUS_LABELS.items()],
                                value="all", interactive=True,
                            )
                        opportunity_list_display = gr.Markdown(
                            "暂无资源机会（点击「🔍 立即扫描」从外部发现学生可用的机会）")
                        opportunity_select_dd = gr.Dropdown(
                            label="选择机会（先选择再标记状态）",
                            choices=[], value=None, interactive=True,
                        )
                        with gr.Row():
                            opportunity_interested_btn = gr.Button(
                                "😍 感兴趣", size="sm", scale=1)
                            opportunity_ignored_btn = gr.Button(
                                "🙈 忽略", size="sm", scale=1)
                            opportunity_favorited_btn = gr.Button(
                                "⭐ 收藏", size="sm", scale=1)
                            opportunity_reset_btn = gr.Button(
                                "↩️ 恢复为新", size="sm", scale=1)
                        opportunity_action_status = gr.Markdown("")

                    # ---- 运维辅助：测试运行器 + 资源监控（v0.1） ----
                    with gr.Tab("🧪 测试与资源监控"):
                        gr.Markdown("### 🧪 辅助测试运行器 v0.1")
                        gr.Markdown("> ℹ️ 自动遍历 `tests/test_*.py` 逐个执行（子进程 + "
                                    "超时看门狗 + 失败重试 1 次），结果写入 SQLite 表 "
                                    "`test_runs`；运行在**后台线程**，不会阻塞界面。")
                        with gr.Row():
                            test_run_btn = gr.Button("▶️ 运行全部测试", variant="primary", size="sm")
                            test_refresh_btn = gr.Button("🔄 刷新结果", size="sm")
                        test_run_status = gr.Markdown("未开始过回归")
                        test_runs_display = gr.Markdown("暂无回归记录")

                        gr.Markdown("---")
                        gr.Markdown("### 📊 Agent 资源消耗监控")
                        gr.Markdown("> ℹ️ 记录每次 LLM 调用 / 搜索调用 / 网页抓取的估算 "
                                    "token（字符数 ÷ 4 粗估，**非精确计费**），写入表 "
                                    "`agent_resource_log`；对话与搜索时自动埋点。")
                        resource_refresh_btn = gr.Button("🔄 刷新资源统计", size="sm")
                        resource_display = gr.Markdown("暂无资源统计")

            with gr.Column(scale=4):
                gr.Markdown(
                    '<div class="app-title"><h1>📚 私人学习助手</h1></div>'
                    '<div class="app-subtitle">Agent Runtime · 插件化 · 记忆库 · 工具调用 · 知识图谱 · Daily Agent · Prompt Evolution · Goal System</div>'
                )
                # ---- 会话启动对账单（P4-14 子项 ②；demo.load 链尾填充，无内容时隐藏） ----
                # 放在标题之下、提醒行之上：提醒行是 equal_height=True 的单行通知带，
                # 账单是多行文本，并进那一行会把它拉高、连带挤压提醒本身。
                with gr.Row(visible=False) as session_bill_row:
                    session_bill_display = gr.Markdown("")
                # ---- 主动提醒通知区（ReminderAgent 定时刷新；无提醒时为空白） ----
                with gr.Row(equal_height=True):
                    reminder_notify = gr.Markdown("", scale=4)
                    check_now_btn = gr.Button("🔔 检查提醒", size="sm", scale=1)
                with gr.Row(equal_height=True):
                    mode_select = gr.Dropdown(
                        label="🎯 学习模式",
                        choices=["自动（Supervisor）"] + _app_module.get_all_roles(),
                        value="自动（Supervisor）",
                        scale=3,
                    )
                    summarize_btn = gr.Button("📝 结束学习并总结", variant="secondary", scale=1)
                    clear_btn = gr.Button("🗑️ 清空对话", variant="secondary", scale=1)
                chatbot = gr.Chatbot(height=480)
                with gr.Row():
                    msg_input = gr.Textbox(placeholder="✨ 输入你的问题...", label="", scale=5)
                    send_btn = gr.Button("发送 ✉️", variant="primary", scale=1)
                    save_note_btn = gr.Button("💾 保存为笔记", size="sm", scale=1)

                # ---- 快速保存为笔记（从聊天区一键存入当前目标） ----
                with gr.Accordion("💾 快速保存为笔记", open=False, visible=False) as quick_save_accordion:
                    gr.Markdown("> ℹ️ 将当前对话内容（或手动输入）保存为笔记到指定目标。"
                                "保存后会自动索引到 RAG 向量库，支持语义搜索。")
                    quick_save_goal_dd = gr.Dropdown(
                        label="🎯 目标", choices=[], value=None, interactive=True,
                    )
                    quick_save_title = gr.Textbox(
                        label="📝 笔记标题", placeholder="输入笔记标题",
                    )
                    quick_save_tags = gr.Textbox(
                        label="🏷️ 标签（逗号分隔）", placeholder="如：学习, 重要, 总结",
                    )
                    quick_save_content = gr.Textbox(
                        label="📄 笔记内容", lines=6, placeholder="输入要保存的笔记内容",
                    )
                    quick_save_btn = gr.Button("✅ 保存笔记", variant="primary", size="sm")
                    quick_save_status = gr.Markdown("")

                # ---- Reflection 经验沉淀（从对话提取有效策略） ----
                with gr.Accordion("🧠 经验沉淀（从对话提取有效策略）", open=False):
                    gr.Markdown("> ℹ️ 分析当前会话最近对话，提取可复用的策略、偏好与成功模式；"
                                "确认保存后，后续对话将自动参考相关经验（最多注入 3 条）。")
                    reflect_btn = gr.Button("🧠 沉淀经验", variant="primary", size="sm")
                    reflect_display = gr.Markdown("反思提取的经验建议将显示在此")
                    reflect_save_btn = gr.Button("✅ 确认保存经验", variant="secondary", size="sm")

                    # ---- 经验评分反馈与淘汰 ----
                    gr.Markdown("---")
                    gr.Markdown("### ⭐ 经验评价与淘汰")
                    exp_feedback_display = gr.Markdown(_app_module.build_experience_feedback_view()[0])
                    with gr.Row():
                        exp_select_dropdown = gr.Dropdown(
                            label="选择要评价的经验", choices=[], value=None,
                            interactive=True, scale=4,
                        )
                        exp_refresh_btn = gr.Button("🔄 刷新", size="sm", scale=1)
                    with gr.Row():
                        exp_up_btn = gr.Button("👍 有用（+1）", size="sm", scale=1)
                        exp_down_btn = gr.Button("👎 无用（-1）", size="sm", scale=1)
                    exp_action_status = gr.Markdown("")
                    exp_cleanup_btn = gr.Button("🧹 清理低分经验（≤-3）",
                                                variant="secondary", size="sm")

        with gr.Row(visible=False) as confirm_row:
            gr.Markdown("⚠️ **确定清空当前对话？**", scale=4)
            confirm_yes = gr.Button("✅ 确认", variant="stop", size="sm", scale=1)
            confirm_no = gr.Button("❌ 取消", variant="secondary", size="sm", scale=1)

        # ==================== 事件绑定 ====================
        # 首页那十一格的**唯一一份**输出清单（2026-10-20 阶段二 第一批）：顺序与
        # `home_controller.refresh_home_panel` 的返回值逐格对齐——概览 → 今日（区＋下拉）→
        # 往日（区＋下拉）→ 持续关注 → 决策 → 发现。凡接它的绑定都用这一份，两处各写一遍
        # 迟早漂移（AGENTS.md「两处手工复制保持一致」那一族）。
        _home_panel_outputs = [
            home_summary, home_today_md, home_today_select_dd,
            home_overdue_md, home_overdue_select_dd,
            home_task_cards, home_task_select_dd,
            home_decision_cards, home_decision_select_dd,
            home_opportunity_cards, home_opportunity_select_dd,
        ]

        demo.load(
            fn=_app_module.create_new_session,
            outputs=[current_session, session_dropdown, chatbot, session_info],
        ).then(
            fn=_app_module.load_profile_to_ui,
            outputs=[profile_english, profile_ai, profile_time, profile_style, profile_goal],
        ).then(
            fn=_app_module.refresh_goal_list,
            outputs=[goal_dropdown],
        ).then(
            fn=_app_module.get_skill_display,
            outputs=[skill_display],
        ).then(
            fn=_app_module.refresh_plan_goal_list,
            outputs=[plan_goal_dropdown],
        ).then(
            # 回填提醒时间设置并做首次提醒检查（仅整点匹配时显示，无副作用）
            fn=_app_module.load_reminder_settings,
            outputs=[reminder_times_input, reminder_status],
        ).then(
            fn=_app_module.do_check_reminders,
            outputs=[reminder_notify],
        ).then(
            # 深度搜索：初始化任务列表与保存目标下拉框（无副作用，异常内部兜底）
            fn=_app_module.do_refresh_search_panel,
            inputs=[search_view_task_dd],
            outputs=[search_task_display, search_view_task_dd],
        ).then(
            fn=_app_module.refresh_search_save_goals,
            outputs=[search_save_goal_dd],
        ).then(
            # 待审批列表：页面加载时回填（重启后仍可看到未处理的审批）
            fn=_app_module.refresh_pending_list,
            inputs=[pending_select_dd],
            outputs=[pending_list_display, pending_select_dd],
        ).then(
            # 会话启动对账单（P4-14 子项 ②）：与上面 refresh_pending_list 同族
            # （"重启后仍然要看到没处理完的东西"），不是新生命周期。
            # 读全量 opportunities → build_session_bill（纯函数）→ 文本 + 容器可见性。
            # gr.Row 作为 outputs 合法：实测 gradio 6.24 的
            # Blocks.get_component() 显式接受 (Component, BlockContext)，
            # 故 gr.update(visible=...) 对容器有效，不需要退化为"只置空文本"。
            fn=_app_module.load_session_bill,
            outputs=[session_bill_display, session_bill_row],
        ).then(
            # 复盘账本启动回填（P7 第一闭环 Phase 3c-乙，判据③"未处理项启动时可见"）：
            # 与上面 refresh_pending_list 同族——"重启后仍然要看到没处理完的东西"，
            # 不是新生命周期。列表首行直接写"有几条等你定"。
            fn=_app_module.refresh_review_panel,
            inputs=[review_select_dd],
            outputs=[review_list_display, review_select_dd],
        ).then(
            # 下拉框带着回填后的选中值 → 全文跟着读一次（没选中时返回那行提示，不报错）
            fn=_app_module.do_view_review_decision,
            inputs=[review_select_dd],
            outputs=[review_detail_display],
        ).then(
            # 下次复盘会读到的那一份 State：latest_project_state() **空表返回 None 而不抛**，
            # 这一格必须显示成人读得懂的一句话（"不会踩空，但这一轮不会立决策"）
            fn=_app_module.refresh_review_snapshot,
            outputs=[review_snapshot_display],
        ).then(
            # 首页三区启动回填（P7 阶段二 B1，2026-10-17 判据③「未处理项在启动时
            # 可见」）：与 refresh_pending_list／refresh_review_panel 同族——重启之后
            # 不点刷新也得看见现在等你看的是哪几件。零写、零模型（判据⑤）。
            fn=_app_module.refresh_home_panel,
            outputs=_home_panel_outputs,
        ).then(
            # 首页「🎯 当前重点」启动回填（组 5 子批 A，2026-10-18）：与上面那一步同族
            # ——重启后不点刷新也得看见现在的重点和当下这批候选。这一步**只读**
            # （`refresh_focus_panel` 一行都不写，候选每次现算）。
            fn=_app_module.refresh_focus_panel,
            outputs=[focus_region, focus_manage_dd, focus_candidate_dd],
        )

        new_session_btn.click(fn=_app_module.create_new_session, outputs=[current_session, session_dropdown, chatbot, session_info])
        session_dropdown.change(fn=_app_module.load_session, inputs=[session_dropdown, current_session], outputs=[current_session, chatbot, session_info])
        delete_session_btn.click(fn=_app_module.delete_current_session, inputs=[current_session], outputs=[current_session, chatbot, session_dropdown, session_info])
        rename_input.submit(fn=_app_module.rename_session, inputs=[rename_input, current_session], outputs=[session_dropdown]).then(fn=lambda: "", outputs=[rename_input])

        profile_save_btn.click(fn=_app_module.save_all_profiles, inputs=[profile_english, profile_ai, profile_time, profile_style, profile_goal], outputs=[profile_status, memory_display])
        update_profile_btn.click(fn=_app_module.analyze_and_update_profile, inputs=[chatbot], outputs=[profile_status, memory_display]).then(fn=_app_module.load_profile_to_ui, outputs=[profile_english, profile_ai, profile_time, profile_style, profile_goal])
        refresh_memory_btn.click(fn=_app_module.get_memory_display, outputs=[memory_display])
        refresh_knowledge_btn.click(fn=_app_module.get_knowledge_tree_display, inputs=[knowledge_domain], outputs=[knowledge_tree])

        # 目标管理事件
        create_goal_btn.click(
            fn=_app_module.create_new_goal,
            inputs=[new_goal_title, new_goal_desc],
            outputs=[goal_status, goal_dropdown],
        ).then(fn=lambda: "", outputs=[new_goal_title]).then(fn=lambda: "", outputs=[new_goal_desc])

        goal_dropdown.change(
            fn=_app_module.select_goal,
            inputs=[goal_dropdown, current_session],
            outputs=[goal_status],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        ).then(
            # 同步刷新笔记面板（v3.7.5 笔记系统；失败由回调内部兜底）
            fn=_app_module.do_refresh_notes,
            inputs=[goal_dropdown, note_tag_filter],
            outputs=[notes_list_display, note_view_dropdown],
        )

        # 发送消息
        send_btn.click(
            fn=_app_module.user_message,
            inputs=[msg_input, chatbot, temperature_slider, max_tokens_slider, enable_memory_check],
            outputs=[chatbot, msg_input], queue=False,
        ).then(
            fn=_app_module.bot_response,
            inputs=[chatbot, mode_select, temperature_slider, max_tokens_slider, enable_memory_check, enable_evaluator_check, current_session],
            outputs=[chatbot],
        ).then(
            fn=_app_module.save_current, inputs=[chatbot, current_session], outputs=[current_session]
        )

        msg_input.submit(
            fn=_app_module.user_message,
            inputs=[msg_input, chatbot, temperature_slider, max_tokens_slider, enable_memory_check],
            outputs=[chatbot, msg_input], queue=False,
        ).then(
            fn=_app_module.bot_response,
            inputs=[chatbot, mode_select, temperature_slider, max_tokens_slider, enable_memory_check, enable_evaluator_check, current_session],
            outputs=[chatbot],
        ).then(
            fn=_app_module.save_current, inputs=[chatbot, current_session], outputs=[current_session]
        )

        summarize_btn.click(
            fn=_app_module.manual_summarize,
            inputs=[chatbot, mode_select],
            outputs=[chatbot],
        ).then(
            fn=_app_module.save_current, inputs=[chatbot, current_session], outputs=[current_session]
        ).then(
            fn=_app_module.get_memory_display, outputs=[memory_display],
        )

        export_btn.click(fn=_app_module.do_export, inputs=[chatbot, export_format, current_session], outputs=[export_output])

        clear_btn.click(fn=_app_module.show_confirm, outputs=[confirm_row, confirm_yes, confirm_no])
        confirm_yes.click(fn=_app_module.do_clear, inputs=[current_session], outputs=[chatbot, current_session, confirm_row, confirm_yes, confirm_no])
        confirm_no.click(fn=_app_module.hide_confirm, outputs=[confirm_row, confirm_yes, confirm_no])

        # Daily Agent
        # 旧版那两枚「⚡ 手动生成」的点击绑定已移除，并且置了 `interactive=False`
        # （2026-10-20 阶段二 第一批 spec §3.3：旧版生成停用，历史仍可只读查看）。
        # 两枚「🔄 刷新」照旧接 `refresh_plan`／`refresh_review`，读的还是历史行。
        refresh_plan_btn.click(fn=_app_module.refresh_plan, outputs=[plan_display])
        refresh_review_btn.click(fn=_app_module.refresh_review, outputs=[review_display])

        # Prompt Evolution
        prompt_evaluate_btn.click(fn=_app_module.evaluate_prompt, inputs=[prompt_task_name, prompt_score_slider, prompt_feedback], outputs=[prompt_status, prompt_suggestion_display, prompt_apply_btn])
        prompt_apply_btn.click(fn=_app_module.apply_prompt, inputs=[prompt_task_name, prompt_suggestion_display], outputs=[prompt_status])

        # 复习推荐
        refresh_review_rec_btn.click(fn=_app_module.refresh_review_recommendations, inputs=[review_rec_domain], outputs=[review_rec_display])

        # 想法孵化器
        incubate_btn.click(fn=_app_module.incubate_idea, inputs=[idea_input, idea_mode], outputs=[incubate_result])

        # 学习计划生成器
        generate_study_plan_btn.click(
            fn=_app_module.generate_study_plan,
            inputs=[plan_goal_dropdown, plan_days_slider, plan_save_checkbox],
            outputs=[plan_study_result_display],
        )

        # 概念测验
        generate_quiz_btn.click(
            fn=_app_module.generate_quiz,
            inputs=[quiz_concept_input, quiz_difficulty_radio, quiz_num_slider],
            outputs=[quiz_result_display],
        )

        # 概念讲解
        explain_concept_btn.click(
            fn=_app_module.explain_concept,
            inputs=[explain_concept_input, explain_level_radio],
            outputs=[explain_result_display],
        )

        # 目标详情与任务管理事件
        generate_tasks_btn.click(
            fn=_app_module.do_generate_tasks,
            inputs=[goal_dropdown],
            outputs=[goal_action_status],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        refresh_tasks_btn.click(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        start_task_btn.click(
            fn=_app_module.do_start_task,
            inputs=[complete_task_dropdown, goal_dropdown],
            outputs=[goal_action_status, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        complete_task_btn.click(
            fn=_app_module.do_complete_task,
            inputs=[complete_task_dropdown, goal_dropdown],
            outputs=[goal_action_status, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # 首页准入两枚（P7 阶段二 组 5 子批 B，2026-10-19）：绑定形状逐字镜像上面
        # `start_task_btn`／`complete_task_btn` 那两段——同一个下拉、同一个状态格、
        # 写完之后同一条 `.then` 把详情刷回来（那一行末尾的「🏠」就是这次点击的结果）。
        add_home_btn.click(
            fn=_app_module.do_add_task_home,
            inputs=[complete_task_dropdown, goal_dropdown],
            outputs=[goal_action_status, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        remove_home_btn.click(
            fn=_app_module.do_remove_task_home,
            inputs=[complete_task_dropdown, goal_dropdown],
            outputs=[goal_action_status, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        mark_completed_btn.click(
            fn=_app_module.do_mark_goal_completed,
            inputs=[goal_dropdown],
            outputs=[goal_action_status, goal_dropdown, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        mark_archived_btn.click(
            fn=_app_module.do_mark_goal_archived,
            inputs=[goal_dropdown],
            outputs=[goal_action_status, goal_dropdown, goal_detail],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # Planner 规划助手事件
        staged_plan_btn.click(
            fn=_app_module.do_generate_staged_plan,
            inputs=[goal_dropdown, plan_new_title, plan_new_desc, plan_weeks_slider],
            outputs=[goal_action_status, staged_plan_display, staged_plan_state, goal_dropdown],
        )

        apply_plan_btn.click(
            fn=_app_module.do_apply_staged_plan,
            inputs=[staged_plan_state],
            outputs=[goal_action_status, staged_plan_state],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        conflict_btn.click(
            fn=_app_module.do_analyze_conflicts,
            inputs=[],
            outputs=[goal_action_status, conflict_display, suggestions_state],
        )

        apply_conflicts_btn.click(
            fn=_app_module.do_apply_conflicts,
            inputs=[suggestions_state],
            outputs=[goal_action_status, suggestions_state],
        ).then(
            fn=_app_module.refresh_goal_list,
            outputs=[goal_dropdown],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # Intent 意图识别事件
        intent_analyze_btn.click(
            fn=_app_module.do_analyze_intent,
            inputs=[chatbot, intent_text_input],
            outputs=[goal_action_status, intent_display, intent_state],
        )

        intent_apply_btn.click(
            fn=_app_module.do_apply_intent_suggestion,
            inputs=[intent_state],
            outputs=[goal_action_status, intent_state],
        ).then(
            fn=_app_module.refresh_goal_list,
            outputs=[goal_dropdown],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # Planning Mirror 规划镜像事件
        mirror_snapshot_btn.click(
            fn=_app_module.do_mirror_snapshot,
            inputs=[],
            outputs=[goal_action_status, mirror_snapshot_display],
        )

        mirror_simulate_btn.click(
            fn=_app_module.do_mirror_simulate,
            inputs=[mirror_new_title, mirror_new_desc, mirror_weeks_slider, mirror_priority_radio],
            outputs=[goal_action_status, mirror_impact_display],
        )

        mirror_suggest_btn.click(
            fn=_app_module.do_mirror_suggest,
            inputs=[],
            outputs=[goal_action_status, mirror_suggest_display, mirror_suggestions_state],
        )

        mirror_apply_btn.click(
            fn=_app_module.do_apply_mirror_suggestions,
            inputs=[mirror_suggestions_state],
            outputs=[goal_action_status, mirror_suggestions_state],
        ).then(
            fn=_app_module.refresh_goal_list,
            outputs=[goal_dropdown],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # Daily Planner 每日规划事件
        daily_plan_btn.click(
            fn=_app_module.do_generate_daily_plan,
            inputs=[],
            outputs=[goal_action_status, daily_plan_display, daily_plan_state],
        )

        daily_plan_apply_btn.click(
            fn=_app_module.do_apply_daily_plan,
            inputs=[daily_plan_state],
            outputs=[goal_action_status, daily_plan_state],
        ).then(
            fn=_app_module.refresh_goal_detail,
            inputs=[goal_dropdown],
            outputs=[goal_detail, task_choices_state, complete_task_dropdown],
        )

        # 任务执行自动化事件（执行 → 审批 → 继续；State 持有暂停中的生成器，
        # 执行结束时回调返回 None 清空 State，防止对已结束生成器重复恢复）
        execute_plan_btn.click(
            fn=_app_module.do_execute_plan,
            inputs=[daily_plan_state],
            outputs=[goal_action_status, exec_display, exec_audio_slot, execution_gen_state],
        )

        exec_approve_btn.click(
            fn=_app_module.do_approve_pending,
            inputs=[execution_gen_state],
            outputs=[goal_action_status, exec_display, exec_audio_slot, execution_gen_state],
        ).then(
            # 内存流审批后同步刷新待审批列表（记录状态已更新/新增暂停点）
            fn=_app_module.refresh_pending_list,
            inputs=[pending_select_dd],
            outputs=[pending_list_display, pending_select_dd],
        )

        exec_reject_btn.click(
            fn=_app_module.do_reject_pending,
            inputs=[execution_gen_state],
            outputs=[goal_action_status, exec_display, exec_audio_slot, execution_gen_state],
        ).then(
            fn=_app_module.refresh_pending_list,
            inputs=[pending_select_dd],
            outputs=[pending_list_display, pending_select_dd],
        )

        # 待审批持久化列表事件（重启后仍可处理未完成的审批）
        pending_refresh_btn.click(
            fn=_app_module.refresh_pending_list,
            inputs=[pending_select_dd],
            outputs=[pending_list_display, pending_select_dd],
        )

        pending_select_dd.change(
            fn=_app_module.view_pending,
            inputs=[pending_select_dd],
            outputs=[pending_display],
        )

        pending_approve_btn.click(
            fn=_app_module.approve_pending_from_db,
            inputs=[pending_select_dd],
            outputs=[pending_status, pending_list_display, pending_select_dd, pending_display],
        )

        pending_reject_btn.click(
            fn=_app_module.reject_pending_from_db,
            inputs=[pending_select_dd],
            outputs=[pending_status, pending_list_display, pending_select_dd, pending_display],
        )

        # 笔记系统事件（列表刷新 / 关键词搜索 / 选中查看内容）
        notes_refresh_btn.click(
            fn=_app_module.do_refresh_notes,
            inputs=[goal_dropdown, note_tag_filter],
            outputs=[notes_list_display, note_view_dropdown],
        )

        note_search_btn.click(
            fn=_app_module.do_search_notes,
            inputs=[goal_dropdown, note_search_input, note_search_mode, note_tag_filter],
            outputs=[notes_list_display, note_view_dropdown],
        )

        note_view_dropdown.change(
            fn=_app_module.do_show_note,
            inputs=[note_view_dropdown],
            outputs=[note_content_display],
        )

        # 快速保存为笔记事件（显示表单 → 保存 → 刷新面板）
        save_note_btn.click(
            fn=_app_module.show_quick_save,
            inputs=[goal_dropdown, chatbot],
            outputs=[quick_save_accordion, quick_save_goal_dd,
                     quick_save_title, quick_save_tags, quick_save_content,
                     quick_save_status],
        )

        quick_save_btn.click(
            fn=_app_module.do_save_note_from_chat,
            inputs=[quick_save_goal_dd, quick_save_title, quick_save_tags,
                    quick_save_content],
            outputs=[quick_save_status, notes_list_display, note_view_dropdown,
                     quick_save_accordion, quick_save_title, quick_save_tags,
                     quick_save_content],
        )

        # Reflection 经验沉淀事件
        reflect_btn.click(
            fn=_app_module.do_reflect_experiences,
            inputs=[chatbot],
            outputs=[reflect_display, experiences_state],
        )

        reflect_save_btn.click(
            fn=_app_module.do_save_experiences,
            inputs=[experiences_state, current_session],
            outputs=[reflect_display, experiences_state],
        ).then(
            fn=_app_module.refresh_exp_panel,
            inputs=[],
            outputs=[exp_feedback_display, exp_select_dropdown],
        )

        # 经验评分反馈与淘汰事件（👍/👎 按钮防抖：点击先禁用，回调完成后经 .then 链恢复）
        exp_refresh_btn.click(
            fn=_app_module.refresh_exp_panel,
            inputs=[],
            outputs=[exp_feedback_display, exp_select_dropdown],
        )

        exp_up_btn.click(
            fn=_app_module.disable_feedback_buttons,
            inputs=[],
            outputs=[exp_up_btn, exp_down_btn],
            queue=False,  # 禁用步骤立即执行，不排队
        ).then(
            fn=lambda exp_id: _app_module.do_exp_feedback(exp_id, "up"),
            inputs=[exp_select_dropdown],
            outputs=[exp_action_status, exp_feedback_display, exp_select_dropdown],
        ).then(
            fn=_app_module.enable_feedback_buttons,
            inputs=[],
            outputs=[exp_up_btn, exp_down_btn],
        )

        exp_down_btn.click(
            fn=_app_module.disable_feedback_buttons,
            inputs=[],
            outputs=[exp_up_btn, exp_down_btn],
            queue=False,  # 禁用步骤立即执行，不排队
        ).then(
            fn=lambda exp_id: _app_module.do_exp_feedback(exp_id, "down"),
            inputs=[exp_select_dropdown],
            outputs=[exp_action_status, exp_feedback_display, exp_select_dropdown],
        ).then(
            fn=_app_module.enable_feedback_buttons,
            inputs=[],
            outputs=[exp_up_btn, exp_down_btn],
        )

        exp_cleanup_btn.click(
            fn=_app_module.do_cleanup_low_experiences,
            inputs=[],
            outputs=[exp_action_status, exp_feedback_display, exp_select_dropdown],
        )

        # Reminder 主动提醒事件
        check_now_btn.click(
            fn=_app_module.do_check_reminders,
            inputs=[],
            outputs=[reminder_notify],
        )

        reminder_save_btn.click(
            fn=_app_module.do_save_reminder_times,
            inputs=[reminder_times_input],
            outputs=[reminder_status, reminder_times_input],
        )

        # Information Engine（深度搜索 V0.1）事件：开始搜索 → 刷新任务列表；
        # 选择任务 → 展示证据 + 填充勾选框；确认保存 → 写入笔记系统。
        search_start_btn.click(
            fn=_app_module.do_start_search,
            inputs=[search_query_input],
            outputs=[search_status, search_query_input, search_evidence_select],
        ).then(
            fn=_app_module.do_refresh_search_panel,
            inputs=[search_view_task_dd],
            outputs=[search_task_display, search_view_task_dd],
        )

        refresh_search_btn.click(
            fn=_app_module.do_refresh_search_panel,
            inputs=[search_view_task_dd],
            outputs=[search_task_display, search_view_task_dd],
        )

        search_view_task_dd.change(
            fn=_app_module.do_view_search_task,
            inputs=[search_view_task_dd],
            outputs=[search_result_display, search_evidence_select],
        )

        search_save_btn.click(
            fn=_app_module.do_save_search_results,
            inputs=[search_view_task_dd, search_save_goal_dd, search_evidence_select],
            outputs=[search_save_status],
        )

        # 删除任务（先确认再执行：不可逆，级联删除该任务全部证据；删除后刷新任务
        # 列表并清空结果预览与证据勾选框）
        search_delete_btn.click(
            fn=_app_module.show_search_delete_confirm,
            inputs=[],
            outputs=[search_delete_confirm_row, search_delete_yes, search_delete_no],
        )

        search_delete_yes.click(
            fn=_app_module.do_delete_search_task,
            inputs=[search_view_task_dd],
            outputs=[search_delete_status, search_task_display, search_view_task_dd,
                     search_result_display, search_evidence_select,
                     search_delete_confirm_row, search_delete_yes, search_delete_no],
        )

        search_delete_no.click(
            fn=_app_module.hide_search_delete_confirm,
            inputs=[],
            outputs=[search_delete_confirm_row, search_delete_yes, search_delete_no],
        )

        # ---- 定时器：优先使用 Gradio 内置 gr.Timer（5.x+ 支持，当前 6.24.0 已验证）----
        # 每 60 秒 tick 一次 → 调用 do_check_reminders 刷新通知区域；
        # 同一分钟内的重复触发由 ReminderAgent 内部去重，不会重复提醒。
        HAS_GR_TIMER = hasattr(gr, "Timer")
        if HAS_GR_TIMER:
            reminder_timer = gr.Timer(60)  # 参数为秒
            reminder_timer.tick(
                fn=_app_module.do_check_reminders,
                inputs=[],
                outputs=[reminder_notify],
            )

        # 深度搜索任务状态自动刷新（每 5 秒；任务完成/失败后任务列表自动更新，
        # 无需手动点击刷新；保留当前选中的任务）
        if HAS_GR_TIMER:
            search_timer = gr.Timer(5)
            search_timer.tick(
                fn=_app_module.do_refresh_search_panel,
                inputs=[search_view_task_dd],
                outputs=[search_task_display, search_view_task_dd],
            )

        # ---- 任务模板（一键复用，v3.8 新增）事件 ----
        template_refresh_btn.click(
            fn=_app_module.refresh_template_panel,
            inputs=[template_select_dd],
            outputs=[template_list_display, template_select_dd],
        ).then(
            # 产物区跟着一起刷新（只读，无副作用）
            fn=_app_module.do_load_template_notes,
            inputs=[template_select_dd],
            outputs=[template_notes_display, template_notes_dd],
        )

        # 选中模板 → 回填表单进入编辑模式（表单 9 个组件的顺序与返回元组一致，
        # 动作类型排在**最后**，口径见 `_template_form_defaults` 的 docstring），
        # 随后刷新该模板的产物列表
        template_select_dd.change(
            fn=_app_module.load_template_to_form,
            inputs=[template_select_dd],
            outputs=[template_id_state, template_name_input, template_desc_input,
                     template_trigger_radio, template_time_input, template_weekday_dd,
                     template_query_input, template_auto_approve_cb,
                     template_action_dd],
        ).then(
            fn=_app_module.do_load_template_notes,
            inputs=[template_select_dd],
            outputs=[template_notes_display, template_notes_dd],
        )

        # 选中产物 → 显示正文全文（只读）
        template_notes_dd.change(
            fn=_app_module.do_view_template_note,
            inputs=[template_notes_dd],
            outputs=[template_note_view],
        )

        template_save_btn.click(
            fn=_app_module.do_save_template,
            inputs=[template_name_input, template_desc_input, template_trigger_radio,
                    template_time_input, template_weekday_dd, template_query_input,
                    template_auto_approve_cb, template_id_state, template_action_dd],
            outputs=[template_action_status, template_list_display, template_select_dd,
                     template_id_state, template_name_input, template_desc_input,
                     template_trigger_radio, template_time_input, template_weekday_dd,
                     template_query_input, template_auto_approve_cb,
                     template_action_dd],
        )

        template_cancel_btn.click(
            fn=lambda: _template_form_defaults(),
            inputs=[],
            outputs=[template_id_state, template_name_input, template_desc_input,
                     template_trigger_radio, template_time_input, template_weekday_dd,
                     template_query_input, template_auto_approve_cb,
                     template_action_dd],
        )

        # 手动运行模板：执行 + 审批流程（与「执行计划」同一套生成器推进逻辑）；
        # 执行结束/审批暂停/审批落盘后都刷新列表（更新上次运行时间）
        template_run_btn.click(
            fn=_app_module.do_run_template,
            inputs=[template_select_dd],
            outputs=[template_action_status, template_exec_display,
                     template_exec_audio, template_exec_state],
        ).then(
            fn=_app_module.refresh_template_panel,
            inputs=[template_select_dd],
            outputs=[template_list_display, template_select_dd],
        )

        template_approve_btn.click(
            fn=_app_module.do_approve_template_pending,
            inputs=[template_exec_state],
            outputs=[template_action_status, template_exec_display,
                     template_exec_audio, template_exec_state],
        ).then(
            fn=_app_module.refresh_template_panel,
            inputs=[template_select_dd],
            outputs=[template_list_display, template_select_dd],
        ).then(
            # 批准落盘后产物才有内容 → 立即重列（这是 1c-③ 补的那个入口）
            fn=_app_module.do_load_template_notes,
            inputs=[template_select_dd],
            outputs=[template_notes_display, template_notes_dd],
        )

        template_reject_btn.click(
            fn=_app_module.do_reject_template_pending,
            inputs=[template_exec_state],
            outputs=[template_action_status, template_exec_display,
                     template_exec_audio, template_exec_state],
        ).then(
            fn=_app_module.refresh_template_panel,
            inputs=[template_select_dd],
            outputs=[template_list_display, template_select_dd],
        )

        template_toggle_btn.click(
            fn=_app_module.do_toggle_template,
            inputs=[template_select_dd],
            outputs=[template_action_status, template_list_display, template_select_dd],
        )

        template_delete_btn.click(
            fn=_app_module.do_delete_template,
            inputs=[template_select_dd],
            outputs=[template_action_status, template_list_display, template_select_dd],
        )

        # ---- 项目复盘（P7 第一闭环 Phase 3c-乙）事件 ----
        # 三个写按钮的 outputs 顺序完全一致：(状态, 列表, 下拉框, 全文)——
        # 同一个控制器契约（`_after_write`）的三个出口共用一条返回元组，
        # 这样任何一处绑错都会在"状态行跑到列表那一格里"这种一眼可见的形态上失败。
        review_refresh_btn.click(
            fn=_app_module.refresh_review_panel,
            inputs=[review_select_dd],
            outputs=[review_list_display, review_select_dd],
        ).then(
            # 顺手重读"下次复盘会读到的那份快照"（只读，空表也要显示成人读得懂的一句话）
            fn=_app_module.refresh_review_snapshot,
            inputs=[],
            outputs=[review_snapshot_display],
        )

        # 选中一条决策 → 全文回读（只读，原文不截断）
        review_select_dd.change(
            fn=_app_module.do_view_review_decision,
            inputs=[review_select_dd],
            outputs=[review_detail_display],
        )

        review_guide_btn.click(
            fn=_app_module.do_run_review_now,
            inputs=[],
            outputs=[review_guide_status],
        )

        # 人工段落盘：只 build＋save 一份新快照，**不触发复盘**（触发口径唯一，理由见
        # review_controller.do_save_manual_section 与上面的 do_run_review_now）。
        # 保存完顺手重读「下次复盘会读到的那份快照」——复用同一个渲染函数，
        # 不在这里再算一遍闸门预判（两份判据一旦分叉，界面上那句预判就成了假话）。
        # 刻意不用 .then 清空输入框：这一格是一整段话，写失败时抹掉比「多点一次」更难恢复
        # （口径同 4a 的「评估」「提炼」那两格）。
        review_save_manual_btn.click(
            fn=_app_module.do_save_manual_section,
            inputs=[review_manual_input],
            outputs=[review_manual_status],
        ).then(
            fn=_app_module.refresh_review_snapshot,
            inputs=[],
            outputs=[review_snapshot_display],
        )

        review_discuss_btn.click(
            fn=_app_module.do_review_discuss,
            inputs=[review_select_dd, review_comment_input, review_prior_intent_input],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        ).then(
            # 写完清空两格输入：同一句话点两次会在账本上留下两条 user_report，
            # 而"他说过两遍"与"我们记了两遍"在复盘时是两件事。清空是这条路径上
            # 最便宜的去重——真正的去重（同内容不许重复登记）没有做，理由见
            # review_controller.do_review_discuss 的 docstring。
            fn=lambda: ("", ""),
            inputs=[],
            outputs=[review_comment_input, review_prior_intent_input],
        )

        review_approve_btn.click(
            fn=_app_module.do_review_approve,
            inputs=[review_select_dd],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        )

        # 否决：和「批准」「标记已执行」同一形状——只喂一个 id、只让状态机判能不能走。
        # 它不是删除按钮（账本不删行），存在的理由是「用户要能说『不』」。
        review_reject_btn.click(
            fn=_app_module.do_review_reject,
            inputs=[review_select_dd],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        )

        review_executed_btn.click(
            fn=_app_module.do_review_mark_executed,
            inputs=[review_select_dd, review_exec_ref_input],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        )

        # 评估与提炼：与上面三个写按钮共用同一条返回元组（状态, 列表, 下拉框, 全文），
        # 绑错会在「状态行跑到列表那一格里」这种一眼可见的形态上失败。
        review_evaluate_btn.click(
            fn=_app_module.do_review_evaluate,
            inputs=[review_select_dd, review_evaluation_dd, review_actual_result_input],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        )

        review_distill_btn.click(
            fn=_app_module.do_review_distill,
            inputs=[review_select_dd, review_future_effect_input],
            outputs=[review_action_status, review_list_display, review_select_dd,
                     review_detail_display],
        )

        # ---- 资源机会（Resource Scout v0.4：资格/画像 + 相关性/AI 筛选）事件 ----
        # 保存画像并重算资格：用户主动操作，直接写 user_profile 表，
        # 随后重算全部存量机会并刷新列表（输出三件套：状态 + 列表 + 下拉框）
        opp_profile_save_btn.click(
            fn=_app_module.do_save_user_profile,
            inputs=[opp_profile_student_dd, opp_profile_email_cb,
                    opp_profile_region_tb, opp_profile_age_nb],
            outputs=[opp_profile_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        # 扫描：后台线程执行（联网不阻塞界面），完成后点「🔄 刷新列表」查看
        opportunity_scan_btn.click(
            fn=_app_module.do_scan_opportunities,
            outputs=[opportunity_scan_status],
        )
        # AI 深度筛选（v0.4）：后台线程执行 LLM 调用（不阻塞界面），
        # 只评估前 N 条候选；完成后机会列表**自动刷新**（见下方 ai_filter_timer）
        opportunity_ai_btn.click(
            fn=_app_module.do_ai_filter_opportunities,
            outputs=[opportunity_ai_status],
        )
        # AI 筛选完成自动刷新（v0.4 修复）：后台线程写 ai_filter_state 后，
        # 本 Timer 每 5 秒轮询一次，检测到「本轮结束且结果未展示」即自动刷新
        # 机会列表（展示 ai_* 评估结果 + 失败原因），无需手动点「🔄 刷新列表」
        if HAS_GR_TIMER:
            ai_filter_timer = gr.Timer(5)
            ai_filter_timer.tick(
                fn=_app_module.poll_ai_filter_results,
                inputs=[opportunity_filter_dd, opportunity_status_dd],
                outputs=[opportunity_ai_status, opportunity_list_display,
                         opportunity_select_dd],
            )
        # 重新计算相关性（v0.4 存量修复）：确定性规则毫秒级完成，同步执行后
        # 立即刷新列表（保持当前资格 + 状态筛选视图），输出三件套
        opportunity_recompute_btn.click(
            fn=_app_module.do_recompute_relevance,
            inputs=[opportunity_filter_dd, opportunity_status_dd],
            outputs=[opportunity_recompute_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        # 过期清理（P7 批次 1b）：一条确定性 UPDATE，同步执行后立即刷新列表
        # （同样保持当前资格 + 状态筛选视图，输出也是三件套）
        opportunity_cleanup_btn.click(
            fn=_app_module.do_cleanup_overdue,
            inputs=[opportunity_filter_dd, opportunity_status_dd],
            outputs=[opportunity_cleanup_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        # 刷新：携带资格 + 状态筛选值（筛选改变或扫描完成后点击刷新均保持当前筛选）
        opportunity_refresh_btn.click(
            fn=_app_module.refresh_opportunities_panel,
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_list_display, opportunity_select_dd],
        )
        # 资格筛选下拉变化 → 立即按筛选刷新列表（保持状态筛选）
        opportunity_filter_dd.change(
            fn=_app_module.refresh_opportunities_panel,
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_list_display, opportunity_select_dd],
        )
        # 状态筛选下拉变化（v0.4 历史机会查看）→ 立即刷新列表（保持资格筛选）
        opportunity_status_dd.change(
            fn=_app_module.refresh_opportunities_panel,
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_list_display, opportunity_select_dd],
        )
        # 状态标记：四个按钮复用同一回调，仅状态参数不同（人工确认后写入数据库）；
        # 同时传入当前资格 + 状态筛选，标记后仍停留在同一筛选视图
        # （若机会因此离开当前状态视图，则列表自动移除该行并提示）
        opportunity_interested_btn.click(
            fn=lambda oid, flt, st: _app_module.do_set_opportunity_status(
                oid, _app_module.OpportunityStatus.INTERESTED, flt, st),
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_action_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        opportunity_ignored_btn.click(
            fn=lambda oid, flt, st: _app_module.do_set_opportunity_status(
                oid, _app_module.OpportunityStatus.IGNORED, flt, st),
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_action_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        opportunity_favorited_btn.click(
            fn=lambda oid, flt, st: _app_module.do_set_opportunity_status(
                oid, _app_module.OpportunityStatus.FAVORITED, flt, st),
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_action_status, opportunity_list_display,
                     opportunity_select_dd],
        )
        opportunity_reset_btn.click(
            fn=lambda oid, flt, st: _app_module.do_set_opportunity_status(
                oid, _app_module.OpportunityStatus.NEW, flt, st),
            inputs=[opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[opportunity_action_status, opportunity_list_display,
                     opportunity_select_dd],
        )

        # 辅助测试运行器 & 资源监控（v0.1）：运行在后台线程（不阻塞事件队列），
        # 结果写入 test_runs / agent_resource_log 表，界面用「刷新」按钮读取
        test_run_btn.click(
            fn=_app_module.do_start_test_run,
            outputs=[test_run_status],
        )
        test_refresh_btn.click(
            fn=_app_module.do_refresh_test_runs,
            outputs=[test_runs_display],
        )
        resource_refresh_btn.click(
            fn=_app_module.do_refresh_resource_stats,
            outputs=[resource_display],
        )

        # 任务模板定时调度（每 60 秒检查一次到期的每日/每周模板；执行放在独立
        # 后台线程，不阻塞事件队列；防重入由 check_scheduled_templates 内部
        # 「先更新 last_run_at 再执行」保证，同一周期不会重复触发）
        if HAS_GR_TIMER:
            template_timer = gr.Timer(60)
            template_timer.tick(
                fn=_app_module.do_check_scheduled_templates,
                inputs=[],
                outputs=[template_schedule_status],
            )

        # ==================== 🏠 首页（P7 阶段二 B1，2026-10-17）====================
        # 三个跳转按钮接的全是**既有读取回调**，首页一条写回调都不接（判据⑤）：
        # 决策那一格后面跟的 `.then(do_view_review_decision, …)` 与上面
        # `review_select_dd.change` 用的是同一条回调、同一对组件，一字不改（§四）。
        # 跳转也不刷首页自己：读操作不改变账本，刷了只会把人刚选中的那张卡清空。
        home_refresh_btn.click(
            fn=_app_module.refresh_home_panel,
            outputs=_home_panel_outputs,
        ).then(
            # 「🔄 刷新首页」那一下也刷「当前重点」：`refresh_focus_panel` 只读，
            # 但它要现算候选——上一批候选可能已经被采纳或过期了。
            fn=_app_module.refresh_focus_panel,
            outputs=[focus_region, focus_manage_dd, focus_candidate_dd],
        )
        home_decision_jump_btn.click(
            fn=_app_module.do_home_jump_decision,
            inputs=[home_decision_select_dd],
            outputs=[home_status, review_list_display, review_select_dd],
        ).then(
            fn=_app_module.do_view_review_decision,
            inputs=[review_select_dd],
            outputs=[review_detail_display],
        )
        home_task_jump_btn.click(
            fn=_app_module.do_home_jump_task,
            inputs=[home_task_select_dd],
            outputs=[home_status, goal_dropdown, goal_detail,
                     task_choices_state, complete_task_dropdown],
        )
        home_opportunity_jump_btn.click(
            fn=_app_module.do_home_jump_opportunity,
            inputs=[home_opportunity_select_dd, opportunity_filter_dd,
                    opportunity_status_dd],
            outputs=[home_status, opportunity_list_display, opportunity_select_dd],
        )

        # -------------------- 🏠 首页 · 🎯 当前重点（写口）--------------------
        # 上面那一段（三区＋三个跳转）一条写回调都不接，B1 判据⑤ 与它的用例一字未动；
        # **从这里开始才是首页上唯一会写库的地方**，写全部走 `ui/controllers/focus_controller.py`
        # 的 `do_focus_*`（2026-10-18 设计席定案「写口放新模块」）。
        # 那个测试切片的边界就是下面这行 marker 注释——`tests/test_home_dashboard.py`
        # 里「除这一格外不许出现写回调」那条用例按它把这一格单独圈出来，
        # 改了这行字要同时改那条用例。
        _focus_write_outputs = [focus_status, focus_region, focus_manage_dd,
                                focus_candidate_dd, focus_preview_md,
                                focus_preview_state]
        # 选中一条 → 把它的原话装进那两个输入框（只读，写要等「保存」那一下）
        focus_manage_dd.change(
            fn=_app_module.do_focus_select,
            inputs=[focus_manage_dd],
            outputs=[focus_title_box, focus_desc_box],
        )
        focus_add_btn.click(
            fn=_app_module.do_focus_add,
            inputs=[focus_title_box, focus_desc_box],
            outputs=_focus_write_outputs,
        )
        focus_save_btn.click(
            fn=_app_module.do_focus_update,
            inputs=[focus_manage_dd, focus_title_box, focus_desc_box],
            outputs=_focus_write_outputs,
        )
        focus_pause_btn.click(
            fn=_app_module.do_focus_pause,
            inputs=[focus_manage_dd],
            outputs=_focus_write_outputs,
        )
        focus_complete_btn.click(
            fn=_app_module.do_focus_complete,
            inputs=[focus_manage_dd],
            outputs=_focus_write_outputs,
        )
        # 说一句人话 → 先只出预览（`do_focus_preview` 一行都不写），
        # 预览存进 `focus_preview_state`，写入只发生在「✅ 按预览写入」那一下
        focus_preview_btn.click(
            fn=_app_module.do_focus_preview,
            inputs=[focus_say_box],
            outputs=[focus_preview_md, focus_preview_state],
        )
        focus_confirm_btn.click(
            fn=_app_module.do_focus_confirm,
            inputs=[focus_preview_state],
            outputs=_focus_write_outputs,
        )
        focus_clear_btn.click(
            fn=_app_module.do_focus_clear,
            inputs=[],
            outputs=_focus_write_outputs,
        )
        focus_adopt_btn.click(
            fn=_app_module.do_focus_adopt,
            inputs=[focus_candidate_dd],
            outputs=_focus_write_outputs,
        )
        focus_ignore_btn.click(
            fn=_app_module.do_focus_ignore,
            inputs=[focus_candidate_dd],
            outputs=_focus_write_outputs,
        )

        # -------------------- 🏠 首页 · 📅 今日安排（写口：只有两列）--------------------
        # 这是首页上第二个会写库的地方（2026-10-20 阶段二 第一批）。`CORE_LOOP_BATCH1_SPEC.md`
        # §1.4 在锚点 §一.11「首页零写」上开了**唯一例外**，放开的只有这两列：`goal_tasks.status`
        # （▶️ 开始／✅ 完成）与 `goal_tasks.planned_date`（📌 安排到今天）。其余照旧零写，
        # 由 `tests/test_home_dashboard.py` 那条按两枚 marker 切片钉住：这一格里的首页写回调
        # 只许 `do_home_start_task`／`do_home_complete_task`／`do_home_reschedule_today`
        # 这三枚，不许借道目标页的 `do_start_task`／`do_complete_task`／`do_add_task_home`——
        # 借道就会连 `is_home` 一起写，而那不在本批放开的两列里。
        home_today_start_btn.click(
            fn=_app_module.do_home_start_task,
            inputs=[home_today_select_dd],
            outputs=_home_panel_outputs,
        )
        home_today_complete_btn.click(
            fn=_app_module.do_home_complete_task,
            inputs=[home_today_select_dd],
            outputs=_home_panel_outputs,
        )
        home_reschedule_btn.click(
            fn=_app_module.do_home_reschedule_today,
            inputs=[home_overdue_select_dd],
            outputs=_home_panel_outputs,
        )
        # 「⚡ 帮我排今天」不新建一条生成链路：它复用目标页那条新版链路
        # `do_generate_daily_plan` → `do_apply_daily_plan`（预览只分析、确认才写 planned_date），
        # 读的也是同一个 `daily_plan_state`——两处预览不会各说各话。
        # 「🔁 按此时长重新生成预览」与它的差别只在有没有把那只数字框传进去（spec §2 第 2、3 步）。
        _home_plan_preview_outputs = [home_status, home_plan_preview_md, daily_plan_state]
        home_plan_btn.click(
            fn=_app_module.do_generate_daily_plan,
            inputs=[home_plan_hours],
            outputs=_home_plan_preview_outputs,
        )
        home_plan_regen_btn.click(
            fn=_app_module.do_generate_daily_plan,
            inputs=[home_plan_hours],
            outputs=_home_plan_preview_outputs,
        )
        home_plan_apply_btn.click(
            fn=_app_module.do_apply_daily_plan,
            inputs=[daily_plan_state],
            outputs=[home_status, daily_plan_state],
        ).then(
            # 应用之后立刻刷首页：今日区那批任务要当场出现（spec §2 第 5 步）。
            fn=_app_module.refresh_home_panel,
            outputs=_home_panel_outputs,
        )

    return demo

