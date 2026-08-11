"""tansuo Web 后端：FastAPI app。

三类接口：
1. 只读查询（summary/trials/curves/space/agent 事件/报告）——直接加载 SQLite study 与 journal；
2. 运行驱动（run start/stop/status/log）——子进程拉起 `python cli.py run`，见 run_manager.py；
3. API 配置切换（config/agent get/probe/save）——复用 probe_endpoint 探测、最小化写回 settings.yaml。

路径来自环境变量 TANSUO_SETTINGS / TANSUO_SPACE（由 `cli.py web` 注入，取绝对路径），
缺省回退到 demo 配置——这样 `uvicorn tansuo.web.app:app` 从项目根直接起也能用。
"""
from __future__ import annotations

import dataclasses
import os
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..analysis import learning_curves, summarize
from ..cohort import (CohortError, abs_data_dir, apply_cohort, code_fingerprint,
                      cohort_stats, list_cohorts, load_cohort, resolve_for_run)
from ..config import ConfigError, load_settings
from ..journal import TRIAL_END, Journal
from ..space import SearchSpace, SpaceError
from ..study import create_or_load_study
from .run_manager import RunManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = os.environ.get(
    "TANSUO_SETTINGS", str(PROJECT_ROOT / "demo" / "configs" / "settings.yaml"))
SPACE_PATH = os.environ.get(
    "TANSUO_SPACE", str(PROJECT_ROOT / "demo" / "configs" / "search_space.yaml"))

app = FastAPI(title="tansuo_agent Web", description="智能调参 agent 可视化后端")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

RUN = RunManager(PROJECT_ROOT, SETTINGS_PATH, SPACE_PATH)


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
    settings = load_settings(SETTINGS_PATH)
    cohort = None
    root = abs_data_dir(settings, PROJECT_ROOT)
    if cohort_id:
        cohort = load_cohort(root, cohort_id, settings=settings)
        apply_cohort(settings, cohort)
    else:
        cohorts = list_cohorts(root, settings=settings)
        if cohorts:
            cohort = cohorts[-1]
            apply_cohort(settings, cohort)
    data_dir = Path(settings.data_dir)
    space = _load_space_with_snapshots(Path(SPACE_PATH), data_dir)
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
    # 代码指纹变化提示（仅对最新实体分区有意义：下次运行将自动新开分区）
    s["code_fingerprint_changed"] = False
    if coh is not None and not coh.virtual and coh.meta.get("code_hash"):
        try:
            root = abs_data_dir(load_settings(SETTINGS_PATH), PROJECT_ROOT)
            latest = [c for c in list_cohorts(root) if not c.virtual]
            if latest and latest[-1].id == coh.id:
                fp = code_fingerprint(settings, PROJECT_ROOT)
                s["code_fingerprint_changed"] = (
                    fp.code_hash != coh.meta.get("code_hash")
                    or fp.objective_hash != coh.meta.get("objective_hash"))
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
    return {"events": journal.agent_events()}


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
    try:
        settings = load_settings(SETTINGS_PATH)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    root = abs_data_dir(settings, PROJECT_ROOT)
    fp = code_fingerprint(settings, PROJECT_ROOT)
    items = []
    for c in list_cohorts(root, settings=settings):
        meta = c.meta or {}
        st = cohort_stats(c)
        if not (meta.get("objective_hash") or meta.get("code_hash")):
            comparable = "legacy"          # 历史记录（无指纹）
        elif meta.get("objective_hash") != fp.objective_hash:
            comparable = "objective-changed"   # 目标语义已变，不可直接比较
        elif meta.get("code_hash") != fp.code_hash:
            comparable = "code-changed"    # 目标一致、训练代码已变
        else:
            comparable = "match"
        items.append({
            "id": c.id,
            "created_at": meta.get("created_at"),
            "note": meta.get("note") or "",
            "objective_hash": meta.get("objective_hash"),
            "code_hash": meta.get("code_hash"),
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
                        "reliable": fp.reliable},
            "default": items[-1]["id"] if items else None}


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


