#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档完整性守卫（checks/check_doc_integrity.py）

目的：
    拦住**文档拼接事故**。动因是 `PROJECT_STATUS.md` 已**第三次**出现同一形态的
    事故：一次编辑把同一段落的新旧两版都留了下来（新版紧跟旧版、或以游离标点
    开头），而任何一次常规阅读都可能只看到其中一版。根文档（份数以复跑为准——
    扫描集＝仓库根 *.md，随根文档增减而增减）是本项目的
    "单一可信参考"，它们被拼坏一次的代价是**所有人照着错的读**——代码有回归
    兜底，文档没有。

为什么是机械检查而不是"编辑时小心点"：
    拼接事故的特征是"两段极长的、几乎相同的文本同时存在"，人眼在几千字一行
    的文档里天然漏看；而"同一份文档里有两段 80% 相同的长文本"这件事本身是
    可计算的。本项目对这类问题的既定处置是**给它一个检查器**，而不是再写一条
    规范——规范是主动遵守，检查器是被动拦截。

扫描范围：
    仅**项目根目录下的 `*.md`（不含子目录、不含隐藏文件）**。刻意不含
    `tests/TEST_RESULTS.md`：那一份由 `core/test_runner.py` 每轮回归自动重写，
    把它扫进来会让本检查器的命中集合随"上一次跑没跑回归"漂移，而门禁必须只
    取决于工作区内容。子目录文档（skills/plugins 的说明）未纳入：拼接事故目前
    只发生在根文档，范围可随时扩大，扩大时要连同豁免台账一起复扫。

检测口径（两条，都可离线纯函数复算）：
    1. **重复长片段**（`[DOC_DUPLICATE_FRAGMENT]`）：把每份文档按行归一化
       （折叠空白）后，对长度 ≥ `MIN_LINE_CHARS` 的行求 `GRAM_SIZE` 元字组集合；
       两行共享的元字组数 ≥ `MIN_SHARED_GRAMS`、且占较短一行元字组总数的比例
       ≥ `DUPLICATE_RATIO` 时判为"同一段长文本出现了两版"。
       为什么用元字组而不是"整行相等"：拼接事故里新旧两版**往往不是逐字相同**
       （新的一版多改了一句话），逐字比较会漏；而"两行有 80% 的 20 字窗口重合"
       正好是"这两段在说同一件事"的可计算表述。
       为什么用比例而不是纯计数：`PROJECT_STATUS.md` 的单行可达数千字，光看共享
       窗口数会把"都很长、都提了同一批文件名"的正常行判成重复；除以较短一行的
       规模后，"长得像"与"只是都长"才分得开。
    2. **章节编号连续性**（`[DOC_NUMBERING_GAP]`）：
       - 标题 `#…###### 一、…` 里的中文序号，**同一层级**必须从"一"开始逐一递增；
       - 有序列表项 `N. ` 必须逐项 +1（回到 1 视为开始新的一段，段内必须连续）。
       拼接会同时把编号弄乱（旧版整段复制回来，序号要么重复要么跳号），
       而编号断裂是**最容易被人一眼看见、也最容易被"顺手改一个数字"掩盖**的
       症状——掩盖之后文档看起来就正常了，内容却是错的。
       被 ``` 围起来的代码块一律跳过：里面的 `# 注释` 与 `1. 步骤` 是代码不是
       章节（本项目根文档里有 PowerShell 命令块，不跳过会当场误报一片）。

豁免机制：
    命中按 `<文件名>:<行号>`（编号断裂）或 `<文件名>:<行号>-<行号>`（重复片段，
    两个行号升序）写入 checks/doc_integrity_exemptions.json；**首次落地时该台账
    为空**——2026-09-13 首扫时零命中；此后根文档会增减，份数以复跑为准。
    格式为对象数组 `{"key": …, "reason": …, "since": …}`，解析统一走
    checks/exemption_loader.py 的 collect_keys()（裸字符串会打印
    [EXEMPTION_NO_REASON] 并以退出码 1 失败）。
    过期检测：台账里有、本次扫描未命中的条目打印 [STALE_EXEMPTION] 并以退出码 1
    失败——本台账是**行号级**的，行号位移即失效，属有意设计。

输出词根约定（全仓库统一）：
    [DOC_DUPLICATE_FRAGMENT] —— 同一文档里出现了两版几乎相同的长文本
    [DOC_NUMBERING_GAP]      —— 章节 / 列表编号断裂
    [STALE_EXEMPTION]        —— 台账条目已不再命中（"豁免是旧的"）

