"""最优配置「毕业赛」：隔离 study、全量数据、满训练轮数复验 best 配置。

搜索阶段为省算力常用抽样数据（budget.data_fraction < 1）与较短的训练轮数，
best 配置因此可能"只在抽样数据上最优"。毕业赛把 best 配置原样拿到满资源下
重跑一次，验证它是否真的毕业：

- **隔离临时 study**（内存态）：不向主 study 追加试验，不污染 top-k/重要度/曲线；
- **TANSUO_DATA_FRACTION=1.0 强制注入**：遵守协议的训练脚本切到全量数据；
- **训练轮数维度参数拉到空间上界**（如 epochs）：搜索期为提速取到的低轮数不复现；
- 结果写 `<data_dir>/reports/graduation.yaml`，并落 journal `graduation` 事件。

失败（脚本退出/超时/缺 final 行）同样落盘——"毕业赛没跑成"本身也是结论。
"""
from __future__ import annotations

import time
from pathlib import Path

import optuna
import yaml

from .journal import Journal
from .runner import TrialFailedError, TrialRunner

GRADUATION_EVENT = "graduation"
# 训练轮数维度参数的自动识别关键词（与 setup 期超时校准同一套启发式）
_ITER_KEYWORDS = ("epoch", "step", "iter", "round")


class GraduationError(RuntimeError):
    """无法举办毕业赛（没有 best 配置等前置条件不满足）。"""


def find_iter_param(space, settings) -> str | None:
    """训练轮数维度参数名：adapter.iter_param 显式声明优先，否则按关键词猜。

    只认数值型参数（choice 不可能是轮数）；猜不到返回 None（毕业赛照常跑，
    只是不拉轮数——有的脚本轮数写死在代码里，本就不归搜索空间管）。
    """
    declared = (settings.adapter.iter_param or "").strip()
    by_name = {p.name: p for p in space.params}
    if declared and declared in by_name and by_name[declared].kind in ("int", "float"):
        return declared
    for p in space.params:
        if p.kind in ("int", "float") and any(k in p.name.lower() for k in _ITER_KEYWORDS):
            return p.name
    return None


def graduate(settings, space, study, journal: Journal, log=print) -> dict:
    """为 study 的 best 配置跑一次满资源复验，返回结果 dict（同时落盘）。"""
    try:
        best = study.best_trial
    except (ValueError, KeyError):
        raise GraduationError("当前分区没有完成的试验，无 best 配置可复验"
                              "（先跑一轮超参数搜索再来毕业）")

    params = dict(best.params)
    iter_name = find_iter_param(space, settings)
    iter_before = None
    if iter_name:
        spec = next(p for p in space.params if p.name == iter_name)
        iter_before = params.get(iter_name)
        # 搜索期 TPE 可能取样到低于上界的轮数；毕业赛拉满（仍在空间定义域内）
        params[iter_name] = spec.high

    log(f"[毕业赛] best trial#{best.number}（主指标={best.value:.6g}）"
        f"，全量数据复验" + (f"，{iter_name}：{iter_before} → {params[iter_name]}"
                            if iter_name else ""))

    # 隔离内存 study：只借 Optuna 的 trial 对象协议，不碰主 study 的存储
    direction = ("maximize" if settings.metrics.primary.direction == "maximize"
                 else "minimize")
    iso = optuna.create_study(direction=direction)
    trial = iso.ask()
    runner = TrialRunner(settings, space, journal,
                         extra_env={"TANSUO_DATA_FRACTION": "1.0"})
    t0 = time.perf_counter()
    payload: dict = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "best_trial": best.number,
        "best_value": best.value,
        "primary": settings.metrics.primary.name,
        "direction": direction,
        "params": params,
        "iter_param": ({"name": iter_name, "before": iter_before,
                        "after": params[iter_name]} if iter_name else None),
    }
    try:
        value = runner.run_trial(trial, cfg_override=params, note="graduation")
    except TrialFailedError as e:
        payload.update({"status": "failed", "reason": e.reason, "hint": e.hint,
                        "value": None, "duration_s": round(time.perf_counter() - t0, 2)})
        log(f"[毕业赛] 复验失败：{e.full()}")
    except optuna.TrialPruned:
        payload.update({"status": "pruned", "reason": "复验中途被剪枝（异常：毕业赛本不该剪枝）",
                        "value": None, "duration_s": round(time.perf_counter() - t0, 2)})
        log("[毕业赛] 复验被剪枝——隔离 study 无历史参照，属异常情况，请查看训练输出")
    else:
        dt = round(time.perf_counter() - t0, 2)
        delta = value - best.value
        # verdict：允许 5% 波动视为"hold 住"（抽样噪声量级），超出则如实标注
        if direction == "maximize":
            verdict = "pass" if value >= best.value * 0.95 else "regressed"
        else:
            verdict = "pass" if value <= best.value * 1.05 else "regressed"
        payload.update({"status": "ok", "value": value, "delta": delta,
                        "verdict": verdict, "duration_s": dt})
        log(f"[毕业赛] 完成：{settings.metrics.primary.name}={value:.6g}"
            f"（搜索期 {best.value:.6g}，Δ={delta:+.6g}）→ "
            + ("全量数据下表现保持 ✔" if verdict == "pass"
               else "全量数据下明显回落 ✘（搜索期可能受益于抽样数据）"))
    _persist(settings, journal, payload)
    return payload


def graduation_path(settings) -> Path:
    return Path(settings.data_dir) / "reports" / "graduation.yaml"


def _persist(settings, journal: Journal, payload: dict) -> None:
    """结果落盘：graduation.yaml（供 Web/人读）+ journal 事件（审计链）。"""
    path = graduation_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    journal.append(GRADUATION_EVENT, **payload)


def load_graduation(settings) -> dict | None:
    """读取 graduation.yaml；不存在返回 None。"""
    path = graduation_path(settings)
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
