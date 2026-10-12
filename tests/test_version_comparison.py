"""Exercise the browser's pure version comparison rules without model calls."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_version_comparison_only_reports_verified_changes():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to verify version comparison rules')
    result = subprocess.run([node, str(ROOT / 'tests/version_comparison_test.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
