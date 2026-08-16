"""单次试验执行：suggest → 驱动 adapter → 解析协议行 → Optuna 上报/剪枝。

只认 `##TANSUO## {json}` 协议行；脚本的其它 print 输出视为噪音直接忽略。
协议行必须包含 settings.metrics.primary 声明的主指标，否则该试验 FAILED
并返回契约修正提示（面向接入自己训练脚本的用户）。
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

import optuna

from .adapter import PROTOCOL_PREFIX, make_adapter
from .config import Settings
from .journal import (TRIAL_END, TRIAL_FAIL, TRIAL_PRUNED, TRIAL_RETRY,
                      TRIAL_START, Journal)
from .space import SearchSpace


class TrialFailedError(RuntimeError):
    """试验失败（脚本出错 / 违反协议 / 超时）。reason 面向人类，hint 给修正建议。

    env_clues：瞬时故障（非零退出码且 stderr 为空）时采集的环境线索
    （磁盘余量/安全软件进程等），随 TRIAL_FAIL 事件落 journal，
    供 STAR #004 那类"单独复现正常"的疑难问题事后定位根因。
    """

    def __init__(self, reason: str, hint: str = "", detail: str = "",
                 env_clues: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.detail = detail
        self.env_clues = env_clues

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


# ------------------------------------------------------------------
# 瞬时故障环境线索（STAR #004 那类"退出码非零、stderr 为空、单独复现正常"
# 的疑难问题，根因多与安全软件/磁盘等环境因素有关）。采集轻量、永不阻塞：
# 任何一步失败就省略对应字段，绝不影响试验本身。
# ------------------------------------------------------------------

# 常见安全软件进程名（小写）。国内环境重点：360/火绒/腾讯电脑管家/金山；
# 其余为国际常见产品。Defender（msmpeng.exe）在 Windows 上几乎必然在列，
# 记录它用于排除"无第三方杀软"的场景。
_KNOWN_SECURITY_PROCS = (
    "360tray.exe", "360sd.exe", "zhudongfangyu.exe", "360rp.exe", "qhsafetray.exe",
    "hipsdaemon.exe", "hipstray.exe",                       # 火绒
    "qqpctray.exe", "qqpcrtp.exe", "qqprotect.exe",        # 腾讯电脑管家
    "kxetray.exe", "ksafetray.exe",                        # 金山毒霸
    "msmpeng.exe", "securityhealthsystray.exe",            # Windows Defender
    "avp.exe", "klnagent.exe",                             # Kaspersky
    "egui.exe", "ekrn.exe",                                # ESET
    "avastsvc.exe", "avastui.exe", "aswidsagent.exe",      # Avast
    "avgui.exe", "avgsvcx.exe",                            # AVG
    "mbamservice.exe", "mbamtray.exe",                     # Malwarebytes
)
_SECURITY_CACHE: tuple[float, list[str]] | None = None
_SECURITY_CACHE_TTL = 300.0   # 秒：安全软件清单短期内不变，避免每试验都跑 tasklist


def _scan_security_procs(timeout: float = 5.0) -> list[str]:
    """列出正在运行的已知安全软件进程（仅 Windows；带缓存；失败返回 []）。"""
    global _SECURITY_CACHE
    if sys.platform != "win32":
        return []
    now = time.monotonic()
    if _SECURITY_CACHE is not None and now - _SECURITY_CACHE[0] < _SECURITY_CACHE_TTL:
        return _SECURITY_CACHE[1]
    found: list[str] = []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=timeout)
        # 字节模式解码：tasklist 输出是系统 ANSI 码页（简中为 GBK），
        # text=True 在 UTF-8 模式 Python 下会解码失败；进程名都是 ASCII，
        # errors=replace 不影响匹配。
        low = out.stdout.decode("utf-8", errors="replace").lower()
        found = sorted(n for n in _KNOWN_SECURITY_PROCS if n in low)
    except (OSError, subprocess.TimeoutExpired):
        pass
    _SECURITY_CACHE = (now, found)
    return found


def collect_env_clues(cwd: str | Path | None = None) -> dict:
    """瞬时故障时的环境快照（写入 trial_retry / trial_fail 事件供事后诊断）。"""
    clues: dict = {}
    try:
        du = shutil.disk_usage(str(cwd or Path.cwd()))
        clues["disk_free_gb"] = round(du.free / 2**30, 2)
    except OSError:
        pass
    try:
        sec = _scan_security_procs()
        if sec:
            clues["security_procs"] = sec
    except Exception:   # noqa: BLE001 —— 线索采集永不影响试验
        pass
    return clues


class TrialRunner:
    def __init__(self, settings: Settings, space: SearchSpace, journal: Journal,
                 extra_env: dict | None = None):
        self.settings = settings
        self.space = space
        self.journal = journal
        env: dict[str, str] = {}
        if settings.budget.data_fraction < 1.0:
            env["TANSUO_DATA_FRACTION"] = str(settings.budget.data_fraction)
        if extra_env:   # 调用方覆盖优先（毕业赛强制全量数据、GPU 选择等）
            env.update({k: str(v) for k, v in extra_env.items()})
        # adapter 按试验创建（并行时每个线程持有独立实例；构造廉价，进程 run() 时才 spawn）
        self._extra_env = env
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
        adapter = make_adapter(self.settings.adapter, extra_env=self._extra_env)

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

        if adapter.mode == "subprocess":
            def on_line(line: str) -> None:
                payload = parse_metric_line(line)
                if payload is not None:
                    _dispatch(payload)
            attempts = self.settings.adapter.retry_on_fail + 1
            result = None
            for attempt in range(1, attempts + 1):
                try:
                    result = adapter.run(cfg, on_line)
                except _PruneSignal:
                    adapter.kill()
                    self.journal.append(TRIAL_PRUNED, trial=trial.number, epochs=len(curve))
                    raise optuna.TrialPruned()
                except TrialFailedError:
                    adapter.kill()
                    raise
                # 可重试的瞬时故障：非零退出码 且 stderr 为空 且未超时
                # （超时/协议错误/stderr 有内容都是确定性失败，重试无益）
                transient = (result.exit_code != 0 and not result.timed_out
                             and not (result.stderr_tail or "").strip())
                if transient and attempt < attempts:
                    self.journal.append(TRIAL_RETRY, trial=trial.number, attempt=attempt,
                                        reason=f"退出码 {result.exit_code} 且 stderr 为空，判定瞬时故障",
                                        env_clues=collect_env_clues())
                    curve.clear()
                    final_value["v"] = None
                    continue
                break
            if result.timed_out:
                raise TrialFailedError(
                    f"训练超时（>{self.settings.adapter.timeout_s}s）",
                    hint="可减少 epochs/width 或调低 budget.data_fraction；"
                         "也可在 settings.yaml 提高 adapter.timeout_s")
            if result.exit_code != 0:
                retried = attempt - 1   # 实际发生的重试次数（确定性失败可能为 0）
                # stderr 为空 = 瞬时故障形态：采环境线索落 journal 供根因诊断
                transient_like = not (result.stderr_tail or "").strip()
                raise TrialFailedError(
                    f"训练脚本退出码 {result.exit_code}"
                    + (f"（已自动重试 {retried} 次仍失败）" if retried else ""),
                    hint="查看下方 stderr/stdout 尾部定位脚本错误（若都为空，多为环境瞬时问题，"
                         "可在 settings.yaml 设置 adapter.retry_on_fail 自动重试）",
                    detail=(f"stderr: {result.stderr_tail or '(空)'} | "
                            f"stdout 尾部: {result.stdout_tail[-3:] or '(空)'}"),
                    env_clues=collect_env_clues() if transient_like else None)
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
                value = adapter.run(cfg, report)
            except _PruneSignal:
                self.journal.append(TRIAL_PRUNED, trial=trial.number, epochs=len(curve))
                raise optuna.TrialPruned()
            if final_value["v"] is not None:
                return float(final_value["v"])
            fv = _as_float(value)
            if fv is None:
                raise TrialFailedError("python 模式函数未返回数值结果")
            return fv
