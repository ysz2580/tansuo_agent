"""单次试验执行：suggest → 驱动 adapter → 解析协议行 → Optuna 上报/剪枝。

只认 `##TANSUO## {json}` 协议行；脚本的其它 print 输出视为噪音直接忽略。
协议行必须包含 settings.metrics.primary 声明的主指标，否则该试验 FAILED
并返回契约修正提示（面向接入自己训练脚本的用户）。
"""
from __future__ import annotations

import json
import math

import optuna

from .adapter import PROTOCOL_PREFIX, make_adapter
from .config import Settings
from .journal import TRIAL_END, TRIAL_FAIL, TRIAL_PRUNED, TRIAL_START, Journal
from .space import SearchSpace


class TrialFailedError(RuntimeError):
    """试验失败（脚本出错 / 违反协议 / 超时）。reason 面向人类，hint 给修正建议。"""

    def __init__(self, reason: str, hint: str = "", detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.detail = detail

    def full(self) -> str:
        parts = [self.reason]
        if self.hint:
            parts.append(f"建议：{self.hint}")
        if self.detail:
            parts.append(f"细节：{self.detail}")
        return " | ".join(parts)


class _PruneSignal(Exception):
    """内部信号：需要立即剪枝（跳出 adapter 的行读取循环）。"""


def _as_float(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_metric_line(line: str) -> dict | None:
    """解析一行输出。非协议行返回 None；协议行但 JSON 损坏则抛 TrialFailedError。"""
    text = line.strip()
    if not text.startswith(PROTOCOL_PREFIX):
        return None
    body = text[len(PROTOCOL_PREFIX):]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise TrialFailedError(
            "协议行 JSON 解析失败",
            hint="格式应为：##TANSUO## {\"type\":\"epoch\",\"epoch\":N,\"metrics\":{...}}",
            detail=f"{body[:200]}... ({e})")
    if not isinstance(payload, dict):
        raise TrialFailedError("协议行 JSON 必须是对象", hint="最外层应为 {...}")
    return payload


class TrialRunner:
    def __init__(self, settings: Settings, space: SearchSpace, journal: Journal):
        self.settings = settings
        self.space = space
        self.journal = journal
        extra_env: dict[str, str] = {}
        if settings.budget.data_fraction < 1.0:
            extra_env["TANSUO_DATA_FRACTION"] = str(settings.budget.data_fraction)
        self.adapter = make_adapter(settings.adapter, extra_env=extra_env)
        self.primary = settings.metrics.primary.name
        self.direction = settings.metrics.primary.direction

    # ------------------------------------------------------------------
    def run_trial(self, trial, cfg_override: dict | None = None, note: str | None = None) -> float:
        """执行一试验并返回最终目标值。剪枝抛 optuna.TrialPruned，失败抛 TrialFailedError。"""
        if cfg_override is not None:
            errors = self.space.validate_config(cfg_override)
            if errors:
                raise TrialFailedError("自定义试验配置非法：" + "；".join(errors),
                                       hint="先用 get_current_space 查看当前空间再修正取值")
            cfg = self.space.inject(cfg_override, trial)
            trial.set_user_attr("custom", True)
            if note:
                trial.set_user_attr("note", note)
        else:
            cfg = self.space.suggest(trial)
        cfg.setdefault("seed", trial.number)

        self.journal.append(TRIAL_START, trial=trial.number, params=cfg,
                            custom=cfg_override is not None, note=note)
        curve: list[dict] = []
        final_value: dict = {"v": None}

        def _handle_epoch(payload: dict) -> None:
            metrics = payload.get("metrics") or {}
            if not isinstance(metrics, dict):
                raise TrialFailedError("epoch 行的 metrics 必须是对象")
            if self.primary not in metrics:
                raise TrialFailedError(
                    f"协议行缺少主指标 '{self.primary}'",
                    hint=("训练脚本每个 epoch 行的 metrics 必须包含 settings.yaml 声明的"
                          f"主指标 {self.primary}（当前收到键：{sorted(metrics)}）"))
            epoch = int(payload.get("epoch", len(curve) + 1))
            val = _as_float(metrics.get(self.primary))
            if val is None:
                raise TrialFailedError(f"主指标 '{self.primary}' 不是数值：{metrics.get(self.primary)!r}")
            row = {"epoch": epoch}
            for k, v in metrics.items():
                fv = _as_float(v)
                row[k] = fv if fv is not None else v
            curve.append(row)
            trial.set_user_attr("curve", curve)
            trial.report(val, epoch)
            if not math.isfinite(val):        # 发散（NaN/Inf）立即剪枝，不等统计比较
                raise _PruneSignal
            if trial.should_prune():
                raise _PruneSignal

        def _handle_final(payload: dict) -> None:
            metrics = payload.get("metrics") or {}
            v = payload.get("value", metrics.get(self.primary))
            fv = _as_float(v)
            if fv is None:
                raise TrialFailedError(
                    "final 行缺少数值结果",
                    hint="final 行应为 {\"type\":\"final\",\"value\":<float>}，"
                         "或 metrics 里带主指标")
            final_value["v"] = fv

        def _dispatch(payload: dict) -> None:
            ptype = payload.get("type")
            if ptype == "epoch":
                _handle_epoch(payload)
            elif ptype == "final":
                _handle_final(payload)
            # 其它 type 忽略（允许脚本扩展）

        if self.adapter.mode == "subprocess":
            def on_line(line: str) -> None:
                payload = parse_metric_line(line)
                if payload is not None:
                    _dispatch(payload)
            try:
                result = self.adapter.run(cfg, on_line)
            except _PruneSignal:
                self.adapter.kill()
                self.journal.append(TRIAL_PRUNED, trial=trial.number, epochs=len(curve))
                raise optuna.TrialPruned()
            except TrialFailedError:
                self.adapter.kill()
                raise
            if result.timed_out:
                raise TrialFailedError(
                    f"训练超时（>{self.settings.adapter.timeout_s}s）",
                    hint="可减少 epochs/width 或调低 budget.data_fraction；"
                         "也可在 settings.yaml 提高 adapter.timeout_s")
            if result.exit_code != 0:
                raise TrialFailedError(
                    f"训练脚本退出码 {result.exit_code}",
                    hint="查看下方 stderr/stdout 尾部定位脚本错误（若都为空，多为环境瞬时问题，重试即可）",
                    detail=(f"stderr: {result.stderr_tail or '(空)'} | "
                            f"stdout 尾部: {result.stdout_tail[-3:] or '(空)'}"))
            if final_value["v"] is None:
                raise TrialFailedError(
                    "训练结束但未收到 final 协议行",
                    hint=("脚本结束时必须打印 "
                          "##TANSUO## {\"type\":\"final\",\"value\":<float>}"),
                    detail=f"stdout 尾部：{result.stdout_tail[-3:]}")
            return float(final_value["v"])
        else:  # python 函数模式
            def report(epoch: int, metrics: dict) -> bool:
                _dispatch({"type": "epoch", "epoch": epoch, "metrics": metrics})
                return True
            try:
                value = self.adapter.run(cfg, report)
            except _PruneSignal:
                self.journal.append(TRIAL_PRUNED, trial=trial.number, epochs=len(curve))
                raise optuna.TrialPruned()
            if final_value["v"] is not None:
                return float(final_value["v"])
            fv = _as_float(value)
            if fv is None:
                raise TrialFailedError("python 模式函数未返回数值结果")
            return fv
