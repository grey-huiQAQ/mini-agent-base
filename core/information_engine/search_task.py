"""
搜索任务管理器（Information Engine V0.1 → V0.2）

职责：
- create_task(query) -> task_id：新建任务（status=pending，持久化到 search_tasks 表）；
- run_task(task_id)：同步执行搜索（V0.2 多轮流程：查询改写 → 多轮搜索 →
  证据去重排序 → 信息增益停止 → 写入 search_evidence 表 → 更新状态与摘要）；
- start(task_id) / submit(query)：后台线程执行，通过 threading.BoundedSemaphore
  限制同时运行的任务数（默认最多 3 个），超出时排队等待；
- 状态查询：get_task / list_tasks / get_status / get_running_count /
  wait_for_completion / delete_task。

设计说明：
- 与存储解耦：providers / evidence_store / query_rewriter 均可注入
  （测试用 mock；扩展"知识图谱整合"时读取 evidence.entities/claims）；
- 线程安全：每个线程使用独立 SQLite 连接（get_db() 每次新建），状态更新原子化；
- 单 Provider 失败不拖垮整体：逐个 try/except，全部失败才标记 failed；
- V0.2 新增参数（query_rewriter / dedup_threshold / max_evidence /
  gain_window / gain_source_min / gain_fact_min）均有默认值，向后兼容：
  未配置查询改写时行为与 V0.1 单轮搜索完全一致；
- 剩余预留扩展点（V0.3+）：知识图谱整合（读取 evidence.entities/claims）。 (2026-09-12)
"""

import threading
import time
from typing import Dict, List, Optional

from memory.database import get_db

from .evidence_quality import (
    rank_evidence,
    deduplicate_evidence,
    information_gain_stop,
)
from .evidence_store import EvidenceStore
from .models import Evidence, TaskStatus
from .search_providers import get_default_providers


