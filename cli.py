"""tansuo_agent 命令行入口。

子命令：
  run    跑超参数搜索（--no-agent 纯 Optuna 巡航；默认预算见 settings.yaml）
  space  查看当前搜索空间与补丁历史（space show）
  check  探测 LLM 端点连通性（Phase 5 提供）
  init   生成离线配置模板兜底（Phase 6 提供）
  setup  配置 agent：自动起草 settings/搜索空间（Phase 6 提供）
  report 生成分析报告（Phase 4 提供）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import optuna

from tansuo.config import ConfigError, load_settings
from tansuo.journal import Journal
from tansuo.orchestrator import Orchestrator
from tansuo.runner import TrialRunner
from tansuo.space import SearchSpace, SpaceError
from tansuo.study import create_or_load_study

optuna.logging.set_verbosity(optuna.logging.WARNING)

DEFAULT_SETTINGS = "demo/configs/settings.yaml"
DEFAULT_SPACE = "demo/configs/search_space.yaml"


def journal_path(settings) -> Path:
    return Path(settings.data_dir) / "journal.jsonl"


def load_space_with_snapshots(space_yaml: Path, data_dir: Path) -> SearchSpace:
    """断点续跑时优先恢复最新空间快照（agent 的编辑状态不丢），否则用初始空间。"""
    snaps = []
    if data_dir.exists():
        for p in data_dir.glob("space_v*.yaml"):
            m = re.fullmatch(r"space_v(\d+)\.yaml", p.name)
            if m:
                snaps.append((int(m.group(1)), p))
    if snaps:
        snaps.sort()
        path = snaps[-1][1]
        print(f"恢复空间快照：{path}")
        return SearchSpace.from_yaml(path)
    return SearchSpace.from_yaml(space_yaml)


def _make_runtime(args):
    settings = load_settings(args.settings)
    if args.seed is not None:
        settings.budget.seed = args.seed
    if getattr(args, "model", None):
        settings.agent.model = args.model
    data_dir = Path(settings.data_dir)
    space = load_space_with_snapshots(Path(args.space), data_dir)
    study = create_or_load_study(settings)
    journal = Journal(journal_path(settings))
    runner = TrialRunner(settings, space, journal)
    orch = Orchestrator(settings, space, study, runner, journal)
    return settings, orch


def cmd_run(args) -> int:
    try:
        settings, orch = _make_runtime(args)
    except (ConfigError, SpaceError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    total = args.trials or settings.budget.total_trials
    wake = args.wake_every or settings.budget.wake_every
    if args.workers is not None and args.workers < 1:
        print("参数错误：--workers 必须 ≥ 1", file=sys.stderr)
        return 2
    if args.hours is not None and args.hours <= 0:
        print("参数错误：--hours 必须是正数（小时）", file=sys.stderr)
        return 2

    agent_on = settings.agent.enabled and not args.no_agent
    supervisor = None
    if agent_on:
        from tansuo.agent.client import make_client, probe_endpoint
        from tansuo.agent.loop import AgentSupervisor
        print(f"[agent] 探测端点（model={settings.agent.model}）……")
        probe = probe_endpoint(make_client(settings.agent), settings.agent.model)
        if probe["ok"]:
            supervisor = AgentSupervisor(settings, orch)
            print(f"[agent] 端点可用（{probe['detail']}），监督者已就绪")
        else:
            print(f"[agent] 探测失败于 [{probe['stage']}]：{probe['detail']}\n"
                  f"[agent] 自动降级为纯 Optuna 巡航（也可手动 --no-agent）。", file=sys.stderr)
    else:
        print("[agent] 已禁用（--no-agent / settings agent.enabled=false），纯 Optuna 巡航。")

    workers = args.workers or settings.budget.workers
    print(f"实验：{settings.experiment_name} | 主指标：{settings.metrics.primary.name}"
          f"（{settings.metrics.primary.better}）| 预算：{total} 次试验 | 每 {wake} 次唤醒"
          + (f" | 并行 {workers} worker" if workers > 1 else "")
          + (f" | 时间上限 {args.hours:g}h" if args.hours else
             (f" | 时间上限 {settings.budget.max_duration_h:g}h"
              if settings.budget.max_duration_h else "")))
    orch.run(total_trials=total, wake_every=wake, supervisor=supervisor,
             workers=args.workers, max_duration_h=args.hours)
    try:
        from tansuo.report import generate_report
        report_path, best_path = generate_report(settings, orch.study, orch.space, orch.journal)
        print(f"报告：{report_path} ｜ 最优配置：{best_path}")
    except Exception as e:   # noqa: BLE001 —— 报告失败不影响搜索本身的成果
        print(f"报告生成失败（不影响已完成的搜索）：{e}", file=sys.stderr)
    return 0


def cmd_space_show(args) -> int:
    try:
        settings = load_settings(args.settings)
        space = load_space_with_snapshots(Path(args.space), Path(settings.data_dir))
    except (ConfigError, SpaceError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    print(space.describe())
    patches = Journal(journal_path(settings)).patches()
    if patches:
        print("\n补丁历史：")
        for ev in patches:
            ops = ev.get("ops") or []
            desc = "; ".join(f"{o.get('op')}({o.get('param')})" for o in ops)
            print(f"  [{ev.get('ts')}] v{ev.get('version')} {desc} —— {ev.get('rationale')}")
    else:
        print("\n补丁历史：（无）")
    return 0


def cmd_report(args) -> int:
    try:
        settings = load_settings(args.settings)
        space = load_space_with_snapshots(Path(args.space), Path(settings.data_dir))
    except (ConfigError, SpaceError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    from tansuo.report import generate_report
    study = create_or_load_study(settings)
    journal = Journal(journal_path(settings))
    report_path, best_path = generate_report(settings, study, space, journal)
    print(f"报告已生成：{report_path}")
    print(f"最优配置导出：{best_path}")
    return 0


def cmd_api(args) -> int:
    from tansuo.agent.api_setup import run_api_setup
    return run_api_setup(args.settings, model=args.model)


def cmd_check(args) -> int:
    try:
        settings = load_settings(args.settings)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    if args.model:
        settings.agent.model = args.model
    from tansuo.agent.client import make_client, probe_endpoint
    cfg = settings.agent
    print(f"探测端点：base_url={cfg.base_url or '(SDK 默认/环境变量)'}，model={cfg.model}")
    if not (cfg.auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")):
        print("警告：未发现鉴权凭据（agent.auth_token / ANTHROPIC_AUTH_TOKEN / "
              "ANTHROPIC_API_KEY 均为空）", file=sys.stderr)
    result = probe_endpoint(make_client(cfg), cfg.model)
    if result["ok"]:
        print(f"✔ 两级探测通过（{result['detail']}）")
        return 0
    print(f"✘ 探测失败于 [{result['stage']}]：{result['detail']}", file=sys.stderr)
    return 1


def cmd_init(args) -> int:
    from tansuo.wizard import init_templates
    init_templates(args.settings, args.space, force=args.force)
    return 0


def cmd_web(args) -> int:
    # 路径以绝对路径经环境变量注入，后端不受启动目录影响。
    # 必须在导入 app 之前设置：app 模块加载时就读取这两个环境变量，
    # 顺序颠倒会让 --settings/--space 被静默忽略（永远回退 demo 配置）。
    os.environ["TANSUO_SETTINGS"] = str(Path(args.settings).resolve())
    os.environ["TANSUO_SPACE"] = str(Path(args.space).resolve())
    import uvicorn
    from tansuo.web.app import app
    print(f"Web 后端：http://{args.host}:{args.port}")
    print(f"前端开发模式：cd web && npm run dev（自动代理 /api → :{args.port}）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_setup(args) -> int:
    train = Path(args.train)
    if not train.exists():
        print(f"训练脚本不存在：{train}", file=sys.stderr)
        return 2
    try:
        settings = load_settings(args.settings)
    except ConfigError:
        from tansuo.config import Settings
        settings = Settings()
        print("（settings.yaml 缺失/无效，agent 连接用默认配置：模型 "
              f"{settings.agent.model}，端点/凭据读环境变量）")
    from tansuo.agent.client import make_client, probe_endpoint
    from tansuo.agent.loop import SetupAgent
    print(f"[setup] 探测端点（model={settings.agent.model}）……")
    probe = probe_endpoint(make_client(settings.agent), settings.agent.model)
    if not probe["ok"]:
        print(f"✘ 端点探测失败于 [{probe['stage']}]：{probe['detail']}\n"
              f"兜底方案：`python cli.py init` 生成离线配置模板手工填写。", file=sys.stderr)
        return 1
    print(f"[setup] 端点可用，开始为 {train} 生成配置……")
    journal = Journal(Path(settings.data_dir) / "journal.jsonl")
    agent = SetupAgent(settings, journal, args.settings, args.space, str(train))
    try:
        summary = agent.run()
    except Exception as e:   # noqa: BLE001
        print(f"✘ 配置会话失败：{e}\n兜底方案：`python cli.py init` 生成离线模板。",
              file=sys.stderr)
        return 1
    print("\n===== 配置 agent 摘要 =====")
    print(summary)
    print(f"\n配置已写入：{args.settings} 与 {args.space}")
    print("建议下一步：python cli.py run --trials 3 --no-agent  # 冒烟验证")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="tansuo_agent：智能超参数调节 agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings", default=DEFAULT_SETTINGS, help="settings.yaml 路径")
    common.add_argument("--space", default=DEFAULT_SPACE, help="search_space.yaml 路径")

    pr = sub.add_parser("run", parents=[common], help="跑超参数搜索")
    pr.add_argument("--trials", type=int, default=None, help="本次会话试验预算（默认取 settings）")
    pr.add_argument("--wake-every", type=int, default=None, help="每多少次试验唤醒 agent")
    pr.add_argument("--no-agent", action="store_true", help="禁用 agent，纯 Optuna 巡航")
    pr.add_argument("--resume", action="store_true",
                    help="断点续跑（默认行为即续跑；此标志为显式声明）")
    pr.add_argument("--seed", type=int, default=None, help="覆盖 TPE 采样种子")
    pr.add_argument("--model", default=None, help="覆盖 agent 模型名")
    pr.add_argument("--workers", type=int, default=None,
                    help="并行试验数（默认取 settings budget.workers，1=串行）")
    pr.add_argument("--hours", type=float, default=None,
                    help="会话时间预算（小时）：到点在途试验跑完后优雅收尾")
    pr.add_argument("--fresh", action="store_true", help="清空历史（db/journal/快照）重新开始")
    pr.set_defaults(fn=cmd_run)

    ps = sub.add_parser("space", parents=[common], help="搜索空间管理")
    ps_sub = ps.add_subparsers(dest="space_cmd", required=True)
    ps_show = ps_sub.add_parser("show", help="查看当前空间与补丁历史")
    ps_show.set_defaults(fn=cmd_space_show)

    pi = sub.add_parser("init", parents=[common], help="生成离线配置模板（无需 LLM 的兜底）")
    pi.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    pi.set_defaults(fn=cmd_init)

    psu = sub.add_parser("setup", parents=[common],
                         help="配置 agent：读训练脚本自动起草 settings 与搜索空间")
    psu.add_argument("--train", required=True, help="训练脚本路径（如 examples/train_mnist.py）")
    psu.set_defaults(fn=cmd_setup)

    prep = sub.add_parser("report", parents=[common], help="生成分析报告与 best.yaml")
    prep.set_defaults(fn=cmd_report)

    pc = sub.add_parser("check", parents=[common], help="两级探测 LLM 端点（ping + tool-use）")
    pc.add_argument("--model", default=None, help="覆盖 settings 中的模型名")
    pc.set_defaults(fn=cmd_check)

    pa = sub.add_parser("api", parents=[common],
                        help="大模型 API 自配置：探测环境→验证模型→写回 settings")
    pa.add_argument("--model", default=None, help="优先探测的模型名")
    pa.set_defaults(fn=cmd_api)

    pw = sub.add_parser("web", parents=[common], help="启动 Web 后端（可视化界面 API）")
    pw.add_argument("--host", default="127.0.0.1", help="监听地址")
    pw.add_argument("--port", type=int, default=8000, help="监听端口")
    pw.set_defaults(fn=cmd_web)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run" and args.fresh:
        try:
            settings = load_settings(args.settings)
        except ConfigError as e:
            print(f"配置错误：{e}", file=sys.stderr)
            return 2
        data_dir = Path(settings.data_dir)
        removed = []
        for pat in ("*.db", "journal.jsonl", "space_v*.yaml"):
            for f in data_dir.glob(pat):
                f.unlink(missing_ok=True)
                removed.append(f.name)
        if removed:
            print(f"--fresh：已清理 {', '.join(removed)}")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
