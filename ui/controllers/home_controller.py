# -*- coding: utf-8 -*-
"""ui.controllers.home_controller —— 🏠 首页（工作台）（P7 阶段二 B1，2026-10-17）

成功标准（设计席 B1 §八，逐字）：「用户打开首页后，十几秒内知道现在最值得关注的几件事，
并知道下一步能做什么。」

**这一格只做读**（B1 §一）：聚合三类数据 ＋ 确定性筛选 ＋ 渲染卡片。零**直接**
LLM 调用、零新增生成逻辑——三类卡片的真源都是账上**已经有**的行，本模块不新建任何行
（零 INSERT），一个模型都不问。

⚠️ **第一批「今日工作流」（2026-10-20）把上面那句「零写库」收窄出一个例外**（口径
原文＝`CORE_LOOP_BATCH1_SPEC.md` §1.4；本批按指令不动 `DESIGN_ANCHORS.md`，所以这里
只引规格、不引尚未落笔的锚点号）：首页
只放开**两个既有写口**——`goal_tasks.status`（「▶️ 开始」→ in_progress／「✅ 完成」→ done）
与 `goal_tasks.planned_date`（「📌 安排到今天」），一共三个回调
（`do_home_start_task`／`do_home_complete_task`／`do_home_reschedule_today`），每个
**只写自己那一列**，由全行对拍钉住（spec §4 第 5 条）。其余写路径（`is_home`／
`add_task`／`update_goal`／决策／机会）照旧一条都不许有；两道守卫（静态禁词与绑定区
marker 切片）同批按 spec §6 收窄，**收窄后的口径仍然有牙齿**（反向证据见
`tests/test_home_dashboard.py::test_static_write_scan_is_not_vacuous` 与新增的
`tests/test_today_region.py`）。本模块仍然不发 INSERT／DELETE，也不新建任何列。

⚠️ **B1b（2026-10-17）把「零 LLM 调用」这句契约修订成「零**直接** LLM 调用」**：发现卡的
②④ 两格读的是 AI 加工后的字段，而加工必须有人发起，所以本模块新增
`ensure_opportunities_processed`——它**只发起**，调模型与写库都在 `core.ai_filter` 里面，
本文件仍然一格不写、一句不问。这不是把旧契约放宽了事：修订后的口径由三条用例分头钉住
（`tests/test_home_dashboard.py`）——① 触发被内存替换掉之后，把本文件全部公开函数跑一遍，
逐表行数／MAX(id) 与三张真源整行仍须一字不变、两个咽喉点计数仍须为 0；② 全模块里
`ai_filter_opportunities` 这个被调用名**只能出现在 `ensure_opportunities_processed` 的函数体
内**（别处冒出来一次就是绕过触发的直接调用）；③ 触发复用的仍是 `_shared.ai_filter_state`
那**同一个 dict 对象**，没有第二套节流。静态那一头照旧：本文件的 import 里不许出现
agents／business.model_client，正文里不许出现写库函数名。

三类卡片的真源与筛选口径（B1 §二 ＋ B1b 定案）：

| 卡片 | 真源 | 筛选 | 排序 | 上限 |
|---|---|---|---|---|
| 决策 | `decisions` | status ∈ {proposed, discussed, approved} | `updated_at` 倒序 | 3 |
| 待办 | `goal_tasks` | **active 目标下的** status ∈ {pending, in_progress}（组 4，2026-10-18：进行中的那条 ③ 开头带「进行中」；done 仍在门外）∧ **`is_home` 非零**（子批 B，2026-10-19：准入制，新行默认 0、存量由迁移回填成 1） | 与当前重点粗匹配者优先 → `created_at` 倒序 | 3 |
| 发现 | `opportunities` | status = new ∧ `relevance_score` ≥ 门槛 ∧ 资格 ≠ 排除档（`core.ai_filter.fetch_home_candidates`，**含已加工过的**；`usable_assets` **不参与筛选**，B2；「与当前重点对不对得上」同样**不参与筛选**，子批 B） | 取回按 `discovered_at` 倒序，界面层再按「与当前重点粗匹配者优先 → `usable_assets` 非空优先 → `discovered_at` 倒序 → `id` 倒序」 | 3 |

「重要待办」第一版用**最近创建**近似，不做重要度排序（§二 原话），所以卡片上不印
`priority` 那个数字——它既没参与筛选也没参与排序，印上去只会让人以为排过。
子批 B（2026-10-19）在那条近似之上又加了一层「与当前重点粗匹配者优先」，它同样**不是
评分**：那一层的取值只有「对得上／对不上」两种，尺是 `core.current_focus.matches_focus_title`
（见第 9 条）。`priority` 到现在仍然既没参与筛选也没参与排序，上面那句不印数字的理由照旧。

实现读法（条数不写死，以免随批漂移——按 §五 第 6 则，需要引用时指本文件这一段）：

1. **排序与取回不同向的那些族，各有一个读出来的扫描窗口**：决策这一族的排序发生在
   这里、不在存储层——`core.decision_store.list_decisions()` 的 ORDER BY 是 `id DESC`，
   而本格锁的是 `updated_at` 倒序 ⇒ 取回之后在这儿重排，并列时按 id 倒序（两个键都确定，
   连点两次刷新不会换顺序）。扫描窗口是一档**读出来的量**：`_DECISION_SCAN_LIMIT` 取 50，
   与「🧭 项目复盘」列表同一口径（`refresh_review_panel` 的 limit 默认值）。账本长到需要
   全表扫的时候改这一个常量，不要在界面上另开第二条读取路径。待办要跨目标聚合、本来就
   在内存里排，没有这个窗口问题。**发现这一族 B2（2026-10-17）起也有了窗口**
   （`_OPPORTUNITY_SCAN_LIMIT`）：`usable_assets` 非空优先那一层必须排在取回**之后**，
   取回只给到 `CARD_CAP` 就等于让日期先淘汰一轮——一条更早、但有可直接拿走资源的机会
   根本进不了重排，「降权」当场变成「删除」。这一族的 SQL 序本身没动（`core.ai_filter.
   fetch_home_candidates` 的 ORDER BY 仍是 `discovered_at DESC, id DESC`，与加工队列共用
   一个排序键）。
   **2026-10-17 设计席定案（§五.62 偏离③）：这条「取回窗口按 id、截断窗口按
   updated_at」的不同向记为已知边界，现在不动 `core/decision_store.py` 的排序契约**
   ——decisions ≤ 50 条时没有影响，接近 50 条时「旧决策被最近更新过」才会漏在门外。
   将来的改法已记进锚点：给 `list_decisions` 加 `order_by` 参数（默认仍是 `id DESC`，
   首页传 `updated_at DESC`）。
2. **待办的来源集合是「进行中目标底下的首页状态任务」，不是全表**（组 4，2026-10-18：
   首页状态＝ pending ＋ in_progress；扩档的理由在本条末尾）：现有读取 API
   `core.goal_manager.get_tasks()` 必须带 goal_id，而 B1 §七 不许改任何既有回调、本格
   也不新增 SQL。仓库里同形状的既有先例是
   `agents/reminder_agent.py::get_today_pending_tasks`（docstring 原话「收集所有进行中
   目标中……的任务」）。⇒ 归档目标下面残留的 pending 任务不上首页——这与「现在最值得
   关注的几件事」一致。**2026-10-17 设计席定案（§五.62 偏离①）：这一收窄就是口径本身**，
   锚点与根文档**当时**按「待办 = active 目标下的 goal_tasks pending」写全，下一批不要
   再按 §二 那句字面全集读（锚点 §一.11 已由组 4 改写成两档，现状以那里为准）。
   **2026-10-18 组 4 把状态那一维从「只 pending」扩成「pending ＋ in_progress」**：目标页
   有了「▶️ 开始」按钮之后，「按了开始」＝那条从首页消失，而它是用户刚碰过的东西。
   扩的只有状态这一维——「active 目标下」这半句照 §五.62 偏离① 一字不动，done 照旧在
   门外。**这一族仍只做读**：首页不因为一条变成 in_progress 就替它做任何事。
3. **卡片上的按钮是跳转，不是写**（§三 ④ 那排按钮名与判据⑤ 的相容读法，见下面
   「点卡片」那一段注释）。
4. **卡片数据模型里那一格叫 `card_type`，不叫 `type`**：原名与 `opportunities.type`
   同名不同义，2026-10-17 设计席定案改名（§五.62 偏离④）。词表见 `CARD_TYPE_*` 那一段。
   注意 `collect_opportunity_cards` 里读 `row.get('type')` 那一行取的是**库里的列**
   （那条机会自己的粗分类），不是卡片字段，不跟着改。
5. **标签表一律复用真源，不在这里复制一份**：状态中文用
   `core.long_term_project_review.STATUS_LABELS_ZH`，作用域中文用
   `review_controller._SCOPE_LABELS_ZH`（**同一个 dict 对象**，不是抄的），资格提示与
   类别用 `core.resource_scout` 的 `ELIGIBILITY_LABELS`／`TYPE_LABELS`，报名截止文案用
   `deadline_display()`。多一处标签表就多一处会说谎的地方（口径同本目录
   `review_controller` 文件头）。
6. **错误语义照 `core.decision_store` 那三层**：读失败打印并把「这一类读不出来」印在
   对应区里，不静默退化成「没有」；本模块没有任何写路径，所以「DB 层异常照常抛」这一条
   在这里只可能来自读。
7. **自动加工（B1b，2026-10-17）的形状与两处副作用**：`ensure_opportunities_processed`
   镜像的是 `resource_controller.do_ai_filter_opportunities` 那段既有形状（同一个
   `_shared.ai_filter_state` 当闸、daemon 线程、`finally` 复位），差别只有两处，且都是
   故意的：① 它先问 `core.ai_filter.has_unprocessed_home_candidates` 才起线程——首页是
   `demo.load` 每次都跑的，不该每一次都去摸模型；② 它只置 `running`，**不清** `report`
   ／`error`，免得把资源页那一轮还没被 timer 消费的报告覆盖掉。
   两处副作用按实登记：① 加工完成后写进 `ai_filter_state["report"]`，资源页那个每 5 秒的
   timer 会把它消费成一句「✅ AI 深度筛选：候选 N 条…」——**人没点按钮也会看到那条报告**，
   它是真事（确实跑了一轮），但没人会知道是谁发起的——**2026-10-17 设计席裁定 ② 接受
   这条副作用**（不为消除一句无害提示而新造第二套状态或给报告加来源标记）；② 模型不可用
   时那批候选不落库、`ai_screened_at` 仍为空，卡片那一格因此停在「还没加工出结论」上。
   这一句原写作「正在加工，刷新后再看」，设计席裁定 ⑤ 判它「本身是谎」并授权改词——失败
   时界面并不知道是不是「正在」，而库里那一格只说「加工过没有」。裁定 ⑤ 同时要求
   **失败态与未加工态必须可分**，于是 ④ 那格现在按 `_last_processing_failed()` 分三句。
   ⚠️ 一处按实登记的限度：`error` 由资源页那个每 5 秒的 timer 消费后清成 None，
   所以失败态那句只在失败后的一个轮询周期上下可见，之后卡片退回未加工那句——
   「可分」成立、「长期停在错状态上」没有根治，两种候选改法记 §五.64 等定案。
8. **强过滤、弱删除（B2，2026-10-17）：这一页只降权，不淘汰**：`usable_assets` 为空的机会
   照旧上首页、照旧在资源页里按状态筛得到、库里的行一条不删。三层口径（不进工作台／
   归档可搜索／硬删除）写在 `DESIGN_ANCHORS.md` §四，这里只锁最容易被写反的那半句：
   **降权是排序，不是 WHERE**。仓库里的既有同形不变量有两处可以对照——
   `resource_controller.refresh_opportunities_panel` 那句「已过项只排到末尾，不删除、
   不隐藏、不改 status（本面板从不隐藏机会）」，和 `core.resource_scout.ignore_overdue`
   置 `ignored` 之后「退出『资源』格与对账单、但资源面板照旧列出」。B2 的首页排序
   就是这同一形状第三次出现，由 `tests/test_home_dashboard.py` 与
   `tests/test_ai_filter.py::test_usable_assets_is_not_a_home_filter` 两头钉住。
   空值那一格为什么**整行不显示**而不是写「（无直接可利用资源）」：`usable_assets` 的
   NULL 同时承载「还没加工／加工过但答了『无』／模型漏答」三件事（`ai_screened_at`
   只能分出一部分），任何一句「没有」都可能是假话——这与待办族「结构上就没有这一格 ⇒
   整行不出现」是同一个选择（见 `render_card` 的 docstring）。风险照实登记：看不出降权
   这件事本身，§五.64 上报给设计席。
9. **子批 B（2026-10-19）的两条：准入制与「同一把尺」**。
   ① **待办是准入制**（`is_home` 非零才上首页）。这一格只改**可见集合**，不改库里那条
      任务本身：「移出首页」既不勾完成也不改 status、更不删行，那条任务在「🎯 目标」页
      的列表里一字不变——它只是不在这一屏占位了（与第 8 条「离开主视图 ≠ 找不到」是
      同一层口径）。写这一列的**只有目标页那两个按钮**（`goals_controller.do_add_task_home`
      ／`do_remove_task_home`），本模块仍然一格不写：首页不许自己把自己准入，
      所以 `set_task_home` 这个名字在本文件的禁词表里（`tests/test_home_dashboard.py`
      的 `_FORBIDDEN_CALLS`）。存量为什么回填成 1（设计席定案）：升级那一刻人正在做的
      事就是那些，一次性把它们请出这一屏会让首页当场空掉；回填只跑一次，绑在「刚刚
      加列」那一刻，否则每次启动应用都会把人手动移出首页的那几条悄悄拉回来。
   ② **「与当前重点对不对得上」只有一把尺**，就是 `core.current_focus.matches_focus_title`
      （归一化后相等或互为子串）。两类卡片的排序用**同一个谓词**，而它同时也是候选函数
      跳过「人已经说过的重点」那条线用的谓词（`suggest_focus_candidates` 的「不覆盖已有
      重点」）。任何一处自己再写一遍「包含」判断，界面上的「相关」和候选里的「跳过」
      就会悄悄变成两件事——这是本文件第 1 段末尾那句「两处手工保持一致」在排序层的形状。
      两处排序都只是**换位置**：没有 active 重点时两类卡片的顺序与子批 B 之前一字不差，
      有重点时也不增减任何一条卡片（判据与负控见 `tests/test_task_home.py`）。
10. **第一批「今日工作流」（2026-10-20）的三组任务口径**（`CORE_LOOP_BATCH1_SPEC.md` §1.2／§1.3）：
    「今日」「往日」「持续关注」三组**互不重复**，优先级＝今日 > 往日 > 持续关注，
    按 `task.id` 去重。「今日」「往日」的**数据查询**下沉到 `core/goal_manager.py`
    的三个只读共享函数（`list_active_pending_tasks`／`get_tasks_planned_for`／
    `get_overdue_tasks`），首页与 `agents/reminder_agent.py` 都调它——「今天该做什么」
    这把尺在本仓库只有一处定义（这就是 spec §4 第 2 条说的「共用同一把尺」，取代旧的
    「两处各写一遍再对拍」）。`is_home` 过滤、去重、focus 加分排序、分组呈现**留在
    本文件**（D1 方案 B 画的边界：不借此重构 `GoalManager`／reminder 子系统）。
    ⚠️ 「今日」「往日」**不是** `CARD_TYPES` 里的卡片族：判据①「恰好三类卡片」由
    `tests/test_home_dashboard.py::test_card_types_are_exactly_three_and_registered`
    钉住四处键表（`CARD_TYPES`／`_COLLECTORS`／两张文案表）同为那三个键，所以这两组
    各走本节自己的常量与采集渲染（`GROUP_TODAY`／`GROUP_OVERDUE`／`GROUP_WATCH`、
    `_group_rows`／`collect_task_groups`／`render_task_region`）。「持续关注」仍是
    `CARD_TYPE_TASK` 那一族卡片，只是界面称呼按 §1.1 第 4 条改了、集合按 §1.3 减了重。
    一处按实登记的代价：**一次刷新里 `_group_rows()` 被取两次**（`collect_home_cards`
    经注册表取持续关注那一组、`collect_task_groups` 取今日与往日），因为「一族一 try」
    那层错误归因的形状与被用例钉住的注册表不能同时省掉。开销是每个 active 目标多两次
    `SELECT`（本地 SQLite）。要收紧应当改**注册表那一层**的取法，不在这里加缓存——
    模块级缓存会让 spec §2 那句「三组列表全部读自库、不依赖 `gr.State`」失去依据。
"""

