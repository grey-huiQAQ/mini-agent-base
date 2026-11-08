"""文档加载与切分"""
from pathlib import Path

from core.text_utils import split_paragraphs


def load_and_split(file_path: str, chunk_size: int = 500, overlap: int = 50) -> list:
    """读入文档并按段落切分为块

    分块算法自 P4-6 起统一由 core/text_utils.split_paragraphs 承担。此前本文件与
    rag/note_indexer._chunk_text 各有一份同算法实现、且 overlap 行为不同：本函数原为
    "无条件取前块尾部"，对短于 overlap 的前块会把整段重复拼进下一块；统一后取
    _chunk_text 一侧的语义（前块长度不足 overlap 时不加重叠）。签名与返回类型不变，
    调用方 index_documents.py 无需改动。
    """
    path = Path(file_path)
    if not path.exists():
        return []
    if path.suffix.lower() not in ['.txt', '.md', '.markdown']:
        return []

    text = path.read_text(encoding='utf-8', errors='ignore')
    return split_paragraphs(text, chunk_size=chunk_size, overlap=overlap)
