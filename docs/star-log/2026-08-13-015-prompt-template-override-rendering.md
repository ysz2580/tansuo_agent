---
date: 2026-08-13
number: "015"
title: Agent 提示词硬编码导致监督策略迭代只能改代码：重构为 {{var}} 模板引擎（弃用 str.format），前端可编辑、版本化、可回滚
severity: low
status: resolved
tags: [提示词, 模板渲染, 设计决策, web后端, 版本管理]
module: tansuo/agent/prompts.py · tansuo/agent/prompt_store.py · tansuo/web/app.py · web 前端 PromptsPage
---

# Agent 提示词硬编码导致监督策略迭代只能改代码：重构为 {{var}} 模板引擎（弃用 str.format），前端可编辑、版本化、可回滚

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 智能超参数调节 agent。三条提示词——`tuning_system`（监督者 system）、`tuning_wake_brief`（每轮唤醒简报）、`setup_system`（配置生成）——以 f-string 函数形式硬编码在 `tansuo/agent/prompts.py`
- **环境**：Windows，Python 3.14.6；前端 React 19 + Vite 8 + TypeScript 6 + Tailwind 4 + shadcn/ui（单 `radix-ui` 包），无 Monaco/CodeMirror
- **当时在做什么**：三个排队方向（跨分区对比、环境审计、新分区热启动）全部交付后，用户提出新需求："agent的提示词管理和迭代，在前端可以调控吗，前后端同步"。项目的终极目标是 agent 替代人工调参工程师，而**提示词是调节 agent 监督行为的第一旋钮**——措辞、纪律约束、策略倾向都写在提示词里
- **问题表现**：这不是报错型 bug，而是能力缺口，但有一个潜在的技术雷区：

  1. 三条提示词是 Python f-string 函数，用户想改一个词（例如把"预算意识"段落改写、强调先压 lr 上界）必须编辑 Python 源码，无配置入口、无版本、无回滚、无审计；
  2. 一旦把模板交给用户编辑，**模板渲染引擎的选择就是硬约束**：提示词正文里极可能出现字面 `{` `}`（例如给 LLM 展示协议行的 JSON 示例 `{"type": "final", ...}`）。若用 `str.format`，字面花括号会被当作占位符解析，直接抛 `ValueError`/`KeyError`——此风险在设计期识别，未实际触发，但决定了引擎选型。

- **影响范围**：监督策略迭代被锁死在"改代码 + 提交 + 重启"流程里，与"配置即文档、运行中可调"的产品方向（settings/space 均已 Web 可编辑）不一致
- **复现步骤**（限制确认）：1) 打开 `tansuo/agent/prompts.py`，三条提示词均为 f-string 函数；2) `settings.yaml` 无任何提示词字段；3) Web 端无任何提示词相关路由

## T · 目标（Task）

- **要达成什么**：提示词成为运行时可配置项——Web 前端编辑、落盘持久化（`prompts.yaml`）、agent 下次唤醒即用新模板，带版本号、迭代历史与一键回滚
- **验收标准**：
  1. **默认模板渲染输出与重构前三条函数逐字节一致**（零回归的硬证明）；
  2. `prompts.yaml` 缺失或覆盖为空串 → 行为与未配置时完全一致；
  3. 新增单测 + e2e web 冒烟全绿，既有 8 个单测套件零回归；
  4. 前端保存后 GET 立即反映覆盖，预览能列出未填充变量
- **约束条件**：不改 `AgentLoop` 的注入契约（`loop.py` 只接收渲染好的字符串，提示词只经 `Skill.system_prompt()` / `opening_message()` 流入）；不新增 Settings 字段或 env 变量；不引入 Monaco（沿用原生 Textarea）；提示词是 agent 全局行为，**不进记录分区**

## A · 解决方案（Action）

### 排查过程

