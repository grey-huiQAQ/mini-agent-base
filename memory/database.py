"""
SQLite 数据库初始化 + 连接管理
"""

import sqlite3
import os
from threading import Lock

DB_PATH = os.path.join("data", "memory.db")

_lock = Lock()

# 建表 SQL（原有表 + 新增目标系统表 + agent_states 增加 goal_id）
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS learning_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT,
    -- 列默认值 0 是历史残留、实际不可达（P7 批次 12-①）：本仓写入路径从不显式省略
    -- 该列，而学习记录的缺失语义由 LearningRecorder.add_learning_record 的 None
    -- 决定（落库 NULL ＝「未评」）。不要改这行 DDL——CREATE TABLE IF NOT EXISTS
    -- 对已存在的库不生效，改它只会让新库与旧库长得不一样。
    mastery_score INTEGER DEFAULT 0,
    difficulty INTEGER DEFAULT 1,
    weak_points TEXT,
    goal_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    category TEXT,
    description TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    subject TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 1,
    due_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    parent_id INTEGER,
    prerequisites TEXT DEFAULT '[]',
    related_nodes TEXT DEFAULT '[]',
    mastery_level INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0,
    last_reviewed TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain, name)
);

CREATE TABLE IF NOT EXISTS daily_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT DEFAULT 'generated',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_date)
);

CREATE TABLE IF NOT EXISTS daily_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(review_date)
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    score REAL DEFAULT 0,
    feedback TEXT,
    is_active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_name, version)
);

CREATE TABLE IF NOT EXISTS execution_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_state_id TEXT,
    step_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    token_used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_states (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    goal TEXT,
    current_task TEXT,
    tasks TEXT,
    checkpoint_ref TEXT,
    token_budget INTEGER DEFAULT 4000,
    loaded_skills TEXT,
    status TEXT DEFAULT 'idle',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT,
    goal_id TEXT  -- 新增列
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    checkpoint_data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 目标系统表
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goal_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 1,
    source TEXT DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

CREATE TABLE IF NOT EXISTS goal_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

-- 里程碑表（PlannerAgent 分阶段学习计划，任务通过 goal_tasks.milestone_id 关联）
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    order_index INTEGER DEFAULT 0,
    duration_weeks INTEGER,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

-- 经验沉淀表（ReflectionAgent 从对话中提取的可复用策略/偏好/成功模式）
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario TEXT NOT NULL,
    strategy TEXT NOT NULL,
    effect TEXT,
    conditions TEXT,
    source_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 目标笔记索引表（v3.7.5 新增，NoteStore 使用；正文同时落盘为 Markdown 文件）
-- 纯新增表，不触碰任何现有表结构；旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT,
    title TEXT NOT NULL,
    content TEXT,
    file_path TEXT,
    tags TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 深度搜索任务表（Information Engine V0.1 新增，纯新增表，不影响现有表结构；
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建，零迁移成本）
CREATE TABLE IF NOT EXISTS search_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    result_summary TEXT
);

-- 深度搜索证据表（搜索结果临时存储：用户确认保存前只存在于本表，确认后写入笔记系统）
-- entities / claims / source_quality 为 V0.2 预留字段（本次不填充，仅建列）；
-- saved_note_id 记录"已保存为笔记"标记（未保存时为 NULL），为"仅保留标记"接口预留
CREATE TABLE IF NOT EXISTS search_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    source_url TEXT,
    source_name TEXT,
    title TEXT,
    snippet TEXT,
    confidence REAL DEFAULT 0.5,
    published_at TEXT,
    retrieved_at TIMESTAMP,
    entities TEXT,
    claims TEXT,
    source_quality TEXT,
    saved_note_id INTEGER,
    FOREIGN KEY (task_id) REFERENCES search_tasks(id)
);

-- 任务执行审批持久化表（TaskExecutor 审批流，纯新增表，不影响现有表结构）
-- 每次执行计划暂停审批时写入一条 pending 记录；重启后即使内存生成器丢失，
-- 用户仍可在 UI 查看并处理未完成的审批（确认后重新执行对应任务并落盘）。
-- detail_json 保存完整审批字典，确认时原样重放 save_approval（无损恢复）。
CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,          -- search / note
    task_id TEXT,                     -- 来源任务 ID（goal_tasks.id，可能为空）
    task_title TEXT,                  -- 任务标题（展示用）
    goal_id TEXT,                     -- 所属目标 ID
    goal_title TEXT,                  -- 所属目标标题
    content TEXT,                     -- 待审阅的 Markdown 草稿
    suggested_path TEXT,              -- 建议保存目录
    filename TEXT,                    -- 建议文件名
    status TEXT DEFAULT 'pending',    -- pending / approved / rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    detail_json TEXT                  -- 完整审批字典（JSON），确认后重放落盘
);

