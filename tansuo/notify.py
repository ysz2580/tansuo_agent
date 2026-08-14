"""会话结束 / agent 降级的 webhook 通知。

调参是长时任务：用户挂机离开后，需要一条推送告诉他"跑完了 / 挂了 / agent
降级了"。通知走通用 HTTP POST（兼容钉钉 / 飞书 / Slack 自定义机器人），
零新依赖（标准库 urllib）。

原则：**通知失败绝不影响搜索本身**——所有对外发送都吞掉异常，只记日志。
"""
from __future__ import annotations

import json
import re
import urllib.request

VALID_FORMATS = ("generic", "dingtalk", "lark", "slack")
VALID_EVENTS = ("session_end", "agent_degrade")

_DEFAULT_TIMEOUT = 10   # 秒；通知是收尾动作，不宜拖太久


def build_payload(fmt: str, text: str) -> dict:
    """按机器人类型组装各自接受的 JSON 信封（纯函数，可单测）。"""
    if fmt == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if fmt == "lark":
        return {"msgtype": "text", "content": {"text": text}}
    # generic / slack 都是 {"text": ...}
    return {"text": text}


def send_webhook(url: str, payload: dict, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """POST JSON 到 webhook。返回 {"ok": bool, "detail": str}，永不抛异常。"""
    if not url:
        return {"ok": False, "detail": "webhook_url 为空"}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            body = r.read(2000).decode("utf-8", "replace")
        return {"ok": 200 <= code < 300, "detail": f"HTTP {code}：{body[:200]}"}
    except Exception as e:   # noqa: BLE001 —— 通知失败不影响搜索
        return {"ok": False, "detail": f"发送失败：{e}"}


def _notify_cfg(settings):
    """settings.notify（缺失时返回 None → 调用方静默跳过）。"""
    return getattr(settings, "notify", None)


def _should_send(cfg, event: str) -> bool:
    return bool(cfg and cfg.enabled and cfg.webhook_url
                and event in (cfg.events or []))


def notify_finish(settings, *, reason: str, finished: int, total: int,
                  best_line: str = "", tokens: dict | None = None,
                  cohort_id: str | None = None, log=print) -> bool:
    """会话结束通知。返回是否成功发送。"""
    cfg = _notify_cfg(settings)
    if not _should_send(cfg, "session_end"):
        return False
    lines = [f"【tansuo 调参结束】{settings.experiment_name}",
             f"结束原因：{reason}",
             f"进度：{finished}/{total} 次试验完结"]
    if cohort_id:
        lines.append(f"记录分区：{cohort_id}")
    if best_line:
        lines.append(f"最优：{best_line}")
    if tokens and tokens.get("total_tokens"):
        lines.append(f"Agent tokens：{tokens['total_tokens']}"
                     f"（in {tokens.get('input_tokens', 0)} / "
                     f"out {tokens.get('output_tokens', 0)}，"
                     f"{tokens.get('rounds', 0)} 轮唤醒）")
    result = send_webhook(cfg.webhook_url, build_payload(cfg.format, "\n".join(lines)))
    log(f"[notify] 会话结束通知{'成功' if result['ok'] else '失败'}：{result['detail']}")
    return result["ok"]


def notify_degrade(settings, *, detail: str = "", log=print) -> bool:
    """agent 连续失败降级为无 agent 巡航时的通知（会话继续）。"""
    cfg = _notify_cfg(settings)
    if not _should_send(cfg, "agent_degrade"):
        return False
    text = (f"【tansuo agent 降级】{settings.experiment_name}\n"
            f"LLM 连续失败达到上限，本会话降级为无 agent 巡航模式（试验继续）。\n"
            f"{detail}".rstrip())
    result = send_webhook(cfg.webhook_url, build_payload(cfg.format, text))
    log(f"[notify] agent 降级通知{'成功' if result['ok'] else '失败'}：{result['detail']}")
    return result["ok"]


# ----------------------------------------------------------------------
# settings.yaml 写回（Web「设置」页保存用；镜像 agent.api_setup.write_back_agent）
# ----------------------------------------------------------------------

def _yaml_scalar(value) -> str:
    """安全写入 YAML 的标量形式：布尔直通；含特殊字符的字符串加双引号。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    value = str(value)
    if re.fullmatch(r"[A-Za-z0-9_\-./:%]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _trailing_comment(rest: str) -> str:
    """行尾注释原样提取（YAML：空白 + # 起注释）；无注释返回空串。"""
    m = re.search(r"\s+#.*$", rest)
    return m.group(0) if m else ""


def write_back_notify(settings_path: str, webhook_url: str | None = None,
                      fmt: str | None = None, events: list | None = None,
                      enabled: bool | None = None) -> dict:
    """把显式给出的字段最小化写回 settings.yaml 的 notify: 块（保留注释，
    含字段行尾注释）。

    未给出（None）的字段一律不动——尤其 webhook_url 留空时保持 ${ENV:...}
    引用，绝不把环境变量里的地址物化进配置文件。
    """
    from pathlib import Path
    path = Path(settings_path)
    fields: dict = {}
    if webhook_url and str(webhook_url).strip():
        fields["webhook_url"] = str(webhook_url).strip()
    if fmt and str(fmt).strip():
        fields["format"] = str(fmt).strip().lower()
    if events is not None:
        fields["events"] = "[" + ", ".join(str(e).strip() for e in events) + "]"
    if enabled is not None:
        fields["enabled"] = bool(enabled)
    if not fields:
        return {"ok": False, "changed": [], "errors": ["没有需要写入的字段"]}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "changed": [], "errors": [f"无法读取 {path}：{e}"]}
    changed: list[str] = []
    errors: list[str] = []
    for name, value in fields.items():
        pattern = re.compile(rf"^(?P<sp>\s*){name}:(?P<rest>.*)$", re.M)
        m = pattern.search(text)
        if not m:
            errors.append(f"settings.yaml 的 notify 块中未找到 {name} 字段所在行，"
                          f"请手动添加后重试")
            continue
        comment = _trailing_comment(m.group("rest"))
        rendered = value if isinstance(value, str) and value.startswith("[") \
            else _yaml_scalar(value)
        text, n = pattern.subn(
            lambda mm, _n=name, _r=rendered, _c=comment:
            f"{mm.group('sp')}{_n}: {_r}{_c}",
            text, count=1)
        if n > 0:
            changed.append(name)
    if changed:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "changed": [], "errors": [f"写入 {path} 失败：{e}"]}
    return {"ok": bool(changed), "changed": changed, "errors": errors}
