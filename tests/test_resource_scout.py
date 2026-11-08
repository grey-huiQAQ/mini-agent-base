# -*- coding: utf-8 -*-
"""
Resource Scout（外部资源机会发现）模块测试（离线：mock 搜索 Provider）

覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
- 数据库建表：opportunities 表存在，含全部列（含 v0.4 相关性/AI 列）与
  source_url 唯一约束，init_db 幂等
- 机会 CRUD：新增（URL 重复返回 0）/ 查询 / 列表排序（发现时间倒序）/
  状态白名单更新 / 计数 / 按状态过滤 / 删除
- 粗分类纯函数：classify_type 关键词规则（hackathon/competition/credit/
  course/scholarship/other）
- 相关性粗筛纯函数（v0.4）：强/弱/不相关关键词与分数、冲突优先级、
  英文词边界（art/smart）、软条件降分、空文本
- 扫描函数 scan_once（mock 搜索）：结果清洗入库、轮内与跨轮 URL 去重、
  type 分类、beneficiary/status 默认值、相关性结果随机会落库、报告计数正确
- Provider 异常 / 空结果 / 缺 URL 结果：不抛异常、整轮扫描不中断
- UI 回调契约：导入 app.py，用 mock 扫描器跑通「后台扫描 → 刷新列表 →
  标记状态」全流程，返回元组与 outputs 匹配
"""
import json
import os
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；保留 _TMP_DIR / _TMP_DB 两个旧变量名，既有断言无需改动。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import init_db, get_connection
from core.resource_scout import (
    ResourceScout,
    OpportunityStatus,
    classify_type,
    evidence_to_opportunity,
    check_relevance,
    check_soft_conditions,
    matched_keywords_to_json,
)
from core.information_engine.models import Evidence, now_str




def setup():
    """初始化独立测试数据库（所有表建在临时库中）"""
    init_db()


# ---------- Mock 工具 ----------

class MockProvider:
    """测试用 mock 搜索源：固定返回预置证据，可配置抛异常（模拟网络故障）"""

    def __init__(self, name="Mock", results=None, error=None):
        self.name = name
        self.results = list(results or [])
        self.error = error
        self.search_calls = 0

    def search(self, query, limit=5):
        self.search_calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return list(self.results)


def make_evidence(title="测试机会", url="https://example.com/opp/1",
                  snippet="", source="Mock"):
    """构造一条测试证据（snippet 缺省给标题相关摘要）"""
    return Evidence(
        source_url=url, source_name=source, title=title,
        snippet=snippet or f"{title} 的摘要内容",
        confidence=0.7, retrieved_at=now_str(),
    )


def _count_opportunities(status=None) -> int:
    """直接查临时库的机会条数（不依赖被测试模块，独立校验用）"""
    conn = get_connection()
    try:
        sql = "SELECT COUNT(*) AS c FROM opportunities"
        params = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        row = conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


def _clear_opportunities():
    """清空临时库中的机会行（临时库专属；保证各测试互相独立、计数确定）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


# ---------- 1. 独立测试数据库与建表 ----------

def test_independent_database_and_table():
    """隔离验证：opportunities 表建在临时库，且临时库与默认库路径不同"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    assert _TMP_DB != os.path.join("data", "memory.db"), "不得使用默认业务库"
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
        # PRAGMA index_list 行：0=seq 1=name 2=unique 3=origin 4=partial
        uniques = [r for r in conn.execute(
            "PRAGMA index_list(opportunities)").fetchall() if r[2] == 1]
    finally:
        conn.close()
    for col in ["id", "title", "type", "source_url", "summary", "beneficiary",
                "status", "notes", "confidence", "discovered_at",
                "eligibility_hint", "eligibility_reason",
                # v0.4 相关性粗筛 + AI 深度筛选列
                "relevance_status", "relevance_score", "matched_keywords",
                "ai_eligibility", "ai_reason", "ai_value_score",
                "ai_project_reuse", "ai_recommendation", "ai_confidence",
                "ai_screened_at"]:
        assert col in cols, f"opportunities 缺少列 {col}，实际为 {cols}"
    # source_url 唯一约束存在（按 URL 去重的库级保障：UNIQUE 自动生成唯一索引）
    assert any(str(r[1]).startswith("sqlite_autoindex_opportunities_")
               for r in uniques), \
        f"source_url 应有唯一索引，实际为 {[r[1] for r in uniques]}"
    print("[OK] independent database and opportunities table")


