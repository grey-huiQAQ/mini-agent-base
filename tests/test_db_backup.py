# -*- coding: utf-8 -*-
"""
数据库快照备份（P4-14 子项 ①）测试 —— 全部离线，走临时库隔离

覆盖：
- create_snapshot：产出文件存在且 size > 0；源库不存在时返回 None
- restore_snapshot：**真删真恢复** —— 删掉临时库后用快照恢复，逐行比对
  （行数 / MAX(rowid) / 抽样表逐行内容），并断言原库被留底为 .pre_restore_*
- list_snapshots：按时间倒序；目录不存在返回空列表
- prune_snapshots：造 5 份、keep=2 → 只剩最新 2 份；keep<1 不把最近一份删掉
- 失败静默降级：源库不存在 / dest_dir 是不可写路径 → 返回 None，不抛异常
- maybe_startup_snapshot：6 小时节流（新鲜快照存在时跳过）

隔离：走 tests/_test_db_helpers.setup_test_db()（临时库），并显式断言
DB_PATH 落在临时目录；**绝不触碰 data/memory.db**。
"""
import os
import sqlite3
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 统一测试数据库隔离（须在导入任何访问数据库的模块之前）----
from tests._test_db_helpers import setup_test_db, cleanup_test_db

_TMP_DIR = setup_test_db()

import memory.database as _database_module  # noqa: E402
from memory.database import init_db, get_connection  # noqa: E402
from core import db_backup  # noqa: E402

_TMP_DB = os.path.join(_TMP_DIR, "test_memory.db")


def setup():
    """初始化独立测试数据库（建表 + 迁移）"""
    init_db()


def teardown():
    """清理：恢复 DB_PATH 并删除本次测试产生的快照目录与留底文件"""
    cleanup_test_db()


def _seed(conn):
    """往临时库塞入已知内容：3 行 opportunities + 2 行 user_profile 语义外的表

    为了让"逐行内容一致"的断言有意义，故意写入非平凡数据（中文/空值混合）。
    """
    conn.execute("DELETE FROM opportunities")
    conn.execute(
        "INSERT INTO opportunities (title, source_url, summary, deadline, status)"
        " VALUES (?,?,?,?,?)",
        ("快照测试-甲", "https://snap.example.com/a", "摘要甲", "2026-09-20", "new"))
    conn.execute(
        "INSERT INTO opportunities (title, source_url, summary, deadline, status)"
        " VALUES (?,?,?,?,?)",
        ("快照测试-乙", "https://snap.example.com/b", "", None, "ignored"))
    conn.execute(
        "INSERT INTO opportunities (title, source_url, summary, deadline, status)"
        " VALUES (?,?,?,?,?)",
        ("快照测试-丙", "https://snap.example.com/c", "摘要丙", "2026-12-31", "") )
    conn.commit()


def _dump_opportunities(conn):
    """把 opportunities 逐行取成可比较的元组列表（按 id 排序）"""
    rows = conn.execute(
        "SELECT id, title, source_url, summary, deadline, status"
        " FROM opportunities ORDER BY id").fetchall()
    return [tuple(r) for r in rows]


def _fresh_dir(name):
    """在临时目录下建一个空的子目录（避免不同用例互相看到彼此的快照）"""
    d = os.path.join(_TMP_DIR, name)
    if os.path.isdir(d):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    os.makedirs(d, exist_ok=True)
    return d


# ---------- 1. 创建快照 ----------

