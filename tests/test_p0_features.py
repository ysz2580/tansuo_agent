"""P0 功能测试：早停护栏 / 试验级日志落盘 / 人工试验插队（inbox）/ 预算写回
（可直接 `python tests/test_p0_features.py` 运行）。

覆盖：
- 失败熔断：连续 N 次失败 → finished_reason=fail_streak 提前收尾；0=关闭跑满预算；
  completed/pruned 清零连败
- 收敛自动停：连续 M 次完结试验无提升 → finished_reason=plateau；方向感知
  （minimize 下变大才算无提升）；续跑时以历史最优为基准（老成绩不算新提升）
- 试验日志：subprocess 模式完整 stdout/stderr 落盘 <data_dir>/trials/trial-NNNN.log，
  含头行（cmd/params）与尾段（stderr/exit_code）；失败错误信息附完整日志路径
- inbox 消费：正常消费（journal source=human 审计）、损坏条目跳过、预算耗尽放回、
  执行异常放回且不静默丢失
- write_back_budget：已有键原位覆盖保注释、行内 budget: {...} 插入、块形态插入、
  无 budget 块报错；写入前经 load_settings 完整校验
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import optuna                                                # noqa: E402

from tansuo.config import ConfigError, load_settings, write_back_budget  # noqa: E402
from tansuo.journal import FINISH, SESSION_START, TRIAL_END, Journal     # noqa: E402
from tansuo.orchestrator import Orchestrator                             # noqa: E402
from tansuo.runner import TrialFailedError, TrialRunner, trial_log_path  # noqa: E402
from tansuo.space import SearchSpace                                     # noqa: E402

PASS = 0

CHILD_OK = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.5}\')\n'
CHILD_ALWAYS_FAIL = "import sys\nsys.exit(2)\n"
CHILD_LOG = (
    "import json, sys\n"
    "print(\"hello stdout\")\n"
    "sys.stderr.write(\"warn line\\n\")\n"
    'print(\'##TANSUO## {"type": "final", "value": 0.8}\')\n'
)

SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率（测试用最小空间）"},
]}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def make_settings(tmp: Path, child_code: str, name: str, *, total: int = 8,
                  direction: str = "maximize", max_fail_streak: int = 5,
                  auto_stop_plateau: int | None = None):
    """每个用例独立的 data_dir（journal/db 互不串扰）。"""
    script = tmp / f"{name}.py"
    script.write_text(child_code, encoding="utf-8")
    data_dir = tmp / "data" / name
    exe = Path(sys.executable).as_posix()
    plateau_txt = (f", auto_stop_plateau: {auto_stop_plateau}"
                   if auto_stop_plateau else "")
    text = (
        "experiment: {name: p0_test, data_dir: " + data_dir.as_posix() + "}\n"
        "metrics:\n"
        f"  primary: {{name: val_acc, direction: {direction}}}\n"
        "adapter:\n"
        f'  mode: subprocess\n  command: ["{exe}", "{script.as_posix()}"]\n'
        "  config_via: env\n  timeout_s: 60\n  retry_on_fail: 0\n"
        f"budget: {{total_trials: {total}, wake_every: 1, seed: 1, workers: 1, "
        f"max_fail_streak: {max_fail_streak}{plateau_txt}}}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        "storage: {url: sqlite:///" + (data_dir / "t.db").as_posix() + "}\n"
    )
    p = tmp / f"{name}_settings.yaml"
    p.write_text(text, encoding="utf-8")
    return load_settings(p)


def make_orch(settings, log_lines: list | None = None,
              study: optuna.Study | None = None) -> Orchestrator:
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(settings.data_dir) / "journal.jsonl")
    study = study or optuna.create_study(direction=settings.metrics.primary.direction)
    runner = TrialRunner(settings, space, journal)
    return Orchestrator(settings, space, study, runner, journal,
                        log=log_lines.append if log_lines is not None else (lambda *_: None))


def fresh_trial():
    return optuna.create_study(direction="maximize").ask()


# ----------------------------------------------------------------------
def test_fail_streak_guard(tmp: Path) -> None:
    print("== 失败熔断（fail_streak） ==")
    s = make_settings(tmp, CHILD_ALWAYS_FAIL, "fuse", total=8, max_fail_streak=3)
    lines: list[str] = []
    orch = make_orch(s, lines)
    orch.run(total_trials=8, wake_every=1)
    ok("连续 3 次失败触发熔断（finished_reason=fail_streak）",
       orch.finished_reason == "fail_streak", str(orch.finished_reason))
    ok("提前收尾未跑满预算（3/8）", orch.finished_count() == 3,
       f"done={orch.finished_count()}")
    events = orch.journal.load_events()
    finish = [e for e in events if e.get("kind") == FINISH][-1]
    ok("FINISH 事件 reason=fail_streak", finish.get("reason") == "fail_streak")
    start = [e for e in events if e.get("kind") == SESSION_START][-1]
    ok("SESSION_START 记录护栏配置",
       start.get("max_fail_streak") == 3 and start.get("auto_stop_plateau") is None,
       str({k: start.get(k) for k in ("max_fail_streak", "auto_stop_plateau")}))
    ok("护栏日志给出关闭提示", any("[护栏]" in ln and "max_fail_streak=0" in ln
                                    for ln in lines), str(lines[-3:]))

    # 关闭熔断（0）→ 同样的脚本跑满预算，reason=budget_exhausted
    s2 = make_settings(tmp, CHILD_ALWAYS_FAIL, "fuse_off", total=5, max_fail_streak=0)
    orch2 = make_orch(s2)
    orch2.run(total_trials=5, wake_every=1)
    finish2 = [e for e in orch2.journal.load_events() if e.get("kind") == FINISH][-1]
    ok("max_fail_streak=0 关闭熔断（跑满预算）",
       finish2.get("reason") == "budget_exhausted" and orch2.finished_count() == 5,
       f"reason={finish2.get('reason')} done={orch2.finished_count()}")

    # 记账语义：failed +1；completed/pruned 清零（单元级）
    orch2._fail_streak = 0
    orch2._outcome("failed")
    orch2._outcome("failed")
    ok("连败累计", orch2._fail_streak == 2)
    orch2._outcome("completed", 0.5)
    ok("completed 清零连败", orch2._fail_streak == 0)
    orch2._outcome("failed")
    orch2._outcome("pruned")
    ok("pruned 也清零连败（剪枝说明流水线通）", orch2._fail_streak == 0)


def test_plateau_guard(tmp: Path) -> None:
    print("== 收敛自动停（plateau） ==")
    s = make_settings(tmp, CHILD_OK, "plateau", total=10,
                      max_fail_streak=0, auto_stop_plateau=3)
    lines: list[str] = []
    orch = make_orch(s, lines)
    orch.run(total_trials=10, wake_every=1)
    ok("连续 3 次无提升触发收敛自动停（finished_reason=plateau）",
       orch.finished_reason == "plateau", str(orch.finished_reason))
    ok("第 1 次刷新基准后第 2/3/4 次无提升 → 4 次即停",
       orch.finished_count() == 4, f"done={orch.finished_count()}")
    finish = [e for e in orch.journal.load_events() if e.get("kind") == FINISH][-1]
    ok("FINISH 事件 reason=plateau", finish.get("reason") == "plateau")
    ok("护栏日志说明节省预算", any("[护栏]" in ln and "无提升" in ln for ln in lines),
       str(lines[-3:]))

    # 续跑基准：study 已有历史最优 0.9 → 新试验 0.5 始终无提升，2 次即停
    from optuna.distributions import FloatDistribution
    from optuna.trial import create_trial
    s2 = make_settings(tmp, CHILD_OK, "plateau_resume", total=6,
                       max_fail_streak=0, auto_stop_plateau=2)
    study2 = optuna.create_study(direction="maximize")
    study2.add_trial(create_trial(state=optuna.trial.TrialState.COMPLETE, value=0.9,
                                  params={"lr": 0.05},
                                  distributions={"lr": FloatDistribution(0.01, 0.1)}))
    orch2 = make_orch(s2, study=study2)
    orch2.run(total_trials=6, wake_every=1)
    ok("续跑以历史最优为基准（老成绩不算新提升）",
       orch2.finished_reason == "plateau" and orch2.finished_count() == 3,
       f"reason={orch2.finished_reason} done={orch2.finished_count()}")

    # 方向感知（minimize）：值变大=无提升；值变小=刷新基准
    s3 = make_settings(tmp, CHILD_OK, "plateau_min", direction="minimize",
                       max_fail_streak=0, auto_stop_plateau=2)
    orch3 = make_orch(s3)
    orch3._best_so_far = None
    orch3._outcome("completed", 0.5)     # 基准 None → 刷新
    ok("minimize：首个完结试验刷新基准",
       orch3._best_so_far == 0.5 and orch3._plateau_streak == 0)
    orch3._outcome("completed", 0.6)     # 变大 → 无提升
    ok("minimize：值变大计为无提升", orch3._plateau_streak == 1)
    orch3._outcome("completed", 0.4)     # 变小 → 改进，清零
    ok("minimize：值变小刷新基准并清零",
       orch3._best_so_far == 0.4 and orch3._plateau_streak == 0)

    # 护栏配置校验
    head = ("metrics:\n  primary: {name: val_acc, direction: maximize}\n"
            "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n")
    for name, body, key in [
        ("max_fail_streak 负数被拒", head + "budget: {max_fail_streak: -1}\n",
         "max_fail_streak"),
        ("auto_stop_plateau=1 被拒（无意义）",
         head + "budget: {auto_stop_plateau: 1}\n", "auto_stop_plateau"),
    ]:
        p = tmp / f"{name}.yaml"
        p.write_text(body, encoding="utf-8")
        try:
            load_settings(p)
            raise AssertionError(f"FAIL: {name} 本应报错但没有")
        except ConfigError as e:
            ok(f"{name}（按预期拒绝）", key in str(e), str(e))


def test_trial_log_file(tmp: Path) -> None:
    print("== 试验级日志落盘 ==")
    s = make_settings(tmp, CHILD_LOG, "triallog", total=3)
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(s.data_dir) / "journal.jsonl")
    runner = TrialRunner(s, space, journal)
    value = runner.run_trial(fresh_trial())
    ok("试验正常完成", abs(value - 0.8) < 1e-9, f"value={value}")
    lp = trial_log_path(s, 0)
    ok("日志落在 <data_dir>/trials/trial-0000.log", lp.exists()
       and lp.parent.name == "trials" and lp.name == "trial-0000.log", str(lp))
    text = lp.read_text(encoding="utf-8")
    ok("头行含 cmd 与参数快照", "=====" in text and "cmd=" in text
       and "params=" in text and '"lr"' in text, text[:200])
    ok("stdout 全量落盘", "hello stdout" in text)
    ok("尾段含 stderr 与退出码",
       "----- stderr -----" in text and "warn line" in text
       and "exit_code=0" in text and "timed_out=False" in text, text[-300:])

    # 失败试验：错误信息附完整日志路径，日志同样落盘
    s2 = make_settings(tmp, CHILD_ALWAYS_FAIL, "triallog_fail", total=1)
    runner2 = TrialRunner(s2, SearchSpace.from_dict(SPACE_DICT),
                          Journal(Path(s2.data_dir) / "journal.jsonl"))
    try:
        runner2.run_trial(fresh_trial())
        raise AssertionError("FAIL: 应抛 TrialFailedError")
    except TrialFailedError as e:
        lp2 = trial_log_path(s2, 0)
        ok("失败错误信息附完整日志路径",
           "完整日志" in e.detail and str(lp2.as_posix()) in e.detail.replace("\\", "/"),
           e.full())
        ok("失败试验日志也落盘且含非零退出码", lp2.exists()
           and "exit_code=2" in lp2.read_text(encoding="utf-8"))


def test_inbox_consume(tmp: Path) -> None:
    print("== 人工试验插队（inbox 消费） ==")
    s = make_settings(tmp, CHILD_OK, "inbox", total=5)
    orch = make_orch(s)

    def enqueue(entries: list[dict]) -> None:
        with open(orch.inbox_path(), "a", encoding="utf-8") as f:
            for en in entries:
                f.write(json.dumps(en, ensure_ascii=False) + "\n")

    # 正常消费：journal source=human 审计，试验属性带 note
    enqueue([{"params": {"lr": 0.05}, "note": "manual-test"}])
    res = orch.consume_inbox()
    ok("消费 1 条人工试验", res == {"consumed": 1, "requeued": 0}, str(res))
    ok("消费后队列文件删除（认领文件也不残留）",
       not orch.inbox_path().exists()
       and not orch.inbox_path().with_name("inbox.processing.jsonl").exists())
    ends = [e for e in orch.journal.load_events() if e.get("kind") == TRIAL_END]
    ok("journal TRIAL_END 带 source=human 审计",
       len(ends) == 1 and ends[0].get("source") == "human"
       and ends[0].get("note") == "manual-test", str(ends))
    trial = orch.study.get_trials(deepcopy=False)[-1]
    ok("试验属性标记 custom 与 note",
       trial.user_attrs.get("custom") is True
       and trial.user_attrs.get("note") == "manual-test")

    # 空队列幂等
    ok("无队列文件时消费幂等（0/0）",
       orch.consume_inbox() == {"consumed": 0, "requeued": 0})

    # 损坏条目跳过、缺 params 跳过、合法条目照常
    raw = orch.inbox_path()
    raw.parent.mkdir(parents=True, exist_ok=True)
    with open(raw, "w", encoding="utf-8") as f:
        f.write("not-json-at-all\n")
        f.write(json.dumps({"note": "没有 params"}) + "\n")
        f.write(json.dumps({"params": {"lr": 0.06}}) + "\n")
    res2 = orch.consume_inbox()
    ok("损坏/缺参条目跳过，合法条目消费", res2 == {"consumed": 1, "requeued": 0},
       str(res2))

    # 预算耗尽 → 条目原样放回队列，不静默丢失
    s3 = make_settings(tmp, CHILD_OK, "inbox_full", total=1)
    orch3 = make_orch(s3)
    orch3.run_batch(1)
    enqueue3 = {"params": {"lr": 0.07}, "note": "排队中"}
    with open(orch3.inbox_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps(enqueue3) + "\n")
    res3 = orch3.consume_inbox()
    ok("预算耗尽时条目放回队列", res3 == {"consumed": 0, "requeued": 1}, str(res3))
    left = [json.loads(ln) for ln in
            orch3.inbox_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    ok("放回的条目内容原样", len(left) == 1 and left[0]["params"] == {"lr": 0.07}
       and left[0]["note"] == "排队中", str(left))

    # 执行异常 → 该条及后续全部放回（stop 语义）
    s4 = make_settings(tmp, CHILD_OK, "inbox_err", total=5)
    orch4 = make_orch(s4)
    with open(orch4.inbox_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps({"params": {"lr": 0.03}}) + "\n")
        f.write(json.dumps({"params": {"lr": 0.04}}) + "\n")
    orig_run_custom = orch4.run_custom

    def boom(params, note=None, source="custom"):
        raise RuntimeError("数据库被运行中的搜索占用")

    orch4.run_custom = boom
    res4 = orch4.consume_inbox()
    ok("执行异常时全部条目放回（不静默丢失）",
       res4 == {"consumed": 0, "requeued": 2}, str(res4))
    orch4.run_custom = orig_run_custom
    res5 = orch4.consume_inbox()
    ok("异常恢复后下轮可继续消费", res5 == {"consumed": 2, "requeued": 0}, str(res5))


def test_write_back_budget(tmp: Path) -> None:
    print("== budget.max_gpu_hours 写回（三形态兼容） ==")
    head = ("experiment: {name: wb, data_dir: " + (tmp / "wb_data").as_posix() + "}\n"
            "metrics:\n  primary: {name: val_acc, direction: maximize}\n"
            "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n")

    # ① 已有键：原位覆盖，保行尾注释
    p1 = tmp / "wb1.yaml"
    p1.write_text(head + "budget:\n  total_trials: 10\n"
                         "  max_gpu_hours: 1.5  # 已有注释\n", encoding="utf-8")
    r1 = write_back_budget(p1, 2.5)
    txt1 = p1.read_text(encoding="utf-8")
    ok("形态①：原位覆盖且行尾注释保留",
       r1["ok"] and "max_gpu_hours: 2.5  # 已有注释" in txt1, txt1)
    ok("形态①：load_settings 读回新值",
       load_settings(p1).budget.max_gpu_hours == 2.5)

    # ② 行内形态 budget: {...}：收尾花括号前插入
    p2 = tmp / "wb2.yaml"
    p2.write_text(head + "budget: {total_trials: 10, wake_every: 5}\n",
                  encoding="utf-8")
    r2 = write_back_budget(p2, 3.0)
    txt2 = p2.read_text(encoding="utf-8")
    ok("形态②：行内 budget 插入成功",
       r2["ok"] and "max_gpu_hours:3}" in txt2.replace(" ", ""), txt2)
    s2 = load_settings(p2)
    ok("形态②：读回新值且原字段不丢",
       s2.budget.max_gpu_hours == 3.0 and s2.budget.total_trials == 10
       and s2.budget.wake_every == 5)

    # ③ 块形态（无键）：作为首个子键插入，沿用子键缩进
    p3 = tmp / "wb3.yaml"
    p3.write_text(head + "budget:\n  total_trials: 10\n", encoding="utf-8")
    r3 = write_back_budget(p3, 4.0)
    txt3 = p3.read_text(encoding="utf-8")
    ok("形态③：块形态插入为首个子键",
       r3["ok"] and "budget:\n  max_gpu_hours: 4\n  total_trials: 10" in txt3, txt3)
    ok("形态③：读回新值", load_settings(p3).budget.max_gpu_hours == 4.0)

    # 无 budget 块 → 明确报错
    p4 = tmp / "wb4.yaml"
    p4.write_text(head, encoding="utf-8")
    r4 = write_back_budget(p4, 1.0)
    ok("无 budget 块时报错不落盘",
       not r4["ok"] and any("budget" in e for e in r4["errors"])
       and "max_gpu_hours" not in p4.read_text(encoding="utf-8"), str(r4))

    # 负数写入会被 load_settings 校验拦下（写回前临时文件校验）
    r5 = write_back_budget(p3, -1.0)
    ok("非法值（负数）写回被校验拒绝", not r5["ok"] and r5["errors"], str(r5))
    ok("拒绝后原文件未被修改",
       load_settings(p3).budget.max_gpu_hours == 4.0)


if __name__ == "__main__":
    import tempfile
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_fail_streak_guard(tmp)
        test_plateau_guard(tmp)
        test_trial_log_file(tmp)
        test_inbox_consume(tmp)
        test_write_back_budget(tmp)
    print(f"\n全部通过：{PASS} 项断言")