def test_init_db_idempotent():
    """重复调用 init_db 不应报错（幂等，旧库自动补建新表）"""
    init_db()
    init_db()
    print("[OK] init db idempotent")


# ---------- 2. 机会 CRUD ----------

def test_opportunity_crud():
    """机会 CRUD：新增（含 URL 重复拒绝）→ 查询 → 列表排序 → 状态更新 → 删除"""
    # 新增
    oid1 = ResourceScout.add_opportunity(
        "2026 全国大学生创新大赛", "https://example.com/contest/2026",
        type_key="competition", summary="面向在校学生", beneficiary="user",
        status=OpportunityStatus.NEW)
    assert oid1 and oid1 > 0, "新增应返回新记录 ID"
    # URL 重复 → 拒绝并返回 0
    dup = ResourceScout.add_opportunity(
        "同一链接标题不同", "https://example.com/contest/2026")
    assert dup == 0, "相同 source_url 不应重复入库"
    assert _count_opportunities() == 1

    # 空标题 / 空 URL / 非法状态 → 拒绝（返回 0）
    assert ResourceScout.add_opportunity("   ", "https://example.com/x") == 0
    assert ResourceScout.add_opportunity("标题", "   ") == 0
    assert ResourceScout.add_opportunity("标题", "https://example.com/y",
                                         status="evil") == 0

    # 查询与默认值
    opp = ResourceScout.get_opportunity(oid1)
    assert opp is not None
    assert opp["title"] == "2026 全国大学生创新大赛"
    assert opp["type"] == "competition"
    assert opp["beneficiary"] == "user"
    assert opp["status"] == OpportunityStatus.NEW
    assert opp["confidence"] == 0.5, "未传 confidence 时应使用默认 0.5"

    # 列表排序：后插入的（同一秒内按 id DESC）排前面
    oid2 = ResourceScout.add_opportunity(
        "后插入的机会", "https://example.com/later/1")
    assert oid2 > oid1
    rows = ResourceScout.list_opportunities()
    assert rows[0]["id"] == oid2 and rows[1]["id"] == oid1, \
        "列表应按发现时间倒序（同一秒内新 ID 在前）"
    assert len(ResourceScout.list_opportunities(status=OpportunityStatus.NEW)) == 2
    assert ResourceScout.count_opportunities() == 2

    # 状态更新：合法状态生效；非法状态 / 不存在 ID 返回 False
    assert ResourceScout.update_status(oid1, OpportunityStatus.INTERESTED)
    opp1 = ResourceScout.get_opportunity(oid1)
    assert opp1["status"] == OpportunityStatus.INTERESTED
    assert _count_opportunities(status=OpportunityStatus.INTERESTED) == 1
    assert len(ResourceScout.list_opportunities(
        status=OpportunityStatus.INTERESTED)) == 1
    assert ResourceScout.update_status(oid1, "bad_status") is False
    assert ResourceScout.update_status(999999, OpportunityStatus.NEW) is False
    assert ResourceScout.get_opportunity(999999) is None

    # 删除
    assert ResourceScout.delete_opportunity(oid2)
    assert ResourceScout.get_opportunity(oid2) is None
    assert ResourceScout.delete_opportunity(oid2) is False, "重复删除应返回 False"
    assert _count_opportunities() == 1
    print("[OK] opportunity crud")


# ---------- 3. 粗分类纯函数 ----------

