# -*- coding: utf-8 -*-
"""
▶️ 任务生命周期（P7 阶段二 组 3，2026-10-18）

被测的是 `ui/controllers/goals_controller.py::do_start_task` 与 `ui/app_factory.py`
里那一枚新按钮「▶️ 开始任务」。设计席 组 3 的判据与边界逐条对上用例：

- **判据「目标页能『开始』一条 pending → 状态变 in_progress」** →
  `test_start_task_moves_pending_to_in_progress`（回库核对状态列，不只看返回文案）＋
  `test_start_task_returns_detail_as_second_element`（第二元必须是**写之后**刷出来的详情，
  绑定声明的那两个 output 才有意义）；
- **边界「不改待办准入」** → `test_started_task_stays_in_the_goal_page_dropdown`
  （开始后这条任务仍留在目标页那个共用下拉里——下拉的准入是「非 done」，
  「开始」不许把它挪出准入，也不许把 in_progress 变成一档新准入）；
- **边界「只走既有写路径、不动表结构」** →
  `test_start_writes_only_the_selected_tasks_status_column`（全行对拍：只有被点那一行的
  `status` 变，同目标另一行与 `goals` 表整行一字不变）；
- **既有形状不许被新按钮带跑** → `test_empty_selection_or_bad_id_writes_nothing`
  （空选／非数字 id 两档都照 `do_complete_task` 的措辞拒绝，且一格不写）＋
  `test_complete_task_still_marks_done_after_start`（开始→完成这条旧链走通，完成后离开下拉）；
- **形状要钉（界面上真的接上了）** → `test_start_button_is_declared_and_bound`
  （静态读 `ui/app_factory.py`：按钮声明在那枚共用下拉之后，`click` 接的是
  `_app_module.do_start_task`、inputs 用的就是那个下拉、outputs 是两个既有组件，
  且后面跟着 `.then(refresh_goal_detail)`——口径照 `tests/test_home_dashboard.py`
  的绑定用例，刻意不重跑 `create_app()`）＋ `test_app_py_reexports_the_same_object`。

⚠️ **负控四项的形状**（逐项改坏→红在哪条→字节级还原，见执行席回报）：摘掉按钮 ⇒ 红在静态那条；
把写入状态改回 `pending` ⇒ 红在库状态＋详情图标两条；把 `click` 的 fn 换成 `do_complete_task`
⇒ 红在静态那条；让第二元返回空串 ⇒ 红在两元组那条。

⚠️ 导入顺序即隔离顺序：`ui/controllers/_shared.py` 在**导入期**调 `init_db()`，
所以必须先 `setup_test_db()` 再碰任何 UI/core 模块，否则本文件一 import 就往生产库建表。
⚠️ 中文串里一律用「」而不用 ASCII 双引号。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 导入顺序即隔离顺序：先切库，再碰任何会访问数据库的模块 ----
from tests._test_db_helpers import setup_test_db, cleanup_test_db  # noqa: E402

_TMP_DIR = setup_test_db()

import memory.database as md  # noqa: E402
from ui.controllers import goals_controller as gc  # noqa: E402
import app as app_module  # noqa: E402  （导入即 create_app()，新按钮能建起来就是判据的冒烟）

md.init_db()

_FACTORY_SOURCE = os.path.join("ui", "app_factory.py")

#: 「开始」之后详情那一行该出现的状态图标，**逐字抄自** `goals_controller.refresh_goal_detail`
#: 里那张函数内局部的 `status_icon` 表，在测试里另写一份。
#: 刻意不从实现推：这一条要防的正是「库里写着 in_progress，界面上却还是 ⬜」——
#: 用实现推实现等于没校验（同 `tests/test_home_dashboard.py` 那两个下拉标签的理由）。
_IN_PROGRESS_ICON = "🔄"
_PENDING_ICON = "⬜"

#: 按钮的显示文本，同样逐字抄自 `ui/app_factory.py`。
_START_BUTTON_LABEL = "▶️ 开始任务"


def _make_goal(title: str) -> str:
    return gc.goal_manager.create_goal(title, "任务生命周期夹具目标").goal_id


def _make_task(goal_id: str, title: str, status: str = "pending") -> int:
    """建一条任务并可选地直接把状态写进去（夹具走 SQL，不借被测试的按钮）"""
    gc.goal_manager.add_task(goal_id, title)
    conn = md.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM goal_tasks WHERE goal_id = ? AND title = ? ORDER BY id DESC",
            (goal_id, title)).fetchone()
        assert row is not None, f"add_task 没落库：{title}"
        task_id = int(row[0])
        if status != "pending":
            conn.execute("UPDATE goal_tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id


def _task_row(task_id: int) -> dict:
    conn = md.get_connection()
    try:
        row = conn.execute("SELECT * FROM goal_tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None, f"任务行不见了：{task_id}"
        return dict(row)
    finally:
        conn.close()


def _db_state() -> dict:
    """逐表 (行数, MAX(rowid)) ＋ goals／goal_tasks 两张表的**整行内容**

    只数行数抓不到原地 UPDATE，所以这两张表按整行取回；「开始」这一格动的正是其中一行
    的 `status`，形状上必须能被这份状态表分辨出来。
    """
    conn = md.get_connection()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        out: dict = {}
        for table in tables:
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            max_rowid = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
            out[table] = (count, int(max_rowid))
        for table in ("goals", "goal_tasks"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[f"{table}:rows"] = repr([tuple(r) for r in rows])
        return out
    finally:
        conn.close()


def _state_diff(before: dict, after: dict) -> list:
    return [f"{key}: {before.get(key)!r} → {after.get(key)!r}"
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)]


def _read_factory_source() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        _FACTORY_SOURCE)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _button_declarations() -> dict:
    """按变量名把三枚按钮的**显示文本**与出现位置从源码里抽出来

    用正则而不是逐字匹配：排版（缩进、换行、`size=` 的先后）随时会被一次格式化改动，
    而这一条要防的是**文案漂移**——按钮不叫那个名字了、或整枚按钮没了，都必须红。
    显示文本逐字抄在 `_START_BUTTON_LABEL`，刻意不从实现推（同本文件头部那段理由）。
    """
    src = _read_factory_source()
    out: dict = {}
    for var in ("start_task_btn", "complete_task_btn"):
        match = re.search(re.escape(var) + r"\s*=\s*gr\.Button\(\s*\"([^\"]*)\"", src)
        out[var] = (match.group(1) if match else None,
                    match.start() if match else None)
    match = re.search(r"complete_task_dropdown\s*=\s*gr\.Dropdown\(", src)
    out["complete_task_dropdown"] = (None, match.start() if match else None)
    return out


def _normalized_factory_source() -> str:
    """把 `ui/app_factory.py` 的空白全部压掉，只用于比对**绑定形状**（不用于文案）"""
    return re.sub(r"\s+", "", _read_factory_source())


def test_start_task_moves_pending_to_in_progress():
    """判据本体：点「开始」之后，库里那一行真的从 pending 变成 in_progress，详情也跟着换图标"""
    goal_id = _make_goal("开始一条待办")
    task_id = _make_task(goal_id, "要被开始的待办")
    assert _task_row(task_id)["status"] == "pending", "夹具没能造出 pending"

    status_md, detail_md = gc.do_start_task(str(task_id), goal_id)

    row = _task_row(task_id)
    assert row["status"] == "in_progress", f"状态没落库：{row}"
    assert "进行中" in status_md, f"状态文案没说出「进行中」：{status_md}"
    assert f"{_IN_PROGRESS_ICON} **要被开始的待办**" in detail_md, (
        f"详情那一行还是旧图标（库里已 in_progress、界面上看不出差别）：{detail_md[:400]}")
    print("[OK] 目标页「开始」把 pending 落成 in_progress，详情行同步换成进行中图标")


def test_start_task_returns_detail_as_second_element():
    """绑定的两个 output 站得住：第二元必须是**写之后**刷出来的详情，不是空串也不是旧详情"""
    goal_id = _make_goal("检查两元组")
    task_id = _make_task(goal_id, "两元组里的任务")

    returned = gc.do_start_task(str(task_id), goal_id)
    assert isinstance(returned, tuple) and len(returned) == 2, (
        f"返回不是两元组（app_factory 那两个 output 就对不上）：{type(returned)}")
    detail_after = gc.refresh_goal_detail(goal_id)[0]
    assert returned[1] == detail_after, (
        "第二元不是写之后刷出来的详情——它若是写之前的旧详情，界面会停在 ⬜ 那一行")
    assert _task_row(task_id)["status"] == "in_progress"
    print("[OK] 返回两元组、第二元等于写后详情，状态已落库")


def test_empty_selection_or_bad_id_writes_nothing():
    """空选与非数字 id 都按既有措辞拒绝，且一格不写（形状照 do_complete_task 的两档）"""
    goal_id = _make_goal("拒绝无效输入")
    task_id = _make_task(goal_id, "不该被动的那一条")
    before = _db_state()

    for bad in ("", None):
        status_md, detail_md = gc.do_start_task(bad, goal_id)
        assert "请先选择要开始的任务" in status_md, f"空选的措辞不对：{status_md}"
        assert detail_md == "", f"空选还回了详情：{detail_md[:80]!r}"
    for bad in ("abc", "1.5"):
        status_md, detail_md = gc.do_start_task(bad, goal_id)
        assert "无效的任务 ID" in status_md, f"坏 id 的措辞不对：{status_md}"
        assert detail_md == "", f"坏 id 还回了详情：{detail_md[:80]!r}"

    diff = _state_diff(before, _db_state())
    assert not diff, f"无效输入写了库：{diff}"
    assert _task_row(task_id)["status"] == "pending", "无效输入把那条改掉了"
    print("[OK] 空选／None／非数字三种输入都拒了，逐表状态一字不变")


def test_start_writes_only_the_selected_tasks_status_column():
    """同目标两条任务，点其中一条 ⇒ 全行对拍只有那一行的 status 变，goals 整行不变"""
    goal_id = _make_goal("只动这一行")
    first_id = _make_task(goal_id, "被点的那一条")
    second_id = _make_task(goal_id, "没被点的那一条")
    before = _db_state()
    before_first = _task_row(first_id)
    before_second = _task_row(second_id)

    gc.do_start_task(str(first_id), goal_id)

    after_first = _task_row(first_id)
    after_second = _task_row(second_id)
    changed = [k for k in after_first if after_first[k] != before_first[k]]
    assert changed == ["status"], f"被点那一行变了不止 status：{changed}"
    assert after_first["status"] == "in_progress"
    assert after_second == before_second, (
        f"没被点的那一条也被改了：{[k for k in after_second if after_second[k] != before_second[k]]}")

    diff = _state_diff(before, _db_state())
    assert all(k.startswith("goal_tasks:rows") for k in diff), (
        f"除了 goal_tasks 的行内容，还有别的表动了：{diff}")
    conn = md.get_connection()
    try:
        goal_rows = conn.execute("SELECT * FROM goals ORDER BY rowid").fetchall()
    finally:
        conn.close()
    assert repr([tuple(r) for r in goal_rows]) == before["goals:rows"], (
        "「开始」把 goals 表也写了——它只该动 goal_tasks 那一行")
    print("[OK] 只有被点那一行的 status 变化，另一行与 goals 整行一字不变")


def test_started_task_stays_in_the_goal_page_dropdown():
    """边界「不改待办准入」：开始后这条仍在下拉里（下拉认的是「非 done」）"""
    goal_id = _make_goal("准入不变")
    task_id = _make_task(goal_id, "开始后仍可选的任务")
    done_id = _make_task(goal_id, "已经完成的旧任务", status="done")

    gc.do_start_task(str(task_id), goal_id)

    _detail, choices, _update = gc.refresh_goal_detail(goal_id)
    values = [value for _label, value in choices]
    assert str(task_id) in values, f"开始后反而从下拉里消失了：{choices}"
    assert str(done_id) not in values, f"done 的任务混进了下拉：{choices}"
    print("[OK] in_progress 仍留在目标页下拉里，done 仍在门外——准入没被这批动过")


def test_complete_task_still_marks_done_after_start():
    """旧链走完：开始 → 完成，完成后这条离开下拉；新按钮没有改变「完成」那一格的语义"""
    goal_id = _make_goal("先开始后完成")
    task_id = _make_task(goal_id, "走完整个生命周期")

    gc.do_start_task(str(task_id), goal_id)
    status_md, _detail = gc.do_complete_task(str(task_id), goal_id)
    assert _task_row(task_id)["status"] == "done", f"完成没落库：{status_md}"

    _detail2, choices, _update = gc.refresh_goal_detail(goal_id)
    assert str(task_id) not in [v for _l, v in choices], (
        f"done 的任务还占着下拉：{choices}")
    print("[OK] 开始→完成这条链走通，完成后离开准入集合")


def test_start_button_is_declared_and_bound():
    """静态钉形状：按钮声明在共用下拉之后，click 接 do_start_task、读那个下拉、写那两个 output"""
    decls = _button_declarations()
    start_label, start_pos = decls["start_task_btn"]
    assert start_label == _START_BUTTON_LABEL, (
        f"目标页那枚「开始」按钮的文案不是用例抄的这一份（或整枚按钮被删了）：{start_label!r}")
    assert decls["complete_task_btn"][0] == "✅ 标记任务完成", (
        f"「标记完成」那枚按钮被这批改动了：{decls['complete_task_btn'][0]!r}")
    # 声明顺序：共用下拉 → 开始 → 完成（排到别处会在界面上把两个动作隔开）
    dropdown_pos = decls["complete_task_dropdown"][1]
    complete_pos = decls["complete_task_btn"][1]
    assert dropdown_pos is not None and dropdown_pos < start_pos < complete_pos, (
        f"按钮声明位置不对：下拉 {dropdown_pos}／开始 {start_pos}／完成 {complete_pos}")

    src = _normalized_factory_source()
    binding = (
        'start_task_btn.click(fn=_app_module.do_start_task,'
        'inputs=[complete_task_dropdown,goal_dropdown],'
        'outputs=[goal_action_status,goal_detail],).then('
        'fn=_app_module.refresh_goal_detail,'
        'inputs=[goal_dropdown],'
        'outputs=[goal_detail,task_choices_state,complete_task_dropdown],)'
    )
    assert binding in src, (
        "「开始」按钮的绑定形状不对：它必须接 do_start_task、读那枚共用下拉与目标下拉、"
        "写状态栏与详情，再 .then 刷详情（口径与「标记完成」那条一字不差地同形）")
    print("[OK] 按钮文案与声明位置、click 绑定形状都在，且复用既有的下拉／状态栏／详情")


def test_app_py_reexports_the_same_object():
    """app.py 薄壳必须 re-export 同一个对象（app_factory 经 _app_module.do_start_task 绑定）"""
    assert hasattr(app_module, "do_start_task"), (
        "app.py 没有 re-export do_start_task——app_factory 里那句 "
        "_app_module.do_start_task 会在建界面时才炸")
    assert app_module.do_start_task is gc.do_start_task, (
        "app.py 导出的不是 goals_controller 里那一个函数（两份实现迟早会说不一样的话）")
    print("[OK] app.py 导出的就是控制器里那一个函数对象")


TESTS = [
    test_start_task_moves_pending_to_in_progress,
    test_start_task_returns_detail_as_second_element,
    test_empty_selection_or_bad_id_writes_nothing,
    test_start_writes_only_the_selected_tasks_status_column,
    test_started_task_stays_in_the_goal_page_dropdown,
    test_complete_task_still_marks_done_after_start,
    test_start_button_is_declared_and_bound,
    test_app_py_reexports_the_same_object,
]


def main():
    try:
        for fn in TESTS:
            fn()
    finally:
        cleanup_test_db()


if __name__ == "__main__":
    main()
