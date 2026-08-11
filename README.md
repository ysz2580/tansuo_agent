# tansuo_agent —— 智能超参数调节 Agent

深度学习项目常有 7-8 类超参数、每类 2-3 种取值，组合上千种。**tansuo_agent**
用「贝叶斯搜索 + LLM 监督 agent」的混合架构，在约 30 次试验内找到好的组合，
而不是穷举上千轮：

- **搜索引擎**：Optuna TPE 贝叶斯采样 + 中位数早停剪枝（劣质试验提前终止）；
- **监督 agent**：每隔几次试验唤醒一轮 LLM，它阅读结果、学习曲线与参数分布对比，
  **自主收窄/放宽/冻结搜索空间**、提出假设驱动实验、判断收敛——所有动作带护栏、
  写审计日志；
- **配置即文档**：指标怎么评估（几个指标、大好还是小好）、每个超参数的含义，
  都写在配置文件里，同时注入 agent 的上下文，成为它调节搜索的领域知识；
- **agent 不掌握关键路径**：LLM 端点挂了，系统自动降级为纯 Optuna 巡航照跑不误。

```
E:\tansuo_agent\
├── cli.py                  # 命令行入口（run/space/report/check/api/setup/init）
├── tansuo\                 # agent 框架本体
│   ├── agent\              # 通用 agent 内核：client / hooks(权限) / skill / loop
│   │   └── skills\         # 内置技能：tune(调参监督) / config(配置生成)
│   ├── config.py           # settings.yaml 加载与强校验
│   ├── space.py            # 搜索空间：envelope 护栏 + patch 引擎 + 快照
│   ├── orchestrator.py     # 主循环：预算中枢、唤醒策略、断点续跑
│   ├── runner.py/adapter.py# 试验执行 + 子进程/函数适配器（协议见下）
│   └── analysis/report.py  # 汇总分析 + Markdown 报告 + best.yaml
├── tansuo\web\             # Web 后端：FastAPI（只读查询 + 运行驱动 + API 配置切换）
├── web\                    # Web 前端：Vite + React + TS + Tailwind + shadcn/ui
├── demo\                   # 演示场景（MNIST 小 CNN，CPU 可跑）
│   ├── configs\            # settings.yaml + search_space.yaml（带详尽注释）
│   ├── train_mnist.py      # 演示训练脚本（遵守子进程协议）
│   └── my_adapter_template.py  # 你自己的训练任务接入模板
└── tests\                  # 空间护栏 34 / 条件空间 30 / 协议 12 / 权限降级 21 / 运行时 23 断言
```

## 快速上手（跑演示）

```powershell
pip install -r requirements.txt      # optuna / pyyaml / anthropic（torch 自备）

python cli.py api                    # ① 大模型 API 自配置：探测环境→验证模型→写回
python cli.py check                  # ② 端点两级探测（ping + tool-use）
python cli.py run --trials 3 --no-agent   # ③ 不带 agent 冒烟（纯 Optuna）
python cli.py run                    # ④ 正式跑：30 次试验，agent 每 5 次唤醒监督
```

跑完看 `demo/data/reports/report.md`（最优配置、top-k、参数对比、空间演化时间线、
agent 决策摘要）与 `best.yaml`。

> 环境变量：`ANTHROPIC_AUTH_TOKEN`（必需）与 `ANTHROPIC_BASE_URL`（可选，默认
> Anthropic 官方端点；用兼容端点如阿里云百炼时设置）。配置文件支持
> `${ENV:变量名:默认值}` 展开。

## CLI 命令

| 命令 | 作用 |
|---|---|
| `run` | 跑搜索。`--trials N --wake-every K --workers W --hours H --no-agent --resume --fresh --seed --model` |
| `space show` | 查看当前搜索空间（含每个参数的语义说明）与补丁历史 |
| `report` | 重新生成分析报告与 best.yaml |
| `api` | 大模型 API 自配置：盘点凭据→候选模型探测→写回 settings |
| `check` | 两级探测端点（ping + tool-use），验证模型名是否可用 |
| `setup --train 你的脚本` | 配置 agent：读训练脚本自动起草两份配置并跑探测试验自证 |
| `init` | 生成离线配置模板（LLM 不可用时的兜底，`--force` 覆盖） |
| `web` | 启动 Web 后端（可视化界面 API），默认 http://127.0.0.1:8000 |

