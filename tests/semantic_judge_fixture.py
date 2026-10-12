"""Shared semantic-judge double and private dataset sampler; all responses are fixtures, no paid calls.

The judge answers only J criteria (the engine no longer asks supplemental C questions).
"""
import copy
import json
from pathlib import Path

import pytest

from code_eval.jev_client import JevCall, JevClientConfig

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data/code_eval/AIONA_winz_Q165_Q100_multiturn65_v1.json'


class Judge:
    config = JevClientConfig()

    def __init__(self, decision='PASS', mode=None):
        self.calls = []
        self.decision, self.mode = decision, mode

    def system_one(self, state, questions):
        self.calls.append(copy.deepcopy({'state': state, 'questions': questions}))
        if self.mode == 'timeout':
            return JevCall('TIMEOUT', error='fixture timeout')
        if self.mode == 'exception':
            raise RuntimeError('fixture exception')
        if self.mode == 'bad_body':
            return JevCall('OK', body=['malformed'])
        answers = {}
        for cid in questions:
            decision = self.decision
            answers[cid] = {'choice': decision, 'confidence': .9,
                            'probabilities': {'PASS': .95 if decision == 'PASS' else .05,
                                              'FAIL': .95 if decision == 'FAIL' else .05},
                            'reason': '원문의 지정 범위를 확인한 대역 판정.'}
            if self.mode == 'bad_j':
                answers[cid]['choice'] = 'MAYBE'
        return JevCall('OK', body={'model': 'fixture-only', 'answers': answers}, attempts=1)


def sample(*indices):
    if not DATA.is_file():
        pytest.skip('Optional private question dataset is not included in public checkouts')
    rows = json.loads(DATA.read_text(encoding='utf-8'))
    if isinstance(rows, dict):
        rows = rows['cases']
    return [rows[i - 1] for i in indices]
