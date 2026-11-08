"""重排模型（三级路径解析：环境变量 → 默认目录 → 自动发现）"""
import time
from sentence_transformers import CrossEncoder
from .model_utils import resolve_model_path

_model = None

DEFAULT_MODEL_PATH = "data/models/bge-reranker-base"

def get_reranker_model():
    global _model
    if _model is None:
        local_path = resolve_model_path(
            keyword="bge-reranker",
            default_path=DEFAULT_MODEL_PATH,
            env_var="RERANKER_MODEL_PATH",
        )
        print(f"[Reranker] 正在加载本地模型 {local_path} ...")
        start = time.time()
        _model = CrossEncoder(local_path)
        print(f"[Reranker] 模型加载完成，耗时 {time.time()-start:.2f}s")
    return _model

def rerank(query: str, documents: list, top_k: int = 3) -> list:
    if not documents:
        return []
    model = get_reranker_model()
    pairs = [[query, doc] for doc in documents]
    start = time.time()
    scores = model.predict(pairs)
    print(f"[Reranker] 预测耗时 {time.time()-start:.3f}s，文档数 {len(documents)}")
    sorted_items = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in sorted_items[:top_k]]