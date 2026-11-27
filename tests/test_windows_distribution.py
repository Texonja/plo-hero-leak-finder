import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
IS_PACKAGE = RUNTIME.exists() or (ROOT / "PLO Hero Leak Finder.exe").exists()


class WindowsDistributionTests(unittest.TestCase):
    @unittest.skipUnless(IS_PACKAGE, "requires the Windows release package")
    def test_runtime_is_included_next_to_the_launcher(self):
        for relative in ("python.exe", "pythonw.exe", "DLLs/_tkinter.pyd", "LICENSE.txt"):
            with self.subTest(relative=relative):
                self.assertTrue((RUNTIME / relative).is_file(), relative)
        self.assertTrue(any(RUNTIME.glob("python3*.zip")))
        self.assertTrue(any((RUNTIME / "tcl").glob("libtk*.zip")))

    @unittest.skipUnless(os.name == "nt", "requires Windows")
    @unittest.skipUnless(IS_PACKAGE, "requires the Windows release package")
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

    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_source_launcher_directs_players_without_python_to_the_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "START.BAT").write_bytes((ROOT / "START.BAT").read_bytes())
            (source / "launch.pyw").touch()
            (source / "leakfinder").mkdir()
            (source / "leakfinder/ui.py").touch()
            env = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
            result = subprocess.run([env["COMSPEC"], "/d", "/c", "START.BAT"],
                                    cwd=source, env=env, input="\n", text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("source code", result.stdout)
            self.assertIn("https://github.com/Texonja/plo-hero-leak-finder/releases/latest", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
