# -*- coding: utf-8 -*-
"""core.text_utils —— 文本处理共享工具（目前只有分块）

为什么存在这个模块（P4-6）：
    同一套「按空行切段落 → 贪心合并到 ≤ chunk_size → 相邻块加 overlap 前缀」的算法，
    在本项目里被实现了两遍、且名字不同：
      - rag/document_loader.py 的 load_and_split()（读文件版）
      - rag/note_indexer.py 的 _chunk_text()（字符串版）
    note_indexer 的模块 docstring 当时自己就写着「分块策略复用 document_loader 的
    段落切分逻辑……但直接操作字符串而非文件」——即"靠复制 + 注释承诺保持一致"维持的
    一致性，正是本项目反复被咬到的模式（另有 `_extract_terms` 在 note_store /
    experience_store 的同型副本）。现收拢到本模块，两个调用方都改为委托。

语义（P4-6 已确认接受的行为变更，取 _chunk_text 一侧）：
    仅当**前一块长度 ≥ overlap** 时才给它加重叠前缀。document_loader 的旧实现是
    无条件取前块尾部 `[-overlap:]`——当前块短于 overlap 时，`[-overlap:]` 会返回整串，
    等于把前一块全文重复拼进下一块。统一后短段落不再被重复拼入。

**混存声明（有意接受的不一致，不是缺陷）**：
    2026-09-12 起分块统一为 split_paragraphs（短前块不加重叠）。Chroma 中此前入库的
    向量仍按旧策略分块，**未重算**，因此向量库内存在两套粒度的块共存——这是用户明确
    接受的取舍（判定旧检索结果不重要），不是遗漏。若日后重跑 index_documents.py，
    新块会按新策略生成；在那之前，检索质量若出现波动，请先想到这条声明。
"""

import re
from typing import List

# 段落分隔：空行（可含空白字符）
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def split_paragraphs(text: str, chunk_size: int = 500,
                     overlap: int = 50) -> List[str]:
    """把文本按段落切分并合并为不超过 chunk_size 的块

    策略：
    1. 按空行分割为段落（顺带去掉段落首尾空白，空段落丢弃）；
    2. 贪心合并段落到不超过 chunk_size（相邻段落之间以一个空格连接）；
    3. 相邻块之间添加 overlap 字符的重叠前缀，提升跨块语义连续性；**前一块长度不足
       overlap 时不加前缀**（避免把短段落整段重复一遍）。

    Args:
        text: 输入文本；空或 None 返回空列表。
        chunk_size: 单块最大字符数。
        overlap: 相邻块重叠的字符数（前块长度不足时不生效）。

    Returns:
        分块列表（保序）。
    """
    if not text:
        return []
    paragraphs = _PARAGRAPH_SPLIT_RE.split(text)
    chunks: List[str] = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + " " + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)

    # 添加重叠
    final_chunks: List[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0 and len(chunks[i - 1]) >= overlap:
            chunk = chunks[i - 1][-overlap:] + " " + chunk
        final_chunks.append(chunk)
    return final_chunks