-- 自动化任务模板表（任务模板一键复用功能，纯新增表，不影响现有表结构）
-- trigger_type: manual（手动）/ daily（每日）/ weekly（每周）；
-- trigger_config / action_params 为 JSON 字符串，如 {"time":"09:00"} /
-- {"weekday":1,"time":"09:00"} 与 {"query":"AI竞赛"}；
-- action_type 先支持 search_and_note（搜索并整理笔记），未来可扩展；
-- auto_approve=1 时执行结果自动审批通过，否则正常产生待审批项；
-- last_run_at 记录最近一次运行时间（定时触发防重入的依据）。
CREATE TABLE IF NOT EXISTS task_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    trigger_type TEXT NOT NULL DEFAULT 'manual',  -- manual | daily | weekly
    trigger_config TEXT DEFAULT '{}',             -- JSON 字符串，如 {"time":"09:00"} 或 {"weekday":1,"time":"09:00"}
    action_type TEXT NOT NULL,                    -- 先支持 'search_and_note'，未来可扩展
    action_params TEXT DEFAULT '{}',              -- JSON 字符串，如 {"query":"AI竞赛"}
    auto_approve INTEGER DEFAULT 0,               -- 0=需审批，1=自动审批
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 模板运行记录表（P7 批次 14，2026-10-06 新增；纯新增表，不动任何现有表结构）
-- 一次自动运行一行，回答报件 v3 的四问：上次开机几点／期间到期几件／成功几件失败几件／
-- 这批行是哪一次运行从哪个库产出的。**这张表是"结果"痕迹，不是"尝试"痕迹**——
-- 与 agent_resource_log（只记发生了的调用、不记结果）的根本区别就在 status 与五个计数列。
-- trigger_source: boot（开机补算，app.py）/ cli（命令行入口 scripts/boot_compensate.py）/
--   timer（界面 60 秒定时器，只在确有执行时落行，空转不落，见 core.task_template 的写行条件）；
-- started_at / finished_at: **一律 UTC 且带 +00:00 偏移**（C9）。
--   ⚠️ 同一次运行写的 task_templates.last_run_at 仍是**本地**时间且不许改（批次 14 定案第 5 条：
--   把那一列改成 UTC 会让模板永久"到期"）——两根轴各读各的列，混用会错 8 小时；
-- status: ok（无失败）/ partial（有失败项）/ error（到期清单没读出来，此时 due_count 为 NULL）；
-- due_count: 本次读到的到期模板数；NULL 表示"没读到"，**不等于 0**（0 是"读到且没有"）；
-- ran / paused / backlog_skip / claim_skip / failed: 到期模板的去向计数，构造性满足
--   due_count == ran + paused + backlog_skip + claim_skip + failed（唯一例外是 status=error
--   那一行：清单没读出来，due_count 为 NULL、五个计数全 0）；
--   ⚠️ ran 只代表"执行通道走完且未暂停"，**不代表产物已落盘**——缺动作参数的模板也会
--   走完全程、什么也不产出，照样算 ran（批次 14 定案第 3 条明确不给结果加新键）；
-- produced_rows: 本次新建的 pending_approvals 行数（差值法），量的是"待处理产出数"，
--   **不代表已落盘**——auto_approve=0 时产物停在审批队列里，一行磁盘文件都没写；
-- budget_exhausted: 调用预算是否用尽（C2）。本表的当前生产者 scout_digest 零 LLM 零网络，
--   所以这一列今天只会是 0；字段存在是 C8 的要求（"取值今天允许只有一种，字段不许省"）；
-- db_path / db_bytes / db_pending_total: 连到了哪个库（绝对路径＋文件字节数＋该库 pending 总数），
--   为的是将来接云端时"接错库／写进平行库"这类错能在库里自己读出来。
CREATE TABLE IF NOT EXISTS template_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_source TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'ok',
    db_path TEXT,
    db_bytes INTEGER,
    db_pending_total INTEGER,
    due_count INTEGER,
    ran INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,
    backlog_skip INTEGER DEFAULT 0,
    claim_skip INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    produced_rows INTEGER DEFAULT 0,
    budget_exhausted INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);

-- 决策表（P7 第一闭环 Phase 1a，2026-10-06 新增；纯新增表，不触碰任何现有表结构，
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建）
-- 一行＝一个「值得改变的地方」的候选决策（一行一个决策，不是一行一句话）。
-- 七值 status：proposed / discussed / approved / rejected / executed / evaluated /
--   insufficient_information。**合法转换表与禁跳规则在 core/decision_store.py，这里刻意
--   不写 CHECK**：本仓库既有写法是「常量表＋写路径校验」（照 core.task_template 的
--   VALID_TRIGGERS／VALID_ACTIONS 先例）；写进 DDL 会让新旧两库的约束来源不一致，而
--   SQLite 改不了列约束，将来要调一次规则就得重建表。修订不进状态机（P9）——走
--   decision_items 追加一条 kind='revision' 的条目＋一条计数查询。
-- decision_scope（改动 1 ＋ Fix A，2026-10-07）：tactical / operational / strategic /
--   critical，默认 operational。它是**跨度词不是频率词**（改前是 daily / weekly /
--   long_term / critical，那是"多久一次"的口径）。**新表的新列**，不涉及迁移；非法取值
--   由写路径拒。⚠️ 改这一句只对**此后新建**的库生效：已经建出 decisions 的库（含生产库）
--   里这一列的 DEFAULT 仍是旧串，SQLite 改列默认值需要重建表。本批刻意不重建——生产库
--   这张表 0 行，且 `create_decision()` 永远显式传值，两侧都不经过 DEFAULT。
--   这条"DDL 默认值必须是枚举成员"的一致性由 tests/test_decision_store.py 钉住。
-- goal_id：4b 按它找候选经验（「经 decisions.goal_id 找候选」）。NULL＝不绑目标的决策，
--   这类决策在 4b 的检索里天然拿不到候选，属预期行为，不是数据缺失。
-- evaluation：4a 的产物，五值 helpful / neutral / harmful / wrong_assumption /
--   insufficient（P4）。**evaluated 态必须 evaluation 与 actual_result 同时非空**这条由
--   状态机校验、不在 DDL 里表达：SQLite 的 CHECK 看不见另一列此前的历史，写出来也拒不掉
--   "先置 evaluated、再把 actual_result 更新成空串"那条路径。
-- execution_reference：4a 回写"这次决策被执行成了什么"（笔记路径／任务 id／模板运行 id）。
-- created_at / updated_at：**一律 UTC 带 +00:00**（沿用批次 14 定案第 5 条的 C9 口径：
--   新表的列只走一根 UTC 轴，给人看的转换放到渲染层）。刻意不给 DEFAULT——CURRENT_TIMESTAMP
--   产出的串不带偏移，与 template_runs 同一处置：由写路径显式写带偏移的 UTC。
-- 不建索引：本仓库对 FK 类列（pending_approvals.run_id、notes.goal_id、goal_tasks.goal_id）
--   一律不建索引，只有 agent_resource_log／opportunities 这两张增长表建过；这两张表今天
--   零行，先按多数惯例，真长出量再单独提。
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    decision_scope TEXT NOT NULL DEFAULT 'operational',  -- tactical/operational/strategic/critical
    evaluation TEXT,
    actual_result TEXT,
    execution_reference TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- 决策条目表（与 decisions 同批新增，P7 第一闭环 Phase 1a）
