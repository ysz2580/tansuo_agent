"""demo2：Two Moons（双月）二分类 —— 纯 numpy 小型 MLP（待调参的示例仓库）。

这是一个"普通用户训练脚本"：不认识任何调参系统的协议——
- 超参数通过命令行参数传入；
- 指标按普通文本行打印到 stdout；
- 数据集为脚本内合成（双月 + 高斯噪声），无需外部文件。

运行示例：
    python train.py --lr 0.05 --hidden 64 --epochs 20 --batch-size 64 \
                    --wd 1e-4 --optim momentum --seed 7

关注指标：val_acc（验证集准确率，越高越好）；
附带观察：val_loss / train_loss。
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

N_TRAIN = 4000
N_TEST = 1000


def make_moons(n: int, seed: int):
    """合成双月数据集：两个交错半圆 + 高斯噪声（噪声偏大，超参敏感）。"""
    rng = np.random.default_rng(seed)
    half = n // 2
    t1 = rng.uniform(0, np.pi, half)
    t2 = rng.uniform(0, np.pi, n - half)
    x1 = np.stack([np.cos(t1), np.sin(t1)], axis=1) + rng.normal(0, 0.22, (half, 2))
    x2 = np.stack([1 - np.cos(t2), 0.3 - np.sin(t2)], axis=1) \
        + rng.normal(0, 0.22, (n - half, 2))
    x = np.vstack([x1, x2]).astype(np.float32)
    y = np.concatenate([np.zeros(half), np.ones(n - half)]).astype(np.float32)
    idx = rng.permutation(n)
    return x[idx], y[idx]


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class MLP:
    """2 -> hidden(ReLU) -> 1(sigmoid) 的小 MLP。"""

    def __init__(self, hidden: int, seed: int):
        rng = np.random.default_rng(seed + 1)
        scale1 = np.sqrt(2.0 / 2)
        scale2 = np.sqrt(2.0 / hidden)
        self.W1 = rng.normal(0, scale1, (2, hidden)).astype(np.float32)
        self.b1 = np.zeros(hidden, dtype=np.float32)
        self.W2 = rng.normal(0, scale2, (hidden, 1)).astype(np.float32)
        self.b2 = np.zeros(1, dtype=np.float32)
        # momentum / adam 状态
        self.mW1 = np.zeros_like(self.W1)
        self.mb1 = np.zeros_like(self.b1)
        self.mW2 = np.zeros_like(self.W2)
        self.mb2 = np.zeros_like(self.b2)
        self.vW1 = np.zeros_like(self.W1)
        self.vb1 = np.zeros_like(self.b1)
        self.vW2 = np.zeros_like(self.W2)
        self.vb2 = np.zeros_like(self.b2)

    def forward(self, x):
        self.h = np.maximum(0.0, x @ self.W1 + self.b1)
        return sigmoid(self.h @ self.W2 + self.b2).ravel()

    def backward(self, x, y, p, wd: float):
        n = x.shape[0]
        dz2 = (p - y).reshape(-1, 1) / n
        gW2 = self.h.T @ dz2 + wd * self.W2
        gb2 = dz2.sum(axis=0)
        dh = (dz2 @ self.W2.T) * (self.h > 0)
        gW1 = x.T @ dh + wd * self.W1
        gb1 = dh.sum(axis=0)
        return gW1, gb1, gW2, gb2

    def step(self, grads, optim: str, lr: float, t: int):
        gW1, gb1, gW2, gb2 = grads
        if optim == "sgd":
            for arr, g in ((self.W1, gW1), (self.b1, gb1),
                           (self.W2, gW2), (self.b2, gb2)):
                arr -= lr * g
        elif optim == "momentum":
            mu = 0.9
            pairs = ((self.W1, gW1, self.mW1), (self.b1, gb1, self.mb1),
                     (self.W2, gW2, self.mW2), (self.b2, gb2, self.mb2))
            for arr, g, m in pairs:
                m *= mu
                m += g
                arr -= lr * m
        elif optim == "adam":
            b1c, b2c, eps = 0.9, 0.999, 1e-8
            for arr, g, m, v in ((self.W1, gW1, self.mW1, self.vW1),
                                 (self.b1, gb1, self.mb1, self.vb1),
                                 (self.W2, gW2, self.mW2, self.vW2),
                                 (self.b2, gb2, self.mb2, self.vb2)):
                m[...] = b1c * m + (1 - b1c) * g
                v[...] = b2c * v + (1 - b2c) * g * g
                mhat = m / (1 - b1c ** t)
                vhat = v / (1 - b2c ** t)
                arr -= lr * mhat / (np.sqrt(vhat) + eps)
        else:
            raise ValueError(f"未知 optim：{optim}（支持 sgd/momentum/adam）")


def bce(y, p):
    p = np.clip(p, 1e-7, 1 - 1e-7)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def evaluate(model, x, y):
    p = model.forward(x)
    loss = bce(y, p)
    acc = float(np.mean(((p >= 0.5).astype(np.float32) == y)))
    return acc, loss


def main() -> int:
    ap = argparse.ArgumentParser(description="Two Moons MLP（numpy）")
    ap.add_argument("--lr", type=float, default=0.05, help="学习率")
    ap.add_argument("--hidden", type=int, default=64, help="隐藏层宽度")
    ap.add_argument("--epochs", type=int, default=20, help="训练轮数")
    ap.add_argument("--batch-size", type=int, default=64, help="批大小")
    ap.add_argument("--wd", type=float, default=1e-4, help="权重衰减（L2）")
    ap.add_argument("--optim", default="momentum",
                    choices=["sgd", "momentum", "adam"], help="优化器")
    ap.add_argument("--seed", type=int, default=7, help="随机种子")
    args = ap.parse_args()

    x_train, y_train = make_moons(N_TRAIN, args.seed)
    x_test, y_test = make_moons(N_TEST, args.seed + 1000)
    model = MLP(args.hidden, args.seed)
    rng = np.random.default_rng(args.seed + 2)

    t_global = 0
    last_acc = 0.0
    for epoch in range(1, args.epochs + 1):
        perm = rng.permutation(N_TRAIN)
        losses = []
        for s in range(0, N_TRAIN, args.batch_size):
            idx = perm[s:s + args.batch_size]
            xb, yb = x_train[idx], y_train[idx]
            p = model.forward(xb)
            loss = bce(yb, p)
            if not np.isfinite(loss):
                print(f"[epoch {epoch}/{args.epochs}] train_loss=nan（发散）",
                      flush=True)
                print(f"final: val_acc={last_acc:.4f}", flush=True)
                return 0
            grads = model.backward(xb, yb, p, args.wd)
            t_global += 1
            model.step(grads, args.optim, args.lr, t_global)
            losses.append(loss)
        val_acc, val_loss = evaluate(model, x_test, y_test)
        last_acc = val_acc
        print(f"[epoch {epoch}/{args.epochs}] "
              f"train_loss={np.mean(losses):.4f} "
              f"val_acc={val_acc:.4f} val_loss={val_loss:.4f}", flush=True)

    print(f"final: val_acc={last_acc:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
