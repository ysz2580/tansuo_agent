"""新分区热启动测试：当前空间清洗 / 同目标种子收集 / 入队与执行。

独立脚本直跑：python tests/test_warmstart.py
"""
import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import optuna                                                    # noqa: E402
from optuna.distributions import (CategoricalDistribution,       # noqa: E402
                                  FloatDistribution)

from tansuo.cohort import apply_cohort, resolve_for_run          # noqa: E402
from tansuo.journal import WARM_START, Journal                   # noqa: E402
from tansuo.space import SearchSpace                             # noqa: E402
from tansuo.study import create_or_load_study                    # noqa: E402
from tansuo.warmstart import (collect_seed_configs,              # noqa: E402
                              sanitize_for_space, warm_start_study)

from test_cohort import (CHILD_SIMPLE, SPACE_DICT, dispose,      # noqa: E402
                         make_settings, ok)

SPACE_WS = {"params": [
    {"name": "optimizer", "type": "choice", "choices": ["adam", "sgd"],
     "description": "优化器"},
    {"name": "momentum", "type": "float", "low": 0.5, "high": 0.99,
     "depends_on": {"optimizer": "sgd"}, "description": "SGD 动量"},
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率"},
    {"name": "epochs", "type": "int", "low": 1, "high": 10,
     "description": "训练轮数"},
]}


def seed_full_trials(settings, cohort, specs):
    """给分区注入完成试验。specs: [(value, params dict), ...]"""
    s = copy.deepcopy(settings)
    apply_cohort(s, cohort)
    study = create_or_load_study(s)
    dist = {"optimizer": CategoricalDistribution(["adam", "sgd"]),
            "momentum": FloatDistribution(0.5, 0.99),
            "lr": FloatDistribution(0.01, 0.1)}
    for value, params in specs:
        study.add_trial(optuna.trial.create_trial(
            value=value, params=params,
            distributions={k: dist[k] for k in params},
            state=optuna.trial.TrialState.COMPLETE))
    dispose(study)


def test_sanitize():
    print("== 当前空间清洗 ==")
    space = SearchSpace.from_dict(SPACE_WS)
    ok("域内取值原样保留",
       sanitize_for_space(space, {"optimizer": "sgd", "momentum": 0.8, "lr": 0.05,
                                  "epochs": 3})
       == {"optimizer": "sgd", "momentum": 0.8, "lr": 0.05, "epochs": 3})
    ok("超出定义域的取值被丢弃",
       sanitize_for_space(space, {"lr": 0.5}) == {})
    ok("未知参数被丢弃",
       sanitize_for_space(space, {"nope": 1, "lr": 0.02}) == {"lr": 0.02})
    ok("父参数条件不满足时条件子参数被丢弃",
       sanitize_for_space(space, {"optimizer": "adam", "momentum": 0.8})
       == {"optimizer": "adam"})
    res = space.apply_patch([{"op": "freeze", "param": "optimizer", "value": "adam"}],
                            rationale="测试冻结")
    ok("冻结补丁成功", res.ok, detail=str(res.errors))
    ok("冻结参数取当前冻结值、失活的条件子参数丢弃",
       sanitize_for_space(space, {"optimizer": "sgd", "momentum": 0.9, "lr": 0.03})
       == {"optimizer": "adam", "lr": 0.03})


def test_collect(tmp: Path):
    print("== 种子收集 ==")
    s1 = make_settings(tmp, "ws", script_text=CHILD_SIMPLE, direction="maximize")
    data_dir = Path(s1.data_dir)
    cA, _ = resolve_for_run(s1, base_dir=tmp)
    seed_full_trials(s1, cA, [(0.5, {"lr": 0.01}), (0.9, {"lr": 0.02}),
                              (0.8, {"lr": 0.03})])
    cB, _ = resolve_for_run(s1, force_new=True)
    seed_full_trials(s1, cB, [(0.95, {"lr": 0.05}), (0.9, {"lr": 0.02})])
    s_min = make_settings(tmp, "ws", script_text=CHILD_SIMPLE, direction="minimize")
    cC, _ = resolve_for_run(s_min, base_dir=tmp)   # 目标变化 → 自动新开
    seed_full_trials(s_min, cC, [(0.1, {"lr": 0.09})])

    seeds = collect_seed_configs(data_dir, s1, base_dir=tmp)
    ok("跨分区合并按从优到劣排序",
       [e["value"] for e in seeds[:4]] == [0.95, 0.9, 0.8, 0.5],
       detail=str([e["value"] for e in seeds]))
    ok("相同配置去重（只保留更优一条）",
       sum(1 for e in seeds if e["params"] == {"lr": 0.02}) == 1)
    ok("异目标分区不参与",
       all(e["params"] != {"lr": 0.09} for e in seeds))
    ok("source 记录来源分区",
       seeds[0]["source"] == cB.id and seeds[1]["source"] == cA.id)


def test_warm_start(tmp: Path):
    print("== 入队与执行 ==")
    s1 = make_settings(tmp, "ws", script_text=CHILD_SIMPLE, direction="maximize")
    data_dir = Path(s1.data_dir)
    # 模拟代码/数据集变化后新开分区：目标不变、历史分区有成果
    c_new, _ = resolve_for_run(s1, force_new=True)
    s_new = copy.deepcopy(s1)
    apply_cohort(s_new, c_new)
    study = create_or_load_study(s_new)
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(s_new.data_dir) / "journal.jsonl")

    ws = warm_start_study(data_dir, s_new, study, space, journal,
                          base_dir=tmp, k=2)
    ok("入队 top-2 种子配置", ws["enqueued"] == 2, detail=str(ws))
    ok("种子全部来自历史分区（新分区自身无试验不参与）",
       all(p["source"] != c_new.id for p in ws["seeds"]))
    events = journal.load_events()
    ok("journal 记录 warm_start 审计事件",
       any(e.get("kind") == WARM_START and e.get("count") == 2 for e in events))

    # ask 优先消费入队试验：suggest 得到种子取值（真实执行路径）
    t1 = study.ask()
    cfg1 = space.suggest(t1)
    ok("第 1 次试验取样历史最优配置（lr=0.05）", abs(cfg1["lr"] - 0.05) < 1e-9,
       detail=str(cfg1))
    study.tell(t1, 0.7)
    t2 = study.ask()
    cfg2 = space.suggest(t2)
    ok("第 2 次试验取样次优种子（lr=0.02）", abs(cfg2["lr"] - 0.02) < 1e-9,
       detail=str(cfg2))
    study.tell(t2, 0.6)
    dispose(study)

    # k=0 → 什么都不做、不落审计事件
    c_x, _ = resolve_for_run(s1, force_new=True)
    s_x = copy.deepcopy(s1)
    apply_cohort(s_x, c_x)
    study_x = create_or_load_study(s_x)
    jx = Journal(Path(s_x.data_dir) / "journal.jsonl")
    ws0 = warm_start_study(data_dir, s_x, study_x, space, jx, base_dir=tmp, k=0)
    ok("k=0 不入队也不落事件",
       ws0["enqueued"] == 0
       and not any(e.get("kind") == WARM_START for e in jx.load_events()))
    dispose(study_x)


if __name__ == "__main__":
    test_sanitize()
    with tempfile.TemporaryDirectory() as td:
        test_collect(Path(td))
        test_warm_start(Path(td))
    from test_cohort import PASS as _P
    print(f"\n全部通过：{_P} 项断言")
