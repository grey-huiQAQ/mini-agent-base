# -*- coding: utf-8 -*-
"""
Resource Scout 资格过滤（确定性规则 + 用户画像 v0.3）测试（离线：mock 搜索 Provider）

覆盖：
- 独立测试数据库：DB_PATH 指向临时库，不污染 data/memory.db（导入业务模块前切换）
- 数据库列迁移：opportunities.eligibility_hint（默认 'unknown'）与
  eligibility_reason（默认 ''）列存在、user_profile 表存在且默认行已播种、init_db 幂等
- 规则纯函数 check_eligibility：英文 / 中文关键词、未知、空文本、
  冲突时以"不符合"为准、大小写不敏感（v0.2 语义保持不变）
- 画像硬性条件（v0.3）：学校邮箱 / 年龄 / 地区 / 学生类型 →
  (likely_no, 中文原因)；画像满足后回退关键词规则
- user_profile 存储：默认值（高中生 / 无学校邮箱 / 中国）、更新持久化、
  非法学生类型拒绝、init_db 不覆盖已保存画像
- evidence_to_opportunity / scan_once / add_opportunity：hint + reason 落库
- recompute_eligibility：按新画像重算全部存量机会
- list_opportunities：资格筛选参数（单独筛选 / 与状态组合筛选）
- UI 回调契约：刷新 / 筛选 / 标记状态 / 保存画像并重算资格
- 两侧一致的保守派生值 effective_hint + 默认视图折叠（P4-10 子项 2）：
  折叠只影响展示，不落库、不改状态、不改写 eligibility_hint，折叠组仍在下拉框内
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：在导入任何会访问数据库的模块之前切换到临时库 ----
# P4-3a：统一走 tests/_test_db_helpers.setup_test_db()（临时库 + atexit 清理 +
# 残留自愈清扫）；保留 _TMP_DIR / _TMP_DB 两个旧变量名，既有断言无需改动。
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()
_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")

import memory.database as _database_module  # noqa: E402 （须在隔离生效之后导入）

from memory.database import (
    init_db,
    get_connection,
    get_user_profile,
    update_user_profile,
)
from core.resource_scout import (
    ResourceScout,
    OpportunityStatus,
    EligibilityHint,
    ELIGIBILITY_LABELS,
    ELIGIBLE_KEYWORDS,
    NEUTRAL_KEYWORDS,
    INELIGIBLE_KEYWORDS,
    check_eligibility,
    effective_hint,
    evidence_to_opportunity,
    normalize_profile,
    build_session_bill,
)
from core.information_engine.models import Evidence, now_str




def setup():
    """初始化独立测试数据库（触发列迁移 + user_profile 默认行播种）"""
    init_db()


def _reset_profile():
    """把画像重置为默认值（高中生 / 无学校邮箱 / 中国 / 年龄未填）

    直接更新单行 id=1，保证各画像相关测试互相独立、结果确定。
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE user_profile SET age = NULL, student_type = 'high_school', "
            "has_school_email = 0, region = '中国', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


# ---------- Mock 工具 ----------

class MockProvider:
    """测试用 mock 搜索源：固定返回预置证据"""

    def __init__(self, name="Mock", results=None):
        self.name = name
        self.results = list(results or [])
        self.search_calls = 0

    def search(self, query, limit=5):
        self.search_calls += 1
        return list(self.results)


def make_evidence(title, url, snippet="", published_at=None):
    """构造一条测试证据

    published_at：一手源把报名截止日换算成 'YYYY-MM-DD' 放在这里
    （见 ModelScopeProvider），落库时映射为 opportunities.deadline（P4-11）。
    """
    return Evidence(
        source_url=url, source_name="Mock", title=title,
        snippet=snippet or f"{title} 的摘要",
        confidence=0.7, retrieved_at=now_str(),
        published_at=published_at,
    )


def _clear_opportunities():
    """清空临时库中的机会行（保证各测试互相独立）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()


def _hint(title, summary="", profile=None):
    """便捷封装：只取 check_eligibility 的 hint（旧断言迁移到二元组用）"""
    return check_eligibility(title, summary, profile)[0]


# ---------- 1. 数据库列迁移与用户画像表 ----------

def test_eligibility_column_and_default():
    """迁移验证：eligibility_hint / eligibility_reason 列存在且默认值正确；幂等"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
    finally:
        conn.close()
    assert "eligibility_hint" in cols, \
        f"init_db 迁移后应存在 eligibility_hint 列，实际为 {cols}"
    assert "eligibility_reason" in cols, \
        f"init_db 迁移后应存在 eligibility_reason 列，实际为 {cols}"

    # 不传两列时默认 'unknown' / ''（列默认值语义）
    oid = ResourceScout.add_opportunity("默认资格的机会", "https://hint.example.com/d1")
    assert oid > 0
    opp = ResourceScout.get_opportunity(oid)
    assert opp["eligibility_hint"] == EligibilityHint.UNKNOWN, opp
    assert opp["eligibility_reason"] == "", opp

    # 重复 init_db 不报错（迁移幂等，不会重复加列 / 覆盖画像）
    init_db()
    init_db()
    print("[OK] eligibility columns exist with defaults, migration idempotent")


def test_deadline_column_both_init_paths():
    """P4-11：opportunities.deadline 两条 init_db 路径都要拿到该列

    ① 全新库：CREATE TABLE 里就带 deadline；
    ② 存量库：建一张**没有** deadline 列的老表 + 老数据，跑 init_db 后
       迁移补列（`_add_column_if_not_exists`），且老行的 deadline 为 NULL
       （空值语义 = "未知"，不做任何回填），老数据本身不丢。
    """
    import sqlite3

    assert _database_module.DB_PATH == _TMP_DB
    # ① 全新库（本测试文件已 init_db 过）
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
    finally:
        conn.close()
    assert "deadline" in cols, f"全新库应含 deadline 列，实际为 {cols}"

    # ② 存量库升级：临时把 DB_PATH 指向一个手工造的"老库"
    legacy = os.path.join(_TMP_DIR, "legacy_no_deadline.db")
    conn = sqlite3.connect(legacy)
    try:
        conn.execute(
            "CREATE TABLE opportunities ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
            " type TEXT DEFAULT 'other', source_url TEXT NOT NULL UNIQUE,"
            " summary TEXT, beneficiary TEXT DEFAULT 'user',"
            " status TEXT DEFAULT 'new', notes TEXT,"
            " confidence REAL DEFAULT 0.5,"
            " discovered_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            "INSERT INTO opportunities (title, source_url) VALUES (?, ?)",
            ("老机会（无 deadline 列）", "https://legacy.example.com/1"))
        conn.commit()
        before = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
        assert "deadline" not in before, before
    finally:
        conn.close()

    original = _database_module.DB_PATH
    try:
        _database_module.DB_PATH = legacy
        init_db()                       # 迁移：应补上 deadline 列
        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        try:
            after = [r[1] for r in conn.execute(
                "PRAGMA table_info(opportunities)").fetchall()]
            assert "deadline" in after, f"存量库迁移后应含 deadline 列：{after}"
            row = conn.execute("SELECT * FROM opportunities").fetchone()
            # 老行的 deadline 为 NULL（未知），且老数据未丢
            assert row["deadline"] is None, dict(row)
            assert row["title"] == "老机会（无 deadline 列）", dict(row)
            # 迁移幂等：再跑一次不报错、不改动
            init_db()
            assert [r[1] for r in conn.execute(
                "PRAGMA table_info(opportunities)").fetchall()] == after
        finally:
            conn.close()
    finally:
        _database_module.DB_PATH = original
    print("[OK] deadline column on both init paths (fresh + legacy upgrade)")


def test_notified_bucket_column_legacy_upgrade():
    """P7 批次 3：opportunities.notified_bucket 在两条 init_db 路径上都要拿到该列

    ① 全新库：init_db 后列存在（走 `_add_column_if_not_exists` 迁移，不写进
       CREATE TABLE——照 eligibility_hint 等列的既有惯例）；
    ② 存量库：建一张**没有**该列的老表 + 老数据，跑 init_db 后迁移补列，老行值为
       NULL（＝"从未播报"）。**不做存量回填**是设计：升级后第一期报告会把当前所有
       符合条件的条目播一遍，第二期才安静——这条用例把"老行必须是 NULL"钉死，
       免得日后有人为了"让第一期安静"偷偷回填。
    """
    import sqlite3

    assert _database_module.DB_PATH == _TMP_DB
    # ① 全新库
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
    finally:
        conn.close()
    assert "notified_bucket" in cols, f"全新库应含 notified_bucket 列，实际为 {cols}"

    # ② 存量库升级
    legacy = os.path.join(_TMP_DIR, "legacy_no_notified_bucket.db")
    conn = sqlite3.connect(legacy)
    try:
        conn.execute(
            "CREATE TABLE opportunities ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
            " type TEXT DEFAULT 'other', source_url TEXT NOT NULL UNIQUE,"
            " summary TEXT, beneficiary TEXT DEFAULT 'user',"
            " status TEXT DEFAULT 'new', notes TEXT,"
            " confidence REAL DEFAULT 0.5,"
            " discovered_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            "INSERT INTO opportunities (title, source_url) VALUES (?, ?)",
            ("老机会（无 notified_bucket 列）", "https://legacy.example.com/wm1"))
        conn.commit()
        before = [r[1] for r in conn.execute(
            "PRAGMA table_info(opportunities)").fetchall()]
        assert "notified_bucket" not in before, before
    finally:
        conn.close()

    original = _database_module.DB_PATH
    try:
        _database_module.DB_PATH = legacy
        init_db()                       # 迁移：应补上 notified_bucket 列
        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        try:
            after = [r[1] for r in conn.execute(
                "PRAGMA table_info(opportunities)").fetchall()]
            assert "notified_bucket" in after, \
                f"存量库迁移后应含 notified_bucket 列：{after}"
            row = conn.execute("SELECT * FROM opportunities").fetchone()
            assert row["notified_bucket"] is None, \
                f"老行必须是 NULL（从未播报，不回填）：{dict(row)}"
            assert row["title"] == "老机会（无 notified_bucket 列）", dict(row)
            # 迁移幂等：再跑一次不报错、列集不变
            init_db()
            assert [r[1] for r in conn.execute(
                "PRAGMA table_info(opportunities)").fetchall()] == after
        finally:
            conn.close()
    finally:
        _database_module.DB_PATH = original
    print("[OK] notified_bucket column on both init paths (fresh + legacy upgrade)")


