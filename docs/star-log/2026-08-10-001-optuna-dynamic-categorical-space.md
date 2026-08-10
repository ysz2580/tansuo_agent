---
date: 2026-08-10
number: "001"
title: Optuna storage 拒绝动态修改分类参数候选集，set_choices（收窄 choices）设计整体作废
severity: high
status: resolved
tags: [optuna, 搜索空间, 动态空间, TPE, 设计决策]
module: tansuo/space.py（搜索空间补丁引擎）
---

# Optuna storage 拒绝动态修改分类参数候选集，set_choices（收窄 choices）设计整体作废

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 智能超参数调节 agent，`tansuo/space.py` 的搜索空间补丁引擎（`apply_patch`）
- **环境**：Windows，Python 3.14.6，Optuna **4.9.0**，sqlalchemy 2.0.51（RDBStorage/SQLite；早期验证用 InMemoryStorage，两层 storage 行为一致）
- **当时在做什么**：Phase 1 验证搜索空间补丁引擎。设计中的补丁操作有 5 种：`narrow / widen / set_choices / freeze / release`。其中 `set_choices` 用于收窄**分类参数**（choice）的候选集——例如 agent 观察到 cosine 调度器全面占优后，把 scheduler 从 `[none, cosine, step]` 收窄为 `[none, cosine]`。写测试脚本验证"同参数名下动态改变 choices"是否可行。
- **问题表现**：在已有试验（scheduler 已按 3 个 choices 注册过）之后，新试验用收窄后的 choices 调 `trial.suggest_categorical`，试验直接失败：

  ```
  [W 2026-08-10 15:57:20,921] Trial 4 failed with parameters: {'lr': 0.0017041266654359867} because of the following error: ValueError('CategoricalDistribution does not support dynamic value space.').
  Traceback (most recent call last):
    File "C:\Users\夜月\AppData\Roaming\Python\Python314\site-packages\optuna\study\_optimize.py", line 206, in _run_trial
      value_or_values = func(trial)
    File "E:\tansuo_agent\tests\test_space_patch.py", line 205, in objective
      cfg["scheduler"] = trial.suggest_categorical("scheduler", ["none", "cosine"])
    ...
    File "C:\Users\夜月\AppData\Roaming\Python\Python314\site-packages\optuna\storages\_in_memory.py", line 205, in set_trial_param
      distributions.check_distribution_compatibility(
          self._studies[study_id].param_distribution[param_name], distribution
      )
    File "C:\Users\夜月\AppData\Roaming\Python\Python314\site-packages\optuna\distributions.py", line 656, in check_distribution_compatibility
  ```

  报错点在 `storage.set_trial_param` → `check_distribution_compatibility`：**参数第一次出现时注册的分布被永久钉死**，之后同名参数传入不同的 CategoricalDistribution 一律拒绝。数值分布（float/int）没有此限制，low/high 可以自由收窄放宽——只有分类分布是硬约束。

- **影响范围**：阻塞了 agent"聚焦分类参数"这一核心能力的设计。若无法收窄 choices，TPE 会继续把预算花在明显劣质的取值上。
- **复现步骤**：1) 建 study，跑一个试验 `suggest_categorical("scheduler", ["none","cosine","step"])`；2) 再跑一个试验 `suggest_categorical("scheduler", ["none","cosine"])`；3) 第 2 个试验 100% 以 `ValueError('CategoricalDistribution does not support dynamic value space.')` 失败。

## T · 目标（Task）

- **要达成什么**：给 agent 一个对 storage 完全安全、可在搜索中途执行的"聚焦分类参数"手段
- **验收标准**：聚焦操作后新试验正常 suggest、历史试验不受影响、断点续跑（RDBStorage 重载）后行为一致；违规操作被护栏明确拒绝而不是运行期崩溃
- **约束条件**：不能要求用户清库重来（断点续跑是一等需求）；不能给已完成试验补写参数（见下）；TPE 按参数名建模，参数名不能随便改

## A · 解决方案（Action）

### 排查过程