退出码：
    0 —— 无未豁免命中，且台账无过期条目；
    1 —— 存在未豁免命中，或台账存在过期条目；
    2 —— 豁免台账缺失 / 格式非法等内部错误（fail-closed）。
"""

import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# 共享台账解析（checks/exemption_loader.py）：把本文件所在目录加入 sys.path，
# 兼容"直跑脚本"与"pre-commit 钩子按文件路径加载"两种方式
_CHECKS_DIR = Path(__file__).resolve().parent
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))
from exemption_loader import collect_keys  # noqa: E402

# 项目根目录（本文件位于 checks/ 下）与被扫描的文档范围
ROOT = _CHECKS_DIR.parent
DOC_GLOB = "*.md"

# 豁免台账路径
EXEMPTIONS_FILE = _CHECKS_DIR / "doc_integrity_exemptions.json"

# ---- 重复长片段的判据（三个常量一起决定灵敏度，改任何一个都要复跑全部根文档，份数以复跑为准）----
GRAM_SIZE = 20          # 元字组长度
MIN_LINE_CHARS = 80     # 只比较够长的行（短行的重复是正常的行文重复）
MIN_SHARED_GRAMS = 60   # 共享元字组下限（≈ 至少 80 字连续或两段共 80 字的重复）
DUPLICATE_RATIO = 0.5   # 共享量须占较短一行的元字组总数一半以上

#: 围栏代码块标记（``` 开头即视为围栏边界，块内内容不参与任何检测）
_FENCE_RE = re.compile(r"^\s*```")

#: 标题里的中文序号（`## 五、…`）
_HEADING_RE = re.compile(r"^(#{1,6})\s*([一二三四五六七八九十]+)、")

#: 有序列表项（`12. 正文…`）；要求点号后跟空白，避免命中 `3.8` 这类版本号
_LIST_ITEM_RE = re.compile(r"^(\d+)\.\s+\S")

#: 中文数字字符表
_CN_DIGITS = {c: i for i, c in enumerate("一二三四五六七八九十", start=1)}


def iter_docs():
    """遍历项目根目录下的 *.md（按文件名排序，保证输出稳定）

    不含子目录、不含隐藏文件；`checks/`、`data/` 等目录因此天然不在范围内。
    """
    for path in sorted(ROOT.glob(DOC_GLOB)):
        if path.is_file() and not path.name.startswith("."):
            yield path


def strip_fenced_blocks(text: str):
    """按行剔除以 ``` 围起来的代码块，返回保序的行列表（含原始行号信息）

    返回的每个元素是 `(行号, 行内容)`，行号从 1 开始且与原文一致——豁免台账按
    行号记账，行号必须指得回原文。
    """
    out = []
    in_fence = False
    for lineno, raw in enumerate(text.split("\n"), start=1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append((lineno, raw))
    return out


def normalize_line(text: str) -> str:
    """归一化一行：折叠所有空白并去首尾（重复判据在归一化后的文本上做）"""
    return re.sub(r"\s+", " ", text or "").strip()


def cn_number_to_int(text: str) -> int:
    """中文序号 → 整数；无法识别返回 0（不猜）

    支持"一"…"十"、"十一"、"二十"、"二十三"这类常见写法；本项目根文档只用到
    一~十，多出来的分支是为了让"十五、"这类写法不会静默算成 0 而**跳过**一次
    断裂检测（静默跳过比误报更糟）。
    """
    text = (text or "").strip()
    if not text:
        return 0
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGITS.get(text[1:], 0)
    if text.endswith("十"):
        return _CN_DIGITS.get(text[:-1], 0) * 10
    if "十" in text:
        head, tail = text.split("十", 1)
        return _CN_DIGITS.get(head, 0) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    return _CN_DIGITS.get(text, 0)


def gram_set(text: str) -> set:
    """文本的 `GRAM_SIZE` 元字组集合（文本短于 GRAM_SIZE 时返回空集）"""
    if len(text) < GRAM_SIZE:
        return set()
    return {text[i:i + GRAM_SIZE] for i in range(len(text) - GRAM_SIZE + 1)}


def find_duplicate_fragments(name: str, lines):
    """返回重复长片段命中：`[(豁免键, 说明, "duplicate"), …]`

    lines: `[(行号, 行内容), …]`（已剔除代码块）
    """
    grams = {}
    for lineno, raw in lines:
        text = normalize_line(raw)
        if len(text) >= MIN_LINE_CHARS:
            got = gram_set(text)
            if got:
                grams[lineno] = got
    if len(grams) < 2:
        return []

    # 元字组 → 出现的行号集合（只保留出现在多行上的元字组）
    index = defaultdict(set)
    for lineno, gs in grams.items():
        for gram in gs:
            index[gram].add(lineno)

    shared = Counter()
    for gram, linenos in index.items():
        if len(linenos) < 2:
            continue
        ordered = sorted(linenos)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                shared[(ordered[i], ordered[j])] += 1

    hits = []
    for (a, b), count in sorted(shared.items()):
        if count < MIN_SHARED_GRAMS:
            continue
        smaller = min(len(grams[a]), len(grams[b]))
        if smaller and count / smaller >= DUPLICATE_RATIO:
            hits.append((
                f"{name}:{a}-{b}",
                f"第 {a} 行与第 {b} 行有 {count} 个 {GRAM_SIZE} 字窗口重合"
                f"（占较短行 {count / smaller:.0%}）——疑似同一段落的新旧两版并存",
                "duplicate",
            ))
    return hits


def find_numbering_gaps(name: str, lines):
    """返回章节 / 列表编号断裂命中：`[(豁免键, 说明, "numbering"), …]`"""
    hits = []
    # 规则一：同层级标题的中文序号必须从"一"起逐一递增
    last_cn = {}
    for lineno, raw in lines:
        m = _HEADING_RE.match(raw.strip())
        if not m:
            continue
        level, want = len(m.group(1)), cn_number_to_int(m.group(2))
        expect = last_cn.get(level, 0) + 1
        if want != expect:
            hits.append((
                f"{name}:{lineno}",
                f"{'#' * level} 标题序号「{m.group(2)}、」应为「{expect}」"
                f"（同级上一个序号是 {last_cn.get(level, 0)}）",
                "numbering",
            ))
        last_cn[level] = want

    # 规则二：有序列表逐项 +1；回到 1 视为新一段，段内必须连续
    current = None
    for lineno, raw in lines:
        m = _LIST_ITEM_RE.match(raw.strip())
        if not m:
            continue
        number = int(m.group(1))
        if current is None or number == 1:
            current = number
            continue
        if number != current + 1:
            hits.append((
                f"{name}:{lineno}",
                f"有序列表项跳到 {number}（上一项是 {current}）",
                "numbering",
            ))
        current = number
    return hits


def load_exemptions() -> set:
    """读取豁免台账；文件不存在返回空集，内容非法则以退出码 2 终止"""
    if not EXEMPTIONS_FILE.exists():
        return set()
    try:
        data = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] 豁免台账无法解析：{EXEMPTIONS_FILE}（{exc}）", file=sys.stderr)
        sys.exit(2)
    return collect_keys(data, EXEMPTIONS_FILE)


def main() -> int:
    started = time.perf_counter()

    # Windows 控制台/重定向编码兜底：尽量以 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    exemptions = load_exemptions()

    docs = list(iter_docs())
    if not docs:
        print("[ERROR] 根目录下没有可扫描的 *.md（扫描范围异常，fail-closed）",
              file=sys.stderr)
        return 2

    flagged = []
    for path in docs:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            print(f"[ERROR] 文档不可读：{path}（{exc}）", file=sys.stderr)
            return 2
        lines = strip_fenced_blocks(text)
        flagged.extend(find_duplicate_fragments(path.name, lines))
        flagged.extend(find_numbering_gaps(path.name, lines))

    unexempted = [(k, d, kind) for k, d, kind in flagged if k not in exemptions]

    # 输出未豁免命中：两类词根分开打，便于一眼看出是哪种事故。
    # 词根由**命中类型**决定，不由说明文字里有没有某个词决定——按文案反推类别
    # 会在改一次措辞之后静默换错词根（本项目已两次被"两处手工保持一致"咬到）。
    for key, detail, kind in sorted(unexempted):
        root = ("[DOC_NUMBERING_GAP]" if kind == "numbering"
                else "[DOC_DUPLICATE_FRAGMENT]")
        print(f"{root} {key}：{detail}")

    stale = sorted(exemptions - {k for k, _, _ in flagged})
    for key in stale:
        print(f"[STALE_EXEMPTION] {key}（已不再命中，建议从台账移除）")

    print(
        f"扫描根文档 {len(docs)} 份，命中 {len(flagged)} 处，"
        f"未豁免 {len(unexempted)} 处，台账过期 {len(stale)} 处"
    )
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")

    return 1 if (unexempted or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
