---
date: 2026-08-16
number: "023"
title: 补齐 #022 三项遗留并开通人工空间编辑入口：唤醒时确定性失败/收敛护栏强制注入、CLI space patch 可扩展 envelope、瞬时失败环境线索、Hyperband widen 真实验收
severity: medium
status: resolved
tags: [确定性护栏, wake-brief, space-patch, 环境线索, hyperband, 设计决策]
module: tansuo/analysis.py + agent/prompts.py + agent/loop.py + space.py + runner.py + orchestrator.py + cli.py + web/agentEvents.ts
---

# 补齐 #022 三项遗留并开通人工空间编辑入口：唤醒时确定性失败/收敛护栏强制注入、CLI space patch 可扩展 envelope、瞬时失败环境线索、Hyperband widen 真实验收

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 的调参 agent 护栏层与人工通道（`tansuo/analysis.py`、`agent/prompts.py`、`agent/loop.py`、`space.py`、`runner.py`、`orchestrator.py`、`cli.py`、`web/src/lib/agentEvents.ts`、`tests/acceptance_real_setup.py`）。
- **环境**：Windows 11（简中系统码页 GBK），Python 3.14（UTF-8 模式），optuna 4.9.0，React/Vite/TS 前端。
- **当时在做什么**：STAR #022 落地四项 robustness 后，其「遗留问题与后续」明确列出三项未闭环；本轮逐项补齐，并额外开通一个人工空间编辑入口。三项遗留原文：
  1. 「失败处置目前靠提示词引导 agent，尚无『检测到成片超时即自动收空间』的确定性护栏」——`#022` 的失败可见性是把 `recent_failures` 塞进 `get_study_summary` 的返回 + `tuning_system` 提示词纪律，均属「建议」：LLM 是否查看、是否照做没有保证（#021/#022 反复验证的教训：LLM 不可靠，护栏必须是确定性代码）。
  2. 「STAR #004 的瞬时退出码 1 根因仍未定位（重试是兜底而非根治）」——那类失败的特征是退出码非零、stderr 为空、单独复现正常；journal 里只有 `reason/hint`，没有任何现场环境证据，事后无从诊断。
  3. 「Hyperband 与动态空间编辑（agent widen epochs）的组合在长程搜索下的表现尚待真实验收检验」——`#022` 只做了配置校验与 `make_pruner` 工厂测试；`max_resource="auto"` 按已完结试验最大步数推断总资源，widen epochs 之后推断能否跟随，没有任何测试或真实验收覆盖；验收脚本 `tests/acceptance_real_setup.py` 也没有注入 pruner 的入口。
- **问题表现**（另在代码核实中发现的第 4 个缺口）：
  - **空间编辑没有人工通道**：`apply_patch` 对 widen 的校验是「⊆ 初始 envelope」（`test_space_patch` 断言「widen 超出 envelope 被拒」），agent 的 `edit_search_space` 工具走同一路径，约束正确。但用户若想亲自扩展空间（如 epochs 上界超出初始 envelope），唯一选择是手改 `search_space.yaml`——绕过全部校验（冻死护栏 MIN_FREE_PARAMS=3、int/log 合法性、版本与审计记录）。
- **影响范围**：遗留 1 使 agent 面对成片失败/长期停滞可能继续烧预算（处置依赖 LLM 自觉）；遗留 2 使疑难瞬时失败无法积累诊断证据；遗留 3 使 Hyperband 这一剪枝选项从未被端到端验证；缺口 4 使「人」这个最高权限角色反而没有合规的编辑入口。
- **复现步骤**：1) 构造连续 3 次失败的搜索并唤醒 agent——wake brief 里没有任何「系统警报」，处置全看 LLM 心情；2) 尝试 `python cli.py space patch`——命令不存在；3) 在 UTF-8 模式 Python 里用 `subprocess.run(["tasklist", ...], text=True)` 读进程列表——GBK 输出解码抛 `UnicodeDecodeError`。

## T · 目标（Task）

