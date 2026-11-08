# -*- coding: utf-8 -*-
"""
Supervisor 旁路测试：观测（Phase 3a／3a′）＋问句生成（阶段一 2026-10-11）

对应实现是 `agents/supervisor.supervisor_observe` 与 `agents/supervisor.supervisor_ask_questions`。

离线：全程 mock 模型调用，**不打网络、不写任何库表**。

背景：3a 要在 supervisor 旁边加一条输出 {问题, 建议下一步, 需要的能力}。拆分文档的字面
读法是把这条输出并进 supervisor_decision 的返回值，但 `core/runtime.py:145` 每轮对话都
调 supervisor_decision ⇒ 每轮多付一次模型调用，而这条旁路的消费者在 Phase 3b 的复盘
流程（每周一次的动作）。用户 2026-10-06 定案：**立函数不接线**。所以本文件验的是两件事：
①路由那半边**一个字节都没变**；②新函数自己那三条契约成立，且它真的不在主路径上。

patch 靶子是 `sv.chat_sync_with_retry_budget`（**不是** business.model_client 那份）：
supervisor.py 是 `from core.ai_filter import chat_sync_with_retry_budget`，读的是本模块
命名空间的属性，打在源头拦不住。

每条断言的负控说明（拆分文档的「恒真断言比没有断言更坏」）：
- `test_observe_*` 与 `test_prose_*`／`test_missing_*`／`test_capability_*`／
  `test_unknown_sentinel_*` 这些旁路自身的用例：旧实现里根本没有 supervisor_observe，
  跑在改前的代码上会 AttributeError 而**红**，所以它们不需要额外负控；
- `test_routing_snapshot_matches_pre_change_baseline` 与
  `test_routing_return_never_carries_observation_keys`（路由「没变」那两条）在旧实现上
  **必然绿——这就是它们的用途**（快照比对的意义就是改前改后一致）。它们的牙齿由
  `test_snapshot_comparator_has_teeth`（比对器本身）与
  `test_negative_control_wiring_would_be_caught`（「接线版」形状）两条分别提供；否则
  「逐键一致」这句就是装饰；
- `test_negative_control_routing_survives_without_bypass` 是判据③的负控本体：把旁路换成
  「一被调用就抛」的桩，路由用例仍全绿**且该桩调用次数为 0**——后半句才证明「旁路」不是
  客气话。

3a′（设计席 2026-10-06 补的前置项：输入从「用户消息」泛化为「观测载荷」）：
- `test_observe_accepts_a_state_summary_payload` 喂的是**用真实采集器现采的 State 摊成的
  文本**（不是手写一段假摘要），它同时检查「提示词还在把输入当用户消息」的残留措辞，并用
  关键字 `payload=` 调用——所以把参数名或提示词改回 3a 那一版，这一条就红，正是设计席
  要的负控；
- `test_negative_control_pre_3a_prompt_wording_is_caught` 与
  `test_negative_control_pre_3a_parameter_name_is_rejected` 把上面那句话**拆开各自钉住**：
  前者拿改前的原措辞过同一份检查器、必须返回非空问题清单（否则 REQUIRES/FORBIDS 是装饰），
  后者直接验旧参数名 `user_message` 已不被接受（在 3a 那一版上它是**成功**的，故当时必红）。

阶段一（设计席 2026-10-11 的决策模型改造）在本文件加的是两样东西，都不碰路由那半边：
- **观测多了一格「置信度」**（`sv.OBSERVE_KEY_CONFIDENCE`）。旁路自身的用例从写死三个字符串
  改成比对 `sv.OBSERVE_KEYS` 这个键表，所以加格时判据不用改，改的只有措辞——
  `test_confidence_is_clamped_or_none_never_fabricated` 钉的是这把尺（越界钳端点、字符串数
  收下、"abc"/true/null/缺席都算没报）与一句要紧的话：**没报不等于 0.5**，也等于说这一格
  转不动不许把前两格一起拖下降级出口。降级出口那条（`test_model_unavailable_*`）同步补了
  「置信度是 None」的断言，理由与 `_unable_observation` 的注释同源：0.5 看着中性，实际是替
  模型说了一句它没说过的话，而复盘那边把「没报」与「报了 0.5」读成同一件事，分档就白做；
- **第二条旁路 `supervisor_ask_questions`**（闸门②请求补充时「该问人哪三句」由它现生成）。
  三契约与观测同源，只有第②条换了写法：判不了给**空列表**而不是「无法判断」那句哨兵。
  拆成两条用例而不是一条，是因为「空列表」有两副面孔——模型答了空是**合法**的（安静返回），
  打不通是**失败**（必须出声）；混在一条里就没法分别钉住，而调用方正是靠这个区别决定
  reason 里写「模型现生成」还是「内置兜底问」。`test_negative_control_ask_bypass_not_wired`
  沿用 3a 的摘桩做法：桩一被调用就抛、再断调用次数为 0，因为「立函数不接线」的定案对
  **新增的这条同样成立**，少一条它就是没人管的那半句。
"""
import contextlib
import io
import os
import sys
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 独立测试数据库：本文件不写库，但 import 链上有 core.project_state →
# ---- memory.database，按仓库惯例仍在导入业务模块之前先切到临时库
from tests._test_db_helpers import setup_test_db, cleanup_test_db

