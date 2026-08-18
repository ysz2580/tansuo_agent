"""调参技能（tune）：超参数搜索监督者。

工具集 7 个：get_study_summary / get_current_space / get_learning_curves /
edit_search_space / add_custom_trial / run_trials / finish。

执行器原则：
- 一切错误（含护栏/权限拒绝）都作为 tool_result 文本回喂模型，让它自我修正；
- 执行器不抛异常给循环（端点/网络错误属于 loop 层）；
- 每次工具调用写 AGENT_TOOL_CALL journal 事件，全程可审计。
"""
from __future__ import annotations

import json

from ...analysis import learning_curves, recent_failures, summarize
from ...journal import SPACE_PATCH
from ..prompt_store import load_overrides
from ..prompts import (build_context_tuning_system, build_context_tuning_wake,
                       render_prompt)
from ..skill import Skill, SkillLimits

TUNING_TOOLS = [
    {
        "name": "get_study_summary",
        "description": ("获取研究汇总：试验计数、当前最优、top-k 配置、"
                        "top25%/bottom25% 参数分布对比、参数重要度（哪些维度影响最大）、"
                        "收敛信号、剩余预算；若有失败试验，还返回 recent_failures"
                        "（最近失败的原因与类别 timeout/exit_code/protocol 等）。"
                        "每轮唤醒必须第一个调用。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_k": {"type": "integer", "description": "top-k 条数，默认 5，上限 10",
                          "default": 5},
            },
        },
    },
    {
        "name": "get_current_space",
        "description": ("查看当前搜索空间（含每个参数的语义说明、冻结状态、envelope 包络）"
                        "与历史补丁。调用 edit_search_space 之前必须先调用本工具。"),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_learning_curves",
        "description": ("查看逐 epoch 学习曲线（默认 top-3 + 最近 2 次完成试验），"
                        "用于判断欠拟合（还在涨→epochs 不够）/ 发散（loss 尖峰→lr 过大）"
                        " / 已收敛。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "trial_ids": {"type": "array", "items": {"type": "integer"},
                              "description": "指定试验编号；省略则给默认集合"},
            },
        },
    },
    {
        "name": "edit_search_space",
        "description": ("编辑搜索空间（原子提交）。op 类型：narrow（收窄数值边界，须在当前范围内）、"
                        "widen（放宽，不得超出初始 envelope）、freeze（冻结参数到某取值）、"
                        "release（解冻）。注意：分类参数聚焦只能用 freeze，不支持 set_choices。"
                        "必须给出 rationale。每次最多 4 条 op；自由参数不得少于 3 个。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string",
                                   "enum": ["narrow", "widen", "freeze", "release"]},
                            "param": {"type": "string", "description": "参数名"},
                            "low": {"type": "number", "description": "narrow/widen 的新下界"},
                            "high": {"type": "number", "description": "narrow/widen 的新上界"},
                            "value": {"description": "freeze 固定到的取值"},
                        },
                        "required": ["op", "param"],
                    },
                },
                "rationale": {"type": "string", "description": "本次编辑的理由（必填）"},
            },
            "required": ["ops", "rationale"],
        },
    },
    {
        "name": "add_custom_trial",
        "description": ("假设驱动实验：手动指定一组完整超参数立即跑一次（计入总预算）。"
                        "用于验证具体假设，如'top 配置把 lr 再降一半是否更稳'。"
                        "冻结参数可省略（自动注入）。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "params": {"type": "object", "description": "超参数取值字典"},
                "note": {"type": "string", "description": "这次实验想验证的假设（必填）"},
            },
            "required": ["params", "note"],
        },
    },
    {
        "name": "run_trials",
        "description": ("让搜索引擎（TPE）按当前空间继续跑 count 次试验（计入总预算，"
                        "单次上限 8）。编辑空间后应先跑几试验验证方向。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "要跑的试验数（1-8）",
                          "default": 5},
            },
        },
    },
    {
        "name": "finish",
        "description": ("结束本次搜索会话：当判断已收敛（连续 2 轮无改进且 top 配置趋同）"
                        "或继续跑收益极低时调用。调用后触发最终报告生成。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "结束理由（写入报告与 journal）"},
            },
            "required": ["reason"],
        },
    },
]


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


