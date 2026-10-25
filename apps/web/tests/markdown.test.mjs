import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {after,before,test} from 'node:test';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

let testCache;
let server,attachmentUrl,rasterContentType,Markdown,CodeHighlight,Documents,WorkspaceContext,diagramKind,validateDiagramSource;
before(async()=>{
 testCache=await mkdtemp(join(tmpdir(),'vks-web-test-'));
 server=await createServer({configFile:false,cacheDir:testCache,optimizeDeps:{noDiscovery:true,include:[]},plugins:[react()],server:{middlewareMode:true,hmr:false,ws:false}});
 ({attachmentUrl,rasterContentType}=await server.ssrLoadModule('/src/MarkdownAttachment.tsx'));
 ({Markdown}=await server.ssrLoadModule('/src/components.tsx'));
 ({default:CodeHighlight}=await server.ssrLoadModule('/src/CodeHighlight.tsx'));
 ({Documents}=await server.ssrLoadModule('/src/pages.tsx'));
 ({WorkspaceContext}=await server.ssrLoadModule('/src/context.ts'));
 ({diagramKind,validateDiagramSource}=await server.ssrLoadModule('/src/diagram-policy.ts'));
});
after(async()=>{await server?.close();if(testCache)await rm(testCache,{recursive:true,force:true});});

test('relative attachments stay bound to one document revision and never become remote URLs',()=>{
 const url=attachmentUrl('document','revision','../images/示意图.png#image');
 assert.equal(url,'/api/v1/documents/document/revisions/revision/attachments?path=..%2Fimages%2F%E7%A4%BA%E6%84%8F%E5%9B%BE.png');
 assert.equal(attachmentUrl(undefined,'revision','image.png'),null);
 for(const path of ['https://outside.test/image.png','//outside.test/image.png','/api/v1/other','data:image/svg+xml,anything','javascript:alert(1)',' javascript:alert(1) ','%2F%2Foutside.test','file:///private/path','folder\\image.png','image%00.png']){
   assert.equal(attachmentUrl('document','revision',path),null,path);
 }
});

test('image preview accepts only explicit raster response types',()=>{
 assert.equal(rasterContentType('IMAGE/PNG; charset=binary'),'image/png');
 assert.equal(rasterContentType('image/jpeg'),'image/jpeg');
 for(const media of ['image/svg+xml','text/html','application/octet-stream','image/png-extra','',null]) assert.equal(rasterContentType(media),null);
});

test('Markdown renders authorized relative downloads and keeps external resources passive',()=>{
 const html=renderToStaticMarkup(createElement(Markdown,{documentId:'document',revisionId:'revision',content:'![图示](images/map.png)\n\n[说明](attachments/spec.pdf)\n\n![外部](https://outside.test/image.png) [外链](https://outside.test/)'}));
 assert.match(html,/正在读取授权图片/);assert.match(html,/attachments\?path=attachments%2Fspec.pdf/);assert.match(html,/download=""/);
 assert.doesNotMatch(html,/src="https?:|href="https?:|outside\.test/);
 assert.match(html,/链接未打开/);
});

