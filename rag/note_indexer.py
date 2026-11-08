"""
笔记 RAG 向量索引器

职责：
- 将笔记内容分块、嵌入并写入 Chroma 向量库，实现语义检索；
- 删除指定笔记在向量库中的所有分块；
- 提供语义搜索接口：向量化查询 → 检索向量库 → 解析 note_id →
  从 SQLite 读取完整笔记返回；
- **笔记侧向量对账**（P7 批次 7-⑤，模块末尾的 live_note_ids / prune_orphan_notes）：
  清理"笔记已不在 notes 表、向量还留在集合里"的孤儿行——原先只有"改/删笔记"时才会
  触发删除、且失败只 print，孤儿向量因此无声积累（本机实测积了 62 条，会被 rag_search
  当笔记返回）。

设计说明：
- 所有方法均为 @staticmethod + lazy import，避免与 NoteStore 形成
  循环依赖（NoteStore 在 add/update/delete 后延迟调用本模块）；
- 向量库或嵌入模型不可用时，捕获异常并返回空列表/打印日志，
  不影响笔记的正常存储（降级为仅数据库保存）；
- 分块策略自 P4-6 起统一由 core/text_utils.split_paragraphs 承担（此前本模块的
  _chunk_text 与 rag/document_loader.load_and_split 是同算法异名的两份实现，
  且 overlap 行为有分叉；现已收拢到单一实现）。
- 向量文档 ID 使用稳定格式 note_{note_id}_{chunk_i}，配合
  Chroma 的 upsert 实现增改幂等；删除使用 metadata where 过滤
  （note_id），无需枚举所有分块 ID。
"""

from typing import List, Optional

from core.text_utils import split_paragraphs


class NoteIndexer:
    """笔记向量索引管理器（Chroma-based）"""

    # ---------- 索引 ----------

    @staticmethod
    def index_note(note_id: int, title: str, content: str) -> bool:
        """将笔记分块、嵌入并写入向量库

        索引内容为「标题 + 正文」，便于语义搜索匹配到标题关键词。
        使用 note_{note_id}_{i} 的稳定 ID，重复索引时覆盖旧块。

        Returns:
            True 表示索引成功（或内容为空时跳过）；False 表示索引失败。
        """
        try:
            from rag.vector_store import add_documents
            from rag.embedding import embed_documents
        except Exception as e:
            print(f"[NoteIndexer] 嵌入/向量库模块导入失败（跳过索引）: {e}")
            return False

        # 组合标题 + 正文作为索引文本
        text = f"{title}\n\n{content or ''}".strip()
        if not text:
            return True  # 内容为空，无需索引但不视为失败

        chunks = split_paragraphs(text)
        if not chunks:
            return True

        try:
            embeddings = embed_documents(chunks)
        except Exception as e:
            print(f"[NoteIndexer] 嵌入生成失败（跳过笔记 {note_id}）: {e}")
            return False

        metadatas = [{"note_id": str(note_id), "title": title or ""} for _ in chunks]
        ids = [f"note_{note_id}_{i}" for i in range(len(chunks))]

        try:
            add_documents(chunks, metadatas, ids, embeddings=embeddings)
            print(f"[NoteIndexer] 笔记 {note_id} 已索引（{len(chunks)} 块）")
            return True
        except Exception as e:
            print(f"[NoteIndexer] 笔记 {note_id} 入库失败（忽略）: {e}")
            return False

    @staticmethod
    def remove_note_from_index(note_id: int) -> bool:
        """从向量库中删除指定笔记的所有分块（通过 metadata where 过滤）"""
        try:
            from rag.vector_store import delete_documents
        except Exception as e:
            print(f"[NoteIndexer] delete_documents 导入失败（忽略）: {e}")
            return False

        try:
            delete_documents(where={"note_id": str(note_id)})
            print(f"[NoteIndexer] 笔记 {note_id} 向量索引已删除")
            return True
        except Exception as e:
            print(f"[NoteIndexer] 笔记 {note_id} 向量删除失败（忽略）: {e}")
            # 可见信号（P7 批次 7-⑤）：本函数在 _run_async 的后台线程里跑，print 基本
            # 没人看见——孤儿向量就是这么无声积累的（本机实测集合里曾积到 62 条）。
            # 埋点规格照批次 5b：延迟导入 + 整体 try/except，失败不影响本次调用。
            try:
                from memory.resource_log import log_resource_usage
                log_resource_usage("rag", 0, "note_unindex_failed",
                                   f"笔记 {note_id} 向量删除失败")
            except Exception:
                pass
            return False

    # ---------- 语义搜索 ----------

    @staticmethod
    def search_notes_semantic(query: str, goal_id: Optional[str] = None,
                              top_k: int = 5) -> List[dict]:
        """语义搜索笔记：向量化查询 → 检索向量库 → 映射回 NoteStore

        流程：
        1. embed_query 将查询文本向量化；
        2. query_documents 从 Chroma 检索 top_k*2 个候选块（多召回去重）；
        3. 从 metadata 中提取 note_id，去重并按相似度顺序保留；
        4. 从 SQLite notes 表读取完整笔记内容；
        5. 若 goal_id 指定，按目标 ID 二次过滤。

        Returns:
            笔记 dict 列表（含 tags 等全部字段）；检索失败或知识库为空返回 []。
        """
        if not query or not query.strip():
            return []

        try:
            from rag.vector_store import query_documents, get_collection
            from rag.embedding import embed_query
        except Exception as e:
            print(f"[NoteIndexer] 语义搜索模块导入失败（降级为空）: {e}")
            return []

        try:
            # 空库检查：未建立索引时直接返回空，调用方可给出友好提示回退关键词模式
            collection = get_collection()
            if collection.count() == 0:
                print("[NoteIndexer] 向量库为空，语义搜索返回 []")
                return []

            query_embedding = embed_query(query)
            recall_k = max(top_k * 2, 10)  # 多召回，保证去重后仍有足够结果
            results = query_documents(query_embedding, top_k=recall_k)

            metadatas = (results.get("metadatas") or [[]])[0] or []
            note_ids: List[int] = []
            seen = set()
            for meta in metadatas:
                nid = meta.get("note_id")
                if nid is None:
                    continue
                try:
                    nid_int = int(nid)
                except (ValueError, TypeError):
                    continue
                if nid_int not in seen:
                    seen.add(nid_int)
                    note_ids.append(nid_int)

            if not note_ids:
                return []

            # 从 SQLite 读取完整笔记
            from memory.database import get_db
            conn = get_db()
            placeholders = ",".join("?" * len(note_ids))
            rows = conn.execute(
                f"SELECT * FROM notes WHERE id IN ({placeholders})",
                note_ids,
            ).fetchall()
            conn.close()

            notes = [dict(r) for r in rows]

            # 按目标过滤（可选）
            if goal_id:
                gid = (goal_id or "").strip()
                notes = [n for n in notes if (n.get("goal_id") or "") == gid]

            # 保持相似度顺序（note_ids 已按召回顺序排序）
            order = {nid: i for i, nid in enumerate(note_ids)}
            notes.sort(key=lambda n: order.get(n["id"], 9999))

            print(f"[NoteIndexer] 语义搜索返回 {len(notes)} 篇笔记")
            return notes

        except Exception as e:
            print(f"[NoteIndexer] 语义搜索失败（降级为空）: {e}")
            return []


