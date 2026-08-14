---
date: 2026-08-14
number: "018"
title: 直连兜底推送只清 http.proxy 不够：.git/config 的 [http]/[https] 各有一条代理；且 Test-NetConnection 通过 ≠ TCP 可用
severity: medium
status: unresolved
tags: [git, 代理, 推送, windows, 网络诊断]
module: git config / CLAUDE.md
---

# 直连兜底推送只清 http.proxy 不够：.git/config 的 [http]/[https] 各有一条代理；且 Test-NetConnection 通过 ≠ TCP 可用

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent；推送 STAR #017 功能提交（`353bb41`）时的 git 推送环节
- **环境**：Windows 11，git 使用 schannel，仓库级代理 `http://127.0.0.1:7897`
  （CLAUDE.md 约定的双路径推送策略：代理失败则 `git -c http.proxy= push` 直连兜底，
  见 STAR #013）
- **当时在做什么**：功能提交完成后按约定推送，两条路先后失败：

```
# 代理路径
fatal: unable to access 'https://github.com/ysz2580/tansuo_agent.git/':
Failed to connect to github.com:443 over proxy 127.0.0.1 after 2066 ms:
Could not connect to server

# 直连兜底（git -c http.proxy= push）
fatal: unable to access '...': Failed to connect to github.com:443 after
21115 ms: Could not connect to server
```

- **影响范围**：推送受阻；本地提交安全（`353bb41` 及后续提交在本地仓库完好），
  不影响任何开发工作

## T · 目标（Task）

- **要达成什么**：完成推送；若不能，诊断出是配置问题还是环境问题，并修掉任何
  可复用的配置缺陷
- **约束条件**：CLAUDE.md 约定"不要在失败时反复重试同一条路超过 2 次；先换
  另一条路，再考虑诊断"；仓库级代理配置本身**勿删**

## A · 解决方案（Action）

### 诊断过程（逐步收窄）

1. **查代理端口**：`Test-NetConnection 127.0.0.1 -Port 7897` →
   `TcpTestSucceeded : False`——代理软件没在运行，代理路径失败原因明确。
2. **查直连可达性**：`Test-NetConnection github.com -Port 443` →
   `TcpTestSucceeded : True`（解析到 20.205.243.166）。看似直连可用，
   与 git 报错矛盾——**这个结果后来被证明不可信**（见第 4 步）。
3. **查残留代理配置**（发现命令缺陷）：
   `git -c http.proxy= config --show-origin --get-all http.proxy` 显示
   file:.git/config 的值仍在。查看 `.git/config` 原文：

   ```ini
   [http]
       proxy = http://127.0.0.1:7897
   [https]
       proxy = http://127.0.0.1:7897
   ```

   **兜底命令只清了 `http.proxy`，`https.proxy` 仍在**——对 https:// remote，
   git 用更特化的 `https.proxy`，请求照样走死掉的代理。
4. **补齐覆盖仍失败 → 用 curl 交叉验证 TCP 层**：
   `git -c http.proxy= -c https.proxy= push` 依旧 21s 超时；改用
   `curl.exe -sS -o NUL -w "%{http_code} connect=%{time_connect}s" --connect-timeout 15 https://github.com`：

   ```
   curl: (28) Connection timed out after 15010 milliseconds
   000 connect=0.000000s
   ```

   `connect=0.000000` = TCP 握手从未建立。curl 不读 git 配置、环境里也无
   任何 proxy 变量（已查），**证明直连 github:443 本身不可达**——
   第 2 步 Test-NetConnection 的 True 只是瞬时假象。
5. 结论分两层：命令缺陷（可修）+ 环境网络（代理软件未运行、直连被断，
   命令无解）。按约定不再盲试（代理路 1 次、直连路 2 次均已到限）。

### 修复落点

1. **CLAUDE.md 兜底命令补全**：`git -c http.proxy= push` →
   `git -c http.proxy= -c https.proxy= push`，并注明 `.git/config` 的
   `[http]`/`[https]` 各有一条 proxy、两条都要清；诊断清单补一条
   curl TCP 层验证命令。
2. 推送本身等网络恢复：代理软件启动后 `git push` 即可，或网络恢复后走
   补全的直连兜底。本地提交不受影响。

## R · 实际效果（Result）

- **验证方式**：`git -c http.proxy= -c https.proxy= config --get http.proxy`
  返回空（覆盖生效）；curl 交叉验证把"配置问题"与"网络问题"干净切开
- **前后对比**：兜底命令从"只清一条、https 流量仍走死代理"变为两条全清；
  但本次推送仍受阻于环境网络（代理未运行 + 直连 TCP 超时），状态记为未解决，
  待网络恢复后补推（本地 `353bb41` 起全部提交完好）
- **副作用与代价**：无（仅文档与命令修正，不触碰仓库代理配置本身）
- **遗留问题与后续**：补推动作待网络恢复执行；若 github 直连长期不可用，
  只能依赖代理软件常驻
- **经验教训**：
  1. **`git -c http.proxy=` 只覆盖一个键**：`.git/config` 里 `[http]` 与
     `[https]` 是两条独立配置，直连兜底必须两条同时清空，否则 https remote
     照样走代理——STAR #013 留下的兜底命令在本次被证不完整；
  2. **Test-NetConnection 的 True 不可尽信**：它是瞬时快照（且可能受 TCP
     fast-open 等影响）；判断"连接是否真能建立"用
     `curl.exe -w "connect=%{time_connect}"` 看握手耗时，`0.000000` 即从未握手；
  3. **推送失败先分"配置"与"网络"两层**：curl 不读 git 配置，是切开两层
     的最快探针；确认是环境网络后按约定停手，不做无谓重试。