-- kind **五类不许混**（改动 2），判据写在这里是因为下一个人一定会把没证据的前提登记成
-- inference，而那样它就永远不会被拿去验证：
--   fact         ＝ 可复验的观测，必须能追到一个表／文件／命令，出处记在 source 列；
--   user_report  ＝ 他本人说过的话，source 记出处（会话、笔记路径、原话所在文件）；
--   inference    ＝ **有事实支撑**的推断（例：测试失败 ⇒ 系统不稳定），支撑它的那条 fact
--                    记在 source；
--   assumption   ＝ **没有证据、但当前必须先采信**的前提（例：每周一次足够）。它和
--                    inference 都是"我想的"，**区别只在于有没有 Fact 支撑**；
--   unknown      ＝ 明确承认不知道（Phase 2 的「找不到来源就写进 unknowns，不要编」落在
--                    这一类行上）。
-- kind='revision' 不在这五类里，是修订条目：content 写"把哪一条改成什么"，**原条目不删**
--   ——删掉就让"我当时以为是什么"失去痕迹，而第一闭环要评的恰恰是这件事。
-- 同样不建 decision_id 索引（理由见上面 decisions 的注释末段）。
CREATE TABLE IF NOT EXISTS decision_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    created_at TEXT
);

-- 项目状态快照表（P7 第一闭环 Phase 2，2026-10-06 新增；纯新增表，不触碰任何现有表结构，
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建）
-- 一行＝一次采集。**只追加、永不 UPDATE**：这一列集合的用途是"当时读到的是什么"，
--   改了历史行，Phase 4a 就没法对照"我当时以为是什么"——而那正是闭环要评的东西。
-- snapshot_at：**UTC 且带 +00:00**（C9 口径，与 decisions / template_runs 同一处置；
--   不给 DEFAULT，理由见上面 decisions 那段）。
--   ⚠️ 本表读的那几张表里，notes / goal_tasks / pending_approvals / agent_resource_log 的
--   created_at 是 **UTC 但没有偏移**，test_runs.started_at 是**本地时间且没有偏移**
--   （core/test_runner.py 用 datetime.now()），三根轴的分工与"边界一律在 SQL 里算"
--   的规矩见 core/project_state.py 的模块 docstring。
-- goal_id：TEXT，对齐 goals.goal_id（那才是业务主键，goals.id 只是自增代理键）。
--   NULL＝这份快照没能为 Decision 的 goal_id 字段提供输入（比如 active 目标不止一个），
--   **不是**"这份快照不绑目标"。
-- items：JSON 数组，元素为 {"decision_field","kind","content","source"}。
--   **数组而不是以内容为键的对象**是刻意的：同一条内容以 fact 与 inference 两种 kind
--   传入时必须两条都在（拆分文档 Phase 2 判据②）。
-- unknowns：JSON 数组，元素为 {"decision_field","reason"}，含义是"这一项快照没能为
--   这个 Decision 字段提供输入，原因是 X"。它是**要给人读的输出**，不是错误码——
--   "找不到来源就登记、不要编"这条定案落在这一列上。
-- note：人写的一句话备注。刻意不给它 kind、也不混进 items，免得它被当成事实读。
-- 同样不建索引、不给 JSON 列加 CHECK（理由与上面两张表一致）。
CREATE TABLE IF NOT EXISTS project_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    goal_id TEXT,
    items TEXT NOT NULL DEFAULT '[]',
    unknowns TEXT NOT NULL DEFAULT '[]',
    note TEXT
);

-- 辅助测试运行器结果表（v0.1 新增，纯新增表，不影响现有表结构；
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建，零迁移成本）
-- status: running（运行中）/ success（全部通过）/ partial（部分失败）/ failed（全部失败）
-- details 保存 JSON 数组：每个测试文件的 {file, ok, timed_out, retried, cases, seconds, tail}
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
);