def test_classify_type():
    """关键词粗分类：中文/英文关键词命中对应类型，无关内容归 other"""
    assert classify_type("Hackathon 2026 黑客松", "面向开发者") == "hackathon"
    assert classify_type("Kaggle 竞赛", "") == "competition"
    assert classify_type("全国大学生计算机设计大赛 报名", "") == "competition"
    assert classify_type("免费 GPU credits 申请", "") == "credit"
    assert classify_type("Coursera 免费课程", "") == "course"
    assert classify_type("奖学金申请指南 scholarship", "") == "scholarship"
    assert classify_type("随便一段无关文字", "") == "other"
    assert classify_type("", "") == "other"
    assert classify_type("   ", "  ") == "other"
    print("[OK] classify type keywords")


def test_evidence_to_opportunity_cleaning():
    """Evidence → Opportunity 清洗：标题/URL 可用才转换；缺 URL 返回 None；
    摘要超长截断；status 固定 new；type 自动分类"""
    ev = make_evidence("  全国大学生 竞赛 报名  ", "https://e.example.com/1",
                       snippet="  摘要内容  ", source="Mock")
    opp = evidence_to_opportunity(ev)
    assert opp is not None
    assert opp.title == "全国大学生 竞赛 报名", "标题应去除首尾空白"
    assert opp.type == "competition"
    assert opp.status == OpportunityStatus.NEW
    assert opp.beneficiary == "user"
    # 缺 URL（只有标题）→ None（不可点击的机会不入库，避免撞唯一约束）
    assert evidence_to_opportunity(make_evidence(
        "只有标题", url="  ", snippet="x")) is None
    # 缺标题 → None
    assert evidence_to_opportunity(Evidence(source_url="https://x.com/1")) is None
    # 摘要超长截断
    long_summary = "长" * 600
    opp2 = evidence_to_opportunity(make_evidence(
        "摘要截断测试", "https://e.example.com/2", snippet=long_summary))
    assert opp2 is not None and len(opp2.summary) <= 500
    print("[OK] evidence to opportunity cleaning")


# ---------- 3.5 相关性粗筛纯函数（v0.4） ----------

def test_relevance_strong_and_weak_scores():
    """强相关 → relevant；分数 = 0.4×强 + 0.2×弱（封顶 1.0）"""
    # 单个强相关：0.4
    status, score, pos, neg = check_relevance("Hackathon 2026", "")
    assert status == "relevant" and score == 0.4, (status, score)
    assert "hackathon" in pos and neg == []

    # 强+弱：hackathon/developers/gpu 三个强 + student 一个弱 → 1.4 → 封顶 1.0
    status2, score2, pos2, _ = check_relevance(
        "Hackathon for student developers", "free GPU credits")
    assert status2 == "relevant" and score2 == 1.0, (status2, score2)
    assert "student" in pos2 and "hackathon" in pos2 and "gpu" in pos2

    # 中文强相关
    status3, score3, _, _ = check_relevance("全国大学生编程竞赛", "")
    assert status3 == "relevant" and score3 > 0, (status3, score3)
    print("[OK] relevance strong & weak scoring")


def test_relevance_irrelevant_excludes_noise():
    """不相关命中且无强相关 → irrelevant（分数 0），用于排除物理等噪声"""
    status, score, pos, neg = check_relevance("全国中学生物理竞赛", "")
    assert status == "irrelevant" and score == 0.0, (status, score)
    assert "物理" in neg and "竞赛" in pos, (pos, neg)

    status2, score2, _, neg2 = check_relevance(
        "Biology olympiad for students", "")
    assert status2 == "irrelevant" and score2 == 0.0, (status2, score2)
    assert "biology" in neg2
    print("[OK] relevance irrelevant excludes noise")


def test_relevance_conflict_strong_wins_and_word_boundary():
    """冲突：强相关 + 不相关 → 以强相关为准（relevant）；
    英文词边界：art 不命中 smart/chart 词内子串"""
    status, score, pos, neg = check_relevance("AI physics hackathon", "")
    assert status == "relevant" and score > 0, (status, score)
    assert "physics" in neg and "hackathon" in pos, (pos, neg)

    # "Smart chart" 内含 art 子串但不应命中不相关词 art（词边界）
    status2, score2, _, neg2 = check_relevance("Smart chart design event", "")
    assert status2 != "irrelevant", (status2, score2)
    assert "art" not in neg2, neg2
    print("[OK] relevance conflict & english word boundary")


