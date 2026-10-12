'use strict';
const assert = require('node:assert/strict');
const {EventState, DisplayQueue, EvalProgress} = require('../frontend/src/evaluation-stream.js');
const state = new EventState(), queue = new DisplayQueue();
let id = 0;
const send = e => { const event = {...e, id:++id}; assert.equal(state.apply(event), true); queue.add(event); return event; };
send({type:'run_start', total:1500});
for (let index=0; index<1500; index++) {
  const base={model:'모델 A',index,qid:'duplicate'};
  send({...base,type:'q_start'});
  send({...base,type:'criterion_start',code:'3-1',name:'계산'});
  send({...base,type:'criterion_result',key:'calculation',result:'UNJUDGED'});
  send({...base,type:'criterion_result',key:'calculation',result:index%2 ? 'PASS':'FAIL'});
  const e=send({...base,type:'q_done',pass:index%2,fail:index%2?0:1,unjudged:0});
  assert.equal(state.apply(e),false); // replaying the same event ID is ignored
}
assert.equal(state.data.done,1500);
assert.deepEqual([state.data.pass,state.data.fail,state.data.unjudged],[750,750,0]);
assert.equal(Object.keys(state.data.rows).length,1500);
assert.equal(queue.fast,true);
send({type:'run_done',done:1500,total:1500,pass:750,fail:750,unjudged:0,skipped:0});
assert.equal(queue.items.length,0);
assert.equal(state.data.status,'COMPLETED');
const backlog=new DisplayQueue();
backlog.add({type:'q_done'});
for(let i=0;i<11;i++) backlog.add({type:'criterion_start'});
assert.equal(backlog.items[0].type,'q_done');
assert.ok(backlog.items.length<=10);
const snapshot=JSON.parse(JSON.stringify(state.data));
const recovered=new EventState(); recovered.apply({type:'snapshot',id:500,...snapshot});
assert.equal(recovered.data.done,1500);
recovered.apply({type:'run_start',id:501,total:1,generation:'retry'});
assert.equal(recovered.data.done,0); assert.equal(Object.keys(recovered.data.rows).length,0);
recovered.apply({type:'criterion_start',id:502,model:'aiona',index:0,qid:'Q1_T1',code:'J5',name:'신뢰도',stage:'LLM',together:true});
assert.equal(recovered.data.rows['aiona:0'].active.together,true);
const ticker = Object.create(EvalProgress.prototype);
ticker.modelLabels = {aiona:'모델 A'};
const semantic = {model:'aiona',qid:'Q1_T1',code:'J5',name:'신뢰도',stage:'LLM',together:true};
assert.equal(ticker.criterionText(semantic),'모델 A · Q1_T1 · 의미 평가 응답 대기 · 여러 기준 함께 평가');
assert.equal(ticker.criterionText({...semantic,code:'3-1',name:'계산',stage:'CODE',together:false}),'모델 A · Q1_T1 · 3-1 계산 기준 평가 중…');
// items 파이프라인: q_done 의 rechecks·unverifiable 이 행에 남고 요약 문구에 표시된다. legacy 행에는 없다.
const itemsState = new EventState();
itemsState.apply({type:'run_start',id:1,total:2});
itemsState.apply({type:'criterion_result',id:2,model:'aiona',index:0,qid:'Q1',key:'J2',code:'J2',name:'요청 적합성',result:'FAIL',reason:'J2-1.a: 확인 불가',stage:'LLM',unverifiable:1});
itemsState.apply({type:'q_done',id:3,model:'aiona',index:0,qid:'Q1',pass:0,fail:1,unjudged:0,rechecks:2,unverifiable:1});
itemsState.apply({type:'q_done',id:4,model:'aiona',index:1,qid:'Q2',pass:1,fail:0,unjudged:0});
assert.equal(itemsState.data.rows['aiona:0'].rechecks,2); assert.equal(itemsState.data.rows['aiona:0'].unverifiable,1);
assert.equal(itemsState.data.rows['aiona:0'].criteria.J2.unverifiable,1);
assert.equal(itemsState.data.rows['aiona:1'].rechecks,undefined);
assert.equal(ticker.rowText(itemsState.data.rows['aiona:0']),'모델 A · Q1  PASS 0 · FAIL 1 · 미판정 0 · 재확인 2회 · 확인 불가 1');
assert.equal(ticker.rowText(itemsState.data.rows['aiona:1']),'모델 A · Q2  PASS 0 · FAIL 0 · 미판정 0');   // 행 집계는 criterion_result 로만 센다
assert.equal(ticker.itemsText({rechecks:0,unverifiable:0}),'');
console.log('1500-row event state, replacement counts, reconnect, retry, bounded queue and truthful semantic waiting label passed.');
