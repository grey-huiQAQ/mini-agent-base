# -*- coding: utf-8 -*-
"""
笔记增强功能测试（v3.7.6）

覆盖：
- 数据库迁移：tags 列存在且可空
- 标签增删改：add_note / update_note 支持 tags（字符串与列表两种输入）
- 标签查询：search_notes_by_tag / search_notes(tag=) / get_tags
- NoteIndexer：index_note / remove_note_from_index（mock 向量库 + embedding）
- NoteStore RAG 同步：add_note 触发索引 / delete_note 触发删除 / update_note 触发重索引
- 语义搜索降级：空向量库 / mock embedding → 安全返回 []
- 快速保存回调：do_save_note_from_chat（mock NoteStore / _notes_panel_view）

所有测试离线运行，无依赖网络与 LLM：
- 涉及 NoteStore.add_note/update_note/delete_note 的用例一律 mock 掉
  rag.note_indexer.NoteIndexer（避免加载 bge-m3 嵌入模型与真实 Chroma 库），
  并显式传入 async_rag=False（同步执行 RAG 调用），杜绝后台线程竞态，
  保证 RAG 调用断言确定可复现。
"""
import os
import sys
import shutil
import types
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection
from memory.note_store import (
    NoteStore,
    _normalize_tags,
    parse_tags,
    note_dir_for,
)

# pytest 可选依赖
try:
    import pytest
except ImportError:
    pytest = None


# ---------- 测试数据管理 ----------

# 本测试使用的目标 ID 前缀（避免污染共享开发库）
_GOAL_PREFIX = "goal_note_enh_" + uuid.uuid4().hex[:6]
_used_goal_ids = []
_created_note_ids = []


def _new_goal_id() -> str:
    """生成本测试专属的目标 ID 并登记"""
    import time as _time
    gid = f"{_GOAL_PREFIX}_{len(_used_goal_ids)}_{int(_time.time())}"
    _used_goal_ids.append(gid)
    return gid


def _cleanup():
    """清理本测试产生的笔记行、目标专属目录"""
    conn = get_connection()
    try:
        for gid in _used_goal_ids:
            conn.execute("DELETE FROM notes WHERE goal_id = ?", (gid,))
        for nid in _created_note_ids:
            conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
        conn.commit()
    finally:
        conn.close()
    for gid in _used_goal_ids:
        goal_root = Path("data") / "goals" / gid
        if goal_root.exists():
            shutil.rmtree(goal_root, ignore_errors=True)
    _used_goal_ids.clear()
    _created_note_ids.clear()


def setup():
    """初始化数据库（触发 notes 表创建 + tags 列迁移）"""
    init_db()
    _cleanup()


def teardown():
    """测试后清理"""
    _cleanup()


# ---------- 1. 数据库迁移 ----------

def test_tags_column_exists():
    """init_db 后 notes 表应包含 tags 列"""
    init_db()
    conn = get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
    conn.close()
    assert "tags" in cols, f"notes 表缺少 tags 列，实际为 {cols}"
    print("[OK] tags column exists")


def test_idempotent_init():
    """重复调用 init_db 不应报错（迁移幂等）"""
    init_db()
    init_db()
    print("[OK] idempotent init")


# ---------- 2. _normalize_tags / parse_tags 工具 ----------

def test_normalize_tags_str():
    """字符串输入：按逗号分割、去空、去重、去空格"""
    assert _normalize_tags("学习, 重要, 学习,  , 总结") == "学习,重要,总结"
    assert _normalize_tags("") == ""
    assert _normalize_tags("  ") == ""
    print("[OK] normalize tags str")


def test_normalize_tags_list():
    """列表输入：直接规范化"""
    assert _normalize_tags(["学习", "重要", "", "学习"]) == "学习,重要"
    assert _normalize_tags(["AI", "学习"]) == "AI,学习"
    print("[OK] normalize tags list")


def test_normalize_tags_none():
    """None 输入返回空字符串"""
    assert _normalize_tags(None) == ""
    print("[OK] normalize tags none")


def test_parse_tags():
    """parse_tags 反向解析：字符串 → 列表"""
    assert parse_tags("学习,重要,总结") == ["学习", "重要", "总结"]
    assert parse_tags("") == []
    assert parse_tags(None) == []
    assert parse_tags("  学习  , , 重要 ") == ["学习", "重要"]
    print("[OK] parse tags")


# ---------- 3. 标签增删改 ----------

