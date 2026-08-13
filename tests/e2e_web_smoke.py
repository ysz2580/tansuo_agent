"""Web 后端冒烟：起真实 uvicorn，验证 /api/runs、?cohort=、run_start 分区换算。

独立脚本直跑：python tests/e2e_web_smoke.py（约 2-4 分钟，占用端口 8123）。
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TRAIN = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.7}\')\n'
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
PASS = 0


def ok(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def api(path, body=None, method=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _expect_404(path):
    import urllib.error
    try:
        api(path)
    except urllib.error.HTTPError as e:
        return e.code == 404
    except OSError:
        return False
    return False


def wait_idle(timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("/api/run/status")
        if not st["running"]:
            return st
        time.sleep(1)
    raise AssertionError("FAIL: 运行超时未结束")


def dump_failures(tag):
    ts = api("/api/trials")
    for t in ts["trials"]:
        if t["state"] == "FAIL":
            print(f"  [{tag}] trial#{t['number']} FAIL: {t['fail_reason']}")
    print("  ---- run log tail ----")
    print("  " + api("/api/run/log?tail=40")["text"].replace("\n", "\n  "))


proc = None
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "train.py").write_text(TRAIN, encoding="utf-8")
    data = tmp / "data"
    settings_yaml = tmp / "settings.yaml"
    settings_yaml.write_text(
        "experiment:\n  name: websmoke\n  data_dir: " + data.as_posix() + "\n"
        "metrics:\n  primary: {name: val_acc, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n"
        f'  command: ["{Path(sys.executable).as_posix()}", "{(tmp / "train.py").as_posix()}"]\n'
        "  config_via: env\n  timeout_s: 60\n"
        "budget: {total_trials: 4, wake_every: 2, seed: 1, workers: 1, data_fraction: 0.5}\n"
        "pruner: {type: median, n_startup_trials: 2, n_warmup_steps: 0}\n"
        "agent: {enabled: false, model: none}\n"
        "storage: {url: sqlite:///" + (data / "t.db").as_posix() + "}\n",
        encoding="utf-8")
    space_yaml = tmp / "space.yaml"
    space_yaml.write_text(yaml.safe_dump({"params": [
        {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
         "description": "学习率"}]}, allow_unicode=True), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "cli.py"), "web",
         "--settings", str(settings_yaml), "--space", str(space_yaml),
         "--port", str(PORT)],
        cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # 等服务就绪
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                api("/api/health")
                break
            except OSError:
                if proc.poll() is not None:
                    out = proc.stdout.read().decode("utf-8", "replace")
                    raise AssertionError(f"FAIL: web 进程退出：{out}")
                time.sleep(0.5)
        else:
            raise AssertionError("FAIL: web 服务 30s 未就绪")

        print("== 1. 空库：/api/runs 与扁平兜底 ==")
        r = api("/api/runs")
        ok("无分区时 runs 为空", r["runs"] == [] and r["default"] is None)
        ok("current 指纹可用", len(r["current"]["code_hash"]) == 12)

        print("== 2. run/start → 自动创建分区 0001 ==")
        api("/api/run/start", {"trials": 2, "no_agent": True})
        st = wait_idle()
        ok("运行正常退出", st["exit_code"] == 0, detail=str(st))
        ok("last_cohort 已记录", (st["last_cohort"] or "").startswith("0001-"))
        s = api("/api/summary")
        ok("summary 带分区 id", (s.get("cohort") or "").startswith("0001-"))
        if s["counts"]["failed"]:
            dump_failures("step2")
            raise AssertionError("存在失败试验，诊断信息见上")
        ok("2 次试验完结", s["counts"]["completed"] == 2,
           detail=json.dumps(s, ensure_ascii=False))
        ok("日志落在分区内", "runs" in (st["log_path"] or "") and "0001-" in st["log_path"])

        print("== 3. /api/runs 可比性 ==")
        r = api("/api/runs")
        ok("列出 0001", len(r["runs"]) == 1 and r["runs"][0]["id"] == st["last_cohort"])
        ok("与当前指纹一致", r["runs"][0]["comparable"] == "match")
        ok("default 指向最新", r["default"] == st["last_cohort"])
        c1 = r["runs"][0]["id"]

        print("== 4. 改训练代码 → summary 横幅标记 ==")
        (tmp / "train.py").write_text(TRAIN + "# edited\n", encoding="utf-8")
        s = api("/api/summary")
        ok("fingerprint_changed=true（代码已变）", s["fingerprint_changed"] is True)

        print("== 5. 再 run/start → 自动新分区且换算从 0 起 ==")
        api("/api/run/start", {"trials": 1, "no_agent": True})
        st2 = wait_idle()
        ok("新开分区 0002", (st2["last_cohort"] or "").startswith("0002-"))
        c2 = st2["last_cohort"]
        s2 = api(f"/api/summary?cohort={c2}")
        ok("新分区只有 1 次试验（未混入旧计数）", s2["counts"]["completed"] == 1,
           detail=json.dumps(s2, ensure_ascii=False))
        r = api("/api/runs")
        comp = {x["id"]: x["comparable"] for x in r["runs"]}
        ok("0001 标记 code-changed", comp[c1] == "code-changed")
        ok("0002 标记 match", comp[c2] == "match")

        print("== 6. ?cohort= 指定分区读取 ==")
        t1 = api(f"/api/trials?cohort={c1}")
        t2 = api(f"/api/trials?cohort={c2}")
        ok("0001 有 2 条试验", len(t1["trials"]) == 2)
        ok("0002 有 1 条试验", len(t2["trials"]) == 1)
        ok("未知分区 404", _expect_404("/api/trials?cohort=9999-99999999-999999"))

        print("== 7. new_cohort + note → 0003 ==")
        api("/api/run/start", {"trials": 1, "no_agent": True,
                               "new_cohort": True, "note": "网页冒烟"})
        st3 = wait_idle()
        ok("新开分区 0003", (st3["last_cohort"] or "").startswith("0003-"))
        r = api("/api/runs")
        item3 = [x for x in r["runs"] if x["id"] == st3["last_cohort"]]
        ok("备注写入 meta", item3 and item3[0]["note"] == "网页冒烟")

        print("== 8. 报告端点分区化 ==")
        g = api(f"/api/report/generate?cohort={c1}", method="POST")
        ok("报告生成在 0001 分区内", c1 in g["report"])
        rep = api(f"/api/report?cohort={c1}")
        ok("报告可读且含分区头", rep["exists"] and f"记录分区：{c1}" in rep["content"])

        print("== 9. fresh 别名 → 新分区而非删除 ==")
        before = {x["id"] for x in api("/api/runs")["runs"]}
        api("/api/run/start", {"trials": 1, "no_agent": True, "fresh": True})
        st4 = wait_idle()
        after = {x["id"] for x in api("/api/runs")["runs"]}
        ok("历史分区全部保留", before <= after)
        ok("fresh 开了新分区", (st4["last_cohort"] or "").startswith("0004-"))
        c4 = st4["last_cohort"]

        print("== 10. 数据集指纹（第三维度）==")
        orig = settings_yaml.read_text(encoding="utf-8")
        cfg = yaml.safe_load(orig)
        cfg["experiment"]["dataset"] = "smoke-A"
        settings_yaml.write_text(yaml.safe_dump(cfg, allow_unicode=True),
                                 encoding="utf-8")
        s = api("/api/summary")
        ok("数据集声明变化 → 横幅标记", s["fingerprint_changed"] is True)
        api("/api/run/start", {"trials": 1, "no_agent": True})
        st5 = wait_idle()
        ok("数据集变化新开分区 0005", (st5["last_cohort"] or "").startswith("0005-"))
        c5 = st5["last_cohort"]
        r = api("/api/runs")
        comp = {x["id"]: x["comparable"] for x in r["runs"]}
        ok("0001 标记 code-data-changed（旧代码+无数据集声明）",
           comp[c1] == "code-data-changed")
        ok("0004 标记 data-changed", comp[c4] == "data-changed")
        ok("0005 标记 match", comp[c5] == "match")
        settings_yaml.write_text(orig, encoding="utf-8")  # 撤销声明=改回数据集
        api("/api/run/start", {"trials": 1, "no_agent": True})
        st6 = wait_idle()
        ok("数据集改回 → 恢复 0004 续跑", st6["last_cohort"] == c4)

        print("\nWeb 冒烟全部通过")
    finally:
        if proc and proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.terminate()

print(f"共 {PASS} 项断言")