setup_test_db()

import agents.supervisor as sv  # noqa: E402
from core import project_state as ps  # noqa: E402

# ---------- 改前基线快照（判据①）----------
# 这两个 dict 是 2026-10-06 在本批动手**之前**，用下面两个固定输入从当时的
# supervisor_decision 里抓出来的原样输出（模型调用被 patch 成定值）。抓取命令：
#   patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY) 与
#   side_effect=RuntimeError("模型不可用") 各调一次，json.dumps(..., sort_keys=True)
# 抄进本文件。此后任何一次改动只要让这两个形状里任何一键变了，用例 1 就红。
_ROUTING_REPLY = ('{"task": "学习", "domains": ["机器学习"], "difficulty": "中级", '
                  '"agent": ["AI学习", "通用助手"], "reason": "用户在问概念"}')

_BASELINE_OK = {
    "task": "学习",
    "domains": ["机器学习"],
    "difficulty": "中级",
    "agent": ["AI学习", "通用助手"],
    "reason": "用户在问概念",
}
_BASELINE_FAIL = {
    "task": "闲聊",
    "domains": ["通用"],
    "difficulty": "未知",
    "agent": ["通用助手"],
}

# 阶段一（2026-10-11）起这份桩回复带第四格「置信度」：0.6 刻意选在分档线
# （CONFIDENCE_SUGGESTION_MIN＝0.5）之上，本文件只验它能原样取出来，
# 分档本身在 tests/test_project_review_loop.py 那边验。
_OBSERVE_REPLY = ('{"问题": "缺少可运行的最小例子", '
                  '"建议下一步": "给一个 20 行的反向传播脚本", '
                  '"需要的能力": ["read_write_notes"], "置信度": 0.6}')

_ASK_REPLY = ('{"补充问题": ["这周想推进到哪一步？", "手上已经有什么材料？", '
              '"每周能给出多少时间？"]}')
# ^ 恰好等于 ASK_QUESTION_LIMIT 的那一份，用在 `test_ask_bypass_caps_at_the_limit_*` 的
#   第一组断言里：正好三条时**一条都不该被削掉**，否则封顶写成了截断就成了丢答案。


def _snapshot_diff(actual: dict, baseline: dict) -> dict:
    """逐键比对，返回差异（空 dict 表示完全一致）

    刻意不写 assert actual == baseline：那样红的时候只有一句「不相等」，看不出是多了键、
    少了键还是某个值被改了——而这三种在 3a 里含义不同（多了键＝接线了，值变了＝路由被改）。
    """
    diff = {}
    for key in sorted(set(actual) | set(baseline)):
        if key not in actual:
            diff[key] = {"missing": baseline[key]}
        elif key not in baseline:
            diff[key] = {"unexpected": actual[key]}
        elif actual[key] != baseline[key]:
            diff[key] = {"was": baseline[key], "now": actual[key]}
    return diff


# ---------- 判据①：路由返回结构与改前逐键一致 ----------

def test_routing_snapshot_matches_pre_change_baseline():
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY):
        got = sv.supervisor_decision("讲讲反向传播")
    diff = _snapshot_diff(got, _BASELINE_OK)
    assert not diff, f"路由返回结构与改前基线不一致：{diff}"

    with patch.object(sv, "chat_sync_with_retry_budget",
                      side_effect=RuntimeError("模型不可用")):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got_fail = sv.supervisor_decision("讲讲反向传播")
    diff_fail = _snapshot_diff(got_fail, _BASELINE_FAIL)
    assert not diff_fail, f"回退路径的返回结构与改前基线不一致：{diff_fail}"
    print("[OK] 路由返回结构逐键等于改前基线（成功形与回退形各一次）")


def test_snapshot_comparator_has_teeth():
    """用例 1 的负控：多一个键、改一个值、少一个键，比对器都必须抓到

    没有这一条，「逐键一致」就是一句可以随时被绕过的话——而 3a 最容易犯的恰恰是
    「顺手往返回值里加一格」（那正是拆分文档禁止的）。
    """
    added = dict(_BASELINE_OK)
    added[sv.OBSERVE_KEY_PROBLEM] = "顺手加的观测"
    assert "问题" in _snapshot_diff(added, _BASELINE_OK), "多一个键没被抓到"

    changed = dict(_BASELINE_OK)
    changed["difficulty"] = "初级"
    assert "difficulty" in _snapshot_diff(changed, _BASELINE_OK), "改一个值没被抓到"

    removed = {k: v for k, v in _BASELINE_OK.items() if k != "reason"}
    assert "reason" in _snapshot_diff(removed, _BASELINE_OK), "少一个键没被抓到"

    assert _snapshot_diff(dict(_BASELINE_OK), _BASELINE_OK) == {}, "一致时被误报成不一致"
    print("[OK] 快照比对器对多键／改值／少键都红，一致时不红")