import threading
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

# ui=3 引 core=0 是正向依赖（见 checks/check_import_direction.py 的层序）
from core import ai_filter as af
from core import current_focus as cf
from core import decision_store as ds
from core import long_term_project_review as review
from core.resource_scout import (
    ELIGIBILITY_LABELS,
    TYPE_LABELS,
    deadline_display,
)
# 同层引用（ui=3 → ui=3）：跳转要复用那两个页面自己的读取回调，不复制它们的渲染与筛选
from ui.controllers import goals_controller as gc
from ui.controllers import resource_controller as res
from ui.controllers import review_controller as rc
# ai_filter_state 是从资源页**借来的那一个 dict**（不是本模块新建的）：B1b 的自动加工
# 要复用同一套节流，再起一套就会有两个线程同时摸同一批候选。
from ui.controllers._shared import ai_filter_state, goal_manager

# ============================================================================
# 卡片类型（B1 §五 信息架构）
# ============================================================================

#: 卡片数据模型 `card_type` 的取值。**只加类型、不改首页骨架**：将来要上 risk／
#: reflection／memory，就再加一个 CARD_TYPE_*、一个采集函数、注册表里加一行，
#: `CARD_TYPES` 跟着长，渲染与跳转的形状不用动（§五 的形状；2026-10-17 B1 定案）。
CARD_TYPE_DECISION = "decision"
CARD_TYPE_TASK = "task"
CARD_TYPE_OPPORTUNITY = "opportunity"

#: 当前恰好三类（判据①「首页恰好三类卡片」的分子）。顺序就是界面上三区的顺序。
CARD_TYPES = (CARD_TYPE_DECISION, CARD_TYPE_TASK, CARD_TYPE_OPPORTUNITY)

#: ⚠️ 卡片字典里这一格**叫 `card_type`，不叫 `type`**（2026-10-17 设计席定案改名，
#: §五.62 偏离④）。原名与 `opportunities.type`（那条机会自己的粗分类，取值见
#: `core.resource_scout.TYPE_LABELS`：hackathon／competition／credit／…）同名不同义
#: ——「同一格两个含义」正是本项目反复咬过的那类坑，所以字段名分开，而不是靠读的人
#: 记住两种含义。卡片文本里那一族印成「类别」而不是「类型」，也是同一个理由。

#: 哪几族渲染 ②「Agent 怎么判断」这一行（B1 §三 ＋ B1b 修订）。**决策**：账本里有
#: `summary` 就搬，没有就明说这一格是空的；**发现**：搬 AI 加工后的 `ai_reason`，
#: 还没加工就明说没加工。**待办不在这一格里**——任务描述是任务自己的内容，把它抄成
#: Agent 的判断就是假话。用集合而不是在渲染里写 if 链：加第五族时必须在这里表态，
#: 而漏表态的表现是那一族的 ② 整行不出现、与「这格本来就空」长得一模一样，
#: 所以 `tests/test_home_dashboard.py` 一族一族数，不数总数。
_FAMILIES_WITH_TAKE = frozenset({CARD_TYPE_DECISION, CARD_TYPE_OPPORTUNITY})

# ============================================================================
# 筛选与排序口径（B1 §二）
# ============================================================================

#: 决策白名单三值。**executed／evaluated／rejected／insufficient_information 都不在
#: 这一格里**（判据④）：前三者是「已经做完／已经评过／已经否决」，第四条是复盘自己
#: 要人补信息的状态，都不该占首页那三个位置。
#: 用 `ds.STATUS_*` 常量而不是字面量，词表跟着 `decision_store` 走。
HOME_DECISION_STATUSES = (
    ds.STATUS_PROPOSED, ds.STATUS_DISCUSSED, ds.STATUS_APPROVED,
)

#: 待办的状态取值**集合**。旧形态是 `HOME_TASK_STATUS = "pending"`（单值），
#: P7 阶段二 组 4（2026-10-18）扩成两档。`goal_tasks.status` 没有集中定义的值域常量
#: （写它的是 `goal_manager.update_task_status()` 的各个调用点；界面上那排图标见
#: `goals_controller.refresh_goal_detail` 里的 status_icon 表：pending／in_progress／
#: done），所以这一格按 §二 的原话落成字面量，并在测试里把「done 的不出现」与
#: 「in_progress 出现且 ③ 带标签」两头钉住。
#: 为什么要扩：组 3 给目标页加了「▶️ 开始」按钮，pending→in_progress 从此是日常动作，
#: 只读 pending 就等于「用户刚按下的那一条从首页消失」——那是他自己刚操作过的东西，
#: 不是 §五.62 偏离① 要挡的归档噪声。done 仍在门外：勾完就该离开「等你处理」这一页。
#: 扩的是状态这一维，**目标那一维（只 active）一字未动**。
HOME_TASK_STATUSES = ("pending", "in_progress")

