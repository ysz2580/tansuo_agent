"""提示词管理与迭代测试：模板渲染 / prompts.yaml 覆盖 / 版本与回滚 / Skill 接线。

独立脚本直跑：python tests/test_prompts.py

说明：默认 wake brief 自 STAR 030 起含 {{last_note}}（跨轮记忆）与
{{guidance}}（人→agent 指令）；本套件聚焦渲染引擎、存储、回滚与 Skill 接线的行为。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

from tansuo.agent.prompt_store import (PromptStoreError, load_doc,       # noqa: E402
                                       load_overrides, resolve_prompts_path,
                                       save_override)
from tansuo.agent.prompts import (DEFAULT_PROMPTS, PROMPT_NAMES,          # noqa: E402
                                  PROMPT_VARS, build_context_setup,
                                  build_context_tuning_system,
                                  build_context_tuning_wake, render_prompt,
                                  setup_system_prompt, tuning_system_prompt,
                                  tuning_wake_brief)

from test_cohort import SPACE_DICT, expect_error, make_settings, ok      # noqa: E402


class _Space:
    """最小空间替身：只提供 describe()。"""

    def describe(self) -> str:
        return "- lr(float 0.01~0.1)：学习率"

    version = 2


class _Orch:
    def __init__(self, space):
        self.space = space
        self.total = 30

    def finished_count(self):
        return 7

    def budget_left(self):
        return 23


def _mk_settings(tmp: Path, name: str):
    return make_settings(tmp, name, script_text=None, direction="maximize")


# ---------------------------------------------------------------- 渲染引擎
def test_render_engine():
    print("== 渲染引擎 ==")
    ctx = {"a": 1, "b": "文本"}
    ok("占位符按上下文替换",
       render_prompt("tuning_system", ctx,
                     {"tuning_system": "x={{a}} y={{b}}"}) == "x=1 y=文本")
    ok("未知占位符原样保留（便于预览排查）",
       render_prompt("tuning_system", ctx,
                     {"tuning_system": "keep {{nope}} and {{a}}"}) == "keep {{nope}} and 1")
    ok("空覆盖回落出厂默认（空上下文时输出即默认模板）",
       render_prompt("tuning_system", {}, {"tuning_system": ""})
       == DEFAULT_PROMPTS["tuning_system"])
    ok("用户文本里的字面花括号不被误伤",
       render_prompt("tuning_system", {},
                     {"tuning_system": "JSON {\"k\": 1} 与 {{a}}"}) == 'JSON {"k": 1} 与 {{a}}')
    ok("PROMPT_VARS 与三条提示词一一对应",
       set(PROMPT_VARS) == set(PROMPT_NAMES) and all(PROMPT_VARS[n] for n in PROMPT_NAMES))


def test_default_content():
    print("== 默认模板内容 ==")
    from tansuo.config import (AgentCfg, BudgetCfg, MetricSpec, MetricsCfg,
                               Settings)
    s = Settings(experiment_name="exp_x",
                 metrics=MetricsCfg(primary=MetricSpec("val_acc", "maximize"),
                                    watch=[MetricSpec("val_loss", "minimize")]),
                 agent=AgentCfg(max_wake_rounds=6),
                 budget=BudgetCfg(total_trials=30))
    sp = _Space()
    sysp = tuning_system_prompt(s, sp)
    ok("默认 system 注入实验名/指标块/空间描述/总预算",
       "exp_x" in sysp and "val_acc" in sysp and "学习率" in sysp
       and "总预算 30 次试验" in sysp)
    ok("system 无残留占位符", "{{" not in sysp)
    brief = tuning_wake_brief(2, s, _Orch(sp))
    ok("唤醒简报注入轮次/完成/剩余/版本/上一轮结论占位",
       brief == "第 2 轮唤醒（最多 6 轮）。"
                "已完成 7/30 次试验，"
                "剩余预算 23 次，当前空间版本 v2。"
                "上一轮你的结论：（首轮，尚无上一轮结论）。"
                "请先调用 get_study_summary 分析，再决定本轮动作；"
                "本轮决策应与上一轮结论保持连贯（避免反复横跳），"
                "除非新证据推翻它。")
    ok("简报无残留占位符", "{{" not in brief)
    ctx2 = build_context_tuning_wake(2, s, _Orch(sp),
                                     last_note="lr=0.05 收敛偏慢",
                                     guidance="优先探更小的 lr")
    ok("last_note 原样注入、guidance 带 👤 前缀",
       ctx2["last_note"] == "lr=0.05 收敛偏慢"
       and ctx2["guidance"] == "\n👤 用户指令：\n优先探更小的 lr")
    ok("无 guidance 时为空串（简报不出现悬空段落）",
       build_context_tuning_wake(2, s, _Orch(sp))["guidance"] == "")
    setupp = setup_system_prompt("train.py", "print('hi')")
    ok("setup 注入脚本路径与源码", "train.py" in setupp and "print('hi')" in setupp)
    ok("上下文构建键与 PROMPT_VARS 一致",
       set(build_context_tuning_system(s, sp)) == set(PROMPT_VARS["tuning_system"])
       and set(build_context_tuning_wake(1, s, _Orch(sp))) == set(PROMPT_VARS["tuning_wake_brief"])
       and set(build_context_setup("p", "s")) == set(PROMPT_VARS["setup_system"]))


# ---------------------------------------------------------------- 存储与回滚
def test_store(tmp: Path):
    print("== prompts.yaml 存储 ==")
    s = _mk_settings(tmp, "ps")
    ok("无文件时 load_overrides 为空（全默认）", load_overrides(s) == {})
    ok("无文件时 load_doc 版本为 0", load_doc(s)["version"] == 0
       and load_doc(s)["history"] == [])
    ok("resolve_prompts_path 是 settings 的同目录 prompts.yaml",
       resolve_prompts_path(s) == Path(s.source_path).with_name("prompts.yaml"))

    r1 = save_override(s, "tuning_system", "自定义：先压 lr 上界。", "发散试验偏多")
    ok("首次保存 version=1 且落盘", r1["version"] == 1
       and resolve_prompts_path(s).exists())
    ok("保存后 load_overrides 读到覆盖",
       load_overrides(s)["tuning_system"] == "自定义：先压 lr 上界。")
    ok("history 记录 which/rationale/source/text/hash",
       r1["entry"]["which"] == "tuning_system"
       and r1["entry"]["rationale"] == "发散试验偏多"
       and r1["entry"]["source"] == "web"
       and r1["entry"]["text"] == "自定义：先压 lr 上界。"
       and len(r1["entry"]["hash"]) == 12)

    r2 = save_override(s, "tuning_system", "再改：widen epochs。", "全部欠拟合")
    ok("二次保存 version 递增且历史追加",
       r2["version"] == 2 and len(load_doc(s)["history"]) == 2)
    ok("回滚：历史第 1 条 text 即旧版可载入",
       load_doc(s)["history"][0]["text"] == "自定义：先压 lr 上界。"
       and load_doc(s)["history"][1]["text"] == "再改：widen epochs。")

    r3 = save_override(s, "tuning_wake_brief", "", "恢复出厂")
    ok("空文本保存=恢复出厂且仍计版本留痕",
       r3["version"] == 3 and load_overrides(s).get("tuning_wake_brief", "") == ""
       and load_doc(s)["history"][-1]["which"] == "tuning_wake_brief")

    expect_error("rationale 为空被拒", PromptStoreError,
                 save_override, s, "tuning_system", "x", "   ")
    expect_error("未知提示词名被拒", PromptStoreError,
                 save_override, s, "nope", "x", "理由")
    expect_error("超长文本被拒", PromptStoreError,
                 save_override, s, "tuning_system", "a" * 20001, "理由")


def test_store_programmatic():
    print("== 程序化 Settings 容错 ==")
    from tansuo.config import Settings
    s = Settings()   # 无 source_path
    ok("无 source_path 时 load_overrides 返回空（不崩）", load_overrides(s) == {})
    expect_error("无 source_path 时保存被拒并给出可读原因", PromptStoreError,
                 save_override, s, "tuning_system", "x", "理由")


# ---------------------------------------------------------------- Skill 接线
def test_skill_wiring(tmp: Path):
    print("== Skill 接线 ==")
    from tansuo.agent.skills.tune import TuneSkill
    from tansuo.config import (AgentCfg, BudgetCfg, MetricSpec, MetricsCfg,
                               Settings)
    s = Settings(experiment_name="wired",
                 metrics=MetricsCfg(primary=MetricSpec("val_acc", "maximize")),
                 agent=AgentCfg(), budget=BudgetCfg(total_trials=30),
                 source_path=str(tmp / "wired_settings.yaml"))
    (tmp / "wired_settings.yaml").write_text("placeholder", encoding="utf-8")
    orch = _Orch(_Space())

    sk_default = TuneSkill(s, orch, 1)
    ok("无 prompts.yaml 时 system 为出厂默认",
       "监督者 agent" in sk_default.system_prompt()
       and "{{" not in sk_default.system_prompt())
    ok("无 prompts.yaml 时开场消息为默认简报",
       sk_default.opening_message().startswith("第 1 轮唤醒"))

    save_override(s, "tuning_system", "【自定义监督策略】总预算 {{total_trials}}。",
                  "测试覆盖")
    save_override(s, "tuning_wake_brief", "轮 {{round_no}} 冲！", "测试覆盖")
    sk_ovr = TuneSkill(s, orch, 3)   # 重新构造以重读覆盖
    ok("有覆盖时 system 用自定义模板并填充上下文",
       sk_ovr.system_prompt() == "【自定义监督策略】总预算 30。")
    ok("有覆盖时开场消息用自定义模板并填充上下文",
       sk_ovr.opening_message() == "轮 3 冲！")


if __name__ == "__main__":
    test_render_engine()
    test_default_content()
    with tempfile.TemporaryDirectory() as td:
        test_store(Path(td))
    test_store_programmatic()
    with tempfile.TemporaryDirectory() as td:
        test_skill_wiring(Path(td))
    from test_cohort import PASS as _P
    print(f"\n全部通过：{_P} 项断言")
