"""条件搜索空间（depends_on）回归测试（可直接 `python tests/test_conditional_space.py` 运行）。

覆盖：
- from_dict 校验：父参数不存在/定义在后面/非 choice/取值越界/空映射
- suggest：条件满足才取样（sgd 有 momentum，adam 无）
- validate_config：条件满足必填 / 不满足禁填 / 冻结父参数下的判定
- inject：条件冻结参数的注入
- to_dict/from_dict 与快照往返保留 depends_on
- patch 引擎对条件参数的 narrow/freeze/release
- 真 Optuna study：缺参数试验不影响 TPE 后续采样（动态空间机制）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import optuna                                        # noqa: E402

from tansuo.space import SearchSpace, SpaceError     # noqa: E402
from tansuo.study import DynamicTPESampler           # noqa: E402

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def expect_error(name: str, fn, *substr: str) -> None:
    global PASS
    try:
        fn()
    except (SpaceError, ValueError) as e:
        msg = str(e)
        missing = [s for s in substr if s not in msg]
        assert not missing, f"FAIL: {name} 错误消息缺少 {missing}，实际：{msg}"
        PASS += 1
        print(f"  [ok] {name}（按预期拒绝：{msg[:60]}...）" if len(msg) > 60
              else f"  [ok] {name}（按预期拒绝：{msg}）")
        return
    raise AssertionError(f"FAIL: {name} 本应报错但没有")


def write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


COND_SPACE = """
params:
  - {name: optimizer, type: choice, choices: [adam, sgd], description: 优化器}
  - {name: lr, type: float, low: 1.0e-3, high: 1.0e-1, log: true, description: 学习率}
  - name: momentum
    type: float
    low: 0.5
    high: 0.99
    description: SGD 动量
    depends_on: {optimizer: sgd}
  - {name: wd, type: float, low: 1.0e-5, high: 1.0e-2, log: true, description: 权重衰减}
  - {name: sched, type: choice, choices: [none, cosine], description: 调度器}