test('real syntax highlighting produces escaped React tokens without injecting markup',()=>{
 const html=renderToStaticMarkup(createElement(CodeHighlight,{language:'javascript',value:'const html = "<img src=x onerror=alert(1)>";'}));
 assert.match(html,/hljs-keyword/);assert.match(html,/hljs-string/);assert.match(html,/&lt;img/);
 assert.doesNotMatch(html,/<img|<script|onerror="/);
 const fallback=renderToStaticMarkup(createElement(CodeHighlight,{language:'unsupported-language',value:'<script>raw</script>'}));
 assert.match(fallback,/&lt;script&gt;/);assert.doesNotMatch(fallback,/<script>/);
});

test('document contents follow original source order and exclude another document',()=>{
 const doc={id:'doc',entity_id:'doc',entity_revision_id:'doc-r1',stable_key:'doc-key',type:'document',name:'Ordered',artifact_id:'artifact',content:'# Ordered',metadata:{},locator:{},row_version:1};
 const section=(id,name,line)=>({id,entity_id:id,entity_revision_id:id+'-r1',stable_key:id,type:'section',name,artifact_id:'artifact',content:name,metadata:{document_key:'doc-key'},locator:{startLine:line},row_version:1});
 const third=section('third','Third chapter',30),first=section('first','First chapter',2),second=section('second','Second chapter',10);
 second.locator={line_start:10};
 const foreign={...section('foreign','Private foreign chapter',1),artifact_id:'different',metadata:{document_key:'other-doc'}};
 const value={w:{project:{id:'project'},selected_analysis_set:{id:'draft',state:'draft'},documents:[doc],entities:[doc,third,foreign,first,second],relations:[],evidence:[]},session:{authenticated:true,project_permissions:{project:['knowledge:read']}},selected:'doc',setSelected(){},navigate(){},notify(){},run:async()=>{},setUnsavedChanges(){}};
 const html=renderToStaticMarkup(createElement(WorkspaceContext.Provider,{value},createElement(Documents)));
 const start=html.indexOf('class="section-tree"');
 assert.ok(start>=0);
 const contents=html.slice(start,html.indexOf('</div>',start));
 assert.ok(contents.indexOf('First chapter')<contents.indexOf('Second chapter'));
 assert.ok(contents.indexOf('Second chapter')<contents.indexOf('Third chapter'));
 assert.doesNotMatch(contents,/Private foreign chapter/);
});

test('math renders inline, display, math/latex/tex fences and keeps invalid input readable',()=>{
 const content=String.raw`Inline $E=mc^2$.

$$
\frac{1}{2}
$$

`+'```math\nx^2\n```\n\n```latex\n\\sqrt{2}\n```\n\n```tex\n\\sum_{i=1}^n i\n```\n\n'+String.raw`Broken $\frac{1}{$`;
 const html=renderToStaticMarkup(createElement(Markdown,{content}));
 assert.equal((html.match(/class="katex"/g)||[]).length,5);
 assert.match(html,/katex-error/);
 assert.match(html,/Broken/);
 assert.match(html,/<math/);
});

test('math does not load images, promote URLs or expand unbounded source',()=>{
 const content=String.raw`$\includegraphics{https://outside.test/pixel}$ $\href{javascript:alert(1)}{click}$`;
 const html=renderToStaticMarkup(createElement(Markdown,{content}));
 assert.doesNotMatch(html,/<img|href="javascript:|src="https?:/);
 const large=renderToStaticMarkup(createElement(Markdown,{content:'$$\n'+'x'.repeat(8001)+'\n$$'}));
 assert.match(large,/公式过大，已显示源码/);assert.doesNotMatch(large,/class="katex"/);
});

test('diagram blocks are semantic figures with source fallback instead of nested pre elements',()=>{
 const html=renderToStaticMarkup(createElement(Markdown,{content:'Before\n\n```mermaid\nflowchart LR\nA-->B\n```\n\n```puml\nAlice -> Bob\n```\n\nAfter'}));
 assert.equal((html.match(/class="diagram-block"/g)||[]).length,2);
 assert.doesNotMatch(html,/<pre><figure/);
 assert.match(html,/查看图表源码/);assert.match(html,/A--&gt;B/);assert.match(html,/Before/);assert.match(html,/After/);
});

test('diagram input policy allows standard DSL while rejecting executable configuration and resource imports',()=>{
 for(const value of ['mermaid','Mermaid'])assert.equal(diagramKind(value),'mermaid');
 for(const value of ['plantuml','puml','uml'])assert.equal(diagramKind(value),'plantuml');
 assert.equal(diagramKind('javascript'),null);
 assert.equal(validateDiagramSource('mermaid','sequenceDiagram\nAlice->>Bob: Hello'),null);
 assert.equal(validateDiagramSource('plantuml','@startuml\nAlice -> Bob: Hello!\n@enduml'),null);
 for(const text of ['!include /private/file','!includeurl https://outside.test/secret','!while 1','%load_json("http://outside.test")']) assert.ok(validateDiagramSource('plantuml',text));
 for(const text of ['%%{init: {securityLevel: "loose"}}%%\nA-->B','---\nconfig: true','flowchart LR\nA@{img:"https://outside.test"}','flowchart LR\nA[<img src=x>]'])assert.ok(validateDiagramSource('mermaid',text));
 assert.ok(validateDiagramSource('mermaid','x'.repeat(24001)));
});
