---
date: 2026-08-14
number: "017"
title: 参数重要度与参数-取值关系图的设计取舍，及 optuna 默认重要度评估器依赖 sklearn 的隐性陷阱
severity: low
status: resolved
tags: [设计决策, optuna, 参数重要度, 可视化, 依赖陷阱]
module: analysis / report / agent prompts / web 前端
---

# 参数重要度与参数-取值关系图的设计取舍，及 optuna 默认重要度评估器依赖 sklearn 的隐性陷阱

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent（Optuna TPE + LLM 监督 agent 的超参调优系统）；
  涉及 `tansuo/analysis.py`、`tansuo/report.py`、`tansuo/agent/skills/tune.py`、
  `tansuo/agent/prompts.py`、前端 `web/src/components/`（两个新图表组件）与
  `web/src/pages/DashboardPage.tsx`
- **环境**：Windows 11，Python 3.14，Optuna 4.9.0（未安装 scikit-learn），
  React + Vite + TS + recharts 3.10.1；上游三项用户优化刚交付
  （commit `11e02d0`，STAR #016）
- **当时在做什么**：从「帮助研究员探索超参数」视角复盘——系统只回答"谁好"
  （best / top-k / 学习曲线 / 收敛信号），不回答"为什么好——哪个超参在起作用、
  取值与指标是什么关系"。用户选定两项补齐：①参数重要度排序，②参数-取值关系图
- **影响范围**：纯增量功能；但功能 ① 首次落地即踩中 optuna 的隐性依赖：
  新单测 first-run 失败 `AssertionError: FAIL: 2 参数 6 试验：两键均存在 {}`——
  `param_importances` 兜底返回了空 dict，说明 `get_param_importances` 抛了被吞的异常
- **复现步骤**：任意 study（≥2 完成试验）调
  `optuna.importance.get_param_importances(study)`（不传 evaluator）即触发

## T · 目标（Task）

- **要达成什么**：
  1. **参数重要度**：后端一个函数 + `summarize` 加一键，agent 工具、`/api/summary`、
     Markdown 报告三处消费方自动透出；前端横向条形图。
  2. **参数-取值关系图**：任选一个参数，散点展示其取值与主指标的关系（数值参数
     数值轴、choice 参数类别轴、最优试验高亮）。
- **验收标准**：`tests/test_compare.py` 新增 `test_param_importances`（2 参数 6 试验
  断言两键均存在、值域 [0,1]、和≈1.0；1 试验 → `{}`）；e2e_web_smoke 断言
  `/api/summary` 含 importances 字段、报告含「参数重要度」段；功能 ② 靠
  `npm run build`（类型与编译）；全部既有套件零回归。
- **约束条件**：零新依赖（不引入 scikit-learn）；不加新后端端点；不改
  `build_context_tuning_system(settings, space)` 签名（提示词不加新 `{{var}}`
  占位符，避免 Web 预览路径 `_preview_context` 连带改动）；分析层任何失败
  不得炸掉汇总（重要度算不出时降级为"无数据"展示）。

## A · 解决方案（Action）

### 排查过程 / 设计取舍

1. **重要度走 summarize 单键透出，而不是新端点**：`summarize(study, settings)`
   是唯一聚合点，被 agent 工具 `get_study_summary`（`{**s}` 展开）、
   `/api/summary`（直接返回加附加字段）、`report.py` 三处共用。加一个
   `"importances"` 键 = 三处全部自动可见，零额外接线、零新增维护面。
2. **关系图复用 `/api/trials`，不新增后端改动**：该端点本就返回每条试验的
   `params + value`，前端直接提取"参数维 vs 指标维"。条件空间下参数只在部分
   试验出现 → 参数清单取完成试验 params 键的并集，选某参数时只画含它的试验。
3. **提示词不加 `{{var}}` 占位符**：改在 `tuning_system` 步骤 1 的既有句式里提
   一句 importances 的用法（高影响维度聚焦、低影响维度可冻结），工具描述加
   "参数重要度"。新增占位符会迫使 `build_context_tuning_system` 与 Web 预览
   上下文同步加键——为一个语义上"由工具返回值承载"的信息不值得。
4. **轮询提升**：DashboardPage 里 `ProgressWithTrials` 原本自持
   `usePolling(api.trials, 8000)`；给关系图再挂一路同端点轮询就是双倍请求。
   把 trials 轮询提升到页面级，两个图表共用一份数据。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| `get_param_importances(study)` 默认评估器 | **出 bug** | 默认 fANOVA 依赖 sklearn（未安装），ImportError 被兜底吞成 `{}`，见下 |
| 给空重要度附 `"_note"` 文案键 | 放弃 | dict 类型契约是 `{参数名: float}`，混入字符串键会污染前端图表与 agent JSON 解读 |
| 引入 scikit-learn 依赖 | 放弃 | 为一个可选分析特征装整个 sklearn（及其传递依赖）与本项目"轻依赖"原则冲突 |
| 关系图新开 `/api/param_relation` 端点 | 放弃 | `/api/trials` 已有全部数据，新端点只是重复搬运 |
| 提示词加 `{{importances}}` 占位符 | 放弃 | 签名扩散（上下文构建 + Web 预览两处连带）；重要度随试验数变化，静态注入不如工具按需读取 |

### 实现中暴露的真实 bug：optuna 默认重要度评估器依赖 sklearn

新单测 first-run 即红：

