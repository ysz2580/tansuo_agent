"""Markdown 报告 + best.yaml 导出。

报告内容：最优配置、top-k、参数对比、参数重要度、空间演化时间线、agent 决策摘要、
watch 指标走势、收敛信号。
"""
from __future__ import annotations

import time
from pathlib import Path

import yaml

from .analysis import completed_trials, convergence_hint, param_contrast, ranked, summarize
from .journal import (AGENT_ERROR, AGENT_TOOL_CALL, AGENT_WAKEUP, FINISH, SPACE_PATCH,
                      Journal)


def _fmt_params_inline(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _fmt_contrast(contrast: dict) -> list[str]:
    lines = []
    for name, c in contrast.items():
        if c["kind"] == "choice":
            top = ", ".join(f"{v}×{n}" for v, n in sorted(c["top"].items()))
            bot = ", ".join(f"{v}×{n}" for v, n in sorted(c["bottom"].items()))
            lines.append(f"- **{name}**（choice）：top25% 取值 {top} ｜ bottom25% 取值 {bot}")
        else:
            lines.append(f"- **{name}**（数值）：top25% 中位数 {c['top_median']:g}"
                         f"（范围 {c['top_range'][0]:g}~{c['top_range'][1]:g}）｜"
                         f" bottom25% 中位数 {c['bottom_median']:g}"
                         f"（范围 {c['bottom_range'][0]:g}~{c['bottom_range'][1]:g}）")
    return lines


def generate_report(settings, study, space, journal: Journal,
                    out_dir: str | Path | None = None,
                    cohort_info: dict | None = None) -> tuple[Path, Path]:
    """生成 report.md 与 best.yaml，返回两者路径。

    cohort_info（可选）：{"id", "fingerprint", "note"}，写入报告头部作为
    可比性证据；缺省 None 时输出与旧版一致。
    """
    out_dir = Path(out_dir) if out_dir else Path(settings.data_dir) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = settings.metrics.primary
    s = summarize(study, settings, top_k=8)
    rk = ranked(study)
    events = journal.load_events()

    md: list[str] = []
    md.append(f"# 超参数搜索报告：{settings.experiment_name}")
    md.append("")
    md.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"- 主指标：**{primary.name}**（{primary.better}）")
    md.append(f"- 观测指标：{', '.join(m.name for m in settings.metrics.watch) or '（无）'}")
    if cohort_info:
        fp = cohort_info.get("fingerprint") or ""
        md.append(f"- 记录分区：{cohort_info.get('id')}"
                  + (f"（代码指纹 {fp[:8]}）" if fp else "")
                  + (f" ｜ 备注：{cohort_info['note']}" if cohort_info.get("note") else ""))
    c = s["counts"]
    md.append(f"- 试验统计：完成 {c['completed']} ｜ 剪枝 {c['pruned']} ｜ "
              f"失败 {c['failed']} ｜ 进行中 {c['running']}")
    md.append(f"- 收敛信号：{s['convergence']}")
    md.append("")

    # ---------- 最优配置 ----------
    md.append("## 最优配置")
    md.append("")
    if s["best"] is None:
        md.append("（没有完成的试验）")
    else:
        best = None
        for t in rk:
            if t.number == s["best"]["trial"]:
                best = t
                break
        md.append(f"**trial#{s['best']['trial']}，{primary.name} = {s['best']['value']:.4f}**")
        md.append("")
        md.append("```yaml")
        md.append(yaml.safe_dump(s["best"]["params"], allow_unicode=True,
                                 sort_keys=False).rstrip())
        md.append("```")
        md.append("")
        curve = (best.user_attrs.get("curve") or [None])[-1] if best else None
        if curve:
            md.append("最终一个 epoch 的指标：")
            md.append("")
            md.append("| 指标 | 值 |")
            md.append("|---|---|")
            for k, v in curve.items():
                if k != "epoch":
                    md.append(f"| {k} | {v:.4f} |" if isinstance(v, float) else f"| {k} | {v} |")
            md.append("")

    # ---------- top-k ----------
    md.append("## Top 试验")
    md.append("")
    if s["top_k"]:
        md.append(f"| trial | {primary.name} | 配置 |")
        md.append("|---|---|---|")
        for row in s["top_k"]:
            md.append(f"| #{row['trial']} | {row['value']:.4f} | {_fmt_params_inline(row['params'])} |")
        md.append("")

    # ---------- 参数对比 ----------
    if s["contrast"]:
        md.append("## 参数分布对比（top25% vs bottom25%）")
        md.append("")
        md += _fmt_contrast(s["contrast"])
        md.append("")

    # ---------- 参数重要度 ----------
    md.append("## 参数重要度")
    md.append("")
    if s["importances"]:
        md.append("按 Optuna PED-ANOVA 重要度评估（已归一化，各参数之和≈1），"
                  "值越大对主指标影响越大：")
        md.append("")
        for name, imp in sorted(s["importances"].items(),
                                key=lambda kv: kv[1], reverse=True):
            md.append(f"- **{name}**：{imp:.3f}")
        md.append("")
    else:
        md.append("（完成试验过少或过多，无法计算参数重要度）")
        md.append("")

    # ---------- 空间演化 ----------
    patches = [e for e in events if e.get("kind") == SPACE_PATCH]
    md.append("## 搜索空间演化")
    md.append("")
    md.append(f"当前空间版本：v{space.version}（{space.free_param_count()} 个自由参数 / "
              f"{len(space.params) - space.free_param_count()} 个冻结）")
    md.append("")
    if patches:
        for e in patches:
            ops = "; ".join(f"{o.get('op')}({o.get('param')}: {o.get('to', o.get('value', ''))})"
                            for o in (e.get("ops") or []))
            md.append(f"- [{e.get('ts')}] → v{e.get('version')}：{ops}")
            md.append(f"  - 理由：{e.get('rationale')}")
    else:
        md.append("- （本次会话没有空间编辑，搜索在初始空间内完成）")
    md.append("")

    # ---------- agent 决策摘要 ----------
    wakes = [e for e in events if e.get("kind") == AGENT_WAKEUP]
    tool_call_events = [e for e in events if e.get("kind") == AGENT_TOOL_CALL]
    tool_calls = [e.get("tool") for e in tool_call_events]
    denied_calls = [e.get("tool") for e in tool_call_events if e.get("allowed") is False]
    agent_errors = [e for e in events if e.get("kind") == AGENT_ERROR]
    finishes = [e for e in events if e.get("kind") == FINISH]
    md.append("## Agent 决策摘要")
    md.append("")
    if wakes or tool_calls:
        md.append(f"- 唤醒 {len(wakes)} 轮，工具调用 {len(tool_calls)} 次"
                  + (f"，其中权限拒绝 {len(denied_calls)} 次（{', '.join(denied_calls)}）"
                     if denied_calls else "")
                  + (f"，异常 {len(agent_errors)} 次" if agent_errors else ""))
        from collections import Counter
        cnt = Counter(tool_calls)
        if cnt:
            md.append("- 工具使用：" + "，".join(f"{k}×{v}" for k, v in cnt.most_common()))
        for e in wakes:
            note = e.get("note") or e.get("summary")
            if note:
                md.append(f"  - [{e.get('ts')}] {note}")
    else:
        md.append("- （本次会话未启用 agent 或 agent 未被唤醒）")
    if finishes:
        md.append(f"- 结束原因：{finishes[-1].get('reason')}")
    md.append("")

    # ---------- watch 指标走势（最优试验） ----------
    if rk:
        md.append("## 观测指标走势（最优试验逐 epoch）")
        md.append("")
        curve = rk[0].user_attrs.get("curve") or []
        watch_names = [m.name for m in settings.metrics.watch]
        cols = [w for w in watch_names if any(w in row for row in curve)]
        if curve and cols:
            md.append("| epoch | " + " | ".join(cols) + " |")
            md.append("|---" * (len(cols) + 1) + "|")
            for row in curve:
                vals = []
                for w in cols:
                    v = row.get(w)
                    vals.append(f"{v:.4f}" if isinstance(v, float) else str(v))
                md.append(f"| {row.get('epoch')} | " + " | ".join(vals) + " |")
            md.append("")
        # 平均单 epoch 耗时
        times = [row.get("epoch_time_s") for t in completed_trials(study)
                 for row in (t.user_attrs.get("curve") or [])
                 if isinstance(row.get("epoch_time_s"), (int, float))]
        if times:
            md.append(f"全部试验平均单 epoch 耗时：{sum(times) / len(times):.1f}s")
            md.append("")

    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    # ---------- best.yaml ----------
    best_path = out_dir / "best.yaml"
    if s["best"] is not None:
        best = next(t for t in rk if t.number == s["best"]["trial"])
        payload = {"trial": best.number, "value": best.value,
                   "primary_metric": primary.name,
                   "params": dict(best.params)}
        curve = best.user_attrs.get("curve") or []
        if curve:
            payload["final_epoch_metrics"] = curve[-1]
        if best.user_attrs.get("note"):
            payload["note"] = best.user_attrs["note"]
    else:
        payload = {"trial": None, "value": None, "params": {}}
    best_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
    return report_path, best_path
