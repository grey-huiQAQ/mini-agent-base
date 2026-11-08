# -*- coding: utf-8 -*-
"""
NoteStore 目标笔记系统测试（离线：不依赖网络与 LLM，共享开发库精确清理）
覆盖：
- 数据库迁移：init_db 后 notes 表存在且列齐全，重复初始化幂等
- 新增：写库 + 写文件 + 回填 file_path；非法参数拒绝；同日同名不互相覆盖
- 查询：按目标获取与目标隔离、新笔记在前；关键词检索计分排序与目标范围限定
- 更新：正文更新同步重写文件；不存在返回 False
- 删除：库行 + 文件同时移除，重复删除安全返回 False
- 索引维护：index.md 随增删自动刷新；渲染工具函数（文件名清理 / 空索引）
"""
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_connection
from memory.note_store import (
    NoteStore,
    note_dir_for,
    build_note_filename,
    sanitize_title,
    render_index_md,
)

# 禁用 NoteIndexer，避免在 NoteStore 单元测试中加载 Embedding 模型 + Chroma
# （v3.7.6 新增 RAG 同步功能已在 tests/test_note_enhancements.py 中单独测试）
from unittest.mock import patch as _patch
_indexer_patcher = _patch("rag.note_indexer.NoteIndexer")
_indexer_patcher.start()

# 本测试使用的目标 ID 前缀（带随机后缀避免污染共享开发库中的真实目标数据）
_GOAL_PREFIX = "goal_notetest_" + uuid.uuid4().hex[:6]
# 本测试创建过的目标 ID（清理时连同其笔记目录一起删除）
_used_goal_ids = []
# 本测试创建过的笔记 ID（精确删除库行）
_created_note_ids = []


def setup():
    """初始化数据库（触发 notes 表创建）"""
    init_db()


def _new_goal_id() -> str:
    """生成一个本测试专属的目标 ID 并登记，供 cleanup 精确清理"""
    gid = f"{_GOAL_PREFIX}_{len(_used_goal_ids)}_{int(time.time())}"
    _used_goal_ids.append(gid)
    return gid


def _cleanup():
    """清理本测试产生的笔记行、目标专属目录（含文件与 index.md）"""
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


# ---------- 数据库迁移 ----------

def test_migration_notes_table():
    """init_db 后应存在 notes 表且列齐全；重复调用幂等不报错"""
    init_db()  # 幂等：重复初始化不应报错
    conn = get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
    conn.close()
    for col in ("id", "goal_id", "title", "content", "file_path", "created_at", "updated_at"):
        assert col in cols, f"notes 表缺少列 {col}，实际为 {cols}"
    print("[OK] migration notes table")


# ---------- 新增 ----------

