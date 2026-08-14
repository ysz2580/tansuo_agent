---
date: 2026-08-14
number: "016"
title: 三项用户视角优化（提示词版本进分区标记 / 跑完 webhook 通知 / LLM 花费可见）的设计权衡，及写回正则吞行尾注释的真实 bug
severity: low
status: resolved
tags: [设计决策, webhook通知, token用量, 分区标记, 配置写回]
module: cohort / notify / orchestrator / journal / web 前后端
---

# 三项用户视角优化（提示词版本进分区标记 / 跑完 webhook 通知 / LLM 花费可见）的设计权衡，及写回正则吞行尾注释的真实 bug

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent（Optuna TPE + LLM 监督 agent 的超参调优系统）；
  涉及 `tansuo/cohort.py`、`tansuo/orchestrator.py`、`tansuo/journal.py`、
  新增 `tansuo/notify.py`、`tansuo/web/app.py`、前端 React 页面与 `tansuo/config.py`
- **环境**：Windows 11，Python 3.14，Optuna 4.x，FastAPI，React + Vite + TS；
  上游提示词管理功能刚交付（commit `2c5129c`，STAR #015）
- **当时在做什么**：从"把 agent 当调参替身用"的用户视角复盘，找出三个真实缺口并逐一实现：
  1. **可比性被静默污染**：分区指纹只有三枚（objective_hash / code_hash / data_hash），
     但提示词改变 agent 的监督行为。两个分区之间改了提示词，跨分区对比页仍显示
     "同目标可比"——与 #012 direction 静默反向同类问题。
  2. **长任务跑完无通知**：调参一跑几小时，用户挂机离开；全仓库 grep 不到任何
     webhook/notify 代码。会话统一出口在 `Orchestrator.run()`，但结束有 3 个 FINISH
     写入点（resume-skip / KeyboardInterrupt / 正常收尾），agent 降级还是中途状态
     切换（不结束会话）。
  3. **LLM 花费零可见**：agent 每轮唤醒烧 token，全仓库无 usage 统计；恰是提示词
     迭代闭环缺失的一环（改提示词 → 花费变化无从得知）。
- **影响范围**：不影响既有功能，属于"用户天天遇到但系统装聋"的体验缺口
- **复现步骤**：1) 跑一个分区 → 改 prompts.yaml 保存 → 再跑（续进同一分区）→
  对比页看不出两批试验提示词不同；2) `python cli.py run --hours 8` 后离开，跑完
  没有任何推送；3) 翻遍分区 journal 找不到一条 token 用量记录

## T · 目标（Task）

- **要达成什么**：
  1. 分区 meta 记录创建时的提示词版本；UI（分区选择器 + 对比页）显式标记差异。
     **提示词变化不得触发新开分区**——它不是训练输入。
  2. 会话结束（含 3 个 FINISH 点）与 agent 降级各推送一条 webhook 消息，兼容
     钉钉/飞书/Slack 自定义机器人；Web 设置页可配置、可一键测试；
     **通知失败绝不影响搜索**。
  3. 每次 LLM 调用的 usage 计入审计，agent 页展示本分区累计 token。
- **验收标准**：单测层面——分区 116 断言含 6 项 prompt_version 新断言；notify
  新套件覆盖 payload/收发/门控/校验/写回；token 审计跨续跑会话累加正确。
  端到端——e2e_web_smoke 用本地 http.server 捕获真实 POST 断言钉钉信封与结束原因；
  全部既有套件零回归；`npm run build` 通过。
- **约束条件**：零新依赖（不引入 requests）；不动 comparable 五值枚举；
  `AgentLoop.run()` 返回值签名不变（零调用方改动）；`${ENV:...}` 机密引用
  绝不能被 Web 写回物化成明文。

## A · 解决方案（Action）

### 排查过程 / 设计取舍

