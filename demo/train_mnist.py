"""演示训练脚本：MNIST 小 CNN（自包含，可独立运行）。

作为 tansuo_agent 的"被调参对象"，它实现了标准适配器契约（子进程模式）：
1. 从环境变量 TANSUO_TRIAL_CONFIG（JSON 字符串）或 TANSUO_CONFIG_FILE（JSON 文件路径）
   读取本次试验的超参数配置；
2. 每完成一个 epoch 向 stdout 打印一行协议行：
   ##TANSUO## {"type":"epoch","epoch":N,"metrics":{...}}
3. 训练结束打印 ##TANSUO## {"type":"final","value":<float>,"metrics":{...}}，退出码 0。
metrics 里的键名与 configs/settings.yaml 的 metrics 声明一致：
   primary=val_acc，watch=val_loss/train_loss/epoch_time_s。

独立调试示例：
  set TANSUO_TRIAL_CONFIG={"optimizer":"adam","lr":0.001,"scheduler":"none","batch_size":64,"weight_decay":0.0001,"dropout":0.2,"augment":"none","width":16,"epochs":2,"seed":1}
  python examples/train_mnist.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms

PROTOCOL = "##TANSUO## "
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mnist_data")


def emit(payload: dict) -> None:
    print(PROTOCOL + json.dumps(payload, ensure_ascii=False), flush=True)


def load_config() -> dict:
    raw = os.environ.get("TANSUO_TRIAL_CONFIG")
    if raw:
        cfg = json.loads(raw)
    else:
        path = os.environ.get("TANSUO_CONFIG_FILE")
        if not path:
            raise RuntimeError(
                "缺少配置：请设置环境变量 TANSUO_TRIAL_CONFIG(JSON) 或 TANSUO_CONFIG_FILE(文件路径)")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    required = ["optimizer", "lr", "scheduler", "batch_size",
                "weight_decay", "dropout", "augment", "width", "epochs"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise RuntimeError(f"配置缺少必需超参数：{missing}（收到键：{sorted(cfg)}）")
    return cfg


class SmallCNN(nn.Module):
    """Conv(w)->Pool->Conv(2w)->Pool->Dropout->Linear(10)。width=32 时约 3.2 万参数。"""

    def __init__(self, width: int, dropout: float):
        super().__init__()
        self.conv1 = nn.Conv2d(1, width, kernel_size=3, padding=1)
        self.conv2 = nn.Conv2d(width, width * 2, kernel_size=3, padding=1)
        self.pool = nn.MaxPool2d(2)
        self.dropout = nn.Dropout2d(dropout)
        self.fc = nn.Linear(width * 2 * 7 * 7, 10)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = self.dropout(x)
        x = self.fc(torch.flatten(x, 1))
        return x


def make_datasets(cfg: dict):
    base_tf = [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    if cfg["augment"] == "affine":
        train_tf = transforms.Compose(
            [transforms.RandomAffine(degrees=10, translate=(0.1, 0.1))] + base_tf)
    else:
        train_tf = transforms.Compose(base_tf)
    test_tf = transforms.Compose(base_tf)
    train_ds = datasets.MNIST(DATA_DIR, train=True, download=True, transform=train_tf)
    test_ds = datasets.MNIST(DATA_DIR, train=False, download=True, transform=test_tf)

    frac = float(os.environ.get("TANSUO_DATA_FRACTION", "1.0"))
    if 0.0 < frac < 1.0:                       # --fast 加速开关：抽样训练集
        seed = int(cfg.get("seed", 0))
        g = torch.Generator().manual_seed(seed)
        n = max(1000, int(len(train_ds) * frac))
        idx = torch.randperm(len(train_ds), generator=g)[:n].tolist()
        train_ds = Subset(train_ds, idx)
    return train_ds, test_ds


def make_optimizer(model, cfg):
    wd = float(cfg["weight_decay"])
    name = cfg["optimizer"]
    if name == "adam":
        return torch.optim.Adam(model.parameters(), lr=float(cfg["lr"]), weight_decay=wd)
    if name == "adamw":
        return torch.optim.AdamW(model.parameters(), lr=float(cfg["lr"]), weight_decay=wd)
    if name == "sgd":
        return torch.optim.SGD(model.parameters(), lr=float(cfg["lr"]),
                               momentum=0.9, weight_decay=wd)
    raise RuntimeError(f"未知 optimizer：{name}（支持 adam/adamw/sgd）")


def make_scheduler(opt, cfg):
    epochs = int(cfg["epochs"])
    name = cfg["scheduler"]
    if name == "none":
        return None
    if name == "cosine":
        return torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))
    if name == "step":
        return torch.optim.lr_scheduler.StepLR(opt, step_size=max(1, epochs // 2), gamma=0.5)
    raise RuntimeError(f"未知 scheduler：{name}（支持 none/cosine/step）")


@torch.no_grad()
def evaluate(model, loader, device) -> tuple[float, float]:
    model.eval()
    correct, total, loss_sum = 0, 0, 0.0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        out = model(x)
        loss_sum += F.cross_entropy(out, y, reduction="sum").item()
        correct += (out.argmax(1) == y).sum().item()
        total += y.size(0)
    return correct / total, loss_sum / total


def main() -> int:
    cfg = load_config()
    seed = int(cfg.get("seed", os.environ.get("TANSUO_TRIAL_SEED", "0")))
    torch.manual_seed(seed)
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    train_ds, test_ds = make_datasets(cfg)
    bs = int(cfg["batch_size"])
    train_loader = DataLoader(train_ds, batch_size=bs, shuffle=True, num_workers=0)
    test_loader = DataLoader(test_ds, batch_size=256, shuffle=False, num_workers=0)

    model = SmallCNN(int(cfg["width"]), float(cfg["dropout"])).to(device)
    opt = make_optimizer(model, cfg)
    sched = make_scheduler(opt, cfg)
    epochs = int(cfg["epochs"])

    last = {"val_acc": 0.0, "val_loss": float("inf"), "train_loss": float("inf")}
    for epoch in range(1, epochs + 1):
        t0 = time.perf_counter()
        model.train()
        loss_sum, n = 0.0, 0
        diverged = False
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            loss = F.cross_entropy(model(x), y)
            if not torch.isfinite(loss):
                diverged = True
                break
            loss.backward()
            opt.step()
            loss_sum += loss.item() * y.size(0)
            n += y.size(0)
        if sched is not None and not diverged:
            sched.step()
        train_loss = (loss_sum / n) if n > 0 else float("nan")
        val_acc, val_loss = evaluate(model, test_loader, device)
        last = {"val_acc": round(val_acc, 4), "val_loss": round(val_loss, 4),
                "train_loss": round(train_loss, 4),
                "epoch_time_s": round(time.perf_counter() - t0, 2)}
        emit({"type": "epoch", "epoch": epoch, "metrics": last})
        if diverged:
            break

    emit({"type": "final", "value": last["val_acc"], "metrics": last})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 契约：异常→stderr + 非零退出码（runner 记为 FAILED）
        print(f"train_mnist.py 失败：{e}", file=sys.stderr, flush=True)
        sys.exit(1)
