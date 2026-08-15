"""setup 技能两条确定性护栏的单测（不依赖 LLM）：

1. save_settings 整体覆写时保留既有「环境字段」（data_dir/storage.url/agent 端点），
   防止 LLM 重写丢掉脚手架的 .tansuo 隔离路径；timeout_s 棘轮不下调；
2. run_probe_trial 的超时校准 _calibrate_timeout：按「探针耗时 × 空间最重配置
   折算 × 3 倍余量」回写 adapter.timeout_s，绝不下调、超限给 warning。

背景：真实 LLM 验收中 setup agent 起草 epochs∈[5,50] 而 timeout 沿用默认 300s，
正式搜索成片超时；且重写 settings 丢 data_dir 导致数据逃出 .tansuo/。见 STAR #021。

独立脚本直跑：python tests/test_setup_guard.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import yaml                                                       # noqa: E402

from tansuo.agent.skills.config import SetupExecutor              # noqa: E402
from tansuo.space import SearchSpace                              # noqa: E402

from test_cohort import ok                                        # noqa: E402


def _mk_executor(tmp: Path) -> SetupExecutor:
    tmp.mkdir(parents=True, exist_ok=True)
    sp = tmp / ".tansuo" / "settings.yaml"
    space = tmp / ".tansuo" / "search_space.yaml"
    train = tmp / "train.py"
    train.write_text("print('hi')", encoding="utf-8")
    return SetupExecutor(str(sp), str(space), str(train), journal=None, log=lambda *a: None)


def _valid_settings(**over) -> dict:
    base = {
        "experiment": {"name": "t"},
        "metrics": {"primary": {"name": "val_acc", "direction": "maximize"}},
        "adapter": {"mode": "subprocess", "command": ["python", "train.py"],
                    "timeout_s": 300},
    }
    base.update(over)
    return base


def test_merge_env_fields(tmp: Path):
    print("== save_settings 保留环境字段 ==")
    ex = _mk_executor(tmp)
    sp = Path(ex.settings_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    # 既有配置：含 .tansuo 隔离路径与端点信息
    sp.write_text(yaml.safe_dump({
        "experiment": {"name": "old", "data_dir": ".tansuo/data",
                       "dataset": ["mnist"]},
        "metrics": {"primary": {"name": "val_acc", "direction": "maximize"}},
        "adapter": {"mode": "subprocess", "command": ["python", "train.py"],
                    "timeout_s": 300},
        "agent": {"model": "qwen3-max", "base_url": "${ENV:ANTHROPIC_BASE_URL:}",
                  "auth_token": "${ENV:ANTHROPIC_AUTH_TOKEN:}"},
        "storage": {"url": "sqlite:///.tansuo/data/tansuo.db"},
    }, allow_unicode=True), encoding="utf-8")

    # LLM 重写时漏掉 data_dir/storage.url/agent 端点 → 应自动补回
    new = _valid_settings()
    r = ex._tool_save_settings(new)
    ok("回执提示已保留环境字段", "保留既有环境字段" in r and "experiment.data_dir" in r, r)
    written = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("data_dir 被保留（.tansuo 隔离不破坏）",
       written["experiment"]["data_dir"] == ".tansuo/data")
    ok("storage.url 被保留", written["storage"]["url"] == "sqlite:///.tansuo/data/tansuo.db")
    ok("agent.base_url 被保留", written["agent"]["base_url"] == "${ENV:ANTHROPIC_BASE_URL:}")
    ok("dataset 被保留", written["experiment"]["dataset"] == ["mnist"])

    # LLM 显式给出的值不被覆盖
    new2 = _valid_settings()
    new2["experiment"] = {"name": "t", "data_dir": "custom/data"}
    ex._tool_save_settings(new2)
    written2 = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("LLM 显式 data_dir 不被旧值覆盖", written2["experiment"]["data_dir"] == "custom/data")


def test_timeout_ratchet(tmp: Path):
    print("== save_settings timeout 棘轮 ==")
    ex = _mk_executor(tmp)
    sp = Path(ex.settings_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(yaml.safe_dump(_valid_settings(
        adapter={"mode": "subprocess", "command": ["python", "train.py"],
                 "timeout_s": 2000}), allow_unicode=True), encoding="utf-8")
    # LLM 重写时给了更低的 timeout → 应保持不低于原值
    new = _valid_settings()   # timeout_s=300
    ex._tool_save_settings(new)
    written = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("timeout_s 不被下调（保持 2000）", written["adapter"]["timeout_s"] == 2000,
       str(written["adapter"].get("timeout_s")))
    # LLM 给更高值 → 允许上调
    new2 = _valid_settings(adapter={"mode": "subprocess",
                                    "command": ["python", "train.py"], "timeout_s": 3000})
    ex._tool_save_settings(new2)
    written2 = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("timeout_s 允许上调（3000）", written2["adapter"]["timeout_s"] == 3000)


def _space_with_epochs(high: int) -> SearchSpace:
    return SearchSpace.from_dict({"params": [
        {"name": "lr", "type": "float", "low": 1e-4, "high": 0.1, "log": True,
         "description": "学习率"},
        {"name": "epochs", "type": "int", "low": 5, "high": high,
         "description": "训练轮数"},
    ]})


def _space_with_iter(name: str, high: int) -> SearchSpace:
    return SearchSpace.from_dict({"params": [
        {"name": "lr", "type": "float", "low": 1e-4, "high": 0.1, "log": True,
         "description": "学习率"},
        {"name": name, "type": "int", "low": 5, "high": high,
         "description": "训练轮数"},
    ]})


def test_calibrate_timeout(tmp: Path):
    print("== 探针超时校准 ==")
    ex = _mk_executor(tmp)
    sp = Path(ex.settings_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(yaml.safe_dump(_valid_settings(), allow_unicode=True), encoding="utf-8")

    space = _space_with_epochs(high=50)
    # 探针 epochs=10 耗时 184.5s → 折算 184.5×(50/10)×3=2767.5→2770
    info = ex._calibrate_timeout(space, {"epochs": 10, "lr": 0.01}, 184.5)
    ok("校准触发并回写", info is not None and info["recommended_timeout_s"] == 2770,
       str(info))
    written = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("settings 里 timeout_s 已提高到 2770", written["adapter"]["timeout_s"] == 2770)
    ok("未达上限不报 capped", info["capped"] is False)

    # 再次校准（已是 2770）不应下调
    info2 = ex._calibrate_timeout(space, {"epochs": 10}, 10.0)
    written2 = yaml.safe_load(sp.read_text(encoding="utf-8"))
    ok("校准绝不下调（保持 2770）", written2["adapter"]["timeout_s"] == 2770
       and info2["action"].startswith("unchanged"), str(info2))

    # 极端空间 → 触顶 7200 并给 warning
    sp.write_text(yaml.safe_dump(_valid_settings(), allow_unicode=True), encoding="utf-8")
    big = _space_with_epochs(high=500)
    info3 = ex._calibrate_timeout(big, {"epochs": 10}, 300.0)
    ok("超限触顶 7200 且带 warning", info3["recommended_timeout_s"] == 7200
       and info3["capped"] and "warning" in info3, str(info3))


def test_calibrate_iter_param(tmp: Path):
    print("== 超时校准泛化（step/iter 自动识别 + iter_param 显式指定） ==")
    # 1) step 命名自动识别（不局限于 epoch）
    ex = _mk_executor(tmp / "p1")
    sp = Path(ex.settings_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(yaml.safe_dump(_valid_settings(), allow_unicode=True), encoding="utf-8")
    info = ex._calibrate_timeout(_space_with_iter("steps", 50), {"steps": 10}, 184.5)
    ok("step 命名的轮数维度自动识别并折算（184.5×5×3→2770）",
       info is not None and info["iter_param"] == "steps"
       and info["recommended_timeout_s"] == 2770, str(info))

    # 2) iter 命名自动识别
    ex2 = _mk_executor(tmp / "p2")
    sp2 = Path(ex2.settings_path)
    sp2.parent.mkdir(parents=True, exist_ok=True)
    sp2.write_text(yaml.safe_dump(_valid_settings(), allow_unicode=True), encoding="utf-8")
    info2 = ex2._calibrate_timeout(_space_with_iter("n_iters", 50), {"n_iters": 10}, 184.5)
    ok("iter 命名的轮数维度自动识别",
       info2 is not None and info2["iter_param"] == "n_iters", str(info2))

    # 3) adapter.iter_param 显式指定优先（参数名无任何线索也能命中）
    ex3 = _mk_executor(tmp / "p3")
    sp3 = Path(ex3.settings_path)
    sp3.parent.mkdir(parents=True, exist_ok=True)
    cfg = _valid_settings()
    cfg["adapter"]["iter_param"] = "passes"
    sp3.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    info3 = ex3._calibrate_timeout(_space_with_iter("passes", 50), {"passes": 10}, 184.5)
    ok("显式 iter_param 优先（名字无线索也折算 2770）",
       info3 is not None and info3["iter_param"] == "passes"
       and info3["recommended_timeout_s"] == 2770, str(info3))

    # 4) 无任何轮数维度 → ratio=1 按探针耗时校准（不报错）
    ex4 = _mk_executor(tmp / "p4")
    sp4 = Path(ex4.settings_path)
    sp4.parent.mkdir(parents=True, exist_ok=True)
    sp4.write_text(yaml.safe_dump(_valid_settings(), allow_unicode=True), encoding="utf-8")
    lr_only = SearchSpace.from_dict({"params": [
        {"name": "lr", "type": "float", "low": 1e-4, "high": 0.1, "log": True,
         "description": "学习率"}]})
    info4 = ex4._calibrate_timeout(lr_only, {"lr": 0.01}, 100.0)
    ok("无轮数维度时 ratio=1 校准不报错",
       info4 is not None and info4["iter_param"] is None
       and info4["recommended_timeout_s"] == 300, str(info4))


def test_setup_context_existing(tmp: Path):
    print("== setup 提示词注入现有配置 ==")
    from tansuo.agent.prompts import build_context_setup, setup_system_prompt
    ctx = build_context_setup("train.py", "print('hi')", "experiment: {name: x}")
    ok("现有配置进入上下文", ctx["existing_settings"] == "experiment: {name: x}")
    ctx2 = build_context_setup("train.py", "print('hi')", None)
    ok("无现有配置给占位文案", "全新起草" in ctx2["existing_settings"])
    p = setup_system_prompt("train.py", "print('hi')")
    ok("system 含超时校准纪律且无残留占位符",
       "超时校准" in p and "{{" not in p)


def main() -> int:
    import test_cohort
    with tempfile.TemporaryDirectory(prefix="tansuo_setup_guard_") as d:
        tmp = Path(d)
        test_merge_env_fields(tmp)
        test_timeout_ratchet(tmp)
        test_calibrate_timeout(tmp)
        test_calibrate_iter_param(tmp)
        test_setup_context_existing(tmp)
    print(f"\n全部通过：{test_cohort.PASS} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
