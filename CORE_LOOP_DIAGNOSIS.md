# 核心工作循环诊断与最小改造路线

> 阶段：产品问题收敛与设计准备（**本阶段不写/不改生产代码**）。
> 依据：本仓库源码（`ui/`、`agents/`、`core/`、`memory/`、`app.py`、`scheduler.py`）与生产库
> `data/memory.db` 的**只读**直连读数（`sqlite3` `mode=ro`，不 import 任何项目模块）。
> 口径：本文凡涉及行数/条数，一律标「以复跑为准」——生产数据随使用增长，只有结构、字段、调用关系是稳的。
> 出处写「符号名＋文件」，不写行号（行号随重构漂移，见 `DESIGN_ANCHORS.md` 维护规则）。

---

## 七.1 目标与缺口定位

- **目标（一句话）**：让用户每天打开 Agent 后，第一屏就能看到"今天做什么"、点一下就能开始推进其中一件，并让昨天的结果回到今天的安排里。
- **最关键的系统性缺口（一句话）**：系统已经有两套"今日计划"机制和一个任务执行器，但它们之间**没有接起来**——计划的产物（`goal_tasks.planned_date`）到不了第一屏、执行器不回写任务状态、"开始"只是一个标签，于是"理解现状→形成安排→协助行动→记录结果"四段各自成立、互相不连通。

---

## 七.2 核心工作循环诊断表

> 分类词：设计问题 / 实现问题 / 测量问题 / 使用问题 / 真实能力缺口。
> 证据等级：**已核实**（读码/读库复现）／**推断**（由已核实事实合理推出）／**待验证**（需真机使用确认）。

### 环节 A：理解现状

- **期望**：打开就能知道：当前重点、长期目标、未完成/进行中/已完成任务、时间约束、最近一次行动的结果与遗留。
- **当前实际**：字段层大多存在，但"当下该做什么"这一问，首页实际只用得到 `goal_tasks.status + is_home + created_at` 三个信号。
- **证据（已核实，以复跑为准）**：
  - `goals` 2 行全 `active`，`deadline` 0/2 有值、`priority` 2/2 等于默认 2（零区分度）、`updated_at == created_at`（建后从未被改过）；
  - `goal_tasks` 10 行（pending 8 / in_progress 1 / done 1），**没有 `updated_at` 列**、没有 `last_attempt`/`blocked_reason`，`planned_date` 0/10 有值；
  - `current_focus` 1 行「提升英语成绩」（explicit/active），但 `goals` 里那条目标叫「提高英语成绩」——**两本账同一件事、连措辞都不一致**（提升≠提高）；
  - `profile` 只有 4 个 key，**没有 `daily_time`**（三处读方全部回落到常量 2.0 小时，等于系统用默认值替用户决定"今天还能放几件"）；
  - `learning_records` 405 行但最新停在 2026-09-05，且 `goal_id` 指向已不存在的目标 ⇒ 现存目标读学习统计是 0；
  - `home_controller` 首页完全不读 `goals.deadline/priority`、`goal_tasks.priority/planned_date/description`、`profile`、`learning_records`、`goal_notes`。
- **分类**：实现问题（有字段无数据：deadline/planned_date/daily_time）+ 真实能力缺口（`goal_tasks` 无"动作时间/受阻原因/上次推进"字段，`goal_notes` 0 行且无人读内容）+ 设计问题（重点与目标两本账互不引用）。
- **根因**：进度只记"创建时刻"（`created_at`），不记"动作时刻"与"卡在哪"；目标层的 deadline/priority 是空的，任务层的 priority 不参与首页排序。
- **影响**：一条搁了三周的 pending 与今天新建的 pending 在库里一模一样，系统无法判断"哪件更急/哪件卡住了"。
- **可否复用**：`reminder_agent.get_today_pending_tasks` 已经实现了"active 目标下 planned_date=今天且未完成"的正确口径，可直接复用；`current_focus` 表结构完整。
- **不处理的代价**：首页继续按"创建时间"猜重要，用户每次都要自己重新理一遍。
- **是否第一批必要**：部分——"今天该做什么"依赖 `planned_date`（环节 B 解决），"上次卡在哪"依赖新增进度字段（可后置）。

