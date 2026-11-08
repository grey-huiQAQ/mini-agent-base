# DESIGN_ANCHORS —— 第一闭环冻结协议

> 本文件是**设计侧的单一可信锚点**：执行席 grep 这里拿到「必须逐字照抄、不许自己推导」的
> 协议字段与纪律。与 `PROJECT_STATUS.md` 的分工——这里只放**定值**（枚举、键名、判据、纪律）；
> 「为什么这么定」的推理与沿革放 `PROJECT_STATUS.md` §五。
>
> 维护规则（2026-10-07 定案）：设计席在对话里做出的任何**会进代码的定案**，必须同轮落到
> 本文件。只活在对话里的定案 = 欠账（本项目已三次被它咬：P5 降级落点 grep 不到、判据④
> 的「拒」假设不存在、Fix C 对照表磁盘零副本）。
>
> **出处一律写「符号名（文件）」，不写行号**（2026-10-07 定案）：行号和行数计数一样
> 随重构漂移，而本文件存在的意义就是稳定。实证——上一轮为了守住 §一.6 那一个行号，
> 一次纯注释替换被硬约束成「等行数替换」；**需要靠保持行数来维持的引用，本身就是脆弱的证据**。
> 代价换在这里：符号改名或被删时，本文件必须同轮跟着改，grep 不到就是漏。

---

## 一、协议字段（真值，逐字照抄；出处＝符号名＋文件，**不带行号**——改过码之后 grep 符号名，别按行号找）

### 1. decision_scope（4 值）—— `VALID_DECISION_SCOPES`（core/decision_store.py）

- `tactical`　这几天内的当下调整
- `operational`　一周内要落地的改法（复盘产物的默认档）
- `strategic`　跨周以上的项目走向
- `critical`　不可逆、或撞上硬时点

改名对照（生产库 decisions 当前 0 行，属「改常量」不是「迁数据」）：
`daily → tactical ／ weekly → operational ／ long_term → strategic ／ critical 不变`。

### 2. EXPERIENCE_ELIGIBLE_SCOPES（3 值）—— `EXPERIENCE_ELIGIBLE_SCOPES`（core/decision_store.py）

- `operational / strategic / critical`（`tactical` 不回流经验）
- 用**集合成员判断**（`frozenset`），**不用全序 / `>=` 比较**。删秩表的原因见
  `EXPERIENCE_ELIGIBLE_SCOPES` 下方那段 ⚠️ 注释（core/decision_store.py）：这四个值是「跨度」与「可逆性」两根轴上的东西，借一个
  全序会让「新增一档」变成一次坐标系改动，而 4a/4b 要的只是集合成员判断。

### 3. evaluation（5 值）—— `VALID_EVALUATIONS`（core/decision_store.py）

- `helpful`　帮上了
- `neutral`　说不上帮也没碍事
- `harmful`　把事带偏了
- `wrong_assumption`　当初先采信的前提被证伪（闭环的牙齿）
- `insufficient`　没攒够能判断的结果，先不下结论

> ⚠️ **已裁决的偏差（2026-10-07 设计席定案）**：ChatGPT 曾提议
> `correct / partially_correct / wrong_assumption / external_change / insufficient_information`
> （「判断对不对」轴）；落地代码（Phase 1b）用的是
> `helpful / neutral / harmful / wrong_assumption / insufficient`（「帮没帮上」轴），
> 设计席当时说「采纳」却从未把这组落进代码，两者从未对账。**裁决：保留代码这组。**
> 理由：`wrong_assumption`（闭环牙齿）两组都有；`helpful/harmful` 比 `correct/partially_correct`
> 更贴近「辅助型自主」（用户在乎帮没帮上，不在乎抽象对错）；「判断对不对」轴由
> `evaluate()` 的 `actual_result` 叙述承载。将来若确需 `external_change` / `partially_correct`，
> 是「加一值到 `VALID_EVALUATIONS` ＋ 一条测试」的便宜改动，不是迁移。

### 4. item.kind（5 类）—— `VALID_ITEM_KINDS`（core/decision_store.py）

- `fact`　可复验的观测（`source` 记表/文件/命令）
- `user_report`　用户本人说过的话（`source` 记出处）
- `inference`　有事实支撑的推断（`source` 记支撑它的那条 fact）
- `assumption`　没有证据、但当前必须先采信的前提（无 `source` 或 `source=None`）
- `unknown`　明确承认不知道（Phase 2「找不到来源就登记、不要编」落这里）

判据（写进代码是因为只写文档时会被忘）：**inference 与 assumption 的区别只在于有没有
fact 支撑。**

### 5. status（7 态）+ 边集 —— `VALID_DECISION_STATUSES`／`LEGAL_TRANSITIONS`（core/decision_store.py）

七态：`proposed / discussed / approved / rejected / executed / evaluated /
insufficient_information`

边集（`LEGAL_TRANSITIONS`）：

- `proposed → {discussed, approved, rejected, insufficient_information}`
- `discussed → {approved, rejected, insufficient_information}`
- `approved → {executed, rejected}`（`approved → rejected` 是有意留的「执行前反悔」边）
- `executed → {evaluated}`
- `insufficient_information → {discussed}`（全表唯一一条向后的边）

终态：`evaluated`、`rejected`（不可回改；推翻 = 立新决策 + 老决策追加 `kind='revision'`）。

- `executed` / `evaluated` **只能**由 `mark_executed()` / `evaluate()` 走，`set_status` 直接拒。
- **同态重复设置 = 非法**（会推 `updated_at`，污染「这条决策什么时候被推进过」）。

### 6. CAPABILITY_MAP（12 键）—— `CAPABILITY_MAP`（core/project_state.py）

`recall_local_material` ／ `read_write_notes` ／ `find_external_information` ／
`filter_by_explicit_rules` ／ `run_scheduled_tasks` ／ `queue_for_approval` ／
`distill_experience` ／ `keep_project_ledger` ／ `inject_context` ／
`extend_with_skills` ／ `verify_and_rollback` ／ `talk_to_model`

