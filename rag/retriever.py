"""
RAG 检索编排：查询嵌入 → Chroma 召回 → 重排精排 → 格式化输出

供 tools/local_tools.py 的 rag_search 工具委托调用。
模型路径通过 rag.model_utils.resolve_model_path 自动发现，
兼容 ModelScope 平铺布局与 HuggingFace 缓存布局。
"""
import time

from rag.embedding import embed_query
from rag.reranker import rerank
from rag.vector_store import query_documents


def rag_search(query: str, top_k: int = 3) -> str:
    """从本地知识库检索相关内容

    流程：
    1. bge-m3 将查询向量化；
    2. Chroma 按余弦相似度召回 top_k*3 个候选（多召回保证精排有余地）；
    3. bge-reranker-base 对候选精排，取前 top_k 条；
    4. 格式化为字符串返回（保持工具调用方期望的文本格式）。

    Args:
        query: 用户查询
        top_k: 最终返回的片段数

    Returns:
        检索结果字符串；知识库为空或出错时返回友好提示（不抛异常）
    """
    start = time.time()
    print(f"[Retriever] rag_search: {query!r}, top_k={top_k}")

    if not query or not query.strip():
        return "❌ 检索内容不能为空。"
    query = query.strip()
    # 钳制 top_k，防止工具传参异常
    try:
        top_k = max(1, min(int(top_k), 10))
    except (ValueError, TypeError):
        top_k = 3

    try:
        from rag.vector_store import get_collection
        # 空库检查：未建立索引时给出可执行的指引，而不是返回空结果
        collection = get_collection()
        if collection.count() == 0:
            return ("⚠️ 本地知识库为空（尚未建立向量索引）。"
                    "请先运行 index_documents.py 将 data/library（公共书架）下的文档入库后再试。")

        # 1) 查询向量化
        query_embedding = embed_query(query)

        # 2) 向量召回候选（过量召回，给重排留余地）
        recall_k = min(top_k * 3, 12)
        results = query_documents(query_embedding, top_k=recall_k)
        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        distances = (results.get("distances") or [[]])[0] or []
        if not docs:
            return "未找到相关内容，请更换关键词重试。"

        # 3) 重排精排（失败时降级为按召回距离排序，保证可用性）
        try:
            ranked = rerank(query, docs, top_k=top_k)
            rank_source = "rerank"
        except Exception as e:
            print(f"[Retriever] 重排失败，降级为向量距离排序: {e}")
            ranked = [d for _, d in sorted(zip(distances, docs), key=lambda x: x[0])][:top_k]
            rank_source = "vector"

        # 4) 格式化输出（保留来源与序号）
        # 以文本内容为键映射来源，避免依赖 id(doc) 对象标识——
        # 精排返回的字符串可能与召回列表内容相等但不是同一对象
        meta_by_doc = {}
        for doc_text, meta in zip(docs, metas):
            meta_by_doc.setdefault(doc_text, meta or {})

        lines = [f"🔍 从本地知识库检索到以下相关内容（{rank_source} 排序）：", ""]
        for i, doc in enumerate(ranked, 1):
            meta = meta_by_doc.get(doc, {})
            # 来源标注（P7 批次 7-③）：集合里两半共用一个集合、metadata 键不同——
            # 文档行有 source（书架相对路径），笔记行有 title 而没有 source。
            # 只看 source 会让所有笔记命中都显示"未知来源"，与工具描述里"能答你的笔记"
            # 自相矛盾；故先取 source，没有则取 title 并标明是笔记，两者都没有才回落。
            src = meta.get("source")
            if not src and meta.get("title"):
                src = f"你的笔记：{meta['title']}"
            if not src:
                src = "未知来源"
            snippet = doc.strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "…"
            lines.append(f"【{i}】(来源: {src})\n{snippet}\n")

        print(f"[Retriever] rag_search 完成，耗时 {time.time()-start:.2f}s，"
              f"召回 {len(docs)} → 返回 {len(ranked)}")
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"[Retriever] rag_search 异常: {e}")
        return f"❌ 检索知识库时出错：{e}"
