"""
Note Store - 目标笔记系统存储（v3.7.5 新增）

职责：
- 以 SQLite notes 表作为笔记索引（支持按关键词检索），同时把笔记正文
  落盘为 Markdown 文件（data/goals/{goal_id}/notes/YYYYMMDD_标题.md），
  并在每个目标的笔记目录维护一份 index.md 索引页；
- 供 TaskExecutor 笔记整理处理器写入草稿，供 app.py「📝 笔记」面板
  列表 / 搜索 / 查看使用。

设计说明（与 ExperienceStore / ProfileManager 保持一致）：
- @staticmethod + memory.database.get_db()，不引入新的依赖；
- notes 表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建，
  不修改任何现有表结构，旧库升级零迁移成本；
- 写入顺序：先插数据库行拿到 id → 写文件 → 回填 file_path → 提交；
  文件写失败时回滚，不会出现"有索引无文件"的中间态；
- 同一天同一标题的笔记会自动追加时间后缀（HHMMSS）避免互相覆盖；
- 检索沿用项目惯例：中文相邻双字（bigram）+ 英文单词切分，Python 侧
  计数打分，规模（数百篇内）下足够高效。
"""

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from .database import get_db

# 笔记文件存储根目录（相对项目运行目录，与 GoalManager 的 data/goals 约定一致）
NOTES_ROOT = Path("data") / "goals"

# 文件名中需要替换为连字符的非法字符（Windows 保留字符 + 空白）
_FILENAME_SANITIZE_RE = re.compile(r'[\\/:*?"<>|\s]+')

# index.md / 列表展示中单条摘要的最大长度
PREVIEW_MAX_LEN = 40


def sanitize_title(title: str) -> str:
    """把笔记标题清理为可安全用作文件名的形式（空标题回退 'untitled'）"""
    cleaned = _FILENAME_SANITIZE_RE.sub("-", (title or "").strip()).strip("-.")
    return cleaned or "untitled"


def note_dir_for(goal_id: str) -> Path:
    """某个目标的笔记文件目录：data/goals/{goal_id}/notes/"""
    return NOTES_ROOT / (goal_id or "unknown") / "notes"


def research_dir_for(goal_id: str) -> Path:
    """某个目标的资料搜索结果目录：data/goals/{goal_id}/research_notes/

    由 TaskExecutor 资料搜索处理器使用，与笔记目录同级、互不干扰。
    """
    return NOTES_ROOT / (goal_id or "unknown") / "research_notes"


def build_note_filename(title: str, created_dt: Optional[datetime] = None) -> str:
    """生成笔记文件名：YYYYMMDD_标题.md（同日同名由调用方追加时间后缀）"""
    dt = created_dt or datetime.now()
    return f"{dt.strftime('%Y%m%d')}_{sanitize_title(title)}.md"


def _extract_terms(query: str) -> List[str]:
    """从查询中提取检索词（去重保序）

    规则与 ExperienceStore._extract_terms 一致：
    - 英文/数字词：按非单词字符切分，保留长度 >= 2 的词；
    - 中文片段：生成相邻双字组合（bigram），单字片段保留单字。
    """
    query = (query or "").strip().lower()
    if not query:
        return []
    terms: List[str] = []
    for word in re.split(r"[^\w]+", query):
        if len(word) >= 2 and not re.search(r"[\u4e00-\u9fff]", word):
            terms.append(word)
    for seg in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(seg) == 1:
            terms.append(seg)
        else:
            terms.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _normalize_tags(tags: Optional[Union[str, List[str]]]) -> str:
    """规范化标签为去重、去空、去空格后的逗号分隔字符串

    支持输入形式：None / 逗号分隔字符串 / 列表 / 元组。
    输出：如 "学习,重要,总结"；输入无效时返回空字符串。
    """
    if tags is None:
        return ""
    if isinstance(tags, str):
        items = tags.split(",")
    elif isinstance(tags, (list, tuple)):
        items = [str(t) for t in tags]
    else:
        items = []

    seen = set()
    result = []
    for item in items:
        tag = item.strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return ",".join(result)


def parse_tags(tags_str: str) -> List[str]:
    """从逗号分隔的字符串解析出标签列表（用于 UI 显示）"""
    if not tags_str:
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def render_note_preview(content: str, max_len: int = PREVIEW_MAX_LEN) -> str:
    """取笔记正文第一个非空行作为摘要展示（超长截断）"""
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:max_len] + ("…" if len(line) > max_len else "")
    return "（空笔记）"