- `carrier` 是**列表**，以 `core/project_state.py` 为准（tests 逐条验存在）。
- `UNKNOWN_CAPABILITY = "unknown"` **刻意不在表里**（「挑不出来」与「挑到 unknown」必须可分）。
- 已知不在表里的能力（第一闭环用不到，不补）：`summarization` / `planning` / `evaluation` /
  `knowledge_graph`。将来某条决策真需要引用，是「加一值到常量表 ＋ 一条测试」。

### 7. VALID_ACTIONS（3 值）—— `VALID_ACTIONS`（core/task_template.py）

- `search_and_note`　资料搜索 ＋ 写笔记（v3.7.1 起就在）
- `scout_digest`　「资源」筛选报告（本地机会库）
- `long_term_review`　长期项目复盘，产物是一行 Decision 账目（3b 立动作、3c-甲 接执行分支）

按范围规则进锚点的理由：它被**三个模块**同时消费——`core`（建/改模板时校验）、`agents`
（执行侧按它分派）、`ui`（表单选项与标签）；而「非法动作名改拒」（3c-乙）钉的就是这一格。
同文件里的 `DEFAULT_TIME`／`DEFAULT_WEEKDAY` 是单模块内部默认值，**不进锚点**。

### 8. 经验 ↔ 决策的链接：`experiences.decision_id`（**单向**）

定案（4a）：新增 `experiences.decision_id INTEGER NULL`，走 `_add_column_if_not_exists`
（`memory/database.py` 的既有迁移入口，同一张表的先例是 `score` 与 `future_effect`）。
**不加中间表、不做 `decisions.experience_id`、不复用 `execution_reference`。**

⚠️ **别读错列**：`decision_items.decision_id` 是**既有**的「条目 → 决策」外键（`NOT NULL`，在 `memory/database.py` 的 `decision_items` 建表文本里），它不是本节说的那一列。本节的 `experiences.decision_id` **可空**、语义是「这条经验从哪次评估来」，两列同名的那格纯属巧合。
4a 的落地写法照抄这一行：`_add_column_if_not_exists(conn, "experiences", "decision_id", "INTEGER")`。

为什么只能单向：
- **因果是单箭头**：经验由决策产生（评估一条决策 → 提炼成经验）。一条经验只有一条来路，
  一个决策可以提炼出多条经验——链接放在「经验」这一侧，一行一个指针就够；放反方向要么得存
  多值，要么把「哪一条」推进中间表，而 4b 要的只是「这条经验从哪次评估来」。
- **一列只装一种含义**：`execution_reference` 的语义是「执行了什么」（笔记路径／模板运行 id），
  不是「学到了什么」。复用它会把两件事压进同一格，将来谁都读不准。

**更正（2026-10-07 设计席定案）**：设计席早前给的「双向冗余
（`decisions.experience_id` ＋ `experiences.decision_id`）」**是错的，正确是单向**。
本席实测留档：两列此前都不存在——2026-10-07 用只读连接跑 `PRAGMA table_info`，
`experiences` 有 id／scenario／strategy／effect／conditions／source_session_id／created_at／
score／use_count／last_used_at／future_effect，`decisions` 有 id／goal_id／title／summary／
status／decision_scope／evaluation／actual_result／execution_reference／created_at／updated_at，
**两边都没有指向对方的列**。所以这条不是「删掉一半」，是「只新增一列」。
⚠️ 归属如实记一句（守 §五.1）：「双向冗余」那句的原文，本席**没有可复核的磁盘副本**——
拆分文档的提取文本（仓库外 `D:/tmp_plan_1423.txt`）grep `experience_id`／`decision_id`／「双向」
均 0 命中，那一节写的只有 `link_experience`。**设计席 2026-10-07 已确认**：那句是其对话转述，磁盘上确实没有原文——所以本节记的是「设计席的定案 ＋ 本席未能见到原文」，不是本席复核过的原文。

### 9. `future_effect` 的写点、作者与落列（4a）

| 落到 `experiences` 的列 | 内容从哪来 |
|---|---|
| `scenario` | 这条经验在什么场景下参考（决策的上下文） |
| `effect` | `decisions.actual_result`（评估时写下的实际结果） |
| `future_effect` | 「下次改什么」的那句教训 |
| `decision_id` | 链接回那条决策（见 §一.8） |

- **写点**：「提炼经验」这一步，在 `evaluate()` **之后**（决策被评 → 提炼成经验）；
  它是 4a 的一个独立步骤，不并进 `evaluate()` 的写路径里。
- **作者**：AI 起草 ＋ 人确认（辅助型自主：最终决策权在用户）。
- **`strategy` 不写**：它是 v3.7.1 对话反思的 legacy 字段，与 `future_effect` 来源不同
  （前者来自对话反思，后者来自决策评估）——**两个字段并存，不合并、不重命名、不互相回填**。
  4b 只读 `future_effect`；生产库现有那 1 行 legacy 经验（`strategy` 有值、`future_effect` 为 NULL）
  在 4b 自然不注入，**属预期，不是缺失**。
- 实现约束（本席 2026-10-07 实测，非设计席定案）：`experiences.strategy` 在 DDL 里是
  **`TEXT NOT NULL`**（生产库 `PRAGMA table_info` 同），而 `ExperienceStore.add_experience` 的
  `strategy` 参数**没有默认值**——所以「不写它」唯一的落法是**调用点显式传空串 `""`**
  （NOT NULL 收空串、不收 NULL）。本席按「调用点传空串，签名与校验都不动」实现；
  若设计席要的是别的形态（例如给参数加默认值，那属「造默认值」），请在本轮指出。

### 10. 阶段一：闸门②的分流协议（2026-10-11，决策模型改造）

