"""pyflakes over every Python file in the repository: unused imports and
variables, undefined names, redefinitions - the simple mistakes a review
can miss. Part of the suite, so a warning fails CI and a local run alike."""
import glob
import io
import os

from pyflakes import api, reporter

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


def test_pyflakes_finds_nothing():
    out, err = io.StringIO(), io.StringIO()
    warnings = sum(api.checkPath(path, reporter.Reporter(out, err)) for path in python_files())
    assert warnings == 0, out.getvalue() + err.getvalue()
