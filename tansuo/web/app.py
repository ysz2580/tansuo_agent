"""tansuo Web 后端：FastAPI app。

三类接口：
1. 只读查询（summary/trials/curves/space/agent 事件/报告）——直接加载 SQLite study 与 journal；
2. 运行驱动（run start/stop/status/log）——子进程拉起 `python cli.py run`，见 run_manager.py；
3. API 配置切换（config/agent get/probe/save）——复用 probe_endpoint 探测、最小化写回 settings.yaml；
   通知配置（config/notify get/save/test）同范式，见 tansuo/notify.py。

路径来自环境变量 TANSUO_SETTINGS / TANSUO_SPACE（由 `cli.py web` 注入，取绝对路径），
缺省回退到 demo 配置——这样 `uvicorn tansuo.web.app:app` 从项目根直接起也能用。
"""
from __future__ import annotations

import dataclasses
import os
import re
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..analysis import learning_curves, summarize
from ..cohort import (CohortError, abs_data_dir, apply_cohort, code_fingerprint,
                      cohort_stats, list_cohorts, load_cohort, resolve_for_run)
from ..compare import CompareError, compare_cohorts
from ..config import ConfigError, load_settings
from ..journal import TRIAL_END, Journal
from ..space import SearchSpace, SpaceError
from ..study import create_or_load_study, dispose_study
from .project_store import ProjectStore
from .run_manager import RunManager
from .setup_manager import SetupManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 环境变量在 `cli.py web` 导入本模块前注入（STAR #010）——模块级捕获作 bootstrap/兜底。
_ENV_SETTINGS = os.environ.get("TANSUO_SETTINGS")
_ENV_SPACE = os.environ.get("TANSUO_SPACE")

app = FastAPI(title="tansuo_agent Web", description="智能调参 agent 可视化后端")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

PROJECTS = ProjectStore(PROJECT_ROOT,
                        store_path=os.environ.get("TANSUO_PROJECT_STORE"))
PROJECTS.bootstrap_from_env(_ENV_SETTINGS, _ENV_SPACE)
RUN = RunManager(PROJECT_ROOT)
SETUP = SetupManager(PROJECT_ROOT)


def _active_paths() -> tuple[str, str, Path]:
    """当前激活项目的 (settings_path, space_path, project_dir)。

    ProjectStore 激活项优先（且 settings 实际存在）；否则回退环境变量 + PROJECT_ROOT
    （保持 STAR #010 的历史语义：`cli.py web --settings` 直接起也照旧工作）。
    project_dir 是 abs_data_dir/code_fingerprint 的 base_dir，也是子进程 cwd。
    """
    p = PROJECTS.get_active()
    if p and p.get("settings_path") and Path(p["settings_path"]).exists():
        space = p.get("space_path") or ""
        return p["settings_path"], space, Path(p["dir"])
    return (
        _ENV_SETTINGS or str(PROJECT_ROOT / "demo" / "configs" / "settings.yaml"),
        _ENV_SPACE or str(PROJECT_ROOT / "demo" / "configs" / "search_space.yaml"),
        PROJECT_ROOT,
    )


# ------------------------------------------------------------------
# 加载工具（与 cli.py::_make_runtime 同构；只读用途，逐请求加载避免 SQLite 锁竞争）
# ------------------------------------------------------------------

def _load_space_with_snapshots(space_yaml: Path, data_dir: Path) -> SearchSpace:
    """优先恢复最新空间快照（agent 的编辑状态不丢），否则用初始空间。"""
    snaps: list[tuple[int, Path]] = []
    if data_dir.exists():
        for p in data_dir.glob("space_v*.yaml"):
            m = re.fullmatch(r"space_v(\d+)\.yaml", p.name)
            if m:
                snaps.append((int(m.group(1)), p))
    if snaps:
        snaps.sort()
        return SearchSpace.from_yaml(snaps[-1][1])
    return SearchSpace.from_yaml(space_yaml)


def _load_for(cohort_id: str | None = None):
    """settings/space/study/journal/cohort 五件套（只读）。

    cohort_id 指定 → 该分区；缺省 → 最新分区；无任何分区 → 扁平布局（新装行为不变）。
    GET 路径不做物理迁移——未迁移的旧布局以虚拟 legacy 分区呈现。
    """
    settings_path, space_path, project_dir = _active_paths()
    settings = load_settings(settings_path)
    cohort = None
    root = abs_data_dir(settings, project_dir)
    if cohort_id:
        cohort = load_cohort(root, cohort_id, settings=settings)
        apply_cohort(settings, cohort)
    else:
        cohorts = list_cohorts(root, settings=settings)
        if cohorts:
            cohort = cohorts[-1]
            apply_cohort(settings, cohort)
    data_dir = Path(settings.data_dir)
    space = _load_space_with_snapshots(Path(space_path), data_dir)
    study = create_or_load_study(settings)
    journal = Journal(data_dir / "journal.jsonl")
    return settings, space, study, journal, cohort