def test_create_snapshot_makes_file():
    """create_snapshot：产出文件存在且 size > 0"""
    assert _database_module.DB_PATH == _TMP_DB, "应已切换到临时测试数据库"
    conn = get_connection()
    try:
        _seed(conn)
    finally:
        conn.close()

    dest = _fresh_dir("snap_create")
    path = db_backup.create_snapshot(db_path=_TMP_DB, dest_dir=dest)
    assert path, f"应返回快照路径：{path!r}"
    assert os.path.exists(path), path
    assert os.path.getsize(path) > 0, "快照文件不应为空"
    assert os.path.basename(path).startswith("memory_"), path
    assert os.path.basename(path).endswith(".db"), path

    # 快照本身是一个可打开的合法库，且内容与源库一致
    snap = sqlite3.connect(path)
    try:
        n = snap.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert n == 3, f"快照应含 3 行：{n}"
    finally:
        snap.close()
    print("[OK] create snapshot makes non-empty file")


def test_create_snapshot_missing_source_returns_none():
    """源库不存在 → 返回 None，不抛异常（备份失败不阻塞调用方）"""
    dest = _fresh_dir("snap_missing")
    missing = os.path.join(_TMP_DIR, "no_such_db.db")
    assert not os.path.exists(missing)
    assert db_backup.create_snapshot(db_path=missing, dest_dir=dest) is None
    print("[OK] create snapshot on missing source returns None")


def test_create_snapshot_bad_dest_returns_none():
    """目标目录不可创建（被同名文件占位）→ 返回 None，不抛异常"""
    dest = os.path.join(_TMP_DIR, "snap_bad_dest")
    # 用同名**文件**占位，令 makedirs 失败
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            os.remove(os.path.join(dest, f))
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("占位，使 makedirs 失败")
    try:
        assert db_backup.create_snapshot(db_path=_TMP_DB, dest_dir=dest) is None
    finally:
        os.remove(dest)
    print("[OK] create snapshot on bad dest returns None")


# ---------- 2. 恢复（本子项的全部意义） ----------

def test_restore_snapshot_really_restores_rows():
    """真删真恢复：删库 → 恢复 → 行数 / MAX(rowid) / 逐行内容全部一致"""
    conn = get_connection()
    try:
        _seed(conn)
        before_rows = _dump_opportunities(conn)
        before_count = len(before_rows)
        before_max_id = conn.execute(
            "SELECT MAX(id) FROM opportunities").fetchone()[0]
    finally:
        conn.close()
    assert before_count == 3, before_count

    dest = _fresh_dir("snap_restore")
    snap = db_backup.create_snapshot(db_path=_TMP_DB, dest_dir=dest)
    assert snap and os.path.exists(snap)

    # 真删：把临时库文件删掉（不是清表——要验证的是"文件级恢复"）
    os.remove(_TMP_DB)
    assert not os.path.exists(_TMP_DB), "临时库应已被删除"

    db_backup.restore_snapshot(snap, db_path=_TMP_DB)
    assert os.path.exists(_TMP_DB), "恢复后临时库文件应重新出现"

    conn = sqlite3.connect(_TMP_DB)
    conn.row_factory = sqlite3.Row
    try:
        after_rows = _dump_opportunities(conn)
        after_max_id = conn.execute(
            "SELECT MAX(id) FROM opportunities").fetchone()[0]
    finally:
        conn.close()

    assert len(after_rows) == before_count, (len(after_rows), before_count)
    assert after_max_id == before_max_id, (after_max_id, before_max_id)
    assert after_rows == before_rows, "逐行内容必须完全一致"

    # 本场景是"目标库已被删"（首次恢复/灾难场景），故**不应**有 .pre_restore_*
    # 留底——留底只在"目标库已存在"时才产生，那由下一条用例专门验证。
    leftovers = [n for n in os.listdir(_TMP_DIR)
                 if n.startswith("test_memory.db.pre_restore_")]
    assert not leftovers, f"目标库不存在时不该产生留底：{leftovers}"
    print("[OK] restore snapshot really restores rows")


