"""
DuckDuckGo 搜索源 Provider（基于 ddgs 库）

- 与 tools/local_tools.web_search 使用同一依赖（ddgs>=7.0，当前环境 9.x）；
- 无需 API Key；网络错误 / 限流 / 空结果一律返回空列表，不抛异常。
"""

from typing import List

from .base import SearchProvider
from ..models import Evidence, now_str


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo 网页搜索适配器"""

    name = "DuckDuckGo"

    def search(self, query: str, limit: int = 5) -> List[Evidence]:
        try:
            from ddgs import DDGS
        except ImportError:
            print("[Search] DuckDuckGo 不可用：未安装 ddgs（pip install ddgs）")
            return []

        try:
            # 与 tools/local_tools.web_search 相同的调用方式（ddgs 9.x 已验证）
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=limit) or []
        except Exception as e:
            print(f"[Search] DuckDuckGo 搜索失败（忽略）: {e}")
            return []

        evidence: List[Evidence] = []
        for r in results:
            title = (r.get("title") or "").strip()
            href = (r.get("href") or r.get("url") or "").strip()
            body = (r.get("body") or "").strip()
            if not title and not href:
                continue  # 丢弃空结果
            evidence.append(Evidence(
                source_url=href,
                source_name=self.name,
                title=title,
                snippet=body[:500],
                confidence=0.5,  # 无排序信号，默认中等置信度
                published_at=None,
                retrieved_at=now_str(),
            ))
        # 资源消耗埋点（v0.1）：记录一次搜索调用（粗粒度 token 估算，失败不影响主流程）
        try:
            from memory.resource_log import log_resource_usage, estimate_tokens
            log_resource_usage("search", estimate_tokens(query), "ddgs",
                               f"查询：{query[:60]}；结果 {len(evidence)} 条")
        except Exception:
            pass
        return evidence