1. **先找注入点**：通读 agent 运行时链路，确认提示词只在两处进入对话——`TuneSkill.system_prompt()` / `opening_message()`（`tansuo/agent/skills/tune.py`）与 `SetupSkill.system_prompt()`（`tansuo/agent/skills/config.py`）；`AgentLoop.run()`（`tansuo/agent/loop.py:47,49,57-59`）对提示词内容完全无感。结论：**改动收敛在 Skill 层，循环引擎一行不动**。
2. **再找存储锚点**：`Settings.source_path` 由 `load_settings` 赋值（`tansuo/config.py:298`），已携带 settings 文件路径——据此解析同目录 `prompts.yaml`（`source_path.with_name("prompts.yaml")`），无需新增任何配置字段。
3. **渲染引擎选型**：核心矛盾是"用户可编辑长文本"与"模板插值"共存。`str.format` 对字面花括号零容忍（提示词里的 JSON 示例必炸）；且 `{var}` 单花号在自然语言文本里太容易误撞。最终选 `{{var}}` 双花括号 + 正则替换，且**未知占位符原样保留**——缺上下文时不崩，预览接口的 `missing_vars` 能直接指出缺哪个变量。
4. **回归信心构建**：重构是"纯搬运 + 插值化"，最大的风险是搬运时悄悄改字。写了对比脚本，用 `git show HEAD:tansuo/agent/prompts.py` 取出旧文件，对三组样例输入分别调旧函数与新 `render_prompt`，逐字节比较——**三条全部 IDENTICAL** 才敢往下接线。（期间小弯路：对比用的临时目录清理命令 `Remove-Item "$env:TEMP\..."` 被沙箱按系统路径拦截，改为专用临时目录 + 单独一步清理后通过。）
5. **审计轨放哪**：最初考虑过写分区 journal，随即否决——journal 是分区作用域，而提示词是全局行为；最终 history 内建在 `prompts.yaml` 自身（`version` 递增 + 每次保存全量快照），文件自带完整审计，随配置一起走。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| `str.format` / f-string 模板化 | 放弃 | 用户可编辑的提示词正文会包含字面 `{` `}`（JSON 协议示例等），`str.format` 将其解析为占位符，直接抛 `ValueError`/`KeyError`；单花括号占位符也太容易与正文误撞 |
| 提示词并入 `settings.yaml` | 放弃 | settings 已有 env 展开与写回 API；三条长文本 + 迭代历史塞进去会让主配置膨胀、历史难维护，故独立成同目录 `prompts.yaml` |
| 审计写分区 journal | 放弃 | journal 分区作用域与提示词的全局性不匹配；改由 `prompts.yaml` 内建 `history` 承载 |
| 正则渲染 `{{var}}` + 未知占位符原样保留 | 有效，采用 | 对字面花括号免疫；缺变量时原样保留并在预览中暴露，可诊断 |

### 最终方案

1. **重构 `tansuo/agent/prompts.py` 为三层**：`DEFAULT_PROMPTS`（三条模板，动态值换成 `{{var}}` 占位符）+ `build_context_*`（三个上下文构建函数，取值逻辑原样搬运）+ 渲染引擎：

   ```python
   _PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

   def render_prompt(name: str, context: dict, overrides: dict | None = None) -> str:
       overrides = overrides or {}
       template = overrides.get(name) or DEFAULT_PROMPTS[name]  # 空覆盖=出厂默认
       def _sub(m: re.Match) -> str:
           key = m.group(1)
           return str(context[key]) if key in context else m.group(0)  # 未知占位符保留
       return _PLACEHOLDER.sub(_sub, template)
   ```

   旧函数签名（`tuning_system_prompt` 等）保留为薄包装，存量调用点零改动。
