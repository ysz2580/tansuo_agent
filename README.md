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
├── cli.py                  # 命令行入口（run/runs/space/report/check/api/setup/init）
├── tansuo\                 # agent 框架本体
│   ├── agent\              # 通用 agent 内核：client / hooks(权限) / skill / loop
│   │   └── skills\         # 内置技能：tune(调参监督) / config(配置生成)
│   ├── config.py           # settings.yaml 加载与强校验
│   ├── cohort.py           # 记录分区：记录永不删除 + 三指纹自动分区 + 迁移
│   ├── compare.py          # 跨分区对比：同目标分区选组 + 最优/参数/曲线并排
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
└── tests\                  # 分区 110 / 对比 22 / 热启动 16 / 空间护栏 34 / 条件空间 30 / 协议 12 / 权限降级 21 / 运行时 23 断言
                            # + e2e_cli_smoke / e2e_web_smoke 端到端冒烟（真实子进程）
```

## 快速上手（跑演示）

```powershell
pip install -r requirements.txt      # optuna / pyyaml / anthropic（torch 自备）

python cli.py api                    # ① 大模型 API 自配置：探测环境→验证模型→写回
python cli.py check                  # ② 端点两级探测（ping + tool-use）
python cli.py run --trials 3 --no-agent   # ③ 不带 agent 冒烟（纯 Optuna）
python cli.py run                    # ④ 正式跑：30 次试验，agent 每 5 次唤醒监督
```

跑完看报告与 `best.yaml`（在当次搜索的分区目录内，如
`demo/data/runs/0001-…/reports/report.md`；最优配置、top-k、参数对比、空间演化
时间线、agent 决策摘要一应俱全）。

> 环境变量：`ANTHROPIC_AUTH_TOKEN`（必需）与 `ANTHROPIC_BASE_URL`（可选，默认
> Anthropic 官方端点；用兼容端点如阿里云百炼时设置）。配置文件支持
> `${ENV:变量名:默认值}` 展开。

## CLI 命令

| 命令 | 作用 |
|---|---|
| `run` | 跑搜索。`--trials N --wake-every K --workers W --hours H --warm-start K --no-agent --resume --new --note --cohort ID --seed --model` |
| `runs` | 列出所有记录分区（时间/备注/三指纹/试验数/最优值/与当前指纹可比性）；`runs show ID` 看详情；`runs compare [ID...]` 跨分区对比 |
| `space show` | 查看搜索空间（含每个参数的语义说明）与补丁历史（默认最新分区，`--cohort ID` 看历史分区） |
| `report` | 重新生成分析报告与 best.yaml（默认最新分区，`--cohort ID` 为历史分区生成） |
| `api` | 大模型 API 自配置：盘点凭据→候选模型探测→写回 settings |
| `check` | 两级探测端点（ping + tool-use），验证模型名是否可用 |
| `setup --train 你的脚本` | 配置 agent：读训练脚本自动起草两份配置并跑探测试验自证 |
| `init` | 生成离线配置模板（LLM 不可用时的兜底，`--force` 覆盖） |
| `web` | 启动 Web 后端（可视化界面 API），默认 http://127.0.0.1:8000 |

`run` 细节：默认断点续跑（storage 与空间快照都在分区目录内，见下节）；`--new` 强制新开
分区；`--note 备注` 给分区写备注；`--cohort ID` 续跑指定分区（优化目标语义不符会被拒绝）；
`--fresh` 仍可用但已去破坏化——等价 `--new`，**不再删除任何记录**；Ctrl+C 会写 finish
事件并提示续跑。

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

## 记录分区管理（记录永不删除）

调参记录是宝贵资产：**本系统不删除任何跑过的记录**。每次搜索的全部痕迹
（Optuna 数据库、journal 事件流、空间快照、报告、最优配置）都收在独立**分区**里：

```
data_dir\runs\
├── 0000-legacy\            # 旧版扁平布局的历史记录（自动迁入，只搬不删）
├── 0001-20260810-143022\   # 一个分区 = meta.yaml + t.db + journal.jsonl
│   ├── meta.yaml           #   + space_v*.yaml + reports\（report.md/best.yaml）
│   ├── ...
└── 0002-20260811-091530\
```

**三指纹**决定续跑还是新开分区（每个分区的 meta.yaml 里都留了审计明细）：

- **objective_hash** = 主指标 `name:direction` + `data_fraction`。它变了意味着新旧
  结果根本不可比——而且 Optuna 加载既有 study 时会**静默丢弃**请求的 direction
  （沿用库内方向），混跑会让排序/剪枝/报告全部失真。所以优化目标变化必然新开分区，
  `--cohort` 显式续跑目标不符的分区会被**直接拒绝**。
- **code_hash** = 训练代码内容（入口脚本 / `python -m` 模块 / entry 模块自动定位）。
  改了模型代码再跑 → 自动新开分区，旧分区原封不动；把代码改回旧版 → 自动恢复对应
  的旧分区续跑。训练脚本不在命令行里时用 `experiment.fingerprint_paths`
  （文件或目录，目录收 `**/*.py`）显式声明要哈希的代码。
- **data_hash** = 数据集身份。同一份代码跑不同数据集，结果范围天然不同，同样不能
  混在一起：数据集变化自动新开分区，改回数据集则恢复旧分区。两种声明方式——
  ① 显式 `experiment.dataset: mnist-5k`（字符串或列表，名称/路径皆可，不读文件系统）；
  ② 不声明时自动兜底：subprocess 模式把命令行中除解释器/脚本外的参数纳入指纹
  （覆盖 `--data X` 这类切换）。python 函数模式没有命令行，需要显式声明，未声明时
  `runs` 会标注"未跟踪"。`--cohort` 显式续跑数据集不符的分区只给警告不拒绝
  （跨数据集热启动是合理的高级用法），与目标变化的硬拒绝不同。

旧版本创建的分区没有 data_hash，升级后首次运行会自动新开一个分区（原因里会说明，
旧记录完整保留）。

每个分区的 meta.yaml 还记录**环境审计**信息：python / optuna / torch 版本、CUDA
是否可用与 GPU 型号、主机名与操作系统、CPU 核数。分区可能跨天、跨依赖升级、跨
机器续跑，这些信息让事后能对上"这批试验是在什么环境下跑出来的"；每次续跑还会
刷新最近一次运行的环境（`environment_last`）。`runs show ID` 直接展示；引入该功能
之前创建的老分区显示"无记录"，不影响任何功能。

`python cli.py runs` 列出所有分区，并标注每个分区与**当前**指纹的可比性
（✔ 一致 / △ 代码或数据集已变 / ✘ 目标已变），`runs show ID` 查看指纹覆盖了哪些文件。
Web 界面顶部的分区选择器同样可切换回看任意历史分区；仪表盘在当前代码/数据集已变化时
给出横幅提示。`space_v*.yaml` 空间演化、报告等只属于各自分区，互不污染。

**跨分区对比**（`python cli.py runs compare [ID...]`，Web「对比」页同构）：把多个
分区的最优结果并排放在一起——每分区的最优值/最优试验/试验数、最优配置参数对照表、
最优试验学习曲线叠加。前提是优化目标一致：缺省取与当前 settings 目标指纹
（objective_hash）相同的全部分区；显式指定目标指纹不同的分区组合会被拒绝
（与 `--cohort` 的硬拒绝同一逻辑——方向不同的结果排序会反转，没有可比性）。
代码或数据集不同不影响对比资格，页面上各分区的 code_hash / data_hash 前 8 位
随时可见，差异自己一眼就能对上。

**新分区热启动**：训练代码或数据集变化自动新开分区时，旧结果虽然不可比（必须
重跑），但"哪些配置曾经有效"的经验不必从零再来——系统把**同优化目标**旧分区里
最优的若干配置（`budget.warm_start`，默认 3，`--warm-start K` 覆盖，0 关闭）
入队为新分区的种子试验，优先执行。种子会按当前搜索空间清洗（冻结参数取当前冻结
值、条件参数父值不满足则丢弃、出界取值交给 TPE 重采），走正常 ask/tell 流程真实
重跑并计入预算——复用的只是配置，旧目标值不会带进新分区；journal 记
`warm_start` 审计事件。目标变化新开的分区没有同源种子，不会错误继承。

## Web 可视化界面（shadcn/ui 前端）

仪表盘（试验统计/最优配置/收敛信号/学习曲线）、试验表（逐 epoch 曲线）、
搜索空间演化（补丁时间线）、agent 决策时间线、**跨分区对比**（同目标分区的最优值/
参数对照/学习曲线叠加，目标分组可切换）、运行控制（启动/停止/实时日志）、
大模型 API 切换（探测→写回 settings.yaml）与报告查看，全部在浏览器里完成。
顶部**分区选择器**可回看任意历史记录分区（记录永不删除；「最新分区」自动跟随
新开的分区），仪表盘在训练代码、数据集或优化目标已变化时横幅提示"下次运行将
自动新开分区"：

```powershell
# 方式一（生产，单端口）：web/dist 构建产物已随仓库提交，克隆后直接起服务即可
python cli.py web                # 打开 http://127.0.0.1:8000

# 修改过前端后需重建并提交 dist：
cd web && npm install && npm run build

# 方式二（开发热重载）：前后端分开跑
python cli.py web                # 后端 :8000
cd web && npm run dev            # 前端 :5173，/api 自动代理到 :8000
```

- **运行控制**：界面上「开始搜索」等价于 `python cli.py run`（子进程驱动，完整复用
  agent 降级与断点续跑），可设本次试验数、唤醒间隔、**并发数**与**时长上限（小时）**；
  「新开分区」强制新建记录分区并可写备注（历史记录不受影响）；
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
- **改了模型/代码，之前的调参结果会混进来吗？** 不会。代码指纹变化会自动新开
  分区，旧记录原封不动；`python cli.py runs` 里旧分区会被标为「△ 代码已变」，
  提示它与当前代码不可直接比较。改了主指标或方向则会新开分区且拒绝混跑
  （Optuna 加载既有 study 会静默沿用库内方向，混跑必失真）。不过历史经验会以
  **热启动种子**的形式延续：同目标旧分区的最优配置优先在新分区重跑一遍（只复用
  配置、重新测值，见上节）。
- **换了数据集呢？** 同样不会混。数据集指纹变化自动新开分区，旧分区标
  「△ 数据集已变」；改回数据集会恢复对应旧分区续跑。数据集经命令行参数驱动
  （`--data X`）时参数自动参与指纹；显式声明用 `experiment.dataset`（python 函数
  模式无命令行可用，必须显式声明，否则视为"未跟踪"）。`--cohort` 显式续跑数据集
  不符的分区只给警告、不拒绝（跨数据集热启动是合理的高级用法）。
- **想从头开始怎么办？** `python cli.py run --new`（或 Web「新开分区」）新开一个
  分区从零开始——历史记录保留，随时可回看对比（`runs compare` 或 Web「对比」页）。
  没有"删除重来"，也不需要。
- **怎么看 agent 干了什么？** 分区目录内的 `journal.jsonl`（JSONL 事件流：试验/补丁/
  工具调用/权限决策，`python cli.py space show` 看补丁历史，
  `python cli.py runs` 找分区）。
