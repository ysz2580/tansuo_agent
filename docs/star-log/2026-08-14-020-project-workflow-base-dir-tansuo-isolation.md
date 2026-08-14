---
date: 2026-08-14
number: "020"
title: 项目管理工作流（新建/打开项目 + setup agent Web 化）的设计权衡，及落地前暴露的致命 base_dir 缺陷
severity: high
status: resolved
tags: [设计决策, 项目管理, web后端, 子进程, 路径解析]
module: tansuo/web + 前端
---

# 项目管理工作流（新建/打开项目 + setup agent Web 化）的设计权衡，及落地前暴露的致命 base_dir 缺陷

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 的 Web 后端（`tansuo/web/app.py`）与 React 前端（`web/src/`）
- **环境**：Windows 11，Python 3.14.6，FastAPI + uvicorn 单进程；前端 React + Vite 8 + shadcn/ui
- **当时在做什么**：用户指出界面明显缺「重新读取某个项目」的操作——不能新建项目、不能打开一个包含数据集和主训练代码的目录、再让 agent 在里面构建探索目录并配置。现状核实：系统整体是「单仓库单实验」假设，没有任何"项目"抽象。
- **问题表现**：
  1. `SETTINGS_PATH`/`SPACE_PATH` 曾是模块导入时冻结的常量（STAR #010 已改为 env 兜底），运行期无法切换实验——只能重启 `cli.py web --settings X`；
  2. `cli.py setup --train <脚本>`（配置 agent：读训练脚本起草 settings + 搜索空间 + 跑探针）只有 CLI 入口，Web 无法触发；
  3. **规划阶段暴露的致命缺陷**：`_load_for` 把 `abs_data_dir(settings, PROJECT_ROOT)` 的 `base_dir` 硬编码为 `PROJECT_ROOT`（代码安装目录 `e:\tansuo_agent`）。对 demo 恰好成立（`data_dir: demo/data`），但一旦允许用户在任意目录新建项目，其相对 `data_dir` 会被解析到代码安装目录下——**数据全部错位**。同理 `code_fingerprint(settings, PROJECT_ROOT)` 与 `RunManager.start()` 的 `cwd=PROJECT_ROOT` 都隐含"项目 = 代码根"假设。
- **影响范围**：不修 base_dir 就引入多项目，新项目的运行记录/数据库/日志会写进代码仓库目录，且与 CLI 子进程（按 cwd 解析相对路径）两边视图分裂
- **复现步骤**：1) 假设在 `D:\my_exp` 建项目，settings 写 `data_dir: data`；2) Web 端解析成 `e:\tansuo_agent\data`；3) 子进程 cwd 若也是代码根则同样错位，若 cwd 是项目目录则两边解析不一致

## T · 目标（Task）

- **要达成什么**：Web UI 完整的项目管理工作流——①新建项目（在用户目录内脚手架 `.tansuo/`）②打开既有项目目录 ③让配置 agent 在其中生成配置；后端支持任意目录的项目，路径解析全部正确
- **验收标准**：既有用户零破坏（`cli.py web --settings X` 与直起 uvicorn 行为不变）；新项目的数据确实落在 `<项目>/.tansuo/data` 且 Web/CLI 两进程视图一致；全量回归绿
- **约束条件**：不碰用户项目里的原有文件（训练脚本、数据集原位保留）；注册表损坏不能炸掉 Web 后端；搜索/配置/切换三方不能并发互相踩踏

## A · 解决方案（Action）

### 排查过程

1. 规划时先问"项目目录模型怎么定"。发现 `abs_data_dir(settings, base_dir)` 的 `base_dir` 语义是「相对路径按 base_dir 解析，缺省 CWD」（cohort.py），CLI 侧 `cmd_run` 用 `base = Path.cwd()`——于是确定统一模型：**每个项目有 project_dir（绝对路径），它是 Web 端一切 `base_dir`，也是子进程 `cwd`**。这样两侧对相对 `data_dir`/`storage.url`/`adapter.command` 的解析天然一致（e2e 第 14 节实测验证：新项目运行日志确实落在 `.tansuo` 内）。
2. setup agent 怎么 Web 化：对比"同进程跑 AgentLoop"与"仿 RunManager 子进程跑 `cli.py setup`"。子进程方案完整复用现成的进程树管理、日志 tail 架构与端点探测退出码语义（探测失败 exit 1、诊断进日志），且与 run 有清晰的互斥边界；同进程方案要在 uvicorn 事件循环里塞 LLM 循环、日志管道也要重造。选子进程。
3. setup_journal.jsonl 的定位：`cmd_setup` 写 `Path(settings.data_dir)/"setup_journal.jsonl"`（相对路径按进程 cwd 解析），子进程 cwd=project_dir，故 Web 端用 `abs_data_dir(load_settings(settings_path), project_dir)/"setup_journal.jsonl"` 定位，两端一致。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| settings/space 直接放项目根（不用 `.tansuo/` 子目录） | 放弃 | 污染用户仓库、与用户自有文件混淆；用户明确选择了 `.tansuo/` 隔离方案 |
| setup agent 同进程跑（Web 直接调 SetupAgent） | 放弃 | 无进程隔离、LLM 长循环阻塞事件循环、日志 tail/进程树管理全部要重做 |
| 脚手架后自动跑 setup | 放弃 | 用户明确选择手动触发（配置会话要花 LLM token，应由用户决定何时跑） |
| 注册表存 PROJECT_ROOT 下 | 放弃 | 项目是跨安装的用户态数据，且会污染 git 仓库；改为 `~/.tansuo_agent/projects.json` |
| 只修前端选择器、不动 base_dir | 放弃 | 致命缺陷不修，新项目的数据会写进代码安装目录；必须 Phase 1 先修再谈功能 |

