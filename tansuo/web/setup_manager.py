"""SetupManager：单配置槽。用子进程驱动 `python cli.py setup ...`，stdout 落盘供前端 tail。

设计要点（与 run_manager.py 同构）：
- 同一时间只允许一个配置会话；与 RunManager 硬互斥（app._busy_reason 统一裁决）；
- Windows 下用 CREATE_NEW_PROCESS_GROUP 启动，stop 用 taskkill /F /T 杀整棵进程树；
- 日志写入 data_dir/web_setup_<时间戳>.log，前端轮询 tail；
- 记录本次会话的 settings_path/project_dir/data_dir，供 /api/setup/events 定位
  setup_journal.jsonl。注意：必须用启动时绑定的 data_dir，不能事后按 settings
  重新解析——setup agent 会覆写 settings.yaml，其 data_dir 解释可能与启动时
  不同，而 journal 实际写在启动时解析出的目录里（cmd_setup 开头读取 settings）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


class SetupManager:
    def __init__(self, project_root: Path):
        # project_root 仅用于定位 cli.py（代码安装目录，恒定）；
        # train/settings/space/project_dir 在 start() 时由调用方传入。
        self.project_root = Path(project_root)
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.args: list[str] = []
        self.log_path: Path | None = None
        self.started_at: str | None = None
        self.exit_code: int | None = None
        self.stopped: bool = False
        self.project_dir: Path | None = None      # 本次配置的项目目录（子进程 cwd）
        self.settings_path: str | None = None     # 本次配置的 settings（定位 journal 用）
        self.train_script: str | None = None
        self.data_dir: Path | None = None         # 本次配置的运行时数据目录（journal 所在）
        self._log_f = None

    @property
    def running(self) -> bool:
        if self.proc is None:
            return False
        code = self.proc.poll()
        if code is not None and self.exit_code is None:
            self.exit_code = code
            if self._log_f:
                try:
                    self._log_f.close()
                except OSError:
                    pass
                self._log_f = None
        return code is None

    def status(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "args": self.args,
            "log_path": self.log_path.as_posix() if self.log_path else None,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "stopped": self.stopped,
            "project_dir": str(self.project_dir) if self.project_dir else None,
            "settings_path": self.settings_path,
            "train_script": self.train_script,
            "data_dir": str(self.data_dir) if self.data_dir else None,
        }

    def start(self, train_script: str, settings_path: str, space_path: str,
              project_dir: str | Path, data_dir: str | Path) -> dict:
        if self.running:
            raise RuntimeError(f"已有配置会话在运行（pid={self.pid}），请等待结束")
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir          # journal（setup_journal.jsonl）所在目录
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = data_dir / f"web_setup_{ts}.log"
        # -u：stdout 重定向到文件后默认块缓冲，前端要实时看日志必须无缓冲
        cmd = [sys.executable, "-u", str(self.project_root / "cli.py"), "setup",
               "--train", train_script,
               "--settings", settings_path, "--space", space_path]
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._log_f = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd, stdout=self._log_f, stderr=subprocess.STDOUT,
            cwd=str(project_dir), **kwargs)   # cwd=项目目录：与 cmd_setup 的相对路径语义一致
        self.pid = self.proc.pid
        self.args = cmd
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.exit_code = None
        self.stopped = False
        self.project_dir = Path(project_dir)
        self.settings_path = settings_path
        self.train_script = train_script
        return self.status()

    def stop(self) -> dict:
        if not self.running:
            raise RuntimeError("当前没有正在运行的配置会话")
        assert self.pid is not None
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)],
                           capture_output=True)
        else:
            assert self.proc is not None
            self.proc.terminate()
        self.stopped = True
        return self.status()

    def log_tail(self, tail: int = 200) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-tail:])
