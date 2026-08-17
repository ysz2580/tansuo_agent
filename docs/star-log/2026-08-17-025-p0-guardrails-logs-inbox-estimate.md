---
date: 2026-08-17
number: "025"
title: 四项 P0 用户侧功能的设计取舍与踩坑：早停护栏记账与续跑基准、人工试验队列原子认领/放回、试验全量日志落盘、预算预估三形态写回
severity: medium
status: resolved
tags: [早停护栏, 人工试验, 试验日志, 预算预估, 配置写回, 设计决策]
module: orchestrator / cli / web 后端与前端
---

# 四项 P0 用户侧功能的设计取舍与踩坑：早停护栏记账与续跑基准、人工试验队列原子认领/放回、试验全量日志落盘、预算预估三形态写回

## S · 背景（Situation）

- **项目 / 模块**：tansuo 智能超参数调优 agent。后端 `tansuo/orchestrator.py`、`tansuo/config.py`、`tansuo/runner.py`、`tansuo/adapter.py`、`tansuo/web/app.py`、`tansuo/agent/skills/config.py`；CLI `cli.py`；前端 `web/src/pages/{SettingsPage,TrialsPage}.tsx`、`web/src/lib/api.ts`。
- **环境**：Windows 11、Python（Optuna TPE + sqlite RDB）、React/Vite/TS/shadcn-ui、PowerShell 5.1。
- **当时在做什么**：前一批七功能（commit 1ff270d）完成后，从用户角度复盘点出 P0 清单并实现：
  1. **早停护栏**——脚本写错/环境坏时试验连续确定性失败，搜索会把预算全烧在失败上；收敛停滞时也缺少自动停机制。
  2. **试验级日志下钻**——失败试验只有一行 reason，stdout/stderr 全量输出无处可查，Web 上也看不到。
  3. **人工试验插队**——用户想手动指定一组参数（经验值/复现某配置）让系统执行，此前完全没有入口。
  4. **预算预估建议**——启动搜索前用户不知道该给多少 `budget.max_gpu_hours`，全靠拍脑袋。
- **问题表现**：属功能缺失而非线上故障；实现过程中暴露的真实报错见「排查过程」（最典型一条是 Web 冒烟的舍入断言失败）：

  ```
  AssertionError: FAIL: 多槽位按 GPU·小时折算（耗时 ×2） {'basis': 'history', 'sample': 3, 'per_trial_s': 3.12, 'trials': 5, 'slots': 2, 'est_hours': 0.0087, 'unit': 'GPU·小时', 'recommended_max': 0.01}
  ```

- **影响范围**：缺早停护栏时一次脚本错误可烧光全部试验预算；失败试验不可诊断；人工经验无法注入搜索；算力预算只能盲填。
- **复现步骤**：不适用（功能新增）。

## T · 目标（Task）

- **要达成什么**：四功能端到端落地——后端语义正确、CLI 可用、Web 可视化、journal 全程可审计。
- **验收标准**：新增单测套件全过；全量回归 17 个单测套件零失败；CLI 冒烟、Web 冒烟全绿；`npm run build` 通过。
- **约束条件**：不破坏既有 settings.yaml 布局（写回必须保注释、保原字段）；人工试验必须可审计（journal source=human）；护栏不能误伤正常搜索（可关闭、阈值可配）；续跑时老成绩不能被当成新提升。

## A · 解决方案（Action）

### 排查过程与关键设计决策

