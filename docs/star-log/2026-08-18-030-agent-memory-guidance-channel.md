---
date: 2026-08-18
number: "030"
title: 监督 agent 跨轮失忆且人→agent 无喊话通道：wake brief 注入 last_note 跨轮记忆 + guidance.jsonl 指令通道原子消费
severity: medium
status: resolved
tags: [agent, 跨轮记忆, guidance, wake-brief, journal, 设计决策, 前端]
module: tansuo.agent / tansuo.web / web 前端
---

# 监督 agent 跨轮失忆且人→agent 无喊话通道：wake brief 注入 last_note 跨轮记忆 + guidance.jsonl 指令通道原子消费

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 的监督 agent（`tansuo/agent/`）与其 Web 前端（`web/src/`）。
- **环境**：Python 3.12 + Optuna TPE 编排；前端 React + Vite 8 + shadcn-ui；journal JSONL 审计（`tansuo/journal.py`）。
- **当时在做什么**：用户问「agent 功能上够用了吗」。核查代码后确认闭环完整，但按「真正可用的监督者」衡量有两个真实缺口（用户通过多选确认先补这两个）：
  1. **跨轮失忆**：wake brief（`tansuo/agent/prompts.py`）变量只有 `round_no/finished_count/budget_left/space_version/wake_signals`，**没有上一轮结论**。每轮唤醒 agent 从零靠 `get_study_summary` 重建认知。矛盾点：tuning_system 提示词要求「连续 2 轮唤醒都没有改进且 top 配置趋同才允许 finish」——但它根本不记得上一轮的判断。上一轮结论其实已落 journal（`AGENT_WAKEUP phase=end` 的 `note` 字段，截 300 字），只是没人读。
  2. **人→agent 没有喊话通道**：`inbox.jsonl`（`tansuo/orchestrator.py`）是人工**试验**插队队列，不是指令通道。搜索跑起来后用户无法告诉 agent「重点探 lr」「本轮别动空间」——只能干看。
- **问题表现**：无报错，属功能缺口。行为症状：① 每轮 brief 逐字相同（除计数），agent 可能反复横跳（上轮收窄 lr、下轮又放宽，无连贯性约束）；② 用户在 Agent 主屏没有任何输入入口。
- **影响范围**：监督质量（决策不连贯）与交互体验（人无法干预方向）；不影响搜索正确性。

## T · 目标（Task）

- **要达成什么**：
  1. wake brief 带上一轮结论（`{{last_note}}`），resume 跨进程新实例也能恢复；
  2. 搜索运行中用户可提交文字指令，下一轮唤醒注入 brief（`{{guidance}}`），全程 journal 可审计、前端时间线可见。
- **验收标准**：
  - `python tests/test_runtime_features.py`、`python tests/test_prompts.py`、`python tests/e2e_web_smoke.py` 全绿；`cd web && npm run build`（tsc -b）通过；
  - guidance 审计事件**不得**污染 `agent_token_summary()` 的 rounds 计数；
  - 用户指令与护栏信号一样，不允许被用户的模板编辑静默丢弃（兜底追加）。
- **约束条件**：不新造轮子——复用既有范式：inbox 原子认领（`os.replace`）、wake_signals 护栏兜底、PROMPT_VARS 变量体系（前端 PromptsPage 自动渲染徽章）、journal `agent_` 前缀匹配（`agent_events()` 自动收录新 kind，`/api/agent/events` 零端点改动）。不改 `report.py`；`wake_count` 跨进程恢复本期不做。

## A · 解决方案（Action）

### 设计要点（先探查后动手）

3 个并行探查确认了全部复用点与两处暗礁：

1. **journal kind 前缀匹配**：`agent_events()` 用 `startswith("agent_")`，新 kind `agent_guidance` 自动进事件流；但 `agent_token_summary()`（`tansuo/journal.py`）用 `e.get("phase") == "end"` 过滤求和——**guidance 审计事件绝不能带 `phase` 字段**，否则 rounds 虚增（常量处留了注释警示）。
2. **前端分组静默丢弃未知 kind**：`groupTune`/`groupSetup`（`web/src/lib/agentEvents.ts`）对非 wakeup、非 ACTION_KINDS 的事件直接跳过，`KIND_META` 缺 key 时索引会崩——新 kind 必须显式加四处（KIND_META、context 类型、group 分支、eventBody case）。
3. **guidance 只在 wake 消费**，无「被拒/失败放回」语义（纯文本），照搬 inbox 的原子认领但不做 requeue；端点**仅运行中接收**（空闲 400）——指令的生命周期依附运行中的会话，无唤醒则无人消费，与 /api/custom 的「空闲也收（派发即时执行）」语义有意区分。

