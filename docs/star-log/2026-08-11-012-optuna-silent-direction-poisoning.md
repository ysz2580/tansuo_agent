---
date: 2026-08-11
number: "012"
title: Optuna create_study(load_if_exists=True) 静默丢弃请求的 direction，改主指标方向续跑会让排序/剪枝/报告整体反向
severity: high
status: resolved
tags: [optuna, direction, 断点续跑, 记录分区, 数据污染]
module: tansuo/cohort.py · tansuo/study.py
---

# Optuna create_study(load_if_exists=True) 静默丢弃请求的 direction，改主指标方向续跑会让排序/剪枝/报告整体反向

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent（Optuna TPE + LLM 监督 agent 的超参数搜索框架），`tansuo/study.py` 的 study 工厂与断点续跑链路
- **环境**：Windows，Python 3.14.6（`C:\Python314\python.exe`），Optuna 4.9.0，存储 SQLite（`RDBStorage`）
- **当时在做什么**：设计「记录分区管理」（cohort）前审查断点续跑链路。`tansuo/study.py` 的 `create_or_load_study` 每次运行都把 settings 里的方向传给 `optuna.create_study(..., direction=direction, load_if_exists=True)`——study 名固定为 `"tansuo"`。疑点：如果用户把主指标从 `direction: maximize` 改成 `minimize`（或改了指标名）再续跑同一个库，Optuna 到底听谁的？
- **问题表现**：答案由 Optuna 源码给出——**请求的 direction 被静默丢弃，沿用库内方向**。`optuna/study/study.py` 第 1322-1333 行（4.9.0）：

  ```python
  storage = storages.get_storage(storage)
  try:
      study_id = storage.create_new_study(direction_objects, study_name)
  except exceptions.DuplicatedStudyError:
      if load_if_exists:
          assert study_name is not None

          _logger.info(
              f"Using an existing study with name '{study_name}' instead of creating a new one."
          )
          study_id = storage.get_study_id_from_name(study_name)
      else:
          raise
  ```

  `DuplicatedStudyError` 分支只按名字取回既有 study，`direction_objects` 从此再无人过问——没有比较、没有警告、没有异常。实测逐字输出（先以 maximize 建 study 写入 1 个试验，再请求 minimize 重载）：

  ```
  [I 2026-08-11 18:28:50,987] A new study created in RDB with name: tansuo
  [I 2026-08-11 18:28:51,023] Trial 0 finished with value: 0.5 and parameters: {}. Best is trial 0 with value: 0.5.
  [I 2026-08-11 18:28:51,047] Using an existing study with name 'tansuo' instead of creating a new one.
  首次创建 direction: 2 =  MAXIMIZE
  请求 minimize，实际 direction: 2 = int 2 MAXIMIZE
  best 排序依据是否反向: 0.5
  ```

  （`StudyDirection` 枚举：MINIMIZE=1、MAXIMIZE=2。唯一的提示是一行 `Using an existing study ...` INFO 日志，对"方向不一致"只字未提。）
- **影响范围**：一切"改了优化目标语义后续跑旧库"的场景全部静默污染——`study.best_trial` 按旧方向排序（val_loss 当 val_acc 排）、MedianPruner 按旧方向比较中间值、TPE 的好/坏样本划分按旧方向、报告的 top-k 全部反向。没有任何报错，用户只会得到一份"看起来正常但结论相反"的结果。对本项目尤其致命：agent 的决策（收窄空间、假设试验）也吃这份被污染的分析。
- **复现步骤**：1) `create_study(study_name="tansuo", storage=url, direction="maximize")` 并写入至少一个试验；2) 同一 storage 上 `create_study(study_name="tansuo", direction="minimize", load_if_exists=True)`；3) 读 `study.direction`——恒为 MAXIMIZE，100% 复现。

## T · 目标（Task）

- **要达成什么**：在分区（cohort）机制里彻底堵住"目标语义变化后混跑旧记录"的路径——宁可拒绝运行，也不允许静默污染
- **验收标准**：1) 主指标 name/direction（及影响可比性的 `data_fraction`）任一变化，自动模式必然新开分区，绝不续跑旧库；2) 用 `--cohort ID` 显式续跑目标不符的分区时**硬拒绝**（非零退出码 + 中文说明），而不是警告后照跑；3) 每个分区的 meta.yaml 留下指纹审计明细，事后可查；4) 有回归测试钉死"direction 不符 → 拒绝"这条语义
- **约束条件**：不改 Optuna 源码；守卫必须发生在 study 加载**之前**（加载后再检查为时已晚，且加载本身会给空分区凭空建库）；仅代码变化的场景要宽容（允许显式续跑、自动新开但要能改回恢复），不能一刀切拒绝一切指纹变化

## A · 解决方案（Action）

### 排查过程

