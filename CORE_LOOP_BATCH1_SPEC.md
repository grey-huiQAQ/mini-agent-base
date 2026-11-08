# 第一批实施规格：把「今日工作流」做顺、做真

> 阶段：范围收敛后的实施规格（交执行席，已按 2026-10-20 产品裁定定稿）。本规格只覆盖"打开首页 → 看到今日安排 → 开始/完成/改期"这一条主流程，**不覆盖**智能优先级、冲突分析、自动执行学习/代码、跨会话恢复细节、经验积累。
> 依据：源码（符号名＋文件，不写行号）＋生产库只读读数（以复跑为准）。已按审查结论修订三处矛盾、落实五项产品取舍与 D1/R1–R7 裁定。

---

## 0. 前置核实结论（写进规格，防止执行席误报）

1. **新版 `daily_planning_agent` 不读 `current_focus`**（已核实）：`agents/daily_planning_agent.py::_collect_candidates` 只读 `goal_manager.list_goals(status="active")` × `goal_manager.get_tasks(goal_id)`，排序键 = `(任务 priority, 目标 deadline, 目标 priority, task_id)`。**当前重点没有进规划排序**。首页把当前重点显示成标题，不代表规划算法用它做了决定。
2. **时间约束现状**（已核实）：规划读 `profile.daily_time`（`DailyPlannerAgent._get_daily_hours`），生产库 `profile` 无该 key ⇒ 回落常量 `2.0` 小时；单任务耗时是 `estimate_task_hours` 的启发式（基础 2h、标题≥15 字 +0.5h、上限 3h）。**没有**"当天实际可支配时间"、没有学业日程表。本批只做"缺值提示＋本次调整"，不新建设置体系、不把默认 2 小时当成用户确认过的时间。
3. **生产库迁移已就绪**（已核实，本次复跑）：`current_focus` 表在；`goal_tasks` 11 列含 `planned_date` 与 `is_home`；`is_home` 10 行全为 1（一次性回填已跑过）；`planned_date` 0/10 有值。⇒ "生产启动会不会缺列"的担心已解除：`init_db()` 幂等，两列都已存在。本批读路径照旧用 `.get(...)` 容错即可，不需新迁移。

---

## 1. 首页信息结构与任务筛选规则（含去重）

### 1.1 首页自上而下（改动后的最终形状）

1. 概览行（现有 `home_summary`，文案微调，计数口径见 1.3）
2. **「🎯 当前重点」区**（现有，`focus_controller`，一字不动）
3. **「📅 今日安排」区（本批新增，第一批核心）**
   - 主列表 = **今日任务**
   - 尾部子分组 = **往日未完成，待重新安排**
   - 空态 + 「⚡ 帮我排今天」按钮（复用新版生成/预览/确认，见 §2）
4. **「🔄 持续关注」区**（原「最近创建的待办」降级改名，筛选与去重见 1.3）
5. 「🧭 等你定的决策」区（现有，不动）
6. 「💡 还没处理的新发现」区（现有，不动）

### 1.2 三组任务的筛选（口径，逐字）

记 `today = 今天(YYYY-MM-DD)`，`active_goal` = `goals.status == 'active'` 的目标。

- **今日任务 `T_today`**：`goal ∈ active_goal` ∧ `task.planned_date == today` ∧ `task.status != 'done'`。**不使用 `is_home` 做筛选**（矛盾 1 已修正）。
- **往日未完成 `T_overdue`**：`goal ∈ active_goal` ∧ `task.planned_date` 非空 ∧ `task.planned_date < today`（字符串序，`YYYY-MM-DD` 定宽可直接比）∧ `task.status != 'done'`。`planned_date` 为 NULL/空串**不属于**这组（那叫"从未排期"，归持续关注）。
- **持续关注 `T_watch`**：`goal ∈ active_goal` ∧ `int(task.is_home or 0) != 0` ∧ `task.status ∈ {pending, in_progress}`，**再减去** `T_today ∪ T_overdue`（按 `task.id` 去重）。

