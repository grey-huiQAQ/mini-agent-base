# -*- coding: utf-8 -*-
"""概念讲解技能测试（P4-9 接线随附，2026-09-12）

背景：`skills/concept_explainer/` 自 Phase 2 起是一个"活着的孤儿"——SkillManager
按目录扫描会正常加载它、技能列表里也看得见它的名字，但界面上**没有任何入口**，
Phase 2 只能在它的 docstring 里写一句「未接线」当作标记。P4-9 把它接进
「🧩 技能列表」页的「📖 概念讲解」区块（回调 `ui/controllers/learning_controller
.explain_concept`），本文件同时钉住链路两侧：

  ① 技能侧：manifest 字段、输入校验、缺概念时的错误路径（均不调用 LLM，离线可跑）；
  ② 接线侧：回调存在、参数映射正确、**回调引用的技能名在磁盘上真实存在**。

第 ② 项是本条债务的复发哨兵。只测技能本身是看不到断链的：`run_skill` 在技能名
不存在时返回 `技能 '名字' 不存在` 这样的 error 字典，UI 上表现为"点了没反应/报错"，
而技能单测照样全绿——这正是 concept_explainer 能在"零入口"状态下活过 Phase 2/3
的原因。（此处刻意不写占位符的大写形式：本仓库的注释哨兵
check_comment_freshness 把特定的大写字母串当债务关键词，写了会被判为无日期债务。）

不隔离的说明：本文件走共享 helper 切临时库（导入 app 薄壳会触发 init_db），
符合 checks/check_test_db_isolation.py 的顺序规则。
"""
import os
import sys
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db
setup_test_db()

_ROOT = Path(__file__).resolve().parent.parent


def test_concept_explainer_manifest_fields():
    """技能可被加载，且 manifest 声明了 UI 依赖的关键字段"""
    from core.skill_manager import SkillManager

    sm = SkillManager()
    sm.load_all_skills()

    skill = sm.get_skill("concept_explainer")
    assert skill is not None, "concept_explainer 技能未加载"

    manifest = sm.get_manifest("concept_explainer")
    assert manifest is not None
    assert manifest.name == "concept_explainer"
    assert manifest.display_name == "概念讲解"
    assert "讲解" in manifest.tags

    # 输入契约：concept 必填、level 可选且有默认值（UI 的下拉框取这三档）
    assert manifest.input_schema["concept"]["required"] is True
    assert manifest.input_schema["level"].get("default") == "beginner"
    assert "explanation" in manifest.output_schema

    print("[OK] concept explainer: manifest fields")


def test_concept_explainer_validate_input():
    """输入校验：concept 为空即不通过，level 缺省不影响通过"""
    from skills.concept_explainer.skill import SkillImpl

    skill = SkillImpl()

    assert skill.validate_input({}) is False, "缺 concept 不应通过校验"
    assert skill.validate_input({"concept": ""}) is False
    assert skill.validate_input({"concept": "梯度下降"}) is True
    assert skill.validate_input({"concept": "梯度下降", "level": "advanced"}) is True

    print("[OK] concept explainer: validate input")


def test_concept_explainer_missing_concept_error():
    """缺概念时 execute 返回中文 error 字典（不触发 LLM 调用）"""
    from skills.concept_explainer.skill import SkillImpl

    result = SkillImpl().execute({})
    assert isinstance(result, dict)
    assert "error" in result and "概念" in result["error"], result

    print("[OK] concept explainer: missing concept returns error")


