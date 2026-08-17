---
date: 2026-08-17
number: "027"
title: demo2 从零接入首跑全败（退出码 2）：脚手架模板占位命令直通运行层无人拦截，且训练脚本忘选后无补登记入口
severity: medium
status: resolved
tags: [项目管理, 脚手架, 快速失败, 训练脚本, web后端, 前端, 用户体验]
module: tansuo/web/app.py · tansuo/web/project_store.py · web/src（SetupPanel/TrainScriptPicker）
---

# demo2 从零接入首跑全败（退出码 2）：脚手架模板占位命令直通运行层无人拦截，且训练脚本忘选后无补登记入口

## S · 背景（Situation）

- **项目 / 模块**：tansuo Web 项目管理（`tansuo/web/app.py`、`project_store.py`、前端 SetupPanel）。
- **环境**：Windows 11、Python 3.14、FastAPI/uvicorn 后端 :8000 + Vite 前端 :5173；optuna 4.9.0。
- **当时在做什么**：用户按「从零接入」流程在 Web UI 新建项目 `test` 指向 `E:\tansuo_agent\demo2`（numpy 双月示例仓库，STAR 027 前序提交 0c0cf72），**新建对话框里未选择训练脚本**，随后未运行「配置 agent」直接点了「启动搜索」。
- **问题表现**：2 次试验全部失败，运行日志：

  ```
  [1/2] trial#0 FAILED  训练脚本退出码 2 (0.5s)
  [2/2] trial#1 FAILED  训练脚本退出码 2 (0.6s)

  结束（budget_exhausted）：本次会话没有完成的试验（全部剪枝/失败） | 算力 0.000 机时
  ```

  P0 落盘的试验全量日志（`demo2/.tansuo/data/runs/0001-.../trials/trial-0000.log`）揭示真因：

  ```
  ===== 2026-08-17 16:54:59 cmd=['python', 'path/to/your_train.py'] params={"optimizer": "adamw", ...} =====
  ----- stderr -----
  python: can't open file 'E:\\tansuo_agent\\demo2\\path\\to\\your_train.py': [Errno 2] No such file or directory

  ----- exit_code=2 timed_out=False -----
  ```

- **影响范围**：两层缺口叠加——
  1. **占位命令直通运行层**：`POST /api/projects` 的脚手架（`_scaffold_project`）把模板原样写出，`adapter.command` 是占位符 `["python", "path/to/your_train.py"]`；它 YAML 合法、`load_settings` 校验通过，于是一路放行到试验子进程才以退出码 2 爆炸。用户看到的只是「全部失败」，没有任何提示指向「项目未配置」。
  2. **训练脚本无补登记入口**：项目注册表里 `"train_script": ""`（新建时忘选），此后没有任何 API/UI 能补上——`POST /api/projects/{id}/setup` 直接 400「未登记训练脚本」，唯一出路是删除项目重建。用户原话：「我刚才是没选，但这个不应该可以后续在前端补吗，还能让agent读项目，判断哪几个是，显示list，可以人为选择」。
- **复现步骤**：1) UI 新建项目指向任意无 `.tansuo/` 的目录，训练脚本留空；2) 激活后直接「启动搜索」；3) 100% 全试验退出码 2。

## T · 目标（Task）

- **要达成什么**：① 未配置的项目点「启动搜索」时**启动前**快速失败，给出可操作的中文指引（去配置），而不是烧完整轮试验；② 训练脚本可事后补登记/更换：后端扫描项目目录给出「像训练脚本」的候选列表供人选择（用户明确要求「agent读项目，判断哪几个是，显示list，人为选择」），占位命令随之自动回填；③ 前端 SetupPanel 落地补选交互。
- **验收标准**：占位命令在 `/api/run/start` 返回 400 + 指引文案；候选扫描对 demo2 把 `train.py` 排第一；补登记后 settings 占位符被回填、注册表更新；18 单测套件 + CLI/Web 两个端到端冒烟零回归；前端构建通过。
- **约束条件**：不动既有 demo 项目与已配置项目的行为（合法命令不得误拦）；候选评分用本地启发式（扫描列候选要求即时、确定性，LLM 留给 setup agent 干重活）。

## A · 解决方案（Action）

### 排查过程

1. 用户贴的运行日志显示两试验各 0.5s 即败、退出码 2——argparse/python 找不到文件的典型码，怀疑启动命令而非训练逻辑。
2. 列 `demo2/.tansuo/` 文件：没有 `setup_journal.jsonl` → setup agent **从未运行**；settings/search_space 的字节数与模板一致。
3. 读 P0 落盘的 `trials/trial-0000.log`（cmd + stderr 全记录）：`cmd=['python', 'path/to/your_train.py']`，stderr 是 `can't open file`——一秒定位：scaffold 模板占位符原样进了运行层。
4. 读 `~/.tansuo_agent/projects.json` 确认 `train_script: ""`——新建时未选，且代码里没有事后补登记的端点；`project_setup` 对空脚本直接 400。两个缺口都坐实。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 让用户删除项目重建并记得选脚本 | 放弃 | 把系统缺口推给用户记忆；「可选项忘选」必然反复发生，需要补救路径而不是重来 |
| 在 settings 校验（`load_settings`）里拒绝占位命令 | 放弃 | 校验层只保证结构合法；「文件是否存在」依赖 project_dir 上下文，属运行决策，放启动入口更合适，也不影响 CLI 直用模板的场景 |
| 候选扫描用 LLM 判断哪个是训练脚本 | 放弃 | 列候选要求即时且零成本；本地启发式（文件名 + argparse + 主入口 + 训练循环关键词）已足够区分，LLM 阅读脚本的活留给 setup agent |
| run_start 启动前校验脚本存在性 + 补登记端点 + 前端选择器 | 采用 | 快速失败前移、补救路径完整、候选扫描降低选择成本，三件套闭环 |