def test_relevance_weak_only_unknown():
    """仅弱相关（无强相关、无不相关）→ unknown，分数 0.2"""
    status, score, pos, neg = check_relevance("大学生奖学金申请", "")
    assert status == "unknown" and score == 0.2, (status, score)
    assert pos and "奖学金" in pos and neg == []
    # 无任何命中 → unknown 0
    status2, score2, pos2, _ = check_relevance("一段普通资讯文字", "")
    assert status2 == "unknown" and score2 == 0.0 and pos2 == []
    # 空文本
    status3, score3, _, _ = check_relevance("", "")
    assert status3 == "unknown" and score3 == 0.0
    print("[OK] relevance weak only / none / empty")


def test_relevance_soft_conditions_downgrade_only():
    """软条件（费用/组队等）：不淘汰，仅降分；check_soft_conditions 可单独查询"""
    status, score, _, _ = check_relevance("Hackathon", "")
    assert score == 0.4, score
    status2, score2, _, _ = check_relevance(
        "Hackathon", "报名费 99 元，需组队提交材料")
    assert status2 == "relevant", status2
    assert 0.0 <= score2 < 0.4, f"软条件应降低分数：{score2}"

    soft = check_soft_conditions("Hackathon", "报名费 99 元，需组队提交材料")
    assert "报名费" in soft and "组队" in soft, soft
    # 纯软条件机会不判不相关，只保持弱相关 unknown 并降分
    status3, score3, _, _ = check_relevance("线上课程", "需付费 199 元")
    assert status3 == "unknown" and score3 <= 0.2, (status3, score3)
    print("[OK] relevance soft conditions downgrade only")


def test_relevance_evidence_and_json():
    """evidence_to_opportunity：相关性随机会写入；matched JSON 可解析"""
    ev = make_evidence("Hackathon for students（报名费 50 元）",
                       "https://rel.example.com/1", snippet="online event")
    opp = evidence_to_opportunity(ev)
    assert opp is not None
    assert opp.relevance_status == "relevant" and opp.relevance_score > 0
    matched = json.loads(opp.matched_keywords)
    assert "positive" in matched and "negative" in matched and "soft" in matched
    assert matched["soft"], "标题含报名费应记录软条件命中"
    # 直接序列化函数
    s = matched_keywords_to_json(["hackathon"], ["physics"], ["报名费"])
    assert json.loads(s)["soft"] == ["报名费"]
    print("[OK] relevance persisted via evidence & matched json")


def test_scan_once_persists_relevance():
    """scan_once（mock 搜索）：相关性结果随机会一起落库"""
    _clear_opportunities()
    provider = MockProvider(name="Mock", results=[
        make_evidence("Hackathon GPU credits for students",
                      "https://rscan.example.com/1"),
        make_evidence("全国中学生物理竞赛", "https://rscan.example.com/2"),
        make_evidence("普通资讯页", "https://rscan.example.com/3"),
    ])
    scout = ResourceScout(queries=["相关性扫描"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 3, report
    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities()}
    assert rows["https://rscan.example.com/1"]["relevance_status"] == "relevant"
    assert rows["https://rscan.example.com/1"]["relevance_score"] > 0
    assert rows["https://rscan.example.com/2"]["relevance_status"] == "irrelevant"
    assert rows["https://rscan.example.com/2"]["relevance_score"] == 0
    assert rows["https://rscan.example.com/3"]["relevance_status"] == "unknown"
    # matched_keywords 是合法 JSON
    json.loads(rows["https://rscan.example.com/1"]["matched_keywords"])
    print("[OK] scan once persists relevance")


# ---------- 4. 扫描函数 scan_once（mock 搜索） ----------

def test_scan_once_mock_search():
    """扫描：结果入库、类型分类、轮内 URL 去重、beneficiary/status 默认值、
    报告计数正确"""
    _clear_opportunities()
    provider = MockProvider(name="MockWeb", results=[
        make_evidence("2026 全国大学生竞赛 报名", "https://scan.example.com/1"),
        make_evidence("GitHub Student Pack hackathon 免费额度",
                      "https://scan.example.com/2"),
        make_evidence("Coursera 免费课程", "https://scan.example.com/3"),
        make_evidence("与第一条完全相同的 URL", "https://scan.example.com/1"),  # 轮内重复
    ])
    scout = ResourceScout(queries=["测试查询 A"],
                          providers=[provider], limit_per_query=5)
    report = scout.scan_once()

    assert report["queries"] == ["测试查询 A"]
    assert report["found"] == 4, "重复 URL 仍计入 found（发现数）"
    assert report["inserted"] == 3, "轮内重复 URL 只入库一次"
    assert report["duplicates"] == 1, "轮内重复应计入 duplicates"
    assert report["skipped_invalid"] == 0
    assert report["errors"] == []
    assert len(report["inserted_ids"]) == 3
    assert report["summary_text"] and "新入库 3 条" in report["summary_text"]

    # 落库校验：type 分类 / 默认值 / 状态
    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities()}
    assert rows["https://scan.example.com/1"]["type"] == "competition"
    assert rows["https://scan.example.com/2"]["type"] == "hackathon"
    assert rows["https://scan.example.com/3"]["type"] == "course"
    for r in rows.values():
        assert r["beneficiary"] == "user"
        assert r["status"] == OpportunityStatus.NEW
        assert r["title"] and r["discovered_at"]
    assert provider.search_calls == 1, "单查询应只触发一次搜索"
    print("[OK] scan once with mock search")