def test_concept_explainer_level_prompt_mapping():
    """三档讲解对象都会落进提示词：不同的 level 生成的提示词互不相同"""
    from skills.concept_explainer import skill as explainer_module

    prompts = {}
    seen = {}

    from business import model_client
    real_chat_sync = model_client.chat_sync

    def _fake_chat_sync(messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        return "（假讲解）"

    # 技能在 execute 内部才 `from business.model_client import chat_sync`，
    # 故替换模块属性即可生效（无须改技能代码）。
    model_client.chat_sync = _fake_chat_sync
    try:
        impl = explainer_module.SkillImpl()
        for level in ("beginner", "intermediate", "advanced"):
            out = impl.execute({"concept": "梯度下降", "level": level})
            assert out.get("explanation") == "（假讲解）", out
            prompts[level] = seen["prompt"]
    finally:
        # 逐测还原：不还原会污染同一子进程内后续用例
        model_client.chat_sync = real_chat_sync

    assert len(set(prompts.values())) == 3, f"三档 level 生成了相同提示词：{prompts}"
    assert "零基础初学者" in prompts["beginner"]
    assert "有一定基础的学习者" in prompts["intermediate"]
    assert "进阶学习者" in prompts["advanced"]

    print("[OK] concept explainer: level prompt mapping")


def test_concept_explainer_ui_wiring():
    """UI 接线：回调把 (concept, level) 正确映射为技能调用，且技能名真实存在

    这条用例是 P4-9 的核心断言——它同时覆盖"UI 能调到"与"调到的名字在磁盘上
    有对应目录"两件事，后者正是本技能长期无入口也无人察觉的原因。
    """
    import app as app_module
    from core.skill_manager import SkillManager
    from ui.controllers import learning_controller as lc

    calls = {}

    class _FakeSkillManager:
        """只为记录调用参数的最小替身（不加载任何技能）"""

        def run_skill(self, name, context):
            calls["name"] = name
            calls["context"] = dict(context)
            return {"explanation": "【讲解】梯度下降就是蒙眼下山"}

    real_manager = lc.runtime.skill_manager
    lc.runtime.skill_manager = _FakeSkillManager()
    try:
        out = lc.explain_concept("梯度下降", "intermediate")
        # 空概念在回调层就被挡住（不进入技能，也不产生 LLM 调用）
        warn = lc.explain_concept("   ", "beginner")
    finally:
        lc.runtime.skill_manager = real_manager

    assert out == "【讲解】梯度下降就是蒙眼下山", out
    assert calls["name"] == "concept_explainer", calls
    assert calls["context"] == {"concept": "梯度下降", "level": "intermediate"}, calls
    assert warn.startswith("⚠️"), warn

    # 断链哨兵：回调里写死的技能名必须能在磁盘上加载到
    sm = SkillManager()
    sm.load_all_skills()
    assert sm.get_skill(calls["name"]) is not None, f"UI 指向的技能不存在：{calls['name']}"

    # app.py 薄壳必须 re-export 同一个对象（app_factory 经 _app_module.explain_concept 绑定）
    assert getattr(app_module, "explain_concept", None) is lc.explain_concept, \
        "app.py 未 re-export explain_concept 或指向了别的对象"

    print("[OK] concept explainer: ui wiring")


def test_concept_explainer_error_is_surfaced():
    """技能返回 error 时，回调把它翻成中文提示而不是把 dict 丢给 Markdown"""
    from ui.controllers import learning_controller as lc

    class _FailingSkillManager:
        def run_skill(self, name, context):
            return {"error": "调用 LLM 失败: 断网"}

    real_manager = lc.runtime.skill_manager
    lc.runtime.skill_manager = _FailingSkillManager()
    try:
        out = lc.explain_concept("梯度下降", "beginner")
    finally:
        lc.runtime.skill_manager = real_manager

    assert isinstance(out, str) and out.startswith("❌") and "断网" in out, out

    print("[OK] concept explainer: error surfaced as text")


def test_concept_explainer_factory_binding():
    """app_factory 里确实存在按钮绑定（静态源码断言）

    刻意不做 `create_app()` 全量构建：那会拉起整个 Gradio 界面（十多个 Tab），
    为一条"绑定还在不在"的断言付出整页构建代价不划算。此处只断言绑定语句与
    回调引用同时出现在工厂源码中——删掉任一者都会让本用例失败。
    """
    src = (_ROOT / "ui" / "app_factory.py").read_text(encoding="utf-8")
    assert "_app_module.explain_concept" in src, "工厂未引用 explain_concept 回调"
    assert "explain_concept_btn.click(" in src, "工厂未绑定概念讲解按钮"
    assert "explain_result_display" in src, "工厂未接概念讲解的输出组件"

    print("[OK] concept explainer: factory binding")


def test_cleanup():
    """清理本进程创建的临时测试库目录"""
    cleanup_test_db()
    print("[OK] cleanup")


if __name__ == "__main__":
    print("=== concept explainer skill test ===\n")

    test_concept_explainer_manifest_fields()
    test_concept_explainer_validate_input()
    test_concept_explainer_missing_concept_error()
    test_concept_explainer_level_prompt_mapping()
    test_concept_explainer_ui_wiring()
    test_concept_explainer_error_is_surfaced()
    test_concept_explainer_factory_binding()
    test_cleanup()

    print("\n=== ALL TESTS PASSED ===")
