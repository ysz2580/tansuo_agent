"""子进程协议与 runner 契约测试（可直接 `python tests/test_protocol.py` 运行）。

覆盖：
- parse_metric_line 协议行解析（正常/非协议/损坏 JSON/非对象）
- 子进程模式：正常跑通 / 缺 primary 指标 / 缺 final 行 / 非零退出码
- python 函数模式适配器
- data_fraction 环境变量注入
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import optuna                                                  # noqa: E402

from tansuo.config import load_settings                       # noqa: E402
from tansuo.journal import Journal                            # noqa: E402
from tansuo.runner import (TrialFailedError, TrialRunner,     # noqa: E402
                           parse_metric_line)
from tansuo.space import SearchSpace                          # noqa: E402

PASS = 0

CHILD_OK = r"""
import json, os
cfg = json.loads(os.environ["TANSUO_TRIAL_CONFIG"])
for e in range(1, 3):
    print("##TANSUO## " + json.dumps({"type": "epoch", "epoch": e,
        "metrics": {"val_acc": 0.5 + 0.1 * e, "val_loss": 1.0 - 0.1 * e,
                    "epoch_time_s": 0.1}}))
print("noise line should be ignored")
print("##TANSUO## " + json.dumps({"type": "final",
    "value": 0.7, "metrics": {"val_acc": 0.7}}))
"""

CHILD_MISSING_PRIMARY = r"""
import json
print("##TANSUO## " + json.dumps({"type": "epoch", "epoch": 1,
    "metrics": {"accuracy": 0.5}}))
"""

CHILD_NO_FINAL = r"""
import json
print("##TANSUO## " + json.dumps({"type": "epoch", "epoch": 1,
    "metrics": {"val_acc": 0.5}}))