1. **护栏记账放哪**：`_outcome(result, value)` 统一记账点，持 `_compute_lock`（与算力记账共用锁，避免并行 workers 下计数撕裂）——failed → 连败 +1；completed/pruned → 清零；plateau 只统计 completed 试验、方向感知对比 `_best_so_far`（pruned 试验无终值，不参与）。主循环顺序：`while budget_left()>0 and not finished_reason`：consume_inbox → 时间/算力检查 → run_batch(min(wake_every, left)) → fail_streak 检查 → plateau 检查 → 时间/算力再检查 → supervisor wake；run_batch 内部派发循环也按 `_time_exceeded() or _fail_streak_hit()` break，避免连败已达标还继续派发。
2. **续跑基准**：run() 开始时重置三项护栏状态，`_best_so_far` 取 `study.best_value`（空 study 抛 ValueError → 置 None）。没有这一步，续跑会话会把历史最优之后的每个普通成绩都误判为「无提升」，plateau 提前误触发；反过来第一个完结试验若优于历史最优则刷新基准——老成绩永远不算「新提升」。
3. **run() 自然耗尽不设置 `self.finished_reason`**：只有 fail_streak/plateau/time/compute/interrupted 才写该属性；run() 尾部用局部变量 `reason = self.finished_reason or "budget_exhausted"` 写 FINISH 事件。这是刻意的（属性语义=「被护栏/外力提前终止」），但第一次写测试时踩了坑，见下。
4. **inbox 原子认领**：`os.replace(inbox.jsonl → inbox.processing.jsonl)` 一步认领（同目录 rename 在 Windows/POSIX 都是原子的），杜绝双进程并发消费同一条。逐条 `run_custom(params, note, source="human")`；JSON 损坏/缺 params → 跳过不炸；**执行异常或预算耗尽拒绝 → 剩余条目原样放回队列**（stop 语义，不静默丢失）；返回 `{"consumed", "requeued"}`。
5. **sqlite 并发事实**：先验证了「运行中的 orchestrator 是否持 sqlite 永久写锁」——不持（事务级锁），Optuna 的 ask/tell 模型支持多进程共享 RDB。所以 CLI `try` 在搜索运行中也能即时执行一条试验，无需排队等批间；执行不了（锁竞争/预算耗尽）时配置保留在队列，下一批开头自动消费。
6. **试验日志落盘**：runner 把子进程完整 stdout/stderr 写到 `<分区>/trials/trial-NNNN.log`（头行记 cmd 与参数快照、尾段记 exit_code/timed_out）；失败试验的 reason detail 附日志绝对路径；Web `/api/trials` 行带 `has_log`，详情弹窗展开 ScrollArea 查看。python 函数模式无子进程输出 → 404 文案说明。
7. **预估口径**：history 优先（分区 journal 里 TRIAL_END 的 `duration_s` 最近 20 次完结均值），无历史则 probe（setup 探针耗时——为此在 `skills/config.py` 探针成功路径补落 `TRIAL_END, source="probe", duration_s=...` 到 setup_journal.jsonl，解决「全新项目零历史也能估」）。`est_hours = per × trials × slots ÷ 3600`（round 4 位）；建议值 `recommended_max = est × 1.2`（round 3 位）；slots>1 → 单位「GPU·小时」否则「机时」。
8. **write_back_budget 三形态**：真实仓库的 settings.yaml 存在三种 budget 写法——① 已有缩进 `max_gpu_hours:` 行 → 原位覆盖并保行尾注释；② 行内流式 `budget: {total_trials: 30, ...}` → 收尾花括号前插入（`sep=", " if body.strip()`）；③ 块形态 `budget:` 独占行（scaffold 模板即此形态）→ 按首个子键缩进插入为首个子键。三种形态写入前都经临时文件 `load_settings` 完整校验，值格式 `f"{max_gpu_hours:g}"`，校验失败不落盘。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| plateau 把 pruned 试验也计入 | 放弃 | pruned 无终值，无「提升/无提升」语义；只统计 completed 才方向感知可靠 |
| run() 自然耗尽也设 `finished_reason="budget_exhausted"` | 放弃 | 破坏属性语义（=被提前终止），且既有代码/测试依赖「None=正常跑满」区分 |
| inbox 用文件锁（portalocker 风格） | 放弃 | 引入新依赖；`os.replace` 同目录原子改名已足够，且跨平台行为一致 |
| 预估只看 setup 探针耗时 | 放弃 | 探针用小数据分数/少轮数，远低于真实试验耗时；有历史时必须以历史实测为准，探针只做新项目兜底 |
| Web 表单数值参数直接提交文本框字符串 | 失败 | shadcn Select/Input 一律返回字符串，数值 choice（如 `0.5`）变 `"0.5"` 后 `domain_contains` 类型不符被拒 → 提交时按参数类型还原（choice 用 `choices.find(c => String(c)===raw)` 还原原始值，float/int 用 `Number()` 校验有限性） |