### 环节 B：形成当日建议

- **期望**：基于目标＋约束＋昨日结果自动形成"今天做什么"，说明理由，可接受/调整/拒绝，时间不足时识别取舍。
- **当前实际**：存在**两套**每日规划，产品面重复、数据面不重复，且都到不了第一屏。
- **证据（已核实）**：
  - **老版** `agents/daily_agent.generate_daily_plan`：读 `profile(long_goal/daily_time)`＋昨日 `learning_records`＋知识图谱薄弱点(硬编码 domain="AI")＋**legacy `tasks` 表**；写 `daily_plans`（一段文本）；无确认环节；由 `scheduler.py` 08:00/20:00 与「📅 计划与复盘」页「⚡ 手动生成」调用。**`daily_plans` 的唯一读者是那个页面的「🔄 刷新」**，首页/提醒/任务系统/执行器一律不读。失败文案也会覆盖当天计划格。
  - **新版** `agents/daily_planning_agent.generate_daily_plan`：读 active 目标×未完成任务＋`daily_time`＋经验检索；LLM 挑选（失败降级本地规则）；`apply_daily_plan` 写 `goal_tasks.planned_date`；**确认环节有**（生成→应用两段）；入口在目标页「📅 每日规划」Accordion。`planned_date` 的读者只有两处：`reminder_agent.get_today_pending_tasks`、目标页任务行的「📅 今天」标签。**首页 `_home_task_rows` 不读 `planned_date`**。
  - 「📅 计划与复盘」页文案自称"只展示、不写库"，**与实现相反**（老版实写 `daily_plans`/`daily_reviews`）。
  - **断点**：① 老版整条断死——`daily_plans` 写了无下游，看得到要自己想到进那页点刷新；② 「▶️ 执行计划」吃的是前端 `gr.State`（`daily_plan_state`），**不是库里的 `planned_date`**，刷新/换页/重启即空、退化为现场重生成且不写库；③ 提醒只在 `reminder_times`（默认 09:00,14:00）整分钟、内存态触发，`planned_date` 写死"今天"不结转 ⇒ 14:00 之后应用的计划要等次日 09:00、而次日已不等于今天 ⇒ **永不提醒**。
  - 另有一处重复：`planner_agent.analyze_conflicts` 与 `planning_mirror_agent.suggest_next_actions` 几乎同构（同数据源、同 action 词表、两套 `_validate_*`＋`apply_*`、两个按钮）。
- **分类**：实现问题（老版死账、速览页错标、执行器吃内存态）+ 使用问题（新版埋在目标页 accordion，3 步才能产出）+ 设计问题（两套机制并存、`planned_date` 不结转）。
- **根因**：老版是历史遗留、没被下线；新版的产物没有接到"第一屏"和"执行"两个消费端。
- **影响**：用户要么用一套只看不动的文本，要么走一套写了标记但不提示、不执行、不上首页的流程。
- **可否复用**：新版 `daily_planning_agent` + `goal_tasks.planned_date` + `reminder_agent` 口径就是可复用的活路径，缺的只是"接出来"。
- **不处理的代价**：计划形同虚设——今天 08:00 已经自动生成了一份计划（`daily_plans` 2026-10-20 那行），但它躺在没人读的表里，用户打开首页完全看不见。
- **是否第一批必要**：**是，第一优先**——这是"决定先做什么"的直接承载。

### 环节 C：协助推进

