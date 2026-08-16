---
date: 2026-08-17
number: "024"
title: GPU 算力记账被两处舍入抹零（TRIAL_END round(1)、FINISH round(3)抹掉快试验），及毕业赛隔离复验与配置回写防逃逸的设计取舍
severity: medium
status: resolved
tags: [算力记账, 舍入精度, 毕业赛, 配置回写, gpu, 设计决策]
module: orchestrator / graduate / export_config / web 端点
---

# GPU 算力记账被两处舍入抹零，及毕业赛隔离复验与配置回写防逃逸的设计取舍

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 功能 4-7 批次——GPU 资源调度（探测 + `CUDA_VISIBLE_DEVICES` 注入）、成本感知（GPU·小时记账 + `budget.max_gpu_hours` 上限）、最优配置「毕业赛」（全量数据复验）、配置回写（best 参数合并进用户配置文件）
- **环境**：Windows 11，Python 3.12，Optuna（TPE），FastAPI + uvicorn Web 后端，React/Vite/TS 前端；开发机无 NVIDIA GPU（nvidia-smi 不存在）
- **当时在做什么**：实现 `orchestrator` 算力记账（`_charge(dt)` 多线程累加试验耗时 × slots）、续跑预热（新进程从 journal `TRIAL_END` 事件恢复算力累计）、`tansuo/graduate.py` 与 `tansuo/export_config.py` 两个新模块，并配套单测 `tests/test_gpu_cost.py`、`tests/test_graduate_export.py`
- **问题表现**：三类问题在测试阶段先后暴露：

  1. FINISH 事件的算力成本恒为 0.0——试验明明跑过了：

  ```
  AssertionError: FINISH 事件带算力成本  （实际 compute_hours == 0.0）
  ```

  2. 续跑预热断言失败——journal 里有完结试验，新进程 `compute_hours()` 却是 0.0：

  ```
  AssertionError: 续跑预热：新进程从 journal 恢复算力累计  （实际 0.0）
  ```

  3. 宿主环境变量干扰测试：`extra_env` 注入测试断言「未注入时子进程看不到 `CUDA_VISIBLE_DEVICES`」，但开发机宿主环境本身设过该变量，子进程从父进程继承，断言时过时不过。

  此外两个新功能自带风险点：毕业赛若直接往主 study 里加试验，会污染搜索记录（best/TPE 采样全被扰动）；配置回写若路径不校验，`../../` 目标可以改到项目外的任意文件。

- **影响范围**：记账为 0 会让 `budget.max_gpu_hours` 上限永远不触发（成本感知形同虚设）、前端成本展示恒 0；毕业赛不隔离会毒化主搜索；回写不防逃逸是安全事故
- **复现步骤**：1) 跑一轮秒级快试验后读 FINISH 事件 `compute_hours`；2) 新进程读 journal 预热后调 `compute_hours()`；3) 在设过 `CUDA_VISIBLE_DEVICES` 的宿主环境跑 env 注入测试

## T · 目标（Task）

- **要达成什么**：① 记账精度保住最小应计量的试验（亚秒级快试验也要累计）；② 毕业赛对主 study 零副作用；③ 配置回写只能碰项目目录内的文件，且写入前必备份
- **验收标准**：`test_gpu_cost.py` / `test_graduate_export.py` 全绿；「主 study 试验数在毕业赛后不变」成为断言；路径逃逸（`../` 或项目外绝对路径）返回 400；既有 15 个单测套件无回归
- **约束条件**：记账是近似口径（不做计费级精度）；不引入新依赖；前端要能看到算力与毕业赛结果（功能 4-7 的前端一并交付）

## A · 解决方案（Action）

### 排查过程

1. **FINISH 为 0**：先怀疑 `_charge` 没被调用——加内部断言发现 `_compute_seconds` 非零，问题只在 FINISH 事件的序列化：快试验 ~0.1s → 2.8e-5 GPU·小时，`round(x, 3)` 抹成 0.0。改 4 位仍是 0.0（2.8e-5 round 4 = 0.0000），最终改 6 位才保住。
2. **预热为 0**：`_seed_compute()` 逐条求和 journal `TRIAL_END` 的 `duration_s`——调试发现事件里 `duration_s` 全是 0.0。根因是 `TRIAL_END` 落盘时 `round(dt, 1)`：<0.05s 的试验直接抹零，journal 里的数据已经坏了，预热怎么算都是 0。**舍入伤害发生在数据落盘时，而不是读取时**——这是关键转折点，两处舍入是独立的两个 bug。
3. **宿主 CUDA 干扰**：env 注入测试最初断言「缺省路径子进程 CUDA_VISIBLE_DEVICES 为空」，宿主设过该变量时继承进来导致断言失败。放宽成「不强求为空」会漏掉覆盖逻辑的验证，于是换思路：不测「宿主没有」，改测「注入的确实到达」——专门脚本在 `TANSUO_DATA_FRACTION=='0.5'` 时返回固定值 107.0，精确断言注入通道生效，与宿主环境完全解耦。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| FINISH `round(compute_hours, 3)` | 失败 | 快试验 2.8e-5 h → 0.0，信息丢失 |
| 提到 4 位 | 失败 | 2.8e-5 四舍五入仍是 0.0000 |
| 提到 6 位 | 有效 | 2.8e-5 → 0.000028 保住；6 位对小时单位足够（微秒级才丢失） |
| `TRIAL_END duration_s` 保 `round(dt, 1)` | 失败 | <0.05s 试验落盘即 0，journal 数据永久损坏，预热无法恢复 |
| `round(dt, 2)` | 有效 | 10ms 精度对近似口径足够；续跑预热断言另用合成 3600s 事件保证确定性 |
| 断言「宿主无 CUDA_VISIBLE_DEVICES」 | 放弃 | 依赖开发机环境状态，CI/他人机器随时可能翻转 |
| 毕业赛直接写主 study（加 note 标记） | 放弃 | TPE 采样、best、剪枝统计全部被扰动，复验失败时还留脏数据 |
| 回写做深度递归合并 | 放弃 | 嵌套结构合并语义复杂、易出意外；顶层键覆盖/追加简单可预期 |

