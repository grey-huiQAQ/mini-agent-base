# -*- coding: utf-8 -*-
"""
数据库快照备份（P4-14 子项 ①）

定位：为 `data/memory.db` 提供**可验证恢复**的轻量快照。三件事——
定时（应用启动时，带 6 小时节流）落一份快照、只保留最近若干份、以及
在需要时能从快照恢复。

为什么用 `sqlite3` 的在线备份 API 而不是复制文件：
    `shutil.copy` 复制一个**正在被写**的库文件会产出半截库（页写到一半），
    而备份的价值恰恰在于"崩了以后还能用"。`sqlite3.Connection.backup()` 是
    SQLite 官方在线备份接口，对源库加正确的锁、产出一致的副本。

为什么用独立连接：
    `memory/database.get_db()` 每次调用新开连接，但它的 `sqlite3.connect(DB_PATH)`
    未传 `timeout`（默认 5s）——那是**业务连接**的参数，本模块不去改它。
    备份自己开一条独立连接、用完即关，互不影响。

**失败语义（重要）**：`create_snapshot` / `prune_snapshots` 内部所有异常都被
吞掉，只打印一行 `[Backup]` 警告并返回 None / 0。备份是**辅助设施**，失败绝
不允许阻塞应用启动——用户宁可没有备份，也不能因为备份失败打不开应用。
`restore_snapshot` 是唯一有破坏性的函数，它**不吞异常**（调用方需要知道恢复
失败），但仍保证"原库先改名留底"这一手，失败可回退。
"""

import os
import sqlite3
import time
from datetime import datetime

#: 默认快照目录（`.gitignore` 已忽略 `data/backups/`，无需额外配置。
#: 刻意**不写行号**：`.gitignore` 加一行注释就会让"第 N 行"指向别人，
#: 而它指向错了也不会报错——这类跨文件行号引用是 AGENTS.md 执行纪律第 6 条
#: 点名的漂移来源，判据一律用"有没有这条规则"，不用"它在第几行"）
DEFAULT_BACKUP_DIR = os.path.join("data", "backups")

#: 默认保留份数。**"7" 不是拍脑袋**：本应用在周末机上一天开一次，7 份跨越
#: 数周的实际开机节奏；对照"每日保留 30 份"那种按日历天设计的策略，它在
#: "一天开一次"的机器上等价于"保留 30 次开机"，反而更长。
DEFAULT_KEEP = 7

#: 快照文件名前缀与后缀（解析时用于识别本模块产出的文件）
_SNAPSHOT_PREFIX = "memory_"
_SNAPSHOT_SUFFIX = ".db"


def _default_db_path() -> str:
    """当前业务库路径（延迟读 memory.database.DB_PATH，便于测试隔离后取到临时库）

    不在此处 import 成模块级常量：`memory.database.DB_PATH` 会被
    `tests/_test_db_helpers.setup_test_db()` 在导入后改写到临时目录，写成模块级
    常量会固化到生产路径。
    """
    from memory.database import DB_PATH
    return DB_PATH


def _snapshot_name(now=None) -> str:
    """快照文件名：memory_YYYYMMDD_HHMMSS.db（秒级定宽，字典序即时间序）"""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{_SNAPSHOT_PREFIX}{stamp}{_SNAPSHOT_SUFFIX}"