### 最终方案（后端→前端→测试）

#### Part 1 跨轮记忆（last_note）

1. `tansuo/agent/loop.py` — `AgentSupervisor`：
   - `__init__` 末尾 `self.last_note = self._load_last_note()`；
   - 新增 `_load_last_note()`：过滤 journal 里最后一条 `agent_wakeup(phase=end)` 的 `note`（写法照搬 `agent_token_summary` 的过滤），无记录返回空串——resume 跨进程新实例也恢复上一进程的结论；
   - `wake()` 在 `phase=end` append 之后 `self.last_note = (last_text or "")[:300]`。
2. `tansuo/agent/skills/tune.py` — `TuneSkill.__init__` 加默认关键字参数 `last_note: str = ""`、`guidance: str = ""`（旧位置参数调用全部兼容）；`opening_message()` 传给 `build_context_tuning_wake`。
3. `tansuo/agent/prompts.py`：
   - `PROMPT_VARS["tuning_wake_brief"]` 追加 `last_note`、`guidance`（前端 PromptsPage 徽章自动反映）；
   - `build_context_tuning_wake(..., last_note="", guidance="")`：`last_note` 空时占位「（首轮，尚无上一轮结论）」（避免模板悬空冒号）；`guidance` 有则 `"\n👤 用户指令：\n" + text`，无则空串；
   - 默认模板加两行：`上一轮你的结论：{{last_note}}。` 与连贯性指引「本轮决策应与上一轮结论保持连贯（避免反复横跳），除非新证据推翻它」，末尾 `{{wake_signals}}{{guidance}}`。
   - **last_note 不做护栏兜底**（普通上下文变量，用户改模板删了就删了），guidance 做（人工输入，与护栏同级）——这是有意的语义区分。

#### Part 2 人→agent 指令通道（guidance）

1. `tansuo/journal.py`：新常量 `AGENT_GUIDANCE = "agent_guidance"`（带「不带 phase」警示注释）。
2. `tansuo/agent/loop.py` — `_consume_guidance()`：`os.replace(guidance.jsonl, guidance.processing.jsonl)` 原子认领 → 逐行 `json.loads` 取 `text`（损坏行跳过并打日志）→ `unlink` → 有内容则 `journal.append(AGENT_GUIDANCE, round=self.wake_count, texts=[...])`（**不写 phase**）并返回拼接原文。`wake()` 在 signals 块后消费、传入 TuneSkill。
3. `tune.py` `opening_message()` 兜底（逐字照搬 signals 兜底范式）：

   ```python
   # 人工指令同等兜底：用户输入不允许被模板编辑静默丢弃（范式同上）
   guidance = ctx.get("guidance") or ""
   if guidance and guidance not in text:
       text += guidance
   ```

4. `tansuo/web/app.py` — `POST /api/agent/guidance`：body `{text}`；空/超 2000 字符 → 400；非运行中 → 400「搜索未运行，指令在搜索运行时接收、下一轮唤醒注入」；运行中 → 仿 /api/custom 定位**正在跑的分区**（`load_cohort` + `apply_cohort`，指纹可能已变不能写最新分区），追加 `{"text", "queued_at"}` 到该分区 `guidance.jsonl`。
5. `_preview_context`（prompts 预览）`tuning_wake_brief` 分支补 `last_note`/`guidance` 样例值——PromptsPage 预览无 missing_vars，前端零改动。

#### Part 3 前端

