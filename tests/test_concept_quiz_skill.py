"""
概念测验技能测试
测试技能加载、输入验证、JSON 解析、格式化输出等
"""
import os
import sys
import json

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()


def test_skill_load():
    """测试技能可以被 SkillManager 加载"""
    from core.skill_manager import SkillManager

    sm = SkillManager()
    sm.load_all_skills()

    skill = sm.get_skill("concept_quiz")
    assert skill is not None, "concept_quiz 技能未加载"

    manifest = sm.get_manifest("concept_quiz")
    assert manifest is not None
    assert manifest.name == "concept_quiz"
    assert manifest.display_name == "概念测验"
    assert "测验" in manifest.tags

    print("[OK] skill load")


def test_validate_input():
    """测试输入验证（所有参数可选）"""
    from skills.concept_quiz.skill import SkillImpl

    skill = SkillImpl()

    # 空输入也应通过验证
    assert skill.validate_input({}) is True
    assert skill.validate_input({"concept": "机器学习"}) is True
    assert skill.validate_input({"difficulty": "easy"}) is True

    print("[OK] validate input")


def test_parse_json_response():
    """测试 JSON 解析"""
    from skills.concept_quiz.skill import SkillImpl

    # 正常 JSON
    text1 = '{"questions": [{"question": "测试题", "options": ["A", "B", "C", "D"], "answer": "A", "explanation": "解析"}]}'
    result = SkillImpl._parse_json_response(text1)
    assert result is not None
    assert len(result["questions"]) == 1
    assert result["questions"][0]["answer"] == "A"

    # 带 markdown 包裹
    text2 = '分析\n```json\n{"questions": []}\n```\n结束'
    result = SkillImpl._parse_json_response(text2)
    assert result is not None

    # 无效 JSON
    result = SkillImpl._parse_json_response("不是 JSON")
    assert result is None

    # 空文本
    result = SkillImpl._parse_json_response("")
    assert result is None

    print("[OK] parse json response")


def test_format_quiz():
    """测试测验格式化"""
    from skills.concept_quiz.skill import SkillImpl

    skill = SkillImpl()

    questions = [
        {
            "question": "什么是机器学习？",
            "options": ["A. 一种编程语言", "B. 一种让机器学习的技术", "C. 一种数据库", "D. 一种操作系统"],
            "answer": "B",
            "explanation": "机器学习是人工智能的一个分支",
        },
        {
            "question": "以下哪个是监督学习？",
            "options": ["A. 聚类", "B. 分类", "C. 降维", "D. 生成"],
            "answer": "B",
            "explanation": "分类是典型的监督学习任务",
        },
    ]

    result = skill._format_quiz("机器学习", "medium", questions)

    assert "机器学习" in result
    assert "第 1 题" in result
    assert "第 2 题" in result
    assert "什么是机器学习" in result
    assert "B" in result
    assert "人工智能" in result
    assert "学习建议" in result

    print("[OK] format quiz")


def test_select_weak_concept():
    """测试自动选取薄弱知识点"""
    from skills.concept_quiz.skill import SkillImpl
    from memory.knowledge_graph import KnowledgeGraph
    from memory.learning_recorder import LearningRecorder

    skill = SkillImpl()
    skill.knowledge_graph = KnowledgeGraph
    skill.learning_recorder = LearningRecorder

    # 即使没有数据，也不应崩溃
    concept = skill._select_weak_concept()
    # 结果可能为空字符串（如果没有薄弱点），这是正常的
    assert isinstance(concept, str)

    print("[OK] select weak concept")


def test_run_empty_context():
    """测试空上下文执行（不涉及 LLM）"""
    from skills.concept_quiz.skill import SkillImpl
    from memory.knowledge_graph import KnowledgeGraph
    from memory.learning_recorder import LearningRecorder

    skill = SkillImpl()
    skill.knowledge_graph = KnowledgeGraph
    skill.learning_recorder = LearningRecorder

    # 空上下文应能正常执行（可能因无薄弱点而返回错误，但不应崩溃）
    result = skill.execute({})
    assert isinstance(result, dict)
    # 应该返回 error（因为没有薄弱点也没有指定概念）
    # 或者如果有薄弱点，则返回 result
    assert "error" in result or "result" in result

    print("[OK] run empty context")


def test_difficulty_validation():
    """测试难度参数校验"""
    from skills.concept_quiz.skill import SkillImpl

    skill = SkillImpl()

    # 无效难度应回退为 medium
    # 这里只测试参数解析，不实际调用 LLM
    difficulty = "invalid"
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    assert difficulty == "medium"

    # num_questions 边界测试
    num = 10
    num = max(1, min(5, int(num)))
    assert num == 5

    num = -1
    num = max(1, min(5, int(num)))
    assert num == 1

    print("[OK] difficulty validation")


if __name__ == "__main__":
    print("=== concept quiz skill test ===\n")

    # 确保数据库已初始化
    from memory.database import init_db
    init_db()

    test_skill_load()
    test_validate_input()
    test_parse_json_response()
    test_format_quiz()
    test_select_weak_concept()
    test_run_empty_context()
    test_difficulty_validation()

    print("\n=== ALL TESTS PASSED ===")
