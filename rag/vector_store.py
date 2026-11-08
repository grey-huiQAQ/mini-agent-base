"""Chroma 向量数据库管理"""
import chromadb
from chromadb.config import Settings
import time

_client = None
_collection = None

def get_collection():
    global _client, _collection
    if _client is None:
        print("[VectorStore] 初始化 Chroma PersistentClient ...")
        start = time.time()
        _client = chromadb.PersistentClient(
            path="data/chroma",
            settings=Settings(anonymized_telemetry=False)
        )
        _collection = _client.get_or_create_collection(
            name="personal_knowledge",
            metadata={"hnsw:space": "cosine"}
        )
        print(f"[VectorStore] Chroma 初始化完成，耗时 {time.time()-start:.2f}s")
    return _collection

def add_documents(texts: list, metadatas: list = None, ids: list = None,
                  embeddings: list = None):
    """向集合添加文档（幂等：相同 ID 走 upsert 覆盖旧块）

    Args:
        texts: 文档文本列表
        metadatas: 元数据列表
        ids: 文档 ID 列表（index_documents.py 基于相对路径生成确定性 ID，
             重复索引同一文件时覆盖旧内容而不是报错或产生重复）
        embeddings: 预计算的向量列表（bge-m3）。若为 None，Chroma 会退回
                    内置默认嵌入函数——本项目应始终传入以保持向量空间一致
    """
    if not texts:
        return
    collection = get_collection()
    if ids is None:
        import uuid
        ids = [str(uuid.uuid4()) for _ in texts]
    collection.upsert(
        documents=texts,
        metadatas=metadatas or [{}] * len(texts),
        ids=ids,
        embeddings=embeddings,
    )

def query_documents(query_embedding, top_k: int = 5):
    collection = get_collection()
    start = time.time()
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )
    print(f"[VectorStore] 查询耗时 {time.time()-start:.3f}s")
    return results


def delete_documents(ids: list = None, where: dict = None):
    """从集合删除文档（按 ID 或 metadata 过滤条件）

    Args:
        ids: 要删除的文档 ID 列表；为 None 时不按 ID 过滤
        where: metadata 过滤条件字典；为 None 时不按 metadata 过滤

    注意：Chroma 的 delete 要求至少提供 ids 或 where 中的一个，
    否则会抛出 ValueError。本方法在两者都为空时直接返回，
    避免意外清空整个集合。
    """
    if not ids and not where:
        return
    collection = get_collection()
    collection.delete(ids=ids, where=where)


def source_counts() -> dict:
    """统计集合里各 `source` 的条数（**只覆盖文档行**），返回 {source: 条数}

    两个必须知道的约束（P7 批次 6-①）：

    ① 这个集合由**两个生产者共用**，两半的 metadata 键不同：
       - 文档行：`index_documents.py` 写入，键是 `source`（书架上文件的相对路径）；
       - 笔记行：`rag/note_indexer.py` 写入，键是 `note_id` + `title`，**没有** `source`。
       本函数刻意只统计带 `source` 的行——对账与清理面向的是文档侧，笔记侧不归它管。

    ② 因此调用方**绝不可**把缺失值（None）交给 `delete_documents` 的 where：
       `where={"source": None}` 选中的是**所有没有 source 的行**，也就是把笔记行整批
       删掉。这一侧选中的是笔记行的总数（随笔记增减而变，故此处不写死数字）。这是数据保护问题，
       不是风格问题。

    实现说明：全量取元数据后在 Python 侧计数（Chroma 的 where 没有稳健的「键存在」
    写法）。本集合是几百行量级，够用；若将来上万行，再改成分页取。
    """
    return _count_by_metadata_key("source")


def note_id_counts() -> dict:
    """统计集合里各 `note_id` 的条数（**只覆盖笔记行**），返回 {note_id: 块数}

    与 `source_counts()` 完全对称，只是换了一半：这里是 `rag/note_indexer.py` 写入的
    笔记行（带 `note_id` + `title`），文档行（带 `source`）不计入；键是**字符串**
    （note_indexer 写 metadata 时用 `str(note_id)`）。

    同样的数据保护要求：调用方**绝不可**把缺失值（None）交给 `delete_documents` 的
    where —— `where={"note_id": None}` 选中的是所有**没有** note_id 的行，也就是把文档行
    整批删掉（对称于 source_counts 记的那条）。
    本机当前集合里文档行是 0 行，所以这一侧的风险眼下是潜伏的，不是不存在。
    """
    return _count_by_metadata_key("note_id")


def _count_by_metadata_key(key: str) -> dict:
    """按某个 metadata 键统计条数（私有公用实现：两个公开计数函数只差这一个键名）

    只统计**带着该键**的行；key 缺失的行不计入、也不会被表达成 None 参与下游过滤。
    """
    collection = get_collection()
    if collection.count() == 0:
        return {}
    got = collection.get(include=["metadatas"])
    counts = {}
    for meta in (got.get("metadatas") or []):
        if not isinstance(meta, dict):
            continue
        value = meta.get(key)
        if value is None:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts
