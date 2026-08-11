---
date: 2026-08-11
number: "010"
title: cli.py web --settings/--space 被静默忽略：app 模块在环境变量注入前就加载，永远回退 demo 配置
severity: high
status: resolved
tags: [cli, web后端, 模块加载顺序, 环境变量, 配置]
module: cli.py / tansuo.web.app
---

# cli.py web --settings/--space 被静默忽略：app 模块在环境变量注入前就加载，永远回退 demo 配置

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent，`cli.py` 的 `web` 子命令与 `tansuo/web/app.py`
- **环境**：Windows 11，Python 3.14（C:\Python314），FastAPI + uvicorn，optuna 4.9.0
- **当时在做什么**：为新增的 workers/时间预算特性做 Web 端到端冒烟——用临时目录配置
  `.smoke/settings.yaml`（实验名 smoke_parallel、total_trials=8）启动
  `python cli.py web --settings .smoke/settings.yaml --space demo/configs/search_space.yaml --port 8123`，
  然后请求 `/api/summary` 核对配置是否生效
- **问题表现**：接口返回的是 demo 配置而不是冒烟配置：

  ```
  experiment=mnist_demo workers=1 eta_s=623 budget_total=30
  ```

  预期是 `experiment=smoke_parallel budget_total=8`。没有任何报错——服务正常、
  接口 200，只是**读错了配置文件**。

- **影响范围**：自 Web 后端上线以来，`cli.py web --settings X --space Y` 的参数
  一直被静默忽略，永远使用 demo 配置（或环境变量中已存在的 TANSUO_SETTINGS）。
  用户拿自己的 settings 起 Web 会看到别人的实验数据，且 run/start 会往 demo 的
  data_dir 里写东西。冒烟测试首次用非 demo 配置启动才暴露。
- **复现步骤**：1) 准备任意非 demo 的 settings.yaml；2) `python cli.py web --settings 该文件 --port 8123`；
  3) GET /api/summary → experiment/budget_total 均为 demo 配置的值。100% 复现。

## T · 目标（Task）

- **要达成什么**：`cli.py web --settings/--space` 真正生效，Web 后端读写调用方指定的配置
- **验收标准**：用冒烟配置起服务后 `/api/summary` 返回 `experiment=smoke_parallel`、
  `budget_total=8`；经 `/api/run/start` 启动的子进程命令行包含冒烟配置的绝对路径
- **约束条件**：不改 app.py 的"模块加载时读取环境变量"设计（`uvicorn tansuo.web.app:app`
  直接从项目根起服务时也依赖这个回退逻辑）

## A · 解决方案（Action）

### 排查过程

1. 先怀疑后台任务的工作目录：确认 `.smoke/settings.yaml` 的 resolve 路径正确、文件存在
   （load_settings 对不存在的文件会明确报"找不到配置文件"，而接口返回了合法数据，
   说明它加载了**某个**有效配置——只能是 demo 的回退默认）。
2. 读 `cmd_web` 源码，发现语句顺序是：

   ```python
   def cmd_web(args) -> int:
       import uvicorn
       from tansuo.web.app import app      # ← 模块在这里加载
       os.environ["TANSUO_SETTINGS"] = ... # ← 环境变量在这之后才注入
       os.environ["TANSUO_SPACE"] = ...
   ```

3. 而 `tansuo/web/app.py` 顶部：

   ```python
   SETTINGS_PATH = os.environ.get(
       "TANSUO_SETTINGS", str(PROJECT_ROOT / "demo" / "configs" / "settings.yaml"))
   ```

   模块加载时环境变量还没被设置 → `os.environ.get` 命中回退默认 → demo 路径。
   之后 `cmd_web` 再设置环境变量为时已晚（模块不会重新求值）。
4. 为什么此前从未暴露：之前所有 Web 演示/测试都用默认 demo 配置起服务，
   回退值恰好等于期望值。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| app.py 改成每请求动态读环境变量 | 放弃 | 改动大且没必要；根因只是注入顺序 |
| cmd_web 里把环境变量注入挪到 import 之前 | 有效，采用 | 一行顺序调整即修根因 |

### 最终方案

调整 `cli.py::cmd_web` 的语句顺序（先注入环境变量，再导入 app），并加注释说明依赖关系：

```python
def cmd_web(args) -> int:
    # 路径以绝对路径经环境变量注入，后端不受启动目录影响。
    # 必须在导入 app 之前设置：app 模块加载时就读取这两个环境变量，
    # 顺序颠倒会让 --settings/--space 被静默忽略（永远回退 demo 配置）。
    os.environ["TANSUO_SETTINGS"] = str(Path(args.settings).resolve())
    os.environ["TANSUO_SPACE"] = str(Path(args.space).resolve())
    import uvicorn
    from tansuo.web.app import app
    ...
```

无其它配套操作（不需要清缓存/迁移数据）。

## R · 实际效果（Result）

- **验证方式**：重启服务后 GET /api/summary → `experiment=smoke_parallel budget_total=8`；
  POST /api/run/start 返回的子进程命令行包含
  `--settings E:\tansuo_agent\.smoke\settings.yaml`；随后完整跑通"Web 启动并行搜索→
  日志实时 tail→正常结束→eta_s 更新"全流程
- **前后对比**：修复前 `--settings` 100% 被忽略；修复后按指定配置加载
- **副作用与代价**：无
- **遗留问题与后续**：无
- **经验教训**：1) "启动器注入环境变量 → 模块加载时读取"是一种隐式时序契约，
  import 顺序就是接口的一部分，必须用注释钉死；2) 静默回退默认值（get 的第二参数）
  掩盖了注入失败——回退发生时值得打一行日志；3) 冒烟测试要覆盖"非默认配置"路径，
  默认值巧合等于期望值的用例验不出这类 bug