**为什么这一组进锚点**（按 §五.3 的范围规则逐格判，不是整包塞进来）：
`evidence` 三值与 unknown 的 `code` 两码是**持久化值**——它们落在 `project_states.items_json`
里，历史快照要能被后来读代码的人正确解读，而「当年没这个概念」与「读到了零」是两件事；
`gate_shape` 五值是 `core` → `ui` 的**跨模块协议字段**（`outcome` 四值里的分档）；
`CONFIDENCE_SUGGESTION_MIN` 是设计席阶段一规格明写的定值。
**不进**的：`SHAPE_LABELS_ZH`／`OUTCOME_LABELS_ZH` 的中文措辞、`REQUEST_QUESTION_LIMIT`
与 `ASK_QUESTION_LIMIT` 这两个各自封顶的数字——单模块显示文案与单模块内部常量。

- **`FACT_EVIDENCE_VALUES` 三值** —— `FACT_EVIDENCE_VALUES`（core/project_state.py）：
  `activity`（这一族读到非零的项目活动）／`activity_empty`（同一族读到了，但计数为零）／
  `context`（上下文与仓库卫生类读数，不参与实质判断）。
  **哪一族算 `activity` 由采集器声明**，设计席拍板 5 的口径是三族：`goal_tasks`／`notes`／
  `decisions` 非零；仓库卫生类（`test_runs`、`checks/*_exemptions.json` 台账行数）与配额类
  （`agent_resource_log` 调用数）、`pending_approvals` 一律 `context`。
  闸门②**只读这个标记**，不比条数、不解析 `k=v` 字符串。
  ⚠️ 这三族**标 `activity` 的条件**各不相同。其中 `notes` 一条**已是设计席定案**
  （2026-10-11 裁决 1 收窄），另两条仍是执行席读码实测、尚未定案：
  `goal_tasks` 看 `total`（`_collect_goal_tasks` 是全表计数，不按 `goal_id` 过滤）；
  `notes` **只看 `_collect_recent_notes` 里的 `new`**——窗口内写过才算，全表存量不算
  （原口径是 `new or total`，2026-10-11 起作废，见下面那条漏口的落地记录）；
  `decisions` 是上一条（`ORDER BY id DESC LIMIT 1`，非空即算）。
  ⇒ 一份只关于目标 A 的快照，它的 `goal_tasks` 读数里含目标 B 的任务；「有实质依据」
  目前**仍是整库层面**的判断，不是「这个目标层面」的判断。阶段二若要改成按目标过滤，
  请在本节一并改这三条条件，别只改采集器。
  ⚠️ **那条实测漏口已由设计席 2026-10-11 裁决 1 收窄，原文形状保留备查**
  （`prod_shape_stage1.txt`，生产库的只读副本）：当时自动段 7 条 fact 里有**两条**带
  `activity`——`goal_tasks[pending=10, total=10]` 与 `notes[recent_days=7, new=0, total=1]`，
  而后者**窗口内那一格是 0**，即「写过一篇很久以前的笔记」足以让闸门②放行。
  **定案句**：周复盘关心的是「最近在推进」，不是「曾经有产出」；`total` 仍逐字进快照文本、
  复盘读得到，只是证据标记归 `activity_empty`。守卫用例
  `test_note_outside_the_window_is_not_evidence`（tests/test_project_state.py）。
  落地后**同一份副本重跑**（`prod_shape_ruling1.txt`，与改前那份的逐行 diff 只剩 `notes`
  这一格）：它降为 `activity_empty`，闸门②放行只剩 `goal_tasks[pending=10, total=10]`
  **一条独力支撑**，出口仍是 `proposed`。⇒ 剩下的两族条件（`goal_tasks` 全表计数、
  不按 goal 过滤且无时效；`decisions` 只看上一条）要不要一并收窄，仍属定案。
- **`evidence` 是「给了才写」的可选键**：人工段条目与历史快照（在该标记引入之前存的）
  都没有这一格。读侧 `.get("evidence")` 必须分开三种读法：`activity`／非 `activity`／
  **当年没这个概念**。第三种按「没有实质依据」处理，且 `reason` 里如实写明「这份快照的条目里
  没有证据标记」，**不许**读成「它读到了一堆零」。
- **unknown 的两个 goal 码** —— `UNKNOWN_CODE_GOAL_NONE`／`UNKNOWN_CODE_GOAL_AMBIGUOUS`
  （core/project_state.py）：分别是「goals 表里选不出 active 目标」与「active 目标不止一个、
  采集层不替人挑」。⚠️ **分流只看 `goal_ambiguous` 这一个码**：`build_context` 是
  「`goal_id` 为空且不带 ambiguous 码」的**补集**，`core/long_term_project_review.py`
  刻意不 import `goal_none`——那两个码在分流上是同一个出口，引入第二个常量会让人以为
  它在判两件事。
- **`gate_shape` 五值** —— `GATE_SHAPES`（core/long_term_project_review.py）：
  `suggestion`／`exploratory`／`ask_direction`／`build_context`／`choose_goal`。
  它是 `outcome` **四值里面**的分档，不是第五个出口（拍板 4：出口四值不动）；
  `manual_section_missing` 与 `no_change_needed` 这两档**不属分流**，那一格是 `None`。
- **置信度线 `CONFIDENCE_SUGGESTION_MIN = 0.5`** —— （core/long_term_project_review.py）：
  `confidence >= 0.5` 说成「建议」，否则说成「探索性假设」。
  ⚠️ **`None` 不等于 `0.5`**：模型没报这一格、或报的不是 0–1 之间的数，一律按「不够线」处理。
  把缺席读成中性就是替模型填了一个它没说过的数——同一理由见下一条的 `bool`。