@patch("rag.note_indexer.NoteIndexer")
def test_add_note_with_tags_str(mock_indexer_cls):
    """add_note 接受逗号分隔字符串标签"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "标签测试笔记", "内容", tags="学习,重要", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)
    note = NoteStore.get_note(nid)
    assert note["tags"] == "学习,重要", f"tags 应为 '学习,重要'，实际为 '{note['tags']}'"
    print("[OK] add note with tags str")


@patch("rag.note_indexer.NoteIndexer")
def test_add_note_with_tags_list(mock_indexer_cls):
    """add_note 接受列表形式标签"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "列表标签笔记", "内容", tags=["AI", "学习", ""], async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)
    note = NoteStore.get_note(nid)
    assert note["tags"] == "AI,学习", f"tags 应为 'AI,学习'，实际为 '{note['tags']}'"
    print("[OK] add note with tags list")


@patch("rag.note_indexer.NoteIndexer")
def test_add_note_without_tags(mock_indexer_cls):
    """add_note 不传 tags 时 tags 为空（向后兼容）"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "无标签笔记", "内容", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)
    note = NoteStore.get_note(nid)
    assert note["tags"] is None or note["tags"] == "", "未传 tags 时应为空"
    print("[OK] add note without tags")


@patch("rag.note_indexer.NoteIndexer")
def test_update_note_tags(mock_indexer_cls):
    """update_note 可以修改标签"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "待更新标签笔记", "旧内容", tags="旧标签", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)

    assert NoteStore.update_note(nid, tags="新标签,重要", async_rag=False) is True
    note = NoteStore.get_note(nid)
    assert note["tags"] == "新标签,重要", f"tags 应更新为 '新标签,重要'，实际为 '{note['tags']}'"
    print("[OK] update note tags")


