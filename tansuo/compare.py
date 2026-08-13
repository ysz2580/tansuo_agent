"""跨分区对比：把优化目标指纹相同的多个记录分区并排比较最优结果。

对比的前提是参与分区的 ``objective_hash`` 相同（同主指标 ``name:direction`` +
同 ``data_fraction``）——目标不同则量纲/方向不同，强行比较必然误导，与
resolve_for_run 对目标变化硬拒绝的语义一致（目标不符直接 CompareError）。

缺省选组：取与**当前 settings 目标指纹**相同的全部分区（"和我正在跑的可比吗"）；
若一个都没有（刚改过目标），退化为包含最新分区的那一组。
"""
from __future__ import annotations

import copy

import optuna

from .analysis import learning_curves, ranked
from .cohort import (Cohort, CohortError, apply_cohort, code_fingerprint,
                     cohort_db_file, cohort_stats, list_cohorts, load_cohort)
from .study import STUDY_NAME, dispose_study, make_storage


class CompareError(CohortError):
    """分区之间不可对比（目标不一致 / 无目标指纹）。CohortError 子类，
    Web 层可据此区分 400（不可比）与 404（分区不存在）。"""


def select_compare_cohorts(data_dir, cohort_ids, settings,
                           base_dir=None) -> list[Cohort]:
    """选出参与对比的分区（按 id 升序）。cohort_ids 显式给出时逐一校验。"""
    all_c = list_cohorts(data_dir, settings=settings)
    usable = [c for c in all_c
              if not c.virtual and not c.incomplete and c.meta.get("objective_hash")]
    if cohort_ids:
        sel = [load_cohort(data_dir, cid, settings=settings) for cid in cohort_ids]
        for c in sel:
            if not c.meta.get("objective_hash"):
                raise CompareError(
                    f"分区 {c.id} 无优化目标指纹（历史记录或 meta 损坏），不能参与对比")
        hashes = {c.meta["objective_hash"] for c in sel}
        if len(hashes) > 1:
            detail = "、".join(f"{c.id}（{str(c.meta['objective_hash'])[:8]}）"
                              for c in sorted(sel, key=lambda x: x.id))
            raise CompareError(
                f"这些分区的优化目标不一致，结果不可直接比较：{detail}。"
                "只有目标指纹（主指标与 data_fraction）相同的分区才可比。")
        return sorted(sel, key=lambda c: c.id)
    if not usable:
        raise CohortError("还没有可对比的记录分区（先运行 `python cli.py run` 创建分区）")
    cur_obj = code_fingerprint(settings, base_dir).objective_hash
    group = [c for c in usable if c.meta["objective_hash"] == cur_obj]
    if not group:
        # 当前目标刚改过、尚无匹配分区：退化为包含最新分区的一组
        latest_obj = usable[-1].meta["objective_hash"]
        group = [c for c in usable if c.meta["objective_hash"] == latest_obj]
    return group


def _open_cohort_study(cohort: Cohort, settings):
    """按「报告路径」先例打开分区 study（不改调用方 settings）。
    返回 (study, err)；err 为 None / 'empty'（无试验）/ 'locked'（打不开）。"""
    db = cohort_db_file(cohort)
    if db is None or not db.exists():
        return None, "empty"
    s = copy.deepcopy(settings)
    apply_cohort(s, cohort)
    try:
        study = optuna.load_study(storage=make_storage(s.storage.url),
                                  study_name=STUDY_NAME)
        return study, None
    except KeyError:
        return None, "empty"
    except Exception:   # noqa: BLE001 —— sqlite 被运行中的任务占用等
        return None, "locked"


def compare_cohorts(data_dir, cohort_ids, settings, *,
                    base_dir=None, top_k: int = 5) -> dict:
    """对比入口：返回结构化结果（CLI 与 Web 共用）。

    {"objective_hash", "primary": {name, direction}, "watch": [观测指标名],
     "cohorts": [{id, created_at, note, code_hash, data_hash, completed,
                  locked, best: {trial,value,params}|None, top_k: [...],
                  curve: [逐 epoch dict]}]}
    """
    group = select_compare_cohorts(data_dir, cohort_ids, settings, base_dir)
    meta0 = group[0].meta
    result = {
        "objective_hash": meta0["objective_hash"],
        "primary": meta0.get("primary_metric") or {},
        "watch": [m.name for m in settings.metrics.watch],
        "cohorts": [],
    }
    for c in group:
        st = cohort_stats(c)
        entry = {
            "id": c.id,
            "created_at": c.meta.get("created_at"),
            "note": c.meta.get("note") or "",
            "code_hash": c.meta.get("code_hash"),
            "data_hash": c.meta.get("data_hash"),
            "completed": st["completed"],
            "locked": st["locked"],
            "best": None, "top_k": [], "curve": [],
        }
        study, err = _open_cohort_study(c, settings)
        if err == "locked":
            entry["locked"] = True
        if study is not None:
            try:
                rk = ranked(study)   # 方向感知排序（objective 守卫保证方向一致）
                if rk:
                    b = rk[0]
                    entry["best"] = {"trial": b.number, "value": b.value,
                                     "params": dict(b.params)}
                    entry["top_k"] = [
                        {"trial": t.number, "value": t.value,
                         "params": dict(t.params)}
                        for t in rk[:top_k]]
                    curves = learning_curves(study, trial_ids=[b.number])
                    entry["curve"] = (curves[0].get("curve") or []) if curves else []
            finally:
                dispose_study(study)
        result["cohorts"].append(entry)
    return result
