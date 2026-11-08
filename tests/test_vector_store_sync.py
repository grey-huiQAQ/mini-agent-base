# -*- coding: utf-8 -*-
"""向量库存对账（P7 批次 6-①）：文档行与笔记行同库共存下的安全删除

覆盖：
- `rag.vector_store.source_counts()`：只统计带 source 的文档行，笔记行不计入；
- `rag.vector_store.note_id_counts()`：对称的另一半，只统计带 note_id 的笔记行；
- `index_documents.prune_orphan_sources()`：只删 source 已不在书架上的行，在架的
  source 一字不动，且删前打印清单、删后打印总数（不静默）；
- `rag.note_indexer.live_note_ids()` / `prune_orphan_notes()`（P7 批次 7-⑤）：笔记侧的
  对账——只删 note_id 已不在 notes 表里的行；**读不到库返回 None 而不是空集**（空集＝
  确实没有笔记，才该全删），库不存在与读库抛异常两条跳过路径都一条不删；
- `index_documents.main()`：同一 source 从 5 块重索引为 3 块后恰好 3 条（先删后加的
  牙齿——旧实现上会剩 5 条）；书架清空后对账仍发生（旧实现上提前 return、向量留着）；
  书架目录不存在时既不索引也不对账（安全闸）；空 notes 表时笔记向量全删。

隔离（本文件不碰生产 data/chroma、也不碰 memory.db）：
- `rag/vector_store.py` 的 `_client` / `_collection` 是模块级缓存，`get_collection()`
  只判 `_client is None`；用「字符串靶子」patch 把它们换成**内存** Chroma 实例即可全程
  走内存（`checks/check_test_db_isolation.py` 认可的同族写法）；
- 用 `chromadb.EphemeralClient`（纯内存）而不是「临时目录上的 PersistentClient」：
  后者在本机实测会因 sqlite 连接在同一进程内未释放而删不掉目录（rmtree 必失败，只有
  进程退出后才删得掉），给 data/.tmp_tests 留下一堆几百 KB 的残留；内存客户端零落盘；
- 本文件不碰 memory.db，故刻意**不**调 `setup_test_db()`；
- 嵌入向量一律传假数据（全 0.1），不加载 bge-m3。
"""
import contextlib
import io
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 假向量维度（本文件所有用例统一，保证同一个临时集合内的维度一致）
_FAKE_DIM = 8


@contextlib.contextmanager
def _isolated_store(shelf_files=None):
    """把向量库换成内存集合；给了 shelf_files 时另建一个临时书架目录

    Yields:
        (shelf_dir, collection)：shelf_dir 在没有给 shelf_files 时是 None
    """
    import chromadb
    from chromadb.config import Settings

    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    # 每个用例用一个**唯一集合名**：EphemeralClient 的「内存系统」是按 settings 全局缓存的，
    # 两次 EphemeralClient() 会共用同一个系统，同名集合会把上一个用例的行带过来
    # （实测：不换名时后一个用例看得见前一个用例塞的 a.txt / b.md）。
    # 代码侧不受影响——本文件把 rag.vector_store._collection 直接换成这个对象，
    # get_collection() 里的 get_or_create(name="personal_knowledge") 不会被走到。
    col = client.get_or_create_collection(
        name=f"personal_knowledge_{uuid.uuid4().hex[:8]}",
        metadata={"hnsw:space": "cosine"})

    shelf = None
    if shelf_files is not None:
        shelf = Path("data") / ".tmp_tests" / f"shelf_{uuid.uuid4().hex[:8]}"
        shelf.mkdir(parents=True, exist_ok=True)
        for name in shelf_files:
            (shelf / name).write_text("临时书架文件", encoding="utf-8")

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("rag.vector_store._client", client))
            stack.enter_context(patch("rag.vector_store._collection", col))
            if shelf is not None:
                # 只在本用例真的要走 main()/prune 的路径时替换书架根：
                # 顺带把 index_documents 的 files_dir 指向临时目录（.tmp_tests 已 gitignore）
                stack.enter_context(patch("index_documents.files_dir", shelf))
            yield shelf, col
    finally:
        if shelf is not None:
            shutil.rmtree(shelf, ignore_errors=True)


