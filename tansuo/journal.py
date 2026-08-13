"""JSONL 事件流：试验、空间补丁、agent 行为全程可审计，支持断点分析。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

# 事件类型
SESSION_START = "session_start"
TRIAL_START = "trial_start"
TRIAL_END = "trial_end"
TRIAL_PRUNED = "trial_pruned"
TRIAL_FAIL = "trial_fail"
TRIAL_RETRY = "trial_retry"
SPACE_PATCH = "space_patch"
WARM_START = "warm_start"
AGENT_WAKEUP = "agent_wakeup"
AGENT_TOOL_CALL = "agent_tool_call"
AGENT_PERMISSION = "agent_permission"
AGENT_ERROR = "agent_error"
FINISH = "finish"


class Journal:
    """追加式 JSONL 日志。每行一个事件，立即 flush（崩溃不丢数据）。
    append 持锁写入，多线程并行试验下保证行完整性。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, kind: str, **fields) -> dict:
        rec = {"schema_version": SCHEMA_VERSION, "kind": kind,
               "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        return rec

    def load_events(self) -> list[dict]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue   # 容错：跳过损坏行（如崩溃时写了一半）
        return events

    def trials(self) -> list[dict]:
        return [e for e in self.load_events()
                if e.get("kind") in (TRIAL_END, TRIAL_PRUNED, TRIAL_FAIL)]

    def patches(self) -> list[dict]:
        return [e for e in self.load_events() if e.get("kind") == SPACE_PATCH]

    def agent_events(self) -> list[dict]:
        return [e for e in self.load_events()
                if str(e.get("kind", "")).startswith("agent_")]
