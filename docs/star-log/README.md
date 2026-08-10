# 开发问题日志（STAR）

> 记录本项目开发中遇到的问题及完整解决过程，每条记录遵循 STAR 框架：
> **S** 背景（Situation）· **T** 目标（Task）· **A** 解决方案（Action）· **R** 实际效果（Result）。
> 遇到类似问题先查下表，点链接看详情。

| 日期 | 编号 | 标题 | 标签 | 状态 |
|------|------|------|------|------|
| 2026-08-10 | 001 | [Optuna storage 拒绝动态修改分类参数候选集，set_choices（收窄 choices）设计整体作废](2026-08-10-001-optuna-dynamic-categorical-space.md) | `optuna` `搜索空间` `动态空间` `TPE` `设计决策` | ✅ 已解决 |
| 2026-08-10 | 002 | [Optuna 4.9 中 MedianPruner 参数 interval 改名为 interval_steps，旧写法 TypeError](2026-08-10-002-optuna-medianpruner-interval-steps.md) | `optuna` `版本兼容` `pruner` `API 变更` | ✅ 已解决 |
| 2026-08-10 | 003 | [程序生成的 settings.yaml 中 Windows 路径进双引号标量，YAML 转义报错配置加载失败](2026-08-10-003-yaml-windows-path-escape.md) | `yaml` `windows` `路径` `转义` `配置` | ✅ 已解决 |
| 2026-08-10 | 004 | [trial#8 训练脚本瞬时退出码 1（stderr 为空），单独复现正常，判定环境瞬时故障](2026-08-10-004-transient-trial-exit-code-1.md) | `子进程` `瞬时故障` `容错` `诊断` `windows` | 🔴 未解决 |

状态图例：✅ 已解决 · 🔶 绕过 · 🔴 未解决 · 🔄 处理中