-- Agent 资源消耗监控表（v0.1 新增，纯新增表，不影响现有表结构）
-- 记录每次 LLM 调用 / 搜索调用 / 网页抓取等操作的估算 token 数（粗粒度，非精确计费）、
-- 类型与时间戳；埋点写入全程 try/except 包裹，任何失败都不影响业务主流程。
-- resource_type: llm（大模型调用）/ search（搜索调用）等；
-- operation: chat_sync / stream_chat / stream_chat_with_tools / web_search / ddgs / github …
CREATE TABLE IF NOT EXISTS agent_resource_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    operation TEXT DEFAULT '',
    token_estimate INTEGER DEFAULT 0,
    detail TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_resource_log_created
    ON agent_resource_log(created_at);

-- 外部资源机会表（Resource Scout MVP v0.1 新增，纯新增表，不影响现有表结构；
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建，零迁移成本）
-- 定位：扫描发现的学生开发者可用的外部资源（比赛/黑客松/免费额度/课程/奖学金等），
-- 仅做初步筛选与展示，不自动评分、不自动采取行动，由用户在 UI 人工标记状态。
-- type: hackathon（黑客松）/ competition（竞赛）/ credit（额度算力）/
--       course（课程）/ scholarship（奖学金资助）/ other（其他）——粗分类；
--       由 classify_type 按 title + summary 的关键词规则给出，与源无关。
-- eligibility_hint: likely_yes / likely_no / unknown——**规则轴**的确定性提示，
--       由 check_eligibility 按 title + summary + user_profile 算得（纯函数）。
--       P4-10 (2026-09-13) 语义收紧后：`student` / `online` / `global` 等
--       不可证伪的泛化词不再抬升（降为 NEUTRAL_KEYWORDS），
--       `university` / `college` / `大学生` / `高校` 等学段错配词改为判
--       likely_no（并入 INELIGIBLE_KEYWORDS）。本列**只存规则轴的原始值**；
--       UI 默认视图展示的是派生值 effective_hint(本列, ai_eligibility)
--       （任一 no → no；两侧都 yes 才 yes；否则 unknown），该派生值**不落库**。
--       实测：存量 36 行重算后 27 条原 likely_yes 全部消失——其中 19 条仅由
--       不可证伪词铸造（降为 unknown）、8 条含学段错配词（升为 likely_no）。
-- beneficiary: 受益对象（MVP 阶段统一为 'user'，为"指定学生群体"预留）；
-- status: new（新发现）/ interested（感兴趣）/ ignored（忽略）/ favorited（收藏）；
-- source_url 唯一约束：同一资源（URL）只保留一条，重复扫描自动跳过；
-- （扫描层跳过无 URL 的结果，保证唯一约束不会因空串冲突）
-- notes: 展示用备注；P4-10 (2026-09-13) 起一手源（魔搭）把官方 Category
--        类目写在这里，仅供 UI 展示，**不参与资格/相关性判断**
--        （避免类目词污染那两条关键词轴）
-- deadline: 报名截止日，'YYYY-MM-DD'（定宽，可按字典序排序；见下方迁移）。
--        P4-11 (2026-09-13)：**仅一手源提供**（当前只有 ModelScopeProvider 把
--        RegistrationDeadline 换算成本列），搜索引擎转载源拿不到这个字段。
--        **空值（NULL / ''）表示"未知"，不是"无截止日"**——两者的区别对用户
--        有意义，故不做任何存量回填（哪怕标题里有年份也不猜）。
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT DEFAULT 'other',
    source_url TEXT NOT NULL UNIQUE,
    summary TEXT,
    beneficiary TEXT DEFAULT 'user',
    status TEXT DEFAULT 'new',
    notes TEXT,
    confidence REAL DEFAULT 0.5,
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    deadline TEXT
);
CREATE INDEX IF NOT EXISTS idx_opportunities_status
    ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opportunities_discovered
    ON opportunities(discovered_at);

-- 用户画像表（Resource Scout v0.3 资格过滤引入，纯新增表，不影响现有表结构）
-- 定位：记录用户的静态属性，供资格过滤判断"硬性条件"（学校邮箱 / 年龄 /
-- 学生类型 / 地区），让过滤比纯关键词更贴近用户实际情况。
-- 约定：单行画像（id 恒为 1，CHECK 约束 + 主键冲突即覆盖）；默认值为
-- 高中生 / 无学校邮箱 / 地区中国 / 年龄未填（NULL 表示未知，跳过年龄判断）。
-- student_type: high_school（高中生）/ undergraduate（大学生）/ graduate（研究生）/
--               other（其他/在职）；
-- has_school_email: 0=无学校邮箱，1=有（许多学生补贴要求大学/学校邮箱）。
CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    age INTEGER,
    student_type TEXT NOT NULL DEFAULT 'high_school',
    has_school_email INTEGER NOT NULL DEFAULT 0,
    region TEXT NOT NULL DEFAULT '中国',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 当前重点表（P7 阶段二 组 5 子批 A，2026-10-18 新增，纯新增表，不碰任何既有表结构；