def _safe_load(cohort_id: str | None = None):
    try:
        return _load_for(cohort_id)
    except CohortError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ConfigError, SpaceError) as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    except sqlite3.OperationalError as e:
        raise HTTPException(status_code=503,
                            detail=f"数据库正被运行中的任务写入，稍后再试（{e}）")


# ------------------------------------------------------------------
# 只读查询
# ------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/summary")
def summary(cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    s = summarize(study, settings, top_k=8)
    s["experiment"] = settings.experiment_name
    s["budget_total"] = settings.budget.total_trials
    s["space_version"] = space.version
    s["workers"] = settings.budget.workers
    s["watch"] = [{"name": m.name, "direction": m.direction}
                  for m in settings.metrics.watch]
    s["cohort"] = coh.id if coh else None
    # 三指纹变化提示（仅对最新实体分区有意义：下次运行将自动新开分区）
    s["fingerprint_changed"] = False
    if coh is not None and not coh.virtual and coh.meta.get("code_hash"):
        try:
            settings_path, _, project_dir = _active_paths()
            root = abs_data_dir(load_settings(settings_path), project_dir)
            latest = [c for c in list_cohorts(root) if not c.virtual]
            if latest and latest[-1].id == coh.id:
                fp = code_fingerprint(settings, project_dir)
                s["fingerprint_changed"] = (
                    fp.code_hash != coh.meta.get("code_hash")
                    or fp.objective_hash != coh.meta.get("objective_hash")
                    or fp.data_hash != coh.meta.get("data_hash"))
        except (ConfigError, CohortError):
            pass
    # ETA：最近 ≤10 次已完结试验平均耗时 × 剩余预算 ÷ 并发数（无样本返回 null）
    from optuna.trial import TrialState
    finished = len(study.get_trials(
        deepcopy=False,
        states=(TrialState.COMPLETE, TrialState.PRUNED, TrialState.FAIL)))
    budget_left = max(0, settings.budget.total_trials - finished)
    ends = [e for e in journal.load_events()
            if e.get("kind") == TRIAL_END
            and isinstance(e.get("duration_s"), (int, float))]
    recent = [float(e["duration_s"]) for e in ends[-10:]]
    s["eta_s"] = (round(sum(recent) / len(recent) * budget_left
                  / max(1, settings.budget.workers))
                  if recent and budget_left > 0 else None)
    return s


@app.get("/api/trials")
def trials(cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    fail_reasons = {e.get("trial"): e.get("reason")
                    for e in journal.load_events() if e.get("kind") == "trial_fail"}
    rows = []
    for t in study.get_trials(deepcopy=False):
        duration = None
        if t.datetime_start and t.datetime_complete:
            duration = round((t.datetime_complete - t.datetime_start).total_seconds(), 1)
        rows.append({
            "number": t.number,
            "state": t.state.name,
            "value": t.value,
            "params": dict(t.params),
            "attrs": {k: v for k, v in t.user_attrs.items() if k != "curve"},
            "duration_s": duration,
            "fail_reason": fail_reasons.get(t.number),
        })
    return {"trials": rows,
            "primary": settings.metrics.primary.name,
            "direction": settings.metrics.primary.direction}


@app.get("/api/trials/{number}/curve")
def trial_curve(number: int, cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    curves = learning_curves(study, trial_ids=[number])
    if not curves:
        raise HTTPException(status_code=404,
                            detail=f"trial#{number} 不存在或未完成（无学习曲线）")
    return {"primary": settings.metrics.primary.name,
            "watch": [m.name for m in settings.metrics.watch],
            "curve": curves[0]}


@app.get("/api/curves")
def curves_default(cohort: str | None = Query(default=None)):
    """默认曲线：top-3 + 最近 2 次完成试验（与 agent 工具 get_learning_curves 一致）。"""
    settings, space, study, journal, coh = _safe_load(cohort)
    return {"primary": settings.metrics.primary.name,
            "watch": [m.name for m in settings.metrics.watch],
            "curves": learning_curves(study)}


@app.get("/api/space")
def space(cohort: str | None = Query(default=None)):
    settings, sp, study, journal, coh = _safe_load(cohort)
    return {"version": sp.version,
            "params": sp.to_dict()["params"],
            "free_params": sp.free_param_count(),
            "patches": journal.patches()}


@app.get("/api/agent/events")
def agent_events(cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    return {"events": journal.agent_events(),
            "tokens": journal.agent_token_summary()}


@app.get("/api/report")
def report(cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    reports_dir = Path(settings.data_dir) / "reports"
    md = reports_dir / "report.md"
    best = reports_dir / "best.yaml"
    if not md.exists():
        return {"exists": False, "content": None, "best": None}
    return {"exists": True,
            "updated": time_iso(md.stat().st_mtime),
            "content": md.read_text(encoding="utf-8"),
            "best": best.read_text(encoding="utf-8") if best.exists() else None}


@app.post("/api/report/generate")
def report_generate(cohort: str | None = Query(default=None)):
    settings, space, study, journal, coh = _safe_load(cohort)
    from ..report import generate_report
    cohort_info = None
    if coh is not None and not coh.virtual:
        cohort_info = {"id": coh.id,
                       "fingerprint": coh.meta.get("code_hash", ""),
                       "note": coh.meta.get("note", "")}
    try:
        report_path, best_path = generate_report(settings, study, space, journal,
                                                 cohort_info=cohort_info)
    except Exception as e:   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"报告生成失败：{e}")
    return {"report": str(report_path), "best": str(best_path)}


def time_iso(ts: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(ts))


@app.get("/api/runs")
def runs_list():
    """记录分区列表 + 当前双指纹 + 各分区与当前指纹的可比性。"""
    settings_path, _, project_dir = _active_paths()
    try:
        settings = load_settings(settings_path)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    root = abs_data_dir(settings, project_dir)
    fp = code_fingerprint(settings, project_dir)
    from ..agent.prompt_store import current_version
    prompt_version_now = current_version(settings)
    items = []
    for c in list_cohorts(root, settings=settings):
        meta = c.meta or {}
        st = cohort_stats(c)
        if not (meta.get("objective_hash") or meta.get("code_hash")):
            comparable = "legacy"          # 历史记录（无指纹）
        elif meta.get("objective_hash") != fp.objective_hash:
            comparable = "objective-changed"   # 目标语义已变，不可直接比较
        else:
            code_diff = meta.get("code_hash") != fp.code_hash
            data_diff = meta.get("data_hash") != fp.data_hash
            if code_diff and data_diff:
                comparable = "code-data-changed"
            elif code_diff:
                comparable = "code-changed"    # 目标一致、训练代码已变
            elif data_diff:
                comparable = "data-changed"    # 目标/代码一致、数据集已变（或无数据集指纹）
            else:
                comparable = "match"
        items.append({
            "id": c.id,
            "created_at": meta.get("created_at"),
            "note": meta.get("note") or "",
            "objective_hash": meta.get("objective_hash"),
            "code_hash": meta.get("code_hash"),
            "data_hash": meta.get("data_hash"),
            "prompt_version": meta.get("prompt_version", 0),
            "prompt_changed": meta.get("prompt_version", 0) != prompt_version_now,
            "primary_metric": meta.get("primary_metric"),
            "completed": st["completed"],
            "best": st["best"],
            "locked": st["locked"],
            "virtual": c.virtual,
            "incomplete": c.incomplete,
            "comparable": comparable,
        })
    return {"runs": items,
            "current": {"objective_hash": fp.objective_hash,
                        "code_hash": fp.code_hash,
                        "data_hash": fp.data_hash,
                        "reliable": fp.reliable,
                        "data_reliable": fp.data_reliable},
            "default": items[-1]["id"] if items else None}


@app.get("/api/runs/compare")
def runs_compare(cohorts: str | None = Query(
        default=None,
        description="参与对比的分区 ID（逗号分隔）；缺省 = 与当前目标指纹相同的全部分区")):
    """跨分区对比：优化目标指纹相同的分区并排比最优值 / top-k / 最优试验学习曲线。"""
    settings_path, _, project_dir = _active_paths()
    try:
        settings = load_settings(settings_path)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    ids = [s.strip() for s in cohorts.split(",") if s.strip()] if cohorts else None
    try:
        return compare_cohorts(abs_data_dir(settings, project_dir), ids, settings,
                               base_dir=project_dir)
    except CompareError as e:      # 目标不一致等不可比情形（先于 CohortError 捕获）
        raise HTTPException(status_code=400, detail=str(e))
    except CohortError as e:       # 分区不存在 / 无分区可比
        raise HTTPException(status_code=404, detail=str(e))


# ------------------------------------------------------------------
# 运行驱动（子进程）
# ------------------------------------------------------------------

class RunStartBody(BaseModel):
    trials: int | None = None
    wake_every: int | None = None
    no_agent: bool = False
    fresh: bool = Field(default=False, description="旧字段别名：等价 new_cohort，不再删除记录")
    new_cohort: bool = Field(default=False, description="强制新开记录分区（不删除历史）")
    note: str | None = Field(default=None, description="分区备注（写入 meta.yaml）")
    workers: int | None = Field(default=None, ge=1, le=32,
                                description="并行试验数（缺省取 settings budget.workers）")
    max_duration_h: float | None = Field(default=None, gt=0,
                                         description="时间预算（小时），到点优雅收尾")


@app.get("/api/run/status")
def run_status():
    return RUN.status()


@app.get("/api/run/log")
def run_log(tail: int = Query(default=200, ge=1, le=5000)):
    st = RUN.status()
    st["text"] = RUN.log_tail(tail)
    return st


def _orphan_cleanup_for(cohort, project_dir: Path) -> list[int]:
    """清理指定分区的孤儿 RUNNING 试验；cohort=None → 扁平布局根目录。"""
    try:
        s = load_settings(_active_paths()[0])
        if cohort is not None:
            apply_cohort(s, cohort)
        journal = Journal(Path(s.data_dir) / "journal.jsonl")
        return _mark_orphaned_running_as_failed(s, journal, project_dir)
    except (ConfigError, CohortError, sqlite3.Error):
        return []


@app.post("/api/run/start")
def run_start(body: RunStartBody):
    # 顺序：先解析目标分区 → 清理目标分区（及上次运行分区）的孤儿试验 →
    # 在**目标分区内**统计已完结数做「本次新增 N → 总预算」换算 → 显式 --cohort 启动 CLI。
    busy = _busy_reason()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    settings_path, space_path, project_dir = _active_paths()
    try:
        settings0 = load_settings(settings_path)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    root = abs_data_dir(settings0, project_dir)
    try:
        target, info = resolve_for_run(settings0,
                                       force_new=body.new_cohort or body.fresh,
                                       note=body.note, base_dir=project_dir)
    except CohortError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if RUN.last_cohort and RUN.last_cohort != target.id:
        try:
            prev = load_cohort(root, RUN.last_cohort, settings=settings0)
            if not prev.virtual:
                _orphan_cleanup_for(prev, project_dir)
        except CohortError:
            pass
    elif RUN.last_cohort is None:
        _orphan_cleanup_for(None, project_dir)   # 升级前的扁平布局可能有历史孤儿
    _orphan_cleanup_for(target, project_dir)

    trials_arg = body.trials
    if trials_arg is not None:
        # cli --trials 语义是「总预算」，界面语义是「本次新增 N 次试验」——换算成总量。
        # 必须在目标分区内统计：新开分区时完结数为 0，不能把旧分区计数带进来。
        from optuna.trial import TrialState
        if info["action"] == "created":
            finished = 0
        else:
            study_t = None
            try:
                s_t = load_settings(settings_path)
                apply_cohort(s_t, target)
                study_t = create_or_load_study(s_t)
                finished = len(study_t.get_trials(
                    deepcopy=False,
                    states=(TrialState.COMPLETE, TrialState.PRUNED, TrialState.FAIL)))
            except sqlite3.OperationalError as e:
                raise HTTPException(status_code=503,
                                    detail=f"数据库正被占用，稍后再试（{e}）")
            finally:
                if study_t is not None:
                    dispose_study(study_t)
        trials_arg = finished + trials_arg
    try:
        return RUN.start(target.path, settings_path=settings_path,
                         space_path=space_path, project_dir=project_dir,
                         trials=trials_arg,
                         wake_every=body.wake_every, no_agent=body.no_agent,
                         workers=body.workers, max_duration_h=body.max_duration_h,
                         cohort=target.id, note=body.note)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/run/stop")
def run_stop():
    try:
        st = RUN.stop()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # 杀进程树不会触发 orchestrator 的 FINISH 逻辑，进行中的试验会永远停在 RUNNING。
    # 把它如实标记为 FAIL（reason 写进 journal），避免仪表盘"进行中"计数永久失真。
    # 清理对象是本次运行所在的分区（RUN.last_cohort），而不是"最新分区"。
    _, _, project_dir = _active_paths()
    marked: list[int] = []
    if RUN.last_cohort:
        try:
            s0 = load_settings(_active_paths()[0])
            c = load_cohort(abs_data_dir(s0, project_dir), RUN.last_cohort,
                            settings=s0)
            marked = _orphan_cleanup_for(c, project_dir)
        except (ConfigError, CohortError):
            pass
    else:
        marked = _orphan_cleanup_for(None, project_dir)
    if marked:
        st["marked_failed"] = marked
    return st


def _mark_orphaned_running_as_failed(settings, journal, project_dir: Path) -> list[int]:
    """把被强制停止遗留的 RUNNING 试验改为 FAIL。返回处理的试验编号。"""
    url = settings.storage.url
    if not url.startswith("sqlite:///"):
        return []   # journal:// 降级存储不支持直接改状态，跳过
    db_rel = url[len("sqlite:///"):]
    db_path = Path(db_rel)
    if not db_path.is_absolute():
        db_path = Path(project_dir) / db_rel
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT trial_id, number FROM trials WHERE state = 'RUNNING'").fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        con.execute(
            f"UPDATE trials SET state = 'FAIL' WHERE trial_id IN ({placeholders})", ids)
        con.commit()
    finally:
        con.close()
    numbers = [r[1] for r in rows]
    for n in numbers:
        journal.append("trial_fail", trial=n, reason="运行被手动停止",
                       hint="该试验在搜索被停止时正在进行，已标记为失败",
                       detail="(无 stderr/stdout：进程树被强制终止)", source="search")
    return numbers


# ------------------------------------------------------------------
# API 配置切换（model / base_url / auth_token）
# ------------------------------------------------------------------

class AgentConfigBody(BaseModel):
    model: str | None = None
    base_url: str | None = None
    auth_token: str | None = None


def _agent_env_refs() -> dict:
    """检查 settings.yaml 原文中 base_url/auth_token 是否为 ${ENV:...} 引用。"""
    try:
        text = Path(_active_paths()[0]).read_text(encoding="utf-8")
    except OSError:
        return {"base_url": False, "auth_token": False}
    return {
        "base_url": bool(re.search(r"^\s*base_url:\s*\$\{ENV:", text, re.M)),
        "auth_token": bool(re.search(r"^\s*auth_token:\s*\$\{ENV:", text, re.M)),
    }


def _effective_cfg(body: AgentConfigBody):
    """请求字段 + 当前 settings + 环境变量兜底 → 用于探测的临时 AgentCfg。"""
    settings = load_settings(_active_paths()[0])
    cfg = settings.agent
    token = ((body.auth_token or "").strip() or cfg.auth_token
             or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
             or os.environ.get("ANTHROPIC_API_KEY", ""))
    return dataclasses.replace(
        cfg,
        model=(body.model or "").strip() or cfg.model,
        base_url=(body.base_url or "").strip() or cfg.base_url,
        auth_token=token,
    )


@app.get("/api/config/agent")
def agent_config_get():
    try:
        settings = load_settings(_active_paths()[0])
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    cfg = settings.agent
    from ..agent.api_setup import _mask
    refs = _agent_env_refs()
    if cfg.auth_token:
        source = ("settings.yaml（${ENV:...} 引用环境变量）" if refs["auth_token"]
                  else "settings.yaml（agent.auth_token 明文）")
    elif os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        source = "环境变量 ANTHROPIC_AUTH_TOKEN"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        source = "环境变量 ANTHROPIC_API_KEY"
    else:
        source = "未设置"
    token = (cfg.auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
             or os.environ.get("ANTHROPIC_API_KEY", ""))
    return {"model": cfg.model,
            "base_url": cfg.base_url,
            "enabled": cfg.enabled,
            "auth_token_masked": _mask(token),
            "auth_token_source": source,
            "env_refs": refs}


@app.post("/api/config/agent/probe")
def agent_config_probe(body: AgentConfigBody):
    from ..agent.client import make_client, probe_endpoint
    try:
        cfg = _effective_cfg(body)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    result = probe_endpoint(make_client(cfg), cfg.model)
    return {"model": cfg.model, "base_url": cfg.base_url, **result}


@app.post("/api/config/agent/save")
def agent_config_save(body: AgentConfigBody):
    """先探测（用生效配置），通过后把「显式给出的字段」最小化写回 settings.yaml。

    留空的字段一律不动——尤其 auth_token 留空时保持 ${ENV:...} 引用，
    绝不把环境变量里的 token 物化进配置文件。
    """
    from ..agent.api_setup import write_back_agent
    from ..agent.client import make_client, probe_endpoint
    try:
        cfg = _effective_cfg(body)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    result = probe_endpoint(make_client(cfg), cfg.model)
    if not result["ok"]:
        raise HTTPException(
            status_code=400,
            detail=f"探测失败于 [{result['stage']}]：{result['detail']}（已拒绝写盘）")
    fields = {"model": (body.model or "").strip() or None,
              "base_url": (body.base_url or "").strip() or None,
              "auth_token": (body.auth_token or "").strip() or None}
    wb = write_back_agent(_active_paths()[0], **fields)
    if not wb["changed"] and wb["errors"]:
        raise HTTPException(status_code=500, detail="；".join(wb["errors"]))
    return {"probe": result, "write_back": wb,
            "warnings": ["auth_token 已以明文写入 settings.yaml，注意不要误提交到公开仓库"]
            if fields["auth_token"] else []}


# ------------------------------------------------------------------
# 通知配置（notify：webhook_url / format / events / enabled）
# ------------------------------------------------------------------

class NotifySaveBody(BaseModel):
    webhook_url: str | None = None
    format: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


def _notify_env_ref() -> bool:
    """检查 settings.yaml 原文中 webhook_url 是否为 ${ENV:...} 引用。"""
    try:
        text = Path(_active_paths()[0]).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"^\s*webhook_url:\s*\$\{ENV:", text, re.M))


@app.get("/api/config/notify")
def notify_config_get():
    try:
        settings = load_settings(_active_paths()[0])
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    cfg = settings.notify
    from ..agent.api_setup import _mask
    source = ("settings.yaml（${ENV:...} 引用环境变量）" if _notify_env_ref()
              else ("settings.yaml（notify.webhook_url 明文）" if cfg.webhook_url
                    else "未设置"))
    return {"enabled": cfg.enabled,
            "format": cfg.format,
            "events": cfg.events,
            "webhook_url_masked": _mask(cfg.webhook_url),
            "webhook_url_source": source}


@app.post("/api/config/notify/save")
def notify_config_save(body: NotifySaveBody):
    """把「显式给出的字段」最小化写回 settings.yaml 的 notify 块。

    webhook_url 留空时保持 ${ENV:...} 引用，绝不把环境变量里的地址物化进
    配置文件；明文写入则告警。
    """
    from ..notify import VALID_EVENTS, VALID_FORMATS, write_back_notify
    fmt = (body.format or "").strip().lower() or None
    if fmt is not None and fmt not in VALID_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"format 必须是 {'/'.join(VALID_FORMATS)} 之一，实际：'{fmt}'")
    if body.events is not None:
        for v in body.events:
            if v not in VALID_EVENTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"events 含非法值 '{v}'，必须是 {'/'.join(VALID_EVENTS)} 之一")
    wb = write_back_notify(_active_paths()[0],
                           webhook_url=(body.webhook_url or "").strip() or None,
                           fmt=fmt, events=body.events, enabled=body.enabled)
    if not wb["changed"] and wb["errors"]:
        raise HTTPException(status_code=500, detail="；".join(wb["errors"]))
    return {"write_back": wb,
            "warnings": ["webhook_url 已以明文写入 settings.yaml，注意不要误提交到公开仓库"]
            if (body.webhook_url or "").strip() else []}


