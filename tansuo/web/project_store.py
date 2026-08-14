"""ProjectStore：项目注册表 + 激活项。

项目 = 一个目录（含用户训练代码与数据集）。tansuo 的探索工作产物放在该目录的
`.tansuo/` 子目录（settings.yaml / search_space.yaml / data/runs）。注册表记录每个
项目的 settings/space 绝对路径、训练脚本与激活状态，存于
``~/.tansuo_agent/projects.json``（可用环境变量 ``TANSUO_PROJECT_STORE`` 改位置，
供测试隔离）。

设计要点：
- 进程内 ``threading.Lock`` 保护「读-改-写」；写回 ``tempfile + os.replace`` 原子替换；
- JSON 不存在/损坏 → 返回空骨架，``get_active()`` 回退 None（调用方再退回环境变量），
  绝不因注册表问题炸掉 Web 后端；
- ``bootstrap_from_env`` 幂等：确保内置 demo 在列，并把环境变量指定的 settings/space
  upsert + 激活——这样现有 ``cli.py web --settings X`` 用户零破坏（见 STAR #010）。
- 不支持 uvicorn 多 worker（RUN 单例本就不支持）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

_DEFAULT_STORE = Path.home() / ".tansuo_agent" / "projects.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _make_entry(name: str, project_dir, settings_path, space_path,
                train_script: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "dir": str(Path(project_dir).resolve()),
        "settings_path": str(Path(settings_path).resolve()),
        "space_path": str(Path(space_path).resolve()) if space_path else "",
        "train_script": str(Path(train_script).resolve()) if train_script else "",
        "created_at": _now(),
        "last_used": _now(),
    }


class ProjectStore:
    def __init__(self, project_root: Path, store_path: str | None = None):
        self.project_root = Path(project_root)      # 代码安装目录（定位 cli.py/demo）
        self.store_path = Path(store_path) if store_path else _DEFAULT_STORE
        self._lock = threading.Lock()

    # -- 底层读写 -------------------------------------------------
    def _read_doc(self) -> dict:
        try:
            doc = json.loads(self.store_path.read_text(encoding="utf-8"))
            if not isinstance(doc.get("projects"), list):
                return {"projects": [], "active_id": None}
            return doc
        except (OSError, json.JSONDecodeError, ValueError):
            return {"projects": [], "active_id": None}

    def _write_doc(self, doc: dict) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.store_path.parent), suffix=".tmp")
        os.close(fd)
        Path(tmp).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, str(self.store_path))

    # -- 查询 -----------------------------------------------------
    def list_projects(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._read_doc()["projects"]]

    def get_active(self) -> dict | None:
        """激活项目；无激活项但有项目 → 取第一个；空 → None（调用方回退环境变量）。"""
        with self._lock:
            doc = self._read_doc()
            for p in doc["projects"]:
                if p.get("id") == doc.get("active_id"):
                    return dict(p)
            return dict(doc["projects"][0]) if doc["projects"] else None

    # -- 写操作 ---------------------------------------------------
    def register(self, name: str, dir, settings_path, space_path,
                 train_script: str = "", activate: bool = True) -> dict:
        with self._lock:
            doc = self._read_doc()
            entry = _make_entry(name, dir, settings_path, space_path, train_script)
            doc["projects"].append(entry)
            if activate or not doc.get("active_id"):
                doc["active_id"] = entry["id"]
            self._write_doc(doc)
            return dict(entry)

    def activate(self, project_id: str) -> dict:
        with self._lock:
            doc = self._read_doc()
            target = next((p for p in doc["projects"] if p.get("id") == project_id), None)
            if target is None:
                raise KeyError(f"项目不存在：{project_id}")
            doc["active_id"] = project_id
            target["last_used"] = _now()
            self._write_doc(doc)
            return dict(target)

    def remove(self, project_id: str) -> None:
        with self._lock:
            doc = self._read_doc()
            doc["projects"] = [p for p in doc["projects"] if p.get("id") != project_id]
            if doc.get("active_id") == project_id:
                doc["active_id"] = doc["projects"][0]["id"] if doc["projects"] else None
            self._write_doc(doc)

    def bootstrap_from_env(self, env_settings: str | None, env_space: str | None) -> None:
        """首次/每次启动确保注册表可用（幂等）：
        - 内置 demo 项目始终在列；
        - 环境变量指定的 settings/space（``cli.py web --settings``）upsert + 激活，
          使 Web 服务指向用户显式给的配置（STAR #010 语义不变）。
        """
        demo_settings = self.project_root / "demo" / "configs" / "settings.yaml"
        demo_space = self.project_root / "demo" / "configs" / "search_space.yaml"
        demo_train = self.project_root / "demo" / "train_mnist.py"
        with self._lock:
            doc = self._read_doc()
            # 1) 确保 demo 在列（按 settings_path 去重）
            if not any(Path(p["settings_path"]).resolve() == demo_settings.resolve()
                       for p in doc["projects"] if p.get("settings_path")):
                doc["projects"].append(_make_entry(
                    "demo（内置示例）", self.project_root,
                    demo_settings, demo_space, str(demo_train)))
            # 2) env 指定且非 demo → upsert 并激活；否则激活 demo
            target_settings = (Path(env_settings).resolve()
                               if env_settings else demo_settings.resolve())
            existing = next(
                (p for p in doc["projects"]
                 if p.get("settings_path")
                 and Path(p["settings_path"]).resolve() == target_settings), None)
            if existing is None:
                existing = _make_entry(
                    f"env：{Path(env_settings).parent.name if env_settings else 'demo'}",
                    self.project_root, target_settings, env_space or "", "")
                doc["projects"].append(existing)
            doc["active_id"] = existing["id"]
            existing["last_used"] = _now()
            self._write_doc(doc)
