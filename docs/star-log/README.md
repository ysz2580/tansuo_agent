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
| 2026-08-11 | 005 | [stop 杀进程树后进行中试验永远停留 RUNNING，而 Optuna 4.9 没有公开 API 可改试验状态](2026-08-11-005-optuna-orphaned-running-trials.md) | `optuna` `sqlite` `进程树` `web后端` `状态清理` | ✅ 已解决 |
| 2026-08-11 | 006 | [Web 后端拉起的 python 子进程日志不实时，stdout 重定向到文件后块缓冲，进程结束才一次性刷出](2026-08-11-006-subprocess-stdout-block-buffering.md) | `python` `子进程` `缓冲` `web后端` `日志` | ✅ 已解决 |
| 2026-08-11 | 007 | [cli.py run --trials 语义是「总预算」而非「本次新增次数」，Web 界面直接透传会让搜索立即结束或少跑](2026-08-11-007-trials-total-budget-semantics.md) | `cli` `语义` `web后端` `预算` | ✅ 已解决 |
| 2026-08-11 | 008 | [shadcn CLI 4.16.2 的 init 不再接受 --base-color，照旧教程初始化报错，参数已重组为 -b/--base 与 -p/--preset](2026-08-11-008-shadcn-cli-base-flag-renamed.md) | `shadcn` `前端` `cli` `版本兼容` | ✅ 已解决 |
| 2026-08-11 | 009 | [TypeScript 6.0 弃用 baseUrl（TS5101），Vite 脚手架默认 tsconfig 构建失败，paths 需脱离 baseUrl 单独使用](2026-08-11-009-typescript6-baseurl-ts5101.md) | `typescript` `vite` `tsconfig` `版本兼容` `前端构建` | ✅ 已解决 |

状态图例：✅ 已解决 · 🔶 绕过 · 🔴 未解决 · 🔄 处理中