@app.post("/api/config/notify/test")
def notify_config_test():
    """用当前生效配置发一条测试通知，返回 {ok, detail}。"""
    from ..notify import build_payload, send_webhook
    try:
        settings = load_settings(_active_paths()[0])
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    cfg = settings.notify
    if not cfg.webhook_url:
        raise HTTPException(status_code=400, detail="webhook_url 为空，请先配置后再测试")
    text = (f"【tansuo 通知测试】{settings.experiment_name}\n"
            f"这是一条测试消息：webhook 配置（{cfg.format}）连通正常。")
    return send_webhook(cfg.webhook_url, build_payload(cfg.format, text))


# ------------------------------------------------------------------
# 提示词管理（prompts.yaml：覆盖 / 版本 / 历史回滚；前后端同步）
# ------------------------------------------------------------------

class PromptPreviewBody(BaseModel):
    which: str
    text: str = ""


class PromptSaveBody(BaseModel):
    which: str
    text: str = ""
    rationale: str


def _preview_context(which: str) -> dict:
    """为预览构建尽力而为的上下文；运行时才有的量用样例值，取不到的留空
    （渲染后原样保留 {{var}}，missing_vars 会列出，便于编辑时排查）。"""
    from ..agent.prompts import build_context_tuning_system
    settings_path, space_path, project_dir = _active_paths()
    try:
        settings = load_settings(settings_path)
    except ConfigError:
        return {}
    root = abs_data_dir(settings, project_dir)
    space = _load_space_with_snapshots(Path(space_path), root)
    if which == "tuning_system":
        return build_context_tuning_system(settings, space)   # 全静态，可完整渲染
    if which == "tuning_wake_brief":
        total = settings.budget.total_trials   # 运行时量用样例值（第 1 轮、未开始）
        return {"round_no": 1, "max_wake_rounds": settings.agent.max_wake_rounds,
                "finished_count": 0, "total": total, "budget_left": total,
                "space_version": space.version,
                # 护栏信号预览样例：运行时由代码按试验状态生成，无信号时为空串
                "wake_signals": "\n⚠ （示例信号）系统警报：连续 3 次试验全部超时。"
                                "实际运行时此项由代码自动填充，无信号时为空。"}
    return {}   # setup_system：Web 侧无训练脚本上下文，占位符原样保留


