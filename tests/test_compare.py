"""跨分区对比单元测试：选组 / 方向感知 / top-k / 曲线 / 错误语义。

独立脚本直跑：python tests/test_compare.py
"""
import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import optuna                                        # noqa: E402
from optuna.distributions import FloatDistribution   # noqa: E402

from tansuo.analysis import param_importances, summarize            # noqa: E402
from tansuo.cohort import CohortError, apply_cohort, resolve_for_run  # noqa: E402
from tansuo.compare import (CompareError, compare_cohorts,            # noqa: E402
                            select_compare_cohorts)
from tansuo.study import create_or_load_study                          # noqa: E402

from test_cohort import CHILD_SIMPLE, dispose, expect_error, make_settings, ok  # noqa: E402

CURVE = [{"epoch": 1, "val_acc": 0.7, "val_loss": 0.5},
         {"epoch": 2, "val_acc": 0.9, "val_loss": 0.3}]


def seed_trials(settings, cohort, specs):
    """给分区注入完成试验。specs: [(value, lr, curve|None), ...]"""
    s = copy.deepcopy(settings)
    apply_cohort(s, cohort)
    study = create_or_load_study(s)
    dist = {"lr": FloatDistribution(0.01, 0.1)}
    for value, lr, curve in specs:
        attrs = {"curve": curve} if curve is not None else {}
        study.add_trial(optuna.trial.create_trial(
            value=value, params={"lr": lr}, distributions=dist,
            user_attrs=attrs, state=optuna.trial.TrialState.COMPLETE))
    dispose(study)


def test_compare(tmp: Path):
    # ---- 造三个分区：0001/0002 同目标（maximize），0003 换目标（minimize）----
    s1 = make_settings(tmp, "cmp", script_text=CHILD_SIMPLE, direction="maximize")
    data_dir = Path(s1.data_dir)
    c1, _ = resolve_for_run(s1, base_dir=tmp)
    seed_trials(s1, c1, [(0.8, 0.05, None), (0.9, 0.02, CURVE)])
    c2, _ = resolve_for_run(s1, force_new=True, note="第二轮")
    seed_trials(s1, c2, [(0.85, 0.03, None)])
    s_min = make_settings(tmp, "cmp", script_text=CHILD_SIMPLE, direction="minimize")
    c3, _ = resolve_for_run(s_min, base_dir=tmp)   # 目标变化 → 自动新开
    seed_trials(s_min, c3, [(0.3, 0.07, None), (0.2, 0.04, None)])

    print("== 选组 ==")
    sel = select_compare_cohorts(data_dir, None, s1, base_dir=tmp)
    ok("缺省组 = 与当前目标相同的全部分区（排除异目标）",
        [c.id for c in sel] == [c1.id, c2.id], detail=str([c.id for c in sel]))
    sel_min = select_compare_cohorts(data_dir, None, s_min, base_dir=tmp)
    ok("目标刚改过 → 退化为包含最新分区的一组",
        [c.id for c in sel_min] == [c3.id], detail=str([c.id for c in sel_min]))
    expect_error("跨目标显式对比被拒（CompareError）", CompareError,
                 select_compare_cohorts, data_dir, [c1.id, c3.id], s1, tmp)
    expect_error("未知分区 id → CohortError", CohortError,
                 select_compare_cohorts, data_dir, ["9999-99999999-999999"], s1, tmp)
    s_empty = make_settings(tmp, "empty", script_text=CHILD_SIMPLE)
    expect_error("无任何分区 → CohortError", CohortError,
                 select_compare_cohorts, Path(s_empty.data_dir), None, s_empty, tmp)

    print("== 对比结果（maximize 组）==")
    r = compare_cohorts(data_dir, None, s1, base_dir=tmp)
    ok("objective_hash 取自分组", r["objective_hash"] == c1.meta["objective_hash"])
    ok("primary 含指标名与方向",
        r["primary"] == {"name": "val_acc", "direction": "maximize"})
    ok("watch 列表透出（无观测指标为空）", r["watch"] == [])
    ok("两个分区按 id 升序", [e["id"] for e in r["cohorts"]] == [c1.id, c2.id])
    e1, e2 = r["cohorts"]
    ok("best 方向感知（maximize 取最大 0.9）",
        e1["best"] is not None and abs(e1["best"]["value"] - 0.9) < 1e-9)
    ok("best 携带参数与试验号",
        e1["best"]["params"].get("lr") == 0.02 and e1["best"]["trial"] == 1)
    ok("top_k 按优劣排序且含全部试验",
        [t["value"] for t in e1["top_k"]] == [0.9, 0.8])
    ok("curve 来自最优试验的 user_attrs", e1["curve"] == CURVE)
    ok("无曲线试验 → curve 为空列表", e2["curve"] == [])
    ok("completed 计数正确", e1["completed"] == 2 and e2["completed"] == 1)
    ok("note 透出", e2["note"] == "第二轮")
    ok("code/data 指纹透出", e1["code_hash"] == c1.meta["code_hash"]
        and e1["data_hash"] == c1.meta["data_hash"])

    print("== 显式 ids 与 minimize 方向 ==")
    r2 = compare_cohorts(data_dir, [c2.id, c1.id], s1, base_dir=tmp)
    ok("显式 ids 乱序输入仍按 id 升序输出",
        [e["id"] for e in r2["cohorts"]] == [c1.id, c2.id])
    r3 = compare_cohorts(data_dir, [c3.id], s_min, base_dir=tmp)
    ok("minimize 方向 best 取最小（0.2）",
        r3["cohorts"][0]["best"] is not None
        and abs(r3["cohorts"][0]["best"]["value"] - 0.2) < 1e-9)

    print("== 降级情形 ==")
    c4, _ = resolve_for_run(s1, force_new=True)   # 新分区、无试验
    r4 = compare_cohorts(data_dir, None, s1, base_dir=tmp)
    ok("无试验分区进组且不报错", [e["id"] for e in r4["cohorts"]]
        == [c1.id, c2.id, c4.id])
    e4 = r4["cohorts"][-1]
    ok("无试验分区：completed=0、best=None、curve=[]",
        e4["completed"] == 0 and e4["best"] is None and e4["curve"] == [])
    # 人为制造 incomplete 分区（目录存在但无 meta）→ 选组时跳过
    (data_dir / "runs" / "0009-19000101-000000").mkdir()
    sel2 = select_compare_cohorts(data_dir, None, s1, base_dir=tmp)
    ok("incomplete 分区被跳过", all(c.id != "0009-19000101-000000" for c in sel2))


