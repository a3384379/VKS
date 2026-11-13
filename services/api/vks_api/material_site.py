"""Build a self-contained browser viewing derivative of a local static site."""

import json
import mimetypes
from base64 import b64encode


def site_html(entry, files):
    payload = {
        path: {"data": b64encode(raw).decode(), "type": mimetypes.guess_type(path)[0] or "application/octet-stream"}
        for path, raw in files
    }
    # This platform-owned bootstrap constructs browser Blob resources. Source
    # scripts are never executed by the parser or API process.
    script = r"""
const files=JSON.parse(document.getElementById('vks-site').textContent),urls={},html={};
const decode=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
for(const [p,f] of Object.entries(files))if(!/\.html?$/i.test(p)&&!/\.css$/i.test(p))urls[p]=URL.createObjectURL(new Blob([decode(f.data)],{type:f.type}));
const resolve=(base,p)=>{try{const u=new URL(p,'https://package.invalid/'+base);return u.origin==='https://package.invalid'?decodeURIComponent(u.pathname.slice(1)):null}catch{return null}};
const resource=(base,value)=>{const key=resolve(base,value);return urls[key]?urls[key]+new URL(value,'https://package.invalid/'+base).hash:null};
const srcset=(base,value)=>{
 let position=0,cursor=0,result='';
 while(position<value.length){
  while(position<value.length&&/[,\t\n\f\r ]/.test(value[position]))position++;
  const start=position;while(position<value.length&&!/[\t\n\f\r ]/.test(value[position]))position++;
  let end=position;while(end>start&&value[end-1]===',')end--;
  if(end===start)break;
  const url=value.slice(start,end);result+=value.slice(cursor,start)+(resource(base,url)||url);cursor=end;
  if(end<position)continue;
  while(position<value.length&&value[position]!==',')position++;
 }
 return result+value.slice(cursor);
};
for(const [p,f] of Object.entries(files))if(/\.css$/i.test(p)){
 const css=new TextDecoder().decode(decode(f.data)).replace(/url\(\s*(['"]?)([^)'"\s]+)\1\s*\)/g,(all,q,v)=>{const target=resource(p,v);return target?'url("'+target+'")':all});
 urls[p]=URL.createObjectURL(new Blob([css],{type:'text/css'}));
}
const render=p=>{
 if(html[p])return html[p]; const f=files[p];if(!f)return null;
 const doc=new DOMParser().parseFromString(new TextDecoder().decode(decode(f.data)),'text/html');
 doc.querySelectorAll('base').forEach(e=>e.remove());
 for(const el of doc.querySelectorAll('[srcset]'))el.setAttribute('srcset',srcset(p,el.getAttribute('srcset')));
 for(const el of doc.querySelectorAll('[src],[href],[poster]'))for(const a of ['src','href','poster'])if(el.hasAttribute(a)){
  const v=el.getAttribute(a),k=resolve(p,v);if(!k||!files[k]||(el.tagName==='A'&&v.startsWith('#')))continue;
  if(el.tagName==='A'&&/\.html?$/i.test(k)){el.dataset.vksPage=k+new URL(v,'https://package.invalid/'+p).hash;el.setAttribute(a,'#');}
  else if(urls[k])el.setAttribute(a,resource(p,v));
 }
 const boot=doc.createElement('script');boot.textContent='window.__prototypePackPage='+JSON.stringify({path:p})+';document.addEventListener("click",e=>{const a=e.target.closest("a[data-vks-page]");if(a){e.preventDefault();parent.__prototypePack.show(a.dataset.vksPage)}},true);';
 (doc.head||doc.documentElement).prepend(boot);
 return html[p]=URL.createObjectURL(new Blob(['<!doctype html>'+doc.documentElement.outerHTML],{type:'text/html'}));
};
window.__prototypePack={show:p=>{
 const target=new URL(p,'https://package.invalid/'),path=decodeURIComponent(target.pathname.slice(1)),fragment=target.hash.slice(1),u=render(path);if(!u)return;
 const frame=document.getElementById('actualPage'),reveal=()=>{if(!fragment)return;try{
  const win=frame.contentWindow,scroll=()=>win.requestAnimationFrame(()=>win.document.getElementById(decodeURIComponent(fragment))?.scrollIntoView());
  win.addEventListener('hashchange',scroll,{once:true});const before=win.location.href;win.location.hash=fragment;
  if(win.location.href===before)win.dispatchEvent(new win.HashChangeEvent('hashchange',{oldURL:before,newURL:before}));
 }catch{}};
 let current='';try{current=frame.contentWindow?.__prototypePackPage?.path||''}catch{}
 if(current===path){reveal();return;}
 frame.onload=reveal;frame.src=u+(fragment?'#'+fragment:'');
}};
window.__prototypePack.show(ENTRY);
""".replace("ENTRY", json.dumps(entry))
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>原型资料包</title>"
        "<style>html,body,iframe{margin:0;width:100%;height:100%;border:0}</style></head><body>"
        '<iframe id="actualPage" title="原型页面"></iframe><script id="vks-site" type="application/json">'
        + data
        + "</script><script>"
        + script
        + "</script></body></html>"
    ).encode()