- **观测四键 `OBSERVE_KEYS`** —— `OBSERVE_KEYS`（agents/supervisor.py）：
  「问题」／「建议下一步」／「需要的能力」／**「置信度」**（阶段一新增）。
  `core/long_term_project_review.py` 里是**按字面量重复的一份**（层序禁止 core 引 agents），
  由 `test_observe_key_literals_match_agents_supervisor` 逐值比对——那边改名这边即红。
  「置信度」这一格的取法：`bool` 也算「没报」（`True` 被钳成 1.0 就是替模型说有十足把握），
  越界与非数一律 `None`，**不补一个 0.5 假装中性**。
- **问句旁路的键 `ASK_KEY_QUESTIONS = "补充问题"`** —— （agents/supervisor.py）：
  闸门②请求补充时那三问由它现生成，**不复用观测那三格**。它的降级出口是**空列表**而不是
  「无法判断」那个哨兵——哨兵是给「一句话」用的，塞进问题列表会变成一条问不出口的问题，
  而调用方（`_record_request`）本来就在等空列表去走内置兜底问。
  空列表有两副面孔，必须可分：**合法的空**（模型如实说不知道 ⇒ 静默返回、由调用方标出处）、
  **失败的空**（打不通／返回散文 ⇒ 必须往 stdout 出声）。

---

### 11. 阶段二 B1：首页工作台三类卡片的口径（2026-10-17 四处缺口定案）

> 出处＝符号名：`CARD_TYPE_*`／`CARD_TYPES`／`HOME_TASK_STATUSES`／`CARD_CAP`／
> `_DECISION_SCAN_LIMIT`（ui/controllers/home_controller.py）；B1b 追加
> `ai_next_step`（memory/database.py）与 `MIN_RELEVANCE_SCORE`／
> `EXCLUDED_ELIGIBILITY_HINT`／`HOME_CANDIDATE_STATUS`／`fetch_home_candidates`／
> `_fetch_candidates`（core/ai_filter.py）。本条只锁**跨模块口径**
> （读哪张表、按什么筛、字段叫什么），不复制界面上的渲染文案。

- **待办 = active 目标下的 `goal_tasks.status ∈ {pending, in_progress}`**，**不是全表**：
  归档目标底下残留的任务（哪一档都不）不上首页。定案理由＝首页回答「现在最值得关注的
  几件事」，陈年任务属噪声；实现约束＝`goal_manager.get_tasks()` 必须带 goal_id、而 B1
  不许新增 SQL。下一批按这句口径读，不要再按 B1 计划 §二 那句字面全集读。
  - **状态那一维由阶段二 组 4（2026-10-18）从「只 pending」扩成两档**（符号名同步改为
    `HOME_TASK_STATUSES`）。理由＝组 3 给目标页加了「▶️ 开始」按钮，pending→in_progress
    从此是日常动作，只读 pending 等于「用户刚按下的那一条从首页消失」。**目标那一维
    一字未动**（仍只 active），**done 仍在门外**（勾完就该离开「等你处理」这一页）。
  - 进行中的那条在卡片 ③ 开头带「进行中」前缀，pending 不带；**不在界面层新造状态
    标签表**（口径同 `home_controller.py` 文件头第 5 条），卡片字典里存的是 `status`
    这一格事实，前缀由它推出。
  - **排序键仍是 `created_at` 倒序，状态不参与**：扩集合不等于「进行中优先」，要把两档
    分出先后必须先在本条留话并过设计席。三条用例分别钉住集合、标签、排序。
  - ⚠️ 上面那句「排序键仍是 `created_at` 倒序」的**前半**已被阶段二 组 5 子批 B
    （2026-10-19）改写：待办与发现两族现在都在**最前面**多了一个「与 active focus 对得上」
    的布尔加分键，`created_at`／`id` 两层原样留在它后面。**后半句「状态不参与」仍然成立、
    本批没碰**（无 active 重点时前导键恒 False，顺序与 B 之前逐条一致；判据由
    `tests/test_home_dashboard.py::test_task_cards_sort_by_created_at_not_by_status` 与
    `tests/test_task_home.py::test_task_card_order_is_the_old_one_when_no_focus_is_active`
    两头钉住）。新键的形状、「只能进 ORDER BY」那条边界与为什么不放进
    `CANDIDATE_ORDER_BY` 见 §一.13 与 §四 组 5 子批 B。
  - 筛选在**内存里**过集合：`get_tasks(status=…)` 的 SQL 是 `status = ?`，一次只匹配一个
    值，照集合循环＝每加一档每目标多一次查询；这一族本来就跨目标在内存重排，取回来的
    done 行当场丢掉，不进排序也不进截断。
- **`card_type` 词表（3 值，`CARD_TYPES` 的顺序＝界面三区的顺序）**：`decision`／`task`／
  `opportunity`。卡片字典里那一格**不叫 `type`**：`opportunities.type` 已经占着「这条
  机会自己的粗分类」那个含义（取值见 `core.resource_scout.TYPE_LABELS`），同一格两个含义
  是本项目反复咬过的坑，所以靠**字段名**分开，而不是靠读的人记住两种含义。加第四族＝
  加一个 `CARD_TYPE_*` ＋一个采集函数＋注册表一行，渲染与跳转的形状不许跟着改。
- **决策的取回窗口与截断键不同向＝已知边界，现在不动 `core/decision_store.py` 的排序
  契约**：`list_decisions` 按 `id DESC` 取前 `_DECISION_SCAN_LIMIT` 条，首页再按
  `updated_at` 倒序截断。`decisions` ≤ 50 条时无影响；接近 50 条时「旧决策被最近更新过」
  会被漏在门外。**将来改法**（等接近 50 条那天再做）：给 `list_decisions` 加 `order_by`
  参数，默认仍是 `id DESC`（既有调用方一字不改），首页显式传 `updated_at DESC`。
- **「点卡片」当前的形状是卡片区＋下拉＋跳转按钮，是近似形态、不是终态**：Gradio 的
  Markdown 不是可点控件。**真·可点卡片＝已知的后续项**，需要 `gr.HTML` ＋自定义事件，
  或换 UI 框架；在那一批动手之前，不许把现在这套当成「已经能点」。
