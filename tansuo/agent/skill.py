"""Skill 抽象：把一种 agent 能力封装成可插拔技能包。

一个技能 = 工具集（schema）+ 执行器（dispatch）+ system prompt + 开场消息
+ 限额（turns/工具调用数）+ 结束判定。同一个 AgentLoop 驱动所有技能。

内置技能（tansuo/agent/skills/）：
- tune：超参数搜索监督者（每轮唤醒一次短对话）
- setup：配置生成（读训练脚本 → 起草两份配置 → 探测试验自证）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SkillLimits:
    max_turns: int = 10        # 最多 messages 往返
    max_tool_calls: int = 8    # 最多工具调用次数


class Skill:
    """技能基类。子类实现各方法；AgentLoop 只依赖这个协议。"""

    name: str = "base"
    mode: str = "base"          # journal 里的模式标记
    description: str = ""

    def tools(self) -> list[dict]:
        """工具 schema 列表（Anthropic tool 格式）。"""
        raise NotImplementedError

    def executor(self):
        """返回执行器：须有 dispatch(name, tool_input) -> str。"""
        raise NotImplementedError

    def system_prompt(self) -> str:
        raise NotImplementedError

    def opening_message(self) -> str:
        """开场 user 消息。"""
        raise NotImplementedError

    def limits(self) -> SkillLimits:
        return SkillLimits()

    def done(self) -> bool:
        """技能是否已自主完成（如 finish 工具被调用）。默认永不提前结束。"""
        return False

    def idle_nudge(self) -> str | None:
        """模型停下但技能未完成时，循环追加的提醒文案；None=不提醒直接结束。"""
        return None

    def max_tokens(self) -> int:
        return 2048
