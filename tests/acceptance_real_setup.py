"""真实 LLM 全流程验收：接入新代码库 → setup agent 起草配置 → 冒烟搜索。

与回归测试套件分离：需要真实 LLM 端点凭据（env ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN），会消耗 token 并真实跑训练，故不自动运行。用法：

  python tests/acceptance_real_setup.py --dir <项目目录> --train <训练脚本> [--trials 3]

流程（全程走 Web API，与界面操作等价）：
1. POST /api/projects   新建项目（自动脚手架 .tansuo/）并激活；
2. POST /api/projects/{id}/setup   setup agent 读训练脚本 → 起草 settings/space → 跑探针；
3. POST /api/run/start   冒烟搜索（带调参 agent 唤醒），核对完结数与最优值。
任一步失败打印诊断并以非零码退出。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8125
BASE = f"http://127.0.0.1:{PORT}"


def api(path, body=None, method=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fail(msg: str, log_tail: str = ""):
    print(f"\nFAIL: {msg}")
    if log_tail:
        print("---- 日志尾部 ----")
        print(log_tail[-3000:])
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="新项目目录（含训练代码/数据集）")
    ap.add_argument("--train", required=True, help="主训练脚本路径")
    ap.add_argument("--trials", type=int, default=3, help="冒烟搜索试验数")
    args = ap.parse_args()

    proj_dir = Path(args.dir).resolve()
    train = Path(args.train).resolve()
    if not proj_dir.is_dir():
        fail(f"项目目录不存在：{proj_dir}")
    if not train.is_file():
        fail(f"训练脚本不存在：{train}")

    tmp = Path(tempfile.mkdtemp(prefix="tansuo_accept_"))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "cli.py"), "web", "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ,
             "TANSUO_PROJECT_STORE": str(tmp / "projects.json")})
    try:
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                api("/api/health")
                break
            except OSError:
                if proc.poll() is not None:
                    fail(f"web 进程退出：{proc.stdout.read().decode('utf-8', 'replace')}")
                time.sleep(0.5)
        else:
            fail("web 服务 30s 未就绪")

        print("== 1. 新建项目（脚手架 .tansuo/） ==")
        cr = api("/api/projects", {"name": proj_dir.name, "dir": str(proj_dir),
                                   "train_script": str(train)})
        if not cr.get("scaffolded"):
            fail("预期 scaffolded=true（全新项目）", str(cr))
        if not (proj_dir / ".tansuo" / "settings.yaml").exists():
            fail(".tansuo/settings.yaml 未生成")
        print(f"  [ok] 项目已创建并激活：{cr['name']}（id={cr['id']}）")

        print("== 2. setup agent（真实 LLM 读代码起草配置） ==")
        api(f"/api/projects/{cr['id']}/setup", method="POST")
        t0 = time.time()
        st = None
        while time.time() - t0 < 1200:
            st = api("/api/setup/status")
            if st["exit_code"] is not None:
                break
            time.sleep(2)
        else:
            fail("setup 20 分钟未结束", api("/api/setup/log?tail=80")["text"])
        log_text = api("/api/setup/log?tail=400")["text"]
        if st["exit_code"] != 0:
            fail(f"setup 退出码 {st['exit_code']}", log_text)
        ev = api("/api/setup/events")
        kinds = {}
        for e in ev["events"]:
            kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
        print(f"  [ok] setup 完成（耗时 {time.time() - t0:.0f}s），事件分布：{kinds}")
        print(f"  [ok] setup 累计 tokens：{ev['tokens']['total_tokens']}")
        if not ev["events"]:
            fail("setup 事件流为空：setup_journal.jsonl 定位与写入位置不一致"
                 "（检查 _setup_journal_path 的 data_dir 绑定）")
        if ev["tokens"]["total_tokens"] <= 0:
            fail("setup 事件流 tokens=0：agent_token_summary 统计异常")
        if "===== 配置 agent 摘要 =====" in log_text:
            print("  ---- agent 摘要 ----")
            print("  " + log_text.split("===== 配置 agent 摘要 =====", 1)[1]
                  .strip().replace("\n", "\n  ")[:1500])

        settings_txt = (proj_dir / ".tansuo" / "settings.yaml").read_text(encoding="utf-8")
        space_txt = (proj_dir / ".tansuo" / "search_space.yaml").read_text(encoding="utf-8")
        print(f"  [ok] settings.yaml 已覆写（{len(settings_txt.splitlines())} 行），"
              f"search_space.yaml（{len(space_txt.splitlines())} 行）")
        if "data_dir" in settings_txt and ".tansuo" not in settings_txt.split("data_dir", 1)[1][:60]:
            print("  [warn] settings 的 data_dir 似乎不再指向 .tansuo/，隔离可能被破坏")

        print(f"== 3. 冒烟搜索（trials={args.trials}，带调参 agent） ==")
        api("/api/run/start", {"trials": args.trials, "wake_every": 2})
        t0 = time.time()
        rs = {}
        while time.time() - t0 < 7200:   # 校准后 timeout 可能较大，单次试验更久
            rs = api("/api/run/status")
            if not rs["running"]:
                break
            time.sleep(3)
        else:
            fail("搜索 2 小时未结束", api("/api/run/log?tail=80")["text"])
        if rs.get("exit_code") != 0:
            fail(f"搜索退出码 {rs.get('exit_code')}", api("/api/run/log?tail=80")["text"])
        # 运行日志必须落在项目 .tansuo 目录内（data_dir 隔离不被 setup 重写破坏）
        if ".tansuo" not in (rs.get("log_path") or ""):
            fail(f"运行日志未落在项目 .tansuo 目录：{rs.get('log_path')}"
                 "（settings 的 data_dir 可能被 setup 重写丢失）")
        s = api("/api/summary")
        c = s["counts"]
        print(f"  [ok] 搜索完成（耗时 {time.time() - t0:.0f}s）：完结 {c['completed']}、"
              f"剪枝 {c['pruned']}、失败 {c['failed']}")
        if s["best"]:
            print(f"  [ok] 最优 {s['primary']}={s['best']['value']}"
                  f"（trial#{s['best']['trial']}，参数 {s['best']['params']}）")
        ae = api("/api/agent/events")
        if ae["tokens"]["rounds"] > 0:
            print(f"  [ok] 调参 agent 唤醒 {ae['tokens']['rounds']} 轮，"
                  f"tokens {ae['tokens']['total_tokens']}")
        else:
            print("  [提示] 本次未触发调参 agent 唤醒（试验数少于 wake_every 或提前结束）")
        if c["completed"] + c["pruned"] == 0:
            fail("无任何试验完结", api("/api/run/log?tail=80")["text"])

        print("\n验收通过：新代码库接入 → setup agent 起草配置 → 冒烟搜索全链路正常")
        return 0
    finally:
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
