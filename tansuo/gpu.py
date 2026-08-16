"""GPU 探测（nvidia-smi）：Web 端选卡与试验子进程 CUDA_VISIBLE_DEVICES 注入。

探测轻量、带缓存、失败静默——没有 GPU / 驱动异常时返回空列表，
绝不阻塞主流程（调参在纯 CPU 机器上同样是完整可用的）。
"""
from __future__ import annotations

import subprocess
import time

_CACHE: tuple[float, list[dict]] | None = None
_CACHE_TTL = 10.0   # 秒：前端多处轮询都打 nvidia-smi 太浪费


def query_gpus(timeout: float = 5.0, refresh: bool = False) -> list[dict]:
    """列出 NVIDIA GPU：[{index, name, memory_used_mb, memory_total_mb, utilization}]。

    nvidia-smi 不存在 / 执行失败 / 输出异常 → 返回 []（视为无 GPU 环境）。
    refresh=True 绕过缓存（前端"刷新"按钮用）。
    """
    global _CACHE
    now = time.monotonic()
    if not refresh and _CACHE is not None and now - _CACHE[0] < _CACHE_TTL:
        return _CACHE[1]
    gpus: list[dict] = []
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=timeout)
        if out.returncode == 0:
            for line in out.stdout.decode("utf-8", errors="replace").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 5:
                    continue
                try:
                    gpus.append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "memory_used_mb": int(float(parts[2])),
                        "memory_total_mb": int(float(parts[3])),
                        "utilization": int(float(parts[4])),
                    })
                except ValueError:
                    continue
    except (OSError, subprocess.TimeoutExpired):
        pass
    _CACHE = (now, gpus)
    return gpus


def parse_gpu_ids(text: str) -> list[int]:
    """解析 CLI/前端传来的 GPU 列表（"0,1,3"）；非法格式抛 ValueError。"""
    ids: list[int] = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            raise ValueError(f"GPU 序号非法：'{part}'（应为逗号分隔的非负整数，如 0,1）")
        if v < 0:
            raise ValueError(f"GPU 序号不能为负：{v}")
        if v not in ids:
            ids.append(v)
    return ids