def test_scan_once_idempotent_across_runs():
    """重复扫描幂等：第二轮全部是重复（0 新增），不产生重复行"""
    _clear_opportunities()
    provider = MockProvider(name="Mock", results=[
        make_evidence("幂等测试机会", "https://idem.example.com/1"),
    ])
    scout = ResourceScout(queries=["幂等查询"], providers=[provider])
    r1 = scout.scan_once()
    assert r1["inserted"] == 1
    r2 = scout.scan_once()
    assert r2["inserted"] == 0, "第二轮不应新增任何行"
    assert r2["duplicates"] == 1
    assert _count_opportunities() == 1, "库内不应出现重复行"
    print("[OK] scan once idempotent across runs")


def test_scan_once_tolerates_errors_and_empty():
    """容错：Provider 抛异常 / 空结果 / 缺 URL 结果都不中断整轮扫描"""
    _clear_opportunities()
    # 一个会抛异常 + 一个正常返回空
    bad = MockProvider(name="Bad", error="网络超时")
    empty = MockProvider(name="Empty", results=[])
    scout = ResourceScout(queries=["q1", "q2"], providers=[bad, empty])
    report = scout.scan_once()  # 不应抛异常
    assert report["found"] == 0 and report["inserted"] == 0
    assert report["skipped_invalid"] == 0

    # 缺 URL / 缺标题的结果被丢弃计数，不影响正常结果入库
    provider = MockProvider(name="Mixed", results=[
        make_evidence("正常机会", "https://mixed.example.com/1"),
        make_evidence("只有标题没有链接", url=""),          # 丢弃
        Evidence(source_url="https://mixed.example.com/2"),   # 缺标题：丢弃
    ])
    scout2 = ResourceScout(queries=["q"], providers=[provider])
    report2 = scout2.scan_once()
    assert report2["found"] == 1 and report2["inserted"] == 1
    assert report2["skipped_invalid"] == 2
    assert report2["errors"] == []

    # 无查询配置：友好报告，不崩溃
    scout3 = ResourceScout(queries=[], providers=[provider])
    report3 = scout3.scan_once()
    assert report3["errors"] and "未配置" in report3["errors"][0]
    assert report3["inserted"] == 0
    print("[OK] scan tolerates errors and empty results")


