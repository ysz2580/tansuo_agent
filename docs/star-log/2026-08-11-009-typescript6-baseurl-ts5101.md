---
date: 2026-08-11
number: "009"
title: TypeScript 6.0 弃用 baseUrl（TS5101），Vite 脚手架默认 tsconfig 构建失败，paths 需脱离 baseUrl 单独使用
severity: medium
status: resolved
tags: [typescript, vite, tsconfig, 版本兼容, 前端构建]
module: web/（前端构建配置）
---

# TypeScript 6.0 弃用 baseUrl（TS5101），Vite 脚手架默认 tsconfig 构建失败，paths 需脱离 baseUrl 单独使用

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent Web 前端（`web/`，Vite 8 + React 19 + TS）
- **环境**：TypeScript 6.0（`npm create vite@latest` 脚手架装入的当前版本），Node v24.18.0
- **当时在做什么**：前端页面全部写完、shadcn 组件就位后首次执行生产构建 `npm run build`（`tsc -b && vite build`）。为支持 `@/` 路径别名，tsconfig 里按惯例同时写了 `baseUrl` 与 `paths`
- **问题表现**：`npm run build` 在 tsc 阶段失败，报错误码 **TS5101**，指出 `baseUrl` 选项已被弃用（报错原文未逐字留存——大意为该选项已 deprecated、应改为不带 baseUrl 直接使用 paths）
- **影响范围**：生产构建完全阻塞，无法产出 `web/dist` 供 FastAPI 托管
- **复现步骤**：1) TypeScript 6.x 环境；2) tsconfig 中同时配置 `"baseUrl": "."` 与 `"paths"`；3) `tsc -b`；4) TS5101

## T · 目标（Task）

- **要达成什么**：构建通过，且 `@/` 别名在 tsc 与 Vite 两侧都继续可用
- **验收标准**：`npm run build` 成功产出 dist；源码中所有 `@/...` 导入无需改动
- **约束条件**：不降级 TypeScript（6.0 是当前正式版，降级是留坑）

## A · 解决方案（Action）

### 排查过程

1. TS5101 属于 TS51xx 系列"选项弃用/移除"错误。查 TypeScript 变更说明：`paths` 自 TS 5.x 起已支持**不依赖 baseUrl** 单独使用（路径相对 tsconfig 文件所在目录解析），而 `baseUrl` 在 6.0 被正式弃用并在构建中报错。
2. 确认脚手架生成的 `tsconfig.json` 与 `tsconfig.app.json` **两个文件**都带 `baseUrl`，只改一个不够。
3. 确认 `paths` 值 `"@/*": ["./src/*"]` 本来就是相对 tsconfig 的写法，去掉 baseUrl 后语义不变。

### 最终方案

1. 从 `web/tsconfig.json` 与 `web/tsconfig.app.json` 中删除 `"baseUrl": "."`，保留 paths 单独使用：

   ```jsonc
   // tsconfig.app.json（节选）
   "compilerOptions": {
     // "baseUrl": ".",   ← 删除（TS6.0 弃用，TS5101）
     "paths": { "@/*": ["./src/*"] }
   }
   ```

2. Vite 侧别名不受影响，无需改动（顺手把 `vite.config.ts` 里的 `__dirname` 换成了 ESM 规范的 `import.meta.dirname`，消除一个 ESM 警告）：

   ```ts
   resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } }
   ```

3. 重新 `npm run build`。

## R · 实际效果（Result）

- **验证方式**：`npm run build` 成功，产出 `web/dist`（入口 chunk 约 729 kB，仅 Vite 的 chunk 大小提示，与本问题无关）；dist 由 FastAPI 单端口托管后所有页面正常
- **前后对比**：tsc 阶段从 TS5101 失败到零错误通过
- **副作用与代价**：无；`@/` 别名行为不变
- **遗留问题与后续**：无
- **经验教训**：1) 脚手架装入的语言/工具版本可能比大多数教程假设的更新，弃用类报错（TS51xx 系列）应尽快按新写法修正而不是绕过；2) monorepo 式多 tsconfig（根 + app + node）要一起排查同一个选项，漏改一个构建照样失败
