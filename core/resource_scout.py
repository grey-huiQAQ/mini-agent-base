# -*- coding: utf-8 -*-
"""
Resource Scout —— 外部资源机会发现（MVP v0.1 → v0.2 资格过滤 → v0.3 用户画像）

定位：
- 以低成本方式，从外部为学生开发者**发现**有价值的资源机会
  （比赛 / 黑客松 / 免费额度算力 / 课程 / 奖学金资助等）；
- 复用 Information Engine 的搜索源 Provider（DuckDuckGo + GitHub 等），
  只调用其公开接口 SearchProvider.search()，不改动其内部任何逻辑；
- 仅做**初步筛选与展示**：清洗标题 / URL / 摘要、按关键词粗分类 type、
  按 URL 去重后写入 opportunities 表（status='new'）；
- **不自动评分、不自动采取行动**：是否感兴趣由用户在 UI 人工标记
  （new / interested / ignored / favorited），符合"辅助型自主"原则。

v0.2 新增（资格过滤，确定性规则）：
- check_eligibility(title, summary)：按 ELIGIBLE_KEYWORDS / INELIGIBLE_KEYWORDS
  关键词规则返回资格提示（likely_yes / likely_no / unknown），写入
  opportunities.eligibility_hint 列（旧库经 init_db 迁移自动补列，默认 unknown）；
- 资格提示**仅用于 UI 筛选与展示，绝不删除 / 不自动忽略任何机会**，
  用户保留最终决策权；规则无 LLM、零 API 成本。

v0.3 新增（用户画像 + 硬性条件，仍为确定性规则、零 LLM 成本）：
- 新增 user_profile 表（单行 id=1，默认：高中生 / 无学校邮箱 / 地区中国），
  读写函数在 memory.database（get_user_profile / update_user_profile）；
- check_eligibility(title, summary, profile=None) 接受画像，优先判断硬性条件：
  要求学校邮箱而画像无 → likely_no；年龄门槛与画像年龄冲突 → likely_no；
  限定地区与画像地区不符 → likely_no；仅面向某一学生类型与画像不符 →
  likely_no。硬性条件满足后（或未传画像）再执行原有关键词规则；
- 返回二元组 (eligibility_hint, eligibility_reason)，reason 为中文判断依据，
  写入 opportunities.eligibility_reason 列（旧库迁移自动补列，默认 ''）；
- evidence_to_opportunity / scan_once 落库时自动套用当前画像；
  recompute_eligibility() 可在用户修改画像后重算全部存量机会的资格提示
  （UI「保存画像」后调用）；仅作提示，绝不删除 / 不自动忽略任何机会。

v0.4 新增（相关性粗筛，为 AI 深度筛选打底；仍确定性规则、零 LLM 成本）：
- check_relevance(title, summary) 纯函数：用硬编码中英文关键词库
  （STRONG / WEAK / IRRELEVANT / SOFT_CONDITION 四类常量，英文按词边界
  匹配、中文直接包含）评估机会与"学生开发者"主题的相关性，返回
  (relevance_status, relevance_score, matched_positive, matched_negative)：
  强相关命中 → relevant（0.4/个）；仅弱相关命中 → unknown（0.2）；
  不相关命中且无强相关 → irrelevant（分数 0）；软条件（费用 / 组队 /
  材料等）不直接淘汰，只降 0.1 分并在 UI 提示；
- relevance 结果随机会落库（relevance_status / relevance_score /
  matched_keywords JSON），供 UI 展示与 AI 深度筛选挑选候选；
- recompute_relevance() 可对**全部存量机会**按其 title + summary 重新执行
  相关性粗筛并更新三列（relevance_status / relevance_score /
  matched_keywords），用于修复 v0.4 落库前的旧记录（迁移默认
  0.0 / unknown）或关键词库更新后的存量机会；UI「🔄 重新计算相关性」
  手动触发（仅更新相关性三列，不触碰资格/状态/画像等其它字段）；
- AI 深度筛选在独立模块 core/ai_filter.py（手动按钮触发，不自动执行），
  仅对"粗筛分数 ≥ 0.3 且资格非 likely_no"的未筛选机会调用 LLM。
- 「资源」格（P7 批次 1a）：build_resource_digest 把本地机会库按「临期 /
  无时点长期挂着」两段筛成一段 Markdown。**零网络、零 LLM、零写库**——
  它存在的理由就是"用户不在时替他看一眼本地已经抓到的东西"，所以不能依赖
  他在场，也不能依赖网络可用。

设计说明（与 EvidenceStore / task_template 保持一致）：
- 数据库统一走 memory.database.get_db()，每次调用独立连接（线程安全），
  全部 try/finally conn.close()；
- 表由 database.init_db() 的 CREATE TABLE IF NOT EXISTS 自动创建（纯新增表），
  新列使用迁移函数 _add_column_if_not_exists（不直接改建表 SQL）；
- 扫描中的网络调用带**超时保护**（单次调用包一层 future.result(timeout)），
  任何 Provider 失败 / 超时 / 空结果都只影响单条查询，不中断整轮扫描；
- URL 去重：轮内用内存集合 + 库内 source_url UNIQUE 约束（INSERT OR IGNORE）
  双保险，重复扫描幂等；无 URL 的结果视为不可用机会直接跳过，
  保证唯一约束不会因空串冲突。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from memory.database import (
    get_db,
    get_user_profile,
    DEFAULT_USER_PROFILE,
    VALID_STUDENT_TYPES,
)

from .information_engine.models import (
    Evidence,
    now_str,
    weekday_index_of_date_text,
    weekday_label,
    WEEKEND_INDEXES,
)
from .information_engine.search_providers import (
    SearchProvider,
    get_default_providers,
)


# ---------- 常量 ----------

class OpportunityStatus:
    """机会状态常量（与 opportunities.status 存储值一致）"""
    NEW = "new"            # 新发现（未处理）
    INTERESTED = "interested"  # 感兴趣
    IGNORED = "ignored"    # 忽略
    FAVORITED = "favorited"    # 收藏

    VALID = (NEW, INTERESTED, IGNORED, FAVORITED)


#: 粗分类的中文展示标签（UI 与渲染使用）
TYPE_LABELS = {
    "hackathon": "黑客松",
    "competition": "竞赛",
    "credit": "额度/算力",
    "course": "课程",
    "scholarship": "奖学金/资助",
    "other": "其他",
}

#: 状态的中文展示标签
STATUS_LABELS = {
    OpportunityStatus.NEW: "🆕 新发现",
    OpportunityStatus.INTERESTED: "😍 感兴趣",
    OpportunityStatus.IGNORED: "🙈 已忽略",
    OpportunityStatus.FAVORITED: "⭐ 已收藏",
}


class EligibilityHint:
    """资格提示常量（与 opportunities.eligibility_hint 存储值一致）

    由确定性关键词规则给出，**仅作提示**：不删除、不自动忽略任何机会，
    最终决策权始终在用户（UI 中仅用于筛选与展示）。
    """
    LIKELY_YES = "likely_yes"   # 可能符合（命中学生/高校/全球/线上等开放关键词）
    LIKELY_NO = "likely_no"     # 可能不符合（命中在职/专业人士/年龄门槛等限定词）
    UNKNOWN = "unknown"         # 未知（无关键词命中）

    VALID = (LIKELY_YES, LIKELY_NO, UNKNOWN)


#: 资格提示的中文展示标签（UI 与渲染使用）
ELIGIBILITY_LABELS = {
    EligibilityHint.LIKELY_YES: "✅ 可能符合",
    EligibilityHint.LIKELY_NO: "❌ 可能不符",
    EligibilityHint.UNKNOWN: "❓ 未知",
}


def effective_hint(rule_hint, ai_hint) -> str:
    """两侧一致的保守派生值（**派生、不落库**，供 UI 默认视图使用）

    动机（2026-09-13）：`check_eligibility` 的疑似误报率高，根源是
    ELIGIBLE_KEYWORDS 里 `student` / `online` / `global` 这类词**不可证伪**
    ——拒绝型活动也这么写，所以"命中即 likely_yes"几乎必然把未知判成符合。
    规则轴与 AI 轴是两个独立信号，各自都会误判；本函数只改**两轴合并口径**，
    不动任何一侧的打分表，是最小改动：**降级任一信号即可、升级需两个独立
    信号同意**。

    规则（确定性，无 LLM 成本）：
    - 任一为 likely_no → likely_no（任一侧说不符合就认为不符合）；
    - 仅当两侧**都**显式 likely_yes → likely_yes；
    - 其余（含任一侧为 None / 空 / 未筛选）→ unknown。

    注意 NULL 语义：`ai_eligibility` 列在**未做 AI 深度筛选**时为 NULL，列默认
    值也是 unknown，二者在此都按"未同意"处理 → unknown。这是刻意的保守取向：
    只有一侧说 yes 不足以升级，与"宁缺毋滥、绝不自动忽略任何机会"的不变量一致。

    Args:
        rule_hint: opportunities.eligibility_hint（规则轴）
        ai_hint: opportunities.ai_eligibility（AI 轴，可为 None）

    Returns:
        likely_yes / likely_no / unknown
    """
    if rule_hint == EligibilityHint.LIKELY_NO or ai_hint == EligibilityHint.LIKELY_NO:
        return EligibilityHint.LIKELY_NO
    if (rule_hint == EligibilityHint.LIKELY_YES
            and ai_hint == EligibilityHint.LIKELY_YES):
        return EligibilityHint.LIKELY_YES
    return EligibilityHint.UNKNOWN

#: 资格过滤"准入证据"关键词（确定性启发式，可扩展；匹配 title + summary）(2026-09-13)
#: **只有这一张表能抬升到 likely_yes。**
#: 收录判据（"能否证伪"）：一个词只有当"若这条机会其实不接受你，这句话有较大概率
#: 不会出现"时，才算准入证据。据此原表（41 词）按三分法拆开——
#:   · 本表 A 类：可证伪的准入证据（写了就是明确说欢迎你这类人）；
#:   · NEUTRAL_KEYWORDS B 类：任何公开活动都会写的词，拒绝型活动也写，
#:     故**不可证伪**，保留展示但不再计分；
#:   · C 类：学段错配词，已移入 INELIGIBLE_KEYWORDS（见该表注释）。
#: 语义：面向青少年 / 高中生 / 零基础 / 不限年龄等**明确**开放表述。
#: 为什么这次是拆表而不是扩表：原表把 `student` / `online` / `global` 这类
#: 不可证伪词与真实准入证据混在一起，"命中即 likely_yes"，于是 27/36 行被判可能
#: 符合而 42 次命中里没有一次是真实准入证据（详见 PROJECT_STATUS §五.21）。
#: 这是**方向性错误**，不是词表不全，补词救不回来。
ELIGIBLE_KEYWORDS = [
    # 英文（含常见变体，如 high-school）
    "high school", "high-school", "beginner", "teens", "all ages",
    "open to all", "open for all", "everyone", "anyone", "13+",
    # 中文
    "高中生", "中学生", "青少年", "初学者", "零基础",
    "不限年龄", "任何人", "所有人",
]

#: 资格过滤"中性词"（保留展示，**不参与分数/判定**）(2026-09-13)
#: 语义：学生 / 线上 / 全球 / 公开报名等泛化表述。它们出现在几乎所有公开活动的
#: 文案里，**包括明确不接受你的活动**（"International students only" 也含
#: "international"），因此不具备证伪能力，不能当准入证据。
#: 这些词不会消失：它们在**相关性轴**（WEAK_RELEVANCE_KEYWORDS / check_relevance）
#: 里照旧命中并写进 `matched_keywords` 供 UI 展示——两条轴各管各的，本表只负责
#: 让资格轴不再被它们抬升。
NEUTRAL_KEYWORDS = [
    # 英文
    "student", "students", "online", "remote", "global", "worldwide",
    "international",
    # 中文
    "学生", "学生群体", "面向学生", "在校", "线上", "远程",
    "全球", "国际", "公开报名",
]

#: 资格过滤"可能不符合"关键词（确定性启发式；冲突时以不符合为准，宁缺毋滥）
#: 语义：限定在职 / 专业人士 / 特定员工 / 年龄门槛 / 仅线下 / **学段错配**等
#: 学段错配说明（2026-09-13）：`university` / `college` / `undergraduate` /
#: `campus` / `academic` / `大学生` / `高校` 原在 ELIGIBLE_KEYWORDS 里，会给一个
#: 高中生铸 likely_yes——这与 `_detect_student_type_restrictions` 的学段逻辑
#: **直接矛盾**，是方向性错误。用户是 high_school 且无学校邮箱，"仅高校/大学生"
#: 对他就是硬门槛。因结果只是提示 + 可展开折叠、不删数据，保守方向符合
#: "宁缺毋滥、绝不自动忽略"的不变量。
INELIGIBLE_KEYWORDS = [
    # 英文
    "graduate", "graduates", "professional", "professionals", "employee",
    "employees", "staff only", "members only", "18+", "21+", "over 18",
    "over 21", "aged 18", "in-person",
    # 英文·学段错配
    "university", "college", "undergraduate", "campus", "academic",
    # 中文
    "在职", "从业人员", "专业人士", "员工", "内部员工", "仅限线下",
    "线下参加", "须年满18", "年满18", "18岁以上",
    # 中文·学段错配
    "大学生", "高校",
]

# ---------- v0.4 相关性粗筛关键词库（确定性规则，人工整理，可扩展） ---------- (2026-09-12)
# 用途：check_relevance 评估机会是否与学生开发者主题相关，先粗筛排除无关
# 领域（如物理竞赛）的噪声，再对高分机会做 AI 深度筛选（core/ai_filter.py）。
# 约定：英文词一律按**词边界**匹配（正则 (?<![a-z0-9])…(?![a-z0-9])），避免
# art/smart、us/campus 这类子串误伤；中文词无词边界，直接包含匹配。

#: 相关性状态常量
class RelevanceStatus:
    """相关性状态（与 opportunities.relevance_status 存储值一致）"""
    RELEVANT = "relevant"        # 相关（命中强相关关键词）
    IRRELEVANT = "irrelevant"    # 不相关（命中不相关关键词且无强相关）
    UNKNOWN = "unknown"          # 未知（仅弱相关或未命中）

    VALID = (RELEVANT, IRRELEVANT, UNKNOWN)


#: 相关性状态的中文展示标签（UI 与渲染使用）
RELEVANCE_LABELS = {
    RelevanceStatus.RELEVANT: "✅ 相关",
    RelevanceStatus.IRRELEVANT: "🚫 不相关",
    RelevanceStatus.UNKNOWN: "❓ 未知",
}

#: 强相关关键词（命中即视为机会与学生开发者主题强相关，每个 +0.4）
#: 语义：编程 / 黑客松 / 软件开发 / 开源 / AI / 数据科学 / 云与算力 /
#:       计算机类课程竞赛等直接面向开发者能力的载体
STRONG_RELEVANCE_KEYWORDS = [
    # 英文（词边界匹配）
    "hackathon", "programming", "coding", "developer", "developers",
    "software", "open source", "github", "machine learning",
    "deep learning", "artificial intelligence", "data science",
    "kaggle", "cloud", "gpu", "api", "llm", "ai",
    "web development", "app development", "computer science",
    "free credits",
    # 中文（直接包含匹配）
    "黑客松", "编程", "程序设计", "软件开发", "开发者", "开源",
    "人工智能", "机器学习", "深度学习", "大模型", "数据科学",
    "云计算", "算力", "免费额度", "编程竞赛", "计算机",
]

#: 弱相关关键词（可能相关但不足以单独判定，每个 +0.2；只命中弱相关 → unknown）
#: 语义：学生 / 高校 / 奖学金 / 课程培训 / 泛竞赛或大赛 / 科技数字设计等
WEAK_RELEVANCE_KEYWORDS = [
    # 英文（词边界匹配）
    "student", "students", "college", "university", "campus", "academic",
    "scholarship", "grant", "fellowship", "internship", "tech", "technology",
    "digital", "computer", "internet", "course", "training", "competition",
    "contest", "challenge", "teens", "design", "robotics", "startup",
    # 中文（直接包含匹配）
    "学生", "大学生", "中学生", "高中生", "在校", "高校", "青少年",
    "奖学金", "资助", "课程", "教程", "培训", "竞赛", "比赛", "大赛",
    "挑战赛", "科技", "技术", "数字", "互联网", "设计", "机器人", "创业",
]

#: 不相关关键词（命中且无强相关 → irrelevant，分数 0；有强相关时强相关优先）
#: 语义：与学生开发者主题明显无关的领域（理科竞赛、医学、文科艺术体育、
#:       财会考证等——默认扫描可能误带回这类噪声，先粗筛排除）
IRRELEVANT_KEYWORDS = [
    # 英文（词边界匹配；art 用词边界避免命中 smart/start 等词内的 art）
    "physics", "chemistry", "biology", "medicine", "medical", "nursing",
    "pharmacy", "literature", "philosophy", "history", "accounting",
    "art", "arts", "artist", "music", "musical", "dance", "painting",
    "poetry", "sports", "athletics",
    # 中文（直接包含匹配）
    "物理", "化学", "生物", "医学", "护理", "药学", "口腔", "农学",
    "兽医", "天文", "历史", "文学", "哲学", "音乐", "美术", "艺术",
    "舞蹈", "体育", "会计", "司法考试", "公务员", "幼教", "学前教育",
]

#: 软条件关键词（费用 / 组队 / 材料等，**不淘汰**，仅分数 -0.1 并在 UI 提示）
SOFT_CONDITION_KEYWORDS = [
    # 英文（词边界匹配）
    "entry fee", "registration fee", "application fee", "subscription fee",
    "paid", "team of", "in teams", "team required", "submission materials",
    # 中文（直接包含匹配）
    "收费", "付费", "报名费", "参赛费", "申请费", "押金",
    "组队", "团队报名", "申请材料", "报名材料", "提交材料",
]

#: 粗分类关键词规则（顺序即优先级：先命中先归类；title + summary 一起匹配）
CLASSIFY_RULES = [
    # 黑客松 / hackathon
    ("hackathon", ["hackathon", "黑客松", "hack day", "hackathon 2026"]),
    # 竞赛 / 比赛（含数据科学竞赛、编程挑战、奥林匹克）
    ("competition", ["competition", "contest", "challenge", "kaggle", "olympiad",
                     "竞赛", "比赛", "大赛", "挑战赛", "杯赛"]),
    # 免费额度 / 算力 / 云资源（学生开发者最常见诉求）
    ("credit", ["credit", "credits", "gpu", "cloud", "aws", "azure", "api 额度",
                "免费额度", "算力", "云资源", "免费云", "开发者额度"]),
    # 课程 / 教程 / 在线学习
    ("course", ["course", "coursera", "mooc", "edx", "tutorial", "training",
                "课程", "教程", "公开课", "免费学习", "证书课程"]),
    # 奖学金 / 资助 / 研究经费
    ("scholarship", ["scholarship", "grant", "fellowship", "funding", "bursary",
                     "奖学金", "资助", "助学金", "经费"]),
]

#: 默认扫描查询集（可配置；中英混合，覆盖各搜索源偏好）
#: 2026-09-13 删除 "GitHub student developer pack benefits"：GitHub Student Pack 及
#: 其转载整类卡学校邮箱（用户 has_school_email=0），长期只能产出误报，实测该查询
#: 产出的簇全部落在被折叠组内。
#: 2026-09-13 删除 "free online course certificate 2026"：产出的是 SEO 证书农场站
#: （免费在线证书聚合页），不是可报名的一手机会，属长期噪声。
#: 去向：这两个缺口由子项 1 新增的魔搭一手源覆盖（一手接口给结构化活动目录，
#: 不依赖搜索引擎转载），故本次是"换源"而不是单纯减查询。
DEFAULT_QUERIES = [
    "student hackathon 2026 registration free",
    "free GPU credits for students developers",
    "大学生 竞赛 报名 2026 计算机",
    "student developer scholarship grant 2026",
]

#: 单条结果摘要入库最大长度（避免冗长摘要撑爆列表）
SUMMARY_MAX_LEN = 500


# ---------- 数据结构 ----------

@dataclass
class Opportunity:
    """一条外部资源机会（对应 opportunities 表行）

    字段与表一一对应：type 为粗分类（见 TYPE_LABELS），status 为人工状态
    （见 OpportunityStatus），beneficiary 为受益对象（MVP 统一 'user'）；
    eligibility_hint 为确定性规则给出的资格提示（见 EligibilityHint），
    eligibility_reason 为该提示的中文判断依据（结合用户画像的硬性条件 /
    关键词命中说明），仅作筛选/展示提示，不自动忽略任何机会。
    deadline 为报名截止日 'YYYY-MM-DD'（P4-11）；**仅一手源提供**，空串表示
    "未知"而非"无报名截止日"。
    """
    title: str
    source_url: str
    type: str = "other"
    summary: str = ""
    beneficiary: str = "user"
    status: str = OpportunityStatus.NEW
    notes: str = ""
    confidence: float = 0.5
    eligibility_hint: str = EligibilityHint.UNKNOWN
    eligibility_reason: str = ""
    relevance_status: str = RelevanceStatus.UNKNOWN
    relevance_score: float = 0.0
    matched_keywords: str = "[]"
    deadline: str = ""
    discovered_at: str = ""
    id: Optional[int] = None


# ---------- v0.3 用户画像硬性条件（确定性规则，无 LLM 成本） ----------

#: 学生类型的中文展示标签（UI 下拉 / 资格原因文案使用）
STUDENT_TYPE_LABELS = {
    "high_school": "高中生",
    "undergraduate": "大学生（本科）",
    "graduate": "研究生",
    "other": "其他/在职",
}

#: 学生类型"限定语境"中的类型写法（命中即视为该机会限定该类型）
_STUDENT_TYPE_WORDS = {
    "high_school": ["高中生", "中学生", "高中在读", "高中",
                    "high school", "high school students",
                    "secondary school"],
    "undergraduate": ["大学生", "本科生", "本科在读", "在校大学生",
                      "undergraduate", "undergraduates", "undergrad",
                      "college student", "college students",
                      "university student", "university students"],
    "graduate": ["研究生", "硕士", "博士生", "博士",
                 "graduate", "graduates", "postgraduate",
                 "graduate student", "graduate students",
                 "phd", "ph.d."],
}

#: 地区限定中的地区写法（顺序即匹配优先级：先命中先判定；
#: 中国香港 / 中国台湾 须排在 中国 之前，避免用户地区"中国香港"被误归为中国）
_REGION_WORDS = {
    "中国香港": ["香港", "hong kong"],
    "中国台湾": ["台湾", "taiwan"],
    "中国": ["中国", "中国大陆", "内地", "china", "mainland"],
    "美国": ["美国", "usa", "united states", "america", "us"],
    "英国": ["英国", "uk", "united kingdom", "britain"],
    "日本": ["日本", "japan"],
    "新加坡": ["新加坡", "singapore"],
    "加拿大": ["加拿大", "canada"],
    "澳大利亚": ["澳大利亚", "australia"],
}

#: 限定语境标记（出现在地区词 / 学生类型词附近 ±窗口内才视为"硬性限定"；
#: 与"面向"等宽泛词保持谨慎：无标记的直接提及时不判为硬性条件）
_LIMIT_MARKERS = (
    "仅限", "只限", "须为", "限于", "面向",
    "only", "exclusive to", "restricted to", "open only to",
    "residents", "citizens", "based in", "located in",
)

#: 机会"要求学校/教育邮箱"的英文正则片段
_EMAIL_REQUIRED_PATTERNS = [
    re.compile(r"\b\.edu\b"),
    re.compile(r"\b(?:school|college|university|campus|student|academic)"
               r"\s+email\b"),
    re.compile(r"\bemail\s+(?:address\s+)?(?:issued|provided|ending)"
               r"\b[^\n]{0,40}\.edu\b"),
]

#: 机会"要求学校/教育邮箱"的中文片段
_EMAIL_REQUIRED_CN_WORDS = (
    "学校邮箱", "学生邮箱", "教育邮箱", "大学邮箱",
    "校园邮箱", "学号邮箱", "edu邮箱", "edu 邮箱",
)

#: 年龄"下限"门槛（年满 X / X+ / 18 岁以上……）
_AGE_MIN_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+"),
    re.compile(r"\b(?:age|aged)\s+(?:of\s+)?(\d{1,2})\b"),
    re.compile(r"\bmust\s+be\s+(?:at\s+least\s+)?(\d{1,2})\b"),
    re.compile(r"\b(?:over|above)\s+(\d{1,2})\b"),
    re.compile(r"年满\s*(\d{1,2})"),
    re.compile(r"须\s*年满\s*(\d{1,2})"),
    re.compile(r"(\d{1,2})\s*岁\s*(?:以上|及以上)"),
]

#: 年龄"上限"门槛（under X / X 岁以下）
_AGE_MAX_PATTERNS = [
    re.compile(r"\bunder\s+(\d{1,2})\b"),
    re.compile(r"(\d{1,2})\s*岁\s*以下"),
]

#: 年龄"区间"门槛（X 至 Y 岁）
_AGE_RANGE_PATTERNS = [
    re.compile(r"\b(?:age|aged)\s+between\s+(\d{1,2})\s+and\s+(\d{1,2})\b"),
    re.compile(r"\baged\s+(\d{1,2})\s*[-–]\s*(\d{1,2})\b"),
    re.compile(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*岁"),
]


# ---------- 纯函数 ----------

def _clean_text(text: str) -> str:
    """清洗文本：去掉首尾空白并把内部连续空白折叠为单个空格"""
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_profile(profile=None) -> dict:
    """把任意画像输入规整为统一结构（缺省字段用默认值，容忍多余键）

    兼容 memory.database.get_user_profile() 的返回值与测试传入的
    部分 dict（如 {"has_school_email": False, "age": 16}）。

    Returns:
        {"age": int|None, "student_type": str,
         "has_school_email": bool, "region": str}
    """
    p = dict(DEFAULT_USER_PROFILE)
    if isinstance(profile, dict):
        for key in DEFAULT_USER_PROFILE:
            if key in profile:
                p[key] = profile[key]
    age = p.get("age")
    try:
        age_val = None if age in (None, "") else int(float(age))
        p["age"] = age_val if (age_val or 0) > 0 else None
    except (TypeError, ValueError):
        p["age"] = None
    if p.get("student_type") not in VALID_STUDENT_TYPES:
        p["student_type"] = DEFAULT_USER_PROFILE["student_type"]
    email_val = p.get("has_school_email")
    if isinstance(email_val, str):
        email_val = email_val.strip().lower() in ("1", "true", "yes", "y",
                                                  "是", "有")
    p["has_school_email"] = bool(email_val)
    region = (p.get("region") or "").strip() or DEFAULT_USER_PROFILE["region"]
    p["region"] = region
    return p


def _truncate_phrase(text: str, limit: int = 24) -> str:
    """截断证据片段（太长时加省略号）"""
    text = _clean_text(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _detect_email_requirement(text: str):
    """机会文本是否"要求学校/教育邮箱"；命中返回片段（用于原因展示）

    Args:
        text: 已归一化小写文本（title + summary）
    """
    for pat in _EMAIL_REQUIRED_PATTERNS:
        m = pat.search(text)
        if m:
            return _truncate_phrase(m.group(0))
    for word in _EMAIL_REQUIRED_CN_WORDS:
        if word in text:
            return word
    return None


def _find_limited_token(text: str, token: str, window: int = 14):
    """token 是否出现在"限定语境"（±window 内存在 _LIMIT_MARKERS 标记）

    - 英文词按词边界匹配（避免 campus→us、because→us 这类子串误伤）；
    - 中文词按子串匹配（中文无词边界概念）。
    Returns: 覆盖"限定标记 + token"的紧凑片段（用于原因展示），或 None
    """
    if token.isascii():
        pattern = re.compile(
            rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])")
    else:
        pattern = re.compile(re.escape(token))
    for m in pattern.finditer(text):
        lo = max(0, m.start() - window)
        hi = min(len(text), m.end() + window)
        context = text[lo:hi]
        hit_marker = next((mk for mk in _LIMIT_MARKERS if mk in context), None)
        if hit_marker:
            marker_idx = text.find(hit_marker, lo, hi)
            if marker_idx >= 0:
                start = min(m.start(), marker_idx)
                end = max(m.end(), marker_idx + len(hit_marker))
                return _truncate_phrase(text[start:end])
            return text[m.start():m.end()]
    return None


def _region_key_of(region_text: str):
    """把用户画像的地区自由文本归类到 _REGION_WORDS 的某个键（未识别返回 None）"""
    low = (region_text or "").lower()
    for region_key, words in _REGION_WORDS.items():
        if any(word in low for word in words):
            return region_key
    return None


def _detect_region_restrictions(text: str) -> dict:
    """检测文本中的地区硬性限定，返回 {地区键: 命中片段}（空 dict = 无限定）

    中文写法就近匹配限定标记（仅限美国 / 美国居民 / 面向美国……）；
    英文写法匹配常见限定句式（only for X / X residents / residents of X），
    避免把正文里泛泛提到的国名误判为"限定地区"。
    """
    found = {}
    for region_key, words in _REGION_WORDS.items():
        for word in words:
            if word.isascii():
                esc = re.escape(word)
                patterns = (
                    rf"(?<![a-z0-9])(?:only for|open only to|restricted to|"
                    rf"exclusive to)\s+(?:the\s+)?{esc}(?![a-z0-9])",
                    rf"(?<![a-z0-9]){esc}\s+(?:residents|citizens)(?![a-z0-9])",
                    rf"(?<![a-z0-9])(?:residents|citizens)\s+of\s+"
                    rf"(?:the\s+)?{esc}(?![a-z0-9])",
                )
                for pat in patterns:
                    m = re.search(pat, text)
                    if m:
                        found.setdefault(region_key,
                                         _truncate_phrase(m.group(0)))
                        break
            else:
                phrase = _find_limited_token(text, word)
                if phrase:
                    found.setdefault(region_key, phrase)
    return found


def _detect_student_type_restrictions(text: str) -> dict:
    """检测文本中的学生类型硬性限定，返回 {类型键: 命中片段}（空 dict = 无限定）"""
    found = {}
    for type_key, words in _STUDENT_TYPE_WORDS.items():
        for word in words:
            phrase = _find_limited_token(text, word)
            if phrase:
                found.setdefault(type_key, phrase)
    return found


def _extract_age_bounds(text: str) -> dict:
    """从文本提取显式年龄门槛

    Returns:
        {"min": int|None, "max": int|None,
         "spans": [(start, end), ...], "evidence": str}
    """
    min_age = max_age = None
    spans = []
    evidence = ""

    def _note(span, snippet):
        nonlocal evidence
        spans.append(span)
        if not evidence:
            evidence = _truncate_phrase(snippet)

    for pat in _AGE_MIN_PATTERNS:
        for m in pat.finditer(text):
            value = int(m.group(1))
            min_age = max(min_age, value) if min_age is not None else value
            _note(m.span(), m.group(0))
    for pat in _AGE_MAX_PATTERNS:
        for m in pat.finditer(text):
            value = int(m.group(1)) - 1  # under 18 → 不超过 17
            max_age = min(max_age, value) if max_age is not None else value
            _note(m.span(), m.group(0))
    for pat in _AGE_RANGE_PATTERNS:
        for m in pat.finditer(text):
            lo, hi = int(m.group(1)), int(m.group(2))
            min_age = max(min_age, lo) if min_age is not None else lo
            max_age = min(max_age, hi) if max_age is not None else hi
            _note(m.span(), m.group(0))
    return {"min": min_age, "max": max_age, "spans": spans,
            "evidence": evidence or ""}


def _strip_spans(text: str, spans) -> str:
    """删除文本中的若干片段（用于把"已判定的年龄门槛"从关键词扫描中剔除，
    避免「18+」等数字门槛在画像年龄已满足时仍被 INELIGIBLE 关键词误杀）"""
    if not spans:
        return text
    ordered = sorted(spans)
    parts = []
    pos = 0
    for start, end in ordered:
        if start >= pos:
            parts.append(text[pos:start])
            pos = max(pos, end)
    parts.append(text[pos:])
    return "".join(parts)


def _check_profile_hard(blob: str, profile: dict):
    """按用户画像检查资格硬性条件（blob 为已归一化小写文本）

    Returns:
        (block_reason, scan_text)：block_reason 非空表示硬性条件不满足
        （对应 likely_no）；scan_text 为后续关键词扫描用文本（年龄门槛
        已判定的片段会被剔除，其余情况与输入一致）。
    """
    # 1) 学校邮箱（优先级最高：许多学生补贴实际要求大学 / 学校邮箱）
    email_phrase = _detect_email_requirement(blob)
    if email_phrase and not profile.get("has_school_email"):
        return (f"要求学校/教育邮箱（命中「{email_phrase}」），"
                "但用户画像显示无学校邮箱"), blob

    # 2) 年龄：文本有显式门槛且画像年龄已知时按年龄比较
    age = profile.get("age")
    age_info = _extract_age_bounds(blob)
    has_bounds = (age_info["min"] is not None or age_info["max"] is not None)
    if age is not None and has_bounds:
        phrase = (f"（命中「{age_info['evidence']}」）"
                  if age_info["evidence"] else "")
        if age_info["min"] is not None and age < age_info["min"]:
            return (f"年龄门槛：需年满 {age_info['min']} 岁{phrase}，"
                    f"画像年龄 {age} 岁不符合"), blob
        if age_info["max"] is not None and age > age_info["max"]:
            return (f"年龄上限：需不超过 {age_info['max']} 岁{phrase}，"
                    f"画像年龄 {age} 岁不符合"), blob
        blob = _strip_spans(blob, age_info["spans"])

    # 3) 地区：文本限定到与画像地区不同的地区 → 不符
    region_hits = _detect_region_restrictions(blob)
    if region_hits:
        user_key = _region_key_of(profile.get("region") or "")
        if user_key and user_key not in region_hits:
            region_key, phrase = next(iter(region_hits.items()))
            return (f"限定地区：{region_key}（命中「{phrase}」），"
                    f"与画像地区（{profile.get('region')}）不符"), blob

    # 4) 学生类型：文本限定到与画像类型不同的类型 → 不符
    type_hits = _detect_student_type_restrictions(blob)
    if type_hits:
        user_type = profile.get("student_type")
        if user_type not in type_hits:
            type_key, phrase = next(iter(type_hits.items()))
            user_label = STUDENT_TYPE_LABELS.get(user_type, user_type or "未知")
            return (f"仅面向{STUDENT_TYPE_LABELS.get(type_key, type_key)}"
                    f"（命中「{phrase}」），"
                    f"与画像学生类型（{user_label}）不符"), blob

    return "", blob


def _keyword_scan(blob: str):
    """原有关键词规则扫描（v0.2 语义，冲突以不符合为准，宁缺毋滥）

    P4-10 子项 3 (2026-09-13)：抬升只认 ELIGIBLE_KEYWORDS（A 类可证伪的准入
    证据）；NEUTRAL_KEYWORDS（B 类不可证伪的泛化词）**不再计分**，仅在无其它
    命中时把命中的中性词写进 reason 供追溯——否则"为什么这行是 unknown"在
    库里完全没有痕迹（B 类词在相关性轴的 matched_keywords 里另有一份展示）。

    Returns:
        (hint, reason)：reason 为中文判断依据；unknown 且无中性词命中时为空串
    """
    if not blob:
        return EligibilityHint.UNKNOWN, ""
    for kw in INELIGIBLE_KEYWORDS:
        if kw.lower() in blob:
            return EligibilityHint.LIKELY_NO, f"命中限制关键词「{kw}」"
    for kw in ELIGIBLE_KEYWORDS:
        if kw.lower() in blob:
            return EligibilityHint.LIKELY_YES, f"命中开放关键词「{kw}」"
    neutral = [kw for kw in NEUTRAL_KEYWORDS if kw.lower() in blob]
    if neutral:
        return (EligibilityHint.UNKNOWN,
                "仅命中中性词「" + "」「".join(neutral[:5])
                + "」，不构成准入证据")
    return EligibilityHint.UNKNOWN, ""


# ---------- v0.4 相关性粗筛（确定性关键词规则，无 LLM 成本） ----------

def _keyword_hits(blob: str, keywords) -> list:
    """返回 blob 中命中的关键词（英文词边界、中文子串；blob 需已小写）"""
    hits = []
    for kw in keywords:
        if kw.isascii():
            # 词边界：避免 art/smart、us/campus 这类子串误伤
            pattern = re.compile(
                rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])")
            if pattern.search(blob):
                hits.append(kw)
        else:
            if kw in blob:
                hits.append(kw)
    return hits


def check_relevance(title: str, summary: str = ""):
    """按关键词库粗筛机会与学生开发者主题的相关性（纯函数，零 LLM 成本）

    规则（确定性，冲突优先级：强相关 > 不相关 > 弱相关）：
    - 命中 STRONG_RELEVANCE_KEYWORDS → relevant，每个 +0.4；
    - 命中 IRRELEVANT_KEYWORDS 且**无强相关** → irrelevant（分数 0）；
    - 仅命中 WEAK_RELEVANCE_KEYWORDS → unknown（分数 0.2）；
    - 无任何命中 → unknown（分数 0）；
    - 软条件（费用 / 组队 / 材料等，见 SOFT_CONDITION_KEYWORDS）**不淘汰**，
      每个 -0.1 分（最低 0），用于 UI 提示"该机会可能有隐性成本"。
    分数封顶 1.0；matched_positive 为命中的正相关词（强+弱），
    matched_negative 为命中的不相关词（仅供 UI 展示，不影响判定结果）。

    Returns:
        (relevance_status, relevance_score, matched_positive, matched_negative)
    """
    blob = _clean_text(f"{title} {summary}").lower()
    if not blob:
        return RelevanceStatus.UNKNOWN, 0.0, [], []
    strong = _keyword_hits(blob, STRONG_RELEVANCE_KEYWORDS)
    weak = _keyword_hits(blob, WEAK_RELEVANCE_KEYWORDS)
    negative = _keyword_hits(blob, IRRELEVANT_KEYWORDS)
    soft = _keyword_hits(blob, SOFT_CONDITION_KEYWORDS)
    # 正相关命中列表：强 + 弱（去重，避免同一词条双计）
    matched_positive = list(strong)
    for w in weak:
        if w not in matched_positive:
            matched_positive.append(w)
    if strong:
        score = 0.4 * len(strong) + 0.2 * len(weak) - 0.1 * len(soft)
        return (RelevanceStatus.RELEVANT, round(min(1.0, max(0.0, score)), 4),
                matched_positive, list(negative))
    if negative:
        # 不相关命中且无强相关 → 直接排除（分数 0）
        return (RelevanceStatus.IRRELEVANT, 0.0, matched_positive,
                list(negative))
    if weak:
        score = 0.2 - 0.1 * len(soft)
        return (RelevanceStatus.UNKNOWN, round(max(0.0, score), 4),
                matched_positive, list(negative))
    return RelevanceStatus.UNKNOWN, 0.0, [], []


def check_soft_conditions(title: str, summary: str = "") -> list:
    """单独返回命中的软条件关键词（费用 / 组队 / 材料等）

    软条件不参与相关性判定（见 check_relevance），但会降低分数并在 UI
    提示；落库时与正/负相关命中一起序列化进 matched_keywords JSON。
    """
    blob = _clean_text(f"{title} {summary}").lower()
    if not blob:
        return []
    return _keyword_hits(blob, SOFT_CONDITION_KEYWORDS)


def matched_keywords_to_json(positive, negative, soft) -> str:
    """把命中关键词序列化为入库 JSON（UI 解析展示用）"""
    return json.dumps({
        "positive": list(positive or []),
        "negative": list(negative or []),
        "soft": list(soft or []),
    }, ensure_ascii=False)


#: `opportunities.notes` 的分段分隔符（P6-3 / P6-5b，2026-09-13）
#:
#: **为什么把展示用信息塞进 notes**：本批硬约束是"零 schema 变更"，而
#: `opportunities` 表里唯一既存在又能被 UI 读到的自由文本列就是 `notes`。
#: 于是它从"类目"升级为"一条机会的展示分段集合"，段与段之间用全角竖线分开，
#: 渲染层逐段展示（`notes_segments`）。用全角竖线而不是 `|`，是因为本面板
#: 是 Markdown 表格以外的行式输出，但 `|` 仍可能在别处被当表格分隔符转义。
NOTES_SEPARATOR = "｜"

#: 来源冲突标记（P6-3② 的显示口径）。**"未实测"必须保留**：它如实说明这个值
#: 是从两个来源读出来的、我们没有亲自验证过哪一个是真的。
DEADLINE_CONFLICT_MARK = "（来源冲突，未实测）"

# ---------- P6-5b 措辞口径：诚实的缺口显示（纯显示层，0 外呼） ----------

#: 一手接口行的"字段来源"说明（P6-5b①，2026-09-13）
#:
#: 动因是 247 实证：**全库正文最厚的一行（10,689 中文字）仍然整条缺失"创空间
#: 700h"这条路径**——它只在另一个官方页面 `modelscope.cn/learn/433641` 上，而 247
#: 的正文里 `learn` / `posts` 出现 **0 次**（只有 28 个 `gallery/` 链接），且链接
#: 方向是**反的**（learn 页页脚才指向 `/events/247`）。
#: ⇒ **"正文长"与"规则全"没有关系。** 所以这里不写"已获取完整规则"，只写清楚
#: "这些字段是从哪个源、什么时刻抓来的"，把"细则以官网为准"放在同一句话里。
PROVENANCE_CAPTION = "以下字段抓取自接口，抓取时刻 {fetched}；细则以官网为准"

#: 有官网链接时的完整句式（→ 目标页）
PROVENANCE_CAPTION_URL = PROVENANCE_CAPTION + " → {url}"

#: 提交截止缺失时的口径（P6-5b②，2026-09-13）
#:
#: **不许显示成"无提交截止"**：接口的 `EndTime` 实测全为 0（即接口没给这个字段），
#: 而"没抓到"与"不存在"对用户是两句完全不同的话——前者要他再去官网找一眼，
#: 后者让他直接放弃。本项目已有实测反例：同一活动的作品提交截止
#: （08.19–09.14 22:00）只在官方落地页上，接口里完全没有。
SUBMIT_DEADLINE_MISSING = "提交截止：未抓到（该源可能另有官方说明页）"

#: 禁止出现在面板/账单里的"已抓全"式措辞（P6-5b①，2026-09-13）
#:
#: 由措辞快照测试锁死：任何一处渲染输出里出现这些字样都判失败。它们的问题不是
#: 措辞不好听，而是**把"我抓到的"说成"存在的全部"**——247 那条最厚的正文就是
#: 反例（10,689 中文字里一次都没提到真正承载那条路径的官方页面）。
FORBIDDEN_COMPLETENESS_PHRASES = (
    "完整规则已获取", "规则已获取", "已获取完整", "规则完整",
    "已抓全", "抓取完整", "全部规则", "完整细则已",
)

#: 一手源实体里用来判定"这一行来自结构化一手接口"的键（见 P6-3 的
#: `deadline_sources`）。只有一手源写它，故它同时是"要不要挂来源说明"的开关。
PRIMARY_SOURCE_ENTITY_KEY = "deadline_sources"


def notes_segments(notes) -> list:
    """把 `opportunities.notes` 拆成展示分段（去掉空段，纯函数）

    首段按约定是 `类目：X`（见 `_notes_from_evidence`），其余段是自带标题的
    完整短语（如 `报名截止 2026-09-14（来源冲突，未实测）`），渲染层不需要
    再解析语义，直接逐段打出来即可。
    """
    return [s.strip() for s in str(notes or "").split(NOTES_SEPARATOR) if s.strip()]


def find_deadline_conflict_note(notes) -> str:
    """返回 notes 里的"来源冲突"分段（没有则返回空串，纯函数）

    给账单用：账单是逐条列出的文本，不适合把 notes 全段都铺开，但
    **来源冲突恰恰是账单必须说出来的那一句**（选错值的代价是不对称的）。
    """
    for seg in notes_segments(notes):
        if DEADLINE_CONFLICT_MARK in seg:
            return seg
    return ""


def _provenance_segment(ev) -> str:
    """一手接口行的"字段来源 + 抓取时刻 + 官网链接"说明（P6-5b①，纯函数）

    这是本批特意**不**做的一件事的正面写法：不把"我们抓到的字段"讲成"全部规则"。
    抓取时刻取自 `Evidence.retrieved_at`（一手源写入时必填），官网链接取自
    `Evidence.source_url`；两者都缺时退化为不带链接的句式，不编造时间。
    """
    fetched = str(getattr(ev, "retrieved_at", "") or "").strip()[:16] or "未知"
    url = _clean_text(getattr(ev, "source_url", "") or "")
    if url:
        return PROVENANCE_CAPTION_URL.format(fetched=fetched, url=url)
    return PROVENANCE_CAPTION.format(fetched=fetched)


def _submit_deadline_segment(obj) -> str:
    """提交截止的展示分段（P6-5b②，纯函数）

    抓到（落地页两跳解析出的 `SubmitDeadlineTs`）就报时刻，"未抓到"就明确说
    **未抓到**并给出下一步（官网可能另有说明页）——**绝不写成"无提交截止"**。
    """
    ts = obj.get("SubmitDeadlineTs")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            when = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return SUBMIT_DEADLINE_MISSING
        return f"提交截止：{when}"
    return SUBMIT_DEADLINE_MISSING


def _notes_from_evidence(ev, deadline_text: str = "") -> str:
    """从 Evidence.entities 组装机会备注分段（零 schema 变更下的展示载体）

    Evidence.entities 是 V0.2+ 的"实体抽取结果（JSON 字符串）"字段（见
    core/information_engine/models.py）(2026-09-13)。
    ModelScopeProvider 借它携带魔搭的结构化原值，这里挑出**要展示的那几项**
    落进 opportunities.notes。**类目只作展示，不参与资格/相关性判断**
    （资格判断只看 title + summary，避免类目词污染那两条关键词轴）。

    分段顺序即展示顺序，当前产出四段（各段都是自带标题的完整短语）：
    1. **来源说明**（P6-5b①，一手接口行专有）：`以下字段抓取自接口，抓取时刻
       YYYY-MM-DD HH:MM；细则以官网为准 → <官网链接>`。放在最前是有意的：
       "以下字段"描述的是它**下面**那几行，而且这句话的作用正是给下面那些字段
       限定边界——把"我抓到的"与"存在的全部"分开。**本批不扩抓取面**（不接站内
       搜索、不爬 learn/*）：扩展抓取面是无底洞且方向不对，真正的失效是"缺失被
       显示成不存在"。
    2. `类目：X`（Category 非空时）；
    3. `报名截止 X（来源冲突，未实测）`（P6-3②：接口与落地页给出的报名截止
       不一致时）。**不择一消解**：较晚的那个值已经进了 `deadline` 列用于开放
       判断，这里把"两个来源打架、我们没实测哪个对"如实说出来。
    4. `提交截止：未抓到（该源可能另有官方说明页）`（P6-5b②）。

    任何解析失败一律退化为"没有备注"，绝不抛异常——备注是锦上添花，
    不该让一条机会入库失败。
    """
    raw = getattr(ev, "entities", None)
    segments = []
    obj = {}
    if raw:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
    if PRIMARY_SOURCE_ENTITY_KEY in obj:
        # 一手接口行：先交代来源与时刻，再列字段，最后交代缺了什么
        segments.append(_provenance_segment(ev))
    category = _clean_text(str(obj.get("Category") or ""))
    if category:
        segments.append(f"类目：{category.replace(NOTES_SEPARATOR, '/')[:100]}")
    if obj.get("deadline_conflict") and deadline_text:
        segments.append(f"报名截止 {deadline_text}{DEADLINE_CONFLICT_MARK}")
    if PRIMARY_SOURCE_ENTITY_KEY in obj:
        segments.append(_submit_deadline_segment(obj))
    return NOTES_SEPARATOR.join(segments)


# ---------- P4-11 报名截止日（确定性纯函数，无 LLM / 无成本） ----------

def _parse_deadline_date(value):
    """把 deadline 字符串解析为 datetime.date；空 / 非法返回 None

    **私有**（下划线前缀）是有意的：本仓库已有两个语义相同的公开实现——
    `agents/planning_mirror_agent.parse_deadline_days_left` 与
    `agents/daily_planning_agent._deadline_sort_key`——但 `core` 是 L0 层、
    `agents` 是 L2 层，`checks/check_import_direction.py` 明文禁止低层 import
    高层，故此处无法复用它们；同时本函数是 `core/resource_scout.py` 首见的
    公开名，若建成公开名会立刻触发 `checks/check_duplicate_names.py`（跨模块
    重复公共名）而需要新增一条豁免。折中：**只解析本项目自己写入的定宽
    'YYYY-MM-DD'**（取自 `deadline[:10]`，与既有两处实现同款口径，兼容带时间的
    时间戳），并保持私有不对外暴露。若日后要统一三份实现，正确做法是把权威
    实现下沉到 `core` 并让两个 agents 模块委托，届时本函数即为其落点。
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _normalize_deadline(value) -> str:
    """把 Evidence.published_at 规整为 deadline 列的定宽 'YYYY-MM-DD'

    合法日期返回 'YYYY-MM-DD'；空 / 非法一律返回空串（列语义：空 = 未知）。
    只认本项目自己写入的格式，不做任何猜测性解析。
    """
    parsed = _parse_deadline_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def deadline_display(deadline, today=None) -> str:
    """把**报名截止**日换算为面板展示文案（纯函数，便于测试）

    文案口径（P6-1①，2026-09-13）：**一律写明"报名截止"**，不再出现无限定的
    "截止"。原因是"报名截止"与"作品提交截止"是两根独立的时间轴（实测同一活动
    接口只给报名截止、提交截止只存在于官方落地页），一个裸"截止"会被读成哪一个
    完全取决于读者，而两个轴对决策的含义完全不同。

    规则（P4-11 起，P6-1 改文案）：
    - 未来      → "⏳ 距报名截止 N 天"
    - 当天      → "⏳ 今天报名截止"
    - 已过      → "⏰ 报名截止日已过 YYYY-MM-DD"
    - 空 / 非法 → 空串（调用方据此**整项不显示**）

    注意"已过"只陈述**日期事实**，不写"已错过 / 无法参加"这类判定
    （P6-2：判死必错，见 `deadline_weekday_notice`）。

    日期基准用**本地日期** `date.today()`（与 ModelScopeProvider 换算截止日时
    用的 `time.localtime` 同一时区口径，不要用 UTC，否则跨日会差一天）；
    `today` 仅为测试注入。

    Args:
        deadline: 'YYYY-MM-DD'（或带时间的时间戳，取前 10 位）
        today: datetime.date，默认取当天
    """
    parsed = _parse_deadline_date(deadline)
    if parsed is None:
        return ""
    base = today or date.today()
    days_left = (parsed - base).days
    if days_left > 0:
        return f"⏳ 距报名截止 {days_left} 天"
    if days_left == 0:
        return "⏳ 今天报名截止"
    return f"⏰ 报名截止日已过 {parsed.strftime('%Y-%m-%d')}"


def deadline_is_weekday(deadline) -> bool:
    """派生标记：报名截止日是否落在**周一~周五**（本地时区，纯函数，P6-1③）

    **派生、不落库**（与 `effective_hint` 同款口径）：星期几完全由
    `opportunities.deadline` 的日期决定，存一遍只是把同一个事实写两处。
    一手源的 `RegistrationDeadline` 是 unix 秒，换算后落库时已按
    `time.localtime` 定成 'YYYY-MM-DD'，故此处按日期反推星期与之一致。

    空 / 非法截止日返回 False——"没有截止日"不等于"落在周中"，
    调用方应先用 `deadline_display` 判空，不要拿本函数当"有截止日"的判据。
    """
    idx = weekday_index_of_date_text(deadline)
    return idx is not None and idx not in WEEKEND_INDEXES


#: 周中报名截止的显示口径：**提问，不是判死**（P6-2，2026-09-13）
#:
#: 依据是两个当晚就被推翻的反例：
#:   (a) 设计侧把周中截止当成"结构性不可参加"，被用户一句"明天我刚好放假"推翻；
#:   (b) 某 10/8 周四截止的活动，官方正文自己写着"跨国庆假期，正好有空写稿"。
#: 结论：**周中只是"要问一句"，从来不是"可以不算"**。
#:
#: 因此这条文案只输出一个**待用户确认的问题**，并在渲染层被测试锁死：
#: 任何"已过期 / 无法参加 / 已错过"式的判定都不许出现（判死一次的成本是机会
#: 永久消失，多问一句的成本是一句话）。
#: 文案写明"报名截止"而非裸"截止"，与 P6-1① 同一条口径。
DEADLINE_WEEKDAY_NOTICE = "⚠️ 报名截止落在{weekday}，需确认当天是否有电脑"


def deadline_weekday_notice(deadline, today=None) -> str:
    """报名截止落在周中时的**提问**文案；无需提问时返回空串（纯函数，P6-2）

    返回空串的两种情况（调用方据此整项不追加）：
    1) 报名截止不在周一~周五，或日期未知 / 非法（见 `deadline_is_weekday`）；
    2) 报名截止日**已经过去**——"那天有没有电脑"对一个已经过去的日期没有意义，
       而"已过去"这件事由 `deadline_display` 如实陈述，不需要也不允许在这里
       升级成"你错过了"。这是一处**有意收窄**：P6-2 的禁令针对的是"从星期几
       推出不能参加"，而这里既不推、也不加一句多余的无效提问。

    刻意**不引入任何"机时日历"状态**（项目不变量：反馈必须是既有动作的副作用）
    ——本函数是纯函数，只读一个已落库的日期，不写库、不记"问过没有"。

    Args:
        deadline: 'YYYY-MM-DD'（或带时间的时间戳，取前 10 位）
        today: `datetime.date`，默认取本地当天（仅测试注入）
    """
    if not deadline_is_weekday(deadline):
        return ""
    parsed = _parse_deadline_date(deadline)
    if parsed is None or parsed < (today or date.today()):
        return ""
    return DEADLINE_WEEKDAY_NOTICE.format(
        weekday=weekday_label(weekday_index_of_date_text(deadline)))


def sort_opportunities_for_panel(rows, today=None) -> list:
    """默认视图排序（纯函数）：报名截止未过的按 deadline 升序排最前，其余保持原序

    规则（P4-11）：`deadline` 非空且报名截止日未过（含"今天报名截止"）的项按
    deadline **升序**在最前（最紧急的先看到）；其余项——无报名截止日（未知）与
    报名截止日已过——**保持传入顺序**（即原有的"按发现时间倒序"）跟在后面。

    **报名截止日已过的项只被排到末尾，不删除、不隐藏、不改 status**：用户仍能
    看到它，只是不再占据前排（本项目"绝不自动忽略任何机会"的不变量）。

    Args:
        rows: opportunities 行 dict 列表（须已按既有顺序排好）
        today: datetime.date，默认取当天（仅测试注入）
    """
    base = today or date.today()
    urgent, rest = [], []
    for row in rows:
        parsed = _parse_deadline_date(row.get("deadline"))
        if parsed is not None and parsed >= base:
            urgent.append((parsed, row))
        else:
            rest.append(row)
    urgent.sort(key=lambda pair: pair[0])   # 升序：最紧急在前
    return [row for _, row in urgent] + rest


# ---------- P4-14 子项 ② 会话启动对账单（纯函数，零 LLM / 零网络 / 零写库） ----------

#: 账单"仍然算数"的状态集合（白名单，而非黑名单）
#: 用白名单是因为要"默认可见"：用户一旦把某条标成 ignored，就是明确表态不看它了；
#: 而 new / interested / favorited 都还意味着"这条还在我的盘子里"。
_BILL_VISIBLE_STATUSES = (
    OpportunityStatus.NEW,
    OpportunityStatus.INTERESTED,
    OpportunityStatus.FAVORITED,
)

#: 账单里"7 天内报名截止"的窗口长度（天，含今天）
_BILL_SOON_DAYS = 7

#: 账单里"近 N 天新发现"的窗口长度（天）
_BILL_RECENT_DAYS = 7


def _bill_candidates(rows):
    """筛出账单的候选行：默认可见状态 + 与面板折叠口径一致

    两条过滤都复用既有语义，不新造规则：
    - **状态白名单**（`_BILL_VISIBLE_STATUSES`）：ignored 不进账单——用户已表态；
    - **`effective_hint(...) != likely_no`**：与
      `ui/controllers/resource_controller.py` 的折叠口径一致。那批行在面板上已被
      折进"已折叠 N 条"，账单里再把它们喊出来等于绕过折叠、自相矛盾。
      unknown / likely_yes 一律显示。
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in _BILL_VISIBLE_STATUSES:
            continue
        if effective_hint(row.get("eligibility_hint"),
                          row.get("ai_eligibility")) == EligibilityHint.LIKELY_NO:
            continue
        out.append(row)
    return out


def _bill_title(row) -> str:
    """账单里一条机会的标题（截断过长标题，避免单行刷屏）"""
    title = _clean_text(str(row.get("title") or "")) or "(无标题)"
    return title[:60] + "…" if len(title) > 60 else title


def _bill_note_lines(row) -> list:
    """账单条目下另起一行的补充说明：来源冲突 + 提交截止缺口（P6-3② / P6-5b②）

    为什么账单也要说这两句：账单是**启动时逐条扫一眼**的地方，而这两句恰好都是
    "要你再去确认一次"的信号——
    - 来源冲突：多验证一次是秒级，判死一次是机会永久消失；"悄悄取了较晚的那个值"
      会让用户以为只有一个日期；
    - 提交截止未抓到：接口实测 `EndTime` 全为 0，"没抓到"与"不存在"是两句不同的
      话（后者会让他直接放弃）。
    长句的来源说明（"以下字段抓取自接口…"）刻意**不进账单**：账单是逐行摘要，
    整句铺开会把 12 行变成 24 行；它留在能逐条细看的面板卡片上。
    """
    lines = []
    for seg in notes_segments(row.get("notes")):
        if DEADLINE_CONFLICT_MARK in seg:
            lines.append(f"  ⚠️ {seg}")
        elif seg.startswith("提交截止："):
            icon = "⚠️" if seg == SUBMIT_DEADLINE_MISSING else "📌"
            lines.append(f"  {icon} {seg}")
    return lines


def build_session_bill(rows, today=None) -> str:
    """把当前机会盘面汇总成一段"会话启动对账单" Markdown（纯函数）

    定位（P4-14）：**会话启动触发的无状态查询**——不记"上次看到哪"，不写库、
    不删行、不改 status、不把任何东西标成"已看过"。定时触发（`scheduler.py`）
    在它之上、不是它的替代：本函数每次调用都从当前数据重算，幂等且可重复。

    分四段，**全部用绝对日期**（不写"3 天前"这类相对描述，方便直接核对）：
    1) **报名截止日已过**：`deadline < today` 且仍在默认可见状态。**必须打出原
       报名截止日**，否则用户分不清"上周过的"和"今早过的"。按截止日升序。
       段标题刻意**不写"已错过"**（P6-2）——日期过去是事实，"你错过了"是判定；
       本项目已有两个反例证明这类判定必错，而错一次的代价是机会永久消失。
    2) **`_BILL_SOON_DAYS` 天内报名截止（含今天）**：用现成的 `deadline_display` 出
       `⏳ 距报名截止 N 天` / `⏳ 今天报名截止`，按报名截止日升序；
       落在周一~周五的条目另附 `deadline_weekday_notice`（要问一句，不判死）。
       段标题里的天数取常量而不是字面量——**同一个窗口长度只应有一处定义**。
    3) **更远的报名截止未过**：只报条数 + 最近一条——这段是"别慌，后面还有"，
       逐条展开就是噪声。
    4) **近 7 天新发现**：按 `discovered_at` 算固定窗口条数。
       刻意**不用"距上次开机"**：那需要存状态、要考虑幂等，而固定窗口在
       "一天开一次"的机器上与之等价，还让整件事保持纯函数。
       真长跑时再换 `last_visit` 不迟。

    复用（不新写第 5 份解析器）：`_parse_deadline_date` / `deadline_display` /
    `effective_hint` / `sort_opportunities_for_panel` 全部取本文件既有实现。
    报名截止日解析**一处都不重写**。

    Args:
        rows: opportunities 行 dict 列表（`list_opportunities()` 的返回形态）；
            非 dict 元素被安全忽略（容忍脏输入）
        today: `datetime.date`，默认取本地当天（**可注入**，否则测试无法固定日期）

    Returns:
        Markdown 字符串；**无任何内容时返回空串**（调用方据此整块隐藏，
        不是显示"无内容"字样）
    """
    base = today or date.today()
    candidates = _bill_candidates(rows)

    overdue, soon, later = [], [], []
    for row in candidates:
        parsed = _parse_deadline_date(row.get("deadline"))
        if parsed is None:
            continue                # 无报名截止日（含 NULL）→ 不参与 1/2/3 段
        if parsed < base:
            overdue.append(row)
        elif (parsed - base).days <= _BILL_SOON_DAYS:
            soon.append(row)
        else:
            later.append(row)

    # 段 1/2 都要"最紧急/最早过的在前"：复用面板排序（它的语义正是
    # "报名截止未过的按 deadline 升序在前"，对已过项则保持传入顺序，故段 1 自己按
    # 解析出的日期排一次，保证"上周过的"排在"今早过的"前面）
    overdue.sort(key=lambda r: _parse_deadline_date(r.get("deadline")))
    soon = sort_opportunities_for_panel(soon, today=base)
    later.sort(key=lambda r: _parse_deadline_date(r.get("deadline")))

    # 段 4：近 N 天新发现（按 discovered_at 的日期部分比较，纯字符串前缀比较即可，
    # 因为该列是 "YYYY-MM-DD HH:MM:SS" 定宽格式；解析失败的行不计入）
    window_start = base - timedelta(days=_BILL_RECENT_DAYS)
    recent = 0
    for row in candidates:
        raw = str(row.get("discovered_at") or "").strip()
        if not raw:
            continue
        try:
            when = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if when >= window_start:
            recent += 1

    if not (overdue or soon or later or recent):
        return ""                        # 全空 → 空串（不是"无内容"字样）

    lines = [f"### 🧾 会话启动对账单（{base.strftime('%Y-%m-%d')}）", ""]

    if overdue:
        lines.append(f"**⏰ 报名截止日已过 {len(overdue)} 条**"
                     "（截止日已过，仍在我的盘子里）：")
        for row in overdue:
            dl = _normalize_deadline(row.get("deadline"))
            lines.append(f"- {_bill_title(row)}　（原报名截止日 {dl}）")
            lines.extend(_bill_note_lines(row))
        lines.append("")

    if soon:
        lines.append(f"**⏳ {_BILL_SOON_DAYS} 天内报名截止 {len(soon)} 条**：")
        for row in soon:
            entry = (f"- {_bill_title(row)}　"
                     f"{deadline_display(row.get('deadline'), today=base)}"
                     f"（{_normalize_deadline(row.get('deadline'))}）")
            lines.append(entry)
            lines.extend(_bill_note_lines(row))
            # P6-2：周中报名截止只提问、不判死（已过去的日期不提问，理由见
            # deadline_weekday_notice 的 docstring）
            notice = deadline_weekday_notice(row.get("deadline"), today=base)
            if notice:
                lines.append(f"  {notice}")
        lines.append("")

    if later:
        nearest = later[0]               # 已是升序，第一条即最近
        tail = (f"**📌 更远的还有 {len(later)} 条**"
                f"（最近一条：{_bill_title(nearest)}，"
                f"{deadline_display(nearest.get('deadline'), today=base)}）")
        notice = deadline_weekday_notice(nearest.get("deadline"), today=base)
        if notice:
            tail += f"　{notice}"
        lines.append(tail)
        lines.append("")

    if recent:
        lines.append(f"**🆕 近 {_BILL_RECENT_DAYS} 天新发现 {recent} 条**")

    # 去掉末尾多余空行，保持输出稳定（便于测试断言与 UI 渲染）
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


# ---------- P7 批次 1a：「资源」格（本地机会库筛选报告，纯函数，零网络 / 零 LLM / 零写库） ----------
#
# 为什么要这一格：截止日驱动的那条线（面板排序 / `build_session_bill`）只在用户
# **在场**时起作用——他连续几天不开机，临期条目就永远没有机会出现在他眼前。本格
# 把本地机会库按用户定的规则过一遍，产出与他人在不在无关。
#
# 用户定的三筛子（2026-09-18 定，2026-09-19 明确为"既是我当下需要的资源，又要我
# 比较容易拿到"＝两条都要满足）落到库里那两根现成的轴上——口径取自
# `core/ai_filter.py` 的 Prompt 原文，不是本处新造的语义：
#   - ai_value_score   ＝ "0 到 1 的小数（对该用户的实际价值）"       → ② 当下需不需要
#   - ai_project_reuse＝ "0 到 1 的小数（可复用到用户项目的程度）"   → ③ 够不够得着
# 二者都由「💡 资源机会」页的**AI 深度筛选**（手动按钮）写入，本格只读不算、不调
# LLM，也不改任何 status——"绝不自动忽略任何机会"这条不变量在本格同样成立。

#: 每段最多列出条数（超出的只计数不逐条列，见"未列入"段）
_DIGEST_LIMIT = 8

#: 临期窗口（天）：报名截止落在"今天 + 本值"之前的条目**必须进格**
#: （2026-09-18 新规则）。理由：临期时用户不在，这类条目**永远没有机会**出现在
#: 他眼前，所以它们不受 ②③ 两筛子约束。默认 7 天＝"下一个可能开机日"。
_DIGEST_PREOPEN_DAYS = 7

#: "长期挂着"的最短悬挂天数（从 discovered_at 起算）
_DIGEST_DORMANT_DAYS = 14

#: ② 当下需要：ai_value_score 下限。低于此值的即"交即得、账户里天天白涨"那一类
#: （分数口径见 ai_filter 的 Prompt："对该用户的实际价值"）
_DIGEST_MIN_VALUE_SCORE = 0.3

#: ③ 够得着：ai_project_reuse 下限（"可复用到用户项目的程度"）。
#: 够不着还有第二重判据：资格提示为 likely_no 的也进不了本格。
_DIGEST_MIN_PROJECT_REUSE = 0.2

#: 空状态文案之一：**一条都没符合条件**时输出它（原义，未经改动）
DIGEST_EMPTY_TEXT = "本期没有符合条件的条目"

#: 空状态文案之二：**符合条件但本期没有新变化**（P7 批次 3 水位线引入）
#: 与 DIGEST_EMPTY_TEXT 是两句不同的话，**不要合并**：前者是"盘子里空"，后者是
#: "盘子里有东西，但都在上一次已经报过了"——后者恰恰证明水位线在起作用。
DIGEST_NO_CHANGE_TEXT = "本期没有新变化（自上次播报以来，没有条目进入新的状态）"

#: "尚未临期"那一类在未列入段里**展开最近几条**（P7 批次 1b）
#: 取 3 而不是全展开：段末本就有"另有 N 条未列出"兜底，再多就退化成第二份对账单；
#: 而"离现在最近的三条"正是从"很久以后"到"快该准备了"之间最值得先看一眼的那一段。
_DIGEST_FAR_PREVIEW = 3

#: 「📄 源站原文」那一行的摘要截断长度（P7 批次 1b-③）
#: 摘要回答的是"**能得到什么**"（奖金/名额/字数要求那类事实），是用户点开之前最想
#: 知道的一句；限长是因为它只用于判断"要不要点进去"，不必在格子里全文铺开。
_DIGEST_SUMMARY_LIMIT = 120


def _digest_params(params) -> dict:
    """把模板 action_params 里的筛选参数规范化（越界 / 非法一律回退默认值）

    只认识本格的五个键，其余键（如 search_and_note 的 query）原样忽略——
    同一份 action_params 不该因为多了个 query 就让本格崩掉。
    """
    out = {
        "limit": _DIGEST_LIMIT,
        "preopen_days": _DIGEST_PREOPEN_DAYS,
        "dormant_days": _DIGEST_DORMANT_DAYS,
        "min_value_score": _DIGEST_MIN_VALUE_SCORE,
        "min_project_reuse": _DIGEST_MIN_PROJECT_REUSE,
    }
    src = params if isinstance(params, dict) else {}
    for key, high in (("limit", 50), ("preopen_days", 365), ("dormant_days", 365)):
        try:
            val = int(src.get(key, out[key]))
        except (TypeError, ValueError):
            continue
        out[key] = min(high, max(1 if key == "limit" else 0, val))
    for key in ("min_value_score", "min_project_reuse"):
        try:
            val = float(src.get(key, out[key]))
        except (TypeError, ValueError):
            continue
        out[key] = min(1.0, max(0.0, val))
    return out


def _digest_score(value) -> Optional[float]:
    """把 ai_* 分数转成 float；None / 空 / 不可解析都返回 None（＝"未经 AI 初筛"）"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _digest_age_days(row, base: date) -> Optional[int]:
    """条目已挂了几个日历天（discovered_at → base）；无法解析返回 None"""
    raw = str(row.get("discovered_at") or "").strip()
    if not raw:
        return None
    try:
        when = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (base - when).days


def _digest_title(row) -> str:
    """条目标题：**不截断**（这是一份给人读的报告，不是面板上的一行）

    面板用 `_bill_title` 截到 60 字是为了不把一行撑爆；报告里截断反而会把
    "…(REGISTER ON GOOGLE…" 这种半句话留在用户眼前，他没法据此判断要不要点开。
    """
    return _clean_text(str(row.get("title") or "")) or "(无标题)"


def _digest_head(row) -> str:
    """条目标题行：标题（粗分类｜资格提示）——与面板同一套标签"""
    kind = TYPE_LABELS.get(str(row.get("type") or "other"), TYPE_LABELS["other"])
    hint = ELIGIBILITY_LABELS.get(
        effective_hint(row.get("eligibility_hint"), row.get("ai_eligibility")), "")
    tail = f"（{kind}｜{hint}）" if hint else f"（{kind}）"
    return f"**{_digest_title(row)}**{tail}"


def _digest_sub_line(text: str) -> str:
    """条目的补充行：渲染成**嵌套子项**（3 空格 + "- "）

    为什么不用"两个空格缩进"接在有序列表项后面：那种写法在 Markdown 里是"同一段
    落的续行"，相邻两行会被合并成一行（换行是否保留取决于渲染器的 breaks 设置）。
    嵌套子项在 commonmark 下必然各占一行——报告要能逐条读，就不能赌渲染器配置。
    """
    return f"   - {text.strip()}"


def _digest_summary_line(row) -> str:
    """「📄 源站原文」那一行：摘要回答"**能得到什么**"

    为什么这一行不是装饰：`ai_reason` 回答的是"你能不能拿到"（资格/难度），
    摘要回答的是"拿到的是什么"（奖金、名额、字数要求那类事实）——实测全格原本
    一处都没渲染 summary，而"能得到什么"只存在于 summary 里，两者不能互相替代。
    摘要为空（或只有空白）时返回空串，调用方据此**整行不加**（不留空行）。
    取库里已抓到的摘要而非联网抓取：本格零网络（见上方「资源」格的设计说明）。
    """
    summary = _truncate_phrase(str(row.get("summary") or ""), _DIGEST_SUMMARY_LIMIT)
    return f"📄 源站原文：{summary}" if summary else ""


# ---------- 桶判定与水位线（P7 批次 3；纯函数，零 IO） ----------

def _digest_overflow_line(lines: list, overflow: int, limit: int) -> None:
    """把"因 limit 未列出的条数"写进段尾（0 条不写）

    受 limit 截断的条目**不落水位线**（见 `_digest_select` 的 notify 只收实际列出的），
    所以它们下一期还会再报一次——这不是遗漏，是有意的：宁可重复一次，也不能让一条
    有变化的条目因为"这期格子满了"就永远消失。
    """
    if overflow > 0:
        lines.append(f"　…另有 {overflow} 条本期有变化但未列出"
                     f"（受 limit={limit} 限制，下一期仍会播报）")


def _digest_key(row):
    """行身份键：优先 `id`；无 `id` 时退回 `source_url`（表上有 UNIQUE 约束）

    实测 `ResourceScout.list_opportunities` 走的是 `SELECT *`，`id` 必然在行 dict 里；
    这里的兜底只是防御"日后有人改成显式列名查询却漏了 id"。
    **这个兜底不是契约**：`mark_digest_notified` 的 SQL 是 `WHERE id = ?`，只认 int 主键，
    兜底键（source_url 字符串）写不进去。契约的兑现点在**写入侧**——那里会打印
    "跳过 N 个非 int 键"而不是安静地少写几行；**不在调用侧**（活路径上 `id` 必然存在，
    补一个永真的 assert 只会多出"看起来有保护"的第二件东西）。
    测试侧那条断言是：tests/test_task_templates.py 的
    test_scout_digest_watermark_end_to_end 断言行 dict 里同时有 `id` 与 `notified_bucket`。
    """
    rid = row.get("id")
    if rid is not None:
        return rid
    return str(row.get("source_url") or "")


def _digest_bucket_of(row, base: date, conf: dict):
    """**桶判定与"未列入原因"的唯一实现**（内部；公开入口见 digest_bucket）

    Returns:
        (bucket, reason)：
        bucket ∈ expired / soon / dormant / far / none；
        reason 只在 bucket == 'none' 时有意义，取值 ignored / young / unscored /
        low_value / no_path（供「未列入」段如实计数），否则为空串。
    """
    if not isinstance(row, dict):
        return "none", "ignored"
    if row.get("status") not in _BILL_VISIBLE_STATUSES:
        # 已忽略 / 状态不在白名单：用户已明确表态不看它了
        return "none", "ignored"
    deadline = _parse_deadline_date(row.get("deadline"))
    if deadline is not None:
        if deadline < base:
            return "expired", ""
        if deadline <= base + timedelta(days=conf["preopen_days"]):
            return "soon", ""
        return "far", ""
    # 无报名截止日（"未知"）：走"无时点"那一套条件
    age = _digest_age_days(row, base)
    if age is None or age < conf["dormant_days"]:
        return "none", "young"
    value = _digest_score(row.get("ai_value_score"))
    reuse = _digest_score(row.get("ai_project_reuse"))
    if value is None and reuse is None:
        return "none", "unscored"
    if value is not None and value < conf["min_value_score"]:
        return "none", "low_value"
    if reuse is not None and reuse < conf["min_project_reuse"]:
        return "none", "no_path"
    if effective_hint(row.get("eligibility_hint"),
                      row.get("ai_eligibility")) == EligibilityHint.LIKELY_NO:
        return "none", "no_path"
    return "dormant", ""


def digest_bucket(row, today=None, params=None) -> str:
    """一行机会落在哪个**状态桶**（公开纯函数；桶判定的唯一权威）

    取值与语义：
      'expired'  有报名截止日且 deadline < today
      'soon'     有报名截止日且 today <= deadline <= today + preopen_days
      'far'      有报名截止日但超出 preopen 窗口
      'dormant'  无报名截止日，且满足现行「无时点」段的**全部**入选条件
                 （悬挂满 dormant_days + ②当下需要 + ③够得着 + 资格提示非 likely_no）
      'none'     其余（含 status 不在可见白名单、已忽略）

    为什么要把它单独提出来当公开函数：一是水位线（"只播报状态变化"）必须与筛选口径
    **同源**——桶变了就是"进入新状态"；二是本仓已经两次被"两处手工复制保持一致"咬到，
    桶判定只能有一份实现，`build_resource_digest` 内部也调它。
    """
    return _digest_bucket_of(row, today or date.today(), _digest_params(params))[0]


#: 会产生播报（也参与水位线比较）的三个桶；far / none 既不播报也不参与比较
_DIGEST_NOTIFY_BUCKETS = ("expired", "soon", "dormant")


def _digest_select(all_rows, base: date, conf: dict, notified=None):
    """把行分桶、算出**本次要播报**的条目与「未列入」计数（内部，唯一筛选实现）

    水位线口径：某行要播报 ⇔ 它的桶属于 {expired, soon, dormant} 且
    **不等于** `notified` 里记着的值（缺键 / None ＝ 从未播报）。
    `far` / `none` 的行不播报、也不参与变化判定。

    Returns:
        dict：
          listed    {桶: [(排序键, 行), ...]}——**已按 limit 截断**，即"实际被播报的"；
          overflow  {桶: 条数}——因 limit 未被列出的条数（它们**不落水位线**，
                    下一期仍会播报，所以不会丢）；
          far       [(deadline, 行), ...]（升序）——仅供「未列入」段预览；
          counts    {ignored/young/low_value/no_path/unscored: 条数}；
          qualified 符合三个播报桶条件的总条数（不论是否播报）；
          notify    [(行键, 桶), ...]——落水位线用，**与实际列出的条目一一对应**。
    """
    notified = notified if isinstance(notified, dict) else {}
    buckets = {name: [] for name in _DIGEST_NOTIFY_BUCKETS}
    far = []
    counts = {"ignored": 0, "young": 0, "unscored": 0,
              "low_value": 0, "no_path": 0}
    qualified = 0
    for row in all_rows:
        bucket, reason = _digest_bucket_of(row, base, conf)
        if bucket in ("far",):
            far.append((_parse_deadline_date(row.get("deadline")), row))
            continue
        if bucket == "none":
            if reason in counts:
                counts[reason] += 1
            continue
        qualified += 1
        if notified.get(_digest_key(row)) != bucket:
            if bucket == "dormant":
                value = _digest_score(row.get("ai_value_score"))
                reuse = _digest_score(row.get("ai_project_reuse"))
                buckets[bucket].append(((value if value is not None else -1.0,
                                         reuse if reuse is not None else -1.0), row))
            else:
                buckets[bucket].append(
                    ((_parse_deadline_date(row.get("deadline")),), row))

    buckets["expired"].sort(key=lambda pair: pair[0])
    buckets["soon"].sort(key=lambda pair: pair[0])
    buckets["dormant"].sort(key=lambda pair: pair[0], reverse=True)
    far.sort(key=lambda pair: pair[0])

    listed, overflow, notify = {}, {}, []
    for name in _DIGEST_NOTIFY_BUCKETS:
        items = buckets[name]
        listed[name] = items[:conf["limit"]]
        overflow[name] = max(0, len(items) - conf["limit"])
        for _, row in listed[name]:
            notify.append((_digest_key(row), name))
    return {"listed": listed, "overflow": overflow, "far": far, "counts": counts,
            "qualified": qualified, "notify": notify}


def digest_notify_items(rows, notified=None, today=None, params=None) -> list:
    """本次报告**实际会播报**的 (机会键, 桶) 列表（纯函数）

    为什么需要它：落水位线的人（模板执行层）必须知道"到底播报了哪几条"，而请求它去
    重算一遍就是第二份筛选实现。这里与 `build_resource_digest` 共用同一个
    `_digest_select`，两者不可能分叉；受 limit 截断而未列出的条目不在这里（因此
    不会落水位线，下一期还会再报，不会丢）。
    """
    base = today or date.today()
    conf = _digest_params(params)
    all_rows = [r for r in (rows or []) if isinstance(r, dict)]
    return _digest_select(all_rows, base, conf, notified)["notify"]


def build_resource_digest(rows, today=None, params=None, notified=None) -> str:
    """把本地机会库筛成一份「资源」格 Markdown（纯函数，可直接单测）

    **三段结构 + 水位线**（P7 批次 3 起；每段的口径都写在段标题里，用户不必反查代码）：
    1) 「⏰ 报名截止日已过」：deadline < today；
    2) 「⏳ 临期」：today <= deadline <= today + preopen_days；
    3) 「🧊 无时点」：无报名截止日 + 挂满 dormant_days + ②当下需要 + ③够得着。

    为什么把原来那一段「临期（含已过）」拆成两段：一句"临期 N 条"把**已过的也算在
    里面**（实测出现过"6 条里 4 条已过期"），那是一个会让人高估紧迫度的计数；而且
    两段的播报状态不同（已过是不会再变的终态、临期会随日期推进），不拆就没法做
    "变化才播报"。已过段的措辞照 P6-2 的既定口径：**只陈述日期事实，不写"已错过"**。

    **水位线（本批新增语义）**：`notified` 由调用方读库传入（`{机会键: 上次播报的桶}`，
    缺键 / None ＝ 从未播报）；本函数只读它、**不写库、不改 rows、不做任何 IO**，
    纯函数性质一个字都没变。某行要播报 ⇔ 它的桶属于 {expired, soon, dormant} 且
    不等于 `notified` 里记着的值；`far` / `none` 既不播报也不参与变化判定。
    这样一期报告只报"这一期新进入某个状态"的条目，而不是每周把同一批机会重念一遍。

    **验收口径（本节为原文口径，改动时不得放宽）**：
      不变量：同一（机会键 ＋ 状态桶）最多被播报一次。
      可判定形式：把相邻两期报告的**播报段条目**抽成 (键, 桶) 集合，交集必须为空。
      「未列入」段的计数与 far 预览、以及段标题里的数字**不计入**该不变量——
      它们不是"某一件事"，是盘面数字。
    （落水位线的人用 `digest_notify_items` 取"实际播报了哪几条"，两者共用同一个
      `_digest_select`。）

    已忽略（status=ignored）的条目一条都不进——用户已经明确表态不看它了
    （口径复用 `_BILL_VISIBLE_STATUSES`，与会话对账单一致）。

    **未列入的条目一律计数报告，不做静默丢弃**（"报告缺席，而不是假装满勤"）：
    尤其"无时点但未经 AI 初筛"那一类——它们的价值与可达性**未知**，本格不替用户
    判断，只如实说"未知、且怎么才能变得可知"。「未列入」段的结构与口径本批一字未动。

    两个空状态是两句不同的话：一条都没符合条件 → `DIGEST_EMPTY_TEXT`；符合条件但
    本期没有新变化 → `DIGEST_NO_CHANGE_TEXT`（后者同样保留「未列入」段）。

    Args:
        rows: opportunities 行 dict 列表（调用方自行取数，便于测试注入）
        today: datetime.date，默认取当天
        params: 模板 action_params 字典（可选覆盖五个筛选参数）
        notified: 水位线 `{机会键: 上次播报的桶}`；None ＝ 全部视为从未播报

    Returns:
        Markdown 文本。**不含缺席页眉**——页眉由模板执行层加在顶部
        （它讲的是"上一份什么时候运行的"，属模板而不属机会库）。
    """
    base = today or date.today()
    conf = _digest_params(params)
    all_rows = [r for r in (rows or []) if isinstance(r, dict)]
    sel = _digest_select(all_rows, base, conf, notified)
    listed, overflow, far = sel["listed"], sel["overflow"], sel["far"]
    counts = sel["counts"]
    cutoff = base + timedelta(days=conf["preopen_days"])

    lines = [f"### 🧊 本期「资源」（{base.strftime('%Y-%m-%d')}｜只读本地机会库，未联网）",
             "",
             "> 本格只播报**状态有变化**的条目（同一「条目＋状态」只播一次）；"
             "盘面数字见文末「未列入」。",
             ""]

    if listed["expired"]:
        lines.append(f"**⏰ 报名截止日已过 {len(listed['expired'])} 条**"
                     "（日期过去是事实；原报名截止日照旧打出，方便直接核对）：")
        for idx, (_, row) in enumerate(listed["expired"], start=1):
            lines.append(f"{idx}. {_digest_head(row)}"
                         f"{deadline_display(row.get('deadline'), today=base)}")
            url = str(row.get("source_url") or "").strip()
            if url:
                lines.append(_digest_sub_line(f"🔗 {url}"))
        _digest_overflow_line(lines, overflow["expired"], conf["limit"])
        lines.append("")

    if listed["soon"]:
        lines.append(f"**⏳ 临期：今天起 {conf['preopen_days']} 天内报名截止 "
                     f"{len(listed['soon'])} 条**（你不在的时候，这类条目没有"
                     "第二次机会出现在你眼前）：")
        for idx, (_, row) in enumerate(listed["soon"], start=1):
            lines.append(f"{idx}. {_digest_head(row)}"
                         f"{deadline_display(row.get('deadline'), today=base)}")
            notice = deadline_weekday_notice(row.get("deadline"), today=base)
            if notice:
                lines.append(_digest_sub_line(notice))
            for extra in _bill_note_lines(row):
                lines.append(_digest_sub_line(extra))
            summary_line = _digest_summary_line(row)
            if summary_line:
                lines.append(_digest_sub_line(summary_line))
            url = str(row.get("source_url") or "").strip()
            if url:
                lines.append(_digest_sub_line(f"🔗 {url}"))
        _digest_overflow_line(lines, overflow["soon"], conf["limit"])
        lines.append("")

    if listed["dormant"]:
        lines.append(f"**🧊 无时点、长期挂着（满 {conf['dormant_days']} 天）、"
                     f"当下需要且够得着 {len(listed['dormant'])} 条**：")
        for idx, (_, row) in enumerate(listed["dormant"], start=1):
            age = _digest_age_days(row, base)
            facts = [f"挂了 {age} 天"]
            value = _digest_score(row.get("ai_value_score"))
            reuse = _digest_score(row.get("ai_project_reuse"))
            if value is not None:
                facts.append(f"价值 {value:g}")
            if reuse is not None:
                facts.append(f"可复用 {reuse:g}")
            lines.append(f"{idx}. {_digest_head(row)}" + "｜".join(facts))
            reason = _clean_text(str(row.get("ai_reason") or ""))
            if reason:
                lines.append(_digest_sub_line(f"理由：{reason}"))
            summary_line = _digest_summary_line(row)
            if summary_line:
                lines.append(_digest_sub_line(summary_line))
            url = str(row.get("source_url") or "").strip()
            if url:
                lines.append(_digest_sub_line(f"🔗 {url}"))
        _digest_overflow_line(lines, overflow["dormant"], conf["limit"])
        lines.append("")

    if not (listed["expired"] or listed["soon"] or listed["dormant"]):
        # 两个空状态严格区分：盘子里空 vs 有东西但都已报过
        lines.extend([f"**{DIGEST_EMPTY_TEXT if sel['qualified'] == 0 else DIGEST_NO_CHANGE_TEXT}**",
                      ""])

    # 未列入段：只报事实与条数（0 条不占一行），不做"这些都不值得看"这类判定。
    # 本段结构与本批之前完全一致（口径未动）。
    notes = []
    if counts["ignored"]:
        notes.append(f"- 你已忽略：{counts['ignored']} 条")
    if counts["young"]:
        notes.append(f"- 无时点但悬挂不足 {conf['dormant_days']} 天："
                     f"{counts['young']} 条")
    if counts["low_value"]:
        notes.append(f"- 无时点但 AI 初筛判价值偏低（< {conf['min_value_score']:g}）："
                     f"{counts['low_value']} 条")
    if counts["no_path"]:
        notes.append(f"- 无时点但够不着（可复用度偏低或资格不符）："
                     f"{counts['no_path']} 条")
    if far:
        preview = far[:_DIGEST_FAR_PREVIEW]
        notes.append(f"- 报名截止在 {cutoff.strftime('%Y-%m-%d')} 之后（尚未临期）："
                     f"{len(far)} 条，最近 {len(preview)} 条：")
        for _, row in preview:
            notes.append(_digest_sub_line(
                f"**{_digest_title(row)}**　"
                f"{deadline_display(row.get('deadline'), today=base)}"
                f"（{_normalize_deadline(row.get('deadline'))}）"))
        if len(far) > _DIGEST_FAR_PREVIEW:
            notes.append(_digest_sub_line(
                f"另有 {len(far) - _DIGEST_FAR_PREVIEW} 条未列出"))
    if counts["unscored"]:
        notes.append(f"- 无时点但**未经 AI 初筛**（价值与可达性未知，本格不替你判断）："
                     f"{counts['unscored']} 条 —— 在「💡 资源机会」页先点"
                     "「🔄 重新计算相关性」再点「🤖 AI 筛选前 5 条」（可多点几次），"
                     "它们才可能进本格")
    lines.append("**未列入（如实计数，不是判定）**")
    if notes:
        lines.extend(notes)
    elif not all_rows:
        lines.append("- 本地机会库当前一条都没有（还没扫描过，或扫描结果为空）")
    else:
        lines.append("- （本期没有未列入的条目）")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def classify_type(title: str, summary: str = "") -> str:
    """按关键词对机会做粗分类（title + summary 合并匹配，规则顺序即优先级）

    Returns:
        类型键（hackathon / competition / credit / course / scholarship / other）
    """
    blob = _clean_text(f"{title} {summary}").lower()
    if not blob:
        return "other"
    for type_key, keywords in CLASSIFY_RULES:
        for kw in keywords:
            if kw.lower() in blob:
                return type_key
    return "other"


def check_eligibility(title: str, summary: str = "",
                      profile=None):
    """评估机会是否符合用户资格（纯函数，确定性规则，无 LLM / 无成本）

    传入 profile（见 normalize_profile / memory.database.get_user_profile）
    时优先判断**硬性条件**（v0.3）：
    - 机会要求学校/教育邮箱而画像显示无学校邮箱 → likely_no（含原因）；
    - 文本含年龄门槛且画像年龄不满足 → likely_no（含原因）；
    - 文本限定到与画像地区不同的地区 → likely_no（含原因）；
    - 文本仅面向与画像不同的学生类型 → likely_no（含原因）。
    硬性条件全部满足（或未传 profile）后，再执行关键词规则：
    - 命中 INELIGIBLE_KEYWORDS（含学段错配词）→ likely_no；
    - 否则命中 ELIGIBLE_KEYWORDS（**仅 A 类可证伪的准入证据**）→ likely_yes；
    - 否则 unknown（P4-10 起：只命中 NEUTRAL_KEYWORDS 这类不可证伪的泛化词
      也算 unknown，reason 会写明命中了哪些中性词以便追溯）。
    冲突时**以不符合为准**（保守提示，宁可不推荐也不错荐）；
    规则仅作提示，不删除、不自动忽略任何机会。

    P4-10 子项 3 (2026-09-13) 语义变更（**有意的**）：`student` / `online` /
    `global` / `公开报名` 等词从"准入证据"降为"中性词"（拒绝型活动也这么写，
    不可证伪），`university` / `college` / `大学生` / `高校` 等学段错配词移入
    INELIGIBLE。因此本函数对**同一输入**可能给出比 v0.2 更保守的结果，这是修正
    方向性错误而非回归。

    Returns:
        (eligibility_hint, eligibility_reason) 二元组：
        hint 为 likely_yes / likely_no / unknown（见 EligibilityHint）；
        reason 为中文判断依据（unknown 时通常为空串，仅在命中中性词时给出说明）
    """
    blob = _clean_text(f"{title} {summary}").lower()
    if not blob:
        return EligibilityHint.UNKNOWN, ""
    if profile is not None:
        prof = normalize_profile(profile)
        block_reason, blob = _check_profile_hard(blob, prof)
        if block_reason:
            return EligibilityHint.LIKELY_NO, block_reason
    return _keyword_scan(blob)


def evidence_to_opportunity(ev: Evidence, beneficiary: str = "user",
                            profile=None) -> Optional[Opportunity]:
    """把一条搜索 Evidence 清洗为可入库的 Opportunity

    - 标题 / URL 必须可用，否则返回 None（不可点击的机会没有意义，
      同时也避免空串撞 source_url 唯一约束）；
    - 粗分类 type 由关键词规则给出；status 固定 new；beneficiary 默认 user；
    - notes 取自 Evidence.entities（一手源类目 + 来源冲突标记等展示分段，
      见 _notes_from_evidence）；搜索引擎源不带 entities，notes 即为空串；
    - deadline 取自 Evidence.published_at（P4-11）。一手源（魔搭）把
      **消解后**的 RegistrationDeadline（P6-3：两个来源冲突时取较晚者）换算成
      'YYYY-MM-DD' 放在该字段；搜索引擎源通常不带 published_at，deadline 即为
      空串（表示"未知"，见 _parse_deadline_date）；
    - eligibility_hint / eligibility_reason 由 check_eligibility 结合
      用户画像（profile=None 时不结合画像）按 title + summary 计算。
    """
    title = _clean_text(ev.title)
    url = _clean_text(ev.source_url)
    if not title or not url:
        return None
    summary = _clean_text(ev.snippet or "")[:SUMMARY_MAX_LEN]
    hint, reason = check_eligibility(title, summary, profile)
    # v0.4 相关性粗筛：强/弱/不相关关键词 + 软条件，随机会一起落库
    rel_status, rel_score, rel_pos, rel_neg = check_relevance(title, summary)
    rel_soft = check_soft_conditions(title, summary)
    deadline_text = _normalize_deadline(ev.published_at)
    return Opportunity(
        title=title,
        source_url=url,
        type=classify_type(title, summary),
        summary=summary,
        beneficiary=beneficiary or "user",
        status=OpportunityStatus.NEW,
        notes=_notes_from_evidence(ev, deadline_text),
        confidence=float(ev.confidence if ev.confidence is not None else 0.5),
        eligibility_hint=hint,
        eligibility_reason=(reason or "")[:200],
        relevance_status=rel_status,
        relevance_score=rel_score,
        matched_keywords=matched_keywords_to_json(rel_pos, rel_neg, rel_soft),
        deadline=deadline_text,
    )


# ---------- ResourceScout ----------

class ResourceScout:
    """外部资源机会扫描器（MVP v0.1）

    - scan_once()：用一组预定义查询 + 一组搜索 Provider 执行一轮同步扫描，
      结果清洗 / 分类 / 去重后写入 opportunities 表，返回统计报告；
    - 其余静态方法为 opportunities 表的 CRUD / 查询（UI 与测试共用）。

    构造参数均可注入（测试传入 Mock Provider / 少量查询即可离线运行）：
        queries: 扫描查询列表（默认 DEFAULT_QUERIES，5~8 条）
        providers: 搜索源列表（默认 get_default_providers() 双源）
        limit_per_query: 每个查询每来源最多取多少条结果
        timeout_sec: 单次搜索调用的超时秒数（网络保护，默认 15s）
    """

    def __init__(self, queries: Optional[List[str]] = None,
                 providers: Optional[List[SearchProvider]] = None,
                 limit_per_query: int = 5, timeout_sec: float = 15.0):
        # None → 默认值；显式传空列表则保持为空（便于边界测试与后续动态配置）
        self.queries = list(queries) if queries is not None \
            else list(DEFAULT_QUERIES)
        self.providers = list(providers) if providers is not None \
            else get_default_providers()
        self.limit_per_query = max(1, int(limit_per_query))
        self.timeout_sec = float(timeout_sec or 15.0)

    # ---------- 扫描 ----------

    def _call_search(self, provider, query: str, limit: int) -> List[Evidence]:
        """带超时的单次搜索调用：超时 / 异常一律按空结果处理（不拖垮整轮扫描）

        Provider 约定本身不抛异常，这里再加一层超时保护（如 DDGS 在极端
        网络场景可能长时间挂起）；等待超时后立即放弃该次调用。
        """
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(provider.search, query, limit)
            results = future.result(timeout=self.timeout_sec)
            return list(results or [])
        except Exception as e:
            print(f"[ResourceScout] 搜索调用失败/超时（忽略）: "
                  f"{getattr(provider, 'name', '?')} | {query[:40]} | {e}")
            return []
        finally:
            # wait=False：不等待可能仍卡在底层网络请求上的线程
            # （Python 3.9+ 线程池工作线程为守护线程，不会阻塞解释器退出）
            executor.shutdown(wait=False)

    def scan_once(self) -> dict:
        """执行一轮资源扫描，结果入库（重复 URL 自动跳过，幂等）

        Returns:
            报告 dict：
                queries: 实际执行的查询列表
                found: 搜索返回的有效结果条数（去重前）
                inserted: 本轮新入库条数
                duplicates: 重复条数（本轮内重复 + 库内已存在）
                skipped_invalid: 因缺标题/URL 被丢弃的条数
                errors: 执行中出现的问题描述列表（单条失败不中断）
                inserted_ids: 新入库机会的 ID 列表
                started_at / finished_at: 起止时间
                summary_text: 一句话汇总（控制台/UI 展示用）
        """
        started = now_str()
        report = {
            "queries": list(self.queries),
            "found": 0,
            "inserted": 0,
            "duplicates": 0,
            "skipped_invalid": 0,
            "errors": [],
            "inserted_ids": [],
            "started_at": started,
            "finished_at": "",
        }
        if not self.queries:
            report["errors"].append("未配置任何扫描查询")
            report["finished_at"] = now_str()
            report["summary_text"] = "扫描失败：未配置任何查询"
            return report
        if not self.providers:
            report["errors"].append("未配置任何搜索来源")
            report["finished_at"] = now_str()
            report["summary_text"] = "扫描失败：未配置任何搜索来源"
            return report

        # 轮内 URL 去重集合（同一轮多次命中同一 URL 只入库一次）
        seen_urls: set = set()
        # 读取当前用户画像（v0.3：资格过滤硬性条件判断用；无画像行时返回默认值）
        profile = get_user_profile()
        conn = get_db()
        try:
            for query in self.queries:
                for provider in self.providers:
                    try:
                        evidences = self._call_search(
                            provider, query, self.limit_per_query)
                    except Exception as e:  # 双保险：单次查询任何异常都不中断
                        report["errors"].append(f"查询「{query[:40]}」执行异常：{e}")
                        continue
                    for ev in evidences:
                        opp = evidence_to_opportunity(ev, profile=profile)
                        if opp is None:
                            report["skipped_invalid"] += 1
                            continue
                        report["found"] += 1
                        url_key = opp.source_url.lower()  # 规范化后再比较
                        if url_key in seen_urls:
                            report["duplicates"] += 1
                            continue
                        seen_urls.add(url_key)
                        try:
                            # INSERT OR IGNORE：库内唯一约束去重（跨轮扫描幂等）；
                            # eligibility_hint / eligibility_reason 由
                            # evidence_to_opportunity 结合画像计算；
                            # relevance_status / relevance_score /
                            # matched_keywords 由 v0.4 相关性粗筛计算；
                            # deadline 由一手源的 RegistrationDeadline 换算（P4-11）
                            cursor = conn.execute(
                                "INSERT OR IGNORE INTO opportunities "
                                "(title, type, source_url, summary, beneficiary, "
                                " status, notes, confidence, eligibility_hint, "
                                " eligibility_reason, relevance_status, "
                                " relevance_score, matched_keywords, deadline) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (opp.title, opp.type, opp.source_url,
                                 opp.summary, opp.beneficiary, opp.status,
                                 opp.notes, opp.confidence,
                                 opp.eligibility_hint, opp.eligibility_reason,
                                 opp.relevance_status, opp.relevance_score,
                                 opp.matched_keywords, opp.deadline),
                            )
                            conn.commit()
                            if cursor.rowcount > 0:
                                report["inserted"] += 1
                                report["inserted_ids"].append(cursor.lastrowid)
                            else:
                                report["duplicates"] += 1
                        except Exception as e:
                            report["errors"].append(
                                f"机会「{opp.title[:30]}」写入失败：{e}")
        finally:
            conn.close()

        report["finished_at"] = now_str()
        report["summary_text"] = (
            f"扫描 {len(report['queries'])} 个查询 × {len(self.providers)} 个来源："
            f"发现 {report['found']} 条，新入库 {report['inserted']} 条，"
            f"跳过重复 {report['duplicates']} 条，"
            f"丢弃无效 {report['skipped_invalid']} 条"
            + (f"，问题 {len(report['errors'])} 个" if report["errors"] else "")
        )
        return report

    # ---------- CRUD / 查询（全部静态方法 + 独立连接，线程安全） ----------

    @staticmethod
    def add_opportunity(title, source_url, type_key="other", summary="",
                        beneficiary="user", status=None, notes="",
                        confidence=0.5, eligibility_hint=None,
                        eligibility_reason="", deadline="") -> int:
        """新增一条机会；URL 已存在时忽略并返回 0（不抛异常）

        Args:
            eligibility_hint: 资格提示（likely_yes/likely_no/unknown）；
                None 或非法值时使用默认 'unknown'（列默认值语义一致）
            eligibility_reason: 资格判断的中文原因（默认空串 ''，
                与列默认值语义一致；非法类型回退空串）
            deadline: 报名截止日 'YYYY-MM-DD'（P4-11，默认 '' = 未知）。
                非法 / 空值一律存空串——**本参数不得接受猜测性日期**，
                调用方（含测试）只应传一手源换算出的真实报名截止日。

        Returns:
            新记录 ID；0 表示 URL 重复被跳过或参数非法
        """
        title = _clean_text(title)
        url = _clean_text(source_url)
        if not title or not url:
            print("[ResourceScout] 新增失败：标题/URL 不能为空")
            return 0
        status = status or OpportunityStatus.NEW
        if status not in OpportunityStatus.VALID:
            print(f"[ResourceScout] 新增失败：非法状态 {status}")
            return 0
        if eligibility_hint not in EligibilityHint.VALID:
            eligibility_hint = EligibilityHint.UNKNOWN
        if not isinstance(eligibility_reason, str):
            eligibility_reason = ""
        try:
            conn = get_db()
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO opportunities "
                    "(title, type, source_url, summary, beneficiary, status, "
                    " notes, confidence, eligibility_hint, eligibility_reason, "
                    " deadline) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, type_key or "other", url, summary or "",
                     beneficiary or "user", status, notes or "",
                     float(confidence if confidence is not None else 0.5),
                     eligibility_hint, eligibility_reason[:200],
                     _normalize_deadline(deadline)),
                )
                conn.commit()
                return cursor.lastrowid if cursor.rowcount > 0 else 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 机会写入失败: {e}")
            return 0

    @staticmethod
    def list_opportunities(status=None, limit: int = 200,
                           eligibility=None) -> List[dict]:
        """列出机会（按发现时间倒序，最新在前）

        Args:
            status: 非空时按状态过滤（new/interested/ignored/favorited）
            limit: 最多返回条数
            eligibility: 非空且非 'all' 时按资格提示过滤
                （likely_yes / likely_no / unknown）
        """
        try:
            conn = get_db()
            try:
                sql = "SELECT * FROM opportunities"
                conds: list = []
                params: list = []
                if status:
                    conds.append("status = ?")
                    params.append(status)
                if eligibility and eligibility != "all":
                    conds.append("eligibility_hint = ?")
                    params.append(eligibility)
                if conds:
                    sql += " WHERE " + " AND ".join(conds)
                sql += " ORDER BY discovered_at DESC, id DESC LIMIT ?"
                params.append(max(1, int(limit)))
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 机会列表读取失败: {e}")
            return []

    @staticmethod
    def get_opportunity(opportunity_id) -> Optional[dict]:
        """按 ID 读取单条机会；不存在返回 None"""
        try:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT * FROM opportunities WHERE id = ?",
                    (int(opportunity_id),),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 机会读取失败: {e}")
            return None

    @staticmethod
    def update_status(opportunity_id, status) -> bool:
        """更新机会状态（只允许白名单内的状态值）

        Returns:
            是否更新到行（ID 不存在或状态非法返回 False）
        """
        if status not in OpportunityStatus.VALID:
            print(f"[ResourceScout] 非法状态: {status}")
            return False
        try:
            conn = get_db()
            try:
                cursor = conn.execute(
                    "UPDATE opportunities SET status = ? WHERE id = ?",
                    (status, int(opportunity_id)),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 状态更新失败: {e}")
            return False

    @staticmethod
    def ignore_overdue(today=None) -> int:
        """把**报名截止日已过**的机会批量置为 ignored，返回改动条数

        定位（P7 批次 1b）：用户手动触发的「一键清理过期比赛」。清理后这些条目
        退出「资源」格与「会话启动对账单」（两者的可见口径都是
        `_BILL_VISIBLE_STATUSES`，ignored 不在其中），**但资源面板照旧列出它们**
        ——"本面板从不隐藏机会"是既定不变量，所以点完按钮列表看着没变化是正常的，
        变的是资源格与对账单。

        两条口径上的刻意选择：
        - **截止日一律在 Python 侧交给 `_parse_deadline_date` 判断**，不写
          `substr(deadline,1,10) < ?` 这类 SQL 比较：本仓已经有两处重复的截止日解析
          实现，再写一个 SQL 版就是第三处；而该函数还要处理空值/非法值——
          **`deadline` 为 NULL 表示"未知"，未知不等于已过期，绝不能一起清掉**。
        - **不按 type 过滤**：实测已过期的那些里就有带真实奖金的征文/其他类，
          类型与"值不值得看"无关。

        已忽略的条目不再重复处理（返回条数只计本次真正改动的行）。

        Args:
            today: 基准日期（默认当天；仅测试注入）

        Returns:
            被置为 ignored 的条数；无行可清返回 0；异常打印日志后返回 0（不抛，
            不影响 UI 主流程）。
        """
        base = today or date.today()
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id, deadline, status FROM opportunities").fetchall()
                ids = []
                for row in rows:
                    if row["status"] not in _BILL_VISIBLE_STATUSES:
                        continue
                    parsed = _parse_deadline_date(row["deadline"])
                    if parsed is not None and parsed < base:
                        ids.append(int(row["id"]))
                if not ids:
                    return 0
                placeholders = ",".join("?" * len(ids))
                cursor = conn.execute(
                    f"UPDATE opportunities SET status = ? WHERE id IN ({placeholders})",
                    [OpportunityStatus.IGNORED] + ids,
                )
                conn.commit()
                return cursor.rowcount or 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 过期清理失败: {e}")
            return 0

    @staticmethod
    def mark_digest_notified(items) -> int:
        """把"已经播报过的 (机会键, 桶)"写进 opportunities.notified_bucket（水位线）

        P7 批次 3：让「资源」格只播"新进入某个状态"的条目。**只写这一列**——
        不碰 status / deadline / notes / ai_* 任何一个字段（"绝不自动忽略任何机会"
        这条不变量在本方法同样成立）。

        **键契约（P7 批次 4 起有牙齿）**：只接受 `opportunities.id`（int）。
        `_digest_key` 在 `id` 缺失时会退回 `source_url` 字符串，而本方法的 SQL 是
        `WHERE id = ?` —— 那种键一条也匹配不上。它比抛异常更坏：`executemany` 的
        rowcount 照样大于 0（其余 int 键写成功），而唯一的 print 只在**抛异常**时发生
        ⇒ 整个破坏**完全无声**，那几条机会每期重复播报、水位线永远写不进去，界面上与
        日志里都不留痕迹。故现在：
          1) 写入前筛出 `isinstance(key, int)` 的项，被筛掉的**打印一行**
             （条数 + 前 3 个样例 + 一句"本方法只认 opportunities.id，这些条目下一期
             仍会播报"）；
          2) 写入后用 `cursor.rowcount` 与请求条数**对账**，不等再打印一行
             （对应"行在读取之后被删掉"）；
          3) 每次"没写成功"都必须留下一行打印——安静地少写几行是不可接受的。
        **刻意不加 `WHERE source_url = ?` 的第二分支**：那会造出两条写路径，并把
        "键契约被破坏"这件事继续藏起来。要的是让破坏出声。

        Args:
            items: [(机会键, 桶字符串), ...]；空列表返回 0

        Returns:
            实际写入的行数；异常返回 0（**不抛**——"水位线绝不拖垮报告生成"这条
            既有契约不变）。被跳过的非 int 键不计入返回值。
        """
        pairs = []
        skipped = []
        for key, bucket in (items or []):
            if isinstance(key, int):
                pairs.append((str(bucket), key))
            else:
                skipped.append(key)
        if skipped:
            print(f"[ResourceScout] 水位线跳过 {len(skipped)} 个非 int 键"
                  f"（样例：{skipped[:3]}）：本方法只认 opportunities.id，"
                  f"这些条目下一期仍会播报")
        if not pairs:
            return 0
        try:
            conn = get_db()
            try:
                cursor = conn.executemany(
                    "UPDATE opportunities SET notified_bucket = ? WHERE id = ?",
                    pairs,
                )
                conn.commit()
                written = cursor.rowcount or 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 水位线写入失败（忽略）: {e}")
            return 0
        if written != len(pairs):
            print(f"[ResourceScout] 水位线写入对账不符：请求 {len(pairs)} 行、"
                  f"实际写入 {written} 行（多半是行在读取之后被删掉了）"
                  f"——这些条目下一期仍会播报")
        return written

    @staticmethod
    def delete_opportunity(opportunity_id) -> bool:
        """删除一条机会；返回是否删除到行"""
        try:
            conn = get_db()
            try:
                cursor = conn.execute(
                    "DELETE FROM opportunities WHERE id = ?",
                    (int(opportunity_id),),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 机会删除失败: {e}")
            return False

    @staticmethod
    def count_opportunities(status=None) -> int:
        """统计机会条数（status 非空时按状态统计）"""
        try:
            conn = get_db()
            try:
                sql = "SELECT COUNT(*) AS c FROM opportunities"
                params: list = []
                if status:
                    sql += " WHERE status = ?"
                    params.append(status)
                row = conn.execute(sql, params).fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[ResourceScout] 机会计数失败: {e}")
            return 0

    @staticmethod
    def recompute_eligibility(profile=None) -> int:
        """按用户画像重新计算**全部存量机会**的资格提示与原因（v0.3）

        只更新 eligibility_hint / eligibility_reason 两列，不触碰其它字段；
        用于用户在 UI 修改画像后立即让已有机会套用新画像（"保存画像并
        重新计算资格"）。profile 为 None 时从 user_profile 表读取当前画像。

        Returns:
            实际更新行数（读库失败 / 异常返回 0，不抛异常）
        """
        if profile is None:
            profile = get_user_profile()
        prof = normalize_profile(profile)
        conn = get_db()
        try:
            try:
                rows = conn.execute(
                    "SELECT id, title, summary FROM opportunities"
                ).fetchall()
            except Exception as e:
                print(f"[ResourceScout] 机会读取失败（重算资格）: {e}")
                return 0
            updated = 0
            for row in rows:
                hint, reason = check_eligibility(
                    row["title"], row["summary"] or "", prof)
                cursor = conn.execute(
                    "UPDATE opportunities SET eligibility_hint = ?, "
                    "eligibility_reason = ? WHERE id = ?",
                    (hint, (reason or "")[:200], row["id"]),
                )
                updated += cursor.rowcount
            conn.commit()
            return updated
        finally:
            conn.close()

    @staticmethod
    def recompute_relevance() -> int:
        """重新计算**全部存量机会**的相关性粗筛结果（v0.4 存量修复）

        对每条机会按其 title + summary 重新执行 check_relevance /
        check_soft_conditions，并更新三列：
        relevance_status / relevance_score / matched_keywords。
        **只更新这三列**，不触碰资格提示 / 状态 / 画像等其它字段，也不
        删除任何机会；用于修复 v0.4 相关性落库链路启用前写入的旧记录
        （迁移默认 0.0 / unknown，导致 AI 深度筛选候选恒为 0），或关键词
        库更新后让存量机会套用新规则（UI「🔄 重新计算相关性」手动触发）。
        与 recompute_eligibility 同构：单连接批量 UPDATE 后一次性 commit。

        Returns:
            实际更新行数（读库失败 / 异常返回 0，不抛异常）
        """
        conn = get_db()
        try:
            try:
                rows = conn.execute(
                    "SELECT id, title, summary FROM opportunities"
                ).fetchall()
            except Exception as e:
                print(f"[ResourceScout] 机会读取失败（重算相关性）: {e}")
                return 0
            updated = 0
            for row in rows:
                title = row["title"] or ""
                summary = row["summary"] or ""
                rel_status, rel_score, rel_pos, rel_neg = check_relevance(
                    title, summary)
                rel_soft = check_soft_conditions(title, summary)
                cursor = conn.execute(
                    "UPDATE opportunities SET relevance_status = ?, "
                    "relevance_score = ?, matched_keywords = ? WHERE id = ?",
                    (rel_status, rel_score,
                     matched_keywords_to_json(rel_pos, rel_neg, rel_soft),
                     row["id"]),
                )
                updated += cursor.rowcount
            conn.commit()
            return updated
        finally:
            conn.close()