# ---------- 笔记侧向量对账（P7 批次 7-⑤） ----------

def live_note_ids() -> Optional[set]:
    """读 `notes` 表现存的笔记 id 集合（int）；**读不到时返回 None**，不是空集

    空集与 None 是两件事，必须分开：空集＝"确实一条笔记都没有"（此时集合里所有笔记行
    都是孤儿，该删）；None＝"读不到库，无法判断"（此时**一条都不能删**）。把两者混成
    空集，就等于"读不到库 ⇒ 把笔记向量全删"。

    安全闸：`memory.database.DB_PATH` 指向的库文件不存在时直接返回 None——它是相对
    当前工作目录的路径，在错误的目录下跑会读到不存在的库，把"什么都没有"当成事实。
    DB_PATH 取的是**模块属性**（不是 from-import 拿到的副本），这样测试的字符串靶子
    patch 才生效。
    """
    import os
    from memory import database as db_module

    try:
        db_path = db_module.DB_PATH
        if not os.path.exists(db_path):
            print(f"⚠️ 笔记库 {db_path} 不存在（可能是工作目录不对），跳过笔记侧对账。")
            return None
        conn = db_module.get_db()
        try:
            rows = conn.execute("SELECT id FROM notes").fetchall()
        finally:
            conn.close()
        return {int(r["id"]) for r in rows if r["id"] is not None}
    except Exception as e:
        print(f"⚠️ 读取笔记库失败，跳过笔记侧对账（不删任何笔记向量）: {e}")
        return None


def prune_orphan_notes(live_ids) -> int:
    """删除向量库里 note_id 已不在 live_ids 里的笔记行，返回实际删除条数

    Args:
        live_ids: `notes` 表现存的笔记 id 集合（int）。集合由调用方给，本函数不读库
                  ——形状照 `index_documents.prune_orphan_sources(files)`。

    只碰「带 note_id 且 note_id ∉ live_ids」的那些行：逐个
    `delete_documents(where={"note_id": str(nid)})`。**绝不用 `where={"note_id": None}`
    这类取反表达**——它会选中所有**没有**该键的行（也就是文档行整批），与文档侧那条
    已知误伤同形。也不假设当前 Chroma 支持 `{"$in": [...]}`（未实测），故逐个 id 调。

    删前逐行打印将删清单（笔记 id + 块数），删后按 note_id_counts 的实际变化报总数；
    预期与实际不符时额外告警——对账不允许静默。
    """
    from rag.vector_store import delete_documents, note_id_counts

    counts = note_id_counts()
    if not counts:
        print("向量库对账（笔记）：集合里没有带 note_id 的行，无需对账")
        return 0

    on_disk = {int(i) for i in live_ids}
    orphans = {}
    for nid, blocks in counts.items():
        try:
            nid_int = int(nid)
        except (TypeError, ValueError):
            # 键不是整数：无法与 notes.id 比对，保守起见不删（宁可留着，也不误删）
            print(f"⚠️ 向量库对账（笔记）：note_id {nid!r} 不是整数，跳过（不删）")
            continue
        if nid_int not in on_disk:
            orphans[nid_int] = blocks

    if not orphans:
        print(f"向量库对账（笔记）：{len(counts)} 个 note_id 都还在笔记库里，无需删除")
        return 0

    before = sum(counts.values())
    print(f"向量库对账（笔记）：以下 {len(orphans)} 个 note_id 已不在笔记库里，"
          f"将删除其全部向量：")
    for nid_int, blocks in sorted(orphans.items()):
        print(f"  - 笔记 {nid_int}（{blocks} 条）")
    for nid_int in sorted(orphans):
        delete_documents(where={"note_id": str(nid_int)})

    removed = before - sum(note_id_counts().values())
    expected = sum(orphans.values())
    if removed != expected:
        print(f"⚠️ 向量库对账（笔记）：预期删除 {expected} 条，实际减少 {removed} 条"
              f"（可能有并发写入，或删除未生效）")
    print(f"向量库对账（笔记）：已删除 {removed} 条笔记向量"
          f"（涉及 {len(orphans)} 个 note_id）")
    return removed