TWO_PARAM_DIST = {"lr": FloatDistribution(0.01, 0.1),
                  "dropout": FloatDistribution(0.0, 0.5)}


def test_param_importances(tmp: Path):
    # ---- 2 参数 × 6 试验：value 主要由 lr 驱动 ----
    s = make_settings(tmp, "imp", script_text=CHILD_SIMPLE, direction="maximize")
    c1, _ = resolve_for_run(s, base_dir=tmp)
    sc = copy.deepcopy(s)
    apply_cohort(sc, c1)
    study = create_or_load_study(sc)
    specs = [(0.90, 0.02, 0.1), (0.85, 0.03, 0.2), (0.70, 0.05, 0.1),
             (0.60, 0.07, 0.3), (0.55, 0.08, 0.4), (0.50, 0.09, 0.2)]
    for value, lr, dropout in specs:
        study.add_trial(optuna.trial.create_trial(
            value=value, params={"lr": lr, "dropout": dropout},
            distributions=TWO_PARAM_DIST, state=optuna.trial.TrialState.COMPLETE))

    print("== 参数重要度 ==")
    imps = param_importances(study)
    ok("2 参数 6 试验：两键均存在", set(imps) == {"lr", "dropout"}, str(imps))
    ok("重要度为 [0,1] 浮点",
       all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in imps.values()), str(imps))
    ok("归一化和≈1.0", abs(sum(imps.values()) - 1.0) < 1e-6, str(imps))
    sm = summarize(study, sc)
    ok("summarize 透出 importances 键且与独立函数一致", sm["importances"] == imps)
    dispose(study)

    # ---- 1 试验 → {}（评估器样本不足守卫）----
    c2, _ = resolve_for_run(s, force_new=True, base_dir=tmp)
    sc2 = copy.deepcopy(s)
    apply_cohort(sc2, c2)
    study2 = create_or_load_study(sc2)
    study2.add_trial(optuna.trial.create_trial(
        value=0.8, params={"lr": 0.05, "dropout": 0.2},
        distributions=TWO_PARAM_DIST, state=optuna.trial.TrialState.COMPLETE))
    ok("仅 1 次试验 → {}（避免评估器 ValueError）", param_importances(study2) == {})
    ok("重要度为空时 summarize 也不炸", summarize(study2, sc2)["importances"] == {})
    dispose(study2)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        test_compare(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_param_importances(Path(td))
    from test_cohort import PASS as _P
    print(f"\n全部通过：{_P} 项断言")
