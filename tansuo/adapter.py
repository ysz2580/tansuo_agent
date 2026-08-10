"""训练驱动适配器：子进程 CLI 模式（推荐）与 Python 函数模式。

子进程协议契约（写进 README）：
- 配置经 env TANSUO_TRIAL_CONFIG(JSON 字符串) 或 TANSUO_CONFIG_FILE(临时文件路径) 传入；
- 训练脚本每完成一个评估步打印一行 `##TANSUO## {"type":"epoch","epoch":N,"metrics":{...}}`；
- 结束打印 `##TANSUO## {"type":"final","value":<float>,...}` 并以退出码 0 结束；
- 非零退出 / 超时 / 缺 final 行 → 该试验记 FAILED（不中断搜索）。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .config import AdapterCfg

PROTOCOL_PREFIX = "##TANSUO## "
ENV_CONFIG = "TANSUO_TRIAL_CONFIG"
ENV_CONFIG_FILE = "TANSUO_CONFIG_FILE"

# Windows 实时性与编码三件套（子进程注入）
_CHILD_ENV = {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class AdapterError(RuntimeError):
    """适配器层错误（配置无法传入、入口加载失败等）。"""


@dataclass
class RunResult:
    """子进程运行结果。stdout 已通过 on_line 实时回调，这里保留尾部诊断信息。"""
    exit_code: int
    timed_out: bool = False
    stderr_tail: str = ""
    stdout_tail: list[str] = field(default_factory=list)


class SubprocessAdapter:
    mode = "subprocess"

    def __init__(self, cfg: AdapterCfg, extra_env: dict | None = None):
        if not cfg.command:
            raise AdapterError("subprocess 模式必须提供 adapter.command")
        self.cfg = cfg
        self.extra_env = dict(extra_env or {})
        self._proc: subprocess.Popen | None = None

    def _build_env(self, params: dict, config_file: str | None) -> dict:
        env = dict(os.environ)
        env.update(_CHILD_ENV)
        env.update(self.extra_env)
        if config_file:
            env[ENV_CONFIG_FILE] = config_file
            env.pop(ENV_CONFIG, None)
        else:
            env[ENV_CONFIG] = json.dumps(params, ensure_ascii=False)
            env.pop(ENV_CONFIG_FILE, None)
        return env

    def run(self, params: dict, on_line: Callable[[str], None]) -> RunResult:
        """启动子进程跑一次试验；stdout 逐行实时回调 on_line。"""
        config_file = None
        if self.cfg.config_via == "file":
            fd, config_file = tempfile.mkstemp(prefix="tansuo_cfg_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False)
        try:
            env = self._build_env(params, config_file)
            t0 = time.perf_counter()
            self._proc = subprocess.Popen(
                list(self.cfg.command), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, shell=False, text=True, encoding="utf-8", errors="replace")
            timed_out = {"v": False}

            def _watchdog():
                time.sleep(max(1.0, self.cfg.timeout_s - (time.perf_counter() - t0)))
                if self._proc and self._proc.poll() is None:
                    timed_out["v"] = True
                    self._proc.kill()

            wd = threading.Thread(target=_watchdog, daemon=True)
            wd.start()

            stdout_tail: list[str] = []
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                stdout_tail.append(line.rstrip("\n"))
                if len(stdout_tail) > 200:
                    stdout_tail.pop(0)
                on_line(line)
            self._proc.wait()
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            return RunResult(exit_code=self._proc.returncode or 0,
                             timed_out=timed_out["v"],
                             stderr_tail=(stderr or "")[-2000:],
                             stdout_tail=stdout_tail[-20:])
        finally:
            self._proc = None
            if config_file:
                try:
                    os.remove(config_file)
                except OSError:
                    pass

    def kill(self) -> None:
        """剪枝时提前终止子进程。"""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass


class PythonFnAdapter:
    """同进程函数模式：entry = "module.path:函数名"，函数签名
    run_trial(config: dict, report: Callable[[int, dict], bool]) -> float | dict。
    report(epoch, metrics) 返回 False 表示已被剪枝，函数应立即 return。
    """
    mode = "python"

    def __init__(self, cfg: AdapterCfg):
        if ":" not in cfg.entry:
            raise AdapterError(f"adapter.entry 格式应为 'module.path:函数名'，实际 '{cfg.entry}'")
        module_path, fn_name = cfg.entry.rsplit(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise AdapterError(f"无法导入模块 {module_path}：{e}") from e
        fn = getattr(module, fn_name, None)
        if not callable(fn):
            raise AdapterError(f"模块 {module_path} 中没有可调用对象 {fn_name}")
        self.fn = fn

    def run(self, params: dict, report: Callable[[int, dict], bool]) -> float:
        result = self.fn(dict(params), report)
        if isinstance(result, dict):
            if "value" not in result:
                raise AdapterError("python 模式函数返回 dict 时必须含 'value' 键")
            return float(result["value"])
        return float(result)


def make_adapter(cfg: AdapterCfg, extra_env: dict | None = None):
    if cfg.mode == "subprocess":
        return SubprocessAdapter(cfg, extra_env=extra_env)
    if cfg.mode == "python":
        return PythonFnAdapter(cfg)
    raise AdapterError(f"未知 adapter.mode：{cfg.mode}")
