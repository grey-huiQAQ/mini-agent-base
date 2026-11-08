# -*- coding: utf-8 -*-
"""
文档完整性检查器（checks/check_doc_integrity.py）测试 —— 全部离线、不打网络、不碰数据库

覆盖（P6-6①，2026-09-13）：
- 重复长片段：**合成一次真实的拼接事故**（同一段落新旧两版并存、中段改了一句），
  断言检查器报出来；同时断言短行的正常重复**不报**（灵敏度不能靠"什么都报"换来）；
- 章节编号连续性：标题跳号 / 有序列表跳号各一条；回到 1 视为新一段（合法重启不报）；
- 围栏代码块内的 `# 注释` 与 `1. 步骤` 不参与检测（本项目根文档里有 PowerShell 块，
  不跳过会当场误报一片）；
- 中文数字换算；
- **全部根文档当前零命中**（扫描集＝仓库根 `*.md`，份数以复跑为准）——这条同时是"文档现在没被拼坏"的回归哨兵，也是本检查器
  灵敏度不至于乱报的实测证据（一个天天报的检查器会在两三次内被绕掉）。
"""
import importlib.util
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CHECKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "checks")


def _load_checker():
    """按文件路径加载检查器模块（checks/ 不是包，用 importlib 直载）"""
    path = os.path.join(_CHECKS_DIR, "check_doc_integrity.py")
    spec = importlib.util.spec_from_file_location("check_doc_integrity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


#: 一段够长的中文正文（拼接事故里的"同一段落"；短于 MIN_LINE_CHARS 的行不参与比较）
_PARA = ("检查器只回答有没有做隔离这件事；一个写了 patch 却不生效的测试会以真实 DB "
         "断言失败的形式暴露，那是测试自己的责任，不由静态检查器替它兜底，这一点"
         "与既有五个检查器的口径一致，不需要额外说明，也不需要任何别的补充文字。")


def setup():
    """本文件不碰数据库，无需隔离；保留空 setup 与既有测试文件同形"""
    return None


def teardown():
    """无资源需要清理"""
    return None


def test_duplicate_fragment_is_reported():
    """拼接事故：同一段落新旧两版并存 → 必须报 [DOC_DUPLICATE_FRAGMENT]"""
    doc = "\n".join([
        "## 一、甲",
        _PARA,
        "",
        "## 二、乙",
        # 新版：只把中段一句话换了，其余逐字相同（这正是真实事故的形态）
        _PARA.replace("那是测试自己的责任", "这是测试自己的责任"),
    ])
    lines = _checker.strip_fenced_blocks(doc)
    hits = _checker.find_duplicate_fragments("X.md", lines)
    assert len(hits) == 1, hits
    key, detail, kind = hits[0]
    assert kind == "duplicate", hits
    assert key == "X.md:2-5", key
    assert "重合" in detail, detail
    print("[OK] doc duplicate fragment is reported")


def test_duplicate_detection_ignores_short_and_unrelated_lines():
    """灵敏度不能靠"什么都报"换来：短行的重复与内容无关的长行都不报"""
    short = "同一句短话"                      # 远短于 MIN_LINE_CHARS
    doc = "\n".join([short, short, short])
    assert _checker.find_duplicate_fragments(
        "X.md", _checker.strip_fenced_blocks(doc)) == []
    # 两行都很长但内容不同（本项目 PROJECT_STATUS 的单行可达数千字，
    # 光看"都很长"就会误报）
    other = ("这一行讲的是完全另一件事，与上一行没有任何共同的长片段，"
             "只是它同样很长，长度本身不构成重复的证据，比例才是。")
    doc2 = "\n".join([_PARA, other])
    assert _checker.find_duplicate_fragments(
        "X.md", _checker.strip_fenced_blocks(doc2)) == []
    print("[OK] duplicate detection ignores short and unrelated lines")


def test_numbering_gaps_are_reported():
    """章节编号连续性：标题跳号、有序列表跳号各报一条"""
    doc = "\n".join([
        "## 一、甲",
        "## 二、乙",
        "## 四、丁",          # 跳过"三" → 断裂
        "",
        "1. 第一项",
        "2. 第二项",
        "4. 第四项",          # 跳过 3 → 断裂
    ])
    hits = _checker.find_numbering_gaps("X.md", _checker.strip_fenced_blocks(doc))
    assert len(hits) == 2, hits
    assert hits[0][0] == "X.md:3" and "标题序号" in hits[0][1], hits
    assert hits[0][2] == "numbering", hits
    assert hits[1][0] == "X.md:7" and "有序列表项" in hits[1][1], hits
    print("[OK] doc numbering gaps are reported")


def test_numbering_restart_at_one_is_legal():
    """回到 1 视为新一段（本仓库 AGENTS.md 里就有多段各自从 1 开始的清单）"""
    doc = "\n".join([
        "1. 甲", "2. 乙",            # 第一段
        "", "1. 丙", "2. 丁", "3. 戊",  # 第二段，重新从 1 开始，合法
    ])
    assert _checker.find_numbering_gaps(
        "X.md", _checker.strip_fenced_blocks(doc)) == []
    # 同层级标题同理：连续的一二三是合法的
    ok = "\n".join(["## 一、a", "## 二、b", "## 三、c"])
    assert _checker.find_numbering_gaps(
        "X.md", _checker.strip_fenced_blocks(ok)) == []
    print("[OK] doc numbering restart at one is legal")


def test_fenced_code_blocks_are_skipped():
    """围栏代码块里的 `# 注释` 与 `1. 步骤` 是代码，不是章节/列表"""
    doc = "\n".join([
        "## 一、甲",
        "```powershell",
        "# 1. 安装依赖",
        "# 2. 配置 API Key",
        "# 9. 这几行在代码块里，既不构成标题也不构成列表",
        "```",
        "## 二、乙",
        "1. 第一项",
        "2. 第二项",
    ])
    lines = _checker.strip_fenced_blocks(doc)
    assert _checker.find_numbering_gaps("X.md", lines) == []
    # 代码块里的重复文本也不该被当成"拼接事故"
    fenced = "\n".join(["```", _PARA, "```", "```", _PARA, "```"])
    assert _checker.find_duplicate_fragments(
        "X.md", _checker.strip_fenced_blocks(fenced)) == []
    # 行号必须指得回原文（豁免台账按行号记账）：代码块被剔除后行号不重编
    assert [n for n, _ in lines][:2] == [1, 7], [n for n, _ in lines]
    print("[OK] fenced code blocks are skipped")


def test_cn_number_to_int():
    """中文序号换算：覆盖本项目用到的写法，无法识别返回 0（不猜）"""
    assert _checker.cn_number_to_int("一") == 1
    assert _checker.cn_number_to_int("八") == 8
    assert _checker.cn_number_to_int("十") == 10
    assert _checker.cn_number_to_int("十一") == 11
    assert _checker.cn_number_to_int("二十") == 20
    assert _checker.cn_number_to_int("二十三") == 23
    assert _checker.cn_number_to_int("") == 0
    assert _checker.cn_number_to_int("甲") == 0
    print("[OK] cn number to int")


def test_repo_root_docs_are_clean():
    """回归哨兵：全部根文档当前零命中（份数以复跑为准，2026-10-07 锚点落盘后扫描集已变大）

    这条断言有两个作用：① 文档一旦被拼坏，本用例立刻红；② 它同时是"检查器灵敏度
    合理"的实测证据——一个天天报的检查器会在两三次之内被绕掉（本项目已明令禁止
    `--no-verify`，所以门禁必须只在真该红的时候红）。
    """
    docs = list(_checker.iter_docs())
    assert docs, "根目录下应至少有一份 *.md"
    total = 0
    for path in docs:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = _checker.strip_fenced_blocks(text)
        dup = _checker.find_duplicate_fragments(path.name, lines)
        num = _checker.find_numbering_gaps(path.name, lines)
        assert dup == [], (path.name, dup)
        assert num == [], (path.name, num)
        total += 1
    assert total >= 3, f"应至少扫到 3 份根文档，实际 {total}"
    print(f"[OK] repo root docs are clean ({total} docs, 0 hits)")


def test_cleanup():
    """无产物需要清理"""
    print("[OK] doc integrity test cleanup")


if __name__ == "__main__":
    print("=== doc integrity test ===\n")
    setup()
    try:
        test_duplicate_fragment_is_reported()
        test_duplicate_detection_ignores_short_and_unrelated_lines()
        test_numbering_gaps_are_reported()
        test_numbering_restart_at_one_is_legal()
        test_fenced_code_blocks_are_skipped()
        test_cn_number_to_int()
        test_repo_root_docs_are_clean()
        test_cleanup()
    finally:
        teardown()
    print("\n=== ALL DOC INTEGRITY TESTS PASSED ===")
