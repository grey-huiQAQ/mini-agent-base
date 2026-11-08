"""
Experience Store - 经验沉淀存储

职责：
- 存储 ReflectionAgent 从对话中提取的可复用经验（有效策略 / 用户偏好 / 成功模式）
- 提供增删查与基于关键词计分的相关性检索，供 ContextManager 注入上下文使用
- 记录用户"有用/无用"反馈评分（score），检索时高评分经验优先，支持淘汰低分经验
- 按目标取「已回流成经验的复盘教训」里最近的那一条，供第一闭环 Phase 4b 注入下一轮上下文（latest_goal_lessons，只读、不写任何表）

设计说明：
- 与 LearningRecorder 等记忆模块保持一致：@staticmethod + get_db()
- 检索不引入任何新依赖：中文按相邻双字组合（bigram）、英文按单词切分，
  在 Python 侧对命中词计数打分，规模（数百条内）下足够高效
- 评分列（score/use_count/last_used_at）由 database.init_db 自动迁移，
  旧数据默认 score=0，行为与历史版本完全兼容
"""

import re
from datetime import datetime
from typing import List, Optional

from .database import get_db


# 合法的反馈类型（record_feedback 的 feedback 参数）
VALID_FEEDBACK = ("up", "down")

# 检索词默认最大参与数量：超长查询会生成大量 bigram，全量计分会稀释相关性，
# 仅保留在查询中出现频次最高的前 N 个词（0 或负数表示不截断，保持旧行为）
DEFAULT_MAX_TERMS = 10


def _extract_terms(query: str) -> List[str]:
    """从查询中提取检索词（去重保序）

    规则：
    - 英文/数字词：按非单词字符切分，保留长度 >= 2 的词
    - 中文片段：生成相邻双字组合（bigram）；单字片段保留单字
      （如"背英语单词" → 背英 / 英语 / 语单 / 单词，无分词依赖即可命中）
    """
    query = (query or "").strip().lower()
    if not query:
        return []
    terms: List[str] = []
    # 1. 英文/数字词（不含中文的 \w 片段）
    for word in re.split(r"[^\w]+", query):
        if len(word) >= 2 and not re.search(r"[\u4e00-\u9fff]", word):
            terms.append(word)
    # 2. 中文二元组合
    for seg in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(seg) == 1:
            terms.append(seg)
        else:
            terms.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    # 去重保序
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _rank_terms(terms: List[str], query: str,
                max_terms: int = DEFAULT_MAX_TERMS) -> List[str]:
    """把检索词按在原查询中的出现频次降序排序，并截断 Top-N

    设计说明：
    - 频次相同的词保持 _extract_terms 的原始提取顺序（稳定排序，结果可复现）；
    - max_terms 为 None / <= 0 时不截断（向后兼容旧行为）；
    - 目的：长输入检索精度优化——避免超长查询生成过多 bigram 导致召回稀释。
    """
    lowered = (query or "").lower()
    order = {t: i for i, t in enumerate(terms)}
    ranked = sorted(terms, key=lambda t: (-lowered.count(t), order[t]))
    if max_terms is not None and max_terms > 0:
        ranked = ranked[:max_terms]
    return ranked


