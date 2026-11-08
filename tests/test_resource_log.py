# -*- coding: utf-8 -*-
"""
Agent 资源消耗监控（memory/resource_log.py）测试（离线：临时数据库）
覆盖：
- init_db 后 agent_resource_log / test_runs 表存在（CREATE TABLE IF NOT EXISTS 幂等）
- log_resource_usage 写入一行（类型/操作/token 估算/时间戳）
- 异常参数容错：None / 负数 / 超长字符串不抛异常
- estimate_tokens / estimate_messages_tokens 粗粒度估算
- get_resource_stats 按类型汇总 + 每日明细 + 总 token
- get_recent_resource_logs 最新在前
- clear_resource_logs 只删超期旧日志
- model_client 埋点：chat_sync 调用后自动写入资源日志（mock OpenAI 客户端，
  临时库验证落库；usage 精确值优先 / 无 usage 时字符粗估）
- 埋点失败不影响主流程（log_resource_usage 抛异常时 chat_sync 仍正常返回）
- 搜索 Provider 埋点：DuckDuckGo 搜索后自动写入 search 资源日志
"""
import contextlib
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import resource_log
from memory.database import init_db


# ---------- 工具：工作区内临时目录（兼容受限文件沙箱 / Windows 文件锁） ----------

@contextlib.contextmanager
def _tmp_dir():
    """创建临时目录（放在项目 data/.tmp_tests 下，自定义命名 + 手动清理）

    说明：
    - 不用 tempfile 临时目录机制，避免受限文件沙箱对系统临时区的写保护；
    - 目录名用 run_<uuid> 自定义命名，避免与沙箱识别的 tmp* 临时目录混淆；
    - 残留目录由 test_cleanup 兜底删除。
    """
    base = (Path(__file__).resolve().parent.parent / "data" / ".tmp_tests")
    base.mkdir(parents=True, exist_ok=True)
    d = base / ("run_" + uuid.uuid4().hex[:10])
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield str(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- 测试用例 ----------

def test_init_db_creates_resource_tables():
    """init_db 后新表存在（临时库验证，不污染开发库）"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        with patch("memory.database.DB_PATH", db):
            init_db()
        conn = sqlite3.connect(db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "agent_resource_log" in tables, "应创建 agent_resource_log 表"
            assert "test_runs" in tables, "应创建 test_runs 表"
            # 幂等：重复初始化不报错
            with patch("memory.database.DB_PATH", db):
                init_db()
        finally:
            conn.close()
    print("[OK] init_db creates agent_resource_log + test_runs")


def test_log_resource_usage_writes_row():
    """log_resource_usage 写入一行，字段正确（类型/操作/token/时间戳）"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resource_log.log_resource_usage("llm", 1234, "chat_sync",
                                        "模型 Qwen", db_path=db)
        resource_log.log_resource_usage("search", 50, "ddgs",
                                        "查询：AI", db_path=db)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT * FROM agent_resource_log ORDER BY id").fetchall()
            assert len(rows) == 2, f"应写入 2 行：{rows}"
            # 列序：id, resource_type, operation, token_estimate, detail, created_at
            assert rows[0][1] == "llm" and rows[0][2] == "chat_sync"
            assert rows[0][3] == 1234
            assert rows[1][1] == "search" and rows[1][2] == "ddgs"
            assert rows[1][3] == 50
            assert rows[0][5] is not None, "created_at 应有默认时间戳"
        finally:
            conn.close()
    print("[OK] log_resource_usage writes row")