-- 旧库升级时由 CREATE TABLE IF NOT EXISTS 自动创建，零迁移成本）
-- 定位：用户**现在**正在做的那几件事。它不是目标（goals 是长期账户）、不是任务
-- （goal_tasks 是可勾完的步子），是「这几天我把注意力放在哪」这一格的人可读事实。
-- 三条按实写下的口径：
--   ① **active 最多 3 个是写路径校验，不靠 DDL**。SQLite 表达不了「同表内 status
--      ='active' 的行数 ≤3」这种跨行约束（部分索引只能约束列的唯一性、不能约束基数），
--      硬写进 DDL 只会得到一个看起来在管、实际管不住的 CHECK。判据因此落在
--      `core/current_focus.py` 的写函数上（超限 raise ValueError）。
--   ② **paused／completed 不删历史**：这一张表从不 DELETE。暂停与完成是状态推进，
--      「我上个月把哪件事停了」本身是要能回读的事实；删掉就等于把决定改成没发生过。
--   ③ `source` 两值：'explicit'＝人自己说的；'inferred'＝系统从近期活动推断出来的
--      候选被人**采纳**之后落的那一行。inferred 那一档 **必须**同时带
--      `confidence` 与 `evidence`（写路径拒绝缺这两格的 inferred 行）——推断来的东西
--      不给出处，过几天谁都不知道它凭什么在这儿。
-- 时间戳这一轴照 `goals`／`goal_tasks`：`DEFAULT CURRENT_TIMESTAMP`（UTC、不带偏移），
-- 与 `core/decision_store.py` 那句「一律 UTC 带 +00:00、由写路径显式写入」刻意不同轴。
-- 理由不是省事：本表的 `created_at`／`updated_at` 要和「🏠 首页」待办卡同屏读，
-- 而那一格的文案写的是 `created_at[:16]（UTC）`——混进一根带偏移的轴就会多出一种
-- 截 16 位截法。全项目此刻有**四根**时钟轴并存（登记在 `PROJECT_STATUS.md` §五.49：
-- `+00:00` 的 UTC／`CURRENT_TIMESTAMP` 产出的 UTC 裸串／`test_runs.started_at` 的本地裸串／
-- `goals.goal_id` 内嵌的本地时间戳串），本表走第二根，不新增第五根；
-- 本表自己的读侧只碰到前两根（候选函数读 `goal_tasks` 与 `decisions`），
-- 所以要两个不同的截止值——那是既存形状的代价，不是本批新造的。
CREATE TABLE IF NOT EXISTS current_focus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    evidence TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
"""


def init_db():
    """初始化数据库表，并进行必要的列迁移"""
    os.makedirs("data", exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(CREATE_TABLES_SQL)
        # 为已有表添加 goal_id 列（如果不存在）
        _add_column_if_not_exists(conn, "learning_records", "goal_id", "TEXT")
        _add_column_if_not_exists(conn, "tasks", "goal_id", "TEXT")
        _add_column_if_not_exists(conn, "agent_states", "goal_id", "TEXT")  # 新增迁移
        _add_column_if_not_exists(conn, "goal_tasks", "source", "TEXT DEFAULT 'manual'")  # 任务来源字段
        # PlannerAgent / 里程碑层级迁移（均为可空列或带默认值，旧数据不受影响）
        _add_column_if_not_exists(conn, "goal_tasks", "milestone_id", "INTEGER")          # 任务所属里程碑
        _add_column_if_not_exists(conn, "goals", "priority", "INTEGER DEFAULT 2")         # 目标优先级（1-3，冲突分析用）
        _add_column_if_not_exists(conn, "goals", "deadline", "TEXT")                      # 目标截止日期
        # 经验评分与淘汰机制迁移（v3.7.1）：旧数据 score/use_count 默认 0，last_used_at 为空
        _add_column_if_not_exists(conn, "experiences", "score", "INTEGER DEFAULT 0")      # 用户反馈评分（有用+1/无用-1）
        _add_column_if_not_exists(conn, "experiences", "use_count", "INTEGER DEFAULT 0")  # 被评价次数
        _add_column_if_not_exists(conn, "experiences", "last_used_at", "TEXT")            # 最近一次被评价时间
        # 每日规划增强迁移（v3.7.2）：goal_tasks.planned_date 记录任务被计划在哪一天执行
        # （可空列：旧数据为 NULL 表示从未排入每日计划，查询/展示行为不变，向后兼容）
        _add_column_if_not_exists(conn, "goal_tasks", "planned_date", "TEXT")
        # 笔记标签增强迁移（v3.7.6）：notes.tags 存储逗号分隔的标签字符串，
        # 可空列：旧笔记 tags 为 NULL/空字符串，查询/展示行为不变，向后兼容
        _add_column_if_not_exists(conn, "notes", "tags", "TEXT")
        # Resource Scout 资格提示迁移（v0.2）：opportunities.eligibility_hint
        # 为确定性关键词规则的结果（likely_yes / likely_no / unknown），
        # 仅作提示不自动忽略；带默认值，旧行自动补 'unknown'，向后兼容
        _add_column_if_not_exists(
            conn, "opportunities", "eligibility_hint", "TEXT DEFAULT 'unknown'")
        # Resource Scout 资格判断原因迁移（v0.3）：opportunities.eligibility_reason
        # 记录 (eligibility_hint, eligibility_reason) 中的原因文案（中文，
        # 说明命中哪条规则 / 哪个画像硬性条件）；带默认值，旧行自动补 ''。
        _add_column_if_not_exists(
            conn, "opportunities", "eligibility_reason", "TEXT DEFAULT ''")
        # Resource Scout 相关性粗筛 + AI 深度筛选列迁移（v0.4）：
        # relevance_*：确定性关键词粗筛结果（相关/不相关/未知 + 0~1 分数 +
        # 命中关键词 JSON {"positive":[...], "negative":[...], "soft":[...]}）；
        # ai_*：AI 深度筛选输出（手动按钮触发，不自动执行；ai_screened_at
        # 非空表示已筛过，重跑自动跳过）。全部带默认值，旧行自动补齐。
        _add_column_if_not_exists(
            conn, "opportunities", "relevance_status", "TEXT DEFAULT 'unknown'")
        _add_column_if_not_exists(
            conn, "opportunities", "relevance_score", "REAL DEFAULT 0")
        _add_column_if_not_exists(
            conn, "opportunities", "matched_keywords", "TEXT DEFAULT '[]'")
        _add_column_if_not_exists(
            conn, "opportunities", "ai_eligibility", "TEXT DEFAULT 'unknown'")
        _add_column_if_not_exists(conn, "opportunities", "ai_reason", "TEXT")
        _add_column_if_not_exists(conn, "opportunities", "ai_value_score", "REAL")
        _add_column_if_not_exists(conn, "opportunities", "ai_project_reuse", "REAL")
        _add_column_if_not_exists(
            conn, "opportunities", "ai_recommendation", "TEXT")
        _add_column_if_not_exists(conn, "opportunities", "ai_confidence", "REAL")
        _add_column_if_not_exists(conn, "opportunities", "ai_screened_at", "TEXT")
        # ai_next_step（P7 阶段二 B1b，2026-10-17）：AI 深度筛选给出的「建议下一步」那一句，
        # 首页「发现」卡的四要素 ④ 读的就是这一列。为什么单独一列而不是塞进 `ai_reason`：
        # 那两格在界面上是两个问题（②「Agent 怎么判断」／④「现在能做什么」），合并成一格
        # 就得在界面上做字符串切割，而切割永远在猜模型上次是怎么断句的。
        # **可空、且不回填存量**（与下面 deadline／notified_bucket 同一条理由：旧数据没有
        # 的信息宁可空着）；NULL 与空串的差别是**设计内的**——「这条还没加工／模型没给这一格」
        # 存 NULL，界面上照实说这一格是空的（2026-10-17 裁定 ⑤ 作废了旧那句「正在加工」，
        # 现在分三句：还没加工／加工失败／加工过了但没给这一句），不拿空串冒充一句建议。
        # 只走迁移、不写进 CREATE TABLE（口径同本项目其它新增可空列）。
        _add_column_if_not_exists(conn, "opportunities", "ai_next_step", "TEXT")
        # usable_assets（P7 阶段二 B2，2026-10-17）：AI 深度筛选给出的「这条机会里能直接
        # 拿走／利用的东西」那一段（免费额度、API、数据集、开源代码、SDK、教程、社区支持等，
        # 顿号分隔、最多 5 项）。为什么单独一列而不是塞进 `ai_reason`：② 是一句**判断**
        # （为什么值得看），这一列是一份**清单**（拿走什么）——合成一格，首页要显示清单时
        # 就得切割那句判断，而切割永远在猜模型上次怎么断句（同 `ai_next_step` 那条理由）。
        # **可空、不回填**（同上面 ai_next_step／deadline 一条理由：旧数据没有的信息宁可空着）。
        # ⚠️ NULL 在这一列上同时承载**三种**事实：还没加工／模型加工过但答了「无」那一族
        # （空值哨兵按 §五.64 ② 定案扩到八档，数组里只装一个「无」也算）／模型
        # 漏答这一格。三者在这一列里无法区分（`ai_screened_at` 只能把第一种分出来一部分），
        # 所以首页对 NULL 的处理是**整行不显示**而不是写一句「没有可用资源」——后者会把
        # 「还没问」说成「问了说没有」。这条口径由 `tests/test_home_dashboard.py` 钉住。
        # 只走迁移、不写进 CREATE TABLE（口径同本项目其它新增可空列）。
        _add_column_if_not_exists(conn, "opportunities", "usable_assets", "TEXT")
        # Resource Scout 报名截止日迁移（P4-11）：opportunities.deadline 存
        # 'YYYY-MM-DD'（定宽，可按字典序排序）。**仅一手源提供**（当前只有
        # ModelScopeProvider 从 RegistrationDeadline 换算），搜索引擎源没有
        # 这个字段。可空列：NULL / '' 表示"未知"，**不是"无截止日"**——
        # 不做任何回填（旧数据没有的信息宁可空着），故旧行自动为 NULL。
        _add_column_if_not_exists(conn, "opportunities", "deadline", "TEXT")
        # 「资源」格水位线迁移（P7 批次 3）：opportunities.notified_bucket 记**上次播报
        # 该行时它所在的桶**（'expired' / 'soon' / 'dormant'），供"只播状态变化"判断；
        # NULL ＝ 从未播报。可空列，照既有惯例只走迁移、不写进 CREATE TABLE。
        # **不做存量回填**（与上面 deadline 同一条理由：不猜）。后果是设计内的：
        # 升级后**第一期报告会把当前所有桶非 far、且符合条件的条目播一遍**，
        # 第二期才安静下来——这是"从零开始记水位线"的必然表现，不是 bug。
        _add_column_if_not_exists(conn, "opportunities", "notified_bucket", "TEXT")
        # 一次运行一行归属迁移（P7 批次 14，2026-10-06）：pending_approvals.run_id 指回
        # 产生它的 template_runs.id，使 C4「撤销单位＝一次运行」在库里成立——按 run_id
        # 能把那一次跑写进去的行整体找出来。可空列：NULL ＝ 本批之前的历史行／手动运行
        # （界面「▶️ 运行」不经 check_scheduled_templates，无归属）／timer 空转。
        # **不做存量回填**（与上面 deadline、notified_bucket 同一条理由：不猜）。
        _add_column_if_not_exists(conn, "pending_approvals", "run_id", "INTEGER")
        # future_effect（P7 第一闭环 Phase 1a，2026-10-06）：这条经验「打算在未来造成什么
        # 改变」。4b 的注入段**只读这一列**（只注入 future_effect，不注入全文，硬长度上限
        # 在 4b 里做），所以它是全批唯一会进每轮 system prompt 的新字段。
        # 设计成可空、且**本阶段没有任何写路径碰它**：SQLite 加列不可逆（没有干净的
        # drop column），只有"空着也完全无害"才敢现在就加。
        # **不做存量回填**（与上面 deadline、notified_bucket、run_id 同一条理由：不猜）。
        # 于是 1a 的验收判据②就是它的安全证明：加列后 experiences 现有各行的**旧列取值
        # 必须逐列一致**——这是"加列不改变既有数据含义"的唯一证据，靠比对做，不靠推理。
        _add_column_if_not_exists(conn, "experiences", "future_effect", "TEXT")
        # decision_id（P7 第一闭环 Phase 4a，2026-10-07，定案见 DESIGN_ANCHORS.md §一.8）：
        # 这条经验**从哪次决策评估来**——单向链接，只放在「经验」这一侧。为什么单向：
        # 因果是单箭头（一条经验只有一条来路，一个决策可以提炼出多条经验），放反方向要么
        # 得存多值、要么得建中间表，而 4a 要的只是「这条经验从哪来」。
        # **不做 `decisions.experience_id`、不加中间表、不复用 `execution_reference`**：
        # 那一列的语义是「执行了什么」（笔记路径／模板运行 id），装进「学到了什么」会把两件
        # 事压进同一格，将来谁都读不准。
        # ⚠️ 别和既有列混：`decision_items.decision_id` 是「条目 → 决策」的 NOT NULL 外键，
        # 不是这一列。本列**可空**，NULL ＝ v3.7.1 对话反思那批 legacy 经验，本来就没有来路。
        # 与上面 future_effect／deadline／notified_bucket／run_id 同一条理由：**不回填存量**
        # （旧数据没有的信息宁可空着）。4b 注入读的是 future_effect，本列今天只用于溯源。
        _add_column_if_not_exists(conn, "experiences", "decision_id", "INTEGER")
        # is_home（P7 阶段二 组 5 子批 B，2026-10-19）：这条任务**准不准上首页**。
        # 为什么是「准入制」而不是「默认都上」：首页那三格是「现在最值得关注的几件事」，
        # 一条自动生成任务的批量落地（`do_generate_tasks` 一次能写十几条）如果默认上首页，
        # 就把这一屏变成了列表的镜像——设计席 组 5 指派里的判据正是「is_home=0 不上首页」，
        # 判据要成立，新行的默认值只能是 0（`add_task` 的 INSERT 不写这一列，走 DEFAULT）。
        # ⚠️ 本列是**唯一带回填的** goal_tasks 迁移，且回填**只跑一次**：上面那批可空列
        # （planned_date／ai_next_step／deadline／run_id／future_effect／decision_id）全部
        # 明令「不回填存量（旧数据没有的信息宁可空着）」，这一列相反——设计席已定案
        # 「存量回填成 1」，理由是升级那一刻已经在做的任务就是那些人的「现在」，把它们
        # 一次性请出首页会让人打开应用时发现那一屏空了。
        # 但回填**必须**绑在「刚刚加列」那一刻：SQLite 的 `ALTER TABLE ADD COLUMN` 会给
        # 既有行填**默认值**（这里是 0）而不是 NULL，所以 `WHERE is_home IS NULL` 一句也
        # 匹配不到；而无条件的 `SET is_home = 1` 若每次 `init_db()` 都跑，就会在每次启动
        # 应用时把用户手动「移出首页」的那几条悄悄拉回来——那不是回填，是每次启动改一次账。
        # 因此这里的判据是 `_add_column_if_not_exists` 的返回值（True＝本次真的加了列），
        # 该函数原先不返回任何东西、既有迁移调用全部忽略返回值 ⇒ 加返回值零行为变更。
        # 这条不变量由 `tests/test_task_home.py` 钉住（第二次 init_db 不许复活）。
        if _add_column_if_not_exists(conn, "goal_tasks", "is_home",
                                     "INTEGER DEFAULT 0"):
            conn.execute("UPDATE goal_tasks SET is_home = 1")
        # 用户画像默认行（v0.3）：单行 id=1；INSERT OR IGNORE 只在首次建表后
        # 写入默认值，用户已保存过的画像不会被重复初始化覆盖（迁移幂等）。
        conn.execute(
            "INSERT OR IGNORE INTO user_profile "
            "(id, age, student_type, has_school_email, region) "
            "VALUES (1, NULL, 'high_school', 0, '中国')")
        conn.commit()
        conn.close()


def get_db():
    """获取数据库连接（带行工厂）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_connection():
    """获取数据库连接（简化接口）"""
    return get_db()


