"""用户接入模板：把你自己的训练任务接到 tansuo_agent。

两种模式任选其一：

═══════════════════════════════════════════════════════════════
模式 A：子进程模式（推荐，任何语言都能接）
═══════════════════════════════════════════════════════════════
你的训练脚本只需遵守三条契约：

1. 读配置：从环境变量 TANSUO_TRIAL_CONFIG 读 JSON（或 settings 里
   config_via: file 时，从 TANSUO_CONFIG_FILE 指向的文件读）。
2. 每完成一个评估步（epoch/每 N step），打印一行协议行（必须含
   settings.yaml 里声明的主指标）：
       ##TANSUO## {"type":"epoch","epoch":1,"metrics":{"val_acc":0.81,...}}
3. 结束打印 final 行并以退出码 0 结束：
       ##TANSUO## {"type":"final","value":0.93}

非零退出 / 超时 / 缺 final 行 / 缺主指标 → 该试验记 FAILED，搜索继续。
其它 print 输出随意（会被当作噪音忽略）。

把下面的伪代码换成你的训练循环即可：
"""
import json
import os


def main_subprocess_mode():
    cfg = json.loads(os.environ["TANSUO_TRIAL_CONFIG"])
    # cfg 形如：{"optimizer": "adam", "lr": 0.001, ..., "seed": 3}
    # 建议用 cfg["seed"] 固定随机种子，保证可复现。

    best = 0.0
    for epoch in range(1, cfg["epochs"] + 1):
        train_one_epoch(cfg)            # ← 你的训练
        metrics = evaluate(cfg)         # ← 你的评估，返回 dict
        # 键名必须与 settings.yaml 的 metrics 声明一致（至少含主指标）
        print("##TANSUO## " + json.dumps(
            {"type": "epoch", "epoch": epoch, "metrics": metrics},
            ensure_ascii=False), flush=True)
        best = metrics.get("val_acc", best)

    print("##TANSUO## " + json.dumps({"type": "final", "value": best}))


def train_one_epoch(cfg):
    ...


def evaluate(cfg):
    return {"val_acc": 0.0, "val_loss": 0.0}   # ← 换成真实评估


"""
settings.yaml 里这样指向你的脚本：

    adapter:
      mode: subprocess
      command: ["python", "path/to/your_train.py"]
      config_via: env
      timeout_s: 600          # 单次试验超时红线，按你的任务调整

═══════════════════════════════════════════════════════════════
模式 B：Python 函数模式（同进程，省启动开销）
═══════════════════════════════════════════════════════════════
写一个签名如下的函数，settings 里用 "模块路径:函数名" 引用：

    adapter:
      mode: python
      entry: "my_pkg.train:run_trial"
"""


def run_trial(config: dict, report) -> float:
    """config：本次试验的超参数字典；返回最终主指标值。

    report(epoch, metrics) 每评估步调用一次：
    - 返回 True 继续；返回 False 表示该试验被剪枝，请立即 return。
    """
    best = 0.0
    for epoch in range(1, config["epochs"] + 1):
        train_one_epoch(config)
        metrics = evaluate(config)
        if not report(epoch, metrics):   # 被剪枝：立即停止
            return best
        best = max(best, metrics.get("val_acc", 0.0))
    return best


"""
═══════════════════════════════════════════════════════════════
接入步骤（推荐顺序）
═══════════════════════════════════════════════════════════════
1. python cli.py setup --train path/to/your_train.py
   让配置 agent 读你的脚本，自动起草 settings.yaml 与 search_space.yaml，
   并跑一次探测试验验证契约（失败会告诉你缺什么）。
   （LLM 不可用时用 python cli.py init 生成离线模板手工填。）
2. python cli.py check          # 验证大模型端点
3. python cli.py run --trials 3 --no-agent   # 不带 agent 冒烟
4. python cli.py run            # 正式跑（agent 每 5 次试验唤醒监督）
5. 看 demo/data/reports/report.md（或你 settings.data_dir 下的 reports/）

搜索空间建议：7-8 个参数、每参数 2-4 个量级的取值（组合上千种），
每个参数写清楚中文 description——它既是文档，也是 agent 调节空间的依据。
"""

if __name__ == "__main__":
    main_subprocess_mode()
