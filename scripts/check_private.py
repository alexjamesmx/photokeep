"""Fail if any tracked file matches a pattern in the untracked .private-patterns."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS_FILE = ".private-patterns"


def load_patterns() -> list[re.Pattern[str]]:
    path = Path(PATTERNS_FILE)
    if not path.is_file():
        return []
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [re.compile(ln, re.IGNORECASE) for ln in lines if ln]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True, text=True
    ).stdout
    return [f for f in out.split("\0") if f]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*", help="check these files instead of git ls-files")
    args = ap.parse_args()
    patterns = load_patterns()
    files = tracked_files() if args.files is None else args.files
    hits = 0
    for name in files:
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for pat in patterns:
                if pat.search(line):
                    print(f"{name}:{n}: {pat.pattern}")
                    hits += 1
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