#: 第一批「今日工作流」（2026-10-20）首页**三组任务**的组名（spec §1.2 的
#: `T_today`／`T_overdue`／`T_watch`）。取值只在本文件与 `tests/test_today_region.py`
#: 之间流动，不进 `CARD_TYPES`、不进 `_COLLECTORS`、不进那两张卡片文案表——
#: 理由与那条被钉住的判据见文件头实现读法第 10 条。
GROUP_TODAY = "today"
GROUP_OVERDUE = "overdue"
GROUP_WATCH = "watch"

#: 三组的键（写进文档与错误文案时用的那一行的头）。与 `CARD_TYPES` 一样按顺序给，
#: `_group_rows` 的返回值与 `collect_task_groups` 的 errors 都用这三档。
_GROUP_KEYS = (GROUP_TODAY, GROUP_OVERDUE, GROUP_WATCH)

#: 概览行那四格的（取值键, 称呼）。spec §1.3 逐字：「今日 N · 持续关注 M · 决策 K · 发现 L」。
#: 刻意**不**复用 `_SUMMARY_LABEL_BY_TYPE`：那张表是三类**卡片**的称呼（「待办」「新发现」），
#: 概览这一行的词按 §1.3 定稿（「持续关注」「发现」），而且第一格取自 `groups` 而不是
#: `cards`——并成一张表就会让卡片区那一族的称呼被界面文案牵着改。
#: ⚠️ 「往日」**不在**这四格里：spec §1.3 与 R7 裁定明令「N 只统计 planned_date == 今天」，
#: 往日未完成只在今日区内作一个小分组展示，不计入概览。
_OVERVIEW_SLOTS = (
    (GROUP_TODAY, "今日"),
    (CARD_TYPE_TASK, "持续关注"),
    (CARD_TYPE_DECISION, "决策"),
    (CARD_TYPE_OPPORTUNITY, "发现"),
)

#: 今日／往日两区各自的那一句头（标题 ＋ 排序说明）。排序说明**必须与 `_group_rows()`
#: 里的排序键同向**：那一行写的是「为什么是这个顺序」，写反了界面就开始编实现的瞎话
#: （口径同文件头第 6 条；判据与负控见 `tests/test_today_region.py`）。
_GROUP_HEAD_BY_KEY = {
    GROUP_TODAY: ("今日安排", "进行中在前，再按优先级数字小者在前"),
    GROUP_OVERDUE: ("往日未完成，待重新安排",
                    "按最近过期优先——搁置很久的可能已经失效，这一组要的是"
                    "「重新判断」，不是「最重要」（spec R2）"),
}

#: 今日／往日两区的空态那一句。两组的「空」含义不同，不许共用一句：
#: 今日空＝今天没有排期（可以点「⚡ 帮我排今天」），往日空＝没有过期遗留，
#: 而**「从未排期」的那批根本不算往日**（spec §1.2：`planned_date` 为 NULL／空串
#: 不进 `T_overdue`），所以那句必须把人可能要找的东西指到「持续关注」那一组去。
_GROUP_EMPTY_HINT_BY_KEY = {
    GROUP_TODAY: "今天没有排定要做的任务。要现在排一份，点下面「⚡ 帮我排今天」"
                 "生成预览——预览不写库，只有点「✅ 确认并应用」才会写 planned_date。",
    GROUP_OVERDUE: "没有往日遗留的未完成任务。⚠️「从未排期」（planned_date 是空的）"
                   "**不算**往日，那些在下面的「🔄 持续关注」那一组里。",
}

#: ⚠️ **B1b（2026-10-17）删掉了这一格的旧形态**：原来这里是
#: `HOME_OPPORTUNITY_STATUS = OpportunityStatus.NEW`，采集时按「只看 status」筛；
#: 现在发现卡的候选集整条搬进 `core.ai_filter.fetch_home_candidates`（三条门槛
#: 与加工队列共用一个拼装点），界面上再留一个**不参与筛选**的状态常量就是说谎。
#: 「只报 NEW」这条口径仍然在，只是它的真源换成了 `ai_filter.HOME_CANDIDATE_STATUS`。

#: 每类最多几张卡（判据①「每类 ≤3 条」）。写成常量是因为测试要构造「4 条」的场景。
CARD_CAP = 3

#: 首页一次最多送几条去加工（B1b 锁定决定 ④ 的 limit=3）。为什么正好是 3：
#: 发现区最多就出 3 张卡，多送的是首页看不到的条目——那属于「替人做主」，
#: 手动批量那一头在资源页，按钮在那儿。
#: ⚠️ B2（2026-10-17）之后**「送加工的三条」与「显示的三张」不再是同一批**：加工队列
#: 按日期取最新未加工的，显示那一头先放大窗口再按 `usable_assets` 重排。所以这一常量的
#: 理由从「正好等于卡片数」变成「每轮排空 3 条、刷新几次就轮到卡上那几条」——
#: 期间卡片照实说「还没加工出结论」。数值没动，动的是它和卡片之间那句关系，
#: 按实写在这里。触发前的那一眼（`core.ai_filter.has_unprocessed_home_candidates`）问的是
#: **整族**还有没有没加工的、不看本常量，本常量只决定每轮送几条去加工——2026-10-17
#: §五.64 ⑤ 定案删掉了那个函数上不起作用的 `limit` 形参，这两句分工才写得开。
HOME_PROCESS_LIMIT = 3

#: 发现这一族的取回窗口（B2，2026-10-17）。为什么不再是 `CARD_CAP`：
#: `usable_assets` 非空优先那一层排序必须在取回**之后**做，窗口只给到 3 就是让日期先
#: 淘汰一轮，一条更早、但有可直接拿走资源的机会连重排都进不去——「降权」当场变成「删除」，
#: 而 B2 的边界明令不许（见文件头实现读法第 1、8 条）。
#: 取 50 是**照抄本文件既有的同形先例**：`_DECISION_SCAN_LIMIT` 同一档、同一个理由
#: （界面层重排 ⇒ 需要一个比卡片数大的读回窗口），另两口的口径分别是资源页 200
#: （`resource_controller.refresh_opportunities_panel`）与复盘页 50。窗口外的机会照旧
#: 在资源页里筛得到，这一页从不删东西；真需要放大时改这一个常量，不要在界面上
#: 另开第二条读取路径。已知边界与决策族那一条同形：候选多于 50 时，第 51 条往后即使
#: 有可用资源也不会出现在首页——按实登记，不在本批解决。
_OPPORTUNITY_SCAN_LIMIT = 50

#: 发现卡 ②④ 两格的占位文案。② 那一句逐字取自 B1b 锁定决定 ③；④ 那一句是
#: 2026-10-17 设计席裁定 ⑤ **改过词的**——原文「正在加工，刷新后再看」被作废，理由
#: 写在下面 `_UNPROCESSED_NEXT_STEP_TEXT` 旁边。为什么**允许**占位句而不是留空：这一族的
#: ②④ 是「有真源、只是这一次还没加工到」，与待办那种「结构上就没有这一格」是两回事
#: （见 `render_card` 里 `_FAMILIES_WITH_TAKE` 那段分工）；留空会让人以为首页漏了一格，
#: 而硬凑一句就是假话。
_UNPROCESSED_TAKE_TEXT = "（AI 还没加工这一条，稍后刷新）"

#: ⚠️ 旧文案「正在加工，刷新后再看」是一句**谎**（设计席裁定 ⑤ 的原话）：卡片渲染那一刻
#: 这一条既可能在排队，也可能刚加工失败——界面分不清两者，因为它读的是库里的
#: `ai_screened_at`，那一格只说「加工过没有」，不说「正在加工」。改成现在这句是
#: 按事实收窄：「还没加工出结论」两种情况都成立，所以它不是假话。
_UNPROCESSED_NEXT_STEP_TEXT = "（还没加工出结论，稍后刷新）"

#: 失败态那一句（设计席裁定 ⑤ 补的第二处）：读 `_shared.ai_filter_state["error"]`，
#: 非空时用它替掉上面那句。⚠️ 这一句**活不久**——资源页那个每 5 秒的 timer
#: （`resource_controller.poll_ai_filter_results`）消费完 `error` 就把它清成 None，
#: 实测见 `tests/test_home_dashboard.py` 那两条；所以「失败态」只在失败后的一轮轮询里
#: 可见，之后卡片退回「还没加工出结论」。两句**可分**这条判据仍然成立（失败那一刻分得出），
#: 但「长期停在错状态上」没有根治——口径差与两种候选改法记进 `PROJECT_STATUS.md` §五.64。
_FAILED_NEXT_STEP_TEXT = "（加工失败，模型可能不可用）"

#: ⚠️ 上面那三句里有两句只覆盖「**还没加工**」（`_UNPROCESSED_TAKE_TEXT` 与
#: `_UNPROCESSED_NEXT_STEP_TEXT`，第三句是它失败时的替身）。还有第三种形状：加工过了、
#: 但模型那一次没给这一句（`ai_reason` 存的是空串、`ai_next_step` 是 NULL，两条都存在——
#: 见 `core.ai_filter.ai_filter_opportunity` 里那句「空就 None，不替它补」）。这一态
#: 若也套用「还没加工／还没加工出结论」，就是把「问过了没答」说成「还没问」，正是本文件
#: 第 6 条点过的那种混法。这两句是执行席按事实补的最小措辞，**2026-10-17 设计席裁定 ③
#: 已采纳原话**（§五.63 那条待定案到此关闭）。
_PROCESSED_EMPTY_TAKE_TEXT = "（这条加工过了，但没有理由那一句）"
_PROCESSED_EMPTY_STEP_TEXT = "（这条加工过了，模型没给下一步）"

#: 决策的扫描窗口，见文件头第 1 条。
_DECISION_SCAN_LIMIT = 50

#: 三个跳转按钮各自把人送去的那一页。文案里出现的 Tab 标签必须与
#: `ui/app_factory.py` 里 `gr.Tab(...)` 的字面一致——测试逐个比对，漂了就红。
TAB_REVIEW = "🧭 项目复盘"
TAB_GOALS = "🎯 目标管理"
TAB_RESOURCE = "💡 资源机会"

