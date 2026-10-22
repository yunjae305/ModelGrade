"""Usage: python verify_with_modelgrade.py /absolute/path/to/ModelGrade-main"""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).resolve()))
from tools.check_question_set import check_items
from webapp.question_generation import flatten,target_counts,k_var_summary,k_var_units
root=Path(__file__).resolve().parent
groups=json.loads((root/'questions.json').read_text(encoding='utf-8'))
report=check_items(flatten(groups),pipeline='items',minimum=10)
counts=target_counts(report);summary=k_var_summary(groups,report)
short={k:v for k,v in counts.items() if v<10}
units=k_var_units()
conversation_k={}
for g in groups:
 if len(g['turns'])>1:
  rows=[r for r in report['cases'] if r['case_id'].startswith('Q'+str(g['id'])+'_')]
  values=[len(set(r['checks']+r['items'])&units) for r in rows]
  conversation_k[str(g['id'])]={'turn_k':values,'exact_average':sum(values)/len(values)}
assert len(conversation_k)==11
assert all(6<=v['exact_average']<=10 for v in conversation_k.values()),conversation_k
print(json.dumps({'multiturn_k':conversation_k,'question_count':len(groups),'countable':len(counts),'short':short,'k':summary,'errors':report['errors'],'duplicates':report['duplicates'],'undetermined':report['undetermined']},ensure_ascii=False,indent=2))
assert len(groups)==78 and len(counts)==64 and not short
assert not summary['out_of_range'] and summary['exempt']==10
assert not report['errors'] and not report['duplicates'] and not report['undetermined']
