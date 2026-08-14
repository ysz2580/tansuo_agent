---
date: 2026-08-15
number: "021"
title: 真实 LLM 全流程验收暴露 setup 三重缺陷：搜索空间未校准超时致全试验超时、settings 重写丢 .tansuo 隔离路径、setup 花费不可见
severity: high
status: resolved
tags: [setup-agent, 项目管理, 超时校准, 配置重写, token审计, 真实验收]
module: tansuo/agent/skills/config.py + tansuo/agent/prompts.py + tansuo/agent/loop.py + tansuo/web
---

# 真实 LLM 全流程验收暴露 setup 三重缺陷：搜索空间未校准超时致全试验超时、settings 重写丢 .tansuo 隔离路径、setup 花费不可见

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 的项目管理工作流（STAR #020 刚落地的「新建/打开项目 + setup agent Web 化」），本次用真实 LLM 端点（DashScope `qwen3-max`）做端到端验收：新建项目 → setup agent 读训练脚本起草配置 → 冒烟搜索。
- **环境**：Windows 11，Python 3.14.6，torch 2.13.0+cpu（纯 CPU），MNIST 小 CNN（`demo/train_mnist.py` 拷进全新项目目录）；LLM 走 `ANTHROPIC_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic`。
- **当时在做什么**：跑 `tests/acceptance_real_setup.py`（隔离注册表 + 真 Web 服务 + 真 LLM），验证「agent 读一个新代码库并跑超参数探索」整条链路。
- **问题表现**：第一跑冒烟搜索 3 次试验**全部超时**，搜索以 0 完结收尾：

  ```
  [1/3] trial#0 FAILED  训练超时（>300s） (300.2s)
  [2/3] trial#1 FAILED  训练超时（>300s） (300.2s)
  [3/3] trial#2 FAILED  训练超时（>300s） (300.2s)
  结束（budget_exhausted）：本次会话没有完成的试验（全部剪枝/失败）
  FAIL: 无任何试验完结
  ```

  journal 里每条 trial_fail 的 hint 都写着「可减少 epochs/width 或调低 budget.data_fraction；也可在 settings.yaml 提高 adapter.timeout_s」，但没有任何环节去兑现这条建议。深挖后又牵出两个隐藏缺陷（见下）。
- **影响范围**：新代码库接入这条主链路在真实场景下**必然失败**——setup agent 起草的配置越"有想象空间"（epochs 范围拉得大），正式搜索越容易成片超时；同时 setup 会话的 LLM 花费对用户不可见，数据隔离也可能被 setup 自己破坏。
- **复现步骤**：1) 全新项目目录放一个按协议打印的训练脚本；2) Web 新建项目并触发 setup；3) setup agent 把 epochs 上界写得明显大于探针采样值（如 epochs∈[5,50] 而探针只测了 epochs=10）；4) `adapter.timeout_s` 沿用默认 300 → 冒烟搜索里任何 epochs 偏大的试验都 >300s 超时。100% 复现（只要空间最重配置耗时 > timeout）。

## T · 目标（Task）

- **要达成什么**：让「新建项目 → setup agent 起草 → 冒烟搜索」在真实 LLM 下稳定跑通；且 setup 重写配置不破坏 `.tansuo` 隔离、setup 花费可见。
- **验收标准**：`acceptance_real_setup.py` 退出码 0；搜索 completed+pruned > 0 且给出最优值；setup 事件流非空且 tokens>0；运行日志落在项目 `.tansuo/` 内；全量回归绿。
- **约束条件**：修复要**确定性**（不能指望 LLM 每次自觉）；不改变既有 demo/CLI 用户行为；不增加真实验收之外的自动回归负担（真实验收耗 token、不进回归套件）。

## A · 解决方案（Action）

### 排查过程

