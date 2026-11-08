"""
搜索源 Provider 抽象基类（Information Engine V0.1）

任何搜索源只需实现 search() 方法即可作为可替换插件接入任务管理器：
- 新增搜索源：新建独立文件，继承 SearchProvider，实现 search()；
- 替换默认组合：修改 search_providers/__init__.py 的 get_default_providers()。
"""

from abc import ABC, abstractmethod
from typing import List

from ..models import Evidence


class SearchProvider(ABC):
    """搜索源适配器统一接口

    实现要求（V0.1 约定）：
    - search() 对网络错误 / 限流 / 空结果一律**不抛异常**，返回空列表并打印日志，
      由任务管理器统一汇总（单源失败不拖垮整个任务）；
    - 返回的 Evidence 至少填写 source_url / source_name / title / snippet /
      confidence / retrieved_at；entities / claims / source_quality 可留空。
    """

    #: 来源名称（写入 search_evidence.source_name，任务摘要与 UI 展示使用）
    name: str = "SearchProvider"

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> List[Evidence]:
        """执行一次搜索，返回标准化的 Evidence 列表"""
        raise NotImplementedError