def test_user_profile_table_and_defaults():
    """user_profile 表：列齐全、默认行已播种（高中生 / 无学校邮箱 / 中国）"""
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(user_profile)").fetchall()]
    finally:
        conn.close()
    for col in ["id", "age", "student_type", "has_school_email", "region",
                "updated_at"]:
        assert col in cols, f"user_profile 缺少列 {col}，实际为 {cols}"

    _reset_profile()
    profile = get_user_profile()
    assert profile["student_type"] == "high_school", profile
    assert profile["has_school_email"] is False, profile
    assert profile["region"] == "中国", profile
    assert profile["age"] is None, profile
    # 未初始化时也返回默认（不抛异常）
    print("[OK] user_profile table with default row seeded")


def test_user_profile_update_and_persist():
    """画像读写：整体更新、部分字段更新保留其它字段、init_db 不覆盖已保存值"""
    _reset_profile()
    saved = update_user_profile(student_type="undergraduate",
                                has_school_email=True, region="美国", age=20)
    assert saved == {"age": 20, "student_type": "undergraduate",
                     "has_school_email": True, "region": "美国"}, saved

    # 部分更新：只改地区，其它字段保留
    saved2 = update_user_profile(region="英国")
    assert saved2["region"] == "英国"
    assert saved2["student_type"] == "undergraduate"
    assert saved2["has_school_email"] is True
    assert saved2["age"] == 20

    # 非法学生类型 → 抛 ValueError，且不落库
    try:
        update_user_profile(student_type="ninja")
        assert False, "非法学生类型应抛 ValueError"
    except ValueError:
        pass
    assert get_user_profile()["student_type"] == "undergraduate"

    # 非正年龄按"清空"处理（置 None）
    cleared = update_user_profile(age=0)
    assert cleared["age"] is None

    # init_db 重复执行不得覆盖用户已保存的画像（INSERT OR IGNORE 语义）
    init_db()
    init_db()
    assert get_user_profile()["region"] == "英国"
    _reset_profile()
    print("[OK] user profile update & persist across init_db")


# ---------- 2. 规则纯函数 check_eligibility（v0.2 语义保持） ----------

def test_check_eligibility_english():
    """英文关键词：A 类准入证据 → 可能符合；限制词 → 可能不符合；未知

    P4-10 子项 3 (2026-09-13) 断言更正（理由，非放宽实现）：
    原用例把 `STUDENT CODE CAMP` + `global online event` 断言为 likely_yes，
    它编码的规则是"命中 student / online / global 即可能符合"。这三个词
    **不可证伪**——拒绝型活动也这么写（"International students only" 同样含
    international），故它们不构成准入证据；实测原表 27 条 likely_yes 的 42 次
    命中里没有一次是真实准入证据。该断言保护的不是"student 类活动应对高中生
    开放"这一合理预期（本用例第一条 `Hackathon for high school students` 走
    A 类 `high school`，仍是 likely_yes，预期不变），而是"不可证伪词应产生
    likely_yes"这一**已被判定为缺陷**的行为，故据实更正为 unknown。
    另新增 A 类词（open to all / 13+ / 零基础）的正向断言，确保"真准入证据
    仍能抬升"这条能力没有被本次收紧削弱。
    """
    assert _hint("Hackathon for high school students", "") == \
        EligibilityHint.LIKELY_YES
    # A 类准入证据仍能抬升（本次收紧不得削掉这条能力）
    assert _hint("Coding camp open to all", "") == EligibilityHint.LIKELY_YES
    assert _hint("Global AI challenge for teens", "") == \
        EligibilityHint.LIKELY_YES
    assert _hint("零基础也能参加的编程活动", "") == EligibilityHint.LIKELY_YES
    # B 类不可证伪词不再抬升（原断言编码的缺陷行为，据实更正）
    assert _hint("STUDENT CODE CAMP", "global online event") == \
        EligibilityHint.UNKNOWN
    assert _hint("Conference for professionals only", "") == \
        EligibilityHint.LIKELY_NO
    assert _hint("Event", "employee internal staff only") == \
        EligibilityHint.LIKELY_NO
    assert _hint("Unrelated general event", "") == \
        EligibilityHint.UNKNOWN
    # 二元组契约：hint 是第一个元素，reason 非空字符串
    hint, reason = check_eligibility("Hackathon for high school students", "")
    assert hint == EligibilityHint.LIKELY_YES and reason, (hint, reason)
    print("[OK] check eligibility english keywords")


def test_check_eligibility_chinese():
    """中文关键词：A 类准入证据 → 符合；在职/员工 → 不符合；学段错配 → 不符合

    P4-10 子项 3 (2026-09-13) 断言更正（理由，非放宽实现）：
    原用例把 `全国大学生程序设计竞赛` 与 `面向在校高校学生免费开放` 断言为
    likely_yes，其 docstring 写明规则是"学生/高校 → 符合"。但用户画像是
    **高中生**且无学校邮箱，"仅高校/大学生"对他就是硬门槛；这与
    `_detect_student_type_restrictions`（:519 一带）的学段逻辑**直接矛盾**。
    该断言保护的语义（"学生类活动应对高中生开放"）本身不成立——`大学生` /
    `高校` 恰恰是**排除**高中生的表述，故更正为 likely_no，并新增真正面向
    高中生的中文 A 类词正向断言。
    """
    assert _hint("全国大学生程序设计竞赛", "") == \
        EligibilityHint.LIKELY_NO          # C 类学段错配（原为 likely_yes）
    assert _hint("AI 训练营", "面向在校高校学生免费开放") == \
        EligibilityHint.LIKELY_NO          # C 类「高校」命中
    # A 类中文准入证据仍能抬升（本次收紧不得削掉这条能力）
    assert _hint("青少年编程挑战赛", "零基础也能参加") == \
        EligibilityHint.LIKELY_YES
    assert _hint("高中生 AI 夏令营", "不限年龄") == \
        EligibilityHint.LIKELY_YES
    assert _hint("某公司内部员工活动", "") == \
        EligibilityHint.LIKELY_NO
    assert _hint("行业交流会", "仅限在职从业人员参加") == \
        EligibilityHint.LIKELY_NO
    assert _hint("普通行业资讯", "一段普通介绍文字") == \
        EligibilityHint.UNKNOWN
    print("[OK] check eligibility chinese keywords")


def test_check_eligibility_conflict_prefers_no():
    """冲突规则：标题命中"符合"、摘要命中"不符合" → 以不符合为准（保守）"""
    assert _hint("student hackathon",
                 "participants must be 18+") == \
        EligibilityHint.LIKELY_NO
    assert _hint("大学生 编程训练营",
                 "仅限在职人员报名") == \
        EligibilityHint.LIKELY_NO
    print("[OK] check eligibility conflict prefers no")


def test_check_eligibility_empty():
    """空标题/空摘要 → unknown 且 reason 为空串（不误判）"""
    assert check_eligibility("", "") == (EligibilityHint.UNKNOWN, "")
    assert check_eligibility("   ", "  ") == (EligibilityHint.UNKNOWN, "")
    print("[OK] check eligibility empty text")


# ---------- 3. 画像硬性条件（v0.3） ----------

def test_check_eligibility_profile_email():
    """学校邮箱硬性条件：需要学校邮箱但画像没有 → likely_no + 原因"""
    no_email = {"has_school_email": False, "student_type": "high_school",
                "age": 16, "region": "中国"}
    has_email = {"has_school_email": True, "student_type": "high_school",
                 "age": 16, "region": "中国"}

    hint, reason = check_eligibility(
        "Student grant requires university email",
        "Applicants must have a .edu email address", no_email)
    assert hint == EligibilityHint.LIKELY_NO, (hint, reason)
    assert "学校邮箱" in reason or "邮箱" in reason, reason

    hint2, reason2 = check_eligibility(
        "Student grant requires university email",
        "Applicants must have a .edu email address", has_email)
    # 画像"有学校邮箱"时硬性条件放行 → 不得再出现"要求学校邮箱"这条阻断
    # （P4-10 子项 3：原断言要求 hint2 != likely_no 且文案含"命中开放关键词"。
    #  该行标题含 `university`，现属 C 类学段错配 → 仍会 likely_no，但**理由不同**
    #  且这正是修正后的正确行为：这条机会确实写着"university"，对一个高中生就是
    #  学段门槛，与邮箱无关。原断言把"有邮箱就该 likely_yes"和"student 抬升"
    #  两件不相干的事捆在一起，故据实更正为分别验证。）
    assert hint2 == EligibilityHint.LIKELY_NO, (hint2, reason2)
    assert "要求学校/教育邮箱" not in reason2, reason2
    assert "university" in reason2, reason2   # 阻断来自学段错配，不是邮箱
    # 对照：同一行在"无学校邮箱"画像下，邮箱硬性条件先命中（优先级 1）

    # 中文写法
    hint3, reason3 = check_eligibility("学生补贴", "申请者须提供学校邮箱", no_email)
    assert hint3 == EligibilityHint.LIKELY_NO and "学校邮箱" in reason3
    print("[OK] profile email hard constraint")


def test_check_eligibility_profile_age():
    """年龄硬性条件：门槛与画像年龄冲突 → likely_no + 原因；满足则放行"""
    age16 = {"has_school_email": True, "student_type": "high_school",
             "age": 16, "region": "中国"}
    age22 = {"has_school_email": True, "student_type": "undergraduate",
             "age": 22, "region": "中国"}

    hint, reason = check_eligibility("Coding camp",
                                     "open to students aged 18+ only", age16)
    assert hint == EligibilityHint.LIKELY_NO, (hint, reason)
    assert "18" in reason and "年龄" in reason, reason

    # 满足门槛：不再被「18+」关键词误杀，回退关键词 → A 类词抬升为可能符合
    # （P4-10 子项 3：输入改用 A 类 `open to all`。原输入只剩 `students` 这个
    #  不可证伪的中性词，收紧后按设计落 unknown——但本用例保护的语义是"年龄
    #  门槛已判定的片段被剔除、不再误杀"，改 A 类词后该语义照样被严格验证）
    hint2, reason2 = check_eligibility(
        "Coding camp", "open to all students aged 18+ only", age22)
    assert hint2 == EligibilityHint.LIKELY_YES, (hint2, reason2)
    assert "开放关键词" in reason2, reason2
    # 同一句只去掉 A 类词 → 仅剩中性词，落 unknown 且原因写明中性词（可追溯）
    hint2b, reason2b = check_eligibility(
        "Coding camp", "open to students aged 18+ only", age22)
    assert hint2b == EligibilityHint.UNKNOWN, (hint2b, reason2b)
    assert "中性词" in reason2b and "student" in reason2b, reason2b
    # 关键：18+ 片段已被剔除，两条都不因年龄被误杀
    assert "年龄" not in reason2 and "年龄" not in reason2b, (reason2, reason2b)

    # 区间上限：13-16 岁 → 22 岁超出
    hint3, _ = check_eligibility("青少年编程营", "面向 13-16 岁学生", age22)
    assert hint3 == EligibilityHint.LIKELY_NO, hint3

    # 中文年满
    hint4, _ = check_eligibility("编程大赛", "参赛者须年满18岁", age16)
    assert hint4 == EligibilityHint.LIKELY_NO, hint4
    print("[OK] profile age hard constraint")


