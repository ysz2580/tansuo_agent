"""AgentLoop：统一的 tool-use 循环；技能（Skill）驱动，钩子链把关权限。

循环机制（常见 agent 框架的同构抽象）：
- user 开场消息 → 模型回复 → tool_use 则经权限钩子→执行器→结果回喂 → 直至
  end_turn / 技能完成 / 限额耗尽；
- 限额：max_turns 往返 + max_tool_calls 次工具调用（技能自带）；
- 权限：每次工具调用先过 HookChain（settings 策略 allow/confirm/deny），
  拒绝原因回喂模型；决策写 AGENT_PERMISSION 审计；
- 端点异常抛 AgentEndpointError，由上层（orchestrator）计连续失败并可降级。

上层封装：
- AgentSupervisor：调参技能，每 wake_every 次试验唤醒一轮短对话；
- SetupAgent：配置生成技能，一次性会话。
"""
from __future__ import annotations

from ..journal import AGENT_ERROR, AGENT_TOOL_CALL, AGENT_WAKEUP, FINISH, SESSION_START
from .client import AgentEndpointError, call_with_retry, make_client
from .hooks import HookChain, PermissionGate
from .skill import Skill

_MAX_RESULT_CHARS = 8000


def log_tool_call(journal, mode: str, name: str, tool_input: dict,
                  allowed: bool = True) -> None:
    journal.append(AGENT_TOOL_CALL, mode=mode, tool=name, input=tool_input,
                   allowed=allowed)


class AgentLoop:
    """通用循环：跑完一个技能的一次会话，返回最后一段模型文本。"""

    def __init__(self, settings, client, journal, gate: HookChain,
                 mode: str, log=print):
        self.settings = settings
        self.client = client
        self.journal = journal
        self.gate = gate
        self.mode = mode
        self.log = log

    def run(self, skill: Skill) -> str:
        cfg = self.settings.agent
        executor = skill.executor()
        limits = skill.limits()
        system = skill.system_prompt()
        tools = skill.tools()
        messages: list[dict] = [{"role": "user", "content": skill.opening_message()}]
        nudged = False
        tool_calls = 0
        last_text = ""

        for _turn in range(limits.max_turns):
            if skill.done():
                break
            resp = call_with_retry(
                self.client, model=cfg.model, system=system, tools=tools,
                max_tokens=skill.max_tokens(), messages=messages)
            texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if texts:
                last_text = texts[-1]
                for t in texts:
                    self.log(f"[{skill.name}-agent] {t}")

            if resp.stop_reason != "tool_use" or not tool_uses:
                if not skill.done() and not nudged and skill.idle_nudge():
                    nudged = True                      # 只提醒一次
                    if texts:
                        messages.append({"role": "assistant", "content": resp.content})
                    messages.append({"role": "user", "content": skill.idle_nudge()})
                    continue
                break

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                if tool_calls >= limits.max_tool_calls:
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": "工具调用配额已用完，请直接输出总结/调用 finish。"})
                    continue
                decision = self.gate.check(skill.mode, tu.name, tu.input)
                if not decision.allowed:
                    self.log(f"[permission] 拒绝 {tu.name}：{decision.reason}")
                    log_tool_call(self.journal, skill.mode, tu.name, tu.input, allowed=False)
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": f"权限拒绝：{decision.reason}"})
                    continue
                tool_calls += 1
                log_tool_call(self.journal, skill.mode, tu.name, tu.input)
                text = executor.dispatch(tu.name, tu.input)
                if len(text) > _MAX_RESULT_CHARS:
                    text = text[:_MAX_RESULT_CHARS] + "\n……（输出过长已截断）"
                self.log(f"[{skill.name}-agent] 工具 {tu.name} 返回 {len(text)} 字符")
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": text})
            messages.append({"role": "user", "content": results})

            if tool_calls >= limits.max_tool_calls:
                # 配额耗尽：给一次无工具的收尾机会，让模型输出总结
                messages.append({"role": "user",
                                 "content": "工具调用配额已用完。请直接输出本轮总结，"
                                            "不要再调用工具。"})
                resp = call_with_retry(self.client, model=cfg.model, system=system,
                                       max_tokens=1024, messages=messages)
                for b in resp.content:
                    if b.type == "text" and b.text.strip():
                        last_text = b.text
                        self.log(f"[{skill.name}-agent] {b.text}")
                break
        return last_text


def make_gate(settings, journal, log=print) -> HookChain:
    """按 settings 构造默认钩子链（权限策略钩子）。"""
    return HookChain([PermissionGate(settings.agent.permissions, journal, log=log)])


# ==================================================================
# 调参模式封装：orchestrator 每 wake_every 次试验调用一次 wake()
# ==================================================================

class AgentSupervisor:
    def __init__(self, settings, orchestrator, client=None, gate=None, log=print):
        self.settings = settings
        self.orch = orchestrator
        self.journal = orchestrator.journal
        self.client = client or make_client(settings.agent)
        self.gate = gate or make_gate(settings, self.journal, log=log)
        self.log = log
        self.wake_count = 0

    def wake(self, orchestrator=None) -> None:
        from .skills.tune import TuneSkill
        cfg = self.settings.agent
        self.wake_count += 1
        if self.wake_count > cfg.max_wake_rounds:
            self.log(f"[agent] 唤醒轮数已达上限（{cfg.max_wake_rounds}），本轮跳过，继续巡航")
            return
        self.journal.append(AGENT_WAKEUP, round=self.wake_count,
                            budget_left=self.orch.budget_left(),
                            space_version=self.orch.space.version)
        self.log(f"\n[agent] 第 {self.wake_count} 轮唤醒：分析试验结果……")
        skill = TuneSkill(self.settings, self.orch, self.wake_count)
        loop = AgentLoop(self.settings, self.client, self.journal, self.gate,
                         mode="tune", log=self.log)
        try:
            last_text = loop.run(skill)
        except AgentEndpointError as e:
            self.journal.append(AGENT_ERROR, round=self.wake_count, error=str(e))
            raise
        if self.orch.finished_reason:
            self.log(f"[agent] 已决定结束搜索：{self.orch.finished_reason}")
        self.journal.append(AGENT_WAKEUP, round=self.wake_count, phase="end",
                            note=(last_text or "")[:300])


# ==================================================================
# 配置模式封装：一次性会话，返回 finish 摘要
# ==================================================================

class SetupAgent:
    def __init__(self, settings, journal, settings_path, space_path,
                 train_script_path, client=None, gate=None, log=print):
        self.settings = settings
        self.journal = journal
        self.client = client or make_client(settings.agent)
        self.gate = gate or make_gate(settings, journal, log=log)
        self.settings_path = settings_path
        self.space_path = space_path
        self.train_script_path = train_script_path
        self.log = log

    def run(self) -> str:
        from .skills.config import SetupSkill
        self.journal.append(SESSION_START, mode="setup", train=self.train_script_path)
        skill = SetupSkill(self.settings, self.journal, self.settings_path,
                           self.space_path, self.train_script_path)
        loop = AgentLoop(self.settings, self.client, self.journal, self.gate,
                         mode="setup", log=self.log)
        try:
            loop.run(skill)
        except AgentEndpointError as e:
            self.journal.append(AGENT_ERROR, mode="setup", error=str(e))
            raise
        summary = skill.executor().summary or "（模型未调用 finish；配置可能未完成，请人工检查）"
        self.journal.append(FINISH, mode="setup", reason="setup_done",
                            summary=summary[:500])
        return summary
