---
date: 2026-08-10
number: "003"
title: 程序生成的 settings.yaml 中 Windows 路径进双引号标量，YAML 转义报错配置加载失败
severity: medium
status: resolved
tags: [yaml, windows, 路径, 转义, 配置]
module: tests/test_protocol.py（子进程协议测试）/ tansuo/config.py
---

# 程序生成的 settings.yaml 中 Windows 路径进双引号标量，YAML 转义报错配置加载失败

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent，`tests/test_protocol.py` 生成临时 settings.yaml（其中 `adapter.command` 写入 `sys.executable` 指向的 Python 解释器路径）；`tansuo/config.py` 负责加载
- **环境**：Windows，Python 3.14.6（解释器位于 `C:\Python314\python.exe`），PyYAML
- **当时在做什么**：跑子进程协议契约测试，测试用代码动态拼出 settings.yaml 文本（双引号风格）再交给 config.py 加载
- **问题表现**：

  ```
  yaml.scanner.ScannerError: while scanning a double-quoted scalar
    in "<unicode string>", line 7, column 13:
        command: ["C:\Python314\python.exe", "C:/U ... 
                  ^
  found unknown escape character 'p'
    in "<unicode string>", line 7, column 27:
        command: ["C:\Python314\python.exe", "C:/Users/夜月/AppDat ... 
                                ^
  ```

  外层被 `tansuo.config.ConfigError: ... YAML 解析失败：...` 包装抛出。

- **影响范围**：协议测试启动即失败；凡是程序动态生成、含 Windows 原生路径的 YAML 配置都会踩中
- **复现步骤**：1) 在 Windows 上把 `sys.executable`（如 `C:\Python314\python.exe`）原样写进**双引号** YAML 标量；2) `yaml.safe_load`；3) 100% 抛 ScannerError。`\P` 被当作转义序列解析，`P` 不是合法转义字符

## T · 目标（Task）

- **要达成什么**：动态生成的配置可被正常加载，子进程能按配置里的路径启动
- **验收标准**：协议测试全部通过；配置中的路径在 Windows 上真实可执行
- **约束条件**：不改 YAML 库行为，从生成端解决

## A · 解决方案（Action）

### 排查过程

1. 报错明确指出是"双引号标量扫描到未知转义字符"。YAML 规范里双引号字符串支持反斜杠转义（`\n`、`\t`、`\"` 等），而 `\P` 不在合法转义表里——Windows 路径分隔符 `\` 恰好踩进转义语法。
2. 对比发现同一行里另一个路径用了正斜杠（`C:/Users/...`）就没炸：YAML 把正斜杠当普通字符。
3. 结论：问题在**生成端把原生反斜杠路径塞进了双引号标量**，而不是解析端的 bug。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 保留双引号，期望解析器宽容 | 失败 | PyYAML 严格按规范，`\P` 即报错 |
| 改用单引号标量包裹路径 | 可行但弃用 | 单引号内反斜杠是字面量，能绕开；但统一改路径表示更彻底 |
| 路径统一转 POSIX 风格（正斜杠） | 有效，采用 | Windows API 同样接受正斜杠路径，且对 YAML/JSON/跨平台都安全 |

### 最终方案

生成配置的路径一律用 `Path(...).as_posix()` 转成正斜杠：

```python
# tests/test_protocol.py
from pathlib import Path
command = [Path(sys.executable).as_posix(), ...]
```

`Path("C:\\Python314\\python.exe").as_posix()` → `C:/Python314/python.exe`，进双引号标量不再触发转义解析，Windows 下子进程照样能按此路径启动（实测通过）。

## R · 实际效果（Result）

- **验证方式**：协议测试 12 项断言全过；子进程按配置中的正斜杠路径正常拉起。
- **前后对比**：配置加载从抛 ScannerError 到正常；路径语义无变化。
- **副作用与代价**：无（正斜杠路径在 Windows 上完全合法）。
- **遗留问题与后续**：无。
- **经验教训**：1) 凡是**程序动态生成**的 YAML/JSON 配置，路径先 `as_posix()` 再写入，不要让用户手写路径时才处理；2) YAML 双引号标量有转义语义，写 Windows 路径要么转义要么用正斜杠——这也是本项目 README 里 SQLite URL 用 `Path.as_posix()` 的同源原因。
