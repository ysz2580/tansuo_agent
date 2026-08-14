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
| 2026-08-11 | 010 | [cli.py web --settings/--space 被静默忽略：app 模块在环境变量注入前就加载，永远回退 demo 配置](2026-08-11-010-cli-web-settings-env-order.md) | `cli` `web后端` `模块加载顺序` `环境变量` `配置` | ✅ 已解决 |
| 2026-08-11 | 011 | [Optuna journal:// 存储在 Windows 上创建符号链接锁失败（WinError 1314 无特权），测试与降级存储不可用](2026-08-11-011-optuna-journal-storage-windows-symlink.md) | `optuna` `journal存储` `windows` `符号链接` `测试` | ✅ 已解决 |
| 2026-08-11 | 012 | [Optuna create_study(load_if_exists=True) 静默丢弃请求的 direction，改主指标方向续跑会让排序/剪枝/报告整体反向](2026-08-11-012-optuna-silent-direction-poisoning.md) | `optuna` `direction` `断点续跑` `记录分区` `数据污染` | ✅ 已解决 |
| 2026-08-13 | 013 | [git push 连续报 schannel TLS 握手失败：代理端口存活但上游断连，直连兜底推送](2026-08-13-013-git-push-schannel-proxy-upstream-down.md) | `git` `代理` `schannel` `TLS` `windows` `推送` | 🔶 绕过 |
| 2026-08-13 | 014 | [torch.__version__ 是 str 子类，写入 meta.yaml 时 PyYAML safe_dump 拒绝序列化](2026-08-13-014-torch-version-str-subclass-yaml.md) | `pyyaml` `torch` `序列化` `环境审计` | ✅ 已解决 |
| 2026-08-13 | 015 | [Agent 提示词硬编码导致监督策略迭代只能改代码：重构为 {{var}} 模板引擎（弃用 str.format），前端可编辑、版本化、可回滚](2026-08-13-015-prompt-template-override-rendering.md) | `提示词` `模板渲染` `设计决策` `web后端` `版本管理` | ✅ 已解决 |
| 2026-08-14 | 016 | [三项用户视角优化（提示词版本进分区标记 / 跑完 webhook 通知 / LLM 花费可见）的设计权衡，及写回正则吞行尾注释的真实 bug](2026-08-14-016-prompt-mark-webhook-notify-token-usage.md) | `设计决策` `webhook通知` `token用量` `分区标记` `配置写回` | ✅ 已解决 |
| 2026-08-14 | 017 | [参数重要度与参数-取值关系图的设计取舍，及 optuna 默认重要度评估器依赖 sklearn 的隐性陷阱](2026-08-14-017-param-importance-relation-chart.md) | `设计决策` `optuna` `参数重要度` `可视化` `依赖陷阱` | ✅ 已解决 |

状态图例：✅ 已解决 · 🔶 绕过 · 🔴 未解决 · 🔄 处理中