### 最终方案

1. **启动前拦截**（`tansuo/web/app.py`）——新增 `_adapter_command_problem(settings, project_dir)`：mode=subprocess 时取 command 里第一个 `.py` 元素（无则不盲目拦，兼容 `python -m pkg`），相对路径按 project_dir 解析，文件不存在即返回错误文案；`run_start` 在 `load_settings` 成功后立即检查，400 拒绝：

   ```
   adapter.command 指向的训练脚本不存在：path/to/your_train.py（解析为
   E:\tansuo_agent\demo2\path\to\your_train.py）。项目可能尚未配置——请先在
   「Agent」页登记训练脚本并运行「配置 agent」，或手工修正
   .tansuo/settings.yaml 的 adapter.command。
   ```

2. **候选扫描 + 补登记端点**（`tansuo/web/app.py`）：
   - `_score_train_file` 启发式评分：文件名含 train/main/fit/run（+3/+2/+2/+1）；内容读 `TANSUO_TRIAL_CONFIG`（+4，已实现协议）、`argparse/add_argument`（+2）、`__main__`（+2）、epoch 循环（+1）、loss/backward/optimizer/accuracy（+1），每项附中文依据；跳过 `.tansuo`/venv/node_modules/隐藏目录，深度 ≤2，score>0 才入列，降序取前 30。
   - `GET /api/projects/{id}/train-candidates`：返回 `{candidates:[{path,rel,name,score,reasons}]}`。
   - `POST /api/projects/{id}/train-script`：校验脚本存在、是 .py、在项目目录内（三条 400）；忙时 409；settings.yaml 仍含字面占位符 `path/to/your_train.py` 时**同步回填**为相对路径并 `load_settings` 校验（坏了立即回滚原文），已被 setup/人工改过的配置不动；`PROJECTS.update()` 写注册表。
3. **ProjectStore.update**（`tansuo/web/project_store.py`）：白名单字段（name/dir/settings_path/space_path/train_script）读-改-写，锁保护 + 原子替换；train_script 统一 `Path.resolve()` 存绝对路径；未知 id 抛 KeyError。
4. **前端**：
   - `web/src/lib/api.ts`：`TrainCandidate` 类型 + `trainCandidates()` / `setTrainScript()`。
   - 新组件 `web/src/components/TrainScriptPicker.tsx`：进入即扫描，候选行（radio 样式 + 依据徽章）点选登记；扫描为空或用户不认可时可展开 DirPicker + 目录 .py 列表手工挑。
   - `SetupPanel.tsx`：未登记时内嵌选择器；已登记显示路径 + 「更换」按钮；「开始配置」的禁用提示同步改文案；切项目重置展开态。
   - `npm run build` 重建 dist 并提交。
5. **测试**：
   - `tests/test_project_store.py` 加 4 断言（update 写入/白名单外忽略/KeyError/回读一致）；
   - `tests/e2e_web_smoke.py` 新增段 15b（11 断言）：projD 不选脚本创建 → 占位命令启动被 400 拦截且文案含指引 → 候选扫描 train_model.py 第一、reasons 非空、无关 utils.py 不入列 → 补登记 `settings_patched=true`、占位符回填、注册表更新 → 不存在/目录外脚本均 400。

## R · 实际效果（Result）

- **验证方式与数字**：
  - 临时自检脚本对 demo2 实测：`train.py` 以 score=9 排第一（5 条依据全中），占位命令检查返回完整指引文案，demo 项目合法命令/`mode=python`/`python -m` 形态均不误拦；
  - `tests/test_project_store.py` → 20 项断言通过；
  - 全量回归 18 个单测套件 → **失败套件数：0 / 18**（共 564 项断言）；
  - `tests/e2e_cli_smoke.py` → 45 项通过；`tests/e2e_web_smoke.py` → **131 项通过**（120 → 131，新增 15b 段 11 项）；
  - 前端 `tsc -b && vite build` 通过，dist 已重建。
- **前后对比**：未配置项目点「启动搜索」从「烧完整轮试验、2 个退出码 2、报告里只有『全部失败』」→「启动前 400 + 明确指引去 Agent 页配置」；训练脚本忘选从「只能删项目重建」→「SetupPanel 里扫描候选一键登记，占位命令自动回填」。
- **副作用与代价**：run_start 多一次文件存在性检查（微秒级）；候选扫描同步读项目内 .py 文件头 200KB（深度 ≤2、最多 30 条，实测 demo2 <10ms）。
- **遗留问题与后续**：无。
- **经验教训**：
  1. **脚手架模板的占位值绝不能直通执行路径**：模板生成的是「格式合法、内容无效」的配置，能通过一切结构校验，只有人真跑了才炸。凡是模板占位（`path/to/...`、`your_xxx`），要么在使用入口做存在性快速失败，要么模板里显式留校验钩子。
  2. **UI 上每个「可稍后再填」的可选输入，都必须有事后的编辑入口**：允许新建时留空却不给补救路径，等于把一次性操作变成不可逆错误。
  3. **P0 的试验全量日志落盘本次立了功**：`trials/trial-0000.log` 记下 cmd + stderr，让根因一分钟定位、无需复现——「给失败留现场」的功能价值在这种排查里兑现。
