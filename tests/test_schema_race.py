"""sqlite 建表竞态回归（STAR #026）：

RDBStorage.__init__ 无条件 create_all（checkfirst）：多线程/多进程并发打开同一个
新 sqlite 时都判定"表不存在"→ 双双 CREATE TABLE → 后到者报
`(sqlite3.OperationalError) table studies already exists`，且真实异常类型是
SQLAlchemy 包装的 sqlalchemy.exc.OperationalError（旧代码只捕 sqlite3.OperationalError，
漏捕后变成 ASGI 500）。

修复两层：
1. make_storage 对 sqlite 加「进程内按路径串行 + 跨进程建表竞态重试」；
2. Web 层"数据库忙"捕获扩为 DB_BUSY_ERRORS（含 SQLAlchemy 包装类型）。

独立脚本直跑：python tests/test_schema_race.py
"""
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import optuna                          # noqa: E402
import sqlalchemy.exc                  # noqa: E402

from tansuo import study as study_mod  # noqa: E402
from tansuo.study import DB_BUSY_ERRORS, make_storage  # noqa: E402

PASS = 0


def ok(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def _race_error():
    """构造与真实现场一致的 SQLAlchemy 包装异常。"""
    return sqlalchemy.exc.OperationalError(
        "CREATE TABLE studies", {},
        sqlite3.OperationalError("table studies already exists"))


def test_thread_race():
    """12 线程并发对同一新 db 建 storage：全部成功（修复前 11/12 失败）。"""
    print("== 进程内多线程建表竞态 ==")
    with tempfile.TemporaryDirectory() as td:
        url = "sqlite:///" + (Path(td) / "race.db").as_posix()
        storages: list = []
        errs: list = []

        def w():
            try:
                storages.append(make_storage(url))
            except Exception as e:            # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=w) for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ok("12 个并发 storage 全部建成（无 already exists）",
           not errs and len(storages) == 12, repr(errs[:1]))
        # 表确实可用：直接续建 study 并 tell 一条试验
        s = optuna.create_study(storage=make_storage(url), study_name="tansuo",
                                direction="maximize", load_if_exists=True)
        s.tell(s.ask(), 0.5)
        ok("竞态后 db 完好（study 可读、试验可写）", len(s.get_trials()) == 1)
        for st in storages:
            st.engine.dispose()
        backend = getattr(s._storage, "_backend", s._storage)
        if getattr(backend, "engine", None) is not None:
            backend.engine.dispose()


def test_retry_after_race():
    """第一次构造撞竞态异常 → 自动重试成功（跨进程竞态的确定性模拟）。"""
    print("== 竞态重试：首次 already exists → 重试成功 ==")
    real = optuna.storages.RDBStorage
    calls = {"n": 0}

    def fake(url, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _race_error()
        return real(url, *a, **kw)

    with tempfile.TemporaryDirectory() as td:
        url = "sqlite:///" + (Path(td) / "retry.db").as_posix()
        try:
            optuna.storages.RDBStorage = fake
            st = make_storage(url)
        finally:
            optuna.storages.RDBStorage = real
        ok("重试后成功返回 storage", st is not None)
        ok("恰好构造 2 次（第 1 次撞竞态、第 2 次成功）", calls["n"] == 2,
           str(calls))
        st.engine.dispose()


def test_retry_exhausted():
    """竞态异常连发 → 重试 3 次后原样抛出（不无限重试、不吞异常）。"""
    print("== 竞态重试上限：3 次后抛出 ==")
    calls = {"n": 0}

    def fake(url, *a, **kw):
        calls["n"] += 1
        raise _race_error()

    with tempfile.TemporaryDirectory() as td:
        url = "sqlite:///" + (Path(td) / "never.db").as_posix()
        real = optuna.storages.RDBStorage
        try:
            optuna.storages.RDBStorage = fake
            try:
                make_storage(url)
                raise AssertionError("应当抛出")
            except sqlalchemy.exc.OperationalError as e:
                ok("重试耗尽后抛出原异常（already exists）",
                   "already exists" in str(e))
        finally:
            optuna.storages.RDBStorage = real
    ok("恰好重试 3 次后放弃", calls["n"] == 3, str(calls))


def test_locked_not_retried():
    """非竞态的 db 忙错误（locked）不触发重试：原样快抛，语义交上层 503。"""
    print("== locked 不属于建表竞态：不重试 ==")
    calls = {"n": 0}

    def fake(url, *a, **kw):
        calls["n"] += 1
        raise sqlalchemy.exc.OperationalError(
            "SELECT", {}, sqlite3.OperationalError("database is locked"))

    with tempfile.TemporaryDirectory() as td:
        url = "sqlite:///" + (Path(td) / "locked.db").as_posix()
        real = optuna.storages.RDBStorage
        try:
            optuna.storages.RDBStorage = fake
            try:
                make_storage(url)
                raise AssertionError("应当抛出")
            except sqlalchemy.exc.OperationalError as e:
                ok("locked 原样抛出", "database is locked" in str(e))
        finally:
            optuna.storages.RDBStorage = real
    ok("只尝试 1 次（无谓重试）", calls["n"] == 1, str(calls))


def test_db_busy_errors_types():
    """DB_BUSY_ERRORS 同时覆盖裸 sqlite3 与 SQLAlchemy 包装两种异常。"""
    print("== DB_BUSY_ERRORS 类型覆盖 ==")
    ok("含 sqlite3.OperationalError", sqlite3.OperationalError in DB_BUSY_ERRORS)
    ok("含 sqlalchemy.exc.OperationalError",
       sqlalchemy.exc.OperationalError in DB_BUSY_ERRORS)
    ok("包装异常实例命中元组", isinstance(_race_error(), DB_BUSY_ERRORS))


def test_safe_load_maps_503():
    """Web 层：SQLAlchemy 包装的 db 忙异常 → 503 降级而非 500 ASGI 崩溃。"""
    print("== _safe_load 把 SQLAlchemy 包装的 db 忙映射为 503 ==")
    import os
    tmp_store = Path(tempfile.gettempdir()) / f"tansuo_ps_{os.getpid()}.json"
    os.environ["TANSUO_PROJECT_STORE"] = str(tmp_store)
    from fastapi import HTTPException
    from tansuo.web import app as webapp

    orig = webapp._load_for

    def boom(cohort_id=None):
        raise sqlalchemy.exc.OperationalError(
            "SELECT 1", {}, sqlite3.OperationalError("database is locked"))

    webapp._load_for = boom
    try:
        try:
            webapp._safe_load(None)
            raise AssertionError("应当抛出 HTTPException")
        except HTTPException as e:
            ok("status_code=503（修复前漏捕 → 500）", e.status_code == 503,
               str(e.detail))
            ok("提示文案说明数据库忙", "数据库" in e.detail)
    finally:
        webapp._load_for = orig
        try:
            tmp_store.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    test_thread_race()
    test_retry_after_race()
    test_retry_exhausted()
    test_locked_not_retried()
    test_db_busy_errors_types()
    test_safe_load_maps_503()
    print(f"\n全部通过：{PASS} 项断言")
