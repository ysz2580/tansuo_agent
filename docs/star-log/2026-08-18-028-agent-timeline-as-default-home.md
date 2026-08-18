---
date: 2026-08-18
number: "028"
title: 界面以 agent 为主轴：监督唤醒时间线做成默认首页 + 头部 agent 状态胶囊
severity: medium
status: resolved
tags: [前端, agent, 信息架构, journal, 事件分组]
module: web 前端（AgentPage / AgentTimeline / App）
---

# 界面以 agent 为主轴：监督唤醒时间线做成默认首页 + 头部 agent 状态胶囊

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 智能超参数调优系统（Optuna TPE 贝叶斯搜索 + LLM 监督 agent）；本次改动全部在 Web 前端（`web/src/`，React 19 + Vite + shadcn/ui），后端零改动
- **环境**：Windows 11；Node 20 + Vite 8（rolldown）+ TypeScript；后端 FastAPI 托管 `web/dist` 静态产物（`tansuo/web/app.py` 挂载 `_DIST = PROJECT_ROOT/"web"/"dist"`）
- **当时在做什么**：用户试用后提出定位错位问题——「我这个项目是以 agent 为主吧，为啥现在界面看起来 agent 只有一小部分」
- **问题表现**：界面是**搜索任务视角**而非 agent 视角，具体症状：
  1. 七个页签里 Agent 只是第 4 个，默认首页是「仪表盘」（trial 曲线/统计）；
  2. agent 的活动形态是间歇的（setup 一生一次、监督 agent 每 N 次试验醒一轮），但第一版把 agent 产物**按数据类型**拆散到各页：空间补丁在「搜索空间」页、提示词在「提示词」页、事件流在「Agent」页里还内嵌二级 Tabs（调参会话 / setup）；
  3. 「Agent」页内只是一条平铺的 `<ol>` 事件流（Badge + 单行文本），看不出「一轮唤醒做了什么决策」的结构，用户无法回答「agent 到底起了什么作用」。
- **影响范围**：不阻塞功能，但产品叙事与实现错位——「引擎是 agent 驱动、界面却是仪表盘驱动」，新用户进来先看到的是 Optuna 统计而不是 agent 的判断与动作
- **复现步骤**：1) `python cli.py web` 打开 :8000；2) 默认落在仪表盘页；3) 点到 Agent 页再点二级 Tabs 才能看到事件流——agent 信息藏在三层导航之下

## T · 目标（Task）

- **要达成什么**：把 agent 活动做成界面主轴——Agent 页重做为默认首页，按唤醒轮次讲叙事（看到什么 → 判断什么 → 做了什么），setup 进展与调参监督并入同一时间线形态；头部加 agent 状态胶囊
- **验收标准**：
  1. 打开 :8000 默认落在 Agent 页，无历史时两节各有空态 CTA；
  2. 有调参历史时每轮唤醒一张卡，三段叙事（看到/判断/做了）字段来源正确；
  3. `npm run build`（含 tsc -b）通过；`tests/e2e_web_smoke.py` 133 项全绿；
  4. 后端零改动、零新增依赖
- **约束条件**：不改 journal 落盘格式（历史 journal 必须原样可读）；不改事件端点结构；不引入新的 UI 依赖（现有 shadcn 组件库没有 Accordion/Timeline/Steps，不能为装组件加包）

## A · 解决方案（Action）

### 排查过程

1. 先用两个并行 Explore agent 摸清数据面与界面面：`tansuo/journal.py` 的事件模型、`tansuo/agent/loop.py` 的落盘字段、`tansuo/web/app.py` 的端点，以及 `web/src` 现有页面结构。
2. 关键事实核对（决定「纯前端可行」）：
   - `agent_wakeup` 三相字段齐全：**start**（无 phase 字段）带 `round/budget_left/space_version`；**signals** 带护栏信号列表；**end** 带 `note`（本轮结论，`loop.py` 内截断 300 字）+ `input_tokens/output_tokens` 增量。
   - `agent_tool_call` **无 round 字段、无结果文本**（只有 `mode/tool/input/allowed`）——轮次归属只能靠时序边界推断。
   - journal `ts` 格式 `%Y-%m-%d %H:%M:%S`：**无秒下精度、无时区**，同一秒内多条事件无法靠 ts 排序——分组只能依赖数组顺序（journal.jsonl 行序即追加序）。
   - setup 与 tune 事件**同构**：setup 用 `mode="setup"` 单轮会话（`round=1`），`finish` 事件带 `summary`（≤500 字）；`/api/setup/events` 返回全量事件、`/api/agent/events` 仅返回 `agent_*` 开头事件。
3. 据此确定取舍：**纯前端分组**，不改后端补 round 字段——补字段要动 journal 写入层且历史数据仍是旧格式，前端按边界推断对两种数据都成立。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 后端给 `agent_tool_call` 补 round 字段 | 放弃 | 需改 journal 写入层；历史 journal 无此字段，前端仍需兼容按边界推断——等于两条路都要维护 |
| 用 ts 排序后分组 | 放弃 | ts 只到秒级且无时区，同秒多条事件顺序会错乱；只能信任数组顺序 |
| 装 shadcn Accordion/Timeline 组件做轮次折叠 | 放弃 | 项目没有这些组件，为一个页面新增依赖不值；v1 全展开已够读，真要折叠用原生 `<details>` 即可 |
| 在事件流里逐条渲染但加分轮分隔线 | 部分有效但放弃 | 只是视觉分组，「看到/判断/做了」的语义结构仍不可见，讲不出决策叙事 |

### 最终方案

