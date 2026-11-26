"""Stdlib test runner: pytest is not installed in Hermes's venv. Test functions are pytest-style, so pytest works unchanged.

    <hermes venv python> tests/run.py
"""
import importlib.util
import inspect
import os
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
HERMES = os.environ.get("HERMES_AGENT_SRC", str(pathlib.Path.home() / "AppData/Local/hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hdl-home-", dir=os.environ.get("TMPDIR"))
for p in (HERMES, str(ROOT / "tests")):
    sys.path.insert(0, p)

# The repo dir name is not importable, so load it as the package "hdl".
spec = importlib.util.spec_from_file_location("hdl", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
hdl = importlib.util.module_from_spec(spec)
sys.modules["hdl"] = hdl
spec.loader.exec_module(hdl)


class MP:
    def __init__(self):
        self.undo = []

    def setattr(self, obj, name, val):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def setenv(self, k, v):
        self.undo.append((os.environ, k, os.environ.get(k)))
        os.environ[k] = v

    def setitem(self, d, k, v):
        self.undo.append((d, k, d.get(k)))
        d[k] = v

    def close(self):
        for o, n, v in reversed(self.undo):
            if isinstance(o, dict) or o is os.environ:
                o.pop(n, None) if v is None else o.__setitem__(n, v)
            else:
                setattr(o, n, v)


fails = passed = 0
for path in sorted((ROOT / "tests").glob("test_*.py")):
    mod = importlib.import_module(path.stem)
    for name, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        mp, kw = MP(), {}
        params = inspect.signature(fn).parameters
        if "monkeypatch" in params:
            kw["monkeypatch"] = mp
        if "tmp_path" in params:
            kw["tmp_path"] = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        try:
            fn(**kw)
            passed += 1
            print("PASS", path.stem, name)
        except Exception:
            fails += 1
            print("FAIL", path.stem, name)
            traceback.print_exc()
        finally:
            mp.close()
print("FAILED" if fails else "ALL PASSED", f"passed={passed} failed={fails}")
sys.exit(1 if fails else 0)