1. **提示词版本放哪**：第一反应是扩 `comparable` 枚举（加 prompt-changed）。盘点后
   放弃：该五值映射在 app.py、cli.py、前端 `COMPARABLE_META`、test_cohort 共十余处
   断言，且语义是"训练输入是否一致"——提示词不属于训练输入，硬塞进去反而污染枚举
   语义。改用**独立字段**：meta 加 `prompt_version`，API 加 `prompt_changed` 布尔，
   纯增量、零既有断言受影响。配套决策：`resolve_for_run` 的三指纹匹配**不看**提示词
   版本（有测试专门断言"改提示词后续跑同一分区"）。
2. **cohort.py 怎么拿版本**：`tansuo/agent/__init__.py` 急切导入 loop→client→anthropic，
   若在 cohort.py 顶层 import prompt_store，等于把分区创建耦合到 SDK 安装。
   用函数体内局部 import + 全异常兜底返回 0（`_prompt_version`），分区创建不因
   提示词读取失败受阻。
3. **token 汇总口径**：supervisor 会话内累计值会在进程重启/断点续跑后清零，
   直接取"最后一条事件的 total"跨会话会丢历史。所以 end 事件**同时**写当轮增量与
   会话累计，`journal.agent_token_summary()` 只按 `phase=end` 的**当轮增量求和**——
   跨进程续跑天然正确（测试：两次 wake、第二个 supervisor 从 0 起算，汇总仍累加到
   in 400 / total 450）。
4. **通知挂钩点**：考虑过把 `run()` 主体包进 try/finally 统一收尾，放弃——
   结构性重构对三条既有路径都有回归风险，而硬崩溃（非预期异常）本来也发不出通知。
   最终在 3 个既有 FINISH 写入点各加一行 `self._notify_finish(reason)`、
   `_wake` 降级分支加一行 `self._notify_degrade(limit)`，每个钩子自身再包一层
   try/except 吞异常：通知出错只记日志。
5. **零依赖发送**：标准库 `urllib.request` POST JSON，`send_webhook` 返回
   `{"ok", "detail"}` 永不抛；四种机器人信封差异收敛进纯函数 `build_payload`
   （dingtalk `text.content` / lark `content.text` / generic、slack `text`），可单测。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| comparable 枚举加 prompt-changed | 放弃 | 触动前后端十余处五值断言；提示词非训练输入，语义不该进该枚举 |
| run() 包 try/finally 统一通知 | 放弃 | 结构重构回归风险 > 收益；硬崩溃场景照样发不出通知 |
| 引入 requests 发 webhook | 放弃 | 为一个收尾动作新增依赖不值；urllib 足够且永不抛的兜底更好写 |
| 取 end 事件最后一行的累计 total 作汇总 | 放弃 | 进程重启/续跑后会话累计清零，跨会话丢历史 |
| 写回正则 `^(?P<sp>\s*){name}:(\s*\S.*)?$` 整行替换 | **出 bug** | 行尾注释被一并吞掉，见下 |

### 实现中暴露的真实 bug：写回正则吞行尾注释

test_notify 首次运行即红：

```
AssertionError: FAIL: 注释保留
```

根因：写回正则 `^(?P<sp>\s*){name}:(\s*\S.*)?$` 的 `(\s*\S.*)?$` 把行尾注释
（`enabled: true            # 总开关`）并进匹配范围，`subn` 整行替换后注释蒸发。
而本仓库 settings.yaml 恰是"每行带尾注释"的配置即文档风格（demo 里 agent 段的
`base_url: ${ENV:...}      # 空则用 SDK 默认/环境变量` 同样在劫难逃）——Web 保存
一次就毁掉一段文档。修复（`tansuo/notify.py::write_back_notify`）：

```python
pattern = re.compile(rf"^(?P<sp>\s*){name}:(?P<rest>.*)$", re.M)
m = pattern.search(text)
...
# 行尾注释原样提取（YAML：空白 + # 起注释）再拼回，避免写回吞注释
cm = re.search(r"\s+#.*$", m.group("rest"))
comment = cm.group(0) if cm else ""
text, n = pattern.subn(lambda mm, ...: f"{mm.group('sp')}{_n}: {_r}{_c}", text, count=1)
```

`tansuo/agent/api_setup.py::write_back_agent` 存在同一潜在问题（同构代码），
一并修复。测试补断言：写回后 `# 总开关` / `# 机器人地址` 仍在。