"""


def make_cond_space(tmp: Path) -> SearchSpace:
    return SearchSpace.from_yaml(write(tmp, "cond.yaml", COND_SPACE))


class FakeTrial:
    """脚本化的假 trial：suggest_* 按 scripted 返回指定值，并记录调用过的参数名。"""

    def __init__(self, scripted: dict):
        self.scripted = scripted
        self.calls: list[str] = []

    def suggest_categorical(self, name, choices):
        self.calls.append(name)
        return self.scripted[name]

    def suggest_float(self, name, low, high, log=False):
        self.calls.append(name)
        return (low + high) / 2

    def suggest_int(self, name, low, high, log=False):
        self.calls.append(name)
        return low

    def set_user_attr(self, k, v):
        pass


def test_validation(tmp: Path) -> None:
    print("== depends_on 校验 ==")
    ok("合法定义可加载", make_cond_space(tmp).params[2].depends_on == {"optimizer": ["sgd"]})

    expect_error("父参数不存在",
                 lambda: SearchSpace.from_dict({"params": [
                     {"name": "optimizer", "type": "choice", "choices": ["adam"], "description": "x"},
                     {"name": "m", "type": "float", "low": 0.1, "high": 0.9,
                      "description": "x", "depends_on": {"nope": "adam"}},
                 ]}),
                 "父参数", "不存在")

    expect_error("父参数定义在后面",
                 lambda: SearchSpace.from_dict({"params": [
                     {"name": "m", "type": "float", "low": 0.1, "high": 0.9,
                      "description": "x", "depends_on": {"optimizer": "sgd"}},
                     {"name": "optimizer", "type": "choice", "choices": ["sgd"], "description": "x"},
                 ]}),
                 "排在前面")

    expect_error("父参数非 choice",
                 lambda: SearchSpace.from_dict({"params": [
                     {"name": "lr", "type": "float", "low": 0.1, "high": 0.9, "description": "x"},
                     {"name": "m", "type": "float", "low": 0.1, "high": 0.9,
                      "description": "x", "depends_on": {"lr": 0.5}},
                 ]}),
                 "choice")

    expect_error("依赖取值不在候选集",
                 lambda: SearchSpace.from_dict({"params": [
                     {"name": "optimizer", "type": "choice", "choices": ["adam"], "description": "x"},
                     {"name": "m", "type": "float", "low": 0.1, "high": 0.9,
                      "description": "x", "depends_on": {"optimizer": "sgd"}},
                 ]}),
                 "候选集")

    expect_error("depends_on 空映射",
                 lambda: SearchSpace.from_dict({"params": [
                     {"name": "optimizer", "type": "choice", "choices": ["adam"], "description": "x"},
                     {"name": "m", "type": "float", "low": 0.1, "high": 0.9,
                      "description": "x", "depends_on": {}},
                 ]}),
                 "非空映射")


def test_suggest(tmp: Path) -> None:
    print("== suggest 条件行为 ==")
    sp = make_cond_space(tmp)

    t_sgd = FakeTrial({"optimizer": "sgd", "sched": "none"})
    cfg = sp.suggest(t_sgd)
    ok("sgd 试验取样 momentum", "momentum" in cfg and cfg["optimizer"] == "sgd",
       detail=str(cfg))
    ok("sgd 试验的常规参数齐全", {"optimizer", "lr", "momentum"} <= set(cfg))

    t_adam = FakeTrial({"optimizer": "adam", "sched": "none"})
    cfg = sp.suggest(t_adam)
    ok("adam 试验不取样 momentum", "momentum" not in cfg and cfg["optimizer"] == "adam")
    ok("adam 试验未调用 momentum 的 suggest", "momentum" not in t_adam.calls)


def test_validate_config(tmp: Path) -> None:
    print("== validate_config 条件语义 ==")
    sp = make_cond_space(tmp)
    base = {"optimizer": "sgd", "lr": 0.01, "wd": 1e-4, "sched": "none"}

    ok("条件满足且提供 momentum → 通过", sp.validate_config({**base, "momentum": 0.9}) == [])
    errs = sp.validate_config(dict(base))
    ok("条件满足但缺 momentum → 报缺失", any("缺少参数 momentum" in e for e in errs), str(errs))

    base_adam = {"optimizer": "adam", "lr": 0.01, "wd": 1e-4, "sched": "none"}
    ok("条件不满足且未提供 → 通过", sp.validate_config(dict(base_adam)) == [])
    errs = sp.validate_config({**base_adam, "momentum": 0.9})
    ok("条件不满足却提供 momentum → 拒绝",
       any("仅当" in e and "不应提供" in e for e in errs), str(errs))


def test_freeze_and_inject(tmp: Path) -> None:
    print("== 冻结父参数与 inject ==")
    sp = make_cond_space(tmp)

    r = sp.apply_patch([{"op": "freeze", "param": "optimizer", "value": "adam"}],
                       rationale="测试：冻结为 adam，momentum 应失活")
    ok("冻结父参数成功", r.ok, str(r.errors))
    cfg = sp.suggest(FakeTrial({"sched": "none"}))
    ok("父参数冻结为 adam 后不再取样 momentum", "momentum" not in cfg
       and cfg.get("optimizer") == "adam", str(cfg))
    ok("inject 也不注入失活的冻结子参数", "momentum" not in sp.inject({"lr": 0.01}))

    sp2 = make_cond_space(tmp)
    r = sp2.apply_patch([{"op": "freeze", "param": "optimizer", "value": "sgd"},
                         {"op": "freeze", "param": "momentum", "value": 0.9}],
                        rationale="测试：冻结 sgd + momentum")
    ok("冻结 sgd 与 momentum 成功", r.ok, str(r.errors))
    merged = sp2.inject({"lr": 0.01})
    ok("条件满足时 inject 补齐冻结的 momentum",
       merged.get("optimizer") == "sgd" and merged.get("momentum") == 0.9, str(merged))
    errs = sp2.validate_config({"lr": 0.01})
    ok("冻结父参数=sgd 时，缺 momentum 不再报缺失（会被 inject 补齐）",
       not any("momentum" in e for e in errs), str(errs))


def test_roundtrip_and_patch(tmp: Path) -> None:
    print("== 快照往返与 patch ==")
    sp = make_cond_space(tmp)
    sp2 = SearchSpace.from_dict(sp.to_dict())
    ok("to_dict/from_dict 保留 depends_on",
       sp2.params[2].depends_on == {"optimizer": ["sgd"]})

    snap_dir = tmp / "snaps"
    path = sp.snapshot(snap_dir)
    sp3 = SearchSpace.from_yaml(path)
    ok("快照落盘再加载保留 depends_on",
       sp3.params[2].depends_on == {"optimizer": ["sgd"]}, str(path))

    r = sp.apply_patch([{"op": "narrow", "param": "momentum", "low": 0.8, "high": 0.95}],
                       rationale="测试收窄条件参数")
    ok("narrow 条件参数成功", r.ok, str(r.errors))
    r = sp.apply_patch([{"op": "freeze", "param": "momentum", "value": 0.9},
                        {"op": "release", "param": "momentum"}],
                       rationale="测试冻结再释放条件参数")
    ok("freeze/release 条件参数成功", r.ok, str(r.errors))
    ok("brief 展示依赖说明", "仅当 optimizer ∈ {sgd}" in sp.params[2].brief())


def test_tpe_with_missing_params(tmp: Path) -> None:
    print("== TPE 对缺参数试验的兼容（真 study） ==")
    sp = make_cond_space(tmp)
    study = optuna.create_study(direction="maximize",
                                sampler=DynamicTPESampler(n_startup_trials=4, seed=42))
    for _ in range(20):
        trial = study.ask()
        cfg = sp.suggest(trial)
        study.tell(trial, float(cfg.get("momentum", 0.7)) + cfg["lr"])
    trials = study.get_trials(deepcopy=False)
    ok("20 次试验全部完结", len(trials) == 20)
    sgd = [t for t in trials if t.params.get("optimizer") == "sgd"]
    other = [t for t in trials if t.params.get("optimizer") != "sgd"]
    ok("至少出现一次 sgd 与非 sgd（seed=42 确定）", len(sgd) >= 1 and len(other) >= 1,
       f"sgd={len(sgd)} other={len(other)}")
    ok("sgd 试验都带 momentum", all("momentum" in t.params for t in sgd))
    ok("非 sgd 试验都不带 momentum", all("momentum" not in t.params for t in other))


def test_demo_space(tmp: Path) -> None:
    print("== 演示空间回归 ==")
    sp = SearchSpace.from_yaml(ROOT / "demo" / "configs" / "search_space.yaml")
    by_name = {p.name: p for p in sp.params}
    ok("demo 含条件参数 momentum", by_name.get("momentum") is not None
       and by_name["momentum"].depends_on == {"optimizer": ["sgd"]})


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_validation(tmp)
        test_suggest(tmp)
        test_validate_config(tmp)
        test_freeze_and_inject(tmp)
        test_roundtrip_and_patch(tmp)
        test_tpe_with_missing_params(tmp)
        test_demo_space(tmp)
    print(f"\n全部通过：{PASS} 项断言")
