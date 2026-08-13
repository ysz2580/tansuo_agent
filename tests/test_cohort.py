"""分区管理测试：三指纹 / 续跑决策 / 旧布局迁移 / 分区化落盘（可直接
`python tests/test_cohort.py` 运行）。

覆盖：
- 三指纹：objective（主指标 name:direction + data_fraction）、code（训练代码内容）
  与 data（数据集身份）各自的敏感项与稳定项；-m 模块/python entry 定位；
  fingerprint_paths 附加路径；命令串兜底（reliable=False）
- 数据集指纹：experiment.dataset 显式声明、命令行参数自动兜底、python 模式
  untracked、数据集切换/改回的续跑决策、无 data_hash 老分区的兼容
- 续跑决策：同指纹续跑、代码变化自动新开、force_new、显式 --cohort 的警告/硬拒绝
  （objective 不符拒绝——Optuna 会静默沿用库内方向）、代码改回恢复旧分区
- 目录/编号：seq 取自目录名、无 meta 目录容忍、非规则目录忽略、冲突重试
- 迁移：扁平旧文件只搬不删进 0000-legacy、幂等、崩溃续扫、外部 db 引用、
  PermissionError 容错
- apply_cohort：data_dir/storage.url 绝对化改写，study 真实落盘到分区内可续接
- 列表/统计：排序、虚拟 legacy、incomplete 标记、缺 db/被占用降级
- E2E-lite：真实子进程跑一轮 → db/journal/快照/报告全在分区内，SESSION_START
  带分区审计字段；改脚本再跑自动落新分区且旧分区原封
- config：experiment.fingerprint_paths 校验
- 环境审计：meta 记录 python/optuna/torch/GPU/机器，torch 缺席容错，
  续跑刷新 environment_last，不完整分区静默跳过
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

import optuna                                                    # noqa: E402
import yaml                                                      # noqa: E402

from tansuo.cohort import (LEGACY_ID, CohortError, apply_cohort,  # noqa: E402
                           code_fingerprint, cohort_stats, collect_env_audit,
                           create_cohort, list_cohorts, load_cohort,
                           migrate_legacy, resolve_for_run, update_cohort_env)
from tansuo.config import ConfigError, load_settings             # noqa: E402
from tansuo.journal import SESSION_START, Journal                # noqa: E402
from tansuo.orchestrator import Orchestrator                     # noqa: E402
from tansuo.runner import TrialRunner                            # noqa: E402
from tansuo.space import SearchSpace                             # noqa: E402
from tansuo.study import create_or_load_study                     # noqa: E402

PASS = 0

CHILD_SIMPLE = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.7}\')\n'

SPACE_DICT = {"params": [
    {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
     "description": "学习率（测试用最小空间）"},
]}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def expect_error(name: str, exc_type, fn, *a, **kw):
    global PASS
    try:
        fn(*a, **kw)
    except exc_type:
        PASS += 1
        print(f"  [ok] {name}（如期报错）")
        return
    raise AssertionError(f"FAIL: {name} 应当抛出 {exc_type.__name__}")


def make_settings(tmp: Path, name: str, *, script_text: str | None = None,
                  direction: str = "maximize", primary: str = "val_acc",
                  data_fraction: float = 0.5, timeout: int = 60, workers: int = 1,
                  fingerprint_paths: list | None = None,
                  dataset: "str | list | None" = None,
                  extra_args: list | None = None,
                  storage_name: str = "t.db", extra_budget: str = ""):
    """在 tmp 下写训练脚本（可选）+ settings.yaml 并加载。"""
    if script_text is not None:
        (tmp / f"{name}_train.py").write_text(script_text, encoding="utf-8")
    script = tmp / f"{name}_train.py"
    exe = Path(sys.executable).as_posix()
    data_dir = tmp / "data" / name
    fp_yaml = ""
    if fingerprint_paths is not None:
        items = ", ".join(str(p) for p in fingerprint_paths)
        fp_yaml = f"\n  fingerprint_paths: [{items}]"
    ds_yaml = ""
    if dataset is not None:
        items = [dataset] if isinstance(dataset, str) else list(dataset)
        ds_yaml = "\n  dataset: [" + ", ".join(json.dumps(v) for v in items) + "]"
    args_yaml = "".join(", " + json.dumps(a) for a in (extra_args or []))
    text = (
        "experiment:\n"
        f"  name: {name}\n"
        f"  data_dir: {data_dir.as_posix()}{fp_yaml}{ds_yaml}\n"
        "metrics:\n"
        f"  primary: {{name: {primary}, direction: {direction}}}\n"
        "adapter:\n"
        "  mode: subprocess\n"
        f'  command: ["{exe}", "{script.as_posix()}"{args_yaml}]\n'
        "  config_via: env\n"
        f"  timeout_s: {timeout}\n"
        f"budget: {{total_trials: 4, wake_every: 2, seed: 1, workers: {workers}, "
        f"data_fraction: {data_fraction}{extra_budget}}}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        f"storage: {{url: sqlite:///{(data_dir / storage_name).as_posix()}}}\n"
    )
    p = tmp / f"{name}_settings.yaml"
    p.write_text(text, encoding="utf-8")
    return load_settings(p)


def dispose(study) -> None:
    """释放 sqlite 连接池，避免 Windows 临时目录清理失败（WinError 32）。
    注意：study._storage 是 _CachedStorage 包装层，引擎在 _backend 上。"""
    storage = getattr(study, "_storage", None)
    backend = getattr(storage, "_backend", storage)
    engine = getattr(backend, "engine", None)
    if engine is not None:
        engine.dispose()


def flat_layout(data_dir: Path, settings, trials: int = 2) -> None:
    """在 data_dir 根伪造一套旧布局（真实 sqlite study + journal + 快照 + 报告）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    study = create_or_load_study(settings)
    for i in range(trials):
        t = study.ask()
        study.tell(t, 0.5 + i * 0.1)
    dispose(study)
    (data_dir / "journal.jsonl").write_text(
        '{"kind": "session_start", "legacy": true}\n', encoding="utf-8")
    (data_dir / "space_v1.yaml").write_text("params: []\n", encoding="utf-8")
    reports = data_dir / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "report.md").write_text("# legacy report\n", encoding="utf-8")


