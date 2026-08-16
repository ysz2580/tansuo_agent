"""功能4+5 回归测试：GPU 调度（parse_gpu_ids / CUDA_VISIBLE_DEVICES 注入）
与成本感知（compute_cost 折算 / max_gpu_hours 到点收尾 / FINISH 事件带成本）。

可直接 `python tests/test_gpu_cost.py` 运行。不依赖 LLM、不依赖真实 GPU：
GPU 探测（nvidia-smi）失败静默返回 []，本文件只测解析与记账的确定性逻辑。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


SPACE_YAML = """\
params:
  - name: lr
    type: float
    low: 0.01
    high: 0.1
    description: 学习率
"""


def make_env(tmp: Path, train_code: str, budget_extra: str = "") -> tuple:
    """最小可运行环境：settings（data_dir 指到 tmp）+ space + 假训练脚本。"""
    tmp.mkdir(parents=True, exist_ok=True)
    train = tmp / "fake_train.py"
    train.write_text(train_code, encoding="utf-8")
    settings_path = tmp / "settings.yaml"
    settings_path.write_text(
        "experiment:\n"
        f"  data_dir: {json.dumps(str(tmp))}\n"
        "metrics:\n"
        "  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n"
        "  mode: subprocess\n"
        f"  command: [{json.dumps(sys.executable)}, {json.dumps(str(train))}]\n"
        "  timeout_s: 120\n"
        "budget:\n"
        "  total_trials: 4\n"
        "  wake_every: 4\n"
        + budget_extra, encoding="utf-8")
    space_path = tmp / "search_space.yaml"
    space_path.write_text(SPACE_YAML, encoding="utf-8")
    return settings_path, space_path, train


def test_parse_gpu_ids() -> None:
    print("== parse_gpu_ids：CLI/前端 GPU 列表解析 ==")
    from tansuo.gpu import parse_gpu_ids
    ok("常规解析", parse_gpu_ids("0,1,3") == [0, 1, 3])
    ok("空格容忍与去重（保序）", parse_gpu_ids(" 0, 1 , 1") == [0, 1])
    ok("空串 → 空列表", parse_gpu_ids("") == [])
    ok("None → 空列表", parse_gpu_ids(None) == [])
    for bad in ("a", "0,x", "-1", "1.5"):
        try:
            parse_gpu_ids(bad)
            ok(f"非法输入被拒：{bad!r}", False)
        except ValueError:
            ok(f"非法输入被拒：{bad!r}", True)


def test_compute_cost() -> None:
    print("== compute_cost：算力折算口径 ==")
    from tansuo.journal import SESSION_START, TRIAL_END, compute_cost
    # 有 GPU 会话：2 卡 × 1 小时 = 2 GPU·小时
    events = [
        {"kind": SESSION_START, "gpus": [0, 1]},
        {"kind": TRIAL_END, "duration_s": 3600},
    ]
    c = compute_cost(events)
    ok("GPU 会话：slots=2 且 1h 试验折算 2 GPU·小时",
       c["slots"] == 2 and abs(c["compute_hours"] - 2.0) < 1e-9, str(c))
    # 无 GPU 会话：机时
    events2 = [{"kind": SESSION_START}, {"kind": TRIAL_END, "duration_s": 1800}]
    c2 = compute_cost(events2)
    ok("无 GPU：slots=1，单位机时", c2["slots"] == 1
       and abs(c2["compute_hours"] - 0.5) < 1e-9, str(c2))
    # 多次会话取最近一次 SESSION_START 的 gpus；无 duration 的事件忽略
    events3 = [{"kind": SESSION_START}, {"kind": SESSION_START, "gpus": [3]},
               {"kind": TRIAL_END, "duration_s": None},
               {"kind": TRIAL_END, "duration_s": 3600}]
    c3 = compute_cost(events3)
    ok("slots 跟随最近一次会话声明", c3["slots"] == 1
       and c3["gpus"] == [3] and abs(c3["compute_hours"] - 1.0) < 1e-9, str(c3))


def _make_runner(tmp: Path, train_code: str, extra_env=None, budget_extra: str = ""):
    from tansuo.config import load_settings
    from tansuo.journal import Journal
    from tansuo.runner import TrialRunner
    from tansuo.space import SearchSpace
    settings_path, space_path, _ = make_env(tmp, train_code, budget_extra)
    settings = load_settings(settings_path)
    space = SearchSpace.from_yaml(space_path)
    journal = Journal(tmp / "journal.jsonl")
    return settings, space, journal, TrialRunner(settings, space, journal,
                                                 extra_env=extra_env)


def _run_one_trial(runner):
    import optuna
    study = optuna.create_study(direction="maximize")
    trial = study.ask()
    value = runner.run_trial(trial)
    study.tell(trial, value)
    return value


def test_extra_env_injection(tmp: Path) -> None:
    print("== extra_env：CUDA_VISIBLE_DEVICES 注入与覆盖优先 ==")
    code = ("import os, json\n"
            "cuda = os.environ.get('CUDA_VISIBLE_DEVICES', '')\n"
            "frac = os.environ.get('TANSUO_DATA_FRACTION', '')\n"
            "value = (float(cuda) if cuda else 0.0)"
            " + (100.0 if frac == '1.0' else 0.0)\n"
            "print('##TANSUO## ' + json.dumps({'type': 'final', 'value': value}))\n")
    # GPU 注入 + data_fraction 覆盖（毕业赛全量数据的共用通道）
    _, _, _, runner = _make_runner(
        tmp / "env1", code,
        extra_env={"CUDA_VISIBLE_DEVICES": "7", "TANSUO_DATA_FRACTION": "1.0"},
        budget_extra="  data_fraction: 0.5\n")
    v = _run_one_trial(runner)
    ok("CUDA_VISIBLE_DEVICES=7 到达训练子进程", abs(v - 107.0) < 1e-9, str(v))
    ok("extra_env 覆盖 settings 的 data_fraction（毕业赛通道）", v >= 100.0, str(v))
    # 无 extra_env 时：settings 的 data_fraction 照常注入（精确核对 0.5）
    code2 = ("import os, json\n"
             "frac = os.environ.get('TANSUO_DATA_FRACTION', '')\n"
             "value = 1.0 if frac == '0.5' else 0.0\n"
             "print('##TANSUO## ' + json.dumps({'type': 'final', 'value': value}))\n")
    _, _, _, runner2 = _make_runner(tmp / "env2", code2, budget_extra="  data_fraction: 0.5\n")
    v2 = _run_one_trial(runner2)
    ok("缺省路径：settings data_fraction=0.5 注入子进程", abs(v2 - 1.0) < 1e-9, str(v2))


def test_orchestrator_cost(tmp: Path) -> None:
    print("== orchestrator：算力记账、预算收尾、FINISH 事件 ==")
    from tansuo.journal import FINISH, SESSION_START, Journal
    from tansuo.orchestrator import Orchestrator
    code = ("print('##TANSUO## {\\\"type\\\": \\\"epoch\\\", \\\"epoch\\\": 1, "
            "\\\"metrics\\\": {\\\"val_acc\\\": 0.5}}')\n"
            "print('##TANSUO## {\\\"type\\\": \\\"final\\\", \\\"value\\\": 0.5}')\n")
    settings, space, journal, runner = _make_runner(tmp / "orch", code)
    settings.budget.total_trials = 6
    settings.budget.max_gpu_hours = 1e-6   # 极小算力预算：第一批跑完必超

    import optuna
    study = optuna.create_study(direction="maximize")
    orch = Orchestrator(settings, space, study, runner, journal,
                        log=lambda *a, **k: None)
    ok("slots 缺省 1（无 GPU）", orch.slots == 1)
    orch.gpus = [0, 1]
    ok("slots = GPU 卡数", orch.slots == 2)
    orch.gpus = []
    orch.run(total_trials=6, wake_every=2)
    ok("算力预算到点 → finished_reason=compute_budget_exhausted",
       orch.finished_reason == "compute_budget_exhausted",
       str(orch.finished_reason))
    events = journal.load_events()
    starts = [e for e in events if e.get("kind") == SESSION_START]
    ok("SESSION_START 审计 gpus（无卡时为 null，不虚构）",
       starts and starts[-1].get("gpus") is None)
    finishes = [e for e in events if e.get("kind") == FINISH]
    ok("FINISH 事件带算力成本",
       finishes and finishes[-1].get("reason") == "compute_budget_exhausted"
       and isinstance(finishes[-1].get("compute_hours"), (int, float))
       and finishes[-1]["compute_hours"] > 0, str(finishes[-1:]))
    ok("compute_hours 与内部记账一致",
       abs(finishes[-1]["compute_hours"] - round(orch.compute_hours(), 6)) < 1e-9)

    # 断点续跑预热：新 orchestrator 从 journal 恢复算力累计
    # （追加一条 1h 合成试验，断言不依赖子进程实际速度）
    from tansuo.journal import TRIAL_END as _TE
    journal.append(_TE, trial=999, value=0.5, params={}, source="test",
                   duration_s=3600.0)
    orch2 = Orchestrator(settings, space, study, runner, journal,
                         log=lambda *a, **k: None)
    orch2._seed_compute()
    ok("续跑预热：新进程从 journal 恢复算力累计（≥1h 合成试验）",
       orch2.compute_hours() >= 1.0, str(orch2.compute_hours()))

    # gpus 写入 SESSION_START（有卡场景）
    j2 = Journal(tmp / "orch2.jsonl")
    settings2, space2, _, runner2 = _make_runner(tmp / "orch2", code)
    study2 = optuna.create_study(direction="maximize")
    orch3 = Orchestrator(settings2, space2, study2, runner2, j2,
                         log=lambda *a, **k: None)
    orch3.run(total_trials=1, wake_every=1, gpus=[3])
    starts3 = [e for e in j2.load_events() if e.get("kind") == SESSION_START]
    ok("有卡会话：SESSION_START 记录 gpus=[3]",
       starts3 and starts3[-1].get("gpus") == [3], str(starts3[-1:]))


def test_config_budget(tmp: Path) -> None:
    print("== settings：budget.max_gpu_hours 解析与校验 ==")
    from tansuo.config import ConfigError, load_settings
    settings_path, _, _ = make_env(tmp / "cfg", "print('x')\n",
                                   budget_extra="  max_gpu_hours: 12.5\n")
    s = load_settings(settings_path)
    ok("正常解析 12.5", s.budget.max_gpu_hours == 12.5)
    settings_path2, _, _ = make_env(tmp / "cfg2", "print('x')\n",
                                    budget_extra="  max_gpu_hours: 0\n")
    try:
        load_settings(settings_path2)
        ok("0 预算被拒", False)
    except ConfigError as e:
        ok("0 预算被拒", "max_gpu_hours" in str(e), str(e))


if __name__ == "__main__":
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    test_parse_gpu_ids()
    test_compute_cost()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_extra_env_injection(tmp)
        test_orchestrator_cost(tmp)
        test_config_budget(tmp)
    print(f"\n全部通过：{PASS} 项断言")