def test_check_eligibility_profile_region():
    """地区硬性条件：限定到与画像地区不同的地区 → likely_no + 原因"""
    cn_user = {"has_school_email": True, "student_type": "high_school",
               "age": 16, "region": "中国"}

    hint, reason = check_eligibility("National competition",
                                     "open only to US residents", cn_user)
    assert hint == EligibilityHint.LIKELY_NO, (hint, reason)
    assert "地区" in reason and ("美国" in reason), reason

    hint2, reason2 = check_eligibility("全国比赛", "仅限美国学生参加", cn_user)
    assert hint2 == EligibilityHint.LIKELY_NO and "美国" in reason2, reason2

    # 限定中国内地 → 与画像地区一致，放行
    hint3, reason3 = check_eligibility("全国比赛", "仅限中国内地学生参加", cn_user)
    assert hint3 != EligibilityHint.LIKELY_NO, (hint3, reason3)

    # 画像地区未识别（自由文本兜底）时不误判
    hint4, _ = check_eligibility("National competition",
                                 "open only to US residents",
                                 dict(cn_user, region="火星"))
    assert hint4 != EligibilityHint.LIKELY_NO
    print("[OK] profile region hard constraint")


def test_check_eligibility_profile_student_type():
    """学生类型硬性条件：仅面向其它类型 → likely_no + 原因"""
    high_school = {"has_school_email": True, "student_type": "high_school",
                   "age": 16, "region": "中国"}
    undergrad = {"has_school_email": True, "student_type": "undergraduate",
                 "age": 20, "region": "中国"}

    hint, reason = check_eligibility("编程训练营", "仅限大学生报名", high_school)
    assert hint == EligibilityHint.LIKELY_NO, (hint, reason)
    assert "学生类型" in reason and "大学生" in reason, reason

    hint2, reason2 = check_eligibility("编程训练营", "仅限大学生报名", undergrad)
    # 该行在 undergrad 画像下**不再被学生类型硬性条件阻断**——这是本用例保护的
    # 语义。（P4-10 子项 3：`大学生` 现属 C 类学段错配词，故关键词轴仍会给出
    #  likely_no；但那与"学生类型不匹配"是两回事，且关键词轴只能降级不能升级，
    #  无法在硬性条件放行后把它抬回去。原断言 `hint2 != likely_no` 把
    #  "硬性条件放行" 与 "关键词轴不命中" 捆在一起，据实拆开验证。）
    assert "仅面向" not in reason2, reason2
    hint3, reason3 = check_eligibility("高中竞赛", "面向高中生的比赛", high_school)
    assert "仅面向" not in reason3, reason3
    assert hint3 != EligibilityHint.LIKELY_NO, hint3   # A 类「高中生」抬升

    # 英文限定句式
    hint4, reason4 = check_eligibility(
        "Research camp", "exclusive to graduate students only", high_school)
    assert hint4 == EligibilityHint.LIKELY_NO, (hint4, reason4)
    print("[OK] profile student type hard constraint")


def test_check_eligibility_profile_partial_defaults():
    """部分画像字段（如只传 student_type）按默认值补齐"""
    p = normalize_profile({"student_type": "graduate"})
    assert p["has_school_email"] is False and p["region"] == "中国"
    assert p["age"] is None
    # 空 dict → 完全默认（高中生无邮箱 → 需要学校邮箱的机会为不符）
    hint, reason = check_eligibility("学生补贴", "申请者须提供学校邮箱", {})
    assert hint == EligibilityHint.LIKELY_NO and "学校邮箱" in reason
    print("[OK] partial profile defaults")


# ---------- 4. 落库流程（hint + reason） ----------

def test_evidence_to_opportunity_sets_hint():
    """evidence_to_opportunity：按 title+summary 计算 hint（不传画像时走关键词轴）

    P4-10 子项 3 (2026-09-13) 断言更正（理由）：原用例把
    `AI Bootcamp for Students` + `online course` 断言为 likely_yes，它依赖的是
    `student` / `online` 抬升——这两个词不可证伪，已被判定为缺陷，故改为
    unknown 且要求 reason 写明命中的中性词（可追溯）。同时新增一条 A 类
    （`open to all`）正向断言，确保"真准入证据仍抬升"没被收紧削掉。
    """
    # A 类准入证据 → likely_yes
    yes = evidence_to_opportunity(
        make_evidence("AI Bootcamp open to all", "https://e.example.com/yes",
                      snippet="beginner friendly"))
    assert yes is not None and yes.eligibility_hint == EligibilityHint.LIKELY_YES
    assert "开放关键词" in yes.eligibility_reason, yes.eligibility_reason
    # 仅中性词（student / online）→ unknown，但 reason 记录命中词
    neutral = evidence_to_opportunity(
        make_evidence("AI Bootcamp for Students", "https://e.example.com/neutral",
                      snippet="online course"))
    assert neutral is not None and \
        neutral.eligibility_hint == EligibilityHint.UNKNOWN
    assert "中性词" in neutral.eligibility_reason, neutral.eligibility_reason
    no = evidence_to_opportunity(
        make_evidence("For professionals", "https://e.example.com/no"))
    assert no is not None and no.eligibility_hint == EligibilityHint.LIKELY_NO
    unknown = evidence_to_opportunity(
        make_evidence("General resource", "https://e.example.com/unknown"))
    assert unknown is not None and \
        unknown.eligibility_hint == EligibilityHint.UNKNOWN
    print("[OK] evidence to opportunity sets eligibility hint")


def test_evidence_to_opportunity_profile_sets_reason():
    """evidence_to_opportunity（带画像）：reason 随 hint 一起写入"""
    no_email = {"has_school_email": False, "student_type": "high_school",
                "age": 16, "region": "中国"}
    opp = evidence_to_opportunity(
        make_evidence("学生补贴（需学校邮箱）",
                      "https://e.example.com/email"),
        profile=no_email)
    assert opp is not None
    assert opp.eligibility_hint == EligibilityHint.LIKELY_NO
    assert opp.eligibility_reason and "学校邮箱" in opp.eligibility_reason, \
        opp.eligibility_reason
    # 关键词路径也会带 reason
    # （P4-10 子项 3：改用 A 类 `open to all`；原用 `Student GPU credits`，
    #  仅命中中性词 `student`，收紧后按设计落 unknown，reason 改为写明中性词）
    plain = evidence_to_opportunity(
        make_evidence("GPU credits open to all", "https://e.example.com/yes2"),
        profile=no_email)
    assert plain.eligibility_reason and "开放关键词" in plain.eligibility_reason
    neutral = evidence_to_opportunity(
        make_evidence("Student GPU credits", "https://e.example.com/yes3"),
        profile=no_email)
    assert neutral.eligibility_hint == EligibilityHint.UNKNOWN
    assert "中性词" in neutral.eligibility_reason, neutral.eligibility_reason
    print("[OK] evidence to opportunity sets eligibility reason")