`run` 细节：默认断点续跑（storage 与空间快照都在 `data_dir`）；`--fresh` 清空重来；
Ctrl+C 会写 finish 事件并提示续跑。

- **并行试验**：`--workers N`（默认取 `budget.workers`）。批内多线程执行
  （Optuna 官方支持的多线程 ask/tell），每个试验独立子进程；agent 唤醒仍发生在
  批边界。python 函数模式下并行要求你的函数**线程安全**。
- **时间预算**：`--hours H`（默认取 `budget.max_duration_h`）。到点后不再派发新试验，
  在途试验跑完即以 `time_budget_exhausted` 优雅收尾——适合"我有一晚上 GPU"的用法。
- **失败重试**：`adapter.retry_on_fail`（0-3，默认 0）。非零退出码**且 stderr 为空**
  判定为瞬时故障自动重试（journal 记 `trial_retry` 事件）；超时/协议错误/stderr
  有内容是确定性失败，不重试。仅子进程模式生效。
- **ETA**：进度行与 Web 仪表盘按最近试验平均耗时 × 剩余预算 ÷ 并发数估算剩余时间
  （断点续跑时从 journal 预热样本）。

## Web 可视化界面（shadcn/ui 前端）

仪表盘（试验统计/最优配置/收敛信号/学习曲线）、试验表（逐 epoch 曲线）、
搜索空间演化（补丁时间线）、agent 决策时间线、运行控制（启动/停止/实时日志）、
大模型 API 切换（探测→写回 settings.yaml）与报告查看，全部在浏览器里完成：

```powershell
# 方式一（生产，单端口）：前端已构建时由后端直接托管
cd web && npm install && npm run build
python cli.py web                # 打开 http://127.0.0.1:8000

# 方式二（开发热重载）：前后端分开跑
python cli.py web                # 后端 :8000
cd web && npm run dev            # 前端 :5173，/api 自动代理到 :8000
```

- **运行控制**：界面上「开始搜索」等价于 `python cli.py run`（子进程驱动，完整复用
  agent 降级与断点续跑），可设本次试验数、唤醒间隔、**并发数**与**时长上限（小时）**；
  「停止」杀整棵进程树，进行中的试验会如实标记为失败。仪表盘预算卡片展示 ETA 估算。
- **API 切换**：设置页改 model / base_url / auth_token，保存前先做两级探测
  （ping + tool-use），失败不落盘。token 留空 = 保持现有 `${ENV:...}` 环境变量引用；
  填明文 = 写入 settings.yaml（界面会警告密钥入库风险）。
- **等价 CLI**：`python cli.py api`（自动探测候选模型并写回）与
  `python cli.py check`（仅探测）在终端完成同样的事。

## 配置即文档

**settings.yaml** 声明"结果怎么评估、训练怎么驱动、预算多少、agent 怎么连"：

```yaml
metrics:
  primary: {name: val_acc, direction: maximize}   # 唯一主指标，驱动搜索与剪枝
  watch:                                          # 观测指标：记录并展示给 agent 权衡
    - {name: val_loss, direction: minimize}
adapter:
  mode: subprocess
  command: ["python", "demo/train_mnist.py"]
  timeout_s: 300
  retry_on_fail: 1        # 瞬时故障（非零退出码且 stderr 为空）自动重试次数
budget:
  total_trials: 30
  wake_every: 5
  data_fraction: 0.5
  workers: 1              # 并行试验数（1=串行，上限 32）
  # max_duration_h: 8     # 可选时间预算（小时）：到点在途试验跑完后优雅收尾
agent:
  model: qwen3-max
  permissions: {default: allow}    # 权限 hook：allow/confirm/deny，可按工具配置
```