- **`ai_next_step` 是持久化列，不是渲染时现造的句子**（阶段二 B1b，2026-10-17）：
  `opportunities.ai_next_step` 只走 `memory/database.py` 里的 `_add_column_if_not_exists`
  迁移——可空、不回填、**不进 `CREATE TABLE`**。写入侧 `core.ai_filter.ai_filter_opportunity`
  的口径是「去空白后非空才保留、空则 **NULL**，不替模型补一句」；「模型加工过了但没给这
  一句」（`ai_screened_at` 非空＋本列 NULL）与「还没加工」（两者都空）在库里必须分得开，
  发现卡那两句不同的占位文案就是靠这一对值分派的。消费侧只有
  `ui/controllers/home_controller.py`。
- **首页发现卡的候选集与 AI 加工队列是两个不同的集合，共享两个门槛、各多一格**
  （B1b 立、2026-10-17 设计席裁定 ① 把不变量措辞改准）：
  首页 `ai_filter.fetch_home_candidates` ＝ `HOME_CANDIDATE_STATUS`（`new`）∧
  `relevance_score >= MIN_RELEVANCE_SCORE`（0.3）∧ `eligibility_hint !=
  EXCLUDED_ELIGIBILITY_HINT`（`likely_no`），**不看 `ai_screened_at`**——加工完的那条仍留在
  首页，按 B1b 原文那句「与队列完全一致」的字面做会把目标反着实现（一加工就消失），那句
  原文**已作废**。队列 `ai_filter._fetch_candidates` ＝ 后两条 ∧ 未加工，**不看 status**。
  不变量按裁定 ① 的准确措辞只有两句：**① 首页取回的每一条都落在「可被加工」的那两个
  门槛（分数、资格）内；② 其中还没加工的那一子集 ⊆ 加工队列的取回集合。**
  ⚠️ 不是「首页 ⊆ 队列」（已加工的那条在首页、却已离开队列），也不是「⊆ 取等号」
  （队列不看 status，「非 new 且未加工」的行在队列而不在首页）——这两侧由
  `tests/test_ai_filter.py::test_unprocessed_flag_and_home_unprocessed_is_strict_subset_of_queue`
  逐值对拍并各带一条反证。两个门槛值在 `core/ai_filter.py` 里各只有**一份字面量**，
  首页与队列共用——出现第二份 0.3 就是分叉。
- **两项刻意不进本锚点**（按 §五 第 3 则的范围规则）：`HOME_PROCESS_LIMIT = 3` 是单模块
  常量，唯一消费者是本模块那个触发函数；发现卡那几句未加工／失败／加工后为空的占位文案是显示
  文案。两者的定案过程记在 `PROJECT_STATUS.md` §五.63／§五.64（裁定 ③⑤ 已闭），
  措辞本身不写进这里——**但裁定 ⑤ 那条判据是跨模块的，写在这里：失败态与未加工态必须
  可分**，两句话不许互相回落成一句。

### 12. 阶段二 组 5 子批 A：当前重点（`current_focus`）的协议值（2026-10-18）

> 出处＝符号名：`VALID_FOCUS_SOURCES`／`VALID_FOCUS_STATUSES`／`MAX_ACTIVE_FOCUS`／
> `CANDIDATE_WINDOW_DAYS`／`CANDIDATE_CAP`／`CONFIDENCE_BASE`／`CONFIDENCE_STEP`／
> `CONFIDENCE_CAP`（core/current_focus.py）；表形状＝`memory/database.py` 里
> `current_focus` 那句 `CREATE TABLE`；写口那一侧的入口名＝`do_focus_*`
> （ui/controllers/focus_controller.py）。本条只锁跨模块口径与持久化值，界面文案不复制。

- **上限 3 是写路径校验，不是 DDL 约束**（设计席判据① 原话）：表上没有 CHECK、没有触发器，
  第 4 条 active 全靠两个写口各自拦一次。⚠️ 2026-10-18 负控实测：`create_focus` 与
  `set_focus_status` 里是**两处各写各的判断**（满档消息各带一个尾巴：新增说「要先暂停或
  完成一条」、恢复说「要恢复 #id」），**摘掉任一处另一处不受影响**——摘新增侧那道闸，
  「恢复也要过闸」那条用例是绿的。所以这两处必须各摘一次才各红一次；将来把它们合并成
  一个公共函数可以，但合并前不许「摘一处＝闸已验证」。
- **paused／completed 不删历史**：生产代码对本表从不 DELETE／DROP（钉的是 SQL 字符串，
  不是中文注释；反向证据同文件扫 `core/goal_manager.py` 必须命中）。腾名额改的是
  `status` 这一格。
- **两个词表**：`source` ∈ {explicit, inferred}、`status` ∈ {active, paused, completed}，
  词表外在写口 `ValueError`。`confidence`／`evidence` **只对 inferred 有意义**——explicit
  那两格存成 NULL，inferred 缺任一格在写口被拒（判据③ 前半）。
- **推断候选零写库，除非人点采纳**（判据③ 后半）：`suggest_focus_candidates` 是只读函数，
  函数体内既不许出现写调用、也不许出现写 SQL；它挑出的候选不自动成为正式重点，更不会把
  `source` 改写成 explicit。采纳走 `do_focus_adopt`，落库那一行**来历仍是 inferred**、
  证据与置信照候选原样带上。「先不看」`_dismissed_candidates` 是 ui 层进程内存集合、
  **不写库**，重启后同一条还会再出现一次——这是登记在案的限度，不是缺陷。
- **候选的挂载点只有首页刷新**（2026-10-18 裁定：不新建 Focus Agent、不进调度器、
  不自动覆盖）：窗口 `CANDIDATE_WINDOW_DAYS`、条数上限 `CANDIDATE_CAP`，两条来路＝
  active 目标下近窗口内新建的 `goal_tasks` ＋ 还在等人定的 `decisions`；与任一 active
  重点标题对得上的那条被跳过（系统不拿推断去劝人改他自己说过的话）。
