"""主循环：预算中枢、批推进、唤醒策略、断点续跑。

试验推进权在 orchestrator，不在 agent——agent 只能通过工具（run_trials /
add_custom_trial）请求额外试验，且一律受预算钳制。LLM 全挂时本模块照常跑完。

并行试验（budget.workers > 1）：批内多线程执行。Optuna 官方支持同进程多线程
ask/tell（study.optimize(n_jobs=) 即此模式）；每个试验在 TrialRunner 内持有
独立 adapter 实例，互不干扰。唤醒仍发生在批边界。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import optuna

from .config import Settings
from .journal import (FINISH, SESSION_START, TRIAL_END, TRIAL_FAIL, Journal,
                      compute_cost)
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


def _fail_fields(e: TrialFailedError) -> dict:
    """TRIAL_FAIL 事件字段；瞬时故障形态附带环境线索（无线索时不带该键）。"""
    f = {"reason": e.reason, "hint": e.hint, "detail": e.detail}
    if getattr(e, "env_clues", None):
        f["env_clues"] = e.env_clues
    return f


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
        self.cohort_id: str | None = None            # 本次会话的记录分区（run() 注入）
        self.gpus: list[int] = []                    # 本次会话使用的 GPU 卡号（run() 注入）
        self._durations: deque = deque(maxlen=20)    # 最近完结试验耗时（ETA 用）
        # 算力成本：Σ(试验耗时 × slots)，秒。多线程试验下持锁累加。
        self._compute_seconds: float = 0.0
        self._compute_lock = threading.Lock()

    # ---------------- 预算 ----------------
    def finished_count(self) -> int:
        return len(self.study.get_trials(deepcopy=False, states=_FINISHED_STATES))

    def budget_left(self) -> int:
        return max(0, self.total - self.finished_count())

    @property
    def slots(self) -> int:
        """资源槽位数：有 GPU 时 = 卡数（成本折算 GPU·小时），否则 1（机时）。"""
        return len(self.gpus) if self.gpus else 1

    def compute_hours(self) -> float:
        return self._compute_seconds / 3600.0

    def _charge(self, dt: float) -> None:
        """一试验消耗 dt 秒 × slots 槽位；剪枝/失败同样消耗算力，一并计入。"""
        with self._compute_lock:
            self._compute_seconds += dt * self.slots

    def _compute_exceeded(self) -> bool:
        limit = self.settings.budget.max_gpu_hours
        return limit is not None and self.compute_hours() >= limit

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

    # ---------------- webhook 通知（失败绝不影响搜索） ----------------
    def _notify_finish(self, reason: str) -> None:
        """会话结束通知（notify.enabled 且订阅 session_end 时发送）。"""
        try:
            from .notify import notify_finish
            best_line = ""
            try:
                best_line = (f"trial#{self.study.best_trial.number} "
                             f"{self.settings.metrics.primary.name}"
                             f"={self.study.best_value:.4f}")
            except ValueError:
                pass   # 本次会话没有完成的试验
            notify_finish(self.settings, reason=reason,
                          finished=self.finished_count(), total=self.total,
                          best_line=best_line,
                          tokens=self.journal.agent_token_summary(),
                          cohort_id=self.cohort_id, log=self.log)
        except Exception as e:   # noqa: BLE001 —— 通知失败绝不影响搜索
            self.log(f"[notify] 会话结束通知出错（已忽略）：{e}")

    def _notify_degrade(self, limit: int) -> None:
        """agent 连续失败达上限、降级为无 agent 巡航时的通知（会话继续）。"""
        try:
            from .notify import notify_degrade
            notify_degrade(self.settings,
                           detail=f"连续失败阈值：{limit} 次（agent."
                                  f"max_consecutive_failures）",
                           log=self.log)
        except Exception as e:   # noqa: BLE001
            self.log(f"[notify] agent 降级通知出错（已忽略）：{e}")

    # ---------------- 试验推进 ----------------
    def _run_one(self, trial, source: str) -> str:
        """执行一次已 ask 的试验并上报（外层统一计算力：剪枝/失败同样消耗资源）。"""
        t0 = time.perf_counter()
        try:
            return self._run_one_impl(trial, source)
        finally:
            self._charge(time.perf_counter() - t0)

    def _run_one_impl(self, trial, source: str) -> str:
        """执行一次已 ask 的试验并上报。返回 completed/pruned/failed。"""
        t0 = time.perf_counter()
        try:
            value = self.runner.run_trial(trial)
            self.study.tell(trial, value)
            dt = time.perf_counter() - t0
            self._durations.append(dt)
            self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                params=dict(trial.params), source=source,
                                duration_s=round(dt, 2))
            self._progress(trial, "COMPLETE",
                           f"{self.settings.metrics.primary.name}={value:.4f}", dt)
            return "completed"
        except optuna.TrialPruned:
            self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            return "pruned"
        except TrialFailedError as e:
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number, source=source,
                                **_fail_fields(e))
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
            return self._run_custom_impl(trial, params, note, t0)
        finally:
            self._charge(time.perf_counter() - t0)

    def _run_custom_impl(self, trial, params: dict, note: str | None, t0: float) -> dict:
        try:
            value = self.runner.run_trial(trial, cfg_override=params, note=note)
            self.study.tell(trial, value)
            self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                params=dict(trial.params), source="custom",
                                note=note, duration_s=round(time.perf_counter() - t0, 2))
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
            self.journal.append(TRIAL_FAIL, trial=trial.number, source="custom",
                                **_fail_fields(e))
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
            max_duration_h: float | None = None,
            cohort: str | None = None, cohort_fp: str | None = None,
            fp_match: bool | None = None, gpus: list[int] | None = None) -> None:
        """跑到预算耗尽 / 时间预算耗尽 / 算力预算耗尽 / agent finish / Ctrl+C。
        supervisor 可为 None。

        cohort/cohort_fp/fp_match：分区管理审计字段（见 tansuo/cohort.py），
        仅写入 SESSION_START 事件，缺省 None 时行为与旧版完全一致。
        gpus：本次会话使用的 GPU 卡号——影响成本折算（GPU·小时）；
        CUDA_VISIBLE_DEVICES 的注入在 TrialRunner（extra_env），此处只做记账。
        """
        if total_trials is not None:
            self.total = total_trials
        if workers is not None:
            self.workers = workers
        self.cohort_id = cohort
        self.gpus = list(gpus or [])
        wake_every = wake_every or self.settings.budget.wake_every
        hours = max_duration_h if max_duration_h is not None else self.settings.budget.max_duration_h
        if hours:
            self.deadline = time.monotonic() + hours * 3600
        self._seed_durations()
        self._seed_compute()
        already = self.finished_count()
        if already:
            self.log(f"断点续跑：study 中已有 {already} 次完结试验，本会话预算剩余 "
                     f"{self.budget_left()}（总预算 {self.total}）")
        if hours:
            self.log(f"时间预算：{hours:g} 小时（到点后在途试验跑完即收尾）")
        gpu_limit = self.settings.budget.max_gpu_hours
        if gpu_limit:
            unit = "GPU·小时" if self.gpus else "机时"
            self.log(f"算力预算：{gpu_limit:g} {unit}（当前已累计 "
                     f"{self.compute_hours():.3f}，到点优雅收尾）")
        if self.gpus:
            self.log(f"GPU：{','.join(str(g) for g in self.gpus)}"
                     f"（成本按 {self.slots} 槽折算）")
        if self.workers > 1:
            self.log(f"并行试验：{self.workers} 个 worker（唤醒发生在批边界）")
        self.journal.append(SESSION_START, total=self.total, wake_every=wake_every,
                            resume=already > 0, space_version=self.space.version,
                            study_trials=already, workers=self.workers,
                            max_duration_h=hours, cohort=cohort,
                            cohort_fp=cohort_fp, fp_match=fp_match,
                            gpus=self.gpus or None)
        self.space.snapshot(self.settings.data_dir)

        if self.budget_left() <= 0:
            self.log("预算内的试验已全部完结，无需再跑。")
            self.journal.append(FINISH, reason="budget_exhausted",
                                compute_hours=round(self.compute_hours(), 6))
            self._notify_finish("budget_exhausted")
            return

        try:
            while self.budget_left() > 0 and not self.finished_reason:
                if self._time_exceeded():
                    self.finished_reason = "time_budget_exhausted"
                    break
                if self._compute_exceeded():
                    self.finished_reason = "compute_budget_exhausted"
                    break
                batch = min(wake_every, self.budget_left())
                self.run_batch(batch)
                if self.finished_reason or self.budget_left() <= 0:
                    break
                if self._time_exceeded():
                    self.finished_reason = "time_budget_exhausted"
                    break
                if self._compute_exceeded():
                    self.finished_reason = "compute_budget_exhausted"
                    break
                if supervisor is not None:
                    supervisor = self._wake(supervisor)   # 返回 None 表示已降级禁用
        except KeyboardInterrupt:
            self.journal.append(FINISH, reason="interrupted", done=self.finished_count(),
                                compute_hours=round(self.compute_hours(), 6))
            self._notify_finish("interrupted")
            self.log("\n已中断。运行 `python cli.py run --resume` 可从断点续跑。")
            return

        reason = self.finished_reason or "budget_exhausted"
        self.journal.append(FINISH, reason=reason, done=self.finished_count(),
                            compute_hours=round(self.compute_hours(), 6))
        self._notify_finish(reason)
        unit = "GPU·小时" if self.gpus else "机时"
        cost_txt = f" | 算力 {self.compute_hours():.3f} {unit}"
        try:
            self.log(f"\n结束（{reason}）：最优 trial#{self.study.best_trial.number} "
                     f"{self.settings.metrics.primary.name}={self.study.best_value:.4f}"
                     f"{cost_txt}")
        except ValueError:
            self.log(f"\n结束（{reason}）：本次会话没有完成的试验（全部剪枝/失败）"
                     f"{cost_txt}")

    def _seed_compute(self) -> None:
        """断点续跑：用 journal 里已有试验耗时预热算力累计（按本次 slots 折算）。"""
        try:
            events = self.journal.load_events()
        except OSError:
            return
        ends = [e for e in events if e.get("kind") == TRIAL_END
                and isinstance(e.get("duration_s"), (int, float))]
        total_s = sum(float(e["duration_s"]) for e in ends)
        with self._compute_lock:
            self._compute_seconds = total_s * self.slots

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
                self._notify_degrade(limit)
                return None
        else:
            self.agent_fail_streak = 0
        return supervisor