- `web/src/lib/api.ts`：`guidance: (body: {text: string}) => http("/agent/guidance", {method: "POST", ...})`。
- `web/src/lib/agentEvents.ts` 四处：`KIND_META.agent_guidance`（青色「人工指令」）、`AgentSegment.context.guidance?: string[]`、`groupTune`/`groupSetup` 显式分支（guidance 写在 wakeup-start 之后、end 之前，`cur` 已开段，直接 `ensure(...).context.guidance = texts`）、`eventBody` case。
- 新组件 `web/src/components/GuidanceComposer.tsx`：textarea + 发送按钮，props `{running}`；非运行中禁用并提示；提交 toast 成功/失败后清空。
- `web/src/pages/AgentPage.tsx`：新加 `usePolling(api.runStatus, 5000)`；GuidanceComposer 挂在 setup 提示卡与最新一轮卡片之间。
- `web/src/components/AgentTimeline.tsx` SeenBlock：仿 ⚠ signals 徽章渲染 👤 cyan 指令徽章（「看到什么」一节），空态判断同步纳入 guidance。

### 回归中踩到的坑（真实失败，已修）

| 现象 | 原因 | 处理 |
|------|------|------|
| `pytest tests/test_prompts.py` 一堆 `fixture 'tmp' not found` | 这些测试文件是**独立脚本**（`tmp: Path` 由 `__main__` 用 `tempfile.TemporaryDirectory()` 喂），不是 pytest 套件；仓库没有 conftest.py | 按文件头注释直跑 `python tests/test_prompts.py`（既有现象，非本次引入） |
| `test_default_content` 逐字比对旧简报文本失败 | 默认 wake brief 合法演进（加 last_note 行与连贯性指引），旧断言写死了上一版全文 | 更新断言为新全文，并新增 last_note/guidance 注入、空 guidance 空串两条断言 |
| `test_wake_signals` 的 `brief5.endswith("再决定本轮动作。")` 失败 | 同上，模板尾巴变了 | 改为「不含 ⚠/👤 且含首轮结论占位」的语义断言；同段新增「覆盖模板缺 {{guidance}} 时兜底追加」断言 |

## R · 实际效果（Result）

- **验证方式与结果**：
  - `cd web && npm run build`（tsc -b + vite）通过；
  - `python tests/test_prompts.py` 全绿（31 项断言，新增 3 项）；
  - `python tests/test_runtime_features.py` 全绿（75 项断言）——新增 `test_supervisor_memory` 段 9 项：首轮 last_note 为空 → wake 后立即更新 → 新实例从 journal 恢复（resume 不失忆）→ guidance 审计事件带 round+texts 且**不带 phase** → guidance.jsonl 消费后删除 → brief 含 👤 指令与上一轮结论 → `agent_token_summary().rounds` 不被 guidance 虚增；另有 guidance 模板兜底断言 1 项；
  - `python tests/e2e_web_smoke.py` 全绿（**141 项断言**，新增第 21 段 8 项：空闲 400 + 说明、空白拒、超 2000 拒、运行中排队返回分区 id、写入分区=正在运行的分区（不是最新分区）、guidance.jsonl 落盘运行分区、条目含原文与 queued_at）。
- **前后对比**：wake brief 从「每轮相同」变为带上一轮结论 + 连贯性约束；Agent 主屏从无输入入口变为运行中可下达指令；新 kind 走 journal 前缀匹配自动进 `/api/agent/events`，后端零路由改动即被时间线收录。
- **副作用与代价**：默认 wake brief 文本变了（用户若覆盖过模板则不受影响）；AgentPage 多一路 5s runStatus 轮询（开销可忽略）。
- **遗留问题与后续**：
  - `wake_count` 跨进程不恢复（max_wake_rounds 是每进程口径）——用户未选，本期不做；恢复时可取 journal AGENT_WAKEUP round 最大值；
  - `report.py` 最终报告暂不展示 guidance（journal 已是审计源）；
  - 运行中消费链（真 LLM 读到指令并回应）靠手动验收。
- **经验教训**：
  1. 给 journal 加新 kind 前先查**所有按字段过滤的读取方**——`agent_token_summary` 只认 `phase=end`，新事件带错字段就会污染统计；前缀匹配收录 + 字段过滤求和的组合里，「不带某字段」本身就是语义。
  2. 前端事件分组对未知 kind 是**静默丢弃**而非报错——新 kind 不加显式分支就「写了审计但界面看不到」，排查方向容易被带偏；此类渲染管线加 kind 要对照 KIND_META/group/eventBody 全链路。
  3. 「人工输入」与「系统上下文」的模板兜底语义应该不同：护栏信号与用户指令都属「不允许被模板编辑绕过」，普通上下文变量（last_note）则允许用户删——同级照抄前先问这个值能不能被静默丢弃。
