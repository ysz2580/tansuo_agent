---
date: 2026-08-13
number: "014"
title: torch.__version__ 是 str 子类，写入 meta.yaml 时 PyYAML safe_dump 拒绝序列化
severity: medium
status: resolved
tags: [pyyaml, torch, 序列化, 环境审计]
module: tansuo/cohort.py（分区 meta.yaml 写入）
---

# torch.__version__ 是 str 子类，写入 meta.yaml 时 PyYAML safe_dump 拒绝序列化

## S · 背景（Situation）

- **项目 / 模块**：tansuo_agent 分区管理模块 `tansuo/cohort.py`，分区 meta.yaml
  的写入函数 `_write_meta`（`yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)`）
- **环境**：Windows 11；Python 3.14.6；PyYAML 6.0.3；torch 2.13.0+cpu
  （`type(torch.__version__)` 是 `torch.torch_version.TorchVersion`）
- **当时在做什么**：开发「环境审计」功能——分区创建时把 python / optuna / torch
  版本、GPU、主机名写进 meta.yaml 的 `environment` 字段。首次运行
  `python tests/test_cohort.py` 验证
- **问题表现**：测试在 create_cohort 写 meta.yaml 时直接抛异常：

  ```
  File "...\yaml\representer.py", line 48, in represent_data
      node = self.yaml_representers[data_types[0]](self, data)
  File "...\yaml\representer.py", line 207, in represent_dict
      return self.represent_mapping('tag:yaml.org,2002:map', data)
  ...
  File "...\yaml\representer.py", line 231, in represent_undefined
      raise RepresenterError("cannot represent an object", data)
  yaml.representer.RepresenterError: ('cannot represent an object', '2.13.0+cpu')
  ```

  `'2.13.0+cpu'` 看起来就是个普通字符串，报错信息完全没有提示它"不是 str"。
- **影响范围**：阻塞环境审计功能——只要机器装了 torch，任何新建分区都会崩
  （create_cohort → _write_meta → safe_dump 路径）。未装 torch 的环境不受影响，
  属于"换台机器就炸"的隐性缺陷
- **复现步骤**：1) 装有 torch 的环境；2) 执行
  `python -c "import yaml, torch; yaml.safe_dump({'v': torch.__version__})"`；
  3) 100% 复现 RepresenterError

## T · 目标（Task）

- **要达成什么**：环境审计信息可靠地序列化进 meta.yaml，无论各依赖库返回的版本
  对象是什么类型
- **验收标准**：test_cohort.py 环境审计组全部通过；有/无 torch 的环境都不再抛
  RepresenterError
- **约束条件**：环境审计的采集原则是"尽力而为、绝不阻塞分区创建"，修复方式不能
  引入新的脆弱点（比如按依赖类型逐个打补丁）

## A · 解决方案（Action）

### 排查过程

1. 报错对象 `'2.13.0+cpu'` 打印出来是字符串，第一反应是"yaml 为什么拒绝字符串"。
   用 `type(torch.__version__)` 检查 → `<class 'torch.torch_version.TorchVersion'>`：
   它是 **str 的子类**（torch 把版本号封装成带语义比较能力的类），不是纯 str。
2. 读 PyYAML 6.0.3 `BaseRepresenter.represent_data` 源码确认机制：
   - 先用 `type(data).__mro__[0]`（精确类型）查 `yaml_representers`——
     `TorchVersion` 不在其中（注册的是 `str`）；
   - **回退分支只遍历 `yaml_multi_representers`**（SafeDumper 没有为 str 注册
     multi representer），并不会按 MRO 回退查 `yaml_representers`；
   - 于是落到 `yaml_representers[None]` = `represent_undefined` → 抛
     RepresenterError。
   即：**PyYAML 对内置类型注册表只做精确匹配，任何 str/int/dict/list 的子类
   都会被 safe_dump 拒绝**——不止 torch，任何第三方库的版本号对象都可能踩中。

### 尝试过的方案（含失败的）

| 方案 | 结果 | 失败/放弃原因 |
|------|------|--------------|
| `yaml.safe_dump(..., default_flow_style=...)` 等参数调整 | 未采用 | 与问题无关，RepresenterError 发生在类型分发阶段，任何 dump 参数都绕不开 |
| 给 TorchVersion 注册自定义 representer（`yaml.add_representer`） | 放弃 | 每冒出一个子类就要注册一次，且污染全局 yaml 状态；治标不治本 |
| 采集时对版本号强制 `str()` 归一 | 有效，采用 | 一次性覆盖所有来源，符合"审计字段必须是纯原生类型"的设计 |

### 最终方案

在 `tansuo/cohort.py` 的 `collect_env_audit()` 里，所有来自第三方库的值统一强制
为原生类型（文件：`tansuo/cohort.py`）：

```python
audit["optuna"] = str(optuna.__version__)   # 强制纯 str（yaml 拒绝 str 子类）
audit["torch"] = str(torch.__version__)
audit["gpus"] = [str(torch.cuda.get_device_name(i))
                 for i in range(torch.cuda.device_count())]
```

GPU 设备名同样走 `str()`（`get_device_name` 当前返回纯 str，但按同一原则防御）。

## R · 实际效果（Result）

- **验证方式**：`python tests/test_cohort.py` 环境审计组 9 项断言全绿（套件共
  110 项）；`python -c "import yaml, torch; yaml.safe_dump({'v': str(torch.__version__)})"`
  正常输出 `'v: 2.13.0+cpu\n'`；随后 CLI 冒烟 27 项、Web 冒烟 35 项通过
- **前后对比**：装 torch 的机器上新建分区从 100% 崩溃恢复为正常，meta.yaml 里
  得到纯字符串版本号
- **副作用与代价**：无。`str()` 对本来就是 str 的值是零成本恒等变换
- **遗留问题与后续**：无
- **经验教训**：1) 把第三方库的属性写进序列化格式（YAML/JSON）之前，一律强制
  转换为原生类型——"打印出来像 str"不等于"type 是 str"；2) PyYAML 的类型分发
  对内置类型**不做 MRO 回退**（只查 multi_representers），这是它和
  `json.dumps`（同样拒绝但报错更直白）都容易踩的点；3) 报错对象显示成
  `'2.13.0+cpu'` 这样的纯值时，先 `type()` 再怀疑别的——报错信息里的 repr
  会掩盖子类型身份