"""

CHILD_CRASH = r"""
import sys
sys.exit(3)
"""

CHILD_BAD_PROTO = r"""
print("##TANSUO## {bad json here")
"""


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def make_settings(tmp: Path, child_code: str, name: str, mode: str = "subprocess",
                  entry: str = "", data_fraction: float = 1.0):
    script = tmp / f"{name}.py"
    script.write_text(child_code, encoding="utf-8")
    if mode == "subprocess":
        exe = Path(sys.executable).as_posix()
        adapter_yaml = (f'  mode: subprocess\n'
                        f'  command: ["{exe}", "{script.as_posix()}"]\n'
                        f'  config_via: env\n  timeout_s: 60\n')
    else:
        adapter_yaml = f'  mode: python\n  entry: "{entry}"\n  timeout_s: 60\n'
    text = (
        "experiment: {name: proto_test, data_dir: " + str((tmp / "data").as_posix()) + "}\n"
        "metrics:\n"
        "  primary: {name: val_acc, direction: maximize}\n"
        "  watch: [{name: val_loss, direction: minimize}]\n"
        "adapter:\n" + adapter_yaml +
        "budget: {total_trials: 5, wake_every: 5, seed: 1, data_fraction: "
        f"{data_fraction}}}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        "storage: {url: sqlite:///" + (tmp / "data" / "t.db").as_posix() + "}\n"
    )
    p = tmp / f"{name}_settings.yaml"
    p.write_text(text, encoding="utf-8")
    return load_settings(p)


SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率（测试用最小空间）"},
]}


def make_runner(tmp: Path, settings) -> TrialRunner:
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(tmp / "data" / "journal.jsonl")
    return TrialRunner(settings, space, journal)


def fresh_trial(direction="maximize"):
    study = optuna.create_study(direction=direction)
    return study.ask()


def test_parse(tmp: Path) -> None:
    print("== parse_metric_line ==")
    ok("非协议行返回 None", parse_metric_line("hello world") is None)
    payload = parse_metric_line('##TANSUO## {"type":"epoch","epoch":1,"metrics":{"val_acc":0.5}}')
    ok("合法协议行解析", payload["type"] == "epoch" and payload["metrics"]["val_acc"] == 0.5)
    try:
        parse_metric_line("##TANSUO## {broken")
        raise AssertionError("FAIL: 损坏 JSON 应报错")
    except TrialFailedError as e:
        ok("损坏 JSON 拒绝并给格式提示", "JSON" in e.reason and e.hint != "")
    try:
        parse_metric_line("##TANSUO## [1,2]")
        raise AssertionError("FAIL: 非对象 JSON 应报错")
    except TrialFailedError:
        ok("非对象 JSON 拒绝", True)


def test_subprocess_contract(tmp: Path) -> None:
    print("== 子进程契约 ==")
    # 正常流程
    s = make_settings(tmp, CHILD_OK, "ok_child")
    r = make_runner(tmp, s)
    trial = fresh_trial()
    value = r.run_trial(trial)
    ok("正常试验返回 final 值", abs(value - 0.7) < 1e-9, f"value={value}")
    curve = trial.user_attrs.get("curve") or []
    ok("学习曲线含 watch 指标", len(curve) == 2 and "val_loss" in curve[0])

    # 缺 primary 指标
    s = make_settings(tmp, CHILD_MISSING_PRIMARY, "missing_primary")
    r = make_runner(tmp, s)
    try:
        r.run_trial(fresh_trial())
        raise AssertionError("FAIL: 缺 primary 应 FAILED")
    except TrialFailedError as e:
        ok("缺 primary → FAILED 且给契约提示",
           "val_acc" in e.reason and "settings.yaml" in e.hint)

    # 缺 final 行
    s = make_settings(tmp, CHILD_NO_FINAL, "no_final")
    r = make_runner(tmp, s)
    try:
        r.run_trial(fresh_trial())
        raise AssertionError("FAIL: 缺 final 应 FAILED")
    except TrialFailedError as e:
        ok("缺 final → FAILED 且给契约提示", "final" in e.reason and e.hint != "")

    # 非零退出码
    s = make_settings(tmp, CHILD_CRASH, "crash")
    r = make_runner(tmp, s)
    try:
        r.run_trial(fresh_trial())
        raise AssertionError("FAIL: 非零退出应 FAILED")
    except TrialFailedError as e:
        ok("非零退出码 → FAILED", "退出码" in e.reason)

    # 协议行 JSON 损坏
    s = make_settings(tmp, CHILD_BAD_PROTO, "bad_proto")
    r = make_runner(tmp, s)
    try:
        r.run_trial(fresh_trial())
        raise AssertionError("FAIL: 损坏协议行应 FAILED")
    except TrialFailedError as e:
        ok("损坏协议行 → FAILED 且给格式提示", "JSON" in e.reason)


def test_python_fn_mode(tmp: Path) -> None:
    print("== python 函数模式 ==")
    s = make_settings(tmp, "", "fn_mode", mode="python",
                      entry="test_protocol:fake_train")
    r = make_runner(tmp, s)
    value = r.run_trial(fresh_trial())
    ok("函数模式返回值", abs(value - 0.88) < 1e-9, f"value={value}")


def fake_train(config: dict, report) -> float:
    """python 模式适配器的示例函数（也是用户接入模板的最小形态）。"""
    for epoch in range(1, 3):
        report(epoch, {"val_acc": 0.4 + 0.2 * epoch, "val_loss": 1.0 - 0.2 * epoch})
    return 0.88


def test_data_fraction_env(tmp: Path) -> None:
    print("== data_fraction 注入 ==")
    child = (
        'import os\n'
        'frac = os.environ.get("TANSUO_DATA_FRACTION", "")\n'
        'import json\n'
        'print("##TANSUO## " + json.dumps({"type": "epoch", "epoch": 1,\n'
        '    "metrics": {"val_acc": 0.5 if frac == "0.5" else 0.1}}))\n'
        'print("##TANSUO## " + json.dumps({"type": "final",\n'
        '    "value": 0.5 if frac == "0.5" else 0.1}))\n'
    )
    s = make_settings(tmp, child, "frac_child", data_fraction=0.5)
    r = make_runner(tmp, s)
    value = r.run_trial(fresh_trial())
    ok("data_fraction=0.5 注入子进程 env", abs(value - 0.5) < 1e-9)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "data").mkdir(exist_ok=True)
        test_parse(tmp)
        test_subprocess_contract(tmp)
        test_python_fn_mode(tmp)
        test_data_fraction_env(tmp)
    print(f"\n全部通过：{PASS} 项断言")
