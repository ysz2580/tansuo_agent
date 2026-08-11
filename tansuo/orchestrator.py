"""主循环：预算中枢、批推进、唤醒策略、断点续跑。

试验推进权在 orchestrator，不在 agent——agent 只能通过工具（run_trials /
add_custom_trial）请求额外试验，且一律受预算钳制。LLM 全挂时本模块照常跑完。

并行试验（budget.workers > 1）：批内多线程执行。Optuna 官方支持同进程多线程
ask/tell（study.optimize(n_jobs=) 即此模式）；每个试验在 TrialRunner 内持有
独立 adapter 实例，互不干扰。唤醒仍发生在批边界。
"""
from __future__ import annotations

import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import optuna

from .config import Settings
from .journal import (FINISH, SESSION_START, TRIAL_END, TRIAL_FAIL, Journal)
from .runner import TrialFailedError, TrialRunner
from .space import SearchSpace

_FINISHED_STATES = (
    optuna.trial.TrialState.COMPLETE,
    optuna.trial.TrialState.PRUNED,
    optuna.trial.TrialState.FAIL,
)


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


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
        self.workers = settings.budget.workers
        self.finished_reason: str | None = None      # finish 工具/预算耗尽时设置
        self.agent_fail_streak = 0
        self.deadline: float | None = None           # 时间预算（monotonic 秒）
        self._durations: deque = deque(maxlen=20)    # 最近完结试验耗时（ETA 用）

    # ---------------- 预算 ----------------
    def finished_count(self) -> int:
        return len(self.study.get_trials(deepcopy=False, states=_FINISHED_STATES))

    def budget_left(self) -> int:
        return max(0, self.total - self.finished_count())

    def _time_exceeded(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def eta_seconds(self) -> float | None:
        """按最近试验平均耗时 × 剩余预算 ÷ 并发数估算；无样本返回 None。"""
        if not self._durations or self.budget_left() <= 0:
            return None
        avg = sum(self._durations) / len(self._durations)
        return avg * self.budget_left() / max(1, self.workers)

    def finish(self, reason: str) -> None:
        """agent 的 finish 工具调用入口。"""
        if not self.finished_reason:
            self.finished_reason = reason

    # ---------------- 试验推进 ----------------
    def _run_one(self, trial, source: str) -> str:
        """执行一次已 ask 的试验并上报。返回 completed/pruned/failed。"""
        t0 = time.perf_counter()
        try:
            value = self.runner.run_trial(trial)
            self.study.tell(trial, value)
            dt = time.perf_counter() - t0
            self._durations.append(dt)
            self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                params=dict(trial.params), source=source,
                                duration_s=round(dt, 1))
            self._progress(trial, "COMPLETE",
                           f"{self.settings.metrics.primary.name}={value:.4f}", dt)
            return "completed"
        except optuna.TrialPruned:
            self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            return "pruned"
        except TrialFailedError as e:
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number, reason=e.reason,
                                hint=e.hint, detail=e.detail, source=source)
            self._progress(trial, "FAILED ", e.reason, time.perf_counter() - t0)
            return "failed"
        except Exception as e:   # noqa: BLE001 —— 意外异常（如 python 模式用户函数错误）不炸掉整个搜索
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number,
                                reason=f"未预期异常：{type(e).__name__}: {e}",
                                hint="检查训练脚本 / adapter 实现", source=source)
            self._progress(trial, "FAILED ", f"未预期异常 {type(e).__name__}",
                           time.perf_counter() - t0)
            return "failed"

    def run_batch(self, n: int, source: str = "search") -> dict:
        """跑 n 次常规试验（n 受剩余预算钳制，时间预算到点停止派发）。返回统计。"""
        n = min(n, self.budget_left())
        stats = {"ran": 0, "completed": 0, "pruned": 0, "failed": 0}
        if self.workers <= 1:
            for _ in range(n):
                if self._time_exceeded():
                    break
                trial = self.study.ask()
                stats[self._run_one(trial, source)] += 1
                stats["ran"] += 1
            return stats
        # 并行：ask 与 submit 交错（主线程逐个 ask），保证不存在"已 ask 未派发"的孤儿
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = []
            for _ in range(n):
                if self._time_exceeded():
                    break
                futures.append(pool.submit(self._run_one, self.study.ask(), source))
            for f in futures:
                stats[f.result()] += 1
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
        eta = self.eta_seconds()
        eta_txt = f" | ETA≈{_fmt_eta(eta)}" if eta is not None else ""
        self.log(f"[{done}/{self.total}] trial#{trial.number} {status} {what}{best}{eta_txt} ({dt:.1f}s)")

    # ---------------- 主循环 ----------------
    def run(self, total_trials: int | None = None, wake_every: int | None = None,
            supervisor=None, workers: int | None = None,
            max_duration_h: float | None = None) -> None:
        """跑到预算耗尽 / 时间预算耗尽 / agent finish / Ctrl+C。supervisor 可为 None。"""
        if total_trials is not None:
            self.total = total_trials
        if workers is not None:
            self.workers = workers
        wake_every = wake_every or self.settings.budget.wake_every
        hours = max_duration_h if max_duration_h is not None else self.settings.budget.max_duration_h
        if hours:
            self.deadline = time.monotonic() + hours * 3600
        self._seed_durations()
        already = self.finished_count()
        if already:
            self.log(f"断点续跑：study 中已有 {already} 次完结试验，本会话预算剩余 "
                     f"{self.budget_left()}（总预算 {self.total}）")
        if hours:
            self.log(f"时间预算：{hours:g} 小时（到点后在途试验跑完即收尾）")
        if self.workers > 1:
            self.log(f"并行试验：{self.workers} 个 worker（唤醒发生在批边界）")
        self.journal.append(SESSION_START, total=self.total, wake_every=wake_every,
                            resume=already > 0, space_version=self.space.version,
                            study_trials=already, workers=self.workers,
                            max_duration_h=hours)
        self.space.snapshot(self.settings.data_dir)

        if self.budget_left() <= 0:
            self.log("预算内的试验已全部完结，无需再跑。")
            self.journal.append(FINISH, reason="budget_exhausted")
            return

        try:
            while self.budget_left() > 0 and not self.finished_reason:
                if self._time_exceeded():
                    self.finished_reason = "time_budget_exhausted"
                    break
                batch = min(wake_every, self.budget_left())
                self.run_batch(batch)
                if self.finished_reason or self.budget_left() <= 0:
                    break
                if self._time_exceeded():
                    self.finished_reason = "time_budget_exhausted"
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

    def _seed_durations(self) -> None:
        """断点续跑时用 journal 里最近的试验耗时预热 ETA。"""
        try:
            events = self.journal.load_events()
        except OSError:
            return
        ends = [e for e in events if e.get("kind") == TRIAL_END
                and isinstance(e.get("duration_s"), (int, float))]
        for e in ends[-self._durations.maxlen:]:
            self._durations.append(float(e["duration_s"]))

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
