"""space patch 引擎 + settings 校验的回归测试（可直接 `python tests/test_space_patch.py` 运行）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tansuo.config import ConfigError, load_settings          # noqa: E402
from tansuo.space import SearchSpace, SpaceError               # noqa: E402

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def expect_error(name: str, fn, *substr: str) -> None:
    """断言 fn 抛出 ValueError 类异常且消息包含所有 substr。"""
    global PASS
    try:
        fn()
    except (ConfigError, SpaceError, ValueError) as e:
        msg = str(e)
        missing = [s for s in substr if s not in msg]
        assert not missing, f"FAIL: {name} 错误消息缺少 {missing}，实际：{msg}"
        PASS += 1
        print(f"  [ok] {name}（按预期拒绝：{msg[:60]}...）" if len(msg) > 60 else f"  [ok] {name}（按预期拒绝：{msg}）")
        return
    raise AssertionError(f"FAIL: {name} 本应报错但没有")


def write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def test_settings(tmp: Path) -> None:
    print("== settings 校验 ==")
    s = load_settings(ROOT / "demo/configs/settings.yaml")
    ok("加载真实 settings", s.metrics.primary.name == "val_acc"
       and s.metrics.primary.direction == "maximize"
       and [w.name for w in s.metrics.watch] == ["val_loss", "train_loss", "epoch_time_s"])

    expect_error("缺 primary",
                 lambda: load_settings(write(tmp, "s1.yaml", "metrics:\n  watch: []\n")),
                 "metrics.primary")
    expect_error("direction 写错",
                 lambda: load_settings(write(tmp, "s2.yaml",
                                             "metrics:\n  primary: {name: acc, direction: bigger}\n")),
                 "direction")
    expect_error("指标重名",
                 lambda: load_settings(write(tmp, "s3.yaml",
                                             "metrics:\n  primary: {name: acc, direction: maximize}\n"
                                             "  watch:\n    - {name: acc, direction: minimize}\n")),
                 "重复")


def make_space(tmp: Path) -> SearchSpace:
    return SearchSpace.from_yaml(ROOT / "demo" / "configs" / "search_space.yaml")


def test_load_and_suggest(tmp: Path) -> None:
    print("== 空间加载与 suggest ==")
    sp = make_space(tmp)
    ok("10 个参数（含条件参数 momentum）", len(sp.params) == 10)
    ok("envelope 记录", sp._by_name["lr"].env_low == 1e-4 and sp._by_name["lr"].env_high == 0.3)

    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize")

    def objective(trial):
        cfg = sp.suggest(trial)
        names = {p.name for p in sp.params}
        # 条件参数：momentum 仅在 optimizer=sgd 时出现
        expected = names if cfg["optimizer"] == "sgd" else names - {"momentum"}
        assert set(cfg) == expected, f"cfg 键集异常：{sorted(cfg)}"
        assert cfg["optimizer"] in ("adam", "adamw", "sgd")
        assert 2 <= cfg["epochs"] <= 5
        return 0.0
    study.optimize(objective, n_trials=5)
    ok("suggest 5 次取样合法（含条件参数）", True)


def test_patch(tmp: Path) -> None:
    print("== patch 引擎 ==")
    sp = make_space(tmp)

    r = sp.apply_patch([], "空 op")
    ok("空 ops 被拒", not r.ok)
    r = sp.apply_patch([{"op": "narrow", "param": "lr", "low": 1e-3, "high": 1e-2}], "")
    ok("缺 rationale 被拒", not r.ok and "rationale" in str(r.errors))

    r = sp.apply_patch([{"op": "narrow", "param": "lr", "low": 1e-3, "high": 1e-2}],
                       "高 lr 区间多次发散，收窄")
    ok("合法 narrow 通过", r.ok and r.new_version == 2 and sp._by_name["lr"].low == 1e-3)

    r = sp.apply_patch([{"op": "narrow", "param": "lr", "low": 1e-4, "high": 3e-1}],
                       "试图超出当前范围")
    ok("narrow 超出当前范围被拒", not r.ok)

    r = sp.apply_patch([{"op": "widen", "param": "lr", "low": 1e-5, "high": 0.3}],
                       "试图超出 envelope")
    ok("widen 超出 envelope 被拒", not r.ok and "envelope" in str(r.errors))

    r = sp.apply_patch([{"op": "widen", "param": "lr", "low": 1e-4, "high": 3e-1}],
                       "还原到 envelope")
    ok("widen 还原 envelope 通过", r.ok and sp._by_name["lr"].high == 0.3)

    r = sp.apply_patch([{"op": "widen", "param": "lr", "low": 0.1, "high": 0.3}],
                       "试图平移区间")
    ok("widen 不包含当前范围被拒（只放宽不平移）",
       not r.ok and "包含当前范围" in str(r.errors))

    r = sp.apply_patch([{"op": "set_choices", "param": "scheduler",
                         "choices": ["none", "cosine"]}], "裁剪取值集")
    ok("set_choices 被拒并给 freeze 指引", not r.ok and "freeze" in str(r.errors))

    r = sp.apply_patch([{"op": "narrow", "param": "optimizer", "low": 0, "high": 1}],
                       "对 choice 用 narrow")
    ok("choice 上 narrow 被拒并给 hint", not r.ok and "freeze" in str(r.errors))

    r = sp.apply_patch([{"op": "freeze", "param": "optimizer", "value": "adamw"}],
                       "AdamW 全面占优")
    ok("freeze 通过", r.ok and sp._by_name["optimizer"].is_frozen)

    r = sp.apply_patch([{"op": "freeze", "param": "augment", "value": "flip"}], "非法取值")
    ok("freeze 非法取值被拒", not r.ok)

    # 冻死护栏：10 个参数（含条件 momentum），已冻 1，再批量冻 3 剩 6 自由；
    # 试图一次再冻 4 个 → 只剩 2（< MIN_FREE_PARAMS=3）→ 应被拒
    freeze_more = [{"op": "freeze", "param": n, "value": v} for n, v in
                   [("scheduler", "none"), ("batch_size", 64), ("augment", "none")]]
    r = sp.apply_patch(freeze_more, "批量冻结三个")
    ok("批量冻结通过（还剩 6 自由）", r.ok and sp.free_param_count() == 6)
    freeze_rest = [{"op": "freeze", "param": n, "value": v} for n, v in
                   [("width", 16), ("dropout", 0.2), ("lr", 1e-3), ("weight_decay", 1e-4)]]
    r = sp.apply_patch(freeze_rest, "再冻四个")
    ok("冻死被拒（自由参数 < 3）", not r.ok and "冻死" in str(r.errors))
    ok("拒绝是原子的（空间未被部分修改）", sp.free_param_count() == 6)

    r = sp.apply_patch([{"op": "release", "param": "optimizer"}], "释放观察")
    ok("release 通过", r.ok and not sp._by_name["optimizer"].is_frozen
       and sp._by_name["optimizer"].choices == ["adam", "adamw", "sgd"])

    r = sp.apply_patch([{"op": "narrow", "param": "x1", "low": 0, "high": 1}] * 1, "未知参数")
    ok("未知参数被拒", not r.ok)
    too_many = [{"op": "narrow", "param": "lr", "low": 1e-4, "high": 0.3}] * 5
    r = sp.apply_patch(too_many, "超过 op 配额")
    ok("超过 4 条 op 被拒", not r.ok and "4" in str(r.errors))


def test_validate_config(tmp: Path) -> None:
    print("== validate_config ==")
    sp = make_space(tmp)
    good = {"optimizer": "adam", "lr": 1e-3, "scheduler": "none", "batch_size": 64,
            "weight_decay": 1e-4, "dropout": 0.2, "augment": "none", "width": 16, "epochs": 3}
    ok("合法配置通过", sp.validate_config(good) == [])
    bad = dict(good, lr=5.0, dropout=0.9, width=64)
    errs = sp.validate_config(bad)
    ok("越界配置被拒", len(errs) == 3, str(errs))
    missing = {k: v for k, v in good.items() if k != "epochs"}
    ok("缺参数被拒", any("epochs" in e for e in sp.validate_config(missing)))
    sp.apply_patch([{"op": "freeze", "param": "optimizer", "value": "adamw"}], "测试冻结")
    errs = sp.validate_config(dict(good, optimizer="sgd"))
    ok("冻结冲突被拒", any("冻结" in e for e in errs))
    merged = sp.inject({k: v for k, v in good.items() if k != "optimizer"})
    ok("inject 补冻结值", merged["optimizer"] == "adamw")


def test_snapshot(tmp: Path) -> None:
    print("== 快照与恢复 ==")
    sp = make_space(tmp)
    sp.apply_patch([{"op": "narrow", "param": "lr", "low": 1e-3, "high": 1e-2}], "收窄")
    sp.apply_patch([{"op": "freeze", "param": "optimizer", "value": "adamw"}], "冻结")
    out = tmp / "snaps"
    sp.snapshot(out)
    latest = SearchSpace.latest_snapshot(out)
    sp2 = SearchSpace.from_yaml(latest)
    ok("快照恢复版本", sp2.version == 3)
    ok("快照恢复边界", sp2._by_name["lr"].low == 1e-3)
    ok("快照恢复冻结", sp2._by_name["optimizer"].frozen == "adamw")
    ok("快照保留 envelope", sp2._by_name["lr"].env_high == 0.3)


def test_dynamic_tpe(tmp: Path) -> None:
    """关键验证：DynamicTPESampler 在动态空间（收窄/裁 choices/冻结）下能否正常工作。"""
    print("== TPE 动态空间 ==")
    import optuna
    from tansuo.study import DynamicTPESampler
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sp = make_space(tmp)
    study = optuna.create_study(direction="maximize",
                                sampler=DynamicTPESampler(n_startup_trials=3, seed=42))
    holder = {"flip": False}

    def objective(trial):
        if holder["flip"]:
            # 模拟 agent 编辑后的空间：收窄 lr、冻结 optimizer（分类参数分布保持不变）
            cfg = {}
            cfg["optimizer"] = "adamw"
            cfg["lr"] = trial.suggest_float("lr", 1e-3, 1e-2, log=True)
            cfg["scheduler"] = trial.suggest_categorical("scheduler", ["none", "cosine", "step"])
            cfg["batch_size"] = trial.suggest_categorical("batch_size", [32, 64, 128, 256])
            cfg["weight_decay"] = trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True)
            cfg["dropout"] = trial.suggest_float("dropout", 0.0, 0.5)
            cfg["augment"] = trial.suggest_categorical("augment", ["none", "affine"])
            cfg["width"] = trial.suggest_categorical("width", [8, 16, 32])
            cfg["epochs"] = trial.suggest_int("epochs", 2, 5)
        else:
            cfg = sp.suggest(trial)
        return float(cfg["width"]) - 100 * abs(cfg["lr"] - 0.005)
    study.optimize(objective, n_trials=4)
    holder["flip"] = True
    study.optimize(objective, n_trials=6)
    ok("动态空间下 TPE 跑通 10 次", len(study.trials) == 10)

    # 边界：数值参数边界大幅漂移（历史观测越界）也不崩——模拟 widen/收窄交替
    study2 = optuna.create_study(direction="maximize",
                                 sampler=DynamicTPESampler(n_startup_trials=2, seed=1))
    study2.optimize(lambda t: -abs(t.suggest_float("x", 0.0, 1.0) - 0.9), n_trials=3)
    study2.optimize(lambda t: -abs(t.suggest_float("x", 0.0, 0.2) - 0.1), n_trials=3)
    ok("数值边界漂移不崩", len(study2.trials) == 6)


def test_extend_envelope() -> None:
    print("== extend_envelope（人工权限：widen 可扩展 envelope） ==")
    sp = SearchSpace.from_dict({"params": [
        {"name": "lr", "type": "float", "low": 0.01, "high": 0.1, "description": "学习率"},
        {"name": "epochs", "type": "int", "low": 5, "high": 50, "description": "训练轮数"},
        {"name": "batch", "type": "int", "low": 16, "high": 256, "description": "批大小"},
    ]})
    # agent 路径（不 extend）：widen 超 envelope 依旧拒绝
    r0 = sp.apply_patch([{"op": "widen", "param": "epochs", "low": 5, "high": 80}], "放宽")
    ok("未经 extend_envelope 的 widen 超 envelope 仍被拒（agent 约束不变）", not r0.ok)
    # 人工权限：先扩展 envelope，同一 op 通过
    sp.extend_envelope([{"op": "widen", "param": "epochs", "low": 5, "high": 80}])
    r1 = sp.apply_patch([{"op": "widen", "param": "epochs", "low": 5, "high": 80}],
                        "人工放宽 epochs 上界")
    ok("extend_envelope 后 widen 超原 envelope 通过", r1.ok and r1.new_version == 2,
       str(r1.errors))
    p = sp._by_name["epochs"]
    ok("envelope 被扩展（env_high=80）且当前上界同步更新",
       p.env_high == 80 and p.high == 80)
    # 非法/无关 op 一律跳过：不崩、不误扩
    sp.extend_envelope([{"op": "narrow", "param": "lr", "low": 0.02, "high": 0.08},
                        {"op": "widen", "param": "nope", "low": 1, "high": 2},
                        {"op": "widen", "param": "batch", "low": 999, "high": 1}])
    ok("非 widen/未知参数/非法边界均被跳过不报错",
       sp._by_name["lr"].env_high == 0.1 and sp._by_name["batch"].env_high == 256)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_settings(tmp)
        test_load_and_suggest(tmp)
        test_patch(tmp)
        test_validate_config(tmp)
        test_snapshot(tmp)
        test_dynamic_tpe(tmp)
    test_extend_envelope()
    print(f"\n全部通过：{PASS} 项断言")
