"""ruff over every Python file in the repository: the coding standard in
CONTRIBUTING.md - PEP 8 (pycodestyle's E and W rules) without a fixed line
length, and pyflakes' F rules (unused imports and variables, undefined names,
redefinitions), as ruff.toml selects. Part of the suite, so a finding fails CI
and a local run alike."""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Named folders, not the whole checkout: a virtualenv inside it (.venv, as
# CONTRIBUTING.md suggests) must not be linted.
FOLDERS = ("apps", "webui", "scripts", "tests")


def python_files():
    files = glob.glob(os.path.join(ROOT, "*.py"))
    for folder in FOLDERS:
        files += glob.glob(os.path.join(ROOT, folder, "**", "*.py"), recursive=True)
    return sorted(files)


def test_the_files_are_found():
    names = {os.path.relpath(path, ROOT) for path in python_files()}
    assert {"backup.py", "apps/servarr.py", "webui/app.py", "scripts/security_scan.py", "tests/test_lint.py"} <= names


def test_ruff_finds_nothing():
    # --no-cache: CI mounts the checkout read-only.
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--quiet",
         "--config", os.path.join(ROOT, "ruff.toml"), *python_files()],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
