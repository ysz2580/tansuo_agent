"""搜索空间：ParamSpec + SearchSpace + patch 引擎（带 envelope 护栏）。

设计要点（与计划一致）：
- 每个参数保留两份边界：当前生效边界（low/high/choices，可被 agent 修改）
  与初始包络 envelope（env_low/env_high/env_choices，永久不变）。
  agent 只能"聚焦或还原"，任何变化必须 ⊆ envelope。
- 参数名永不改变（TPE 按名建模）；冻结参数不调 trial.suggest_*，直接注入常量。
- apply_patch 原子提交：所有 op 先在副本上校验，全部通过才生效，版本号 +1。
- suggest(trial) 每次从"当前"空间读取——这是 Optuna 官方支持的动态空间机制。
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 冻结哨兵：frozen is None 表示未冻结。
# 合法冻结值只可能是 str/int/float（choice 取值或数值），永远不可能是 None，
# 且 None 在 deepcopy 下保持身份不变（自定义哨兵对象 deepcopy 后会变成新对象）。

MAX_OPS_PER_PATCH = 4      # 单次 edit_search_space 最多几条 op
MIN_FREE_PARAMS = 3        # 补丁后至少保留多少个非冻结参数（禁止把空间冻死）
# 注意：没有 set_choices——Optuna storage 禁止修改 CategoricalDistribution
# （"does not support dynamic value space"），也不允许给已完成试验补参数。
# 因此分类参数的聚焦手段是 freeze；数值参数可自由 narrow/widen（动态安全）。
VALID_OPS = ("narrow", "widen", "freeze", "release")
_EPS = 1e-9


class SpaceError(ValueError):
    """搜索空间定义/补丁非法。"""


@dataclass
class PatchResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    new_version: int | None = None
    hint: str = ""


@dataclass
class ParamSpec:
    name: str
    kind: str                        # 'choice' | 'float' | 'int'
    choices: list | None = None      # 当前生效取值集（choice）
    low: float | None = None         # 当前生效边界（float/int）
    high: float | None = None
    log: bool = False
    description: str = ""
    # ---- envelope：初始包络，永久不变 ----
    env_choices: list | None = None
    env_low: float | None = None
    env_high: float | None = None
    # ---- 冻结状态 ----
    frozen: Any = None
    # ---- 条件依赖：{父参数名: [允许取值...]}，全部满足才生效（父参数必须是先定义的 choice）----
    depends_on: dict | None = None

    @property
    def is_frozen(self) -> bool:
        return self.frozen is not None

    @property
    def is_conditional(self) -> bool:
        return bool(self.depends_on)

    def condition_met(self, cfg: dict) -> bool:
        """已构建的配置是否满足 depends_on（无依赖恒为 True）。父参数缺失视为不满足。"""
        if not self.depends_on:
            return True
        for pname, vals in self.depends_on.items():
            if pname not in cfg or cfg[pname] not in vals:
                return False
        return True

    def dep_text(self) -> str:
        """依赖关系的可读文本（注入 agent 上下文 / space show / 报告）。"""
        if not self.depends_on:
            return ""
        parts = [f"{pn} ∈ {{{', '.join(map(str, vs))}}}"
                 for pn, vs in self.depends_on.items()]
        return "仅当 " + " 且 ".join(parts) + " 时生效"

    def domain_contains(self, value: Any) -> bool:
        if self.kind == "choice":
            return value in (self.choices or [])
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if self.kind == "int" and not float(value).is_integer():
            return False
        if self.log and v <= 0:
            return False
        return (self.low - _EPS) <= v <= (self.high + _EPS)

    def domain_text(self) -> str:
        if self.kind == "choice":
            return "{" + ", ".join(str(c) for c in (self.choices or [])) + "}"
        scale = "log" if self.log else "linear"
        fmt = lambda v: f"{v:g}"
        return f"[{fmt(self.low)}, {fmt(self.high)}] ({scale})"

    def brief(self) -> str:
        frozen_txt = f"  【已冻结={self.frozen}】" if self.is_frozen else ""
        dep_txt = f"  【{self.dep_text()}】" if self.is_conditional else ""
        return f"- {self.name} [{self.kind}] {self.domain_text()}{frozen_txt}{dep_txt}" + (
            f"\n    含义: {self.description}" if self.description else "")


class SearchSpace:
    def __init__(self, params: list[ParamSpec]):
        if not params:
            raise SpaceError("搜索空间不能为空（params 至少要有一个参数）")
        self.params = params
        self.version = 1
        self.patch_history: list[dict] = []
        self._reindex()

    def _reindex(self) -> None:
        self._by_name = {p.name: p for p in self.params}

    # ================= 加载 / 输出 =================

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SearchSpace":
        path = Path(path)
        if not path.exists():
            raise SpaceError(
                f"找不到搜索空间文件 {path}。可运行 `python cli.py setup --train 你的训练脚本` "
                f"自动生成，或 `python cli.py init` 生成离线模板。"
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise SpaceError(f"{path} YAML 解析失败：{e}") from e
        space = cls.from_dict(data)
        return space

    @classmethod
    def from_dict(cls, data: dict) -> "SearchSpace":
        """从 dict 构建空间并做完整性校验（也用于 setup agent 写盘前校验）。"""
        if not isinstance(data, dict):
            raise SpaceError("搜索空间顶层必须是映射（含 params 列表）")
        params_raw = data.get("params")
        if not isinstance(params_raw, list) or not params_raw:
            raise SpaceError("搜索空间必须包含非空的 params 列表")
        valid_keys = {"name", "type", "choices", "low", "high", "log",
                      "description", "env_choices", "env_low", "env_high", "frozen",
                      "depends_on"}
        params: list[ParamSpec] = []
        seen: set[str] = set()
        for i, pr in enumerate(params_raw):
            ctx = f"params[{i}]"
            if not isinstance(pr, dict):
                raise SpaceError(f"{ctx} 必须是映射")
            unknown = set(pr) - valid_keys
            if unknown:
                raise SpaceError(
                    f"{ctx} 含未知字段 {sorted(unknown)}，合法字段：{sorted(valid_keys)}（检查拼写）")
            name = str(pr.get("name") or "").strip()
            if not name:
                raise SpaceError(f"{ctx}.name 缺失")
            if name in seen:
                raise SpaceError(f"参数名重复：{name}（参数名是 TPE 建模的键，必须唯一）")
            seen.add(name)
            kind = str(pr.get("type") or "").strip().lower()
            if kind not in ("choice", "float", "int"):
                raise SpaceError(f"参数 {name} 的 type 非法：'{pr.get('type')}'，必须是 choice/float/int")
            desc = str(pr.get("description") or "").strip()
            if not desc:
                raise SpaceError(
                    f"参数 {name} 缺少 description：配置即文档，每个超参数都必须写明含义"
                    f"（它也是 agent 自主调节搜索空间的领域知识依据）")
            log = bool(pr.get("log", False))
            # ---- 条件依赖解析：父参数必须已定义（排在前面）、必须是 choice、取值 ⊆ 父候选集 ----
            dep_raw = pr.get("depends_on")
            depends_on: dict | None = None
            if dep_raw is not None:
                if not isinstance(dep_raw, dict) or not dep_raw:
                    raise SpaceError(
                        f"参数 {name} 的 depends_on 必须是非空映射"
                        f"（父参数名 → 取值或取值列表），实际：{dep_raw!r}")
                depends_on = {}
                by_name = {p.name: p for p in params}   # 只含本参数之前的定义
                for pname, pvals in dep_raw.items():
                    pname = str(pname)
                    parent = by_name.get(pname)
                    if parent is None:
                        raise SpaceError(
                            f"参数 {name} 依赖的父参数 '{pname}' 不存在，或定义在它后面"
                            f"（被依赖的参数必须排在前面，现有参数：{sorted(by_name)}）")
                    if parent.kind != "choice":
                        raise SpaceError(
                            f"参数 {name} 依赖的父参数 '{pname}' 必须是 choice 类型"
                            f"（当前仅支持分类父参数），实际是 {parent.kind}")
                    vals = pvals if isinstance(pvals, list) else [pvals]
                    if not vals:
                        raise SpaceError(f"参数 {name} 的依赖 depends_on.{pname} 取值列表为空")
                    bad = [v for v in vals if v not in (parent.env_choices or [])]
                    if bad:
                        raise SpaceError(
                            f"参数 {name} 的依赖取值 {bad} 不在父参数 {pname} 的候选集 "
                            f"{parent.env_choices} 内")
                    depends_on[pname] = vals
            if kind == "choice":
                choices = pr.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise SpaceError(f"参数 {name} 是 choice 类型，choices 必须是非空列表")
                if len(set(map(str, choices))) != len(choices):
                    raise SpaceError(f"参数 {name} 的 choices 有重复值：{choices}")
                if log:
                    raise SpaceError(f"参数 {name}：choice 类型不支持 log")
                env_choices = pr.get("env_choices")
                env_choices = list(env_choices) if isinstance(env_choices, list) else list(choices)
                if not set(map(str, choices)) <= set(map(str, env_choices)):
                    raise SpaceError(f"参数 {name} 的 choices 超出 envelope 范围")
                frozen = pr.get("frozen", None)
                params.append(ParamSpec(name=name, kind=kind, choices=list(choices),
                                        description=desc, env_choices=env_choices,
                                        frozen=frozen, depends_on=depends_on))
            else:
                try:
                    low, high = float(pr["low"]), float(pr["high"])
                except (KeyError, TypeError, ValueError):
                    raise SpaceError(f"参数 {name} 是 {kind} 类型，必须给出数值 low/high") from None
                if not (low < high):
                    raise SpaceError(f"参数 {name} 要求 low < high，实际 low={low}, high={high}")
                if kind == "int":
                    if not (float(low).is_integer() and float(high).is_integer()):
                        raise SpaceError(f"参数 {name} 是 int 类型，low/high 必须是整数")
                    low, high = int(low), int(high)
                if log and low <= 0:
                    raise SpaceError(f"参数 {name} 使用 log 尺度，low 必须 > 0")
                env_low = pr.get("env_low", low)
                env_high = pr.get("env_high", high)
                env_low, env_high = type(low)(env_low), type(high)(env_high)
                if not (env_low <= low and high <= env_high):
                    raise SpaceError(f"参数 {name} 当前边界 [{low}, {high}] 超出 envelope [{env_low}, {env_high}]")
                frozen = pr.get("frozen", None)
                params.append(ParamSpec(name=name, kind=kind, low=low, high=high, log=log,
                                        description=desc, env_low=env_low, env_high=env_high,
                                        frozen=frozen, depends_on=depends_on))
        space = cls(params)
        v = data.get("version")
        if isinstance(v, int) and v >= 1:
            space.version = v
        return space

    def to_dict(self) -> dict:
        out: dict = {"version": self.version, "params": []}
        for p in self.params:
            d: dict = {"name": p.name, "type": p.kind, "description": p.description}
            if p.kind == "choice":
                d["choices"] = list(p.choices or [])
                d["env_choices"] = list(p.env_choices or [])
            else:
                d.update({"low": p.low, "high": p.high, "log": p.log,
                          "env_low": p.env_low, "env_high": p.env_high})
            if p.is_frozen:
                d["frozen"] = p.frozen
            if p.is_conditional:
                # 单值写回标量、多值写回列表（from_dict 两种都接受）
                d["depends_on"] = {pn: (vs[0] if len(vs) == 1 else list(vs))
                                   for pn, vs in p.depends_on.items()}
            out["params"].append(d)
        return out

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)

    def snapshot(self, out_dir: str | Path) -> Path:
        """把当前空间版本落盘（审计 + 断点续跑时恢复空间状态）。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"space_v{self.version}.yaml"
        path.write_text(self.to_yaml(), encoding="utf-8")
        return path

    def describe(self) -> str:
        """带语义说明的空间文本（注入 agent 上下文 / space show / 报告）。"""
        lines = [f"搜索空间 v{self.version}"
                 f"（{len(self.params) - self.free_param_count()} 个已冻结 / {self.free_param_count()} 个自由）："]
        lines += [p.brief() for p in self.params]
        return "\n".join(lines)

    # ================= suggest / 校验 =================

    def free_param_count(self) -> int:
        return sum(1 for p in self.params if not p.is_frozen)

    def suggest(self, trial) -> dict:
        """从当前空间为一试验取样。冻结参数不 suggest、直接注入常量。

        条件参数（depends_on）仅在已构建配置满足条件时才取样；不满足时该试验
        params 缺少此键——TPE 对缺键历史试验自动跳过（Optuna 官方动态空间机制）。
        """
        cfg: dict = {}
        for p in self.params:
            if not p.condition_met(cfg):
                continue
            if p.is_frozen:
                cfg[p.name] = p.frozen
                continue
            if p.kind == "choice":
                cfg[p.name] = trial.suggest_categorical(p.name, list(p.choices))
            elif p.kind == "float":
                cfg[p.name] = trial.suggest_float(p.name, p.low, p.high, log=p.log)
            else:
                cfg[p.name] = trial.suggest_int(p.name, int(p.low), int(p.high),
                                                log=p.log)
        trial.set_user_attr("space_version", self.version)
        return cfg

    def inject(self, cfg: dict, trial=None) -> dict:
        """为自定义试验补齐冻结参数（agent 的 add_custom_trial 用）。
        条件冻结参数仅在依赖条件满足时注入。"""
        merged = dict(cfg)
        for p in self.params:
            if p.is_frozen and p.name not in merged and p.condition_met(merged):
                merged[p.name] = p.frozen
        if trial is not None:
            trial.set_user_attr("space_version", self.version)
        return merged

    def validate_config(self, cfg: dict) -> list[str]:
        """校验一组完整超参数取值是否在当前空间内（返回错误列表，空=通过）。"""
        errors: list[str] = []
        probe = self.inject(dict(cfg))   # 补齐冻结值后，再判断条件参数的生效状态
        for p in self.params:
            active = p.condition_met(probe)
            if p.name not in cfg:
                if not p.is_frozen and active:
                    errors.append(f"缺少参数 {p.name}")
                continue
            if not active:
                errors.append(
                    f"参数 {p.name} {p.dep_text()}，当前父参数取值不满足，不应提供该参数")
                continue
            v = cfg[p.name]
            if p.is_frozen and v != p.frozen:
                errors.append(f"参数 {p.name} 已冻结为 {p.frozen}，不能传 {v}（先 release 再改）")
                continue
            if not p.domain_contains(v):
                errors.append(f"参数 {p.name}={v!r} 超出当前定义域 {p.domain_text()}")
        unknown = set(cfg) - {p.name for p in self.params}
        if unknown:
            errors.append(f"未知参数：{sorted(unknown)}（参数名必须与搜索空间一致）")
        return errors

    # ================= patch 引擎 =================

    def apply_patch(self, ops: list[dict], rationale: str) -> PatchResult:
        """应用一组空间编辑 op（原子提交）。返回 PatchResult，不抛异常。"""
        rationale = str(rationale or "").strip()
        if not rationale:
            return PatchResult(ok=False, errors=["缺少 rationale"],
                               hint="每次编辑搜索空间必须给出理由（这是可审计的硬性要求）")
        if not isinstance(ops, list) or not ops:
            return PatchResult(ok=False, errors=["ops 必须是非空列表"],
                               hint=f"合法 op：{list(VALID_OPS)}")
        if len(ops) > MAX_OPS_PER_PATCH:
            return PatchResult(ok=False,
                               errors=[f"单次最多 {MAX_OPS_PER_PATCH} 条 op，收到 {len(ops)} 条"],
                               hint="把编辑拆成多轮，每轮聚焦最关键的改动")

        ws = copy.deepcopy(self)          # 工作副本：逐条校验+应用
        ws._reindex()
        errors: list[str] = []
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                errors.append(f"ops[{i}] 必须是映射")
                continue
            errors.extend(_apply_one_op(ws, op, i))
        if not errors and ws.free_param_count() < MIN_FREE_PARAMS:
            errors.append(
                f"补丁后自由参数只剩 {ws.free_param_count()} 个（< {MIN_FREE_PARAMS}），禁止把搜索空间冻死")

        if errors:
            return PatchResult(ok=False, errors=errors,
                               hint="修正上述问题后重试；先用 get_current_space 确认当前空间状态")
        self.params = ws.params
        self._reindex()
        self.version += 1
        self.patch_history.append({
            "version": self.version, "ops": ops, "rationale": rationale,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return PatchResult(ok=True, new_version=self.version)

    # ================= 静态工具 =================

    @staticmethod
    def latest_snapshot(out_dir: str | Path) -> Path | None:
        out_dir = Path(out_dir)
        snaps = sorted(out_dir.glob("space_v*.yaml"))
        if not snaps:
            return None
        def _ver(p: Path) -> int:
            try:
                return int(p.stem.split("_v")[1])
            except (IndexError, ValueError):
                return -1
        snaps.sort(key=_ver)
        return snaps[-1]


# ------------------------------------------------------------------
def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _apply_one_op(ws: SearchSpace, op: dict, idx: int) -> list[str]:
    """在工作副本上校验并应用单条 op，返回错误列表（空=成功）。"""
    ctx = f"ops[{idx}]"
    kind = str(op.get("op") or "").strip().lower()
    if kind == "set_choices":
        return [f"{ctx} 不支持 set_choices：Optuna 禁止修改分类参数的取值集"
                f"（storage 会拒绝动态 CategoricalDistribution）。"
                f"要聚焦分类参数请用 freeze 固定到某个取值；数值参数可用 narrow/widen"]
    if kind not in VALID_OPS:
        return [f"{ctx}.op 非法：'{op.get('op')}'，合法值：{list(VALID_OPS)}"]
    name = str(op.get("param") or "").strip()
    p = ws._by_name.get(name)
    if p is None:
        return [f"{ctx} 未知参数 '{name}'，现有参数：{sorted(ws._by_name)}"]

    if kind == "release":
        if not p.is_frozen:
            return [f"{ctx} 参数 {name} 未处于冻结状态，无需 release"]
        p.frozen = None
        p.low, p.high = p.env_low, p.env_high
        p.choices = list(p.env_choices) if p.env_choices is not None else p.choices
        return []

    if kind == "freeze":
        if p.is_frozen:
            return [f"{ctx} 参数 {name} 已冻结为 {p.frozen}，如需改值请先 release"]
        if "value" not in op:
            return [f"{ctx} freeze 必须带 value 字段"]
        v = op["value"]
        if not p.domain_contains(v):
            return [f"{ctx} 冻结值 {v!r} 不在 {name} 当前定义域 {p.domain_text()} 内"]
        p.frozen = p.choices[p.choices.index(v)] if p.kind == "choice" else (
            int(v) if p.kind == "int" else float(v))
        return []

    # narrow / widen（数值参数）
    if p.kind not in ("float", "int"):
        return [f"{ctx} {kind} 只适用于数值参数，{name} 是 choice；"
                f"分类参数请用 freeze（固定到某个取值）"]
    low, high = _num(op.get("low")), _num(op.get("high"))
    if low is None or high is None:
        return [f"{ctx} {kind} 需要数值 low 与 high"]
    if p.kind == "int" and not (float(low).is_integer() and float(high).is_integer()):
        return [f"{ctx} {name} 是 int 参数，low/high 必须是整数"]
    if not (low < high):
        return [f"{ctx} 要求 low < high，实际 [{low}, {high}]"]
    if p.log and low <= 0:
        return [f"{ctx} {name} 使用 log 尺度，low 必须 > 0"]

    if kind == "narrow":
        if low < p.low - _EPS or high > p.high + _EPS:
            return [f"{ctx} narrow 必须 ⊆ 当前范围 [{p.low}, {p.high}]，"
                    f"收到 [{low}, {high}]（想放宽请用 widen）"]
        p.low = int(low) if p.kind == "int" else low
        p.high = int(high) if p.kind == "int" else high
    else:  # widen
        if low < p.env_low - _EPS or high > p.env_high + _EPS:
            return [f"{ctx} widen 不得超过初始 envelope [{p.env_low}, {p.env_high}]，"
                    f"收到 [{low}, {high}]——agent 只能聚焦或还原，不能发明新空间"]
        p.low = int(low) if p.kind == "int" else low
        p.high = int(high) if p.kind == "int" else high
    return []
