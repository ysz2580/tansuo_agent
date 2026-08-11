---
date: 2026-08-11
number: "005"
title: stop 杀进程树后进行中试验永远停留 RUNNING，而 Optuna 4.9 没有公开 API 可改试验状态
severity: medium
status: resolved
tags: [optuna, sqlite, 进程树, web后端, 状态清理]
module: tansuo/web（app.py · run_manager.py）
---

# stop 杀进程树后进行中试验永远停留 RUNNING，而 Optuna 4.9 没有公开 API 可改试验状态

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent Web 后端（`tansuo/web/app.py`、`tansuo/web/run_manager.py`）
- **环境**：Windows，Python 3.14.6（`C:\Python314\python.exe`），Optuna 4.9.0，FastAPI 0.140.0；存储为 SQLite（URL 形如 `sqlite:///demo/data/....db`）
- **当时在做什么**：实现 Web 界面「停止搜索」——RunManager 用 `taskkill /F /T /PID` 杀 `python cli.py run` 整棵进程树，前端随后从 study 读取试验状态
- **问题表现**：停止后正在进行的那次试验（当时为 trial#13）永远停留在 RUNNING：
  - 仪表盘「进行中」计数永久失真；
  - 断点续跑与 `run_start` 的新增试验数换算都基于试验状态计数，孤儿 RUNNING 会干扰换算；
  - Optuna 4.9 没有任何命令/公开 API 能把既成试验改判为失败
- **影响范围**：仪表盘、试验表、预算换算全部显示失真；不影响数据完整性（SQLite 本身无损）
- **复现步骤**：1) `/api/run/start` 启动搜索；2) 任一试验进行中调用 `/api/run/stop`；3) 查询 `/api/trials`，最后一次试验 state 恒为 RUNNING

## T · 目标（Task）

- **要达成什么**：stop 之后把孤儿 RUNNING 试验如实标记为 FAIL，并把原因写进 journal 审计
- **验收标准**：stop 后 `/api/trials` 中无 RUNNING，被停试验显示 FAIL 且 journal 可查到原因；新一次 start 前也要清理上次遗留
- **约束条件**：不能用公开 API（4.9 已无此能力）；不能破坏 SQLite 中其他数据；`journal://` 降级存储没有数据库，要优雅跳过

## A · 解决方案（Action）

### 排查过程

1. 第一反应是旧版本的内部接口 `study._storage.set_trial_state(trial_id, TrialState.FAIL)` → AttributeError。
2. 转向 `optuna.storages.RDBStorage` 找同类方法 → 核对 4.9 的 API 后确认它同样没有 `set_trial_state`——状态迁移只在 ask/tell 流程内部发生，不开放对既成试验的修改。
3. 结论：Optuna 4.9 刻意不暴露"事后改试验状态"。但 SQLite 存储格式是透明的：`trials` 表的 `state` 列是 VARCHAR 字符串（'RUNNING'/'COMPLETE'/'PRUNED'/'FAIL' 文本本身，不是整数枚举），可以直连 SQL 修改。
4. 实现细节：先 SELECT 出 trial_id 与 number 再 UPDATE——journal 审计事件需要试验编号；且必须只碰 `state = 'RUNNING'` 的行。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| `study._storage.set_trial_state(id, TrialState.FAIL)` | 失败 | AttributeError: `'_CachedStorage' object has no attribute 'set_trial_state'` |
| 在 `optuna.storages.RDBStorage` 上找等价方法 | 失败 | 核对 4.9 API 确认无此方法，状态迁移仅封装于 ask/tell 内部 |
| sqlite3 直连 `UPDATE trials SET state='FAIL'` | 有效，采用 | — |

### 最终方案

1. 在 `tansuo/web/app.py` 增加 `_mark_orphaned_running_as_failed(settings, journal)`：

   ```python
   con = sqlite3.connect(str(db_path))
   try:
       rows = con.execute(
           "SELECT trial_id, number FROM trials WHERE state = 'RUNNING'").fetchall()
       if not rows:
           return []
       ids = [r[0] for r in rows]
       placeholders = ",".join("?" * len(ids))
       con.execute(
           f"UPDATE trials SET state = 'FAIL' WHERE trial_id IN ({placeholders})", ids)
       con.commit()
   finally:
       con.close()
   ```

   随后为每个编号往 journal 追加 `trial_fail` 事件（`reason="运行被手动停止"`），保持审计链完整。
2. 两处调用：
   - `run_stop`：杀进程树后立即清理；
   - `run_start` 开头：兜底清理上次遗留（服务器重启、异常退出同样会留下孤儿），**且必须在加载 study 换算新增试验数之前执行**，否则缓存计数偏差。
3. `journal://` 降级存储无 SQLite，函数以 `url.startswith("sqlite:///")` 判断后直接返回空列表跳过。

## R · 实际效果（Result）

- **验证方式**：端到端实测 start → 等试验进行中 → stop → `/api/trials` 显示该试验 state=FAIL；`/api/summary` running=0；journal.jsonl 出现 trial_fail（reason="运行被手动停止"）；stop 后再次 start 正常
- **前后对比**：修复前「进行中」永久显示 1；修复后 stop 即归零，failed 计数 +1，续跑与预算换算自洽
- **副作用与代价**：绕过 Optuna 直改 DB 非官方支持路径；好在 state 列的字符串格式在 4.x 稳定，且只在手动停止场景按 state 精确过滤读写，风险可控
- **遗留问题与后续**：未来 Optuna 大版本若改变 state 列存储格式（如改整数枚举），此 SQL 需同步调整
- **经验教训**：1) Optuna 旧版私有接口 `_storage.set_trial_state` 在 4.x 已移除，写"事后修正试验状态"逻辑前先核对当前版本 API，不要凭旧印象；2) 进程树强杀后的状态清理要做双保险——stop 时清理一次、下次 start 前再兜底一次，因为孤儿来源不止手动停止（还有服务重启、断电）