- **期望**：选定后明确"当前下一步"，工具能做的做、不能做的给支撑，受阻时能定位/调整，而不是把"生成任务/勾完成"当成推进。
- **当前实际**：执行器**能真执行，但只对"计划/模板"执行，不对"你选中的那条任务"执行**。
- **证据（已核实）**：
  - `agents/task_executor.execute_plan` 支持四类真执行：`search`（DDGS 真联网）、`note`（真打模型写草稿）、`scout_digest`（零网络读本地机会库）、`long_term_review`（打模型＋写 `decisions`）；落盘一律走 `save_approval`（`pending_approvals` 是**写盘审批**，不是执行审批——搜索/生成在暂停点之前已跑完）。
  - `execute_plan` **全程不碰 `goal_manager`**：跑完那条任务仍是 pending/in_progress，要用户自己回目标页勾完成——**执行与进度是两条互不相通的线**。
  - 「▶️ 开始任务」（`goals_controller.do_start_task`）只写 `goal_tasks.status = in_progress` 一列：不调执行器、不产待审批、不发事件；`tests/test_task_lifecycle` 用全行对拍钉住"只改那一行 status"。
  - **唯一执行入口是"计划"粒度**（`do_execute_plan` 的 `inputs=[daily_plan_state]`），没有"对单条选中任务执行"的调用点；能否被执行由 `auto_executable`（标题关键词/LLM 的 auto 字段）决定，标题不含搜索/整理关键词的任务永远进不了执行器。
  - 背单词、写代码、线下报名这类任务 `classify_task` 返回 `None`，执行器只会输出"暂不支持，已跳过"。
- **分类**：真实能力缺口（无"任务→执行"直连、无"下一步具体是什么"、执行不回写状态）+ 使用问题（推进要七步：生成→判断⚡→应用→执行→审批→回目标页勾完成→失败自行判断，系统只代劳第四步的一部分）。
- **根因**：执行器与任务表是两套契约；"开始"被做成了状态标签而不是动作入口。
- **影响**：用户选定事情后，系统能帮忙的部分（搜索/写笔记草稿）与任务账本脱节，帮不了的部分（学习/写代码）连"分解成今天 30 分钟做什么"都没有。
- **可否复用**：`task_executor` 四类执行、`approval_store`、`save_approval`、`do_start_task`/`do_complete_task` 全部可复用。
- **不处理的代价**："生成任务"与"推进任务"继续是两件事，用户承担全部协调。
- **是否第一批必要**：**部分**——第一批先打通"选定→开始/完成"这最薄的一层（复用现有回调），"任务→执行"直连可后置（它依赖 B 的计划载体先统一）。

### 环节 D：恢复与调整

- **期望**：记住推进到哪、识别未完成及原因、按当天时间变化重排、新会话不用重新交代背景、能处理延期/优先级变化/临时学业要求。
- **当前实际**：**基本没有恢复能力**——`goal_tasks` 没有 `updated_at`/受阻原因，上下文注入不含"当前重点/今日计划/上次没做完的"。
- **证据（已核实）**：
  - `goal_tasks` 建表与 `PRAGMA` 都无 `updated_at`；`goal_notes` 表存在但 0 行、`GoalManager.add_note` 无调用方（`get_notes` 只在统计里数个数）；
  - `core/context_manager.build_system_prompt` 注入的是：目标上下文（仅当传入单个 `goal_id`，读其 title/desc＋pending 前 5 条）＋档案（`key_labels` 五键）＋最近学习/薄弱点＋**legacy `tasks`**＋相关经验＋复盘教训（`future_effect`）；**不注入 `current_focus`、不注入 `planned_date` 的今日计划、不注入"上次没做完及原因"**；`detail_level` 由 supervisor 的 task_type 决定，闲聊/搜索走 low（连档案段都不注入）。
  - 延期/优先级：`goals.priority/deadline` 可改（`update_goal`），`goal_tasks.priority` 有 `update_task_priority` 写口但**生产调用 0、UI 无对应操作**；任务日期只有"标记为今天"、无"改到某天/顺延"入口；"临时学业要求"在三表里没有任何承载字段。
  - 会话恢复有一处隐性坑：`chat_controller.create_new_session` 不设 `goal_id`，而 `demo.load` 每次打开都新建会话 ⇒ **默认新会话连目标段都不注入**（`context_manager._build_goal_context` 需要传入的 goal_id）。