@app.get("/api/config/prompts")
def prompts_get():
    """三条提示词的当前覆盖、出厂默认、生效模板、可用变量与历史。"""
    from ..agent.prompt_store import load_doc
    from ..agent.prompts import DEFAULT_PROMPTS, PROMPT_NAMES, PROMPT_VARS
    try:
        settings = load_settings(_active_paths()[0])
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    doc = load_doc(settings)
    overrides = doc["prompts"]
    prompts = []
    for name in PROMPT_NAMES:
        override = str(overrides.get(name, ""))
        default = DEFAULT_PROMPTS[name]
        prompts.append({"name": name, "override": override, "default": default,
                        "effective": override or default, "vars": PROMPT_VARS[name]})
    return {"version": doc["version"], "prompts": prompts, "history": doc["history"]}


@app.post("/api/config/prompts/preview")
def prompts_preview(body: PromptPreviewBody):
    """不落盘地渲染一段提示词，供编辑时预览；列出未被填充的占位符。"""
    from ..agent.prompt_store import MAX_PROMPT_LEN
    from ..agent.prompts import PROMPT_NAMES, render_prompt
    if body.which not in PROMPT_NAMES:
        raise HTTPException(status_code=400, detail=f"未知提示词 {body.which!r}")
    if len(body.text) > MAX_PROMPT_LEN:
        raise HTTPException(status_code=400, detail=f"提示词过长（>{MAX_PROMPT_LEN} 字符）")
    ctx = _preview_context(body.which)
    overrides = {body.which: body.text} if body.text else {}
    rendered = render_prompt(body.which, ctx, overrides)
    missing = sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", rendered)))
    return {"rendered": rendered, "missing_vars": missing}


