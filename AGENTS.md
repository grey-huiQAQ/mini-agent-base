# AGENTS.md —— AI 协作规范（项目根目录，单一可信入口之一）

> 本文档面向**后续接手本项目的 AI/人类开发者**，与 `PROJECT_STATUS.md`（项目状态）、`README.md`（快速上手）配合阅读。修改代码前请先读本文档与 `PROJECT_STATUS.md`。

## 项目定位与设计哲学

**私人学习助手（Personal Agent Harness）**：基于 ModelScope 与 Gradio 的个人 AI 学习与生活助理（长期记忆、目标管理、技能扩展、任务执行自动化、RAG 检索、深度搜索与资源机会发现）。

设计哲学：
- **辅助型自主**：系统主动发现与建议，最终决策权在用户；
- **渐进式开发**：小步、可验证、先立护栏再演进；
- **中文约定**：代码注释、docstring、输出文案、文档一律使用中文；
- **重要操作需用户确认**：涉及破坏性、全局性或规范变更前先与用户确认。

## 机械检查（pre-commit 七项守卫）

仓库在 `checks/` 下维护七套 AST/静态检查脚本，已接入 Git pre-commit 钩子，**每次 `git commit` 前自动按序运行**；任一脚本退出码非零即阻止提交：

| 顺序 | 脚本 | 拦截目标 | 豁免台账 |
|---|---|---|---|
| 1 | `checks/check_duplicate_names.py` | 新增的跨模块重复公共名（顶层函数/类） | `checks/duplicate_name_exemptions.json` |
| 2 | `checks/check_no_dynamic_namespace.py` | 新增动态命名空间操作（globals/locals/vars/exec/eval/compile/__import__/模块 setattr） | `checks/dynamic_namespace_exemptions.json` |
| 3 | `checks/check_comment_freshness.py` | 新增无日期/版本标记的 TODO/需要实现/后续/预留 等注释与 docstring | `checks/comment_freshness_exemptions.json` |
| 4 | `checks/check_test_db_isolation.py` | tests/ 下“使用 DB 但未隔离”的测试文件（须调 `setup_test_db()`、切换 `DB_PATH`，或用 `patch("memory.database.DB_PATH", …)` 这类**字符串靶子**逐测隔离） | `checks/test_db_isolation_exemptions.json` |
| 5 | `checks/check_import_direction.py` | 跨层依赖倒置（低层 import 高层、生产代码 import 测试包） | `checks/import_direction_exemptions.json` |
| 6 | `checks/check_doc_integrity.py` | 根文档的**拼接事故**：同一段落新旧两版并存（重复长片段）、章节/列表编号断裂 | `checks/doc_integrity_exemptions.json` |
| 7 | `checks/check_debt_ratchet.py` | 台账条数与 `checks/debt_baseline.json` 不符（增长 = 债务涨；减少 = 基线未收紧） | `checks/debt_baseline.json`（基线本身） |

**第 6 项（P6-6①，2026-09-13）**：`PROJECT_STATUS.md` 已**第三次**出现同一形态的拼接事故——一次编辑把同一段落的新旧两版都留下（新版紧跟旧版、或以游离标点开头）。根文档（份数以复跑为准）是本项目的单一可信参考，被拼坏一次就是所有人照着错的读。检测口径是“两段长文本 80% 重合”与“编号断裂”这两件可计算的事，扫描范围仅**项目根目录下的 `*.md`**（不含 `tests/TEST_RESULTS.md`——它由回归自动重写，扫进来会让命中集合随“上次跑没跑回归”漂移）。

**台账条目的格式要求（P5-1 起会拦提交）**：每个条目必须是对象 `{"name"|"key", "reason", "since"}`。**裸字符串、或对象缺 `reason`/`since`，都会打印 `[EXEMPTION_NO_REASON]` 并以退出码 1 失败**——写不出理由的豁免无法被质疑，只能被整体删或整体留。发射点是 `checks/exemption_loader.py` 的 `collect_keys()`（五个检查器共用），**改这一处即覆盖五个检查器**；台账格式非法（非数组 / 条目类型不对 / 缺 name|key）仍走既有的退出码 2（fail-closed）。

三个共同性质：① **台账过期（`[STALE_EXEMPTION]`）同样计入退出码 1**——台账只允许随债务一起减，不允许沉淀死条目；② 第 7 项是**棘轮**，收紧基线必须与台账改动在同一次提交内（写回命令见下），使"烧减"在 diff 里留下 `-N` 证据；③ 棘轮在钩子内**只校验不写回**（钩子运行于 `git add` 之后，写回不会进本次提交）。

手动运行（定位失败原因时）：

