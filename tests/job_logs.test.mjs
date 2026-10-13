import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const template=readFileSync(new URL('../transcription/templates/job.html',import.meta.url),'utf8');
const appendLogs=template.slice(template.indexOf('function appendLogs('),template.indexOf('let speakerSignature='));

function fixture(distance, hasContent=true) {
  const original={textContent:hasContent ? 'Earlier event\n' : 'Waiting for graph events…'};
  const element={
    children:[original], scrollHeight:1000, clientHeight:330, scrollTop:670-distance,
    replaceChildren() { this.children=[]; },
    appendChild(node) { this.children.push(node); this.scrollHeight+=100; },
    set textContent(value) { this.children=[{textContent:value}]; },
  };
  const context=vm.createContext({
    logsElement:element, logsBottomThreshold:48, logsHaveContent:hasContent, logsOffset:14,
    document:{createTextNode:textContent=>({textContent})},
  });
  vm.runInContext(appendLogs,context);
  return {element,original,context};
}

test('appending preserves existing nodes and a reader scrolled away from the bottom',()=>{
  const {element,original,context}=fixture(49);
  context.appendLogs({logs:'New event\n',logs_offset:24});
  assert.equal(element.children[0],original);
  assert.equal(element.children[1].textContent,'New event\n');
  assert.equal(element.scrollTop,621);
  assert.equal(context.logsOffset,24);
});

test('follows new events within 48px of the bottom',()=>{
  for(const distance of [0,30,48]) {
    const {element,context}=fixture(distance);
    context.appendLogs({logs:'New event\n',logs_offset:24});
    assert.equal(element.scrollTop,element.scrollHeight);
  }
});

test('empty polls leave the DOM and scroll position untouched',()=>{
  const {element,original,context}=fixture(0);
  context.appendLogs({logs:'',logs_offset:14});
  assert.deepEqual(element.children,[original]);
  assert.equal(element.scrollTop,670);
});

test('first events replace the placeholder and reset responses replace stale logs',()=>{
  const {element,context}=fixture(0,false);
  context.appendLogs({logs:'First event\n',logs_offset:12});
  assert.deepEqual(element.children,[{textContent:'First event\n'}]);
  context.appendLogs({logs:'Restarted\n',logs_offset:10,logs_reset:true});
  assert.deepEqual(element.children,[{textContent:'Restarted\n'}]);
  context.appendLogs({logs:'',logs_offset:0,logs_reset:true});
  assert.deepEqual(element.children,[{textContent:'Waiting for graph events…'}]);
  assert.equal(context.logsOffset,0);
});