@app.post("/api/config/prompts/save")
def prompts_save(body: PromptSaveBody):
    """保存一条覆盖并记历史（text 为空=恢复出厂）；校验失败转 400。"""
    from ..agent.prompt_store import PromptStoreError, save_override
    try:
        settings = load_settings(_active_paths()[0])
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"settings 加载失败：{e}")
    try:
        return save_override(settings, body.which, body.text, body.rationale, source="web")
    except PromptStoreError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ------------------------------------------------------------------
# 项目管理（注册 / 激活 / 删除 / 新建脚手架）+ 服务端目录浏览
# ------------------------------------------------------------------

class ProjectCreateBody(BaseModel):
    name: str | None = Field(default=None, description="项目名；缺省用目录名")
    dir: str = Field(description="项目目录（含训练代码与数据集）")
    train_script: str | None = Field(default=None, description="主训练脚本路径（可空）")


# 目录浏览排除的系统/保留目录（Windows 为主），避免噪音与越权窥探
_EXCLUDE_DIRS = {"Windows", "Program Files", "Program Files (x86)", "ProgramData",
                 "$Recycle.Bin", "System Volume Information", "Recovery", "PerfLogs",
                 "MSOCache", "Intel", "AMD"}


def _busy_reason() -> str | None:
    """有任务在跑时返回原因（切换项目 / 启动搜索 / 启动 setup 前调用）；空闲返回 None。

    setup 与 run 硬互斥：两者都会写 settings/空间/分区状态，并发会互相踩踏。
    """
    if RUN.running:
        return f"有搜索正在运行（pid={RUN.pid}），请先停止"
    if SETUP.running:
        return f"配置 agent 正在运行（pid={SETUP.pid}），请等待结束或先停止"
    return None