1. 先看报错栈：拦截发生在 storage 层的分布兼容性检查，而不是 sampler 层。说明这不是"换个采样器"能绕过的，是存储格式层面的硬约束。
2. 读 Optuna 源码确认：`check_distribution_compatibility` 对数值分布只要求类型一致（区间可变），对 `CategoricalDistribution` 要求 choices 完全相同。
3. 转向绕路思路：既然同名不能改 choices，那给收窄后的参数换个新名字？随即发现连锁问题——TPE 按参数名建模，换名等于历史经验清零；且要让历史可比，需要给**已完成**的旧试验补写新名参数，于是实测补写。
4. 补写实测失败（见下表方案 3），至此所有在"动态 choices"框架内的绕路全部封死，结论锁定：**Optuna 4.x 下同名分类参数的 choices 不可变是硬约束，set_choices 必须作废，换机制**。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| set_choices：同名收窄 choices | 失败 | storage 拒绝：`ValueError('CategoricalDistribution does not support dynamic value space.')`（见上） |
| 换参数名承载新 choices | 放弃 | TPE 按名建模，换名即历史失效；且引出的补写问题见下行 |
| 给已完成试验补写参数（`set_trial_param`） | 失败 | `optuna.exceptions.UpdateFinishedTrialError: Trial#1 has already finished and can not be updated.`（栈：`set_trial_param` → `optuna\storages\_base.py` line 619 `check_trial_is_updatable`）。已完成的试验不可再改 |
| freeze：聚焦改为"固定到某个取值" | 有效，采用 | 冻结参数不走 `suggest`，直接把常量注入配置，storage 层完全不感知变化，天然兼容 |

### 最终方案

1. **废弃 set_choices**：`tansuo/space.py` 的 `VALID_OPS` 从 `(narrow, widen, set_choices, freeze, release)` 收窄为 `(narrow, widen, freeze, release)`；`apply_patch` 收到 `set_choices` 直接返回结构化错误（提示改用 freeze）。
2. **聚焦分类参数统一走 freeze**：freeze 记录 `frozen: <取值>`（哨兵用 `None` 表示未冻结，避免与 0/False 等合法冻结值冲突，且 deepcopy 安全）；`suggest(trial)` 跳过冻结参数，由 `inject()` 把常量写进配置。分类分布保持 envelope 原样，storage 永远看到同一个 CategoricalDistribution。
3. **数值参数不受影响**：narrow/widen 只动 low/high，动态安全（实测通过）。
4. 同步更新：tune 技能的工具 schema 与 system prompt（分类聚焦只能 freeze）、README FAQ（"为什么不能改分类参数的候选集？"）、`tests/test_space_patch.py` 用例。

## R · 实际效果（Result）

- **验证方式**：`tests/test_space_patch.py` 34 项断言全过（含 freeze/release、envelope 护栏、非法 op 拒绝）；后续 10 次带 agent 的正式运行（SQLite 断点续跑）无任何分布兼容性报错；agent 侧在 prompt 约束下未再尝试 set_choices。
- **前后对比**：设计期发现并改道，未流入正式运行；代价是分类参数只能"定点冻结"不能"收窄到子集"——但实践中 agent 观察到某个取值占优时，freeze 到该取值正是想要的语义，能力损失可忽略。
- **副作用与代价**：冻结比收窄更激进（完全排除其余取值）；靠护栏"自由参数 ≥3"与 release 可逆来对冲误冻结。
- **遗留问题与后续**：无。若未来需要"收窄到子集"的中间态，只能走"新 study + 迁移历史"，成本不值当，不做。
- **经验教训**：1) 任何"每试验动态 suggest"的设计，**分类分布必须按不可变对待**——这是 Optuna 文档不显眼、storage 层才暴露的约束；2) 设计空间编辑能力前先写最小复现脚本捅一遍 storage 边界，比先写完整实现再发现便宜得多；3) 护栏拒绝要返回结构化错误+替代方案提示（"改用 freeze"），让 LLM 能自我修正而不是卡死。
