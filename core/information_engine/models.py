"""
Information Engine 数据结构（V0.1）

- Evidence：标准化的单条搜索证据，与 search_evidence 表行一一对应；
- TaskStatus：搜索任务状态常量（pending/running/done/failed，持久化到 search_tasks.status）；
- now_str()：统一时间戳工具（YYYY-MM-DD HH:MM:SS，与项目其他模块一致）；
- 星期换算三件套（`WEEKDAY_LABELS` / `weekday_index_of_timestamp` /
  `weekday_index_of_date_text`）：**"报名截止落在星期几"这条派生轴的唯一实现**（P6-1③）。

为什么星期换算放在本模块（新增，2026-09-13）：
    "截止落在周几"要被两侧同时用到——一手源 provider 手里是 unix 秒
    （`RegistrationDeadline`），面板/账单手里是库里存好的 'YYYY-MM-DD'。
    本模块是 `core` 层里被双方共同依赖的叶子模块（`resource_scout` 已经
    import 它），因此把换算放在这里可以做到**一处实现、两处调用**；
    若各写一份，就是本项目已被咬过两次的"两处手工复制保持一致"。
    换算一律用**本地时区**：用户的机器在本地开机，"周中/周末"只有按本地
    日历才有意义（用 UTC 会在跨日时整体差一天）。

预留字段说明（V0.2+ 使用，本次不填充）(2026-09-12)：
- entities：从证据中抽取的实体（JSON 数组字符串），供"知识图谱整合"使用；
- claims：从证据中抽取的陈述/观点（JSON 数组字符串），供"信息增益停止"评估；
- source_quality：来源质量评分（如站点权威度），供置信度加权。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


def now_str() -> str:
    """当前时间字符串（YYYY-MM-DD HH:MM:SS）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


#: 星期序号（0=周一 … 6=周日，与 `datetime.date.weekday()` 同口径）→ 中文标签
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 周末标记（周六 / 周日）的序号集合——"机器只有周末开机"这条用户事实的唯一编码点
WEEKEND_INDEXES = (5, 6)


def weekday_label(index) -> str:
    """星期序号（0=周一 … 6=周日）→ 中文标签；越界 / 非法返回空串"""
    if not isinstance(index, int) or isinstance(index, bool):
        return ""
    if 0 <= index < len(WEEKDAY_LABELS):
        return WEEKDAY_LABELS[index]
    return ""


def weekday_index_of_timestamp(value) -> Optional[int]:
    """unix 秒 → **本地时区**星期序号（0=周一 … 6=周日）；非法 / 非正数返回 None

    用于一手源：魔搭列表接口给的是 `RegistrationDeadline`（unix 秒）。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value)).weekday()
    except (ValueError, OSError, OverflowError):
        return None


def weekday_index_of_date_text(value) -> Optional[int]:
    """'YYYY-MM-DD'（取前 10 位）→ 星期序号（0=周一 … 6=周日）；非法返回 None

    用于面板/账单：库里 `opportunities.deadline` 存的就是这个定宽格式。
    只认本项目自己写入的格式，不做猜测性解析（与 `_parse_deadline_date` 同款口径）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").weekday()
    except (ValueError, TypeError):
        return None


class TaskStatus:
    """搜索任务状态常量（与 search_tasks.status 存储值一致）"""
    PENDING = "pending"    # 排队中（等待并发名额）
    RUNNING = "running"    # 执行中
    DONE = "done"          # 已完成（证据已写入临时存储）
    FAILED = "failed"      # 失败（所有搜索源均出错）


@dataclass
class Evidence:
    """单条搜索证据（Provider 输出的统一数据结构）

    所有字段与 search_evidence 表列一一对应；task_id / id 为数据库归属字段，
    写入时由 SearchTaskManager 填充，读取时由 EvidenceStore 填充。
    """
    source_url: str = ""                # 来源链接（可访问地址）
    source_name: str = ""               # 来源名称（如 DuckDuckGo / GitHub）
    title: str = ""                     # 标题
    snippet: str = ""                   # 摘要片段
    confidence: float = 0.5             # 置信度（0~1）
    published_at: Optional[str] = None  # 发布日期（字符串，可为空）
    retrieved_at: Optional[str] = None  # 检索时间（默认取当前时间）
    entities: Optional[str] = None      # 预留：实体抽取结果（JSON 字符串） (2026-09-12)
    claims: Optional[str] = None        # 预留：陈述/观点抽取结果（JSON 字符串） (2026-09-12)
    source_quality: Optional[str] = None  # 预留：来源质量评分 (2026-09-12)
    task_id: Optional[int] = None       # 归属搜索任务 ID（写入时填充）
    id: Optional[int] = None            # 数据库主键（读取时填充）
