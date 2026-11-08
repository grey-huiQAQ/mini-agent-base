# -*- coding: utf-8 -*-
"""
Resource Scout AI 深度筛选（core/ai_filter.py v0.4）测试（离线：mock LLM）

覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
- 数据库迁移：opportunities 表含 relevance_* / ai_* 新列，init_db 幂等
- ai_filter_opportunity：正常 JSON（含 ```json 围栏 / 前后文字）解析、
  字段白名单与 0~1 区间钳制、LLM 抛异常 / 输出非法 JSON → 默认值且 ok=False
- ai_filter_opportunities：候选选取（分数 ≥ 0.3、资格非 likely_no、
  未筛过，前 N 条）、已筛自动跳过、失败单条不影响其它条、
  成功结果与 ai_screened_at 落库、报告计数正确
- P7 阶段二 B1b（2026-10-17）新增：ai_next_step 那一格的 prompt 原文与排序、
  解析（截断 200 字）、「空／缺键 ⇒ None，不替模型补一句」、批量落库（含「加工过
  但没给下一步」⇒ 存 NULL），以及首页候选集两个只读函数
  fetch_home_candidates／has_unprocessed_home_candidates 的三条门槛、排序、
  「含已加工过的行」，和「首页候选（未加工那部分）⊆ 加工队列取回集合」这条包含关系
  （措辞按 2026-10-17 设计席裁定 ① 改准：是 ⊆ 不是 ＝，两侧各有一条反证被钉住）
- P7 阶段二 B2（2026-10-17）新增：usable_assets 那一格的 prompt 原文与位置、
  解析（截断 200 字）、「空值哨兵 ⇒ NULL，不替模型补一句」的**逐字**集合（前三档出自
  锁定决定 1，八档是同日 §五.64 ② 定案扩的结果；「未知／不确定／待定」按定案保留原样）、
  模型答成数组时的顿号拼接分支（§五.64 ③ 定案）、批量落库，以及「这一格不是首页的筛选
  门槛」——usable 为空的机会照旧在
  fetch_home_candidates 的取回集合里（排序降权发生在界面层，归 test_home_dashboard 管）
- UI 回调契约：导入 app.py，mock LLM 后跑通「后台 AI 筛选 → 等待完成 →
  刷新列表展示 AI 评估」全流程
"""
import json
import os
import sys
import time
import uuid

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；保留 _TMP_DIR / _TMP_DB 两个旧变量名，既有断言无需改动。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_connection, get_user_profile
from core.resource_scout import ResourceScout, EligibilityHint
from core import ai_filter
from core.ai_filter import (
    ai_filter_opportunity,
    ai_filter_opportunities,
    AI_ELIGIBILITY_VALUES,
    AI_RECOMMENDATIONS,
)




def setup():
    """初始化独立测试数据库（触发 v0.4 新列迁移）"""
    init_db()


def _clear_opportunities():
    """清空临时库中的机会行（保证各测试互相独立、计数确定）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


def _add_row(title, url=None, score=0.0, hint="unknown", screened_at="",
             status=None, discovered_at=None, assets=None):
    """插入一条机会并直接设置相关性/资格/筛选状态（绕过扫描，确定性）

    status／discovered_at 两个参数是 P7 阶段二 B1b（2026-10-17）为首页候选那两条用例
    加的：前者要构造「不是 new 的机会」，后者要把排序钉成可比对的顺序——
    discovered_at 的默认值只到秒，同批插入的行会并列，排序断言就会随机器速度漂。
    `assets`（B2，2026-10-17）写 `usable_assets` 那一格：**只有显式传才 UPDATE**，
    不传就保持迁移后的 NULL——「这条还没加工」与「加工过但答了无」在库里都是 NULL，
    夹具替它们造出默认形状就等于替测试撒了谎。
    """
    url = url or f"https://ai-test.example.com/{uuid.uuid4().hex[:8]}"
    oid = ResourceScout.add_opportunity(title, url)
    assert oid > 0
    sets = ["relevance_score = ?", "relevance_status = ?",
            "eligibility_hint = ?", "ai_screened_at = ?"]
    vals = [float(score), "relevant" if score >= ai_filter.MIN_RELEVANCE_SCORE
            else "unknown", hint, screened_at or ""]
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if discovered_at is not None:
        sets.append("discovered_at = ?")
        vals.append(discovered_at)
    if assets is not None:
        sets.append("usable_assets = ?")
        vals.append(assets)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE opportunities SET {', '.join(sets)} WHERE id = ?",
            vals + [oid])
        conn.commit()
    finally:
        conn.close()
    return oid


def _row(oid):
    """读取单条机会行（断言用）"""
    return ResourceScout.get_opportunity(oid)


def _ok_json(**overrides):
    """构造一份合法的 AI 评估 JSON 字符串

    `ai_next_step`（P7 阶段二 B1b，2026-10-17）放在 `ai_reason` 之后，与 prompt
    模板里那两格的先后一致；要构造「模型没给这一句」的用 override 传空串、
    要构造「整格缺失」则 `json.loads(_ok_json())` 之后 pop 掉那个键（见下面那两条用例）。
    `usable_assets`（B2，2026-10-17）同理排在 `ai_next_step` 之后，与模板里那一格的
    位置一致——默认给**非哨兵**的值，因为要测「模型答了无」的用 override 传「无」。
    """
    payload = {
        "ai_eligibility": "likely_yes",
        "ai_reason": "面向学生开发者，价值较高",
        "ai_next_step": "先去报名页确认学生组别的截止时间",
        "usable_assets": "免费 GPU 额度、数据集",
        "ai_value_score": 0.85,
        "ai_project_reuse": 0.7,
        "ai_recommendation": "建议关注",
        "ai_confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _json_without_next_step(**overrides):
    """模型那一次没给 ai_next_step 这一格（键整个缺失）时的输出"""
    payload = json.loads(_ok_json(**overrides))
    payload.pop("ai_next_step")
    return json.dumps(payload, ensure_ascii=False)


# ---------- 1. 数据库迁移 ----------

def test_db_columns_exist():
    """迁移验证：relevance_* / ai_* 列存在；init_db 幂等"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
    finally:
        conn.close()
    for col in ["relevance_status", "relevance_score", "matched_keywords",
                "ai_eligibility", "ai_reason", "ai_value_score",
                "ai_project_reuse", "ai_recommendation", "ai_confidence",
                "ai_screened_at", "ai_next_step", "usable_assets"]:
        assert col in cols, f"opportunities 缺少列 {col}，实际为 {cols}"
    init_db()
    init_db()
    print("[OK] ai & relevance columns exist, migration idempotent")


