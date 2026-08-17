"""Web 后端冒烟：起真实 uvicorn，验证 /api/runs、?cohort=、run_start 分区换算、
项目管理、setup 互斥、GPU 清单/选卡记账、毕业赛、配置回写。

独立脚本直跑：python tests/e2e_web_smoke.py（约 3-5 分钟，占用端口 8123）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TRAIN = 'import json\nprint(\'##TANSUO## {"type": "final", "value": 0.7}\')\n'
# 慢速训练脚本：每试验约 3s，供「运行中切换项目被拒」一类时序断言用
TRAIN_SLOW = ('import time\n'
              'print(\'##TANSUO## {"type": "epoch", "epoch": 0, '
              '"metrics": {"val_acc": 0.5}}\')\n'
              'time.sleep(3)\n'
              'print(\'##TANSUO## {"type": "final", "value": 0.7}\')\n')
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
        "storage: {url: sqlite:///" + (data / "t.db").as_posix() + "}\n"
        "notify:\n"
        "  enabled: true\n"
        "  webhook_url: ${ENV:WEB_SMOKE_WEBHOOK_NOTSET:}\n"
        "  format: generic\n"
        "  events: [session_end, agent_degrade]\n",
        encoding="utf-8")
    space_yaml = tmp / "space.yaml"
    space_yaml.write_text(yaml.safe_dump({"params": [
        {"name": "lr", "type": "float", "low": 0.01, "high": 0.1,
         "description": "学习率"}]}, allow_unicode=True), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "cli.py"), "web",
         "--settings", str(settings_yaml), "--space", str(space_yaml),
         "--port", str(PORT)],
        cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 项目注册表隔离到临时目录：不污染用户真实的 ~/.tansuo_agent/projects.json
        # 假 LLM 端点：保证 setup 的 probe_endpoint 快速必败（exit 1），
        # 即使开发机有真实凭据也不会命中真 LLM
        env={**os.environ, "TANSUO_PROJECT_STORE": str(tmp / "projects.json"),
             "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
             "ANTHROPIC_AUTH_TOKEN": "smoke-fake"})
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
        ok("summary 含参数重要度字段（dict；2 试验可能尚不可算）",
           isinstance(s.get("importances"), dict), str(s.get("importances")))
        ok("日志落在分区内", "runs" in (st["log_path"] or "") and "0001-" in st["log_path"])

        print("== 3. /api/runs 可比性 ==")
        r = api("/api/runs")
        ok("列出 0001", len(r["runs"]) == 1 and r["runs"][0]["id"] == st["last_cohort"])
        ok("与当前指纹一致", r["runs"][0]["comparable"] == "match")
        ok("runs 条目带提示词版本（无 prompts.yaml → 0 且未变化）",
           r["runs"][0]["prompt_version"] == 0
           and r["runs"][0]["prompt_changed"] is False)
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
        ok("报告含参数重要度段", "## 参数重要度" in rep["content"])

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

        print("== 11. 跨分区对比端点 ==")
        # 本冒烟全程目标未变：0001-0005 五个分区同属一个可比组
        cp = api("/api/runs/compare")
        ok("缺省对比组含全部五个同目标分区", len(cp["cohorts"]) == 5)
        ok("对比基准含主指标与方向",
           cp["primary"]["name"] == "val_acc" and cp["primary"]["direction"] == "maximize")
        ok("每个分区结构完整（best/curve 字段齐全）",
           all({"best", "curve", "completed", "locked"} <= set(e) for e in cp["cohorts"]))
        ok("对比条目带提示词版本字段",
           all(isinstance(e.get("prompt_version"), int) for e in cp["cohorts"]))
        one = cp["cohorts"][0]["id"]
        cp1 = api(f"/api/runs/compare?cohorts={one}")
        ok("显式指定单分区返回一条", len(cp1["cohorts"]) == 1 and cp1["cohorts"][0]["id"] == one)
        ok("未知分区 404", _expect_404("/api/runs/compare?cohorts=9999-99999999-999999"))

        print("== 12. 提示词管理（前后端同步）==")
        import urllib.error
        pr = api("/api/config/prompts")
        ok("三条提示词 + 版本 0 + 空历史",
           len(pr["prompts"]) == 3 and pr["version"] == 0 and pr["history"] == [])
        tune = next(p for p in pr["prompts"] if p["name"] == "tuning_system")
        ok("初始 override 为空、生效=默认、含可用变量",
           tune["override"] == "" and tune["effective"] == tune["default"]
           and "total_trials" in tune["vars"])
        pv = api("/api/config/prompts/preview",
                 {"which": "tuning_system", "text": ""})
        ok("预览默认模板：实验名注入且无未填充占位符",
           "websmoke" in pv["rendered"] and pv["missing_vars"] == [])
        pv2 = api("/api/config/prompts/preview",
                  {"which": "tuning_wake_brief", "text": "轮 {{round_no}} / 余 {{budget_left}}"})
        ok("预览自定义简报按样例上下文渲染", pv2["rendered"] == "轮 1 / 余 4")
        pv3 = api("/api/config/prompts/preview",
                  {"which": "tuning_wake_brief", "text": ""})
        ok("默认简报预览含护栏信号样例且无未填充占位符",
           pv3["missing_vars"] == [] and "⚠" in pv3["rendered"])
        sv = api("/api/config/prompts/save",
                 {"which": "tuning_system", "text": "自定义：预算 {{total_trials}}",
                  "rationale": "冒烟覆盖"})
        ok("保存 version=1 且落 prompts.yaml",
           sv["version"] == 1 and (tmp / "prompts.yaml").exists())
        r = api("/api/runs")
        ok("提示词版本递增后既有分区全部标记 prompt_changed",
           all(x["prompt_changed"] is True for x in r["runs"]))
        pr2 = api("/api/config/prompts")
        tune2 = next(p for p in pr2["prompts"] if p["name"] == "tuning_system")
        ok("GET 反映覆盖（effective=override）且历史 +1",
           tune2["override"] == "自定义：预算 {{total_trials}}"
           and tune2["effective"] == tune2["override"] and len(pr2["history"]) == 1)
        pv3 = api("/api/config/prompts/preview",
                  {"which": "tuning_system", "text": tune2["override"]})
        ok("覆盖模板按真实 total_trials 渲染", pv3["rendered"] == "自定义：预算 4")
        try:
            api("/api/config/prompts/save",
                {"which": "tuning_system", "text": "x", "rationale": "   "})
            raise AssertionError("FAIL: 空 rationale 应被拒（400）")
        except urllib.error.HTTPError as e:
            ok("空 rationale 保存被拒（400）", e.code == 400)
        sv2 = api("/api/config/prompts/save",
                  {"which": "tuning_system", "text": "", "rationale": "恢复出厂"})
        ok("空文本=恢复出厂且仍计版本留痕",
           sv2["version"] == 2
           and next(p for p in api("/api/config/prompts")["prompts"]
                    if p["name"] == "tuning_system")["effective"]
           == tune["default"])

        print("== 13. webhook 通知（配置写回 + 测试 + 会话结束实收）==")
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Cap(BaseHTTPRequestHandler):
            bodies = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                _Cap.bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_):
                pass

        cap = ThreadingHTTPServer(("127.0.0.1", 0), _Cap)
        threading.Thread(target=cap.serve_forever, daemon=True).start()
        cap_url = f"http://127.0.0.1:{cap.server_address[1]}/robot"

        nc = api("/api/config/notify")
        ok("GET 返回初始通知配置（ENV 空默认 → 未设置）",
           nc["enabled"] is True and nc["format"] == "generic"
           and sorted(nc["events"]) == ["agent_degrade", "session_end"]
           and nc["webhook_url_masked"] == "(未设置)", str(nc))
        try:
            api("/api/config/notify/save", {"format": "wechat"})
            raise AssertionError("FAIL: 非法 format 应被拒（400）")
        except urllib.error.HTTPError as e:
            ok("保存拒绝非法 format（400）", e.code == 400)
        sv3 = api("/api/config/notify/save",
                  {"webhook_url": cap_url, "format": "dingtalk"})
        ok("写回 webhook_url 与 format",
           set(sv3["write_back"]["changed"]) == {"webhook_url", "format"}, str(sv3))
        ok("明文写回触发告警", any("明文" in w for w in sv3["warnings"]),
           str(sv3["warnings"]))
        nc2 = api("/api/config/notify")
        ok("GET 反映写回（format=dingtalk、url 已打码）",
           nc2["format"] == "dingtalk" and nc2["webhook_url_masked"]
           and nc2["webhook_url_masked"] != "(未设置)", str(nc2))
        tr = api("/api/config/notify/test", method="POST")
        ok("测试通知发送成功", tr["ok"] is True, str(tr))
        ok("捕获服务器收到钉钉信封测试消息",
           len(_Cap.bodies) == 1 and _Cap.bodies[0].get("msgtype") == "text"
           and "测试" in _Cap.bodies[0]["text"]["content"], str(_Cap.bodies))

        # 再跑一次会话（预算内的试验已全部完结 → resume-skip 收尾也要通知）
        api("/api/run/start", {"trials": 1, "no_agent": True})
        wait_idle()
        t0 = time.time()
        while time.time() - t0 < 5 and len(_Cap.bodies) < 2:
            time.sleep(0.2)
        fin = [b for b in _Cap.bodies
               if "调参结束" in b.get("text", {}).get("content", "")]
        ok("会话结束 webhook 实收（钉钉信封 + 结束原因）",
           len(fin) == 1 and "budget_exhausted" in fin[0]["text"]["content"],
           str(_Cap.bodies[1:]))
        cap.shutdown()

        print("== 14. 项目管理：注册 / 新建脚手架 / 激活切换 / 目录浏览 ==")
        import urllib.error as _ue
        pj = api("/api/projects")
        ok("注册表至少 2 个项目（demo + env）", len(pj["projects"]) >= 2, str(pj))
        ok("active_id 非空", pj["active_id"] is not None)
        act = api("/api/projects/active")
        ok("active 端点返回激活项", act.get("id") == pj["active_id"])

        # 新建项目目录 + 慢速训练脚本（供运行中切换断言）
        projc = tmp / "projC"
        projc.mkdir()
        (projc / "train.py").write_text(TRAIN_SLOW, encoding="utf-8")

        # fs/browse：浏览 tmp 应见 projC；拒绝 ..
        br = api("/api/fs/browse?path=" + urllib.parse.quote(str(tmp)))
        ok("目录浏览列出 projC", any(d["name"] == "projC" for d in br["dirs"]), str(br))
        try:
            api("/api/fs/browse?path=" + urllib.parse.quote(str(tmp / "..")))
            raise AssertionError("FAIL: 含 .. 的路径应被拒")
        except _ue.HTTPError as e:
            ok("目录浏览拒绝 ..（400）", e.code == 400)

        # 新建项目 → 脚手架 .tansuo/
        cr = api("/api/projects", {"name": "projC", "dir": str(projc),
                                   "train_script": str(projc / "train.py")})
        ok("新建项目返回 scaffolded=true", cr["scaffolded"] is True, str(cr))
        ok(".tansuo/settings.yaml 已生成",
           (projc / ".tansuo" / "settings.yaml").exists())
        ok(".tansuo/search_space.yaml 已生成",
           (projc / ".tansuo" / "search_space.yaml").exists())

        # 激活新项目 → /api/runs 为空
        api(f"/api/projects/{cr['id']}/activate", method="POST")
        ok("激活后 active 指向新项目",
           api("/api/projects/active")["id"] == cr["id"])
        ok("新项目尚无分区", api("/api/runs")["runs"] == [])

        # 新项目跑一次 → 验证 base_dir=projC 全链路（数据落 .tansuo/data）
        api("/api/run/start", {"trials": 1, "no_agent": True})
        stc = wait_idle()
        ok("新项目运行正常退出", stc["exit_code"] == 0, str(stc))
        ok("日志落在项目 .tansuo 目录内", ".tansuo" in (stc["log_path"] or ""),
           str(stc.get("log_path")))
        ok("新项目 summary 有 1 次完结",
           api("/api/summary")["counts"]["completed"] == 1)

        # 跨项目并行：projC 搜索运行中，允许切走、允许别的项目也开搜索
        api("/api/run/start", {"trials": 3, "no_agent": True})
        t0 = time.time()
        while time.time() - t0 < 10 and not api("/api/run/status")["running"]:
            time.sleep(0.1)
        ok("projC 搜索已启动", api("/api/run/status")["running"])
        demo_id = next(p["id"] for p in api("/api/projects")["projects"]
                       if "内置示例" in p["name"])
        api(f"/api/projects/{demo_id}/activate", method="POST")
        ok("运行中切换项目被允许（跨项目并行）",
           api("/api/projects/active")["id"] == demo_id)
        ok("切换后 status 仍指向运行中的槽", api("/api/run/status")["running"])
        api("/api/run/start", {"trials": 1, "no_agent": True})
        t0 = time.time()
        both = False
        while time.time() - t0 < 15:
            projs = {p["id"]: p for p in api("/api/projects")["projects"]}
            if projs[cr["id"]]["run_running"] and projs[demo_id]["run_running"]:
                both = True
                break
            time.sleep(0.2)
        ok("两个项目并行搜索（各自 run_running=true）", both, str(projs))
        # 清理：反复 stop 运行中的槽，直到没有项目在跑
        t0 = time.time()
        while time.time() - t0 < 90:
            running = [p for p in api("/api/projects")["projects"]
                       if p["run_running"]]
            if not running:
                break
            try:
                api("/api/run/stop", method="POST")
            except _ue.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("FAIL: 并行搜索 90s 内未清理完毕")
        ok("并行槽清理后无项目在运行",
           not any(p["run_running"] for p in api("/api/projects")["projects"]))

        # 切回 env 项目 → 历史分区仍在（前面已积累 >=5 个）
        env_entry = next(p for p in api("/api/projects")["projects"]
                         if Path(p["settings_path"]).resolve()
                         == settings_yaml.resolve())
        api(f"/api/projects/{env_entry['id']}/activate", method="POST")
        ok("切回 env 项目历史分区仍在（>=5）",
           len(api("/api/runs")["runs"]) >= 5,
           str(len(api("/api/runs")["runs"])))

        print("== 15. setup agent Web 化：互斥语义 + 子进程拉起（假端点必败） ==")
        st0 = api("/api/setup/status")
        ok("setup 初始空闲", st0["running"] is False and st0["exit_code"] is None)
        try:
            api("/api/projects/nonexistent/setup", method="POST")
            raise AssertionError("FAIL: 未知项目 setup 应 404")
        except _ue.HTTPError as e:
            ok("未知项目 setup 被拒（404）", e.code == 404)
        try:
            api(f"/api/projects/{env_entry['id']}/setup", method="POST")
            raise AssertionError("FAIL: 未登记训练脚本的项目 setup 应 400")
        except _ue.HTTPError as e:
            ok("未登记训练脚本 setup 被拒（400）", e.code == 400)

        # 同项目内搜索运行中启动 setup → 409（项目内硬互斥；跨项目则互不阻塞）
        api(f"/api/projects/{cr['id']}/activate", method="POST")
        api("/api/run/start", {"trials": 2, "no_agent": True})
        t0 = time.time()
        while time.time() - t0 < 10 and not api("/api/run/status")["running"]:
            time.sleep(0.1)
        ok("projC 搜索已启动（互斥前置）", api("/api/run/status")["running"])
        try:
            api(f"/api/projects/{cr['id']}/setup", method="POST")
            raise AssertionError("FAIL: 搜索运行中 setup 应被拒")
        except _ue.HTTPError as e:
            ok("搜索运行中启动 setup 被拒（409）", e.code == 409)
        api("/api/run/stop", method="POST")
        wait_idle()

        # 拉起 setup 子进程：假端点 → probe 必败 exit 1，日志含诊断
        sp = api(f"/api/projects/{cr['id']}/setup", method="POST")
        ok("setup 拉起返回 pid 与日志路径", bool(sp["pid"]) and bool(sp["log_path"]),
           str(sp))
        t0 = time.time()
        st = None
        while time.time() - t0 < 60:
            st = api("/api/setup/status")
            if st["exit_code"] is not None:
                break
            time.sleep(0.3)
        else:
            raise AssertionError("FAIL: setup 子进程 60s 未结束")
        ok("假端点探测失败退出（exit_code=1）", st["exit_code"] == 1, str(st))
        lg = api("/api/setup/log?tail=500")
        ok("setup 日志含端点探测失败诊断", "端点探测失败" in lg["text"],
           lg["text"][-300:])
        ev = api("/api/setup/events")
        ok("setup 事件端点可查（probe 失败未写 journal → 空列表）",
           isinstance(ev["events"], list) and ev["events"] == [], str(ev))

        print("== 15b. 训练脚本补登记：候选扫描 + 占位命令拦截 + 回填 ==")
        projd = tmp / "projD"
        projd.mkdir()
        (projd / "train_model.py").write_text(
            "import argparse\n\n"
            'if __name__ == "__main__":\n'
            "    ap = argparse.ArgumentParser()\n"
            "    for epoch in range(2):\n"
            "        loss = 0.1\n", encoding="utf-8")
        (projd / "utils.py").write_text("def helper():\n    return 1\n",
                                        encoding="utf-8")
        crd = api("/api/projects", {"name": "projD", "dir": str(projd)})
        ok("projD 创建时未选训练脚本 → scaffolded", crd["scaffolded"] is True)
        ok("projD train_script 为空", crd["train_script"] == "")
        api(f"/api/projects/{crd['id']}/activate", method="POST")
        # 占位启动命令（path/to/your_train.py）应在启动前拦截，而不是跑一轮全败
        try:
            api("/api/run/start", {"trials": 1, "no_agent": True})
            raise AssertionError("FAIL: 占位启动命令应被拦截")
        except _ue.HTTPError as e:
            detail = json.loads(e.read().decode("utf-8"))["detail"]
            ok("未配置的占位命令启动前拦截（400 + 指引）",
               e.code == 400 and "不存在" in detail and "配置" in detail,
               detail)
        # 候选扫描：train_model.py 排第一且依据非空；纯工具文件不入列
        cds = api(f"/api/projects/{crd['id']}/train-candidates")["candidates"]
        ok("候选扫描把 train_model.py 排第一",
           bool(cds) and cds[0]["rel"] == "train_model.py", str(cds))
        ok("候选带评分依据（reasons 非空）", bool(cds[0]["reasons"]))
        ok("无关 utils.py 不入候选列（score 0）",
           all(c["rel"] != "utils.py" for c in cds), str(cds))
        # 补登记 → 脚手架占位命令同步回填
        sts = api(f"/api/projects/{crd['id']}/train-script",
                  {"train_script": str(projd / "train_model.py")})
        ok("补登记返回 settings_patched=true", sts["settings_patched"] is True,
           str(sts))
        txt = (projd / ".tansuo" / "settings.yaml").read_text(encoding="utf-8")
        ok("占位命令已回填真实脚本路径",
           "train_model.py" in txt and "path/to/your_train.py" not in txt)
        ok("注册表条目的 train_script 已更新",
           any(p["id"] == crd["id"] and p["train_script"]
               for p in api("/api/projects")["projects"]))
        try:
            api(f"/api/projects/{crd['id']}/train-script",
                {"train_script": str(projd / "nope.py")})
            raise AssertionError("FAIL: 不存在的脚本应被拒")
        except _ue.HTTPError as e:
            ok("不存在的脚本被拒（400）", e.code == 400)
        try:
            api(f"/api/projects/{crd['id']}/train-script",
                {"train_script": str(tmp / "train.py")})   # 项目目录之外
            raise AssertionError("FAIL: 项目目录外的脚本应被拒")
        except _ue.HTTPError as e:
            ok("项目目录外的脚本被拒（400）", e.code == 400)

        print("== 16. GPU 清单与选卡记账 ==")
        g = api("/api/gpus")
        ok("/api/gpus 返回列表结构（无 GPU 时为空列表）",
           isinstance(g["gpus"], list), str(g))
        if g["gpus"]:
            ok("GPU 条目字段齐全",
               all({"index", "name", "memory_used_mb", "memory_total_mb",
                    "utilization"} <= set(x) for x in g["gpus"]), str(g["gpus"]))
        try:
            api("/api/run/start", {"trials": 1, "no_agent": True, "gpus": [-1]})
            raise AssertionError("FAIL: 负数 GPU 序号应被拒")
        except _ue.HTTPError as e:
            ok("负数 GPU 序号被拒（400）", e.code == 400)
        # projC 选卡再跑两次：成本按卡数折算（无卡机器注入照样成立）
        api(f"/api/projects/{cr['id']}/activate", method="POST")
        api("/api/run/start", {"trials": 2, "no_agent": True, "gpus": [0]})
        stg = wait_idle()
        ok("选卡运行正常退出", stg["exit_code"] == 0, str(stg))
        cmp_ = api("/api/summary").get("compute")
        ok("summary 含算力记账块", isinstance(cmp_, dict), str(cmp_))
        ok("选卡入记账且按卡数折算（单位=GPU·小时）",
           cmp_["gpus"] == [0] and cmp_["slots"] == 1
           and cmp_["unit"] == "GPU·小时", str(cmp_))
        ok("算力量非负", cmp_["compute_hours"] >= 0, str(cmp_))

        print("== 17. 毕业赛：best 配置全量复验 ==")
        gr0 = api("/api/graduate")
        ok("未举办过毕业赛（exists=false）", gr0["exists"] is False, str(gr0))
        # 搜索运行中启动毕业赛 → 409（同项目互斥，复用运行槽）
        api("/api/run/start", {"trials": 3, "no_agent": True})
        t0 = time.time()
        while time.time() - t0 < 10 and not api("/api/run/status")["running"]:
            time.sleep(0.1)
        try:
            api("/api/graduate", method="POST")
            raise AssertionError("FAIL: 搜索运行中启动毕业赛应被拒")
        except _ue.HTTPError as e:
            ok("搜索运行中启动毕业赛被拒（409）", e.code == 409)
        api("/api/run/stop", method="POST")
        wait_idle()
        # 启动毕业赛（慢脚本约 3s）→ 随即再启动：运行槽被占 → 409
        api("/api/graduate", method="POST")
        t0 = time.time()
        conflict = None
        while time.time() - t0 < 5:
            try:
                api("/api/graduate", method="POST")
            except _ue.HTTPError as e:
                conflict = e.code
                break
            time.sleep(0.2)
        ok("毕业赛运行中重复启动被拒（409）", conflict == 409, str(conflict))
        stg2 = wait_idle()
        ok("毕业赛正常退出", stg2["exit_code"] == 0, str(stg2))
        gr = api("/api/graduate")
        ok("结果落盘且 status=ok",
           gr["exists"] and gr["result"]["status"] == "ok", str(gr))
        ok("全量复验 verdict=pass（脚本恒定 0.7）",
           gr["result"]["verdict"] == "pass", str(gr["result"]))
        ok("结果含 best 溯源与复验参数",
           isinstance(gr["result"]["params"], dict)
           and isinstance(gr["result"]["best_trial"], int),
           str(sorted(gr["result"].keys())))

        print("== 18. 配置回写：preview 不落盘、apply 备份后写入 ==")
        cfg_target = projc / "user_cfg.yaml"
        cfg_target.write_text("lr: 0.5\nmomentum: 0.9\n", encoding="utf-8")
        pv = api("/api/export/preview", {"target": str(cfg_target)})
        ok("preview：同名键 lr 进 changed（旧值保留）",
           any(c_["key"] == "lr" and c_["old"] == 0.5 for c_ in pv["changed"]),
           str(pv["changed"]))
        ok("preview 不落盘",
           cfg_target.read_text(encoding="utf-8") == "lr: 0.5\nmomentum: 0.9\n")
        ap = api("/api/export/apply", {"target": str(cfg_target)})
        ok("apply：备份后写入",
           ap["applied"] and ap["backup"].endswith(".bak"), str(ap))
        ok("备份内容 = 写入前原文",
           (projc / "user_cfg.yaml.bak").read_text(encoding="utf-8")
           == "lr: 0.5\nmomentum: 0.9\n")
        new = yaml.safe_load(cfg_target.read_text(encoding="utf-8"))
        ok("回写后 lr=best 值、无关键保留",
           abs(new["lr"] - pv["params"]["lr"]) < 1e-12 and new["momentum"] == 0.9,
           str(new))
        try:
            # settings_yaml 在 tmp 根上、projC 项目目录之外 → 路径逃逸被拒
            api("/api/export/preview", {"target": str(settings_yaml)})
            raise AssertionError("FAIL: 项目目录外的路径应被拒")
        except _ue.HTTPError as e:
            ok("项目目录外的路径被拒（400）", e.code == 400)

        print("== 19. P0：预算预估 / 人工试验插队 / 试验日志下钻 ==")
        # 1) 预算预估：projC 已有完结试验 → history 口径；多槽位按 GPU·小时折算
        est = api("/api/estimate?trials=5&slots=1")
        ok("estimate 走分区历史口径（basis=history 且样本数 ≥1）",
           est["basis"] == "history" and est.get("sample", 0) >= 1
           and est["per_trial_s"] > 0, str(est))
        ok("estimate 给出估算与含余量建议值",
           est["est_hours"] > 0 and est["unit"] == "机时"
           and est["recommended_max"] > est["est_hours"], str(est))
        est2 = api("/api/estimate?trials=5&slots=2")
        ok("多槽位按 GPU·小时折算（耗时 ×2，容许 4 位小数舍入）",
           est2["unit"] == "GPU·小时"
           and abs(est2["est_hours"] - est["est_hours"] * 2) < 5e-4, str(est2))
        # 2) 一键采纳 → budget.max_gpu_hours 写回 projC 的 settings（块形态插入）
        ad = api("/api/estimate/adopt", {"max_gpu_hours": est["recommended_max"]})
        ok("adopt 写回成功",
           ad["write_back"]["ok"] and ad["write_back"]["changed"] == ["max_gpu_hours"],
           str(ad))
        cfgw = yaml.safe_load((projc / ".tansuo" / "settings.yaml")
                              .read_text(encoding="utf-8"))
        ok("budget.max_gpu_hours 落盘且模板其余字段完好",
           abs(cfgw["budget"]["max_gpu_hours"] - est["recommended_max"]) < 1e-9
           and cfgw["budget"]["total_trials"] == 30, str(cfgw.get("budget")))
        try:
            api("/api/estimate/adopt", {"max_gpu_hours": -1})
            raise AssertionError("FAIL: 非正算力上限应被拒")
        except _ue.HTTPError as e:
            ok("非正算力上限被拒（422/400）", e.code in (400, 422))

        # 3) 人工试验插队：非法参数当场拒；合法参数空闲即时执行
        try:
            api("/api/custom", {"params": {}})
            raise AssertionError("FAIL: 空 params 应被拒")
        except _ue.HTTPError as e:
            ok("空 params 被拒（400）", e.code == 400)
        try:
            api("/api/custom", {"params": {"optimizer": "adam", "lr": 9.9,
                                           "batch_size": 32}})
            raise AssertionError("FAIL: 超定义域参数应被拒")
        except _ue.HTTPError as e:
            ok("超定义域参数被拒（400 且说明校验原因）",
               e.code == 400 and "校验" in e.read().decode("utf-8", "replace"))
        try:
            api("/api/custom", {"params": {"lr": 0.001}})
            raise AssertionError("FAIL: 缺参数应被拒")
        except _ue.HTTPError as e:
            ok("缺参数被拒（validate_config 要求给全）", e.code == 400)
        ct = api("/api/custom", {"params": {"optimizer": "adam", "lr": 0.001,
                                            "batch_size": 32},
                                 "note": "web-smoke-human"})
        ok("空闲时提交即时派发执行（mode=executing）",
           ct["queued"] is True and ct["mode"] == "executing"
           and ct["cohort"], str(ct))
        sth = wait_idle()
        ok("人工试验子进程正常退出", sth["exit_code"] == 0, str(sth))
        tsh = api("/api/trials")
        hum = [t for t in tsh["trials"]
               if t["attrs"].get("note") == "web-smoke-human"]
        ok("人工试验落最新分区（custom 属性 + note 审计）",
           len(hum) == 1 and hum[0]["attrs"].get("custom") is True
           and hum[0]["state"] == "COMPLETE", str(hum))

        # 4) 试验日志下钻：列表带 has_log；详情端点返回全量 stdout/stderr
        ok("完结试验均有全量日志标记",
           all(t["has_log"] for t in tsh["trials"] if t["state"] == "COMPLETE"),
           str([(t["number"], t["state"], t["has_log"]) for t in tsh["trials"]]))
        n_h = hum[0]["number"]
        lg2 = api(f"/api/trials/{n_h}/log")
        ok("日志端点返回全量输出（头行 + 协议行 + 尾段）",
           lg2["trial"] == n_h and "=====" in lg2["text"]
           and "##TANSUO##" in lg2["text"] and "exit_code=" in lg2["text"],
           lg2["text"][:200])
        ok("未知试验日志 404", _expect_404("/api/trials/9999/log"))

        print("\nWeb 冒烟全部通过")
    finally:
        if proc and proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.terminate()

print(f"共 {PASS} 项断言")
