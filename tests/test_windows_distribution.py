import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


class WindowsDistributionTests(unittest.TestCase):
    def test_runtime_is_included_next_to_the_launcher(self):
        for relative in ("python.exe", "pythonw.exe", "DLLs/_tkinter.pyd", "LICENSE.txt"):
            with self.subTest(relative=relative):
                self.assertTrue((RUNTIME / relative).is_file(), relative)
        self.assertTrue(any(RUNTIME.glob("python3*.zip")))
        self.assertTrue(any((RUNTIME / "tcl").glob("libtk*.zip")))

    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_embedded_python_can_open_tk_without_a_system_python(self):
        script = """import sys, sqlite3, tkinter
from datetime import UTC
from pathlib import Path
from leakfinder.ui import main
base = Path(sys.executable).resolve().parent.parent
assert all(Path(p).resolve().is_relative_to(base) for p in sys.path)
root = tkinter.Tk()
root.withdraw()
root.destroy()
"""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
                       PYTHONHOME=tmp, PYTHONPATH=tmp, LOCALAPPDATA=tmp)
            subprocess.run([str(RUNTIME / "python.exe"), "-B", "-c", script],
                           cwd=tmp, env=env, check=True, timeout=30)
