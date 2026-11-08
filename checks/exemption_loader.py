#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豁免台账解析（checks/exemption_loader.py）

用途：
    五个检查器共用的台账读取入口。P4-5c 起台账支持**对象格式**，让豁免从
    "放行"升级成"论证"：

        [{"name": "SkillImpl",           # 或 "key"（按位置/文件名等键语义）
          "reason": "skills 接口契约，加载器按类名反射",   # 为什么可以放行
          "since": "2026-09-06"}]                        # 何时登记

    裸字符串仍可读取（兼容旧台账），但会打印 [EXEMPTION_NO_REASON] 并以**退出码 1**
     终止：写不出理由的豁免无法被质疑，只能被整体删或整体留。

     退出码分工（别混）：**1 = 违规**（存在无理由豁免，P5-1 起生效）；
     **2 = 格式非法 fail-closed**（台账不是数组 / 条目类型不对 / 缺 name|key），见下方。

设计说明（为什么是共享模块而不是各检查器各写一份）：
    本项目已两次被"两处手工复制保持一致"咬到（`_extract_terms` 在 note_store 与
    experience_store 的副本、`load_and_split` 与 `_chunk_text` 的同算法异名）。
    台账解析正是最容易漂移的那类"共享实现"，故只有一个载体。`checks/` 目录不在
    任何扫描器的扫描面内（SKIP_DIRS 含 checks），本模块不会被当成检查器或被
    重复名检查器统计。

fail-closed：
    台账不是数组 / 条目既非对象也非字符串 / 对象缺 name|key —— 一律以退出码 2
    终止，不静默放行（AGENTS.md 规范 4）。
"""

import sys


def collect_keys(data, source):
    """把台账条目解析为键集合；无理由豁免以退出码 1 终止，格式非法以退出码 2 终止

    两种失败要分清：
    - **退出码 1**（P5-1 起）：存在"无理由豁免"（裸字符串，或对象缺 reason/since）。
      这不是格式问题而是治理问题——写不出理由的豁免无法被质疑，只能被整体删或整体留。
      修复方式是把条目补成 {"name":..., "reason":..., "since":...} 对象。
    - **退出码 2**：台账格式非法（不是 JSON 数组 / 条目既非对象也非字符串 /
      对象缺 name|key），fail-closed 不静默放行（AGENTS.md 规范 4）。

    `source` 支持 Path 或 str（检查器传的是 Path）。

    **每个台账只打印一行汇总**（而非逐条打印）：P4-5c 落地时曾逐条打印，实测一本 26 条
    的台账会让每次提交刷 26 行噪声——门禁一旦吵起来就会被整体无视，那与"没有门禁"
    等价。汇总行里列出键名（≤5 条全列，>5 条列前 5 + "…详见台账"），
    否则失败信息不足以定位要改哪条。
    """
    src_name = getattr(source, "name", None) or str(source)

    def _fail_format(msg):
        """格式非法：打印并以退出码 2 终止（fail-closed）"""
        print(f"[ERROR] {msg}（{src_name}）", file=sys.stderr)
        sys.exit(2)

    if not isinstance(data, list):
        _fail_format("豁免台账必须是 JSON 数组")
    keys = set()
    no_reason = []
    for item in data:
        if isinstance(item, str):
            no_reason.append(item)
            keys.add(item)
            continue
        if not isinstance(item, dict):
            _fail_format(f"台账条目必须是对象或字符串：{item!r}")
        key = item.get("name") or item.get("key")
        if not isinstance(key, str) or not key:
            _fail_format(f"台账条目缺少 name/key 字段：{item!r}")
        if not item.get("reason") or not item.get("since"):
            no_reason.append(key)
        keys.add(key)
    if no_reason:
        shown = "、".join(no_reason[:5])
        if len(no_reason) > 5:
            shown += " …详见台账"
        print(
            f"[EXEMPTION_NO_REASON] {src_name}：{len(no_reason)}/{len(keys)} 条豁免"
            f"缺 reason/since，无理由豁免已按违规处理（P5-1 起）。待补条目：{shown}",
            file=sys.stderr,
        )
        sys.exit(1)
    return keys
