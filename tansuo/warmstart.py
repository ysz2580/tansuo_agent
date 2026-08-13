"""新分区热启动：把同目标旧分区的 top 配置入队为新分区的种子试验。

训练代码或数据集变化会让系统自动新开分区——旧结果与新分区不可比（必须重跑），
但"哪些超参数组合曾经有效"的经验仍然有价值：在**优化目标相同**
（objective_hash 一致）的旧分区里，把完成试验按优劣排序，取其配置经当前
搜索空间清洗后依次 ``study.enqueue_trial``——新搜索会优先把这些试验跑一遍，
从历史经验的肩膀上出发，而不是从零探索。

关键语义：
- 种子试验走正常 ask/tell 流程真实执行、计入预算、可被剪枝——**不是**把旧
  结果直接记进新分区。代码/数据已经变了，旧目标值不再成立，复用的只是配置；
- 只取与当前目标指纹相同的分区（与跨分区对比的资格线一致）；目标变化新开的
  分区天然没有同源种子，不会错误继承；
- 入队数量还受本次总预算钳制，小预算运行不会留下一堆待跑的 WAITING 尾巴。
"""
from __future__ import annotations

from .analysis import ranked
from .cohort import code_fingerprint, list_cohorts
from .compare import _open_cohort_study
from .journal import WARM_START
from .study import dispose_study


def sanitize_for_space(space, params: dict) -> dict:
    """把历史配置投影到**当前**搜索空间（按参数定义序遍历，被依赖的父参数
    保证先于条件子参数处理）：

    - 冻结参数 → 一律取当前冻结值（旧值作废；Optuna storage 也禁止改 choices，
      冻结是分类参数唯一的演化方式）；
    - 条件参数 → 仅当已构建配置中的父参数取值满足 depends_on 时保留；
    - 取值超出当前定义域（空间被收窄过）或未知参数 → 丢弃，交给 TPE 在
      当前域内重新采样。
    """
    cfg: dict = {}
    for p in space.params:
        if not p.condition_met(cfg):
            continue
        if p.is_frozen:
            cfg[p.name] = p.frozen
            continue
        if p.name in params and p.domain_contains(params[p.name]):
            cfg[p.name] = params[p.name]
    return cfg


def collect_seed_configs(data_dir, settings, *, base_dir=None) -> list[dict]:
    """收集与当前目标指纹相同的全部分区的候选种子配置。

    跨分区合并按主指标从优到劣排序（各分区方向一致——objective 指纹保证），
    相同配置去重（只保留更优的那条）。返回
    [{"params", "value", "source"（来源分区 id）}]。
    """
    cur_obj = code_fingerprint(settings, base_dir).objective_hash
    sources = [c for c in list_cohorts(data_dir, settings=settings)
               if not c.virtual and not c.incomplete
               and (c.meta or {}).get("objective_hash") == cur_obj]
    entries: list[dict] = []
    for c in sources:
        study, err = _open_cohort_study(c, settings)
        if study is None:
            continue
        try:
            for t in ranked(study):
                entries.append({"params": dict(t.params), "value": t.value,
                                "source": c.id})
        finally:
            dispose_study(study)
    entries.sort(key=lambda e: e["value"],
                 reverse=(settings.metrics.primary.direction == "maximize"))
    seen: set = set()
    seeds: list[dict] = []
    for e in entries:
        key = tuple(sorted(e["params"].items()))
        if key in seen:
            continue
        seen.add(key)
        seeds.append(e)
    return seeds


def warm_start_study(data_dir, settings, study, space, journal, *,
                     base_dir=None, k: int | None = None) -> dict:
    """把 top-k 种子配置入队进 study（应为刚新建、尚无试验的分区）。

    返回 {"enqueued": 实际入队数, "seeds": [{params, source, value}]}。
    k 缺省取 settings.budget.warm_start；0 表示关闭。
    """
    k = settings.budget.warm_start if k is None else k
    if k <= 0:
        return {"enqueued": 0, "seeds": []}
    picked: list[dict] = []
    seen: set = set()
    for e in collect_seed_configs(data_dir, settings, base_dir=base_dir):
        cfg = sanitize_for_space(space, e["params"])
        if not cfg:
            continue   # 清洗后一无所有（全被冻结覆盖/出界），跳过
        key = tuple(sorted(cfg.items()))
        if key in seen:
            continue   # 清洗可能让原本不同的配置归一，二次去重
        seen.add(key)
        picked.append({"params": cfg, "source": e["source"], "value": e["value"]})
        if len(picked) >= k:
            break
    for p in picked:
        study.enqueue_trial(p["params"])
    if picked:
        journal.append(WARM_START, count=len(picked),
                       seeds=[{"source": p["source"], "params": p["params"]}
                              for p in picked])
    return {"enqueued": len(picked), "seeds": picked}
