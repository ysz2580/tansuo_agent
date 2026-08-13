---
date: 2026-08-13
number: "013"
title: git push 连续报 schannel TLS 握手失败：代理端口存活但上游断连，直连兜底推送
severity: medium
status: workaround
tags: [git, 代理, schannel, TLS, windows, 推送]
module: 版本控制 / 开发环境
---

# git push 连续报 schannel TLS 握手失败：代理端口存活但上游断连，直连兜底推送

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 仓库，推送到远端 `https://github.com/ysz2580/tansuo_agent.git`
- **环境**：Windows（PowerShell 5.1）；`git version 2.55.0.windows.2`（SSL 后端为
  Windows 自带的 schannel）；仓库级配置了 HTTP 代理
  （`file:.git/config` 中 `http.proxy = http://127.0.0.1:7897`，本地代理客户端监听端口）
- **当时在做什么**：数据集指纹（第三维度）功能开发完毕，全套回归通过后提交
  （`2e26256`）并执行 `git push`
- **问题表现**：`git push` 以退出码 128 失败，报错：

  ```
  fatal: unable to access 'https://github.com/ysz2580/tansuo_agent.git/': schannel: failed to receive handshake, SSL/TLS connection failed
  ```

  连续 3 次失败（首次、立即重试、等待 5 秒后重试，报错逐字相同）。而在 2 天前
  （2026-08-11，提交 `28a6d79` 后）出现过**逐字相同**的报错，当时重试 1 次即成功
  （`028bfec..28a6d79`）——同一个报错信息，两次根因不同。
- **影响范围**：阻塞推送；本地提交不受影响，远端落后于本地
- **复现步骤**：1) 本地代理客户端上游断连（但客户端进程仍在监听 7897 端口）时；
  2) 执行 `git push`；3) 100% 复现上述 schannel 报错。代理上游恢复后不再复现。

## T · 目标（Task）

- **要达成什么**：把本地提交推到 GitHub；并沉淀一条推送策略，以后遇到同样报错
  不必重新排查
- **验收标准**：推送成功（远端 ref 前进）；推送策略写进项目约定，后续会话可直接执行
- **约束条件**：不改动仓库里的 `http.proxy` 配置（用户有意固定，代理恢复后应自动
  生效）；不关闭 TLS 校验等削弱安全的做法

## A · 解决方案（Action）

### 排查过程

1. 第一次失败时按 2 天前的经验直接重试——仍失败；等待 5 秒第三次重试——仍失败。
   三次同错说明这次不是瞬时抖动，与 2 天前"重试即好"的根因不同。
2. 核对代理配置与端口：

   ```powershell
   git config --get http.proxy
   # → http://127.0.0.1:7897
   Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -InformationLevel Quiet
   # → True
   ```

   代理配置在、端口也在监听——**端口可达不代表代理可用**。
3. 关键一步：分别经代理与直连访问 GitHub，隔离故障段：

   ```powershell
   Invoke-WebRequest -Uri "https://github.com" -Proxy "http://127.0.0.1:7897" -UseBasicParsing -TimeoutSec 20
   # → 失败：基础连接已经关闭: 发送时发生错误。
   Invoke-WebRequest -Uri "https://github.com" -UseBasicParsing -TimeoutSec 20
   # → 200
   ```

   结论：代理客户端进程活着（端口在监听）但**上游已断**，git 的流量经代理出不去；
   本机直连 GitHub 畅通。schannel 报错只是表象，根因在代理上游，不在 git/TLS 配置。
4. 据此选择直连兜底，用一次性 `-c` 覆盖代理配置（不动仓库配置）：

   ```powershell
   git -c http.proxy= push
   # → To https://github.com/ysz2580/tansuo_agent.git
   #      28a6d79..2e26256  main -> main
   ```

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 原地重试 `git push`（3 次，含间隔 5 秒） | 失败 | 本次根因是代理上游持续断连，不是瞬时抖动；重试同一条路无效（2 天前那次成功只是当时代理恰好恢复） |
| 改仓库 `http.proxy` 配置删掉代理 | 放弃 | 用户有意固定该配置，代理恢复后应自动走代理；改动会污染仓库配置 |
| 关闭/放宽 TLS 校验 | 放弃 | 削弱安全，且根因不在 TLS 配置 |
| `git -c http.proxy= push` 一次性直连 | 有效，采用 | 不动仓库配置，仅本次推送绕过代理 |

### 最终方案

1. 推送策略固化为项目约定，新建文件 `CLAUDE.md`（提交 `3b20142`）：先按仓库配置
   走代理 `git push`；失败则直连兜底 `git -c http.proxy= push`；同一条路不重试超过
   2 次，先换路再诊断（`Test-NetConnection` 查端口、`Invoke-WebRequest -Proxy`
   查上游）。
2. 该约定随即实战验证：推送 `3b20142` 时 `git push`（代理）仍报同样的 schannel
   错误，`git -c http.proxy= push` 直连成功（`2e26256..3b20142`）。

## R · 实际效果（Result）

- **验证方式**：两次推送均看到远端 ref 前进（`28a6d79..2e26256`、
  `2e26256..3b20142`），且第二次完整演练了"先代理失败→直连成功"的双路径流程
- **前后对比**：此前同一报错只能盲目重试（2 天前碰巧一次就好）；现在有明确的
  两步流程与隔离诊断命令，3 条命令内可定位故障段
- **副作用与代价**：无。直连覆盖仅作用于单次命令；代理上游恢复后 `git push`
  自动回到代理路径
- **遗留问题与后续**：代理客户端上游为何断连属外部问题（代理软件本身），本记录
  不覆盖；若直连长期可用，可考虑清理仓库 `http.proxy` 配置（由用户决定）
- **经验教训**：1) `schannel: failed to receive handshake` 是**表象**，至少对应两种
  根因——网络瞬时抖动（重试即好）与代理上游断连（重试无效）；区分方法是
  `Invoke-WebRequest` 分别经代理/直连访问目标；2) "端口在监听"不等于"代理可用"，
  `Test-NetConnection` 通过不代表流量能出去；3) 同一报错不同根因，值得把诊断命令
  沉淀进项目约定而不是依赖上次碰运气成功的经验
