---
date: 2026-08-11
number: "006"
title: Web 后端拉起的 python 子进程日志不实时，stdout 重定向到文件后块缓冲，进程结束才一次性刷出
severity: medium
status: resolved
tags: [python, 子进程, 缓冲, web后端, 日志]
module: tansuo/web/run_manager.py
---

# Web 后端拉起的 python 子进程日志不实时，stdout 重定向到文件后块缓冲，进程结束才一次性刷出

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent Web 后端运行驱动（`tansuo/web/run_manager.py`）
- **环境**：Windows，Python 3.14.6；`subprocess.Popen` 把子进程 stdout/stderr 重定向到 `web_run_<时间戳>.log` 文件
- **当时在做什么**：实现前端「实时日志」——RunManager 启动 `python cli.py run ...`，前端轮询 `/api/run/log?tail=N` 读日志文件尾部展示
- **问题表现**：启动搜索后日志区长时间空白，试验明明在跑（SQLite 里能看到新试验），但日志文件是空的；直到进程结束（或被 taskkill 杀掉）时，全部日志才一次性出现
- **影响范围**：「实时日志」功能形同虚设，用户无法观察运行进度，只能靠试验表猜状态
- **复现步骤**：1) `Popen(cmd, stdout=open(log,"w"), stderr=STDOUT)` 启动任意输出量小的 python 脚本；2) 运行中反复读日志文件——内容为空；3) 进程结束后文件才出现全部内容。100% 复现

## T · 目标（Task）

- **要达成什么**：子进程的每一行输出（试验进度、agent 唤醒日志）能即时出现在日志文件里，前端轮询可见
- **验收标准**：start 后数秒内 `/api/run/log` 就能读到最新输出，随运行持续增长
- **约束条件**：不改训练脚本与 orchestrator 的打印代码；方案对 Windows/POSIX 都有效

## A · 解决方案（Action）

### 排查过程

1. 先怀疑轮询端点读错了文件/时机不对——直接 `Get-Content` 日志文件，同样是空的，排除后端读取问题。
2. 再怀疑子进程没启动成功——但 SQLite study 里新试验在增加，说明进程在正常跑，只是输出没落盘。
3. 由此锁定 Python 输出缓冲机制：stdout 连接终端（TTY）时是行缓冲，**重定向到文件时默认切换为块缓冲**（数 KB 一块）。调参运行每行日志很短，缓冲区迟迟不满，直到进程退出才最终 flush。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 前端加大轮询间隔"等日志出现" | 放弃 | 治标都算不上，进程不死日志就是不出来 |
| 环境变量 `PYTHONUNBUFFERED=1` | 有效但未采用 | 与 -u 等价；-u 写在命令行里更显眼，不易被环境配置覆盖 |
| 命令行加 `-u`（无缓冲模式） | 有效，采用 | — |

### 最终方案

在 `tansuo/web/run_manager.py` 的启动命令中给解释器加 `-u`：

```python
# -u：stdout 重定向到文件后默认块缓冲，前端要实时看日志必须无缓冲
cmd = [sys.executable, "-u", str(self.project_root / "cli.py"), "run",
       "--settings", self.settings_path, "--space", self.space_path]
```

无需重启其他服务，下次 start 即生效。

## R · 实际效果（Result）

- **验证方式**：start 后立刻轮询 `/api/run/log`，`[trial#N]` 进度、agent 唤醒等输出逐行实时出现；stop 杀掉进程后日志也不再丢失尾部
- **前后对比**：从"进程结束才见全部日志"变为"每行秒级可见"
- **副作用与代价**：无缓冲模式理论上对大量输出的程序有微小性能影响；本项目日志量很小，忽略不计
- **遗留问题与后续**：无
- **经验教训**：CPython 的 stdout 在 TTY 与文件两种目标下缓冲策略不同（行缓冲 vs 块缓冲），这是"子进程输出被采集做实时监控"场景（Web 控制台、CI 日志采集）的经典坑——凡是 `Popen(stdout=文件)` 且要实时读，先想到 `-u` 或 `PYTHONUNBUFFERED=1`
