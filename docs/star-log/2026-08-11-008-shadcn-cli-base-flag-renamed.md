---
date: 2026-08-11
number: "008"
title: shadcn CLI 4.16.2 的 init 不再接受 --base-color，照旧教程初始化报错，参数已重组为 -b/--base 与 -p/--preset
severity: low
status: resolved
tags: [shadcn, 前端, cli, 版本兼容]
module: web/（前端脚手架）
---

# shadcn CLI 4.16.2 的 init 不再接受 --base-color，照旧教程初始化报错，参数已重组为 -b/--base 与 -p/--preset

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent Web 前端脚手架（`web/`，Vite + React 19 + TS + Tailwind v4）
- **环境**：Node v24.18.0，npm 11.16.0（registry 为 npmmirror 镜像），shadcn CLI 4.16.2（`npx shadcn@latest`），tailwindcss 4.3.3
- **当时在做什么**：按网上教程执行 `npx shadcn@latest init --base-color <颜色>` 初始化 shadcn/ui
- **问题表现**：CLI 拒绝该参数、初始化失败（报错原文未留存——大意是 unknown option，且缺少必要选项导致流程走不下去）
- **影响范围**：阻塞前端脚手架初始化；与网络无关（当时已准备好代理兜底，实际没用上）
- **复现步骤**：1) 任意 Tailwind v4 项目；2) `npx shadcn@latest init --base-color slate`（shadcn 4.16.x）；3) 报错退出

## T · 目标（Task）

- **要达成什么**：完成 shadcn/ui 初始化，生成 components.json 与主题变量
- **验收标准**：init 成功，后续 `npx shadcn@latest add <组件>` 可正常拉取组件
- **约束条件**：不降级 CLI 版本（`@latest` 是项目惯例，降级只会把问题留给下一个人）

## A · 解决方案（Action）

### 排查过程

1. 先怀疑网络（shadcn 组件模板要访问 ui.shadcn.com）——但报错是参数层面而非请求层面，排除。
2. 直接查当前版本的帮助：`npx shadcn@latest init --help`，发现新版参数已重组：
   - `-b, --base <base>`：组件库基座，取值 base / **radix** / aria；
   - `-p, --preset <preset>`：主题预设，取值 nova / vega 等（取代了旧 `--base-color` 的职责）；
   - `-y, --yes`：跳过交互确认。
3. 确认教程里的 `--base-color` 是旧版参数，4.16.x 已移除。

### 最终方案

```powershell
npx shadcn@latest init -y -b radix -p nova
```

初始化成功后按需添加组件（此命令形式未变）：

```powershell
npx shadcn@latest add button card table tabs input label switch badge dialog textarea separator scroll-area sonner alert select
```

## R · 实际效果（Result）

- **验证方式**：init 生成 `components.json` 与含完整主题变量的 `src/index.css`（nova 预设）；15 个组件全部添加成功；`npm run build` 通过
- **前后对比**：从 init 直接失败到一次成功
- **副作用与代价**：无
- **遗留问题与后续**：无
- **经验教训**：迭代极快的脚手架类 CLI（shadcn、tailwind、create-vite 等）的参数名是高频变更项，教程的保鲜期很短——命令失败时先跑 `--help` 对照当前版本参数，不要反复按教程重试
