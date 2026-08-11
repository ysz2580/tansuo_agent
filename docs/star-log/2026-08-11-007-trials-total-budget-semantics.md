---
date: 2026-08-11
number: "007"
title: cli.py run --trials 语义是「总预算」而非「本次新增次数」，Web 界面直接透传会让搜索立即结束或少跑
severity: medium
status: resolved
tags: [cli, 语义, web后端, 预算]
module: tansuo/web/app.py · cli.py
---

# cli.py run --trials 语义是「总预算」而非「本次新增次数」，Web 界面直接透传会让搜索立即结束或少跑

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent Web 后端运行驱动（`tansuo/web/app.py` 的 `run_start`）
- **环境**：Optuna 4.9.0；study 持久化在 SQLite，断点续跑是默认行为
- **当时在做什么**：实现「开始搜索」端点，前端输入框的语义是"**本次再跑 N 次试验**"，后端把它透传给 `python cli.py run --trials N`
- **问题表现**（代码审查阶段发现的潜在故障，未上线）：`cli.py run` 内部是 `self.total = total_trials`——orchestrator 用「总预算 − 已完成数」算还要跑几次。例如 study 已完成 10 次时：
  - 界面想"再跑 2 次" → 传 `--trials 2` → orchestrator 认为预算 2 ≤ 已完成 10，**一次不跑直接退出**；
  - 界面想"再跑 20 次" → 传 `--trials 20` → 实际只跑 10 次
- **影响范围**：Web「开始搜索」行为与用户预期完全不符（少跑甚至不跑）；CLI 本身语义没错，问题在两个语义的衔接
- **复现步骤**：1) 准备一个已完成 ≥1 次试验的 study；2) 直接 `python cli.py run --trials 1`；3) 进程立即正常退出，不跑任何新试验

## T · 目标（Task）

- **要达成什么**：界面上的 N（新增次数）正确换算成 CLI 的总预算
- **验收标准**：study 已有 F 次已结束试验时，界面「跑 N 次」实际新增 N 次试验
- **约束条件**：不改 CLI 语义（「总预算」对断点续跑场景是正确的）；换算必须与孤儿清理的顺序正确配合

## A · 解决方案（Action）

### 排查过程

1. 实现 `run_start` 时重读 `cli.py run` 与 orchestrator 的预算逻辑，确认 `--trials` 覆盖的是 `budget.total_trials`（总量），而不是增量。
2. 确认"已结束"的口径：Optuna 中占用过预算的试验状态是 COMPLETE / PRUNED / FAIL 三种（RUNNING、WAITING 不算）。
3. 发现一个顺序依赖：换算 `finished` 之前必须先清理孤儿 RUNNING（见记录 005）——被强杀遗留的 RUNNING 试验不在三种结束态里，会让 `finished` 少算，换算出的总预算偏小。

### 最终方案

`tansuo/web/app.py` 的 `run_start`：先兜底清理孤儿，再换算：

```python
if trials_arg is not None:
    # cli --trials 语义是「总预算」，界面语义是「本次新增 N 次试验」——换算成总量
    from optuna.trial import TrialState
    finished = len(study.get_trials(
        deepcopy=False,
        states=(TrialState.COMPLETE, TrialState.PRUNED, TrialState.FAIL)))
    trials_arg = finished + trials_arg
```

注意 `get_trials(deepcopy=False, states=(...))` 必须显式给出三种结束态，缺一种换算就偏。

## R · 实际效果（Result）

- **验证方式**：端到端实测——study 已有 10 次已结束试验时，界面「跑 2 次」，子进程实际参数为 `--trials 12`，运行结束后试验数 10→12
- **前后对比**：修复前同样的输入会直接退出（0 次新试验）；修复后新增次数与输入一致
- **副作用与代价**：无；CLI 直接使用者不受影响
- **遗留问题与后续**：无
- **经验教训**：给既有 CLI 做 Web/UI 封装时，每个参数都要核对语义再透传，尤其是「预算/配额/上限」这类天然存在"总量 vs 增量"歧义的参数；两种语义都合理，必须在衔接层显式换算并写注释说明换算方向