#: 空态那一行。词表按类型给，三类共用一种形状，好让判据①那条用例数得出来。
_EMPTY_HINT_BY_TYPE = {
    CARD_TYPE_DECISION: "没有等你定的决策：账本要么空着，要么每条都已经走完执行与复盘。",
    CARD_TYPE_TASK: "「持续关注」这一组现在没有条目：要么没有已准入首页的未完成任务，"
                    "要么它们都已经在上面「📅 今日安排」那两组里（去重按 "
                    "今日 > 往日 > 持续关注，spec §1.3）。",
    CARD_TYPE_OPPORTUNITY: "没有还没处理的新发现。",
}

#: 概览那一行里每一类叫什么。与 `_EMPTY_HINT_BY_TYPE` 一样按 `CARD_TYPES` 的键给：
#: 加一族卡片要在这里加一行，漏了就是 KeyError——`test_every_card_type_has_its_words`
#: 把它变成一条明确的红而不是一次启动崩溃。
_SUMMARY_LABEL_BY_TYPE = {
    CARD_TYPE_DECISION: "决策",
    CARD_TYPE_TASK: "待办",
    CARD_TYPE_OPPORTUNITY: "新发现",
}


def _scope_label(scope: Any) -> str:
    """作用域 → 中文标签，用的就是复盘页那张表**本身**

    刻意不复制一份：`review_controller._SCOPE_LABELS_ZH` 是四值跨度的显示层，这里换
    一行措辞就会造出「同一档在两个页面上叫不同名字」那种对不上账的东西。
    表外值照实说表外（口径同那边）。
    """
    labels = rc._SCOPE_LABELS_ZH
    return labels.get(scope, f"作用域 {scope!r}（词表里没有，按原样读）")


def _status_label(status: Any) -> str:
    """决策状态 → 中文标签，取 `core.long_term_project_review.STATUS_LABELS_ZH`"""
    return review.STATUS_LABELS_ZH.get(
        status, f"表外状态 {status!r}（词表里没有，按原样读）")


# ============================================================================
# 三类卡片的采集（确定性筛选 ＋ 排序 ＋ cap）
# ============================================================================

def _home_task_rows() -> List[Dict[str, Any]]:
    """进行中目标下「**已准入首页**的待办」的原始行（未排序未截断）

    采集卡片与跳转找回 goal_id 都走这一处——两处各写一遍「列 active 目标 ＋ 按
    status 取任务」就是本项目被咬过的「两处手工保持一致」。

    组 4（2026-10-18）起筛选在**内存里**过 `HOME_TASK_STATUSES`，不再把 status 传给
    `get_tasks()`：那个参数的 SQL 是 `status = ?`，一次只能匹配一个值，照集合循环就是
    「集合每加一档、每个目标多一次查询」；而这一族本来就必须跨目标在内存里按
    `created_at` 重排（文件头第 1 条末），在这里过一道集合不新增排序责任。取回来的
    done 行当场丢掉，不进排序也不进截断。

    子批 B（2026-10-19）在同一道内存筛里再加一格 `is_home`——**准入制**：只有人在目标页
    点过「🏠 加入首页」的任务才算首页待办（判据原文＝设计席 组 5 指派「is_home=0 不上
    首页」；存量行由 `memory/database.py` 那一条一次性迁移回填成 1，所以升级那一刻
    已经在做的事不会突然从这一屏消失）。为什么不写进 SQL：`goal_manager.get_tasks()`
    没有带 is_home 的参数，为它新拼一条 SQL 就是破 B1 §七「本格不新增 SQL」那条边界，
    而这一族今天已经在内存里过 status，扫描量没有因为本批变差。
    判真值用「非零」而不是 `== 1`（口径同 `goal_manager.set_task_home` 的 docstring）：
    `int(... or 0)` 把 NULL 与空值一并读成「没准入」，因为这一列的语义是**有人才算有**，
    读不出来时必须落在「不上首页」那一侧。
    """
    rows: List[Dict[str, Any]] = []
    for goal in goal_manager.list_goals(status="active"):
        for task in goal_manager.get_tasks(goal.goal_id):
            if task.get("status") not in HOME_TASK_STATUSES:
                continue
            if int(task.get("is_home") or 0) == 0:
                continue
            merged = dict(task)
            merged["goal_title"] = goal.title
            rows.append(merged)
    return rows


def collect_decision_cards() -> List[Dict[str, Any]]:
    """决策卡：`decisions` 里 status ∈ 三值白名单，按 updated_at 倒序，取前 CARD_CAP 条

    不吞异常：DB 层异常交给 `collect_home_cards`，那边按「这一类读不出来」显示。
    """
    rows = ds.list_decisions(limit=_DECISION_SCAN_LIMIT)
    picked = [row for row in rows if row.get("status") in HOME_DECISION_STATUSES]
    # 两个排序键都是确定的：updated_at 倒序（§二），并列按 id 倒序兜住稳定性。
    picked.sort(key=lambda row: (str(row.get("updated_at") or ""), int(row["id"])),
                reverse=True)
    cards: List[Dict[str, Any]] = []
    for row in picked[:CARD_CAP]:
        summary = str(row.get("summary") or "").strip()
        cards.append({
            "card_type": CARD_TYPE_DECISION,
            "id": row["id"],
            "what_happened": str(row.get("title") or "（无标题）"),
            # ② 只认账本里的 summary；空就明说空，不拿标题凑一句「判断」出来。
            "agent_take": summary or None,
            "why_now": (f"{_status_label(row.get('status'))}｜"
                        f"{_scope_label(row.get('decision_scope'))}｜"
                        f"更新于 {str(row.get('updated_at') or '')[:16]}"),
            "can_do": f"进「{TAB_REVIEW}」看全文，批准或否决都在那一页",
            "status": row.get("status"),
            "decision_scope": row.get("decision_scope"),
        })
    return cards


def collect_task_cards() -> List[Dict[str, Any]]:
    """持续关注卡（界面上那一区 2026-10-20 起改名「🔄 持续关注」，spec §1.1 第 4 条）：
    已准入首页的 pending ＋ in_progress，**减去今日／往日两组**，
    与当前重点粗匹配者优先、其余按 created_at 倒序，取前 CARD_CAP 条

    ⚠️ 这一族的空态与旧文案不一样是有原因的：一个任务**排到今天**之后就从这一族消失，
    它没有离开账本，只是被上面那一组更具体的分类接走了（口径同文件头第 9 条①
    「离开主视图 ≠ 找不到」）。所以「这一组空了」不能写成「没有待办任务」——那是假话，
    词表在 `_EMPTY_HINT_BY_TYPE` 那一行。

    来源集合为什么要收窄到 active 目标、状态为什么要含 in_progress、为什么要过
    `is_home` 这一格，见文件头第 2 条与 `_home_task_rows`。

    排序为什么多了最前面那一层（子批 B，2026-10-19，设计席指派原文「按 Focus 粗匹配
    排序」）：这一屏只有 `CARD_CAP` 个位置，而「现在最值得关注的几件事」在有一格
    明确写着「我正在做 X」的时候，仍然把 X 那条排在一条无关任务之后是说不通的。
    ⚠️ 它是**排序，不是门槛**（锚点 §四「强过滤、弱删除」那一层，口径同 `usable_assets`
    非空优先）：没有对得上的重点时顺序一字不变，有对得上的时候也只是**换位置**，
    一条任务会不会出现在这一屏只由 `status` ＋ `is_home` 两格决定。
    判「对得上」用的是全项目唯一那把尺 `core.current_focus.matches_focus_title`
    （归一化后相等或互为子串），不在这里另写一份——分词、评分、模糊匹配都没有。
    卡片上**不印**「与重点相关」这句话：那一格只说库里有的事实，而这条顺序是界面自己
    的加工（限度按实登记在 `PROJECT_STATUS.md` §五.66）。
    """
    # 去重、那把唯一的尺与前导键都在 `_group_rows()` 里做完（文件头第 10 条），
    # 这里只负责「持续关注那一组 → 卡片」。读失败时 `_group_rows()` 照旧抛，
    # 由 `collect_home_cards` 那一层按「这一类读不出来」显示——**不许**在这里
    # 接住异常并返回空列表，那等于把读失败说成「没有」（文件头第 6 条）。
    rows = _group_rows()[GROUP_WATCH]
    cards: List[Dict[str, Any]] = []
    for row in rows[:CARD_CAP]:
        status = row.get("status")
        # 「进行中」只是 ③ 开头的一个事实前缀，**不是一张新标签表**（文件头第 5 条
        # 不许在这里复制词表）：pending 不加——③ 说的每一条本来就都没开始，加了等于
        # 没说；in_progress 加，因为它和「等你去点开始」在 ③ 里原来长得一模一样。
        prefix = "进行中｜" if status == "in_progress" else ""
        cards.append({
            "card_type": CARD_TYPE_TASK,
            "id": row["id"],
            "goal_id": row.get("goal_id"),
            "what_happened": str(row.get("title") or "（无标题）"),
            # §二 说这一族按「最近创建」近似、§三 说待办这格可省，所以这里留 None：
            # 任务描述是任务自己的内容，不是 Agent 的判断，硬凑进②就是假话。
            "agent_take": None,
            "why_now": (f"{prefix}目标「{row.get('goal_title') or '（读不到目标名）'}」｜"
                        f"创建于 {str(row.get('created_at') or '')[:16]}（UTC）"),
            "can_do": f"进「{TAB_GOALS}」把这一条勾成已完成",
            # 状态是这条任务自己的事实，照 `collect_decision_cards` 的写法存进卡片；
            # ③ 那个前缀由它推出，不在卡片里另存一份显示串（两处手工保持一致）。
            "status": status,
        })
    return cards


# ============================================================================
# 三组任务：今日／往日／持续关注（第一批「今日工作流」，2026-10-20）
# ============================================================================
#
# 口径逐字取自 `CORE_LOOP_BATCH1_SPEC.md` §1.2／§1.3。
# 为什么「今日」「往日」的**查询**在 core 而**分组**在这里：D1 定案选方案 B——
# 「今天该做什么」这把尺必须有唯一实现（首页与 `agents/reminder_agent.py` 调的是同一个
# `goal_manager.get_tasks_planned_for`），而 `is_home` 过滤、按 id 去重、focus 加分排序、
# 分组呈现是**界面**的事，搬进 core 就等于让存储层决定屏幕怎么摆。
# ⚠️ 本文件**不 import agents 层**（`_FORBIDDEN_IMPORT_ROOTS` 拦的就是这个），
# 所以「共用同一把尺」靠的是两边各自去调 core 那一个函数，不是首页去调提醒模块。