def test_restore_snapshot_keeps_pre_restore_copy():
    """目标库**已存在**时恢复 → 先把原库留底为 .pre_restore_<ts>，再写入快照

    这是 `restore_snapshot` 唯一有破坏性的部分，也是"自留一手"的验证点：
    原库内容必须还能在被改名后的文件里找到。
    """
    conn = get_connection()
    try:
        _seed(conn)
        original_rows = _dump_opportunities(conn)
    finally:
        conn.close()

    dest = _fresh_dir("snap_prerestore")
    snap = db_backup.create_snapshot(db_path=_TMP_DB, dest_dir=dest)
    assert snap

    # 让目标库"变脏"：删掉一行，使恢复前后可区分
    conn = sqlite3.connect(_TMP_DB)
    try:
        conn.execute("DELETE FROM opportunities WHERE title = '快照测试-乙'")
        conn.commit()
        dirty_count = conn.execute(
            "SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        conn.close()
    assert dirty_count == 2, dirty_count

    db_backup.restore_snapshot(snap, db_path=_TMP_DB)

    # 1) 目标库已恢复为快照内容（3 行）
    conn = sqlite3.connect(_TMP_DB)
    try:
        restored = _dump_opportunities(conn)
    finally:
        conn.close()
    assert restored == original_rows, "恢复后应与快照一致"

    # 2) 原库（脏版本，2 行）必须还能在留底文件里读到
    leftovers = sorted(n for n in os.listdir(_TMP_DIR)
                       if n.startswith("test_memory.db.pre_restore_"))
    assert leftovers, f"应产生 .pre_restore_* 留底：{os.listdir(_TMP_DIR)}"
    kept = os.path.join(_TMP_DIR, leftovers[-1])
    conn = sqlite3.connect(kept)
    try:
        kept_count = conn.execute(
            "SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        conn.close()
    assert kept_count == dirty_count, \
        f"留底应保存恢复前的脏库（{dirty_count} 行），实际 {kept_count} 行"
    print("[OK] restore snapshot keeps pre-restore copy")


def test_restore_snapshot_missing_file_raises():
    """快照不存在 → FileNotFoundError（唯一不吞异常的函数，调用方需知道）"""
    missing = os.path.join(_TMP_DIR, "snap_restore", "nope.db")
    try:
        db_backup.restore_snapshot(missing, db_path=_TMP_DB)
        assert False, "应抛 FileNotFoundError"
    except FileNotFoundError:
        pass
    print("[OK] restore snapshot missing file raises")


# ---------- 3. 列出与清理 ----------

def test_list_snapshots_newest_first_and_empty_dir():
    """list_snapshots：按时间倒序；目录不存在返回空列表"""
    dest = _fresh_dir("snap_list")
    assert db_backup.list_snapshots(dest) == []
    # 目录不存在
    assert db_backup.list_snapshots(os.path.join(_TMP_DIR, "no_such_dir")) == []

    # 造 3 份，mtime 递增（显式 set mtime，避免同秒产出导致顺序不确定）
    made = []
    for i in range(3):
        p = os.path.join(dest, f"memory_2026010{i + 1}_000000.db")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(p, (1000 + i, 1000 + i))     # mtime 递增
        made.append(p)
    got = db_backup.list_snapshots(dest)
    assert [os.path.basename(p) for p in got] == [
        "memory_20260103_000000.db",
        "memory_20260102_000000.db",
        "memory_20260101_000000.db"], got
    # 非约定命名不入列
    with open(os.path.join(dest, "other.db"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert len(db_backup.list_snapshots(dest)) == 3
    print("[OK] list snapshots newest first + empty dir")


def test_prune_snapshots_keeps_newest():
    """prune_snapshots(keep=2)：造 5 份 → 只剩最新 2 份"""
    dest = _fresh_dir("snap_prune")
    for i in range(5):
        p = os.path.join(dest, f"memory_2026020{i + 1}_000000.db")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(p, (2000 + i, 2000 + i))

    assert len(db_backup.list_snapshots(dest)) == 5
    deleted = db_backup.prune_snapshots(keep=2, dest_dir=dest)
    assert deleted == 3, f"应删除 3 份：{deleted}"
    left = [os.path.basename(p) for p in db_backup.list_snapshots(dest)]
    assert left == ["memory_20260205_000000.db",
                    "memory_20260204_000000.db"], left

    # keep < 1 归一为 1：只保留最新一份（**不得把最近一份也删掉**）
    assert db_backup.prune_snapshots(keep=0, dest_dir=dest) == 1, \
        "keep=0 应归一为 1，即删掉除最新一份以外的 1 份"
    left2 = [os.path.basename(p) for p in db_backup.list_snapshots(dest)]
    assert left2 == ["memory_20260205_000000.db"], \
        f"最新一份必须留下：{left2}"
    # 目录不存在：返回 0，不抛异常
    assert db_backup.prune_snapshots(keep=2,
                                     dest_dir=os.path.join(_TMP_DIR, "nope")) == 0
    print("[OK] prune snapshots keeps newest")


# ---------- 4. 启动期节流 ----------

def test_maybe_startup_snapshot_throttled():
    """6 小时节流：新鲜快照存在时跳过；过期后才会新建"""
    dest = _fresh_dir("snap_throttle")
    # 先造一份"很新"的快照 → 应被节流跳过
    p1 = db_backup.create_snapshot(db_path=_TMP_DB, dest_dir=dest)
    assert p1
    assert db_backup.maybe_startup_snapshot(
        db_path=_TMP_DB, dest_dir=dest, min_interval_sec=6 * 3600) is None, \
        "6 小时内不应再次快照"
    assert len(db_backup.list_snapshots(dest)) == 1

    # 把唯一一份的 mtime 改到 7 小时前 → 应新建
    old = time.time() - 7 * 3600
    os.utime(p1, (old, old))
    p2 = db_backup.maybe_startup_snapshot(
        db_path=_TMP_DB, dest_dir=dest, min_interval_sec=6 * 3600, keep=7)
    assert p2, "超过节流窗口后应新建快照"
    assert os.path.exists(p2), p2
    # 不断言"恰好 2 份"：文件名是秒级时间戳，本用例两次快照很可能落在同一秒
    #   （实测会发生），此时后者刷新同一路径而非新增一份——这是 `_snapshot_name`
    #   注释里写明的既有行为，不是缺陷。这里只断言"新建发生了且文件在"。
    assert len(db_backup.list_snapshots(dest)) >= 1
    print("[OK] startup snapshot throttled by 6h window")


def test_maybe_startup_snapshot_survives_failure():
    """启动期备份失败绝不向上抛（否则应用起不来）"""
    dest = os.path.join(_TMP_DIR, "snap_startup_bad")
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            os.remove(os.path.join(dest, f))
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("占位")
    try:
        # 源库也不存在 → 双重失败，仍应返回 None 且不抛
        assert db_backup.maybe_startup_snapshot(
            db_path=os.path.join(_TMP_DIR, "no_such.db"),
            dest_dir=dest) is None
    finally:
        os.remove(dest)
    print("[OK] startup snapshot survives failure")


def test_cleanup():
    """清理测试产物（临时数据库目录）"""
    cleanup_test_db()
    print("[OK] test data cleanup")


if __name__ == "__main__":
    print("=== db backup test ===\n")
    setup()
    test_create_snapshot_makes_file()
    test_create_snapshot_missing_source_returns_none()
    test_create_snapshot_bad_dest_returns_none()
    test_restore_snapshot_really_restores_rows()
    test_restore_snapshot_keeps_pre_restore_copy()
    test_restore_snapshot_missing_file_raises()
    test_list_snapshots_newest_first_and_empty_dir()
    test_prune_snapshots_keeps_newest()
    test_maybe_startup_snapshot_throttled()
    test_maybe_startup_snapshot_survives_failure()
    test_cleanup()
    print("\n=== ALL DB BACKUP TESTS PASSED ===")
