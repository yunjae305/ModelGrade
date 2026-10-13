"""결과 화면의 평가 방식 표시 규칙(frontend/src/evaluation-method.js)을 node 로 확인한다."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_evaluation_method_label_reads_only_the_saved_run():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to verify the evaluation method label')
    result = subprocess.run([node, str(ROOT / 'tests/evaluation_method_test.js')],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
