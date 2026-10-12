"""Run the production JavaScript's verdict/coverage regression checks in pytest."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_result_presentation_keeps_quality_and_completion_separate():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to verify the production result presentation rules')
    result = subprocess.run([node, str(ROOT / 'tests/workspace_results_test.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failure_locations_match_saved_evidence_without_changing_answers():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to verify the production failure location rules')
    result = subprocess.run([node, str(ROOT / 'tests/failure_locations_test.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
