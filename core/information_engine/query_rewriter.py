"""
查询改写器（Information Engine V0.2）

LLMQueryRewriter：调用云端 LLM 把单个查询改写为 2~4 个搜索子查询
（JSON 数组，中英文混合，保留原始查询至少一个）。实际调用走
core.ai_filter.chat_sync_with_retry_budget（两档预算，P7 批次 10-①）：
本器输出严格是 JSON 数组，预算低于推理地板就会因截断而整块失效。
LLM 调用失败 / 返回非 JSON / 解析非法时降级为仅原始查询（不抛异常），
保证搜索流程不中断。
"""

import json
import re
from typing import List


class LLMQueryRewriter:
    """云端 LLM 查询改写器（V0.2）

    用法：
        rewriter = LLMQueryRewriter()
        sub_queries = rewriter.rewrite("Transformer 位置编码")
        # -> ["Transformer 位置编码", "positional encoding implementation", ...]

    设计说明：
    - 延迟导入 business.model_client（避免 app 启动时加载 openai 客户端链）；
    - 失败降级：任何异常/非法输出都返回 [query]；
    - SearchTaskManager 侧另有二次校验（强制保留原始查询、上限 4 个），双保险。
    """

    def __init__(self, max_queries: int = 4, temperature: float = 0.4,
                 max_tokens: int = None, timeout: int = 30):
        """
        设计说明（P7 批次 10-①）：
        - max_tokens 为 None（默认）时取 core.ai_filter.DEFAULT_MAX_TOKENS——
          本器输出严格是 JSON 数组，属「输出当 JSON 解析」类，截断即硬失败
          （解析不出来 ⇒ 改写整块失效，只剩原始查询这一档降级）；原先的默认
          300 低于推理型模型的推理地板 1500，必然被 reasoning_content 耗尽。
        - 延迟导入 core.ai_filter：本模块刻意不在导入期拉起它（连同
          memory.database 的 sqlite 链），保持原有启动期轻量。
        """
        self.max_queries = max(2, min(4, int(max_queries)))
        self.temperature = temperature
        if max_tokens is None:
            from core.ai_filter import DEFAULT_MAX_TOKENS
            max_tokens = DEFAULT_MAX_TOKENS
        self.max_tokens = int(max_tokens)
        self.timeout = timeout

    def rewrite(self, query: str) -> List[str]:
        """把查询改写为 2~4 个子查询；任何失败降级为 [query]"""
        query = (query or "").strip()
        if not query:
            return [query]
        prompt = (
            "你是一个搜索查询改写助手。请把下面的查询改写为 2~4 个更具体、"
            "覆盖不同角度（可中英文混合）的搜索子查询，其中至少一个必须是原始查询本身。\n"
            "只输出 JSON 数组，不要输出任何解释或其他内容。\n"
            f"原始查询：{query}\n"
            '输出格式：["子查询1", "子查询2", ...]'
        )
        try:
            # 预算与重试统一实现（P7 批次 10-①）：首档 ≤ 地板时自动补一轮更大预算。
            # 本模块的"降级为原始查询"语义不变——helper 只负责给够预算并重试一次。
            from core.ai_filter import chat_sync_with_retry_budget
            response = chat_sync_with_retry_budget(
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                timeout=self.timeout,
                first_max_tokens=self.max_tokens,
            )
        except Exception as e:
            print(f"[Search] LLM 查询改写调用失败，降级为原始查询: {e}")
            return [query]
        return self._parse(response, query)

    def _parse(self, response: str, query: str) -> List[str]:
        """解析 LLM 输出为查询列表；非法时降级为 [query]"""
        try:
            text = (response or "").strip()
            # 兼容 ```json ... ``` 代码块包裹
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
            if m:
                text = m.group(1).strip()
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError("LLM 返回的不是 JSON 数组")
            items = [str(s).strip() for s in data if str(s).strip()]
            if not items:
                raise ValueError("LLM 返回的数组为空")
            if query not in items:
                # 强制保留原始查询（至少一个）
                items.insert(0, query)
            return items[: self.max_queries]
        except Exception as e:
            print(f"[Search] LLM 查询改写返回非法，降级为原始查询: {e}")
            return [query]
