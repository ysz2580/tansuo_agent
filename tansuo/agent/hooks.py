"""权限 hook：工具执行前的钩子链（借鉴常见 agent 框架的 PreToolUse 机制）。

- 每个工具调用依次经过钩子链，任一钩子拒绝即终止（拒绝原因回喂模型）；
- 策略来自 settings.yaml 的 agent.permissions：allow（放行）/ confirm（控制台
  人工确认）/ deny（禁用）；未配置的工具取 default，default 缺省为 allow；
- confirm 在无交互终端（cron/无人值守）下自动拒绝并写审计，避免卡死；
- 所有权限决策写 AGENT_PERMISSION journal 事件，全程可审计。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"


@dataclass
class HookDecision:
    allowed: bool
    policy: str
    reason: str = ""


class PermissionGate:
    """默认权限钩子：settings 驱动的策略查询 + 控制台确认 + 审计。"""

    def __init__(self, permissions: dict | None, journal=None, log=print):
        self.permissions = permissions or {}
        self.journal = journal
        self.log = log

    def policy_for(self, tool_name: str) -> str:
        return self.permissions.get(tool_name,
                                    self.permissions.get("default", ALLOW))

    def check(self, mode: str, tool_name: str, tool_input: dict) -> HookDecision:
        policy = self.policy_for(tool_name)
        if policy == DENY:
            self._audit(mode, tool_name, policy, "denied")
            return HookDecision(False, policy,
                                f"工具 '{tool_name}' 已被 settings agent.permissions 禁用")
        if policy == CONFIRM:
            granted = self._confirm(mode, tool_name, tool_input)
            self._audit(mode, tool_name, policy,
                        "confirmed-yes" if granted else "confirmed-no")
            if not granted:
                return HookDecision(False, policy,
                                    "控制台人工确认未通过（无人值守时可调整 "
                                    "settings agent.permissions 改变该工具策略）")
        return HookDecision(True, policy, "")

    def _confirm(self, mode: str, tool_name: str, tool_input: dict) -> bool:
        if not (sys.stdin and sys.stdin.isatty()):
            self.log(f"[permission] {tool_name} 需要人工确认，"
                     f"但当前无交互终端 → 自动拒绝")
            return False
        preview = str(tool_input)
        if len(preview) > 200:
            preview = preview[:200] + "…"
        try:
            answer = input(f"[permission] {mode} 模式工具 {tool_name} 请求执行，"
                           f"入参：{preview}\n是否放行？[y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    def _audit(self, mode: str, tool_name: str, policy: str, outcome: str) -> None:
        if self.journal is not None:
            from ..journal import AGENT_PERMISSION
            self.journal.append(AGENT_PERMISSION, mode=mode, tool=tool_name,
                                policy=policy, outcome=outcome)


class HookChain:
    """钩子链：任一钩子拒绝即短路。后续可挂载限流、参数守卫等钩子。"""

    def __init__(self, hooks: list | None = None):
        self.hooks = list(hooks or [])

    def add(self, hook) -> "HookChain":
        self.hooks.append(hook)
        return self

    def check(self, mode: str, tool_name: str, tool_input: dict) -> HookDecision:
        for hook in self.hooks:
            decision = hook.check(mode, tool_name, tool_input)
            if not decision.allowed:
                return decision
        return HookDecision(True, ALLOW, "")