# ---------- 2. 单条 AI 评估 ----------

def test_ai_filter_opportunity_success():
    """正常 LLM 输出：JSON 解析（容忍围栏/前后文字）+ 字段规范化"""
    opp = {"id": 1, "title": "Hackathon 2026", "summary": "online",
           "type": "hackathon", "source_url": "https://x.example.com/1"}
    profile = get_user_profile()

    def fake_llm(messages):
        assert messages[0]["role"] == "system"
        assert "Hackathon 2026" in messages[1]["content"]
        assert "高中生" in messages[1]["content"]  # 画像注入 prompt
        # 带 Markdown 围栏 + 前后文字（模型常见输出形式）
        return "好的，评估如下：\n```json\n" + _ok_json() + "\n```\n完毕"

    result = ai_filter_opportunity(opp, profile, llm_fn=fake_llm)
    assert result["ok"] is True, result
    assert result["ai_eligibility"] == "likely_yes"
    assert result["ai_value_score"] == 0.85
    assert result["ai_project_reuse"] == 0.7
    assert result["ai_recommendation"] == "建议关注"
    assert result["ai_confidence"] == 0.9
    assert result["ai_reason"]
    # usable_assets（B2）：默认 JSON 里给的是非哨兵值 ⇒ 原样保留（只去空白、不加工）
    assert result["usable_assets"] == "免费 GPU 额度、数据集", result
    print("[OK] ai filter opportunity success & json parse")


def test_ai_filter_opportunity_clamping_and_whitelist():
    """越界分数钳制 0~1；非法枚举回退默认"""
    opp = {"id": 2, "title": "AI course", "summary": "", "type": "course",
           "source_url": "https://x.example.com/2"}
    profile = get_user_profile()

    def fake_llm(messages):
        return _ok_json(ai_eligibility="bogus", ai_value_score=1.7,
                        ai_project_reuse=-0.5, ai_confidence="high",
                        ai_recommendation="随便说说")

    result = ai_filter_opportunity(opp, profile, llm_fn=fake_llm)
    assert result["ok"] is True, result
    assert result["ai_eligibility"] == "unknown", "非法 ai_eligibility 应回退"
    assert result["ai_value_score"] == 1.0, "1.7 应钳制到 1.0"
    assert result["ai_project_reuse"] == 0.0, "-0.5 应钳制到 0.0"
    assert result["ai_confidence"] is None, "非数字 confidence 应回退 None"
    assert result["ai_recommendation"] == "无法判断", \
        "非法推荐语应回退「无法判断」"
    print("[OK] ai filter clamping & whitelist fallback")


def test_ai_filter_opportunity_failure_defaults():
    """LLM 抛异常 / 输出非法 JSON → 默认值且 ok=False（调用方不写库可重试）"""
    opp = {"id": 3, "title": "GPU credits", "summary": "", "type": "credit",
           "source_url": "https://x.example.com/3"}
    profile = get_user_profile()

    def boom(messages):
        raise RuntimeError("网络超时")

    result = ai_filter_opportunity(opp, profile, llm_fn=boom)
    assert result["ok"] is False, result
    assert result["ai_eligibility"] == "unknown"
    assert result["ai_value_score"] is None
    # 失败时 usable_assets 也必须是 None（B2）——绝不能拿一个空串或「无」冒充「模型说没有」
    assert result["usable_assets"] is None, result
    assert "失败" in result["ai_reason"], result["ai_reason"]

    def junk(messages):
        return "抱歉我没看懂你说什么"

    result2 = ai_filter_opportunity(opp, profile, llm_fn=junk)
    assert result2["ok"] is False and "失败" in result2["ai_reason"]
    print("[OK] ai filter failure returns safe defaults")


# ---------- 3. 批量候选选取与落库 ----------

def test_ai_filter_candidates_selection_and_persist():
    """候选选取：分数≥0.3 且非 likely_no 且未筛过，取前 N 条；结果落库"""
    _clear_opportunities()
    # 6 条：高分未筛 ×3、低分 ×1、likely_no ×1、已筛过 ×1
    id_a = _add_row("Candidate A hackathon", score=0.8)
    id_b = _add_row("Candidate B AI course", score=0.7)
    id_c = _add_row("Candidate C GPU credits", score=0.6)
    id_low = _add_row("Low score generic", score=0.2)
    id_no = _add_row("No eligibility professional", score=0.9,
                     hint="likely_no")
    id_done = _add_row("Screened already hackathon", score=0.9,
                       screened_at="2026-01-01 00:00:00")

    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return _ok_json(ai_reason=f"评估 {len(calls)} 号")

    # 前 2 条候选（按发现时间倒序：id_c、id_b，因为 id_done 已筛、id_no 被资格排除）
    report = ai_filter_opportunities(limit=2, llm_fn=fake_llm)
    assert report["candidates"] == 2, report
    assert report["screened"] == 2 and report["failed"] == 0, report
    assert sorted(report["ids"]) == sorted([id_c, id_b]), report["ids"]
    assert len(calls) == 2

    # 落库校验：id_b / id_c 已写 AI 结果与时间戳；其它行不受影响
    for oid in (id_b, id_c):
        row = _row(oid)
        assert row["ai_eligibility"] == "likely_yes", row
        assert row["ai_value_score"] == 0.85, row
        assert row["ai_screened_at"], row
    assert _row(id_done)["ai_screened_at"] == "2026-01-01 00:00:00"
    assert _row(id_no)["ai_screened_at"] in (None, "")
    assert _row(id_low)["ai_screened_at"] in (None, "")

    # 再次运行（limit 2）：剩余高分候选 id_a 被筛
    report2 = ai_filter_opportunities(limit=2, llm_fn=fake_llm)
    assert report2["candidates"] == 1 and report2["screened"] == 1, report2
    assert report2["ids"] == [id_a], report2["ids"]
    # 第三轮：全部筛完 → 无候选（幂等）
    report3 = ai_filter_opportunities(limit=5, llm_fn=fake_llm)
    assert report3["candidates"] == 0 and report3["screened"] == 0, report3
    assert report3["summary_text"] and "候选 0 条" in report3["summary_text"]
    print("[OK] ai filter candidate selection & persist")