def _add_column_if_not_exists(conn, table, column, col_type):
    """如果列不存在，则添加（用于迁移）

    Returns:
        True ＝ 本次调用**真的**加了列（表里原先没有）；False ＝ 列已存在、什么都没做。
        既有调用全部忽略返回值（幂等语义不变），加这个返回值只为一件事：让「只在加列
        那一刻跑一次」的存量回填有地方判断（现读 `init_db()` 里的 is_home 那一段）。
    """
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        return True
    return False


# ==================== 用户画像（Resource Scout v0.3 资格过滤） ====================
# 画像为单行存储（id=1），读写统一走本文件的两个函数，避免各模块各写一套。
# 键与返回值约定（与 core/resource_scout.normalize_profile 保持一致）：
#     age: int 或 None（未知/未填；为 None 时资格过滤跳过年龄判断）
#     student_type: high_school / undergraduate / graduate / other
#     has_school_email: bool（是否有学校/教育邮箱）
#     region: str（自由文本地区，默认 '中国'）

#: 默认用户画像（未显式设置任何字段时的兜底值：高中生、无学校邮箱、中国）
DEFAULT_USER_PROFILE = {
    "age": None,
    "student_type": "high_school",
    "has_school_email": False,
    "region": "中国",
}

#: 合法学生类型（与 core/resource_scout 中类型标签一一对应）
VALID_STUDENT_TYPES = ("high_school", "undergraduate", "graduate", "other")


