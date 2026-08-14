"""试验结果分析：汇总、top-k、参数分布对比、参数重要度、学习曲线、收敛信号。

供 agent 工具（get_study_summary / get_learning_curves）与最终报告共用。
"""
from __future__ import annotations

import statistics

import optuna
from optuna.trial import TrialState


def completed_trials(study) -> list:
    return [t for t in study.get_trials(deepcopy=False)
            if t.state == TrialState.COMPLETE and t.value is not None]


def ranked(study) -> list:
    """完成试验按主指标从优到劣排序（方向感知）。"""
    done = completed_trials(study)
    reverse = study.direction == optuna.study.StudyDirection.MAXIMIZE
    return sorted(done, key=lambda t: t.value, reverse=reverse)


def _better(a: float, b: float, maximize: bool) -> float:
    return max(a, b) if maximize else min(a, b)


def param_contrast(ranked_trials: list) -> dict:
    """top25% vs bottom25% 的参数分布对比：哪边更集中，搜索就该往哪收。"""
    n = len(ranked_trials)
    if n < 4:
        return {}
    q = max(1, n // 4)
    top, bot = ranked_trials[:q], ranked_trials[-q:]
    names = sorted({p for t in ranked_trials for p in t.params})
    out: dict = {}
    for name in names:
        tv = [t.params[name] for t in top if name in t.params]
        bv = [t.params[name] for t in bot if name in t.params]
        if not tv or not bv:
            continue
        if any(isinstance(v, str) for v in tv + bv):
            def freq(vs):
                d: dict = {}
                for v in vs:
                    d[str(v)] = d.get(str(v), 0) + 1
                return d
            out[name] = {"kind": "choice", "top": freq(tv), "bottom": freq(bv)}
        else:
            out[name] = {"kind": "num",
                         "top_median": statistics.median(tv),
                         "top_range": [min(tv), max(tv)],
                         "bottom_median": statistics.median(bv),
                         "bottom_range": [min(bv), max(bv)]}
    return out


def param_importances(study) -> dict:
    """参数重要度排序：返回 {参数名: 重要度}（归一化、和≈1，值越大影响越大）。

    optuna 默认 fANOVA 评估器依赖 scikit-learn（本项目未安装），改用 PED-ANOVA
    （optuna v3.6+，仅依赖 numpy，随 optuna 自带）。守卫与 param_contrast 同范式：
    完成试验 <2 直接返回 {}（评估器会抛 ValueError）；试验过多（>500）跳过以封顶
    计算开销；任何评估失败都兜底 {}——分析层不炸汇总。
    """
    done = completed_trials(study)
    if len(done) < 2 or len(done) > 500:
        return {}
    try:
        import warnings

        from optuna.importance import PedAnovaImportanceEvaluator
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # 实验性 API 警告不打扰调用方
            return optuna.importance.get_param_importances(
                study, evaluator=PedAnovaImportanceEvaluator())
    except Exception:
        return {}


def convergence_hint(study, settings, window: int = 5) -> str:
    """收敛信号：最近 window 次完成试验相对之前最优有没有改进。"""
    maximize = settings.metrics.primary.direction == "maximize"
    done = [t for t in study.get_trials(deepcopy=False)
            if t.state == TrialState.COMPLETE and t.value is not None]
    n = len(done)
    if n < window + 2:
        return f"完成试验仅 {n} 次，样本不足以判断收敛"
    recent, before = done[-window:], done[:-window]
    best_recent = recent[0].value
    for t in recent[1:]:
        best_recent = _better(best_recent, t.value, maximize)
    best_before = before[0].value
    for t in before[1:]:
        best_before = _better(best_before, t.value, maximize)
    imp = (best_recent - best_before) if maximize else (best_before - best_recent)
    scale = max(abs(best_before), 1e-9)
    if imp <= 0:
        return f"最近 {window} 次试验未超过之前最优（{best_before:.4f}），疑似收敛"
    if imp / scale < 1e-3:
        return f"最近 {window} 次仅微幅改进（+{imp:.5f}），趋于收敛"
    return f"最近 {window} 次仍在明显改进（+{imp:.4f}），建议继续搜索"


def summarize(study, settings, top_k: int = 5) -> dict:
    trials = study.get_trials(deepcopy=False)
    counts = {
        "completed": sum(1 for t in trials if t.state == TrialState.COMPLETE),
        "pruned": sum(1 for t in trials if t.state == TrialState.PRUNED),
        "failed": sum(1 for t in trials if t.state == TrialState.FAIL),
        "running": sum(1 for t in trials if t.state == TrialState.RUNNING),
    }
    rk = ranked(study)
    best = rk[0] if rk else None
    return {
        "counts": counts,
        "primary": settings.metrics.primary.name,
        "direction": settings.metrics.primary.direction,
        "best": None if best is None else {"trial": best.number, "value": best.value,
                                           "params": dict(best.params)},
        "top_k": [{"trial": t.number, "value": t.value, "params": dict(t.params)}
                  for t in rk[:top_k]],
        "contrast": param_contrast(rk),
        "importances": param_importances(study),
        "convergence": convergence_hint(study, settings),
    }


def learning_curves(study, trial_ids: list[int] | None = None) -> list[dict]:
    """逐 epoch 曲线。默认：top-3 + 最近 2 次完成试验（agent 判断欠拟合/发散/还在涨）。"""
    done = completed_trials(study)
    if trial_ids:
        by_num = {t.number: t for t in done}
        picked = [by_num[i] for i in trial_ids if i in by_num]
    else:
        rk = ranked(study)
        recent = sorted(done, key=lambda t: t.number)[-2:]
        seen: set[int] = set()
        picked = []
        for t in rk[:3] + recent:
            if t.number not in seen:
                seen.add(t.number)
                picked.append(t)
    return [{"trial": t.number, "value": t.value,
             "params": dict(t.params),
             "curve": t.user_attrs.get("curve", [])} for t in picked]