# ======================================================================
# 一、双指纹
# ======================================================================
def test_fingerprint(tmp: Path) -> None:
    print("\n== 双指纹 ==")
    s1 = make_settings(tmp, "fp1", script_text="x = 1\n")
    f1 = code_fingerprint(s1, tmp)
    f1b = code_fingerprint(s1, tmp)
    ok("指纹稳定（同配置两次相同）",
       f1.code_hash == f1b.code_hash and f1.objective_hash == f1b.objective_hash)
    ok("摘要为 12 位十六进制",
       len(f1.code_hash) == 12 and all(c in "0123456789abcdef" for c in f1.code_hash)
       and len(f1.objective_hash) == 12)
    ok("inputs-detail 列出被哈希的文件",
       f1.code_inputs["mode"] == "files"
       and any("fp1_train.py" in e["path"] for e in f1.code_inputs["files"]))
    ok("reliable=True（定位到了脚本文件）", f1.reliable is True)

    # 运行参数不敏感（复用同一 settings/脚本，只改 timeout/workers）
    s1.adapter.timeout_s = 999
    s1.budget.workers = 4
    f_run = code_fingerprint(s1, tmp)
    ok("timeout/workers 变化不改 code_hash", f_run.code_hash == f1.code_hash)
    ok("timeout/workers 变化不改 objective_hash", f_run.objective_hash == f1.objective_hash)

    # objective 三项敏感
    s_dir = make_settings(tmp, "fp_dir", script_text="x = 1\n", direction="minimize",
                          primary="val_loss")
    ok("主指标名/方向变化 → objective_hash 变",
       code_fingerprint(s_dir, tmp).objective_hash != f1.objective_hash)
    s_frac = make_settings(tmp, "fp_frac", script_text="x = 1\n", data_fraction=1.0)
    ok("data_fraction 变化 → objective_hash 变",
       code_fingerprint(s_frac, tmp).objective_hash != f1.objective_hash)

    # 代码 1 字节改动 → code 变、objective 不变
    f_before = code_fingerprint(s1, tmp)
    (tmp / "fp1_train.py").write_text("x = 2\n", encoding="utf-8")
    f2 = code_fingerprint(s1, tmp)
    ok("脚本 1 字节改动 → code_hash 变", f2.code_hash != f_before.code_hash)
    ok("脚本改动不影响 objective_hash", f2.objective_hash == f_before.objective_hash)

    # fingerprint_paths：附加文件敏感、缺失容忍、目录确定性
    extra = tmp / "model_lib.py"
    extra.write_text("layers = 3\n", encoding="utf-8")
    s_fp = make_settings(tmp, "fp_paths", script_text="x = 1\n",
                         fingerprint_paths=["model_lib.py"])
    f_base = code_fingerprint(s_fp, tmp)
    extra.write_text("layers = 4\n", encoding="utf-8")
    ok("fingerprint_paths 附加文件变化 → code_hash 变",
       code_fingerprint(s_fp, tmp).code_hash != f_base.code_hash)
    s_miss = make_settings(tmp, "fp_miss", script_text="x = 1\n",
                           fingerprint_paths=["not_exist_dir"])
    f_miss = code_fingerprint(s_miss, tmp)   # 不得抛错
    ok("fingerprint_paths 缺失路径不报错且被记录",
       "not_exist_dir" in f_miss.code_inputs["missing"])
    pkg = tmp / "mypkg"
    pkg.mkdir()
    (pkg / "b.py").write_text("b = 1\n", encoding="utf-8")
    (pkg / "a.py").write_text("a = 1\n", encoding="utf-8")
    s_dirp = make_settings(tmp, "fp_dirp", script_text="x = 1\n",
                           fingerprint_paths=["mypkg"])
    d1 = code_fingerprint(s_dirp, tmp).code_hash
    d2 = code_fingerprint(s_dirp, tmp).code_hash
    ok("目录指纹确定性（两次相同）", d1 == d2)
    (pkg / "a.py").write_text("a = 2\n", encoding="utf-8")
    ok("目录内子文件变化 → code_hash 变",
       code_fingerprint(s_dirp, tmp).code_hash != d1)

    # 兜底：定位不到代码文件 → command-string 且 reliable=False
    exe = Path(sys.executable).as_posix()
    s_nofile = make_settings(tmp, "fp_nofile", script_text=None)
    # 把 command 改成不含可读 .py 的形式
    s_nofile.adapter.command = [exe, "-c", "print(1)"]
    f_nf = code_fingerprint(s_nofile, tmp)
    ok("无脚本文件 → command-string 兜底且 reliable=False",
       f_nf.code_inputs["mode"] == "command-string" and f_nf.reliable is False)