```powershell
.\.venv\Scripts\python.exe checks\check_duplicate_names.py
.\.venv\Scripts\python.exe checks\check_no_dynamic_namespace.py
.\.venv\Scripts\python.exe checks\check_comment_freshness.py
.\.venv\Scripts\python.exe checks\check_test_db_isolation.py
.\.venv\Scripts\python.exe checks\check_import_direction.py
.\.venv\Scripts\python.exe checks\check_doc_integrity.py
.\.venv\Scripts\python.exe checks\check_debt_ratchet.py

# 收紧棘轮基线（棘轮报"基线未收紧"时执行，再 git add）
.\.venv\Scripts\python.exe scripts\snapshot_state.py --update-baseline
```

钩子本体：`scripts/pre_commit_hook.py`（跟踪）；`.git/hooks/pre-commit`（非跟踪）由 `scripts/install_pre_commit.ps1` 生成。克隆仓库后请先执行安装脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_pre_commit.ps1
```

## 禁止绕过规范（必须遵守）

1. **禁止使用 `git commit --no-verify`（或 `--no-gpg-sign` 等任何绕过方式）跳过 pre-commit 检查**。钩子形同虚设即失去意义；被拦截说明有真实债务或误报需要处理。
2. 检查失败时的正确处置顺序：
   - **优先修复代码**，使检查真实通过；
   - 若确认为**误报**（检查器盲区/历史设计使然），才允许将命中项加入**对应的** `checks/*_exemptions.json` 豁免台账，并在**该次 commit message 中说明原因**（一句话即可，如“test_resource_log.py 以逐测 patch 隔离，属检查盲区，入豁免”）。
3. 豁免台账**不允许无理由随意添加**；台账条目遵循各检查器的语义（按名字 / 按“文件:行号” / 按文件名），行号类豁免随行号变动失效，属有意设计（强迫重构时正面处理债务）。
4. 新增检查脚本一律放入 `checks/`，命名 `check_<主题>.py`，并遵循既有模式：仅标准库、中文注释、`sys.exit(main())`、UTF-8 输出兜底、扫描跳过 `data/.venv/venv/__pycache__/.git/node_modules/checks` 与隐藏目录、豁免台账格式非法时 fail-closed。

## 新检查器落地惯例（P4-1g，2026-09-12）

新增一个检查器 = **同一次提交**内完成下面五件事。缺哪件都会留下"中间态永远红着"的局面，而一个每次都红的门禁会在两三次之内被绕掉（本项目已明令禁止 `--no-verify`，所以必须负责让红只在真该红的时候出现）。

1. **首扫全量命中写入台账**：先跑一次，把当次全部命中登记为新台账。条目必须是带 `reason`（为什么可以放行）与 `since`（何时登记）的**对象格式**，不是裸字符串——一个写了理由的豁免将来能被质疑，裸字符串只能被整体删或整体留。解析统一走 `checks/exemption_loader.py` 的 `collect_keys()`（**不要在各检查器里各写一份**：本项目已两次被"两处手工复制保持一致"咬到）：裸字符串仍可读但会打印 `[EXEMPTION_NO_REASON]` 并**以退出码 1 失败（P5-1 起已生效）**。
2. **台账先入棘轮基线**（`checks/debt_baseline.json`），**再接 `CHECKS`**。顺序反了，棘轮会把"新建台账"当成"债务增长"，两个机制互相锁死。
3. **接进 `scripts/pre_commit_hook.py` 的 `CHECKS` 列表**（并同步该文件的文档字符串）。不接 = 永不自动运行，等于手工脚本。
4. **同步四处文档**：本文件上方的检查器表、`PROJECT_STATUS.md` §八 检查器一览、该检查器自己的 docstring（目的 / 检测口径 / 豁免机制 / 输出词根 / 退出码）、以及 `scripts/snapshot_state.py` 的 `LEDGER_FILES`（否则快照数不到这本台账）。
5. **commit message 写明台账前后条数**；纯行号位移写"条数不变、N 条位移"。

### 台账文件编辑注意（P4-1d 实测）

台账是 JSON：读取端用 `utf-8-sig` 容忍 BOM，写入端**不要**产出 BOM。两个已实测的坑：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM（检查器随即以退出码 2 拒绝全部提交）；Python 的 `Path.write_text()` 在 Windows 会把 `\n` 翻成 `\r\n`（工作区变脏，`git diff` 却看不到）。改台账请用能精确控制字节的写入方式，改完跑一次 `git status --porcelain` 确认干净。

### 元规则：清单里的行号必须复跑

任何"某文件某行是什么"的断言，执行前必须自己跑一次对应命令确认，**不能因为"上一轮审查已核验"而跳过**。本项目已被同一模式咬过两次：`_chunk_text`「多处复制」源自 `checks/check_duplicate_names.py` docstring 里的**假设性举例**，被 `PROJECT_STATUS.md` §六.1 转抄、再被执行清单第三次转抄，一句虚构例子变成了待办；`test_runs.details[].tail` 的「GBK 乱码」是一次坏测量被写进报告、又被上升成方法论。**未被复测的数字经过一次转抄获得权威，经过两次转抄变成工作项。**

### 耦合总则（约束 7）

任何会改变「命中集合」的动作——改检查规则、改被扫描的代码、移动文件、新增检查器、删掉被豁免的那一行——必须在**同一次提交**内把三处一起对齐：**台账、棘轮基线、文档**。做不到，就把这两个动作合并成一个子项，不要留下中间态。

## 动态命名空间存量说明（2026-09-12：12 → 8 → 1）

Phase 3b 已删除 `ui/app_factory.py` 的 `vars()+globals()` 运行时注入与 `app.py` 的 `globals().get(...)`，动态命名空间操作由 12 处降至 8 处；**P4-5b 又把它降到 1 处——方式是"还一行债"，不是"给 8 条造一个更稳的键"**：

- `tests/test_planning_mirror.py` 的 6 处 `__import__("datetime")`：该文件顶层补 `from datetime import date, timedelta` 后改用 `date.today()` / `timedelta(days=N)`，**零行为变更**（同时删掉函数内那处冗余的局部 `from datetime import date`）；
- `tests/test_task_executor.py:389` 的 `__import__("memory.note_store", fromlist=["NoteStore"])`：改为函数内普通 `from memory.note_store import NoteStore`。

收益不是"少 7 条豁免"，而是**代码本来就不该这么写**——这两种写法的唯一动机只是"不想在模块顶部加一行 import"。这就是"一本每条都能用一行改动清零的台账，不值得为它设计坐标系"的实证（同 P4-5a0 收窄关键词表的判断）。

| # | 位置 | 操作 | 存在理由（如实记录） |
|---|---|---|---|
| 1 | `business/model_client.py:232` | `locals().get("response_text", "")` | `stream_chat_with_tools` 的 `finally` 资源埋点：生成器可能在 `create()` 阶段就抛异常（`response_text` 尚未赋值）或被调用方提前关闭，此处用 `locals()` 防御性取值，避免埋点自身抛 `UnboundLocalError` 影响主流程（埋点整体已包在 `try/except` 内，失败不影响对话）。改为无动态写法需把变量初始化前移到 `try` 之前，属触碰该函数重构时正面处理的项。 |

约束：**这 1 处是审计基线，不是可以照抄的模板**。新增任何 `globals/locals/vars/exec/eval/compile/__import__` 或对模块对象的 `setattr` 仍会被 pre-commit 拦截；如确有必要，按「禁止绕过规范」先修复、确属误报再入台账（对象格式，带 `reason`/`since`）并在 commit message 说明原因。

## 执行纪律（回归与编辑）

这一节不是"要求"，是**已经付过学费的实测结论**（P4-12，2026-09-13）。五条全部来自执行 AI 自己在上一批的自我复盘，此处只把"为什么"钉在旁边——理由是实测发现：带依据的纪律比不带依据的要求更可能被遵守。

> 总账：40 轮带明细的回归里 **15 轮含超时（37.5%）**，累计纯看门狗等待 **5,580s（93 分钟）**；11 天内用过 180/300/600 三档阈值，**每次提高阈值都没有救回任何一个挂住的进程**（同一批文件把三档全部吃满）。

1. **跑回归先读已有的数，不要自己重新数。** 权威来源是两处：`tests/TEST_RESULTS.md`（自动生成区块）与 `test_runs` 表（`details` 列存逐文件的 `file`/`ok`/`timed_out`/`cases`/`seconds`）。**禁止再手写逐文件计数脚本**——这个工具 P4-4 就建好了，上一批仍然手写了两次，其中一次还被工具自身的 600s 上限砍掉重来。
   *（为什么写这条：一次手写计数漏了一行，导致"39 文件 / 469 用例"这个错数字被写进文档并转抄到下一批的前提里——正是本文档「元规则」警告的"未被复测的数字经过转抄获得权威"。）*

2. **阈值不是判定量。** 被看门狗砍掉的文件，**必须用 `--files` 单跑复验一次之后**才允许写进结论；整轮里的 `timed_out` 只是"待复验"的排队标记，**不是故障判定**。
   **禁止逐批临时调 `--timeout`。** 需要改阈值就改 `DEFAULT_TIMEOUT_SEC` 这一处常量、在 commit message 里写实测依据，并同步所有描述"当前配置"的文字位置（P4-13 子项 ④ 列过清单：一个常量曾散成 12 处文字）。
   保留成立的那半句依据：**180→300→600 三档都被同一批文件吃满、15 次挂里 0 次救回**（口径是"结束与否"，不受下面那处混淆影响，站得住）。
   *（为什么改：原文写"`--timeout` 不许超过默认 180"，且引"默认 180 那轮 982.6s vs 600 档三轮 1151.7/1268.7/1346.2s"作依据——该论证是混淆的：982.6s 那轮含两个 180s 超时（=360s），600 档每轮含一个 600s；把超时项剥掉后真实工作量是 622.6s(180 档) vs 552–746s(600 档)，基本没差。而一条写着"不许超过 180"的规范会**命令下一批继续踩已经踩过的误杀**——实测全仓历史上 8 次合法完成超过 180s、涉及 6 个文件、最大 298.1s。规范写成"不许调大"与写成"调大没用"是两回事。）*

3. **改断言 / 改实现前，先出全量预期破点清单，一次改完，只跑一次。**
   依据：上一批子项 3 已用只读脚本一次算出"3 条会变 + 5 条连带破"，却仍然逐条改逐条跑，连续撞 5 轮 × 30–60s ≈ 250s 纯浪费。**信息明明已经在手上却没使用**，是最该避免的一类返工。

4. **编辑长 Markdown 时，`old_string` 不要包含标题行（`---` / `## …`）。**
   依据：上一批两次删掉 `PROJECT_STATUS.md` 的 `## 五` 标题、一次把 §五.23 整段替换没了。本项目 `PROJECT_STATUS.md` 的单行极长（一个里程碑行可含数千字），跨行匹配标题极易连带吃掉结构行。

5. **需要跑旧代码时用 `git worktree add`，禁止 `git stash`。**
   依据：`stash` 在本规模下迟早吃掉未提交的活。附带一个真实好处：`memory/database.py` 的 `DB_PATH` 是相对路径 `data/memory.db`，所以在 worktree 目录里跑，天然落在另一个库上，**不会碰生产库**（但请确认工作目录，别在项目根跑 worktree 里的脚本）。

6. **注释与文档里不写会随重构漂移的具体计数**（文件数、用例数、行数、Tab 数）。需要引用时写"以复跑为准"，或指向 `tests/TEST_RESULTS.md` 与 `PROJECT_STATUS.md` §七。
   依据：P4-14 的启动快照注释写下"不会给 **39 个**测试文件…增加磁盘噪声"——**它落盘那一刻就已经是错的**（同一提交把文件数从 39 变成 40）。`check_comment_freshness` 扫不出这类数字（关键词收窄后只抓 TODO/历史原因那类行），所以只能靠这条纪律。同理，`DEFAULT_TIMEOUT_SEC` 这类"一个常量散成十几处文字"的教训（P4-13 子项 ④）也是同一形态。

## 其他约定速览

- **禁止事项**：不使用 `git add .`（显式列出文件）；不执行 `git push`（除非用户要求）；不读取 `.env`；不修改 `checks/`、`scripts/`、`AGENTS.md` 之外文件时须先询问。
- **扩展面**：`skills/`（用户能力，UI 可见）与 `plugins/`（运行时基础设施：模型 / 工具集）都是 **drop-in 目录**——加能力只需新建 `manifest.json` + 实现类，不需要改代码。两者清单 schema、基类与消费者不同，边界说明与合并取舍见 `PROJECT_STATUS.md` §三「扩展面」。
- **版本口径**：以 `PROJECT_STATUS.md` §四 里程碑表为准。**代码里不写版本字面量**——`app.py` 头部与 UI 标题曾各带一个（`v3.6.1` / `v3.5`），P5-2 已删除；代码里的「v3.8 新增」这类是**功能引入注记**，不属版本声明，保留。
- **文档同步**：涉及功能/数据结构的变更，应同步更新 `PROJECT_STATUS.md` 相应条目。
- **依赖口径**：`requirements.txt` 全部使用 `==` 精确钉版本，版本号**必须取自当前 `.venv` 实测安装版本**，禁止凭记忆填写；本机 `.venv` 由 uv 创建、无 pip 模块，读取方式为 `python -c "import importlib.metadata as m; print(m.version('<包名>'))"`。新增直接 `import` 的第三方包须同步声明并钉版本。
- **测试口径**：统一用 `core/test_runner.py` 直跑，**不要用 pytest 直跑**（`tests/` 下 21 个文件保留 nose 时代的模块级 `setup()`，pytest 8.0+ 已不再自动调用；且 `.venv` 未安装 pytest）。详见 `PROJECT_STATUS.md` §七。