def test_routing_return_never_carries_observation_keys():
    """旁路的键（数以 sv.OBSERVE_KEYS 为准）不许出现在路由返回值里——「不接线」的第二种形状

    用例 4 从调用次数证，本条从返回形状证：将来有人把观测并进 decision，
    这里会先红，而 core/runtime.py 那句 decision["agent"][0] 不会替他报错。
    """
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY):
        got = sv.supervisor_decision("讲讲反向传播")
    for key in sv.OBSERVE_KEYS:
        assert key not in got, f"路由返回值里混进了旁路的键 {key!r}：{got}"
    print("[OK] 路由返回值里没有旁路的任何一个键")


def test_negative_control_wiring_would_be_caught():
    """用例 1／3 的牙齿：造一个「接线版」返回值，两道守卫都必须红

    3a 最可能被「顺手改宽」的形状就是有人把观测 `decision.update(observation)` 并进返回值
    （字面读法本来就是这么说的）。这里**不改生产文件**——那属「接线」，与 2026-10-06 的
    定案冲突，也不该由测试去动主路径代码——而是在测试里复现那个返回值形状。
    若这两条断言有一条不红，对应的生产用例就是恒真的。
    """
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY):
        with contextlib.redirect_stdout(io.StringIO()):
            # 路由那份回复里没有「问题」两格，所以观测走的是降级出口——这正是接线后
            # 每轮都会发生的形状：主路径多花一次调用，换来一整包降级值塞进返回值。
            observation = sv.supervisor_observe("讲讲反向传播")
    wired = dict(_BASELINE_OK)
    wired.update(observation)
    assert _snapshot_diff(wired, _BASELINE_OK), (
        "接线版没被快照比对抓到——用例 1「逐键与改前一致」是恒真的")
    leaked = [key for key in sv.OBSERVE_KEYS if key in wired]
    assert leaked, (
        f"接线版没被「旁路的键不侵入返回值」抓到——用例 3 是恒真的：{leaked}")
    print("[OK] 负控：接线版的返回值同时被两道守卫抓到")


# ---------- 判据③负控：摘掉旁路，路由用例仍全绿 ----------

def test_negative_control_routing_survives_without_bypass():
    """把 supervisor_observe 换成「一被调用就抛」的桩，路由必须照常工作且桩调用次数为 0

    这就是拆分文档要的「把旁路摘掉，路由用例必须仍全绿」。桩用 side_effect=AssertionError
    而不是删除属性：删除会让「有没有被调用」没法计数，而**没被调用**才是这条的全部信息。
    """
    boom = AssertionError("旁路被主路径调用了——它不该在上面")
    with patch.object(sv, "supervisor_observe", side_effect=boom) as stub:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY):
            got = sv.supervisor_decision("讲讲反向传播")
        assert _snapshot_diff(got, _BASELINE_OK) == {}, "摘掉旁路后路由形状就变了，说明它不是旁路"
        with patch.object(sv, "chat_sync_with_retry_budget",
                          side_effect=RuntimeError("模型不可用")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got_fail = sv.supervisor_decision("讲讲反向传播")
        assert _snapshot_diff(got_fail, _BASELINE_FAIL) == {}, "摘掉旁路后回退形状变了"
    assert stub.call_count == 0, (
        f"桩被调用了 {stub.call_count} 次：旁路已经被接进主路径，每轮要多付一次模型调用")
    print("[OK] 负控：旁路换成会抛的桩后路由仍全绿，且该桩一次都没被调用")


# ---------- 旁路自身的三条契约 ----------

def test_observe_valid_reply_has_exactly_four_keys():
    """合法回复：键的集合恰好等于 sv.OBSERVE_KEYS，且第四格「置信度」原样取出

    阶段一（2026-10-11）之前这条叫 has_exactly_three_keys。名字里的数会随键表漂移，
    所以断言本体从一开始就是 `set(got) == set(sv.OBSERVE_KEYS)`（比对键表而不是写死四个
    字符串）——加第四格时**只有措辞和名字**要改，判据不用改，这正是当初这么写的目的。
    """
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_OBSERVE_REPLY):
        got = sv.supervisor_observe("我没看懂反向传播")
    assert set(got) == set(sv.OBSERVE_KEYS), (
        f"旁路输出必须恰好是 OBSERVE_KEYS 那一组键，实得 {sorted(got)}")
    assert got[sv.OBSERVE_KEY_PROBLEM] == "缺少可运行的最小例子"
    assert got[sv.OBSERVE_KEY_NEXT_STEP] == "给一个 20 行的反向传播脚本"
    assert got[sv.OBSERVE_KEY_CAPABILITY] == ["read_write_notes"]
    assert got[sv.OBSERVE_KEY_CONFIDENCE] == 0.6, (
        f"置信度没被原样取出（实得 {got[sv.OBSERVE_KEY_CONFIDENCE]!r}）")
    print("[OK] 合法观测四键齐全且原样取出")