class TuneExecutor:
    """调参技能工具执行器。所有返回都是给模型看的文本。"""

    def __init__(self, orchestrator, log=print):
        self.orch = orchestrator
        self.log = log

    @property
    def space_edits(self) -> int:
        """全会话已发生的空间编辑次数（journal 为单一事实源，跨唤醒/续跑正确）。"""
        return len(self.journal.patches())

    @property
    def settings(self):
        return self.orch.settings

    @property
    def space(self):
        return self.orch.space

    @property
    def study(self):
        return self.orch.study

    @property
    def journal(self):
        return self.orch.journal

    # ------------------------------------------------------------------
    def dispatch(self, name: str, tool_input: dict) -> str:
        """执行一个工具调用；内部捕获一切异常，返回文本结果。"""
        tool_input = tool_input or {}
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"未知工具 '{name}'。可用工具：{[t['name'] for t in TUNING_TOOLS]}"
            return handler(**tool_input)
        except TypeError as e:
            return f"工具 '{name}' 参数不匹配：{e}。请按工具 schema 传参。"
        except Exception as e:   # noqa: BLE001 —— 错误回喂模型而不是炸掉循环
            return f"工具 '{name}' 执行出错：{type(e).__name__}: {e}"

    # ---------------- 只读分析 ----------------
    def _tool_get_study_summary(self, top_k: int = 5) -> str:
        top_k = max(1, min(int(top_k), 10))
        s = summarize(self.study, self.settings, top_k=top_k)
        out = {
            "budget": {"total": self.orch.total,
                       "done": self.orch.finished_count(),
                       "left": self.orch.budget_left()},
            **s,
        }
        fails = recent_failures(self.journal)
        if fails:
            out["recent_failures"] = fails
        return _json(out)

    def _tool_get_current_space(self) -> str:
        lines = [self.space.describe()]
        patches = self.journal.patches()
        if patches:
            lines.append("\n补丁历史：")
            for ev in patches:
                ops = "; ".join(f"{o.get('op')}({o.get('param')})" for o in (ev.get("ops") or []))
                lines.append(f"  v{ev.get('version')} [{ev.get('ts')}] {ops} —— {ev.get('rationale')}")
        else:
            lines.append("\n补丁历史：（无）")
        return "\n".join(lines)

    def _tool_get_learning_curves(self, trial_ids: list | None = None) -> str:
        curves = learning_curves(self.study, trial_ids=trial_ids)
        if not curves:
            return "没有可展示的完成试验。"
        out = []
        for c in curves:
            rows = [f"  epoch {r.get('epoch')}: " +
                    ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                              for k, v in r.items() if k != "epoch")
                    for r in c["curve"]]
            out.append(f"trial#{c['trial']}（最终值 {c['value']:.4f}）\n" + "\n".join(rows))
        return "\n\n".join(out)

    # ---------------- 空间编辑 ----------------
    def _tool_edit_search_space(self, ops: list, rationale: str = "") -> str:
        quota = self.settings.agent.max_space_edits_total
        if self.space_edits >= quota:
            return (f"本会话空间编辑配额已用完（{quota} 次）。"
                    f"改用 add_custom_trial 验证假设，或调用 finish。")
        result = self.space.apply_patch(ops, rationale)
        if not result.ok:
            text = "空间编辑被拒绝：\n- " + "\n- ".join(result.errors)
            if result.hint:
                text += f"\n提示：{result.hint}"
            return text
        self.journal.append(SPACE_PATCH, version=result.new_version,
                            ops=ops, rationale=rationale)
        snap = self.space.snapshot(self.settings.data_dir)
        self.log(f"[agent] 空间已更新 → v{result.new_version}（快照 {snap.name}）：{rationale}")
        return (f"编辑成功，空间版本 → v{result.new_version}"
                f"（本会话编辑配额剩余 {quota - self.space_edits} 次）。"
                f"\n{self.space.describe()}")

    # ---------------- 试验驱动 ----------------
    def _tool_add_custom_trial(self, params: dict, note: str = "") -> str:
        note = str(note or "").strip()
        if not note:
            return "add_custom_trial 必须给出 note（这次实验想验证的假设），便于事后审计。"
        res = self.orch.run_custom(params, note=note)
        if res.get("status") == "complete":
            res["hint"] = (f"trial#{res['trial']} 完成。可再调 get_study_summary 看它是否进入 top。")
        return _json(res)

    def _tool_run_trials(self, count: int = 5) -> str:
        count = max(1, min(int(count), 8))
        if self.orch.budget_left() <= 0:
            return "预算已用完，无法再跑试验。若判断已收敛请调用 finish。"
        stats = self.orch.run_batch(count, source="agent")
        stats["budget_left"] = self.orch.budget_left()
        return _json(stats)

    def _tool_finish(self, reason: str) -> str:
        reason = str(reason or "").strip()
        if not reason:
            return "finish 必须给出 reason。"
        self.orch.finish(reason)
        return f"已记录结束决定：{reason}。剩余批处理完成后将生成最终报告。"