### 最终方案落点

1. **功能 1**：`prompt_store.current_version()` 薄函数 → `create_cohort` meta 加
   `prompt_version` → app.py `runs_list` / compare.py 透出 → 前端分区选择器琥珀
   「△ 提示词已变」小徽章、对比页新增提示词列 + 组内版本不一致琥珀警告。
2. **功能 3**：`AgentLoop` 两个 `call_with_retry` 点后 `_count_usage(resp)` 累加
   `resp.usage` → supervisor wake 结束写 AGENT_WAKEUP end 事件（当轮 + 会话累计）
   → `journal.agent_token_summary()` → `/api/agent/events` 返回加 `tokens` →
   agent 页顶部蓝色累计 chip、每轮事件行内「本轮 in/out tokens」。
3. **功能 2**：新模块 `tansuo/notify.py`（payload / send / notify_finish /
   notify_degrade / write_back_notify）；`config.py` 新增 `NotifyCfg`
   （enabled/webhook_url/format/events，`${ENV:...}` 走既有 `_expand_env` 自动展开，
   format/events 强校验）；orchestrator 五处单行挂钩；Web 三路由
   （GET 打码回显、save 校验 + 明文告警、test 实发）；前端 SettingsPage `NotifyPanel`
   （格式下拉 / 事件开关 / 测试 / 保存）；demo settings.yaml 加 notify 块
   （webhook_url 默认 `${ENV:TANSUO_WEBHOOK:}` 空引用 = 不发）。
   e2e 第 13 步：本地 `ThreadingHTTPServer` 捕获，断言测试消息与**真实会话收尾**
   的钉钉信封 POST（含 `budget_exhausted` 原因——该场景恰好走的是 resume-skip
   通知路径）。

## R · 实际效果（Result）

- **验证方式**：
  - `python tests/test_notify.py` 32 断言（payload 四格式 / 真实收发 / 四种门控静默 /
    校验拒绝 / ENV 展开 / 写回含注释保留与缺行报错）；
  - `tests/test_cohort.py` 116 断言（含 prompt_version 6 项：改提示词不新开分区、
    force_new 记录版本、meta 落盘往返、save_override 后新分区同步 +1）；
  - `tests/test_runtime_features.py` 29 断言（含 token 审计 6 项：跨会话累加正确）；
  - `tests/e2e_web_smoke.py` 55 断言全绿（第 13 步 webhook 8 断言）；
    `tests/e2e_cli_smoke.py` 31 断言；其余 7 套件（对比 22 / 热启动 16 / 条件空间 30 /
    护栏 21 / 协议 12 / 空间补丁 34 / 提示词 28）全绿；`npm run build` 通过
- **前后对比**：三缺口全部闭环——分区对比能看见提示词版本差；会话结束/降级有推送；
  agent 页有累计 token。单测断言总数 296 → 340（+32 notify、+12 其余增量）
- **副作用与代价**：无回归（所有挂钩均为增量单行）；硬崩溃（非预期异常退出）
  不发通知，属接受的边界；降级通知只在达到阈值那一刻发一次
- **遗留问题与后续**：`_wake` 降级分支的通知依赖 `events` 订阅配置，若用户只订阅
  session_end 则降级静默——这是设计内行为，界面已明示订阅项
- **经验教训**：
  1. **"不参与决策的元数据"用独立字段，别扩既有枚举**——枚举值域牵动全链路断言，
     新维度独立承载才能做到零回归；
  2. **收尾类横切功能挂在既有事件写入点**（每个 FINISH 点加一行），而不是重构控制流
     包 try/finally——前提是先数清全部写入点（本例 3 个）；
  3. **跨进程累计值要在事件里同时留"增量"与"累计"两份**，汇总层只信增量，
     否则断点续跑场景必丢历史；
  4. **"镜像既有实现"要连测试一起镜像**：write_back 抄了 agent 的正则，也抄来了
     它吞行尾注释的潜在 bug——本仓库配置是"每行尾注释"风格，写回类函数必须有
     注释保留断言；这次是新测试 first-run 就抓住的，成本最低。
