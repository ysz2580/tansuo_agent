"""提示词持久化：prompts.yaml 覆盖、版本递增与历史快照（支持回滚）。

prompts.yaml 与 settings.yaml 同目录（settings.source_path.with_name）。
文件缺失 = 全用出厂默认（load_overrides 返回 {}），存量行为不变。

文件格式：
    version: 1                 # 单调递增；每次保存 +1
    prompts:
      tuning_system: ""        # 空串 = 用出厂默认
      tuning_wake_brief: ""
      setup_system: ""
    history:
      - ts: "..."
        version: 1
        which: tuning_system
        rationale: "..."
        source: web
        text: "该版本生效的完整文本"   # 载入此版本=用 text 覆盖当前
        hash: "sha256 前 12 位"

提示词是 agent 全局行为、不分分区；审计轨放本文件 history，不写分区 journal。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import yaml

from .prompts import PROMPT_NAMES

MAX_PROMPT_LEN = 20000


class PromptStoreError(Exception):
    """提示词存储/校验错误（Web 层转 400）。"""


def resolve_prompts_path(settings) -> Path:
    """prompts.yaml 与 settings 同目录；程序化 Settings（无 source_path）抛错。"""
    src = str(getattr(settings, "source_path", "") or "")
    if not src:
        raise PromptStoreError(
            "settings 未携带配置路径（source_path），无法定位 prompts.yaml；"
            "请经 load_settings 加载配置后再读写提示词")
    return Path(src).with_name("prompts.yaml")


def _load_doc(path: Path) -> dict:
    """读整份文档；缺失/损坏返回 {}（视为全默认）。"""
    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return doc if isinstance(doc, dict) else {}
    except yaml.YAMLError:
        return {}


def load_doc(settings) -> dict:
    """返回 {version, prompts, history}（文件缺失时 version=0、prompts={}、history=[]）。"""
    try:
        path = resolve_prompts_path(settings)
    except PromptStoreError:
        return {"version": 0, "prompts": {}, "history": []}
    doc = _load_doc(path)
    return {"version": int(doc.get("version", 0) or 0),
            "prompts": doc.get("prompts") or {},
            "history": doc.get("history") or []}


def load_overrides(settings) -> dict:
    """name → 覆盖文本（空串视为未覆盖）。供 Skill 运行时读取。"""
    return {k: str(v) for k, v in load_doc(settings)["prompts"].items()
            if k in PROMPT_NAMES}


def _atomic_write(path: Path, doc: dict) -> None:
    """temp + os.replace 原子写，避免半截文件。"""
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prompts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def save_override(settings, which: str, text: str, rationale: str,
                  source: str = "web") -> dict:
    """保存一条覆盖并记历史（text 为空串 = 恢复出厂默认）。

    返回 {"version": N, "entry": {...}}；entry.text 是该版本生效的文本（供回滚载入）。
    """
    if which not in PROMPT_NAMES:
        raise PromptStoreError(f"未知提示词 {which!r}，可选：{'、'.join(PROMPT_NAMES)}")
    if not (rationale or "").strip():
        raise PromptStoreError("rationale 必填：请说明本次改动的理由（与空间编辑同级要求）")
    text = "" if text is None else str(text)
    if len(text) > MAX_PROMPT_LEN:
        raise PromptStoreError(f"提示词过长：{len(text)} 字符（上限 {MAX_PROMPT_LEN}）")

    path = resolve_prompts_path(settings)
    doc = _load_doc(path)
    new_version = int(doc.get("version", 0) or 0) + 1
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
             "version": new_version,
             "which": which,
             "rationale": rationale.strip(),
             "source": source,
             "text": text,
             "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}
    prompts = dict(doc.get("prompts") or {})
    prompts[which] = text
    _atomic_write(path, {"version": new_version,
                         "prompts": prompts,
                         "history": list(doc.get("history") or []) + [entry]})
    return {"version": new_version, "entry": entry}
