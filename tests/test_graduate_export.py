"""功能6+7 回归测试：毕业赛（隔离复验）与配置回写（preview/export）。

可直接 `python tests/test_graduate_export.py` 运行。不依赖 LLM / 真实 GPU。
"""
from __future__ import annotations

import json
import sys
import tempfile
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
  - name: batch_size
    type: int
    low: 16
    high: 64
    description: 批大小
  - name: epochs
    type: int
    low: 1
    high: 9
    description: 训练轮数
"""


def make_env(tmp: Path, train_code: str) -> tuple:
    tmp.mkdir(parents=True, exist_ok=True)
    train = tmp / "fake_train.py"
    train.write_text(train_code, encoding="utf-8")
    settings_path = tmp / "settings.yaml"
    settings_path.write_text(
        "experiment:\n"
        f"  data_dir: {json.dumps(str(tmp))}\n"
        "metrics:\n"
        "  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n"
        "  mode: subprocess\n"
        f"  command: [{json.dumps(sys.executable)}, {json.dumps(str(train))}]\n"
        "  timeout_s: 120\n"
        "budget:\n"
        "  total_trials: 4\n"
        "  wake_every: 4\n"
        "  data_fraction: 0.3\n", encoding="utf-8")
    space_path = tmp / "search_space.yaml"
    space_path.write_text(SPACE_YAML, encoding="utf-8")
    return settings_path, space_path


def _build(tmp: Path, train_code: str):
    import optuna
    from tansuo.config import load_settings
    from tansuo.journal import Journal
    from tansuo.runner import TrialRunner
    from tansuo.space import SearchSpace
    settings_path, space_path = make_env(tmp, train_code)
    settings = load_settings(settings_path)
    space = SearchSpace.from_yaml(space_path)
    journal = Journal(tmp / "journal.jsonl")
    runner = TrialRunner(settings, space, journal)
    study = optuna.create_study(direction="maximize")
    return settings, space, journal, runner, study


def test_find_iter_param(tmp: Path) -> None:
    print("== find_iter_param：训练轮数维度识别 ==")
    from tansuo.graduate import find_iter_param
    settings, space, _, _, _ = _build(tmp / "iter", "print('x')\n")
    ok("按关键词自动识别 epochs", find_iter_param(space, settings) == "epochs")
    settings.adapter.iter_param = "batch_size"   # 显式声明优先（即使是批大小）
    ok("adapter.iter_param 显式声明优先", find_iter_param(space, settings) == "batch_size")
    settings.adapter.iter_param = "no_such"      # 声明了但空间里没有 → 退回猜测
    ok("声明不存在时退回关键词猜测", find_iter_param(space, settings) == "epochs")


# 训练脚本：value = 数据比例 × 10 + epochs —— 可同时验证全量注入与轮数拉满
_GRAD_CODE = (
    "import os, json\n"
    "cfg = json.loads(os.environ.get('TANSUO_TRIAL_CONFIG', '{}'))\n"
    "frac = float(os.environ.get('TANSUO_DATA_FRACTION', '0') or '0')\n"
    "print('##TANSUO## ' + json.dumps({'type': 'final', "
    "'value': frac * 10.0 + float(cfg.get('epochs', 0))}))\n")


def test_graduate(tmp: Path) -> None:
    print("== graduate：隔离复验 + 全量数据 + 轮数拉满 ==")
    from tansuo.graduate import graduate, graduation_path, load_graduation
    d = tmp / "grad"
    settings, space, journal, runner, study = _build(d, _GRAD_CODE)

    # 主搜索跑两次（value = 0.3×10 + epochs ∈ [1,9]）
    for _ in range(2):
        t = study.ask()
        study.tell(t, runner.run_trial(t))
    best = study.best_trial
    trials_before = len(study.trials)

    result = graduate(settings, space, study, journal, log=lambda *a, **k: None)
    ok("毕业赛完成（status=ok）", result["status"] == "ok", str(result))
    # 关键 1：全量数据注入（value 的 frac×10 部分应为 10 而非 3）
    ok("TANSUO_DATA_FRACTION=1.0 强制注入", result["value"] >= 10.0, str(result["value"]))
    # 关键 2：epochs 拉到空间上界 9
    ok("epochs 拉到空间上界", result["params"]["epochs"] == 9
       and result["iter_param"]["after"] == 9, str(result.get("iter_param")))
    ok("best_trial 溯源正确", result["best_trial"] == best.number)
    # 关键 3：隔离——主 study 试验数不变
    ok("主 study 未被污染（试验数不变）", len(study.trials) == trials_before,
       f"{trials_before} → {len(study.trials)}")
    # 落盘：graduation.yaml + journal 事件
    ok("graduation.yaml 已写入", graduation_path(settings).exists())
    loaded = load_graduation(settings)
    ok("yaml 读回一致", loaded and loaded["value"] == result["value"])
    evs = [e for e in journal.load_events() if e.get("kind") == "graduation"]
    ok("journal 有 graduation 事件（含 verdict）",
       len(evs) == 1 and evs[0].get("verdict") in ("pass", "regressed"), str(evs))


def test_graduate_failure(tmp: Path) -> None:
    print("== graduate：复验失败也如实落盘 ==")
    from tansuo.graduate import graduate, load_graduation
    d = tmp / "grad_fail"
    settings, space, journal, runner, study = _build(d, "print('无协议行')\n")
    # 用合规脚本完成一次主搜索（伪造出 best）
    good = ("import json\n"
            "print('##TANSUO## ' + json.dumps({'type': 'final', 'value': 0.5}))\n")
    good_script = d / "good.py"
    good_script.write_text(good, encoding="utf-8")
    settings.adapter.command[-1] = str(good_script)
    t = study.ask()
    study.tell(t, runner.run_trial(t))
    # 毕业赛换回坏脚本（复验时脚本行为变了 → 失败如实记录）
    settings.adapter.command[-1] = str(d / "fake_train.py")
    result = graduate(settings, space, study, journal, log=lambda *a, **k: None)
    ok("复验失败 status=failed 且带 reason",
       result["status"] == "failed" and result.get("reason"), str(result))
    ok("失败结果同样落 graduation.yaml",
       (load_graduation(settings) or {}).get("status") == "failed")


def test_export(tmp: Path) -> None:
    print("== export_config：preview 不落盘、apply 备份+回写 ==")
    import optuna
    from tansuo.export_config import ExportError, export, preview

    # 带真实参数的 best trial（经 ask 固定分布）
    study = optuna.create_study(direction="maximize")
    t = study.ask({"lr": optuna.distributions.FloatDistribution(0.01, 0.1),
                   "batch_size": optuna.distributions.IntDistribution(16, 64)})
    study.tell(t, 0.95)
    best = study.best_trial

    target = tmp / "user_cfg.yaml"
    target.write_text("lr: 0.5\nmomentum: 0.9\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    # preview：变更清单正确、绝不落盘
    pv = preview(target, study)
    ok("preview：同名键覆盖进 changed（lr 旧值保留）",
       any(c["key"] == "lr" and c["old"] == 0.5 for c in pv["changed"]), str(pv["changed"]))
    ok("preview：异名键进 appended（batch_size）",
       any(a["key"] == "batch_size" for a in pv["appended"]), str(pv["appended"]))
    ok("preview 不落盘", target.read_text(encoding="utf-8") == before)
    ok("preview 全文含未改动键（momentum 保留）",
       "momentum" in pv["merged_text"] and "batch_size" in pv["merged_text"])

    # apply：备份 + 写入
    rs = export(target, study)
    ok("apply 写入成功且带备份路径", rs["applied"] and rs["backup"].endswith(".bak"))
    bak = tmp / "user_cfg.yaml.bak"
    ok("备份内容 = 写入前原文", bak.exists()
       and bak.read_text(encoding="utf-8") == before)
    import yaml as _y
    new = _y.safe_load(target.read_text(encoding="utf-8"))
    ok("回写后 lr=best 值、momentum 保留、batch_size 追加",
       new["lr"] == best.params["lr"] and new["momentum"] == 0.9
       and new["batch_size"] == best.params["batch_size"], str(new))

    # JSON 目标同样支持
    tj = tmp / "user_cfg.json"
    tj.write_text(json.dumps({"lr": 0.1}), encoding="utf-8")
    rsj = export(tj, study)
    newj = json.loads(tj.read_text(encoding="utf-8"))
    ok("JSON 目标回写正确", rsj["format"] == "json"
       and newj["lr"] == best.params["lr"], str(newj))

    # 边界：无 best / 目标不存在 / 后缀不支持
    empty = optuna.create_study(direction="maximize")
    try:
        preview(target, empty)
        ok("空 study 被拒", False)
    except ExportError as e:
        ok("空 study 被拒", "没有完成的试验" in str(e), str(e))
    try:
        preview(tmp / "nope.yaml", study)
        ok("目标不存在被拒", False)
    except ExportError as e:
        ok("目标不存在被拒", "不存在" in str(e), str(e))
    bad = tmp / "x.toml"
    bad.write_text("a=1", encoding="utf-8")
    try:
        preview(bad, study)
        ok("不支持的后缀被拒", False)
    except ExportError as e:
        ok("不支持的后缀被拒", "格式不支持" in str(e), str(e))


if __name__ == "__main__":
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_find_iter_param(tmp)
        test_graduate(tmp)
        test_graduate_failure(tmp)
        test_export(tmp)
    print(f"\n全部通过：{PASS} 项断言")
