"""
搜索源适配器集合（插件化：新增搜索源只需实现 SearchProvider 并加入 get_default_providers）

- SearchProvider：抽象基类（统一接口）；
- DuckDuckGoProvider：DuckDuckGo 网页搜索（ddgs 库，无需 Key）；
- GitHubProvider：GitHub 仓库搜索（公开 API，无需 token）；
- ModelScopeProvider：魔搭竞赛/活动一手列表接口（匿名只读，结构化含报名截止时间）。
"""

from .base import SearchProvider
from .ddgs_provider import DuckDuckGoProvider
from .github_provider import GitHubProvider
from .modelscope_provider import ModelScopeProvider

__all__ = [
    "SearchProvider",
    "DuckDuckGoProvider",
    "GitHubProvider",
    "ModelScopeProvider",
    "get_default_providers",
]


def get_default_providers() -> list:
    """默认搜索源组合（可替换：新增/移除搜索源只需修改此函数）

    未来如需按配置动态加载（如读取配置文件决定启用哪些源），
    也只需改动此工厂函数，任务管理器无需感知。

    注：ModelScopeProvider 与 query 无关（一轮只拉一次，见其模块 docstring），
    放在最后不影响其它源；scan_once 会为默认 6 条查询各调它一次，它只在
    首次返回数据。
    """
    return [DuckDuckGoProvider(), GitHubProvider(), ModelScopeProvider()]
