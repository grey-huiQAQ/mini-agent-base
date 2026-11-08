import time
from pathlib import Path
from sentence_transformers import SentenceTransformer

from rag.model_utils import resolve_model_path

_model = None

DEFAULT_MODEL_PATH = "data/models/bge-m3"

def get_embedding_model():
    global _model
    if _model is None:
        # 三级解析：环境变量 EMBEDDING_MODEL_PATH → 默认目录 → data/models 下自动发现
        local_path = resolve_model_path(
            keyword="bge-m3",
            default_path=DEFAULT_MODEL_PATH,
            env_var="EMBEDDING_MODEL_PATH",
        )
        print(f"[Embedding] 正在加载本地模型 {local_path} ...")
        start = time.time()
        _model = SentenceTransformer(local_path)
        print(f"[Embedding] 模型加载完成，耗时 {time.time()-start:.2f}s")
    return _model

def embed_documents(texts: list) -> list:
    model = get_embedding_model()
    return model.encode(texts, normalize_embeddings=True).tolist()

def embed_query(text: str) -> list:
    model = get_embedding_model()
    start = time.time()
    vec = model.encode([text], normalize_embeddings=True)[0].tolist()
    print(f"[Embedding] 查询嵌入耗时 {time.time()-start:.3f}s")
    return vec