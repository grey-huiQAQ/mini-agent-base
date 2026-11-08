# -*- coding: utf-8 -*-
"""
ModelScopeProvider（魔搭竞赛/活动一手源）测试 —— 全部离线、不打网络

覆盖：
- _is_open 纯函数边界：未截止 / 已截止 / 恰好等于 now（不算未截止）/ 缺字段 /
  0（无截止时间→保守保留）/ 非数值 / 布尔值；
- _deadline_to_str：正常 unix 秒 → YYYY-MM-DD；0 / 非数值 → None；
- 失败静默降级：Success=false → []、缺 Data.Races → []、非 200 → []、
  requests 异常 → []、非 JSON → []（一律不抛异常，符合 base.py 约定）；
- _served 轮级语义：同实例第二次调用返回 []；**失败后同样置位**（不重试）；
- 忽略 limit：本源语义是"返回所有报名未截止的条目"，limit=5 不截断；
- 与 query 无关：不同 query 得到同一批结果（首次调用）；
- Evidence 映射：source_url = /events/<Id>、confidence=0.9、snippet=Brief、
  entities 携带 Category（供 notes 落库）；
- P6-1②：`entities` 回带 EndTime / RegistrationType / MemberLimit / PlayerNum /
  BonusDesc / SignUpUrl / SubmitUrl（**EndTime=0 保留**，0 = 接口未提供）；
- P6-1③：`entities` 派生 `deadline_is_weekday` / `deadline_weekday`，
  未知一律 None（不是 False）；
- P6-3：`resolve_deadline` 保留两个来源的原值、逐秒判冲突、取**较晚**者；
  `_is_open(race, now, alt)` 用较晚者；`entities.deadline_conflict` 标记；
- P6-4：正文主路径是**列表行 Content**（具名分段数组 / 单棵树两种形态都认）；
  `_detail_body_or_none` 的成功判据是 `Data.Race` 存在且非空，并各有一条
  **假成功**（Success=true 但载荷为空）与**显式失败码**（10011701001）用例；
  正文缺失不构成跳过一行的理由；
- 端到端（仍离线，打桩 _fetch_all_races）：scan_once 只入库未截止条目、
  重复执行第二次 inserted=0（验证 /events/<Id> 的跨轮幂等）、notes 落 Category、
  type/eligibility 由既有规则给出；
- get_default_providers 已含 ModelScopeProvider。

所有网络交互通过替换 provider._fetch_all_races 或 core.information_engine.
search_providers.modelscope_provider.requests.get 打桩；数据库用临时库隔离。
"""
import os
import sys
import time
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（须在导入任何访问数据库的模块之前）----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

import memory.database as _database_module  # noqa: E402
from memory.database import init_db, get_connection  # noqa: E402
from core.information_engine.search_providers import (  # noqa: E402
    ModelScopeProvider,
    get_default_providers,
)
from core.information_engine.search_providers.modelscope_provider import (  # noqa: E402
    LANDING_HOP_TIMEOUT,
    LANDING_IFRAME_TIMEOUT,
    LANDING_MAX_FETCHES,
)
from core.resource_scout import ResourceScout  # noqa: E402

_PROVIDER_MOD = "core.information_engine.search_providers.modelscope_provider"

#: 固定"当前时间"（unix 秒），让截止判断确定性；取 2026-09-13 00:00:00 UTC
_NOW = 1789257600.0


def setup():
    """初始化独立测试数据库"""
    init_db()


