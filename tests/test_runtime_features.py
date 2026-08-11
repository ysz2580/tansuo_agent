"""运行时特性测试：失败重试 / 并行试验 / 时间预算 + ETA（可直接
`python tests/test_runtime_features.py` 运行）。

覆盖：
- 重试：瞬时故障（非零退出码且 stderr 为空）自动重试后成功；重试耗尽仍失败
  时错误信息注明次数；stderr 有内容视为确定性失败不重试；journal 记 trial_retry
- 并行：workers=2 时 run_batch 全部完结，且子进程运行区间存在真实重叠
- 时间预算：极小 max_duration_h → finished_reason=time_budget_exhausted 且未跑满
- ETA：完成试验后 eta_seconds 有值、进度行含 ETA≈；断点续跑可从 journal 预热
- 兜底：python 模式用户函数抛异常 → 该试验 FAIL 但搜索不崩
- 配置校验：workers / retry_on_fail / max_duration_h 越界拒绝
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import optuna                                                  # noqa: E402

from tansuo.config import ConfigError, load_settings           # noqa: E402
from tansuo.journal import (FINISH, SESSION_START, TRIAL_END,  # noqa: E402
                            TRIAL_FAIL, TRIAL_RETRY, Journal)
from tansuo.orchestrator import Orchestrator                   # noqa: E402
from tansuo.runner import TrialFailedError, TrialRunner        # noqa: E402
from tansuo.space import SearchSpace                           # noqa: E402
from tansuo.study import make_pruner, make_sampler             # noqa: E402

PASS = 0

# 第一次运行制造"退出码 1 且无 stderr"的瞬时故障，第二次正常完成（marker 文件判定）
CHILD_FLAKY = """
import json, os, sys
marker = r"%(marker)s"
if not os.path.exists(marker):
    with open(marker, "w") as f:
        f.write("1")
    sys.exit(1)
print("##TANSUO## " + json.dumps({"type": "final", "value": 0.9}))
"""

CHILD_ALWAYS_FAIL = "import sys\nsys.exit(2)\n"

CHILD_STDERR_FAIL = "import sys\nsys.stderr.write('boom error\\n')\nsys.exit(1)\n"

# 每个子进程把自己的运行起止时间写入 OVERLAP_DIR/<pid>.txt（供并发重叠断言）
CHILD_OVERLAP = r"""
import json, os, time
t0 = time.monotonic()
time.sleep(1.2)
t1 = time.monotonic()
d = os.environ.get("OVERLAP_DIR", "")
if d:
    with open(os.path.join(d, f"{os.getpid()}.txt"), "w") as f:
        f.write(f"{t0:.6f} {t1:.6f}")