def test_observe_makes_exactly_one_model_call():
    """配额口径：本函数一次调用＝一次模型请求（它没被接线，所以现在每轮还是 0 次）"""
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_OBSERVE_REPLY) as m:
        sv.supervisor_observe("我没看懂反向传播")
    assert m.call_count == 1, f"模型调用次数不是 1（实得 {m.call_count}）——重试逻辑不在本层"
    print("[OK] 一次观测＝一次模型调用")


def test_observe_prompt_lists_the_closed_vocabulary():
    """提示词里必须带上 CAPABILITY_MAP 的全部键，且**不**带 carrier 路径

    不带路径是我在实现注释里写下的承诺（模型照路径「发明」新能力更容易），这里把它钉住；
    带全部键是「闭集」能被遵守的前提——清单没进提示词，过滤就只是在丢合法答案。
    """
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_OBSERVE_REPLY) as m:
        sv.supervisor_observe("随便一句话")
    system_prompt = m.call_args[0][0][0]["content"]
    for key in sv.CAPABILITY_MAP:
        assert key in system_prompt, f"能力键 {key} 没进提示词，闭集形同虚设"
    assert sv.UNKNOWN_CAPABILITY in system_prompt, "提示词没告诉模型：挑不出来时填 unknown"
    # ⚠️ carrier 从单路径改成列表之后（Fix C），`info["carrier"] not in system_prompt`
    # 会变成一句**恒真断言**：整串的列表 repr 永远不是提示词的子串，路径泄进去也照样绿。
    # 恒真断言比没有断言更坏，所以这里逐条摊开验。
    for info in sv.CAPABILITY_MAP.values():
        for carrier in info["carrier"]:
            assert carrier not in system_prompt, (
                f"carrier 路径 {carrier} 泄进了提示词")
    print("[OK] 提示词带全能力键与 unknown，不带 carrier 路径")


# ---------- 3a′：观测载荷（输入从「用户消息」泛化为「一份材料」）----------

# 「合格载荷提示词」的两组条件。写成返回问题清单的函数而不是两条 assert，是因为同一份
# 检查必须跑在**两份提示词**上：生产那份要求问题为空，改前那份要求问题非空。
# 只测前者就是恒真断言（拆分文档：恒真断言比没有断言更坏）。
_OBSERVE_PROMPT_REQUIRES = (
    "观测下面这份材料",   # 设计席给的口径：输入是一份材料，不是一句话
    "State",             # 提示词必须告诉模型：载荷还可能是项目状态摘要
)
# 3a 那版把输入写死成「用户的一句话」，这三处措辞都是那个假设留下的痕迹
_OBSERVE_PROMPT_FORBIDS = (
    "只读用户这句话",
    "这句话里",
    "用户没有说到",
)
# 改前（HEAD d2fff4b 的 supervisor.py）的原措辞，逐字摘自当时的提示词，供负控使用
_OBSERVE_PROMPT_PRE_3A = (
    '你是挂在对话旁边的观测员：不参与选 Agent、不参与回答，只读用户这句话，'
    '输出一个 JSON 对象，格式为：\n'
    '    "问题": "这句话里真正卡住的地方；看不出来就原样写 无法判断",\n'
    '硬规则：\n- 不许编造用户没有说到的困难；说不准就写 无法判断\n'
)


def _prompt_wording_problems(prompt: str) -> list:
    """列出「这份提示词还在把输入当用户消息」的证据；空列表＝泛化到位"""
    problems = []
    for phrase in _OBSERVE_PROMPT_REQUIRES:
        if phrase not in prompt:
            problems.append(f"缺泛化说法：{phrase!r}")
    for phrase in _OBSERVE_PROMPT_FORBIDS:
        if phrase in prompt:
            problems.append(f"残留「用户消息」口径：{phrase!r}")
    return problems


def _state_summary_payload() -> str:
    """把一份**真实采集出来的** State 摊成 3b 会喂进观测的文本

    刻意不手写一段假摘要：手写的形状和 Phase 2 真正产出的内容没有必然关系，那样的
    用例只能在验「我编的字符串能通过」。这里走 core.project_state.build_project_state，
    临时库为空 ⇒ 自动段多半落进 unknowns（判据④的「未知」路径，不抛异常），人工段用
    一条 user_report 顶着——这正是设计席在 3b 判据里要求「人工段不许缺」的那个形状。
    """
    state = ps.build_project_state(manual_items=[
        ("summary", "user_report",
         "笔记检索我没用过，不确定是没配好还是没必要", "用户 2026-10-06 口述"),
    ])
    lines = ["【项目状态摘要 · 自动段（k=v 事实）】"]
    facts = [i for i in state["items"] if i["kind"] == "fact"]
    for item in facts:
        lines.append(f"- {item['decision_field']}={item['content']}")
    if not facts:
        lines.append("- （本次采集没有可用的自动段事实）")
    lines.append("【人工段（用户原话，不是模型生成）】")
    for item in state["items"]:
        if item["kind"] != "fact":
            lines.append(f"- [{item['kind']}] {item['content']}")
    lines.append("【未知（读不出来就明说，不猜）】")
    for unknown in state["unknowns"]:
        lines.append(f"- {unknown['decision_field']}: {unknown['reason']}")
    return "\n".join(lines)


