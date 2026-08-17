---
date: 2026-08-17
number: "026"
title: 前端并发轮询撞 sqlite 建表竞态：`table studies already exists`，且真实异常是 SQLAlchemy 包装类型，旧捕获全部漏网变 ASGI 500
severity: high
status: resolved
tags: [sqlite, optuna, 并发, 建表竞态, sqlalchemy, 异常捕获, web后端]
module: tansuo/study.py · tansuo/web/app.py
---

# 前端并发轮询撞 sqlite 建表竞态：`table studies already exists`，且真实异常是 SQLAlchemy 包装类型，旧捕获全部漏网变 ASGI 500

## S · 背景（Situation）

- **项目 / 模块**：tansuo Web 后端（FastAPI/uvicorn）+ `tansuo/study.py` storage 工厂。
- **环境**：Windows 11、Python 3.14、optuna 4.9.0、SQLAlchemy 2.0.51、uvicorn；后端 `http://127.0.0.1:8000` + 前端 Vite dev（`/api` 代理到 :8000）。
- **当时在做什么**：用户启动后端 + 前端开发模式正常使用，后端日志抛出未处理异常。
- **问题表现**：uvicorn 打印 `Exception in ASGI application`，根因链为：

  ```
  sqlite3.OperationalError: table studies already exists

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File ".../sqlalchemy/engine/base.py", line 1969, in _exec_single_context
      self.dialect.do_execute(
          cursor, str_statement, effective_parameters, context
      )
    ...
    File ".../uvicorn/protocols/http/h11_impl.py", line 416, in run_asgi
    File ".../fastapi/applications.py", line 1163, in __call__
    File ".../starlette/middleware/errors.py", line 186, in __call__
      raise exc
  ```

- **影响范围**：任何「多个请求并发打开同一个尚未建表的 sqlite」的场景都会 500：前端并发轮询 `/api/summary`、`/api/trials` 等只读端点（sync 端点跑在 FastAPI 线程池）；`/api/run/start` 的孤儿清理与 CLI 子进程同时打开新分区 db。表现为页面请求随机失败、后端日志刷 ASGI 堆栈。
- **复现步骤**：1) 准备一个不存在的 sqlite 路径；2) 12 个线程同时 `make_storage(url)`；3) 稳定复现 11/12 线程失败（本地实测）。

## T · 目标（Task）

- **要达成什么**：并发/跨进程打开同一 sqlite 不再因建表竞态报错；即使 db 真忙，Web 层也返回 503 降级而非 500 ASGI 崩溃。
- **验收标准**：12 线程竞态复现 0 失败；新增回归测试全过；18 个单测套件 + CLI/Web 两个端到端冒烟零回归。
- **约束条件**：不改 Optuna 版本；不引入新依赖；CLI 子进程路径同样受益（不能只修 Web 层）。

## A · 解决方案（Action）

### 排查过程

1. **定位建表点**：全仓 grep `RDBStorage|create_study`，所有生产路径都经 `tansuo/study.py::make_storage`。查 optuna 4.9.0 源码，`RDBStorage.__init__` 无条件执行：

   ```python
   if not skip_table_creation:
       models.BaseModel.metadata.create_all(self.engine)
   ```

   `create_all` 是 checkfirst（先 inspect 再 CREATE），但**inspect 与 CREATE 之间无锁**：两个线程/进程对同一新 db 同时 inspect 都判定"表不存在"→ 双双 `CREATE TABLE studies` → 后到者报错。
2. **确认并发来源**：前端同时轮询多个端点，FastAPI 把 sync 端点调度到线程池——**单进程内就有真并发**；另外 `/api/run/start` 的孤儿清理（`_orphan_cleanup_for`）与随后拉起的 CLI 子进程会跨进程打开同一新分区 db。
3. **写复现脚本**：12 线程并发 `make_storage` 同一新 db——**11 个线程失败**，与用户现场一致。
4. **第二层 bug 浮现**：复现输出的异常类型是 `sqlalchemy.exc.OperationalError`（SQLAlchemy 把 DBAPI 的 `sqlite3.OperationalError` 包装后 `raise ... from e`），而代码里三处"数据库忙"捕获写的都是裸 `sqlite3.OperationalError` / `sqlite3.Error`——`isinstance` 不命中，全部漏网，于是不降级为 503 而是未处理异常直冲 ASGI。这解释了为什么用户看到的是 500 堆栈而不是友好提示。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 给 `RDBStorage` 传 `skip_table_creation=True`（4.9 新增参数） | 放弃 | 建表总得有人做：还得另写"首个打开者负责建表"的协调逻辑，复杂度不低于重试，且只覆盖 RDB 一处 |
| 只在 Web 端点捕获异常降级 | 放弃 | CLI 子进程（run/custom/graduate）同样会撞竞态，只修 Web 层是半个修复 |
| 捕获 `already exists` 后忽略并继续用 | 放弃 | 异常抛在 `__init__` 里，storage 对象没构造出来，无从"继续用"；必须重建 |
| `make_storage` 按路径串行 + 竞态重试 | 采用 | 进程内锁消掉线程级竞态；跨进程竞态靠重试容忍（赢家提交后 checkfirst 能看到表，重试必然成功）；所有调用方自动受益 |