@contextlib.contextmanager
def _isolated_notes_db(live_ids=(), kind="table"):
    """把 memory.database.DB_PATH 指到临时库并建好 notes 表；yield 该库路径

    kind 控制库的状态（P7 批次 7-⑤ 的三条跳过路径各要一种）：
      - "table"  ：建 notes 表并插入 live_ids（默认空表）
      - "missing"：指向一个**不存在**的路径（安全闸那一支）
      - "garbage"：写一个非 sqlite 内容的文件（读库那一步抛异常那一支）

    为什么必须隔离：`index_documents.main()` 自本批起会读 notes 表，不隔离的话用例就
    依赖生产库的数据（check_test_db_isolation.py 的静态口径看不见这种间接读）。
    """
    base = Path("data") / ".tmp_tests"
    base.mkdir(parents=True, exist_ok=True)
    db = base / f"notes_{uuid.uuid4().hex[:8]}.db"
    if kind == "table":
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT)")
        for nid in live_ids:
            conn.execute("INSERT OR REPLACE INTO notes (id, title) VALUES (?, ?)",
                         (int(nid), f"笔记{nid}"))
        conn.commit()
        conn.close()
    elif kind == "garbage":
        db.write_text("这不是一个 sqlite 库", encoding="utf-8")
    elif kind == "missing":
        pass                      # 故意不创建：安全闸要求"库文件不存在"
    else:
        raise ValueError(f"未知的 kind：{kind!r}")
    try:
        with patch("memory.database.DB_PATH", str(db)):
            yield db
    finally:
        for _ in range(6):
            try:
                db.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.2)


def _seed(col, rows):
    """往集合塞行：rows 为 (id, metadata, 文本) 三元组列表，向量用假数据"""
    col.upsert(
        documents=[r[2] for r in rows],
        metadatas=[r[1] for r in rows],
        ids=[r[0] for r in rows],
        embeddings=[[0.1] * _FAKE_DIM for _ in rows],
    )


def _metadatas(col) -> list:
    """取集合里全部 metadata（按 id 排序，保证顺序稳定）"""
    got = col.get(include=["metadatas"])
    pairs = sorted(zip(got.get("ids") or [], got.get("metadatas") or []))
    return [m for _, m in pairs]


def test_source_counts_covers_document_rows_only():
    """source_counts 只统计带 source 的文档行；笔记行不计入"""
    from rag.vector_store import source_counts

    with _isolated_store() as (_, col):
        _seed(col, [("a_0", {"source": "a.txt"}, "A0"),
                    ("a_1", {"source": "a.txt"}, "A1"),
                    ("b_0", {"source": "b.md"}, "B0")])
        assert source_counts() == {"a.txt": 2, "b.md": 1}, source_counts()

        # 同一集合里混入笔记行（只有 note_id / title，没有 source）
        _seed(col, [("note_9_0", {"note_id": "9", "title": "t"}, "N0")])
        assert source_counts() == {"a.txt": 2, "b.md": 1}, \
            f"笔记行不该计入 source_counts：{source_counts()}"
    print("[OK] source_counts 只覆盖文档行")