- **要达成什么**（用户指令「修补 1，2，3，4，5」）：
  1. **失败处置确定性护栏**：唤醒前由代码检测连续同类失败 → 强制注入 wake brief 警报（不依赖提示词纪律/LLM 自觉）；
  2. **Hyperband 真实验收**：acceptance 脚本加 `--pruner` 注入 + 搜索中途 widen 的确定性测试 + 凭据真跑；
  3. **CLI space patch 手动编辑入口**：人工权限通道，复用 `apply_patch` 全部校验，且 widen 可扩展 envelope；
  4. **收敛信号主动推送**：最近窗口内完成试验未刷新最优 → wake brief 带确定性收敛信号；
  5. **瞬时失败根因诊断线索**：瞬时故障时采集环境线索（磁盘余量/安全软件进程）写入 `trial_retry`/`trial_fail` 事件（STAR #004 遗留的证据链）。
- **验收标准**：五项各有单测；无信号时 wake brief 渲染输出与旧版逐字一致（`test_prompts` 有精确字符串断言）；全量回归（12 单测套件 + CLI/Web 冒烟）绿；Hyperband 真跑验收通过。
- **约束条件**：信号计算失败绝不影响唤醒主路径（增强项语义）；agent 权限边界不变——`edit_search_space` 依旧 widen ⊆ envelope，扩展 envelope 只有人工 CLI 通道可走；保留提示词测试依赖的字面量（「监督者 agent」「总预算 {{total_trials}} 次试验」、无残留 `{{`）。

## A · 解决方案（Action）

### 排查过程