1. 审查断点续跑链路时注意到 `create_or_load_study` 把 settings 方向当"请求"传入，但 `load_if_exists=True` 意味着续跑时这个请求可能不被采纳——Optuna 文档只说 "load if exists"，没提 direction 的处理。
2. 直接读 Optuna 4.9.0 源码 `optuna/study/study.py`：`create_new_study` 抛 `DuplicatedStudyError` 后的分支（1322-1333 行）只按 study_name 找回既有 study，请求的 `direction_objects` 被丢弃且无任何比较逻辑。确认这是**设计如此**而非 bug 报告能修的东西——`study_name` 是唯一键。
3. 写最小复现脚本（S 节实测），确认行为与源码一致：请求 minimize，实际方向仍是 MAXIMIZE，且唯一痕迹是一行无关痛痒的 INFO 日志。
4. 结论：Optuna 层指望不上，守卫必须建在我们自己的分区解析层。进一步意识到"目标语义"与"训练代码"是两种性质不同的变化——前者混跑必污染（方向反了），后者混跑只是"不可直接比较"（数据本身没错），所以不该用单一指纹一刀切。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 在 `create_or_load_study` 加载后比对 `study.direction` 与请求方向，不符就抛错 | 放弃 | 时机太晚：加载动作本身已发生，且该函数被 Web 查询路径复用（只读加载不应受运行守卫影响）；治标不治本——方向相同但指标名/data_fraction 变化同样不可比，单查 direction 拦不住 |
| 每个目标语义一个 study 名（把 direction 编进 study_name） | 放弃 | 碎片化存储、旧库仍要人肉对号入座，且绕开了"记录要按分区整体管理"的正题 |
| 单一指纹（代码+目标混算一个哈希），变了就拒绝续跑 | 放弃 | 把两种性质不同的变化混为一谈：改一行模型代码后想 `--cohort` 显式续跑旧分区（用户自担风险、数据本身有效）是合理需求，单一指纹下无法放行 |
| **双指纹分区**：objective_hash（目标语义）硬门槛 + code_hash（训练代码）软提示 | 有效，采用 | — |

### 最终方案

1. **新模块 `tansuo/cohort.py` 的双指纹**（`code_fingerprint`）：
   - `objective_hash` = 主指标 `name:direction` + `budget.data_fraction` 的 sha256 前 12 位——"目标语义"指纹；
   - `code_hash` = 训练代码内容指纹（subprocess 命令里的 `.py` token / `-m` 模块 / python entry 模块自动定位，另有 `experiment.fingerprint_paths` 显式补充）；
   - 两者连同逐文件 `fingerprint_inputs` 一起写进每个分区 `meta.yaml`，全程可审计。
2. **分区解析守卫**（`tansuo/cohort.py` `resolve_for_run`）——显式续跑时 objective 不符**硬拒绝**：

   ```python
   obj_match = cohort.meta.get("objective_hash") == fp.objective_hash
   ...
   if not obj_match:
       raise CohortError(
           f"拒绝续跑分区 {cohort_id}：优化目标已变化（{_objective_diff_reason(old, fp.objective_inputs)}）。\n"
           f"Optuna 加载既有 study 时会静默沿用库内方向，混跑会让排序/剪枝/报告全部失真。\n"
           f"请直接 `python cli.py run`（自动新开分区），旧分区记录会完整保留。")
   ```

   仅 `code_hash` 不符 → 打印警告但按用户指定继续（数据本身有效，只是不可与旧分区直接比较）。
3. **自动模式**：只续跑"双指纹均匹配"的最新分区；objective 变化 → 新开分区并打印具体变化项（如"主指标方向 maximize → minimize"），绝不静默混跑。
4. **CLI 退出码**（`cli.py` `cmd_run`）：`CohortError` → stderr 中文说明 + `return 2`，Web 的 `run_start` 同路径（FastAPI 404/400 呈现）。守卫在 study 加载之前执行，不匹配的旧库连打开都不会发生。
5. **回归测试钉死语义**（`tests/test_cohort.py`）：显式 `--cohort` + direction 不符 → `expect_error(CohortError)`；指标名、方向、data_fraction 各自单独变化 → objective_hash 必变；timeout/workers/agent 等运行参数变化 → 双指纹皆不变。

## R · 实际效果（Result）

- **验证方式**：1) `tests/test_cohort.py` 72 断言全绿，含"direction 不符硬拒绝"用例；2) CLI 冒烟：改 settings 主指标方向后 `run --cohort 0001-...` 实测退出码 2 + 中文拒绝说明；3) 全套回归 6 套件 192 断言（分区 72 / 空间护栏 34 / 条件空间 30 / 协议 12 / 权限降级 21 / 运行时 23）无回归；4) Web 冒烟 24 断言覆盖 `/api/runs` 可比性标注（objective-changed 分区标 ✘）
- **前后对比**：修复前——改主指标方向续跑，Optuna 静默沿用旧方向，best/剪枝/报告/agent 决策全部反向且零提示；修复后——自动模式必新开分区（旧记录完整保留），显式混跑直接拒绝运行（退出码 2），每个分区的 meta.yaml 可事后审计当时的双指纹
- **副作用与代价**：改主指标后无法再"接着旧库跑"——这是刻意设计（混跑结论必错，没有合法用例）；`runs` 列表里旧分区会被标 ✘ 不可比，用户仍可随时查看历史
- **遗留问题与后续**：`create_or_load_study` 内未再加加载后 direction 断言（cohort 守卫已前置拦截）；若未来出现绕过 cohort 直接调 study 工厂的调用方，值得补一条加载后断言做纵深防御
- **经验教训**：1) `load_if_exists=True` 这类"便利开关"的语义边界必须读源码确认——它静默丢弃的不只 direction，凡是"创建时参数"（sampler/pruner 除外，那些加载时可覆盖）都可能不生效；2) 框架层的宽容（"已有就复用"）对上层可能是陷阱，防御要建在自己的边界上而不是指望依赖库报错；3) 区分变化的性质再定策略：目标语义变化（硬拒绝）与训练代码变化（软提示）一刀切处理都会伤及合理用例——这也是双指纹而非单指纹设计的由来