class SearchTaskManager:
    """深度搜索任务管理器（后台异步执行 + 并发控制 + 状态持久化）"""

    MAX_CONCURRENCY = 3  # 默认最大同时运行任务数（超过时排队等待）
    DEFAULT_LIMIT = 5    # 每个搜索源每轮默认返回的证据条数
    MAX_EVIDENCE = 20    # 默认总证据数上限（达到后提前停止）

    def __init__(self, providers: Optional[list] = None,
                 evidence_store=None,
                 max_concurrency: int = MAX_CONCURRENCY,
                 default_limit: int = DEFAULT_LIMIT,
                 query_rewriter=None,
                 dedup_threshold: float = 0.9,
                 max_evidence: int = MAX_EVIDENCE,
                 gain_window: int = 2,
                 gain_source_min: int = 1,
                 gain_fact_min: int = 2):
        """
        Args:
            providers: 搜索源列表（默认 get_default_providers()；测试可注入 mock）
            evidence_store: 证据存储（默认 EvidenceStore；可注入 mock/自定义实现）
            max_concurrency: 最大并发数（>=1）
            default_limit: 每个搜索源每轮默认的证据条数
            query_rewriter: 查询改写器（V0.2，可选；None 表示不启用改写，只用原始查询）
            dedup_threshold: 证据去重相似度阈值（0~1，默认 0.9）
            max_evidence: 总证据数上限（达到后提前停止，默认 20）
            gain_window: 信息增益停止的连续低增益轮数（默认 2）
            gain_source_min: 新增有效来源数阈值（默认 1）
            gain_fact_min: 新增事实片段数阈值（默认 2）

        说明：所有 V0.2 新增参数均有默认值，既有调用方式完全向后兼容。
        """
        self.providers = providers if providers is not None else get_default_providers()
        self.evidence_store = evidence_store or EvidenceStore
        self.max_concurrency = max(1, int(max_concurrency))
        self.default_limit = max(1, int(default_limit))
        # V0.2：查询改写 / 去重排序 / 信息增益停止参数
        self.query_rewriter = query_rewriter
        self.dedup_threshold = float(dedup_threshold)
        self.max_evidence = max(1, int(max_evidence))
        self.gain_window = max(1, int(gain_window))
        self.gain_source_min = max(0, int(gain_source_min))
        self.gain_fact_min = max(0, int(gain_fact_min))
        # 并发控制：信号量保证同时运行线程数不超过 max_concurrency
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        # 已启动的后台线程登记表（task_id -> Thread），兼作重复启动防护
        self._threads: Dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    # ========== 任务创建与状态持久化 ==========

    def create_task(self, query: str) -> int:
        """创建一个搜索任务（status=pending），返回任务 ID

        Raises:
            ValueError: 查询为空时抛出
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("搜索查询不能为空")
        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO search_tasks (query, status, created_at) "
                "VALUES (?, 'pending', CURRENT_TIMESTAMP)",
                (query,),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _set_status(self, task_id: int, status: str,
                    result_summary: Optional[str] = None) -> None:
        """更新任务状态；进入终态（done/failed）时记录完成时间（保留首次完成时间）"""
        conn = get_db()
        try:
            if status in (TaskStatus.DONE, TaskStatus.FAILED):
                conn.execute(
                    "UPDATE search_tasks SET status = ?, "
                    "result_summary = COALESCE(?, result_summary), "
                    "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                    "WHERE id = ?",
                    (status, result_summary, task_id),
                )
            else:
                conn.execute(
                    "UPDATE search_tasks SET status = ? WHERE id = ?",
                    (status, task_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Optional[dict]:
        """读取任务行（dict）；不存在返回 None"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM search_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_tasks(self, limit: int = 20) -> List[dict]:
        """列出最近任务（按 ID 倒序，最新在前）"""
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM search_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_status(self, task_id: int) -> Optional[str]:
        """查询任务状态（pending/running/done/failed；不存在返回 None）"""
        task = self.get_task(task_id)
        return task["status"] if task else None

    def get_running_count(self) -> int:
        """当前状态为 running 的任务数（实际执行中，不含排队中）"""
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM search_tasks WHERE status = 'running'"
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    # ========== 任务执行 ==========

    # ========== 任务执行（V0.2：查询改写 + 多轮搜索 + 去重排序 + 信息增益停止） ==========

    def _build_sub_queries(self, query: str) -> List[str]:
        """生成搜索子查询列表（V0.2 查询改写）

        未配置 query_rewriter 时返回 [query]（单轮，与 V0.1 行为一致）；
        配置时调用改写器，失败/非法输出由改写器内部降级为 [query]；
        此处再做二次校验：必须包含原始查询（至少一个），最多 4 个。
        """
        if self.query_rewriter is None:
            return [query]
        try:
            sub_queries = self.query_rewriter.rewrite(query)
        except Exception as e:
            print(f"[Search] 查询改写器异常，降级为原始查询: {e}")
            return [query]
        if not isinstance(sub_queries, list):
            return [query]
        cleaned = [str(s).strip() for s in sub_queries if str(s).strip()]
        if not cleaned:
            return [query]
        if query not in cleaned:
            cleaned.insert(0, query)  # 强制保留原始查询
        return cleaned[:4]

    def run_task(self, task_id: int) -> Optional[dict]:
        """同步执行搜索任务（V0.2：查询改写 + 多轮搜索 + 去重排序 + 信息增益停止）

        流程：
        1. 查询改写（可注入 query_rewriter；未配置/失败时降级为仅原始查询）；
        2. 多轮搜索：每轮使用一个子查询运行全部 Provider，合并证据后
           rank_evidence（评分）→ 按 confidence 降序排序 → deduplicate_evidence（去重）；
        3. 信息增益停止：连续 gain_window 轮新增来源数 ≤ gain_source_min 且
           新增事实片段数 ≤ gain_fact_min 时提前停止；子查询用尽或总证据数
           ≥ max_evidence 也停止；
        4. 证据写入 search_evidence 临时存储，更新任务状态与摘要。

        未配置查询改写时等价于 V0.1 的单轮行为，向后兼容。

        Returns:
            执行后的任务行（dict）；任务不存在返回 None
        """
        self._set_status(task_id, TaskStatus.RUNNING)
        task = self.get_task(task_id)
        if not task:
            return None
        query = task.get("query") or ""

        # 1) 查询改写：生成子查询列表（降级安全）
        sub_queries = self._build_sub_queries(query)

        # 2) 多轮搜索 + 合并去重排序
        all_evidence: List[Evidence] = []
        errors = []
        no_gain_streak = 0
        stop_reason = ""

        for _round_idx, sub_query in enumerate(sub_queries, 1):
            round_evidence: List[Evidence] = []
            for provider in self.providers:
                provider_name = getattr(provider, "name", type(provider).__name__)
                try:
                    results = provider.search(sub_query, limit=self.default_limit)
                    for ev in (results or []):
                        if ev is None:
                            continue
                        if not ev.source_url and not ev.title:
                            continue  # 丢弃空证据
                        round_evidence.append(ev)
                except Exception as e:
                    errors.append(f"{provider_name}: {e}")
                    print(f"[Search] 搜索源 {provider_name} 失败（忽略）: {e}")

            # 合并本轮与前几轮证据：先评分排序，再去重（保证顺序稳定）
            before = all_evidence
            merged = rank_evidence(before + round_evidence, query=query)
            merged.sort(key=lambda e: e.confidence, reverse=True)  # 先排序
            merged = deduplicate_evidence(merged, threshold=self.dedup_threshold)
            all_evidence = merged

            # 3) 信息增益停止判定（连续 gain_window 轮增益不足才停止）
            if information_gain_stop(before, all_evidence, window=self.gain_window,
                                     source_min=self.gain_source_min,
                                     fact_min=self.gain_fact_min):
                no_gain_streak += 1
            else:
                no_gain_streak = 0
            if no_gain_streak >= self.gain_window:
                stop_reason = f"连续 {self.gain_window} 轮信息增益不足，提前停止"
                break
            if len(all_evidence) >= self.max_evidence:
                stop_reason = f"证据数达到上限（{self.max_evidence} 条），提前停止"
                break

        # 4) 写入临时存储（单条失败不影响其他证据）
        for ev in all_evidence:
            try:
                self.evidence_store.add_evidence(
                    task_id=task_id,
                    source_url=ev.source_url,
                    source_name=ev.source_name,
                    title=ev.title,
                    snippet=ev.snippet,
                    confidence=ev.confidence,
                    published_at=ev.published_at,
                    retrieved_at=ev.retrieved_at,
                    entities=ev.entities,
                    claims=ev.claims,
                    source_quality=ev.source_quality,
                )
            except Exception as e:
                print(f"[Search] 证据写入失败（忽略单条）: {e}")

        if all_evidence:
            # 汇总来源名称（去重保序）
            sources = []
            seen = set()
            for ev in all_evidence:
                name = ev.source_name or "未知"
                if name not in seen:
                    seen.add(name)
                    sources.append(name)
            summary = f"共收集 {len(all_evidence)} 条证据（来源：{'、'.join(sources)}）"
            if len(sub_queries) > 1:
                summary += f"；本轮使用 {len(sub_queries)} 个子查询"
            if stop_reason:
                summary += f"；{stop_reason}"
            self._set_status(task_id, TaskStatus.DONE, summary)
        elif errors:
            # 所有来源均失败 → 任务失败，保留错误摘要
            self._set_status(task_id, TaskStatus.FAILED, "；".join(errors)[:300])
        else:
            self._set_status(task_id, TaskStatus.DONE, "未找到任何相关证据")
        return self.get_task(task_id)

    # ========== 后台执行与并发控制 ==========

    def start(self, task_id: int) -> bool:
        """后台线程执行任务（受信号量限制：同时最多 max_concurrency 个运行）

        任务须处于 pending 状态才会启动；已启动/运行中/终态返回 False。
        线程以 daemon 方式运行，不会阻塞进程退出。
        """
        thread = threading.Thread(
            target=self._worker, args=(task_id,),
            daemon=True, name=f"search-task-{task_id}",
        )
        with self._lock:
            # _threads 登记表兼作启动防护：同一任务只能注册一次
            if task_id in self._threads or self.get_status(task_id) != TaskStatus.PENDING:
                return False
            self._threads[task_id] = thread
        thread.start()
        return True

    def _worker(self, task_id: int) -> None:
        """后台线程入口：获取并发名额后执行，结束后从登记表移除"""
        try:
            with self._semaphore:
                self.run_task(task_id)
        except Exception as e:
            print(f"[Search] 后台任务 #{task_id} 异常: {e}")
            try:
                self._set_status(task_id, TaskStatus.FAILED, f"后台执行异常: {e}")
            except Exception:
                pass
        finally:
            with self._lock:
                self._threads.pop(task_id, None)

    def submit(self, query: str) -> int:
        """创建任务并立即后台执行（一步操作），返回任务 ID"""
        task_id = self.create_task(query)
        self.start(task_id)
        return task_id

    def wait_for_completion(self, task_id: int, timeout: float = 30.0) -> Optional[dict]:
        """阻塞等待任务到达终态（done/failed），返回最终任务行；超时返回当前行"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get_task(task_id)
            if task and task["status"] in (TaskStatus.DONE, TaskStatus.FAILED):
                return task
            time.sleep(0.05)
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        """删除搜索任务及其全部证据（级联删除，单事务原子提交）

        不可逆操作：先删 search_evidence 中该任务的全部证据，再删 search_tasks
        任务行，两次删除在同一事务内完成，任一步失败整体回滚，不会出现
        "任务没了但证据残留"的中间态。

        Returns:
            是否删除到任务行（任务不存在返回 False）
        """
        conn = get_db()
        try:
            conn.execute("DELETE FROM search_evidence WHERE task_id = ?", (task_id,))
            cursor = conn.execute("DELETE FROM search_tasks WHERE id = ?", (task_id,))
            conn.commit()
            return (cursor.rowcount or 0) > 0
        except Exception as e:
            conn.rollback()
            print(f"[Search] 删除任务 #{task_id} 失败已回滚: {e}")
            return False
        finally:
            conn.close()