1. **#1 与 #4 合并为同一注入通道**：两类信号（失败警报、收敛信号）的落点都是 wake brief，检测都发生在唤醒时刻——设计为单一通道 `analysis.build_wake_signals(orch) → prompts 的 wake_signals 变量 → loop 审计事件 → 前端渲染`，一次接线复用两类信号，避免注入点扩散。
2. **失败警报规则**（`failure_alerts(study, journal, streak=3)`）：只在最近 3 次失败**类别同质**时触发——全 timeout → 警报 + 点名疑似耗时维度（`suspicious_dims`：失败组均值 > 完结组均值且相对差 > 20%，排除无大小概念的 bool）；全 exit_code → 「停止烧预算」警报；类别混杂不触发（瞬时噪声已有重试兜底，避免把偶发当系统性打扰）。
3. **收敛信号**（`plateau_note`）：窗口默认 `max(4, 2×wake_every)`，且要求完结试验数 ≥ window+2 才判断——搜索早期样本不足时绝不触发；触发文案带此前最优值与 trial 号，并要求 agent「考虑 finish 或给出继续搜索的理由」。
4. **兼容性关键**：`test_prompts` 对 wake brief 有精确字符串断言。对策：新增 `wake_signals` 模板变量，无信号时渲染为**空串**（而非删占位符），旧断言逐字兼容；`build_wake_signals` 对 orch 纯鸭子类型（getattr study/journal/settings）且整体 try/except → `[]`，测试替身 `_Orch` 无这些属性时安静返回空。
5. **#3 权限模型**：`SearchSpace.extend_envelope(ops)` 只为 widen op 预扩 `env_low/env_high`（int 校验整数、log 校验 >0、非法条目跳过留给 apply_patch 报错），且**只在 CLI 路径调用**；随后仍走统一 `apply_patch`——收窄⊆当前范围、自由参数下限、版本与审计一应俱全。生效语义：写入 `search_space.yaml` 对之后新开的记录分区生效，已有分区续跑用分区快照（`load_space_with_snapshots` 快照优先）——CLI 成功输出里明示这一点。
6. **#5 线索取舍**：磁盘余量（`shutil.disk_usage`）+ 安全软件进程（`tasklist /FO CSV` 匹配 `_KNOWN_SECURITY_PROCS` 清单：360/火绒/腾讯/金山/Defender/Kaspersky/ESET/Avast/AVG/Malwarebytes，300s 缓存，仅 win32）。只附在**瞬时形态**失败（非零退出码且 stderr 为空）——确定性失败（stderr 有内容）不带线索，避免噪音；采集全程兜底，任何异常只省略字段绝不影响试验。
7. **#2 验收设计**：`max_resource="auto"` 是 widen 验收的核心——剪枝器按已完结试验的最大步数推断总资源，widen epochs 后推断必须跟随。确定性测试 `test_hyperband_widen`：epochs int[2,8]（env 上界 32）的三参数空间先跑 6 次，apply_patch widen 到 [2,32]，再跑 8 次——断言全部 COMPLETE/PRUNED 且存在曲线长度 > 8 的试验（证明真有试验越过旧上界 8 步、auto 推断跟上了）。acceptance 脚本加 `--pruner {median,hyperband}`：setup 起草配置后、冒烟搜索前注入 settings（hyperband 用 `min_resource=1, max_resource="auto", reduction_factor=3`）。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 失败警报继续走 `get_study_summary` + 提示词纪律 | 放弃 | 即 #022 的遗留形态：是否查看、是否照做全凭 LLM；护栏必须是代码检测 + 强制注入 |
| 检测成片超时后代码自动 narrow 空间 | 本轮不做 | 自动改空间风险高（噪声误触发会污染搜索）；先做到「确定性警报 + 提示词规定必须优先处置 + 人工 CLI 兜底」，自动收空间留作后续 |
| `apply_patch` 加 `human=True` 参数放开 envelope | 放弃 | 污染 agent 路径的签名与语义；改为独立的 `extend_envelope` 预处理、仅 CLI 调用，agent 路径物理上走不到 |
| `tasklist` 用 `text=True` 读输出 | 失败 | UTF-8 模式 Python 下 GBK 输出解码抛 `UnicodeDecodeError`（reader 线程），扫描静默返回空清单——改字节模式 `decode("utf-8", errors="replace")`；修复前本机 `security_procs` 恒空，修复后实测检出 `securityhealthsystray.exe` |
| `space patch` 复用 `space` 顶层 parser 的 `--settings/--space` | 失败 | `parents=[common]` 的参数挂在 `ps` 层，写在子命令之后报 `unrecognized arguments`；给 `ps_show`/`ps_patch` 补 `parents=[common]`（对齐 `runs show/compare` 的既有风格） |
| 首次 Edit 插入 `extend_envelope` | 失败重做 | old/new 锚点几乎相同导致代码未插入，且别处出现行合并损坏（`latest_snapshot` 的 def 签名与函数体首行被并到一行，全包 `IndentationError`）；以更长的真实内容锚点重做，并以 `ast.parse` 编译检查后才继续 |
| 合成试验数据里失败/完结组的 batch 随意取值 | 测试失败 | batch 失败组均值 32 vs 完结组均值 24，相对差 33% > 20% 阈值被 `suspicious_dims` 一并点名——非目标维度未控制的测试数据会污染断言；把完结组 batch 统一为 32 后只点名 epochs |

### 最终方案

1. **唤醒信号通道（#1/#4）**
   - `tansuo/analysis.py`：新增 `suspicious_dims`/`failure_alerts`/`plateau_note`/`build_wake_signals`（`learning_curves` 之前），规则如排查过程所述。
   - `tansuo/agent/prompts.py`：`PROMPT_VARS["tuning_wake_brief"]` 加 `wake_signals`；默认模板尾部加 `{{wake_signals}}`；`build_context_tuning_wake` 返回：

     ```python
     "wake_signals": "".join(f"\n⚠ {s}" for s in signals)
     ```

     `tuning_system` 失败处置节追加一条纪律：「wake brief 里以『系统警报』或『确定性收敛信号』开头的条目是代码检测的确定性结果（护栏），优先级高于你自己的判断，必须优先处置或明确回应。」
   - `tansuo/agent/loop.py` `AgentSupervisor.wake`：

     ```python
     signals = build_wake_signals(self.orch)
     if signals:
         self.journal.append(AGENT_WAKEUP, round=self.wake_count,
                             phase="signals", signals=signals)
         for s in signals:
             self.log(f"[护栏] {s}")
     ```

   - `web/src/lib/agentEvents.ts`：`agent_wakeup` 事件加 `phase === "signals"` 分支 → 「第 N 轮护栏信号：…」。