def test_observe_accepts_a_state_summary_payload():
    """3a′ 新增用例：喂一份 State 摘要形态的载荷，仍能产出 OBSERVE_KEYS 那一组键

    这条同时是三样东西的负控落点（设计席原话：把参数名与提示词改回「用户消息」那一版，
    该用例必须红）：
      ① 用关键字 `payload=` 调用 ⇒ 参数名退回 user_message 时 TypeError；
      ② 断言生产提示词的问题清单为空 ⇒ 提示词退回旧措辞时非空、红；
      ③ 断言载荷原样进了 user 轮 ⇒ 若实现把 State 摘要截成第一行（当成一句话读），红。
    """
    payload = _state_summary_payload()
    assert "\n" in payload, "State 摘要载荷必须是多行材料，单行就退回成「一句话」了"
    assert "=" in payload or "未知" in payload, (
        f"载荷里没有机器形态的段，不像一份 State：{payload[:120]!r}")
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_OBSERVE_REPLY) as m:
        got = sv.supervisor_observe(payload=payload)
    assert set(got) == set(sv.OBSERVE_KEYS), (
        f"State 载荷下旁路输出的键集合变了，实得 {sorted(got)}")
    problems = _prompt_wording_problems(m.call_args[0][0][0]["content"])
    assert not problems, f"提示词没完成 3a′ 的泛化：{problems}"
    assert m.call_args[0][0][1]["content"] == payload, (
        "载荷被改动后才发给模型：观测到的就不是那份 State 了")
    print("[OK] State 摘要载荷：键集合齐全、措辞已泛化、载荷原样送达")


def test_negative_control_pre_3a_prompt_wording_is_caught():
    """负控：同一份检查器跑在改前措辞上必须**非空**，否则 REQUIRES/FORBIDS 是装饰"""
    problems = _prompt_wording_problems(_OBSERVE_PROMPT_PRE_3A)
    assert problems, (
        "3a 那版提示词在检查器下竟然没有问题清单——说明检查器恒真，"
        "test_observe_accepts_a_state_summary_payload 的第②条牙齿是假的")
    print(f"[OK] 负控：改前措辞被查出 {len(problems)} 处残留")


def test_negative_control_pre_3a_parameter_name_is_rejected():
    """负控：旧参数名 user_message 必须不再被接受

    在 3a 那一版上 `supervisor_observe(user_message="…")` 是**成功**的，所以这条当时
    必红——它就是「把参数名改回去该用例就红」的那一半证明，不靠改仓库文件来演示。
    """
    rejected = False
    reason = ""
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_OBSERVE_REPLY):
        try:
            sv.supervisor_observe(user_message="讲讲反向传播")
        except TypeError as err:
            rejected = True
            reason = str(err)
    assert rejected, (
        "supervisor_observe(user_message=…) 仍然能跑通：载荷泛化没落到参数名上")
    print(f"[OK] 负控：旧参数名被签名拒绝（TypeError: {reason}）")


def test_model_unavailable_returns_unable_and_is_audible():
    """判据②本体：模型不可用时文字格全是「无法判断」／unknown，且失败要在 stdout 出声

    出声这条不是装饰：本仓库的「安静类缺陷」已经吃过三次（批次 1a 页眉假话、批次 9 静默
    截断、批次 13 空正文），只断言返回值会让「观测永远是无法判断」这种状态没人发现。
    """
    buf = io.StringIO()
    with patch.object(sv, "chat_sync_with_retry_budget",
                      side_effect=RuntimeError("连接被重置")):
        with contextlib.redirect_stdout(buf):
            got = sv.supervisor_observe("讲讲反向传播")
    out = buf.getvalue()
    assert got[sv.OBSERVE_KEY_PROBLEM] == sv.UNABLE_TO_JUDGE, f"编出了问题：{got}"
    assert got[sv.OBSERVE_KEY_NEXT_STEP] == sv.UNABLE_TO_JUDGE, f"编出了下一步：{got}"
    assert got[sv.OBSERVE_KEY_CAPABILITY] == [sv.UNKNOWN_CAPABILITY], f"编出了能力：{got}"
    # 阶段一新增的第四格在降级出口里必须是 None 而不是 0.5：0.5 看着中性，实际是替模型
    # 说了一句它没说过的话，而复盘那边把「没报」与「报了 0.5」读成同一件事，分档就白做。
    assert got[sv.OBSERVE_KEY_CONFIDENCE] is None, (
        f"降级出口替模型填了把握：{got[sv.OBSERVE_KEY_CONFIDENCE]!r}")
    assert "观测" in out, f"旁路失败必须出声（照既有 stdout 断言口径），实得 stdout={out!r}"
    print("[OK] 模型不可用：返回「无法判断」（置信度为 None）且 stdout 有声")


