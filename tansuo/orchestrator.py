"""主循环：预算中枢、批推进、唤醒策略、断点续跑。

试验推进权在 orchestrator，不在 agent——agent 只能通过工具（run_trials /
add_custom_trial）请求额外试验，且一律受预算钳制。LLM 全挂时本模块照常跑完。
"""
from __future__ import annotations

import time

import optuna

from .config import Settings
from .journal import FINISH, SESSION_START, TRIAL_END, TRIAL_FAIL, Journal
from .runner import TrialFailedError, TrialRunner
from .space import SearchSpace

_FINISHED_STATES = (
    optuna.trial.TrialState.COMPLETE,
    optuna.trial.TrialState.PRUNED,
    optuna.trial.TrialState.FAIL,
)


class Orchestrator:
    def __init__(self, settings: Settings, space: SearchSpace, study,
                 runner: TrialRunner, journal: Journal, log=print):
        self.settings = settings
        self.space = space
        self.study = study
        self.runner = runner
        self.journal = journal
        self.log = log
        self.total = settings.budget.total_trials
        self.finished_reason: str | None = None      # finish 工具/预算耗尽时设置
        self.agent_fail_streak = 0

    # ---------------- 预算 ----------------
    def finished_count(self) -> int:
        return len(self.study.get_trials(deepcopy=False, states=_FINISHED_STATES))

    def budget_left(self) -> int:
        return max(0, self.total - self.finished_count())

    def finish(self, reason: str) -> None:
        """agent 的 finish 工具调用入口。"""
        if not self.finished_reason:
            self.finished_reason = reason

    # ---------------- 试验推进 ----------------
    def run_batch(self, n: int, source: str = "search") -> dict:
        """同步跑 n 次常规试验（n 受剩余预算钳制）。返回统计。"""
        n = min(n, self.budget_left())
        stats = {"ran": 0, "completed": 0, "pruned": 0, "failed": 0}
        for _ in range(n):
            trial = self.study.ask()
            t0 = time.perf_counter()
            try:
                value = self.runner.run_trial(trial)
                self.study.tell(trial, value)
                stats["completed"] += 1
                self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                    params=dict(trial.params), source=source,
                                    duration_s=round(time.perf_counter() - t0, 1))
                self._progress(trial, "COMPLETE",
                               f"{self.settings.metrics.primary.name}={value:.4f}",
                               time.perf_counter() - t0)
            except optuna.TrialPruned:
                self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
                stats["pruned"] += 1
                self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            except TrialFailedError as e:
                self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
                stats["failed"] += 1
                self.journal.append(TRIAL_FAIL, trial=trial.number, reason=e.reason,
                                    hint=e.hint, detail=e.detail, source=source)
                self._progress(trial, "FAILED ", e.reason, time.perf_counter() - t0)
            stats["ran"] += 1
        return stats

    def run_custom(self, params: dict, note: str | None = None) -> dict:
        """agent 的假设驱动试验。计入预算，返回结构化结果供工具回喂。"""
        if self.budget_left() <= 0:
            return {"status": "rejected", "reason": "预算已用完，无法再增加试验"}
        trial = self.study.ask()
        t0 = time.perf_counter()
        try:
            value = self.runner.run_trial(trial, cfg_override=params, note=note)
            self.study.tell(trial, value)
            self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                params=dict(trial.params), source="custom",
                                note=note, duration_s=round(time.perf_counter() - t0, 1))
            self._progress(trial, "COMPLETE",
                           f"{self.settings.metrics.primary.name}={value:.4f}",
                           time.perf_counter() - t0)
            return {"status": "complete", "trial": trial.number, "value": value}
        except optuna.TrialPruned:
            self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            return {"status": "pruned", "trial": trial.number}
        except TrialFailedError as e:
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number, reason=e.reason,
                                hint=e.hint, detail=e.detail, source="custom")
            self._progress(trial, "FAILED ", e.reason, time.perf_counter() - t0)
            return {"status": "failed", "trial": trial.number, "reason": e.full()}

    def _progress(self, trial, status: str, what: str, dt: float) -> None:
        done = self.finished_count()
        best = ""
        try:
            best = f" | best={self.study.best_value:.4f}"
        except ValueError:
            pass
        self.log(f"[{done}/{self.total}] trial#{trial.number} {status} {what}{best} ({dt:.1f}s)")

    # ---------------- 主循环 ----------------
    def run(self, total_trials: int | None = None, wake_every: int | None = None,
            supervisor=None) -> None:
        """跑到预算耗尽 / agent finish / Ctrl+C。supervisor 为 agent 监督者（可为 None）。"""
        if total_trials is not None:
            self.total = total_trials
        wake_every = wake_every or self.settings.budget.wake_every
        already = self.finished_count()
        if already:
            self.log(f"断点续跑：study 中已有 {already} 次完结试验，本会话预算剩余 "
                     f"{self.budget_left()}（总预算 {self.total}）")
        self.journal.append(SESSION_START, total=self.total, wake_every=wake_every,
                            resume=already > 0, space_version=self.space.version,
                            study_trials=already)
        self.space.snapshot(self.settings.data_dir)

        if self.budget_left() <= 0:
            self.log("预算内的试验已全部完结，无需再跑。")
            self.journal.append(FINISH, reason="budget_exhausted")
            return

        try:
            while self.budget_left() > 0 and not self.finished_reason:
                batch = min(wake_every, self.budget_left())
                self.run_batch(batch)
                if self.finished_reason or self.budget_left() <= 0:
                    break
                if supervisor is not None:
                    supervisor = self._wake(supervisor)   # 返回 None 表示已降级禁用
        except KeyboardInterrupt:
            self.journal.append(FINISH, reason="interrupted", done=self.finished_count())
            self.log("\n已中断。运行 `python cli.py run --resume` 可从断点续跑。")
            return

        reason = self.finished_reason or "budget_exhausted"
        self.journal.append(FINISH, reason=reason, done=self.finished_count())
        try:
            self.log(f"\n结束（{reason}）：最优 trial#{self.study.best_trial.number} "
                     f"{self.settings.metrics.primary.name}={self.study.best_value:.4f}")
        except ValueError:
            self.log(f"\n结束（{reason}）：本次会话没有完成的试验（全部剪枝/失败）")

    def _wake(self, supervisor):
        """唤醒 agent 一轮；连续失败超限则返回 None（本会话降级 --no-agent）。"""
        try:
            supervisor.wake(self)
        except Exception as e:   # noqa: BLE001 —— agent 任何异常都不阻塞试验推进
            self.agent_fail_streak += 1
            self.log(f"[agent] 本轮唤醒失败（连续第 {self.agent_fail_streak} 次）：{e}")
            limit = self.settings.agent.max_consecutive_failures
            if self.agent_fail_streak >= limit:
                self.log(f"[agent] 连续失败达到 {limit} 次，本会话自动降级为无 agent 巡航模式")
                return None
        else:
            self.agent_fail_streak = 0
        return supervisor