2. **人工编辑通道（#3）**
   - `tansuo/space.py::extend_envelope(ops)`：预扩 envelope（见排查过程第 5 条）。
   - `cli.py space patch --ops <JSON 数组> --rationale <理由>`：JSON 非法/非数组 → stderr rc2；`extend_envelope → apply_patch`；失败打印全部 errors + hint rc2；成功写回 `to_yaml()`、打印 `搜索空间已更新 → v{N}` + describe + 「注意：对之后新开的记录分区生效；已有分区的续跑仍使用该分区的空间快照。」
3. **环境线索（#5）**
   - `tansuo/runner.py`：`TrialFailedError` 增 `env_clues` 字段；`_KNOWN_SECURITY_PROCS`/`_scan_security_procs`（300s 缓存）/`collect_env_clues(cwd)`；`TRIAL_RETRY` 事件恒带 `env_clues`；退出码失败仅 `transient_like`（stderr 空）时带。
   - `tansuo/orchestrator.py`：模块级 `_fail_fields(e)` 统一提取 `reason/hint/detail`（`env_clues` 真值时附带），`_run_one` 与 `run_custom` 的 `TRIAL_FAIL` 落盘共用。
4. **Hyperband 验收（#2）**
   - `tests/acceptance_real_setup.py --pruner {median,hyperband}`：setup 完成、冒烟搜索前把 settings 的 pruner 段改写并打印「[ok] 验收注入」。
   - `tests/test_runtime_features.py::test_hyperband_widen`：见排查过程第 7 条；配套新子进程脚本 `CHILD_STEPS`（读 `TANSUO_TRIAL_CONFIG` 的 epochs 逐轮上报）与三参数空间 `SPACE_WITH_EPOCHS`。

## R · 实际效果（Result）

- **验证方式**：新增/扩展测试 + 全量回归 + 真实验收。
  - `test_runtime_features.py` 46 → **64**（`test_wake_signals` 11 项：exit_code 警报/类别混杂静默/超时点名 epochs/`suspicious_dims` 正反例/收敛信号触发与静默/`build_wake_signals` 汇总/wake brief 强制注入 ⚠/无信号逐字兼容；`test_env_clues` 4 项：磁盘余量/`trial_retry` 带线索/瞬时形态 `trial_fail` 带线索/stderr 有内容不带；`test_hyperband_widen` 3 项：中途 widen 成功/全完结/有试验越过旧上界 8 步）。
  - `test_space_patch.py` 34 → **38**（`test_extend_envelope`：agent 路径不 extend 时 widen 超 envelope 仍被拒、extend 后通过且 envelope 同步扩展、非法 op 跳过不误扩）。
  - `tests/e2e_cli_smoke.py` 31 → **39**（第 10 节：narrow 写回 v2 边界生效/人工 widen 超 envelope 且 env_high=80/未知参数 rc2/冻结致自由参数不足 rc2/非法 JSON rc2/分区生效语义提示）。
  - 全量回归：12 单测套件 **421 项断言**全绿（cohort 116 / runtime 64 / space_patch 38 / notify 32 / conditional_space 30 / compare 28 / prompts 28 / guardrails 21 / setup_guard 20 / project_store 16 / warmstart 16 / protocol 12），CLI 冒烟 39 项、Web 冒烟 82 项全绿；`npm run build`（tsc + vite）通过（agentEvents.ts 类型正确）。
  - Hyperband 真实验收（`python tests/acceptance_real_setup.py --dir <scratch> --train <scratch>\train_mnist.py --trials 5 --pruner hyperband`，真实 LLM 端点 + MNIST CPU）：新建项目脚手架 → setup agent 起草配置（耗时 1035s、65443 tokens，推断 epochs 3-15，探针 43.9s 校准 timeout 600s）→ 注入 `pruner: {type: hyperband, min_resource: 1, max_resource: auto, reduction_factor: 3}` 成功 → 冒烟搜索 5 次试验（301s）：**完结 4、剪枝 1（Hyperband 真实剪掉一次中途试验）、失败 0**，最优 val_acc=0.984（trial#0，adamw）；调参 agent 唤醒 2 轮（11773 tokens），退出码 0，全链路通过。
