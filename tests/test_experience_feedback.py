# -*- coding: utf-8 -*-
"""
经验评分与淘汰机制测试（离线，不依赖 LLM）
覆盖：
- 数据库迁移：experiences 新列（score / use_count / last_used_at）存在且旧数据默认兼容
- record_feedback：有用+1 / 无用-1、use_count 累计、last_used_at 更新、非法输入与不存在 ID
- search_experiences：关键词得分相同时高评分优先；相关性仍是首要排序因子
- get_all_experiences：sort_by_score 可选排序，默认行为与历史版本一致
- cleanup_low_score：按阈值淘汰并返回数量、重复执行幂等、阈值容错
- ContextManager 注入：每条经验附带"[评分: N]"（v3.7.2 起的结构化标记格式，含默认 0 的显示）
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（P0-5）：切换到临时库，须在导入任何访问数据库的模块之前 ----
from tests._test_db_helpers import setup_test_db
setup_test_db()

from memory.database import init_db, get_db
from memory.experience_store import ExperienceStore
from core.context_manager import ContextManager

# 本测试创建的经验 ID，cleanup 时精确清理
_created_ids = []

# 本文件全部 fixture 的 scenario 名单：setup 时按名单清除历史残留
# （中途失败的运行来不及执行末尾 cleanup，重跑时在此自愈，避免污染检索类断言）
_FIXTURE_SCENARIOS = (
    "默认场景", "迁移检查场景", "反馈场景", "非法反馈场景",
    "背英语单词总是记不住", "英语单词遗忘快", "英语口语发音不标准",
    "排序场景A", "排序场景B",
    "淘汰场景差", "淘汰场景临界", "淘汰场景好",
    "容错保留场景", "注入展示场景：背诵英语单词",
)


def setup():
    """初始化数据库（触发新列自动迁移），并清除本文件 fixture 的历史残留"""
    init_db()
    conn = get_db()
    placeholders = ", ".join("?" * len(_FIXTURE_SCENARIOS))
    conn.execute(f"DELETE FROM experiences WHERE scenario IN ({placeholders})",
                 _FIXTURE_SCENARIOS)
    conn.commit()
    conn.close()


def _make_exp(**kwargs) -> int:
    """新增经验并记录 ID 供清理"""
    new_id = ExperienceStore.add_experience(
        kwargs.get("scenario", "默认场景"),
        kwargs.get("strategy", "默认策略"),
        effect=kwargs.get("effect", ""),
        conditions=kwargs.get("conditions", ""),
        source_session_id=kwargs.get("source_session_id"),
    )
    _created_ids.append(new_id)
    return new_id


def _get_by_id(exp_id: int) -> dict:
    """按 ID 读取单条经验"""
    for exp in ExperienceStore.get_all_experiences(limit=500):
        if exp["id"] == exp_id:
            return exp
    return {}


def test_migration_columns():
    """迁移检查：三个新列存在；新建经验默认 score=0 / use_count=0 / last_used_at 为空"""
    conn = get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(experiences)").fetchall()}
    conn.close()
    assert {"score", "use_count", "last_used_at"}.issubset(cols), f"缺少评分列，实际: {cols}"

    new_id = _make_exp(scenario="迁移检查场景", strategy="迁移检查策略")
    exp = _get_by_id(new_id)
    assert exp["score"] == 0, "旧数据兼容：默认评分应为 0"
    assert exp["use_count"] == 0, "默认评价次数应为 0"
    assert not exp["last_used_at"], "默认最近评价时间应为空"
    print("[OK] migration columns")


def test_record_feedback_up_down():
    """反馈计分：up 加分 / down 减分，use_count 累计，last_used_at 更新"""
    exp_id = _make_exp(scenario="反馈场景", strategy="反馈策略")

    assert ExperienceStore.record_feedback(exp_id, "up") is True
    exp = _get_by_id(exp_id)
    assert exp["score"] == 1 and exp["use_count"] == 1
    assert exp["last_used_at"], "评价后 last_used_at 应写入时间戳"

    assert ExperienceStore.record_feedback(exp_id, "up") is True
    assert ExperienceStore.record_feedback(exp_id, "down") is True
    exp = _get_by_id(exp_id)
    assert exp["score"] == 1, "+1 +1 -1 后评分应回到 1"
    assert exp["use_count"] == 3, "三次评价 use_count 应为 3"
    print("[OK] record feedback up/down")


def test_record_feedback_invalid():
    """非法反馈类型与不存在的 ID 返回 False，且不改变任何数据"""
    exp_id = _make_exp(scenario="非法反馈场景", strategy="非法反馈策略")
    before = _get_by_id(exp_id)

    assert ExperienceStore.record_feedback(exp_id, "maybe") is False
    assert ExperienceStore.record_feedback(exp_id, "") is False
    assert ExperienceStore.record_feedback(exp_id, None) is False
    assert ExperienceStore.record_feedback(99999999, "up") is False, "不存在的 ID 应返回 False"

    after = _get_by_id(exp_id)
    assert after["score"] == before["score"] and after["use_count"] == before["use_count"], \
        "失败反馈不应改变评分"
    print("[OK] record feedback invalid input")


def test_search_prefers_higher_score():
    """检索加权：关键词得分相同的两条经验，评分高者排前"""
    same_scenario = "背英语单词总是记不住"
    same_strategy = "间隔重复加默写检测"
    low_id = _make_exp(scenario=same_scenario, strategy=same_strategy)
    high_id = _make_exp(scenario=same_scenario, strategy=same_strategy)
    # 文本完全相同 → 关键词得分必然相同；仅评分不同
    ExperienceStore.record_feedback(high_id, "up")
    ExperienceStore.record_feedback(high_id, "up")

    hits = ExperienceStore.search_experiences("英语单词怎么记", limit=500)
    hit_ids = [h["id"] for h in hits]
    assert low_id in hit_ids and high_id in hit_ids, "两条都应命中"
    assert hit_ids.index(high_id) < hit_ids.index(low_id), \
        f"关键词得分相同时高评分应排前，实际顺序: high={hit_ids.index(high_id)}, low={hit_ids.index(low_id)}"
    print("[OK] search prefers higher score")


def test_search_keyword_score_dominant():
    """相关性优先：高分但弱相关的经验不应越过强相关的经验"""
    strong_id = _make_exp(
        scenario="英语单词遗忘快",
        strategy="每天复习前一天的英语单词再加新词",
    )  # 查询"英语单词"会命中多次（bigram 密集）
    weak_id = _make_exp(
        scenario="英语口语发音不标准",
        strategy="跟读模仿原声材料",
    )
    ExperienceStore.record_feedback(weak_id, "up")
    ExperienceStore.record_feedback(weak_id, "up")
    ExperienceStore.record_feedback(weak_id, "up")  # 弱相关经验给高分

    # 用大 limit 取全部命中，避免开发库中其他高命中经验干扰相对顺序比较
    hits = ExperienceStore.search_experiences("怎么背英语单词", limit=500)
    ids = [h["id"] for h in hits]
    assert strong_id in ids and weak_id in ids, f"两条都应命中，实际命中 {len(ids)} 条"
    assert ids.index(strong_id) < ids.index(weak_id), \
        f"关键词得分应主导排序（相关性优先），实际顺序: strong={ids.index(strong_id)}, weak={ids.index(weak_id)}"
    print("[OK] search keyword score dominant")


def test_get_all_sort_modes():
    """get_all_experiences：sort_by_score=True 按评分降序；默认保持时间倒序旧行为"""
    a = _make_exp(scenario="排序场景A", strategy="排序策略A")
    b = _make_exp(scenario="排序场景B", strategy="排序策略B")
    ExperienceStore.record_feedback(a, "down")
    ExperienceStore.record_feedback(a, "down")  # a 评分为 -2

    by_time = ExperienceStore.get_all_experiences(limit=50)
    assert by_time[0]["id"] >= max(a, b), "默认模式应按创建时间倒序（最新在前）"

    by_score = ExperienceStore.get_all_experiences(limit=50, sort_by_score=True)
    score_a = next(e["score"] for e in by_score if e["id"] == a)
    score_b = next(e["score"] for e in by_score if e["id"] == b)
    pos_a = next(i for i, e in enumerate(by_score) if e["id"] == a)
    pos_b = next(i for i, e in enumerate(by_score) if e["id"] == b)
    assert (pos_b < pos_a) if score_b > score_a else (pos_a <= pos_b or True)
    assert score_a == -2 and score_b == 0, "评分读取应正确"
    # 明确断言：b（0 分）必须排在 a（-2 分）之前
    assert pos_b < pos_a, f"按评分排序时 0 分应排在 -2 分前，实际: pos_b={pos_b}, pos_a={pos_a}"
    # 自产自销：立即删除本用例创建的负分经验，避免影响后续全表清理类用例
    for done in (a, b):
        ExperienceStore.delete_experience(done)
        if done in _created_ids:
            _created_ids.remove(done)
    print("[OK] get all sort modes")


def test_cleanup_low_score():
    """淘汰：删除 score <= 阈值的经验并返回数量；0 分保留；重复执行返回 0"""
    bad = _make_exp(scenario="淘汰场景差", strategy="淘汰策略差")
    borderline = _make_exp(scenario="淘汰场景临界", strategy="淘汰策略临界")
    good = _make_exp(scenario="淘汰场景好", strategy="淘汰策略好")
    for _ in range(5):
        ExperienceStore.record_feedback(bad, "down")      # -5
    for _ in range(3):
        ExperienceStore.record_feedback(borderline, "down")  # -3
    ExperienceStore.record_feedback(good, "up")           # +1

    deleted = ExperienceStore.cleanup_low_score(threshold=-3)
    assert deleted >= 2, f"至少应删除 bad 与 borderline 两条，实际 {deleted}"
    assert _get_by_id(bad) == {} and _get_by_id(borderline) == {}, "≤-3 的应被删除"
    assert _get_by_id(good)["id"] == good, "+1 分的不应被删除"

    # 从追踪列表移除已被淘汰的 ID，避免 cleanup 阶段空删
    for gone in (bad, borderline):
        if gone in _created_ids:
            _created_ids.remove(gone)

    assert ExperienceStore.cleanup_low_score(threshold=-3) == 0, "再次执行应删除 0 条（幂等）"
    print("[OK] cleanup low score")


def test_cleanup_threshold_tolerance():
    """阈值容错：字符串数字可正常转换；非法值回退默认 -3（当前库无 ≤-3 数据时不误删）"""
    keep = _make_exp(scenario="容错保留场景", strategy="容错保留策略")
    ExperienceStore.record_feedback(keep, "down")  # -1，高于任何合理阈值

    assert ExperienceStore.cleanup_low_score(threshold="-2") == 0, "字符串阈值应被转换且 -1 不满足 ≤-2"
    assert _get_by_id(keep)["id"] == keep, "-1 分的经验不应被误删"
    # 非法阈值回退 -3：当前 -1 不满足条件，不应删除任何记录
    assert ExperienceStore.cleanup_low_score(threshold="abc") == 0
    assert _get_by_id(keep)["id"] == keep, "非法阈值不应误删数据"
    print("[OK] cleanup threshold tolerance")


def test_context_manager_shows_score():
    """注入增强：经验片段每条附带"[评分: N]"（v3.7.2 格式），默认 0 也显示"""
    cm = ContextManager()
    exp_id = _make_exp(scenario="注入展示场景：背诵英语单词",
                       strategy="间隔重复策略用于英语单词记忆")

    # 默认评分 0 也要显示
    ctx0 = cm._build_experiences_context("英语单词记忆方法", limit=3)
    assert "💡 相关经验参考" in ctx0, "经验片段应存在"
    assert "[评分: 0]" in ctx0, f"未评分经验应显示评分 0，实际:\n{ctx0}"

    ExperienceStore.record_feedback(exp_id, "up")
    ExperienceStore.record_feedback(exp_id, "up")
    ctx2 = cm._build_experiences_context("英语单词记忆方法", limit=3)
    assert "[评分: 2]" in ctx2, f"评分 2 应体现在注入片段中，实际:\n{ctx2}"
    assert "评分越高的经验越值得优先参考" in ctx2, "使用建议应提示评分含义"
    print("[OK] context manager shows score")


def test_cleanup():
    """清理本测试产生的全部经验数据"""
    for exp_id in list(_created_ids):
        ExperienceStore.delete_experience(exp_id)
    _created_ids.clear()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== experience feedback test ===\n")
    setup()
    test_migration_columns()
    test_record_feedback_up_down()
    test_record_feedback_invalid()
    test_search_prefers_higher_score()
    test_search_keyword_score_dominant()
    test_get_all_sort_modes()
    test_cleanup_low_score()
    test_cleanup_threshold_tolerance()
    test_context_manager_shows_score()
    test_cleanup()
    print("\n=== ALL TESTS PASSED ===")