# 兼容旧名
ToolExecutor = TuneExecutor


class TuneSkill(Skill):
    """调参技能：每轮唤醒一次短对话（靠 brief+summary 传状态，不累积长上下文）。
    跨轮记忆（上一轮结论 {{last_note}}）与人→agent 指令（{{guidance}}）随 brief 注入。"""

    name = "tune"
    mode = "tune"
    description = "超参数搜索监督者：分析试验结果、调节搜索空间、提出假设实验、判断收敛"

    def __init__(self, settings, orchestrator, round_no: int,
                 last_note: str = "", guidance: str = ""):
        self.settings = settings
        self.orch = orchestrator
        self.round_no = round_no
        # 跨轮记忆（上一轮结论）与人→agent 指令：由 AgentSupervisor 注入 brief
        self.last_note = last_note
        self.guidance = guidance
        self._executor = TuneExecutor(orchestrator)
        self._prompts = load_overrides(settings)   # prompts.yaml 覆盖（无文件→{}→出厂默认）

    def tools(self) -> list[dict]:
        return TUNING_TOOLS

    def executor(self):
        return self._executor

    def system_prompt(self) -> str:
        return render_prompt("tuning_system",
                             build_context_tuning_system(self.settings, self.orch.space),
                             self._prompts)

    def opening_message(self) -> str:
        ctx = build_context_tuning_wake(self.round_no, self.settings, self.orch,
                                        last_note=self.last_note, guidance=self.guidance)
        text = render_prompt("tuning_wake_brief", ctx, self._prompts)
        # 护栏兜底：用户覆盖的模板若未带 {{wake_signals}}，确定性信号会被静默
        # 丢掉——护栏不允许被模板编辑绕过，缺了就强制追加到末尾。
        signals = ctx.get("wake_signals") or ""
        if signals and signals not in text:
            text += signals
        # 人工指令同等兜底：用户输入不允许被模板编辑静默丢弃（范式同上）
        guidance = ctx.get("guidance") or ""
        if guidance and guidance not in text:
            text += guidance
        return text

    def limits(self) -> SkillLimits:
        cfg = self.settings.agent
        return SkillLimits(max_turns=cfg.max_turns_per_wake,
                           max_tool_calls=cfg.max_tool_calls_per_wake)

    def done(self) -> bool:
        return bool(self.orch.finished_reason)

    def max_tokens(self) -> int:
        return 2048