def test_log_resource_usage_tolerates_bad_args():
    """异常参数容错：None / 负数 / 超长字符串不抛异常且不污染字段长度"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resource_log.log_resource_usage(None, None, None, None, db_path=db)
        resource_log.log_resource_usage("llm", -5, "x" * 200, "y" * 500,
                                        db_path=db)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT resource_type, operation, token_estimate, detail "
                "FROM agent_resource_log ORDER BY id").fetchall()
            assert len(rows) == 2
            assert rows[0][0] == "unknown" and rows[0][2] == 0
            assert rows[1][2] == 0, "负数 token 应按 0 处理"
            assert len(rows[1][1]) <= 64 and len(rows[1][3]) <= 300, "应截断超长字段"
        finally:
            conn.close()
    print("[OK] log_resource_usage tolerant of bad args")


def test_estimates():
    """token 粗粒度估算：字符数 ÷ 4（中英文混合经验值）"""
    assert resource_log.estimate_tokens("abcd") == 1
    assert resource_log.estimate_tokens("") == 0
    assert resource_log.estimate_tokens(None) == 0
    assert resource_log.estimate_tokens(12345) == 1, "非字符串按 str 估算"
    toks = resource_log.estimate_messages_tokens([
        {"role": "user", "content": "abcd"},
        {"role": "assistant", "content": "efgh"},
    ])
    assert toks >= 2, f"应包含 role 与 content 的估算：{toks}"
    assert resource_log.estimate_messages_tokens(None) == 0
    assert resource_log.estimate_messages_tokens(
        [{"role": "user", "content": [{"text": "abcd"}]}]) >= 1
    print("[OK] token estimates")


def test_stats_aggregation():
    """统计：按类型汇总 + 每日明细 + 总 token + 最近记录最新在前"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resource_log.log_resource_usage("llm", 1000, "chat_sync", "", db_path=db)
        resource_log.log_resource_usage("llm", 500, "stream_chat", "", db_path=db)
        resource_log.log_resource_usage("search", 30, "ddgs", "", db_path=db)
        stats = resource_log.get_resource_stats(days=7, db_path=db)
        by_type = {i["type"]: i for i in stats["by_type"]}
        assert by_type["llm"]["calls"] == 2, by_type
        assert by_type["llm"]["tokens"] == 1500, by_type
        assert by_type["search"]["calls"] == 1 and by_type["search"]["tokens"] == 30
        assert stats["total_tokens"] == 1530, stats
        assert len(stats["daily"]) >= 2, "应有按日明细"
        recent = resource_log.get_recent_resource_logs(limit=10, db_path=db)
        assert len(recent) == 3
        assert recent[0]["operation"] == "ddgs", "最近一条应最新在前"
    print("[OK] stats aggregation by type + daily")


def test_clear_resource_logs():
    """清理旧日志：仅删除超期行，保留新行"""
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resource_log.log_resource_usage("llm", 10, "chat_sync", "", db_path=db)
        # 手工写入一条 40 天前的旧记录
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO agent_resource_log (resource_type, operation, "
                "token_estimate, created_at) VALUES ('search', 'ddgs', 5, "
                "datetime('now', '-40 days'))")
            conn.commit()
        finally:
            conn.close()
        deleted = resource_log.clear_resource_logs(before_days=30, db_path=db)
        assert deleted == 1, f"应删除 1 条旧记录：{deleted}"
        recent = resource_log.get_recent_resource_logs(limit=10, db_path=db)
        assert len(recent) == 1 and recent[0]["resource_type"] == "llm"
    print("[OK] clear old logs only")


# ---------- 埋点集成测试（mock OpenAI 客户端，临时库验证落库） ----------

def test_chat_sync_instrumented_with_usage():
    """chat_sync 埋点：响应带 usage 时以精确 token 数写入（10+20=30）"""
    from business import model_client as mc
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: resp)))
        with patch.object(mc, "get_client", return_value=fake_client), \
             patch("memory.database.DB_PATH", db):
            text = mc.chat_sync([{"role": "user", "content": "你好"}],
                                max_tokens=10)
        assert text == "你好", "chat_sync 返回值不应受埋点影响"
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT resource_type, operation, token_estimate FROM "
                "agent_resource_log").fetchall()
            assert len(rows) == 1, f"应写入 1 条 LLM 日志：{rows}"
            assert rows[0][0] == "llm" and rows[0][1] == "chat_sync"
            assert rows[0][2] == 30, f"应使用 usage 精确值：{rows[0][2]}"
        finally:
            conn.close()
    print("[OK] chat_sync instrumentation with usage")