- **置信是一把三档粗尺，界面必须把话说完**：按近期活动条数给，`CONFIDENCE_BASE`／
  `CONFIDENCE_STEP`／`CONFIDENCE_CAP` 三档，文案里那句「不是概率」属于跨模块口径
  （它决定这一格能不能被当成排序依据之外的东西读）。
- **自然语言只认两种显式形状**（裁定：可借鉴开源，没有就 core＋确定性原则）：
  「我现在做 X」／「我不做 Y 了」走 core 里的前缀／后缀确定性匹配，**没有模型、没有模糊
  打分**；命中 0 条或多条 ⇒ 预览 items 为空、账本一个字不写；含糊表达不覆盖已有重点。
  **预览 → 确认 → 写入**是这一格唯一的写路径，`focus_preview_state` 是那条链的载体。
  满档时预览里的 `blocked` 只是**预告**，最终判定仍然只在写路径那一道闸上。
- **候选读到的是两根轴，全项目此刻是四根**（登记出处＝`PROJECT_STATUS.md` §五.49，
  不是 §五.64——本席首版注释指错过，2026-10-18 复跑 grep 后更正）：`current_focus` 与
  `goals`／`goal_tasks` 同走 `CURRENT_TIMESTAMP`，`decisions` 走 ISO+00:00。候选函数因此对两张表用两个不同的截止值（一个完整时间戳、
  一个日期串），跨轴只能比 `SUBSTR(...,1,10)`——这不是笔误，是本项目既存的形状。

### 13. 阶段二 组 5 子批 B：待办准入（`goal_tasks.is_home`）与重点关联排序的协议值（2026-10-19）

> 出处＝符号名：`set_task_home`（core/goal_manager.py）／`matches_focus_title`、
> `normalize_focus_title`、`active_focus_titles`（core/current_focus.py）／`_home_task_rows`、
> `collect_task_cards`、`collect_opportunity_cards`、`CARD_CAP`、`HOME_TASK_STATUSES`
> （ui/controllers/home_controller.py）／`do_add_task_home`、`do_remove_task_home`、
> `_set_task_home`（ui/controllers/goals_controller.py）；列形状＝`memory/database.py` 里那句
> `_add_column_if_not_exists(conn, "goal_tasks", "is_home", "INTEGER DEFAULT 0")`。本条只锁
> **跨模块口径**（哪一列、谁在写、按什么排），不复制界面上的按钮文案与状态串。

- **那一列的形状＝`is_home INTEGER DEFAULT 0`**（逐字照设计席指派原文），且**不写进
  `CREATE TABLE`**：仓库惯例是新列走 `_add_column_if_not_exists`，与 B1b 的 `ai_next_step`
  同形。⚠️ 一条容易写反的读数（本批实测，设计席可驳）：`ALTER TABLE ... ADD COLUMN` 会
  **改写** `sqlite_master` 里存着的那句建表文本（新列插进括号里、`FOREIGN KEY` 行仍留在
  最后），所以迁移跑过之后运行期读到的 DDL **一定**含这一列——「没改建表 SQL」这句话
  只能在**源码文本**上量。本批第一条用例首版就是按运行期 DDL 断言的，跑起来当场红在
  错误的一侧；处置与理由写在 `tests/test_task_home.py::_create_block_of` 的 docstring 里。
- **准入制**：`is_home` **非零**的那几条才算首页待办（判据原文＝指派那句「is_home=0 不上
  首页」）。默认 0 ⇒ `add_task` 与 AI 批量生成的新任务都不上首页，要人在「🎯 目标」页点
  「🏠 加入首页」。读侧判真值用「非零」而不是 `== 1`：这一列的语义是**有人才算有**，
  读不出来（NULL／空）时必须落在「不上首页」那一侧，而不是反过来凭空多一张卡。
- **存量一次性回填绑在「本次真的加了列」那一刻**：只有 `_add_column_if_not_exists` 返回
  True 才跑 `UPDATE goal_tasks SET is_home = 1`（该函数原先不返回任何东西、既有迁移调用
  全部忽略返回值 ⇒ 加返回值零行为变更）。两个理由都是现读的：① SQLite 的
  `ALTER ... DEFAULT 0` 给既有行填的是**默认值 0 而不是 NULL**，所以「哪些行回填过」事后
  **不能**从值上分辨，`WHERE is_home IS NULL` 一句也匹配不到；② 把那道 if 摘成恒真 ⇒
  每次 `init_db()` 都把人已经移出的行重新灌回首页，那不是回填而是每次启动改一次账
  （负控 N2 实测：夹具读数从 `[1, 0, 1]` 变成 `[1, 1, 1]`）。
- **写口只有一个**：`goal_manager.set_task_home(task_id, is_home) -> bool`，返回的是
  「有没有真的写着一条」（任务不存在 ⇒ False，库里一行都没改）。界面上的两个入口
  `do_add_task_home`／`do_remove_task_home` 共用 `_set_task_home`、**绑定在目标页**，
  首页那一格仍然零写（`set_task_home` 已进 `tests/test_home_dashboard.py` 的静态禁词表）。
  落库的是 `int` 0／1 而不是布尔——SQLite 没有布尔列，DDL 写的是 `INTEGER DEFAULT 0`。
  **移出首页 ≠ 删任务 ≠ 勾完成**：那一行只差 `is_home` 一列，`status` 一字不动。
- **全项目只有一把「是不是同一件事」的尺**：`matches_focus_title` ＝ 归一化
  （`normalize_focus_title`：去空白、去标点）之后**相等或互为子串**。它是从子批 A 的
  「候选跳过」那段里抽出来的公共函数，候选跳过与两类卡片的排序都调它；界面层不许再调
  `normalize_focus_title` 自己拼第二份。没有分词、没有评分、没有 embedding、没有模型。