def home_today() -> str:
    """今天（`YYYY-MM-DD`）。单独成一个函数只为一件事：**用例能把时钟换掉**

    「今日」「往日」两组都由这一个字符串判，测试里把它换成昨天就能造出
    「一条昨天排期、一条今天排期」的场景，不必改库、也不必等到明天才验得出
    （同形状的既有先例＝`agents/reminder_agent.py::_today`，两边各有一份是因为
    层序不许倒过来，两份的**取值口径相同**：本机日历）。
    """
    return date.today().isoformat()


def _group_rows() -> Dict[str, List[Dict[str, Any]]]:
    """三组的**原始任务行**（已排序、未截断）；读失败照旧抛，由调用方分头记账

    三个键恒在（`_GROUP_KEYS`）。筛选口径逐字照 spec §1.2：
    - 今日＝`goal_manager.get_tasks_planned_for(home_today())`：active 目标 ∧
      `planned_date == 今天` ∧ 非 done，**不看 `is_home`**（那是 spec §0 里那条
      「矛盾 1」的修正，判据 §4 第 1 条钉住）；
    - 往日＝`goal_manager.get_overdue_tasks(home_today())`：`planned_date` 非空且
      `< 今天` ∧ 非 done；`planned_date` 为 NULL／空串的**不属于**这一组（那叫
      「从未排期」，归持续关注）；
    - 持续关注＝`_home_task_rows()` 再按 `task.id` 减去上面两组（优先级
      今日 > 往日 > 持续关注，spec §1.3）。

    三把排序键也逐字照 §1.3，都在这一处，界面上那句「按什么顺序」由
    `_GROUP_HEAD_BY_KEY` 复述：
    - 今日 `(status != 'in_progress', priority, id)` **升序** ⇒ 进行中在前、
      再优先级数字小者在前（R1）；
    - 往日 `(planned_date, id)` **降序** ⇒ 最近过期优先（R2：搁置很久的可能已失效，
      这一组要的是「重新判断」而不是「最重要」）；
    - 持续关注沿用子批 B 那把尺：`(matches_focus_title, created_at, id)` 降序——
      **本批不改 focus 加分口径**（R3，锚点 §一.11／§一.13 一字未动）。

    为什么今日与往日各调一次 core 查询、而不是取一次全表自己在内存里切：那两条查询
    是 core 里「今日／往日」的**唯一实现**，绕开它们自己在内存里切就等于在本仓库里
    再造第二把尺，而 spec §4 第 2 条要钉住的正是「两边共用同一把」。
    代价按实登记在文件头实现读法第 10 条末尾。
    """
    today = home_today()
    today_rows = goal_manager.get_tasks_planned_for(today)
    today_rows.sort(key=lambda row: (row.get("status") != "in_progress",
                                     int(row.get("priority") or 1), int(row["id"])))
    overdue_rows = goal_manager.get_overdue_tasks(today)
    overdue_rows.sort(key=lambda row: (str(row.get("planned_date") or ""), int(row["id"])),
                      reverse=True)
    taken = ({int(row["id"]) for row in today_rows}
             | {int(row["id"]) for row in overdue_rows})
    focus_texts = cf.active_focus_titles()
    watch_rows = [row for row in _home_task_rows() if int(row["id"]) not in taken]
    watch_rows.sort(key=lambda row: (cf.matches_focus_title(row.get("title"), focus_texts),
                                     str(row.get("created_at") or ""), int(row["id"])),
                    reverse=True)
    return {GROUP_TODAY: today_rows, GROUP_OVERDUE: overdue_rows,
            GROUP_WATCH: watch_rows}


def collect_task_groups() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    """一次把三组取齐；读失败按「这一组读不出来」记账，三个键恒在

    形状照 `collect_home_cards`（读失败登记而不静默）。今天这三组共用 `_group_rows()`
    那一次取数，所以一次失败会同时是三组失败——那是**事实**而不是省事：它们来自同一批
    查询，界面不假装还能拆出其中两组。「读失败」与「读到但没有」不许写成同一句话
    （文件头第 6 条），分开的方式与三类卡片一模一样：空列表 ＋ errors 里有它。
    """
    errors: Dict[str, str] = {}
    try:
        groups = _group_rows()
    except Exception as e:
        print(f"[Home] 今日／往日／持续关注读取失败（忽略）: {e}")
        groups = {}
        for key in _GROUP_KEYS:
            errors[key] = str(e)
    for key in _GROUP_KEYS:
        groups.setdefault(key, [])
    return groups, errors


def _row_choice(row: Dict[str, Any]) -> Tuple[str, Any]:
    """原始任务行 → 下拉那一条选项（显示的是标题，值就是写回调要的 id）

    与 `_choice_of` 各写一行、不合并：那一族取的是**卡片**里的 `what_happened`，
    这里取的是**库里那一行**的 `title`。并成一个函数就得在界面层造一个「卡片与原始行
    都有」的假字段——那正是本项目点过的「同一格两个含义」。
    """
    return (f"#{row.get('id')}｜{str(row.get('title') or '')[:24]}", row.get("id"))


def render_task_region(rows: List[Dict[str, Any]], group: str,
                       error: Optional[str] = None) -> str:
    """今日／往日那一区的 Markdown（判据 §4 第 1／3 条：标题必须出现在这一格里）

    三态分法与 `render_home_region` 一致（读失败／空态／有条目），但**不复用那个函数**：
    那一族渲染的是四要素卡片，这里渲染的是库里那一行的事实。今日这一族**没有**
    ②「Agent 怎么判断」那一格——任务行结构上就没有，硬印一句「（无）」反而假装它
    本来该有值（口径同 `render_card` 里待办族整行不出现那条分工）。

    每一区的最后一行是**动作说明**：这一屏会写哪一列、不写哪一列，写在条目旁边而不是
    只写在文件头的注释里，因为「开始／完成／安排到今天」这三枚按钮就长在这一区下面。
    """
    if error:
        return (f"⚠️ 这一组（{group}）读不出来：{error}\n\n"
                "刷新或看控制台；首页不替这一组编内容。")
    if not rows:
        return f"（{_GROUP_EMPTY_HINT_BY_KEY.get(group, '这一组没有可显示的内容。')}）"
    head, order_note = _GROUP_HEAD_BY_KEY.get(
        group, (f"这一组（{group}）", "顺序以 `_group_rows()` 里的排序键为准"))
    lines = [f"**{head}：{len(rows)} 条**（{order_note}）"]
    for row in rows:
        mark = ("🔄 进行中" if row.get("status") == "in_progress" else "⬜ 未完成")
        goal_title = str(row.get("goal_title") or "（读不到目标名）")
        title = str(row.get("title") or "（无标题）")
        if group == GROUP_OVERDUE:
            lines.append(f"- {mark}｜#{row.get('id')} **{title}**｜原定 "
                         f"{str(row.get('planned_date') or '')}｜目标「{goal_title}」")
        else:
            lines.append(f"- {mark}｜#{row.get('id')} **{title}**｜目标「{goal_title}」｜"
                         f"优先级 {int(row.get('priority') or 1)}")
    if group == GROUP_OVERDUE:
        lines.append("> 选中一条再点「📌 安排到今天」：它**只写这一条的 planned_date**，"
                     "不碰 status、不碰 is_home、也不改创建时间。没人点它就一直留在这一组里"
                     "（spec §3.2「不自动改任务日期」）。")
    else:
        lines.append("> 选中一条再点「▶️ 开始」或「✅ 完成」：两枚按钮**只写这一条的 "
                     "status**，计划日期、优先级、首页准入那一格都不动。这一组**不看**"
                     "「已准入首页」那一格：只要排在今天又没完成就出现在这里（spec §0 "
                     "矛盾 1）。要现在排一份，点「⚡ 帮我排今天」——预览不写库。")
    return "\n\n".join(lines)


def _last_processing_failed() -> bool:
    """最近一轮 AI 加工有没有留下失败痕迹（裁定 ⑤，2026-10-17）

    读的是资源页那**同一个 dict** 的 `error` 键，只读、不改、也不新建第二套状态——
    写入方是 `ensure_opportunities_processed` 的 `_worker`（线程起不来或整批抛异常时），
    清除方是资源页那个每 5 秒的 timer（`poll_ai_filter_results` 消费完置回 None）。
    ⚠️ 正因为清除方在别处，这一格为 True 的窗口**只有一个轮询周期上下**，
    失败态那句文案因此是「刚失败时看得见」而不是「失败期间一直看得见」；
    实测写在 `tests/test_home_dashboard.py` 的那两条用例里，口径差记 §五.64。
    """
    return bool(str(ai_filter_state.get("error") or "").strip())