def _scaffold_project(dir_path: Path, train_script: str | None) -> None:
    """在项目目录内创建 `.tansuo/` 工作子目录 + settings/space 模板。

    相对路径（data_dir / storage.url / adapter.command）一律相对**项目目录**解析，
    与 `_active_paths()` 返回的 project_dir（= base_dir = 子进程 cwd）一致。
    """
    from ..wizard import SETTINGS_TEMPLATE, SPACE_TEMPLATE
    tansuo_dir = dir_path / ".tansuo"
    tansuo_dir.mkdir(parents=True, exist_ok=True)
    text = SETTINGS_TEMPLATE
    text = text.replace("data_dir: data", "data_dir: .tansuo/data")
    text = text.replace("sqlite:///data/tansuo.db",
                        "sqlite:///.tansuo/data/tansuo.db")
    if train_script:
        try:
            rel = Path(train_script).resolve().relative_to(dir_path)
        except ValueError:
            rel = Path(Path(train_script).name)   # 不在项目内 → 退化为文件名
        text = text.replace('command: ["python", "path/to/your_train.py"]',
                            f'command: ["python", "{rel.as_posix()}"]')
    (tansuo_dir / "settings.yaml").write_text(text, encoding="utf-8")
    (tansuo_dir / "search_space.yaml").write_text(SPACE_TEMPLATE, encoding="utf-8")