**筛选落在哪一层（D1 已定：方案 B，限定范围）**：
- 「今日」「往日」的**数据查询**下沉到 `core/goal_manager.py`，作为唯一共享实现；首页与 `reminder_agent` 都调它，不再各自手写同一套筛选。
- 「持续关注」的**数据筛选**复用同一查询能力（active 目标下未完成任务枚举），但 `is_home` 过滤、去重、focus 加分排序、分组呈现这些 **UI 层逻辑留在 `home_controller.py`**，不搬进 core。
- 本批只下沉当前工作流必需的两个查询＋一个通用枚举，**不借此重构整个 `GoalManager` 或 reminder 子系统**。

具体符号（执行席落点，签名以现读 `core/goal_manager.py` 为准）：
- `GoalManager.list_active_pending_tasks() -> List[Dict]`：active 目标 × `status != 'done'` 的全部任务行，每行附 `goal_id`/`goal_title`（供持续关注复用 + 供下面两查询内部使用）。
- `GoalManager.get_tasks_planned_for(plan_date: str) -> List[Dict]`：`list_active_pending_tasks()` 里 `planned_date == plan_date` 的子集（今日任务 & `reminder_agent` 共用）。
- `GoalManager.get_overdue_tasks(today: str) -> List[Dict]`：`list_active_pending_tasks()` 里 `planned_date` 非空且 `< today` 的子集（往日未完成）。

### 1.3 去重与排序

- **去重**：一个任务只出现在优先级最高的一组，顺序 = 今日 > 往日 > 持续关注（按 `task.id` 全局去重）。
- **排序**：
  - `T_today`：`(task.status != 'in_progress', task.priority, task.id)` 升序 ⇒ 进行中在前，再优先级小者在前（R1 批准）；
  - `T_overdue`：`(task.planned_date, task.id)` **降序** ⇒ **最近过期的优先**（R2 裁定：搁置很久的任务可能已失效，不应仅因时间久就占最显眼位置；它的含义是"需要重新判断/安排"，不是"最重要"）；
  - `T_watch`：沿用现有 `collect_task_cards` 的排序（`matches_focus_title` 前导键 + `created_at` 倒序 + `id`），**本批不改 focus 加分口径**（R3 批准，锚点 §一.11/§一.13 不动）。
- **概览行计数**：`今日 N · 持续关注 M · 决策 K · 发现 L`。**`N` 只统计 `planned_date == 今天` 的未完成任务**；往日未完成作为今日区内的独立小分组展示、**不计入 `N`**（R7 裁定：不把历史遗留伪装成今天新安排的任务）。读失败写「读不出来」，不写 0。

### 1.4 三组的动作（首页写口，仅此三处）

| 位置 | 动作 | 写什么 | 复用写口 |
|---|---|---|---|
| 今日任务每条 | 「▶️ 开始」 | `goal_tasks.status = 'in_progress'` | `goal_manager.update_task_status` |
| 今日任务每条 | 「✅ 完成」 | `goal_tasks.status = 'done'` | `goal_manager.update_task_status` |
| 往日未完成每条 | 「📌 安排到今天」 | `goal_tasks.planned_date = today` | `goal_manager.set_task_planned_date` |
| 持续关注每条 | （无新写口） | — | 保持现有「去目标页标记完成」跳转 |

- **首页新增写口只有 `status`（开始/完成）与 `planned_date`（安排到今天）两类，共三处回调**；其余（`is_home`、`add_task`、`update_goal`、决策、机会）照旧零写。
- 三处写回调只写**各自那一列**，由全行对拍测试钉住（见 §4）。
- 不额外加确认弹窗（用户裁定 1）；不新造撤销机制。

---

## 2. 单一主流程（每步读写 + 刷新/换页/重启依赖）