def _dispose_study(study) -> None:
    """释放 sqlite 连接池（Windows 下避免 ~30s 句柄残留）。
    study._storage 是 _CachedStorage 包装层，引擎在 _backend 上。"""
    backend = getattr(getattr(study, "_storage", None), "_backend", None)
    engine = getattr(backend, "engine", None) if backend else None
    if engine is None:
        engine = getattr(getattr(study, "_storage", None), "engine", None)
    if engine is not None:
        try:
            engine.dispose()
        except Exception:   # noqa: BLE001
            pass


def _orphan_cleanup_for(cohort) -> list[int]:
    """清理指定分区的孤儿 RUNNING 试验；cohort=None → 扁平布局根目录。"""
    try:
        s = load_settings(SETTINGS_PATH)
        if cohort is not None:
            apply_cohort(s, cohort)
        journal = Journal(Path(s.data_dir) / "journal.jsonl")
        return _mark_orphaned_running_as_failed(s, journal)
    except (ConfigError, CohortError, sqlite3.Error):
        return []


@app.post("/api/run/start")
def run_start(body: RunStartBody):
    # 顺序：先解析目标分区 → 清理目标分区（及上次运行分区）的孤儿试验 →
    # 在**目标分区内**统计已完结数做「本次新增 N → 总预算」换算 → 显式 --cohort 启动 CLI。
    try:
        settings0 = load_settings(SETTINGS_PATH)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=f"配置加载失败：{e}")
    root = abs_data_dir(settings0, PROJECT_ROOT)
    try:
        target, info = resolve_for_run(settings0,
                                       force_new=body.new_cohort or body.fresh,
                                       note=body.note, base_dir=PROJECT_ROOT)
    except CohortError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if RUN.last_cohort and RUN.last_cohort != target.id:
        try:
            prev = load_cohort(root, RUN.last_cohort, settings=settings0)
            if not prev.virtual:
                _orphan_cleanup_for(prev)
        except CohortError:
            pass
    elif RUN.last_cohort is None:
        _orphan_cleanup_for(None)   # 升级前的扁平布局可能有历史孤儿
    _orphan_cleanup_for(target)

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
                s_t = load_settings(SETTINGS_PATH)
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
                    _dispose_study(study_t)
        trials_arg = finished + trials_arg
    try:
        return RUN.start(target.path, trials=trials_arg,
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
    marked: list[int] = []
    if RUN.last_cohort:
        try:
            s0 = load_settings(SETTINGS_PATH)
            c = load_cohort(abs_data_dir(s0, PROJECT_ROOT), RUN.last_cohort,
                            settings=s0)
            marked = _orphan_cleanup_for(c)
        except (ConfigError, CohortError):
            pass
    else:
        marked = _orphan_cleanup_for(None)
    if marked:
        st["marked_failed"] = marked
    return st


def _mark_orphaned_running_as_failed(settings, journal) -> list[int]:
    """把被强制停止遗留的 RUNNING 试验改为 FAIL。返回处理的试验编号。"""
    url = settings.storage.url
    if not url.startswith("sqlite:///"):
        return []   # journal:// 降级存储不支持直接改状态，跳过
    db_rel = url[len("sqlite:///"):]
    db_path = Path(db_rel)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_rel
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
        text = Path(SETTINGS_PATH).read_text(encoding="utf-8")
    except OSError:
        return {"base_url": False, "auth_token": False}
    return {
        "base_url": bool(re.search(r"^\s*base_url:\s*\$\{ENV:", text, re.M)),
        "auth_token": bool(re.search(r"^\s*auth_token:\s*\$\{ENV:", text, re.M)),
    }


def _effective_cfg(body: AgentConfigBody):
    """请求字段 + 当前 settings + 环境变量兜底 → 用于探测的临时 AgentCfg。"""
    settings = load_settings(SETTINGS_PATH)
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
        settings = load_settings(SETTINGS_PATH)
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
    wb = write_back_agent(SETTINGS_PATH, **fields)
    if not wb["changed"] and wb["errors"]:
        raise HTTPException(status_code=500, detail="；".join(wb["errors"]))
    return {"probe": result, "write_back": wb,
            "warnings": ["auth_token 已以明文写入 settings.yaml，注意不要误提交到公开仓库"]
            if fields["auth_token"] else []}


# ------------------------------------------------------------------
# 静态前端（生产构建产物存在时托管，单端口部署）
# ------------------------------------------------------------------

_DIST = PROJECT_ROOT / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