@patch("rag.note_indexer.NoteIndexer")
def test_update_note_tags_none_keeps_existing(mock_indexer_cls):
    """update_note 不传 tags 时保留已有标签"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "保留标签笔记", "内容", tags="保留我", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)

    # 只更新内容，不传 tags → 标签应保留
    assert NoteStore.update_note(nid, content="新内容", async_rag=False) is True
    note = NoteStore.get_note(nid)
    assert note["content"] == "新内容"
    assert note["tags"] == "保留我", "未传 tags 时应保留原标签"
    print("[OK] update note tags none keeps existing")


# ---------- 4. 标签查询 ----------

@patch("rag.note_indexer.NoteIndexer")
def test_search_notes_by_tag(mock_indexer_cls):
    """search_notes_by_tag 按标签过滤，返回匹配笔记"""
    gid = _new_goal_id()
    n1 = NoteStore.add_note(gid, "笔记1", "内容1", tags="学习,AI", async_rag=False)
    n2 = NoteStore.add_note(gid, "笔记2", "内容2", tags="重要,总结", async_rag=False)
    n3 = NoteStore.add_note(gid, "笔记3", "内容3", tags="学习,重要", async_rag=False)
    _created_note_ids.extend([n1, n2, n3])

    # 搜索 "学习" → 匹配 n1, n3
    hits = NoteStore.search_notes_by_tag("学习", goal_id=gid)
    ids = [n["id"] for n in hits]
    assert n1 in ids and n3 in ids and n2 not in ids, f"标签过滤结果错误：{ids}"

    # 搜索 "重要" → 匹配 n2, n3
    hits2 = NoteStore.search_notes_by_tag("重要", goal_id=gid)
    ids2 = [n["id"] for n in hits2]
    assert n2 in ids2 and n3 in ids2 and n1 not in ids2, f"标签过滤结果错误：{ids2}"
    print("[OK] search notes by tag")


def test_search_notes_by_tag_empty():
    """search_notes_by_tag 空标签返回 []"""
    assert NoteStore.search_notes_by_tag("") == []
    assert NoteStore.search_notes_by_tag("   ") == []
    print("[OK] search notes by tag empty")


@patch("rag.note_indexer.NoteIndexer")
def test_search_notes_with_tag_param(mock_indexer_cls):
    """search_notes 支持 tag 参数联合关键词过滤"""
    gid = _new_goal_id()
    n1 = NoteStore.add_note(gid, "机器学习基础", "神经网络入门", tags="学习", async_rag=False)
    n2 = NoteStore.add_note(gid, "英语语法", "虚拟语气总结", tags="英语", async_rag=False)
    n3 = NoteStore.add_note(gid, "学习计划", "每天学习神经网络", tags="计划", async_rag=False)
    _created_note_ids.extend([n1, n2, n3])

    # 关键词 "神经网络" 但限制标签 "学习" → 仅 n1（n3 虽含关键词但标签不匹配）
    hits = NoteStore.search_notes("神经网络", goal_id=gid, tag="学习")
    ids = [n["id"] for n in hits]
    assert n1 in ids and n3 not in ids, f"联合过滤结果错误：{ids}"

    # 关键词 "学习" 但限制标签 "AI" → n1 无 AI 标签 → 空
    hits2 = NoteStore.search_notes("学习", goal_id=gid, tag="AI")
    assert hits2 == [], f"不应匹配到任何笔记：{hits2}"
    print("[OK] search notes with tag param")


@patch("rag.note_indexer.NoteIndexer")
def test_get_tags(mock_indexer_cls):
    """get_tags 返回目标下所有已使用标签（去重排序）"""
    gid = _new_goal_id()
    NoteStore.add_note(gid, "笔记1", "内容", tags="学习,AI", async_rag=False)
    NoteStore.add_note(gid, "笔记2", "内容", tags="学习,重要", async_rag=False)
    NoteStore.add_note(gid, "笔记3", "内容", tags="总结", async_rag=False)

    tags = NoteStore.get_tags(gid)
    assert "学习" in tags
    assert "AI" in tags
    assert "重要" in tags
    assert "总结" in tags
    assert len(tags) == 4  # 去重后
    print("[OK] get tags")


def test_get_tags_empty():
    """get_tags 在无笔记时返回空列表"""
    gid = _new_goal_id()
    assert NoteStore.get_tags(gid) == []
    print("[OK] get tags empty")


# ---------- 5. RAG 索引集成（Mock 向量库 + Embedding） ----------

class MockCollection:
    """模拟 Chroma Collection，用于测试 NoteIndexer"""

    def __init__(self):
        self.docs = {}  # id -> {"text": str, "metadata": dict}

    @property
    def count_val(self):
        return len(self.docs)

    def count(self):
        return len(self.docs)

    def upsert(self, documents=None, metadatas=None, ids=None, embeddings=None):
        for i, doc_id in enumerate(ids or []):
            self.docs[doc_id] = {
                "text": documents[i] if documents and i < len(documents) else "",
                "metadata": (metadatas or [{}] * max(len(ids or []), 1))[i]
                if (metadatas and i < len(metadatas)) else {},
            }

    def delete(self, ids=None, where=None, **kwargs):
        if where:
            note_id = where.get("note_id")
            to_remove = [k for k, v in self.docs.items()
                         if v["metadata"].get("note_id") == note_id]
            for k in to_remove:
                del self.docs[k]
        elif ids:
            for doc_id in ids or []:
                self.docs.pop(doc_id, None)

    def query(self, query_embeddings=None, n_results=None, include=None):
        """模拟 Chroma query：返回所有文档（简化）"""
        ids = list(self.docs.keys())[:n_results] if n_results else list(self.docs.keys())
        return {
            "ids": [ids],
            "documents": [[self.docs[i]["text"] for i in ids]],
            "metadatas": [[self.docs[i]["metadata"] for i in ids]],
            "distances": [[0.1 * (j + 1) for j in range(len(ids))]],
        }


def _patch_note_indexer_deps(mock_collection):
    """构建 NoteIndexer 依赖的 mock 对象：向量库 + embedding"""
    # mock embed_documents 返回固定向量
    embed_mock = MagicMock()
    embed_mock.encode = MagicMock(return_value=[[0.1] * 768, [0.2] * 768])
    return embed_mock


def test_note_indexer_index_note():
    """NoteIndexer.index_note 正常调用 add_documents（mock）"""
    mock_collection = MockCollection()

    with patch("rag.vector_store.get_collection", return_value=mock_collection), \
         patch("rag.embedding.embed_documents", return_value=[[0.1] * 768, [0.2] * 768]):

        from rag.note_indexer import NoteIndexer
        result = NoteIndexer.index_note(42, "测试标题", "第一段内容\n\n第二段内容")
        assert result is True
        assert mock_collection.count() > 0
        # 验证 metadata 中包含 note_id（只检查本次索引的文档）
        expected_id = "note_42_0"
        assert expected_id in mock_collection.docs, f"缺少预期文档 {expected_id}，实际 keys: {list(mock_collection.docs.keys())}"
        doc = mock_collection.docs[expected_id]
        assert doc["metadata"].get("note_id") == "42"
        assert doc["metadata"].get("title") == "测试标题"
        print("[OK] note indexer index note")


def test_note_indexer_remove_from_index():
    """NoteIndexer.remove_note_from_index 正常调用 delete_documents（mock）"""
    mock_collection = MockCollection()
    # 模拟已有数据
    mock_collection.docs = {
        "note_42_0": {"text": "text1", "metadata": {"note_id": "42", "title": "T1"}},
        "note_42_1": {"text": "text2", "metadata": {"note_id": "42", "title": "T1"}},
        "note_99_0": {"text": "text3", "metadata": {"note_id": "99", "title": "T2"}},
    }

    with patch("rag.vector_store.get_collection", return_value=mock_collection):
        from rag.note_indexer import NoteIndexer
        result = NoteIndexer.remove_note_from_index(42)
        assert result is True
        # 只有 note_42 的分块被删除，note_99 保留
        remaining_ids = list(mock_collection.docs.keys())
        assert len(remaining_ids) == 1
        assert "note_99_0" in remaining_ids
        print("[OK] note indexer remove from index")


def test_note_indexer_index_empty_content():
    """NoteIndexer.index_note 内容为空时返回 True（跳过）"""
    mock_collection = MockCollection()

    with patch("rag.vector_store.get_collection", return_value=mock_collection), \
         patch("rag.embedding.embed_documents") as mock_embed:
        from rag.note_indexer import NoteIndexer
        result = NoteIndexer.index_note(1, "", "")
        assert result is True
        assert mock_collection.count() == 0
        # embed_documents 不应被调用
        mock_embed.assert_not_called()
        print("[OK] note indexer index empty content")


def test_note_indexer_search_semantic():
    """NoteIndexer.search_notes_semantic 正常查询并返回笔记（mock）"""
    gid = _new_goal_id()
    # 仅 mock 掉 NoteStore 内部触发的 RAG 索引（同步），避免加载真实模型；
    # 本用例自身需要真实的 NoteIndexer 类，因此只在 add_note 期间临时 mock
    with patch("rag.note_indexer.NoteIndexer"):
        nid = NoteStore.add_note(gid, "机器学习笔记", "神经网络内容", tags="AI", async_rag=False)
    _created_note_ids.append(nid)

    mock_collection = MockCollection()
    # 模拟已有 1 个笔记块被索引
    mock_collection.docs = {
        "note_1_0": {"text": "神经网络内容",
                     "metadata": {"note_id": str(nid), "title": "机器学习笔记"}},
    }

    with patch("rag.vector_store.get_collection", return_value=mock_collection), \
         patch("rag.embedding.embed_query", return_value=[0.1] * 768):

        from rag.note_indexer import NoteIndexer
        notes = NoteIndexer.search_notes_semantic("神经网络", goal_id=gid, top_k=5)
        assert len(notes) == 1
        assert notes[0]["id"] == nid
        assert notes[0]["title"] == "机器学习笔记"
        print("[OK] note indexer search semantic")


def test_note_indexer_search_empty_collection():
    """NoteIndexer.search_notes_semantic 在空向量库时返回 []"""
    mock_collection = MockCollection()

    with patch("rag.vector_store.get_collection", return_value=mock_collection):
        from rag.note_indexer import NoteIndexer
        notes = NoteIndexer.search_notes_semantic("测试查询")
        assert notes == []
        print("[OK] note indexer search empty collection")


def test_note_indexer_index_failure_does_not_raise():
    """NoteIndexer.index_note 在嵌入失败时返回 False，不抛异常"""
    with patch("rag.vector_store.get_collection", return_value=MockCollection()), \
         patch("rag.embedding.embed_documents", side_effect=RuntimeError("模型未加载")):
        from rag.note_indexer import NoteIndexer
        result = NoteIndexer.index_note(999, "标题", "内容")
        assert result is False
        print("[OK] note indexer index failure safe")


# ---------- 6. NoteStore RAG 同步 ----------

@patch("rag.note_indexer.NoteIndexer")
def test_note_store_add_triggers_index(mock_indexer_cls):
    """add_note 成功后应触发 NoteIndexer.index_note"""
    gid = _new_goal_id()
    # async_rag=False：同步执行 RAG 调用，断言确定可复现（无后台线程竞态）
    nid = NoteStore.add_note(gid, "RAG同步测试", "内容", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)
    # NoteIndexer.index_note 应被调用
    mock_indexer_cls.index_note.assert_called_once()
    args = mock_indexer_cls.index_note.call_args[0]
    assert args[0] == nid  # note_id
    assert args[1] == "RAG同步测试"  # title
    assert args[2] == "内容"  # content
    print("[OK] note store add triggers index")


@patch("rag.note_indexer.NoteIndexer")
def test_note_store_update_triggers_reindex(mock_indexer_cls):
    """update_note 成功后应触发 remove_note_from_index + index_note"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "待更新笔记", "旧内容", async_rag=False)
    _created_note_ids.append(nid)

    # reset mock（add_note 已经调用过一次）
    mock_indexer_cls.reset_mock()

    assert NoteStore.update_note(nid, content="新内容", tags="新标签", async_rag=False) is True
    mock_indexer_cls.remove_note_from_index.assert_called_once_with(nid)
    mock_indexer_cls.index_note.assert_called_once()
    print("[OK] note store update triggers reindex")