def test_fingerprint_module_mode(tmp: Path) -> None:
    print("\n== 模块定位（-m / python entry）==")
    mod = tmp / "fake_train_mod.py"
    mod.write_text("VERSION = 1\n", encoding="utf-8")
    sys.path.insert(0, str(tmp))
    try:
        s_m = make_settings(tmp, "fp_m", script_text="x = 1\n")
        exe = Path(sys.executable).as_posix()
        s_m.adapter.command = [exe, "-m", "fake_train_mod"]
        f_m = code_fingerprint(s_m, tmp)
        ok("-m 模块能定位到源文件",
           f_m.code_inputs["mode"] == "files"
           and any("fake_train_mod.py" in e["path"] for e in f_m.code_inputs["files"]))
        mod.write_text("VERSION = 2\n", encoding="utf-8")
        ok("模块内容变化 → code_hash 变",
           code_fingerprint(s_m, tmp).code_hash != f_m.code_hash)

        s_py = make_settings(tmp, "fp_py", script_text=None)
        s_py.adapter.mode = "python"
        s_py.adapter.entry = "fake_train_mod:run"
        f_py1 = code_fingerprint(s_py, tmp)
        ok("python entry 定位到模块文件",
           any("fake_train_mod.py" in e["path"] for e in f_py1.code_inputs["files"]))
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop("fake_train_mod", None)


# ======================================================================
# 一b、数据集指纹（第三维度）
# ======================================================================
def test_dataset_fingerprint(tmp: Path) -> None:
    print("\n== 数据集指纹 ==")
    # 显式声明
    s1 = make_settings(tmp, "ds1", script_text="x = 1\n", dataset="mnist-5k")
    f1 = code_fingerprint(s1, tmp)
    ok("声明数据集 → data_hash 为 12 位 hex 且稳定",
       len(f1.data_hash) == 12 and all(c in "0123456789abcdef" for c in f1.data_hash)
       and code_fingerprint(s1, tmp).data_hash == f1.data_hash)
    ok("data_inputs 记录 mode=declared 与原值",
       f1.data_inputs == {"mode": "declared", "values": ["mnist-5k"]})
    ok("声明模式下 data_reliable=True", f1.data_reliable is True)
    s2 = make_settings(tmp, "ds2", script_text="x = 1\n", dataset="cifar10")
    ok("数据集声明变化 → data_hash 变", code_fingerprint(s2, tmp).data_hash != f1.data_hash)
    s3 = make_settings(tmp, "ds3", script_text="x = 1\n", dataset=["mnist-5k"])
    ok("str 与单元素 list 归一等价（同哈希）",
       code_fingerprint(s3, tmp).data_hash == f1.data_hash)
    s4 = make_settings(tmp, "ds4", script_text="x = 1\n", dataset=["train_a", "val_a"])
    ok("多数据集列表参与指纹",
       code_fingerprint(s4, tmp).data_hash != f1.data_hash
       and code_fingerprint(s4, tmp).data_inputs["values"] == ["train_a", "val_a"])
    s_iso = make_settings(tmp, "ds_iso", script_text="x = 1\n")
    f_iso_before = code_fingerprint(s_iso, tmp)
    s_iso.dataset = ["cifar10"]
    f_iso_after = code_fingerprint(s_iso, tmp)
    ok("数据集声明变化不影响 code/objective 哈希",
       f_iso_after.code_hash == f_iso_before.code_hash
       and f_iso_after.objective_hash == f_iso_before.objective_hash
       and f_iso_after.data_hash != f_iso_before.data_hash)

    # 未声明 → 命令行参数兜底
    s5 = make_settings(tmp, "ds5", script_text="x = 1\n", extra_args=["--data", "A"])
    f5 = code_fingerprint(s5, tmp)
    ok("未声明 → command-args 兜底（剔除解释器与脚本）",
       f5.data_inputs == {"mode": "command-args", "values": ["--data", "A"]}
       and f5.data_reliable is True)
    s6 = make_settings(tmp, "ds6", script_text="x = 1\n", extra_args=["--data", "B"])
    ok("数据集参数切换 → data_hash 变", code_fingerprint(s6, tmp).data_hash != f5.data_hash)
    s7 = make_settings(tmp, "ds7", script_text="x = 1\n")
    s8 = make_settings(tmp, "ds8", script_text="y = 2\n")
    ok("无参数命令行 → data_hash 跨配置恒定",
       code_fingerprint(s7, tmp).data_hash == code_fingerprint(s8, tmp).data_hash
       and code_fingerprint(s7, tmp).data_inputs["mode"] == "command-args")
    s9 = make_settings(tmp, "ds9", script_text="x = 1\n", dataset=["--data", "A"])
    ok("声明与命令行推导出相同值不分裂", code_fingerprint(s9, tmp).data_hash == f5.data_hash)

    # python 模式：未声明不可见，声明后可靠
    s_py = make_settings(tmp, "ds_py", script_text=None)
    s_py.adapter.mode = "python"
    s_py.adapter.entry = "whatever:run"
    f_py = code_fingerprint(s_py, tmp)
    ok("python 模式未声明 → untracked 且 data_reliable=False",
       f_py.data_inputs == {"mode": "untracked", "values": []}
       and f_py.data_reliable is False)
    s_py.dataset = ["myds"]
    ok("python 模式显式声明后 data_reliable=True",
       code_fingerprint(s_py, tmp).data_reliable is True
       and code_fingerprint(s_py, tmp).data_inputs["mode"] == "declared")

    # 运行参数不敏感
    s1.adapter.timeout_s = 999
    s1.budget.workers = 4
    ok("timeout/workers 变化不改 data_hash", code_fingerprint(s1, tmp).data_hash == f1.data_hash)