class ExperienceStore:
    """经验沉淀管理器（SQLite experiences 表）"""

    @staticmethod
    def add_experience(scenario: str, strategy: str, effect: str = "",
                       conditions: str = "", source_session_id: str = None) -> int:
        """新增一条经验，返回自增 ID

        Args:
            scenario: 适用场景（何时参考这条经验）
            strategy: 具体策略 / 偏好 / 做法
            effect: 观察到的效果（可选）
            conditions: 适用条件或注意事项（可选）
            source_session_id: 来源会话 ID（可选，便于溯源）
        """
        conn = get_db()
        cursor = conn.execute(
            """INSERT INTO experiences (scenario, strategy, effect, conditions, source_session_id)
               VALUES (?, ?, ?, ?, ?)""",
            (scenario, strategy, effect, conditions, source_session_id),
        )
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    @staticmethod
    def get_all_experiences(limit: int = 100, sort_by_score: bool = False) -> List[dict]:
        """获取全部经验

        Args:
            limit: 最多返回条数
            sort_by_score: True 时按评分降序（评分同分再按 id 倒序）；
                           False（默认）保持旧行为：按创建时间倒序
        """
        conn = get_db()
        order = ("score DESC, id DESC, created_at DESC" if sort_by_score
                 else "created_at DESC, id DESC")
        rows = conn.execute(
            f"SELECT * FROM experiences ORDER BY {order} LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def search_experiences(query: str, limit: int = 3,
                           max_terms: int = DEFAULT_MAX_TERMS) -> List[dict]:
        """按关键词检索最相关的经验（最多 limit 条）

        计分规则：
        - 主排序因子：关键词命中次数（相关性优先，命中越多越靠前，无命中不返回）；
        - 次要排序因子：用户反馈评分 score（关键词得分相同时，高评分经验优先）；
        - 末位：id 倒序（前两者都相同时较新的经验优先）。

        Args:
            query: 检索查询文本
            limit: 最多返回条数
            max_terms: 参与计分的检索词上限（按出现频次取 Top-N，默认 10；
                       传 0 / 负数表示不截断）。接口向后兼容：不传即用默认值。
        """
        query = (query or "").strip()
        if not query or limit <= 0:
            return []
        # 长输入检索精度优化：按频次排序并截断 Top-N，避免过多 bigram 稀释召回
        terms = _rank_terms(_extract_terms(query), query, max_terms=max_terms)
        if not terms:
            return []
        scored = []
        for exp in ExperienceStore.get_all_experiences(limit=500):
            hay = " ".join([
                str(exp.get("scenario") or ""),
                str(exp.get("strategy") or ""),
                str(exp.get("effect") or ""),
                str(exp.get("conditions") or ""),
            ]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                scored.append((score, exp))
        # 排序：(关键词得分, 用户评分, id) 全部倒序——相关性为主，评分为辅
        scored.sort(key=lambda x: (x[0], x[1].get("score") or 0,
                                   x[1].get("id") or 0), reverse=True)
        return [exp for _, exp in scored[:limit]]

    @staticmethod
    def record_feedback(experience_id: int, feedback: str,
                        dedup_window_seconds: float = 0) -> bool:
        """记录用户对某条经验的"有用/无用"反馈

        Args:
            experience_id: 经验记录 ID
            feedback: "up" 表示有用（score +1），"down" 表示无用（score -1）
            dedup_window_seconds: 防抖窗口（秒）。> 0 时，同一 ID 在窗口内的
                重复反馈会被忽略（直接返回 True，不重复累计评分），用于防止
                用户连续点击重复计分；默认 0 表示不做去重，与历史行为完全一致
                （UI 层调用传入 1 启用防抖）。

        Returns:
            是否反馈生效；feedback 非法或经验不存在时返回 False，
            防抖命中的重复反馈视为已处理、返回 True。
        """
        feedback = (feedback or "").strip().lower()
        if feedback not in VALID_FEEDBACK:
            return False
        delta = 1 if feedback == "up" else -1
        conn = get_db()
        try:
            # 防抖检查：读取最近一次反馈时间，落在窗口内则忽略本次
            if dedup_window_seconds and dedup_window_seconds > 0:
                row = conn.execute(
                    "SELECT last_used_at FROM experiences WHERE id = ?",
                    (experience_id,),
                ).fetchone()
                if row is None:
                    return False  # 经验不存在
                last_used_at = str(row["last_used_at"] or "")
                if last_used_at:
                    try:
                        last_dt = datetime.strptime(last_used_at, "%Y-%m-%d %H:%M:%S")
                        elapsed = (datetime.now() - last_dt).total_seconds()
                        if 0 <= elapsed < dedup_window_seconds:
                            # 时间戳为秒级精度，elapsed 落在 [0, 窗口) 内视为连点
                            return True
                    except ValueError:
                        pass  # 历史数据时间格式异常时不去重，照常记录
            cursor = conn.execute(
                "UPDATE experiences "
                "SET score = score + ?, use_count = use_count + 1, last_used_at = ? "
                "WHERE id = ?",
                (delta, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), experience_id),
            )
            updated = (cursor.rowcount or 0) > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    @staticmethod
    def cleanup_low_score(threshold: int = -3) -> int:
        """淘汰低分经验：删除 score <= threshold 的记录，返回删除数量

        用于手动清理长期被用户标记"无用"的经验；默认阈值 -3。
        """
        try:
            threshold = int(threshold)
        except (ValueError, TypeError):
            threshold = -3
        conn = get_db()
        cursor = conn.execute("DELETE FROM experiences WHERE score <= ?", (threshold,))
        deleted = cursor.rowcount or 0
        conn.commit()
        conn.close()
        return deleted

    @staticmethod
    def find_exact(scenario: str, strategy: str) -> Optional[dict]:
        """精确查找相同「场景+策略」的经验（防重复沉淀），命中返回该记录"""
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM experiences WHERE scenario = ? AND strategy = ? LIMIT 1",
            ((scenario or "").strip(), (strategy or "").strip()),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete_experience(experience_id: int) -> bool:
        """删除指定经验，返回是否存在并删除成功"""
        conn = get_db()
        cursor = conn.execute("DELETE FROM experiences WHERE id = ?", (experience_id,))
        deleted = (cursor.rowcount or 0) > 0
        conn.commit()
        conn.close()
        return deleted

    @staticmethod
    def count_experiences() -> int:
        """当前经验总数（UI 展示用）"""
        conn = get_db()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM experiences").fetchone()
        conn.close()
        return row["cnt"] or 0

    @staticmethod
    def latest_goal_lessons(goal_id: str, eligible_scopes,
                            limit: int = 1) -> List[dict]:
        """按目标取「已回流成经验的复盘教训」里最近的那几条（Phase 4b 的候选查询）

        候选链是**单向链接的读法**（`DESIGN_ANCHORS.md` §一.8）：
        `experiences.decision_id` → `decisions.goal_id`，问的是「哪条经验的来路属于这个目标」，
        不是反过来在 `decisions` 上找经验列——那一列已被定案否掉，库里也没有。

        筛选四环（设计席 2026-10-07 裁决 2 的原文顺序，v1 到此为止）：
        1. `decisions.goal_id` 与传入的一致；
        2. `decisions.decision_scope` **属于** `eligible_scopes` 这个集合——成员判断，
           不写 `>=`：那根跨度序已被 Fix A 连同 `min_scope` 一起删掉，理由见
           `EXPERIENCE_ELIGIBLE_SCOPES` 上方那段注释；
        3. 最近：`ORDER BY e.created_at DESC, e.id DESC`（同一秒落的两条靠 id 分先后）；
        4. 最多 `limit` 条（调用方要 1 条）。

        「未验证的 Assumption 优先」按裁决 2 **v1 不做**：`decision_items` 里没有表示
        验证状态的列，为它加列或加联表都落在冻结清单里，所以这一档不进 SQL。

        `eligible_scopes` 由调用方从 `core.decision_store` 传进来，而不是本模块去 import 它：
        那一份集合是 4a 定下的唯一真源，这里读它但不复制它，免得两处各有一份各改各的。

        本方法是**只读**的，不加守卫：库还没有 `decision_id` 那一列（旧库未迁移）时
        `sqlite3.OperationalError` 照常向上抛，由调用方决定怎么兜
        （`ContextManager` 那侧兜成「这一轮不注入」，见其 `_build_goal_lesson_context`）。

        Returns:
            list[dict]: 命中的经验行（带 `decision_id`／`future_effect` 与该决策的
            `goal_id`／`decision_scope`／`evaluation`），按新→旧排列；没有候选时返回 []
        """
        key = str(goal_id or "").strip()
        scopes = sorted({str(s).strip() for s in (eligible_scopes or ()) if str(s).strip()})
        if not key or not scopes or limit <= 0:
            # 三种「没有候选」的形状一律返回 []：没选目标、集合是空的、不让取条。
            # 判据「无候选经验时注入段为空且不报错」要的就是这个返回，不是抛异常。
            return []
        placeholders = ", ".join("?" for _ in scopes)
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT e.id, e.scenario, e.effect, e.future_effect, e.decision_id, "
                "e.created_at, d.goal_id AS decision_goal_id, d.decision_scope, "
                "d.evaluation, d.title AS decision_title "
                "FROM experiences e "
                "JOIN decisions d ON d.id = e.decision_id "
                "WHERE e.decision_id IS NOT NULL "
                "AND d.goal_id = ? "
                f"AND d.decision_scope IN ({placeholders}) "
                "AND e.future_effect IS NOT NULL AND TRIM(e.future_effect) <> '' "
                "ORDER BY e.created_at DESC, e.id DESC "
                "LIMIT ?",
                [key] + scopes + [int(limit)],
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
