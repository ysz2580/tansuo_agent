"""运行时特性测试：失败重试 / 并行试验 / 时间预算 + ETA（可直接
`python tests/test_runtime_features.py` 运行）。

覆盖：
- 重试：瞬时故障（非零退出码且 stderr 为空）自动重试后成功；重试耗尽仍失败
  时错误信息注明次数；stderr 有内容视为确定性失败不重试；journal 记 trial_retry
- 环境线索：瞬时故障的 trial_retry / trial_fail 事件带 env_clues（磁盘余量等），
  stderr 有内容的确定性失败不带
- 并行：workers=2 时 run_batch 全部完结，且子进程运行区间存在真实重叠
- 时间预算：极小 max_duration_h → finished_reason=time_budget_exhausted 且未跑满
- ETA：完成试验后 eta_seconds 有值、进度行含 ETA≈；断点续跑可从 journal 预热
- 兜底：python 模式用户函数抛异常 → 该试验 FAIL 但搜索不崩
- 唤醒信号：连续同类失败触发系统警报（含疑似耗时维度）、收敛信号
  （最近 window 次未刷新最优），强制注入 wake brief；无信号时渲染不变
- Hyperband：配置校验、工厂、真实搜索冒烟；中途 widen 后 max_resource=auto 自适应
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

from tansuo.analysis import failure_category, recent_failures  # noqa: E402
from tansuo.config import ConfigError, load_settings           # noqa: E402
from tansuo.journal import (AGENT_WAKEUP, FINISH, SESSION_START,  # noqa: E402
                            TRIAL_END, TRIAL_FAIL, TRIAL_RETRY, Journal)
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

# 按配置里的 epochs 逐轮上报（hyperband widen 测试用：步数随空间放宽而变长）
CHILD_STEPS = """
import json, os
cfg = json.loads(os.environ.get("TANSUO_TRIAL_CONFIG", "{}"))
epochs = int(cfg.get("epochs", 2))
for ep in range(1, epochs + 1):
    print("##TANSUO## " + json.dumps(
        {"type": "epoch", "epoch": ep, "metrics": {"val_acc": round(0.05 * ep, 4)}}),
        flush=True)