2. **新建 `tansuo/agent/prompt_store.py`**：`save_override` 每次保存 `version += 1` 并向 `history` 追加全量快照（`ts/version/which/rationale/source/text/hash`，text 即该版本生效文本，回滚=载入旧快照再保存）；原子写盘（`tempfile.mkstemp` + `os.replace`）；校验 `which ∈ PROMPT_NAMES`、`rationale` 必填（镜像空间编辑的 rationale 硬要求）、长度上限 20000。`text=""` 即恢复出厂，仍计版本留痕。
3. **Skill 接线**：`TuneSkill.__init__` / `SetupSkill.__init__` 各加一行 `self._prompts = load_overrides(settings)`（构造时读一次，开销可忽略）；`system_prompt()` / `opening_message()` 改调 `render_prompt(name, build_context_*(...), self._prompts)`。无 `prompts.yaml` 时 `load_overrides` 返回 `{}` → 全走默认。
4. **Web 三路由**（`tansuo/web/app.py`）：`GET /api/config/prompts`（返回 version + 每条的 override/default/effective/vars + history）、`POST /api/config/prompts/preview`（best-effort 上下文渲染，取不到的运行时变量填样本值，返回 `rendered` + `missing_vars`）、`POST /api/config/prompts/save`（`PromptStoreError` → HTTP 400）。
5. **前端 `web/src/pages/PromptsPage.tsx`**：选择器 + 可用变量 Badges + Textarea 编辑器 + 预览渲染 + 保存（Dialog 收 rationale）+ 恢复出厂 + 只读出厂默认对照 + 迭代历史「载入此版本」。App.tsx 挂「提示词」Tab（**不加 cohort key**——全局配置，仿 ComparePage）。
6. **配套**：`demo/configs/prompts.yaml` 文档化模板（空覆盖 + 占位符语法说明）、settings.yaml 注释指引、README 更新、`tests/test_prompts.py`（28 断言）与 `tests/e2e_web_smoke.py` 第 12 步（9 断言，含空 rationale 400、恢复出厂仍计版本）。

### 交付过程中的两个真实报错

- **JSX 里写 `{{...}}` 字面量的编译错误**：页面说明文案想显示字符串 `{{变量}}`，误写成 `{{"{{变量}}"}}`——外层 `{{` 被 JSX 解析为对象字面量，构建失败：

  ```
  src/pages/PromptsPage.tsx(211,49): error TS1005: ':' expected
  ```

  修复：改为单个表达式 `{"{{变量}}"}`。双花括号在本功能里同时是"模板占位符语法"和"JSX 表达式定界符"，两种语义极易混淆，这是本功能唯一的前端坑。
- **测试脚本的 YAML 转义错误**（与 STAR #003 同类）：web 功能测试拼 settings YAML 时用 `f'command: ["{sys.executable}", ...]'`，Windows 反斜杠进入双引号标量，PyYAML 报 `found unknown escape character 'p'`。修复：`Path(sys.executable).as_posix()`。是测试脚本 bug，非应用 bug。

## R · 实际效果（Result）

- **验证方式**：
  1. 字节一致对比：新旧实现对三组样例输入输出**三条全部 IDENTICAL**；
  2. 全量回归：9 个单测套件 296 断言（test_cohort 110 / test_compare 22 / test_warmstart 16 / test_prompts 28 / test_conditional_space 30 / test_guardrails 21 / test_protocol 12 / test_runtime_features 23 / test_space_patch 34）+ e2e_cli_smoke 31 + e2e_web_smoke 44（含提示词 9 断言）全绿；`npm run build` 通过；
  3. 提交 `2c5129c`（18 文件，+1055 行），经仓库代理路径一次推送成功。
- **前后对比**：从"改一个词 = 编辑 Python + 提交 + 重启"变为"网页编辑 → 填理由保存 → 下次 agent 唤醒即生效"；每次改动自动 version +1 并留全量快照，历史可一键载入回滚；无 `prompts.yaml` 的存量部署行为零变化（字节一致已证）。
- **副作用与代价**：覆盖在 Skill 构造时读取一次，保存后**本轮已开始的对话不生效、下轮唤醒生效**——这是设计上的取舍（不做热替换，避免一轮对话内提示词不一致）；预览接口的运行时变量（如 `finished_count`）用样本值填充，预览≠真实运行上下文，前端已明示。
- **遗留问题与后续**：无。若未来需要按实验/分区隔离提示词，可在 `prompts.yaml` 加作用域维度，当前有意保持全局。
- **经验教训**：
  1. **用户可编辑的长文本模板，永远不要用 `str.format`**——字面花括号必然出现（JSON 示例、代码片段），`{{var}}` + 正则 + 未知占位符保留是可诊断、不崩溃的组合；
  2. "与 git 历史中的旧实现逐字节对比"是纯重构最便宜的信心来源，先跑通对比再接线改调用点；
  3. 一个功能里同时存在两套"双花括号"语义（模板占位符 / JSX 表达式）时，写展示文案前先想清楚解析器看到的是哪一种；
  4. **STAR 日志本身的教训**：交付顺利不构成不记录的理由——收录标准是"是否属于设计权衡/优化"，不是"是否踩了坑"。本条记录就是在用户追问"这不是优化吗"之后补写的。
