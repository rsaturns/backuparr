"""Run the settings race regression with Node's built-in test runner."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_connection_settings_events():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript event regression")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("app_connection.cjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
