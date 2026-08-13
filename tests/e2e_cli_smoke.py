"""CLI 端到端冒烟：迁移 → 0001 → 改脚本 → 0002 → --fresh 零删除 → runs/report/拒绝续跑
→ 跨分区对比 → 新分区热启动。

独立脚本直跑：python tests/e2e_cli_smoke.py（约 1-2 分钟，起真实子进程）。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TRAIN = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.7}\')\n'
PASS = 0


def ok(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def run_cli(cwd, *args, expect=0):
    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), *args],
                       cwd=str(cwd), capture_output=True, text=True, timeout=300)
    if r.returncode != expect:
        print("---- stdout ----\n" + r.stdout)
        print("---- stderr ----\n" + r.stderr)
        raise AssertionError(f"FAIL: cli {' '.join(args)} rc={r.returncode} (期望 {expect})")
    return r


def snapshot(dirp: Path):
    return sorted((str(p.relative_to(dirp)), p.stat().st_size)
                  for p in dirp.rglob("*") if p.is_file())


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "train.py").write_text(TRAIN, encoding="utf-8")
    data = tmp / "data"
    settings_yaml = tmp / "settings.yaml"
    settings_yaml.write_text(
        "experiment:\n  name: smoke\n  data_dir: " + data.as_posix() + "\n"
        "metrics:\n  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n"
        f'  command: ["{Path(sys.executable).as_posix()}", "train.py"]\n'
        "  config_via: env\n  timeout_s: 60\n"
        "budget: {total_trials: 4, wake_every: 2, seed: 1, workers: 1, data_fraction: 0.5}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        "storage: {url: sqlite:///" + (data / "t.db").as_posix() + "}\n",
        encoding="utf-8")
    settings_min = tmp / "settings_min.yaml"
    settings_min.write_text(
        settings_yaml.read_text(encoding="utf-8").replace("direction: maximize",
                                                          "direction: minimize"),
        encoding="utf-8")
    space_yaml = tmp / "space.yaml"
    space_yaml.write_text(yaml.safe_dump({"params": [
        {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
         "description": "学习率"}]}, allow_unicode=True), encoding="utf-8")

    S = ["--settings", str(settings_yaml), "--space", str(space_yaml)]

    print("== 1. 旧布局迁移 + 首跑 ==")
    data.mkdir(parents=True)
    (data / "journal.jsonl").write_text('{"kind": "leftover"}\n', encoding="utf-8")
    r1 = run_cli(tmp, "run", *S, "--trials", "2", "--no-agent")
    ok("旧文件迁入 0000-legacy", "旧布局记录已迁入 runs/0000-legacy/" in r1.stdout)
    ok("新开分区 0001", "[记录] 新开分区 0001-" in r1.stdout)
    runs_root = data / "runs"
    c0 = runs_root / "0000-legacy"
    ok("legacy 内有旧 journal", (c0 / "journal.jsonl").exists())
    c1 = next(p for p in runs_root.iterdir() if p.name.startswith("0001-"))
    ok("分区内自带 db/journal/meta",
       (c1 / "t.db").exists() and (c1 / "journal.jsonl").exists()
       and (c1 / "meta.yaml").exists())
    ok("根目录不再残留 db", not (data / "t.db").exists())

    print("== 2. runs 列表（当前指纹一致）==")
    r2 = run_cli(tmp, "runs", *S)
    ok("列出 0000-legacy 与 0001", "0000-legacy" in r2.stdout and c1.name in r2.stdout)
    ok("0001 与当前指纹一致", "✔ 与当前指纹完全一致" in r2.stdout)
    ok("legacy 标历史记录", "历史记录（无指纹）" in r2.stdout)

    print("== 3. 改训练脚本 → 自动新分区 ==")
    snap1 = snapshot(c1)
    (tmp / "train.py").write_text(TRAIN + "# edited\n", encoding="utf-8")
    r3 = run_cli(tmp, "run", *S, "--trials", "1", "--no-agent")
    ok("指纹变化自动新开分区", "[记录] 新开分区 0002-" in r3.stdout
       and "训练代码指纹变化" in r3.stdout)
    ok("旧分区原封不动", snap1 == snapshot(c1))
    r4 = run_cli(tmp, "runs", *S)
    ok("0001 变为不可比标记", "△ 目标一致、训练代码已变" in r4.stdout)

    print("== 4. --fresh 零删除 ==")
    snap_all = snapshot(runs_root)
    r5 = run_cli(tmp, "run", *S, "--trials", "1", "--no-agent", "--fresh",
                 "--note", "冒烟备注")
    ok("fresh 提示不再删除", "不再删除任何历史记录" in r5.stdout)
    ok("fresh 新开分区 0003", "[记录] 新开分区 0003-" in r5.stdout)
    after = snapshot(runs_root)
    ok("历史分区文件分毫未动", all(item in after for item in snap_all))
    ok("0003 分区已创建", any(x[0].startswith("0003-") for x in after))
    r6 = run_cli(tmp, "runs", *S)
    ok("备注出现在列表", "冒烟备注" in r6.stdout)

    print("== 5. runs show / report --cohort ==")
    r7 = run_cli(tmp, "runs", "show", c1.name, *S)
    ok("show 输出指纹覆盖文件", "指纹覆盖文件" in r7.stdout and "train.py" in r7.stdout)
    ok("show 输出环境审计", "环境审计（创建时）" in r7.stdout and "机器：" in r7.stdout)
    r7b = run_cli(tmp, "runs", "show", "0000-legacy", *S)
    ok("legacy 分区无环境审计如实提示", "无记录" in r7b.stdout)
    r8 = run_cli(tmp, "report", "--cohort", c1.name, *S)
    ok("为旧分区生成报告", f"为分区 {c1.name} 生成报告" in r8.stdout
       and (c1 / "reports" / "report.md").exists())

    print("== 6. 目标语义变化 → 拒绝显式续跑 ==")
    c3 = next(p for p in runs_root.iterdir() if p.name.startswith("0003-"))
    r9 = run_cli(tmp, "run", "--settings", str(settings_min), "--space", str(space_yaml),
                 "--trials", "1", "--no-agent", "--cohort", c3.name, expect=2)
    ok("拒绝信息说明方向污染", "优化目标已变化" in r9.stderr
       and "静默沿用库内方向" in r9.stderr)

    print("== 7. 自动模式遇目标变化 → 新开分区 ==")
    r10 = run_cli(tmp, "run", "--settings", str(settings_min), "--space", str(space_yaml),
                  "--trials", "1", "--no-agent")
    ok("目标变化新开分区且原因具体", "[记录] 新开分区 0004-" in r10.stdout
       and "优化目标变化" in r10.stdout)

    print("== 8. runs compare 跨分区对比 ==")
    # 现状：0001/0002/0003 同目标（maximize），0004 目标为 minimize
    c2 = next(p for p in runs_root.iterdir() if p.name.startswith("0002-"))
    c4 = next(p for p in runs_root.iterdir() if p.name.startswith("0004-"))
    r11 = run_cli(tmp, "runs", "compare", *S)
    ok("缺省组含当前目标可比的全部三个分区",
       c1.name in r11.stdout and c2.name in r11.stdout and c3.name in r11.stdout)
    ok("异目标分区 0004 不在缺省组", c4.name not in r11.stdout)
    ok("对比头部与参数对比表齐全",
       "跨分区对比" in r11.stdout and "最优配置对比" in r11.stdout)
    r12 = run_cli(tmp, "runs", "compare", c1.name, c2.name, *S)
    ok("显式指定两个分区对比成功", c1.name in r12.stdout and c2.name in r12.stdout)
    r13 = run_cli(tmp, "runs", "compare", c1.name, c4.name, *S, expect=2)
    ok("跨目标对比被拒并说明原因", "优化目标不一致" in r13.stderr)

    print("== 9. 新分区热启动 ==")
    # 同目标历史分区 0001/0002/0003 都有成果；--new 开 0005 应自动入队种子
    r14 = run_cli(tmp, "run", *S, "--trials", "1", "--no-agent", "--new")
    ok("新分区 0005 出现热启动提示", "[热启动]" in r14.stdout
       and "[记录] 新开分区 0005-" in r14.stdout)
    ok("种子来源含同目标历史分区", "0001-" in r14.stdout)
    ok("异目标分区不作为种子来源", "来源分区" in r14.stdout
       and r14.stdout.split("来源分区：")[1].split("）")[0].find("0004-") < 0)
    r15 = run_cli(tmp, "run", *S, "--trials", "1", "--no-agent", "--new",
                  "--warm-start", "0")
    ok("--warm-start 0 关闭热启动", "[热启动]" not in r15.stdout)

print(f"\nCLI 冒烟全部通过：{PASS} 项")