- **分类**：真实能力缺口（无进度/受阻字段、上下文不恢复工作现场）+ 使用问题（新会话要重新交代背景）。
- **根因**：任务对象只记"出生"，不记"动作"；上下文构建没把"工作现场"当一等公民。
- **影响**：用户每次打开都要重新回忆"我上次做到哪、为什么停"。
- **可否复用**：`context_manager` 已有注入骨架，补字段即可；`current_focus` 已有表。
- **不处理的代价**：即使 B 打通了"今天做什么"，第二天还是从零回忆。
- **是否第一批必要**：**后置**——第一批先让"当天"闭环，跨天恢复作为紧接的第二批。

### 环节 E：结果与能力积累

- **期望**：记录真实完成（区分完成/部分/未开始/受阻）、沉淀经验与偏好、下次安排真正利用、验证积累减少了重复劳动。
- **当前实际**：**写入多、消费少**——"记忆写入成功"≠"积累有效"。
- **证据（已核实，以复跑为准）**：
  - `experiences` 1 行 legacy（scenario「制定今日计划时容易贪多」），`future_effect` NULL、`decision_id` NULL ⇒ 4b 的"复盘教训注入"只读 `future_effect`，这一条永远不注入；
  - `experiences` 的消费者三处：`context_manager._build_experiences_context`（按 `user_input` 关键词检索）、`context_manager._build_goal_lesson_context`（只读 `future_effect`）、`daily_planning_agent._get_experiences`（固定查询「今日计划 时间安排 学习」）；
  - `decisions` 0 行、`project_states` 0 行 ⇒ 复盘闭环、决策账本、经验回流的整条链在生产库里是空的，谈不上"被下次消费"；
  - `learning_records` 405 行但 `goal_id` 已与现存目标脱钩；`notes` 1 行；`goal_notes` 0 行；
  - `goal_tasks` 没有"完成时间"字段（只有 `status`），无法统计"何时完成的、这周推进多少"。
- **分类**：测量问题（没有"积累是否改善下次"的证据）+ 实现问题（legacy 经验缺 `future_effect`、学习记录与目标脱钩）+ 使用问题（沉淀入口存在但没形成闭环）。
- **根因**：结果没有回流到"下次安排"的唯一真源（`planned_date`/优先级/重点），回流链（复盘→经验→注入）在生产库从未跑通一遍。
- **影响**：昨天的行动不改变今天的安排，系统不会越用越顺手。
- **可否复用**：`experience_store`、`decision_store`、`project_state`、`long_term_project_review` 全套都在，缺的是让它们在生产里跑起来并接进 B 的输入。
- **不处理的代价**：合作不积累，每次都是新一次。
- **是否第一批必要**：**后置**——先打通"当天闭环"，积累验证作为验证批次。

---

## 七.3 功能处置表（五格）

> 只覆盖与核心工作循环相关的模块；「复用/修改」是第一批的主力，「冻结/删除」是为给第一批腾出注意力，不是本阶段就动手。

### 复用（保持不动，第一批直接调用）