### 最终方案

1. **配置**（`tansuo/config.py`）：`BudgetCfg` 增 `max_fail_streak: int = 5`（0=关闭）、`auto_stop_plateau: int | None = None`（默认关闭），校验分别 ≥0 / ≥2（plateau=1 无意义）。
2. **护栏与日志**（`tansuo/orchestrator.py`、`tansuo/runner.py`）：如上「排查过程」第 1-3、6 点；SESSION_START 事件记录两项护栏配置，护栏触发时打 `[护栏]` 日志（fail_streak 附关闭提示 `max_fail_streak=0`，plateau 附节省预算说明）。
3. **人工试验**（`tansuo/orchestrator.py` consume_inbox / run_custom、`cli.py` cmd_try/cmd_custom、`web/app.py` POST /api/custom）：
   - `cli.py try --params JSON [--note] [--cohort]`：校验链 load_settings → JSON 解析（非空对象）→ `_pick_read_cohort` → `validate_config` 逐条报错 → 追加 `{"params","note","queued_at"}` 到 `inbox.jsonl` → 尝试 `_make_runtime` + `consume_inbox()` 即时执行，失败降级提示「配置保留在队列」；
   - `cli.py custom`：空闲时手动消费队列；
   - Web POST /api/custom：运行中写运行分区（`mgr.last_cohort`），空闲写最新分区；校验失败 400 带 detail；空闲即时派发 `mode=executing`，运行中 `mode=inbox`。
4. **预估**（`web/app.py` GET /api/estimate、POST /api/estimate/adopt）：如上第 7-8 点；adopt 返回 `{write_back: {ok, changed, errors}, max_gpu_hours}`。
5. **前端**（`api.ts`、`SettingsPage.tsx`、`TrialsPage.tsx`）：
   - RunPanel 加 500ms 防抖预估（依赖 `[trials, selGpus]`），展示口径说明 + 「采纳为算力上限」一键写回；
   - TrialsPage 顶部加 `CustomTrialCard`（space 拉取后按参数预填默认值：choice→首选项、float/int→区间中点、log 用几何中点 `sqrt(low*high)`；冻结参数只读 Badge 展示）；
   - TrialDetailDialog 按 `has_log` 拉 `/api/trials/{n}/log`，ScrollArea + `<pre>` 展示全量输出。
6. **测试**：新增 `tests/test_p0_features.py`（45 断言：fail_streak/plateau/日志/inbox/写回五组）；CLI 冒烟加第 11 节（try 合法/超域/非 JSON/空对象、custom 空队列）；Web 冒烟加第 19 节（estimate 口径/折算/adopt 落盘/非法拒绝、custom 三拒绝+即时执行+journal 审计、has_log、日志端点、9999→404）。

### 踩坑记录（测试期真实失败）

