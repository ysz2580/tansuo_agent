# demo2：Two Moons 二分类小实验

一个用于演示「从零接入调参 agent」的示例训练仓库。

## 任务

在合成的 **Two Moons（双月）** 数据集上训练一个纯 numpy 实现的小型 MLP
（2 → hidden → 1），做二分类。数据由 `train.py` 内部生成（固定随机种子，
无需任何外部数据文件），噪声偏大，**超参数选择对验证集准确率影响明显**。

## 运行

```bash
python train.py --lr 0.05 --hidden 64 --epochs 20 --batch-size 64 \
                --wd 1e-4 --optim momentum --seed 7
```

依赖：仅 `numpy`。

## 命令行参数（超参数）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--lr` | float | 0.05 | 学习率 |
| `--hidden` | int | 64 | 隐藏层宽度 |
| `--epochs` | int | 20 | 训练轮数 |
| `--batch-size` | int | 64 | 批大小 |
| `--wd` | float | 1e-4 | 权重衰减（L2） |
| `--optim` | choice | momentum | 优化器：`sgd` / `momentum` / `adam` |
| `--seed` | int | 7 | 随机种子 |

## 指标

- **val_acc**（验证集准确率）——主指标，越高越好；
- val_loss / train_loss ——观察指标。

输出格式为普通文本行（每个 epoch 一行，结尾一行 `final:`）：

```
[epoch 3/20] train_loss=0.1783 val_acc=0.9350 val_loss=0.1519
...
final: val_acc=0.9840
```

## 备注

- 本脚本**不依赖任何调参框架**，超参全部经命令行传入；
- 单次训练约 0.5~2 秒（CPU，4000 训练样本 / 1000 验证样本）。