- **排序不是门槛**（接 §四「强过滤、弱删除」那一层，形状同 B2 的 `usable_assets`）：
  focus 匹配只能进排序、不许进 WHERE。⚠️ **这句边界的出处不是子批 B 的指派原文**：现读
  原文那行只写「在 `usable_assets` 排序基础上，按『是否有助 active focus』加分排序（粗匹配
  即可）」，没有一个字提到 WHERE；它是本席把 §四 第 1 层那句「写进 WHERE 就变成②那一层的
  越权」（P7 阶段二 B2，2026-10-17 锁定决定 3）**推广**到 focus 加分上的执行席自决。
  §五.65 ③ 早就把「这条边界要不要在指派原文之外再补一句」列成待拍板项，本批一度误当原文
  引用，更正登记见 §五.66 ⑩。两处落点判读按实登记、设计席可驳：
  ① 它落在**界面层的内存重排**，不是 `core/ai_filter.CANDIDATE_ORDER_BY` 那串 SQL——那个
  常量被**三条**查询共用（加工队列／首页候选／has-unprocessed），写进去等于让首页的显示
  偏好去改**AI 加工队列谁先被处理**；② 新键放在排序键**最前面**而不是追加在末尾——两族的
  现有键都以唯一的 `id` 收尾（待办族 `(created_at, id)`、发现族 `(usable_assets 非空,
  discovered_at, id)`）、**已经构成全序**，追加在 `id` 之后的键永远取不到（负控 N3／N5
  实测：挪到末尾之后「与重点对得上」当场退化成一个装饰）。
  ⚠️ 发现卡那一句的字面（「在 `usable_assets` 排序**基础上**加分」）也可以读成
  `(usable_assets 非空, focus, discovered_at, id)`，那一支**并非**恒不可见；本批按
  「两族同一个前导键」实现，属**执行席自决、设计席可驳**，读数与理由见
  `PROJECT_STATUS.md` §五.66 ②。
- **界面上不印「与重点相关」**：卡片那一格只说库里有的事实，而这条顺序是界面自己的加工；
  `CARD_CAP` 没动，focus 只改变**谁占那几个位置**（能不能真排进前三仍受这个上限约束）。

### 14. 阶段二 第一批：首页「今日安排」的写口例外与三组共享口径（2026-10-20 定案）

> 出处＝符号名：`GoalManager.list_active_pending_tasks`／`get_tasks_planned_for`／
> `get_overdue_tasks`（core/goal_manager.py，本批新增共享查询）；`do_home_start_task`／
> `do_home_complete_task`／`do_home_reschedule_today`（ui/controllers/home_controller.py，
> 本批新增首页写回调）。本条锁跨模块口径与写口契约，不复制界面文案与排序细节。

- **首页写口例外（对 §一.11 那个「首页零写」的唯一例外）**：首页只放开两个既有写口——
  `goal_tasks.status`（「开始」→ `in_progress`／「完成」→ `done`）与 `goal_tasks.planned_date`
  （「安排到今天」）。其余（`is_home`／`add_task`／`update_goal`／决策／机会）照旧零写。
  三个写回调各只写自己那一列，全行对拍钉住（判据见 `CORE_LOOP_BATCH1_SPEC.md` §4）。
- **三组口径（跨模块共享的唯一实现）**：
  - 今日 `T_today`＝active 目标 ∧ `planned_date == 今天` ∧ `status != 'done'`，**不看 `is_home`**；
  - 往日 `T_overdue`＝active 目标 ∧ `planned_date` 非空且 `< 今天` ∧ `status != 'done'`；
  - 持续关注 `T_watch`＝active 目标 ∧ `is_home 非零` ∧ `status ∈ {pending, in_progress}`，
    **减去** `T_today ∪ T_overdue`（按 `task.id` 去重，优先级 今日 > 往日 > 持续关注）。
- **共享查询下沉 core**：`get_tasks_planned_for`／`get_overdue_tasks` 是「今日」「往日」的
  唯一实现，首页与 `agents/reminder_agent.get_today_pending_tasks` 都调它（不再各自手写）；
  `is_home` 过滤、去重、focus 加分排序、分组呈现留在 `home_controller.py`。本批只下沉
  这三个查询，不重构 `GoalManager`／reminder 子系统。

---

## 二、命名判据（1 句）

> 如果替换底层实现而不应该改变这个词，它是 Capability；如果替换实现后这个词应该消失，它是 Tool。

---

## 三、两条原则句

1. **State 是 Decision 的证据包，不是全局状态镜像。**
2. **系统真正成长的对象不是 Fact，而是 Assumption 的不断验证与修正。**

---

## 四、冻结清单（执行期间不再讨论，只允许 bug 修正 / 实现细节 / 明确阻塞）

Provider 抽象 · Capability Registry · 多 Workflow · Belief 系统 · 通用 State 框架。

**「强过滤、弱删除」三层（P7 阶段二 B2，2026-10-17 锁定决定 3）**。写进本节而不是 §三：
§三 的标题「两条原则句」和被引用的条数本身是个形状，动它要连章节名一起改；而这一条要冻结的
是「本轮**不做**什么」，正是本节括号里那句「不再讨论」。

1. **① 不进入工作台**——靠首页的筛选与排序把弱的压下去。B2 的 `usable_assets` 非空优先
   就在这一层，且它是**排序降权、不是门槛**：写进 WHERE 就变成②那一层的越权（判据
   由 `tests/test_ai_filter.py` 的「不是首页门槛」那条与 `tests/test_home_dashboard.py`
   的「没有资源也留着」那条两头钉住）；
   同层还有一条**显示口径**（执行席自决补记，设计席可驳）：`usable_assets` 的 NULL 承载
   三件事——还没加工／加工过但答了「无」／模型漏答，三者分不出，所以首页对 NULL 的处理是
   **整行不出现**，不写「（无直接可利用资源）」那种把三件事冒充成一件的话；
