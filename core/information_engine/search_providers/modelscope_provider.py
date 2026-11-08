"""
魔搭（ModelScope）竞赛/活动搜索源 Provider（一手、匿名、结构化）

设计动机（2026-09-13）：现有两个搜索源都是"搜索引擎转载"——ddgs 返回的是
第三方站点对机会的二手描述，GitHub 只覆盖代码仓库。用户的真实资源史里唯一
拿到过资源的就是魔搭，而它是本项目唯一能直连的一手机会目录：官方列表接口
匿名可读、字段结构化（含报名截止时间），且不需要任何 cookie / token。

接口（实测核实，2026-09-13）：
- 列表：GET https://www.modelscope.cn/api/v1/competitions?PageNumber=<n>&PageSize=50
  → {"Code":200,"Data":{"Races":[...],"TotalCount":142},"Success":true}，
  按 PageNumber 翻页至累计达 TotalCount（当前需 3 页）。
- 详情：GET https://www.modelscope.cn/api/v1/competitions/<Id>/detail（同一对象结构，
  本 Provider 只用列表接口，详情接口暂未接入）(2026-09-13)。
- 请求头只需非空 User-Agent；**匿名、不携带任何 cookie**。
- Content 字段是**字符串形式的**具名分段数组（`json.loads` 后形如
  `[{"name": …, "content": <富文本树>}]`，不是单棵树），见 P6-4；
- 时间戳只有 `RegistrationDeadline`（**报名截止**）；`EndTime` 实测全为 0，
  即接口**没有**给出"作品提交截止"——0 不等于"无提交截止"，见 P6-5b。

source_url 选择（关键）：统一用 https://www.modelscope.cn/events/<Id>
（实测直接 200、slug 任意、无重定向）。opportunities.source_url 是 UNIQUE +
INSERT OR IGNORE（resource_scout.py:937），URL 必须逐字符稳定才能跨轮幂等；
Id 是稳定主键，而 Title 会改名、列表顺序会变，都不可用作 URL。

三条与基类约定的有意偏离（**本接口是内部接口，无兼容承诺**）：
1. **一轮扫描只拉一次网络。** ResourceScout.scan_once 是
   `for query in self.queries: for provider in self.providers:`（默认 6 条查询），
   而本源返回与 query 无关——不加保护会被调 6 次。故用实例级 `self._served`
   标志：首次返回全量，之后返回 []。ResourceScout.__init__ 每轮新建 provider
   实例，所以"实例标志"恰好就是"轮级标志"，无需改动 scan_once 的循环结构。
   **失败语义：无论成功还是失败，`self._served` 一律置位**（失败也返回 [] 并打
   日志）。这意味着单轮内网络失败不会自动重试；用户要重试需重新触发扫描
   （ResourceScout 每轮重建，重新扫描即可重试）。
2. **忽略 limit 参数。** 本源语义是"返回所有报名未截止的条目"，被
   limit_per_query（默认 5）截断会让这个最有价值的源只贡献 5 条。这是有意
   偏离基类约定：search(query, limit) 的 limit 在此被忽略。
3. **失败静默降级。** 网络异常 / 超时 / Success != true / 无 Data.Races
   → 一律 return [] 并打印一行原因（base.py 明文要求单源失败不拖垮任务）。
"""

import json
import re
import time
from datetime import datetime
from typing import List, Optional

import requests

from .base import SearchProvider
from ..models import (
    Evidence,
    now_str,
    weekday_index_of_timestamp,
    weekday_label,
    WEEKEND_INDEXES,
)

#: 从列表行**原样**带回 `Evidence.entities` 的字段（P6-1②，2026-09-13）
#:
#: 动机：这些字段此前被整批丢弃，于是"报名截止"成了界面上唯一的时间轴，
#: 而报名与作品提交是**两根轴**（实测同一活动：接口只给报名截止 09-14 12:00，
#: 作品提交截止 09-14 22:00 只存在于官方落地页）。丢字段的直接代价是
#: 显示层只能把两个语义不同的时间压成一个。
#: `EndTime` 值为 0 表示**接口未提供**（实测全部为 0），保留 0 而不是丢弃——
#: "未抓到"和"不存在"是两句不同的话（见 P6-5b②）。
ENTITY_RAW_FIELDS = (
    "EndTime",            # 作品提交截止（unix 秒；0 = 接口未提供）
    "RegistrationType",   # 报名方式（个人 / 团队）
    "MemberLimit",        # 队伍人数上限
    "PlayerNum",          # 当前参与人数
    "BonusDesc",          # 奖励说明
    "SignUpUrl",          # 官方报名入口（落地页两跳的触发依据，见 P6-5）
    "SubmitUrl",          # 官方提交入口
)

#: 报名截止的来源标签（P6-3）：两个值都要留在数据面里，不许择一消解
DEADLINE_SOURCE_INTERFACE = "接口"
DEADLINE_SOURCE_LANDING = "落地页"