def create_snapshot(db_path=None, dest_dir=DEFAULT_BACKUP_DIR):
    """把业务库在线备份到 dest_dir，返回快照文件路径；任何失败返回 None

    Args:
        db_path: 源库路径（None 时取 memory.database.DB_PATH）
        dest_dir: 目标目录（不存在时创建）

    Returns:
        快照文件绝对/相对路径（成功）或 None（失败；已打印 [Backup] 警告）

    说明：文件名用"秒级时间戳"，同一秒内重复调用会得到**同一个路径**，此时
    SQLite 会直接在已存在的目标上重写（结果是刷新为同一份快照），而不是产出一份
    新文件。这是**已知且接受**的行为：同一秒内两次启动不是真实场景，而加唯一
    后缀会让文件名不可预测、也让 `list_snapshots` 的"定宽字典序即时间序"失效。
    （本模块的单测里因此不去断言"恰好 N 份"，只断言"新建发生了"。）
    """
    try:
        src_path = db_path or _default_db_path()
        if not os.path.exists(src_path):
            print(f"[Backup] 跳过：源库不存在 {src_path}")
            return None
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, _snapshot_name())
        # 官方在线备份 API：对正在被写的库也产出一致副本（不要用 shutil.copy）
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(dest_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        print(f"[Backup] 已创建快照：{dest_path}")
        return dest_path
    except Exception as e:      # 备份失败绝不允许阻塞调用方（如应用启动）
        print(f"[Backup] 创建快照失败（忽略）：{e}")
        return None


def list_snapshots(dest_dir=DEFAULT_BACKUP_DIR) -> list:
    """列出目录下的快照文件路径，按修改时间**倒序**（最新在前）

    只认本模块的命名约定（`memory_*.db`）；目录不存在 / 不可读时返回空列表。
    """
    try:
        if not os.path.isdir(dest_dir):
            return []
        names = [n for n in os.listdir(dest_dir)
                 if n.startswith(_SNAPSHOT_PREFIX) and n.endswith(_SNAPSHOT_SUFFIX)]
        paths = [os.path.join(dest_dir, n) for n in names]
        # 按 (mtime, 文件名) 排序：mtime 相同（同秒产出）时用文件名兜底，保证稳定
        paths.sort(key=lambda p: (os.path.getmtime(p), os.path.basename(p)),
                   reverse=True)
        return paths
    except Exception as e:
        print(f"[Backup] 列出快照失败（忽略）：{e}")
        return []


def prune_snapshots(keep=DEFAULT_KEEP, dest_dir=DEFAULT_BACKUP_DIR) -> int:
    """只保留最新的 keep 份快照，删除更旧的；返回**实际删除**的文件数

    Args:
        keep: 保留份数（<1 视为 1，避免把最近一份也删掉）
        dest_dir: 快照目录

    删除失败（文件被占用等）只记账为"未删除"，不影响其它文件，也不抛异常。
    """
    deleted = 0
    try:
        keep = max(1, int(keep))
        snaps = list_snapshots(dest_dir)      # 已按时间倒序
        for path in snaps[keep:]:             # 保留前 keep 份，其余删掉
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                print(f"[Backup] 删除旧快照失败（忽略）{os.path.basename(path)}：{e}")
        if deleted:
            print(f"[Backup] 已清理 {deleted} 份旧快照（保留最新 {keep} 份）")
        return deleted
    except Exception as e:
        print(f"[Backup] 清理快照失败（忽略）：{e}")
        return deleted


def restore_snapshot(snapshot_path, db_path=None) -> None:
    """用快照恢复业务库；**恢复前先把目标库改名为 <name>.pre_restore_<ts>**

    这是本模块唯一有破坏性的函数，故强制"自留一手"：先把现有库改名留底，
    再写入快照内容。改名之后若写入失败，原库仍在 `<...>.pre_restore_<ts>`
    处，可手工改回。

    Args:
        snapshot_path: 快照文件路径
        db_path: 目标库路径（None 时取 memory.database.DB_PATH）

    Raises:
        FileNotFoundError: 快照不存在
        sqlite3.Error / OSError: 恢复过程出错（**不吞**——调用方必须知道）
    """
    if not os.path.exists(snapshot_path):
        raise FileNotFoundError(f"快照不存在：{snapshot_path}")
    target = db_path or _default_db_path()
    # 1) 原库改名留底（存在才改名；首次建库时没有原库）
    if os.path.exists(target):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_of_old = f"{target}.pre_restore_{stamp}"
        os.replace(target, backup_of_old)
        print(f"[Backup] 原库已留底：{backup_of_old}")
    else:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
    # 2) 从快照读出、写进目标库（同样走在线备份 API，保证写到一半也有原库兜底）
    #    注意：不用 shutil.copy——改为从快照连接 backup() 到新目标，
    #    这样即便快照本身被并发写（不应发生）也保持一致读。
    src = sqlite3.connect(snapshot_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"[Backup] 已从快照恢复：{snapshot_path} -> {target}")


def maybe_startup_snapshot(db_path=None, dest_dir=DEFAULT_BACKUP_DIR,
                           keep=DEFAULT_KEEP, min_interval_sec=6 * 3600):
    """应用启动期的节流快照：最新一份距今不足 min_interval_sec 就跳过

    Args:
        min_interval_sec: 节流窗口（默认 6 小时）

    Returns:
        本次新建的快照路径；被节流跳过或失败时返回 None

    说明（为什么节流）：周末机一天开一次，节流实际不生效；它的真正作用是防止
    将来长跑时每次 reload / 重开都产一份文件。
    """
    try:
        snaps = list_snapshots(dest_dir)
        if snaps:
            age = time.time() - os.path.getmtime(snaps[0])
            if age < float(min_interval_sec):
                print(f"[Backup] 距上次快照 {age / 3600:.1f} 小时，本次跳过")
                return None
        path = create_snapshot(db_path=db_path, dest_dir=dest_dir)
        if path:
            prune_snapshots(keep=keep, dest_dir=dest_dir)
        return path
    except Exception as e:      # 启动期绝不允许因备份失败而中断
        print(f"[Backup] 启动快照失败（忽略）：{e}")
        return None
