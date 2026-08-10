"""护栏测试：权限 hook、工具执行器护栏、agent 失败降级（可直接运行）。

不依赖 LLM 端点：只测钩子链、执行器与 orchestrator 的确定性行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import optuna                                            # noqa: E402

from tansuo.agent.hooks import HookChain, PermissionGate  # noqa: E402
from tansuo.agent.skills.tune import TuneExecutor        # noqa: E402
from tansuo.config import AgentCfg, MetricSpec, MetricsCfg, Settings  # noqa: E402
from tansuo.journal import Journal                       # noqa: E402
from tansuo.orchestrator import Orchestrator             # noqa: E402
from tansuo.space import SearchSpace                     # noqa: E402

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


class FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_permission_gate(tmp: Path) -> None:
    print("== 权限 hook ==")
    journal = Journal(tmp / "journal.jsonl")
    import tansuo.agent.hooks as hooks_mod

    # allow（默认）
    gate = PermissionGate({"default": "allow"}, journal)
    d = gate.check("tune", "edit_search_space", {})
    ok("默认 allow 放行", d.allowed)

    # deny
    gate = PermissionGate({"default": "allow", "add_custom_trial": "deny"}, journal)
    d = gate.check("tune", "add_custom_trial", {})
    ok("deny 拒绝且给原因", not d.allowed and "禁用" in d.reason)
    d = gate.check("tune", "get_study_summary", {})
    ok("deny 不影响其它工具", d.allowed)

    # confirm 无交互终端 → 自动拒绝
    gate = PermissionGate({"edit_search_space": "confirm"}, journal)
    old_stdin = sys.stdin
    sys.stdin = FakeStdin(tty=False)
    try:
        d = gate.check("tune", "edit_search_space", {})
    finally:
        sys.stdin = old_stdin
    ok("confirm 无终端自动拒绝", not d.allowed and "确认" in d.reason)

    # confirm 有终端且同意 → 放行
    import builtins
    old_input = builtins.input
    builtins.input = lambda _prompt="": "y"
    sys.stdin = FakeStdin(tty=True)
    try:
        d = gate.check("tune", "edit_search_space", {})
    finally:
        sys.stdin = old_stdin
        builtins.input = old_input
    ok("confirm 人工同意放行", d.allowed)

    # 审计落盘
    events = journal.load_events()
    perm_events = [e for e in events if e.get("kind") == "agent_permission"]
    ok("权限决策写入 journal 审计", len(perm_events) >= 3)

    # 钩子链短路
    class DenyAll:
        def check(self, mode, name, inp):
            from tansuo.agent.hooks import HookDecision
            return HookDecision(False, "test", "链上前置钩子拒绝")

    called = {"v": False}

    class Marker:
        def check(self, mode, name, inp):
            called["v"] = True
            from tansuo.agent.hooks import HookDecision
            return HookDecision(True, "allow", "")

    chain = HookChain([DenyAll(), Marker()])
    d = chain.check("tune", "run_trials", {})
    ok("钩子链任一拒绝即短路", not d.allowed and not called["v"])


SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.001, "high": 1.0,
     "description": "学习率"},
    {"name": "width", "type": "choice", "choices": [8, 16, 32],
     "description": "宽度"},
    {"name": "dropout", "type": "float", "low": 0.0, "high": 0.5,
     "description": "丢弃率"},
    {"name": "epochs", "type": "int", "low": 2, "high": 5,
     "description": "轮数"},
]}


class FakeOrch:
    """只实现 TuneExecutor 用到的接口。"""

    def __init__(self, tmp: Path, budget_left: int = 30):
        self.settings = Settings(
            experiment_name="guard", data_dir=str(tmp),
            metrics=MetricsCfg(MetricSpec("val_acc", "maximize")),
            agent=AgentCfg(max_space_edits_total=1),
        )
        self.space = SearchSpace.from_dict(SPACE_DICT)
        self.study = optuna.create_study(direction="maximize")
        self.journal = Journal(tmp / "journal.jsonl")
        self.total = 30
        self._left = budget_left
        self.finished_reason = None
        self.last_batch_count = None

    def finished_count(self):
        return self.total - self._left

    def budget_left(self):
        return self._left

    def finish(self, reason):
        self.finished_reason = reason

    def run_batch(self, n, source="search"):
        self.last_batch_count = n
        self._left -= n
        return {"ran": n, "completed": n, "pruned": 0, "failed": 0}

    def run_custom(self, params, note=None):
        return {"status": "complete", "trial": 99, "value": 0.99}


def test_tune_executor_guardrails(tmp: Path) -> None:
    print("== 调参执行器护栏 ==")
    orch = FakeOrch(tmp)
    ex = TuneExecutor(orch, log=lambda *_: None)

    out = ex.dispatch("no_such_tool", {})
    ok("未知工具给清单提示", "未知工具" in out and "get_study_summary" in out)

    out = ex.dispatch("edit_search_space", {"ops": [{"op": "narrow", "param": "lr",
                                                     "low": 0.01, "high": 0.1}]})
    ok("缺 rationale 被拒", "rationale" in out)

    out = ex.dispatch("edit_search_space",
                      {"ops": [{"op": "set_choices", "param": "width",
                                "choices": [8]}], "rationale": "x"})
    ok("set_choices 被拒并给 freeze 指引", "freeze" in out)

    out = ex.dispatch("edit_search_space",
                      {"ops": [{"op": "narrow", "param": "lr",
                                "low": 0.01, "high": 0.1}],
                       "rationale": "高 lr 区间多次发散"})
    ok("合法 narrow 成功", "编辑成功" in out and "v2" in out)
    ok("补丁写入 journal", len(orch.journal.patches()) == 1)

    out = ex.dispatch("edit_search_space",
                      {"ops": [{"op": "narrow", "param": "dropout",
                                "low": 0.0, "high": 0.1}], "rationale": "再改一个"})
    ok("空间编辑配额（1 次）拦截", "配额" in out)

    orch2 = FakeOrch(tmp)
    ex2 = TuneExecutor(orch2, log=lambda *_: None)
    ex2.dispatch("run_trials", {"count": 99})
    ok("run_trials 钳制到 8", orch2.last_batch_count == 8)

    orch3 = FakeOrch(tmp, budget_left=0)
    ex3 = TuneExecutor(orch3, log=lambda *_: None)
    out = ex3.dispatch("run_trials", {"count": 3})
    ok("预算耗尽拒绝 run_trials", "预算" in out)
    out = ex3.dispatch("add_custom_trial", {"params": {}, "note": ""})
    ok("add_custom_trial 强制 note", "note" in out)

    out = ex3.dispatch("finish", {"reason": ""})
    ok("finish 强制 reason", "reason" in out)
    out = ex3.dispatch("finish", {"reason": "top 配置趋同"})
    ok("finish 记录结束原因", orch3.finished_reason == "top 配置趋同")


def test_agent_failure_downgrade(tmp: Path) -> None:
    print("== agent 失败自动降级 ==")
    settings = Settings(
        experiment_name="guard", data_dir=str(tmp),
        metrics=MetricsCfg(MetricSpec("val_acc", "maximize")),
        agent=AgentCfg(max_consecutive_failures=2),
    )
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(tmp / "journal2.jsonl")
    orch = Orchestrator(settings, space, optuna.create_study(direction="maximize"),
                        runner=None, journal=journal, log=lambda *_: None)

    class Boom:
        def wake(self, o):
            raise RuntimeError("端点 503")

    sup = orch._wake(Boom())
    ok("第 1 次失败仍保留 supervisor", sup is not None and orch.agent_fail_streak == 1)
    sup = orch._wake(Boom())
    ok("连续失败达上限 → 降级（返回 None）", sup is None)

    orch.agent_fail_streak = 0

    class Fine:
        def wake(self, o):
            pass

    sup = orch._wake(Fine())
    ok("成功后失败计数清零", sup is not None and orch.agent_fail_streak == 0)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_permission_gate(tmp)
        test_tune_executor_guardrails(tmp)
        test_agent_failure_downgrade(tmp)
    print(f"\n全部通过：{PASS} 项断言")