1. 先看搜索日志：3 次试验全在 300.2s 被掐，journal 的 trial_start 显示采样到 epochs=14/28/27，而 setup 的探测试验只用了 epochs=10、耗时 184.5s。**探针是最轻的配置之一，它的耗时根本不代表空间里最重配置**。setup agent 拿到的 `run_probe_trial` 返回里其实有 `duration_s`，但没有任何机制把它折算成 `adapter.timeout_s`——默认 300s 原样沿用，于是 epochs 稍大就爆。
2. 顺带核对 setup 重写后的 settings.yaml：`data_dir` 和 `storage.url` **不见了**，回退到默认 `data`/`sqlite:///data/tansuo.db`，运行数据落到了 `<项目>/data/` 而非 `<项目>/.tansuo/data/`——`.tansuo` 隔离被 setup 自己捅破。根因：`save_settings` 是整体覆写，LLM 重写时并不知道脚手架写进去的 `.tansuo/` 路径这些"部署事实"。
3. 再查 setup 事件流为空：journal 实际写在 `.tansuo/data/setup_journal.jsonl`（cmd_setup 启动时按当时 settings 解析），但 `/api/setup/events` 在 setup 结束后**重新 load_settings 解析** data_dir——此时 settings 已被 agent 覆写、data_dir 变了，路径错位，读到空。
4. 第二跑修完前两条后，事件流能查到了（`session_start/agent_tool_call/trial_start/finish`），但 `tokens=0`：`agent_token_summary` 只统计 `agent_wakeup` 的 `phase=end` 事件（调参会话才有），setup 会话从不产生 wakeup 事件，token 用量没落 journal——setup 花费不可见。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 只在 setup 提示词里写"记得把 timeout_s 调大" | 放弃 | LLM 算术与自觉性不可靠，真实验收要的是稳定通过；提示词只能作为确定性护栏的说明 |
| 脚手架把 `timeout_s` 默认写大（如 3600） | 放弃 | 盲目调大治标：对不同训练脚本"最重配置耗时"无从预知，且掩盖了"探针耗时 ↔ 空间 envelope 需折算"这个真正该建模的关系 |
| 让 `save_settings` 逐字段 diff 合并旧配置 | 放弃 | 过度设计；只需保留少数几个"部署事实"字段，用一张白名单 + 深取/深设即可，改动最小 |
| `/api/setup/events` 继续按 setup 结束后的 settings 解析 journal 路径 | 失败 | 正是 bug 本身：settings 会被 setup 覆写，事后解析必错位；必须改用会话启动时绑定的 data_dir |
| 把超时校准做成"探针失败后才补救" | 放弃 | 探针往往采样到轻配置根本不会失败，等正式搜索才爆就晚了；必须在探针成功后**主动**折算 |

### 最终方案

1. **探针超时校准（确定性回写）**——`tansuo/agent/skills/config.py` 新增 `SetupExecutor._calibrate_timeout`：探针成功后，按 `探针耗时 ×(空间最大 epochs÷探针 epochs)× 3 倍余量` 折算并回写 `adapter.timeout_s`（上限 7200s、绝不下调；超限给 warning 让 agent 在 finish 摘要里建议收窄）。探针返回 JSON 里新增 `timeout_calibration` 字段供 agent 知情：

   ```python
   recommended = int(math.ceil(duration_s * max(ratio, 1.0) * 3.0 / 10) * 10)
   recommended = max(recommended, current, 300)
   capped = recommended > _TIMEOUT_CAP_S   # 7200
   recommended = min(recommended, _TIMEOUT_CAP_S)
   ```