def test_add_and_get_note():
    """新增笔记：返回自增 ID，库行字段齐全，Markdown 文件落盘且含标题与正文"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "Transformer 学习笔记", "第一段要点内容")
    assert nid and nid > 0, "add_note 应返回正整数 ID"
    _created_note_ids.append(nid)

    note = NoteStore.get_note(nid)
    assert note is not None
    assert note["title"] == "Transformer 学习笔记"
    assert "第一段要点内容" in note["content"]
    assert note["goal_id"] == gid

    # 文件落盘校验：路径在目标 notes 目录下，内容包含标题头与正文
    fp = Path(note["file_path"])
    assert fp.exists(), f"笔记文件应存在：{fp}"
    assert str(fp).replace("\\", "/").startswith("data/goals/")
    assert fp.parent == note_dir_for(gid)
    text = fp.read_text(encoding="utf-8")
    assert "Transformer 学习笔记" in text and "第一段要点内容" in text
    print("[OK] add and get note")


def test_add_note_rejects_invalid_params():
    """空 goal_id 或空标题应被拒绝（返回 -1），且不产生任何数据"""
    before = NoteStore.count_notes()
    assert NoteStore.add_note("", "无目标笔记", "x") == -1
    assert NoteStore.add_note(_new_goal_id(), "", "x") == -1
    assert NoteStore.add_note(_new_goal_id(), "   ", "") == -1
    assert NoteStore.count_notes() == before, "非法参数不应产生新笔记"
    print("[OK] add note rejects invalid params")


def test_same_day_same_title_no_overwrite():
    """同日同名笔记自动加时间后缀：两篇都保留，互不覆盖"""
    gid = _new_goal_id()
    nid1 = NoteStore.add_note(gid, "重复标题", "版本一")
    nid2 = NoteStore.add_note(gid, "重复标题", "版本二")
    _created_note_ids.extend([nid1, nid2])
    assert nid1 > 0 and nid2 > 0 and nid1 != nid2

    notes = NoteStore.get_notes_by_goal(gid)
    assert len(notes) == 2
    paths = {n["file_path"] for n in notes}
    assert len(paths) == 2, "两篇同名笔记应写入不同文件"
    for n in notes:
        assert Path(n["file_path"]).exists()
    bodies = {Path(n["file_path"]).read_text(encoding="utf-8") for n in notes}
    assert any("版本一" in b for b in bodies) and any("版本二" in b for b in bodies), \
        "每个文件应保存各自的正文"
    print("[OK] same day same title no overwrite")


# ---------- 查询 ----------

def test_get_notes_by_goal_isolation():
    """按目标获取：只返回该目标的笔记，新的在前；目标之间互相隔离"""
    ga, gb = _new_goal_id(), _new_goal_id()
    na1 = NoteStore.add_note(ga, "甲目标旧笔记", "A1")
    nb1 = NoteStore.add_note(gb, "乙目标笔记", "B1")
    na2 = NoteStore.add_note(ga, "甲目标新笔记", "A2")
    _created_note_ids.extend([na1, na2, nb1])

    notes_a = NoteStore.get_notes_by_goal(ga)
    assert [n["id"] for n in notes_a] == [na2, na1], "目标 A 应只有自己的两条且新在前"
    notes_b = NoteStore.get_notes_by_goal(gb)
    assert [n["id"] for n in notes_b] == [nb1], "目标 B 不应看到目标 A 的笔记"

    # 数量统计与全库口径
    assert NoteStore.count_notes(ga) == 2
    assert NoteStore.count_notes() >= 3
    print("[OK] get notes by goal isolation")


def test_search_notes_scoring_and_scope():
    """检索：命中标题或正文才返回；命中多者排前；可限定目标范围；空查询返回 []"""
    ga, gb = _new_goal_id(), _new_goal_id()
    n_common = NoteStore.add_note(ga, "机器学习基础", "神经网络 反向传播 入门")
    n_rich = NoteStore.add_note(ga, "进阶资料整理", "神经网络 神经网络 神经网络 深度解析")
    n_other = NoteStore.add_note(gb, "英语语法", "虚拟语气总结")
    n_miss = NoteStore.add_note(ga, "跑步计划", "每天五公里")
    _created_note_ids.extend([n_common, n_rich, n_other, n_miss])

    # 全库检索："神经网络" 在 n_rich 中出现更多次 → 排前；未命中的不返回
    hits = NoteStore.search_notes("神经网络")
    ids = [n["id"] for n in hits]
    assert ids and ids[0] == n_rich, f"命中次数多的笔记应排前：{ids}"
    assert n_miss not in ids, "未命中的笔记不应返回"

    # 范围限定：只在目标 gb 内检索时看不到 ga 的笔记
    hits_b = NoteStore.search_notes("神经网络", goal_id=gb)
    assert hits_b == []
    hits_b2 = NoteStore.search_notes("虚拟语气", goal_id=gb)
    assert [n["id"] for n in hits_b2] == [n_other]

    # 边界：空查询 / 纯符号查询返回 []
    assert NoteStore.search_notes("") == []
    assert NoteStore.search_notes("   ") == []
    print("[OK] search notes scoring and scope")


# ---------- 更新 ----------

def test_update_note_rewrites_file():
    """更新正文后文件同步重写；更新不存在的笔记返回 False"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "待更新笔记", "旧内容")
    _created_note_ids.append(nid)
    old_path = Path(NoteStore.get_note(nid)["file_path"])

    assert NoteStore.update_note(nid, content="全新的内容") is True
    note = NoteStore.get_note(nid)
    assert "全新的内容" in note["content"]
    assert old_path.exists() and "全新的内容" in old_path.read_text(encoding="utf-8"), \
        "原文件应被同步重写"
    # 标题不变时文件路径保持稳定（外部链接不失效）
    assert Path(note["file_path"]) == old_path

    assert NoteStore.update_note(99999999, content="x") is False
    print("[OK] update note rewrites file")


