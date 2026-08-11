"""RunManager：单运行槽。用子进程驱动 `python cli.py run ...`，stdout 落盘供前端 tail。

设计要点：
- 同一时间只允许一个搜索运行（槽占用时 start 抛错）；
- Windows 下用 CREATE_NEW_PROCESS_GROUP 启动，stop 用 taskkill /F /T 杀整棵进程树
  （cli.py 还会派生训练子进程，只 terminate 父进程会留下孤儿）；
- 日志写入 data_dir/web_run_<时间戳>.log，前端轮询 tail。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


class RunManager:
    def __init__(self, project_root: Path, settings_path: str, space_path: str):
        self.project_root = Path(project_root)
        self.settings_path = settings_path
        self.space_path = space_path
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.args: list[str] = []
        self.log_path: Path | None = None
        self.started_at: str | None = None
        self.exit_code: int | None = None
        self.stopped: bool = False
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
        }

    def start(self, data_dir: str | Path, trials: int | None = None,
              wake_every: int | None = None, no_agent: bool = False,
              fresh: bool = False) -> dict:
        if self.running:
            raise RuntimeError(f"已有搜索在运行（pid={self.pid}），请先停止再启动新任务")
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = data_dir / f"web_run_{ts}.log"
        # -u：stdout 重定向到文件后默认块缓冲，前端要实时看日志必须无缓冲
        cmd = [sys.executable, "-u", str(self.project_root / "cli.py"), "run",
               "--settings", self.settings_path, "--space", self.space_path]
        if trials:
            cmd += ["--trials", str(trials)]
        if wake_every:
            cmd += ["--wake-every", str(wake_every)]
        if no_agent:
            cmd.append("--no-agent")
        if fresh:
            cmd.append("--fresh")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._log_f = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd, stdout=self._log_f, stderr=subprocess.STDOUT,
            cwd=str(self.project_root), **kwargs)
        self.pid = self.proc.pid
        self.args = cmd
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.exit_code = None
        self.stopped = False
        return self.status()

    def stop(self) -> dict:
        if not self.running:
            raise RuntimeError("当前没有正在运行的搜索任务")
        assert self.pid is not None
        if sys.platform == "win32":
            # /T 杀整棵进程树：cli.py 派生的训练子进程是孙进程，只杀父进程会留孤儿
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
