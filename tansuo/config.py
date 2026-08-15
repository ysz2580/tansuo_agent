"""settings.yaml 的加载与校验（配置即文档：指标定义、适配器、预算、agent）。

关键约束：
- metrics.primary 必须且只能有一个，direction ∈ {maximize, minimize}；
- watch 为观测指标列表，名字不得重复、不得与 primary 重名；
- 支持 ${ENV:NAME} / ${ENV:NAME:default} 环境变量展开。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_DIRECTIONS = ("maximize", "minimize")
_ENV_PATTERN = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)(:([^}]*))?\}")


class ConfigError(ValueError):
    """配置错误（文件缺失、字段非法等），错误信息面向人类可读。"""


def _expand_env(value: Any, ctx: str = "") -> Any:
    """递归展开字符串中的 ${ENV:NAME} / ${ENV:NAME:default}。"""
    if isinstance(value, str):
        def _repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(3)
            v = os.environ.get(name)
            if v is None or v == "":
                if default is not None:
                    return default
                raise ConfigError(
                    f"配置 {ctx or '<root>'} 引用了环境变量 {name}，但它未设置。"
                    f"可写成 ${{ENV:{name}:默认值}} 提供兜底。"
                )
            return v
        return _ENV_PATTERN.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{ctx}.{k}" if ctx else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{ctx}[{i}]") for i, v in enumerate(value)]
    return value


@dataclass
class MetricSpec:
    name: str
    direction: str  # maximize | minimize

    @property
    def better(self) -> str:
        return "越大越好" if self.direction == "maximize" else "越小越好"


@dataclass
class MetricsCfg:
    primary: MetricSpec
    watch: list[MetricSpec] = field(default_factory=list)

    def all_names(self) -> list[str]:
        return [self.primary.name] + [m.name for m in self.watch]


@dataclass
class AdapterCfg:
    mode: str = "subprocess"          # subprocess | python
    command: list[str] = field(default_factory=list)
    entry: str = ""                   # mode=python: "module.path:fn"
    config_via: str = "env"           # env | file
    timeout_s: int = 300
    retry_on_fail: int = 1            # 瞬时失败自动重试次数（仅"非零退出码且 stderr 为空"）；默认 1 兜底环境噪声
    iter_param: str = ""              # 训练轮数维度参数名（超时校准用）；空=按 epoch/step/iter/round 自动识别


@dataclass
class BudgetCfg:
    total_trials: int = 30
    wake_every: int = 5
    seed: int = 42
    data_fraction: float = 1.0   # 训练集抽样比例（加速开关，注入 TANSUO_DATA_FRACTION）
    workers: int = 1             # 并行试验数（多线程 ask/tell + 每试验独立子进程）
    max_duration_h: float | None = None   # 会话时间预算（小时）；到点优雅收尾
    warm_start: int = 3          # 新分区热启动：入队同目标旧分区 top-k 配置为种子试验（0=关）


@dataclass
class PrunerCfg:
    type: str = "median"              # median | hyperband
    # median 用：
    n_startup_trials: int = 4
    n_warmup_steps: int = 1
    # hyperband 用（median 时忽略）：
    min_resource: int = 1             # 最小训练步数（epoch），达到前不剪枝
    max_resource: int | str = "auto"  # 最大训练步数；"auto"=按已完结试验的最大步数推断
    reduction_factor: int = 3         # 逐层晋级比例（η）：每轮约 1/η 试验晋级


@dataclass
class AgentCfg:
    enabled: bool = True
    model: str = "qwen3-max"
    base_url: str = ""
    auth_token: str = ""
    max_wake_rounds: int = 6
    max_turns_per_wake: int = 10
    max_tool_calls_per_wake: int = 8
    max_space_edits_total: int = 6
    max_consecutive_failures: int = 3
    permissions: dict = field(default_factory=dict)   # 权限 hook：tool→allow/confirm/deny


@dataclass
class StorageCfg:
    url: str = "sqlite:///data/tansuo.db"


@dataclass
class NotifyCfg:
    """会话结束 / agent 降级的 webhook 通知（见 tansuo/notify.py）。

    webhook_url 支持 ${ENV:变量名} 引用（load_settings 统一展开），
    避免把含 token 的机器人地址明文提交进仓库。
    """
    enabled: bool = True
    webhook_url: str = ""
    format: str = "generic"   # generic / dingtalk / lark / slack
    events: list = field(default_factory=lambda: ["session_end", "agent_degrade"])


@dataclass
class Settings:
    experiment_name: str = "experiment"
    data_dir: str = "data"
    metrics: MetricsCfg = field(default_factory=lambda: MetricsCfg(MetricSpec("value", "maximize")))
    adapter: AdapterCfg = field(default_factory=AdapterCfg)
    budget: BudgetCfg = field(default_factory=BudgetCfg)
    pruner: PrunerCfg = field(default_factory=PrunerCfg)
    agent: AgentCfg = field(default_factory=AgentCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    fingerprint_paths: list[str] = field(default_factory=list)  # 分区代码指纹附加文件/目录
    dataset: list[str] = field(default_factory=list)  # 数据集标识（参与分区数据集指纹）
    source_path: str = ""
    raw: dict = field(default_factory=dict)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _parse_metric(d: Any, ctx: str) -> MetricSpec:
    _require(isinstance(d, dict), f"{ctx} 必须是映射（含 name 与 direction），实际是 {type(d).__name__}")
    name = str(d.get("name") or "").strip()
    _require(bool(name), f"{ctx}.name 缺失：每个指标必须声明名字（训练脚本协议行 metrics 里的键名）")
    direction = str(d.get("direction") or "").strip().lower()
    _require(
        direction in VALID_DIRECTIONS,
        f"{ctx}.direction 非法：'{d.get('direction')}'，必须是 maximize(越大越好) 或 minimize(越小越好)",
    )
    return MetricSpec(name=name, direction=direction)


def load_settings(path: str | Path = "configs/settings.yaml") -> Settings:
    """加载并强校验 settings.yaml。任何非法字段抛 ConfigError（信息可读）。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"找不到配置文件 {path}。可运行 `python cli.py setup --train 你的训练脚本` "
            f"让配置 agent 自动生成，或 `python cli.py init` 生成离线模板。"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} YAML 解析失败：{e}") from e
    _require(isinstance(raw, dict), f"{path} 顶层必须是映射")
    raw = _expand_env(raw)

    # ---- metrics ----
    m = raw.get("metrics") or {}
    _require(isinstance(m, dict) and m.get("primary") is not None,
             "settings 缺少 metrics.primary：必须声明唯一主优化指标（name + direction）")
    primary = _parse_metric(m["primary"], "metrics.primary")
    watch_raw = m.get("watch") or []
    _require(isinstance(watch_raw, list), "metrics.watch 必须是列表")
    watch = [_parse_metric(w, f"metrics.watch[{i}]") for i, w in enumerate(watch_raw)]
    names = [primary.name] + [w.name for w in watch]
    _require(len(names) == len(set(names)),
             f"指标名重复：{names}（primary 与 watch 之间、watch 内部都不允许重名）")

    # ---- adapter ----
    a = raw.get("adapter") or {}
    adapter = AdapterCfg(
        mode=str(a.get("mode", "subprocess")).strip().lower(),
        command=list(a.get("command") or []),
        entry=str(a.get("entry") or "").strip(),
        config_via=str(a.get("config_via", "env")).strip().lower(),
        timeout_s=int(a.get("timeout_s", 300)),
        retry_on_fail=int(a.get("retry_on_fail", 1)),
        iter_param=str(a.get("iter_param") or "").strip(),
    )
    _require(adapter.mode in ("subprocess", "python"),
             f"adapter.mode 非法：'{adapter.mode}'，必须是 subprocess 或 python")
    if adapter.mode == "subprocess":
        _require(bool(adapter.command), "adapter.command 缺失：subprocess 模式必须给出启动命令，"
                                        "如 [\"python\", \"examples/train_mnist.py\"]")
        adapter.command = [str(c) for c in adapter.command]
    else:
        _require(":" in adapter.entry,
                 f"adapter.entry 格式应为 'module.path:函数名'，实际：'{adapter.entry}'")
    _require(adapter.config_via in ("env", "file"),
             f"adapter.config_via 非法：'{adapter.config_via}'，必须是 env 或 file")
    _require(adapter.timeout_s >= 5, "adapter.timeout_s 不能小于 5 秒")
    _require(0 <= adapter.retry_on_fail <= 3,
             f"adapter.retry_on_fail 应在 [0, 3] 内（0=不重试），实际 {adapter.retry_on_fail}")

    # ---- budget ----
    b = raw.get("budget") or {}
    max_duration_h = b.get("max_duration_h")
    budget = BudgetCfg(
        total_trials=int(b.get("total_trials", 30)),
        wake_every=int(b.get("wake_every", 5)),
        seed=int(b.get("seed", 42)),
        data_fraction=float(b.get("data_fraction", 1.0)),
        workers=int(b.get("workers", 1)),
        max_duration_h=float(max_duration_h) if max_duration_h is not None else None,
        warm_start=int(b.get("warm_start", 3)),
    )
    _require(budget.total_trials >= 1, "budget.total_trials 必须 ≥ 1")
    _require(1 <= budget.wake_every <= budget.total_trials,
             f"budget.wake_every 应在 [1, total_trials={budget.total_trials}] 内，实际 {budget.wake_every}")
    _require(0.0 < budget.data_fraction <= 1.0,
             f"budget.data_fraction 应在 (0, 1] 内，实际 {budget.data_fraction}")
    _require(1 <= budget.workers <= 32,
             f"budget.workers 应在 [1, 32] 内（1=串行），实际 {budget.workers}")
    _require(budget.max_duration_h is None or budget.max_duration_h > 0,
             f"budget.max_duration_h 必须是正数（小时），实际 {budget.max_duration_h}")
    _require(0 <= budget.warm_start <= 8,
             f"budget.warm_start 应在 [0, 8] 内（0=不热启动），实际 {budget.warm_start}")

    # ---- pruner ----
    p = raw.get("pruner") or {}
    max_resource = p.get("max_resource", "auto")
    pruner = PrunerCfg(
        type=str(p.get("type", "median")).strip().lower(),
        n_startup_trials=int(p.get("n_startup_trials", 4)),
        n_warmup_steps=int(p.get("n_warmup_steps", 1)),
        min_resource=int(p.get("min_resource", 1)),
        reduction_factor=int(p.get("reduction_factor", 3)),
    )
    _require(pruner.type in ("median", "hyperband"),
             f"pruner.type 必须是 median 或 hyperband，实际：'{pruner.type}'")
    _require(pruner.n_startup_trials >= 0 and pruner.n_warmup_steps >= 0,
             "pruner.n_startup_trials / n_warmup_steps 不能为负")
    if pruner.type == "hyperband":
        _require(pruner.min_resource >= 1,
                 f"pruner.min_resource 必须 ≥ 1，实际 {pruner.min_resource}")
        _require(pruner.reduction_factor >= 2,
                 f"pruner.reduction_factor 必须 ≥ 2（每轮晋级比例 η），实际 {pruner.reduction_factor}")
        if isinstance(max_resource, str):
            _require(max_resource.strip().lower() == "auto",
                     f"pruner.max_resource 必须是正整数或 'auto'，实际：'{max_resource}'")
            pruner.max_resource = "auto"
        else:
            mr = int(max_resource)
            _require(mr > pruner.min_resource,
                     f"pruner.max_resource 必须大于 min_resource={pruner.min_resource}，实际 {mr}")
            pruner.max_resource = mr

    # ---- agent ----
    g = raw.get("agent") or {}
    agent = AgentCfg(
        enabled=bool(g.get("enabled", True)),
        model=str(g.get("model", "qwen3-max")).strip(),
        base_url=str(g.get("base_url") or "").strip(),
        auth_token=str(g.get("auth_token") or "").strip(),
        max_wake_rounds=int(g.get("max_wake_rounds", 6)),
        max_turns_per_wake=int(g.get("max_turns_per_wake", 10)),
        max_tool_calls_per_wake=int(g.get("max_tool_calls_per_wake", 8)),
        max_space_edits_total=int(g.get("max_space_edits_total", 6)),
        max_consecutive_failures=int(g.get("max_consecutive_failures", 3)),
    )
    _require(agent.max_wake_rounds >= 1, "agent.max_wake_rounds 必须 ≥ 1")
    _require(bool(agent.model), "agent.model 不能为空")
    perms_raw = g.get("permissions") or {}
    _require(isinstance(perms_raw, dict), "agent.permissions 必须是映射（工具名 → allow/confirm/deny）")
    perms: dict = {}
    for tool_name, policy in perms_raw.items():
        policy = str(policy).strip().lower()
        _require(policy in ("allow", "confirm", "deny"),
                 f"agent.permissions.{tool_name} 非法：'{policy}'，必须是 allow/confirm/deny")
        perms[str(tool_name).strip()] = policy
    agent.permissions = perms

    # ---- storage ----
    s = raw.get("storage") or {}
    storage = StorageCfg(url=str(s.get("url", "sqlite:///data/tansuo.db")).strip())
    _require(storage.url.startswith(("sqlite:///", "journal://")),
             f"storage.url 必须以 sqlite:/// 或 journal:// 开头，实际：'{storage.url}'")

    # ---- notify ----
    from .notify import VALID_EVENTS, VALID_FORMATS
    n = raw.get("notify") or {}
    notify = NotifyCfg(
        enabled=bool(n.get("enabled", True)),
        webhook_url=str(n.get("webhook_url") or "").strip(),
        format=(str(n.get("format", "generic")).strip().lower() or "generic"),
    )
    _require(notify.format in VALID_FORMATS,
             f"notify.format 必须是 {'/'.join(VALID_FORMATS)} 之一，实际：'{notify.format}'")
    ev_raw = n.get("events")
    if ev_raw is not None:
        _require(isinstance(ev_raw, list),
                 f"notify.events 必须是列表（可选：{'/'.join(VALID_EVENTS)}），"
                 f"实际是 {type(ev_raw).__name__}")
        events: list = []
        for i, v in enumerate(ev_raw):
            v = str(v).strip()
            _require(v in VALID_EVENTS,
                     f"notify.events[{i}] 非法：'{v}'，必须是 {'/'.join(VALID_EVENTS)} 之一")
            events.append(v)
        notify.events = events

    exp = raw.get("experiment") or {}
    fp_paths_raw = exp.get("fingerprint_paths") or []
    _require(isinstance(fp_paths_raw, list),
             "experiment.fingerprint_paths 必须是列表（参与分区代码指纹的文件/目录）")
    fingerprint_paths = []
    for i, v in enumerate(fp_paths_raw):
        _require(isinstance(v, str) and v.strip(),
                 f"experiment.fingerprint_paths[{i}] 必须是非空字符串路径，实际：{v!r}")
        fingerprint_paths.append(v.strip())
    # 数据集标识：字符串或字符串列表（名称/路径皆可，原样参与数据集指纹，不读文件）
    ds_raw = exp.get("dataset")
    dataset: list[str] = []
    if ds_raw is not None:
        if isinstance(ds_raw, str):
            ds_raw = [ds_raw]
        _require(isinstance(ds_raw, list),
                 "experiment.dataset 必须是字符串或字符串列表（数据集名称/路径），"
                 f"实际是 {type(ds_raw).__name__}")
        for i, v in enumerate(ds_raw):
            _require(isinstance(v, str) and v.strip(),
                     f"experiment.dataset[{i}] 必须是非空字符串，实际：{v!r}")
            dataset.append(v.strip())
    return Settings(
        experiment_name=str(exp.get("name", "experiment")).strip() or "experiment",
        data_dir=str(exp.get("data_dir", "data")).strip() or "data",
        metrics=MetricsCfg(primary=primary, watch=watch),
        adapter=adapter,
        budget=budget,
        pruner=pruner,
        agent=agent,
        storage=storage,
        notify=notify,
        fingerprint_paths=fingerprint_paths,
        dataset=dataset,
        source_path=str(path),
        raw=raw,
    )
