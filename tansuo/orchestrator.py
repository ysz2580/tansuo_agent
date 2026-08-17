"""主循环：预算中枢、批推进、唤醒策略、断点续跑。

试验推进权在 orchestrator，不在 agent——agent 只能通过工具（run_trials /
add_custom_trial）请求额外试验，且一律受预算钳制。LLM 全挂时本模块照常跑完。

并行试验（budget.workers > 1）：批内多线程执行。Optuna 官方支持同进程多线程
ask/tell（study.optimize(n_jobs=) 即此模式）；每个试验在 TrialRunner 内持有
独立 adapter 实例，互不干扰。唤醒仍发生在批边界。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        # 早停护栏状态（每会话从 run() 内重新起算）：
        # _fail_streak 连败（completed/pruned 重置）；_plateau_streak 连续无提升
        # （仅 completed 计入，方向感知），阈值 0/None 表示关闭。
        self._fail_streak = 0
        self._plateau_streak = 0
        self._best_so_far: float | None = None

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

    # ---------------- 早停护栏（失败熔断 + 收敛自动停） ----------------
    def _outcome(self, result: str, value: float | None = None) -> None:
        """每次试验完结后统一记账护栏状态。

        连败：failed +1，completed/pruned 清零（剪枝说明流水线本身是通的）；
        无提升：仅 completed 参与——与历史最优比（方向感知），未严格变好 +1，
        变好则刷新基准并清零。阈值在 settings.budget（0/None=关闭）。
        并行 worker 下多线程同时记账，整体持锁（批边界才读取判定）。
        """
        with self._compute_lock:
            if result == "failed":
                self._fail_streak += 1
            else:
                self._fail_streak = 0
            if result == "completed" and value is not None:
                limit = self.settings.budget.auto_stop_plateau or 0
                if limit <= 0:
                    return
                b = self._best_so_far
                if self.settings.metrics.primary.direction == "maximize":
                    improved = b is None or value > b
                else:
                    improved = b is None or value < b
                if improved:
                    self._best_so_far = value
                    self._plateau_streak = 0
                else:
                    self._plateau_streak += 1

    def _fail_streak_hit(self) -> bool:
        limit = self.settings.budget.max_fail_streak
        return limit > 0 and self._fail_streak >= limit

    def _plateau_hit(self) -> bool:
        limit = self.settings.budget.auto_stop_plateau or 0
        return limit > 0 and self._plateau_streak >= limit

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
            self._outcome("completed", value)
            return "completed"
        except optuna.TrialPruned:
            self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            self._outcome("pruned")
            return "pruned"
        except TrialFailedError as e:
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number, source=source,
                                **_fail_fields(e))
            self._progress(trial, "FAILED ", e.reason, time.perf_counter() - t0)
            self._outcome("failed")
            return "failed"
        except Exception as e:   # noqa: BLE001 —— 意外异常（如 python 模式用户函数错误）不炸掉整个搜索
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number,
                                reason=f"未预期异常：{type(e).__name__}: {e}",
                                hint="检查训练脚本 / adapter 实现", source=source)
            self._progress(trial, "FAILED ", f"未预期异常 {type(e).__name__}",
                           time.perf_counter() - t0)
            self._outcome("failed")
            return "failed"

    def run_batch(self, n: int, source: str = "search") -> dict:
        """跑 n 次常规试验（n 受剩余预算钳制，时间预算到点/失败熔断停止派发）。返回统计。"""
        n = min(n, self.budget_left())
        stats = {"ran": 0, "completed": 0, "pruned": 0, "failed": 0}
        if self.workers <= 1:
            for _ in range(n):
                if self._time_exceeded() or self._fail_streak_hit():
                    break
                trial = self.study.ask()
                stats[self._run_one(trial, source)] += 1
                stats["ran"] += 1
            return stats
        # 并行：ask 与 submit 交错（主线程逐个 ask），保证不存在"已 ask 未派发"的孤儿
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = []
            for _ in range(n):
                if self._time_exceeded() or self._fail_streak_hit():
                    break
                futures.append(pool.submit(self._run_one, self.study.ask(), source))
            for f in futures:
                stats[f.result()] += 1
                stats["ran"] += 1
        return stats

    def run_custom(self, params: dict, note: str | None = None,
                   source: str = "custom") -> dict:
        """假设驱动试验（agent source="custom" / 人工插队 source="human"）。
        计入预算，返回结构化结果供工具回喂。"""
        if self.budget_left() <= 0:
            return {"status": "rejected", "reason": "预算已用完，无法再增加试验"}
        trial = self.study.ask()
        t0 = time.perf_counter()
        try:
            return self._run_custom_impl(trial, params, note, t0, source)
        finally:
            self._charge(time.perf_counter() - t0)

    def _run_custom_impl(self, trial, params: dict, note: str | None, t0: float,
                         source: str) -> dict:
        try:
            value = self.runner.run_trial(trial, cfg_override=params, note=note)
            self.study.tell(trial, value)
            self.journal.append(TRIAL_END, trial=trial.number, value=value,
                                params=dict(trial.params), source=source,
                                note=note, duration_s=round(time.perf_counter() - t0, 2))
            self._progress(trial, "COMPLETE",
                           f"{self.settings.metrics.primary.name}={value:.4f}",
                           time.perf_counter() - t0)
            self._outcome("completed", value)
            return {"status": "complete", "trial": trial.number, "value": value}
        except optuna.TrialPruned:
            self.study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._progress(trial, "PRUNED ", "(中途剪枝)", time.perf_counter() - t0)
            self._outcome("pruned")
            return {"status": "pruned", "trial": trial.number}
        except TrialFailedError as e:
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)
            self.journal.append(TRIAL_FAIL, trial=trial.number, source=source,
                                **_fail_fields(e))
            self._progress(trial, "FAILED ", e.reason, time.perf_counter() - t0)
            self._outcome("failed")
            return {"status": "failed", "trial": trial.number, "reason": e.full()}

    # ---------------- 人工试验插队（inbox 队列消费） ----------------
    def inbox_path(self) -> Path:
        """人工试验队列文件：在分区 data_dir 内，run() 批边界消费。"""
        return Path(self.settings.data_dir) / "inbox.jsonl"

    def consume_inbox(self) -> dict:
        """消费人工排队的试验（journal 审计 source=human）。

        用 os.replace 原子认领队列文件再逐条执行：消费期间新追加的条目落在
        新建的 inbox.jsonl，下批再消费。执行不了的条目（预算耗尽 / 数据库被
        运行中搜索占用）原样放回队列，绝不静默丢弃。
        返回 {"consumed": 执行条数, "requeued": 放回条数}。
        """
        path = self.inbox_path()
        if not path.exists():
            return {"consumed": 0, "requeued": 0}
        claimed = path.with_name("inbox.processing.jsonl")
        try:
            os.replace(path, claimed)
        except OSError:
            return {"consumed": 0, "requeued": 0}
        try:
            lines = [ln.strip() for ln in claimed.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
        except OSError:
            return {"consumed": 0, "requeued": 0}
        consumed = 0
        remaining: list[str] = []
        stop = False
        for line in lines:
            if stop:
                remaining.append(line)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                self.log(f"[人工试验] 跳过损坏的排队条目：{line[:120]}")
                continue
            params = entry.get("params") if isinstance(entry, dict) else None
            if not isinstance(params, dict) or not params:
                self.log("[人工试验] 跳过缺少 params 的排队条目")
                continue
            note = str(entry.get("note") or "human")
            self.log(f"[人工试验] 执行人工排队配置：{json.dumps(params, ensure_ascii=False)}")
            try:
                res = self.run_custom(params, note=note, source="human")
            except Exception as e:   # noqa: BLE001 —— 数据库被运行中搜索占用等
                self.log(f"[人工试验] 执行异常，放回队列待下轮：{e}")
                remaining.append(line)
                stop = True
                continue
            if res.get("status") == "rejected":
                self.log(f"[人工试验] 被拒（{res.get('reason')}），放回队列")
                remaining.append(line)
                stop = True
                continue
            consumed += 1
            if res.get("status") == "complete":
                self.log(f"[人工试验] trial#{res.get('trial')} 完成，"
                         f"{self.settings.metrics.primary.name}={res.get('value')}")
            elif res.get("status") == "failed":
                self.log(f"[人工试验] trial#{res.get('trial')} 失败："
                         f"{res.get('reason')}")
        if remaining:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n".join(remaining) + "\n")
            except OSError as e:
                self.log(f"[人工试验] 放回队列失败（{e}）：{len(remaining)} 条条目可能丢失")
        try:
            claimed.unlink()
        except OSError:
            pass
        return {"consumed": consumed, "requeued": len(remaining)}

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
        # 护栏状态每会话重新起算：连败从零计；无提升基准用 study 现有最优
        # （续跑时老最优不算本会话的新提升，首个未超越它的完结试验即开始累计）
        self._fail_streak = 0
        self._plateau_streak = 0
        try:
            self._best_so_far = float(self.study.best_value)
        except ValueError:
            self._best_so_far = None
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
        guardrails = []
        if self.settings.budget.max_fail_streak:
            guardrails.append(f"连续失败 ≥{self.settings.budget.max_fail_streak} 次早停")
        if self.settings.budget.auto_stop_plateau:
            guardrails.append(f"连续 {self.settings.budget.auto_stop_plateau} 次完结试验无提升自动停")
        if guardrails:
            self.log("早停护栏：" + "；".join(guardrails)
                     + "（budget.max_fail_streak / budget.auto_stop_plateau）")
        self.journal.append(SESSION_START, total=self.total, wake_every=wake_every,
                            resume=already > 0, space_version=self.space.version,
                            study_trials=already, workers=self.workers,
                            max_duration_h=hours, cohort=cohort,
                            cohort_fp=cohort_fp, fp_match=fp_match,
                            gpus=self.gpus or None,
                            max_fail_streak=self.settings.budget.max_fail_streak,
                            auto_stop_plateau=self.settings.budget.auto_stop_plateau)
        self.space.snapshot(self.settings.data_dir)

        if self.budget_left() <= 0:
            self.log("预算内的试验已全部完结，无需再跑。")
            self.journal.append(FINISH, reason="budget_exhausted",
                                compute_hours=round(self.compute_hours(), 6))
            self._notify_finish("budget_exhausted")
            return

        try:
            while self.budget_left() > 0 and not self.finished_reason:
                self.consume_inbox()   # 人工插队试验：批边界消费（source=human 审计）
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
                if self._fail_streak_hit():
                    self.log(f"[护栏] 连续 {self._fail_streak} 次试验失败"
                             f"（熔断阈值 {self.settings.budget.max_fail_streak}）："
                             "提前收尾，请检查训练脚本或配置是否有误"
                             "（budget.max_fail_streak=0 可关闭）")
                    self.finished_reason = "fail_streak"
                    break
                if self._plateau_hit():
                    self.log(f"[护栏] 连续 {self._plateau_streak} 次完结试验无提升"
                             f"（阈值 {self.settings.budget.auto_stop_plateau}）：收敛自动停，节省剩余预算")
                    self.finished_reason = "plateau"
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