| 对象 | 说明 |
|---|---|
| `goal_tasks`＋`GoalManager`（`core/goal_manager.py`） | 现行任务账本，`add_task`/`update_task_status`/`set_task_planned_date`/`set_task_home` 齐备 |
| `current_focus`＋`core/current_focus.py` | 当前重点表与读写函数，结构完整 |
| `agents/daily_planning_agent.py`（新版） | 生成今日计划（只读）＋应用写 `planned_date`，**是活路径，第一批复用** |
| `agents/reminder_agent.get_today_pending_tasks` | "active 目标下 planned_date=今天且未完成"的正确口径，第一批首页「今日」区直接复用 |
| `agents/task_executor.py`＋`memory/approval_store.py` | 四类真执行＋写盘审批，复用不改 |
| `goals_controller.do_start_task`/`do_complete_task` | 状态写口（首批的"开始/完成"直达就是它们） |
| `memory/experience_store.py`/`decision_store.py`/`project_state.py`/`long_term_project_review.py` | 积累链全套，后置批次复用 |

### 修改（第一批的主体）

| 对象 | 改什么 | 为什么 |
|---|---|---|
| 首页 `home_controller` | 顶部加「今天」区，读 `planned_date=今天` 的未完成任务（复用 `get_today_pending_tasks` 口径），每条带「开始/完成」直达；把「当前重点」作为这一区的抬头 | 让第一屏回答"今天做什么"，点一下就开始 |
| 两个「今日计划」入口收敛 | 「📅 计划与复盘」速览版与目标页「📅 每日规划」合并成首页「今天」区的一个「帮我排今天」（复用新版 生成→预览→确认） | 消除两套机制，老版死账下线 |
| `daily_planning_agent`/执行载体 | 「▶️ 执行计划」改读库里的 `planned_date`（而非 `gr.State`）；应用计划后把"今天"的未完成任务设为首页「今日」区的主键 | 修"刷新即空、计划与执行两份"的断点 |
| 速览页文案/`PROJECT_STATUS.md` §五.19 | 更正"只展示、不写库"的错标 | 文档与实现一致 |

### 新增（第一批尽量不新增；此处为后置候选，先登记不实现）

| 对象 | 为什么现有机制不够 | 最小职责 |
|---|---|---|
| `goal_tasks` 进度列（如 `updated_at` 或 `last_attempt_at`/`blocked_reason`） | 现有无"动作时刻/卡在哪"字段，环节 D 无法恢复 | 记"上次推进时间＋受阻原因"，供次日恢复 |
| （候选）「今日计划」结转语义 | `planned_date` 写死当天，迟应用的计划永不触达 | 让"未完成的今日任务"在次日仍可被看见（不改数据，改读侧口径即可，尽量不加表） |

### 冻结（不再扩展，保留现有能力）

| 对象 | 理由 |
|---|---|
| `agents/prompt_evolution.py`（Prompt 进化） | 无真实使用证据，不影响核心循环 |
| 深度搜索独立任务体系（`core/information_engine`） | 与"每天决定先做什么"无关 |
| 复杂知识图谱/向量记忆扩展 | 同上 |
| 项目复盘/决策账本进一步扩张 | 生产库 decisions 0 行，先让基础闭环跑起来再扩张 |
| 规划镜像/意图识别/多目标冲突分析继续扩张 | `planner_agent` 与 `planning_mirror_agent` 已重复，先不扩 |
| 资源机会扫描自动化扩展 | 与"今天做什么"弱相关，保持现状 |

### 删除（本阶段**不动手**，只登记依据；删除需先过用户）

| 对象 | 依据 | 风险与前置 |
|---|---|---|
| legacy `tasks` 表（`memory/learning_recorder.py`） | 16 行全 pending、`complete_task`/`delete_task` 零调用、零测试/零契约/零棘轮；但仍有 2 写 3 读（`event_handlers.next_task`、`evaluator.suggested_task`、`context_manager._build_tasks`、`profile_controller.get_memory_display`、`daily_agent._generate_plan`） | **删表必须同批删 `memory/database.py::init_db` 里那行 `_add_column_if_not_exists(conn,"tasks","goal_id",…)`，否则缺表即抛 `OperationalError`、应用起不来**；且 16 行数据不可逆 |
| 「📅 计划与复盘」速览页＋`daily_plans`/`daily_reviews`＋`agents/daily_agent.py` | 唯一读者就是该页自己；老版读 legacy `tasks` 与 profile，写死文本 | 必须与 `scheduler.py` 同格决策——删页而留 scheduler，等于每天 08:00 打一次 LLM 写进没人读的表 |
| `scheduler.py` | 只服务老版 daily_agent（08:00/20:00），核心流程不依赖任何定时器 | 删了 `tests/test_skeleton.py` 两条用例会红；`requirements.txt` 的 `schedule` 可同批去 |
| `mistakes` 表（`memory/database.py`） | 0 行、`LearningRecorder` 里读它/写它的函数零调用方 ⇒ **死表** | 数据为 0，删除无数据损失；但同批要把 `LearningRecorder` 里那几个零调用函数一起处理 |