def _has_subdirs(d: Path) -> bool:
    try:
        return any(c.is_dir() for c in d.iterdir())
    except OSError:
        return False


def _browse_dir(p: Path) -> dict:
    dirs = []
    try:
        for d in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if not d.is_dir():
                continue
            if d.name.startswith(".") or d.name.startswith("$") \
                    or d.name in _EXCLUDE_DIRS:
                continue
            dirs.append({"name": d.name, "path": str(d.resolve()),
                         "has_children": _has_subdirs(d)})
    except OSError:
        pass
    parent = str(p.parent) if p.parent != p else (
        "" if sys.platform == "win32" else None)
    return {"path": str(p), "parent": parent, "dirs": dirs}


@app.get("/api/fs/browse")
def fs_browse(path: str = Query(default="")):
    """服务端目录浏览（浏览器无法枚举服务器文件夹）。只列目录、排除系统/隐藏目录。

    path 为空 → Windows 列盘符 / 其他平台列 home；盘符根的 parent 为 ""（回盘符列表）。
    """
    if not path:
        if sys.platform == "win32":
            import string
            dirs = [{"name": f"{letter}:", "path": f"{letter}:\\",
                     "has_children": True}
                    for letter in string.ascii_uppercase
                    if Path(f"{letter}:\\").exists()]
            return {"path": "", "parent": None, "dirs": dirs}
        return _browse_dir(Path.home())
    if ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="路径不得包含 ..")
    p = Path(path).resolve()
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"不是目录：{p}")
    return _browse_dir(p)


@app.get("/api/fs/files")
def fs_files(path: str = Query(...), ext: str = Query(default=".py")):
    """列目录下指定扩展名的文件（默认 .py，供选择训练脚本）。"""
    p = Path(path).resolve()
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"不是目录：{p}")
    try:
        files = [f.name for f in sorted(p.iterdir(), key=lambda x: x.name.lower())
                 if f.is_file() and f.suffix == ext]
    except OSError:
        files = []
    return {"path": str(p), "files": files}


