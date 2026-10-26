import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {after, before, test} from 'node:test';
import {createServer} from 'vite';
let server, cacheDir, resolve, empty;
before(async()=>{
  cacheDir=await mkdtemp(join(tmpdir(),'vks-navigation-test-'));
  server=await createServer({configFile:false,cacheDir,server:{middlewareMode:true,hmr:false,ws:false}});
  ({resolveExplanation:resolve,emptyNavigation:empty}=await server.ssrLoadModule('/src/navigation.ts'));
});
after(async()=>{await server?.close();await rm(cacheDir,{recursive:true,force:true});});
function workspace(){return {pages:[
  {id:'pa',stable_key:'page.a',metadata:{stateKey:'ready'}},{id:'pb',stable_key:'page.b',metadata:{stateKey:'next'}},
  {id:'empty',stable_key:'page.empty',metadata:{}}],actions:[
  {id:'a',stable_key:'action.a',metadata:{page_key:'page.a',target_state_key:'next'}},
  {id:'b',stable_key:'action.b',metadata:{page_key:'page.b'}}],scenarios:[
  {id:'s',metadata:{dsl:{explanation_graph:{states:[{stateKey:'ready'},{stateKey:'next'}]}}}}]};}
test('G01 hotspot and restored URL resolve the same declared page/action/state',()=>{
  const w=workspace();
  const start=resolve(w,empty,{actionId:'a',followTarget:false});
  const next=resolve(w,start,{actionId:'a',followTarget:true});
  assert.deepEqual(next,{pageId:'pb',actionId:'b',scenarioId:'s',stateKey:'next'});
  assert.deepEqual(resolve(w,empty,{...next,followTarget:false}),next);
});
test('G01 a page without actions stays selected and clears an unrelated state',()=>{
  const w=workspace();const start=resolve(w,empty,{actionId:'a',followTarget:false});
  assert.deepEqual(resolve(w,start,{pageId:'empty',actionId:'',followTarget:false}),
    {pageId:'empty',actionId:'',scenarioId:'s',stateKey:''});
});
test('G01 invalid or ambiguous navigation never invents a state or action',()=>{
  const w=workspace();
  assert.throws(()=>resolve(w,empty,{actionId:'missing'}));
  assert.throws(()=>resolve(w,empty,{pageId:'missing'}));
  assert.throws(()=>resolve(w,empty,{scenarioId:'s',stateKey:'missing'}));
  w.scenarios.push({id:'other',metadata:w.scenarios[0].metadata});
  assert.throws(()=>resolve(w,empty,{actionId:'a',followTarget:true}));
});
