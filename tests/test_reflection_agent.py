# -*- coding: utf-8 -*-
"""
ReflectionAgent 经验沉淀代理测试（离线：通过替换 model_client.chat_sync 模拟 LLM）
覆盖：
- 反思提取全链路（mock LLM）：字段校验、Markdown 展示
- 无经验 / 非法 JSON / LLM 异常的降级
- 校验器：缺必填字段过滤、超长字段截断、非法结构
- 保存经验写库：单条与批量（含无效项跳过）、来源会话记录
- ContextManager 注入集成：相关经验进入系统提示，失败不影响构建
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db
from memory.experience_store import ExperienceStore
from agents import reflection_agent as ra_module
from agents.reflection_agent import ReflectionAgent

# 本测试创建的经验 ID，cleanup 时精确清理
_created_ids = []


def setup():
    """初始化数据库"""
    init_db()


def _patch_chat_sync(fake):
    """临时替换 model_client.chat_sync，返回还原函数"""
    original = ra_module.model_client.chat_sync
    ra_module.model_client.chat_sync = fake

    def restore():
        ra_module.model_client.chat_sync = original
    return restore


def test_reflect_success_with_mock_llm():
    """mock LLM 全链路：提取经验建议并生成 Markdown"""
    agent = ReflectionAgent()
    fake_response = '''```json
{"has_experience": true, "experiences":[{"scenario":"向初学者解释抽象概念","strategy":"先举生活化例子再给正式定义","effect":"用户反馈更容易理解","conditions":"概念较为抽象时"}]}
```'''
    restore = _patch_chat_sync(lambda messages, **kw: fake_response)
    try:
        history = [
            {"role": "user", "content": "什么是递归？总是搞不懂"},
            {"role": "assistant", "content": "就像俄罗斯套娃，打开一层还有一层……"},
            {"role": "user", "content": "这个类比懂了！"},
        ]
        result = agent.reflect_on_conversation(history)
        assert result["ok"], result["message"]
        assert len(result["suggestions"]) == 1
        s = result["suggestions"][0]
        assert s["scenario"] == "向初学者解释抽象概念"
        assert s["strategy"].startswith("先举生活化例子")
        assert s["effect"] == "用户反馈更容易理解"
        assert s["conditions"] == "概念较为抽象时"
        assert "先举生活化例子" in result["display"], "展示文本应包含策略内容"
        # 纯文本输入也应支持（字符串透传）
        result2 = agent.reflect_on_conversation("- 用户说喜欢要点式回答")
        assert isinstance(result2["ok"], bool)
    finally:
        restore()
    print("[OK] reflect success with mock llm")


def test_reflect_no_experience():
    """mock LLM 返回无经验：ok=True 且 suggestions 为空"""
    agent = ReflectionAgent()
    restore = _patch_chat_sync(
        lambda messages, **kw: '{"has_experience": false, "experiences": []}'
    )
    try:
        result = agent.reflect_on_conversation([{"role": "user", "content": "今天天气如何"}])
        assert result["ok"], result["message"]
        assert result["suggestions"] == []
        assert "暂无值得沉淀" in result["message"]
    finally:
        restore()
    print("[OK] reflect no experience")


def test_reflect_degrade_paths():
    """空历史、LLM 异常、非法 JSON 三种情况的友好降级"""
    agent = ReflectionAgent()

    # 1. 空历史 / 空白文本
    r1 = agent.reflect_on_conversation([])
    assert not r1["ok"] and "没有可分析的对话内容" in r1["message"]
    r1b = agent.reflect_on_conversation("   ")
    assert not r1b["ok"]

    # 2. LLM 抛异常 → 不崩溃
    def boom(messages, **kw):
        raise RuntimeError("网络中断模拟")
    restore = _patch_chat_sync(boom)
    try:
        r2 = agent.reflect_on_conversation("- 聊了几句")
        assert not r2["ok"] and "模型调用失败" in r2["message"]
    finally:
        restore()

    # 3. 非法 JSON → 解析失败提示
    restore = _patch_chat_sync(lambda messages, **kw: "完全不是 JSON")
    try:
        r3 = agent.reflect_on_conversation("- 聊了几句")
        assert not r3["ok"] and "无法从模型回复中解析" in r3["message"]
    finally:
        restore()
    print("[OK] reflect degrade paths")


def test_validate_suggestions_filters_and_clamps():
    """校验器单元测试：缺必填过滤、超长截断、非法元素丢弃"""
    agent = ReflectionAgent()
    dirty = {
        "experiences": [
            "junk_string",                                   # 非法元素 → 过滤
            {"scenario": "", "strategy": "缺场景"},           # scenario 为空 → 过滤
            {"scenario": "只有场景"},                          # strategy 缺失 → 过滤
            {"scenario": "合法场景",
             "strategy": "策" * 500,                          # 超长截断到 400（含省略号）
             "effect": "效" * 300,                            # 截断到 200
             "conditions": "  多余空白   清洗  "},             # 空白压缩
        ]
    }
    cleaned = agent._validate_suggestions(dirty)
    assert len(cleaned) == 1
    s = cleaned[0]
    assert s["strategy"] == "策" * 399 + "…", "策略应截断为 400 字符"
    assert s["effect"] == "效" * 199 + "…", "效果应截断为 200 字符"
    assert s["conditions"] == "多余空白 清洗", "空白应被压缩"

    # 完全非法结构
    assert agent._validate_suggestions(None) == []
    assert agent._validate_suggestions({"experiences": "not a list"}) == []
    print("[OK] validate suggestions filters and clamps")


def test_store_experience_writes_db():
    """保存单条经验：落库且字段/来源会话正确"""
    agent = ReflectionAgent()
    exp = {
        "scenario": "用户要求解释复杂概念",
        "strategy": "先类比再定义，最后给一个小练习",
        "effect": "互动更顺畅",
        "conditions": "非紧急问答场景",
    }
    new_id = agent.store_experience(exp, source_session_id="sess_reflect_001")
    _created_ids.append(new_id)
    stored = next(e for e in ExperienceStore.get_all_experiences(limit=50)
                  if e["id"] == new_id)
    assert stored["scenario"] == exp["scenario"]
    assert stored["strategy"] == exp["strategy"]
    assert stored["effect"] == exp["effect"]
    assert stored["conditions"] == exp["conditions"]
    assert stored["source_session_id"] == "sess_reflect_001"
    print("[OK] store experience writes db")


def test_apply_suggestions_batch_skips_invalid():
    """批量保存：有效条目写入、无效条目跳过并给出明细"""
    agent = ReflectionAgent()
    before = ExperienceStore.count_experiences()
    suggestions = [
        {"scenario": "批量场景A", "strategy": "批量策略A"},
        {"scenario": "", "strategy": "缺场景应跳过"},
        {"scenario": "批量场景B", "strategy": "批量策略B", "effect": "效果好"},
    ]
    result = agent.apply_suggestions(suggestions, source_session_id="sess_batch")
    assert result["ok"] and result["saved"] == 2, f"应成功保存 2 条：{result}"
    assert len(result["details"]) == 3, "每条都应有处理明细"
    assert ExperienceStore.count_experiences() == before + 2
    # 从明细中解析新纪录 ID，供 cleanup 精确清理（格式："#<id> 场景「...」"）
    import re
    for d in result["details"]:
        m = re.match(r"#(\d+) ", d)
        if m:
            _created_ids.append(int(m.group(1)))

    # 全部无效 → ok=False 且不崩溃
    r2 = agent.apply_suggestions([{"scenario": ""}, "junk"])
    assert not r2["ok"] and r2["saved"] == 0

    # 空列表
    assert agent.apply_suggestions([])["saved"] == 0
    print("[OK] apply suggestions batch skips invalid")


def test_context_manager_injects_experiences():
    """ContextManager 集成：相关经验注入系统提示；低详级别不注入"""
    from core.context_manager import ContextManager

    exp_id = ExperienceStore.add_experience(
        scenario="背诵英语单词容易遗忘",
        strategy="间隔重复：每天复习前一天的单词再加新词",
        conditions="需每天固定时间执行",
        source_session_id="sess_ctx_test",
    )
    _created_ids.append(exp_id)

    cm = ContextManager()
    prompt = cm.build_system_prompt(
        role="英语", base_prompt="你是英语学习助手。",
        detail_level="high",
        user_input="怎么背英语单词不容易忘？",
    )
    assert "💡 相关经验参考" in prompt, "相关经验应注入系统提示"
    assert "间隔重复" in prompt, "策略内容应出现在提示中"

    # low 级别不注入（token 控制）
    prompt_low = cm.build_system_prompt(
        role="英语", base_prompt="你是英语学习助手。",
        detail_level="low",
        user_input="怎么背英语单词不容易忘？",
    )
    assert "💡 相关经验参考" not in prompt_low

    # 无 user_input 时不注入且不报错
    prompt_none = cm.build_system_prompt(role="英语", base_prompt="你是助手。")
    assert "💡 相关经验参考" not in prompt_none

    # 注入失败容错：检索方法抛异常时 build_system_prompt 正常返回基础提示
    orig_search = ExperienceStore.search_experiences
    try:
        ExperienceStore.search_experiences = staticmethod(
            lambda q, limit=3: (_ for _ in ()).throw(RuntimeError("模拟故障"))
        )
        prompt_fail = cm.build_system_prompt(
            role="英语", base_prompt="你是英语学习助手。",
            detail_level="high", user_input="背单词方法",
        )
        assert prompt_fail.startswith("你是英语学习助手。"), "注入失败不应影响基础提示"
        assert "💡 相关经验参考" not in prompt_fail
    finally:
        ExperienceStore.search_experiences = orig_search
    print("[OK] context manager injects experiences with failure tolerance")


def test_apply_suggestions_dedup():
    """防重复沉淀：相同「场景+策略」的第二条被跳过，不产生重复记录"""
    import re
    agent = ReflectionAgent()
    exp = {"scenario": "去重场景X", "strategy": "去重策略Y"}

    # 第一次保存成功
    r1 = agent.apply_suggestions([dict(exp)], source_session_id="sess_dedup_1")
    assert r1["ok"] and r1["saved"] == 1, f"首次保存应成功：{r1}"
    for d in r1["details"]:
        m = re.match(r"#(\d+) ", d)
        if m:
            _created_ids.append(int(m.group(1)))

    before = ExperienceStore.count_experiences()
    # 完全相同的经验再次保存 → 跳过且不写库
    r2 = agent.apply_suggestions([dict(exp)])
    assert not r2["ok"] and r2["saved"] == 0, "重复经验应被跳过"
    assert any("重复" in d for d in r2["details"]), "明细应提示重复跳过"
    assert ExperienceStore.count_experiences() == before, "不应新增记录"

    # 场景或策略任一不同 → 视为新经验，正常保存
    r3 = agent.apply_suggestions([{"scenario": "去重场景X", "strategy": "不同策略Z"}])
    assert r3["ok"] and r3["saved"] == 1
    for d in r3["details"]:
        m = re.match(r"#(\d+) ", d)
        if m:
            _created_ids.append(int(m.group(1)))
    print("[OK] apply suggestions dedup")


def test_cleanup():
    """清理本测试产生的全部经验数据"""
    for exp_id in _created_ids:
        ExperienceStore.delete_experience(exp_id)
    _created_ids.clear()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== reflection agent test ===\n")
    setup()
    test_reflect_success_with_mock_llm()
    test_reflect_no_experience()
    test_reflect_degrade_paths()
    test_validate_suggestions_filters_and_clamps()
    test_store_experience_writes_db()
    test_apply_suggestions_batch_skips_invalid()
    test_apply_suggestions_dedup()
    test_context_manager_injects_experiences()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
