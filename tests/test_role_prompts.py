# -*- coding: utf-8 -*-
"""
角色人设加载（prompts/role_prompts.py）测试 —— 纯配置读取，不涉及数据库

覆盖（P0-2 回归）：
- roles.json schema：每个角色配置同时含 "prompt" 与 "tools" 键（键名契约）
- load_default_prompt 对每个角色返回非空默认人设（曾误读 "system_prompt"
  键导致四段角色人设恒为空、从未发送给模型）
- 工具存在性：get_role_tools 返回各角色的工具白名单；未知角色安全回退

覆盖（P7 批次 5b 新增）：
- get_prompt 按工具白名单追加文件读写边界文案（可写版 / 只读版 / 空串三档），
  且角色人设"进化"过时仍然追加（故落点在 get_prompt 而非 roles.json）
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompts.role_prompts import (
    load_roles,
    load_default_prompt,
    get_prompt,
    get_role_tools,
    get_all_roles,
    ROLE_PROMPTS,
)


def _roles() -> dict:
    """读取 roles.json 原始配置（测试用）"""
    roles = load_roles()
    assert roles, "roles.json 不应为空"
    return roles


def test_roles_schema():
    """roles.json 每个角色必须含 prompt 与 tools 键（键名契约）"""
    roles = _roles()
    for role, cfg in roles.items():
        assert isinstance(cfg, dict), f"{role} 配置应为对象"
        assert "prompt" in cfg, f"{role} 缺少 prompt 键"
        assert "tools" in cfg, f"{role} 缺少 tools 键"
        assert isinstance(cfg["prompt"], str) and cfg["prompt"].strip(), \
            f"{role} 的 prompt 应为非空字符串"
        assert isinstance(cfg["tools"], list), f"{role} 的 tools 应为列表"
    # ROLE_PROMPTS 导出与源文件一致（加载正常）
    assert set(ROLE_PROMPTS.keys()) == set(roles.keys())
    print(f"[OK] roles schema: {len(roles)} 个角色均含 prompt/tools 键")


def test_load_default_prompt_non_empty():
    """回归 P0-2：按 prompt 键返回非空默认人设（曾误读 system_prompt 恒为空）"""
    roles = _roles()
    for role in roles:
        prompt = load_default_prompt(role)
        assert prompt and prompt.strip(), f"{role} 默认人设为空，疑似键名错误"
    # 未知角色安全回退为空字符串，不抛异常
    assert load_default_prompt("不存在的角色") == ""
    print(f"[OK] load_default_prompt 非空：{len(roles)} 个角色均返回人设")


def test_get_role_tools_available():
    """工具存在性：get_role_tools 返回与 roles.json 一致的工具白名单"""
    roles = _roles()
    assert sorted(get_all_roles()) == sorted(roles.keys())
    for role, cfg in roles.items():
        assert get_role_tools(role) == cfg["tools"], f"{role} 工具白名单不一致"
    # 未知角色安全回退为空列表
    assert get_role_tools("不存在的角色") == []
    print(f"[OK] get_role_tools 可用：{len(roles)} 个角色工具白名单一致")


def test_get_prompt_appends_file_usage_notice():
    """P7 批次 5b / 6-④：工具用法边界写进系统提示（落点在 get_prompt，不在 roles.json）

    断言什么：
      ① 每个角色经 get_prompt() 拿到的提示词都含稳定词「专属目录」「公共书架」；
      ② 工具白名单含 write_file 的角色拿到**可写版**（逐字比对），不含 write_file 但含
         文件类工具的角色拿到**只读版**（逐字比对，且不出现"你写了"这类表述）；
         稳定词断言只在这两支里做——不含任何文件类工具的那一档 roles.json 里**没有角色**，
         故它实际不可达（P7 批次 7-④ 之前把稳定词断言放在循环开头，会让那一档结构性不可达
         却看不出来）；该档的计数器照常打印，为 0 就是 0，不谎称"已测"；
      ③ 白名单含 web_search 的角色拿到**搜索用法**（逐字比对），不含的角色一个字都不该有
         ——「英语」有 rag_search 却没有 web_search，所以判据必须是 web_search 本身，
         不能用 rag_search 或"有检索类工具"来近似；
      ④ 两维各自的档位在 roles.json 里**都被真实用到**（否则会静默只覆盖一档）；
      ⑤ 片段顺序＝人设 → 搜索说明 → 文件用法（文件用法留在结尾，既有 endswith 断言因此
         一字不用改），且不产生前导空行或连续空行；
      ⑥ 人设"进化"过（PromptStore 有活跃版本）时两类文案仍在、人设本身不被改动
         —— 这正是落点取 get_prompt 而不是 roles.json 的原因；
      ⑦ 不含文件类工具的角色不追加文件用法；未知角色仍返回空串。

    隔离说明：用 mock 替掉 PromptStore.get_active_prompt，本用例全程不碰数据库
    （比把 DB_PATH 指向临时库更彻底：连生产库的只读连接都不会建立）。
    """
    from unittest.mock import patch

    writable = (
        "文件用法：写入只会落在你的专属目录，路径由系统决定——不要在工具参数里传路径，"
        "传了会被忽略。另有一个只读的「公共书架」，放的是共享的知识与经验，可直接用 "
        "read_file / list_files 读取；想贡献内容，请在回复里整理好交给用户决定是否收录。"
    )
    readonly = (
        "文件用法：你只读不写。可用 read_file / list_files 读取你的专属目录，"
        "以及只读「公共书架」（共享的知识与经验）。不要在工具参数里传路径，传了会被忽略。"
    )
    search_notice = (
        "搜索用法：调用 web_search 时，查询词里不要出现你从文件或知识库里读到的内容原文——"
        "搜索词会离开本机，需要检索时请自己概括成关键词。"
    )

    with patch("prompts.role_prompts.PromptStore.get_active_prompt", return_value=None):
        writable_roles, readonly_roles = 0, 0
        no_file_roles, search_roles, no_search_roles = 0, 0, 0
        for role in get_all_roles():
            prompt = get_prompt(role)
            tools = set(get_role_tools(role))
            if "write_file" in tools:
                # 稳定词断言只放在"真的追加了文件用法"的两支里（P7 批次 7-④）：
                # 原先它放在循环开头、对每个角色无条件断言，于是下面那个 else 支
                # 结构性不可达（真有"不含文件类工具"的角色时，先炸的是这一条，
                # 而不是 else 支自己的断言）——"三档"实际只有两档。
                assert "专属目录" in prompt and "公共书架" in prompt, \
                    f"{role} 的系统提示缺文件用法稳定词：{prompt!r}"
                assert prompt.endswith(writable), \
                    f"{role} 含 write_file，应拿到可写版文件用法：{prompt!r}"
                writable_roles += 1
            elif tools & {"read_file", "list_files"}:
                assert "专属目录" in prompt and "公共书架" in prompt, \
                    f"{role} 的系统提示缺文件用法稳定词：{prompt!r}"
                assert prompt.endswith(readonly), \
                    f"{role} 只读，应拿到只读版文件用法：{prompt!r}"
                assert "你写了" not in prompt, \
                    f"只读角色的文案不该出现写了东西的表述：{prompt!r}"
                readonly_roles += 1
            else:
                # 本意不变：不含文件类工具 ⇒ 不追加文件用法。本批（7-④）多了"搜索用法"
                # 这一维，故这里断言"两版文件用法都不在"，而不是整个提示词等于默认人设。
                assert not prompt.endswith(writable) and not prompt.endswith(readonly), \
                    f"{role} 不含文件类工具，不该被追加文件用法：{prompt!r}"
                no_file_roles += 1

            if "web_search" in tools:
                assert search_notice in prompt, \
                    f"{role} 含 web_search，应拿到搜索用法（逐字）：{prompt!r}"
                search_roles += 1
            else:
                assert search_notice not in prompt, \
                    f"{role} 不含 web_search，不该拿到搜索用法：{prompt!r}"
                no_search_roles += 1

            assert "\n\n\n" not in prompt and not prompt.startswith("\n"), \
                f"{role} 的提示词不该出现连续空行或前导空行：{prompt!r}"
            if "web_search" in tools and tools & {"read_file", "list_files", "write_file"}:
                assert prompt.index(search_notice) < prompt.index("文件用法："), \
                    f"{role}：搜索说明必须排在文件用法之前（文件用法留在结尾）：{prompt!r}"
        assert writable_roles and readonly_roles, (
            "可写版与只读版都必须在 roles.json 里被真实用到，否则本用例会静默只覆盖一档："
            f"可写 {writable_roles} 个 / 只读 {readonly_roles} 个")
        assert search_roles and no_search_roles, (
            "含 web_search 与不含 web_search 两档都必须在 roles.json 里被真实用到，"
            f"否则这一维会静默只覆盖一档：含 {search_roles} 个 / 不含 {no_search_roles} 个")
        assert get_prompt("不存在的角色") == "", "未知角色无工具集可依据，应仍返回空串"

    # ⑥ 人设进化过：两类文案都仍追加，且不改动人设本身
    evolved = "（这是进化后的人设文本）"
    with patch("prompts.role_prompts.PromptStore.get_active_prompt", return_value=evolved):
        for role in get_all_roles():
            prompt = get_prompt(role)
            assert prompt.startswith(evolved), \
                f"{role}：取到活跃版本时不该改动人设本身：{prompt!r}"
            assert "专属目录" in prompt and "公共书架" in prompt, \
                f"{role}：人设进化后边界文案仍应追加（否则它会随进化静默消失）：{prompt!r}"
            if "web_search" in set(get_role_tools(role)):
                assert search_notice in prompt, \
                    f"{role}：人设进化后搜索用法仍应追加：{prompt!r}"
    print(f"[OK] get_prompt 追加用法说明：可写 {writable_roles} / 只读 {readonly_roles} / "
          f"无文件工具 {no_file_roles}（该档 roles.json 现无角色，未覆盖：硬造一个角色名会"
          f"同时改配置，本批不做）/ 含搜索 {search_roles} / 不含搜索 {no_search_roles} 个角色")


if __name__ == "__main__":
    print("=== role prompts test ===\n")
    test_roles_schema()
    test_load_default_prompt_non_empty()
    test_get_role_tools_available()
    test_get_prompt_appends_file_usage_notice()
    print("\n=== ALL TESTS PASSED ===")
