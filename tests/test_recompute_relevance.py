# -*- coding: utf-8 -*-
"""
Resource Scout 相关性存量重算（recompute_relevance v0.4 修复）测试（离线）

背景与覆盖：
- 34 条存量机会（v0.4 落库链路启用前写入）的 relevance_status /
  relevance_score / matched_keywords 全部为迁移默认值（unknown / 0.0 / []），
  导致「AI 深度筛选」候选（相关性 ≥ 0.3 且资格非 likely_no）恒为 0；
- ResourceScout.recompute_relevance() 按每条 title + summary 重新执行
  check_relevance / check_soft_conditions，只更新相关性三列，返回更新行数。

本文件覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入前切换）
- 存量旧记录回填：模拟 v0.4 前的行（三列清零）→ 重算后状态/分数/关键词
  与 check_relevance 直接计算结果一致；只动相关性三列（资格/状态/画像/
  AI 字段快照对比不变）；不删除任何行
- 空表重算返回 0；重复重算幂等（值稳定、行数不变）
- 手动新增（add_opportunity 默认不写相关性）同样可被重算补齐
- UI 回调契约：do_recompute_relevance 返回三元组匹配 outputs、状态提示
  含已重算条数、列表 Markdown 展示更新后的相关性行、筛选视图保持
"""
import json
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；本文件不再引用 DB_PATH 变量，故只保留 helper 返回的目录名。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

from memory.database import init_db, get_connection
from core.resource_scout import (
    ResourceScout,
    OpportunityStatus,
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
    """测试用 mock 搜索源：固定返回预置证据"""

    def __init__(self, name="Mock", results=None):
        self.name = name
        self.results = list(results or [])
        self.search_calls = 0

    def search(self, query, limit=5):
        self.search_calls += 1
        return list(self.results)


def make_evidence(title, url, snippet=""):
    """构造一条测试证据（snippet 缺省给空串，保证重算结果只取决于标题）"""
    return Evidence(
        source_url=url, source_name="Mock", title=title,
        snippet=snippet or "", confidence=0.7, retrieved_at=now_str(),
    )


def _clear_opportunities():
    """清空临时库中的机会行（临时库专属；保证各测试互相独立、计数确定）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


def _stale_columns():
    """把全部机会的相关性三列重置为迁移默认值（模拟 v0.4 落库前的旧记录）"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE opportunities SET relevance_status = 'unknown', "
            "relevance_score = 0, matched_keywords = '[]'")
        conn.commit()
    finally:
        conn.close()