@patch("rag.note_indexer.NoteIndexer")
def test_note_store_delete_triggers_remove(mock_indexer_cls):
    """delete_note 成功后应触发 NoteIndexer.remove_note_from_index"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "待删除笔记", "内容", async_rag=False)
    _created_note_ids.append(nid)

    mock_indexer_cls.reset_mock()
    assert NoteStore.delete_note(nid, async_rag=False) is True
    mock_indexer_cls.remove_note_from_index.assert_called_once_with(nid)
    print("[OK] note store delete triggers remove")


@patch("rag.note_indexer.NoteIndexer")
def test_note_store_rag_failure_does_not_block_save(mock_indexer_cls):
    """RAG 索引失败不应影响笔记保存（降级为仅数据库保存）"""
    # 模拟 index_note 抛出异常
    mock_indexer_cls.index_note.side_effect = RuntimeError("向量库错误")
    mock_indexer_cls.remove_note_from_index.return_value = True

    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "降级测试笔记", "内容", tags="测试", async_rag=False)
    # 笔记仍应成功保存到数据库
    assert nid > 0
    _created_note_ids.append(nid)
    note = NoteStore.get_note(nid)
    assert note is not None
    assert note["title"] == "降级测试笔记"
    assert note["tags"] == "测试"
    print("[OK] note store rag failure does not block save")


# ---------- 7. 搜索模式与降级 ----------

def test_semantic_search_fallback():
    """语义搜索在向量库不可用时不抛出异常（返回空列表）"""
    with patch("rag.vector_store.get_collection", side_effect=RuntimeError("Chroma 未初始化")):
        from rag.note_indexer import NoteIndexer
        result = NoteIndexer.search_notes_semantic("测试", goal_id="goal_x")
        assert result == []
        print("[OK] semantic search fallback")


def test_notes_panel_semantic_fallback():
    """笔记面板：语义搜索空结果 → 自动回退关键词检索并展示降级提示

    验证 app._notes_panel_view 在 search_mode="semantic" 且语义返回空时，
    自动调用 NoteStore.search_notes（关键词），并在列表头部给出
    "语义搜索暂不可用…已自动切换为关键词检索" 的提示，下拉框与列表一致。
    """
    from app import _notes_panel_view  # noqa: E402（NoteStore 直接复用顶部从 memory.note_store 导入的类）
    gid = _new_goal_id()
    # 建一篇真实笔记（同步写库；RAG 索引 mock 掉，保持离线）
    with patch("rag.note_indexer.NoteIndexer"):
        nid = NoteStore.add_note(gid, "语义降级测试笔记", "这是关键词能命中的正文", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)

    # 语义搜索返回空列表 → 触发关键词回退
    with patch("rag.note_indexer.NoteIndexer.search_notes_semantic", return_value=[]):
        md, dd = _notes_panel_view(gid, query="关键词", search_mode="semantic")
    assert "语义搜索暂不可用" in md, f"应包含降级提示，实际：{md}"
    assert "已自动切换为关键词检索" in md, f"降级提示应说明已切换关键词模式：{md}"
    assert "语义降级测试笔记" in md, f"回退后应展示匹配的笔记：{md}"
    # 下拉框选项与列表一致（包含刚创建的笔记 ID）
    # gradio 6 的 gr.update 返回 dict，用 .get 读取 choices
    choices = list((dd.get("choices") or []))
    assert choices, f"下拉框不应为空：{dd}"
    assert any(c[1] == nid for c in choices), f"下拉框应包含笔记 #{nid}：{choices}"
    print("[OK] notes panel semantic fallback")


# ---------- 8. UI 快速保存回调测试 ----------

def test_do_save_note_from_chat_without_goal():
    """do_save_note_from_chat 在未选择目标时返回提示（不崩溃）"""
    # 导入 app.py 中的函数（app.py 会执行 Heavy 初始化，但在测试环境下应可导入）
    # 使用 mock 避免实际依赖
    with patch("memory.note_store.NoteStore.add_note", return_value=-1):
        from app import do_save_note_from_chat
        msg, panel_md, panel_dd, acc_vis, title, tags, content = do_save_note_from_chat(
            None, "标题", "标签", "内容"
        )
        assert "⚠️" in msg or "请先" in msg
        print("[OK] do save note without goal")


def test_do_save_note_from_chat_success():
    """do_save_note_from_chat 正常保存后刷新面板并隐藏 Accordion"""
    gid = _new_goal_id()

    with patch("memory.note_store.NoteStore.add_note", return_value=1), \
         patch("memory.note_store.NoteStore.get_note", return_value={"id": 1, "title": "测试笔记", "tags": "标签"}), \
         patch("ui.controllers.notes_controller._notes_panel_view", return_value=("笔记列表", {})):

        from app import do_save_note_from_chat
        msg, panel_md, panel_dd, acc_vis, title, tags, content = do_save_note_from_chat(
            gid, "测试笔记", "学习,重要", "笔记内容"
        )
        assert "✅" in msg
        assert "笔记列表" in panel_md  # 面板被刷新
        assert title == ""  # 表单被清空
        assert tags == ""
        assert content == ""
        print("[OK] do save note from chat success")


def test_do_save_note_from_chat_empty_title():
    """do_save_note_from_chat 标题为空时返回提示"""
    gid = _new_goal_id()
    from app import do_save_note_from_chat
    msg, *_ = do_save_note_from_chat(gid, "", "标签", "内容")
    assert "⚠️" in msg
    print("[OK] do save note empty title")


# ---------- 9. 兼容性回归 ----------

@patch("rag.note_indexer.NoteIndexer")
def test_add_note_backward_compatible(mock_indexer_cls):
    """task_executor.py 以位置参数调用 add_note 不受影响"""
    gid = _new_goal_id()
    # 模拟 task_executor 的位置参数调用：add_note(goal_id, title, content)；
    # 显式 async_rag=False 让 RAG 调用同步执行，配合 mock 保证离线快速
    nid = NoteStore.add_note(gid, "兼容性测试", "内容", async_rag=False)
    assert nid > 0
    _created_note_ids.append(nid)
    note = NoteStore.get_note(nid)
    assert note["tags"] is None or note["tags"] == ""
    print("[OK] add note backward compatible")


# ---------- 主入口 ----------

if __name__ == "__main__":
    print("=== note enhancements test ===\n")
    setup()

    test_tags_column_exists()
    test_idempotent_init()

    test_normalize_tags_str()
    test_normalize_tags_list()
    test_normalize_tags_none()
    test_parse_tags()

    test_add_note_with_tags_str()
    test_add_note_with_tags_list()
    test_add_note_without_tags()
    test_update_note_tags()
    test_update_note_tags_none_keeps_existing()

    test_search_notes_by_tag()
    test_search_notes_by_tag_empty()
    test_search_notes_with_tag_param()
    test_get_tags()
    test_get_tags_empty()

    test_note_indexer_index_note()
    test_note_indexer_remove_from_index()
    test_note_indexer_index_empty_content()
    test_note_indexer_search_semantic()
    test_note_indexer_search_empty_collection()
    test_note_indexer_index_failure_does_not_raise()

    test_note_store_add_triggers_index()
    test_note_store_update_triggers_reindex()
    test_note_store_delete_triggers_remove()
    test_note_store_rag_failure_does_not_block_save()

    test_semantic_search_fallback()
    test_notes_panel_semantic_fallback()

    test_do_save_note_from_chat_without_goal()
    test_do_save_note_from_chat_success()
    test_do_save_note_from_chat_empty_title()

    test_add_note_backward_compatible()

    teardown()
    print("\n=== ALL ENHANCEMENT TESTS PASSED ===")
