# 📚 私人学习助手（Personal Agent Harness）

基于 [魔搭 ModelScope](https://modelscope.cn) 和 Gradio 构建的个人 AI 学习助手，
具有长期记忆、目标管理、技能扩展和自主工作流能力。

当前版本口径见 [PROJECT_STATUS.md](PROJECT_STATUS.md) §四 里程碑表（README 不单独维护版本号；
此前此处写的 **v3.6.1** 是历史遗留，已随 2026-09-12 文档清理撤下）。

## ✨ 功能特色

| 功能 | 说明 |
|------|------|
| 🎭 四种角色 | 通用助手、AI 学习教练、系统科学导师、英语辅导老师，Supervisor 自动路由 |
| 🎯 目标系统 | 多目标管理、自动任务分解、目标专属文件目录、任务完成率统计 |
| 🧠 长期记忆 | 用户档案、学习记录、知识图谱、关键词动态检索（按 detail_level 分层注入） |
| 🧩 技能系统 | 概念讲解、想法孵化器、学习计划生成器、概念测验 |
| 🔧 工具调用 | 时间查询、文件读写、网络搜索、本地知识库检索（RAG），按角色分配工具权限 |
| 💬 会话管理 | 多会话、流式输出、历史自动压缩、导出 TXT/Markdown |
| 📅 计划复盘 | 每日 8:00 生成计划 / 20:00 生成复盘（后台定时任务） |
| 🧪 Prompt 进化 | 基于对话质量评估的提示词迭代优化 |

## 📁 项目结构

```
├── app.py                  # 启动入口（薄壳：re-export 回调 + 启动逻辑）
├── ui/
│   ├── app_factory.py      #   Gradio 界面构建（各页 Tab；Tab 数以复跑为准，不在此处记数）
│   └── controllers/        #   各页回调（会话 / 目标 / 笔记 / 技能 / 资源机会 …）
├── core/                   # 核心运行时
│   ├── runtime.py          #   AgentRuntime：主流程编排
│   ├── context_manager.py  #   系统提示词构建（分层记忆注入）
│   ├── event_bus.py        #   事件总线 + event_handlers 自动记录学习
│   ├── goal_manager.py     #   目标管理
│   ├── plugin_manager.py   #   插件加载
│   ├── test_runner.py      #   全量回归运行器（超时看门狗 / 失败重试）
│   └── ...                 #   技能管理 / 状态 / 追踪 / 检查点
├── agents/                 # 子代理：Supervisor 路由、对话评估、每日计划、Prompt 进化、工具 schema
├── business/
│   └── model_client.py     # 魔搭模型客户端（单例、流式、Function Call、异常分类）
├── memory/                 # 记忆系统（SQLite：档案/学习记录/知识图谱/对话/计划/Prompt）
├── prompts/
│   └── roles.json          # 角色 System Prompt 与工具权限
├── tools/                  # 本地工具实现（文件/时间/搜索/RAG）+ 输入验证、导出
├── skills/                 # 技能包（manifest.json + skill.py，drop-in 目录）
├── rag/                    # 本地向量检索（bge-m3 嵌入 + bge-reranker + Chroma）
├── plugins/                # 插件：modelscope_model（模型）、local_tools（工具），drop-in 目录
├── checks/                 # 机械检查六件套 + 豁免台账 + 棘轮基线（pre-commit 钩子调用）
├── scripts/                # 钩子安装/本体、状态快照等工程脚本
├── data/                   # 运行数据（数据库、会话、目标文件、本地模型，不入库）
├── tests/                  # 测试用例
├── requirements.txt
└── PROJECT_STATUS.md       # 版本状态与待办
```

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key（见下方「配置说明」）

# 3. 启动
python app.py        # 启动后浏览器访问 http://127.0.0.1:8502
```

## 🔑 配置说明

所有敏感配置集中存放在项目根目录的 **`.env`** 文件中。
该文件已被 `.gitignore` 忽略，不会提交到仓库；模板见 `.env.example`：

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `MODELSCOPE_API_KEY` | ✅ | 魔搭 API Token，[点此获取](https://modelscope.cn/my/myaccesstoken) |
| `MODELSCOPE_API_BASE_URL` | ❌ | 默认 `https://api-inference.modelscope.cn/v1/` |
| `MODELSCOPE_MODEL_NAME` | ❌ | 默认 `Qwen/Qwen3.5-35B-A3B` |

配置由 [python-dotenv](https://pypi.org/project/python-dotenv/) 在 `business/model_client.py` 中自动加载；
未配置 Key 时会抛出带明确指引的错误提示。

## 🧪 运行测试

本仓库**不用 pytest**，测试以自带 runner 逐文件直跑（每个测试文件都带 `__main__` 入口）：

```bash
python core/test_runner.py --timeout 300 --retries 1

# 单个文件直跑
python tests/test_role_prompts.py
```

> ⚠️ 请勿改用 `python -m pytest tests/ -v`（2026-09-12 更正）：`tests/` 下 21 个文件保留了
> nose 时代的模块级 `setup()`，pytest 8.0+ 已不再自动调用它们，库表初始化会被静默跳过；
> 且本项目未把 pytest 列入依赖。详见 [PROJECT_STATUS.md](PROJECT_STATUS.md) §七。

## 📦 依赖版本

`requirements.txt` 中所有依赖均以 `==` 精确钉定，版本号取自开发环境 `.venv` 的实测安装值
（Gradio 主版本间差异较大，浮动约束容易在新环境装出不兼容的 UI 运行时）。
`pytest` 为可选依赖、刻意未列入；`modelscope` 仅 `download_models.py` 使用，亦未列入。
