"""
GitHub 搜索源 Provider（GitHub Search API）

- 使用公开搜索接口 /search/repositories（无需 token；未认证限流约 10 次/分钟）；
- 按 stars 排序返回最相关仓库，作为代码/工程资料的证据来源；
- 限流（403）/ 查询语法非法（422）/ 网络错误一律返回空列表并打印日志，不抛异常；
- 预留：后续可扩展 /search/code、/search/issues 等适配器（切换 API 路径即可）。 (2026-09-12)
"""

from typing import List

import requests

from .base import SearchProvider
from ..models import Evidence, now_str


class GitHubProvider(SearchProvider):
    """GitHub 仓库搜索适配器"""

    name = "GitHub"
    SEARCH_API = "https://api.github.com/search/repositories"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def search(self, query: str, limit: int = 5) -> List[Evidence]:
        params = {
            "q": query,
            "per_page": min(int(limit), 30),
            "sort": "stars",
            "order": "desc",
        }
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-agent-information-engine",
        }
        try:
            resp = requests.get(
                self.SEARCH_API, params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as e:
            print(f"[Search] GitHub 请求失败（忽略）: {e}")
            return []

        if resp.status_code == 403:
            # 未认证搜索限流（约 10 次/分钟）：跳过该来源，不拖垮任务
            print("[Search] GitHub 搜索限流（403），本次跳过该来源")
            return []
        if resp.status_code != 200:
            print(f"[Search] GitHub 搜索返回非 200（{resp.status_code}），跳过该来源")
            return []

        try:
            data = resp.json()
        except ValueError:
            print("[Search] GitHub 返回非 JSON 响应，跳过该来源")
            return []

        evidence: List[Evidence] = []
        for item in (data.get("items") or []):
            full_name = (item.get("full_name") or item.get("name") or "").strip()
            html_url = (item.get("html_url") or "").strip()
            description = (item.get("description") or "").strip()
            if not full_name and not html_url:
                continue  # 丢弃空结果
            evidence.append(Evidence(
                source_url=html_url,
                source_name=self.name,
                title=full_name,
                snippet=description[:500],
                confidence=0.6,  # 仓库 star 排序，置信度略高于通用网页搜索
                published_at=(item.get("created_at") or None),
                retrieved_at=now_str(),
            ))
        # 资源消耗埋点（v0.1）：记录一次搜索调用（粗粒度 token 估算，失败不影响主流程）
        try:
            from memory.resource_log import log_resource_usage, estimate_tokens
            log_resource_usage("search", estimate_tokens(query), "github",
                               f"查询：{query[:60]}；结果 {len(evidence)} 条")
        except Exception:
            pass
        return evidence
