"""Study 工厂：storage 选型、动态 TPE 采样器、剪枝器、study 创建/续跑。"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import optuna

logger = logging.getLogger("tansuo")

STUDY_NAME = "tansuo"

# sqlite「忙/建表竞态」异常类型元组：
# optuna RDB 路径经 SQLAlchemy 包装，实际抛出的是 sqlalchemy.exc.OperationalError
# （内含 sqlite3.OperationalError），只捕 sqlite3.OperationalError 会漏（STAR #026）。
try:
    from sqlalchemy import exc as _sa_exc
    DB_BUSY_ERRORS: tuple[type, ...] = (sqlite3.OperationalError,
                                        _sa_exc.OperationalError)
except ImportError:                                    # pragma: no cover
    DB_BUSY_ERRORS = (sqlite3.OperationalError,)       # sqlalchemy 随 optuna RDB 必装


class DynamicTPESampler(optuna.samplers.TPESampler):
    """对"动态搜索空间"友好的 TPE 采样器。

    Optuna 默认 TPE 在分类参数 choices 缩小后会崩溃：历史试验若取过已被移除的
    choice，`to_internal_repr` 直接抛 ValueError。这里把这类与当前 choices 不兼容
    的历史试验整体跳过（而不是让整个搜索崩溃）——它们对其它参数的信息一并放弃，
    以保证 Parzen 估计器各参数观测数组对齐。

    数值参数的 narrow/widen 不受影响（Optuna 内部会裁剪越界观测）。
    注意：_get_internal_repr/_get_params 是 optuna 私有 API，升级 optuna 时需回归
    tests/test_space_patch.py::test_dynamic_tpe 验证。
    """

    def _get_internal_repr(self, trials, search_space):
        values: dict[str, list[float]] = {name: [] for name in search_space}
        for trial in trials:
            params = self._get_params(trial)
            if not (search_space.keys() <= params.keys()):
                continue
            row: dict[str, float] = {}
            compatible = True
            for name, dist in search_space.items():
                try:
                    row[name] = dist.to_internal_repr(params[name])
                except ValueError:
                    compatible = False   # 历史取值不在当前 choices 内 → 跳过该试验
                    break
            if compatible:
                for name in search_space:
                    values[name].append(row[name])
        return {k: np.asarray(v) for k, v in values.items()}


_SCHEMA_LOCKS_GUARD = threading.Lock()
_SCHEMA_LOCKS: dict[str, threading.Lock] = {}


def _is_schema_race_error(e: BaseException) -> bool:
    """建表竞态特征：另一线程/进程已抢先把同名表建好（CREATE TABLE 撞车）。"""
    return isinstance(e, DB_BUSY_ERRORS) and "already exists" in str(e)


def _make_rdb_storage(db_path: Path) -> optuna.storages.RDBStorage:
    """建 RDBStorage，带「进程内按路径串行 + 跨进程建表竞态重试」。

    RDBStorage.__init__ 无条件执行 create_all（checkfirst）：两个线程/进程对同一个
    新 db 同时 inspect 都判定"表不存在"→ 双双 CREATE TABLE → 后到者报
    `table studies already exists`（STAR #026）。触发场景：Web 前端并发轮询多个
    只读端点（sync 端点跑在线程池）、Web 后端与 cli 子进程同时打开新分区 db。
    进程内竞态用按路径锁串行化；跨进程竞态靠重试容忍——赢家提交后，下一轮
    checkfirst 能看到表，重试必然成功。
    """
    key = str(db_path.resolve())
    with _SCHEMA_LOCKS_GUARD:
        lock = _SCHEMA_LOCKS.setdefault(key, threading.Lock())
    attempts = 3
    for i in range(attempts):
        try:
            with lock:
                return optuna.storages.RDBStorage(
                    "sqlite:///" + db_path.resolve().as_posix())
        except Exception as e:   # noqa: BLE001 — 类型随 SQLAlchemy 包装而变
            if _is_schema_race_error(e) and i < attempts - 1:
                logger.warning("sqlite 建表竞态（%s），第 %d 次重试：%s",
                               db_path.name, i + 1, str(e).splitlines()[0])
                time.sleep(0.05 * (i + 1))
                continue
            raise
    raise AssertionError("unreachable")   # 循环内要么 return 要么 raise


def make_storage(url: str) -> optuna.storages.BaseStorage:
    """storage 工厂：sqlite:/// 优先；journal:// 为降级方案（纯 Python、零额外依赖）。

    相对路径以进程工作目录为基准；Windows 路径统一转为 POSIX 再拼 URL。
    """
    if url.startswith("sqlite:///"):
        rel = url[len("sqlite:///"):]
        db_path = Path(rel)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return _make_rdb_storage(db_path)
    if url.startswith("journal://"):
        rel = url[len("journal://"):]
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        backend = optuna.storages.journal.JournalFileBackend(str(path.resolve()))
        return optuna.storages.JournalStorage(backend)
    raise ValueError(f"storage.url 不支持：{url}（应以 sqlite:/// 或 journal:// 开头）")


def make_sampler(seed: int = 42, n_startup_trials: int = 5) -> DynamicTPESampler:
    # 单变量模式（默认）：对中途变化的搜索空间稳健；multivariate 在分布漂移时不稳。
    return DynamicTPESampler(seed=seed, n_startup_trials=n_startup_trials)


def make_pruner(pruner_cfg) -> optuna.pruners.BasePruner:
    """剪枝器工厂：median（默认）或 hyperband。

    hyperband 的 max_resource="auto" 时，Optuna 用「已完结试验的最大步数 +1」推断
    总资源；冷启动（无完结试验）不剪枝，安全。搜索空间 epochs 上界若会被 agent
    大幅 widen，建议显式给 max_resource，避免早期推断值偏小。
    """
    if pruner_cfg.type == "hyperband":
        return optuna.pruners.HyperbandPruner(
            min_resource=pruner_cfg.min_resource,
            max_resource=pruner_cfg.max_resource,
            reduction_factor=pruner_cfg.reduction_factor,
        )
    return optuna.pruners.MedianPruner(
        n_startup_trials=pruner_cfg.n_startup_trials,
        n_warmup_steps=pruner_cfg.n_warmup_steps,
        interval_steps=1,
    )


def create_or_load_study(settings, storage: optuna.storages.BaseStorage | None = None) -> optuna.Study:
    """创建或加载 study（load_if_exists=True 即断点续跑）。"""
    storage = storage or make_storage(settings.storage.url)
    direction = "maximize" if settings.metrics.primary.direction == "maximize" else "minimize"
    return optuna.create_study(
        study_name=STUDY_NAME,
        storage=storage,
        direction=direction,
        sampler=make_sampler(seed=settings.budget.seed),
        pruner=make_pruner(settings.pruner),
        load_if_exists=True,
    )


def dispose_study(study) -> None:
    """释放 sqlite 连接池（Windows 下避免 ~30s 句柄残留、临时目录清理失败）。
    study._storage 是 _CachedStorage 包装层，引擎在 _backend 上。"""
    storage = getattr(study, "_storage", None)
    backend = getattr(storage, "_backend", storage)
    engine = getattr(backend, "engine", None)
    if engine is not None:
        try:
            engine.dispose()
        except Exception:   # noqa: BLE001
            pass
