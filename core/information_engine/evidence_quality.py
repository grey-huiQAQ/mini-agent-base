"""
证据质量处理（Information Engine V0.2）

- rank_evidence：证据排序评分（来源类型 / 关键词命中 / 内容长度 → 更新 confidence）；
- deduplicate_evidence：证据去重（URL 精确匹配或标题/摘要相似度，保留置信度更高者）；
- information_gain_stop：信息增益停止判定（新增来源 / 新增事实片段不足）。

全部为纯函数（不访问数据库、不发起网络），便于单测；
SearchTaskManager 每轮合并证据后调用：rank（评分）→ sort（按 confidence 降序）
→ deduplicate（去重）→ information_gain_stop（停止判定）。
"""

import difflib
import re
from typing import List, Optional

from .models import Evidence

# 默认来源权重（可配置；GitHub 优先于 DuckDuckGo，其余来源取默认 0.6）
DEFAULT_SOURCE_WEIGHTS = {"GitHub": 0.9, "DuckDuckGo": 0.7}

# 内容长度评分满分所需字符数（长度分 = min(1, 规范化文本长度 / 200)）
LENGTH_SCORE_FULL = 200.0

# 事实片段相似度阈值：规范化文本 ratio() >= 该值视为同一事实（非新增）
FACT_SIMILARITY_THRESHOLD = 0.8


def _normalize_text(text: str) -> str:
    """规范化文本：小写 + 去全部空白（供相似度与关键词匹配使用）"""
    return re.sub(r"\s+", "", (text or "").lower())


def _extract_keywords(query: str, top: int = 8) -> List[str]:
    """从查询中提取关键词（与项目检索词规则一致）：
    英文/数字词按非单词字符切分（保留长度 >= 2），中文片段生成相邻双字组合；
    去重保序，最多 top 个。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    words: List[str] = []
    for w in re.split(r"[^\w]+", q):
        if len(w) >= 2 and not re.search(r"[\u4e00-\u9fff]", w):
            words.append(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            words.append(seg)
        else:
            words.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    seen, uniq = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq[:top]


def rank_evidence(evidences: List[Evidence], query: str = "",
                  source_weights: Optional[dict] = None,
                  source_weight: float = 0.3,
                  keyword_weight: float = 0.5,
                  length_weight: float = 0.2) -> List[Evidence]:
    """证据排序评分（纯函数）：更新每条证据的 confidence（0~1）

    评分公式：
        confidence = source_weight * 来源权重
                   + keyword_weight * 关键词命中率
                   + length_weight * 长度分
    - 来源权重：source_weights.get(source_name, 0.6)；默认 GitHub 0.9 > DuckDuckGo 0.7；
    - 关键词命中率：命中关键词数 / 总关键词数（查询为空时按 0.5 中性处理）；
    - 长度分：min(1, 规范化文本（标题+摘要）长度 / 200)。

    Returns:
        评分后的证据列表（顺序不变，供调用方按 confidence 排序后去重）。
    """
    weights = dict(source_weights or DEFAULT_SOURCE_WEIGHTS)
    keywords = _extract_keywords(query or "")
    total_kw = max(1, len(keywords))
    ranked: List[Evidence] = []
    for ev in evidences:
        src_score = weights.get(ev.source_name or "", 0.6)
        text = _normalize_text(f"{ev.title} {ev.snippet}")
        hits = sum(1 for kw in keywords if kw in text)
        kw_score = hits / total_kw if keywords else 0.5
        len_score = min(1.0, len(text) / LENGTH_SCORE_FULL)
        score = (source_weight * src_score
                 + keyword_weight * kw_score
                 + length_weight * len_score)
        ev.confidence = round(max(0.0, min(1.0, score)), 3)
        ranked.append(ev)
    return ranked


def deduplicate_evidence(evidences: List[Evidence],
                         threshold: float = 0.9) -> List[Evidence]:
    """证据去重（纯函数）：重复组保留首个（即置信度最高者，调用方需先按
    confidence 降序排序），输出保持输入顺序

    重复判定：
    - 双方 URL 均非空且完全一致 → 重复；
    - 否则标题+摘要规范化文本的 SequenceMatcher.ratio() >= threshold → 重复。

    Returns:
        去重后的证据列表（顺序与输入一致）。
    """
    if not evidences:
        return []
    deduped: List[Evidence] = []
    for ev in evidences:
        if any(_is_duplicate(ev, kept, threshold) for kept in deduped):
            continue
        deduped.append(ev)
    return deduped


def _is_duplicate(a: Evidence, b: Evidence, threshold: float) -> bool:
    if a.source_url and b.source_url and a.source_url == b.source_url:
        return True
    text_a = _normalize_text(f"{a.title} {a.snippet}")
    text_b = _normalize_text(f"{b.title} {b.snippet}")
    if not text_a or not text_b:
        return False
    return difflib.SequenceMatcher(None, text_a, text_b).ratio() >= threshold


def information_gain_stop(previous_evidences, current_evidences,
                          window: int = 2, source_min: int = 1,
                          fact_min: int = 2) -> bool:
    """信息增益停止判定（纯函数）：本轮相对上一轮增益不足返回 True

    判定条件（同时满足）：
    - 新增有效来源数 ≤ source_min：current 的来源集合 - previous 的来源集合；
    - 新增事实片段数 ≤ fact_min：current 的 snippet 规范化文本中，与 previous
      全部历史片段相似度（SequenceMatcher.ratio）均 < 0.8 才视为新增，同轮内
      重复片段只计一次。

    Args:
        previous_evidences: 上一轮合并去重排序后的证据（增量比较基准）
        current_evidences:  本轮合并去重排序后的证据
        window: 连续低增益轮数阈值 —— 由 SearchTaskManager 累计判断，**连续
                window 轮**本函数均返回 True 时才真正停止搜索（本函数只做单轮
                判定；首轮 previous 为空时视为增益充足，返回 False）
        source_min: 新增有效来源数阈值
        fact_min: 新增事实片段数阈值

    Returns:
        True = 本轮增益不足；False = 本轮有实质新增（或为首轮）。
    """
    if not previous_evidences:
        # 首轮（无历史基准）视为增益充足，避免刚起步就被误判停止
        return False
    prev_sources = {e.source_name for e in previous_evidences if e.source_name}
    curr_sources = {e.source_name for e in current_evidences if e.source_name}
    new_sources = len(curr_sources - prev_sources)
    new_facts = _count_new_facts(previous_evidences, current_evidences)
    return new_sources <= source_min and new_facts <= fact_min


def _count_new_facts(previous_evidences, current_evidences,
                     similarity_threshold: float = FACT_SIMILARITY_THRESHOLD) -> int:
    """统计当前证据相对上一轮集合的新增事实片段数

    对每条当前证据的 snippet 规范化（小写、去空白）后，与上一轮全部历史片段
    比较 SequenceMatcher.ratio()：与所有历史片段相似度均 < 阈值才视为新增；
    同一轮内重复片段只计一次。
    """
    prev_norms = {_normalize_text(e.snippet) for e in previous_evidences}
    seen_in_round = set()
    count = 0
    for ev in current_evidences:
        norm = _normalize_text(ev.snippet)
        if not norm or norm in seen_in_round:
            continue
        seen_in_round.add(norm)
        is_new = True
        for prev_norm in prev_norms:
            if not prev_norm:
                continue
            if difflib.SequenceMatcher(None, norm, prev_norm).ratio() >= similarity_threshold:
                is_new = False
                break
        if is_new:
            count += 1
    return count
