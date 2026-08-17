"""ProjectStore 单测：并发读写、bootstrap 幂等与回退、注册表损坏容错。

独立脚本直跑：python tests/test_project_store.py
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tansuo.web.project_store import ProjectStore   # noqa: E402

PASS = 0


def ok(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  [ok] {name}")


def test_store():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp / "repo"                      # 模拟代码安装目录
        (root / "demo" / "configs").mkdir(parents=True)
        (root / "demo" / "configs" / "settings.yaml").write_text("x: 1")
        (root / "demo" / "configs" / "search_space.yaml").write_text("y: 1")
        (root / "demo" / "train_mnist.py").write_text("# train")
        store_file = tmp / "projects.json"
        st = ProjectStore(root, store_path=str(store_file))

        print("== bootstrap：无 env → 注册 demo 并激活 ==")
        st.bootstrap_from_env(None, None)
        projs = st.list_projects()
        ok("注册表含 1 个 demo 项目", len(projs) == 1, str(projs))
        ok("demo 名为内置示例", "内置示例" in projs[0]["name"])
        act = st.get_active()
        ok("激活项为 demo", act is not None and act["id"] == projs[0]["id"])

        print("== bootstrap 幂等：重复调用不重复注册 ==")
        st.bootstrap_from_env(None, None)
        st.bootstrap_from_env(None, None)
        ok("仍只有 1 个项目", len(st.list_projects()) == 1)

        print("== bootstrap：env 指定新 settings → upsert 并激活 ==")
        ext_settings = tmp / "projA" / "settings.yaml"
        ext_settings.parent.mkdir(parents=True)
        ext_settings.write_text("a: 1")
        ext_space = tmp / "projA" / "space.yaml"
        ext_space.write_text("b: 1")
        st.bootstrap_from_env(str(ext_settings), str(ext_space))
        projs = st.list_projects()
        ok("注册表增至 2 个（demo + env）", len(projs) == 2, str(projs))
        act = st.get_active()
        ok("激活项切到 env 项目", act is not None
           and Path(act["settings_path"]) == ext_settings.resolve(), str(act))

        print("== bootstrap 幂等：同一 env settings 不重复 ==")
        st.bootstrap_from_env(str(ext_settings), str(ext_space))
        ok("仍 2 个项目", len(st.list_projects()) == 2)

        print("== register / activate / remove ==")
        entry = st.register("myproj", tmp / "projB", tmp / "projB" / "settings.yaml",
                            tmp / "projB" / "space.yaml", train_script="")
        ok("register 返回含 id 的条目", bool(entry.get("id")))
        st.activate(projs[0]["id"])
        ok("可激活既有项目", st.get_active()["id"] == projs[0]["id"])
        st.remove(entry["id"])
        ok("remove 后注册表回到 2", len(st.list_projects()) == 2)

        print("== update：补登记 train_script + 白名单外字段忽略 ==")
        train = tmp / "projB" / "train.py"
        train.parent.mkdir(parents=True, exist_ok=True)
        train.write_text("# train")
        entry = st.register("updproj", tmp / "projB",
                            tmp / "projB" / "settings.yaml",
                            tmp / "projB" / "space.yaml", train_script="")
        upd = st.update(entry["id"], train_script=str(train), bogus="x")
        ok("补登记 train_script 写入且解析为绝对路径",
           upd["train_script"] == str(train.resolve()), str(upd))
        ok("白名单外字段被忽略", "bogus" not in upd)
        try:
            st.update("nonexistent", train_script="a.py")
            raise AssertionError("FAIL: 未知项目 update 应 KeyError")
        except KeyError:
            ok("未知项目 update 抛 KeyError", True)
        ok("update 后注册表条目同步可读",
           next(p for p in st.list_projects()
                if p["id"] == entry["id"])["train_script"] == str(train.resolve()))
        st.remove(entry["id"])

        print("== 并发：20 线程同时 register + activate 不损坏 ==")
        errors = []

        def worker(i):
            try:
                e = st.register(f"c{i}", tmp / f"c{i}", tmp / f"c{i}" / "s.yaml", "")
                st.activate(e["id"])
            except Exception as ex:   # noqa: BLE001
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok("并发无异常", not errors, str(errors))
        doc = json.loads(store_file.read_text(encoding="utf-8"))
        ok("并发后 JSON 仍可解析且 projects 为列表",
           isinstance(doc.get("projects"), list))
        ok("并发后激活项存在且唯一",
           doc.get("active_id") in {p["id"] for p in doc["projects"]})

        print("== 损坏容错：JSON 坏掉 → 空骨架不炸 ==")
        store_file.write_text("{not valid json", encoding="utf-8")
        st2 = ProjectStore(root, store_path=str(store_file))
        ok("损坏注册表 list 为空", st2.list_projects() == [])
        ok("损坏注册表 get_active 为 None", st2.get_active() is None)
        st2.bootstrap_from_env(None, None)
        ok("损坏后 bootstrap 自愈注册 demo", len(st2.list_projects()) == 1)


if __name__ == "__main__":
    test_store()
    print(f"\ntest_project_store 通过，共 {PASS} 项断言")