### 最终方案

按五个阶段提交，每期独立可验证（提交 df2d214 / 9bd8627 / 91eca3a / 0b19366 / 本次）：

1. **P1 base_dir 动态化 + ProjectStore**（零行为变化）：
   - 新增 `tansuo/web/project_store.py`：注册表 `~/.tansuo_agent/projects.json`（env `TANSUO_PROJECT_STORE` 可改位置，供测试隔离）；`threading.Lock` 保护读-改-写，`tempfile.mkstemp + os.replace` 原子写回；JSON 损坏 → 空骨架自愈不炸；`bootstrap_from_env` 幂等——确保内置 demo 在列、把 env 指定的 settings upsert+激活，保住 STAR #010 语义。
   - `app.py` 所有 `abs_data_dir`/`code_fingerprint`/`resolve_for_run`/孤儿清理/db 定位的 `PROJECT_ROOT` → `_active_paths()` 返回的 project_dir；`RunManager.start()` 增 `settings_path/space_path/project_dir` 参数，`cwd=str(project_dir)`。
2. **P2 项目管理 API + 目录浏览**：`/api/projects`（列表/激活项/新建/激活/删除——删除仅移除注册不删文件）、`/api/fs/browse`（只列目录、排除系统/隐藏目录、拒绝 `..`、Windows 空 path 列盘符）、`/api/fs/files`（选训练脚本用）。新建项目时目录无 `.tansuo/settings.yaml` 则脚手架：模板里 `data_dir: data → .tansuo/data`、`sqlite:///data/tansuo.db → sqlite:///.tansuo/data/tansuo.db`、`adapter.command` 指向训练脚本（相对项目目录）。
3. **P3 setup Web 化 + 硬互斥**：新增 `tansuo/web/setup_manager.py`（与 RunManager 同构的单槽子进程管理器）；端点 `POST /api/projects/{id}/setup`（404 未知 / 400 未登记训练脚本 / 409 忙）、`/api/setup/{status,log,events,stop}`；`_busy_reason()` 统一裁决 run/setup/activate 三方互斥——activate 与 run/start 都接入，任何一方在跑时其余操作 409。
4. **P4 前端**：api.ts 全套封装；`ProjectContext`；header 加 ProjectSelector（层级语义：项目→分区）+「新建/打开」对话框（项目名 + DirPicker 服务端目录浏览 + .py 脚本下拉）。**项目切换纪元（epoch）机制**：切换项目除 `setCohort(null)` 外还要 `epoch+1` 重挂载各页——因为 cohort 本就是 null（跟随最新）时 key 不变，页面不会重挂载、会持续显示旧项目数据。prompts/compare 两页也随 epoch 重挂载（prompts.yaml 在 settings 同目录、对比组按激活项目解析，都是项目级而非全局）。
5. **P5 setup 面板**：AgentPage 拆「调参会话 / 配置 agent」两个子页签；SetupPanel = 触发/停止按钮 + 实时日志 tail + setup_journal 事件列表（kind 与调参会话同构，渲染逻辑抽到 `lib/agentEvents.ts` 共用）。

### 测试策略（无 LLM 凭据环境下确定性验证 setup）

e2e 给 Web 服务进程注入假端点 env：`ANTHROPIC_BASE_URL=http://127.0.0.1:1`（秒拒端口）+ 假 token——`probe_endpoint` 吞所有异常返回 `ok:False`，`cmd_setup` 打印「端点探测失败」到 stderr（进日志）并 exit 1。断言覆盖：子进程拉起（pid/log_path）、退出码、日志含诊断、events 空态、run 中 setup 409、未知项目 404、未登记训练脚本 400。真实 LLM 路径留给手动验收（`dev.bat` 起服务 → 新建项目 → 点「配置」）。

## R · 实际效果（Result）

- **验证方式**：全量回归——11 个单测套件 362 项断言、CLI 冒烟 31 项、Web 冒烟 82 项（其中新增：项目管理 16 项 + setup 9 项）全绿；`npm run build` 通过；新项目全链路 e2e 实测数据落 `<项目>/.tansuo/data`、Web/CLI 两侧解析一致
- **前后对比**：之前只能重启服务换实验；现在浏览器内新建/切换项目、点一下跑配置 agent。既有 `cli.py web --settings X` 用户与直起 uvicorn 用户行为零变化（bootstrap 自动注册 demo/env 项目）
- **副作用与代价**：1) 注册表是进程内单例 + 文件锁只防同进程并发，多 worker uvicorn 不支持（RUN 单例本就不支持，一致）；2) 运行中的搜索绑定启动时路径，切换项目只影响之后的操作；3) 删除项目仅移除注册不删文件（数据安全优先，用户需手动清 `.tansuo/`）
- **遗留问题与后续**：真实 LLM 端点下的 setup 全流程（含探针成功、事件流渲染）待有凭据时手动验收一次；注册表暂无「项目重命名」入口
- **经验教训**：1) 引入"资源可以在任意目录"之前，先把所有隐式的"根目录假设"（base_dir、cwd、静态资源定位）列清单改成显式参数——本次若直接做多项目，数据错位要到用户真跑起来才暴露；2) 同构复用（SetupManager 仿 RunManager、setup 事件复用调参渲染）让新功能几乎不带新风险面；3) 无凭据的 CI/冒烟环境验证 LLM 功能，靠"注入必败假端点 + 断言退出码与诊断文本"可以确定性覆盖主路径
