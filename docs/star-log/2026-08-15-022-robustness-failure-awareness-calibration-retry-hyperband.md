---
date: 2026-08-15
number: "022"
title: 真实验收暴露的四块 robustness 短板补齐：调参 agent 失败原因感知、超时校准泛化、瞬时重试默认开启、Hyperband 剪枝
severity: medium
status: resolved
tags: [agent-robustness, 失败感知, 超时校准, 失败重试, hyperband, 剪枝, 设计决策]
module: tansuo/analysis.py + agent/skills/tune.py + agent/prompts.py + config.py + study.py + agent/skills/config.py + wizard.py
---

# 真实验收暴露的四块 robustness 短板补齐：调参 agent 失败原因感知、超时校准泛化、瞬时重试默认开启、Hyperband 剪枝

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 的调参 agent 与配置层（`tansuo/analysis.py`、`agent/skills/tune.py`、`agent/prompts.py`、`config.py`、`study.py`、`agent/skills/config.py`、`wizard.py`）。
- **环境**：Windows 11，Python 3.14.6，optuna 4.9.0（`HyperbandPruner(min_resource, max_resource, reduction_factor)`），torch 2.13.0+cpu。
- **当时在做什么**：上一轮真实 LLM 验收（STAR #021）通过后复盘「项目还需要什么改进」，对照代码核实出四块短板，本轮逐一落地。
- **问题表现**（逐条核实，非臆测）：
  1. **调参 agent 看不到失败原因**：`tansuo/analysis.py::summarize()` 只统计 `failed: N` 计数（`analysis.py:111`），不回失败原因。上一轮验收第一跑 3/3 全超时，agent 只输出「本轮保持巡航」——它根本不知道失败是超时。失败原因其实写在 journal 的 `trial_fail` 事件里（含 `reason`/`hint`），但 agent 的 `get_study_summary` 读不到。
  2. **超时校准只认 epoch**：`agent/skills/config.py::_calibrate_timeout` 用 `"epoch" in p.name.lower()` 定位训练轮数维度，训练脚本用 step/iter/round 命名就漏校准。
  3. **瞬时重试机制存在但默认关闭**：核实发现重试早在提交 cf09e3d（"失败重试"）就实现了（`runner.py` 按 `adapter.retry_on_fail` 循环、`trial_retry` 事件、`test_runtime_features.py::test_retry` 全覆盖），但 `AdapterCfg.retry_on_fail` 默认 `0`，新建配置默认不享受保护。
  4. **剪枝只有 median**：`config.py:244` 硬校验 `pruner.type == "median"`，深度学习常见的 Hyperband（epochs 跨度大时更省预算）没有入口。
- **影响范围**：短板 1 让 agent「不如人」（人类调参工程师看到成片超时会立刻收空间/提 timeout）；短板 2/3 让非 epoch 命名脚本、新接入项目在真实搜索里更易成片翻车；短板 4 限制剪枝策略选型。
- **复现步骤**：1) 任意配置跑一次会让试验失败的搜索（如训练脚本 `sys.exit(2)`）；2) agent 唤醒后调 `get_study_summary`——返回 JSON 里只有 `counts.failed`，无原因；3) 对照 journal 里确有 `trial_fail` 事件带 `reason`。

## T · 目标（Task）

- **要达成什么**：补齐四块短板——agent 能按失败类别（timeout/exit_code/protocol/unexpected）采取不同应对；超时校准泛化到 step/iter/round 且可显式指定维度；瞬时重试默认开启；pruner 增加 hyperband 选项。
- **验收标准**：四项各有单测；全量回归（12 单测套件 + CLI 冒烟 + Web 冒烟）绿；不改变既有 demo/CLI 用户行为。
- **约束条件**：确定性优先（失败类别判定、校准折算是代码而非提示词）；不动前端（四者均为后端/agent 行为，前端无硬编码 pruner/retry UI，已 grep 确认）；保留既有测试断言的键名与字面量（如 `recommended_timeout_s`、提示词「监督者 agent」「总预算 N 次试验」）。

## A · 解决方案（Action）

### 排查过程