#: 列表行 Content 解析后的两种形态里，"具名分段数组"用来判别的键（P6-4②）
#: 实测：`[{"name": …, "content": <富文本树>}, …]` 是**分段数组**，不是单棵树。
#: 按单棵树走平会得到 0 字，并曾因此得出"列表行没有正文"的错误结论。
CONTENT_SEGMENT_KEY = "content"

# ---------- P6-5 落地页两跳（本批唯一新增外呼） ----------

#: 触发两跳的正文厚度阈值（中文字数，**严格小于**才走）
#: 2026-09-13 23:55 实测计数（不是推测）：未截止 12 行里 SignUpUrl 命中
#: `/active/` 的**只有 1 行**——这正是条件收窄到"薄正文 + /active/ 入口"的依据：
#: 单看正文厚度会命中 4 行，单看 SignUpUrl 会命中 1 行，两者同时满足才是那 1 行。
LANDING_THIN_BODY_CHARS = 400

#: SignUpUrl 必须命中这个形状才走两跳（`https://modelscope.cn/active/<slug>`）
LANDING_SIGNUP_MARK = "modelscope.cn/active/"

#: 落地页两跳的超时（秒）。**不与列表接口的 20s 假设复用**（P6-5④）：
#: - 第二跳 `mseo-ai-inf.ms.show` 是**另一个域名**（Next.js SSR，实测 55,077 字节），
#:   故障面与魔搭接口无关，共用一个超时假设等于把两次独立故障绑在一次判定上；
#: - 更硬的上界来自调用方：`ResourceScout._call_search` 用
#:   `future.result(timeout=self.timeout_sec)`（默认 **15s**）包住**整个
#:   provider.search()**。两跳一旦把这 15s 拖爆，代价不是"少一个落地页"，而是
#:   **这一轮已拉到的十几行全部丢掉**。所以 4 + 6 = 10s，必须留出余量。
LANDING_HOP_TIMEOUT = 4.0
LANDING_IFRAME_TIMEOUT = 6.0

#: 每轮最多做几次两跳。取 1 的依据是实测计数（命中 /active/ 的只有 1 行），
#: 同时把最坏耗时钉死在 4+6=10s 以内——一个无上限的外呼循环会随数据漂移
#: 悄悄吃掉上面那 15s 的预算。
LANDING_MAX_FETCHES = 1

#: 从赛程 JSON 的 `timeline_label` 字段值里识别两根轴（读的是**字段值**，
#: 不是拿正则去扫正文中文）
LANDING_LABEL_REGISTRATION = "报名"
LANDING_LABEL_SUBMIT = "提交"

#: 赛程日期实测形状：`08.19 – 09.14 12:00` / `09.19`（取区间**结束**端）
_LANDING_DATE_RE = re.compile(
    r"(\d{2})\.(\d{2})(?:\s+(\d{2}):(\d{2}))?\s*$")


def resolve_deadline(candidates) -> dict:
    """多个来源给出的报名截止：取**较晚**的一个，并如实报告冲突（纯函数，P6-3）

    动机（不对称代价）：多验证一次的成本是秒级，判死一次的成本是机会**永久消失**。
    2026-09-13 的真实样本：接口写 09-13 23:59 且 Status=1，官方手册写 09-13 00:00
    通道关闭；设计侧选了悲观值并**整节归档**，用户因此错过报名。
    **归档是比选错值更大的错**——选错值最多多问一次，归档连问的机会都没有。

    因此本函数的输出是三件事，而不是一个数：
    ① `sources` 保留**每个来源的原值**（含给不出值的来源，值为 None）；
    ② `conflict` 在有两来源给出**不同**值时置真（逐秒比较：两个来源都是精确时间戳，
       按"同一天就算一致"会把这批样本里的 23:59 vs 00:00 当成一致）；
    ③ `value` 取其中较晚的一个，供开放判断使用。

    Args:
        candidates: `[(来源标签, unix 秒或 None), ...]`，顺序即展示顺序

    Returns:
        {"value": float|None, "conflict": bool, "sources": {来源标签: 原值}}
    """
    sources = {}
    values = []
    for label, raw in candidates:
        sources[label] = raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            values.append(float(raw))
    distinct = sorted(set(values))
    return {
        "value": max(values) if values else None,
        "conflict": len(distinct) > 1,
        "sources": sources,
    }


