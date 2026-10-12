"""Persist randomized display/evaluation order; never rewrite original Q/A."""
from random import SystemRandom


def blind_plan(providers):
    order = list(providers)
    SystemRandom().shuffle(order)
    return {
        'version': 'comparison-blind-v1',
        'enabled': True,
        'order': order,
        'labels': {provider: '모델 ' + chr(65 + index) for index, provider in enumerate(order)},
        'judge_input_fields': ['question', 'answer', 'reference', 'history'],
        'provider_metadata_sent_to_judge': False,
        'original_text_preserved': True,
        'comparison_scope': 'common_code_and_J1_J6',
        'note': '모델명·공급자·원본 응답 메타데이터는 의미 평가 요청에 포함하지 않습니다. 원문 안의 이름은 그대로 보존합니다.',
    }