def render_index_md(goal_id: str, notes: List[dict]) -> str:
    """渲染某目标笔记目录下的 index.md 索引内容（纯函数，便于单测）"""
    lines = [f"# 📝 目标 {goal_id} 笔记索引", ""]
    lines.append(f"> 共 {len(notes)} 篇 · 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    if not notes:
        lines.append("（暂无笔记）")
        return "\n".join(lines)
    for n in notes:
        fname = Path(n.get("file_path") or "").name or "（未存文件）"
        created = str(n.get("created_at") or "")[:19]
        tags_str = n.get("tags") or ""
        tag_suffix = f" 🏷️ {tags_str}" if tags_str else ""
        lines.append(f"- [{fname}]({fname}) —— {n.get('title')}"
                     f"{f'（{created}）' if created else ''}{tag_suffix}")
        lines.append(f"  {render_note_preview(n.get('content') or '')}")
    return "\n".join(lines)


def _run_async(func, *args, **kwargs):
    """在后台守护线程中执行函数，避免阻塞 UI 主线程。

    RAG 向量索引（模型加载 + 嵌入推理）可能耗时数十秒，
    在笔记保存时异步执行，失败仅打印日志，不向上抛出异常。
    """
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


class NoteStore:
    """目标笔记管理器（SQLite notes 表索引 + Markdown 文件存储）

    所有 RAG 向量操作（index_note / remove_note_from_index）均通过
    _run_async 在后台线程执行，避免模型加载或向量推理阻塞 UI。
    """

    # ========== 写入 ==========

    @staticmethod
    def add_note(goal_id: str, title: str, content: str,
                 tags: Optional[Union[str, List[str]]] = None,
                 async_rag: bool = True) -> int:
        """新增一篇笔记：写数据库索引行 + Markdown 文件 + 刷新 index.md

        Args:
            goal_id: 所属目标 ID（非空）
            title: 笔记标题（非空；同时决定默认文件名）
            content: 笔记正文（Markdown 文本，允许为空串）
            tags: 可选标签，字符串（逗号分隔）或列表；内部规范化为逗号分隔字符串
            async_rag: 是否异步执行 RAG 向量索引；默认 True（避免阻塞 UI）；
                       测试时传入 False 可同步执行便于断言

        Returns:
            新笔记的数据库 ID；参数非法或写文件失败时返回 -1（不抛异常，
            由调用方根据返回值给出友好提示）。
        """
        goal_id = (goal_id or "").strip()
        title = (title or "").strip()
        if not goal_id or not title:
            return -1
        content = content or ""
        tags_str = _normalize_tags(tags)
        now = datetime.now()

        target_dir = note_dir_for(goal_id)
        filename = build_note_filename(title, now)
        # 同日同名防覆盖：追加写入时刻的时分秒
        if (target_dir / filename).exists():
            filename = (f"{now.strftime('%Y%m%d')}_{sanitize_title(title)}"
                        f"_{now.strftime('%H%M%S')}.md")

        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO notes (goal_id, title, content, file_path, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (goal_id, title, content, "", tags_str),
            )
            note_id = cursor.lastrowid

            # 写文件失败 → 整体回滚，不留半截数据
            target_dir.mkdir(parents=True, exist_ok=True)
            file_path = target_dir / filename
            header = (f"# {title}\n\n"
                      f"> 目标：{goal_id}｜创建时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(header + content)

            rel_path = str(file_path).replace("\\", "/")
            conn.execute("UPDATE notes SET file_path = ? WHERE id = ?", (rel_path, note_id))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[NoteStore] 笔记写入失败已回滚: {e}")
            return -1
        finally:
            conn.close()

        NoteStore._refresh_index(goal_id)

        # 异步尝试索引到 RAG 向量库（失败不影响笔记保存）
        def _do_index():
            try:
                from rag.note_indexer import NoteIndexer
                NoteIndexer.index_note(note_id, title, content)
            except Exception as e:
                print(f"[NoteStore] RAG 索引失败（忽略）: {e}")

        if async_rag:
            _run_async(_do_index)
        else:
            _do_index()

        return note_id

    @staticmethod
    def update_note(note_id: int, title: Optional[str] = None,
                    content: Optional[str] = None,
                    tags: Optional[Union[str, List[str]]] = None,
                    async_rag: bool = True) -> bool:
        """更新笔记的标题/正文/标签（None 表示不修改），同步重写文件与 index.md

        Args:
            note_id: 笔记 ID
            title: 新标题（None 表示保留原标题）
            content: 新正文（None 表示保留原正文）
            tags: 新标签，字符串（逗号分隔）或列表；None 表示保留原标签
            async_rag: 是否异步执行 RAG 重索引（先删后加）；默认 True（避免
                       模型加载/向量推理阻塞 UI）；测试时传入 False 可同步
                       执行，便于直接断言 RAG 调用

        Returns:
            是否更新成功（笔记不存在或写文件失败返回 False）。
        """
        note = NoteStore.get_note(note_id)
        if not note:
            return False
        new_title = (title if title is not None else note["title"]) or ""
        new_content = content if content is not None else (note["content"] or "")
        new_tags = _normalize_tags(tags) if tags is not None else (note.get("tags") or "")
        old_path = Path(note.get("file_path") or "")

        conn = get_db()
        try:
            conn.execute(
                "UPDATE notes SET title = ?, content = ?, tags = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_title.strip() or note["title"], new_content, new_tags, note_id),
            )
            # 旧文件存在则原路径覆写（保持链接稳定）；不存在则按新标题重建
            target = old_path if (old_path.name and old_path.exists()) \
                else note_dir_for(note["goal_id"]) / build_note_filename(new_title)
            target.parent.mkdir(parents=True, exist_ok=True)
            header = (f"# {new_title.strip() or note['title']}\n\n"
                      f"> 目标：{note['goal_id']}｜更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            with open(target, "w", encoding="utf-8") as f:
                f.write(header + new_content)
            rel_path = str(target).replace("\\", "/")
            conn.execute("UPDATE notes SET file_path = ? WHERE id = ?", (rel_path, note_id))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[NoteStore] 笔记更新失败已回滚: {e}")
            return False
        finally:
            conn.close()

        NoteStore._refresh_index(note["goal_id"])

        # 异步重新索引到 RAG 向量库（先删后加，保证索引与正文一致）
        # 使用后台线程避免模型加载/向量推理阻塞 UI 主线程
        _title = new_title.strip() or note["title"]

        def _do_reindex():
            try:
                from rag.note_indexer import NoteIndexer
                NoteIndexer.remove_note_from_index(note_id)
                NoteIndexer.index_note(note_id, _title, new_content)
            except Exception as e:
                print(f"[NoteStore] RAG 重索引失败（忽略）: {e}")

        if async_rag:
            _run_async(_do_reindex)
        else:
            _do_reindex()

        return True

    @staticmethod
    def delete_note(note_id: int, async_rag: bool = True) -> bool:
        """删除笔记：移除数据库行与对应文件，并刷新目标 index.md

        Args:
            note_id: 笔记 ID
            async_rag: 是否异步从 RAG 向量库移除索引；默认 True（避免阻塞 UI）；
                       测试时传入 False 可同步执行，便于直接断言 RAG 调用

        Returns:
            是否删除成功（笔记不存在返回 False；文件缺失不影响删除结果）。
        """
        note = NoteStore.get_note(note_id)
        if not note:
            return False
        conn = get_db()
        try:
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
        finally:
            conn.close()
        try:
            path = Path(note.get("file_path") or "")
            if path.name and path.exists() and path.is_file():
                path.unlink()
        except Exception as e:
            print(f"[NoteStore] 笔记文件删除失败（忽略，索引已删除）: {e}")
        NoteStore._refresh_index(note["goal_id"])

        # 异步从 RAG 向量库移除（后台线程，失败不影响数据库删除）
        def _do_remove():
            try:
                from rag.note_indexer import NoteIndexer
                NoteIndexer.remove_note_from_index(note_id)
            except Exception as e:
                print(f"[NoteStore] RAG 删除失败（忽略）: {e}")

        if async_rag:
            _run_async(_do_remove)
        else:
            _do_remove()

        return True

    # ========== 查询 ==========

    @staticmethod
    def get_note(note_id: int) -> Optional[dict]:
        """按 ID 获取单篇笔记；不存在返回 None"""
        conn = get_db()
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_notes_by_goal(goal_id: str, limit: int = 100) -> List[dict]:
        """获取某目标的全部笔记（新在前）"""
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM notes WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
            ((goal_id or "").strip(), limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def search_notes(query: str, goal_id: Optional[str] = None,
                     limit: int = 20, tag: Optional[str] = None) -> List[dict]:
        """按关键词检索笔记（命中标题或正文），最多返回 limit 条

        计分规则：关键词命中次数为主（相关性优先），id 倒序为辅；
        无命中的笔记不返回；query 为空时返回 []（调用方可自行改用列表接口）。
        若 tag 参数非空，则仅返回包含该标签的笔记。

        Args:
            query: 检索文本
            goal_id: 可选，限定在某目标内检索；None 表示全库检索
            tag: 可选，仅返回包含指定标签的笔记（大小写不敏感）
        """
        query = (query or "").strip()
        if not query or limit <= 0:
            return []
        terms = _extract_terms(query)
        if not terms:
            return []
        conn = get_db()
        if goal_id:
            rows = conn.execute(
                "SELECT * FROM notes WHERE goal_id = ?", ((goal_id or "").strip(),)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM notes").fetchall()
        conn.close()

        tag_lower = (tag or "").strip().lower()
        scored = []
        for r in rows:
            # 标签过滤：仅保留包含指定标签的笔记
            if tag_lower:
                stored_tags = [t.strip().lower() for t in (r["tags"] or "").split(",") if t.strip()]
                if tag_lower not in stored_tags:
                    continue
            hay = " ".join([str(r["title"] or ""), str(r["content"] or "")]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                scored.append((score, dict(r)))
        scored.sort(key=lambda x: (x[0], x[1].get("id") or 0), reverse=True)
        return [note for _, note in scored[:limit]]

    @staticmethod
    def search_notes_by_tag(tag: str, goal_id: Optional[str] = None,
                            limit: int = 20) -> List[dict]:
        """按标签筛选笔记（大小写不敏感），最多返回 limit 条

        Args:
            tag: 标签关键词，匹配时进行子字符串包含判断（如输入「学」可匹配「学习」）
            goal_id: 可选，限定在某目标内；None 表示全库检索

        Returns:
            匹配的笔记列表（新在前）；tag 为空返回 []。
        """
        tag_lower = (tag or "").strip().lower()
        if not tag_lower:
            return []
        conn = get_db()
        if goal_id:
            rows = conn.execute(
                "SELECT * FROM notes WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
                ((goal_id or "").strip(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()

        result = []
        for r in rows:
            # 标签匹配：任一存储标签包含搜索关键词（子字符串）即视为匹配
            stored_tags = parse_tags(r["tags"] or "")
            if any(tag_lower in t.lower() for t in stored_tags):
                result.append(dict(r))
                if len(result) >= limit:
                    break
        return result

    @staticmethod
    def get_tags(goal_id: Optional[str] = None) -> List[str]:
        """获取某目标（或全库）下所有已使用的标签（去重排序）

        Args:
            goal_id: 可选，限定范围；None 表示全库

        Returns:
            排序后的标签列表
        """
        conn = get_db()
        if goal_id:
            rows = conn.execute(
                "SELECT tags FROM notes WHERE goal_id = ? AND tags IS NOT NULL AND tags != ''",
                ((goal_id or "").strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tags FROM notes WHERE tags IS NOT NULL AND tags != ''"
            ).fetchall()
        conn.close()

        all_tags = set()
        for r in rows:
            for t in parse_tags(r["tags"] or ""):
                all_tags.add(t)
        return sorted(all_tags)

    @staticmethod
    def count_notes(goal_id: Optional[str] = None) -> int:
        """统计笔记数量（可限定单个目标）"""
        conn = get_db()
        if goal_id:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notes WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM notes").fetchone()
        conn.close()
        return row["cnt"] or 0

    # ========== 内部工具 ==========

    @staticmethod
    def _refresh_index(goal_id: str):
        """重写某目标笔记目录下的 index.md（失败仅打日志，不影响主流程）"""
        try:
            notes = NoteStore.get_notes_by_goal(goal_id, limit=500)
            target_dir = note_dir_for(goal_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            with open(target_dir / "index.md", "w", encoding="utf-8") as f:
                f.write(render_index_md(goal_id, notes))
        except Exception as e:
            print(f"[NoteStore] index.md 刷新失败（忽略）: {e}")


# 模块级默认实例（供偏好实例化风格的调用方使用；NoteStore 本身全静态方法）
note_store = NoteStore()
