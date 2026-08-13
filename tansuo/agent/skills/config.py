"""配置生成技能（setup）：读训练脚本 → 写两份配置（经校验器）→ 探测试验自证可用。

工具集 5 个：read_train_script / save_settings / save_search_space /
run_probe_trial / finish。
"""
from __future__ import annotations

import json

from ..prompt_store import load_overrides
from ..prompts import build_context_setup, render_prompt
from ..skill import Skill, SkillLimits

SETUP_TOOLS = [
    {
        "name": "read_train_script",
        "description": ("读取训练脚本 import 的本地模块或配置文件（主脚本源码已在你的上下文里）。"
                        "只允许读项目内文本文件。"),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件相对路径"}},
            "required": ["path"],
        },
    },
    {
        "name": "save_settings",
        "description": ("写入 settings.yaml。入参是完整配置对象（experiment/metrics/adapter/"
                        "budget/pruner/agent/storage）。写入前经校验器强校验：metrics 必须恰一个 "
                        "primary（direction ∈ maximize/minimize），adapter 必须给出 command 等。"
                        "校验失败会返回错误列表，按提示修正后重试。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "settings": {"type": "object", "description": "完整 settings 配置对象"},
            },
            "required": ["settings"],
        },
    },
    {
        "name": "save_search_space",
        "description": ("写入 search_space.yaml。入参是 params 列表；每个参数必须含 name/type/"
                        "取值范围（choice 给 choices；float/int 给 low/high，可用 log）与中文 "
                        "description。写入前经校验器校验，缺描述或类型非法会被拒绝。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "params": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["choice", "float", "int"]},
                            "choices": {"type": "array"},
                            "low": {"type": "number"},
                            "high": {"type": "number"},
                            "log": {"type": "boolean"},
                            "description": {"type": "string",
                                            "description": "中文语义说明（必填）"},
                        },
                        "required": ["name", "type", "description"],
                    },
                },
            },
            "required": ["params"],
        },
    },
    {
        "name": "run_probe_trial",
        "description": ("用刚写入的配置实跑一次探测试验，验证端到端契约：脚本能读到配置、"
                        "协议行格式正确、primary 指标存在、耗时合理。失败信息（含 stderr 尾部）"
                        "会返回给你：配置问题就改配置重试，脚本问题就写进 finish 摘要。"
                        "探测试验未通过前不要 finish 声称配置完成。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "params": {"type": "object",
                           "description": "可选：手动指定一组取值；省略则由搜索空间采样"},
            },
        },
    },
    {
        "name": "finish",
        "description": ("结束配置会话。summary 必须包含：推断了哪些超参数（名称/类型/范围）、"
                        "哪些指标（名称/方向/主或观测）、探测试验结果、需要人工确认的地方"
                        "（尤其脚本需要补充协议行打印时的具体修改建议）。"),
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "配置推断摘要"}},
            "required": ["summary"],
        },
    },
]


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