def get_user_profile() -> dict:
    """读取用户画像（单行 id=1）

    行不存在（表尚未初始化等异常情况）时返回默认画像，绝不抛异常；
    返回 dict 键见模块 docstring（DEFAULT_USER_PROFILE）。

    Returns:
        {"age": int|None, "student_type": str,
         "has_school_email": bool, "region": str}
    """
    conn = get_db()
    try:
        try:
            row = conn.execute(
                "SELECT age, student_type, has_school_email, region "
                "FROM user_profile WHERE id = 1"
            ).fetchone()
        except Exception:
            row = None
    finally:
        conn.close()
    if row is None:
        return dict(DEFAULT_USER_PROFILE)
    student_type = row["student_type"]
    return {
        "age": row["age"],
        "student_type": (student_type if student_type in VALID_STUDENT_TYPES
                         else DEFAULT_USER_PROFILE["student_type"]),
        "has_school_email": bool(row["has_school_email"]),
        "region": (row["region"] if row["region"] else
                   DEFAULT_USER_PROFILE["region"]),
    }


def update_user_profile(age=None, student_type=None,
                        has_school_email=None, region=None) -> dict:
    """更新用户画像（单行 id=1 upsert；只修改显式传入的参数）

    参数语义：None = 保持原值不变（不清空）；age 传非 None 时取整，
    非正整数按"清空（置 None）"处理；region 传空白串回退 '中国'；
    has_school_email 接受 bool / 0 / 1 / '0' / '1' / 'true' / 'false'；
    student_type 必须是 VALID_STUDENT_TYPES 之一，否则抛 ValueError
    （UI 层应先校验再落库，这里做最后一道防线）。

    Returns:
        更新后的完整画像 dict（同 get_user_profile）
    """
    current = get_user_profile()
    # 年龄：None → 保持原值；否则转 int，<=0 视为"未填"置 None
    if age is not None:
        try:
            age_val = int(float(age))
            current["age"] = age_val if age_val > 0 else None
        except (TypeError, ValueError):
            current["age"] = None
    # 学生类型：None → 保持原值；非法值直接抛错（调用方应已校验）
    if student_type is not None:
        if student_type not in VALID_STUDENT_TYPES:
            raise ValueError(
                f"非法学生类型: {student_type}（应为 "
                f"{'/'.join(VALID_STUDENT_TYPES)}）")
        current["student_type"] = student_type
    # 是否有学校邮箱：None → 保持原值；否则统一转 bool
    if has_school_email is not None:
        if isinstance(has_school_email, str):
            has_school_email = has_school_email.strip().lower() in (
                "1", "true", "yes", "y", "是", "有")
        current["has_school_email"] = bool(has_school_email)
    # 地区：None → 保持原值；空白回退默认 '中国'
    if region is not None:
        region = (region or "").strip()
        current["region"] = region or DEFAULT_USER_PROFILE["region"]
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_profile "
            "(id, age, student_type, has_school_email, region, updated_at) "
            "VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET "
            "age = excluded.age, "
            "student_type = excluded.student_type, "
            "has_school_email = excluded.has_school_email, "
            "region = excluded.region, "
            "updated_at = CURRENT_TIMESTAMP",
            (current["age"], current["student_type"],
             int(current["has_school_email"]), current["region"]),
        )
        conn.commit()
    finally:
        conn.close()
    return get_user_profile()