1. 先核实每项的真实现状再动手，避免重复造轮子：`grep` + 读源码确认 `summarize()` 不含失败原因、`_calibrate_timeout` 只认 epoch、`make_pruner` 只返回 `MedianPruner`。
2. 关键转折：查 `retry_on_fail` 历史（`git log -S retry_on_fail`）发现重试机制**早已实现**（cf09e3d），只是默认 0——所以第 3 项不是"实现"而是"默认开启 + 核实"。这避免了重写已有逻辑。
3. 失败原因的数据源定位：Optuna study 不记录失败原因，唯一事实源是 journal 的 `trial_fail` 事件；`TuneExecutor` 恰好持有 `self.journal`（`self.orch.journal`），注入点就在 `_tool_get_study_summary`。
4. Hyperband 可行性核实：读 optuna 4.9 `HyperbandPruner` 源码确认 `max_resource="auto"` 在冷启动（无完结试验）时 `_try_initialization` 提前 `return`、不剪枝，安全；`make_pruner` 返回类型从 `MedianPruner` 放宽为 `BasePruner`。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 把失败原因塞进 `summarize()` 签名 | 放弃 | `summarize` 被 report/web/tune 三处以 `(study, settings)` 调用，改签名波及面大；失败原因来自 journal 而非 study，应保持 `summarize` 纯 study 语义 |
| 重试从零实现 | 放弃 | 核实后发现 cf09e3d 已实现并全覆盖测试，重写是重复造轮子；只需把默认 0 改 1 |
| 超时校准靠提示词提醒 agent 识别 step/iter | 放弃 | LLM 不可靠（同 STAR #021 教训）；校准是确定性代码，扩名字线索 + 显式字段才稳 |
| Hyperband 强制要求显式 max_resource | 放弃 | optuna 支持 `auto` 且冷启动安全，强制显式会抬高接入门槛；改为 `auto` 默认 + 文档提示 widen 场景显式给值 |

### 最终方案

1. **失败原因感知（#1）**
   - `tansuo/analysis.py`：新增 `failure_category(reason)`（超时/退出码/协议/未预期/其它 → timeout/exit_code/protocol/unexpected/other）与 `recent_failures(journal, limit=5)`（读 journal `trial_fail`，返回 `[{trial, category, reason, hint}]`，无失败返回 `[]`）。
   - `tansuo/agent/skills/tune.py::_tool_get_study_summary`：`summarize` 结果之外追加 `out["recent_failures"] = recent_failures(self.journal)`（仅在有失败时带此键）；工具描述同步说明。
   - `tansuo/agent/prompts.py` `tuning_system` 新增「失败处置（硬性纪律）」节：按 category 给出应对——成片 timeout → narrow 收耗时维度/建议提 timeout_s；连续 exit_code 非瞬时 → 停止烧预算、提示查脚本；偶发且注明"已自动重试" → 环境噪声无需处理；PRUNED 属正常。保留测试依赖的字面量（「监督者 agent」「总预算 {{total_trials}} 次试验」、无残留 `{{`）。
2. **超时校准泛化（#2）**：`agent/skills/config.py` 新增模块级 `_ITER_KEYWORDS = ("epoch","step","iter","round")` 与 `_find_iter_param(space, explicit)`（优先 `adapter.iter_param` 显式指定，否则按关键词优先级自动识别数值型且有上界的参数）；`_calibrate_timeout` 改用它，info 键从 `probe_epochs/space_max_epochs` 泛化为 `iter_param/probe_iter/space_max_iter`（测试依赖的 `recommended_timeout_s/capped/action/warning` 键名不变）。`config.py::AdapterCfg` 增 `iter_param: str = ""`。
3. **瞬时重试默认开启（#3）**：`config.py` `AdapterCfg.retry_on_fail` 默认 `0→1`，`load_settings` 默认值同步。重试逻辑本身（runner.py）不动——已正确区分瞬时（非零退出码且 stderr 为空且未超时）与确定性失败。
4. **Hyperband 剪枝（#5）**：`config.py::PrunerCfg` 增 `min_resource/max_resource/reduction_factor`；`load_settings` 校验 `type ∈ {median, hyperband}`，hyperband 时 `min_resource≥1`、`reduction_factor≥2`、`max_resource` 为正整数或 `"auto"` 且整数时须 > min_resource；`study.py::make_pruner` 按 type 分支返回 `HyperbandPruner`/`MedianPruner`，返回类型放宽为 `BasePruner`。
5. **模板/文档同步**：`wizard.py` SETTINGS_TEMPLATE 更新（retry_on_fail 默认 1、新增 iter_param 注释、pruner 段补 hyperband 三参数说明）；setup 提示词校准措辞从「空间最大 epochs」泛化为「空间最大训练轮数（可用 adapter.iter_param 指定）」。

