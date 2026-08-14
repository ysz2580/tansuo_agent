"""webhook 通知测试：build_payload 四种格式 / send_webhook 兜底与真实收发 /
notify_finish·notify_degrade 消息内容与开关门控 / settings 校验与 ${ENV:...} /
write_back_notify 最小化写回（可直接 `python tests/test_notify.py` 运行）。

覆盖：
- build_payload：generic/slack={text}、dingtalk={msgtype,text.content}、
  lark={msgtype,content.text}
- send_webhook：本地 http.server 捕获真实 POST（JSON 原样送达）；连接拒绝 /
  空 URL 均返回 {"ok": False} 而不抛异常（通知失败绝不影响搜索）
- notify_finish：消息含实验名/原因/进度/分区/最优/token 累计；
  enabled=False / 未订阅 session_end / webhook_url 空 → 静默跳过不发送
- notify_degrade：订阅 agent_degrade 时发送，消息注明降级
- load_settings：notify.format / notify.events 非法值拒绝；合法块解析；
  webhook_url 的 ${ENV:...} 展开
- write_back_notify：显式字段最小化写回、注释保留、未给出字段不动、
  缺失字段行报错
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tansuo.config import ConfigError, load_settings           # noqa: E402
from tansuo.notify import (VALID_EVENTS, VALID_FORMATS,        # noqa: E402
                           build_payload, notify_degrade, notify_finish,
                           send_webhook, write_back_notify)

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


# ----------------------------------------------------------------------
# 本地捕获服务器：记录每一笔 POST 的 JSON body
# ----------------------------------------------------------------------

class _Capture(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        _Capture.received.append({"path": self.path, "body": json.loads(body)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode": 0}')

    def log_message(self, *_):   # 静默
        pass


def start_capture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    _Capture.received.clear()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/robot"


def fake_settings(url: str, *, enabled: bool = True, fmt: str = "generic",
                  events=None):
    return SimpleNamespace(
        experiment_name="notify_test_exp",
        notify=SimpleNamespace(enabled=enabled, webhook_url=url, format=fmt,
                               events=events if events is not None
                               else ["session_end", "agent_degrade"]))


# ----------------------------------------------------------------------
def test_build_payload() -> None:
    print("== build_payload 四种格式 ==")
    ok("generic = {text}", build_payload("generic", "hi") == {"text": "hi"})
    ok("slack = {text}", build_payload("slack", "hi") == {"text": "hi"})
    ok("dingtalk = msgtype+text.content",
       build_payload("dingtalk", "hi") == {"msgtype": "text",
                                           "text": {"content": "hi"}})
    ok("lark = msgtype+content.text",
       build_payload("lark", "hi") == {"msgtype": "text",
                                       "content": {"text": "hi"}})


def test_send_webhook(capture_url: str) -> None:
    print("== send_webhook 真实收发 + 兜底 ==")
    payload = build_payload("dingtalk", "【tansuo】测试消息：中文与符号 !@#")
    r = send_webhook(capture_url, payload)
    ok("本地服务器收到 POST 且返回 ok", r["ok"] and "HTTP 200" in r["detail"], str(r))
    ok("收到的 JSON 与发出的一致（中文无损）",
       len(_Capture.received) == 1 and _Capture.received[0]["body"] == payload,
       str(_Capture.received))

    # 兜底：连接拒绝 / 空 URL 都不抛异常，只报 ok=False
    r2 = send_webhook("http://127.0.0.1:1/hook", {"text": "x"}, timeout=2)
    ok("连接拒绝时返回 ok=False 不抛异常", r2["ok"] is False, str(r2))
    r3 = send_webhook("", {"text": "x"})
    ok("空 URL 返回 ok=False 不抛异常", r3["ok"] is False and "空" in r3["detail"])


def test_notify_finish(capture_url: str) -> None:
    print("== notify_finish 消息内容 + 门控 ==")
    s = fake_settings(capture_url, fmt="dingtalk")
    tokens = {"rounds": 2, "input_tokens": 300, "output_tokens": 40,
              "total_tokens": 340}
    sent = notify_finish(s, reason="budget_exhausted", finished=10, total=10,
                         best_line="trial#7 val_acc=0.9821", tokens=tokens,
                         cohort_id="0001-20260814-abc", log=lambda *_: None)
    ok("notify_finish 发送成功", sent is True)
    msg = _Capture.received[-1]["body"]["text"]["content"]
    ok("消息含实验名与结束原因",
       "notify_test_exp" in msg and "budget_exhausted" in msg, msg)
    ok("消息含进度 / 分区 / 最优 / token 累计",
       "10/10" in msg and "0001-20260814-abc" in msg
       and "trial#7" in msg and "340" in msg, msg)

    n0 = len(_Capture.received)
    ok("enabled=False 静默跳过",
       notify_finish(fake_settings(capture_url, enabled=False),
                     reason="x", finished=0, total=1, log=lambda *_: None)
       is False and len(_Capture.received) == n0)
    ok("未订阅 session_end 静默跳过",
       notify_finish(fake_settings(capture_url, events=["agent_degrade"]),
                     reason="x", finished=0, total=1, log=lambda *_: None)
       is False and len(_Capture.received) == n0)
    ok("webhook_url 为空静默跳过",
       notify_finish(fake_settings(""), reason="x", finished=0, total=1,
                     log=lambda *_: None) is False)
    ok("settings 无 notify 属性也安全跳过",
       notify_finish(SimpleNamespace(experiment_name="x"),
                     reason="x", finished=0, total=1,
                     log=lambda *_: None) is False)


def test_notify_degrade(capture_url: str) -> None:
    print("== notify_degrade ==")
    s = fake_settings(capture_url, fmt="lark")
    sent = notify_degrade(s, detail="连续失败阈值：3 次", log=lambda *_: None)
    ok("订阅 agent_degrade 时发送成功", sent is True)
    body = _Capture.received[-1]["body"]
    ok("lark 信封 + 消息注明降级与阈值",
       body["msgtype"] == "text" and "降级" in body["content"]["text"]
       and "3 次" in body["content"]["text"], str(body))
    n0 = len(_Capture.received)
    ok("未订阅 agent_degrade 静默跳过",
       notify_degrade(fake_settings(capture_url, events=["session_end"]),
                      log=lambda *_: None) is False
       and len(_Capture.received) == n0)


SETTINGS_HEAD = ("metrics:\n  primary: {name: val_acc, direction: maximize}\n"
                 "adapter:\n  mode: subprocess\n"
                 '  command: ["python", "x.py"]\n')


def test_settings_validation(tmp: Path) -> None:
    print("== settings.notify 校验与 ENV 展开 ==")

    def load(yaml_body: str, name: str):
        p = tmp / f"{name}.yaml"
        p.write_text(yaml_body, encoding="utf-8")
        return load_settings(p)

    try:
        load(SETTINGS_HEAD + "notify: {format: wechat}\n", "notify_bad_fmt")
        raise AssertionError("FAIL: 非法 format 应被拒绝")
    except ConfigError as e:
        ok("非法 format 被拒并注明合法取值",
           "format" in str(e) and "/".join(VALID_FORMATS) in str(e), str(e))
    try:
        load(SETTINGS_HEAD + "notify: {events: [trial_end]}\n", "notify_bad_ev")
        raise AssertionError("FAIL: 非法 events 应被拒绝")
    except ConfigError as e:
        ok("非法 events 元素被拒并注明合法取值",
           "trial_end" in str(e) and "/".join(VALID_EVENTS) in str(e), str(e))

    s = load(SETTINGS_HEAD + "notify:\n  format: dingtalk\n"
             "  events: [session_end]\n  enabled: false\n", "notify_ok")
    ok("合法 notify 块解析",
       s.notify.format == "dingtalk" and s.notify.events == ["session_end"]
       and s.notify.enabled is False, str(s.notify))

    # 缺省块 → 默认值；events 默认订阅两类事件
    s2 = load(SETTINGS_HEAD, "notify_absent")
    ok("无 notify 块时用默认值（enabled + 两事件）",
       s2.notify.enabled and s2.notify.format == "generic"
       and s2.notify.events == ["session_end", "agent_degrade"])

    os.environ["TANSUO_TEST_WEBHOOK"] = "http://127.0.0.1:9/hook"
    try:
        s3 = load(SETTINGS_HEAD + "notify:\n"
                  "  webhook_url: ${ENV:TANSUO_TEST_WEBHOOK}\n", "notify_env")
        ok("webhook_url 的 ${ENV:...} 展开",
           s3.notify.webhook_url == "http://127.0.0.1:9/hook",
           s3.notify.webhook_url)
    finally:
        os.environ.pop("TANSUO_TEST_WEBHOOK", None)


def test_write_back(tmp: Path) -> None:
    print("== write_back_notify 最小化写回 ==")
    original = (SETTINGS_HEAD +
                "notify:\n"
                "  enabled: true            # 总开关\n"
                "  webhook_url: ${ENV:DINGTALK_WEBHOOK:}   # 机器人地址\n"
                "  format: generic\n"
                "  events: [session_end, agent_degrade]\n")
    p = tmp / "settings_wb.yaml"
    p.write_text(original, encoding="utf-8")

    r = write_back_notify(str(p), webhook_url="https://oapi.dingtalk.com/robot?access_token=abc",
                          fmt="dingtalk", events=["session_end"], enabled=False)
    text = p.read_text(encoding="utf-8")
    ok("四个字段全部写回", r["ok"] and sorted(r["changed"])
       == ["enabled", "events", "format", "webhook_url"], str(r))
    ok("webhook_url 明文写入（含特殊字符自动加引号）",
       'webhook_url: "https://oapi.dingtalk.com/robot?access_token=abc"' in text, text)
    ok("format/events/enabled 写回", "format: dingtalk" in text
       and "events: [session_end]" in text and "enabled: false" in text, text)
    ok("注释保留", "# 总开关" in text and "# 机器人地址" in text)

    # 未给出的字段一律不动（尤其 webhook_url 留空 → 保持 ${ENV:...}）
    p.write_text(original, encoding="utf-8")
    r2 = write_back_notify(str(p), fmt="lark")
    text2 = p.read_text(encoding="utf-8")
    ok("只写显式给出的字段", r2["changed"] == ["format"], str(r2))
    ok("${ENV:...} 引用原样保留",
       "webhook_url: ${ENV:DINGTALK_WEBHOOK:}" in text2)

    # 缺失字段行 → 报错而不是乱写
    r3 = write_back_notify(str(p), webhook_url="https://x.example/hook")
    ok("webhook_url 行被 ENV 覆盖后仍在则正常写回", r3["ok"], str(r3))
    p2 = tmp / "settings_no_notify.yaml"
    p2.write_text(SETTINGS_HEAD, encoding="utf-8")
    r4 = write_back_notify(str(p2), fmt="slack")
    ok("文件中无 format 行时报错不写盘",
       r4["ok"] is False and r4["errors"] and "format" in r4["errors"][0], str(r4))
    r5 = write_back_notify(str(p2))
    ok("无字段可写时报错", r5["ok"] is False and r5["errors"], str(r5))


if __name__ == "__main__":
    import tempfile
    server, capture_url = start_capture()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_build_payload()
        test_send_webhook(capture_url)
        test_notify_finish(capture_url)
        test_notify_degrade(capture_url)
        test_settings_validation(tmp)
        test_write_back(tmp)
    server.shutdown()
    print(f"\n全部通过：{PASS} 项断言")
