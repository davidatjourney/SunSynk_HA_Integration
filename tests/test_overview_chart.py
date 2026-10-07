"""Run the shipped chart's history and lifecycle checks in Node."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_overview_chart_history_and_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for chart checks")
    subprocess.run(
        [node, str(Path(__file__).with_name("overview_chart.test.mjs"))],
        check=True,
        capture_output=True,
        text=True,
    )
