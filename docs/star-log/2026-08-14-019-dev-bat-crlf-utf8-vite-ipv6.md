---
date: 2026-08-14
number: "019"
title: 一键启动 bat 三连坑：LF 换行让 cmd 解析碎成片段、beta UTF-8 系统让 Default 编码撒谎、Vite 8 默认只绑 IPv6
severity: low
status: resolved
tags: [windows, bat, 编码, 换行符, vite, 一键启动]
module: dev.bat / web/vite.config.ts
---

# 一键启动 bat 三连坑：LF 换行让 cmd 解析碎成片段、beta UTF-8 系统让 Default 编码撒谎、Vite 8 默认只绑 IPv6

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent；新增根目录 `dev.bat`（一键启动前后端：
  `python cli.py web` :8000 + `npm run dev` :5173，各开独立窗口）
- **环境**：Windows 11，关键特性：**系统启用了「Beta: 使用 Unicode UTF-8 提供
  全球语言支持」**——`[System.Text.Encoding]::Default.CodePage = 65001`、
  控制台 `chcp` 也是 65001（非常规的 936）
- **当时在做什么**：写一个使用相对路径、双击可用的中文说明 bat
- **影响范围**：仅开发体验脚本；但排查中暴露的规律（cmd 对 bat 的解析要求、
  beta UTF-8 系统的编码陷阱）影响今后在本机写任何 .bat/.cmd
- **复现步骤**：用 Write 工具（UTF-8 + LF 换行）写含中文的 bat，双击运行，
  控制台刷出一屏 `'xxx' 不是内部或外部命令`，且碎片毫无规律

## T · 目标（Task）

- **要达成什么**：双击 bat 后前后端各起一个窗口并真实可用（含 Vite `/api`
  代理链路）；脚本在本仓库相对路径约束下工作
- **约束条件**：中文输出要正常显示；不动 `python cli.py web` 生产模式语义

## A · 解决方案（Action）

### 失败尝试全过程（每一版都炸，错误形态各不相同）

| 版本 | 编码/换行 | 典型报错（逐字） |
|------|-----------|------------------|
| v1：UTF-8 + `chcp 65001` | UTF-8，LF | `'/api' 不是内部或外部命令`、`'前端' 不是…`、`'装前端依赖（仅首次需要）...' 不是…`、`'on' 不是…` |
| v2：`Set-Content -Encoding Default`（以为是 GBK） | 实为 UTF-8，LF | `'em'`、`'ttp:'`、`'cho'`、`'thon'`——行首字符被吞 |
| v3：显式 `GetEncoding(936)` + 文件第二行 `chcp 936 >nul` | GBK，LF | `'0.1:8000'`、`'st'`、`'pm'`、`']'`、`'2'`、`'o.'`——碎得更彻底 |

三版错误的共同形态：**完整行被拆成随机片段逐条执行**（`start`→`'st'`、
`npm`→`'pm'`、`echo.`→`'o.'`），像是 cmd 不按行读取文件。

### 真正根因：LF 换行

```powershell
$cr = ($b | Where-Object { $_ -eq 0x0D }).Count   # → CR=0
$lf = ($b | Where-Object { $_ -eq 0x0A }).Count   # → LF=34
```

**CR=0**——Write/Edit 工具落盘全是 LF-only，而 **cmd 的批处理解析器要求
CRLF**：没有 CR 时它按字节流乱切，多字节中文和块语法进一步放大错乱。
把三版错误全归因于"编码"是误判；换行符才是主因。修复：

```powershell
$t = [System.IO.File]::ReadAllText($p, $gbk)
$t = $t.Replace("`r`n", "`n").Replace("`n", "`r`n")   # 规范化为 CRLF
[System.IO.File]::WriteAllText($p, $t, $gbk)
```

CRLF 后脚本一次通过。**编码仍保留 GBK + `chcp 936`**：beta UTF-8 系统上
"默认 ANSI"不可靠（见下），GBK+936 是中文 Windows 上含中文 bat 的稳健组合。

### 伴生发现 1：beta UTF-8 让 `-Encoding Default` 撒谎

本机 `Encoding.Default.CodePage = 65001`，所以 v2 以为写出 GBK、实际是
UTF-8。教训：写代码页相关代码前先查
`[System.Text.Encoding]::Default.CodePage`，别信"Default=GBK"的常识。

### 伴生发现 2：Vite 8 默认只绑 `[::1]`

bat 跑通后前端窗口正常，但 `http://127.0.0.1:5173` 拒连：

```
netstat -ano | findstr :517
  TCP    [::1]:5173    [::]:0    LISTENING    39024
```

Vite 8.2.1 默认 host=localhost 只绑 IPv6 loopback，脚本里打印的
`127.0.0.1` 地址根本连不上。修复：`web/vite.config.ts` 加
`server.host: "127.0.0.1"`；注意 **server 段配置变更不会热生效**，要重启
dev server（文件监听重启不重绑端口）。

### 伴生发现 3：本会话环境对长 PowerShell 命令有沙箱限制

两版含大段 here-string 的写文件命令直接
`EPERM: operation not permitted, uv_spawn '…powershell.exe'`，短命令正常。
对策：内容交给文件编辑工具写，PowerShell 只做短小的编码转换/验证。

### 最终 dev.bat 形态（GBK 编码、CRLF、相对路径）

```bat
@echo off
chcp 936 >nul
rem …中文说明…
cd /d %~dp0
if not exist web\node_modules ( pushd web & call npm install & popd )
start "tansuo-backend" cmd /k python cli.py web
start /d web "tansuo-frontend" cmd /k npm run dev
```

`cd /d %~dp0` 锚定脚本目录后全部走相对路径；`start /d web` 用 start 自己的
工作目录参数进入前端目录；附 node_modules 缺失自动 `npm install`（仅首次）。

## R · 实际效果（Result）

- **验证方式**：`cmd /c "echo. | .\dev.bat"` 输出干净无报错；
  `http://127.0.0.1:8000/api/health` 200；`http://127.0.0.1:5173/` 200；
  `http://127.0.0.1:5173/api/health` 200（Vite 代理链路通）；netstat 确认
  :5173 绑在 IPv4；测试完按 PID 清理，端口全部释放
- **前后对比**：从"双击刷一屏不是内部或外部命令"到一键起前后端 + 代理链路
  全通；README「方式二」补一行 dev.bat 说明
- **副作用与代价**：`web/vite.config.ts` 绑死 127.0.0.1（仅影响 dev server，
  生产构建无关）；dev.bat 以 GBK 入库，UTF-8 环境查看注释会乱码但执行无碍
- **遗留问题与后续**：无
- **经验教训**：
  1. **bat 报"不是内部或外部命令"且碎片没规律时，先查换行符再查编码**——
     LF-only 的 bat 在 cmd 里就是逐字节乱切，错误形态极具迷惑性；
  2. **含中文的 bat 用 GBK + 文件内 `chcp 936`**，别依赖系统默认代码页，
     尤其本机开了 beta UTF-8（ACP=65001），`-Encoding Default` 会骗人；
  3. **编辑器/工具写出的文件换行要按消费者校验**：cmd 要 CRLF，一个
     `Where-Object { $_ -eq 0x0D }` 计数就能验明；
  4. **给用户的 URL 要和实际绑定一致**：Vite 8 默认 `[::1]`，打印
     127.0.0.1 之前先显式 `server.host`。
