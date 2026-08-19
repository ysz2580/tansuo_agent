---
date: 2026-08-19
number: "031"
title: dev.bat 启动不做端口占用检测，旧后端进程占着 8000 导致 WinError 10048 启动失败
severity: low
status: resolved
tags: [windows, bat, 端口占用, 一键启动]
module: dev.bat
---

# dev.bat 启动不做端口占用检测，旧后端进程占着 8000 导致 WinError 10048 启动失败

## S · 背景（Situation）

- **项目 / 模块**：`dev.bat` 一键启动脚本（GBK+CRLF，见 STAR #019）。
- **环境**：Windows，后端 `python cli.py web` 绑 `127.0.0.1:8000`，前端 Vite dev 绑 `:5173`。
- **当时在做什么**：前一天开发会话遗留的旧后端进程（pid 4908，上午 9:28 启动）一直占着 8000 端口；用户第二天启动新后端时报错：

  ```
  ERROR:    [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000): [winerror 10048] 通常每个套接字地址(协议/网络地址/端口)只允许使用一次。
  ```

- **影响范围**：每次有遗留进程（忘关窗口、直接关终端没杀子进程、代码更新后旧进程还在跑旧代码）都要手动 `netstat` 找 PID 再 `taskkill`。且旧进程跑的是旧代码，即便不报错也容易造成「改了没生效」的困惑。

## T · 目标（Task）

- **要达成什么**：dev.bat 启动前自动检测 8000/5173，被占用则结束占用进程再启动（用户明确要求）。
- **验收标准**：`netstat` 能匹配出两个端口的 LISTENING 行与 PID；杀不掉时给出明确警告而不是静默继续。
- **约束条件**：bat 必须保持 GBK(936)+CRLF+`chcp 936 >nul`（STAR #019 的结论）；中文文案用 Write 工具写 UTF-8 再短命令转码，不在 shell 里拼长 here-string（沙箱 EPERM）。

## A · 解决方案（Action）

### 最终方案

`dev.bat` 新增 `:free_port` 子程序，在启动前后端之前各调一次：

```bat
call :free_port 8000 后端
call :free_port 5173 前端
...
:free_port
rem %~1=端口号  %~2=用途（仅用于提示文案）
set "FP_FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":%~1.*LISTENING"') do (
    if not "%%p"=="0" (
        echo [提示] 端口 %~1（%~2）被进程 %%p 占用，正在结束以腾出端口...
        taskkill /F /PID %%p >nul 2>&1
        set FP_FOUND=1
    )
)
if defined FP_FOUND (
    timeout /t 1 /nobreak >nul
    netstat -ano | findstr /r ":%~1.*LISTENING" >nul
    if not errorlevel 1 echo [警告] 端口 %~1 仍被占用，请手动结束占用进程后重试。
)
goto :eof
```

要点：
1. `findstr /r ":%~1.*LISTENING"` 只认 LISTENING 行（避开 TIME_WAIT 等无 PID 的行）；`tokens=5` 取 PID 列。
2. `taskkill` 失败不阻塞（`>nul 2>&1`），靠杀后**复检**兜底：仍 LISTENING 就打 `[警告]` 提示手动处理（典型是权限不够）。
3. 杀完 `timeout /t 1` 等端口真正释放再启动，避免竞态。
4. `pause` 后必须 `goto :eof`，否则脚本会坠入 `:free_port` 子程序体。
5. 编码流程：Write 工具写 UTF-8 → 一条 PowerShell 短命令规范化 CRLF 并转 936：

   ```powershell
   $t=[IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)
   $t=$t -replace "`r`n","`n"; $t=$t -replace "`n","`r`n"
   [IO.File]::WriteAllText($p,$t,[Text.Encoding]::GetEncoding(936))
   ```

## R · 实际效果（Result）

- **验证方式**：GBK 解码回读全文无乱码；`netstat -ano | findstr /r ":8000.*LISTENING"` 与 `:5173` 各自匹配出占用 PID（4908 / 27776），与 `Get-NetTCPConnection` 结果一致。
- **前后对比**：原来遗留进程 → 启动失败报错、手动查杀；现在 bat 自动结束占用进程并提示，杀不掉时明确警告。
- **副作用与代价**：自动 `taskkill /F` 是破坏性动作——若用户恰好在同端口跑了**别的项目**的服务也会被一并结束；提示行会打印被杀进程 PID，便于事后辨认。开发机单项目场景下利大于弊。
- **遗留问题与后续**：无。若将来要更温和，可先按进程命令行过滤只杀 `cli.py web`/`node ... vite`（wmic 查询），当前从简。
- **经验教训**：一键启动脚本的健壮性清单里，「端口预检」与「依赖预检」同级——遗留进程是 Windows 开发环境最常见的隐形状态。
