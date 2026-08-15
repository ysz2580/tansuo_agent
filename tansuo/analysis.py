"""试验结果分析：汇总、top-k、参数分布对比、参数重要度、学习曲线、收敛信号。

供 agent 工具（get_study_summary / get_learning_curves）与最终报告共用。
"""
from __future__ import annotations

import statistics

import optuna
from optuna.trial import TrialState

from .journal import TRIAL_FAIL


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


def failure_category(reason: str) -> str:
    """把失败原因归成 agent 可直接决策的类别。"""
    if "超时" in reason:
        return "timeout"
    if "退出码" in reason:
        return "exit_code"
    if "协议" in reason or "JSON" in reason:
        return "protocol"
    if "未预期异常" in reason:
        return "unexpected"
    return "other"


def recent_failures(journal, limit: int = 5) -> list[dict]:
    """最近 limit 次失败试验的原因（供 agent 区分瞬时噪声与系统性问题）。

    失败原因只存在于 journal（Optuna study 不记录），无失败返回 []。
    每条含 trial/category/reason/hint：category 供 agent 按类别采取不同应对。
    """
    try:
        events = journal.load_events()
    except OSError:
        return []
    fails = [e for e in events if e.get("kind") == TRIAL_FAIL]
    out = []
    for e in fails[-limit:]:
        reason = str(e.get("reason") or "")
        out.append({
            "trial": e.get("trial"),
            "category": failure_category(reason),
            "reason": reason,
            "hint": e.get("hint") or "",
        })
    return out


# ------------------------------------------------------------------
# 唤醒信号（确定性护栏）：失败警报 + 收敛信号，注入 wake brief。
# 提示词纪律依赖 agent 主动调工具查看；这里的信号由代码在唤醒前算好、
# 强制进入开场消息——"是否看到"不再取决于 LLM（见 STAR #023）。
# ------------------------------------------------------------------

def suspicious_dims(failed_params: list[dict], done_params: list[dict]) -> list[str]:
    """失败试验里均值显著偏高的数值维度 → 疑似耗时维度（供超时警报点名）。

    判据：失败组均值 > 完结组均值，且差值超过完结组均值的 20%
    （相对量纲，对 lr 这类小数值与 epochs 这类大数值同样适用）。
    """
    if not failed_params or not done_params:
        return []
    names = sorted({n for p in failed_params for n in p})
    out: list[str] = []
    for n in names:
        fv = [p[n] for p in failed_params
              if n in p and isinstance(p[n], (int, float)) and not isinstance(p[n], bool)]
        dv = [p[n] for p in done_params
              if n in p and isinstance(p[n], (int, float)) and not isinstance(p[n], bool)]
        if not fv or not dv:
            continue
        fm, dm = sum(fv) / len(fv), sum(dv) / len(dv)
        if fm > dm and (fm - dm) / max(abs(dm), 1e-9) > 0.2:
            out.append(n)
    return out


def failure_alerts(study, journal, streak: int = 3) -> list[str]:
    """确定性失败护栏：最近 streak 次失败同属一类 → 返回警报文本（否则 []）。

    警报会注入 wake brief 开场消息，是硬性事实而非建议：
    - 连续 timeout：点名疑似耗时维度（失败试验里取值显著偏高的数值参数）；
    - 连续 exit_code：多为脚本确定性 bug，提示停止烧预算。
    偶发/混杂类别不触发（瞬时噪声已有重试兜底，不打扰）。
    """
    fails = recent_failures(journal, limit=streak)
    if len(fails) < streak:
        return []
    cats = {f["category"] for f in fails}
    nums = "、".join(f"trial#{f['trial']}" for f in fails)
    if cats == {"timeout"}:
        failed_params, done_params = [], []
        fail_set = {f["trial"] for f in fails}
        for t in study.get_trials(deepcopy=False):
            if t.number in fail_set and t.params:
                failed_params.append(dict(t.params))
            elif t.state == TrialState.COMPLETE and t.params:
                done_params.append(dict(t.params))
        dims = suspicious_dims(failed_params, done_params)
        dim_txt = (f"。疑似耗时维度：{'、'.join(dims)}（在失败试验中取值显著偏高）"
                   if dims else "")
        return [f"系统警报（确定性检测）：最近 {streak} 次试验全部超时（{nums}）"
                f"{dim_txt}。必须立即处置：用 narrow 压低耗时维度上界；若空间已收无可收，"
                "在输出中建议用户提高 adapter.timeout_s 或调低 budget.data_fraction。"]
    if cats == {"exit_code"}:
        return [f"系统警报（确定性检测）：最近 {streak} 次试验全部以非零退出码失败"
                f"（{nums}）。这不是搜索能修复的问题——停止烧预算，在输出中指出问题并"
                "建议用户检查训练脚本（必要时调用 finish）。"]
    return []


def plateau_note(study, settings, window: int | None = None) -> str:
    """确定性收敛信号：最近 window 次完成试验未刷新最优 → 返回提示（否则空串）。

    window 默认 max(4, 2×wake_every)：比单次唤醒间隔更长，避免正常波动误报。
    """
    maximize = settings.metrics.primary.direction == "maximize"
    window = window or max(4, 2 * int(settings.budget.wake_every))
    done = [t for t in study.get_trials(deepcopy=False)
            if t.state == TrialState.COMPLETE and t.value is not None]
    if len(done) < window + 2:
        return ""
    recent, before = done[-window:], done[:-window]
    best_recent = recent[0].value
    for t in recent[1:]:
        best_recent = _better(best_recent, t.value, maximize)
    best_before, best_trial = before[0].value, before[0].number
    for t in before[1:]:
        if (t.value > best_before) if maximize else (t.value < best_before):
            best_before, best_trial = t.value, t.number
    imp = (best_recent - best_before) if maximize else (best_before - best_recent)
    if imp > 0:
        return ""
    return (f"确定性收敛信号：最近 {window} 次完成试验均未超过此前最优 "
            f"{best_before:.4f}（trial#{best_trial}）。搜索可能已收敛——"
            "若 top 配置也趋同，考虑调用 finish；否则给出继续搜索的理由。")


def build_wake_signals(orch) -> list[str]:
    """汇总唤醒信号（失败警报 + 收敛信号）。任何异常返回 []——绝不打扰唤醒。

    对 orchestrator 仅鸭子类型依赖（study/journal/settings），测试替身缺字段
    时安静地返回空。
    """
    try:
        study = getattr(orch, "study", None)
        journal = getattr(orch, "journal", None)
        settings = getattr(orch, "settings", None)
        if study is None or journal is None or settings is None:
            return []
        signals = failure_alerts(study, journal)
        note = plateau_note(study, settings)
        if note:
            signals.append(note)
        return signals
    except Exception:   # noqa: BLE001 —— 信号是增强项，计算失败不影响唤醒
        return []


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
