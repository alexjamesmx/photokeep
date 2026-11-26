import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_private.py"


def run_check(cwd, tracked):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--files", *tracked],
        cwd=cwd, capture_output=True, text=True,
    )


def test_check_private_flags_pattern(tmp_path):
    (tmp_path / ".private-patterns").write_text("secret-name\n")
    (tmp_path / "a.md").write_text("hello secret-name")
    r = run_check(tmp_path, tracked=["a.md"])
    assert r.returncode == 1 and "a.md:1" in r.stdout


def test_check_private_passes_without_patterns_file(tmp_path):
    (tmp_path / "a.md").write_text("anything")
    assert run_check(tmp_path, tracked=["a.md"]).returncode == 0