print("##TANSUO## " + json.dumps({"type": "final", "value": 0.5}))
"""

CHILD_SLOW = "import json, time\ntime.sleep(0.7)\n" \
             'print(\'##TANSUO## {"type": "final", "value": 0.5}\')\n'

SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率（测试用最小空间）"},
]}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def make_settings(tmp: Path, child_code: str, name: str, *, retry: int = 0,
                  workers: int = 1, total: int = 5, mode: str = "subprocess",
                  entry: str = ""):
    """每个用例独立的 data_dir（journal/db 互不串扰）。"""
    script = tmp / f"{name}.py"
    script.write_text(child_code, encoding="utf-8")
    data_dir = tmp / "data" / name
    if mode == "subprocess":
        exe = Path(sys.executable).as_posix()
        adapter_yaml = (f'  mode: subprocess\n'
                        f'  command: ["{exe}", "{script.as_posix()}"]\n'
                        f'  config_via: env\n  timeout_s: 60\n'
                        f'  retry_on_fail: {retry}\n')
    else:
        adapter_yaml = (f'  mode: python\n  entry: "{entry}"\n'
                        f'  timeout_s: 60\n  retry_on_fail: {retry}\n')
    text = (
        "experiment: {name: rt_test, data_dir: " + data_dir.as_posix() + "}\n"
        "metrics:\n"
        "  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n" + adapter_yaml +
        f"budget: {{total_trials: {total}, wake_every: {min(5, total)}, seed: 1, "
        f"workers: {workers}}}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        # 注：测试里统一传入内存 study（见 make_orch），不落 storage 文件，
        # 避免 sqlite 句柄/journal 符号链接锁导致 Windows 临时目录清理失败
        "storage: {url: sqlite:///" + (data_dir / "t.db").as_posix() + "}\n"
    )
    p = tmp / f"{name}_settings.yaml"
    p.write_text(text, encoding="utf-8")
    return load_settings(p)


def make_runner(settings) -> TrialRunner:
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(settings.data_dir) / "journal.jsonl")
    return TrialRunner(settings, space, journal)


def make_orch(settings, log_lines: list | None = None,
              study: optuna.Study | None = None) -> Orchestrator:
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(settings.data_dir) / "journal.jsonl")
    study = study or fresh_study(settings)
    runner = TrialRunner(settings, space, journal)
    return Orchestrator(settings, space, study, runner, journal,
                        log=log_lines.append if log_lines is not None else (lambda *_: None))


def fresh_study(settings=None) -> optuna.Study:
    """内存 study（与 create_or_load_study 同款采样器/剪枝器，但不落盘）。"""
    if settings is None:
        return optuna.create_study(direction="maximize")
    return optuna.create_study(direction="maximize",
                               sampler=make_sampler(seed=settings.budget.seed,
                                                    n_startup_trials=2),
                               pruner=make_pruner(settings.pruner))


def fresh_trial():
    return optuna.create_study(direction="maximize").ask()


# ----------------------------------------------------------------------
def test_retry(tmp: Path) -> None:
    print("== 失败重试 ==")
    marker = tmp / "flaky_marker.txt"
    s = make_settings(tmp, CHILD_FLAKY % {"marker": marker.as_posix()}, "flaky", retry=1)
    r = make_runner(s)
    value = r.run_trial(fresh_trial())
    ok("瞬时故障（退出码1+空stderr）自动重试后成功", abs(value - 0.9) < 1e-9,
       f"value={value}")
    retries = [e for e in r.journal.load_events() if e.get("kind") == TRIAL_RETRY]
    ok("journal 记录 trial_retry（attempt=1）",
       len(retries) == 1 and retries[0].get("attempt") == 1, str(retries))

    # 重试耗尽仍失败：错误信息注明次数
    s2 = make_settings(tmp, CHILD_ALWAYS_FAIL, "always_fail", retry=1)
    r2 = make_runner(s2)
    try:
        r2.run_trial(fresh_trial())
        raise AssertionError("FAIL: 持续失败应 FAILED")
    except TrialFailedError as e:
        ok("重试耗尽仍失败且注明已重试次数",
           "退出码 2" in e.reason and "已自动重试 1 次" in e.reason, e.reason)
    retries2 = [e for e in r2.journal.load_events() if e.get("kind") == TRIAL_RETRY]
    ok("持续失败也只记 1 次 trial_retry（重试后仍败不再记）", len(retries2) == 1)

    # stderr 有内容 → 确定性失败，不重试
    s3 = make_settings(tmp, CHILD_STDERR_FAIL, "stderr_fail", retry=1)
    r3 = make_runner(s3)
    try:
        r3.run_trial(fresh_trial())
        raise AssertionError("FAIL: stderr 有内容的失败应 FAILED")
    except TrialFailedError as e:
        ok("stderr 有内容不重试（无'已自动重试'字样）",
           "退出码 1" in e.reason and "已自动重试" not in e.reason
           and "boom error" in e.detail, e.full())
    retries3 = [e for e in r3.journal.load_events() if e.get("kind") == TRIAL_RETRY]
    ok("确定性失败无 trial_retry 事件", len(retries3) == 0)


def test_parallel(tmp: Path) -> None:
    print("== 并行试验 ==")
    import os
    import time as _time
    overlap_dir = tmp / "overlap"
    overlap_dir.mkdir(exist_ok=True)
    os.environ["OVERLAP_DIR"] = str(overlap_dir)
    try:
        s = make_settings(tmp, CHILD_OVERLAP, "overlap", workers=2, total=4)
        lines: list[str] = []
        orch = make_orch(s, lines)
        t0 = _time.perf_counter()
        stats = orch.run_batch(4)
        wall = _time.perf_counter() - t0
    finally:
        os.environ.pop("OVERLAP_DIR", None)
    ok("4 个试验全部完结",
       stats == {"ran": 4, "completed": 4, "pruned": 0, "failed": 0}, str(stats))
    done = [t for t in orch.study.get_trials(deepcopy=False)
            if t.state == optuna.trial.TrialState.COMPLETE]
    ok("study 记录 4 次 COMPLETE", len(done) == 4)
    ends = [e for e in orch.journal.load_events() if e.get("kind") == TRIAL_END]
    ok("journal 加锁下 4 条 trial_end 完整无串行丢失", len(ends) == 4, f"wall={wall:.1f}s")
    intervals = []
    for f in overlap_dir.glob("*.txt"):
        a, b = f.read_text().split()
        intervals.append((float(a), float(b)))
    overlap = any(a0 < b1 and b0 < a1
                  for i, (a0, a1) in enumerate(intervals)
                  for j, (b0, b1) in enumerate(intervals) if i < j)
    ok("子进程运行区间存在真实重叠（确实并发了）", overlap and len(intervals) == 4,
       f"intervals={intervals}")


def test_time_budget_and_eta(tmp: Path) -> None:
    print("== 时间预算 + ETA ==")
    s = make_settings(tmp, CHILD_SLOW, "slow", total=20)
    lines: list[str] = []
    orch = make_orch(s, lines)
    orch.run(total_trials=20, wake_every=5, max_duration_h=0.5 / 3600)   # 0.5 秒
    ok("时间预算耗尽的 finished_reason 正确",
       orch.finished_reason == "time_budget_exhausted", str(orch.finished_reason))
    ok("未跑满试验预算（优雅收尾）", 1 <= orch.finished_count() < 20,
       f"done={orch.finished_count()}")
    events = orch.journal.load_events()
    finish = [e for e in events if e.get("kind") == FINISH][-1]
    ok("finish 事件 reason=time_budget_exhausted",
       finish.get("reason") == "time_budget_exhausted")
    start = [e for e in events if e.get("kind") == SESSION_START][-1]
    ok("session_start 带 workers 与 max_duration_h",
       start.get("workers") == 1 and start.get("max_duration_h") is not None,
       str({k: start.get(k) for k in ("workers", "max_duration_h")}))
    ok("完成后 ETA 有值（还有剩余预算）",
       orch.eta_seconds() is not None and orch.eta_seconds() > 0,
       f"eta={orch.eta_seconds()}")
    ok("进度行展示 ETA≈", any("ETA≈" in ln for ln in lines), str(lines[-2:]))

    # 断点续跑预热：全新 Orchestrator 从 journal 恢复试验耗时样本
    orch2 = make_orch(s)
    orch2._seed_durations()
    ok("新会话从 journal 预热 ETA 样本", len(orch2._durations) >= 1
       and orch2.eta_seconds() is not None)


def boom_train(config: dict, report) -> float:
    raise ValueError("用户函数炸了")


def test_exception_guard(tmp: Path) -> None:
    print("== 意外异常兜底 ==")
    s = make_settings(tmp, "", "boom", mode="python",
                      entry="test_runtime_features:boom_train", total=2)
    orch = make_orch(s)
    stats = orch.run_batch(2)
    ok("用户函数抛异常不炸掉搜索（逐试验兜底 FAIL）",
       stats == {"ran": 2, "completed": 0, "pruned": 0, "failed": 2}, str(stats))
    fails = [t for t in orch.study.get_trials(deepcopy=False)
             if t.state == optuna.trial.TrialState.FAIL]
    ok("两次试验均记 FAIL", len(fails) == 2)
    fail_events = [e for e in orch.journal.load_events() if e.get("kind") == TRIAL_FAIL]
    ok("journal 记录未预期异常原因",
       len(fail_events) == 2 and all("未预期异常" in e.get("reason", "") for e in fail_events))


def test_config_validation(tmp: Path) -> None:
    print("== 新字段配置校验 ==")

    def expect_error(name: str, yaml_body: str, *substr: str) -> None:
        global PASS
        p = tmp / f"{name}.yaml"
        p.write_text(yaml_body, encoding="utf-8")
        try:
            load_settings(p)
        except ConfigError as e:
            missing = [x for x in substr if x not in str(e)]
            assert not missing, f"FAIL: {name} 错误消息缺少 {missing}，实际：{e}"
            PASS += 1
            print(f"  [ok] {name}（按预期拒绝）")
            return
        raise AssertionError(f"FAIL: {name} 本应报错但没有")

    head = ("metrics:\n  primary: {name: val_acc, direction: maximize}\n"
            "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n")
    expect_error("workers 越界被拒", head + "budget: {workers: 0}\n", "workers")
    expect_error("retry_on_fail 越界被拒",
                 head.replace("command:", "retry_on_fail: 5\n  command:"), "retry_on_fail")
    expect_error("max_duration_h 非正被拒",
                 head + "budget: {max_duration_h: -1}\n", "max_duration_h")


if __name__ == "__main__":
    import tempfile
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_retry(tmp)
        test_parallel(tmp)
        test_time_budget_and_eta(tmp)
        test_exception_guard(tmp)
        test_config_validation(tmp)
    print(f"\n全部通过：{PASS} 项断言")