---

## 七.4 最小改造路线（只留解决核心断点的阶段）

> 顺序 = 依赖顺序。每阶段一个根因、一个"不跳过"的理由、一个可观察的用户行为变化、一个验证方式。

**阶段 0（前置，零产品改动）**：清两处错标——「速览版只展示不写库」与 `PROJECT_STATUS.md` §五.19 的同一句。不跳过理由：文档与实现相反会误导下一批所有判断。验证：文案与 `daily_agent` 实际写库行为一致。

**阶段 1（第一批，本路线的主体）——把"今天做什么"接到第一屏，并让"开始/完成"直达**：
- 解决根因：环节 B 的断点（`planned_date` 到不了首屏、计划与执行两份载体）+ 环节 C 的"选定→开始"最薄一层。
- 涉及：`ui/controllers/home_controller.py`、`ui/app_factory.py`（首页布局与两处今日入口）、`ui/controllers/learning_controller.py`／`task_controller.py`（复用 `do_generate_daily_plan`/`do_apply_daily_plan`/`do_execute_plan`/`do_start_task`/`do_complete_task`）、`agents/reminder_agent.py`（复用 `get_today_pending_tasks`）。
- 为什么不跳过：这是"决定先做什么"的唯一承载，其他阶段都建立在"今天做什么能被看见、被开始"之上。
- 预期用户行为变化：打开首页直接看到「今天：提升英语成绩 → 这几步」，点「开始」那条变 in_progress 且首页保留、点「完成」离开；「帮我排今天」一键产出计划（预览→确认）。
- 验证：见七.6。
- **明确不做**：不删表、不删旧页（旧页先冻结/隐藏，不做硬删除）、不动执行器四类能力、不做任务→执行直连、不加进度字段。

**阶段 2（紧接）——"计划载体"统一与"开始"真正启动**：
- 解决根因：`do_execute_plan` 吃 `gr.State` 而非库里的 `planned_date`；「开始」只改状态不准备下一步。
- 涉及：`task_controller.do_execute_plan`（改读库）、可选「开始」时把该任务的"下一步"（复用 `task_executor` 对 auto 任务）提示出来。
- 预期变化：刷新/重启后仍能执行今天应用的那份计划；点「开始」能看到"这条我能帮你搜资料/写草稿，或这条要你自己做，第一步是 X"。
- 不做：通用自主执行、给每条任务配 AI 子步骤。

**阶段 3（跨天恢复）**：
- 解决根因：环节 D 无进度字段、上下文不恢复现场。
- 涉及：`goal_tasks` 加 `updated_at`（或 `last_attempt_at`/`blocked_reason`）、`context_manager` 注入"当前重点＋今日计划＋上次未完成及原因"。
- 预期变化：第二天打开，系统直接说"昨天这 3 条没做完（因为 X），今天建议先做 Y"。
- 不做：自动改代码、多 Agent 协作。

**阶段 4（积累验证）**：
- 解决根因：环节 E 回流没跑通。
- 涉及：让复盘→经验→注入在生产里跑一遍，并验证下次计划是否真的用了昨天的结果。
- 预期变化：能给出"上一次结果改善了这一次安排"的真实证据。

