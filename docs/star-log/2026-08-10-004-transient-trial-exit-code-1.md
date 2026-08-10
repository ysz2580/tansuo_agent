---
date: 2026-08-10
number: "004"
title: trial#8 训练脚本瞬时退出码 1（stderr 为空），单独复现正常，判定环境瞬时故障
severity: medium
status: open
tags: [子进程, 瞬时故障, 容错, 诊断, windows]
module: tansuo/runner.py + tansuo/adapter.py（试验执行）
---

# trial#8 训练脚本瞬时退出码 1（stderr 为空），单独复现正常，判定环境瞬时故障

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent，`tansuo/runner.py`（试验执行）/ `tansuo/adapter.py`（子进程适配器），演示任务 MNIST 小 CNN（`demo/train_mnist.py`）
- **环境**：Windows，Python 3.14.6，torch 2.13（纯 CPU，`torch.set_num_threads(8)`），Optuna 4.9.0
- **当时在做什么**：Phase 5 带 agent 的 10 次试验正式运行（agent 每 5 次唤醒）。运行整体顺利，唯独第 9 个试验失败：

  ```
  [9/10] trial#8 FAILED  训练脚本退出码 1 | best=0.9808 (34.0s)
  ```

  journal 事件（`demo/data/journal.jsonl`）原文：

  ```json
  {"kind": "trial_start", "ts": "2026-08-10 17:17:07", "trial": 8, "params": {"optimizer": "sgd", "lr": 0.045922307785474056, "scheduler": "step", "batch_size": 128, "weight_decay": 0.00703785878096277, "dropout": 0.012774747405271736, "augment": "affine", "width": 8, "epochs": 4, "seed": 8}}
  {"kind": "trial_fail", "ts": "2026-08-10 17:17:40", "trial": 8, "reason": "训练脚本退出码 1", "hint": "查看下方 stderr 尾部定位脚本错误", "detail": "(stderr 为空)", "source": "search"}
  ```

- **影响范围**：该试验记 FAILED、浪费一次预算；搜索本身未中断（后续 trial#9 正常完成）——恰好验证了"FAILED 不中断搜索"的设计
- **复现步骤**：**无法稳定复现**。用 journal 中记录的 trial#8 完整配置（含 seed=8）单独手动运行同一脚本：退出码 0，正常产出 `val_acc=0.9767`。出现频率：本次 10 次运行中 1 次；此后的运行未再出现（样本量小，不能断言绝迹）

## T · 目标（Task）

- **要达成什么**：弄清 trial#8 失败原因；若查不明，确保系统对这类瞬时失败有容错与足够的诊断信息
- **验收标准**：1) 若可复现则修复；2) 若不可复现，失败诊断信息足够下次定位（退出码 + stderr + stdout 尾部都留痕）
- **约束条件**：不引入重量级进程监控；不能因一次瞬时失败中断整轮搜索

## A · 解决方案（Action）

### 排查过程

1. **看现场**：journal 显示失败原因"训练脚本退出码 1"，detail 是"(stderr 为空)"——脚本崩了但什么都没留下。失败发生在启动后约 33 秒（17:17:07 → 17:17:40），即训练中途而非启动阶段，说明进程拉起、配置读取、协议行打印前期都正常。
2. **精确复现**：从 journal 取出 trial#8 的完整参数（含 seed=8，决定数据抽样、权重初始化、dropout 的全部随机性），单独手动运行 `demo/train_mnist.py`——退出码 0，val_acc=0.9767。同配置可复现正常 ⇒ 排除配置本身的问题。
3. **排除法**：该配置中 lr≈0.046 的 SGD + step 调度不属于会发散的组合（发散会打印 NaN 协议行并走剪枝路径，而非退出码 1）；脚本没有任何会主动 `sys.exit(1)` 的路径不打 stderr。结论指向**进程层瞬时故障**（资源抢占、Windows 侧瞬时异常等），无法从应用层进一步取证。
4. **转向加固**：既然根因不可达，把力气花在"下次发生时能留下更多线索 + 系统不受影响"。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 同配置（含 seed）手动复现 | 正常退出 | 证明不是确定性 bug，是瞬时问题 |
| 追查 torch/CPU 层根因 | 放弃 | 无堆栈、无 stderr，无取证抓手；成本不划算 |
| 失败自动重试 | 未做 | 会掩盖真实脚本错误、污染统计；记 FAILED 让用户/agent 知情更符合设计 |

### 最终方案

1. **容错走既有设计**：FAILED 试验记录后搜索继续（本次实际验证：trial#8 失败后 trial#9 照常跑完，最终 best=0.9808 不受影响）。
2. **加强失败诊断**（`tansuo/runner.py`）：失败 detail 同时带上 stderr 尾部与 stdout 尾部（此前只有 stderr），hint 补充引导：

   ```python
   raise TrialFailedError(
       f"训练脚本退出码 {result.exit_code}",
       hint="查看下方 stderr/stdout 尾部定位脚本错误（若都为空，多为环境瞬时问题，重试即可）",
       detail=(f"stderr: {result.stderr_tail or '(空)'} | "
               f"stdout 尾部: {result.stdout_tail[-3:] or '(空)'}"))
   ```

3. 审计留痕：journal 的 `trial_fail` 事件完整记录 reason/hint/detail，报告（`report.md`）统计失败次数。

## R · 实际效果（Result）

- **验证方式**：同配置手动复现退出码 0（val_acc=0.9767）；10 次运行中该失败未中断搜索、最终结果正确；诊断改动后的失败路径在测试中以构造的失败脚本验证过（stderr/stdout 尾部均出现在错误信息里）。
- **前后对比**：失败信息从"退出码 1 + (stderr 为空)"增强为"退出码 + stderr 尾部 + stdout 尾部 + 指向性 hint"；下次同类瞬时失败可更快排除脚本自身问题。
- **副作用与代价**：无。
- **遗留问题与后续**：**根因未找到**（status=open）。若未来高频复现，下一步取证方向：子进程包装器记录进程退出前的资源状况、或在训练脚本顶层加全局异常钩子把未捕获异常强制写入 stderr。
- **经验教训**：1) "退出码非零 + stderr 为空"在 Windows 子进程场景下真实存在，失败诊断必须 stdout/stderr 双通道留痕；2) journal 记录完整试验参数（含 seed）是本次能精确复现的关键——审计日志在排查期的价值不亚于运行期；3) 搜索框架把单次试验失败当常态而非异常来设计（记 FAILED、继续搜索），瞬时故障的破坏面就被限制在一次预算内。