def test_ai_filter_batch_failure_isolated():
    """失败单条不影响其它条：失败的留待下次可重试"""
    _clear_opportunities()
    id_ok = _add_row("Good candidate hackathon", score=0.8)
    id_bad = _add_row("Bad candidate GPU credits", score=0.7)
    call_count = {"n": 0}

    def flaky_llm(messages):
        call_count["n"] += 1
        if call_count["n"] == 1:  # 第一条（id_bad，最新）失败
            raise RuntimeError("模拟超时")
        return _ok_json()

    report = ai_filter_opportunities(limit=5, llm_fn=flaky_llm)
    # 候选两条：id_bad（新）先被评估失败，id_ok 成功
    assert report["candidates"] == 2, report
    assert report["screened"] == 1 and report["failed"] == 1, report
    assert report["ids"] == [id_ok], report
    assert _row(id_ok)["ai_screened_at"], "成功行应写库"
    assert _row(id_bad)["ai_screened_at"] in (None, ""), "失败行不应写库"

    # 重跑：失败行仍为候选并成功
    report2 = ai_filter_opportunities(limit=5, llm_fn=lambda m: _ok_json())
    assert report2["screened"] == 1 and report2["ids"] == [id_bad], report2
    assert _row(id_bad)["ai_screened_at"]
    print("[OK] ai filter batch failure isolated & retryable")


# ---------- 3.5 空输出自动重试（v0.4.1 修复） ----------

