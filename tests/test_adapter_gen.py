"""功能1 回归测试：训练脚本自动适配（write_adapter_script + 探针结构化诊断）。

可直接 `python tests/test_adapter_gen.py` 运行。不依赖 LLM：全部走
SetupExecutor 的确定性工具层。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


SPACE_YAML = """\
params:
  - name: lr
    type: float
    low: 0.01
    high: 0.1
    description: 学习率
"""


def make_env(tmp: Path, train_code: str) -> tuple[Path, Path, Path]:
    """搭一个最小可探针环境：settings + space + 假训练脚本。"""
    tmp.mkdir(parents=True, exist_ok=True)
    train = tmp / "fake_train.py"
    train.write_text(train_code, encoding="utf-8")
    settings_path = tmp / "settings.yaml"
    settings_path.write_text(
        "metrics:\n"
        "  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n"
        "  mode: subprocess\n"
        f"  command: [{json.dumps(sys.executable)}, {json.dumps(str(train))}]\n"
        "  timeout_s: 120\n",
        encoding="utf-8")
    space_path = tmp / "search_space.yaml"
    space_path.write_text(SPACE_YAML, encoding="utf-8")
    return settings_path, space_path, train


def make_executor(tmp: Path, settings_path: Path, space_path: Path, train: Path):
    from tansuo.agent.skills.config import SetupExecutor
    from tansuo.journal import Journal
    return SetupExecutor(settings_path, space_path, train,
                         Journal(tmp / "j.jsonl"), log=lambda *a, **k: None)


def test_write_adapter_script(tmp: Path) -> None:
    print("== write_adapter_script：路径安全与语法检查 ==")
    settings_path, space_path, train = make_env(tmp, "print('x')\n")
    ex = make_executor(tmp, settings_path, space_path, train)

    r = ex.dispatch("write_adapter_script",
                    {"filename": "../evil.py", "content": "print(1)"})
    ok("文件名含路径分隔符被拒", "非法" in r and not (tmp.parent / "evil.py").exists(), r)
    r = ex.dispatch("write_adapter_script",
                    {"filename": "a/b.py", "content": "print(1)"})
    ok("文件名含子目录被拒", "非法" in r, r)
    r = ex.dispatch("write_adapter_script",
                    {"filename": "wrapper.txt", "content": "print(1)"})
    ok("非 .py 后缀被拒", "非法" in r, r)
    r = ex.dispatch("write_adapter_script",
                    {"filename": "wrapper.py", "content": "   "})
    ok("空内容被拒", "不能为空" in r, r)
    r = ex.dispatch("write_adapter_script",
                    {"filename": "wrapper.py", "content": "def broken(:\n"})
    ok("语法错误不落盘", "语法错误" in r and "未写入" in r
       and not (tmp / "wrapper.py").exists(), r)

    good = ("import os\n"
            "cfg = os.environ.get('TANSUO_TRIAL_CONFIG', '{}')\n"
            "print('##TANSUO## {\\\"type\\\": \\\"final\\\", \\\"value\\\": 0.9}')\n")
    r = ex.dispatch("write_adapter_script",
                    {"filename": "adapter_wrapper.py", "content": good})
    p = tmp / "adapter_wrapper.py"
    ok("合法 wrapper 写入 settings 同目录", p.exists() and "adapter.command" in r, r)
    ok("回执提示下一步（command 指向 wrapper + 探针）",
       "adapter_wrapper.py" in r and "run_probe_trial" in r, r)


def test_probe_diagnosis(tmp: Path) -> None:
    print("== 探针结构化诊断：三点契约逐条核对 ==")
    from tansuo.agent.skills.config import SETUP_TOOLS
    names = [t["name"] for t in SETUP_TOOLS]
    ok("工具集扩为 6 个且含 write_adapter_script",
       len(names) == 6 and "write_adapter_script" in names, str(names))

    # 场景 A：脚本跑完但一条协议行都没有 → 契约①②全缺 → 指向 wrapper
    settings_path, space_path, train = make_env(tmp / "a", "print('训练完成')\n")
    r = make_executor(tmp / "a", settings_path, space_path, train)._tool_run_probe_trial()
    d = json.loads(r)
    ok("A：无协议行 → status=failed 且 epoch_lines_received=0",
       d["status"] == "failed" and d["epoch_lines_received"] == 0, r)
    ok("A：诊断指向 write_adapter_script 自动适配",
       "write_adapter_script" in d.get("contract_diagnosis", ""), r)

    # 场景 B：epoch 行正常、只缺 final 行 → 契约①②已通
    code_b = ("print('##TANSUO## {\\\"type\\\": \\\"epoch\\\", \\\"epoch\\\": 1, "
              "\\\"metrics\\\": {\\\"val_acc\\\": 0.5}}')\n")
    settings_path, space_path, train = make_env(tmp / "b", code_b)
    r = make_executor(tmp / "b", settings_path, space_path, train)._tool_run_probe_trial()
    d = json.loads(r)
    ok("B：epoch 行收到 1 条、缺 final → 诊断点名只缺③",
       d["status"] == "failed" and d["epoch_lines_received"] == 1
       and "final" in d.get("contract_diagnosis", ""), r)

    # 场景 C：协议行有但主指标键名对不上
    code_c = ("print('##TANSUO## {\\\"type\\\": \\\"epoch\\\", \\\"epoch\\\": 1, "
              "\\\"metrics\\\": {\\\"acc\\\": 0.5}}')\n"
              "print('##TANSUO## {\\\"type\\\": \\\"final\\\", \\\"value\\\": 0.5}')\n")
    settings_path, space_path, train = make_env(tmp / "c", code_c)
    r = make_executor(tmp / "c", settings_path, space_path, train)._tool_run_probe_trial()
    d = json.loads(r)
    ok("C：主指标键名不符 → 诊断点名 metrics.primary.name / 键名映射",
       d["status"] == "failed" and "主指标" in d.get("contract_diagnosis", ""), r)

    # 场景 D：契约完整 → 探针通过（对照组，证明诊断不干扰正常路径）
    code_d = ("print('##TANSUO## {\\\"type\\\": \\\"epoch\\\", \\\"epoch\\\": 1, "
              "\\\"metrics\\\": {\\\"val_acc\\\": 0.9}}')\n"
              "print('##TANSUO## {\\\"type\\\": \\\"final\\\", \\\"value\\\": 0.9}')\n")
    settings_path, space_path, train = make_env(tmp / "d", code_d)
    r = make_executor(tmp / "d", settings_path, space_path, train)._tool_run_probe_trial()
    d = json.loads(r)
    ok("D：契约完整 → 探针通过", d.get("status") == "ok" and abs(d["value"] - 0.9) < 1e-9, r)


def test_setup_prompt_mentions_wrapper() -> None:
    print("== setup 提示词：适配策略进模板 ==")
    from tansuo.agent.prompts import DEFAULT_PROMPTS, setup_system_prompt
    text = setup_system_prompt("train.py", "print('hi')")
    ok("默认 setup 提示词含 write_adapter_script 策略",
       "write_adapter_script" in text and "适配策略" in text)
    ok("提示词仍含探针结构化诊断指引",
       "contract_diagnosis" in text)
    ok("DEFAULT_PROMPTS 键集不变（未新增模板，仅内容升级）",
       set(DEFAULT_PROMPTS) == {"tuning_system", "tuning_wake_brief", "setup_system"})


if __name__ == "__main__":
    import tempfile
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_write_adapter_script(tmp)
        test_probe_diagnosis(tmp)
    test_setup_prompt_mentions_wrapper()
    print(f"\n全部通过：{PASS} 项断言")