### 最终方案

1. **两处精度修复**（`tansuo/orchestrator.py`）：

   ```python
   # TRIAL_END 落盘：1 位 → 2 位（保住快试验）
   journal.append(TRIAL_END, ..., duration_s=round(dt, 2))
   # FINISH 事件：3 位 → 6 位（保住会话级小累计）
   journal.append(FINISH, ..., compute_hours=round(self.compute_hours(), 6))
   ```

   记账本体：`slots = len(gpus) if gpus else 1`；`_run_one` 拆成 wrapper + 实现，wrapper 用 `try/finally` 里 `self._charge(dt)` 保证剪枝/失败/异常试验同样计费；`budget.max_gpu_hours` 到点以 `finished_reason="compute_budget_exhausted"` 收尾。

2. **毕业赛隔离**（`tansuo/graduate.py`，新模块）：

   ```python
   iso = optuna.create_study(direction=direction)   # 纯内存，无 storage → 不碰主 study
   trial = iso.ask(...)
   runner = TrialRunner(settings, space, journal,
                        extra_env={"TANSUO_DATA_FRACTION": "1.0"})   # 强制全量数据
   result = runner.run_trial(trial, cfg_override=best_params, note="graduation")
   ```

   配套：训练轮数维度拉到空间上界（`adapter.iter_param` 显式声明优先，否则按 epoch/step/iter/round 关键词猜）；verdict 阈值 maximize 时 `value ≥ best×0.95`（minimize 对称 ×1.05）；结果落 `<data_dir>/reports/graduation.yaml` + journal `graduation` 事件，**失败也落盘**（status=failed + reason）。

3. **配置回写防逃逸**（`tansuo/export_config.py` + `tansuo/web/app.py`）：

   ```python
   cand = (p if p.is_absolute() else root / p).resolve()
   cand.relative_to(root)   # ValueError → 400「目标文件必须位于项目目录内」
   ```

   校验顺序刻意把路径逃逸检查放在「目标是否存在」之前（逃逸探测不泄漏项目外文件是否存在）；preview 只产出变更清单 + 合并全文绝不落盘；apply 先 `shutil.copy2` 备份 `<目标>.<后缀>.bak` 再写入；顶层同名键覆盖、异名键追加，不做递归合并。

4. **前端配套**：SettingsPage 新增 GPU 选卡区（无卡自动隐藏）与「成果交付」面板（毕业赛启动/结果卡 + 回写预览/确认）；DashboardPage「预算」卡并入算力行（`算力 X.XX / 上限 GPU·小时（N 卡）`）。

5. **graduate.py 自查修掉的两处毛刺**：遗留占位行 `iso.optimize(...) if False else None`（无意义执行路径）与成功分支重复且自相矛盾的 journal 双事件——合并为单事件 `journal.append(GRADUATION_EVENT, **payload)`。

## R · 实际效果（Result）

- **验证方式**：
  - `python tests/test_gpu_cost.py` 24 项断言全绿（含 max_gpu_hours=1e-6 触发 `compute_budget_exhausted`、SESSION_START gpus 审计、续跑预热 ≥1h 合成试验）
  - `python tests/test_graduate_export.py` 24 项断言全绿（含「主 study 未被污染（试验数不变）」「preview 不落盘」「备份内容=写入前原文」）
  - 15 个既有单测套件全量回归无回归；`tests/e2e_web_smoke.py` 新增 16-18 节（GPU 清单/选卡记账、毕业赛 409 互斥与 verdict、回写 preview/apply/逃逸 400）
  - `cd web && npm run build` 类型通过，dist 重新构建提交
- **前后对比**：FINISH compute_hours 从恒 0.0 → 快试验 0.000028 级别可计量；续跑预热从 0.0 → 正确恢复历史累计；env 注入测试从依赖宿主环境 → 完全确定
- **副作用与代价**：① journal 里 `TRIAL_END.duration_s` 精度从 1 位变 2 位，历史分区数据不变但新旧精度不一致（近似口径可接受）；② 毕业赛轮数拉满 + 全量数据，耗时可能远大于搜索期单试验，前端文案已提示；③ 回写只支持 YAML/JSON 顶层键合并，嵌套配置结构会被整体替换（文档已注明语义）
- **遗留问题与后续**：无。多机多卡的 slots 语义（跨节点）不在本期范围，当前 slots=所选卡数
- **经验教训**：
  1. **累加型计量的精度由最小应计量量决定**——`round(dt, 1)` 对「试验耗时」这个语义就是错的，因为快试验是合法输入；落盘前的舍入是数据损坏，读出来再修就晚了
  2. 测试断言不要依赖宿主环境状态（已设的 env、有没有 GPU），要么注入控制要么改成「注入值确实到达」的正向断言
  3. 带副作用的验证步骤（毕业赛、回写 apply）先问两个问题：失败路径落不落盘？能不能被路径/参数逃逸出沙箱？——这两问在本期各抓到一个真实缺陷