1. **数据层 `groupByRounds()`**（`web/src/lib/agentEvents.ts`）：扫描事件数组，按时序边界分组成 `AgentSegment[]`：
   - tune 流：wakeup 无 phase（=start）开新段并记 `budget_left/space_version`；`phase=signals` 把护栏信号附入当前段；`phase=end` 附 `note/tokens` 并闭段；段间 `agent_tool_call/agent_permission/agent_error` 归入当前段。
   - setup 流（自动识别：流内出现 `mode="setup"` 事件）：整条流一段——`session_start` 开段带训练脚本，action 累入，`wakeup(mode=setup, phase=end)` 附 tokens，`finish.summary` 作 judgment。
   - 防御兜底：end/signals/action 出现在 wakeup start 之前时兜底开段（round=0），保证任何畸形流都不丢事件。
   - 段结构：`{mode, round, startTs?, endTs?, context:{round?, budgetLeft?, spaceVersion?, signals?, trainScript?}, judgment?, toolCalls, tokens?}`。

2. **新组件 `AgentTimeline.tsx`**（`web/src/components/`）：每段一张 Card，三段叙事：
   - **看到什么**：结构化上下文（剩余预算/空间版本；setup 显示训练脚本与任务）+ 护栏信号 amber Badge；
   - **判断什么**：`note`（tune）/ `summary`（setup），`<pre whitespace-pre-wrap>` 渲染；
   - **做了什么**：tool_calls 列表，其中 `edit_search_space` 高亮展开 `input.ops`（逐条 op·param→范围/取值）+ `rationale`——空间改动就此并入时间线叙事，不再只散落在「搜索空间」页。
   - 复用既有 `KIND_META/eventBody` 做非编辑类动作的单条渲染；`segments` 为空时渲染调用方给的 `emptyHint`。

3. **`AgentPage.tsx` 重写**：删内嵌二级 Tabs，单页两节——「配置 agent（setup）」复用 SetupPanel（自管控件/日志/时间线），「监督会话」轮询 `agentEvents(cohort)` → `groupByRounds` → `AgentTimeline`，顶部统计行（轮数 + 累计 tokens）保留，两节各有空态 CTA。

4. **`SetupPanel.tsx`**：原事件 `<ol>` 平铺列表替换为 `AgentTimeline`（setup 模式单段）；控件、互斥、轮询逻辑不动；补 setup 空态提示卡。

5. **`App.tsx`**：`defaultValue="dashboard"` → `"agent"`，TabsList/TabsContent 中 agent 提序到第一（Radix 不依赖顺序，提序为视觉一致）；`RunIndicator` 替换为 `AgentIndicator` 胶囊（双 usePolling：setupStatus + runStatus 各 3s），优先级：setup 进行中 → 「配置 agent 进行中」（Loader2 旋转）；否则搜索运行中且 args 不含 `--no-agent` → 「搜索运行中 · agent 监督」；含 `--no-agent` → 「搜索运行中（纯 Optuna）」；否则 → muted「agent 待命」。胶囊不显示轮次（轮次在默认 Agent 页已可见，避免头部额外轮询事件流）。

6. 重建并提交 `web/dist`。

## R · 实际效果（Result）

- **验证方式**：
  - `cd web && npm run build`（tsc -b 类型检查 + vite 产物）通过；
  - `python tests/e2e_web_smoke.py` 133 项断言全绿（纯前端改动，端点未变，冒烟覆盖首屏与全部 API）；
  - 数据面字段逐一对照源码核实：`tansuo/agent/loop.py:158-186`（wakeup 三相）、`loop.py:219-226`（setup wakeup end + finish.summary）、`tansuo/agent/skills/tune.py:58-83`（edit_search_space 的 ops/rationale schema）、`cli.py:772`（`--no-agent` 标志确实存在且进 runStatus.args）。
- **前后对比**：agent 信息从「第三个页签的二级 Tab 里的平铺事件流」变为「打开即见的默认首页、按轮次的三段叙事卡」；头部状态从「只在搜索运行时出现的 Badge」变为「常驻 agent 状态胶囊（含 setup 进行中态）」；后端改动 0 行、新增依赖 0 个。
- **副作用与代价**：
  - **数据缺口诚实标注**：中间推理文本与工具结果文本不落盘（journal 只记工具调用入参不记返回值），「做了什么」一节看不到工具返回；`note` 截断 300 字、「判断什么」可能不完整——若日后要更完整需放宽 `tansuo/agent/loop.py:182` 的截断并把工具结果落盘，本期不做；
  - 分组依赖「start 开段 / end 闭段」边界，若进程在轮中被杀，会出现未闭合段——防御逻辑把它渲染为只有「看到/做了」没有「判断」的卡，不丢事件但会看到半段，属预期；
  - 胶囊的「agent 监督」判断只看 `runStatus.args` 是否含 `--no-agent`，若 agent 因端点故障自动降级为纯 Optuna 巡航（`cli.py:179`），胶囊仍显示「agent 监督」——降级是小概率事件且降级时日志有醒目提示，接受此误差。
- **遗留问题与后续**：无阻塞项。可选后续：轮次卡折叠（原生 `<details>`）、工具结果落盘后在「做了什么」展开结果文本。
- **经验教训**：
  1. **先核数据面再动界面**——本次两个 Explore agent 提前确认了「ts 不能排序、tool_call 无 round、note 300 字截断、setup/tune 同构」四个事实，避免了写到一半发现要改后端；
  2. **时序分组要防御畸形流**——真实 journal 会有进程中途被杀的半轮数据，分组函数必须兜底开段而不是假设 start/end 严格配对；
  3. **界面主轴应与引擎主轴一致**：agent 是驱动者就应占据默认视野，统计视图退居辅助页签——产品叙事错位本身就是 bug。