@app.get("/api/projects")
def projects_list():
    act = PROJECTS.get_active()
    return {"projects": PROJECTS.list_projects(),
            "active_id": act["id"] if act else None}


@app.get("/api/projects/active")
def project_active():
    act = PROJECTS.get_active()
    if act is None:
        raise HTTPException(status_code=404, detail="无激活项目")
    return act


@app.post("/api/projects")
def project_create(body: ProjectCreateBody):
    """新建/打开项目：目录已含 `.tansuo/settings.yaml` → 直接注册（打开）；
    否则脚手架 `.tansuo/` 模板再注册（新建）。"""
    dir_path = Path(body.dir).resolve()
    if not dir_path.is_dir():
        raise HTTPException(status_code=400,
                            detail=f"项目目录不存在或不是目录：{body.dir}")
    settings_path = dir_path / ".tansuo" / "settings.yaml"
    space_path = dir_path / ".tansuo" / "search_space.yaml"
    scaffolded = False
    if not settings_path.exists():
        _scaffold_project(dir_path, body.train_script)
        scaffolded = True
    try:
        load_settings(settings_path)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"项目 settings.yaml 无效：{e}")
    entry = PROJECTS.register(body.name or dir_path.name, dir_path,
                              settings_path, space_path,
                              train_script=body.train_script or "")
    return {**entry, "scaffolded": scaffolded}


@app.post("/api/projects/{project_id}/activate")
def project_activate(project_id: str):
    busy = _busy_reason()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    try:
        return PROJECTS.activate(project_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/projects/{project_id}")
def project_delete(project_id: str):
    PROJECTS.remove(project_id)   # 仅移除注册，不删文件（数据安全优先）
    return {"ok": True}


# ------------------------------------------------------------------
# 配置 agent（setup）Web 化：子进程驱动 `cli.py setup`，与搜索硬互斥
# ------------------------------------------------------------------

@app.post("/api/projects/{project_id}/setup")
def project_setup(project_id: str):
    """对指定项目跑配置 agent：读训练脚本 → LLM 起草 settings + search_space。

    不要求该项目处于激活态（路径全部显式传给子进程），但要求登记了训练脚本。
    """
    busy = _busy_reason()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    entry = next((p for p in PROJECTS.list_projects() if p["id"] == project_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    if not entry.get("train_script"):
        raise HTTPException(status_code=400,
                            detail="该项目未登记训练脚本，无法自动配置"
                                   "（新建项目时请选择主训练脚本）")
    settings_path = entry["settings_path"]
    space_path = entry.get("space_path") or ""
    project_dir = Path(entry["dir"])
    try:
        settings = load_settings(settings_path)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    data_dir = abs_data_dir(settings, project_dir)
    try:
        return SETUP.start(entry["train_script"], settings_path, space_path,
                           project_dir, data_dir)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/setup/stop")
def setup_stop():
    try:
        return SETUP.stop()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/setup/status")
def setup_status():
    return SETUP.status()


@app.get("/api/setup/log")
def setup_log(tail: int = Query(default=200, ge=1, le=5000)):
    st = SETUP.status()
    st["text"] = SETUP.log_tail(tail)
    return st


def _setup_journal_path() -> Path | None:
    """setup_journal.jsonl 定位：优先本次会话启动时绑定的 data_dir（= journal
    实际写入位置，cmd_setup 开头按启动时的 settings 解析）；不能用 setup 结束后
    的 settings 重新解析——setup agent 会覆写 settings.yaml，data_dir 可能变化。
    服务重启后 SETUP 无绑定，回退按当前激活项目的 settings/project_dir 解析。"""
    if SETUP.data_dir is not None:
        return Path(SETUP.data_dir) / "setup_journal.jsonl"
    settings_path = SETUP.settings_path or _active_paths()[0]
    project_dir = SETUP.project_dir or _active_paths()[2]
    try:
        settings = load_settings(settings_path)
    except ConfigError:
        return None
    return abs_data_dir(settings, project_dir) / "setup_journal.jsonl"


@app.get("/api/setup/events")
def setup_events():
    """setup 会话事件流（journal）。事件 kind 与调参会话同构
    （session_start / agent_* / finish），前端渲染逻辑可复用 AgentPage。"""
    jp = _setup_journal_path()
    if jp is None or not jp.exists():
        return {"events": [],
                "tokens": {"rounds": 0, "input_tokens": 0,
                           "output_tokens": 0, "total_tokens": 0}}
    j = Journal(jp)
    return {"events": j.load_events(), "tokens": j.agent_token_summary()}


# ------------------------------------------------------------------
# 静态前端（生产构建产物存在时托管，单端口部署）
# ------------------------------------------------------------------

_DIST = PROJECT_ROOT / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