def test_chat_sync_instrumented_fallback_estimate():
    """chat_sync 埋点：无 usage 信息时按字符粗估（输入+输出均计入）"""
    from business import model_client as mc
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="你好世界"))],
            usage=None,
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: resp)))
        with patch.object(mc, "get_client", return_value=fake_client), \
             patch("memory.database.DB_PATH", db):
            mc.chat_sync([{"role": "user", "content": "你好世界"}],
                         max_tokens=10)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT token_estimate FROM agent_resource_log").fetchall()
            assert len(rows) == 1
            assert rows[0][0] >= 2, f"应包含输入+输出粗估：{rows[0][0]}"
        finally:
            conn.close()
    print("[OK] chat_sync instrumentation fallback estimate")


def test_chat_sync_failure_still_logs_input():
    """chat_sync 失败时也记录输入 token（消耗已发生），且仍抛异常"""
    from business import model_client as mc
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")

        def _boom(**kw):
            raise RuntimeError("API 错误")

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_boom)))
        with patch.object(mc, "get_client", return_value=fake_client), \
             patch("memory.database.DB_PATH", db):
            try:
                mc.chat_sync([{"role": "user", "content": "你好"}], max_tokens=5)
                raise AssertionError("应抛出异常")
            except Exception as e:
                assert "同步请求失败" in str(e)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT token_estimate FROM agent_resource_log").fetchall()
            assert len(rows) == 1 and rows[0][0] >= 1, "失败也应记录输入 token"
        finally:
            conn.close()
    print("[OK] chat_sync failure logs input + re-raises")


def test_instrumentation_failure_does_not_break_chat_sync():
    """埋点自身失败（写库抛异常）不应影响主流程"""
    from business import model_client as mc
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=None,
    )
    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: resp)))
    with patch.object(mc, "get_client", return_value=fake_client), \
         patch("memory.resource_log.log_resource_usage",
               side_effect=RuntimeError("写入失败")):
        text = mc.chat_sync([{"role": "user", "content": "hi"}], max_tokens=5)
    assert text == "ok", "埋点失败不应影响主流程"
    print("[OK] instrumentation failure tolerated")


def test_search_provider_instrumented():
    """DuckDuckGo 搜索成功（mock ddgs 库）后自动写入 search 资源日志"""
    from core.information_engine.search_providers import ddgs_provider
    with _tmp_dir() as tmp:
        db = str(Path(tmp) / "m.db")

        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query, max_results=5):
                return [{"title": "t", "href": "https://x", "body": "b"}]

        # 注入假 ddgs 模块（无论环境是否安装 ddgs 均可离线测试）
        import types as _types
        fake_ddgs_mod = _types.ModuleType("ddgs")
        fake_ddgs_mod.DDGS = FakeDDGS
        provider = ddgs_provider.DuckDuckGoProvider()
        with patch.dict(sys.modules, {"ddgs": fake_ddgs_mod}), \
             patch("memory.database.DB_PATH", db):
            evs = provider.search("测试查询", limit=3)
        assert len(evs) == 1, "搜索应正常返回证据"
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT resource_type, operation, token_estimate FROM "
                "agent_resource_log").fetchall()
            assert len(rows) == 1, f"应写入 1 条 search 日志：{rows}"
            assert rows[0][0] == "search" and rows[0][1] == "ddgs"
            assert rows[0][2] >= 1, "应按查询字符粗估 token"
        finally:
            conn.close()
    print("[OK] search provider instrumentation")


def test_cleanup():
    """清理临时目录（子进程被 kill 后可能残留，兜底删除）"""
    base = Path(__file__).resolve().parent.parent / "data" / ".tmp_tests"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== resource log test ===\n")
    test_init_db_creates_resource_tables()
    test_log_resource_usage_writes_row()
    test_log_resource_usage_tolerates_bad_args()
    test_estimates()
    test_stats_aggregation()
    test_clear_resource_logs()
    test_chat_sync_instrumented_with_usage()
    test_chat_sync_instrumented_fallback_estimate()
    test_chat_sync_failure_still_logs_input()
    test_instrumentation_failure_does_not_break_chat_sync()
    test_search_provider_instrumented()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
