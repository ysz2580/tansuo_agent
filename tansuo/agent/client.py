"""Anthropic 兼容端点客户端：工厂 + 两级连通探测 + 指数退避重试。

两级探测（cli.py check）：
  1) ping：最小 messages 请求，验证端点/鉴权/模型名可达；
  2) tool-use：带一个微型工具的强制调用，验证端点支持 function calling
     （DashScope 等兼容端点此项能力可能有差异，必须提前暴露）。
"""
from __future__ import annotations

import time

import anthropic

from ..config import AgentCfg

# 可重试的传输层/限流错误；4xx（模型名错误、请求非法）不重试
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)

_PROBE_TOOL = {
    "name": "echo",
    "description": "连通性探测工具：原样返回输入。",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要回显的文本"}},
        "required": ["text"],
    },
}


class AgentEndpointError(RuntimeError):
    """端点不可用（含两级探测的失败阶段信息）。"""

    def __init__(self, message: str, stage: str = ""):
        super().__init__(message)
        self.stage = stage


def make_client(cfg: AgentCfg) -> anthropic.Anthropic:
    """构造客户端。空字符串字段不显式传入，交给 SDK 读环境变量。"""
    kwargs: dict = {"timeout": 120.0, "max_retries": 0}   # 重试由 call_with_retry 统一管
    if cfg.auth_token:
        kwargs["auth_token"] = cfg.auth_token
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return anthropic.Anthropic(**kwargs)


def call_with_retry(client, *, model: str, max_retries: int = 3,
                    backoff: float = 2.0, **kwargs) -> anthropic.types.Message:
    """messages.create 带指数退避重试（仅传输层/限流/5xx）。"""
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return client.messages.create(model=model, **kwargs)
        except _RETRYABLE as e:
            last = e
            if attempt < max_retries:
                time.sleep(backoff * (2 ** attempt))
    raise AgentEndpointError(f"LLM 端点重试 {max_retries} 次后仍失败：{last}")


def probe_endpoint(client, model: str) -> dict:
    """两级探测。返回 {ok, stage, detail}；stage ∈ ping/tool_use/done。"""
    # ---- 第 1 级：ping ----
    try:
        resp = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "连通性测试，回复 ok 即可。"}])
    except anthropic.NotFoundError as e:
        return {"ok": False, "stage": "ping",
                "detail": f"模型名 '{model}' 不存在或端点路径错误（404）：{e}。"
                          f"请改 agent.model 或 ANTHROPIC_BASE_URL。"}
    except anthropic.AuthenticationError as e:
        return {"ok": False, "stage": "ping",
                "detail": f"鉴权失败：{e}。检查 ANTHROPIC_AUTH_TOKEN。"}
    except Exception as e:   # noqa: BLE001 —— 探测要吞所有异常并给出可读信息
        return {"ok": False, "stage": "ping", "detail": f"{type(e).__name__}: {e}"}

    # ---- 第 2 级：tool-use ----
    try:
        resp = client.messages.create(
            model=model, max_tokens=128,
            tools=[_PROBE_TOOL], tool_choice={"type": "any"},
            messages=[{"role": "user", "content": "请调用 echo 工具，参数 text 填 ok。"}])
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "stage": "tool_use",
                "detail": f"基础对话可用，但 tool-use 请求被拒绝：{type(e).__name__}: {e}。"
                          f"可先用 --no-agent 巡航。"}
    tool_used = any(b.type == "tool_use" for b in resp.content)
    if not tool_used:
        return {"ok": False, "stage": "tool_use",
                "detail": "端点返回了响应但没有产生 tool_use 块（stop_reason="
                          f"{resp.stop_reason}），该端点可能不支持 function calling。"
                          "可先用 --no-agent 巡航。"}
    return {"ok": True, "stage": "done", "detail": "ping 与 tool-use 均通过"}