### 最终方案

1. **`tansuo/study.py`**——`make_storage` 的 sqlite 分支改走 `_make_rdb_storage(db_path)`：

   ```python
   _SCHEMA_LOCKS_GUARD = threading.Lock()
   _SCHEMA_LOCKS: dict[str, threading.Lock] = {}

   def _make_rdb_storage(db_path: Path) -> optuna.storages.RDBStorage:
       key = str(db_path.resolve())
       with _SCHEMA_LOCKS_GUARD:
           lock = _SCHEMA_LOCKS.setdefault(key, threading.Lock())
       attempts = 3
       for i in range(attempts):
           try:
               with lock:
                   return optuna.storages.RDBStorage(
                       "sqlite:///" + db_path.resolve().as_posix())
           except Exception as e:
               if _is_schema_race_error(e) and i < attempts - 1:
                   logger.warning("sqlite 建表竞态（%s），第 %d 次重试：%s", ...)
                   time.sleep(0.05 * (i + 1))
                   continue
               raise
   ```

   - 按**解析后的 db 绝对路径**加进程内锁，串行化本进程的建表；
   - 仅当异常属于 `DB_BUSY_ERRORS` 且消息含 `"already exists"` 才重试（`database is locked` 等其他忙错误**不重试**，原样上抛交上层 503 语义）；最多 3 次、退避 `0.05*(i+1)` 秒。
   - 同文件导出异常类型元组：

   ```python
   DB_BUSY_ERRORS: tuple[type, ...] = (sqlite3.OperationalError,
                                       sqlalchemy.exc.OperationalError)
   ```

2. **`tansuo/web/app.py`**——四处捕获拓宽为 `DB_BUSY_ERRORS`：
   - `_safe_load`（所有只读端点的公共入口）：`except sqlite3.OperationalError` → `except DB_BUSY_ERRORS` → 503；
   - `/api/run/start` 的完结数换算：同样拓宽 → 503；
   - `_orphan_cleanup_for`：`except (ConfigError, CohortError, sqlite3.Error)` 追加 `*DB_BUSY_ERRORS`（孤儿清理失败静默跳过，不阻塞启动）；
   - `/api/runs/compare`：新增 `DB_BUSY_ERRORS` → 503（此前分区 db 被写入时对比会裸 500）。

3. **回归测试** `tests/test_schema_race.py`（13 断言）：12 线程竞态全成、db 竞态后完好可用、首次 `already exists` → 重试恰好 2 次成功、竞态连发 → 恰好 3 次后原样抛出、`locked` 不重试只试 1 次、`DB_BUSY_ERRORS` 类型覆盖、`_safe_load` 把 SQLAlchemy 包装的 db 忙映射为 503。

## R · 实际效果（Result）

- **验证方式**：
  - 复现脚本（12 线程并发建 storage）：修复前 **11/12 失败**，修复后 **0 失败**（连跑 5 轮稳定）；
  - `python tests/test_schema_race.py` → 全部通过：13 项断言；
  - 全量回归 18 个单测套件 → **失败套件数：0 / 18**；
  - `tests/e2e_cli_smoke.py` → CLI 冒烟全部通过：45 项；
  - `tests/e2e_web_smoke.py` → Web 冒烟全部通过：共 120 项断言。
- **前后对比**：并发打开新 db 从「必有一方抛 `table studies already exists` 且变 500」→「进程内零竞态、跨进程重试自愈」；真遇 db 忙时前端收到 503 + 中文提示（"数据库正被运行中的任务写入，稍后再试"）而非 ASGI 堆栈。
- **副作用与代价**：sqlite 分支建 storage 多一次按路径取锁（微秒级）；竞态命中时最多多等 0.15 秒——相对此前直接 500 是净收益。`_SCHEMA_LOCKS` 字典随访问过的 db 路径增长（进程生命周期内分区数有限，不处理）。
- **遗留问题与后续**：无。
- **经验教训**：
  1. **异常捕获要对着"实际抛出的类型"写，不是对着"以为会抛的类型"写**。SQLAlchemy 会把 DBAPI 异常包装成自己的类型再 `raise ... from e`——traceback 头一行是 `sqlite3.OperationalError` 极具误导性，真正传播的是 `sqlalchemy.exc.OperationalError`。写完降级捕获后应当构造一次真实异常链验证 `isinstance` 命中。
  2. `CREATE TABLE`（哪怕 checkfirst）在多线程/多进程共享存储的场景默认不安全；任何"每个请求各自初始化 storage"的架构都要在建表这一步加协调（锁/重试/预建三选一）。
  3. sync 端点 + 前端轮询 = 单进程内真并发，别拿"只有一个后端进程"当串行假设。