def teardown():
    """清理：清空本测试写入的机会行（临时库，不影响生产库）"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM opportunities")
        conn.commit()
    finally:
        conn.close()
    cleanup_test_db()


def _race(rid, title, deadline, **extra):
    """构造一条 race（字段名与魔搭接口一致）"""
    r = {
        "Id": rid,
        "Title": title,
        "Brief": f"{title} 的简述",
        "Category": "hackathon",
        "Content": '["root",{},["p",{},"富文本，不是 HTML"]]',
        "RegistrationDeadline": deadline,
        "Status": 1,
        "PublishStatus": 1,
    }
    r.update(extra)
    return r


def _payload(races, total=None, success=True, code=200):
    """构造列表接口响应体"""
    body = {"Success": success, "Code": code}
    if success:
        body["Data"] = {"Races": races,
                        "TotalCount": len(races) if total is None else total}
    return body


class _FakeResp:
    """最小 requests.Response 替身"""

    def __init__(self, payload=None, status_code=200, raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _StubProvider(ModelScopeProvider):
    """用固定 race 列表替换网络拉取（仍走真实的过滤 / 映射 / _served 逻辑）"""

    def __init__(self, races, **kw):
        kw.setdefault("now_ts", _NOW)
        super().__init__(**kw)
        self._races = races
        self.fetch_calls = 0

    def _fetch_all_races(self):
        self.fetch_calls += 1
        return list(self._races)


# ---------- 1. _is_open 纯函数边界 ----------

def test_is_open_boundaries():
    """_is_open：只用 RegistrationDeadline 判断；缺字段 / 0 保守保留"""
    future = _NOW + 86400
    past = _NOW - 86400
    assert ModelScopeProvider._is_open({"RegistrationDeadline": future}, _NOW) is True
    assert ModelScopeProvider._is_open({"RegistrationDeadline": past}, _NOW) is False
    # 恰好等于当前时间：不算"未截止"（> 而非 >=）
    assert ModelScopeProvider._is_open(
        {"RegistrationDeadline": _NOW}, _NOW) is False
    # 缺字段 / None / 非数值 → 保守保留（不因字段缺失静默丢弃一手机会）
    assert ModelScopeProvider._is_open({}, _NOW) is True
    assert ModelScopeProvider._is_open({"RegistrationDeadline": None}, _NOW) is True
    assert ModelScopeProvider._is_open(
        {"RegistrationDeadline": "2026-12-31"}, _NOW) is True
    # 0 = 接口的"无截止时间"
    assert ModelScopeProvider._is_open({"RegistrationDeadline": 0}, _NOW) is True
    # bool 不是合法时间戳（isinstance(True, int) 为真，需显式排除）
    assert ModelScopeProvider._is_open({"RegistrationDeadline": True}, _NOW) is True
    # 不因 Status 而改变判定（枚举语义未核实，见模块 docstring）
    assert ModelScopeProvider._is_open(
        {"RegistrationDeadline": future, "Status": 9}, _NOW) is True
    print("[OK] modelscope provider _is_open boundaries")


def test_is_open_ignores_status():
    """_is_open 完全无视 Status：同截止时间下 Status=1/9 结果一致"""
    future = _NOW + 100
    a = ModelScopeProvider._is_open({"RegistrationDeadline": future, "Status": 1}, _NOW)
    b = ModelScopeProvider._is_open({"RegistrationDeadline": future, "Status": 9}, _NOW)
    assert a == b is True
    print("[OK] modelscope provider _is_open ignores status")


def test_deadline_to_str():
    """_deadline_to_str：unix 秒 → YYYY-MM-DD；非法 → None"""
    s = ModelScopeProvider._deadline_to_str(1789056000)
    assert isinstance(s, str) and len(s) == 10 and s.count("-") == 2, s
    assert ModelScopeProvider._deadline_to_str(0) is None
    assert ModelScopeProvider._deadline_to_str(None) is None
    assert ModelScopeProvider._deadline_to_str("x") is None
    assert ModelScopeProvider._deadline_to_str(True) is None
    print("[OK] modelscope provider deadline formatting")


# ---------- 2. 映射与 limit 语义 ----------

def test_maps_race_to_evidence():
    """Evidence 映射：/events/<Id> 稳定 URL、confidence 0.9、Category 进 entities"""
    prov = _StubProvider([_race(365, "千问办公实战营", _NOW + 1000)])
    evs = prov.search("任意查询", limit=5)
    assert len(evs) == 1, evs
    ev = evs[0]
    assert ev.source_url == "https://www.modelscope.cn/events/365", ev.source_url
    assert ev.source_name == ModelScopeProvider.name
    assert ev.title == "千问办公实战营"
    assert ev.snippet == "千问办公实战营 的简述"
    assert ev.confidence == 0.9, ev.confidence
    assert ev.retrieved_at, ev.retrieved_at
    assert '"Category": "hackathon"' in (ev.entities or ""), ev.entities
    print("[OK] modelscope provider maps race to evidence")


def _local_ts(text):
    """'YYYY-MM-DD HH:MM' → **本地时区** unix 秒

    与 provider 侧 `weekday_index_of_timestamp`（`datetime.fromtimestamp`）同源，
    因此在任何时区下"本地 2026-09-14 12:00"都是周一——用裸 unix 常量会让
    星期断言随机器时区漂移。
    """
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def test_entities_carry_back_raw_fields():
    """P6-1②：此前被丢弃的七个接口字段原样回到 Evidence.entities

    **EndTime=0 必须保留**：0 是接口"未提供作品提交截止"的明确写法，
    丢掉它显示层就只能把"未抓到"渲染成"无"（见 P6-5b②）。
    """
    import json

    race = _race(
        371, "字段回带验证", _local_ts("2026-09-14 12:00"),
        EndTime=0,
        RegistrationType="团队赛",
        MemberLimit=4,
        PlayerNum=17,
        BonusDesc="一等奖 5000 元",
        SignUpUrl="https://modelscope.cn/active/AIstudio",
        SubmitUrl="https://modelscope.cn/active/AIstudio/submit",
        # 未列入回带清单的字段不该被顺手带进来（清单是白名单，不是整个 race）
        OrganizerInfo={"name": "不该出现"},
    )
    ents = ModelScopeProvider._entities_from_race(race)
    assert ents["EndTime"] == 0, ents          # 0 保留，不是"缺字段"
    assert ents["RegistrationType"] == "团队赛", ents
    assert ents["MemberLimit"] == 4, ents
    assert ents["PlayerNum"] == 17, ents
    assert ents["BonusDesc"] == "一等奖 5000 元", ents
    assert ents["SignUpUrl"] == "https://modelscope.cn/active/AIstudio", ents
    assert ents["SubmitUrl"].endswith("/submit"), ents
    assert "OrganizerInfo" not in ents, ents
    # 缺失字段不进 entities（而不是写 None），但 EndTime=0 这类"有值"必须在
    assert "BonusDesc" not in ModelScopeProvider._entities_from_race(
        {"Category": "devEvent"})
    # 真值形态：Evidence.entities 是可 json.loads 的字符串，键值都是 JSON 原生类型
    # （打桩两跳：本用例的 SignUpUrl 恰好命中 /active/ 触发条件，不打桩会真联网）
    with patch.object(ModelScopeProvider, "_fetch_landings", return_value={}):
        ev = _StubProvider([race]).search("任意查询")[0]
    parsed = json.loads(ev.entities)
    assert parsed["MemberLimit"] == 4 and parsed["deadline_is_weekday"] is True
    print("[OK] modelscope provider entities carry back raw fields")


def test_entities_derive_weekday_flag():
    """P6-1③：deadline_is_weekday / deadline_weekday 由 RegistrationDeadline 派生

    实锚：2026-09-14 周一、09-18 周五、09-19 周六、09-13 周日。
    **字段缺失 / 非法时必须为 None，不能是 False**——"不知道"不能写成
    "不是工作日"，否则显示层会把未知当成已知。
    """
    wd = ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": _local_ts("2026-09-14 12:00")})
    assert wd["deadline_is_weekday"] is True, wd
    assert wd["deadline_weekday"] == "周一", wd
    assert ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": _local_ts("2026-09-18 23:59")}
    )["deadline_weekday"] == "周五"
    we = ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": _local_ts("2026-09-19 23:59")})
    assert we["deadline_is_weekday"] is False and we["deadline_weekday"] == "周六", we
    assert ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": _local_ts("2026-09-13 08:00")}
    )["deadline_is_weekday"] is False
    # 未知一律 None（含 0 = 接口的"无报名截止时间"）
    for bad in ({}, {"RegistrationDeadline": 0},
                {"RegistrationDeadline": None},
                {"RegistrationDeadline": "2026-12-31"},
                {"RegistrationDeadline": True}):
        ents = ModelScopeProvider._entities_from_race(bad)
        assert ents["deadline_is_weekday"] is None, (bad, ents)
        assert ents["deadline_weekday"] is None, (bad, ents)
    print("[OK] modelscope provider derives weekday flag")


def test_resolve_deadline_keeps_both_values():
    """P6-3①：两个来源的报名截止**都保留**，不打一架就丢掉一个

    真实样本（2026-09-13）：接口写 09-13 23:59 且 Status=1，官方手册写 09-13
    00:00 通道关闭。设计侧选了悲观值并**整节归档**，用户因此错过报名——
    **归档是比选错值更大的错**。
    """
    from core.information_engine.search_providers.modelscope_provider import (
        resolve_deadline,
        DEADLINE_SOURCE_INTERFACE,
        DEADLINE_SOURCE_LANDING,
    )

    a = _local_ts("2026-09-13 23:59")
    b = _local_ts("2026-09-13 00:00")
    got = resolve_deadline([(DEADLINE_SOURCE_INTERFACE, a),
                            (DEADLINE_SOURCE_LANDING, b)])
    # ③ 取较晚者
    assert got["value"] == a, got
    # ① 两个值都在数据面里
    assert got["sources"][DEADLINE_SOURCE_INTERFACE] == a, got
    assert got["sources"][DEADLINE_SOURCE_LANDING] == b, got
    # ② 逐秒比较才算冲突：同一天但不同时刻也是冲突（本批样本正是这种）
    assert got["conflict"] is True, got

    # 两个来源一致 → 不报冲突
    same = resolve_deadline([(DEADLINE_SOURCE_INTERFACE, a),
                             (DEADLINE_SOURCE_LANDING, a)])
    assert same["conflict"] is False and same["value"] == a, same

    # 只有一个来源给了值 → 不报冲突，但也如实记下"另一个来源没给"
    one = resolve_deadline([(DEADLINE_SOURCE_INTERFACE, 0),
                            (DEADLINE_SOURCE_LANDING, a)])
    assert one["value"] == a and one["conflict"] is False, one
    assert one["sources"][DEADLINE_SOURCE_INTERFACE] == 0, one   # 0 原样保留
    # 两个都没给 → value 为 None（显示层据此说"未抓到"，不许静默填空）
    none = resolve_deadline([(DEADLINE_SOURCE_INTERFACE, None),
                             (DEADLINE_SOURCE_LANDING, 0)])
    assert none["value"] is None and none["conflict"] is False, none
    print("[OK] resolve_deadline keeps both values")


def test_is_open_uses_later_value_on_conflict():
    """P6-3③：来源冲突时"是否还开放"用**较晚的**那个值（保守方向 = 活着）"""
    early = _NOW - 3600          # 已过
    late = _NOW + 3600           # 未过
    race = {"RegistrationDeadline": early}
    assert ModelScopeProvider._is_open(race, _NOW) is False
    # 落地页给出更晚的值 → 判为仍开放，并保留接口原值不覆盖
    assert ModelScopeProvider._is_open(race, _NOW, late) is True
    # 顺序无关：接口晚、落地页早 → 同样取较晚者
    assert ModelScopeProvider._is_open(
        {"RegistrationDeadline": late}, _NOW, early) is True
    # alt 给不出合法值时行为与不传一致（0 / None / 非数值都不参与 max，
    # 否则 0 会把接口的真值压掉）
    for junk in (0, None, "2026-12-31", True):
        assert ModelScopeProvider._is_open(race, _NOW, junk) is False, junk
        assert ModelScopeProvider._is_open(
            {"RegistrationDeadline": late}, _NOW, junk) is True, junk
    # 接口 0（未提供）+ 落地页有值 → 用落地页
    assert ModelScopeProvider._is_open(
        {"RegistrationDeadline": 0}, _NOW, late) is True
    print("[OK] is_open uses later value on conflict")


def test_entities_mark_conflict_and_keep_sources():
    """P6-3①②：entities 打 deadline_conflict 标记并保留两个来源的原值"""
    early = _local_ts("2026-09-13 00:00")
    late = _local_ts("2026-09-13 23:59")
    ents = ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": early},
        landing={"registration_deadline_ts": late})
    assert ents["deadline_conflict"] is True, ents
    assert ents["RegistrationDeadline"] == early, ents
    assert ents["deadline_sources"]["接口"] == early, ents
    assert ents["deadline_sources"]["落地页"] == late, ents
    assert ents["deadline_chosen"] == late, ents          # 较晚者胜出
    # published_at 与开放判据同源：落库的日期必须是消解后的那个
    ev = _StubProvider([
        _race(381, "冲突样本", late),
    ]).search("任意查询")[0]
    assert ev.published_at == "2026-09-13", ev.published_at
    # 没有落地页结果 → 不报冲突
    ents2 = ModelScopeProvider._entities_from_race(
        {"RegistrationDeadline": late})
    assert ents2["deadline_conflict"] is False, ents2
    assert ents2["deadline_sources"]["落地页"] is None, ents2
    print("[OK] entities mark conflict and keep sources")


# ---------- P6-4 正文主路径 = 列表行，不是 detail ----------

def test_list_row_content_is_named_segment_array():
    """P6-4②：Content 是**具名分段数组**，不是单棵树——两种形态都要认

    实测（2026-09-13）：`type(Content) is str`，`json.loads` 后形如
    `[{"name": "…", "content": ["root",{},["p",{},…]]}]`。上一版按"单棵树"走平
    会解析成 0 字，并因此得出"列表行没有正文"的错误结论。
    """
    import json as _json

    seg = [{"name": "赛制说明",
            "content": ["root", {}, ["p", {"jc": "center"},
                                     ["span", {"data-type": "text"},
                                      ["span", {"data-type": "leaf"},
                                       "报名时间八月十九日"]]]]},
           {"name": "参赛须知",
            "content": ["root", {}, ["p", {}, ["span", {}, "作品须为原创"]]]}]
    text = ModelScopeProvider._extract_body_text(
        {"Content": _json.dumps(seg, ensure_ascii=False)})
    # 两段都取到，且**标签名没有混进正文**（root / p / span / text / leaf）
    assert "报名时间八月十九日" in text, text
    assert "作品须为原创" in text, text
    for tag in ("root", "span", "data-type", "leaf"):
        assert tag not in text, (tag, text)
    assert ModelScopeProvider._chinese_char_count(text) == 15, text

    # 旧形态（单棵树）仍然认——解析器不是"改成只认新形态"，而是"先判首元素"
    single = ["root", {}, ["p", {}, ["span", {}, "单棵树形态的正文"]]]
    assert ModelScopeProvider._extract_body_text({"Content": single}) == "单棵树形态的正文"
    # Content 已经是解析好的对象（而非字符串）也要能处理
    assert "单棵树形态的正文" in ModelScopeProvider._extract_body_text(
        {"Content": single})
    # 空 / 脏输入 → 空串，不抛异常
    for bad in ({}, {"Content": ""}, {"Content": None}, {"Content": "不是 JSON"},
                {"Content": "{}"}, {"Content": []}):
        assert ModelScopeProvider._extract_body_text(bad) == "", bad
    print("[OK] list row content is a named segment array")


def test_detail_fake_success_is_treated_as_failure():
    """P6-4③④ 失败形态一：**假成功**——Success=true 但 Data 里没有 Race

    "Success:true 但载荷为空"是最阴的失败：它看起来像成功。实测多行返回
    `{"Code":200,"Message":"success","Success":true}` 而 Data 里没有 Race。
    只看 Success 字段的写法会把它当成"这一行没有正文"，进而当成跳过这一行的
    理由——而正文其实一直在列表行里。
    """
    # 真·假成功：连 Data 都没有
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 200, "Message": "success", "Success": True}) is None
    # Data 在但 Race 缺失 / 为 None / 为空对象 / 不是 dict
    for data in ({}, {"Race": None}, {"Race": {}}, {"Race": []}, {"Race": "x"}):
        assert ModelScopeProvider._detail_body_or_none(
            {"Code": 200, "Success": True, "Data": data}) is None, data
    # 非 dict 载荷（列表 / 字符串 / None）
    for bad in (None, "success", [], 0):
        assert ModelScopeProvider._detail_body_or_none(bad) is None, bad
    # Race 非空 → 成功；正文为空则是"拿到了对象、它没有正文"（空串，不是 None）
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 200, "Success": True,
         "Data": {"Race": {"Id": 1, "Content": ""}}}) == ""
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 200, "Success": True,
         "Data": {"Race": {"Id": 1, "Content": ["root", {}, ["p", {}, "详情正文"]]}}}
    ) == "详情正文"
    print("[OK] detail fake success treated as failure")


def test_detail_explicit_failure_code_is_treated_as_failure():
    """P6-4③④ 失败形态二：**显式失败码**（Code=10011701001「获取竞赛失败」）

    与"假成功"成对：这条至少自报了失败，但判据必须同样落在 `Data.Race` 上——
    只认失败码、不认载荷的写法会漏掉形态一，只认载荷、不认码的写法在这里也对，
    所以真正的判据只有一条：**Data.Race 存在且非空**。
    """
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 10011701001, "Message": "获取竞赛失败",
         "Success": False}) is None
    # 显式失败码 + 结构上"看起来有 Data"（但 Race 为空）→ 仍然失败
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 10011701001, "Message": "获取竞赛失败", "Success": False,
         "Data": {"Race": {}}}) is None
    # 失败码但 Race 竟然非空 → 判据优先看载荷（接口自相矛盾时以数据为准）
    assert ModelScopeProvider._detail_body_or_none(
        {"Code": 10011701001, "Success": False,
         "Data": {"Race": {"Content": ["root", {}, ["p", {}, "有正文"]]}}}
    ) == "有正文"
    print("[OK] detail explicit failure code treated as failure")


def test_missing_body_never_skips_a_row():
    """P6-4①：正文缺失**不是**跳过一行的理由；正文主路径是列表行

    另有一类薄正文行（境外第三方站、零 Content），它们不满足 P6-5 的两跳条件，
    本批**不给它们扩抓取通道**——但也不能因此丢掉这一行。
    """
    thin = _race(391, "零正文行", _NOW + 86400, Content="")
    evs = _StubProvider([thin]).search("任意查询")
    assert len(evs) == 1, evs
    import json as _json
    ents = _json.loads(evs[0].entities)
    assert ents["ContentChars"] == 0, ents
    assert evs[0].published_at, "正文缺失不得影响报名截止的换算"
    print("[OK] missing body never skips a row")


# ---------- P6-5 落地页两跳（触发条件收窄 + 独立超时 + 失败等于没这功能） ----------

#: 第二跳 Next.js 载荷的**真实结构摘录**（2026-09-13 22:27 实测，字段名逐字取自
#: 抓回的 HTML：schedule.stages[].timeline_label / .date）。这里不联网、也不放
#: 整段 55KB HTML，只放解析器真正依赖的那几个 JSON 字段。
_FLIGHT_HTML = (
    '<script>self.__next_f.push([1,"12:{\\"hero\\":{\\"title\\":\\"AI无限\\"},'
    '\\"schedule\\":{\\"title\\":\\"比赛日程\\",\\"stages\\":['
    '{\\"timeline_label\\":\\"报名\\",\\"card_title\\":\\"第一步\\",'
    '\\"date\\":\\"08.19 – 09.14 12:00\\"},'
    '{\\"timeline_label\\":\\"作品提交\\",\\"card_title\\":\\"第二步\\",'
    '\\"date\\":\\"08.19 – 09.14 22:00\\"},'
    '{\\"timeline_label\\":\\"专家评审\\",\\"date\\":\\"09.15 – 09.18 23:59\\"}]}}"])'
    "</script>"
)


def _rich_content(text):
    """把一段正文包成列表行 Content 的真实形态（单棵树）"""
    import json as _json
    return _json.dumps(["root", {}, ["p", {}, ["span", {}, text]]],
                       ensure_ascii=False)


def _landing_race(rid, signup="https://modelscope.cn/active/AIstudio",
                  content=None):
    """构造一条命中两跳触发条件的 race（薄正文 + /active/ 入口）"""
    return _race(rid, "落地页样本", _local_ts("2026-09-14 12:00"),
                 SignUpUrl=signup,
                 Content=_rich_content("短正文") if content is None else content,
                 EndTime=0)


def test_landing_trigger_conditions():
    """P6-5①：**薄正文 + /active/ 入口**两个条件同时满足才走两跳

    计数依据（2026-09-13 23:55 实测，不是推测）：未截止 12 行里 SignUpUrl 命中
    /active/ 的只有 1 行；单看正文厚度会命中 4 行。另有一类薄正文行（境外第三方
    站、零 Content）**不满足**入口条件，本批不给它们扩抓取通道。
    """
    thin = _rich_content("短正文")            # 3 个中文字
    fat = _rich_content("长" * 500)
    assert ModelScopeProvider._landing_triggered(_landing_race(401, content=thin)) is True
    # 正文够厚 → 不走（哪怕入口命中）
    assert ModelScopeProvider._landing_triggered(
        _landing_race(402, content=fat)) is False
    # 入口不是 /active/ → 不走（哪怕正文很薄：境外第三方站就是这一类）
    for url in ("", "https://flagos.io/Register?utm_source=modelscope",
                "https://alidocs.dingtalk.com/notable/share/form/xxx",
                "https://modelscope.cn/active/",          # 光秃秃的目录路径
                "https://modelscope.cn/events/347"):
        assert ModelScopeProvider._landing_triggered(
            _landing_race(403, signup=url, content=thin)) is False, url
    # 阈值边界：恰好 400 不算"薄"（严格小于）
    assert ModelScopeProvider._chinese_char_count("长" * 400) == 400
    assert ModelScopeProvider._landing_triggered(
        _landing_race(404, content=_rich_content("长" * 400))) is False
    assert ModelScopeProvider._landing_triggered(
        _landing_race(405, content=_rich_content("长" * 399))) is True
    print("[OK] landing trigger conditions")


def test_landing_parse_reads_named_json_fields():
    """P6-5②：解析取**内嵌 JSON 字段**，不拿正则扫中文

    一手对齐证据：接口 RegistrationDeadline 与页面 `报名 08.19–09.14 12:00`
    逐字符吻合；同页 `作品提交 08.19–09.14 22:00` 在接口里完全没有（EndTime=0）
    ——这正是这两跳存在的理由。
    """
    from datetime import datetime

    payload = ModelScopeProvider._extract_flight_payload(_FLIGHT_HTML)
    assert '"schedule"' in payload, payload
    parsed = ModelScopeProvider._parse_landing_schedule(payload, year=2026)
    assert [s["label"] for s in parsed["stages"]] == ["报名", "作品提交", "专家评审"]
    expect_reg = time.mktime(datetime(2026, 9, 14, 12, 0).timetuple())
    expect_sub = time.mktime(datetime(2026, 9, 14, 22, 0).timetuple())
    assert parsed["registration_ts"] == expect_reg, parsed
    assert parsed["submit_ts"] == expect_sub, parsed
    # 两根轴是独立的：提交截止比报名截止晚 10 小时，压成一个就会丢掉这个事实
    assert parsed["submit_ts"] > parsed["registration_ts"]

    # 只认实测到的 MM.DD[ HH:MM] 形状，其余返回 None（不猜）
    assert ModelScopeProvider._parse_landing_date("08.19 – 09.14 12:00", 2026) \
        == expect_reg
    assert ModelScopeProvider._parse_landing_date("09.19", 2026) \
        == time.mktime(datetime(2026, 9, 19, 0, 0).timetuple())
    for bad in ("", None, "待定", "8月19日", "2026-09-14"):
        assert ModelScopeProvider._parse_landing_date(bad, 2026) is None, bad
    # 载荷里没有 schedule / 结构不对 → 空结果（调用方据此退回"只有报名截止"）
    for bad_payload in ("", "self.__next_f.push([1,\"no schedule here\"])",
                        '{"schedule": {"stages": []}}',
                        '{"schedule": {"stages": "not a list"}}'):
        got = ModelScopeProvider._parse_landing_schedule(bad_payload, year=2026)
        assert got["stages"] == [] and got["registration_ts"] is None, bad_payload
    # 花括号配对必须跳过字符串内部的括号，否则会被提前截断
    tricky = '{"schedule":{"title":"a}b{c","stages":[{"timeline_label":"报名",' \
             '"date":"09.14 12:00"}]}}'
    assert ModelScopeProvider._extract_json_object(tricky, "schedule") is not None
    print("[OK] landing parse reads named json fields")


def test_landing_two_hops_and_feeds_conflict():
    """P6-5③：两跳成功 → 结果进 entities；报名截止按 P6-3 取较晚者并存两个值"""
    iframe = "https://mseo-ai-inf.ms.show/"
    hop1 = _FakeResp(status_code=200)
    hop1.text = f'<html><iframe src="{iframe}"></iframe></html>'
    hop1.content = hop1.text.encode("utf-8")
    hop2 = _FakeResp(status_code=200)
    hop2.text = _FLIGHT_HTML
    hop2.content = _FLIGHT_HTML.encode("utf-8")

    race = _landing_race(411)
    prov = _StubProvider([race])
    with patch(f"{_PROVIDER_MOD}.requests.get",
               side_effect=[hop1, hop2]) as m:
        evs = prov.search("任意查询")
    assert m.call_count == 2, m.call_args_list        # 恰好两跳，不多不少
    assert m.call_args_list[0][0][0] == "https://modelscope.cn/active/AIstudio"
    assert m.call_args_list[1][0][0] == iframe
    # ④ 两跳各有自己的超时，且都不等于列表接口的 20s 假设
    assert m.call_args_list[0][1]["timeout"] == LANDING_HOP_TIMEOUT
    assert m.call_args_list[1][1]["timeout"] == LANDING_IFRAME_TIMEOUT
    assert LANDING_HOP_TIMEOUT != 20.0 and LANDING_IFRAME_TIMEOUT != 20.0

    import json as _json
    ents = _json.loads(evs[0].entities)
    assert ents["LandingPageUrl"] == iframe, ents
    assert ents["LandingFetchedAt"], ents
    assert [s["label"] for s in ents["LandingStages"]][:2] == ["报名", "作品提交"], ents
    # 落地页 09.14 12:00 与接口同值 → 不算冲突；提交截止则被补齐
    assert ents["deadline_conflict"] is False, ents
    assert ents["SubmitDeadlineTs"] is not None, ents
    assert ents["deadline_chosen"] == _local_ts("2026-09-14 12:00"), ents
    print("[OK] landing two hops and feeds conflict")


def test_landing_failure_equals_no_feature():
    """P6-5⑤：抓失败**必须等于没这个功能**——退回"只有报名截止"，不许静默填空

    五种失败形态逐一验证：第一跳非 200 / 第一跳没有 iframe / 第二跳非 200 /
    第二跳没有内嵌载荷 / 请求直接抛异常。**每一种都必须让这一行照常入库**
    （不是返回 []），并在 entities 里如实留下"未抓到"的空位。
    """
    import json as _json

    def _code_of(ev):
        return _json.loads(ev.entities)

    cases = []
    # ① 第一跳非 200
    r = _FakeResp(status_code=500)
    r.text, r.content = "", b""
    cases.append(("hop1-500", [r]))
    # ② 第一跳没有 iframe
    r = _FakeResp(status_code=200)
    r.text = "<html>没有 iframe</html>"
    r.content = r.text.encode("utf-8")
    cases.append(("hop1-no-iframe", [r]))
    # ③/④ 第二跳非 200 / 没有载荷
    hop1 = _FakeResp(status_code=200)
    hop1.text = '<iframe src="https://mseo-ai-inf.ms.show/"></iframe>'
    hop1.content = hop1.text.encode("utf-8")
    r = _FakeResp(status_code=502)
    r.text, r.content = "", b""
    cases.append(("hop2-502", [hop1, r]))
    hop1b = _FakeResp(status_code=200)
    hop1b.text = hop1.text
    hop1b.content = hop1.content
    empty = _FakeResp(status_code=200)
    empty.text = "<html>没有任何内嵌载荷</html>"
    empty.content = empty.text.encode("utf-8")
    cases.append(("hop2-no-payload", [hop1b, empty]))

    for name, responses in cases:
        race = _landing_race(421)
        with patch(f"{_PROVIDER_MOD}.requests.get", side_effect=responses):
            evs = _StubProvider([race]).search("任意查询")
        assert len(evs) == 1, (name, evs)      # 失败不等于丢行
        ents = _code_of(evs[0])
        assert ents["LandingPageUrl"] is None, (name, ents)
        assert ents["LandingFetchedAt"] is None, (name, ents)
        assert ents["LandingStages"] == [], (name, ents)
        assert ents["SubmitDeadlineTs"] is None, (name, ents)
        # 只有报名截止那一条来源，且不报冲突（不静默填空，也不虚构第二个值）
        assert ents["deadline_conflict"] is False, (name, ents)
        assert ents["deadline_sources"]["落地页"] is None, (name, ents)
        assert evs[0].published_at == "2026-09-14", (name, evs[0].published_at)

    # ⑤ 请求直接抛异常（超时 / DNS / 连接重置）
    import requests as _requests
    race = _landing_race(422)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               side_effect=_requests.RequestException("boom")):
        evs = _StubProvider([race]).search("任意查询")
    assert len(evs) == 1 and evs[0].published_at == "2026-09-14"
    print("[OK] landing failure equals no feature")


def test_landing_fetches_are_bounded():
    """P6-5：两跳次数受硬预算约束，且优先给"按接口口径还开着"的行

    为什么必须设上界：`ResourceScout._call_search` 用 `future.result(timeout=
    timeout_sec)`（默认 15s）包住**整个 search()**；无上限的外呼循环一旦把这
    15s 拖爆，代价是这一轮已拉到的十几行**全部丢掉**。
    """
    closed = _landing_race(431)
    closed["RegistrationDeadline"] = _NOW - 86400      # 接口口径已截止
    open_ = _landing_race(432)
    # 两条都命中触发条件，预算只允许 1 次 → 必须先给"还开着"的那条
    prov = _StubProvider([closed, open_])
    with patch.object(ModelScopeProvider, "_fetch_landing") as m:
        m.return_value = {}
        prov.search("任意查询")
    assert m.call_count == LANDING_MAX_FETCHES == 1, m.call_args_list
    assert m.call_args_list[0][0][0].get("Id") == 432, m.call_args_list
    print("[OK] landing fetches are bounded")


def test_ignores_limit_per_query():
    """有意的基类偏离：limit=5 不得截断（本源语义是"返回所有未截止条目"）"""
    races = [_race(i, f"活动{i}", _NOW + 1000 + i) for i in range(12)]
    prov = _StubProvider(races)
    evs = prov.search("任意查询", limit=5)
    assert len(evs) == 12, len(evs)
    print("[OK] modelscope provider ignores limit_per_query")


def test_filters_closed_registrations():
    """只保留报名未截止的条目"""
    races = [
        _race(1, "未截止", _NOW + 1000),
        _race(2, "已截止", _NOW - 1000),
        _race(3, "也无截止时间", 0),
    ]
    prov = _StubProvider(races)
    titles = [e.title for e in prov.search("q")]
    assert titles == ["未截止", "也无截止时间"], titles
    print("[OK] modelscope provider filters closed registrations")


def test_skips_invalid_races():
    """缺 Id / 缺 Title 的条目被丢弃（不可点击的机会没有意义）"""
    races = [
        _race(1, "正常", _NOW + 1000),
        {"Title": "缺 Id", "RegistrationDeadline": _NOW + 1000},
        {"Id": 2, "Title": "   ", "RegistrationDeadline": _NOW + 1000},
    ]
    prov = _StubProvider(races)
    evs = prov.search("q")
    assert len(evs) == 1 and evs[0].title == "正常", [e.title for e in evs]
    print("[OK] modelscope provider skips invalid races")


# ---------- 3. _served 轮级语义 ----------

def test_served_makes_second_call_empty():
    """同实例第二次调用返回 []：一轮扫描只准跑一次网络拉取"""
    prov = _StubProvider([_race(1, "活动", _NOW + 1000)])
    assert len(prov.search("q1")) == 1
    assert prov.search("q2") == []
    assert prov.search("q3") == []
    assert prov.fetch_calls == 1, prov.fetch_calls   # 只拉了一次
    print("[OK] modelscope provider serves only once per instance")


def test_served_set_even_on_failure():
    """失败也置位 _served：单轮内网络失败不重试（用户需重新触发扫描）"""
    body = _payload([], success=False, code=500)
    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               return_value=_FakeResp(body)):
        assert prov.search("q1") == []
    assert prov._served is True, "_served 必须已置位"
    # 第二次直接返回 []，且**不再打网络**
    with patch(f"{_PROVIDER_MOD}.requests.get") as m:
        assert prov.search("q2") == []
        assert m.call_count == 0, "失败后第二次调用不应再打网络"
    print("[OK] modelscope provider sets _served on failure too")


def test_served_set_on_exception():
    """requests 抛异常同样置位 _served，且不向外抛"""
    import requests as _rq

    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               side_effect=_rq.RequestException("boom")):
        assert prov.search("q1") == []
    assert prov._served is True
    print("[OK] modelscope provider sets _served on exception")


# ---------- 4. 失败静默降级 ----------

def test_success_false_returns_empty():
    """Success=false → []（不抛异常）"""
    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               return_value=_FakeResp(_payload([], success=False, code=500))):
        assert prov.search("q") == []
    print("[OK] modelscope provider handles Success=false")


def test_missing_races_returns_empty():
    """schema 缺 Data.Races → []"""
    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               return_value=_FakeResp({"Success": True, "Code": 200,
                                       "Data": {"TotalCount": 142}})):
        assert prov.search("q") == []
    print("[OK] modelscope provider handles missing Races")


def test_non_200_returns_empty():
    """HTTP 非 200 → []"""
    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               return_value=_FakeResp({}, status_code=503)):
        assert prov.search("q") == []
    print("[OK] modelscope provider handles non-200")


def test_non_json_returns_empty():
    """非 JSON 响应 → []"""
    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get",
               return_value=_FakeResp(raise_json=True)):
        assert prov.search("q") == []
    print("[OK] modelscope provider handles non-json")


def test_pagination_accumulates_until_total():
    """翻页：按 TotalCount 累计，PageSize 每页大小；不重复拉已满的页"""
    calls = []

    def _fake_get(url, params=None, headers=None, timeout=None):
        page = params["PageNumber"]
        calls.append(page)
        if page == 1:
            return _FakeResp(_payload([_race(1, "A", _NOW + 1000)], total=2))
        return _FakeResp(_payload([_race(2, "B", _NOW + 1000)], total=2))

    prov = ModelScopeProvider(now_ts=_NOW, page_size=1)
    with patch(f"{_PROVIDER_MOD}.requests.get", side_effect=_fake_get):
        evs = prov.search("q")
    assert len(evs) == 2, [e.title for e in evs]
    assert calls == [1, 2], calls   # 达到 TotalCount=2 即停
    print("[OK] modelscope provider paginates until TotalCount")


def test_pagination_stops_at_max_pages():
    """max_pages 硬上限：TotalCount 异常大时不会无限翻页"""
    calls = []

    def _fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["PageNumber"])
        return _FakeResp(_payload([_race(params["PageNumber"], "A", _NOW + 1000)],
                                  total=9999))

    prov = ModelScopeProvider(now_ts=_NOW, page_size=1, max_pages=3)
    with patch(f"{_PROVIDER_MOD}.requests.get", side_effect=_fake_get):
        prov.search("q")
    assert calls == [1, 2, 3], calls
    print("[OK] modelscope provider stops at max_pages")


def test_anonymous_no_cookie_header():
    """匿名请求：只带非空 User-Agent，不携带任何 cookie"""
    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured.update(headers or {})
        return _FakeResp(_payload([], total=0))

    prov = ModelScopeProvider(now_ts=_NOW)
    with patch(f"{_PROVIDER_MOD}.requests.get", side_effect=_fake_get):
        prov.search("q")
    assert captured.get("User-Agent"), captured
    lowered = {k.lower() for k in captured}
    assert "cookie" not in lowered, captured
    print("[OK] modelscope provider sends anonymous headers")


# ---------- 5. 端到端（离线打桩，仍不打网络）----------

def test_scan_once_inserts_open_only_and_dedups():
    """端到端：scan_once 只入库未截止条目；重复执行第二次 inserted=0（URL 幂等）"""
    teardown_conn = get_connection()
    try:
        teardown_conn.execute("DELETE FROM opportunities")
        teardown_conn.commit()
    finally:
        teardown_conn.close()

    races = [
        _race(101, "校园黑客松 2026", _NOW + 86400, Category="hackathon"),
        _race(102, "已结束的征文", _NOW - 86400, Category="devEvent"),
        _race(103, "数据库大赛", _NOW + 172800, Category="application"),
    ]
    prov = _StubProvider(races)
    scout = ResourceScout(queries=["q1", "q2"], providers=[prov])
    report = scout.scan_once()
    assert report["errors"] == [], report["errors"]
    assert report["inserted"] == 2, report      # 已截止的 102 不入库
    assert prov.fetch_calls == 1, prov.fetch_calls   # 两条查询只拉一次

    rows = {r["source_url"]: r for r in ResourceScout.list_opportunities()}
    assert "https://www.modelscope.cn/events/101" in rows
    assert "https://www.modelscope.cn/events/103" in rows
    assert "https://www.modelscope.cn/events/102" not in rows
    # Category 经 entities → notes 落库（P6-5b 起 notes 是分段集合，第一段是
    # "以下字段抓取自接口…" 的来源说明，类目退为第二段）
    notes_101 = rows["https://www.modelscope.cn/events/101"]["notes"]
    assert "类目：hackathon" in notes_101, notes_101
    assert "类目：application" in \
        rows["https://www.modelscope.cn/events/103"]["notes"]
    assert notes_101.startswith("以下字段抓取自接口，抓取时刻 "), notes_101

    # 跨轮幂等：新实例（每轮重建）再扫一次，URL 相同 → inserted=0
    prov2 = _StubProvider(races)
    scout2 = ResourceScout(queries=["q1"], providers=[prov2])
    report2 = scout2.scan_once()
    assert report2["inserted"] == 0, report2
    assert report2["duplicates"] >= 2, report2
    print("[OK] modelscope provider scan_once idempotent by stable url")


def test_default_providers_includes_modelscope():
    """默认组合已含 ModelScopeProvider（替换点集中在 get_default_providers）"""
    names = [p.name for p in get_default_providers()]
    assert ModelScopeProvider.name in names, names
    print("[OK] default providers include modelscope")


# ---------- 主入口（无 pytest 时直接运行）----------
def _run_all():
    setup()
    try:
        test_is_open_boundaries()
        test_is_open_ignores_status()
        test_deadline_to_str()
        test_maps_race_to_evidence()
        # P6-1②/③：entities 回带原始字段 + 派生"是否落在周中"
        test_entities_carry_back_raw_fields()
        test_entities_derive_weekday_flag()
        # P6-3①②③：来源冲突不择一消解（两个值都留 + 打标记 + 取较晚者）
        test_resolve_deadline_keeps_both_values()
        test_is_open_uses_later_value_on_conflict()
        test_entities_mark_conflict_and_keep_sources()
        # P6-4：正文主路径 = 列表行 Content；detail 判据与两种失败形态
        test_list_row_content_is_named_segment_array()
        test_detail_fake_success_is_treated_as_failure()
        test_detail_explicit_failure_code_is_treated_as_failure()
        test_missing_body_never_skips_a_row()
        # P6-5：落地页两跳（触发条件 / 内嵌 JSON 解析 / 失败=没这功能 / 预算上界）
        test_landing_trigger_conditions()
        test_landing_parse_reads_named_json_fields()
        test_landing_two_hops_and_feeds_conflict()
        test_landing_failure_equals_no_feature()
        test_landing_fetches_are_bounded()
        test_ignores_limit_per_query()
        test_filters_closed_registrations()
        test_skips_invalid_races()
        test_served_makes_second_call_empty()
        test_served_set_even_on_failure()
        test_served_set_on_exception()
        test_success_false_returns_empty()
        test_missing_races_returns_empty()
        test_non_200_returns_empty()
        test_non_json_returns_empty()
        test_pagination_accumulates_until_total()
        test_pagination_stops_at_max_pages()
        test_anonymous_no_cookie_header()
        test_scan_once_inserts_open_only_and_dedups()
        test_default_providers_includes_modelscope()
    finally:
        teardown()
    print("\n=== ALL MODELSCOPE PROVIDER TESTS PASSED ===")


if __name__ == "__main__":
    _run_all()