def collect_opportunity_cards() -> List[Dict[str, Any]]:
    """发现卡：展示 **AI 加工后**的信息（B1b 锁定决定 ③，四要素各读哪一格如下）

    ① `title`；② `ai_reason`；③ 报名截止 ＋ 类别 ＋ 资格提示 ＋ 发现时间；
    ④ `ai_next_step`。空着的那两格在 `render_card` 里按「加工过了没给这一句」与
    「还没加工」两种情况分别说，不共用一句。B2 另起的一行「能直接拿走：…」读
    `usable_assets`，它**不占四要素的编号**（裁定 ④：四要素是判据① 的形状，不新增第五格），
    并且**只在非空时出现**。

    **原始 `summary` 不再上卡片**（这一条是 B1b 明确的改动）：那是外部资源自己的
    说明文字，与 ②「Agent 怎么判断」不是同一件事——把别人的广告词搬进那一格，
    就是把它冒充成助理的判断。原先 ④ 那句「进「💡 资源机会」看原文并标…」也让位了：
    跳转没丢，它在那三区下面的下拉＋按钮上（见「点卡片」那一段），四要素这一行
    现在回答的是「现在能做什么」，而 AI 给的那一句比一句路标更配得上这一格。

    候选集由 `core.ai_filter.fetch_home_candidates` 定——两个共享门槛与加工队列在同一处
    拼装（`_home_candidate_where`），所以「首页出现的每一条都在可被加工的门槛内」是
    **构造性**的，不在这里再筛一遍、也不在这里补一遍条件（措辞按 2026-10-17 设计席裁定 ①
    改准，与 `core/ai_filter.py` 那段是同一句话）。

    B2（2026-10-17）多出来的那一层是**排序**，不是筛选：取回 `_OPPORTUNITY_SCAN_LIMIT` 条，
    在这里按「`usable_assets` 非空优先 → `discovered_at` 倒序 → `id` 倒序」重排，再截
    `CARD_CAP`。为什么必须在界面层（窗口为什么必须大于卡片数），见文件头实现读法第 1 条；
    为什么 `usable_assets` **绝不进 WHERE**，见第 8 条与
    `tests/test_ai_filter.py::test_usable_assets_is_not_a_home_filter`。

    子批 B（2026-10-19）在最前面又加了一层「这条对得上哪条当前重点吗」。设计席指派原文
    写的是「按『是否有助 active focus』**加分排序**（只能进 ORDER BY，不许进 WHERE）」，
    两句各按字面落在这里：
    ① **「不许进 WHERE」**——这一层不新增任何门槛，一条机会会不会出现在这一屏，
       仍然只由 `fetch_home_candidates` 那三条门槛决定（`status=new` ∧ 粗筛分数 ∧ 资格档）。
       这条是原话里最硬的一半，也是 `usable_assets` 那一层已经立过的同一形状。
    ② **「进 ORDER BY」落在这界面层的内存重排，而不是 `core.ai_filter.CANDIDATE_ORDER_BY`
       那串 SQL**（本席判读，设计席可驳）。理由是现读的：那一个常量被**三条**查询共用
       （`_fetch_candidates`＝AI 加工队列、`fetch_home_candidates`＝首页、
       `has_unprocessed_home_candidates`），把重点标题的匹配写进那串 SQL 就等于让
       首页的显示偏好去改**加工队列谁先被处理**——那是本批没被要求、也没法在界面上
       解释的副作用。而 B2 早已把「显示序」定在界面层（`fetch_home_candidates` 的
       docstring 原话「这一条的返回序不等于首页的显示序」），所以这里才是显示序的住处。
    ③ **新加的键放在最前面，不是加在 `usable_assets` 之后**（本席判读，理由可验）：
       现在那三个分量以唯一的 `id` 收尾，**已经是一个全序**——任何键排在这三行之后
       永远不会被取到，「加分」当场成了一个装饰。取「先对重点、再对资源」这个次序的
       依据是这一屏只有 `CARD_CAP` 个位置，而「我正在做 X」比「这条有可直接拿走的资源」
       更接近 §一 那句成功标准里的「现在最值得关注」。两种读法都说得通，已按实登记待拍板。
    """
    rows = af.fetch_home_candidates(_OPPORTUNITY_SCAN_LIMIT)
    # 排序键的四个分量都取自库里的原始值 ＋ 那把唯一的粗尺，不在这里另加工：
    # ① 与当前重点对不对得上（True 排前面）；② 有没有可用资源（True 排前面）；
    # ③④ 仍是 SQL 那两条键，所以**并列时的相对序与取回序完全一致**——连点两次刷新
    # 不会换顺序（口径同决策族那句）。①② 都只是排序：摘掉它们，可见集合一字不变。
    focus_texts = cf.active_focus_titles()
    rows = sorted(
        rows,
        key=lambda r: (cf.matches_focus_title(r.get("title"), focus_texts),
                       bool(str(r.get("usable_assets") or "").strip()),
                       str(r.get("discovered_at") or ""),
                       int(r.get("id") or 0)),
        reverse=True,
    )[:CARD_CAP]
    cards: List[Dict[str, Any]] = []
    for row in rows:
        segments = []
        deadline_text = deadline_display(row.get("deadline"))
        if deadline_text:
            segments.append(deadline_text)
        segments.append(f"类别 {TYPE_LABELS.get(row.get('type'), '读不出')}")
        segments.append(
            f"资格提示 {ELIGIBILITY_LABELS.get(row.get('eligibility_hint'), '❓ 未知')}")
        segments.append(f"发现于 {str(row.get('discovered_at') or '')[:16]}（UTC）")
        # 「加工过」与「加工到的内容」是两件事：ai_screened_at 只说明前者。
        processed = bool(str(row.get("ai_screened_at") or "").strip())
        reason = str(row.get("ai_reason") or "").strip()
        next_step = str(row.get("ai_next_step") or "").strip()
        cards.append({
            "card_type": CARD_TYPE_OPPORTUNITY,
            "id": row["id"],
            "what_happened": str(row.get("title") or "（无标题）"),
            # ②④ 都留 None 而不是留空串：渲染层要能分清「没有这一句」与「有一句空话」，
            # 而 None 是唯一不会被 `.strip()` 之后的空串冒充的那个写法。
            "agent_take": reason or None,
            "why_now": "｜".join(segments),
            "can_do": next_step or None,
            "processed": processed,
            # usable_assets（B2，2026-10-17）：**照 `ai_next_step` 那一条口径**留 None 而不是
            # 空串——渲染层要能分清「这一格没有」与「这一格是一句空话」。这一列的 NULL
            # 承载三件事（还没加工／加工过但答了「无」那一族／模型漏答），所以 `render_card`
            # 对 None 的处理是**整行不出现**，不写「（无直接可利用资源）」那种冒充判断的话。
            "usable_assets": str(row.get("usable_assets") or "").strip() or None,
            # 库里那一格分不出「排在队里」与「刚加工失败」，所以这一态只能从
            # `_shared.ai_filter_state["error"]` 借（裁定 ⑤ 的第二句）。卡片里存的是
            # **事实**而不是解释：这一格只说「最近一轮加工留了失败痕迹」，
            # 「因此 ④ 该说哪一句」由 `render_card` 决定。
            "last_processing_failed": _last_processing_failed(),
        })
    return cards


#: 三类采集函数的注册表。**加一族就加一行**（§五 的形状），不要在渲染里写 if 链。
_COLLECTORS = {
    CARD_TYPE_DECISION: collect_decision_cards,
    CARD_TYPE_TASK: collect_task_cards,
    CARD_TYPE_OPPORTUNITY: collect_opportunity_cards,
}


def collect_home_cards() -> Tuple[Dict[str, List[Dict[str, Any]]],
                                  Dict[str, str]]:
    """一次把三类卡片取齐；某一族读失败不影响另外两族

    Returns:
        (cards, errors)：cards 是 {卡片类型: 卡片列表}，**三类键恒在**；
        errors 是 {卡片类型: 错误文案}，只含有读失败的那几族。读失败与「读到但没有」
        在卡片上都表现为空列表，靠 errors 分开——把它们混成一句「没有」正是本项目
        点过名的假话形态。
    """
    cards: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for card_type in CARD_TYPES:
        try:
            cards[card_type] = _COLLECTORS[card_type]()
        except Exception as e:
            # 不冒到 Gradio 事件循环上（口径同 review_controller 的列表读取：读没有
            # 写副作用，最坏是这一屏没刷新），但也不静默。
            print(f"[Home] {card_type} 卡片读取失败（忽略）: {e}")
            cards[card_type] = []
            errors[card_type] = str(e)
    return cards, errors


# ============================================================================
# 渲染
# ============================================================================

def render_card(card: Dict[str, Any]) -> str:
    """一张卡片的四要素（B1 §三 ＋ B1b 修订）：① 发生了什么 ② Agent 怎么判断
    ③ 为什么现在重要 ④ 现在能做什么

    四要素各占一行、标号逐字写出来：判据②「每张卡片四要素齐全」要能对着文本数。
    ② 这一格允许**合法缺省**（§三 原话「没有就不硬凑」），但三族各有各的缺法：
    **决策**账本里 `summary` 为空 ⇒ 那一行照样在、写成「（账本里这一格是空的）」；
    **发现**（B1b 起有 ② 了）⇒ 分两态，「还没加工」与「加工过了但没给这一句」
    不许套同一句话；**待办**结构上就没有这一格，所以整行**不出现**——印「（无）」
    反而假装它本来该有值。④ 同一条分工，但按裁定 ⑤（2026-10-17）**多一个态**：
    发现族空着时分「加工过了没给这一句」／「还没加工」／「最近一轮加工失败」三句，
    三句两两不许互抄；另两族永远有那句指路（`can_do` 非空）。
    B2 之后发现族可能多**第五行**「能直接拿走：…」——它**不带编号**，所以「四要素齐全」
    那条判据仍然对着 ①②③④ 数；这一行只在 `usable_assets` 非空时出现，空值整行不出现
    （理由见下面那一段注释，与待办族「结构上没有 ⇒ 不印（无）」是同一条选择）。
    """
    card_type = card.get("card_type")
    lines = [f"**① {card.get('what_happened')}**"]
    if card_type in _FAMILIES_WITH_TAKE:
        take = card.get("agent_take")
        if not take and card_type == CARD_TYPE_OPPORTUNITY:
            take = (_PROCESSED_EMPTY_TAKE_TEXT if card.get("processed")
                    else _UNPROCESSED_TAKE_TEXT)
        lines.append(f"- ② Agent 怎么判断："
                     f"{take if take else '（账本里这一格是空的）'}")
    lines.append(f"- ③ 为什么现在重要：{card.get('why_now')}")
    step = card.get("can_do")
    if not step and card_type == CARD_TYPE_OPPORTUNITY:
        # 三态分三句，不许合并（裁定 ③＋⑤）：加工过了却没给这一句 ≠ 还没加工；
        # 还没加工里，「刚失败」与「还在排队」也得分开——库里那一格分不出这两者，
        # 所以只能借 `ai_filter_state["error"]`。
        if card.get("processed"):
            step = _PROCESSED_EMPTY_STEP_TEXT
        elif card.get("last_processing_failed"):
            step = _FAILED_NEXT_STEP_TEXT
        else:
            step = _UNPROCESSED_NEXT_STEP_TEXT
    lines.append(f"- ④ 现在能做什么：{step}")
    # B2（2026-10-17）那一行：**不占四要素的编号**（裁定 ④：四要素是判据① 的形状，
    # 不新增第五格），且**只在非空时出现**。为什么不给空值一句话：`usable_assets` 的
    # NULL 同时是「还没加工」「加工过但答了『无』」「模型漏答」三种事实，任何一句
    # 「没有可用资源」都可能是假话——这与上面「待办族结构上就没有 ⇒ 整行不出现」
    # 是同一条选择，不是省事。
    assets = card.get("usable_assets")
    if card_type == CARD_TYPE_OPPORTUNITY and assets:
        lines.append(f"- 能直接拿走：{assets}")
    return "\n".join(lines)


def _choice_of(card: Dict[str, Any]) -> Tuple[str, Any]:
    """卡片 → 下拉框那一条选项（显示的是标题，值就是跳转要的 id）"""
    return (f"#{card.get('id')}｜{str(card.get('what_happened') or '')[:24]}",
            card.get("id"))


