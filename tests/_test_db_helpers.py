# -*- coding: utf-8 -*-
"""
统一测试数据库隔离 helper（P0-5）

用途：为曾经直接使用生产 data/memory.db 的遗留测试文件提供一键式临时库隔离，
避免回归测试在真实用户数据上建表/迁移/写测试行。

用法（在 sys.path.insert(...) 之后、任何导入 memory.database 或会访问数据库的
业务模块之前，调用一次即可）：

    from tests._test_db_helpers import setup_test_db
    setup_test_db()

注意：
- 此隔离仅限数据库，不隔离文件系统（如 data/goals/、data/files/、data/conversations/
  等目录）；涉及文件产物的测试仍需自行精确清理自身创建的目录与文件；
- 临时库放在项目 data/ 下（_test_tmp_ 前缀），与业务库 data/memory.db 同盘隔离，
  避免依赖系统临时目录（部分运行环境下其子目录不可写）；
- 清理带重试：Windows 实时杀软可能瞬时锁定新建的 db 文件导致 rmtree 失败，
  删除失败会短暂重试；进程异常被杀（如回归超时看门狗）留下的残留目录，
  由后续运行的 setup_test_db 自愈清扫（仅清理超过 5 分钟的老目录，避免误删
  并行运行中的其它测试进程的临时库）。
- 自检（P4-3b 新增）：setup_test_db() 在返回前自行核对 DB_PATH 是否真的落在
  临时目录下，不满足即 raise RuntimeError。为什么放在 helper 内部而不是让每个
  测试文件自己断言：本仓库**没有 pytest**，模块级 setup() 只在 __main__ 直跑
  路径被执行，任何"依赖调用方记得调用某函数"的哨兵都可能在换 runner 时静默失效。
  "隔离是否生效"本来就是本 helper 自己的不变量，就近负责。顺序问题（隔离调用
  是否早于业务模块 import）由 checks/check_test_db_isolation.py 静态检查负责。
"""
import atexit
import os
import shutil
import time
import uuid

import memory.database as _database_module

# 本进程内创建过的临时目录清单（进程退出兜底清理用）
_created_tmp_dirs: list = []

# 清扫历史残留的目录名前缀与最小年龄（秒）
_TMP_PREFIX = "_test_tmp_"
_STALE_MIN_AGE = 300


def _rmtree_retry(d: str, attempts: int = 8, delay: float = 0.3) -> None:
    """删除目录；容忍瞬时文件锁（如杀软扫描），失败则短暂重试后放弃"""
    for _ in range(attempts):
        if not os.path.exists(d):
            return
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            return
        time.sleep(delay)


def _cleanup_all_tmp_dirs():
    """删除本进程创建过的全部临时数据库目录（atexit 兜底）

    先 gc.collect()：测试中遗留的未 close() 的 sqlite3 连接因「连接↔语句缓存」
    引用环，在局部变量释放后仍存活到解释器关闭期 GC，导致 db 文件在 atexit
    阶段仍被占用、rmtree 失败；此处先强制回收循环引用、关闭连接再删除目录。
    """
    import gc
    gc.collect()
    for d in _created_tmp_dirs:
        _rmtree_retry(d)
    _created_tmp_dirs.clear()


def _sweep_stale_tmp_dirs():
    """清扫历史遗留的 _test_tmp_* 目录（上次运行被强杀/文件锁残留的自愈）

    仅删除创建超过 _STALE_MIN_AGE 秒的目录，避免误删并行运行中其它测试进程
    正在使用的临时库。
    """
    base = os.path.join("data")
    if not os.path.isdir(base):
        return
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not name.startswith(_TMP_PREFIX):
            continue
        d = os.path.join(base, name)
        if not os.path.isdir(d):
            continue
        try:
            if now - os.path.getmtime(d) >= _STALE_MIN_AGE:
                _rmtree_retry(d)
        except OSError:
            continue


def cleanup_test_db() -> None:
    """删除本进程创建的全部临时测试库目录（幂等）

    供各测试文件的 test_cleanup() 显式调用。收拢动机（P4-3a）：原本每个文件各自
    `shutil.rmtree(_TMP_DIR)`，属"两处手工复制保持一致"的载体——一旦 helper 改了
    清理策略（如重试、自愈），副本不会跟着改。现统一由本函数承担。
    """
    _cleanup_all_tmp_dirs()


def setup_test_db() -> str:
    """切换到独立临时测试数据库，返回临时目录路径

    步骤：
    ① 自愈清扫历史残留的 _test_tmp_* 老目录（仅首次调用执行一次）；
    ② 在项目 data/ 下创建 _test_tmp_{uuid8} 临时目录；
    ③ 把 memory.database.DB_PATH 指向该目录下的 test_memory.db；
    ④ 注册 atexit 兜底清理（幂等，重复注册无副作用）。

    返回临时目录绝对路径，测试可在结束时自行删除（可选；atexit 亦会兜底）。
    """
    if not getattr(setup_test_db, "_swept", False):
        _sweep_stale_tmp_dirs()
        setup_test_db._swept = True
    tmp_dir = os.path.join("data", f"{_TMP_PREFIX}{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    _database_module.DB_PATH = os.path.join(tmp_dir, "test_memory.db")
    _created_tmp_dirs.append(tmp_dir)
    # 幂等注册：无论 setup_test_db 调用多少次，只挂一个清理回调
    if not getattr(setup_test_db, "_atexit_registered", False):
        atexit.register(_cleanup_all_tmp_dirs)
        setup_test_db._atexit_registered = True

    # 自检（P4-3b）：隔离是否真的生效，由本 helper 就近负责，不依赖调用方在
    # __main__ 里记得调用任何函数。用显式 raise 而非裸 assert（python -O 会剥离 assert）。
    current = os.path.abspath(_database_module.DB_PATH)
    expected = os.path.abspath(tmp_dir)
    if not current.startswith(expected + os.sep):
        raise RuntimeError(
            f"DB 隔离未生效：DB_PATH={current}，期望位于 {expected} 之下"
        )
    return tmp_dir