**search_space.yaml** 每个参数必须带中文 `description`（含义 + 取值建议）——它既是
给人看的文档，也是注入 agent system prompt 的领域知识（例如它看到高 lr 区间频繁
发散，就有依据地收窄 lr 上界）。支持**条件参数**（真实调参几乎都是条件空间）：

```yaml
- name: optimizer
  type: choice
  choices: [adam, sgd]
  description: 优化器
- name: momentum
  type: float
  low: 0.5
  high: 0.99
  depends_on: {optimizer: sgd}   # 仅 optimizer=sgd 时生效；值可为列表，多键=AND
  description: SGD 动量系数
```

条件不满足的试验不会取样该参数（TPE 正确处理"缺参数"的历史试验）；`space show`
与 Web 空间页会标注依赖关系，agent 上下文中同样可见。

## agent 框架：skill / loop / hooks

- **Skill**：一种能力 = 工具集 + 执行器 + system prompt + 限额 + 完成判定。
  内置 `tune`（调参监督：get_study_summary / get_current_space /
  get_learning_curves / edit_search_space / add_custom_trial / run_trials / finish）
  与 `setup`（配置生成：read_train_script / save_settings / save_search_space /
  run_probe_trial / finish）。
- **AgentLoop**：统一 tool-use 循环（限额内往返、配额收尾、空转提醒），驱动所有技能。
- **权限 hook**：每个工具调用先过钩子链。策略在 `agent.permissions`：
  `default: allow`（默认放行+全程 journal 审计），可对单个工具升级为
  `confirm`（控制台人工确认；无交互终端时自动拒绝）或 `deny`。决策写
  `agent_permission` 审计事件。
- **护栏**（tune 技能的空间编辑）：任何编辑 ⊆ 初始 envelope 包络；必须写 rationale；
  单次 ≤4 条 op；自由参数 ≥3；全会话编辑配额；分类参数聚焦只能用 freeze
  （Optuna storage 禁止动态修改 choices）。违规以结构化错误回喂模型自我修正。
- **降级**：agent 连续失败 N 次（默认 3）自动转纯 Optuna 巡航；`--no-agent` 一等公民。

## 接入你自己的训练任务

见 `demo/my_adapter_template.py`。子进程模式三条契约：

1. 从 env `TANSUO_TRIAL_CONFIG` 读 JSON 配置（或 `config_via: file` 时读
   `TANSUO_CONFIG_FILE` 指向的文件）；
2. 每评估步打印 `##TANSUO## {"type":"epoch","epoch":N,"metrics":{...}}`
   （metrics 键名须与 settings 的 metrics 声明一致，必含主指标）；
3. 结束打印 `##TANSUO## {"type":"final","value":<float>}`，退出码 0。

推荐流程：`python cli.py setup --train 你的脚本` 让配置 agent 起草配置并跑探测试验
验证契约，再人工微调。

## FAQ

- **端点不支持 tool-use？** `check` 会在第 2 级探测暴露；用 `--no-agent` 巡航，
  或换支持 function calling 的端点/模型。
- **试验太慢？** `budget.data_fraction`（训练集抽样，演示默认 0.5）、调小 epochs
  上界、提高 `adapter.timeout_s` 前先评估单次耗时；多核/多卡机器用
  `budget.workers`（或 `--workers`）并行跑试验，ETA 会按并发数折算。
  机器不稳定常偶发退出码 1？设 `adapter.retry_on_fail: 1` 自动重试瞬时故障。
- **为什么不能改分类参数的候选集？** Optuna storage 拒绝动态
  CategoricalDistribution（实测）。聚焦分类参数用 `freeze` 固定到某个取值。
- **怎么看 agent 干了什么？** `demo/data/journal.jsonl`（JSONL 事件流：试验/补丁/
  工具调用/权限决策，`python cli.py space show` 看补丁历史）。