def test_resolve_dataset(tmp: Path) -> None:
    print("\n== 数据集续跑决策 ==")
    logs: list[str] = []
    s = make_settings(tmp, "drs", script_text="v = 1\n", extra_args=["--data", "A"])
    c1, i1 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("首跑建分区且 meta 含 data_inputs",
       i1["action"] == "created" and c1.meta["data_inputs"]["values"] == ["--data", "A"]
       and c1.meta["data_hash"])
    c2, i2 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("同数据集续跑同一分区", i2["action"] == "continue" and c2.id == c1.id)

    exe, script = s.adapter.command[0], s.adapter.command[1]
    s.adapter.command = [exe, script, "--data", "B"]
    c3, i3 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("数据集变化自动新开分区", i3["action"] == "created" and c3.id != c1.id)
    ok("新开原因说明数据集变化明细",
       "数据集变化" in i3["reason"] and "--data B" in i3["reason"])

    s.adapter.command = [exe, script, "--data", "A"]
    c4, i4 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("数据集改回 → 恢复旧分区", i4["action"] == "continue" and c4.id == c1.id)

    logs.clear()
    c5, i5 = resolve_for_run(s, cohort_id=c3.id, base_dir=tmp, log=logs.append)
    ok("显式续跑数据集不符分区放行（软拦截）",
       i5["action"] == "explicit" and c5.id == c3.id and i5["data_match"] is False)
    ok("给出数据集不一致警告", any("数据集指纹" in m for m in logs))

    # 无数据集指纹的老分区（数据集分区引入前创建）：警告+放行
    from tansuo.cohort import abs_data_dir
    fp_now = code_fingerprint(s, tmp)
    c_old = create_cohort(abs_data_dir(s, tmp), fp_now, s, "old")
    meta = yaml.safe_load((c_old.path / "meta.yaml").read_text(encoding="utf-8"))
    meta.pop("data_hash")
    meta.pop("data_inputs")
    (c_old.path / "meta.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logs.clear()
    c6, i6 = resolve_for_run(s, cohort_id=c_old.id, base_dir=tmp, log=logs.append)
    ok("显式续跑无数据指纹的老分区放行", i6["action"] == "explicit" and c6.id == c_old.id)
    ok("提示无法核验数据集一致性", any("无法核验数据集" in m for m in logs))

    # 升级兼容：所有分区都无 data_hash 时自动新开且原因明确（不回填旧 meta）
    s_up = make_settings(tmp, "drs_up", script_text="v = 1\n")
    c_up, _ = resolve_for_run(s_up, base_dir=tmp, log=logs.append)
    meta_up = yaml.safe_load((c_up.path / "meta.yaml").read_text(encoding="utf-8"))
    meta_up.pop("data_hash")
    meta_up.pop("data_inputs")
    (c_up.path / "meta.yaml").write_text(
        yaml.safe_dump(meta_up, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logs.clear()
    c_up2, i_up2 = resolve_for_run(s_up, base_dir=tmp, log=logs.append)
    ok("升级兼容：老分区无 data_hash → 自动新开分区",
       i_up2["action"] == "created" and c_up2.id != c_up.id)
    ok("原因说明旧分区无数据集指纹记录", "无数据集指纹记录" in i_up2["reason"])


# ======================================================================
# 二、续跑决策
# ======================================================================
def test_resolve(tmp: Path) -> None:
    print("\n== 续跑决策 ==")
    logs: list[str] = []
    s = make_settings(tmp, "rs", script_text="v = 1\n")
    cohort, info = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("首跑创建分区", info["action"] == "created" and cohort.id.startswith("0001-"))
    meta = yaml.safe_load((cohort.path / "meta.yaml").read_text(encoding="utf-8"))
    ok("meta 含双指纹/主指标/时间",
       meta["objective_hash"] and meta["code_hash"]
       and meta["primary_metric"] == {"name": "val_acc", "direction": "maximize"}
       and meta["created_at"])
    ok("分区目录在 data_dir/runs 下",
       cohort.path.parent.name == "runs" and "data" in cohort.path.as_posix())

    cohort2, info2 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("同指纹续跑同一分区", info2["action"] == "continue" and cohort2.id == cohort.id)

    (tmp / "rs_train.py").write_text("v = 2\n", encoding="utf-8")
    cohort3, info3 = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("脚本变化自动新开分区", info3["action"] == "created" and cohort3.id != cohort.id)
    ok("新开原因说明指纹变化", "指纹变化" in info3["reason"])
    ok("旧分区目录原封不动", (cohort.path / "meta.yaml").exists())

    cohort4, info4 = resolve_for_run(s, force_new=True, note="手动新开",
                                     base_dir=tmp, log=logs.append)
    ok("force_new 即使指纹一致也新开", info4["action"] == "created"
       and cohort4.id != cohort3.id)
    ok("中文备注写入 meta",
       yaml.safe_load((cohort4.path / "meta.yaml").read_text(encoding="utf-8"))["note"]
       == "手动新开")

    # 显式续跑：仅 code 不符 → 允许但警告
    c_exp, info_exp = resolve_for_run(s, cohort_id=cohort.id, base_dir=tmp,
                                      log=logs.append)
    ok("显式续跑旧分区（仅代码不符）允许", c_exp.id == cohort.id
       and info_exp["code_match"] is False)
    ok("代码不符时给出警告", any("警告" in ln for ln in logs))

    # objective 不符 → 硬拒绝
    s_dir = make_settings(tmp, "rs_dir", script_text="v = 2\n", direction="minimize",
                          primary="val_loss")
    s_dir.data_dir = s.data_dir   # 指向同一 data_dir 才能看到既有分区
    expect_error("显式续跑遇目标变化 → 拒绝", CohortError,
                 resolve_for_run, s_dir, cohort_id=cohort3.id, base_dir=tmp)

    expect_error("未知分区 id → 拒绝", CohortError,
                 resolve_for_run, s, cohort_id="9999-00000000-000000", base_dir=tmp)
    expect_error("force_new 与显式 cohort 互斥", CohortError,
                 resolve_for_run, s, force_new=True, cohort_id=cohort.id, base_dir=tmp)

    # 代码改回旧版 → 恢复旧分区（不误开新分区）
    (tmp / "rs_train.py").write_text("v = 1\n", encoding="utf-8")
    c_back, info_back = resolve_for_run(s, base_dir=tmp, log=logs.append)
    ok("代码改回 → 恢复匹配的旧分区", c_back.id == cohort.id
       and info_back["action"] == "continue")

    # 目标语义变化 → 自动新开并说明原因
    s_frac = make_settings(tmp, "rs_frac", script_text="v = 1\n", data_fraction=1.0)
    s_frac.data_dir = s.data_dir
    _, info_frac = resolve_for_run(s_frac, base_dir=tmp, log=logs.append)
    ok("data_fraction 变化自动新开且原因说明",
       info_frac["action"] == "created" and "data_fraction" in info_frac["reason"])


def test_ids_and_dirs(tmp: Path) -> None:
    print("\n== 目录与编号 ==")
    s = make_settings(tmp, "ids", script_text="v = 1\n")
    runs = Path(s.data_dir) / "runs"
    (runs / "0003-20260101-000000").mkdir(parents=True)   # 无 meta 的目录
    (runs / "foo").mkdir()                                  # 不合规则目录
    cohort, _ = resolve_for_run(s, base_dir=tmp)
    ok("seq 取目录名最大值（0003 存在 → 新建 0004）", cohort.id.startswith("0004-"))
    import re
    ok("分区名格式 NNNN-YYYYMMDD-HHMMSS",
       re.fullmatch(r"\d{4}-\d{8}-\d{6}", cohort.id) is not None)
    lst = list_cohorts(Path(s.data_dir))
    ok("非规则目录被忽略、无 meta 目录标 incomplete",
       all(c.id != "foo" for c in lst)
       and any(c.id.startswith("0003-") and c.incomplete for c in lst))
    expect_error("无 meta 分区不允许显式续跑", CohortError,
                 resolve_for_run, s, cohort_id=lst[0].id if lst[0].incomplete else "x",
                 base_dir=tmp)


# ======================================================================
# 三、旧布局迁移
# ======================================================================
def test_migration(tmp: Path) -> None:
    print("\n== 旧布局迁移 ==")
    logs: list[str] = []
    s = make_settings(tmp, "mig", script_text="v = 1\n")
    data_dir = Path(s.data_dir)
    flat_layout(data_dir, s, trials=2)

    rep = migrate_legacy(s, base_dir=tmp, log=logs.append)
    legacy = data_dir / "runs" / LEGACY_ID
    ok("journal/快照/报告/db 全部迁入 0000-legacy",
       (legacy / "journal.jsonl").exists() and (legacy / "space_v1.yaml").exists()
       and (legacy / "reports" / "report.md").exists() and (legacy / "t.db").exists())
    ok("根目录清空（journal/快照/报告不再残留）",
       not (data_dir / "journal.jsonl").exists()
       and not list(data_dir.glob("space_v*.yaml"))
       and not (data_dir / "reports").exists())
    ok("迁移返回搬移清单", len(rep["moved"]) >= 4 and not rep["skipped"])
    con = sqlite3.connect(str(legacy / "t.db"))
    n = con.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    con.close()
    ok("sqlite 数据完好（2 次试验仍在）", n == 2)
    meta = yaml.safe_load((legacy / "meta.yaml").read_text(encoding="utf-8"))
    ok("legacy meta 标记 fingerprint=legacy", meta.get("fingerprint") == "legacy")

    rep2 = migrate_legacy(s, base_dir=tmp, log=logs.append)
    ok("二次迁移幂等（无可搬文件）", not rep2["moved"])

    # 崩溃续扫：只留 journal 在根，下次补扫进同一 legacy 目录
    (data_dir / "journal.jsonl").write_text('{"kind": "leftover"}\n', encoding="utf-8")
    migrate_legacy(s, base_dir=tmp, log=logs.append)
    ok("残留文件续扫入同一 legacy 分区",
       (legacy / "journal.jsonl").read_text(encoding="utf-8").count("leftover") == 1)

    # 无扁平文件 → 不建 legacy
    s2 = make_settings(tmp, "mig_clean", script_text="v = 1\n")
    rep3 = migrate_legacy(s2, base_dir=tmp, log=logs.append)
    ok("无旧文件则不创建 legacy 目录",
       not rep3["moved"] and not (Path(s2.data_dir) / "runs" / LEGACY_ID).exists())

    # 外部 db：storage.url 指向 data_dir 之外 → 不搬、meta 引用原位置
    outside = tmp / "outside" / "ext.db"
    outside.parent.mkdir(parents=True, exist_ok=True)
    s3 = make_settings(tmp, "mig_ext", script_text="v = 1\n", storage_name="x.db")
    s3.storage.url = "sqlite:///" + outside.as_posix()
    outside.write_bytes(b"dummy-db")
    (Path(s3.data_dir)).mkdir(parents=True, exist_ok=True)
    (Path(s3.data_dir) / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    rep4 = migrate_legacy(s3, base_dir=tmp, log=logs.append)
    ok("外部 db 不搬迁", outside.exists() and rep4["external_db"])
    meta3 = yaml.safe_load((Path(s3.data_dir) / "runs" / LEGACY_ID / "meta.yaml")
                           .read_text(encoding="utf-8"))
    ok("legacy meta 引用外部 db 原位置",
       meta3.get("storage_url", "").endswith("ext.db"))

    # PermissionError 容错
    s4 = make_settings(tmp, "mig_lock", script_text="v = 1\n")
    flat_layout(Path(s4.data_dir), s4, trials=1)
    import tansuo.cohort as ch
    real_move = ch.shutil.move

    def flaky_move(src, dst, *a, **kw):
        if str(src).endswith("journal.jsonl"):
            raise PermissionError(32, "文件正被另一进程使用")
        return real_move(src, dst, *a, **kw)

    logs4: list[str] = []
    ch.shutil.move = flaky_move
    try:
        rep5 = migrate_legacy(s4, base_dir=tmp, log=logs4.append)
    finally:
        ch.shutil.move = real_move
    ok("PermissionError 被容忍（文件留原地、不抛异常）",
       "journal.jsonl" in rep5["skipped"]
       and (Path(s4.data_dir) / "journal.jsonl").exists())
    ok("锁定时给出中文提示", any("web 服务" in ln for ln in logs4))


def test_virtual_legacy(tmp: Path) -> None:
    print("\n== 虚拟 legacy（未迁移视图）==")
    s = make_settings(tmp, "vl", script_text="v = 1\n")
    data_dir = Path(s.data_dir)
    flat_layout(data_dir, s, trials=1)
    lst = list_cohorts(data_dir, settings=s)
    ok("迁移前可见虚拟 legacy 条目",
       lst and lst[0].id == LEGACY_ID and lst[0].virtual)
    st = cohort_stats(lst[0])
    ok("虚拟 legacy 也能统计（1 次完成试验）", st["completed"] == 1)
    migrate_legacy(s, base_dir=tmp)
    lst2 = list_cohorts(data_dir, settings=s)
    ok("迁移后虚拟条目消失、实体 legacy 出现",
       any(c.id == LEGACY_ID and not c.virtual for c in lst2)
       and all(not c.virtual for c in lst2))


# ======================================================================
# 四、apply_cohort 与分区落盘
# ======================================================================
def test_apply(tmp: Path) -> None:
    print("\n== apply_cohort ==")
    s = make_settings(tmp, "apply", script_text="v = 1\n", storage_name="my.db")
    before_name = s.experiment_name
    cohort, _ = resolve_for_run(s, base_dir=tmp)
    # 用未被 resolve 改写的新 settings 做 apply（模拟 cli 流程）
    s2 = make_settings(tmp, "apply", script_text="v = 1\n", storage_name="my.db")
    apply_cohort(s2, cohort)
    ok("data_dir 指向分区目录", Path(s2.data_dir) == cohort.path)
    ok("storage.url 改为分区内绝对路径（保留原文件名）",
       s2.storage.url.startswith("sqlite:///")
       and s2.storage.url.endswith("my.db")
       and cohort.path.as_posix() in s2.storage.url)
    ok("其他 settings 字段不受影响", s2.experiment_name == before_name)

    # study 真实落盘到分区内且可续接
    study = create_or_load_study(s2)
    t = study.ask()
    study.tell(t, 0.88)
    dispose(study)
    ok("study db 落在分区目录内", (cohort.path / "my.db").exists())
    study2 = create_or_load_study(s2)
    ok("重载 study 续接（试验仍在）", len(study2.trials) == 1)
    dispose(study2)

    # journal:// scheme 保留
    s3 = make_settings(tmp, "apply_j", script_text="v = 1\n")
    s3.storage.url = "journal://" + (Path(s3.data_dir) / "j.log").as_posix()
    apply_cohort(s3, cohort)
    ok("journal:// scheme 与文件名保留",
       s3.storage.url.startswith("journal://") and s3.storage.url.endswith("j.log"))


# ======================================================================
# 五、列表与统计
# ======================================================================
def test_list_and_stats(tmp: Path) -> None:
    print("\n== 列表与统计 ==")
    s = make_settings(tmp, "lst", script_text="v = 1\n")
    c1, _ = resolve_for_run(s, base_dir=tmp)
    apply_cohort(s, c1)
    study = create_or_load_study(s)
    for v in (0.3, 0.9, 0.6):
        t = study.ask()
        study.tell(t, v)
    dispose(study)
    c1.meta = yaml.safe_load((c1.path / "meta.yaml").read_text(encoding="utf-8"))
    st = cohort_stats(c1)
    ok("统计：3 次完成、最优 0.9", st["completed"] == 3 and abs(st["best"] - 0.9) < 1e-9)

    s_min = make_settings(tmp, "lst_min", script_text="v = 1\n", direction="minimize",
                          primary="val_loss")
    c_min = create_cohort(Path(s_min.data_dir), code_fingerprint(s_min, tmp), s_min)
    c_min.meta["primary_metric"] = {"name": "val_loss", "direction": "minimize"}
    apply_cohort(s_min, c_min)
    study_m = optuna.create_study(storage=s_min.storage.url, study_name="tansuo",
                                  direction="minimize")
    for v in (0.3, 0.1, 0.6):
        t = study_m.ask()
        study_m.tell(t, v)
    dispose(study_m)
    st_min = cohort_stats(c_min)
    ok("minimize 方向按最小值取 best", abs(st_min["best"] - 0.1) < 1e-9)

    ok("缺 db → completed=0 不报错",
       cohort_stats(create_cohort(Path(s.data_dir), code_fingerprint(s, tmp), s))
       ["completed"] == 0)

    # db 路径指向一个目录 → 打开失败降级为 locked（不抛异常）
    bad = create_cohort(Path(s.data_dir), code_fingerprint(s, tmp), s)
    (bad.path / "t.db").mkdir()
    st_bad = cohort_stats(bad)
    ok("db 不可打开 → locked 降级", st_bad["locked"] is True)

    lst = list_cohorts(Path(s.data_dir), settings=s)
    ok("列表按编号排序", [c.id for c in lst] == sorted(c.id for c in lst))


# ======================================================================
# 六、E2E-lite：真实子进程跑一轮
# ======================================================================
def test_e2e(tmp: Path) -> None:
    print("\n== E2E-lite ==")
    s = make_settings(tmp, "e2e", script_text=CHILD_SIMPLE)
    migrate_legacy(s, base_dir=tmp)
    cohort, info = resolve_for_run(s, base_dir=tmp)
    apply_cohort(s, cohort)
    space = SearchSpace.from_dict(SPACE_DICT)
    journal = Journal(Path(s.data_dir) / "journal.jsonl")
    study = create_or_load_study(s)
    runner = TrialRunner(s, space, journal)
    orch = Orchestrator(s, space, study, runner, journal, log=lambda *_: None)
    orch.run(total_trials=1, wake_every=1, cohort=cohort.id,
             cohort_fp=cohort.meta.get("code_hash"), fp_match=True)
    dispose(study)

    ok("db/journal/快照都在分区内",
       (cohort.path / "t.db").exists() and (cohort.path / "journal.jsonl").exists()
       and list(cohort.path.glob("space_v*.yaml")))
    events = journal.load_events()
    ss = [e for e in events if e.get("kind") == SESSION_START]
    ok("SESSION_START 携带分区审计字段",
       ss and ss[-1].get("cohort") == cohort.id
       and ss[-1].get("cohort_fp") == cohort.meta.get("code_hash")
       and ss[-1].get("fp_match") is True)

    from tansuo.report import generate_report
    rp, bp = generate_report(s, study, space, journal,
                             cohort_info={"id": cohort.id,
                                          "fingerprint": cohort.meta.get("code_hash"),
                                          "note": ""})
    dispose(study)   # generate_report 复查 study 会让连接重新入池，须再释放一次
    ok("报告落在分区内且头部含分区信息",
       rp.parent.parent == cohort.path
       and f"记录分区：{cohort.id}" in rp.read_text(encoding="utf-8"))

    # 改脚本再跑 → 自动新分区，旧分区原封
    files_before = sorted((str(p.relative_to(cohort.path)), p.stat().st_size)
                          for p in cohort.path.rglob("*") if p.is_file())
    (tmp / "e2e_train.py").write_text(CHILD_SIMPLE + "# edited\n", encoding="utf-8")
    cohort2, info2 = resolve_for_run(s, base_dir=tmp)
    ok("脚本改动后自动落新分区", cohort2.id != cohort.id and info2["action"] == "created")
    ok("新分区与旧分区同级（不嵌套）", cohort2.path.parent == cohort.path.parent)
    files_after = sorted((str(p.relative_to(cohort.path)), p.stat().st_size)
                         for p in cohort.path.rglob("*") if p.is_file())
    ok("旧分区文件原封不动", files_before == files_after)


# ======================================================================
# 七、config 校验
# ======================================================================
def test_config(tmp: Path) -> None:
    print("\n== config：fingerprint_paths ==")
    s = make_settings(tmp, "cfg_ok", script_text="v = 1\n",
                      fingerprint_paths=["a.py", "pkg"])
    ok("fingerprint_paths 接受字符串列表", s.fingerprint_paths == ["a.py", "pkg"])
    p = tmp / "cfg_bad_settings.yaml"
    p.write_text(
        "experiment: {name: cfg_bad, data_dir: " + (tmp / "d").as_posix()
        + ", fingerprint_paths: not_a_list}\n"
        "metrics:\n  primary: {name: v, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n",
        encoding="utf-8")
    expect_error("fingerprint_paths 非列表 → ConfigError", ConfigError, load_settings, p)

    print("\n== config：dataset ==")
    s_ds = make_settings(tmp, "cfg_ds", script_text="v = 1\n", dataset="mnist")
    ok("dataset 接受字符串并归一为列表", s_ds.dataset == ["mnist"])
    s_ds2 = make_settings(tmp, "cfg_ds2", script_text="v = 1\n", dataset=["a", "b"])
    ok("dataset 接受字符串列表", s_ds2.dataset == ["a", "b"])
    p2 = tmp / "cfg_ds_bad_settings.yaml"
    p2.write_text(
        "experiment: {name: cfg_ds_bad, data_dir: " + (tmp / "d2").as_posix()
        + ", dataset: 42}\n"
        "metrics:\n  primary: {name: v, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n",
        encoding="utf-8")
    expect_error("dataset 非字符串/列表 → ConfigError", ConfigError, load_settings, p2)
    p3 = tmp / "cfg_ds_bad2_settings.yaml"
    p3.write_text(
        "experiment: {name: cfg_ds_bad2, data_dir: " + (tmp / "d3").as_posix()
        + ", dataset: [a, \"\"]}\n"
        "metrics:\n  primary: {name: v, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n  command: [\"python\", \"x.py\"]\n",
        encoding="utf-8")
    expect_error("dataset 含空字符串 → ConfigError", ConfigError, load_settings, p3)


# ======================================================================
# 八、环境审计
# ======================================================================
def test_env_audit(tmp: Path) -> None:
    print("\n== 环境审计 ==")
    from unittest import mock

    audit = collect_env_audit()
    ok("审计含 python/optuna 版本",
       bool(audit["python"]) and bool(audit["optuna"]))
    ok("审计含机器信息（hostname/platform/cpu_count）",
       bool(audit["hostname"]) and bool(audit["platform"])
       and (audit["cpu_count"] or 0) >= 1)
    ok("GPU 字段为合法值（gpus 列表 / cuda_available 布尔）",
       isinstance(audit["gpus"], list) and isinstance(audit["cuda_available"], bool))

    # torch 未安装 → 不导入不报错，字段留 None
    with mock.patch("importlib.util.find_spec", return_value=None):
        a2 = collect_env_audit()
    ok("torch 缺席 → torch=None 且其余字段不受影响",
       a2["torch"] is None and a2["python"] == audit["python"])

    s = make_settings(tmp, "env", script_text="v = 1\n")
    c = create_cohort(Path(s.data_dir), code_fingerprint(s, tmp), s)
    ok("create_cohort 把 environment 写入 meta",
       bool(c.meta.get("environment"))
       and c.meta["environment"]["python"] == audit["python"])
    c2 = load_cohort(Path(s.data_dir), c.id, settings=s)
    ok("environment 随 meta.yaml 落盘往返",
       (c2.meta.get("environment") or {}).get("hostname") == audit["hostname"])

    update_cohort_env(c2)
    ok("续跑刷新写入 environment_last",
       bool(c2.meta.get("environment_last"))
       and c2.meta["environment_last"]["python"] == audit["python"])
    c3 = load_cohort(Path(s.data_dir), c.id, settings=s)
    ok("environment_last 已落盘", bool(c3.meta.get("environment_last")))

    # 不完整分区（无 meta）→ 静默跳过不报错
    runs = Path(s.data_dir) / "runs"
    (runs / "0009-19000101-000000").mkdir(parents=True)
    inc = load_cohort(Path(s.data_dir), "0009-19000101-000000", settings=s)
    update_cohort_env(inc)
    ok("不完整分区刷新被静默跳过", not inc.meta.get("environment_last"))


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_fingerprint(tmp)
        test_fingerprint_module_mode(tmp)
        test_dataset_fingerprint(tmp)
        test_resolve(tmp)
        test_resolve_dataset(tmp)
        test_ids_and_dirs(tmp)
        test_migration(tmp)
        test_virtual_legacy(tmp)
        test_apply(tmp)
        test_list_and_stats(tmp)
        test_e2e(tmp)
        test_config(tmp)
        test_env_audit(tmp)
    print(f"\n全部通过：{PASS} 项断言")