def test_prune_removes_only_orphan_sources():
    """prune_orphan_sources 只删已不在书架上的 source；在架 source 一字不动，且不静默"""
    from rag.vector_store import source_counts
    from index_documents import iter_supported_files, prune_orphan_sources

    with _isolated_store(["keep.txt"]) as (shelf, col):
        _seed(col, [("keep_0", {"source": "keep.txt"}, "K0"),
                    ("keep_1", {"source": "keep.txt"}, "K1"),
                    ("gone_0", {"source": "gone.txt"}, "G0")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            removed = prune_orphan_sources(iter_supported_files(shelf))
        out = buf.getvalue()

        assert removed == 1, f"只该删 1 条孤儿向量，实际 {removed}：{out!r}"
        assert source_counts() == {"keep.txt": 2}, \
            f"在架的 source 必须一字不动：{source_counts()}"
        # 删前清单：只列孤儿，且带条数
        listed = out.split("将删除其全部向量")[-1].split("已删除")[0]
        assert "gone.txt" in listed and "（1 条）" in listed, f"删前应打印将删清单：{out!r}"
        assert "keep.txt" not in listed, f"在架 source 不该进将删清单：{listed!r}"
        # 删后总数 + 无对账不符告警
        assert "已删除 1 条文档向量" in out, f"删后应打印总数：{out!r}"
        assert "⚠️" not in out, f"条数对得上就不该告警：{out!r}"
    print("[OK] prune_orphan_sources 只删孤儿 source")


def test_note_rows_survive_reconciliation():
    """对账只碰文档行：同一集合里只有 note_id 的笔记行必须活着（防误删的牙齿）"""
    from rag.vector_store import source_counts
    from index_documents import prune_orphan_sources

    with _isolated_store() as (_, col):
        _seed(col, [("doc_0", {"source": "gone.txt"}, "G0"),
                    ("note_113_0", {"note_id": "113", "title": "x"}, "N0"),
                    ("note_114_0", {"note_id": "114", "title": "y"}, "N1")])

        # 空书架 ⇒ 所有文档 source 都是孤儿，笔记行一条都不该被碰
        removed = prune_orphan_sources([])
        assert removed == 1, removed
        metas = _metadatas(col)
        assert len(metas) == 2, f"笔记行必须原样留着，实际剩：{metas}"
        assert all("note_id" in m for m in metas), metas
        assert source_counts() == {}, source_counts()
    print("[OK] 笔记行在对账中存活")


def test_reindex_with_fewer_chunks_leaves_no_stale_rows():
    """同一 source 从 5 块重索引为 3 块后恰好 3 条（先删后加的牙齿：旧实现会剩 5 条）"""
    import index_documents
    from rag.vector_store import source_counts

    with _isolated_store(["doc.txt"]) as (_, col), _isolated_notes_db():
        five = ["段1", "段2", "段3", "段4", "段5"]
        with patch.object(index_documents, "load_and_split", return_value=five), \
             patch.object(index_documents, "embed_documents",
                          return_value=[[0.1] * _FAKE_DIM] * 5):
            index_documents.main()
        assert source_counts() == {"doc.txt": 5}, source_counts()

        three = ["段1", "段2", "段3"]
        with patch.object(index_documents, "load_and_split", return_value=three), \
             patch.object(index_documents, "embed_documents",
                          return_value=[[0.1] * _FAKE_DIM] * 3):
            index_documents.main()
        assert source_counts() == {"doc.txt": 3}, \
            f"先删后加应让该 source 恰好 3 条（纯 upsert 会剩 5 条）：{source_counts()}"
        assert col.count() == 3, f"集合总数应为 3，实际 {col.count()}"
    print("[OK] 块数变少时不留陈旧块")


def test_prune_runs_before_empty_shelf_early_return():
    """书架清空后对账仍发生（旧实现在"未发现可索引的文档"处提前 return，向量留着）"""
    import index_documents
    from rag.vector_store import source_counts

    with _isolated_store(["doc.txt"]) as (shelf, col), _isolated_notes_db():
        with patch.object(index_documents, "load_and_split", return_value=["段1"]), \
             patch.object(index_documents, "embed_documents",
                          return_value=[[0.1] * _FAKE_DIM]):
            index_documents.main()
        assert source_counts() == {"doc.txt": 1}, source_counts()

        (shelf / "doc.txt").unlink()          # 书架被清空

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            index_documents.main()
        out = buf.getvalue()
        assert source_counts() == {}, f"书架清空后旧向量必须被删掉：{source_counts()}"
        assert "未发现可索引的文档" in out, out
        assert "已删除 1 条文档向量" in out, out
    print("[OK] 空书架仍先对账")


def test_missing_shelf_dir_skips_reconciliation():
    """书架目录不存在时不对账也不索引（否则在错误的工作目录下会把整个语料当孤儿删掉）"""
    import index_documents
    from rag.vector_store import source_counts

    with _isolated_store() as (_, col), _isolated_notes_db():
        _seed(col, [("doc_0", {"source": "gone.txt"}, "G0")])

        missing = Path("data") / ".tmp_tests" / "no_such_shelf_dir_p7b6"
        buf = io.StringIO()
        with patch("index_documents.files_dir", missing), \
             contextlib.redirect_stdout(buf):
            index_documents.main()

        out = buf.getvalue()
        assert source_counts() == {"gone.txt": 1}, f"安全闸：不该删任何东西（{out!r}）"
        assert "跳过索引与向量库对账" in out, out
    print("[OK] 书架目录不存在时跳过对账")


def test_prune_orphan_notes_removes_only_orphans():
    """笔记侧对账只删 note_id 已不在 notes 表里的行；在册的 note_id 一字不动，且不静默"""
    from rag.note_indexer import prune_orphan_notes
    from rag.vector_store import note_id_counts

    with _isolated_store() as (_, col):
        _seed(col, [("note_901_0", {"note_id": "901", "title": "甲"}, "A0"),
                    ("note_901_1", {"note_id": "901", "title": "甲"}, "A1"),
                    ("note_902_0", {"note_id": "902", "title": "乙"}, "B0"),
                    ("note_903_0", {"note_id": "903", "title": "丙"}, "C0")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            removed = prune_orphan_notes({901, 902})
        out = buf.getvalue()

        assert removed == 1, f"只该删 903 那一条，实际 {removed}：{out!r}"
        assert note_id_counts() == {"901": 2, "902": 1}, \
            f"在册的 note_id 必须一字不动：{note_id_counts()}"
        listed = out.split("将删除其全部向量")[-1].split("已删除")[0]
        assert "903" in listed and "（1 条）" in listed, f"删前应打印将删清单：{out!r}"
        assert "901" not in listed and "902" not in listed, \
            f"在册的 note_id 不该进将删清单：{listed!r}"
        assert "已删除 1 条笔记向量" in out, f"删后应打印总数：{out!r}"
        assert "⚠️" not in out, f"条数对得上就不该告警：{out!r}"
    print("[OK] 笔记侧只删孤儿 note_id")


def test_live_note_ids_none_when_db_missing():
    """库文件不存在 ⇒ live_note_ids() 返回 None（**不是空集**），main() 一条笔记向量都不删"""
    import index_documents
    from rag.note_indexer import live_note_ids
    from rag.vector_store import note_id_counts

    with _isolated_store(["doc.txt"]) as (_, col), _isolated_notes_db(kind="missing"):
        _seed(col, [("note_777_0", {"note_id": "777", "title": "无主"}, "N0")])
        assert live_note_ids() is None, \
            "库不存在时必须返回 None——返回空集会把笔记向量全删"

        buf = io.StringIO()
        with patch.object(index_documents, "load_and_split", return_value=["段1"]), \
             patch.object(index_documents, "embed_documents",
                          return_value=[[0.1] * _FAKE_DIM]), \
             contextlib.redirect_stdout(buf):
            index_documents.main()
        out = buf.getvalue()
        assert note_id_counts() == {"777": 1}, \
            f"读不到库就不该删任何笔记向量：{note_id_counts()}"
        assert "跳过笔记侧对账" in out, out
    print("[OK] 笔记库不存在时跳过笔记侧对账")


def test_live_note_ids_none_when_db_unreadable():
    """读库那一步抛异常 ⇒ 同样返回 None、一条不删、有告警（fail-closed）"""
    from rag.note_indexer import live_note_ids
    from rag.vector_store import note_id_counts

    with _isolated_store() as (_, col), _isolated_notes_db(kind="garbage"):
        _seed(col, [("note_888_0", {"note_id": "888", "title": "无主"}, "N0")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ids = live_note_ids()
        out = buf.getvalue()

        assert ids is None, f"读库失败必须返回 None（不是空集）：{ids!r}"
        assert "跳过笔记侧对账" in out, out
        assert note_id_counts() == {"888": 1}, f"读库失败时不该删任何东西：{note_id_counts()}"
    print("[OK] 读库失败时跳过笔记侧对账")


def test_empty_notes_table_prunes_all_note_rows():
    """空 notes 表 ⇒ 集合里的笔记行全是孤儿、全部删除（与"空书架仍先对账"同形）"""
    import index_documents
    from rag.note_indexer import live_note_ids
    from rag.vector_store import note_id_counts

    with _isolated_store(["doc.txt"]) as (_, col), _isolated_notes_db([]):
        _seed(col, [("note_555_0", {"note_id": "555", "title": "旧"}, "N0"),
                    ("note_556_0", {"note_id": "556", "title": "旧"}, "N1")])
        assert live_note_ids() == set(), \
            f"空表是合法状态：应返回空集（None 表示读不到库）：{live_note_ids()!r}"

        buf = io.StringIO()
        with patch.object(index_documents, "load_and_split", return_value=["段1"]), \
             patch.object(index_documents, "embed_documents",
                          return_value=[[0.1] * _FAKE_DIM]), \
             contextlib.redirect_stdout(buf):
            index_documents.main()
        out = buf.getvalue()
        assert note_id_counts() == {}, f"空 notes 表 ⇒ 笔记向量全删：{note_id_counts()}"
        assert "已删除 2 条笔记向量" in out, out
    print("[OK] 空 notes 表时笔记向量全删")


if __name__ == "__main__":
    print("=== vector store sync test ===\n")
    test_source_counts_covers_document_rows_only()
    test_prune_removes_only_orphan_sources()
    test_note_rows_survive_reconciliation()
    test_reindex_with_fewer_chunks_leaves_no_stale_rows()
    test_prune_runs_before_empty_shelf_early_return()
    test_missing_shelf_dir_skips_reconciliation()
    test_prune_orphan_notes_removes_only_orphans()
    test_live_note_ids_none_when_db_missing()
    test_live_note_ids_none_when_db_unreadable()
    test_empty_notes_table_prunes_all_note_rows()
    print("\n=== ALL TESTS PASSED ===")
