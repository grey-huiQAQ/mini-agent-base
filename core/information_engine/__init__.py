"""
Information Engine V0.2 —— 深度搜索基础设施（独立、可替换的搜索模块）

定位：搜索不是 Skill，而是 core/ 下的独立子包，作为搜索基础设施供 UI / Agent 复用。
V0.1 范围：
- 搜索源 Provider 插件化：SearchProvider 抽象基类 + DuckDuckGo / GitHub 两个实现；
- 后台异步任务：SearchTaskManager（threading + 信号量，最大 3 并发，状态持久化 SQLite）；
- 证据临时存储：EvidenceStore（search_evidence 表；entities/claims/source_quality 预留）； (2026-09-12)
- 用户确认后才保存：UI 层勾选证据 → 保存为笔记（NoteStore），保存后打 saved_note_id 标记。

V0.2 新增：
- 查询改写：LLMQueryRewriter（云端 LLM 生成 2~4 个子查询；失败降级为原始查询）；
- 证据去重排序：rank_evidence / deduplicate_evidence（来源权重 + 关键词命中 +
  内容长度评分，URL/相似度去重）；
- 信息增益停止：information_gain_stop（连续多轮无新增来源/事实则提前停止）。

预留扩展点（V0.3+）(2026-09-12)：
- 知识图谱整合：读取 evidence.entities/claims 后写入知识图谱。
"""

from .models import Evidence, TaskStatus, now_str
from .evidence_store import EvidenceStore, render_evidence_note
from .evidence_quality import (
    DEFAULT_SOURCE_WEIGHTS,
    rank_evidence,
    deduplicate_evidence,
    information_gain_stop,
)
from .query_rewriter import LLMQueryRewriter
from .search_providers import (
    SearchProvider,
    DuckDuckGoProvider,
    GitHubProvider,
    get_default_providers,
)
from .search_task import SearchTaskManager

__all__ = [
    "Evidence",
    "TaskStatus",
    "now_str",
    "EvidenceStore",
    "render_evidence_note",
    "DEFAULT_SOURCE_WEIGHTS",
    "rank_evidence",
    "deduplicate_evidence",
    "information_gain_stop",
    "LLMQueryRewriter",
    "SearchProvider",
    "DuckDuckGoProvider",
    "GitHubProvider",
    "get_default_providers",
    "SearchTaskManager",
]