1. **`finished_reason` 语义坑**：test_p0 首跑断言 `orch2.finished_reason == "budget_exhausted"` 得到 `None`——run() 自然耗尽预算不设置该属性（见决策 3）。改断言为 FINISH 事件 `reason=="budget_exhausted"`。
2. **单元级记账状态残留**：测「连败累计」时期望 2 实际 7——run() 已跑完 5 次失败，`_fail_streak` 停在 5。单元测前先 `orch2._fail_streak = 0` 重置。
3. **后端 round 与测试容差**：Web 冒烟多槽位折算断言 `abs(est2.est_hours - est.est_hours*2) < 1e-6` 失败——后端 `round(..., 4)` 舍入：`per=3.12, trials=5, slots=1 → 0.0043`；`slots=2 → 0.0087`，而 `0.0043×2=0.0086`。容差放宽到 `< 5e-4`（断言改名注明「容许 4 位小数舍入」）。
4. **cmd_try finally NameError**：`_make_runtime` 抛异常时 `orch` 未绑定，finally 里 `dispose_study(orch.study)` 抛 NameError 覆盖原 return 码 → 先 `orch = None`，finally 内 `if orch is not None`。同批修复：argparse 子命令经 `set_defaults(seed=None, warm_start=None, model=None)` 补三属性（`_make_runtime` 会访问）。
5. **has_log 断言范围**：最初断言「所有完结+失败试验均有日志」——被 stop 标记为 FAIL 的试验（进程树被杀、未到落盘阶段）可能无日志，放宽为仅 COMPLETE 试验必须有。

## R · 实际效果（Result）

- **验证方式**：
  - `python tests/test_p0_features.py` → 全部通过：45 项断言；
  - 全量回归 17 个单测套件逐个直跑 → **失败套件数：0 / 17**（含 test_p0_features 45、test_cohort 116、test_runtime_features 65、test_space_patch 39、test_notify 32、test_conditional_space 30、test_compare 28、test_prompts 28、test_graduate_export 24、test_gpu_cost 24、test_guardrails 21、test_setup_guard 20、test_adapter_gen 16、test_project_store 16、test_warmstart 16、test_env_mgmt 15、test_protocol 12）；
  - `python tests/e2e_cli_smoke.py` → 全部通过（含第 11 节人工试验 5 组断言）；
  - `python tests/e2e_web_smoke.py` → **Web 冒烟全部通过，共 120 项断言**（第 19 节 P0 全绿：estimate 三断言、adopt 落盘且模板其余字段完好、custom 三拒绝+即时执行+note 审计、完结试验均带日志、日志端点头行/协议行/尾段齐全、9999→404）；
  - `cd web && npm run build` → tsc -b && vite build 通过，dist 随仓库提交。
- **前后对比**：
  - 脚本错误场景：从「烧光全部预算」→ 连败 5 次（默认）即优雅收尾，FINISH reason=fail_streak；
  - 收敛停滞：可选 auto_stop_plateau，连续 M 次完结无提升自动停，FINISH reason=plateau；
  - 失败诊断：从「一行 reason」→ trial-NNNN.log 全量 stdout/stderr + Web 弹窗展开；
  - 人工经验注入：从「无入口」→ CLI try/custom + Web 表单双通道，journal TRIAL_END source=human 可审计；
  - 预算：从「盲填」→ 启动前按历史/探针实测给出估算 + 20% 余量建议值，一键写回 settings.yaml。
- **副作用与代价**：试验日志落盘增加磁盘占用（每试验一个文件，位于分区 trials/ 目录内，随分区归档）；plateau 护栏对噪声大的指标可能偏早停——默认关闭，文档注明。
- **遗留问题与后续**：无阻塞遗留。后续可考虑 P1 项（多指标 Pareto、试验间依赖等）。
- **经验教训**：
  1. 「属性 vs 事件」语义要在实现前钉死：`finished_reason=None` 表示「正常跑满」是既有约定，新功能（budget_exhausted 也想设属性）差点破坏它——先查既有测试对 None 的依赖再动语义。
  2. 涉及 round 的后端输出，测试断言容差必须按「输出精度」设定（round 4 位 → 容差 ≥5e-4），不能用 1e-6 假精确。
  3. 前端控件（Select/Input）产出的永远是字符串，提交前必须按搜索空间参数类型还原——数值 choice 尤其隐蔽（界面上看不出 `"0.5"` 与 `0.5` 的差别）。
  4. 队列消费类功能先想清楚失败语义：跳过（损坏条目）vs 放回（异常/拒绝），两条路径都要有测试覆盖，否则静默丢用户数据。