print("##TANSUO## " + json.dumps({"type": "final", "value": round(0.05 * epochs, 4)}))
"""

SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率（测试用最小空间）"},
]}

# epochs 当前上界 8，envelope 上界 32（供 widen 放宽；模拟 setup 留有余量的空间）
SPACE_WITH_EPOCHS = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率"},
    {"name": "epochs", "type": "int", "low": 2, "high": 8,
     "env_low": 2, "env_high": 32, "description": "训练轮数"},
    {"name": "batch", "type": "int", "low": 16, "high": 64,
     "description": "批大小"},
]}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def make_settings(tmp: Path, child_code: str, name: str, *, retry: int = 0,
                  workers: int = 1, total: int = 5, mode: str = "subprocess",
                  entry: str = "", pruner_yaml: str | None = None):
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
        + (pruner_yaml or
           "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n") +
        "agent: {enabled: false, model: none}\n"
        # 注：测试里统一传入内存 study（见 make_orch），不落 storage 文件，
        # 避免 sqlite 句柄/journal 符号链接锁导致 Windows 临时目录清理失败
        "storage: {url: sqlite:///" + (data_dir / "t.db").as_posix() + "}\n"
    )
    p = tmp / f"{name}_settings.yaml"
    p.write_text(text, encoding="utf-8")
    return load_settings(p)


def make_runner(settings, space_dict: dict | None = None) -> TrialRunner:
    space = SearchSpace.from_dict(space_dict or SPACE_DICT)
    journal = Journal(Path(settings.data_dir) / "journal.jsonl")
    return TrialRunner(settings, space, journal)


def make_orch(settings, log_lines: list | None = None,
              study: optuna.Study | None = None,
              space_dict: dict | None = None) -> Orchestrator:
    space = SearchSpace.from_dict(space_dict or SPACE_DICT)
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


def test_agent_token_usage(tmp: Path) -> None:
    print("== LLM token 用量审计 ==")
    from types import SimpleNamespace

    from tansuo.agent.loop import AgentLoop, AgentSupervisor, make_gate
    from tansuo.agent.skill import Skill, SkillLimits

    def make_resp(in_t, out_t, blocks=None, stop="end_turn"):
        return SimpleNamespace(
            content=blocks if blocks is not None
            else [SimpleNamespace(type="text", text="分析完成")],
            stop_reason=stop,
            usage=SimpleNamespace(input_tokens=in_t, output_tokens=out_t))

    class FakeClient:
        """按序吐出预设响应，记录调用。"""
        def __init__(self, resps):
            self.resps = list(resps)
            self.calls: list[dict] = []
            outer = self

            class _Messages:
                def create(self, **kw):
                    outer.calls.append(kw)
                    return outer.resps.pop(0)

            self.messages = _Messages()

    class _Executor:
        def dispatch(self, name, tool_input):
            return "ok"

    class FakeSkill(Skill):
        name = mode = "fake"

        def tools(self):
            return [{"name": "probe", "description": "探针",
                     "input_schema": {"type": "object", "properties": {}}}]

        def executor(self):
            return _Executor()

        def system_prompt(self):
            return "sys"

        def opening_message(self):
            return "hi"

        def limits(self):
            return SkillLimits(max_turns=4, max_tool_calls=2)

    child = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.7}\')\n'
    s = make_settings(tmp, child, "tokens")
    orch = make_orch(s)
    gate = make_gate(s, orch.journal, log=lambda *_: None)

    # 1) AgentLoop 多次模型调用 → usage 逐次累加（第一次 tool_use，第二次收尾）
    tool_block = SimpleNamespace(type="tool_use", id="tu1", name="probe", input={})
    client = FakeClient([make_resp(100, 20, blocks=[tool_block], stop="tool_use"),
                         make_resp(150, 30)])
    loop = AgentLoop(s, client, orch.journal, gate, mode="fake", log=lambda *_: None)
    text = loop.run(FakeSkill())
    ok("AgentLoop 返回最后一段模型文本", text == "分析完成")
    ok("两次调用的 usage 累加（in 250 / out 50）",
       loop.round_input_tokens == 250 and loop.round_output_tokens == 50,
       f"in={loop.round_input_tokens} out={loop.round_output_tokens}")

    # 2) AgentSupervisor.wake：真实 TuneSkill + 假端点 → 审计事件带用量
    sup = AgentSupervisor(s, orch,
                          client=FakeClient([make_resp(300, 40)]),
                          gate=gate, log=lambda *_: None)
    sup.wake(orch)
    ok("supervisor 会话累计 token（in 300 / out 40）",
       sup.total_input_tokens == 300 and sup.total_output_tokens == 40)
    ends = [e for e in orch.journal.load_events()
            if e.get("kind") == AGENT_WAKEUP and e.get("phase") == "end"]
    ok("agent_wakeup end 事件记录当轮与会话累计用量",
       len(ends) == 1
       and ends[0]["input_tokens"] == 300 and ends[0]["output_tokens"] == 40
       and ends[0]["total_input_tokens"] == 300
       and ends[0]["total_output_tokens"] == 40, str(ends))

    # 3) journal.agent_token_summary：按轮次增量汇总（跨进程续跑安全）
    summ = orch.journal.agent_token_summary()
    ok("token 汇总 = 各轮增量之和",
       summ == {"rounds": 1, "input_tokens": 300, "output_tokens": 40,
                "total_tokens": 340}, str(summ))
    # 再来一轮（续跑场景：新 supervisor 从 0 起算，汇总仍累加历史）
    sup2 = AgentSupervisor(s, orch,
                           client=FakeClient([make_resp(100, 10)]),
                           gate=gate, log=lambda *_: None)
    sup2.wake(orch)
    summ2 = orch.journal.agent_token_summary()
    ok("续跑新会话后汇总跨会话累加（in 400 / out 50）",
       summ2["rounds"] == 2 and summ2["input_tokens"] == 400
       and summ2["total_tokens"] == 450, str(summ2))


def test_hyperband_pruner(tmp: Path) -> None:
    print("== Hyperband 剪枝器 ==")
    base = ("metrics:\n  primary: {name: val_acc, direction: maximize}\n"
            "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n")

    def load(body: str, name: str):
        p = tmp / f"{name}.yaml"
        p.write_text(body, encoding="utf-8")
        return load_settings(p)

    s = load(base + "pruner: {type: hyperband, min_resource: 2, "
                    "max_resource: 27, reduction_factor: 3}\n", "hb_ok")
    ok("hyperband 配置通过校验（字段透出）",
       s.pruner.type == "hyperband" and s.pruner.min_resource == 2
       and s.pruner.max_resource == 27 and s.pruner.reduction_factor == 3)
    ok("make_pruner(hyperband) → HyperbandPruner",
       isinstance(make_pruner(s.pruner), optuna.pruners.HyperbandPruner))
    ok("make_pruner(median) 仍是 MedianPruner",
       isinstance(make_pruner(load(base, "md").pruner), optuna.pruners.MedianPruner))
    s_auto = load(base + "pruner: {type: hyperband, max_resource: auto}\n", "hb_auto")
    ok("hyperband max_resource=auto 合法", s_auto.pruner.max_resource == "auto")

    def expect_error(name: str, body: str, *substr: str) -> None:
        global PASS
        safe = "".join(c if c.isalnum() else "_" for c in name)   # 文件名去 <> 空格
        p = tmp / f"{safe}.yaml"
        p.write_text(body, encoding="utf-8")
        try:
            load_settings(p)
        except ConfigError as e:
            missing = [x for x in substr if x not in str(e)]
            assert not missing, f"FAIL: {name} 错误消息缺 {missing}，实际：{e}"
            PASS += 1
            print(f"  [ok] {name}（按预期拒绝）")
            return
        raise AssertionError(f"FAIL: {name} 本应报错但没有")

    expect_error("hyperband min_resource<1 被拒",
                 base + "pruner: {type: hyperband, min_resource: 0}\n", "min_resource")
    expect_error("hyperband reduction_factor<2 被拒",
                 base + "pruner: {type: hyperband, reduction_factor: 1}\n", "reduction_factor")
    expect_error("hyperband max_resource 非法字符串被拒",
                 base + "pruner: {type: hyperband, max_resource: bogus}\n", "max_resource")
    expect_error("hyperband max_resource≤min_resource 被拒",
                 base + "pruner: {type: hyperband, min_resource: 5, max_resource: 5}\n",
                 "max_resource")
    expect_error("未知 pruner.type 被拒", base + "pruner: {type: nope}\n",
                 "median", "hyperband")

    # 功能冒烟：真实跑一个 hyperband 搜索（每试验报告 10 个 step），不崩、全部完结
    study = optuna.create_study(direction="maximize",
                                sampler=make_sampler(seed=1, n_startup_trials=2),
                                pruner=make_pruner(s.pruner))

    def objective(trial):
        x = trial.suggest_float("x", 0.0, 1.0)
        for step in range(1, 11):
            trial.report(x * step, step)
            if trial.should_prune():
                raise optuna.TrialPruned()
        return x

    study.optimize(objective, n_trials=15)
    states = [t.state for t in study.trials]
    ok("hyperband 下 15 次试验全部完结（COMPLETE/PRUNED，无 FAIL）",
       len(states) == 15
       and all(st in (optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.PRUNED)
               for st in states), str(states))
    ok("hyperband 搜索给出最优值", study.best_value >= 0.0,
       f"best={study.best_value:.4f}")


def test_failure_awareness(tmp: Path) -> None:
    print("== 失败原因感知（agent summary 注入 recent_failures） ==")
    # 两次确定性失败（退出码 2）→ journal 记 trial_fail，recent_failures 能还原
    s = make_settings(tmp, CHILD_ALWAYS_FAIL, "failaware", retry=0, total=2)
    orch = make_orch(s)
    orch.run_batch(2)
    fails = recent_failures(orch.journal)
    ok("recent_failures 返回 2 条失败", len(fails) == 2, str(fails))
    ok("退出码失败归类 exit_code 且带 trial/reason/hint",
       all(f["category"] == "exit_code" and f["trial"] is not None
           and "退出码" in f["reason"] for f in fails), str(fails))

    ok("failure_category 分类正确（timeout/protocol/unexpected/exit_code/other）",
       failure_category("训练超时（>300s）") == "timeout"
       and failure_category("协议行 JSON 解析失败") == "protocol"
       and failure_category("未预期异常：ValueError: x") == "unexpected"
       and failure_category("训练脚本退出码 1") == "exit_code"
       and failure_category("其它原因") == "other")

    # tune.get_study_summary 注入 recent_failures（失败可见 → agent 才能应对）
    from tansuo.agent.skills.tune import TuneExecutor
    ex = TuneExecutor(orch, log=lambda *_: None)
    out = ex._tool_get_study_summary(top_k=3)
    ok("get_study_summary 返回含 recent_failures", "recent_failures" in out)
    ok("summary 的 recent_failures 透出 exit_code 类别与退出码原因",
       "exit_code" in out and "退出码" in out)

    # 无失败试验时不带 recent_failures 键（不打扰）
    s2 = make_settings(tmp, CHILD_SLOW, "nofail", total=1)
    orch2 = make_orch(s2)
    orch2.run_batch(1)
    out2 = TuneExecutor(orch2, log=lambda *_: None)._tool_get_study_summary(top_k=3)
    ok("无失败试验时 summary 不含 recent_failures", "recent_failures" not in out2)


def test_wake_signals(tmp: Path) -> None:
    print("== 唤醒信号（确定性护栏：失败警报 + 收敛信号） ==")
    from optuna.distributions import FloatDistribution, IntDistribution
    from optuna.trial import create_trial

    from tansuo.analysis import (build_wake_signals, failure_alerts,  # noqa: E402
                                 plateau_note, suspicious_dims)
    from tansuo.agent.prompts import tuning_wake_brief                 # noqa: E402

    # 1) 连续 3 次 exit_code 失败 → 系统警报
    s = make_settings(tmp, CHILD_ALWAYS_FAIL, "wakesig_exit", retry=0, total=3)
    orch = make_orch(s)
    orch.run_batch(3)
    alerts = failure_alerts(orch.study, orch.journal)
    ok("连续 3 次 exit_code 失败触发系统警报",
       len(alerts) == 1 and "系统警报" in alerts[0] and "非零退出码" in alerts[0]
       and "trial#0" in alerts[0], str(alerts))
    sigs = build_wake_signals(orch)
    ok("build_wake_signals 汇总失败警报", len(sigs) == 1 and "系统警报" in sigs[0])
    brief = tuning_wake_brief(1, s, orch)
    ok("wake brief 强制注入警报（⚠ 前缀，agent 无法绕过）",
       "⚠ 系统警报" in brief and brief.startswith("第 1 轮唤醒"), brief)
    # 混入一条协议错误 → 类别混杂不触发（避免把偶发问题当系统性问题打扰）
    orch.journal.append(TRIAL_FAIL, trial=99, reason="协议行 JSON 解析失败")
    ok("失败类别混杂时不触发警报", failure_alerts(orch.study, orch.journal) == [])

    # 2) 连续超时 → 警报点名疑似耗时维度（失败试验中取值显著偏高的数值参数）
    s2 = make_settings(tmp, CHILD_SLOW, "wakesig_to", total=2)
    orch2 = make_orch(s2, space_dict=SPACE_WITH_EPOCHS)
    dists = {"lr": FloatDistribution(0.01, 0.1),
             "epochs": IntDistribution(2, 32),
             "batch": IntDistribution(16, 64)}
    orch2.study.add_trial(create_trial(state=optuna.trial.TrialState.COMPLETE, value=0.9,
                                       params={"lr": 0.05, "epochs": 3, "batch": 32},
                                       distributions=dists))
    orch2.study.add_trial(create_trial(state=optuna.trial.TrialState.COMPLETE, value=0.85,
                                       params={"lr": 0.04, "epochs": 4, "batch": 32},
                                       distributions=dists))
    for n, ep in ((2, 12), (3, 16), (4, 14)):
        orch2.study.add_trial(create_trial(state=optuna.trial.TrialState.FAIL,
                                           params={"lr": 0.05, "epochs": ep, "batch": 32},
                                           distributions=dists))
        orch2.journal.append(TRIAL_FAIL, trial=n, reason="训练超时（>60s）")
    alerts2 = failure_alerts(orch2.study, orch2.journal)
    ok("连续超时触发警报并点名疑似耗时维度 epochs",
       len(alerts2) == 1 and "全部超时" in alerts2[0]
       and "疑似耗时维度：epochs" in alerts2[0], str(alerts2))
    ok("suspicious_dims 纯函数：失败组均值显著偏高被点名",
       suspicious_dims([{"epochs": 12}, {"epochs": 16}],
                       [{"epochs": 3}, {"epochs": 4}]) == ["epochs"])
    ok("suspicious_dims：无显著差异不点名",
       suspicious_dims([{"epochs": 4}], [{"epochs": 4}]) == []
       and suspicious_dims([], [{"epochs": 4}]) == [])

    # 3) 收敛信号：最近 window 次未刷新最优 → 提示；仍在改进 → 空
    study3 = fresh_study()
    dists3 = {"lr": FloatDistribution(0.01, 0.1)}
    for v in [0.5, 0.6, 0.9, 0.95, 0.93, 0.94, 0.92, 0.94]:
        study3.add_trial(create_trial(state=optuna.trial.TrialState.COMPLETE, value=v,
                                      params={"lr": 0.05}, distributions=dists3))
    note = plateau_note(study3, s2, window=4)
    ok("连续 4 次未刷新最优 → 确定性收敛信号（含最优试验号）",
       "确定性收敛信号" in note and "trial#3" in note, note)
    study4 = fresh_study()
    for v in [0.5, 0.6, 0.7, 0.75, 0.8, 0.85]:
        study4.add_trial(create_trial(state=optuna.trial.TrialState.COMPLETE, value=v,
                                      params={"lr": 0.05}, distributions=dists3))
    ok("仍在明显改进 → 不发收敛信号", plateau_note(study4, s2, window=4) == "")

    # 4) 无信号场景：build_wake_signals 为空，简报渲染与旧版逐字一致
    s5 = make_settings(tmp, CHILD_SLOW, "wakesig_none", total=1)
    orch5 = make_orch(s5)
    orch5.run_batch(1)
    ok("无信号 → build_wake_signals 为空", build_wake_signals(orch5) == [])
    brief5 = tuning_wake_brief(1, s5, orch5)
    ok("无信号时简报不含 ⚠ 且正文不变",
       "⚠" not in brief5 and brief5.endswith("再决定本轮动作。"), brief5)


def test_env_clues(tmp: Path) -> None:
    print("== 瞬时失败环境线索（根因诊断证据链） ==")
    from tansuo.runner import collect_env_clues                          # noqa: E402

    clues = collect_env_clues(tmp)
    ok("线索含磁盘剩余（GB）", isinstance(clues.get("disk_free_gb"), (int, float)),
       str(clues))

    # 重试事件带线索（flaky 脚本：第一次瞬时失败 → 重试成功）
    s = make_settings(tmp, CHILD_FLAKY % {"marker": str(tmp / "clue_marker")},
                      "clues_retry", retry=1, total=1)
    orch = make_orch(s)
    orch.run_batch(1)
    retries = [e for e in orch.journal.load_events() if e.get("kind") == TRIAL_RETRY]
    ok("trial_retry 事件记录 env_clues",
       len(retries) == 1 and "disk_free_gb" in (retries[0].get("env_clues") or {}),
       str(retries))

    # 瞬时形态最终失败（stderr 为空）→ trial_fail 也带线索
    s2 = make_settings(tmp, CHILD_ALWAYS_FAIL, "clues_fail", retry=0, total=1)
    orch2 = make_orch(s2)
    orch2.run_batch(1)
    fails = [e for e in orch2.journal.load_events() if e.get("kind") == TRIAL_FAIL]
    ok("stderr 为空的 trial_fail 带 env_clues",
       len(fails) == 1 and "disk_free_gb" in (fails[0].get("env_clues") or {}),
       str(fails))

    # stderr 有内容 = 确定性失败 → 不带线索（不产生噪音）
    s3 = make_settings(tmp, CHILD_STDERR_FAIL, "clues_stderr", retry=0, total=1)
    orch3 = make_orch(s3)
    orch3.run_batch(1)
    fails3 = [e for e in orch3.journal.load_events() if e.get("kind") == TRIAL_FAIL]
    ok("stderr 有内容的 trial_fail 不带 env_clues",
       len(fails3) == 1 and not fails3[0].get("env_clues"), str(fails3))


def test_hyperband_widen(tmp: Path) -> None:
    print("== Hyperband + 动态 widen（max_resource=auto 自适应） ==")
    s = make_settings(tmp, CHILD_STEPS, "hb_widen", total=14,
                      pruner_yaml="pruner: {type: hyperband, min_resource: 1, "
                                  "max_resource: auto, reduction_factor: 2}\n")
    orch = make_orch(s, space_dict=SPACE_WITH_EPOCHS)
    orch.run_batch(6)
    # 中途放宽：epochs 上界 8 → 32（envelope 内，模拟 agent 放宽搜索）
    r = orch.space.apply_patch(
        [{"op": "widen", "param": "epochs", "low": 2, "high": 32}],
        "验收测试：放宽 epochs 上界")
    ok("搜索中途 widen 成功", r.ok, str(r.errors))
    orch.run_batch(8)
    trials = orch.study.get_trials(deepcopy=False)
    states = [t.state for t in trials]
    ok("widen 后全部试验完结（COMPLETE/PRUNED，无 FAIL）",
       len(trials) == 14
       and all(st in (optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.PRUNED)
               for st in states), str(states))
    max_steps = max((len(t.user_attrs.get("curve", [])) for t in trials), default=0)
    ok("widen 后有试验实际跑过旧上界 8 步（auto 推断跟随扩展）",
       max_steps > 8, f"max_steps={max_steps}")


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
        test_agent_token_usage(tmp)
        test_hyperband_pruner(tmp)
        test_failure_awareness(tmp)
        test_wake_signals(tmp)
        test_env_clues(tmp)
        test_hyperband_widen(tmp)
        test_config_validation(tmp)
    print(f"\n全部通过：{PASS} 项断言")