| 步 | 用户动作 | 回调 | 读 | 写 | 依赖的数据 |
|---|---|---|---|---|---|
| 1 | 打开/刷新首页 | `refresh_home_panel`（扩）+ `refresh_focus_panel` | `goals`(active)、`goal_tasks`(status/priority/created_at/**planned_date/is_home**)、`decisions`、`opportunities`、`current_focus` | 无 | 全部来自库（经 core 共享查询） |
| 2 | 点「⚡ 帮我排今天」 | `do_generate_daily_plan(override_hours?)` | active 目标×未完成任务＋`daily_time`(或本次 override)＋经验检索；LLM 挑选 | **无（只分析）** | 库；本次时长 override 来自输入框 |
| 3 | 预览阶段调整本次时长 → 点「按此时长重新生成预览」 | 同上（带 override 重算预览） | 同上 | 无 | 输入框值（**不写 profile**） |
| 4 | 点「✅ 确认并应用」 | `do_apply_daily_plan(daily_plan_state)` | — | `goal_tasks.planned_date = today`（`set_task_planned_date`） | `daily_plan_state`（gr.State） |
| 5 | 应用后 | `.then(refresh_home_panel)` | 库 | 无 | 库（今日区出现这批任务） |
| 6 | 点「▶️ 开始」 | `do_home_start_task(task_id)` | — | `status = 'in_progress'` | `task_id` |
| 7 | 点「✅ 完成」 | `do_home_complete_task(task_id)` | — | `status = 'done'` | `task_id` |
| 8 | 点「📌 安排到今天」 | `do_home_reschedule_today(task_id)` | — | `planned_date = today` | `task_id` |

**刷新 / 换页 / 重启依赖**：
- 三组列表全部读自库（`planned_date`/`status`/`is_home`），**不依赖 `gr.State`** ⇒ 刷新、换页、重启后所见一致；「开始/完成/安排到今天」只写库、写后刷新，不依赖内存。
- **唯一依赖 `gr.State` 的是"预览"**（`daily_plan_state`）：换页/重启后预览消失属预期（它是临时态）；应用之后已写库，不依赖预览还在不在。
- **本批不改 `do_execute_plan`**：它仍吃 `daily_plan_state`（内存态）的既有问题不在第一批范围（那是"执行计划"，本批只做"计划出现＋开始/完成"）。执行席**不要顺手改它**，边界见 §3。

### 2.1 缺 `daily_time` 的提示与本次调整（裁定 5，选 A、不阻断；R4 修正交互）

- `generate_daily_plan` 增加可选参数 `daily_hours_override: Optional[float] = None`：非空时 `_get_daily_hours` 用它，否则走原逻辑（`profile.daily_time` → 回落 2.0）。
- 展示文案在缺 `profile.daily_time` 时**显式**说：「尚未设置每日可用时间，本次暂按 2 小时安排」；有 override 时按 override 展示。
- 预览阶段给一个「本次可用小时」输入框。**用户调整时长后，通过一个明确的「按此时长重新生成预览」按钮触发重算**——**不要因每次输入变化就自动反复调用规划模型**（R4）。生成与重算预览**都不写 profile**，用户点「✅ 确认并应用」之后才写 `planned_date`。
- 负控见 §4 第 6 条：把"缺值提示"这半句摘掉 ⇒ 用例红；把默认 2.0 写成"用户已确认" ⇒ 用例红。

---

## 3. 第一批确切改动范围与不改范围

### 3.1 改（确切清单）

| 文件 | 改什么 |
|---|---|
| `core/goal_manager.py` | 新增只读查询（**不写库**）：`list_active_pending_tasks` / `get_tasks_planned_for` / `get_overdue_tasks`（§1.2 定义；返回行含 `goal_title`，供首页与 reminder 复用） |
| `agents/reminder_agent.py` | `get_today_pending_tasks` 改为内部调 `goal_manager.get_tasks_planned_for(today)`（**不再手写同一套筛选**）；对外返回 shape 保持不变或仅做投影 |
| `ui/controllers/home_controller.py` | 改调 core 共享查询做三组分组；`refresh_home_panel` 输出扩为含「今日安排」与「持续关注」；新增三个写回调 `do_home_start_task`/`do_home_complete_task`/`do_home_reschedule_today`（各只写一列，返回首页刷新元组） |
| `ui/app_factory.py` | 首页 Tab 新增「📅 今日安排」组件（列表 Markdown＋开始/完成/安排到今天按钮＋「⚡ 帮我排今天」＋本次时长输入框＋「按此时长重新生成预览」按钮）；原「最近创建的待办」区改名「🔄 持续关注」；`refresh_home_panel` 的 outputs 相应扩；三处写按钮绑定新回调；**「📅 计划与复盘」页的 `generate_plan_btn`/`generate_review_btn` 停用**（移除点击绑定或置 `interactive=False`，标注"旧版已停用"），`refresh_plan_btn`/`refresh_review_btn` 保留只读历史查看 |
| `agents/daily_planning_agent.py` | `generate_daily_plan` 加 `daily_hours_override` 可选参数；`format_plan_markdown` 加"缺 daily_time 提示"句 |
| `ui/controllers/learning_controller.py` | `do_generate_daily_plan` 透传 override；`do_apply_daily_plan` 成功后触发首页刷新 |
| `app.py`（启动入口） | **移除 `start_scheduler()` 调用**（R6）；保留 `scheduler.py` 文件与函数不删 |
| `DESIGN_ANCHORS.md` | §一.11 追加"首页写口例外"：仅 `status`(开始/完成) 与 `planned_date`(安排到今天) 两个写口；今日/往日/持续关注三组口径；去重规则；共享查询下沉 core 的事实 |
| `PROJECT_STATUS.md` | §五 新条目登记本批；§三 首页行更新 |

### 3.2 不改（明确边界，执行席不得顺手扩展）

- **不删**任何表/页/代码：`tasks`、`daily_plans`、`daily_reviews`、`agents/daily_agent.py`、`scheduler.py` 文件、速览页都保留（裁定 2/3）。
- **不改** `do_execute_plan`/`task_executor`/`save_approval`（执行计划不在本批）。
- **不改** focus 加分排序（`matches_focus_title` 前导键，锚点 §一.11/§一.13 不动）；**不把 `current_focus` 接进规划排序**（能力边界，见 §0.1）。
- **不加**进度字段（`updated_at`/`blocked_reason`）、**不自动改任务日期**（往日未完成只展示＋人点「安排到今天」，不悄悄改）。
- **不做**智能优先级/复杂冲突分析/自动执行学习与代码/跨会话恢复细节/经验回流。
- 不碰冻结清单（Provider 抽象/Capability Registry/多 Workflow/Belief/通用 State 框架）。
- **不借此重构整个 `GoalManager` 或 reminder 子系统**（D1 第 5 条）：只下沉本批必需的两个查询＋一个通用枚举。

### 3.3 老版停用方式（保留历史，不留两套活跃入口；R5/R6）

- 停用**生成**入口：速览页「⚡ 手动生成（速览）」「⚡ 手动生成（复盘）」两按钮停用；`app.py` 启动入口**移除 `start_scheduler()` 调用**，08:00/20:00 定时不再生成旧版计划。
- **核实真实生产启动路径**（R6 硬要求）：执行席必须现读 `app.py` 的 `if __name__ == "__main__"` 与 `scheduler.py`，确认 `start_scheduler()` 只在生产启动路径被调用、且移除后旧版定时不再触发；**不能只验证某个测试入口没起定时器就认为生产入口已停用**。
- 保留**历史查看**：`refresh_plan_btn`/`refresh_review_btn` 仍读 `daily_plans`/`daily_reviews`（只读）；表与 `agents/daily_agent.py` 代码保留。**"仅历史查看"必须是真的**：页面不再提供任何生成旧版计划的操作，也不属于日常规划主流程。
- 页面标注「旧版·仅历史查看」；**新旧两套今日规划不得同时活跃**。

---

## 4. 验收标准 → 测试、断言、负控

> 每条：判据 → 测试符号名（执行席落点）→ 具体断言 → 负控（摘掉哪个守卫 ⇒ 哪条红）。负控只动生产代码、不打真网络、先桩住 `business.model_client` 咽喉点再变异。

1. **今日任务不用 `is_home` 筛选**（矛盾 1）：`is_home=0` 且 `planned_date=今天` 且未完成的任务，必须出现在「今日安排」。
   - 测试：`tests/test_today_region.py::test_today_shows_planned_even_if_not_home`。
   - 断言：夹具造一条 `is_home=0`、`planned_date=今天` 的任务，渲染后标题在今日区出现。
   - 负控：在 `get_tasks_planned_for` 里加 `and is_home != 0` ⇒ 该用例红（任务从今日区消失）。

2. **共享查询：首页与 reminder 共用同一把"今日"尺**（D1，取代"两处对拍"）：`reminder_agent.get_today_pending_tasks` 与首页今日区都来自 `goal_manager.get_tasks_planned_for`，不再各自手写。
   - 测试：`tests/test_goal_manager.py`（共享函数返回与边界）+ `tests/test_reminder_agent.py::test_reminder_uses_shared_planned_query`。
   - 断言：① `get_tasks_planned_for("2026-10-20")` 返回 active 目标下 `planned_date==2026-10-20` 且非 done 的任务；② `get_overdue_tasks("2026-10-20")` 返回 `planned_date<2026-10-20` 且非空且非 done；③ `reminder_agent.get_today_pending_tasks` 的集合 == 共享函数结果（同 id 集合）。
   - 负控：让 `reminder_agent` 退回手写 `planned_date==今天` 筛选（绕开共享函数）⇒ ③红；让 `get_overdue_tasks` 把 `planned_date` NULL 也算进往日 ⇒ ②红。

3. **往日未完成展示 + 安排到今天**（矛盾 2/3）：`planned_date < 今天` 且未完成且目标 active 的任务，出现在「往日未完成，待重新安排」，点「安排到今天」只写 `planned_date=今天`（不碰 status）。
   - 测试：`test_overdue_shows_and_reschedules_without_touching_status`。
   - 断言：① 往日任务渲染于往日子分组；② 点「安排到今天」后它从往日组消失、出现在今日组；③ 全行对拍：除 `planned_date` 外（含 `status`）逐列不变。
   - 负控：摘掉 `get_overdue_tasks` 里 `status != 'done'` 的过滤 ⇒ 已完成的往日任务也冒出来，用例红；把「安排到今天」写成 `update_task_status` ⇒ 全行对拍红。

4. **持续关注去重**：出现在今日/往日组的任务，不得在持续关注区重复出现。
   - 测试：`test_watch_excludes_today_and_overdue`。
   - 断言：同一 `task.id` 出现在今日组后，持续关注渲染里无它的标题。
   - 负控：去掉 `T_watch` 的"减去 T_today∪T_overdue" ⇒ 用例红（出现两次）。

5. **首页写口只开两列**：三处新写回调只写各自一列，其余全行不变。
   - 测试：`test_home_start_complete_reschedule_write_only_their_column`（全行对拍，复用 `test_task_lifecycle` 的既有对拍写法）。
   - 断言：`do_home_start_task` 后仅 `status` 变；`do_home_complete_task` 后仅 `status` 变；`do_home_reschedule_today` 后仅 `planned_date` 变；`is_home`/`priority`/`source`/`milestone_id`/`created_at` 逐列不变。
   - 负控：把 `do_home_reschedule_today` 里顺手写 `status='in_progress'` ⇒ 全行对拍红。

6. **静态禁词与绑定区口径收窄**（护栏修订，见 §6）：首页只允许 `update_task_status` 与 `set_task_planned_date` 两个写调用；其余禁词（`set_task_home`/`add_task`/`update_goal`/`execute` 等）仍禁。
   - 测试：`tests/test_home_dashboard.py` 的 `_FORBIDDEN_CALLS`（改）、`test_home_controller_source_has_no_write_or_llm`（改）、`test_home_write_callbacks_only_in_focus_block`（改，见 §6.3）。
   - 断言：新回调调用了这两个写口不被判红；调用 `set_task_home` 判红。
   - 负控：在 `do_home_complete_task` 里换成 `set_task_home` ⇒ 静态扫描红。

7. **缺 `daily_time` 提示 + 本次调整不写 profile + 不自动重算**（裁定 5/R4）。
   - 测试：`test_daily_plan_missing_daily_time_hint_and_override`。
   - 断言：① 缺 `daily_time` 时 `format_plan_markdown` 含「尚未设置每日可用时间」；② `generate_daily_plan(daily_hours_override=1.5)` 的展示用 1.5 小时；③ 全程 `profile` 表无 `daily_time` 新增行；④ 预览阶段调整输入框**不自动**触发模型调用，只有点「按此时长重新生成预览」才重算。
   - 负控：把提示句从 `format_plan_markdown` 删掉 ⇒ ①红；把 override 写进 `ProfileManager.set("daily_time",…)` ⇒ ③红；把输入框 `change` 事件绑到重算 ⇒ ④红。

8. **老版生成停用、历史可读**（裁定 2/R5/R6）：速览页生成按钮与 scheduler 定时不再产 `daily_plans` 新行；`refresh_plan` 仍能读到历史。
   - 测试：`test_old_plan_generation_disabled_history_readable`。
   - 断言：① `app.py` 的 `__main__` 不再调用 `start_scheduler()`（现读确认）；② 速览页 `generate_plan_btn` 无 `.click` 绑定（或 `interactive=False`）；③ `refresh_plan` 读历史正常；④ 改 `tests/test_skeleton.py::test_scheduler_registers_two_daily_jobs` 的断言口径为"不再注册每日生成任务"。
   - 负控：重新接回 `generate_plan_btn.click(generate_daily_plan)` ⇒ ②红；重新在 `__main__` 加回 `start_scheduler()` ⇒ ①红。

9. **启动即见今日区**：`demo.load` 链上 `refresh_home_panel` 覆盖「今日安排」与「持续关注」（重启不点刷新也可见）。
   - 测试：`test_home_load_shows_today_region`。
   - 断言：`refresh_home_panel` 返回值包含今日/往日/持续关注三组 Markdown，且 outputs 数量与组件一致。
   - 负控：从 `refresh_home_panel` 返回里漏掉今日组 ⇒ 该用例红。

---

## 5. 生产库迁移检查 + 真实使用最小验证

### 5.1 迁移检查（执行席落点）

- 已核实：`current_focus` 表在、`goal_tasks.planned_date`/`is_home` 列在（11 列）、`is_home` 已回填。**本批零新迁移**。
- 执行席自检命令（只读，不 import 项目模块）：
  ```powershell
  .\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:data/memory.db?mode=ro',uri=True);print([r[1] for r in c.execute('PRAGMA table_info(goal_tasks)')])"
  ```
  确认 `planned_date`、`is_home` 都在 11 列里。
- 新增回调与 core 共享查询的读路径一律 `.get("planned_date")`/`.get("is_home")`（`.get` 容错），**不在读路径里触发任何迁移或写**；core 共享查询是纯只读（`SELECT`，无 `UPDATE/INSERT`）。

### 5.2 真实使用最小验证（不建遥测/分析系统）

用实际操作观察四个问题，每个都回答"是/否"并记录步数：

1. **从打开首页到确认安排要几步**：打开 → 点「帮我排今天」→（可选调时长）→ 确认 → 看到今日任务。目标：≤3 次点击，不跨页。
2. **能否直接开始一件事**：在今日区点「▶️ 开始」，该任务变为进行中且留在首页；点「✅ 完成」后离开今日区。全程不翻页、不重新描述目标。
3. **下一次打开时未完成任务是否仍可见**：今天没做完、没点「安排到今天」的任务，次日应出现在「往日未完成，待重新安排」（按最近过期优先）；已安排今天的出现在「今日」。
4. **是否还需要重新整理思路**：观察打开后是否还要自己翻目标页/记忆页去回忆"我该干嘛"。若仍要，记录卡在哪一步，回报设计席。

判定：第 1、2 条都"是"且第 4 条明显减轻，才算第一批打通了核心断点；测试全绿**不能代替**这四条。

---

## 6. 护栏修订清单（必须同批改，否则既有测试会误红或失效）

`tests/test_home_dashboard.py` 有三道守卫与本批直接冲突，必须同批修订、且各自带负控证明仍有牙齿：

1. **`_FORBIDDEN_CALLS` 禁词表**（现含 `update_task_status`、无 `set_task_planned_date`）：改为「全禁写集合 **减去** 已授权的 `{update_task_status, set_task_planned_date}`」；同时把 `set_task_planned_date` 补进表作为"本批放开前曾未被扫到"的记录。断言：新回调调这两个不红，调 `set_task_home`/`add_task`/`execute` 仍红。
2. **`test_home_controller_source_has_no_write_or_llm`**：同一静态扫描器，改判据为"除两个授权写口外无其它写/模型调用"；保留 `test_static_write_scan_is_not_vacuous`（扫 `review_controller` 仍必须命中）证明扫描器有牙齿。
3. **`test_home_write_callbacks_only_in_focus_block`（绑定区 marker 切片）**：marker 之前"零写回调"的辖境要改——今日区是**新增的第二个写口块**，需新增一条 marker（或复用同一切片规则）把「今日安排」块也划进"允许写"的一侧，且该块内只许出现 `do_home_start_task`/`do_home_complete_task`/`do_home_reschedule_today`（不许借道 `do_start_task`/`do_complete_task`/`do_add_task_home`）。
4. `tests/test_skeleton.py::test_scheduler_registers_two_daily_jobs`：断言从"注册 08:00/20:00"改为"不再注册每日生成任务"。

> 执行席注意：这四处的**精确改法必须现读**对应测试文件的当前文本再动，不得凭本规格的记忆改写；改完复跑对应测试文件确认"该红的红、该绿的绿"。

---

## 7. 待执行席现读清单（下笔前必须 grep/read 核实，不许凭印象）

- `ui/controllers/home_controller.py`：`_home_task_rows`、`collect_task_cards`、`refresh_home_panel` 的当前返回值与 outputs 一一对应关系。
- `ui/app_factory.py`：首页 Tab 组件与 `refresh_home_panel` 的 outputs 绑定、`demo.load` 链尾、速览页四枚按钮绑定。
- `app.py`：`if __name__ == "__main__"` 里 `start_scheduler()` 的调用点（R6 核实生产启动路径）。
- `scheduler.py`：`run_scheduler`/`start_scheduler` 的注册与调用（确认移除调用点后旧版定时不再触发）。
- `agents/daily_planning_agent.py`：`generate_daily_plan`/`_get_daily_hours`/`format_plan_markdown` 签名与展示拼装。
- `agents/reminder_agent.py`：`get_today_pending_tasks` 当前实现（改为调共享函数后对外 shape 是否保持）。
- `core/goal_manager.py`：`update_task_status`/`set_task_planned_date`/`get_tasks`/`list_goals` 的返回与写 SQL（共享查询要复用同一取行方式，仍须现读确认签名未变）。
- `tests/test_home_dashboard.py` 全文：§6 列的三道守卫当前精确文本。

---

## 8. 已定的实现选择（D1 = 方案 B，限定下沉范围）

**"今日/往日/持续关注"三组筛选**：数据查询下沉 `core/goal_manager.py`（`list_active_pending_tasks`/`get_tasks_planned_for`/`get_overdue_tasks`），首页与 `reminder_agent` 共用；`is_home` 过滤、去重、focus 加分排序、分组呈现留在 `home_controller.py`。本批只下沉当前工作流必需的两个查询＋一个通用枚举，不重构整个 `GoalManager`/reminder 子系统。"下沉 core"只登记为本批已落实的共享查询设计，**不再另开一项泛化重构任务**。