def test_prose_reply_returns_unable_not_a_guess():
    """模型回一段散文（JSON 提取取不到对象）⇒ 同判据②，不许从散文里"读"出一个问题"""
    buf = io.StringIO()
    with patch.object(sv, "chat_sync_with_retry_budget",
                      return_value="抱歉，我需要更多信息才能判断。"):
        with contextlib.redirect_stdout(buf):
            got = sv.supervisor_observe("嗯")
    assert set(got) == set(sv.OBSERVE_KEYS), (
        f"降级出口也必须把键表凑齐：{sorted(got)}")
    assert got[sv.OBSERVE_KEY_PROBLEM] == sv.UNABLE_TO_JUDGE
    print("[OK] 散文回复：走「无法判断」，不从散文里猜")


def test_missing_field_is_not_backfilled():
    """缺一格**不补一个看起来合理的说法**——补全就是判据②要防的编造"""
    cases = [
        '{"问题": "", "建议下一步": "继续", "需要的能力": []}',
        '{"建议下一步": "继续", "需要的能力": ["read_write_notes"]}',
        '{"问题": "缺例子", "需要的能力": ["read_write_notes"]}',
        '{"问题": {"嵌套": "不是字符串"}, "建议下一步": "继续", "需要的能力": []}',
    ]
    for reply in cases:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=reply):
            with contextlib.redirect_stdout(io.StringIO()):
                got = sv.supervisor_observe("x")
        assert got[sv.OBSERVE_KEY_PROBLEM] == sv.UNABLE_TO_JUDGE, (
            f"回复 {reply} 缺字段或被补全了：{got}")
        assert got[sv.OBSERVE_KEY_NEXT_STEP] == sv.UNABLE_TO_JUDGE, (
            f"回复 {reply} 的另一格被补全了：{got}")
    print("[OK] 缺字段／空串／非字符串一律降级，不做补全")


def test_capability_names_are_filtered_to_the_closed_set():
    """「需要的能力」是闭集：造名被丢弃、合法名保留、全造名落 unknown（不许造能力名）"""
    cases = [
        ('{"问题": "a", "建议下一步": "b", "需要的能力": ["read_write_notes", "made_up_power"]}',
         ["read_write_notes"]),
        ('{"问题": "a", "建议下一步": "b", "需要的能力": ["totally_fake"]}',
         [sv.UNKNOWN_CAPABILITY]),
        ('{"问题": "a", "建议下一步": "b", "需要的能力": []}', [sv.UNKNOWN_CAPABILITY]),
        ('{"问题": "a", "建议下一步": "b"}', [sv.UNKNOWN_CAPABILITY]),
        ('{"问题": "a", "建议下一步": "b", "需要的能力": "recall_local_material"}',
         ["recall_local_material"]),
        ('{"问题": "a", "建议下一步": "b", '
         '"需要的能力": ["recall_local_material", "recall_local_material"]}',
         ["recall_local_material"]),
    ]
    for reply, expected in cases:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=reply):
            got = sv.supervisor_observe("x")
        assert got[sv.OBSERVE_KEY_CAPABILITY] == expected, (
            f"回复 {reply} 的能力格实得 {got[sv.OBSERVE_KEY_CAPABILITY]}，应为 {expected}")
    print("[OK] 能力格按闭集过滤：造名丢弃、合法保留、空则 unknown、去重")


def test_unknown_sentinel_is_not_a_real_capability():
    """unknown 是哨兵，不是 CAPABILITY_MAP 的键——否则模型答 unknown 就算「挑到了能力」"""
    assert sv.UNKNOWN_CAPABILITY not in sv.CAPABILITY_MAP, (
        "哨兵混进了能力表，闭集与降级出口会互相污染")
    real_keys = [k for k in sv.CAPABILITY_MAP if k != sv.UNKNOWN_CAPABILITY]
    assert len(real_keys) == len(sv.CAPABILITY_MAP), "能力表里混进了哨兵"
    print("[OK] unknown 不在能力表里，表内键数不变")


def test_observe_never_raises_for_weird_returns():
    """契约①：观测失败不许把调用方拖倒——None／数字／空串／非零异常都得返回完整键表"""
    weird = [None, "", 12345, {"不是": "字符串"}, "[1, 2, 3]"]
    for value in weird:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=value):
            with contextlib.redirect_stdout(io.StringIO()):
                got = sv.supervisor_observe("x")
        assert set(got) == set(sv.OBSERVE_KEYS), f"怪返回值 {value!r} 让形状变了：{got}"
        assert got[sv.OBSERVE_KEY_PROBLEM] == sv.UNABLE_TO_JUDGE, (
            f"怪返回值 {value!r} 没有降级成「无法判断」：{got}")
    print("[OK] 五种怪返回值都不抛异常、都走降级")