---

## 七.5 第一批的候选范围

**只做一件事：把「今日计划」提到首页第一屏，作为唯一的"今天"入口，并让计划今天的任务能被「开始/完成」直达。**

具体到实现形状（供设计席/执行席后续细化，本阶段不写代码）：

> ⚠️ 本节的实现口径以 `CORE_LOOP_BATCH1_SPEC.md` 为准（2026-10-20 审查后收敛），此处只留方向。

1. 首页新增「📅 今日安排」区：主列表 = 今日任务（`planned_date==今天` 且未完成、目标 active，**不看 is_home**），尾部小分组 = 往日未完成（`planned_date<今天` 且未完成、目标 active，带「安排到今天」）；每条今日任务带「▶️ 开始」「✅ 完成」直达。**读侧在 `home_controller` 内实现（读 `goal_manager`），不 import `agents/reminder_agent`（层序禁止 ui 引 agents）**。
2. 收敛两个旧入口：新版「每日规划」收敛到首页「⚡ 帮我排今天」（生成→预览→确认）；老版速览页**停用生成入口**（手动按钮＋08:00/20:00 定时都停），页面降为只读历史查看、表与代码保留——不留两套同时活跃的生成入口。
3. 「开始/完成/安排到今天」在首页落地（对"首页零写"契约的**有意例外**，进 `DESIGN_ANCHORS.md` §一.11：首页只放开 `status`(开始/完成) 与 `planned_date`(安排到今天) 两类写口，其余照旧零写）。

**为什么是它，而不是别的看似更该做的**：
- "生成更多任务/更多 Tab/更好看的卡片"都改变不了"计划产物到不了第一屏"这个断点；
- "先做对象级讨论/自动复盘"解决的是"推进之后沉淀"，在"推进"本身还没闭环时是空的（生产库 decisions 0 行就是证据）；
- "先删 legacy tasks/旧页"是清债，不是减负——删了用户负担不变，还动数据；
- 这一件事同时踩中用户 §1.2 的第 1、2 条愿望，且全部复用现有代码（`planned_date` 写口、`get_today_pending_tasks` 读口、`do_start/complete` 写口都已存在，只是没接上）。

**第一批明确不做的**（防止范围蔓延）：不删任何表/页/数据；不动执行器四类能力；不做任务→执行直连；不加进度字段；不做 AI 自动改代码；不新开 Agent；不碰冻结清单（Provider 抽象/Capability Registry/多 Workflow/Belief/通用 State 框架）。

---

## 七.6 验收与测量

### 系统正确性证据（判据必须能变红，配负控）

1. 首页「今日安排」区 = `planned_date==今天` 且未完成的 active 目标任务，**不含**：`done`、`planned_date` 为其它日期、归档目标；**不使用 `is_home` 作筛选**（`is_home=0` 但已安排今天的也必须显示）。
2. 「⚡ 帮我排今天」生成→预览→确认→写 `planned_date`，预览阶段**一行不写**；确认后首页「今日安排」区当次刷新出现这些任务。
3. 首页三个写回调各只写一列、全行对拍：`do_home_start_task`/`do_home_complete_task` 只写 `status`；`do_home_reschedule_today` 只写 `planned_date`（复用 `test_task_lifecycle` 的对拍写法），不写 `is_home`/其它表。
4. 两处旧入口收敛后，不再存在"生成一段写死 `daily_plans` 的文本、与任务账本无关"的路径（负控：重新接回老版 `generate_daily_plan` ⇒ 对应用例红）。
5. 负控不打真网络（先桩住 `business.model_client` 咽喉点再变异）；负控只动生产代码、不动测试。

### 真实使用证据（测试全绿不能代替）