2. **② 归档可搜索**——`status=ignored` ＋ 资源页那个状态筛选就是现成的逃生口，**不新做**；
3. **③ 硬删除**——以后再加，**本轮不硬删任何机会**。

配套口径：原判据「**绝不放弃任何机会**」自本轮起**正式废除**（设计席 2026-10-17 裁定）。
执行时补一句实测：**该句在本仓库没有原文**（全仓零命中，四份根文档与代码注释都算上），
所以不存在「就地删掉某句话」这个动作。仓库里最近的同族原文是 `core/ai_filter.py` 头部那句
「绝不自动标记 / 自动忽略任何机会」——那句讲的是**谁按下按钮**（辅助型自主），
与这里讲的**能不能被隐藏**不是同一件事，**未被触动，也不该被读成一起废除了**。

**组 5 子批 A 的「不做什么」（2026-10-18，同样写进本节、理由同上）**：这一批的边界不是实现
细节，是设计席指派原文里的三句「不做」，落在本节的括号里那句「不再讨论」——

1. **不新建 Focus Agent**：当前重点没有自己的 agent 循环，推断候选只在首页刷新时现算；
2. **不自动覆盖**：自然语言与候选都不许改已有重点的 `title`／`source`／`status`；
   含糊与多命中一律走「预览为空、什么都不写」，而不是替人挑一条；
3. **不做复杂评分**：置信就是近期活动条数的三档粗尺，不引入加权模型、不引入时间衰减；
4. **不动既有状态机**：`decisions` 的 7 态与边集、复盘闸门、`goal_tasks` 既有字段的语义
   都不因这一批改变（子批 B 只**加列** `is_home`，不改既有列的含义）。
5. **不引入外部 HITL 运行时**：借鉴形状、不借机制——「预览→确认→写入」这条唯一写路径
   用的是本仓库自己的确定性实现（`core/current_focus.py` 的 `parse_focus_intent` ＋
   `build_focus_preview` ＋ `apply_focus_preview`），**不引入 LangChain／LangGraph 的
   `interrupt()`＋checkpointer 那一套中断-恢复**：它把「等人点一下」做成需要持久化的
   运行图状态，而本项目要的是「确认前后库里都没有写、界面上那一格随时可重算」。
   出处见 `PROJECT_STATUS.md` §五.65 ⑪。

**组 5 子批 B 的「不做什么」（2026-10-19，同样写进本节、理由同上）**：这一批的边界不是
实现细节，而是两句话——「focus 加分只进排序、不进 WHERE」走的是 §四 第 1 层「排序降权
不是门槛」那条**既有**口径（不是子批 B 指派原文，出处辨析见 §一.13 与 §五.66 ⑩）；
「`goal_tasks` 既有字段的语义不变」才是指派原文里的原句——

1. **不改 `goal_tasks` 既有列的含义**：只加 `is_home` 一列；`status`／`priority`／
   `source`／`milestone_id`／`planned_date` 一字不动（这是子批 A 第 4 条那句预告的正面
   落地，反向证据由 `tests/test_task_home.py` 的整行指纹那条用例给出）。
2. **不做自动准入**：系统不许因为「这条与当前重点对得上」「优先级高」「刚创建」就把任何
   任务加上首页——那一格只能由人点。推断与排序都不替人决定谁上这一屏。
3. **不新建第二把相似度尺**：不引分词、不引评分、不引 embedding、不引模型；「像不像」
   一律按 `matches_focus_title` 那把粗尺的结果。尺不够用时**先回来找设计席**，不在界面层
   自己加一份。
4. **不让 focus 匹配变成门槛**：不进 WHERE，也不进三条查询共用的 `CANDIDATE_ORDER_BY`；
   它只活在界面层的内存重排里（判据＝`core/ai_filter.py` 里不许出现 focus／current_focus
   这两个词，静态那条用例直接扫源码）。
5. **不动首页那三格的形状**：`CARD_CAP`、四要素编号、`agent_take` 那一格的分工都不因
   这一批改变；待办族结构上仍然没有 ②，不因为「与重点相关」就给它编一句。

---

## 五、纪律（设计席与执行席都遵守）

1. **现有行为下笔前先读源码**（grep / read），不许写「应该如此」式断言。
2. **负控必须能变红**：摘掉守卫，正确的那条用例必须红；恒真断言比没断言更坏。
3. **定案必须落本文件**：会进代码的设计决定，同轮写进本文件，不许只说在对话里。
   **范围规则（2026-10-07 补）**：算「定案」的是**跨模块协议字段**或**持久化值**；
   单模块内部常量与显示文案**不进锚点**（否则锚点退化成第二份代码清单，谁都不来查，
   而查不到比没写更坏）。判据就问一句：这个东西被几个模块消费／它落进库吗。
4. 全量回归（按字母序跑）运行期间**不许编辑测试文件**。
5. 改变 `checks/` 命中集合的动作，台账 + 棘轮基线 + 文档必须同一次提交对齐。
6. **「只读探查生产库」的脚本一律 `mode=ro` 直连、且不 import 任何项目模块**：
   `ui/controllers/_shared.py` 在**导入期**调 `init_db()`，所以只要 import 到 `ui/`
   （或被它牵到的模块）的脚本，一落地就在生产库跑了一遍幂等建表——不写业务数据，但
   **确实改了库**，而现场一行都看不出来。取证脚本因此不 import 项目模块，直接
   `sqlite3.connect("file:...?mode=ro", uri=True)`；写错这一条的代价由生产库付。
7. **负控变异不许打真网络**：改坏之前先看这条路径能否到达 `business.model_client` 的
   咽喉点（`chat_sync`／`stream_chat`）；能到达就**先桩住再改坏**，桩不住则放弃该条变异、
   改用静态层或内存替换。本项目的负控已经真发过一次请求（返回 400、没取到任何内容），
   「测试文件里某条用例没 mock」是看不见的洞，只能靠这一条拦住。