- **前后对比**：
  - 唤醒护栏：成片失败/长期停滞从「提示词建议、LLM 自觉」→「代码检测、⚠ 强制注入 wake brief、提示词规定优先级高于 agent 自身判断」；journal 增 `agent_wakeup phase="signals"` 审计事件，前端 Agent 页渲染「第 N 轮护栏信号」。
  - 空间编辑：用户从「只能手改 YAML 绕过校验」→「`cli.py space patch` 合规通道：人工可扩 envelope，其余校验（narrow⊆当前范围、冻死下限、int/log）一视同仁，版本与审计留痕」。
  - 疑难失败诊断：STAR #004 那类「退出码非零、stderr 空、单独复现正常」的失败，journal 里从无现场证据 → `trial_retry`/`trial_fail` 附 `env_clues`（disk_free_gb + security_procs），本机实测检出 `securityhealthsystray.exe`。
  - Hyperband：从「仅配置校验+工厂测试」→「中途 widen 的确定性测试（auto 推断跟随扩展）+ 验收脚本可注入 pruner + 真实验收跑通（有 1 次真实剪枝、0 失败）」。
- **副作用与代价**：1) 收敛信号要求完结数 ≥ window+2（默认 wake_every=2 时 ≥ 8 次）才可能触发，早期绝不扰动；2) `env_clues` 只附瞬时形态失败，确定性失败不带（设计如此，避免噪音）；3) `retry_on_fail≥1` 时瞬时失败会先记一条带线索的 `trial_retry` 再重试，journal 体积略增；4) `extend_envelope` 只改 envelope 边界，不触碰当前搜索范围，真正的 widen 仍由 apply_patch 落版本。
- **遗留问题与后续**：1) 「成片超时自动收空间」仍未做——当前链路是确定性警报 → agent 必须处置，人工可用 `space patch` 兜底；自动改空间待更保守的触发条件设计；2) STAR #004 根因待 `env_clues` 在真实运行中积累证据后定位（证据链已就位）；3) 收敛信号触发后 agent 是否 finish 仍由其判断（提示词要求给出理由），未做强制终止。
- **经验教训**：
  1. **增强型信号必须静默失败**：`build_wake_signals` 鸭子类型 + 整体异常吞掉返回 `[]`——测试替身缺属性、真实计算出错，唤醒主路径都毫发无损；给主流程加「增强项」时先设计好它坏掉时的行为。
  2. **有精确断言的模板做逐字兼容 = 新变量默认渲染空串**，而不是挪占位符位置——`test_prompts` 的 wake brief 断言因此零改动通过。
  3. **构造合成测试数据时控制非目标维度的组间均值差**：`suspicious_dims` 的 20% 相对阈值对任何数值维度一视同仁，batch 随手取值就把断言打挂；同理**测试步骤的顺序也是数据**——混杂检查污染了 journal，汇总断言必须排在污染之前。
  4. **Windows 系统命令输出是系统码页（简中=GBK）**：UTF-8 模式 Python 用 `text=True` 读 `tasklist` 会解码失败且错误发生在 reader 线程、表现隐蔽（功能静默降级为空清单）；读系统命令输出一律用字节模式 + `errors="replace"`。
  5. **argparse 加子-子命令先抄邻居**：`runs show/compare` 已带 `parents=[common]`，`space show/patch` 漏了就是 `unrecognized arguments`；CLI 的一致性靠对照既有结构而不是凭记忆。
  6. Edit 失败后重做要换**更长的真实内容锚点**，并在继续前跑 `ast.parse` 编译检查——本次行合并损坏（def 签名并入函数体）是在全包 import 失败时才暴露的。
