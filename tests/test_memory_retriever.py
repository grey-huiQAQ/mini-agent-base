"""
记忆检索器单元测试
测试关键词提取、检索功能
"""
import os
import sys

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()


def test_extract_keywords():
    """测试关键词提取"""
    from core.memory_retriever import extract_keywords

    # 基本中英文混合
    keywords = extract_keywords("我想学习Python编程和机器学习")
    assert "Python" in keywords or "python" in keywords, f"应包含 Python: {keywords}"
    assert "编程" in keywords, f"应包含 编程: {keywords}"
    assert "机器" in keywords or "机器学习" in keywords, f"应包含 机器学习相关: {keywords}"

    # 停用词过滤
    keywords = extract_keywords("什么是Transformer的注意力机制")
    assert "Transformer" in keywords or "transformer" in keywords, f"应包含 Transformer: {keywords}"
    # "什么" 作为双字词组应被停用词过滤
    assert "什么" not in keywords, f"不应包含停用词 '什么': {keywords}"
    # "的注意力机制" 作为连续中文序列应被提取
    assert any("注意力" in kw for kw in keywords), f"应保留含'注意力'的词: {keywords}"

    # 空输入
    assert extract_keywords("") == []
    assert extract_keywords(None) == []

    print("[OK] extract_keywords")


def test_stop_words():
    """测试停用词过滤"""
    from core.memory_retriever import extract_keywords

    # 单个停用词应被过滤
    for word in ["的", "了", "我", "是", "你", "什么", "可以"]:
        keywords = extract_keywords(word)
        assert len(keywords) == 0, f"单个停用词 '{word}' 应被过滤: {keywords}"

    # 纯停用词句子可能产生双字组合（"的了" 等），但不应包含有意义词
    keywords = extract_keywords("的了我你是")
    # 允许为空或只包含无意义组合
    for kw in keywords:
        assert len(kw) >= 2, f"关键词长度应 >= 2: {kw}"

    print("[OK] stop words filtering")


def test_calculate_relevance():
    """测试相关度计算"""
    from core.memory_retriever import _calculate_relevance

    # 完全匹配
    score = _calculate_relevance("Python 是一种编程语言", ["Python", "编程"])
    assert score == 2, f"应匹配 2 个关键词: {score}"

    # 部分匹配
    score = _calculate_relevance("Python 是一种编程语言", ["Java", "编程"])
    assert score == 1, f"应匹配 1 个关键词: {score}"

    # 无匹配
    score = _calculate_relevance("今天天气真好", ["Python", "编程"])
    assert score == 0, f"应匹配 0 个关键词: {score}"

    # 空输入
    assert _calculate_relevance("", ["test"]) == 0
    assert _calculate_relevance("test", []) == 0

    print("[OK] _calculate_relevance")


def test_retriever_init():
    """测试检索器初始化"""
    from core.memory_retriever import KeywordMemoryRetriever

    retriever = KeywordMemoryRetriever()
    assert retriever is not None
    assert hasattr(retriever, 'retrieve')
    assert hasattr(retriever, '_search_learning_records')
    assert hasattr(retriever, '_search_knowledge_nodes')

    print("[OK] retriever init")


def test_retrieve_empty_input():
    """测试空输入检索"""
    from core.memory_retriever import KeywordMemoryRetriever

    retriever = KeywordMemoryRetriever()
    result = retriever.retrieve("", role="AI学习")
    assert result == {"learning_records": [], "knowledge_nodes": []}, f"空输入应返回空结果: {result}"

    result = retriever.retrieve(None, role="AI学习")
    assert result == {"learning_records": [], "knowledge_nodes": []}, f"None 输入应返回空结果: {result}"

    print("[OK] retrieve empty input")


def test_retrieve_with_db():
    """测试带数据库的检索（需要数据库中有数据）"""
    from core.memory_retriever import KeywordMemoryRetriever
    from memory.database import init_db, get_db

    # 确保数据库已初始化
    init_db()

    retriever = KeywordMemoryRetriever()

    # 尝试检索（可能有数据也可能没有）
    result = retriever.retrieve("Python 编程学习", role="AI学习")
    assert "learning_records" in result
    assert "knowledge_nodes" in result
    assert isinstance(result["learning_records"], list)
    assert isinstance(result["knowledge_nodes"], list)

    print("[OK] retrieve with db")


def test_retrieve_role_mapping():
    """测试领域映射"""
    from core.memory_retriever import KeywordMemoryRetriever

    retriever = KeywordMemoryRetriever()

    # 测试不同角色的领域映射
    result = retriever.retrieve("测试", role="AI学习")
    assert isinstance(result, dict)

    result = retriever.retrieve("测试", role="英语")
    assert isinstance(result, dict)

    result = retriever.retrieve("测试", role="未知角色")
    assert isinstance(result, dict)

    print("[OK] role mapping")


def test_retrieve_exception_safety():
    """测试异常安全性"""
    from core.memory_retriever import KeywordMemoryRetriever

    retriever = KeywordMemoryRetriever()

    # 输入特殊字符不应崩溃
    result = retriever.retrieve("!@#$%^&*()_+", role="AI学习")
    assert isinstance(result, dict)

    # 超长输入不应崩溃
    result = retriever.retrieve("a" * 10000, role="AI学习")
    assert isinstance(result, dict)

    print("[OK] exception safety")


if __name__ == "__main__":
    print("=== memory retriever test ===\n")

    test_extract_keywords()
    test_stop_words()
    test_calculate_relevance()
    test_retriever_init()
    test_retrieve_empty_input()
    test_retrieve_with_db()
    test_retrieve_role_mapping()
    test_retrieve_exception_safety()

    print("\n=== ALL TESTS PASSED ===")
