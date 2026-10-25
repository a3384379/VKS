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
let server,permissions,imports,LoginForm,TeamSettings,WorkspaceContext;
before(async()=>{
 testCache=await mkdtemp(join(tmpdir(),'vks-web-test-'));
 server=await createServer({configFile:false,cacheDir:testCache,optimizeDeps:{noDiscovery:true,include:[]},plugins:[react()],server:{middlewareMode:true,hmr:false,ws:false}});
 permissions=await server.ssrLoadModule('/src/permissions.ts');
 imports=await server.ssrLoadModule('/src/imports.ts');
 ({LoginForm}=await server.ssrLoadModule('/src/LoginForm.tsx'));
 ({TeamSettings}=await server.ssrLoadModule('/src/TeamSettings.tsx'));
 ({WorkspaceContext}=await server.ssrLoadModule('/src/context.ts'));
});
after(async()=>{await server?.close();if(testCache)await rm(testCache,{recursive:true,force:true});});

test('project-specific grants take precedence over union scopes and never leak cross-project actions',()=>{
 const session={authenticated:true,scopes:['project:manage','source:read'],project_permissions:{one:['knowledge:read'],two:['knowledge:read','project:manage']}};
 assert.equal(permissions.hasProjectPermission(session,'one','project:manage'),false);
 assert.equal(permissions.hasProjectPermission(session,'two','project:manage'),true);
 assert.equal(permissions.hasProjectPermission(session,'unknown','source:read'),false);
 assert.equal(permissions.hasProjectPermission({...session,authenticated:false},'two','project:manage'),false);
});

test('standalone import inputs preserve exact API field names and writable baseline scope',()=>{
 assert.deepEqual(imports.importPayload('documents',['one','two'],'draft-one'),{kind:'documents',artifact_ids:['one','two'],analysis_set_id:'draft-one'});
 assert.deepEqual(imports.importPayload('prototype','image','draft-two','screenshot_hotspots'),{kind:'prototype',artifact_ids:['image'],analysis_set_id:'draft-two',prototype_mode:'screenshot_hotspots'});
 assert.deepEqual(imports.importPayload('bundle','zip','ignored'),{kind:'bundle',artifact_ids:['zip']});
 assert.equal(imports.importDetails('build.sh','documents'),null);
 assert.equal(imports.importDetails('unsafe.svg','prototype'),null);
 assert.equal(imports.importDetails('api.yaml','documents').media_type,'application/yaml');
 assert.equal(imports.importDetails('image.png','prototype').prototype_mode,'screenshot_hotspots');
 assert.equal(imports.importDetails('index.html','prototype').prototype_mode,'axure_html');
});

test('team login offers username/password and a separate bootstrap mode without embedding credentials',()=>{
 const html=renderToStaticMarkup(createElement(LoginForm,{busy:false,error:'',onLogin:async()=>{}}));
 assert.match(html,/团队账户/);assert.match(html,/初始化凭证/);assert.match(html,/autoComplete="username"/);assert.match(html,/type="password"/);
 assert.doesNotMatch(html,/value="vks_|localStorage|bootstrap_token:/);
});

function team(session){
 const w={project:{id:'one',name:'Team project',row_version:1}};
 const value={w,session,notify(){},refresh:async()=>{}};
 return renderToStaticMarkup(createElement(WorkspaceContext.Provider,{value},createElement(TeamSettings)));
}

test('viewer account settings do not render instance or project administration controls',()=>{
 const html=team({authenticated:true,name:'Reader',user:{id:'reader',instance_admin:false},actor_id:'reader',project_permissions:{one:['knowledge:read']}});
 assert.match(html,/我的账户/);assert.match(html,/Reader/);
 assert.doesNotMatch(html,/创建用户|加入项目成员|重置密码/);
});

test('instance owner sees account and project member management only with its project grant',()=>{
 const html=team({authenticated:true,name:'Owner',user:{id:'owner',instance_admin:true},actor_id:'owner',project_permissions:{one:['knowledge:read','project:manage']}});
 assert.match(html,/创建用户/);assert.match(html,/加入项目成员/);
 const withoutProject=team({authenticated:true,name:'Owner',user:{id:'owner',instance_admin:true},actor_id:'owner',project_permissions:{one:['knowledge:read']}});
 assert.match(withoutProject,/创建用户/);assert.doesNotMatch(withoutProject,/加入项目成员/);
});