1. 用户一次真实的"当天安排"：打开首页 → 点「帮我排今天」→ 确认 → 看到「今日安排」区。
2. 有真实任务的开始/推进/调整记录：至少一条 `planned_date=今天` 的任务被「开始」，然后被「完成」或改期。
3. 操作步骤下降：把"生成今日计划→应用→执行"从目标页 3 步＋翻页，降到首页 1–2 步；能数出点击次数/翻页次数变化。
4. 跨会话恢复：第二天打开，首页「今日安排」显示今天的计划任务；昨天未完成且目标仍有效的任务显示在「往日未完成，待重新安排」分组（可一键「安排到今天」），不要求重新规划。
5. 下一次使用是否利用了上一次结果（阶段 4 再验，第一批只留观测点）。

---

## 七.7 待用户裁定项（已全部裁定，2026-10-20）

1. **首页写口**（选 A）：首页允许直接「开始/完成」写入 `goal_tasks.status`，仅开放这两个既有写口（加「安排到今天」写 `planned_date`）；其余区域零写。不额外加确认弹窗。进锚点 §一.11。
2. **老版速览页**：冻结并退出主流程——保留历史数据与代码、不删页面/表/`scheduler.py`；手动生成入口退出日常使用、页面降为只读历史查看并标"旧版"；停用 08:00/20:00 定时。不留两套同时活跃的生成入口。
3. **legacy `tasks`/`daily_plans`/`daily_reviews`**：登记为后续清理候选，本批不删。保留数据与兼容代码，后续按真实调用关系、迁移风险、使用证据再决定。
4. **「今日安排」与 `is_home`**：语义分离、首页并存。今日区读 `planned_date/status/目标状态`、不受 `is_home` 限制（`is_home=0` 也显示）；「持续关注」读 `is_home`（原「最近待办」降级改名）并排除已出现在今日/往日组的任务。
5. **`daily_time` 缺值**（选 A 但不阻断）：生成计划时提示「尚未设置每日可用时间，本次暂按 2 小时安排」；预览阶段允许直接调整本次时长（不要求先去设置页、不写 profile）；不得再把默认 2 小时当作用户确认过的时间。

---

## 七.8 最后回答：如果只能改一件事

**把「今日计划」搬到首页第一屏，作为唯一的"今天"入口，并让计划今天的任务能被「开始/完成」一键直达。**

理由（全部已核实，非断言）：
- 你现在打开 Agent 不知道先做什么，**不是缺信息，是信息到不了你眼前**——系统今天 08:00 已经自动生成了一份"复习错词＋造句练习"的计划，但它写进了 `daily_plans` 那张没人读的表；你手动走的新版计划写进 `goal_tasks.planned_date`（现在 0/10，一条都没有），又到不了首页、不触发提醒、执行器还吃的是会随刷新消失的内存态。
- 而"点一下就开始"的零件**全都现成**：`do_generate_daily_plan`/`do_apply_daily_plan`（生成/确认写 `planned_date`）、`get_today_pending_tasks`（今天未完成的正确口径）、`do_start_task`/`do_complete_task`（开始/完成写口）、`current_focus`（你已设了「提升英语成绩」）。缺的只是一根把它们接到首页的线。
- 这一改同时回答你的两个最痛的点：**决定先做什么**（首页直接给今天的任务＋理由）、**少理一次思路**（不用进目标页 3 步生成、再回来勾完成）。
- 它全部复用现有能力，不动数据、不删表、不新开 Agent、不碰冻结清单，是"最小、可验证、优先复用"的最贴合形态。
- 第一批的验收，就是你能不能**打开首页 → 点「帮我排今天」→ 点「开始」→ 干完一件 → 点「完成」**，全程不翻页、不重新描述目标；做到，就说明核心断点被打通了。

> 其余一切（进度字段、跨天恢复、经验回流、对象级讨论、自动复盘）都不是"只改一件事"能解决的，也不该在打通这个入口之前先做——它们解决的是"推进之后怎么办"，而你现在卡在"推进"这一步之前。
