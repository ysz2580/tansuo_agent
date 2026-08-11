---
date: 2026-08-11
number: "011"
title: Optuna journal:// 存储在 Windows 上创建符号链接锁失败（WinError 1314 无特权），测试与降级存储不可用
severity: medium
status: resolved
tags: [optuna, journal存储, windows, 符号链接, 测试]
module: tansuo.study.make_storage / tests
---

# Optuna journal:// 存储在 Windows 上创建符号链接锁失败（WinError 1314 无特权），测试与降级存储不可用

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent，`tansuo/study.py::make_storage`（支持 `sqlite:///` 与
  `journal://` 两种存储）与 tests/（运行时特性测试）
- **环境**：Windows 11（普通用户，无管理员、未开开发者模式），Python 3.14，optuna 4.9.0
- **当时在做什么**：给新增的并行/重试/时间预算写测试。为避免 sqlite 文件句柄残留导致
  `tempfile.TemporaryDirectory` 清理失败（WinError 32），把测试配置的 storage 从
  `sqlite:///` 换成 `journal://`，结果 `optuna.create_study` 直接抛错
- **问题表现**：

  ```
  File "...\optuna\storages\journal\_file.py", line 161, in acquire
      os.symlink(self._lock_target_file, self._lock_file)
  OSError: [WinError 1314] 客户端没有所需的特权。:
  'C:\\Users\\夜月\\AppData\\Local\\Temp\\...\\study.log' ->
  'C:\\Users\\夜月\\AppData\\Local\\Temp\\...\\study.log.lock'
  ```

- **影响范围**：Windows 普通用户权限下 `journal://` 存储完全不可用。生产上 demo 默认
  `sqlite:///` 不受影响；但 settings 校验明确允许 `journal://`（文档也写"降级方案"），
  Windows 用户照着配会在 create_study 时炸。
- **复现步骤**：1) Windows 普通权限环境；2) settings.yaml 配
  `storage: {url: journal://任意路径}`；3) `optuna.create_study(storage=...)` →
  100% 复现 WinError 1314。

## T · 目标（Task）

- **要达成什么**：测试在 Windows 上稳定跑通且不残留文件句柄；明确 journal:// 的平台边界
- **验收标准**：tests/test_runtime_features.py 全绿且临时目录可正常清理；
  journal:// 的 Windows 限制被文档化
- **约束条件**：不能要求用户开开发者模式/管理员权限

## A · 解决方案（Action）

### 排查过程

1. 起初以为是文件被占用，读堆栈发现错误发生在 `JournalFileBackend` 的**加锁**环节：
   Optuna 的 JournalFileBackend 跨进程互斥用"创建符号链接成功与否"当锁原语
   （`_file.py::JournalFileBackendLock.acquire` → `os.symlink`）。
2. Windows 上 `os.symlink` 需要 `SeCreateSymbolicLinkPrivilege`（管理员或开发者模式），
   普通用户进程调用直接 WinError 1314。这不是文件占用，是权限模型问题。
3. 查证：Optuna 官方文档即注明 JournalFileBackend 的锁实现依赖符号链接，
   Windows 上建议用 JournalRedisBackend 或 RDBStorage。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| 测试改用 journal:// 存储避开 sqlite 句柄残留 | 失败 | 触发 WinError 1314（本条记录） |
| 测试改用内存 study（`optuna.create_study()` 不传 storage） | 有效，采用 | 测试只关心 orchestrator/runner 行为，不需要持久存储，内存 study 最干净 |
| 引导 Windows 用户开开发者模式用 journal:// | 放弃 | 对普通用户要求过高；sqlite 本来就是 Windows 首选 |

### 最终方案

1. 测试侧：`tests/test_runtime_features.py` 的 `make_orch` 接受外部传入 study，
   新增 `fresh_study(settings)` 工厂创建**内存** study（同款 DynamicTPESampler +
   MedianPruner，但不落盘），彻底绕开存储文件与句柄问题：

   ```python
   def fresh_study(settings=None) -> optuna.Study:
       """内存 study（与 create_or_load_study 同款采样器/剪枝器，但不落盘）。"""
       if settings is None:
           return optuna.create_study(direction="maximize")
       return optuna.create_study(direction="maximize",
                                  sampler=make_sampler(seed=settings.budget.seed,
                                                       n_startup_trials=2),
                                  pruner=make_pruner(settings.pruner))
   ```

2. 平台边界说明：`journal://` 仅在类 Unix 系统（或 Windows 开发者模式）可用；
   Windows 普通权限请用默认 `sqlite:///`。该限制写进本记录备查（make_storage 保留
   journal:// 分支以服务 Linux 部署与离线降级场景）。

## R · 实际效果（Result）

- **验证方式**：`python tests/test_runtime_features.py` 23 项断言全绿，退出码 0，
  临时目录无清理报错；全套回归（space_patch 34 / conditional 30 / guardrails 21 /
  protocol 12 / runtime 23）共 120 断言全过
- **前后对比**：修复前测试在 create_study 即崩（WinError 1314）；修复后不再触碰
  文件存储
- **副作用与代价**：内存 study 不验证"断点续跑加载既有 study"路径——该路径由
  `test_space_patch` 的快照用例与端到端冒烟（真实 sqlite 续跑）覆盖
- **遗留问题与后续**：若未来 Windows 用户反馈 journal://，引导其用 sqlite 或开开发者模式
- **经验教训**：1) 选"纯 Python、零依赖"的存储方案时要看它的**锁原语**是否跨平台
  （符号链接/文件锁语义在 Windows 上常常变味）；2) 测试要隔离外部状态时，优先问
  "这个测试真的需要持久存储吗"，内存对象往往比换一种文件存储更稳
