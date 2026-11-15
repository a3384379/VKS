// Shared by the standalone loader and portable export. No network or dynamic code evaluation.
async function fhUnpackDocument(meta, encoded) {
  if(meta?.format!=='fh-gzip-html-1' || !Number.isSafeInteger(meta.bytes) || meta.bytes<1 || meta.bytes>83886080 || !/^[a-f0-9]{64}$/.test(meta.sha256))throw new Error('压缩文件信息无效。');
  if(typeof DecompressionStream!=='function' || !globalThis.crypto?.subtle)throw new Error('浏览器不支持本地解压，请使用较新的 Chrome、Edge 或 Safari。');
  const raw=atob(encoded.trim()),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
  const reader=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
  const chunks=[];let size=0;
  try{for(;;){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>meta.bytes)throw new Error('解压长度与文件信息不符。');chunks.push(value);}}
  catch(error){await reader.cancel().catch(()=>{});throw error;}
  if(size!==meta.bytes)throw new Error('压缩文件不完整。');
  const decoded=new Uint8Array(size);let offset=0;for(const chunk of chunks){decoded.set(chunk,offset);offset+=chunk.length;}
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',decoded))].map(x=>x.toString(16).padStart(2,'0')).join('');
  if(digest!==meta.sha256)throw new Error('文件校验未通过，请重新复制完整文件。');
  return new TextDecoder('utf-8',{fatal:true}).decode(decoded);
}

async function fhPackDocument(html, template) {
  if(typeof CompressionStream!=='function')throw new Error('浏览器不支持保存压缩版，请使用较新的 Chrome、Edge 或 Safari。');
  const bytes=new TextEncoder().encode(html);
  if(bytes.length>83886080)throw new Error('内容超过单文件压缩版支持的大小。');
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const compressed=new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer());
  const parts=[];for(let i=0;i<compressed.length;i+=24576)parts.push(btoa(String.fromCharCode(...compressed.subarray(i,i+24576))));
  return template.replace('__FH_PACK_META__',JSON.stringify({format:'fh-gzip-html-1',authoring:'vks-handbook-authoring-1',bytes:bytes.length,sha256:digest})).replace('__FH_PACK_DATA__',parts.join(''));
}

// Article markup is trusted local handbook data, preserved byte-for-byte in this payload.
// Only articles being read are attached to the live document; anchors keep their original IDs.
(()=>{
  const pages=JSON.parse(document.getElementById('fh-book-pages').textContent);
  const anchors=new Map();
  for(const [id,page] of Object.entries(pages)){anchors.set(id,id);for(const anchor of page.anchors)if(!anchors.has(anchor))anchors.set(anchor,id);}
  let active=null;
  const readingStates=new Map();
  const scrollSelector='.table-scroll,.diagram-canvas,pre';
  function saveReadingState(article){
    readingStates.set(article.id,{
      details:[...article.querySelectorAll('details')].map(el=>el.open),
      scroll:[...article.querySelectorAll(scrollSelector)].map(el=>({left:el.scrollLeft,top:el.scrollTop}))
    });
  }
  function ensure(anchor){
    const id=anchors.get(anchor),article=id&&document.getElementById(id);
    if(!article)return null;
    if(!article.hasAttribute('data-fh-loaded')){
      article.innerHTML=pages[id].html;article.setAttribute('data-fh-loaded','');
      article.querySelectorAll('.prose table').forEach(table=>{
        const wrapper=document.createElement('div');wrapper.className='table-scroll';wrapper.tabIndex=0;
        wrapper.setAttribute('role','region');wrapper.setAttribute('aria-label','表格，可横向滚动');table.replaceWith(wrapper);wrapper.append(table);
      });
      const state=readingStates.get(id);
      if(state){
        article.querySelectorAll('details').forEach((el,index)=>{el.open=state.details[index]??el.open;});
        // Hidden shells have no scroll geometry until the controller shows them.
        const mounted=article.firstChild;
        requestAnimationFrame(()=>{
          if(article.firstChild!==mounted)return;
          article.querySelectorAll(scrollSelector).forEach((el,index)=>{const position=state.scroll[index];if(position){el.scrollLeft=position.left;el.scrollTop=position.top;}});
        });
      }
    }
    return article;
  }
  function activate(id){
    active=id||null;if(id)ensure(id);
    for(const article of document.querySelectorAll('article.document[data-fh-loaded]'))if(article.id!==active){saveReadingState(article);article.replaceChildren();article.removeAttribute('data-fh-loaded');}
  }
  function cleanExport(root){
    for(const article of root.querySelectorAll('article.document')){article.replaceChildren();article.removeAttribute('data-fh-loaded');}
  }
  window.fhBookPages=Object.freeze({ensure,activate,cleanExport});
  // Existing print action prints the current article; keep inactive bodies unloaded.
  const template=JSON.parse(document.getElementById('fh-pack-template').textContent);
  window.fhPortablePack=Object.freeze({
    encode:html=>fhPackDocument(html,template),
    async decode(html){
      // Importing an index from a packed HTML reads inert data; no imported scripts run.
      if(!html.includes('id="fh-pack-meta"'))return html;
      const meta=[...html.matchAll(/<script\s+id="fh-pack-meta"\s+type="application\/json">([^<]*)<\/script>/gi)];
      const data=[...html.matchAll(/<script\s+id="fh-pack-data"\s+type="application\/octet-stream">([^<]*)<\/script>/gi)];
      if(meta.length!==1 || data.length!==1)throw new Error('压缩文件内容缺失或重复。');
      return fhUnpackDocument(JSON.parse(meta[0][1]),data[0][1]);
    }
  });
})();