## R · 实际效果（Result）

- **验证方式**：新增/扩展单测 + 全量回归。
  - `test_setup_guard.py` 16 → **20**（新增 `test_calibrate_iter_param`：step/iter 自动识别、显式 iter_param、无轮数维度 ratio=1 不报错）。
  - `test_runtime_features.py` 29 → **46**（新增 `test_hyperband_pruner`：配置校验/工厂返回类型/真实 15 试验搜索全完结；`test_failure_awareness`：recent_failures 还原、五类分类、`get_study_summary` 注入、无失败不带键）。
  - 全量回归：12 单测套件 **399 项断言**全绿（cohort 116 / runtime 46 / space_patch 34 / notify 32 / conditional_space 30 / compare 28 / prompts 28 / guardrails 21 / setup_guard 20 / project_store 16 / warmstart 16 / protocol 12），CLI 冒烟 **31** 项、Web 冒烟 **82** 项全绿；`cli.py init` 生成的模板经 `load_settings` 解析通过（pruner.type=median、retry=1）。
- **前后对比**：
  - agent 失败可见性：`get_study_summary` 从「只有 failed 计数」→「附 recent_failures（trial/category/reason/hint）」，配合提示词纪律，成片超时时 agent 有了明确的收空间/提 timeout 指引，不再只会"保持巡航"。
  - 超时校准：从仅识别 `epoch` → 覆盖 epoch/step/iter/round 且可 `adapter.iter_param` 显式指定。
  - 重试：新配置默认 `retry_on_fail=1`，瞬时环境噪声（STAR #004 那类退出码 1+空 stderr）自动兜底，无需用户手动开启。
  - 剪枝：`pruner.type` 增 `hyperband` 选项，epochs 跨度大的搜索可用逐层晋级省预算。
- **副作用与代价**：1) 失败处置纪律使 tuning_system 提示词变长（仍在模板长度限额内）；2) `retry_on_fail` 默认 1 意味着确定性失败（如脚本 bug 退出码非零且 stderr 空）会多跑一次才报错，代价是单次失败试验多一倍耗时，但错误信息已注明"已自动重试 1 次"；3) Hyperband `max_resource="auto"` 依据早期完结试验推断总资源，若 agent 后续大幅 widen epochs，推断值可能偏小（文档已提示此场景显式给 max_resource）。
- **遗留问题与后续**：1) 失败处置目前靠提示词引导 agent，尚无"检测到成片超时即自动收空间"的确定性护栏（可作为下一步，类比 #021 的超时校准护栏思路）；2) STAR #004 的瞬时退出码 1 根因仍未定位（重试是兜底而非根治）；3) Hyperband 与动态空间编辑（agent widen epochs）的组合在长程搜索下的表现尚待真实验收检验。
- **经验教训**：1) **实现前先核实是否已存在**——四项里有一项（重试）早已实现，盲目"实现"会重复造轮子甚至引回归；`git log -S <符号>` 是低成本核实手段；2) **失败原因这类"过程信息"在 journal 而非 study**，给 agent 补感知能力时先定位事实源再选注入点；3) 改默认值（retry 0→1）前 grep 全部显式设置该字段的配置/测试，确认影响面（demo 已显式 `retry_on_fail: 1`，测试均显式传值，故安全）；4) 动提示词前先查提示词测试断言了哪些字面量，避免破坏既有契约。
