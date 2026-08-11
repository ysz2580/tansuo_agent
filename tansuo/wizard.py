"""离线配置模板生成（cli.py init）：不依赖 LLM 的兜底路径。

生成带详尽注释的 settings.yaml / search_space.yaml 模板，每字段有说明与示例，
用户照填即可。文件已存在时不覆盖（除非 force）。
"""
from __future__ import annotations

from pathlib import Path

SETTINGS_TEMPLATE = """\
# ============================================================
# tansuo_agent 总配置模板（cli.py init 生成）
# 建议改用 `python cli.py setup --train 你的训练脚本` 让配置 agent
# 自动推断生成；本模板是 LLM 不可用时的兜底。
# 支持 ${ENV:变量名} 与 ${ENV:变量名:默认值} 环境变量展开。
# ============================================================

experiment:
  name: my_experiment        # 实验名（报告标题/日志用）
  data_dir: data             # 运行时产物目录（db / journal / 报告 / 空间快照）

# ---------- 结果指标定义：有几个指标、各自大好还是小好 ----------
metrics:
  # 唯一主优化目标（必须且只能一个），驱动 Optuna 搜索与剪枝
  primary:
    name: val_acc            # ← 训练脚本协议行 metrics 里的键名
    direction: maximize      # maximize=越大越好 | minimize=越小越好
  # 观测指标：不影响搜索方向，但会记录并展示给 agent 做多维权衡
  watch:
    - {name: val_loss, direction: minimize}
    - {name: train_loss, direction: minimize}
    - {name: epoch_time_s, direction: minimize}

# ---------- 训练驱动方式 ----------
adapter:
  mode: subprocess           # subprocess=子进程跑脚本(推荐) | python=同进程调函数
  # mode=subprocess：启动命令；脚本从 env TANSUO_TRIAL_CONFIG 读 JSON 配置
  command: ["python", "path/to/your_train.py"]
  # mode=python：入口 "module.path:函数名"，函数签名 (config, report)->float
  # entry: "mypkg.train:run_trial"
  config_via: env            # env=环境变量传 JSON | file=临时文件路径(TANSUO_CONFIG_FILE)
  timeout_s: 300             # 单次试验超时红线（秒）
  # retry_on_fail: 1         # 瞬时故障自动重试次数（0-3）：仅"非零退出码且 stderr 为空"
                             # 判为瞬时故障；超时/协议错误/stderr 有内容是确定性失败，不重试

# ---------- 预算 ----------
budget:
  total_trials: 30           # 一次会话最多跑多少次试验
  wake_every: 5              # 每完成多少次试验唤醒 agent 一轮
  seed: 42                   # TPE 采样种子（可复现）
  data_fraction: 1.0         # 训练集抽样比例（加速开关；纯 CPU 调试可设 0.5）
  workers: 1                 # 并行试验数（1=串行，上限 32）；CLI --workers 可临时覆盖
  # max_duration_h: 8        # 可选时间预算（小时）：到点不再派发新试验，在途试验跑完
                             # 即以 time_budget_exhausted 收尾；CLI --hours 可临时覆盖

# ---------- 早停剪枝器 ----------
pruner:
  type: median               # 中位数剪枝：劣于已完成试验中位数的提前终止
  n_startup_trials: 4        # 前 N 次试验不剪枝，先积累比较样本
  n_warmup_steps: 1          # 每个试验至少跑满 N 步才参与剪枝比较

# ---------- LLM agent（监督者） ----------
agent:
  enabled: true              # false 等价于 --no-agent
  model: qwen3-max           # 端点支持的模型名（python cli.py check 可验证）
  base_url: ${ENV:ANTHROPIC_BASE_URL:}     # 空则用 SDK 默认/环境变量
  auth_token: ${ENV:ANTHROPIC_AUTH_TOKEN:}
  max_wake_rounds: 6         # 全会话最多唤醒轮数
  max_turns_per_wake: 10     # 每轮最多 messages 往返
  max_tool_calls_per_wake: 8
  max_space_edits_total: 6   # 全会话最多空间编辑次数
  max_consecutive_failures: 3  # LLM 连续失败 N 次→自动降级 --no-agent

# ---------- 结果存储 ----------
storage:
  url: sqlite:///data/tansuo.db   # 也支持 journal://data/optuna_journal.jsonl
"""

SPACE_TEMPLATE = """\
# ============================================================
# 初始搜索空间模板（cli.py init 生成）
# 每个参数必须带 description（配置即文档；也是 agent 调节空间的
# 领域知识依据）。参数名在会话中不可更改（TPE 按名建模）。
# type 取值：choice（离散选项）/ float / int；float 可用 log 尺度。
# 条件参数：depends_on 声明父参数依赖（父参数必须是定义在前面的 choice），
# 如 momentum 仅 optimizer=sgd 时生效；条件不满足的试验不取样该参数。
# ============================================================

params:
  - name: optimizer          # ← 示例：分类超参数
    type: choice
    choices: [adam, adamw, sgd]
    description: >-
      优化算法。Adam/AdamW 对小学习率稳定；SGD 通常需要更高 lr
      （配合动量 0.9）。按你的训练脚本支持的优化器修改 choices。

  - name: momentum           # ← 示例：条件参数（仅 optimizer=sgd 时生效）
    type: float
    low: 0.5
    high: 0.99
    depends_on: {optimizer: sgd}   # 值可为列表 {optimizer: [sgd, adam]}；多键=AND
    description: >-
      SGD 动量系数。条件参数示例：optimizer 取 adam/adamw 时本参数
      不参与搜索。不用条件参数可删除本段。

  - name: lr                 # ← 示例：log 尺度连续超参数
    type: float
    low: 1.0e-4
    high: 3.0e-1
    log: true
    description: >-
      学习率：参数更新步长。过大→loss 发散；过小→收敛慢。
      Adam 系常用 1e-4~1e-3，SGD 可高 10~100 倍。

  - name: batch_size         # ← 示例：数值型离散（也可用 choice）
    type: int
    low: 16
    high: 256
    description: >-
      批大小。小 batch 正则效果略强但更慢；大 batch 更快更平滑，
      通常需相应提高 lr。

  # ……按你的训练脚本继续添加（argparse/config 里暴露的超参数）。
  # 经验：7-8 个参数、组合上千种，正是本工具最适合的规模。
"""


def init_templates(settings_path: str | Path, space_path: str | Path,
                   force: bool = False) -> list[Path]:
    """生成模板文件，返回实际写入的路径列表。已存在且未 force 则跳过（提示）。"""
    written: list[Path] = []
    for path, template, name in [(Path(settings_path), SETTINGS_TEMPLATE, "settings.yaml"),
                                 (Path(space_path), SPACE_TEMPLATE, "search_space.yaml")]:
        if path.exists() and not force:
            print(f"跳过 {path}（已存在；如需覆盖加 --force）")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")
        written.append(path)
        print(f"已生成 {name} 模板：{path}")
    if written:
        print("请按注释修改两份配置，然后 `python cli.py check` 验证端点、"
              "`python cli.py run --trials 3 --no-agent` 冒烟。")
    return written