# ---------- 阶段一（2026-10-11）：第四格「置信度」的清洗 ----------

def test_confidence_is_clamped_or_none_never_fabricated():
    """置信度：越界钳到端点、字符串数收下、转不动与缺席一律 None——**不补中性 0.5**

    六类输入各管一件事：
    - 1.7／-2：模型给的数越界，钳到 1.0／0.0。尺用的是 `core.ai_filter._clamp01`
      （设计席阶段一规格里点名要用的那把），本模块不另造一把，避免两处口径漂移；
    - "0.7"：模型把数写成字符串是合法的，转得动就收；
    - "abc"／true／null／整格缺席：都算「没报」。布尔值单独列出来是因为
      `_clamp01(True)` 会给出 1.0，那是替模型说它有十足的把握。
    后四类用的回复里「问题」「建议下一步」都写全，所以**必须顺手断言前两格仍然有效**：
    置信度这一格转不动不许把整份观测拖进降级出口——那会让复盘把「模型没报把握」误读成
    「这轮判不了」，而这两件事在分档里走的是不同的说法。
    """
    clamped = [
        ('{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": 1.7}', 1.0),
        ('{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": -2}', 0.0),
        ('{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": "0.7"}', 0.7),
    ]
    for reply, expected in clamped:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=reply):
            got = sv.supervisor_observe("x")
        assert got[sv.OBSERVE_KEY_CONFIDENCE] == expected, (
            f"回复 {reply} 的置信度实得 {got[sv.OBSERVE_KEY_CONFIDENCE]!r}，应为 {expected}")

    silent = [
        '{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": "abc"}',
        '{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": true}',
        '{"问题": "a", "建议下一步": "b", "需要的能力": [], "置信度": null}',
        '{"问题": "a", "建议下一步": "b", "需要的能力": []}',
    ]
    for reply in silent:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=reply):
            got = sv.supervisor_observe("x")
        assert got[sv.OBSERVE_KEY_CONFIDENCE] is None, (
            f"回复 {reply} 里模型没报的把握被补成了 {got[sv.OBSERVE_KEY_CONFIDENCE]!r}")
        assert got[sv.OBSERVE_KEY_PROBLEM] == "a", (
            f"置信度转不动却把前两格一起拖下了降级出口：{got}")
    print("[OK] 置信度钳到端点、收下字符串数、没报一律 None 且不拖累前两格")


# ---------- 阶段一（2026-10-11）：第二条旁路 supervisor_ask_questions ----------

def test_ask_bypass_caps_at_the_limit_and_cleans_the_list():
    """契约③：一次最多 `ASK_QUESTION_LIMIT` 条，且只收非空字符串、去重

    封顶发生在代码里而不是提示词里：提示词那句「最多三条」挡不住模型给五条，
    五条一起进 Human View 就成了一页问卷，而那三句是给人**回答**的。
    """
    # 恰好等于上限的一份：一条都不许丢（封顶写成「切掉最后一条」就成丢答案了）
    with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ASK_REPLY):
        got = sv.supervisor_ask_questions("这份材料不够")
    assert got[sv.ASK_KEY_QUESTIONS] == [
        "这周想推进到哪一步？", "手上已经有什么材料？", "每周能给出多少时间？"], (
        f"恰好三条时被削掉了：{got}")

    with patch.object(sv, "chat_sync_with_retry_budget",
                      return_value='{"补充问题": ["一", "二", "三", "四", "五"]}') as m:
        got = sv.supervisor_ask_questions("这份材料不够")
    assert got[sv.ASK_KEY_QUESTIONS] == ["一", "二", "三"], f"封顶没生效：{got}"
    assert sv.ASK_QUESTION_LIMIT == 3, (
        f"设计席阶段一规格定的是「三个具体问题」，上限常量现在是 {sv.ASK_QUESTION_LIMIT}")
    assert m.call_count == 1, f"一次问句生成应当只打一次模型（实得 {m.call_count}）"
    assert m.call_args[0][0][1]["content"] == "这份材料不够", (
        "载荷被改动后才发给模型：生成的问句就不是对着这份材料问的")

    with patch.object(sv, "chat_sync_with_retry_budget",
                      return_value='{"补充问题": ["  ", "甲", " 甲 ", "甲", 7, null]}'):
        got = sv.supervisor_ask_questions("x")
    assert got[sv.ASK_KEY_QUESTIONS] == ["甲"], f"空白／重复／非字符串没被清掉：{got}"
    print("[OK] 问句旁路：五条封顶成上限条数，空白／重复／非字符串都进不来")


