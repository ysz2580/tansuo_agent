"""agent 内核：client（端点）/ hooks（权限）/ skill（技能）/ loop（统一循环）。

内置技能见 tansuo/agent/skills/（tune=调参监督，setup=配置生成）。
"""
from .hooks import HookChain, PermissionGate
from .loop import AgentLoop, AgentSupervisor, SetupAgent, make_gate
from .skill import Skill, SkillLimits

__all__ = ["HookChain", "PermissionGate", "AgentLoop", "AgentSupervisor",
           "SetupAgent", "make_gate", "Skill", "SkillLimits"]