def test_scan_once_across_queries_dedup():
    """多查询间去重：同一 URL 被不同查询命中，只入库一次"""
    _clear_opportunities()
    provider = MockProvider(name="MultiQ", results=[
        make_evidence("跨查询同一机会", "https://cross.example.com/1"),
    ])
    scout = ResourceScout(queries=["查询甲", "查询乙"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 1
    assert report["duplicates"] == 1, "第二个查询命中的相同 URL 应判重复"
    assert _count_opportunities() == 1
    print("[OK] dedup across queries")


# ---------- 5. UI 回调契约 ----------

def test_ui_callback_contract():
    """UI 回调契约：后台扫描 → 刷新列表 → 标记状态，返回元组与 outputs 匹配"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 回调契约测试")
        return
    # Phase 3b：扫描器实例绑定已随回调迁入 resource_controller，注入目标随之迁移
    from ui.controllers import resource_controller as _rc

    _clear_opportunities()  # 保证计数断言独立（临时库，不影响业务数据）
    # 用 mock 扫描器替换模块级实例（真实扫描器联网，测试必须离线）
    mock_provider = MockProvider(name="MockUI", results=[
        make_evidence("UI 契约竞赛机会", "https://ui.example.com/contest/1",
                      snippet="面向学生的比赛"),
    ])
    mock_scout = ResourceScout(queries=["UI 契约查询"], providers=[mock_provider],
                               timeout_sec=5)
    original_scout = _rc.resource_scout
    _rc.resource_scout = mock_scout
    try:
        # 1) 未开始 → 触发扫描：返回状态文本（后台线程执行，立即返回）
        status = app_module.do_scan_opportunities()
        assert isinstance(status, str) and "已启动" in status, status

        # 2) 等待后台扫描完成（mock 瞬时返回；轮询状态字典）
        deadline = time.time() + 15
        while time.time() < deadline and \
                app_module.opportunity_scan_state.get("running"):
            time.sleep(0.05)
        assert not app_module.opportunity_scan_state.get("running"), "扫描应已结束"
        report = app_module.opportunity_scan_state.get("report")
        assert report is not None, "扫描完成后应写入 report"
        assert report["inserted"] == 1
        oid = report["inserted_ids"][0]

        # 3) 刷新列表：Markdown 含机会标题；下拉框带选项
        panel_md, dd_update = app_module.refresh_opportunities_panel(None)
        assert isinstance(panel_md, str)
        assert "UI 契约竞赛机会" in panel_md, panel_md
        assert "共 1 条机会" in panel_md, panel_md
        choices = dd_update["choices"]
        assert choices and oid in [c[1] for c in choices], choices

        # 4) 标记状态：返回 (状态, 列表, 下拉框) 三元组；数据库已更新
        res = app_module.do_set_opportunity_status(
            oid, OpportunityStatus.INTERESTED)
        assert isinstance(res, tuple) and len(res) == 3, \
            "do_set_opportunity_status 必须返回三元组以匹配 outputs"
        msg, panel_md2, dd_update2 = res
        assert "感兴趣" in msg, msg
        opp = ResourceScout.get_opportunity(oid)
        assert opp["status"] == OpportunityStatus.INTERESTED, opp
        # 下拉框保留选中值（id 仍存在）
        assert dd_update2["value"] == oid
        # 列表 Markdown 中出现对应状态标签
        assert "感兴趣" in panel_md2, panel_md2

        # 5) 未选中机会 → 友好提示而非崩溃
        res_none = app_module.do_set_opportunity_status(
            None, OpportunityStatus.FAVORITED)
        assert res_none[0] == "⚠️ 请先在下拉框中选择一条机会"

        # 6) 重复扫描（同 mock）：全重复，不新增行
        status2 = app_module.do_scan_opportunities()
        assert "已启动" in status2
        deadline2 = time.time() + 15
        while time.time() < deadline2 and \
                app_module.opportunity_scan_state.get("running"):
            time.sleep(0.05)
        report2 = app_module.opportunity_scan_state.get("report")
        assert report2 is not None and report2["inserted"] == 0
        assert ResourceScout.count_opportunities() == 1, "重复扫描不应新增行"

        print(f"[OK] ui callback contract flow (opportunity #{oid})")
    finally:
        _rc.resource_scout = original_scout


# ---------- P7 批次 1b：一键清理过期比赛（ignore_overdue） ----------

def _set_opportunity(oid, **fields):
    """按下标更新机会若干列（测试夹具；列名由调用方显式给出）"""
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        conn.execute(f"UPDATE opportunities SET {cols} WHERE id = ?",
                     list(fields.values()) + [oid])
        conn.commit()
    finally:
        conn.close()


def _status_of(oid):
    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM opportunities WHERE id = ?",
                           (oid,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def test_ignore_overdue():
    """一键清理：只清「截止日已过 且 仍可见」的行；今天到期、未来、未知(null)、
    已忽略的一律不动（未知 ≠ 已过期）；重复调用第二次返回 0"""
    from datetime import date as _date
    today = _date(2026, 9, 19)
    past = ResourceScout.add_opportunity(
        "已过期比赛A", "https://example.com/overdue/a")
    _set_opportunity(past, deadline="2026-09-13", status=OpportunityStatus.NEW)
    past_fav = ResourceScout.add_opportunity(
        "已过期但已收藏B", "https://example.com/overdue/b")
    _set_opportunity(past_fav, deadline="2026-09-01",
                     status=OpportunityStatus.FAVORITED)
    due_today = ResourceScout.add_opportunity(
        "今天到期C", "https://example.com/overdue/c")
    _set_opportunity(due_today, deadline="2026-09-19")
    future = ResourceScout.add_opportunity(
        "未来到期D", "https://example.com/overdue/d")
    _set_opportunity(future, deadline="2026-09-22")
    unknown = ResourceScout.add_opportunity(
        "截止日未知E", "https://example.com/overdue/e")
    _set_opportunity(unknown, deadline=None)
    already = ResourceScout.add_opportunity(
        "已忽略F", "https://example.com/overdue/f")
    _set_opportunity(already, deadline="2026-08-01",
                     status=OpportunityStatus.IGNORED)

    n = ResourceScout.ignore_overdue(today=today)
    assert n == 2, f"应只清 2 条已过且可见的（new + favorited），实际 {n}"
    assert _status_of(past) == OpportunityStatus.IGNORED
    assert _status_of(past_fav) == OpportunityStatus.IGNORED
    assert _status_of(due_today) == OpportunityStatus.NEW, "今天到期不算已过"
    assert _status_of(future) == OpportunityStatus.NEW
    assert _status_of(unknown) == OpportunityStatus.NEW, "截止日未知不得被清"
    assert _status_of(already) == OpportunityStatus.IGNORED
    assert ResourceScout.ignore_overdue(today=today) == 0, "重复调用应为 0（无行可改）"

    for oid in (past, past_fav, due_today, future, unknown, already):
        assert ResourceScout.delete_opportunity(oid)
    print("[OK] ignore overdue")


def test_cleanup_overdue_ui_callback_contract():
    """UI 回调契约：do_cleanup_overdue 返回三元组匹配 outputs（3 个输出）"""
    import app as app_module

    res = app_module.do_cleanup_overdue("all", "all")
    assert isinstance(res, tuple) and len(res) == 3, \
        "do_cleanup_overdue 必须返回三元组以匹配 outputs"
    assert isinstance(res[0], str) and res[0]
    print("[OK] cleanup overdue ui callback contract")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== resource scout test ===\n")
    setup()
    test_independent_database_and_table()
    test_init_db_idempotent()
    test_opportunity_crud()
    test_classify_type()
    test_evidence_to_opportunity_cleaning()
    test_relevance_strong_and_weak_scores()
    test_relevance_irrelevant_excludes_noise()
    test_relevance_conflict_strong_wins_and_word_boundary()
    test_relevance_weak_only_unknown()
    test_relevance_soft_conditions_downgrade_only()
    test_relevance_evidence_and_json()
    test_scan_once_persists_relevance()
    test_scan_once_mock_search()
    test_scan_once_idempotent_across_runs()
    test_scan_once_tolerates_errors_and_empty()
    test_scan_once_across_queries_dedup()
    test_ui_callback_contract()
    test_ignore_overdue()
    test_cleanup_overdue_ui_callback_contract()
    test_cleanup()
    print("\n=== ALL RESOURCE SCOUT TESTS PASSED ===")