def render_home_region(cards: List[Dict[str, Any]], card_type: str,
                       error: Optional[str] = None) -> str:
    """一类卡片区（含空态与读失败那一行）"""
    if error:
        return (f"⚠️ 这一类（{card_type}）读不出来：{error}\n\n"
                "刷新或看控制台；首页不替这一类编内容。")
    if not cards:
        return f"（{_EMPTY_HINT_BY_TYPE.get(card_type, '这一类没有可显示的内容。')}）"
    return "\n\n".join(render_card(card) for card in cards)


# ============================================================================
# 自动加工触发（B1b，2026-10-17）
# ============================================================================

def ensure_opportunities_processed(limit: int = HOME_PROCESS_LIMIT) -> None:
    """发现卡要是还有没加工到的候选，就在后台起一轮 AI 深度筛选（非阻塞、无返回）

    「加工自动发生」这句目标（B1b §〇）在界面上的全部落地就是这一个函数：它**只发起**，
    调模型与写库都在 `core.ai_filter` 里面，本文件仍然一格不写、一句不问（文件头那段
    契约修订）。形状镜像 `resource_controller.do_ai_filter_opportunities`，两处故意的
    差别见文件头第 7 条：先问 `has_unprocessed_home_candidates` 才起线程；只置 `running`、
    **不清** `report`／`error`。

    三道闸门（顺序就是开销顺序，最便宜的那一眼放最前）：
    1. `ai_filter_state["running"]` 已真 ⇒ 直接返回。节流用的是资源页那**同一个 dict**，
       所以人在资源页点过「🤖 AI 深度筛选」还没跑完时，首页不会再起一轮去摸同一批候选；
    2. 首页候选族里还有没加工的吗？没有 ⇒ 直接返回（只读一眼，整族 `LIMIT 1`，
       与本函数那个 `limit` **无关**——那一格只决定闸门 3 每轮送几条去加工）；
    3. 起 daemon 线程跑 `ai_filter_opportunities(limit=limit)`，`finally` 复位 `running`。

    异常**不外抛**：本函数挂在 `demo.load` 与刷新链上，一轮后台加工起不来不该让首页那一屏
    报错——首页的内容仍然完整可读，只是 ②④ 停在占位句上。失败原因写进
    `ai_filter_state["error"]`，由资源页那个 timer 显示（那里本来就有这条展示路径，
    首页不再另造一个错误出口）。
    """
    if ai_filter_state.get("running"):
        return
    if not af.has_unprocessed_home_candidates():
        return

    def _worker():
        try:
            report = af.ai_filter_opportunities(limit=limit)
            ai_filter_state["report"] = report
            print(f"[Home] 自动加工完成：{report.get('summary_text')}")
        except Exception as e:
            ai_filter_state["error"] = str(e)
            print(f"[Home] 自动加工异常（忽略）: {e}")
        finally:
            ai_filter_state["running"] = False

    ai_filter_state["running"] = True
    threading.Thread(target=_worker, daemon=True, name="home-ai-filter").start()
    print(f"[Home] 已发起后台 AI 深度筛选（前 {limit} 条候选，非阻塞）")


#: `refresh_home_panel()` 那一组输出的别名。展开是十一格＝概览 ＋ 今日（Markdown ＋
#: 下拉）＋ 往日（同）＋ 三族卡片各一对。三个写回调返回的就是同一组，写成别名而不是
#: 把十一项类型抄四遍——抄四遍就是本项目点过的「两处手工保持一致」的第四份。
HomeOutputs = Tuple[str, str, Dict, str, Dict, str, Dict, str, Dict, str, Dict]


def refresh_home_panel() -> HomeOutputs:
    """刷新首页：概览 ＋「📅 今日安排」（今日／往日）＋ 三族卡片（持续关注／决策／发现）

    判据③「未处理项在启动时可见」在这一格落地：`demo.load` 链尾调它，重启后打开就是
    这几区，不需要人先点一次刷新（与待审批面板、复盘列表同族；第一批判据 §4 第 9 条
    要的就是这一句——今日区也在同一条链上，重启后不用点任何东西）。

    B1b 起它末尾还多一步：**发起**后台加工（`ensure_opportunities_processed`），
    所以「打开首页十几秒后能看到 AI 加工后的发现」不再依赖人去点资源页那个按钮。
    这一步在非阻塞的独立线程里跑，本次返回不等它；卡片内容取齐在前、发起在后，
    这一屏展示的仍然是**上一次**已经落库的结果（不会为了等模型而空着）。

    第一批「今日工作流」（2026-10-20，spec §1.1／§1.3）在这一格多输出**两区**
    （今日、往日），并把概览那一行换成 §1.3 那四个数。两区各自成对（Markdown ＋
    下拉框 update）而不是只给一块 Markdown，理由与那三区是同一句事实约束：
    Gradio 的 Markdown 不是可点控件，而「开始／完成／安排到今天」必须先有
    「哪一条」这一格（近似形态与它的定案见下面「点卡片」那一段）。
    ⚠️ 本函数**仍然一格不写**：写只发生在 `do_home_start_task`／`do_home_complete_task`／
    `do_home_reschedule_today` 那三个回调里（spec §1.4 开的例外），而它们写完就是回来
    调本函数刷一屏。

    Returns:
        (summary_md, today_md, today_dd, overdue_md, overdue_dd,
        watch_md, watch_dd, decision_md, decision_dd, opportunity_md, opportunity_dd)
        —— 与 `ui/app_factory.py` 首页那十一格 outputs 一一对应。今日／往日两对排在
        三族卡片之前（spec §1.1 自上而下的最终形状），三族那三对之间仍是 `CARD_TYPES`
        的顺序。
    """
    groups, group_errors = collect_task_groups()
    cards, errors = collect_home_cards()

    def _count_text(key: str) -> str:
        """概览那一格读失败时写「读不出来」，不写 0

        0 条的意思是「账上没有」，读不出来是「这一屏没数出来」——把两者写成同一句话
        正是文件头第 6 条点过的那个假话形态，而概览行是人最先看的那一行，
        只把区里换成 ⚠️ 不够。今日那一格数的是**整组行数**（今日区不截断），
        卡片那三格数的是**这一屏放得下的那几张**（`CARD_CAP` 之外的由上面那句
        「放不下的去对应那一页看全部」交代）——两种数法各自与它下面那一区一致。
        """
        if key in errors or key in group_errors:
            return "读不出来"
        if key == GROUP_TODAY:
            return str(len(groups[GROUP_TODAY]))
        return str(len(cards[key]))

    parts = [f"{_count_text(key)} {label}" for key, label in _OVERVIEW_SLOTS]
    summary = ("**现在等你看的：" + " · ".join(parts) + "。**\n\n"
               f"> 卡片那三区每类最多放 {CARD_CAP} 张，放不下的去对应那一页看全部；"
               "上面今日与往日那两区**不截断**，要点开始、要改期的都在这一屏。"
               "「往日未完成」那几条**不计进上面第一个数**（spec R7：不把历史遗留"
               "伪装成今天新安排的任务）。首页只把账上已有的东西聚到一处，"
               "**这里点不出新东西**：不新增任务、不新增决策、不新增发现。这一屏会写的"
               "只有三枚按钮——「▶️ 开始／✅ 完成」写 `status`、「📌 安排到今天」写"
               " `planned_date`，各只写选中那一条的那一列（这一屏上只有这两列能动）；"
               "要看全文、要批准、要提炼，仍然跟着卡片那一步进到原来那一页做。")

    def _region(card_type: str) -> Tuple[str, Dict]:
        region_cards = cards[card_type]
        choices = [_choice_of(card) for card in region_cards]
        return (render_home_region(region_cards, card_type, errors.get(card_type)),
                gr.update(choices=choices, value=None,
                          interactive=bool(choices)))

    def _group_region(group: str) -> Tuple[str, Dict]:
        """今日／往日那一区：Markdown ＋ 下拉（选项就是这一组的条目，值是 id）"""
        rows = groups[group]
        choices = [_row_choice(row) for row in rows]
        return (render_task_region(rows, group, group_errors.get(group)),
                gr.update(choices=choices, value=None, interactive=bool(choices)))

    today_md, today_dd = _group_region(GROUP_TODAY)
    overdue_md, overdue_dd = _group_region(GROUP_OVERDUE)
    watch_md, watch_dd = _region(CARD_TYPE_TASK)
    decision_md, decision_dd = _region(CARD_TYPE_DECISION)
    opportunity_md, opportunity_dd = _region(CARD_TYPE_OPPORTUNITY)
    # 发起后台加工放在各区都渲染完之后：这一屏该显示什么已经不取决于它了。
    # 整段包 try——触发本身起不来（比如那一眼只读查询失败）不该让已经渲染好的首页报错，
    # 最坏是 ②④ 停在占位句上，等下一次刷新。
    try:
        ensure_opportunities_processed()
    except Exception as e:
        print(f"[Home] 自动加工触发失败（忽略）: {e}")
    return (summary, today_md, today_dd, overdue_md, overdue_dd,
            watch_md, watch_dd, decision_md, decision_dd,
            opportunity_md, opportunity_dd)


# ============================================================================
# 点卡片 → 现有详情（B1 §四：复用，不新做）
# ============================================================================
#
# Gradio 的 Markdown 不是可点控件，所以「点卡片」在界面上的形状是：**每一类卡片区
# 下面一格下拉（选项就是这三张卡）＋一个跳转按钮**。跳转按钮只做一件事——把选中的
# 那条 id 装进**它原本就在的那个页面**：
#
# - 决策 → `rc.refresh_review_panel(id)` 刷复盘页的列表与下拉（下拉的 value 就是这一条），
#   界面再 `.then(rc.do_view_review_decision, …)` 跑**同一个既有回调**把全文渲染到复盘页
#   那块详情上；
# - 待办 → `gc.refresh_goal_detail(goal_id)` 刷目标页的详情与任务下拉，并把任务下拉的
#   value 设成这一条；
# - 发现 → `res.refresh_opportunities_panel(id, …)` 刷机会列表并把下拉选中这一条。
#
# 三个跳转函数**全部只读**（判据⑤）。§三 ④ 那排按钮名字里的「批准」「标记完成」因此在
# 首页上是**去处**而不是动作：按字面读成动作就会在首页写库，而判据⑤「负控塞一个写操作
# 进去测试红」没留例外分支，§四 又逐字写了「只是首页链接过去」——两句话里取相容的那一读法，
# 批准与勾完成仍然在原来那一页按原回调点。
#
# ⚠️ **这一套「卡片区＋下拉＋跳转按钮」是 B1 的近似形态，不是最终形态**（2026-10-17
# 设计席定案，§五.62 偏离②）：上面第一段那句「Gradio 的 Markdown 不是可点控件」是事实
# 约束，不是取舍。**真·可点卡片（直接点卡片本体就跳）是已知的后续项**——要做它需要换成
# `gr.HTML` ＋自定义事件，或换 UI 框架；这两条都不在 B1 的范围里（§七 不许改既有回调、
# 也不许为此引新依赖）。下一批动之前，这一格就是下拉＋按钮，别把它当成已经能点。