def test_scan_once_persists_hint():
    """scan_once（mock 搜索）：资格提示随机会一起落库"""
    _clear_opportunities()
    provider = MockProvider(name="Mock", results=[
        make_evidence("GPU credits open to all", "https://scan.example.com/yes"),
        make_evidence("For professionals only", "https://scan.example.com/no"),
        make_evidence("普通资源页", "https://scan.example.com/unknown"),
    ])
    scout = ResourceScout(queries=["资格扫描查询"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 3, report
    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities()}
    assert rows["https://scan.example.com/yes"]["eligibility_hint"] == \
        EligibilityHint.LIKELY_YES
    assert rows["https://scan.example.com/no"]["eligibility_hint"] == \
        EligibilityHint.LIKELY_NO
    assert rows["https://scan.example.com/unknown"]["eligibility_hint"] == \
        EligibilityHint.UNKNOWN
    print("[OK] scan once persists eligibility hint")


def test_scan_once_persists_reason_with_profile():
    """scan_once + 画像：资格原因随画像落库；改画像 + 重算后原因联动更新"""
    _clear_opportunities()
    _reset_profile()  # 默认画像：无学校邮箱
    provider = MockProvider(name="Mock", results=[
        make_evidence("学生补贴（需学校邮箱）", "https://scan2.example.com/1"),
    ])
    scout = ResourceScout(queries=["画像扫描查询"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 1, report
    row = ResourceScout.get_opportunity(report["inserted_ids"][0])
    assert row["eligibility_hint"] == EligibilityHint.LIKELY_NO, row
    assert row["eligibility_reason"] and "学校邮箱" in row["eligibility_reason"], \
        row["eligibility_reason"]

    # 用户补充学校邮箱 → 重算存量：该行不再被邮箱硬性条件阻断
    # （P4-10 子项 3：原断言要求重算后升到 likely_yes 且文案含"命中开放关键词"，
    #  它依赖 `学生` 抬升——`学生` 已判为不可证伪的中性词，故重算后按设计落
    #  unknown。本用例真正保护的语义是"改画像 + 重算 → 原因联动更新"，这一条
    #  仍被严格断言：原因从"要求学校/教育邮箱"变成了命中中性词的说明。）
    update_user_profile(has_school_email=True)
    updated = ResourceScout.recompute_eligibility()
    assert updated == 1, updated
    row2 = ResourceScout.get_opportunity(report["inserted_ids"][0])
    assert row2["eligibility_hint"] != EligibilityHint.LIKELY_NO, row2
    assert "学校邮箱" not in row2["eligibility_reason"], row2["eligibility_reason"]
    assert "中性词" in row2["eligibility_reason"], row2["eligibility_reason"]
    _reset_profile()
    print("[OK] scan once persists eligibility reason with profile")


def test_add_opportunity_hint_handling():
    """add_opportunity：显式 hint 生效；非法 hint 回退 unknown"""
    _clear_opportunities()
    oid = ResourceScout.add_opportunity("显式符合", "https://add.example.com/1",
                                        eligibility_hint=EligibilityHint.LIKELY_YES)
    assert oid > 0
    assert ResourceScout.get_opportunity(oid)["eligibility_hint"] == \
        EligibilityHint.LIKELY_YES
    oid2 = ResourceScout.add_opportunity("非法值", "https://add.example.com/2",
                                         eligibility_hint="bogus")
    assert ResourceScout.get_opportunity(oid2)["eligibility_hint"] == \
        EligibilityHint.UNKNOWN, "非法 hint 应回退 unknown"
    print("[OK] add opportunity hint handling")


def test_add_opportunity_reason_handling():
    """add_opportunity：显式 reason 落库；缺省 / 非法 reason 回退空串"""
    _clear_opportunities()
    oid = ResourceScout.add_opportunity("带原因", "https://add2.example.com/1",
                                        eligibility_hint=EligibilityHint.LIKELY_NO,
                                        eligibility_reason="需要学校邮箱")
    assert oid > 0
    opp = ResourceScout.get_opportunity(oid)
    assert opp["eligibility_reason"] == "需要学校邮箱", opp

    oid2 = ResourceScout.add_opportunity("默认原因", "https://add2.example.com/2")
    assert ResourceScout.get_opportunity(oid2)["eligibility_reason"] == ""

    oid3 = ResourceScout.add_opportunity("非法原因", "https://add2.example.com/3",
                                         eligibility_reason=12345)
    assert ResourceScout.get_opportunity(oid3)["eligibility_reason"] == ""
    print("[OK] add opportunity reason handling")


# ---------- 5. 列表资格筛选 ----------

def test_list_opportunities_eligibility_filter():
    """list_opportunities：按资格筛选 / 与状态组合筛选"""
    _clear_opportunities()
    id_yes1 = ResourceScout.add_opportunity("符合A", "https://f.example.com/a1",
                                            eligibility_hint=EligibilityHint.LIKELY_YES)
    id_yes2 = ResourceScout.add_opportunity("符合B", "https://f.example.com/a2",
                                            eligibility_hint=EligibilityHint.LIKELY_YES)
    id_no = ResourceScout.add_opportunity("不符C", "https://f.example.com/b1",
                                          eligibility_hint=EligibilityHint.LIKELY_NO)
    id_unk = ResourceScout.add_opportunity("未知D", "https://f.example.com/c1")
    assert id_yes1 and id_yes2 and id_no and id_unk

    # 不过滤（None / 'all'）返回全部
    assert len(ResourceScout.list_opportunities(limit=50)) == 4
    assert len(ResourceScout.list_opportunities(eligibility="all")) == 4
    # 按资格过滤
    yes_rows = ResourceScout.list_opportunities(eligibility=EligibilityHint.LIKELY_YES)
    assert len(yes_rows) == 2 and all(
        r["eligibility_hint"] == EligibilityHint.LIKELY_YES for r in yes_rows)
    no_rows = ResourceScout.list_opportunities(eligibility=EligibilityHint.LIKELY_NO)
    assert [r["id"] for r in no_rows] == [id_no]
    unk_rows = ResourceScout.list_opportunities(eligibility=EligibilityHint.UNKNOWN)
    assert [r["id"] for r in unk_rows] == [id_unk]

    # 与状态组合筛选：把一个"符合"机会标记感兴趣后组合查询
    assert ResourceScout.update_status(id_yes1, OpportunityStatus.INTERESTED)
    combo = ResourceScout.list_opportunities(
        status=OpportunityStatus.INTERESTED,
        eligibility=EligibilityHint.LIKELY_YES)
    assert [r["id"] for r in combo] == [id_yes1]
    print("[OK] list opportunities eligibility filter")


def test_recompute_eligibility_updates_rows():
    """recompute_eligibility：按画像重算全部存量机会（只动资格两列）"""
    _clear_opportunities()
    _reset_profile()
    id1 = ResourceScout.add_opportunity("学生补贴（需学校邮箱）",
                                        "https://rc.example.com/1")
    id2 = ResourceScout.add_opportunity("面向高中生的竞赛",
                                        "https://rc.example.com/2")
    assert id1 and id2
    profile = get_user_profile()  # 默认：高中生 / 无学校邮箱 / 中国
    updated = ResourceScout.recompute_eligibility(profile)
    assert updated == 2, updated

    opp1 = ResourceScout.get_opportunity(id1)
    assert opp1["eligibility_hint"] == EligibilityHint.LIKELY_NO, opp1
    assert "学校邮箱" in opp1["eligibility_reason"], opp1
    opp2 = ResourceScout.get_opportunity(id2)
    assert opp2["eligibility_hint"] == EligibilityHint.LIKELY_YES, opp2
    # 其它字段不受影响
    assert opp1["title"] == "学生补贴（需学校邮箱）"
    assert opp2["status"] == OpportunityStatus.NEW
    print("[OK] recompute eligibility updates all rows")


# ---------- 6. UI 筛选 / 画像回调契约 ----------

def test_ui_filter_callback_contract():
    """UI 筛选回调：按资格筛选刷新列表、标记状态后保持筛选视图"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 筛选回调契约测试")
        return

    _clear_opportunities()
    # 直接插入不同资格提示的机会（不联网、不触发扫描线程，确定性）
    id_yes = ResourceScout.add_opportunity(
        "资格符合的竞赛", "https://ui.example.com/yes",
        eligibility_hint=EligibilityHint.LIKELY_YES)
    id_no = ResourceScout.add_opportunity(
        "专业人士专享活动", "https://ui.example.com/no",
        eligibility_hint=EligibilityHint.LIKELY_NO)
    assert id_yes and id_no

    # 1) 默认「全部」：列表含全部机会
    md_all, dd_all = app_module.refresh_opportunities_panel(None, "all")
    assert "共 2 条机会" in md_all, md_all
    assert "资格符合的竞赛" in md_all and "专业人士专享活动" in md_all
    # 列表行内展示资格标签
    assert "可能符合" in md_all and "可能不符" in md_all, md_all

    # 2) 按「可能符合」筛选：只展示命中机会，下拉框只含该项
    md_yes, dd_yes = app_module.refresh_opportunities_panel(None, "likely_yes")
    assert "资格符合的竞赛" in md_yes, md_yes
    assert "专业人士专享活动" not in md_yes, md_yes
    assert "资格筛选：✅ 可能符合" in md_yes, md_yes
    choices = dd_yes["choices"]
    assert len(choices) == 1 and choices[0][1] == id_yes, choices

    # 3) 无匹配的筛选（未知）：友好空提示而非崩溃
    md_empty, dd_empty = app_module.refresh_opportunities_panel(None, "unknown")
    assert "该资格筛选" in md_empty and "暂无机会" in md_empty, md_empty
    assert dd_empty["choices"] == []

    # 4) 标记状态（携带筛选）→ 三元组契约 + 保持筛选视图 + 选中值保留
    res = app_module.do_set_opportunity_status(
        id_yes, OpportunityStatus.INTERESTED, "likely_yes")
    assert isinstance(res, tuple) and len(res) == 3, \
        "do_set_opportunity_status 必须返回三元组以匹配 outputs"
    msg, md_after, dd_after = res
    assert "感兴趣" in msg, msg
    assert "资格符合的竞赛" in md_after, md_after
    assert "专业人士专享活动" not in md_after, md_after
    assert dd_after["value"] == id_yes, dd_after  # 筛选视图下选中值保留
    opp = ResourceScout.get_opportunity(id_yes)
    assert opp["status"] == OpportunityStatus.INTERESTED

    # 5) 未选中机会 → 友好提示（携带筛选不崩溃）
    res_none = app_module.do_set_opportunity_status(
        None, OpportunityStatus.FAVORITED, "likely_yes")
    assert isinstance(res_none, tuple) and len(res_none) == 3
    assert "请先在下拉框中选择一条机会" in res_none[0]
    print(f"[OK] ui filter callback contract flow (opportunities #{id_yes}, #{id_no})")


def test_ui_profile_callback_contract():
    """UI 画像回调：保存画像 → 更新 user_profile + 重算存量机会 + 列表展示原因"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 画像回调契约测试")
        return

    _clear_opportunities()
    _reset_profile()
    # 插入一条需要学校邮箱的机会（先用默认画像保存 → 应判为可能不符并附原因）
    # P4-10 子项 3：标题保留"需学校邮箱"并补 A 类词 `零基础`——否则步骤 3 放行后
    # 只剩中性词 `学生`，按收紧后的规则落 unknown，本用例就失去了"放行 → 可能符合"
    # 这条正向断言。（不能用 `高中生` 作 A 类词：步骤 3 会把画像切成大学生，
    #  那时它会同时命中学生类型硬性条件而仍然 likely_no。）
    oid = ResourceScout.add_opportunity(
        "零基础可参加的学生补贴（需学校邮箱）", "https://ui-profile.example.com/1")
    assert oid > 0

    # 1) 保存默认画像（高中生 / 无学校邮箱 / 中国 / 年龄 16）
    res = app_module.do_save_user_profile("high_school", False, "中国", 16)
    assert isinstance(res, tuple) and len(res) == 3, \
        "do_save_user_profile 必须返回三元组以匹配 outputs"
    msg, md, dd_update = res
    assert "已保存" in msg and "重算 1 条" in msg, msg
    assert "高中生" in msg and "无学校邮箱" in msg, msg
    profile = get_user_profile()
    assert profile["student_type"] == "high_school"
    assert profile["has_school_email"] is False
    assert profile["region"] == "中国"
    assert profile["age"] == 16

    # 2) 存量机会已被重算：可能不符 + 原因展示在列表 Markdown 中
    opp = ResourceScout.get_opportunity(oid)
    assert opp["eligibility_hint"] == EligibilityHint.LIKELY_NO, opp
    assert "学校邮箱" in opp["eligibility_reason"], opp
    assert "学生补贴（需学校邮箱）" in md and "可能不符" in md, md
    assert "学校邮箱" in md, "列表应展示资格判断原因"
    assert "💬" in md, "原因行应有 💬 前缀"

    # 3) 改为有学校邮箱的大学生 → 保存后该机会放行（回退关键词 → 可能符合）
    res2 = app_module.do_save_user_profile("undergraduate", True, "美国", 20)
    msg2, md2, _ = res2
    assert "已保存" in msg2 and "重算 1 条" in msg2, msg2
    opp2 = ResourceScout.get_opportunity(oid)
    assert opp2["eligibility_hint"] == EligibilityHint.LIKELY_YES, opp2
    assert "可能符合" in md2, md2
    # 原因行文案随 hint 更新
    assert "命中开放关键词" in opp2["eligibility_reason"], opp2

    # 4) 非法学生类型不会崩溃（UI 层先校验回退默认）
    res3 = app_module.do_save_user_profile("bad_type", True, "中国", 20)
    assert isinstance(res3, tuple) and len(res3) == 3
    assert get_user_profile()["student_type"] == "high_school", \
        "非法学生类型应回退默认并不落库"
    _reset_profile()
    print(f"[OK] ui profile callback contract flow (opportunity #{oid})")


def test_ui_status_filter_contract():
    """UI 状态筛选（v0.4 历史机会查看）：按状态过滤、与资格组合、
    标记状态后保持视图（离开视图时自动移除并友好提示）"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过 UI 状态筛选契约测试")
        return

    _clear_opportunities()
    id_new = ResourceScout.add_opportunity(
        "新发现编程竞赛", "https://st.example.com/new",
        eligibility_hint=EligibilityHint.LIKELY_YES)
    id_int = ResourceScout.add_opportunity(
        "已感兴趣的课程", "https://st.example.com/int",
        eligibility_hint=EligibilityHint.LIKELY_YES)
    id_ign = ResourceScout.add_opportunity(
        "已忽略的旧机会", "https://st.example.com/ign",
        eligibility_hint=EligibilityHint.LIKELY_NO)
    assert id_new and id_int and id_ign
    assert ResourceScout.update_status(id_int, OpportunityStatus.INTERESTED)
    assert ResourceScout.update_status(id_ign, OpportunityStatus.IGNORED)

    # 1) 默认「全部」：三行都展示
    md_all, _ = app_module.refresh_opportunities_panel(None, "all", "all")
    assert "共 3 条机会" in md_all, md_all

    # 2) 按状态筛选 interested：只展示该行；下拉框只含该 id
    md_int, dd_int = app_module.refresh_opportunities_panel(
        None, "all", "interested")
    assert "已感兴趣的课程" in md_int, md_int
    assert "新发现编程竞赛" not in md_int, md_int
    assert "共 1 条机会" in md_int and "状态筛选：😍 感兴趣" in md_int, md_int
    assert [c[1] for c in dd_int["choices"]] == [id_int], dd_int

    # 3) 与资格筛选组合：状态 + 资格 AND 过滤
    md_combo, _ = app_module.refresh_opportunities_panel(
        None, "likely_yes", "interested")
    assert "已感兴趣的课程" in md_combo and "共 1 条机会" in md_combo, md_combo
    md_miss, dd_miss = app_module.refresh_opportunities_panel(
        None, "likely_yes", "ignored")
    assert "暂无机会" in md_miss and dd_miss["choices"] == [], md_miss

    # 4) 在状态视图「new」下把该行标记为 interested → 离开视图：
    #    列表自动移除该行、下拉清空，友好提示而非崩溃
    res = app_module.do_set_opportunity_status(
        id_new, OpportunityStatus.INTERESTED, "all", "new")
    assert isinstance(res, tuple) and len(res) == 3
    msg, md_after, dd_after = res
    assert "感兴趣" in msg, msg
    assert "新发现编程竞赛" not in md_after, md_after
    assert "暂无机会" in md_after, md_after
    assert dd_after["choices"] == [] and dd_after["value"] is None, dd_after
    opp = ResourceScout.get_opportunity(id_new)
    assert opp["status"] == OpportunityStatus.INTERESTED, opp

    # 5) 「全部」视图下标记状态：行保留、选中值保留（标记后仍在视图内）
    res2 = app_module.do_set_opportunity_status(
        id_int, OpportunityStatus.FAVORITED, "all", "all")
    assert isinstance(res2, tuple) and len(res2) == 3
    md2, dd2 = res2[1], res2[2]
    assert "已感兴趣的课程" in md2, md2
    assert dd2["value"] == id_int, dd2
    print(f"[OK] ui status filter contract flow (#{id_new}, #{id_int}, #{id_ign})")


def test_effective_hint_derivation():
    """effective_hint 派生口径：降级任一信号即可、升级需两个独立信号同意

    P4-10 子项 2：规则轴与 AI 轴各自都会误判（规则轴尤其会把"未明说禁止"
    当成"准入证据"），故合并口径取保守值。本用例锁死这张真值表。
    """
    Y, N, U = (EligibilityHint.LIKELY_YES, EligibilityHint.LIKELY_NO,
               EligibilityHint.UNKNOWN)
    # 任一 no → no（降级只需一个信号）
    assert effective_hint(Y, N) == N
    assert effective_hint(N, Y) == N
    assert effective_hint(N, U) == N
    assert effective_hint(U, N) == N
    assert effective_hint(N, N) == N
    # 两侧都 yes → yes（升级需两个独立信号同意）
    assert effective_hint(Y, Y) == Y
    # 只有一侧 yes → unknown（不得升级）
    assert effective_hint(Y, U) == U
    assert effective_hint(U, Y) == U
    # 其它组合 → unknown
    assert effective_hint(U, U) == U
    # 未 AI 筛选（列值 None）按"未同意"处理，不因缺列而升级
    assert effective_hint(Y, None) == U
    assert effective_hint(None, None) == U
    assert effective_hint(None, N) == N
    # 返回值必须落在合法域内
    for a in (Y, N, U, None, ""):
        for b in (Y, N, U, None, ""):
            assert effective_hint(a, b) in EligibilityHint.VALID
    print("[OK] effective hint derivation")


def test_ui_fold_default_view_without_db_mutation():
    """默认视图折叠：只折叠不落库、不改状态、不改写 eligibility_hint

    同时锁死三条不得被破坏的性质：
    1) 折叠组的行仍在**下拉框**里（可被选中/标记，不因折叠而不可达）；
    2) eligibility='all' 的既有语义保持（默认视图仍展示全部机会）；
    3) 用户显式筛某资格值时**不折叠**（他要看的正是那一组）。
    """
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过折叠契约测试")
        return

    _clear_opportunities()
    conn = get_connection()
    try:
        # 直接插库以便给 ai_eligibility 赋值（add_opportunity 不暴露该列）
        rows = [
            ("可见：两侧一致yes", "likely_yes", "likely_yes", "new"),
            ("折叠：规则yes但AIno", "likely_yes", "likely_no", "new"),
            ("折叠：规则no", "likely_no", None, "new"),
            ("可见：两侧unknown", "unknown", "unknown", "new"),
        ]
        ids = []
        for i, (t, rh, ah, st) in enumerate(rows):
            cur = conn.execute(
                "INSERT INTO opportunities (title, type, source_url, summary,"
                " status, eligibility_hint, ai_eligibility)"
                " VALUES (?,?,?,?,?,?,?)",
                (t, "other", f"https://fold.example.com/{i}", "摘要", st, rh, ah))
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    before = {r["id"]: dict(r) for r in ResourceScout.list_opportunities(limit=50)}

    # 默认视图：2 条折叠（#2 #3），2 条可见；总数仍为 4（'all' 语义不变）
    md_all, dd_all = app_module.refresh_opportunities_panel(None, "all", "all")
    assert "共 4 条机会" in md_all, md_all
    assert "已折叠 2 条" in md_all, md_all
    assert "<details>" in md_all and "</details>" in md_all, md_all
    # 折叠摘要行必须点明原因，且展开入口存在
    assert "点击展开" in md_all, md_all
    # 4 条全部在下拉框内 —— 折叠不得让任何行不可达
    assert sorted(c[1] for c in dd_all["choices"]) == sorted(ids), dd_all["choices"]
    # 折叠组内的行标题仍在（在折叠容器内），说明没有被删除
    assert "折叠：规则yes但AIno" in md_all, md_all

    # 不改库：刷新前后每一列都必须逐字一致
    after = {r["id"]: dict(r) for r in ResourceScout.list_opportunities(limit=50)}
    assert before == after, "刷新面板（含折叠）不得改动任何库内字段"
    # 派生值不得回写 eligibility_hint
    assert after[ids[1]]["eligibility_hint"] == "likely_yes", after[ids[1]]
    assert after[ids[2]]["status"] == "new", after[ids[2]]

    # 显式筛 likely_no：不折叠（用户点名的组必须直接可见）
    md_no, dd_no = app_module.refresh_opportunities_panel(None, "likely_no", "all")
    assert "<details>" not in md_no, md_no
    assert [c[1] for c in dd_no["choices"]] == [ids[2]], dd_no["choices"]

    # 显式筛状态：同样不折叠
    md_st, _ = app_module.refresh_opportunities_panel(None, "all", "new")
    assert "<details>" not in md_st, md_st
    print(f"[OK] ui fold default view without db mutation (#{ids})")


def test_eligibility_keyword_three_way_split():
    """关键词表三分法不被悄悄回退（P4-10 子项 3 的核心不变量）

    背景：原 ELIGIBLE_KEYWORDS（41 词）把"不可证伪"的泛化词与真实准入证据混在
    一起，命中即 likely_yes，于是 27/36 行被判可能符合而 42 次命中里没有一次是
    真实准入证据。

    本用例锁死三件事：
    1) 不可证伪的词**不得**出现在 ELIGIBLE_KEYWORDS（否则缺陷会悄悄回来）；
    2) 学段错配词**必须**在 INELIGIBLE_KEYWORDS（否则又会给高中生铸 yes）；
    3) 三者互不重叠，且合起来覆盖原表 40 词（拆分不得丢词）。
    """
    NEUTRAL_EXPECTED = {
        "student", "students", "学生", "学生群体", "面向学生", "在校",
        "online", "remote", "线上", "远程", "global", "worldwide",
        "international", "全球", "国际", "公开报名",
    }
    MISMATCH_EXPECTED = {
        "university", "college", "undergraduate", "campus", "academic",
        "大学生", "高校",
    }
    A = {k.lower() for k in ELIGIBLE_KEYWORDS}
    B = {k.lower() for k in NEUTRAL_KEYWORDS}
    C = {k.lower() for k in INELIGIBLE_KEYWORDS}

    # 1) 不可证伪词不得留/回到 ELIGIBLE
    leaked = NEUTRAL_EXPECTED & A
    assert not leaked, f"不可证伪词不得出现在 ELIGIBLE_KEYWORDS：{sorted(leaked)}"
    # 2) 学段错配词必须在 INELIGIBLE，且不在 ELIGIBLE
    assert MISMATCH_EXPECTED <= C, \
        f"学段错配词必须移入 INELIGIBLE：缺 {sorted(MISMATCH_EXPECTED - C)}"
    assert not (MISMATCH_EXPECTED & A), "学段错配词不得同时在 ELIGIBLE"
    # 3) B 表与 A 表不得重叠（中性词若在 A 里就还能抬升）
    assert not (A & B), f"A/B 不得重叠：{sorted(A & B)}"
    # 4) 原表 41 词一个都不能丢（三分是重排而非删除）
    # 计数：中性词 16 + 学段错配 7 + 显式列出的 A 类 18 = 41
    ORIGINAL_41 = NEUTRAL_EXPECTED | MISMATCH_EXPECTED | {
        "high school", "high-school", "beginner", "teens", "all ages",
        "open to all", "open for all", "everyone", "anyone", "13+",
        "高中生", "中学生", "青少年", "初学者", "零基础",
        "不限年龄", "任何人", "所有人",
    }
    assert len(ORIGINAL_41) == 41, f"原表词数应为 41，实际 {len(ORIGINAL_41)}"
    lost = ORIGINAL_41 - (A | B | C)
    assert not lost, f"拆分后丢失的词：{sorted(lost)}"
    # 5) 每条中性词单独出现时都不得抬升（逐词端点验证，不只查表）
    for kw in sorted(NEUTRAL_EXPECTED):
        hint = _hint(f"Activity {kw}", "")
        assert hint != EligibilityHint.LIKELY_YES, \
            f"中性词「{kw}」不得单独抬升到 likely_yes，实际 {hint}"
    # 6) 每条学段错配词单独出现时都必须判 likely_no
    for kw in sorted(MISMATCH_EXPECTED):
        hint = _hint(f"Activity {kw}", "")
        assert hint == EligibilityHint.LIKELY_NO, \
            f"学段错配词「{kw}」应判 likely_no，实际 {hint}"
    print("[OK] eligibility keyword three-way split")


def test_deadline_display_four_inputs():
    """P4-11 换算纯函数：距报名截止 N 天 / 今天报名截止 / 已过 / 空值 四种输入

    P6-1① 起文案**一律写明"报名截止"**，不再出现无限定的"截止"——"报名截止"
    与"作品提交截止"是两根轴，裸"截止"会被读成哪一个取决于读者。
    """
    from datetime import date

    from core.resource_scout import deadline_display, _normalize_deadline

    today = date(2026, 6, 15)
    # 距报名截止 N 天
    assert deadline_display("2026-06-20", today=today) == "⏳ 距报名截止 5 天"
    assert deadline_display("2026-06-16", today=today) == "⏳ 距报名截止 1 天"
    # 当天
    assert deadline_display("2026-06-15", today=today) == "⏳ 今天报名截止"
    # 已过（回显日期，便于核对）
    assert deadline_display("2026-06-10", today=today) == "⏰ 报名截止日已过 2026-06-10"
    assert deadline_display("2025-01-01", today=today) == "⏰ 报名截止日已过 2025-01-01"
    # 空值 / 非法 → 空串（调用方据此整项不显示）
    assert deadline_display("", today=today) == ""
    assert deadline_display(None, today=today) == ""
    assert deadline_display("   ", today=today) == ""
    assert deadline_display("不是日期", today=today) == ""
    # 兼容带时间的时间戳（取前 10 位，与 provider 同款口径）
    assert deadline_display("2026-06-16 08:30:00", today=today) == "⏳ 距报名截止 1 天"

    # 入库规整：只接受合法定宽日期，非法一律空串（不猜）
    assert _normalize_deadline("2026-06-20") == "2026-06-20"
    assert _normalize_deadline("2026-06-20 12:00:00") == "2026-06-20"
    assert _normalize_deadline("") == ""
    assert _normalize_deadline(None) == ""
    assert _normalize_deadline("2026") == ""
    assert _normalize_deadline("明天") == ""
    print("[OK] deadline display four inputs")


def test_deadline_is_weekday_derived():
    """P6-1③ 派生标记：报名截止日是否落在周一~周五（纯函数，不落库、不打网络）

    用真实观测值锚定：2026-09-13 是周日，2026-09-14 是周一——正是 2026-09-13
    实测里"未截止 12 条有 10 条落在周中"的那批日期所在区间。
    """
    from datetime import date

    from core.resource_scout import deadline_is_weekday

    # 已知星期：2026-09-13 周日 / 09-14 周一 / 09-18 周五 / 09-19 周六
    assert date(2026, 9, 13).weekday() == 6
    assert date(2026, 9, 14).weekday() == 0
    assert deadline_is_weekday("2026-09-14") is True     # 周一
    assert deadline_is_weekday("2026-09-18") is True     # 周五
    assert deadline_is_weekday("2026-09-19") is False    # 周六
    assert deadline_is_weekday("2026-09-13") is False    # 周日
    # 未知 / 非法一律 False（"没有截止日"不等于"落在周中"）
    assert deadline_is_weekday("") is False
    assert deadline_is_weekday(None) is False
    assert deadline_is_weekday("不是日期") is False
    # 带时间戳形态（取前 10 位，与 deadline_display 同款口径）
    assert deadline_is_weekday("2026-09-14 12:00:00") is True
    print("[OK] deadline is_weekday derived")


def test_deadline_default_ordering_keeps_expired():
    """P4-11 默认排序：未过截止日按 deadline 升序在前；已过期**不消失**只排末尾"""
    from datetime import date

    from core.resource_scout import sort_opportunities_for_panel

    today = date(2026, 6, 15)
    # 传入顺序模拟"按发现时间倒序"：最新发现在前
    rows = [
        {"id": 1, "deadline": "", "title": "无截止日-最新"},
        {"id": 2, "deadline": "2026-06-30", "title": "未过-较晚"},
        {"id": 3, "deadline": "2026-06-10", "title": "已过期"},
        {"id": 4, "deadline": "2026-06-16", "title": "未过-最紧急"},
        {"id": 5, "deadline": "2026-06-15", "title": "今天截止"},
        {"id": 6, "deadline": "2026-06-20", "title": "未过-中间"},
    ]
    out = sort_opportunities_for_panel(rows, today=today)
    ids = [r["id"] for r in out]
    # 未过（含今天截止）按 deadline 升序：16 < 15(今天) < 20 < 30 → 4, 5, 6, 2
    # 今天截止的 deadline(06-15) 比 06-16 早，故排序上在 4 之后
    assert ids[:4] == [5, 4, 6, 2], ids
    # 其余保持传入顺序（1 在 3 前，因为传入时 1 更新）
    assert ids[4:] == [1, 3], ids
    # 关键不变量：已过期项**仍在结果里**，只是排到末尾
    assert 3 in ids, "已截止项不得被删除/隐藏"
    assert len(out) == len(rows), "排序不得增删任何一行"
    # 幂等：同一输入重复排序结果一致
    assert [r["id"] for r in sort_opportunities_for_panel(out, today=today)] == ids
    # 空输入 / 全无截止日：保持原序
    assert sort_opportunities_for_panel([], today=today) == []
    no_dl = [{"id": 9, "deadline": ""}, {"id": 8, "deadline": None}]
    assert [r["id"] for r in sort_opportunities_for_panel(no_dl, today=today)] == [9, 8]
    print("[OK] deadline default ordering keeps expired")


def test_scan_once_persists_deadline():
    """P4-11：deadline 随 Evidence.published_at 落库；两条写入口列集不分叉"""
    import sqlite3

    _clear_opportunities()
    # 写入口 A：scan_once（走 evidence_to_opportunity）
    provider = MockProvider(name="Mock", results=[
        make_evidence("有截止日的机会", "https://dl.example.com/1",
                      published_at="2026-06-20"),
        make_evidence("无截止日的机会", "https://dl.example.com/2"),
    ])
    scout = ResourceScout(queries=["截止日扫描"], providers=[provider])
    report = scout.scan_once()
    assert report["inserted"] == 2, report
    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities()}
    assert rows["https://dl.example.com/1"]["deadline"] == "2026-06-20", \
        rows["https://dl.example.com/1"]["deadline"]
    assert rows["https://dl.example.com/2"]["deadline"] == "", \
        rows["https://dl.example.com/2"]["deadline"]

    # 写入口 B：add_opportunity（必须也能写 deadline，否则两条路径列集分叉）
    oid = ResourceScout.add_opportunity(
        "手工机会", "https://dl.example.com/3", deadline="2026-07-01")
    assert oid > 0
    assert ResourceScout.get_opportunity(oid)["deadline"] == "2026-07-01"
    # 非法日期落空串（不猜）
    oid2 = ResourceScout.add_opportunity(
        "手工机会2", "https://dl.example.com/4", deadline="下个月")
    assert ResourceScout.get_opportunity(oid2)["deadline"] == ""

    # 两条写入口写入后的列值集合一致（同表同列，无 NULL/缺列分叉）
    conn = sqlite3.connect(_database_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        a = conn.execute("SELECT * FROM opportunities WHERE source_url=?",
                         ("https://dl.example.com/1",)).fetchone()
        b = conn.execute("SELECT * FROM opportunities WHERE source_url=?",
                         ("https://dl.example.com/3",)).fetchone()
        assert set(a.keys()) == set(b.keys()), "两条写入口列集必须一致"
        assert a["deadline"] and b["deadline"]
    finally:
        conn.close()
    print("[OK] scan once persists deadline (both write paths)")


def test_ui_panel_shows_deadline_and_orders():
    """P4-11 面板：显示截止日文案、无截止日不显示该项、默认视图按截止日排序"""
    from datetime import date, timedelta

    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过面板截止日契约测试")
        return

    _clear_opportunities()
    today = date.today()
    soon = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    later = (today + timedelta(days=9)).strftime("%Y-%m-%d")
    past = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    # 插入顺序刻意与期望展示顺序相反
    assert ResourceScout.add_opportunity(
        "无截止日", "https://pan.example.com/none")
    assert ResourceScout.add_opportunity(
        "较晚截止", "https://pan.example.com/later", deadline=later)
    assert ResourceScout.add_opportunity(
        "已过期", "https://pan.example.com/past", deadline=past)
    assert ResourceScout.add_opportunity(
        "最紧急", "https://pan.example.com/soon", deadline=soon)

    md, dd = app_module.refresh_opportunities_panel(None, "all", "all")
    assert "共 4 条机会" in md, md
    # 未过截止日的按 deadline 升序在前：最紧急 → 较晚
    i_soon = md.index("最紧急")
    i_later = md.index("较晚截止")
    i_none = md.index("无截止日")
    i_past = md.index("已过期")
    assert i_soon < i_later, (i_soon, i_later)
    # 两个未过项必须都在"其余"之前：其余两项（已截止 / 无截止日）保持传入顺序，
    # 而传入顺序是"按发现时间倒序" → 后插入的 #3(已截止) 先于 #1(无截止日)
    assert i_later < i_past and i_later < i_none, (i_later, i_past, i_none)
    assert i_past < i_none, "其余部分须保持传入顺序（发现时间倒序）"
    # 已过项**仍在列表里**（不删除/不隐藏），且显示"报名截止日已过 + 日期"
    assert f"⏰ 报名截止日已过 {past}" in md, md
    # 剩余天数文案（P6-1① 起写明"报名截止"）
    assert "⏳ 距报名截止 2 天" in md and "⏳ 距报名截止 9 天" in md, md
    # 无截止日的行不得出现任何截止日标记
    none_block = md.split("无截止日", 1)[1].split("- #", 1)[0]
    assert "⏳" not in none_block and "⏰" not in none_block, none_block
    # 排序不改变选取/筛选语义：四条都在下拉框里
    assert len(dd["choices"]) == 4, dd["choices"]
    print("[OK] ui panel shows deadline and orders")


# ---------- P4-14 子项 ②：会话启动对账单（纯函数） ----------

def _bill_row(rid, title, deadline, status="new", discovered_at="2026-09-10 08:00:00",
              eligibility_hint=None, ai_eligibility=None):
    """构造一行"账单候选"（dict 形态，与 list_opportunities 的返回一致）"""
    return {"id": rid, "title": title, "deadline": deadline, "status": status,
            "discovered_at": discovered_at,
            "eligibility_hint": eligibility_hint, "ai_eligibility": ai_eligibility}


def test_session_bill_six_deadline_kinds():
    """对账单：六类 deadline 输入不崩且各归其段（含 NULL —— 迁移前旧行就是 NULL）

    today 固定为 2026-09-13（可注入，否则日期相关断言无法稳定）。
    """
    from datetime import date

    today = date(2026, 9, 13)
    rows = [
        # 1) 已过期（-3 天）→ 段 1
        _bill_row(1, "已过期项", "2026-09-10"),
        # 2) 今天 → 段 2
        _bill_row(2, "今天截止项", "2026-09-13"),
        # 3) 3 天后 → 段 2
        _bill_row(3, "三天后项", "2026-09-16"),
        # 4) 30 天后 → 段 3
        _bill_row(4, "三十天后项", "2026-10-13"),
        # 5) 空串 deadline → 不参与 1/2/3
        _bill_row(5, "空截止日项", ""),
        # 6) NULL deadline（旧行形态）→ 不参与 1/2/3，且**不得崩**
        _bill_row(6, "NULL截止日项", None),
    ]
    text = build_session_bill(rows, today=today)
    assert text, "有内容时不应返回空串"

    # 段 1 只有已过期项
    assert "报名截止日已过 1 条" in text, text
    assert "已过期项" in text and "原报名截止日 2026-09-10" in text, text
    # 段 2 含今天与 3 天后
    assert "7 天内报名截止 2 条" in text, text
    assert "今天截止项" in text and "⏳ 今天报名截止" in text, text
    assert "三天后项" in text and "⏳ 距报名截止 3 天" in text, text
    # 段 3 只报条数 + 最近一条，不逐条展开
    assert "更远的还有 1 条" in text, text
    assert "三十天后项" in text, text
    # 空 / NULL 截止日的两条既不进段 1/2/3，也不该消失得无影无踪而被误报为"未截止"
    assert "空截止日项" not in text, "无截止日的项不应出现在 1/2/3 段"
    assert "NULL截止日项" not in text, "NULL 截止日的项不应出现在 1/2/3 段"

    # 边界：第 7 天在窗内、第 8 天在窗外
    edge = build_session_bill([
        _bill_row(11, "第七天项", "2026-09-20"),
        _bill_row(12, "第八天项", "2026-09-21"),
    ], today=today)
    assert "第七天项" in edge and "7 天内报名截止" in edge, edge
    assert "更远的还有 1 条" in edge and "第八天项" in edge, edge
    print("[OK] session bill six deadline kinds")


def test_session_bill_filters_folded_and_ignored():
    """对账单：likely_no 不进账单（与面板折叠口径一致）、ignored 不进账单、unknown 进"""
    from datetime import date

    today = date(2026, 9, 13)
    rows = [
        _bill_row(1, "未知资格项", "2026-09-10",
                  eligibility_hint="unknown", ai_eligibility="unknown"),
        _bill_row(2, "可能符合项", "2026-09-10",
                  eligibility_hint="likely_yes", ai_eligibility="likely_yes"),
        # 规则轴 unknown 但 AI 轴 no → effective_hint = likely_no（面板会折叠它）
        _bill_row(3, "被折叠项", "2026-09-10",
                  eligibility_hint="unknown", ai_eligibility="likely_no"),
        # 规则轴直接 likely_no
        _bill_row(4, "规则不符项", "2026-09-10", eligibility_hint="likely_no"),
        # 状态 ignored（用户已表态）
        _bill_row(5, "已忽略项", "2026-09-10", status="ignored"),
    ]
    text = build_session_bill(rows, today=today)
    assert "未知资格项" in text, "unknown 应出现"
    assert "可能符合项" in text, "likely_yes 应出现"
    assert "被折叠项" not in text, "effective_hint=likely_no 不应进账单（面板已折叠它）"
    assert "规则不符项" not in text, "likely_no 不应进账单"
    assert "已忽略项" not in text, "ignored 不应进账单"
    assert "报名截止日已过 2 条" in text, text
    print("[OK] session bill filters folded and ignored")


def test_session_bill_tolerates_dirty_rows():
    """对账单：非 dict 元素 / 缺字段的行被安全忽略（不抛异常）"""
    from datetime import date

    today = date(2026, 9, 13)
    # None / int / 空 dict / 缺 deadline 键
    assert build_session_bill([None, 42, {}, {"status": "new"}], today=today) == ""
    mixed = [None, _bill_row(1, "唯一有效项", "2026-09-10"), "脏字符串"]
    text = build_session_bill(mixed, today=today)
    assert "唯一有效项" in text and "报名截止日已过 1 条" in text, text
    print("[OK] session bill tolerates dirty rows")


def test_session_bill_empty_returns_empty_string():
    """全空输入 → 返回空串（不是报错、不是"无内容"字样）"""
    from datetime import date

    today = date(2026, 9, 13)
    assert build_session_bill([], today=today) == ""
    # 有行但全部不满足任何一段（无 deadline 且 discovered_at 在窗口外）
    none_relevant = [
        _bill_row(1, "远古无期限项", "", discovered_at="2020-01-01 00:00:00"),
        _bill_row(2, "远古NULL项", None, discovered_at="2020-01-01 00:00:00"),
    ]
    assert build_session_bill(none_relevant, today=today) == ""
    # 空串里不得出现"无内容"这类占位字样
    assert "无内容" not in build_session_bill([], today=today)
    print("[OK] session bill empty returns empty string")


def test_session_bill_reports_recent_discovery_window():
    """对账单第 4 段：近 7 天新发现按固定窗口计数（绝对日期，无状态）"""
    from datetime import date

    today = date(2026, 9, 13)
    rows = [
        _bill_row(1, "今天发现", "", discovered_at="2026-09-13 06:00:00"),
        _bill_row(2, "7天前发现", "", discovered_at="2026-09-06 06:00:00"),   # 边界内
        _bill_row(3, "8天前发现", "", discovered_at="2026-09-05 06:00:00"),   # 边界外
        _bill_row(4, "很久以前", "", discovered_at="2026-01-01 06:00:00"),
        _bill_row(5, "脏时间", "", discovered_at="不是日期"),
    ]
    text = build_session_bill(rows, today=today)
    assert "近 7 天新发现 2 条" in text, text
    print("[OK] session bill reports recent discovery window")


def test_session_bill_is_read_only():
    """对账单是纯展示：调用前后库内数据与行对象都不被改动

    不变量：资格判定只过滤/展示，从不自动处理掉任何东西（不删行、不改 status、
    不把任何东西标成"已看过"）。
    """
    from datetime import date

    today = date(2026, 9, 13)
    _clear_opportunities()
    oid = ResourceScout.add_opportunity(
        "账单只读检查项", "https://bill.example.com/ro", deadline="2026-09-10")
    assert oid > 0
    before = {r["id"]: dict(r) for r in ResourceScout.list_opportunities(limit=50)}

    rows = ResourceScout.list_opportunities(limit=50)
    text = build_session_bill(rows, today=today)
    assert "账单只读检查项" in text, text

    after = {r["id"]: dict(r) for r in ResourceScout.list_opportunities(limit=50)}
    assert before == after, "对账单不得改动任何库内字段"
    # 传入的行对象也不得被就地改写
    assert rows[0]["status"] == "new" and "status" in rows[0]
    print("[OK] session bill is read only")


def test_ui_load_session_bill_callback():
    """UI 回调契约：load_session_bill 返回 (Markdown 文本, 容器可见性 update)"""
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过对账单回调契约测试")
        return

    _clear_opportunities()
    # 空库 → 空串 + 容器隐藏
    text, upd = app_module.load_session_bill()
    assert text == "", repr(text)
    assert upd.get("visible") is False, upd

    # 造一条已过期的 → 有内容 + 容器显示
    assert ResourceScout.add_opportunity(
        "回调检查-已过期", "https://bill.example.com/cb1", deadline="2020-01-01") > 0
    text2, upd2 = app_module.load_session_bill()
    assert "回调检查-已过期" in text2, text2
    assert upd2.get("visible") is True, upd2
    print("[OK] ui load session bill callback")


def test_deadline_weekday_notice_is_question_not_verdict():
    """P6-2：周中报名截止只**提问**，绝不输出"已过期/无法参加/已错过"式判定

    依据（两个当晚就被推翻的反例）：(a) 设计侧把周中截止当"结构性不可参加"，
    被用户一句"明天我刚好放假"推翻；(b) 某 10/8 周四截止的活动，官方正文自己
    写着"跨国庆假期，正好有空写稿"。**周中只是"要问一句"，从来不是"可以不算"。**

    本条同时锁死账单与面板两处渲染——这是"判死"最容易复发的两个出口。
    """
    from datetime import date, timedelta

    from core.resource_scout import deadline_weekday_notice

    today = date(2026, 9, 13)                      # 周日
    assert today.weekday() == 6
    # 未来 3 天的周三 → 提问
    assert date(2026, 9, 16).weekday() == 2
    assert (deadline_weekday_notice("2026-09-16", today=today)
            == "⚠️ 报名截止落在周三，需确认当天是否有电脑")
    assert (deadline_weekday_notice("2026-09-18", today=today)
            == "⚠️ 报名截止落在周五，需确认当天是否有电脑")
    # 周末 / 未知 / 非法 → 不提问
    assert deadline_weekday_notice("2026-09-19", today=today) == ""   # 周六
    assert deadline_weekday_notice("2026-09-20", today=today) == ""   # 周日
    assert deadline_weekday_notice("", today=today) == ""
    assert deadline_weekday_notice(None, today=today) == ""
    assert deadline_weekday_notice("不是日期", today=today) == ""
    # 已经过去的周中日期不提问：那天有没有电脑已经无从谈起，
    # 而"已过去"由 deadline_display 如实陈述，不在这里升级成判定
    assert deadline_weekday_notice("2026-09-10", today=today) == ""   # 周四，已过

    # ---- 账单 ----
    rows = [
        _bill_row(1, "周中截止项", "2026-09-16"),      # 周三 → 提问
        _bill_row(2, "周末截止项", "2026-09-20"),      # 周日 → 不提问
    ]
    text = build_session_bill(rows, today=today)
    assert "⚠️ 报名截止落在周三，需确认当天是否有电脑" in text, text
    for banned in ("已过期", "无法参加", "已错过"):
        assert banned not in text, (banned, text)

    # ---- 面板 ----
    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过面板周中文案契约测试")
        return
    _clear_opportunities()
    today_real = date.today()
    # 取"今天或之后的下一个周一"，让断言不受运行日期影响
    monday = today_real + timedelta(days=(0 - today_real.weekday()) % 7)
    assert monday.weekday() == 0
    assert ResourceScout.add_opportunity(
        "周中面板项", "https://wd.example.com/a",
        deadline=monday.strftime("%Y-%m-%d")) > 0
    md, _ = app_module.refresh_opportunities_panel(None, "all", "all")
    assert "⚠️ 报名截止落在周一，需确认当天是否有电脑" in md, md
    for banned in ("已过期", "无法参加", "已错过"):
        assert banned not in md, (banned, md)
    print("[OK] deadline weekday notice is a question not a verdict")


def test_deadline_conflict_display_in_panel_and_bill():
    """P6-3②：面板与账单都必须说出"报名截止 X（来源冲突，未实测）"

    零 schema 变更下 notes 是唯一能被 UI 读到的展示载体（见
    core.resource_scout.NOTES_SEPARATOR 的说明），因此这条同时锁死：
    ① notes 的**首段仍是类目**（P4-10 的契约不被这次改动吃掉）；
    ② 冲突段以完整短语形式出现，渲染层不需要二次解析语义。
    """
    import json as _json
    from datetime import date

    from core.resource_scout import (
        evidence_to_opportunity, notes_segments,
        find_deadline_conflict_note, DEADLINE_CONFLICT_MARK,
        SUBMIT_DEADLINE_MISSING,
    )

    ev = Evidence(
        source_url="https://cf.example.com/1", source_name="ModelScope 竞赛/活动",
        title="来源冲突样本", snippet="两个来源给出的报名截止打架",
        confidence=0.9, published_at="2026-09-13",
        retrieved_at="2026-09-13 23:55:00",
        entities=_json.dumps({
            "Category": "devEvent",
            "deadline_sources": {"接口": 1789257540, "落地页": 1789228800},
            "deadline_conflict": True,
            "SubmitDeadlineTs": None,
        }, ensure_ascii=False))
    opp = evidence_to_opportunity(ev)
    assert opp is not None
    segs = notes_segments(opp.notes)
    # P6-5b①：首段是来源说明（"以下字段…"描述的是它下面那几行），类目退为第二段
    assert segs[0] == ("以下字段抓取自接口，抓取时刻 2026-09-13 23:55；"
                       "细则以官网为准 → https://cf.example.com/1"), segs
    assert segs[1] == "类目：devEvent", segs
    assert segs[2] == f"报名截止 2026-09-13{DEADLINE_CONFLICT_MARK}", segs
    # P6-5b②：未抓到时如实说"未抓到"，不写成"无提交截止"
    assert segs[3] == SUBMIT_DEADLINE_MISSING, segs
    assert "无提交截止" not in opp.notes, opp.notes
    assert find_deadline_conflict_note(opp.notes) == segs[2]
    # 没有冲突时仍有来源说明与提交截止缺口，但**不产生冲突段**（不给每行挂噪声）
    ev2 = Evidence(source_url="https://cf.example.com/2", title="无冲突样本",
                   retrieved_at="2026-09-13 23:55:00",
                   entities=_json.dumps({"Category": "devEvent",
                                         "deadline_sources": {"接口": 1},
                                         "SubmitDeadlineTs": 1789394400},
                                        ensure_ascii=False))
    segs2 = notes_segments(evidence_to_opportunity(ev2).notes)
    assert find_deadline_conflict_note(evidence_to_opportunity(ev2).notes) == ""
    assert segs2[1] == "类目：devEvent", segs2
    assert segs2[2] == "提交截止：2026-09-14 22:00", segs2
    # 非一手源（没有 entities）→ 完全没有备注分段
    ev3 = Evidence(source_url="https://ddg.example.com/3", title="搜索引擎转载")
    assert notes_segments(evidence_to_opportunity(ev3).notes) == []
    assert find_deadline_conflict_note("") == ""
    assert find_deadline_conflict_note(None) == ""

    _clear_opportunities()
    assert ResourceScout.add_opportunity(
        "来源冲突条", "https://cf.example.com/1", deadline="2026-09-13",
        notes=opp.notes) > 0
    rows = ResourceScout.list_opportunities()
    text = build_session_bill(rows, today=date(2026, 9, 14))
    assert DEADLINE_CONFLICT_MARK in text, text
    assert "报名截止 2026-09-13" in text, text

    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过面板来源冲突契约测试")
        return
    md, _ = app_module.refresh_opportunities_panel(None, "all", "all")
    assert f"🏷️ 报名截止 2026-09-13{DEADLINE_CONFLICT_MARK}" in md, md
    assert "🏷️ 类目：devEvent" in md, md
    print("[OK] deadline conflict shown in panel and bill")


def test_no_completeness_wording_snapshot():
    """P6-5b①：措辞快照——面板/账单**不许**出现"已抓全"式字样

    动因（247 实证，凌晨二修新增）：全库正文最厚的一行（10,689 中文字）仍然
    整条缺失"创空间 700h"这条路径——它只在另一个官方页面
    `modelscope.cn/learn/433641` 上，而 247 的正文里 `learn` / `posts` 出现
    **0 次**（只有 28 个 `gallery/` 链接），链接方向还是**反的**（learn 页页脚
    才指向 `/events/247`）。⇒ **"正文长"与"规则全"没有关系。**

    所以这条测试锁的不是"某个词"，而是**一个不能越过的口径**：不要把"我抓到的"
    说成"存在的全部"。本批也明确**不新增抓取面**（不接站内搜索、不爬 learn/*）
    ——真正的失效是"缺失被显示成不存在"，不是"抓得不够多"。
    """
    import json as _json
    from datetime import date

    from core.resource_scout import (
        FORBIDDEN_COMPLETENESS_PHRASES, PROVENANCE_CAPTION_URL,
    )

    assert FORBIDDEN_COMPLETENESS_PHRASES, "禁令清单不能为空"

    _clear_opportunities()
    ev = Evidence(
        source_url="https://ms.example.com/events/247", title="最厚的正文也缺路径",
        snippet="正文 10689 中文字", retrieved_at="2026-09-13 23:55:00",
        entities=_json.dumps({"Category": "devEvent",
                              "deadline_sources": {"接口": 1},
                              "SubmitDeadlineTs": None}, ensure_ascii=False))
    opp = evidence_to_opportunity(ev)
    assert ResourceScout.add_opportunity(
        "口径快照行", "https://ms.example.com/events/247",
        deadline="2026-09-16", notes=opp.notes) > 0

    rows = ResourceScout.list_opportunities()
    text = build_session_bill(rows, today=date(2026, 9, 13))
    for banned in FORBIDDEN_COMPLETENESS_PHRASES:
        assert banned not in text, (banned, text)
    # 账单里确实出现了"提交截止：未抓到"这条缺口提示（正面要求，不只是禁词）
    assert "提交截止：未抓到" in text, text
    assert "无提交截止" not in text, text

    try:
        import app as app_module
    except Exception as e:
        print(f"[SKIP] 无法导入 app.py（{e}），跳过面板措辞快照")
        return
    md, _ = app_module.refresh_opportunities_panel(None, "all", "all")
    for banned in FORBIDDEN_COMPLETENESS_PHRASES:
        assert banned not in md, (banned, md)
    # 面板给出的是**来源 + 时刻 + 官网链接**，这正是"不宣称抓全"的正面写法
    expected = PROVENANCE_CAPTION_URL.format(
        fetched="2026-09-13 23:55", url="https://ms.example.com/events/247")
    assert expected in md, md
    assert "提交截止：未抓到（该源可能另有官方说明页）" in md, md
    print("[OK] no completeness wording snapshot")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== eligibility filter test ===\n")
    setup()
    test_eligibility_column_and_default()
    test_deadline_column_both_init_paths()
    test_notified_bucket_column_legacy_upgrade()
    test_user_profile_table_and_defaults()
    test_user_profile_update_and_persist()
    test_check_eligibility_english()
    test_check_eligibility_chinese()
    test_check_eligibility_conflict_prefers_no()
    test_check_eligibility_empty()
    test_check_eligibility_profile_email()
    test_check_eligibility_profile_age()
    test_check_eligibility_profile_region()
    test_check_eligibility_profile_student_type()
    test_check_eligibility_profile_partial_defaults()
    test_evidence_to_opportunity_sets_hint()
    test_evidence_to_opportunity_profile_sets_reason()
    test_scan_once_persists_hint()
    test_scan_once_persists_reason_with_profile()
    test_add_opportunity_hint_handling()
    test_add_opportunity_reason_handling()
    test_list_opportunities_eligibility_filter()
    test_recompute_eligibility_updates_rows()
    test_ui_filter_callback_contract()
    test_ui_profile_callback_contract()
    test_ui_status_filter_contract()
    # P4-10 子项 2：两侧一致的保守派生值 + 默认视图折叠
    test_effective_hint_derivation()
    test_ui_fold_default_view_without_db_mutation()
    # P4-10 子项 3：关键词表三分法不变量
    test_eligibility_keyword_three_way_split()
    # P4-11 报名截止日：换算 / 排序 / 两条写入口 / 面板展示
    test_deadline_display_four_inputs()
    # P6-1③：报名截止落在周中 / 周末的派生标记（纯函数）
    test_deadline_is_weekday_derived()
    # P6-2：周中报名截止只提问、不判死（账单 + 面板两处渲染同时锁死）
    test_deadline_weekday_notice_is_question_not_verdict()
    # P6-3：来源冲突不择一消解（面板 + 账单两处都要说出来）
    test_deadline_conflict_display_in_panel_and_bill()
    # P6-5b：诚实的缺口显示（措辞快照锁死"已抓全"式字样）
    test_no_completeness_wording_snapshot()
    test_deadline_default_ordering_keeps_expired()
    test_scan_once_persists_deadline()
    test_ui_panel_shows_deadline_and_orders()
    # P4-14 子项 ②：会话启动对账单（纯函数 + 容器可见性契约）
    test_session_bill_six_deadline_kinds()
    test_session_bill_filters_folded_and_ignored()
    test_session_bill_tolerates_dirty_rows()
    test_session_bill_empty_returns_empty_string()
    test_session_bill_reports_recent_discovery_window()
    test_session_bill_is_read_only()
    test_ui_load_session_bill_callback()
    test_cleanup()
    print("\n=== ALL ELIGIBILITY FILTER TESTS PASSED ===")