class SetupExecutor:
    """配置技能执行器：一切校验复用 config.py / space.py，错误回喂模型。"""

    def __init__(self, settings_path, space_path, train_script_path, journal, log=print):
        self.settings_path = settings_path
        self.space_path = space_path
        self.train_script_path = train_script_path
        self.journal = journal
        self.log = log
        self.summary: str | None = None      # finish 工具写入；非 None 即技能完成

    def dispatch(self, name: str, tool_input: dict) -> str:
        tool_input = tool_input or {}
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"未知工具 '{name}'。可用工具：{[t['name'] for t in SETUP_TOOLS]}"
            return handler(**tool_input)
        except TypeError as e:
            return f"工具 '{name}' 参数不匹配：{e}。请按工具 schema 传参。"
        except Exception as e:   # noqa: BLE001
            return f"工具 '{name}' 执行出错：{type(e).__name__}: {e}"

    # ---------------- 读 ----------------
    def _tool_read_train_script(self, path: str) -> str:
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return f"文件不存在：{path}"
        if not p.is_file():
            return f"不是文件：{path}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"读取失败：{e}"
        limit = 30000
        if len(text) > limit:
            text = text[:limit] + f"\n……（文件过长，截断到 {limit} 字符）"
        return f"===== {path} =====\n{text}"

    # ---------------- 写（经校验器） ----------------
    def _tool_save_settings(self, settings: dict) -> str:
        import os
        import tempfile
        import yaml
        from pathlib import Path
        from ...config import ConfigError, load_settings
        if not isinstance(settings, dict):
            return "settings 必须是对象"
        try:
            text = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
        except yaml.YAMLError as e:
            return f"settings 序列化失败：{e}"
        fd, tmp = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            load_settings(tmp)      # 强校验：抛 ConfigError 则不写目标文件
        except ConfigError as e:
            return f"settings 校验失败（未写入）：{e}"
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        Path(self.settings_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.settings_path).write_text(text, encoding="utf-8")
        return f"settings.yaml 已写入 {self.settings_path}（校验通过）"

    def _tool_save_search_space(self, params: list) -> str:
        from pathlib import Path
        from ...space import SearchSpace, SpaceError
        try:
            space = SearchSpace.from_dict({"params": params})   # 强校验（description 必填等）
        except SpaceError as e:
            return f"搜索空间校验失败（未写入）：{e}"
        Path(self.space_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.space_path).write_text(space.to_yaml(), encoding="utf-8")
        return (f"search_space.yaml 已写入 {self.space_path}"
                f"（{len(space.params)} 个参数，校验通过）\n{space.describe()}")

    # ---------------- 探测试验 ----------------
    def _tool_run_probe_trial(self, params: dict | None = None) -> str:
        import time
        import optuna
        from ...config import load_settings
        from ...runner import TrialFailedError, TrialRunner
        from ...space import SearchSpace
        try:
            settings = load_settings(self.settings_path)
            space = SearchSpace.from_yaml(self.space_path)
        except Exception as e:   # noqa: BLE001
            return f"加载配置失败，先保存配置：{e}"
        runner = TrialRunner(settings, space, self.journal)
        study = optuna.create_study(
            direction="maximize" if settings.metrics.primary.direction == "maximize"
            else "minimize")
        trial = study.ask()
        t0 = time.perf_counter()
        try:
            value = runner.run_trial(trial, cfg_override=params, note="probe")
        except optuna.TrialPruned:
            return ("探测试验被剪枝（主指标出现 NaN/Inf，疑似发散）。"
                    "契约本身是通的；检查默认超参数是否过大（如 lr）。")
        except TrialFailedError as e:
            return f"探测试验失败：{e.full()}"
        curve = trial.user_attrs.get("curve") or []
        return _json({
            "status": "ok",
            "value": value,
            "duration_s": round(time.perf_counter() - t0, 1),
            "params": dict(trial.params),
            "last_epoch": curve[-1] if curve else None,
            "hint": "端到端契约验证通过：配置可读、协议行解析正常、primary 指标存在",
        })

    def _tool_finish(self, summary: str) -> str:
        summary = str(summary or "").strip()
        if not summary:
            return "finish 必须给出 summary（推断了什么、探测试验结果、需人工确认项）。"
        self.summary = summary
        return "配置会话结束。"


class SetupSkill(Skill):
    """配置生成技能：为指定训练脚本起草 settings.yaml 与 search_space.yaml。"""

    name = "setup"
    mode = "setup"
    description = "配置生成：读训练脚本，推断超参数与指标定义，写配置并探测试验自证"

    def __init__(self, settings, journal, settings_path, space_path, train_script_path):
        from pathlib import Path
        self.settings = settings
        self.journal = journal
        self.settings_path = settings_path
        self.space_path = space_path
        self.train_script_path = train_script_path
        self._src = Path(train_script_path).read_text(encoding="utf-8", errors="replace")
        self._prompts = load_overrides(settings)   # prompts.yaml 覆盖（无文件→{}→出厂默认）
        self._executor = SetupExecutor(settings_path, space_path,
                                       train_script_path, journal)

    def tools(self) -> list[dict]:
        return SETUP_TOOLS

    def executor(self):
        return self._executor

    def system_prompt(self) -> str:
        return render_prompt("setup_system",
                             build_context_setup(self.train_script_path, self._src),
                             self._prompts)

    def opening_message(self) -> str:
        return ("请按 system 中的流程为这个训练脚本生成配置："
                "先读脚本（及其依赖的本地模块），再 save_settings 与 "
                "save_search_space，然后 run_probe_trial 验证，最后 finish。")

    def limits(self) -> SkillLimits:
        return SkillLimits(max_turns=20, max_tool_calls=15)

    def done(self) -> bool:
        return self._executor.summary is not None

    def idle_nudge(self) -> str | None:
        return "请调用 finish 工具结束配置会话（summary 必填）。"

    def max_tokens(self) -> int:
        return 3000