```
AssertionError: FAIL: 2 参数 6 试验：两键均存在 {}
```

兜底 `try/except Exception: return {}` 把真实异常吞了，写个诊断脚本把异常放出来：

```
ModuleNotFoundError: No module named 'sklearn'
  File ".../optuna/importance/_fanova/_fanova.py", line 28, in <module>
    from sklearn.ensemble import RandomForestRegressor
ImportError: Tried to import 'sklearn' but failed. Please make sure that the
package is installed correctly to use this feature. Actual error: No module
named 'sklearn'.
```

根因：`optuna.importance.get_param_importances` 不传 evaluator 时默认构造
`FanovaImportanceEvaluator`，fANOVA 实现用 sklearn 的 RandomForestRegressor；
sklearn 是 optuna 的**可选**依赖（extras），本项目没装。optuna 4.9 内置三个
评估器里 `MeanDecreaseImpurityImportanceEvaluator` 同样依赖 sklearn，唯有
**`PedAnovaImportanceEvaluator`（v3.6+，实验性）只依赖 numpy**（随 optuna 自带）。
诊断验证：6 试验 2 参数（value 主要由 lr 驱动）返回
`{'lr': 0.904344, 'dropout': 0.095656}`、和=1.0，方向完全正确；1 试验时如期抛
`ValueError: Cannot evaluate parameter importances with only a single trial.`
（守卫已覆盖）。

修复（`tansuo/analysis.py::param_importances`）：显式传入 PED-ANOVA 评估器，
并用 `warnings.catch_warnings()` 压掉其实验性 API 警告：

```python
from optuna.importance import PedAnovaImportanceEvaluator
with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # 实验性 API 警告不打扰调用方
    return optuna.importance.get_param_importances(
        study, evaluator=PedAnovaImportanceEvaluator())
```

### 最终方案落点

1. **功能 ①**：`analysis.param_importances(study)` 三级守卫（完成 <2 → `{}`，
   >500 → `{}` 封顶计算开销，全异常兜底 `{}`）→ `summarize` 返回加
   `"importances"` 键 → 报告在「参数分布对比」后新增「## 参数重要度」段
   （降序列表，空则注明"完成试验过少或过多，无法计算"）→ agent 工具描述与
   `tuning_system` 步骤 1 各一句话轻触 → 前端 `ImportanceChart.tsx`
   （recharts 横向 BarChart，按值降序，空数据提示）→ DashboardPage 底部
   新增「参数重要度」卡片（数据取自已轮询的 summary，无新轮询）。
2. **功能 ②**：前端 `ParamRelationChart.tsx`（组件内 Select 选参数 +
   ScatterChart；数值参数 type="number"、字符串参数 type="category" +
   `allowDuplicatedCategory={false}`；最优试验红点 Cell 高亮；自定义 Tooltip
   显示 trial#/参数值/指标值；无完成试验或条件参数无样本给提示文案）→
   DashboardPage 底部新增「参数-取值关系」卡片，trials 轮询从
   ProgressWithTrials 提升到页面级共用。

## R · 实际效果（Result）

- **验证方式**：
  - `tests/test_compare.py` 28 断言（22 旧 + 6 新：两键存在 / [0,1] 值域 /
    和≈1.0 / summarize 透出一致 / 1 试验 → `{}` / 空重要度不炸）；
  - 10 个单测套件全绿共 346 断言（分区 116 / 对比 28 / 条件空间 30 / 权限降级 21 /
    通知 32 / 提示词 28 / 协议 12 / 运行时 29 / 空间护栏 34 / 热启动 16）；
  - `tests/e2e_cli_smoke.py` 31 断言；`tests/e2e_web_smoke.py` 57 断言
    （新增 2：summary 含 importances dict、报告含「参数重要度」段）；
  - `npm run build` 通过
- **前后对比**：研究员现在能回答"哪个参数真正起作用"（重要度排序）与
  "取值怎么影响指标"（散点探索）；agent 的 get_study_summary 同步获得重要度
  信息用于空间编辑决策；单测断言总数 340 → 346
- **副作用与代价**：无回归（后端仅增量一键 + 报告一段，前端新增两组件）；
  PED-ANOVA 是 optuna 实验性 API（v3.6+），接口若未来变化只需换评估器一行；
  试验 >500 时跳过重要度计算是接受的边界（报告如实注明）
- **遗留问题与后续**：关系图无抖动（jitter），choice 参数同取值多点会重叠——
  样本量小可接受，样本大了再议
- **经验教训**：
  1. **optuna 的"默认"常藏可选依赖**：importance 三评估器两个依赖 sklearn，
     不装时默认路径直接 ImportError——用 optuna 可选特性前先查实现依赖，
     或像本次让兜底 except 的测试先跑起来暴露问题；
  2. **"吞异常兜底"必须配套 first-run 诊断**：本次正是 `{}` 兜底让 bug 以
     断言失败而非堆栈的形式出现，一个 5 行诊断脚本（临时文件跑真实异常）
     即可定位——PowerShell 5.1 传参吞引号，诊断代码务必写文件不写 `-c`；
  3. **聚合单点加键 > 新端点**：summarize 三处消费方共享一个返回结构，
     加一键的接线成本是零，新增端点则后端路由/前端类型/轮询三处全要动；
  4. **提示词模板变量克制**：信息能由工具返回值承载就不加 `{{var}}`——
     占位符会扩散到上下文构建、Web 预览、PROMPT_VARS 校验三处。
