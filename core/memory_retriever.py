"""
记忆检索器 - 根据用户输入动态检索最相关的学习记录和知识图谱节点
纯 Python 实现，无外部依赖（不引入 jieba 等）
"""
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional


# 常见停用词列表（中英文混合）
_STOP_WORDS = {
    # 中文停用词
    "的", "了", "我", "你", "他", "她", "它", "们", "这", "那", "是",
    "在", "有", "和", "就", "不", "也", "都", "会", "到", "说", "要",
    "能", "对", "还", "没", "很", "让", "把", "被", "过", "又", "去",
    "来", "看", "好", "自己", "什么", "怎么", "可以", "可能", "因为",
    "所以", "但是", "如果", "虽然", "或者", "而且", "以及", "然后",
    "已经", "正在", "应该", "需要", "使用", "通过", "关于", "进行",
    "一个", "一些", "这些", "那些", "什么", "为什么", "怎么样",
    "请问", "请", "帮我", "告诉", "知道", "想", "想了解", "学习",
    "什么", "怎么", "哪", "哪些", "这里", "那里", "这样", "那样",
    # 英文停用词
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "need", "must",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "how", "when", "where", "why",
    "not", "no", "nor", "and", "but", "or", "so", "if", "then",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "as", "into", "about", "between", "through", "during",
    "please", "help", "tell", "know", "want", "learn",
}


def extract_keywords(text: str) -> List[str]:
    """
    从文本中提取关键词（中文、英文、数字词元）
    去除停用词和长度为1的无意义词
    
    策略：
    - 英文/数字：作为整体词元提取（如 Python、GPT4）
    - 中文：按字符逐个提取（因中文无空格分隔）
    - 长度 >= 2 的词元直接作为关键词
    - 单字符中文词元尝试组合成双字词组（如「机器」「学习」）

    Args:
        text: 输入文本

    Returns:
        关键词列表（已去重）
    """
    if not text:
        return []

    keywords = []
    seen = set()

    # 1. 提取英文/数字词元（作为整体）
    eng_tokens = re.findall(r'[a-zA-Z][a-zA-Z0-9]+', text)
    for token in eng_tokens:
        token_lower = token.lower()
        if token_lower in _STOP_WORDS:
            continue
        if token_lower not in seen:
            seen.add(token_lower)
            keywords.append(token)

    # 2. 提取中文字符序列
    chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
    if chinese_chars:
        # 长度 >= 2 的中文序列直接作为关键词
        for seq in re.findall(r'[\u4e00-\u9fa5]{2,}', text):
            seq_lower = seq.lower()
            if seq_lower not in _STOP_WORDS and seq_lower not in seen:
                seen.add(seq_lower)
                keywords.append(seq)
        # 对单个中文字符，尝试组合成双字词组
        for i in range(len(chinese_chars) - 1):
            bigram = chinese_chars[i] + chinese_chars[i + 1]
            bigram_lower = bigram.lower()
            if bigram_lower not in _STOP_WORDS and bigram_lower not in seen:
                seen.add(bigram_lower)
                keywords.append(bigram)

    return keywords


def _calculate_relevance(record_text: str, keywords: List[str]) -> int:
    """
    计算一条记录文本与关键词的匹配分数
    匹配方式：检查关键词是否出现在记录文本中（子串匹配）

    Args:
        record_text: 记录的文本内容
        keywords: 关键词列表

    Returns:
        匹配的关键词数量（分数越高越相关）
    """
    if not record_text or not keywords:
        return 0
    record_lower = record_text.lower()
    score = 0
    for kw in keywords:
        if kw.lower() in record_lower:
            score += 1
    return score


class MemoryRetriever(ABC):
    """记忆检索器抽象基类"""

    @abstractmethod
    def retrieve(self, user_input: str, role: str = "", limit: int = 3) -> Dict[str, List[Dict]]:
        """
        根据用户输入检索最相关的记忆

        Args:
            user_input: 用户当前输入
            role: 当前角色（用于过滤领域）
            limit: 每类检索结果的最大数量

        Returns:
            {
                "learning_records": [record_dict, ...],
                "knowledge_nodes": [node_dict, ...]
            }
        """
        pass


