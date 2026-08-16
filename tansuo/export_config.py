"""配置回写：把调参得到的最优超参数合并进用户自己的配置文件。

调参的终点不是 best.yaml（那是 tansuo 的工作产物），而是用户训练脚本真正
读取的那份配置。本模块完成"最后一公里"：

- 支持 YAML（.yaml/.yml）与 JSON（.json）目标文件；
- 合并语义：顶层同名键覆盖为用户配置原格式之外的新值、异名键追加；
  不做深度递归合并（嵌套结构整体替换，语义简单可预期）；
- **覆盖前必备份** `<目标>.bak`（写入前的原始内容），误操作可一键回滚；
- preview 模式只产出合并后文本与变更清单，绝不落盘（前端先给用户过目）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml


class ExportError(ValueError):
    """回写前置条件不满足（目标不存在/格式不支持/无 best 等），信息面向用户。"""


SUPPORTED_SUFFIXES = {".yaml": "yaml", ".yml": "yaml", ".json": "json"}


def best_params_live(study) -> tuple[int, float, dict]:
    """从 study 实时取 best（不落盘、不读报告缓存）。无完结试验抛 ExportError。"""
    try:
        best = study.best_trial
    except (ValueError, KeyError):
        raise ExportError("当前分区没有完成的试验，无最优配置可回写")
    return best.number, best.value, dict(best.params)


def _load_target(path: Path) -> tuple[dict, str]:
    """读目标配置：返回 (顶层 dict, 格式)。顶层非映射 → ExportError。"""
    fmt = SUPPORTED_SUFFIXES.get(path.suffix.lower())
    if fmt is None:
        raise ExportError(f"目标文件格式不支持：{path.suffix}（支持 .yaml/.yml/.json）")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ExportError(f"无法读取目标文件 {path}：{e}")
    try:
        data = yaml.safe_load(text) if fmt == "yaml" else json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise ExportError(f"目标文件 {path} 解析失败：{e}")
    if not isinstance(data, dict):
        raise ExportError(f"目标文件 {path} 顶层必须是映射/对象（实际："
                          f"{type(data).__name__}），无法按顶层键合并")
    return data, fmt


def _serialize(data: dict, fmt: str) -> str:
    if fmt == "yaml":
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def preview(target_path: str | Path, study) -> dict:
    """预演合并：返回变更清单与合并后全文，不写任何文件。"""
    target = Path(target_path)
    if not target.exists():
        raise ExportError(f"目标文件不存在：{target}（回写只改既有文件，不凭空创建）")
    trial_no, value, params = best_params_live(study)
    data, fmt = _load_target(target)
    changed: list[dict] = []
    appended: list[dict] = []
    merged = dict(data)
    for k, v in params.items():
        if k in data:
            if data[k] != v:
                changed.append({"key": k, "old": data[k], "new": v})
            merged[k] = v
        else:
            appended.append({"key": k, "new": v})
            merged[k] = v
    return {"target": str(target), "format": fmt,
            "best_trial": trial_no, "best_value": value, "params": params,
            "changed": changed, "appended": appended,
            "merged_text": _serialize(merged, fmt)}


def export(target_path: str | Path, study) -> dict:
    """正式回写：.bak 备份 → 写入合并结果。返回结果含备份路径。"""
    result = preview(target_path, study)
    target = Path(target_path)
    backup = target.with_suffix(target.suffix + ".bak")
    try:
        shutil.copy2(target, backup)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(result["merged_text"])
    except OSError as e:
        raise ExportError(f"写入失败（目标未被修改）：{e}")
    n = len(result["changed"]) + len(result["appended"])
    return {**result, "backup": str(backup), "applied": True,
            "summary": f"已回写 {n} 项（覆盖 {len(result['changed'])}、"
                       f"新增 {len(result['appended'])}），原文件备份至 {backup.name}"}
