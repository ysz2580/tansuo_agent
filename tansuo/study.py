"""Study 工厂：storage 选型、动态 TPE 采样器、剪枝器、study 创建/续跑。"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import optuna

logger = logging.getLogger("tansuo")

STUDY_NAME = "tansuo"


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


def make_storage(url: str) -> optuna.storages.BaseStorage:
    """storage 工厂：sqlite:/// 优先；journal:// 为降级方案（纯 Python、零额外依赖）。

    相对路径以进程工作目录为基准；Windows 路径统一转为 POSIX 再拼 URL。
    """
    if url.startswith("sqlite:///"):
        rel = url[len("sqlite:///"):]
        db_path = Path(rel)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return optuna.storages.RDBStorage("sqlite:///" + db_path.resolve().as_posix())
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


def make_pruner(pruner_cfg) -> optuna.pruners.MedianPruner:
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
