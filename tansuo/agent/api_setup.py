"""大模型 API 自配置：探测环境 → 验证端点与模型 → 写回 settings.yaml。

`cli.py api` 的流程：
1. 盘点凭据来源（settings / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY）与端点；
2. 用候选模型名逐个做两级探测（ping + tool-use）；
3. 首个通过的模型名写回 settings.yaml 的 agent 段（最小化文本替换，保留注释）；
4. 全部失败且有交互终端时，允许手动输入模型名再试一次。

设计原则：不臆测厂商模型清单——候选只来自"用户配置过的名字"，避免硬编码过期列表。
"""
from __future__ import annotations

import os
import re
import sys

from ..config import ConfigError, Settings, load_settings
from .client import make_client, probe_endpoint


def _mask(token: str) -> str:
    if not token:
        return "(未设置)"
    if len(token) <= 8:
        return "***"
    return token[:4] + "***" + token[-4:]


def _ordered_candidates(*names) -> list[str]:
    seen: list[str] = []
    for n in names:
        n = str(n or "").strip()
        if n and n not in seen:
            seen.append(n)
    return seen


def _write_back(settings_path: str, model: str, base_url: str) -> bool:
    """最小化文本替换写回 agent.model / agent.base_url（保留文件注释）。"""
    from pathlib import Path
    path = Path(settings_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    changed = False
    pattern_model = re.compile(r"^(?P<sp>\s*)model:\s*\S.*$", re.M)
    if pattern_model.search(text):
        text, n = pattern_model.subn(lambda m: f"{m.group('sp')}model: {model}", text, count=1)
        changed = changed or n > 0
    else:
        return False
    if base_url:
        # 保留 ${ENV:...} 形式不动（它已能工作）；只替换写死的 URL 值
        pattern_url = re.compile(r"^(\s*)base_url:\s*(?![$])(\S.*)$", re.M)
        if pattern_url.search(text):
            text, n = pattern_url.subn(
                lambda m: f"{m.group(1)}base_url: {base_url}", text, count=1)
            changed = changed or n > 0
    if changed:
        path.write_text(text, encoding="utf-8")
    return True


def run_api_setup(settings_path: str, model: str | None = None,
                  log=print) -> int:
    """API 自配置主流程。返回退出码（0=配置完成并验证通过）。"""
    try:
        settings = load_settings(settings_path)
    except ConfigError as e:
        log(f"settings 不可用（{e}），使用默认 agent 配置继续探测")
        settings = Settings()

    cfg = settings.agent
    base_url = cfg.base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
    token = (cfg.auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN")
             or os.environ.get("ANTHROPIC_API_KEY") or "")

    log("===== 大模型 API 自配置 =====")
    log(f"端点：{base_url or '(未设置：将用 SDK 默认 api.anthropic.com)'}")
    log(f"凭据：{_mask(token)}（来源：" +
        ("settings.agent.auth_token" if cfg.auth_token else
         "ANTHROPIC_AUTH_TOKEN" if os.environ.get("ANTHROPIC_AUTH_TOKEN") else
         "ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY") else "无") + "）")
    if not token:
        if sys.stdin and sys.stdin.isatty():
            try:
                token = input("请粘贴 API token（输入留空则中止）：").strip()
            except EOFError:
                token = ""
        if not token:
            log("✘ 无可用凭据：设置环境变量 ANTHROPIC_AUTH_TOKEN 后重试，"
                "或在 settings.yaml 的 agent.auth_token 中填写。")
            return 1
        cfg.auth_token = token

    candidates = _ordered_candidates(model, cfg.model)
    if not candidates:
        log("✘ 没有候选模型名：用 --model 指定，或在 settings.yaml 填写 agent.model")
        return 1

    log(f"候选模型：{candidates}")
    client = make_client(cfg)
    chosen = None
    for name in candidates:
        log(f"探测 {name} ……")
        result = probe_endpoint(client, name)
        if result["ok"]:
            log(f"✔ {name}：{result['detail']}")
            chosen = name
            break
        log(f"✘ {name} 失败于 [{result['stage']}]：{result['detail']}")

    if chosen is None and sys.stdin and sys.stdin.isatty():
        try:
            manual = input("内置候选均失败。手动输入一个模型名再试（留空放弃）：").strip()
        except EOFError:
            manual = ""
        if manual:
            result = probe_endpoint(client, manual)
            if result["ok"]:
                log(f"✔ {manual}：{result['detail']}")
                chosen = manual
            else:
                log(f"✘ {manual} 失败于 [{result['stage']}]：{result['detail']}")

    if chosen is None:
        log("✘ 没有找到可用模型。请确认端点支持的模型名后重试（cli.py api --model <名字>）。")
        return 1

    if chosen != cfg.model or (base_url and "${ENV:" not in (cfg.base_url or "")):
        if _write_back(settings_path, chosen, base_url):
            log(f"已写回 {settings_path}：agent.model = {chosen}")
        else:
            log(f"⚠ 自动写回失败（文件缺少 model 字段？）：请手动把 agent.model 设为 {chosen}")
    else:
        log(f"settings 中 agent.model={chosen} 已是可用配置，无需修改。")
    log("API 自配置完成。下一步可运行 `python cli.py run`。")
    return 0
