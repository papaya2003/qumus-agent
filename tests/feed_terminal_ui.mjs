// Terminal read-only Feed projection; no browser, jobs, World or provider.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {Feed} from '../qumus2/ui/static/feed.js';
import {Session} from '../qumus2/ui/static/session.js';

class Element {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.style={};this.parentNode=null;this._text='';
    this.scrollTop=this.scrollHeight=this.clientHeight=0;
    this.classList={contains:name=>(this.attrs.class||'').split(/\s+/).includes(name),
      add:(...names)=>this.setAttribute('class',[...new Set([...(this.attrs.class||'').split(/\s+/).filter(Boolean),...names])].join(' ')),
      remove:(...names)=>this.setAttribute('class',(this.attrs.class||'').split(/\s+/).filter(name=>!names.includes(name)).join(' ')),
      toggle:(name,force)=>{if(force??!this.classList.contains(name))this.classList.add(name);else this.classList.remove(name);}};
  }
  get id(){return this.attrs.id;}
  get textContent(){return this._text+this.children.map(child=>child instanceof Element?child.textContent:child).join('');}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  set className(value){this.setAttribute('class',value);}
  get className(){return this.attrs.class||'';}
  setAttribute(key,value){this.attrs[key]=String(value);}
  getAttribute(key){return this.attrs[key]??null;}
  addEventListener(){}
  append(...children){for(const child of children){if(child instanceof Element){child.remove();child.parentNode=this;}this.children.push(child);}}
  appendChild(child){this.append(child);return child;}
  replaceChildren(...children){for(const child of this.children)if(child instanceof Element)child.parentNode=null;this.children=[];this._text='';this.append(...children);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(child=>child!==this);this.parentNode=null;}
  matches(selector){return selector.startsWith('.')?selector.slice(1).split('.').every(name=>this.classList.contains(name)):this.tagName===selector;}
  querySelectorAll(selector){return walk(this).filter(node=>node!==this&&node.matches(selector));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
const walk=node=>[node,...node.children.filter(child=>child instanceof Element).flatMap(walk)];
globalThis.Node=Element;
globalThis.document={createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
globalThis.ResizeObserver=class{observe(){}};
globalThis.requestAnimationFrame=callback=>{callback(0);return 0;};

const event=(id,kind,payload={},agent='hands',run_id=null)=>({id,ts:id,kind,payload,agent,run_id});
const call=(id=1,agent='hands')=>event(id,'tool.call',{tool:'get_job_status',tool_use_id:`c${id}`,args:{}},agent);
const result=(id=2,status='ok',agent='hands',key='c1')=>event(id,'tool.result',{tool:'get_job_status',tool_use_id:key,status,summary:`saved ${status}`,data:{}},agent);
function fixture(events=[]) {
  const parent=new Element(),box=new Element();parent.append(box);
  const feed=new Feed(box,{openRun(){},openPlan(){},photo(){},faultText:()=>''}),session=new Session();
  const add=e=>{const run=session.add(e);feed.add(e,run);};
  events.forEach(add);return {feed,session,add};
}

test('matched implicit tools become neutral recorded activity without fabricated completion or lost expansion',()=>{
  const {feed,session}=fixture([call(),result()]),member=feed.cards.get('hands');
  feed.toggleTools(member);const detail=member._tbody.children[0]._detail;detail.hidden=false;
  const before=JSON.stringify(session.events);
  feed.finishRecordedActivity(session);
  assert.equal(member._tag.textContent,'Recorded activity');
  assert.equal(member.classList.contains('busy'),false);
  assert.equal(member.classList.contains('returned'),false);
  assert.equal(member._expanded,true);assert.equal(member.classList.contains('expanded'),true);
  assert.equal(detail.hidden,false);
  assert.equal(JSON.stringify(session.events),before);
});

test('unmatched calls remain incomplete and retain the original unresolved tool',()=>{
  const {feed,session}=fixture([call()]),member=feed.cards.get('hands');
  feed.finishRecordedActivity(session);
  assert.equal(member._tag.textContent,'Incomplete in recording');
  assert.equal(session.log.tools.calls.size,1);
  assert.equal(member._tbody.children[0]._dot.getAttribute('aria-label'),'Running');
  assert.equal(member.classList.contains('busy'),true);
});

test('an explicit unreported delegation or SDK turn is not completed by returned calls',()=>{
  const delegated=fixture([event(1,'agent.delegate',{to:'hands',task:'Review',tool_use_id:'d1'},'orchestrator'),call(2),result(3,'ok','hands','c2')]);
  delegated.feed.finishRecordedActivity(delegated.session);
  assert.equal(delegated.feed.cards.get('hands')._tag.textContent,'Incomplete in recording');
  const sdk=fixture([call(),result()]);sdk.feed.finishRecordedActivity(sdk.session,{modelDriven:true});
  assert.equal(sdk.feed.cards.get('hands')._tag.textContent,'Incomplete in recording');
  const requested=fixture([event(0,'agent.request',{},'hands'),call(),result()]);
  requested.feed.finishRecordedActivity(requested.session);
  assert.equal(requested.feed.cards.get('hands')._tag.textContent,'Incomplete in recording');
});

for(const status of ['queued','running','paused','parked','waiting_human']) test(`recording end retains ${status} jobs without promoting success`,()=>{
  const {feed,session}=fixture([call(),result(2,'pending'),event(3,'job.submitted',{flow:'search_chip',params:{}},'hands','j'),event(4,'job.status',{status,message:'Original unresolved job'},null,'j')]);
  const before=JSON.stringify(session.events);feed.finishRecordedActivity(session);
  assert.equal(feed.cards.get('hands')._tag.textContent,'Incomplete in recording');
  assert.equal(session.book.runs.get('j').status,status);
  assert.equal(JSON.stringify(session.events),before);
  if(['paused','parked','waiting_human'].includes(status))assert.ok(feed.list.querySelector(`.jline.${status}`));
});

test('returned errors, denials and failed jobs retain their original warnings and problems',()=>{
  const {feed,session}=fixture([call(),result(2,'error'),event(3,'tool.denied',{tool:'operate',reason:'Original refusal'},'advisor'),event(4,'job.submitted',{flow:'search_chip',params:{}},'hands','j'),event(5,'job.status',{status:'failed',message:'Original failure'},null,'j'),event(6,'safety.violation',{device:'arm_right',command:'move',reason:'Original safety'},null,'j')]);
  const problems=session.log.problems.length;feed.finishRecordedActivity(session);
  assert.equal(session.log.problems.length,problems);assert.ok(problems>0);
  assert.ok(feed.list.querySelector('.tc.bad'));assert.ok(feed.list.querySelector('.tc.warn'));
  assert.ok(feed.list.querySelector('.jline.failed'));
  assert.equal(session.book.runs.get('j').status,'failed');
  assert.match(feed.list.textContent,/Original safety/);
  assert.equal(feed.cards.get('hands')._tag.textContent,'Recorded activity');
});

test('SDK completion keeps existing returned semantics, while live idle and rebuilt prefixes do not finalize groups',()=>{
  const completed=fixture([event(1,'agent.delegate',{to:'hands',task:'Review',tool_use_id:'d'},'orchestrator'),call(2),result(3,'ok','hands','c2')]);
  const member=completed.feed.cards.get('hands');completed.add(event(4,'agent.done',{},'hands'));
  completed.feed.finishRecordedActivity(completed.session,{modelDriven:true});
  assert.equal(member.classList.contains('returned'),true);assert.equal(member._tag.hidden,true);
  const prefix=fixture([call(),result()]);prefix.feed.setWorking(false);
  assert.equal(prefix.feed.cards.get('hands')._tag.textContent,'working');
  prefix.feed.finishRecordedActivity(prefix.session);prefix.feed.reset();prefix.session.reset();prefix.add(call());
  assert.equal(prefix.feed.cards.get('hands')._tag.textContent,'working');
  assert.equal(prefix.session.log.tools.calls.size,1);
});

for(const modelDriven of [false,true]) test(`explicit Stop survives terminal projection in ${modelDriven?'SDK':'scripted'} mode`,()=>{
  const status=modelDriven?'paused':'succeeded';
  const {feed,session}=fixture([call(),result(),
    event(3,'job.submitted',{flow:'search_chip',params:{}},'hands','j'),
    event(4,'job.status',{status,message:'Original job state'},null,'j'),
    event(5,'system.stopped',{dropped:[]},'orchestrator')]);
  const member=feed.cards.get('hands'),before=JSON.stringify(session.events);
  assert.equal(member._tag.textContent,'stopped');
  feed.finishRecordedActivity(session,{modelDriven});
  assert.equal(member._tag.textContent,'stopped');
  assert.equal(member._tag.className,'tag paused');
  assert.ok(feed.list.querySelector('.divider.stop'));
  assert.ok(feed.list.querySelector(`.jline.${status}`));
  assert.equal(session.book.runs.get('j').status,status);
  assert.equal(JSON.stringify(session.events),before);
  assert.equal(session.log.tools.calls.size,0);
});

const actual=process.env.QUMUS_TERMINAL_RECORDING;
test('explicit actual Diagnostic 2 audit: 45 pairs, 12 successful jobs and no manufactured lifecycle events',{skip:!actual},()=>{
  const raw=readFileSync(actual),record=JSON.parse(raw),{feed,session}=fixture(record.events);
  assert.equal(record.events.filter(e=>e.kind==='tool.call').length,45);
  assert.equal(record.events.filter(e=>e.kind==='tool.result').length,45);
  assert.equal(session.book.runs.size,12);
  assert.ok([...session.book.runs.values()].every(run=>run.status==='succeeded'));
  assert.equal(session.log.tools.calls.size,0);assert.equal(session.log.problems.length,0);
  const before=JSON.stringify(session.events);feed.finishRecordedActivity(session,{modelDriven:record.meta.agents==='sdk'});
  assert.deepEqual([...feed.cards.values()].map(member=>member._tag.textContent),['Recorded activity','Recorded activity']);
  assert.ok([...feed.cards.values()].every(member=>!member.classList.contains('returned')));
  assert.equal(JSON.stringify(session.events),before);
  assert.equal(createHash('sha256').update(readFileSync(actual)).digest('hex'),createHash('sha256').update(raw).digest('hex'));
});
