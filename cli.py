"""tansuo_agent 命令行入口。

子命令：
  run    跑超参数搜索（--no-agent 纯 Optuna 巡航；默认预算见 settings.yaml）
  runs   查看记录分区（记录永不删除；按双指纹自动分区）
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

from tansuo.cohort import (CohortError, abs_data_dir, apply_cohort, code_fingerprint,
                           cohort_stats, list_cohorts, load_cohort, migrate_legacy,
                           resolve_for_run)
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


def _make_runtime(args, settings=None):
    if settings is None:
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
        settings = load_settings(args.settings)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    # ---- 记录分区：迁移旧布局 → 决定分区 → 改写 settings（严格在运行时组装之前）----
    force_new = args.new or args.fresh
    if args.fresh:
        print("--fresh 现等价于 --new：新开一个记录分区，不再删除任何历史记录。")
    if args.note and not force_new and not args.cohort:
        print("提示：--note 会写入分区 meta.yaml（本次新建分区时生效）。")
    base = Path.cwd()
    try:
        mig = migrate_legacy(settings, base_dir=base)
        if mig["moved"]:
            print(f"[记录] 旧布局记录已迁入 runs/0000-legacy/：{', '.join(mig['moved'])}")
        cohort, info = resolve_for_run(settings, force_new=force_new,
                                       cohort_id=args.cohort, note=args.note,
                                       base_dir=base)
        apply_cohort(settings, cohort)
    except CohortError as e:
        print(f"分区错误：{e}", file=sys.stderr)
        return 2
    action = info["action"]
    if action == "continue":
        print(f"[记录] 续跑分区 {cohort.id}（三指纹一致）｜ 记录目录：{cohort.path}")
    elif action == "created":
        print(f"[记录] 新开分区 {cohort.id}：{info['reason']}")
    else:  # explicit
        tails = []
        if not info["code_match"]:
            tails.append("代码指纹不一致")
        if not info.get("data_match", True):
            tails.append("数据集指纹不一致")
        tail = f"（{'、'.join(tails)}，按你的指定继续）" if tails else ""
        print(f"[记录] 续跑指定分区 {cohort.id}{tail}")
    fp_match = (action != "explicit") or (bool(info.get("code_match"))
                                          and bool(info.get("data_match", True)))

    try:
        settings, orch = _make_runtime(args, settings=settings)
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
             workers=args.workers, max_duration_h=args.hours,
             cohort=cohort.id, cohort_fp=cohort.meta.get("code_hash"),
             fp_match=fp_match)
    try:
        from tansuo.report import generate_report
        report_path, best_path = generate_report(
            settings, orch.study, orch.space, orch.journal,
            cohort_info={"id": cohort.id,
                         "fingerprint": cohort.meta.get("code_hash", ""),
                         "note": cohort.meta.get("note", "")})
        print(f"报告：{report_path} ｜ 最优配置：{best_path}")
    except Exception as e:   # noqa: BLE001 —— 报告失败不影响搜索本身的成果
        print(f"报告生成失败（不影响已完成的搜索）：{e}", file=sys.stderr)
    return 0


def _pick_read_cohort(settings, cohort_id: str | None):
    """只读命令（space show / report）的分区解析：
    显式 ID → 该分区；缺省 → 最新分区；无任何分区 → None（扁平布局兜底）。"""
    data_dir = abs_data_dir(settings, Path.cwd())
    if cohort_id:
        return load_cohort(data_dir, cohort_id, settings=settings)
    cohorts = list_cohorts(data_dir, settings=settings)
    return cohorts[-1] if cohorts else None


def cmd_space_show(args) -> int:
    try:
        settings = load_settings(args.settings)
        cohort = _pick_read_cohort(settings, getattr(args, "cohort", None))
        if cohort is not None:
            apply_cohort(settings, cohort)
            print(f"[记录] 查看分区 {cohort.id}")
        space = load_space_with_snapshots(Path(args.space), Path(settings.data_dir))
    except (ConfigError, SpaceError, CohortError) as e:
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
        cohort = _pick_read_cohort(settings, getattr(args, "cohort", None))
        if cohort is not None:
            apply_cohort(settings, cohort)
            print(f"[记录] 为分区 {cohort.id} 生成报告")
        space = load_space_with_snapshots(Path(args.space), Path(settings.data_dir))
    except (ConfigError, SpaceError, CohortError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    # 存储文件不存在 → 该分区从未跑过：明确提示而不是凭空创建空库
    url = settings.storage.url
    for scheme in ("sqlite:///", "journal://"):
        if url.startswith(scheme):
            store_file = Path(url[len(scheme):])
            if not store_file.exists():
                print("该分区尚无试验记录（存储文件不存在），无报告可生成。",
                      file=sys.stderr)
                return 1
            break
    from tansuo.report import generate_report
    from tansuo.study import STUDY_NAME, make_storage
    try:
        study = optuna.load_study(storage=make_storage(url), study_name=STUDY_NAME)
    except KeyError:
        print("该分区尚无试验记录，无报告可生成。", file=sys.stderr)
        return 1
    except Exception as e:   # noqa: BLE001 —— sqlite 被占用等
        print(f"无法打开试验数据库（可能被其他进程占用）：{e}", file=sys.stderr)
        return 1
    journal = Journal(journal_path(settings))
    cohort_info = None
    if cohort is not None and not cohort.virtual:
        cohort_info = {"id": cohort.id,
                       "fingerprint": cohort.meta.get("code_hash", ""),
                       "note": cohort.meta.get("note", "")}
    report_path, best_path = generate_report(settings, study, space, journal,
                                             cohort_info=cohort_info)
    print(f"报告已生成：{report_path}")
    print(f"最优配置导出：{best_path}")
    return 0


def cmd_runs(args) -> int:
    """列出所有记录分区及与当前指纹的可比性。"""
    try:
        settings = load_settings(args.settings)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    base = Path.cwd()
    data_dir = abs_data_dir(settings, base)
    fp = code_fingerprint(settings, base)
    cohorts = list_cohorts(data_dir, settings=settings)
    if not cohorts:
        print("还没有记录分区（运行 `python cli.py run` 会创建第一个分区）。")
        return 0
    print(f"== 记录分区（{data_dir / 'runs'}）==")
    for c in cohorts:
        st = cohort_stats(c)
        meta = c.meta or {}
        oh = str(meta.get("objective_hash") or "-")[:8]
        ch = str(meta.get("code_hash") or "-")[:8]
        dh = str(meta.get("data_hash") or "-")[:8]
        if not (meta.get("objective_hash") or meta.get("code_hash")):
            comp = "— 历史记录（无指纹）"
        elif meta.get("objective_hash") != fp.objective_hash:
            comp = "✘ 目标已变，不可直接比较"
        else:
            code_diff = meta.get("code_hash") != fp.code_hash
            data_diff = meta.get("data_hash") != fp.data_hash
            if code_diff and data_diff:
                comp = "△ 目标一致、训练代码与数据集已变"
            elif code_diff:
                comp = "△ 目标一致、训练代码已变"
            elif data_diff:
                comp = ("△ 目标一致、数据集已变" if meta.get("data_hash")
                        else "△ 目标一致、无数据集指纹（旧记录）")
            else:
                comp = "✔ 与当前指纹完全一致"
        best = f"{st['best']:.6g}" if st["best"] is not None else "-"
        lock = "（db 被占用，统计降级）" if st["locked"] else ""
        note = meta.get("note") or ""
        note_s = f" 备注「{note}」" if note else ""
        print(f"  {c.id}  {meta.get('created_at', '-')}"
              f"  试验 {st['completed']:>4}  最优 {best:>12}"
              f"  目标 {oh}  代码 {ch}  数据 {dh}  {comp}{lock}{note_s}")
    rel = "" if fp.reliable else "（不可靠：未定位到脚本文件，仅哈希了命令串）"
    data_rel = "" if fp.data_reliable else \
        "（数据集未跟踪：python 模式且未声明 experiment.dataset）"
    print(f"当前指纹：目标={fp.objective_hash} 代码={fp.code_hash}"
          f" 数据集={fp.data_hash}{rel}{data_rel}")
    return 0


def cmd_runs_show(args) -> int:
    try:
        settings = load_settings(args.settings)
        data_dir = abs_data_dir(settings, Path.cwd())
        cohort = load_cohort(data_dir, args.cohort_id, settings=settings)
    except (ConfigError, CohortError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    meta = cohort.meta or {}
    print(f"== 分区 {cohort.id} ==")
    print(f"目录：{cohort.path}")
    print(f"创建时间：{meta.get('created_at', '-')}")
    if meta.get("note"):
        print(f"备注：{meta['note']}")
    flags = []
    if cohort.virtual:
        flags.append("虚拟（旧布局尚未物理迁移）")
    if cohort.incomplete:
        flags.append("meta 缺失/损坏")
    if flags:
        print(f"状态：{'；'.join(flags)}")
    if meta.get("objective_hash"):
        print(f"objective_hash：{meta['objective_hash']}")
        print(f"code_hash：{meta['code_hash']}")
        print(f"data_hash：{meta.get('data_hash') or '（无——创建于数据集分区引入前）'}")
        oi = meta.get("objective_inputs") or {}
        print(f"优化目标：{oi.get('primary', '-')} ｜ data_fraction={oi.get('data_fraction', '-')}")
        ci = meta.get("code_inputs") or {}
        files = ci.get("files") or []
        if files:
            print(f"指纹覆盖文件（mode={ci.get('mode')}）：")
            for fe in files:
                print(f"  {fe.get('path')}  {str(fe.get('sha256', ''))[:12]}")
        else:
            print(f"指纹模式：{ci.get('mode', '-')}（未覆盖任何文件，指纹不可靠）")
        if ci.get("missing"):
            print(f"未定位到：{', '.join(ci['missing'])}")
        if ci.get("skipped"):
            print(f"跳过（过大）：{', '.join(ci['skipped'])}")
        di = meta.get("data_inputs") or {}
        if di:
            vals = di.get("values") or []
            print(f"数据集指纹（mode={di.get('mode', '-')}）："
                  + (" ".join(vals) if vals else "（无取值）")
                  + ("；未跟踪——声明 experiment.dataset 可参与分区隔离"
                     if di.get("mode") == "untracked" else ""))
    pm = meta.get("primary_metric") or {}
    if pm:
        print(f"主指标：{pm.get('name')}（{pm.get('direction')}）")
    if meta.get("storage_url"):
        print(f"外部存储：{meta['storage_url']}")
    st = cohort_stats(cohort)
    best = f"{st['best']:.6g}" if st["best"] is not None else "-"
    print(f"已完成试验：{st['completed']} ｜ 最优值：{best}"
          + ("（db 被占用，统计降级）" if st["locked"] else ""))
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
    # setup 事件写独立 journal：与搜索记录分离，也不会被旧布局迁移误收
    journal = Journal(Path(settings.data_dir) / "setup_journal.jsonl")
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
    pr.add_argument("--new", action="store_true",
                    help="强制新开一个记录分区（不删除任何历史记录）")
    pr.add_argument("--note", default=None, help="分区备注（写入 meta.yaml，便于日后辨认）")
    pr.add_argument("--cohort", default=None, metavar="ID",
                    help="续跑指定分区（优化目标语义不符时会被拒绝）")
    pr.add_argument("--fresh", action="store_true",
                    help="已废弃的别名：等价 --new，不再删除任何记录")
    pr.set_defaults(fn=cmd_run)

    pru = sub.add_parser("runs", parents=[common],
                         help="列出记录分区（记录永不删除，按双指纹自动分区）")
    pru_sub = pru.add_subparsers(dest="runs_cmd")
    pru_show = pru_sub.add_parser("show", parents=[common], help="查看某个分区的详细信息")
    pru_show.add_argument("cohort_id", help="分区 ID（如 0001-20260811-120000 或 0000-legacy）")
    pru_show.set_defaults(fn=cmd_runs_show)
    pru.set_defaults(fn=cmd_runs)

    ps = sub.add_parser("space", parents=[common], help="搜索空间管理")
    ps_sub = ps.add_subparsers(dest="space_cmd", required=True)
    ps_show = ps_sub.add_parser("show", help="查看当前空间与补丁历史")
    ps_show.add_argument("--cohort", default=None, metavar="ID",
                         help="查看指定分区的空间（默认最新分区）")
    ps_show.set_defaults(fn=cmd_space_show)

    pi = sub.add_parser("init", parents=[common], help="生成离线配置模板（无需 LLM 的兜底）")
    pi.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    pi.set_defaults(fn=cmd_init)

    psu = sub.add_parser("setup", parents=[common],
                         help="配置 agent：读训练脚本自动起草 settings 与搜索空间")
    psu.add_argument("--train", required=True, help="训练脚本路径（如 examples/train_mnist.py）")
    psu.set_defaults(fn=cmd_setup)

    prep = sub.add_parser("report", parents=[common], help="生成分析报告与 best.yaml")
    prep.add_argument("--cohort", default=None, metavar="ID",
                      help="为指定分区生成报告（默认最新分区）")
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
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
