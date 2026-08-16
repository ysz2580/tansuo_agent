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

    def agent_token_summary(self) -> dict:
        """本分区调参会话的累计 token 用量（来自 agent_wakeup end 事件审计）。

        每轮 end 事件带当轮增量 input_tokens/output_tokens；跨进程续跑时
        supervisor 的会话累计会清零，故汇总一律按轮次增量求和。
        老分区（用量审计引入前）→ 各项 0。
        """
        ends = [e for e in self.agent_events() if e.get("phase") == "end"]
        ti = sum(int(e.get("input_tokens") or 0) for e in ends)
        to = sum(int(e.get("output_tokens") or 0) for e in ends)
        return {"rounds": len(ends), "input_tokens": ti, "output_tokens": to,
                "total_tokens": ti + to}


def compute_cost(events: list[dict]) -> dict:
    """累计算力成本：Σ(完结试验耗时) × slots ÷ 3600。

    slots = 最近一次会话声明的 GPU 数（SESSION_START.gpus），无 GPU 时为 1
    （此时单位语义为"机时"）。注意这是近似口径：历史会话若用过不同卡数，
    统一按最近会话的 slots 折算——精确到会话的归属需要逐事件分段，成本
    统计只做量级参考，不做计费级精度。
    返回 {compute_hours, slots, gpus}。
    """
    gpus: list = []
    for e in events:
        if e.get("kind") == SESSION_START:
            g = e.get("gpus")
            gpus = list(g) if isinstance(g, list) else []
    slots = len(gpus) if gpus else 1
    total_s = 0.0
    for e in events:
        if e.get("kind") == TRIAL_END and isinstance(e.get("duration_s"), (int, float)):
            total_s += float(e["duration_s"])
    return {"compute_hours": round(total_s * slots / 3600.0, 4),
            "slots": slots, "gpus": gpus}
