"""Count substantive exposure of8 answer-dependent criteria in collected answers; no model/API calls.
Usage: python check_answer_dependent_coverage.py MODEL_GRADE_ROOT ANSWERS.csv
CSV fields: case_id,answer. One provider per file. Exit2 means fewer than10 independent conversations.
This checks applicability and structure presence, not semantic PASS/FAIL.
"""
import argparse,csv,json,re,sys
from pathlib import Path
from collections import defaultdict

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('project');p.add_argument('answers_csv');a=p.parse_args()
 sys.path.insert(0,str(Path(a.project).resolve()))
 from webapp.question_generation import flatten
 from code_eval.engine import evaluate_batch
 from code_eval.jev_applicability import j5_items_applicability
 root=Path(__file__).resolve().parent
 groups=json.loads((root/'questions.json').read_text(encoding='utf-8'))
 plan=json.loads((root/'answer_dependent_plan.json').read_text(encoding='utf-8'))
 with open(a.answers_csv,encoding='utf-8-sig',newline='')as f:
  reader=csv.DictReader(f)
  if not {'case_id','answer'}<=set(reader.fieldnames or []):raise ValueError('CSV must contain case_id,answer')
  answers={}
  for row in reader:
   key=row['case_id'].strip()
   if key in answers:raise ValueError('duplicate case_id: '+key)
   answers[key]=row['answer']
 wanted=set(plan['requirements']);observed={c:set()for c in wanted};evidence=defaultdict(list)
 payload=flatten([g for g in groups if g['id']in plan['conversation_ids']])
 for row in payload:row['answer']=answers.get(row['case_id'],'')
 batch=evaluate_batch(payload)
 for case in batch['cases']:
  if case['case_id']not in answers or case.get('case_status')!='EVALUATED':continue
  qid=int(re.match(r'Q(\d+)',case['case_id']).group(1))
  for row in case.get('results',[]):
   cid=str(row.get('check_id',''));base=cid.split(':')[0]
   if base in wanted and not cid.endswith(':none'):
    observed[base].add(qid);evidence[base].append({'case_id':case['case_id'],'check_id':cid,'verdict':row.get('verdict')})
  j=j5_items_applicability(case['question'],case.get('answer',''),answer_tools='unknown')
  if j['subs']['J5-3']['items']:
   observed['J5-3.a'].add(qid);evidence['J5-3.a'].append({'case_id':case['case_id'],'matches':j['subs']['J5-3']['matches'],'meaning':'applicable; semantic verdict still required'})
 short={k:len(v)for k,v in observed.items()if len(v)<10}
 result={'method':'actual ModelGrade answer structure checks; excludes :none rows; J5 source mention applicability','minimum_independent_conversations':10,'counts':{k:len(v)for k,v in observed.items()},'question_ids':{k:sorted(v)for k,v in observed.items()},'short':short,'missing_structures_by_conversation':{str(q):sorted(k for k,v in observed.items()if q not in v)for q in plan['conversation_ids']},'evidence':dict(evidence),'semantic_evaluation_completed':False}
 print(json.dumps(result,ensure_ascii=False,indent=2));return 2 if short else 0
if __name__=='__main__':raise SystemExit(main())
