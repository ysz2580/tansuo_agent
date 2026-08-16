"""功能2 回归测试：Python 环境管理（adapter.python 解释器替换 + venv 探测）。

可直接 `python tests/test_env_mgmt.py` 运行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def test_resolve_command() -> None:
    print("== resolve_command：解释器替换规则 ==")
    from tansuo.adapter import resolve_command
    from tansuo.config import AdapterCfg

    cfg = AdapterCfg(command=["python", "train.py"], python=sys.executable)
    ok("python 首元素被替换为项目解释器",
       resolve_command(cfg) == [sys.executable, "train.py"])

    cfg = AdapterCfg(command=[r"C:\envs\py\python.exe", "train.py"],
                     python=sys.executable)
    ok("绝对路径 python.exe 首元素同样被替换",
       resolve_command(cfg)[0] == sys.executable)

    cfg = AdapterCfg(command=["torchrun", "--nproc", "2"], python=sys.executable)
    ok("非 python 启动器（torchrun）不被替换",
       resolve_command(cfg) == ["torchrun", "--nproc", "2"])

    cfg = AdapterCfg(command=["python", "train.py"], python="")
    ok("未配置 adapter.python 时逐字保留（历史行为）",
       resolve_command(cfg) == ["python", "train.py"])


def test_settings_python(tmp: Path) -> None:
    print("== settings 校验：adapter.python ==")
    from tansuo.config import ConfigError, load_settings

    def write(text: str) -> Path:
        p = tmp / f"s_{len(list(tmp.glob('s_*.yaml')))}.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    base = ("metrics:\n  primary: {name: acc, direction: maximize}\n"
            "adapter:\n  mode: subprocess\n  command: [python, t.py]\n")
    s = load_settings(write(base + "  python: python3.11\n"))
    ok("裸命令名（交给 PATH）接受", s.adapter.python == "python3.11")

    s = load_settings(write(base + f"  python: {sys.executable}\n"))
    ok("真实存在的解释器路径接受", s.adapter.python == sys.executable)

    try:
        load_settings(write(base + "  python: /no/such/dir/python.exe\n"))
        raise AssertionError("FAIL: 不存在的路径本应被拒")
    except ConfigError as e:
        ok("带路径但不存在的解释器在配置期被拒", "不存在" in str(e), str(e))


def test_venv_detect_and_scaffold(tmp: Path) -> None:
    print("== venv 探测与项目脚手架 ==")
    store = tmp / "projects.json"
    os.environ["TANSUO_PROJECT_STORE"] = str(store)   # 隔离注册表，不碰用户真实状态
    try:
        if "tansuo.web.app" in sys.modules:
            del sys.modules["tansuo.web.app"]
        from tansuo.web.app import _detect_venv_python, _scaffold_project
    finally:
        os.environ.pop("TANSUO_PROJECT_STORE", None)

    proj = tmp / "myproj"
    (proj / "models").mkdir(parents=True)
    ok("无 venv → 探测返回 None", _detect_venv_python(proj) is None)

    if sys.platform == "win32":
        fake_py = proj / ".venv" / "Scripts" / "python.exe"
    else:
        fake_py = proj / ".venv" / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_bytes(b"fake")
    found = _detect_venv_python(proj)
    ok(".venv 探测命中", found is not None and Path(found).is_file(), str(found))

    train = proj / "train.py"
    train.write_text("print('train')\n", encoding="utf-8")
    _scaffold_project(proj, str(train))
    text = (proj / ".tansuo" / "settings.yaml").read_text(encoding="utf-8")
    ok("脚手架写入 adapter.python", "python:" in text, text[:400])
    ok("脚手架 python 值无 Windows 反斜杠（YAML 转义安全）",
       "\\" not in text.split("python:")[1].splitlines()[0],
       text.split("python:")[1].splitlines()[0])
    ok("脚手架 command 指向训练脚本", 'command: ["python", "train.py"]' in text, text[:400])

    from tansuo.config import load_settings
    s = load_settings(proj / ".tansuo" / "settings.yaml")
    ok("脚手架 settings 通过强校验且 python/command 就位",
       s.adapter.python.endswith("python.exe" if sys.platform == "win32" else "python")
       and s.adapter.command == ["python", "train.py"],
       f"python={s.adapter.python} command={s.adapter.command}")


def test_preserve_python_on_rewrite(tmp: Path) -> None:
    print("== setup 覆写保留 adapter.python ==")
    from tansuo.agent.skills.config import SetupExecutor
    from tansuo.journal import Journal
    settings_path = tmp / ".tansuo" / "settings.yaml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        "metrics:\n  primary: {name: acc, direction: maximize}\n"
        "adapter:\n  mode: subprocess\n  command: [python, t.py]\n"
        f"  python: {sys.executable}\n"
        "experiment: {data_dir: .tansuo/data}\n"
        "storage: {url: sqlite:///.tansuo/data/tansuo.db}\n",
        encoding="utf-8")
    ex = SetupExecutor(settings_path, tmp / "space.yaml", tmp / "t.py",
                       Journal(tmp / "j.jsonl"), log=lambda *a, **k: None)
    new_cfg = {
        "metrics": {"primary": {"name": "acc", "direction": "maximize"}},
        "adapter": {"mode": "subprocess", "command": ["python", "t.py"]},
        "experiment": {"name": "x"},
    }
    r = ex._tool_save_settings(new_cfg)
    ok("覆写成功", "已写入" in r, r)
    import yaml
    saved = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    ok("adapter.python 作为部署事实被保留",
       saved["adapter"].get("python") == sys.executable, str(saved.get("adapter")))


if __name__ == "__main__":
    import tempfile
    test_resolve_command()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_settings_python(tmp)
        test_venv_detect_and_scaffold(tmp)
        test_preserve_python_on_rewrite(tmp)
    print(f"\n全部通过：{PASS} 项断言")