def test_ai_filter_retry_on_empty_then_success():
    """推理型模型空输出重试：首轮输出为空（JSON 解析失败）→ 自动重试一次
    成功；只调用两次（不无限重试）"""
    opp = {"id": 50, "title": "Hackathon for students", "summary": "online",
           "type": "hackathon", "source_url": "https://x.example.com/50"}
    profile = get_user_profile()
    calls = {"n": 0}

    def flaky_llm(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # 首轮空输出（模拟 max_tokens 被推理耗尽）
        return _ok_json(ai_reason="重试后成功")

    result = ai_filter_opportunity(opp, profile, llm_fn=flaky_llm)
    assert result["ok"] is True, result
    assert result["ai_reason"] == "重试后成功", result
    assert calls["n"] == 2, f"应恰好调用 2 次（首轮 + 重试），实际 {calls['n']}"
    print("[OK] ai filter auto retry on empty output then success")


def test_ai_filter_retry_exhausted_returns_defaults():
    """重试仍失败（连续两轮空输出）→ 返回默认值并记录失败原因；调用恰好 2 次"""
    opp = {"id": 51, "title": "GPU credits", "summary": "", "type": "credit",
           "source_url": "https://x.example.com/51"}
    profile = get_user_profile()
    calls = {"n": 0}

    def always_empty(messages):
        calls["n"] += 1
        return ""

    result = ai_filter_opportunity(opp, profile, llm_fn=always_empty)
    assert result["ok"] is False, result
    assert "LLM 输出为空" in result["ai_reason"], result
    assert result["ai_value_score"] is None
    assert calls["n"] == 2, f"空输出应重试一次后放弃，实际调用 {calls['n']}"
    print("[OK] ai filter retry exhausted returns defaults")


def test_ai_filter_no_retry_on_network_error():
    """网络/超时类错误不重试（重试无意义）：只调用一次即返回默认值"""
    opp = {"id": 52, "title": "AI course", "summary": "", "type": "course",
           "source_url": "https://x.example.com/52"}
    profile = get_user_profile()
    calls = {"n": 0}

    def boom(messages):
        calls["n"] += 1
        raise RuntimeError("网络超时")

    result = ai_filter_opportunity(opp, profile, llm_fn=boom)
    assert result["ok"] is False, result
    assert "网络超时" in result["ai_reason"], result
    assert calls["n"] == 1, f"网络错误不应重试，实际调用 {calls['n']}"
    print("[OK] ai filter does not retry on network error")


def test_ai_filter_retry_with_large_max_tokens_single_round():
    """max_tokens 已 ≥ RETRY_MAX_TOKENS 时不追加重试轮（预算已足够）：
    首轮即空输出 → 只调用一次并返回默认值"""
    from core.ai_filter import RETRY_MAX_TOKENS
    opp = {"id": 53, "title": "Scholarship guide", "summary": "",
           "type": "scholarship", "source_url": "https://x.example.com/53"}
    profile = get_user_profile()
    calls = {"n": 0}

    def always_empty(messages):
        calls["n"] += 1
        return ""

    result = ai_filter_opportunity(opp, profile, llm_fn=always_empty,
                                   max_tokens=RETRY_MAX_TOKENS)
    assert result["ok"] is False, result
    assert calls["n"] == 1, f"预算已足够不应再重试，实际调用 {calls['n']}"
    print("[OK] ai filter single round when max_tokens already large")


# ---------- 4. UI 回调契约 ----------

def test_ui_ai_button_callback_contract():
    """UI AI 筛选回调：后台执行 → 状态字典报告 → **自动刷新**（poll）展示结果"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI AI 筛选回调契约测试")
        return

    _clear_opportunities()
    _add_row("UI 契约 hackathon A", score=0.8)
    _add_row("UI 契约 hackathon B", score=0.7)
    oid_low = _add_row("UI 低分无关页", score=0.1)

    # mock LLM：替换 ai_filter 模块内的默认调用（worker 不传 llm_fn）；
    # 签名需兼容 _default_llm_call(messages, timeout_sec=…, max_tokens=…)
    def fake_default_llm(messages, timeout_sec=30.0, **kwargs):
        return _ok_json(ai_reason="UI 契约评估通过",
                        ai_recommendation="建议关注")

    original = ai_filter._default_llm_call
    ai_filter._default_llm_call = fake_default_llm
    try:
        # 1) 点击触发 → 立即返回启动提示（后台线程执行）
        status = app_module.do_ai_filter_opportunities()
        assert isinstance(status, str) and "已启动" in status, status
        # 2) 等待后台完成（mock LLM 瞬时返回；轮询状态字典）
        deadline = time.time() + 15
        while time.time() < deadline and \
                app_module.ai_filter_state.get("running"):
            time.sleep(0.05)
        assert not app_module.ai_filter_state.get("running"), "筛选应已结束"
        report = app_module.ai_filter_state.get("report")
        assert report is not None, "完成后应写入 report"
        assert report["screened"] == 2 and report["failed"] == 0, report

        # 3) 数据库已写 AI 字段；低分行不在候选中，保持未筛
        rows = ResourceScout.list_opportunities(limit=50)
        ai_rows = [r for r in rows if (r.get("ai_screened_at") or "").strip()]
        assert len(ai_rows) == 2, rows
        assert all(r.get("ai_eligibility") == "likely_yes" for r in ai_rows)
        assert _row(oid_low)["ai_screened_at"] in (None, "")

        # 4) 自动刷新轮询（模拟 gr.Timer tick）：返回三元组匹配 outputs，
        #    状态行含统计、列表 Markdown 展示 AI 评估行（含 🤖 与推荐语）
        st, md, dd = app_module.poll_ai_filter_results("all", "all")
        assert isinstance(st, str) and "AI 深度筛选" in st, st
        assert "成功 2 条" in st and "候选 2 条" in st, st
        assert "AI 评估" in md, md
        assert "建议关注" in md, md
        assert "UI 契约 hackathon A" in md, md
        assert dd["choices"], "下拉框应保留机会选项"
        # 结果已被消费：再次轮询 → 占位（不再重复刷新）
        assert app_module.ai_filter_state.get("report") is None
        st2, md2, dd2 = app_module.poll_ai_filter_results("all", "all")
        assert app_module.ai_filter_state.get("report") is None
        assert st2 is not None and md2 is not None and dd2 is not None

        # 5) 部分失败场景：report 含 errors → 状态行展示失败原因
        app_module.ai_filter_state.update(
            running=False, error=None,
            report={"summary_text": "AI 深度筛选：候选 1 条，成功 0 条，"
                                    "失败 1 条",
                    "errors": ["#1 AI 评估失败：LLM 输出为空"]})
        st3, md3, _ = app_module.poll_ai_filter_results("all", "all")
        assert "成功 0 条" in st3 and "失败 1 条" in st3, st3
        assert "LLM 输出为空" in st3, "失败原因应展示在状态行"
        assert app_module.ai_filter_state.get("report") is None

        # 6) 手动「🔄 刷新列表」路径仍然可用（AI 结果已落库）
        md_manual, _ = app_module.refresh_opportunities_panel(None, "all", "all")
        assert "AI 评估" in md_manual and "建议关注" in md_manual, md_manual
    finally:
        ai_filter._default_llm_call = original
    print(f"[OK] ui ai button callback + auto-refresh poll flow "
          f"({report['screened']} 条)")


# ---------- 3.6 ai_next_step 与首页候选集（P7 阶段二 B1b，2026-10-17） ----------

def test_prompt_asks_for_next_step_right_after_reason():
    """prompt 里那一格的原文，以及它**排在 ai_reason 之后、而不是列表末尾**的理由"""
    text = ai_filter.AI_FILTER_PROMPT_TEMPLATE
    assert '"ai_next_step": "一句话建议下一步，不超过 60 字"' in text, text
    # 截断风险落在靠后的键上：④「现在能做什么」是给人看的那一句，
    # ai_confidence 是个数字——两者里宁可让后者承担被截的风险。
    assert (text.index("ai_reason") < text.index("ai_next_step")
            < text.index("ai_confidence")), "新键的先后位置被改动"
    # 这次改动没弄坏 .format 的转义（模板里除 {{ }} 外不许出现裸花括号）
    messages = ai_filter._build_messages(
        {"title": "T 标题", "summary": "S 摘要", "type": "hackathon",
         "source_url": "https://x.example.com/p"},
        ai_filter.normalize_profile(get_user_profile()))
    content = messages[1]["content"]
    assert '"ai_next_step"' in content and "T 标题" in content, content
    print("[OK] prompt asks for ai_next_step right after ai_reason")


def test_next_step_parsed_and_length_capped():
    """解析：原样保留（去首尾空白）、超长截到 200 字（口径同 ai_reason）"""
    opp = {"id": 60, "title": "Student grant", "summary": "", "type": "other",
           "source_url": "https://x.example.com/60"}
    profile = get_user_profile()

    result = ai_filter_opportunity(opp, profile,
                                  llm_fn=lambda m: _ok_json(ai_next_step="  先去报名  "))
    assert result["ok"] is True, result
    assert result["ai_next_step"] == "先去报名", repr(result["ai_next_step"])

    long_result = ai_filter_opportunity(
        opp, profile, llm_fn=lambda m: _ok_json(ai_next_step="长" * 300))
    assert long_result["ai_next_step"] == "长" * 200, \
        f"应截到 200 字，实际长度 {len(long_result['ai_next_step'] or '')}"
    # 非字符串（模型偶尔给数字／对象）也不能抛：str() 之后按同一口径处理
    odd = ai_filter_opportunity(opp, profile,
                                llm_fn=lambda m: _ok_json(ai_next_step=123))
    assert odd["ok"] is True and odd["ai_next_step"] == "123", odd
    print("[OK] ai_next_step parsed, stripped and capped at 200 chars")


def test_next_step_missing_is_none_not_invented():
    """空串／纯空白／整格缺失／失败分支 ⇒ 一律 None，**不替模型补一句**

    这一条钉的是设计席 B1b 锁定决定 ① 的后半句：界面上那两态（「还没加工」与
    「加工过了但没给这一句」）全靠 NULL 与空串分开，实现里偷偷补一句就成了假话。
    """
    opp = {"id": 61, "title": "GPU credits", "summary": "", "type": "credit",
           "source_url": "https://x.example.com/61"}
    profile = get_user_profile()

    for label, raw in (("空串", _ok_json(ai_next_step="")),
                       ("纯空白", _ok_json(ai_next_step="   \n ")),
                       ("整格缺失", _json_without_next_step())):
        result = ai_filter_opportunity(opp, profile, llm_fn=lambda m, r=raw: r)
        assert result["ok"] is True, (label, result)
        assert result["ai_next_step"] is None, (label, result)
        # 其它字段照常落：不能因为这一格空就把整条判成没加工
        assert result["ai_reason"] == "面向学生开发者，价值较高", (label, result)

    def boom(messages):
        raise RuntimeError("网络超时")

    failed = ai_filter_opportunity(opp, profile, llm_fn=boom)
    assert failed["ok"] is False and failed["ai_next_step"] is None, failed
    print("[OK] ai_next_step absent/empty -> None (never invented)")


def test_next_step_persisted_by_batch():
    """批量落库：成功那条写入 ai_next_step；模型没给那一格时存 **NULL**（不是空串）

    「存 NULL」不是实现细节而是首页的判据：`ai_screened_at` 非空 + `ai_next_step` 为
    NULL ＝「问过了没答」，与 `ai_screened_at` 为空的「还没问」在卡片上是两句话。
    """
    _clear_opportunities()
    id_a = _add_row("下一步落库 hackathon", score=0.8)
    report = ai_filter_opportunities(
        limit=1, llm_fn=lambda m: _ok_json(ai_next_step="先确认报名截止日"))
    assert report["screened"] == 1, report
    row = _row(id_a)
    assert row["ai_next_step"] == "先确认报名截止日", row
    assert row["ai_screened_at"], row

    id_b = _add_row("没给下一步 hackathon", score=0.7)
    report2 = ai_filter_opportunities(limit=1, llm_fn=lambda m: _json_without_next_step())
    assert report2["screened"] == 1 and report2["ids"] == [id_b], report2
    row2 = _row(id_b)
    assert row2["ai_screened_at"], "没给下一步不等于失败，这条算加工过"
    assert row2["ai_next_step"] is None, repr(row2["ai_next_step"])
    assert row2["ai_reason"], "ai_reason 那一句不该被这次改动一起丢掉"
    print("[OK] ai_next_step persisted (NULL when model skipped it)")


# ---------- 3.7 usable_assets（P7 阶段二 B2，2026-10-17） ----------

def test_prompt_asks_for_usable_assets_right_after_next_step():
    """prompt 里那一格的**原文**，以及它排在 `ai_next_step` 之后、数值键之前的理由

    原文逐字钉（设计席 B2 锁定决定 1）：这一句以后会被改词，改的人应当在这里被绊一下，
    而不是悄悄换一套问法——模型答的形状由这句问话决定，改文案就是改数据形状。
    位置钉（执行席自决，已写进 `PROJECT_STATUS.md` §五.64 等确认）：与 B1b 那条
    「宁可让 `ai_confidence` 去撞截断」同一个依据，给人看的两格在前、数字在后。
    """
    text = ai_filter.AI_FILTER_PROMPT_TEMPLATE
    assert ('"usable_assets": "列出这条机会里能直接拿走/利用的东西（免费额度、API、'
            '数据集、开源代码、SDK、教程、社区支持等），用顿号分隔、最多 5 项；'
            '没有就写「无」"') in text, text
    assert (text.index("ai_next_step") < text.index("usable_assets")
            < text.index("ai_value_score")), "新键的先后位置被改动"
    # 没弄坏 .format 的转义：模板里除 {{ }} 外不许出现裸花括号
    messages = ai_filter._build_messages(
        {"title": "T 标题", "summary": "S 摘要", "type": "hackathon",
         "source_url": "https://x.example.com/p"},
        ai_filter.normalize_profile(get_user_profile()))
    content = messages[1]["content"]
    assert '"usable_assets"' in content and "T 标题" in content, content
    print("[OK] prompt asks for usable_assets right after ai_next_step")


def test_usable_assets_parsed_stripped_and_capped():
    """解析：去首尾空白后原样保留、超长截到 200 字（口径同 ai_reason／ai_next_step）"""
    opp = {"id": 70, "title": "GPU credits", "summary": "", "type": "credit",
           "source_url": "https://x.example.com/70"}
    profile = get_user_profile()

    result = ai_filter_opportunity(
        opp, profile,
        llm_fn=lambda m: _ok_json(usable_assets="  免费额度、数据集  "))
    assert result["ok"] is True, result
    assert result["usable_assets"] == "免费额度、数据集", repr(result["usable_assets"])

    long_result = ai_filter_opportunity(
        opp, profile, llm_fn=lambda m: _ok_json(usable_assets="额" * 300))
    assert len(long_result["usable_assets"]) == 200, len(long_result["usable_assets"])
    print("[OK] usable_assets parsed, stripped and capped at 200 chars")


def test_usable_assets_sentinel_set_is_verbatim():
    """空值哨兵**逐字**成立，也**逐字**失效——两边都按实钉住

    集合内容逐字照设计席 2026-10-17 §五.64 ② 定案给的八档（前三档是 B2 锁定决定 1
    原话，后五档是那次定案补进来的「无」的近义写法）⇒ 命中就存 NULL。
    判据的两头都在下面：

    - **近义归 NULL**：「无。」「没有」「暂无」「暂无可用」「无可用资源」不再带着标点
      上首页那句「能直接拿走：…」（那是同一件事的五种说法，不是五种资源）；
    - **「不知道」三档保留**：「未知」「不确定」「待定」**刻意不进表**——它们表达的是模型
      没答上来，不是「这条没有」，归进 NULL 等于把一次漏答洗成一条结论。

    仍然**没有**前缀或包含匹配：「暂无可用资源」（多出来的字不在任何一档）与「无.」
    （半角句号）都原样保留。这一头是设计席原话「逐字」的第二半，别因为它不像清单就删。
    """
    opp = {"id": 71, "title": "Course", "summary": "", "type": "course",
           "source_url": "https://x.example.com/71"}
    profile = get_user_profile()

    locked = ("", "无", "无可用", "无。", "没有", "暂无", "暂无可用", "无可用资源")
    assert ai_filter.USABLE_ASSET_NULL_VALUES == locked, (
        f"哨兵集合与设计席 §五.64 ② 定案那八档不一致（顺序或条数漂了）："
        f"{ai_filter.USABLE_ASSET_NULL_VALUES}")

    for sentinel in ai_filter.USABLE_ASSET_NULL_VALUES:
        result = ai_filter_opportunity(
            opp, profile, llm_fn=lambda m, s=sentinel: _ok_json(usable_assets=s))
        assert result["ok"] is True, result
        assert result["usable_assets"] is None, f"哨兵 {sentinel!r} 没落成 NULL"

    # 判据第二头：「不知道」不是「没有」，这三档必须原样保留
    for unknown in ("未知", "不确定", "待定"):
        result = ai_filter_opportunity(
            opp, profile, llm_fn=lambda m, u=unknown: _ok_json(usable_assets=u))
        assert result["usable_assets"] == unknown, (
            f"{unknown!r} 被当成「没有」归进 NULL 了——那是漏答，不是结论：{result!r}")

    # 边界（逐字比对的另一半）：多一个字／换一种标点都不算命中
    for variant in ("暂无可用资源", "无.", "没有可用资源"):
        result = ai_filter_opportunity(
            opp, profile, llm_fn=lambda m, v=variant: _ok_json(usable_assets=v))
        assert result["usable_assets"] == variant, (
            f"哨兵做成了包含匹配（{variant!r} 本该原样保留）：{result!r}")

    # 整格缺失 ⇒ None（与 ai_next_step 同一口径：模型没写就是没写，不替它补）
    payload = json.loads(_ok_json())
    payload.pop("usable_assets")
    missing = ai_filter_opportunity(opp, profile,
                                    llm_fn=lambda m: json.dumps(payload, ensure_ascii=False))
    assert missing["ok"] is True and missing["usable_assets"] is None, missing
    print("[OK] usable_assets sentinel set is verbatim (8 locked values, "
          "'unknown' trio kept)")


def test_usable_assets_list_answer_is_joined_not_repr():
    """模型把这一格答成 **JSON 数组**时：逐项去空白、丢空项、最多 5 项、顿号接成一行

    这一条是 §五.64 ③ 定案换来的分支，替换掉原来那条「走 `str()` 得到
    `"['API', '数据集']"`」的登记边界——那个形状从此不该再出现在库里。
    三条都要钉住：
    ① 产出的是**顿号分隔的正文**，不是带引号括号的 Python repr；
    ② 截断由代码做（最多取 5 项，Prompt 那句「最多 5 项」字符串那档只能靠模型自觉）；
    ③ 拼完之后走**同一个出口**——所以 `["无"]` 这种「数组里只装一个无」也归 NULL，
       而不是留下一个看起来像清单的空清单。
    """
    opp = {"id": 711, "title": "List answer", "summary": "", "type": "dataset",
           "source_url": "https://x.example.com/711"}
    profile = get_user_profile()

    result = ai_filter_opportunity(
        opp, profile,
        llm_fn=lambda m: _ok_json(usable_assets=["API", "数据集"]))
    assert result["ok"] is True, result
    assert result["usable_assets"] == "API、数据集", repr(result["usable_assets"])
    assert result["usable_assets"] != str(["API", "数据集"]), (
        "又退回 str() 归一那条路径了（会存成带引号括号的那种 repr）")

    # 逐项去空白、丢掉空项：模型常把清单答成 [" API ", "", "数据集"]
    spaces = ai_filter_opportunity(
        opp, profile,
        llm_fn=lambda m: _ok_json(usable_assets=[" API ", "", "数据集", "  "]))
    assert spaces["usable_assets"] == "API、数据集", repr(spaces["usable_assets"])

    # 最多 5 项：第 6 项起丢掉，不是「拼完再截字」
    six = ai_filter_opportunity(
        opp, profile,
        llm_fn=lambda m: _ok_json(usable_assets=["a1", "a2", "a3", "a4", "a5", "a6"]))
    assert six["usable_assets"] == "a1、a2、a3、a4、a5", repr(six["usable_assets"])

    # 同一个出口：数组里只装一个哨兵 ⇒ NULL
    only_no = ai_filter_opportunity(
        opp, profile, llm_fn=lambda m: _ok_json(usable_assets=["无"]))
    assert only_no["ok"] is True and only_no["usable_assets"] is None, only_no
    empty_list = ai_filter_opportunity(
        opp, profile, llm_fn=lambda m: _ok_json(usable_assets=[]))
    assert empty_list["usable_assets"] is None, empty_list

    # 与字符串同一条截 200：数组拼超长时照样截，不因为走了新分支就漏掉上限
    many = ai_filter_opportunity(
        opp, profile,
        llm_fn=lambda m: _ok_json(usable_assets=["额" * 60] * 5))
    assert len(many["usable_assets"]) == 200, len(many["usable_assets"])
    print("[OK] usable_assets array answer joined with 、 (max 5 items, same NULL exit)")


def test_usable_assets_persisted_by_batch():
    """批量落库：非哨兵写原值、哨兵存 **NULL**（不是空串），且不影响别的格

    「存 NULL」是首页那条判据的地基：这一列的 NULL 同时承载「还没加工／加工过但答了无／
    模型漏答」，首页因此对 NULL **整行不显示**（见 `tests/test_home_dashboard.py`）。
    """
    _clear_opportunities()
    id_yes = _add_row("有资源 hackathon", score=0.8)
    report = ai_filter_opportunities(
        limit=1, llm_fn=lambda m: _ok_json(usable_assets="免费 GPU 额度、数据集"))
    assert report["screened"] == 1, report
    row = _row(id_yes)
    assert row["usable_assets"] == "免费 GPU 额度、数据集", row
    assert row["ai_screened_at"], row

    id_no = _add_row("答了无 hackathon", score=0.7)
    report2 = ai_filter_opportunities(
        limit=1, llm_fn=lambda m: _ok_json(usable_assets="无"))
    assert report2["screened"] == 1 and report2["ids"] == [id_no], report2
    row2 = _row(id_no)
    assert row2["usable_assets"] is None, repr(row2["usable_assets"])
    assert row2["ai_screened_at"], "答了「无」不等于失败，这条算加工过"
    # 其它字段照常落：不能因为这一格是哨兵就把整条判成没加工
    assert row2["ai_next_step"], row2
    print("[OK] usable_assets persisted (NULL for the sentinel, value otherwise)")


def test_usable_assets_is_not_a_home_filter():
    """B2 的边界：`usable_assets` **不参与**首页取回的 WHERE——降权是排序，不是门槛

    这一条钉的是「强过滤弱删除」里那条最容易写反的：把 `usable_assets` 加进
    `fetch_home_candidates` 的 SQL，界面就看不出任何异常，只有那几条「没有可下载资源
    但依然重要」的机会从此再不上首页。设计席原话警告过这一形状，所以把它写成断言、
    不写成注释。判据 1（排序）与这一条（不过滤）是一对，合起来才是「降权 ≠ 删除」。
    """
    _clear_opportunities()
    id_with = _add_row("有资源的那条", score=0.9, assets="API、数据集",
                       discovered_at="2026-10-02 00:00:00")
    id_without = _add_row("没资源的那条", score=0.8,
                          discovered_at="2026-10-03 00:00:00")
    id_null_but_new = _add_row("还没加工的", score=0.7,
                               discovered_at="2026-10-04 00:00:00")
    rows = ai_filter.fetch_home_candidates(limit=10)
    ids = [r["id"] for r in rows]
    for oid in (id_with, id_without, id_null_but_new):
        assert oid in ids, f"#{oid} 被 usable_assets 挡在首页门外了：{ids}"
    # SQL 那一头的序没变（降权发生在界面层）：还是按发现时间倒序
    assert ids == [id_null_but_new, id_without, id_with], ids
    # 取回的是整行，界面层才可能在内存里按这一格重排
    assert rows[0].get("usable_assets") is None, rows[0]
    assert _row(id_with)["usable_assets"] == "API、数据集"
    print("[OK] usable_assets is a sort key, not a home filter")


def test_fetch_home_candidates_rules_and_order():
    """首页候选＝逐字三条门槛（new ∧ 分数 ≥ 门槛 ∧ 资格 ≠ 排除档），且**含已加工过的行**

    第三点这条用例的存在理由（B1b §一 冲突①）：加工队列那一条多查「还没加工」，
    首页不能跟着它——一加工就消失的那一页没法展示加工后的信息。
    """
    _clear_opportunities()
    id_early = _add_row("首页 早", score=0.9, discovered_at="2026-10-01 00:00:00")
    id_processed = _add_row("首页 已加工", score=0.8, discovered_at="2026-10-02 00:00:00",
                            screened_at="2026-10-03 00:00:00")
    id_edge = _add_row("首页 压线", score=ai_filter.MIN_RELEVANCE_SCORE,
                       discovered_at="2026-10-03 00:00:00")
    id_low = _add_row("首页 低分", score=round(ai_filter.MIN_RELEVANCE_SCORE - 0.01, 2),
                      discovered_at="2026-10-09 00:00:00")
    id_no = _add_row("首页 排除档", score=0.9, hint=EligibilityHint.LIKELY_NO,
                     discovered_at="2026-10-09 00:00:00")
    id_taken = _add_row("首页 已处理", score=0.9, status="interested",
                        discovered_at="2026-10-09 00:00:00")

    rows = ai_filter.fetch_home_candidates(limit=10)
    ids = [r["id"] for r in rows]
    # 按发现时间倒序；三条被挡在门外的即使排在最新也不出现。
    # 压线那条的分数用的就是常量本身（≥ 而非 >），改比较符号会在这里红。
    assert ids == [id_edge, id_processed, id_early], ids
    for excluded in (id_low, id_no, id_taken):
        assert excluded not in ids, f"#{excluded} 不该上首页"
    # 已加工过的那条**仍然在**首页——这正是它与加工队列那条查询唯一实质的差别
    # （队列多看了「还没加工」那一格；首页看了就会一加工就消失）
    assert id_processed in ids, ids
    assert [r["id"] for r in ai_filter.fetch_home_candidates(limit=1)] == [id_edge]
    # 返回整行：首页 ②④ 与 B2 那行「能直接拿走」读的就是这四格
    for key in ("ai_reason", "ai_next_step", "ai_screened_at", "usable_assets"):
        assert key in rows[0], f"fetch_home_candidates 没带出 {key}"
    print("[OK] fetch_home_candidates: three rules, order, keeps processed rows")


def test_unprocessed_flag_and_home_unprocessed_is_strict_subset_of_queue():
    """has_unprocessed_home_candidates 的三态 ＋ 裁定 ① 那条不变量的**两侧**都量

    不变量按 2026-10-17 设计席裁定 ① 的准确措辞落成两条断言，一条不多一条不少：
    ① 首页取回的每一条都落在「可被加工」的那两个门槛（分数、资格）内；
    ② 其中**还没加工**的那一子集 ⊆ 加工队列的取回集合。

    为什么不写成等号（这一条是执行席对裁定 ① 那句「子集**就是**队列取回集」的实测修正，
    已按实上报）：队列那条查询不看 `status`，所以「非 new 且还没加工」的行**在队列、
    不在首页**——下面那条 `已处理未加工` 就是现成的反证，这里把它断言出来而不是回避它。
    写成 `==` 会在下次加一条 `status≠new` 的未加工行时红，而那不是缺陷。

    两个查询共用一个 WHERE 拼装点（`_home_candidate_where`），所以这里逐值对拍，
    不靠读代码的人自己比 SQL。

    ⚠️ 下面四处是**无参**调用：`limit` 形参从未参与那条 SQL（结尾写死 `LIMIT 1`），
    2026-10-17 §五.64 ⑤ 定案把它删掉，名字与实情从此一致。
    """
    _clear_opportunities()
    assert ai_filter.has_unprocessed_home_candidates() is False, "空库不该发起加工"

    _add_row("待加工 A", score=0.8, discovered_at="2026-10-01 00:00:00")
    assert ai_filter.has_unprocessed_home_candidates() is True

    # 全加工完 ⇒ False（首页因此不再起线程；这一条同时证明它真的在看 ai_screened_at）
    ai_filter_opportunities(limit=5, llm_fn=lambda m: _ok_json())
    assert ai_filter.has_unprocessed_home_candidates() is False

    # 门槛外的未加工行不算：既不上首页也不进队列，不该因此每刷新一次就摸一次模型
    _add_row("低分未加工", score=0.1)
    _add_row("排除档未加工", score=0.9, hint=EligibilityHint.LIKELY_NO)
    # 这一条不同：它被首页的 `status` 那一格挡住，但队列不看 `status` ⇒ 它是下面那个
    # 「真包含于」的反证，也是「has_unprocessed 只看首页那一族」的正面证据
    id_ignored_unprocessed = _add_row("已处理未加工", score=0.9, status="ignored")
    assert ai_filter.has_unprocessed_home_candidates() is False

    # 包含关系：首页那几条里凡是还没加工的，必然出现在加工队列的取回集合里。
    # 两边都取足够大的 limit：这条断言要钉的是**两条 WHERE 的包含关系**，不是 limit
    # 的窗口——按 limit=3 对拍，上面那条 status≠new 却排在最新的行会先占掉队列的位置，
    # 测出来的是窗口之争，与「门槛有没有漂」无关。
    for i, score in enumerate((0.9, 0.8, 0.7)):
        _add_row(f"对拍 {i}", score=score, discovered_at=f"2026-10-1{i} 00:00:00")
    home = {r["id"] for r in ai_filter.fetch_home_candidates(50)}
    home_unprocessed = {r["id"] for r in ai_filter.fetch_home_candidates(50)
                        if not (r.get("ai_screened_at") or "").strip()}
    queue_ids = {r["id"] for r in ai_filter._fetch_candidates(50)}
    assert home_unprocessed, "对拍数据没造出未加工的首页候选，这条会空转"
    assert home_unprocessed <= queue_ids, (home_unprocessed, queue_ids)
    # 严格那一半：反证必须**真的**在队列里而不在首页里，否则上面的 ⊆ 只是恰好相等
    assert id_ignored_unprocessed in queue_ids, "反证没进队列 ⇒ 队列现在看 status 了？"
    assert id_ignored_unprocessed not in home, "反证上了首页 ⇒ 首页的 status 门槛漂了"
    assert home_unprocessed < queue_ids, (
        f"两侧相等，这条用例就退化成 ==（反证失效）：{home_unprocessed} / {queue_ids}")
    print("[OK] has_unprocessed_home_candidates & home-unprocessed STRICT subset of queue")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== ai filter test ===\n")
    setup()
    test_db_columns_exist()
    test_ai_filter_opportunity_success()
    test_ai_filter_opportunity_clamping_and_whitelist()
    test_ai_filter_opportunity_failure_defaults()
    test_ai_filter_candidates_selection_and_persist()
    test_ai_filter_batch_failure_isolated()
    test_ai_filter_retry_on_empty_then_success()
    test_ai_filter_retry_exhausted_returns_defaults()
    test_ai_filter_no_retry_on_network_error()
    test_ai_filter_retry_with_large_max_tokens_single_round()
    test_ui_ai_button_callback_contract()
    test_prompt_asks_for_next_step_right_after_reason()
    test_next_step_parsed_and_length_capped()
    test_next_step_missing_is_none_not_invented()
    test_next_step_persisted_by_batch()
    test_prompt_asks_for_usable_assets_right_after_next_step()
    test_usable_assets_parsed_stripped_and_capped()
    test_usable_assets_sentinel_set_is_verbatim()
    test_usable_assets_list_answer_is_joined_not_repr()
    test_usable_assets_persisted_by_batch()
    test_usable_assets_is_not_a_home_filter()
    test_fetch_home_candidates_rules_and_order()
    test_unprocessed_flag_and_home_unprocessed_is_strict_subset_of_queue()
    test_cleanup()
    print("\n=== ALL AI FILTER TESTS PASSED ===")