def do_home_jump_decision(decision_id: Optional[int]) -> Tuple[str, str, Dict]:
    """把选中的决策装进「🧭 项目复盘」那一页（只读，不推状态机）

    复用 `refresh_review_panel(decision_id)`：它本来就返回 (列表 Markdown, 下拉框
    update)，而「这条还在账上吗」由它自己判——首页不重写这个判断。

    Returns:
        (首页状态 Markdown, 复盘列表 Markdown, 复盘下拉框 update) —— 之后由界面
        `.then(do_view_review_decision, [review_select_dd], [review_detail_display])`
        接上全文那一格（既有回调，一字不改）。
    """
    if not decision_id:
        return ("⚠️ 先在首页选一张决策卡", "", gr.update())
    panel_md, dd_update = rc.refresh_review_panel(int(decision_id))
    return (f"✅ 已把 Decision #{int(decision_id)} 装到「{TAB_REVIEW}」：全文在那一页的"
            "「📄 全文回读」下面，批准／否决／标记已执行／评估／提炼都在那一页按原来的"
            "按钮点（首页不替你写）。",
            panel_md, dd_update)


def do_home_jump_task(task_id: Optional[int]) -> Tuple[str, Dict, str, List, Dict]:
    """把选中的待办装进「🎯 目标管理」那一页（只读）

    刷详情要 goal_id，而卡片下拉给的只有 task id：按 id 在**同一批首页待办**里
    （组 4，2026-10-18：那一批现在含 in_progress，跳转回调自己的形状一字未改；子批 B
    起那一批还过 `is_home`，所以这一处与卡片采集用的是同一个集合，不另开一条读取路径）。
    找回它属于哪个目标（不新增 SQL、不改 `goals_controller` 的任何回调）。找不到＝
    这条刚被勾掉、目标被归档，或它被移出了首页，照实说，一行都不改。

    Returns:
        (首页状态 Markdown, 目标下拉框 update, 目标详情 Markdown, 任务选项列表 State,
        完成任务下拉框 update) —— 与 `[home_jump_status, goal_dropdown, goal_detail,
        task_choices_state, complete_task_dropdown]` 一一对应。后三格取自
        `refresh_goal_detail(goal_id)` 的既有返回，只有下拉框的 value 是这里补的。
    """
    if not task_id:
        return ("⚠️ 先在首页选一张待办卡", gr.update(), "", [], gr.update())
    wanted = int(task_id)
    for row in _home_task_rows():
        if int(row["id"]) != wanted:
            continue
        goal_id = row.get("goal_id")
        detail_md, task_choices, dd_update = gc.refresh_goal_detail(goal_id)
        # 既有回调把下拉的 value 设成 None（它不知道人要哪一条），这里只补这一格：
        # choices 用的还是它自己算出来的那份，不重算、不复制它的筛选。
        dd = dict(dd_update) if isinstance(dd_update, dict) else {}
        dd["value"] = str(row["id"])
        return (f"✅ 已把目标「{row.get('goal_title') or goal_id}」和待办 #{wanted} 装到"
                f"「{TAB_GOALS}」：那一页的「选择要完成的任务」已经选中这一条，点它下面的"
                "「✅ 标记任务完成」才落库（首页不替你写）。",
                gr.update(value=goal_id), detail_md, task_choices, dd)
    return (f"⚠️ 待办 #{wanted} 已经不在首页那批待办里了（大概是刚被勾掉、目标被归档，"
            "或它被「🏠 移出首页」摘掉了）：一行都没改", gr.update(), "", [], gr.update())


def do_home_jump_opportunity(opportunity_id: Optional[int],
                             eligibility: str = "all",
                             status: str = "all") -> Tuple[str, str, Dict]:
    """把选中的发现装进「💡 资源机会」那一页（只读）

    `eligibility`／`status` 透传资源页当前那两个筛选下拉的值——跳转不替人改筛选。
    选中项会不会真出现在那一屏里由既有回调自己的判据决定（它只保留筛选结果里的行，
    `current_id` 不在其中就把 value 置空），首页不预判也不重复这套筛选。

    Returns:
        (首页状态 Markdown, 机会列表 Markdown, 机会下拉框 update) —— 与
        `[home_jump_status, opportunity_list_display, opportunity_select_dd]` 对应，
        后两格直接取 `refresh_opportunities_panel()` 的既有返回。
    """
    if not opportunity_id:
        return ("⚠️ 先在首页选一张发现卡", gr.update(), gr.update())
    wanted = int(opportunity_id)
    list_md, dd_update = res.refresh_opportunities_panel(
        current_id=wanted, eligibility=eligibility, status=status)
    chosen = isinstance(dd_update, dict) and dd_update.get("value") == wanted
    if chosen:
        note = (f"✅ 已把机会 #{wanted} 装到「{TAB_RESOURCE}」并选中它："
                "「感兴趣／忽略／收藏」在那一页点（首页不替你写状态）。")
    else:
        note = (f"⚠️ 机会 #{wanted} 不在资源页当前的筛选视图里（资格或状态筛选把它挡在"
                "外面了）：列表已按你现在的筛选刷新，要标它请先把那一页的筛选放宽。")
    return (note, list_md, dd_update)


# ============================================================================
# 今日那一组的三个写回调（第一批「今日工作流」，2026-10-20）
# ============================================================================
#
# spec §1.4 在「首页零写」（锚点 §一.11）上开的**唯一例外**，形状是：每个回调只写
# 自己那一列，写完刷新整屏并返回 `refresh_home_panel()` 那十一格。四条一起遵守：
# ① **只走既有写口**，不新增 SQL、不碰表结构：`status` 由
#    `goal_manager.update_task_status`、`planned_date` 由
#    `goal_manager.set_task_planned_date` 写——这两个函数本来就是目标页与速览页在用的
#    那两个，首页不养第三份实现；
# ② **各只写一列**：`is_home`／`priority`／`source`／`milestone_id`／`created_at`
#    逐列不许动，由
#    `tests/test_today_region.py::test_home_start_complete_reschedule_write_only_their_column`
#    的全行对拍钉住（spec §4 第 5 条）；
# ③ **不加确认弹窗**（spec §1.4 末条＝用户裁定 1），也不新造撤销机制：这两格都是
#    可逆的事实，改回去的路径是再选一次点另一枚按钮，不是回滚；
# ④ **不许借道**：这三枚按钮不接目标页的 `do_start_task`／`do_complete_task`，
#    也不接 `do_add_task_home`——那三个回调带着它们自己那一页的刷新形状（goal_id、
#    详情 Markdown），接到首页上就把「哪一屏的数据变了」含糊掉了。spec §6.3 把这一句
#    钉成绑定区里的第二段 marker 切片。
#
# ⚠️ 与上面那三个跳转回调的**分工**：跳转只读（把人带去别的那一页做动作），
# 这三个会写（在本屏改这一条）。`home_status` 那一格仍然只归跳转，写回调不占它——
# 写完由刷新后的那一屏自己说明结果（那条去了哪、还剩几条），不在旁边再补一句话。

def _home_task_write(task_id: Optional[int], label: str,
                     writer) -> HomeOutputs:
    """三个写回调共用的那一段：校验 id → 只写那一列 → 刷新整屏

    形状照 `goals_controller._set_task_home`（那一对也是「两个公开回调共用一段校验」）。
    空选与非数字 id **一次都不写**，只把当前这一屏原样刷出来（同
    `tests/test_task_lifecycle.py::test_empty_selection_or_bad_id_writes_nothing`
    钉的那个形状：没选中就点按钮，库里一个字都不该变）。
    `writer` 只负责「拿 int 型 task_id 写那一列」，本函数不假设它有返回值。
    """
    if not task_id:
        print(f"[Home] {label}：没有选中条目，一个字都没写")
        return refresh_home_panel()
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        print(f"[Home] {label}：id {task_id!r} 不是数字，一个字都没写")
        return refresh_home_panel()
    writer(task_id_int)
    return refresh_home_panel()


def do_home_start_task(task_id: Optional[int]) -> HomeOutputs:
    """「▶️ 开始」：只把这一条的 `status` 写成 in_progress，写完刷新首页

    写完**仍留在今日那一组**（`HOME_TASK_STATUSES` 两档都在内、今日那把尺只看
    `planned_date` 与非 done），而那条的 ③ 从此带「🔄 进行中」——刚按下按钮的那条
    不该从眼前消失（口径同组 4 把首页状态扩成两档时写的那句理由）。
    与目标页那枚一样**不做状态前置校验**：再点一次落的是同一个值，幂等。
    """
    return _home_task_write(
        task_id, "开始",
        lambda tid: goal_manager.update_task_status(tid, "in_progress"))


def do_home_complete_task(task_id: Optional[int]) -> HomeOutputs:
    """「✅ 完成」：只把这一条的 `status` 写成 done，写完刷新首页

    done 不在 `HOME_TASK_STATUSES` 里，也不在 core 那两把尺的「非 done」那一半里，
    所以这条同时离开今日／往日／持续关注三组——这是**离开「等你处理」这一屏**，
    不是离开账本：那一行还在，目标页的任务列表里照旧能看见（锚点 §四
    「离开主视图 ≠ 找不到」）。
    """
    return _home_task_write(
        task_id, "完成",
        lambda tid: goal_manager.update_task_status(tid, "done"))


def do_home_reschedule_today(task_id: Optional[int]) -> HomeOutputs:
    """「📌 安排到今天」：只把这一条的 `planned_date` 写成今天，写完刷新首页

    与上面两枚的区别只有那一列，以及 `set_task_planned_date` **返回 bool**：
    False＝库里没有这一条（比如刚被删掉），这里打印一句留痕，界面上由刷新后的那一屏
    自己说明它不在今日与往日里了——首页不猜、也不无条件回一句「已安排到今天」。
    ⚠️ 它**不碰 status**（spec §4 第 3 条③）：一条往日任务被安排到今天之后仍然是
    pending，要不要开始由人再点一次「▶️ 开始」决定。
    """

    def _write(task_id_int: int) -> None:
        if not goal_manager.set_task_planned_date(task_id_int, home_today()):
            print(f"[Home] 安排到今天：库里没有 #{task_id_int} 这一条，没写着任何行")

    return _home_task_write(task_id, "安排到今天", _write)
