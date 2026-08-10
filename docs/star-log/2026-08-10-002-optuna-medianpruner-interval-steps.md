---
date: 2026-08-10
number: "002"
title: Optuna 4.9 中 MedianPruner 参数 interval 改名为 interval_steps，旧写法 TypeError
severity: low
status: resolved
tags: [optuna, 版本兼容, pruner, API 变更]
module: tansuo/study.py（study 与剪枝器构造）
---

# Optuna 4.9 中 MedianPruner 参数 interval 改名为 interval_steps，旧写法 TypeError

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent，`tansuo/study.py` 的 `make_pruner`（构造中位数早停剪枝器）
- **环境**：Windows，Python 3.14.6，Optuna **4.9.0**
- **当时在做什么**：首次跑通 Optuna 主干（Phase 3），构造 `MedianPruner` 时按历史文档/记忆传参
- **问题表现**：

  ```
  TypeError: MedianPruner.__init__() got an unexpected keyword argument 'interval'
  ```

  报错位置（调用栈片段）：

  ```
      pruner_cfg.n_startup_trials,
      n_warmup_steps=pruner_cfg.n_warmup_steps,
      interval=1,
      ^^^^^^^^^^^
  ```

- **影响范围**：`cli.py run` 启动即崩，阻塞所有搜索运行
- **复现步骤**：1) Optuna 4.9.0；2) `optuna.pruners.MedianPruner(n_startup_trials=..., n_warmup_steps=..., interval=1)`；3) 100% 抛 TypeError

## T · 目标（Task）

- **要达成什么**：构造 MedianPruner 成功，剪枝逻辑可用
- **验收标准**：study 能正常创建并跑试验；剪枝生效（劣质试验提前终止）
- **约束条件**：不降级 Optuna 版本（4.9 是当前环境既定版本）

## A · 解决方案（Action）

### 排查过程

1. 历史记忆里 `MedianPruner` 的签名是 `(n_startup_trials=5, n_warmup_steps=0, interval=1, n_min_trials=1)`——`interval` 一直是合法参数。但 TypeError 明确说不认识 `interval`，说明 4.9 改了签名。
2. 用 `inspect.signature` 直接读当前安装版本的真实签名核实，确认参数已由 `interval` 改名为 `interval_steps`。

  ```
  python -c "import inspect, optuna; print(inspect.signature(optuna.pruners.MedianPruner.__init__))"
  ```

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 沿用 `interval=1` | 失败 | 4.9 已移除该参数名，TypeError |
| 改用 `interval_steps=1` | 有效，采用 | 与 4.9 实际签名一致 |

### 最终方案

`tansuo/study.py` 构造剪枝器处改用新参数名：

```python
optuna.pruners.MedianPruner(
    n_startup_trials=pruner_cfg.n_startup_trials,
    n_warmup_steps=pruner_cfg.n_warmup_steps,
    interval_steps=1,
)
```

## R · 实际效果（Result）

- **验证方式**：`cli.py run` 冒烟跑通；后续运行中剪枝正常触发（10 次运行里 3 次 PRUNED 提前终止）。
- **前后对比**：构造从抛 TypeError 到正常；`interval_steps=1` 与原意（每步都可比较）等价。
- **副作用与代价**：无。
- **遗留问题与后续**：无。
- **经验教训**：对第三方库的构造参数，不要依赖记忆中的旧签名，直接用 `inspect.signature` 以当前安装版本为准核实——这是本项目 README/注释里"以实测为准"原则的直接体现。