class ModelScopeProvider(SearchProvider):
    """魔搭竞赛/活动适配器（一手结构化接口，匿名只读）

    Args:
        timeout: 单次 HTTP 请求超时秒数
        page_size: 每页条数（接口实测支持 50）
        max_pages: 翻页硬上限（防御 TotalCount 异常导致无限翻页）
        now_ts: 报名截止判断用的"当前时间"（unix 秒）；仅测试注入，默认实时
    """

    name = "ModelScope 竞赛/活动"

    LIST_API = "https://www.modelscope.cn/api/v1/competitions"
    DETAIL_API = "https://www.modelscope.cn/api/v1/competitions/{id}/detail"
    EVENT_URL = "https://www.modelscope.cn/events/{id}"

    def __init__(self, timeout: float = 20.0, page_size: int = 50,
                 max_pages: int = 5, now_ts: Optional[float] = None):
        self.timeout = float(timeout or 20.0)
        self.page_size = max(1, int(page_size))
        self.max_pages = max(1, int(max_pages))
        self._now_ts = now_ts
        # 轮级"已服务"标志（见模块 docstring 偏离 1）：成功失败都置位
        self._served = False

    # ---------- 纯函数（便于测试，不打网络） ----------

    @staticmethod
    def _effective_registration_deadline(race: dict,
                                         alt_deadline=None) -> Optional[float]:
        """各来源里**较晚**的报名截止（unix 秒）；都拿不出合法值时返回 None

        合法性口径与 `_is_open` 一致：正数才算给了值，0 / 缺失 / 非数值都算
        "没说"。注意 0 与缺失**不参与** max ——否则 0 会把另一个来源的真值压掉。
        """
        values = []
        for raw in (race.get("RegistrationDeadline"), alt_deadline):
            if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                    and raw > 0):
                values.append(float(raw))
        return max(values) if values else None

    @staticmethod
    def _is_open(race: dict, now_ts: float, alt_deadline=None) -> bool:
        """报名是否未截止（纯函数）

        判据是**较晚的那个**报名截止 > now_ts（P6-3③），**不用 Status 判断是否
        开放**：实测未截止的 12 条都是 Status=1，但也见过 Status=9 且截止日已过，
        该枚举语义未经核实，拿它当开关是猜测。

        Args:
            race: 列表接口的一条 race dict
            now_ts: 当前时间（unix 秒）
            alt_deadline: 第二个来源给出的报名截止（unix 秒，可为 None）。
                **取较晚者**的依据是不对称代价：判死一次机会永久消失，多验证
                一次只花几秒；两个来源打架时，"还在窗口内"的那一侧代价更低。
        """
        dl = ModelScopeProvider._effective_registration_deadline(
            race, alt_deadline)
        if dl is None:
            return True   # 两个来源都没给出截止时间 → 保守保留
        return dl > float(now_ts)

    def _race_to_evidence(self, race: dict,
                          landing=None) -> Optional[Evidence]:
        """把一条 race 映射为 Evidence；缺 Id 或 Title 则返回 None（不可点击无意义）

        Args:
            race: 列表接口的一条 race dict
            landing: P6-5 两跳抓取的解析结果（可为 None）。它只为报名截止提供
                **第二个来源**，拿不到不影响本行入库（失败必须等于没这个功能）。
        """
        rid = race.get("Id")
        title = (race.get("Title") or "").strip()
        if rid is None or not title:
            return None
        try:
            url = self.EVENT_URL.format(id=int(rid))
        except (TypeError, ValueError):
            return None
        brief = (race.get("Brief") or "").strip()
        # Category 原样写进 notes（供 UI 展示，**不参与资格判断**——
        # 资格判断只看 title + summary，避免类目词污染关键词轴）
        entities = self._entities_from_race(race, landing)
        # published_at 落的是**消解后**的报名截止（较晚者），与 _is_open 同一来源，
        # 否则会出现"判它开放、显示另一个日期"这种自相矛盾的组合
        published = self._deadline_to_str(entities.get("deadline_chosen"))
        return Evidence(
            source_url=url,
            source_name=self.name,
            title=title,
            snippet=brief[:500],
            confidence=0.9,   # 一手接口，高于搜索引擎转载（ddgs 0.5 / GitHub 0.6）
            published_at=published,
            retrieved_at=now_str(),
            entities=json.dumps(entities, ensure_ascii=False),
        )

    @staticmethod
    def _entities_from_race(race: dict, landing=None) -> dict:
        """构造一条 race 的 `Evidence.entities`（纯函数，零 schema 变更）

        `Evidence.entities` 是 V0.2+ 的既有 TEXT 列（见
        `core/information_engine/models.py`），本 Provider 借它承载一手接口的
        **结构化原值**：补回 `ENTITY_RAW_FIELDS` 七个字段，并新增派生标记
        `deadline_is_weekday` / `deadline_weekday`（P6-1③）。

        为什么要派生"是不是工作日"：用户的机器**只有周末开机**，而列表里
        "还剩 3 天"这种相对口径会系统性地骗他——3 天后可能正落在周二上午。
        实测（2026-09-13 23:31）未截止的 12 条里有 10 条报名截止落在周一~周五。
        换算用**本地时区**（`weekday_index_of_timestamp`），并且只做**标记**、
        不做判定：周中只是"当天要确认有没有电脑"，不是"不能参加"（见 P6-2）。

        字段缺失策略（与 `_is_open` 的保守取向一致）：无 `RegistrationDeadline`
        或值非法时，两个派生标记为 `None` 而不是 `False`——"不知道"不能写成
        "不是工作日"，否则显示层会把未知当成已知。

        来源冲突（P6-3）：报名截止可以有两个来源（列表接口 / 官方落地页），
        此处**两个值都保留**在 `deadline_sources` 里，另打 `deadline_conflict`
        标记并记下 `deadline_chosen`（较晚者）。子项明确不许择一消解——选错值
        最多多问一次，而"整节归档"连问的机会都没有。

        Args:
            race: 列表接口的一条 race dict
            landing: P6-5 两跳抓取的解析结果（可为 None）；只用其中的
                `registration_deadline_ts` 作为第二个来源

        Returns:
            可直接 `json.dumps` 的 dict（键值均为 JSON 原生类型）
        """
        entities = {"Category": (race.get("Category") or "").strip()}
        for key in ENTITY_RAW_FIELDS:
            value = race.get(key)
            # None / 空串 = 接口没给这个字段；但 0 要**保留**（EndTime=0 是
            # 接口"未提供提交截止"的明确写法，不能与"字段不存在"混为一谈）
            if value is not None and value != "":
                entities[key] = value
        alt = (landing or {}).get("registration_deadline_ts")
        entities["RegistrationDeadline"] = race.get("RegistrationDeadline")
        resolved = resolve_deadline([
            (DEADLINE_SOURCE_INTERFACE, race.get("RegistrationDeadline")),
            (DEADLINE_SOURCE_LANDING, alt),
        ])
        entities["deadline_chosen"] = resolved["value"]
        entities["deadline_conflict"] = resolved["conflict"]
        entities["deadline_sources"] = resolved["sources"]
        idx = weekday_index_of_timestamp(resolved["value"])
        # 正文中文字数（P6-4）：它既是"这一行正文有多厚"的事实，也是 P6-5 判断
        # 要不要走两跳的唯一依据；解析失败时为 0（0 就是"没取到正文"）。
        entities["ContentChars"] = ModelScopeProvider._chinese_char_count(
            ModelScopeProvider._extract_body_text(race))
        # P6-5③：两跳结果写进 entities（零 schema）。抓不到时**如实记 None**，
        # 不静默填空——"未抓到"与"不存在"是两句不同的话（见 P6-5b②）。
        entities["LandingPageUrl"] = (landing or {}).get("landing_page_url")
        entities["LandingFetchedAt"] = (landing or {}).get("landing_fetched_at")
        entities["LandingStages"] = (landing or {}).get("landing_stages") or []
        entities["SubmitDeadlineTs"] = (landing or {}).get("submit_deadline_ts")
        if idx is None:
            entities["deadline_is_weekday"] = None
            entities["deadline_weekday"] = None
        else:
            entities["deadline_is_weekday"] = idx not in WEEKEND_INDEXES
            entities["deadline_weekday"] = weekday_label(idx)
        return entities

    @staticmethod
    def _deadline_to_str(value) -> Optional[str]:
        """报名截止 unix 秒 → 'YYYY-MM-DD'（仅作展示；非法值返回 None）"""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if value <= 0:
            return None
        try:
            return time.strftime("%Y-%m-%d", time.localtime(float(value)))
        except (ValueError, OSError, OverflowError):
            return None

    # ---------- P6-4 正文：列表行是主路径，detail 只是可选补充 ----------

    @staticmethod
    def _flatten_rich_node(node, out: list) -> None:
        """把一棵富文本树的**文本叶子**收进 out（就地追加）

        实测节点形状（2026-09-13，magic 列表接口的 Content）：
            ["root", {}, ["p", {"jc": "center"}, ["span", {"data-type": "text"},
                                              ["span", {"data-type": "leaf"}, "正文"]]]]
        约定：列表节点是 `[标签名, 属性 dict?, 子节点…]`，**前两个位置是标签与
        属性、不是文本**；只有裸字符串才是文本。若不跳过这两个位置，标签名
        （root / p / span / text / leaf）会被当成正文混进来。
        """
        if isinstance(node, str):
            out.append(node)
            return
        if not isinstance(node, list):
            return
        start = 0
        if node and isinstance(node[0], str) and len(node) >= 2 and \
                isinstance(node[1], dict):
            start = 2
        elif node and isinstance(node[0], str) and len(node) >= 2:
            start = 1
        for child in node[start:]:
            ModelScopeProvider._flatten_rich_node(child, out)

    @staticmethod
    def _content_to_text(content) -> str:
        """列表行 Content → 纯文本（纯函数，P6-4②）

        **Content 是字符串**（实测：`type(Content) is str`，需要 `json.loads`），
        解析后是**具名分段数组** `[{"name": …, "content": <树>}, …]`，**不是单棵树**。
        上一版按"单棵树"走平，于是 0 字，并因此得出"列表行没有正文"的错误结论——
        所以这里**必须先判首元素是否含 `content` 键**，两种形态都要认：
        - 首元素是含 `content` 键的 dict → 具名分段数组，逐段取 `content`；
        - 否则 → 单棵富文本树，整体走平。

        任何解析失败返回空串（正文是附加价值，不该让一行入库失败）。
        """
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                return ""
        else:
            parsed = content          # 容忍已解析对象（未来的接口变更 / 测试）
        if not isinstance(parsed, list) or not parsed:
            return ""
        first = parsed[0]
        if isinstance(first, dict) and CONTENT_SEGMENT_KEY in first:
            trees = [seg.get(CONTENT_SEGMENT_KEY) for seg in parsed
                     if isinstance(seg, dict) and CONTENT_SEGMENT_KEY in seg]
        else:
            trees = [parsed]
        chunks = []
        for tree in trees:
            out: list = []
            ModelScopeProvider._flatten_rich_node(tree, out)
            chunks.append("".join(out))
        return "\n".join(c for c in chunks if c)

    @staticmethod
    def _chinese_char_count(text) -> int:
        """中文字符数（P6-5 的触发阈值用它，与"正文长度"这套口径一致）"""
        return sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")

    @staticmethod
    def _detail_body_or_none(payload) -> Optional[str]:
        """detail 响应 → 正文文本；判定失败一律返回 None（纯函数，P6-4③④）

        **成功判据是 `Data.Race` 存在且非空，不能只看 `Success` 字段。**
        实测两种失败形态（各有一条不打网络的用例）：
        - **假成功**：`{"Code":200,"Message":"success","Success":true}` 但 `Data`
          里**没有 `Race`**。这是最阴的失败——它看起来像成功；
        - **显式失败码**：`Code=10011701001`「获取竞赛失败」。

        detail 在本批是**可选补充**，不是"拿到正文"的必要条件，也**不是跳过一行
        的理由**（正文的主路径是列表行 Content，见 `_extract_body_text`）。
        Race 存在但正文为空 → 返回空串（这是"拿到了对象、它没有正文"，与
        "这次调用失败了"是两件事，调用方不要混用）。
        """
        if not isinstance(payload, dict):
            return None
        data = payload.get("Data")
        if not isinstance(data, dict):
            return None
        race = data.get("Race")
        if not isinstance(race, dict) or not race:
            return None
        return ModelScopeProvider._content_to_text(race.get("Content"))

    @staticmethod
    def _extract_body_text(race: dict) -> str:
        """**正文一律从列表行 Content 取**（纯函数，P6-4①）

        实测依据（2026-09-13 23:55 审计，只用列表接口、零 detail 调用）：
        列表行自带的 Content 就是全文，未截止的 12 行全部取到正文，中文字数
        分布为 8 行 ≥747 字（最高 10689）、仅 4 行 <400 字；而 detail 接口对多行
        返回"假成功"（Success=true 但 Data 里没有 Race）。
        """
        return ModelScopeProvider._content_to_text(race.get("Content"))

    @staticmethod
    def _fetch_detail_body(race_id):
        """可选补充：按 Id 拉 detail 并取其正文；任何失败返回 None

        **本批刻意不接线**（2026-09-13）：本批唯一新增外呼是 P6-5 的落地页两跳，
        detail 在这里只是"判据 + 抓取"两件可独立验证的事——判据已由
        `_detail_body_or_none` 承载并有两条失败形态用例。将来要接线时的硬约束：
        必须先看 `Data.Race` 是否存在且非空，只看 `Success` 会把"假成功"当成
        "这一行没有正文"，进而把它当成跳过这一行的理由。
        """
        try:
            resp = requests.get(
                self.DETAIL_API.format(id=int(race_id)),
                headers={"User-Agent": "personal-agent-resource-scout/0.1"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            return self._detail_body_or_none(resp.json())
        except Exception as e:      # 可选补充：任何失败都等于"没这个功能"
            print(f"[Search] ModelScope detail 补充抓取失败（忽略）: {e}")
            return None

    # ---------- P6-5 落地页两跳：触发条件 / 解析（纯函数，便于离线测试） ----------

    @staticmethod
    def _landing_signup_url(race: dict) -> str:
        """列表行的 SignUpUrl 是否命中两跳触发条件；命中返回该 URL，否则空串

        实测（2026-09-13 22:27，匿名、无 Cookie、curl 即可）：第一跳 GET 该 URL
        返回 1,377 字节 HTML，原文含 iframe 地址 `https://mseo-ai-inf.ms.show/`。
        """
        url = (race.get("SignUpUrl") or "").strip()
        if not url or LANDING_SIGNUP_MARK not in url:
            return ""
        # `/active/<slug>`：mark 之后必须还有非空 slug，避免命中光秃秃的目录路径
        slug = url.split(LANDING_SIGNUP_MARK, 1)[1].split("?", 1)[0].strip("/")
        return url if slug else ""

    @staticmethod
    def _landing_triggered(race: dict) -> bool:
        """两跳触发条件（P6-5①，纯函数）

        **正文中文字数 < 400 且 SignUpUrl 命中 modelscope.cn/active/<slug>**。
        刻意不覆盖另一类薄正文行（境外第三方站、零 Content）——它们不满足入口
        条件，本批**不给它们扩抓取通道**（扩抓取面是无底洞且方向不对）。
        """
        if ModelScopeProvider._landing_signup_url(race) == "":
            return False
        return (ModelScopeProvider._chinese_char_count(
            ModelScopeProvider._extract_body_text(race))
            < LANDING_THIN_BODY_CHARS)

    @staticmethod
    def _landing_iframe_url(html) -> str:
        """第一跳 HTML → iframe 地址（纯函数）

        先找 `<iframe src=…>`；找不到再退而找任意 `ms.show` 字面 URL。
        这里的正则只为**取一个 URL**，不是"扫中文"——两跳的解析对象是结构化
        JSON 字段，见 `_parse_landing_schedule`。
        """
        text = html or ""
        m = re.search(r"""<iframe[^>]*\bsrc\s*=\s*["']([^"']+)["']""",
                      text, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
        m2 = re.search(r"""https://[A-Za-z0-9.\-]*ms\.show[^"'\s<>]*""", text)
        return m2.group(0) if m2 else ""

    @staticmethod
    def _extract_flight_payload(html) -> str:
        """第二跳 HTML → 拼接后的 Next.js Flight 载荷文本（纯函数）

        实测：数据在 `self.__next_f.push([1,"…"])` 里，每段是一个 **JSON 字符串
        字面量**（含 `\\"`、`\\n` 等转义），必须 `json.loads` 解转义后再拼接，
        否则取到的是带转义的原文，`json.loads` 那一步会失败。
        """
        chunks = re.findall(
            r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\s*\]\)',
            html or "")
        out = []
        for chunk in chunks:
            try:
                out.append(json.loads(chunk))
            except (ValueError, TypeError):
                continue
        return "".join(out)

    @staticmethod
    def _extract_json_object(text, key) -> Optional[dict]:
        """按**花括号配对**取出 `"key": {…}` 这个对象并 `json.loads`（纯函数）

        为什么不"找子串再截一段"：Flight 载荷里 `schedule` 与 hero / rules /
        awards / criteria 是平级兄弟，靠长度或结束标记猜边界必然出错；花括号配对
        （跳过字符串内部的括号与转义）是唯一不用猜的读法。
        """
        if not text or not key:
            return None
        pos = text.find('"%s"' % key)
        if pos < 0:
            return None
        start = text.find("{", pos)
        if start < 0:
            return None
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    if text[i] == "\\":
                        i += 1
                    i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except (ValueError, TypeError):
                        return None
                    return obj if isinstance(obj, dict) else None
            i += 1
        return None

    @staticmethod
    def _parse_landing_date(value, year) -> Optional[float]:
        """赛程日期 `09.14 12:00` / `09.19` → 本地时区 unix 秒（纯函数）

        只认实测到的 `MM.DD[ HH:MM]` 形状，其余返回 None（不猜）。年份由调用方
        给出（优先取接口报名截止的年份：同一场活动，两者同年）；
        **已知边界**：跨年的赛程（如接口在 2027-01、页面写 12.20–01.05）会取到
        错误的年份，届时返回的日期会明显偏移——本批选择如实暴露这种偏移，
        而不是用一个"猜年份"的启发式把它藏起来。
        """
        m = _LANDING_DATE_RE.search(str(value or "").strip())
        if not m:
            return None
        month, day = int(m.group(1)), int(m.group(2))
        hour = int(m.group(3)) if m.group(3) else 0
        minute = int(m.group(4)) if m.group(4) else 0
        try:
            return time.mktime(
                datetime(int(year), month, day, hour, minute).timetuple())
        except (ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _parse_landing_schedule(payload_text, year=None) -> dict:
        """Flight 载荷 → 赛程各分段及其两根时间轴（纯函数，P6-5②）

        **只读具名 JSON 字段，不拿正则去扫中文**：实测结构是
        `{"schedule": {"stages": [{"timeline_label": "报名",
                                   "date": "08.19 – 09.14 12:00"}, …]}}`，
        `timeline_label` 与 `date` 是字段名给的契约；"扫中文"拿到的是一段给人读的
        文本，字段一旦调整就会静默取到空值。

        一手对齐证据（2026-09-13）：接口 `RegistrationDeadline=1789358400` 与页面
        `报名 08.19–09.14 12:00` 逐字符吻合；而同页 `作品提交 08.19–09.14 22:00`
        在接口里**完全没有**（`EndTime=0`）——这正是这两跳存在的理由。

        Returns:
            {"stages": [{"label": str, "date": str}, …],
             "registration_ts": float|None, "submit_ts": float|None}
        """
        schedule = ModelScopeProvider._extract_json_object(payload_text, "schedule")
        stages = []
        if isinstance(schedule, dict) and isinstance(schedule.get("stages"), list):
            for raw in schedule["stages"]:
                if not isinstance(raw, dict):
                    continue
                stages.append({
                    "label": str(raw.get("timeline_label") or ""),
                    "date": str(raw.get("date") or ""),
                })
        if not stages:
            return {"stages": [], "registration_ts": None, "submit_ts": None}

        if year is None:
            year = datetime.now().year

        def _axis(keyword):
            for st in stages:
                if keyword in st["label"]:
                    return ModelScopeProvider._parse_landing_date(st["date"], year)
            return None

        reg = _axis(LANDING_LABEL_REGISTRATION)
        submit = None
        for st in stages:               # "作品提交" 也含"报名"以外的词，先精确后兜底
            if LANDING_LABEL_SUBMIT in st["label"]:
                submit = ModelScopeProvider._parse_landing_date(st["date"], year)
                break
        return {"stages": stages, "registration_ts": reg, "submit_ts": submit}

    def _fetch_landing(self, race: dict) -> dict:
        """两跳抓取落地页并解析赛程；**任何失败返回 {}**（等于没这个功能）

        链路（2026-09-13 22:27 实测，匿名、无 Cookie）：
          第一跳 GET `<SignUpUrl>`（modelscope.cn/active/<slug>）→ 1,377 字节 HTML
          第二跳 GET 其中的 iframe `https://mseo-ai-inf.ms.show/` → 55,077 字节 SSR

        **抓失败必须等于没这个功能**（与 `base.py` 的单源失败不拖垮任务一致，
        也与本 Provider 的 `_served` 降级语义一致）：失败路径只打印一行原因并
        返回 {}，调用方按"只有报名截止"继续，并按 P6-3 打缺失标记，
        **不许静默填空**。
        """
        result: dict = {}
        if not self._landing_triggered(race):
            return result
        signup = self._landing_signup_url(race)
        headers = {"User-Agent": "personal-agent-resource-scout/0.1"}
        try:
            hop1 = requests.get(signup, headers=headers,
                                timeout=LANDING_HOP_TIMEOUT)
            if hop1.status_code != 200:
                print(f"[Search] 落地页第一跳非 200（{hop1.status_code}），"
                      f"退回报名截止")
                return result
            iframe = self._landing_iframe_url(hop1.text)
            if not iframe:
                print("[Search] 落地页第一跳未找到 iframe 地址，退回报名截止")
                return result
            hop2 = requests.get(iframe, headers=headers,
                                timeout=LANDING_IFRAME_TIMEOUT)
            if hop2.status_code != 200:
                print(f"[Search] 落地页第二跳非 200（{hop2.status_code}），"
                      f"退回报名截止")
                return result
            # 显式按 UTF-8 解码：载荷里的中文是字面 UTF-8 字节，
            # 交给 requests 按响应头猜编码会在缺 charset 时得到 ISO-8859-1 乱码
            payload = self._extract_flight_payload(
                hop2.content.decode("utf-8", errors="replace"))
            if not payload:
                print("[Search] 落地页第二跳没有 Next.js 内嵌载荷，退回报名截止")
                return result
            year = datetime.fromtimestamp(
                float(race.get("RegistrationDeadline"))).year \
                if isinstance(race.get("RegistrationDeadline"), (int, float)) \
                and not isinstance(race.get("RegistrationDeadline"), bool) \
                and race.get("RegistrationDeadline") > 0 else None
            parsed = self._parse_landing_schedule(payload, year=year)
        except requests.RequestException as e:
            print(f"[Search] 落地页两跳请求失败（忽略，退回报名截止）: {e}")
            return result
        except Exception as e:      # 解析异常同样降级：抓失败 = 没这个功能
            print(f"[Search] 落地页解析失败（忽略，退回报名截止）: {e!r}")
            return result

        if not parsed["stages"]:
            print("[Search] 落地页赛程字段为空，退回报名截止")
            return result
        result["landing_page_url"] = iframe
        result["landing_fetched_at"] = now_str()
        result["landing_stages"] = parsed["stages"]
        result["registration_deadline_ts"] = parsed["registration_ts"]
        result["submit_deadline_ts"] = parsed["submit_ts"]
        return result

    def _fetch_landings(self, races: List[dict], now_ts: float) -> dict:
        """对命中触发条件的行做两跳，返回 {id(race): 解析结果}

        两个刻意的边界：
        - **先处理"按接口口径还开着"的行**：它们才是这一轮真正会被展示的行，
          预算有限时优先给它们；
        - 总数受 `LANDING_MAX_FETCHES` 硬约束（见该常量的注释），
          保证最坏耗时不会随数据漂移吃掉调用方的 15s 预算。
        """
        out: dict = {}
        if LANDING_MAX_FETCHES <= 0:
            return out
        planned = [r for r in races if self._landing_triggered(r)]
        if not planned:
            return out
        planned.sort(key=lambda r: 0 if self._is_open(r, now_ts) else 1)
        for race in planned[:LANDING_MAX_FETCHES]:
            got = self._fetch_landing(race)
            if got:
                out[id(race)] = got
        return out

    # ---------- 网络 ----------

    def _fetch_all_races(self) -> List[dict]:
        """翻页拉取全部 race（失败返回 []，由调用方统一降级）"""
        headers = {"User-Agent": "personal-agent-resource-scout/0.1"}
        races: List[dict] = []
        seen_ids = set()
        total = None
        for page in range(1, self.max_pages + 1):
            try:
                resp = requests.get(
                    self.LIST_API,
                    params={"PageNumber": page, "PageSize": self.page_size},
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as e:
                print(f"[Search] ModelScope 列表请求失败（第 {page} 页，忽略）: {e}")
                return []
            if resp.status_code != 200:
                print(f"[Search] ModelScope 列表返回非 200"
                      f"（{resp.status_code}），跳过该来源")
                return []
            try:
                payload = resp.json()
            except ValueError:
                print("[Search] ModelScope 返回非 JSON 响应，跳过该来源")
                return []
            if not isinstance(payload, dict) or payload.get("Success") is not True:
                print(f"[Search] ModelScope Success != true"
                      f"（Code={payload.get('Code') if isinstance(payload, dict) else '?'}），"
                      f"跳过该来源")
                return []
            data = payload.get("Data")
            if not isinstance(data, dict) or not isinstance(data.get("Races"), list):
                print("[Search] ModelScope 响应缺少 Data.Races，跳过该来源")
                return []
            batch = data["Races"]
            if total is None:
                tc = data.get("TotalCount")
                total = int(tc) if isinstance(tc, (int, float)) else None
            for r in batch:
                if not isinstance(r, dict):
                    continue
                key = r.get("Id")
                if key in seen_ids:      # 防御：接口在翻页期间有新增时可能重叠
                    continue
                seen_ids.add(key)
                races.append(r)
            if not batch:
                break
            if total is not None and len(races) >= total:
                break
        return races

    def search(self, query: str, limit: int = 5) -> List[Evidence]:
        """返回所有报名未截止的魔搭竞赛/活动（query / limit 均被忽略，见模块 docstring）

        失败（网络异常 / 非 200 / Success != true / 缺 Data.Races）一律返回 []
        并打印一行原因；**无论成功失败都置位 self._served**，故同一实例第二次
        调用必然返回 []（单轮内不重试）。

        P6-5：命中触发条件的行会额外做一次**落地页两跳**，用来补第二根时间轴与
        第二个报名截止来源。两跳失败只是"没这个功能"——这一行照常入库，
        只是按 P6-3 打缺失标记；**它不会让本条返回 []，也不会让某一行被跳过**。
        """
        if self._served:
            return []
        self._served = True   # 先置位：保证任何失败路径也不会被重复调用
        races = self._fetch_all_races()
        if not races:
            print("[Search] ModelScope 本轮无可用条目（失败或空结果），跳过该来源")
            return []
        now_ts = self._now_ts if self._now_ts is not None else time.time()
        # 两跳必须在"判断是否还开放"之前：落地页可能给出**更晚**的报名截止
        # （P6-3③），先判再抓会把这种行按接口口径误判为已截止而丢掉
        landings = self._fetch_landings(races, now_ts)
        evidence: List[Evidence] = []
        for race in races:
            landing = landings.get(id(race))
            if not self._is_open(race, now_ts,
                                 (landing or {}).get("registration_deadline_ts")):
                continue
            ev = self._race_to_evidence(race, landing)
            if ev is not None:
                evidence.append(ev)
        print(f"[Search] ModelScope 拉取 {len(races)} 条，"
              f"其中报名未截止 {len(evidence)} 条"
              + (f"，落地页两跳成功 {len(landings)} 条" if landings else ""))
        # 资源消耗埋点（与 ddgs / github 同款：失败不影响主流程）
        try:
            from memory.resource_log import log_resource_usage, estimate_tokens
            log_resource_usage("search", estimate_tokens(query), "modelscope",
                               f"一手列表接口；返回 {len(evidence)} 条未截止活动")
        except Exception:
            pass
        return evidence