2. **settings 覆写保留环境字段**——同文件 `_merge_env_fields`：`save_settings` 整体覆写前，把既有配置里的白名单字段（`experiment.data_dir / fingerprint_paths / dataset`、`storage.url`、`agent.model / base_url / auth_token`）在 LLM 漏写时自动补回；`adapter.timeout_s` 做棘轮（不允许覆写时调低）。回执里提示"已自动保留既有环境字段：…"。
3. **journal 定位改用会话绑定**——`tansuo/web/setup_manager.py` `start()` 存 `self.data_dir`；`tansuo/web/app.py` `_setup_journal_path()` 优先返回 `SETUP.data_dir / "setup_journal.jsonl"`，不再事后按被覆写的 settings 重解析（服务重启无绑定时才回退）。
4. **setup 花费落审计**——`tansuo/agent/loop.py` `SetupAgent.run()` 结束时追加一条 `AGENT_WAKEUP(phase=end)` 事件，携带 `loop.round_input/output_tokens`，使 `agent_token_summary` 对 setup 会话同样有效；前端 `agentEvents.ts` 对 `mode=setup` 渲染为"配置会话完成（本轮 in X / out Y tokens）"。
5. **setup 提示词补纪律**——`tansuo/agent/prompts.py` `setup_system` 增加「现有配置注入」（`{{existing_settings}}` 占位符）「环境字段保留」「超时校准」三节；`build_context_setup` 增 `existing_settings` 参数。
6. **配套回归**——新增 `tests/test_setup_guard.py`（16 项：环境字段保留 / timeout 棘轮 / 校准折算与触顶 / 提示词注入）；`acceptance_real_setup.py` 增断言（setup 事件非空、tokens>0、运行日志落在 `.tansuo` 内）并把搜索轮询上限从 3600s 提到 7200s。

## R · 实际效果（Result）

- **验证方式**：`python tests/acceptance_real_setup.py --dir <全新项目> --train <脚本> --trials 3` 真实 LLM 三跑收敛到退出码 0；全量回归 12 单测套件 + CLI 冒烟 31 项 + Web 冒烟 82 项全绿；`npm run build` 通过。
- **前后对比**：
  - 冒烟搜索：完结 **0/3（全超时）→ 3/3**，最优 **val_acc=0.9905**（trial#1，epochs=11）；
  - 超时校准真实触发：探针 42.1s（epochs=2）→ `adapter.timeout_s` 自动 300 → **1270**，agent 摘要主动向用户解释折算逻辑；
  - setup 事件流：空 → `session_start/agent_tool_call×6/trial_start/agent_wakeup/finish`，**tokens 32523**（花费可见）；
  - 数据隔离：运行数据曾逃出 `<项目>/data/` → 修复后全部落 `<项目>/.tansuo/data/`（runs/setup_journal/日志），项目根无逃逸 `data/`。
- **副作用与代价**：1) 超时校准是启发式（epochs 折算 + 3 倍余量），极端脚本若耗时主要由非 epochs 维度驱动可能仍偏紧，靠上限 warning + agent 建议收窄兜底；2) setup journal 现在多一条 `agent_wakeup` 审计事件，前端已按 setup 文案渲染，不影响调参会话；3) 真实验收耗时更长（搜索轮询放到 7200s），属预期。
- **遗留问题与后续**：超时折算目前只识别"名字含 epoch 的数值参数"作为轮数维度，遇到用 step/iter 命名的脚本可扩展；`_calibrate_timeout` 与 `_merge_env_fields` 已有单测，后续若新增"部署事实"字段记得进 `_PRESERVE_FIELDS` 白名单。
- **经验教训**：1) **探针成功 ≠ 搜索能跑**——探针往往是最轻配置之一，它的耗时必须折算到搜索空间 envelope 才有意义，否则"看起来都通了"的端到端会在正式搜索成片翻车；2) **LLM 重写整体配置必然丢它不知道的字段**——部署事实（数据目录/存储/端点）要么注入上下文 + 白名单兜底，要么被悄悄抹掉；3) **路径解析要绑定时机**——凡是"子进程写入、父进程事后读取"的路径，父进程必须用**启动时**的解析结果，不能在产物可能被覆写后重新解析；4) 真实 LLM 验收是照妖镜：单测/冒烟全绿仍掩盖不了这三条，确定性护栏 + 真实验收缺一不可。
