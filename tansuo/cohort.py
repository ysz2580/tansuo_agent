"""实验记录分区管理（cohort）：记录永不删除 + 三指纹自动分区。

背景：调参记录（optuna study db / journal / 空间快照 / 报告）过去混在同一个
data_dir 里，`--fresh` 一删全没；用户改了训练代码后新旧结果还会混进同一个
study 做对比分析——不可比的数据互相污染。同一份代码跑不同数据集同理：
结果范围天然不同，也不可混在一起。

本模块的分区（cohort）模型：
- 每个分区是 `data_dir/runs/<NNNN>-<YYYYMMDD-HHMMSS>/` 下的独立目录，自带
  meta.yaml / db / journal.jsonl / space_v*.yaml / reports/，互不干扰；
- **三指纹**决定续跑还是新开：
  * objective_hash = 主指标 name:direction + data_fraction——"目标语义"。
    它变了意味着新旧结果根本不可比，而且 Optuna 加载既有 study 时会**静默
    丢弃**请求的 direction（create_study load_if_exists 只按库里的方向走），
    混跑会让排序/剪枝/报告全部静默反向——所以 objective 不符时显式续跑被硬拒绝；
  * code_hash = 训练代码内容（入口脚本/entry 模块/fingerprint_paths 附加文件）。
    仅它变化时允许显式续跑旧分区（带警告），自动模式则新开分区；
  * data_hash = 数据集身份（experiment.dataset 显式声明；未声明时取命令行
    剔除解释器/脚本/-m 模块后的参数）。语义与 code_hash 同级：不同数据集的
    结果不可直接比较，混跑会污染搜索历史，但不会静默反转排序——所以显式
    续跑仅警告放行，自动模式新开分区；数据集改回旧值会恢复对应旧分区；
- `apply_cohort` 只改写 settings 的 data_dir 与 storage.url（绝对路径），
  journal/快照/报告/study 全链路自动落入分区——下游零改动；
- 旧版扁平布局的文件在 run 启动时被**搬**进 `runs/0000-legacy/`（只搬不删，
  逐文件容错）；任何代码路径都不删除用户记录。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

COHORT_DIR = "runs"
LEGACY_ID = "0000-legacy"
NAME_RE = re.compile(r"^(\d{4})-(\d{8})-(\d{6})$")
MAX_HASH_FILE_BYTES = 5 * 1024 * 1024   # 超过 5MB 的文件不参与指纹（记录 skip）


class CohortError(ValueError):
    """分区相关错误（未知分区、目标语义冲突等），信息面向人类可读。"""


@dataclass
class Fingerprint:
    objective_hash: str          # 12 位 hex：主指标 name:direction + data_fraction
    code_hash: str               # 12 位 hex：训练代码内容（或命令串兜底）
    data_hash: str               # 12 位 hex：数据集身份（声明值或命令行参数）
    objective_inputs: dict       # 参与 objective 指纹的字段（审计/展示用）
    code_inputs: dict            # {"mode": files|command-string, "files": [...], ...}
    data_inputs: dict            # {"mode": declared|command-args|untracked, "values": [...]}
    reliable: bool               # False = 没定位到代码文件，指纹只覆盖命令字符串
    data_reliable: bool          # False = 数据集身份不可见（python 模式且未声明）


@dataclass
class Cohort:
    id: str
    path: Path
    meta: dict = field(default_factory=dict)
    virtual: bool = False        # 尚未物理迁移的扁平旧布局（只读视图）
    incomplete: bool = False     # 目录存在但 meta.yaml 缺失/损坏


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------

def abs_data_dir(settings, base_dir: str | Path | None = None) -> Path:
    """settings.data_dir 的绝对路径。相对路径按 base_dir（缺省 CWD）解析——
    CLI 与 Web 进程 CWD 可能不同，调用方必须显式给出基准。"""
    p = Path(settings.data_dir)
    if not p.is_absolute() and base_dir is not None:
        p = Path(base_dir) / p
    return p.resolve()


def root_data_dir(settings, base_dir: str | Path | None = None) -> Path:
    """分区机制的"根" data_dir。apply_cohort 会把 settings.data_dir 改写为
    分区目录，若之后再次调用 resolve_for_run/migrate_legacy（Web 这类长驻进程
    复用同一 settings 对象），必须仍按根目录扫描——否则会误扫分区内部、
    甚至把新分区嵌套建进旧分区。apply_cohort 首次改写时记下原值。"""
    prev = getattr(settings, "_cohort_root", None)
    if prev:
        return Path(prev)
    return abs_data_dir(settings, base_dir)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    """文件内容 sha256；过大/不可读返回 None（调用方记录 skip）。"""
    try:
        if path.stat().st_size > MAX_HASH_FILE_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _url_scheme_and_path(url: str) -> tuple[str, str]:
    for scheme in ("sqlite:///", "journal://"):
        if url.startswith(scheme):
            return scheme, url[len(scheme):]
    raise CohortError(f"storage.url 无法解析：{url}")


def storage_db_path(settings) -> Path | None:
    """settings.storage.url 指向的文件路径（相对路径按 CWD 解析）。"""
    try:
        _, rel = _url_scheme_and_path(settings.storage.url)
    except CohortError:
        return None
    return Path(rel)


def _url_basename(url: str) -> str:
    _, rel = _url_scheme_and_path(url)
    return Path(rel).name


# ----------------------------------------------------------------------
# 双指纹
# ----------------------------------------------------------------------

def _resolve_module_file(module: str) -> Path | None:
    """importlib.util.find_spec 定位模块源文件（失败返回 None）。"""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError, ModuleNotFoundError):
        return None
    if spec is not None and spec.origin and spec.origin != "built-in":
        p = Path(spec.origin)
        if p.exists():
            return p
    return None


def _collect_code_files(settings, base_dir: Path) -> tuple[list[Path], list[str], list[str], str]:
    """定位参与代码指纹的文件。返回 (files, missing, skipped_note, mode)。"""
    files: list[Path] = []
    missing: list[str] = []
    adapter = settings.adapter

    if adapter.mode == "python":
        module = adapter.entry.split(":", 1)[0]
        p = _resolve_module_file(module)
        if p is not None:
            files.append(p)
        else:
            missing.append(adapter.entry)
    else:
        cmd = list(adapter.command)
        for i, tok in enumerate(cmd):
            if tok == "-m" and i + 1 < len(cmd):
                p = _resolve_module_file(cmd[i + 1])
                if p is not None:
                    files.append(p)
                else:
                    missing.append(f"-m {cmd[i + 1]}")
            elif tok.endswith(".py"):
                p = (base_dir / tok) if not Path(tok).is_absolute() else Path(tok)
                if p.exists():
                    files.append(p.resolve())
                else:
                    missing.append(tok)

    # 附加路径（experiment.fingerprint_paths）：模型代码与入口脚本分离时的逃生门
    for extra in getattr(settings, "fingerprint_paths", []) or []:
        p = Path(extra)
        if not p.is_absolute():
            p = base_dir / p
        if p.is_file():
            files.append(p.resolve())
        elif p.is_dir():
            files.extend(sorted(f.resolve() for f in p.rglob("*.py") if f.is_file()))
        else:
            missing.append(str(extra))

    mode = "files" if files else "command-string"
    return files, missing, [], mode


def _collect_data_signal(settings) -> tuple[str, list[str]]:
    """数据集指纹的取值来源。返回 (mode, values)：

    - declared：experiment.dataset 显式声明（首选）——按声明字符串原样哈希，
      不读文件系统（数据集动辄 GB 级，名称/路径足以区分身份）；
    - command-args：未声明时，命令行剔除解释器、.py 脚本 token、-m 及其模块
      后剩余的参数（覆盖 `--data X` 这类参数驱动的数据集切换）；
    - untracked：python 函数模式且未声明——数据集不可见，建议显式声明。
    """
    declared = [str(v).strip() for v in (getattr(settings, "dataset", None) or [])]
    declared = [v for v in declared if v]
    if declared:
        return "declared", declared
    adapter = settings.adapter
    if adapter.mode == "python":
        return "untracked", []
    cmd = list(adapter.command)
    args: list[str] = []
    skip_next = False
    for i, tok in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if i == 0:               # 解释器（python 等）
            continue
        if tok == "-m":          # -m 模块：模块名与 -m 本身都不是数据集参数
            skip_next = True
            continue
        if tok.endswith(".py"):  # 脚本 token（存在与否都按脚本排除）
            continue
        args.append(tok)
    return "command-args", args


def _fmt_data_values(values: list[str] | None) -> str:
    values = values or []
    return " ".join(values) if values else "（无）"


def code_fingerprint(settings, base_dir: str | Path | None = None) -> Fingerprint:
    """计算三指纹。base_dir：相对脚本路径的解析基准（CLI 传 CWD、Web 传项目根）。"""
    base = Path(base_dir).resolve() if base_dir else Path.cwd()
    primary = settings.metrics.primary
    objective_inputs = {
        "primary": f"{primary.name}:{primary.direction}",
        "data_fraction": settings.budget.data_fraction,
    }
    objective_hash = _sha256_text(json.dumps(objective_inputs, sort_keys=True,
                                             ensure_ascii=False))[:12]

    files, missing, _, mode = _collect_code_files(settings, base)
    file_entries = []
    skipped = []
    for f in sorted(set(files)):
        digest = _sha256_file(f)
        try:
            display = f.relative_to(base).as_posix()
        except ValueError:
            display = f.as_posix()
        if digest is None:
            skipped.append(display)
        else:
            file_entries.append({"path": display, "sha256": digest})
    if file_entries:
        payload = "\n".join(f"{e['path']}:{e['sha256']}" for e in file_entries)
        code_hash = _sha256_text(payload)[:12]
        reliable = True
    else:
        # 兜底：只哈希命令/entry 字符串——代码内容变化不可见，指纹不可靠
        adapter = settings.adapter
        payload = (" ".join(adapter.command) if adapter.mode == "subprocess"
                   else adapter.entry)
        code_hash = _sha256_text(payload)[:12]
        mode = "command-string"
        reliable = False
    code_inputs = {"mode": mode, "files": file_entries,
                   "missing": missing, "skipped": skipped}

    # 数据集指纹：只哈希值列表（不含 mode）——声明与命令行推导出相同值时不分裂
    data_mode, data_values = _collect_data_signal(settings)
    data_hash = _sha256_text(json.dumps(data_values, ensure_ascii=False))[:12]
    data_inputs = {"mode": data_mode, "values": data_values}
    data_reliable = data_mode != "untracked"

    return Fingerprint(objective_hash, code_hash, data_hash, objective_inputs,
                       code_inputs, data_inputs, reliable, data_reliable)


# ----------------------------------------------------------------------
# 分区读写
# ----------------------------------------------------------------------

def _runs_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / COHORT_DIR


def _read_meta(cohort_dir: Path) -> tuple[dict, bool]:
    """读 meta.yaml；缺失/损坏返回 ({}, incomplete=True)。"""
    p = cohort_dir / "meta.yaml"
    if not p.exists():
        return {}, True
    try:
        meta = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(meta, dict):
            return {}, True
        return meta, False
    except yaml.YAMLError:
        return {}, True


def _write_meta(cohort_dir: Path, meta: dict) -> None:
    (cohort_dir / "meta.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _scan_physical(data_dir: str | Path) -> list[Cohort]:
    runs = _runs_dir(data_dir)
    if not runs.exists():
        return []
    out: list[Cohort] = []
    for d in runs.iterdir():
        if not d.is_dir():
            continue
        if d.name != LEGACY_ID and not NAME_RE.match(d.name):
            continue   # 忽略不合规则的目录（手工建的 foo/ 等）
        meta, incomplete = _read_meta(d)
        out.append(Cohort(id=d.name, path=d, meta=meta, incomplete=incomplete))
    out.sort(key=lambda c: c.id)
    return out


def _flat_legacy_items(data_dir: Path, settings) -> list[Path]:
    """仍散落在 data_dir 根的旧布局文件（不含 setup_journal.jsonl）。"""
    items: list[Path] = []
    journal = data_dir / "journal.jsonl"
    if journal.exists():
        items.append(journal)
    items.extend(sorted(data_dir.glob("space_v*.yaml")))
    reports = data_dir / "reports"
    if reports.is_dir() and any(reports.iterdir()):
        items.append(reports)
    db = storage_db_path(settings)
    if db is not None:
        db_abs = db if db.is_absolute() else (data_dir / db)
        if db_abs.exists():
            items.append(db_abs)
    return items


def list_cohorts(data_dir: str | Path, settings=None) -> list[Cohort]:
    """按时间序返回所有分区。settings 给出时，未迁移的扁平旧布局会以
    virtual=True 的 0000-legacy 条目出现（只读路径不做物理迁移）。"""
    cohorts = _scan_physical(data_dir)
    if settings is not None and not any(c.id == LEGACY_ID for c in cohorts):
        data_dir_p = Path(data_dir)
        if _flat_legacy_items(data_dir_p, settings):
            db = storage_db_path(settings)
            meta = {"id": LEGACY_ID, "fingerprint": "legacy",
                    "created_at": "未知（旧布局迁移前）",
                    "note": "历史扁平布局记录（下次 run 时自动迁入 0000-legacy）"}
            if db is not None:
                meta["db_name"] = Path(db).name
            cohorts.insert(0, Cohort(id=LEGACY_ID, path=data_dir_p,
                                     meta=meta, virtual=True))
    return cohorts


def load_cohort(data_dir: str | Path, cohort_id: str, settings=None) -> Cohort:
    for c in list_cohorts(data_dir, settings):
        if c.id == cohort_id:
            return c
    raise CohortError(f"找不到分区：{cohort_id}（`python cli.py runs` 查看全部可用分区）")


def collect_env_audit() -> dict:
    """采集运行环境审计信息（python/optuna/torch/GPU/机器），写入分区 meta。

    用途：同一个分区可能跨天、跨机器、跨依赖升级续跑，环境信息让事后复盘
    能对上"这批试验是在什么环境下跑出来的"。全部字段尽力而为——任何依赖
    缺失或探测异常只留 None/空值，绝不影响分区创建与续跑。
    """
    import os
    import platform
    audit = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
        "optuna": None,
        "torch": None,
        "cuda_available": False,
        "gpus": [],
    }
    try:
        import optuna
        audit["optuna"] = str(optuna.__version__)   # 强制纯 str（yaml 拒绝 str 子类）
    except Exception:
        pass
    # torch 体积大、导入慢：先 find_spec 探测，未安装则完全不导入
    try:
        has_torch = importlib.util.find_spec("torch") is not None
    except Exception:
        has_torch = False
    if has_torch:
        try:
            import torch
            audit["torch"] = str(torch.__version__)
            try:
                audit["cuda_available"] = bool(torch.cuda.is_available())
                if audit["cuda_available"]:
                    audit["gpus"] = [str(torch.cuda.get_device_name(i))
                                     for i in range(torch.cuda.device_count())]
            except Exception:
                pass   # CUDA 探测失败不影响版本记录
        except Exception:
            pass
    return audit


def update_cohort_env(cohort: Cohort) -> None:
    """续跑时把当前运行环境记到 meta['environment_last']。

    创建时的环境在 meta['environment']；分区活得久，依赖/机器可能变，
    最近一次运行的环境同样值得留痕。虚拟/不完整分区没有可写 meta，跳过。
    """
    if cohort.virtual or cohort.incomplete or not cohort.meta:
        return
    meta = dict(cohort.meta)
    meta["environment_last"] = collect_env_audit()
    _write_meta(cohort.path, meta)
    cohort.meta = meta


def create_cohort(data_dir: str | Path, fp: Fingerprint, settings,
                  note: str | None = None) -> Cohort:
    """新建分区目录并写 meta.yaml。目录名冲突（并发/同秒）时 seq 递增重试。"""
    runs = _runs_dir(data_dir)
    runs.mkdir(parents=True, exist_ok=True)
    while True:
        seq = 0
        for d in runs.iterdir():
            m = NAME_RE.match(d.name) if d.is_dir() else None
            if m:
                seq = max(seq, int(m.group(1)))
        name = f"{seq + 1:04d}-{time.strftime('%Y%m%d-%H%M%S')}"
        path = runs / name
        try:
            path.mkdir()
            break
        except FileExistsError:
            continue   # 并发冲突：重扫 seq 再来
    primary = settings.metrics.primary
    meta = {
        "id": name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "objective_hash": fp.objective_hash,
        "code_hash": fp.code_hash,
        "data_hash": fp.data_hash,
        "objective_inputs": fp.objective_inputs,
        "code_inputs": fp.code_inputs,
        "data_inputs": fp.data_inputs,
        "note": note or "",
        "primary_metric": {"name": primary.name, "direction": primary.direction},
        "settings_digest": {"data_fraction": settings.budget.data_fraction,
                            "adapter_mode": settings.adapter.mode},
        "db_name": _url_basename(settings.storage.url),
        "environment": collect_env_audit(),
    }
    _write_meta(path, meta)
    return Cohort(id=name, path=path, meta=meta)


# ----------------------------------------------------------------------
# 续跑决策
# ----------------------------------------------------------------------

def _objective_diff_reason(old_inputs: dict, new_inputs: dict) -> str:
    parts = []
    if old_inputs.get("primary") != new_inputs.get("primary"):
        parts.append(f"主指标 {old_inputs.get('primary')} → {new_inputs.get('primary')}")
    if old_inputs.get("data_fraction") != new_inputs.get("data_fraction"):
        parts.append(f"data_fraction {old_inputs.get('data_fraction')} → "
                     f"{new_inputs.get('data_fraction')}")
    return "；".join(parts) or "目标定义变化"


def resolve_for_run(settings, *, force_new: bool = False, cohort_id: str | None = None,
                    note: str | None = None, base_dir: str | Path | None = None,
                    log=print) -> tuple[Cohort, dict]:
    """决定本次 run 用哪个分区。返回 (cohort, info)，info 含 action/reason/匹配情况。

    - 显式 cohort_id：objective 不符 → CohortError 硬拒绝（Optuna 会静默沿用库里
      的方向，混跑必污染）；data/code 不符 → 警告但允许（不同数据集/代码的结果
      不可直接比较，但数据本身有效，跨数据集热启动等属合理的自主决定）；
    - force_new：无条件新开；
    - 自动：续跑最新的"三指纹均匹配"分区（代码/数据集改回旧版→恢复旧分区）；
      否则新开。老分区无 data_hash（数据集分区引入前创建）视为不匹配。
    """
    if force_new and cohort_id:
        raise CohortError("--new/--fresh（新开分区）与 --cohort（指定分区）不能同时使用")
    data_dir = root_data_dir(settings, base_dir)
    fp = code_fingerprint(settings, base_dir)

    if cohort_id:
        cohort = load_cohort(data_dir, cohort_id)
        if cohort.virtual or cohort.incomplete:
            raise CohortError(f"分区 {cohort_id} 无有效 meta 记录，无法安全续跑"
                              f"（请先 `python cli.py run` 让旧记录完成迁移）")
        obj_match = cohort.meta.get("objective_hash") == fp.objective_hash
        code_match = cohort.meta.get("code_hash") == fp.code_hash
        data_match = cohort.meta.get("data_hash") == fp.data_hash
        if not obj_match:
            old = cohort.meta.get("objective_inputs") or {}
            raise CohortError(
                f"拒绝续跑分区 {cohort_id}：优化目标已变化（{_objective_diff_reason(old, fp.objective_inputs)}）。\n"
                f"Optuna 加载既有 study 时会静默沿用库内方向，混跑会让排序/剪枝/报告全部失真。\n"
                f"请直接 `python cli.py run`（自动新开分区），旧分区记录会完整保留。")
        if not data_match:
            if cohort.meta.get("data_hash"):
                old_vals = (cohort.meta.get("data_inputs") or {}).get("values") or []
                log(f"警告：分区 {cohort_id} 的数据集指纹与当前不一致"
                    f"（{_fmt_data_values(old_vals)} → {_fmt_data_values(fp.data_inputs.get('values'))}），"
                    f"按你的指定继续。不同数据集的结果不可直接比较，混跑会污染搜索历史。")
            else:
                log(f"警告：分区 {cohort_id} 创建于数据集分区引入前，无数据集指纹记录，"
                    f"无法核验数据集一致性，按你的指定继续。")
        if not code_match:
            log(f"警告：分区 {cohort_id} 的训练代码指纹与当前不一致"
                f"（{cohort.meta.get('code_hash')} → {fp.code_hash}），按你的指定继续。")
        return cohort, {"action": "explicit", "reason": f"指定续跑分区 {cohort_id}",
                        "objective_match": True, "code_match": code_match,
                        "data_match": data_match}

    if force_new:
        cohort = create_cohort(data_dir, fp, settings, note)
        return cohort, {"action": "created", "reason": "按要求新开分区",
                        "objective_match": False, "code_match": False,
                        "data_match": False}

    cohorts = [c for c in list_cohorts(data_dir) if not c.virtual and not c.incomplete]
    for c in reversed(cohorts):   # 最新优先：代码/数据集改回旧版时恢复对应旧分区
        if (c.meta.get("objective_hash") == fp.objective_hash
                and c.meta.get("code_hash") == fp.code_hash
                and c.meta.get("data_hash") == fp.data_hash):
            return c, {"action": "continue", "reason": "指纹一致，续跑既有分区",
                       "objective_match": True, "code_match": True, "data_match": True}

    # 需要新开分区：说明原因（按实际变化项，可同时并报）
    real = [c for c in cohorts if c.meta.get("objective_hash") or c.meta.get("code_hash")]
    if not real:
        reason = "首次运行，创建分区"
    else:
        latest = real[-1]
        reasons = []
        if latest.meta.get("objective_hash") != fp.objective_hash:
            reasons.append("优化目标变化：" + _objective_diff_reason(
                latest.meta.get("objective_inputs") or {}, fp.objective_inputs))
        if latest.meta.get("data_hash") != fp.data_hash:
            if not latest.meta.get("data_hash"):
                reasons.append("旧分区无数据集指纹记录（数据集分区为新版本引入，"
                               "无法确认数据集一致")
            else:
                old_vals = (latest.meta.get("data_inputs") or {}).get("values") or []
                reasons.append("数据集变化："
                               f"{_fmt_data_values(old_vals)} → "
                               f"{_fmt_data_values(fp.data_inputs.get('values'))}")
        if latest.meta.get("code_hash") != fp.code_hash:
            reasons.append(f"训练代码指纹变化：{latest.meta.get('code_hash')} → "
                           f"{fp.code_hash}")
        reason = "；".join(reasons) + "（旧分区记录保留，可随时查看）"
    cohort = create_cohort(data_dir, fp, settings, note)
    return cohort, {"action": "created", "reason": reason,
                    "objective_match": False, "code_match": False, "data_match": False}


# ----------------------------------------------------------------------
# 应用分区 / 迁移
# ----------------------------------------------------------------------

def apply_cohort(settings, cohort: Cohort) -> None:
    """把 settings 的 data_dir/storage.url 指向分区（绝对路径）。

    这是整个分区机制的枢纽：journal 路径、空间快照、报告输出、study 加载
    全部以这两个字段为准，改写后下游自动分区化。
    """
    if not getattr(settings, "_cohort_root", None):
        # 从分区路径反推根目录（物理分区 = <root>/runs/<id>；虚拟 legacy = root 本身），
        # 不依赖 settings.data_dir 的相对路径解析，Web/CLI 任意 CWD 下都一致。
        root = (cohort.path.parent.parent
                if cohort.path.parent.name == COHORT_DIR else cohort.path)
        try:
            settings._cohort_root = str(root)
        except AttributeError:   # 极端情况下 settings 不允许加属性：降级不记录
            pass
    settings.data_dir = str(cohort.path)
    if cohort.meta.get("storage_url"):   # 外部 db 的 legacy 分区：原地引用
        settings.storage.url = cohort.meta["storage_url"]
        return
    scheme, rel = _url_scheme_and_path(settings.storage.url)
    base = Path(rel)
    new_path = (cohort.path / base.name).resolve()
    settings.storage.url = scheme + new_path.as_posix()


def migrate_legacy(settings, base_dir: str | Path | None = None, log=print) -> dict:
    """自愈式清扫：把 data_dir 根的旧布局文件搬进 runs/0000-legacy/。

    只搬不删；逐文件容错（Windows 下 Web 连接池持有 sqlite 句柄时 PermissionError
    → 警告并保留原地，虚拟 legacy 仍可见）；每次 run 都补扫残留，中途崩溃可恢复。
    """
    if getattr(settings, "_cohort_root", None):
        # settings 已被 apply_cohort 指向分区：此时 storage.url 指向分区内的 db，
        # 继续扫描会把分区自己的文件误当旧布局搬走。迁移必须发生在 resolve/apply 之前。
        return {"moved": [], "skipped": [], "external_db": None}
    data_dir = root_data_dir(settings, base_dir)
    items = _flat_legacy_items(data_dir, settings) if data_dir.exists() else []
    db = storage_db_path(settings)
    db_abs = None
    if db is not None:
        db_abs = db.resolve() if db.is_absolute() else (data_dir / db).resolve()
    external_db = db_abs is not None and db_abs.exists() and data_dir not in db_abs.parents

    if not items and not external_db:
        return {"moved": [], "skipped": [], "external_db": None}

    legacy_dir = _runs_dir(data_dir) / LEGACY_ID
    legacy_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    skipped: list[str] = []
    for item in items:
        # data_dir 之外的 db 不搬（只记录引用），其余就地迁入
        if external_db and db_abs is not None and item.resolve() == db_abs:
            continue
        dest = legacy_dir / item.name
        try:
            if item.is_dir():
                if dest.exists():
                    for child in item.iterdir():
                        shutil.move(str(child), str(dest / child.name))
                    item.rmdir()
                else:
                    shutil.move(str(item), str(dest))
            else:
                shutil.move(str(item), str(dest))
            moved.append(item.name)
        except (PermissionError, OSError) as e:
            log(f"警告：{item.name} 暂无法迁移（{e}）。请停止 web 服务后下次运行会自动重试；"
                f"文件保留原地，不会丢失。")
            skipped.append(item.name)

    meta, incomplete = _read_meta(legacy_dir)
    if incomplete or not meta:
        meta = {"id": LEGACY_ID, "fingerprint": "legacy",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "旧布局历史记录（自动迁移，只搬不删）"}
    if db_abs is not None:
        meta["db_name"] = db_abs.name
    if external_db and db_abs is not None:
        scheme, _ = _url_scheme_and_path(settings.storage.url)
        meta["storage_url"] = scheme + db_abs.as_posix()
        log(f"提示：storage.url 指向 data_dir 之外的数据库（{db_abs}），"
            f"不搬迁，legacy 分区直接引用原位置。")
    _write_meta(legacy_dir, meta)
    return {"moved": moved, "skipped": skipped,
            "external_db": str(db_abs) if external_db else None}


# ----------------------------------------------------------------------
# 分区统计（runs 列表用；原生 sqlite3 只读连接，不建连接池）
# ----------------------------------------------------------------------

def cohort_db_file(cohort: Cohort) -> Path | None:
    if cohort.meta.get("storage_url"):
        try:
            _, rel = _url_scheme_and_path(cohort.meta["storage_url"])
        except CohortError:
            return None
        return Path(rel)
    db_name = cohort.meta.get("db_name")
    return (cohort.path / db_name) if db_name else None


def cohort_stats(cohort: Cohort) -> dict:
    """只读统计：完成试验数与最优值。db 缺失→0；被占用→locked 降级。"""
    out = {"completed": 0, "best": None, "locked": False}
    db = cohort_db_file(cohort)
    if db is None or not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        out["locked"] = True
        return out
    try:
        n = con.execute("SELECT COUNT(*) FROM trials WHERE state = 'COMPLETE'").fetchone()[0]
        out["completed"] = int(n or 0)
        rows = con.execute(
            "SELECT v.value FROM trial_values v JOIN trials t ON v.trial_id = t.trial_id "
            "WHERE t.state = 'COMPLETE' AND v.objective = 0").fetchall()
        if rows:
            vals = [float(r[0]) for r in rows]
            direction = (cohort.meta.get("primary_metric") or {}).get("direction", "maximize")
            out["best"] = min(vals) if direction == "minimize" else max(vals)
    except sqlite3.OperationalError:
        out["locked"] = True
    finally:
        con.close()
    return out