def test_ask_bypass_returns_empty_not_a_generic_question():
    """契约①②：失败与「答了空」都返回空列表，**绝不编一条通用问句凑数**

    空列表有两条合法来源（模型答了空数组／答的那一格根本不是列表）与一条失败来源
    （打不通、回了散文）。三者的返回值都是 `{补充问题: []}`，区别只在**有没有在
    stdout 出声**：合法的空不必出声，失败的沉默才是本仓库吃过三次的那类缺陷。
    调用方（`core/long_term_project_review._record_request`）看到空就走内置兜底问，
    并在 source／reason 里如实写明那三句是内置的、不是这里生成的——所以这里必须
    真的给空，给一条编出来的问句就会让那句「如实」变成假话。
    """
    for label, kwargs in [
        ("模型不可用", {"side_effect": RuntimeError("连接被重置")}),
        ("散文回复", {"return_value": "我需要更多信息才问得出问题。"}),
    ]:
        with patch.object(sv, "chat_sync_with_retry_budget", **kwargs):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = sv.supervisor_ask_questions("x")
        assert got[sv.ASK_KEY_QUESTIONS] == [], f"{label} 时没落到空列表：{got}"
        assert "问句" in buf.getvalue(), (
            f"{label} 时没在 stdout 出声（照既有出声口径），实得 {buf.getvalue()!r}")

    for label, reply in [
        ("空数组", '{"补充问题": []}'),
        ("键缺失", '{"别的东西": 1}'),
        ("整格是对象", '{"补充问题": {"嵌套": "不是列表"}}'),
    ]:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=reply):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = sv.supervisor_ask_questions("x")
        assert set(got) == {sv.ASK_KEY_QUESTIONS}, f"{label} 时返回形状变了：{sorted(got)}"
        assert got[sv.ASK_KEY_QUESTIONS] == [], f"{label} 时被编出了问句：{got}"
        assert buf.getvalue() == "", (
            f"{label} 是合法答案却出了声，失败与正常就分不开了：{buf.getvalue()!r}")
    print("[OK] 问句旁路：失败有声地给空列表，合法的空安静地给空列表，都不编句子")


def test_negative_control_ask_bypass_not_wired():
    """判据③的负控（第二条旁路版）：摘掉问句旁路，路由与观测必须照常，且桩零调用

    做法与 `test_negative_control_routing_survives_without_bypass` 一致：桩一被调用就抛，
    再断调用次数为 0——后半句才是「立函数不接线」的全部信息（2026-10-11 的定案没变，
    它的消费者在 `agents/task_executor.py` 的复盘 handler 里由调用方注入，
    本模块不主动调用它）。少了这条，「每轮对话不多花一次模型调用」就只对被摘掉的那条
    旁路成立，新加的这条没人管。
    """
    boom = AssertionError("问句旁路被调用了——它不该在主路径上")
    with patch.object(sv, "supervisor_ask_questions", side_effect=boom) as stub:
        with patch.object(sv, "chat_sync_with_retry_budget", return_value=_ROUTING_REPLY):
            got = sv.supervisor_decision("讲讲反向传播")
            observed = sv.supervisor_observe("讲讲反向传播")
        assert _snapshot_diff(got, _BASELINE_OK) == {}, "摘掉问句旁路后路由形状就变了"
        assert set(observed) == set(sv.OBSERVE_KEYS), "摘掉问句旁路后观测的形状变了"
    assert stub.call_count == 0, (
        f"桩被调用 {stub.call_count} 次：问句旁路被接进了每轮对话，每轮多付一次模型调用")
    print("[OK] 负控：问句旁路换成会抛的桩后路由与观测照常，且该桩一次都没被调用")


TESTS = [
    test_routing_snapshot_matches_pre_change_baseline,
    test_snapshot_comparator_has_teeth,
    test_routing_return_never_carries_observation_keys,
    test_negative_control_wiring_would_be_caught,
    test_negative_control_routing_survives_without_bypass,
    test_observe_valid_reply_has_exactly_four_keys,
    test_observe_makes_exactly_one_model_call,
    test_observe_prompt_lists_the_closed_vocabulary,
    test_observe_accepts_a_state_summary_payload,
    test_negative_control_pre_3a_prompt_wording_is_caught,
    test_negative_control_pre_3a_parameter_name_is_rejected,
    test_model_unavailable_returns_unable_and_is_audible,
    test_prose_reply_returns_unable_not_a_guess,
    test_missing_field_is_not_backfilled,
    test_capability_names_are_filtered_to_the_closed_set,
    test_unknown_sentinel_is_not_a_real_capability,
    test_observe_never_raises_for_weird_returns,
    test_confidence_is_clamped_or_none_never_fabricated,
    test_ask_bypass_caps_at_the_limit_and_cleans_the_list,
    test_ask_bypass_returns_empty_not_a_generic_question,
    test_negative_control_ask_bypass_not_wired,
]


def main():
    try:
        for fn in TESTS:
            fn()
    finally:
        cleanup_test_db()


if __name__ == "__main__":
    main()
