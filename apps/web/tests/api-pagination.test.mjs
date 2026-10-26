import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createServer} from 'vite';

test('A11 pagination helper preserves metadata while existing api returns data', async()=>{
  const cacheDir=await mkdtemp(join(tmpdir(),'vks-api-test-'));
  const server=await createServer({configFile:false,cacheDir,server:{middlewareMode:true,hmr:false,ws:false}});
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async()=>({ok:true,json:async()=>({data:[{id:'p21'}],meta:{request_id:'synthetic',next_cursor:'p21',extra:'retained'}})});
  try {
    const {api,apiPage}=await server.ssrLoadModule('/src/api.ts');
    assert.deepEqual(await api('/projects'),[{id:'p21'}]);
    assert.deepEqual(await apiPage('/projects'),{data:[{id:'p21'}],meta:{request_id:'synthetic',next_cursor:'p21',extra:'retained'}});
  } finally {globalThis.fetch=originalFetch;await server.close();await rm(cacheDir,{recursive:true,force:true});}
});