def _snapshot_without_relevance():
    """快照全部机会行（剔除相关性三列），用于验证重算只动这三列"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM opportunities").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("relevance_status", "relevance_score",
                        "matched_keywords"):
                d.pop(col, None)
            out.append(d)
        return out
    finally:
        conn.close()


def _expected_relevance(title, summary=""):
    """重算的预期结果：直接调用纯函数，避免测试内重复规则推导"""
    status, score, pos, neg = check_relevance(title, summary or "")
    soft = check_soft_conditions(title, summary or "")
    return status, score, matched_keywords_to_json(pos, neg, soft)


# ---------- 1. 存量旧记录回填（核心场景） ----------

def test_recompute_backfills_stale_rows_only_relevance():
    """模拟 v0.4 前的旧记录（相关性三列为默认值）→ 重算后补齐；
    只动相关性三列：资格/状态/画像/AI 字段快照完全不变、行不丢失"""
    _clear_opportunities()
    # 先经正常扫描链路入库（保证类型/资格等字段完整），再把相关性三列清零
    provider = MockProvider(results=[
        make_evidence("Hackathon 2026 for developers",
                      "https://rr.example.com/1", snippet="free GPU credits"),
        make_evidence("全国中学生物理竞赛", "https://rr.example.com/2"),
        make_evidence("普通资讯页面", "https://rr.example.com/3"),
        make_evidence("大学生奖学金申请指南", "https://rr.example.com/4"),
    ])
    scout = ResourceScout(queries=["重算测试"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 4, report
    before = _snapshot_without_relevance()
    # 模拟旧记录：相关性三列全为迁移默认值
    _stale_columns()
    rows = ResourceScout.list_opportunities(limit=50)
    for r in rows:
        assert r["relevance_status"] == "unknown", r
        assert r["relevance_score"] == 0, r

    updated = ResourceScout.recompute_relevance()
    assert updated == 4, f"应更新 4 条，实际 {updated}"

    # 1) 更新后的值 = check_relevance(title, summary) 直接结果
    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities(limit=50)}
    for r in rows.values():
        exp_status, exp_score, exp_json = _expected_relevance(
            r["title"], r["summary"] or "")
        assert r["relevance_status"] == exp_status, (r["id"], r["relevance_status"], exp_status)
        assert abs(float(r["relevance_score"]) - exp_score) < 1e-9, \
            (r["id"], r["relevance_score"], exp_score)
        assert json.loads(r["matched_keywords"]) == json.loads(exp_json), \
            (r["id"], r["matched_keywords"], exp_json)
    # 覆盖四种判定：relevant（强相关）/ irrelevant / unknown(0) / unknown(0.2)
    assert rows["https://rr.example.com/1"]["relevance_status"] == "relevant"
    assert rows["https://rr.example.com/1"]["relevance_score"] > 0
    assert rows["https://rr.example.com/2"]["relevance_status"] == "irrelevant"
    assert rows["https://rr.example.com/4"]["relevance_status"] == "unknown"
    assert float(rows["https://rr.example.com/4"]["relevance_score"]) > 0

    # 2) 其它字段快照不变（资格/状态/画像/AI 等未被触碰）
    after = _snapshot_without_relevance()
    assert after == before, "重算只允许更新相关性三列，其它字段应保持不变"
    assert ResourceScout.count_opportunities() == 4, "重算不得删除/新增行"
    print(f"[OK] recompute backfills stale rows ({updated} updated)")
    print("[OK] recompute touches only relevance columns, no row lost")


def test_recompute_covers_manual_adds_and_preserves_status():
    """add_opportunity 手动新增的行（默认不写相关性）同样可被重算补齐；
    人工状态 / AI 字段不被重算覆盖"""
    _clear_opportunities()
    oid = ResourceScout.add_opportunity(
        "学生 Hackathon 黑客松", "https://rr-manual.example.com/1",
        summary="编程比赛 报名")
    assert oid > 0
    assert ResourceScout.update_status(oid, OpportunityStatus.INTERESTED)
    # 预置一条 AI 筛选结果（重算不应触碰 ai_* 列）
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE opportunities SET ai_eligibility = 'likely_yes', "
            "ai_screened_at = '2026-09-05 10:00:00' WHERE id = ?", (oid,))
        conn.commit()
    finally:
        conn.close()

    updated = ResourceScout.recompute_relevance()
    assert updated == 1, updated
    opp = ResourceScout.get_opportunity(oid)
    assert opp["relevance_status"] == "relevant", opp
    assert float(opp["relevance_score"]) > 0, opp
    assert json.loads(opp["matched_keywords"])["positive"], opp
    # 人工状态 / AI 字段保持原值
    assert opp["status"] == OpportunityStatus.INTERESTED, opp
    assert opp["ai_screened_at"] == "2026-09-05 10:00:00", opp
    assert opp["ai_eligibility"] == "likely_yes", opp
    print("[OK] recompute covers manual adds and preserves status/AI fields")


# ---------- 2. 边界与幂等 ----------

def test_recompute_empty_and_idempotent():
    """空表重算返回 0 不抛异常；重复重算结果稳定（行数与判定不变）"""
    _clear_opportunities()
    assert ResourceScout.recompute_relevance() == 0, "空表应返回 0"

    provider = MockProvider(results=[
        make_evidence("Hackathon 2026", "https://rr-idem.example.com/1"),
    ])
    report = ResourceScout(queries=["幂等"], providers=[provider]).scan_once()
    assert report["inserted"] == 1, report
    oid = report["inserted_ids"][0]
    _stale_columns()
    n1 = ResourceScout.recompute_relevance()
    assert n1 == 1, n1
    row1 = dict(ResourceScout.get_opportunity(oid))
    n2 = ResourceScout.recompute_relevance()
    assert n2 == 1, "第二次重算仍应逐行命中并返回行数"
    row2 = ResourceScout.get_opportunity(oid)
    for col in ("relevance_status", "relevance_score", "matched_keywords"):
        assert row1[col] == row2[col], (col, row1[col], row2[col])
    assert ResourceScout.count_opportunities() == 1
    print("[OK] recompute empty table returns 0 and rerun is stable")


# ---------- 3. UI 回调契约 ----------

def test_ui_recompute_callback_contract():
    """UI 回调：点「🔄 重新计算相关性」→ 重算全部存量并刷新列表，
    返回 (状态, 列表, 下拉框) 三元组匹配 outputs；筛选视图保持"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 重算回调契约测试")
        return

    _clear_opportunities()
    # 直接手动入库（add_opportunity 不写相关性 → 模拟存量旧记录）
    id1 = ResourceScout.add_opportunity(
        "Hackathon 2026 online", "https://rr-ui.example.com/1",
        eligibility_hint="likely_yes")
    id2 = ResourceScout.add_opportunity(
        "全国中学生物理竞赛", "https://rr-ui.example.com/2",
        eligibility_hint="likely_yes")
    assert id1 and id2
    # 空表/重算前校验：机会行存在但相关性为默认值
    assert ResourceScout.get_opportunity(id1)["relevance_score"] == 0

    res = app_module.do_recompute_relevance("likely_yes", "all")
    assert isinstance(res, tuple) and len(res) == 3, \
        "do_recompute_relevance 必须返回三元组以匹配 outputs"
    msg, md, dd_update = res
    assert "已重新计算 2 条" in msg, msg
    # 列表 Markdown 展示更新后的相关性（relevant 行有「相关性：✅ 相关」行；
    # irrelevant 行有「相关性：🚫 不相关」）
    assert "相关性：✅ 相关" in md, md
    assert "相关性：🚫 不相关" in md, md
    # 下拉框保留两条（筛选 likely_yes 视图不变）
    assert {c[1] for c in dd_update["choices"]} == {id1, id2}, dd_update
    # 数据库已更新
    opp1 = ResourceScout.get_opportunity(id1)
    assert opp1["relevance_status"] == "relevant", opp1
    assert float(opp1["relevance_score"]) > 0, opp1
    opp2 = ResourceScout.get_opportunity(id2)
    assert opp2["relevance_status"] == "irrelevant", opp2
    # 视图保持：资格筛选 likely_yes + 状态筛选 all 下两条都在
    md_all, _ = app_module.refresh_opportunities_panel(None, "likely_yes", "all")
    assert "共 2 条机会" in md_all, md_all
    print(f"[OK] ui recompute relevance callback contract "
          f"(opportunities #{id1}, #{id2})")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== recompute relevance test ===\n")
    setup()
    test_recompute_backfills_stale_rows_only_relevance()
    test_recompute_covers_manual_adds_and_preserves_status()
    test_recompute_empty_and_idempotent()
    test_ui_recompute_callback_contract()
    test_cleanup()
    print("\n=== ALL RECOMPUTE RELEVANCE TESTS PASSED ===")