# ---------- 删除 ----------

def test_delete_note_removes_row_and_file():
    """删除：库行与文件一并移除；再次删除返回 False"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "将被删除的笔记", "临时内容")
    file_path = Path(NoteStore.get_note(nid)["file_path"])
    assert file_path.exists()

    assert NoteStore.delete_note(nid) is True
    assert NoteStore.get_note(nid) is None
    assert not file_path.exists(), "删除后笔记文件应一并移除"
    assert NoteStore.delete_note(nid) is False, "重复删除应安全返回 False"
    print("[OK] delete note removes row and file")


# ---------- index.md 维护与渲染工具 ----------

def test_index_md_maintained_on_add_and_delete():
    """index.md 随新增/删除自动刷新：列出文件名与摘要，删除后条目消失"""
    gid = _new_goal_id()
    nid = NoteStore.add_note(gid, "索引验证笔记", "这里是索引摘要内容")

    index_path = note_dir_for(gid) / "index.md"
    assert index_path.exists(), "新增笔记后应自动生成 index.md"
    text = index_path.read_text(encoding="utf-8")
    fname = Path(NoteStore.get_note(nid)["file_path"]).name
    assert fname in text and "索引验证笔记" in text, "index.md 应包含文件名与标题"
    assert "共 1 篇" in text

    NoteStore.delete_note(nid)
    text_after = index_path.read_text(encoding="utf-8")
    assert fname not in text_after, "删除后 index.md 不再列出该文件"
    assert "共 0 篇" in text_after
    print("[OK] index md maintained on add and delete")


def test_render_helpers():
    """渲染工具：文件名清理非法字符、默认文件名格式、空索引渲染"""
    # 首尾的替换符会被 strip("-.") 去掉，避免文件名以 - 结尾
    assert sanitize_title('笔记:标题/测试*?') == "笔记-标题-测试", "非法字符应替换为连字符"
    assert sanitize_title("   ") == "untitled", "空标题回退 untitled"

    name = build_note_filename("我的笔记", datetime(2026, 1, 15))
    assert name == "20260115_我的笔记.md", f"文件名格式应为 YYYYMMDD_标题.md：{name}"

    empty_index = render_index_md("goal_x", [])
    assert "共 0 篇" in empty_index and "暂无笔记" in empty_index

    sample = render_index_md("goal_x", [{"id": 1, "title": "T",
                                         "content": "第一行内容",
                                         "file_path": "data/goals/goal_x/notes/a.md",
                                         "created_at": "2026-01-01 10:00:00"}])
    assert "a.md" in sample and "T" in sample and "第一行内容" in sample
    print("[OK] render helpers")


def test_cleanup():
    """清理测试数据（库行 + 目标目录）"""
    _cleanup()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== note store test ===\n")
    setup()
    test_migration_notes_table()
    test_add_and_get_note()
    test_add_note_rejects_invalid_params()
    test_same_day_same_title_no_overwrite()
    test_get_notes_by_goal_isolation()
    test_search_notes_scoring_and_scope()
    test_update_note_rewrites_file()
    test_delete_note_removes_row_and_file()
    test_index_md_maintained_on_add_and_delete()
    test_render_helpers()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