class KeywordMemoryRetriever(MemoryRetriever):
    """
    基于关键词匹配的记忆检索器
    使用纯 Python 实现，不依赖分词库
    """

    # 领域映射（与 ContextManager.domain_map 保持一致）
    domain_map = {
        "AI学习": "AI",
        "英语": "英语",
        "系统科学": "系统科学",
        "通用助手": "通用",
    }

    def retrieve(self, user_input: str, role: str = "", limit: int = 3) -> Dict[str, List[Dict]]:
        """
        根据用户输入检索最相关的学习记录和知识图谱节点

        Args:
            user_input: 用户当前输入
            role: 当前角色名称
            limit: 每类检索结果的最大数量

        Returns:
            包含 learning_records 和 knowledge_nodes 的字典
        """
        if not user_input or not user_input.strip():
            return {"learning_records": [], "knowledge_nodes": []}

        try:
            keywords = extract_keywords(user_input)
            if not keywords:
                return {"learning_records": [], "knowledge_nodes": []}

            domain = self.domain_map.get(role, "")

            # 检索学习记录
            learning_records = self._search_learning_records(keywords, domain, limit)

            # 检索知识图谱节点
            knowledge_nodes = self._search_knowledge_nodes(keywords, domain, limit)

            return {
                "learning_records": learning_records,
                "knowledge_nodes": knowledge_nodes,
            }
        except Exception as e:
            print(f"[MemoryRetriever] 检索失败: {e}")
            return {"learning_records": [], "knowledge_nodes": []}

    def _search_learning_records(self, keywords: List[str], domain: str, limit: int) -> List[Dict]:
        """
        从 learning_records 表检索最相关的记录

        Args:
            keywords: 关键词列表
            domain: 领域过滤（可为空）
            limit: 最大返回数量

        Returns:
            按相关度排序的记录列表
        """
        try:
            from memory.database import get_db
            conn = get_db()

            # 获取所有记录（或按领域过滤）
            if domain:
                rows = conn.execute(
                    "SELECT * FROM learning_records WHERE subject = ? ORDER BY created_at DESC LIMIT 50",
                    (domain,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learning_records ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
            conn.close()

            if not rows:
                return []

            # 计算每条记录的相关度分数
            scored = []
            for row in rows:
                record = dict(row)
                # 拼接记录的可搜索文本
                searchable = " ".join([
                    str(record.get("topic", "")),
                    str(record.get("summary", "")),
                    str(record.get("weak_points", "")),
                    str(record.get("subject", "")),
                ])
                score = _calculate_relevance(searchable, keywords)
                if score > 0:
                    scored.append((score, record))

            # 按分数降序排序，取 top limit
            scored.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in scored[:limit]]

        except Exception as e:
            print(f"[MemoryRetriever] 学习记录检索失败: {e}")
            return []

    def _search_knowledge_nodes(self, keywords: List[str], domain: str, limit: int) -> List[Dict]:
        """
        从 knowledge_nodes 表检索最相关的节点

        Args:
            keywords: 关键词列表
            domain: 领域过滤（可为空）
            limit: 最大返回数量

        Returns:
            按相关度排序的节点列表
        """
        try:
            from memory.database import get_db
            conn = get_db()

            # 获取所有节点（或按领域过滤）
            if domain:
                rows = conn.execute(
                    "SELECT * FROM knowledge_nodes WHERE domain = ? ORDER BY id DESC LIMIT 50",
                    (domain,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM knowledge_nodes ORDER BY id DESC LIMIT 50"
                ).fetchall()
            conn.close()

            if not rows:
                return []

            # 计算每个节点的相关度分数
            scored = []
            for row in rows:
                node = dict(row)
                # 拼接节点的可搜索文本
                searchable = " ".join([
                    str(node.get("name", "")),
                    str(node.get("domain", "")),
                ])
                score = _calculate_relevance(searchable, keywords)
                if score > 0:
                    scored.append((score, node))

            # 按分数降序排序，取 top limit
            scored.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in scored[:limit]]

        except Exception as e:
            print(f"[MemoryRetriever] 知识节点检索失败: {e}")
            return []
